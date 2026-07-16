"""Central scheduler for modular platform phases optimized for Railway Light."""
from __future__ import annotations

import threading
import time
from typing import Protocol

import schedule

from project.config.settings import PlatformSettings
from project.data_collector.service import DataCollectorService
from project.shared.logging import get_module_logger


MIN_RAILWAY_LIGHT_RESEARCH_SECONDS = 3600


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
    def __init__(
        self,
        settings: PlatformSettings,
        data_collector: DataCollectorService | None = None,
        research_engine: ResearchEngineProtocol | None = None,
        decision_engine: DecisionEngineProtocol | None = None,
        strategy_engine: StrategyEngineProtocol | None = None,
        position_monitor: PositionMonitorProtocol | None = None,
    ) -> None:
        self.settings = settings
        self.data_collector = data_collector
        self.research_engine = research_engine
        self.decision_engine = decision_engine
        self.strategy_engine = strategy_engine
        self.position_monitor = position_monitor
        self.logger = get_module_logger("system")
        self._collector_lock = threading.Lock()
        self._collector_thread: threading.Thread | None = None
        self._research_lock = threading.Lock()
        self._research_thread: threading.Thread | None = None

    def effective_strategy_interval(self) -> int:
        requested = max(1, int(self.settings.scheduler_strategy_seconds))
        if self.settings.run_mode != "RAILWAY_LIGHT":
            return requested
        # A strategy evaluation cannot see fresher candles than the collector. Running
        # it five times on the same 15m data only repeats pandas work and duplicate SQL.
        return max(requested, int(self.settings.scheduler_collector_seconds))

    def configure(self) -> None:
        """Start the operational path before long collector/research work.

        Only the operational timeframe is refreshed synchronously at startup. The
        remaining collector timeframes and all research work run in background so
        Strategy Engine, Position Monitor and Telegram delivery are not starved.
        """
        startup_remaining_timeframes: list[str] = []
        if self.settings.enable_data_collector and self.data_collector is not None:
            schedule.every(self.settings.scheduler_collector_seconds).seconds.do(
                self.start_data_collector_sync
            )
            self.logger.info(
                "Data Collector scheduled every %ss in background",
                self.settings.scheduler_collector_seconds,
            )
            if self.settings.run_data_collector_on_startup:
                operational_timeframe = self.settings.operational_timeframe
                self.logger.info(
                    "Data Collector startup operational sync requested timeframe=%s",
                    operational_timeframe,
                )
                self.data_collector.sync_all_pairs(
                    self.settings.collector_pairs,
                    [operational_timeframe],
                )
                startup_remaining_timeframes = [
                    timeframe
                    for timeframe in self.settings.collector_timeframes
                    if timeframe != operational_timeframe
                ]

        # Notification subscriptions are already installed in main.
        if self.settings.enable_decision_engine and self.decision_engine is not None:
            schedule.every(self.settings.scheduler_decision_seconds).seconds.do(
                self.decision_engine.evaluate_market
            )
            self.logger.info(
                "Decision Engine scheduled every %ss",
                self.settings.scheduler_decision_seconds,
            )
            self.decision_engine.evaluate_market()

        if self.settings.enable_strategy_engine and self.strategy_engine is not None:
            strategy_interval = self.effective_strategy_interval()
            schedule.every(strategy_interval).seconds.do(
                self.strategy_engine.evaluate
            )
            schedule.every(1).hours.do(
                self.strategy_engine.publish_daily_signal_report
            )
            if strategy_interval != self.settings.scheduler_strategy_seconds:
                self.logger.info(
                    "Strategy Engine interval optimized requested=%ss effective=%ss reason=no_new_candles_before_collector",
                    self.settings.scheduler_strategy_seconds,
                    strategy_interval,
                )
            self.logger.info(
                "Strategy Engine scheduled every %ss",
                strategy_interval,
            )
            self.strategy_engine.evaluate()
            self.strategy_engine.publish_daily_signal_report()

        if self.settings.enable_position_monitor and self.position_monitor is not None:
            schedule.every(self.settings.scheduler_position_monitor_seconds).seconds.do(
                self.position_monitor.monitor_open_signals
            )
            self.logger.info(
                "Position Monitor scheduled every %ss",
                self.settings.scheduler_position_monitor_seconds,
            )
            self.position_monitor.monitor_open_signals()

        if startup_remaining_timeframes:
            self.logger.info(
                "Data Collector startup background sync requested timeframes=%s",
                startup_remaining_timeframes,
            )
            self.start_data_collector_sync(startup_remaining_timeframes)

        if self.settings.enable_research_engine and self.research_engine is not None:
            self.research_engine.seed_combinations(
                self.settings.collector_pairs,
                self.settings.collector_timeframes,
            )
            if self.settings.run_mode == "RESEARCH":
                interval = self.settings.research_sleep_between_batches_seconds
            elif self.settings.run_mode == "RAILWAY_LIGHT":
                requested_interval = self.settings.railway_light_research_seconds
                interval = max(
                    MIN_RAILWAY_LIGHT_RESEARCH_SECONDS,
                    requested_interval,
                )
                if requested_interval < MIN_RAILWAY_LIGHT_RESEARCH_SECONDS:
                    self.logger.warning(
                        "Research Engine interval clamped requested=%ss effective=%ss reason=protect_operational_runtime_and_railway_costs",
                        requested_interval,
                        interval,
                    )
            else:
                interval = None

            if interval is None:
                schedule.every().day.at("02:00").do(self.start_research_batch)
                self.logger.info(
                    "Research Engine scheduled nightly at 02:00 in background"
                )
            else:
                schedule.every(interval).seconds.do(self.start_research_batch)
                self.logger.info(
                    "Research Engine scheduled every %ss in background with batch_size=%s runtime_limit=%sm",
                    interval,
                    self.settings.research_batch_size,
                    self.settings.max_research_runtime_minutes,
                )

            if self.settings.run_research_on_startup:
                self.logger.info(
                    "Research Engine startup batch requested after operational startup"
                )
                self.start_research_batch()

        if self.settings.enable_notification_engine:
            self.logger.info("Notification Engine enabled and subscribed to EventBus")

    def start_data_collector_sync(
        self,
        timeframes: list[str] | None = None,
    ) -> bool:
        """Start one collector sync unless a previous sync is still running."""
        if self.data_collector is None:
            return False
        if not self._collector_lock.acquire(blocking=False):
            self.logger.info(
                "Data Collector sync skipped: previous background sync still running"
            )
            return False

        selected_timeframes = list(timeframes or self.settings.collector_timeframes)
        self._collector_thread = threading.Thread(
            target=self._run_data_collector_sync,
            args=(selected_timeframes,),
            name="data-collector-sync",
            daemon=True,
        )
        self._collector_thread.start()
        return True

    def _run_data_collector_sync(self, timeframes: list[str]) -> None:
        try:
            self.logger.info(
                "Data Collector background sync started timeframes=%s",
                timeframes,
            )
            self.data_collector.sync_all_pairs(
                self.settings.collector_pairs,
                timeframes,
            )
            self.logger.info(
                "Data Collector background sync completed timeframes=%s",
                timeframes,
            )
        except Exception:
            self.logger.exception(
                "Data Collector background sync failed timeframes=%s",
                timeframes,
            )
        finally:
            self._collector_lock.release()

    def start_research_batch(self) -> bool:
        """Start one daemon research batch unless another batch is still running."""
        if self.research_engine is None:
            return False
        if not self._research_lock.acquire(blocking=False):
            self.logger.info(
                "Research Engine batch skipped: previous background batch still running"
            )
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
            if now - last_heartbeat >= 300:
                self.logger.info("Heartbeat")
                last_heartbeat = now
            time.sleep(1)
