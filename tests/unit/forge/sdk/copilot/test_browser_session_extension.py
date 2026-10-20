from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, NoReturn

import pytest
from agents import FunctionTool

from skyvern.exceptions import BrowserSessionExtensionUnconfirmed, BrowserSessionNotExtendable, BrowserSessionNotFound
from skyvern.forge import app
from skyvern.forge.sdk.copilot.blocker_signal import contains_internal_machinery_leak
from skyvern.forge.sdk.copilot.browser_ablation import resolve_copilot_tool_surface
from skyvern.forge.sdk.copilot.context import CopilotContext
from skyvern.forge.sdk.copilot.output_utils import (
    format_tool_result_for_user,
    summarize_tool_result,
    user_facing_success,
)
from skyvern.forge.sdk.copilot.tools import NATIVE_TOOLS, copilot_native_tools
from skyvern.forge.sdk.schemas.persistent_browser_sessions import PersistentBrowserSession
from skyvern.webeye.persistent_sessions_manager import BrowserSessionExtension
from tests.unit.conftest import make_copilot_context

TOOL_NAME = "extend_browser_session"
SESSION_ID = "pbs_chat"
_NOW = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)


def _session(
    timeout_minutes: int = 30, status: str = "running", session_id: str = SESSION_ID
) -> PersistentBrowserSession:
    return PersistentBrowserSession(
        persistent_browser_session_id=session_id,
        organization_id="o",
        created_at=_NOW,
        modified_at=_NOW,
        started_at=_NOW,
        status=status,
        timeout_minutes=timeout_minutes,
    )


class StrictManager:
    """Only the two operations the tool may use exist; touching anything else fails the test."""

    def __init__(
        self,
        sessions: list[PersistentBrowserSession | BaseException | None],
        extend: Callable[[int], BrowserSessionExtension] | BaseException | None = None,
    ) -> None:
        self._sessions = sessions
        self._extend = extend
        self.calls: list[tuple[str, tuple[str | int, ...]]] = []

    async def get_session(self, session_id: str, organization_id: str) -> PersistentBrowserSession | None:
        self.calls.append(("get_session", (session_id, organization_id)))
        session = self._sessions.pop(0)
        if isinstance(session, BaseException):
            raise session
        return session

    async def extend_session(
        self, session_id: str, organization_id: str, additional_minutes: int
    ) -> BrowserSessionExtension:
        self.calls.append(("extend_session", (session_id, organization_id, additional_minutes)))
        if isinstance(self._extend, BaseException):
            raise self._extend
        assert self._extend is not None
        return self._extend(additional_minutes)

    def __getattr__(self, name: str) -> NoReturn:
        raise AssertionError(f"extend_browser_session touched manager.{name}")

    def extend_calls(self) -> int:
        return sum(1 for name, _ in self.calls if name == "extend_session")


def _grant(granted: int, base: int = 30) -> Callable[[int], BrowserSessionExtension]:
    return lambda _requested: BrowserSessionExtension(session=_session(base + granted), granted_minutes=granted)


def _ctx(session_id: str | None = SESSION_ID) -> CopilotContext:
    ctx = make_copilot_context()
    ctx.browser_session_id = session_id
    return ctx


async def _call(ctx: CopilotContext, minutes: int, tool: FunctionTool | None = None) -> dict[str, Any]:
    target = tool or next(t for t in NATIVE_TOOLS if t.name == TOOL_NAME)
    raw = await target.on_invoke_tool(
        SimpleNamespace(context=ctx, tool_name=TOOL_NAME), json.dumps({"additional_minutes": minutes})
    )
    return json.loads(raw)


@pytest.mark.asyncio
async def test_extends_the_chat_session_and_reports_the_managers_grant(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = StrictManager([_session()], extend=_grant(45))
    monkeypatch.setattr(app, "PERSISTENT_SESSIONS_MANAGER", manager)
    catalog_tool = next(
        t
        for t in copilot_native_tools(
            supports_question_tool=True, browser_code_available=True, run_tools_available=True
        )
        if t.name == TOOL_NAME
    )

    payload = await _call(_ctx(), 45, tool=catalog_tool)

    assert manager.extend_calls() == 1
    assert payload["ok"] is True
    assert payload["confirmed"] is True
    assert payload["browser_session_id"] == SESSION_ID
    assert (payload["requested_minutes"], payload["granted_minutes"], payload["timeout_minutes"]) == (45, 45, 75)
    assert (payload["max_creation_timeout_minutes"], payload["max_total_timeout_minutes"]) == (240, 360)
    assert "warning" not in payload
    assert "page_state" not in payload
    assert format_tool_result_for_user(TOOL_NAME, payload) not in {"", "OK"}


@pytest.mark.asyncio
async def test_a_clipped_grant_reports_what_was_granted_with_the_cap_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app, "PERSISTENT_SESSIONS_MANAGER", StrictManager([_session(350)], extend=_grant(10, 350)))

    payload = await _call(_ctx(), 120)

    assert payload["ok"] is True
    assert (payload["requested_minutes"], payload["granted_minutes"], payload["timeout_minutes"]) == (120, 10, 360)
    assert payload["warning"]


