"""Project Alpha research: price action / market structure / liquidity backtests."""
from __future__ import annotations

import csv
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Callable

import pandas as pd

from config import ALPHA_PAIRS, ALPHA_REPORT_CSV, ALPHA_REPORT_SUMMARY, ALPHA_TIMEFRAMES, RESEARCH_OHLC_LIMIT, TRADE_AMOUNT_EUR
from exchange import fetch_ohlc
from indicators import add_indicators
from risk import calculate_plan
from strategy import closed_candles

SignalBuilder = Callable[[pd.DataFrame], pd.DataFrame]


@dataclass
class AlphaMetrics:
    trades: int
    win_rate: float
    profit_factor: float
    expectancy_eur: float
    max_drawdown_eur: float
    net_profit_eur: float
    avg_profit_eur: float
    avg_loss_eur: float


@dataclass
class AlphaResult:
    strategy: str
    pair: str
    timeframe: str
    train_trades: int
    train_win_rate: float
    train_profit_factor: float
    train_expectancy_eur: float
    validation_trades: int
    validation_win_rate: float
    validation_profit_factor: float
    validation_expectancy_eur: float
    total_trades: int
    total_net_profit_eur: float
    total_max_drawdown_eur: float
    avg_profit_eur: float
    avg_loss_eur: float
    best_hour: str
    worst_hour: str
    validated: bool
    conditions: str


def add_market_map(frame: pd.DataFrame) -> pd.DataFrame:
    """Add market-structure and liquidity context without using indicators as entry triggers."""
    data = frame.copy()
    data["swing_high_20"] = data["high"].rolling(20).max().shift(1)
    data["swing_low_20"] = data["low"].rolling(20).min().shift(1)
    data["resistance"] = data["high"].rolling(50).max().shift(1)
    data["support"] = data["low"].rolling(50).min().shift(1)
    data["range_high"] = data["high"].rolling(80).max().shift(1)
    data["range_low"] = data["low"].rolling(80).min().shift(1)
    data["range_mid"] = (data["range_high"] + data["range_low"]) / 2
    data["liquidity_above"] = data["swing_high_20"]
    data["liquidity_below"] = data["swing_low_20"]
    data["daily_high"] = data.groupby(data["timestamp"].dt.date)["high"].transform("max")
    data["daily_low"] = data.groupby(data["timestamp"].dt.date)["low"].transform("min")
    data["prev_day_high"] = data["daily_high"].shift(1)
    data["prev_day_low"] = data["daily_low"].shift(1)
    data["weekly_high"] = data["high"].rolling(7 * 24).max().shift(1)
    data["weekly_low"] = data["low"].rolling(7 * 24).min().shift(1)
    data["atr_pct"] = data["atr14"] / data["close"] * 100
    data["body"] = (data["close"] - data["open"]).abs()
    data["candle_range"] = (data["high"] - data["low"]).replace(0, pd.NA)
    data["body_ratio"] = (data["body"] / data["candle_range"]).fillna(0)
    data["hour"] = data["timestamp"].dt.hour
    return data


def classify_alpha_regime(row: pd.Series) -> str:
    """Classify market regime from structure, ATR and ADX as a secondary filter."""
    if pd.isna(row.get("support")) or pd.isna(row.get("resistance")) or row.get("close", 0) <= 0:
        return "NO_TRADE"
    range_pct = (row["resistance"] - row["support"]) / row["close"] * 100
    if row["atr_pct"] > 5:
        return "NO_TRADE"
    if range_pct < 0.8:
        return "COMPRESSION"
    if row["close"] > row["resistance"] and row["relative_volume"] > 1.2:
        return "BREAKOUT"
    if row["adx14"] < 18:
        return "RANGE"
    if row["close"] > row["swing_high_20"]:
        return "TREND_UP"
    if row["close"] < row["swing_low_20"]:
        return "TREND_DOWN"
    return "RANGE"


def with_regime(frame: pd.DataFrame) -> pd.DataFrame:
    data = frame.copy()
    data["alpha_regime"] = data.apply(classify_alpha_regime, axis=1)
    return data


def _signals_from_mask(frame: pd.DataFrame, mask: pd.Series, entry: pd.Series, stop: pd.Series, target: pd.Series, conditions: str) -> pd.DataFrame:
    signals = frame.loc[mask.fillna(False), ["timestamp", "hour"]].copy()
    signals["entry"] = entry.loc[signals.index]
    signals["stop"] = stop.loc[signals.index]
    signals["target"] = target.loc[signals.index]
    signals["conditions"] = conditions
    valid = (signals["stop"] < signals["entry"]) & (signals["entry"] < signals["target"])
    return signals[valid]


def liquidity_sweep_long(frame: pd.DataFrame) -> pd.DataFrame:
    swept_low = frame["low"] < frame["liquidity_below"]
    recovered = frame["close"] > frame["liquidity_below"]
    reaction = (frame["close"] > frame["open"]) & (frame["body_ratio"] >= 0.45)
    volume = frame["relative_volume"] >= 1.0
    mask = swept_low & recovered & reaction & volume & frame["alpha_regime"].isin(["RANGE", "COMPRESSION", "TREND_UP"])
    entry = frame["high"]
    stop = frame["low"] - 0.05 * frame["atr14"]
    target = frame[["resistance", "liquidity_above", "range_mid"]].max(axis=1)
    return _signals_from_mask(frame, mask, entry, stop, target, "sweep minimo + recupero + candela positiva + volume")


