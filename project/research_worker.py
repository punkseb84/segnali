"""One-shot robust research worker used by the low-cost Railway scheduler."""
from __future__ import annotations

import os

from project.config.settings import load_settings
from project.database.migrations import run_migrations
from project.database.postgres import PostgresClient, PostgresConfig
from project.research_engine.robust_memory import MemoryEfficientRobustResearchEngine
from project.research_engine.robust_repository import RobustResearchRepository
from project.shared.events import EventBus
from project.shared.logging import get_module_logger
from project.shared.memory import release_unused_memory


def main() -> None:
    os.environ.setdefault("ROBUST_MIN_CANDLES", "1800")
    os.environ.setdefault("ROBUST_MIN_TOTAL_TRADES", "30")
    os.environ.setdefault("ROBUST_MIN_TRAIN_TRADES", "18")
    os.environ.setdefault("ROBUST_MIN_TEST_TRADES", "10")
    os.environ.setdefault("ROBUST_MIN_TRAIN_PROFIT_FACTOR", "1.05")
    os.environ.setdefault("ROBUST_MIN_TEST_PROFIT_FACTOR", "1.15")
    os.environ.setdefault("ROBUST_MIN_TEST_EXPECTANCY_EUR", "0.02")
    os.environ.setdefault("ROBUST_RESEARCH_OHLC_LIMIT", "3000")

    settings = load_settings()
    logger = get_module_logger("research-worker")
    logger.info(
        "LOW_COST_RESEARCH_WORKER start batch_size=%s runtime_minutes=%s ohlc_limit=%s",
        settings.research_batch_size,
        settings.max_research_runtime_minutes,
        os.environ["ROBUST_RESEARCH_OHLC_LIMIT"],
    )
    postgres = PostgresClient(PostgresConfig(settings.database_url))
    postgres.test_connection()
    run_migrations(postgres)

    repository = RobustResearchRepository(postgres)
    engine = MemoryEfficientRobustResearchEngine(
        repository,
        EventBus(),
        batch_size=settings.research_batch_size,
        max_runtime_minutes=settings.max_research_runtime_minutes,
        sleep_between_batches_seconds=0,
        batch_interval_seconds=max(21600, settings.railway_light_research_seconds),
        priority_timeframe=settings.operational_timeframe,
        trade_notional_eur=settings.trade_notional_eur,
        buy_fee_rate=settings.binance_buy_fee_rate,
        sell_fee_rate=settings.binance_sell_fee_rate,
        spread_rate=settings.binance_spread_rate,
        slippage_rate=settings.binance_slippage_rate,
        probability_horizon_candles=settings.probability_horizon_candles,
    )
    engine.seed_combinations(
        settings.collector_pairs,
        [settings.operational_timeframe],
    )
    result = engine.process_one_batch(sleep_after=False)
    rss_mb = release_unused_memory()
    logger.info(
        "LOW_COST_RESEARCH_WORKER completed status=%s processed=%s rss_mb=%s",
        result.status,
        result.processed,
        f"{rss_mb:.1f}" if rss_mb is not None else "unknown",
    )


if __name__ == "__main__":
    main()
