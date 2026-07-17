from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyvern.exceptions import MaskedInputReadbackMismatch
from skyvern.forge.sdk.models import StepStatus
from skyvern.webeye.actions.actions import InputTextAction
from skyvern.webeye.actions.handler import (
    _committed_value_is_masked_incomplete,
    _has_input_mask_evidence,
    _verify_masked_input_after_fill,
    handle_input_text_action,
)
from skyvern.webeye.actions.responses import ActionFailure
from tests.unit.conftest import make_input_element_mock
from tests.unit.helpers import make_organization, make_step, make_task

_NOW = datetime.now(UTC)
_ORG = make_organization(_NOW)
_TASK = make_task(_NOW, _ORG, navigation_payload={}, navigation_goal="Fill the postal code field")
_STEP = make_step(_NOW, _TASK, step_id="stp-masked-readback", status=StepStatus.created, order=0, output=None)


def test_mask_evidence_from_pattern() -> None:
    assert _has_input_mask_evidence(pattern=r"\d{5}(-\d{4})?", placeholder=None) is True


def test_mask_evidence_from_placeholder_slots() -> None:
    assert _has_input_mask_evidence(pattern=None, placeholder="_____") is True
    assert _has_input_mask_evidence(pattern=None, placeholder="___-__-____") is True


def test_no_mask_evidence_for_plain_field() -> None:
    assert _has_input_mask_evidence(pattern=None, placeholder="Enter ZIP") is False
    assert _has_input_mask_evidence(pattern=None, placeholder=None) is False


def test_incomplete_when_commit_is_empty() -> None:
    assert _committed_value_is_masked_incomplete("12345", "") is True
    assert _committed_value_is_masked_incomplete("12345", None) is True


def test_incomplete_when_only_placeholders_remain() -> None:
    assert _committed_value_is_masked_incomplete("12345", "_____") is True
    assert _committed_value_is_masked_incomplete("12345", "123__") is True


def test_complete_when_value_committed() -> None:
    assert _committed_value_is_masked_incomplete("12345", "12345") is False
    # A ZIP+4 field that reformats the committed value is complete, not incomplete.
    assert _committed_value_is_masked_incomplete("12345", "12345-6789") is False


def test_placeholder_char_inside_complete_value_is_not_incomplete() -> None:
    # An underscore that is part of a fully-typed value must not read as an unfilled mask slot.
    assert _committed_value_is_masked_incomplete("foo_bar", "foo_bar") is False


def test_nothing_expected_is_never_incomplete() -> None:
    assert _committed_value_is_masked_incomplete("", "") is False
    assert _committed_value_is_masked_incomplete("---", "_____") is False


def _mock_element(input_values: list[str | None]) -> MagicMock:
    locator = MagicMock()
    locator.input_value = AsyncMock(side_effect=input_values)
    element = MagicMock()
    element.get_locator = MagicMock(return_value=locator)
    return element


def _mock_frame() -> MagicMock:
    frame = MagicMock()
    frame.safe_wait_for_animation_end = AsyncMock()
    return frame


@pytest.mark.asyncio
async def test_masked_readback_raises_on_empty_commit() -> None:
    element = _mock_element(["", ""])
    with pytest.raises(MaskedInputReadbackMismatch) as exc:
        await _verify_masked_input_after_fill(
            skyvern_element=element,
            skyvern_frame=_mock_frame(),
            tag_name="input",
            expected_value="12345",
            known_masked=True,
        )
    assert exc.value.expected_char_count == 5
    assert exc.value.committed_char_count == 0


@pytest.mark.asyncio
async def test_masked_readback_accepts_committed_value() -> None:
    element = _mock_element(["12345"])
    await _verify_masked_input_after_fill(
        skyvern_element=element,
        skyvern_frame=_mock_frame(),
        tag_name="input",
        expected_value="12345",
        known_masked=True,
    )


@pytest.mark.asyncio
async def test_masked_readback_settles_before_declaring_failure() -> None:
    # First read is still empty, the value lands after the settle wait — no failure.
    element = _mock_element(["", "12345"])
    await _verify_masked_input_after_fill(
        skyvern_element=element,
        skyvern_frame=_mock_frame(),
        tag_name="input",
        expected_value="12345",
        known_masked=True,
    )


@pytest.mark.asyncio
async def test_masked_readback_skips_field_without_mask_signal() -> None:
    # No attribute evidence and no placeholder chars in the commit: leave the field alone even if empty.
    element = _mock_element([""])
    await _verify_masked_input_after_fill(
        skyvern_element=element,
        skyvern_frame=_mock_frame(),
        tag_name="input",
        expected_value="12345",
        known_masked=False,
    )


@pytest.mark.asyncio
async def test_masked_readback_triggers_on_placeholder_commit_without_attr_evidence() -> None:
    element = _mock_element(["_____", "_____"])
    with pytest.raises(MaskedInputReadbackMismatch):
        await _verify_masked_input_after_fill(
            skyvern_element=element,
            skyvern_frame=_mock_frame(),
            tag_name="input",
            expected_value="12345",
            known_masked=False,
        )


async def _run_masked_input_text(text: str, read_backs: list[str | None]) -> list:
    element = make_input_element_mock()
    dom_instance = MagicMock()
    dom_instance.get_skyvern_element_by_id = AsyncMock(return_value=element)

    inc = MagicMock()
    inc.start_listen_dom_increment = AsyncMock()
    inc.stop_listen_dom_increment = AsyncMock()
    inc.get_incremental_element_tree = AsyncMock(return_value=[])

    skyvern_frame = MagicMock()
    skyvern_frame.safe_wait_for_animation_end = AsyncMock()

    scraped_page = MagicMock()
    scraped_page.id_to_element_dict = {"AADC": {"tagName": "input"}}

    with (
        patch("skyvern.webeye.actions.handler.DomUtil", return_value=dom_instance),
        patch("skyvern.webeye.actions.handler.SkyvernFrame.create_instance", new=AsyncMock(return_value=skyvern_frame)),
        patch("skyvern.webeye.actions.handler.IncrementalScrapePage", return_value=inc),
        patch("skyvern.webeye.actions.handler.get_input_value", new=AsyncMock(side_effect=read_backs)),
        patch("skyvern.webeye.actions.handler.get_actual_value_of_parameter_if_secret_with_task", return_value=text),
        patch("skyvern.webeye.actions.handler._get_input_or_select_context", new=AsyncMock(return_value=None)),
        patch("skyvern.webeye.actions.handler._is_masked_input_readback_fix_enabled", new=AsyncMock(return_value=True)),
    ):
        return await handle_input_text_action(
            action=InputTextAction(element_id="AADC", text=text, reasoning="fill postal code"),
            page=MagicMock(),
            scraped_page=scraped_page,
            task=_TASK,
            step=_STEP,
        )


@pytest.mark.asyncio
async def test_masked_retype_swallowed_by_mask_fails_loudly() -> None:
    # The field advertises no mask attrs, so the only mask evidence is the placeholder chars seen on the
    # first read-back. The retype is then swallowed whole and reads back empty: without carrying that
    # evidence into the retry, the empty commit looks like an unmasked field and passes silently.
    results = await _run_masked_input_text("12345", ["", "12_45", "12_45", "", ""])

    assert len(results) == 1
    assert isinstance(results[0], ActionFailure)
    assert results[0].exception_type == MaskedInputReadbackMismatch.__name__
