"""Telegram report for the V6 capital-protection and shadow-validation runtime."""
from __future__ import annotations

from datetime import datetime, timezone

from project.capital_protection_scanner import (
    RUNTIME_VERSION,
    CapitalProtectionScanner,
)
from project.shared.events import Event, EventType


class CapitalProtectionReportScanner(CapitalProtectionScanner):
    def publish_daily_signal_report(self) -> bool:
        if not self.enable_daily_signal_report:
            return False
        now = datetime.now(timezone.utc)
        if self._last_no_trade_report_at is not None:
            elapsed = (now - self._last_no_trade_report_at).total_seconds() / 3600.0
            if elapsed < self.daily_signal_report_hours:
                return False

        summary = self._last_cycle_summary or {}
        overview = self.shadow_overview()
        shadow_lines: list[str] = []
        for item in overview:
            completed = int(item.get("completed", 0) or 0)
            pf = float(item.get("profit_factor", 0.0) or 0.0)
            expectancy_r = float(item.get("expectancy_r", 0.0) or 0.0)
            state = "IN VALIDAZIONE"
            if (
                completed >= self.shadow_min_samples
                and pf >= self.shadow_min_profit_factor
                and expectancy_r >= self.shadow_min_expectancy_r
            ):
                state = "SOGLIE BASE SUPERATE"
            shadow_lines.append(
                f"• {item['strategy']}: <b>{completed}</b> chiuse · "
                f"PF <b>{pf:.2f}</b> · Exp <b>{expectancy_r:+.3f}R</b> · {state}"
            )

        admission_lines: list[str] = []
        for item in (summary.get("admission_details") or [])[:5]:
            if item.get("admitted"):
                text = "AMMESSO LIVE"
            else:
                blockers = item.get("blockers") or []
                text = str(blockers[0]) if blockers else "edge non ancora validato"
            admission_lines.append(
                f"• {item.get('pair')} · {item.get('strategy')} · "
                f"n={item.get('completed', 0)} · PF {float(item.get('profit_factor') or 0.0):.2f} · {text}"
            )
        if not admission_lines:
            admission_lines = ["• Nessun setup tecnicamente qualificato nell'ultimo ciclo"]

        breaker = summary.get("live_circuit_breaker") or self.live_circuit_breaker()
        breaker_label = "ATTIVO" if breaker.get("active") else "non attivo"
        regimes = summary.get("regimes") or {}
        rejections = summary.get("rejections") or {}
        rejection_text = ", ".join(
            f"{name}: {count}" for name, count in sorted(rejections.items())
        ) or "nessuna"
        message = (
            "🛡 <b>REPORT PROTEZIONE CAPITALE V6</b>\n\n"
            "💶 <b>Stato operativo</b>\n"
            "• Modalità: <b>SHADOW FIRST</b>\n"
            f"• Setup qualificati ultimo ciclo: <b>{summary.get('qualified', 0)}</b>\n"
            f"• Operazioni shadow salvate: <b>{summary.get('shadow_saved', 0)}</b>\n"
            f"• Segnali live inviati: <b>{summary.get('published', 0)}</b>\n"
            f"• Circuit breaker live: <b>{breaker_label}</b>\n"
            f"• Stop live consecutivi V6: <b>{breaker.get('consecutive_losses', 0)}</b>\n"
            f"• Stop live ultime 24h: <b>{breaker.get('losses_24h', 0)}</b>\n\n"
            "✅ <b>Requisiti per ammissione live</b>\n"
            f"• almeno <b>{self.shadow_min_samples}</b> operazioni shadow non ambigue\n"
            f"• Profit Factor almeno <b>{self.shadow_min_profit_factor:.2f}</b>\n"
            f"• Expectancy almeno <b>{self.shadow_min_expectancy_r:+.2f}R</b>\n"
            f"• win rate sopra il pareggio di almeno <b>{self.shadow_min_win_margin * 100:.0f} punti</b>\n"
            f"• ultime {self.shadow_recent_window} operazioni ancora positive\n"
            f"• sequenza massima non oltre <b>{self.shadow_max_loss_streak}</b> stop\n\n"
            "📊 <b>Validazione shadow per strategia</b>\n"
            + "\n".join(shadow_lines)
            + "\n\n📍 <b>Decisioni ultimo ciclo</b>\n"
            + "\n".join(admission_lines)
            + "\n\n🌐 <b>Mercato</b>\n"
            f"• rialzo {regimes.get('TREND_UP', 0)}, ribasso {regimes.get('TREND_DOWN', 0)}, "
            f"range {regimes.get('RANGE', 0)}, neutro {regimes.get('NEUTRAL', 0)}\n"
            f"• Esclusioni: <code>{rejection_text}</code>\n\n"
            "Finché l'edge non è dimostrato, i setup vengono monitorati internamente e non sono segnali operativi."
        )
        self.event_bus.publish(
            Event(EventType.REPORT_READY, {"message": message, "trusted_html": True})
        )
        self._last_no_trade_report_at = now
        self.logger.info(
            "CAPITAL_PROTECTION_REPORT_SENT runtime=%s shadow=%s live=%s breaker=%s",
            RUNTIME_VERSION,
            summary.get("shadow_saved", 0),
            summary.get("published", 0),
            bool(breaker.get("active")),
        )
        return True
