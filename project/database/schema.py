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
CREATE TABLE IF NOT EXISTS research.strategy_combinations (
    id BIGSERIAL PRIMARY KEY,
    strategy TEXT NOT NULL,
    pair TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    parameters JSONB NOT NULL,
    combination_key TEXT GENERATED ALWAYS AS (md5(strategy || '|' || pair || '|' || timeframe || '|' || parameters::text)) STORED,
    status TEXT NOT NULL DEFAULT 'PENDING',
    error_message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(combination_key)
);
CREATE INDEX IF NOT EXISTS idx_strategy_combinations_status
    ON research.strategy_combinations(status, id);

CREATE TABLE IF NOT EXISTS research.strategy_results (
    id BIGSERIAL PRIMARY KEY,
    combination_id BIGINT REFERENCES research.strategy_combinations(id),
    batch_id BIGINT,
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
    batch_size INTEGER NOT NULL DEFAULT 100,
    total_combinations INTEGER NOT NULL DEFAULT 0,
    processed_combinations INTEGER NOT NULL DEFAULT 0,
    last_combination_id BIGINT,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at TIMESTAMPTZ
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
CREATE TABLE IF NOT EXISTS signals.generated_signals (
    id BIGSERIAL PRIMARY KEY,
    strategy TEXT NOT NULL,
    pair TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    regime TEXT NOT NULL,
    entry NUMERIC NOT NULL,
    stop_loss NUMERIC NOT NULL,
    take_profit NUMERIC NOT NULL,
    signal_time TIMESTAMPTZ,
    reference_candle_time TIMESTAMPTZ,
    entry_timing TEXT NOT NULL DEFAULT 'IMMEDIATE_ON_SIGNAL_RECEIPT',
    quantity NUMERIC,
    entry_notional_eur NUMERIC,
    tp_notional_eur NUMERIC,
    sl_notional_eur NUMERIC,
    gross_profit_tp1_eur NUMERIC,
    gross_loss_sl_eur NUMERIC,
    estimated_buy_fee_eur NUMERIC,
    estimated_sell_fee_eur NUMERIC,
    estimated_sell_fee_sl_eur NUMERIC,
    estimated_spread_cost_eur NUMERIC,
    estimated_slippage_cost_eur NUMERIC,
    net_profit_tp1_eur NUMERIC,
    net_loss_sl_eur NUMERIC,
    gross_rr NUMERIC,
    net_rr NUMERIC,
    signal_class TEXT NOT NULL DEFAULT 'B',
    historical_expected_value_eur NUMERIC,
    score NUMERIC NOT NULL,
    probability NUMERIC,
    reasons JSONB NOT NULL,
    status TEXT NOT NULL DEFAULT 'NEW',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
ALTER TABLE signals.generated_signals
    ADD COLUMN IF NOT EXISTS signal_time TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS reference_candle_time TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS entry_timing TEXT NOT NULL DEFAULT 'IMMEDIATE_ON_SIGNAL_RECEIPT',
    ADD COLUMN IF NOT EXISTS quantity NUMERIC,
    ADD COLUMN IF NOT EXISTS entry_notional_eur NUMERIC,
    ADD COLUMN IF NOT EXISTS tp_notional_eur NUMERIC,
    ADD COLUMN IF NOT EXISTS sl_notional_eur NUMERIC,
    ADD COLUMN IF NOT EXISTS gross_profit_tp1_eur NUMERIC,
    ADD COLUMN IF NOT EXISTS gross_loss_sl_eur NUMERIC,
    ADD COLUMN IF NOT EXISTS estimated_buy_fee_eur NUMERIC,
    ADD COLUMN IF NOT EXISTS estimated_sell_fee_eur NUMERIC,
    ADD COLUMN IF NOT EXISTS estimated_sell_fee_sl_eur NUMERIC,
    ADD COLUMN IF NOT EXISTS estimated_spread_cost_eur NUMERIC,
    ADD COLUMN IF NOT EXISTS estimated_slippage_cost_eur NUMERIC,
    ADD COLUMN IF NOT EXISTS net_profit_tp1_eur NUMERIC,
    ADD COLUMN IF NOT EXISTS net_loss_sl_eur NUMERIC,
    ADD COLUMN IF NOT EXISTS gross_rr NUMERIC,
    ADD COLUMN IF NOT EXISTS net_rr NUMERIC,
    ADD COLUMN IF NOT EXISTS signal_class TEXT NOT NULL DEFAULT 'B',
    ADD COLUMN IF NOT EXISTS historical_expected_value_eur NUMERIC;
ALTER TABLE signals.generated_signals
    ALTER COLUMN probability DROP NOT NULL;
CREATE INDEX IF NOT EXISTS idx_generated_signals_active
    ON signals.generated_signals(strategy, pair, timeframe, regime, status, created_at DESC);
"""
STATISTICS_SCHEMA = """
CREATE SCHEMA IF NOT EXISTS statistics;
"""
SYSTEM_SCHEMA = """
CREATE SCHEMA IF NOT EXISTS system;
"""

ALL_SCHEMAS = [MARKET_DATA_SCHEMA, RESEARCH_SCHEMA, SIGNALS_SCHEMA, STATISTICS_SCHEMA, SYSTEM_SCHEMA]
