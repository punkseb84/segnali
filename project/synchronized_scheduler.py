"""Railway scheduler that evaluates strategies only after fresh operational OHLC."""
from __future__ import annotations

from typing import Any

import schedule

from project.scheduler import PlatformScheduler


class SynchronizedPlatformScheduler(PlatformScheduler):
    """Run decision and strategy after the operational collector finishes.

    The base scheduler starts the collector in a background thread and then immediately
    evaluates the strategy on the previous snapshot. That race can publish stale and
    repeated entries. In Railway Light the recurring decision/strategy jobs are removed
    and triggered after the 15m batch has completed.
    """

    @staticmethod
    def _job_is_bound_method(job: Any, instance: Any, method_name: str) -> bool:
        job_func = getattr(job, "job_func", None)
        target = getattr(job_func, "func", job_func)
        return (
            getattr(target, "__self__", None) is instance
            and getattr(target, "__name__", "") == method_name
        )

    def configure(self) -> None:
        super().configure()
        if self.settings.run_mode != "RAILWAY_LIGHT" or self.data_collector is None:
            return

        cancelled = 0
        for job in list(schedule.jobs):
            if self.decision_engine is not None and self._job_is_bound_method(
                job, self.decision_engine, "evaluate_market"
            ):
                schedule.cancel_job(job)
                cancelled += 1
                continue
            if self.strategy_engine is not None and self._job_is_bound_method(
                job, self.strategy_engine, "evaluate"
            ):
                schedule.cancel_job(job)
                cancelled += 1
        self.logger.info(
            "Operational cycle synchronized after collector cancelled_independent_jobs=%s",
            cancelled,
        )

        # A new versioned robust research set would otherwise wait up to one hour before
        # processing its first batch. Start exactly one background batch on deployment;
        # the normal lock prevents overlap with RUN_RESEARCH_ON_STARTUP or another batch.
        if self.research_engine is not None and not self.settings.run_research_on_startup:
            repository = getattr(self.research_engine, "repository", None)
            diagnostics = None
            if repository is not None and hasattr(repository, "fetch_candidate_diagnostics"):
                try:
                    diagnostics = repository.fetch_candidate_diagnostics(
                        timeframe=self.settings.operational_timeframe
                    )
                except Exception:
                    self.logger.exception("Robust research startup diagnostics failed")
            if diagnostics and diagnostics.get("pending_combinations", 0) > 0:
                started = self.start_research_batch()
                self.logger.info(
                    "ROBUST_RESEARCH startup_batch_started=%s diagnostics=%s",
                    started,
                    diagnostics,
                )

    def _run_data_collector_sync(self, timeframes: list[str]) -> None:
        try:
            self.logger.info(
                "Data Collector background sync started timeframes=%s", timeframes
            )
            results = self.data_collector.sync_all_pairs(
                self.settings.collector_pairs,
                timeframes,
            )
            success = sum(1 for item in results if getattr(item, "status", None) == "SUCCESS")
            self.logger.info(
                "Data Collector background sync completed timeframes=%s success=%s total=%s",
                timeframes,
                success,
                len(results),
            )

            if self.settings.operational_timeframe in timeframes and success > 0:
                self.logger.info(
                    "OPERATIONAL_CYCLE fresh_data=true timeframe=%s action=decision_then_strategy",
                    self.settings.operational_timeframe,
                )
                if self.decision_engine is not None:
                    self.decision_engine.evaluate_market()
                if self.strategy_engine is not None:
                    self.strategy_engine.evaluate()
        except Exception:
            self.logger.exception(
                "Data Collector synchronized operational cycle failed timeframes=%s",
                timeframes,
            )
        finally:
            self._collector_lock.release()
