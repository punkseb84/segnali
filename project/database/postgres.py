"""PostgreSQL access layer for the modular quant platform."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator, Sequence


class PostgresUnavailableError(RuntimeError):
    """Raised when the PostgreSQL driver or DATABASE_URL is unavailable."""


@dataclass(frozen=True)
class PostgresConfig:
    database_url: str


class PostgresClient:
    """Thin psycopg wrapper with lazy imports so tests do not require PostgreSQL."""

    def __init__(self, config: PostgresConfig) -> None:
        self.config = config

    @contextmanager
    def connect(self) -> Iterator[Any]:
        if not self.config.database_url:
            raise PostgresUnavailableError("DATABASE_URL is required for PostgreSQL-backed modules")
        try:
            import psycopg  # type: ignore[import-not-found]
        except ImportError as exc:
            raise PostgresUnavailableError("Install psycopg[binary] to use PostgreSQL-backed modules") from exc
        with psycopg.connect(self.config.database_url) as conn:
            yield conn

    def execute(self, sql: str, params: Sequence[Any] | None = None) -> None:
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params or ())
            conn.commit()

    def executemany(self, sql: str, rows: Sequence[Sequence[Any]]) -> None:
        if not rows:
            return
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.executemany(sql, rows)
            conn.commit()

    def fetch_all(self, sql: str, params: Sequence[Any] | None = None) -> list[tuple[Any, ...]]:
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params or ())
                return list(cur.fetchall())
