"""Railway entrypoint for V6 shadow-first capital protection."""
from __future__ import annotations

import os

import project.notification_engine.service as notification_service
from project.audited_formatters import (
    format_audited_outcome_message,
    format_simple_report_message,
    format_simple_signal_message,
)
from project.capital_protection_migration import run_capital_protection_migration
from project.capital_protection_report import CapitalProtectionReportScanner
from project.capital_protection_scanner import RUNTIME_VERSION
from project.config.settings import load_settings
from project.database.migrations import run_migrations
from project.database.postgres import parse_postgres_connection_info, sanitize_postgres_error
from project.harmonic_live_scanner import LIVE_LONG_STRATEGIES
from project.operational_scheduler import OperationalMarketScheduler
from project.reliable_notification import ReliableNotificationEngine
from project.shadow_signal_monitor import ShadowValidationMonitor
from project.shared.events import EventBus
from project.shared.logging import get_module_logger
from project.simple_main import apply_simple_policy, build_collector, build_postgres


def main() -> None:
    settings = apply_simple_policy(load_settings())
    logger = get_module_logger("system")
    logger.info("======================================")
    logger.info("PROJECT MAIN VERSION: 2026-07-20 CAPITAL PROTECTION V6")
    logger.info("======================================")
    logger.info(
        "CAPITAL_PROTECTION_MODE runtime=%s pairs=%s strategies=%s shadow_first=true "
        "live_requires_empirical_edge=true min_shadow_samples=%s min_shadow_pf=%.2f "
        "min_shadow_expectancy_r=%.2f max_shadow_loss_streak=%s "
        "live_stop_trigger=%s live_stop_24h_limit=%s live_cooldown_hours=%s "
        "short_signals=false",
        RUNTIME_VERSION,
        len(settings.collector_pairs),
        list(LIVE_LONG_STRATEGIES),
        int(os.getenv("CAPITAL_MIN_SHADOW_SAMPLES", "30")),
        float(os.getenv("CAPITAL_MIN_SHADOW_PF", "1.30")),
        float(os.getenv("CAPITAL_MIN_SHADOW_EXPECTANCY_R", "0.10")),
        int(os.getenv("CAPITAL_MAX_SHADOW_LOSS_STREAK", "4")),
        int(os.getenv("CAPITAL_LIVE_STOP_TRIGGER", "3")),
        int(os.getenv("CAPITAL_LIVE_STOP_24H_LIMIT", "2")),
        int(os.getenv("CAPITAL_LIVE_COOLDOWN_HOURS", "24")),
    )

    try:
        postgres = build_postgres(settings)
    except Exception as exc:
        logger.error(
            "PostgreSQL connection failed: %s",
            sanitize_postgres_error(str(exc), settings.database_url),
        )
        raise SystemExit(1) from exc

    logger.info("PostgreSQL connection: OK")
    logger.info(
        "PostgreSQL %s",
        parse_postgres_connection_info(settings.database_url).display(),
    )
    run_migrations(postgres)
    run_capital_protection_migration(postgres)

    event_bus = EventBus()
    notification_service.format_signal_message = format_simple_signal_message
    notification_service.format_report_message = format_simple_report_message
    notification_service.format_outcome_message = format_audited_outcome_message

    collector = build_collector(settings, event_bus, postgres)
    scanner = CapitalProtectionReportScanner(
        postgres,
        event_bus,
        settings.collector_pairs,
        exchange=settings.exchange_name,
        trade_notional_eur=settings.trade_notional_eur,
        buy_fee_rate=settings.binance_buy_fee_rate,
        sell_fee_rate=settings.binance_sell_fee_rate,
        spread_rate=settings.binance_spread_rate,
        slippage_rate=settings.binance_slippage_rate,
        quantity_step=settings.binance_quantity_step,
        min_qty=settings.binance_min_qty,
        min_notional_eur=settings.binance_min_notional_eur,
        signal_cooldown_minutes=max(90, settings.signal_cooldown_minutes),
        min_signal_score=float(os.getenv("SIMPLE_MIN_SIGNAL_SCORE", "68")),
        min_net_rr=float(os.getenv("SIMPLE_MIN_NET_RR", "1.10")),
        min_net_profit_eur=float(os.getenv("SIMPLE_MIN_NET_PROFIT_EUR", "0.25")),
        max_signals_per_cycle=int(os.getenv("SIMPLE_MAX_SIGNALS_PER_CYCLE", "3")),
        max_open_signals=int(os.getenv("SIMPLE_MAX_OPEN_POSITIONS", "5")),
        max_correlated_per_cycle=int(os.getenv("SIMPLE_MAX_CORRELATED_PER_CYCLE", "2")),
        correlation_threshold=float(os.getenv("SIMPLE_CORRELATION_THRESHOLD", "0.85")),
        performance_guard_min_trades=int(os.getenv("SIMPLE_PERFORMANCE_GUARD_MIN_TRADES", "20")),
        performance_guard_min_pf=float(os.getenv("SIMPLE_PERFORMANCE_GUARD_MIN_PF", "0.85")),
        performance_pause_hours=int(os.getenv("SIMPLE_PERFORMANCE_PAUSE_HOURS", "72")),
        probation_interval_hours=int(os.getenv("SIMPLE_PROBATION_INTERVAL_HOURS", "24")),
        probation_min_score=float(os.getenv("SIMPLE_PROBATION_MIN_SCORE", "76")),
        probation_min_rr=float(os.getenv("SIMPLE_PROBATION_MIN_RR", "1.25")),
        max_net_loss_eur=float(os.getenv("SIMPLE_MAX_NET_LOSS_EUR", "1.00")),
        loss_streak_trigger=int(os.getenv("SIMPLE_LOSS_STREAK_TRIGGER", "3")),
        loss_guard_hours=int(os.getenv("SIMPLE_LOSS_GUARD_HOURS", "12")),
        shadow_min_samples=int(os.getenv("CAPITAL_MIN_SHADOW_SAMPLES", "30")),
        shadow_min_profit_factor=float(os.getenv("CAPITAL_MIN_SHADOW_PF", "1.30")),
        shadow_min_expectancy_r=float(os.getenv("CAPITAL_MIN_SHADOW_EXPECTANCY_R", "0.10")),
        shadow_min_win_margin=float(os.getenv("CAPITAL_MIN_SHADOW_WIN_MARGIN", "0.03")),
        shadow_recent_window=int(os.getenv("CAPITAL_SHADOW_RECENT_WINDOW", "10")),
        shadow_max_loss_streak=int(os.getenv("CAPITAL_MAX_SHADOW_LOSS_STREAK", "4")),
        max_shadow_per_cycle=int(os.getenv("CAPITAL_MAX_SHADOW_PER_CYCLE", "3")),
        live_stop_trigger=int(os.getenv("CAPITAL_LIVE_STOP_TRIGGER", "3")),
        live_stop_24h_limit=int(os.getenv("CAPITAL_LIVE_STOP_24H_LIMIT", "2")),
        live_cooldown_hours=int(os.getenv("CAPITAL_LIVE_COOLDOWN_HOURS", "24")),
        enable_daily_signal_report=settings.enable_daily_signal_report,
        daily_signal_report_hours=settings.daily_signal_report_hours,
    )
    position_monitor = ShadowValidationMonitor(
        postgres,
        event_bus,
        ambiguous_candle_mode=settings.ambiguous_candle_mode,
    )

    notification = ReliableNotificationEngine(
        event_bus,
        enabled=True,
        max_message_length=settings.telegram_max_message_length,
        max_retries=settings.telegram_max_retries,
        postgres=postgres,
    )
    notification.subscribe()
    if settings.telegram_send_startup_message:
        notification.send_telegram(
            "🛡 <b>PROTEZIONE CAPITALE V6 ATTIVA</b>\n\n"
            "Strategie monitorate: <b>7 LONG</b>\n"
            "Modalità iniziale: <b>SHADOW · nessun segnale operativo</b>\n"
            "I setup validi vengono seguiti internamente senza usare denaro reale.\n\n"
            "Ammissione automatica al live solo dopo:\n"
            "• almeno <b>30</b> operazioni shadow non ambigue\n"
            "• Profit Factor almeno <b>1,30</b>\n"
            "• expectancy almeno <b>+0,10R</b>\n"
            "• risultati recenti ancora positivi\n"
            "• nessuna sequenza eccessiva di stop\n\n"
            "Circuit breaker live: <b>3 stop consecutivi oppure 2 stop in 24h</b>\n"
            "Perdita teorica massima per eventuale segnale ammesso: <b>€1,00</b>\n"
            "Segnali SHORT: <b>disattivati</b>"
        )

    OperationalMarketScheduler(
        settings,
        collector,
        None,
        None,
        scanner,
        position_monitor,
    ).run_forever()


if __name__ == "__main__":
    main()
