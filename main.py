"""Crypto signal worker for Kraken + Telegram.

Runs continuously, fetches public OHLCV candles from Kraken, evaluates a
simple technical strategy, sends LONG/SHORT alerts to Telegram, tracks
signals/trades, and sends a daily Telegram report.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from datetime import datetime, time as datetime_time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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

SYMBOLS = [
    "BTC/USD",
    "ETH/USD",
    "SOL/USD",
    "XRP/USD",
    "BNB/USD",
    "DOGE/USD",
    "ADA/USD",
    "AVAX/USD",
    "DOT/USD",
    "LINK/USD",
    "LTC/USD",
    "BCH/USD",
    "XLM/USD",
    "TRX/USD",
    "UNI/USD",
    "AAVE/USD",
    "ATOM/USD",
    "NEAR/USD",
    "FIL/USD",
    "ETC/USD",
]
MAIN_TIMEFRAME = "15m"
TREND_TIMEFRAME = "1h"
LOOP_SLEEP_SECONDS = 300
OHLCV_LIMIT = 300
SIGNAL_STATE_FILE = Path(os.getenv("SIGNAL_STATE_FILE", "last_signals.json"))
SIGNAL_HISTORY_FILE = Path(os.getenv("SIGNAL_HISTORY_FILE", "signal_history.json"))
TRADE_STATE_FILE = Path(os.getenv("TRADE_STATE_FILE", "trades.json"))
REPORT_STATE_FILE = Path(os.getenv("REPORT_STATE_FILE", "report_state.json"))
DAILY_REPORT_TIME = os.getenv("DAILY_REPORT_TIME", "09:00").strip()
REPORT_TIMEZONE = os.getenv("REPORT_TIMEZONE", "Europe/Rome").strip()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | %(levelname)s | %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("crypto-signal-worker")


def redact_secret(text: str, secret: str) -> str:
    """Remove sensitive tokens from text before writing logs."""
    if not secret:
        return text
    return text.replace(secret, "***REDACTED***")


def utc_now() -> datetime:
    """Return the current UTC datetime with timezone information."""
    return datetime.now(timezone.utc)


def utc_now_iso() -> str:
    """Return the current UTC timestamp in ISO-8601 format."""
    return utc_now().isoformat()


def parse_iso_datetime(value: str) -> datetime | None:
    """Parse an ISO datetime and return an aware UTC datetime when possible."""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def create_exchange() -> ccxt.kraken:
    """Create a Kraken client using public market data only."""
    return ccxt.kraken({"enableRateLimit": True, "timeout": 30000})


def load_json_file(path: Path, default: Any) -> Any:
    """Load JSON data from disk, returning default when the file is missing/broken."""
    if not path.exists():
        return default

    try:
        with path.open("r", encoding="utf-8") as file:
            return json.load(file)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Could not read %s: %s", path, exc)
        return default


def save_json_file(path: Path, data: Any) -> None:
    """Save JSON data to disk."""
    try:
        with path.open("w", encoding="utf-8") as file:
            json.dump(data, file, indent=2, sort_keys=True)
    except OSError as exc:
        logger.error("Could not write %s: %s", path, exc)


def load_signal_state() -> dict[str, str]:
    """Load the last sent candle timestamp for each symbol/direction."""
    data = load_json_file(SIGNAL_STATE_FILE, {})
    return data if isinstance(data, dict) else {}


def save_signal_state(state: dict[str, str]) -> None:
    """Persist signal state to avoid duplicate alerts after restarts."""
    save_json_file(SIGNAL_STATE_FILE, state)


def load_signal_history() -> list[dict[str, Any]]:
    """Load the historical list of Telegram signals."""
    data = load_json_file(SIGNAL_HISTORY_FILE, [])
    return data if isinstance(data, list) else []


def save_signal_history(history: list[dict[str, Any]]) -> None:
    """Save the historical list of Telegram signals."""
    save_json_file(SIGNAL_HISTORY_FILE, history)


def append_signal_history(history: list[dict[str, Any]], signal: dict[str, Any]) -> None:
    """Append a sent signal to local history for reporting."""
    history.append(
        {
            "sent_at": utc_now_iso(),
            "symbol": signal["symbol"],
            "direction": signal["direction"],
            "entry": signal["entry"],
            "stop_loss": signal["stop_loss"],
            "target_1": signal["target_1"],
            "target_2": signal["target_2"],
            "candle_timestamp": signal["candle_timestamp"],
            "timeframe": MAIN_TIMEFRAME,
            "trend_timeframe": TREND_TIMEFRAME,
        }
    )
    save_signal_history(history)


def load_trades() -> list[dict[str, Any]]:
    """Load theoretical trade tracking state."""
    data = load_json_file(TRADE_STATE_FILE, [])
    return data if isinstance(data, list) else []


def save_trades(trades: list[dict[str, Any]]) -> None:
    """Save theoretical trade tracking state."""
    save_json_file(TRADE_STATE_FILE, trades)


def load_report_state() -> dict[str, Any]:
    """Load daily report state."""
    data = load_json_file(REPORT_STATE_FILE, {})
    return data if isinstance(data, dict) else {}


def save_report_state(state: dict[str, Any]) -> None:
    """Save daily report state."""
    save_json_file(REPORT_STATE_FILE, state)


def parse_daily_report_time(value: str) -> datetime_time:
    """Parse HH:MM report time, falling back to 09:00 on invalid values."""
    try:
        hour_text, minute_text = value.split(":", maxsplit=1)
        return datetime_time(hour=int(hour_text), minute=int(minute_text))
    except (ValueError, TypeError):
        logger.warning("Invalid DAILY_REPORT_TIME=%r; using 09:00", value)
        return datetime_time(hour=9, minute=0)


def get_report_timezone() -> ZoneInfo:
    """Return the configured report timezone, falling back to Europe/Rome."""
    try:
        return ZoneInfo(REPORT_TIMEZONE)
    except ZoneInfoNotFoundError:
        logger.warning("Invalid REPORT_TIMEZONE=%r; using Europe/Rome", REPORT_TIMEZONE)
        return ZoneInfo("Europe/Rome")


def send_telegram_message(message: str) -> bool:
    """Send a Telegram message using the Bot API."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("Telegram variables are missing; message not sent")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}

    try:
        response = requests.post(url, json=payload, timeout=20)
        if not response.ok:
            logger.error(
                "Telegram send failed: status=%s response=%s",
                response.status_code,
                response.text[:500],
            )
            return False

        logger.info("Telegram message sent")
        return True
    except requests.RequestException as exc:
        safe_error = redact_secret(str(exc), TELEGRAM_BOT_TOKEN)
        logger.error("Telegram send failed before response: %s", safe_error)
        return False


