"""Railway entrypoint: python app.py."""
from __future__ import annotations

import logging
import sys
import time
from datetime import datetime, timedelta

from config import ACTIVE_MODE, DATABASE_URL, DATA_DIR, DUPLICATE_MINUTES, ENABLE_LIVE_SIGNALS, ENABLE_WATCHLIST_ALERTS, EXPORT_DIR, LOOP_SLEEP_SECONDS, MAX_CONSECUTIVE_STOP_LOSSES, MAX_SIGNALS_PER_DAY, MAX_SIGNALS_PER_PAIR_PER_DAY, OHLC_DB_PATH, OHLC_LIMIT, PAIRS, PERSISTENT_VOLUME_DETECTED, PROJECT_ALPHA_RESEARCH_MODE, QUANT_PAIRS, QUANT_TIMEFRAMES, RESEARCH_DB_PATH, RESEARCH_INTERVAL_SECONDS, RESEARCH_MODE, RUN_MODE, STOP_LOSS_PAUSE_HOURS, TIMEFRAMES, USES_LOCAL_SQLITE_PERSISTENCE
from exchange import fetch_ohlc
from indicators import add_indicators, classify_market_regime
from monitor import monitor_open_trades
from reporter import maybe_send_daily_report
from research import run_research
from research_alpha import run_project_alpha_research
from ohlc_cache import sync_ohlc_cache
from strategy import build_signal
from telegram_bot import send_message
from trade_store import all_closed, init_db, insert_signal, signals_since, update_signal_telegram_message_id
from risk import format_price

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s", stream=sys.stdout)
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


def stop_loss_circuit_breaker_active() -> bool:
    """Pause new operative signals after too many consecutive stop losses."""
    closed = sorted(
        all_closed(),
        key=lambda row: row.get("closed_at") or row.get("timestamp") or "",
        reverse=True,
    )
    if not closed:
        return False

    consecutive_sl = 0
    latest_sl_time: datetime | None = None
    for row in closed:
        if row.get("status") != "SL":
            break
        consecutive_sl += 1
        if latest_sl_time is None:
            raw_time = row.get("closed_at") or row.get("timestamp")
            if raw_time:
                latest_sl_time = datetime.fromisoformat(str(raw_time).replace("Z", "+00:00")).replace(tzinfo=None)

    if consecutive_sl < MAX_CONSECUTIVE_STOP_LOSSES or latest_sl_time is None:
        return False
    return datetime.utcnow() - latest_sl_time < timedelta(hours=STOP_LOSS_PAUSE_HOURS)


def operative_message(record: dict) -> str:
    risk = "ALTO" if record["market_regime"] in {"HIGH_VOLATILITY", "RANGE"} else "MEDIO"
    reasons = "\n".join(f"- {reason}" for reason in record["reasons"])
    return (
        f"🟢 LONG {record['pair']}\n"
        f"ID Segnale: {record['signal_id']}\n"
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
        f"👀 WATCHLIST\n{record['pair']}\nID Segnale: {record['signal_id']}\nDirezione: LONG\n"
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
    if record["status"] == "OPEN" and stop_loss_circuit_breaker_active():
        record["status"] = "REJECTED"
        record["penalties"].append(
            f"pausa automatica: {MAX_CONSECUTIVE_STOP_LOSSES} stop loss consecutivi nelle ultime {STOP_LOSS_PAUSE_HOURS}h"
        )
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
        telegram_result = send_message(operative_message(record))
        if telegram_result:
            update_signal_telegram_message_id(record["signal_id"], telegram_result.get("message_id"))
    elif record["status"] == "WATCHLIST":
        if ENABLE_WATCHLIST_ALERTS:
            send_message(watchlist_message(record))
        else:
            logger.info(
                "Watchlist Telegram disabled; saved %s as WATCHLIST only",
                record["signal_id"],
            )



def log_runtime_config() -> None:
    logger.info("RUN_MODE=%s", RUN_MODE)
    if RUN_MODE == "RAILWAY_LIGHT":
        logger.info("DATABASE_URL detected: %s", "yes" if DATABASE_URL else "no")
        logger.info("Storage backend: PostgreSQL")
        logger.info("SQLite cache: disabled")
        return
    logger.info("DATA_DIR=%s", DATA_DIR)
    logger.info("OHLC database path=%s", OHLC_DB_PATH)
    logger.info("Research database path=%s", RESEARCH_DB_PATH)
    logger.info("EXPORT_DIR=%s", EXPORT_DIR)
    if USES_LOCAL_SQLITE_PERSISTENCE and not PERSISTENT_VOLUME_DETECTED:
        logger.warning("WARNING: persistent volume not detected, local SQLite research data may be lost on redeploy")
    elif not USES_LOCAL_SQLITE_PERSISTENCE:
        logger.info("Persistent volume check skipped: RUN_MODE=%s uses PostgreSQL as primary storage", RUN_MODE)


def run_sync_data_worker() -> None:
    log_runtime_config()
    for pair in QUANT_PAIRS:
        for timeframe in QUANT_TIMEFRAMES:
            try:
                frame = sync_ohlc_cache(pair, timeframe)
                logger.info("SYNC_DATA pair=%s timeframe=%s candles=%s", pair, timeframe, len(frame))
            except Exception as exc:
                logger.exception("SYNC_DATA failed pair=%s timeframe=%s: %s", pair, timeframe, exc)


def run_project_alpha_worker() -> None:
    init_db()
    logger.info("PROJECT_ALPHA_RESEARCH_MODE enabled: live operative signals are disabled")
    while True:
        _, summary = run_project_alpha_research()
        logger.info("Project Alpha report generated:\n%s", summary)
        time.sleep(RESEARCH_INTERVAL_SECONDS)


def run_research_worker() -> None:
    init_db()
    log_runtime_config()
    logger.info("RESEARCH mode enabled: live operative signals are disabled")
    while True:
        report = run_research()
        logger.info("Research report generated:\n%s", report)
        send_message(report)
        time.sleep(RESEARCH_INTERVAL_SECONDS)


def main() -> None:
    log_runtime_config()
    if RUN_MODE == "RAILWAY_LIGHT":
        logger.info("RUN_MODE=RAILWAY_LIGHT detected in legacy app.py; delegating to project.main")
        from project.main import main as modular_main

        modular_main()
        return
    if RUN_MODE == "SYNC_DATA":
        run_sync_data_worker()
        return
    if RUN_MODE == "RESEARCH":
        run_research_worker()
        return
    if RUN_MODE == "LIVE" and not ENABLE_LIVE_SIGNALS:
        logger.warning("ENABLE_LIVE_SIGNALS=false: live operative signals are disabled")
        return
    if PROJECT_ALPHA_RESEARCH_MODE:
        run_project_alpha_worker()
        return
    if RESEARCH_MODE:
        run_research_worker()
        return
    if not ENABLE_LIVE_SIGNALS:
        logger.warning("ENABLE_LIVE_SIGNALS=false: live operative signals are disabled")
        return
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
