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

    @staticmethod
    def _to_utc(value: Any) -> pd.Timestamp:
        timestamp = pd.Timestamp(value)
        return (
            timestamp.tz_localize("UTC")
            if timestamp.tzinfo is None
            else timestamp.tz_convert("UTC")
        )

    def _resolve_reference_candle(
        self,
        signal: dict[str, Any],
    ) -> tuple[Any, float, float, float, float] | None:
        """Resolve the first candle affected by the signal using delivery time.

        Telegram delivery time is the operational boundary. OHLC exchange labels are
        matched case-insensitively; when historical signals contain a different or
        missing exchange label, the lookup safely falls back to pair/timeframe.
        """
        delivery_rows = self.postgres.fetch_all(
            """SELECT COALESCE(telegram_sent_at, created_at)
            FROM signals.generated_signals
            WHERE id = %s
            LIMIT 1""",
            (signal["id"],),
        )
        boundary_value = (
            delivery_rows[0][0]
            if delivery_rows and delivery_rows[0][0] is not None
            else signal.get("created_at")
        )
        if boundary_value is None:
            self.logger.warning(
                "ICHIMOKU_MANAGEMENT_BOUNDARY_MISSING id=%s pair=%s",
                signal.get("id"),
                signal.get("pair"),
            )
            return None

        boundary = self._to_utc(boundary_value)
        requested_exchange = str(signal.get("exchange") or "").strip()

        rows = self.postgres.fetch_all(
            """SELECT timestamp, open, high, low, close, exchange
            FROM market_data.ohlc
            WHERE LOWER(TRIM(exchange)) = LOWER(TRIM(%s))
              AND pair = %s
              AND timeframe = %s
              AND timestamp <= %s
            ORDER BY timestamp DESC
            LIMIT 1""",
            (
                requested_exchange,
                signal["pair"],
                signal["timeframe"],
                boundary,
            ),
        )

        lookup_mode = "EXACT_EXCHANGE"
        if not rows:
            rows = self.postgres.fetch_all(
                """SELECT timestamp, open, high, low, close, exchange
                FROM market_data.ohlc
                WHERE pair = %s
                  AND timeframe = %s
                  AND timestamp <= %s
                ORDER BY timestamp DESC
                LIMIT 1""",
                (
                    signal["pair"],
                    signal["timeframe"],
                    boundary,
                ),
            )
            lookup_mode = "PAIR_TIMEFRAME_FALLBACK"

        if not rows:
            latest_rows = self.postgres.fetch_all(
                """SELECT timestamp, exchange
                FROM market_data.ohlc
                WHERE pair = %s AND timeframe = %s
                ORDER BY timestamp DESC
                LIMIT 1""",
                (signal["pair"], signal["timeframe"]),
            )
            latest_timestamp = latest_rows[0][0] if latest_rows else None
            latest_exchange = latest_rows[0][1] if latest_rows else None
            self.logger.warning(
                "ICHIMOKU_DELIVERY_CANDLE_NOT_FOUND id=%s pair=%s timeframe=%s "
                "requested_exchange=%s boundary=%s latest_timestamp=%s latest_exchange=%s",
                signal.get("id"),
                signal.get("pair"),
                signal.get("timeframe"),
                requested_exchange,
                boundary,
                latest_timestamp,
                latest_exchange,
            )
            return None

        raw = rows[0]
        selected = raw[:5]
        resolved_exchange = raw[5]
        candle_open = self._to_utc(selected[0])
        seconds = 3600 if signal["timeframe"] == "1h" else 900
        candle_end = candle_open + pd.Timedelta(seconds=seconds)
        now = pd.Timestamp.now(tz="UTC")

        self.logger.info(
            "ICHIMOKU_OHLC_LOOKUP id=%s pair=%s mode=%s requested_exchange=%s "
            "resolved_exchange=%s boundary=%s open=%s",
            signal.get("id"),
            signal.get("pair"),
            lookup_mode,
            requested_exchange,
            resolved_exchange,
            boundary,
            candle_open,
        )

        if candle_end > now:
            self.logger.info(
                "ICHIMOKU_DELIVERY_CANDLE_WAITING id=%s pair=%s boundary=%s open=%s end=%s",
                signal.get("id"),
                signal.get("pair"),
                boundary,
                candle_open,
                candle_end,
            )
            return None

        self.logger.info(
            "ICHIMOKU_DELIVERY_CANDLE_RESOLVED id=%s pair=%s boundary=%s open=%s end=%s",
            signal.get("id"),
            signal.get("pair"),
            boundary,
            candle_open,
            candle_end,
        )
        return selected

    def check_signal(self, signal: dict[str, Any]) -> bool:
        """Evaluate the delivery candle close, then monitor later candles normally."""
        resolved = self._resolve_reference_candle(signal)
        normalized_signal = dict(signal)

        if resolved is not None:
            timestamp, open_price, high_price, low_price, close_price = resolved
            timestamp = self._to_utc(timestamp)
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
                        "ICHIMOKU_DELIVERY_CANDLE_BREAK_EVEN id=%s pair=%s close=%.8f trigger=%.8f",
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
                        "ICHIMOKU_DELIVERY_CANDLE_TP1 id=%s pair=%s close=%.8f trigger=%.8f",
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