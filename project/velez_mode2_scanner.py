"""Velez 15m entry scanner with intraday Mode 2 lifecycle metadata.

Entry qualification is delegated unchanged to the clean EMA20/EMA200 V2 scanner.
This wrapper gives the new lifecycle its own runtime identity and explicitly marks
that the stored 1.5R target is accounting-only: it is NOT an exit instruction.
"""
from __future__ import annotations

from dataclasses import replace
from typing import Any

import project.velez_15m_scanner as base
from project.strategy_engine.service import GeneratedSignal

RUNTIME_VERSION = "VELEZ_15M_EMA20_EMA200_MODE2_V3"
STRATEGY_NAME = "Oliver Velez EMA20 EMA200 15m Mode 2"


class VelezMode2Scanner(base.Velez15mScanner):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        # All inherited persistence/counting methods read this module global at runtime.
        base.RUNTIME_VERSION = RUNTIME_VERSION
        kwargs["max_hold_candles"] = 48
        super().__init__(*args, **kwargs)

    def evaluate_pair(self, pair: str) -> tuple[GeneratedSignal | None, str]:
        candidate, reason = super().evaluate_pair(pair)
        if candidate is None:
            return None, reason
        state = dict(candidate.score_breakdown)
        state.update(
            {
                "runtime_version": RUNTIME_VERSION,
                "strategy_version": "VELEZ_MODE2_V3",
                "management_mode": "EMA20_EXIT_OR_HARD_STOP_OR_12H",
                "max_hold_candles": 48,
                "fixed_target_active": False,
                "stored_target_accounting_only": True,
                "breakeven_active": False,
                "trailing_active": False,
            }
        )
        reasons = list(candidate.reasons)
        reasons.append("gestione: stop strutturale, chiusura contro EMA20 o massimo 12 ore")
        return replace(
            candidate,
            strategy=STRATEGY_NAME,
            validation_status="VELEZ_MODE2_RULES_PASSED",
            validation_reason="; ".join(reasons),
            reasons=reasons,
            score_breakdown=state,
        ), reason
