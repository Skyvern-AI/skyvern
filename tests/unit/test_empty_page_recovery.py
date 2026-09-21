"""Empty-page recovery: close a dead blank working page and fall back to a survivor.

Covers the scraper blank predicate, the scrape-ladder blank-evidence preservation, the
handle_completed_step ActionBlock/max-step guards for a recovery-only step, the download-popup
claim read, and the cached-script generation marker filter.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyvern.exceptions import FailedToReloadPage, ScrapingFailedBlankPage
from skyvern.forge.agent import EMPTY_PAGE_RECOVERY_MAX_ATTEMPTS, ForgeAgent
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


_AGENT = "skyvern.forge.agent"


def _task_block(*, complete_on_download: bool = True, download_timeout: float | None = None) -> MagicMock:
    block = MagicMock()
    block.complete_on_download = complete_on_download
    block.download_timeout = download_timeout
    return block


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


def _recovery_state(dead: MagicMock, survivor: MagicMock) -> MagicMock:
    state = MagicMock()
    state.get_working_page = AsyncMock(return_value=dead)
    state.list_valid_pages = AsyncMock(return_value=[dead, survivor])
    return state


# ---- bounded claimed-dead-blank recovery (deadline anchor) -----------------------------------


def test_recovery_grace_is_per_exact_page_with_independent_budgets() -> None:
    """Each exact Page anchors its OWN recovery-grace start (at its first recovery observation), so a
    sibling anchored later gets an independent remaining budget; releasing one leaves the other intact."""
    ctx = SkyvernContext(task_id="t")
    page_a = MagicMock()
    page_b = MagicMock()
    ctx.record_download_popup_claim("t", page_a)
    ctx.record_download_popup_late_candidate("t", page_b)
    ctx.anchor_download_popup_recovery_grace("t", page_a, 0.0)  # A's recovery grace starts at t=0
    ctx.anchor_download_popup_recovery_grace("t", page_b, 1000.0)  # B's independently at t=1000
    # At t=1000, A is fully aged for a 60s grace; B still has the full 60s -> independent budgets.
    assert ctx.remaining_download_popup_grace("t", page_a, 60.0, 1000.0) == 0.0
    assert ctx.remaining_download_popup_grace("t", page_b, 60.0, 1000.0) == 60.0
    ctx.retire_intentional_page("t", page_a)  # releasing A must not touch B's anchor
    assert ctx.remaining_download_popup_grace("t", page_a, 60.0, 1000.0) is None
    assert ctx.remaining_download_popup_grace("t", page_b, 60.0, 1000.0) == 60.0


def test_download_popup_claim_baseline_is_per_exact_page_and_lifecycle_bound() -> None:
    """Each exact Page anchors its OWN baseline snapshot (first-creation wins); a sibling gets an
    independent snapshot; a page is only dropped once it leaves BOTH registries."""
    ctx = SkyvernContext(task_id="t")
    page_a = MagicMock()
    page_b = MagicMock()
    ctx.record_download_popup_claim("t", page_a, baseline_files=["/d/a1.pdf"])
    ctx.record_download_popup_claim("t", page_a, baseline_files=["/d/a2.pdf"])  # re-record must not reset
    ctx.record_download_popup_late_candidate("t", page_b, baseline_files=["/d/a1.pdf", "/d/b.pdf"])
    assert ctx.download_popup_claim_baseline_for("t", page_a) == ("/d/a1.pdf",)  # first snapshot wins
    assert ctx.download_popup_claim_baseline_for("t", page_b) == ("/d/a1.pdf", "/d/b.pdf")  # sibling independent
    # Releasing A drops only A's baseline; B (still a late candidate) keeps its own.
    ctx.release_download_popup_claim("t", page_a)
    assert ctx.download_popup_claim_baseline_for("t", page_a) is None
    assert ctx.download_popup_claim_baseline_for("t", page_b) == ("/d/a1.pdf", "/d/b.pdf")
    # B leaving its last registry drops its baseline too.
    ctx.discard_download_popup_late_candidate("t", page_b)
    assert ctx.download_popup_claim_baseline_for("t", page_b) is None
    assert ctx.download_popup_claim_baseline == {}


def test_release_download_popup_claim_frees_exact_page_and_preserves_intentional_semantics() -> None:
    """The narrow release helper drops one exact page from both registries; ``retire_intentional_page``
    delegates to it so NEW_TAB/magic-link protection is unchanged (sibling and other pages survive)."""
    ctx = SkyvernContext(task_id="t")
    expired = MagicMock()
    sibling = MagicMock()
    intentional = MagicMock()
    ctx.record_download_popup_claim("t", expired, baseline_files=["/d/x.pdf"])
    ctx.record_download_popup_claim("t", sibling, baseline_files=["/d/y.pdf"])
    ctx.record_download_popup_late_candidate("t", intentional)
    ctx.release_download_popup_claim("t", expired)
    assert ctx.has_download_popup_claim("t", expired) is False
    assert ctx.has_download_popup_claim("t", sibling) is True
    # retire_intentional_page still retires a deliberate tab from both registries (unchanged behavior).
    ctx.retire_intentional_page("t", intentional)
    assert ctx.has_download_popup_claim("t", intentional) is False
    assert ctx.has_download_popup_claim("t", sibling) is True


def test_action_delta_sweep_anchors_page_baseline_not_baseline_less() -> None:
    """Finding 1: the action-finally delta sweep must anchor each swept popup's own download baseline --
    a claim-time local run-dir read unioned with the caller's pre-fetched session snapshot -- not create a
    baseline-less claim that falls back to the stale block baseline. With an earlier file in that baseline,
    the swept popup captures it so recovery cannot miscredit that pre-existing file to the popup."""
    from skyvern.webeye.actions.handler import _PageDeltaBaseline, _record_action_owned_popup_delta

    local_file = "/d/earlier.pdf"
    session_file = "s3://bucket/earlier-session.pdf"
    ctx = SkyvernContext(task_id="t")
    browser_context = MagicMock()
    initiating = MagicMock()
    initiating.context = browser_context
    pre_existing = MagicMock()
    new_popup = MagicMock()
    browser_context.pages = [initiating, pre_existing, new_popup]  # new_popup added after the baseline
    baseline = _PageDeltaBaseline(browser_context=browser_context, pages=[initiating, pre_existing])

    with patch("skyvern.webeye.actions.handler.list_files_in_directory", return_value=[local_file]):
        outcome = _record_action_owned_popup_delta(
            ctx,
            task_id="t",
            initiating_page=initiating,
            baseline=baseline,
            download_dir=Path("/d"),
            attempt_started_at=None,
            session_baseline_files=[session_file],
        )

    assert outcome.claimed == 1 and outcome.pages == [new_popup]
    assert ctx.download_popup_claim_baseline_for("t", new_popup) == (local_file, session_file)


def test_retain_prunes_baseline_for_dropped_page_not_just_created_at() -> None:
    """Finding 2: the retain/pending-release seam (_prune_claim_deadlines) must prune the baseline
    snapshot for a page dropped from both registries, not only its creation anchor -- otherwise a stale
    baseline lingers (first-wins + id() reuse) and could attach to a new Page in the miscredit direction."""
    ctx = SkyvernContext(task_id="t")
    kept = MagicMock()
    dropped = MagicMock()
    ctx.record_download_popup_claim("t", kept, baseline_files=["/d/kept.pdf"])
    ctx.record_download_popup_claim("t", dropped, baseline_files=["/d/dropped.pdf"])
    ctx.anchor_download_popup_recovery_grace("t", kept, 0.0)
    ctx.anchor_download_popup_recovery_grace("t", dropped, 0.0)
    # Retain only `kept`: `dropped` leaves both registries at this seam and must be fully pruned.
    ctx.retain_download_popup_reservations("t", ((kept,), ()))
    assert ctx.download_popup_claim_baseline_for("t", kept) == ("/d/kept.pdf",)
    assert ctx.download_popup_claim_baseline_for("t", dropped) is None
    assert ctx.download_popup_recovery_grace_started_at.get("t", {}).get(id(dropped)) is None


def test_recovery_grace_anchor_is_first_wins_and_clamps() -> None:
    ctx = SkyvernContext(task_id="t")
    page = MagicMock()
    ctx.record_download_popup_claim("t", page)
    ctx.anchor_download_popup_recovery_grace("t", page, 0.0)
    ctx.anchor_download_popup_recovery_grace("t", page, 5.0)  # re-entry/cancellation must not reset
    assert ctx.remaining_download_popup_grace("t", page, 30.0, 0.0) == 30.0
    assert ctx.remaining_download_popup_grace("t", page, 30.0, 100.0) == 0.0  # aged clamps at 0
    assert ctx.remaining_download_popup_grace("t", MagicMock(), 30.0, 0.0) is None  # no anchor


def test_recovery_grace_is_not_anchored_at_claim_mint() -> None:
    """Round-22 (Blocker 1): the handler's post-click wait owns the pre-recovery phase, so recording a
    claim (event-recorded OR finally-swept) must NOT start the recovery grace. It is anchored first-wins at
    the first recovery observation -> a full grace regardless of when the claim was minted; a later
    observation consumes the remaining budget and never restarts."""
    ctx = SkyvernContext(task_id="t")
    page = MagicMock()
    ctx.record_download_popup_claim("t", page)
    ctx.record_download_popup_late_candidate("t", page)
    # Recording alone does not start the recovery grace.
    assert ctx.remaining_download_popup_grace("t", page, 60.0, 500.0) is None
    # First recovery observation starts it -> full grace no matter how long ago the claim was minted.
    ctx.anchor_download_popup_recovery_grace("t", page, 500.0)
    assert ctx.remaining_download_popup_grace("t", page, 60.0, 500.0) == 60.0
    # A later recovery observation consumes remaining and never restarts (re-entry/cancellation safe).
    ctx.anchor_download_popup_recovery_grace("t", page, 540.0)
    assert ctx.remaining_download_popup_grace("t", page, 60.0, 540.0) == 20.0


@pytest.mark.asyncio
async def test_first_recovery_grants_full_grace_after_handler_wait() -> None:
    """Round-22 (Blocker 1): even after the handler's own post-click wait has fully elapsed between claim
    mint and recovery, the FIRST blank-page recovery observation must receive ~one full recovery grace, not
    ~0. Proven by capturing the bounded observer's ``remaining``."""
    now = datetime.now(UTC)
    task = make_task(now, make_organization(now), task_id="t", workflow_run_id="wr_x")
    step = make_step(now, task, step_id="s", status=StepStatus.running, order=0, output=None)
    ctx = SkyvernContext(task_id=task.task_id)
    dead = _page("about:blank")
    survivor = _page("https://example.test/")
    survivor.is_closed.return_value = False
    captured: dict[str, float] = {}

    async def _capture(*_a: object, remaining: float, **_k: object) -> bool:
        captured["remaining"] = remaining
        return False

    # The claim is minted well before recovery (the handler wait then burns its own budget). Recovery
    # observes at monotonic t=100 (>> the 45s grace). Anchoring at claim mint would leave < 45s here;
    # anchoring first-wins at THIS observation leaves the full 45s.
    ctx.record_download_popup_claim(task.task_id, dead, baseline_files=[], session_observed=True)
    skyvern_context.set(ctx)
    try:
        agent = ForgeAgent()
        with (
            patch(f"{_AGENT}.time.monotonic", return_value=100.0),
            patch(_OBSERVE, new=AsyncMock(side_effect=_capture)),
        ):
            await agent._empty_page_recovery_plan(
                task,
                step,
                _no_credit_state(dead, [dead, survivor]),
                task_block=_task_block(complete_on_download=True, download_timeout=45.0),
                attempt_started_at=None,
                list_files_before=[],
            )
        assert captured["remaining"] == 45.0  # full grace at first recovery, not ~0
    finally:
        skyvern_context.reset()


