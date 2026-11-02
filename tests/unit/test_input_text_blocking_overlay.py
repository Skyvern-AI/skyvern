"""SKY-19100: a pop-up control covering a text field must not receive the typing.

``input_text`` retargets a fill onto whatever overlaps the field when that blocker reports
editable. The driver's editability check follows a ``<label>`` to its control and answers for the
control, so a pop-up's radio label covering an email field reports editable -- then every fill
routed through it is rejected by type ("doesn't support text input") and the step burns its
retries. ``accepts_text_fill()`` answers for the node a fill would really land on, so the
retarget survives only where it can work (a text field's own label, a wrapper) and a pop-up
control is reported as covering the field instead.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from playwright.async_api import Page, async_playwright

from skyvern.exceptions import InputElementCoveredByUntypeableElement
from skyvern.forge.sdk.models import StepStatus
from skyvern.webeye.actions.actions import InputOrSelectContext, InputTextAction
from skyvern.webeye.actions.handler import handle_input_text_action
from skyvern.webeye.actions.responses import ActionFailure
from skyvern.webeye.utils.dom import SkyvernElement
from tests.unit.conftest import make_input_element_mock
from tests.unit.helpers import make_organization, make_step, make_task

_NOW = datetime.now(UTC)
_ORG = make_organization(_NOW)
_TASK = make_task(_NOW, _ORG, navigation_payload={}, navigation_goal="submit the contact form")
_STEP = make_step(_NOW, _TASK, step_id="stp-blocking-overlay", status=StepStatus.created, order=0, output=None)

_EMAIL = "someone@example.com"

# A pop-up painted over the email field, as the reported runs render it: the field's own <label>
# overlaps it harmlessly, the pop-up's radio label is what the hit test lands on.
_POPUP_FIXTURE_HTML = """
<html><body>
  <form>
    <label id="emaillbl" for="email">Confirmation Email</label>
    <input id="email" type="email" />
    <textarea id="notes"></textarea>
    <select id="state"><option>CA</option></select>
  </form>
  <div id="popup" style="position:fixed; left:0; top:0; width:420px; height:220px; background:#fff; z-index:9">
    <p>Pick one to continue</p>
    <label id="yeslbl" for="yes" style="display:block; padding:40px">Yes<input id="yes" type="radio" name="c"/></label>
    <label id="nolbl"><span id="nospan">No</span><input id="no" type="radio" name="c"/></label>
  </div>
  <div id="richtext" contenteditable="true">notes</div>
  <label id="filelbl" for="upload">Upload</label><input id="upload" type="file" />
