"""Telegram presentation for the Velez 15m protected Mode 2 PAPER engine."""
from __future__ import annotations

import html
from typing import Any


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _price(value: Any) -> str:
    if value is None or value == "":
        return "n/d"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "n/d"
    decimals = 2 if abs(number) >= 1000 else 3 if abs(number) >= 1 else 5 if abs(number) >= 0.01 else 8
    return f"{number:,.{decimals}f}"


def _esc(value: Any) -> str:
    return html.escape(str(value if value is not None else "n/d"), quote=True)


def _state(payload: dict[str, Any]) -> dict[str, Any]:
    raw = payload.get("score_breakdown") or payload.get("state") or {}
    return dict(raw) if isinstance(raw, dict) else {}


def format_velez_signal(payload: dict[str, Any]) -> str:
    state = _state(payload)
    direction = str(state.get("direction", "LONG"))
    icon = "🟢" if direction == "LONG" else "🔴"
    signal_id = payload.get("signal_id") or payload.get("id") or "n/d"
    runtime = state.get("runtime_version") or "n/d"
    return (
        f"{icon} <b>NUOVO SEGNALE VELEZ MODE 2 · PAPER</b> <code>#{_esc(signal_id)}</code>\n"
        f"<b>{_esc(payload.get('pair'))}</b> · <b>{direction}</b> · 15m\n"
        f"Mercato: <b>{_esc(payload.get('exchange') or 'Kraken')}</b>\n"
        f"Motore: <code>{_esc(runtime)}</code>\n\n"
        f"Entrata: <b>{_price(payload.get('entry'))}</b>\n"
        f"Stop iniziale: <b>{_price(payload.get('stop_loss'))}</b>\n"
        f"Livello attivazione protezione (+1,5R): <b>{_price(state.get('profit_protection_trigger_price'))}</b>\n"
        f"Stop protetto dopo attivazione (+1R): <b>{_price(state.get('profit_protected_stop_price'))}</b>\n"
        f"EMA20: <b>{_price(state.get('ema20'))}</b>\n"
        f"EMA200: <b>{_price(state.get('ema200'))}</b>\n"
        f"Setup: <b>pullback EMA20 + rottura barra precedente</b>\n\n"
        "Gestione Mode 2 protetta:\n"
        "• <b>nessun Take Profit fisso</b>\n"
        "• a <b>+1,5R</b> la posizione resta aperta e lo stop sale a <b>+1R</b>\n"
        "• poi uscita su stop protetto oppure chiusura 15m contro EMA20\n"
        "• uscita forzata dopo massimo 12 ore\n\n"
        "⚠️ Solo PAPER TRADING: nessun ordine reale."
    )


def format_velez_outcome(payload: dict[str, Any]) -> str:
    state = _state(payload)
    direction = str(payload.get("direction") or state.get("direction") or "LONG")
    runtime = state.get("runtime_version") or "n/d"
    result = _f(payload.get("realized_net_eur"))
    gross = _f(payload.get("gross_pnl_eur"))
    entry_fee = _f(payload.get("entry_fee_eur"))
    exit_fee = _f(payload.get("exit_fee_eur"))
    spread = _f(payload.get("spread_cost_eur"))
    slippage = _f(payload.get("slippage_cost_eur"))
    total = _f(payload.get("total_costs_eur"), entry_fee + exit_fee + spread + slippage)
    resolution = str(payload.get("outcome_resolution") or "n/d")
    labels = {
        "VELEZ_HARD_STOP": "Stop Loss iniziale raggiunto",
        "VELEZ_PROTECTED_STOP": "Stop protetto a +1R raggiunto",
        "VELEZ_EMA20_EXIT": "Chiusura 15m contro EMA20",
        "VELEZ_12H_TIME_EXIT": "Limite massimo di 12 ore raggiunto",
    }
    title = (
        "🟢 <b>POSIZIONE CHIUSA IN PROFITTO · PAPER</b>"
        if result > 0.005
        else "🟡 <b>POSIZIONE CHIUSA A PAREGGIO · PAPER</b>"
        if result >= -0.005
        else "🔴 <b>POSIZIONE CHIUSA IN PERDITA · PAPER</b>"
    )
    signal_id = payload.get("signal_id") or payload.get("id") or "n/d"
    budget = ""
    if payload.get("budget_before_eur") is not None:
        budget = (
            f"\n\nBudget paper: <b>€{_f(payload.get('budget_before_eur')):.2f}</b>"
            f" → <b>€{_f(payload.get('budget_after_eur')):.2f}</b>"
        )
    return (
        f"{title}\n"
        f"🆔 <code>#{_esc(signal_id)}</code>\n"
        f"<b>{_esc(payload.get('pair'))}</b> · <b>{direction}</b> · 15m\n"
        f"Motore: <code>{_esc(runtime)}</code>\n\n"
        f"Entrata: <b>{_price(payload.get('entry'))}</b>\n"
        f"Uscita: <b>{_price(payload.get('outcome_price'))}</b>\n"
        f"Motivo: <b>{_esc(labels.get(resolution, resolution))}</b>\n\n"
        f"P&amp;L lordo: <b>€{gross:+.2f}</b>\n"
        f"Fee apertura: <b>-€{entry_fee:.2f}</b>\n"
        f"Fee chiusura: <b>-€{exit_fee:.2f}</b>\n"
        f"Spread stimato: <b>-€{spread:.2f}</b>\n"
        f"Slippage stimato: <b>-€{slippage:.2f}</b>\n"
        f"Costi totali: <b>-€{total:.2f}</b>\n"
        f"Risultato netto: <b>€{result:+.2f}</b>"
        f"{budget}\n\n"
        "⚠️ Costi e prezzi sono stimati."
    )
