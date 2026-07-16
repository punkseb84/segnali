"""Regime-first and economics-first operational signal selection.

This layer keeps the existing probabilistic score and strict A/B economic guard, but
changes candidate priority and target construction so the engine evaluates first the
setups that can realistically become operative. Weak C observations are still stored
for research, while only near-B watchlists are sent to Telegram.
"""
from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

from project.shared.events import Event, EventType
from project.shared.memory import release_unused_memory
from project.strategy_engine.economic_guard import EconomicallyGuardedProbabilisticStrategyEngine
from project.strategy_engine.service import GeneratedSignal


class RegimeEconomicsFirstStrategyEngine(EconomicallyGuardedProbabilisticStrategyEngine):
    """Prioritize regime-aligned, economically feasible and statistically robust setups."""

    def __init__(
        self,
        *args: Any,
        min_research_trades: int = 40,
        require_regime_alignment_for_operative: bool = True,
        dynamic_economic_target: bool = True,
        min_watchlist_net_profit_eur: float = 0.20,
        min_watchlist_net_rr: float = 0.85,
        min_watchlist_live_score: float = 45.0,
        pair_spread_rates: dict[str, float] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.min_research_trades = max(0, int(min_research_trades))
        self.require_regime_alignment_for_operative = bool(
            require_regime_alignment_for_operative
        )
        self.dynamic_economic_target = bool(dynamic_economic_target)
        self.min_watchlist_net_profit_eur = max(0.0, float(min_watchlist_net_profit_eur))
        self.min_watchlist_net_rr = max(0.0, float(min_watchlist_net_rr))
        self.min_watchlist_live_score = max(0.0, float(min_watchlist_live_score))
        self.pair_spread_rates = {
            str(pair).upper(): max(0.0, float(rate))
            for pair, rate in (pair_spread_rates or {}).items()
        }
        self.logger.info(
            "Regime/economics priority enabled min_research_trades=%s "
            "require_regime_alignment=%s dynamic_target=%s",
            self.min_research_trades,
            self.require_regime_alignment_for_operative,
            self.dynamic_economic_target,
        )
        self.logger.info(
            "Near-B watchlist alerts min_profit=%.4f min_rr=%.4f min_live_score=%.2f",
            self.min_watchlist_net_profit_eur,
            self.min_watchlist_net_rr,
            self.min_watchlist_live_score,
        )

    def evaluate(self) -> GeneratedSignal | None:
        """Evaluate candidates in regime/economic order and retain memory trimming."""
        try:
            self._candidate_rejection_reasons.clear()
            decision = self.decision_engine.latest_decision or self.decision_engine.evaluate_market()
            candidates = self.fetch_strategy_candidates(limit=self.max_candidate_evaluations)
            candidates = self.prioritize_candidates(decision, candidates)
            if not candidates:
                diagnostics = self.research_repository.fetch_candidate_diagnostics(
                    timeframe=self.operational_timeframe
                )
                self._last_cycle_summary = {
                    "timeframe": self.operational_timeframe,
                    "candidates": 0,
                    "class_a": 0,
                    "class_b": 0,
                    "class_c": 0,
                    "rejected": 0,
                    "top_rejections": {},
                    "best_candidate": "NONE",
                    "diagnostics": diagnostics,
                }
                self.logger.info(
                    "STRATEGY no robust research candidate available timeframe=%s "
                    "min_research_trades=%s diagnostics=%s",
                    self.operational_timeframe,
                    self.min_research_trades,
                    json.dumps(diagnostics, sort_keys=True),
                )
                return None

            best_watchlist: GeneratedSignal | None = None
            class_counts = {"A": 0, "B": 0, "C": 0}
            evaluated = 0
            for best in candidates:
                evaluated += 1
                signal = self.evaluate_candidate(decision, best, publish_event=False)
                if signal is None:
                    continue
                class_counts[signal.signal_class] = class_counts.get(signal.signal_class, 0) + 1
                if signal.signal_class in self.operative_signal_classes:
                    self.log_cycle_summary(evaluated, class_counts, signal)
                    self.publish_signal_event(signal)
                    return signal
                if best_watchlist is None or signal.score > best_watchlist.score:
                    best_watchlist = signal

            self.log_cycle_summary(evaluated, class_counts, best_watchlist)
            if best_watchlist is not None:
                self.publish_signal_event(best_watchlist)
                return best_watchlist
            return None
        finally:
            rss_mb = release_unused_memory()
            if rss_mb is not None:
                self.logger.info("MEMORY_RELEASE phase=strategy rss_mb=%.1f", rss_mb)

    def fetch_strategy_candidates(self, limit: int = 10) -> list[dict[str, Any]]:
        """Remove fragile samples and add a realistic economic target preview."""
        candidates = super().fetch_strategy_candidates(limit)
        prices_by_market: dict[tuple[str, str], list[tuple[Any, ...]]] = {}
        robust: list[dict[str, Any]] = []

        for candidate in candidates:
            validation = dict(candidate.get("validation") or {})
            research_trades = int(validation.get("trades") or 0)
            if research_trades < self.min_research_trades:
                self.record_candidate_rejection("insufficient_research_trades")
                self.logger.info(
                    "STRATEGY candidate skipped reason=insufficient_research_trades "
                    "strategy=%s pair=%s trades=%s minimum=%s",
                    candidate.get("strategy"),
                    candidate.get("pair"),
                    research_trades,
                    self.min_research_trades,
                )
                continue

            item = dict(candidate)
            item["research_trades"] = research_trades
            key = (str(item["pair"]), str(item["timeframe"]))
            if key not in prices_by_market:
                prices_by_market[key] = self.research_repository.fetch_ohlc(
                    key[0], key[1], limit=self.ohlc_limit
                )
            item.update(self._economic_preview(item, prices_by_market[key]))
            robust.append(item)

        self.logger.info(
            "STRATEGY_RESEARCH_SAMPLE_FILTER input=%s robust=%s minimum_trades=%s",
            len(candidates),
            len(robust),
            self.min_research_trades,
        )
        return robust

    def prioritize_candidates(
        self,
        decision: Any,
        candidates: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        enabled = set(decision.enabled_strategies)
        ordered = sorted(
            candidates,
            key=lambda item: (
                str(item.get("strategy")) in enabled,
                bool(item.get("economic_target_feasible")),
                float(item.get("live_setup_score") or 0.0),
                int(item.get("research_trades") or 0),
                float(item.get("expectancy") or 0.0),
                float(item.get("profit_factor") or 0.0),
            ),
            reverse=True,
        )
        preview = [
            {
                "pair": item.get("pair"),
                "strategy": item.get("strategy"),
                "aligned": str(item.get("strategy")) in enabled,
                "economic": bool(item.get("economic_target_feasible")),
                "live": round(float(item.get("live_setup_score") or 0.0), 2),
                "trades": int(item.get("research_trades") or 0),
            }
            for item in ordered[:5]
        ]
        self.logger.info(
            "STRATEGY_PRIORITY regime=%s enabled=%s top=%s",
            decision.regime,
            sorted(enabled),
            json.dumps(preview, sort_keys=True),
        )
        return ordered

    def evaluate_candidate(
        self,
        decision: Any,
        best: dict[str, Any],
        publish_event: bool = True,
    ) -> GeneratedSignal | None:
        """Use pair-specific costs and attach the diagnostics used for C/B decisions."""
        previous_spread = self.spread_rate
        self.spread_rate = self._spread_for_pair(str(best.get("pair") or ""))
        try:
            signal = super().evaluate_candidate(decision, best, publish_event=False)
        finally:
            self.spread_rate = previous_spread
        if signal is None:
            return None

        regime_aligned = str(best.get("strategy")) in set(decision.enabled_strategies)
        live_score = float(best.get("live_setup_score") or 0.0)
        target_feasible = bool(best.get("economic_target_feasible"))
        target_adjusted = bool(best.get("economic_target_adjusted"))
        research_trades = int(best.get("research_trades") or 0)
        enriched_breakdown = dict(signal.score_breakdown)
        enriched_breakdown.update(
            {
                "live_setup_raw": live_score,
                "regime_aligned_flag": 1.0 if regime_aligned else 0.0,
                "economic_target_feasible_flag": 1.0 if target_feasible else 0.0,
                "research_trades_raw": float(research_trades),
            }
        )
        enriched_reasons = list(signal.reasons)
        enriched_reasons.extend(
            [
                f"research_trades={research_trades}",
                f"live_setup_score={live_score:.4f}",
                f"economic_target_feasible={'true' if target_feasible else 'false'}",
                f"economic_target_adjusted={'true' if target_adjusted else 'false'}",
                f"original_reward_risk={float(best.get('original_reward_risk') or 0.0):.4f}",
                f"effective_reward_risk={float(best.get('effective_reward_risk') or 0.0):.4f}",
                f"pair_spread_rate={self._spread_for_pair(signal.pair):.6f}",
            ]
        )
        signal = replace(
            signal,
            reasons=enriched_reasons,
            score_breakdown=enriched_breakdown,
        )
        self._persist_enriched_diagnostics(signal)
        if publish_event:
            self.publish_signal_event(signal)
        return signal

    def classify_signal(
        self,
        best: dict[str, Any],
        economics: dict[str, float | bool],
        validation: dict[str, str],
        probability_context: dict[str, Any],
        regime_aligned: bool,
        ev_decision: dict[str, Any] | None = None,
        score: float = 0.0,
    ) -> str:
        signal_class = super().classify_signal(
            best,
            economics,
            validation,
            probability_context,
            regime_aligned,
            ev_decision,
            score,
        )
        if signal_class not in self.operative_signal_classes:
            return signal_class
        if not bool(best.get("economic_target_feasible")):
            self.logger.info(
                "SIGNAL_DOWNGRADED class_from=%s class_to=C reason=target_not_realistically_feasible "
                "strategy=%s pair=%s",
                signal_class,
                best.get("strategy"),
                best.get("pair"),
            )
            return "C"
        if self.require_regime_alignment_for_operative and not regime_aligned:
            self.logger.info(
                "SIGNAL_DOWNGRADED class_from=%s class_to=C reason=regime_not_aligned "
                "strategy=%s pair=%s",
                signal_class,
                best.get("strategy"),
                best.get("pair"),
            )
            return "C"
        return signal_class

    def publish_signal_event(self, signal: GeneratedSignal) -> None:
        if signal.signal_class not in self.watchlist_signal_classes:
            super().publish_signal_event(signal)
            return
        if not self.enable_watchlist_alerts:
            self.logger.info(
                "WATCHLIST saved notification disabled strategy=%s pair=%s",
                signal.strategy,
                signal.pair,
            )
            return

        live_score = float(signal.score_breakdown.get("live_setup_raw") or 0.0)
        regime_aligned = bool(signal.score_breakdown.get("regime_aligned_flag"))
        near_b = (
            signal.net_profit_tp1_eur >= self.min_watchlist_net_profit_eur
            and signal.net_rr >= self.min_watchlist_net_rr
            and live_score >= self.min_watchlist_live_score
            and regime_aligned
        )
        if not near_b:
            self.logger.info(
                "WATCHLIST_NOTIFICATION_SUPPRESSED reason=not_near_b id=%s pair=%s strategy=%s "
                "net_profit=%.6f min_profit=%.6f net_rr=%.4f min_rr=%.4f "
                "live_score=%.2f min_live=%.2f regime_aligned=%s",
                signal.signal_id,
                signal.pair,
                signal.strategy,
                signal.net_profit_tp1_eur,
                self.min_watchlist_net_profit_eur,
                signal.net_rr,
                self.min_watchlist_net_rr,
                live_score,
                self.min_watchlist_live_score,
                regime_aligned,
            )
            return
        self.logger.info(
            "WATCHLIST_NEAR_B_NOTIFICATION id=%s pair=%s strategy=%s net_profit=%.6f "
            "net_rr=%.4f live_score=%.2f",
            signal.signal_id,
            signal.pair,
            signal.strategy,
            signal.net_profit_tp1_eur,
            signal.net_rr,
            live_score,
        )
        self.event_bus.publish(Event(EventType.WATCHLIST, signal.__dict__.copy()))

    def _economic_preview(
        self,
        candidate: dict[str, Any],
        prices: list[tuple[Any, ...]],
    ) -> dict[str, Any]:
        if len(prices) < 20:
            return {
                "economic_target_feasible": False,
                "economic_target_adjusted": False,
                "economic_preview_net_profit": 0.0,
                "economic_preview_net_rr": 0.0,
            }
        entry = float(prices[0][4])
        atr_value = self.calculate_atr(prices)
        parameters = dict(candidate.get("parameters") or {})
        original_rr = float(
            parameters.get(
                "reward_risk",
                dict(candidate.get("validation") or {}).get("reward_risk", 1.5),
            )
            or 1.5
        )
        atr_multiplier = float(
            parameters.get(
                "atr_multiplier",
                dict(candidate.get("validation") or {}).get("atr_multiplier", 1.0),
            )
            or 1.0
        )
        stop_distance = max(atr_value * atr_multiplier, entry * 0.0001)
        stop_loss = max(entry - stop_distance, entry * 0.98)
        stop_distance = entry - stop_loss
        plan = self._reward_risk_plan(
            pair=str(candidate.get("pair") or ""),
            prices=prices,
            entry=entry,
            stop_loss=stop_loss,
            stop_distance=stop_distance,
            atr_value=atr_value,
            original_rr=original_rr,
        )
        parameters["reward_risk"] = plan["effective_reward_risk"]
        return {
            "parameters": parameters,
            "original_reward_risk": original_rr,
            **plan,
        }

    def _reward_risk_plan(
        self,
        pair: str,
        prices: list[tuple[Any, ...]],
        entry: float,
        stop_loss: float,
        stop_distance: float,
        atr_value: float,
        original_rr: float,
    ) -> dict[str, Any]:
        if stop_distance <= 0:
            return self._infeasible_plan(original_rr)

        distance_rr_cap = entry * self.max_take_profit_distance_pct / stop_distance
        atr_rr_cap = (
            atr_value * self.max_tp_atr_multiple / stop_distance
            if atr_value > 0
            else distance_rr_cap
        )
        caps = [value for value in (distance_rr_cap, atr_rr_cap) if value > 0]
        mfe_distance, mfe_samples = self._historical_mfe_distance(prices)
        if mfe_samples >= self.min_probability_sample_size and mfe_distance > 0:
            caps.append(mfe_distance / stop_distance)
        rr_cap = min(caps) if caps else 0.0
        if rr_cap <= 0:
            return self._infeasible_plan(original_rr)

        if not self.dynamic_economic_target:
            effective_rr = min(original_rr, rr_cap)
            economics = self._economics_for_rr(pair, entry, stop_loss, effective_rr)
            return self._plan_from_economics(
                original_rr,
                effective_rr,
                rr_cap,
                economics,
            )

        upper_economics = self._economics_for_rr(pair, entry, stop_loss, rr_cap)
        if not self._meets_economic_minimums(upper_economics):
            return self._plan_from_economics(
                original_rr,
                min(original_rr, rr_cap),
                rr_cap,
                self._economics_for_rr(pair, entry, stop_loss, min(original_rr, rr_cap)),
                feasible=False,
            )

        low = 0.0
        high = rr_cap
        for _ in range(50):
            middle = (low + high) / 2.0
            if self._meets_economic_minimums(
                self._economics_for_rr(pair, entry, stop_loss, middle)
            ):
                high = middle
            else:
                low = middle
        required_rr = high
        effective_rr = min(max(original_rr, required_rr), rr_cap)
        economics = self._economics_for_rr(pair, entry, stop_loss, effective_rr)
        return self._plan_from_economics(
            original_rr,
            effective_rr,
            rr_cap,
            economics,
            feasible=self._meets_economic_minimums(economics),
            required_rr=required_rr,
        )

    def _economics_for_rr(
        self,
        pair: str,
        entry: float,
        stop_loss: float,
        reward_risk: float,
    ) -> dict[str, float | bool]:
        take_profit = entry + (entry - stop_loss) * max(0.0, reward_risk)
        previous_spread = self.spread_rate
        self.spread_rate = self._spread_for_pair(pair)
        try:
            return self.calculate_net_economics(entry, stop_loss, take_profit)
        finally:
            self.spread_rate = previous_spread

    def _historical_mfe_distance(
        self,
        prices: list[tuple[Any, ...]],
    ) -> tuple[float, int]:
        chronological = list(reversed(prices))
        horizon = max(1, self.probability_horizon_candles)
        excursions: list[float] = []
        for index in range(0, max(0, len(chronological) - horizon - 1)):
            sample_entry = float(chronological[index][4])
            future = chronological[index + 1 : index + 1 + horizon]
            if future:
                excursions.append(
                    max(float(row[2]) - sample_entry for row in future)
                )
        return self.percentile(excursions, self.historical_mfe_percentile), len(excursions)

    def _spread_for_pair(self, pair: str) -> float:
        return self.pair_spread_rates.get(pair.upper(), self.spread_rate)

    def _meets_economic_minimums(
        self,
        economics: dict[str, float | bool],
    ) -> bool:
        return (
            float(economics.get("net_profit_tp1_eur") or 0.0)
            >= self.min_tp1_net_profit_eur
            and float(economics.get("net_rr") or 0.0) >= self.min_net_rr
        )

    @staticmethod
    def _infeasible_plan(original_rr: float) -> dict[str, Any]:
        return {
            "economic_target_feasible": False,
            "economic_target_adjusted": False,
            "economic_preview_net_profit": 0.0,
            "economic_preview_net_rr": 0.0,
            "effective_reward_risk": original_rr,
            "required_reward_risk": None,
            "reward_risk_cap": 0.0,
        }

    def _plan_from_economics(
        self,
        original_rr: float,
        effective_rr: float,
        rr_cap: float,
        economics: dict[str, float | bool],
        feasible: bool | None = None,
        required_rr: float | None = None,
    ) -> dict[str, Any]:
        if feasible is None:
            feasible = self._meets_economic_minimums(economics)
        return {
            "economic_target_feasible": bool(feasible),
            "economic_target_adjusted": abs(effective_rr - original_rr) > 1e-6,
            "economic_preview_net_profit": float(
                economics.get("net_profit_tp1_eur") or 0.0
            ),
            "economic_preview_net_rr": float(economics.get("net_rr") or 0.0),
            "effective_reward_risk": effective_rr,
            "required_reward_risk": required_rr,
            "reward_risk_cap": rr_cap,
        }

    def _persist_enriched_diagnostics(self, signal: GeneratedSignal) -> None:
        if signal.signal_id is None:
            return
        try:
            self.research_repository.client.execute(
                """UPDATE signals.generated_signals
                SET reasons = %s::jsonb,
                    score_breakdown = %s::jsonb
                WHERE id = %s""",
                (
                    json.dumps(signal.reasons),
                    json.dumps(signal.score_breakdown),
                    signal.signal_id,
                ),
            )
        except Exception as exc:
            self.logger.warning(
                "Signal diagnostic enrichment failed id=%s error=%s",
                signal.signal_id,
                exc,
            )
