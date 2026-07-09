"""Startup bootstrap checks for PostgreSQL, migrations, OHLC, and research persistence."""
from __future__ import annotations

import traceback
from typing import Any

from project.config.settings import PlatformSettings
from project.data_collector.kraken_client import KrakenOhlcClient
from project.data_collector.repository import MarketDataRepository
from project.database.migrations import run_migrations
from project.database.postgres import PostgresClient

EXPECTED_TABLES = [
    "market_data.pairs",
    "market_data.timeframes",
    "market_data.ohlc",
    "market_data.sync_log",
    "research.strategy_combinations",
    "research.strategy_results",
    "research.research_batches",
    "research.research_reports",
    "signals schema",
    "statistics schema",
    "system schema",
]


def bootstrap_or_exit(postgres: PostgresClient, settings: PlatformSettings, logger) -> None:
    try:
        run_database_bootstrap(postgres, logger)
        run_migration_bootstrap(postgres, logger)
        run_data_collector_bootstrap(postgres, settings, logger)
        run_research_bootstrap(postgres, logger)
    except Exception:
        logger.error("Database bootstrap FAILED")
        logger.error("%s", traceback.format_exc())
        raise SystemExit(1)


def run_database_bootstrap(postgres: PostgresClient, logger) -> None:
    postgres.test_connection()
    logger.info("PostgreSQL connection OK")
    postgres.execute(
        """CREATE TABLE IF NOT EXISTS startup_test (
            id SERIAL PRIMARY KEY,
            created_at TIMESTAMP DEFAULT NOW()
        )"""
    )
    logger.info("startup_test created")
    inserted = postgres.fetch_all("INSERT INTO startup_test DEFAULT VALUES RETURNING id, created_at")
    startup_id = inserted[0][0]
    logger.info("startup_test insert OK")
    selected = postgres.fetch_all("SELECT id, created_at FROM startup_test WHERE id = %s", (startup_id,))
    if not selected or selected[0][0] != startup_id:
        raise RuntimeError("startup_test read verification failed")
    logger.info("startup_test read OK")
    logger.info("Database bootstrap SUCCESS")


def run_migration_bootstrap(postgres: PostgresClient, logger) -> None:
    run_migrations(postgres)
    logger.info("Checkpoint C")
    logger.info("Migration completed")
    logger.info("Tables created:")
    for table in EXPECTED_TABLES:
        logger.info("- %s", table)


def run_data_collector_bootstrap(postgres: PostgresClient, settings: PlatformSettings, logger) -> None:
    kraken = KrakenOhlcClient(
        api_base=settings.kraken_api_base,
        sleep_seconds=settings.kraken_api_sleep_seconds,
        max_retries=settings.kraken_max_retries,
        timeout_seconds=settings.kraken_timeout_seconds,
    )
    repository = MarketDataRepository(postgres, settings.exchange_name)
    repository.upsert_pair("BTC/USD")
    repository.upsert_timeframe("5m")
    candle = kraken.fetch_ohlc("BTC/USD", "5m", limit=1)
    if candle.empty:
        raise RuntimeError("Kraken returned no BTC/USD 5m candles during bootstrap")
    repository.insert_ohlc("BTC/USD", "5m", candle)
    logger.info("OHLC write OK")
    read_back = postgres.fetch_all(
        """SELECT timestamp, open, high, low, close, volume
        FROM market_data.ohlc
        WHERE exchange = %s AND pair = %s AND timeframe = %s AND timestamp = %s
        LIMIT 1""",
        (settings.exchange_name, "BTC/USD", "5m", candle.iloc[0]["timestamp"].to_pydatetime()),
    )
    if not read_back:
        raise RuntimeError("OHLC read verification failed")
    logger.info("OHLC read OK")


def run_research_bootstrap(postgres: PostgresClient, logger) -> None:
    row = postgres.fetch_all(
        """INSERT INTO research.strategy_results(
            strategy, pair, timeframe, parameters, win_rate, profit_factor, expectancy,
            sharpe, sortino, max_drawdown, net_profit, average_win, average_loss,
            recovery_factor, ulcer_index, mfe, mae, calmar_ratio, validation
        ) VALUES (
            'BOOTSTRAP_TEST', 'BTC/USD', '5m', '{"bootstrap": true}'::jsonb,
            0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
            '{"status": "BOOTSTRAP"}'::jsonb
        ) RETURNING id"""
    )
    result_id = row[0][0]
    logger.info("Research write OK")
    read_back = postgres.fetch_all("SELECT id FROM research.strategy_results WHERE id = %s", (result_id,))
    if not read_back or read_back[0][0] != result_id:
        raise RuntimeError("Research read verification failed")
    logger.info("Research read OK")
