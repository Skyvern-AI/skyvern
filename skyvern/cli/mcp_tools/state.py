"""MCP tools for browser auth state persistence (save/load).

Save and restore cookies, localStorage, and sessionStorage across sessions.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlparse

import structlog
from pydantic import Field

from ._common import ErrorCode, Timer, make_error, make_result
from ._session import BrowserNotAvailableError, get_current_session, get_page, no_browser_error

LOG = structlog.get_logger(__name__)


def _validate_state_path(file_path: str, *, must_exist: bool = False) -> Path:
    """Validate and resolve state file path. Prevents path traversal.

    Restricts paths to the current working directory or ~/.skyvern/.
    Rejects symlinks to prevent TOCTOU attacks.
    """
    raw = Path(file_path)
    if raw.is_symlink():
        raise ValueError(f"Symlinks not allowed for state files: {raw}")
    resolved = raw.resolve()
    allowed_roots = [Path.cwd().resolve(), (Path.home() / ".skyvern").resolve()]
    if not any(resolved == root or str(resolved).startswith(str(root) + "/") for root in allowed_roots):
        raise ValueError(f"State file must be under working directory or ~/.skyvern/: {resolved}")
    if must_exist and not resolved.exists():
        raise FileNotFoundError(f"State file not found: {resolved}")
    if resolved.suffix not in (".json", ""):
        raise ValueError(f"State file must have .json extension or no extension: {resolved}")
    return resolved


async def skyvern_state_save(
    file_path: Annotated[
        str,
        Field(description="Path to save state file (JSON). Must be under cwd or ~/.skyvern/."),
    ],
    session_id: Annotated[str | None, Field(description="Browser session ID (pbs_...).")] = None,
    cdp_url: Annotated[str | None, Field(description="CDP WebSocket URL.")] = None,
) -> dict[str, Any]:
    """Save browser auth state (cookies + localStorage + sessionStorage) to a JSON file for later restore via state_load."""
    try:
        page, ctx = await get_page(session_id=session_id, cdp_url=cdp_url)
    except BrowserNotAvailableError:
        return make_result("state_save", ok=False, error=no_browser_error())

    with Timer() as timer:
        try:
            resolved = _validate_state_path(file_path)
            resolved.parent.mkdir(parents=True, exist_ok=True)

            session = get_current_session()
            browser = session.browser
            if browser is None:
                return make_result(
                    "state_save",
                    ok=False,
                    browser_context=ctx,
                    error=make_error(ErrorCode.NO_ACTIVE_BROWSER, "No browser available", "Create a session first"),
                )

            from skyvern.cli.core.browser_ops import do_state_save

            result = await do_state_save(page.page, browser, resolved)
            timer.mark("sdk")

            return make_result(
                "state_save",
                browser_context=ctx,
                data={
                    "file_path": result.file_path,
                    "cookie_count": result.cookie_count,
                    "local_storage_count": result.local_storage_count,
                    "session_storage_count": result.session_storage_count,
                    "url": result.url,
                },
                timing_ms=timer.timing_ms,
            )
        except (ValueError, OSError) as e:
            return make_result(
                "state_save",
                ok=False,
                browser_context=ctx,
                timing_ms=timer.timing_ms,
                error=make_error(ErrorCode.ACTION_FAILED, str(e), "Check file path is valid and writable"),
            )
        except Exception as e:
            LOG.exception("state_save failed", error=str(e))
            return make_result(
                "state_save",
                ok=False,
                browser_context=ctx,
                timing_ms=timer.timing_ms,
                error=make_error(ErrorCode.ACTION_FAILED, str(e), "Unexpected error during state save"),
            )


async def skyvern_state_load(
    file_path: Annotated[
        str,
        Field(description="Path to state file (JSON) previously created by state_save."),
    ],
    session_id: Annotated[str | None, Field(description="Browser session ID (pbs_...).")] = None,
    cdp_url: Annotated[str | None, Field(description="CDP WebSocket URL.")] = None,
) -> dict[str, Any]:
    """Restore browser auth state from a JSON file. Navigate to the target site BEFORE loading so cookie domain filtering works."""
    try:
        page, ctx = await get_page(session_id=session_id, cdp_url=cdp_url)
    except BrowserNotAvailableError:
        return make_result("state_load", ok=False, error=no_browser_error())

    with Timer() as timer:
        try:
            resolved = _validate_state_path(file_path, must_exist=True)

            session = get_current_session()
            browser = session.browser
            if browser is None:
                return make_result(
                    "state_load",
                    ok=False,
                    browser_context=ctx,
                    error=make_error(ErrorCode.NO_ACTIVE_BROWSER, "No browser available", "Create a session first"),
                )

            from skyvern.cli.core.browser_ops import do_state_load

            current_domain = urlparse(page.page.url).hostname or ""
            result = await do_state_load(page.page, browser, resolved, current_domain)
            timer.mark("sdk")

            return make_result(
                "state_load",
                browser_context=ctx,
                data={
                    "cookie_count": result.cookie_count,
                    "local_storage_count": result.local_storage_count,
                    "session_storage_count": result.session_storage_count,
                    "source_url": result.source_url,
                    "skipped_cookies": result.skipped_cookies,
                },
                timing_ms=timer.timing_ms,
            )
        except (ValueError, FileNotFoundError, json.JSONDecodeError) as e:
            return make_result(
                "state_load",
                ok=False,
                browser_context=ctx,
                timing_ms=timer.timing_ms,
                error=make_error(ErrorCode.ACTION_FAILED, str(e), "Check file path and file format"),
            )
        except Exception as e:
            LOG.exception("state_load failed", error=str(e))
            return make_result(
                "state_load",
                ok=False,
                browser_context=ctx,
                timing_ms=timer.timing_ms,
                error=make_error(ErrorCode.ACTION_FAILED, str(e), "Unexpected error during state load"),
            )
