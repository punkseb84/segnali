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

    def check_signal(self, signal: dict[str, Any]) -> bool:
        """Evaluate the reference candle close without using its pre-signal range.

        The base monitor skips ``timestamp <= reference_candle_time`` to avoid false
        stop/target hits caused by price action before Telegram delivery. That also
        skipped a legitimate management move when the reference candle *closed*
        above +1 ATR or +2 ATR. Here only the close is inspected for that candle;
        later candles continue to use the full high/low range in the base monitor.
        """
        reference = signal.get("reference_candle_time")
        if reference is not None:
            rows = self.postgres.fetch_all(
                """SELECT timestamp, open, high, low, close
                FROM market_data.ohlc
                WHERE exchange = %s AND pair = %s AND timeframe = %s
                  AND timestamp = %s
                LIMIT 1""",
                (
                    signal["exchange"],
                    signal["pair"],
                    signal["timeframe"],
                    reference,
                ),
            )
            if rows:
                timestamp, open_price, high_price, low_price, close_price = rows[0]
                timestamp = pd.Timestamp(timestamp)
                seconds = 3600 if signal["timeframe"] == "1h" else 900
                candle_closed = (
                    timestamp.timestamp() + seconds
                    <= pd.Timestamp.now(tz="UTC").timestamp()
                )
                if candle_closed:
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
                            self._arm_break_even(signal, state, timestamp)

                        if (
                            not bool(state.get("tp1_hit"))
                            and close_value >= tp1_price
                        ):
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

        return super().check_signal(signal)

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
