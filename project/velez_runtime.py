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
from project.velez_1h_audit import Velez1hAudit
from project.velez_candidate_audit import VelezCandidateAudit
from project.velez_edge_validation_audit import VelezEdgeValidationAudit
from project.velez_utc0612_net_audit import VelezUTC0612NetAudit
from project.velez_structure_economics_audit import VelezStructureEconomicsAudit
from project.velez_pullback_entry_audit import VelezPullbackEntryAudit
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
    settings = replace(settings, collector_pairs=pairs, collector_timeframes=["15m"], operational_timeframe="15m", scheduler_collector_seconds=900, telegram_send_startup_message=False, enable_daily_signal_report=False, binance_buy_fee_rate=BINANCE_VIP1_TAKER_FEE_RATE, binance_sell_fee_rate=BINANCE_VIP1_TAKER_FEE_RATE)
    logger = get_module_logger("system")
    logger.info("======================================")
    logger.info("PROJECT MAIN: VELEZ 15M MODE 2 PROTECTED PAPER ONLY")
    logger.info("======================================")
    logger.info("VELEZ_RUNTIME version=%s timeframe=15m pairs=%s budget=%.2f per_trade=%.2f fixed_tp=false protection=1.5R_to_1R exit=protected_stop_or_ema20_close_or_12h relative_strength=false trix=false ichimoku=false paper=true notifier=strict", RUNTIME_VERSION, pairs, PUBLIC_BUDGET_EUR, PER_TRADE_EUR)
    logger.info("BINANCE_FEES vip=1 spot_maker=%.4f%% spot_taker=%.4f%% paper_entry=taker paper_exit=taker buy_fee=%.4f%% sell_fee=%.4f%% spread=%.4f%% slippage=%.4f%%", BINANCE_VIP1_MAKER_FEE_RATE*100, BINANCE_VIP1_TAKER_FEE_RATE*100, settings.binance_buy_fee_rate*100, settings.binance_sell_fee_rate*100, settings.binance_spread_rate*100, settings.binance_slippage_rate*100)
    try: postgres = build_postgres(settings)
    except Exception as exc:
        logger.error("PostgreSQL connection failed: %s", sanitize_postgres_error(str(exc), settings.database_url)); raise SystemExit(1) from exc
    logger.info("PostgreSQL connection: OK"); logger.info("PostgreSQL %s", parse_postgres_connection_info(settings.database_url).display())
    run_migrations(postgres); run_capital_protection_migration(postgres); monitor_module.RUNTIME_VERSION = RUNTIME_VERSION
    event_bus=EventBus(); notification_service.format_signal_message=format_velez_signal; notification_service.format_outcome_message=format_velez_outcome
    collector=build_collector(settings,event_bus,postgres)
    scanner=VelezMode2Scanner(postgres,event_bus,settings.collector_pairs,exchange=settings.exchange_name,trade_notional_eur=PER_TRADE_EUR,buy_fee_rate=settings.binance_buy_fee_rate,sell_fee_rate=settings.binance_sell_fee_rate,spread_rate=settings.binance_spread_rate,slippage_rate=settings.binance_slippage_rate,quantity_step=settings.binance_quantity_step,min_qty=settings.binance_min_qty,min_notional_eur=settings.binance_min_notional_eur,signal_cooldown_minutes=60,max_signals_per_cycle=2,max_signals_per_day=8,max_open_positions=MAX_OPEN,max_per_pair_day=2,target_r=1.5,max_hold_candles=48,pullback_bars=3)
    monitor=Velez15mMonitor(postgres,event_bus,ambiguous_candle_mode=settings.ambiguous_candle_mode,max_shadow_signals_per_cycle=300,sell_fee_rate=settings.binance_sell_fee_rate,spread_rate=settings.binance_spread_rate,slippage_rate=settings.binance_slippage_rate)
    notification=VelezNotificationEngine(event_bus,enabled=True,max_message_length=settings.telegram_max_message_length,max_retries=settings.telegram_max_retries,postgres=postgres); notification.subscribe()
    logger.info("VELEZ_MIGRATION_DIAGNOSTIC repaired=%s",monitor.reconcile_migration_expired_outcomes()); logger.info("VELEZ_SAFE_MIGRATION_OUTCOME_RECONCILIATION repaired=%s",reconcile_legacy_expired_safe(monitor)); logger.info("VELEZ_TARGETED_OUTCOME_RECONCILIATION repaired=%s",reconcile_known_legacy_ids(monitor)); logger.info("VELEZ_STARTUP_STOP_RECONCILIATION closed=%s",monitor.reconcile_all_active_hard_stops()); _retire_previous_open_signals(postgres)

    for audit_cls,name,report_type,kwargs in [
        (VelezExitAudit,"velez-exit-audit","VELEZ_EXIT_AUDIT",{}),
        (VelezEntryAudit,"velez-entry-audit","VELEZ_ENTRY_AUDIT",{}),
        (VelezPathAudit,"velez-path-audit","VELEZ_PATH_AUDIT",{"notional_eur":PER_TRADE_EUR,"buy_fee_rate":settings.binance_buy_fee_rate,"sell_fee_rate":settings.binance_sell_fee_rate,"spread_rate":settings.binance_spread_rate,"slippage_rate":settings.binance_slippage_rate}),
    ]:
        try:
            audit=audit_cls(postgres,get_module_logger(name),**kwargs); summary=audit.run(); message=audit.format_report(summary)
            if message: event_bus.publish(Event(EventType.REPORT_READY,{"message":message,"trusted_html":True,"report_type":report_type}))
        except Exception: logger.exception("%s_FATAL_GUARD action=CONTINUE_RUNTIME",report_type)

    for audit_cls,name,report_type in [
        (VelezCandidateAudit,"velez-candidate-audit","VELEZ_CANDIDATE_AUDIT"),
        (VelezEdgeValidationAudit,"velez-edge-validation-audit","VELEZ_EDGE_VALIDATION_AUDIT"),
    ]:
        try:
            audit=audit_cls(postgres,get_module_logger(name),settings.collector_pairs,exchange=settings.exchange_name,pullback_bars=3,max_bars_per_pair=3200)
            summary=audit.run()
            if not summary.get("skipped"):
                message=audit.format_report(summary)
                if message: event_bus.publish(Event(EventType.REPORT_READY,{"message":message,"trusted_html":True,"report_type":report_type}))
                logger.info("%s_REPORT_SENT",report_type)
            else: logger.info("%s_SKIP already_completed=true",report_type)
        except Exception: logger.exception("%s_FATAL_GUARD action=CONTINUE_RUNTIME",report_type)

    try:
        audit_1h=Velez1hAudit(postgres,get_module_logger("velez-1h-audit"),settings.collector_pairs,exchange=settings.exchange_name,notional_eur=PER_TRADE_EUR,buy_fee_rate=settings.binance_buy_fee_rate,sell_fee_rate=settings.binance_sell_fee_rate,spread_rate=settings.binance_spread_rate,slippage_rate=settings.binance_slippage_rate,pullback_bars=3,max_bars_per_pair=1800)
        summary_1h=audit_1h.run()
        if not summary_1h.get("skipped"):
            message=audit_1h.format_report(summary_1h)
            if message: event_bus.publish(Event(EventType.REPORT_READY,{"message":message,"trusted_html":True,"report_type":"VELEZ_1H_AUDIT"}))
            logger.info("VELEZ_1H_AUDIT_REPORT_SENT qualified=%s",summary_1h.get("qualified",0))
        else: logger.info("VELEZ_1H_AUDIT_SKIP already_completed=true")
    except Exception: logger.exception("VELEZ_1H_AUDIT_FATAL_GUARD action=CONTINUE_RUNTIME")

    try:
        net_audit=VelezUTC0612NetAudit(postgres,get_module_logger("velez-utc0612-net-audit"),settings.collector_pairs,exchange=settings.exchange_name,pullback_bars=3,max_bars_per_pair=3200,notional_eur=PER_TRADE_EUR,buy_fee_rate=settings.binance_buy_fee_rate,sell_fee_rate=settings.binance_sell_fee_rate,spread_rate=settings.binance_spread_rate,slippage_rate=settings.binance_slippage_rate)
        net_summary=net_audit.run()
        if not net_summary.get("skipped"):
            message=net_audit.format_report(net_summary)
            if message: event_bus.publish(Event(EventType.REPORT_READY,{"message":message,"trusted_html":True,"report_type":"VELEZ_UTC0612_NET_AUDIT"}))
            logger.info("VELEZ_UTC0612_NET_AUDIT_REPORT_SENT utc_n=%s holdout_n=%s",net_summary.get("utc_n",0),net_summary.get("utc_hold_n",0))
        else: logger.info("VELEZ_UTC0612_NET_AUDIT_SKIP already_completed=true")
    except Exception: logger.exception("VELEZ_UTC0612_NET_AUDIT_FATAL_GUARD action=CONTINUE_RUNTIME")

    try:
        structure_audit=VelezStructureEconomicsAudit(postgres,get_module_logger("velez-structure-economics-audit"),settings.collector_pairs,exchange=settings.exchange_name,pullback_bars=3,max_bars_per_pair=3200,notional_eur=PER_TRADE_EUR,buy_fee_rate=settings.binance_buy_fee_rate,sell_fee_rate=settings.binance_sell_fee_rate,spread_rate=settings.binance_spread_rate,slippage_rate=settings.binance_slippage_rate)
        structure_summary=structure_audit.run()
        if not structure_summary.get("skipped"):
            message=structure_audit.format_report(structure_summary)
            if message: event_bus.publish(Event(EventType.REPORT_READY,{"message":message,"trusted_html":True,"report_type":"VELEZ_STRUCTURE_ECONOMICS_AUDIT"}))
            logger.info("VELEZ_STRUCTURE_ECONOMICS_AUDIT_REPORT_SENT rows=%s hold=%s",structure_summary.get("rows",0),structure_summary.get("hold",0))
        else: logger.info("VELEZ_STRUCTURE_ECONOMICS_AUDIT_SKIP already_completed=true")
    except Exception: logger.exception("VELEZ_STRUCTURE_ECONOMICS_AUDIT_FATAL_GUARD action=CONTINUE_RUNTIME")

    try:
        pullback_audit=VelezPullbackEntryAudit(postgres,get_module_logger("velez-pullback-entry-audit"),settings.collector_pairs,exchange=settings.exchange_name,pullback_bars=3,max_bars_per_pair=3200,notional_eur=PER_TRADE_EUR,maker_fee_rate=BINANCE_VIP1_MAKER_FEE_RATE,taker_fee_rate=BINANCE_VIP1_TAKER_FEE_RATE,spread_rate=settings.binance_spread_rate,slippage_rate=settings.binance_slippage_rate)
        pullback_summary=pullback_audit.run()
        if not pullback_summary.get("skipped"):
            message=pullback_audit.format_report(pullback_summary)
            if message: event_bus.publish(Event(EventType.REPORT_READY,{"message":message,"trusted_html":True,"report_type":"VELEZ_PULLBACK_ENTRY_AUDIT"}))
            logger.info("VELEZ_PULLBACK_ENTRY_AUDIT_REPORT_SENT rows=%s hold=%s",pullback_summary.get("rows",0),pullback_summary.get("hold",0))
        else: logger.info("VELEZ_PULLBACK_ENTRY_AUDIT_SKIP already_completed=true")
    except Exception: logger.exception("VELEZ_PULLBACK_ENTRY_AUDIT_FATAL_GUARD action=CONTINUE_RUNTIME")

    Velez15mScheduler(settings,collector,None,None,scanner,monitor).run_forever()

if __name__ == "__main__": main()
