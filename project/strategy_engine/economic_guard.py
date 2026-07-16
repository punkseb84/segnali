"""Economic safety guard for probabilistic operational signals.

The signal engine remains probabilistic for technical and statistical evidence, but
an operative A/B signal must still satisfy the configured minimum net profit and
net risk/reward. Candidates below those economic thresholds are downgraded to the
watchlist class C rather than being discarded.
"""
from __future__ import annotations

from typing import Any

from project.strategy_engine.probabilistic_service import ProbabilisticStrategyEngine as _ProbabilisticStrategyEngine


class EconomicallyGuardedProbabilisticStrategyEngine(_ProbabilisticStrategyEngine):
    """Prevent economically unusable candidates from becoming operative signals."""

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
        if signal_class not in {"A", "B"}:
            return signal_class

        net_profit = float(economics.get("net_profit_tp1_eur") or 0.0)
        net_rr = float(economics.get("net_rr") or 0.0)
        profit_ok = net_profit >= self.min_tp1_net_profit_eur
        rr_ok = net_rr >= self.min_net_rr
        if profit_ok and rr_ok:
            return signal_class

        self.logger.info(
            "SIGNAL_DOWNGRADED class_from=%s class_to=C reason=economic_minimums "
            "net_profit=%.6f min_net_profit=%.6f net_rr=%.4f min_net_rr=%.4f",
            signal_class,
            net_profit,
            self.min_tp1_net_profit_eur,
            net_rr,
            self.min_net_rr,
        )
        return "C"
