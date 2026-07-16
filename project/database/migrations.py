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


def run_migrations(client: PostgresClient) -> None:
    for statement in ALL_SCHEMAS:
        client.execute(statement)
    client.execute(WATCHLIST_ISOLATION_MIGRATION)
