from __future__ import annotations

import os
from typing import Annotated, Any

from pydantic import Field

from skyvern.cli.core.api_key_hash import hash_api_key_for_cache
from skyvern.cli.core.client import get_active_api_key
from skyvern.cli.core.session_manager import is_stateless_http_mode
from skyvern.cli.core.session_ops import coerce_proxy_location, do_session_close, do_session_create, do_session_list
from skyvern.schemas.runs import proxy_location_to_request

from ._common import BrowserContext, ErrorCode, Timer, make_error, make_result
from ._session import (
    SessionState,
    get_current_session,
    get_skyvern,
    resolve_browser,
    set_current_session,
)


def _session_api_key_hash() -> str | None:
    api_key = get_active_api_key()
    if not api_key:
        return None
    return hash_api_key_for_cache(api_key)


def _should_default_to_cdp() -> tuple[bool, str | None]:
    """Check if BROWSER_TYPE is cdp-connect and return the configured debugging URL."""
    browser_type = os.environ.get("BROWSER_TYPE", "")
    if browser_type == "cdp-connect":
        cdp_url = os.environ.get("BROWSER_REMOTE_DEBUGGING_URL", "http://127.0.0.1:9222")
        return True, cdp_url
    return False, None


async def skyvern_browser_session_create(
    timeout: Annotated[int | None, Field(description="Session timeout in minutes (5-1440)")] = 60,
    proxy_location: Annotated[
        str | dict[str, Any] | None,
        Field(
            description=(
                "Proxy location as a legacy enum string like RESIDENTIAL, or a GeoTarget object like "
                '{"country":"US","subdivision":"CA","city":"San Francisco"}.'
            )
        ),
    ] = None,
    local: Annotated[bool, Field(description="Launch local browser instead of cloud")] = False,
    headless: Annotated[bool, Field(description="Run local browser in headless mode")] = False,
) -> dict[str, Any]:
    """Create a new browser session to start interacting with websites. Creates a cloud-hosted browser by default with geographic proxy support. This must be called before using any browser tools (navigate, click, extract, etc.).

    Use local=true for a local Chromium instance.
    The session persists across tool calls until explicitly closed.
    """
    # When BROWSER_TYPE=cdp-connect, auto-connect to the user's local browser via CDP.
    # resolve_browser() stores the browser in session state via set_current_session()
    # internally, so we don't need to call it again here.
    use_cdp, cdp_url = _should_default_to_cdp()
    if use_cdp and not local and cdp_url:
        with Timer() as timer:
            try:
                _browser, ctx = await resolve_browser(cdp_url=cdp_url)
                timer.mark("sdk")
            except Exception as e:
                return make_result(
                    "skyvern_browser_session_create",
                    ok=False,
                    timing_ms=timer.timing_ms,
                    error=make_error(
                        ErrorCode.SDK_ERROR,
                        str(e),
                        f"Failed to connect to local browser at {cdp_url}. "
                        "Make sure Chrome is running with remote debugging enabled.",
                    ),
                )
        return make_result(
            "skyvern_browser_session_create",
            browser_context=ctx,
            data={"local": True, "cdp_url": cdp_url},
            timing_ms=timer.timing_ms,
        )

    with Timer() as timer:
        try:
            if is_stateless_http_mode() and local:
                return make_result(
                    "skyvern_browser_session_create",
                    ok=False,
                    error=make_error(
                        ErrorCode.INVALID_INPUT,
                        "Local browser sessions are not supported in stateless HTTP mode",
                        "Use cloud sessions for remote MCP transport",
                    ),
                )

            skyvern = get_skyvern()
            if is_stateless_http_mode():
                proxy = proxy_location_to_request(coerce_proxy_location(proxy_location))
                session = await skyvern.create_browser_session(timeout=timeout or 60, proxy_location=proxy)
                timer.mark("sdk")
                ctx = BrowserContext(mode="cloud_session", session_id=session.browser_session_id)
                return make_result(
                    "skyvern_browser_session_create",
                    browser_context=ctx,
                    data={
                        "session_id": session.browser_session_id,
                        "timeout_minutes": timeout or 60,
                    },
                    timing_ms=timer.timing_ms,
                )

            browser, result = await do_session_create(
                skyvern,
                timeout=timeout or 60,
                proxy_location=coerce_proxy_location(proxy_location),
                local=local,
                headless=headless,
            )
            timer.mark("sdk")

            if result.local:
                ctx = BrowserContext(mode="local")
            else:
                ctx = BrowserContext(mode="cloud_session", session_id=result.session_id)
            set_current_session(SessionState(browser=browser, context=ctx, api_key_hash=_session_api_key_hash()))

        except ValueError as e:
            return make_result(
                "skyvern_browser_session_create",
                ok=False,
                timing_ms=timer.timing_ms,
                error=make_error(
                    ErrorCode.SDK_ERROR,
                    str(e),
                    "Cloud sessions require SKYVERN_API_KEY. Check your environment.",
                ),
            )
        except Exception as e:
            return make_result(
                "skyvern_browser_session_create",
                ok=False,
                timing_ms=timer.timing_ms,
                error=make_error(ErrorCode.SDK_ERROR, str(e), "Failed to create browser session"),
            )

    if result.local:
        return make_result(
            "skyvern_browser_session_create",
            browser_context=ctx,
            data={"local": True, "headless": result.headless},
            timing_ms=timer.timing_ms,
        )

    return make_result(
        "skyvern_browser_session_create",
        browser_context=ctx,
        data={
            "session_id": result.session_id,
            "timeout_minutes": result.timeout_minutes,
        },
        timing_ms=timer.timing_ms,
    )


