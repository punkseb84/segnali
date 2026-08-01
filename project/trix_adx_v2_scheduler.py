"""Quarter-hour scheduler for 15m TRIX triggers with 1h context."""
from __future__ import annotations

from datetime import datetime, timezone

import schedule

from project.operational_scheduler import OperationalMarketScheduler
from project.shared.memory import release_unused_memory


class QuarterHourTrixScheduler(OperationalMarketScheduler):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._last_close_key: str | None = None

    def configure(self) -> None:
        super().configure()
        for job in list(schedule.jobs):
            if self._job_is_bound_method(job, self, "start_data_collector_sync"):
                schedule.cancel_job(job)
        schedule.every(20).seconds.do(self._sync_after_close_if_due)
        self.logger.info("TRIX_V2_CLOSE_SYNC poll=20s eligible_minutes=01,16,31,46 timeframes=15m,1h")

    def _run_startup_cycle(self) -> None:
        self._run_cycle(["15m", "1h"], startup=True)

    def _run_cycle(self, timeframes: list[str], startup: bool = False) -> None:
        if self.data_collector is None:
            return
        try:
            results = self.data_collector.sync_all_pairs(self.settings.collector_pairs, timeframes)
            successful = [item for item in results if getattr(item, "status", None) == "SUCCESS"]
            ok15 = any(getattr(item, "timeframe", "") == "15m" for item in successful)
            ok1h = any(getattr(item, "timeframe", "") == "1h" for item in successful)
            self.logger.info("TRIX_V2_SYNC startup=%s success=%s total=%s 15m=%s 1h=%s", startup, len(successful), len(results), ok15, ok1h)
            if ok15 and self.position_monitor is not None:
                self.position_monitor.monitor_open_signals()
            if ok15 and ok1h and self.strategy_engine is not None:
                self.strategy_engine.evaluate()
                if startup:
                    self.strategy_engine.publish_daily_signal_report()
        except Exception:
            self.logger.exception("TRIX_V2_SYNC failed")
        finally:
            rss = release_unused_memory()
            if rss is not None:
                self.logger.info("MEMORY_RELEASE phase=trix_v2 rss_mb=%.1f", rss)

    def _run_data_collector_sync(self, timeframes: list[str]) -> None:
        try:
            self._run_cycle(timeframes)
        finally:
            self._collector_lock.release()

    def _sync_after_close_if_due(self, now: datetime | None = None) -> bool:
        current = now or datetime.now(timezone.utc)
        eligible = {1, 16, 31, 46}
        if current.minute not in eligible:
            return False
        close_key = current.strftime("%Y-%m-%dT%H:%M")
        if self._last_close_key == close_key:
            return False
        started = self.start_data_collector_sync(["15m", "1h"])
        if not started:
            return False
        self._last_close_key = close_key
        return True
