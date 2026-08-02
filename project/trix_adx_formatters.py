"""Telegram messages for the TRIX Pulse V3 LONG/SHORT PAPER runtime."""
from __future__ import annotations

import html
from typing import Any


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


def _escape(value: Any) -> str:
    return html.escape(str(value if value is not None else "n/d"), quote=True)


def _breakdown(payload: dict[str, Any]) -> dict[str, Any]:
    raw = payload.get("score_breakdown") or {}
    return dict(raw) if isinstance(raw, dict) else {}


def _direction(payload: dict[str, Any]) -> str:
    breakdown = _breakdown(payload)
    return "SHORT" if str(payload.get("direction") or breakdown.get("direction") or "LONG").upper() == "SHORT" else "LONG"


def format_trix_adx_signal_message(payload: dict[str, Any]) -> str:
    breakdown = _breakdown(payload)
    direction = _direction(payload)
    signal_id = payload.get("signal_id") or payload.get("id") or "n/d"
    entry = _float(payload.get("entry"))
    stop = _float(payload.get("stop_loss"))
    risk = _float(breakdown.get("risk_distance"), abs(entry - stop))
    sign = 1.0 if direction == "LONG" else -1.0
    be_r = _float(breakdown.get("be_trigger_r"), 0.8)
    tp_r = _float(breakdown.get("tp1_r"), 1.5)
    trailing = _float(breakdown.get("trailing_atr_multiple"), 1.8)
    be_trigger = _float(breakdown.get("be_trigger_price"), entry + sign * be_r * risk)
    tp1 = _float(breakdown.get("tp1_price"), entry + sign * tp_r * risk)
    budget = _float(payload.get("entry_notional_eur") or payload.get("trade_notional_eur"), 100.0)
    adx = _float(breakdown.get("adx_1h"))
    volume_ratio = _float(breakdown.get("volume_ratio"))
    setup = _escape(breakdown.get("setup") or "TRIX_PULSE")
    icon = "🟢" if direction == "LONG" else "🔴"
    return (
        f"{icon} <b>NUOVO SEGNALE TRIX PULSE V3 · PAPER</b> <code>#{_escape(signal_id)}</code>\n"
        f"<b>{_escape(payload.get('pair'))}</b> · <b>{direction}</b> · 15m\n"
        f"Setup: <b>{setup}</b>\n\n"
        f"Entrata: <b>{_price(entry)}</b>\n"
        f"Stop: <b>{_price(stop)}</b>\n"
        f"Rischio 1R: <b>{_price(risk)}</b>\n"
        f"Protezione +{be_r:.1f}R: <b>{_price(be_trigger)}</b>\n"
        f"TP1 50% a +{tp_r:.1f}R: <b>{_price(tp1)}</b>\n\n"
        "Gestione:\n"
        f"• a +{be_r:.1f}R → stop al pareggio netto stimato\n"
        f"• a +{tp_r:.1f}R → chiusura del 50%\n"
        f"• restante 50% → trailing {trailing:.1f} ATR e uscita su inversione TRIX/EMA50\n\n"
        f"Contesto 1h: ADX <b>{adx:.1f}</b> · volume 15m <b>{volume_ratio:.2f}x</b>\n"
        f"Budget simulato: <b>€{budget:.2f}</b>\n\n"
        "⚠️ PAPER TRADING: nessuna operazione reale."
    )


def _resolution_label(resolution: str) -> str:
    labels = {
        "TRIX_LONG_INITIAL_STOP": "Stop iniziale LONG",
        "TRIX_SHORT_INITIAL_STOP": "Stop iniziale SHORT",
        "TRIX_NET_BREAK_EVEN_STOP": "Stop al pareggio netto",
        "TRIX_TRAILING_STOP": "Trailing stop",
        "TRIX_PULSE_DIRECTIONAL_EXIT": "Inversione TRIX/EMA50",
        "TRIX_TP1_1_5R": "TP1 a +1,5R",
    }
    return labels.get(resolution, resolution or "n/d")


def format_trix_adx_outcome_message(payload: dict[str, Any]) -> str:
    signal_id = payload.get("signal_id") or payload.get("id") or "n/d"
    pair = _escape(payload.get("pair"))
    direction = _direction(payload)
    resolution = str(payload.get("outcome_resolution") or "")
    result = _float(payload.get("realized_net_eur"))
    if bool(payload.get("partial_take_profit")):
        return (
            "✅ <b>TP1 50% RAGGIUNTO · PAPER</b> "
            f"<code>#{_escape(signal_id)}</code>\n"
            f"<b>{pair}</b> · {direction} · 15m\n\n"
            f"Chiusura 50% a: <b>{_price(payload.get('outcome_price'))}</b>\n"
            f"Risultato netto parziale: <b>€{result:+.2f}</b>\n"
            "Posizione residua: <b>50%</b> · trailing 1,8 ATR attivo."
        )
    title = "🟢 <b>OPERAZIONE CHIUSA IN PROFITTO · PAPER</b>" if result > 0.005 else "🟡 <b>OPERAZIONE CHIUSA A PAREGGIO · PAPER</b>" if result >= -0.005 else "🔴 <b>OPERAZIONE CHIUSA IN PERDITA · PAPER</b>"
    budget_lines = ""
    if payload.get("budget_before_eur") is not None:
        budget_lines = f"\nBudget prima: <b>€{_float(payload.get('budget_before_eur')):.2f}</b>\nBudget dopo: <b>€{_float(payload.get('budget_after_eur')):.2f}</b>"
    return (
        f"{title}\n"
        f"🆔 <code>#{_escape(signal_id)}</code>\n"
        f"<b>{pair}</b> · {direction} · 15m\n\n"
        f"Entrata: <b>{_price(payload.get('entry'))}</b>\n"
        f"Uscita: <b>{_price(payload.get('outcome_price'))}</b>\n"
        f"Risultato netto stimato: <b>€{result:+.2f}</b>\n"
        f"Motivo: <b>{_escape(_resolution_label(resolution))}</b>{budget_lines}\n\n"
        "⚠️ Esito PAPER: commissioni, spread e slippage sono stimati."
    )