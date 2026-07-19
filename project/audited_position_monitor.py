"""Exchange-aware position monitor with complete outcome-candle audit data."""
from __future__ import annotations

from typing import Any

from project.position_monitor.service import PositionMonitor
from project.shared.events import Event, EventType


class AuditedPositionMonitor(PositionMonitor):
    """Resolve TP/SL only on the signal exchange and persist actual candle OHLC."""

    def fetch_open_signals(self) -> list[dict[str, Any]]:
        rows = self.postgres.fetch_all(
            """SELECT id, strategy, pair, timeframe, regime, entry, stop_loss, take_profit,
                      score, probability, signal_class,
                      net_profit_tp1_eur, net_loss_sl_eur, net_rr,
                      reference_candle_time, created_at,
                      COALESCE(exchange, 'kraken')
            FROM signals.generated_signals
            WHERE status IN ('NEW', 'OPEN')
              AND signal_class IN ('A', 'B')
              AND telegram_sent_at IS NOT NULL
            ORDER BY created_at ASC
            LIMIT %s""",
            (self.max_signals_per_cycle,),
        )
        signals: list[dict[str, Any]] = []
        for row in rows:
            signals.append(
                {
                    "id": row[0],
                    "strategy": row[1],
                    "pair": row[2],
                    "timeframe": row[3],
                    "regime": row[4],
                    "entry": float(row[5]),
                    "stop_loss": float(row[6]),
                    "take_profit": float(row[7]),
                    "score": float(row[8]),
                    "probability": float(row[9]) if row[9] is not None else None,
                    "signal_class": row[10],
                    "net_profit_tp1_eur": (
                        float(row[11]) if row[11] is not None else None
                    ),
                    "net_loss_sl_eur": (
                        float(row[12]) if row[12] is not None else None
                    ),
                    "net_rr": float(row[13]) if row[13] is not None else None,
                    "reference_candle_time": row[14],
                    "created_at": row[15],
                    "exchange": str(row[16] or "kraken"),
                }
            )
        return signals

    def check_signal(self, signal: dict[str, Any]) -> bool:
        reference_time = signal.get("reference_candle_time")
        start_time = reference_time or signal["created_at"]
        comparison = ">=" if reference_time is not None else ">"
        candles = self.postgres.fetch_all(
            f"""SELECT timestamp, open, high, low, close
            FROM market_data.ohlc
            WHERE exchange = %s
              AND pair = %s
              AND timeframe = %s
              AND timestamp {comparison} %s
            ORDER BY timestamp ASC
            LIMIT 200""",
            (
                signal.get("exchange", "kraken"),
                signal["pair"],
                signal["timeframe"],
                start_time,
            ),
        )
        for timestamp, open_price, high, low, close in candles:
            open_f = float(open_price)
            high_f = float(high)
            low_f = float(low)
            close_f = float(close)
            entry_candle = reference_time is not None and timestamp == reference_time
            if entry_candle:
                outcome = self.resolve_close_outcome(
                    "LONG", close_f, signal["take_profit"], signal["stop_loss"]
                )
                resolution = "ENTRY_CANDLE_CLOSE"
                ambiguous = False
            else:
                outcome = self.resolve_candle_outcome(
                    "LONG",
                    high_f,
                    low_f,
                    signal["take_profit"],
                    signal["stop_loss"],
                    self.ambiguous_candle_mode,
                )
                resolution = "FULL_CANDLE_RANGE"
                ambiguous = (
                    high_f >= signal["take_profit"]
                    and low_f <= signal["stop_loss"]
                )
            if outcome is None:
                continue
            outcome_price = (
                signal["stop_loss"]
                if outcome == "STOP_LOSS"
                else signal["take_profit"]
            )
            self.close_signal_with_candle(
                signal,
                outcome,
                outcome_price,
                timestamp,
                open_f,
                high_f,
                low_f,
                close_f,
                ambiguous=ambiguous,
                resolution=resolution,
            )
            return True
        return False

    def close_signal_with_candle(
        self,
        signal: dict[str, Any],
        outcome: str,
        outcome_price: float,
        timestamp: Any,
        open_price: float,
        high_price: float,
        low_price: float,
        close_price: float,
        *,
        ambiguous: bool = False,
        resolution: str = "FULL_CANDLE_RANGE",
    ) -> None:
        self.postgres.execute(
            """UPDATE signals.generated_signals
            SET outcome_price = %s,
                outcome_open_price = %s,
                outcome_high_price = %s,
                outcome_low_price = %s,
                outcome_close_price = %s,
                outcome_candle_time = %s,
                closed_at = NOW(),
                outcome_ambiguous = %s,
                outcome_resolution = %s
            WHERE id = %s""",
            (
                outcome_price,
                open_price,
                high_price,
                low_price,
                close_price,
                timestamp,
                ambiguous,
                resolution,
                signal["id"],
            ),
        )
        self.postgres.execute(
            """UPDATE signals.generated_signals
            SET status = %s
            WHERE id = %s
              AND status IN ('NEW', 'OPEN')""",
            (outcome, signal["id"]),
        )
        event_type = (
            EventType.STOP_LOSS if outcome == "STOP_LOSS" else EventType.TARGET_HIT
        )
        payload = {
            **signal,
            "signal_id": signal["id"],
            "outcome": outcome,
            "outcome_price": outcome_price,
            "outcome_open_price": open_price,
            "outcome_high_price": high_price,
            "outcome_low_price": low_price,
            "close_price": close_price,
            "closed_at": str(timestamp),
            "ambiguous": ambiguous,
            "outcome_resolution": resolution,
        }
        self.logger.info(
            "AUDITED_POSITION_CLOSED id=%s exchange=%s outcome=%s pair=%s timeframe=%s "
            "stop=%.8f target=%.8f candle_open=%.8f candle_high=%.8f "
            "candle_low=%.8f candle_close=%.8f candle=%s resolution=%s ambiguous=%s",
            signal["id"],
            signal.get("exchange", "kraken"),
            outcome,
            signal["pair"],
            signal["timeframe"],
            signal["stop_loss"],
            signal["take_profit"],
            open_price,
            high_price,
            low_price,
            close_price,
            timestamp,
            resolution,
            ambiguous,
        )
        self.event_bus.publish(Event(event_type, payload))
