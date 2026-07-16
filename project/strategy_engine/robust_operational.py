"""Operational engine that only consumes statistically eligible OOS research."""
from __future__ import annotations

from dataclasses import replace
from typing import Any

from project.strategy_engine.operational_integrity import OperationalIntegrityStrategyEngine
from project.strategy_engine.service import GeneratedSignal


class RobustEdgeOperationalStrategyEngine(OperationalIntegrityStrategyEngine):
    """Reject research-only observations before A/B/C live classification.

    Class C is reserved for a statistically validated strategy that is close to an
    operative setup. Insufficient research is not a watchlist signal.
    """

    def fetch_strategy_candidates(self, limit: int = 10) -> list[dict[str, Any]]:
        candidates = super().fetch_strategy_candidates(limit=limit)
        eligible: list[dict[str, Any]] = []
        for candidate in candidates:
            validation = dict(candidate.get("validation") or {})
            if not candidate.get("research_eligible"):
                self.record_candidate_rejection("research_not_eligible")
                continue
            if validation.get("edge_status") != "ELIGIBLE":
                self.record_candidate_rejection("oos_edge_not_eligible")
                continue
            if int(validation.get("test_trades") or 0) <= 0:
                self.record_candidate_rejection("missing_oos_trades")
                continue
            eligible.append(candidate)
        self.logger.info(
            "ROBUST_EDGE_CANDIDATES input=%s eligible=%s rejected=%s",
            len(candidates),
            len(eligible),
            len(candidates) - len(eligible),
        )
        return eligible

    def evaluate_candidate(
        self,
        decision: Any,
        best: dict[str, Any],
        publish_event: bool = True,
    ) -> GeneratedSignal | None:
        research_validation = dict(best.get("validation") or {})
        if not best.get("research_eligible") or research_validation.get("edge_status") != "ELIGIBLE":
            self.record_candidate_rejection("research_not_eligible")
            self.logger.info(
                "SIGNAL_REJECTED reason=research_not_eligible strategy=%s pair=%s "
                "edge_status=%s total_trades=%s test_trades=%s",
                best.get("strategy"),
                best.get("pair"),
                research_validation.get("edge_status"),
                research_validation.get("trades"),
                research_validation.get("test_trades"),
            )
            return None

        pair_regime = self._pair_regime(best, decision)
        if pair_regime == "TREND_DOWN":
            self.record_candidate_rejection("long_blocked_in_trend_down")
            self.logger.info(
                "SIGNAL_REJECTED reason=long_blocked_in_trend_down strategy=%s pair=%s "
                "timeframe=%s research_regime=%s",
                best.get("strategy"),
                best.get("pair"),
                best.get("timeframe"),
                dict(best.get("parameters") or {}).get("market_regime"),
            )
            return None

        signal = super().evaluate_candidate(decision, best, publish_event=False)
        if signal is None:
            return None

        total_trades = int(research_validation.get("trades") or 0)
        train_trades = int(research_validation.get("train_trades") or 0)
        test_trades = int(research_validation.get("test_trades") or 0)
        train_pf = float(research_validation.get("train_profit_factor") or 0.0)
        test_pf = float(research_validation.get("test_profit_factor") or best.get("profit_factor") or 0.0)
        train_ev = float(research_validation.get("train_expectancy") or 0.0)
        test_ev = float(research_validation.get("test_expectancy") or best.get("expectancy") or 0.0)
        reasons = [
            reason
            for reason in signal.reasons
            if not str(reason).startswith(
                (
                    "research_validation=",
                    "research_total_trades=",
                    "research_train_trades=",
                    "research_test_trades=",
                    "research_train_pf=",
                    "research_test_pf=",
                    "research_train_expectancy=",
                    "research_test_expectancy=",
                )
            )
        ]
        reasons.extend(
            [
                "research_validation=walk_forward_out_of_sample",
                f"research_total_trades={total_trades}",
                f"research_train_trades={train_trades}",
                f"research_test_trades={test_trades}",
                f"research_train_pf={train_pf:.4f}",
                f"research_test_pf={test_pf:.4f}",
                f"research_train_expectancy={train_ev:.6f}",
                f"research_test_expectancy={test_ev:.6f}",
            ]
        )
        breakdown = dict(signal.score_breakdown)
        breakdown.update(
            {
                "robust_research_flag": 1.0,
                "research_train_trades_raw": float(train_trades),
                "research_test_trades_raw": float(test_trades),
                "research_train_pf_raw": train_pf,
                "research_test_pf_raw": test_pf,
                "research_train_expectancy_raw": train_ev,
                "research_test_expectancy_raw": test_ev,
            }
        )
        signal = replace(signal, reasons=reasons, score_breakdown=breakdown)
        self._persist_enriched_diagnostics(signal)
        self.logger.info(
            "ROBUST_SIGNAL_EVIDENCE class=%s strategy=%s pair=%s total_trades=%s "
            "train_trades=%s test_trades=%s train_pf=%.4f test_pf=%.4f "
            "train_ev=%.6f test_ev=%.6f",
            signal.signal_class,
            signal.strategy,
            signal.pair,
            total_trades,
            train_trades,
            test_trades,
            train_pf,
            test_pf,
            train_ev,
            test_ev,
        )
        if publish_event:
            self.publish_signal_event(signal)
        return signal
