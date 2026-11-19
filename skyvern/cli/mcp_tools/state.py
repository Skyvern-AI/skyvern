"""MCP tools for browser auth state persistence (save/load).

Save and restore cookies, localStorage, and sessionStorage across sessions.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlparse

import structlog
from pydantic import Field

from skyvern.forge import app

from ._common import ErrorCode, Timer, make_error, make_result
from ._session import BrowserNotAvailableError, get_current_session, get_page, no_browser_error

LOG = structlog.get_logger(__name__)

_MCP_STATE_NAMESPACE_DIR = ".mcp-state"


def _state_organization_id() -> str | None:
    organization_id = app.AGENT_FUNCTION.get_mcp_request_organization_id()
    if organization_id is not None:
        return organization_id
    try:
        # fastmcp is a server extra, and `skyvern browser` commands import this module without it.
        from fastmcp.server.dependencies import get_http_request  # noqa: PLC0415

        request = get_http_request()
    except (ImportError, RuntimeError):
        return None
    # A standalone HTTP server's API-key middleware records the caller's organization on the request.
    organization_id = getattr(request.state, "organization_id", None)
    if isinstance(organization_id, str) and organization_id:
        return organization_id
    raise ValueError(
        "State files are not available on this HTTP MCP server: it has no organization to keep each caller's files "
        "apart. Use a local stdio server for state files."
    )


_LEGACY_STATE_MAX_BYTES = 50 * 1024 * 1024


def _is_saved_state(path: Path) -> bool:
    try:
        # A caller names this file, so a large one is never read whole just to word an error.
        if not path.is_file() or path.stat().st_size > _LEGACY_STATE_MAX_BYTES:
            return False
        payload = json.loads(path.read_text())
    except (OSError, ValueError):
        return False
    return isinstance(payload, dict) and "cookies" in payload


def _validate_state_path(
    file_path: str,
    *,
    must_exist: bool = False,
    organization_id: str | None = None,
) -> Path:
    """Validate and resolve state file path. Prevents path traversal.

    Restricts paths to the current working directory or ~/.skyvern/.
    Rejects symlinks to prevent TOCTOU attacks.
    """
    raw = Path(file_path)
    if raw.is_symlink():
        raise ValueError(f"Symlinks not allowed for state files: {raw}")
    resolved = raw.resolve()
    allowed_roots = [Path.cwd().resolve(), (Path.home() / ".skyvern").resolve()]
    matching_roots = [root for root in allowed_roots if resolved == root or resolved.is_relative_to(root)]
    if not matching_roots:
        raise ValueError(f"State file must be under working directory or ~/.skyvern/: {resolved}")

    if organization_id is not None:
        allowed_root = max(matching_roots, key=lambda root: len(root.parts))
        namespace_parent = (allowed_root / _MCP_STATE_NAMESPACE_DIR).resolve()
        namespace_id = hashlib.sha256(organization_id.encode()).hexdigest()
        namespace_root = namespace_parent / namespace_id

        if resolved == namespace_parent or resolved.is_relative_to(namespace_parent):
            if resolved != namespace_root and not resolved.is_relative_to(namespace_root):
                raise ValueError("State file is outside the authenticated organization namespace")
        else:
            namespaced = namespace_root / resolved.relative_to(allowed_root)
            if namespaced.is_symlink():
                raise ValueError(f"Symlinks not allowed for state files: {namespaced}")
            resolved = namespaced.resolve()
            if resolved != namespace_root and not resolved.is_relative_to(namespace_root):
                raise ValueError("State file is outside the authenticated organization namespace")

    if resolved.suffix not in (".json", ""):
        raise ValueError(f"State file must have .json extension or no extension: {resolved}")
    if must_exist and not resolved.exists():
        legacy = raw.resolve()
        # A file saved before per-organization folders has no owner on record, so only the operator may claim it.
        # Only a real state file is named, so the message reveals no other file under the working directory.
        if legacy != resolved and _is_saved_state(legacy):
            raise FileNotFoundError(
                f"State file not found: {resolved}. {legacy} was saved before this server kept each organization's "
                f"state files apart; to use it with this API key's organization, the server's operator moves it to "
                f"{resolved}."
            )
        raise FileNotFoundError(f"State file not found: {resolved}")
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
    except BrowserNotAvailableError as exc:
        return make_result("state_save", ok=False, error=no_browser_error(exc))

    with Timer() as timer:
        try:
            resolved = _validate_state_path(file_path, organization_id=_state_organization_id())
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
                error=make_error(ErrorCode.ACTION_FAILED, str(e), "Check file path is valid and writable", exc=e),
            )
        except Exception as e:
            LOG.exception("state_save failed", error=str(e))
            return make_result(
                "state_save",
                ok=False,
                browser_context=ctx,
                timing_ms=timer.timing_ms,
                error=make_error(ErrorCode.ACTION_FAILED, str(e), "Unexpected error during state save", exc=e),
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
    except BrowserNotAvailableError as exc:
        return make_result("state_load", ok=False, error=no_browser_error(exc))

    with Timer() as timer:
        try:
            resolved = _validate_state_path(file_path, must_exist=True, organization_id=_state_organization_id())

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
                error=make_error(ErrorCode.ACTION_FAILED, str(e), "Check file path and file format", exc=e),
            )
        except Exception as e:
            LOG.exception("state_load failed", error=str(e))
            return make_result(
                "state_load",
                ok=False,
                browser_context=ctx,
                timing_ms=timer.timing_ms,
                error=make_error(ErrorCode.ACTION_FAILED, str(e), "Unexpected error during state load", exc=e),
            )
