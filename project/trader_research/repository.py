"""PostgreSQL persistence for verifiable public-trader research."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
import json
from typing import Any, Iterable

from project.database.postgres import PostgresClient
from project.trader_research.hyperliquid import HyperliquidFill
from project.trader_research.reconstruction import PositionCycle


RESEARCH_SCHEMA = """
CREATE SCHEMA IF NOT EXISTS research;

CREATE TABLE IF NOT EXISTS research.public_trader_fills (
    fill_key TEXT PRIMARY KEY,
    wallet TEXT NOT NULL,
    coin TEXT NOT NULL,
    side TEXT NOT NULL,
    direction TEXT NOT NULL,
    price NUMERIC NOT NULL,
    size NUMERIC NOT NULL,
    start_position NUMERIC NOT NULL,
    end_position NUMERIC NOT NULL,
    closed_pnl NUMERIC NOT NULL,
    fee NUMERIC NOT NULL,
    fee_token TEXT NOT NULL,
    fill_time TIMESTAMPTZ NOT NULL,
    crossed BOOLEAN NOT NULL,
    tx_hash TEXT,
    order_id TEXT,
    trade_id TEXT,
    raw JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_public_trader_fills_wallet_time
    ON research.public_trader_fills(wallet, fill_time);

CREATE TABLE IF NOT EXISTS research.public_trader_cycles (
    cycle_key TEXT PRIMARY KEY,
    wallet TEXT NOT NULL,
    coin TEXT NOT NULL,
    pair TEXT,
    side TEXT NOT NULL,
    open_time TIMESTAMPTZ NOT NULL,
    close_time TIMESTAMPTZ NOT NULL,
    average_entry_price NUMERIC NOT NULL,
    average_exit_price NUMERIC NOT NULL,
    entry_quantity NUMERIC NOT NULL,
    exit_quantity NUMERIC NOT NULL,
    max_abs_position NUMERIC NOT NULL,
    gross_closed_pnl NUMERIC NOT NULL,
    fees NUMERIC NOT NULL,
    net_pnl NUMERIC NOT NULL,
    fill_count INTEGER NOT NULL,
    maker_fill_ratio NUMERIC NOT NULL,
    duration_minutes NUMERIC NOT NULL,
    truncated_start BOOLEAN NOT NULL,
    close_reason TEXT NOT NULL,
    entry_features JSONB,
    dataset_split TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_public_trader_cycles_wallet_close
    ON research.public_trader_cycles(wallet, close_time);
CREATE INDEX IF NOT EXISTS idx_public_trader_cycles_wallet_side
    ON research.public_trader_cycles(wallet, side, coin, close_time);

CREATE TABLE IF NOT EXISTS research.public_trader_playbooks (
    playbook_key TEXT PRIMARY KEY,
    wallet TEXT NOT NULL,
    rule JSONB NOT NULL,
    train_metrics JSONB NOT NULL,
    test_metrics JSONB NOT NULL,
    eligible_long BOOLEAN NOT NULL DEFAULT FALSE,
    rank_score NUMERIC NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_public_trader_playbooks_wallet_rank
    ON research.public_trader_playbooks(wallet, eligible_long, rank_score DESC);

CREATE TABLE IF NOT EXISTS research.public_trader_runs (
    id BIGSERIAL PRIMARY KEY,
    wallet TEXT NOT NULL,
    status TEXT NOT NULL,
    user_role TEXT,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at TIMESTAMPTZ,
    fill_count INTEGER NOT NULL DEFAULT 0,
    cycle_count INTEGER NOT NULL DEFAULT 0,
    feature_count INTEGER NOT NULL DEFAULT 0,
    playbook_count INTEGER NOT NULL DEFAULT 0,
    fill_limit_reached BOOLEAN NOT NULL DEFAULT FALSE,
    diagnostics JSONB NOT NULL DEFAULT '{}'::jsonb,
    summary JSONB NOT NULL DEFAULT '{}'::jsonb,
    error TEXT
);
"""


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"Unsupported JSON value: {type(value).__name__}")


def json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=_json_default)


class TraderResearchRepository:
    def __init__(self, client: PostgresClient) -> None:
        self.client = client

    def migrate(self) -> None:
        self.client.execute(RESEARCH_SCHEMA)

    def start_run(self, wallet: str, role: str) -> int:
        rows = self.client.fetch_all(
            """INSERT INTO research.public_trader_runs(wallet, status, user_role)
            VALUES (%s, 'RUNNING', %s) RETURNING id""",
            (wallet, role),
        )
        return int(rows[0][0])

    def finish_run(
        self,
        run_id: int,
        *,
        status: str,
        fill_count: int = 0,
        cycle_count: int = 0,
        feature_count: int = 0,
        playbook_count: int = 0,
        fill_limit_reached: bool = False,
        diagnostics: dict[str, Any] | None = None,
        summary: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        self.client.execute(
            """UPDATE research.public_trader_runs
            SET status = %s, finished_at = NOW(), fill_count = %s, cycle_count = %s,
                feature_count = %s, playbook_count = %s, fill_limit_reached = %s,
                diagnostics = %s::jsonb, summary = %s::jsonb, error = %s
            WHERE id = %s""",
            (
                status,
                fill_count,
                cycle_count,
                feature_count,
                playbook_count,
                fill_limit_reached,
                json_text(diagnostics or {}),
                json_text(summary or {}),
                error,
                run_id,
            ),
        )

    def save_fills(self, fills: Iterable[HyperliquidFill]) -> int:
        rows = [
            (
                fill.fill_key,
                fill.wallet,
                fill.coin,
                fill.side,
                fill.direction,
                fill.price,
                fill.size,
                fill.start_position,
                fill.end_position,
                fill.closed_pnl,
                fill.fee,
                fill.fee_token,
                fill.time,
                fill.crossed,
                fill.tx_hash,
                fill.order_id,
                fill.trade_id,
                json_text(fill.raw),
            )
            for fill in fills
        ]
        return self.client.executemany(
            """INSERT INTO research.public_trader_fills(
                fill_key, wallet, coin, side, direction, price, size, start_position,
                end_position, closed_pnl, fee, fee_token, fill_time, crossed, tx_hash,
                order_id, trade_id, raw
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s::jsonb
            ) ON CONFLICT (fill_key) DO UPDATE SET raw = EXCLUDED.raw""",
            rows,
        )

    def save_cycles(self, cycles: Iterable[PositionCycle], pair_map: dict[str, str]) -> int:
        rows = [
            (
                cycle.cycle_key,
                cycle.wallet,
                cycle.coin,
                pair_map.get(cycle.coin),
                cycle.side,
                cycle.open_time,
                cycle.close_time,
                cycle.average_entry_price,
                cycle.average_exit_price,
                cycle.entry_quantity,
                cycle.exit_quantity,
                cycle.max_abs_position,
                cycle.gross_closed_pnl,
                cycle.fees,
                cycle.net_pnl,
                cycle.fill_count,
                cycle.maker_fill_ratio,
                cycle.duration_minutes,
                cycle.truncated_start,
                cycle.close_reason,
            )
            for cycle in cycles
        ]
        return self.client.executemany(
            """INSERT INTO research.public_trader_cycles(
                cycle_key, wallet, coin, pair, side, open_time, close_time,
                average_entry_price, average_exit_price, entry_quantity, exit_quantity,
                max_abs_position, gross_closed_pnl, fees, net_pnl, fill_count,
                maker_fill_ratio, duration_minutes, truncated_start, close_reason
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s,
                %s, %s, %s, %s, %s,
                %s, %s, %s, %s
            ) ON CONFLICT (cycle_key) DO UPDATE SET
                pair = EXCLUDED.pair, net_pnl = EXCLUDED.net_pnl,
                fees = EXCLUDED.fees, updated_at = NOW()""",
            rows,
        )

    def available_pairs(self, exchange: str = "kraken") -> set[str]:
        rows = self.client.fetch_all(
            "SELECT DISTINCT pair FROM market_data.ohlc WHERE exchange = %s",
            (exchange,),
        )
        return {str(row[0]) for row in rows}

    def fetch_closed_ohlc(
        self,
        pair: str,
        timeframe: str,
        before_time: datetime,
        *,
        exchange: str = "kraken",
        limit: int = 260,
    ) -> list[tuple[Any, ...]]:
        seconds = {"15m": 900, "1h": 3600}[timeframe]
        return self.client.fetch_all(
            """SELECT timestamp, open, high, low, close, volume
            FROM market_data.ohlc
            WHERE exchange = %s AND pair = %s AND timeframe = %s
              AND timestamp + (%s * INTERVAL '1 second') <= %s
            ORDER BY timestamp DESC LIMIT %s""",
            (exchange, pair, timeframe, seconds, before_time, limit),
        )

    def save_cycle_feature(
        self,
        cycle_key: str,
        *,
        pair: str,
        split: str,
        features: dict[str, Any],
    ) -> None:
        self.client.execute(
            """UPDATE research.public_trader_cycles
            SET pair = %s, dataset_split = %s, entry_features = %s::jsonb, updated_at = NOW()
            WHERE cycle_key = %s""",
            (pair, split, json_text(features), cycle_key),
        )

    def replace_playbooks(self, wallet: str, playbooks: list[dict[str, Any]]) -> int:
        self.client.execute(
            "DELETE FROM research.public_trader_playbooks WHERE wallet = %s",
            (wallet,),
        )
        rows = [
            (
                item["playbook_key"],
                wallet,
                json_text(item["rule"]),
                json_text(item["train_metrics"]),
                json_text(item["test_metrics"]),
                bool(item["eligible_long"]),
                float(item["rank_score"]),
            )
            for item in playbooks
        ]
        return self.client.executemany(
            """INSERT INTO research.public_trader_playbooks(
                playbook_key, wallet, rule, train_metrics, test_metrics,
                eligible_long, rank_score
            ) VALUES (%s, %s, %s::jsonb, %s::jsonb, %s::jsonb, %s, %s)""",
            rows,
        )
