"""Progressive operational classification after the regime/economics-first rollout.

The previous rollout used two hard gates at candidate-selection time: at least forty
research trades and exact regime alignment. Together they could remove every candidate
before the economic guard had a chance to classify it. This layer keeps the strict
net-profit and net-R/R requirements for operative signals, but treats sample size and
regime as confidence evidence instead of universal vetoes.
"""
from __future__ import annotations

import json
from typing import Any

from project.shared.events import Event, EventType
from project.strategy_engine.economic_guard import EconomicallyGuardedProbabilisticStrategyEngine
from project.strategy_engine.pair_regime import PairRegimeEconomicsFirstStrategyEngine
from project.strategy_engine.service import GeneratedSignal


class ProgressiveOperationalStrategyEngine(PairRegimeEconomicsFirstStrategyEngine):
    """Allow robust B signals while reserving A for larger historical samples."""

    def __init__(
        self,
        *args: Any,
        min_research_trades: int = 40,
        min_research_trades_b: int = 20,
        allow_high_score_regime_mismatch_b: bool = True,
        min_watchlist_progress_ratio: float = 0.50,
        min_watchlist_sample_size: int = 10,
        **kwargs: Any,
    ) -> None:
        requested_regime_gate = bool(
            kwargs.pop("require_regime_alignment_for_operative", False)
        )
        # Disable the parent's candidate-removal gate. The requested value becomes
        # the class-A threshold; class B uses a smaller, explicit confidence floor.
        super().__init__(
            *args,
            min_research_trades=0,
            require_regime_alignment_for_operative=False,
            **kwargs,
        )
        self.min_research_trades_a = max(1, int(min_research_trades))
        self.min_research_trades_b = max(
            1, min(int(min_research_trades_b), self.min_research_trades_a)
        )
        self.requested_regime_alignment = requested_regime_gate
        self.allow_high_score_regime_mismatch_b = bool(
            allow_high_score_regime_mismatch_b
        )
        self.min_watchlist_progress_ratio = max(
            0.0, min(1.0, float(min_watchlist_progress_ratio))
        )
        self.min_watchlist_sample_size = max(0, int(min_watchlist_sample_size))
        self.logger.info(
            "Progressive sample policy enabled class_b_trades=%s class_a_trades=%s "
            "regime_gate_requested=%s high_score_mismatch_b=%s",
            self.min_research_trades_b,
            self.min_research_trades_a,
            self.requested_regime_alignment,
            self.allow_high_score_regime_mismatch_b,
        )

    def fetch_strategy_candidates(self, limit: int = 10) -> list[dict[str, Any]]:
        """Keep all research candidates and annotate sample confidence.

        Results generated before ``validation.trades`` existed are not interpreted as
        proven zero-trade configurations. They are marked missing and can rely on the
        independent live probability sample during classification.
        """
        candidates = EconomicallyGuardedProbabilisticStrategyEngine.fetch_strategy_candidates(
            self, limit
        )
        prices_by_market: dict[tuple[str, str], list[tuple[Any, ...]]] = {}
        enriched: list[dict[str, Any]] = []
        distribution = {
            "missing": 0,
            "0_9": 0,
            "10_19": 0,
            "20_39": 0,
            "40_plus": 0,
        }

        for candidate in candidates:
            validation = dict(candidate.get("validation") or {})
            raw_trades = validation.get("trades")
            trades_missing = raw_trades is None
            research_trades = int(raw_trades or 0)
            if trades_missing:
                distribution["missing"] += 1
            elif research_trades < 10:
                distribution["0_9"] += 1
            elif research_trades < 20:
                distribution["10_19"] += 1
            elif research_trades < 40:
                distribution["20_39"] += 1
            else:
                distribution["40_plus"] += 1

            item = dict(candidate)
            item["research_trades"] = research_trades
            item["research_trades_missing"] = trades_missing
            item["research_sample_band"] = self._sample_band(research_trades, trades_missing)
            key = (str(item["pair"]), str(item["timeframe"]))
            if key not in prices_by_market:
                prices_by_market[key] = self.research_repository.fetch_ohlc(
                    key[0], key[1], limit=self.ohlc_limit
                )
            item.update(self._economic_preview(item, prices_by_market[key]))
            enriched.append(item)

        self.logger.info(
            "STRATEGY_RESEARCH_SAMPLE_DISTRIBUTION input=%s distribution=%s "
            "hard_filter=false class_b_min=%s class_a_min=%s",
            len(candidates),
            json.dumps(distribution, sort_keys=True),
            self.min_research_trades_b,
            self.min_research_trades_a,
        )
        return enriched

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
        """Apply strict economics, then progressive confidence downgrades."""
        signal_class = EconomicallyGuardedProbabilisticStrategyEngine.classify_signal(
            self,
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

        research_trades = int(best.get("research_trades") or 0)
        probability_sample = int(probability_context.get("sample_size") or 0)
        effective_sample = research_trades or probability_sample
        live_score = float(best.get("live_setup_score") or 0.0)

        if signal_class == "A":
            if effective_sample < self.min_research_trades_b:
                self._log_progressive_downgrade(
                    "A", "C", "sample_below_class_b", best, effective_sample, score
                )
                return "C"
            if effective_sample < self.min_research_trades_a or not regime_aligned:
                self._log_progressive_downgrade(
                    "A", "B", "class_a_confidence_not_met", best, effective_sample, score
                )
                return "B"
            return "A"

        if effective_sample < self.min_research_trades_b:
            self._log_progressive_downgrade(
                "B", "C", "sample_below_class_b", best, effective_sample, score
            )
            return "C"

        if self.requested_regime_alignment and not regime_aligned:
            strong_mismatch = (
                self.allow_high_score_regime_mismatch_b
                and score >= self.min_operative_signal_score + 5.0
                and live_score >= self.min_live_setup_score + 5.0
            )
            if not strong_mismatch:
                self._log_progressive_downgrade(
                    "B", "C", "regime_mismatch_without_score_buffer", best,
                    effective_sample, score,
                )
                return "C"
            self.logger.info(
                "SIGNAL_CLASS_B_ALLOWED reason=high_score_regime_mismatch "
                "strategy=%s pair=%s score=%.2f live_score=%.2f sample=%s",
                best.get("strategy"), best.get("pair"), score, live_score,
                effective_sample,
            )
        return "B"

    def publish_signal_event(self, signal: GeneratedSignal) -> None:
        """Notify operative signals and useful C observations without flooding."""
        if signal.signal_class not in self.watchlist_signal_classes:
            super().publish_signal_event(signal)
            return
        if not self.enable_watchlist_alerts:
            self.logger.info(
                "WATCHLIST saved notification disabled strategy=%s pair=%s",
                signal.strategy, signal.pair,
            )
            return

        live_score = float(signal.score_breakdown.get("live_setup_raw") or 0.0)
        research_sample = int(signal.score_breakdown.get("research_trades_raw") or 0)
        probability_sample = int(signal.historical_sample_size or 0)
        effective_sample = research_sample or probability_sample
        profit_progress = signal.net_profit_tp1_eur / max(self.min_tp1_net_profit_eur, 1e-9)
        rr_progress = signal.net_rr / max(self.min_net_rr, 1e-9)
        progress = min(profit_progress, rr_progress)
        classic_near_b = (
            signal.net_profit_tp1_eur >= self.min_watchlist_net_profit_eur
            and signal.net_rr >= self.min_watchlist_net_rr
            and live_score >= self.min_watchlist_live_score
        )
        progressive_c = (
            progress >= self.min_watchlist_progress_ratio
            and live_score >= self.min_watchlist_signal_score
            and effective_sample >= self.min_watchlist_sample_size
        )
        if not (classic_near_b or progressive_c):
            self.logger.info(
                "WATCHLIST_NOTIFICATION_SUPPRESSED reason=low_progress id=%s pair=%s "
                "progress=%.3f minimum=%.3f live_score=%.2f sample=%s",
                signal.signal_id, signal.pair, progress,
                self.min_watchlist_progress_ratio, live_score, effective_sample,
            )
            return

        self.logger.info(
            "WATCHLIST_PROGRESSIVE_NOTIFICATION id=%s pair=%s strategy=%s "
            "progress=%.3f net_profit=%.6f net_rr=%.4f live_score=%.2f sample=%s",
            signal.signal_id, signal.pair, signal.strategy, progress,
            signal.net_profit_tp1_eur, signal.net_rr, live_score, effective_sample,
        )
        self.event_bus.publish(Event(EventType.WATCHLIST, signal.__dict__.copy()))

    def _log_progressive_downgrade(
        self,
        source: str,
        target: str,
        reason: str,
        best: dict[str, Any],
        sample: int,
        score: float,
    ) -> None:
        self.logger.info(
            "SIGNAL_DOWNGRADED class_from=%s class_to=%s reason=%s strategy=%s "
            "pair=%s sample=%s score=%.2f",
            source, target, reason, best.get("strategy"), best.get("pair"), sample, score,
        )

    def _sample_band(self, trades: int, missing: bool) -> str:
        if missing:
            return "MISSING_USE_LIVE_SAMPLE"
        if trades >= self.min_research_trades_a:
            return "A_READY"
        if trades >= self.min_research_trades_b:
            return "B_READY"
        if trades >= self.min_watchlist_sample_size:
            return "WATCHLIST_ONLY"
        return "LOW_CONFIDENCE"
