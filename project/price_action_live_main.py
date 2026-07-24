"""Railway entrypoint with live-friendly pure price-action confirmations."""
from __future__ import annotations

import os

import project.price_action_main as base_runtime
from project.price_action_scanner_livefix import LivePurePriceActionScanner


def main() -> None:
    # Keep the strategy pure price action, but use defaults suitable for a live
    # 15m scanner rather than an overly restrictive research detector.
    os.environ.setdefault("PRICE_ACTION_MAX_PER_DAY", "6")
    os.environ.setdefault("PRICE_ACTION_MIN_INTERVAL_MINUTES", "30")
    os.environ.setdefault("PRICE_ACTION_PAIR_COOLDOWN_MINUTES", "120")
    os.environ.setdefault("PRICE_ACTION_MIN_SCORE", "68")
    os.environ.setdefault("PRICE_ACTION_MIN_NET_RR", "1.05")
    os.environ.setdefault("PRICE_ACTION_MIN_NET_PROFIT_EUR", "0.05")
    os.environ.setdefault("PRICE_ACTION_MIN_TRENDLINE_TOUCHES", "2")
    os.environ.setdefault("PRICE_ACTION_RETEST_WINDOW_15M", "12")

    # price_action_main builds the scanner through this module-level symbol.
    base_runtime.PurePriceActionScanner = LivePurePriceActionScanner
    base_runtime.main()


if __name__ == "__main__":
    main()
