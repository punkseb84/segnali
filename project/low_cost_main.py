"""Low-memory Railway entrypoint for the always-on crypto signal service."""
from __future__ import annotations

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
from project.decision_engine.service import DecisionEngine
from project.low_cost_scheduler import LowCostPlatformScheduler
from project.notification_engine.robust_formatters import (
    format_robust_signal_message,
    format_robust_watchlist_message,
)
from project.notification_engine.service import NotificationEngine
from project.position_monitor.service import PositionMonitor
from project.research_engine.robust_repository import RobustResearchRepository
from project.shared.events import EventBus
from project.shared.logging import get_module_logger
from project.strategy_engine.robust_operational import RobustEdgeOperationalStrategyEngine


RAILWAY_LIGHT_MISSING_DATABASE_URL = (
    "DATABASE_URL missing: Railway PostgreSQL is required in RAILWAY_LIGHT mode"
)


def apply_low_cost_policy(settings: PlatformSettings) -> PlatformSettings:
    """Enforce a bounded always-on workload even when older Railway vars are present."""
    operational = settings.operational_timeframe
    timeframes = [operational]
    if "1h" in settings.collector_timeframes and operational != "1h":
        timeframes.append("1h")
    return replace(
        settings,
        enable_research_engine=False,
        run_research_on_startup=False,
        collector_timeframes=timeframes,
        scheduler_collector_seconds=max(900, settings.scheduler_collector_seconds),
        scheduler_decision_seconds=max(900, settings.scheduler_decision_seconds),
        scheduler_strategy_seconds=max(900, settings.scheduler_strategy_seconds),
        scheduler_position_monitor_seconds=max(
            900, settings.scheduler_position_monitor_seconds
        ),
        strategy_candidate_limit=min(25, max(1, settings.strategy_candidate_limit)),
        strategy_ohlc_limit=min(500, max(220, settings.strategy_ohlc_limit)),
        research_batch_size=min(60, max(1, settings.research_batch_size)),
        max_research_runtime_minutes=min(
            6, max(1, settings.max_research_runtime_minutes)
        ),
    )


def build_postgres(settings: PlatformSettings) -> PostgresClient:
    if settings.run_mode == "RAILWAY_LIGHT" and not settings.database_url:
        raise PostgresUnavailableError(RAILWAY_LIGHT_MISSING_DATABASE_URL)
    postgres = PostgresClient(PostgresConfig(settings.database_url))
    postgres.test_connection()
    return postgres


