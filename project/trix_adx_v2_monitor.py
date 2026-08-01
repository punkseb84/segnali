"""Direction-aware PAPER management for TRIX V2 LONG and SHORT positions."""
from __future__ import annotations

import json
from typing import Any

import pandas as pd

from project.notifying_trix_adx_outcome_monitor import NotifyingTrixAdxOutcomeMonitor
from project.shared.events import Event, EventType
from project.trix_adx_v2_scanner import RUNTIME_VERSION


class TrixAdxV2Monitor(NotifyingTrixAdxOutcomeMonitor):
    def monitor_open_signals(self) -> int:
        signals = self.fetch_open_signals()
        closed = 0
        for signal in signals:
            closed += 1 if self.check_signal(signal) else 0
        return closed

    def fetch_open_signals(self) -> list[dict[str, Any]]:
        rows = self.postgres.fetch_all(
            """SELECT id, strategy, pair, timeframe, regime, entry, stop_loss, take_profit,
                      score, probability, signal_class, net_profit_tp1_eur, net_loss_sl_eur,
                      net_rr, reference_candle_time, created_at, telegram_sent_at,
                      COALESCE(exchange, 'Kraken'), quantity, COALESCE(estimated_buy_fee_eur, 0)
               FROM signals.generated_signals
               WHERE status IN ('NEW', 'OPEN') AND telegram_sent_at IS NOT NULL
                 AND score_breakdown->>'runtime_version' = %s
               ORDER BY created_at ASC LIMIT %s""",
            (RUNTIME_VERSION, self.max_signals_per_cycle),
        )
        keys = ["id","strategy","pair","timeframe","regime","entry","stop_loss","take_profit","score","probability","signal_class","net_profit_tp1_eur","net_loss_sl_eur","net_rr","reference_candle_time","created_at","telegram_sent_at","exchange","quantity","estimated_buy_fee_eur"]
        result = []
        for row in rows:
            item = dict(zip(keys, row))
            for key in ["entry","stop_loss","take_profit","score","net_profit_tp1_eur","net_loss_sl_eur","net_rr","quantity","estimated_buy_fee_eur"]:
                item[key] = float(item[key] or 0.0)
            item["id"] = int(item["id"])
            result.append(item)
        return result

    @staticmethod
    def _direction(state: dict[str, Any]) -> str:
        return "SHORT" if str(state.get("direction", "LONG")).upper() == "SHORT" else "LONG"

    def _leg_net(self, signal: dict[str, Any], exit_price: float, fraction: float) -> float:
        state = self._load_state(signal["id"])
        direction = self._direction(state)
        quantity = float(signal.get("quantity") or 0.0) * min(1.0, max(0.0, fraction))
        entry = float(signal["entry"])
        gross = quantity * ((exit_price - entry) if direction == "LONG" else (entry - exit_price))
        open_fee = float(signal.get("estimated_buy_fee_eur") or 0.0) * fraction
        close_fee = quantity * exit_price * self.sell_fee_rate
        friction = quantity * (entry + exit_price) * ((self.spread_rate / 2.0) + self.slippage_rate)
        return gross - open_fee - close_fee - friction

    def true_break_even_price(self, signal: dict[str, Any]) -> float:
        state = self._load_state(signal["id"])
        direction = self._direction(state)
        entry = float(signal["entry"])
        total_rate = self.sell_fee_rate + self.spread_rate + (2.0 * self.slippage_rate) + self.break_even_buffer_rate
        return entry * (1.0 + total_rate) if direction == "LONG" else entry * (1.0 - total_rate)

    def _arm_break_even(self, signal: dict[str, Any], state: dict[str, Any], timestamp: Any) -> None:
        direction = self._direction(state)
        entry = float(signal["entry"])
        risk = float(state.get("risk_distance") or abs(entry - float(state.get("initial_stop") or signal["stop_loss"])))
        trigger = entry + risk if direction == "LONG" else entry - risk
        break_even = self.true_break_even_price(signal)
        patch = {"breakeven_armed": True, "breakeven_price": break_even, "breakeven_armed_at": str(timestamp)}
        self._patch_state(signal["id"], patch, stop_loss=break_even, status="OPEN")
        signal["stop_loss"] = break_even
        state.update(patch)
        self.event_bus.publish(Event(EventType.REPORT_READY, {"message": f"🟡 <b>BREAK EVEN NETTO · PAPER</b> <code>#{signal['id']}</code>\n<b>{signal['pair']}</b> · {direction} · 15m\nStop spostato a: <b>{break_even:.8f}</b>", "trusted_html": True, "signal_id": signal["id"]}))

    def _take_partial_profit(self, signal: dict[str, Any], state: dict[str, Any], tp1_price: float, timestamp: Any, open_price: float, high_price: float, low_price: float, close_price: float) -> None:
        direction = self._direction(state)
        partial_net = self._leg_net(signal, tp1_price, 0.5)
        break_even = float(state.get("breakeven_price") or self.true_break_even_price(signal))
        patch = {"tp1_hit": True, "tp1_price": tp1_price, "tp1_fraction": 0.5, "tp1_realized_net_eur": partial_net, "tp1_hit_at": str(timestamp), "remaining_fraction": 0.5}
        patch["highest_high_since_tp1" if direction == "LONG" else "lowest_low_since_tp1"] = high_price if direction == "LONG" else low_price
        new_stop = max(float(signal["stop_loss"]), break_even) if direction == "LONG" else min(float(signal["stop_loss"]), break_even)
        self._patch_state(signal["id"], patch, stop_loss=new_stop, status="OPEN")
        signal["stop_loss"] = new_stop
        state.update(patch)
        payload = {**signal, "signal_id": signal["id"], "direction": direction, "outcome": "TARGET_HIT", "outcome_price": tp1_price, "outcome_open_price": open_price, "outcome_high_price": high_price, "outcome_low_price": low_price, "close_price": close_price, "closed_at": str(timestamp), "outcome_resolution": "TRIX_TP1_2R", "partial_take_profit": True, "partial_fraction": 0.5, "remaining_fraction": 0.5, "realized_net_eur": partial_net}
        self.event_bus.publish(Event(EventType.TARGET_HIT, payload))

    def _fetch_closed_frame(self, signal: dict[str, Any]) -> pd.DataFrame:
        rows = self.postgres.fetch_all(
            """SELECT timestamp, open, high, low, close FROM market_data.ohlc
               WHERE pair = %s AND timeframe = '15m' ORDER BY timestamp DESC LIMIT 500""",
            (signal["pair"],),
        )
        frame = pd.DataFrame(rows, columns=["timestamp","open","high","low","close"])
        if frame.empty:
            return frame
        frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
        for column in ["open","high","low","close"]:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
        cutoff = pd.Timestamp.now(tz="UTC").timestamp()
        end = frame["timestamp"].astype("int64") // 1_000_000_000 + 900
        return frame[end <= cutoff].sort_values("timestamp").reset_index(drop=True)

    def check_signal(self, signal: dict[str, Any]) -> bool:
        frame = self._fetch_closed_frame(signal)
        if frame.empty:
            return False
        data = self._add_indicators(frame)
        state = self._load_state(signal["id"])
        direction = self._direction(state)
        entry = float(signal["entry"])
        risk = float(state.get("risk_distance") or abs(entry - float(state.get("initial_stop") or signal["stop_loss"])))
        if risk <= 0:
            return False
        delivery = self._to_utc(signal.get("telegram_sent_at") or signal.get("created_at") or signal.get("reference_candle_time"))
        first_open = delivery.floor("15min")
        last_raw = state.get("last_processed_candle_time")
        last_processed = self._to_utc(last_raw) if last_raw else None
        for index in range(1, len(data)):
            latest, previous = data.iloc[index], data.iloc[index - 1]
            timestamp = self._to_utc(latest["timestamp"])
            if timestamp < first_open or (last_processed is not None and timestamp <= last_processed):
                continue
            op, high, low, close = map(float, [latest["open"], latest["high"], latest["low"], latest["close"]])
            stop = float(signal["stop_loss"])
            stop_hit = low <= stop if direction == "LONG" else high >= stop
            if stop_hit:
                resolution = "TRIX_TRAILING_STOP" if state.get("tp1_hit") else "TRIX_NET_BREAK_EVEN_STOP" if state.get("breakeven_armed") else "TRIX_LONG_INITIAL_STOP" if direction == "LONG" else "TRIX_SHORT_INITIAL_STOP"
                return self._close_signal(signal, outcome_price=stop, timestamp=timestamp, open_price=op, high_price=high, low_price=low, close_price=close, resolution=resolution, state=state)
            be_trigger = entry + risk if direction == "LONG" else entry - risk
            tp1 = entry + 2.0 * risk if direction == "LONG" else entry - 2.0 * risk
            be_hit = high >= be_trigger if direction == "LONG" else low <= be_trigger
            tp_hit = high >= tp1 if direction == "LONG" else low <= tp1
            if not state.get("breakeven_armed") and be_hit:
                self._arm_break_even(signal, state, timestamp)
            if not state.get("tp1_hit") and tp_hit:
                self._take_partial_profit(signal, state, tp1, timestamp, op, high, low, close)
            trix_now = float(latest["trix"]) if not pd.isna(latest["trix"]) else 0.0
            trix_prev = float(previous["trix"]) if not pd.isna(previous["trix"]) else trix_now
            ema50 = float(latest["ema50"])
            exit_condition = (trix_prev >= 0 > trix_now or close < ema50) if direction == "LONG" else (trix_prev <= 0 < trix_now or close > ema50)
            if exit_condition:
                return self._close_signal(signal, outcome_price=close, timestamp=timestamp, open_price=op, high_price=high, low_price=low, close_price=close, resolution="TRIX_DIRECTIONAL_EXIT", state=state)
            patch: dict[str, Any] = {}
            if state.get("tp1_hit"):
                atr = float(latest["atr"])
                be = float(state.get("breakeven_price") or self.true_break_even_price(signal))
                if direction == "LONG":
                    extreme = max(float(state.get("highest_high_since_tp1") or high), high)
                    candidate = extreme - atr * self.trailing_atr_multiple
                    new_stop = max(stop, be, candidate)
                    patch["highest_high_since_tp1"] = extreme
                    improve = new_stop > stop
                else:
                    extreme = min(float(state.get("lowest_low_since_tp1") or low), low)
                    candidate = extreme + atr * self.trailing_atr_multiple
                    new_stop = min(stop, be, candidate)
                    patch["lowest_low_since_tp1"] = extreme
                    improve = new_stop < stop
                if improve:
                    patch["trailing_stop"] = new_stop
                    self._patch_state(signal["id"], patch, stop_loss=new_stop, status="OPEN")
                    signal["stop_loss"] = new_stop
                    state.update(patch)
                    patch = {}
            self._mark_processed(signal["id"], timestamp, patch)
            state["last_processed_candle_time"] = str(timestamp)
            last_processed = timestamp
        return False