def test_released_page_not_resurrected_by_pending_release_retention() -> None:
    """Amendment 5 / non-resurrection: once the exact page is released, applying a stale pending-release
    snapshot that still names it cannot bring the page or its anchor back."""
    ctx = SkyvernContext(task_id="t")
    dead = MagicMock()
    sibling = MagicMock()
    ctx.record_download_popup_claim("t", dead)
    ctx.record_download_popup_claim("t", sibling)
    ctx.anchor_download_popup_recovery_grace("t", dead, 0.0)
    ctx.stash_pending_download_reservation_release("t", ((dead, sibling), ()))  # snapshot includes dead
    ctx.retire_intentional_page("t", dead)  # recovery releases the exact page
    ctx.apply_pending_download_reservation_release("t")  # retention only shrinks; must not re-add dead
    assert ctx.has_download_popup_claim("t", dead) is False
    assert ctx.remaining_download_popup_grace("t", dead, 10.0, time.monotonic()) is None
    assert ctx.has_download_popup_claim("t", sibling) is True


def test_recovery_grace_anchor_dropped_only_when_page_leaves_both_registries() -> None:
    ctx = SkyvernContext(task_id="t")
    page = MagicMock()
    ctx.record_download_popup_claim("t", page)
    ctx.record_download_popup_late_candidate("t", page)
    ctx.anchor_download_popup_recovery_grace("t", page, 0.0)
    ctx.discard_download_popup_claim("t", page)  # still a late candidate -> anchor survives
    assert ctx.remaining_download_popup_grace("t", page, 10.0, time.monotonic()) is not None
    ctx.discard_download_popup_late_candidate("t", page)  # gone from both -> anchor dropped
    assert ctx.remaining_download_popup_grace("t", page, 10.0, time.monotonic()) is None
    # clear / detach also drop anchors
    other = MagicMock()
    ctx.record_download_popup_claim("t", other)
    ctx.anchor_download_popup_recovery_grace("t", other, 0.0)
    ctx.clear_download_popup_claims("t")
    assert ctx.download_popup_recovery_grace_started_at == {}


@pytest.mark.asyncio
async def test_observe_returns_true_on_direct_final_file(tmp_path: Path) -> None:
    """Amendment 2/6a: a silent claim whose download materializes as a FINAL file with no
    ``.crdownload`` is observed as credited (read-only)."""
    task = make_task(datetime.now(UTC), make_organization(datetime.now(UTC)), task_id="t", workflow_run_id="wr_x")
    agent = ForgeAgent()
    before = [str(tmp_path / "old.pdf")]
    after = before + [str(tmp_path / "new.pdf")]
    with (
        patch(f"{_AGENT}.resolve_run_download_id", return_value="wr_x"),
        patch(f"{_AGENT}.get_path_for_workflow_download_directory", return_value=tmp_path),
        patch(f"{_AGENT}.list_downloading_files_in_directory", return_value=[]),
        patch(f"{_AGENT}.list_files_in_directory", return_value=after),
        patch(f"{_AGENT}.skyvern_context.current", return_value=SkyvernContext(task_id="t")),
    ):
        credited = await agent._observe_claimed_download_within_grace(
            task, _task_block(), remaining=5.0, list_files_before=before, attempt_started_at=None
        )
    assert credited is True


@pytest.mark.asyncio
async def test_observe_returns_false_when_nothing_lands(tmp_path: Path) -> None:
    task = make_task(datetime.now(UTC), make_organization(datetime.now(UTC)), task_id="t", workflow_run_id="wr_x")
    agent = ForgeAgent()
    before = [str(tmp_path / "old.pdf")]
    with (
        patch(f"{_AGENT}.resolve_run_download_id", return_value="wr_x"),
        patch(f"{_AGENT}.get_path_for_workflow_download_directory", return_value=tmp_path),
        patch(f"{_AGENT}.list_downloading_files_in_directory", return_value=[]),
        patch(f"{_AGENT}.list_files_in_directory", return_value=before),
        patch(f"{_AGENT}.skyvern_context.current", return_value=SkyvernContext(task_id="t")),
    ):
        credited = await agent._observe_claimed_download_within_grace(
            task, _task_block(), remaining=0.05, list_files_before=before, attempt_started_at=None
        )
    assert credited is False


@pytest.mark.asyncio
async def test_observe_bounds_session_read_by_remaining_grace(tmp_path: Path) -> None:
    """Finding A (P1): the session-storage reads in the grace observer must be bounded by the remaining
    grace, so a STALLED remote read cannot hold the step past the download deadline. A slow read yields no
    credit and the observer returns within the (tiny) budget rather than blocking on the storage call."""
    task = make_task(
        datetime.now(UTC),
        make_organization(datetime.now(UTC)),
        task_id="t",
        workflow_run_id="wr_x",
        browser_session_id="pbs_x",
    )
    agent = ForgeAgent()

    async def _stalled_session_read(**_: object) -> list[str]:
        await asyncio.sleep(5)  # storage stall far exceeding the grace; bounded read must cut it off
        return ["s3://bucket/late.pdf"]

    with (
        patch(f"{_AGENT}.resolve_run_download_id", return_value="wr_x"),
        patch(f"{_AGENT}.get_path_for_workflow_download_directory", return_value=tmp_path),
        patch(f"{_AGENT}.list_downloading_files_in_directory", return_value=[]),
        patch(f"{_AGENT}.list_files_in_directory", return_value=[]),
        patch(f"{_AGENT}.skyvern_context.current", return_value=SkyvernContext(task_id="t")),
        patch(f"{_AGENT}.app") as mock_app,
    ):
        mock_app.STORAGE.list_downloaded_files_in_browser_session = AsyncMock(side_effect=_stalled_session_read)
        mock_app.STORAGE.list_downloading_files_in_browser_session = AsyncMock(side_effect=_stalled_session_read)
        started = time.monotonic()
        credited = await agent._observe_claimed_download_within_grace(
            task, _task_block(), remaining=0.05, list_files_before=[], attempt_started_at=None
        )
        elapsed = time.monotonic() - started

    assert credited is False  # a stalled read is never credited
    assert elapsed < 2.0, f"observer held the step {elapsed:.2f}s past the grace on a stalled session read"


