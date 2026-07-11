from datetime import datetime, timezone

import pandas as pd

from project.data_collector.service import DataCollectorService
from project.shared.events import EventBus, EventType


class FakeClient:
    def __init__(self):
        self.since_seen = None

    def fetch_ohlc(self, pair, timeframe, since=None):
        self.since_seen = since
        return pd.DataFrame(
            [
                {
                    "timestamp": pd.Timestamp("2026-01-01T00:00:00Z"),
                    "open": 1.0,
                    "high": 2.0,
                    "low": 0.5,
                    "close": 1.5,
                    "volume": 10.0,
                }
            ]
        )


class FakeRepository:
    def __init__(self):
        self.latest = datetime(2025, 12, 31, tzinfo=timezone.utc)
        self.logs = []
        self.inserted = []

    def upsert_pair(self, pair):
        self.pair = pair

    def upsert_timeframe(self, timeframe):
        self.timeframe = timeframe

    def latest_timestamp(self, pair, timeframe):
        return self.latest

    def insert_ohlc(self, pair, timeframe, frame):
        self.inserted.append((pair, timeframe, len(frame)))
        return len(frame)

    def write_sync_log(self, pair, timeframe, started_at, status, rows_inserted, error=None):
        self.logs.append((pair, timeframe, status, rows_inserted, error))

    def verify_integrity(self, pair, timeframe):
        raise AssertionError("not used")


def test_sync_pair_downloads_incrementally_persists_and_publishes_event():
    client = FakeClient()
    repo = FakeRepository()
    bus = EventBus()
    events = []
    bus.subscribe(EventType.MARKET_UPDATED, events.append)
    service = DataCollectorService(client, repo, bus)

    result = service.sync_pair("BTC/USD", "5m")

    assert client.since_seen == repo.latest
    assert result.status == "SUCCESS"
    assert result.rows_downloaded == 1
    assert result.rows_inserted == 1
    assert repo.inserted == [("BTC/USD", "5m", 1)]
    assert repo.logs[-1][2:] == ("SUCCESS", 1, None)
    assert events[-1].payload == {"pair": "BTC/USD", "timeframe": "5m", "rows_inserted": 1}
