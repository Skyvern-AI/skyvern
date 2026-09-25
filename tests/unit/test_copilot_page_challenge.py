from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.forge.sdk.copilot.runtime import (
    SENSITIVE_ORIGIN_ACTIVE_RUN_PAGE_ERROR,
    SENSITIVE_ORIGIN_PAGE_ERROR,
    AgentContext,
    CopilotBrowserSessionUnavailable,
)
from skyvern.forge.sdk.copilot.tools import page_challenge
from skyvern.webeye.utils.captcha_solver import CaptchaChallengeUnsolvedError
from tests.unit.test_copilot_hooks import _ListenerPage
from tests.unit.test_copilot_runtime import _FakeBrowserContext, _make_ctx

_VENDOR_FRAME = "https://challenge.vendor.test/turnstile/v0/api.html"
_UNTRIED_FRESH_BROWSER = "start_fresh_browser: a new browser session with no cookies, storage or challenge history"


class _Page(_ListenerPage):
    def __init__(self, child_frame_urls: list[str] | None = None) -> None:
        super().__init__(child_frame_urls)
        self.url = "https://records.example.test/search"
        self.context = _FakeBrowserContext()
        self.closed = False

    def is_closed(self) -> bool:
        return self.closed


def _chat(monkeypatch: pytest.MonkeyPatch, page: _Page, ladder: AsyncMock, *, available: bool = True) -> AgentContext:
    @asynccontextmanager
    async def _admitted(_ctx: AgentContext) -> AsyncIterator[None]:
        yield

    agent_function = MagicMock()
    agent_function.captcha_solving_available = AsyncMock(return_value=available)
    monkeypatch.setattr(page_challenge, "app", MagicMock(AGENT_FUNCTION=agent_function))
    monkeypatch.setattr(page_challenge, "mcp_browser_context", _admitted)
    monkeypatch.setattr(page_challenge, "live_working_page", AsyncMock(return_value=page))
    monkeypatch.setattr(page_challenge, "solve_challenge_ladder", ladder)
    ctx = _make_ctx()
    ctx.browser_session_id = "pbs_chat"
    return ctx


async def _hang(*_args: object, **_kwargs: object) -> bool:
    await asyncio.Event().wait()
    return True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("ladder", "frames", "available", "expected"),
    [
        (AsyncMock(return_value=True), [], True, {"outcome": "solved"}),
        (AsyncMock(return_value=False), [], True, {"outcome": "none"}),
        (
            AsyncMock(return_value=False),
            [_VENDOR_FRAME],
            True,
            {"outcome": "unsupported", "untried_in_this_request": [_UNTRIED_FRESH_BROWSER]},
        ),
        (
            AsyncMock(side_effect=CaptchaChallengeUnsolvedError()),
            [],
            True,
            {
                "outcome": "unsolved",
                "unsolved_challenges_in_this_browser_session": 1,
                "untried_in_this_request": [_UNTRIED_FRESH_BROWSER],
            },
        ),
        (AsyncMock(side_effect=_hang), [], True, {"outcome": "unsolved", "timed_out": True}),
        (AsyncMock(side_effect=RuntimeError("boom")), [], True, {"outcome": "unsolved", "solver_failed": True}),
        (AsyncMock(return_value=True), [], False, {"outcome": "unavailable"}),
    ],
    ids=["solved", "none", "vendor_on_screen_but_undetected", "unsolved", "timed_out", "solver_failed", "unavailable"],
)
async def test_each_ladder_result_is_reported_as_its_own_outcome(
    monkeypatch: pytest.MonkeyPatch,
    ladder: AsyncMock,
    frames: list[str],
    available: bool,
    expected: dict[str, Any],
) -> None:
    monkeypatch.setattr(page_challenge, "SOLVE_CEILING_SECONDS", 0.05)
    ctx = _chat(monkeypatch, _Page(frames), ladder, available=available)

    result = await page_challenge.solve_page_challenge(ctx)

    assert {key: result.get(key) for key in expected} == expected
    assert result["ok"] is True
    assert ladder.await_count == (1 if available else 0)


@pytest.mark.asyncio
async def test_a_browser_lost_during_the_solve_is_reported_as_session_loss_not_unsolved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = _Page()
    page.closed = True
    ctx = _chat(monkeypatch, page, AsyncMock(side_effect=CaptchaChallengeUnsolvedError()))
    disposition = AsyncMock(return_value="reestablished")
    monkeypatch.setattr(page_challenge, "_browser_session_error_disposition", disposition)

    result = await page_challenge.solve_page_challenge(ctx)

    lost = disposition.await_args.args[1]
    assert isinstance(lost, CopilotBrowserSessionUnavailable) and lost.session_id == "pbs_chat"
    assert result["data"]["browser_session_continuity"]["disposition"] == "reestablished"
    assert "outcome" not in result
    assert ctx.unsolved_page_challenges_by_session_id == {}


@pytest.mark.asyncio
async def test_unsolved_attempts_are_counted_per_browser_and_a_new_browser_starts_over(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ladder = AsyncMock(side_effect=CaptchaChallengeUnsolvedError())
    ctx = _chat(monkeypatch, _Page(), ladder)

    for attempt in (1, 2):
        result = await page_challenge.solve_page_challenge(ctx)
        assert result["unsolved_challenges_in_this_browser_session"] == attempt
        assert result["untried_in_this_request"] == [_UNTRIED_FRESH_BROWSER]
    ctx.browser_session_replacements["pbs_chat"] = "pbs_fresh"
    ctx.browser_session_id = "pbs_fresh"
    after_replacement = await page_challenge.solve_page_challenge(ctx)

    assert after_replacement["outcome"] == "unsolved"
    assert after_replacement["unsolved_challenges_in_this_browser_session"] == 1
    assert after_replacement["fresh_browser_tried_for_this_request"] is True
    assert "untried_in_this_request" not in after_replacement
    assert ladder.await_count == 3


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("taint_field", "expected_error"),
    [
        ("active_sensitive_origin_browser_session_ids", SENSITIVE_ORIGIN_ACTIVE_RUN_PAGE_ERROR),
        ("sensitive_origin_browser_session_ids", SENSITIVE_ORIGIN_PAGE_ERROR),
    ],
    ids=["active_sensitive_run", "tainted_page"],
)
async def test_a_sensitive_page_is_never_sent_to_the_solver(
    monkeypatch: pytest.MonkeyPatch, taint_field: str, expected_error: str
) -> None:
    ladder = AsyncMock(return_value=True)
    ctx = _chat(monkeypatch, _Page(), ladder)
    setattr(ctx, taint_field, {"pbs_chat"})

    result = await page_challenge.solve_page_challenge(ctx)

    assert result == {"ok": False, "error": expected_error}
    ladder.assert_not_awaited()
