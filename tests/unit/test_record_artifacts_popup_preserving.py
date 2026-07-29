"""Post-action transient-UI capture gate.

record_artifacts_after_action takes the LLM-facing SCREENSHOT_ACTION. Under the treatment arm
of the PRESERVE_TRANSIENT_UI_CAPTURE experiment, an open ARIA popup forces scrolling_number=0
so the scroll events of a multi-page capture do not dismiss the popup before the next step sees
it. Control shadow-detects only (scrolling unchanged); off never detects. CUA keeps its existing
scrolling_number=0 independently of the experiment arm.
"""

from __future__ import annotations

import contextlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyvern.constants import DEFAULT_MAX_SCREENSHOT_SCROLLS
from skyvern.forge.agent import ForgeAgent
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.schemas.run_enums import RunEngine


@contextlib.asynccontextmanager
async def _run(
    *,
    engine: RunEngine,
    popup_trigger: dict | None,
    arm_value: bool | None,
    context: SkyvernContext | None = None,
):
    """Drive record_artifacts_after_action with stubbed IO, yielding (frame_mock, scrolling_number).
    arm_value maps to the cached experiment assignment: True=treatment, False=control, None=off.
    ``context`` lets a caller reuse one SkyvernContext across successive post-action captures to
    exercise the shared per-run suppression cap."""
    working_page = MagicMock()

    frame_mock = MagicMock()
    frame_mock.safe_wait_for_animation_end = AsyncMock()
    frame_mock.get_scroll_x_y = AsyncMock(return_value=(0, 0))
    frame_mock.safe_scroll_to_x_y = AsyncMock()
    frame_mock.get_open_aria_popup_trigger = AsyncMock(return_value=popup_trigger)
    # Force the html-artifact block to fail fast so we don't need the artifact manager.
    frame_mock.get_content = AsyncMock(side_effect=RuntimeError("stop after screenshot"))

    browser_state = MagicMock()
    browser_state.get_working_page = AsyncMock(return_value=working_page)
    browser_state.take_post_action_screenshot = AsyncMock(side_effect=RuntimeError("stop"))

    task = MagicMock()
    task.workflow_run_id = "wr_test"
    task.task_id = "tsk_test"
    task.organization_id = "o_test"
    step = MagicMock()
    step.order = 1
    step.is_speculative = False
    action = MagicMock()
    action.action_id = None
    action.organization_id = None

    skyvern_context.set(
        context
        or SkyvernContext(
            max_screenshot_scrolls=None,
            workflow_run_id="wr_test",
            preserve_transient_ui_capture=arm_value,
        )
    )

    try:
        with contextlib.ExitStack() as stack:
            skyvern_frame_cls = stack.enter_context(patch("skyvern.forge.agent.SkyvernFrame"))
            skyvern_frame_cls.create_instance = AsyncMock(return_value=frame_mock)

            agent = MagicMock(spec=ForgeAgent)
            await ForgeAgent.record_artifacts_after_action(
                agent, task=task, step=step, browser_state=browser_state, engine=engine, action=action
            )
            scrolling_number = browser_state.take_post_action_screenshot.await_args.kwargs["scrolling_number"]
            yield frame_mock, scrolling_number
    finally:
        skyvern_context.reset()


@pytest.mark.asyncio
async def test_treatment_open_popup_forces_zero_scroll() -> None:
    async with _run(engine=RunEngine.skyvern_v1, popup_trigger={"role": "combobox"}, arm_value=True) as (
        frame_mock,
        scrolling_number,
    ):
        assert scrolling_number == 0, "treatment + open popup must force scrolling_number=0"
        frame_mock.get_open_aria_popup_trigger.assert_awaited()


@pytest.mark.asyncio
async def test_control_open_popup_shadow_detects_but_keeps_scroll() -> None:
    async with _run(engine=RunEngine.skyvern_v1, popup_trigger={"role": "combobox"}, arm_value=False) as (
        frame_mock,
        scrolling_number,
    ):
        assert scrolling_number == DEFAULT_MAX_SCREENSHOT_SCROLLS, "control must not alter scrolling"
        frame_mock.get_open_aria_popup_trigger.assert_awaited()  # shadow detection ran


@pytest.mark.asyncio
async def test_off_open_popup_keeps_scroll_and_skips_predicate() -> None:
    async with _run(engine=RunEngine.skyvern_v1, popup_trigger={"role": "combobox"}, arm_value=None) as (
        frame_mock,
        scrolling_number,
    ):
        assert scrolling_number == DEFAULT_MAX_SCREENSHOT_SCROLLS
        frame_mock.get_open_aria_popup_trigger.assert_not_awaited()  # off pays no shadow cost


