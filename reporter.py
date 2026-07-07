"""Daily Telegram reports and historical analysis."""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date
from statistics import mean
from typing import Any

from config import DAILY_REPORT_HOUR, TRADE_AMOUNT_EUR
from telegram_bot import send_message
from trade_store import all_closed, get_state, recent_signals, set_state


def _profit_factor(rows: list[dict[str, Any]]) -> float:
    wins = sum(float(row["realized_result_eur"] or 0) for row in rows if float(row["realized_result_eur"] or 0) > 0)
    losses = abs(sum(float(row["realized_result_eur"] or 0) for row in rows if float(row["realized_result_eur"] or 0) < 0))
    return wins / losses if losses else (wins if wins else 0.0)


def _group_best_worst(rows: list[dict[str, Any]], key: str) -> tuple[list[tuple[str, float]], list[tuple[str, float]]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[str(row.get(key, "unknown"))].append(row)
    ranked = sorted(((group, _profit_factor(items)) for group, items in groups.items()), key=lambda x: x[1], reverse=True)
    return ranked[:3], ranked[-3:]


def analyze_history() -> list[str]:
    rows = all_closed()
    if len(rows) < 5:
        return ["Storico ancora limitato: attendere più trade prima di ottimizzare i filtri."]
    suggestions: list[str] = []
    by_pair: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_pair[row["pair"]].append(row)
    for pair, items in by_pair.items():
        losses = [row for row in items if row["status"] == "SL"]
        if len(items) >= 3 and len(losses) / len(items) > 0.6:
            suggestions.append(f"{pair}: troppi SL, valutare esclusione temporanea o score minimo più alto.")
    high_rsi_losses = [row for row in rows if float(row.get("rsi") or 0) > 68 and row["status"] == "SL"]
    if len(high_rsi_losses) >= 3:
        suggestions.append("RSI > 68 sta perdendo spesso: valutare penalità più forte.")
    low_adx_losses = [row for row in rows if float(row.get("adx") or 0) < 18 and row["status"] == "SL"]
    if len(low_adx_losses) >= 3:
        suggestions.append("ADX basso produce falsi segnali: valutare meno operatività in RANGE.")
    return suggestions or ["Nessun pattern negativo evidente nello storico attuale."]


def build_daily_report() -> str:
    rows = recent_signals(24)
    lines = ["📊 Report giornaliero crypto", "Periodo: ultime 24 ore"]
    for mode in ["CONSERVATIVE", "SCALPING_FAST"]:
        mode_rows = [row for row in rows if row["mode"] == mode]
        closed = [row for row in mode_rows if row["status"] in {"TP1", "SL"}]
        open_rows = [row for row in mode_rows if row["status"] == "OPEN"]
        tp1 = [row for row in mode_rows if row["status"] == "TP1"]
        sl = [row for row in mode_rows if row["status"] == "SL"]
        watch = [row for row in mode_rows if row["status"] == "WATCHLIST"]
        rejected = [row for row in mode_rows if row["status"] == "REJECTED"]
        profit = sum(float(row["realized_result_eur"] or 0) for row in tp1)
        loss = abs(sum(float(row["realized_result_eur"] or 0) for row in sl))
        net = profit - loss
        best_pairs, worst_pairs = _group_best_worst(closed, "pair")
        best_hours, worst_hours = _group_best_worst(closed, "hour")
        reject_reasons = Counter()
        for row in rejected:
            for reason in str(row.get("penalties") or "").split(","):
                if reason:
                    reject_reasons[reason[:60]] += 1
        lines.extend([
            "",
            f"Modalità: {mode}",
            f"Segnali operativi: {len([row for row in mode_rows if row['status'] in {'OPEN','TP1','SL'}])}",
            f"Watchlist: {len(watch)}",
            f"Scartati: {len(rejected)}",
            f"Trade chiusi: {len(closed)}",
            f"Trade aperti: {len(open_rows)}",
            f"TP1: {len(tp1)}",
            f"SL: {len(sl)}",
            f"Win rate TP1: {(len(tp1) / len(closed) * 100) if closed else 0:.2f}%",
            f"Profit factor: {_profit_factor(closed):.2f}",
            f"Profitto totale: €{profit:.3f}",
            f"Perdita totale: €{loss:.3f}",
            f"Saldo netto: €{net:.3f}",
            f"Capitale iniziale: €{TRADE_AMOUNT_EUR:.2f}",
            f"Capitale attuale teorico: €{TRADE_AMOUNT_EUR + net:.3f}",
            f"Migliori coppie: {best_pairs}",
            f"Peggiori coppie: {worst_pairs}",
            f"Migliori orari: {best_hours}",
            f"Peggiori orari: {worst_hours}",
            f"Motivi principali di scarto: {reject_reasons.most_common(3)}",
        ])
    lines.extend(["", "Analisi automatica:"])
    lines.extend(f"- {item}" for item in analyze_history())
    return "\n".join(lines)


def maybe_send_daily_report(now) -> None:
    today = date.today().isoformat()
    if now.hour < DAILY_REPORT_HOUR:
        return
    if get_state("last_report_date") == today:
        return
    if send_message(build_daily_report()):
        set_state("last_report_date", today)
