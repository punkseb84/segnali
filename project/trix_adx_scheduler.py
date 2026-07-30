"""Closed-candle scheduler for the 1h TRIX trigger and 4h context."""
from __future__ import annotations

from datetime import datetime, timezone

import schedule

from project.operational_scheduler import OperationalMarketScheduler
from project.shared.memory import release_unused_memory


class HourAlignedTrixAdxScheduler(OperationalMarketScheduler):
    """Synchronize 1h and 4h data immediately after every UTC hour closes."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._last_hour_close_sync_key: str | None = None

    def configure(self) -> None:
        super().configure()

        cancelled = 0
        for job in list(schedule.jobs):
            if self._job_is_bound_method(job, self, "start_data_collector_sync"):
                schedule.cancel_job(job)
                cancelled += 1

        schedule.every(30).seconds.do(self._sync_after_hour_close_if_due)
        self.logger.info(
            "TRIX_ADX_HOUR_CLOSE_SYNC scheduled poll=30s eligible_utc_minutes=1-4 "
            "timeframes=1h,4h cancelled_collector_jobs=%s",
            cancelled,
        )

    def _run_startup_cycle(self) -> None:
        if self.data_collector is None:
            return
        selected = ["1h", "4h"]
        self.logger.info(
            "TRIX_ADX_STARTUP_SYNC started timeframes=%s action=sync_then_position_then_scan",
            selected,
        )
        try:
            results = self.data_collector.sync_all_pairs(
                self.settings.collector_pairs,
                selected,
            )
            successful = [
                item for item in results if getattr(item, "status", None) == "SUCCESS"
            ]
            one_hour_success = any(
                getattr(item, "timeframe", "") == "1h"
                for item in successful
            )
            four_hour_success = any(
                getattr(item, "timeframe", "") == "4h"
                for item in successful
            )
            self.logger.info(
                "TRIX_ADX_STARTUP_SYNC completed success=%s total=%s 1h=%s 4h=%s",
                len(successful),
                len(results),
                one_hour_success,
                four_hour_success,
            )
            if one_hour_success and four_hour_success:
                if self.position_monitor is not None:
                    self.position_monitor.monitor_open_signals()
                if self.strategy_engine is not None:
                    self.strategy_engine.evaluate()
                    self.strategy_engine.publish_daily_signal_report()
        except Exception:
            self.logger.exception("TRIX_ADX_STARTUP_SYNC failed")
        finally:
            rss_mb = release_unused_memory()
            if rss_mb is not None:
                self.logger.info(
                    "MEMORY_RELEASE phase=trix_adx_startup rss_mb=%.1f",
                    rss_mb,
                )

    def _run_data_collector_sync(self, timeframes: list[str]) -> None:
        try:
            selected = list(dict.fromkeys(timeframes))
            self.logger.info(
                "TRIX_ADX_COLLECTOR started timeframes=%s",
                selected,
            )
            results = self.data_collector.sync_all_pairs(
                self.settings.collector_pairs,
                selected,
            )
            successful = [
                item for item in results
                if getattr(item, "status", None) == "SUCCESS"
            ]
            one_hour_success = any(
                getattr(item, "timeframe", "") == "1h"
                for item in successful
            )
            four_hour_success = any(
                getattr(item, "timeframe", "") == "4h"
                for item in successful
            )
            self.logger.info(
                "TRIX_ADX_COLLECTOR completed success=%s total=%s 1h=%s 4h=%s",
                len(successful),
                len(results),
                one_hour_success,
                four_hour_success,
            )
            if one_hour_success and four_hour_success:
                if self.position_monitor is not None:
                    self.position_monitor.monitor_open_signals()
                if self.strategy_engine is not None:
                    self.strategy_engine.evaluate()
        except Exception:
            self.logger.exception(
                "TRIX_ADX_OPERATIONAL_CYCLE failed timeframes=%s",
                timeframes,
            )
        finally:
            rss_mb = release_unused_memory()
            if rss_mb is not None:
                self.logger.info(
                    "MEMORY_RELEASE phase=trix_adx_cycle rss_mb=%.1f",
                    rss_mb,
                )
            self._collector_lock.release()

    def _sync_after_hour_close_if_due(
        self,
        now: datetime | None = None,
    ) -> bool:
        current = now or datetime.now(timezone.utc)
        if current.minute not in {1, 2, 3, 4}:
            return False

        hour_key = current.strftime("%Y-%m-%dT%H")
        if self._last_hour_close_sync_key == hour_key:
            return False

        started = self.start_data_collector_sync(["1h", "4h"])
        if not started:
            self.logger.info(
                "TRIX_ADX_HOUR_CLOSE_SYNC deferred hour=%s reason=collector_busy",
                hour_key,
            )
            return False

        self._last_hour_close_sync_key = hour_key
        self.logger.info(
            "TRIX_ADX_HOUR_CLOSE_SYNC started hour=%s timeframes=1h,4h",
            hour_key,
        )
        return True
