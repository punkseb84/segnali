"""Scheduler that scans only after synchronized 15m and 1h market updates."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

import schedule

from project.shared.memory import release_unused_memory
from project.simple_scheduler import SimpleMarketScheduler


class OperationalMarketScheduler(SimpleMarketScheduler):
    """Keep the low-cost 15m cadence while refreshing 1h context once per hour."""

    @staticmethod
    def _hourly_context_due(now: datetime | None = None) -> bool:
        current = now or datetime.now(timezone.utc)
        return current.minute < 15

    def configure(self) -> None:
        # Prevent the base scheduler from running a strategy scan before the startup 1h
        # context has completed. Only the 15m collector is scheduled independently; this
        # class appends 1h to the first operational cycle after each UTC hour boundary.
        original = self.settings
        self.settings = replace(
            original,
            collector_timeframes=[original.operational_timeframe],
            run_data_collector_on_startup=False,
            enable_strategy_engine=False,
            enable_position_monitor=False,
        )
        super().configure()
        self.settings = original

        if self.strategy_engine is not None:
            schedule.every(1).hours.do(self.strategy_engine.publish_daily_signal_report)
            self.logger.info("OPERATIONAL_REPORT scheduled every 1h")

        self._run_startup_cycle()

    def _run_startup_cycle(self) -> None:
        if self.data_collector is None:
            return
        selected = list(
            dict.fromkeys([self.settings.operational_timeframe, "1h"])
        )
        self.logger.info(
            "OPERATIONAL_STARTUP_SYNC started timeframes=%s action=sync_then_position_then_scan",
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
            operational_success = any(
                getattr(item, "timeframe", self.settings.operational_timeframe)
                == self.settings.operational_timeframe
                for item in successful
            )
            self.logger.info(
                "OPERATIONAL_STARTUP_SYNC completed success=%s total=%s operational_success=%s",
                len(successful),
                len(results),
                operational_success,
            )
            if operational_success:
                if self.position_monitor is not None:
                    self.position_monitor.monitor_open_signals()
                if self.strategy_engine is not None:
                    self.strategy_engine.evaluate()
                    self.strategy_engine.publish_daily_signal_report()
        except Exception:
            self.logger.exception("OPERATIONAL_STARTUP_SYNC failed")
        finally:
            rss_mb = release_unused_memory()
            if rss_mb is not None:
                self.logger.info(
                    "MEMORY_RELEASE phase=operational_startup rss_mb=%.1f", rss_mb
                )

    def _run_data_collector_sync(self, timeframes: list[str]) -> None:
        try:
            selected = list(dict.fromkeys(timeframes))
            if (
                self.settings.operational_timeframe in selected
                and self._hourly_context_due()
                and "1h" not in selected
            ):
                selected.append("1h")
            self.logger.info(
                "OPERATIONAL_COLLECTOR started timeframes=%s", selected
            )
            results = self.data_collector.sync_all_pairs(
                self.settings.collector_pairs,
                selected,
            )
            successful = [
                item for item in results if getattr(item, "status", None) == "SUCCESS"
            ]
            operational_success = any(
                getattr(item, "timeframe", self.settings.operational_timeframe)
                == self.settings.operational_timeframe
                for item in successful
            )
            self.logger.info(
                "OPERATIONAL_COLLECTOR completed timeframes=%s success=%s total=%s operational_success=%s",
                selected,
                len(successful),
                len(results),
                operational_success,
            )
            if operational_success:
                self.logger.info(
                    "OPERATIONAL_CYCLE timeframe=%s action=position_then_scan context_1h_refreshed=%s",
                    self.settings.operational_timeframe,
                    "1h" in selected,
                )
                if self.position_monitor is not None:
                    self.position_monitor.monitor_open_signals()
                if self.strategy_engine is not None:
                    self.strategy_engine.evaluate()
        except Exception:
            self.logger.exception(
                "OPERATIONAL_CYCLE failed timeframes=%s", timeframes
            )
        finally:
            rss_mb = release_unused_memory()
            if rss_mb is not None:
                self.logger.info(
                    "MEMORY_RELEASE phase=operational_cycle rss_mb=%.1f", rss_mb
                )
            self._collector_lock.release()
