"""Scrape-site transient-UI capture gate.

Under the treatment arm of PRESERVE_TRANSIENT_UI_CAPTURE, when an ARIA popup (e.g. a portal
date-picker popup) is open, the next scrape's split screenshot must NOT scroll — scrolling
fires events that dismiss the portal popup before the just-built element tree's day-cell IDs
can be acted on. Control shadow-detects only (scrolls as before); off never detects.
"""

from __future__ import annotations

import contextlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyvern.forge.agent import ForgeAgent, ScrapeType
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.webeye.scraper import scraper


def _make_page() -> MagicMock:
    page = MagicMock()
    page.url = "https://example.com"
    page.main_frame.url = "https://example.com"
    page.main_frame.child_frames = []
    page.viewport_size = {"width": 1280, "height": 800}
    return page


_UNSET = object()


@contextlib.contextmanager
def _span_capture():
    """Patch traced_span to yield a MagicMock span so tests can inspect emitted attributes."""
    span = MagicMock()

    @contextlib.contextmanager
    def _fake_traced_span(_tracer, _name):
        yield span

    with patch.object(scraper, "traced_span", new=_fake_traced_span):
        yield span


def _span_attrs(span: MagicMock) -> dict:
    return {call.args[0]: call.args[1] for call in span.set_attribute.call_args_list}


@contextlib.asynccontextmanager
async def _run_scrape(
    *,
    popup_trigger: dict | None,
    arm_value: bool | None,
    scroll: bool = True,
    allow_transient_ui_suppression: object = True,
    context: SkyvernContext | None = None,
):
    """Drive scrape_web_unsafe with the heavy dependencies stubbed, yielding
    (take_split_screenshots_mock, frame_mock, span_mock). arm_value maps to the cached experiment
    assignment: True=treatment, False=control, None=off.

    allow_transient_ui_suppression is forwarded to scrape_web_unsafe unless left as ``_UNSET``, which
    simulates a caller (verify/extract/error path) that never opts into suppression. ``context`` lets a
    caller reuse one SkyvernContext across successive scrapes to exercise the per-run suppression cap.
    """
    page = _make_page()
    browser_state = MagicMock()
    browser_state.must_get_working_page = AsyncMock(return_value=page)

    frame_mock = MagicMock()
    frame_mock.get_scroll_x_y = AsyncMock(return_value=(0, 0))
    frame_mock.safe_scroll_to_x_y = AsyncMock()
    frame_mock.get_open_aria_popup_trigger = AsyncMock(return_value=popup_trigger)
    frame_mock.get_content = AsyncMock(return_value="<html></html>")

    element = {"id": "btn", "tagName": "button"}

    ctx = context or SkyvernContext(workflow_run_id="wr_test", preserve_transient_ui_capture=arm_value)
    skyvern_context.set(ctx)
    try:
        with contextlib.ExitStack() as stack:
            skyvern_frame_cls = stack.enter_context(patch.object(scraper, "SkyvernFrame"))
            skyvern_frame_cls.create_instance = AsyncMock(return_value=frame_mock)
            take_split = AsyncMock(return_value=[b"img"])
            skyvern_frame_cls.take_split_screenshots = take_split

            stack.enter_context(patch.object(scraper, "_wait_for_scrape_ready", new=AsyncMock()))
            stack.enter_context(
                patch.object(
                    scraper,
                    "get_interactable_element_tree",
                    new=AsyncMock(return_value=([element], [element], [])),
                )
            )
            stack.enter_context(patch.object(scraper, "trim_element_tree", new=MagicMock(return_value=[])))
            stack.enter_context(
                patch.object(scraper, "build_element_dict", new=MagicMock(return_value=({}, {}, {}, {}, {})))
            )
            stack.enter_context(patch.object(scraper, "get_frame_text", new=AsyncMock(return_value="")))
            stack.enter_context(patch.object(scraper, "advance_observation_epoch", new=MagicMock()))
            stack.enter_context(patch.object(scraper, "_record_scrape_span_attrs", new=MagicMock()))
            span = stack.enter_context(_span_capture())

            kwargs: dict = dict(
                browser_state=browser_state,
                url="https://example.com",
                cleanup_element_tree=AsyncMock(return_value=[element]),
                scroll=scroll,
            )
            if allow_transient_ui_suppression is not _UNSET:
                kwargs["allow_transient_ui_suppression"] = allow_transient_ui_suppression
            await scraper.scrape_web_unsafe(**kwargs)
            yield take_split, frame_mock, span
    finally:
        skyvern_context.reset()


