"""Readable and auditable HTML formatters for Telegram notifications."""
from __future__ import annotations

import html
import json
from typing import Any, Iterable


CLASSIFICATION_LABELS = {
    "NET_PROFIT_BELOW_MINIMUM": "profitto netto inferiore alla soglia",
    "NET_RR_BELOW_MINIMUM": "R/R netto inferiore alla soglia",
    "TOTAL_SCORE_BELOW_MINIMUM": "score complessivo insufficiente",
    "LIVE_SCORE_BELOW_MINIMUM": "qualità del setup corrente insufficiente",
    "PROFIT_FACTOR_BELOW_MINIMUM": "Profit Factor storico sotto soglia",
    "EXPECTANCY_NOT_POSITIVE": "expectancy storica non positiva",
    "REGIME_MISMATCH_WITHOUT_SCORE_BUFFER": "strategia non coerente con il regime e senza score compensativo",
    "VALIDATION_WARNING_NOT_COMPENSATED": "avvisi tecnici non compensati da una qualità sufficiente",
    "RESEARCH_SAMPLE_MISSING": "numero di trade della ricerca non disponibile",
    "RESEARCH_SAMPLE_LIMITED": "campione specifico della strategia limitato",
    "PROBABILITY_SAMPLE_LOW": "campione probabilistico ridotto",
    "HISTORICAL_METRICS_SHRUNK": "peso di Profit Factor ed expectancy ridotto per prudenza",
    "EV_CONFIDENCE_LIMITED": "confidenza dell’EV storico limitata",
    "OPERATIVE_REQUIREMENTS_MET": "tutti i requisiti operativi sono soddisfatti",
    "WATCHLIST_OBSERVATION": "setup conservato come osservazione",
}


def _escape(value: Any, fallback: str = "n/d") -> str:
    if value is None or value == "":
        return fallback
    return html.escape(str(value), quote=True)


def _float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _price(value: Any) -> str:
    number = _float(value)
    if number is None:
        return "n/d"
    absolute = abs(number)
    decimals = 2 if absolute >= 1000 else 3 if absolute >= 1 else 5 if absolute >= 0.01 else 8
    return f"{number:,.{decimals}f}"


def _money(value: Any, signed: bool = False) -> str:
    number = _float(value)
    if number is None:
        return "n/d"
    prefix = "+" if signed and number > 0 else ""
    return f"{prefix}€{number:.2f}"


def _number(value: Any, decimals: int = 2) -> str:
    number = _float(value)
    return "n/d" if number is None else f"{number:.{decimals}f}"


def _percentage(value: Any) -> str:
    number = _float(value)
    if number is None:
        return "n/d"
    if abs(number) <= 1:
        number *= 100
    return f"{number:.1f}%"


def _timestamp(value: Any) -> str:
    if value is None:
        return "n/d"
    text = str(value).replace("T", " ").replace("+00:00", " UTC").replace("Z", " UTC")
    return _escape(text)


def _signal_id(payload: dict[str, Any], linked: bool = False) -> str:
    signal_id = payload.get("signal_id") or payload.get("id")
    label = f"#{signal_id}" if signal_id is not None else "n/d"
    link = payload.get("telegram_message_link") or payload.get("signal_message_link")
    if linked and link and str(link).startswith("https://t.me/"):
        return f'<a href="{html.escape(str(link), quote=True)}"><b>{html.escape(label)}</b></a>'
    return f"<code>{html.escape(label)}</code>"


def _reason_value(payload: dict[str, Any], prefix: str) -> str | None:
    for reason in payload.get("reasons") or []:
        text = str(reason)
        if text.startswith(prefix):
            return text[len(prefix):]
    return None


def _reason_codes(payload: dict[str, Any], prefix: str) -> list[str]:
    value = _reason_value(payload, prefix)
    if not value or value == "NONE":
        return []
    return [item for item in value.split("|") if item and item != "NONE"]


def _score_breakdown(payload: dict[str, Any]) -> dict[str, Any]:
    raw = payload.get("score_breakdown")
    return raw if isinstance(raw, dict) else {}


def _metric(payload: dict[str, Any], key: str, fallback: Any = None) -> Any:
    breakdown = _score_breakdown(payload)
    return breakdown.get(key, fallback)


def _pass_line(ok: bool, label: str, value: str, threshold: str | None = None) -> str:
    symbol = "✅" if ok else "❌"
    comparison = f" · soglia {threshold}" if threshold is not None else ""
    return f"{symbol} {label}: <b>{_escape(value)}</b>{_escape(comparison, '')}"


