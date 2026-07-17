"""Research repository with an explicit market-data exchange source."""
from __future__ import annotations

from typing import Any

from project.research_engine.robust_repository import RobustResearchRepository

ROBUST_RESEARCH_VERSION_V2 = "ROBUST_WALK_FORWARD_V2"
BINANCE_RESEARCH_EXCHANGE = "BINANCE_RESEARCH"


class SourcedRobustResearchRepository(RobustResearchRepository):
    """Keep historical research candles isolated from live Kraken candles."""

    def __init__(
        self,
        client,
        market_exchange: str,
        research_version: str = ROBUST_RESEARCH_VERSION_V2,
        **kwargs: Any,
    ) -> None:
        super().__init__(client, research_version=research_version, **kwargs)
        self.market_exchange = str(market_exchange)

    def fetch_ohlc(
        self,
        pair: str,
        timeframe: str,
        limit: int = 720,
    ) -> list[tuple[Any, ...]]:
        return self.client.fetch_all(
            """SELECT timestamp, open, high, low, close, volume
            FROM market_data.ohlc
            WHERE exchange = %s AND pair = %s AND timeframe = %s
            ORDER BY timestamp DESC
            LIMIT %s""",
            (self.market_exchange, pair, timeframe, limit),
        )
