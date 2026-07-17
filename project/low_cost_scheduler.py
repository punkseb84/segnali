"""Low-cost Railway scheduler with fast isolated research bootstrap.

Research still runs outside the always-on process, but the bootstrap is large enough to
produce usable candidates quickly instead of taking weeks to traverse the robust grid.
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
from typing import Any

import schedule

from project.shared.memory import release_unused_memory
from project.synchronized_scheduler import SynchronizedPlatformScheduler


class LowCostPlatformScheduler(SynchronizedPlatformScheduler):
    """Synchronize operational work and isolate research memory."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._research_process_lock = threading.Lock()
        self._research_process: subprocess.Popen[str] | None = None
        self._startup_research_timer: threading.Timer | None = None
        self.research_interval_hours = max(
            2,
            int(os.getenv("LOW_COST_RESEARCH_INTERVAL_HOURS", "3")),
        )
        self.startup_research_delay_seconds = max(
            20,
            int(os.getenv("LOW_COST_RESEARCH_STARTUP_DELAY_SECONDS", "45")),
        )

    def configure(self) -> None:
        super().configure()

        schedule.every(self.research_interval_hours).hours.do(
            self.start_isolated_research_worker
        )
        self._startup_research_timer = threading.Timer(
            self.startup_research_delay_seconds,
            self.start_isolated_research_worker,
            kwargs={"startup": True},
        )
        self._startup_research_timer.daemon = True
        self._startup_research_timer.start()
        self.logger.info(
            "LOW_COST_RESEARCH isolated=true startup=true startup_delay_seconds=%s "
            "startup_batch_size=%s interval_hours=%s periodic_batch_size=%s",
            self.startup_research_delay_seconds,
            os.getenv("LOW_COST_RESEARCH_STARTUP_BATCH_SIZE", "60"),
            self.research_interval_hours,
            os.getenv("LOW_COST_RESEARCH_BATCH_SIZE", "40"),
        )

        cancelled = 0
        for job in list(schedule.jobs):
            if self.position_monitor is not None and self._job_is_bound_method(
                job, self.position_monitor, "monitor_open_signals"
            ):
                schedule.cancel_job(job)
                cancelled += 1
        self.logger.info(
            "LOW_COST_POSITION_MONITOR synchronized_after_collector=true cancelled_jobs=%s",
            cancelled,
        )

    def _run_data_collector_sync(self, timeframes: list[str]) -> None:
        try:
            self.logger.info(
                "Data Collector low-cost sync started timeframes=%s", timeframes
            )
            results = self.data_collector.sync_all_pairs(
                self.settings.collector_pairs,
                timeframes,
            )
            success = sum(
                1 for item in results if getattr(item, "status", None) == "SUCCESS"
            )
            self.logger.info(
                "Data Collector low-cost sync completed timeframes=%s success=%s total=%s",
                timeframes,
                success,
                len(results),
            )

            if self.settings.operational_timeframe in timeframes and success > 0:
                self.logger.info(
                    "LOW_COST_OPERATIONAL_CYCLE fresh_data=true timeframe=%s "
                    "action=position_then_decision_then_strategy",
                    self.settings.operational_timeframe,
                )
                if self.position_monitor is not None:
                    self.position_monitor.monitor_open_signals()
                if self.decision_engine is not None:
                    self.decision_engine.evaluate_market()
                if self.strategy_engine is not None:
                    self.strategy_engine.evaluate()
        except Exception:
            self.logger.exception(
                "Data Collector low-cost operational cycle failed timeframes=%s",
                timeframes,
            )
        finally:
            rss_mb = release_unused_memory()
            if rss_mb is not None:
                self.logger.info(
                    "MEMORY_RELEASE phase=operational_cycle rss_mb=%.1f", rss_mb
                )
            self._collector_lock.release()

    def start_isolated_research_worker(self, startup: bool = False) -> bool:
        """Launch one short-lived research subprocess unless one is already active."""
        if not self._research_process_lock.acquire(blocking=False):
            self.logger.info("LOW_COST_RESEARCH skipped reason=worker_lock_busy")
            return False
        try:
            if self._research_process is not None and self._research_process.poll() is None:
                self.logger.info("LOW_COST_RESEARCH skipped reason=worker_already_running")
                return False

            mode = "startup" if startup else "periodic"
            batch_size = os.getenv(
                "LOW_COST_RESEARCH_STARTUP_BATCH_SIZE" if startup else "LOW_COST_RESEARCH_BATCH_SIZE",
                "60" if startup else "40",
            )
            runtime_minutes = os.getenv(
                "LOW_COST_RESEARCH_STARTUP_RUNTIME_MINUTES" if startup else "LOW_COST_RESEARCH_RUNTIME_MINUTES",
                "6" if startup else "4",
            )
            env = os.environ.copy()
            env.update(
                {
                    "RESEARCH_BATCH_SIZE": batch_size,
                    "MAX_RESEARCH_RUNTIME_MINUTES": runtime_minutes,
                    "ROBUST_RESEARCH_OHLC_LIMIT": env.get(
                        "LOW_COST_RESEARCH_OHLC_LIMIT", "3000"
                    ),
                    "LOW_COST_RESEARCH_WORKER_MODE": mode,
                    "OMP_NUM_THREADS": "1",
                    "OPENBLAS_NUM_THREADS": "1",
                    "MKL_NUM_THREADS": "1",
                    "NUMEXPR_NUM_THREADS": "1",
                    "MALLOC_ARENA_MAX": "2",
                }
            )
            self._research_process = subprocess.Popen(
                [sys.executable, "-m", "project.research_worker"],
                env=env,
                text=True,
            )
            self.logger.info(
                "LOW_COST_RESEARCH worker_started mode=%s pid=%s batch_size=%s "
                "runtime_minutes=%s ohlc_limit=%s",
                mode,
                self._research_process.pid,
                env["RESEARCH_BATCH_SIZE"],
                env["MAX_RESEARCH_RUNTIME_MINUTES"],
                env["ROBUST_RESEARCH_OHLC_LIMIT"],
            )
            threading.Thread(
                target=self._wait_for_research_worker,
                args=(mode,),
                name="research-worker-waiter",
                daemon=True,
            ).start()
            return True
        finally:
            self._research_process_lock.release()

    def _wait_for_research_worker(self, mode: str) -> None:
        process = self._research_process
        if process is None:
            return
        return_code = process.wait()
        self.logger.info(
            "LOW_COST_RESEARCH worker_finished mode=%s pid=%s return_code=%s "
            "memory_released=true",
            mode,
            process.pid,
            return_code,
        )
