"""Daily-limited wrapper for the single Ichimoku strategy."""
from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

from project.ichimoku_scanner import IchimokuCloudScanner, RUNTIME_VERSION
from project.shared.events import Event, EventType
from project.strategy_engine.service import GeneratedSignal


class DailyLimitedIchimokuScanner(IchimokuCloudScanner):
    """Publish no more than a configured number of Telegram signals per Rome day."""

    def __init__(self, *args: Any, max_signals_per_day: int = 10, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.max_signals_per_day = max(1, int(max_signals_per_day))

    def _signals_sent_today(self) -> int:
        rows = self.client.fetch_all(
            """SELECT COUNT(*)
            FROM signals.generated_signals
            WHERE score_breakdown->>'runtime_version' = %s
              AND telegram_sent_at IS NOT NULL
              AND (created_at AT TIME ZONE 'Europe/Rome')::date =
                  (NOW() AT TIME ZONE 'Europe/Rome')::date""",
            (RUNTIME_VERSION,),
        )
        return int(rows[0][0] or 0) if rows else 0

    def evaluate(self) -> GeneratedSignal | None:
        sent_before = self._signals_sent_today()
        remaining = max(0, self.max_signals_per_day - sent_before)
        if remaining <= 0:
            self._last_cycle_summary = {
                "pairs_scanned": len(self.pairs),
                "qualified": 0,
                "published": 0,
                "signals_today": sent_before,
                "daily_limit": self.max_signals_per_day,
                "best_candidate": "DAILY_LIMIT_REACHED",
                "rejections": {"ICHIMOKU_DAILY_LIMIT": len(self.pairs)},
            }
            self.logger.info(
                "ICHIMOKU_DAILY_LIMIT reached today=%s/%s",
                sent_before,
                self.max_signals_per_day,
            )
            return None

        candidates: list[GeneratedSignal] = []
        rejection_reasons: dict[str, int] = {}
        for pair in self.pairs:
            try:
                candidate, reason = self.evaluate_pair(pair)
            except Exception:
                self.logger.exception("ICHIMOKU pair_failed pair=%s", pair)
                candidate, reason = None, "PAIR_ERROR"
            if candidate is not None:
                candidates.append(candidate)
            else:
                rejection_reasons[reason] = rejection_reasons.get(reason, 0) + 1

        candidates.sort(key=lambda item: item.score, reverse=True)
        cycle_limit = min(self.max_signals_per_cycle, remaining)
        published: list[GeneratedSignal] = []
        for candidate in candidates:
            if len(published) >= cycle_limit:
                break
            duplicate_id = self.find_active_duplicate(candidate)
            if duplicate_id is not None:
                rejection_reasons["ACTIVE_PAIR_COOLDOWN"] = (
                    rejection_reasons.get("ACTIVE_PAIR_COOLDOWN", 0) + 1
                )
                continue
            signal_id = self.save_signal(candidate)
            signal = replace(candidate, signal_id=signal_id)
            self.publish_signal_event(signal)
            published.append(signal)
            self.logger.info(
                "ICHIMOKU_SIGNAL_SENT id=%s pair=%s score=%.1f entry=%.8f stop=%.8f",
                signal_id,
                signal.pair,
                signal.score,
                signal.entry,
                signal.stop_loss,
            )

        best = candidates[0] if candidates else None
        total_today = sent_before + len(published)
        self._last_cycle_summary = {
            "pairs_scanned": len(self.pairs),
            "qualified": len(candidates),
            "published": len(published),
            "signals_today": total_today,
            "daily_limit": self.max_signals_per_day,
            "best_candidate": (
                f"{best.pair} {best.strategy} score={best.score:.1f}" if best else "NONE"
            ),
            "rejections": rejection_reasons,
        }
        self.logger.info(
            "ICHIMOKU_SUMMARY pairs=%s qualified=%s published=%s today=%s/%s best=%s rejections=%s",
            len(self.pairs),
            len(candidates),
            len(published),
            total_today,
            self.max_signals_per_day,
            self._last_cycle_summary["best_candidate"],
            json.dumps(rejection_reasons, sort_keys=True),
        )
        return published[0] if published else None

    def publish_daily_signal_report(self) -> bool:
        if not self.enable_daily_signal_report:
            return False
        now = datetime.now(timezone.utc)
        if self._last_no_trade_report_at is not None:
            elapsed = (now - self._last_no_trade_report_at).total_seconds() / 3600.0
            if elapsed < self.daily_signal_report_hours:
                return False

        recent = self.client.fetch_all(
            """SELECT COUNT(*)
            FROM signals.generated_signals
            WHERE score_breakdown->>'runtime_version' = %s
              AND telegram_sent_at IS NOT NULL
              AND created_at >= NOW() - (%s * INTERVAL '1 hour')""",
            (RUNTIME_VERSION, self.daily_signal_report_hours),
        )
        recent_count = int(recent[0][0] or 0) if recent else 0
        summary = self._last_cycle_summary or {}
        message = (
            "☁️ <b>REPORT SCANNER CRYPTO</b>\n\n"
            f"• Coppie monitorate: <b>{summary.get('pairs_scanned', len(self.pairs))}</b>\n"
            f"• Setup qualificati ultimo ciclo: <b>{summary.get('qualified', 0)}</b>\n"
            f"• Segnali inviati ultimo ciclo: <b>{summary.get('published', 0)}</b>\n"
            f"• Segnali ultime {self.daily_signal_report_hours}h: <b>{recent_count}</b>\n"
            f"• Miglior setup: <b>{summary.get('best_candidate', 'NONE')}</b>\n"
            f"• Motivi senza segnale: <code>{json.dumps(summary.get('rejections', {}), sort_keys=True)}</code>\n\n"
            "Strategia attiva: Ichimoku Cloud Breakout."
        )
        self.event_bus.publish(
            Event(EventType.REPORT_READY, {"message": message, "trusted_html": True})
        )
        self._last_no_trade_report_at = now
        return True
