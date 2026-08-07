"""Velez 15m entry scanner with protected-trend Mode 2 lifecycle metadata.

Entry qualification is delegated unchanged to the clean EMA20/EMA200 scanner.
The lifecycle has no fixed take profit: at +1.5R the full position remains open
and the protective stop moves to +1R. The trade then runs until that protected
stop, a closed 15m EMA20 exit, or the 12 hour limit.
"""
from __future__ import annotations

from dataclasses import replace
from typing import Any

import project.velez_15m_scanner as base
from project.strategy_engine.service import GeneratedSignal

RUNTIME_VERSION = "VELEZ_15M_EMA20_EMA200_MODE2_PROTECT_V4"
STRATEGY_NAME = "Oliver Velez EMA20 EMA200 15m Mode 2 Protected"


class VelezMode2Scanner(base.Velez15mScanner):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        base.RUNTIME_VERSION = RUNTIME_VERSION
        kwargs["max_hold_candles"] = 48
        super().__init__(*args, **kwargs)

    def evaluate_pair(self, pair: str) -> tuple[GeneratedSignal | None, str]:
        candidate, reason = super().evaluate_pair(pair)
        if candidate is None:
            return None, reason

        state = dict(candidate.score_breakdown)
        entry = float(candidate.entry)
        initial_stop = float(candidate.stop_loss)
        direction = str(state.get("direction", "LONG"))
        risk = abs(entry - initial_stop)
        protection_trigger = entry + 1.5 * risk if direction == "LONG" else entry - 1.5 * risk
        protected_stop = entry + 1.0 * risk if direction == "LONG" else entry - 1.0 * risk

        state.update(
            {
                "runtime_version": RUNTIME_VERSION,
                "strategy_version": "VELEZ_MODE2_PROTECT_V4",
                "management_mode": "PROTECT_AT_1_5R_TO_1R_THEN_EMA20_OR_STOP_OR_12H",
                "max_hold_candles": 48,
                "fixed_target_active": False,
                "stored_target_accounting_only": True,
                "profit_protection_trigger_r": 1.5,
                "profit_protection_stop_r": 1.0,
                "profit_protection_trigger_price": round(protection_trigger, 10),
                "profit_protected_stop_price": round(protected_stop, 10),
                "profit_protection_armed": False,
                "breakeven_active": False,
                "trailing_active": False,
            }
        )
        reasons = list(candidate.reasons)
        reasons.append(
            "gestione: a +1,5R stop a +1R; poi stop protetto, chiusura contro EMA20 o massimo 12 ore"
        )
        return replace(
            candidate,
            strategy=STRATEGY_NAME,
            validation_status="VELEZ_MODE2_PROTECTED_RULES_PASSED",
            validation_reason="; ".join(reasons),
            reasons=reasons,
            score_breakdown=state,
        ), reason
