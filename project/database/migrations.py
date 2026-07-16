"""Schema migration runner for Railway PostgreSQL."""
from __future__ import annotations

from project.database.postgres import PostgresClient
from project.database.schema import ALL_SCHEMAS


WATCHLIST_ISOLATION_MIGRATION = """
UPDATE signals.generated_signals
SET status = 'WATCHLIST'
WHERE signal_class = 'C'
  AND status IN ('NEW', 'OPEN');

CREATE INDEX IF NOT EXISTS idx_generated_signals_operational_active
    ON signals.generated_signals(signal_class, status, created_at DESC);
"""

TELEGRAM_MESSAGE_REFERENCE_MIGRATION = """
ALTER TABLE signals.generated_signals
    ADD COLUMN IF NOT EXISTS telegram_message_id BIGINT,
    ADD COLUMN IF NOT EXISTS telegram_chat_id TEXT,
    ADD COLUMN IF NOT EXISTS telegram_message_link TEXT,
    ADD COLUMN IF NOT EXISTS telegram_sent_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_generated_signals_telegram_message
    ON signals.generated_signals(telegram_message_id)
    WHERE telegram_message_id IS NOT NULL;
"""

SIGNAL_LIFECYCLE_INTEGRITY_MIGRATION = """
ALTER TABLE signals.generated_signals
    ADD COLUMN IF NOT EXISTS outcome_price NUMERIC,
    ADD COLUMN IF NOT EXISTS outcome_close_price NUMERIC,
    ADD COLUMN IF NOT EXISTS outcome_candle_time TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS closed_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS outcome_ambiguous BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS outcome_resolution TEXT;

CREATE INDEX IF NOT EXISTS idx_generated_signals_pair_lifecycle
    ON signals.generated_signals(pair, timeframe, signal_class, status, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_generated_signals_reference_candle
    ON signals.generated_signals(pair, timeframe, reference_candle_time, signal_class);
"""


def run_migrations(client: PostgresClient) -> None:
    for statement in ALL_SCHEMAS:
        client.execute(statement)
    client.execute(WATCHLIST_ISOLATION_MIGRATION)
    client.execute(TELEGRAM_MESSAGE_REFERENCE_MIGRATION)
    client.execute(SIGNAL_LIFECYCLE_INTEGRITY_MIGRATION)
