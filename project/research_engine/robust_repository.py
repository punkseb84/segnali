"""Versioned repository for statistically eligible walk-forward research results."""
from __future__ import annotations

import json
import os
from typing import Any

from project.research_engine.repository import ResearchCombination, ResearchRepository


ROBUST_RESEARCH_VERSION = "ROBUST_WALK_FORWARD_V1"
ROBUST_VALIDATION_STATUS = "ROBUST_WALK_FORWARD_BACKTEST_V1"


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


class RobustResearchRepository(ResearchRepository):
    """Ignore legacy optimization results and expose only robust OOS candidates."""

    def __init__(
        self,
        client,
        research_version: str = ROBUST_RESEARCH_VERSION,
        min_total_trades: int | None = None,
        min_test_trades: int | None = None,
        min_test_profit_factor: float | None = None,
        min_test_expectancy_eur: float | None = None,
    ) -> None:
        super().__init__(client)
        self.research_version = research_version
        self.min_total_trades = max(
            1,
            min_total_trades
            if min_total_trades is not None
            else _int_env("ROBUST_MIN_TOTAL_TRADES", 20),
        )
        self.min_test_trades = max(
            1,
            min_test_trades
            if min_test_trades is not None
            else _int_env("ROBUST_MIN_TEST_TRADES", 6),
        )
        self.min_test_profit_factor = max(
            1.0,
            min_test_profit_factor
            if min_test_profit_factor is not None
            else _float_env("ROBUST_MIN_TEST_PROFIT_FACTOR", 1.10),
        )
        self.min_test_expectancy_eur = max(
            0.0,
            min_test_expectancy_eur
            if min_test_expectancy_eur is not None
            else _float_env("ROBUST_MIN_TEST_EXPECTANCY_EUR", 0.01),
        )

    def count_combinations(self) -> int:
        rows = self.client.fetch_all(
            """SELECT COUNT(*)
            FROM research.strategy_combinations
            WHERE parameters->>'research_version' = %s""",
            (self.research_version,),
        )
        return int(rows[0][0] or 0)

    def existing_timeframes(self, timeframes: list[str]) -> set[str]:
        if not timeframes:
            return set()
        rows = self.client.fetch_all(
            """SELECT DISTINCT timeframe
            FROM research.strategy_combinations
            WHERE parameters->>'research_version' = %s
              AND timeframe = ANY(%s)""",
            (self.research_version, timeframes),
        )
        return {str(row[0]) for row in rows}

    def fetch_next_pending(
        self,
        limit: int,
        priority_timeframe: str | None = None,
    ) -> list[ResearchCombination]:
        priority_expression = (
            "CASE WHEN timeframe = %s THEN 0 ELSE 1 END" if priority_timeframe else "0"
        )
        params: tuple[Any, ...]
        if priority_timeframe:
            params = (self.research_version, priority_timeframe, limit)
        else:
            params = (self.research_version, limit)
        rows = self.client.fetch_all(
            f"""WITH pending AS (
                SELECT id, strategy, pair, timeframe, parameters,
                       ROW_NUMBER() OVER (
                           PARTITION BY strategy, pair, timeframe
                           ORDER BY id
                       ) AS research_bucket
                FROM research.strategy_combinations
                WHERE status IN ('PENDING', 'FAILED_RETRYABLE')
                  AND parameters->>'research_version' = %s
            )
            SELECT id, strategy, pair, timeframe, parameters
            FROM pending
            ORDER BY {priority_expression}, research_bucket, strategy, pair, timeframe, id
            LIMIT %s""",
            params,
        )
        return [
            ResearchCombination(
                int(row[0]),
                str(row[1]),
                str(row[2]),
                str(row[3]),
                dict(row[4] or {})
                if isinstance(row[4], dict)
                else json.loads(row[4] or "{}"),
            )
            for row in rows
        ]

    def reset_stale_results(self, required_validation_status: str = ROBUST_VALIDATION_STATUS) -> int:
        # The version is part of the combination key. Legacy combinations remain archived
        # and are never re-queued by the robust engine.
        return 0

    def fetch_candidate_results(
        self,
        timeframe: str | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        timeframe_filter = "AND result.timeframe = %s" if timeframe else ""
        params: list[Any] = [
            self.research_version,
            ROBUST_VALIDATION_STATUS,
            self.min_total_trades,
            self.min_test_trades,
            self.min_test_profit_factor,
            self.min_test_expectancy_eur,
        ]
        if timeframe:
            params.append(timeframe)
        params.append(limit)
        rows = self.client.fetch_all(
            f"""WITH eligible AS (
                SELECT
                    result.strategy,
                    result.pair,
                    result.timeframe,
                    result.profit_factor,
                    result.expectancy,
                    result.net_profit,
                    result.parameters,
                    result.validation,
                    COALESCE((result.validation->>'trades')::int, 0) AS total_trades,
                    COALESCE((result.validation->>'test_trades')::int, 0) AS test_trades,
                    ROW_NUMBER() OVER (
                        PARTITION BY result.strategy, result.pair, result.timeframe
                        ORDER BY
                            COALESCE((result.validation->>'test_trades')::int, 0) DESC,
                            result.expectancy DESC NULLS LAST,
                            result.profit_factor DESC NULLS LAST,
                            result.net_profit DESC NULLS LAST
                    ) AS rank_in_market
                FROM research.strategy_results AS result
                WHERE result.strategy <> 'BOOTSTRAP_TEST'
                  AND result.parameters->>'research_version' = %s
                  AND COALESCE(result.validation->>'status', '') = %s
                  AND COALESCE(result.validation->>'edge_status', '') = 'ELIGIBLE'
                  AND COALESCE((result.validation->>'trades')::int, 0) >= %s
                  AND COALESCE((result.validation->>'test_trades')::int, 0) >= %s
                  AND result.profit_factor >= %s
                  AND result.expectancy > %s
                  AND result.net_profit > 0
                  {timeframe_filter}
            )
            SELECT strategy, pair, timeframe, profit_factor, expectancy, net_profit,
                   parameters, validation
            FROM eligible
            WHERE rank_in_market = 1
            ORDER BY
                test_trades DESC,
                expectancy DESC NULLS LAST,
                profit_factor DESC NULLS LAST,
                net_profit DESC NULLS LAST
            LIMIT %s""",
            tuple(params),
        )
        return [
            {
                "strategy": row[0],
                "pair": row[1],
                "timeframe": row[2],
                "profit_factor": float(row[3] or 0),
                "expectancy": float(row[4] or 0),
                "net_profit": float(row[5] or 0),
                "parameters": dict(row[6] or {})
                if isinstance(row[6], dict)
                else json.loads(row[6] or "{}"),
                "validation": dict(row[7] or {})
                if isinstance(row[7], dict)
                else json.loads(row[7] or "{}"),
                "research_eligible": True,
            }
            for row in rows
        ]

    def fetch_candidate_diagnostics(self, timeframe: str | None = None) -> dict[str, Any]:
        timeframe_filter = "AND timeframe = %s" if timeframe else ""
        params: list[Any] = [self.research_version]
        if timeframe:
            params.append(timeframe)
        combination_rows = self.client.fetch_all(
            f"""SELECT
                    COUNT(*) AS total_combinations,
                    COUNT(*) FILTER (WHERE status = 'DONE') AS done_combinations,
                    COUNT(*) FILTER (WHERE status = 'PENDING') AS pending_combinations,
                    COUNT(*) FILTER (WHERE status = 'FAILED_RETRYABLE') AS retryable_combinations,
                    COUNT(*) FILTER (WHERE status = 'RUNNING') AS running_combinations
                FROM research.strategy_combinations
                WHERE parameters->>'research_version' = %s
                {timeframe_filter}""",
            tuple(params),
        )

        result_params: list[Any] = [
            self.research_version,
            ROBUST_VALIDATION_STATUS,
            self.min_total_trades,
            self.min_test_trades,
            self.min_test_profit_factor,
            self.min_test_expectancy_eur,
        ]
        if timeframe:
            result_params.append(timeframe)
        result_rows = self.client.fetch_all(
            f"""SELECT
                    COUNT(*) AS total_results,
                    COUNT(*) FILTER (
                        WHERE COALESCE(validation->>'status', '') = %s
                    ) AS walk_forward_results,
                    COUNT(*) FILTER (
                        WHERE COALESCE(validation->>'status', '') = %s
                          AND COALESCE(validation->>'edge_status', '') = 'ELIGIBLE'
                          AND COALESCE((validation->>'trades')::int, 0) >= %s
                          AND COALESCE((validation->>'test_trades')::int, 0) >= %s
                          AND profit_factor >= %s
                          AND expectancy > %s
                          AND net_profit > 0
                    ) AS eligible_results,
                    MAX(COALESCE((validation->>'candles')::int, 0)) AS max_candles,
                    MAX(COALESCE((validation->>'trades')::int, 0)) AS max_trades,
                    MAX(COALESCE((validation->>'test_trades')::int, 0)) AS max_test_trades
                FROM research.strategy_results
                WHERE parameters->>'research_version' = %s
                  {timeframe_filter}""".replace(
                "WHERE COALESCE(validation->>'status', '') = %s",
                "WHERE COALESCE(validation->>'status', '') = %s",
                1,
            ),
            tuple(
                [
                    ROBUST_VALIDATION_STATUS,
                    ROBUST_VALIDATION_STATUS,
                    self.min_total_trades,
                    self.min_test_trades,
                    self.min_test_profit_factor,
                    self.min_test_expectancy_eur,
                    self.research_version,
                ]
                + ([timeframe] if timeframe else [])
            ),
        )
        combination = combination_rows[0] if combination_rows else (0, 0, 0, 0, 0)
        result = result_rows[0] if result_rows else (0, 0, 0, 0, 0, 0)
        return {
            "research_version": self.research_version,
            "timeframe": timeframe or "ANY",
            "total_combinations": int(combination[0] or 0),
            "done_combinations": int(combination[1] or 0),
            "pending_combinations": int(combination[2] or 0),
            "retryable_combinations": int(combination[3] or 0),
            "running_combinations": int(combination[4] or 0),
            "total_results": int(result[0] or 0),
            "walk_forward_results": int(result[1] or 0),
            "eligible_results": int(result[2] or 0),
            "max_candles": int(result[3] or 0),
            "max_trades": int(result[4] or 0),
            "max_test_trades": int(result[5] or 0),
            "min_total_trades": self.min_total_trades,
            "min_test_trades": self.min_test_trades,
            "min_test_profit_factor": self.min_test_profit_factor,
            "min_test_expectancy_eur": self.min_test_expectancy_eur,
        }
