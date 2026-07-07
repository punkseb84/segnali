"""Daily Telegram report for crypto signal performance."""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from trade_history import INVESTMENT_AMOUNT_EUR, calculate_pl_eur, load_trades

logger = logging.getLogger("crypto-signal-worker")

REPORT_STATE_FILE = Path(os.getenv("REPORT_STATE_FILE", "daily_report_state.json"))
REPORT_TIMEZONE = ZoneInfo(os.getenv("REPORT_TIMEZONE", "Europe/Rome"))
REPORT_HOUR = int(os.getenv("REPORT_HOUR", "9"))
REPORT_MINUTE = int(os.getenv("REPORT_MINUTE", "0"))


def _load_report_state() -> dict[str, Any]:
    if not REPORT_STATE_FILE.exists():
        return {}

    try:
        with REPORT_STATE_FILE.open("r", encoding="utf-8") as file:
            data = json.load(file)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Could not read %s: %s", REPORT_STATE_FILE, exc)
        return {}

    return data if isinstance(data, dict) else {}


def _save_report_state(state: dict[str, Any]) -> None:
    try:
        with REPORT_STATE_FILE.open("w", encoding="utf-8") as file:
            json.dump(state, file, indent=2, sort_keys=True)
    except OSError as exc:
        logger.error("Could not write %s: %s", REPORT_STATE_FILE, exc)


def should_send_daily_report(now: datetime | None = None) -> bool:
    """Return True once per local day after the configured report time."""
    local_now = now.astimezone(REPORT_TIMEZONE) if now else datetime.now(REPORT_TIMEZONE)
    scheduled = datetime.combine(local_now.date(), time(REPORT_HOUR, REPORT_MINUTE), tzinfo=REPORT_TIMEZONE)
    if local_now < scheduled:
        return False

    state = _load_report_state()
    return state.get("last_report_date") != local_now.date().isoformat()


def mark_daily_report_sent(now: datetime | None = None) -> None:
    local_now = now.astimezone(REPORT_TIMEZONE) if now else datetime.now(REPORT_TIMEZONE)
    _save_report_state(
        {
            "last_report_date": local_now.date().isoformat(),
            "last_report_sent_at": local_now.isoformat(),
        }
    )


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _trade_in_period(trade: dict[str, Any], start_utc: datetime, end_utc: datetime) -> bool:
    created = _parse_dt(trade.get("created_at_utc"))
    if created is None:
        return False
    return start_utc <= created <= end_utc


