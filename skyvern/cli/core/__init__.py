"""Shared core layer for Skyvern CLI and MCP tools.

This package provides reusable primitives that both MCP tools and CLI commands
import from, preventing logic duplication across interfaces.
"""

from .artifacts import get_artifact_dir, save_artifact
from .client import get_skyvern
from .result import Artifact, BrowserContext, ErrorCode, Timer, make_error, make_result
from .session_manager import (
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
    # client.py
    "get_skyvern",
    # result.py
    "Artifact",
    "BrowserContext",
    "ErrorCode",
    "Timer",
    "make_error",
    "make_result",
    # artifacts.py
    "get_artifact_dir",
    "save_artifact",
    # session_manager.py
    "BrowserNotAvailableError",
    "SessionState",
    "browser_session",
    "get_current_session",
    "get_page",
    "no_browser_error",
    "resolve_browser",
    "set_current_session",
]
