"""Optimized LONG-only scanner with transparent rules and forward performance tracking.

This module deliberately avoids parameter grids and continuous research. It evaluates a
small fixed set of readable strategies on closed candles, records near-setups for
calibration, and measures profitability only from signals actually closed by the forward
position monitor.
"""
from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

import pandas as pd

from project.reliable_simple_scanner import ReliableSimpleStrategyScanner
from project.shared.events import Event, EventBus, EventType
from project.shared.logging import get_module_logger
from project.strategy_engine.service import GeneratedSignal


SIMPLE_STRATEGIES = (
    "Trend Pullback",
    "Breakout 20",
    "Range Bounce Bollinger",
    "Oversold Reversal",
)


class OptimizedLongStrategyScanner(ReliableSimpleStrategyScanner):
    """Scan 20 markets with four fixed LONG strategies and no short exposure."""

    def __init__(
        self,
        *args: Any,
        min_signal_score: float = 68.0,
        min_net_rr: float = 1.10,
        min_net_profit_eur: float = 0.25,
        max_signals_per_cycle: int = 2,
        performance_guard_min_trades: int = 20,
        performance_guard_min_pf: float = 0.85,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            *args,
            min_signal_score=min_signal_score,
            min_net_rr=min_net_rr,
            min_net_profit_eur=min_net_profit_eur,
            max_signals_per_cycle=max_signals_per_cycle,
            **kwargs,
        )
        self.performance_guard_min_trades = max(10, int(performance_guard_min_trades))
        self.performance_guard_min_pf = max(0.0, float(performance_guard_min_pf))
        self._pair_diagnostics: list[dict[str, Any]] = []
        self._performance_cache: dict[str, dict[str, float | int | str]] = {}
        self.logger = get_module_logger("optimized-long-scanner")

    @staticmethod
    def add_indicators(frame: pd.DataFrame) -> pd.DataFrame:
        data = ReliableSimpleStrategyScanner.add_indicators(frame)
        close = data["close"]
        ema12 = close.ewm(span=12, adjust=False).mean()
        ema26 = close.ewm(span=26, adjust=False).mean()
        data["macd"] = ema12 - ema26
        data["macd_signal"] = data["macd"].ewm(span=9, adjust=False).mean()
        data["macd_hist"] = data["macd"] - data["macd_signal"]
        data["rsi_delta"] = data["rsi"].diff()
        data["bullish"] = data["close"] > data["open"]
        data["ema20_distance_pct"] = (
            (data["close"] - data["ema20"]).abs()
            / data["close"].replace(0, float("nan"))
            * 100.0
        ).fillna(0.0)
        return data

    @staticmethod
    def detect_regime(frame_15m: pd.DataFrame, frame_1h: pd.DataFrame) -> str:
        latest = frame_15m.iloc[-1]
        context = frame_1h.iloc[-1]
        close = float(context["close"])
        ema20 = float(context["ema20"])
        ema50 = float(context["ema50"])
        ema200 = float(context["ema200"])
        ema_gap = abs(ema20 - ema50) / close if close else 0.0

        if close >= ema200 * 0.995 and ema20 > ema50:
            return "TREND_UP"
        if close < ema200 * 0.995 and ema20 < ema50:
            return "TREND_DOWN"
        if float(latest["bb_width"]) <= 0.035 or ema_gap <= 0.008:
            return "RANGE"
        return "NEUTRAL"

    @staticmethod
    def _assessment(
        strategy: str,
        score: float,
        qualified: bool,
        reasons: list[str],
        blockers: list[str],
        target_rr: float,
    ) -> dict[str, Any]:
        return {
            "strategy": strategy,
            "score": round(min(100.0, max(0.0, score)), 2),
            "qualified": bool(qualified),
            "reasons": reasons,
            "blockers": blockers,
            "target_rr": float(target_rr),
        }

    @classmethod
    def trend_pullback_assessment(
        cls,
        frame_15m: pd.DataFrame,
        frame_1h: pd.DataFrame,
        regime: str,
    ) -> dict[str, Any]:
        row = frame_15m.iloc[-1]
        previous = frame_15m.iloc[-2]
        context = frame_1h.iloc[-1]
        recent = frame_15m.tail(3)

        trend_ok = (
            regime in {"TREND_UP", "NEUTRAL"}
            and float(context["close"]) >= float(context["ema200"]) * 0.99
            and float(context["ema20"]) > float(context["ema50"])
        )
        support_touch = (
            float(recent["low"].min()) <= float(row["ema20"]) * 1.006
            or float(recent["low"].min()) <= float(row["ema50"]) * 1.004
            or float(row["ema20_distance_pct"]) <= 0.75
        )
        recovery = (
            float(row["close"]) > float(row["ema20"]) * 0.998
            and (
                bool(row["bullish"])
                or float(row["close"]) > float(previous["close"])
            )
        )
        rsi_ok = 42.0 <= float(row["rsi"]) <= 69.0
        momentum_ok = (
            float(row["macd_hist"]) > float(previous["macd_hist"])
            or float(row["macd_hist"]) > 0
        )
        volume_ok = float(row["volume_ratio"]) >= 0.60
        structure_ok = float(row["close"]) >= float(row["ema50"]) * 0.995

        score = 0.0
        score += 25 if trend_ok else 0
        score += 20 if support_touch else 4
        score += 18 if recovery else 0
        score += 10 if rsi_ok else 0
        score += 12 if momentum_ok else 0
        score += 8 if volume_ok else 2
        score += 7 if structure_ok else 0
        blockers = []
        if not trend_ok:
            blockers.append("trend 1h non rialzista")
        if not support_touch:
            blockers.append("pullback non vicino a EMA20/EMA50")
        if not recovery:
            blockers.append("manca recupero del prezzo")
        if not rsi_ok:
            blockers.append("RSI fuori fascia 42-69")
        if not momentum_ok:
            blockers.append("momentum MACD non in miglioramento")
        if not volume_ok:
            blockers.append("volume sotto 0,60 della media")
        if not structure_ok:
            blockers.append("prezzo sotto EMA50")
        qualified = (
            trend_ok
            and support_touch
            and recovery
            and rsi_ok
            and momentum_ok
            and structure_ok
            and score >= 68
        )
        reasons = [
            "trend 1h favorevole",
            "pullback verso media dinamica",
            "recupero del momentum 15m",
        ]
        return cls._assessment(
            "Trend Pullback", score, qualified, reasons, blockers, target_rr=1.80
        )

    @classmethod
    def breakout_assessment(
        cls,
        frame_15m: pd.DataFrame,
        frame_1h: pd.DataFrame,
        regime: str,
    ) -> dict[str, Any]:
        row = frame_15m.iloc[-1]
        previous = frame_15m.iloc[-2]
        context = frame_1h.iloc[-1]
        level = float(row["high20_prev"])
        previous_level = float(previous["high20_prev"])
        breakout_now = (
            float(row["high"]) > level
            and float(row["close"]) >= level * 0.998
        )
        held_previous_breakout = (
            float(previous["close"]) > previous_level
            and float(row["low"]) >= level * 0.995
            and float(row["close"]) >= level
        )
        breakout_ok = breakout_now or held_previous_breakout
        context_ok = (
            regime != "TREND_DOWN"
            or (
                float(context["close"]) >= float(context["ema200"]) * 0.97
                and float(row["close"]) > float(row["ema50"])
            )
        )
        rsi_ok = 48.0 <= float(row["rsi"]) <= 76.0
        volume_ok = float(row["volume_ratio"]) >= 0.85
        momentum_ok = (
            float(row["macd_hist"]) >= float(previous["macd_hist"])
            or float(row["close"]) > float(row["ema20"]) > float(row["ema50"])
        )

        score = 0.0
        score += 32 if breakout_ok else 5
        score += 20 if context_ok else 0
        score += 14 if rsi_ok else 0
        score += min(16.0, max(0.0, float(row["volume_ratio"]) * 10.0))
        score += 12 if momentum_ok else 2
        score += 6 if bool(row["bullish"]) else 2
        blockers = []
        if not breakout_ok:
            blockers.append("massimo 20 candele non superato o non mantenuto")
        if not context_ok:
            blockers.append("contesto 1h ancora troppo ribassista")
        if not rsi_ok:
            blockers.append("RSI fuori fascia 48-76")
        if not volume_ok:
            blockers.append("volume sotto 0,85 della media")
        if not momentum_ok:
            blockers.append("momentum non conferma la rottura")
        qualified = (
            breakout_ok
            and context_ok
            and rsi_ok
            and volume_ok
            and momentum_ok
            and score >= 68
        )
        reasons = [
            "rottura o retest del massimo 20 candele",
            "volume e momentum confermano",
            "contesto 1h compatibile con un LONG",
        ]
        return cls._assessment(
            "Breakout 20", score, qualified, reasons, blockers, target_rr=1.90
        )

    @classmethod
    def range_bounce_assessment(
        cls,
        frame_15m: pd.DataFrame,
        frame_1h: pd.DataFrame,
        regime: str,
    ) -> dict[str, Any]:
        row = frame_15m.iloc[-1]
        previous = frame_15m.iloc[-2]
        context = frame_1h.iloc[-1]
        recent = frame_15m.tail(3)
        regime_ok = regime in {"RANGE", "NEUTRAL"}
        context_ok = float(context["close"]) >= float(context["ema200"]) * 0.965
        touched_lower = bool(
            (recent["low"] <= recent["bb_lower"] * 1.005).any()
        )
        reentry = (
            float(row["close"]) > float(row["bb_lower"])
            and (
                bool(row["bullish"])
                or float(row["close"]) > float(previous["close"])
            )
        )
        rsi_ok = 30.0 <= float(row["rsi"]) <= 55.0
        rsi_recovery = float(row["rsi"]) > float(previous["rsi"]) - 0.5
        room_to_mean = float(row["close"]) < float(row["bb_middle"]) * 1.005
        volume_ok = float(row["volume_ratio"]) >= 0.50

        score = 0.0
        score += 20 if regime_ok else 0
        score += 12 if context_ok else 0
        score += 22 if touched_lower else 3
        score += 18 if reentry else 0
        score += 10 if rsi_ok else 0
        score += 8 if rsi_recovery else 0
        score += 6 if room_to_mean else 0
        score += 4 if volume_ok else 0
        blockers = []
        if not regime_ok:
            blockers.append("mercato non laterale/neutrale")
        if not context_ok:
            blockers.append("prezzo 1h troppo sotto EMA200")
        if not touched_lower:
            blockers.append("nessun test recente della banda inferiore")
        if not reentry:
            blockers.append("manca rientro rialzista nelle Bollinger")
        if not rsi_ok or not rsi_recovery:
            blockers.append("RSI non mostra recupero")
        if not room_to_mean:
            blockers.append("spazio ridotto verso la media Bollinger")
        if not volume_ok:
            blockers.append("volume sotto 0,50 della media")
        qualified = (
            regime_ok
            and context_ok
            and touched_lower
            and reentry
            and rsi_ok
            and rsi_recovery
            and room_to_mean
            and score >= 68
        )
        reasons = [
            "rimbalzo dalla banda Bollinger inferiore",
            "RSI in recupero",
            "spazio disponibile verso la media del range",
        ]
        return cls._assessment(
            "Range Bounce Bollinger", score, qualified, reasons, blockers, target_rr=1.55
        )

    @classmethod
    def oversold_reversal_assessment(
        cls,
        frame_15m: pd.DataFrame,
        frame_1h: pd.DataFrame,
        regime: str,
    ) -> dict[str, Any]:
        row = frame_15m.iloc[-1]
        previous = frame_15m.iloc[-2]
        before = frame_15m.iloc[-3]
        context = frame_1h.iloc[-1]
        regime_ok = regime in {"TREND_DOWN", "NEUTRAL"}
        not_capitulation = float(context["close"]) >= float(context["ema200"]) * 0.90
        ema_reclaim = (
            float(row["close"]) > float(row["ema20"])
            and (
                float(previous["close"]) <= float(previous["ema20"])
                or float(row["close"]) > float(previous["high"])
            )
        )
        rsi_reversal = (
            35.0 <= float(row["rsi"]) <= 60.0
            and float(row["rsi"]) >= float(previous["rsi"]) + 1.5
        )
        macd_reversal = (
            float(row["macd_hist"]) > float(previous["macd_hist"])
            and float(previous["macd_hist"]) >= float(before["macd_hist"])
        )
        bullish_confirmation = bool(row["bullish"]) and float(row["close"]) > float(previous["close"])
        volume_ok = float(row["volume_ratio"]) >= 0.80

        score = 0.0
        score += 14 if regime_ok else 0
        score += 12 if not_capitulation else 0
        score += 26 if ema_reclaim else 0
        score += 18 if rsi_reversal else 0
        score += 16 if macd_reversal else 0
        score += 8 if bullish_confirmation else 0
        score += 6 if volume_ok else 0
        blockers = []
        if not regime_ok:
            blockers.append("strategia riservata a inversioni da debolezza")
        if not not_capitulation:
            blockers.append("trend 1h troppo distante da EMA200")
        if not ema_reclaim:
            blockers.append("EMA20 non ancora recuperata")
        if not rsi_reversal:
            blockers.append("RSI non ha confermato l'inversione")
        if not macd_reversal:
            blockers.append("MACD non migliora per due candele")
        if not bullish_confirmation:
            blockers.append("manca candela rialzista di conferma")
        if not volume_ok:
            blockers.append("volume sotto 0,80 della media")
        qualified = (
            regime_ok
            and not_capitulation
            and ema_reclaim
            and rsi_reversal
            and macd_reversal
            and bullish_confirmation
            and volume_ok
            and score >= 78
        )
        reasons = [
            "recupero confermato di EMA20",
            "RSI e MACD in inversione",
            "candela rialzista sostenuta dai volumi",
        ]
        return cls._assessment(
            "Oversold Reversal", score, qualified, reasons, blockers, target_rr=1.60
        )

    def evaluate(self) -> GeneratedSignal | None:
        self._pair_diagnostics = []
        self._performance_cache = self.fetch_forward_performance()
        candidates: list[GeneratedSignal] = []
        rejection_reasons: dict[str, int] = {}
        regime_counts: dict[str, int] = {}

        for pair in self.pairs:
            try:
                candidate, reason, diagnostic = self.evaluate_pair_detailed(pair)
            except Exception:
                self.logger.exception("OPTIMIZED_LONG pair_failed pair=%s", pair)
                candidate = None
                reason = "PAIR_ERROR"
                diagnostic = {
                    "pair": pair,
                    "regime": "UNKNOWN",
                    "strategy": "NONE",
                    "score": 0.0,
                    "blockers": ["errore durante l'analisi"],
                }
            self._pair_diagnostics.append(diagnostic)
            regime = str(diagnostic.get("regime") or "UNKNOWN")
            regime_counts[regime] = regime_counts.get(regime, 0) + 1
            if candidate is not None:
                candidates.append(candidate)
            else:
                rejection_reasons[reason] = rejection_reasons.get(reason, 0) + 1

        candidates.sort(key=lambda item: item.score, reverse=True)
        published: list[GeneratedSignal] = []
        for candidate in candidates:
            if len(published) >= self.max_signals_per_cycle:
                break
            duplicate_id = self.find_active_duplicate(candidate)
            if duplicate_id is not None:
                rejection_reasons["ACTIVE_PAIR_COOLDOWN"] = (
                    rejection_reasons.get("ACTIVE_PAIR_COOLDOWN", 0) + 1
                )
                continue
            signal_id = self.save_signal(candidate)
            signal = replace(candidate, signal_id=signal_id)
            self.publish_signal_event(signal)
            published.append(signal)
            self.logger.info(
                "OPTIMIZED_LONG_SIGNAL id=%s pair=%s strategy=%s score=%.1f "
                "entry=%.8f stop=%.8f target=%.8f net_rr=%.3f",
                signal_id,
                signal.pair,
                signal.strategy,
                signal.score,
                signal.entry,
                signal.stop_loss,
                signal.take_profit,
                signal.net_rr,
            )

        ranked_diagnostics = sorted(
            self._pair_diagnostics,
            key=lambda item: float(item.get("score") or 0.0),
            reverse=True,
        )
        best = ranked_diagnostics[0] if ranked_diagnostics else None
        self._last_cycle_summary = {
            "pairs_scanned": len(self.pairs),
            "qualified": len(candidates),
            "published": len(published),
            "best_candidate": (
                f"{best['pair']} {best['strategy']} score={float(best['score']):.1f}"
                if best
                else "NONE"
            ),
            "rejections": rejection_reasons,
            "regimes": regime_counts,
            "near_setups": ranked_diagnostics[:5],
            "performance": self.aggregate_forward_performance(self._performance_cache),
        }
        self.logger.info(
            "OPTIMIZED_LONG_SUMMARY pairs=%s qualified=%s published=%s regimes=%s "
            "best=%s rejections=%s",
            len(self.pairs),
            len(candidates),
            len(published),
            json.dumps(regime_counts, sort_keys=True),
            self._last_cycle_summary["best_candidate"],
            json.dumps(rejection_reasons, sort_keys=True),
        )
        return published[0] if published else None

    def evaluate_pair_detailed(
        self, pair: str
    ) -> tuple[GeneratedSignal | None, str, dict[str, Any]]:
        frame_15m = self.fetch_closed_frame(pair, "15m", 260)
        frame_1h = self.fetch_closed_frame(pair, "1h", 260)
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
        latest = frame_15m.iloc[-1]
        context = frame_1h.iloc[-1]
        regime = self.detect_regime(frame_15m, frame_1h)
        assessments = [
            self.trend_pullback_assessment(frame_15m, frame_1h, regime),
            self.breakout_assessment(frame_15m, frame_1h, regime),
            self.range_bounce_assessment(frame_15m, frame_1h, regime),
            self.oversold_reversal_assessment(frame_15m, frame_1h, regime),
        ]
        best = max(assessments, key=lambda item: float(item["score"]))
        diagnostic = {
            "pair": pair,
            "regime": regime,
            "strategy": best["strategy"],
            "score": best["score"],
            "qualified": best["qualified"],
            "blockers": list(best["blockers"])[:3],
            "rsi": round(float(latest["rsi"]), 1),
            "volume_ratio": round(float(latest["volume_ratio"]), 2),
            "candle_time": str(latest["timestamp"]),
        }
        if not bool(best["qualified"]):
            return None, f"NO_SETUP_{regime}", diagnostic
        if float(best["score"]) < self.min_signal_score:
            diagnostic["blockers"] = ["score sotto soglia operativa"]
            return None, "SCORE_BELOW_MINIMUM", diagnostic

        performance = self._performance_cache.get(str(best["strategy"]), {})
        completed = int(performance.get("completed", 0) or 0)
        profit_factor = float(performance.get("profit_factor", 0.0) or 0.0)
        net_result = float(performance.get("net_result_eur", 0.0) or 0.0)
        if (
            completed >= self.performance_guard_min_trades
            and profit_factor < self.performance_guard_min_pf
            and net_result < 0
        ):
            diagnostic["blockers"] = [
                f"strategia sospesa: PF forward {profit_factor:.2f} su {completed} trade"
            ]
            return None, "PERFORMANCE_GUARD", diagnostic

        entry = float(latest["close"])
        atr = float(latest["atr"])
        stop_distance = self.stop_distance_for_strategy(
            str(best["strategy"]), frame_15m, entry, atr
        )
        if stop_distance <= 0 or stop_distance >= entry:
            diagnostic["blockers"] = ["stop tecnico non valido"]
            return None, "INVALID_STOP", diagnostic
        stop_loss = entry - stop_distance
        target = self.technical_target(
            str(best["strategy"]),
            frame_15m,
            entry,
            stop_distance,
            atr,
            float(best["target_rr"]),
        )
        economics = self.calculate_net_economics(entry, stop_loss, target)
        if not bool(economics["tradable"]):
            diagnostic["blockers"] = ["quantità o nozionale non negoziabile"]
            return None, "NOT_TRADABLE", diagnostic
        if float(economics["net_profit_tp1_eur"]) < self.min_tp1_net_profit_eur:
            diagnostic["blockers"] = ["profitto netto sotto soglia"]
            return None, "NET_PROFIT_TOO_LOW", diagnostic
        if float(economics["net_rr"]) < self.min_net_rr:
            diagnostic["blockers"] = ["R/R netto sotto soglia"]
            return None, "NET_RR_TOO_LOW", diagnostic

        reasons = list(best["reasons"])
        reasons.extend(
            [
                f"regime={regime}",
                f"rsi_15m={float(latest['rsi']):.1f}",
                f"volume_ratio={float(latest['volume_ratio']):.2f}",
                f"forward_trades={completed}",
                f"forward_profit_factor={profit_factor:.3f}",
            ]
        )
        signal_time = datetime.now(timezone.utc)
        reference_time = latest["timestamp"].to_pydatetime()
        score = min(100.0, float(best["score"]))
        signal = GeneratedSignal(
            strategy=str(best["strategy"]),
            pair=pair,
            timeframe="15m",
            regime=regime,
            entry=entry,
            stop_loss=stop_loss,
            take_profit=target,
            technical_take_profit=target,
            effective_take_profit=target,
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
            stop_pct=(stop_distance / entry * 100.0),
            atr=atr,
            stop_atr_ratio=(stop_distance / atr if atr > 0 else 0.0),
            tp_atr_ratio=((target - entry) / atr if atr > 0 else 0.0),
            historical_sample_size=completed,
            historical_win_rate=(
                float(performance.get("win_rate", 0.0)) if completed else None
            ),
            historical_expected_value_eur=(
                float(performance.get("expectancy_eur", 0.0)) if completed else None
            ),
            historical_mfe_percentile=0.0,
            historical_mae_percentile=0.0,
            probability_confidence=(
                "FORWARD_HIGH"
                if completed >= 30
                else "FORWARD_MEDIUM"
                if completed >= 15
                else "FORWARD_BUILDING"
            ),
            validation_status="FIXED_RULES_PASSED",
            validation_reason="; ".join(reasons),
            signal_class="B",
            score=score,
            probability=(
                float(performance.get("win_rate", 0.0)) if completed else None
            ),
            reasons=reasons,
            score_breakdown={
                "setup_score": score,
                "rsi": round(float(latest["rsi"]), 2),
                "volume_ratio": round(float(latest["volume_ratio"]), 3),
                "forward_completed": completed,
                "forward_profit_factor": round(profit_factor, 4),
                "forward_net_result_eur": round(net_result, 4),
            },
        )
        diagnostic["blockers"] = []
        diagnostic["net_rr"] = round(float(economics["net_rr"]), 2)
        return signal, "OK", diagnostic

    @staticmethod
    def stop_distance_for_strategy(
        strategy: str,
        frame: pd.DataFrame,
        entry: float,
        atr: float,
    ) -> float:
        if atr <= 0:
            return 0.0
        if strategy == "Breakout 20":
            level = float(frame.iloc[-1]["high20_prev"])
            distance = max(atr * 1.10, entry - level + atr * 0.20)
        elif strategy == "Range Bounce Bollinger":
            recent_low = float(frame["low"].tail(5).min())
            distance = max(atr * 1.00, entry - recent_low + atr * 0.12)
        elif strategy == "Oversold Reversal":
            recent_low = float(frame["low"].tail(5).min())
            distance = max(atr * 1.15, entry - recent_low + atr * 0.18)
        else:
            recent_low = float(frame["low"].tail(8).min())
            distance = max(atr * 1.10, entry - recent_low + atr * 0.10)
        return min(max(distance, entry * 0.003), entry * 0.022)

    @staticmethod
    def technical_target(
        strategy: str,
        frame: pd.DataFrame,
        entry: float,
        stop_distance: float,
        atr: float,
        target_rr: float,
    ) -> float:
        target = entry + stop_distance * target_rr
        if strategy == "Range Bounce Bollinger":
            middle = float(frame.iloc[-1]["bb_middle"])
            minimum = entry + stop_distance * 1.25
            if middle > minimum:
                target = min(target, middle)
        maximum = min(entry * 1.03, entry + atr * 3.0)
        return max(entry + stop_distance * 1.20, min(target, maximum))

    def fetch_forward_performance(self) -> dict[str, dict[str, float | int | str]]:
        placeholders = ", ".join(["%s"] * len(SIMPLE_STRATEGIES))
        rows = self.client.fetch_all(
            f"""SELECT strategy, status, net_profit_tp1_eur, net_loss_sl_eur, closed_at
            FROM signals.generated_signals
            WHERE strategy IN ({placeholders})
              AND status IN ('TARGET_HIT', 'STOP_LOSS')
              AND created_at >= NOW() - INTERVAL '120 days'
            ORDER BY closed_at ASC, id ASC""",
            SIMPLE_STRATEGIES,
        )
        grouped: dict[str, list[tuple[Any, ...]]] = {name: [] for name in SIMPLE_STRATEGIES}
        for row in rows:
            grouped.setdefault(str(row[0]), []).append(row)
        return {
            strategy: self.calculate_performance_metrics(items)
            for strategy, items in grouped.items()
        }

    @staticmethod
    def calculate_performance_metrics(
        rows: list[tuple[Any, ...]],
    ) -> dict[str, float | int | str]:
        wins = 0
        losses = 0
        gross_profit = 0.0
        gross_loss = 0.0
        current_loss_streak = 0
        max_loss_streak = 0
        for _, status, profit, loss, *_ in rows:
            if str(status) == "TARGET_HIT":
                wins += 1
                gross_profit += float(profit or 0.0)
                current_loss_streak = 0
            elif str(status) == "STOP_LOSS":
                losses += 1
                gross_loss += abs(float(loss or 0.0))
                current_loss_streak += 1
                max_loss_streak = max(max_loss_streak, current_loss_streak)
        completed = wins + losses
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else (
            99.0 if gross_profit > 0 else 0.0
        )
        net_result = gross_profit - gross_loss
        win_rate = wins / completed if completed else 0.0
        expectancy = net_result / completed if completed else 0.0
        if completed < 10:
            health = "CAMPIONE_IN_COSTRUZIONE"
        elif profit_factor >= 1.10 and net_result > 0:
            health = "POSITIVA"
        elif completed >= 20 and profit_factor < 0.85 and net_result < 0:
            health = "SOSPESA"
        else:
            health = "DA_MONITORARE"
        return {
            "completed": completed,
            "wins": wins,
            "losses": losses,
            "win_rate": win_rate,
            "gross_profit_eur": gross_profit,
            "gross_loss_eur": gross_loss,
            "net_result_eur": net_result,
            "profit_factor": profit_factor,
            "expectancy_eur": expectancy,
            "max_loss_streak": max_loss_streak,
            "health": health,
        }

    @staticmethod
    def aggregate_forward_performance(
        by_strategy: dict[str, dict[str, float | int | str]],
    ) -> dict[str, float | int | str]:
        completed = sum(int(item.get("completed", 0) or 0) for item in by_strategy.values())
        wins = sum(int(item.get("wins", 0) or 0) for item in by_strategy.values())
        gross_profit = sum(float(item.get("gross_profit_eur", 0.0) or 0.0) for item in by_strategy.values())
        gross_loss = sum(float(item.get("gross_loss_eur", 0.0) or 0.0) for item in by_strategy.values())
        net_result = gross_profit - gross_loss
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else (
            99.0 if gross_profit > 0 else 0.0
        )
        return {
            "completed": completed,
            "wins": wins,
            "losses": completed - wins,
            "win_rate": wins / completed if completed else 0.0,
            "profit_factor": profit_factor,
            "net_result_eur": net_result,
            "expectancy_eur": net_result / completed if completed else 0.0,
        }

    def publish_daily_signal_report(self) -> bool:
        if not self.enable_daily_signal_report:
            return False
        now = datetime.now(timezone.utc)
        if self._last_no_trade_report_at is not None:
            elapsed = (now - self._last_no_trade_report_at).total_seconds() / 3600.0
            if elapsed < self.daily_signal_report_hours:
                return False

        recent = self.client.fetch_all(
            """SELECT COUNT(*) FROM signals.generated_signals
            WHERE created_at >= NOW() - (%s * INTERVAL '1 hour')
              AND strategy IN (%s, %s, %s, %s)""",
            (self.daily_signal_report_hours, *SIMPLE_STRATEGIES),
        )
        recent_count = int(recent[0][0] or 0) if recent else 0
        summary = self._last_cycle_summary or {}
        near = summary.get("near_setups") or []
        near_lines = []
        for item in near[:5]:
            blockers = item.get("blockers") or []
            missing = str(blockers[0]) if blockers else "tutti i requisiti superati"
            near_lines.append(
                f"• <b>{item.get('pair')}</b> · {item.get('strategy')} · "
                f"score <b>{float(item.get('score') or 0.0):.1f}</b> · manca: {missing}"
            )
        if not near_lines:
            near_lines = ["• Nessun dato diagnostico disponibile"]

        performance = summary.get("performance") or self.aggregate_forward_performance(
            self.fetch_forward_performance()
        )
        completed = int(performance.get("completed", 0) or 0)
        if completed:
            performance_text = (
                f"• Operazioni chiuse: <b>{completed}</b> · "
                f"win rate <b>{float(performance.get('win_rate') or 0.0) * 100:.1f}%</b>\n"
                f"• Profit Factor forward: <b>{float(performance.get('profit_factor') or 0.0):.2f}</b> · "
                f"risultato netto <b>€{float(performance.get('net_result_eur') or 0.0):+.2f}</b>\n"
                f"• Expectancy per segnale: <b>€{float(performance.get('expectancy_eur') or 0.0):+.3f}</b>"
            )
        else:
            performance_text = (
                "• Nessuna operazione del nuovo scanner ancora chiusa.\n"
                "• Profittevolezza: <b>non ancora misurabile</b>."
            )

        regimes = summary.get("regimes") or {}
        message = (
            "📊 <b>REPORT SCANNER LONG</b>\n\n"
            "🔎 <b>Ultimo ciclo</b>\n"
            f"• Coppie analizzate: <b>{summary.get('pairs_scanned', len(self.pairs))}</b>\n"
            f"• Setup qualificati: <b>{summary.get('qualified', 0)}</b>\n"
            f"• Segnali inviati: <b>{summary.get('published', 0)}</b>\n"
            f"• Segnali ultime {self.daily_signal_report_hours}h: <b>{recent_count}</b>\n"
            f"• Regimi: rialzo <b>{regimes.get('TREND_UP', 0)}</b>, "
            f"ribasso <b>{regimes.get('TREND_DOWN', 0)}</b>, "
            f"range <b>{regimes.get('RANGE', 0)}</b>, "
            f"neutro <b>{regimes.get('NEUTRAL', 0)}</b>\n\n"
            "📍 <b>Setup più vicini</b>\n"
            + "\n".join(near_lines)
            + "\n\n📈 <b>Risultati forward reali</b>\n"
            + performance_text
            + "\n\nStrategie LONG: Trend Pullback, Breakout 20, Range Bounce, Reversal confermato."
        )
        self.event_bus.publish(
            Event(EventType.REPORT_READY, {"message": message, "trusted_html": True})
        )
        self._last_no_trade_report_at = now
        return True