</body></html>
"""


def _has_playwright_browser() -> bool:
    try:
        from playwright.sync_api import sync_playwright  # noqa: PLC0415

        with sync_playwright() as playwright:
            return Path(playwright.chromium.executable_path).exists()
    except Exception:
        return False


_skip_no_browser = pytest.mark.skipif(
    not _has_playwright_browser(),
    reason="Requires Playwright browsers installed (run: playwright install chromium)",
)


@contextlib.asynccontextmanager
async def _popup_fixture_page() -> AsyncIterator[Page]:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, args=["--use-mock-keychain", "--password-store=basic"])
        try:
            page = await browser.new_page()
            await page.set_content(_POPUP_FIXTURE_HTML)
            yield page
        finally:
            await browser.close()


def _real_element(page: Page, css_id: str, tag_name: str) -> SkyvernElement:
    static = {"id": css_id.upper(), "tagName": tag_name, "attributes": {"id": css_id}}
    return SkyvernElement(page.locator(f"#{css_id}"), page, static)


@pytest.mark.asyncio
@_skip_no_browser
@pytest.mark.parametrize(
    ("css_id", "tag_name", "fillable"),
    [
        # The reported shape: a pop-up radio's label, and the same label with the text in a <span>.
        ("yeslbl", "label", False),
        ("nospan", "span", False),
        # Other controls whose label the driver also reports as editable.
        ("filelbl", "label", False),
        ("state", "select", False),
        ("popup", "div", False),
        # The retarget's real beneficiaries: a text field, its own label, a contenteditable surface.
        ("emaillbl", "label", True),
        ("email", "input", True),
        ("notes", "textarea", True),
        ("richtext", "div", True),
    ],
)
async def test_accepts_text_fill_tracks_what_the_driver_will_actually_fill(
    css_id: str, tag_name: str, fillable: bool
) -> None:
    async with _popup_fixture_page() as page:
        element = _real_element(page, css_id, tag_name)
        assert await element.accepts_text_fill() is fillable

        # The predicate's whole job is to agree with the live fill where is_editable() does not.
        try:
            await element.input_fill(_EMAIL, timeout=1500)
            driver_filled = True
        except Exception:
            driver_filled = False
        assert driver_filled is fillable


async def _run_input_text(blocker: MagicMock | None) -> tuple[list, MagicMock, MagicMock]:
    target = make_input_element_mock(element_id="AABH", attrs={"type": "email"})
    if blocker is not None:
        target.find_blocking_element = AsyncMock(return_value=(blocker, True))

    dom_instance = MagicMock()
    dom_instance.get_skyvern_element_by_id = AsyncMock(return_value=target)
    dom_instance.safe_get_skyvern_element_by_id = AsyncMock(return_value=target)

    incremental = MagicMock()
    incremental.start_listen_dom_increment = AsyncMock()
    incremental.stop_listen_dom_increment = AsyncMock()
    incremental.get_incremental_element_tree = AsyncMock(return_value=[])

    skyvern_frame = MagicMock()
    skyvern_frame.safe_wait_for_animation_end = AsyncMock()

    scraped_page = MagicMock()
    scraped_page.id_to_element_dict = {"AABH": {"tagName": "input"}}

    with (
        patch("skyvern.webeye.actions.handler.DomUtil", return_value=dom_instance),
        patch("skyvern.webeye.actions.handler.SkyvernFrame.create_instance", new=AsyncMock(return_value=skyvern_frame)),
        patch("skyvern.webeye.actions.handler.IncrementalScrapePage", return_value=incremental),
        patch("skyvern.webeye.actions.handler.get_input_value", new=AsyncMock(return_value="")),
        patch(
            "skyvern.webeye.actions.handler.get_actual_value_of_parameter_if_secret_with_task",
            return_value=_EMAIL,
        ),
        patch(
            "skyvern.webeye.actions.handler._get_input_or_select_context",
            new=AsyncMock(return_value=InputOrSelectContext()),
        ),
    ):
        results = await handle_input_text_action(
            action=InputTextAction(element_id="AABH", text=_EMAIL, reasoning="fill the confirmation email"),
            page=MagicMock(),
            scraped_page=scraped_page,
            task=_TASK,
            step=_STEP,
        )
    return results, target, blocker


def _blocker(*, tag_name: str, accepts_text_fill: bool | None) -> MagicMock:
    blocker = make_input_element_mock(element_id="AAA8")
    blocker.get_tag_name.return_value = tag_name
    blocker.accepts_text_fill = AsyncMock(return_value=accepts_text_fill)
    return blocker


@pytest.mark.asyncio
async def test_untypeable_blocker_reports_the_cover_instead_of_typing_into_it() -> None:
    results, target, blocker = await _run_input_text(_blocker(tag_name="label", accepts_text_fill=False))

    assert len(results) == 1
    assert isinstance(results[0], ActionFailure)
    assert results[0].exception_type == InputElementCoveredByUntypeableElement.__name__
    message = results[0].exception_message or ""
    # The agent needs both ids to know which pop-up to clear and which field to come back to.
    assert "AAA8" in message and "AABH" in message
    blocker.input_sequentially.assert_not_awaited()
    blocker.input_fill.assert_not_awaited()
    blocker.input_clear.assert_not_awaited()
    target.input_sequentially.assert_not_awaited()
    target.input_fill.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("accepts_text_fill", [True, None])
async def test_typeable_or_unreadable_blocker_keeps_the_retarget(accepts_text_fill: bool | None) -> None:
    # None is an unreadable live node: without positive evidence the blocker can't take text, the
    # long-standing retarget stands rather than inventing a cover report.
    results, target, blocker = await _run_input_text(_blocker(tag_name="input", accepts_text_fill=accepts_text_fill))

    assert not any(isinstance(result, ActionFailure) for result in results)
    blocker.input_fill.assert_awaited_once_with(_EMAIL)
    target.input_fill.assert_not_awaited()
