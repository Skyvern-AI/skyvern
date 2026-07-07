"""Shared session operations for MCP tools and CLI commands."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from skyvern.client.types.extensions import Extensions
from skyvern.schemas.runs import GeoTarget, ProxyLocation, ProxyLocationInput


@dataclass
class SessionCreateResult:
    session_id: str | None
    local: bool = False
    headless: bool = False
    timeout_minutes: int | None = None


@dataclass
class SessionCloseResult:
    session_id: str | None
    closed: bool = True


@dataclass
class SessionInfo:
    session_id: str
    status: str | None
    started_at: str | None
    timeout: int | None
    runnable_id: str | None = None
    available: bool = False


def coerce_proxy_location(proxy_location: ProxyLocationInput | str | None) -> ProxyLocationInput:
    if proxy_location is None or isinstance(proxy_location, (GeoTarget, ProxyLocation)):
        return proxy_location
    if isinstance(proxy_location, dict):
        return GeoTarget.model_validate(proxy_location)

    stripped = proxy_location.strip()
    try:
        parsed_proxy_location = json.loads(stripped)
    except json.JSONDecodeError:
        pass
    else:
        if isinstance(parsed_proxy_location, dict):
            return GeoTarget.model_validate(parsed_proxy_location)
        raise ValueError("Proxy location JSON must be a GeoTarget object.")

    try:
        return ProxyLocation(stripped)
    except ValueError as exc:
        raise ValueError(
            f"Unknown proxy location: {stripped!r}. Expected a valid enum value or a GeoTarget JSON object."
        ) from exc


async def do_session_create(
    skyvern: Any,
    timeout: int = 60,
    proxy_location: ProxyLocationInput | str | None = None,
    extensions: list[Extensions] | None = None,
    browser_profile_id: str | None = None,
    generate_browser_profile: bool = False,
    local: bool = False,
    headless: bool = False,
) -> tuple[Any, SessionCreateResult]:
    """Create browser session. Returns (browser, result)."""
    if local:
        if browser_profile_id is not None or generate_browser_profile:
            raise ValueError(
                "browser_profile_id and generate_browser_profile are only supported for cloud sessions, not local=True."
            )
        browser = await skyvern.launch_local_browser(headless=headless)
        return browser, SessionCreateResult(session_id=None, local=True, headless=headless)

    proxy = coerce_proxy_location(proxy_location)
    launch_kwargs: dict[str, Any] = {"timeout": timeout, "proxy_location": proxy}
    if extensions is not None:
        launch_kwargs["extensions"] = extensions
    if browser_profile_id is not None:
        launch_kwargs["browser_profile_id"] = browser_profile_id
    browser = await skyvern.launch_cloud_browser(**launch_kwargs)
    session_id = browser.browser_session_id
    if generate_browser_profile and session_id:
        await do_session_arm_generate_browser_profile(skyvern, session_id, browser=browser)
    return browser, SessionCreateResult(
        session_id=session_id,
        timeout_minutes=timeout,
    )


async def do_session_arm_generate_browser_profile(
    skyvern: Any,
    session_id: str,
    *,
    browser: Any | None = None,
) -> None:
    """Arm profile capture on a freshly created session, rolling the session back if arming fails.

    Shared by the stateful and stateless create paths so the create -> PATCH -> rollback contract
    lives in one place. Pass ``browser`` for the stateful path so its local CDP connection is torn
    down on rollback too.
    """
    try:
        await do_session_update_generate_browser_profile(skyvern, session_id, True)
    except Exception as exc:
        await do_session_rollback_created_session(skyvern, session_id, exc, browser=browser)
        raise


async def do_session_update_generate_browser_profile(
    skyvern: Any,
    session_id: str,
    generate_browser_profile: bool,
) -> None:
    """PATCH a live browser session's profile-capture flag via the private SDK wrapper.

    The generated Fern client has no browser-session update method yet (the route exists and is
    annotated for Fern), so we reach through the wrapper deliberately until the SDK is regenerated.
    """
    response = await skyvern._client_wrapper.httpx_client.request(
        f"v1/browser_sessions/{session_id}",
        method="PATCH",
        json={"generate_browser_profile": generate_browser_profile},
        headers={"content-type": "application/json"},
    )
    if response.status_code >= 400:
        try:
            body = response.json()
            detail = body.get("detail", response.text) if isinstance(body, dict) else response.text
        except (ValueError, json.JSONDecodeError):
            detail = response.text
        raise RuntimeError(f"Failed to update browser session {session_id}: HTTP {response.status_code}: {detail}")


async def do_session_rollback_created_session(
    skyvern: Any,
    session_id: str,
    failure: Exception,
    *,
    browser: Any | None = None,
) -> None:
    """Close a just-created cloud session (and its local browser) after later setup fails.

    Tears down both the remote session and, when provided, the local CDP/Playwright connection. If
    either cleanup fails, the original ``failure`` is preserved and the cleanup error is appended.
    """
    cleanup_errors: list[str] = []
    try:
        await do_session_close(skyvern, session_id)
    except Exception as session_error:
        cleanup_errors.append(f"failed to close browser session {session_id}: {session_error}")
    if browser is not None:
        try:
            await browser.close()
        except Exception as browser_error:
            cleanup_errors.append(f"failed to close local browser for {session_id}: {browser_error}")
    if cleanup_errors:
        raise RuntimeError(
            f"{failure}; additionally {'; '.join(cleanup_errors)} after session setup failed"
        ) from failure


async def do_session_close(skyvern: Any, session_id: str) -> SessionCloseResult:
    """Close a browser session by ID."""
    await skyvern.close_browser_session(session_id)
    return SessionCloseResult(session_id=session_id)


async def do_session_list(skyvern: Any) -> list[SessionInfo]:
    """List all browser sessions."""
    sessions = await skyvern.get_browser_sessions()
    return [
        SessionInfo(
            session_id=s.browser_session_id,
            status=s.status,
            started_at=s.started_at.isoformat() if s.started_at else None,
            timeout=s.timeout,
            runnable_id=s.runnable_id,
            available=s.runnable_id is None and s.browser_address is not None,
        )
        for s in sessions
    ]
