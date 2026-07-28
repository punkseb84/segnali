"""Outcome formatter with compact Ichimoku posts and audited legacy fallbacks."""
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


def _is_ichimoku(payload: dict[str, Any], resolution: str) -> bool:
    return "ICHIMOKU" in str(payload.get("strategy") or "").upper() or resolution.startswith("ICHIMOKU_")


def _tp1_price(payload: dict[str, Any]) -> Any:
    explicit = payload.get("tp1_price") or payload.get("take_profit")
    if explicit is not None:
        return explicit
    breakdown = payload.get("score_breakdown") or {}
    atr = _float(breakdown.get("atr")) if isinstance(breakdown, dict) else 0.0
    entry = payload.get("entry")
    return _float(entry) + (2.0 * atr) if entry is not None and atr > 0 else None


def _format_compact_ichimoku(payload: dict[str, Any], resolution: str, signal_id: Any) -> str:
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
        result = -abs(_float(payload.get("net_loss_sl_eur"))) if is_stop else _float(payload.get("net_profit_tp1_eur"))

    tp1_hit = bool(payload.get("tp1_hit"))
    prior_net = _float(payload.get("tp1_realized_net_eur"))
    final_leg_net = result - prior_net if tp1_hit else result
    budget_before = _float(payload.get("budget_before_eur"), 100.0)
    budget_after = _float(payload.get("budget_after_eur"), budget_before + result)
    title = "🟢 <b>OPERAZIONE CHIUSA</b>" if result >= 0 else "🔴 <b>OPERAZIONE CHIUSA</b>"
    outcome_label = "PROFITTO" if result >= 0 else "PERDITA"
    entry_price = payload.get("entry")
    final_exit_price = payload.get("outcome_price")
    tp1_level = _tp1_price(payload)
    price_lines = f"Entrata: <b>{_price(entry_price)}</b>\n"
    if tp1_hit and tp1_level is not None:
        price_lines += f"TP1 50% a: <b>{_price(tp1_level)}</b>\n"
        price_lines += f"Uscita finale 50% a: <b>{_price(final_exit_price)}</b>\n\n"
    else:
        price_lines += f"Uscita finale a: <b>{_price(final_exit_price)}</b>\n\n"
    result_lines = ""
    if tp1_hit:
        result_lines = (
            f"TP1 50%: <b>{_money(prior_net, signed=True)}</b>\n"
            f"Uscita restante: <b>{_money(final_leg_net, signed=True)}</b>\n"
        )
    return (
        f"{title} <code>#{_escape(signal_id)}</code>\n"
        f"<b>{_escape(payload.get('pair'))}</b> · <b>{outcome_label}</b>\n\n"
        f"{price_lines}"
        f"{result_lines}"
        f"Risultato totale: <b>{_money(result, signed=True)}</b>\n\n"
        f"Budget iniziale: <b>{_money(budget_before)}</b>\n"
        f"Budget aggiornato: <b>{_money(budget_after)}</b>"
    )


def _format_legacy_audited(payload: dict[str, Any], resolution: str, signal_id: Any) -> str:
    is_stop = str(payload.get("outcome")) == "STOP_LOSS"
    result = _float(payload.get("realized_net_eur"))
    if payload.get("realized_net_eur") is None:
        result = -abs(_float(payload.get("net_loss_sl_eur"))) if is_stop else _float(payload.get("net_profit_tp1_eur"))
    title = "🔴 <b>STOP LOSS RAGGIUNTO</b>" if is_stop else "🎯 <b>TAKE PROFIT RAGGIUNTO</b>"
    open_price = payload.get("outcome_open_price")
    high = payload.get("outcome_high_price")
    low = payload.get("outcome_low_price")
    close_price = payload.get("close_price")
    candle_block = ""
    if all(value is not None for value in (open_price, high, low, close_price)):
        candle_block = (
            "\n📐 <b>Candela reale che ha chiuso il trade</b>\n"
            f"• Open reale: <b>{_price(open_price)}</b>\n"
            f"• Massimo reale: <b>{_price(high)}</b>\n"
            f"• Minimo reale: <b>{_price(low)}</b>\n"
            f"• Close reale: <b>{_price(close_price)}</b>\n"
        )
    ambiguity = (
        "\n⚠️ <b>Candela ambigua: SÌ · applicata gestione conservativa.</b>\n"
        if payload.get("ambiguous")
        else ""
    )
    return (
        f"{title}\n"
        f"🆔 <code>#{_escape(signal_id)}</code>\n\n"
        f"<b>{_escape(payload.get('pair'))}</b> · {_escape(payload.get('timeframe'))}\n"
        f"Strategia: <b>{_escape(payload.get('strategy'))}</b>\n"
        f"Exchange: <b>{_escape(payload.get('exchange'))}</b>\n\n"
        f"• Entry: <b>{_price(payload.get('entry'))}</b>\n"
        f"• Uscita: <b>{_price(payload.get('outcome_price'))}</b>\n"
        f"• Risultato netto: <b>{_money(result, signed=True)}</b>\n"
        f"{ambiguity}{candle_block}"
        f"• Candela di chiusura: <b>{_escape(payload.get('closed_at'))}</b>\n"
        f"• Motivo: <b>{_escape(resolution or 'n/d')}</b>"
    )


def format_audited_outcome_message(payload: dict[str, Any]) -> str:
    resolution = str(payload.get("outcome_resolution") or "")
    signal_id = payload.get("signal_id") or payload.get("id") or "n/d"
    if _is_ichimoku(payload, resolution):
        return _format_compact_ichimoku(payload, resolution, signal_id)
    return _format_legacy_audited(payload, resolution, signal_id)


format_simple_outcome_message = format_audited_outcome_message
