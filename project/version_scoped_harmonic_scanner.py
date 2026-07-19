"""Version-scoped reporting and loss guard for the live seven-strategy scanner."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from project.harmonic_live_scanner import LIVE_LONG_STRATEGIES
from project.reliable_harmonic_scanner import RUNTIME_VERSION
from project.report_safe_harmonic_scanner import ReportSafeHarmonicLiveStrategyScanner
from project.shared.events import Event, EventType


class VersionScopedHarmonicLiveStrategyScanner(ReportSafeHarmonicLiveStrategyScanner):
    """Prevent previous runtime outcomes from activating the current loss guard."""

    def fetch_loss_streak(self) -> tuple[int, datetime | None]:
        placeholders = ", ".join(["%s"] * len(LIVE_LONG_STRATEGIES))
        rows = self.client.fetch_all(
            f"""SELECT status, closed_at FROM signals.generated_signals
            WHERE strategy IN ({placeholders})
              AND score_breakdown->>'runtime_version' = %s
              AND telegram_sent_at IS NOT NULL
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
                    closed_at
                    if closed_at.tzinfo
                    else closed_at.replace(tzinfo=timezone.utc)
                )
            if str(status) != "STOP_LOSS":
                break
            streak += 1
        return streak, last_closed

    def publish_daily_signal_report(self) -> bool:
        if not self.enable_daily_signal_report:
            return False

        now = datetime.now(timezone.utc)
        if self._last_no_trade_report_at is not None:
            elapsed = (now - self._last_no_trade_report_at).total_seconds() / 3600.0
            if elapsed < self.daily_signal_report_hours:
                return False

        placeholders = ", ".join(["%s"] * len(LIVE_LONG_STRATEGIES))
        recent = self.client.fetch_all(
            f"""SELECT COUNT(*) FROM signals.generated_signals
            WHERE created_at >= NOW() - (%s * INTERVAL '1 hour')
              AND strategy IN ({placeholders})
              AND score_breakdown->>'runtime_version' = %s
              AND telegram_sent_at IS NOT NULL""",
            (
                self.daily_signal_report_hours,
                *LIVE_LONG_STRATEGIES,
                RUNTIME_VERSION,
            ),
        )
        recent_count = int(recent[0][0] or 0) if recent else 0

        summary = self._last_cycle_summary or {}
        near = summary.get("near_setups") or []
        near_lines: list[str] = []
        for item in near[:5]:
            blockers = item.get("blockers") or []
            missing = str(blockers[0]) if blockers else "tutti i requisiti superati"
            near_lines.append(
                f"• <b>{item.get('pair')}</b> · {item.get('strategy')} · "
                f"score <b>{float(item.get('score') or 0.0):.1f}</b> · manca: {missing}"
            )
        if not near_lines:
            near_lines = ["• Nessun dato diagnostico disponibile"]

        states = summary.get("strategy_states") or self._strategy_states
        state_lines: list[str] = []
        for strategy in LIVE_LONG_STRATEGIES:
            info: dict[str, Any] = states.get(strategy, {"state": "ACTIVE"})
            state = str(info.get("state") or "ACTIVE")
            extra = (
                f" · riattivazione {info.get('next_review')}"
                if state == "PAUSED" and info.get("next_review")
                else " · segnale-test massimo ogni 24h"
                if state == "PROBATION"
                else ""
            )
            state_lines.append(f"• {strategy}: <b>{state}</b>{extra}")

        performance = summary.get("performance") or self.aggregate_forward_performance(
            self.fetch_forward_performance()
        )
        completed = int(performance.get("completed", 0) or 0)
        if completed:
            performance_text = (
                f"• Operazioni chiuse: <b>{completed}</b> · "
                f"win rate <b>{float(performance.get('win_rate') or 0.0) * 100:.1f}%</b>\n"
                f"• Profit Factor: <b>{float(performance.get('profit_factor') or 0.0):.2f}</b> · "
                f"risultato netto <b>€{float(performance.get('net_result_eur') or 0.0):+.2f}</b>\n"
                f"• Expectancy: <b>€{float(performance.get('expectancy_eur') or 0.0):+.3f}</b> per segnale"
            )
        else:
            performance_text = (
                "• Nessuna operazione della V5.1.2 ancora chiusa.\n"
                "• Profittevolezza: <b>non ancora misurabile</b>."
            )

        regimes = summary.get("regimes") or {}
        rejections = summary.get("rejections") or {}
        rejection_text = ", ".join(
            f"{name}: {count}" for name, count in sorted(rejections.items())
        ) or "nessuno"
        message = (
            "📊 <b>REPORT SCANNER LONG V5.1.2</b>\n\n"
            "🔎 <b>Ultimo ciclo</b>\n"
            f"• Coppie analizzate: <b>{summary.get('pairs_scanned', len(self.pairs))}</b>\n"
            f"• Setup qualificati: <b>{summary.get('qualified', 0)}</b>\n"
            f"• Segnali inviati: <b>{summary.get('published', 0)}</b>\n"
            f"• Posizioni aperte: <b>{summary.get('open_after', 0)}</b> / <b>{summary.get('max_open', 5)}</b>\n"
            f"• Segnali V5.1.2 consegnati ultime {self.daily_signal_report_hours}h: <b>{recent_count}</b>\n"
            f"• Stop consecutivi V5.1.2: <b>{int(summary.get('loss_streak', 0) or 0)}</b>\n"
            f"• Modalità prudente: <b>{'ATTIVA' if summary.get('loss_guard_active') else 'non attiva'}</b>\n"
            f"• Regimi: rialzo <b>{regimes.get('TREND_UP', 0)}</b>, "
            f"ribasso <b>{regimes.get('TREND_DOWN', 0)}</b>, "
            f"range <b>{regimes.get('RANGE', 0)}</b>, "
            f"neutro <b>{regimes.get('NEUTRAL', 0)}</b>\n"
            f"• Esclusioni: <code>{rejection_text}</code>\n\n"
            "📍 <b>Setup più vicini</b>\n"
            + "\n".join(near_lines)
            + "\n\n🛡 <b>Stato strategie</b>\n"
            + "\n".join(state_lines)
            + "\n\n📈 <b>Risultati forward V5.1.2</b>\n"
            + performance_text
            + "\n\nLimiti: massimo 3 segnali per ciclo, 5 posizioni aperte e 2 esposizioni fortemente correlate."
        )

        self.event_bus.publish(
            Event(EventType.REPORT_READY, {"message": message, "trusted_html": True})
        )
        self._last_no_trade_report_at = now
        self.logger.info(
            "REPORT_V5_1_2_SENT strategies=%s recent_delivered=%s loss_streak=%s guard=%s",
            len(LIVE_LONG_STRATEGIES),
            recent_count,
            int(summary.get("loss_streak", 0) or 0),
            bool(summary.get("loss_guard_active")),
        )
        return True
