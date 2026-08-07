"""Automatic PAPER lifecycle for relative-strength positions."""
from __future__ import annotations

import json
import os
from typing import Any

import pandas as pd
import requests

from project.daily_paper_position_monitor import DailyPaperPositionMonitor
from project.data_collector.kraken_client import kraken_pair
from project.relative_strength_scanner import RUNTIME_VERSION
from project.shared.events import Event, EventType


class RelativeStrengthMonitor(DailyPaperPositionMonitor):
    def __init__(self, *args: Any, sell_fee_rate: float = 0.001,
                 spread_rate: float = 0.0005, slippage_rate: float = 0.0,
                 **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.sell_fee_rate = max(0.0, float(sell_fee_rate))
        self.spread_rate = max(0.0, float(spread_rate))
        self.slippage_rate = max(0.0, float(slippage_rate))
        self.initial_budget = float(os.getenv("PAPER_INITIAL_BUDGET_EUR", "100"))
        self.kraken_api_base = os.getenv("KRAKEN_API_BASE", "https://api.kraken.com/0/public").rstrip("/")

    def monitor_open_signals(self) -> int:
        signals = self._open_signals()
        self.logger.info(
            "RS_MONITOR_CYCLE runtime=%s open_signals=%s ids=%s",
            RUNTIME_VERSION,
            len(signals),
            [signal["id"] for signal in signals],
        )
        closed = 0
        for signal in signals:
            try:
                closed += 1 if self._check(signal) else 0
            except Exception:
                self.logger.exception(
                    "RS_MONITOR_SIGNAL_FAILED id=%s pair=%s",
                    signal.get("id"),
                    signal.get("pair"),
                )
        self.logger.info("RS_MONITOR_CYCLE_END closed=%s", closed)
        return closed

    def _open_signals(self) -> list[dict[str, Any]]:
        rows = self.postgres.fetch_all(
            """SELECT id, strategy, pair, timeframe, entry, stop_loss, take_profit,
                      reference_candle_time, created_at, telegram_sent_at,
                      COALESCE(exchange, 'Kraken'), quantity,
                      COALESCE(estimated_buy_fee_eur, 0),
                      COALESCE(score_breakdown, '{}'::jsonb)
               FROM signals.generated_signals
               WHERE status IN ('NEW', 'OPEN')
                 AND telegram_sent_at IS NOT NULL
                 AND score_breakdown->>'runtime_version' = %s
               ORDER BY created_at ASC LIMIT %s""",
            (RUNTIME_VERSION, self.max_signals_per_cycle),
        )
        result = []
        for row in rows:
            state = row[13]
            if isinstance(state, str):
                try:
                    state = json.loads(state)
                except json.JSONDecodeError:
                    state = {}
            result.append({
                "id": int(row[0]), "strategy": str(row[1]), "pair": str(row[2]),
                "timeframe": str(row[3]), "entry": float(row[4]),
                "stop_loss": float(row[5]), "take_profit": float(row[6]),
                "reference_candle_time": row[7], "created_at": row[8],
                "telegram_sent_at": row[9], "exchange": str(row[10]),
                "quantity": float(row[11] or 0.0),
                "estimated_buy_fee_eur": float(row[12] or 0.0),
                "state": dict(state or {}),
            })
        return result

    def _frame(self, pair: str, exchange: str = "Kraken") -> pd.DataFrame:
        rows = self.postgres.fetch_all(
            """SELECT timestamp, open, high, low, close
               FROM market_data.ohlc
               WHERE LOWER(TRIM(exchange)) = LOWER(TRIM(%s))
                 AND pair = %s AND timeframe = '15m'
               ORDER BY timestamp DESC LIMIT 160""",
            (exchange, pair),
        )
        frame = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close"])
        if frame.empty:
            return frame
        frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
        for column in ["open", "high", "low", "close"]:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
        end = frame["timestamp"].astype("int64") // 1_000_000_000 + 900
        return frame[end <= pd.Timestamp.now(tz="UTC").timestamp()].sort_values("timestamp").reset_index(drop=True)

    def _live_price(self, pair: str) -> float | None:
        """Fetch Kraken last-trade price when the normal monitor cycle runs."""
        try:
            response = requests.get(
                f"{self.kraken_api_base}/Ticker",
                params={"pair": kraken_pair(pair)},
                timeout=8,
            )
            response.raise_for_status()
            payload = response.json()
            if payload.get("error"):
                self.logger.warning("RS_LIVE_PRICE kraken_error pair=%s error=%s", pair, payload.get("error"))
                return None
            result = payload.get("result") or {}
            ticker = next(iter(result.values()), None)
            if not ticker:
                return None
            return float(ticker["c"][0])
        except Exception as exc:
            self.logger.warning("RS_LIVE_PRICE failed pair=%s error=%s", pair, exc)
            return None

    @staticmethod
    def _live_row(price: float) -> dict[str, Any]:
        now = pd.Timestamp.now(tz="UTC")
        return {"timestamp": now, "open": price, "high": price, "low": price, "close": price}

    def _check_live_levels(self, signal: dict[str, Any]) -> bool:
        if str(signal.get("exchange") or "Kraken").lower() != "kraken":
            return False
        price = self._live_price(signal["pair"])
        if price is None:
            return False
        direction = str(signal["state"].get("direction", "LONG"))
        stop = float(signal["stop_loss"])
        target = float(signal["take_profit"])
        self.logger.info(
            "RS_MONITOR_LIVE id=%s pair=%s price=%.8f stop=%.8f target=%.8f direction=%s",
            signal["id"], signal["pair"], price, stop, target, direction,
        )
        if direction == "LONG":
            if price <= stop:
                return self._close(signal, stop, self._live_row(price), "RELATIVE_STRENGTH_LIVE_STOP")
            if price >= target:
                return self._close(signal, target, self._live_row(price), "RELATIVE_STRENGTH_LIVE_TARGET")
        else:
            if price >= stop:
                return self._close(signal, stop, self._live_row(price), "RELATIVE_STRENGTH_LIVE_STOP")
            if price <= target:
                return self._close(signal, target, self._live_row(price), "RELATIVE_STRENGTH_LIVE_TARGET")
        return False

    @staticmethod
    def _utc(value: Any) -> pd.Timestamp:
        ts = pd.Timestamp(value)
        return ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")

    def _costs(self, signal: dict[str, Any], exit_price: float) -> dict[str, float]:
        quantity = float(signal["quantity"])
        entry = float(signal["entry"])
        entry_fee = float(signal["estimated_buy_fee_eur"])
        exit_fee = quantity * exit_price * self.sell_fee_rate
        spread = quantity * (entry + exit_price) * (self.spread_rate / 2.0)
        slippage = quantity * (entry + exit_price) * self.slippage_rate
        return {
            "entry_fee_eur": entry_fee,
            "exit_fee_eur": exit_fee,
            "spread_cost_eur": spread,
            "slippage_cost_eur": slippage,
            "total_costs_eur": entry_fee + exit_fee + spread + slippage,
        }

    def _pnl(self, signal: dict[str, Any], exit_price: float) -> dict[str, float]:
        direction = str(signal["state"].get("direction", "LONG"))
        quantity = float(signal["quantity"])
        entry = float(signal["entry"])
        gross = quantity * ((exit_price - entry) if direction == "LONG" else (entry - exit_price))
        costs = self._costs(signal, exit_price)
        return {"gross_pnl_eur": gross, "net_pnl_eur": gross - costs["total_costs_eur"], **costs}

    def _budget_before(self) -> float:
        rows = self.postgres.fetch_all(
            """SELECT COALESCE(SUM(COALESCE(net_profit_tp1_eur,0)-COALESCE(net_loss_sl_eur,0)),0)
               FROM signals.generated_signals
               WHERE score_breakdown->>'runtime_version' = %s
                 AND closed_at IS NOT NULL""",
            (RUNTIME_VERSION,),
        )
        cumulative = float(rows[0][0] or 0.0) if rows else 0.0
        return self.initial_budget + cumulative

    def _relative_return(self, pair: str, exchange: str = "Kraken", candles: int = 4) -> float | None:
        asset, btc = self._frame(pair, exchange), self._frame("BTC/USD", exchange)
        if len(asset) <= candles or len(btc) <= candles:
            return None
        return float(asset["close"].iloc[-1] / asset["close"].iloc[-1-candles] - 1.0) - float(btc["close"].iloc[-1] / btc["close"].iloc[-1-candles] - 1.0)

    def _check(self, signal: dict[str, Any]) -> bool:
        if self._check_live_levels(signal):
            return True
        exchange = str(signal.get("exchange") or "Kraken")
        frame = self._frame(signal["pair"], exchange)
        if frame.empty:
            self.logger.warning("RS_MONITOR_NO_FRAME id=%s pair=%s exchange=%s", signal["id"], signal["pair"], exchange)
            return False
        direction = str(signal["state"].get("direction", "LONG"))
        delivery = self._utc(signal.get("telegram_sent_at") or signal.get("created_at") or signal.get("reference_candle_time"))
        eligible = frame[frame["timestamp"] >= delivery.floor("15min")]
        if eligible.empty:
            self.logger.warning("RS_MONITOR_NO_ELIGIBLE id=%s pair=%s delivery=%s", signal["id"], signal["pair"], delivery)
            return False

        stop, target = float(signal["stop_loss"]), float(signal["take_profit"])
        self.logger.info(
            "RS_MONITOR_CHECK id=%s pair=%s exchange=%s direction=%s entry=%.8f stop=%.8f target=%.8f candles=%s first=%s last=%s max_high=%.8f min_low=%.8f",
            signal["id"], signal["pair"], exchange, direction, float(signal["entry"]), stop, target,
            len(eligible), eligible.iloc[0]["timestamp"], eligible.iloc[-1]["timestamp"],
            float(eligible["high"].max()), float(eligible["low"].min()),
        )
        for _, row in eligible.iterrows():
            high, low = float(row["high"]), float(row["low"])
            stop_hit = low <= stop if direction == "LONG" else high >= stop
            target_hit = high >= target if direction == "LONG" else low <= target
            self.logger.info(
                "RS_MONITOR_CANDLE id=%s pair=%s candle=%s high=%.8f low=%.8f stop_hit=%s target_hit=%s",
                signal["id"], signal["pair"], row["timestamp"], high, low, stop_hit, target_hit,
            )
            if stop_hit and target_hit:
                return self._close(signal, float(row["close"]), row, "RELATIVE_STRENGTH_AMBIGUOUS_BOTH_HIT")
            if stop_hit:
                return self._close(signal, stop, row, "RELATIVE_STRENGTH_STOP")
            if target_hit:
                return self._close(signal, target, row, "RELATIVE_STRENGTH_TARGET")

        relative = self._relative_return(signal["pair"], exchange)
        reversal = relative is not None and ((direction == "LONG" and relative < 0) or (direction == "SHORT" and relative > 0))
        max_hold = int(signal["state"].get("max_hold_candles", 8))
        if reversal or len(eligible) >= max_hold:
            last = eligible.iloc[-1]
            return self._close(signal, float(last["close"]), last,
                               "RELATIVE_STRENGTH_REVERSAL" if reversal else "RELATIVE_STRENGTH_TIME_EXIT")
        return False

    def _close(self, signal: dict[str, Any], price: float, row: Any, resolution: str) -> bool:
        pnl = self._pnl(signal, price)
        result = pnl["net_pnl_eur"]
        status = "TARGET_HIT" if result >= 0 else "STOP_LOSS"
        net_profit, net_loss = max(result, 0.0), max(-result, 0.0)
        budget_before = self._budget_before()
        budget_after = budget_before + result
        patch = {
            "gross_pnl_eur": pnl["gross_pnl_eur"],
            "entry_fee_eur": pnl["entry_fee_eur"],
            "exit_fee_eur": pnl["exit_fee_eur"],
            "spread_cost_eur": pnl["spread_cost_eur"],
            "slippage_cost_eur": pnl["slippage_cost_eur"],
            "total_costs_eur": pnl["total_costs_eur"],
            "budget_before_eur": budget_before,
            "budget_after_eur": budget_after,
        }
        self.logger.info(
            "RS_MONITOR_CLOSE id=%s pair=%s resolution=%s outcome=%s price=%.8f net=%.4f budget_before=%.4f budget_after=%.4f",
            signal["id"], signal["pair"], resolution, status, price, result, budget_before, budget_after,
        )
        self.postgres.execute(
            """UPDATE signals.generated_signals
               SET status=%s, outcome_price=%s, outcome_open_price=%s,
                   outcome_high_price=%s, outcome_low_price=%s, outcome_close_price=%s,
                   outcome_candle_time=%s, closed_at=NOW(), outcome_ambiguous=%s,
                   outcome_resolution=%s, net_profit_tp1_eur=%s, net_loss_sl_eur=%s,
                   score_breakdown=COALESCE(score_breakdown,'{}'::jsonb)||%s::jsonb
               WHERE id=%s AND status IN ('NEW','OPEN')""",
            (status, price, float(row["open"]), float(row["high"]), float(row["low"]),
             float(row["close"]), row["timestamp"], resolution == "RELATIVE_STRENGTH_AMBIGUOUS_BOTH_HIT",
             resolution, net_profit, net_loss, json.dumps(patch), signal["id"]),
        )
        payload = {**signal, **patch, "signal_id": signal["id"],
                   "direction": signal["state"].get("direction", "LONG"),
                   "outcome": status, "outcome_price": price,
                   "close_price": float(row["close"]), "closed_at": str(row["timestamp"]),
                   "outcome_resolution": resolution, "realized_net_eur": result,
                   "net_profit_tp1_eur": net_profit, "net_loss_sl_eur": net_loss}
        self.event_bus.publish(Event(EventType.TARGET_HIT if status == "TARGET_HIT" else EventType.STOP_LOSS, payload))
        return True