@pytest.mark.asyncio
async def test_observe_bounds_nested_inflight_session_listing(tmp_path: Path) -> None:
    """Finding A (P1, round 16): the IN-FLIGHT branch calls _wait_for_in_flight_downloads, whose OWN
    browser-session listing runs before its internal timeout cap. A stall there must not hold the step past
    the grace either. This drives the in-flight branch (local .crdownload present) with a stalled nested
    session listing and proves the observer still returns within the budget -- the completed-file read is
    fast here, unlike ``test_observe_bounds_session_read_by_remaining_grace`` which never reaches this branch."""
    task = make_task(
        datetime.now(UTC),
        make_organization(datetime.now(UTC)),
        task_id="t",
        workflow_run_id="wr_x",
        browser_session_id="pbs_x",
    )
    agent = ForgeAgent()

    async def _stalled_session_read(**_: object) -> list[str]:
        await asyncio.sleep(5)  # nested in-flight listing stall far exceeding the grace
        return ["s3://bucket/late.pdf.crdownload"]

    with (
        patch(f"{_AGENT}.resolve_run_download_id", return_value="wr_x"),
        patch(f"{_AGENT}.get_path_for_workflow_download_directory", return_value=tmp_path),
        # A local in-flight file drives the in-flight branch without any session read in _in_flight_present.
        patch(f"{_AGENT}.list_downloading_files_in_directory", return_value=[str(tmp_path / "foo.crdownload")]),
        patch(f"{_AGENT}.list_files_in_directory", return_value=[]),
        patch(f"{_AGENT}.wait_for_download_finished", new=AsyncMock(return_value=None)),
        patch(f"{_AGENT}.skyvern_context.current", return_value=SkyvernContext(task_id="t")),
        patch(f"{_AGENT}.app") as mock_app,
    ):
        # The completed-file read is fast (empty) so the budget survives to the in-flight branch; only the
        # nested in-flight session listing stalls.
        mock_app.STORAGE.list_downloaded_files_in_browser_session = AsyncMock(return_value=[])
        mock_app.STORAGE.list_downloading_files_in_browser_session = AsyncMock(side_effect=_stalled_session_read)
        started = time.monotonic()
        credited = await agent._observe_claimed_download_within_grace(
            task, _task_block(), remaining=0.05, list_files_before=[], attempt_started_at=None
        )
        elapsed = time.monotonic() - started

    assert credited is False
    assert mock_app.STORAGE.list_downloading_files_in_browser_session.await_count >= 1  # the branch was reached
    assert elapsed < 2.0, f"observer held {elapsed:.2f}s past the grace on a stalled nested in-flight listing"


@pytest.mark.asyncio
async def test_observe_skips_session_read_when_grace_exhausted() -> None:
    """Finding B: pin the documented 'exhausted budget skips the read entirely' contract -- with no
    remaining grace the observer must NOT issue any remote session listing (a doomed remote call), rather
    than a floored-budget read. Proves the early-out is intentional, not incidental."""
    task = make_task(
        datetime.now(UTC),
        make_organization(datetime.now(UTC)),
        task_id="t",
        workflow_run_id="wr_x",
        browser_session_id="pbs_x",
    )
    agent = ForgeAgent()
    with (
        patch(f"{_AGENT}.resolve_run_download_id", return_value="wr_x"),
        patch(f"{_AGENT}.get_path_for_workflow_download_directory", return_value=Path("/tmp")),
        patch(f"{_AGENT}.list_downloading_files_in_directory", return_value=[]),
        patch(f"{_AGENT}.list_files_in_directory", return_value=[]),
        patch(f"{_AGENT}.skyvern_context.current", return_value=SkyvernContext(task_id="t")),
        patch(f"{_AGENT}.app") as mock_app,
    ):
        mock_app.STORAGE.list_downloaded_files_in_browser_session = AsyncMock(return_value=[])
        mock_app.STORAGE.list_downloading_files_in_browser_session = AsyncMock(return_value=[])
        credited = await agent._observe_claimed_download_within_grace(
            task, _task_block(), remaining=0.0, list_files_before=[], attempt_started_at=None
        )
    assert credited is False
    mock_app.STORAGE.list_downloaded_files_in_browser_session.assert_not_awaited()
    mock_app.STORAGE.list_downloading_files_in_browser_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_session_listing_error_during_grace_waits_then_releases_and_recovers(tmp_path: Path) -> None:
    """Round-17 B: a NON-timeout session-listing failure during the dead-blank grace must be treated as an
    empty observation, not propagated. The observer must still wait out the remaining grace and return no
    credit, so _empty_page_recovery_plan releases the exact claim and recovers -- rather than the error
    escaping to the broad catch and re-raising the original blank-page failure."""
    now = datetime.now(UTC)
    task = make_task(now, make_organization(now), task_id="t", workflow_run_id="wr_x", browser_session_id="pbs_x")
    step = make_step(now, task, step_id="s", status=StepStatus.running, order=0, output=None)
    ctx = SkyvernContext(task_id=task.task_id)
    dead = _page("about:blank")
    survivor = _page("https://example.test/")
    survivor.is_closed.return_value = False
    ctx.record_download_popup_claim(task.task_id, dead, baseline_files=[], session_observed=True)
    skyvern_context.set(ctx)
    try:
        agent = ForgeAgent()
        with (
            patch(f"{_AGENT}.resolve_run_download_id", return_value="wr_x"),
            patch(f"{_AGENT}.get_path_for_workflow_download_directory", return_value=tmp_path),
            patch(f"{_AGENT}.list_downloading_files_in_directory", return_value=[]),
            patch(f"{_AGENT}.list_files_in_directory", return_value=[]),
            patch(f"{_AGENT}.app") as agent_app,
        ):
            agent_app.STORAGE.list_downloaded_files_in_browser_session = AsyncMock(
                side_effect=RuntimeError("transient session storage failure")
            )
            agent_app.STORAGE.list_downloading_files_in_browser_session = AsyncMock(
                side_effect=RuntimeError("transient session storage failure")
            )
            result = await agent._empty_page_recovery_plan(
                task,
                step,
                _recovery_state(dead, survivor),
                task_block=_task_block(complete_on_download=True, download_timeout=0.05),
                attempt_started_at=None,
                list_files_before=[],
            )
        # Listing error -> empty observation -> no credit -> release the exact claim + recover (not re-raise).
        assert result is not None and len(result) == 1 and result[0].is_internal_recovery is True
        assert ctx.has_download_popup_claim(task.task_id, dead) is False
    finally:
        skyvern_context.reset()


