"""Railway entrypoint for strict Relative Strength and auditable TP/SL outcomes."""
from __future__ import annotations

import os

import project.notification_engine.service as notification_service
from project.audited_formatters import (
    format_audited_outcome_message,
    format_simple_report_message,
    format_simple_signal_message,
)
from project.audited_position_monitor import AuditedPositionMonitor
from project.config.settings import load_settings
from project.database.migrations import run_migrations
from project.database.postgres import parse_postgres_connection_info, sanitize_postgres_error
from project.harmonic_live_scanner import LIVE_LONG_STRATEGIES
from project.operational_scheduler import OperationalMarketScheduler
from project.reliable_notification import ReliableNotificationEngine
from project.shared.events import EventBus
from project.shared.logging import get_module_logger
from project.simple_main import apply_simple_policy, build_collector, build_postgres
from project.validated_live_scanner_v513 import (
    RUNTIME_VERSION,
    ValidatedLiveStrategyScannerV513,
)
from project.version_scoped_main import startup_loss_guard_status


def main() -> None:
    settings = apply_simple_policy(load_settings())
    logger = get_module_logger("system")
    logger.info("======================================")
    logger.info("PROJECT MAIN VERSION: 2026-07-19 VALIDATED LONG SCANNER V5.1.3")
    logger.info("======================================")
    logger.info(
        "VALIDATED_LIVE_MODE runtime=%s pairs=%s timeframes=%s interval_seconds=%s "
        "strategies=%s relative_strength_strict=true relative_min_net_rr=1.45 "
        "entry_extension_max_atr=0.80 exchange_aware_monitor=true "
        "outcome_ohlc_audit=true ambiguous_excluded=true short_signals=false",
        RUNTIME_VERSION,
        len(settings.collector_pairs),
        settings.collector_timeframes,
        settings.scheduler_collector_seconds,
        list(LIVE_LONG_STRATEGIES),
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

    event_bus = EventBus()
    notification_service.format_signal_message = format_simple_signal_message
    notification_service.format_report_message = format_simple_report_message
    notification_service.format_outcome_message = format_audited_outcome_message

    collector = build_collector(settings, event_bus, postgres)
    scanner = ValidatedLiveStrategyScannerV513(
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
        enable_daily_signal_report=settings.enable_daily_signal_report,
        daily_signal_report_hours=settings.daily_signal_report_hours,
    )
    position_monitor = AuditedPositionMonitor(
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
    startup_streak, _startup_guard_active, startup_guard_label = startup_loss_guard_status(scanner)
    logger.info(
        "STARTUP_LOSS_GUARD runtime=%s streak=%s status=%s",
        RUNTIME_VERSION,
        startup_streak,
        startup_guard_label,
    )
    if settings.telegram_send_startup_message:
        notification.send_telegram(
            "🟦 <b>SCANNER LONG V5.1.3 ATTIVO</b>\n\n"
            "Monitoraggio: <b>20 coppie</b>\n"
            "Timeframe: <b>15m</b> con conferma <b>1h</b>\n"
            "Strategie: <b>7 LONG live</b>\n"
            "Relative Strength: <b>RSI e MACD obbligatori, R/R netto minimo 1,45</b>\n"
            "Ingresso momentum: <b>pullback e ripartenza, massimo 0,80 ATR sopra EMA20</b>\n"
            "Monitor TP/SL: <b>exchange corretto e candela OHLC registrata</b>\n"
            "Statistiche: <b>candele ambigue escluse</b>\n"
            f"Stop consecutivi V5.1.3: <b>{startup_streak}</b>\n"
            f"Modalità prudente: <b>{startup_guard_label}</b>\n"
            "Perdita netta stimata massima: <b>€1,00</b>\n"
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
