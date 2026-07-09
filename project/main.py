"""Entrypoint for the modular quant platform.

Phase 1 wires the architecture and Data Collector. Later phases plug into the
same event bus and scheduler without direct module-to-module calls.
"""
from __future__ import annotations

from project.config.settings import load_settings
from project.data_collector.kraken_client import KrakenOhlcClient
from project.data_collector.repository import MarketDataRepository
from project.data_collector.service import DataCollectorService
from project.database.migrations import run_migrations
from project.database.postgres import PostgresClient, PostgresConfig
from project.scheduler import PlatformScheduler
from project.shared.events import EventBus
from project.shared.logging import get_module_logger


def build_data_collector(settings, event_bus: EventBus) -> DataCollectorService:
    postgres = PostgresClient(PostgresConfig(settings.database_url))
    run_migrations(postgres)
    kraken = KrakenOhlcClient(
        api_base=settings.kraken_api_base,
        sleep_seconds=settings.kraken_api_sleep_seconds,
        max_retries=settings.kraken_max_retries,
        timeout_seconds=settings.kraken_timeout_seconds,
    )
    repository = MarketDataRepository(postgres, settings.exchange_name)
    return DataCollectorService(kraken, repository, event_bus)


def main() -> None:
    settings = load_settings()
    logger = get_module_logger("system")
    event_bus = EventBus()
    logger.info("Starting modular quant platform | collector=%s research=%s decision=%s strategy=%s notification=%s", settings.enable_data_collector, settings.enable_research_engine, settings.enable_decision_engine, settings.enable_strategy_engine, settings.enable_notification_engine)
    data_collector = build_data_collector(settings, event_bus) if settings.enable_data_collector else None
    scheduler = PlatformScheduler(settings, data_collector)
    scheduler.run_forever()


if __name__ == "__main__":
    main()
