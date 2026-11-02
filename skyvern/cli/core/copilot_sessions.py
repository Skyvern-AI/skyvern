"""The copilot browser-session registry, importable without the MCP session manager and its SDK client."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from skyvern.forge.sdk.copilot.turn_origin import is_self_heal_session_id
from skyvern.forge.sdk.forge_log import current_codeblock_log_redactor

if TYPE_CHECKING:
    from .session_manager import SessionState

# Process-wide registry for copilot browser sessions. Keys always carry the
# owning organization so authenticated HTTP requests cannot address another
# tenant's entry even when the browser session ID is known.
# This bypasses ContextVar propagation issues when FastMCP runs tool handlers
# in a separate task whose context snapshot predates scoped_session().
_copilot_sessions: dict[tuple[str, str], list[SessionState]] = {}


def register_copilot_session(session_id: str, state: SessionState, *, organization_id: str) -> None:
    """Register a pre-configured browser session for cross-task lookup.

    The registry is process-local and in-memory: entries do not survive a
    process restart and are not shared across uvicorn workers. Callers that
    need cross-process continuity must reconnect via the cloud session API.
    """
    if not organization_id:
        raise ValueError("organization_id is required for a copilot browser session")
    if state.organization_id not in {None, organization_id}:
        raise ValueError("session state belongs to a different organization")
    if is_self_heal_session_id(session_id):
        os.environ.pop("DEBUGP", None)
        redactor = current_codeblock_log_redactor()
        if redactor is not None:
            state._codeblock_redactor = redactor
    state.organization_id = organization_id
    state.tab_state_persists = True
    registrations = _copilot_sessions.setdefault((organization_id, session_id), [])
    if not any(registered is state for registered in registrations):
        registrations.append(state)


def unregister_copilot_session(
    session_id: str,
    *,
    organization_id: str,
    expected_state: SessionState | None = None,
) -> None:
    """Remove one exact registration, or every registration for explicit cleanup callers."""
    key = (organization_id, session_id)
    if expected_state is None:
        _copilot_sessions.pop(key, None)
        return
    registrations = _copilot_sessions.get(key)
    if registrations is None:
        return
    registrations[:] = [registered for registered in registrations if registered is not expected_state]
    if not registrations:
        _copilot_sessions.pop(key, None)


def active_copilot_session_ids() -> set[str]:
    """Browser-session IDs currently bound to an active copilot turn."""
    return {session_id for _, session_id in _copilot_sessions}


def registered_copilot_session(session_id: str, *, organization_id: str) -> SessionState | None:
    registrations = _copilot_sessions.get((organization_id, session_id))
    return registrations[-1] if registrations else None
