"""Economic and lifecycle guards for probabilistic signals.

Technical and statistical evidence remains probabilistic. Operative A/B signals must
satisfy the configured economic minimums, while class C candidates are persisted as
WATCHLIST observations and never treated as open positions.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from project.shared.events import Event, EventType
from project.strategy_engine.probabilistic_service import ProbabilisticStrategyEngine as _ProbabilisticStrategyEngine
from project.strategy_engine.service import GeneratedSignal


class EconomicallyGuardedProbabilisticStrategyEngine(_ProbabilisticStrategyEngine):
    """Keep probabilistic scoring while separating watchlists from trades."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.logger.info(
            "Watchlist isolation enabled operative_classes=%s watchlist_classes=%s",
            self.operative_signal_classes,
            self.watchlist_signal_classes,
        )

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

    def find_active_duplicate(self, signal: GeneratedSignal) -> int | None:
        """Keep watchlist cooldown separate from active operative positions."""
        common_params = (
            signal.strategy,
            signal.pair,
            signal.timeframe,
            signal.regime,
        )
        if signal.signal_class in self.watchlist_signal_classes:
            rows = self.research_repository.client.fetch_all(
                """SELECT id
                FROM signals.generated_signals
                WHERE strategy = %s AND pair = %s AND timeframe = %s AND regime = %s
                  AND signal_class = %s
                  AND status = 'WATCHLIST'
                  AND created_at >= NOW() - (%s * INTERVAL '1 minute')
                ORDER BY created_at DESC
                LIMIT 1""",
                (*common_params, signal.signal_class, self.signal_cooldown_minutes),
            )
        else:
            operative_classes = tuple(self.operative_signal_classes) or ("A", "B")
            placeholders = ", ".join(["%s"] * len(operative_classes))
            rows = self.research_repository.client.fetch_all(
                f"""SELECT id
                FROM signals.generated_signals
                WHERE strategy = %s AND pair = %s AND timeframe = %s AND regime = %s
                  AND signal_class IN ({placeholders})
                  AND status IN ('NEW', 'OPEN')
                  AND created_at >= NOW() - (%s * INTERVAL '1 minute')
                ORDER BY created_at DESC
                LIMIT 1""",
                (*common_params, *operative_classes, self.signal_cooldown_minutes),
            )
        return int(rows[0][0]) if rows else None

    def save_signal(self, signal: GeneratedSignal) -> int:
        """Persist C as WATCHLIST so the position monitor cannot close it as a trade."""
        signal_id = super().save_signal(signal)
        if signal.signal_class in self.watchlist_signal_classes:
            self.research_repository.client.execute(
                """UPDATE signals.generated_signals
                SET status = 'WATCHLIST'
                WHERE id = %s""",
                (signal_id,),
            )
            self.logger.info(
                "WATCHLIST_STATUS_SET id=%s class=%s pair=%s strategy=%s",
                signal_id,
                signal.signal_class,
                signal.pair,
                signal.strategy,
            )
        return signal_id

    def publish_daily_signal_report(self) -> bool:
        """Count only operative A/B signals in the no-signal report."""
        if not self.enable_daily_signal_report:
            return False
        now = datetime.now(timezone.utc)
        if self._last_no_trade_report_at is not None:
            elapsed_hours = (now - self._last_no_trade_report_at).total_seconds() / 3600.0
            if elapsed_hours < self.daily_signal_report_hours:
                return False

        operative_classes = tuple(self.operative_signal_classes) or ("A", "B")
        placeholders = ", ".join(["%s"] * len(operative_classes))
        recent = self.research_repository.client.fetch_all(
            f"""SELECT COUNT(*)
            FROM signals.generated_signals
            WHERE created_at >= NOW() - (%s * INTERVAL '1 hour')
              AND signal_class IN ({placeholders})""",
            (self.daily_signal_report_hours, *operative_classes),
        )
        recent_count = int(recent[0][0] or 0) if recent else 0
        if recent_count > 0:
            return False

        cycle_summary = self._last_cycle_summary or {}
        top_rejections = cycle_summary.get("top_rejections") or {}
        diagnostics = cycle_summary.get("diagnostics") or {}
        message = (
            "📊 Daily signal report\n"
            f"Nessun segnale operativo nelle ultime {self.daily_signal_report_hours}h.\n"
            f"Ultimo ciclo Strategy Engine: candidati={cycle_summary.get('candidates', 0)} "
            f"A={cycle_summary.get('class_a', 0)} B={cycle_summary.get('class_b', 0)} "
            f"C={cycle_summary.get('class_c', 0)} rejected={cycle_summary.get('rejected', 0)}.\n"
            f"Miglior candidato: {cycle_summary.get('best_candidate', 'NONE')}.\n"
            f"Top motivi di rifiuto: {json.dumps(top_rejections, sort_keys=True) if top_rejections else '{}'}.\n"
            f"Diagnostica ricerca: {json.dumps(diagnostics, sort_keys=True) if diagnostics else '{}'}.\n"
            f"Classi abilitate: operative={','.join(self.operative_signal_classes)} "
            f"watchlist={','.join(self.watchlist_signal_classes)}.\n"
            "Questo non è un segnale di ingresso: evita operazioni forzate e valuta il sistema su molte operazioni."
        )
        self.event_bus.publish(Event(EventType.REPORT_READY, {"message": message}))
        self._last_no_trade_report_at = now
        self.logger.info(
            "STRATEGY daily no-signal report published hours=%s operative_only=true",
            self.daily_signal_report_hours,
        )
        return True
