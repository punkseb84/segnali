"""Final V5.1.3 Railway entrypoint wiring."""
from __future__ import annotations

import project.validated_main as validated_main
from project.validated_report_scanner import ValidatedReportStrategyScanner

validated_main.ValidatedLiveStrategyScannerV513 = ValidatedReportStrategyScanner
main = validated_main.main

__all__ = ["main"]
