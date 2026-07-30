"""One-time Telegram performance report for delivered Ichimoku signals."""
from __future__ import annotations

import html
import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

from project.ichimoku_scanner import STRATEGY_NAME


DEFAULT_REPORT_SINCE_UTC = "2026-07-28T04:45:00+00:00"
DEFAULT_REPORT_KEY = "ICHIMOKU_PERFORMANCE_AFTER_PR123"


@dataclass
class PerformanceRow:
    signal_id: int
    sent_at: Any
    pair: str
    timeframe: str
    status: str
    entry: float
    current_stop: float | None
    initial_stop: float | None
    atr: float | None
    break_even_armed: bool
    tp1_hit: bool
    tp1_price: float | None
    outcome_price: float | None
    closed_at: Any
    outcome_resolution: str
    net_result_eur: float | None
    budget_after_eur: float | None = None


@dataclass
class PerformanceSummary:
    total_signals: int
    closed_signals: int
    open_signals: int
    wins: int
    losses: int
    flat: int
    win_rate: float
    gross_profit_eur: float
    gross_loss_eur: float
    net_result_eur: float
    profit_factor: float | None
    average_win_eur: float
    average_loss_eur: float
    max_loss_streak: int
    max_drawdown_eur: float
    max_drawdown_pct: float
    initial_budget_eur: float
    final_budget_eur: float


