"""Eighth LONG strategy inspired by Tori Trades' public trend-line framework.

The public method is translated into deterministic rules suitable for paper validation:
- aggregate closed 1h candles into closed 4h candles;
- require a descending action line with at least three separated touches;
- require a confirmed 4h close above that line;
- derive the stop from an opposing ascending safety line;
- reject extended breaks and targets that lack 2R room before resistance.

This is an independent implementation for testing, not an endorsement or exact copy of
private course material.
"""
from __future__ import annotations

from dataclasses import replace
from typing import Any

import numpy as np
import pandas as pd

import project.audited_long_scanner as audited_module
import project.capital_protection_scanner as capital_module
import project.daily_paper_scanner as daily_module
import project.harmonic_live_scanner as harmonic_module
import project.validated_report_scanner as validated_report_module
from project.harmonic_live_scanner import LIVE_LONG_STRATEGIES
from project.strategy_engine.service import GeneratedSignal
from project.structure_aware_daily_scanner import StructureAwareDailyPaperScanner


TORI_TRENDLINE_STRATEGY = "Three-Touch Trendline Break"
EIGHT_LONG_STRATEGIES = (*LIVE_LONG_STRATEGIES, TORI_TRENDLINE_STRATEGY)
TORI_RUNTIME_VERSION = "TORI_TRENDLINE_V1"

# Runtime modules use imported tuples while building states/reports. Keep the eighth strategy
# visible without changing the legacy six/seven-strategy classes used by isolated unit tests.
audited_module.ENHANCED_LONG_STRATEGIES = EIGHT_LONG_STRATEGIES
harmonic_module.LIVE_LONG_STRATEGIES = EIGHT_LONG_STRATEGIES
capital_module.LIVE_LONG_STRATEGIES = EIGHT_LONG_STRATEGIES
daily_module.LIVE_LONG_STRATEGIES = EIGHT_LONG_STRATEGIES
validated_report_module.LIVE_LONG_STRATEGIES = EIGHT_LONG_STRATEGIES


