"""V6.1 daily LONG paper signals with parallel V6 shadow validation.

The V6 empirical gate still controls eventual LIVE_ADMITTED signals. Technically valid
candidates can also be published as clearly labelled PAPER_DAILY signals so the user
receives a useful flow while the shadow sample is being built.
"""
from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any

from project.capital_protection_scanner import RUNTIME_VERSION
from project.capital_protection_v601 import CapitalProtectionReportScannerV601
from project.harmonic_live_scanner import LIVE_LONG_STRATEGIES
from project.shared.events import Event, EventType
from project.strategy_engine.service import GeneratedSignal


PAPER_RUNTIME_VERSION = "DAILY_PAPER_V6_1"
PAPER_EXECUTION_MODE = "PAPER_DAILY"
LIVE_EXECUTION_MODE = "LIVE_ADMITTED"


class DailyPaperSignalScanner(CapitalProtectionReportScannerV601):
    """Publish up to a small daily quota of ranked LONG paper signals."""

    def __init__(
        self,
        *args: Any,
        paper_max_per_day: int = 3,
        paper_max_per_cycle: int = 1,
        paper_min_interval_minutes: int = 120,
        paper_pair_cooldown_minutes: int = 360,
        paper_min_score: float = 64.0,
        paper_min_net_rr: float = 1.05,
        paper_min_net_profit_eur: float = 0.20,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.paper_max_per_day = max(1, min(5, int(paper_max_per_day)))
        self.paper_max_per_cycle = max(1, min(2, int(paper_max_per_cycle)))
        self.paper_min_interval_minutes = max(30, int(paper_min_interval_minutes))
        self.paper_pair_cooldown_minutes = max(90, int(paper_pair_cooldown_minutes))
        self.paper_min_score = max(55.0, float(paper_min_score))
        self.paper_min_net_rr = max(1.0, float(paper_min_net_rr))
        self.paper_min_net_profit_eur = max(0.10, float(paper_min_net_profit_eur))

    def paper_daily_state(self) -> dict[str, Any]:
        rows = self.client.fetch_all(
            """SELECT COUNT(*), MAX(created_at)
            FROM signals.generated_signals
            WHERE score_breakdown->>'paper_runtime_version' = %s
              AND score_breakdown->>'execution_mode' = %s
              AND telegram_sent_at IS NOT NULL
              AND (created_at AT TIME ZONE 'Europe/Rome')::date =
                  (NOW() AT TIME ZONE 'Europe/Rome')::date""",
            (PAPER_RUNTIME_VERSION, PAPER_EXECUTION_MODE),
        )
        count = int(rows[0][0] or 0) if rows else 0
        last_sent = rows[0][1] if rows else None
        if isinstance(last_sent, datetime) and last_sent.tzinfo is None:
            last_sent = last_sent.replace(tzinfo=timezone.utc)
        return {"count": count, "last_sent": last_sent}

    def paper_duplicate_exists(self, signal: GeneratedSignal) -> bool:
        rows = self.client.fetch_all(
            """SELECT id
            FROM signals.generated_signals
            WHERE pair = %s
              AND timeframe = %s
              AND score_breakdown->>'paper_runtime_version' = %s
              AND score_breakdown->>'execution_mode' = %s
              AND created_at >= NOW() - (%s * INTERVAL '1 minute')
            ORDER BY created_at DESC
            LIMIT 1""",
            (
                signal.pair,
                signal.timeframe,
                PAPER_RUNTIME_VERSION,
                PAPER_EXECUTION_MODE,
                self.paper_pair_cooldown_minutes,
            ),
        )
        return bool(rows)

    def live_duplicate_exists(self, signal: GeneratedSignal) -> bool:
        rows = self.client.fetch_all(
            """SELECT id
            FROM signals.generated_signals
            WHERE strategy = %s
              AND pair = %s
              AND timeframe = %s
              AND score_breakdown->>'runtime_version' = %s
              AND score_breakdown->>'execution_mode' = %s
              AND status IN ('NEW', 'OPEN')
              AND telegram_sent_at IS NOT NULL
              AND created_at >= NOW() - (%s * INTERVAL '1 minute')
            ORDER BY created_at DESC
            LIMIT 1""",
            (
                signal.strategy,
                signal.pair,
                signal.timeframe,
                RUNTIME_VERSION,
                LIVE_EXECUTION_MODE,
                self.signal_cooldown_minutes,
            ),
        )
        return bool(rows)

    def fetch_live_exposure_pairs(self) -> list[str]:
        rows = self.client.fetch_all(
            """SELECT DISTINCT pair
            FROM signals.generated_signals
            WHERE score_breakdown->>'runtime_version' = %s
              AND score_breakdown->>'execution_mode' = %s
              AND status IN ('NEW', 'OPEN')
              AND telegram_sent_at IS NOT NULL
              AND created_at >= NOW() - INTERVAL '48 hours'
            ORDER BY pair""",
            (RUNTIME_VERSION, LIVE_EXECUTION_MODE),
        )
        return [str(row[0]) for row in rows]

    def fetch_paper_performance(self) -> dict[str, Any]:
        rows = self.client.fetch_all(
            """SELECT pair, status, net_profit_tp1_eur, net_loss_sl_eur, net_rr, closed_at
            FROM signals.generated_signals
            WHERE score_breakdown->>'paper_runtime_version' = %s
              AND score_breakdown->>'execution_mode' = %s
              AND telegram_sent_at IS NOT NULL
              AND COALESCE(outcome_ambiguous, FALSE) = FALSE
              AND status IN ('TARGET_HIT', 'STOP_LOSS')
              AND closed_at IS NOT NULL
              AND created_at >= NOW() - INTERVAL '120 days'
            ORDER BY closed_at ASC, id ASC""",
            (PAPER_RUNTIME_VERSION, PAPER_EXECUTION_MODE),
        )
        return self.calculate_shadow_statistics(rows, self.shadow_recent_window)

    @staticmethod
    def candidate_rank(signal: GeneratedSignal) -> float:
        return (
            float(signal.score)
            + min(float(signal.net_rr), 2.5) * 6.0
            + min(float(signal.net_profit_tp1_eur), 2.0) * 2.0
        )

    def paper_candidate_allowed(self, signal: GeneratedSignal) -> bool:
        return bool(
            float(signal.score) >= self.paper_min_score
            and float(signal.net_rr) >= self.paper_min_net_rr
            and float(signal.net_profit_tp1_eur) >= self.paper_min_net_profit_eur
            and float(signal.net_loss_sl_eur) <= float(self.max_net_loss_eur) + 1e-9
        )

    def _publish_paper(
        self,
        candidate: GeneratedSignal,
        decision: dict[str, Any],
    ) -> GeneratedSignal:
        breakdown = dict(candidate.score_breakdown or {})
        breakdown.update(
            {
                "runtime_version": PAPER_RUNTIME_VERSION,
                "paper_runtime_version": PAPER_RUNTIME_VERSION,
                "execution_mode": PAPER_EXECUTION_MODE,
                "paper_only": True,
                "capital_protection_runtime": RUNTIME_VERSION,
                "shadow_completed": int(decision["stats"]["completed"]),
                "shadow_profit_factor": round(
                    float(decision["stats"]["profit_factor"]), 4
                ),
                "shadow_expectancy_r": round(
                    float(decision["stats"]["expectancy_r"]), 4
                ),
                "live_admission_blockers": list(decision.get("blockers") or []),
            }
        )
        reasons = list(candidate.reasons or [])
        reasons.append(
            "PAPER DAILY: segnale sperimentale pubblicato mentre la validazione shadow continua"
        )
        paper_candidate = replace(
            candidate,
            score_breakdown=breakdown,
            reasons=reasons,
        )
        signal_id = self.save_signal(paper_candidate)
        signal = replace(paper_candidate, signal_id=signal_id)
        self.publish_signal_event(signal)
        self.logger.info(
            "DAILY_PAPER_SIGNAL id=%s pair=%s strategy=%s regime=%s score=%.1f net_rr=%.2f shadow_n=%s",
            signal_id,
            signal.pair,
            signal.strategy,
            signal.regime,
            signal.score,
            signal.net_rr,
            int(decision["stats"]["completed"]),
        )
        return signal

    def _publish_live(
        self,
        candidate: GeneratedSignal,
        decision: dict[str, Any],
    ) -> GeneratedSignal:
        breakdown = dict(candidate.score_breakdown or {})
        breakdown.update(
            {
                "runtime_version": RUNTIME_VERSION,
                "execution_mode": LIVE_EXECUTION_MODE,
                "shadow_completed": int(decision["stats"]["completed"]),
                "shadow_profit_factor": round(
                    float(decision["stats"]["profit_factor"]), 4
                ),
                "shadow_expectancy_r": round(
                    float(decision["stats"]["expectancy_r"]), 4
                ),
            }
        )
        live_candidate = replace(candidate, score_breakdown=breakdown)
        signal_id = self.save_signal(live_candidate)
        signal = replace(live_candidate, signal_id=signal_id)
        self.publish_signal_event(signal)
        self.logger.info(
            "LIVE_ADMITTED_SIGNAL id=%s pair=%s strategy=%s regime=%s score=%.1f shadow_n=%s shadow_pf=%.2f",
            signal_id,
            signal.pair,
            signal.strategy,
            signal.regime,
            signal.score,
            int(decision["stats"]["completed"]),
            float(decision["stats"]["profit_factor"]),
        )
        return signal

    def evaluate(self) -> GeneratedSignal | None:
        self.expire_stale_signals()
        self._shadow_saved_cycle = 0
        self._admission_cache = {}
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
            for strategy in LIVE_LONG_STRATEGIES
        }

        streak, last_closed = self.fetch_loss_streak()
        self._loss_streak = streak
        self._loss_guard_active = bool(
            streak >= self.loss_streak_trigger
            and last_closed
            and now < last_closed + timedelta(hours=self.loss_guard_hours)
        )

        original_limits = (
            self.max_signals_per_cycle,
            self.min_signal_score,
            self.min_net_rr,
            self.min_tp1_net_profit_eur,
        )
        strict_min_score = float(self.min_signal_score)
        strict_min_rr = float(self.min_net_rr)
        strict_min_profit = float(self.min_tp1_net_profit_eur)
        self.min_signal_score = min(self.min_signal_score, self.paper_min_score)
        self.min_net_rr = min(self.min_net_rr, self.paper_min_net_rr)
        self.min_tp1_net_profit_eur = min(
            self.min_tp1_net_profit_eur, self.paper_min_net_profit_eur
        )

        try:
            candidates: list[GeneratedSignal] = []
            rejection_reasons: dict[str, int] = {}
            regime_counts: dict[str, int] = {}
            for pair in self.pairs:
                try:
                    candidate, reason, diagnostic = self.evaluate_pair_detailed(pair)
                except Exception:
                    self.logger.exception("DAILY_PAPER pair_failed pair=%s", pair)
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

            candidates.sort(key=self.candidate_rank, reverse=True)
            live_open_pairs = self.fetch_live_exposure_pairs()
            available_live_slots = max(0, self.max_open_signals - len(live_open_pairs))
            paper_state = self.paper_daily_state()
            paper_today_before = int(paper_state["count"])
            paper_last_sent = paper_state.get("last_sent")
            paper_remaining = max(0, self.paper_max_per_day - paper_today_before)
            paper_published: list[GeneratedSignal] = []
            live_published: list[GeneratedSignal] = []
            shadowed = 0
            admission_details: list[dict[str, Any]] = []

            for candidate in candidates:
                decision = self.admission_decision(candidate)
                admission_details.append(
                    {
                        "strategy": candidate.strategy,
                        "pair": candidate.pair,
                        "regime": candidate.regime,
                        "admitted": bool(decision["admitted"]),
                        "blockers": list(decision["blockers"])[:3],
                        "completed": int(decision["stats"]["completed"]),
                        "profit_factor": round(
                            float(decision["stats"]["profit_factor"]), 3
                        ),
                        "expectancy_r": round(
                            float(decision["stats"]["expectancy_r"]), 3
                        ),
                    }
                )

                strict_economics = bool(
                    float(candidate.score) >= strict_min_score
                    and float(candidate.net_rr) >= strict_min_rr
                    and float(candidate.net_profit_tp1_eur) >= strict_min_profit
                )
                live_sent = False
                if decision["admitted"] and strict_economics:
                    if available_live_slots <= 0:
                        rejection_reasons["MAX_OPEN_EXPOSURE"] = (
                            rejection_reasons.get("MAX_OPEN_EXPOSURE", 0) + 1
                        )
                    elif len(live_published) >= self.max_signals_per_cycle:
                        rejection_reasons["LIVE_CYCLE_LIMIT"] = (
                            rejection_reasons.get("LIVE_CYCLE_LIMIT", 0) + 1
                        )
                    elif self.live_duplicate_exists(candidate):
                        rejection_reasons["ACTIVE_LIVE_COOLDOWN"] = (
                            rejection_reasons.get("ACTIVE_LIVE_COOLDOWN", 0) + 1
                        )
                    else:
                        exposure_pairs = list(
                            dict.fromkeys(
                                live_open_pairs + [item.pair for item in live_published]
                            )
                        )
                        correlated = self.correlated_with_pairs(
                            candidate.pair, exposure_pairs
                        )
                        if len(correlated) >= self.max_correlated_per_cycle:
                            rejection_reasons["CORRELATED_LIVE_EXPOSURE"] = (
                                rejection_reasons.get("CORRELATED_LIVE_EXPOSURE", 0) + 1
                            )
                        else:
                            signal = self._publish_live(candidate, decision)
                            live_published.append(signal)
                            available_live_slots -= 1
                            live_sent = True

                if live_sent:
                    continue

                if self.save_shadow_signal(candidate, decision) is not None:
                    shadowed += 1

                interval_ok = bool(
                    paper_last_sent is None
                    or now - paper_last_sent
                    >= timedelta(minutes=self.paper_min_interval_minutes)
                )
                paper_allowed = bool(
                    paper_remaining > 0
                    and len(paper_published) < self.paper_max_per_cycle
                    and interval_ok
                    and self.paper_candidate_allowed(candidate)
                    and not self.paper_duplicate_exists(candidate)
                )
                if paper_allowed:
                    signal = self._publish_paper(candidate, decision)
                    paper_published.append(signal)
                    paper_remaining -= 1
                    paper_last_sent = now
                else:
                    reason = (
                        "PAPER_DAILY_CAP"
                        if paper_remaining <= 0
                        else "PAPER_CYCLE_LIMIT"
                        if len(paper_published) >= self.paper_max_per_cycle
                        else "PAPER_MIN_INTERVAL"
                        if not interval_ok
                        else "PAPER_PAIR_COOLDOWN"
                        if self.paper_duplicate_exists(candidate)
                        else "PAPER_REQUIREMENTS_NOT_MET"
                    )
                    rejection_reasons[reason] = rejection_reasons.get(reason, 0) + 1

            ranked = sorted(
                self._pair_diagnostics,
                key=lambda item: float(item.get("score") or 0.0),
                reverse=True,
            )
            best = ranked[0] if ranked else None
            breaker = self.live_circuit_breaker()
            paper_stats = self.fetch_paper_performance()
            total_published = paper_published + live_published
            self._last_cycle_summary = {
                "pairs_scanned": len(self.pairs),
                "qualified": len(candidates),
                "published": len(live_published),
                "paper_published": len(paper_published),
                "paper_today": paper_today_before + len(paper_published),
                "paper_daily_max": self.paper_max_per_day,
                "shadow_saved": shadowed,
                "best_candidate": (
                    f"{best['pair']} {best['strategy']} score={float(best['score']):.1f}"
                    if best
                    else "NONE"
                ),
                "rejections": rejection_reasons,
                "regimes": regime_counts,
                "near_setups": ranked[:5],
                "performance": self.aggregate_forward_performance(
                    self._performance_cache
                ),
                "paper_performance": paper_stats,
                "strategy_states": self._strategy_states,
                "open_positions_before_cycle": len(live_open_pairs),
                "open_positions_after_cycle": len(live_open_pairs)
                + len(live_published),
                "max_open_positions": self.max_open_signals,
                "loss_streak": streak,
                "loss_guard_active": self._loss_guard_active,
                "runtime_version": RUNTIME_VERSION,
                "paper_runtime_version": PAPER_RUNTIME_VERSION,
                "capital_protection": True,
                "daily_paper_mode": True,
                "admission_details": admission_details[:10],
                "live_circuit_breaker": breaker,
            }
            self.logger.info(
                "DAILY_PAPER_SUMMARY pairs=%s qualified=%s shadow=%s paper=%s paper_today=%s/%s live=%s breaker=%s rejections=%s",
                len(self.pairs),
                len(candidates),
                shadowed,
                len(paper_published),
                paper_today_before + len(paper_published),
                self.paper_max_per_day,
                len(live_published),
                bool(breaker["active"]),
                json.dumps(rejection_reasons, sort_keys=True),
            )
            return total_published[0] if total_published else None
        finally:
            (
                self.max_signals_per_cycle,
                self.min_signal_score,
                self.min_net_rr,
                self.min_tp1_net_profit_eur,
            ) = original_limits

    def publish_daily_signal_report(self) -> bool:
        if not self.enable_daily_signal_report:
            return False
        now = datetime.now(timezone.utc)
        if self._last_no_trade_report_at is not None:
            elapsed = (now - self._last_no_trade_report_at).total_seconds() / 3600.0
            if elapsed < self.daily_signal_report_hours:
                return False

        summary = self._last_cycle_summary or {}
        paper_state = self.paper_daily_state()
        paper_stats = self.fetch_paper_performance()
        completed = int(paper_stats.get("completed", 0) or 0)
        if completed:
            paper_result = (
                f"• Paper chiusi non ambigui: <b>{completed}</b>\n"
                f"• Win rate paper: <b>{float(paper_stats.get('win_rate') or 0.0) * 100:.1f}%</b>\n"
                f"• PF paper: <b>{float(paper_stats.get('profit_factor') or 0.0):.2f}</b>\n"
                f"• Expectancy paper: <b>{float(paper_stats.get('expectancy_r') or 0.0):+.3f}R</b>"
            )
        else:
            paper_result = "• Nessun segnale paper V6.1 ancora chiuso: risultati non misurabili."

        overview = self.shadow_overview()
        shadow_lines = [
            f"• {item['strategy']}: <b>{int(item.get('completed', 0) or 0)}</b> chiuse · "
            f"PF <b>{float(item.get('profit_factor', 0.0) or 0.0):.2f}</b> · "
            f"Exp <b>{float(item.get('expectancy_r', 0.0) or 0.0):+.3f}R</b>"
            for item in overview
        ]
        message = (
            "📊 <b>REPORT DAILY PAPER V6.1</b>\n\n"
            "📨 <b>Frequenza segnali</b>\n"
            f"• Paper inviati oggi: <b>{int(paper_state['count'])}/{self.paper_max_per_day}</b>\n"
            f"• Paper inviati ultimo ciclo: <b>{summary.get('paper_published', 0)}</b>\n"
            f"• Setup qualificati ultimo ciclo: <b>{summary.get('qualified', 0)}</b>\n"
            f"• Segnali ammessi live: <b>{summary.get('published', 0)}</b>\n\n"
            "🧪 <b>Risultati paper pubblicati</b>\n"
            + paper_result
            + "\n\n🔬 <b>Validazione shadow parallela</b>\n"
            + "\n".join(shadow_lines)
            + "\n\nI segnali PAPER servono a costruire uno storico visibile; non sono dichiarati profittevoli o validati per denaro reale."
        )
        self.event_bus.publish(
            Event(EventType.REPORT_READY, {"message": message, "trusted_html": True})
        )
        self._last_no_trade_report_at = now
        self.logger.info(
            "DAILY_PAPER_REPORT_SENT paper_today=%s paper_completed=%s",
            int(paper_state["count"]),
            completed,
        )
        return True
