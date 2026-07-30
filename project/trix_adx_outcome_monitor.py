"""PAPER position management for the TRIX + ADX strategy."""
from __future__ import annotations

import json
from typing import Any

import pandas as pd

from project.daily_paper_position_monitor import DailyPaperPositionMonitor
from project.shared.events import Event, EventType
from project.trix_adx_scanner import RUNTIME_VERSION


class TrixAdxOutcomeMonitor(DailyPaperPositionMonitor):
    """Manage net break-even at +1R, 50% TP at +2R, and a trailing final leg."""

    def __init__(
        self,
        *args: Any,
        trix_length: int = 20,
        trix_signal_length: int = 9,
        trailing_atr_multiple: float = 2.5,
        break_even_buffer_rate: float = 0.0002,
        sell_fee_rate: float = 0.001,
        spread_rate: float = 0.0005,
        slippage_rate: float = 0.0,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.trix_length = max(2, int(trix_length))
        self.trix_signal_length = max(2, int(trix_signal_length))
        self.trailing_atr_multiple = max(0.5, float(trailing_atr_multiple))
        self.break_even_buffer_rate = max(0.0, float(break_even_buffer_rate))
        self.sell_fee_rate = max(0.0, float(sell_fee_rate))
        self.spread_rate = max(0.0, float(spread_rate))
        self.slippage_rate = max(0.0, float(slippage_rate))
        self.logger = __import__(
            "project.shared.logging", fromlist=["get_module_logger"]
        ).get_module_logger("trix-adx-position-monitor")

    def fetch_open_signals(self) -> list[dict[str, Any]]:
        rows = self.postgres.fetch_all(
            """SELECT id, strategy, pair, timeframe, regime, entry, stop_loss, take_profit,
                      score, probability, signal_class,
                      net_profit_tp1_eur, net_loss_sl_eur, net_rr,
                      reference_candle_time, created_at, telegram_sent_at,
                      COALESCE(exchange, 'Kraken'), quantity,
                      COALESCE(estimated_buy_fee_eur, 0)
               FROM signals.generated_signals
               WHERE status IN ('NEW', 'OPEN')
                 AND telegram_sent_at IS NOT NULL
                 AND score_breakdown->>'runtime_version' = %s
               ORDER BY created_at ASC
               LIMIT %s""",
            (RUNTIME_VERSION, self.max_signals_per_cycle),
        )
        result: list[dict[str, Any]] = []
        for row in rows:
            result.append(
                {
                    "id": int(row[0]),
                    "strategy": str(row[1]),
                    "pair": str(row[2]),
                    "timeframe": str(row[3]),
                    "regime": str(row[4]),
                    "entry": float(row[5]),
                    "stop_loss": float(row[6]),
                    "take_profit": float(row[7]),
                    "score": float(row[8]),
                    "probability": float(row[9]) if row[9] is not None else None,
                    "signal_class": str(row[10]),
                    "net_profit_tp1_eur": float(row[11] or 0.0),
                    "net_loss_sl_eur": float(row[12] or 0.0),
                    "net_rr": float(row[13] or 0.0),
                    "reference_candle_time": row[14],
                    "created_at": row[15],
                    "telegram_sent_at": row[16],
                    "exchange": str(row[17] or "Kraken"),
                    "quantity": float(row[18] or 0.0),
                    "estimated_buy_fee_eur": float(row[19] or 0.0),
                    "execution_mode": "PAPER_DAILY",
                }
            )
        return result

    def _load_state(self, signal_id: int) -> dict[str, Any]:
        rows = self.postgres.fetch_all(
            """SELECT COALESCE(score_breakdown, '{}'::jsonb)
               FROM signals.generated_signals
               WHERE id = %s""",
            (signal_id,),
        )
        raw = rows[0][0] if rows else {}
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except json.JSONDecodeError:
                raw = {}
        return dict(raw or {})

    def _patch_state(
        self,
        signal_id: int,
        patch: dict[str, Any],
        *,
        stop_loss: float | None = None,
        status: str | None = None,
    ) -> None:
        assignments = [
            "score_breakdown = COALESCE(score_breakdown, '{}'::jsonb) || %s::jsonb"
        ]
        params: list[Any] = [json.dumps(patch)]
        if stop_loss is not None:
            assignments.append("stop_loss = %s")
            params.append(stop_loss)
        if status is not None:
            assignments.append("status = %s")
            params.append(status)
        params.append(signal_id)
        self.postgres.execute(
            f"""UPDATE signals.generated_signals
                SET {', '.join(assignments)}
                WHERE id = %s AND status IN ('NEW', 'OPEN')""",
            tuple(params),
        )

    def _leg_net(
        self,
        signal: dict[str, Any],
        exit_price: float,
        fraction: float,
    ) -> float:
        fraction = min(1.0, max(0.0, float(fraction)))
        quantity = float(signal.get("quantity") or 0.0) * fraction
        entry = float(signal["entry"])
        gross = quantity * (exit_price - entry)
        buy_fee = float(signal.get("estimated_buy_fee_eur") or 0.0) * fraction
        sell_fee = quantity * exit_price * self.sell_fee_rate
        half_spread = self.spread_rate / 2.0
        spread_cost = quantity * (
            (entry * half_spread) + (exit_price * half_spread)
        )
        slippage_cost = quantity * (
            (entry * self.slippage_rate) + (exit_price * self.slippage_rate)
        )
        return gross - buy_fee - sell_fee - spread_cost - slippage_cost

    def true_break_even_price(self, signal: dict[str, Any]) -> float:
        entry = float(signal["entry"])
        quantity = float(signal.get("quantity") or 0.0)
        if quantity <= 0:
            return entry * (1.0 + self.break_even_buffer_rate)
        buy_fee = float(signal.get("estimated_buy_fee_eur") or 0.0)
        friction = (self.spread_rate / 2.0) + self.slippage_rate
        denominator = quantity * (1.0 - self.sell_fee_rate - friction)
        if denominator <= 0:
            return entry * (1.0 + self.break_even_buffer_rate)
        numerator = quantity * entry * (1.0 + friction) + buy_fee
        return (numerator / denominator) * (1.0 + self.break_even_buffer_rate)

    def _publish_break_even(
        self,
        signal: dict[str, Any],
        trigger_price: float,
        break_even_price: float,
        timestamp: Any,
    ) -> None:
        message = (
            "🟡 <b>BREAK EVEN NETTO</b> "
            f"<code>#{signal['id']}</code>\n"
            f"<b>{signal['pair']}</b> · 1h\n\n"
            f"Prezzo arrivato a +1R: <b>{trigger_price:.8f}</b>\n"
            f"Stop spostato a: <b>{break_even_price:.8f}</b>\n"
            "Il nuovo stop include costi stimati e un piccolo buffer.\n"
            "Posizione aperta: <b>100%</b>\n"
            "Prossimo obiettivo: <b>TP1 50% a +2R</b>"
        )
        self.event_bus.publish(
            Event(
                EventType.REPORT_READY,
                {
                    "message": message,
                    "trusted_html": True,
                    "management_update": "BREAK_EVEN_ARMED",
                    "signal_id": signal["id"],
                    "pair": signal["pair"],
                    "timeframe": signal["timeframe"],
                    "entry": signal["entry"],
                    "break_even_price": break_even_price,
                    "trigger_price": trigger_price,
                    "trigger_label": "+1R",
                    "next_objective": "TP1 50% a +2R",
                    "timestamp": str(timestamp),
                },
            )
        )

    def _arm_break_even(
        self,
        signal: dict[str, Any],
        state: dict[str, Any],
        timestamp: Any,
    ) -> None:
        entry = float(signal["entry"])
        risk = float(
            state.get("risk_distance")
            or (
                entry
                - float(state.get("initial_stop") or signal["stop_loss"])
            )
        )
        trigger_price = entry + risk
        break_even_price = self.true_break_even_price(signal)
        patch = {
            "breakeven_armed": True,
            "breakeven_price": break_even_price,
            "breakeven_armed_at": str(timestamp),
        }
        self._patch_state(
            signal["id"],
            patch,
            stop_loss=break_even_price,
            status="OPEN",
        )
        signal["stop_loss"] = break_even_price
        state.update(patch)
        self._publish_break_even(
            signal,
            trigger_price,
            break_even_price,
            timestamp,
        )
        self.logger.info(
            "TRIX_BREAK_EVEN_ARMED id=%s pair=%s trigger=%.8f stop=%.8f",
            signal["id"],
            signal["pair"],
            trigger_price,
            break_even_price,
        )

    def _take_partial_profit(
        self,
        signal: dict[str, Any],
        state: dict[str, Any],
        tp1_price: float,
        timestamp: Any,
        open_price: float,
        high_price: float,
        low_price: float,
        close_price: float,
    ) -> None:
        partial_fraction = 0.5
        partial_net = self._leg_net(signal, tp1_price, partial_fraction)
        break_even_price = float(
            state.get("breakeven_price") or self.true_break_even_price(signal)
        )
        patch = {
            "tp1_hit": True,
            "tp1_price": tp1_price,
            "tp1_fraction": partial_fraction,
            "tp1_realized_net_eur": partial_net,
            "tp1_hit_at": str(timestamp),
            "remaining_fraction": 0.5,
            "highest_high_since_tp1": high_price,
        }
        self._patch_state(
            signal["id"],
            patch,
            stop_loss=max(float(signal["stop_loss"]), break_even_price),
            status="OPEN",
        )
        signal["stop_loss"] = max(float(signal["stop_loss"]), break_even_price)
        state.update(patch)
        payload = {
            **signal,
            "signal_id": signal["id"],
            "outcome": "TARGET_HIT",
            "outcome_price": tp1_price,
            "outcome_open_price": open_price,
            "outcome_high_price": high_price,
            "outcome_low_price": low_price,
            "close_price": close_price,
            "closed_at": str(timestamp),
            "outcome_resolution": "TRIX_TP1_2R",
            "partial_take_profit": True,
            "partial_fraction": partial_fraction,
            "remaining_fraction": 0.5,
            "realized_net_eur": partial_net,
            "net_profit_tp1_eur": max(partial_net, 0.0),
            "net_loss_sl_eur": max(-partial_net, 0.0),
        }
        self.event_bus.publish(Event(EventType.TARGET_HIT, payload))
        self.logger.info(
            "TRIX_TP1_HIT id=%s pair=%s price=%.8f net=%.4f",
            signal["id"],
            signal["pair"],
            tp1_price,
            partial_net,
        )

    def _fetch_closed_frame(self, signal: dict[str, Any]) -> pd.DataFrame:
        requested_exchange = str(signal.get("exchange") or "Kraken")
        rows = self.postgres.fetch_all(
            """SELECT timestamp, open, high, low, close
               FROM market_data.ohlc
               WHERE LOWER(TRIM(exchange)) = LOWER(TRIM(%s))
                 AND pair = %s AND timeframe = '1h'
               ORDER BY timestamp DESC
               LIMIT 400""",
            (requested_exchange, signal["pair"]),
        )
        if not rows:
            rows = self.postgres.fetch_all(
                """SELECT timestamp, open, high, low, close
                   FROM market_data.ohlc
                   WHERE pair = %s AND timeframe = '1h'
                   ORDER BY timestamp DESC
                   LIMIT 400""",
                (signal["pair"],),
            )
        frame = pd.DataFrame(
            rows,
            columns=["timestamp", "open", "high", "low", "close"],
        )
        if frame.empty:
            return frame
        frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
        for column in ["open", "high", "low", "close"]:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
        now_seconds = pd.Timestamp.now(tz="UTC").timestamp()
        candle_end = frame["timestamp"].astype("int64") // 1_000_000_000 + 3600
        frame = frame[candle_end <= now_seconds]
        return frame.sort_values("timestamp").reset_index(drop=True)

    def _add_indicators(self, frame: pd.DataFrame) -> pd.DataFrame:
        data = frame.copy()
        close = data["close"].astype(float)
        high = data["high"].astype(float)
        low = data["low"].astype(float)
        data["ema50"] = close.ewm(span=50, adjust=False).mean()
        previous_close = close.shift(1)
        true_range = pd.concat(
            [
                high - low,
                (high - previous_close).abs(),
                (low - previous_close).abs(),
            ],
            axis=1,
        ).max(axis=1)
        data["atr"] = true_range.ewm(alpha=1 / 14, adjust=False).mean()
        ema1 = close.ewm(span=self.trix_length, adjust=False).mean()
        ema2 = ema1.ewm(span=self.trix_length, adjust=False).mean()
        ema3 = ema2.ewm(span=self.trix_length, adjust=False).mean()
        data["trix"] = ema3.pct_change() * 100.0
        data["trix_signal"] = data["trix"].ewm(
            span=self.trix_signal_length,
            adjust=False,
        ).mean()
        return data

    @staticmethod
    def _to_utc(value: Any) -> pd.Timestamp:
        timestamp = pd.Timestamp(value)
        return (
            timestamp.tz_localize("UTC")
            if timestamp.tzinfo is None
            else timestamp.tz_convert("UTC")
        )

    def _mark_processed(
        self,
        signal_id: int,
        timestamp: Any,
        extra: dict[str, Any] | None = None,
    ) -> None:
        patch = {"last_processed_candle_time": str(timestamp)}
        if extra:
            patch.update(extra)
        self._patch_state(signal_id, patch)

    def check_signal(self, signal: dict[str, Any]) -> bool:
        frame = self._fetch_closed_frame(signal)
        if frame.empty:
            return False
        data = self._add_indicators(frame)
        state = self._load_state(signal["id"])
        entry = float(signal["entry"])
        initial_stop = float(state.get("initial_stop") or signal["stop_loss"])
        risk = float(state.get("risk_distance") or (entry - initial_stop))
        if risk <= 0:
            self.logger.warning(
                "TRIX_MANAGEMENT_INVALID_RISK id=%s pair=%s",
                signal["id"],
                signal["pair"],
            )
            return False

        delivery = self._to_utc(
            signal.get("telegram_sent_at")
            or signal.get("created_at")
            or signal.get("reference_candle_time")
        )
        first_candle_open = delivery.floor("1h")
        last_processed_raw = state.get("last_processed_candle_time")
        last_processed = (
            self._to_utc(last_processed_raw) if last_processed_raw else None
        )

        for index in range(1, len(data)):
            latest = data.iloc[index]
            previous = data.iloc[index - 1]
            timestamp = self._to_utc(latest["timestamp"])
            if timestamp < first_candle_open:
                continue
            if last_processed is not None and timestamp <= last_processed:
                continue

            open_price = float(latest["open"])
            high_price = float(latest["high"])
            low_price = float(latest["low"])
            close_price = float(latest["close"])
            current_stop = float(signal["stop_loss"])

            if low_price <= current_stop:
                resolution = (
                    "TRIX_TRAILING_STOP"
                    if bool(state.get("tp1_hit"))
                    else "TRIX_NET_BREAK_EVEN_STOP"
                    if bool(state.get("breakeven_armed"))
                    else "TRIX_INITIAL_STOP"
                )
                return self._close_signal(
                    signal,
                    outcome_price=current_stop,
                    timestamp=timestamp,
                    open_price=open_price,
                    high_price=high_price,
                    low_price=low_price,
                    close_price=close_price,
                    resolution=resolution,
                    state=state,
                )

            be_trigger = entry + risk
            tp1_price = entry + (2.0 * risk)

            if (
                not bool(state.get("breakeven_armed"))
                and high_price >= be_trigger
            ):
                self._arm_break_even(signal, state, timestamp)

            if not bool(state.get("tp1_hit")) and high_price >= tp1_price:
                self._take_partial_profit(
                    signal,
                    state,
                    tp1_price,
                    timestamp,
                    open_price,
                    high_price,
                    low_price,
                    close_price,
                )

            trix_now = float(latest["trix"]) if not pd.isna(latest["trix"]) else 0.0
            trix_previous = (
                float(previous["trix"])
                if not pd.isna(previous["trix"])
                else trix_now
            )
            ema50 = float(latest["ema50"])
            bearish_trix_cross = trix_previous >= 0 > trix_now
            close_below_ema50 = close_price < ema50
            if bearish_trix_cross or close_below_ema50:
                resolution = (
                    "TRIX_ZERO_CROSS_EXIT"
                    if bearish_trix_cross
                    else "TRIX_EMA50_EXIT"
                )
                return self._close_signal(
                    signal,
                    outcome_price=close_price,
                    timestamp=timestamp,
                    open_price=open_price,
                    high_price=high_price,
                    low_price=low_price,
                    close_price=close_price,
                    resolution=resolution,
                    state=state,
                )

            patch: dict[str, Any] = {}
            if bool(state.get("tp1_hit")):
                highest = max(
                    float(state.get("highest_high_since_tp1") or high_price),
                    high_price,
                )
                current_atr = float(latest["atr"])
                break_even_price = float(
                    state.get("breakeven_price")
                    or self.true_break_even_price(signal)
                )
                trailing_candidate = highest - (
                    current_atr * self.trailing_atr_multiple
                )
                new_stop = max(
                    float(signal["stop_loss"]),
                    break_even_price,
                    trailing_candidate,
                )
                patch["highest_high_since_tp1"] = highest
                if new_stop > float(signal["stop_loss"]):
                    patch["trailing_stop"] = new_stop
                    patch["trailing_stop_updated_at"] = str(timestamp)
                    self._patch_state(
                        signal["id"],
                        patch,
                        stop_loss=new_stop,
                        status="OPEN",
                    )
                    signal["stop_loss"] = new_stop
                    state.update(patch)
                    patch = {}

            self._mark_processed(signal["id"], timestamp, patch)
            state["last_processed_candle_time"] = str(timestamp)
            last_processed = timestamp

        return False

    def _close_signal(
        self,
        signal: dict[str, Any],
        *,
        outcome_price: float,
        timestamp: Any,
        open_price: float,
        high_price: float,
        low_price: float,
        close_price: float,
        resolution: str,
        state: dict[str, Any],
    ) -> bool:
        tp1_hit = bool(state.get("tp1_hit"))
        prior_net = float(state.get("tp1_realized_net_eur") or 0.0)
        remaining_fraction = float(
            state.get("remaining_fraction") or (0.5 if tp1_hit else 1.0)
        )
        final_leg_net = self._leg_net(
            signal,
            outcome_price,
            remaining_fraction,
        )
        realized_net = prior_net + final_leg_net
        persisted_outcome = "TARGET_HIT" if realized_net >= 0 else "STOP_LOSS"
        net_profit = max(realized_net, 0.0)
        net_loss = max(-realized_net, 0.0)
        closing_patch = {
            "final_exit_fraction": remaining_fraction,
            "final_leg_net_eur": final_leg_net,
            "total_realized_net_eur": realized_net,
            "last_processed_candle_time": str(timestamp),
        }
        self.postgres.execute(
            """UPDATE signals.generated_signals
               SET status = %s,
                   outcome_price = %s,
                   outcome_open_price = %s,
                   outcome_high_price = %s,
                   outcome_low_price = %s,
                   outcome_close_price = %s,
                   outcome_candle_time = %s,
                   closed_at = NOW(),
                   outcome_ambiguous = FALSE,
                   outcome_resolution = %s,
                   net_profit_tp1_eur = %s,
                   net_loss_sl_eur = %s,
                   score_breakdown = COALESCE(score_breakdown, '{}'::jsonb) || %s::jsonb
               WHERE id = %s AND status IN ('NEW', 'OPEN')""",
            (
                persisted_outcome,
                outcome_price,
                open_price,
                high_price,
                low_price,
                close_price,
                timestamp,
                resolution,
                net_profit,
                net_loss,
                json.dumps(closing_patch),
                signal["id"],
            ),
        )
        event_type = (
            EventType.TARGET_HIT
            if persisted_outcome == "TARGET_HIT"
            else EventType.STOP_LOSS
        )
        payload = {
            **signal,
            "signal_id": signal["id"],
            "outcome": persisted_outcome,
            "outcome_price": outcome_price,
            "outcome_open_price": open_price,
            "outcome_high_price": high_price,
            "outcome_low_price": low_price,
            "close_price": close_price,
            "closed_at": str(timestamp),
            "ambiguous": False,
            "outcome_resolution": resolution,
            "realized_net_eur": realized_net,
            "net_profit_tp1_eur": net_profit,
            "net_loss_sl_eur": net_loss,
            "tp1_hit": tp1_hit,
            "tp1_realized_net_eur": prior_net,
            "remaining_fraction": remaining_fraction,
        }
        self.event_bus.publish(Event(event_type, payload))
        self.logger.info(
            "TRIX_POSITION_CLOSED id=%s pair=%s status=%s resolution=%s exit=%.8f net=%.4f",
            signal["id"],
            signal["pair"],
            persisted_outcome,
            resolution,
            outcome_price,
            realized_net,
        )
        return True