class ToriTrendlineDailyPaperScanner(StructureAwareDailyPaperScanner):
    """Structure-aware daily scanner with a strict 4h three-touch trendline setup."""

    def __init__(
        self,
        *args: Any,
        tori_min_touch_separation_bars: int = 3,
        tori_touch_tolerance_atr: float = 0.28,
        tori_break_buffer_atr: float = 0.10,
        tori_max_extension_atr: float = 0.80,
        tori_min_gross_rr: float = 2.0,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.tori_min_touch_separation_bars = max(2, int(tori_min_touch_separation_bars))
        self.tori_touch_tolerance_atr = max(0.12, float(tori_touch_tolerance_atr))
        self.tori_break_buffer_atr = max(0.03, float(tori_break_buffer_atr))
        self.tori_max_extension_atr = max(0.30, float(tori_max_extension_atr))
        self.tori_min_gross_rr = max(1.5, float(tori_min_gross_rr))
        self._tori_assessment: dict[str, Any] | None = None

    @staticmethod
    def aggregate_closed_4h(frame_1h: pd.DataFrame) -> pd.DataFrame:
        """Aggregate already-closed UTC 1h candles into deterministic UTC 4h candles."""
        if frame_1h.empty:
            return frame_1h.copy()
        data = frame_1h.copy()
        data["timestamp"] = pd.to_datetime(data["timestamp"], utc=True)
        data = data.sort_values("timestamp").set_index("timestamp")
        result = data.resample("4h", label="left", closed="left").agg(
            {
                "open": "first",
                "high": "max",
                "low": "min",
                "close": "last",
                "volume": "sum",
            }
        )
        result = result.dropna(subset=["open", "high", "low", "close"])
        counts = data["close"].resample("4h", label="left", closed="left").count()
        result = result[counts.reindex(result.index).fillna(0) >= 4]
        return result.reset_index()

    @staticmethod
    def _atr(frame: pd.DataFrame, period: int = 14) -> pd.Series:
        previous_close = frame["close"].shift(1)
        true_range = pd.concat(
            [
                frame["high"] - frame["low"],
                (frame["high"] - previous_close).abs(),
                (frame["low"] - previous_close).abs(),
            ],
            axis=1,
        ).max(axis=1)
        return true_range.ewm(alpha=1 / period, adjust=False).mean()

    @staticmethod
    def _pivot_indices(series: pd.Series, *, kind: str, window: int = 2) -> list[int]:
        values = pd.to_numeric(series, errors="coerce").to_numpy(dtype=float)
        indices: list[int] = []
        for index in range(window, len(values) - window):
            value = values[index]
            if not np.isfinite(value):
                continue
            neighborhood = values[index - window : index + window + 1]
            if kind == "high" and value >= np.nanmax(neighborhood):
                indices.append(index)
            elif kind == "low" and value <= np.nanmin(neighborhood):
                indices.append(index)
        return indices

    @staticmethod
    def _line_value(slope: float, intercept: float, index: int) -> float:
        return float(intercept + slope * index)

    def _descending_action_line(self, frame_4h: pd.DataFrame) -> dict[str, Any] | None:
        if len(frame_4h) < 24:
            return None
        highs = pd.to_numeric(frame_4h["high"], errors="coerce")
        atrs = self._atr(frame_4h)
        pivots = self._pivot_indices(highs, kind="high")
        pivots = [index for index in pivots if index <= len(frame_4h) - 3]
        best: dict[str, Any] | None = None
        for left_pos, first in enumerate(pivots):
            for second in pivots[left_pos + 1 :]:
                if second - first < self.tori_min_touch_separation_bars:
                    continue
                first_value = float(highs.iloc[first])
                second_value = float(highs.iloc[second])
                slope = (second_value - first_value) / (second - first)
                if slope >= 0:
                    continue
                intercept = first_value - slope * first
                touches: list[int] = []
                for pivot in pivots:
                    if pivot < first:
                        continue
                    expected = self._line_value(slope, intercept, pivot)
                    tolerance = max(
                        float(atrs.iloc[pivot]) * self.tori_touch_tolerance_atr,
                        expected * 0.0015,
                    )
                    if abs(float(highs.iloc[pivot]) - expected) <= tolerance:
                        if not touches or pivot - touches[-1] >= self.tori_min_touch_separation_bars:
                            touches.append(pivot)
                if len(touches) < 3:
                    continue
                invalid = False
                for index in range(touches[0], len(frame_4h) - 1):
                    line = self._line_value(slope, intercept, index)
                    tolerance = max(float(atrs.iloc[index]) * 0.15, line * 0.001)
                    if float(frame_4h.iloc[index]["close"]) > line + tolerance:
                        invalid = True
                        break
                if invalid:
                    continue
                span = touches[-1] - touches[0]
                quality = len(touches) * 10 + span
                candidate = {
                    "slope": float(slope),
                    "intercept": float(intercept),
                    "touches": touches,
                    "quality": quality,
                }
                if best is None or quality > int(best["quality"]):
                    best = candidate
        return best

    def _ascending_safety_line(
        self, frame_4h: pd.DataFrame, *, start_index: int
    ) -> dict[str, Any] | None:
        lows = pd.to_numeric(frame_4h["low"], errors="coerce")
        pivots = [
            index
            for index in self._pivot_indices(lows, kind="low")
            if index >= start_index and index <= len(frame_4h) - 2
        ]
        best: dict[str, Any] | None = None
        for left_pos, first in enumerate(pivots):
            for second in pivots[left_pos + 1 :]:
                if second - first < 2:
                    continue
                first_value = float(lows.iloc[first])
                second_value = float(lows.iloc[second])
                slope = (second_value - first_value) / (second - first)
                if slope <= 0:
                    continue
                intercept = first_value - slope * first
                current_value = self._line_value(slope, intercept, len(frame_4h) - 1)
                if current_value <= 0:
                    continue
                quality = second - first
                candidate = {
                    "slope": float(slope),
                    "intercept": float(intercept),
                    "first": first,
                    "second": second,
                    "current_value": current_value,
                    "quality": quality,
                }
                if best is None or quality > int(best["quality"]):
                    best = candidate
        return best

    def tori_trendline_assessment(
        self,
        frame_15m: pd.DataFrame,
        frame_1h: pd.DataFrame,
        regime: str,
    ) -> dict[str, Any]:
        frame_4h = self.aggregate_closed_4h(frame_1h)
        if len(frame_4h) < 24:
            return self._assessment(
                TORI_TRENDLINE_STRATEGY,
                0.0,
                False,
                [],
                [f"storico 4h insufficiente: {len(frame_4h)}/24"],
                self.tori_min_gross_rr,
            )
        frame_4h = frame_4h.copy()
        frame_4h["atr"] = self._atr(frame_4h)
        action = self._descending_action_line(frame_4h)
        if action is None:
            return self._assessment(
                TORI_TRENDLINE_STRATEGY,
                0.0,
                False,
                [],
                ["nessuna trendline discendente valida con tre contatti"],
                self.tori_min_gross_rr,
            )

        current_index = len(frame_4h) - 1
        previous_index = current_index - 1
        current = frame_4h.iloc[current_index]
        previous = frame_4h.iloc[previous_index]
        atr_4h = float(current["atr"])
        current_line = self._line_value(
            float(action["slope"]), float(action["intercept"]), current_index
        )
        previous_line = self._line_value(
            float(action["slope"]), float(action["intercept"]), previous_index
        )
        break_buffer = max(atr_4h * self.tori_break_buffer_atr, current_line * 0.001)
        confirmed_break = bool(
            float(previous["close"]) <= previous_line + break_buffer * 0.5
            and float(current["close"]) > current_line + break_buffer
        )
        extension_atr = (
            (float(current["close"]) - current_line) / atr_4h if atr_4h > 0 else 999.0
        )
        not_extended = extension_atr <= self.tori_max_extension_atr
        bullish_close = float(current["close"]) > float(current["open"])

        safety = self._ascending_safety_line(
            frame_4h, start_index=int(action["touches"][0])
        )
        safety_valid = bool(
            safety is not None
            and float(safety["current_value"]) < float(current["close"])
        )
        trend_context_safe = regime != "TREND_DOWN"

        score = 0.0
        score += min(30.0, len(action["touches"]) * 10.0)
        score += 24.0 if confirmed_break else 0.0
        score += 14.0 if safety_valid else 0.0
        score += 10.0 if not_extended else 0.0
        score += 8.0 if bullish_close else 0.0
        score += 8.0 if trend_context_safe else 0.0
        score += 6.0 if int(action["touches"][-1]) - int(action["touches"][0]) >= 12 else 0.0

        blockers: list[str] = []
        if not confirmed_break:
            blockers.append("manca chiusura 4h confermata sopra la action line")
        if not safety_valid:
            blockers.append("safety line ascendente non valida")
        if not not_extended:
            blockers.append(
                f"breakout già esteso {extension_atr:.2f} ATR oltre la trendline"
            )
        if not bullish_close:
            blockers.append("candela 4h di rottura non rialzista")
        if not trend_context_safe:
            blockers.append("contesto 1h classificato TREND_DOWN")

        qualified = bool(
            len(action["touches"]) >= 3
            and confirmed_break
            and safety_valid
            and not_extended
            and bullish_close
            and trend_context_safe
            and score >= 82.0
        )
        result = self._assessment(
            TORI_TRENDLINE_STRATEGY,
            score,
            qualified,
            [
                f"action line 4h con {len(action['touches'])} contatti puliti",
                "rottura confermata dalla chiusura della candela 4h",
                "safety line opposta disponibile per invalidazione strutturale",
                "ingresso non esteso oltre la trendline",
            ],
            blockers,
            self.tori_min_gross_rr,
        )
        result.update(
            {
                "tori_runtime_version": TORI_RUNTIME_VERSION,
                "tori_action_slope": round(float(action["slope"]), 10),
                "tori_action_intercept": round(float(action["intercept"]), 10),
                "tori_action_line": round(current_line, 10),
                "tori_touch_indices": list(action["touches"]),
                "tori_touch_count": len(action["touches"]),
                "tori_safety_line": (
                    round(float(safety["current_value"]), 10) if safety else None
                ),
                "tori_extension_atr": round(extension_atr, 4),
            }
        )
        return result

    def evaluate_pair_detailed(
        self, pair: str
    ) -> tuple[GeneratedSignal | None, str, dict[str, Any]]:
        frame_15m = self.fetch_closed_frame(pair, "15m", 260)
        frame_1h = self.fetch_closed_frame(pair, "1h", 520)
        if len(frame_15m) < 210 or len(frame_1h) < 210:
            return None, "INSUFFICIENT_OHLC", {
                "pair": pair,
                "regime": "UNKNOWN",
                "strategy": "NONE",
                "score": 0.0,
                "blockers": [
                    f"storico insufficiente 15m={len(frame_15m)} 1h={len(frame_1h)}"
                ],
            }
        frame_15m = self.add_indicators(frame_15m)
        frame_1h = self.add_indicators(frame_1h)
        self._frame_cache[pair] = frame_15m
        self._structure_hourly_frames[pair] = frame_1h.copy()
        latest = frame_15m.iloc[-1]
        regime = self.detect_regime(frame_15m, frame_1h)
        relative = self._relative_strength_snapshot.get(pair, {})
        assessments = [
            self.trend_pullback_assessment(frame_15m, frame_1h, regime),
            self.breakout_assessment(frame_15m, frame_1h, regime),
            self.range_bounce_assessment(frame_15m, frame_1h, regime),
            self.oversold_reversal_assessment(frame_15m, frame_1h, regime),
            self.relative_strength_assessment(frame_15m, frame_1h, regime, relative),
            self.volatility_squeeze_assessment(frame_15m, frame_1h, regime),
            self.harmonic_assessment(frame_15m, frame_1h, regime),
            self.tori_trendline_assessment(frame_15m, frame_1h, regime),
        ]
        ordered = sorted(
            assessments,
            key=lambda item: self.assessment_priority(item, self._strategy_states),
            reverse=True,
        )
        fallback = ordered[0]
        last_failure: tuple[str, dict[str, Any]] | None = None
        for assessment in ordered:
            if not bool(assessment.get("qualified")):
                continue
            self._tori_assessment = assessment if assessment.get("strategy") == TORI_TRENDLINE_STRATEGY else None
            try:
                signal, reason, diagnostic = self._build_signal_for_assessment(
                    pair, frame_15m, regime, relative, assessment
                )
            finally:
                self._tori_assessment = None
            if signal is not None:
                return signal, reason, diagnostic
            if last_failure is None:
                last_failure = (reason, diagnostic)
        if last_failure is not None:
            return None, last_failure[0], last_failure[1]
        strategy = str(fallback.get("strategy") or "NONE")
        return None, f"NO_SETUP_{regime}", {
            "pair": pair,
            "regime": regime,
            "strategy": strategy,
            "strategy_state": str(
                self._strategy_states.get(strategy, {}).get("state") or "ACTIVE"
            ),
            "score": float(fallback.get("score") or 0.0),
            "qualified": False,
            "blockers": list(fallback.get("blockers") or [])[:3],
            "rsi": round(float(latest["rsi"]), 1),
            "volume_ratio": round(float(latest["volume_ratio"]), 2),
            "relative_24h_pct": round(
                float(relative.get("relative_24h", 0.0)) * 100, 2
            ),
            "candle_time": str(latest["timestamp"]),
        }

    def stop_distance_for_strategy(
        self, strategy: str, frame: pd.DataFrame, entry: float, atr: float
    ) -> float:
        if strategy != TORI_TRENDLINE_STRATEGY:
            return super().stop_distance_for_strategy(strategy, frame, entry, atr)
        assessment = self._tori_assessment or {}
        safety_line = assessment.get("tori_safety_line")
        if safety_line is None:
            return 0.0
        safety = float(safety_line)
        distance = entry - (safety - atr * 0.25)
        return min(max(distance, atr * 1.10, entry * 0.004), entry * 0.025)

    def technical_target(
        self,
        strategy: str,
        frame: pd.DataFrame,
        entry: float,
        stop_distance: float,
        atr: float,
        target_rr: float,
    ) -> float:
        if strategy != TORI_TRENDLINE_STRATEGY:
            return super().technical_target(
                strategy, frame, entry, stop_distance, atr, target_rr
            )
        return min(
            entry + stop_distance * max(self.tori_min_gross_rr, target_rr),
            entry * 1.05,
            entry + atr * 8.0,
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
        if signal is None or signal.strategy != TORI_TRENDLINE_STRATEGY:
            return signal, reason, diagnostic
        breakdown = dict(signal.score_breakdown or {})
        breakdown.update(
            {
                "tori_runtime_version": TORI_RUNTIME_VERSION,
                "execution_timeframe": "4h",
                "action_line_touches": int(assessment.get("tori_touch_count", 0)),
                "action_line_level": assessment.get("tori_action_line"),
                "safety_line_level": assessment.get("tori_safety_line"),
                "break_extension_atr": assessment.get("tori_extension_atr"),
                "method_source": "public Tori Trades three-touch trendline framework",
            }
        )
        reasons = list(signal.reasons or [])
        reasons.append("strategia 4h: action line a tre contatti e safety line strutturale")
        return replace(signal, score_breakdown=breakdown, reasons=reasons), reason, diagnostic

    def fetch_forward_performance(self) -> dict[str, dict[str, Any]]:
        placeholders = ", ".join(["%s"] * len(EIGHT_LONG_STRATEGIES))
        rows = self.client.fetch_all(
            f"""SELECT strategy, status, net_profit_tp1_eur, net_loss_sl_eur, closed_at
            FROM signals.generated_signals
            WHERE strategy IN ({placeholders})
              AND score_breakdown->>'runtime_version' = %s
              AND telegram_sent_at IS NOT NULL
              AND COALESCE(outcome_ambiguous, FALSE) = FALSE
              AND status IN ('TARGET_HIT', 'STOP_LOSS')
              AND created_at >= NOW() - INTERVAL '120 days'
            ORDER BY closed_at ASC, id ASC""",
            (*EIGHT_LONG_STRATEGIES, capital_module.RUNTIME_VERSION),
        )
        grouped: dict[str, list[tuple[Any, ...]]] = {
            name: [] for name in EIGHT_LONG_STRATEGIES
        }
        for row in rows:
            grouped.setdefault(str(row[0]), []).append(row)
        return {
            strategy: self.calculate_enhanced_performance(items)
            for strategy, items in grouped.items()
        }

    def evaluate(self) -> GeneratedSignal | None:
        result = super().evaluate()
        self._last_cycle_summary["tori_strategy"] = TORI_TRENDLINE_STRATEGY
        self._last_cycle_summary["strategy_count"] = len(EIGHT_LONG_STRATEGIES)
        self.logger.info(
            "TORI_TRENDLINE_V1 strategy=%s timeframe=4h touches=3 safety_line=true "
            "confirmed_close=true long_only=true",
            TORI_TRENDLINE_STRATEGY,
        )
        return result
