"""Shared session operations for MCP tools and CLI commands."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from skyvern.schemas.runs import ProxyLocation


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


async def do_session_create(
    skyvern: Any,
    timeout: int = 60,
    proxy_location: str | None = None,
    local: bool = False,
    headless: bool = False,
) -> tuple[Any, SessionCreateResult]:
    """Create browser session. Returns (browser, result)."""
    if local:
        browser = await skyvern.launch_local_browser(headless=headless)
        return browser, SessionCreateResult(session_id=None, local=True, headless=headless)

    proxy = ProxyLocation(proxy_location) if proxy_location else None
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
