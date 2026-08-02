"""TRIX Pulse V3: frequent 15m LONG/SHORT PAPER signals with 1h context."""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

import pandas as pd

from project.strategy_engine.service import GeneratedSignal
from project.trix_adx_v2_scanner import TrixAdxV2Scanner

STRATEGY_NAME = "TRIX Pulse V3 LONG SHORT"
RUNTIME_VERSION = "TRIX_PULSE_1H_15M_LS_V3"


class TrixPulseV3Scanner(TrixAdxV2Scanner):
    """Use permissive 1h bias and frequent 15m continuation/breakout triggers."""

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

    def find_active_duplicate(self, candidate: GeneratedSignal) -> int | None:
        direction = str(candidate.score_breakdown.get("direction", "LONG"))
        rows = self.client.fetch_all(
            """SELECT id FROM signals.generated_signals
               WHERE pair = %s
                 AND score_breakdown->>'direction' = %s
                 AND score_breakdown->>'runtime_version' = %s
                 AND created_at >= NOW() - INTERVAL '60 minutes'
               ORDER BY created_at DESC LIMIT 1""",
            (candidate.pair, direction, RUNTIME_VERSION),
        )
        if rows:
            return int(rows[0][0])
        daily = self.client.fetch_all(
            """SELECT COUNT(*) FROM signals.generated_signals
               WHERE pair = %s
                 AND score_breakdown->>'runtime_version' = %s
                 AND telegram_sent_at IS NOT NULL
                 AND (created_at AT TIME ZONE 'Europe/Rome')::date = (NOW() AT TIME ZONE 'Europe/Rome')::date""",
            (candidate.pair, RUNTIME_VERSION),
        )
        return -1 if daily and int(daily[0][0] or 0) >= 2 else None

    def evaluate_pair(self, pair: str) -> tuple[GeneratedSignal | None, str]:
        frame_15m = self.fetch_closed_frame(pair, "15m", 360)
        frame_1h = self.fetch_closed_frame(pair, "1h", 360)
        if len(frame_15m) < 100 or len(frame_1h) < 220:
            return None, "INSUFFICIENT_OHLC"

        trigger = self.add_entry_indicators(frame_15m)
        context_data = self.add_context_indicators(frame_1h)
        latest, previous, previous2 = trigger.iloc[-1], trigger.iloc[-2], trigger.iloc[-3]
        context = context_data.iloc[-1]
        required = [
            latest["close"], latest["atr"], latest["ema20"], latest["trix"],
            latest["trix_signal"], latest["volume_ratio"], previous["trix"],
            previous["trix_signal"], previous2["trix"], context["close"],
            context["ema50"], context["ema200"], context["macd"],
            context["macd_signal"], context["adx"],
        ]
        if any(pd.isna(value) for value in required):
            return None, "INDICATORS_NOT_READY"

        close_1h = float(context["close"])
        ema50_1h = float(context["ema50"])
        ema200_1h = float(context["ema200"])
        macd_1h = float(context["macd"])
        macd_signal_1h = float(context["macd_signal"])
        adx_1h = float(context["adx"])
        if adx_1h < self.adx_threshold:
            return None, "1H_ADX_BELOW_MINIMUM"

        macd_tolerance = max(abs(ema200_1h) * 0.0005, 1e-12)
        long_bias = (close_1h > ema200_1h or ema50_1h > ema200_1h) and macd_1h >= macd_signal_1h - macd_tolerance
        short_bias = (close_1h < ema200_1h or ema50_1h < ema200_1h) and macd_1h <= macd_signal_1h + macd_tolerance
        if long_bias and short_bias:
            direction = "LONG" if macd_1h >= macd_signal_1h else "SHORT"
        elif long_bias:
            direction = "LONG"
        elif short_bias:
            direction = "SHORT"
        else:
            return None, "1H_NO_DIRECTIONAL_BIAS"

        trix_now = float(latest["trix"])
        trix_prev = float(previous["trix"])
        trix_prev2 = float(previous2["trix"])
        signal_now = float(latest["trix_signal"])
        signal_prev = float(previous["trix_signal"])
        bullish_cross = trix_prev <= signal_prev and trix_now > signal_now
        bearish_cross = trix_prev >= signal_prev and trix_now < signal_now
        bullish_acceleration = trix_now > trix_prev > trix_prev2
        bearish_acceleration = trix_now < trix_prev < trix_prev2
        candle_up = float(latest["close"]) > float(latest["open"])
        candle_down = float(latest["close"]) < float(latest["open"])
        if direction == "LONG" and not ((bullish_cross or bullish_acceleration) and candle_up):
            return None, "15M_NO_LONG_PULSE"
        if direction == "SHORT" and not ((bearish_cross or bearish_acceleration) and candle_down):
            return None, "15M_NO_SHORT_PULSE"

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

        recent = trigger.iloc[-11:-1]
        breakout_long = entry > float(recent["high"].max())
        breakout_short = entry < float(recent["low"].min())
        pullback_long = entry >= ema20 and float(previous["low"]) <= ema20 + 0.35 * atr
        pullback_short = entry <= ema20 and float(previous["high"]) >= ema20 - 0.35 * atr
        if direction == "LONG":
            setup = "MOMENTUM_BREAKOUT" if breakout_long else "TREND_CONTINUATION" if pullback_long else "TRIX_ACCELERATION"
        else:
            setup = "MOMENTUM_BREAKOUT" if breakout_short else "TREND_CONTINUATION" if pullback_short else "TRIX_ACCELERATION"

        window = trigger.iloc[-self.structure_lookback - 1:-1]
        if direction == "LONG":
            structural_distance = entry - (float(window["low"].min()) - atr * self.structure_buffer_atr)
        else:
            structural_distance = (float(window["high"].max()) + atr * self.structure_buffer_atr) - entry
        distance = min(max(structural_distance, atr * self.min_stop_atr), atr * self.max_stop_atr)
        if distance <= 0:
            return None, "INVALID_STOP"
        stop = entry - distance if direction == "LONG" else entry + distance
        target = entry + 1.5 * distance if direction == "LONG" else entry - 1.5 * distance
        if target <= 0:
            return None, "INVALID_TARGET"

        economics = self._economics(direction, entry, stop, target)
        if not bool(economics.get("tradable")) or float(economics["net_profit_tp1_eur"]) <= 0:
            return None, "NOT_TRADABLE"

        pulse_bonus = 10.0 if (bullish_cross or bearish_cross) else 6.0
        setup_bonus = 10.0 if setup == "MOMENTUM_BREAKOUT" else 8.0 if setup == "TREND_CONTINUATION" else 5.0
        score = min(
            100.0,
            45.0 + pulse_bonus + setup_bonus
            + min(12.0, max(0.0, adx_1h - self.adx_threshold) * 0.7)
            + min(10.0, max(0.0, volume_ratio - 0.7) * 10.0)
            + max(0.0, 10.0 * (1.0 - extension_atr / self.max_extension_atr)),
        )
        if score < self.min_signal_score:
            return None, "SCORE_BELOW_MINIMUM"

        sign = 1.0 if direction == "LONG" else -1.0
        reasons = [
            f"bias {direction} su 1h",
            f"ADX 1h {adx_1h:.1f}",
            "incrocio TRIX/signal" if (bullish_cross or bearish_cross) else "TRIX accelera da 2 candele",
            f"setup {setup}",
            f"volume 15m {volume_ratio:.2f}x mediana",
            f"stop {distance / atr:.2f} ATR",
        ]
        breakdown: dict[str, Any] = {
            "runtime_version": RUNTIME_VERSION,
            "strategy_version": "TRIX_PULSE_V3",
            "paper_only": True,
            "direction": direction,
            "setup": setup,
            "execution_timeframe": "15m",
            "context_timeframe": "1h",
            "risk_distance": round(distance, 10),
            "initial_stop": round(stop, 10),
            "be_trigger_r": 0.8,
            "be_trigger_price": round(entry + sign * 0.8 * distance, 10),
            "tp1_r": 1.5,
            "tp1_price": round(target, 10),
            "tp1_fraction": 0.5,
            "trailing_atr_multiple": 1.8,
            "atr": round(atr, 10),
            "adx_1h": round(adx_1h, 4),
            "volume_ratio": round(volume_ratio, 4),
            "extension_atr": round(extension_atr, 4),
        }
        return GeneratedSignal(
            strategy=STRATEGY_NAME, pair=pair, timeframe="15m",
            regime=f"1H_{direction}_15M_{setup}", entry=entry, stop_loss=stop,
            take_profit=target, technical_take_profit=target, effective_take_profit=target,
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
            gross_rr=float(economics["gross_rr"]), net_rr=float(economics["net_rr"]),
            stop_distance=distance, stop_pct=distance / entry * 100.0, atr=atr,
            stop_atr_ratio=distance / atr, tp_atr_ratio=1.5 * distance / atr,
            historical_sample_size=0, historical_win_rate=None,
            historical_expected_value_eur=None, historical_mfe_percentile=0.0,
            historical_mae_percentile=0.0, probability_confidence="FORWARD_TEST",
            validation_status="TRIX_PULSE_V3_RULES_PASSED",
            validation_reason="; ".join(reasons), signal_class="B", score=score,
            probability=None, reasons=reasons, score_breakdown=breakdown,
        ), "OK"
