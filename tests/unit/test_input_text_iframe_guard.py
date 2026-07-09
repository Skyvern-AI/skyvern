"""Regression tests for SKY-12019.

The planner occasionally selects a non-editable element (e.g. an hCaptcha iframe)
as the target for an INPUT_TEXT action. Execution would then attempt
``Locator.clear``/``Locator.fill`` on that iframe and fail mid-step.

``SkyvernElement.supports_text_input`` guards clear/fill so they only target
editable elements (input/textarea/select/contenteditable), and
``handle_input_text_action`` rejects everything else up front with a clear error.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyvern.exceptions import InvalidElementForTextInput
from skyvern.forge.sdk.models import StepStatus
from skyvern.webeye.actions.actions import InputTextAction
from skyvern.webeye.actions.handler import handle_input_text_action
from skyvern.webeye.actions.responses import ActionFailure
from skyvern.webeye.utils.dom import SkyvernElement
from tests.unit.helpers import make_organization, make_step, make_task

_NOW = datetime.now(UTC)
_ORG = make_organization(_NOW)
_TASK = make_task(_NOW, _ORG, navigation_payload={}, navigation_goal="Log in")
_STEP = make_step(_NOW, _TASK, step_id="stp-1", status=StepStatus.created, order=0, output=None)


def _make_element(tag_name: str, *, editable: bool, attributes: dict | None = None) -> SkyvernElement:
    locator = MagicMock()
    locator.is_editable = AsyncMock(return_value=editable)
    static_element = {"id": "EL", "tagName": tag_name, "attributes": attributes or {}}
    return SkyvernElement(locator=locator, frame=MagicMock(), static_element=static_element)


# --------------------------------------------------------------------------- #
# SkyvernElement.supports_text_input
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_iframe_does_not_support_text_input() -> None:
    element = _make_element("iframe", editable=False)
    assert await element.supports_text_input() is False


@pytest.mark.asyncio
@pytest.mark.parametrize("tag_name", ["input", "textarea", "select", "INPUT"])
async def test_common_input_tags_support_text_input(tag_name: str) -> None:
    # short-circuits on tag name, without consulting locator.is_editable
    element = _make_element(tag_name, editable=False)
    assert await element.supports_text_input() is True


@pytest.mark.asyncio
async def test_editable_element_supports_text_input() -> None:
    element = _make_element("div", editable=True)
    assert await element.supports_text_input() is True


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["", "true", "plaintext-only"])
async def test_contenteditable_supports_text_input(value: str) -> None:
    element = _make_element("div", editable=False, attributes={"contenteditable": value})
    assert await element.supports_text_input() is True


@pytest.mark.asyncio
async def test_contenteditable_false_does_not_support_text_input() -> None:
    element = _make_element("div", editable=False, attributes={"contenteditable": "false"})
    assert await element.supports_text_input() is False


# --------------------------------------------------------------------------- #
# handle_input_text_action — iframe target is rejected before clear/fill
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_input_text_on_iframe_is_rejected() -> None:
    skyvern_el = MagicMock()
    skyvern_el.get_id.return_value = "IFRM"
    skyvern_el.get_tag_name.return_value = "iframe"
    skyvern_el.get_frame.return_value = MagicMock()
    skyvern_el.get_locator.return_value = MagicMock()
    skyvern_el.is_disabled = AsyncMock(return_value=False)
    skyvern_el.supports_text_input = AsyncMock(return_value=False)
    skyvern_el.input_clear = AsyncMock()
    skyvern_el.input_fill = AsyncMock()
    skyvern_el.get_selectable = AsyncMock(return_value=False)

    dom_instance = MagicMock()
    dom_instance.get_skyvern_element_by_id = AsyncMock(return_value=skyvern_el)

    skyvern_frame = MagicMock()
    skyvern_frame.safe_wait_for_animation_end = AsyncMock()

    scraped_page = MagicMock()
    scraped_page.id_to_element_dict = {"IFRM": {"tagName": "iframe"}}

    action = InputTextAction(element_id="IFRM", text="hello", reasoning="fill the field")

    with (
        patch("skyvern.webeye.actions.handler.DomUtil", return_value=dom_instance),
        patch(
            "skyvern.webeye.actions.handler.SkyvernFrame.create_instance",
            new=AsyncMock(return_value=skyvern_frame),
        ),
        patch("skyvern.webeye.actions.handler.IncrementalScrapePage", return_value=MagicMock()),
        patch("skyvern.webeye.actions.handler.get_input_value", new=AsyncMock(return_value="")),
        patch(
            "skyvern.webeye.actions.handler.get_actual_value_of_parameter_if_secret_with_task",
            return_value="hello",
        ),
    ):
        results = await handle_input_text_action(
            action=action, page=MagicMock(), scraped_page=scraped_page, task=_TASK, step=_STEP
        )

    assert len(results) == 1
    assert isinstance(results[0], ActionFailure)
    assert results[0].exception_type == InvalidElementForTextInput.__name__
    skyvern_el.input_clear.assert_not_called()
    skyvern_el.input_fill.assert_not_called()
