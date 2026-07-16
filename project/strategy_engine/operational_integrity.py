"""Operational lifecycle guards for duplicate exposure and repeated entries."""
from __future__ import annotations

from project.strategy_engine.progressive_operational import (
    ProgressiveOperationalStrategyEngine,
)
from project.strategy_engine.service import GeneratedSignal


class OperationalIntegrityStrategyEngine(ProgressiveOperationalStrategyEngine):
    """Allow at most one operative exposure per pair/timeframe and candle.

    Different strategies frequently share the same latest market price. Treating the
    strategy name as part of the duplicate key allowed several signals on the same pair
    with the same entry, multiplying exposure without adding diversification.
    """

    def find_active_duplicate(self, signal: GeneratedSignal) -> int | None:
        reference_time = signal.reference_candle_time

        if signal.signal_class in self.watchlist_signal_classes:
            rows = self.research_repository.client.fetch_all(
                """SELECT id
                FROM signals.generated_signals
                WHERE pair = %s
                  AND timeframe = %s
                  AND signal_class = %s
                  AND status = 'WATCHLIST'
                  AND (
                        (%s IS NOT NULL AND reference_candle_time = %s)
                        OR created_at >= NOW() - (%s * INTERVAL '1 minute')
                  )
                ORDER BY created_at DESC
                LIMIT 1""",
                (
                    signal.pair,
                    signal.timeframe,
                    signal.signal_class,
                    reference_time,
                    reference_time,
                    self.signal_cooldown_minutes,
                ),
            )
            if rows:
                self.logger.info(
                    "WATCHLIST_DUPLICATE_BLOCKED existing_id=%s pair=%s timeframe=%s "
                    "reference_candle=%s",
                    rows[0][0],
                    signal.pair,
                    signal.timeframe,
                    reference_time,
                )
                return int(rows[0][0])
            return None

        operative_classes = tuple(self.operative_signal_classes) or ("A", "B")
        placeholders = ", ".join(["%s"] * len(operative_classes))
        rows = self.research_repository.client.fetch_all(
            f"""SELECT id
            FROM signals.generated_signals
            WHERE pair = %s
              AND timeframe = %s
              AND signal_class IN ({placeholders})
              AND (
                    status IN ('NEW', 'OPEN')
                    OR (%s IS NOT NULL AND reference_candle_time = %s)
                    OR created_at >= NOW() - (%s * INTERVAL '1 minute')
              )
            ORDER BY
                CASE WHEN status IN ('NEW', 'OPEN') THEN 0 ELSE 1 END,
                created_at DESC
            LIMIT 1""",
            (
                signal.pair,
                signal.timeframe,
                *operative_classes,
                reference_time,
                reference_time,
                self.signal_cooldown_minutes,
            ),
        )
        if not rows:
            return None

        existing_id = int(rows[0][0])
        self.logger.info(
            "OPERATIVE_PAIR_DUPLICATE_BLOCKED existing_id=%s pair=%s timeframe=%s "
            "candidate_strategy=%s candidate_entry=%.8f reference_candle=%s "
            "cooldown_minutes=%s",
            existing_id,
            signal.pair,
            signal.timeframe,
            signal.strategy,
            signal.entry,
            reference_time,
            self.signal_cooldown_minutes,
        )
        return existing_id
