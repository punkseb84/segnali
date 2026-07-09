"""Entrypoint for the modular quant platform on Railway.

RAILWAY_LIGHT keeps the process alive, collects data, and processes small
research batches without requiring a dedicated server.
"""
from __future__ import annotations

from project.config.settings import PlatformSettings, load_settings
from project.data_collector.kraken_client import KrakenOhlcClient
from project.data_collector.repository import MarketDataRepository
from project.data_collector.service import DataCollectorService
from project.database.migrations import run_migrations
from project.database.postgres import PostgresClient, PostgresConfig
from project.research_engine.repository import ResearchRepository
from project.research_engine.service import ProgressiveResearchEngine
from project.scheduler import PlatformScheduler
from project.shared.events import EventBus
from project.shared.logging import get_module_logger


def build_postgres(settings: PlatformSettings) -> PostgresClient:
    postgres = PostgresClient(PostgresConfig(settings.database_url))
    run_migrations(postgres)
    return postgres


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
    )


def main() -> None:
    settings = load_settings()
    logger = get_module_logger("system")
    event_bus = EventBus()
    logger.info(
        "Starting modular quant platform | run_mode=%s collector=%s research=%s decision=%s strategy=%s notification=%s",
        settings.run_mode,
        settings.enable_data_collector,
        settings.enable_research_engine,
        settings.enable_decision_engine,
        settings.enable_strategy_engine,
        settings.enable_notification_engine,
    )
    postgres = build_postgres(settings)
    data_collector = build_data_collector(settings, event_bus, postgres) if settings.enable_data_collector else None
    research_engine = build_research_engine(settings, event_bus, postgres) if settings.enable_research_engine else None
    scheduler = PlatformScheduler(settings, data_collector, research_engine)
    scheduler.run_forever()


if __name__ == "__main__":
    main()
