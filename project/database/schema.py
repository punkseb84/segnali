"""PostgreSQL schema definitions grouped by platform domain."""
from __future__ import annotations

MARKET_DATA_SCHEMA = """
CREATE SCHEMA IF NOT EXISTS market_data;

CREATE TABLE IF NOT EXISTS market_data.pairs (
    id BIGSERIAL PRIMARY KEY,
    exchange TEXT NOT NULL,
    symbol TEXT NOT NULL,
    base_asset TEXT,
    quote_asset TEXT,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(exchange, symbol)
);

CREATE TABLE IF NOT EXISTS market_data.timeframes (
    id BIGSERIAL PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    minutes INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS market_data.ohlc (
    id BIGSERIAL PRIMARY KEY,
    exchange TEXT NOT NULL,
    pair TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    timestamp TIMESTAMPTZ NOT NULL,
    open NUMERIC NOT NULL,
    high NUMERIC NOT NULL,
    low NUMERIC NOT NULL,
    close NUMERIC NOT NULL,
    volume NUMERIC NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(exchange, pair, timeframe, timestamp)
);

CREATE INDEX IF NOT EXISTS idx_ohlc_lookup
    ON market_data.ohlc(exchange, pair, timeframe, timestamp DESC);

CREATE TABLE IF NOT EXISTS market_data.sync_log (
    id BIGSERIAL PRIMARY KEY,
    exchange TEXT NOT NULL,
    pair TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    started_at TIMESTAMPTZ NOT NULL,
    finished_at TIMESTAMPTZ,
    status TEXT NOT NULL,
    rows_inserted INTEGER NOT NULL DEFAULT 0,
    error_message TEXT
);
"""

RESEARCH_SCHEMA = """
CREATE SCHEMA IF NOT EXISTS research;
CREATE TABLE IF NOT EXISTS research.strategy_results (
    id BIGSERIAL PRIMARY KEY,
    strategy TEXT NOT NULL,
    pair TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    parameters JSONB NOT NULL,
    win_rate NUMERIC,
    profit_factor NUMERIC,
    expectancy NUMERIC,
    sharpe NUMERIC,
    sortino NUMERIC,
    max_drawdown NUMERIC,
    net_profit NUMERIC,
    average_win NUMERIC,
    average_loss NUMERIC,
    recovery_factor NUMERIC,
    ulcer_index NUMERIC,
    mfe NUMERIC,
    mae NUMERIC,
    calmar_ratio NUMERIC,
    validation JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS research.research_batches (
    id BIGSERIAL PRIMARY KEY,
    status TEXT NOT NULL,
    total_combinations INTEGER NOT NULL DEFAULT 0,
    processed_combinations INTEGER NOT NULL DEFAULT 0,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS research.research_reports (
    id BIGSERIAL PRIMARY KEY,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""

SIGNALS_SCHEMA = """
CREATE SCHEMA IF NOT EXISTS signals;
"""
STATISTICS_SCHEMA = """
CREATE SCHEMA IF NOT EXISTS statistics;
"""
SYSTEM_SCHEMA = """
CREATE SCHEMA IF NOT EXISTS system;
"""

ALL_SCHEMAS = [MARKET_DATA_SCHEMA, RESEARCH_SCHEMA, SIGNALS_SCHEMA, STATISTICS_SCHEMA, SYSTEM_SCHEMA]
