from __future__ import annotations

import pandas as pd

from project.notification_engine.simple_formatters import (
    format_simple_outcome_message,
    format_simple_report_message,
)
from project.optimized_long_scanner import (
    OptimizedLongStrategyScanner,
    SIMPLE_STRATEGIES,
)
from project.simple_strategy_scanner import SimpleStrategyScanner


def indicator_frame() -> pd.DataFrame:
    now = pd.Timestamp.now(tz="UTC").floor("15min")
    rows = []
    for index in range(240):
        close = 100.0 + index * 0.06
        rows.append(
            {
                "timestamp": now - pd.Timedelta(minutes=15 * (239 - index)),
                "open": close - 0.05,
                "high": close + 0.20,
                "low": close - 0.20,
                "close": close,
                "volume": 100.0 + index,
            }
        )
    return OptimizedLongStrategyScanner.add_indicators(pd.DataFrame(rows))


def test_scanner_contains_only_long_strategies() -> None:
    assert SIMPLE_STRATEGIES == (
        "Trend Pullback",
        "Breakout 20",
        "Range Bounce Bollinger",
        "Oversold Reversal",
    )
    assert all("SHORT" not in strategy.upper() for strategy in SIMPLE_STRATEGIES)


def test_oversold_reversal_requires_confirmed_reclaim() -> None:
    frame = indicator_frame()
    hourly = indicator_frame()
    before_index = frame.index[-3]
    previous_index = frame.index[-2]
    current_index = frame.index[-1]

    frame.loc[before_index, "macd_hist"] = -0.50
    frame.loc[previous_index, "macd_hist"] = -0.30
    frame.loc[current_index, "macd_hist"] = -0.05
    frame.loc[previous_index, "rsi"] = 37.0
    frame.loc[current_index, "rsi"] = 41.0
    frame.loc[previous_index, "close"] = frame.loc[previous_index, "ema20"] * 0.995
    frame.loc[current_index, "open"] = frame.loc[current_index, "ema20"] * 0.998
    frame.loc[current_index, "close"] = max(
        frame.loc[current_index, "ema20"] * 1.006,
        frame.loc[previous_index, "high"] * 1.002,
    )
    frame.loc[current_index, "volume_ratio"] = 1.05
    frame.loc[current_index, "bullish"] = True
    hourly.loc[hourly.index[-1], "close"] = hourly.loc[hourly.index[-1], "ema200"] * 0.95

    assessment = OptimizedLongStrategyScanner.oversold_reversal_assessment(
        frame, hourly, "TREND_DOWN"
    )

    assert assessment["qualified"] is True
    assert assessment["score"] >= 78
    assert assessment["strategy"] == "Oversold Reversal"


def test_oversold_reversal_blocks_falling_price() -> None:
    frame = indicator_frame()
    hourly = indicator_frame()
    frame.loc[frame.index[-1], "close"] = frame.loc[frame.index[-1], "ema20"] * 0.98
    frame.loc[frame.index[-1], "rsi"] = 30.0
    frame.loc[frame.index[-1], "volume_ratio"] = 2.0

    assessment = OptimizedLongStrategyScanner.oversold_reversal_assessment(
        frame, hourly, "TREND_DOWN"
    )

    assert assessment["qualified"] is False
    assert "EMA20 non ancora recuperata" in assessment["blockers"]


def test_forward_performance_metrics_are_net_and_auditable() -> None:
    rows = [
        ("Breakout 20", "TARGET_HIT", 1.20, 0.80, None),
        ("Breakout 20", "STOP_LOSS", 1.10, 0.60, None),
        ("Breakout 20", "TARGET_HIT", 0.90, 0.70, None),
        ("Breakout 20", "STOP_LOSS", 1.00, 0.50, None),
    ]

    metrics = OptimizedLongStrategyScanner.calculate_performance_metrics(rows)

    assert metrics["completed"] == 4
    assert metrics["wins"] == 2
    assert metrics["losses"] == 2
    assert metrics["win_rate"] == 0.5
    assert metrics["gross_profit_eur"] == 2.1
    assert metrics["gross_loss_eur"] == 1.1
    assert round(float(metrics["profit_factor"]), 4) == round(2.1 / 1.1, 4)
    assert round(float(metrics["net_result_eur"]), 4) == 1.0


def test_trusted_report_html_is_preserved() -> None:
    message = "📊 <b>REPORT SCANNER LONG</b>\n• Coppie: <b>20</b>"
    formatted = format_simple_report_message(
        {"message": message, "trusted_html": True}
    )
    assert formatted == message
    assert "&lt;b&gt;" not in formatted


def test_untrusted_report_is_escaped() -> None:
    formatted = format_simple_report_message({"message": "<b>test</b>"})
    assert "&lt;b&gt;test&lt;/b&gt;" in formatted


def test_simple_outcome_does_not_show_old_classes_or_shorts() -> None:
    message = format_simple_outcome_message(
        {
            "signal_id": 21,
            "outcome": "TARGET_HIT",
            "pair": "SOL/USD",
            "timeframe": "15m",
            "strategy": "Trend Pullback",
            "entry": 100,
            "outcome_price": 102,
            "net_profit_tp1_eur": 1.2,
            "net_rr": 1.4,
            "closed_at": "2026-07-18 10:00 UTC",
            "outcome_resolution": "FULL_CANDLE_RANGE",
        }
    )
    assert "Classe" not in message
    assert "SHORT" not in message
    assert "TAKE PROFIT" in message


def test_existing_indicator_engine_remains_compatible() -> None:
    frame = indicator_frame()
    assert "ema20" in frame.columns
    assert "macd_hist" in frame.columns
    assert len(frame) == 240
    assert callable(SimpleStrategyScanner.calculate_net_economics)
