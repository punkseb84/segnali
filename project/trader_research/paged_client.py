"""Inclusive and deduplicated pagination for public Hyperliquid fill history."""
from __future__ import annotations

import time

from project.trader_research.hyperliquid import (
    HyperliquidApiError,
    HyperliquidFill,
    HyperliquidInfoClient,
)


class PagedHyperliquidInfoClient(HyperliquidInfoClient):
    def fetch_user_fills_by_time(
        self,
        wallet: str,
        *,
        start_time_ms: int,
        end_time_ms: int | None = None,
        aggregate_by_time: bool = True,
        max_fills: int = 10_000,
        max_pages: int = 100,
    ) -> list[HyperliquidFill]:
        wallet = self.validate_address(wallet)
        start = max(0, int(start_time_ms))
        end = int(end_time_ms) if end_time_ms is not None else int(time.time() * 1000)
        if end < start:
            raise ValueError("end_time_ms must not be earlier than start_time_ms")
        hard_limit = min(10_000, max(1, int(max_fills)))
        page_limit = max(1, int(max_pages))

        fills: dict[str, HyperliquidFill] = {}
        cursor = start
        pages = 0
        while cursor <= end and len(fills) < hard_limit and pages < page_limit:
            pages += 1
            before_count = len(fills)
            payload = self._post(
                {
                    "type": "userFillsByTime",
                    "user": wallet,
                    "startTime": cursor,
                    "endTime": end,
                    "aggregateByTime": bool(aggregate_by_time),
                }
            )
            if not isinstance(payload, list):
                raise HyperliquidApiError("Unexpected userFillsByTime response")

            page: list[HyperliquidFill] = []
            for raw in payload:
                if not isinstance(raw, dict):
                    continue
                fill = HyperliquidFill.from_api(wallet, raw)
                if fill.is_valid() and start <= fill.time_ms <= end:
                    page.append(fill)
                    fills[fill.fill_key] = fill
            if not page:
                break

            last_time = max(fill.time_ms for fill in page)
            if last_time < cursor:
                raise HyperliquidApiError("Fill pagination moved backwards")
            if last_time >= end:
                break

            new_items = len(fills) - before_count
            cursor = cursor + 1 if last_time == cursor and new_items == 0 else last_time

        ordered = sorted(
            fills.values(),
            key=lambda item: (item.time_ms, item.trade_id, item.order_id, item.fill_key),
        )
        return ordered[-hard_limit:]
