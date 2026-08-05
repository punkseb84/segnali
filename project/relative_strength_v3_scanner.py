"""Balanced relative-strength scanner with aligned data and auditable filters.

Uses BTC-relative ranking only for candidate selection. Entries require either:
1) pullback + restart, or 2) controlled breakout continuation.
"""
from __future__ import annotations

from collections import Counter
from typing import Any

import pandas as pd

from project.relative_strength_scanner import RelativeStrengthScanner

RUNTIME_VERSION_V3 = "RELATIVE_STRENGTH_BALANCED_1H_15M_V4"
STRATEGY_VERSION_V3 = "RELATIVE_STRENGTH_BALANCED_V4"


class RelativeStrengthV3Scanner(RelativeStrengthScanner):
    def __init__(
        self,
        *args: Any,
        max_extension_atr: float = 1.25,
        pullback_lookback: int = 8,
        breakout_lookback: int = 8,
        btc_regime_tolerance: float = 0.008,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.max_extension_atr = max(0.50, float(max_extension_atr))
        self.pullback_lookback = max(4, int(pullback_lookback))
        self.breakout_lookback = max(4, int(breakout_lookback))
        self.btc_regime_tolerance = max(0.0, float(btc_regime_tolerance))
        self.last_rejections: Counter[str] = Counter()
        self.last_ranked: list[dict[str, Any]] = []

    @staticmethod
    def _aligned(asset: pd.DataFrame, btc: pd.DataFrame) -> pd.DataFrame:
        left = asset[["timestamp", "open", "high", "low", "close", "volume"]].copy()
        right = btc[["timestamp", "close"]].rename(columns={"close": "btc_close"}).copy()
        return pd.merge(left, right, on="timestamp", how="inner").dropna().sort_values("timestamp").reset_index(drop=True)

    @staticmethod
    def _pct(series: pd.Series, periods: int) -> float:
        if len(series) <= periods:
            return 0.0
        before = float(series.iloc[-1 - periods])
        return 0.0 if before == 0 else float(series.iloc[-1] / before - 1.0)

    def _rank_pair(self, pair: str, btc_15m: pd.DataFrame, btc_1h: pd.DataFrame) -> dict[str, Any] | None:
        asset_15m = self._frame(pair, "15m", 180)
        asset_1h = self._frame(pair, "1h", 140)
        if len(asset_15m) < 50 or len(asset_1h) < 35:
            return None
        aligned_15 = self._aligned(asset_15m, btc_15m)
        aligned_1h = self._aligned(asset_1h, btc_1h)
        if len(aligned_15) < 40 or len(aligned_1h) < 25:
            return None

        indicators = self._indicators(asset_15m)
        latest = indicators.iloc[-1]
        volume_ratio = float(latest["volume_ratio"])
        if pd.isna(volume_ratio):
            volume_ratio = 0.0

        relative_15m = self._pct(aligned_15["close"], 1) - self._pct(aligned_15["btc_close"], 1)
        relative_1h = self._pct(aligned_15["close"], 4) - self._pct(aligned_15["btc_close"], 4)
        relative_4h = self._pct(aligned_1h["close"], 4) - self._pct(aligned_1h["btc_close"], 4)
        ratio = aligned_15["close"] / aligned_15["btc_close"]
        ratio_fast = ratio.ewm(span=5, adjust=False).mean()
        ratio_slow = ratio.ewm(span=20, adjust=False).mean()
        ratio_momentum = float(ratio_fast.iloc[-1] / ratio_slow.iloc[-1] - 1.0)

        btc_close = aligned_1h["btc_close"]
        btc_ema20 = btc_close.ewm(span=20, adjust=False).mean()
        btc_4h_return = self._pct(btc_close, 4)
        btc_bullish = bool(btc_close.iloc[-1] >= btc_ema20.iloc[-1] and btc_4h_return >= -self.btc_regime_tolerance)
        btc_bearish = bool(btc_close.iloc[-1] <= btc_ema20.iloc[-1] and btc_4h_return <= self.btc_regime_tolerance)

        rs_score = (
            0.50 * relative_4h * 100.0
            + 0.30 * relative_1h * 100.0
            + 0.15 * ratio_momentum * 100.0
            + 0.05 * relative_15m * 100.0
        )
        return {
            "pair": pair,
            "asset_15m": indicators,
            "rs_score": rs_score,
            "relative_4h": relative_4h,
            "relative_1h": relative_1h,
            "relative_15m": relative_15m,
            "ratio_momentum": ratio_momentum,
            "volume_ratio": volume_ratio,
            "btc_bullish": btc_bullish,
            "btc_bearish": btc_bearish,
            "btc_4h_return": btc_4h_return,
        }

    def _reject(self, reason: str):
        self.last_rejections[reason] += 1
        return None

    def _build_candidate(self, item: dict[str, Any], direction: str, rank: int, universe_size: int):
        frame = item["asset_15m"]
        if len(frame) < max(self.pullback_lookback, self.breakout_lookback) + 3:
            return self._reject("INSUFFICIENT_HISTORY")
        latest, previous = frame.iloc[-1], frame.iloc[-2]
        recent = frame.iloc[-1 - self.pullback_lookback : -1]
        breakout_window = frame.iloc[-1 - self.breakout_lookback : -1]
        entry, atr, ema20 = float(latest["close"]), float(latest["atr"]), float(latest["ema20"])
        previous_ema20 = float(previous["ema20"])
        volume_ratio = float(item.get("volume_ratio") or 0.0)
        if atr <= 0:
            return self._reject("ATR_INVALID")
        if volume_ratio < self.volume_ratio_min:
            return self._reject("VOLUME_LOW")

        extension_atr = abs(entry - ema20) / atr
        if extension_atr > self.max_extension_atr:
            return self._reject("OVEREXTENDED")

        if direction == "LONG":
            if not bool(item.get("btc_bullish")):
                return self._reject("BTC_REGIME")
            pullback = bool((recent["low"] <= recent["ema20"] + 0.30 * recent["atr"]).any())
            restart = bool(entry > float(latest["open"]) and entry > ema20 and entry > float(previous["high"]) and ema20 >= previous_ema20)
            breakout = bool(entry > float(breakout_window["high"].max()) and entry > ema20 and volume_ratio >= max(0.90, self.volume_ratio_min))
        else:
            if not bool(item.get("btc_bearish")):
                return self._reject("BTC_REGIME")
            pullback = bool((recent["high"] >= recent["ema20"] - 0.30 * recent["atr"]).any())
            restart = bool(entry < float(latest["open"]) and entry < ema20 and entry < float(previous["low"]) and ema20 <= previous_ema20)
            breakout = bool(entry < float(breakout_window["low"].min()) and entry < ema20 and volume_ratio >= max(0.90, self.volume_ratio_min))

        setup = "PULLBACK_RESTART" if pullback and restart else "CONTROLLED_BREAKOUT" if breakout else None
        if setup is None:
            return self._reject("NO_ENTRY_TRIGGER")

        candidate = RelativeStrengthScanner._build_candidate(self, item, direction, rank, universe_size)
        if candidate is None:
            return self._reject("ECONOMICS_OR_BASE_FILTER")
        candidate.score_breakdown.update({
            "runtime_version": RUNTIME_VERSION_V3,
            "strategy_version": STRATEGY_VERSION_V3,
            "setup_type": setup,
            "btc_regime_confirmed": True,
            "btc_4h_return": round(float(item.get("btc_4h_return") or 0.0), 8),
            "extension_atr": round(extension_atr, 4),
            "timestamps_aligned": True,
        })
        candidate.reasons.extend([
            "candele asset/BTC allineate per timestamp",
            "regime BTC coerente con la direzione",
            f"setup {setup}",
            f"estensione {extension_atr:.2f} ATR",
        ])
        return candidate

    def evaluate(self):
        self.last_rejections = Counter()
        result = super().evaluate()
        self.logger.info("RS_V4_DIAGNOSTIC rejections=%s", dict(self.last_rejections))
        return result