async def skyvern_browser_session_close(
    session_id: Annotated[str | None, Field(description="Session ID to close (uses current if not specified)")] = None,
) -> dict[str, Any]:
    """Close a browser session when you're done. Frees cloud resources.

    Closes the specified session or the current active session.
    """
    current = get_current_session()

    with Timer() as timer:
        try:
            if session_id:
                matching_cloud_session = (
                    current.context is not None
                    and current.context.mode == "cloud_session"
                    and current.context.session_id == session_id
                )

                skyvern = get_skyvern()
                result = None
                close_error: Exception | None = None
                try:
                    result = await do_session_close(skyvern, session_id)
                except Exception as e:
                    close_error = e

                if matching_cloud_session:
                    if current.browser is None:
                        set_current_session(SessionState())
                        raise RuntimeError("Expected active browser for matching cloud session")
                    try:
                        await current.browser.close()
                    except Exception as browser_err:
                        if close_error is not None:
                            raise browser_err from close_error
                        raise
                    finally:
                        set_current_session(SessionState())
                elif current.context and current.context.session_id == session_id:
                    set_current_session(SessionState())

                if close_error is not None:
                    raise close_error
                if result is None:
                    raise RuntimeError("Expected session close result after successful close operation")

                timer.mark("sdk")
                return make_result(
                    "skyvern_browser_session_close",
                    data={"session_id": result.session_id, "closed": result.closed},
                    timing_ms=timer.timing_ms,
                )

            if current.browser is None:
                return make_result(
                    "skyvern_browser_session_close",
                    ok=False,
                    error=make_error(
                        ErrorCode.NO_ACTIVE_BROWSER,
                        "No active session to close",
                        "Provide a session_id or create a session first",
                    ),
                )

            closed_id = current.context.session_id if current.context else None
            await current.browser.close()
            set_current_session(SessionState())
            timer.mark("sdk")

        except Exception as e:
            return make_result(
                "skyvern_browser_session_close",
                ok=False,
                timing_ms=timer.timing_ms,
                error=make_error(ErrorCode.SDK_ERROR, str(e), "Failed to close session"),
            )

    return make_result(
        "skyvern_browser_session_close",
        data={"session_id": closed_id, "closed": True},
        timing_ms=timer.timing_ms,
    )