@pytest.mark.asyncio
async def test_treatment_no_popup_keeps_default_scroll() -> None:
    async with _run(engine=RunEngine.skyvern_v1, popup_trigger=None, arm_value=True) as (
        frame_mock,
        scrolling_number,
    ):
        assert scrolling_number == DEFAULT_MAX_SCREENSHOT_SCROLLS


@pytest.mark.asyncio
async def test_cua_engine_stays_zero_and_skips_predicate_regardless_of_arm() -> None:
    async with _run(engine=RunEngine.openai_cua, popup_trigger={"role": "combobox"}, arm_value=True) as (
        frame_mock,
        scrolling_number,
    ):
        assert scrolling_number == 0
        frame_mock.get_open_aria_popup_trigger.assert_not_awaited()


@pytest.mark.asyncio
async def test_treatment_zero_scroll_still_reads_scroll_position_legacy() -> None:
    # Legacy always snapshots the scroll position (regardless of scrolling_number); treatment must
    # change ONLY the treatment decision (scrolling_number=0), not the legacy read/restore control
    # flow. Restoring to the unchanged position is a no-op window.scroll, so it does not scroll.
    async with _run(engine=RunEngine.skyvern_v1, popup_trigger={"role": "combobox"}, arm_value=True) as (
        frame_mock,
        scrolling_number,
    ):
        assert scrolling_number == 0
        frame_mock.get_scroll_x_y.assert_awaited()  # legacy always-attempt read preserved


@pytest.mark.asyncio
async def test_scrolling_capture_snapshots_scroll_position() -> None:
    async with _run(engine=RunEngine.skyvern_v1, popup_trigger=None, arm_value=True) as (
        frame_mock,
        scrolling_number,
    ):
        assert scrolling_number == DEFAULT_MAX_SCREENSHOT_SCROLLS
        frame_mock.get_scroll_x_y.assert_awaited_once()


@pytest.mark.asyncio
async def test_treatment_zero_scroll_records_screenshot_action_via_legacy_nesting() -> None:
    # Suppressing the scroll (scrolling_number=0) still records the LLM-facing SCREENSHOT_ACTION,
    # via the LEGACY nesting: get_scroll_x_y succeeds (0, 0) so x/y are known and the recording runs.
    from skyvern.forge import app

    frame_mock = MagicMock()
    frame_mock.safe_wait_for_animation_end = AsyncMock()
    frame_mock.get_scroll_x_y = AsyncMock(return_value=(0, 0))
    frame_mock.safe_scroll_to_x_y = AsyncMock()
    frame_mock.get_open_aria_popup_trigger = AsyncMock(return_value={"role": "combobox"})
    frame_mock.get_content = AsyncMock(side_effect=RuntimeError("stop after screenshot"))

    browser_state = MagicMock()
    browser_state.get_working_page = AsyncMock(return_value=MagicMock())
    browser_state.take_post_action_screenshot = AsyncMock(return_value=b"img")

    task = MagicMock()
    task.workflow_run_id = "wr_test"
    task.task_id = "tsk_test"
    task.organization_id = "o_test"
    step = MagicMock()
    step.order = 1
    step.is_speculative = False
    action = MagicMock()
    action.action_id = None
    action.organization_id = None

    skyvern_context.set(
        SkyvernContext(max_screenshot_scrolls=None, workflow_run_id="wr_test", preserve_transient_ui_capture=True)
    )
    try:
        with contextlib.ExitStack() as stack:
            skyvern_frame_cls = stack.enter_context(patch("skyvern.forge.agent.SkyvernFrame"))
            skyvern_frame_cls.create_instance = AsyncMock(return_value=frame_mock)
            prepare = AsyncMock(return_value=None)
            stack.enter_context(patch.object(app.ARTIFACT_MANAGER, "prepare_llm_artifact", prepare))

            agent = MagicMock(spec=ForgeAgent)
            await ForgeAgent.record_artifacts_after_action(
                agent, task=task, step=step, browser_state=browser_state, engine=RunEngine.skyvern_v1, action=action
            )
            assert browser_state.take_post_action_screenshot.await_args.kwargs["scrolling_number"] == 0
            frame_mock.get_scroll_x_y.assert_awaited()  # legacy always-attempt read
            frame_mock.safe_scroll_to_x_y.assert_awaited()  # legacy restore (no-op to same position)
            prepare.assert_awaited()  # artifact recorded via legacy nesting (x/y known)
    finally:
        skyvern_context.reset()


