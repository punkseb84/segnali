"""Central scheduler for modular platform phases."""
from __future__ import annotations

import time

import schedule

from project.config.settings import PlatformSettings
from project.data_collector.service import DataCollectorService
from project.shared.logging import get_module_logger


class PlatformScheduler:
    def __init__(self, settings: PlatformSettings, data_collector: DataCollectorService | None = None) -> None:
        self.settings = settings
        self.data_collector = data_collector
        self.logger = get_module_logger("system")

    def configure(self) -> None:
        if self.settings.enable_data_collector and self.data_collector is not None:
            schedule.every(self.settings.scheduler_collector_seconds).seconds.do(
                self.data_collector.sync_all_pairs,
                self.settings.collector_pairs,
                self.settings.collector_timeframes,
            )
            self.logger.info("Data Collector scheduled every %ss", self.settings.scheduler_collector_seconds)
        if self.settings.enable_research_engine:
            self.logger.info("Research Engine scheduling reserved for Phase 3")
        if self.settings.enable_decision_engine:
            self.logger.info("Decision Engine scheduling reserved for Phase 4")
        if self.settings.enable_strategy_engine:
            self.logger.info("Strategy Engine scheduling reserved for Phase 5")
        if self.settings.enable_notification_engine:
            self.logger.info("Notification Engine real-time subscriptions reserved for Phase 6")

    def run_forever(self) -> None:
        self.configure()
        while True:
            schedule.run_pending()
            time.sleep(1)
