"""Low-cost scheduler for the simple 20-pair scanner."""
from __future__ import annotations

from typing import Any

import schedule

from project.shared.memory import release_unused_memory
from project.synchronized_scheduler import SynchronizedPlatformScheduler


class SimpleMarketScheduler(SynchronizedPlatformScheduler):
    """Run scanner and position monitor only after fresh 15m data."""

    def configure(self) -> None:
        super().configure()
        cancelled = 0
        for job in list(schedule.jobs):
            if self.position_monitor is not None and self._job_is_bound_method(
                job, self.position_monitor, "monitor_open_signals"
            ):
                schedule.cancel_job(job)
                cancelled += 1
        self.logger.info(
            "SIMPLE_SCHEDULER position_monitor_after_collector=true cancelled_jobs=%s research=false decision_engine=false",
            cancelled,
        )

    def _run_data_collector_sync(self, timeframes: list[str]) -> None:
        try:
            self.logger.info("SIMPLE_COLLECTOR started timeframes=%s", timeframes)
            results = self.data_collector.sync_all_pairs(
                self.settings.collector_pairs,
                timeframes,
            )
            success = sum(
                1 for item in results if getattr(item, "status", None) == "SUCCESS"
            )
            self.logger.info(
                "SIMPLE_COLLECTOR completed timeframes=%s success=%s total=%s",
                timeframes,
                success,
                len(results),
            )
            if self.settings.operational_timeframe in timeframes and success > 0:
                self.logger.info(
                    "SIMPLE_OPERATIONAL_CYCLE timeframe=%s action=position_then_scan",
                    self.settings.operational_timeframe,
                )
                if self.position_monitor is not None:
                    self.position_monitor.monitor_open_signals()
                if self.strategy_engine is not None:
                    self.strategy_engine.evaluate()
        except Exception:
            self.logger.exception(
                "SIMPLE_OPERATIONAL_CYCLE failed timeframes=%s", timeframes
            )
        finally:
            rss_mb = release_unused_memory()
            if rss_mb is not None:
                self.logger.info("MEMORY_RELEASE phase=simple_cycle rss_mb=%.1f", rss_mb)
            self._collector_lock.release()
