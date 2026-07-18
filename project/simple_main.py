"""Railway entrypoint for the simple 20-crypto Telegram scanner."""
from __future__ import annotations

import os
from dataclasses import replace

import project.notification_engine.service as notification_service
from project.config.settings import PlatformSettings, load_settings
from project.data_collector.kraken_client import KrakenOhlcClient
from project.data_collector.repository import MarketDataRepository
from project.data_collector.service import DataCollectorService
from project.database.migrations import run_migrations
from project.database.postgres import (
    PostgresClient,
    PostgresConfig,
    PostgresUnavailableError,
    parse_postgres_connection_info,
    sanitize_postgres_error,
)
from project.notification_engine.service import NotificationEngine
from project.notification_engine.simple_formatters import format_simple_signal_message
from project.position_monitor.service import PositionMonitor
from project.reliable_simple_scanner import ReliableSimpleStrategyScanner
from project.shared.events import EventBus
from project.shared.logging import get_module_logger
from project.simple_scheduler import SimpleMarketScheduler


RAILWAY_LIGHT_MISSING_DATABASE_URL = (
    "DATABASE_URL missing: Railway PostgreSQL is required in RAILWAY_LIGHT mode"
)


def apply_simple_policy(settings: PlatformSettings) -> PlatformSettings:
    """Force the small operational architecture regardless of obsolete Railway flags."""
    return replace(
        settings,
        enable_research_engine=False,
        run_research_on_startup=False,
        enable_decision_engine=False,
        operational_timeframe="15m",
        collector_timeframes=["15m", "1h"],
        scheduler_collector_seconds=900,
        scheduler_strategy_seconds=900,
        scheduler_position_monitor_seconds=900,
        run_data_collector_on_startup=True,
        enable_watchlist_alerts=False,
        signal_classes=["B"],
        operative_signal_classes=["B"],
        watchlist_signal_classes=[],
    )


def build_postgres(settings: PlatformSettings) -> PostgresClient:
    if settings.run_mode == "RAILWAY_LIGHT" and not settings.database_url:
        raise PostgresUnavailableError(RAILWAY_LIGHT_MISSING_DATABASE_URL)
    postgres = PostgresClient(PostgresConfig(settings.database_url))
    postgres.test_connection()
    return postgres


def build_collector(
    settings: PlatformSettings,
    event_bus: EventBus,
    postgres: PostgresClient,
) -> DataCollectorService:
    kraken = KrakenOhlcClient(
        api_base=settings.kraken_api_base,
        sleep_seconds=settings.kraken_api_sleep_seconds,
        max_retries=settings.kraken_max_retries,
        timeout_seconds=settings.kraken_timeout_seconds,
    )
    repository = MarketDataRepository(postgres, settings.exchange_name)
    return DataCollectorService(kraken, repository, event_bus)


def main() -> None:
    settings = apply_simple_policy(load_settings())
    logger = get_module_logger("system")
    logger.info("======================================")
    logger.info("PROJECT MAIN VERSION: 2026-07-18 SIMPLE 20-CRYPTO SCANNER")
    logger.info("======================================")
    logger.info(
        "SIMPLE_MODE pairs=%s timeframes=%s interval_seconds=%s strategies=%s research=false classes_hidden=true",
        len(settings.collector_pairs),
        settings.collector_timeframes,
        settings.scheduler_collector_seconds,
        ["Trend Pullback", "Breakout 20", "Mean Reversion Bollinger"],
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

    collector = build_collector(settings, event_bus, postgres)
    scanner = ReliableSimpleStrategyScanner(
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
        signal_cooldown_minutes=max(60, settings.signal_cooldown_minutes),
        min_signal_score=float(os.getenv("SIMPLE_MIN_SIGNAL_SCORE", "70")),
        min_net_rr=float(os.getenv("SIMPLE_MIN_NET_RR", "1.20")),
        min_net_profit_eur=float(os.getenv("SIMPLE_MIN_NET_PROFIT_EUR", "0.30")),
        max_signals_per_cycle=int(os.getenv("SIMPLE_MAX_SIGNALS_PER_CYCLE", "3")),
        enable_daily_signal_report=settings.enable_daily_signal_report,
        daily_signal_report_hours=settings.daily_signal_report_hours,
    )
    position_monitor = PositionMonitor(
        postgres,
        event_bus,
        ambiguous_candle_mode=settings.ambiguous_candle_mode,
    )

    notification: NotificationEngine | None = None
    if settings.enable_notification_engine:
        notification = NotificationEngine(
            event_bus,
            enabled=True,
            max_message_length=settings.telegram_max_message_length,
            max_retries=settings.telegram_max_retries,
            postgres=postgres,
        )
        notification.subscribe()
        if settings.telegram_send_startup_message:
            notification.send_telegram(
                "🟦 <b>SCANNER CRYPTO ATTIVO</b>\n\n"
                "Monitoraggio: <b>20 coppie</b>\n"
                "Timeframe: <b>15m</b> con conferma <b>1h</b>\n"
                "Strategie: <b>Trend Pullback, Breakout 20, Mean Reversion</b>\n"
                "Ricerca quantistica: <b>disattivata</b>\n\n"
                "Telegram riceverà soltanto setup che superano tutte le regole operative."
            )

    scheduler = SimpleMarketScheduler(
        settings,
        collector,
        None,
        None,
        scanner,
        position_monitor,
    )
    scheduler.run_forever()


if __name__ == "__main__":
    main()
