"""PostgreSQL access layer for the modular quant platform."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import time
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
    """Thin psycopg wrapper with retries for transient Railway network/DNS failures."""

    def __init__(self, config: PostgresConfig) -> None:
        self.config = config

    @property
    def connection_info(self) -> PostgresConnectionInfo:
        return parse_postgres_connection_info(self.config.database_url)

    @staticmethod
    def _is_transient_connection_error(exc: Exception) -> bool:
        text = str(exc).lower()
        transient_markers = (
            "temporary failure in name resolution",
            "name or service not known",
            "could not translate host name",
            "connection refused",
            "connection reset",
            "connection timed out",
            "timeout expired",
            "server closed the connection unexpectedly",
            "network is unreachable",
        )
        return any(marker in text for marker in transient_markers)

    @contextmanager
    def connect(self) -> Iterator[Any]:
        if not self.config.database_url:
            raise PostgresUnavailableError("DATABASE_URL missing: Railway PostgreSQL is required in RAILWAY_LIGHT mode")
        try:
            import psycopg  # type: ignore[import-not-found]
        except ImportError as exc:
            raise PostgresUnavailableError("Install psycopg[binary] to use PostgreSQL-backed modules") from exc

        # Railway private DNS can briefly become unavailable during deploys or
        # internal network reconfiguration.  A single failed DNS lookup must not
        # surface as an operational failure or interrupt the daily signal cycle.
        delays = (1.0, 2.0, 4.0, 8.0, 12.0)
        conn = None
        last_exc: Exception | None = None
        for attempt in range(len(delays) + 1):
            try:
                conn = psycopg.connect(self.config.database_url, connect_timeout=10)
                break
            except Exception as exc:
                last_exc = exc
                if not self._is_transient_connection_error(exc) or attempt >= len(delays):
                    raise
                time.sleep(delays[attempt])

        if conn is None:
            if last_exc is not None:
                raise last_exc
            raise PostgresUnavailableError("PostgreSQL connection could not be established")

        with conn:
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

    def executemany(self, sql: str, rows: Sequence[Sequence[Any]]) -> int:
        if not rows:
            return 0
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.executemany(sql, rows)
                rowcount = max(cur.rowcount, 0)
            conn.commit()
            return rowcount

    def fetch_all(self, sql: str, params: Sequence[Any] | None = None) -> list[tuple[Any, ...]]:
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params or ())
                return list(cur.fetchall())
