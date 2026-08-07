"""Managed outcome monitor for Relative Strength V5+.

Exit priority is price-based: hard stop, target, cost-aware breakeven and trailing
stop, then maximum holding time. Relative-strength reversal no longer closes a
position immediately; it remains an analytical signal only.

Management is reconstructed deterministically from closed 15m candles on every
cycle, so no mutable trailing state is required in PostgreSQL. To avoid lookahead,
a stop activated by a candle becomes executable from the following candle.
"""
from __future__ import annotations

from typing import Any

from project.relative_strength_monitor import RelativeStrengthMonitor


class RelativeStrengthV3Monitor(RelativeStrengthMonitor):
    BREAKEVEN_TRIGGER_R = 0.80
    TRAILING_TRIGGER_R = 1.00
    TRAILING_DISTANCE_R = 0.75

    def _net_breakeven_price(self, signal: dict[str, Any], direction: str) -> float:
        """Price that approximately covers estimated round-trip execution costs."""
        entry = float(signal["entry"])
        quantity = float(signal.get("quantity") or 0.0)
        if quantity <= 0:
            return entry
        costs = self._costs(signal, entry)
        per_unit = float(costs["total_costs_eur"]) / quantity
        return entry + per_unit if direction == "LONG" else max(0.0, entry - per_unit)

    def _check(self, signal: dict[str, Any]) -> bool:
        # Current Kraken price can close an already-reached hard TP/SL immediately.
        if self._check_live_levels(signal):
            return True

        exchange = str(signal.get("exchange") or "Kraken")
        frame = self._frame(signal["pair"], exchange)
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

        entry = float(signal["entry"])
        initial_stop = float(signal["stop_loss"])
        target = float(signal["take_profit"])
        risk = abs(entry - initial_stop)
        if risk <= 0:
            return False

        net_be = self._net_breakeven_price(signal, direction)
        active_stop = initial_stop
        pending_stop = initial_stop
        best_favorable = entry
        be_armed = False
        trailing_armed = False

        self.logger.info(
            "RS_MANAGEMENT_START id=%s pair=%s direction=%s entry=%.8f hard_stop=%.8f target=%.8f risk=%.8f net_be=%.8f",
            signal["id"], signal["pair"], direction, entry, initial_stop, target, risk, net_be,
        )

        for index, (_, row) in enumerate(eligible.iterrows()):
            high = float(row["high"])
            low = float(row["low"])

            # A management stop derived from the prior candle is active now.
            active_stop = pending_stop
            stop_hit = low <= active_stop if direction == "LONG" else high >= active_stop
            target_hit = high >= target if direction == "LONG" else low <= target

            if stop_hit and target_hit:
                return self._close(
                    signal,
                    float(row["close"]),
                    row,
                    "RELATIVE_STRENGTH_AMBIGUOUS_BOTH_HIT",
                )
            if target_hit:
                return self._close(signal, target, row, "RELATIVE_STRENGTH_TARGET")
            if stop_hit:
                if trailing_armed:
                    reason = "RELATIVE_STRENGTH_TRAILING_STOP"
                elif be_armed:
                    reason = "RELATIVE_STRENGTH_BREAKEVEN_STOP"
                else:
                    reason = "RELATIVE_STRENGTH_STOP"
                return self._close(signal, active_stop, row, reason)

            if direction == "LONG":
                best_favorable = max(best_favorable, high)
                favorable_r = (best_favorable - entry) / risk
                if favorable_r >= self.TRAILING_TRIGGER_R:
                    trailing_armed = True
                    be_armed = True
                    trailing_level = best_favorable - self.TRAILING_DISTANCE_R * risk
                    pending_stop = max(pending_stop, net_be, trailing_level)
                elif favorable_r >= self.BREAKEVEN_TRIGGER_R:
                    be_armed = True
                    pending_stop = max(pending_stop, net_be)
            else:
                best_favorable = min(best_favorable, low)
                favorable_r = (entry - best_favorable) / risk
                if favorable_r >= self.TRAILING_TRIGGER_R:
                    trailing_armed = True
                    be_armed = True
                    trailing_level = best_favorable + self.TRAILING_DISTANCE_R * risk
                    pending_stop = min(pending_stop, net_be, trailing_level)
                elif favorable_r >= self.BREAKEVEN_TRIGGER_R:
                    be_armed = True
                    pending_stop = min(pending_stop, net_be)

            self.logger.info(
                "RS_MANAGEMENT_CANDLE id=%s n=%s candle=%s high=%.8f low=%.8f favorable_r=%.3f active_stop=%.8f next_stop=%.8f be=%s trailing=%s",
                signal["id"], index + 1, row["timestamp"], high, low, favorable_r,
                active_stop, pending_stop, be_armed, trailing_armed,
            )

        # RS reversal is deliberately NOT an exit anymore. It is logged only.
        relative = self._relative_return(signal["pair"], exchange)
        reversal = relative is not None and (
            (direction == "LONG" and relative < 0)
            or (direction == "SHORT" and relative > 0)
        )
        if reversal:
            self.logger.info(
                "RS_REVERSAL_OBSERVED id=%s pair=%s action=HOLD_UNTIL_PRICE_EXIT relative=%.6f",
                signal["id"], signal["pair"], float(relative),
            )

        max_hold = int(signal["state"].get("max_hold_candles", 8))
        if len(eligible) >= max_hold:
            last = eligible.iloc[-1]
            return self._close(
                signal,
                float(last["close"]),
                last,
                "RELATIVE_STRENGTH_TIME_EXIT",
            )
        return False
