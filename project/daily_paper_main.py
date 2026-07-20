"""Railway entrypoint for V6.1 daily LONG paper signals plus V6 validation."""
from __future__ import annotations

import os

import project.notification_engine.service as notification_service
from project.capital_protection_migration import run_capital_protection_migration
from project.capital_protection_scanner import RUNTIME_VERSION
from project.config.settings import load_settings
from project.daily_paper_formatters import (
    format_daily_paper_outcome_message,
    format_daily_paper_signal_message,
)
from project.daily_paper_position_monitor import DailyPaperPositionMonitor
from project.daily_paper_scanner import DailyPaperSignalScanner, PAPER_RUNTIME_VERSION
from project.database.migrations import run_migrations
from project.database.postgres import parse_postgres_connection_info, sanitize_postgres_error
from project.harmonic_live_scanner import LIVE_LONG_STRATEGIES
from project.notification_engine.simple_formatters import format_simple_report_message
from project.operational_scheduler import OperationalMarketScheduler
from project.reliable_notification import ReliableNotificationEngine
from project.shared.events import EventBus
from project.shared.logging import get_module_logger
from project.simple_main import apply_simple_policy, build_collector, build_postgres


def main() -> None:
    settings = apply_simple_policy(load_settings())
    logger = get_module_logger("system")
    shadow_monitor_limit = max(
        20, int(os.getenv("CAPITAL_SHADOW_MONITOR_LIMIT", "200"))
    )
    paper_max_per_day = max(1, int(os.getenv("DAILY_PAPER_MAX_PER_DAY", "3")))
    paper_max_per_cycle = max(1, int(os.getenv("DAILY_PAPER_MAX_PER_CYCLE", "1")))
    paper_min_interval = max(
        30, int(os.getenv("DAILY_PAPER_MIN_INTERVAL_MINUTES", "120"))
    )
    paper_pair_cooldown = max(
        90, int(os.getenv("DAILY_PAPER_PAIR_COOLDOWN_MINUTES", "360"))
    )

    logger.info("======================================")
    logger.info("PROJECT MAIN VERSION: 2026-07-20 DAILY PAPER LONG V6.1")
    logger.info("======================================")
    logger.info(
        "DAILY_PAPER_MODE paper_runtime=%s capital_runtime=%s pairs=%s strategies=%s "
        "paper_enabled=true paper_max_day=%s paper_max_cycle=%s paper_interval_minutes=%s "
        "pair_cooldown_minutes=%s paper_min_score=%.1f paper_min_net_rr=%.2f "
        "paper_min_net_profit=%.2f shadow_validation_parallel=true "
        "live_requires_empirical_edge=true short_signals=false leverage=false",
        PAPER_RUNTIME_VERSION,
        RUNTIME_VERSION,
        len(settings.collector_pairs),
        list(LIVE_LONG_STRATEGIES),
        paper_max_per_day,
        paper_max_per_cycle,
        paper_min_interval,
        paper_pair_cooldown,
        float(os.getenv("DAILY_PAPER_MIN_SCORE", "64")),
        float(os.getenv("DAILY_PAPER_MIN_NET_RR", "1.05")),
        float(os.getenv("DAILY_PAPER_MIN_NET_PROFIT_EUR", "0.20")),
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
    notification_service.format_signal_message = format_daily_paper_signal_message
    notification_service.format_report_message = format_simple_report_message
    notification_service.format_outcome_message = format_daily_paper_outcome_message

    collector = build_collector(settings, event_bus, postgres)
    scanner = DailyPaperSignalScanner(
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
        paper_max_per_day=paper_max_per_day,
        paper_max_per_cycle=paper_max_per_cycle,
        paper_min_interval_minutes=paper_min_interval,
        paper_pair_cooldown_minutes=paper_pair_cooldown,
        paper_min_score=float(os.getenv("DAILY_PAPER_MIN_SCORE", "64")),
        paper_min_net_rr=float(os.getenv("DAILY_PAPER_MIN_NET_RR", "1.05")),
        paper_min_net_profit_eur=float(
            os.getenv("DAILY_PAPER_MIN_NET_PROFIT_EUR", "0.20")
        ),
        enable_daily_signal_report=settings.enable_daily_signal_report,
        daily_signal_report_hours=settings.daily_signal_report_hours,
    )
    position_monitor = DailyPaperPositionMonitor(
        postgres,
        event_bus,
        ambiguous_candle_mode=settings.ambiguous_candle_mode,
        max_shadow_signals_per_cycle=shadow_monitor_limit,
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
            "🧪 <b>DAILY PAPER LONG V6.1 ATTIVO</b>\n\n"
            "Obiettivo: <b>1–3 segnali LONG paper al giorno</b>, quando esistono setup tecnicamente validi.\n"
            f"Massimo giornaliero: <b>{paper_max_per_day}</b>\n"
            f"Distanza minima tra segnali: <b>{paper_min_interval} minuti</b>\n"
            "Watchlist: <b>20 coppie</b> · timeframe <b>15m + conferma 1h</b>\n\n"
            "I segnali PAPER vengono inviati su Telegram e monitorati fino a TP o SL.\n"
            "La validazione shadow V6 continua in parallelo e resta necessaria per eventuali segnali validati.\n\n"
            "Segnali SHORT: <b>disattivati</b>\n"
            "Leva: <b>disattivata</b>\n"
            "⚠️ Nessun segnale paper è una garanzia di profitto o un invito a usare denaro reale."
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