@pytest.mark.parametrize(
    ("session_id", "minutes", "sessions", "extend", "extend_called"),
    [
        pytest.param(None, 30, [], None, False, id="no-browser-in-chat"),
        pytest.param(SESSION_ID, 30, [None], None, False, id="missing-or-other-organization"),
        pytest.param(SESSION_ID, 30, [_session(status="completed")], None, False, id="ended"),
        pytest.param(
            SESSION_ID,
            30,
            [_session()],
            BrowserSessionNotExtendable("runs on infrastructure whose lifetime is fixed", SESSION_ID),
            True,
            id="not-extendable",
        ),
        pytest.param(SESSION_ID, 0, [_session()], _grant(0), False, id="zero-minutes"),
        pytest.param(SESSION_ID, -20, [_session()], _grant(-20), False, id="negative-minutes-would-shorten"),
    ],
)
@pytest.mark.asyncio
async def test_refusals_grant_nothing_and_touch_no_other_session(
    monkeypatch: pytest.MonkeyPatch,
    session_id: str | None,
    minutes: int,
    sessions: list[PersistentBrowserSession | BaseException | None],
    extend: Callable[[int], BrowserSessionExtension] | BaseException | None,
    extend_called: bool,
) -> None:
    manager = StrictManager(sessions, extend=extend)
    monkeypatch.setattr(app, "PERSISTENT_SESSIONS_MANAGER", manager)

    payload = await _call(_ctx(session_id), minutes)

    assert payload["ok"] is False
    assert payload.get("confirmed") is not False
    assert "granted_minutes" not in payload
    assert manager.extend_calls() == int(extend_called)
    assert all(args[0] == SESSION_ID for _, args in manager.calls)


@pytest.mark.parametrize(
    "failure",
    [
        BrowserSessionExtensionUnconfirmed(SESSION_ID),
        RuntimeError("rpc deadline exceeded"),
        BrowserSessionNotFound(SESSION_ID),
    ],
    ids=["unconfirmed", "unknown-failure", "not-found-after-signal"],
)
@pytest.mark.asyncio
async def test_an_unconfirmed_extension_reads_back_the_same_session_once_and_claims_no_grant(
    monkeypatch: pytest.MonkeyPatch, failure: BaseException
) -> None:
    ctx = _ctx()
    manager = StrictManager([_session(), _session()], extend=failure)
    original_extend = manager.extend_session

    async def extend_while_a_sibling_rebinds(
        session_id: str, organization_id: str, minutes: int
    ) -> BrowserSessionExtension:
        ctx.browser_session_id = "pbs_successor"
        return await original_extend(session_id, organization_id, minutes)

    manager.extend_session = extend_while_a_sibling_rebinds  # type: ignore[method-assign]
    monkeypatch.setattr(app, "PERSISTENT_SESSIONS_MANAGER", manager)

    payload = await _call(ctx, 60)

    assert manager.extend_calls() == 1
    assert manager.calls[-1] == ("get_session", (SESSION_ID, "o"))
    assert all(args[0] == SESSION_ID for _, args in manager.calls)
    assert payload["ok"] is False
    assert payload["confirmed"] is False
    assert payload["browser_session_id"] == SESSION_ID
    assert payload["session_readback"]["timeout_minutes"] == 30
    assert "granted_minutes" not in payload
    assert user_facing_success(payload) is True
    for text in (
        payload["error"],
        summarize_tool_result(TOOL_NAME, payload),
        format_tool_result_for_user(TOOL_NAME, payload),
    ):
        assert "Failed" not in text and "Nothing was extended" not in text
        assert "session_readback" not in text and not contains_internal_machinery_leak(text)


@pytest.mark.asyncio
async def test_a_failed_readback_claims_no_grant_and_does_not_extend_again(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = StrictManager(
        [_session(), RuntimeError("readback unavailable")], extend=BrowserSessionExtensionUnconfirmed(SESSION_ID)
    )
    monkeypatch.setattr(app, "PERSISTENT_SESSIONS_MANAGER", manager)

    payload = await _call(_ctx(), 60)

    assert manager.extend_calls() == 1
    assert payload["confirmed"] is False
    assert payload["session_readback"] is None
    assert "granted_minutes" not in payload


@pytest.mark.asyncio
async def test_cancellation_propagates_instead_of_becoming_a_result(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = StrictManager([_session()], extend=asyncio.CancelledError())
    monkeypatch.setattr(app, "PERSISTENT_SESSIONS_MANAGER", manager)

    with pytest.raises(asyncio.CancelledError):
        await _call(_ctx(), 30)
    assert manager.extend_calls() == 1


def test_a_turn_without_browser_authority_does_not_advertise_the_tool() -> None:
    native = copilot_native_tools(supports_question_tool=True, browser_code_available=True, run_tools_available=True)
    assert TOOL_NAME in {tool.name for tool in native}

    surface = resolve_copilot_tool_surface(
        mode=None, native_tools=native, alias_map={}, overlays={}, browser_tools_available=False
    )

    assert TOOL_NAME not in surface.ordered_native_names
