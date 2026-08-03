"""Production entrypoint for two isolated relative-strength PAPER engines.

Public engine: frequent LONG/SHORT signals for the Telegram channel.
Private engine: one spot LONG decision at 06:00 Europe/Rome, max 12 hours.
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
