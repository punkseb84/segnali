"""Central scheduler for modular platform phases optimized for Railway Light."""
from __future__ import annotations

import threading
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
        self._research_lock = threading.Lock()
        self._research_thread: threading.Thread | None = None

    def configure(self) -> None:
        """Configure operational jobs before starting non-blocking research.

        The previous order executed a research batch synchronously before the first
        Decision/Strategy cycle. A long research batch could therefore prevent the
        bot from generating and delivering signals for most of its runtime.
        """
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

        # The live operational path must be ready before research is allowed to
        # consume CPU. Notification subscriptions are already installed in main.
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

        if self.settings.enable_research_engine and self.research_engine is not None:
            self.research_engine.seed_combinations(self.settings.collector_pairs, self.settings.collector_timeframes)
            if self.settings.run_mode == "RESEARCH":
                interval = self.settings.research_sleep_between_batches_seconds
            elif self.settings.run_mode == "RAILWAY_LIGHT":
                interval = self.settings.railway_light_research_seconds
            else:
                interval = None

            if interval is None:
                schedule.every().day.at("02:00").do(self.start_research_batch)
                self.logger.info("Research Engine scheduled nightly at 02:00 in background")
            else:
                schedule.every(interval).seconds.do(self.start_research_batch)
                self.logger.info(
                    "Research Engine scheduled every %ss in background with batch_size=%s runtime_limit=%sm",
                    interval,
                    self.settings.research_batch_size,
                    self.settings.max_research_runtime_minutes,
                )

            if self.settings.run_research_on_startup:
                self.logger.info("Research Engine startup batch requested after operational startup")
                self.start_research_batch()

        if self.settings.enable_notification_engine:
            self.logger.info("Notification Engine enabled and subscribed to EventBus")

    def start_research_batch(self) -> bool:
        """Start one daemon research batch unless another batch is still running."""
        if self.research_engine is None:
            return False
        if not self._research_lock.acquire(blocking=False):
            self.logger.info("Research Engine batch skipped: previous background batch still running")
            return False

        self._research_thread = threading.Thread(
            target=self._run_research_batch,
            name="research-batch",
            daemon=True,
        )
        self._research_thread.start()
        return True

    def _run_research_batch(self) -> None:
        try:
            self.logger.info("Research Engine background batch started")
            self.research_engine.process_one_batch(sleep_after=False)
            self.logger.info("Research Engine background batch completed")
        except Exception:
            self.logger.exception("Research Engine background batch failed")
        finally:
            self._research_lock.release()

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
