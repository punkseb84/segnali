"""Safe retroactive reconciliation for Velez signals expired during deploy migrations."""
from __future__ import annotations

from typing import Any


def reconcile_legacy_expired_safe(monitor: Any) -> int:
    """Repair EXPIRED Velez records without embedding percent signs in psycopg SQL.

    The existing monitor owns the chronology and P&L rules. This helper only loads
    eligible archived records using a parameterized LIKE predicate, then delegates
    event reconstruction and closing to the monitor.
    """
    logger = monitor.logger
    logger.info("VELEZ_SAFE_MIGRATION_AUDIT_START")
    try:
        rows = monitor.postgres.fetch_all(
            """SELECT id, strategy, pair, timeframe, entry, stop_loss, take_profit,
                      reference_candle_time, created_at, telegram_sent_at,
                      COALESCE(exchange, 'Kraken'), quantity,
                      COALESCE(estimated_buy_fee_eur, 0),
                      COALESCE(score_breakdown, '{}'::jsonb), closed_at,
                      COALESCE(outcome_resolution,'')
               FROM signals.generated_signals
               WHERE status='EXPIRED'
                 AND telegram_sent_at IS NOT NULL
                 AND LOWER(COALESCE(strategy,'')) LIKE %s
                 AND outcome_price IS NULL
               ORDER BY created_at ASC
               LIMIT 500""",
            ("%velez%",),
        )
    except Exception:
        logger.exception("VELEZ_SAFE_MIGRATION_QUERY_FAILED action=CONTINUE_RUNTIME")
        return 0

    signals = [monitor._row_to_signal(row, archived=True) for row in rows]
    logger.info(
        "VELEZ_SAFE_MIGRATION_AUDIT candidates=%s ids=%s",
        len(signals),
        [signal["id"] for signal in signals],
    )
    repaired = 0
    for signal in signals:
        try:
            event = monitor._legacy_first_terminal_event(signal)
            if event is None:
                logger.info(
                    "VELEZ_SAFE_MIGRATION_NONE id=%s pair=%s archived_at=%s",
                    signal["id"], signal["pair"], signal.get("archived_at"),
                )
                continue
            resolution, price, row = event
            if monitor._close_expired_reconciled(signal, price, row, resolution):
                repaired += 1
        except Exception:
            logger.exception(
                "VELEZ_SAFE_MIGRATION_FAILED id=%s pair=%s action=CONTINUE",
                signal.get("id"), signal.get("pair"),
            )
    logger.info(
        "VELEZ_SAFE_MIGRATION_AUDIT_DONE candidates=%s repaired=%s",
        len(signals), repaired,
    )
    return repaired
