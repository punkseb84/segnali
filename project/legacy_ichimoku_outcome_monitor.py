"""Runtime-scoped legacy Ichimoku monitor used while old PAPER positions finish."""
from __future__ import annotations

from project.ichimoku_scanner import RUNTIME_VERSION
from project.notifying_ichimoku_outcome_monitor import NotifyingIchimokuOutcomeMonitor


class LegacyIchimokuOutcomeMonitor(NotifyingIchimokuOutcomeMonitor):
    """Prevent legacy break-even backfill from touching new TRIX positions."""

    def _backfill_pending_break_even_notifications(self) -> int:
        rows = self.postgres.fetch_all(
            """SELECT id, pair, timeframe, entry,
                      score_breakdown->>'breakeven_armed_at'
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
