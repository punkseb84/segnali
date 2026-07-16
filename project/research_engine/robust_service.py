"""Compact walk-forward research designed to produce defensible operational edges.

The legacy grid explored a very large parameter space on only 720 candles. This module
uses a small strategy-specific profile set, longer stored history, a chronological
train/test split, non-overlapping trades and explicit out-of-sample eligibility.
"""
from __future__ import annotations

import math
import os
from typing import Any, Iterable

import pandas as pd

from project.research_engine.repository import ResearchCombination
from project.research_engine.robust_repository import (
    ROBUST_RESEARCH_VERSION,
    ROBUST_VALIDATION_STATUS,
)
from project.research_engine.service import ProgressiveResearchEngine, ResearchBatchResult


# LONG-only profiles. TREND_DOWN is intentionally absent: a falling market is not a
# valid long regime until a separate reversal model is proven out of sample.
ROBUST_STRATEGY_PROFILES: dict[str, list[dict[str, Any]]] = {
    "Breakout": [
        {"rsi_range": "50-70", "adx_min": 20, "atr_multiplier": 1.0, "relative_volume_min": 1.0, "reward_risk": 1.5, "regimes": ["BREAKOUT", "HIGH_VOLATILITY", "TREND_UP"]},
        {"rsi_range": "55-70", "adx_min": 25, "atr_multiplier": 1.2, "relative_volume_min": 1.2, "reward_risk": 2.0, "regimes": ["BREAKOUT", "HIGH_VOLATILITY"]},
    ],
    "Breakout Retest": [
        {"rsi_range": "45-65", "adx_min": 20, "atr_multiplier": 1.0, "relative_volume_min": 0.8, "reward_risk": 1.5, "regimes": ["TREND_UP", "BREAKOUT"]},
        {"rsi_range": "50-65", "adx_min": 25, "atr_multiplier": 1.2, "relative_volume_min": 1.0, "reward_risk": 2.0, "regimes": ["TREND_UP"]},
    ],
    "Liquidity Sweep": [
        {"rsi_range": "40-60", "adx_min": 15, "atr_multiplier": 1.0, "relative_volume_min": 0.8, "reward_risk": 1.5, "regimes": ["RANGE", "LOW_VOLATILITY"]},
        {"rsi_range": "40-55", "adx_min": 20, "atr_multiplier": 1.2, "relative_volume_min": 1.0, "reward_risk": 2.0, "regimes": ["RANGE", "COMPRESSION"]},
    ],
    "Pullback Trend": [
        {"rsi_range": "45-65", "adx_min": 20, "atr_multiplier": 1.0, "relative_volume_min": 0.8, "reward_risk": 1.5, "regimes": ["TREND_UP"]},
        {"rsi_range": "50-65", "adx_min": 25, "atr_multiplier": 1.2, "relative_volume_min": 1.0, "reward_risk": 2.0, "regimes": ["TREND_UP"]},
    ],
    "Range Reversal": [
        {"rsi_range": "40-60", "adx_min": 15, "atr_multiplier": 1.0, "relative_volume_min": 0.8, "reward_risk": 1.5, "regimes": ["RANGE", "LOW_VOLATILITY"]},
        {"rsi_range": "40-55", "adx_min": 15, "atr_multiplier": 1.2, "relative_volume_min": 1.0, "reward_risk": 2.0, "regimes": ["RANGE"]},
    ],
    "Compression Breakout": [
        {"rsi_range": "45-65", "adx_min": 15, "atr_multiplier": 1.0, "relative_volume_min": 1.0, "reward_risk": 1.5, "regimes": ["COMPRESSION", "BREAKOUT"]},
        {"rsi_range": "50-70", "adx_min": 20, "atr_multiplier": 1.2, "relative_volume_min": 1.2, "reward_risk": 2.0, "regimes": ["COMPRESSION", "BREAKOUT"]},
    ],
    "Momentum": [
        {"rsi_range": "50-70", "adx_min": 20, "atr_multiplier": 1.0, "relative_volume_min": 1.0, "reward_risk": 1.5, "regimes": ["TREND_UP", "BREAKOUT", "HIGH_VOLATILITY"]},
        {"rsi_range": "55-70", "adx_min": 25, "atr_multiplier": 1.2, "relative_volume_min": 1.2, "reward_risk": 2.0, "regimes": ["TREND_UP", "BREAKOUT"]},
    ],
    "Mean Reversion": [
        {"rsi_range": "40-55", "adx_min": 15, "atr_multiplier": 1.0, "relative_volume_min": 0.8, "reward_risk": 1.5, "regimes": ["RANGE", "LOW_VOLATILITY"]},
        {"rsi_range": "40-50", "adx_min": 15, "atr_multiplier": 1.2, "relative_volume_min": 1.0, "reward_risk": 2.0, "regimes": ["RANGE"]},
    ],
    "Trend Following": [
        {"rsi_range": "50-70", "adx_min": 20, "atr_multiplier": 1.0, "relative_volume_min": 0.8, "reward_risk": 1.5, "regimes": ["TREND_UP"]},
        {"rsi_range": "55-70", "adx_min": 25, "atr_multiplier": 1.2, "relative_volume_min": 1.0, "reward_risk": 2.0, "regimes": ["TREND_UP"]},
    ],
    "Volatility Expansion": [
        {"rsi_range": "50-70", "adx_min": 20, "atr_multiplier": 1.0, "relative_volume_min": 1.0, "reward_risk": 1.5, "regimes": ["HIGH_VOLATILITY", "BREAKOUT"]},
        {"rsi_range": "55-70", "adx_min": 25, "atr_multiplier": 1.2, "relative_volume_min": 1.2, "reward_risk": 2.0, "regimes": ["HIGH_VOLATILITY", "BREAKOUT"]},
    ],
}


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