@pytest.mark.asyncio
async def test_treatment_open_popup_suppresses_only_the_scroll_argument() -> None:
    # Treatment differs from legacy ONLY in the scroll argument to take_split_screenshots. The
    # scroll-position read + restore stay exactly as legacy (always-attempt); restoring to the
    # unchanged position is a no-op window.scroll that does not dismiss the popup.
    async with _run_scrape(popup_trigger={"role": "combobox"}, arm_value=True) as (take_split, frame_mock, _span):
        assert take_split.await_args.kwargs["scroll"] is False, "treatment + open popup must force scroll=False"
        frame_mock.get_scroll_x_y.assert_awaited()  # legacy always-attempt read preserved
        frame_mock.safe_scroll_to_x_y.assert_awaited()  # legacy restore preserved


@pytest.mark.asyncio
async def test_off_scroll_false_preserves_legacy_read_and_restore() -> None:
    # OFF must follow EXACT legacy control flow: legacy read the scroll position and restored it
    # unconditionally, even when scroll=False. The current effective_scroll gate skips the read
    # (RED); OFF must keep the legacy always-attempt read + restore.
    async with _run_scrape(popup_trigger={"role": "combobox"}, arm_value=None, scroll=False) as (
        take_split,
        frame_mock,
        _span,
    ):
        assert take_split.await_args.kwargs["scroll"] is False
        frame_mock.get_open_aria_popup_trigger.assert_not_awaited()  # OFF never detects
        frame_mock.get_scroll_x_y.assert_awaited()  # legacy always-attempt read
        frame_mock.safe_scroll_to_x_y.assert_awaited()  # legacy restore


@pytest.mark.asyncio
async def test_treatment_no_popup_scrolls_and_restores_as_before() -> None:
    async with _run_scrape(popup_trigger=None, arm_value=True) as (take_split, frame_mock, _span):
        assert take_split.await_args.kwargs["scroll"] is True
        frame_mock.get_open_aria_popup_trigger.assert_awaited()
        frame_mock.get_scroll_x_y.assert_awaited()
        frame_mock.safe_scroll_to_x_y.assert_awaited()


@pytest.mark.asyncio
async def test_control_open_popup_shadow_detects_but_keeps_scroll() -> None:
    async with _run_scrape(popup_trigger={"role": "combobox"}, arm_value=False) as (take_split, frame_mock, _span):
        assert take_split.await_args.kwargs["scroll"] is True, "control must not alter scrolling"
        frame_mock.get_open_aria_popup_trigger.assert_awaited()  # shadow detection ran


@pytest.mark.asyncio
async def test_off_open_popup_scrolls_and_skips_predicate() -> None:
    async with _run_scrape(popup_trigger={"role": "combobox"}, arm_value=None) as (take_split, frame_mock, _span):
        assert take_split.await_args.kwargs["scroll"] is True, "off must preserve status-quo scrolling"
        frame_mock.get_open_aria_popup_trigger.assert_not_awaited()


@pytest.mark.asyncio
async def test_caller_scroll_false_short_circuits_predicate() -> None:
    async with _run_scrape(popup_trigger={"role": "combobox"}, arm_value=True, scroll=False) as (
        take_split,
        frame_mock,
        _span,
    ):
        assert take_split.await_args.kwargs["scroll"] is False
        frame_mock.get_open_aria_popup_trigger.assert_not_awaited()


