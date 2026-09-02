"""Characterization tests for ``ForgeAgent.execute_step`` exception handling (SKY-11786).

These pin the CURRENT observable behavior of the exception handlers at the tail of
``skyvern/forge/agent.py::ForgeAgent.execute_step``. For each exception type they pin:

* whether ``fail_task`` runs,
* whether ``clean_up_task`` runs (and, for the conditional handlers, whether that is
  gated on ``fail_task`` reporting the task as failed),
* the webhook decision (``need_call_webhook``) and the final-screenshot decision
  (``need_final_screenshot``) handed to ``clean_up_task``.

They gate the SKY-11743 restructure (SKY-11787), which collapses the nine cleanup
handlers behind shared per-exception configuration; the suite must pass against current
main with no change to ``execute_step``.

Each handler is reached by making the first in-``try`` await
(``app.AGENT_FUNCTION.validate_step_execution``) raise the target exception, which lands
control directly in the matching ``except`` clause. ``fail_task`` and ``clean_up_task``
are mocked, so the assertions read the decisions off their call args and never touch a
real browser or database. Webhook/screenshot are asserted as *effective* values
(kwarg-or-default), so a restructure that makes the current defaults explicit still
passes — only a change in behavior fails.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, Callable
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest

from skyvern.config import settings as agent_settings
from skyvern.exceptions import (
    BrowserSessionAlreadyOccupiedError,
    BrowserSessionClosed,
    BrowserSessionOwnershipConflict,
    FailedToNavigateToUrl,
    FailedToParseActionInstruction,
    FailedToSendWebhook,
    InvalidTaskStatusTransition,
    MissingBrowserStatePage,
    ScrapingFailed,
    StepTerminationError,
    StepUnableToExecuteError,
    TaskAlreadyCanceled,
    TaskAlreadyTimeout,
    UnknownErrorWhileCreatingBrowserContext,
    UnsupportedActionType,
    UnsupportedTaskType,
)
from skyvern.forge.agent import ForgeAgent
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.models import StepStatus
from skyvern.webeye.browser_errors import BrowserTargetClosedError
from tests.unit.helpers import make_organization, make_step, make_task

# ``clean_up_task``'s real signature defaults; handlers that omit these kwargs get them.
_CLEANUP_WEBHOOK_DEFAULT = True
_CLEANUP_SCREENSHOT_DEFAULT = True


@dataclass
class ExecuteStepOutcome:
    returned: tuple | None
    raised: BaseException | None
    fail_task: AsyncMock
    clean_up_task: AsyncMock

    @property
    def fail_task_called(self) -> bool:
        return self.fail_task.await_count > 0

    @property
    def cleanup_called(self) -> bool:
        return self.clean_up_task.await_count > 0

    def _cleanup_kwarg(self, name: str, default: Any) -> Any:
        assert self.cleanup_called, "clean_up_task was not called"
        return self.clean_up_task.await_args.kwargs.get(name, default)

    @property
    def effective_webhook(self) -> bool:
        return bool(self._cleanup_kwarg("need_call_webhook", _CLEANUP_WEBHOOK_DEFAULT))

    @property
    def effective_final_screenshot(self) -> bool:
        return bool(self._cleanup_kwarg("need_final_screenshot", _CLEANUP_SCREENSHOT_DEFAULT))


async def _drive_execute_step(
    monkeypatch: pytest.MonkeyPatch,
    exc: BaseException,
    *,
    fail_task_result: bool = True,
) -> ExecuteStepOutcome:
    """Run ``execute_step`` so that ``exc`` is raised at the first in-``try`` await."""
    agent = ForgeAgent()
    now = datetime.now(UTC)
    organization = make_organization(now)
    task = make_task(now, organization)
    step = make_step(now, task, step_id="step-0", status=StepStatus.running, order=0, output=None)

    fail_task_mock = AsyncMock(return_value=fail_task_result)
    clean_up_task_mock = AsyncMock(return_value=None)
    agent.fail_task = fail_task_mock  # type: ignore[method-assign]
    agent.clean_up_task = clean_up_task_mock  # type: ignore[method-assign]

    # Pre-``try`` DB reads in execute_step: keep them inert so we reach the try body.
    monkeypatch.setattr("skyvern.forge.agent.app.DATABASE.tasks.get_task", AsyncMock(return_value=None))
    monkeypatch.setattr("skyvern.forge.agent.app.DATABASE.tasks.update_task", AsyncMock(return_value=task))
    # First in-``try`` await; raising here routes straight to the matching except clause.
    monkeypatch.setattr(
        "skyvern.forge.agent.app.AGENT_FUNCTION.validate_step_execution",
        AsyncMock(side_effect=exc),
    )

    context = SkyvernContext(
        organization_id=organization.organization_id,
        task_id=task.task_id,
        step_id=None,
        tz_info=ZoneInfo("UTC"),
    )
    skyvern_context.set(context)
    returned: tuple | None = None
    raised: BaseException | None = None
    try:
        returned = await agent.execute_step(
            organization=organization,
            task=task,
            step=step,
            api_key="api-key",
            download_baseline_files=[],
        )
    except BaseException as caught:  # noqa: BLE001 - we characterize which exceptions propagate
        raised = caught
    finally:
        skyvern_context.reset()

    return ExecuteStepOutcome(
        returned=returned,
        raised=raised,
        fail_task=fail_task_mock,
        clean_up_task=clean_up_task_mock,
    )


@dataclass(frozen=True)
class CleanupHandlerCase:
    """Pinned contract for one cleanup ``except`` clause in ``execute_step``."""

    id: str
    exc_factory: Callable[[], BaseException]
    fail_task_called: bool
    effective_webhook: bool
    effective_final_screenshot: bool
    # True => clean_up_task runs only when fail_task reports the task as failed.
    cleanup_gated_on_fail_task: bool


# The nine cleanup ``except`` clauses (SKY-11743's "nine cleanup handlers"). The
# unsupported-* clause catches three exception types and is exercised by all three below.
CLEANUP_CASES: list[CleanupHandlerCase] = [
    CleanupHandlerCase(
        id="task_already_timeout",
        exc_factory=lambda: TaskAlreadyTimeout("task-123"),
        fail_task_called=False,
        effective_webhook=True,
        effective_final_screenshot=True,
        cleanup_gated_on_fail_task=False,
    ),
    CleanupHandlerCase(
        id="step_termination",
        exc_factory=lambda: StepTerminationError("terminated", step_id="step-0", task_id="task-123"),
        fail_task_called=True,
        effective_webhook=True,
        effective_final_screenshot=True,
        cleanup_gated_on_fail_task=True,
    ),
    CleanupHandlerCase(
        id="failed_to_navigate",
        exc_factory=lambda: FailedToNavigateToUrl("https://example.com", "boom"),
        fail_task_called=True,
        effective_webhook=True,
        effective_final_screenshot=False,  # the only handler that suppresses the final screenshot
        cleanup_gated_on_fail_task=True,
    ),
    CleanupHandlerCase(
        id="task_already_canceled",
        exc_factory=lambda: TaskAlreadyCanceled("failed", "task-123"),
        fail_task_called=False,
        effective_webhook=False,
        effective_final_screenshot=True,
        cleanup_gated_on_fail_task=False,
    ),
    CleanupHandlerCase(
        id="invalid_status_transition",
        exc_factory=lambda: InvalidTaskStatusTransition("running", "failed", "task-123"),
        fail_task_called=False,
        effective_webhook=False,
        effective_final_screenshot=True,
        cleanup_gated_on_fail_task=False,
    ),
    CleanupHandlerCase(
        id="unsupported_action_type",
        exc_factory=lambda: UnsupportedActionType("MYSTERY_ACTION"),
        fail_task_called=True,
        effective_webhook=False,
        effective_final_screenshot=True,
        cleanup_gated_on_fail_task=False,
    ),
    CleanupHandlerCase(
        id="unsupported_task_type",
        exc_factory=lambda: UnsupportedTaskType("mystery_task"),
        fail_task_called=True,
        effective_webhook=False,
        effective_final_screenshot=True,
        cleanup_gated_on_fail_task=False,
    ),
    CleanupHandlerCase(
        id="failed_to_parse_action",
        exc_factory=lambda: FailedToParseActionInstruction("bad", "ValueError"),
        fail_task_called=True,
        effective_webhook=False,
        effective_final_screenshot=True,
        cleanup_gated_on_fail_task=False,
    ),
    CleanupHandlerCase(
        id="scraping_failed",
        exc_factory=lambda: ScrapingFailed(reason="page gone"),
        fail_task_called=True,
        effective_webhook=True,
        effective_final_screenshot=True,
        cleanup_gated_on_fail_task=False,
    ),
    CleanupHandlerCase(
        id="missing_browser_state_page",
        exc_factory=lambda: MissingBrowserStatePage(task_id="task-123"),
        fail_task_called=True,
        effective_webhook=True,
        effective_final_screenshot=True,
        cleanup_gated_on_fail_task=False,
    ),
    CleanupHandlerCase(
        id="generic_exception",
        exc_factory=lambda: RuntimeError("something unexpected"),
        fail_task_called=True,
        effective_webhook=True,
        effective_final_screenshot=True,
        cleanup_gated_on_fail_task=True,
    ),
    # SKY-15xxx: these four are Skyvern's own named browser/session exceptions (already
    # anticipated, unlike a true "unexpected" error) that used to fall through to the
    # generic_exception handler above and page P1 on every occurrence. They now share a
    # dedicated LOG.warning handler with the same fail_task/clean_up_task shape as
    # generic_exception, just without the error-level page. fail_task/clean_up_task shape
    # is identical here regardless of whether the wrapped cause is recognized (see the
    # dedicated log-level tests below for that split).
    CleanupHandlerCase(
        id="unknown_error_creating_browser_context",
        exc_factory=lambda: UnknownErrorWhileCreatingBrowserContext("chromium", RuntimeError("boom")),
        fail_task_called=True,
        effective_webhook=True,
        effective_final_screenshot=True,
        cleanup_gated_on_fail_task=True,
    ),
    CleanupHandlerCase(
        id="browser_session_already_occupied",
        exc_factory=lambda: BrowserSessionAlreadyOccupiedError("pbs-1", "tsk-2"),
        fail_task_called=True,
        effective_webhook=True,
        effective_final_screenshot=True,
        cleanup_gated_on_fail_task=True,
    ),
    CleanupHandlerCase(
        id="browser_session_closed",
        exc_factory=lambda: BrowserSessionClosed("pbs-1"),
        fail_task_called=True,
        effective_webhook=True,
        effective_final_screenshot=True,
        cleanup_gated_on_fail_task=True,
    ),
    CleanupHandlerCase(
        id="browser_session_ownership_conflict",
        exc_factory=lambda: BrowserSessionOwnershipConflict("pbs-1"),
        fail_task_called=True,
        effective_webhook=True,
        effective_final_screenshot=True,
        cleanup_gated_on_fail_task=True,
    ),
    CleanupHandlerCase(
        id="browser_target_closed",
        exc_factory=lambda: BrowserTargetClosedError("session closed underneath the run"),
        fail_task_called=True,
        effective_webhook=True,
        effective_final_screenshot=True,
        cleanup_gated_on_fail_task=True,
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("case", CLEANUP_CASES, ids=[c.id for c in CLEANUP_CASES])
async def test_cleanup_handler_pins_webhook_screenshot_and_fail_task(
    monkeypatch: pytest.MonkeyPatch, case: CleanupHandlerCase
) -> None:
    """Each cleanup handler runs clean_up_task with a pinned webhook/screenshot decision.

    Driven with fail_task reporting the task as failed, so the gated handlers also clean up.
    """
    outcome = await _drive_execute_step(monkeypatch, case.exc_factory(), fail_task_result=True)

    assert outcome.raised is None, f"{case.id} unexpectedly propagated {outcome.raised!r}"
    assert outcome.fail_task_called is case.fail_task_called
    assert outcome.cleanup_called is True
    assert outcome.effective_webhook is case.effective_webhook
    assert outcome.effective_final_screenshot is case.effective_final_screenshot
    # Every cleanup handler returns (step, detailed_output, next_step); nothing advanced here.
    assert outcome.returned is not None
    assert outcome.returned[0].step_id == "step-0"
    assert outcome.returned[2] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("case", CLEANUP_CASES, ids=[c.id for c in CLEANUP_CASES])
async def test_cleanup_gating_when_fail_task_reports_not_failed(
    monkeypatch: pytest.MonkeyPatch, case: CleanupHandlerCase
) -> None:
    """Pin whether clean_up_task is skipped when fail_task reports the task was NOT failed.

    Only three handlers (step-termination, failed-to-navigate, generic Exception) gate
    cleanup on that result; the rest clean up unconditionally.
    """
    outcome = await _drive_execute_step(monkeypatch, case.exc_factory(), fail_task_result=False)

    assert outcome.raised is None
    assert outcome.cleanup_called is not case.cleanup_gated_on_fail_task


@pytest.mark.asyncio
async def test_unknown_browser_context_error_pages_p1_for_unrecognized_cause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BrowserFactory.create_browser_context wraps *any* exception, so a cause that isn't
    one of Skyvern's own named exceptions (e.g. a real bug's AttributeError) is not an
    anticipated browser/session failure -- it must keep the error-level page, not go quiet.
    """
    log_mock = MagicMock()
    monkeypatch.setattr("skyvern.forge.agent.LOG", log_mock)

    cause = AttributeError("'NoneType' object has no attribute 'frame'")
    exc = UnknownErrorWhileCreatingBrowserContext("chromium", cause)
    exc.__cause__ = cause  # mirrors `raise ... from e` at the real call site

    outcome = await _drive_execute_step(monkeypatch, exc, fail_task_result=True)

    assert outcome.raised is None
    assert outcome.fail_task_called is True
    assert outcome.cleanup_called is True
    log_mock.exception.assert_called_once_with("Got an unexpected exception in step, marking task as failed")
    log_mock.warning.assert_not_called()


