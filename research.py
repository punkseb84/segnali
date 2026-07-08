"""Research/backtest mode for finding statistically validated strategy edges."""
from __future__ import annotations

from dataclasses import dataclass
from math import sqrt
from typing import Callable

import pandas as pd

from config import PAIRS, RESEARCH_MIN_TRADES, RESEARCH_OHLC_LIMIT, RESEARCH_TIMEFRAMES, TRADE_AMOUNT_EUR
from exchange import fetch_ohlc
from indicators import add_indicators
from risk import calculate_plan
from strategy import closed_candles

SignalFn = Callable[[pd.DataFrame], pd.Series]


@dataclass
class Metrics:
    trades: int
    win_rate: float
    profit_factor: float
    expectancy_eur: float
    max_drawdown_eur: float
    net_profit_eur: float


@dataclass
class ResearchResult:
    strategy: str
    pair: str
    timeframe: str
    train: Metrics
    validation: Metrics
    total: Metrics
    params: dict[str, str | float | int | bool]
    avoid: list[str]


def _empty_metrics() -> Metrics:
    return Metrics(0, 0.0, 0.0, 0.0, 0.0, 0.0)


def _profit_factor(results: list[float]) -> float:
    gross_profit = sum(value for value in results if value > 0)
    gross_loss = abs(sum(value for value in results if value < 0))
    if gross_loss == 0:
        return gross_profit if gross_profit else 0.0
    return gross_profit / gross_loss


def _max_drawdown(results: list[float]) -> float:
    equity = TRADE_AMOUNT_EUR
    peak = equity
    max_dd = 0.0
    for result in results:
        equity += result
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    return max_dd


def metrics(results: list[float]) -> Metrics:
    if not results:
        return _empty_metrics()
    wins = [value for value in results if value > 0]
    return Metrics(
        trades=len(results),
        win_rate=len(wins) / len(results) * 100,
        profit_factor=_profit_factor(results),
        expectancy_eur=sum(results) / len(results),
        max_drawdown_eur=_max_drawdown(results),
        net_profit_eur=sum(results),
    )


def _atr_percentile_filter(frame: pd.DataFrame, low: float = 0.2, high: float = 0.9) -> pd.Series:
    atr_pct = frame["atr14"] / frame["close"]
    low_value = atr_pct.quantile(low)
    high_value = atr_pct.quantile(high)
    return atr_pct.between(low_value, high_value)


def breakout_trend(frame: pd.DataFrame) -> pd.Series:
    prior_high = frame["high"].rolling(20).max().shift(1)
    return (
        (frame["close"] > prior_high)
        & (frame["ema20"] > frame["ema50"])
        & (frame["close"] > frame["ema200"])
        & frame["rsi14"].between(50, 68)
        & frame["adx14"].between(18, 45)
        & (frame["relative_volume"] >= 1.0)
        & _atr_percentile_filter(frame)
    )


def pullback_ema(frame: pd.DataFrame) -> pd.Series:
    touched_ema = (frame["low"] <= frame["ema20"]) | (frame["low"] <= frame["ema50"])
    return (
        touched_ema
        & (frame["close"] > frame["ema20"])
        & (frame["ema20"] > frame["ema50"])
        & (frame["close"] > frame["ema200"])
        & frame["rsi14"].between(45, 62)
        & frame["adx14"].between(15, 38)
        & (frame["relative_volume"] >= 0.8)
        & _atr_percentile_filter(frame)
    )


def mean_reversion_range(frame: pd.DataFrame) -> pd.Series:
    support_room = (frame["close"] - frame["support"]) / frame["close"] * 100
    return (
        (frame["adx14"] < 18)
        & frame["rsi14"].between(25, 42)
        & (support_room <= 0.8)
        & (frame["close"] > frame["support"])
        & (frame["relative_volume"] >= 0.7)
        & _atr_percentile_filter(frame, 0.1, 0.8)
    )


