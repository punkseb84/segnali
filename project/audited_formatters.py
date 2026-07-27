"""Compact Telegram outcome formatter for Ichimoku position management."""
from __future__ import annotations

import html
from typing import Any

from project.notification_engine.simple_formatters import (
    format_simple_report_message,
    format_simple_signal_message,
)


def _escape(value: Any) -> str:
    return html.escape(str(value if value is not None else "n/d"), quote=True)


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _price(value: Any) -> str:
    number = _float(value)
    absolute = abs(number)
    decimals = 2 if absolute >= 1000 else 3 if absolute >= 1 else 5 if absolute >= 0.01 else 8
    return f"{number:,.{decimals}f}"


def _money(value: Any, *, signed: bool = False) -> str:
    number = _float(value)
    return f"€{number:+.2f}" if signed else f"€{number:.2f}"


def format_audited_outcome_message(payload: dict[str, Any]) -> str:
    resolution = str(payload.get("outcome_resolution") or "")
    signal_id = payload.get("signal_id") or payload.get("id") or "n/d"
    result = _float(payload.get("realized_net_eur"))

    if bool(payload.get("partial_take_profit")) or resolution == "ICHIMOKU_TP1_2ATR":
        return (
            "✅ <b>TP1 RAGGIUNTO</b> "
            f"<code>#{_escape(signal_id)}</code>\n"
            f"<b>{_escape(payload.get('pair'))}</b> · {_escape(payload.get('timeframe'))}\n\n"
            "Chiudi ora: <b>50%</b>\n"
            f"Prezzo TP1: <b>{_price(payload.get('outcome_price'))}</b>\n"
            f"Profitto prima metà: <b>{_money(result, signed=True)}</b>\n\n"
            "Posizione aperta: <b>50%</b>\n"
            "Stop restante: <b>Break Even</b>\n"
            "Attendi il messaggio di uscita finale."
        )

    if payload.get("realized_net_eur") is None:
        is_stop = str(payload.get("outcome")) == "STOP_LOSS"
        result = (
            -abs(_float(payload.get("net_loss_sl_eur")))
            if is_stop
            else _float(payload.get("net_profit_tp1_eur"))
        )

    tp1_hit = bool(payload.get("tp1_hit"))
    prior_net = _float(payload.get("tp1_realized_net_eur"))
    final_leg_net = result - prior_net if tp1_hit else result
    budget_before = _float(payload.get("budget_before_eur"), 100.0)
    budget_after = _float(payload.get("budget_after_eur"), budget_before + result)

    title = (
        "🟢 <b>OPERAZIONE CHIUSA</b>"
        if result >= 0
        else "🔴 <b>OPERAZIONE CHIUSA</b>"
    )
    outcome_label = "PROFITTO" if result >= 0 else "PERDITA"

    result_lines = ""
    if tp1_hit:
        result_lines = (
            f"TP1 50%: <b>{_money(prior_net, signed=True)}</b>\n"
            f"Uscita restante: <b>{_money(final_leg_net, signed=True)}</b>\n"
        )

    return (
        f"{title} <code>#{_escape(signal_id)}</code>\n"
        f"<b>{_escape(payload.get('pair'))}</b> · <b>{outcome_label}</b>\n\n"
        f"{result_lines}"
        f"Risultato totale: <b>{_money(result, signed=True)}</b>\n\n"
        f"Budget iniziale: <b>{_money(budget_before)}</b>\n"
        f"Budget aggiornato: <b>{_money(budget_after)}</b>"
    )


format_simple_outcome_message = format_audited_outcome_message
