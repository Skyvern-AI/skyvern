"""Unit tests for the Task V3 captcha tool."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from skyvern.forge import app
from skyvern.forge.taskv3 import captcha_tools
from skyvern.webeye.utils import captcha_solver as captcha_solver_module
from skyvern.webeye.utils.captcha_solver import CaptchaChallengeUnsolvedError
from tests.unit.conftest import ScopeRecordingAgentFunction


def _task(**overrides: Any) -> SimpleNamespace:
    base: dict[str, Any] = {"task_id": "tsk_1", "workflow_run_id": "wr_1", "browser_session_id": "bs_1"}
    base.update(overrides)
    return SimpleNamespace(**base)


def _provider(page: Any) -> Any:
    async def _get_page() -> Any:
        return page

    return _get_page


def test_build_captcha_tools_always_offered() -> None:
    tools, guidance = captcha_tools.build_captcha_tools(_task(), _provider(object()), organization_id="o_1")
    assert [t.name for t in tools] == ["solve_captcha"]
    tool = tools[0]
    # Persisted with a screenshot for artifact parity, but not billed/budgeted: a solve is anti-bot
    # overhead, not a user-facing navigation step, and a no-op "absent" ok must not meter like an action.
    assert tool.recordable is True
    assert tool.billable is False
    assert "captcha" in guidance.lower()
    # Guidance must steer the model off the visible symptom (submit didn't advance), since it cannot
    # see the cross-origin iframe that carries the gate.
    assert "advance" in guidance.lower() or "verify you are human" in guidance.lower()


@pytest.mark.asyncio
async def test_solve_captcha_page_unavailable_is_error(monkeypatch: pytest.MonkeyPatch) -> None:
    ladder = AsyncMock()
    monkeypatch.setattr(captcha_tools, "solve_challenge_ladder", ladder)
    tools, _ = captcha_tools.build_captcha_tools(_task(), _provider(None), organization_id="o_1")
    result = await tools[0].handler({})
    assert result.status == "error"
    ladder.assert_not_awaited()


@pytest.mark.asyncio
async def test_solve_captcha_solved_returns_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(captcha_tools, "solve_challenge_ladder", AsyncMock(return_value=True))
    tools, _ = captcha_tools.build_captcha_tools(_task(), _provider(object()), organization_id="o_1")
    result = await tools[0].handler({})
    assert result.status == "ok"
    assert "solved" in result.content.lower()


@pytest.mark.asyncio
async def test_solve_captcha_absent_is_ok_and_steers_away(monkeypatch: pytest.MonkeyPatch) -> None:
    # No challenge present: this must be an ok the model can retry after a fresh observe — never an
    # error, and never a blanket instruction to stop calling solve_captcha, since a later page state
    # (e.g. a frame-nested challenge that appears after further navigation) may genuinely have one.
    ladder = AsyncMock(return_value=False)
    monkeypatch.setattr(captcha_tools, "solve_challenge_ladder", ladder)
    tools, _ = captcha_tools.build_captcha_tools(_task(), _provider(object()), organization_id="o_1")
    handler = tools[0].handler

    result = await handler({})

    assert result.status == "ok"
    assert "no" in result.content.lower() and "captcha" in result.content.lower()
    assert "do not retry solve_captcha" not in result.content.lower()

    # The cap is not consumed by an absent result: calling again still reaches the ladder.
    await handler({})
    assert ladder.await_count == 2


@pytest.mark.asyncio
async def test_every_ok_branch_names_a_distinct_outcome_class(monkeypatch: pytest.MonkeyPatch) -> None:
    # The tool returns ok on three different events -- it solved one, there was none to solve, and it
    # declined after the cap -- and `tool_status` cannot tell them apart, so a blind detector and a
    # working one are the same row to every downstream reader. Drives all three through the real
    # handler rather than pinning strings: the branches were previously separable only by
    # `len(content)`, which is exactly the accident this replaces (it has already changed once).
    ladder = AsyncMock(return_value=True)
    monkeypatch.setattr(captcha_tools, "solve_challenge_ladder", ladder)
    tools, _ = captcha_tools.build_captcha_tools(_task(), _provider(object()), organization_id="o_1")
    handler = tools[0].handler

    solved = await handler({})
    ladder.return_value = False
    absent = await handler({})
    # Exhaust the cap on real failures, then the next call is the short-circuit branch.
    ladder.side_effect = CaptchaChallengeUnsolvedError("x")
    for _ in range(captcha_tools._MAX_SOLVE_ATTEMPTS):
        await handler({})
    declined = await handler({})

    assert (solved.status, solved.ok_class) == ("ok", "solved")
    assert (absent.status, absent.ok_class) == ("ok", "absent")
    assert (declined.status, declined.ok_class) == ("ok", "attempts_exhausted")
    # Distinctness is the property that matters: two branches sharing a value would reintroduce the
    # conflation while every per-branch assertion above still passed.
    assert len({solved.ok_class, absent.ok_class, declined.ok_class}) == 3


def test_every_ok_construction_in_the_module_names_an_ok_class() -> None:
    """Totality, which the three per-branch assertions above cannot give.

    `tool_ok_class` is emitted only when the site names one, so a fourth `ok` branch added here
    without an `ok_class` is silently absent from the facet: its rows still carry `tool_status=ok`,
    the buckets stop summing to the tool's own ok total, and every test above stays green because
    each pins a branch that exists. Read out of the module's own AST rather than from a list of
    known call sites -- an enumerated list is the same defect one level up and would not see the
    fourth branch either.
    """
    import ast
    import inspect
    import pathlib

    module_path = pathlib.Path(inspect.getfile(captcha_tools))
    source = module_path.read_text()
    tree = ast.parse(source)

    ok_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "ok"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "ToolResult"
    ]
    # Anti-vacuity, both directions. A walk that matched nothing would satisfy the loop below in
    # silence, and a walk that matched fewer sites than the file spells out is reading past some of
    # them -- either way the check would vouch for branches it never saw.
    assert len(ok_calls) == source.count("ToolResult.ok("), "the AST walk missed an ok construction"
    assert len(ok_calls) >= 3, "the scan found no ok constructions to check; it is not looking"

    unnamed = []
    for node in ok_calls:
        named = [k for k in node.keywords if k.arg == "ok_class"]
        # `**mapping` carries keys this scan cannot read, so it cannot vouch for the site either.
        readable = named and not any(isinstance(k.value, ast.Constant) and k.value.value is None for k in named)
        if not readable:
            unnamed.append(node.lineno)
    assert not unnamed, f"ToolResult.ok() with no ok_class at {module_path.name} lines {unnamed}"

    # The other route to an ok: the raw constructor takes the class positionally and can pair an
    # `ok` status with an `error_class`, so it would evade the check above entirely. This module
    # has no reason to use it, and the cheapest way to keep the scan total is to keep it that way.
    raw = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "ToolResult"
    ]
    assert not raw, f"raw ToolResult(...) construction bypasses the ok_class check at lines {raw}"

    classes = {
        k.value.value
        for node in ok_calls
        for k in node.keywords
        if k.arg == "ok_class" and isinstance(k.value, ast.Constant)
    }
    assert {"solved", "absent", "attempts_exhausted"} <= classes, "the scan is not reading the real sites"


@pytest.mark.asyncio
async def test_solve_captcha_unsolved_is_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        captcha_tools, "solve_challenge_ladder", AsyncMock(side_effect=CaptchaChallengeUnsolvedError("x"))
    )
    tools, _ = captcha_tools.build_captcha_tools(_task(), _provider(object()), organization_id="o_1")
    result = await tools[0].handler({})
    assert result.status == "error"


@pytest.mark.asyncio
async def test_solve_captcha_hang_is_bounded_to_error(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _never_returns(*_a: object, **_k: object) -> bool:
        await asyncio.Event().wait()
        raise AssertionError("should be cancelled")

    monkeypatch.setattr(captcha_tools, "solve_challenge_ladder", AsyncMock(side_effect=_never_returns))
    monkeypatch.setattr(captcha_tools, "_SOLVE_CAPTCHA_CEILING_SECONDS", 0.01)
    tools, _ = captcha_tools.build_captcha_tools(_task(), _provider(object()), organization_id="o_1")
    result = await asyncio.wait_for(tools[0].handler({}), timeout=5)
    assert result.status == "error"


@pytest.mark.asyncio
async def test_solve_captcha_threads_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    ladder = AsyncMock(return_value=True)
    monkeypatch.setattr(captcha_tools, "solve_challenge_ladder", ladder)
    page = object()
    tools, _ = captcha_tools.build_captcha_tools(
        _task(workflow_run_id="wr_9", browser_session_id="bs_9"), _provider(page), organization_id="o_9"
    )
    await tools[0].handler({})
    ladder.assert_awaited_once_with(
        page, organization_id="o_9", workflow_run_id="wr_9", browser_session_id="bs_9", probe_child_frames=True
    )


@pytest.mark.asyncio
async def test_solve_captcha_attempt_guard_stops_calling_ladder(monkeypatch: pytest.MonkeyPatch) -> None:
    # A pathological loop must not run the solver forever: after the cap, the tool short-circuits with a
    # steer-away result and never invokes the ladder again.
    ladder = AsyncMock(side_effect=CaptchaChallengeUnsolvedError("x"))
    monkeypatch.setattr(captcha_tools, "solve_challenge_ladder", ladder)
    tools, _ = captcha_tools.build_captcha_tools(_task(), _provider(object()), organization_id="o_1")
    handler = tools[0].handler
    for _ in range(captcha_tools._MAX_SOLVE_ATTEMPTS):
        await handler({})
    assert ladder.await_count == captcha_tools._MAX_SOLVE_ATTEMPTS
    result = await handler({})
    assert ladder.await_count == captcha_tools._MAX_SOLVE_ATTEMPTS  # not called again
    assert result.status in {"ok", "error"}


@pytest.mark.asyncio
async def test_solve_captcha_success_resets_failure_streak(monkeypatch: pytest.MonkeyPatch) -> None:
    # A real solve must clear the failure streak, so a task with several genuine captchas is not
    # disabled by earlier failures.
    unsolved = CaptchaChallengeUnsolvedError("x")
    ladder = AsyncMock(side_effect=[unsolved, unsolved, True, unsolved, unsolved, unsolved])
    monkeypatch.setattr(captcha_tools, "solve_challenge_ladder", ladder)
    tools, _ = captcha_tools.build_captcha_tools(_task(), _provider(object()), organization_id="o_1")
    handler = tools[0].handler
    # 2 failures, then a solve (streak resets), then 3 more failures = 6 ladder calls before the cap trips.
    for _ in range(6):
        await handler({})
    assert ladder.await_count == 6
    result = await handler({})
    assert ladder.await_count == 6  # cap reached only after 3 CONSECUTIVE post-reset failures
    assert result.status == "ok"


@pytest.mark.asyncio
async def test_solve_captcha_absent_does_not_consume_the_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    # A page with no captcha returns absent(ok); repeated absent no-ops must never trip the failure cap.
    ladder = AsyncMock(return_value=False)
    monkeypatch.setattr(captcha_tools, "solve_challenge_ladder", ladder)
    tools, _ = captcha_tools.build_captcha_tools(_task(), _provider(object()), organization_id="o_1")
    handler = tools[0].handler
    for _ in range(captcha_tools._MAX_SOLVE_ATTEMPTS + 3):
        result = await handler({})
        assert result.status == "ok"
    assert ladder.await_count == captcha_tools._MAX_SOLVE_ATTEMPTS + 3  # every call reached the ladder


@pytest.mark.asyncio
async def test_solve_captcha_provider_raising_is_error_not_crash(monkeypatch: pytest.MonkeyPatch) -> None:
    ladder = AsyncMock()
    monkeypatch.setattr(captcha_tools, "solve_challenge_ladder", ladder)

    async def _raising_provider() -> Any:
        raise RuntimeError("page lost")

    tools, _ = captcha_tools.build_captcha_tools(_task(), _raising_provider, organization_id="o_1")
    result = await tools[0].handler({})
    assert result.status == "error"
    ladder.assert_not_awaited()


class _MarkerOnlyLocator:
    def __init__(self, count: int) -> None:
        self._count = count

    async def count(self) -> int:
        return self._count

    def nth(self, _index: int) -> _MarkerOnlyLocator:
        return self

    async def is_visible(self) -> bool:
        return True

    async def bounding_box(self) -> dict[str, float]:
        return {"x": 10.0, "y": 10.0, "width": 300.0, "height": 80.0}


class _MarkerOnlyPage:
    """Only the generic CAPTCHA marker selector matches, and it is rendered: the real ladder detects a
    challenge, finds no checkbox/anchor/recaptcha arm, and raises CaptchaChallengeUnsolvedError."""

    def __init__(self) -> None:
        self.url = "https://app.example/login"
        self.frames: list[Any] = []

    def locator(self, selector: str) -> _MarkerOnlyLocator:
        return _MarkerOnlyLocator(1 if selector == captcha_solver_module._CAPTCHA_MARKER_SELECTOR else 0)

    async def wait_for_timeout(self, _milliseconds: int) -> None:
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_capped_solve_never_enters_lifecycle_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    # Each real solve opens the shared lifecycle scope once; once the failure cap trips, the tool
    # fast-fails BEFORE the ladder, so the vendor lifecycle is never entered again.
    agent_function = ScopeRecordingAgentFunction(record_arms=False)
    monkeypatch.setattr(app, "AGENT_FUNCTION", agent_function)
    tools, _ = captcha_tools.build_captcha_tools(_task(), _provider(_MarkerOnlyPage()), organization_id="o_1")
    handler = tools[0].handler

    for _ in range(captcha_tools._MAX_SOLVE_ATTEMPTS):
        result = await handler({})
        assert result.status == "error"
    assert agent_function.events == ["enter", "exit"] * captcha_tools._MAX_SOLVE_ATTEMPTS

    events_at_cap = list(agent_function.events)
    result = await handler({})
    assert result.status == "ok"  # steer-away, not another solver run
    assert agent_function.events == events_at_cap  # no further enter/exit


@pytest.mark.asyncio
async def test_page_unavailable_never_enters_lifecycle_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    # A missing page fast-fails before the ladder, so the vendor lifecycle is never armed.
    agent_function = ScopeRecordingAgentFunction(record_arms=False)
    monkeypatch.setattr(app, "AGENT_FUNCTION", agent_function)
    tools, _ = captcha_tools.build_captcha_tools(_task(), _provider(None), organization_id="o_1")

    result = await tools[0].handler({})
    assert result.status == "error"
    assert agent_function.events == []
