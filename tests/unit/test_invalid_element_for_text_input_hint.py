from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyvern.exceptions import InvalidElementForTextInput, SkyvernException
from skyvern.forge.sdk.models import StepStatus
from skyvern.webeye.actions.actions import InputOrSelectContext, InputTextAction
from skyvern.webeye.actions.handler import handle_input_text_action
from skyvern.webeye.actions.responses import ActionFailure
from tests.unit.helpers import make_organization, make_step, make_task


def test_default_message_stays_generic_when_not_date_related() -> None:
    err = InvalidElementForTextInput(element_id="AAAA", tag_name="span")
    msg = str(err)
    assert "doesn't support text input" in msg
    assert "AAAA" in msg
    assert "span" in msg
    assert "date" not in msg.lower()
    assert "calendar" not in msg.lower()
    assert "picker" not in msg.lower()
    assert "stepper" not in msg.lower()


def test_explicit_non_date_flag_keeps_generic_message() -> None:
    err = InvalidElementForTextInput(element_id="AAAA", tag_name="span", is_date_related=False)
    msg = str(err)
    assert "doesn't support text input" in msg
    assert "date" not in msg.lower()


def test_date_related_failure_appends_actionable_hint() -> None:
    err = InvalidElementForTextInput(element_id="AAAA", tag_name="span", is_date_related=True)
    msg = str(err)
    assert "doesn't support text input" in msg
    assert "AAAA" in msg
    lower = msg.lower()
    assert "date" in lower
    assert "calendar" in lower or "date picker" in lower
    assert "stepper" in lower


def test_is_subclass_of_skyvern_exception() -> None:
    err = InvalidElementForTextInput(element_id="x", tag_name="span", is_date_related=True)
    assert isinstance(err, SkyvernException)


def test_signature_is_keyword_compatible_with_legacy_callers() -> None:
    err = InvalidElementForTextInput("AAAA", "span")
    assert "doesn't support text input" in str(err)


_NOW = datetime.now(UTC)
_ORG = make_organization(_NOW)
_TASK = make_task(_NOW, _ORG, navigation_payload={}, navigation_goal="set the from date")
_STEP = make_step(_NOW, _TASK, step_id="stp-date-hint-1", status=StepStatus.created, order=0, output=None)


def _mock_span_segment_element() -> MagicMock:
    el = MagicMock()
    el.get_id.return_value = "AAAA"
    el.get_tag_name.return_value = "span"
    el.get_frame.return_value = MagicMock()
    el.get_frame_id.return_value = "frame-1"
    locator = MagicMock()
    locator.focus = AsyncMock()
    el.get_locator.return_value = locator
    el.is_disabled = AsyncMock(return_value=False)
    el.get_selectable = AsyncMock(return_value=False)
    el.has_hidden_attr = AsyncMock(return_value=False)
    el.is_readonly = AsyncMock(return_value=False)
    el.get_attr = AsyncMock(return_value=None)
    el.is_spinbtn_input = AsyncMock(return_value=False)
    el.is_editable = AsyncMock(return_value=False)
    el.is_visible = AsyncMock(return_value=True)
    el.is_raw_input = AsyncMock(return_value=False)
    el.is_auto_completion_input = AsyncMock(return_value=False)
    el.find_blocking_element = AsyncMock(return_value=(None, False))
    el.get_element_handler = AsyncMock(return_value=MagicMock())
    el.input_clear = AsyncMock(side_effect=Exception("span cannot be cleared"))
    el.scroll_into_view = AsyncMock()
    return el


async def _run_failure_path(resolved_context: InputOrSelectContext | None) -> list:
    el = _mock_span_segment_element()

    dom_instance = MagicMock()
    dom_instance.get_skyvern_element_by_id = AsyncMock(return_value=el)

    inc = MagicMock()
    inc.start_listen_dom_increment = AsyncMock()
    inc.stop_listen_dom_increment = AsyncMock()
    inc.get_incremental_element_tree = AsyncMock(return_value=[])

    skyvern_frame = MagicMock()
    skyvern_frame.safe_wait_for_animation_end = AsyncMock()

    scraped_page = MagicMock()
    scraped_page.id_to_element_dict = {"AAAA": {"tagName": "span"}}

    # Simulate the minimal/legacy-parsed path: raw context is None; the resolved local var carries the truth.
    action = InputTextAction(element_id="AAAA", text="01", reasoning="set day segment")
    assert action.input_or_select_context is None

    with (
        patch("skyvern.webeye.actions.handler.DomUtil", return_value=dom_instance),
        patch(
            "skyvern.webeye.actions.handler.SkyvernFrame.create_instance",
            new=AsyncMock(return_value=skyvern_frame),
        ),
        patch("skyvern.webeye.actions.handler.IncrementalScrapePage", return_value=inc),
        patch("skyvern.webeye.actions.handler.get_input_value", new=AsyncMock(return_value="")),
        patch(
            "skyvern.webeye.actions.handler.get_actual_value_of_parameter_if_secret_with_task",
            return_value="01",
        ),
        patch(
            "skyvern.webeye.actions.handler._get_input_or_select_context",
            new=AsyncMock(return_value=resolved_context),
        ),
    ):
        return await handle_input_text_action(
            action=action, page=MagicMock(), scraped_page=scraped_page, task=_TASK, step=_STEP
        )


@pytest.mark.asyncio
async def test_resolved_local_context_drives_date_hint_for_minimal_action() -> None:
    resolved = InputOrSelectContext(field="From date - Day", is_date_related=True, date_format="dd")
    results = await _run_failure_path(resolved)
    assert len(results) == 1
    assert isinstance(results[0], ActionFailure)
    message = results[0].exception_message or ""
    assert "doesn't support text input" in message
    lower = message.lower()
    assert "calendar" in lower or "date picker" in lower


@pytest.mark.asyncio
async def test_no_hint_when_resolved_context_is_not_date_related() -> None:
    resolved = InputOrSelectContext(field="Account number", is_date_related=False)
    results = await _run_failure_path(resolved)
    assert len(results) == 1
    assert isinstance(results[0], ActionFailure)
    message = results[0].exception_message or ""
    assert "doesn't support text input" in message
    assert "calendar" not in message.lower()
    assert "date picker" not in message.lower()


@pytest.mark.asyncio
async def test_no_hint_when_resolved_context_is_none() -> None:
    results = await _run_failure_path(None)
    assert len(results) == 1
    assert isinstance(results[0], ActionFailure)
    message = results[0].exception_message or ""
    assert "doesn't support text input" in message
    assert "calendar" not in message.lower()