def _classification_lines(payload: dict[str, Any]) -> str:
    blockers = _reason_codes(payload, "classification_blockers=")
    notes = _reason_codes(payload, "classification_confidence_notes=")
    primary = _reason_value(payload, "classification_primary_reason=")
    items = blockers or ([primary] if primary and primary != "OPERATIVE_REQUIREMENTS_MET" else [])
    if not items:
        items = ["WATCHLIST_OBSERVATION"]
    lines = [f"• {_escape(CLASSIFICATION_LABELS.get(code, code.replace('_', ' ').lower()))}" for code in items]
    if notes:
        lines.append("\n🔎 <b>Note sulla confidenza</b>")
        lines.extend(
            f"• {_escape(CLASSIFICATION_LABELS.get(code, code.replace('_', ' ').lower()))}"
            for code in notes
        )
    return "\n".join(lines)


def _requirements_lines(payload: dict[str, Any]) -> str:
    breakdown = _score_breakdown(payload)
    net_profit = float(_float(payload.get("net_profit_tp1_eur")) or 0.0)
    min_profit = float(
        _float(breakdown.get("min_net_profit_required_raw"))
        or _float(_reason_value(payload, "min_net_profit_required="))
        or 0.30
    )
    net_rr = float(_float(payload.get("net_rr")) or 0.0)
    min_rr = float(
        _float(breakdown.get("min_net_rr_required_raw"))
        or _float(_reason_value(payload, "min_net_rr_required="))
        or 1.05
    )
    score = float(_float(payload.get("score")) or 0.0)
    min_score = float(_float(breakdown.get("min_total_score_required_raw")) or 55.0)
    live_score = float(_float(breakdown.get("live_setup_raw")) or 0.0)
    min_live = float(_float(breakdown.get("min_live_score_required_raw")) or 45.0)
    regime_aligned = bool(float(_float(breakdown.get("regime_aligned_flag")) or 0.0))
    validation = str(payload.get("validation_status") or "n/d")
    validation_ok = validation == "PASSED"
    return "\n".join(
        [
            _pass_line(net_profit + 1e-9 >= min_profit, "Profitto netto", f"€{net_profit:.4f}", f"≥ €{min_profit:.4f}"),
            _pass_line(net_rr + 1e-9 >= min_rr, "R/R netto", f"{net_rr:.4f}", f"≥ {min_rr:.4f}"),
            _pass_line(score >= min_score, "Score totale", f"{score:.1f}/100", f"≥ {min_score:.1f}"),
            _pass_line(live_score >= min_live, "Score setup live", f"{live_score:.1f}/100", f"≥ {min_live:.1f}"),
            _pass_line(regime_aligned, "Regime", "coerente" if regime_aligned else "non coerente"),
            _pass_line(validation_ok, "Validazione", validation),
        ]
    )


def _reason_text(reason: Any) -> str | None:
    text = str(reason or "").strip()
    if not text or text.startswith("score_breakdown="):
        return None
    if text.startswith(
        (
            "classification_primary_reason=",
            "classification_blockers=",
            "classification_confidence_notes=",
            "probability_sample=",
            "net_rr_exact=",
            "min_net_rr_required=",
            "min_net_profit_required=",
        )
    ):
        return None
    mappings = {
        "profit_factor=": "Profit factor storico",
        "expectancy=": "Expectancy storica",
        "reward_risk=": "R/R tecnico",
        "atr_multiplier=": "Moltiplicatore ATR",
        "regime=": "Regime",
        "signal_class=": "Classe",
        "historical_ev=": "EV storico",
        "historical_ev_decision=": "Valutazione EV",
        "research_trades=": "Trade ricerca specifica",
        "live_setup_score=": "Score setup live",
        "economic_target_feasible=": "Target economicamente fattibile",
    }
    for prefix, label in mappings.items():
        if text.startswith(prefix):
            return f"{label}: {text[len(prefix):]}"
    if text == "regime_aligned=true":
        return "Strategia coerente con il regime"
    if text == "regime_aligned=false":
        return "Strategia non coerente con il regime"
    return text.replace("_", " ")


def _reason_lines(reasons: Iterable[Any] | None, limit: int = 4) -> str:
    readable: list[str] = []
    for reason in reasons or []:
        converted = _reason_text(reason)
        if converted and converted not in readable:
            readable.append(converted)
        if len(readable) >= limit:
            break
    if not readable:
        return "• Nessun dettaglio aggiuntivo"
    return "\n".join(f"• {_escape(item)}" for item in readable)