def volume_spike_momentum(frame: pd.DataFrame) -> pd.Series:
    return (
        (frame["relative_volume"] >= 1.5)
        & (frame["macd_hist"] > 0)
        & (frame["close"] > frame["ema20"])
        & frame["rsi14"].between(50, 70)
        & frame["adx14"].between(16, 45)
        & _atr_percentile_filter(frame)
    )


def strong_candle_continuation(frame: pd.DataFrame) -> pd.Series:
    candle_range = (frame["high"] - frame["low"]).replace(0, pd.NA)
    body_ratio = ((frame["close"] - frame["open"]).abs() / candle_range).fillna(0)
    return (
        (frame["close"] > frame["open"])
        & (body_ratio >= 0.65)
        & (frame["close"] > frame["ema20"])
        & (frame["ema20"] >= frame["ema50"])
        & frame["rsi14"].between(50, 72)
        & (frame["relative_volume"] >= 1.0)
        & _atr_percentile_filter(frame)
    )


def btc_trend_filtered_momentum(frame: pd.DataFrame) -> pd.Series:
    base = volume_spike_momentum(frame) | breakout_trend(frame)
    if "btc_trend_ok" not in frame.columns:
        return pd.Series(False, index=frame.index)
    return base & frame["btc_trend_ok"]


STRATEGIES: dict[str, SignalFn] = {
    "Breakout trend-following": breakout_trend,
    "Pullback EMA20/EMA50": pullback_ema,
    "Mean reversion range": mean_reversion_range,
    "Momentum volume spike": volume_spike_momentum,
    "Strong candle continuation": strong_candle_continuation,
    "Avoid counter-BTC trend": btc_trend_filtered_momentum,
}


