"""Stable Railway entrypoint for the pure price-action paper scanner."""
from __future__ import annotations

import os

# Live-friendly defaults are applied here so Railway starts the already-tested
# runtime without importing an additional wrapper module.
os.environ.setdefault("PRICE_ACTION_MAX_PER_DAY", "6")
os.environ.setdefault("PRICE_ACTION_MIN_INTERVAL_MINUTES", "30")
os.environ.setdefault("PRICE_ACTION_PAIR_COOLDOWN_MINUTES", "120")
os.environ.setdefault("PRICE_ACTION_MIN_SCORE", "68")
os.environ.setdefault("PRICE_ACTION_MIN_NET_RR", "1.05")
os.environ.setdefault("PRICE_ACTION_MIN_NET_PROFIT_EUR", "0.05")
os.environ.setdefault("PRICE_ACTION_RETEST_WINDOW_15M", "12")

from project.price_action_main import main


if __name__ == "__main__":
    main()
