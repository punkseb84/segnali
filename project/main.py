"""Entrypoint for the modular quant platform on Railway.

RAILWAY_LIGHT keeps the process alive, collects data, and processes small
research batches without requiring a dedicated server.
"""
from __future__ import annotations

from project.bootstrap import bootstrap_or_exit
from project.config.settings import PlatformSettings, load_settings
from project.data_collector.kraken_client import KrakenOhlcClient
from project.data_collector.repository import MarketDataRepository
from project.data_collector.service import DataCollectorService
from project.decision_engine.service import DecisionEngine
from project.notification_engine.service import NotificationEngine
from project.database.migrations import run_migrations
from project.database.postgres import PostgresClient, PostgresConfig, PostgresUnavailableError, parse_postgres_connection_info, sanitize_postgres_error
from project.position_monitor.service import PositionMonitor
from project.research_engine.repository import ResearchRepository
from project.research_engine.service import ProgressiveResearchEngine
from project.scheduler import PlatformScheduler
from project.strategy_engine.service import StrategyEngine
from project.shared.events import EventBus
from project.shared.logging import get_module_logger


RAILWAY_LIGHT_MISSING_DATABASE_URL = "DATABASE_URL missing: Railway PostgreSQL is required in RAILWAY_LIGHT mode"


def build_postgres(settings: PlatformSettings) -> PostgresClient:
    if settings.run_mode == "RAILWAY_LIGHT" and not settings.database_url:
        raise PostgresUnavailableError(RAILWAY_LIGHT_MISSING_DATABASE_URL)
    postgres = PostgresClient(PostgresConfig(settings.database_url))
    postgres.test_connection()
    return postgres


def log_storage_startup(settings: PlatformSettings, logger) -> None:
    logger.info("RUN_MODE=%s", settings.run_mode)
    logger.info("DATABASE_URL detected: %s", "yes" if settings.database_url else "no")
    logger.info("Storage backend: PostgreSQL" if settings.run_mode == "RAILWAY_LIGHT" else "Storage backend: configured by RUN_MODE")
    if settings.database_url:
        logger.info("PostgreSQL %s", parse_postgres_connection_info(settings.database_url).display())
    logger.info("SQLite cache: %s", "enabled" if settings.sqlite_cache_enabled else "disabled")


def log_postgres_success(postgres: PostgresClient, logger) -> None:
    logger.info("PostgreSQL connection: OK")
    logger.info("PostgreSQL %s", postgres.connection_info.display())


def log_postgres_failure(exc: Exception, settings: PlatformSettings, logger) -> None:
    logger.error("PostgreSQL connection: FAILED")
    logger.error("%s", sanitize_postgres_error(str(exc), settings.database_url))


def build_data_collector(settings: PlatformSettings, event_bus: EventBus, postgres: PostgresClient) -> DataCollectorService:
    kraken = KrakenOhlcClient(
        api_base=settings.kraken_api_base,
        sleep_seconds=settings.kraken_api_sleep_seconds,
        max_retries=settings.kraken_max_retries,
        timeout_seconds=settings.kraken_timeout_seconds,
    )
    repository = MarketDataRepository(postgres, settings.exchange_name)
    return DataCollectorService(kraken, repository, event_bus)


def build_research_engine(settings: PlatformSettings, event_bus: EventBus, postgres: PostgresClient) -> ProgressiveResearchEngine:
    return ProgressiveResearchEngine(
        ResearchRepository(postgres),
        event_bus,
        batch_size=settings.research_batch_size,
        max_runtime_minutes=settings.max_research_runtime_minutes,
        sleep_between_batches_seconds=settings.research_sleep_between_batches_seconds,
        batch_interval_seconds=settings.railway_light_research_seconds if settings.run_mode == "RAILWAY_LIGHT" else settings.research_sleep_between_batches_seconds,
        priority_timeframe=settings.operational_timeframe,
    )


def build_decision_engine(settings: PlatformSettings, event_bus: EventBus, postgres: PostgresClient) -> DecisionEngine:
    return DecisionEngine(postgres, event_bus, timeframe=settings.operational_timeframe)


