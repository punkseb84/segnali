"""Pure price-action LONG scanner for paper validation.

The runtime intentionally avoids EMA, RSI, MACD, Bollinger Bands, ADX and volume
confirmations. Setups are derived only from closed OHLC candles, swing points,
trendlines, horizontal levels, breakouts, retests and structural invalidation.
"""
from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from statistics import median
from typing import Any

import pandas as pd

from project.daily_paper_scanner import PAPER_EXECUTION_MODE
from project.shared.events import Event, EventType
from project.shared.logging import get_module_logger
from project.simple_strategy_scanner import SimpleStrategyScanner
from project.strategy_engine.service import GeneratedSignal


PRICE_ACTION_RUNTIME_VERSION = "PURE_PRICE_ACTION_V7"
TRENDLINE_RETEST = "Price Action Trendline Retest"
HORIZONTAL_RETEST = "Price Action Level Retest"
PRICE_ACTION_STRATEGIES = (TRENDLINE_RETEST, HORIZONTAL_RETEST)


class PurePriceActionScanner(SimpleStrategyScanner):
    """Publish paper signals from market structure without technical indicators."""

    def __init__(
        self,
        *args: Any,
        paper_max_per_day: int = 4,
        paper_max_per_cycle: int = 1,
        paper_min_interval_minutes: int = 60,
        paper_pair_cooldown_minutes: int = 240,
        min_signal_score: float = 72.0,
        min_net_rr: float = 1.15,
        min_net_profit_eur: float = 0.15,
        max_net_loss_eur: float = 1.00,
        swing_window: int = 2,
        min_trendline_touches: int = 3,
        min_horizontal_touches: int = 2,
        retest_window_15m: int = 6,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            *args,
            min_signal_score=min_signal_score,
            min_net_rr=min_net_rr,
            min_net_profit_eur=min_net_profit_eur,
            max_signals_per_cycle=paper_max_per_cycle,
            **kwargs,
        )
        self.paper_max_per_day = max(1, min(8, int(paper_max_per_day)))
        self.paper_max_per_cycle = max(1, min(3, int(paper_max_per_cycle)))
        self.paper_min_interval_minutes = max(15, int(paper_min_interval_minutes))
        self.paper_pair_cooldown_minutes = max(60, int(paper_pair_cooldown_minutes))
        self.max_net_loss_eur = max(0.25, float(max_net_loss_eur))
        self.swing_window = max(1, int(swing_window))
        self.min_trendline_touches = max(3, int(min_trendline_touches))
        self.min_horizontal_touches = max(2, int(min_horizontal_touches))
        self.retest_window_15m = max(3, int(retest_window_15m))
        self._pair_diagnostics: list[dict[str, Any]] = []
        self.logger = get_module_logger("pure-price-action")

    @staticmethod
    def _median_range(frame: pd.DataFrame, lookback: int = 24) -> float:
        sample = frame.tail(lookback)
        values = (sample["high"] - sample["low"]).dropna()
        if values.empty:
            close = float(frame.iloc[-1]["close"]) if not frame.empty else 0.0
            return max(close * 0.002, 1e-12)
        return max(float(median(float(value) for value in values)), 1e-12)

    def _pivot_indices(
        self,
        series: pd.Series,
        *,
        kind: str,
        window: int | None = None,
    ) -> list[int]:
        size = self.swing_window if window is None else max(1, int(window))
        values = pd.to_numeric(series, errors="coerce").to_numpy(dtype=float)
        pivots: list[int] = []
        for index in range(size, len(values) - size):
            value = values[index]
            neighborhood = values[index - size : index + size + 1]
            if pd.isna(value) or pd.isna(neighborhood).any():
                continue
            if kind == "high" and value >= max(neighborhood):
                pivots.append(index)
            elif kind == "low" and value <= min(neighborhood):
                pivots.append(index)
        return pivots

    @staticmethod
    def _line_value(slope: float, intercept: float, index: int) -> float:
        return float(intercept + slope * index)

    def _descending_trendline(self, frame_1h: pd.DataFrame) -> dict[str, Any] | None:
        data = frame_1h.tail(140).reset_index(drop=True)
        if len(data) < 36:
            return None
        highs = pd.to_numeric(data["high"], errors="coerce")
        pivots = [
            index
            for index in self._pivot_indices(highs, kind="high")
            if index <= len(data) - 3
        ]
        range_unit = self._median_range(data, 36)
        best: dict[str, Any] | None = None
        for left_position, first in enumerate(pivots):
            for second in pivots[left_position + 1 :]:
                if second - first < 4:
                    continue
                first_high = float(highs.iloc[first])
                second_high = float(highs.iloc[second])
                slope = (second_high - first_high) / (second - first)
                if slope >= 0:
                    continue
                intercept = first_high - slope * first
                tolerance = max(range_unit * 0.35, first_high * 0.0015)
                touches: list[int] = []
                for pivot in pivots:
                    if pivot < first:
                        continue
                    expected = self._line_value(slope, intercept, pivot)
                    if abs(float(highs.iloc[pivot]) - expected) <= tolerance:
                        if not touches or pivot - touches[-1] >= 3:
                            touches.append(pivot)
                if len(touches) < self.min_trendline_touches:
                    continue
                invalid = False
                stale_end = max(touches[-1] + 1, len(data) - 5)
                for index in range(touches[0], stale_end):
                    line = self._line_value(slope, intercept, index)
                    if float(data.iloc[index]["close"]) > line + tolerance:
                        invalid = True
                        break
                if invalid:
                    continue
                quality = len(touches) * 20 + (touches[-1] - touches[0])
                candidate = {
                    "frame": data,
                    "slope": float(slope),
                    "intercept": float(intercept),
                    "touches": touches,
                    "tolerance": float(tolerance),
                    "quality": quality,
                    "range_unit": range_unit,
                }
                if best is None or quality > int(best["quality"]):
                    best = candidate
        return best

    def _trendline_break(self, line: dict[str, Any]) -> dict[str, Any] | None:
        data = line["frame"]
        slope = float(line["slope"])
        intercept = float(line["intercept"])
        tolerance = float(line["tolerance"])
        for index in range(max(1, len(data) - 5), len(data)):
            previous_line = self._line_value(slope, intercept, index - 1)
            current_line = self._line_value(slope, intercept, index)
            previous_close = float(data.iloc[index - 1]["close"])
            current_close = float(data.iloc[index]["close"])
            if (
                previous_close <= previous_line + tolerance * 0.5
                and current_close > current_line + tolerance * 0.35
            ):
                return {
                    "index": index,
                    "level": current_line,
                    "timestamp": data.iloc[index]["timestamp"],
                    "close": current_close,
                }
        return None

    def _horizontal_level(self, frame_1h: pd.DataFrame) -> dict[str, Any] | None:
        data = frame_1h.tail(120).reset_index(drop=True)
        if len(data) < 30:
            return None
        highs = pd.to_numeric(data["high"], errors="coerce")
        pivots = self._pivot_indices(highs, kind="high")
        range_unit = self._median_range(data, 36)
        tolerance = max(range_unit * 0.30, float(data.iloc[-1]["close"]) * 0.0012)
        clusters: list[list[tuple[int, float]]] = []
        for index in pivots:
            value = float(highs.iloc[index])
            for cluster in clusters:
                center = sum(item[1] for item in cluster) / len(cluster)
                if abs(value - center) <= tolerance:
                    cluster.append((index, value))
                    break
            else:
                clusters.append([(index, value)])
        latest_close = float(data.iloc[-1]["close"])
        candidates: list[dict[str, Any]] = []
        for cluster in clusters:
            if len(cluster) < self.min_horizontal_touches:
                continue
            level = sum(item[1] for item in cluster) / len(cluster)
            if level >= latest_close:
                continue
            last_touch = max(item[0] for item in cluster)
            if len(data) - last_touch > 80:
                continue
            candidates.append(
                {
                    "level": float(level),
                    "touches": len(cluster),
                    "touch_indices": [item[0] for item in cluster],
                }
            )
        if not candidates:
            return None
        return max(candidates, key=lambda item: (item["level"], item["touches"]))

    @staticmethod
    def _bullish_rejection(row: pd.Series) -> bool:
        candle_range = max(float(row["high"]) - float(row["low"]), 1e-12)
        body = abs(float(row["close"]) - float(row["open"]))
        lower_wick = min(float(row["open"]), float(row["close"])) - float(row["low"])
        close_location = (float(row["close"]) - float(row["low"])) / candle_range
        return bool(
            float(row["close"]) > float(row["open"])
            and close_location >= 0.62
            and (lower_wick >= body * 0.35 or body >= candle_range * 0.45)
        )

    def _retest_trigger(
        self,
        frame_15m: pd.DataFrame,
        *,
        level: float,
        break_time: Any | None,
        tolerance: float,
    ) -> dict[str, Any] | None:
        recent = frame_15m.tail(self.retest_window_15m).copy()
        if break_time is not None:
            timestamp = pd.Timestamp(break_time)
            if timestamp.tzinfo is None:
                timestamp = timestamp.tz_localize("UTC")
            recent = recent[recent["timestamp"] >= timestamp]
        if recent.empty:
            return None
        touched = recent[
            (recent["low"] <= level + tolerance)
            & (recent["high"] >= level - tolerance)
            & (recent["close"] >= level - tolerance * 0.25)
        ]
        latest = frame_15m.iloc[-1]
        if touched.empty or not self._bullish_rejection(latest):
            return None
        if float(latest["close"]) < level:
            return None
        return {
            "entry": float(latest["close"]),
            "structural_low": float(recent["low"].min()),
            "trigger_time": latest["timestamp"],
            "retest_count": len(touched),
        }

    def _fresh_break_trigger(
        self,
        frame_15m: pd.DataFrame,
        *,
        level: float,
        tolerance: float,
    ) -> dict[str, Any] | None:
        latest = frame_15m.iloc[-1]
        previous = frame_15m.iloc[-2]
        range_unit = self._median_range(frame_15m, 24)
        crossed = (
            float(previous["close"]) <= level + tolerance
            and float(latest["close"]) > level + tolerance * 0.25
        )
        extension = float(latest["close"]) - level
        if (
            not crossed
            or not self._bullish_rejection(latest)
            or extension > range_unit * 0.85
        ):
            return None
        return {
            "entry": float(latest["close"]),
            "structural_low": float(min(latest["low"], previous["low"])),
            "trigger_time": latest["timestamp"],
            "retest_count": 0,
            "fresh_break": True,
        }

    def _next_resistance(
        self,
        frame_15m: pd.DataFrame,
        frame_1h: pd.DataFrame,
        *,
        entry: float,
    ) -> float | None:
        levels: list[float] = []
        for frame in (
            frame_15m.tail(160).reset_index(drop=True),
            frame_1h.tail(120).reset_index(drop=True),
        ):
            highs = pd.to_numeric(frame["high"], errors="coerce")
            for index in self._pivot_indices(highs, kind="high", window=2):
                level = float(highs.iloc[index])
                if level > entry * 1.001:
                    levels.append(level)
        return min(levels) if levels else None

    def _structure_regime(self, frame_1h: pd.DataFrame) -> str:
        data = frame_1h.tail(100).reset_index(drop=True)
        highs = self._pivot_indices(data["high"], kind="high")
        lows = self._pivot_indices(data["low"], kind="low")
        if len(highs) >= 2 and len(lows) >= 2:
            high_up = float(data.iloc[highs[-1]]["high"]) > float(data.iloc[highs[-2]]["high"])
            low_up = float(data.iloc[lows[-1]]["low"]) > float(data.iloc[lows[-2]]["low"])
            high_down = float(data.iloc[highs[-1]]["high"]) < float(data.iloc[highs[-2]]["high"])
            low_down = float(data.iloc[lows[-1]]["low"]) < float(data.iloc[lows[-2]]["low"])
            if high_up and low_up:
                return "UP_STRUCTURE"
            if high_down and low_down:
                return "DOWN_STRUCTURE"
        return "MIXED_STRUCTURE"

    def _build_signal(
        self,
        *,
        pair: str,
        frame_15m: pd.DataFrame,
        frame_1h: pd.DataFrame,
        strategy: str,
        regime: str,
        level: float,
        trigger: dict[str, Any],
        score: float,
        reasons: list[str],
        metadata: dict[str, Any],
    ) -> tuple[GeneratedSignal | None, str, dict[str, Any]]:
        entry = float(trigger["entry"])
        range_unit = self._median_range(frame_15m, 24)
        stop_buffer = max(range_unit * 0.18, entry * 0.0005)
        stop_loss = float(trigger["structural_low"]) - stop_buffer
        stop_distance = entry - stop_loss
        diagnostic = {
            "pair": pair,
            "regime": regime,
            "strategy": strategy,
            "score": round(score, 2),
            "qualified": False,
            "level": round(level, 10),
            "candle_time": str(trigger["trigger_time"]),
            "blockers": [],
        }
        if stop_loss <= 0 or stop_distance <= 0:
            diagnostic["blockers"] = ["stop strutturale non valido"]
            return None, "INVALID_STRUCTURAL_STOP", diagnostic
        stop_pct = stop_distance / entry
        if stop_pct > 0.03:
            diagnostic["blockers"] = [
                f"invalidazione strutturale troppo distante: {stop_pct * 100:.2f}%"
            ]
            return None, "STRUCTURAL_STOP_TOO_WIDE", diagnostic
        if stop_pct < 0.0025:
            stop_loss = min(stop_loss, entry * 0.9975)
            stop_distance = entry - stop_loss
            stop_pct = stop_distance / entry

        resistance = self._next_resistance(frame_15m, frame_1h, entry=entry)
        planned_target = min(entry + stop_distance * 1.65, entry * 1.03)
        if resistance is not None:
            resistance_buffer = max(range_unit * 0.15, entry * 0.0004)
            planned_target = min(planned_target, resistance - resistance_buffer)
        if planned_target <= entry:
            diagnostic["blockers"] = ["prima resistenza troppo vicina"]
            return None, "RESISTANCE_TOO_CLOSE", diagnostic

        economics = self.calculate_net_economics(entry, stop_loss, planned_target)
        if not bool(economics.get("tradable")):
            diagnostic["blockers"] = ["quantità o nozionale non negoziabile"]
            return None, "NOT_TRADABLE", diagnostic
        if float(economics["net_rr"]) < self.min_net_rr:
            diagnostic["blockers"] = [
                f"R/R netto insufficiente: {float(economics['net_rr']):.2f} < {self.min_net_rr:.2f}"
            ]
            return None, "NET_RR_TOO_LOW", diagnostic
        if float(economics["net_profit_tp1_eur"]) < self.min_tp1_net_profit_eur:
            diagnostic["blockers"] = [
                f"profitto netto insufficiente: €{float(economics['net_profit_tp1_eur']):.2f}"
            ]
            return None, "NET_PROFIT_TOO_LOW", diagnostic

        score = min(100.0, max(float(self.min_signal_score), score))
        breakdown: dict[str, Any] = {
            "runtime_version": PRICE_ACTION_RUNTIME_VERSION,
            "paper_runtime_version": PRICE_ACTION_RUNTIME_VERSION,
            "execution_mode": PAPER_EXECUTION_MODE,
            "paper_only": True,
            "execution_timeframe": "1h",
            "monitor_timeframe": "15m",
            "price_action_only": True,
            "oscillators_used": False,
            "dynamic_averages_used": False,
            "setup_level": round(level, 10),
            "structural_stop": round(stop_loss, 10),
            "next_resistance": round(resistance, 10) if resistance else None,
            "retest_count": int(trigger.get("retest_count", 0)),
            "fresh_break": bool(trigger.get("fresh_break", False)),
            "median_candle_range_15m": round(range_unit, 10),
            **metadata,
        }
        reasons = list(reasons) + [
            "stop sotto il minimo che invalida il setup",
            "target prima della resistenza successiva",
            "nessun RSI, MACD, EMA, ADX o Bollinger usato",
        ]
        signal_time = datetime.now(timezone.utc)
        reference_time = pd.Timestamp(trigger["trigger_time"]).to_pydatetime()
        signal = GeneratedSignal(
            strategy=strategy,
            pair=pair,
            timeframe="15m",
            regime=regime,
            entry=entry,
            stop_loss=stop_loss,
            take_profit=planned_target,
            technical_take_profit=planned_target,
            effective_take_profit=planned_target,
            signal_time=signal_time,
            reference_candle_time=reference_time,
            entry_timing="ENTER_ON_SIGNAL_RECEIPT",
            quantity=float(economics["quantity"]),
            entry_notional_eur=float(economics["entry_notional_eur"]),
            tp_notional_eur=float(economics["tp_notional_eur"]),
            sl_notional_eur=float(economics["sl_notional_eur"]),
            gross_profit_tp1_eur=float(economics["gross_profit_tp1_eur"]),
            gross_loss_sl_eur=float(economics["gross_loss_sl_eur"]),
            estimated_buy_fee_eur=float(economics["estimated_buy_fee_eur"]),
            estimated_sell_fee_eur=float(economics["estimated_sell_fee_eur"]),
            estimated_sell_fee_sl_eur=float(economics["estimated_sell_fee_sl_eur"]),
            estimated_spread_cost_eur=float(economics["estimated_spread_cost_eur"]),
            estimated_slippage_cost_eur=float(economics["estimated_slippage_cost_eur"]),
            net_profit_tp1_eur=float(economics["net_profit_tp1_eur"]),
            net_loss_sl_eur=float(economics["net_loss_sl_eur"]),
            gross_rr=float(economics["gross_rr"]),
            net_rr=float(economics["net_rr"]),
            stop_distance=stop_distance,
            stop_pct=stop_pct * 100.0,
            atr=range_unit,
            stop_atr_ratio=stop_distance / range_unit if range_unit > 0 else 0.0,
            tp_atr_ratio=(planned_target - entry) / range_unit if range_unit > 0 else 0.0,
            historical_sample_size=0,
            historical_win_rate=None,
            historical_expected_value_eur=None,
            historical_mfe_percentile=0.0,
            historical_mae_percentile=0.0,
            probability_confidence="NOT_USED",
            validation_status="PRICE_ACTION_RULES_PASSED",
            validation_reason="; ".join(reasons),
            signal_class="B",
            score=score,
            probability=None,
            reasons=reasons,
            score_breakdown=breakdown,
        )
        diagnostic.update(
            {
                "qualified": True,
                "entry": round(entry, 10),
                "stop": round(stop_loss, 10),
                "target": round(planned_target, 10),
                "net_rr": round(float(economics["net_rr"]), 3),
                "blockers": [],
            }
        )
        return signal, "OK", diagnostic

    def evaluate_pair_detailed(
        self,
        pair: str,
    ) -> tuple[GeneratedSignal | None, str, dict[str, Any]]:
        frame_15m = self.fetch_closed_frame(pair, "15m", 260)
        frame_1h = self.fetch_closed_frame(pair, "1h", 180)
        if len(frame_15m) < 100 or len(frame_1h) < 60:
            return None, "INSUFFICIENT_OHLC", {
                "pair": pair,
                "regime": "UNKNOWN",
                "strategy": "NONE",
                "score": 0.0,
                "qualified": False,
                "blockers": [
                    f"storico insufficiente 15m={len(frame_15m)} 1h={len(frame_1h)}"
                ],
            }

        regime = self._structure_regime(frame_1h)
        assessments: list[dict[str, Any]] = []
        line = self._descending_trendline(frame_1h)
        if line is not None:
            broken = self._trendline_break(line)
            if broken is not None:
                tolerance_15m = max(
                    self._median_range(frame_15m, 24) * 0.40,
                    float(broken["level"]) * 0.0012,
                )
                trigger = self._retest_trigger(
                    frame_15m,
                    level=float(broken["level"]),
                    break_time=broken["timestamp"],
                    tolerance=tolerance_15m,
                )
                fresh = False
                if trigger is None:
                    trigger = self._fresh_break_trigger(
                        frame_15m,
                        level=float(broken["level"]),
                        tolerance=tolerance_15m,
                    )
                    fresh = trigger is not None
                if trigger is not None:
                    touch_count = len(line["touches"])
                    score = 74.0 + min(12.0, (touch_count - 3) * 4.0)
                    score += 8.0 if not fresh else 2.0
                    score += 4.0 if regime != "DOWN_STRUCTURE" else 0.0
                    assessments.append(
                        {
                            "strategy": TRENDLINE_RETEST,
                            "level": float(broken["level"]),
                            "trigger": trigger,
                            "score": score,
                            "reasons": [
                                f"trendline discendente 1h con {touch_count} contatti",
                                "chiusura sopra la trendline",
                                "retest rialzista della linea"
                                if not fresh
                                else "breakout fresco non esteso",
                            ],
                            "metadata": {
                                "trendline_touch_count": touch_count,
                                "trendline_slope": round(float(line["slope"]), 10),
                                "break_candle_time": str(broken["timestamp"]),
                            },
                        }
                    )

        horizontal = self._horizontal_level(frame_1h)
        if horizontal is not None:
            level = float(horizontal["level"])
            tolerance_15m = max(
                self._median_range(frame_15m, 24) * 0.38,
                level * 0.0010,
            )
            trigger = self._retest_trigger(
                frame_15m,
                level=level,
                break_time=None,
                tolerance=tolerance_15m,
            )
            if trigger is not None:
                recent = frame_15m.tail(self.retest_window_15m + 4)
                breakout_seen = bool(
                    (recent["close"] > level + tolerance_15m * 0.20).any()
                )
                prior_below = bool(
                    (recent["close"] <= level + tolerance_15m).any()
                )
                if breakout_seen and prior_below:
                    score = 72.0 + min(
                        12.0, int(horizontal["touches"]) * 3.0
                    )
                    score += 5.0 if regime == "UP_STRUCTURE" else 0.0
                    assessments.append(
                        {
                            "strategy": HORIZONTAL_RETEST,
                            "level": level,
                            "trigger": trigger,
                            "score": score,
                            "reasons": [
                                f"resistenza 1h testata {int(horizontal['touches'])} volte",
                                "rottura confermata sopra il livello",
                                "retest rialzista del vecchio ostacolo",
                            ],
                            "metadata": {
                                "horizontal_touch_count": int(
                                    horizontal["touches"]
                                ),
                                "horizontal_touch_indices": list(
                                    horizontal["touch_indices"]
                                ),
                            },
                        }
                    )

        if not assessments:
            return None, f"NO_PRICE_ACTION_SETUP_{regime}", {
                "pair": pair,
                "regime": regime,
                "strategy": "NONE",
                "score": 0.0,
                "qualified": False,
                "blockers": [
                    "nessuna rottura con retest o breakout fresco rilevato",
                    "in attesa di chiusura e conferma su candele chiuse",
                ],
                "candle_time": str(frame_15m.iloc[-1]["timestamp"]),
            }

        assessments.sort(key=lambda item: float(item["score"]), reverse=True)
        first_failure: tuple[str, dict[str, Any]] | None = None
        for setup in assessments:
            signal, reason, diagnostic = self._build_signal(
                pair=pair,
                frame_15m=frame_15m,
                frame_1h=frame_1h,
                strategy=str(setup["strategy"]),
                regime=regime,
                level=float(setup["level"]),
                trigger=dict(setup["trigger"]),
                score=float(setup["score"]),
                reasons=list(setup["reasons"]),
                metadata=dict(setup["metadata"]),
            )
            if signal is not None:
                return signal, reason, diagnostic
            if first_failure is None:
                first_failure = (reason, diagnostic)
        assert first_failure is not None
        return None, first_failure[0], first_failure[1]

    def paper_daily_state(self) -> dict[str, Any]:
        rows = self.client.fetch_all(
            """SELECT COUNT(*), MAX(created_at)
            FROM signals.generated_signals
            WHERE score_breakdown->>'paper_runtime_version' = %s
              AND score_breakdown->>'execution_mode' = %s
              AND telegram_sent_at IS NOT NULL
              AND (created_at AT TIME ZONE 'Europe/Rome')::date =
                  (NOW() AT TIME ZONE 'Europe/Rome')::date""",
            (PRICE_ACTION_RUNTIME_VERSION, PAPER_EXECUTION_MODE),
        )
        count = int(rows[0][0] or 0) if rows else 0
        last_sent = rows[0][1] if rows else None
        if isinstance(last_sent, datetime) and last_sent.tzinfo is None:
            last_sent = last_sent.replace(tzinfo=timezone.utc)
        return {"count": count, "last_sent": last_sent}

    def duplicate_exists(self, signal: GeneratedSignal) -> bool:
        rows = self.client.fetch_all(
            """SELECT id
            FROM signals.generated_signals
            WHERE pair = %s
              AND strategy = %s
              AND score_breakdown->>'paper_runtime_version' = %s
              AND telegram_sent_at IS NOT NULL
              AND created_at >= NOW() - (%s * INTERVAL '1 minute')
            ORDER BY created_at DESC
            LIMIT 1""",
            (
                signal.pair,
                signal.strategy,
                PRICE_ACTION_RUNTIME_VERSION,
                self.paper_pair_cooldown_minutes,
            ),
        )
        return bool(rows)

    def calculate_net_economics(
        self,
        entry: float,
        stop_loss: float,
        take_profit: float,
    ) -> dict[str, float | bool]:
        original_notional = self.trade_notional_eur
        result = super().calculate_net_economics(entry, stop_loss, take_profit)
        loss = float(result.get("net_loss_sl_eur") or 0.0)
        if loss <= self.max_net_loss_eur or original_notional <= self.min_notional_eur:
            return result
        adjusted_notional = max(
            self.min_notional_eur,
            original_notional * self.max_net_loss_eur / max(loss, 1e-9) * 0.98,
        )
        try:
            self.trade_notional_eur = adjusted_notional
            adjusted = super().calculate_net_economics(entry, stop_loss, take_profit)
        finally:
            self.trade_notional_eur = original_notional
        adjusted["risk_capped"] = True
        return adjusted

    def evaluate(self) -> GeneratedSignal | None:
        self._pair_diagnostics = []
        rejection_reasons: dict[str, int] = {}
        candidates: list[GeneratedSignal] = []
        now = datetime.now(timezone.utc)
        for pair in self.pairs:
            try:
                candidate, reason, diagnostic = self.evaluate_pair_detailed(pair)
            except Exception:
                self.logger.exception("PURE_PRICE_ACTION pair_failed pair=%s", pair)
                candidate = None
                reason = "PAIR_ERROR"
                diagnostic = {
                    "pair": pair,
                    "regime": "UNKNOWN",
                    "strategy": "NONE",
                    "score": 0.0,
                    "qualified": False,
                    "blockers": ["errore durante l'analisi price action"],
                }
            self._pair_diagnostics.append(diagnostic)
            if candidate is not None:
                candidates.append(candidate)
            else:
                rejection_reasons[reason] = rejection_reasons.get(reason, 0) + 1

        candidates.sort(key=lambda item: (item.score, item.net_rr), reverse=True)
        state = self.paper_daily_state()
        sent_today_before = int(state["count"])
        last_sent = state.get("last_sent")
        remaining = max(0, self.paper_max_per_day - sent_today_before)
        interval_ok = bool(
            last_sent is None
            or now - last_sent
            >= timedelta(minutes=self.paper_min_interval_minutes)
        )
        published: list[GeneratedSignal] = []
        for candidate in candidates:
            if remaining <= 0:
                rejection_reasons["PRICE_ACTION_DAILY_CAP"] = (
                    rejection_reasons.get("PRICE_ACTION_DAILY_CAP", 0) + 1
                )
                break
            if len(published) >= self.paper_max_per_cycle:
                rejection_reasons["PRICE_ACTION_CYCLE_LIMIT"] = (
                    rejection_reasons.get("PRICE_ACTION_CYCLE_LIMIT", 0) + 1
                )
                break
            if not interval_ok:
                rejection_reasons["PRICE_ACTION_MIN_INTERVAL"] = (
                    rejection_reasons.get("PRICE_ACTION_MIN_INTERVAL", 0) + 1
                )
                break
            if self.duplicate_exists(candidate):
                rejection_reasons["PRICE_ACTION_PAIR_COOLDOWN"] = (
                    rejection_reasons.get("PRICE_ACTION_PAIR_COOLDOWN", 0) + 1
                )
                continue
            signal_id = self.save_signal(candidate)
            signal = replace(candidate, signal_id=signal_id)
            self.publish_signal_event(signal)
            published.append(signal)
            remaining -= 1
            interval_ok = False
            self.logger.info(
                "PURE_PRICE_ACTION_SIGNAL id=%s pair=%s strategy=%s regime=%s score=%.1f "
                "entry=%.8f stop=%.8f target=%.8f net_rr=%.3f",
                signal_id,
                signal.pair,
                signal.strategy,
                signal.regime,
                signal.score,
                signal.entry,
                signal.stop_loss,
                signal.take_profit,
                signal.net_rr,
            )

        ranked = sorted(
            self._pair_diagnostics,
            key=lambda item: float(item.get("score") or 0.0),
            reverse=True,
        )
        best = ranked[0] if ranked else None
        self._last_cycle_summary = {
            "pairs_scanned": len(self.pairs),
            "qualified": len(candidates),
            "published": len(published),
            "paper_today": sent_today_before + len(published),
            "paper_daily_max": self.paper_max_per_day,
            "best_candidate": (
                f"{best.get('pair')} {best.get('strategy')} "
                f"score={float(best.get('score') or 0.0):.1f}"
                if best
                else "NONE"
            ),
            "rejections": rejection_reasons,
            "near_setups": ranked[:5],
            "runtime_version": PRICE_ACTION_RUNTIME_VERSION,
            "price_action_only": True,
        }
        self.logger.info(
            "PURE_PRICE_ACTION_SUMMARY pairs=%s qualified=%s published=%s today=%s/%s "
            "best=%s rejections=%s",
            len(self.pairs),
            len(candidates),
            len(published),
            sent_today_before + len(published),
            self.paper_max_per_day,
            self._last_cycle_summary["best_candidate"],
            json.dumps(rejection_reasons, sort_keys=True),
        )
        return published[0] if published else None

    def publish_daily_signal_report(self) -> bool:
        if not self.enable_daily_signal_report:
            return False
        now = datetime.now(timezone.utc)
        if self._last_no_trade_report_at is not None:
            elapsed = (
                now - self._last_no_trade_report_at
            ).total_seconds() / 3600.0
            if elapsed < self.daily_signal_report_hours:
                return False
        summary = self._last_cycle_summary or {}
        state = self.paper_daily_state()
        near_lines = []
        for item in list(summary.get("near_setups") or [])[:5]:
            blockers = ", ".join(
                str(value) for value in list(item.get("blockers") or [])[:2]
            )
            near_lines.append(
                f"• {item.get('pair', '?')}: {item.get('strategy', 'nessun setup')} · "
                f"score {float(item.get('score') or 0.0):.1f}"
                + (f" · {blockers}" if blockers else "")
            )
        if not near_lines:
            near_lines = [
                "• Nessun mercato vicino alla conferma nel ciclo più recente."
            ]
        message = (
            "📐 <b>REPORT PRICE ACTION V7</b>\n\n"
            "Motore: <b>solo candele, swing, trendline, livelli, breakout e retest</b>\n"
            "Indicatori: <b>RSI/MACD/EMA/ADX/Bollinger disattivati</b>\n\n"
            f"• Coppie analizzate: <b>{int(summary.get('pairs_scanned', 0))}</b>\n"
            f"• Setup qualificati ultimo ciclo: <b>{int(summary.get('qualified', 0))}</b>\n"
            f"• Segnali inviati ultimo ciclo: <b>{int(summary.get('published', 0))}</b>\n"
            f"• Segnali inviati oggi: <b>{int(state.get('count', 0))}/{self.paper_max_per_day}</b>\n"
            f"• Miglior candidato: <b>{summary.get('best_candidate', 'NONE')}</b>\n\n"
            "🔎 <b>Diagnostica</b>\n"
            + "\n".join(near_lines)
            + "\n\nTutti i segnali restano PAPER finché i risultati forward non dimostrano un vantaggio."
        )
        self.event_bus.publish(
            Event(EventType.REPORT_READY, {"message": message, "trusted_html": True})
        )
        self._last_no_trade_report_at = now
        self.logger.info("PURE_PRICE_ACTION_REPORT_SENT")
        return True