class RobustWalkForwardResearchEngine(ProgressiveResearchEngine):
    """Research only compact, versioned profiles and validate them out of sample."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.research_version = ROBUST_RESEARCH_VERSION
        self.robust_ohlc_limit = max(720, _int_env("ROBUST_RESEARCH_OHLC_LIMIT", 5000))
        self.min_candles = max(300, _int_env("ROBUST_MIN_CANDLES", 1500))
        self.train_fraction = min(0.85, max(0.55, _float_env("ROBUST_TRAIN_FRACTION", 0.70)))
        self.min_total_trades = max(1, _int_env("ROBUST_MIN_TOTAL_TRADES", 20))
        self.min_train_trades = max(1, _int_env("ROBUST_MIN_TRAIN_TRADES", 12))
        self.min_test_trades = max(1, _int_env("ROBUST_MIN_TEST_TRADES", 6))
        self.min_train_pf = max(1.0, _float_env("ROBUST_MIN_TRAIN_PROFIT_FACTOR", 1.05))
        self.min_test_pf = max(1.0, _float_env("ROBUST_MIN_TEST_PROFIT_FACTOR", 1.10))
        self.min_test_expectancy = max(0.0, _float_env("ROBUST_MIN_TEST_EXPECTANCY_EUR", 0.01))
        self._market_cache: dict[tuple[str, str], tuple[list[tuple[Any, ...]], pd.DataFrame]] = {}
        self.logger.info(
            "ROBUST_RESEARCH enabled version=%s ohlc_limit=%s min_candles=%s "
            "train_fraction=%.2f total_trades=%s train_trades=%s test_trades=%s "
            "train_pf=%.3f test_pf=%.3f test_expectancy=%.4f profiles=%s",
            self.research_version,
            self.robust_ohlc_limit,
            self.min_candles,
            self.train_fraction,
            self.min_total_trades,
            self.min_train_trades,
            self.min_test_trades,
            self.min_train_pf,
            self.min_test_pf,
            self.min_test_expectancy,
            sum(len(profile["regimes"]) for profiles in ROBUST_STRATEGY_PROFILES.values() for profile in profiles),
        )

    def seed_combinations(
        self,
        pairs: list[str],
        timeframes: list[str],
        chunk_size: int = 1000,
    ) -> int:
        selected = [self.priority_timeframe] if self.priority_timeframe else timeframes
        return super().seed_combinations(pairs, selected, chunk_size=chunk_size)

    def generate_combinations(self, pairs: list[str], timeframes: list[str]):
        selected_timeframes = [self.priority_timeframe] if self.priority_timeframe else timeframes
        for strategy, profiles in ROBUST_STRATEGY_PROFILES.items():
            for pair in pairs:
                for timeframe in selected_timeframes:
                    for profile_index, profile in enumerate(profiles, start=1):
                        for regime in profile["regimes"]:
                            parameters = {
                                key: value
                                for key, value in profile.items()
                                if key != "regimes"
                            }
                            parameters.update(
                                {
                                    "market_regime": regime,
                                    "research_version": self.research_version,
                                    "profile_id": f"{strategy}:{profile_index}:{regime}",
                                }
                            )
                            yield strategy, pair, timeframe, parameters

    def process_one_batch(self, sleep_after: bool = True) -> ResearchBatchResult:
        self._market_cache.clear()
        try:
            return super().process_one_batch(sleep_after=sleep_after)
        finally:
            self._market_cache.clear()

    def evaluate_combination(self, combination: ResearchCombination) -> dict[str, Any]:
        chronological, frame = self._market_frame(combination.pair, combination.timeframe)
        candle_count = len(chronological)
        if candle_count < self.min_candles:
            return self._robust_empty_metrics(
                "INSUFFICIENT_LONG_HISTORY",
                candle_count,
                combination,
            )

        horizon = max(1, self.probability_horizon_candles)
        split_index = int(len(frame) * self.train_fraction)
        signal_mask = self.build_signal_mask(frame, combination).fillna(False)
        all_indexes = [
            int(index)
            for index in frame.index[signal_mask].tolist()
            if int(index) < len(frame) - horizon
        ]
        train_indexes = [index for index in all_indexes if index < split_index - horizon]
        test_indexes = [index for index in all_indexes if index >= split_index]
        reward_risk = float(combination.parameters.get("reward_risk", 1.5) or 1.5)
        atr_multiplier = float(combination.parameters.get("atr_multiplier", 1.0) or 1.0)

        train = self._evaluate_indexes(
            frame,
            chronological,
            train_indexes,
            horizon,
            reward_risk,
            atr_multiplier,
        )
        test = self._evaluate_indexes(
            frame,
            chronological,
            test_indexes,
            horizon,
            reward_risk,
            atr_multiplier,
        )
        total_trades = int(train["trades"] + test["trades"])
        blockers = self._edge_blockers(train, test, total_trades)
        edge_status = "ELIGIBLE" if not blockers else "INSUFFICIENT_OR_UNSTABLE"

        self.logger.info(
            "ROBUST_RESEARCH_RESULT strategy=%s pair=%s timeframe=%s profile=%s "
            "candles=%s train_trades=%s test_trades=%s train_pf=%.4f test_pf=%.4f "
            "train_ev=%.6f test_ev=%.6f edge_status=%s blockers=%s",
            combination.strategy,
            combination.pair,
            combination.timeframe,
            combination.parameters.get("profile_id"),
            candle_count,
            train["trades"],
            test["trades"],
            train["profit_factor"],
            test["profit_factor"],
            train["expectancy"],
            test["expectancy"],
            edge_status,
            blockers,
        )

        # Top-level metrics are deliberately OOS metrics. The Strategy Engine must never
        # rank configurations using their in-sample performance.
        return {
            "win_rate": test["win_rate"],
            "profit_factor": test["profit_factor"],
            "expectancy": test["expectancy"],
            "sharpe": test["sharpe"],
            "sortino": test["sortino"],
            "max_drawdown": test["max_drawdown"],
            "net_profit": test["net_profit"],
            "average_win": test["average_win"],
            "average_loss": test["average_loss"],
            "recovery_factor": test["recovery_factor"],
            "ulcer_index": 0.0,
            "mfe": test["mfe"],
            "mae": test["mae"],
            "calmar_ratio": test["recovery_factor"],
            "validation": {
                "status": ROBUST_VALIDATION_STATUS,
                "edge_status": edge_status,
                "edge_blockers": blockers,
                "research_version": self.research_version,
                "candles": candle_count,
                "split_index": split_index,
                "train_fraction": self.train_fraction,
                "trades": total_trades,
                "train_trades": train["trades"],
                "test_trades": test["trades"],
                "train_profit_factor": train["profit_factor"],
                "test_profit_factor": test["profit_factor"],
                "train_expectancy": train["expectancy"],
                "test_expectancy": test["expectancy"],
                "train_net_profit": train["net_profit"],
                "test_net_profit": test["net_profit"],
                "horizon_candles": horizon,
                "reward_risk": reward_risk,
                "atr_multiplier": atr_multiplier,
                "trade_notional_eur": self.trade_notional_eur,
                "buy_fee_rate": self.buy_fee_rate,
                "sell_fee_rate": self.sell_fee_rate,
                "spread_rate": self.spread_rate,
                "slippage_rate": self.slippage_rate,
                "strategy_filter": combination.strategy,
                "non_overlapping_trades": True,
                "long_only_trend_down_disabled": True,
            },
        }

    def _market_frame(
        self,
        pair: str,
        timeframe: str,
    ) -> tuple[list[tuple[Any, ...]], pd.DataFrame]:
        key = (pair, timeframe)
        cached = self._market_cache.get(key)
        if cached is not None:
            return cached
        candles = self.repository.fetch_ohlc(
            pair,
            timeframe,
            limit=self.robust_ohlc_limit,
        )
        chronological = list(reversed(candles))
        frame = self.build_feature_frame(chronological) if chronological else pd.DataFrame()
        cached = (chronological, frame)
        self._market_cache[key] = cached
        return cached

    def _evaluate_indexes(
        self,
        frame: pd.DataFrame,
        chronological: list[tuple[Any, ...]],
        indexes: Iterable[int],
        horizon: int,
        reward_risk: float,
        atr_multiplier: float,
    ) -> dict[str, float | int]:
        trade_results: list[float] = []
        favorable: list[float] = []
        adverse: list[float] = []
        last_exit_index = -1
        for index in indexes:
            # Overlapping observations are not independent trades and artificially inflate
            # sample size. Wait until the previous horizon has completed.
            if index <= last_exit_index:
                continue
            row = frame.iloc[index]
            entry = float(row["close"])
            atr_value = float(row.get("atr14") or 0.0)
            if entry <= 0 or atr_value <= 0 or pd.isna(atr_value):
                continue
            future = chronological[index + 1 : index + horizon + 1]
            if not future:
                continue
            stop_distance = max(atr_value * atr_multiplier, entry * 0.0001)
            stop = entry - stop_distance
            target = entry + stop_distance * reward_risk
            exit_price = float(future[-1][4])
            exit_offset = len(future)
            for offset, candle in enumerate(future, start=1):
                high = float(candle[2])
                low = float(candle[3])
                hit_target = high >= target
                hit_stop = low <= stop
                if hit_stop and hit_target:
                    exit_price = stop
                    exit_offset = offset
                    break
                if hit_stop:
                    exit_price = stop
                    exit_offset = offset
                    break
                if hit_target:
                    exit_price = target
                    exit_offset = offset
                    break
            favorable.append(max(float(candle[2]) - entry for candle in future))
            adverse.append(min(float(candle[3]) - entry for candle in future))
            trade_results.append(self._net_long_result(entry, exit_price))
            last_exit_index = index + exit_offset
        return self._metrics(trade_results, favorable, adverse)

    def _metrics(
        self,
        results: list[float],
        favorable: list[float],
        adverse: list[float],
    ) -> dict[str, float | int]:
        wins = [value for value in results if value > 0]
        losses = [value for value in results if value < 0]
        gross_profit = sum(wins)
        gross_loss = abs(sum(losses))
        net_profit = sum(results)
        trades = len(results)
        expectancy = net_profit / trades if trades else 0.0
        profit_factor = (
            min(gross_profit / gross_loss, 10.0)
            if gross_loss > 1e-9
            else (10.0 if gross_profit > 0 else 0.0)
        )
        average_win = gross_profit / len(wins) if wins else 0.0
        average_loss = sum(losses) / len(losses) if losses else 0.0
        max_drawdown = self._max_drawdown(results)
        recovery_factor = net_profit / max_drawdown if max_drawdown > 1e-9 else 0.0
        mean = expectancy
        variance = sum((value - mean) ** 2 for value in results) / max(trades - 1, 1)
        std = math.sqrt(max(variance, 0.0))
        sharpe = mean / std * math.sqrt(trades) if std > 1e-9 and trades > 1 else 0.0
        downside = [value for value in results if value < 0]
        downside_variance = (
            sum(value**2 for value in downside) / len(downside) if downside else 0.0
        )
        sortino = mean / math.sqrt(downside_variance) * math.sqrt(trades) if downside_variance > 1e-9 else 0.0
        return {
            "trades": trades,
            "win_rate": len(wins) / trades * 100 if trades else 0.0,
            "profit_factor": profit_factor,
            "expectancy": expectancy,
            "net_profit": net_profit,
            "average_win": average_win,
            "average_loss": average_loss,
            "max_drawdown": max_drawdown,
            "recovery_factor": recovery_factor,
            "sharpe": sharpe,
            "sortino": sortino,
            "mfe": max(favorable) if favorable else 0.0,
            "mae": min(adverse) if adverse else 0.0,
        }

    def _edge_blockers(
        self,
        train: dict[str, float | int],
        test: dict[str, float | int],
        total_trades: int,
    ) -> list[str]:
        blockers: list[str] = []
        if total_trades < self.min_total_trades:
            blockers.append("TOTAL_TRADES_BELOW_MINIMUM")
        if int(train["trades"]) < self.min_train_trades:
            blockers.append("TRAIN_TRADES_BELOW_MINIMUM")
        if int(test["trades"]) < self.min_test_trades:
            blockers.append("TEST_TRADES_BELOW_MINIMUM")
        if float(train["profit_factor"]) < self.min_train_pf:
            blockers.append("TRAIN_PROFIT_FACTOR_BELOW_MINIMUM")
        if float(train["expectancy"]) <= 0.0:
            blockers.append("TRAIN_EXPECTANCY_NOT_POSITIVE")
        if float(test["profit_factor"]) < self.min_test_pf:
            blockers.append("TEST_PROFIT_FACTOR_BELOW_MINIMUM")
        if float(test["expectancy"]) < self.min_test_expectancy:
            blockers.append("TEST_EXPECTANCY_BELOW_MINIMUM")
        if float(test["net_profit"]) <= 0.0:
            blockers.append("TEST_NET_PROFIT_NOT_POSITIVE")
        return blockers

    def _robust_empty_metrics(
        self,
        reason: str,
        candles: int,
        combination: ResearchCombination,
    ) -> dict[str, Any]:
        metrics = self.empty_metrics(ROBUST_VALIDATION_STATUS, candles)
        metrics["validation"].update(
            {
                "edge_status": "INSUFFICIENT_OR_UNSTABLE",
                "edge_blockers": [reason],
                "research_version": self.research_version,
                "trades": 0,
                "train_trades": 0,
                "test_trades": 0,
                "profile_id": combination.parameters.get("profile_id"),
            }
        )
        return metrics
