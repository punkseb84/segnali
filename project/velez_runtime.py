"""Clean Railway runtime for Oliver Velez 15m PAPER Mode 2 protected lifecycle."""
from __future__ import annotations

from dataclasses import replace
from html import escape
import threading

import project.notification_engine.service as notification_service
import project.relative_strength_monitor as monitor_module
from project.capital_protection_migration import run_capital_protection_migration
from project.config.settings import load_settings
from project.database.migrations import run_migrations
from project.database.postgres import parse_postgres_connection_info, sanitize_postgres_error
from project.shared.events import Event, EventBus, EventType
from project.shared.logging import get_module_logger
from project.simple_main import apply_simple_policy, build_collector, build_postgres
from project.high_magnitude_edge_discovery_36m_audit import HighMagnitudeEdgeDiscovery36mAudit
from project.velez_formatters import format_velez_outcome, format_velez_signal
from project.velez_mode2_scanner import RUNTIME_VERSION, VelezMode2Scanner
from project.velez_monitor import Velez15mMonitor
from project.velez_notification import VelezNotificationEngine
from project.velez_scheduler import Velez15mScheduler
from project.velez_universe import resolve_top_market_cap_pairs

PUBLIC_BUDGET_EUR = 100.0
MAX_OPEN = 4
PER_TRADE_EUR = 25.0
BINANCE_VIP1_MAKER_FEE_RATE = .0007
BINANCE_VIP1_TAKER_FEE_RATE = .0016


def _retire_previous_open_signals(postgres: object) -> None:
    postgres.execute(
        """UPDATE signals.generated_signals
              SET status='EXPIRED',
                  closed_at=COALESCE(closed_at,NOW()),
                  outcome_resolution='REPLACED_BY_VELEZ_MODE2_PROTECT_RUNTIME'
            WHERE status IN ('NEW','OPEN')
              AND COALESCE(score_breakdown->>'runtime_version','') <> %s""",
        (RUNTIME_VERSION,),
    )


def _run_requested_high_magnitude_discovery(settings: object, event_bus: EventBus) -> None:
    """Run high-magnitude cross-sectional research without blocking live PAPER."""
    logger = get_module_logger('high-magnitude-edge-discovery-36m-audit')
    try:
        event_bus.publish(Event(
            EventType.REPORT_READY,
            {
                'message': (
                    '🔭 <b>HIGH-MAGNITUDE EDGE DISCOVERY · 36 MESI</b>\n'
                    'Audit avviato correttamente.\n'
                    'Scarico BTC + ETH/SOL/BNB/XRP/DOGE 1h dal 2023 al 2026 e testo eventi cross-sectional/lead-lag.\n'
                    'Gate DEV: lordo medio ≥0.70% per trade prima di poter avanzare.\n'
                    'Il live PAPER continua normalmente; il report finale arriverà in un secondo messaggio.'
                ),
                'trusted_html': True,
                'report_type': 'HIGH_MAGNITUDE_EDGE_DISCOVERY_36M_STATUS',
            },
        ))
        audit_postgres = build_postgres(settings)
        audit = HighMagnitudeEdgeDiscovery36mAudit(
            audit_postgres,
            logger,
            settings.collector_pairs,
            exchange=settings.exchange_name,
            pullback_bars=3,
            max_bars_per_pair=3200,
            notional_eur=PER_TRADE_EUR,
            buy_fee_rate=settings.binance_buy_fee_rate,
            sell_fee_rate=settings.binance_sell_fee_rate,
            spread_rate=settings.binance_spread_rate,
            slippage_rate=settings.binance_slippage_rate,
        )
        summary = audit.run()
        if summary.get('skipped'):
            logger.info('HIGH_MAGNITUDE_EDGE_DISCOVERY_36M_SKIP already_completed=true')
            event_bus.publish(Event(
                EventType.REPORT_READY,
                {
                    'message': 'ℹ️ <b>HIGH-MAGNITUDE EDGE DISCOVERY · 36 MESI</b>\nAudit già completato per questa versione; nessun nuovo calcolo eseguito.',
                    'trusted_html': True,
                    'report_type': 'HIGH_MAGNITUDE_EDGE_DISCOVERY_36M_STATUS',
                },
            ))
            return
        message = audit.format_report(summary)
        if message:
            event_bus.publish(Event(
                EventType.REPORT_READY,
                {
                    'message': message,
                    'trusted_html': True,
                    'report_type': 'HIGH_MAGNITUDE_EDGE_DISCOVERY_36M_AUDIT',
                },
            ))
        logger.info('HIGH_MAGNITUDE_EDGE_DISCOVERY_36M_REPORT_SENT')
    except Exception as exc:
        logger.exception('HIGH_MAGNITUDE_EDGE_DISCOVERY_36M_FATAL_GUARD action=CONTINUE_RUNTIME')
        err = escape(str(exc)[:800])
        event_bus.publish(Event(
            EventType.REPORT_READY,
            {
                'message': (
                    '⚠️ <b>HIGH-MAGNITUDE EDGE DISCOVERY · 36 MESI</b>\n'
                    'Audit non completato.\n'
                    f'Errore: <code>{err}</code>\n'
                    'Il live Velez continua normalmente.'
                ),
                'trusted_html': True,
                'report_type': 'HIGH_MAGNITUDE_EDGE_DISCOVERY_36M_AUDIT_ERROR',
            },
        ))


