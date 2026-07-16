"""Position monitoring for modular operative signals stored in PostgreSQL."""
from __future__ import annotations

from typing import Any

from project.database.postgres import PostgresClient
from project.shared.events import Event, EventBus, EventType
from project.shared.logging import get_module_logger


class PositionMonitor:
    """Monitor only operative A/B signals and publish TP/SL events.

    The entry candle is evaluated with its updated close only. Its high/low may include
    price action that happened before the Telegram signal, so using the full range could
    create false outcomes. Later candles use normal high/low barrier detection.
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
                      score, probability, signal_class,
                      net_profit_tp1_eur, net_loss_sl_eur, net_rr,
                      reference_candle_time, created_at
            FROM signals.generated_signals
            WHERE status IN ('NEW', 'OPEN')
              AND signal_class IN ('A', 'B')
            ORDER BY created_at ASC
            LIMIT %s""",
            (self.max_signals_per_cycle,),
        )
        signals: list[dict[str, Any]] = []
        for row in rows:
            if len(row) >= 16:
                signal_class = row[10]
                net_profit = float(row[11]) if row[11] is not None else None
                net_loss = float(row[12]) if row[12] is not None else None
                net_rr = float(row[13]) if row[13] is not None else None
                reference_candle_time = row[14]
                created_at = row[15]
            elif len(row) >= 15:
                signal_class = row[10]
                net_profit = float(row[11]) if row[11] is not None else None
                net_loss = float(row[12]) if row[12] is not None else None
                net_rr = float(row[13]) if row[13] is not None else None
                reference_candle_time = None
                created_at = row[14]
            elif len(row) >= 12:
                signal_class = row[10]
                net_profit = None
                net_loss = None
                net_rr = None
                reference_candle_time = None
                created_at = row[11]
            else:
                signal_class = "B"
                net_profit = None
                net_loss = None
                net_rr = None
                reference_candle_time = None
                created_at = row[10]

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
                    "signal_class": signal_class,
                    "net_profit_tp1_eur": net_profit,
                    "net_loss_sl_eur": net_loss,
                    "net_rr": net_rr,
                    "reference_candle_time": reference_candle_time,
                    "created_at": created_at,
                }
            )
        return signals

    def check_signal(self, signal: dict[str, Any]) -> bool:
        reference_time = signal.get("reference_candle_time")
        start_time = reference_time or signal["created_at"]
        comparison = ">=" if reference_time is not None else ">"
        candles = self.postgres.fetch_all(
            f"""SELECT timestamp, high, low, close
            FROM market_data.ohlc
            WHERE pair = %s AND timeframe = %s AND timestamp {comparison} %s
            ORDER BY timestamp ASC
            LIMIT 200""",
            (signal["pair"], signal["timeframe"], start_time),
        )
        for timestamp, high, low, close in candles:
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
                ambiguous = high_f >= signal["take_profit"] and low_f <= signal["stop_loss"]
            if outcome is None:
                continue
            outcome_price = signal["stop_loss"] if outcome == "STOP_LOSS" else signal["take_profit"]
            self.close_signal(
                signal,
                outcome,
                outcome_price,
                timestamp,
                close_f,
                ambiguous=ambiguous,
                resolution=resolution,
            )
            return True
        return False

    @staticmethod
    def resolve_close_outcome(
        direction: str,
        close: float,
        take_profit: float,
        stop_loss: float,
    ) -> str | None:
        if direction.upper() == "SHORT":
            if close <= take_profit:
                return "TARGET_HIT"
            if close >= stop_loss:
                return "STOP_LOSS"
            return None
        if close >= take_profit:
            return "TARGET_HIT"
        if close <= stop_loss:
            return "STOP_LOSS"
        return None

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
        resolution: str = "FULL_CANDLE_RANGE",
    ) -> None:
        # Persist audit details first, then keep the legacy two-parameter status update
        # used by existing integrations and tests.
        self.postgres.execute(
            """UPDATE signals.generated_signals
            SET outcome_price = %s,
                outcome_close_price = %s,
                outcome_candle_time = %s,
                closed_at = NOW(),
                outcome_ambiguous = %s,
                outcome_resolution = %s
            WHERE id = %s""",
            (
                outcome_price,
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
        event_type = EventType.STOP_LOSS if outcome == "STOP_LOSS" else EventType.TARGET_HIT
        payload = {
            **signal,
            "signal_id": signal["id"],
            "outcome": outcome,
            "outcome_price": outcome_price,
            "close_price": close_price,
            "closed_at": str(timestamp),
            "ambiguous": ambiguous,
            "outcome_resolution": resolution,
        }
        self.logger.info(
            "POSITION CLOSED id=%s class=%s outcome=%s pair=%s timeframe=%s "
            "outcome_price=%.6f close_price=%.6f candle=%s resolution=%s ambiguous=%s",
            signal["id"],
            signal.get("signal_class", "UNKNOWN"),
            outcome,
            signal["pair"],
            signal["timeframe"],
            outcome_price,
            close_price,
            timestamp,
            resolution,
            ambiguous,
        )
        self.event_bus.publish(Event(event_type, payload))