def _technical_appendix(payload: dict[str, Any]) -> str:
    historical_ev = payload.get("historical_expected_value_eur")
    historical_ev_text = "non disponibile" if historical_ev is None else str(historical_ev)
    return (
        "\n\n🧾 <b>Dettagli tecnici</b>\n"
        f"Class: {_escape(payload.get('signal_class', 'B'))}\n"
        f"Quantity: {_number(payload.get('quantity'), 8)}\n"
        f"Gross R/R: {_number(payload.get('gross_rr'), 4)}\n"
        f"Technical TP: {_number(payload.get('technical_take_profit'), 6)}\n"
        f"Effective TP: {_number(payload.get('effective_take_profit'), 6)}\n"
        f"Probability confidence: {_escape(payload.get('probability_confidence'))}\n"
        f"Historical EV: {_escape(historical_ev_text)}\n"
        f"Validation: {_escape(payload.get('validation_status'))}\n"
        f"Net profit TP1: €{_number(payload.get('net_profit_tp1_eur'), 4)}\n"
        f"Net loss SL: €{_number(payload.get('net_loss_sl_eur'), 4)}\n"
        f"Net R/R: {_number(payload.get('net_rr'), 4)}\n"
        "Costi: "
        f"buy fee €{_number(payload.get('estimated_buy_fee_eur'), 4)}, "
        f"sell fee TP €{_number(payload.get('estimated_sell_fee_eur'), 4)}, "
        f"sell fee SL €{_number(payload.get('estimated_sell_fee_sl_eur'), 4)}, "
        f"spread €{_number(payload.get('estimated_spread_cost_eur'), 4)}, "
        f"slippage €{_number(payload.get('estimated_slippage_cost_eur'), 4)}"
    )


def format_signal_message(payload: dict[str, Any]) -> str:
    signal_class = _escape(payload.get("signal_class", "B"))
    breakdown = _score_breakdown(payload)
    research_trades = int(_float(breakdown.get("research_trades_raw")) or 0)
    probability_sample = int(
        _float(breakdown.get("probability_sample_raw"))
        or _float(payload.get("historical_sample_size"))
        or 0
    )
    live_score = _float(breakdown.get("live_setup_raw"))
    return (
        f"🟢 <b>NUOVO SEGNALE LONG</b> · Classe <b>{signal_class}</b>\n"
        f"🆔 Segnale {_signal_id(payload)}\n\n"
        f"<b>{_escape(payload.get('pair'))}</b> · {_escape(payload.get('timeframe'))}\n"
        f"Strategia: <b>{_escape(payload.get('strategy'))}</b>\n"
        f"Regime: <b>{_escape(payload.get('regime'))}</b>\n\n"
        "💰 <b>Livelli operativi</b>\n"
        f"• Entry: <code>{_price(payload.get('entry'))}</code>\n"
        f"• Stop Loss: <code>{_price(payload.get('stop_loss'))}</code>\n"
        f"• Target: <code>{_price(payload.get('effective_take_profit') or payload.get('take_profit'))}</code>\n"
        "• Entrata: <b>immediata alla ricezione</b>\n\n"
        "📊 <b>Rischio e rendimento</b>\n"
        f"• Profitto netto a target: <b>{_money(payload.get('net_profit_tp1_eur'))}</b>\n"
        f"• Perdita netta a stop: <b>{_money(payload.get('net_loss_sl_eur'))}</b>\n"
        f"• R/R netto: <b>{_number(payload.get('net_rr'), 4)}</b>\n"
        f"• Capitale impiegato: <b>{_money(payload.get('entry_notional_eur'))}</b>\n\n"
        "🧠 <b>Qualità e confidenza</b>\n"
        f"• Score totale: <b>{_number(payload.get('score'), 1)}/100</b>\n"
        f"• Score setup live: <b>{_number(live_score, 1)}/100</b>\n"
        f"• Trade ricerca specifica: <b>{research_trades}</b>\n"
        f"• Campione probabilistico: <b>{probability_sample}</b>\n"
        f"• Probabilità storica: <b>{_percentage(payload.get('probability'))}</b>\n"
        f"• Confidenza: <b>{_escape(payload.get('probability_confidence'))}</b>\n\n"
        "📌 <b>Motivi principali</b>\n"
        f"{_reason_lines(payload.get('reasons'))}\n\n"
        f"🕯 Candela di riferimento: {_timestamp(payload.get('reference_candle_time'))}\n"
        f"⏱ Generato: {_timestamp(payload.get('signal_time'))}\n\n"
        "⚠️ <i>Segnale in dry-run: verifica sempre prezzo, liquidità e costi prima di operare.</i>"
        f"{_technical_appendix(payload)}"
    )


