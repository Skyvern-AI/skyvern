"""Empty-page recovery: close a dead blank working page and fall back to a survivor.

Covers the scraper blank predicate, the scrape-ladder blank-evidence preservation, the
handle_completed_step ActionBlock/max-step guards for a recovery-only step, the download-popup
claim read, and the cached-script generation marker filter.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.exceptions import FailedToReloadPage, ScrapingFailedBlankPage
from skyvern.forge.agent import ForgeAgent
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.models import StepStatus
from skyvern.forge.sdk.workflow.models.block import ActionBlock
from skyvern.schemas.steps import AgentStepOutput
from skyvern.webeye.actions.actions import ClosePageAction
from skyvern.webeye.actions.responses import ActionSuccess
from skyvern.webeye.scraper.scraper import (
    page_has_meaningful_child_frame,
    page_is_dead_blank,
    page_is_http_survivor,
    scrape_web_unsafe,
)
from tests.unit.helpers import make_organization, make_step, make_task


def _frame(url: str) -> MagicMock:
    frame = MagicMock()
    frame.url = url
    return frame


def _page(url: str, child_frame_urls: list[str] | None = None) -> MagicMock:
    page = MagicMock()
    page.url = url
    page.main_frame.child_frames = [_frame(u) for u in (child_frame_urls or [])]
    return page


# ---- Case 1: scraper blank predicate + raise -------------------------------------------------


@pytest.mark.parametrize(
    ("url", "child_frame_urls", "expected"),
    [
        (":", [], True),
        ("about:blank", [], True),
        (":", ["about:blank", ""], True),  # blank/empty frames are not meaningful
        (":", ["https://real.test/app"], False),  # meaningful frame -> not a dead blank
        ("about:blank", ["https://real.test/app"], False),
        ("https://real.test/app", [], False),
    ],
)
def test_page_is_dead_blank(url: str, child_frame_urls: list[str], expected: bool) -> None:
    assert page_is_dead_blank(_page(url, child_frame_urls)) is expected


def test_page_has_meaningful_child_frame_filters_blank_and_empty() -> None:
    assert page_has_meaningful_child_frame(_page(":", ["about:blank", ""])) is False
    assert page_has_meaningful_child_frame(_page(":", ["https://real.test"])) is True


@pytest.mark.parametrize(
    ("url", "is_closed", "expected"),
    [
        ("https://real.test/app", False, True),
        ("http://real.test", False, True),
        (":", False, False),  # dead blank is not a survivor
        ("about:blank", False, False),
        ("chrome-error://chromewebdata/", False, False),  # error tab is not a survivor
        ("https://real.test/app", True, False),  # closed http page is not a survivor
    ],
)
def test_page_is_http_survivor(url: str, is_closed: bool, expected: bool) -> None:
    page = MagicMock()
    page.url = url
    page.is_closed.return_value = is_closed
    assert page_is_http_survivor(page) is expected


@pytest.mark.asyncio
async def test_scrape_raises_blank_for_colon_page_without_meaningful_frames() -> None:
    # The production incident signature: the working page's url is ":" with no meaningful child
    # frame. On main this url slips past the about:blank-only guard; it must now be classified blank.
    browser_state = MagicMock()
    browser_state.must_get_working_page = AsyncMock(return_value=_page(":", []))

    with pytest.raises(ScrapingFailedBlankPage):
        await scrape_web_unsafe(browser_state, "https://real.test", AsyncMock())


@pytest.mark.asyncio
async def test_scrape_does_not_raise_blank_when_support_empty_page() -> None:
    # Negative: an empty-page-tolerant scrape never classifies the dead blank, so recovery never
    # arms for those callers (the support_empty_page escape is preserved).
    browser_state = MagicMock()
    browser_state.must_get_working_page = AsyncMock(return_value=_page(":", []))

    with pytest.raises(Exception) as exc_info:
        await scrape_web_unsafe(browser_state, "https://real.test", AsyncMock(), support_empty_page=True)
    assert not isinstance(exc_info.value, ScrapingFailedBlankPage)


@pytest.mark.asyncio
async def test_scrape_does_not_classify_colon_page_with_meaningful_frame_as_blank() -> None:
    # A ":" page carrying a meaningful child frame is not a dead blank; it must not raise the blank
    # classification (it proceeds into normal scraping, which then fails on the bare mock elsewhere).
    browser_state = MagicMock()
    browser_state.must_get_working_page = AsyncMock(return_value=_page(":", ["https://real.test/app"]))

    with pytest.raises(Exception) as exc_info:
        await scrape_web_unsafe(browser_state, "https://real.test", AsyncMock())
    assert not isinstance(exc_info.value, ScrapingFailedBlankPage)


# ---- Case 2: scrape ladder preserves the earlier blank evidence -----------------------------


@pytest.mark.asyncio
async def test_ladder_reraises_blank_even_when_final_rung_fails_to_reload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = ForgeAgent()
    now = datetime.now(UTC)
    organization = make_organization(now)
    task = make_task(now, organization, navigation_goal="do it", workflow_run_id="wr-ladder")
    step = make_step(now, task, step_id="step-ladder", status=StepStatus.running, order=0, output=None)

    # NORMAL and STOPLOADING see the blank page; the final RELOAD rung fails to reload the
    # uncommitted ":" page. The ladder must surface the blank evidence, not the reload failure.
    side_effects = [
        ScrapingFailedBlankPage(),
        ScrapingFailedBlankPage(),
        FailedToReloadPage(url="https://real.test", error_message="cannot reload :"),
    ]
    agent._scrape_with_type = AsyncMock(side_effect=side_effects)
    agent._persist_scrape_artifacts = AsyncMock()

    context = SkyvernContext(task_id=task.task_id, organization_id=task.organization_id)
    skyvern_context.set(context)
    try:
        with pytest.raises(ScrapingFailedBlankPage):
            await agent.build_and_record_step_prompt(task, step, MagicMock(), engine=None)  # type: ignore[arg-type]
    finally:
        skyvern_context.reset()


# ---- Case 4: outer contract -- a recovery-only step does not complete an ActionBlock ---------


def _recovery_step(now: datetime, task, *, order: int):
    output = AgentStepOutput(actions_and_results=[(ClosePageAction(is_internal_recovery=True), [ActionSuccess()])])
    return make_step(now, task, step_id="step-recovery", status=StepStatus.completed, order=order, output=output)


async def _run_handle_completed_step(monkeypatch: pytest.MonkeyPatch, *, mark_recovery: bool):
    agent = ForgeAgent()
    now = datetime.now(UTC)
    organization = make_organization(now)
    # max_steps_per_run=1 is the ActionBlock default; step.order+1 would trip the max-steps failure.
    task = make_task(now, organization, navigation_goal="do it", workflow_run_id="wr-block", max_steps_per_run=1)
    step = _recovery_step(now, task, order=0)

    agent._check_workflow_run_step_budget = AsyncMock(return_value=None)
    agent.update_step = AsyncMock(side_effect=lambda s, *a, **k: s)
    agent.update_task = AsyncMock(side_effect=lambda t, *a, **k: t)
    next_step_sentinel = MagicMock(name="next_step")
    monkeypatch.setattr(
        "skyvern.forge.agent.app.DATABASE.tasks.create_step",
        AsyncMock(return_value=next_step_sentinel),
    )

    context = SkyvernContext(task_id=task.task_id, organization_id=task.organization_id)
    if mark_recovery:
        context.empty_page_recovery_step_id = step.step_id
    skyvern_context.set(context)
    try:
        return next_step_sentinel, await agent.handle_completed_step(
            organization=organization,
            task=task,
            step=step,
            page=MagicMock(),
            task_block=ActionBlock.model_construct(label="b", max_steps_per_run=1),
            browser_state=MagicMock(),
            scraped_page=None,
        )
    finally:
        skyvern_context.reset()


async def _run_navigation_completed_step(monkeypatch: pytest.MonkeyPatch, *, mark_recovery: bool):
    # A non-ActionBlock navigation task with goal verification enabled: without a recovery guard this
    # routes through the parallel-verification path, which applies its own unconditional max-step
    # failure and would fail a recovery step on the last allowed step (violating invariant I3).
    agent = ForgeAgent()
    now = datetime.now(UTC)
    organization = make_organization(now)
    task = make_task(now, organization, navigation_goal="do it", workflow_run_id="wr-nav", max_steps_per_run=1)
    step = _recovery_step(now, task, order=0)

    agent._check_workflow_run_step_budget = AsyncMock(return_value=None)
    agent.update_step = AsyncMock(side_effect=lambda s, *a, **k: s)
    agent.update_task = AsyncMock(side_effect=lambda t, *a, **k: t)
    agent.summary_failure_reason_for_max_steps = AsyncMock()
    next_step_sentinel = MagicMock(name="next_step")
    monkeypatch.setattr(
        "skyvern.forge.agent.app.DATABASE.tasks.create_step",
        AsyncMock(return_value=next_step_sentinel),
    )
    # DISABLE_USER_GOAL_CHECK=False => verification would dispatch unless the step is recovery-only.
    monkeypatch.setattr(
        "skyvern.forge.agent.app.EXPERIMENTATION_PROVIDER.is_feature_enabled_cached",
        AsyncMock(return_value=False),
    )
    parallel_sentinel = MagicMock(name="parallel_result")
    parallel = AsyncMock(return_value=parallel_sentinel)
    agent._handle_completed_step_with_parallel_verification = parallel

    context = SkyvernContext(task_id=task.task_id, organization_id=task.organization_id)
    if mark_recovery:
        context.empty_page_recovery_step_id = step.step_id
    skyvern_context.set(context)
    try:
        result = await agent.handle_completed_step(
            organization=organization,
            task=task,
            step=step,
            page=MagicMock(),
            task_block=None,
            browser_state=MagicMock(),
            scraped_page=MagicMock(),
        )
        return next_step_sentinel, parallel, result
    finally:
        skyvern_context.reset()


@pytest.mark.asyncio
async def test_recovery_only_navigation_step_skips_parallel_verification(monkeypatch: pytest.MonkeyPatch) -> None:
    next_step_sentinel, parallel, (task_result, last_step, created_next) = await _run_navigation_completed_step(
        monkeypatch, mark_recovery=True
    )
    # Recovery-only steps must not be routed through parallel verification (which would apply its own
    # unconditional max-step failure); they take the guarded path and create the next step.
    parallel.assert_not_awaited()
    assert task_result is None
    assert created_next is next_step_sentinel


@pytest.mark.asyncio
async def test_non_recovery_navigation_step_still_uses_parallel_verification(monkeypatch: pytest.MonkeyPatch) -> None:
    # Control: an ordinary navigation step with verification enabled still dispatches parallel
    # verification. Proves the recovery marker is what diverts, not the setup.
    _next, parallel, result = await _run_navigation_completed_step(monkeypatch, mark_recovery=False)
    parallel.assert_awaited_once()
    assert result is parallel.return_value


@pytest.mark.asyncio
async def test_recovery_step_does_not_complete_or_maxstep_fail_action_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    next_step_sentinel, (task_result, last_step, created_next) = await _run_handle_completed_step(
        monkeypatch, mark_recovery=True
    )
    # The block is neither completed (True) nor max-step-failed (False, last_step); a next step runs
    # so the block's real action still gets its chance.
    assert task_result is None
    assert last_step is None
    assert created_next is next_step_sentinel


@pytest.mark.asyncio
async def test_non_recovery_success_still_completes_action_block(monkeypatch: pytest.MonkeyPatch) -> None:
    # Control: without the recovery marker, the same successful step completes the ActionBlock via the
    # pre-existing single-step hack. Proves the guard is what discriminates, not the setup.
    _next, (task_result, _last_step, created_next) = await _run_handle_completed_step(monkeypatch, mark_recovery=False)
    assert task_result is True
    assert created_next is None


# ---- download-popup claim read ---------------------------------------------------------------


def test_has_download_popup_claim_is_identity_based() -> None:
    context = SkyvernContext(task_id="tsk", organization_id="o")
    claimed = MagicMock()
    other = MagicMock()
    context.record_download_popup_claim("tsk", claimed)

    assert context.has_download_popup_claim("tsk", claimed) is True
    assert context.has_download_popup_claim("tsk", other) is False
    assert context.has_download_popup_claim("other-task", claimed) is False


# ---- Case 8: cached-script generation skips the recovery marker ------------------------------


def test_generate_script_skips_internal_recovery_close_action() -> None:
    from skyvern.core.script_generations.generate_script import _build_block_fn

    recovery_action = {"action_type": "close_page", "is_internal_recovery": True}
    block = {"block_type": "navigation", "label": "b", "actions": [recovery_action]}

    # The recovery close has no ACTION_MAP entry; without the marker filter _action_to_stmt raises
    # KeyError and aborts whole-script generation. The filter must drop it so generation succeeds.
    fn = _build_block_fn(block, actions=[recovery_action])
    assert fn is not None