def fetch_ohlcv(exchange: ccxt.kraken, symbol: str, timeframe: str) -> pd.DataFrame | None:
    """Fetch closed OHLCV candles from Kraken and return a DataFrame."""
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

    # The newest exchange candle can still be forming. Excluding it makes the
    # strategy work only with fully closed candles without changing its rules.
    if len(rows) > 1:
        rows = rows[:-1]

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
        "risk": risk,
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


def create_trade(signal: dict[str, Any]) -> dict[str, Any]:
    """Create a theoretical trade from a sent signal."""
    return {
        "id": f"{signal['symbol']}:{signal['direction']}:{signal['candle_timestamp']}",
        "symbol": signal["symbol"],
        "direction": signal["direction"],
        "entry": signal["entry"],
        "stop_loss": signal["stop_loss"],
        "target_1": signal["target_1"],
        "target_2": signal["target_2"],
        "risk": signal["risk"],
        "candle_timestamp": signal["candle_timestamp"],
        "timeframe": MAIN_TIMEFRAME,
        "trend_timeframe": TREND_TIMEFRAME,
        "status": "open",
        "target_1_hit": False,
        "target_2_hit": False,
        "stop_loss_hit": False,
        "opened_at": utc_now_iso(),
        "closed_at": None,
        "result_r": None,
    }


def format_trade_update_message(trade: dict[str, Any], event: str) -> str:
    """Format Telegram update when a theoretical trade reaches target/stop."""
    icon = "✅" if event.startswith("TARGET") else "🛑"
    status_text = {
        "TARGET_1": "Target 1 raggiunto",
        "TARGET_2": "Target 2 raggiunto",
        "STOP_LOSS": "Stop Loss raggiunto",
    }[event]
    result = trade.get("result_r")
    result_line = f"\nRisultato teorico: {result:.2f}R" if isinstance(result, (int, float)) else ""
    return (
        f"{icon} {status_text}\n"
        f"{trade['direction']} {trade['symbol']}\n"
        f"Entry: {trade['entry']:.2f}\n"
        f"Stop Loss: {trade['stop_loss']:.2f}\n"
        f"Target 1: {trade['target_1']:.2f}\n"
        f"Target 2: {trade['target_2']:.2f}"
        f"{result_line}"
    )


