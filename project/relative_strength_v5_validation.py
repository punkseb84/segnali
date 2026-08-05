"""Fail-fast validation for the robust-volume Relative Strength V5 runtime."""
from __future__ import annotations

from project.relative_strength_v3_monitor import RelativeStrengthV3Monitor
from project.relative_strength_v5_scanner import (
    RUNTIME_VERSION_V5,
    STRATEGY_VERSION_V5,
    RelativeStrengthV5Scanner,
)


def validate_v5_configuration(
    *,
    portfolio_budget: float,
    max_open: int,
    per_trade: float,
) -> None:
    errors: list[str] = []
    if RUNTIME_VERSION_V5 != "RELATIVE_STRENGTH_BALANCED_ROBUST_VOLUME_1H_15M_V5":
        errors.append("unexpected V5 runtime version")
    if STRATEGY_VERSION_V5 != "RELATIVE_STRENGTH_BALANCED_ROBUST_VOLUME_V5":
        errors.append("unexpected V5 strategy version")
    if not issubclass(RelativeStrengthV5Scanner, object):
        errors.append("V5 scanner class unavailable")
    if not issubclass(RelativeStrengthV3Monitor, object):
        errors.append("position monitor class unavailable")
    if portfolio_budget <= 0 or max_open <= 0 or per_trade <= 0:
        errors.append("portfolio allocation must be positive")
    if per_trade * max_open > portfolio_budget + 1e-9:
        errors.append("maximum simultaneous exposure exceeds portfolio budget")
    if errors:
        raise RuntimeError("Relative Strength V5 validation failed: " + "; ".join(errors))
