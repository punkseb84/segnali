"""Data Collector module: downloads market data and stores it in PostgreSQL only."""

from project.data_collector.kraken_client import KrakenOhlcClient
from project.data_collector.repository import MarketDataRepository, OhlcIntegrityReport
from project.data_collector.service import DataCollectorService, SyncResult

__all__ = ["DataCollectorService", "KrakenOhlcClient", "MarketDataRepository", "OhlcIntegrityReport", "SyncResult"]
