"""Skyvern HTTP API client accessor.

Workflow tools import from here to get the API client without pulling in
browser/Playwright dependencies.
"""

from __future__ import annotations

from skyvern.cli.core.client import get_skyvern

__all__ = ["get_skyvern"]