@pytest.mark.asyncio
async def test_unknown_browser_context_error_stays_quiet_for_recognized_cause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cause that IS one of Skyvern's own named exceptions (e.g. a proxy-capacity error
    surfacing as MissingBrowserStatePage) is the anticipated case this handler exists for --
    it must stay on the quiet, non-paging path.
    """
    log_mock = MagicMock()
    monkeypatch.setattr("skyvern.forge.agent.LOG", log_mock)

    cause = MissingBrowserStatePage(task_id="task-123")
    exc = UnknownErrorWhileCreatingBrowserContext("chromium", cause)
    exc.__cause__ = cause

    outcome = await _drive_execute_step(monkeypatch, exc, fail_task_result=True)

    assert outcome.raised is None
    assert outcome.fail_task_called is True
    assert outcome.cleanup_called is True
    log_mock.warning.assert_called_once()
    log_mock.exception.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "exc_factory",
    [
        lambda: BrowserSessionAlreadyOccupiedError("pbs-1", "tsk-2"),
        lambda: BrowserSessionClosed("pbs-1"),
        lambda: BrowserSessionOwnershipConflict("pbs-1"),
        lambda: BrowserTargetClosedError("session closed underneath the run"),
    ],
    ids=[
        "browser_session_already_occupied",
        "browser_session_closed",
        "browser_session_ownership_conflict",
        "browser_target_closed",
    ],
)
async def test_direct_session_exceptions_stay_on_the_quiet_path(
    monkeypatch: pytest.MonkeyPatch, exc_factory: Callable[[], BaseException]
) -> None:
    """These four are Skyvern's own named browser/session exceptions -- unlike
    UnknownErrorWhileCreatingBrowserContext's conditional handling, they're unconditionally
    quiet. CLEANUP_CASES pins fail_task/clean_up_task/webhook/screenshot, which stay
    identical to generic_exception's -- only the log level tells these apart, so pin that
    directly: dropping any one of the four from the tuple must fail this test.
    """
    log_mock = MagicMock()
    monkeypatch.setattr("skyvern.forge.agent.LOG", log_mock)

    outcome = await _drive_execute_step(monkeypatch, exc_factory(), fail_task_result=True)

    assert outcome.raised is None
    log_mock.warning.assert_called_once()
    log_mock.exception.assert_not_called()


@pytest.mark.asyncio
async def test_step_unable_to_execute_reraises_without_cleanup(monkeypatch: pytest.MonkeyPatch) -> None:
    """StepUnableToExecuteError propagates out of execute_step; no fail_task, no cleanup."""
    outcome = await _drive_execute_step(monkeypatch, StepUnableToExecuteError("step-0", "cannot run"))

    assert isinstance(outcome.raised, StepUnableToExecuteError)
    assert outcome.returned is None
    assert outcome.fail_task_called is False
    assert outcome.cleanup_called is False


@pytest.mark.asyncio
async def test_failed_to_send_webhook_is_swallowed_without_cleanup(monkeypatch: pytest.MonkeyPatch) -> None:
    """FailedToSendWebhook is swallowed (step returned); it never fails or cleans up the task."""
    outcome = await _drive_execute_step(monkeypatch, FailedToSendWebhook(task_id="task-123"))

    assert outcome.raised is None
    assert outcome.fail_task_called is False
    assert outcome.cleanup_called is False
    assert outcome.returned is not None
    assert outcome.returned[0].step_id == "step-0"
    assert outcome.returned[2] is None


# SKY-13472: execute_step recurses into the next step via `return await self.execute_step(...)`.
# The parent frame's ``detailed_output`` (holding that step's ``scraped_page``) would otherwise stay
# alive on the stack for the whole recursive subtree, pinning one ``ScrapedPage`` per level. Once
# ``record_fail_fast_shadow`` and ``handle_completed_step`` have consumed it, nothing else reads it
# before the child runs, so it is released. These drive one real ``execute_step`` frame to each of
# the two recursive call sites and, via a spy on the recursive entry, assert the parent's
# ``scraped_page`` is already cleared when the child begins — while proving the pre-release consumers
# still saw the real page and the child's return value is forwarded unchanged.
@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["retry", "execute_all_steps"], ids=["retry-site", "execute-all-steps-site"])
async def test_execute_step_releases_scraped_page_before_recursion(monkeypatch: pytest.MonkeyPatch, path: str) -> None:
    agent = ForgeAgent()
    now = datetime.now(UTC)
    organization = make_organization(now)
    task = make_task(now, organization)
    step = make_step(now, task, step_id="step-0", status=StepStatus.running, order=0, output=None)
    next_step = make_step(now, task, step_id="step-1", status=StepStatus.running, order=1, output=None)

    scraped_page = object()
    detailed_output = SimpleNamespace(scraped_page=scraped_page, cua_response=None)

    step_after = make_step(
        now,
        task,
        step_id="step-0",
        status=StepStatus.failed if path == "retry" else StepStatus.completed,
        order=0,
        output=None,
    )

    browser_state = MagicMock()
    browser_state.get_working_page = AsyncMock(return_value=MagicMock())

    monkeypatch.setattr("skyvern.forge.agent.app.DATABASE.tasks.get_task", AsyncMock(return_value=None))
    monkeypatch.setattr("skyvern.forge.agent.app.DATABASE.tasks.update_task", AsyncMock(return_value=task))
    monkeypatch.setattr("skyvern.forge.agent.app.AGENT_FUNCTION.validate_step_execution", AsyncMock(return_value=None))
    monkeypatch.setattr("skyvern.forge.agent.app.AGENT_FUNCTION.post_step_execution", AsyncMock(return_value=None))
    monkeypatch.setattr("skyvern.forge.agent.app.ARTIFACT_MANAGER.flush_step_archive", AsyncMock(return_value=None))
    monkeypatch.setattr(type(agent_settings), "execute_all_steps", lambda self: True)

    fail_fast_spy = AsyncMock(return_value=None)
    monkeypatch.setattr("skyvern.forge.agent.record_fail_fast_shadow", fail_fast_spy)

    agent.initialize_execution_state = AsyncMock(return_value=(step, browser_state, detailed_output))  # type: ignore[method-assign]
    agent.register_async_operations = AsyncMock(return_value=None)  # type: ignore[method-assign]
    agent.agent_step = AsyncMock(return_value=(step_after, detailed_output))  # type: ignore[method-assign]
    agent.update_task_errors_from_detailed_output = AsyncMock(return_value=task)  # type: ignore[method-assign]
    agent._sync_video_artifact_after_step = AsyncMock(return_value=None)  # type: ignore[method-assign]
    handle_completed_step = AsyncMock(return_value=(None, None, next_step))
    handle_failed_step = AsyncMock(return_value=next_step)
    agent.handle_completed_step = handle_completed_step  # type: ignore[method-assign]
    agent.handle_failed_step = handle_failed_step  # type: ignore[method-assign]

    snapshots: dict[str, Any] = {}
    calls = {"n": 0}
    real_execute_step = agent.execute_step

    async def spy(*args: Any, **kwargs: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            return await real_execute_step(*args, **kwargs)
        snapshots["scraped_page_at_child_entry"] = detailed_output.scraped_page
        child_step = kwargs["step"] if "step" in kwargs else args[2]
        return (child_step, None, None)

    agent.execute_step = spy  # type: ignore[method-assign]

    context = SkyvernContext(
        organization_id=organization.organization_id, task_id=task.task_id, tz_info=ZoneInfo("UTC")
    )
    skyvern_context.set(context)
    try:
        result = await agent.execute_step(
            organization=organization,
            task=task,
            step=step,
            api_key="api-key",
            download_baseline_files=[],
        )
    finally:
        skyvern_context.reset()

    # The recursion happened, and the child's return value is forwarded verbatim.
    assert calls["n"] == 2
    assert result == (next_step, None, None)

    # The parent's scraped_page is already released when the child begins.
    assert snapshots["scraped_page_at_child_entry"] is None
    assert detailed_output.scraped_page is None

    # Consumers that run before the release still saw the real page (behavior unchanged).
    assert fail_fast_spy.await_args.kwargs["scraped_page"] is scraped_page
    if path == "execute_all_steps":
        assert handle_completed_step.await_args.kwargs["scraped_page"] is scraped_page