def main() -> None:
    pairs = resolve_top_market_cap_pairs(20)
    settings = apply_simple_policy(load_settings())
    settings = replace(
        settings,
        collector_pairs=pairs,
        collector_timeframes=['15m'],
        operational_timeframe='15m',
        scheduler_collector_seconds=900,
        telegram_send_startup_message=False,
        enable_daily_signal_report=False,
        binance_buy_fee_rate=BINANCE_VIP1_TAKER_FEE_RATE,
        binance_sell_fee_rate=BINANCE_VIP1_TAKER_FEE_RATE,
    )

    logger = get_module_logger('system')
    logger.info('======================================')
    logger.info('PROJECT MAIN: VELEZ 15M MODE 2 PROTECTED PAPER ONLY')
    logger.info('======================================')
    logger.info(
        'VELEZ_RUNTIME version=%s timeframe=15m pairs=%s budget=%.2f per_trade=%.2f '
        'fixed_tp=false protection=1.5R_to_1R exit=protected_stop_or_ema20_close_or_12h '
        'relative_strength=false trix=false ichimoku=false paper=true notifier=strict',
        RUNTIME_VERSION,
        pairs,
        PUBLIC_BUDGET_EUR,
        PER_TRADE_EUR,
    )
    logger.info(
        'BINANCE_FEES vip=1 spot_maker=%.4f%% spot_taker=%.4f%% paper_entry=taker '
        'paper_exit=taker buy_fee=%.4f%% sell_fee=%.4f%% spread=%.4f%% slippage=%.4f%%',
        BINANCE_VIP1_MAKER_FEE_RATE * 100,
        BINANCE_VIP1_TAKER_FEE_RATE * 100,
        settings.binance_buy_fee_rate * 100,
        settings.binance_sell_fee_rate * 100,
        settings.binance_spread_rate * 100,
        settings.binance_slippage_rate * 100,
    )

    try:
        postgres = build_postgres(settings)
    except Exception as exc:
        logger.error(
            'PostgreSQL connection failed: %s',
            sanitize_postgres_error(str(exc), settings.database_url),
        )
        raise SystemExit(1) from exc

    logger.info('PostgreSQL connection: OK')
    logger.info('PostgreSQL %s', parse_postgres_connection_info(settings.database_url).display())
    run_migrations(postgres)
    run_capital_protection_migration(postgres)
    monitor_module.RUNTIME_VERSION = RUNTIME_VERSION

    event_bus = EventBus()
    notification_service.format_signal_message = format_velez_signal
    notification_service.format_outcome_message = format_velez_outcome

    collector = build_collector(settings, event_bus, postgres)
    scanner = VelezMode2Scanner(
        postgres,
        event_bus,
        settings.collector_pairs,
        exchange=settings.exchange_name,
        trade_notional_eur=PER_TRADE_EUR,
        buy_fee_rate=settings.binance_buy_fee_rate,
        sell_fee_rate=settings.binance_sell_fee_rate,
        spread_rate=settings.binance_spread_rate,
        slippage_rate=settings.binance_slippage_rate,
        quantity_step=settings.binance_quantity_step,
        min_qty=settings.binance_min_qty,
        min_notional_eur=settings.binance_min_notional_eur,
        signal_cooldown_minutes=60,
        max_signals_per_cycle=2,
        max_signals_per_day=8,
        max_open_positions=MAX_OPEN,
        max_per_pair_day=2,
        target_r=1.5,
        max_hold_candles=48,
        pullback_bars=3,
    )
    monitor = Velez15mMonitor(
        postgres,
        event_bus,
        ambiguous_candle_mode=settings.ambiguous_candle_mode,
        max_shadow_signals_per_cycle=300,
        sell_fee_rate=settings.binance_sell_fee_rate,
        spread_rate=settings.binance_spread_rate,
        slippage_rate=settings.binance_slippage_rate,
    )
    notification = VelezNotificationEngine(
        event_bus,
        enabled=True,
        max_message_length=settings.telegram_max_message_length,
        max_retries=settings.telegram_max_retries,
        postgres=postgres,
    )
    notification.subscribe()

    logger.info(
        'VELEZ_STARTUP_STOP_RECONCILIATION closed=%s',
        monitor.reconcile_all_active_hard_stops(),
    )
    _retire_previous_open_signals(postgres)

    threading.Thread(
        target=_run_requested_high_magnitude_discovery,
        args=(settings, event_bus),
        name='high-magnitude-edge-discovery-36m',
        daemon=True,
    ).start()
    logger.info('HIGH_MAGNITUDE_EDGE_DISCOVERY_36M_THREAD_STARTED')

    Velez15mScheduler(settings, collector, None, None, scanner, monitor).run_forever()


if __name__ == '__main__':
    main()
