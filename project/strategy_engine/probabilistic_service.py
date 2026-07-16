"""Probabilistic operational signal engine.

This layer keeps the existing persistence, economics and notification pipeline, but
replaces all-or-nothing quality decisions with weighted evidence. Only invalid
market data, non-tradable orders and non-positive net targets remain hard stops.
"""
from __future__ import annotations

from typing import Any

import pandas as pd

from indicators import add_indicators
from project.strategy_engine.service import StrategyEngine


class ProbabilisticStrategyEngine(StrategyEngine):
    """Score the current setup and soften statistical vetoes.

    Research metrics describe a strategy candidate; they must not become a rigid
    requirement that blocks every current setup. The live score therefore combines
    pattern, momentum, trend, volume, volatility regime and historical evidence.
    """

    def __init__(
        self,
        *args: Any,
        hard_block_negative_historical_ev: bool = False,
        min_live_setup_score: float = 45.0,
        min_operative_signal_score: float = 55.0,
        min_watchlist_signal_score: float = 30.0,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.hard_block_negative_historical_ev = hard_block_negative_historical_ev
        self.min_live_setup_score = max(0.0, min(100.0, min_live_setup_score))
        self.min_operative_signal_score = max(0.0, min(100.0, min_operative_signal_score))
        self.min_watchlist_signal_score = max(0.0, min(100.0, min_watchlist_signal_score))

    def fetch_strategy_candidates(self, limit: int = 10) -> list[dict[str, Any]]:
        candidates = super().fetch_strategy_candidates(limit)
        price_cache: dict[tuple[str, str], list[tuple[Any, ...]]] = {}
        enriched: list[dict[str, Any]] = []
        for candidate in candidates:
            key = (str(candidate["pair"]), str(candidate["timeframe"]))
            if key not in price_cache:
                price_cache[key] = self.research_repository.fetch_ohlc(
                    key[0], key[1], limit=self.ohlc_limit
                )
            live_context = self.score_live_setup(price_cache[key], candidate)
            item = dict(candidate)
            item["live_setup_score"] = live_context["score"]
            item["live_setup_breakdown"] = live_context["breakdown"]
            item["live_setup_regime"] = live_context["regime"]
            enriched.append(item)
        return sorted(
            enriched,
            key=lambda item: (
                float(item.get("live_setup_score") or 0.0),
                float(item.get("profit_factor") or 0.0),
                float(item.get("expectancy") or 0.0),
            ),
            reverse=True,
        )

    def evaluate_historical_ev_gate(
        self,
        historical_expected_value: float | None,
        probability_context: dict[str, Any],
    ) -> dict[str, Any]:
        decision = super().evaluate_historical_ev_gate(
            historical_expected_value, probability_context
        )
        if self.hard_block_negative_historical_ev or not decision["block"]:
            return decision
        return {
            "block": False,
            "reason": f"soft_{decision['reason']}",
        }

    def build_score_breakdown(
        self,
        best: dict[str, Any],
        economics: dict[str, float | bool],
        validation: dict[str, str],
        probability_context: dict[str, Any],
        regime_aligned: bool,
        ev_decision: dict[str, Any],
    ) -> dict[str, float]:
        base = super().build_score_breakdown(
            best,
            economics,
            validation,
            probability_context,
            regime_aligned,
            ev_decision,
        )
        # Preserve the relative meaning of the existing score while reserving 20%
        # for what is happening now on the operational timeframe.
        scaled = {name: round(value * 0.80, 4) for name, value in base.items()}
        live_score = float(best.get("live_setup_score") or 0.0)
        scaled["live_setup_score"] = round(live_score * 0.20, 4)
        return scaled

    def classify_signal(
        self,
        best: dict[str, Any],
        economics: dict[str, float | bool],
        validation: dict[str, str],
        probability_context: dict[str, Any],
        regime_aligned: bool,
        ev_decision: dict[str, Any] | None = None,
        score: float = 0.0,
    ) -> str:
        """Classify from accumulated evidence rather than an AND-chain."""
        profit_factor = float(best.get("profit_factor") or 0.0)
        expectancy = float(best.get("expectancy") or 0.0)
        live_score = float(best.get("live_setup_score") or 0.0)
        net_profit = float(economics.get("net_profit_tp1_eur") or 0.0)
        net_rr = float(economics.get("net_rr") or 0.0)
        historical_ev = self.calculate_historical_expected_value(
            economics, probability_context
        )

        evidence = {
            "profit_factor_positive": profit_factor > 1.0,
            "expectancy_positive": expectancy > 0.0,
            "historical_ev_non_negative": historical_ev is not None
            and historical_ev >= self.min_historical_ev_eur,
            "regime_aligned": regime_aligned,
            "clean_validation": validation.get("status") == "PASSED",
            "acceptable_net_rr": net_rr >= max(0.75, self.min_net_rr * 0.80),
        }
        positive_votes = sum(1 for value in evidence.values() if value)

        if live_score < self.min_watchlist_signal_score or score < self.min_watchlist_signal_score:
            # Class D is intentionally unsupported by the base engine and is
            # therefore rejected without being stored or notified.
            return "D"

        if (
            score >= 75.0
            and live_score >= 70.0
            and positive_votes >= 5
            and profit_factor >= 1.15
            and net_profit > 0.0
        ):
            return "A"

        if (
            score >= self.min_operative_signal_score
            and live_score >= self.min_live_setup_score
            and positive_votes >= 3
            and net_profit > 0.0
        ):
            return "B"

        return "C"

    def score_live_setup(
        self,
        prices: list[tuple[Any, ...]],
        candidate: dict[str, Any],
    ) -> dict[str, Any]:
        if len(prices) < 60:
            return {
                "score": 0.0,
                "regime": "INSUFFICIENT_DATA",
                "breakdown": {"data_quality": 0.0},
            }

        frame = pd.DataFrame(
            list(reversed(prices)),
            columns=["timestamp", "open", "high", "low", "close", "volume"],
        )
        numeric = ["open", "high", "low", "close", "volume"]
        frame[numeric] = frame[numeric].apply(pd.to_numeric, errors="coerce")
        frame = add_indicators(frame).reset_index(drop=True)
        latest = frame.iloc[-1]
        previous = frame.iloc[-2]
        prior_window = frame.iloc[-21:-1]
        required = [
            "close",
            "open",
            "high",
            "low",
            "volume",
            "ema20",
            "ema50",
            "ema200",
            "rsi14",
            "macd_hist",
            "atr14",
            "adx14",
            "relative_volume",
            "support",
        ]
        if prior_window.empty or any(pd.isna(latest.get(name)) for name in required):
            return {
                "score": 0.0,
                "regime": "INDICATORS_NOT_READY",
                "breakdown": {"data_quality": 0.0},
            }

        parameters = dict(candidate.get("parameters") or {})
        rsi_low, rsi_high = self._parse_rsi_range(
            str(parameters.get("rsi_range", "0-100"))
        )
        adx_min = float(parameters.get("adx_min", 0.0) or 0.0)
        relative_volume_min = float(
            parameters.get("relative_volume_min", 0.0) or 0.0
        )
        requested_regime = str(parameters.get("market_regime", "ANY"))

        close = float(latest["close"])
        rsi = float(latest["rsi14"])
        adx_value = float(latest["adx14"])
        relative_volume = float(latest["relative_volume"])
        prior_high = float(prior_window["high"].max())
        prior_low = float(prior_window["low"].min())
        actual_regime = self._infer_regime(frame, prior_high)

        breakdown: dict[str, float] = {"data_quality": 5.0}

        if rsi_low <= rsi <= rsi_high:
            breakdown["rsi_fit"] = 15.0
        else:
            distance = min(abs(rsi - rsi_low), abs(rsi - rsi_high))
            breakdown["rsi_fit"] = round(max(0.0, 8.0 * (1.0 - distance / 20.0)), 4)

        breakdown["adx_strength"] = 10.0 if adx_min <= 0 else round(
            min(10.0, max(0.0, adx_value / adx_min * 10.0)), 4
        )
        breakdown["relative_volume"] = 10.0 if relative_volume_min <= 0 else round(
            min(10.0, max(0.0, relative_volume / relative_volume_min * 10.0)), 4
        )

        trend_points = 0.0
        if close > float(latest["ema200"]):
            trend_points += 5.0
        if float(latest["ema20"]) > float(latest["ema50"]):
            trend_points += 5.0
        breakdown["trend"] = trend_points

        macd_now = float(latest["macd_hist"])
        macd_previous = float(previous["macd_hist"])
        momentum_points = 0.0
        if macd_now > 0:
            momentum_points += 5.0
        if macd_now > macd_previous:
            momentum_points += 5.0
        breakdown["momentum"] = momentum_points

        breakdown["regime_fit"] = self._regime_points(
            requested_regime, actual_regime
        )
        breakdown["pattern"] = self._pattern_points(
            str(candidate.get("strategy") or ""),
            frame,
            prior_high,
            prior_low,
        )

        score = round(min(100.0, sum(breakdown.values())), 4)
        return {
            "score": score,
            "regime": actual_regime,
            "breakdown": breakdown,
        }

    @staticmethod
    def _parse_rsi_range(raw: str) -> tuple[float, float]:
        try:
            low, high = raw.split("-", maxsplit=1)
            return float(low), float(high)
        except (TypeError, ValueError):
            return 0.0, 100.0

    @staticmethod
    def _infer_regime(frame: pd.DataFrame, prior_high: float) -> str:
        latest = frame.iloc[-1]
        close = float(latest["close"])
        atr_pct = float(latest["atr14"]) / close if close else 0.0
        atr_series = (frame["atr14"] / frame["close"]).dropna().tail(100)
        atr_median = float(atr_series.median()) if not atr_series.empty else atr_pct
        if close > prior_high:
            return "BREAKOUT"
        if atr_median > 0 and atr_pct > atr_median * 1.25:
            return "HIGH_VOLATILITY"
        if atr_median > 0 and atr_pct < atr_median * 0.80:
            return "COMPRESSION"
        if (
            float(latest["ema20"]) > float(latest["ema50"])
            and close > float(latest["ema200"])
            and float(latest["adx14"]) >= 20
        ):
            return "TREND_UP"
        if (
            float(latest["ema20"]) < float(latest["ema50"])
            and close < float(latest["ema200"])
            and float(latest["adx14"]) >= 20
        ):
            return "TREND_DOWN"
        if float(latest["adx14"]) < 20:
            return "RANGE"
        return "LOW_VOLATILITY"

    @staticmethod
    def _regime_points(requested: str, actual: str) -> float:
        if requested in {"", "ANY"} or requested == actual:
            return 10.0
        compatible = {
            ("COMPRESSION", "LOW_VOLATILITY"),
            ("LOW_VOLATILITY", "COMPRESSION"),
            ("BREAKOUT", "HIGH_VOLATILITY"),
            ("HIGH_VOLATILITY", "BREAKOUT"),
        }
        return 5.0 if (requested, actual) in compatible else 0.0

    @staticmethod
    def _pattern_points(
        strategy: str,
        frame: pd.DataFrame,
        prior_high: float,
        prior_low: float,
    ) -> float:
        latest = frame.iloc[-1]
        close = float(latest["close"])
        open_price = float(latest["open"])
        high = float(latest["high"])
        low = float(latest["low"])
        ema20 = float(latest["ema20"])
        ema50 = float(latest["ema50"])
        ema200 = float(latest["ema200"])
        rsi = float(latest["rsi14"])
        adx_value = float(latest["adx14"])
        macd_hist = float(latest["macd_hist"])
        support = float(latest["support"])
        candle_range = max(high - low, 1e-12)
        body_ratio = abs(close - open_price) / candle_range
        distance_to_high = abs(close - prior_high) / close if close else 1.0
        distance_to_low = abs(close - prior_low) / close if close else 1.0
        distance_to_ema20 = abs(close - ema20) / close if close else 1.0

        if strategy == "Breakout":
            if close > prior_high:
                return 35.0
            return 20.0 if distance_to_high <= 0.005 else 8.0 if distance_to_high <= 0.01 else 0.0
        if strategy == "Breakout Retest":
            if close >= prior_high * 0.995 and ema20 >= ema50:
                return 35.0
            return 18.0 if distance_to_high <= 0.01 and ema20 >= ema50 else 0.0
        if strategy == "Liquidity Sweep":
            if low < prior_low and close > prior_low:
                return 35.0
            return 15.0 if distance_to_low <= 0.01 else 0.0
        if strategy == "Pullback Trend":
            if low <= ema20 and close > ema20 and ema20 > ema50:
                return 35.0
            return 18.0 if distance_to_ema20 <= 0.01 and ema20 > ema50 else 0.0
        if strategy == "Range Reversal":
            if close <= support * 1.015 and close > support and adx_value < 25:
                return 35.0
            return 15.0 if close <= support * 1.025 and adx_value < 30 else 0.0
        if strategy == "Compression Breakout":
            atr_pct = frame["atr14"] / frame["close"]
            atr_median = float(atr_pct.dropna().tail(100).median())
            compressed = atr_median > 0 and float(latest["atr14"]) / close < atr_median * 0.8
            if compressed and close > prior_high:
                return 35.0
            return 20.0 if compressed and distance_to_high <= 0.01 else 0.0
        if strategy == "Momentum":
            if macd_hist > 0 and close > ema20:
                return 35.0
            return 18.0 if macd_hist > 0 or close > ema20 else 0.0
        if strategy == "Mean Reversion":
            if close <= support * 1.01 and rsi <= 50:
                return 35.0
            return 15.0 if close <= support * 1.02 or rsi <= 45 else 0.0
        if strategy == "Trend Following":
            if close > ema200 and ema20 > ema50:
                return 35.0
            return 15.0 if close > ema200 or ema20 > ema50 else 0.0
        if strategy == "Volatility Expansion":
            return round(min(35.0, body_ratio / 0.6 * 35.0), 4)
        return 0.0
