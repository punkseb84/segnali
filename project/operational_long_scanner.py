"""Operational LONG-only scanner with strict cores and flexible confirmations.

The six strategy names remain unchanged. Each strategy keeps a mandatory price-structure
core, while RSI, MACD and volume act as corroborating confirmations instead of a brittle
all-or-nothing chain. Economic thresholds are never lowered: when a valid setup initially
fails only because fees dominate a narrow target, the target is expanded within the existing
3 ATR / 3% technical ceiling and the economics are recalculated.
"""
from __future__ import annotations

from dataclasses import replace
from typing import Any

import pandas as pd

from project.audited_long_scanner import AuditedLongStrategyScanner
from project.strategy_engine.service import GeneratedSignal


OPERATIONAL_LONG_STRATEGIES = (
    "Trend Pullback",
    "Breakout 20",
    "Range Bounce Bollinger",
    "Oversold Reversal",
    "Relative Strength Momentum",
    "Volatility Squeeze Breakout",
)


class OperationalLongStrategyScanner(AuditedLongStrategyScanner):
    """Run the six LONG strategies with core-plus-confirmation qualification."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._adaptive_confirmations_used = 0
        self._economic_targets_adjusted = 0

    @staticmethod
    def _remove_blocker(
        assessment: dict[str, Any],
        blocker: str,
        *,
        score_bonus: float,
        reason: str,
    ) -> dict[str, Any]:
        result = dict(assessment)
        blockers = list(result.get("blockers") or [])
        if blocker not in blockers:
            return result
        blockers.remove(blocker)
        result["blockers"] = blockers
        result["score"] = min(100.0, float(result.get("score") or 0.0) + score_bonus)
        reasons = list(result.get("reasons") or [])
        if reason not in reasons:
            reasons.append(reason)
        result["reasons"] = reasons
        return result

    @staticmethod
    def _promote_assessment(
        assessment: dict[str, Any],
        *,
        core_blockers: set[str],
        max_secondary_blockers: int,
        min_score: float,
        confirmation_reason: str,
    ) -> dict[str, Any]:
        result = dict(assessment)
        if bool(result.get("qualified")):
            return result
        blockers = list(result.get("blockers") or [])
        core_failures = [item for item in blockers if item in core_blockers]
        secondary_failures = [item for item in blockers if item not in core_blockers]
        score = float(result.get("score") or 0.0)
        if core_failures or len(secondary_failures) > max_secondary_blockers or score < min_score:
            return result
        reasons = list(result.get("reasons") or [])
        reasons.append(confirmation_reason)
        result.update(
            {
                "qualified": True,
                "blockers": [],
                "reasons": reasons,
                "confirmation_mode": "CORE_PLUS_CONFIRMATIONS",
                "accepted_secondary_blockers": secondary_failures,
            }
        )
        return result

    @staticmethod
    def _recovering_hourly_context(
        frame_15m: pd.DataFrame,
        frame_1h: pd.DataFrame,
        *,
        min_ema200_ratio: float = 0.965,
    ) -> bool:
        if len(frame_15m) < 2 or len(frame_1h) < 2:
            return False
        latest = frame_15m.iloc[-1]
        previous = frame_15m.iloc[-2]
        context = frame_1h.iloc[-1]
        context_previous = frame_1h.iloc[-2]
        ema200 = float(context["ema200"])
        if ema200 <= 0:
            return False
        return bool(
            float(context["close"]) >= ema200 * min_ema200_ratio
            and float(context["ema20"]) >= float(context_previous["ema20"])
            and float(latest["close"]) > float(latest["ema50"])
            and float(latest["rsi"]) >= 47.0
            and float(latest["macd_hist"]) >= float(previous["macd_hist"])
        )

    @staticmethod
    def detect_regime(frame_15m: pd.DataFrame, frame_1h: pd.DataFrame) -> str:
        regime = AuditedLongStrategyScanner.detect_regime(frame_15m, frame_1h)
        if regime == "TREND_DOWN" and OperationalLongStrategyScanner._recovering_hourly_context(
            frame_15m, frame_1h
        ):
            return "NEUTRAL"
        return regime

    @classmethod
    def trend_pullback_assessment(
        cls,
        frame_15m: pd.DataFrame,
        frame_1h: pd.DataFrame,
        regime: str,
    ) -> dict[str, Any]:
        result = super().trend_pullback_assessment(frame_15m, frame_1h, regime)
        blockers = list(result.get("blockers") or [])
        recent = frame_15m.tail(5)
        row = frame_15m.iloc[-1]
        wider_touch = bool(
            float(recent["low"].min()) <= float(row["ema20"]) * 1.010
            or float(recent["low"].min()) <= float(row["ema50"]) * 1.008
        )
        if "pullback non vicino a EMA20/EMA50" in blockers and wider_touch:
            result = cls._remove_blocker(
                result,
                "pullback non vicino a EMA20/EMA50",
                score_bonus=12.0,
                reason="pullback recente entro la zona EMA20/EMA50",
            )
        if "trend 1h non rialzista" in list(result.get("blockers") or []) and cls._recovering_hourly_context(
            frame_15m, frame_1h, min_ema200_ratio=0.97
        ):
            result = cls._remove_blocker(
                result,
                "trend 1h non rialzista",
                score_bonus=14.0,
                reason="contesto 1h in recupero confermato",
            )
        return cls._promote_assessment(
            result,
            core_blockers={
                "trend 1h non rialzista",
                "pullback non vicino a EMA20/EMA50",
                "manca recupero del prezzo",
                "prezzo sotto EMA50",
            },
            max_secondary_blockers=1,
            min_score=64.0,
            confirmation_reason="nucleo pullback valido con almeno due conferme tra RSI, MACD e volume",
        )

    @classmethod
    def breakout_assessment(
        cls,
        frame_15m: pd.DataFrame,
        frame_1h: pd.DataFrame,
        regime: str,
    ) -> dict[str, Any]:
        result = super().breakout_assessment(frame_15m, frame_1h, regime)
        return cls._promote_assessment(
            result,
            core_blockers={
                "massimo 20 candele non superato o non mantenuto",
                "contesto 1h ancora troppo ribassista",
            },
            max_secondary_blockers=1,
            min_score=66.0,
            confirmation_reason="breakout valido con almeno due conferme tra RSI, momentum e volume",
        )

    @classmethod
    def range_bounce_assessment(
        cls,
        frame_15m: pd.DataFrame,
        frame_1h: pd.DataFrame,
        regime: str,
    ) -> dict[str, Any]:
        result = super().range_bounce_assessment(frame_15m, frame_1h, regime)
        blockers = list(result.get("blockers") or [])
        recent = frame_15m.tail(5)
        wider_touch = bool((recent["low"] <= recent["bb_lower"] * 1.010).any())
        if "nessun test recente della banda inferiore" in blockers and wider_touch:
            result = cls._remove_blocker(
                result,
                "nessun test recente della banda inferiore",
                score_bonus=12.0,
                reason="test della banda inferiore rilevato nelle ultime cinque candele",
            )
        return cls._promote_assessment(
            result,
            core_blockers={
                "mercato non laterale/neutrale",
                "prezzo 1h troppo sotto EMA200",
                "nessun test recente della banda inferiore",
                "manca rientro rialzista nelle Bollinger",
                "spazio ridotto verso la media Bollinger",
            },
            max_secondary_blockers=1,
            min_score=64.0,
            confirmation_reason="rimbalzo dal range valido con recupero RSI o volume sufficiente",
        )

    @classmethod
    def oversold_reversal_assessment(
        cls,
        frame_15m: pd.DataFrame,
        frame_1h: pd.DataFrame,
        regime: str,
    ) -> dict[str, Any]:
        result = super().oversold_reversal_assessment(frame_15m, frame_1h, regime)
        return cls._promote_assessment(
            result,
            core_blockers={
                "strategia riservata a inversioni da debolezza",
                "trend 1h troppo distante da EMA200",
                "EMA20 non ancora recuperata",
                "manca candela rialzista di conferma",
            },
            max_secondary_blockers=1,
            min_score=72.0,
            confirmation_reason="reversal LONG confermato da almeno due segnali tra RSI, MACD e volume",
        )

    @classmethod
    def relative_strength_assessment(
        cls,
        frame_15m: pd.DataFrame,
        frame_1h: pd.DataFrame,
        regime: str,
        relative: dict[str, float],
    ) -> dict[str, Any]:
        result = super().relative_strength_assessment(frame_15m, frame_1h, regime, relative)
        if "moneta non tra i leader relativi a 24h" in list(result.get("blockers") or []):
            operational_leader = bool(
                float(relative.get("rank_percentile", 0.0)) >= 0.60
                and float(relative.get("relative_24h", -1.0)) >= 0.0
            )
            if operational_leader:
                result = cls._remove_blocker(
                    result,
                    "moneta non tra i leader relativi a 24h",
                    score_bonus=16.0,
                    reason="moneta nel 40% più forte e non inferiore a BTC nelle 24h",
                )
        if "trend 1h non favorevole" in list(result.get("blockers") or []) and cls._recovering_hourly_context(
            frame_15m, frame_1h, min_ema200_ratio=0.97
        ):
            result = cls._remove_blocker(
                result,
                "trend 1h non favorevole",
                score_bonus=12.0,
                reason="forza relativa sostenuta da un contesto 1h in recupero",
            )
        return cls._promote_assessment(
            result,
            core_blockers={
                "moneta non tra i leader relativi a 24h",
                "trend 1h non favorevole",
                "prezzo 15m non sopra EMA20/EMA50",
            },
            max_secondary_blockers=1,
            min_score=68.0,
            confirmation_reason="leadership relativa valida con almeno due conferme di breve periodo",
        )

    @classmethod
    def volatility_squeeze_assessment(
        cls,
        frame_15m: pd.DataFrame,
        frame_1h: pd.DataFrame,
        regime: str,
    ) -> dict[str, Any]:
        result = super().volatility_squeeze_assessment(frame_15m, frame_1h, regime)
        if "compressione Bollinger/ATR non sufficiente" in list(result.get("blockers") or []):
            pre_breakout = frame_15m.iloc[-8:-1]
            operational_squeeze = bool(
                float(pre_breakout["bb_width_ratio"].min()) <= 0.90
                and float(pre_breakout["atr_ratio"].min()) <= 0.98
            )
            if operational_squeeze:
                result = cls._remove_blocker(
                    result,
                    "compressione Bollinger/ATR non sufficiente",
                    score_bonus=16.0,
                    reason="compressione moderata di volatilità prima del breakout",
                )
        if "contesto 1h ribassista" in list(result.get("blockers") or []) and cls._recovering_hourly_context(
            frame_15m, frame_1h, min_ema200_ratio=0.97
        ):
            result = cls._remove_blocker(
                result,
                "contesto 1h ribassista",
                score_bonus=10.0,
                reason="contesto 1h in recupero durante il breakout",
            )
        return cls._promote_assessment(
            result,
            core_blockers={
                "compressione Bollinger/ATR non sufficiente",
                "rottura rialzista non confermata",
                "contesto 1h ribassista",
            },
            max_secondary_blockers=1,
            min_score=68.0,
            confirmation_reason="squeeze breakout valido con espansione o momentum confermato",
        )

    @staticmethod
    def economic_rr_candidates(initial_rr: float) -> list[float]:
        values = {
            round(max(1.20, float(initial_rr)), 2),
            2.00,
            2.20,
            2.50,
            2.80,
            3.20,
        }
        return sorted(value for value in values if value >= float(initial_rr))

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
        if signal is not None:
            if assessment.get("confirmation_mode") == "CORE_PLUS_CONFIRMATIONS":
                self._adaptive_confirmations_used += 1
                signal = replace(
                    signal,
                    score_breakdown={
                        **(signal.score_breakdown or {}),
                        "confirmation_mode": "CORE_PLUS_CONFIRMATIONS",
                    },
                )
            return signal, reason, diagnostic
        if reason not in {"NET_PROFIT_TOO_LOW", "NET_RR_TOO_LOW"}:
            return signal, reason, diagnostic

        original_rr = float(assessment.get("target_rr") or 1.20)
        for target_rr in self.economic_rr_candidates(original_rr):
            if target_rr <= original_rr + 1e-9:
                continue
            adjusted = dict(assessment)
            adjusted["target_rr"] = target_rr
            rescued, rescued_reason, rescued_diagnostic = super()._build_signal_for_assessment(
                pair, frame_15m, regime, relative, adjusted
            )
            if rescued is None:
                continue
            reasons = list(rescued.reasons or [])
            reasons.append("target ampliato entro il limite tecnico per coprire costi e R/R netto")
            breakdown = dict(rescued.score_breakdown or {})
            breakdown.update(
                {
                    "economic_target_adjusted": True,
                    "initial_target_rr": round(original_rr, 3),
                    "adjusted_target_rr": round(target_rr, 3),
                }
            )
            if assessment.get("confirmation_mode") == "CORE_PLUS_CONFIRMATIONS":
                self._adaptive_confirmations_used += 1
                breakdown["confirmation_mode"] = "CORE_PLUS_CONFIRMATIONS"
            self._economic_targets_adjusted += 1
            rescued_diagnostic["economic_target_adjusted"] = True
            return replace(rescued, reasons=reasons, score_breakdown=breakdown), rescued_reason, rescued_diagnostic
        return signal, reason, diagnostic

    def evaluate(self) -> GeneratedSignal | None:
        self._adaptive_confirmations_used = 0
        self._economic_targets_adjusted = 0
        result = super().evaluate()
        self._last_cycle_summary["adaptive_confirmations_used"] = self._adaptive_confirmations_used
        self._last_cycle_summary["economic_targets_adjusted"] = self._economic_targets_adjusted
        self.logger.info(
            "OPERATIONAL_LONG_V4 adaptive_confirmations=%s economic_targets_adjusted=%s",
            self._adaptive_confirmations_used,
            self._economic_targets_adjusted,
        )
        return result
