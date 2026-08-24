"""Unit tests for the Task V3 captcha tool."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from skyvern.forge.taskv3 import captcha_tools
from skyvern.webeye.utils.captcha_solver import CaptchaChallengeUnsolvedError


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
    # No challenge present: the model must NOT loop on solve_captcha, so this is an ok whose content
    # tells it to stop retrying — never an error it would retry.
    monkeypatch.setattr(captcha_tools, "solve_challenge_ladder", AsyncMock(return_value=False))
    tools, _ = captcha_tools.build_captcha_tools(_task(), _provider(object()), organization_id="o_1")
    result = await tools[0].handler({})
    assert result.status == "ok"
    assert "no" in result.content.lower() and "captcha" in result.content.lower()


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
    ladder.assert_awaited_once_with(page, organization_id="o_9", workflow_run_id="wr_9", browser_session_id="bs_9")


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
