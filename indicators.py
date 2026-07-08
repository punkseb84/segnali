"""Technical indicators and market-regime helpers."""
from __future__ import annotations

import numpy as np
import pandas as pd


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss.replace(0, np.nan)
    return (100 - (100 / (1 + rs))).fillna(50)


def macd(series: pd.Series) -> tuple[pd.Series, pd.Series, pd.Series]:
    macd_line = ema(series, 12) - ema(series, 26)
    signal = ema(macd_line, 9)
    hist = macd_line - signal
    return macd_line, signal, hist


def atr(frame: pd.DataFrame, period: int = 14) -> pd.Series:
    previous_close = frame["close"].shift(1)
    tr = pd.concat(
        [(frame["high"] - frame["low"]).abs(), (frame["high"] - previous_close).abs(), (frame["low"] - previous_close).abs()],
        axis=1,
    ).max(axis=1)
    return tr.rolling(period).mean()


def adx(frame: pd.DataFrame, period: int = 14) -> pd.Series:
    up_move = frame["high"].diff()
    down_move = -frame["low"].diff()
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
    atr_values = atr(frame, period).replace(0, np.nan)
    plus_di = 100 * pd.Series(plus_dm, index=frame.index).rolling(period).sum() / atr_values
    minus_di = 100 * pd.Series(minus_dm, index=frame.index).rolling(period).sum() / atr_values
    dx = ((plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)) * 100
    return dx.rolling(period).mean().fillna(0)


def add_indicators(frame: pd.DataFrame) -> pd.DataFrame:
    """Add all indicators required by the strategy."""
    data = frame.copy()
    if data.empty:
        return data
    data["ema20"] = ema(data["close"], 20)
    data["ema50"] = ema(data["close"], 50)
    data["ema200"] = ema(data["close"], 200)
    data["rsi14"] = rsi(data["close"], 14)
    data["macd"], data["macd_signal"], data["macd_hist"] = macd(data["close"])
    data["atr14"] = atr(data, 14)
    data["adx14"] = adx(data, 14)
    data["avg_volume_20"] = data["volume"].rolling(20).mean()
    data["relative_volume"] = data["volume"] / data["avg_volume_20"].replace(0, np.nan)
    data["support"] = data["low"].rolling(20).min()
    data["resistance"] = data["high"].rolling(20).max()
    data["swing_high"] = data["high"].rolling(5).max()
    data["swing_low"] = data["low"].rolling(5).min()
    return data


def classify_market_regime(data: pd.DataFrame) -> str:
    """Classify the latest market regime."""
    if data.empty or len(data) < 220:
        return "NO_TRADE"
    latest = data.iloc[-1]
    required = ["ema20", "ema50", "ema200", "adx14", "atr14", "close"]
    if any(pd.isna(latest.get(column)) for column in required):
        return "NO_TRADE"
    atr_pct = latest["atr14"] / latest["close"] * 100 if latest["close"] else 0
    atr_pct_series = data["atr14"] / data["close"] * 100
    atr_avg = atr_pct_series.tail(100).mean()
    if atr_pct > atr_avg * 2.2:
        return "HIGH_VOLATILITY"
    if atr_pct < max(0.05, atr_avg * 0.35):
        return "LOW_VOLATILITY"
    if latest["ema20"] > latest["ema50"] > latest["ema200"] and latest["adx14"] > 22:
        return "TREND_STRONG"
    close_to_emas = abs(latest["close"] - latest["ema20"]) / latest["close"] < 0.01
    if latest["adx14"] < 18 and close_to_emas:
        return "RANGE"
    return "TREND_STRONG" if latest["close"] > latest["ema200"] else "RANGE"
