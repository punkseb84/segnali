"""Operational engine that only consumes statistically eligible OOS research."""
from __future__ import annotations

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
        validation = dict(best.get("validation") or {})
        if not best.get("research_eligible") or validation.get("edge_status") != "ELIGIBLE":
            self.record_candidate_rejection("research_not_eligible")
            self.logger.info(
                "SIGNAL_REJECTED reason=research_not_eligible strategy=%s pair=%s "
                "edge_status=%s total_trades=%s test_trades=%s",
                best.get("strategy"),
                best.get("pair"),
                validation.get("edge_status"),
                validation.get("trades"),
                validation.get("test_trades"),
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

        return super().evaluate_candidate(decision, best, publish_event=publish_event)