def update_trade_with_candle(trade: dict[str, Any], candle: pd.Series) -> str | None:
    """Update one open theoretical trade using a closed candle."""
    high = float(candle["high"])
    low = float(candle["low"])
    direction = trade["direction"]

    if direction == "LONG":
        stop_hit = low <= trade["stop_loss"]
        target_1_hit = high >= trade["target_1"]
        target_2_hit = high >= trade["target_2"]
    else:
        stop_hit = high >= trade["stop_loss"]
        target_1_hit = low <= trade["target_1"]
        target_2_hit = low <= trade["target_2"]

    # Conservative assumption when stop and target happen inside the same candle.
    if stop_hit:
        trade["status"] = "closed"
        trade["stop_loss_hit"] = True
        trade["closed_at"] = utc_now_iso()
        trade["result_r"] = 0.25 if trade.get("target_1_hit") else -1.0
        return "STOP_LOSS"

    if target_2_hit:
        trade["target_1_hit"] = True
        trade["target_2_hit"] = True
        trade["status"] = "closed"
        trade["closed_at"] = utc_now_iso()
        trade["result_r"] = 2.25
        return "TARGET_2"

    if target_1_hit and not trade.get("target_1_hit"):
        trade["target_1_hit"] = True
        return "TARGET_1"

    return None


def update_open_trades_for_symbol(symbol: str, frame: pd.DataFrame, trades: list[dict[str, Any]]) -> None:
    """Check open theoretical trades for one symbol against the latest candle."""
    if frame.empty:
        return

    latest_candle = frame.iloc[-1]
    changed = False
    for trade in trades:
        if trade.get("symbol") != symbol or trade.get("status") != "open":
            continue

        event = update_trade_with_candle(trade, latest_candle)
        if event is None:
            continue

        changed = True
        if send_telegram_message(format_trade_update_message(trade, event)):
            logger.info("Trade update sent for %s %s: %s", trade["symbol"], trade["direction"], event)

    if changed:
        save_trades(trades)


def get_recent_signals(history: list[dict[str, Any]], now_utc: datetime, hours: int = 24) -> list[dict[str, Any]]:
    """Return signals sent within the last N hours."""
    since = now_utc - timedelta(hours=hours)
    recent = []
    for item in history:
        sent_at = parse_iso_datetime(str(item.get("sent_at", "")))
        if sent_at and sent_at >= since:
            recent.append(item)
    return recent


def get_recent_closed_trades(trades: list[dict[str, Any]], now_utc: datetime, hours: int = 24) -> list[dict[str, Any]]:
    """Return trades closed within the last N hours."""
    since = now_utc - timedelta(hours=hours)
    recent = []
    for trade in trades:
        closed_at = parse_iso_datetime(str(trade.get("closed_at", "")))
        if closed_at and closed_at >= since:
            recent.append(trade)
    return recent


def format_daily_report(history: list[dict[str, Any]], trades: list[dict[str, Any]], now_local: datetime) -> str:
    """Build the daily Telegram report message."""
    now_utc = now_local.astimezone(timezone.utc)
    recent_signals = get_recent_signals(history, now_utc)
    recent_closed_trades = get_recent_closed_trades(trades, now_utc)
    open_trades = [trade for trade in trades if trade.get("status") == "open"]

    long_count = sum(1 for signal in recent_signals if signal.get("direction") == "LONG")
    short_count = sum(1 for signal in recent_signals if signal.get("direction") == "SHORT")
    target_1_count = sum(1 for trade in recent_closed_trades if trade.get("target_1_hit"))
    target_2_count = sum(1 for trade in recent_closed_trades if trade.get("target_2_hit"))
    stop_count = sum(1 for trade in recent_closed_trades if trade.get("stop_loss_hit"))
    result_r = sum(float(trade.get("result_r") or 0) for trade in recent_closed_trades)

    per_symbol: dict[str, int] = {}
    for signal in recent_signals:
        symbol = str(signal.get("symbol", "UNKNOWN"))
        per_symbol[symbol] = per_symbol.get(symbol, 0) + 1

    lines = [
        "📊 Report giornaliero segnali",
        f"Ora report: {now_local.strftime('%Y-%m-%d %H:%M %Z')}",
        "Periodo: ultime 24 ore",
        f"Segnali: {len(recent_signals)}",
        f"LONG: {long_count}",
        f"SHORT: {short_count}",
        f"Trade aperti: {len(open_trades)}",
        f"Trade chiusi ultime 24h: {len(recent_closed_trades)}",
        f"Target 1 presi: {target_1_count}",
        f"Target 2 presi: {target_2_count}",
        f"Stop Loss presi: {stop_count}",
        f"Risultato teorico: {result_r:.2f}R",
    ]

    if not recent_signals:
        lines.append("\nNessun segnale generato nelle ultime 24 ore.")
        return "\n".join(lines)

    lines.append("\nPer coppia:")
    for symbol, count in sorted(per_symbol.items()):
        lines.append(f"- {symbol}: {count}")

    lines.append("\nUltimi segnali:")
    for signal in recent_signals[-10:]:
        icon = "🟢" if signal.get("direction") == "LONG" else "🔴"
        lines.append(f"- {icon} {signal.get('direction')} {signal.get('symbol')} @ {float(signal.get('entry', 0)):.2f}")

    return "\n".join(lines)


