"""Operational scheduler aligned to closed 1h candles for Ichimoku management."""
from __future__ import annotations

from datetime import datetime, timezone

import schedule

from project.operational_scheduler import OperationalMarketScheduler


class HourAlignedIchimokuScheduler(OperationalMarketScheduler):
    """Run the normal cycle and add one fresh sync immediately after each UTC hour."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._last_hour_close_sync_key: str | None = None

    def configure(self) -> None:
        super().configure()
        if self.settings.operational_timeframe != "1h":
            return
        schedule.every(30).seconds.do(self._sync_after_hour_close_if_due)
        self.logger.info(
            "ICHIMOKU_HOUR_CLOSE_SYNC scheduled poll=30s eligible_utc_minutes=1-4"
        )

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

        started = self.start_data_collector_sync(
            [self.settings.operational_timeframe]
        )
        if not started:
            self.logger.info(
                "ICHIMOKU_HOUR_CLOSE_SYNC deferred hour=%s reason=collector_busy",
                hour_key,
            )
            return False

        self._last_hour_close_sync_key = hour_key
        self.logger.info(
            "ICHIMOKU_HOUR_CLOSE_SYNC started hour=%s timeframe=%s",
            hour_key,
            self.settings.operational_timeframe,
        )
        return True
