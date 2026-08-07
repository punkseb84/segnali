"""Position lifecycle for Velez intraday mode 2.

The entry setup belongs to the Velez EMA20/EMA200 scanner. Once a PAPER position
is open there is deliberately no fixed take profit, no automatic breakeven and
no trailing stop. Exits are limited to:
1) the original structural stop;
2) a CLOSED 15m candle crossing EMA20 against the trade;
3) a maximum holding time of 12 hours (48 completed 15m candles).

The trigger candle is excluded from exit evaluation because the simulated entry
occurs only after that candle has closed.
"""
from __future__ import annotations

from typing import Any

import pandas as pd

from project.relative_strength_monitor import RelativeStrengthMonitor


class Velez15mMonitor(RelativeStrengthMonitor):
    MAX_HOLD_CANDLES = 48

    def _check_live_hard_stop(self, signal: dict[str, Any]) -> bool:
        """Use Kraken live price only for the original hard stop; never for EMA exits."""
        if str(signal.get("exchange") or "Kraken").lower() != "kraken":
            return False
        price = self._live_price(signal["pair"])
        if price is None:
            return False
        direction = str(signal["state"].get("direction", "LONG"))
        stop = float(signal["stop_loss"])
        hit = price <= stop if direction == "LONG" else price >= stop
        self.logger.info(
            "VELEZ_MONITOR_LIVE id=%s pair=%s price=%.8f hard_stop=%.8f direction=%s stop_hit=%s",
            signal["id"], signal["pair"], price, stop, direction, hit,
        )
        if hit:
            return self._close(
                signal,
                stop,
                self._live_row(price),
                "VELEZ_HARD_STOP",
            )
        return False

    def _check(self, signal: dict[str, Any]) -> bool:
        if self._check_live_hard_stop(signal):
            return True

        exchange = str(signal.get("exchange") or "Kraken")
        frame = self._frame(signal["pair"], exchange)
        if frame.empty:
            self.logger.warning(
                "VELEZ_MONITOR_NO_FRAME id=%s pair=%s exchange=%s",
                signal["id"], signal["pair"], exchange,
            )
            return False

        # EMA20 is recomputed from the same closed 15m Kraken candles used by the scanner.
        frame = frame.copy()
        frame["ema20"] = frame["close"].ewm(span=20, adjust=False).mean()

        direction = str(signal["state"].get("direction", "LONG"))
        reference = self._utc(
            signal.get("reference_candle_time")
            or signal.get("telegram_sent_at")
            or signal.get("created_at")
        )
        # Entry is after the trigger candle close, therefore never evaluate that candle as an exit.
        eligible = frame[frame["timestamp"] > reference.floor("15min")].copy()
        if eligible.empty:
            return False

        hard_stop = float(signal["stop_loss"])
        max_hold = int(signal["state"].get("max_hold_candles") or self.MAX_HOLD_CANDLES)
        max_hold = max(1, min(max_hold, self.MAX_HOLD_CANDLES))

        self.logger.info(
            "VELEZ_MONITOR_START id=%s pair=%s direction=%s hard_stop=%.8f candles=%s max_hold=%s",
            signal["id"], signal["pair"], direction, hard_stop, len(eligible), max_hold,
        )

        for index, (_, row) in enumerate(eligible.iterrows(), start=1):
            high = float(row["high"])
            low = float(row["low"])
            close = float(row["close"])
            ema20 = float(row["ema20"])
            stop_hit = low <= hard_stop if direction == "LONG" else high >= hard_stop
            ema_exit = close < ema20 if direction == "LONG" else close > ema20

            self.logger.info(
                "VELEZ_MONITOR_CANDLE id=%s n=%s candle=%s close=%.8f ema20=%.8f stop_hit=%s ema_exit=%s",
                signal["id"], index, row["timestamp"], close, ema20, stop_hit, ema_exit,
            )

            # The structural stop has priority because it can be touched intrabar.
            if stop_hit:
                return self._close(signal, hard_stop, row, "VELEZ_HARD_STOP")

            # EMA20 exit is based ONLY on a completed 15m candle close.
            if ema_exit:
                return self._close(signal, close, row, "VELEZ_EMA20_EXIT")

            if index >= max_hold:
                return self._close(signal, close, row, "VELEZ_12H_TIME_EXIT")

        return False
