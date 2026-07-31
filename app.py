"""Single Railway entrypoint for the TRIX + ADX PAPER runtime.

All legacy live, research, Ichimoku and classic-strategy branches have been
removed from the process entrypoint.  Railway may start either ``python app.py``
or ``python -m project.main``; both now execute exactly the same TRIX PAPER
runtime.
"""
from __future__ import annotations

from project.main import main


if __name__ == "__main__":
    main()
