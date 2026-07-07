"""Open trade monitoring with real Kraken OHLC candles."""
from __future__ import annotations

import logging
from typing import Any

import pandas as pd

from exchange import fetch_ohlc
from risk import format_price
from telegram_bot import send_message
from trade_store import open_trades, update_outcome

logger = logging.getLogger("crypto-bot.monitor")


def check_trade(trade: dict[str, Any]) -> None:
    """Close one open LONG trade when TP1 or SL is touched by real OHLC."""
    frame = fetch_ohlc(trade["pair"], trade["timeframe"], limit=120)
    if frame.empty:
        return
    opened = pd.to_datetime(trade.get("timestamp"), utc=True)
    candles = frame[frame["timestamp"] > opened]
    for _, candle in candles.iterrows():
        tp_hit = float(candle["high"]) >= float(trade["target_1"])
        sl_hit = float(candle["low"]) <= float(trade["stop_loss"])
        if not tp_hit and not sl_hit:
            continue
        ambiguous = tp_hit and sl_hit
        if sl_hit:
            result = -abs(float(trade["net_loss_sl_eur"] or 0))
            update_outcome(trade["signal_id"], "SL", "SL", result, ambiguous)
            send_message(
                "🛑 Stop Loss raggiunto\n"
                f"LONG {trade['pair']}\n"
                f"Entry: {format_price(float(trade['entry']))}\n"
                f"Stop Loss: {format_price(float(trade['stop_loss']))}\n"
                f"Risultato netto: -€{format_price(abs(result))}"
            )
        else:
            result = float(trade["net_profit_tp1_eur"] or 0)
            update_outcome(trade["signal_id"], "TP1", "TP1", result, ambiguous)
            send_message(
                "✅ Target 1 raggiunto\n"
                f"LONG {trade['pair']}\n"
                f"Entry: {format_price(float(trade['entry']))}\n"
                f"Target 1: {format_price(float(trade['target_1']))}\n"
                f"Risultato netto: +€{format_price(result)}"
            )
        logger.info("Closed %s as %s ambiguous=%s", trade["signal_id"], "SL" if sl_hit else "TP1", ambiguous)
        return


def monitor_open_trades() -> None:
    for trade in open_trades():
        check_trade(trade)
