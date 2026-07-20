"""V6 capital-protection scanner.

Technically valid setups are first followed as shadow trades. Live Telegram signals are
admitted only after the same strategy and market regime demonstrate a positive after-cost
edge. This prevents real money from being used as the validation sample.
"""
from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any

from project.harmonic_live_scanner import LIVE_LONG_STRATEGIES
from project.strategy_engine.service import GeneratedSignal
from project.validated_report_scanner import ValidatedReportStrategyScanner


RUNTIME_VERSION = "CAPITAL_PROTECTION_V6"


class CapitalProtectionScanner(ValidatedReportStrategyScanner):
    def __init__(
        self,
        *args: Any,
        shadow_min_samples: int = 30,
        shadow_min_profit_factor: float = 1.30,
        shadow_min_expectancy_r: float = 0.10,
        shadow_min_win_margin: float = 0.03,
        shadow_recent_window: int = 10,
        shadow_max_loss_streak: int = 4,
        max_shadow_per_cycle: int = 3,
        live_stop_trigger: int = 3,
        live_stop_24h_limit: int = 2,
        live_cooldown_hours: int = 24,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.shadow_min_samples = max(20, int(shadow_min_samples))
        self.shadow_min_profit_factor = max(1.05, float(shadow_min_profit_factor))
        self.shadow_min_expectancy_r = max(0.0, float(shadow_min_expectancy_r))
        self.shadow_min_win_margin = max(0.0, float(shadow_min_win_margin))
        self.shadow_recent_window = max(5, int(shadow_recent_window))
        self.shadow_max_loss_streak = max(2, int(shadow_max_loss_streak))
        self.max_shadow_per_cycle = max(1, int(max_shadow_per_cycle))
        self.live_stop_trigger = max(2, int(live_stop_trigger))
        self.live_stop_24h_limit = max(1, int(live_stop_24h_limit))
        self.live_cooldown_hours = max(3, int(live_cooldown_hours))
        self._shadow_saved_cycle = 0
        self._admission_cache: dict[tuple[str, str, str], dict[str, Any]] = {}

    def fetch_forward_performance(self) -> dict[str, dict[str, Any]]:
        placeholders = ", ".join(["%s"] * len(LIVE_LONG_STRATEGIES))
        rows = self.client.fetch_all(
            f"""SELECT strategy, status, net_profit_tp1_eur, net_loss_sl_eur, closed_at
            FROM signals.generated_signals
            WHERE strategy IN ({placeholders})
              AND score_breakdown->>'runtime_version' = %s
              AND telegram_sent_at IS NOT NULL
              AND COALESCE(outcome_ambiguous, FALSE) = FALSE
              AND status IN ('TARGET_HIT', 'STOP_LOSS')
              AND created_at >= NOW() - INTERVAL '120 days'
            ORDER BY closed_at ASC, id ASC""",
            (*LIVE_LONG_STRATEGIES, RUNTIME_VERSION),
        )
        grouped: dict[str, list[tuple[Any, ...]]] = {
            name: [] for name in LIVE_LONG_STRATEGIES
        }
        for row in rows:
            grouped.setdefault(str(row[0]), []).append(row)
        return {
            strategy: self.calculate_enhanced_performance(items)
            for strategy, items in grouped.items()
        }

    def fetch_loss_streak(self) -> tuple[int, datetime | None]:
        placeholders = ", ".join(["%s"] * len(LIVE_LONG_STRATEGIES))
        rows = self.client.fetch_all(
            f"""SELECT status, closed_at
            FROM signals.generated_signals
            WHERE strategy IN ({placeholders})
              AND score_breakdown->>'runtime_version' = %s
              AND telegram_sent_at IS NOT NULL
              AND COALESCE(outcome_ambiguous, FALSE) = FALSE
              AND status IN ('TARGET_HIT', 'STOP_LOSS')
              AND closed_at IS NOT NULL
              AND closed_at >= NOW() - INTERVAL '30 days'
            ORDER BY closed_at DESC, id DESC
            LIMIT 12""",
            (*LIVE_LONG_STRATEGIES, RUNTIME_VERSION),
        )
        streak = 0
        last_closed: datetime | None = None
        for status, closed_at in rows:
            if last_closed is None and isinstance(closed_at, datetime):
                last_closed = (
                    closed_at if closed_at.tzinfo else closed_at.replace(tzinfo=timezone.utc)
                )
            if str(status) != "STOP_LOSS":
                break
            streak += 1
        return streak, last_closed

    def _build_signal_for_assessment(
        self,
        pair: str,
        frame_15m: Any,
        regime: str,
        relative: dict[str, float],
        assessment: dict[str, Any],
    ) -> tuple[GeneratedSignal | None, str, dict[str, Any]]:
        signal, reason, diagnostic = super()._build_signal_for_assessment(
            pair, frame_15m, regime, relative, assessment
        )
        if signal is None:
            return signal, reason, diagnostic
        breakdown = dict(signal.score_breakdown or {})
        breakdown.update(
            {
                "runtime_version": RUNTIME_VERSION,
                "capital_protection": True,
                "live_admission_required": True,
            }
        )
        reasons = list(signal.reasons or [])
        reasons.append("ammissione live subordinata a edge shadow positivo dopo i costi")
        return replace(signal, score_breakdown=breakdown, reasons=reasons), reason, diagnostic

    @staticmethod
    def calculate_shadow_statistics(rows: list[tuple[Any, ...]], recent_window: int = 10) -> dict[str, float | int]:
        completed = len(rows)
        wins = [row for row in rows if str(row[1]) == "TARGET_HIT"]
        losses = [row for row in rows if str(row[1]) == "STOP_LOSS"]
        gross_profit = sum(float(row[2] or 0.0) for row in wins)
        gross_loss = sum(float(row[3] or 0.0) for row in losses)
        profit_factor = (
            gross_profit / gross_loss
            if gross_loss > 0
            else 999.0
            if gross_profit > 0
            else 0.0
        )
        win_rate = len(wins) / completed if completed else 0.0
        expectancy_eur = (gross_profit - gross_loss) / completed if completed else 0.0
        expectancy_r = (
            sum(float(row[4] or 0.0) if str(row[1]) == "TARGET_HIT" else -1.0 for row in rows)
            / completed
            if completed
            else 0.0
        )
        average_win = gross_profit / len(wins) if wins else 0.0
        average_loss = gross_loss / len(losses) if losses else 0.0
        break_even_rate = (
            average_loss / (average_win + average_loss)
            if average_win > 0 and average_loss > 0
            else 1.0
            if not wins
            else 0.0
        )
        maximum_loss_streak = 0
        running = 0
        for row in rows:
            if str(row[1]) == "STOP_LOSS":
                running += 1
                maximum_loss_streak = max(maximum_loss_streak, running)
            else:
                running = 0

        recent = rows[-max(1, recent_window):]
        recent_wins = [row for row in recent if str(row[1]) == "TARGET_HIT"]
        recent_losses = [row for row in recent if str(row[1]) == "STOP_LOSS"]
        recent_profit = sum(float(row[2] or 0.0) for row in recent_wins)
        recent_loss = sum(float(row[3] or 0.0) for row in recent_losses)
        recent_pf = (
            recent_profit / recent_loss
            if recent_loss > 0
            else 999.0
            if recent_profit > 0
            else 0.0
        )
        recent_expectancy_r = (
            sum(float(row[4] or 0.0) if str(row[1]) == "TARGET_HIT" else -1.0 for row in recent)
            / len(recent)
            if recent
            else 0.0
        )
        return {
            "completed": completed,
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": win_rate,
            "profit_factor": profit_factor,
            "expectancy_eur": expectancy_eur,
            "expectancy_r": expectancy_r,
            "break_even_rate": break_even_rate,
            "max_loss_streak": maximum_loss_streak,
            "recent_completed": len(recent),
            "recent_profit_factor": recent_pf,
            "recent_expectancy_r": recent_expectancy_r,
        }

    def fetch_shadow_rows(
        self,
        strategy: str,
        regime: str,
        pair: str | None = None,
    ) -> list[tuple[Any, ...]]:
        pair_clause = " AND pair = %s" if pair else ""
        params: tuple[Any, ...] = (
            (RUNTIME_VERSION, strategy, regime, pair)
            if pair
            else (RUNTIME_VERSION, strategy, regime)
        )
        return self.client.fetch_all(
            f"""SELECT pair, status, net_profit_tp_eur, net_loss_sl_eur, net_rr, closed_at
            FROM signals.shadow_signals
            WHERE runtime_version = %s
              AND strategy = %s
              AND regime = %s
              {pair_clause}
              AND status IN ('TARGET_HIT', 'STOP_LOSS')
              AND COALESCE(outcome_ambiguous, FALSE) = FALSE
              AND closed_at IS NOT NULL
              AND closed_at >= NOW() - INTERVAL '120 days'
            ORDER BY closed_at ASC, id ASC""",
            params,
        )

    def live_circuit_breaker(self) -> dict[str, Any]:
        rows = self.client.fetch_all(
            """SELECT status, closed_at
            FROM signals.generated_signals
            WHERE score_breakdown->>'runtime_version' = %s
              AND telegram_sent_at IS NOT NULL
              AND COALESCE(outcome_ambiguous, FALSE) = FALSE
              AND status IN ('TARGET_HIT', 'STOP_LOSS')
              AND closed_at IS NOT NULL
              AND closed_at >= NOW() - INTERVAL '7 days'
            ORDER BY closed_at DESC, id DESC
            LIMIT 20""",
            (RUNTIME_VERSION,),
        )
        now = datetime.now(timezone.utc)
        latest_closed: datetime | None = None
        consecutive_losses = 0
        losses_24h = 0
        for index, (status, closed_at) in enumerate(rows):
            closed = closed_at
            if isinstance(closed, datetime) and closed.tzinfo is None:
                closed = closed.replace(tzinfo=timezone.utc)
            if index == 0 and isinstance(closed, datetime):
                latest_closed = closed
            if isinstance(closed, datetime) and now - closed <= timedelta(hours=24):
                if str(status) == "STOP_LOSS":
                    losses_24h += 1
            if index == consecutive_losses and str(status) == "STOP_LOSS":
                consecutive_losses += 1

        triggered = (
            consecutive_losses >= self.live_stop_trigger
            or losses_24h >= self.live_stop_24h_limit
        )
        active_until = (
            latest_closed + timedelta(hours=self.live_cooldown_hours)
            if triggered and latest_closed
            else None
        )
        active = bool(active_until and now < active_until)
        return {
            "active": active,
            "active_until": active_until,
            "consecutive_losses": consecutive_losses,
            "losses_24h": losses_24h,
        }

    def admission_decision(self, signal: GeneratedSignal) -> dict[str, Any]:
        key = (signal.strategy, signal.regime, signal.pair)
        if key in self._admission_cache:
            return self._admission_cache[key]

        breaker = self.live_circuit_breaker()
        strategy_rows = self.fetch_shadow_rows(signal.strategy, signal.regime)
        pair_rows = self.fetch_shadow_rows(signal.strategy, signal.regime, signal.pair)
        stats = self.calculate_shadow_statistics(
            strategy_rows, self.shadow_recent_window
        )
        pair_stats = self.calculate_shadow_statistics(
            pair_rows, self.shadow_recent_window
        )
        blockers: list[str] = []
        if breaker["active"]:
            blockers.append(
                f"circuit breaker live fino a {breaker['active_until']}"
            )
        if int(stats["completed"]) < self.shadow_min_samples:
            blockers.append(
                f"campione shadow {int(stats['completed'])}/{self.shadow_min_samples}"
            )
        if float(stats["profit_factor"]) < self.shadow_min_profit_factor:
            blockers.append(
                f"PF shadow {float(stats['profit_factor']):.2f} < {self.shadow_min_profit_factor:.2f}"
            )
        if float(stats["expectancy_r"]) < self.shadow_min_expectancy_r:
            blockers.append(
                f"expectancy shadow {float(stats['expectancy_r']):+.3f}R < {self.shadow_min_expectancy_r:+.3f}R"
            )
        required_win_rate = float(stats["break_even_rate"]) + self.shadow_min_win_margin
        if float(stats["win_rate"]) < required_win_rate:
            blockers.append(
                f"win rate {float(stats['win_rate']) * 100:.1f}% < soglia {required_win_rate * 100:.1f}%"
            )
        if int(stats["recent_completed"]) >= self.shadow_recent_window:
            if float(stats["recent_profit_factor"]) < 1.10:
                blockers.append(
                    f"PF ultime {self.shadow_recent_window} operazioni < 1.10"
                )
            if float(stats["recent_expectancy_r"]) <= 0.0:
                blockers.append(
                    f"expectancy ultime {self.shadow_recent_window} operazioni non positiva"
                )
        if int(stats["max_loss_streak"]) > self.shadow_max_loss_streak:
            blockers.append(
                f"sequenza massima shadow {int(stats['max_loss_streak'])} stop"
            )
        if int(pair_stats["completed"]) >= 8 and (
            float(pair_stats["profit_factor"]) < 0.85
            or float(pair_stats["expectancy_r"]) <= -0.10
        ):
            blockers.append(
                f"veto coppia {signal.pair}: risultati shadow negativi"
            )

        decision = {
            "admitted": not blockers,
            "blockers": blockers,
            "stats": stats,
            "pair_stats": pair_stats,
            "circuit_breaker": breaker,
        }
        self._admission_cache[key] = decision
        return decision

    def shadow_duplicate_exists(self, signal: GeneratedSignal) -> bool:
        rows = self.client.fetch_all(
            """SELECT id
            FROM signals.shadow_signals
            WHERE runtime_version = %s
              AND strategy = %s
              AND pair = %s
              AND regime = %s
              AND status IN ('NEW', 'OPEN')
              AND created_at >= NOW() - (%s * INTERVAL '1 minute')
            ORDER BY created_at DESC
            LIMIT 1""",
            (
                RUNTIME_VERSION,
                signal.strategy,
                signal.pair,
                signal.regime,
                self.signal_cooldown_minutes,
            ),
        )
        return bool(rows)

    def save_shadow_signal(
        self,
        signal: GeneratedSignal,
        decision: dict[str, Any],
    ) -> int | None:
        if self._shadow_saved_cycle >= self.max_shadow_per_cycle:
            return None
        if self.shadow_duplicate_exists(signal):
            return None
        breakdown = dict(signal.score_breakdown or {})
        breakdown.update(
            {
                "runtime_version": RUNTIME_VERSION,
                "execution_mode": "SHADOW",
                "admission_blockers": list(decision.get("blockers") or []),
            }
        )
        rows = self.client.fetch_all(
            """INSERT INTO signals.shadow_signals(
                runtime_version, strategy, pair, timeframe, regime, exchange,
                entry, stop_loss, take_profit, net_profit_tp_eur, net_loss_sl_eur,
                net_rr, score, score_breakdown, reasons, signal_time,
                reference_candle_time, status, created_at
            ) VALUES (
                %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s,
                %s, %s, %s::jsonb, %s::jsonb, %s,
                %s, 'NEW', %s
            ) RETURNING id""",
            (
                RUNTIME_VERSION,
                signal.strategy,
                signal.pair,
                signal.timeframe,
                signal.regime,
                self.exchange,
                signal.entry,
                signal.stop_loss,
                signal.take_profit,
                signal.net_profit_tp1_eur,
                signal.net_loss_sl_eur,
                signal.net_rr,
                signal.score,
                json.dumps(breakdown),
                json.dumps(signal.reasons),
                signal.signal_time,
                signal.reference_candle_time,
                signal.signal_time,
            ),
        )
        shadow_id = int(rows[0][0])
        self._shadow_saved_cycle += 1
        self.logger.info(
            "SHADOW_SIGNAL_SAVED id=%s strategy=%s pair=%s regime=%s score=%.1f net_rr=%.2f blockers=%s",
            shadow_id,
            signal.strategy,
            signal.pair,
            signal.regime,
            signal.score,
            signal.net_rr,
            json.dumps(decision.get("blockers") or [], ensure_ascii=False),
        )
        return shadow_id

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
        if self._loss_guard_active:
            self.max_signals_per_cycle = 1
            self.min_signal_score = max(self.min_signal_score, 78)
            self.min_net_rr = max(self.min_net_rr, 1.25)
            self.min_tp1_net_profit_eur = max(self.min_tp1_net_profit_eur, 0.30)

        try:
            candidates: list[GeneratedSignal] = []
            rejection_reasons: dict[str, int] = {}
            regime_counts: dict[str, int] = {}
            for pair in self.pairs:
                try:
                    candidate, reason, diagnostic = self.evaluate_pair_detailed(pair)
                except Exception:
                    self.logger.exception("CAPITAL_PROTECTION pair_failed pair=%s", pair)
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
                        "profit_factor": round(float(decision["stats"]["profit_factor"]), 3),
                        "expectancy_r": round(float(decision["stats"]["expectancy_r"]), 3),
                    }
                )
                if not decision["admitted"]:
                    if self.save_shadow_signal(candidate, decision) is not None:
                        shadowed += 1
                    rejection_reasons["SHADOW_EDGE_NOT_VALIDATED"] = (
                        rejection_reasons.get("SHADOW_EDGE_NOT_VALIDATED", 0) + 1
                    )
                    continue
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
                exposure_pairs = list(
                    dict.fromkeys(open_pairs + [item.pair for item in published])
                )
                correlated = self.correlated_with_pairs(candidate.pair, exposure_pairs)
                if len(correlated) >= self.max_correlated_per_cycle:
                    rejection_reasons["CORRELATED_EXPOSURE"] = (
                        rejection_reasons.get("CORRELATED_EXPOSURE", 0) + 1
                    )
                    continue

                breakdown = dict(candidate.score_breakdown or {})
                breakdown.update(
                    {
                        "runtime_version": RUNTIME_VERSION,
                        "execution_mode": "LIVE_ADMITTED",
                        "shadow_completed": int(decision["stats"]["completed"]),
                        "shadow_profit_factor": round(
                            float(decision["stats"]["profit_factor"]), 4
                        ),
                        "shadow_expectancy_r": round(
                            float(decision["stats"]["expectancy_r"]), 4
                        ),
                    }
                )
                candidate = replace(candidate, score_breakdown=breakdown)
                signal_id = self.save_signal(candidate)
                signal = replace(candidate, signal_id=signal_id)
                self.publish_signal_event(signal)
                published.append(signal)
                available_slots -= 1
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

            ranked = sorted(
                self._pair_diagnostics,
                key=lambda item: float(item.get("score") or 0.0),
                reverse=True,
            )
            best = ranked[0] if ranked else None
            breaker = self.live_circuit_breaker()
            self._last_cycle_summary = {
                "pairs_scanned": len(self.pairs),
                "qualified": len(candidates),
                "published": len(published),
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
                "strategy_states": self._strategy_states,
                "open_positions_before_cycle": len(open_pairs),
                "open_positions_after_cycle": len(open_pairs) + len(published),
                "max_open_positions": self.max_open_signals,
                "loss_streak": streak,
                "loss_guard_active": self._loss_guard_active,
                "runtime_version": RUNTIME_VERSION,
                "capital_protection": True,
                "admission_details": admission_details[:10],
                "live_circuit_breaker": breaker,
            }
            self.logger.info(
                "CAPITAL_PROTECTION_SUMMARY pairs=%s qualified=%s shadow=%s live=%s "
                "open=%s breaker=%s rejections=%s",
                len(self.pairs),
                len(candidates),
                shadowed,
                len(published),
                len(open_pairs) + len(published),
                bool(breaker["active"]),
                json.dumps(rejection_reasons, sort_keys=True),
            )
            return published[0] if published else None
        finally:
            (
                self.max_signals_per_cycle,
                self.min_signal_score,
                self.min_net_rr,
                self.min_tp1_net_profit_eur,
            ) = original_limits

    def shadow_overview(self) -> list[dict[str, Any]]:
        rows = self.client.fetch_all(
            """SELECT strategy, status, net_profit_tp_eur, net_loss_sl_eur, net_rr, closed_at
            FROM signals.shadow_signals
            WHERE runtime_version = %s
              AND status IN ('TARGET_HIT', 'STOP_LOSS')
              AND COALESCE(outcome_ambiguous, FALSE) = FALSE
              AND closed_at IS NOT NULL
            ORDER BY strategy, closed_at ASC, id ASC""",
            (RUNTIME_VERSION,),
        )
        grouped: dict[str, list[tuple[Any, ...]]] = {
            strategy: [] for strategy in LIVE_LONG_STRATEGIES
        }
        for strategy, status, profit, loss, net_rr, closed_at in rows:
            grouped.setdefault(str(strategy), []).append(
                ("ALL", status, profit, loss, net_rr, closed_at)
            )
        overview = []
        for strategy in LIVE_LONG_STRATEGIES:
            stats = self.calculate_shadow_statistics(
                grouped.get(strategy, []), self.shadow_recent_window
            )
            overview.append({"strategy": strategy, **stats})
        return overview
