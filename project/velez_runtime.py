"""Clean Railway runtime for Oliver Velez 15m PAPER Mode 2 protected lifecycle."""
from __future__ import annotations

from dataclasses import replace

import project.notification_engine.service as notification_service
import project.relative_strength_monitor as monitor_module
from project.capital_protection_migration import run_capital_protection_migration
from project.config.settings import load_settings
from project.database.migrations import run_migrations
from project.database.postgres import parse_postgres_connection_info, sanitize_postgres_error
from project.shared.events import Event, EventBus, EventType
from project.shared.logging import get_module_logger
from project.simple_main import apply_simple_policy, build_collector, build_postgres
from project.velez_candidate_audit import VelezCandidateAudit
from project.velez_entry_audit import VelezEntryAudit
from project.velez_exit_audit import VelezExitAudit
from project.velez_path_audit import VelezPathAudit
from project.velez_formatters import format_velez_outcome, format_velez_signal
from project.velez_legacy_reconcile import reconcile_legacy_expired_safe
from project.velez_targeted_reconcile import reconcile_known_legacy_ids
from project.velez_mode2_scanner import RUNTIME_VERSION, VelezMode2Scanner
from project.velez_monitor import Velez15mMonitor
from project.velez_notification import VelezNotificationEngine
from project.velez_scheduler import Velez15mScheduler
from project.velez_universe import resolve_top_market_cap_pairs

PUBLIC_BUDGET_EUR = 100.0
MAX_OPEN = 4
PER_TRADE_EUR = PUBLIC_BUDGET_EUR / MAX_OPEN
BINANCE_VIP1_MAKER_FEE_RATE = 0.0007
BINANCE_VIP1_TAKER_FEE_RATE = 0.0016


def _retire_previous_open_signals(postgres: object) -> None:
    postgres.execute(
        """UPDATE signals.generated_signals
           SET status='EXPIRED', closed_at=COALESCE(closed_at,NOW()),
               outcome_resolution='REPLACED_BY_VELEZ_MODE2_PROTECT_RUNTIME'
           WHERE status IN ('NEW','OPEN')
             AND COALESCE(score_breakdown->>'runtime_version','') <> %s""",
        (RUNTIME_VERSION,),
    )


