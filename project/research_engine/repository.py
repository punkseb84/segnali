"""PostgreSQL persistence owned by the progressive Research Engine."""
from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Sequence

from project.database.postgres import PostgresClient


@dataclass(frozen=True)
class ResearchCombination:
    id: int
    strategy: str
    pair: str
    timeframe: str
    parameters: dict[str, Any]


class ResearchRepository:
    """Repository restricted to research.* tables plus read-only market_data.ohlc."""

    def __init__(self, client: PostgresClient) -> None:
        self.client = client

    def seed_combinations(self, combinations: list[tuple[str, str, str, dict[str, Any]]]) -> int:
        rows: list[Sequence[Any]] = [(strategy, pair, timeframe, json.dumps(params, sort_keys=True)) for strategy, pair, timeframe, params in combinations]
        self.client.executemany(
            """INSERT INTO research.strategy_combinations(strategy, pair, timeframe, parameters)
            VALUES (%s, %s, %s, %s::jsonb)
            ON CONFLICT(combination_key) DO NOTHING""",
            rows,
        )
        return len(rows)

    def count_combinations(self) -> int:
        rows = self.client.fetch_all("SELECT COUNT(*) FROM research.strategy_combinations")
        return int(rows[0][0] or 0)

    def fetch_next_pending(self, limit: int) -> list[ResearchCombination]:
        rows = self.client.fetch_all(
            """SELECT id, strategy, pair, timeframe, parameters
            FROM research.strategy_combinations
            WHERE status IN ('PENDING', 'FAILED_RETRYABLE')
            ORDER BY id
            LIMIT %s""",
            (limit,),
        )
        return [ResearchCombination(int(row[0]), str(row[1]), str(row[2]), str(row[3]), dict(row[4] or {}) if isinstance(row[4], dict) else json.loads(row[4] or "{}")) for row in rows]

    def mark_running(self, combination_ids: list[int]) -> None:
        if not combination_ids:
            return
        self.client.execute(
            """UPDATE research.strategy_combinations
            SET status = 'RUNNING', updated_at = NOW()
            WHERE id = ANY(%s)""",
            (combination_ids,),
        )

    def mark_done(self, combination_id: int, status: str, error: str | None = None) -> None:
        self.client.execute(
            """UPDATE research.strategy_combinations
            SET status = %s, error_message = %s, updated_at = NOW()
            WHERE id = %s""",
            (status, error, combination_id),
        )

    def create_batch(self, batch_size: int, total_combinations: int) -> int:
        rows = self.client.fetch_all(
            """INSERT INTO research.research_batches(status, batch_size, total_combinations, processed_combinations)
            VALUES ('RUNNING', %s, %s, 0)
            RETURNING id""",
            (batch_size, total_combinations),
        )
        return int(rows[0][0])

    def finish_batch(self, batch_id: int, status: str, processed: int, last_combination_id: int | None = None) -> None:
        self.client.execute(
            """UPDATE research.research_batches
            SET status = %s,
                processed_combinations = %s,
                last_combination_id = %s,
                updated_at = NOW(),
                finished_at = NOW()
            WHERE id = %s""",
            (status, processed, last_combination_id, batch_id),
        )

    def fetch_ohlc(self, pair: str, timeframe: str, limit: int = 720) -> list[tuple[Any, ...]]:
        return self.client.fetch_all(
            """SELECT timestamp, open, high, low, close, volume
            FROM market_data.ohlc
            WHERE pair = %s AND timeframe = %s
            ORDER BY timestamp DESC
            LIMIT %s""",
            (pair, timeframe, limit),
        )


    def fetch_progress_counts(self) -> dict[str, int]:
        rows = self.client.fetch_all(
            """SELECT status, COUNT(*)
            FROM research.strategy_combinations
            GROUP BY status"""
        )
        return {str(status): int(count or 0) for status, count in rows}

    def fetch_best_result(self) -> dict[str, Any] | None:
        rows = self.client.fetch_all(
            """SELECT strategy, pair, timeframe, profit_factor, expectancy, net_profit
            FROM research.strategy_results
            WHERE strategy <> 'BOOTSTRAP_TEST'
              AND COALESCE(validation->>'status', '') <> 'BOOTSTRAP'
            ORDER BY profit_factor DESC NULLS LAST, expectancy DESC NULLS LAST, net_profit DESC NULLS LAST
            LIMIT 1"""
        )
        if not rows:
            return None
        row = rows[0]
        return {
            "strategy": row[0],
            "pair": row[1],
            "timeframe": row[2],
            "profit_factor": float(row[3] or 0),
            "expectancy": float(row[4] or 0),
            "net_profit": float(row[5] or 0),
        }

    def save_result(self, combination: ResearchCombination, metrics: dict[str, Any], batch_id: int) -> None:
        self.client.execute(
            """INSERT INTO research.strategy_results(
                combination_id, batch_id, strategy, pair, timeframe, parameters,
                win_rate, profit_factor, expectancy, sharpe, sortino, max_drawdown,
                net_profit, average_win, average_loss, recovery_factor, ulcer_index,
                mfe, mae, calmar_ratio, validation
            ) VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)""",
            (
                combination.id,
                batch_id,
                combination.strategy,
                combination.pair,
                combination.timeframe,
                json.dumps(combination.parameters, sort_keys=True),
                metrics["win_rate"],
                metrics["profit_factor"],
                metrics["expectancy"],
                metrics["sharpe"],
                metrics["sortino"],
                metrics["max_drawdown"],
                metrics["net_profit"],
                metrics["average_win"],
                metrics["average_loss"],
                metrics["recovery_factor"],
                metrics["ulcer_index"],
                metrics["mfe"],
                metrics["mae"],
                metrics["calmar_ratio"],
                json.dumps(metrics["validation"], sort_keys=True),
            ),
        )
