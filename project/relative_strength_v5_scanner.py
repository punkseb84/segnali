"""Relative Strength V5 with robust volume and practical entry triggers.

V5.1 keeps the robust one-hour volume calculation, but removes the requirement for
near-perfect entries. Candidates can qualify through pullback restart, controlled
breakout, or confirmed momentum continuation. Risk guards and portfolio limits are
kept outside this scanner.
"""
from __future__ import annotations

from typing import Any

import pandas as pd

from project.relative_strength_scanner import RelativeStrengthScanner
from project.relative_strength_v3_scanner import RelativeStrengthV3Scanner
from project.robust_volume import add_robust_volume_features

RUNTIME_VERSION_V5 = "RELATIVE_STRENGTH_BALANCED_ROBUST_VOLUME_1H_15M_V5"
STRATEGY_VERSION_V5 = "RELATIVE_STRENGTH_BALANCED_ROBUST_VOLUME_V5_1"


class RelativeStrengthV5Scanner(RelativeStrengthV3Scanner):
    """Balanced RS scanner using robust volume and three practical entry setups."""

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
        frame = item["asset_15m"]
        audit = {
            "pair": str(item["pair"]),
            "direction": direction,
            "rank": rank,
            "universe_size": universe_size,
            "rs_score": round(float(item["rs_score"]), 4),
            "relative_4h_pct": round(float(item["relative_4h"]) * 100.0, 3),
            "relative_1h_pct": round(float(item["relative_1h"]) * 100.0, 3),
            "volume_ratio": round(float(item.get("volume_ratio") or 0.0), 3),
            "btc_4h_pct": round(float(item.get("btc_4h_return") or 0.0) * 100.0, 3),
            "decision": "EVALUATING",
            "reason": "",
            "setup": "NONE",
        }
        self.last_audit.append(audit)
        self._active_audit = audit
        try:
            if len(frame) < max(self.pullback_lookback, self.breakout_lookback) + 3:
                return self._reject("INSUFFICIENT_HISTORY")

            latest = frame.iloc[-1]
            previous = frame.iloc[-2]
            recent = frame.iloc[-1 - self.pullback_lookback : -1]
            breakout_window = frame.iloc[-1 - self.breakout_lookback : -1]

            entry = float(latest["close"])
            atr = float(latest["atr"])
            ema20 = float(latest["ema20"])
            previous_ema20 = float(previous["ema20"])
            volume_ratio = float(item.get("volume_ratio") or 0.0)
            rel_1h = float(item.get("relative_1h") or 0.0)
            ratio_momentum = float(item.get("ratio_momentum") or 0.0)
            audit.update({"entry": entry, "atr": atr, "ema20": ema20})

            if atr <= 0:
                return self._reject("ATR_INVALID")
            if bool(item.get("volume_data_valid")) and volume_ratio < self.volume_ratio_min:
                return self._reject("VOLUME_LOW")

            extension_atr = abs(entry - ema20) / atr
            audit["extension_atr"] = round(extension_atr, 3)
            if extension_atr > self.max_extension_atr:
                return self._reject("OVEREXTENDED")

            if direction == "LONG":
                if not bool(item.get("btc_bullish")):
                    return self._reject("BTC_REGIME")

                bullish = entry > float(latest["open"])
                pullback = bool(
                    (recent["low"] <= recent["ema20"] + 0.50 * recent["atr"]).any()
                )
                restart = bool(
                    bullish
                    and entry > ema20
                    and entry > float(previous["close"])
                    and ema20 >= previous_ema20
                )
                breakout_level = float(breakout_window["high"].max())
                breakout = bool(
                    bullish
                    and entry > ema20
                    and entry >= breakout_level - 0.10 * atr
                    and volume_ratio >= max(0.50, self.volume_ratio_min)
                )
                momentum = bool(
                    bullish
                    and entry > ema20
                    and ema20 >= previous_ema20
                    and entry > float(previous["close"])
                    and rel_1h > 0.0
                    and ratio_momentum > 0.0
                )
            else:
                if not bool(item.get("btc_bearish")):
                    return self._reject("BTC_REGIME")

                bearish = entry < float(latest["open"])
                pullback = bool(
                    (recent["high"] >= recent["ema20"] - 0.50 * recent["atr"]).any()
                )
                restart = bool(
                    bearish
                    and entry < ema20
                    and entry < float(previous["close"])
                    and ema20 <= previous_ema20
                )
                breakout_level = float(breakout_window["low"].min())
                breakout = bool(
                    bearish
                    and entry < ema20
                    and entry <= breakout_level + 0.10 * atr
                    and volume_ratio >= max(0.50, self.volume_ratio_min)
                )
                momentum = bool(
                    bearish
                    and entry < ema20
                    and ema20 <= previous_ema20
                    and entry < float(previous["close"])
                    and rel_1h < 0.0
                    and ratio_momentum < 0.0
                )

            audit.update(
                {
                    "pullback": pullback,
                    "restart": restart,
                    "breakout": breakout,
                    "momentum": momentum,
                }
            )
            setup = (
                "PULLBACK_RESTART"
                if pullback and restart
                else "CONTROLLED_BREAKOUT"
                if breakout
                else "MOMENTUM_CONTINUATION"
                if momentum
                else None
            )
            if setup is None:
                return self._reject("NO_ENTRY_TRIGGER")

            candidate = RelativeStrengthScanner._build_candidate(
                self, item, direction, rank, universe_size
            )
            if candidate is None:
                return self._reject("ECONOMICS_OR_BASE_FILTER")

            audit.update(
                {
                    "decision": "QUALIFIED",
                    "reason": "PASSED_ALL_FILTERS",
                    "setup": setup,
                    "net_rr": round(float(candidate.net_rr), 3),
                    "net_profit_eur": round(float(candidate.net_profit_tp1_eur), 4),
                    "net_loss_eur": round(float(candidate.net_loss_sl_eur), 4),
                }
            )
            candidate.score_breakdown.update(
                {
                    "runtime_version": RUNTIME_VERSION_V5,
                    "strategy_version": STRATEGY_VERSION_V5,
                    "setup_type": setup,
                    "btc_regime_confirmed": True,
                    "btc_4h_return": round(float(item.get("btc_4h_return") or 0.0), 8),
                    "extension_atr": round(extension_atr, 4),
                    "timestamps_aligned": True,
                    "volume_method": "LAST_4_CLOSED_15M_VS_SHIFTED_MEDIAN_1H_WINDOWS",
                    "volume_ratio": round(volume_ratio, 4),
                    "volume_ratio_15m": round(float(item.get("volume_ratio_15m") or 0.0), 4),
                    "volume_ratio_1h": round(float(item.get("volume_ratio_1h") or 0.0), 4),
                    "volume_data_valid": bool(item.get("volume_data_valid")),
                }
            )
            candidate.reasons.extend(
                [
                    "candele asset/BTC allineate per timestamp",
                    "regime BTC coerente con la direzione",
                    f"setup {setup}",
                    f"estensione {extension_atr:.2f} ATR",
                    f"volume robusto 1h={float(item.get('volume_ratio_1h') or 0.0):.2f}x",
                    f"volume ultima 15m={float(item.get('volume_ratio_15m') or 0.0):.2f}x",
                ]
            )
            return candidate
        finally:
            self._active_audit = None

    def evaluate(self):
        result = super().evaluate()
        ranked = list(getattr(self, "last_ranked", []) or [])
        if ranked:
            strongest = sorted(
                ranked,
                key=lambda value: abs(float(value.get("rs_score") or 0.0)),
                reverse=True,
            )[:10]
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
