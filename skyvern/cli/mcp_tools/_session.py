"""Backward-compatible re-exports from skyvern.cli.core.

MCP tools import from here; the canonical implementations live in core/.
"""

from __future__ import annotations

from skyvern.cli.core.client import get_skyvern
from skyvern.cli.core.session_manager import (
    BrowserNotAvailableError,
    SessionState,
    browser_session,
    clear_session_ref_map,
    close_current_session,
    get_current_session,
    get_page,
    get_session_ref,
    no_browser_error,
    page_ref_key,
    replace_session_ref_map,
    resolve_browser,
    session_ref_generation,
    set_current_session,
)

__all__ = [
    "BrowserNotAvailableError",
    "SessionState",
    "browser_session",
    "clear_session_ref_map",
    "close_current_session",
    "get_current_session",
    "get_page",
    "get_session_ref",
    "get_skyvern",
    "no_browser_error",
    "page_ref_key",
    "replace_session_ref_map",
    "resolve_browser",
    "session_ref_generation",
    "set_current_session",
]
