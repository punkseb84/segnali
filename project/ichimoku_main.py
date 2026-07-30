"""Retired Railway entrypoint for the legacy Ichimoku scanner.

The active runtime is TRIX + ADX and is exposed by ``project.main``.  This module
is intentionally kept as a fail-closed compatibility tombstone so an obsolete
Railway start command cannot generate new Ichimoku signals or Telegram alerts.
Legacy open positions are still managed by ``LegacyIchimokuOutcomeMonitor``
inside the active TRIX runtime.
"""
from __future__ import annotations


RETIREMENT_MESSAGE = (
    "Legacy Ichimoku runtime is disabled. "
    "Start Railway with: python -m project.main"
)


def main() -> None:
    """Refuse to start the retired scanner before any database or Telegram work."""
    raise SystemExit(RETIREMENT_MESSAGE)


if __name__ == "__main__":
    main()
