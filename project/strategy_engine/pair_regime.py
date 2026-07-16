"""Pair-specific regime alignment for the regime/economics-first engine."""
from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

from project.strategy_engine.regime_economics import RegimeEconomicsFirstStrategyEngine
from project.strategy_engine.service import GeneratedSignal


REGIME_STRATEGIES: dict[str, tuple[str, ...]] = {
    "COMPRESSION": ("Compression Breakout", "Breakout"),
    "BREAKOUT": ("Breakout", "Compression Breakout", "Momentum", "Volatility Expansion"),
    "HIGH_VOLATILITY": ("Breakout", "Volatility Expansion", "Momentum"),
    "TREND_UP": ("Breakout", "Breakout Retest", "Pullback Trend", "Trend Following", "Momentum"),
    "TREND_DOWN": ("Mean Reversion", "Range Reversal", "Liquidity Sweep"),
    "RANGE": ("Range Reversal", "Mean Reversion", "Liquidity Sweep"),
    "LOW_VOLATILITY": ("Compression Breakout", "Range Reversal", "Mean Reversion"),
}


class PairRegimeEconomicsFirstStrategyEngine(RegimeEconomicsFirstStrategyEngine):
    """Use each market's live regime instead of applying BTC's regime to every pair."""

    def prioritize_candidates(
        self,
        decision: Any,
        candidates: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        ordered = sorted(
            candidates,
            key=lambda item: (
                self._is_pair_regime_aligned(item, decision),
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
                "pair_regime": self._pair_regime(item, decision),
                "aligned": self._is_pair_regime_aligned(item, decision),
                "economic": bool(item.get("economic_target_feasible")),
                "net_rr": round(float(item.get("economic_preview_net_rr") or 0.0), 3),
                "live": round(float(item.get("live_setup_score") or 0.0), 2),
                "trades": int(item.get("research_trades") or 0),
            }
            for item in ordered[:5]
        ]
        self.logger.info(
            "STRATEGY_PRIORITY global_regime=%s pair_specific=true top=%s",
            decision.regime,
            json.dumps(preview, sort_keys=True),
        )
        return ordered

    def evaluate_candidate(
        self,
        decision: Any,
        best: dict[str, Any],
        publish_event: bool = True,
    ) -> GeneratedSignal | None:
        pair_regime = self._pair_regime(best, decision)
        enabled = self._enabled_for_candidate(best, decision)
        pair_decision = SimpleNamespace(
            pair=str(best.get("pair") or getattr(decision, "pair", "")),
            timeframe=str(best.get("timeframe") or getattr(decision, "timeframe", "")),
            regime=pair_regime,
            enabled_strategies=list(enabled),
            reasons=[
                f"pair_specific_regime={pair_regime}",
                f"global_fallback_regime={getattr(decision, 'regime', 'UNKNOWN')}",
            ],
        )
        return super().evaluate_candidate(pair_decision, best, publish_event=publish_event)

    def _pair_regime(self, candidate: dict[str, Any], decision: Any) -> str:
        live_regime = str(candidate.get("live_setup_regime") or "").upper()
        if live_regime in REGIME_STRATEGIES:
            return live_regime
        return str(getattr(decision, "regime", "NO_TRADE"))

    def _enabled_for_candidate(
        self,
        candidate: dict[str, Any],
        decision: Any,
    ) -> tuple[str, ...]:
        pair_regime = self._pair_regime(candidate, decision)
        configured = REGIME_STRATEGIES.get(pair_regime)
        if configured:
            return configured
        return tuple(getattr(decision, "enabled_strategies", ()) or ())

    def _is_pair_regime_aligned(
        self,
        candidate: dict[str, Any],
        decision: Any,
    ) -> bool:
        return str(candidate.get("strategy") or "") in self._enabled_for_candidate(
            candidate,
            decision,
        )
