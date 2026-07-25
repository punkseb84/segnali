"""Single LONG Ichimoku cloud breakout strategy for closed 1h candles."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pandas as pd

from project.simple_strategy_scanner import SimpleStrategyScanner
from project.strategy_engine.service import GeneratedSignal

STRATEGY_NAME = "Ichimoku Cloud Breakout"
RUNTIME_VERSION = "ICHIMOKU_CLOUD_BREAKOUT_V1"


class IchimokuCloudScanner(SimpleStrategyScanner):
    """Evaluate only the Ichimoku cloud breakout rules."""

    def __init__(
        self,
        *args: Any,
        tenkan_period: int = 9,
        kijun_period: int = 26,
        senkou_b_period: int = 52,
        displacement: int = 26,
        atr_period: int = 14,
        atr_stop_multiple: float = 1.5,
        reference_target_atr: float = 3.0,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.tenkan_period = max(2, int(tenkan_period))
        self.kijun_period = max(self.tenkan_period, int(kijun_period))
        self.senkou_b_period = max(self.kijun_period, int(senkou_b_period))
        self.displacement = max(1, int(displacement))
        self.atr_period = max(2, int(atr_period))
        self.atr_stop_multiple = max(0.5, float(atr_stop_multiple))
        self.reference_target_atr = max(
            self.atr_stop_multiple + 0.1,
            float(reference_target_atr),
        )
        self.logger = __import__(
            "project.shared.logging", fromlist=["get_module_logger"]
        ).get_module_logger("ichimoku-scanner")

    def add_ichimoku(self, frame: pd.DataFrame) -> pd.DataFrame:
        data = frame.copy()
        high = data["high"].astype(float)
        low = data["low"].astype(float)
        close = data["close"].astype(float)

        tenkan_high = high.rolling(self.tenkan_period).max()
        tenkan_low = low.rolling(self.tenkan_period).min()
        data["tenkan"] = (tenkan_high + tenkan_low) / 2.0

        kijun_high = high.rolling(self.kijun_period).max()
        kijun_low = low.rolling(self.kijun_period).min()
        data["kijun"] = (kijun_high + kijun_low) / 2.0

        span_a_raw = (data["tenkan"] + data["kijun"]) / 2.0
        span_b_raw = (
            high.rolling(self.senkou_b_period).max()
            + low.rolling(self.senkou_b_period).min()
        ) / 2.0

        # The cloud visible at the current candle was calculated displacement
        # candles earlier and plotted forward by standard Ichimoku charts.
        data["cloud_a"] = span_a_raw.shift(self.displacement)
        data["cloud_b"] = span_b_raw.shift(self.displacement)
        data["cloud_top"] = data[["cloud_a", "cloud_b"]].max(axis=1)
        data["cloud_bottom"] = data[["cloud_a", "cloud_b"]].min(axis=1)

        previous_close = close.shift(1)
        true_range = pd.concat(
            [
                high - low,
                (high - previous_close).abs(),
                (low - previous_close).abs(),
            ],
            axis=1,
        ).max(axis=1)
        data["atr"] = true_range.ewm(
            alpha=1 / self.atr_period,
            adjust=False,
        ).mean()
        return data

    def evaluate_pair(self, pair: str) -> tuple[GeneratedSignal | None, str]:
        required = self.senkou_b_period + self.displacement + 20
        frame = self.fetch_closed_frame(pair, "1h", required + 80)
        if len(frame) < required:
            return None, "INSUFFICIENT_OHLC"

        data = self.add_ichimoku(frame)
        latest = data.iloc[-1]
        previous = data.iloc[-2]
        required_values = [
            latest["close"],
            latest["tenkan"],
            latest["kijun"],
            latest["cloud_top"],
            latest["cloud_bottom"],
            latest["atr"],
            previous["cloud_top"],
        ]
        if any(pd.isna(value) for value in required_values):
            return None, "ICHIMOKU_NOT_READY"

        entry = float(latest["close"])
        previous_close = float(previous["close"])
        cloud_top = float(latest["cloud_top"])
        cloud_bottom = float(latest["cloud_bottom"])
        previous_cloud_top = float(previous["cloud_top"])
        tenkan = float(latest["tenkan"])
        kijun = float(latest["kijun"])
        atr = float(latest["atr"])

        if entry <= cloud_top:
            return None, "PRICE_NOT_ABOVE_CLOUD"
        if previous_close > previous_cloud_top:
            return None, "CLOUD_BREAKOUT_NOT_FRESH"
        if tenkan <= kijun:
            return None, "TENKAN_NOT_ABOVE_KIJUN"
        if atr <= 0:
            return None, "INVALID_ATR"

        stop_distance = atr * self.atr_stop_multiple
        stop_loss = entry - stop_distance
        if stop_loss <= 0:
            return None, "INVALID_STOP"

        # A reference target is stored only because the shared signal schema
        # requires one. The live PAPER exit is dynamic and is handled by the
        # Ichimoku position monitor, not by this reference level.
        reference_target = entry + atr * self.reference_target_atr
        economics = self.calculate_net_economics(
            entry,
            stop_loss,
            reference_target,
        )
        if not bool(economics.get("tradable")):
            return None, "NOT_TRADABLE"
        if float(economics["net_profit_tp1_eur"]) < self.min_tp1_net_profit_eur:
            return None, "REFERENCE_MOVE_TOO_SMALL"

        cloud_clearance_atr = max(0.0, (entry - cloud_top) / atr)
        tenkan_kijun_atr = max(0.0, (tenkan - kijun) / atr)
        score = min(
            100.0,
            70.0
            + min(15.0, cloud_clearance_atr * 12.0)
            + min(15.0, tenkan_kijun_atr * 15.0),
        )
        if score < self.min_signal_score:
            return None, "SCORE_BELOW_MINIMUM"

        reasons = [
            "chiusura 1h sopra la nuvola Ichimoku",
            "breakout della nuvola appena confermato",
            "Tenkan Sen sopra Kijun Sen",
            f"stop iniziale a {self.atr_stop_multiple:.2f} ATR",
            "uscita se il prezzo rientra nella nuvola o Tenkan incrocia sotto Kijun",
        ]
        signal_time = datetime.now(timezone.utc)
        reference_time = pd.Timestamp(latest["timestamp"]).to_pydatetime()
        return GeneratedSignal(
            strategy=STRATEGY_NAME,
            pair=pair,
            timeframe="1h",
            regime="ICHIMOKU_CLOUD_BREAKOUT",
            entry=entry,
            stop_loss=stop_loss,
            take_profit=reference_target,
            technical_take_profit=reference_target,
            effective_take_profit=reference_target,
            signal_time=signal_time,
            reference_candle_time=reference_time,
            entry_timing="ENTER_AFTER_CLOSED_1H_CANDLE",
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
            tp_atr_ratio=self.reference_target_atr,
            historical_sample_size=0,
            historical_win_rate=None,
            historical_expected_value_eur=None,
            historical_mfe_percentile=0.0,
            historical_mae_percentile=0.0,
            probability_confidence="NOT_USED",
            validation_status="ICHIMOKU_RULES_PASSED",
            validation_reason="; ".join(reasons),
            signal_class="B",
            score=score,
            probability=None,
            reasons=reasons,
            score_breakdown={
                "runtime_version": RUNTIME_VERSION,
                "execution_mode": "PAPER_DAILY",
                "execution_timeframe": "1h",
                "tenkan_period": self.tenkan_period,
                "kijun_period": self.kijun_period,
                "senkou_b_period": self.senkou_b_period,
                "displacement": self.displacement,
                "cloud_top": round(cloud_top, 10),
                "cloud_bottom": round(cloud_bottom, 10),
                "tenkan": round(tenkan, 10),
                "kijun": round(kijun, 10),
                "atr": round(atr, 10),
                "atr_stop_multiple": self.atr_stop_multiple,
                "reference_target_atr": self.reference_target_atr,
                "dynamic_exit": True,
                "paper_only": True,
            },
        ), "OK"
