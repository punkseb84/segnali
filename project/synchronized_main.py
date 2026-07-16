"""Railway entrypoint with synchronized data and signal lifecycle integrity."""
from __future__ import annotations

import project.main as modular_main
from project.synchronized_scheduler import SynchronizedPlatformScheduler
from project.strategy_engine.operational_integrity import OperationalIntegrityStrategyEngine


# The original builders intentionally remain reusable. Replacing their module globals
# upgrades the runtime without duplicating the large startup/bootstrap implementation.
modular_main.PlatformScheduler = SynchronizedPlatformScheduler
modular_main.ProgressiveOperationalStrategyEngine = OperationalIntegrityStrategyEngine


if __name__ == "__main__":
    modular_main.main()
