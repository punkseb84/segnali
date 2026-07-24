"""Single LONG Donchian breakout strategy for paper signals.

Rules on closed 15m candles:
- close breaks above the previous Donchian upper channel;
- close is above EMA 200;
- ADX(14) is at least the configured threshold;
- stop and target are ATR-based.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pandas as pd

from project.simple_strategy_scanner import SimpleStrategyScanner
from project.strategy_engine.service import GeneratedSignal

STRATEGY_NAME = "Donchian Breakout EMA200 ADX ATR"
RUNTIME_VERSION = "DONCHIAN_EMA200_ADX_ATR_V1"


class DonchianBreakoutScanner(SimpleStrategyScanner):
    """Evaluate exactly one transparent breakout strategy."""

    def __init__(
        self,
        *args: Any,
        donchian_period: int = 20,
        ema_period: int = 200,
        adx_period: int = 14,
        adx_min: float = 25.0,
        atr_period: int = 14,
        atr_stop_multiple: float = 1.5,
        atr_target_multiple: float = 3.0,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.donchian_period = max(5, int(donchian_period))
        self.ema_period = max(20, int(ema_period))
        self.adx_period = max(5, int(adx_period))
        self.adx_min = max(1.0, float(adx_min))
        self.atr_period = max(5, int(atr_period))
        self.atr_stop_multiple = max(0.5, float(atr_stop_multiple))
        self.atr_target_multiple = max(
            self.atr_stop_multiple + 0.1, float(atr_target_multiple)
        )
        self.logger = __import__(
            "project.shared.logging", fromlist=["get_module_logger"]
        ).get_module_logger("donchian-scanner")

    def _add_strategy_indicators(self, frame: pd.DataFrame) -> pd.DataFrame:
        data = frame.copy()
        high = data["high"].astype(float)
        low = data["low"].astype(float)
        close = data["close"].astype(float)

        data["ema200"] = close.ewm(span=self.ema_period, adjust=False).mean()
        data["donchian_upper"] = high.shift(1).rolling(self.donchian_period).max()
        data["donchian_lower"] = low.shift(1).rolling(self.donchian_period).min()

        previous_close = close.shift(1)
        true_range = pd.concat(
            [high - low, (high - previous_close).abs(), (low - previous_close).abs()],
            axis=1,
        ).max(axis=1)
        data["atr"] = true_range.ewm(alpha=1 / self.atr_period, adjust=False).mean()

        up_move = high.diff()
        down_move = -low.diff()
        plus_dm = up_move.where((up_move > down_move) & (up_move > 0), 0.0)
        minus_dm = down_move.where((down_move > up_move) & (down_move > 0), 0.0)
        atr_smoothed = true_range.ewm(alpha=1 / self.adx_period, adjust=False).mean()
        plus_di = 100.0 * plus_dm.ewm(alpha=1 / self.adx_period, adjust=False).mean() / atr_smoothed.replace(0, float("nan"))
        minus_di = 100.0 * minus_dm.ewm(alpha=1 / self.adx_period, adjust=False).mean() / atr_smoothed.replace(0, float("nan"))
        dx = 100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, float("nan"))
        data["adx"] = dx.ewm(alpha=1 / self.adx_period, adjust=False).mean().fillna(0.0)
        data["plus_di"] = plus_di.fillna(0.0)
        data["minus_di"] = minus_di.fillna(0.0)
        return data

    def evaluate_pair(self, pair: str) -> tuple[GeneratedSignal | None, str]:
        required = max(self.ema_period + 20, self.donchian_period + 20)
        frame = self.fetch_closed_frame(pair, "15m", required + 20)
        if len(frame) < required:
            return None, "INSUFFICIENT_OHLC"

        data = self._add_strategy_indicators(frame)
        latest = data.iloc[-1]
        previous = data.iloc[-2]
        values = [latest["close"], latest["ema200"], latest["donchian_upper"], latest["atr"], latest["adx"]]
        if any(pd.isna(value) for value in values):
            return None, "INDICATORS_NOT_READY"

        entry = float(latest["close"])
        ema200 = float(latest["ema200"])
        upper = float(latest["donchian_upper"])
        previous_upper = float(previous["donchian_upper"])
        adx = float(latest["adx"])
        plus_di = float(latest["plus_di"])
        minus_di = float(latest["minus_di"])
        atr = float(latest["atr"])

        if entry <= ema200:
            return None, "BELOW_EMA200"
        if adx < self.adx_min:
            return None, "ADX_BELOW_MINIMUM"
        if plus_di <= minus_di:
            return None, "DIRECTION_NOT_BULLISH"
        if float(previous["close"]) > previous_upper:
            return None, "BREAKOUT_NOT_FRESH"
        if entry <= upper:
            return None, "NO_DONCHIAN_BREAKOUT"
        if atr <= 0:
            return None, "INVALID_ATR"

        stop_distance = atr * self.atr_stop_multiple
        stop_loss = entry - stop_distance
        target = entry + atr * self.atr_target_multiple
        if stop_loss <= 0:
            return None, "INVALID_STOP"

        economics = self.calculate_net_economics(entry, stop_loss, target)
        if not bool(economics.get("tradable")):
            return None, "NOT_TRADABLE"
        if float(economics["net_rr"]) < self.min_net_rr:
            return None, "NET_RR_TOO_LOW"
        if float(economics["net_profit_tp1_eur"]) < self.min_tp1_net_profit_eur:
            return None, "NET_PROFIT_TOO_LOW"

        breakout_pct = (entry / upper - 1.0) * 100.0
        score = min(100.0, 70.0 + min(20.0, max(0.0, adx - self.adx_min)) + min(10.0, breakout_pct * 20.0))
        if score < self.min_signal_score:
            return None, "SCORE_BELOW_MINIMUM"

        reasons = [
            f"chiusura sopra Donchian {self.donchian_period}",
            f"prezzo sopra EMA {self.ema_period}",
            f"ADX({self.adx_period})={adx:.1f} >= {self.adx_min:.1f}",
            f"+DI={plus_di:.1f} > -DI={minus_di:.1f}",
            f"stop={self.atr_stop_multiple:.2f} ATR",
            f"target={self.atr_target_multiple:.2f} ATR",
        ]
        signal_time = datetime.now(timezone.utc)
        reference_time = pd.Timestamp(latest["timestamp"]).to_pydatetime()
        return GeneratedSignal(
            strategy=STRATEGY_NAME,
            pair=pair,
            timeframe="15m",
            regime="DONCHIAN_TREND_BREAKOUT",
            entry=entry,
            stop_loss=stop_loss,
            take_profit=target,
            technical_take_profit=target,
            effective_take_profit=target,
            signal_time=signal_time,
            reference_candle_time=reference_time,
            entry_timing="ENTER_ON_SIGNAL_RECEIPT",
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
            stop_distance=stop_distance,
            stop_pct=stop_distance / entry * 100.0,
            atr=atr,
            stop_atr_ratio=self.atr_stop_multiple,
            tp_atr_ratio=self.atr_target_multiple,
            historical_sample_size=0,
            historical_win_rate=None,
            historical_expected_value_eur=None,
            historical_mfe_percentile=0.0,
            historical_mae_percentile=0.0,
            probability_confidence="NOT_USED",
            validation_status="DONCHIAN_RULES_PASSED",
            validation_reason="; ".join(reasons),
            signal_class="B",
            score=score,
            probability=None,
            reasons=reasons,
            score_breakdown={
                "runtime_version": RUNTIME_VERSION,
                "donchian_period": self.donchian_period,
                "ema_period": self.ema_period,
                "adx": round(adx, 2),
                "adx_min": self.adx_min,
                "atr": round(atr, 10),
                "atr_stop_multiple": self.atr_stop_multiple,
                "atr_target_multiple": self.atr_target_multiple,
                "breakout_pct": round(breakout_pct, 4),
                "paper_only": True,
            },
        ), "OK"
