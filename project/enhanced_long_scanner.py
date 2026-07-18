"""Enhanced LONG-only scanner with six readable strategies and safe reactivation.

Strategies are evaluated independently. A weak strategy is never disabled forever: after
an evidence-based temporary pause it automatically enters probation and may produce a
limited high-quality probe signal. The scanner also limits correlated exposure and keeps
all profitability measurements strictly forward-looking.
"""
from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from statistics import median
from typing import Any

import pandas as pd

from project.optimized_long_scanner import OptimizedLongStrategyScanner
from project.shared.events import Event, EventType
from project.shared.logging import get_module_logger
from project.strategy_engine.service import GeneratedSignal


ENHANCED_LONG_STRATEGIES = (
    "Trend Pullback",
    "Breakout 20",
    "Range Bounce Bollinger",
    "Oversold Reversal",
    "Relative Strength Momentum",
    "Volatility Squeeze Breakout",
)


class EnhancedLongStrategyScanner(OptimizedLongStrategyScanner):
    """Operate six fixed LONG strategies with correlation and recovery controls."""

    def __init__(
        self,
        *args: Any,
        max_signals_per_cycle: int = 3,
        performance_pause_hours: int = 72,
        probation_interval_hours: int = 24,
        probation_min_score: float = 76.0,
        probation_min_rr: float = 1.25,
        correlation_threshold: float = 0.85,
        max_correlated_per_cycle: int = 2,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            *args,
            max_signals_per_cycle=max_signals_per_cycle,
            **kwargs,
        )
        self.performance_pause_hours = max(24, int(performance_pause_hours))
        self.probation_interval_hours = max(6, int(probation_interval_hours))
        self.probation_min_score = max(70.0, float(probation_min_score))
        self.probation_min_rr = max(1.10, float(probation_min_rr))
        self.correlation_threshold = min(0.99, max(0.50, float(correlation_threshold)))
        self.max_correlated_per_cycle = max(1, int(max_correlated_per_cycle))
        self._relative_strength_snapshot: dict[str, dict[str, float]] = {}
        self._frame_cache: dict[str, pd.DataFrame] = {}
        self._strategy_states: dict[str, dict[str, Any]] = {}
        self.logger = get_module_logger("enhanced-long-scanner")

    @staticmethod
    def add_indicators(frame: pd.DataFrame) -> pd.DataFrame:
        data = OptimizedLongStrategyScanner.add_indicators(frame)
        data["atr_avg20"] = data["atr"].rolling(20).mean()
        data["atr_ratio"] = (
            data["atr"] / data["atr_avg20"].replace(0, float("nan"))
        ).fillna(1.0)
        data["bb_width_avg50"] = data["bb_width"].rolling(50).mean()
        data["bb_width_ratio"] = (
            data["bb_width"] / data["bb_width_avg50"].replace(0, float("nan"))
        ).fillna(1.0)
        return data

    def evaluate(self) -> GeneratedSignal | None:
        self._pair_diagnostics = []
        self._frame_cache = {}
        self._relative_strength_snapshot = self.fetch_relative_strength_snapshot()
        self._performance_cache = self.fetch_forward_performance()
        now = datetime.now(timezone.utc)
        self._strategy_states = {
            strategy: self.determine_strategy_state(
                strategy,
                self._performance_cache.get(strategy, {}),
                now,
            )
            for strategy in ENHANCED_LONG_STRATEGIES
        }

        candidates: list[GeneratedSignal] = []
        rejection_reasons: dict[str, int] = {}
        regime_counts: dict[str, int] = {}
        for pair in self.pairs:
            try:
                candidate, reason, diagnostic = self.evaluate_pair_detailed(pair)
            except Exception:
                self.logger.exception("ENHANCED_LONG pair_failed pair=%s", pair)
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
                rejection_reasons["CYCLE_LIMIT"] = rejection_reasons.get("CYCLE_LIMIT", 0) + 1
                continue
            duplicate_id = super().find_active_duplicate(candidate)
            if duplicate_id is not None:
                rejection_reasons["ACTIVE_PAIR_COOLDOWN"] = (
                    rejection_reasons.get("ACTIVE_PAIR_COOLDOWN", 0) + 1
                )
                continue
            correlated = self.correlated_with_published(candidate.pair, published)
            if len(correlated) >= self.max_correlated_per_cycle:
                rejection_reasons["CORRELATED_EXPOSURE"] = (
                    rejection_reasons.get("CORRELATED_EXPOSURE", 0) + 1
                )
                self.logger.info(
                    "ENHANCED_LONG_SKIPPED pair=%s reason=CORRELATED_EXPOSURE peers=%s",
                    candidate.pair,
                    correlated,
                )
                continue
            signal_id = self.save_signal(candidate)
            signal = replace(candidate, signal_id=signal_id)
            self.publish_signal_event(signal)
            published.append(signal)
            self.logger.info(
                "ENHANCED_LONG_SIGNAL id=%s pair=%s strategy=%s state=%s "
                "score=%.1f entry=%.8f stop=%.8f target=%.8f net_rr=%.3f",
                signal_id,
                signal.pair,
                signal.strategy,
                (signal.score_breakdown or {}).get("strategy_state", "ACTIVE"),
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
            "best_candidate": (
                f"{best['pair']} {best['strategy']} score={float(best['score']):.1f}"
                if best
                else "NONE"
            ),
            "rejections": rejection_reasons,
            "regimes": regime_counts,
            "near_setups": ranked[:5],
            "performance": self.aggregate_forward_performance(self._performance_cache),
            "strategy_states": self._strategy_states,
        }
        self.logger.info(
            "ENHANCED_LONG_SUMMARY pairs=%s qualified=%s published=%s regimes=%s "
            "states=%s rejections=%s",
            len(self.pairs),
            len(candidates),
            len(published),
            json.dumps(regime_counts, sort_keys=True),
            json.dumps(
                {name: item.get("state") for name, item in self._strategy_states.items()},
                sort_keys=True,
            ),
            json.dumps(rejection_reasons, sort_keys=True),
        )
        return published[0] if published else None

    def evaluate_pair_detailed(
        self,
        pair: str,
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
        self._frame_cache[pair] = frame_15m
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
        ]
        best = max(assessments, key=lambda item: float(item["score"]))
        strategy = str(best["strategy"])
        state_info = self._strategy_states.get(strategy, {"state": "ACTIVE"})
        state = str(state_info.get("state") or "ACTIVE")
        diagnostic = {
            "pair": pair,
            "regime": regime,
            "strategy": strategy,
            "strategy_state": state,
            "score": best["score"],
            "qualified": best["qualified"],
            "blockers": list(best["blockers"])[:3],
            "rsi": round(float(latest["rsi"]), 1),
            "volume_ratio": round(float(latest["volume_ratio"]), 2),
            "relative_24h_pct": round(float(relative.get("relative_24h", 0.0)) * 100, 2),
            "candle_time": str(latest["timestamp"]),
        }
        if not bool(best["qualified"]):
            return None, f"NO_SETUP_{regime}", diagnostic

        required_score = self.min_signal_score
        required_rr = self.min_net_rr
        required_profit = self.min_tp1_net_profit_eur
        if state == "PAUSED":
            diagnostic["blockers"] = [
                f"strategia in pausa fino a {state_info.get('next_review', 'n/d')}"
            ]
            return None, "STRATEGY_TEMPORARILY_PAUSED", diagnostic
        if state == "PROBATION":
            required_score = max(required_score, self.probation_min_score)
            required_rr = max(required_rr, self.probation_min_rr)
            required_profit = max(required_profit, 0.30)
            if self.has_recent_probation_signal(strategy):
                diagnostic["blockers"] = [
                    f"segnale probation già eseguito nelle ultime {self.probation_interval_hours}h"
                ]
                return None, "PROBATION_RATE_LIMIT", diagnostic

        if float(best["score"]) < required_score:
            diagnostic["blockers"] = [
                f"score {float(best['score']):.1f} sotto soglia {required_score:.1f}"
            ]
            return None, "SCORE_BELOW_MINIMUM", diagnostic

        performance = self._performance_cache.get(strategy, {})
        completed = int(performance.get("completed", 0) or 0)
        profit_factor = float(performance.get("profit_factor", 0.0) or 0.0)
        net_result = float(performance.get("net_result_eur", 0.0) or 0.0)
        entry = float(latest["close"])
        atr = float(latest["atr"])
        stop_distance = self.stop_distance_for_strategy(strategy, frame_15m, entry, atr)
        if stop_distance <= 0 or stop_distance >= entry:
            diagnostic["blockers"] = ["stop tecnico non valido"]
            return None, "INVALID_STOP", diagnostic
        stop_loss = entry - stop_distance
        target = self.technical_target(
            strategy,
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
        if float(economics["net_profit_tp1_eur"]) < required_profit:
            diagnostic["blockers"] = [
                f"profitto netto sotto soglia €{required_profit:.2f}"
            ]
            return None, "NET_PROFIT_TOO_LOW", diagnostic
        if float(economics["net_rr"]) < required_rr:
            diagnostic["blockers"] = [f"R/R netto sotto soglia {required_rr:.2f}"]
            return None, "NET_RR_TOO_LOW", diagnostic

        reasons = list(best["reasons"])
        reasons.extend(
            [
                f"regime={regime}",
                f"rsi_15m={float(latest['rsi']):.1f}",
                f"volume_ratio={float(latest['volume_ratio']):.2f}",
                f"strategy_state={state}",
                f"forward_trades={completed}",
                f"forward_profit_factor={profit_factor:.3f}",
            ]
        )
        signal_time = datetime.now(timezone.utc)
        reference_time = latest["timestamp"].to_pydatetime()
        score = min(100.0, float(best["score"]))
        signal = GeneratedSignal(
            strategy=strategy,
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
            probability=(float(performance.get("win_rate", 0.0)) if completed else None),
            reasons=reasons,
            score_breakdown={
                "setup_score": score,
                "rsi": round(float(latest["rsi"]), 2),
                "volume_ratio": round(float(latest["volume_ratio"]), 3),
                "strategy_state": state,
                "forward_completed": completed,
                "forward_profit_factor": round(profit_factor, 4),
                "forward_net_result_eur": round(net_result, 4),
                "relative_24h_pct": round(
                    float(relative.get("relative_24h", 0.0)) * 100,
                    3,
                ),
            },
        )
        diagnostic["blockers"] = []
        diagnostic["net_rr"] = round(float(economics["net_rr"]), 2)
        return signal, "OK", diagnostic

    @classmethod
    def relative_strength_assessment(
        cls,
        frame_15m: pd.DataFrame,
        frame_1h: pd.DataFrame,
        regime: str,
        relative: dict[str, float],
    ) -> dict[str, Any]:
        row = frame_15m.iloc[-1]
        previous = frame_15m.iloc[-2]
        context = frame_1h.iloc[-1]
        leader = (
            float(relative.get("rank_percentile", 0.0)) >= 0.70
            and float(relative.get("relative_24h", 0.0)) >= 0.003
        )
        short_momentum = (
            float(relative.get("return_4h", 0.0))
            >= float(relative.get("median_return_4h", 0.0))
            and float(relative.get("return_4h", 0.0)) > 0
        )
        trend_ok = (
            regime != "TREND_DOWN"
            and float(context["close"]) >= float(context["ema200"]) * 0.99
            and float(context["ema20"]) > float(context["ema50"])
        )
        structure_ok = float(row["close"]) > float(row["ema20"]) >= float(row["ema50"])
        rsi_ok = 48.0 <= float(row["rsi"]) <= 72.0
        momentum_ok = float(row["macd_hist"]) >= float(previous["macd_hist"])
        volume_ok = float(row["volume_ratio"]) >= 0.70
        score = 0.0
        score += 26 if leader else 0
        score += 18 if short_momentum else 0
        score += 18 if trend_ok else 0
        score += 16 if structure_ok else 0
        score += 8 if rsi_ok else 0
        score += 8 if momentum_ok else 0
        score += 6 if volume_ok else 0
        blockers = []
        if not leader:
            blockers.append("moneta non tra i leader relativi a 24h")
        if not short_momentum:
            blockers.append("forza relativa 4h non superiore alla mediana")
        if not trend_ok:
            blockers.append("trend 1h non favorevole")
        if not structure_ok:
            blockers.append("prezzo 15m non sopra EMA20/EMA50")
        if not rsi_ok or not momentum_ok:
            blockers.append("momentum 15m non confermato")
        if not volume_ok:
            blockers.append("volume sotto 0,70 della media")
        qualified = (
            leader
            and short_momentum
            and trend_ok
            and structure_ok
            and rsi_ok
            and momentum_ok
            and volume_ok
            and score >= 72
        )
        return cls._assessment(
            "Relative Strength Momentum",
            score,
            qualified,
            [
                "moneta tra i leader rispetto a BTC e al paniere",
                "momentum 4h e 15m concordi",
                "trend 1h favorevole al LONG",
            ],
            blockers,
            target_rr=1.75,
        )

    @classmethod
    def volatility_squeeze_assessment(
        cls,
        frame_15m: pd.DataFrame,
        frame_1h: pd.DataFrame,
        regime: str,
    ) -> dict[str, Any]:
        row = frame_15m.iloc[-1]
        previous = frame_15m.iloc[-2]
        context = frame_1h.iloc[-1]
        pre_breakout = frame_15m.iloc[-8:-1]
        squeeze = (
            float(pre_breakout["bb_width_ratio"].min()) <= 0.78
            and float(pre_breakout["atr_ratio"].min()) <= 0.90
        )
        level = float(row["high20_prev"])
        breakout = float(row["high"]) > level and float(row["close"]) >= level * 0.998
        context_ok = (
            regime != "TREND_DOWN"
            and float(context["close"]) >= float(context["ema200"]) * 0.985
        )
        expansion = (
            float(row["volume_ratio"]) >= 1.00
            and float(row["bb_width"]) >= float(previous["bb_width"])
        )
        rsi_ok = 48.0 <= float(row["rsi"]) <= 75.0
        momentum_ok = (
            float(row["macd_hist"]) > float(previous["macd_hist"])
            and float(row["close"]) > float(row["ema20"])
        )
        score = 0.0
        score += 25 if squeeze else 0
        score += 28 if breakout else 0
        score += 15 if context_ok else 0
        score += 14 if expansion else 0
        score += 8 if rsi_ok else 0
        score += 10 if momentum_ok else 0
        blockers = []
        if not squeeze:
            blockers.append("compressione Bollinger/ATR non sufficiente")
        if not breakout:
            blockers.append("rottura rialzista non confermata")
        if not context_ok:
            blockers.append("contesto 1h ribassista")
        if not expansion:
            blockers.append("volume o volatilità non in espansione")
        if not rsi_ok or not momentum_ok:
            blockers.append("momentum del breakout non confermato")
        qualified = (
            squeeze
            and breakout
            and context_ok
            and expansion
            and rsi_ok
            and momentum_ok
            and score >= 72
        )
        return cls._assessment(
            "Volatility Squeeze Breakout",
            score,
            qualified,
            [
                "compressione di volatilità precedente",
                "breakout rialzista con espansione dei volumi",
                "momentum e contesto 1h favorevoli",
            ],
            blockers,
            target_rr=1.90,
        )

    def fetch_relative_strength_snapshot(self) -> dict[str, dict[str, float]]:
        placeholders = ", ".join(["%s"] * len(self.pairs))
        rows = self.client.fetch_all(
            f"""WITH ranked AS (
                SELECT pair, close,
                       ROW_NUMBER() OVER (PARTITION BY pair ORDER BY timestamp DESC) AS rn
                FROM market_data.ohlc
                WHERE exchange = %s
                  AND timeframe = '15m'
                  AND pair IN ({placeholders})
                  AND timestamp + INTERVAL '15 minutes' <= NOW()
            )
            SELECT pair,
                   MAX(CASE WHEN rn = 1 THEN close END) AS latest_close,
                   MAX(CASE WHEN rn = 17 THEN close END) AS close_4h,
                   MAX(CASE WHEN rn = 97 THEN close END) AS close_24h
            FROM ranked
            WHERE rn IN (1, 17, 97)
            GROUP BY pair""",
            (self.exchange, *self.pairs),
        )
        raw: dict[str, dict[str, float]] = {}
        for pair, latest, close_4h, close_24h in rows:
            if latest is None or close_4h is None or close_24h is None:
                continue
            latest_f = float(latest)
            four_f = float(close_4h)
            day_f = float(close_24h)
            if four_f <= 0 or day_f <= 0:
                continue
            raw[str(pair)] = {
                "return_4h": latest_f / four_f - 1.0,
                "return_24h": latest_f / day_f - 1.0,
            }
        if not raw:
            return {}
        four_returns = [item["return_4h"] for item in raw.values()]
        day_returns = [item["return_24h"] for item in raw.values()]
        median_4h = float(median(four_returns))
        median_24h = float(median(day_returns))
        btc_return = float(raw.get("BTC/USD", {}).get("return_24h", median_24h))
        ordered = sorted(raw, key=lambda pair: raw[pair]["return_24h"])
        denominator = max(1, len(ordered) - 1)
        for index, pair in enumerate(ordered):
            raw[pair].update(
                {
                    "median_return_4h": median_4h,
                    "median_return_24h": median_24h,
                    "relative_24h": raw[pair]["return_24h"] - btc_return,
                    "rank_percentile": index / denominator,
                }
            )
        return raw

    def fetch_forward_performance(self) -> dict[str, dict[str, Any]]:
        placeholders = ", ".join(["%s"] * len(ENHANCED_LONG_STRATEGIES))
        rows = self.client.fetch_all(
            f"""SELECT strategy, status, net_profit_tp1_eur, net_loss_sl_eur, closed_at
            FROM signals.generated_signals
            WHERE strategy IN ({placeholders})
              AND status IN ('TARGET_HIT', 'STOP_LOSS')
              AND created_at >= NOW() - INTERVAL '120 days'
            ORDER BY closed_at ASC, id ASC""",
            ENHANCED_LONG_STRATEGIES,
        )
        grouped: dict[str, list[tuple[Any, ...]]] = {
            name: [] for name in ENHANCED_LONG_STRATEGIES
        }
        for row in rows:
            grouped.setdefault(str(row[0]), []).append(row)
        return {
            strategy: self.calculate_enhanced_performance(items)
            for strategy, items in grouped.items()
        }

    @staticmethod
    def calculate_enhanced_performance(rows: list[tuple[Any, ...]]) -> dict[str, Any]:
        base = OptimizedLongStrategyScanner.calculate_performance_metrics(rows)
        latest_status = str(rows[-1][1]) if rows else "NONE"
        last_closed = rows[-1][4] if rows and len(rows[-1]) > 4 else None
        base.update(
            {
                "latest_status": latest_status,
                "last_closed_at": last_closed,
            }
        )
        return base

    def determine_strategy_state(
        self,
        strategy: str,
        performance: dict[str, Any],
        now: datetime | None = None,
    ) -> dict[str, Any]:
        del strategy
        now = now or datetime.now(timezone.utc)
        completed = int(performance.get("completed", 0) or 0)
        profit_factor = float(performance.get("profit_factor", 0.0) or 0.0)
        net_result = float(performance.get("net_result_eur", 0.0) or 0.0)
        weak = (
            completed >= self.performance_guard_min_trades
            and profit_factor < self.performance_guard_min_pf
            and net_result < 0
        )
        if not weak:
            return {"state": "ACTIVE", "next_review": None}

        last_closed = performance.get("last_closed_at")
        latest_status = str(performance.get("latest_status") or "NONE")
        if isinstance(last_closed, str):
            try:
                last_closed = datetime.fromisoformat(last_closed.replace("Z", "+00:00"))
            except ValueError:
                last_closed = None
        if isinstance(last_closed, datetime):
            if last_closed.tzinfo is None:
                last_closed = last_closed.replace(tzinfo=timezone.utc)
            review_at = last_closed + timedelta(hours=self.performance_pause_hours)
            if latest_status == "STOP_LOSS" and now < review_at:
                return {
                    "state": "PAUSED",
                    "next_review": review_at.isoformat(),
                }
        return {"state": "PROBATION", "next_review": None}

    def has_recent_probation_signal(self, strategy: str) -> bool:
        rows = self.client.fetch_all(
            """SELECT COUNT(*)
            FROM signals.generated_signals
            WHERE strategy = %s
              AND created_at >= NOW() - (%s * INTERVAL '1 hour')""",
            (strategy, self.probation_interval_hours),
        )
        return bool(rows and int(rows[0][0] or 0) > 0)

    def correlated_with_published(
        self,
        pair: str,
        published: list[GeneratedSignal],
    ) -> list[str]:
        frame = self._frame_cache.get(pair)
        if frame is None or frame.empty:
            return []
        candidate_returns = frame.set_index("timestamp")["close"].pct_change().tail(96)
        correlated: list[str] = []
        for signal in published:
            peer = self._frame_cache.get(signal.pair)
            if peer is None or peer.empty:
                continue
            peer_returns = peer.set_index("timestamp")["close"].pct_change().tail(96)
            aligned = pd.concat([candidate_returns, peer_returns], axis=1).dropna()
            if len(aligned) < 30:
                continue
            correlation = float(aligned.iloc[:, 0].corr(aligned.iloc[:, 1]))
            if pd.notna(correlation) and correlation >= self.correlation_threshold:
                correlated.append(signal.pair)
        return correlated

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
              AND strategy IN (%s, %s, %s, %s, %s, %s)""",
            (self.daily_signal_report_hours, *ENHANCED_LONG_STRATEGIES),
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

        states = summary.get("strategy_states") or self._strategy_states
        state_lines = []
        for strategy in ENHANCED_LONG_STRATEGIES:
            info = states.get(strategy, {"state": "ACTIVE"})
            state = str(info.get("state") or "ACTIVE")
            extra = (
                f" · riattivazione {info.get('next_review')}"
                if state == "PAUSED" and info.get("next_review")
                else " · segnale-test massimo ogni 24h"
                if state == "PROBATION"
                else ""
            )
            state_lines.append(f"• {strategy}: <b>{state}</b>{extra}")

        performance = summary.get("performance") or self.aggregate_forward_performance(
            self.fetch_forward_performance()
        )
        completed = int(performance.get("completed", 0) or 0)
        if completed:
            performance_text = (
                f"• Operazioni chiuse: <b>{completed}</b> · "
                f"win rate <b>{float(performance.get('win_rate') or 0.0) * 100:.1f}%</b>\n"
                f"• Profit Factor: <b>{float(performance.get('profit_factor') or 0.0):.2f}</b> · "
                f"risultato netto <b>€{float(performance.get('net_result_eur') or 0.0):+.2f}</b>\n"
                f"• Expectancy: <b>€{float(performance.get('expectancy_eur') or 0.0):+.3f}</b> per segnale"
            )
        else:
            performance_text = (
                "• Nessuna operazione delle sei strategie ancora chiusa.\n"
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
            + "\n\n🛡 <b>Stato strategie</b>\n"
            + "\n".join(state_lines)
            + "\n\n📈 <b>Risultati forward reali</b>\n"
            + performance_text
            + "\n\nLimiti: massimo 3 segnali per ciclo e massimo 2 esposizioni fortemente correlate."
        )
        self.event_bus.publish(
            Event(EventType.REPORT_READY, {"message": message, "trusted_html": True})
        )
        self._last_no_trade_report_at = now
        return True
