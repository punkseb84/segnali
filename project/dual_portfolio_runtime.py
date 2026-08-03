"""Railway entrypoint for two isolated PAPER engines.

Public engine: relative-strength Telegram signals.
Private engine: one spot LONG decision at 06:00 Europe/Rome, EUR 10 budget,
maximum holding time 12 hours, with net costs and explicit USDT exit alerts.
"""
from __future__ import annotations

import project.relative_strength_main as runtime
from project.dual_portfolio_scheduler import DualPortfolioScheduler
from project.relative_strength_v2_scanner import RelativeStrengthV2Scanner


def main() -> None:
    runtime.RelativeStrengthScanner = RelativeStrengthV2Scanner
    runtime.RelativeStrengthScheduler = DualPortfolioScheduler
    runtime.main()


if __name__ == "__main__":
    main()
