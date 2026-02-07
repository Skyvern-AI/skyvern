"""Backward-compatible re-exports from skyvern.cli.core.

MCP tools import from here; the canonical implementations live in core/.
"""

from __future__ import annotations

from skyvern.cli.core.client import get_skyvern
from skyvern.cli.core.session_manager import (
    BrowserNotAvailableError,
    SessionState,
    browser_session,
    get_current_session,
    get_page,
    no_browser_error,
    resolve_browser,
    set_current_session,
)

__all__ = [
    "BrowserNotAvailableError",
    "SessionState",
    "browser_session",
    "get_current_session",
    "get_page",
    "get_skyvern",
    "no_browser_error",
    "resolve_browser",
    "set_current_session",
]
