"""Compatibility aliases: the only active scanner is Donchian."""
from project.donchian_scanner import (
    DonchianBreakoutScanner,
    RUNTIME_VERSION,
    STRATEGY_NAME,
)

PurePriceActionScanner = DonchianBreakoutScanner
PRICE_ACTION_RUNTIME_VERSION = RUNTIME_VERSION
TRENDLINE_RETEST = STRATEGY_NAME
HORIZONTAL_RETEST = STRATEGY_NAME
PRICE_ACTION_STRATEGIES = (STRATEGY_NAME,)

__all__ = [
    "PurePriceActionScanner",
    "PRICE_ACTION_RUNTIME_VERSION",
    "TRENDLINE_RETEST",
    "HORIZONTAL_RETEST",
    "PRICE_ACTION_STRATEGIES",
]
