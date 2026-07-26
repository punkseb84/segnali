"""Ichimoku outcome monitor with retryable Telegram break-even updates."""
from __future__ import annotations

from typing import Any

import pandas as pd

from project.ichimoku_outcome_monitor import IchimokuOutcomeMonitor
from project.shared.events import Event, EventType


class NotifyingIchimokuOutcomeMonitor(IchimokuOutcomeMonitor):
    """Publish Telegram updates for new and previously armed break-even states."""

    def _publish_break_even_update(
        self,
        *,
        signal_id: Any,
        pair: str,
        timeframe: str,
        entry: float,
        timestamp: Any,
        backfilled: bool = False,
    ) -> None:
        note = (
            "\n• Notifica recuperata dopo aggiornamento del bot"
            if backfilled
            else ""
        )
        message = (
            "🟡 <b>STOP SPOSTATO A BREAK EVEN</b>\n"
            f"🆔 <code>#{signal_id}</code>\n\n"
            f"<b>{pair}</b> · {timeframe}\n"
            f"• Entry: <b>{entry:.8f}</b>\n"
            f"• Nuovo Stop Loss: <b>{entry:.8f}</b>\n"
            "• Condizione raggiunta: <b>+1 ATR</b>\n"
            f"• Candela di conferma: <b>{timestamp}</b>{note}\n\n"
            "La posizione resta aperta. Il prossimo obiettivo è TP1 a +2 ATR."
        )
        self.event_bus.publish(
            Event(
                EventType.REPORT_READY,
                {
                    "message": message,
                    "trusted_html": True,
                    "signal_id": signal_id,
                    "pair": pair,
                    "timeframe": timeframe,
                    "management_update": "BREAK_EVEN_ARMED",
                },
            )
        )
        self.logger.info(
            "ICHIMOKU_BREAK_EVEN_NOTIFICATION_PUBLISHED id=%s pair=%s backfilled=%s",
            signal_id,
            pair,
            backfilled,
        )

    def _backfill_pending_break_even_notifications(self) -> int:
        rows = self.postgres.fetch_all(
            """SELECT id, pair, timeframe, entry,
                      score_breakdown->>'breakeven_armed_at'
            FROM signals.generated_signals
            WHERE status IN ('NEW', 'OPEN')
              AND COALESCE((score_breakdown->>'breakeven_armed')::boolean, FALSE) = TRUE
              AND COALESCE((score_breakdown->>'breakeven_notification_sent')::boolean, FALSE) = FALSE
            ORDER BY created_at ASC
            LIMIT %s""",
            (self.max_signals_per_cycle,),
        )
        published = 0
        for signal_id, pair, timeframe, entry, armed_at in rows:
            self._publish_break_even_update(
                signal_id=signal_id,
                pair=str(pair or "n/d"),
                timeframe=str(timeframe or "1h"),
                entry=float(entry),
                timestamp=armed_at or "già registrato nel database",
                backfilled=True,
            )
            published += 1
        return published

    def monitor_open_signals(self) -> int:
        backfilled = self._backfill_pending_break_even_notifications()
        closed = super().monitor_open_signals()
        if backfilled:
            self.logger.info(
                "ICHIMOKU_BREAK_EVEN_BACKFILL published=%s",
                backfilled,
            )
        return closed

    def _resolve_reference_candle(
        self,
        signal: dict[str, Any],
    ) -> tuple[Any, float, float, float, float] | None:
        """Resolve whether the stored reference is an OHLC open or close timestamp.

        Some historical signals store the candle opening timestamp, while others store
        the boundary at which the candle closed. We inspect the two candles immediately
        preceding the stored reference and select the latest candle that had already
        closed when the signal was created. This avoids matching the new, still-open
        candle when a close boundary such as 11:00 is stored.
        """
        reference = signal.get("reference_candle_time")
        if reference is None:
            return None

        rows = self.postgres.fetch_all(
            """SELECT timestamp, open, high, low, close
            FROM market_data.ohlc
            WHERE exchange = %s AND pair = %s AND timeframe = %s
              AND timestamp <= %s
            ORDER BY timestamp DESC
            LIMIT 2""",
            (
                signal["exchange"],
                signal["pair"],
                signal["timeframe"],
                reference,
            ),
        )
        if not rows:
            return None

        seconds = 3600 if signal["timeframe"] == "1h" else 900
        created = pd.Timestamp(signal.get("created_at") or pd.Timestamp.now(tz="UTC"))
        created = created.tz_localize("UTC") if created.tzinfo is None else created.tz_convert("UTC")
        now = pd.Timestamp.now(tz="UTC")

        selected = None
        for row in rows:
            timestamp = pd.Timestamp(row[0])
            timestamp = timestamp.tz_localize("UTC") if timestamp.tzinfo is None else timestamp.tz_convert("UTC")
            candle_end = timestamp + pd.Timedelta(seconds=seconds)
            if candle_end <= created:
                selected = row
                break

        if selected is None:
            for row in rows:
                timestamp = pd.Timestamp(row[0])
                timestamp = timestamp.tz_localize("UTC") if timestamp.tzinfo is None else timestamp.tz_convert("UTC")
                if timestamp + pd.Timedelta(seconds=seconds) <= now:
                    selected = row
                    break

        if selected is None:
            return None

        resolved_timestamp = pd.Timestamp(selected[0])
        self.logger.info(
            "ICHIMOKU_REFERENCE_CANDLE_RESOLVED id=%s pair=%s stored=%s resolved_open=%s",
            signal.get("id"),
            signal.get("pair"),
            reference,
            resolved_timestamp,
        )
        return selected

    def check_signal(self, signal: dict[str, Any]) -> bool:
        """Evaluate the resolved reference close, then monitor later candles normally."""
        resolved = self._resolve_reference_candle(signal)
        normalized_signal = dict(signal)

        if resolved is not None:
            timestamp, open_price, high_price, low_price, close_price = resolved
            timestamp = pd.Timestamp(timestamp)
            normalized_signal["reference_candle_time"] = timestamp

            state = self._load_management_state(signal)
            atr = float(state.get("atr") or 0.0)
            if atr > 0:
                entry = float(signal["entry"])
                close_value = float(close_price)
                break_even_trigger = entry + atr
                tp1_price = entry + (2.0 * atr)

                if (
                    not bool(state.get("breakeven_armed"))
                    and close_value >= break_even_trigger
                ):
                    self.logger.info(
                        "ICHIMOKU_REFERENCE_CANDLE_BREAK_EVEN id=%s pair=%s close=%.8f trigger=%.8f",
                        signal.get("id"),
                        signal.get("pair"),
                        close_value,
                        break_even_trigger,
                    )
                    self._arm_break_even(signal, state, timestamp)

                if (
                    not bool(state.get("tp1_hit"))
                    and close_value >= tp1_price
                ):
                    self.logger.info(
                        "ICHIMOKU_REFERENCE_CANDLE_TP1 id=%s pair=%s close=%.8f trigger=%.8f",
                        signal.get("id"),
                        signal.get("pair"),
                        close_value,
                        tp1_price,
                    )
                    self._take_partial_profit(
                        signal,
                        state,
                        tp1_price,
                        timestamp,
                        float(open_price),
                        float(high_price),
                        float(low_price),
                        close_value,
                    )

        return super().check_signal(normalized_signal)

    def _arm_break_even(
        self,
        signal: dict[str, Any],
        state: dict[str, Any],
        timestamp: Any,
    ) -> None:
        was_armed = bool(state.get("breakeven_armed"))
        super()._arm_break_even(signal, state, timestamp)
        if was_armed or bool(state.get("breakeven_notification_sent")):
            return

        entry = float(signal["entry"])
        signal_id = signal.get("id") or signal.get("signal_id") or "n/d"
        pair = str(signal.get("pair") or "n/d")
        timeframe = str(signal.get("timeframe") or "1h")
        self._publish_break_even_update(
            signal_id=signal_id,
            pair=pair,
            timeframe=timeframe,
            entry=entry,
            timestamp=timestamp,
            backfilled=False,
        )
