"""Monitor Ichimoku PAPER positions with ATR stop and dynamic cloud exits."""
from __future__ import annotations

from typing import Any

import pandas as pd

from project.daily_paper_position_monitor import DailyPaperPositionMonitor
from project.ichimoku_scanner import RUNTIME_VERSION
from project.shared.events import Event, EventType


class IchimokuPositionMonitor(DailyPaperPositionMonitor):
    """Close LONG signals on ATR stop, cloud re-entry or bearish Tenkan/Kijun cross."""

    def __init__(
        self,
        *args: Any,
        tenkan_period: int = 9,
        kijun_period: int = 26,
        senkou_b_period: int = 52,
        displacement: int = 26,
        sell_fee_rate: float = 0.001,
        spread_rate: float = 0.0005,
        slippage_rate: float = 0.0,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.tenkan_period = max(2, int(tenkan_period))
        self.kijun_period = max(self.tenkan_period, int(kijun_period))
        self.senkou_b_period = max(self.kijun_period, int(senkou_b_period))
        self.displacement = max(1, int(displacement))
        self.sell_fee_rate = max(0.0, float(sell_fee_rate))
        self.spread_rate = max(0.0, float(spread_rate))
        self.slippage_rate = max(0.0, float(slippage_rate))
        self.logger = __import__(
            "project.shared.logging", fromlist=["get_module_logger"]
        ).get_module_logger("ichimoku-position-monitor")

    def fetch_open_signals(self) -> list[dict[str, Any]]:
        rows = self.postgres.fetch_all(
            """SELECT id, strategy, pair, timeframe, regime, entry, stop_loss, take_profit,
                      score, probability, signal_class,
                      net_profit_tp1_eur, net_loss_sl_eur, net_rr,
                      reference_candle_time, created_at,
                      COALESCE(exchange, 'Kraken'), quantity,
                      COALESCE(estimated_buy_fee_eur, 0)
            FROM signals.generated_signals
            WHERE status IN ('NEW', 'OPEN')
              AND signal_class IN ('A', 'B')
              AND telegram_sent_at IS NOT NULL
              AND score_breakdown->>'runtime_version' = %s
            ORDER BY created_at ASC
            LIMIT %s""",
            (RUNTIME_VERSION, self.max_signals_per_cycle),
        )
        signals: list[dict[str, Any]] = []
        for row in rows:
            signals.append(
                {
                    "id": int(row[0]),
                    "strategy": str(row[1]),
                    "pair": str(row[2]),
                    "timeframe": str(row[3]),
                    "regime": str(row[4]),
                    "entry": float(row[5]),
                    "stop_loss": float(row[6]),
                    "take_profit": float(row[7]),
                    "score": float(row[8]),
                    "probability": float(row[9]) if row[9] is not None else None,
                    "signal_class": str(row[10]),
                    "net_profit_tp1_eur": float(row[11] or 0.0),
                    "net_loss_sl_eur": float(row[12] or 0.0),
                    "net_rr": float(row[13] or 0.0),
                    "reference_candle_time": row[14],
                    "created_at": row[15],
                    "exchange": str(row[16] or "Kraken"),
                    "quantity": float(row[17] or 0.0),
                    "estimated_buy_fee_eur": float(row[18] or 0.0),
                    "execution_mode": "PAPER_DAILY",
                }
            )
        return signals

    def _add_ichimoku(self, frame: pd.DataFrame) -> pd.DataFrame:
        data = frame.copy()
        high = data["high"].astype(float)
        low = data["low"].astype(float)

        data["tenkan"] = (
            high.rolling(self.tenkan_period).max()
            + low.rolling(self.tenkan_period).min()
        ) / 2.0
        data["kijun"] = (
            high.rolling(self.kijun_period).max()
            + low.rolling(self.kijun_period).min()
        ) / 2.0
        span_a_raw = (data["tenkan"] + data["kijun"]) / 2.0
        span_b_raw = (
            high.rolling(self.senkou_b_period).max()
            + low.rolling(self.senkou_b_period).min()
        ) / 2.0
        data["cloud_a"] = span_a_raw.shift(self.displacement)
        data["cloud_b"] = span_b_raw.shift(self.displacement)
        data["cloud_top"] = data[["cloud_a", "cloud_b"]].max(axis=1)
        return data

    def _realized_net(self, signal: dict[str, Any], exit_price: float) -> float:
        quantity = float(signal.get("quantity") or 0.0)
        entry = float(signal["entry"])
        gross = quantity * (exit_price - entry)
        buy_fee = float(signal.get("estimated_buy_fee_eur") or 0.0)
        sell_fee = quantity * exit_price * self.sell_fee_rate
        half_spread = self.spread_rate / 2.0
        spread_cost = quantity * ((entry * half_spread) + (exit_price * half_spread))
        slippage_cost = quantity * (
            (entry * self.slippage_rate) + (exit_price * self.slippage_rate)
        )
        return gross - buy_fee - sell_fee - spread_cost - slippage_cost

    def check_signal(self, signal: dict[str, Any]) -> bool:
        rows = self.postgres.fetch_all(
            """SELECT timestamp, open, high, low, close
            FROM market_data.ohlc
            WHERE exchange = %s AND pair = %s AND timeframe = %s
            ORDER BY timestamp DESC
            LIMIT 320""",
            (signal["exchange"], signal["pair"], signal["timeframe"]),
        )
        if not rows:
            return False

        frame = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close"])
        frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
        for column in ["open", "high", "low", "close"]:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
        frame = frame.sort_values("timestamp").reset_index(drop=True)

        now_seconds = pd.Timestamp.now(tz="UTC").timestamp()
        seconds = 3600 if signal["timeframe"] == "1h" else 900
        candle_end = frame["timestamp"].astype("int64") // 1_000_000_000 + seconds
        frame = frame[candle_end <= now_seconds].reset_index(drop=True)
        data = self._add_ichimoku(frame)

        start = pd.Timestamp(signal.get("reference_candle_time") or signal["created_at"])
        start = start.tz_localize("UTC") if start.tzinfo is None else start.tz_convert("UTC")

        for index in range(1, len(data)):
            latest = data.iloc[index]
            previous = data.iloc[index - 1]
            timestamp = pd.Timestamp(latest["timestamp"])
            if timestamp <= start:
                continue

            open_price = float(latest["open"])
            high_price = float(latest["high"])
            low_price = float(latest["low"])
            close_price = float(latest["close"])

            if low_price <= float(signal["stop_loss"]):
                return self._close_signal(
                    signal,
                    outcome="STOP_LOSS",
                    outcome_price=float(signal["stop_loss"]),
                    timestamp=timestamp,
                    open_price=open_price,
                    high_price=high_price,
                    low_price=low_price,
                    close_price=close_price,
                    resolution="ATR_STOP_ICHIMOKU",
                )

            values = [latest["cloud_top"], latest["tenkan"], latest["kijun"]]
            if any(pd.isna(value) for value in values):
                continue

            cloud_exit = close_price <= float(latest["cloud_top"])
            bearish_cross = (
                float(latest["tenkan"]) < float(latest["kijun"])
                and not pd.isna(previous["tenkan"])
                and not pd.isna(previous["kijun"])
                and float(previous["tenkan"]) >= float(previous["kijun"])
            )
            if not cloud_exit and not bearish_cross:
                continue

            resolution = (
                "ICHIMOKU_CLOUD_EXIT"
                if cloud_exit
                else "ICHIMOKU_TENKAN_KIJUN_EXIT"
            )
            return self._close_signal(
                signal,
                outcome="TARGET_HIT",
                outcome_price=close_price,
                timestamp=timestamp,
                open_price=open_price,
                high_price=high_price,
                low_price=low_price,
                close_price=close_price,
                resolution=resolution,
            )
        return False

    def _close_signal(
        self,
        signal: dict[str, Any],
        *,
        outcome: str,
        outcome_price: float,
        timestamp: Any,
        open_price: float,
        high_price: float,
        low_price: float,
        close_price: float,
        resolution: str,
    ) -> bool:
        realized_net = self._realized_net(signal, outcome_price)
        net_profit = max(realized_net, 0.0)
        net_loss = max(-realized_net, 0.0)
        self.postgres.execute(
            """UPDATE signals.generated_signals
            SET status = %s,
                outcome_price = %s,
                outcome_open_price = %s,
                outcome_high_price = %s,
                outcome_low_price = %s,
                outcome_close_price = %s,
                outcome_candle_time = %s,
                closed_at = NOW(),
                outcome_ambiguous = FALSE,
                outcome_resolution = %s,
                net_profit_tp1_eur = %s,
                net_loss_sl_eur = %s
            WHERE id = %s AND status IN ('NEW', 'OPEN')""",
            (
                outcome,
                outcome_price,
                open_price,
                high_price,
                low_price,
                close_price,
                timestamp,
                resolution,
                net_profit,
                net_loss,
                signal["id"],
            ),
        )
        event_type = EventType.STOP_LOSS if outcome == "STOP_LOSS" else EventType.TARGET_HIT
        payload = {
            **signal,
            "signal_id": signal["id"],
            "outcome": outcome,
            "outcome_price": outcome_price,
            "outcome_open_price": open_price,
            "outcome_high_price": high_price,
            "outcome_low_price": low_price,
            "close_price": close_price,
            "closed_at": str(timestamp),
            "ambiguous": False,
            "outcome_resolution": resolution,
            "realized_net_eur": realized_net,
            "net_profit_tp1_eur": net_profit,
            "net_loss_sl_eur": net_loss,
        }
        self.event_bus.publish(Event(event_type, payload))
        self.logger.info(
            "ICHIMOKU_POSITION_CLOSED id=%s pair=%s outcome=%s resolution=%s exit=%.8f net=%.4f",
            signal["id"],
            signal["pair"],
            outcome,
            resolution,
            outcome_price,
            realized_net,
        )
        return True
