"""Daily-limited wrapper for the single Ichimoku strategy."""
from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

from project.ichimoku_scanner import IchimokuCloudScanner, RUNTIME_VERSION
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
