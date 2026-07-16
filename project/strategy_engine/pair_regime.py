"""Pair-specific regime alignment for the regime/economics-first engine."""
from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

from project.shared.memory import release_unused_memory
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

    def evaluate(self) -> GeneratedSignal | None:
        """Prefer operative signals, then the C observation closest to class B."""
        try:
            self._candidate_rejection_reasons.clear()
            decision = self.decision_engine.latest_decision or self.decision_engine.evaluate_market()
            candidates = self.prioritize_candidates(
                decision,
                self.fetch_strategy_candidates(limit=self.max_candidate_evaluations),
            )
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
                if (
                    best_watchlist is None
                    or self._watchlist_priority(signal)
                    > self._watchlist_priority(best_watchlist)
                ):
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

    def _watchlist_priority(self, signal: GeneratedSignal) -> tuple[Any, ...]:
        live_score = float(signal.score_breakdown.get("live_setup_raw") or 0.0)
        regime_aligned = bool(signal.score_breakdown.get("regime_aligned_flag"))
        profit_ratio = signal.net_profit_tp1_eur / max(
            self.min_watchlist_net_profit_eur,
            1e-9,
        )
        rr_ratio = signal.net_rr / max(self.min_watchlist_net_rr, 1e-9)
        near_b = (
            profit_ratio >= 1.0
            and rr_ratio >= 1.0
            and live_score >= self.min_watchlist_live_score
            and regime_aligned
        )
        return (
            near_b,
            min(profit_ratio, rr_ratio),
            regime_aligned,
            live_score,
            signal.score,
        )

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
