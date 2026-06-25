"""Crypto signal worker for Kraken + Telegram.

Runs continuously, fetches public OHLCV candles from Kraken, evaluates a
simple technical strategy, and sends LONG/SHORT alerts to Telegram.
"""

from __future__ import annotations

import sys
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

import ccxt
import pandas as pd
import requests
from ccxt.base.errors import BadSymbol, DDoSProtection, ExchangeError, NetworkError, RateLimitExceeded
from dotenv import load_dotenv
from ta.momentum import RSIIndicator
from ta.trend import EMAIndicator, MACD
from ta.volatility import AverageTrueRange

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

SYMBOLS = ["BTC/USD", "ETH/USD", "SOL/USD", "XRP/USD", "BNB/USD", "DOGE/USD"]
MAIN_TIMEFRAME = "15m"
TREND_TIMEFRAME = "1h"
LOOP_SLEEP_SECONDS = 300
OHLCV_LIMIT = 300
SIGNAL_STATE_FILE = Path(os.getenv("SIGNAL_STATE_FILE", "last_signals.json"))

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | %(levelname)s | %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("crypto-signal-worker")


def create_exchange() -> ccxt.kraken:
    """Create a Kraken client using public market data only."""
    return ccxt.kraken({"enableRateLimit": True, "timeout": 30000})


def load_signal_state() -> dict[str, str]:
    """Load the last sent candle timestamp for each symbol/direction."""
    if not SIGNAL_STATE_FILE.exists():
        return {}

    try:
        with SIGNAL_STATE_FILE.open("r", encoding="utf-8") as file:
            data = json.load(file)
            return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Could not read %s: %s", SIGNAL_STATE_FILE, exc)
        return {}


def save_signal_state(state: dict[str, str]) -> None:
    """Persist signal state to avoid duplicate alerts after restarts."""
    try:
        with SIGNAL_STATE_FILE.open("w", encoding="utf-8") as file:
            json.dump(state, file, indent=2, sort_keys=True)
    except OSError as exc:
        logger.error("Could not write %s: %s", SIGNAL_STATE_FILE, exc)


