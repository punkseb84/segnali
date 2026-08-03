"""Compatibility entrypoint for the active relative-strength PAPER runtime.

Railway previously started this module directly. It now delegates exclusively to
``project.relative_strength_main`` so no obsolete Ichimoku or TRIX runtime can
be started through the legacy command.
"""
from __future__ import annotations

from project.relative_strength_main import main


if __name__ == "__main__":
    main()