def should_send_daily_report(now_local: datetime, report_state: dict[str, Any]) -> bool:
    """Return True when the daily report should be sent."""
    report_time = parse_daily_report_time(DAILY_REPORT_TIME)
    today = now_local.date().isoformat()
    last_report_date = report_state.get("last_report_date")

    if last_report_date == today:
        logger.info("Daily report already sent for %s", today)
        return False

    if now_local.time() < report_time:
        logger.info("Daily report not due yet; now=%s target=%s", now_local.strftime("%H:%M"), report_time.strftime("%H:%M"))
        return False

    return True


def maybe_send_daily_report(history: list[dict[str, Any]], trades: list[dict[str, Any]], report_state: dict[str, Any]) -> None:
    """Send the daily Telegram report once per local day when due."""
    timezone_info = get_report_timezone()
    now_local = utc_now().astimezone(timezone_info)
    logger.info("Checking daily report schedule for %s", now_local.strftime("%Y-%m-%d %H:%M %Z"))

    if not should_send_daily_report(now_local, report_state):
        return

    message = format_daily_report(history, trades, now_local)
    if send_telegram_message(message):
        report_state["last_report_date"] = now_local.date().isoformat()
        report_state["last_report_sent_at"] = utc_now_iso()
        save_report_state(report_state)
        logger.info("Daily report sent for %s", report_state["last_report_date"])


def process_symbol(
    exchange: ccxt.kraken,
    symbol: str,
    signal_state: dict[str, str],
    signal_history: list[dict[str, Any]],
    trades: list[dict[str, Any]],
) -> None:
    """Fetch data, update trades, evaluate strategy, and send a Telegram signal if needed."""
    logger.info("Checking %s", symbol)
    main_frame = fetch_ohlcv(exchange, symbol, MAIN_TIMEFRAME)
    trend_frame = fetch_ohlcv(exchange, symbol, TREND_TIMEFRAME)
    if main_frame is None or trend_frame is None:
        return

    update_open_trades_for_symbol(symbol, main_frame, trades)

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
        append_signal_history(signal_history, signal)
        trades.append(create_trade(signal))
        save_trades(trades)
        logger.info("Stored %s signal state/history/trade for %s", signal["direction"], symbol)


def run_worker() -> None:
    """Run the Railway-ready worker forever."""
    logger.info("Starting Kraken crypto signal worker")
    exchange = create_exchange()
    signal_state = load_signal_state()
    signal_history = load_signal_history()
    trades = load_trades()
    report_state = load_report_state()

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
                process_symbol(exchange, symbol, signal_state, signal_history, trades)
            except Exception as exc:  # Defensive guard so one symbol never kills the worker.
                logger.exception("Unexpected error while processing %s: %s", symbol, exc)

        try:
            maybe_send_daily_report(signal_history, trades, report_state)
        except Exception as exc:  # Defensive guard so reporting never kills the worker.
            logger.exception("Unexpected error while sending daily report: %s", exc)

        elapsed = time.time() - cycle_started
        sleep_for = max(0, LOOP_SLEEP_SECONDS - elapsed)
        logger.info("Scan cycle finished in %.1fs; sleeping %.1fs", elapsed, sleep_for)
        time.sleep(sleep_for)


if __name__ == "__main__":
    run_worker()