def send_telegram_message(message: str) -> bool:
    """Send a Telegram message using the Bot API."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("Telegram variables are missing; message not sent")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}

    try:
        response = requests.post(url, json=payload, timeout=20)
        response.raise_for_status()
        logger.info("Telegram message sent")
        return True
    except requests.RequestException as exc:
        logger.error("Telegram send failed: %s", exc)
        return False


def fetch_ohlcv(exchange: ccxt.kraken, symbol: str, timeframe: str) -> pd.DataFrame | None:
    """Fetch OHLCV candles from Kraken and return a DataFrame."""
    try:
        rows = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=OHLCV_LIMIT)
    except BadSymbol:
        logger.warning("Symbol %s is not available on Kraken; skipping", symbol)
        return None
    except (NetworkError, RateLimitExceeded, DDoSProtection) as exc:
        logger.warning("Temporary Kraken issue for %s %s: %s", symbol, timeframe, exc)
        return None
    except ExchangeError as exc:
        logger.warning("Kraken exchange error for %s %s: %s", symbol, timeframe, exc)
        return None

    if not rows:
        logger.warning("No OHLCV data returned for %s %s", symbol, timeframe)
        return None

    frame = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
    frame["datetime"] = pd.to_datetime(frame["timestamp"], unit="ms", utc=True)
    return frame


def add_indicators(frame: pd.DataFrame) -> pd.DataFrame:
    """Add EMA, RSI, MACD, ATR, and average volume columns."""
    data = frame.copy()
    data["ema20"] = EMAIndicator(data["close"], window=20).ema_indicator()
    data["ema50"] = EMAIndicator(data["close"], window=50).ema_indicator()
    data["ema200"] = EMAIndicator(data["close"], window=200).ema_indicator()
    data["rsi14"] = RSIIndicator(data["close"], window=14).rsi()

    macd = MACD(data["close"], window_slow=26, window_fast=12, window_sign=9)
    data["macd_hist"] = macd.macd_diff()

    atr = AverageTrueRange(data["high"], data["low"], data["close"], window=14)
    data["atr14"] = atr.average_true_range()
    data["volume_avg20"] = data["volume"].rolling(window=20).mean()
    return data


def enough_indicator_data(main_data: pd.DataFrame, trend_data: pd.DataFrame) -> bool:
    """Ensure latest rows contain all indicators needed by the strategy."""
    required_main = ["ema20", "ema50", "ema200", "rsi14", "macd_hist", "atr14", "volume_avg20"]
    required_trend = ["ema200"]
    return not main_data[required_main].tail(2).isna().any().any() and not trend_data[required_trend].tail(1).isna().any().any()


def calculate_signal(symbol: str, main_data: pd.DataFrame, trend_data: pd.DataFrame) -> dict[str, Any] | None:
    """Evaluate strategy conditions and return a signal dictionary if valid."""
    if len(main_data) < 2 or len(trend_data) < 1 or not enough_indicator_data(main_data, trend_data):
        logger.info("Not enough indicator data for %s", symbol)
        return None

    previous = main_data.iloc[-2]
    latest = main_data.iloc[-1]
    trend = trend_data.iloc[-1]

    bullish_1h = trend["close"] > trend["ema200"]
    bearish_1h = trend["close"] < trend["ema200"]
    bullish_emas = latest["ema20"] > latest["ema50"] > latest["ema200"]
    bearish_emas = latest["ema20"] < latest["ema50"] < latest["ema200"]
    volume_confirmed = latest["volume"] > latest["volume_avg20"]
    macd_cross_up = previous["macd_hist"] < 0 and latest["macd_hist"] > 0
    macd_cross_down = previous["macd_hist"] > 0 and latest["macd_hist"] < 0

    long_conditions = bullish_1h and bullish_emas and 50 <= latest["rsi14"] <= 70 and macd_cross_up and volume_confirmed
    short_conditions = bearish_1h and bearish_emas and 30 <= latest["rsi14"] <= 50 and macd_cross_down and volume_confirmed

    if not long_conditions and not short_conditions:
        logger.info("No signal for %s", symbol)
        return None

    direction = "LONG" if long_conditions else "SHORT"
    entry = float(latest["close"])
    atr = float(latest["atr14"])
    risk = 1.5 * atr

    if direction == "LONG":
        stop_loss = entry - risk
        target_1 = entry + 1.5 * risk
        target_2 = entry + 3 * risk
    else:
        stop_loss = entry + risk
        target_1 = entry - 1.5 * risk
        target_2 = entry - 3 * risk

    return {
        "symbol": symbol,
        "direction": direction,
        "entry": entry,
        "stop_loss": stop_loss,
        "target_1": target_1,
        "target_2": target_2,
        "candle_timestamp": str(int(latest["timestamp"])),
    }


def format_signal_message(signal: dict[str, Any]) -> str:
    """Build a readable Telegram signal message."""
    is_long = signal["direction"] == "LONG"
    icon = "🟢" if is_long else "🔴"
    trend_text = "rialzista" if is_long else "ribassista"
    ema_reason = "EMA 20 > EMA 50 > EMA 200" if is_long else "EMA 20 < EMA 50 < EMA 200"
    rsi_reason = "RSI in zona positiva" if is_long else "RSI in zona negativa"
    macd_reason = "MACD histogram positivo" if is_long else "MACD histogram negativo"
    price_reason = "Prezzo sopra EMA 200 su 1h" if is_long else "Prezzo sotto EMA 200 su 1h"

    return (
        f"{icon} {signal['direction']} {signal['symbol']}\n"
        f"Entry: {signal['entry']:.2f}\n"
        f"Stop Loss: {signal['stop_loss']:.2f}\n"
        f"Target 1: {signal['target_1']:.2f}\n"
        f"Target 2: {signal['target_2']:.2f}\n"
        f"Timeframe: {MAIN_TIMEFRAME}\n"
        f"Conferma: trend 1h {trend_text}\n"
        "Motivi:\n"
        f"- {price_reason}\n"
        f"- {ema_reason}\n"
        f"- {rsi_reason}\n"
        f"- {macd_reason}\n"
        "- Volume superiore alla media"
    )


def process_symbol(exchange: ccxt.kraken, symbol: str, signal_state: dict[str, str]) -> None:
    """Fetch data, evaluate strategy, and send a Telegram signal if needed."""
    logger.info("Checking %s", symbol)
    main_frame = fetch_ohlcv(exchange, symbol, MAIN_TIMEFRAME)
    trend_frame = fetch_ohlcv(exchange, symbol, TREND_TIMEFRAME)
    if main_frame is None or trend_frame is None:
        return

    signal = calculate_signal(symbol, add_indicators(main_frame), add_indicators(trend_frame))
    if signal is None:
        return

    state_key = f"{symbol}:{signal['direction']}"
    if signal_state.get(state_key) == signal["candle_timestamp"]:
        logger.info("Duplicate %s signal for %s on candle %s; skipping", signal["direction"], symbol, signal["candle_timestamp"])
        return

    if send_telegram_message(format_signal_message(signal)):
        signal_state[state_key] = signal["candle_timestamp"]
        save_signal_state(signal_state)
        logger.info("Stored %s signal state for %s", signal["direction"], symbol)


def run_worker() -> None:
    """Run the Railway-ready worker forever."""
    logger.info("Starting Kraken crypto signal worker")
    exchange = create_exchange()
    signal_state = load_signal_state()

    try:
        exchange.load_markets()
    except (NetworkError, RateLimitExceeded, DDoSProtection, ExchangeError) as exc:
        logger.warning("Could not pre-load Kraken markets: %s", exc)

    send_telegram_message("🤖 Crypto signal bot avviato. Monitoraggio Kraken attivo.")

    while True:
        cycle_started = time.time()
        logger.info("Starting scan cycle")
        for symbol in SYMBOLS:
            try:
                process_symbol(exchange, symbol, signal_state)
            except Exception as exc:  # Defensive guard so one symbol never kills the worker.
                logger.exception("Unexpected error while processing %s: %s", symbol, exc)

        elapsed = time.time() - cycle_started
        sleep_for = max(0, LOOP_SLEEP_SECONDS - elapsed)
        logger.info("Scan cycle finished in %.1fs; sleeping %.1fs", elapsed, sleep_for)
        time.sleep(sleep_for)


if __name__ == "__main__":
    run_worker()
