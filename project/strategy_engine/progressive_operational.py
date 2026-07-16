"""Audited progressive operational classification.

The operational class is based on strict net economics, current setup quality,
strategy edge and regime/validation evidence. Research sample size changes the
reliability of historical metrics and eligibility for class A, but it is not an
independent hidden veto for class B.
"""
from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

from project.shared.events import Event, EventType
from project.strategy_engine.economic_guard import EconomicallyGuardedProbabilisticStrategyEngine
from project.strategy_engine.pair_regime import PairRegimeEconomicsFirstStrategyEngine
from project.strategy_engine.service import GeneratedSignal


class ProgressiveOperationalStrategyEngine(PairRegimeEconomicsFirstStrategyEngine):
    """Classify A/B/C with transparent requirements and sample-aware scoring."""

    ECONOMIC_EPSILON = 1e-9

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
        # The parent filter is disabled: sample size is handled as reliability,
        # not as an all-or-nothing candidate-removal gate.
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
            "Audited classification enabled class_b_reference_trades=%s "
            "class_a_min_trades=%s sample_is_hard_b_gate=false "
            "regime_gate_requested=%s high_score_mismatch_b=%s",
            self.min_research_trades_b,
            self.min_research_trades_a,
            self.requested_regime_alignment,
            self.allow_high_score_regime_mismatch_b,
        )

    def fetch_strategy_candidates(self, limit: int = 10) -> list[dict[str, Any]]:
        """Keep every candidate and annotate setup-specific sample reliability."""
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
            item["research_sample_band"] = self._sample_band(
                research_trades, trades_missing
            )
            item["historical_reliability"] = self._historical_reliability(
                research_trades, trades_missing, probability_sample=0
            )
            key = (str(item["pair"]), str(item["timeframe"]))
            if key not in prices_by_market:
                prices_by_market[key] = self.research_repository.fetch_ohlc(
                    key[0], key[1], limit=self.ohlc_limit
                )
            item.update(self._economic_preview(item, prices_by_market[key]))
            enriched.append(item)

        self.logger.info(
            "STRATEGY_RESEARCH_SAMPLE_DISTRIBUTION input=%s distribution=%s "
            "hard_filter=false class_b_reference=%s class_a_min=%s",
            len(candidates),
            json.dumps(distribution, sort_keys=True),
            self.min_research_trades_b,
            self.min_research_trades_a,
        )
        return enriched

    def build_score_breakdown(
        self,
        best: dict[str, Any],
        economics: dict[str, float | bool],
        validation: dict[str, str],
        probability_context: dict[str, Any],
        regime_aligned: bool,
        ev_decision: dict[str, Any],
    ) -> dict[str, float]:
        """Shrink historical metrics when the setup-specific sample is small."""
        breakdown = super().build_score_breakdown(
            best,
            economics,
            validation,
            probability_context,
            regime_aligned,
            ev_decision,
        )
        reliability = self._historical_reliability(
            int(best.get("research_trades") or 0),
            bool(best.get("research_trades_missing")),
            int(probability_context.get("sample_size") or 0),
        )
        best["historical_reliability"] = reliability
        # Profit factor and expectancy are setup-specific and must not receive full
        # weight when produced by only a handful of historical entries.
        for key in ("profit_factor_score", "expectancy_score"):
            if key in breakdown:
                breakdown[key] = round(float(breakdown[key]) * reliability, 4)
        return breakdown

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
        """Return A/B/C and store the exact, user-visible decision explanation."""
        net_profit = float(economics.get("net_profit_tp1_eur") or 0.0)
        net_rr = float(economics.get("net_rr") or 0.0)
        live_score = float(best.get("live_setup_score") or 0.0)
        profit_factor = float(best.get("profit_factor") or 0.0)
        expectancy = float(best.get("expectancy") or 0.0)
        research_trades = int(best.get("research_trades") or 0)
        research_missing = bool(best.get("research_trades_missing"))
        probability_sample = int(probability_context.get("sample_size") or 0)
        reliability = self._historical_reliability(
            research_trades, research_missing, probability_sample
        )
        best["historical_reliability"] = reliability

        economic_profit_ok = (
            net_profit + self.ECONOMIC_EPSILON >= self.min_tp1_net_profit_eur
        )
        economic_rr_ok = net_rr + self.ECONOMIC_EPSILON >= self.min_net_rr
        score_ok = score >= self.min_operative_signal_score
        live_score_ok = live_score >= self.min_live_setup_score
        profit_factor_ok = profit_factor >= self.min_profit_factor
        expectancy_ok = expectancy > 0.0

        mismatch_buffer_ok = (
            self.allow_high_score_regime_mismatch_b
            and score >= self.min_operative_signal_score + 5.0
            and live_score >= self.min_live_setup_score + 5.0
        )
        regime_ok = (
            regime_aligned
            or not self.requested_regime_alignment
            or mismatch_buffer_ok
        )

        validation_status = str(validation.get("status") or "UNKNOWN")
        validation_reason = str(validation.get("reason") or "UNKNOWN")
        validation_clean = validation_status == "PASSED"
        warnings_buffer_ok = (
            validation_status == "PASSED_WITH_WARNINGS"
            and score >= self.min_operative_signal_score + 7.5
            and live_score >= self.min_live_setup_score + 7.5
            and "STOP_TOO_TIGHT_FOR_VOLATILITY" not in validation_reason
        )
        validation_ok = validation_clean or warnings_buffer_ok

        blockers: list[str] = []
        if not economic_profit_ok:
            blockers.append("NET_PROFIT_BELOW_MINIMUM")
        if not economic_rr_ok:
            blockers.append("NET_RR_BELOW_MINIMUM")
        if not score_ok:
            blockers.append("TOTAL_SCORE_BELOW_MINIMUM")
        if not live_score_ok:
            blockers.append("LIVE_SCORE_BELOW_MINIMUM")
        if not profit_factor_ok:
            blockers.append("PROFIT_FACTOR_BELOW_MINIMUM")
        if not expectancy_ok:
            blockers.append("EXPECTANCY_NOT_POSITIVE")
        if not regime_ok:
            blockers.append("REGIME_MISMATCH_WITHOUT_SCORE_BUFFER")
        if not validation_ok:
            blockers.append("VALIDATION_WARNING_NOT_COMPENSATED")

        confidence_notes: list[str] = []
        if research_missing:
            confidence_notes.append("RESEARCH_SAMPLE_MISSING")
        elif research_trades < self.min_research_trades_b:
            confidence_notes.append("RESEARCH_SAMPLE_LIMITED")
        if probability_sample < self.min_probability_sample_size:
            confidence_notes.append("PROBABILITY_SAMPLE_LOW")
        if reliability < 1.0:
            confidence_notes.append("HISTORICAL_METRICS_SHRUNK")
        ev_reason = str((ev_decision or {}).get("reason") or "")
        if ev_reason in {"ev_unavailable", "low_confidence_no_block"}:
            confidence_notes.append("EV_CONFIDENCE_LIMITED")

        operative_ok = not blockers
        signal_class = "C"
        if operative_ok:
            class_a_ready = (
                score >= 75.0
                and live_score >= 70.0
                and profit_factor >= 1.15
                and research_trades >= self.min_research_trades_a
                and not research_missing
                and regime_aligned
                and validation_clean
                and probability_sample >= self.min_probability_sample_size
            )
            signal_class = "A" if class_a_ready else "B"

        details = {
            "class": signal_class,
            "blockers": blockers,
            "confidence_notes": confidence_notes,
            "net_profit": net_profit,
            "min_net_profit": self.min_tp1_net_profit_eur,
            "net_rr": net_rr,
            "min_net_rr": self.min_net_rr,
            "score": score,
            "min_score": self.min_operative_signal_score,
            "live_score": live_score,
            "min_live_score": self.min_live_setup_score,
            "profit_factor": profit_factor,
            "min_profit_factor": self.min_profit_factor,
            "expectancy": expectancy,
            "research_trades": research_trades,
            "research_missing": research_missing,
            "probability_sample": probability_sample,
            "historical_reliability": reliability,
            "regime_aligned": regime_aligned,
            "regime_buffer_used": bool(not regime_aligned and mismatch_buffer_ok),
            "validation_status": validation_status,
            "validation_reason": validation_reason,
        }
        best["_classification_details"] = details
        self.logger.info(
            "SIGNAL_DECISION class=%s strategy=%s pair=%s blockers=%s "
            "confidence_notes=%s net_profit=%.8f min_profit=%.8f "
            "net_rr=%.8f min_rr=%.8f score=%.2f min_score=%.2f "
            "live_score=%.2f min_live=%.2f research_trades=%s "
            "probability_sample=%s reliability=%.3f regime_aligned=%s "
            "validation=%s",
            signal_class,
            best.get("strategy"),
            best.get("pair"),
            blockers,
            confidence_notes,
            net_profit,
            self.min_tp1_net_profit_eur,
            net_rr,
            self.min_net_rr,
            score,
            self.min_operative_signal_score,
            live_score,
            self.min_live_setup_score,
            research_trades,
            probability_sample,
            reliability,
            regime_aligned,
            validation_status,
        )
        return signal_class

    def evaluate_candidate(
        self,
        decision: Any,
        best: dict[str, Any],
        publish_event: bool = True,
    ) -> GeneratedSignal | None:
        """Attach exact classification diagnostics before notifying Telegram."""
        signal = super().evaluate_candidate(decision, best, publish_event=False)
        if signal is None:
            return None
        details = dict(best.get("_classification_details") or {})
        blockers = list(details.get("blockers") or [])
        confidence_notes = list(details.get("confidence_notes") or [])
        primary_reason = (
            blockers[0]
            if blockers
            else "OPERATIVE_REQUIREMENTS_MET"
            if signal.signal_class in self.operative_signal_classes
            else "WATCHLIST_OBSERVATION"
        )
        reasons = [
            reason
            for reason in signal.reasons
            if not str(reason).startswith(
                (
                    "classification_primary_reason=",
                    "classification_blockers=",
                    "classification_confidence_notes=",
                    "probability_sample=",
                    "net_rr_exact=",
                    "min_net_rr_required=",
                    "min_net_profit_required=",
                )
            )
        ]
        reasons.extend(
            [
                f"classification_primary_reason={primary_reason}",
                "classification_blockers=" + ("|".join(blockers) if blockers else "NONE"),
                "classification_confidence_notes="
                + ("|".join(confidence_notes) if confidence_notes else "NONE"),
                f"probability_sample={int(details.get('probability_sample') or 0)}",
                f"net_rr_exact={signal.net_rr:.8f}",
                f"min_net_rr_required={self.min_net_rr:.8f}",
                f"min_net_profit_required={self.min_tp1_net_profit_eur:.8f}",
            ]
        )
        score_breakdown = dict(signal.score_breakdown)
        score_breakdown.update(
            {
                "classification_economic_profit_ok_flag": 1.0
                if signal.net_profit_tp1_eur + self.ECONOMIC_EPSILON
                >= self.min_tp1_net_profit_eur
                else 0.0,
                "classification_economic_rr_ok_flag": 1.0
                if signal.net_rr + self.ECONOMIC_EPSILON >= self.min_net_rr
                else 0.0,
                "classification_score_ok_flag": 1.0
                if signal.score >= self.min_operative_signal_score
                else 0.0,
                "classification_live_score_ok_flag": 1.0
                if float(best.get("live_setup_score") or 0.0)
                >= self.min_live_setup_score
                else 0.0,
                "research_trades_raw": float(details.get("research_trades") or 0),
                "research_trades_missing_flag": 1.0
                if details.get("research_missing")
                else 0.0,
                "probability_sample_raw": float(details.get("probability_sample") or 0),
                "historical_reliability_raw": float(
                    details.get("historical_reliability") or 0.0
                ),
                "min_net_profit_required_raw": self.min_tp1_net_profit_eur,
                "min_net_rr_required_raw": self.min_net_rr,
                "min_total_score_required_raw": self.min_operative_signal_score,
                "min_live_score_required_raw": self.min_live_setup_score,
                "regime_aligned_flag": 1.0 if details.get("regime_aligned") else 0.0,
                "regime_buffer_used_flag": 1.0
                if details.get("regime_buffer_used")
                else 0.0,
            }
        )
        signal = replace(signal, reasons=reasons, score_breakdown=score_breakdown)
        self._persist_enriched_diagnostics(signal)
        if publish_event:
            self.publish_signal_event(signal)
        return signal

    def publish_signal_event(self, signal: GeneratedSignal) -> None:
        """Notify every operative signal and only useful, explainable C observations."""
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
        research_sample = int(signal.score_breakdown.get("research_trades_raw") or 0)
        research_missing = bool(
            signal.score_breakdown.get("research_trades_missing_flag")
        )
        probability_sample = int(
            signal.score_breakdown.get("probability_sample_raw")
            or signal.historical_sample_size
            or 0
        )
        profit_progress = signal.net_profit_tp1_eur / max(
            self.min_tp1_net_profit_eur, 1e-9
        )
        rr_progress = signal.net_rr / max(self.min_net_rr, 1e-9)
        progress = min(profit_progress, rr_progress)
        sample_available = (
            research_sample >= self.min_watchlist_sample_size
            or probability_sample >= self.min_probability_sample_size
            or research_missing
        )
        classic_near_b = (
            signal.net_profit_tp1_eur >= self.min_watchlist_net_profit_eur
            and signal.net_rr >= self.min_watchlist_net_rr
            and live_score >= self.min_watchlist_live_score
        )
        progressive_c = (
            progress >= self.min_watchlist_progress_ratio
            and live_score >= self.min_watchlist_signal_score
            and sample_available
        )
        if not (classic_near_b or progressive_c):
            self.logger.info(
                "WATCHLIST_NOTIFICATION_SUPPRESSED reason=low_progress id=%s pair=%s "
                "progress=%.3f minimum=%.3f live_score=%.2f "
                "research_sample=%s probability_sample=%s",
                signal.signal_id,
                signal.pair,
                progress,
                self.min_watchlist_progress_ratio,
                live_score,
                research_sample,
                probability_sample,
            )
            return

        self.logger.info(
            "WATCHLIST_AUDITED_NOTIFICATION id=%s pair=%s strategy=%s "
            "progress=%.3f net_profit=%.8f net_rr=%.8f live_score=%.2f "
            "research_sample=%s probability_sample=%s",
            signal.signal_id,
            signal.pair,
            signal.strategy,
            progress,
            signal.net_profit_tp1_eur,
            signal.net_rr,
            live_score,
            research_sample,
            probability_sample,
        )
        self.event_bus.publish(Event(EventType.WATCHLIST, signal.__dict__.copy()))

    def _historical_reliability(
        self,
        research_trades: int,
        missing: bool,
        probability_sample: int,
    ) -> float:
        if missing:
            return 0.50 if probability_sample >= self.min_probability_sample_size else 0.25
        if research_trades >= self.min_research_trades_a:
            return 1.0
        if research_trades >= self.min_research_trades_b:
            return 0.85
        if research_trades >= 10:
            return 0.65
        if research_trades > 0:
            return 0.35
        return 0.20

    def _sample_band(self, trades: int, missing: bool) -> str:
        if missing:
            return "MISSING_USE_MARKET_CONTEXT"
        if trades >= self.min_research_trades_a:
            return "A_READY"
        if trades >= self.min_research_trades_b:
            return "B_CONFIDENCE"
        if trades >= self.min_watchlist_sample_size:
            return "LIMITED_SAMPLE"
        return "VERY_LOW_SAMPLE"