def main() -> None:
    pairs = resolve_top_market_cap_pairs(20)
    settings = apply_simple_policy(load_settings())
    settings = replace(
        settings,
        collector_pairs=pairs,
        collector_timeframes=["15m"],
        operational_timeframe="15m",
        scheduler_collector_seconds=900,
        telegram_send_startup_message=False,
        enable_daily_signal_report=False,
        binance_buy_fee_rate=BINANCE_VIP1_TAKER_FEE_RATE,
        binance_sell_fee_rate=BINANCE_VIP1_TAKER_FEE_RATE,
    )
    logger = get_module_logger("system")
    logger.info("======================================")
    logger.info("PROJECT MAIN: VELEZ 15M MODE 2 PROTECTED PAPER ONLY")
    logger.info("======================================")
    logger.info(
        "VELEZ_RUNTIME version=%s timeframe=15m pairs=%s budget=%.2f per_trade=%.2f fixed_tp=false protection=1.5R_to_1R exit=protected_stop_or_ema20_close_or_12h relative_strength=false trix=false ichimoku=false paper=true notifier=strict",
        RUNTIME_VERSION, pairs, PUBLIC_BUDGET_EUR, PER_TRADE_EUR,
    )
    logger.info(
        "BINANCE_FEES vip=1 spot_maker=%.4f%% spot_taker=%.4f%% paper_entry=taker paper_exit=taker buy_fee=%.4f%% sell_fee=%.4f%% spread=%.4f%% slippage=%.4f%%",
        BINANCE_VIP1_MAKER_FEE_RATE * 100, BINANCE_VIP1_TAKER_FEE_RATE * 100,
        settings.binance_buy_fee_rate * 100, settings.binance_sell_fee_rate * 100,
        settings.binance_spread_rate * 100, settings.binance_slippage_rate * 100,
    )

    try:
        postgres = build_postgres(settings)
    except Exception as exc:
        logger.error("PostgreSQL connection failed: %s", sanitize_postgres_error(str(exc), settings.database_url))
        raise SystemExit(1) from exc

    logger.info("PostgreSQL connection: OK")
    logger.info("PostgreSQL %s", parse_postgres_connection_info(settings.database_url).display())
    run_migrations(postgres)
    run_capital_protection_migration(postgres)
    monitor_module.RUNTIME_VERSION = RUNTIME_VERSION

    event_bus = EventBus()
    notification_service.format_signal_message = format_velez_signal
    notification_service.format_outcome_message = format_velez_outcome
    collector = build_collector(settings, event_bus, postgres)
    scanner = VelezMode2Scanner(
        postgres, event_bus, settings.collector_pairs, exchange=settings.exchange_name,
        trade_notional_eur=PER_TRADE_EUR, buy_fee_rate=settings.binance_buy_fee_rate,
        sell_fee_rate=settings.binance_sell_fee_rate, spread_rate=settings.binance_spread_rate,
        slippage_rate=settings.binance_slippage_rate, quantity_step=settings.binance_quantity_step,
        min_qty=settings.binance_min_qty, min_notional_eur=settings.binance_min_notional_eur,
        signal_cooldown_minutes=60, max_signals_per_cycle=2, max_signals_per_day=8,
        max_open_positions=MAX_OPEN, max_per_pair_day=2, target_r=1.5, max_hold_candles=48, pullback_bars=3,
    )
    monitor = Velez15mMonitor(
        postgres, event_bus, ambiguous_candle_mode=settings.ambiguous_candle_mode,
        max_shadow_signals_per_cycle=300, sell_fee_rate=settings.binance_sell_fee_rate,
        spread_rate=settings.binance_spread_rate, slippage_rate=settings.binance_slippage_rate,
    )
    notification = VelezNotificationEngine(
        event_bus, enabled=True, max_message_length=settings.telegram_max_message_length,
        max_retries=settings.telegram_max_retries, postgres=postgres,
    )
    notification.subscribe()

    legacy_diagnostic = monitor.reconcile_migration_expired_outcomes()
    logger.info("VELEZ_MIGRATION_DIAGNOSTIC repaired=%s", legacy_diagnostic)
    repaired = reconcile_legacy_expired_safe(monitor)
    logger.info("VELEZ_SAFE_MIGRATION_OUTCOME_RECONCILIATION repaired=%s", repaired)
    targeted = reconcile_known_legacy_ids(monitor)
    logger.info("VELEZ_TARGETED_OUTCOME_RECONCILIATION repaired=%s", targeted)
    reconciled = monitor.reconcile_all_active_hard_stops()
    logger.info("VELEZ_STARTUP_STOP_RECONCILIATION closed=%s", reconciled)
    _retire_previous_open_signals(postgres)

    try:
        exit_audit = VelezExitAudit(postgres, get_module_logger("velez-exit-audit"))
        exit_summary = exit_audit.run()
        event_bus.publish(Event(EventType.REPORT_READY, {"message": exit_audit.format_report(exit_summary), "trusted_html": True, "report_type": "VELEZ_EXIT_AUDIT"}))
        logger.info("VELEZ_EXIT_AUDIT_REPORT_SENT trades=%s", exit_summary.get("trades", 0))
    except Exception:
        logger.exception("VELEZ_EXIT_AUDIT_FATAL_GUARD action=CONTINUE_RUNTIME")

    try:
        entry_audit = VelezEntryAudit(postgres, get_module_logger("velez-entry-audit"))
        entry_summary = entry_audit.run()
        event_bus.publish(Event(EventType.REPORT_READY, {"message": entry_audit.format_report(entry_summary), "trusted_html": True, "report_type": "VELEZ_ENTRY_AUDIT"}))
        logger.info("VELEZ_ENTRY_AUDIT_REPORT_SENT trades=%s", entry_summary.get("trades", 0))
    except Exception:
        logger.exception("VELEZ_ENTRY_AUDIT_FATAL_GUARD action=CONTINUE_RUNTIME")

    try:
        path_audit = VelezPathAudit(
            postgres, get_module_logger("velez-path-audit"), notional_eur=PER_TRADE_EUR,
            buy_fee_rate=settings.binance_buy_fee_rate, sell_fee_rate=settings.binance_sell_fee_rate,
            spread_rate=settings.binance_spread_rate, slippage_rate=settings.binance_slippage_rate,
        )
        path_summary = path_audit.run()
        event_bus.publish(Event(EventType.REPORT_READY, {"message": path_audit.format_report(path_summary), "trusted_html": True, "report_type": "VELEZ_PATH_AUDIT"}))
        logger.info("VELEZ_PATH_AUDIT_REPORT_SENT trades=%s", path_summary.get("trades", 0))
    except Exception:
        logger.exception("VELEZ_PATH_AUDIT_FATAL_GUARD action=CONTINUE_RUNTIME")

    # One-shot historical candidate audit. The DB version marker prevents costly reruns.
    try:
        candidate_audit = VelezCandidateAudit(
            postgres, get_module_logger("velez-candidate-audit"), settings.collector_pairs,
            exchange=settings.exchange_name, pullback_bars=3, max_bars_per_pair=3200,
        )
        candidate_summary = candidate_audit.run()
        if not candidate_summary.get("skipped"):
            message = candidate_audit.format_report(candidate_summary)
            if message:
                event_bus.publish(Event(EventType.REPORT_READY, {"message": message, "trusted_html": True, "report_type": "VELEZ_CANDIDATE_AUDIT"}))
            logger.info("VELEZ_CANDIDATE_AUDIT_REPORT_SENT candidates=%s qualified=%s", candidate_summary.get("candidates", 0), candidate_summary.get("qualified", 0))
        else:
            logger.info("VELEZ_CANDIDATE_AUDIT_SKIP already_completed=true")
    except Exception:
        logger.exception("VELEZ_CANDIDATE_AUDIT_FATAL_GUARD action=CONTINUE_RUNTIME")

    Velez15mScheduler(settings, collector, None, None, scanner, monitor).run_forever()


if __name__ == "__main__":
    main()
