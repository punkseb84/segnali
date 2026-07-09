"""Phase 1 Data Collector service.

This module only downloads and stores market data. It does not know about
strategies, indicators, Telegram, research, or live signals.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from project.data_collector.kraken_client import KrakenOhlcClient
from project.data_collector.repository import MarketDataRepository, OhlcIntegrityReport
from project.shared.events import Event, EventBus, EventType
from project.shared.logging import get_module_logger


@dataclass(frozen=True)
class SyncResult:
    pair: str
    timeframe: str
    rows_downloaded: int
    rows_inserted: int
    status: str
    error: str | None = None


class DataCollectorService:
    """Owns data download, incremental updates, integrity checks, and sync logs."""

    def __init__(self, client: KrakenOhlcClient, repository: MarketDataRepository, event_bus: EventBus | None = None) -> None:
        self.client = client
        self.repository = repository
        self.event_bus = event_bus or EventBus()
        self.logger = get_module_logger("collector")

    def sync_pair(self, pair: str, timeframe: str) -> SyncResult:
        started_at = datetime.now(timezone.utc)
        try:
            self.repository.upsert_pair(pair)
            self.repository.upsert_timeframe(timeframe)
            latest = self.repository.latest_timestamp(pair, timeframe)
            frame = self.client.fetch_ohlc(pair, timeframe, since=latest)
            rows_inserted = self.repository.insert_ohlc(pair, timeframe, frame)
            self.repository.write_sync_log(pair, timeframe, started_at, "SUCCESS", rows_inserted)
            result = SyncResult(pair, timeframe, len(frame), rows_inserted, "SUCCESS")
            self.logger.info("Synced pair=%s timeframe=%s downloaded=%s inserted=%s", pair, timeframe, len(frame), rows_inserted)
            self.event_bus.publish(Event(EventType.MARKET_UPDATED, {"pair": pair, "timeframe": timeframe, "rows_inserted": rows_inserted}))
            return result
        except Exception as exc:
            try:
                self.repository.write_sync_log(pair, timeframe, started_at, "FAILED", 0, str(exc))
            except Exception as log_exc:
                self.logger.error("Could not write sync failure log pair=%s timeframe=%s: %s", pair, timeframe, log_exc)
            self.logger.exception("Sync failed pair=%s timeframe=%s: %s", pair, timeframe, exc)
            return SyncResult(pair, timeframe, 0, 0, "FAILED", str(exc))

    def sync_all_pairs(self, pairs: list[str], timeframes: list[str]) -> list[SyncResult]:
        results: list[SyncResult] = []
        for pair in pairs:
            for timeframe in timeframes:
                results.append(self.sync_pair(pair, timeframe))
        return results

    def update_missing_data(self, pair: str, timeframe: str) -> SyncResult:
        return self.download_incremental(pair, timeframe)

    def download_incremental(self, pair: str, timeframe: str) -> SyncResult:
        return self.sync_pair(pair, timeframe)

    def verify_integrity(self, pair: str, timeframe: str) -> OhlcIntegrityReport:
        report = self.repository.verify_integrity(pair, timeframe)
        self.logger.info(
            "Integrity pair=%s timeframe=%s rows=%s duplicates=%s missing=%s latest=%s",
            pair,
            timeframe,
            report.rows,
            report.duplicate_rows,
            report.missing_required_values,
            report.latest_timestamp,
        )
        return report
