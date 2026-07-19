"""Seven live LONG strategies with harmonic patterns and loss-streak safeguards."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd

import project.audited_long_scanner as audited_module
import project.enhanced_long_scanner as enhanced_module
from project.harmonic_patterns import HARMONIC_STRATEGY, detect_bullish_harmonic
from project.operational_long_scanner import OPERATIONAL_LONG_STRATEGIES, OperationalLongStrategyScanner
from project.strategy_engine.service import GeneratedSignal


LIVE_LONG_STRATEGIES = (*OPERATIONAL_LONG_STRATEGIES, HARMONIC_STRATEGY)
# AuditedLongStrategyScanner reads this module-level tuple while building strategy states.
# Keep the legacy enhanced module unchanged so its six-strategy tests remain isolated.
audited_module.ENHANCED_LONG_STRATEGIES = LIVE_LONG_STRATEGIES


class HarmonicLiveStrategyScanner(OperationalLongStrategyScanner):
    def __init__(
        self,
        *args: Any,
        max_net_loss_eur: float = 1.00,
        loss_streak_trigger: int = 3,
        loss_guard_hours: int = 12,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.max_net_loss_eur = max(0.25, float(max_net_loss_eur))
        self.loss_streak_trigger = max(2, int(loss_streak_trigger))
        self.loss_guard_hours = max(3, int(loss_guard_hours))
        self._loss_streak = 0
        self._loss_guard_active = False
        self._building_assessment: dict[str, Any] | None = None

    @classmethod
    def harmonic_assessment(
        cls, frame_15m: pd.DataFrame, frame_1h: pd.DataFrame, regime: str
    ) -> dict[str, Any]:
        row, previous = frame_15m.iloc[-1], frame_15m.iloc[-2]
        context = frame_1h.iloc[-1]
        pattern = detect_bullish_harmonic(frame_15m)
        if pattern is None:
            return cls._assessment(
                HARMONIC_STRATEGY,
                0,
                False,
                [],
                ["nessun pattern armonico bullish completato"],
                1.80,
            )
        d_price, atr = float(pattern["points"]["D"]), float(row["atr"])
        confirmations = [
            bool(row["bullish"]) and float(row["close"]) > float(previous["close"]),
            28 <= float(row["rsi"]) <= 60 and float(row["rsi"]) > float(previous["rsi"]),
            float(row["macd_hist"]) > float(previous["macd_hist"]),
            float(row["close"]) >= d_price + atr * 0.20,
        ]
        count = sum(confirmations)
        not_chasing = float(row["close"]) <= d_price + atr * 1.60
        context_safe = float(context["close"]) >= float(context["ema200"]) * 0.82
        target_room = float(pattern["target_382"]) >= float(row["close"]) + atr * 1.20
        score = 45 + float(pattern["quality"]) * 25 + count * 7
        score += 6 if context_safe else 0
        score += 5 if target_room else 0
        blockers = []
        if count < 3:
            blockers.append("conferma rialzista insufficiente dopo il punto D")
        if not not_chasing:
            blockers.append("prezzo già troppo distante dalla zona D")
        if not context_safe:
            blockers.append("contesto 1h in capitolazione")
        if not target_room:
            blockers.append("spazio insufficiente verso il target armonico")
        result = cls._assessment(
            HARMONIC_STRATEGY,
            score,
            count >= 3 and not_chasing and context_safe and target_room and score >= 78,
            [
                f"pattern bullish {pattern['pattern']} completato",
                "punto D confermato su candele chiuse",
                f"conferme={count}/4",
            ],
            blockers,
            1.80,
        )
        result.update(
            {
                "harmonic_pattern": pattern["pattern"],
                "harmonic_quality": round(float(pattern["quality"]), 4),
                "harmonic_points": pattern["points"],
                "harmonic_target": float(pattern["target_382"]),
            }
        )
        return result

    def evaluate_pair_detailed(
        self, pair: str
    ) -> tuple[GeneratedSignal | None, str, dict[str, Any]]:
        f15 = self.fetch_closed_frame(pair, "15m", 260)
        f1h = self.fetch_closed_frame(pair, "1h", 260)
        if len(f15) < 210 or len(f1h) < 210:
            return None, "INSUFFICIENT_OHLC", {
                "pair": pair,
                "regime": "UNKNOWN",
                "strategy": "NONE",
                "score": 0.0,
                "blockers": [f"storico insufficiente 15m={len(f15)} 1h={len(f1h)}"],
            }
        f15, f1h = self.add_indicators(f15), self.add_indicators(f1h)
        self._frame_cache[pair] = f15
        latest = f15.iloc[-1]
        regime = self.detect_regime(f15, f1h)
        relative = self._relative_strength_snapshot.get(pair, {})
        assessments = [
            self.trend_pullback_assessment(f15, f1h, regime),
            self.breakout_assessment(f15, f1h, regime),
            self.range_bounce_assessment(f15, f1h, regime),
            self.oversold_reversal_assessment(f15, f1h, regime),
            self.relative_strength_assessment(f15, f1h, regime, relative),
            self.volatility_squeeze_assessment(f15, f1h, regime),
            self.harmonic_assessment(f15, f1h, regime),
        ]
        ordered = sorted(
            assessments,
            key=lambda item: self.assessment_priority(item, self._strategy_states),
            reverse=True,
        )
        fallback, last_failure = ordered[0], None
        for assessment in ordered:
            if not assessment.get("qualified"):
                continue
            signal, reason, diagnostic = self._build_signal_for_assessment(
                pair, f15, regime, relative, assessment
            )
            if signal is not None:
                return signal, reason, diagnostic
            if last_failure is None:
                last_failure = (reason, diagnostic)
        if last_failure:
            return None, last_failure[0], last_failure[1]
        strategy = str(fallback.get("strategy") or "NONE")
        return None, f"NO_SETUP_{regime}", {
            "pair": pair,
            "regime": regime,
            "strategy": strategy,
            "strategy_state": str(
                self._strategy_states.get(strategy, {}).get("state") or "ACTIVE"
            ),
            "score": float(fallback.get("score") or 0),
            "qualified": False,
            "blockers": list(fallback.get("blockers") or [])[:3],
            "rsi": round(float(latest["rsi"]), 1),
            "volume_ratio": round(float(latest["volume_ratio"]), 2),
            "relative_24h_pct": round(float(relative.get("relative_24h", 0)) * 100, 2),
            "candle_time": str(latest["timestamp"]),
        }

    def fetch_forward_performance(self) -> dict[str, dict[str, Any]]:
        placeholders = ", ".join(["%s"] * len(LIVE_LONG_STRATEGIES))
        rows = self.client.fetch_all(
            f"""SELECT strategy, status, net_profit_tp1_eur, net_loss_sl_eur, closed_at
            FROM signals.generated_signals
            WHERE strategy IN ({placeholders})
              AND status IN ('TARGET_HIT', 'STOP_LOSS')
              AND created_at >= NOW() - INTERVAL '120 days'
            ORDER BY closed_at ASC, id ASC""",
            LIVE_LONG_STRATEGIES,
        )
        grouped: dict[str, list[tuple[Any, ...]]] = {
            name: [] for name in LIVE_LONG_STRATEGIES
        }
        for row in rows:
            grouped.setdefault(str(row[0]), []).append(row)
        return {
            strategy: self.calculate_enhanced_performance(items)
            for strategy, items in grouped.items()
        }

    def publish_daily_signal_report(self) -> bool:
        # The inherited formatter iterates the enhanced-module tuple. Patch it only for
        # the duration of this runtime call, then restore it immediately.
        original = enhanced_module.ENHANCED_LONG_STRATEGIES
        enhanced_module.ENHANCED_LONG_STRATEGIES = LIVE_LONG_STRATEGIES
        try:
            return super().publish_daily_signal_report()
        finally:
            enhanced_module.ENHANCED_LONG_STRATEGIES = original

    def fetch_loss_streak(self) -> tuple[int, datetime | None]:
        placeholders = ", ".join(["%s"] * len(LIVE_LONG_STRATEGIES))
        rows = self.client.fetch_all(
            f"""SELECT status, closed_at FROM signals.generated_signals
            WHERE strategy IN ({placeholders}) AND status IN ('TARGET_HIT','STOP_LOSS')
              AND closed_at IS NOT NULL AND closed_at >= NOW() - INTERVAL '30 days'
            ORDER BY closed_at DESC, id DESC LIMIT 12""",
            LIVE_LONG_STRATEGIES,
        )
        streak, last_closed = 0, None
        for status, closed_at in rows:
            if last_closed is None and isinstance(closed_at, datetime):
                last_closed = (
                    closed_at
                    if closed_at.tzinfo
                    else closed_at.replace(tzinfo=timezone.utc)
                )
            if str(status) != "STOP_LOSS":
                break
            streak += 1
        return streak, last_closed

    def stop_distance_for_strategy(
        self, strategy: str, frame: pd.DataFrame, entry: float, atr: float
    ) -> float:
        if atr <= 0:
            return 0.0
        if strategy == HARMONIC_STRATEGY:
            points = (self._building_assessment or {}).get("harmonic_points") or {}
            d_price = float(points.get("D", entry - atr * 1.5))
            distance = max(atr * 1.55, entry - (d_price - atr * 0.30))
        else:
            distance = super().stop_distance_for_strategy(strategy, frame, entry, atr)
            floors = {
                "Trend Pullback": 1.45,
                "Breakout 20": 1.35,
                "Range Bounce Bollinger": 1.35,
                "Oversold Reversal": 1.60,
                "Relative Strength Momentum": 1.45,
                "Volatility Squeeze Breakout": 1.40,
            }
            distance = max(distance, atr * floors.get(strategy, 1.45))
        return min(max(distance, entry * 0.004), entry * 0.025)

    def technical_target(
        self,
        strategy: str,
        frame: pd.DataFrame,
        entry: float,
        stop_distance: float,
        atr: float,
        target_rr: float,
    ) -> float:
        if strategy != HARMONIC_STRATEGY:
            return super().technical_target(
                strategy, frame, entry, stop_distance, atr, target_rr
            )
        target = float(
            (self._building_assessment or {}).get(
                "harmonic_target", entry + stop_distance * target_rr
            )
        )
        return max(
            entry + stop_distance * 1.30,
            min(target, entry * 1.03, entry + atr * 3.0),
        )

    def calculate_net_economics(
        self, entry: float, stop_loss: float, take_profit: float
    ) -> dict[str, float | bool]:
        original = self.trade_notional_eur
        result = super().calculate_net_economics(entry, stop_loss, take_profit)
        loss = float(result.get("net_loss_sl_eur") or 0)
        if loss <= self.max_net_loss_eur or original <= self.min_notional_eur:
            return result
        adjusted_notional = max(
            self.min_notional_eur,
            original * self.max_net_loss_eur / max(loss, 1e-9) * 0.98,
        )
        try:
            self.trade_notional_eur = adjusted_notional
            adjusted = super().calculate_net_economics(entry, stop_loss, take_profit)
        finally:
            self.trade_notional_eur = original
        adjusted["risk_capped"] = True
        if float(adjusted.get("net_loss_sl_eur") or 0) > self.max_net_loss_eur * 1.05:
            adjusted["tradable"] = False
        return adjusted

    def _build_signal_for_assessment(
        self,
        pair: str,
        frame_15m: pd.DataFrame,
        regime: str,
        relative: dict[str, float],
        assessment: dict[str, Any],
    ) -> tuple[GeneratedSignal | None, str, dict[str, Any]]:
        strategy, latest = str(assessment.get("strategy") or ""), frame_15m.iloc[-1]
        atr = float(latest["atr"])
        if (
            strategy != HARMONIC_STRATEGY
            and atr > 0
            and float(latest["close"]) > float(latest["ema20"]) + atr * 1.80
        ):
            return None, "ENTRY_TOO_EXTENDED", {
                "pair": pair,
                "regime": regime,
                "strategy": strategy,
                "score": float(assessment.get("score") or 0),
                "qualified": True,
                "blockers": ["ingresso troppo distante da EMA20 rispetto all'ATR"],
                "rsi": round(float(latest["rsi"]), 1),
                "volume_ratio": round(float(latest["volume_ratio"]), 2),
                "candle_time": str(latest["timestamp"]),
            }
        if (
            self._loss_guard_active
            and strategy != HARMONIC_STRATEGY
            and assessment.get("confirmation_mode") == "CORE_PLUS_CONFIRMATIONS"
        ):
            return None, "LOSS_STREAK_STRICT_MODE", {
                "pair": pair,
                "regime": regime,
                "strategy": strategy,
                "score": float(assessment.get("score") or 0),
                "qualified": True,
                "blockers": [
                    f"setup adattivo sospeso dopo {self._loss_streak} stop consecutivi"
                ],
                "rsi": round(float(latest["rsi"]), 1),
                "volume_ratio": round(float(latest["volume_ratio"]), 2),
                "candle_time": str(latest["timestamp"]),
            }
        self._building_assessment = assessment
        try:
            signal, reason, diagnostic = super()._build_signal_for_assessment(
                pair, frame_15m, regime, relative, assessment
            )
        finally:
            self._building_assessment = None
        if signal is None:
            return signal, reason, diagnostic
        breakdown, reasons = dict(signal.score_breakdown or {}), list(signal.reasons or [])
        if signal.entry_notional_eur < self.trade_notional_eur * 0.99:
            breakdown.update(
                {"risk_sized": True, "max_net_loss_eur": round(self.max_net_loss_eur, 2)}
            )
            reasons.append(
                f"posizione ridotta per limitare la perdita netta a circa €{self.max_net_loss_eur:.2f}"
            )
        if strategy == HARMONIC_STRATEGY:
            breakdown.update(
                {
                    "harmonic_live": True,
                    "harmonic_quality": float(assessment.get("harmonic_quality") or 0),
                }
            )
        return replace(signal, score_breakdown=breakdown, reasons=reasons), reason, diagnostic

    def evaluate(self) -> GeneratedSignal | None:
        streak, last_closed = self.fetch_loss_streak()
        self._loss_streak = streak
        self._loss_guard_active = bool(
            streak >= self.loss_streak_trigger
            and last_closed
            and datetime.now(timezone.utc)
            < last_closed + timedelta(hours=self.loss_guard_hours)
        )
        original = (
            self.max_signals_per_cycle,
            self.min_signal_score,
            self.min_net_rr,
            self.min_tp1_net_profit_eur,
        )
        if self._loss_guard_active:
            self.max_signals_per_cycle = 1
            self.min_signal_score = max(self.min_signal_score, 78)
            self.min_net_rr = max(self.min_net_rr, 1.25)
            self.min_tp1_net_profit_eur = max(self.min_tp1_net_profit_eur, 0.30)
        try:
            result = super().evaluate()
        finally:
            (
                self.max_signals_per_cycle,
                self.min_signal_score,
                self.min_net_rr,
                self.min_tp1_net_profit_eur,
            ) = original
        self._last_cycle_summary.update(
            {
                "live_strategies": 7,
                "loss_streak": streak,
                "loss_guard_active": self._loss_guard_active,
                "max_net_loss_eur": self.max_net_loss_eur,
            }
        )
        self.logger.info(
            "HARMONIC_LIVE_V5 strategies=7 loss_streak=%s loss_guard=%s max_net_loss_eur=%.2f",
            streak,
            self._loss_guard_active,
            self.max_net_loss_eur,
        )
        return result
