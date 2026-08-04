"""Outcome monitor for Relative Strength V3.

A 15m candle that touches both stop and target is not forced into a stop-loss.
With OHLC data the intrabar order is unknowable, so the trade is closed at the
candle close and explicitly tagged ambiguous for later audit.
"""
from __future__ import annotations

from typing import Any

from project.relative_strength_monitor import RelativeStrengthMonitor


class RelativeStrengthV3Monitor(RelativeStrengthMonitor):
    def _check(self, signal: dict[str, Any]) -> bool:
        frame = self._frame(signal["pair"])
        if frame.empty:
            return False
        direction = str(signal["state"].get("direction", "LONG"))
        delivery = self._utc(
            signal.get("telegram_sent_at")
            or signal.get("created_at")
            or signal.get("reference_candle_time")
        )
        eligible = frame[frame["timestamp"] >= delivery.floor("15min")]
        if eligible.empty:
            return False

        for _, row in eligible.iterrows():
            high, low = float(row["high"]), float(row["low"])
            stop, target = float(signal["stop_loss"]), float(signal["take_profit"])
            stop_hit = low <= stop if direction == "LONG" else high >= stop
            target_hit = high >= target if direction == "LONG" else low <= target
            if stop_hit and target_hit:
                return self._close(
                    signal,
                    float(row["close"]),
                    row,
                    "RELATIVE_STRENGTH_AMBIGUOUS_BOTH_HIT",
                )
            if stop_hit:
                return self._close(signal, stop, row, "RELATIVE_STRENGTH_STOP")
            if target_hit:
                return self._close(signal, target, row, "RELATIVE_STRENGTH_TARGET")

        relative = self._relative_return(signal["pair"])
        reversal = relative is not None and (
            (direction == "LONG" and relative < 0)
            or (direction == "SHORT" and relative > 0)
        )
        max_hold = int(signal["state"].get("max_hold_candles", 8))
        if reversal or len(eligible) >= max_hold:
            last = eligible.iloc[-1]
            return self._close(
                signal,
                float(last["close"]),
                last,
                "RELATIVE_STRENGTH_REVERSAL"
                if reversal
                else "RELATIVE_STRENGTH_TIME_EXIT",
            )
        return False