# --- MUST-FIX 3: suppression is opt-in per call. A caller that does not opt in (verify /
# extraction / error-detection scrapes) keeps legacy scrolling even under treatment + open popup. ---


@pytest.mark.asyncio
async def test_no_optin_caller_keeps_legacy_scroll_and_skips_detection() -> None:
    async with _run_scrape(
        popup_trigger={"role": "combobox"}, arm_value=True, allow_transient_ui_suppression=_UNSET
    ) as (take_split, frame_mock, _span):
        assert take_split.await_args.kwargs["scroll"] is True, "non-opted-in scrape must stay legacy"
        frame_mock.get_open_aria_popup_trigger.assert_not_awaited(), "no detection off the agent-step path"


@pytest.mark.asyncio
async def test_explicit_disallow_keeps_legacy_scroll() -> None:
    async with _run_scrape(
        popup_trigger={"role": "combobox"}, arm_value=True, allow_transient_ui_suppression=False
    ) as (take_split, frame_mock, _span):
        assert take_split.await_args.kwargs["scroll"] is True
        frame_mock.get_open_aria_popup_trigger.assert_not_awaited()


@pytest.mark.asyncio
async def test_optin_agent_step_suppresses() -> None:
    async with _run_scrape(popup_trigger={"role": "combobox"}, arm_value=True, allow_transient_ui_suppression=True) as (
        take_split,
        frame_mock,
        _span,
    ):
        assert take_split.await_args.kwargs["scroll"] is False, "opted-in agent-step scrape suppresses"


# --- MUST-FIX 2: at most two consecutive suppressing captures per run, then fall back to legacy
# scrolling; reset the counter when no qualifying popup is detected. ---


@pytest.mark.asyncio
async def test_consecutive_suppression_capped_at_two_then_legacy() -> None:
    ctx = SkyvernContext(workflow_run_id="wr_cap", preserve_transient_ui_capture=True)
    scroll_args = []
    for _ in range(3):
        async with _run_scrape(popup_trigger={"role": "combobox"}, arm_value=True, context=ctx) as (
            take_split,
            _frame,
            _span,
        ):
            scroll_args.append(take_split.await_args.kwargs["scroll"])
    assert scroll_args == [False, False, True], f"expected suppress, suppress, fall back; got {scroll_args}"
    assert ctx.transient_ui_consecutive_suppressions == 2


@pytest.mark.asyncio
async def test_no_popup_resets_consecutive_counter() -> None:
    ctx = SkyvernContext(workflow_run_id="wr_reset", preserve_transient_ui_capture=True)
    # burn the cap
    for _ in range(2):
        async with _run_scrape(popup_trigger={"role": "combobox"}, arm_value=True, context=ctx):
            pass
    assert ctx.transient_ui_consecutive_suppressions == 2
    # a scrape with no qualifying popup resets the counter
    async with _run_scrape(popup_trigger=None, arm_value=True, context=ctx):
        pass
    assert ctx.transient_ui_consecutive_suppressions == 0
    # ...so the next open popup suppresses again
    async with _run_scrape(popup_trigger={"role": "combobox"}, arm_value=True, context=ctx) as (
        take_split,
        _frame,
        _span,
    ):
        assert take_split.await_args.kwargs["scroll"] is False


@pytest.mark.asyncio
async def test_off_arm_never_touches_counter() -> None:
    ctx = SkyvernContext(workflow_run_id="wr_off", preserve_transient_ui_capture=None)
    async with _run_scrape(popup_trigger={"role": "combobox"}, arm_value=None, context=ctx) as (
        take_split,
        _frame,
        _span,
    ):
        assert take_split.await_args.kwargs["scroll"] is True
    assert ctx.transient_ui_consecutive_suppressions == 0


