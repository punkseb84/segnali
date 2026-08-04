"""Fail-fast validation for the corrected Relative Strength V3 runtime."""
from __future__ import annotations

from project.relative_strength_v3_scanner import (
    RUNTIME_VERSION_V3,
    STRATEGY_VERSION_V3,
    RelativeStrengthV3Scanner,
)
from project.relative_strength_v3_monitor import RelativeStrengthV3Monitor


def validate_v3_configuration(*, portfolio_budget: float, max_open: int, per_trade: float) -> None:
    errors: list[str] = []
    if not RUNTIME_VERSION_V3.endswith("V3"):
        errors.append("runtime version is not V3")
    if STRATEGY_VERSION_V3 != "RELATIVE_STRENGTH_PULLBACK_V3":
        errors.append("unexpected strategy version")
    if not issubclass(RelativeStrengthV3Scanner, object):
        errors.append("scanner class unavailable")
    if not issubclass(RelativeStrengthV3Monitor, object):
        errors.append("monitor class unavailable")
    if portfolio_budget <= 0 or max_open <= 0 or per_trade <= 0:
        errors.append("portfolio allocation must be positive")
    if per_trade * max_open > portfolio_budget + 1e-9:
        errors.append("maximum simultaneous exposure exceeds portfolio budget")
    if errors:
        raise RuntimeError("Relative Strength V3 validation failed: " + "; ".join(errors))
