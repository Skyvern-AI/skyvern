"""Shared core layer for Skyvern CLI and MCP tools.

This package provides reusable primitives that both MCP tools and CLI commands
import from, preventing logic duplication across interfaces.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

_LAZY_EXPORTS = {
    # client.py
    "get_skyvern": ("skyvern.cli.core.client", "get_skyvern"),
    # result.py
    "Artifact": ("skyvern.cli.core.result", "Artifact"),
    "BrowserContext": ("skyvern.cli.core.result", "BrowserContext"),
    "ErrorCode": ("skyvern.cli.core.result", "ErrorCode"),
    "Timer": ("skyvern.cli.core.result", "Timer"),
    "make_error": ("skyvern.cli.core.result", "make_error"),
    "make_result": ("skyvern.cli.core.result", "make_result"),
    # artifacts.py
    "get_artifact_dir": ("skyvern.cli.core.artifacts", "get_artifact_dir"),
    "save_artifact": ("skyvern.cli.core.artifacts", "save_artifact"),
    # session_manager.py
    "BrowserNotAvailableError": ("skyvern.cli.core.session_manager", "BrowserNotAvailableError"),
    "SessionState": ("skyvern.cli.core.session_manager", "SessionState"),
    "browser_session": ("skyvern.cli.core.session_manager", "browser_session"),
    "get_current_session": ("skyvern.cli.core.session_manager", "get_current_session"),
    "get_page": ("skyvern.cli.core.session_manager", "get_page"),
    "no_browser_error": ("skyvern.cli.core.session_manager", "no_browser_error"),
    "resolve_browser": ("skyvern.cli.core.session_manager", "resolve_browser"),
    "set_current_session": ("skyvern.cli.core.session_manager", "set_current_session"),
}

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


def __getattr__(name: str) -> Any:
    if name not in _LAZY_EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    module_path, attr_name = _LAZY_EXPORTS[name]
    value = getattr(import_module(module_path), attr_name)
    globals()[name] = value
    return value
