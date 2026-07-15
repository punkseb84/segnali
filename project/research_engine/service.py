"""Progressive PostgreSQL-backed Research Engine for Railway Light mode.

The service never calls Kraken and never uses SQLite as its primary database. It
reads OHLC rows already collected in PostgreSQL and processes one small batch per
cycle so Railway can run without a dedicated server.
"""
from __future__ import annotations

import itertools
import time
from dataclasses import dataclass
from typing import Any

import pandas as pd

from indicators import add_indicators
from project.research_engine.repository import ResearchCombination, ResearchRepository
from project.shared.events import Event, EventBus, EventType
from project.shared.logging import get_module_logger


STRATEGIES = [
    "Breakout",
    "Breakout Retest",
    "Liquidity Sweep",
    "Pullback Trend",
    "Range Reversal",
    "Compression Breakout",
    "Momentum",
    "Mean Reversion",
    "Trend Following",
    "Volatility Expansion",
]
RSI_RANGES = [(40, 45), (45, 50), (50, 55), (55, 60), (60, 65), (65, 70)]
ADX_VALUES = [15, 20, 25, 30]
ATR_VALUES = [0.5, 0.8, 1.0, 1.2]
RELATIVE_VOLUME_VALUES = [0.8, 1.0, 1.2, 1.5]
REWARD_RISK_VALUES = [1.0, 1.2, 1.5, 2.0]
REGIMES = ["TREND_UP", "TREND_DOWN", "RANGE", "HIGH_VOLATILITY", "LOW_VOLATILITY", "COMPRESSION", "BREAKOUT"]
VALIDATION_STATUS = "FILTERED_LIVE_ALIGNED_BACKTEST"


@dataclass(frozen=True)
class ResearchBatchResult:
    batch_id: int | None
    processed: int
    status: str
    message: str


