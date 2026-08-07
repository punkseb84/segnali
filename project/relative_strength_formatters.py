"""Telegram presentation for public and private relative-strength PAPER engines."""
from __future__ import annotations

import html
from typing import Any


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _price(value: Any) -> str:
    number = _f(value)
    decimals = 2 if abs(number) >= 1000 else 3 if abs(number) >= 1 else 5 if abs(number) >= 0.01 else 8
    return f"{number:,.{decimals}f}"


def _esc(value: Any) -> str:
    return html.escape(str(value if value is not None else "n/d"), quote=True)


def _state(payload: dict[str, Any]) -> dict[str, Any]:
    raw = payload.get("score_breakdown") or payload.get("state") or {}
    return dict(raw) if isinstance(raw, dict) else {}


def _is_private(payload: dict[str, Any]) -> bool:
    state = _state(payload)
    return bool(payload.get("private_portfolio") or state.get("private_portfolio"))


def format_relative_strength_signal(payload: dict[str, Any]) -> str:
    state = _state(payload)
    direction = str(state.get("direction", "LONG"))
    signal_id = payload.get("signal_id") or payload.get("id") or "n/d"
    exchange = payload.get("exchange") or "Kraken"
    if _is_private(payload):
        expected_net = _f(payload.get("net_profit_tp1_eur"))
        budget = _f(state.get("budget_before_eur") or payload.get("entry_notional_eur"), 10.0)
        return (
            "🔐 <b>INVESTIMENTO PRIVATO · ACQUISTA</b> "
            f"<code>#{_esc(signal_id)}</code>\n"
            f"Crypto: <b>{_esc(payload.get('pair'))}</b> · SPOT LONG\n"
            f"Mercato di riferimento: <b>{_esc(exchange)}</b>\n\n"
            f"Capitale simulato: <b>€{budget:.2f}</b>\n"
            f"Entrata indicativa: <b>{_price(payload.get('entry'))}</b>\n"
            f"Stop Loss: <b>{_price(payload.get('stop_loss'))}</b>\n"
            f"Take Profit previsto: <b>{_price(payload.get('take_profit'))}</b>\n"
            f"Profitto netto stimato al target: <b>€{expected_net:+.2f}</b>\n"
            f"Rank globale: <b>{_esc(state.get('rank'))}/{_esc(state.get('universe_size'))}</b>\n"
            f"RS score vs BTC: <b>{_f(state.get('rs_score')):+.3f}</b>\n\n"
            "Azione: compra la crypto indicata usando il capitale personale dedicato.\n"
            "Il bot invierà l'avviso per vendere e tornare in USDT a target, stop, inversione RS o entro 12 ore.\n"
            "⚠️ PAPER/decision support: verifica sempre il prezzo sullo stesso mercato di riferimento."
        )

    icon = "🟢" if direction == "LONG" else "🔴"
    vol1h = _f(state.get("volume_ratio_1h"), _f(state.get("volume_ratio")))
    vol15 = _f(state.get("volume_ratio_15m"), 0.0)
    volume_line = f"Volume robusto 1h: <b>{vol1h:.2f}x</b>"
    if state.get("volume_ratio_15m") is not None:
        volume_line += f" · ultima 15m: <b>{vol15:.2f}x</b>"
    return (
        f"{icon} <b>NUOVO SEGNALE FORZA RELATIVA · PAPER</b> <code>#{_esc(signal_id)}</code>\n"
        f"<b>{_esc(payload.get('pair'))}</b> · <b>{direction}</b> · 15m\n"
        f"Mercato di riferimento: <b>{_esc(exchange)}</b>\n\n"
        f"Entrata: <b>{_price(payload.get('entry'))}</b>\n"
        f"Stop: <b>{_price(payload.get('stop_loss'))}</b>\n"
        f"Target previsto: <b>{_price(payload.get('take_profit'))}</b>\n"
        f"Rank globale: <b>{_esc(state.get('rank'))}/{_esc(state.get('universe_size'))}</b>\n"
        f"RS score vs BTC: <b>{_f(state.get('rs_score')):+.3f}</b>\n"
        f"RS 4h: <b>{_f(state.get('relative_4h')) * 100:+.2f}%</b>\n"
        f"RS 1h: <b>{_f(state.get('relative_1h')) * 100:+.2f}%</b>\n"
        f"{volume_line}\n\n"
        "Il bot monitorerà automaticamente target, stop, inversione RS e durata massima sul mercato indicato.\n"
        "Alla chiusura riceverai P&amp;L lordo, costi, risultato netto e budget aggiornato.\n"
        "⚠️ Solo PAPER TRADING: nessun ordine reale."
    )


