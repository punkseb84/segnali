"""Railway entrypoint: python app.py."""
from __future__ import annotations

import logging
import time
from datetime import datetime

from config import ACTIVE_MODE, DUPLICATE_MINUTES, LOOP_SLEEP_SECONDS, MAX_SIGNALS_PER_DAY, MAX_SIGNALS_PER_PAIR_PER_DAY, OHLC_LIMIT, PAIRS, TIMEFRAMES
from exchange import fetch_ohlc
from indicators import add_indicators, classify_market_regime
from monitor import monitor_open_trades
from reporter import maybe_send_daily_report
from strategy import build_signal
from telegram_bot import send_message
from trade_store import init_db, insert_signal, signals_since
from risk import format_price

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
logger = logging.getLogger("crypto-bot")


def daily_limits_ok(pair: str) -> bool:
    today = datetime.utcnow().date().isoformat()
    all_today = signals_since(today, status="OPEN") + signals_since(today, status="TP1") + signals_since(today, status="SL")
    pair_today = [row for row in all_today if row["pair"] == pair]
    if len(all_today) >= MAX_SIGNALS_PER_DAY or len(pair_today) >= MAX_SIGNALS_PER_PAIR_PER_DAY:
        return False
    latest_pair = pair_today[-1:] or []
    if latest_pair:
        last = datetime.fromisoformat(latest_pair[0]["timestamp"])
        if (datetime.utcnow() - last.replace(tzinfo=None)).total_seconds() < DUPLICATE_MINUTES * 60:
            return False
    return True


def operative_message(record: dict) -> str:
    risk = "ALTO" if record["market_regime"] in {"HIGH_VOLATILITY", "RANGE"} else "MEDIO"
    reasons = "\n".join(f"- {reason}" for reason in record["reasons"])
    return (
        f"🟢 LONG {record['pair']}\n"
        f"Modalità: {record['mode']}\n"
        f"Rischio: {risk}\n"
        f"Importo: €{record['trade_amount_eur']:.2f}\n\n"
        f"Entry: {format_price(record['entry'])}\n"
        f"Stop Loss: {format_price(record['stop_loss'])}\n"
        f"Target 1: {format_price(record['target_1'])}\n\n"
        f"Profitto netto stimato TP1: +€{format_price(record['net_profit_tp1_eur'])}\n"
        f"Perdita netta stimata SL: -€{format_price(record['net_loss_sl_eur'])}\n"
        f"RR netto: {record['net_rr']:.2f}\n"
        f"Score: {record['score']}/100\n\n"
        f"Regime mercato: {record['market_regime']}\n\n"
        f"Motivi:\n{reasons}"
    )


def watchlist_message(record: dict) -> str:
    reasons = "\n".join(f"- {reason}" for reason in record["reasons"])
    penalties = "\n".join(f"- {penalty}" for penalty in record["penalties"])
    return (
        f"👀 WATCHLIST\n{record['pair']}\nDirezione: LONG\n"
        f"Entry teorica: {format_price(record['entry'])}\n"
        f"Score: {record['score']}/100\n"
        f"Motivi:\n{reasons}\n"
        f"Perché non operativo:\n{penalties or '- sotto soglia operativa'}"
    )


def scan_pair(pair: str) -> None:
    timeframes = TIMEFRAMES[ACTIVE_MODE]
    main = add_indicators(fetch_ohlc(pair, timeframes["main"], OHLC_LIMIT))
    confirm_1 = add_indicators(fetch_ohlc(pair, timeframes["confirm_1"], OHLC_LIMIT))
    confirm_2 = add_indicators(fetch_ohlc(pair, timeframes["confirm_2"], OHLC_LIMIT))
    btc = add_indicators(fetch_ohlc("BTC/USD", timeframes["confirm_1"], OHLC_LIMIT))
    record = build_signal(pair, main, confirm_1, confirm_2, btc, ACTIVE_MODE)
    if record is None:
        logger.info("PAIR=%s MODE=%s DECISION=NO_DATA", pair, ACTIVE_MODE)
        return
    if record["status"] == "OPEN" and not daily_limits_ok(pair):
        record["status"] = "REJECTED"
        record["penalties"].append("limite giornaliero o duplicato entro 60 minuti")
    insert_signal(record)
    logger.info(
        "PAIR=%s MODE=%s REGIME=%s SCORE=%s REASONS=%s PENALTIES=%s DECISION=%s ENTRY=%.6f SL=%.6f TP1=%.6f NET_PROFIT_EUR=%.6f NET_LOSS_EUR=%.6f RR_NETTO=%.2f",
        pair,
        ACTIVE_MODE,
        record["market_regime"],
        record["score"],
        record["reasons"],
        record["penalties"],
        record["status"],
        record["entry"],
        record["stop_loss"],
        record["target_1"],
        record["net_profit_tp1_eur"],
        record["net_loss_sl_eur"],
        record["net_rr"],
    )
    if record["status"] == "OPEN":
        send_message(operative_message(record))
    elif record["status"] == "WATCHLIST":
        send_message(watchlist_message(record))


def main() -> None:
    init_db()
    send_message(f"🤖 Crypto bot avviato su Kraken. Modalità: {ACTIVE_MODE}")
    while True:
        cycle_started = time.time()
        monitor_open_trades()
        for pair in PAIRS:
            try:
                scan_pair(pair)
            except Exception as exc:
                logger.exception("Errore scansione %s: %s", pair, exc)
        maybe_send_daily_report(datetime.now())
        sleep_for = max(0, LOOP_SLEEP_SECONDS - (time.time() - cycle_started))
        logger.info("Ciclo completato; sleep %.1fs", sleep_for)
        time.sleep(sleep_for)


if __name__ == "__main__":
    main()
