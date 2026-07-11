"""Schema migration runner for Railway PostgreSQL."""
from __future__ import annotations

from project.database.postgres import PostgresClient
from project.database.schema import ALL_SCHEMAS


def run_migrations(client: PostgresClient) -> None:
    for statement in ALL_SCHEMAS:
        client.execute(statement)
