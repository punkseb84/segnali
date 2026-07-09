"""PostgreSQL access layer for the modular quant platform."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator, Sequence
from urllib.parse import urlparse, urlunparse


class PostgresUnavailableError(RuntimeError):
    """Raised when the PostgreSQL driver or DATABASE_URL is unavailable."""


@dataclass(frozen=True)
class PostgresConfig:
    database_url: str


@dataclass(frozen=True)
class PostgresConnectionInfo:
    host: str
    database: str
    port: int | None = None

    def display(self) -> str:
        port = f":{self.port}" if self.port else ""
        return f"host={self.host}{port} database={self.database}"


def parse_postgres_connection_info(database_url: str) -> PostgresConnectionInfo:
    parsed = urlparse(database_url)
    database = parsed.path.lstrip("/") or "unknown"
    return PostgresConnectionInfo(host=parsed.hostname or "unknown", port=parsed.port, database=database)


def sanitize_database_url(database_url: str) -> str:
    if not database_url:
        return database_url
    parsed = urlparse(database_url)
    host = parsed.hostname or ""
    if parsed.port:
        host = f"{host}:{parsed.port}"
    if parsed.username:
        netloc = f"{parsed.username}:***@{host}"
    else:
        netloc = host
    return urlunparse((parsed.scheme, netloc, parsed.path, parsed.params, parsed.query, parsed.fragment))


def sanitize_postgres_error(message: str, database_url: str) -> str:
    sanitized_url = sanitize_database_url(database_url)
    if database_url and database_url in message:
        message = message.replace(database_url, sanitized_url)
    parsed = urlparse(database_url)
    if parsed.password:
        message = message.replace(parsed.password, "***")
    return message


class PostgresClient:
    """Thin psycopg wrapper with lazy imports so tests do not require PostgreSQL."""

    def __init__(self, config: PostgresConfig) -> None:
        self.config = config

    @property
    def connection_info(self) -> PostgresConnectionInfo:
        return parse_postgres_connection_info(self.config.database_url)

    @contextmanager
    def connect(self) -> Iterator[Any]:
        if not self.config.database_url:
            raise PostgresUnavailableError("DATABASE_URL missing: Railway PostgreSQL is required in RAILWAY_LIGHT mode")
        try:
            import psycopg  # type: ignore[import-not-found]
        except ImportError as exc:
            raise PostgresUnavailableError("Install psycopg[binary] to use PostgreSQL-backed modules") from exc
        with psycopg.connect(self.config.database_url) as conn:
            yield conn

    def test_connection(self) -> None:
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()

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
