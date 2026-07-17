"""Railway runtime using separated Kraken live and Binance research history."""
from __future__ import annotations

import os
from functools import partial

import project.low_cost_main as low_cost_main
from project.research_engine.sourced_repository import (
    ROBUST_RESEARCH_VERSION_V2,
    SourcedRobustResearchRepository,
)


def main() -> None:
    os.environ.setdefault("LOW_COST_RESEARCH_STARTUP_BATCH_SIZE", "700")
    os.environ.setdefault("LOW_COST_RESEARCH_STARTUP_RUNTIME_MINUTES", "20")
    os.environ.setdefault("LOW_COST_RESEARCH_BATCH_SIZE", "100")
    os.environ.setdefault("LOW_COST_RESEARCH_INTERVAL_HOURS", "6")
    os.environ.setdefault("ROBUST_RESEARCH_OHLC_LIMIT", "3000")
    os.environ.setdefault("ROBUST_MIN_CANDLES", "1800")

    live_exchange = os.getenv("EXCHANGE_NAME", "KRAKEN")
    low_cost_main.RobustResearchRepository = partial(
        SourcedRobustResearchRepository,
        market_exchange=live_exchange,
        research_version=ROBUST_RESEARCH_VERSION_V2,
    )
    low_cost_main.main()


if __name__ == "__main__":
    main()
