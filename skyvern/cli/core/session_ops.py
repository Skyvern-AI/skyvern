"""Shared session operations for MCP tools and CLI commands."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

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
    local: bool = False,
    headless: bool = False,
) -> tuple[Any, SessionCreateResult]:
    """Create browser session. Returns (browser, result)."""
    if local:
        browser = await skyvern.launch_local_browser(headless=headless)
        return browser, SessionCreateResult(session_id=None, local=True, headless=headless)

    proxy = coerce_proxy_location(proxy_location)
    browser = await skyvern.launch_cloud_browser(timeout=timeout, proxy_location=proxy)
    return browser, SessionCreateResult(
        session_id=browser.browser_session_id,
        timeout_minutes=timeout,
    )


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
