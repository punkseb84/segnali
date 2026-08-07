"""Position lifecycle for Velez intraday Mode 2 with profit protection.

There is no fixed take profit. The original structural stop is active from entry.
When price reaches +1.5R, the full position remains open and the stop is promoted
to +1R. Afterwards the position exits on that protected stop, on a CLOSED 15m
candle crossing EMA20 against the trade, or after 12 hours (48 completed 15m bars).

For deterministic recovery from closed candles, a protection level reached inside
a candle becomes executable from the following candle. Live monitoring can arm it
immediately when the Kraken price reaches +1.5R.
"""
from __future__ import annotations

import json
from typing import Any

import pandas as pd

from project.relative_strength_monitor import RelativeStrengthMonitor
from project.shared.events import Event, EventType


class Velez15mMonitor(RelativeStrengthMonitor):
    MAX_HOLD_CANDLES = 48
    PROTECTION_TRIGGER_R = 1.5
    PROTECTED_STOP_R = 1.0

    @staticmethod
    def _levels(signal: dict[str, Any], direction: str) -> tuple[float, float, float, float]:
        entry = float(signal["entry"])
        hard_stop = float(signal["stop_loss"])
        risk = abs(entry - hard_stop)
        trigger = entry + 1.5 * risk if direction == "LONG" else entry - 1.5 * risk
        protected_stop = entry + 1.0 * risk if direction == "LONG" else entry - 1.0 * risk
        return entry, hard_stop, trigger, protected_stop

    def _all_active_signals(self) -> list[dict[str, Any]]:
        """Load every delivered NEW/OPEN signal, regardless of legacy runtime version."""
        rows = self.postgres.fetch_all(
            """SELECT id, strategy, pair, timeframe, entry, stop_loss, take_profit,
                      reference_candle_time, created_at, telegram_sent_at,
                      COALESCE(exchange, 'Kraken'), quantity,
                      COALESCE(estimated_buy_fee_eur, 0),
                      COALESCE(score_breakdown, '{}'::jsonb)
               FROM signals.generated_signals
               WHERE status IN ('NEW','OPEN')
                 AND telegram_sent_at IS NOT NULL
               ORDER BY created_at ASC"""
        )
        result: list[dict[str, Any]] = []
        for row in rows:
            state = row[13]
            if isinstance(state, str):
                try:
                    state = json.loads(state)
                except json.JSONDecodeError:
                    state = {}
            result.append(
                {
                    "id": int(row[0]),
                    "strategy": str(row[1]),
                    "pair": str(row[2]),
                    "timeframe": str(row[3]),
                    "entry": float(row[4]),
                    "stop_loss": float(row[5]),
                    "take_profit": float(row[6] or 0.0),
                    "reference_candle_time": row[7],
                    "created_at": row[8],
                    "telegram_sent_at": row[9],
                    "exchange": str(row[10]),
                    "quantity": float(row[11] or 0.0),
                    "estimated_buy_fee_eur": float(row[12] or 0.0),
                    "state": dict(state or {}),
                }
            )
        return result

    def _historical_stop_row(self, signal: dict[str, Any]) -> dict[str, Any] | None:
        """Return the first completed 15m candle after entry that touched the stored hard stop."""
        exchange = str(signal.get("exchange") or "Kraken")
        direction = str(signal["state"].get("direction") or "LONG").upper()
        stop = float(signal["stop_loss"])
        reference = self._utc(
            signal.get("reference_candle_time")
            or signal.get("telegram_sent_at")
            or signal.get("created_at")
        ).floor("15min")
        comparator = "low <= %s" if direction == "LONG" else "high >= %s"
        rows = self.postgres.fetch_all(
            f"""SELECT timestamp, open, high, low, close
                  FROM market_data.ohlc
                 WHERE LOWER(TRIM(exchange)) = LOWER(TRIM(%s))
                   AND pair = %s
                   AND timeframe = '15m'
                   AND timestamp > %s
                   AND {comparator}
                 ORDER BY timestamp ASC
                 LIMIT 1""",
            (exchange, signal["pair"], reference.to_pydatetime(), stop),
        )
        if not rows:
            return None
        ts, open_, high, low, close = rows[0]
        # Never use a still-forming candle for historical reconciliation.
        timestamp = self._utc(ts)
        if timestamp + pd.Timedelta(minutes=15) > pd.Timestamp.now(tz="UTC"):
            return None
        return {
            "timestamp": timestamp,
            "open": float(open_),
            "high": float(high),
            "low": float(low),
            "close": float(close),
        }

    def reconcile_all_active_hard_stops(self) -> int:
        """One-shot startup audit: close every active signal whose stored stop has been reached."""
        active = self._all_active_signals()
        closed = 0
        self.logger.info(
            "VELEZ_STARTUP_STOP_AUDIT active=%s ids=%s",
            len(active),
            [signal["id"] for signal in active],
        )
        for signal in active:
            try:
                direction = str(signal["state"].get("direction") or "LONG").upper()
                stop = float(signal["stop_loss"])

                # Current Kraken price catches a stop that has been reached since the latest closed candle.
                if str(signal.get("exchange") or "Kraken").lower() == "kraken":
                    live = self._live_price(signal["pair"])
                    live_hit = live is not None and (
                        live <= stop if direction == "LONG" else live >= stop
                    )
                    if live_hit:
                        self.logger.warning(
                            "VELEZ_STARTUP_STOP_HIT_LIVE id=%s pair=%s direction=%s live=%.8f stop=%.8f",
                            signal["id"], signal["pair"], direction, float(live), stop,
                        )
                        if self._close(signal, stop, self._live_row(float(live)), "VELEZ_STARTUP_HARD_STOP_RECONCILIATION"):
                            closed += 1
                        continue

                row = self._historical_stop_row(signal)
                if row is not None:
                    self.logger.warning(
                        "VELEZ_STARTUP_STOP_HIT_HISTORY id=%s pair=%s direction=%s candle=%s high=%.8f low=%.8f stop=%.8f",
                        signal["id"], signal["pair"], direction, row["timestamp"],
                        row["high"], row["low"], stop,
                    )
                    if self._close(signal, stop, row, "VELEZ_STARTUP_HARD_STOP_RECONCILIATION"):
                        closed += 1
                else:
                    self.logger.info(
                        "VELEZ_STARTUP_STOP_CLEAR id=%s pair=%s direction=%s stop=%.8f",
                        signal["id"], signal["pair"], direction, stop,
                    )
            except Exception:
                self.logger.exception(
                    "VELEZ_STARTUP_STOP_AUDIT_FAILED id=%s pair=%s",
                    signal.get("id"), signal.get("pair"),
                )
        self.logger.info(
            "VELEZ_STARTUP_STOP_AUDIT_DONE active=%s closed=%s",
            len(active), closed,
        )
        return closed

    def _persist_protection(self, signal: dict[str, Any], trigger: float, protected_stop: float) -> None:
        state = signal["state"]
        if bool(state.get("profit_protection_armed")):
            return
        patch = {
            "profit_protection_armed": True,
            "profit_protection_trigger_price": trigger,
            "profit_protected_stop_price": protected_stop,
            "profit_protection_trigger_r": self.PROTECTION_TRIGGER_R,
            "profit_protection_stop_r": self.PROTECTED_STOP_R,
        }
        self.postgres.execute(
            """UPDATE signals.generated_signals
               SET score_breakdown=COALESCE(score_breakdown,'{}'::jsonb)||%s::jsonb
               WHERE id=%s AND status IN ('NEW','OPEN')""",
            (json.dumps(patch), signal["id"]),
        )
        state.update(patch)
        direction = str(state.get("direction", "LONG"))
        message = (
            "🛡️ <b>PROTEZIONE PROFITTO ATTIVATA · PAPER</b>\n"
            f"🆔 <code>#{signal['id']}</code>\n"
            f"<b>{signal['pair']}</b> · <b>{direction}</b> · 15m\n\n"
            f"Raggiunto: <b>+1,5R</b> ({trigger:.8f})\n"
            f"Nuovo stop protetto: <b>+1R</b> ({protected_stop:.8f})\n\n"
            "La posizione resta aperta. Uscita su stop protetto, chiusura 15m contro EMA20 o limite 12 ore."
        )
        self.event_bus.publish(
            Event(
                EventType.REPORT_READY,
                {
                    "message": message,
                    "trusted_html": True,
                    "signal_id": signal["id"],
                    "pair": signal["pair"],
                    "timeframe": "15m",
                    "management_update": "VELEZ_PROFIT_PROTECTION_ARMED",
                },
            )
        )
        self.logger.info(
            "VELEZ_PROTECTION_ARMED id=%s pair=%s trigger=%.8f protected_stop=%.8f",
            signal["id"], signal["pair"], trigger, protected_stop,
        )

    def _check_live_levels(self, signal: dict[str, Any]) -> bool:
        if str(signal.get("exchange") or "Kraken").lower() != "kraken":
            return False
        price = self._live_price(signal["pair"])
        if price is None:
            return False

        direction = str(signal["state"].get("direction", "LONG"))
        _, hard_stop, trigger, protected_stop = self._levels(signal, direction)
        armed = bool(signal["state"].get("profit_protection_armed"))
        active_stop = protected_stop if armed else hard_stop

        self.logger.info(
            "VELEZ_MONITOR_LIVE id=%s pair=%s price=%.8f active_stop=%.8f trigger=%.8f armed=%s direction=%s",
            signal["id"], signal["pair"], price, active_stop, trigger, armed, direction,
        )

        stop_hit = price <= active_stop if direction == "LONG" else price >= active_stop
        if stop_hit:
            reason = "VELEZ_PROTECTED_STOP" if armed else "VELEZ_HARD_STOP"
            return self._close(signal, active_stop, self._live_row(price), reason)

        trigger_hit = price >= trigger if direction == "LONG" else price <= trigger
        if trigger_hit and not armed:
            self._persist_protection(signal, trigger, protected_stop)
        return False

    def _check(self, signal: dict[str, Any]) -> bool:
        if self._check_live_levels(signal):
            return True

        exchange = str(signal.get("exchange") or "Kraken")
        frame = self._frame(signal["pair"], exchange)
        if frame.empty:
            self.logger.warning(
                "VELEZ_MONITOR_NO_FRAME id=%s pair=%s exchange=%s",
                signal["id"], signal["pair"], exchange,
            )
            return False

        frame = frame.copy()
        frame["ema20"] = frame["close"].ewm(span=20, adjust=False).mean()

        direction = str(signal["state"].get("direction", "LONG"))
        reference = self._utc(
            signal.get("reference_candle_time")
            or signal.get("telegram_sent_at")
            or signal.get("created_at")
        )
        eligible = frame[frame["timestamp"] > reference.floor("15min")].copy()
        if eligible.empty:
            return False

        _, hard_stop, trigger, protected_stop = self._levels(signal, direction)
        armed = bool(signal["state"].get("profit_protection_armed"))
        max_hold = int(signal["state"].get("max_hold_candles") or self.MAX_HOLD_CANDLES)
        max_hold = max(1, min(max_hold, self.MAX_HOLD_CANDLES))

        self.logger.info(
            "VELEZ_MONITOR_START id=%s pair=%s direction=%s hard_stop=%.8f trigger=%.8f protected_stop=%.8f armed=%s candles=%s max_hold=%s",
            signal["id"], signal["pair"], direction, hard_stop, trigger, protected_stop,
            armed, len(eligible), max_hold,
        )

        for index, (_, row) in enumerate(eligible.iterrows(), start=1):
            high = float(row["high"])
            low = float(row["low"])
            close = float(row["close"])
            ema20 = float(row["ema20"])
            active_stop = protected_stop if armed else hard_stop
            stop_hit = low <= active_stop if direction == "LONG" else high >= active_stop
            ema_exit = close < ema20 if direction == "LONG" else close > ema20

            self.logger.info(
                "VELEZ_MONITOR_CANDLE id=%s n=%s candle=%s high=%.8f low=%.8f close=%.8f ema20=%.8f active_stop=%.8f armed=%s stop_hit=%s ema_exit=%s",
                signal["id"], index, row["timestamp"], high, low, close, ema20,
                active_stop, armed, stop_hit, ema_exit,
            )

            if stop_hit:
                reason = "VELEZ_PROTECTED_STOP" if armed else "VELEZ_HARD_STOP"
                return self._close(signal, active_stop, row, reason)

            if ema_exit:
                return self._close(signal, close, row, "VELEZ_EMA20_EXIT")

            if not armed:
                trigger_hit = high >= trigger if direction == "LONG" else low <= trigger
                if trigger_hit:
                    # Protection becomes active after this completed candle to avoid lookahead.
                    armed = True
                    self._persist_protection(signal, trigger, protected_stop)

            if index >= max_hold:
                return self._close(signal, close, row, "VELEZ_12H_TIME_EXIT")

        return False