async def skyvern_browser_session_list() -> dict[str, Any]:
    """List all active browser sessions. Use to find available sessions to connect to."""
    with Timer() as timer:
        try:
            skyvern = get_skyvern()
            sessions = await do_session_list(skyvern)
            timer.mark("sdk")

            session_data = [
                {
                    "session_id": s.session_id,
                    "status": s.status,
                    "started_at": s.started_at,
                    "timeout": s.timeout,
                    "runnable_id": s.runnable_id,
                    "available": s.available,
                }
                for s in sessions
            ]

        except ValueError as e:
            return make_result(
                "skyvern_browser_session_list",
                ok=False,
                timing_ms=timer.timing_ms,
                error=make_error(
                    ErrorCode.SDK_ERROR,
                    str(e),
                    "Listing sessions requires SKYVERN_API_KEY",
                ),
            )
        except Exception as e:
            return make_result(
                "skyvern_browser_session_list",
                ok=False,
                timing_ms=timer.timing_ms,
                error=make_error(ErrorCode.SDK_ERROR, str(e), "Failed to list sessions"),
            )

    current = get_current_session()
    current_id = current.context.session_id if current.context else None

    return make_result(
        "skyvern_browser_session_list",
        data={
            "sessions": session_data,
            "count": len(session_data),
            "current_session_id": current_id,
        },
        timing_ms=timer.timing_ms,
    )


async def skyvern_browser_session_get(
    session_id: Annotated[str, "Browser session ID to get details for"],
) -> dict[str, Any]:
    """Get details about a specific browser session -- status, timeout, availability."""
    with Timer() as timer:
        try:
            skyvern = get_skyvern()
            session = await skyvern.get_browser_session(session_id)
            timer.mark("sdk")
        except Exception as e:
            return make_result(
                "skyvern_browser_session_get",
                ok=False,
                timing_ms=timer.timing_ms,
                error=make_error(ErrorCode.BROWSER_NOT_FOUND, str(e), "Check the session ID is correct"),
            )

    current = get_current_session()
    is_current = current.context and current.context.session_id == session_id

    return make_result(
        "skyvern_browser_session_get",
        browser_context=BrowserContext(mode="cloud_session", session_id=session_id) if is_current else None,
        data={
            "session_id": session.browser_session_id,
            "status": session.status,
            "started_at": session.started_at.isoformat() if session.started_at else None,
            "completed_at": session.completed_at.isoformat() if session.completed_at else None,
            "timeout": session.timeout,
            "runnable_id": session.runnable_id,
            "is_current": is_current,
        },
        timing_ms=timer.timing_ms,
    )


async def skyvern_browser_session_connect(
    session_id: Annotated[str | None, Field(description="Cloud session ID (pbs_...)")] = None,
    cdp_url: Annotated[str | None, Field(description="CDP WebSocket URL")] = None,
) -> dict[str, Any]:
    """Connect to an existing browser -- a cloud session by ID or any browser via CDP URL.

    Use this to resume work in a previously created session or attach to an external browser.
    """
    if not session_id and not cdp_url:
        return make_result(
            "skyvern_browser_session_connect",
            ok=False,
            error=make_error(
                ErrorCode.INVALID_INPUT,
                "Must provide session_id or cdp_url",
                "Specify which browser to connect to",
            ),
        )

    with Timer() as timer:
        try:
            browser, ctx = await resolve_browser(session_id=session_id, cdp_url=cdp_url)
            timer.mark("sdk")
        except Exception as e:
            return make_result(
                "skyvern_browser_session_connect",
                ok=False,
                timing_ms=timer.timing_ms,
                error=make_error(ErrorCode.BROWSER_NOT_FOUND, str(e), "Check the session ID or CDP URL is valid"),
            )

    return make_result(
        "skyvern_browser_session_connect",
        browser_context=ctx,
        data={"connected": True},
        timing_ms=timer.timing_ms,
    )