@pytest.mark.asyncio
async def test_nested_inflight_waiter_error_during_grace_waits_then_releases_and_recovers(tmp_path: Path) -> None:
    """Round-18 B: the in-flight branch calls _wait_for_in_flight_downloads, whose OWN session listing can
    raise a NON-timeout error. That must be contained as no settle for the iteration -- the observer keeps
    waiting to grace expiry and returns no credit, so _empty_page_recovery_plan releases the exact claim and
    recovers -- rather than the error escaping to the broad catch and re-raising the original blank failure."""
    now = datetime.now(UTC)
    task = make_task(now, make_organization(now), task_id="t", workflow_run_id="wr_x", browser_session_id="pbs_x")
    step = make_step(now, task, step_id="s", status=StepStatus.running, order=0, output=None)
    ctx = SkyvernContext(task_id=task.task_id)
    dead = _page("about:blank")
    survivor = _page("https://example.test/")
    survivor.is_closed.return_value = False
    ctx.record_download_popup_claim(task.task_id, dead, baseline_files=[], session_observed=True)
    skyvern_context.set(ctx)
    waiter = AsyncMock(side_effect=RuntimeError("transient session storage failure in the nested waiter"))
    try:
        agent = ForgeAgent()
        with (
            patch(f"{_AGENT}.resolve_run_download_id", return_value="wr_x"),
            patch(f"{_AGENT}.get_path_for_workflow_download_directory", return_value=tmp_path),
            # A local in-flight file drives the in-flight branch, so the nested waiter is reached; the
            # completed-file reads stay empty so nothing is ever credited.
            patch(f"{_AGENT}.list_downloading_files_in_directory", return_value=[str(tmp_path / "foo.crdownload")]),
            patch(f"{_AGENT}.list_files_in_directory", return_value=[]),
            patch(f"{_AGENT}.ForgeAgent._wait_for_in_flight_downloads", new=waiter),
            patch(f"{_AGENT}.app") as agent_app,
        ):
            agent_app.STORAGE.list_downloaded_files_in_browser_session = AsyncMock(return_value=[])
            agent_app.STORAGE.list_downloading_files_in_browser_session = AsyncMock(return_value=[])
            result = await agent._empty_page_recovery_plan(
                task,
                step,
                _recovery_state(dead, survivor),
                task_block=_task_block(complete_on_download=True, download_timeout=0.05),
                attempt_started_at=None,
                list_files_before=[],
            )
        # Nested waiter error -> contained -> no credit -> release the exact claim + recover (not re-raise).
        assert waiter.await_count >= 1  # the in-flight branch (and the failing nested waiter) was reached
        assert result is not None and len(result) == 1 and result[0].is_internal_recovery is True
        assert ctx.has_download_popup_claim(task.task_id, dead) is False
    finally:
        skyvern_context.reset()


@pytest.mark.asyncio
async def test_sibling_delta_popup_not_credited_by_sibling_file_and_settles_own_download(tmp_path: Path) -> None:
    """Round-19: two blank popups claimed together in one delta sweep are same-action siblings with no
    file->page mapping. In a NON-complete block, sibling #2's dead-blank recovery must NOT early-credit and
    close on sibling #1's completed file (which landed after the shared snapshot); it must keep observing so
    its OWN in-flight download can settle first, rather than being cancelled by an early close."""
    now = datetime.now(UTC)
    task = make_task(now, make_organization(now), task_id="t", workflow_run_id="wr_x")
    step = make_step(now, task, step_id="s", status=StepStatus.running, order=0, output=None)
    ctx = SkyvernContext(task_id=task.task_id)
    dead2 = _page("about:blank")  # the sibling under observation (its own download is still in flight)
    sibling1 = _page("about:blank")
    survivor = _page("https://example.test/")
    survivor.is_closed.return_value = False
    # Both claimed together -> marked as sibling-ambiguous. dead2's baseline is empty: sibling #1's file
    # settled only AFTER the shared sweep snapshot, so it is "new" against dead2's baseline.
    ctx.record_download_popup_claim(task.task_id, sibling1, baseline_files=[])
    ctx.record_download_popup_claim(task.task_id, dead2, baseline_files=[])
    ctx.mark_download_popup_claim_delta_siblings(task.task_id, [sibling1, dead2])
    skyvern_context.set(ctx)
    waiter = AsyncMock(return_value=None)
    try:
        agent = ForgeAgent()
        with (
            patch(f"{_AGENT}.resolve_run_download_id", return_value="wr_x"),
            patch(f"{_AGENT}.get_path_for_workflow_download_directory", return_value=tmp_path),
            # sibling #1's COMPLETED file is present (new vs dead2's empty baseline); dead2's OWN download
            # is still an in-flight partial, which drives the in-flight settle branch.
            patch(f"{_AGENT}.list_files_in_directory", return_value=[str(tmp_path / "sibling1.pdf")]),
            patch(
                f"{_AGENT}.list_downloading_files_in_directory",
                return_value=[str(tmp_path / "dead2-own.crdownload")],
            ),
            patch(f"{_AGENT}.ForgeAgent._wait_for_in_flight_downloads", new=waiter),
        ):
            result = await agent._empty_page_recovery_plan(
                task,
                step,
                _recovery_state(dead2, survivor),
                task_block=_task_block(complete_on_download=False, download_timeout=0.05),
                attempt_started_at=None,
                list_files_before=[],
            )
        # The sibling's file did NOT short-circuit dead2's grace: the in-flight settle branch was reached,
        # giving dead2's own download its full budget before recovery closes the dead blank.
        assert waiter.await_count >= 1
        assert result is not None and len(result) == 1 and result[0].is_internal_recovery is True
    finally:
        skyvern_context.reset()


@pytest.mark.asyncio
async def test_lone_delta_popup_keeps_fast_new_file_credit(tmp_path: Path) -> None:
    """Round-19 non-regression: the sibling suppression must be scoped to multi-popup sweeps. A LONE
    claimed popup (not marked as a sibling) owns a new completed file unambiguously (round-14), so its
    recovery still credits early on that file and never needs the in-flight settle branch."""
    now = datetime.now(UTC)
    task = make_task(now, make_organization(now), task_id="t", workflow_run_id="wr_x")
    step = make_step(now, task, step_id="s", status=StepStatus.running, order=0, output=None)
    ctx = SkyvernContext(task_id=task.task_id)
    dead = _page("about:blank")
    survivor = _page("https://example.test/")
    survivor.is_closed.return_value = False
    ctx.record_download_popup_claim(task.task_id, dead, baseline_files=[])  # lone claim, never marked
    skyvern_context.set(ctx)
    waiter = AsyncMock(return_value=None)
    try:
        agent = ForgeAgent()
        with (
            patch(f"{_AGENT}.resolve_run_download_id", return_value="wr_x"),
            patch(f"{_AGENT}.get_path_for_workflow_download_directory", return_value=tmp_path),
            patch(f"{_AGENT}.list_files_in_directory", return_value=[str(tmp_path / "own.pdf")]),
            patch(f"{_AGENT}.list_downloading_files_in_directory", return_value=[]),
            patch(f"{_AGENT}.ForgeAgent._wait_for_in_flight_downloads", new=waiter),
        ):
            result = await agent._empty_page_recovery_plan(
                task,
                step,
                _recovery_state(dead, survivor),
                task_block=_task_block(complete_on_download=False, download_timeout=0.05),
                attempt_started_at=None,
                list_files_before=[],
            )
        # Credited early on its own new file: the in-flight settle branch was never needed.
        assert waiter.await_count == 0
        assert result is not None and len(result) == 1 and result[0].is_internal_recovery is True
    finally:
        skyvern_context.reset()


_OBSERVE = f"{_AGENT}.ForgeAgent._observe_claimed_download_within_grace"


def _no_credit_state(working: MagicMock, valid_pages: list[MagicMock]) -> MagicMock:
    state = MagicMock()
    state.get_working_page = AsyncMock(return_value=working)
    state.list_valid_pages = AsyncMock(return_value=valid_pages)
    return state


@pytest.mark.asyncio
async def test_recovery_no_survivor_keeps_claim_and_consumes_no_attempt() -> None:
    """Round-21: grace expires with no credit and NO http survivor -> return None with the exact claim,
    its baseline, and its creation anchor all intact (so terminal cleanup can still settle a late
    download under the original claim), and no recovery attempt consumed. The release must not precede
    the survivor check."""
    now = datetime.now(UTC)
    task = make_task(now, make_organization(now), task_id="t", workflow_run_id="wr_x")
    step = make_step(now, task, step_id="s", status=StepStatus.running, order=0, output=None)
    ctx = SkyvernContext(task_id=task.task_id)
    dead = _page("about:blank")
    ctx.record_download_popup_claim(task.task_id, dead, baseline_files=["/d/x.pdf"], session_observed=True)
    skyvern_context.set(ctx)
    try:
        agent = ForgeAgent()
        with patch(_OBSERVE, new=AsyncMock(return_value=False)):
            result = await agent._empty_page_recovery_plan(
                task,
                step,
                _no_credit_state(dead, [dead]),  # only the dead blank; no survivor
                task_block=_task_block(complete_on_download=True, download_timeout=0.01),
                attempt_started_at=None,
                list_files_before=[],
            )
        assert result is None
        assert ctx.has_download_popup_claim(task.task_id, dead) is True
        assert ctx.download_popup_claim_baseline_for(task.task_id, dead) is not None
        assert ctx.remaining_download_popup_grace(task.task_id, dead, 60.0, time.monotonic()) is not None
        assert ctx.empty_page_recovery_attempts.get(task.task_id, 0) == 0
    finally:
        skyvern_context.reset()