@pytest.mark.asyncio
async def test_control_arm_never_suppresses_and_counter_stays_zero() -> None:
    ctx = SkyvernContext(workflow_run_id="wr_ctrl", preserve_transient_ui_capture=False)
    for _ in range(3):
        async with _run_scrape(popup_trigger={"role": "combobox"}, arm_value=False, context=ctx) as (
            take_split,
            _frame,
            _span,
        ):
            assert take_split.await_args.kwargs["scroll"] is True
    assert ctx.transient_ui_consecutive_suppressions == 0


# --- MUST-FIX 4: page-controlled role / aria-haspopup must not reach the span verbatim. Only
# allowlisted matched tokens are emitted. ---


@pytest.mark.asyncio
async def test_arbitrary_role_and_haspopup_not_emitted_to_span() -> None:
    trigger = {"role": "<script>evil", "hasPopup": "arbitrary-attacker-text", "controlsResolved": 0}
    async with _run_scrape(popup_trigger=trigger, arm_value=True) as (_take_split, _frame, span):
        attrs = _span_attrs(span)
        assert attrs.get("transient_ui_role") != "<script>evil"
        assert attrs.get("transient_ui_haspopup") != "arbitrary-attacker-text"
        assert "transient_ui_role" not in attrs, "non-allowlisted role must be omitted"
        assert "transient_ui_haspopup" not in attrs, "non-allowlisted haspopup must be omitted"


@pytest.mark.asyncio
async def test_allowlisted_role_and_haspopup_are_emitted() -> None:
    trigger = {"role": "combobox", "hasPopup": "menu", "controlsResolved": 1}
    async with _run_scrape(popup_trigger=trigger, arm_value=True) as (_take_split, _frame, span):
        attrs = _span_attrs(span)
        assert attrs.get("transient_ui_role") == "combobox"
        assert attrs.get("transient_ui_haspopup") == "menu"


# --- Optional 5: bounded boolean controlsResolved telemetry so the false-positive source
# (no-target portal fallback) is measurable. ---


@pytest.mark.asyncio
async def test_controls_resolved_boolean_emitted_true_and_false() -> None:
    resolved = {"role": "combobox", "hasPopup": "menu", "controlsResolved": 2}
    async with _run_scrape(popup_trigger=resolved, arm_value=True) as (_take_split, _frame, span):
        assert _span_attrs(span).get("transient_ui_controls_resolved") is True
    fallback = {"role": "combobox", "hasPopup": None, "controlsResolved": 0}
    async with _run_scrape(popup_trigger=fallback, arm_value=True) as (_take_split, _frame, span):
        assert _span_attrs(span).get("transient_ui_controls_resolved") is False


# --- MUST-FIX 3 call-site wiring: the agent-step scrape (ForgeAgent._scrape_with_type) is the only
# call site that opts into suppression. verify / extraction / error-detection paths call
# scrape_website without the flag, so they inherit the legacy default (proven above). ---


@pytest.mark.asyncio
async def test_agent_step_scrape_opts_into_suppression() -> None:
    browser_state = MagicMock()
    browser_state.scrape_website = AsyncMock(return_value=MagicMock())

    task = MagicMock()
    task.url = "https://example.com"
    step = MagicMock()

    fake_app = MagicMock()
    fake_app.AGENT_FUNCTION.cleanup_element_tree_factory = MagicMock(return_value=AsyncMock())
    fake_app.scrape_exclude = AsyncMock()

    with patch("skyvern.forge.agent.app", fake_app):
        # unbound call with a mock self: _scrape_with_type only touches module-level app/settings.
        await ForgeAgent._scrape_with_type(
            MagicMock(),
            task=task,
            step=step,
            browser_state=browser_state,
            scrape_type=ScrapeType.NORMAL,
            engine=MagicMock(),  # not a CUA engine
        )

    assert browser_state.scrape_website.await_args.kwargs["allow_transient_ui_suppression"] is True
