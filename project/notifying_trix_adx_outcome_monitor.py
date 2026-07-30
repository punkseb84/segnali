"""Retryable and delivery-aware monitoring for TRIX + ADX PAPER positions."""
from __future__ import annotations

import json
from typing import Any

import pandas as pd

from project.trix_adx_outcome_monitor import TrixAdxOutcomeMonitor
from project.trix_adx_scanner import RUNTIME_VERSION


class NotifyingTrixAdxOutcomeMonitor(TrixAdxOutcomeMonitor):
    """Retry unsent updates and ignore pre-Telegram range on the delivery candle."""

    @staticmethod
    def _state_dict(raw: Any) -> dict[str, Any]:
        if isinstance(raw, dict):
            return dict(raw)
        if isinstance(raw, str):
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                return {}
            return dict(parsed) if isinstance(parsed, dict) else {}
        return {}

    def _backfill_pending_break_even_notifications(self) -> int:
        rows = self.postgres.fetch_all(
            """SELECT id, pair, timeframe, entry, quantity,
                      COALESCE(estimated_buy_fee_eur, 0),
                      COALESCE(score_breakdown, '{}'::jsonb)
               FROM signals.generated_signals
               WHERE status IN ('NEW', 'OPEN')
                 AND score_breakdown->>'runtime_version' = %s
                 AND COALESCE(
                       (score_breakdown->>'breakeven_armed')::boolean,
                       FALSE
                     ) = TRUE
                 AND COALESCE(
                       (score_breakdown->>'breakeven_notification_sent')::boolean,
                       FALSE
                     ) = FALSE
               ORDER BY created_at ASC
               LIMIT %s""",
            (RUNTIME_VERSION, self.max_signals_per_cycle),
        )
        published = 0
        for signal_id, pair, timeframe, entry, quantity, buy_fee, state_raw in rows:
            state = self._state_dict(state_raw)
            signal = {
                "id": int(signal_id),
                "pair": str(pair or "n/d"),
                "timeframe": str(timeframe or "1h"),
                "entry": float(entry),
                "quantity": float(quantity or 0.0),
                "estimated_buy_fee_eur": float(buy_fee or 0.0),
            }
            risk = float(state.get("risk_distance") or 0.0)
            trigger_price = float(entry) + risk
            break_even_price = float(
                state.get("breakeven_price")
                or self.true_break_even_price(signal)
            )
            timestamp = state.get("breakeven_armed_at") or "già registrato"
            self._publish_break_even(
                signal,
                trigger_price,
                break_even_price,
                timestamp,
            )
            published += 1

        if published:
            self.logger.info(
                "TRIX_BREAK_EVEN_BACKFILL published=%s",
                published,
            )
        return published

    def monitor_open_signals(self) -> int:
        self._backfill_pending_break_even_notifications()
        signals = self.fetch_open_signals()
        closed = 0
        for signal in signals:
            closed += 1 if self.check_signal(signal) else 0
        if signals:
            self.logger.info(
                "TRIX_POSITION_MONITOR checked=%s closed=%s",
                len(signals),
                closed,
            )
        return closed

    def check_signal(self, signal: dict[str, Any]) -> bool:
        """Use only the close on the candle that contains Telegram delivery.

        Its high/low may include price action from the first minutes of the hour,
        before the user could have received the signal. Every later closed candle
        is evaluated with its full range and conservative stop-first ordering.
        """
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
        delivery_candle_open = delivery.floor("1h")
        last_processed_raw = state.get("last_processed_candle_time")
        last_processed = (
            self._to_utc(last_processed_raw) if last_processed_raw else None
        )

        for index in range(1, len(data)):
            latest = data.iloc[index]
            previous = data.iloc[index - 1]
            timestamp = self._to_utc(latest["timestamp"])
            if timestamp < delivery_candle_open:
                continue
            if last_processed is not None and timestamp <= last_processed:
                continue

            raw_open = float(latest["open"])
            raw_high = float(latest["high"])
            raw_low = float(latest["low"])
            close_price = float(latest["close"])
            is_delivery_candle = timestamp == delivery_candle_open

            management_open = close_price if is_delivery_candle else raw_open
            management_high = close_price if is_delivery_candle else raw_high
            management_low = close_price if is_delivery_candle else raw_low
            current_stop = float(signal["stop_loss"])

            if management_low <= current_stop:
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
                    open_price=management_open,
                    high_price=management_high,
                    low_price=management_low,
                    close_price=close_price,
                    resolution=resolution,
                    state=state,
                )

            be_trigger = entry + risk
            tp1_price = entry + (2.0 * risk)

            if (
                not bool(state.get("breakeven_armed"))
                and management_high >= be_trigger
            ):
                self._arm_break_even(signal, state, timestamp)

            if (
                not bool(state.get("tp1_hit"))
                and management_high >= tp1_price
            ):
                self._take_partial_profit(
                    signal,
                    state,
                    tp1_price,
                    timestamp,
                    management_open,
                    management_high,
                    management_low,
                    close_price,
                )

            trix_now = (
                float(latest["trix"])
                if not pd.isna(latest["trix"])
                else 0.0
            )
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
                    open_price=management_open,
                    high_price=management_high,
                    low_price=management_low,
                    close_price=close_price,
                    resolution=resolution,
                    state=state,
                )

            patch: dict[str, Any] = {}
            if bool(state.get("tp1_hit")):
                highest = max(
                    float(
                        state.get("highest_high_since_tp1")
                        or management_high
                    ),
                    management_high,
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
