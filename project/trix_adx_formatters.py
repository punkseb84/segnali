"""Compact Telegram messages for the TRIX + ADX PAPER runtime."""
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


def format_trix_adx_signal_message(payload: dict[str, Any]) -> str:
    breakdown = _breakdown(payload)
    signal_id = payload.get("signal_id") or payload.get("id") or "n/d"
    entry = _float(payload.get("entry"))
    stop = _float(payload.get("stop_loss"))
    risk = _float(breakdown.get("risk_distance"), entry - stop)
    be_trigger = _float(breakdown.get("be_trigger_price"), entry + risk)
    tp1 = _float(breakdown.get("tp1_price"), entry + (2.0 * risk))
    budget = _float(
        payload.get("entry_notional_eur")
        or payload.get("trade_notional_eur"),
        100.0,
    )
    adx = _float(breakdown.get("adx_4h"))
    volume_ratio = _float(breakdown.get("volume_ratio"))
    return (
        "🧪 <b>NUOVO SEGNALE TRIX + ADX · PAPER</b> "
        f"<code>#{_escape(signal_id)}</code>\n"
        f"<b>{_escape(payload.get('pair'))}</b> · LONG · 1h\n\n"
        f"Entrata: <b>{_price(entry)}</b>\n"
        f"Stop strutturale: <b>{_price(stop)}</b>\n"
        f"Rischio 1R: <b>{_price(risk)}</b>\n"
        f"Trigger protezione +1R: <b>{_price(be_trigger)}</b>\n"
        f"TP1 50% a +2R: <b>{_price(tp1)}</b>\n\n"
        "Gestione:\n"
        "• a +1R → stop al vero pareggio netto, inclusi costi stimati\n"
        "• a +2R → chiusura del 50%\n"
        "• restante 50% → trailing 2,5 ATR e uscita su TRIX&lt;0 o close sotto EMA50\n\n"
        f"Conferme 4h: ADX <b>{adx:.1f}</b> · volume 1h <b>{volume_ratio:.2f}x</b>\n"
        f"Budget simulato: <b>€{budget:.2f}</b>\n\n"
        "⚠️ PAPER TRADING: nessuna operazione reale; strategia in forward test."
    )


def _resolution_label(resolution: str) -> str:
    labels = {
        "TRIX_INITIAL_STOP": "Stop strutturale iniziale",
        "TRIX_NET_BREAK_EVEN_STOP": "Stop al pareggio netto",
        "TRIX_TRAILING_STOP": "Trailing stop",
        "TRIX_ZERO_CROSS_EXIT": "TRIX tornato sotto zero",
        "TRIX_EMA50_EXIT": "Chiusura sotto EMA50",
        "TRIX_TP1_2R": "TP1 a +2R",
    }
    return labels.get(resolution, resolution or "n/d")


def format_trix_adx_outcome_message(payload: dict[str, Any]) -> str:
    signal_id = payload.get("signal_id") or payload.get("id") or "n/d"
    pair = _escape(payload.get("pair"))
    resolution = str(payload.get("outcome_resolution") or "")
    result = _float(payload.get("realized_net_eur"))
    if bool(payload.get("partial_take_profit")):
        return (
            "✅ <b>TP1 50% RAGGIUNTO · PAPER</b> "
            f"<code>#{_escape(signal_id)}</code>\n"
            f"<b>{pair}</b> · 1h\n\n"
            f"Chiusura 50% a: <b>{_price(payload.get('outcome_price'))}</b>\n"
            f"Risultato netto parziale: <b>€{result:+.2f}</b>\n"
            "Posizione residua: <b>50%</b>\n"
            "Gestione residua: trailing 2,5 ATR, stop almeno al pareggio netto."
        )

    title = (
        "🟢 <b>OPERAZIONE CHIUSA IN PROFITTO · PAPER</b>"
        if result > 0.005
        else "🟡 <b>OPERAZIONE CHIUSA A PAREGGIO · PAPER</b>"
        if result >= -0.005
        else "🔴 <b>OPERAZIONE CHIUSA IN PERDITA · PAPER</b>"
    )
    budget_lines = ""
    if payload.get("budget_before_eur") is not None:
        budget_lines = (
            f"\nBudget prima: <b>€{_float(payload.get('budget_before_eur')):.2f}</b>"
            f"\nBudget dopo: <b>€{_float(payload.get('budget_after_eur')):.2f}</b>"
        )
    return (
        f"{title}\n"
        f"🆔 <code>#{_escape(signal_id)}</code>\n"
        f"<b>{pair}</b> · 1h\n\n"
        f"Entrata: <b>{_price(payload.get('entry'))}</b>\n"
        f"Uscita: <b>{_price(payload.get('outcome_price'))}</b>\n"
        f"Risultato netto stimato: <b>€{result:+.2f}</b>\n"
        f"Motivo: <b>{_escape(_resolution_label(resolution))}</b>"
        f"{budget_lines}\n\n"
        "⚠️ Esito PAPER: commissioni, spread e slippage sono stimati."
    )
