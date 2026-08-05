"""Fail-fast validation for the active Relative Strength runtime."""
from __future__ import annotations

from project.relative_strength_v3_scanner import (
    RUNTIME_VERSION_V3,
    STRATEGY_VERSION_V3,
    RelativeStrengthV3Scanner,
)
from project.relative_strength_v3_monitor import RelativeStrengthV3Monitor


EXPECTED_RUNTIME_VERSION = "RELATIVE_STRENGTH_BALANCED_1H_15M_V4"
EXPECTED_STRATEGY_VERSION = "RELATIVE_STRENGTH_BALANCED_V4"


def validate_v3_configuration(*, portfolio_budget: float, max_open: int, per_trade: float) -> None:
    """Validate the active balanced runtime and coherent portfolio allocation.

    The function name is kept for backward compatibility with the existing
    Railway entrypoint, but validation targets the currently active V4 runtime.
    """
    errors: list[str] = []
    if RUNTIME_VERSION_V3 != EXPECTED_RUNTIME_VERSION:
        errors.append(
            f"unexpected runtime version: {RUNTIME_VERSION_V3!r}; expected {EXPECTED_RUNTIME_VERSION!r}"
        )
    if STRATEGY_VERSION_V3 != EXPECTED_STRATEGY_VERSION:
        errors.append(
            f"unexpected strategy version: {STRATEGY_VERSION_V3!r}; expected {EXPECTED_STRATEGY_VERSION!r}"
        )
    if not isinstance(RelativeStrengthV3Scanner, type):
        errors.append("scanner class unavailable")
    if not isinstance(RelativeStrengthV3Monitor, type):
        errors.append("monitor class unavailable")
    if portfolio_budget <= 0 or max_open <= 0 or per_trade <= 0:
        errors.append("portfolio allocation must be positive")
    if per_trade * max_open > portfolio_budget + 1e-9:
        errors.append("maximum simultaneous exposure exceeds portfolio budget")
    if errors:
        raise RuntimeError("Relative Strength runtime validation failed: " + "; ".join(errors))