@pytest.mark.asyncio
async def test_recovery_cap_reached_keeps_claim_and_consumes_no_attempt() -> None:
    """Round-21: grace expires with no credit and the attempt cap is already reached -> return None with
    the exact claim intact and no additional attempt consumed. The release must not precede the cap
    check."""
    now = datetime.now(UTC)
    task = make_task(now, make_organization(now), task_id="t", workflow_run_id="wr_x")
    step = make_step(now, task, step_id="s", status=StepStatus.running, order=0, output=None)
    ctx = SkyvernContext(task_id=task.task_id)
    dead = _page("about:blank")
    survivor = _page("https://example.test/")
    survivor.is_closed.return_value = False
    ctx.record_download_popup_claim(task.task_id, dead, baseline_files=["/d/x.pdf"], session_observed=True)
    ctx.empty_page_recovery_attempts[task.task_id] = EMPTY_PAGE_RECOVERY_MAX_ATTEMPTS
    skyvern_context.set(ctx)
    try:
        agent = ForgeAgent()
        with patch(_OBSERVE, new=AsyncMock(return_value=False)):
            result = await agent._empty_page_recovery_plan(
                task,
                step,
                _no_credit_state(dead, [dead, survivor]),  # survivor present, but cap is exhausted
                task_block=_task_block(complete_on_download=True, download_timeout=0.01),
                attempt_started_at=None,
                list_files_before=[],
            )
        assert result is None
        assert ctx.has_download_popup_claim(task.task_id, dead) is True
        assert ctx.download_popup_claim_baseline_for(task.task_id, dead) is not None
        assert ctx.empty_page_recovery_attempts[task.task_id] == EMPTY_PAGE_RECOVERY_MAX_ATTEMPTS
    finally:
        skyvern_context.reset()


@pytest.mark.parametrize("mutation", ["page_replaced", "claim_adopted"])
@pytest.mark.asyncio
async def test_recovery_toctou_during_grace_fails_closed_with_claim_intact(mutation: str) -> None:
    """Round-21 TOCTOU: the grace wait is a window in which the working page can be replaced or the exact
    claim adopted. Revalidation before mutation must fail closed -> return None, no recovery attempt
    consumed, and (for the replaced case) the ORIGINAL exact claim intact -- recovery must never close a
    page other than the one it observed, nor recover a page whose ownership was handed off."""
    now = datetime.now(UTC)
    task = make_task(now, make_organization(now), task_id="t", workflow_run_id="wr_x")
    step = make_step(now, task, step_id="s", status=StepStatus.running, order=0, output=None)
    ctx = SkyvernContext(task_id=task.task_id)
    dead = _page("about:blank")
    survivor = _page("https://example.test/")
    survivor.is_closed.return_value = False
    ctx.record_download_popup_claim(task.task_id, dead, baseline_files=["/d/x.pdf"], session_observed=True)
    skyvern_context.set(ctx)
    state = MagicMock()
    observe: AsyncMock
    if mutation == "page_replaced":
        replacement = _page("https://example.test/replacement")
        replacement.is_closed.return_value = False
        state.get_working_page = AsyncMock(side_effect=[dead, replacement])  # replaced during the wait
        state.list_valid_pages = AsyncMock(return_value=[replacement, survivor])
        observe = AsyncMock(return_value=False)
    else:  # claim adopted (released) mid-wait

        async def _observe_then_adopt(*_: object, **__: object) -> bool:
            ctx.retire_intentional_page(task.task_id, dead)
            return False

        state.get_working_page = AsyncMock(return_value=dead)
        state.list_valid_pages = AsyncMock(return_value=[dead, survivor])
        observe = AsyncMock(side_effect=_observe_then_adopt)
    try:
        agent = ForgeAgent()
        with patch(_OBSERVE, new=observe):
            result = await agent._empty_page_recovery_plan(
                task,
                step,
                state,
                task_block=_task_block(complete_on_download=True, download_timeout=0.01),
                attempt_started_at=None,
                list_files_before=[],
            )
        assert result is None
        assert ctx.empty_page_recovery_attempts.get(task.task_id, 0) == 0
        if mutation == "page_replaced":
            assert ctx.has_download_popup_claim(task.task_id, dead) is True
    finally:
        skyvern_context.reset()


@pytest.mark.asyncio
async def test_recovery_eligible_releases_only_exact_claim_and_consumes_one_attempt() -> None:
    """Round-21 non-regression: survivor present and cap available -> exactly one internal recovery
    action, one attempt consumed, ONLY the exact page's claim released, and a sibling claim preserved."""
    now = datetime.now(UTC)
    task = make_task(now, make_organization(now), task_id="t", workflow_run_id="wr_x")
    step = make_step(now, task, step_id="s", status=StepStatus.running, order=0, output=None)
    ctx = SkyvernContext(task_id=task.task_id)
    dead = _page("about:blank")
    sibling = _page("about:blank")
    survivor = _page("https://example.test/")
    survivor.is_closed.return_value = False
    ctx.record_download_popup_claim(task.task_id, dead, baseline_files=["/d/x.pdf"], session_observed=True)
    ctx.record_download_popup_claim(task.task_id, sibling, baseline_files=["/d/y.pdf"], session_observed=True)
    skyvern_context.set(ctx)
    try:
        agent = ForgeAgent()
        with patch(_OBSERVE, new=AsyncMock(return_value=False)):
            result = await agent._empty_page_recovery_plan(
                task,
                step,
                _no_credit_state(dead, [dead, survivor]),
                task_block=_task_block(complete_on_download=True, download_timeout=0.01),
                attempt_started_at=None,
                list_files_before=[],
            )
        assert result is not None and len(result) == 1 and result[0].is_internal_recovery is True
        assert ctx.has_download_popup_claim(task.task_id, dead) is False  # exact claim released
        assert ctx.has_download_popup_claim(task.task_id, sibling) is True  # sibling preserved
        assert ctx.empty_page_recovery_attempts[task.task_id] == 1
    finally:
        skyvern_context.reset()


@pytest.mark.asyncio
async def test_observe_returns_true_on_session_only_final_file(tmp_path: Path) -> None:
    """A completed download that lands ONLY in persistent browser-session storage (not the local run
    dir) must be observed as credited, mirroring the authoritative credit seam's local+session union."""
    task = make_task(
        datetime.now(UTC),
        make_organization(datetime.now(UTC)),
        task_id="t",
        workflow_run_id="wr_x",
        browser_session_id="pbs_x",
    )
    agent = ForgeAgent()
    before = [str(tmp_path / "old.pdf")]  # local baseline; session had no files before the step
    with (
        patch(f"{_AGENT}.resolve_run_download_id", return_value="wr_x"),
        patch(f"{_AGENT}.get_path_for_workflow_download_directory", return_value=tmp_path),
        patch(f"{_AGENT}.list_downloading_files_in_directory", return_value=[]),
        patch(f"{_AGENT}.list_files_in_directory", return_value=before),  # local run dir unchanged
        patch(f"{_AGENT}.skyvern_context.current", return_value=SkyvernContext(task_id="t")),
        patch(f"{_AGENT}.app") as mock_app,
    ):
        mock_app.STORAGE.list_downloaded_files_in_browser_session = AsyncMock(return_value=["s3://bucket/new.pdf"])
        mock_app.STORAGE.list_downloading_files_in_browser_session = AsyncMock(return_value=[])
        credited = await agent._observe_claimed_download_within_grace(
            task, _task_block(), remaining=5.0, list_files_before=before, attempt_started_at=None
        )
    assert credited is True


@pytest.mark.asyncio
async def test_inflight_crdownload_settling_during_grace_does_not_credit(tmp_path: Path) -> None:
    """Finding 2: an earlier download in-flight at claim time (``foo.pdf.crdownload`` in the baseline)
    settles to ``foo.pdf`` during the grace. Normalized identity comparison must collapse the two shapes
    so the settle is NOT read as a new arrival that would miscredit this claimed page."""
    task = make_task(datetime.now(UTC), make_organization(datetime.now(UTC)), task_id="t", workflow_run_id="wr_x")
    agent = ForgeAgent()
    baseline = [str(tmp_path / "foo.pdf.crdownload")]  # earlier download in-flight when the claim was created
    settled = [str(tmp_path / "foo.pdf")]  # same download, settled to its final name during the grace
    with (
        patch(f"{_AGENT}.resolve_run_download_id", return_value="wr_x"),
        patch(f"{_AGENT}.get_path_for_workflow_download_directory", return_value=tmp_path),
        patch(f"{_AGENT}.list_downloading_files_in_directory", return_value=[]),
        patch(f"{_AGENT}.list_files_in_directory", return_value=settled),
        patch(f"{_AGENT}.skyvern_context.current", return_value=SkyvernContext(task_id="t")),
    ):
        credited = await agent._observe_claimed_download_within_grace(
            task, _task_block(), remaining=0.05, list_files_before=baseline, attempt_started_at=None
        )
    assert credited is False


