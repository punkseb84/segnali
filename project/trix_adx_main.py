"""Retired compatibility entrypoint.

TRIX has been removed from the active application. Any obsolete Railway command
that still starts this module is redirected to the relative-strength PAPER runtime.
"""
from __future__ import annotations

from project.relative_strength_main import main


if __name__ == "__main__":
    main()
