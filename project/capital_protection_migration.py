"""Database objects used by the V6 capital-protection admission gate."""
from __future__ import annotations

from project.database.postgres import PostgresClient


CAPITAL_PROTECTION_SCHEMA = """
CREATE TABLE IF NOT EXISTS signals.shadow_signals (
    id BIGSERIAL PRIMARY KEY,
    runtime_version TEXT NOT NULL,
    strategy TEXT NOT NULL,
    pair TEXT NOT NULL,
    timeframe TEXT NOT NULL DEFAULT '15m',
    regime TEXT NOT NULL,
    exchange TEXT NOT NULL DEFAULT 'kraken',
    entry NUMERIC NOT NULL,
    stop_loss NUMERIC NOT NULL,
    take_profit NUMERIC NOT NULL,
    net_profit_tp_eur NUMERIC NOT NULL,
    net_loss_sl_eur NUMERIC NOT NULL,
    net_rr NUMERIC NOT NULL,
    score NUMERIC NOT NULL,
    score_breakdown JSONB NOT NULL DEFAULT '{}'::jsonb,
    reasons JSONB NOT NULL DEFAULT '[]'::jsonb,
    signal_time TIMESTAMPTZ NOT NULL,
    reference_candle_time TIMESTAMPTZ,
    status TEXT NOT NULL DEFAULT 'NEW',
    outcome_price NUMERIC,
    outcome_open_price NUMERIC,
    outcome_high_price NUMERIC,
    outcome_low_price NUMERIC,
    outcome_close_price NUMERIC,
    outcome_candle_time TIMESTAMPTZ,
    outcome_ambiguous BOOLEAN NOT NULL DEFAULT FALSE,
    outcome_resolution TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    closed_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_shadow_signals_open
    ON signals.shadow_signals(runtime_version, status, created_at ASC);

CREATE INDEX IF NOT EXISTS idx_shadow_signals_admission
    ON signals.shadow_signals(runtime_version, strategy, regime, status, closed_at DESC);

CREATE INDEX IF NOT EXISTS idx_shadow_signals_pair
    ON signals.shadow_signals(runtime_version, strategy, pair, regime, status, closed_at DESC);
"""


def run_capital_protection_migration(client: PostgresClient) -> None:
    client.execute(CAPITAL_PROTECTION_SCHEMA)
