"""Relative Strength V5 with robust one-hour volume confirmation.

V5 keeps the balanced V4 entry logic and replaces the fragile single-candle
volume veto with a closed one-hour volume window compared with a shifted median
baseline. The latest 15m ratio remains available for diagnostics only.
"""
from __future__ import annotations

from typing import Any

import pandas as pd

from project.relative_strength_v3_scanner import RelativeStrengthV3Scanner
from project.robust_volume import add_robust_volume_features

RUNTIME_VERSION_V5 = "RELATIVE_STRENGTH_BALANCED_ROBUST_VOLUME_1H_15M_V5"
STRATEGY_VERSION_V5 = "RELATIVE_STRENGTH_BALANCED_ROBUST_VOLUME_V5"


class RelativeStrengthV5Scanner(RelativeStrengthV3Scanner):
    """Balanced RS scanner using robust one-hour volume instead of one 15m bar."""

    def _indicators(self, frame: pd.DataFrame) -> pd.DataFrame:
        indicators = super()._indicators(frame)
        return add_robust_volume_features(indicators)

    def _rank_pair(
        self,
        pair: str,
        btc_15m: pd.DataFrame,
        btc_1h: pd.DataFrame,
    ) -> dict[str, Any] | None:
        item = super()._rank_pair(pair, btc_15m, btc_1h)
        if item is None:
            return None
        latest = item["asset_15m"].iloc[-1]
        item.update(
            {
                "volume_ratio": float(latest["volume_ratio"]),
                "volume_ratio_15m": float(latest["volume_ratio_15m"]),
                "volume_ratio_1h": float(latest["volume_ratio_1h"]),
                "volume_window_1h": float(latest["volume_window_1h"] or 0.0),
                "volume_baseline_1h": float(latest["volume_baseline_1h"] or 0.0),
                "volume_data_valid": bool(latest["volume_data_valid"]),
            }
        )
        return item

    def _build_candidate(
        self,
        item: dict[str, Any],
        direction: str,
        rank: int,
        universe_size: int,
    ):
        candidate = super()._build_candidate(item, direction, rank, universe_size)
        if candidate is None:
            return None
        candidate.score_breakdown.update(
            {
                "runtime_version": RUNTIME_VERSION_V5,
                "strategy_version": STRATEGY_VERSION_V5,
                "volume_method": "LAST_4_CLOSED_15M_VS_SHIFTED_MEDIAN_1H_WINDOWS",
                "volume_ratio": round(float(item.get("volume_ratio") or 0.0), 4),
                "volume_ratio_15m": round(float(item.get("volume_ratio_15m") or 0.0), 4),
                "volume_ratio_1h": round(float(item.get("volume_ratio_1h") or 0.0), 4),
                "volume_data_valid": bool(item.get("volume_data_valid")),
            }
        )
        candidate.reasons.extend(
            [
                f"volume robusto 1h={float(item.get('volume_ratio_1h') or 0.0):.2f}x",
                f"volume ultima 15m={float(item.get('volume_ratio_15m') or 0.0):.2f}x",
            ]
        )
        return candidate

    def evaluate(self):
        result = super().evaluate()
        ranked = list(getattr(self, "last_ranked", []) or [])
        if ranked:
            strongest = sorted(
                ranked,
                key=lambda value: abs(float(value.get("rs_score") or 0.0)),
                reverse=True,
            )[:6]
            diagnostics = [
                {
                    "pair": item.get("pair"),
                    "rs": round(float(item.get("rs_score") or 0.0), 4),
                    "vol15": round(float(item.get("volume_ratio_15m") or 0.0), 3),
                    "vol1h": round(float(item.get("volume_ratio_1h") or 0.0), 3),
                    "valid": bool(item.get("volume_data_valid")),
                }
                for item in strongest
            ]
            self.logger.info("RS_V5_VOLUME_AUDIT %s", diagnostics)
        return result