@pytest.mark.asyncio
async def test_session_only_file_present_at_claim_time_does_not_credit_but_later_arrival_does(
    tmp_path: Path,
) -> None:
    """Finding 1: per-claim baselines include the pre-click session snapshot (captured from the single
    authoritative pre-click listing). A session-only file present when the claim is created is in the
    baseline (must NOT credit); a session-only file that arrives AFTER the claim is new (must credit)."""
    now = datetime.now(UTC)
    task = make_task(now, make_organization(now), task_id="t", workflow_run_id="wr_x", browser_session_id="pbs_x")
    step = make_step(now, task, step_id="s", status=StepStatus.running, order=0, output=None)
    session_file = "s3://bucket/earlier-session.pdf"

    async def _run(claim_session_files: list[str], observed_session_files: list[str]):
        # Claim-time session baseline = the pre-click session snapshot the authoritative read captures.
        claim_baseline = list(claim_session_files)
        ctx = SkyvernContext(task_id="t")
        dead = _page("about:blank")
        survivor = _page("https://example.test/")
        survivor.is_closed.return_value = False
        # main-path claims are session-observed, so recovery unions session files for them
        ctx.record_download_popup_claim("t", dead, baseline_files=claim_baseline, session_observed=True)
        skyvern_context.set(ctx)
        try:
            agent = ForgeAgent()
            with (
                patch(f"{_AGENT}.resolve_run_download_id", return_value="wr_x"),
                patch(f"{_AGENT}.get_path_for_workflow_download_directory", return_value=tmp_path),
                patch(f"{_AGENT}.list_downloading_files_in_directory", return_value=[]),
                patch(f"{_AGENT}.list_files_in_directory", return_value=[]),
                patch(f"{_AGENT}.app") as agent_app,
            ):
                agent_app.STORAGE.list_downloaded_files_in_browser_session = AsyncMock(
                    return_value=observed_session_files
                )
                agent_app.STORAGE.list_downloading_files_in_browser_session = AsyncMock(return_value=[])
                return await agent._empty_page_recovery_plan(
                    task,
                    step,
                    _recovery_state(dead, survivor),
                    task_block=_task_block(complete_on_download=True, download_timeout=0.05),
                    attempt_started_at=None,
                    list_files_before=[],  # stale block baseline never contained the session file
                )
        finally:
            skyvern_context.reset()

    # Present at claim time -> in the union claim baseline -> NOT credited -> release + recover.
    present = await _run(claim_session_files=[session_file], observed_session_files=[session_file])
    assert present is not None and len(present) == 1 and present[0].is_internal_recovery is True

    # Arrived only after the claim -> not in the baseline -> credited -> complete-on-download short-circuit.
    after = await _run(claim_session_files=[], observed_session_files=[session_file])
    assert after == []


@pytest.mark.asyncio
async def test_false_click_claim_not_credited_by_session_only_file(tmp_path: Path) -> None:
    """Round-11: a claim WITHOUT a session baseline (the v4 false-click path, local-only, no remote
    listing) must NOT be credited by a session-only file that appeared after the block baseline. The
    observer has no session reference for such a claim, so it must not union session files -- otherwise an
    earlier action's session-only file reads as new and prematurely completes / wrongly releases."""
    now = datetime.now(UTC)
    task = make_task(now, make_organization(now), task_id="t", workflow_run_id="wr_x", browser_session_id="pbs_x")
    step = make_step(now, task, step_id="s", status=StepStatus.running, order=0, output=None)
    session_file = "s3://bucket/earlier-action-session.pdf"
    ctx = SkyvernContext(task_id="t")
    dead = _page("about:blank")
    survivor = _page("https://example.test/")
    survivor.is_closed.return_value = False
    # A false-click claim: local-only baseline, NOT session-observed (session_observed defaults to False).
    ctx.record_download_popup_claim("t", dead, baseline_files=[])
    skyvern_context.set(ctx)
    try:
        agent = ForgeAgent()
        with (
            patch(f"{_AGENT}.resolve_run_download_id", return_value="wr_x"),
            patch(f"{_AGENT}.get_path_for_workflow_download_directory", return_value=tmp_path),
            patch(f"{_AGENT}.list_downloading_files_in_directory", return_value=[]),
            patch(f"{_AGENT}.list_files_in_directory", return_value=[]),
            patch(f"{_AGENT}.app") as agent_app,
        ):
            agent_app.STORAGE.list_downloaded_files_in_browser_session = AsyncMock(return_value=[session_file])
            agent_app.STORAGE.list_downloading_files_in_browser_session = AsyncMock(return_value=[])
            result = await agent._empty_page_recovery_plan(
                task,
                step,
                _recovery_state(dead, survivor),
                task_block=_task_block(complete_on_download=True, download_timeout=0.05),
                attempt_started_at=None,
                list_files_before=[],
            )
        # Not session-observed -> the session file is ignored -> not credited -> release + recover.
        assert result is not None and len(result) == 1 and result[0].is_internal_recovery is True
        # And the observer never consulted session storage for this local-only claim.
        agent_app.STORAGE.list_downloaded_files_in_browser_session.assert_not_awaited()
        agent_app.STORAGE.list_downloading_files_in_browser_session.assert_not_awaited()
    finally:
        skyvern_context.reset()


@pytest.mark.asyncio
async def test_finally_resnapshot_augments_stale_event_recorded_session_baseline(tmp_path: Path) -> None:
    """Round-12: an event-recorded popup anchors the STALE pre-action session snapshot (empty here). An
    earlier action's session download settles before the popup is minted; the action-finally resnapshot
    re-records the same popup and AUGMENTS (unions) its session baseline -- setdefault would keep the stale
    first snapshot. Recovery must then NOT treat that earlier file as new. A popup whose own download lands
    only after its mint (absent from the augment) must still credit."""
    now = datetime.now(UTC)
    task = make_task(now, make_organization(now), task_id="t", workflow_run_id="wr_x", browser_session_id="pbs_x")
    step = make_step(now, task, step_id="s", status=StepStatus.running, order=0, output=None)
    earlier_session_file = "s3://bucket/earlier-action.pdf"

    async def _run(finally_session: list[str], observed_session: list[str]):
        ctx = SkyvernContext(task_id="t")
        dead = _page("about:blank")
        survivor = _page("https://example.test/")
        survivor.is_closed.return_value = False
        # Event-recorded (sync popup callback): stale pre-action session snapshot is empty.
        ctx.record_download_popup_claim("t", dead, baseline_files=[], session_observed=True, session_baseline_files=[])
        # Action-finally resnapshot re-records the SAME page with the fresh session view.
        ctx.record_download_popup_claim(
            "t", dead, baseline_files=[], session_observed=True, session_baseline_files=finally_session
        )
        skyvern_context.set(ctx)
        try:
            agent = ForgeAgent()
            with (
                patch(f"{_AGENT}.resolve_run_download_id", return_value="wr_x"),
                patch(f"{_AGENT}.get_path_for_workflow_download_directory", return_value=tmp_path),
                patch(f"{_AGENT}.list_downloading_files_in_directory", return_value=[]),
                patch(f"{_AGENT}.list_files_in_directory", return_value=[]),
                patch(f"{_AGENT}.app") as agent_app,
            ):
                agent_app.STORAGE.list_downloaded_files_in_browser_session = AsyncMock(return_value=observed_session)
                agent_app.STORAGE.list_downloading_files_in_browser_session = AsyncMock(return_value=[])
                return await agent._empty_page_recovery_plan(
                    task,
                    step,
                    _recovery_state(dead, survivor),
                    task_block=_task_block(complete_on_download=True, download_timeout=0.05),
                    attempt_started_at=None,
                    list_files_before=[],
                )
        finally:
            skyvern_context.reset()

    # Earlier action's file settled by the finally -> augmented into the baseline -> NOT credited -> recover.
    present = await _run(finally_session=[earlier_session_file], observed_session=[earlier_session_file])
    assert present is not None and len(present) == 1 and present[0].is_internal_recovery is True

    # The popup's own download lands only after its mint AND after the finally (absent from the augment)
    # -> not in the baseline -> credited -> complete-on-download short-circuit.
    after = await _run(finally_session=[], observed_session=["s3://bucket/own-download.pdf"])
    assert after == []