def _add_btc_trend(frame: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    btc = add_indicators(fetch_ohlc("BTC/USD", timeframe, RESEARCH_OHLC_LIMIT))
    btc = closed_candles(btc, timeframe)
    if btc.empty:
        frame["btc_trend_ok"] = True
        return frame
    trend = btc[["timestamp", "close", "ema200"]].copy()
    trend["btc_trend_ok"] = trend["close"] >= trend["ema200"]
    merged = pd.merge_asof(
        frame.sort_values("timestamp"),
        trend[["timestamp", "btc_trend_ok"]].sort_values("timestamp"),
        on="timestamp",
        direction="backward",
    )
    merged["btc_trend_ok"] = merged["btc_trend_ok"].fillna(True)
    return merged


def evaluate_signals(frame: pd.DataFrame, signal_mask: pd.Series, horizon_bars: int = 12) -> list[float]:
    results: list[float] = []
    signals = frame[signal_mask.fillna(False)].index.tolist()
    for index in signals:
        entry_row = frame.loc[index]
        entry = float(entry_row["close"])
        atr = float(entry_row["atr14"])
        if entry <= 0 or atr <= 0:
            continue
        target = entry + 0.8 * atr
        stop = entry - 0.8 * atr
        plan = calculate_plan(entry, stop, target)
        if plan is None:
            continue
        future = frame.loc[index + 1 : index + horizon_bars]
        for _, candle in future.iterrows():
            tp_hit = float(candle["high"]) >= target
            sl_hit = float(candle["low"]) <= stop
            if tp_hit and sl_hit:
                results.append(-abs(plan.net_loss_sl))
                break
            if sl_hit:
                results.append(-abs(plan.net_loss_sl))
                break
            if tp_hit:
                results.append(plan.net_profit_tp1)
                break
    return results


def split_results(results: list[float]) -> tuple[list[float], list[float]]:
    split_at = int(len(results) * 0.7)
    return results[:split_at], results[split_at:]


def is_valid(train: Metrics, validation: Metrics, total: Metrics) -> bool:
    return (
        total.trades >= RESEARCH_MIN_TRADES
        and train.win_rate > 55
        and train.profit_factor > 1.25
        and train.expectancy_eur > 0
        and validation.win_rate > 55
        and validation.profit_factor > 1.25
        and validation.expectancy_eur > 0
    )


def analyze_avoid_conditions(result: ResearchResult) -> list[str]:
    avoid = []
    if result.validation.max_drawdown_eur > max(TRADE_AMOUNT_EUR * 0.25, result.validation.net_profit_eur):
        avoid.append("drawdown validation troppo alto rispetto al profitto")
    if result.validation.trades < RESEARCH_MIN_TRADES * 0.3:
        avoid.append("campione validation limitato")
    return avoid or ["nessuna condizione negativa evidente sul campione validato"]


def run_research() -> str:
    candidates: list[ResearchResult] = []
    tested = 0
    for pair in PAIRS:
        for timeframe in RESEARCH_TIMEFRAMES:
            frame = add_indicators(fetch_ohlc(pair, timeframe, RESEARCH_OHLC_LIMIT))
            frame = closed_candles(frame, timeframe).reset_index(drop=True)
            if len(frame) < 220:
                continue
            frame = _add_btc_trend(frame, timeframe)
            for strategy_name, signal_fn in STRATEGIES.items():
                tested += 1
                signal_mask = signal_fn(frame)
                results = evaluate_signals(frame, signal_mask)
                train_results, validation_results = split_results(results)
                train_metrics = metrics(train_results)
                validation_metrics = metrics(validation_results)
                total_metrics = metrics(results)
                result = ResearchResult(
                    strategy=strategy_name,
                    pair=pair,
                    timeframe=timeframe,
                    train=train_metrics,
                    validation=validation_metrics,
                    total=total_metrics,
                    params={
                        "tp": "entry + 0.8 ATR",
                        "sl": "entry - 0.8 ATR",
                        "split": "70% training / 30% validation",
                        "min_trades": RESEARCH_MIN_TRADES,
                    },
                    avoid=[],
                )
                result.avoid = analyze_avoid_conditions(result)
                if is_valid(train_metrics, validation_metrics, total_metrics):
                    candidates.append(result)
    if not candidates:
        return (
            "🔬 Report ricerca strategie\n"
            f"Configurazioni testate: {tested}\n"
            f"Criterio minimo trade: {RESEARCH_MIN_TRADES}\n\n"
            "NESSUN EDGE STATISTICO TROVATO\n\n"
            "Nessuna combinazione ha superato contemporaneamente: almeno 200 trade, win rate TP1 > 55%, "
            "profit factor > 1.25, expectancy positiva e validazione profittevole.\n"
            "Segnali live operativi disattivati in RESEARCH_MODE."
        )
    best = max(candidates, key=lambda item: (item.validation.profit_factor, item.validation.net_profit_eur))
    return format_research_report(best, len(candidates), tested)


def format_research_report(best: ResearchResult, valid_count: int, tested: int) -> str:
    return (
        "🔬 Report ricerca strategie\n"
        f"Configurazioni testate: {tested}\n"
        f"Strategie validate: {valid_count}\n\n"
        f"Miglior strategia: {best.strategy}\n"
        f"Pair migliore: {best.pair}\n"
        f"Timeframe migliore: {best.timeframe}\n"
        f"Numero trade: {best.total.trades}\n"
        f"Win rate validation: {best.validation.win_rate:.2f}%\n"
        f"Profit factor validation: {best.validation.profit_factor:.2f}\n"
        f"Expectancy validation: €{best.validation.expectancy_eur:.4f}\n"
        f"Max drawdown validation: €{best.validation.max_drawdown_eur:.4f}\n"
        f"Guadagno netto validation: €{best.validation.net_profit_eur:.4f}\n"
        f"Parametri: {best.params}\n"
        f"Condizioni da evitare: {best.avoid}\n\n"
        "Segnali live operativi disattivati in RESEARCH_MODE finché una strategia validata non viene approvata manualmente."
    )
