"""Telegram wrappers that expose train and out-of-sample research evidence."""
from __future__ import annotations

from typing import Any, Callable

from project.notification_engine.formatters import (
    format_signal_message as base_signal_message,
    format_watchlist_message as base_watchlist_message,
)


def _float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _oos_section(payload: dict[str, Any]) -> str:
    breakdown = payload.get("score_breakdown")
    if not isinstance(breakdown, dict) or _float(breakdown.get("robust_research_flag")) < 0.5:
        return ""
    train_trades = int(_float(breakdown.get("research_train_trades_raw")))
    test_trades = int(_float(breakdown.get("research_test_trades_raw")))
    train_pf = _float(breakdown.get("research_train_pf_raw"))
    test_pf = _float(breakdown.get("research_test_pf_raw"))
    train_ev = _float(breakdown.get("research_train_expectancy_raw"))
    test_ev = _float(breakdown.get("research_test_expectancy_raw"))
    return (
        "🧪 <b>Validazione walk-forward</b>\n"
        f"• Sviluppo: <b>{train_trades} trade</b> · PF <b>{train_pf:.3f}</b> · EV <b>€{train_ev:.4f}</b>\n"
        f"• Fuori campione: <b>{test_trades} trade</b> · PF <b>{test_pf:.3f}</b> · EV <b>€{test_ev:.4f}</b>\n"
        "• Criterio: <b>edge positivo in entrambe le finestre</b>"
    )


def _insert_before(message: str, marker: str, section: str) -> str:
    if not section or marker not in message:
        return message
    return message.replace(marker, f"{section}\n\n{marker}", 1)


def format_robust_signal_message(payload: dict[str, Any]) -> str:
    return _insert_before(
        base_signal_message(payload),
        "📌 <b>Motivi principali</b>",
        _oos_section(payload),
    )


def format_robust_watchlist_message(payload: dict[str, Any]) -> str:
    return _insert_before(
        base_watchlist_message(payload),
        "📐 <b>Confidenza statistica</b>",
        _oos_section(payload),
    )