def build_strategy_engine(settings: PlatformSettings, research_repository: ResearchRepository, decision_engine: DecisionEngine, event_bus: EventBus) -> StrategyEngine:
    return StrategyEngine(
        research_repository,
        decision_engine,
        event_bus,
        operational_timeframe=settings.operational_timeframe,
        trade_notional_eur=settings.trade_notional_eur,
        buy_fee_rate=settings.binance_buy_fee_rate,
        sell_fee_rate=settings.binance_sell_fee_rate,
        spread_rate=settings.binance_spread_rate,
        min_tp1_net_profit_eur=settings.min_tp1_net_profit_eur,
        signal_cooldown_minutes=settings.signal_cooldown_minutes,
    )


def build_notification_engine(settings: PlatformSettings, event_bus: EventBus) -> NotificationEngine:
    notification = NotificationEngine(event_bus, enabled=settings.enable_notification_engine)
    notification.subscribe()
    return notification


def build_position_monitor(event_bus: EventBus, postgres: PostgresClient) -> PositionMonitor:
    return PositionMonitor(postgres, event_bus)


def main() -> None:
    settings = load_settings()
    logger = get_module_logger("system")
    if settings.enable_bootstrap_test:
        logger.info("BOOTSTRAP TEST STARTED")

        try:
            import os
            import psycopg2

            database_url = os.getenv("DATABASE_URL")
            logger.info("BOOTSTRAP DATABASE_URL exists: %s", "yes" if database_url else "no")

            conn = psycopg2.connect(database_url)
            cur = conn.cursor()

            cur.execute("""
                CREATE TABLE IF NOT EXISTS startup_test (
                    id SERIAL PRIMARY KEY,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)

            cur.execute("INSERT INTO startup_test DEFAULT VALUES RETURNING id")
            row_id = cur.fetchone()[0]

            cur.execute("SELECT id, created_at FROM startup_test WHERE id = %s", (row_id,))
            row = cur.fetchone()

            conn.commit()
            cur.close()
            conn.close()

            logger.info("BOOTSTRAP startup_test created")
            logger.info("BOOTSTRAP insert OK id=%s", row_id)
            logger.info("BOOTSTRAP read OK row=%s", row)
            logger.info("BOOTSTRAP TEST SUCCESS")

        except Exception:
            logger.exception("BOOTSTRAP TEST FAILED")
            raise

        logger.info("BOOTSTRAP TEST COMPLETED, continuing normal startup")
    else:
        logger.info("BOOTSTRAP TEST SKIPPED")

    logger.info("======================================")
    logger.info("PROJECT MAIN VERSION: 2026-07-09 BUILD 1")
    logger.info("======================================")
    event_bus = EventBus()
    log_storage_startup(settings, logger)
    logger.info(
        "Starting modular quant platform | collector=%s research=%s decision=%s strategy=%s position_monitor=%s notification=%s",
        settings.enable_data_collector,
        settings.enable_research_engine,
        settings.enable_decision_engine,
        settings.enable_strategy_engine,
        settings.enable_position_monitor,
        settings.enable_notification_engine,
    )
    logger.info("Checkpoint A")
    try:
        postgres = build_postgres(settings)
    except PostgresUnavailableError as exc:
        log_postgres_failure(exc, settings, logger)
        raise SystemExit(1) from exc
    except Exception as exc:
        log_postgres_failure(exc, settings, logger)
        raise SystemExit(1) from exc
    logger.info("Checkpoint B")
    log_postgres_success(postgres, logger)
    if settings.enable_bootstrap_test:
        bootstrap_or_exit(postgres, settings, logger)
    else:
        run_migrations(postgres)
        logger.info("Checkpoint C")
        logger.info("Migration completed")
    logger.info("Database schema ready")
    data_collector = build_data_collector(settings, event_bus, postgres) if settings.enable_data_collector else None
    research_repository = ResearchRepository(postgres)
    research_engine = build_research_engine(settings, event_bus, postgres) if settings.enable_research_engine else None
    decision_engine = build_decision_engine(settings, event_bus, postgres) if settings.enable_decision_engine else None
    strategy_engine = build_strategy_engine(settings, research_repository, decision_engine, event_bus) if settings.enable_strategy_engine and decision_engine is not None else None
    position_monitor = build_position_monitor(event_bus, postgres) if settings.enable_position_monitor else None
    if settings.enable_notification_engine:
        build_notification_engine(settings, event_bus)
    scheduler = PlatformScheduler(settings, data_collector, research_engine, decision_engine, strategy_engine, position_monitor)
    logger.info("Checkpoint D")
    scheduler.run_forever()


if __name__ == "__main__":
    main()