def _summarize(trades: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(trades)
    tp1 = sum(1 for trade in trades if trade.get("status") == "TP1")
    sl = sum(1 for trade in trades if trade.get("status") == "SL")
    open_trades = sum(1 for trade in trades if trade.get("status") == "OPEN")
    closed = tp1 + sl
    win_rate = (tp1 / closed * 100) if closed else None

    profits = [calculate_pl_eur(trade) for trade in trades if trade.get("status") == "TP1"]
    losses = [calculate_pl_eur(trade) for trade in trades if trade.get("status") == "SL"]
    gross_profit = sum(profits)
    gross_loss = abs(sum(losses))
    net = gross_profit - gross_loss

    if gross_loss > 0:
        profit_factor = gross_profit / gross_loss
    elif gross_profit > 0:
        profit_factor = float("inf")
    else:
        profit_factor = None

    return {
        "total": total,
        "tp1": tp1,
        "sl": sl,
        "open": open_trades,
        "closed": closed,
        "win_rate": win_rate,
        "gross_profit": gross_profit,
        "gross_loss": gross_loss,
        "net": net,
        "profit_factor": profit_factor,
    }


def _fmt_money(value: float) -> str:
    sign = "+" if value > 0 else ""
    return f"{sign}{value:.2f} €".replace(".", ",")


def _fmt_pct(value: float | None) -> str:
    if value is None:
        return "N/D"
    return f"{value:.2f}%".replace(".", ",")


def _fmt_profit_factor(value: float | None) -> str:
    if value is None:
        return "N/D"
    if value == float("inf"):
        return "∞"
    return f"{value:.2f}".replace(".", ",")


def _format_trade_line(index: int, trade: dict[str, Any]) -> str:
    pl = calculate_pl_eur(trade)
    status = trade.get("status", "OPEN")
    status_icon = {"TP1": "✅ TP1", "SL": "❌ SL", "OPEN": "⏳ OPEN"}.get(status, status)
    created = _parse_dt(trade.get("created_at_utc"))
    when = created.astimezone(REPORT_TIMEZONE).strftime("%d/%m %H:%M") if created else "data N/D"

    return (
        f"{index}) {trade.get('symbol')} {trade.get('direction')} - {when}\n"
        f"Entry {float(trade.get('entry', 0)):.4f} | TP1 {float(trade.get('target_1', 0)):.4f} | "
        f"SL {float(trade.get('stop_loss', 0)):.4f}\n"
        f"Esito: {status_icon} | P/L: {_fmt_money(pl)}"
    )


def build_daily_report_message(now: datetime | None = None) -> str:
    """Build a report containing last 24h and full-history statistics."""
    end_local = now.astimezone(REPORT_TIMEZONE) if now else datetime.now(REPORT_TIMEZONE)
    start_local = end_local - timedelta(hours=24)
    start_utc = start_local.astimezone(ZoneInfo("UTC"))
    end_utc = end_local.astimezone(ZoneInfo("UTC"))

    all_trades = load_trades()
    last_24h = [trade for trade in all_trades if _trade_in_period(trade, start_utc, end_utc)]

    day_stats = _summarize(last_24h)
    total_stats = _summarize(all_trades)

    lines = [
        "📊 REPORT GIORNALIERO BOT CRYPTO",
        f"Periodo ultime 24h: {start_local.strftime('%d/%m %H:%M')} → {end_local.strftime('%d/%m %H:%M')}",
        "",
        "Ultime 24 ore",
        f"Segnali: {day_stats['total']}",
        f"✅ TP1: {day_stats['tp1']}",
        f"❌ SL: {day_stats['sl']}",
        f"⏳ OPEN: {day_stats['open']}",
        f"Win rate: {_fmt_pct(day_stats['win_rate'])}",
        f"Profit Factor: {_fmt_profit_factor(day_stats['profit_factor'])}",
        f"Profitto teorico: {_fmt_money(day_stats['net'])}",
        "",
        "Storico completo",
        f"Segnali totali: {total_stats['total']}",
        f"✅ TP1: {total_stats['tp1']}",
        f"❌ SL: {total_stats['sl']}",
        f"⏳ OPEN: {total_stats['open']}",
        f"Trade chiusi: {total_stats['closed']}",
        f"Win rate totale: {_fmt_pct(total_stats['win_rate'])}",
        f"Profit Factor totale: {_fmt_profit_factor(total_stats['profit_factor'])}",
        f"Profitto teorico totale: {_fmt_money(total_stats['net'])}",
        f"Size teorica: {INVESTMENT_AMOUNT_EUR:.2f} € per operazione spot 1:1".replace(".", ","),
    ]

    if last_24h:
        lines.extend(["", "Operazioni ultime 24h:"])
        for index, trade in enumerate(last_24h, start=1):
            lines.append(_format_trade_line(index, trade))
    else:
        lines.extend(["", "Nessuna nuova operazione nelle ultime 24h."])

    return "\n\n".join(lines)


def send_daily_report_if_due(send_message: Any, now: datetime | None = None) -> bool:
    """Send the daily report once per day if the configured time has passed."""
    if not should_send_daily_report(now):
        return False

    message = build_daily_report_message(now)
    if send_message(message):
        mark_daily_report_sent(now)
        logger.info("Daily report sent")
        return True

    logger.warning("Daily report not marked as sent because Telegram send failed")
    return False
