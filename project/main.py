"""Primary Railway entrypoint for the complete relative-strength V2 PAPER runtime."""
from __future__ import annotations

from project.relative_strength_runtime_v2 import main
from project.simple_main import RAILWAY_LIGHT_MISSING_DATABASE_URL, build_postgres

__all__ = [
    "RAILWAY_LIGHT_MISSING_DATABASE_URL",
    "build_postgres",
    "main",
]


if __name__ == "__main__":
    main()
