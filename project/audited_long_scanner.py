"""Audited runtime fixes for the six-strategy LONG scanner.

This layer keeps the readable V3 strategies but fixes three operational gaps:
- a qualified strategy is never hidden by a higher-scoring unqualified strategy;
- a paused strategy cannot hide another qualified ACTIVE/PROBATION strategy;
- PostgreSQL relative-strength work is bounded to three days and portfolio exposure is
  capped across cycles, not only inside the current scan.
"""
from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone
from statistics import median
from typing import Any

import pandas as pd

from project.enhanced_long_scanner import (
    ENHANCED_LONG_STRATEGIES,
    EnhancedLongStrategyScanner,
)
from project.strategy_engine.service import GeneratedSignal


class AuditedLongStrategyScanner(EnhancedLongStrategyScanner):
    """Six-strategy LONG scanner with deterministic fallback and portfolio limits."""

    def __init__(
        self,
        *args: Any,
        max_open_signals: int = 5,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.max_open_signals = max(1, int(max_open_signals))
        self._probation_recent_cache: dict[str, bool] = {}

    @staticmethod
    def assessment_priority(
        assessment: dict[str, Any],
        strategy_states: dict[str, dict[str, Any]],
    ) -> tuple[int, float]:
        """Rank executable setups before diagnostic-only setups.

        ACTIVE qualified setups rank first, then PROBATION qualified setups, then PAUSED
        qualified setups (useful for diagnostics), and finally unqualified near-setups.
        """
        strategy = str(assessment.get("strategy") or "")
        state = str(strategy_states.get(strategy, {}).get("state") or "ACTIVE")
        qualified = bool(assessment.get("qualified"))
        state_rank = {
            "ACTIVE": 3,
            "PROBATION": 2,
            "PAUSED": 1,
        }.get(state, 2)
        return (state_rank if qualified else 0, float(assessment.get("score") or 0.0))

    def evaluate(self) -> GeneratedSignal | None:
        self._pair_diagnostics = []
        self._frame_cache = {}
        self._probation_recent_cache = {}
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
                self.logger.exception("AUDITED_LONG pair_failed pair=%s", pair)
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
        open_pairs = self.fetch_open_exposure_pairs()
        available_slots = max(0, self.max_open_signals - len(open_pairs))
        published: list[GeneratedSignal] = []
        for candidate in candidates:
            if available_slots <= 0:
                rejection_reasons["MAX_OPEN_EXPOSURE"] = (
                    rejection_reasons.get("MAX_OPEN_EXPOSURE", 0) + 1
                )
                continue
            if len(published) >= self.max_signals_per_cycle:
                rejection_reasons["CYCLE_LIMIT"] = (
                    rejection_reasons.get("CYCLE_LIMIT", 0) + 1
                )
                continue
            duplicate_id = super().find_active_duplicate(candidate)
            if duplicate_id is not None:
                rejection_reasons["ACTIVE_PAIR_COOLDOWN"] = (
                    rejection_reasons.get("ACTIVE_PAIR_COOLDOWN", 0) + 1
                )
                continue

            exposure_pairs = list(dict.fromkeys(open_pairs + [item.pair for item in published]))
            correlated = self.correlated_with_pairs(candidate.pair, exposure_pairs)
            if len(correlated) >= self.max_correlated_per_cycle:
                rejection_reasons["CORRELATED_EXPOSURE"] = (
                    rejection_reasons.get("CORRELATED_EXPOSURE", 0) + 1
                )
                self.logger.info(
                    "AUDITED_LONG_SKIPPED pair=%s reason=CORRELATED_EXPOSURE peers=%s",
                    candidate.pair,
                    correlated,
                )
                continue

            signal_id = self.save_signal(candidate)
            signal = replace(candidate, signal_id=signal_id)
            self.publish_signal_event(signal)
            published.append(signal)
            available_slots -= 1
            self.logger.info(
                "AUDITED_LONG_SIGNAL id=%s pair=%s strategy=%s state=%s "
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
            "open_positions_before_cycle": len(open_pairs),
            "open_positions_after_cycle": len(open_pairs) + len(published),
            "max_open_positions": self.max_open_signals,
        }
        self.logger.info(
            "AUDITED_LONG_SUMMARY pairs=%s qualified=%s published=%s open_before=%s "
            "open_after=%s max_open=%s regimes=%s states=%s rejections=%s",
            len(self.pairs),
            len(candidates),
            len(published),
            len(open_pairs),
            len(open_pairs) + len(published),
            self.max_open_signals,
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
            signal, reason, diagnostic = self._build_signal_for_assessment(
                pair,
                frame_15m,
                regime,
                relative,
                assessment,
            )
            if signal is not None:
                return signal, reason, diagnostic
            if last_failure is None:
                last_failure = (reason, diagnostic)

        if last_failure is not None:
            return None, last_failure[0], last_failure[1]

        strategy = str(fallback.get("strategy") or "NONE")
        state = str(
            self._strategy_states.get(strategy, {}).get("state") or "ACTIVE"
        )
        return None, f"NO_SETUP_{regime}", {
            "pair": pair,
            "regime": regime,
            "strategy": strategy,
            "strategy_state": state,
            "score": float(fallback.get("score") or 0.0),
            "qualified": False,
            "blockers": list(fallback.get("blockers") or [])[:3],
            "rsi": round(float(latest["rsi"]), 1),
            "volume_ratio": round(float(latest["volume_ratio"]), 2),
            "relative_24h_pct": round(
                float(relative.get("relative_24h", 0.0)) * 100,
                2,
            ),
            "candle_time": str(latest["timestamp"]),
        }

    def _build_signal_for_assessment(
        self,
        pair: str,
        frame_15m: pd.DataFrame,
        regime: str,
        relative: dict[str, float],
        assessment: dict[str, Any],
    ) -> tuple[GeneratedSignal | None, str, dict[str, Any]]:
        latest = frame_15m.iloc[-1]
        strategy = str(assessment["strategy"])
        state_info = self._strategy_states.get(strategy, {"state": "ACTIVE"})
        state = str(state_info.get("state") or "ACTIVE")
        diagnostic = {
            "pair": pair,
            "regime": regime,
            "strategy": strategy,
            "strategy_state": state,
            "score": float(assessment.get("score") or 0.0),
            "qualified": True,
            "blockers": [],
            "rsi": round(float(latest["rsi"]), 1),
            "volume_ratio": round(float(latest["volume_ratio"]), 2),
            "relative_24h_pct": round(
                float(relative.get("relative_24h", 0.0)) * 100,
                2,
            ),
            "candle_time": str(latest["timestamp"]),
        }

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
            if self._probation_signal_is_recent(strategy):
                diagnostic["blockers"] = [
                    f"segnale probation già eseguito nelle ultime {self.probation_interval_hours}h"
                ]
                return None, "PROBATION_RATE_LIMIT", diagnostic

        score = float(assessment.get("score") or 0.0)
        if score < required_score:
            diagnostic["blockers"] = [
                f"score {score:.1f} sotto soglia {required_score:.1f}"
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
            float(assessment["target_rr"]),
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

        reasons = list(assessment.get("reasons") or [])
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
        score = min(100.0, score)
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
        diagnostic["net_rr"] = round(float(economics["net_rr"]), 2)
        return signal, "OK", diagnostic

    def _probation_signal_is_recent(self, strategy: str) -> bool:
        if strategy not in self._probation_recent_cache:
            self._probation_recent_cache[strategy] = self.has_recent_probation_signal(strategy)
        return self._probation_recent_cache[strategy]

    def fetch_open_exposure_pairs(self) -> list[str]:
        rows = self.client.fetch_all(
            """SELECT DISTINCT pair
            FROM signals.generated_signals
            WHERE status IN ('NEW', 'OPEN')
              AND signal_class IN ('A', 'B')
            ORDER BY pair"""
        )
        return [str(row[0]) for row in rows]

    def correlated_with_pairs(self, pair: str, exposure_pairs: list[str]) -> list[str]:
        frame = self._frame_cache.get(pair)
        if frame is None or frame.empty:
            return []
        candidate_returns = frame.set_index("timestamp")["close"].pct_change().tail(96)
        correlated: list[str] = []
        for peer_pair in exposure_pairs:
            if peer_pair == pair:
                continue
            peer = self._frame_cache.get(peer_pair)
            if peer is None or peer.empty:
                continue
            peer_returns = peer.set_index("timestamp")["close"].pct_change().tail(96)
            aligned = pd.concat([candidate_returns, peer_returns], axis=1).dropna()
            if len(aligned) < 30:
                continue
            correlation = float(aligned.iloc[:, 0].corr(aligned.iloc[:, 1]))
            if pd.notna(correlation) and correlation >= self.correlation_threshold:
                correlated.append(peer_pair)
        return correlated

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
                  AND timestamp >= NOW() - INTERVAL '3 days'
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
        ordered = sorted(raw, key=lambda item: raw[item]["return_24h"])
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
