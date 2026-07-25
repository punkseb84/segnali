"""Ichimoku monitor with break-even, partial TP1 and dynamic final exit."""
from __future__ import annotations

import json
from typing import Any

import pandas as pd

from project.ichimoku_position_monitor import IchimokuPositionMonitor
from project.shared.events import Event, EventType


class IchimokuOutcomeMonitor(IchimokuPositionMonitor):
    """Manage +1 ATR break-even, +2 ATR partial profit and Ichimoku final exit."""

    def _load_management_state(self, signal: dict[str, Any]) -> dict[str, Any]:
        rows = self.postgres.fetch_all(
            """SELECT COALESCE(score_breakdown, '{}'::jsonb)
            FROM signals.generated_signals
            WHERE id = %s""",
            (signal["id"],),
        )
        raw = rows[0][0] if rows else {}
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except json.JSONDecodeError:
                raw = {}
        return dict(raw or {})

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
        spread_cost = quantity * ((entry * half_spread) + (exit_price * half_spread))
        slippage_cost = quantity * (
            (entry * self.slippage_rate) + (exit_price * self.slippage_rate)
        )
        return gross - buy_fee - sell_fee - spread_cost - slippage_cost

    def _arm_break_even(
        self,
        signal: dict[str, Any],
        state: dict[str, Any],
        timestamp: Any,
    ) -> None:
        entry = float(signal["entry"])
        patch = {
            "breakeven_armed": True,
            "breakeven_price": entry,
            "breakeven_armed_at": str(timestamp),
        }
        self.postgres.execute(
            """UPDATE signals.generated_signals
            SET stop_loss = %s,
                status = 'OPEN',
                score_breakdown = COALESCE(score_breakdown, '{}'::jsonb) || %s::jsonb
            WHERE id = %s AND status IN ('NEW', 'OPEN')""",
            (entry, json.dumps(patch), signal["id"]),
        )
        signal["stop_loss"] = entry
        state.update(patch)
        self.logger.info(
            "ICHIMOKU_BREAK_EVEN_ARMED id=%s pair=%s stop=%.8f",
            signal["id"],
            signal["pair"],
            entry,
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
        patch = {
            "breakeven_armed": True,
            "breakeven_price": float(signal["entry"]),
            "tp1_hit": True,
            "tp1_price": tp1_price,
            "tp1_fraction": partial_fraction,
            "tp1_realized_net_eur": partial_net,
            "tp1_hit_at": str(timestamp),
            "remaining_fraction": 0.5,
        }
        self.postgres.execute(
            """UPDATE signals.generated_signals
            SET stop_loss = entry,
                status = 'OPEN',
                score_breakdown = COALESCE(score_breakdown, '{}'::jsonb) || %s::jsonb
            WHERE id = %s AND status IN ('NEW', 'OPEN')""",
            (json.dumps(patch), signal["id"]),
        )
        signal["stop_loss"] = float(signal["entry"])
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
            "outcome_resolution": "ICHIMOKU_TP1_2ATR",
            "partial_take_profit": True,
            "partial_fraction": partial_fraction,
            "remaining_fraction": 0.5,
            "realized_net_eur": partial_net,
            "net_profit_tp1_eur": max(partial_net, 0.0),
            "net_loss_sl_eur": max(-partial_net, 0.0),
        }
        self.event_bus.publish(Event(EventType.TARGET_HIT, payload))
        self.logger.info(
            "ICHIMOKU_TP1_HIT id=%s pair=%s price=%.8f fraction=%.2f net=%.4f",
            signal["id"],
            signal["pair"],
            tp1_price,
            partial_fraction,
            partial_net,
        )

    def check_signal(self, signal: dict[str, Any]) -> bool:
        rows = self.postgres.fetch_all(
            """SELECT timestamp, open, high, low, close
            FROM market_data.ohlc
            WHERE exchange = %s AND pair = %s AND timeframe = %s
            ORDER BY timestamp DESC
            LIMIT 320""",
            (signal["exchange"], signal["pair"], signal["timeframe"]),
        )
        if not rows:
            return False

        frame = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close"])
        frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
        for column in ["open", "high", "low", "close"]:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
        frame = frame.sort_values("timestamp").reset_index(drop=True)

        now_seconds = pd.Timestamp.now(tz="UTC").timestamp()
        seconds = 3600 if signal["timeframe"] == "1h" else 900
        candle_end = frame["timestamp"].astype("int64") // 1_000_000_000 + seconds
        frame = frame[candle_end <= now_seconds].reset_index(drop=True)
        data = self._add_ichimoku(frame)

        start = pd.Timestamp(signal.get("reference_candle_time") or signal["created_at"])
        start = start.tz_localize("UTC") if start.tzinfo is None else start.tz_convert("UTC")
        state = self._load_management_state(signal)
        atr = float(state.get("atr") or 0.0)
        if atr <= 0:
            self.logger.warning(
                "ICHIMOKU_MANAGEMENT_MISSING_ATR id=%s pair=%s",
                signal["id"],
                signal["pair"],
            )
            return super().check_signal(signal)

        entry = float(signal["entry"])
        break_even_trigger = entry + atr
        tp1_price = entry + (2.0 * atr)

        for index in range(1, len(data)):
            latest = data.iloc[index]
            previous = data.iloc[index - 1]
            timestamp = pd.Timestamp(latest["timestamp"])
            if timestamp <= start:
                continue

            open_price = float(latest["open"])
            high_price = float(latest["high"])
            low_price = float(latest["low"])
            close_price = float(latest["close"])
            current_stop = float(signal["stop_loss"])

            # Existing stop always has priority. This is conservative when the
            # intrabar order of high and low is unknown.
            if low_price <= current_stop:
                resolution = (
                    "ICHIMOKU_BREAK_EVEN_STOP"
                    if bool(state.get("breakeven_armed"))
                    else "ATR_STOP_ICHIMOKU"
                )
                return self._close_signal(
                    signal,
                    outcome="STOP_LOSS",
                    outcome_price=current_stop,
                    timestamp=timestamp,
                    open_price=open_price,
                    high_price=high_price,
                    low_price=low_price,
                    close_price=close_price,
                    resolution=resolution,
                    state=state,
                )

            if not bool(state.get("breakeven_armed")) and high_price >= break_even_trigger:
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

            values = [latest["cloud_top"], latest["tenkan"], latest["kijun"]]
            if any(pd.isna(value) for value in values):
                continue

            cloud_exit = close_price <= float(latest["cloud_top"])
            bearish_cross = (
                float(latest["tenkan"]) < float(latest["kijun"])
                and not pd.isna(previous["tenkan"])
                and not pd.isna(previous["kijun"])
                and float(previous["tenkan"]) >= float(previous["kijun"])
            )
            if not cloud_exit and not bearish_cross:
                continue

            resolution = (
                "ICHIMOKU_CLOUD_EXIT"
                if cloud_exit
                else "ICHIMOKU_TENKAN_KIJUN_EXIT"
            )
            return self._close_signal(
                signal,
                outcome="TARGET_HIT",
                outcome_price=close_price,
                timestamp=timestamp,
                open_price=open_price,
                high_price=high_price,
                low_price=low_price,
                close_price=close_price,
                resolution=resolution,
                state=state,
            )
        return False

    def _close_signal(
        self,
        signal: dict[str, Any],
        *,
        outcome: str,
        outcome_price: float,
        timestamp: Any,
        open_price: float,
        high_price: float,
        low_price: float,
        close_price: float,
        resolution: str,
        state: dict[str, Any] | None = None,
    ) -> bool:
        management = state or self._load_management_state(signal)
        tp1_hit = bool(management.get("tp1_hit"))
        prior_net = float(management.get("tp1_realized_net_eur") or 0.0)
        remaining_fraction = float(management.get("remaining_fraction") or (0.5 if tp1_hit else 1.0))
        final_leg_net = self._leg_net(signal, outcome_price, remaining_fraction)
        realized_net = prior_net + final_leg_net
        persisted_outcome = "TARGET_HIT" if realized_net >= 0 else "STOP_LOSS"

        net_profit = max(realized_net, 0.0)
        net_loss = max(-realized_net, 0.0)
        closing_patch = {
            "final_exit_fraction": remaining_fraction,
            "final_leg_net_eur": final_leg_net,
            "total_realized_net_eur": realized_net,
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
            EventType.STOP_LOSS
            if persisted_outcome == "STOP_LOSS"
            else EventType.TARGET_HIT
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
            "ICHIMOKU_POSITION_CLOSED id=%s pair=%s status=%s resolution=%s exit=%.8f tp1=%s net=%.4f",
            signal["id"],
            signal["pair"],
            persisted_outcome,
            resolution,
            outcome_price,
            tp1_hit,
            realized_net,
        )
        return True