def build_data_collector(
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
    return DataCollectorService(
        kraken,
        MarketDataRepository(postgres, settings.exchange_name),
        event_bus,
    )


def build_strategy_engine(
    settings: PlatformSettings,
    repository: RobustResearchRepository,
    decision_engine: DecisionEngine,
    event_bus: EventBus,
) -> RobustEdgeOperationalStrategyEngine:
    return RobustEdgeOperationalStrategyEngine(
        repository,
        decision_engine,
        event_bus,
        min_profit_factor=settings.min_signal_profit_factor,
        operational_timeframe=settings.operational_timeframe,
        trade_notional_eur=settings.trade_notional_eur,
        buy_fee_rate=settings.binance_buy_fee_rate,
        sell_fee_rate=settings.binance_sell_fee_rate,
        spread_rate=settings.binance_spread_rate,
        pair_spread_rates=settings.binance_pair_spread_rates,
        min_tp1_net_profit_eur=settings.min_tp1_net_profit_eur,
        signal_cooldown_minutes=settings.signal_cooldown_minutes,
        min_net_rr=settings.min_net_rr,
        min_historical_ev_eur=settings.min_historical_ev_eur,
        historical_ev_tolerance_eur=settings.historical_ev_tolerance_eur,
        hard_block_negative_historical_ev=settings.hard_block_negative_historical_ev,
        min_live_setup_score=settings.min_live_setup_score,
        min_operative_signal_score=settings.min_operative_signal_score,
        min_watchlist_signal_score=settings.min_watchlist_signal_score,
        min_research_trades=settings.min_research_trades,
        min_research_trades_b=settings.min_research_trades_b,
        require_regime_alignment_for_operative=settings.require_regime_alignment_for_operative,
        allow_high_score_regime_mismatch_b=settings.allow_high_score_regime_mismatch_b,
        dynamic_economic_target=settings.dynamic_economic_target,
        min_watchlist_net_profit_eur=settings.min_watchlist_notification_net_profit_eur,
        min_watchlist_net_rr=settings.min_watchlist_notification_net_rr,
        min_watchlist_live_score=settings.min_watchlist_notification_live_score,
        min_watchlist_progress_ratio=settings.min_watchlist_progress_ratio,
        min_watchlist_sample_size=settings.min_watchlist_sample_size,
        slippage_rate=settings.binance_slippage_rate,
        quantity_step=settings.binance_quantity_step,
        min_qty=settings.binance_min_qty,
        min_notional_eur=settings.binance_min_notional_eur,
        max_take_profit_distance_pct=settings.max_take_profit_distance_pct,
        min_stop_atr_ratio=settings.min_stop_atr_ratio,
        min_stop_spread_multiple=settings.min_stop_spread_multiple,
        max_tp_atr_multiple=settings.max_tp_atr_multiple,
        historical_mfe_percentile=settings.historical_mfe_percentile,
        min_probability_sample_size=settings.min_probability_sample_size,
        probability_horizon_candles=settings.probability_horizon_candles,
        ohlc_limit=settings.strategy_ohlc_limit,
        allowed_signal_classes=settings.signal_classes,
        operative_signal_classes=settings.operative_signal_classes,
        watchlist_signal_classes=settings.watchlist_signal_classes,
        enable_watchlist_alerts=settings.enable_watchlist_alerts,
        max_candidate_evaluations=settings.strategy_candidate_limit,
        enable_daily_signal_report=settings.enable_daily_signal_report,
        daily_signal_report_hours=settings.daily_signal_report_hours,
    )


def main() -> None:
    settings = apply_low_cost_policy(load_settings())
    logger = get_module_logger("system")
    logger.info("======================================")
    logger.info("PROJECT MAIN VERSION: 2026-07-17 TELEGRAM RECOVERY BUILD")
    logger.info("======================================")
    logger.info(
        "LOW_COST_POLICY research_in_parent=false collector_seconds=%s "
        "timeframes=%s candidate_limit=%s strategy_ohlc_limit=%s "
        "position_monitor_after_collector=true",
        settings.scheduler_collector_seconds,
        settings.collector_timeframes,
        settings.strategy_candidate_limit,
        settings.strategy_ohlc_limit,
    )
    logger.info("DATABASE_URL detected: %s", "yes" if settings.database_url else "no")

    try:
        postgres = build_postgres(settings)
    except Exception as exc:
        logger.error(
            "PostgreSQL connection failed: %s",
            sanitize_postgres_error(str(exc), settings.database_url),
        )
        raise SystemExit(1) from exc

    logger.info("PostgreSQL connection: OK")
    logger.info("PostgreSQL %s", parse_postgres_connection_info(settings.database_url).display())
    run_migrations(postgres)

    notification_service.format_signal_message = format_robust_signal_message
    notification_service.format_watchlist_message = format_robust_watchlist_message

    event_bus = EventBus()
    data_collector = (
        build_data_collector(settings, event_bus, postgres)
        if settings.enable_data_collector
        else None
    )
    repository = RobustResearchRepository(postgres)
    decision_engine = (
        DecisionEngine(
            postgres,
            event_bus,
            timeframe=settings.operational_timeframe,
        )
        if settings.enable_decision_engine
        else None
    )
    strategy_engine = (
        build_strategy_engine(settings, repository, decision_engine, event_bus)
        if settings.enable_strategy_engine and decision_engine is not None
        else None
    )
    position_monitor = (
        PositionMonitor(
            postgres,
            event_bus,
            ambiguous_candle_mode=settings.ambiguous_candle_mode,
        )
        if settings.enable_position_monitor
        else None
    )
    if settings.enable_notification_engine:
        notification = NotificationEngine(
            event_bus,
            enabled=True,
            max_message_length=settings.telegram_max_message_length,
            max_retries=settings.telegram_max_retries,
            postgres=postgres,
        )
        notification.subscribe()
        notification.send_telegram(
            "🟦 <b>BOT ATTIVO</b>\n\n"
            "Il collegamento Telegram funziona.\n"
            "La ricerca robusta iniziale partirà entro circa 45 secondi e verrà eseguita "
            "in un processo separato per contenere i costi Railway.\n\n"
            "⚠️ Questo è un messaggio di stato, non un segnale di ingresso."
        )

    scheduler = LowCostPlatformScheduler(
        settings,
        data_collector,
        None,
        decision_engine,
        strategy_engine,
        position_monitor,
    )
    scheduler.run_forever()


if __name__ == "__main__":
    main()
