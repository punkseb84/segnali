"""TRIX + ADX V2 PAPER scanner: 1h context, 15m LONG and SHORT triggers."""
from __future__ import annotations

import math
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

import pandas as pd

from project.strategy_engine.service import GeneratedSignal
from project.trix_adx_scanner import TrixAdxScanner

STRATEGY_NAME = "TRIX ADX 1H/15M LONG SHORT"
RUNTIME_VERSION = "TRIX_ADX_1H_15M_LS_V2"


class TrixAdxV2Scanner(TrixAdxScanner):
    """Confirm direction on 1h and enter on a closed 15m TRIX zero crossing."""

    def _runtime_count(self, *, open_only: bool = False, today_only: bool = False) -> int:
        clauses = ["score_breakdown->>'runtime_version' = %s"]
        params: list[Any] = [RUNTIME_VERSION]
        if open_only:
            clauses.append("status IN ('NEW', 'OPEN')")
        if today_only:
            clauses.append("(created_at AT TIME ZONE 'Europe/Rome')::date = (NOW() AT TIME ZONE 'Europe/Rome')::date")
            clauses.append("telegram_sent_at IS NOT NULL")
        rows = self.client.fetch_all(
            f"SELECT COUNT(*) FROM signals.generated_signals WHERE {' AND '.join(clauses)}",
            tuple(params),
        )
        return int(rows[0][0] or 0) if rows else 0

    def _full_stops_today(self) -> int:
        rows = self.client.fetch_all(
            """SELECT COUNT(*) FROM signals.generated_signals
               WHERE score_breakdown->>'runtime_version' = %s
                 AND status = 'STOP_LOSS'
                 AND outcome_resolution LIKE 'TRIX_%INITIAL_STOP'
                 AND (closed_at AT TIME ZONE 'Europe/Rome')::date = (NOW() AT TIME ZONE 'Europe/Rome')::date""",
            (RUNTIME_VERSION,),
        )
        return int(rows[0][0] or 0) if rows else 0

    def evaluate(self) -> GeneratedSignal | None:
        result = super().evaluate()
        return result

    def _economics(self, direction: str, entry: float, stop: float, target: float) -> dict[str, float | bool]:
        step = max(float(self.quantity_step), 1e-12)
        quantity = math.floor((self.trade_notional_eur / entry) / step) * step
        entry_notional = quantity * entry
        if quantity < self.min_qty or entry_notional < self.min_notional_eur:
            return {"tradable": False}
        target_notional = quantity * target
        stop_notional = quantity * stop
        if direction == "LONG":
            gross_profit = quantity * (target - entry)
            gross_loss = quantity * (entry - stop)
        else:
            gross_profit = quantity * (entry - target)
            gross_loss = quantity * (stop - entry)
        open_fee = entry_notional * self.buy_fee_rate
        target_fee = target_notional * self.sell_fee_rate
        stop_fee = stop_notional * self.sell_fee_rate
        spread_cost = quantity * (entry + target) * (self.spread_rate / 2.0)
        slippage_cost = quantity * (entry + target) * self.slippage_rate
        target_cost = open_fee + target_fee + spread_cost + slippage_cost
        stop_cost = open_fee + stop_fee + quantity * (entry + stop) * ((self.spread_rate / 2.0) + self.slippage_rate)
        net_profit = gross_profit - target_cost
        net_loss = gross_loss + stop_cost
        return {
            "tradable": True,
            "quantity": quantity,
            "entry_notional_eur": entry_notional,
            "tp_notional_eur": target_notional,
            "sl_notional_eur": stop_notional,
            "gross_profit_tp1_eur": gross_profit,
            "gross_loss_sl_eur": gross_loss,
            "estimated_buy_fee_eur": open_fee,
            "estimated_sell_fee_eur": target_fee,
            "estimated_sell_fee_sl_eur": stop_fee,
            "estimated_spread_cost_eur": spread_cost,
            "estimated_slippage_cost_eur": slippage_cost,
            "net_profit_tp1_eur": net_profit,
            "net_loss_sl_eur": net_loss,
            "gross_rr": gross_profit / gross_loss if gross_loss > 0 else 0.0,
            "net_rr": net_profit / net_loss if net_loss > 0 else 0.0,
        }

    def evaluate_pair(self, pair: str) -> tuple[GeneratedSignal | None, str]:
        frame_15m = self.fetch_closed_frame(pair, "15m", 360)
        frame_1h = self.fetch_closed_frame(pair, "1h", 360)
        if len(frame_15m) < 100 or len(frame_1h) < 220:
            return None, "INSUFFICIENT_OHLC"

        trigger = self.add_entry_indicators(frame_15m)
        context_data = self.add_context_indicators(frame_1h)
        latest, previous = trigger.iloc[-1], trigger.iloc[-2]
        context, context_previous = context_data.iloc[-1], context_data.iloc[-2]
        required = [latest["close"], latest["atr"], latest["ema20"], latest["trix"], latest["trix_signal"], latest["volume_ratio"], previous["trix"], context["close"], context["ema50"], context["ema200"], context["macd"], context["macd_signal"], context["adx"], context_previous["adx"]]
        if any(pd.isna(value) for value in required):
            return None, "INDICATORS_NOT_READY"

        close_1h = float(context["close"])
        ema50_1h = float(context["ema50"])
        ema200_1h = float(context["ema200"])
        macd_1h = float(context["macd"])
        macd_signal_1h = float(context["macd_signal"])
        adx_1h = float(context["adx"])
        adx_previous = float(context_previous["adx"])
        bullish = close_1h > ema50_1h > ema200_1h and macd_1h > macd_signal_1h and macd_1h > 0
        bearish = close_1h < ema50_1h < ema200_1h and macd_1h < macd_signal_1h and macd_1h < 0
        if not bullish and not bearish:
            return None, "1H_DIRECTION_NOT_CONFIRMED"
        if adx_1h < self.adx_threshold or adx_1h <= adx_previous:
            return None, "1H_ADX_NOT_STRONG_RISING"

        direction = "LONG" if bullish else "SHORT"
        trix_now = float(latest["trix"])
        trix_previous = float(previous["trix"])
        trix_signal = float(latest["trix_signal"])
        if direction == "LONG" and not (trix_previous <= 0 < trix_now and trix_now > trix_signal):
            return None, "15M_NO_LONG_TRIX_CROSS"
        if direction == "SHORT" and not (trix_previous >= 0 > trix_now and trix_now < trix_signal):
            return None, "15M_NO_SHORT_TRIX_CROSS"

        volume_ratio = float(latest["volume_ratio"])
        if volume_ratio < self.volume_ratio_min:
            return None, "15M_VOLUME_TOO_LOW"
        entry = float(latest["close"])
        atr = float(latest["atr"])
        ema20 = float(latest["ema20"])
        if atr <= 0:
            return None, "INVALID_ATR"
        extension_atr = abs(entry - ema20) / atr
        if extension_atr > self.max_extension_atr:
            return None, "15M_PRICE_TOO_EXTENDED"
        if direction == "LONG" and entry <= ema20:
            return None, "15M_PRICE_NOT_ABOVE_EMA20"
        if direction == "SHORT" and entry >= ema20:
            return None, "15M_PRICE_NOT_BELOW_EMA20"

        window = trigger.iloc[-self.structure_lookback - 1:-1]
        if direction == "LONG":
            structural = float(window["low"].min()) - atr * self.structure_buffer_atr
            distance = max(entry - structural, atr * self.min_stop_atr)
            stop, target = entry - distance, entry + 2.0 * distance
        else:
            structural = float(window["high"].max()) + atr * self.structure_buffer_atr
            distance = max(structural - entry, atr * self.min_stop_atr)
            stop, target = entry + distance, entry - 2.0 * distance
        if distance <= 0 or distance > atr * self.max_stop_atr or target <= 0:
            return None, "STRUCTURAL_STOP_INVALID"

        economics = self._economics(direction, entry, stop, target)
        if not bool(economics.get("tradable")) or float(economics["net_profit_tp1_eur"]) <= 0:
            return None, "NOT_TRADABLE"

        score = min(100.0, 55.0 + min(20.0, adx_1h - self.adx_threshold) + min(10.0, max(0.0, volume_ratio - 1.0) * 10.0) + max(0.0, 15.0 * (1.0 - extension_atr / self.max_extension_atr)))
        if score < self.min_signal_score:
            return None, "SCORE_BELOW_MINIMUM"

        risk_sign = 1.0 if direction == "LONG" else -1.0
        reasons = [
            f"trend {direction} confermato su 1h",
            f"EMA50/EMA200 e MACD 1h coerenti con {direction}",
            f"ADX 1h {adx_1h:.1f} crescente",
            f"TRIX 15m attraversa lo zero verso {direction}",
            f"volume 15m {volume_ratio:.2f}x mediana 20",
            f"stop strutturale {distance / atr:.2f} ATR",
        ]
        breakdown: dict[str, Any] = {
            "runtime_version": RUNTIME_VERSION,
            "strategy_version": "TRIX_LS_V2",
            "paper_only": True,
            "direction": direction,
            "execution_timeframe": "15m",
            "context_timeframe": "1h",
            "risk_distance": round(distance, 10),
            "initial_stop": round(stop, 10),
            "be_trigger_price": round(entry + risk_sign * distance, 10),
            "tp1_price": round(target, 10),
            "tp1_fraction": 0.5,
            "atr": round(atr, 10),
            "adx_1h": round(adx_1h, 4),
            "volume_ratio": round(volume_ratio, 4),
            "extension_atr": round(extension_atr, 4),
        }
        return GeneratedSignal(
            strategy=STRATEGY_NAME,
            pair=pair,
            timeframe="15m",
            regime=f"1H_{direction}_15M_TRIX",
            entry=entry,
            stop_loss=stop,
            take_profit=target,
            technical_take_profit=target,
            effective_take_profit=target,
            signal_time=datetime.now(timezone.utc),
            reference_candle_time=pd.Timestamp(latest["timestamp"]).to_pydatetime(),
            entry_timing="ENTER_AFTER_CLOSED_15M_CANDLE",
            quantity=float(economics["quantity"]),
            entry_notional_eur=float(economics["entry_notional_eur"]),
            tp_notional_eur=float(economics["tp_notional_eur"]),
            sl_notional_eur=float(economics["sl_notional_eur"]),
            gross_profit_tp1_eur=float(economics["gross_profit_tp1_eur"]),
            gross_loss_sl_eur=float(economics["gross_loss_sl_eur"]),
            estimated_buy_fee_eur=float(economics["estimated_buy_fee_eur"]),
            estimated_sell_fee_eur=float(economics["estimated_sell_fee_eur"]),
            estimated_sell_fee_sl_eur=float(economics["estimated_sell_fee_sl_eur"]),
            estimated_spread_cost_eur=float(economics["estimated_spread_cost_eur"]),
            estimated_slippage_cost_eur=float(economics["estimated_slippage_cost_eur"]),
            net_profit_tp1_eur=float(economics["net_profit_tp1_eur"]),
            net_loss_sl_eur=float(economics["net_loss_sl_eur"]),
            gross_rr=float(economics["gross_rr"]),
            net_rr=float(economics["net_rr"]),
            stop_distance=distance,
            stop_pct=distance / entry * 100.0,
            atr=atr,
            stop_atr_ratio=distance / atr,
            tp_atr_ratio=2.0 * distance / atr,
            historical_sample_size=0,
            historical_win_rate=None,
            historical_expected_value_eur=None,
            historical_mfe_percentile=0.0,
            historical_mae_percentile=0.0,
            probability_confidence="FORWARD_TEST",
            validation_status="TRIX_V2_RULES_PASSED",
            validation_reason="; ".join(reasons),
            signal_class="B",
            score=score,
            probability=None,
            reasons=reasons,
            score_breakdown=breakdown,  # type: ignore[arg-type]
        ), "OK"

    def fetch_closed_frame(self, pair: str, timeframe: str, limit: int) -> pd.DataFrame:
        seconds_map = {"15m": 900, "1h": 3600}
        rows = self.client.fetch_all(
            """SELECT timestamp, open, high, low, close, volume
               FROM market_data.ohlc
               WHERE LOWER(TRIM(exchange)) = LOWER(TRIM(%s))
                 AND pair = %s AND timeframe = %s
               ORDER BY timestamp DESC LIMIT %s""",
            (self.exchange, pair, timeframe, limit + 4),
        )
        frame = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
        if frame.empty:
            return frame
        frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
        for column in ["open", "high", "low", "close", "volume"]:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
        cutoff = pd.Timestamp.now(tz="UTC").timestamp()
        candle_end = frame["timestamp"].astype("int64") // 1_000_000_000 + seconds_map[timeframe]
        return frame[candle_end <= cutoff].sort_values("timestamp").tail(limit).reset_index(drop=True)