def breakout_retest_long(frame: pd.DataFrame) -> pd.DataFrame:
    level = frame["resistance"].shift(1)
    recent_breakout = (frame["close"].shift(1) > level).rolling(5).max().fillna(False).astype(bool)
    retest = (frame["low"] <= level) & (frame["close"] > level)
    confirmation = (frame["close"] > frame["open"]) & (frame["body_ratio"] >= 0.4)
    mask = recent_breakout & retest & confirmation & (frame["relative_volume"] >= 0.8)
    entry = frame["high"]
    stop = level - 0.1 * frame["atr14"]
    target = frame[["liquidity_above", "weekly_high"]].max(axis=1)
    return _signals_from_mask(frame, mask, entry, stop, target, "breakout resistenza + retest dall'alto + conferma bullish")


def pullback_trend_long(frame: pd.DataFrame) -> pd.DataFrame:
    trend_up = (frame["close"] > frame["ema200"]) & (frame["swing_high_20"] > frame["swing_high_20"].shift(10))
    value_area = (frame["low"] <= frame["support"] + 0.35 * frame["atr14"]) | (frame["low"] <= frame["swing_low_20"] + 0.35 * frame["atr14"])
    impulse = (frame["close"] > frame["open"]) & (frame["body_ratio"] >= 0.55) & (frame["close"] > frame["high"].shift(1))
    mask = trend_up & value_area & impulse & (frame["relative_volume"] >= 0.8)
    entry = frame["high"]
    stop = frame[["support", "swing_low_20"]].min(axis=1) - 0.1 * frame["atr14"]
    target = frame[["swing_high_20", "resistance"]].max(axis=1)
    return _signals_from_mask(frame, mask, entry, stop, target, "trend up + pullback area valore + candela impulsiva")


def range_reversal_long(frame: pd.DataFrame) -> pd.DataFrame:
    false_break = frame["low"] < frame["range_low"]
    recovered = frame["close"] > frame["range_low"]
    mask = false_break & recovered & frame["alpha_regime"].isin(["RANGE", "COMPRESSION"]) & (frame["relative_volume"] >= 0.8)
    entry = frame["range_low"]
    stop = frame["low"] - 0.05 * frame["atr14"]
    target = frame["range_mid"]
    return _signals_from_mask(frame, mask, entry, stop, target, "range low + falso breakdown + recupero range")


ALPHA_STRATEGIES: dict[str, SignalBuilder] = {
    "Liquidity Sweep Long": liquidity_sweep_long,
    "Breakout Retest Long": breakout_retest_long,
    "Pullback Trend Long": pullback_trend_long,
    "Range Reversal Long": range_reversal_long,
}


def _max_drawdown(results: list[float]) -> float:
    equity = TRADE_AMOUNT_EUR
    peak = equity
    max_dd = 0.0
    for result in results:
        equity += result
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    return max_dd


def _metrics(results: list[float]) -> AlphaMetrics:
    if not results:
        return AlphaMetrics(0, 0, 0, 0, 0, 0, 0, 0)
    wins = [value for value in results if value > 0]
    losses = [value for value in results if value < 0]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    return AlphaMetrics(
        trades=len(results),
        win_rate=len(wins) / len(results) * 100,
        profit_factor=gross_profit / gross_loss if gross_loss else (gross_profit if gross_profit else 0),
        expectancy_eur=sum(results) / len(results),
        max_drawdown_eur=_max_drawdown(results),
        net_profit_eur=sum(results),
        avg_profit_eur=gross_profit / len(wins) if wins else 0,
        avg_loss_eur=gross_loss / len(losses) if losses else 0,
    )


def backtest_signals(frame: pd.DataFrame, signals: pd.DataFrame, horizon_bars: int = 24) -> tuple[list[float], dict[int, float]]:
    results: list[float] = []
    by_hour: dict[int, float] = {}
    for index, signal in signals.iterrows():
        plan = calculate_plan(float(signal["entry"]), float(signal["stop"]), float(signal["target"]))
        if plan is None:
            continue
        future = frame.loc[index + 1 : index + horizon_bars]
        entered = False
        for _, candle in future.iterrows():
            if not entered:
                if float(candle["high"]) < plan.entry:
                    continue
                entered = True
            tp_hit = float(candle["high"]) >= plan.target_1
            sl_hit = float(candle["low"]) <= plan.stop_loss
            if tp_hit and sl_hit:
                result = -abs(plan.net_loss_sl)
            elif sl_hit:
                result = -abs(plan.net_loss_sl)
            elif tp_hit:
                result = plan.net_profit_tp1
            else:
                continue
            results.append(result)
            hour = int(signal["hour"])
            by_hour[hour] = by_hour.get(hour, 0.0) + result
            break
    return results, by_hour


