"""Compatibility entrypoint redirected to the TRIX + ADX PAPER runtime.

An old Railway start command may still reference ``project.ichimoku_main``.
Redirecting here prevents both legacy Ichimoku execution and a stopped worker:
every supported start command now launches the same TRIX PAPER process.
"""
from __future__ import annotations

from project.trix_adx_main import main


if __name__ == "__main__":
    main()
