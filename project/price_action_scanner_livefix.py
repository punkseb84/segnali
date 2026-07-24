"""Production fixes for the pure price-action scanner.

This module keeps the V7 price-action rules but removes an accidental timing
bottleneck: the original scanner required the retest and bullish rejection to
occur on the latest 15m candle inside a six-candle window. In live markets that
made valid setups exceptionally rare.
"""
from __future__ import annotations

from typing import Any

import pandas as pd

from project.price_action_scanner import (
    HORIZONTAL_RETEST,
    PRICE_ACTION_RUNTIME_VERSION,
    TRENDLINE_RETEST,
    PurePriceActionScanner,
)


class LivePurePriceActionScanner(PurePriceActionScanner):
    """Price-action scanner with live-friendly confirmation timing."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs.setdefault("min_trendline_touches", 2)
        kwargs.setdefault("retest_window_15m", 12)
        super().__init__(*args, **kwargs)
        # The base class historically forced at least three trendline touches.
        # Two clean swing highs already define a valid descending trendline;
        # the breakout/retest confirmation remains mandatory.
        self.min_trendline_touches = max(2, int(kwargs.get("min_trendline_touches", 2)))
        self.retest_window_15m = max(6, int(kwargs.get("retest_window_15m", 12)))

    def _retest_trigger(
        self,
        frame_15m: pd.DataFrame,
        *,
        level: float,
        break_time: Any | None,
        tolerance: float,
    ) -> dict[str, Any] | None:
        recent = frame_15m.tail(self.retest_window_15m).copy()
        if break_time is not None:
            timestamp = pd.Timestamp(break_time)
            if timestamp.tzinfo is None:
                timestamp = timestamp.tz_localize("UTC")
            recent = recent[recent["timestamp"] >= timestamp]
        if recent.empty:
            return None

        touched_mask = (
            (recent["low"] <= level + tolerance)
            & (recent["high"] >= level - tolerance)
            & (recent["close"] >= level - tolerance * 0.35)
        )
        touched = recent[touched_mask]
        if touched.empty:
            return None

        # Accept a rejection on any of the latest three closed candles after the
        # most recent touch, instead of requiring it on the single latest candle.
        last_touch_time = touched.iloc[-1]["timestamp"]
        confirmations = recent[recent["timestamp"] >= last_touch_time].tail(3)
        trigger_row = None
        for _, row in confirmations.iloc[::-1].iterrows():
            if float(row["close"]) >= level - tolerance * 0.10 and self._bullish_rejection(row):
                trigger_row = row
                break
        if trigger_row is None:
            return None

        post_touch = recent[recent["timestamp"] >= last_touch_time]
        return {
            "entry": float(trigger_row["close"]),
            "structural_low": float(post_touch["low"].min()),
            "trigger_time": trigger_row["timestamp"],
            "retest_count": len(touched),
        }


__all__ = [
    "HORIZONTAL_RETEST",
    "PRICE_ACTION_RUNTIME_VERSION",
    "TRENDLINE_RETEST",
    "LivePurePriceActionScanner",
]
