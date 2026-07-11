"""Position monitoring for modular signals stored in PostgreSQL."""
from __future__ import annotations

from typing import Any

from project.database.postgres import PostgresClient
from project.shared.events import Event, EventBus, EventType
from project.shared.logging import get_module_logger


class PositionMonitor:
    """Monitor generated signals and publish TP/SL events.

    This module does not generate new signals and does not decide which strategy
    to trade. It only watches already-created signals against PostgreSQL OHLC.
    """

    def __init__(self, postgres: PostgresClient, event_bus: EventBus | None = None, max_signals_per_cycle: int = 50) -> None:
        self.postgres = postgres
        self.event_bus = event_bus or EventBus()
        self.max_signals_per_cycle = max_signals_per_cycle
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
            """SELECT id, strategy, pair, timeframe, regime, entry, stop_loss, take_profit, score, probability, created_at
            FROM signals.generated_signals
            WHERE status IN ('NEW', 'OPEN')
            ORDER BY created_at ASC
            LIMIT %s""",
            (self.max_signals_per_cycle,),
        )
        return [
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
                "probability": float(row[9]),
                "created_at": row[10],
            }
            for row in rows
        ]

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
            tp_hit = high_f >= signal["take_profit"]
            sl_hit = low_f <= signal["stop_loss"]
            if not tp_hit and not sl_hit:
                continue
            # Conservative handling: if TP and SL happen in the same candle, SL wins.
            outcome = "STOP_LOSS" if sl_hit else "TARGET_HIT"
            outcome_price = signal["stop_loss"] if sl_hit else signal["take_profit"]
            self.close_signal(signal, outcome, outcome_price, timestamp, close_f, ambiguous=tp_hit and sl_hit)
            return True
        return False

    def close_signal(self, signal: dict[str, Any], outcome: str, outcome_price: float, timestamp: Any, close_price: float, ambiguous: bool = False) -> None:
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
            "POSITION CLOSED id=%s outcome=%s pair=%s timeframe=%s outcome_price=%.6f ambiguous=%s",
            signal["id"],
            outcome,
            signal["pair"],
            signal["timeframe"],
            outcome_price,
            ambiguous,
        )
        self.event_bus.publish(Event(event_type, payload))
