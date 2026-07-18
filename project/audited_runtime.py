"""Runtime bridge that activates the audited V3.1 scanner without duplicating startup code."""
from __future__ import annotations

import project.simple_main as simple_main
from project.audited_long_scanner import AuditedLongStrategyScanner
from project.shared.logging import get_module_logger


# simple_main resolves this global when main() builds the scanner.
simple_main.EnhancedLongStrategyScanner = AuditedLongStrategyScanner


def main() -> None:
    logger = get_module_logger("system")
    logger.info("PROJECT AUDIT PATCH: 2026-07-18 LONG SCANNER V3.1")
    logger.info(
        "AUDITED_RUNTIME qualified_fallback=true max_open_positions=5 "
        "correlation_across_open_positions=true relative_strength_window=3d"
    )
    simple_main.main()


if __name__ == "__main__":
    main()
