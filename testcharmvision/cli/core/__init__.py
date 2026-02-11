"""Shared core layer for Testcharmvision CLI.

This package provides reusable primitives for CLI commands.
"""

from .artifacts import get_artifact_dir, save_artifact
from .client import get_testcharmvision
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
    "get_testcharmvision",
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