def _best_worst_hour(by_hour: dict[int, float]) -> tuple[str, str]:
    if not by_hour:
        return "n/a", "n/a"
    ranked = sorted(by_hour.items(), key=lambda item: item[1], reverse=True)
    return str(ranked[0][0]), str(ranked[-1][0])


def _split(results: list[float]) -> tuple[list[float], list[float]]:
    split_at = int(len(results) * 0.7)
    return results[:split_at], results[split_at:]


def _is_valid(train: AlphaMetrics, validation: AlphaMetrics) -> bool:
    return (
        train.trades >= 100
        and train.profit_factor > 1.20
        and train.win_rate > 52
        and train.expectancy_eur > 0
        and validation.net_profit_eur > 0
        and validation.profit_factor > 1.10
    )


def run_project_alpha_research() -> tuple[list[AlphaResult], str]:
    results: list[AlphaResult] = []
    for pair in ALPHA_PAIRS:
        for timeframe in ALPHA_TIMEFRAMES:
            frame = add_indicators(fetch_ohlc(pair, timeframe, RESEARCH_OHLC_LIMIT))
            frame = closed_candles(frame, timeframe).reset_index(drop=True)
            if len(frame) < 220:
                continue
            frame = with_regime(add_market_map(frame))
            for strategy_name, builder in ALPHA_STRATEGIES.items():
                signals = builder(frame)
                trade_results, by_hour = backtest_signals(frame, signals)
                train_results, validation_results = _split(trade_results)
                train = _metrics(train_results)
                validation = _metrics(validation_results)
                total = _metrics(trade_results)
                best_hour, worst_hour = _best_worst_hour(by_hour)
                conditions = signals["conditions"].iloc[0] if not signals.empty else "nessun setup"
                results.append(
                    AlphaResult(
                        strategy=strategy_name,
                        pair=pair,
                        timeframe=timeframe,
                        train_trades=train.trades,
                        train_win_rate=train.win_rate,
                        train_profit_factor=train.profit_factor,
                        train_expectancy_eur=train.expectancy_eur,
                        validation_trades=validation.trades,
                        validation_win_rate=validation.win_rate,
                        validation_profit_factor=validation.profit_factor,
                        validation_expectancy_eur=validation.expectancy_eur,
                        total_trades=total.trades,
                        total_net_profit_eur=total.net_profit_eur,
                        total_max_drawdown_eur=total.max_drawdown_eur,
                        avg_profit_eur=total.avg_profit_eur,
                        avg_loss_eur=total.avg_loss_eur,
                        best_hour=best_hour,
                        worst_hour=worst_hour,
                        validated=_is_valid(train, validation),
                        conditions=conditions,
                    )
                )
    summary = write_alpha_reports(results)
    return results, summary


def write_alpha_reports(results: list[AlphaResult]) -> str:
    csv_path = Path(ALPHA_REPORT_CSV)
    summary_path = Path(ALPHA_REPORT_SUMMARY)
    csv_path.parent.mkdir(parents=True, exist_ok=True) if csv_path.parent != Path(".") else None
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(asdict(results[0]).keys()) if results else list(AlphaResult.__dataclass_fields__.keys()))
        writer.writeheader()
        for result in results:
            writer.writerow(asdict(result))

    validated = [result for result in results if result.validated]
    if not validated:
        summary = (
            "PROJECT ALPHA RESEARCH SUMMARY\n"
            "NESSUN EDGE VALIDATO\n\n"
            "Nessuna strategia price action / market structure ha superato i criteri minimi su training e validation.\n"
            "Segnali live: NON ABILITATI.\n"
            f"Configurazioni testate: {len(results)}\n"
        )
    else:
        best = max(validated, key=lambda item: (item.validation_profit_factor, item.total_net_profit_eur))
        summary = (
            "PROJECT ALPHA RESEARCH SUMMARY\n"
            "EDGE VALIDATO\n\n"
            f"Miglior strategia: {best.strategy}\n"
            f"Pair: {best.pair}\n"
            f"Timeframe: {best.timeframe}\n"
            f"Condizioni: {best.conditions}\n"
            f"Training: {best.train_trades} trade | WR {best.train_win_rate:.2f}% | PF {best.train_profit_factor:.2f}\n"
            f"Validation: {best.validation_trades} trade | WR {best.validation_win_rate:.2f}% | PF {best.validation_profit_factor:.2f}\n"
            f"Expectancy validation: €{best.validation_expectancy_eur:.4f}\n"
            f"Max drawdown totale: €{best.total_max_drawdown_eur:.4f}\n"
            f"Saldo netto totale: €{best.total_net_profit_eur:.4f}\n"
            f"Miglior ora: {best.best_hour}\n"
            f"Peggior ora: {best.worst_hour}\n"
            "Può essere trasformata in segnale live: SOLO dopo revisione manuale.\n"
        )
    summary_path.write_text(summary, encoding="utf-8")
    return summary


if __name__ == "__main__":
    _, output = run_project_alpha_research()
    print(output)
