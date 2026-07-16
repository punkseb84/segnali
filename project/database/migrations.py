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


def run_migrations(client: PostgresClient) -> None:
    for statement in ALL_SCHEMAS:
        client.execute(statement)
    client.execute(WATCHLIST_ISOLATION_MIGRATION)
    client.execute(TELEGRAM_MESSAGE_REFERENCE_MIGRATION)
