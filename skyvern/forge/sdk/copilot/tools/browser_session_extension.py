from __future__ import annotations

from typing import Any, TypedDict

import structlog

from skyvern.exceptions import BrowserSessionExtensionUnconfirmed, BrowserSessionNotExtendable
from skyvern.forge import app
from skyvern.forge.sdk.copilot.runtime import AgentContext
from skyvern.forge.sdk.schemas.persistent_browser_sessions import PersistentBrowserSession, is_final_status
from skyvern.schemas.browser_session_timeouts import (
    MAX_EXTENDED_TIMEOUT,
    MAX_TIMEOUT,
    max_lifetime_exceeded_warning,
)

LOG = structlog.get_logger()

TOOL_NAME = "extend_browser_session"

TOOL_DESCRIPTION = (
    "Add the minutes the user asked for to this chat's current browser session, when they ask to keep the "
    "browser open longer; its pages, cookies and sign-ins stay, and it never renews on its own. "
    f"A session's total lifetime is capped at {MAX_EXTENDED_TIMEOUT} minutes ({MAX_EXTENDED_TIMEOUT // 60} "
    "hours), so a browser cannot be kept indefinitely. Each call adds again: never repeat a call whose result "
    "has `confirmed` false."
)

_LIMITS = {
    "max_creation_timeout_minutes": MAX_TIMEOUT,
    "max_total_timeout_minutes": MAX_EXTENDED_TIMEOUT,
}
_NOTHING_EXTENDED = "Nothing was extended."
_NOT_RETRIED = (
    "Do not request it again for this same ask; each request adds again, and the recorded budget may not show it yet."
)
_UNCONFIRMED = f"More browser time was requested once and is expected to apply, but is unconfirmed. {_NOT_RETRIED}"
_OUTCOME_UNKNOWN = f"More browser time may have been requested once, and whether it applied is unknown. {_NOT_RETRIED}"


class _SessionFacts(TypedDict):
    status: str | None
    timeout_minutes: int | None
    started_at: str | None


def _session_facts(session: PersistentBrowserSession) -> _SessionFacts:
    return {
        "status": session.status,
        "timeout_minutes": session.timeout_minutes,
        "started_at": session.started_at.isoformat() if session.started_at else None,
    }


async def _unconfirmed_result(session_id: str, organization_id: str, error: str) -> dict[str, Any]:
    try:
        readback = await app.PERSISTENT_SESSIONS_MANAGER.get_session(session_id, organization_id)
    except Exception:
        LOG.warning("copilot browser session extension readback failed", browser_session_id=session_id, exc_info=True)
        readback = None
    return {
        "ok": False,
        "confirmed": False,
        "browser_session_id": session_id,
        "error": error,
        "session_readback": _session_facts(readback) if readback else None,
        **_LIMITS,
    }


async def extend_browser_session(ctx: AgentContext, additional_minutes: int) -> dict[str, Any]:
    # Captured once: sibling tool calls share ctx and may rebind the chat's browser while this call awaits.
    session_id = ctx.browser_session_id
    organization_id = ctx.organization_id
    if additional_minutes < 1:
        return {"ok": False, "error": f"additional_minutes must be a positive whole number. {_NOTHING_EXTENDED}"}
    if session_id is None:
        return {
            "ok": False,
            "error": f"No browser is open in this chat, so there is no session to extend. {_NOTHING_EXTENDED}",
        }

    existing = await app.PERSISTENT_SESSIONS_MANAGER.get_session(session_id, organization_id)
    if existing is None:
        return {"ok": False, "error": f"This chat's browser session was not found. {_NOTHING_EXTENDED}"}
    if is_final_status(existing.status):
        return {
            "ok": False,
            "error": f"This chat's browser session has already ended ({existing.status}). {_NOTHING_EXTENDED}",
            **_LIMITS,
        }

    try:
        extension = await app.PERSISTENT_SESSIONS_MANAGER.extend_session(
            session_id, organization_id, additional_minutes
        )
    except BrowserSessionNotExtendable as ex:
        return {
            "ok": False,
            "error": f"This chat's browser session cannot be extended: {ex.reason}. {_NOTHING_EXTENDED}",
            **_LIMITS,
        }
    except BrowserSessionExtensionUnconfirmed:
        return await _unconfirmed_result(session_id, organization_id, _UNCONFIRMED)
    except Exception:
        # Includes BrowserSessionNotFound: the cloud manager can raise it after the extend signal was sent.
        LOG.warning("copilot browser session extension failed", browser_session_id=session_id, exc_info=True)
        return await _unconfirmed_result(session_id, organization_id, _OUTCOME_UNKNOWN)

    result: dict[str, Any] = {
        "ok": True,
        "confirmed": True,
        "browser_session_id": session_id,
        "requested_minutes": additional_minutes,
        "granted_minutes": extension.granted_minutes,
        **_session_facts(extension.session),
        **_LIMITS,
    }
    if extension.granted_minutes < additional_minutes:
        result["warning"] = max_lifetime_exceeded_warning(additional_minutes, extension.granted_minutes)
    return result