class ProgressiveResearchEngine:
    def __init__(
        self,
        repository: ResearchRepository,
        event_bus: EventBus | None = None,
        batch_size: int = 100,
        max_runtime_minutes: int = 20,
        sleep_between_batches_seconds: int = 60,
        batch_interval_seconds: int = 3600,
        priority_timeframe: str | None = None,
        trade_notional_eur: float = 100.0,
        buy_fee_rate: float = 0.001,
        sell_fee_rate: float = 0.001,
        spread_rate: float = 0.0005,
        slippage_rate: float = 0.0,
        probability_horizon_candles: int = 8,
    ) -> None:
        self.repository = repository
        self.event_bus = event_bus or EventBus()
        self.batch_size = batch_size
        self.max_runtime_seconds = max_runtime_minutes * 60
        self.sleep_between_batches_seconds = sleep_between_batches_seconds
        self.batch_interval_seconds = batch_interval_seconds
        self.priority_timeframe = priority_timeframe
        self.trade_notional_eur = trade_notional_eur
        self.buy_fee_rate = buy_fee_rate
        self.sell_fee_rate = sell_fee_rate
        self.spread_rate = spread_rate
        self.slippage_rate = slippage_rate
        self.probability_horizon_candles = probability_horizon_candles
        self._stale_results_reset = False
        self.logger = get_module_logger("research")

    def generate_combinations(self, pairs: list[str], timeframes: list[str]):
        for strategy, pair, timeframe, rsi, adx, atr, rv, rr, regime in itertools.product(
            STRATEGIES,
            pairs,
            timeframes,
            RSI_RANGES,
            ADX_VALUES,
            ATR_VALUES,
            RELATIVE_VOLUME_VALUES,
            REWARD_RISK_VALUES,
            REGIMES,
        ):
            yield (
                strategy,
                pair,
                timeframe,
                {
                    "rsi_range": f"{rsi[0]}-{rsi[1]}",
                    "adx_min": adx,
                    "atr_multiplier": atr,
                    "relative_volume_min": rv,
                    "reward_risk": rr,
                    "market_regime": regime,
                },
            )

    def seed_combinations(self, pairs: list[str], timeframes: list[str], chunk_size: int = 1000) -> int:
        existing = self.repository.count_combinations()
        existing_timeframes = self.repository.existing_timeframes(timeframes)
        missing_timeframes = [timeframe for timeframe in timeframes if timeframe not in existing_timeframes]
        if existing > 0 and not missing_timeframes:
            self.logger.info("Research combinations already present=%s timeframes=%s; skip reseed", existing, sorted(existing_timeframes))
            return existing
        seed_timeframes = missing_timeframes or timeframes
        if missing_timeframes:
            self.logger.info("Research combinations missing for timeframes=%s; seeding only missing timeframes", missing_timeframes)
        seeded = 0
        chunk: list[tuple[str, str, str, dict[str, Any]]] = []
        for combination in self.generate_combinations(pairs, seed_timeframes):
            chunk.append(combination)
            if len(chunk) >= chunk_size:
                seeded += self.repository.seed_combinations(chunk)
                chunk = []
        if chunk:
            seeded += self.repository.seed_combinations(chunk)
        self.logger.info("Research combinations seeded_or_seen=%s", seeded)
        return seeded

    def process_one_batch(self, sleep_after: bool = True) -> ResearchBatchResult:
        started = time.monotonic()
        self.logger.info("RESEARCH BATCH START batch_size=%s runtime_limit_seconds=%s", self.batch_size, self.max_runtime_seconds)
        self.reset_stale_results_once()
        total = self.repository.count_combinations()
        pending = self.repository.fetch_next_pending(self.batch_size, priority_timeframe=self.priority_timeframe)
        if not pending:
            self.logger.info("RESEARCH BATCH IDLE total_combinations=%s", total)
            self.log_progress_snapshot(total)
            return ResearchBatchResult(None, 0, "IDLE", "No pending research combinations")
        batch_id = self.repository.create_batch(self.batch_size, total)
        self.repository.mark_running([item.id for item in pending])
        processed = 0
        last_id: int | None = None
        for combination in pending:
            if time.monotonic() - started >= self.max_runtime_seconds:
                self.repository.mark_done(combination.id, "FAILED_RETRYABLE", "Runtime limit reached before processing")
                self.repository.finish_batch(batch_id, "PAUSED_RUNTIME_LIMIT", processed, last_id)
                return ResearchBatchResult(batch_id, processed, "PAUSED_RUNTIME_LIMIT", "Runtime limit reached")
            last_id = combination.id
            try:
                metrics = self.evaluate_combination(combination)
                self.repository.save_result(combination, metrics, batch_id)
                self.repository.mark_done(combination.id, "DONE")
                processed += 1
            except Exception as exc:
                self.repository.mark_done(combination.id, "FAILED_RETRYABLE", str(exc))
                self.logger.exception("Research combination failed id=%s: %s", combination.id, exc)
        self.repository.finish_batch(batch_id, "COMPLETED", processed, last_id)
        self.logger.info("RESEARCH BATCH COMPLETED batch_id=%s processed=%s total_combinations=%s last_combination_id=%s", batch_id, processed, total, last_id)
        self.log_progress_snapshot(total)
        self.event_bus.publish(Event(EventType.RESEARCH_COMPLETED, {"batch_id": batch_id, "processed": processed}))
        if sleep_after and self.sleep_between_batches_seconds > 0:
            time.sleep(self.sleep_between_batches_seconds)
        return ResearchBatchResult(batch_id, processed, "COMPLETED", f"Processed {processed} combinations")

    def reset_stale_results_once(self) -> None:
        if self._stale_results_reset:
            return
        reset_count = self.repository.reset_stale_results(VALIDATION_STATUS)
        if reset_count:
            self.logger.info("RESEARCH stale baseline combinations reset count=%s required_validation_status=%s", reset_count, VALIDATION_STATUS)
        self._stale_results_reset = True

    def log_progress_snapshot(self, total: int) -> None:
        counts = self.repository.fetch_progress_counts()
        done = counts.get("DONE", 0)
        pending = counts.get("PENDING", 0)
        retryable = counts.get("FAILED_RETRYABLE", 0)
        running = counts.get("RUNNING", 0)
        progress = (done / total * 100) if total else 0.0
        batches_remaining = (pending + retryable + running + self.batch_size - 1) // self.batch_size if self.batch_size else 0
        days_remaining = (batches_remaining * self.batch_interval_seconds) / 86400 if self.batch_interval_seconds else 0.0
        self.logger.info(
            "RESEARCH PROGRESS total=%s done=%s pending=%s retryable=%s running=%s progress=%.4f%% estimated_batches_remaining=%s estimated_days_remaining=%.2f",
            total,
            done,
            pending,
            retryable,
            running,
            progress,
            batches_remaining,
            days_remaining,
        )
        best = self.repository.fetch_best_result(timeframe=self.priority_timeframe) if self.priority_timeframe else self.repository.fetch_best_result()
        if best:
            self.logger.info(
                "RESEARCH BEST operational_timeframe=%s strategy=%s pair=%s timeframe=%s profit_factor=%.4f expectancy=%.6f net_profit=%.6f",
                self.priority_timeframe or "ANY",
                best["strategy"],
                best["pair"],
                best["timeframe"],
                best["profit_factor"],
                best["expectancy"],
                best["net_profit"],
            )

    def evaluate_combination(self, combination: ResearchCombination) -> dict[str, Any]:
        candles = self.repository.fetch_ohlc(combination.pair, combination.timeframe)
        if len(candles) < 200:
            return self.empty_metrics("INSUFFICIENT_OHLC_DATA", len(candles))

        chronological = list(reversed(candles))
        frame = self.build_feature_frame(chronological)
        reward_risk = float(combination.parameters.get("reward_risk", 1.5) or 1.5)
        atr_multiplier = float(combination.parameters.get("atr_multiplier", 1.0) or 1.0)
        horizon = max(1, self.probability_horizon_candles)
        signal_mask = self.build_signal_mask(frame, combination)
        signal_indexes = [int(index) for index in frame.index[signal_mask.fillna(False)].tolist() if int(index) < len(frame) - horizon]
        trade_results: list[float] = []
        favorable_excursions: list[float] = []
        adverse_excursions: list[float] = []

        for index in signal_indexes:
            row = frame.iloc[index]
            entry = float(row["close"])
            atr_value = float(row.get("atr14") or 0.0)
            if entry <= 0 or atr_value <= 0 or pd.isna(atr_value):
                continue
            stop_distance = max(atr_value * atr_multiplier, entry * 0.0001)
            target_distance = stop_distance * reward_risk
            stop = entry - stop_distance
            target = entry + target_distance
            future = chronological[index + 1:index + horizon + 1]
            if not future:
                continue
            exit_price = float(future[-1][4])

            for candle in future:
                high = float(candle[2])
                low = float(candle[3])
                hit_target = high >= target
                hit_stop = low <= stop
                if hit_stop and hit_target:
                    exit_price = stop
                    break
                if hit_stop:
                    exit_price = stop
                    break
                if hit_target:
                    exit_price = target
                    break

            favorable_excursions.append(max(float(row[2]) - entry for row in future))
            adverse_excursions.append(min(float(row[3]) - entry for row in future))
            trade_results.append(self._net_long_result(entry, exit_price))

        if not trade_results:
            return self.empty_metrics("NO_RESEARCH_TRADES", len(candles))

        wins = [value for value in trade_results if value > 0]
        losses = [value for value in trade_results if value < 0]
        gross_profit = sum(wins)
        gross_loss = abs(sum(losses))
        net_profit = sum(trade_results)
        expectancy = net_profit / len(trade_results)
        average_win = gross_profit / len(wins) if wins else 0.0
        average_loss = sum(losses) / len(losses) if losses else 0.0
        max_drawdown = self._max_drawdown(trade_results)
        recovery_factor = net_profit / max_drawdown if max_drawdown else 0.0

        return {
            "win_rate": len(wins) / len(trade_results) * 100,
            "profit_factor": gross_profit / gross_loss if gross_loss else gross_profit,
            "expectancy": expectancy,
            "sharpe": 0.0,
            "sortino": 0.0,
            "max_drawdown": max_drawdown,
            "net_profit": net_profit,
            "average_win": average_win,
            "average_loss": average_loss,
            "recovery_factor": recovery_factor,
            "ulcer_index": 0.0,
            "mfe": max(favorable_excursions) if favorable_excursions else 0.0,
            "mae": min(adverse_excursions) if adverse_excursions else 0.0,
            "calmar_ratio": recovery_factor,
            "validation": {
                "status": VALIDATION_STATUS,
                "candles": len(candles),
                "trades": len(trade_results),
                "horizon_candles": horizon,
                "reward_risk": reward_risk,
                "atr_multiplier": atr_multiplier,
                "trade_notional_eur": self.trade_notional_eur,
                "buy_fee_rate": self.buy_fee_rate,
                "sell_fee_rate": self.sell_fee_rate,
                "spread_rate": self.spread_rate,
                "slippage_rate": self.slippage_rate,
                "signals": len(signal_indexes),
                "strategy_filter": combination.strategy,
                "filters_applied": True,
            },
        }

    @staticmethod
    def build_feature_frame(chronological: list[tuple[Any, ...]]) -> pd.DataFrame:
        frame = pd.DataFrame(chronological, columns=["timestamp", "open", "high", "low", "close", "volume"])
        numeric_columns = ["open", "high", "low", "close", "volume"]
        frame[numeric_columns] = frame[numeric_columns].astype(float)
        return add_indicators(frame).reset_index(drop=True)

    def build_signal_mask(self, frame: pd.DataFrame, combination: ResearchCombination) -> pd.Series:
        if frame.empty:
            return pd.Series(False, index=frame.index)
        rsi_low, rsi_high = self.parse_rsi_range(str(combination.parameters.get("rsi_range", "0-100")))
        adx_min = float(combination.parameters.get("adx_min", 0.0) or 0.0)
        relative_volume_min = float(combination.parameters.get("relative_volume_min", 0.0) or 0.0)
        requested_regime = str(combination.parameters.get("market_regime", "ANY"))
        base = (
            frame["rsi14"].between(rsi_low, rsi_high)
            & (frame["adx14"] >= adx_min)
            & (frame["relative_volume"].fillna(0.0) >= relative_volume_min)
            & self.regime_mask(frame, requested_regime)
        )
        prior_high = frame["high"].rolling(20).max().shift(1)
        prior_low = frame["low"].rolling(20).min().shift(1)
        body = (frame["close"] - frame["open"]).abs()
        candle_range = (frame["high"] - frame["low"]).replace(0, pd.NA)
        atr_pct = (frame["atr14"] / frame["close"]).replace([float("inf"), float("-inf")], pd.NA)
        compression = atr_pct < atr_pct.rolling(100, min_periods=20).median() * 0.8
        rules = {
            "Breakout": frame["close"] > prior_high,
            "Breakout Retest": (frame["close"] > prior_high) & (frame["ema20"] >= frame["ema50"]),
            "Liquidity Sweep": (frame["low"] < prior_low) & (frame["close"] > prior_low),
            "Pullback Trend": (frame["low"] <= frame["ema20"]) & (frame["close"] > frame["ema20"]) & (frame["ema20"] > frame["ema50"]),
            "Range Reversal": (frame["close"] > frame["support"]) & (frame["adx14"] < 25),
            "Compression Breakout": compression & (frame["close"] > prior_high),
            "Momentum": (frame["macd_hist"] > 0) & (frame["close"] > frame["ema20"]),
            "Mean Reversion": (frame["rsi14"] <= rsi_high) & (frame["close"] <= frame["support"] * 1.01),
            "Trend Following": (frame["close"] > frame["ema200"]) & (frame["ema20"] > frame["ema50"]),
            "Volatility Expansion": (body / candle_range).fillna(0.0) > 0.6,
        }
        return (base & rules.get(combination.strategy, pd.Series(True, index=frame.index))).fillna(False)

    @staticmethod
    def parse_rsi_range(raw: str) -> tuple[float, float]:
        try:
            low, high = raw.split("-", maxsplit=1)
            return float(low), float(high)
        except ValueError:
            return 0.0, 100.0

    @staticmethod
    def regime_mask(frame: pd.DataFrame, regime: str) -> pd.Series:
        atr_pct = (frame["atr14"] / frame["close"]).replace([float("inf"), float("-inf")], pd.NA)
        atr_median = atr_pct.rolling(100, min_periods=20).median()
        trend_up = (frame["ema20"] > frame["ema50"]) & (frame["close"] > frame["ema200"]) & (frame["adx14"] >= 20)
        trend_down = (frame["ema20"] < frame["ema50"]) & (frame["close"] < frame["ema200"]) & (frame["adx14"] >= 20)
        masks = {
            "TREND_UP": trend_up,
            "TREND_DOWN": trend_down,
            "RANGE": frame["adx14"] < 20,
            "HIGH_VOLATILITY": atr_pct > atr_median * 1.25,
            "LOW_VOLATILITY": atr_pct < atr_median,
            "COMPRESSION": atr_pct < atr_median * 0.8,
            "BREAKOUT": frame["close"] > frame["high"].rolling(20).max().shift(1),
        }
        return masks.get(regime, pd.Series(True, index=frame.index)).fillna(False)

    def _net_long_result(self, entry: float, exit_price: float) -> float:
        half_spread = self.spread_rate / 2
        effective_entry = entry * (1 + half_spread + self.slippage_rate)
        effective_exit = exit_price * (1 - half_spread - self.slippage_rate)
        if effective_entry <= 0:
            return 0.0
        quantity = self.trade_notional_eur / effective_entry
        entry_notional = quantity * effective_entry
        exit_notional = quantity * effective_exit
        buy_fee = entry_notional * self.buy_fee_rate
        sell_fee = exit_notional * self.sell_fee_rate
        return exit_notional - entry_notional - buy_fee - sell_fee

    @staticmethod
    def empty_metrics(status: str, candles: int) -> dict[str, Any]:
        return {
            "win_rate": 0.0,
            "profit_factor": 0.0,
            "expectancy": 0.0,
            "sharpe": 0.0,
            "sortino": 0.0,
            "max_drawdown": 0.0,
            "net_profit": 0.0,
            "average_win": 0.0,
            "average_loss": 0.0,
            "recovery_factor": 0.0,
            "ulcer_index": 0.0,
            "mfe": 0.0,
            "mae": 0.0,
            "calmar_ratio": 0.0,
            "validation": {"status": status, "candles": candles},
        }

    @staticmethod
    def _max_drawdown(values: list[float]) -> float:
        equity = 0.0
        peak = 0.0
        max_drawdown = 0.0
        for value in values:
            equity += value
            peak = max(peak, equity)
            max_drawdown = max(max_drawdown, peak - equity)
        return max_drawdown