@pytest.mark.asyncio
async def test_own_inflight_partial_excluded_from_baseline_still_credits(tmp_path: Path) -> None:
    """Round-13 (observer view): the popup's OWN download in-flight at the finally must NOT be in its
    baseline, so when it settles during a later grace the observer credits it. If the partial's identity had
    been augmented in (the round-12 over-augment), normalize would collapse the settled URI to it and drop
    the credit -- releasing/failing the popup for a slightly-late own download."""
    now = datetime.now(UTC)
    task = make_task(now, make_organization(now), task_id="t", workflow_run_id="wr_x", browser_session_id="pbs_x")
    step = make_step(now, task, step_id="s", status=StepStatus.running, order=0, output=None)
    own_partial = "s3://bucket/own.pdf.crdownload"
    own_settled = "s3://bucket/own.pdf"

    async def _run(session_baseline: list[str]):
        ctx = SkyvernContext(task_id="t")
        dead = _page("about:blank")
        survivor = _page("https://example.test/")
        survivor.is_closed.return_value = False
        ctx.record_download_popup_claim(
            "t", dead, baseline_files=[], session_observed=True, session_baseline_files=session_baseline
        )
        skyvern_context.set(ctx)
        try:
            agent = ForgeAgent()
            with (
                patch(f"{_AGENT}.resolve_run_download_id", return_value="wr_x"),
                patch(f"{_AGENT}.get_path_for_workflow_download_directory", return_value=tmp_path),
                patch(f"{_AGENT}.list_downloading_files_in_directory", return_value=[]),
                patch(f"{_AGENT}.list_files_in_directory", return_value=[]),
                patch(f"{_AGENT}.app") as agent_app,
            ):
                # The own download has settled by the time recovery observes it.
                agent_app.STORAGE.list_downloaded_files_in_browser_session = AsyncMock(return_value=[own_settled])
                agent_app.STORAGE.list_downloading_files_in_browser_session = AsyncMock(return_value=[])
                return await agent._empty_page_recovery_plan(
                    task,
                    step,
                    _recovery_state(dead, survivor),
                    task_block=_task_block(complete_on_download=True, download_timeout=0.05),
                    attempt_started_at=None,
                    list_files_before=[],
                )
        finally:
            skyvern_context.reset()

    # Settled-only augment (the fix): the own in-flight partial never entered the baseline -> its settle
    # is a new file -> credited.
    assert await _run(session_baseline=[]) == []
    # The harm the fix avoids: had the own partial been augmented in (round-12 over-augment), the settled
    # URI normalizes to the baselined identity -> not new -> the popup is wrongly released instead of credited.
    released = await _run(session_baseline=[own_partial])
    assert released is not None and len(released) == 1 and released[0].is_internal_recovery is True


@pytest.mark.asyncio
async def test_session_inflight_partial_settling_during_grace_does_not_credit_but_later_partial_does(
    tmp_path: Path,
) -> None:
    """Round-6 residual: the claim baseline must also union the session's IN-FLIGHT partials (the s3
    downloaded-listing excludes them). An earlier session download still partial at claim time settles to
    its final name during the grace; ``normalize_download_identity`` collapses partial->final so it is NOT
    miscredited. A partial that starts only AFTER the claim is still allowed to settle and credit."""
    now = datetime.now(UTC)
    task = make_task(now, make_organization(now), task_id="t", workflow_run_id="wr_x", browser_session_id="pbs_x")
    step = make_step(now, task, step_id="s", status=StepStatus.running, order=0, output=None)
    partial = "s3://bucket/foo.pdf.crdownload"
    settled = "s3://bucket/foo.pdf"

    async def _run(
        claim_downloaded: list[str],
        claim_downloading: list[str],
        observed_downloaded: list[str],
        observed_downloading: list[str],
    ):
        # Claim-time session baseline = the pre-click session snapshot (downloaded + in-flight) the
        # authoritative read captures, exactly as the download-action claim recorder builds the baseline.
        claim_baseline = list(claim_downloaded) + list(claim_downloading)
        ctx = SkyvernContext(task_id="t")
        dead = _page("about:blank")
        survivor = _page("https://example.test/")
        survivor.is_closed.return_value = False
        # main-path claims are session-observed, so recovery unions session files for them
        ctx.record_download_popup_claim("t", dead, baseline_files=claim_baseline, session_observed=True)
        skyvern_context.set(ctx)
        try:
            agent = ForgeAgent()
            with (
                patch(f"{_AGENT}.resolve_run_download_id", return_value="wr_x"),
                patch(f"{_AGENT}.get_path_for_workflow_download_directory", return_value=tmp_path),
                patch(f"{_AGENT}.list_downloading_files_in_directory", return_value=[]),
                patch(f"{_AGENT}.list_files_in_directory", return_value=[]),
                patch(f"{_AGENT}.app") as agent_app,
            ):
                agent_app.STORAGE.list_downloaded_files_in_browser_session = AsyncMock(return_value=observed_downloaded)
                agent_app.STORAGE.list_downloading_files_in_browser_session = AsyncMock(
                    return_value=observed_downloading
                )
                return await agent._empty_page_recovery_plan(
                    task,
                    step,
                    _recovery_state(dead, survivor),
                    task_block=_task_block(complete_on_download=True, download_timeout=0.05),
                    attempt_started_at=None,
                    list_files_before=[],
                )
        finally:
            skyvern_context.reset()

    # Partial present at claim time, settled to its final name during the grace -> NOT credited.
    present = await _run(
        claim_downloaded=[], claim_downloading=[partial], observed_downloaded=[settled], observed_downloading=[]
    )
    assert present is not None and len(present) == 1 and present[0].is_internal_recovery is True

    # A partial that starts only AFTER the claim -> not in the baseline -> allowed to settle and credit.
    after = await _run(
        claim_downloaded=[], claim_downloading=[], observed_downloaded=[settled], observed_downloading=[]
    )
    assert after == []


@pytest.mark.asyncio
async def test_recovery_uses_exact_page_baseline_not_stale_block_baseline(tmp_path: Path) -> None:
    """Multi-download regression: an earlier action's file already sits in the run dir when a LATER
    claimed dead-blank popup is created, so that popup's page baseline captures it. The observer must
    compare against THIS page's baseline, not the stale block baseline reused across recursive steps --
    otherwise the earlier file is miscredited to the later page, releasing/closing it before its own
    download can settle. Proves per-page isolation: with an empty page baseline the same file DOES
    credit its own page."""
    now = datetime.now(UTC)
    task = make_task(now, make_organization(now), task_id="t", workflow_run_id="wr_x")
    step = make_step(now, task, step_id="s", status=StepStatus.running, order=0, output=None)
    earlier_file = tmp_path / "earlier.pdf"
    earlier_file.write_text("x")

    # Later popup: its claim is created AFTER earlier.pdf exists, so its baseline captures earlier.pdf.
    later_ctx = SkyvernContext(task_id=task.task_id)
    dead_later = _page("about:blank")
    survivor = _page("https://example.test/")
    survivor.is_closed.return_value = False
    later_ctx.record_download_popup_claim(task.task_id, dead_later, baseline_files=[str(earlier_file)])
    skyvern_context.set(later_ctx)
    try:
        agent = ForgeAgent()
        with (
            patch(f"{_AGENT}.resolve_run_download_id", return_value="wr_x"),
            patch(f"{_AGENT}.get_path_for_workflow_download_directory", return_value=tmp_path),
        ):
            result = await agent._empty_page_recovery_plan(
                task,
                step,
                _recovery_state(dead_later, survivor),
                # tiny positive grace so the un-credited observation ages out fast instead of hanging
                task_block=_task_block(complete_on_download=True, download_timeout=0.05),
                attempt_started_at=None,
                list_files_before=[],  # STALE block baseline: captured before earlier.pdf landed
            )
        # earlier.pdf is in the later page's own baseline -> NOT new credit -> release + recover.
        assert result is not None and len(result) == 1 and result[0].is_internal_recovery is True
        assert later_ctx.has_download_popup_claim(task.task_id, dead_later) is False
    finally:
        skyvern_context.reset()

    # Earlier page (isolation counter-case): its claim predates earlier.pdf (empty page baseline), so the
    # very same file legitimately credits ITS page -> complete-on-download short-circuit returns [].
    earlier_ctx = SkyvernContext(task_id=task.task_id)
    dead_earlier = _page("about:blank")
    survivor2 = _page("https://example.test/")
    survivor2.is_closed.return_value = False
    earlier_ctx.record_download_popup_claim(task.task_id, dead_earlier, baseline_files=[])
    skyvern_context.set(earlier_ctx)
    try:
        agent = ForgeAgent()
        with (
            patch(f"{_AGENT}.resolve_run_download_id", return_value="wr_x"),
            patch(f"{_AGENT}.get_path_for_workflow_download_directory", return_value=tmp_path),
        ):
            result = await agent._empty_page_recovery_plan(
                task,
                step,
                _recovery_state(dead_earlier, survivor2),
                task_block=_task_block(complete_on_download=True, download_timeout=0.05),
                attempt_started_at=None,
                list_files_before=[],
            )
        assert result == [], "a file new to THIS page's baseline must credit it"
    finally:
        skyvern_context.reset()