def format_watchlist_message(payload: dict[str, Any]) -> str:
    breakdown = _score_breakdown(payload)
    research_trades = int(_float(breakdown.get("research_trades_raw")) or 0)
    research_missing = bool(_float(breakdown.get("research_trades_missing_flag")) or 0.0)
    probability_sample = int(
        _float(breakdown.get("probability_sample_raw"))
        or _float(payload.get("historical_sample_size"))
        or 0
    )
    return (
        "🟡 <b>WATCHLIST</b> · Classe <b>C</b>\n"
        f"🆔 Osservazione {_signal_id(payload)}\n\n"
        f"<b>{_escape(payload.get('pair'))}</b> · {_escape(payload.get('timeframe'))}\n"
        f"Strategia: <b>{_escape(payload.get('strategy'))}</b>\n"
        f"Regime: <b>{_escape(payload.get('regime'))}</b>\n\n"
        "👀 <b>SETUP NON ANCORA OPERATIVO</b>\n\n"
        "💰 <b>Livelli teorici</b>\n"
        f"• Entry: <code>{_price(payload.get('entry'))}</code>\n"
        f"• Stop: <code>{_price(payload.get('stop_loss'))}</code>\n"
        f"• Target: <code>{_price(payload.get('effective_take_profit') or payload.get('take_profit'))}</code>\n\n"
        "🚦 <b>Verifica requisiti A/B</b>\n"
        f"{_requirements_lines(payload)}\n\n"
        "⛔ <b>Motivo effettivo della classe C</b>\n"
        f"{_classification_lines(payload)}\n\n"
        "📐 <b>Confidenza statistica</b>\n"
        f"• Trade ricerca specifica: <b>{'non disponibili' if research_missing else research_trades}</b>\n"
        f"• Campione probabilistico: <b>{probability_sample}</b>\n"
        f"• Confidenza probabilistica: <b>{_escape(payload.get('probability_confidence'))}</b>\n"
        f"• Validazione: <b>{_escape(payload.get('validation_status'))}</b>"
        f" ({_escape(payload.get('validation_reason'))})\n\n"
        "📌 <b>Elementi osservati</b>\n"
        f"{_reason_lines(payload.get('reasons'))}\n\n"
        "⚠️ <b>Solo monitoraggio:</b> entrare soltanto quando il bot pubblica un nuovo segnale A/B."
    )


def format_outcome_message(payload: dict[str, Any]) -> str:
    is_stop = payload.get("outcome") == "STOP_LOSS"
    title = "🔴 <b>STOP LOSS RAGGIUNTO</b>" if is_stop else "🎯 <b>TARGET RAGGIUNTO</b>"
    result_value = payload.get("net_loss_sl_eur") if is_stop else payload.get("net_profit_tp1_eur")
    result_text = f"-€{abs(_float(result_value) or 0):.2f}" if is_stop else _money(result_value, signed=True)
    link_note = "Tocca l’ID o la risposta Telegram per aprire il segnale originale."
    return (
        f"{title}\n"
        f"🆔 Segnale {_signal_id(payload, linked=True)}\n\n"
        f"<b>{_escape(payload.get('pair'))}</b> · {_escape(payload.get('timeframe'))}\n"
        f"Strategia: <b>{_escape(payload.get('strategy'))}</b>\n"
        f"Classe iniziale: <b>{_escape(payload.get('signal_class', 'B'))}</b>\n\n"
        "📍 <b>Esito operazione</b>\n"
        f"• Entry: <code>{_price(payload.get('entry'))}</code>\n"
        f"• Livello raggiunto: <code>{_price(payload.get('outcome_price'))}</code>\n"
        f"• Risultato netto stimato: <b>{result_text}</b>\n"
        f"• R/R iniziale: <b>{_number(payload.get('net_rr'), 4)}</b>\n\n"
        "🧠 <b>Dati iniziali</b>\n"
        f"• Score: <b>{_number(payload.get('score'), 1)}/100</b>\n"
        f"• Probabilità: <b>{_percentage(payload.get('probability'))}</b>\n"
        f"• Chiusura: {_timestamp(payload.get('closed_at'))}\n"
        f"• Candela ambigua: <b>{'Sì' if payload.get('ambiguous') else 'No'}</b>\n\n"
        f"🔗 <i>{link_note}</i>"
    )


def format_report_message(payload: dict[str, Any]) -> str:
    raw = payload.get("message")
    if raw is None:
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    lines = [line.strip() for line in str(raw).splitlines() if line.strip()]
    body: list[str] = []
    for line in lines[1:] if lines and "report" in lines[0].lower() else lines:
        escaped = _escape(line)
        if ":" in escaped:
            label, value = escaped.split(":", 1)
            body.append(f"<b>{label}:</b>{value}")
        else:
            body.append(escaped)
    return "📊 <b>REPORT GIORNALIERO</b>\n\n" + "\n".join(body[:18])
