"""Central scheduler for modular platform phases optimized for Railway Light."""
from __future__ import annotations

import time
from typing import Protocol

import schedule

from project.config.settings import PlatformSettings
from project.data_collector.service import DataCollectorService
from project.shared.logging import get_module_logger


class ResearchEngineProtocol(Protocol):
    def seed_combinations(self, pairs: list[str], timeframes: list[str], chunk_size: int = 1000) -> int: ...
    def process_one_batch(self, sleep_after: bool = True): ...


class DecisionEngineProtocol(Protocol):
    def evaluate_market(self): ...


class StrategyEngineProtocol(Protocol):
    def evaluate(self): ...
    def publish_daily_signal_report(self) -> bool: ...


class PositionMonitorProtocol(Protocol):
    def monitor_open_signals(self) -> int: ...


class PlatformScheduler:
    def __init__(self, settings: PlatformSettings, data_collector: DataCollectorService | None = None, research_engine: ResearchEngineProtocol | None = None, decision_engine: DecisionEngineProtocol | None = None, strategy_engine: StrategyEngineProtocol | None = None, position_monitor: PositionMonitorProtocol | None = None) -> None:
        self.settings = settings
        self.data_collector = data_collector
        self.research_engine = research_engine
        self.decision_engine = decision_engine
        self.strategy_engine = strategy_engine
        self.position_monitor = position_monitor
        self.logger = get_module_logger("system")

    def configure(self) -> None:
        if self.settings.enable_data_collector and self.data_collector is not None:
            schedule.every(self.settings.scheduler_collector_seconds).seconds.do(
                self.data_collector.sync_all_pairs,
                self.settings.collector_pairs,
                self.settings.collector_timeframes,
            )
            self.logger.info("Data Collector scheduled every %ss", self.settings.scheduler_collector_seconds)
            if self.settings.run_data_collector_on_startup:
                self.logger.info("Data Collector startup sync requested")
                self.data_collector.sync_all_pairs(self.settings.collector_pairs, self.settings.collector_timeframes)
        if self.settings.enable_research_engine and self.research_engine is not None:
            self.research_engine.seed_combinations(self.settings.collector_pairs, self.settings.collector_timeframes)
            if self.settings.run_mode == "RESEARCH":
                schedule.every(self.settings.research_sleep_between_batches_seconds).seconds.do(self.research_engine.process_one_batch, False)
                self.logger.info("Research Engine scheduled in RUN_MODE=RESEARCH every %ss", self.settings.research_sleep_between_batches_seconds)
            elif self.settings.run_mode == "RAILWAY_LIGHT":
                schedule.every(self.settings.railway_light_research_seconds).seconds.do(self.research_engine.process_one_batch, False)
                self.logger.info("Research Engine scheduled in RAILWAY_LIGHT every %ss with batch_size=%s runtime_limit=%sm", self.settings.railway_light_research_seconds, self.settings.research_batch_size, self.settings.max_research_runtime_minutes)
            else:
                schedule.every().day.at("02:00").do(self.research_engine.process_one_batch, False)
                self.logger.info("Research Engine scheduled nightly at 02:00")
            if self.settings.run_research_on_startup:
                self.logger.info("Research Engine startup batch requested")
                self.research_engine.process_one_batch(sleep_after=False)
        if self.settings.enable_decision_engine and self.decision_engine is not None:
            schedule.every(self.settings.scheduler_decision_seconds).seconds.do(self.decision_engine.evaluate_market)
            self.logger.info("Decision Engine scheduled every %ss", self.settings.scheduler_decision_seconds)
            self.decision_engine.evaluate_market()
        if self.settings.enable_strategy_engine and self.strategy_engine is not None:
            schedule.every(self.settings.scheduler_strategy_seconds).seconds.do(self.strategy_engine.evaluate)
            schedule.every(1).hours.do(self.strategy_engine.publish_daily_signal_report)
            self.logger.info("Strategy Engine scheduled every %ss", self.settings.scheduler_strategy_seconds)
            self.strategy_engine.evaluate()
            self.strategy_engine.publish_daily_signal_report()
        if self.settings.enable_position_monitor and self.position_monitor is not None:
            schedule.every(self.settings.scheduler_position_monitor_seconds).seconds.do(self.position_monitor.monitor_open_signals)
            self.logger.info("Position Monitor scheduled every %ss", self.settings.scheduler_position_monitor_seconds)
            self.position_monitor.monitor_open_signals()
        if self.settings.enable_notification_engine:
            self.logger.info("Notification Engine enabled and subscribed to EventBus")

    def run_once(self) -> None:
        self.configure()
        schedule.run_pending()

    def run_forever(self) -> None:
        self.configure()
        self.logger.info("Checkpoint E - Scheduler started")
        last_heartbeat = 0.0
        while True:
            schedule.run_pending()
            now = time.monotonic()
            if now - last_heartbeat >= 30:
                self.logger.info("Heartbeat")
                last_heartbeat = now
            time.sleep(1)