def format_relative_strength_outcome(payload: dict[str, Any]) -> str:
    private = _is_private(payload)
    direction = str(payload.get("direction") or _state(payload).get("direction") or "LONG")
    exchange = payload.get("exchange") or "Kraken"
    result = _f(payload.get("realized_net_eur"))
    gross = _f(payload.get("gross_pnl_eur"))
    entry_fee = _f(payload.get("entry_fee_eur"))
    exit_fee = _f(payload.get("exit_fee_eur"))
    spread = _f(payload.get("spread_cost_eur"))
    slippage = _f(payload.get("slippage_cost_eur"))
    total_costs = _f(payload.get("total_costs_eur"), entry_fee + exit_fee + spread + slippage)
    resolution = str(payload.get("outcome_resolution") or "n/d")
    labels = {
        "RELATIVE_STRENGTH_TARGET": "Take Profit raggiunto",
        "RELATIVE_STRENGTH_LIVE_TARGET": "Take Profit raggiunto (prezzo live)",
        "RELATIVE_STRENGTH_STOP": "Stop Loss raggiunto",
        "RELATIVE_STRENGTH_LIVE_STOP": "Stop Loss raggiunto (prezzo live)",
        "RELATIVE_STRENGTH_REVERSAL": "Forza relativa invertita",
        "RELATIVE_STRENGTH_TIME_EXIT": "Durata massima raggiunta",
        "RELATIVE_STRENGTH_AMBIGUOUS_BOTH_HIT": "Stop e target nella stessa candela (ordine intrabar non determinabile)",
        "PRIVATE_SPOT_TARGET": "Take Profit raggiunto",
        "PRIVATE_SPOT_STOP": "Stop Loss raggiunto",
        "PRIVATE_SPOT_RS_REVERSAL": "Forza relativa invertita",
        "PRIVATE_SPOT_12H_EXIT": "Limite massimo di 12 ore",
    }
    if private:
        title = "✅ <b>INVESTIMENTO PRIVATO · VENDI E TORNA IN USDT</b>"
    else:
        title = "🟢 <b>POSIZIONE CHIUSA IN PROFITTO · PAPER</b>" if result > 0.005 else "🟡 <b>POSIZIONE CHIUSA A PAREGGIO · PAPER</b>" if result >= -0.005 else "🔴 <b>POSIZIONE CHIUSA IN PERDITA · PAPER</b>"
    signal_id = payload.get("signal_id") or payload.get("id") or "n/d"
    budget = ""
    if payload.get("budget_before_eur") is not None:
        budget = (
            f"\n\nBudget {'personale' if private else 'paper'}: "
            f"<b>€{_f(payload.get('budget_before_eur')):.2f}</b>"
            f" → <b>€{_f(payload.get('budget_after_eur')):.2f}</b>"
        )
    action = "\nAzione: <b>vendi la posizione e converti il ricavato in USDT.</b>\n" if private else ""
    return (
        f"{title}\n"
        f"🆔 <code>#{_esc(signal_id)}</code>\n"
        f"<b>{_esc(payload.get('pair'))}</b> · <b>{direction}</b> · 15m\n"
        f"Mercato verificato: <b>{_esc(exchange)}</b>\n\n"
        f"Entrata: <b>{_price(payload.get('entry'))}</b>\n"
        f"Uscita: <b>{_price(payload.get('outcome_price'))}</b>\n"
        f"Motivo reale: <b>{_esc(labels.get(resolution, resolution))}</b>\n\n"
        f"P&amp;L lordo: <b>€{gross:+.2f}</b>\n"
        f"Fee apertura: <b>-€{entry_fee:.2f}</b>\n"
        f"Fee chiusura: <b>-€{exit_fee:.2f}</b>\n"
        f"Spread stimato: <b>-€{spread:.2f}</b>\n"
        f"Slippage stimato: <b>-€{slippage:.2f}</b>\n"
        f"Costi totali: <b>-€{total_costs:.2f}</b>\n"
        f"Risultato netto: <b>€{result:+.2f}</b>"
        f"{budget}\n"
        f"{action}\n"
        "⚠️ Costi e prezzi di esecuzione sono stimati."
    )