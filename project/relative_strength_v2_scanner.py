"""Relative-strength V2 scanner with a true market-wide ranking.

All sufficiently populated assets enter the ranking. Liquidity/volume filters are
applied only after rank assignment, so Telegram reports the real universe size.
"""
from __future__ import annotations

from typing import Any

import pandas as pd

from project.relative_strength_scanner import RelativeStrengthScanner


class RelativeStrengthV2Scanner(RelativeStrengthScanner):
    def _rank_pair(
        self,
        pair: str,
        btc_15m: pd.DataFrame,
        btc_1h: pd.DataFrame,
    ) -> dict[str, Any] | None:
        asset_15m = self._frame(pair, "15m", 160)
        asset_1h = self._frame(pair, "1h", 120)
        if len(asset_15m) < 40 or len(asset_1h) < 30:
            return None

        a15, b15 = self._indicators(asset_15m), self._indicators(btc_15m)
        a1, b1 = self._indicators(asset_1h), self._indicators(btc_1h)
        latest = a15.iloc[-1]
        volume_ratio = float(latest["volume_ratio"])
        if pd.isna(volume_ratio):
            volume_ratio = 0.0

        ret_1h = self._ret(a15, 4) - self._ret(b15, 4)
        ret_4h = self._ret(a1, 4) - self._ret(b1, 4)
        ret_15m = self._ret(a15, 1) - self._ret(b15, 1)
        ratio = (
            a15["close"].tail(40).reset_index(drop=True)
            / b15["close"].tail(40).reset_index(drop=True)
        )
        ratio_momentum = (
            float(ratio.iloc[-1] / ratio.iloc[-5] - 1.0)
            if len(ratio) >= 5
            else 0.0
        )
        trend_component = float(latest["close"]) / float(latest["ema20"]) - 1.0
        rs_score = (
            0.35 * ret_4h * 100.0
            + 0.25 * ret_1h * 100.0
            + 0.15 * ret_15m * 100.0
            + 0.15 * ratio_momentum * 100.0
            + 0.05 * (volume_ratio - 1.0)
            + 0.05 * trend_component * 100.0
        )
        return {
            "pair": pair,
            "asset_15m": a15,
            "rs_score": rs_score,
            "relative_4h": ret_4h,
            "relative_1h": ret_1h,
            "relative_15m": ret_15m,
            "ratio_momentum": ratio_momentum,
            "volume_ratio": volume_ratio,
        }

    def _build_candidate(
        self,
        item: dict[str, Any],
        direction: str,
        rank: int,
        universe_size: int,
    ):
        # Volume is an execution filter, not a ranking-universe filter.
        if float(item.get("volume_ratio") or 0.0) < self.volume_ratio_min:
            return None
        return super()._build_candidate(item, direction, rank, universe_size)
