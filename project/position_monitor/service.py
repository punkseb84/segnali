"""Position monitoring for modular operative signals stored in PostgreSQL."""
from __future__ import annotations

from typing import Any

from project.database.postgres import PostgresClient
from project.shared.events import Event, EventBus, EventType
from project.shared.logging import get_module_logger


class PositionMonitor:
    """Monitor only operative A/B signals and publish TP/SL events.

    Watchlist C rows are observations, not trades. They must never be closed as TP or
    SL and must not generate position-outcome notifications.
    """

    def __init__(
        self,
        postgres: PostgresClient,
        event_bus: EventBus | None = None,
        max_signals_per_cycle: int = 50,
        ambiguous_candle_mode: str = "conservative",
    ) -> None:
        self.postgres = postgres
        self.event_bus = event_bus or EventBus()
        self.max_signals_per_cycle = max_signals_per_cycle
        self.ambiguous_candle_mode = ambiguous_candle_mode
        self.logger = get_module_logger("position_monitor")

    def monitor_open_signals(self) -> int:
        signals = self.fetch_open_signals()
        closed = 0
        for signal in signals:
            closed += 1 if self.check_signal(signal) else 0
        if signals:
            self.logger.info("POSITION MONITOR cycle checked=%s closed=%s", len(signals), closed)
        return closed

    def fetch_open_signals(self) -> list[dict[str, Any]]:
        rows = self.postgres.fetch_all(
            """SELECT id, strategy, pair, timeframe, regime, entry, stop_loss, take_profit,
                      score, probability, signal_class, created_at
            FROM signals.generated_signals
            WHERE status IN ('NEW', 'OPEN')
              AND signal_class IN ('A', 'B')
            ORDER BY created_at ASC
            LIMIT %s""",
            (self.max_signals_per_cycle,),
        )
        signals: list[dict[str, Any]] = []
        for row in rows:
            # Older tests and transitional callers may still provide the previous
            # 11-column projection without signal_class. Treat those rows as B.
            has_signal_class = len(row) >= 12
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
                    "signal_class": row[10] if has_signal_class else "B",
                    "created_at": row[11] if has_signal_class else row[10],
                }
            )
        return signals

    def check_signal(self, signal: dict[str, Any]) -> bool:
        candles = self.postgres.fetch_all(
            """SELECT timestamp, high, low, close
            FROM market_data.ohlc
            WHERE pair = %s AND timeframe = %s AND timestamp > %s
            ORDER BY timestamp ASC
            LIMIT 200""",
            (signal["pair"], signal["timeframe"], signal["created_at"]),
        )
        for timestamp, high, low, close in candles:
            high_f = float(high)
            low_f = float(low)
            close_f = float(close)
            outcome = self.resolve_candle_outcome(
                "LONG",
                high_f,
                low_f,
                signal["take_profit"],
                signal["stop_loss"],
                self.ambiguous_candle_mode,
            )
            if outcome is None:
                continue
            outcome_price = signal["stop_loss"] if outcome == "STOP_LOSS" else signal["take_profit"]
            ambiguous = high_f >= signal["take_profit"] and low_f <= signal["stop_loss"]
            self.close_signal(signal, outcome, outcome_price, timestamp, close_f, ambiguous=ambiguous)
            return True
        return False

    @staticmethod
    def resolve_candle_outcome(
        direction: str,
        high: float,
        low: float,
        take_profit: float,
        stop_loss: float,
        ambiguous_mode: str = "conservative",
    ) -> str | None:
        direction = direction.upper()
        if direction == "SHORT":
            tp_hit = low <= take_profit
            sl_hit = high >= stop_loss
        else:
            tp_hit = high >= take_profit
            sl_hit = low <= stop_loss
        if not tp_hit and not sl_hit:
            return None
        if tp_hit and sl_hit:
            if ambiguous_mode == "optimistic":
                return "TARGET_HIT"
            if ambiguous_mode == "ambiguous":
                return None
            return "STOP_LOSS"
        return "TARGET_HIT" if tp_hit else "STOP_LOSS"

    def close_signal(
        self,
        signal: dict[str, Any],
        outcome: str,
        outcome_price: float,
        timestamp: Any,
        close_price: float,
        ambiguous: bool = False,
    ) -> None:
        self.postgres.execute(
            """UPDATE signals.generated_signals
            SET status = %s
            WHERE id = %s""",
            (outcome, signal["id"]),
        )
        event_type = EventType.STOP_LOSS if outcome == "STOP_LOSS" else EventType.TARGET_HIT
        payload = {
            **signal,
            "signal_id": signal["id"],
            "outcome": outcome,
            "outcome_price": outcome_price,
            "close_price": close_price,
            "closed_at": str(timestamp),
            "ambiguous": ambiguous,
        }
        self.logger.info(
            "POSITION CLOSED id=%s class=%s outcome=%s pair=%s timeframe=%s outcome_price=%.6f ambiguous=%s",
            signal["id"],
            signal.get("signal_class", "UNKNOWN"),
            outcome,
            signal["pair"],
            signal["timeframe"],
            outcome_price,
            ambiguous,
        )
        self.event_bus.publish(Event(event_type, payload))
