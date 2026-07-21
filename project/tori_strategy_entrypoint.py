"""Final Railway wiring for the hardened eighth LONG strategy."""
from __future__ import annotations

import project.reliable_harmonic_scanner as reliable_module
import project.report_safe_harmonic_scanner as report_safe_module
import project.structure_aware_main as structure_main
import project.validated_live_scanner as validated_module
import project.validated_live_scanner_v513 as validated_v513_module
import project.version_scoped_harmonic_scanner as version_scoped_module
from project.tori_trendline_scanner import EIGHT_LONG_STRATEGIES
from project.tori_trendline_v11 import ToriTrendlineDailyPaperScannerV11

# These inherited layers imported the old seven-strategy tuple by value. Update only the
# Railway V6.3 entrypoint so expiration, version-scoped statistics and reports also include
# Three-Touch Trendline Break. Legacy modules remain unchanged when tested or imported alone.
for module in (
    reliable_module,
    report_safe_module,
    version_scoped_module,
    validated_module,
    validated_v513_module,
):
    module.LIVE_LONG_STRATEGIES = EIGHT_LONG_STRATEGIES

structure_main.ToriTrendlineDailyPaperScanner = ToriTrendlineDailyPaperScannerV11
main = structure_main.main

__all__ = ["main"]
