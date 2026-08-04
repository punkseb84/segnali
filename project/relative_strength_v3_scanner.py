"""Corrected relative-strength scanner.

V3 fixes the main weaknesses of V1/V2:
- aligns asset and BTC candles by timestamp;
- uses BTC absolute regime as a direction guard;
- uses relative strength only to select candidates;
- requires a pullback and a fresh restart instead of chasing extension;
- rejects entries too far from EMA20;
- allocates a coherent per-position notional from a fixed portfolio budget.
"""
from __future__ import annotations

from typing import Any

import pandas as pd

from project.relative_strength_scanner import RelativeStrengthScanner

RUNTIME_VERSION_V3 = "RELATIVE_STRENGTH_PULLBACK_1H_15M_V3"
STRATEGY_VERSION_V3 = "RELATIVE_STRENGTH_PULLBACK_V3"


class RelativeStrengthV3Scanner(RelativeStrengthScanner):
    def __init__(
        self,
        *args: Any,
        max_extension_atr: float = 0.75,
        pullback_lookback: int = 6,
        btc_regime_tolerance: float = 0.005,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.max_extension_atr = max(0.25, float(max_extension_atr))
        self.pullback_lookback = max(3, int(pullback_lookback))
        self.btc_regime_tolerance = max(0.0, float(btc_regime_tolerance))

    @staticmethod
    def _aligned(asset: pd.DataFrame, btc: pd.DataFrame) -> pd.DataFrame:
        left = asset[["timestamp", "open", "high", "low", "close", "volume"]].copy()
        right = btc[["timestamp", "close"]].rename(columns={"close": "btc_close"}).copy()
        merged = pd.merge(left, right, on="timestamp", how="inner").sort_values("timestamp")
        return merged.dropna().reset_index(drop=True)

    @staticmethod
    def _pct(series: pd.Series, periods: int) -> float:
        if len(series) <= periods:
            return 0.0
        before = float(series.iloc[-1 - periods])
        return 0.0 if before == 0 else float(series.iloc[-1] / before - 1.0)

    def _rank_pair(
        self,
        pair: str,
        btc_15m: pd.DataFrame,
        btc_1h: pd.DataFrame,
    ) -> dict[str, Any] | None:
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
        btc_bullish = bool(
            btc_close.iloc[-1] >= btc_ema20.iloc[-1]
            and btc_4h_return >= -self.btc_regime_tolerance
        )
        btc_bearish = bool(
            btc_close.iloc[-1] <= btc_ema20.iloc[-1]
            and btc_4h_return <= self.btc_regime_tolerance
        )

        # Avoid counting the same short-term move several times. Long-horizon
        # relative strength dominates; 15m is only a small freshness component.
        rs_score = (
            0.55 * relative_4h * 100.0
            + 0.30 * relative_1h * 100.0
            + 0.10 * ratio_momentum * 100.0
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

    def _build_candidate(
        self,
        item: dict[str, Any],
        direction: str,
        rank: int,
        universe_size: int,
    ):
        frame = item["asset_15m"]
        if len(frame) < self.pullback_lookback + 3:
            return None
        latest = frame.iloc[-1]
        previous = frame.iloc[-2]
        recent = frame.iloc[-1 - self.pullback_lookback : -1]

        entry = float(latest["close"])
        atr = float(latest["atr"])
        ema20 = float(latest["ema20"])
        previous_ema20 = float(previous["ema20"])
        if atr <= 0 or float(item.get("volume_ratio") or 0.0) < self.volume_ratio_min:
            return None

        if direction == "LONG":
            if not bool(item.get("btc_bullish")):
                return None
            pulled_back = bool((recent["low"] <= recent["ema20"] + 0.20 * recent["atr"]).any())
            restart = bool(
                float(latest["close"]) > float(latest["open"])
                and entry > ema20
                and entry > float(previous["high"])
                and ema20 >= previous_ema20
            )
        else:
            if not bool(item.get("btc_bearish")):
                return None
            pulled_back = bool((recent["high"] >= recent["ema20"] - 0.20 * recent["atr"]).any())
            restart = bool(
                float(latest["close"]) < float(latest["open"])
                and entry < ema20
                and entry < float(previous["low"])
                and ema20 <= previous_ema20
            )

        extension_atr = abs(entry - ema20) / atr
        if not pulled_back or not restart or extension_atr > self.max_extension_atr:
            return None

        candidate = super()._build_candidate(item, direction, rank, universe_size)
        if candidate is None:
            return None
        candidate.score_breakdown.update(
            {
                "runtime_version": RUNTIME_VERSION_V3,
                "strategy_version": STRATEGY_VERSION_V3,
                "btc_regime_confirmed": True,
                "btc_4h_return": round(float(item.get("btc_4h_return") or 0.0), 8),
                "pullback_confirmed": True,
                "restart_confirmed": True,
                "extension_atr": round(extension_atr, 4),
                "timestamps_aligned": True,
            }
        )
        candidate.reasons.extend(
            [
                "candele asset/BTC allineate per timestamp",
                "regime assoluto BTC coerente con la direzione",
                "pullback verso EMA20 seguito da ripartenza",
                f"estensione contenuta a {extension_atr:.2f} ATR",
            ]
        )
        return candidate
