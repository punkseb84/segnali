from __future__ import annotations

import time

from project.data_collector.binance_history import (
    BinanceHistoricalKlineClient,
    binance_symbol,
)
from project.research_engine.sourced_repository import (
    BINANCE_RESEARCH_EXCHANGE,
    ROBUST_RESEARCH_VERSION_V2,
    SourcedRobustResearchRepository,
)


def _kline(open_time: int) -> list[object]:
    return [
        open_time,
        "100.0",
        "101.0",
        "99.0",
        "100.5",
        "10.0",
        open_time + 899_999,
        "0",
        1,
        "0",
        "0",
        "0",
    ]


def test_binance_symbol_normalizes_usd_pairs() -> None:
    assert binance_symbol("BTC/USD") == "BTCUSDT"
    assert binance_symbol("DOGE/USD") == "DOGEUSDT"
    assert binance_symbol("XBT/USD") == "BTCUSDT"


def test_history_client_paginates_and_returns_closed_candles(monkeypatch) -> None:
    now_ms = int(time.time() * 1000)
    interval = 900_000
    latest_closed = now_ms - interval * 2
    first_chunk = [
        _kline(latest_closed - interval * index)
        for index in range(999, -1, -1)
    ]
    second_chunk = [_kline(latest_closed - interval * 1000)]
    calls: list[dict[str, object]] = []

    def fake_request(self, params):
        calls.append(dict(params))
        return first_chunk if len(calls) == 1 else second_chunk

    monkeypatch.setattr(BinanceHistoricalKlineClient, "_request", fake_request)
    monkeypatch.setattr("project.data_collector.binance_history.time.sleep", lambda _: None)
    client = BinanceHistoricalKlineClient()

    frame = client.fetch_recent_closed_klines("BTC/USD", "15m", limit=1001)

    assert len(frame) == 1001
    assert len(calls) == 2
    assert "endTime" not in calls[0]
    assert int(calls[1]["endTime"]) < int(first_chunk[0][0])
    assert frame["timestamp"].is_monotonic_increasing


class FakePostgres:
    def __init__(self) -> None:
        self.params = None

    def fetch_all(self, query, params=()):
        self.params = params
        return []


def test_research_repository_reads_only_selected_exchange() -> None:
    postgres = FakePostgres()
    repository = SourcedRobustResearchRepository(
        postgres,
        market_exchange=BINANCE_RESEARCH_EXCHANGE,
    )

    repository.fetch_ohlc("SOL/USD", "15m", limit=3000)

    assert postgres.params == (BINANCE_RESEARCH_EXCHANGE, "SOL/USD", "15m", 3000)
    assert repository.research_version == ROBUST_RESEARCH_VERSION_V2
