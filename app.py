"""Single Railway entrypoint for the relative-strength PAPER runtime.

Railway may start either ``python app.py`` or ``python -m project.main``; both
execute the same relative-strength strategy against BTC and never start TRIX or
Ichimoku.
"""
from __future__ import annotations

from project.main import main


if __name__ == "__main__":
    main()