@pytest.mark.asyncio
async def test_observe_workflow_less_task_observes_via_task_id(tmp_path: Path) -> None:
    """A true workflow-less standalone task (workflow_run_id=None) keys its download dir by task_id,
    exactly as the authoritative credit seam resolves it (``workflow_run_id or task_id``). The observer
    must still observe such a task, not bail early on the missing workflow_run_id."""
    task = make_task(
        datetime.now(UTC),
        make_organization(datetime.now(UTC)),
        task_id="t",
        workflow_run_id=None,
    )
    agent = ForgeAgent()
    before = [str(tmp_path / "old.pdf")]
    after = before + [str(tmp_path / "new.pdf")]
    ctx = SkyvernContext(task_id="t")  # no run_id/workflow_run_id -> resolve_run_download_id falls to task_id
    with (
        patch(f"{_AGENT}.get_path_for_workflow_download_directory", return_value=tmp_path),
        patch(f"{_AGENT}.list_downloading_files_in_directory", return_value=[]),
        patch(f"{_AGENT}.list_files_in_directory", return_value=after),
        patch(f"{_AGENT}.skyvern_context.current", return_value=ctx),
    ):
        credited = await agent._observe_claimed_download_within_grace(
            task, _task_block(), remaining=5.0, list_files_before=before, attempt_started_at=None
        )
    assert credited is True


@pytest.mark.asyncio
async def test_standalone_task_grace_uses_task_download_timeout() -> None:
    """Standalone task (task_block is None): the settlement grace must come from ``task.download_timeout``,
    not the 600s global fallback, matching the authoritative credit seams' standalone-task resolution."""
    now = datetime.now(UTC)
    task = make_task(now, make_organization(now), task_id="t", workflow_run_id="wr_x", download_timeout=123.0)
    step = make_step(now, task, step_id="s", status=StepStatus.running, order=0, output=None)
    ctx = SkyvernContext(task_id=task.task_id)
    dead = _page("about:blank")
    survivor = _page("https://example.test/")
    survivor.is_closed.return_value = False
    ctx.record_download_popup_claim(task.task_id, dead)
    skyvern_context.set(ctx)
    try:
        agent = ForgeAgent()
        observe = AsyncMock(return_value=False)
        with patch.object(ForgeAgent, "_observe_claimed_download_within_grace", observe):
            await agent._empty_page_recovery_plan(
                task,
                step,
                _recovery_state(dead, survivor),
                task_block=None,
                attempt_started_at=None,
                list_files_before=[],
            )
        remaining = observe.call_args.kwargs["remaining"]
        assert remaining <= 123.0, "grace must be bounded by task.download_timeout"
        assert remaining > 100.0, "grace must not fall back to the 600s global for a configured standalone task"
    finally:
        skyvern_context.reset()


@pytest.mark.asyncio
async def test_claimed_blank_complete_on_download_credit_routes_empty_list() -> None:
    """Credit during grace -> return [] so the single authoritative 3106 seam finalizes and completes;
    the claim is NOT released here and no recovery attempt is consumed."""
    now = datetime.now(UTC)
    task = make_task(now, make_organization(now), task_id="t", workflow_run_id="wr_x")
    step = make_step(now, task, step_id="s", status=StepStatus.running, order=0, output=None)
    ctx = SkyvernContext(task_id=task.task_id)
    dead = _page("about:blank")
    survivor = _page("https://example.test/")
    survivor.is_closed.return_value = False
    ctx.record_download_popup_claim(task.task_id, dead)
    skyvern_context.set(ctx)
    try:
        agent = ForgeAgent()
        with patch.object(ForgeAgent, "_observe_claimed_download_within_grace", AsyncMock(return_value=True)):
            result = await agent._empty_page_recovery_plan(
                task,
                step,
                _recovery_state(dead, survivor),
                task_block=_task_block(complete_on_download=True),
                attempt_started_at=None,
                list_files_before=[],
            )
        assert result == []
        assert ctx.has_download_popup_claim(task.task_id, dead) is True  # not released
        assert ctx.empty_page_recovery_attempts == {}  # no attempt consumed
    finally:
        skyvern_context.reset()


@pytest.mark.asyncio
@pytest.mark.parametrize("complete_on_download", [True, False])
async def test_claimed_blank_no_credit_releases_exact_page_and_recovers(complete_on_download: bool) -> None:
    """Both paths bounded (amendment 5): after the grace elapses with no credit, the EXACT page is
    released from both registries and a recovery ClosePageAction is returned; attempt consumed once."""
    now = datetime.now(UTC)
    task = make_task(now, make_organization(now), task_id="t")
    step = make_step(now, task, step_id="s", status=StepStatus.running, order=0, output=None)
    ctx = SkyvernContext(task_id=task.task_id)
    dead = _page("about:blank")
    sibling = _page("about:blank")
    survivor = _page("https://example.test/")
    survivor.is_closed.return_value = False
    ctx.record_download_popup_claim(task.task_id, dead)
    ctx.record_download_popup_late_candidate(task.task_id, dead)
    ctx.record_download_popup_claim(task.task_id, sibling)  # unrelated sibling must survive
    skyvern_context.set(ctx)
    try:
        agent = ForgeAgent()
        with patch.object(ForgeAgent, "_observe_claimed_download_within_grace", AsyncMock(return_value=False)):
            result = await agent._empty_page_recovery_plan(
                task,
                step,
                _recovery_state(dead, survivor),
                task_block=_task_block(complete_on_download=complete_on_download),
                attempt_started_at=None,
                list_files_before=[],
            )
        assert result is not None and len(result) == 1 and result[0].is_internal_recovery is True
        assert ctx.has_download_popup_claim(task.task_id, dead) is False  # exact page released, both registries
        assert ctx.has_download_popup_claim(task.task_id, sibling) is True  # sibling preserved
        assert ctx.empty_page_recovery_attempts.get(task.task_id) == 1
    finally:
        skyvern_context.reset()


@pytest.mark.asyncio
async def test_claimed_blank_cancellation_performs_no_ownership_mutation() -> None:
    now = datetime.now(UTC)
    task = make_task(now, make_organization(now), task_id="t")
    step = make_step(now, task, step_id="s", status=StepStatus.running, order=0, output=None)
    ctx = SkyvernContext(task_id=task.task_id)
    dead = _page("about:blank")
    survivor = _page("https://example.test/")
    survivor.is_closed.return_value = False
    ctx.record_download_popup_claim(task.task_id, dead)
    skyvern_context.set(ctx)
    try:
        agent = ForgeAgent()
        with patch.object(
            ForgeAgent, "_observe_claimed_download_within_grace", AsyncMock(side_effect=asyncio.CancelledError())
        ):
            with pytest.raises(asyncio.CancelledError):
                await agent._empty_page_recovery_plan(
                    task,
                    step,
                    _recovery_state(dead, survivor),
                    task_block=_task_block(),
                    attempt_started_at=None,
                    list_files_before=[],
                )
        assert ctx.has_download_popup_claim(task.task_id, dead) is True  # claim intact
        assert ctx.empty_page_recovery_attempts == {}  # no attempt consumed
    finally:
        skyvern_context.reset()


@pytest.mark.asyncio
async def test_unclaimed_dead_blank_recovers_without_observation() -> None:
    """No claim -> today's fast path, no grace observation invoked."""
    now = datetime.now(UTC)
    task = make_task(now, make_organization(now), task_id="t")
    step = make_step(now, task, step_id="s", status=StepStatus.running, order=0, output=None)
    ctx = SkyvernContext(task_id=task.task_id)
    dead = _page("about:blank")
    survivor = _page("https://example.test/")
    survivor.is_closed.return_value = False
    skyvern_context.set(ctx)
    try:
        agent = ForgeAgent()
        observe = AsyncMock(return_value=False)
        with patch.object(ForgeAgent, "_observe_claimed_download_within_grace", observe):
            result = await agent._empty_page_recovery_plan(
                task,
                step,
                _recovery_state(dead, survivor),
                task_block=_task_block(),
                attempt_started_at=None,
                list_files_before=[],
            )
        assert result is not None and len(result) == 1
        observe.assert_not_awaited()
    finally:
        skyvern_context.reset()
