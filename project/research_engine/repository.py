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

    def existing_timeframes(self, timeframes: list[str]) -> set[str]:
        if not timeframes:
            return set()
        rows = self.client.fetch_all(
            """SELECT DISTINCT timeframe
            FROM research.strategy_combinations
            WHERE timeframe = ANY(%s)""",
            (timeframes,),
        )
        return {str(row[0]) for row in rows}

    def fetch_next_pending(self, limit: int, priority_timeframe: str | None = None) -> list[ResearchCombination]:
        priority_expression = "CASE WHEN timeframe = %s THEN 0 ELSE 1 END" if priority_timeframe else "0"
        params: tuple[Any, ...] = (priority_timeframe, limit) if priority_timeframe else (limit,)
        rows = self.client.fetch_all(
            f"""WITH pending AS (
                SELECT id, strategy, pair, timeframe, parameters,
                       ROW_NUMBER() OVER (PARTITION BY strategy, pair, timeframe ORDER BY id) AS research_bucket
                FROM research.strategy_combinations
                WHERE status IN ('PENDING', 'FAILED_RETRYABLE')
            )
            SELECT id, strategy, pair, timeframe, parameters
            FROM pending
            ORDER BY {priority_expression}, research_bucket, strategy, pair, timeframe, id
            LIMIT %s""",
            params,
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


    def reset_stale_results(self, required_validation_status: str = "FILTERED_LIVE_ALIGNED_BACKTEST") -> int:
        rows = self.client.fetch_all(
            """UPDATE research.strategy_combinations AS combination
            SET status = 'PENDING', error_message = NULL, updated_at = NOW()
            WHERE combination.status = 'DONE'
              AND EXISTS (
                  SELECT 1
                  FROM research.strategy_results AS result
                  WHERE result.combination_id = combination.id
                    AND COALESCE(result.validation->>'status', '') <> %s
              )
              AND NOT EXISTS (
                  SELECT 1
                  FROM research.strategy_results AS result
                  WHERE result.combination_id = combination.id
                    AND COALESCE(result.validation->>'status', '') = %s
              )
            RETURNING combination.id""",
            (required_validation_status, required_validation_status),
        )
        return len(rows)

    def fetch_progress_counts(self) -> dict[str, int]:
        rows = self.client.fetch_all(
            """SELECT status, COUNT(*)
            FROM research.strategy_combinations
            GROUP BY status"""
        )
        return {str(status): int(count or 0) for status, count in rows}

    def fetch_best_result(self, timeframe: str | None = None) -> dict[str, Any] | None:
        rows = self.fetch_candidate_results(timeframe=timeframe, limit=1)
        return rows[0] if rows else None

    def fetch_candidate_results(self, timeframe: str | None = None, limit: int = 10) -> list[dict[str, Any]]:
        timeframe_filter = "AND timeframe = %s" if timeframe else ""
        params: tuple[Any, ...] = (timeframe, limit) if timeframe else (limit,)
        rows = self.client.fetch_all(
            f"""WITH best_per_market AS (
                SELECT DISTINCT ON (strategy, pair, timeframe)
                    strategy, pair, timeframe, profit_factor, expectancy, net_profit, parameters, validation
                FROM research.strategy_results
                WHERE strategy <> 'BOOTSTRAP_TEST'
                  AND COALESCE(validation->>'status', '') = 'FILTERED_LIVE_ALIGNED_BACKTEST'
                  AND (profit_factor > 1.0 OR expectancy > 0)
                  {timeframe_filter}
                ORDER BY strategy, pair, timeframe, profit_factor DESC NULLS LAST, expectancy DESC NULLS LAST, net_profit DESC NULLS LAST
            )
            SELECT strategy, pair, timeframe, profit_factor, expectancy, net_profit, parameters, validation
            FROM best_per_market
            ORDER BY profit_factor DESC NULLS LAST, expectancy DESC NULLS LAST, net_profit DESC NULLS LAST
            LIMIT %s""",
            params,
        )
        return [
            {
                "strategy": row[0],
                "pair": row[1],
                "timeframe": row[2],
                "profit_factor": float(row[3] or 0),
                "expectancy": float(row[4] or 0),
                "net_profit": float(row[5] or 0),
                "parameters": dict(row[6] or {}) if isinstance(row[6], dict) else json.loads(row[6] or "{}"),
                "validation": dict(row[7] or {}) if isinstance(row[7], dict) else json.loads(row[7] or "{}"),
            }
            for row in rows
        ]


    def fetch_candidate_diagnostics(self, timeframe: str | None = None) -> dict[str, Any]:
        timeframe_filter = "WHERE timeframe = %s" if timeframe else ""
        combination_params: tuple[Any, ...] = (timeframe,) if timeframe else ()
        combination_rows = self.client.fetch_all(
            f"""SELECT
                    COUNT(*) AS total_combinations,
                    COUNT(*) FILTER (WHERE status = 'DONE') AS done_combinations,
                    COUNT(*) FILTER (WHERE status = 'PENDING') AS pending_combinations,
                    COUNT(*) FILTER (WHERE status = 'FAILED_RETRYABLE') AS retryable_combinations,
                    COUNT(*) FILTER (WHERE status = 'RUNNING') AS running_combinations
                FROM research.strategy_combinations
                {timeframe_filter}""",
            combination_params,
        )
        result_filter = "AND timeframe = %s" if timeframe else ""
        result_params: tuple[Any, ...] = (timeframe,) if timeframe else ()
        result_rows = self.client.fetch_all(
            f"""SELECT
                    COUNT(*) AS total_results,
                    COUNT(*) FILTER (WHERE COALESCE(validation->>'status', '') = 'FILTERED_LIVE_ALIGNED_BACKTEST') AS filtered_live_aligned_results,
                    COUNT(*) FILTER (
                        WHERE COALESCE(validation->>'status', '') = 'FILTERED_LIVE_ALIGNED_BACKTEST'
                          AND (profit_factor > 1.0 OR expectancy > 0)
                    ) AS edge_results,
                    MAX(profit_factor) FILTER (WHERE COALESCE(validation->>'status', '') = 'FILTERED_LIVE_ALIGNED_BACKTEST') AS best_live_profit_factor,
                    MAX(expectancy) FILTER (WHERE COALESCE(validation->>'status', '') = 'FILTERED_LIVE_ALIGNED_BACKTEST') AS best_live_expectancy
                FROM research.strategy_results
                WHERE strategy <> 'BOOTSTRAP_TEST'
                {result_filter}""",
            result_params,
        )
        combination = combination_rows[0] if combination_rows else (0, 0, 0, 0, 0)
        result = result_rows[0] if result_rows else (0, 0, 0, None, None)
        return {
            "timeframe": timeframe or "ANY",
            "total_combinations": int(combination[0] or 0),
            "done_combinations": int(combination[1] or 0),
            "pending_combinations": int(combination[2] or 0),
            "retryable_combinations": int(combination[3] or 0),
            "running_combinations": int(combination[4] or 0),
            "total_results": int(result[0] or 0),
            "filtered_live_aligned_results": int(result[1] or 0),
            "edge_results": int(result[2] or 0),
            "best_live_profit_factor": float(result[3]) if result[3] is not None else None,
            "best_live_expectancy": float(result[4]) if result[4] is not None else None,
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