@pytest.mark.asyncio
async def test_off_scroll_fetch_failure_drops_screenshot_action_legacy() -> None:
    # OFF must follow EXACT legacy artifact semantics: legacy nested the SCREENSHOT_ACTION recording
    # inside `if x is not None and y is not None`, so a failing get_scroll_x_y drops the screenshot.
    # (Decoupling the recording from the scroll snapshot is an unrelated hardening, deferred out of
    # this PR so OFF keeps legacy control flow.)
    from skyvern.forge import app

    frame_mock = MagicMock()
    frame_mock.safe_wait_for_animation_end = AsyncMock()
    frame_mock.get_scroll_x_y = AsyncMock(side_effect=RuntimeError("scroll read failed"))
    frame_mock.safe_scroll_to_x_y = AsyncMock()
    frame_mock.get_open_aria_popup_trigger = AsyncMock(return_value=None)
    frame_mock.get_content = AsyncMock(side_effect=RuntimeError("stop after screenshot"))

    browser_state = MagicMock()
    browser_state.get_working_page = AsyncMock(return_value=MagicMock())
    browser_state.take_post_action_screenshot = AsyncMock(return_value=b"img")

    task = MagicMock()
    task.workflow_run_id = "wr_test"
    task.task_id = "tsk_test"
    task.organization_id = "o_test"
    step = MagicMock()
    step.order = 1
    step.is_speculative = False
    action = MagicMock()
    action.action_id = None
    action.organization_id = None

    skyvern_context.set(
        SkyvernContext(max_screenshot_scrolls=None, workflow_run_id="wr_test", preserve_transient_ui_capture=None)
    )
    try:
        with contextlib.ExitStack() as stack:
            skyvern_frame_cls = stack.enter_context(patch("skyvern.forge.agent.SkyvernFrame"))
            skyvern_frame_cls.create_instance = AsyncMock(return_value=frame_mock)
            prepare = AsyncMock(return_value=None)
            stack.enter_context(patch.object(app.ARTIFACT_MANAGER, "prepare_llm_artifact", prepare))

            agent = MagicMock(spec=ForgeAgent)
            await ForgeAgent.record_artifacts_after_action(
                agent, task=task, step=step, browser_state=browser_state, engine=RunEngine.skyvern_v1, action=action
            )
            assert browser_state.take_post_action_screenshot.await_args.kwargs["scrolling_number"] > 0
            frame_mock.get_scroll_x_y.assert_awaited()  # fetch was attempted (and raised)
            frame_mock.safe_scroll_to_x_y.assert_not_awaited()  # no restore (x/y unknown)
            prepare.assert_not_awaited()  # legacy: recording is nested under x/y-not-None, so it is dropped
    finally:
        skyvern_context.reset()


# --- The post-action capture is the sibling of the agent-step scrape and must honor the same two
# invariants: bounded telemetry tokens, and the shared per-run consecutive-suppression cap. ---


@pytest.mark.asyncio
async def test_post_action_routes_telemetry_through_bounded_helper() -> None:
    # The post-action site must emit popup telemetry only through the shared, bounded
    # emit_transient_ui_popup_telemetry helper (allowlisted tokens + bounded boolean), never by
    # writing raw page-controlled role/aria-haspopup to the span. The helper itself is unit-tested
    # in test_transient_ui_capture_experiment.py; here we assert the wiring.
    trigger = {"role": "<script>evil", "hasPopup": "arbitrary-attacker-text", "controlsResolved": 0}
    with patch("skyvern.forge.agent.emit_transient_ui_popup_telemetry") as emit:
        async with _run(engine=RunEngine.skyvern_v1, popup_trigger=trigger, arm_value=True):
            pass
        emit.assert_called_once()
        assert emit.call_args.args[1] is trigger


@pytest.mark.asyncio
async def test_post_action_participates_in_shared_consecutive_cap() -> None:
    ctx = SkyvernContext(max_screenshot_scrolls=None, workflow_run_id="wr_cap", preserve_transient_ui_capture=True)
    scrolls = []
    for _ in range(3):
        async with _run(
            engine=RunEngine.skyvern_v1, popup_trigger={"role": "combobox"}, arm_value=True, context=ctx
        ) as (_f, scrolling_number):
            scrolls.append(scrolling_number)
    assert scrolls[0] == 0 and scrolls[1] == 0, "first two post-action captures suppress"
    assert scrolls[2] == DEFAULT_MAX_SCREENSHOT_SCROLLS, "third consecutive capture falls back to legacy scrolling"
    assert ctx.transient_ui_consecutive_suppressions == 2


@pytest.mark.asyncio
async def test_post_action_no_popup_resets_shared_counter() -> None:
    ctx = SkyvernContext(max_screenshot_scrolls=None, workflow_run_id="wr_reset", preserve_transient_ui_capture=True)
    for _ in range(2):
        async with _run(engine=RunEngine.skyvern_v1, popup_trigger={"role": "combobox"}, arm_value=True, context=ctx):
            pass
    assert ctx.transient_ui_consecutive_suppressions == 2
    async with _run(engine=RunEngine.skyvern_v1, popup_trigger=None, arm_value=True, context=ctx):
        pass
    assert ctx.transient_ui_consecutive_suppressions == 0
