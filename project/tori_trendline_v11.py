"""Fresh-break and structural-risk hardening for the eighth strategy."""
from __future__ import annotations

from dataclasses import replace
from typing import Any

import pandas as pd

from project.strategy_engine.service import GeneratedSignal
from project.tori_trendline_scanner import (
    TORI_RUNTIME_VERSION,
    TORI_TRENDLINE_STRATEGY,
    ToriTrendlineDailyPaperScanner,
)


class ToriTrendlineDailyPaperScannerV11(ToriTrendlineDailyPaperScanner):
    """Only publish fresh 4h breaks with a genuine safety-line stop and full 2R room."""

    def tori_trendline_assessment(
        self,
        frame_15m: pd.DataFrame,
        frame_1h: pd.DataFrame,
        regime: str,
    ) -> dict[str, Any]:
        result = dict(super().tori_trendline_assessment(frame_15m, frame_1h, regime))
        frame_4h = self.aggregate_closed_4h(frame_1h)
        if frame_4h.empty:
            return result
        frame_4h = frame_4h.copy()
        frame_4h["atr"] = self._atr(frame_4h)
        current = frame_4h.iloc[-1]
        break_end = pd.Timestamp(current["timestamp"]) + pd.Timedelta(hours=4)
        latest_15m_end = pd.Timestamp(frame_15m.iloc[-1]["timestamp"]) + pd.Timedelta(minutes=15)
        age_minutes = (latest_15m_end - break_end).total_seconds() / 60.0
        fresh_break = 0.0 <= age_minutes <= 90.0
        blockers = list(result.get("blockers") or [])
        if not fresh_break:
            blockers.append(
                f"rottura 4h non recente: età {age_minutes:.0f} minuti, massimo 90"
            )
        result.update(
            {
                "blockers": blockers,
                "qualified": bool(result.get("qualified")) and fresh_break,
                "tori_break_end": str(break_end),
                "tori_break_age_minutes": round(age_minutes, 2),
                "tori_atr_4h": round(float(current["atr"]), 10),
                "tori_runtime_version": f"{TORI_RUNTIME_VERSION}_1",
            }
        )
        return result

    def stop_distance_for_strategy(
        self, strategy: str, frame: pd.DataFrame, entry: float, atr: float
    ) -> float:
        if strategy != TORI_TRENDLINE_STRATEGY:
            return super().stop_distance_for_strategy(strategy, frame, entry, atr)
        assessment = self._tori_assessment or {}
        safety_value = assessment.get("tori_safety_line")
        if safety_value is None:
            return 0.0
        safety = float(safety_value)
        atr_4h = float(assessment.get("tori_atr_4h") or 0.0)
        buffer = max(atr * 0.25, atr_4h * 0.10, entry * 0.0005)
        stop = safety - buffer
        distance = entry - stop
        # Never move the stop above the safety line merely to make the trade fit.
        if stop <= 0 or distance <= 0 or distance > entry * 0.04:
            return 0.0
        return max(distance, atr * 1.10, entry * 0.004)

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

        action_line = float(assessment.get("tori_action_line") or 0.0)
        atr_4h = float(assessment.get("tori_atr_4h") or 0.0)
        actual_extension = (
            (float(signal.entry) - action_line) / atr_4h if atr_4h > 0 else 999.0
        )
        if action_line <= 0 or actual_extension < 0 or actual_extension > self.tori_max_extension_atr:
            diagnostic["blockers"] = [
                f"entry reale non più valida rispetto alla action line: {actual_extension:.2f} ATR"
            ]
            diagnostic["tori_actual_entry_extension_atr"] = round(actual_extension, 4)
            return None, "TORI_ENTRY_NO_LONGER_VALID", diagnostic

        # This method is deliberately stricter than the generic daily-paper filter: after any
        # resistance cap, the setup must still preserve the method's full initial 2R geometry.
        if float(signal.gross_rr) + 1e-9 < self.tori_min_gross_rr:
            diagnostic["blockers"] = [
                f"prima resistenza non lascia {self.tori_min_gross_rr:.2f}R lordo"
            ]
            diagnostic["gross_rr_after_structure"] = round(float(signal.gross_rr), 4)
            return None, "TORI_INSUFFICIENT_2R_ROOM", diagnostic

        breakdown = dict(signal.score_breakdown or {})
        breakdown.update(
            {
                "tori_runtime_version": f"{TORI_RUNTIME_VERSION}_1",
                "tori_break_age_minutes": assessment.get("tori_break_age_minutes"),
                "tori_actual_entry_extension_atr": round(actual_extension, 4),
                "tori_full_2r_preserved": True,
            }
        )
        return replace(signal, score_breakdown=breakdown), reason, diagnostic

    def evaluate(self) -> GeneratedSignal | None:
        result = super().evaluate()
        self.logger.info(
            "TORI_TRENDLINE_V1_1 fresh_break_max_minutes=90 structural_stop=true full_2r=true"
        )
        return result
