"""Dedicated 15m scheduler for the clean Velez PAPER engine."""
from __future__ import annotations

from datetime import datetime, timezone

import schedule

from project.operational_scheduler import OperationalMarketScheduler
from project.shared.memory import release_unused_memory


class Velez15mScheduler(OperationalMarketScheduler):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._last_key: str | None = None

    def configure(self) -> None:
        super().configure()
        for job in list(schedule.jobs):
            if self._job_is_bound_method(job, self, "start_data_collector_sync"):
                schedule.cancel_job(job)
        schedule.every(30).seconds.do(self._sync_if_due)
        self.logger.info("VELEZ_SCHEDULER poll=30s scan_minutes=01,16,31,46 timeframe=15m")

    def _run_startup_cycle(self) -> None:
        if self.data_collector is None:
            return
        try:
            results = self.data_collector.sync_all_pairs(self.settings.collector_pairs, ["15m"])
            ok15 = any(getattr(item, "status", None) == "SUCCESS" for item in results)
            if ok15 and self.position_monitor is not None:
                self.position_monitor.monitor_open_signals()
            if ok15 and self.strategy_engine is not None:
                self.strategy_engine.evaluate()
        except Exception:
            self.logger.exception("VELEZ_STARTUP failed")
        finally:
            release_unused_memory()

    def _run_data_collector_sync(self, timeframes: list[str]) -> None:
        try:
            results = self.data_collector.sync_all_pairs(self.settings.collector_pairs, ["15m"])
            ok15 = any(getattr(item, "status", None) == "SUCCESS" for item in results)
            if ok15 and self.position_monitor is not None:
                self.position_monitor.monitor_open_signals()
            if ok15 and self.strategy_engine is not None:
                self.strategy_engine.evaluate()
        except Exception:
            self.logger.exception("VELEZ_CYCLE failed")
        finally:
            release_unused_memory()
            self._collector_lock.release()

    def _sync_if_due(self, now: datetime | None = None) -> bool:
        current = now or datetime.now(timezone.utc)
        if current.minute not in {1, 16, 31, 46}:
            return False
        key = current.strftime("%Y-%m-%dT%H:%M")
        if self._last_key == key:
            return False
        if not self.start_data_collector_sync(["15m"]):
            return False
        self._last_key = key
        return True
