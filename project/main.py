"""Primary Railway entrypoint for the single Ichimoku strategy.

Compatibility exports are kept here because startup tests and older callers import
PostgreSQL helpers from ``project.main``.
"""
from __future__ import annotations

from project.ichimoku_main import main
from project.simple_main import RAILWAY_LIGHT_MISSING_DATABASE_URL, build_postgres

__all__ = [
    "RAILWAY_LIGHT_MISSING_DATABASE_URL",
    "build_postgres",
    "main",
]


if __name__ == "__main__":
    main()
