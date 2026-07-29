"""input_text wrapper -> nested-input retarget (cross-origin secure card-entry fields).

The visible card-field target is a wrapper <div>/<iframe> (interactable: false); the usable
<input> lives one frame deeper (a distinct frame_index, interactable: true). Without a retarget,
input_text rejects the wrapper and relies on the LLM to descend on retry -- which it does only
inconsistently. These tests pin the deterministic, fail-closed retarget and its production wiring.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyvern.exceptions import InputToInvisibleElement, InteractWithDisabledElement, InvalidElementForTextInput
from skyvern.forge.sdk.models import StepStatus
from skyvern.webeye.actions.actions import InputTextAction
from skyvern.webeye.actions.handler import _retarget_wrapper_for_input_text, handle_input_text_action
from skyvern.webeye.actions.responses import ActionFailure, ActionSuccess
from skyvern.webeye.utils.dom import SkyvernElement
from tests.unit.helpers import make_organization, make_step, make_task

_NOW = datetime.now(UTC)
_ORG = make_organization(_NOW)
_TASK = make_task(_NOW, _ORG, navigation_payload={}, navigation_goal="enter the card number")
_STEP = make_step(_NOW, _TASK, step_id="stp-wrap-retarget-1", status=StepStatus.created, order=0, output=None)

_CARD = "4111111111111111"


def _mock_wrapper(*, deepest_descendant: str | None) -> MagicMock:
    el = MagicMock()
    el.get_id.return_value = "IFRM"
    el.get_tag_name.return_value = "iframe"
    el.get_frame.return_value = MagicMock()
    el.supports_text_input = AsyncMock(return_value=False)
    el.has_hidden_attr = AsyncMock(return_value=False)
    el.find_deepest_interactable_descendant_in_single_chain = MagicMock(return_value=deepest_descendant)
    # Reached only if a reject path runs on the wrapper itself (fail-closed cases).
    el.is_disabled = AsyncMock(return_value=False)
    el.get_selectable = AsyncMock(return_value=False)
    el.is_readonly = AsyncMock(return_value=False)
    el.get_attr = AsyncMock(return_value=None)
    el.is_spinbtn_input = AsyncMock(return_value=False)
    el.is_editable = AsyncMock(return_value=False)
    el.is_raw_input = AsyncMock(return_value=False)
    el.is_visible = AsyncMock(return_value=True)
    el.find_blocking_element = AsyncMock(return_value=(None, False))
    el.get_element_handler = AsyncMock(return_value=MagicMock())
    el.input_clear = AsyncMock()
    el.input_fill = AsyncMock()
    el.scroll_into_view = AsyncMock()
    locator = MagicMock()
    locator.focus = AsyncMock()
    el.get_locator.return_value = locator
    return el


def _mock_nested_input(*, element_id: str, supports_text: bool, disabled: bool = False) -> MagicMock:
    el = MagicMock()
    el.get_id.return_value = element_id
    el.get_tag_name.return_value = "input"
    el.get_frame.return_value = MagicMock()
    el.supports_text_input = AsyncMock(return_value=supports_text)
    el.is_disabled = AsyncMock(return_value=disabled)
    el.has_hidden_attr = AsyncMock(return_value=False)
    el.is_visible = AsyncMock(return_value=True)
    locator = MagicMock()
    locator.focus = AsyncMock()
    el.get_locator.return_value = locator
    return el


async def _run(action: InputTextAction, wrapper: MagicMock, child: MagicMock | None) -> tuple[list, list[str]]:
    elements: dict = {"IFRM": wrapper}
    if child is not None:
        elements["CARD_INPUT"] = child
    dom_instance = MagicMock()
    dom_instance.get_skyvern_element_by_id = AsyncMock(side_effect=lambda eid: elements[eid])
    dom_instance.safe_get_skyvern_element_by_id = AsyncMock(side_effect=lambda eid: elements.get(eid))

    skyvern_frame = MagicMock()
    skyvern_frame.safe_wait_for_animation_end = AsyncMock()

    scraped_page = MagicMock()
    scraped_page.id_to_element_dict = {"IFRM": {"tagName": "iframe"}, "CARD_INPUT": {"tagName": "input"}}

    # Record which element's tag each current-value read hits. Early normalization reads the value once,
    # from the normalized target; a recursive re-run (the design we avoid) would read twice -- the
    # wrapper's iframe first, then the nested input. The nested <input> reads back the card value so a
    # successful retarget short-circuits to success, proving the child drove the pipeline.
    read_tags: list[str] = []

    async def _get_input_value(tag_name: str, locator: object, engine_selection: object = None) -> str:
        read_tags.append(tag_name)
        return _CARD if tag_name == "input" else ""

    with (
        patch("skyvern.webeye.actions.handler.DomUtil", return_value=dom_instance),
        patch(
            "skyvern.webeye.actions.handler.SkyvernFrame.create_instance",
            new=AsyncMock(return_value=skyvern_frame),
        ),
        patch("skyvern.webeye.actions.handler.IncrementalScrapePage", return_value=MagicMock()),
        patch("skyvern.webeye.actions.handler.get_input_value", new=AsyncMock(side_effect=_get_input_value)),
        patch(
            "skyvern.webeye.actions.handler.get_actual_value_of_parameter_if_secret_with_task",
            return_value=_CARD,
        ),
        patch("skyvern.webeye.actions.handler._get_input_or_select_context", new=AsyncMock(return_value=None)),
    ):
        results = await handle_input_text_action(
            action=action, page=MagicMock(), scraped_page=scraped_page, task=_TASK, step=_STEP
        )
    return results, read_tags


@pytest.mark.asyncio
async def test_wrapper_retargets_to_nested_input() -> None:
    wrapper = _mock_wrapper(deepest_descendant="CARD_INPUT")
    child = _mock_nested_input(element_id="CARD_INPUT", supports_text=True)
    action = InputTextAction(element_id="IFRM", text=_CARD, reasoning="enter card number")

    # After retargeting to the nested input, its rendered value already equals the target, so the
    # handler short-circuits to success -- proving the wrapper was resolved to the real <input>.
    results, read_tags = await _run(action, wrapper, child)

    assert action.element_id == "CARD_INPUT"
    assert len(results) == 1
    assert isinstance(results[0], ActionSuccess)
    # Exactly one current-value read, against the nested <input> tag: the normalized child drove the
    # pipeline in a single pass, with no recursive handler re-entry (which would read iframe then input).
    assert read_tags == ["input"]


@pytest.mark.asyncio
async def test_ambiguous_descendants_preserve_reject() -> None:
    # Real helper returns None for sibling/decoy inputs (separate branches). No retarget, no mis-fill.
    wrapper = _mock_wrapper(deepest_descendant=None)
    action = InputTextAction(element_id="IFRM", text=_CARD, reasoning="enter card number")

    results, read_tags = await _run(action, wrapper, None)

    assert action.element_id == "IFRM"
    assert len(results) == 1
    assert isinstance(results[0], ActionFailure)
    assert results[0].exception_type == InvalidElementForTextInput.__name__
    # Value read once from the wrapper (iframe) itself: no retarget occurred, single pass to the reject.
    assert read_tags == ["iframe"]


@pytest.mark.asyncio
async def test_nested_candidate_without_text_input_preserves_reject() -> None:
    wrapper = _mock_wrapper(deepest_descendant="CARD_INPUT")
    child = _mock_nested_input(element_id="CARD_INPUT", supports_text=False)
    action = InputTextAction(element_id="IFRM", text=_CARD, reasoning="enter card number")

    results, read_tags = await _run(action, wrapper, child)

    assert action.element_id == "IFRM"
    assert len(results) == 1
    assert isinstance(results[0], ActionFailure)
    assert results[0].exception_type == InvalidElementForTextInput.__name__
    # Nested candidate rejected by the helper's text-input gate: element unchanged, value read once from
    # the wrapper (iframe), single pass to the reject.
    assert read_tags == ["iframe"]


@pytest.mark.asyncio
async def test_hidden_non_text_wrapper_rejects_and_reads_hidden_attr_once() -> None:
    # A genuinely hidden, non-text target is still rejected as invisible; the wrapper's dynamic hidden
    # state read for the retarget gate is cached and reused at the reject block, so has_hidden_attr
    # (two Playwright round-trips) is awaited exactly once instead of twice.
    wrapper = _mock_wrapper(deepest_descendant=None)
    wrapper.has_hidden_attr = AsyncMock(return_value=True)
    action = InputTextAction(element_id="IFRM", text=_CARD, reasoning="enter card number")

    results, _ = await _run(action, wrapper, None)

    assert action.element_id == "IFRM"
    assert len(results) == 1
    assert isinstance(results[0], ActionFailure)
    assert results[0].exception_type == InputToInvisibleElement.__name__
    wrapper.has_hidden_attr.assert_awaited_once()


@pytest.mark.asyncio
async def test_selectable_wrapper_routes_to_select_not_retarget() -> None:
    # A selectable wrapper that happens to expose a unique nested input must still flow through the
    # existing input -> select-option conversion, not be retargeted/typed into the child. Selectable
    # targets are gated out of the retarget entirely, so the descendant helper is never consulted.
    wrapper = _mock_wrapper(deepest_descendant="CARD_INPUT")
    wrapper.get_selectable = AsyncMock(return_value=True)
    child = _mock_nested_input(element_id="CARD_INPUT", supports_text=True)
    action = InputTextAction(element_id="IFRM", text=_CARD, reasoning="choose the saved card")

    elements = {"IFRM": wrapper, "CARD_INPUT": child}
    dom_instance = MagicMock()
    dom_instance.get_skyvern_element_by_id = AsyncMock(side_effect=lambda eid: elements[eid])
    dom_instance.safe_get_skyvern_element_by_id = AsyncMock(side_effect=lambda eid: elements.get(eid))

    scraped_page = MagicMock()
    scraped_page.id_to_element_dict = {"IFRM": {"tagName": "iframe"}, "CARD_INPUT": {"tagName": "input"}}

    select_spy = AsyncMock(return_value=[ActionSuccess()])

    with (
        patch("skyvern.webeye.actions.handler.DomUtil", return_value=dom_instance),
        patch("skyvern.webeye.actions.handler.SkyvernFrame.create_instance", new=AsyncMock(return_value=MagicMock())),
        patch("skyvern.webeye.actions.handler.IncrementalScrapePage", return_value=MagicMock()),
        patch("skyvern.webeye.actions.handler.get_input_value", new=AsyncMock(return_value="")),
        patch("skyvern.webeye.actions.handler.get_actual_value_of_parameter_if_secret_with_task", return_value=_CARD),
        patch("skyvern.webeye.actions.handler.handle_select_option_action", new=select_spy),
    ):
        results = await handle_input_text_action(
            action=action, page=MagicMock(), scraped_page=scraped_page, task=_TASK, step=_STEP
        )

    assert isinstance(results[0], ActionSuccess)
    # Routed to select-option conversion on the ORIGINAL wrapper; the retarget path never ran.
    select_spy.assert_awaited_once()
    assert select_spy.await_args.args[0].element_id == "IFRM"
    assert action.element_id == "IFRM"
    wrapper.find_deepest_interactable_descendant_in_single_chain.assert_not_called()


@pytest.mark.asyncio
async def test_retargeted_disabled_child_waits_then_fails_closed_when_never_enabled() -> None:
    # A retargeted-but-still-disabled nested input must be waited on (wait_until_enabled) and, if it
    # never enables, fail closed on the CHILD with InteractWithDisabledElement -- not reject the
    # wrapper with InvalidElementForTextInput. Proves the child drives the normal pipeline, wait included.
    wrapper = _mock_wrapper(deepest_descendant="CARD_INPUT")
    child = _mock_nested_input(element_id="CARD_INPUT", supports_text=True, disabled=True)
    child.get_selectable = AsyncMock(return_value=False)
    action = InputTextAction(element_id="IFRM", text=_CARD, reasoning="enter card number")

    elements = {"IFRM": wrapper, "CARD_INPUT": child}
    dom_instance = MagicMock()
    dom_instance.get_skyvern_element_by_id = AsyncMock(side_effect=lambda eid: elements[eid])
    dom_instance.safe_get_skyvern_element_by_id = AsyncMock(side_effect=lambda eid: elements.get(eid))

    scraped_page = MagicMock()
    scraped_page.id_to_element_dict = {"IFRM": {"tagName": "iframe"}, "CARD_INPUT": {"tagName": "input"}}

    wait_spy = AsyncMock(return_value=False)

    with (
        patch("skyvern.webeye.actions.handler.DomUtil", return_value=dom_instance),
        patch("skyvern.webeye.actions.handler.SkyvernFrame.create_instance", new=AsyncMock(return_value=MagicMock())),
        patch("skyvern.webeye.actions.handler.IncrementalScrapePage", return_value=MagicMock()),
        patch("skyvern.webeye.actions.handler.get_input_value", new=AsyncMock(return_value="")),
        patch("skyvern.webeye.actions.handler.get_actual_value_of_parameter_if_secret_with_task", return_value=_CARD),
        patch("skyvern.webeye.actions.handler.SkyvernElement.wait_until_enabled", new=wait_spy),
    ):
        results = await handle_input_text_action(
            action=action, page=MagicMock(), scraped_page=scraped_page, task=_TASK, step=_STEP
        )

    # Retargeted to the child, the enabled-wait ran on the child, and it failed closed on the child.
    assert action.element_id == "CARD_INPUT"
    wait_spy.assert_awaited_once()
    assert wait_spy.await_args.args[0] is child
    assert len(results) == 1
    assert isinstance(results[0], ActionFailure)
    assert results[0].exception_type == InteractWithDisabledElement.__name__


# --- helper-level tests: real single-chain structural logic on the nested-iframe shape ---


def _static(
    *,
    element_id: str,
    tag_name: str,
    interactable: bool,
    hidden: bool = False,
    disabled: bool = False,
    children: list[dict] | None = None,
) -> dict:
    attrs: dict = {}
    if hidden:
        attrs["aria-hidden"] = "true"
    if disabled:
        attrs["disabled"] = True
    return {
        "id": element_id,
        "tagName": tag_name,
        "interactable": interactable,
        "hoverOnly": False,
        "attributes": attrs,
        "children": children or [],
    }


def _el(static: dict) -> SkyvernElement:
    return SkyvernElement(MagicMock(), MagicMock(), static)


def _nested_secure_field_wrapper(*, decoys_interactable: bool) -> SkyvernElement:
    # <div AADN> -> <iframe AADO (interactable:false)> -> <input aAAC (interactable:true)> (+ 3 decoys)
    real = _static(element_id="aAAC", tag_name="input", interactable=True)
    decoys = [
        _static(element_id=did, tag_name="input", interactable=decoys_interactable) for did in ("aAAB", "aAAD", "aAAE")
    ]
    iframe = _static(element_id="AADO", tag_name="iframe", interactable=False, children=[real, *decoys])
    wrapper_div = _static(element_id="AADN", tag_name="div", interactable=False, children=[iframe])
    return _el(wrapper_div)


def _stub_child(
    static: dict, *, disabled: bool = False, hidden: bool = False, supports_text: bool = True, visible: bool = True
) -> SkyvernElement:
    child = _el(static)
    child.is_disabled = AsyncMock(return_value=disabled)  # type: ignore[method-assign]
    child.has_hidden_attr = AsyncMock(return_value=hidden)  # type: ignore[method-assign]
    child.supports_text_input = AsyncMock(return_value=supports_text)  # type: ignore[method-assign]
    child.is_visible = AsyncMock(return_value=visible)  # type: ignore[method-assign]
    return child


@pytest.mark.asyncio
async def test_helper_resolves_single_nested_input_when_decoys_inert() -> None:
    parent = _nested_secure_field_wrapper(decoys_interactable=False)
    child = _stub_child(_static(element_id="aAAC", tag_name="input", interactable=True))
    dom = MagicMock()
    dom.safe_get_skyvern_element_by_id = AsyncMock(return_value=child)
    action = MagicMock()
    action.element_id = "AADN"

    result = await _retarget_wrapper_for_input_text(dom, parent, action)

    assert result is child
    assert action.element_id == "aAAC"
    dom.safe_get_skyvern_element_by_id.assert_awaited_once_with("aAAC")


@pytest.mark.asyncio
async def test_helper_bails_when_decoys_interactable() -> None:
    # Interactable decoys are siblings of the real input -> separate branches -> no unambiguous
    # target. Fail-closed: preserve the reject rather than risk typing the card into a decoy.
    parent = _nested_secure_field_wrapper(decoys_interactable=True)
    dom = MagicMock()
    dom.safe_get_skyvern_element_by_id = AsyncMock()
    action = MagicMock()
    action.element_id = "AADN"

    result = await _retarget_wrapper_for_input_text(dom, parent, action)

    assert result is None
    assert action.element_id == "AADN"
    dom.safe_get_skyvern_element_by_id.assert_not_called()


@pytest.mark.asyncio
async def test_helper_retargets_child_reporting_disabled() -> None:
    # A transiently-disabled unique nested input must still be retargeted -- the downstream
    # wait_until_enabled then waits on the real input instead of failing closed on the wrapper.
    parent = _nested_secure_field_wrapper(decoys_interactable=False)
    child = _stub_child(_static(element_id="aAAC", tag_name="input", interactable=True), disabled=True)
    dom = MagicMock()
    dom.safe_get_skyvern_element_by_id = AsyncMock(return_value=child)
    action = MagicMock()
    action.element_id = "AADN"

    result = await _retarget_wrapper_for_input_text(dom, parent, action)

    assert result is child
    assert action.element_id == "aAAC"


def _nested_secure_field_wrapper_disabled_real(*, decoys_interactable: bool) -> SkyvernElement:
    # Same shape as _nested_secure_field_wrapper, but the real input is disabled at scrape time
    # (provider still enabling it). It must remain the single retarget candidate.
    real = _static(element_id="aAAC", tag_name="input", interactable=True, disabled=True)
    decoys = [
        _static(element_id=did, tag_name="input", interactable=decoys_interactable) for did in ("aAAB", "aAAD", "aAAE")
    ]
    iframe = _static(element_id="AADO", tag_name="iframe", interactable=False, children=[real, *decoys])
    wrapper_div = _static(element_id="AADN", tag_name="div", interactable=False, children=[iframe])
    return _el(wrapper_div)


@pytest.mark.asyncio
async def test_helper_retargets_statically_disabled_single_input() -> None:
    # Scrape-time disabled unique input: the single-chain walk must still surface it, so the retarget
    # picks the real field rather than bailing to the non-fillable wrapper.
    parent = _nested_secure_field_wrapper_disabled_real(decoys_interactable=False)
    child = _stub_child(_static(element_id="aAAC", tag_name="input", interactable=True, disabled=True))
    dom = MagicMock()
    dom.safe_get_skyvern_element_by_id = AsyncMock(return_value=child)
    action = MagicMock()
    action.element_id = "AADN"

    result = await _retarget_wrapper_for_input_text(dom, parent, action)

    assert result is child
    assert action.element_id == "aAAC"
    dom.safe_get_skyvern_element_by_id.assert_awaited_once_with("aAAC")


@pytest.mark.asyncio
async def test_helper_still_bails_on_ambiguous_disabled_candidates() -> None:
    # Allowing a disabled candidate must not defeat the fail-closed ambiguity guard: a disabled real
    # input plus interactable decoy siblings are separate branches -> no unambiguous target -> reject.
    parent = _nested_secure_field_wrapper_disabled_real(decoys_interactable=True)
    dom = MagicMock()
    dom.safe_get_skyvern_element_by_id = AsyncMock()
    action = MagicMock()
    action.element_id = "AADN"

    result = await _retarget_wrapper_for_input_text(dom, parent, action)

    assert result is None
    assert action.element_id == "AADN"
    dom.safe_get_skyvern_element_by_id.assert_not_called()


@pytest.mark.asyncio
async def test_helper_bails_when_child_hidden() -> None:
    parent = _nested_secure_field_wrapper(decoys_interactable=False)
    child = _stub_child(_static(element_id="aAAC", tag_name="input", interactable=True), hidden=True)
    dom = MagicMock()
    dom.safe_get_skyvern_element_by_id = AsyncMock(return_value=child)
    action = MagicMock()
    action.element_id = "AADN"

    result = await _retarget_wrapper_for_input_text(dom, parent, action)

    assert result is None
    assert action.element_id == "AADN"


@pytest.mark.asyncio
async def test_helper_bails_when_child_missing() -> None:
    parent = _nested_secure_field_wrapper(decoys_interactable=False)
    dom = MagicMock()
    dom.safe_get_skyvern_element_by_id = AsyncMock(return_value=None)
    action = MagicMock()
    action.element_id = "AADN"

    result = await _retarget_wrapper_for_input_text(dom, parent, action)

    assert result is None
    assert action.element_id == "AADN"


@pytest.mark.asyncio
async def test_helper_bails_when_child_not_visible() -> None:
    # Time drift: a sole nested candidate with no hidden attrs can still become CSS-hidden
    # (display:none/visibility:hidden) after scrape. Fail closed rather than type the card into it.
    # (has_hidden_attr is False here, so the reject is driven purely by the rendered-visibility gate.)
    parent = _nested_secure_field_wrapper(decoys_interactable=False)
    child = _stub_child(_static(element_id="aAAC", tag_name="input", interactable=True), visible=False)
    dom = MagicMock()
    dom.safe_get_skyvern_element_by_id = AsyncMock(return_value=child)
    action = MagicMock()
    action.element_id = "AADN"

    result = await _retarget_wrapper_for_input_text(dom, parent, action)

    assert result is None
    assert action.element_id == "AADN"


@pytest.mark.asyncio
async def test_helper_retargets_when_child_visible() -> None:
    # Positive path: a rendered-visible, unambiguous nested input is still retargeted.
    parent = _nested_secure_field_wrapper(decoys_interactable=False)
    child = _stub_child(_static(element_id="aAAC", tag_name="input", interactable=True), visible=True)
    dom = MagicMock()
    dom.safe_get_skyvern_element_by_id = AsyncMock(return_value=child)
    action = MagicMock()
    action.element_id = "AADN"

    result = await _retarget_wrapper_for_input_text(dom, parent, action)

    assert result is child
    assert action.element_id == "aAAC"
