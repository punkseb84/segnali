"""Compatibility alias: the only active scanner is Donchian."""
from project.donchian_scanner import DonchianBreakoutScanner

LivePurePriceActionScanner = DonchianBreakoutScanner

__all__ = ["LivePurePriceActionScanner"]
