"""V6.2 market-structure validation for daily LONG paper signals.

A technically valid LONG may not use a target beyond the first meaningful resistance.
When sufficient room exists, the target is capped below resistance. Otherwise the setup is
rejected and the daily scanner continues with the next ranked market.
"""
from __future__ import annotations

import math
from dataclasses import replace
from typing import Any

import pandas as pd

from project.daily_paper_scanner import DailyPaperSignalScanner
from project.strategy_engine.service import GeneratedSignal


STRUCTURE_RUNTIME_VERSION = "DAILY_PAPER_V6_2"


class StructureAwareDailyPaperScanner(DailyPaperSignalScanner):
    """Daily scanner that includes first-resistance room in every LONG decision."""

    def __init__(
        self,
        *args: Any,
        structure_min_net_rr: float = 1.20,
        structure_min_net_profit_eur: float = 0.20,
        resistance_lookback_15m: int = 120,
        resistance_lookback_1h: int = 120,
        resistance_buffer_atr: float = 0.10,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.structure_min_net_rr = max(1.05, float(structure_min_net_rr))
        self.structure_min_net_profit_eur = max(
            0.10, float(structure_min_net_profit_eur)
        )
        self.resistance_lookback_15m = max(40, int(resistance_lookback_15m))
        self.resistance_lookback_1h = max(40, int(resistance_lookback_1h))
        self.resistance_buffer_atr = max(0.05, float(resistance_buffer_atr))
        self._structure_hourly_frames: dict[str, pd.DataFrame] = {}

    def fetch_closed_frame(self, pair: str, timeframe: str, limit: int) -> pd.DataFrame:
        frame = super().fetch_closed_frame(pair, timeframe, limit)
        if timeframe == "1h":
            self._structure_hourly_frames[pair] = frame.copy()
        return frame

    @staticmethod
    def psychological_step(price: float) -> float:
        """Return a human-observed round-number interval appropriate for the price scale."""
        if price >= 10_000:
            return 500.0
        if price >= 1_000:
            return 50.0
        if price >= 100:
            return 5.0
        if price >= 10:
            return 0.50
        if price >= 1:
            return 0.05
        if price >= 0.10:
            return 0.005
        if price >= 0.01:
            return 0.0005
        return max(price * 0.005, 0.00000001)

    @staticmethod
    def _cluster_resistances(
        frame: pd.DataFrame,
        *,
        entry: float,
        tolerance: float,
        lookback: int,
        source: str,
    ) -> list[dict[str, Any]]:
        if frame.empty or len(frame) < 7:
            return []
        history = frame.tail(lookback).copy()
        if len(history) > 1:
            history = history.iloc[:-1]
        highs = pd.to_numeric(history["high"], errors="coerce").dropna()
        closes = pd.to_numeric(history["close"], errors="coerce").dropna()
        if len(highs) < 5:
            return []

        local_max = highs.rolling(5, center=True, min_periods=3).max()
        pivot_values = highs[(highs >= local_max - 1e-12) & (highs > entry)]
        raw_levels = sorted(float(value) for value in pivot_values)
        clusters: list[list[float]] = []
        for level in raw_levels:
            if not clusters or abs(level - sum(clusters[-1]) / len(clusters[-1])) > tolerance:
                clusters.append([level])
            else:
                clusters[-1].append(level)

        levels: list[dict[str, Any]] = []
        for cluster in clusters:
            level = float(sum(cluster) / len(cluster))
            high_touches = int(((highs - level).abs() <= tolerance).sum())
            close_touches = int(((closes - level).abs() <= tolerance).sum())
            touches = high_touches + min(close_touches, 2)
            if level > entry and touches >= 2:
                levels.append(
                    {
                        "level": level,
                        "touches": touches,
                        "source": source,
                    }
                )
        return levels

    def first_resistance(
        self,
        pair: str,
        frame_15m: pd.DataFrame,
        entry: float,
        atr: float,
    ) -> dict[str, Any] | None:
        tolerance_15m = max(atr * 0.22, entry * 0.0015)
        tolerance_1h = max(atr * 0.35, entry * 0.0020)
        levels = self._cluster_resistances(
            frame_15m,
            entry=entry,
            tolerance=tolerance_15m,
            lookback=self.resistance_lookback_15m,
            source="15m swing cluster",
        )
        hourly = self._structure_hourly_frames.get(pair)
        if hourly is not None and not hourly.empty:
            levels.extend(
                self._cluster_resistances(
                    hourly,
                    entry=entry,
                    tolerance=tolerance_1h,
                    lookback=self.resistance_lookback_1h,
                    source="1h swing cluster",
                )
            )

        step = self.psychological_step(entry)
        round_level = math.ceil((entry + step * 1e-6) / step) * step
        historical = frame_15m.tail(self.resistance_lookback_15m)
        round_touches = 0
        if not historical.empty:
            highs = pd.to_numeric(historical["high"], errors="coerce")
            closes = pd.to_numeric(historical["close"], errors="coerce")
            round_touches = int(((highs - round_level).abs() <= tolerance_15m).sum())
            round_touches += min(
                int(((closes - round_level).abs() <= tolerance_15m).sum()), 2
            )
        if round_level > entry and (
            round_touches >= 2 or round_level - entry <= atr * 0.75
        ):
            levels.append(
                {
                    "level": float(round_level),
                    "touches": max(1, round_touches),
                    "source": "round number",
                }
            )

        valid = [
            item
            for item in levels
            if float(item["level"]) > entry + max(entry * 0.0002, atr * 0.03)
        ]
        if not valid:
            return None
        nearest = min(valid, key=lambda item: float(item["level"]))
        nearest = dict(nearest)
        nearest["distance_pct"] = (
            (float(nearest["level"]) - entry) / entry * 100.0 if entry else 0.0
        )
        nearest["distance_atr"] = (
            (float(nearest["level"]) - entry) / atr if atr > 0 else 0.0
        )
        return nearest

    @staticmethod
    def _replace_target_economics(
        signal: GeneratedSignal,
        target: float,
        economics: dict[str, Any],
        breakdown: dict[str, Any],
        reasons: list[str],
    ) -> GeneratedSignal:
        return replace(
            signal,
            take_profit=target,
            effective_take_profit=target,
            tp_notional_eur=float(economics["tp_notional_eur"]),
            gross_profit_tp1_eur=float(economics["gross_profit_tp1_eur"]),
            estimated_buy_fee_eur=float(economics["estimated_buy_fee_eur"]),
            estimated_sell_fee_eur=float(economics["estimated_sell_fee_eur"]),
            estimated_sell_fee_sl_eur=float(economics["estimated_sell_fee_sl_eur"]),
            estimated_spread_cost_eur=float(economics["estimated_spread_cost_eur"]),
            estimated_slippage_cost_eur=float(economics["estimated_slippage_cost_eur"]),
            net_profit_tp1_eur=float(economics["net_profit_tp1_eur"]),
            net_loss_sl_eur=float(economics["net_loss_sl_eur"]),
            gross_rr=float(economics["gross_rr"]),
            net_rr=float(economics["net_rr"]),
            tp_atr_ratio=(
                (target - signal.entry) / signal.atr if signal.atr > 0 else 0.0
            ),
            score_breakdown=breakdown,
            reasons=reasons,
        )

    def _build_signal_for_assessment(
        self,
        pair: str,
        frame_15m: pd.DataFrame,
        regime: str,
        relative: dict[str, float],
        assessment: dict[str, Any],
    ) -> tuple[GeneratedSignal | None, str, dict[str, Any]]:
        signal, reason, diagnostic = super()._build_signal_for_assessment(
            pair, frame_15m, regime, relative, assessment
        )
        if signal is None:
            return signal, reason, diagnostic

        resistance = self.first_resistance(
            pair, frame_15m, float(signal.entry), float(signal.atr)
        )
        if resistance is None:
            breakdown = dict(signal.score_breakdown or {})
            breakdown.update(
                {
                    "structure_runtime_version": STRUCTURE_RUNTIME_VERSION,
                    "first_resistance_found": False,
                }
            )
            return replace(signal, score_breakdown=breakdown), reason, diagnostic

        resistance_level = float(resistance["level"])
        buffer = max(
            float(signal.atr) * self.resistance_buffer_atr,
            float(signal.entry) * 0.0005,
        )
        safe_target = resistance_level - buffer
        breakdown = dict(signal.score_breakdown or {})
        breakdown.update(
            {
                "structure_runtime_version": STRUCTURE_RUNTIME_VERSION,
                "first_resistance_found": True,
                "first_resistance": round(resistance_level, 10),
                "resistance_source": str(resistance["source"]),
                "resistance_touches": int(resistance["touches"]),
                "resistance_distance_pct": round(
                    float(resistance["distance_pct"]), 4
                ),
                "resistance_distance_atr": round(
                    float(resistance["distance_atr"]), 3
                ),
                "safe_target_below_resistance": round(safe_target, 10),
            }
        )

        if float(signal.take_profit) <= safe_target + 1e-12:
            reasons = list(signal.reasons or [])
            reasons.insert(
                0,
                f"spazio libero fino alla prima resistenza {resistance_level:.8f}",
            )
            return replace(
                signal, score_breakdown=breakdown, reasons=reasons
            ), reason, diagnostic

        if safe_target <= float(signal.entry):
            diagnostic["blockers"] = [
                f"prima resistenza {resistance_level:.8f} troppo vicina all'entry"
            ]
            diagnostic.update(breakdown)
            return None, "RESISTANCE_TOO_CLOSE", diagnostic

        safe_economics = self.calculate_net_economics(
            float(signal.entry), float(signal.stop_loss), safe_target
        )
        safe_net_rr = float(safe_economics.get("net_rr", 0.0) or 0.0)
        safe_profit = float(
            safe_economics.get("net_profit_tp1_eur", 0.0) or 0.0
        )
        breakdown.update(
            {
                "net_rr_to_first_resistance": round(safe_net_rr, 4),
                "net_profit_to_first_resistance_eur": round(safe_profit, 4),
            }
        )
        diagnostic.update(breakdown)

        if (
            not bool(safe_economics.get("tradable"))
            or safe_net_rr < self.structure_min_net_rr
            or safe_profit < self.structure_min_net_profit_eur
        ):
            diagnostic["blockers"] = [
                (
                    f"spazio insufficiente prima della resistenza {resistance_level:.8f}: "
                    f"R/R netto {safe_net_rr:.2f} < {self.structure_min_net_rr:.2f}"
                )
            ]
            self.logger.info(
                "STRUCTURE_REJECT pair=%s strategy=%s entry=%.8f target=%.8f "
                "resistance=%.8f safe_target=%.8f safe_net_rr=%.3f source=%s",
                pair,
                signal.strategy,
                signal.entry,
                signal.take_profit,
                resistance_level,
                safe_target,
                safe_net_rr,
                resistance["source"],
            )
            return None, "INSUFFICIENT_ROOM_TO_FIRST_RESISTANCE", diagnostic

        reasons = list(signal.reasons or [])
        reasons.insert(
            0,
            (
                f"target limitato a {safe_target:.8f}, sotto la prima resistenza "
                f"{resistance_level:.8f}"
            ),
        )
        breakdown["target_capped_by_resistance"] = True
        adjusted = self._replace_target_economics(
            signal, safe_target, safe_economics, breakdown, reasons
        )
        self.logger.info(
            "STRUCTURE_TARGET_CAPPED pair=%s strategy=%s original_target=%.8f "
            "effective_target=%.8f resistance=%.8f net_rr=%.3f",
            pair,
            signal.strategy,
            signal.take_profit,
            safe_target,
            resistance_level,
            adjusted.net_rr,
        )
        diagnostic["net_rr"] = round(adjusted.net_rr, 2)
        return adjusted, reason, diagnostic

    def evaluate(self) -> GeneratedSignal | None:
        self._structure_hourly_frames = {}
        result = super().evaluate()
        self._last_cycle_summary["structure_runtime_version"] = STRUCTURE_RUNTIME_VERSION
        self._last_cycle_summary["structure_min_net_rr"] = self.structure_min_net_rr
        self.logger.info(
            "STRUCTURE_AWARE_V6_2 runtime=%s min_rr_to_resistance=%.2f "
            "target_crossing_resistance=false fallback_next_candidate=true",
            STRUCTURE_RUNTIME_VERSION,
            self.structure_min_net_rr,
        )
        return result
