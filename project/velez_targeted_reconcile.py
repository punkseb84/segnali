"""Targeted one-shot reconciliation for known legacy Velez migration gaps.

This is deliberately idempotent and only touches signals still marked EXPIRED by a
runtime replacement. It exists to repair historical records such as #307 that may
not match the generic strategy-name/outcome-price filters used by broad audits.
"""
from __future__ import annotations

from typing import Any

TARGET_IDS = (307,)


def reconcile_known_legacy_ids(monitor: Any) -> int:
    logger = monitor.logger
    repaired = 0
    logger.info("VELEZ_TARGETED_RECONCILE_START ids=%s", list(TARGET_IDS))

    for signal_id in TARGET_IDS:
        try:
            rows = monitor.postgres.fetch_all(
                """SELECT id, strategy, pair, timeframe, entry, stop_loss, take_profit,
                          reference_candle_time, created_at, telegram_sent_at,
                          COALESCE(exchange, 'Kraken'), quantity,
                          COALESCE(estimated_buy_fee_eur, 0),
                          COALESCE(score_breakdown, '{}'::jsonb), closed_at,
                          COALESCE(outcome_resolution,'')
                   FROM signals.generated_signals
                   WHERE id=%s
                   LIMIT 1""",
                (signal_id,),
            )
            if not rows:
                logger.warning("VELEZ_TARGETED_RECONCILE_MISSING id=%s", signal_id)
                continue

            row = rows[0]
            signal = monitor._row_to_signal(row, archived=True)
            status_rows = monitor.postgres.fetch_all(
                "SELECT status, COALESCE(outcome_resolution,'') FROM signals.generated_signals WHERE id=%s",
                (signal_id,),
            )
            status = str(status_rows[0][0]) if status_rows else ""
            resolution = str(status_rows[0][1]) if status_rows else ""

            # Never overwrite a normally resolved trade. Only repair migration-expired records.
            if status != "EXPIRED" or not resolution.startswith("REPLACED_BY_VELEZ"):
                logger.info(
                    "VELEZ_TARGETED_RECONCILE_SKIP id=%s status=%s resolution=%s",
                    signal_id, status, resolution,
                )
                continue

            event = monitor._legacy_first_terminal_event(signal)
            if event is None:
                logger.info(
                    "VELEZ_TARGETED_RECONCILE_NONE id=%s pair=%s archived_at=%s",
                    signal_id, signal["pair"], signal.get("archived_at"),
                )
                continue

            event_resolution, price, candle = event
            if monitor._close_expired_reconciled(signal, price, candle, event_resolution):
                repaired += 1
                logger.warning(
                    "VELEZ_TARGETED_RECONCILE_DONE id=%s pair=%s resolution=%s price=%.8f candle=%s",
                    signal_id, signal["pair"], event_resolution, float(price), candle["timestamp"],
                )
        except Exception:
            logger.exception(
                "VELEZ_TARGETED_RECONCILE_FAILED id=%s action=CONTINUE_RUNTIME",
                signal_id,
            )

    logger.info("VELEZ_TARGETED_RECONCILE_END repaired=%s", repaired)
    return repaired
