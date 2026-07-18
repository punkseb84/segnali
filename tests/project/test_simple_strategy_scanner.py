from __future__ import annotations

from datetime import timedelta

import pandas as pd

from project.config.settings import PlatformSettings
from project.notification_engine.simple_formatters import format_simple_signal_message
from project.shared.events import EventBus
from project.simple_main import apply_simple_policy
from project.simple_strategy_scanner import SimpleStrategyScanner


class FakeClient:
    def __init__(self, rows=None):
        self.rows = rows or []
        self.queries = []

    def fetch_all(self, query, params=()):
        self.queries.append((query, params))
        return self.rows


def indicator_frame() -> pd.DataFrame:
    now = pd.Timestamp.now(tz="UTC").floor("15min")
    rows = []
    for index in range(220):
        close = 100.0 + index * 0.10
        rows.append(
            {
                "timestamp": now - pd.Timedelta(minutes=15 * (219 - index)),
                "open": close - 0.05,
                "high": close + 0.20,
                "low": close - 0.20,
                "close": close,
                "volume": 100.0 + index,
            }
        )
    return SimpleStrategyScanner.add_indicators(pd.DataFrame(rows))


def test_simple_policy_disables_quant_research() -> None:
    settings = apply_simple_policy(
        PlatformSettings(
            enable_research_engine=True,
            enable_decision_engine=True,
            collector_timeframes=["5m", "15m", "1h", "4h"],
        )
    )
    assert settings.enable_research_engine is False
    assert settings.enable_decision_engine is False
    assert settings.collector_timeframes == ["15m", "1h"]
    assert settings.scheduler_collector_seconds == 900
    assert settings.watchlist_signal_classes == []


def test_detects_uptrend_from_hourly_context() -> None:
    frame_15m = indicator_frame()
    frame_1h = indicator_frame()
    assert SimpleStrategyScanner.detect_regime(frame_15m, frame_1h) == "TREND_UP"


def test_trend_pullback_setup_is_readable_and_actionable() -> None:
    frame = indicator_frame()
    frame.loc[frame.index[-1], "low"] = frame.iloc[-1]["ema20"] * 0.999
    frame.loc[frame.index[-1], "close"] = frame.iloc[-1]["ema20"] * 1.003
    frame.loc[frame.index[-2], "close"] = frame.iloc[-1]["close"] * 0.998
    frame.loc[frame.index[-1], "rsi"] = 56.0
    frame.loc[frame.index[-1], "volume_ratio"] = 1.2
    setup = SimpleStrategyScanner.trend_pullback_setup(frame, frame, "TREND_UP")
    assert setup is not None
    assert setup["strategy"] == "Trend Pullback"
    assert setup["score"] >= 70


def test_breakout_is_blocked_in_downtrend() -> None:
    frame = indicator_frame()
    frame.loc[frame.index[-1], "close"] = frame.iloc[-1]["high20_prev"] + 1
    frame.loc[frame.index[-1], "rsi"] = 60.0
    frame.loc[frame.index[-1], "volume_ratio"] = 2.0
    assert SimpleStrategyScanner.breakout_setup(frame, frame, "TREND_DOWN") is None


def test_mean_reversion_requires_reentry_inside_bollinger_band() -> None:
    frame = indicator_frame()
    frame.loc[frame.index[-2], "close"] = frame.iloc[-2]["bb_lower"] - 0.1
    frame.loc[frame.index[-1], "close"] = frame.iloc[-1]["bb_lower"] + 0.1
    frame.loc[frame.index[-1], "rsi"] = 37.0
    frame.loc[frame.index[-1], "volume_ratio"] = 1.0
    hourly = frame.copy()
    hourly.loc[hourly.index[-1], "close"] = hourly.iloc[-1]["ema200"] * 1.01
    setup = SimpleStrategyScanner.mean_reversion_setup(frame, hourly, "RANGE")
    assert setup is not None
    assert setup["strategy"] == "Mean Reversion Bollinger"


def test_fetch_closed_frame_excludes_open_candle() -> None:
    current_interval = pd.Timestamp.now(tz="UTC").floor("15min").to_pydatetime()
    closed = current_interval - timedelta(minutes=15)
    client = FakeClient(
        [
            (current_interval, 100, 101, 99, 100.5, 10),
            (closed, 99, 100, 98, 99.5, 9),
        ]
    )
    scanner = SimpleStrategyScanner(client, EventBus(), ["BTC/USD"])
    frame = scanner.fetch_closed_frame("BTC/USD", "15m", 10)
    assert len(frame) == 1
    assert float(frame.iloc[0]["close"]) == 99.5


def test_simple_telegram_message_does_not_show_classes() -> None:
    message = format_simple_signal_message(
        {
            "signal_id": 10,
            "pair": "BTC/USD",
            "timeframe": "15m",
            "strategy": "Breakout 20",
            "regime": "TREND_UP",
            "entry": 100,
            "stop_loss": 98,
            "take_profit": 104,
            "score": 78,
            "net_rr": 1.4,
            "net_profit_tp1_eur": 1.2,
            "net_loss_sl_eur": 0.8,
            "reasons": ["rottura massimo 20 candele", "volume sopra media"],
        }
    )
    assert "SEGNALE LONG" in message
    assert "Classe" not in message
    assert "Breakout 20" in message
