from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pandas as pd

from project.enhanced_long_scanner import ENHANCED_LONG_STRATEGIES, EnhancedLongStrategyScanner
from project.notification_engine.simple_formatters import format_simple_signal_message
from project.shared.events import EventBus


class FakeClient:
    def __init__(self, rows=None):
        self.rows = rows or []

    def fetch_all(self, query, params=()):
        return self.rows


def indicator_frame() -> pd.DataFrame:
    now = pd.Timestamp.now(tz="UTC").floor("15min")
    rows = []
    for index in range(230):
        close = 100.0 + index * 0.08
        rows.append({
            "timestamp": now - pd.Timedelta(minutes=15 * (229 - index)),
            "open": close - 0.04,
            "high": close + 0.12,
            "low": close - 0.12,
            "close": close,
            "volume": 100.0 + index,
        })
    return EnhancedLongStrategyScanner.add_indicators(pd.DataFrame(rows))


def build_scanner(**kwargs) -> EnhancedLongStrategyScanner:
    return EnhancedLongStrategyScanner(
        FakeClient(), EventBus(), ["BTC/USD", "ETH/USD", "SOL/USD"], **kwargs
    )


def test_six_strategies_and_three_signal_limit() -> None:
    assert len(ENHANCED_LONG_STRATEGIES) == 6
    assert "Relative Strength Momentum" in ENHANCED_LONG_STRATEGIES
    assert "Volatility Squeeze Breakout" in ENHANCED_LONG_STRATEGIES
    assert build_scanner().max_signals_per_cycle == 3


def test_relative_strength_momentum_can_qualify() -> None:
    frame_15m = indicator_frame()
    frame_1h = indicator_frame()
    frame_15m.loc[frame_15m.index[-1], "rsi"] = 58.0
    frame_15m.loc[frame_15m.index[-1], "volume_ratio"] = 1.1
    frame_15m.loc[frame_15m.index[-1], "macd_hist"] = frame_15m.iloc[-2]["macd_hist"] + 0.1
    frame_15m.loc[frame_15m.index[-1], "close"] = frame_15m.iloc[-1]["ema20"] * 1.01
    result = EnhancedLongStrategyScanner.relative_strength_assessment(
        frame_15m,
        frame_1h,
        "TREND_UP",
        {"rank_percentile": 0.9, "relative_24h": 0.025, "return_4h": 0.03, "median_return_4h": 0.01},
    )
    assert result["qualified"] is True


def test_volatility_squeeze_breakout_can_qualify() -> None:
    frame_15m = indicator_frame()
    frame_1h = indicator_frame()
    frame_15m.loc[frame_15m.index[-8:-1], "bb_width_ratio"] = 0.60
    frame_15m.loc[frame_15m.index[-8:-1], "atr_ratio"] = 0.80
    level = float(frame_15m.iloc[-1]["high20_prev"])
    frame_15m.loc[frame_15m.index[-1], "high"] = level * 1.004
    frame_15m.loc[frame_15m.index[-1], "close"] = level * 1.002
    frame_15m.loc[frame_15m.index[-1], "volume_ratio"] = 1.3
    frame_15m.loc[frame_15m.index[-1], "rsi"] = 61.0
    frame_15m.loc[frame_15m.index[-1], "bb_width"] = frame_15m.iloc[-2]["bb_width"] * 1.1
    frame_15m.loc[frame_15m.index[-1], "macd_hist"] = frame_15m.iloc[-2]["macd_hist"] + 0.1
    result = EnhancedLongStrategyScanner.volatility_squeeze_assessment(frame_15m, frame_1h, "TREND_UP")
    assert result["qualified"] is True


def weak_performance(last_closed_at, latest_status="STOP_LOSS"):
    return {
        "completed": 25,
        "profit_factor": 0.70,
        "net_result_eur": -4.0,
        "latest_status": latest_status,
        "last_closed_at": last_closed_at,
    }


def test_strategy_pause_is_temporary_and_becomes_probation() -> None:
    scanner = build_scanner(performance_pause_hours=72)
    now = datetime.now(timezone.utc)
    paused = scanner.determine_strategy_state("Breakout 20", weak_performance(now - timedelta(hours=2)), now)
    probation = scanner.determine_strategy_state("Breakout 20", weak_performance(now - timedelta(hours=73)), now)
    assert paused["state"] == "PAUSED"
    assert probation["state"] == "PROBATION"


def test_winning_probe_stays_probation_until_metrics_recover() -> None:
    scanner = build_scanner()
    now = datetime.now(timezone.utc)
    state = scanner.determine_strategy_state(
        "Breakout 20", weak_performance(now - timedelta(hours=1), "TARGET_HIT"), now
    )
    assert state["state"] == "PROBATION"


def test_strategy_reactivates_when_metrics_recover() -> None:
    scanner = build_scanner()
    state = scanner.determine_strategy_state(
        "Breakout 20",
        {
            "completed": 25,
            "profit_factor": 1.05,
            "net_result_eur": 0.5,
            "latest_status": "TARGET_HIT",
            "last_closed_at": datetime.now(timezone.utc),
        },
    )
    assert state["state"] == "ACTIVE"


def test_correlation_filter_allows_two_and_blocks_third() -> None:
    scanner = build_scanner(correlation_threshold=0.80, max_correlated_per_cycle=2)
    frame = indicator_frame()
    scanner._frame_cache = {"BTC/USD": frame.copy(), "ETH/USD": frame.copy(), "SOL/USD": frame.copy()}
    one = scanner.correlated_with_published("ETH/USD", [SimpleNamespace(pair="BTC/USD")])
    two = scanner.correlated_with_published(
        "SOL/USD", [SimpleNamespace(pair="BTC/USD"), SimpleNamespace(pair="ETH/USD")]
    )
    assert len(one) == 1
    assert len(two) == 2


def test_telegram_explains_probation() -> None:
    message = format_simple_signal_message({
        "signal_id": 10,
        "pair": "SOL/USD",
        "timeframe": "15m",
        "strategy": "Relative Strength Momentum",
        "regime": "TREND_UP",
        "entry": 100,
        "stop_loss": 98,
        "take_profit": 104,
        "score": 80,
        "net_rr": 1.3,
        "net_profit_tp1_eur": 0.4,
        "net_loss_sl_eur": 0.3,
        "reasons": ["forza relativa confermata"],
        "score_breakdown": {"strategy_state": "PROBATION", "forward_completed": 25, "forward_profit_factor": 0.8},
    })
    assert "PROBATION" in message
    assert "segnale-test" in message
