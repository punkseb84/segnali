"""PAPER position monitor for the BTC-relative-strength strategy."""
from __future__ import annotations

import json
from typing import Any

import pandas as pd

from project.daily_paper_position_monitor import DailyPaperPositionMonitor
from project.relative_strength_scanner import RUNTIME_VERSION
from project.shared.events import Event, EventType


class RelativeStrengthMonitor(DailyPaperPositionMonitor):
    def __init__(self, *args: Any, sell_fee_rate: float = 0.001, spread_rate: float = 0.0005, slippage_rate: float = 0.0, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.sell_fee_rate = max(0.0, float(sell_fee_rate))
        self.spread_rate = max(0.0, float(spread_rate))
        self.slippage_rate = max(0.0, float(slippage_rate))

    def monitor_open_signals(self) -> int:
        closed = 0
        for signal in self._open_signals():
            closed += 1 if self._check(signal) else 0
        return closed

    def _open_signals(self) -> list[dict[str, Any]]:
        rows = self.postgres.fetch_all(
            """SELECT id, strategy, pair, timeframe, entry, stop_loss, take_profit,
                      reference_candle_time, created_at, telegram_sent_at,
                      COALESCE(exchange, 'Kraken'), quantity,
                      COALESCE(estimated_buy_fee_eur, 0), COALESCE(score_breakdown, '{}'::jsonb)
               FROM signals.generated_signals
               WHERE status IN ('NEW', 'OPEN') AND telegram_sent_at IS NOT NULL
                 AND score_breakdown->>'runtime_version' = %s
               ORDER BY created_at ASC LIMIT %s""",
            (RUNTIME_VERSION, self.max_signals_per_cycle),
        )
        result: list[dict[str, Any]] = []
        for row in rows:
            state = row[13]
            if isinstance(state, str):
                try:
                    state = json.loads(state)
                except json.JSONDecodeError:
                    state = {}
            result.append({
                "id": int(row[0]), "strategy": str(row[1]), "pair": str(row[2]),
                "timeframe": str(row[3]), "entry": float(row[4]), "stop_loss": float(row[5]),
                "take_profit": float(row[6]), "reference_candle_time": row[7],
                "created_at": row[8], "telegram_sent_at": row[9], "exchange": str(row[10]),
                "quantity": float(row[11] or 0.0), "estimated_buy_fee_eur": float(row[12] or 0.0),
                "state": dict(state or {}),
            })
        return result

    def _frame(self, pair: str) -> pd.DataFrame:
        rows = self.postgres.fetch_all(
            """SELECT timestamp, open, high, low, close FROM market_data.ohlc
               WHERE pair = %s AND timeframe = '15m' ORDER BY timestamp DESC LIMIT 160""",
            (pair,),
        )
        frame = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close"])
        if frame.empty:
            return frame
        frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
        for column in ["open", "high", "low", "close"]:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
        end = frame["timestamp"].astype("int64") // 1_000_000_000 + 900
        return frame[end <= pd.Timestamp.now(tz="UTC").timestamp()].sort_values("timestamp").reset_index(drop=True)

    @staticmethod
    def _utc(value: Any) -> pd.Timestamp:
        ts = pd.Timestamp(value)
        return ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")

    def _leg_net(self, signal: dict[str, Any], exit_price: float) -> float:
        direction = str(signal["state"].get("direction", "LONG"))
        quantity = float(signal["quantity"])
        entry = float(signal["entry"])
        gross = quantity * ((exit_price - entry) if direction == "LONG" else (entry - exit_price))
        close_fee = quantity * exit_price * self.sell_fee_rate
        friction = quantity * (entry + exit_price) * ((self.spread_rate / 2.0) + self.slippage_rate)
        return gross - float(signal["estimated_buy_fee_eur"]) - close_fee - friction

    def _relative_return(self, pair: str, candles: int = 4) -> float | None:
        asset = self._frame(pair)
        btc = self._frame("BTC/USD")
        if len(asset) <= candles or len(btc) <= candles:
            return None
        asset_ret = float(asset["close"].iloc[-1] / asset["close"].iloc[-1 - candles] - 1.0)
        btc_ret = float(btc["close"].iloc[-1] / btc["close"].iloc[-1 - candles] - 1.0)
        return asset_ret - btc_ret

    def _check(self, signal: dict[str, Any]) -> bool:
        frame = self._frame(signal["pair"])
        if frame.empty:
            return False
        direction = str(signal["state"].get("direction", "LONG"))
        delivery = self._utc(signal.get("telegram_sent_at") or signal.get("created_at") or signal.get("reference_candle_time"))
        start = delivery.floor("15min")
        eligible = frame[frame["timestamp"] >= start]
        if eligible.empty:
            return False
        max_hold = int(signal["state"].get("max_hold_candles", 8))
        for index, row in eligible.iterrows():
            high, low, close = float(row["high"]), float(row["low"]), float(row["close"])
            stop, target = float(signal["stop_loss"]), float(signal["take_profit"])
            stop_hit = low <= stop if direction == "LONG" else high >= stop
            target_hit = high >= target if direction == "LONG" else low <= target
            if stop_hit:
                return self._close(signal, stop, row, "RELATIVE_STRENGTH_STOP")
            if target_hit:
                return self._close(signal, target, row, "RELATIVE_STRENGTH_TARGET")
        held = len(eligible)
        relative = self._relative_return(signal["pair"])
        reversal = relative is not None and ((direction == "LONG" and relative < 0) or (direction == "SHORT" and relative > 0))
        if held >= max_hold or reversal:
            last = eligible.iloc[-1]
            reason = "RELATIVE_STRENGTH_REVERSAL" if reversal else "RELATIVE_STRENGTH_TIME_EXIT"
            return self._close(signal, float(last["close"]), last, reason)
        return False

    def _close(self, signal: dict[str, Any], price: float, row: Any, resolution: str) -> bool:
        result = self._leg_net(signal, price)
        status = "TARGET_HIT" if result >= 0 else "STOP_LOSS"
        net_profit, net_loss = max(result, 0.0), max(-result, 0.0)
        self.postgres.execute(
            """UPDATE signals.generated_signals
               SET status = %s, outcome_price = %s, outcome_open_price = %s,
                   outcome_high_price = %s, outcome_low_price = %s, outcome_close_price = %s,
                   outcome_candle_time = %s, closed_at = NOW(), outcome_ambiguous = FALSE,
                   outcome_resolution = %s, net_profit_tp1_eur = %s, net_loss_sl_eur = %s
               WHERE id = %s AND status IN ('NEW', 'OPEN')""",
            (status, price, float(row["open"]), float(row["high"]), float(row["low"]),
             float(row["close"]), row["timestamp"], resolution, net_profit, net_loss, signal["id"]),
        )
        event_type = EventType.TARGET_HIT if status == "TARGET_HIT" else EventType.STOP_LOSS
        payload = {
            **signal, "signal_id": signal["id"], "direction": signal["state"].get("direction", "LONG"),
            "outcome": status, "outcome_price": price, "close_price": float(row["close"]),
            "closed_at": str(row["timestamp"]), "outcome_resolution": resolution,
            "realized_net_eur": result, "net_profit_tp1_eur": net_profit, "net_loss_sl_eur": net_loss,
        }
        self.event_bus.publish(Event(event_type, payload))
        return True
