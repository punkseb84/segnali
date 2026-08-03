"""Production entrypoint for Relative Strength V2.

Uses the existing runtime wiring while replacing its scanner with the corrected
market-wide ranking implementation.
"""
from __future__ import annotations

import project.relative_strength_main as runtime
from project.relative_strength_v2_scanner import RelativeStrengthV2Scanner


def main() -> None:
    runtime.RelativeStrengthScanner = RelativeStrengthV2Scanner
    runtime.main()


if __name__ == "__main__":
    main()