def _float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _state(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return dict(parsed) if isinstance(parsed, dict) else {}
    return {}


def _parse_since(value: str) -> datetime:
    normalized = str(value or DEFAULT_REPORT_SINCE_UTC).strip().replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def load_performance_rows(postgres: Any, since_utc: str) -> list[PerformanceRow]:
    since = _parse_since(since_utc)
    rows = postgres.fetch_all(
        """SELECT id,
                  telegram_sent_at,
                  pair,
                  timeframe,
                  status,
                  entry,
                  stop_loss,
                  outcome_price,
                  closed_at,
                  COALESCE(outcome_resolution, ''),
                  COALESCE(net_profit_tp1_eur, 0),
                  COALESCE(net_loss_sl_eur, 0),
                  COALESCE(score_breakdown, '{}'::jsonb)
           FROM signals.generated_signals
           WHERE strategy = %s
             AND telegram_sent_at IS NOT NULL
             AND telegram_sent_at >= %s
           ORDER BY telegram_sent_at ASC, id ASC""",
        (STRATEGY_NAME, since),
    )

    result: list[PerformanceRow] = []
    for raw in rows:
        (
            signal_id,
            sent_at,
            pair,
            timeframe,
            status,
            entry_raw,
            stop_raw,
            outcome_raw,
            closed_at,
            resolution,
            profit_raw,
            loss_raw,
            state_raw,
        ) = raw
        state = _state(state_raw)
        entry = float(entry_raw)
        atr = _float(state.get("atr"))
        stop_multiple = _float(state.get("atr_stop_multiple")) or 1.5
        initial_stop = entry - atr * stop_multiple if atr and atr > 0 else _float(stop_raw)
        tp1_price = _float(state.get("tp1_price"))
        if tp1_price is None and atr and atr > 0:
            tp1_price = entry + (2.0 * atr)
        closed = closed_at is not None or str(status).upper() not in {"NEW", "OPEN"}
        net_result = None
        if closed:
            net_result = float(profit_raw or 0.0) - float(loss_raw or 0.0)
        result.append(
            PerformanceRow(
                signal_id=int(signal_id),
                sent_at=sent_at,
                pair=str(pair),
                timeframe=str(timeframe),
                status=str(status),
                entry=entry,
                current_stop=_float(stop_raw),
                initial_stop=initial_stop,
                atr=atr,
                break_even_armed=bool(state.get("breakeven_armed")),
                tp1_hit=bool(state.get("tp1_hit")),
                tp1_price=tp1_price,
                outcome_price=_float(outcome_raw),
                closed_at=closed_at,
                outcome_resolution=str(resolution or ""),
                net_result_eur=net_result,
            )
        )
    return result


def calculate_summary(
    rows: list[PerformanceRow],
    initial_budget_eur: float = 100.0,
) -> PerformanceSummary:
    budget = float(initial_budget_eur)
    peak = budget
    max_drawdown = 0.0
    max_drawdown_pct = 0.0
    gross_profit = 0.0
    gross_loss = 0.0
    wins = losses = flat = 0
    current_loss_streak = max_loss_streak = 0

    for row in rows:
        if row.net_result_eur is None:
            continue
        value = float(row.net_result_eur)
        if value > 0.005:
            wins += 1
            gross_profit += value
            current_loss_streak = 0
        elif value < -0.005:
            losses += 1
            gross_loss += abs(value)
            current_loss_streak += 1
            max_loss_streak = max(max_loss_streak, current_loss_streak)
        else:
            flat += 1
            current_loss_streak = 0

        budget += value
        row.budget_after_eur = budget
        peak = max(peak, budget)
        drawdown = peak - budget
        drawdown_pct = (drawdown / peak * 100.0) if peak > 0 else 0.0
        max_drawdown = max(max_drawdown, drawdown)
        max_drawdown_pct = max(max_drawdown_pct, drawdown_pct)

    closed = wins + losses + flat
    decisive = wins + losses
    return PerformanceSummary(
        total_signals=len(rows),
        closed_signals=closed,
        open_signals=len(rows) - closed,
        wins=wins,
        losses=losses,
        flat=flat,
        win_rate=(wins / decisive * 100.0) if decisive else 0.0,
        gross_profit_eur=gross_profit,
        gross_loss_eur=gross_loss,
        net_result_eur=gross_profit - gross_loss,
        profit_factor=(gross_profit / gross_loss) if gross_loss > 0 else None,
        average_win_eur=(gross_profit / wins) if wins else 0.0,
        average_loss_eur=(gross_loss / losses) if losses else 0.0,
        max_loss_streak=max_loss_streak,
        max_drawdown_eur=max_drawdown,
        max_drawdown_pct=max_drawdown_pct,
        initial_budget_eur=float(initial_budget_eur),
        final_budget_eur=budget,
    )


def _money(value: float, signed: bool = False) -> str:
    prefix = "+" if signed and value > 0 else ""
    return f"{prefix}€{value:.2f}".replace(".", ",")


def _price(value: float | None) -> str:
    if value is None:
        return "-"
    absolute = abs(value)
    decimals = 2 if absolute >= 100 else 3 if absolute >= 1 else 5
    return f"{value:.{decimals}f}".replace(".", ",")


def _date(value: Any) -> str:
    if value is None:
        return "-"
    try:
        timestamp = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        return timestamp.astimezone(timezone.utc).strftime("%d/%m %H:%M")
    except (TypeError, ValueError):
        return str(value)[:11]


def _resolution_label(row: PerformanceRow) -> str:
    if row.net_result_eur is None:
        return "APERTA"
    labels = {
        "ATR_STOP_ICHIMOKU": "SL iniziale",
        "ICHIMOKU_BREAK_EVEN_STOP": "Stop BE",
        "ICHIMOKU_CLOUD_EXIT": "Uscita cloud",
        "ICHIMOKU_TENKAN_KIJUN_EXIT": "Incrocio T/K",
    }
    return labels.get(row.outcome_resolution, row.outcome_resolution or row.status)


def format_summary_message(summary: PerformanceSummary, since_utc: str) -> str:
    pf = "n/d" if summary.profit_factor is None else f"{summary.profit_factor:.2f}".replace(".", ",")
    return (
        "📊 <b>RESOCONTO ICHIMOKU</b>\n"
        f"Dal: <b>{html.escape(_date(_parse_since(since_utc))) } UTC</b>\n\n"
        f"Segnali inviati: <b>{summary.total_signals}</b>\n"
        f"Chiusi: <b>{summary.closed_signals}</b> · Aperti: <b>{summary.open_signals}</b>\n"
        f"Profitti: <b>{summary.wins}</b> · Perdite: <b>{summary.losses}</b> · Pari: <b>{summary.flat}</b>\n"
        f"Win rate: <b>{summary.win_rate:.1f}%</b>\n"
        f"Profit factor: <b>{pf}</b>\n\n"
        f"Profitto lordo: <b>{_money(summary.gross_profit_eur)}</b>\n"
        f"Perdita lorda: <b>{_money(summary.gross_loss_eur)}</b>\n"
        f"Risultato netto: <b>{_money(summary.net_result_eur, signed=True)}</b>\n"
        f"Profitto medio: <b>{_money(summary.average_win_eur)}</b>\n"
        f"Perdita media: <b>{_money(summary.average_loss_eur)}</b>\n\n"
        f"Serie max perdite: <b>{summary.max_loss_streak}</b>\n"
        f"Drawdown max: <b>{_money(summary.max_drawdown_eur)} ({summary.max_drawdown_pct:.2f}%)</b>\n"
        f"Budget iniziale: <b>{_money(summary.initial_budget_eur)}</b>\n"
        f"Budget aggiornato: <b>{_money(summary.final_budget_eur)}</b>\n\n"
        "⚠️ Risultati PAPER netti stimati sulla base di commissioni, spread e slippage configurati."
    )


def _chunks(items: list[PerformanceRow], size: int = 10) -> Iterable[list[PerformanceRow]]:
    for index in range(0, len(items), size):
        yield items[index : index + size]


def format_detail_messages(rows: list[PerformanceRow]) -> list[str]:
    if not rows:
        return ["📋 <b>DETTAGLIO SEGNALI</b>\nNessun segnale Telegram nel periodo selezionato."]

    messages: list[str] = []
    for part, batch in enumerate(_chunks(rows), start=1):
        lines = [
            "ID   Data UTC    Coppia    Esito        BE TP1 Netto     Budget",
            "---- ----------- --------- ------------ -- --- --------- --------",
        ]
        for row in batch:
            net = "aperta" if row.net_result_eur is None else _money(row.net_result_eur, signed=True)
            budget = "-" if row.budget_after_eur is None else _money(row.budget_after_eur)
            lines.append(
                f"{row.signal_id:<4} {_date(row.sent_at):<11} {row.pair:<9} "
                f"{_resolution_label(row)[:12]:<12} "
                f"{'S' if row.break_even_armed else 'N':<2} "
                f"{'S' if row.tp1_hit else 'N':<3} "
                f"{net:<9} {budget:<8}"
            )
        messages.append(
            f"📋 <b>DETTAGLIO OPERAZIONI · {part}</b>\n<pre>{html.escape(chr(10).join(lines))}</pre>"
        )

        level_lines = [
            "ID   Entrata     SL iniz.    TP1         Uscita      ATR",
            "---- ----------- ----------- ----------- ----------- --------",
        ]
        for row in batch:
            level_lines.append(
                f"{row.signal_id:<4} {_price(row.entry):<11} {_price(row.initial_stop):<11} "
                f"{_price(row.tp1_price):<11} {_price(row.outcome_price):<11} {_price(row.atr):<8}"
            )
        messages.append(
            f"📐 <b>LIVELLI OPERATIVI · {part}</b>\n<pre>{html.escape(chr(10).join(level_lines))}</pre>"
        )
    return messages


def _ensure_dispatch_table(postgres: Any) -> None:
    postgres.execute(
        """CREATE TABLE IF NOT EXISTS signals.report_dispatches (
               report_key TEXT PRIMARY KEY,
               sent_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
               details JSONB NOT NULL DEFAULT '{}'::jsonb
           )"""
    )


def _already_sent(postgres: Any, report_key: str) -> bool:
    rows = postgres.fetch_all(
        "SELECT 1 FROM signals.report_dispatches WHERE report_key = %s LIMIT 1",
        (report_key,),
    )
    return bool(rows)


def _mark_sent(postgres: Any, report_key: str, summary: PerformanceSummary) -> None:
    details = {
        "signals": summary.total_signals,
        "closed": summary.closed_signals,
        "net_result_eur": round(summary.net_result_eur, 6),
        "final_budget_eur": round(summary.final_budget_eur, 6),
    }
    postgres.execute(
        """INSERT INTO signals.report_dispatches (report_key, details)
           VALUES (%s, %s::jsonb)
           ON CONFLICT (report_key) DO UPDATE
           SET sent_at = NOW(), details = EXCLUDED.details""",
        (report_key, json.dumps(details)),
    )


def send_startup_performance_report(
    postgres: Any,
    notification: Any,
    *,
    since_utc: str = DEFAULT_REPORT_SINCE_UTC,
    report_key: str = DEFAULT_REPORT_KEY,
    initial_budget_eur: float = 100.0,
    force: bool = False,
) -> bool:
    """Send one audited summary plus compact tables; retry on a later restart if delivery fails."""
    _ensure_dispatch_table(postgres)
    if not force and _already_sent(postgres, report_key):
        return False

    rows = load_performance_rows(postgres, since_utc)
    summary = calculate_summary(rows, initial_budget_eur)
    messages = [format_summary_message(summary, since_utc), *format_detail_messages(rows)]
    for message in messages:
        if not notification.send_telegram(message):
            return False
    _mark_sent(postgres, report_key, summary)
    return True
