"""SKY-12325 & SKY-12317: element/frame-gone conditions are expected, not crashes.

When the LLM references an element or iframe that is no longer resolvable in the
live DOM (detached iframe -> ``NoneFrameError``; stale element id, e.g. on a
dynamically injected captcha -> ``MissingElementDict``), the action handler must
convert it into an ``ActionFailure`` (so the agent re-scrapes and retries) AND
log it as a known/expected condition -- not as ``"Unhandled exception in action
handler"`` (``LOG.exception``), which surfaces every transient DOM race as an
error in tracking.
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyvern.exceptions import (
    MissingElementDict,
    MissingElementInCSSMap,
    MissingElementInIframe,
    NoneFrameError,
)
from skyvern.forge.sdk.models import StepStatus
from skyvern.webeye.actions.actions import ClickAction
from skyvern.webeye.actions.handler import ActionHandler
from skyvern.webeye.actions.responses import ActionFailure
from skyvern.webeye.scraper.scraped_page import ScrapedPage
from tests.unit.helpers import make_organization, make_step, make_task


def _make_context() -> tuple:
    now = datetime.now(UTC)
    organization = make_organization(now)
    task = make_task(now, organization, workflow_run_id="wr-1", browser_session_id=None)
    step = make_step(now, task, step_id="step-1", status=StepStatus.created, order=0, output=None)
    page = MagicMock()
    page.url = "https://example.com/"
    scraped_page = ScrapedPage(
        elements=[],
        element_tree=[],
        element_tree_trimmed=[],
        _browser_state=MagicMock(),
        _clean_up_func=AsyncMock(return_value=[]),
        _scrape_exclude=None,
    )
    # x/y set => check_for_invalid_web_action short-circuits before element resolution.
    action = ClickAction(
        element_id="el-1",
        x=1,
        y=1,
        organization_id=task.organization_id,
        task_id=task.task_id,
        step_id=step.step_id,
    )
    return task, step, page, scraped_page, action


def _first_args(mock_method: MagicMock) -> list:
    return [call.args[0] if call.args else None for call in mock_method.call_args_list]


@pytest.mark.parametrize(
    "exc",
    [
        NoneFrameError(frame_id="iframe-1"),
        MissingElementDict("el-1"),
        MissingElementInIframe("el-1"),
        MissingElementInCSSMap("el-1"),
    ],
)
@pytest.mark.asyncio
async def test_handle_action_treats_frame_element_gone_as_known_failure(exc: Exception) -> None:
    task, step, page, scraped_page, action = _make_context()

    async def _raise(*_args, **_kwargs):
        raise exc

    mock_log = MagicMock()
    with (
        patch.object(ActionHandler, "_handled_action_types", {action.action_type: _raise}),
        patch("skyvern.webeye.actions.handler.app.AGENT_FUNCTION.wait_for_challenge_solver", new=AsyncMock()),
        patch("skyvern.webeye.actions.handler.LOG", new=mock_log),
    ):
        results = await ActionHandler._handle_action(scraped_page, task, step, page, action)

    # Still converted to a (recoverable) ActionFailure carrying the right exception.
    assert len(results) == 1
    assert isinstance(results[0], ActionFailure)
    assert results[0].exception_type == type(exc).__name__

    # Must be classified as a known/expected condition, not an unhandled exception.
    assert "Known exceptions" in _first_args(mock_log.info)
    assert "Unhandled exception in action handler" not in _first_args(mock_log.exception)
