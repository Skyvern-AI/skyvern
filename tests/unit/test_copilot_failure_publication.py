from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.forge.sdk.artifact.models import ArtifactType
from skyvern.forge.sdk.copilot import enforcement as enforcement_module
from skyvern.forge.sdk.copilot.agent import _build_timeout_exit_result
from skyvern.forge.sdk.copilot.build_test_outcome import (
    BuildTestFailedOperation,
    RecordedBuildTestOutcome,
    record_build_test_outcome,
)
from skyvern.forge.sdk.copilot.output_utils import BUILD_TEST_PACKET_KEY, build_run_blocks_response
from skyvern.forge.sdk.copilot.run_outcome import RecordedRunOutcome
from skyvern.forge.sdk.copilot.tools.run_execution import (
    CopilotExecutionSnapshot,
    _ExecutionResult,
    _record_run_blocks_result,
    _recorded_run_block_result,
    _run_blocks_and_collect_debug,
    _RunExecution,
    finalize_build_test_result,
)
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.routes import workflow_copilot as workflow_copilot_route
from skyvern.forge.sdk.schemas.workflow_copilot import WorkflowCopilotChatRequest
from skyvern.forge.sdk.schemas.workflow_runs import WorkflowRunBlock
from skyvern.forge.sdk.workflow import context_manager as context_manager_module
from skyvern.forge.sdk.workflow import service as service_module
from skyvern.forge.sdk.workflow.context_manager import WorkflowRunContext
from skyvern.forge.sdk.workflow.models import block as block_module
from skyvern.forge.sdk.workflow.models.block import (
    BlockResult,
    BlockStatus,
    CodeBlock,
    ForLoopBlock,
    JinjaBranchCriteria,
    NavigationBlock,
    WhileLoopBlock,
)
from skyvern.forge.sdk.workflow.models.parameter import OutputParameter, ParameterType
from skyvern.forge.sdk.workflow.models.workflow import Workflow
from skyvern.forge.sdk.workflow.service import WorkflowService
from skyvern.schemas.self_heal import HealClassification
from skyvern.schemas.workflows import BlockType
from skyvern.services import script_service as script_service_module
from skyvern.webeye.browser_artifacts import BrowserArtifacts
from tests.unit.copilot_route_test_support import setup_new_copilot_mocks
from tests.unit.copilot_test_helpers import make_copilot_ctx


def _block() -> CodeBlock:
    now = datetime.now(UTC)
    return CodeBlock(
        label="run_code",
        code="raise RuntimeError('failed')",
        output_parameter=OutputParameter(
            parameter_type=ParameterType.OUTPUT,
            key="code_output",
            description="code output",
            output_parameter_id="op_code",
            workflow_id="wf_test",
            created_at=now,
            modified_at=now,
        ),
    )


def _context() -> WorkflowRunContext:
    context = WorkflowRunContext(
        workflow_title="Failure publication",
        workflow_id="wf_test",
        workflow_permanent_id="wpid_test",
        workflow_run_id="wr_test",
        aws_client=None,  # type: ignore[arg-type]
    )
    context.organization_id = "o_test"
    context.secrets["otp"] = "123456"
    return context


@pytest.mark.asyncio
@pytest.mark.parametrize("_sample", range(5))
async def test_failure_is_committed_before_exact_page_capture_finishes(
    monkeypatch: pytest.MonkeyPatch, _sample: int
) -> None:
    capture_started = asyncio.Event()
    capture_release = asyncio.Event()
    captured_page: list[object] = []
    updates: list[dict[str, object]] = []

    async def capture(*, page: object, **_: object) -> bytes:
        captured_page.append(page)
        capture_started.set()
        await capture_release.wait()
        return b"exact-page-png"

    async def legacy_capture() -> bytes:
        capture_started.set()
        await capture_release.wait()
        return b"mutable-active-page-png"

    async def update_block(**kwargs: object) -> SimpleNamespace:
        updates.append(dict(kwargs))
        return SimpleNamespace(
            workflow_run_block_id="wrb_test",
            workflow_run_id="wr_test",
            organization_id="o_test",
            status=kwargs.get("status", BlockStatus.failed),
            final_url=kwargs.get("final_url"),
        )

    create_artifact = AsyncMock()
    monkeypatch.setattr(block_module.SkyvernFrame, "take_scrolling_screenshot", capture)
    monkeypatch.setattr(block_module.app.DATABASE.observer, "update_workflow_run_block", update_block)
    monkeypatch.setattr(
        block_module.app.DATABASE.observer,
        "get_workflow_run_block",
        AsyncMock(
            return_value=SimpleNamespace(
                workflow_run_block_id="wrb_test",
                workflow_run_id="wr_test",
                organization_id="o_test",
                status=BlockStatus.failed,
                final_url=None,
            )
        ),
    )
    monkeypatch.setattr(
        block_module.app.ARTIFACT_MANAGER,
        "create_workflow_run_block_artifact",
        create_artifact,
    )
    monkeypatch.setattr(CodeBlock, "_failure_output_with_downloads", AsyncMock(return_value=None))
    page = SimpleNamespace(url="https://example.com/failure?otp=123456")
    browser_state = SimpleNamespace(
        take_fullpage_screenshot=legacy_capture,
        engine_selection=None,
        browser_artifacts=BrowserArtifacts(),
    )
    context = _context()

    publication = asyncio.create_task(
        _block()._failed_result_with_evidence(
            failure_reason="specific runner failure",
            status=BlockStatus.failed,
            workflow_run_context=context,
            workflow_run_id="wr_test",
            workflow_run_block_id="wrb_test",
            organization_id="o_test",
            browser_state=browser_state,
            page=page,
            engine="secure_runner",
            resolved_download_id=None,
            download_dir_before=None,
        )
    )
    await asyncio.wait_for(capture_started.wait(), timeout=1)

    result = await asyncio.wait_for(publication, timeout=1)
    assert result.failure_reason == "specific runner failure"
    assert result.workflow_run_block_id == "wrb_test"
    assert any(update.get("status") is BlockStatus.failed for update in updates)
    # The end URL is an execution fact: masked and persisted with the failure row, before any
    # frame is released. Only the screenshot is deferred.
    final_url_update = next(update for update in updates if update.get("final_url"))
    assert final_url_update["final_url"] == "https://example.com/failure?otp=*****"
    create_artifact.assert_not_awaited()

    capture_release.set()
    await context.drain_failure_evidence_capture()

    assert captured_page == [page]
    create_artifact.assert_awaited_once()
    assert create_artifact.await_args.kwargs["artifact_type"] is ArtifactType.SCREENSHOT_LLM
    assert create_artifact.await_args.kwargs["data"] == b"exact-page-png"


@pytest.mark.asyncio
@pytest.mark.parametrize("_sample", range(5))
async def test_continuation_cancels_pending_capture_without_attaching(
    monkeypatch: pytest.MonkeyPatch, _sample: int
) -> None:
    capture_started = asyncio.Event()
    capture_cancelled = asyncio.Event()
    updates: list[dict[str, object]] = []

    async def capture(**_: object) -> bytes:
        capture_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            capture_cancelled.set()
            raise

    async def update_block(**kwargs: object) -> SimpleNamespace:
        updates.append(dict(kwargs))
        return SimpleNamespace(
            workflow_run_block_id="wrb_test",
            workflow_run_id="wr_test",
            organization_id="o_test",
            status=kwargs.get("status", BlockStatus.failed),
        )

    create_artifact = AsyncMock()
    monkeypatch.setattr(block_module.SkyvernFrame, "take_scrolling_screenshot", capture)
    monkeypatch.setattr(block_module.app.DATABASE.observer, "update_workflow_run_block", update_block)
    monkeypatch.setattr(
        block_module.app.ARTIFACT_MANAGER,
        "create_workflow_run_block_artifact",
        create_artifact,
    )
    monkeypatch.setattr(CodeBlock, "_failure_output_with_downloads", AsyncMock(return_value=None))
    context = _context()
    page = SimpleNamespace(url="https://example.com/original")
    browser_state = SimpleNamespace(engine_selection=None, browser_artifacts=BrowserArtifacts())

    result = await _block()._failed_result_with_evidence(
        failure_reason="specific runner failure",
        status=BlockStatus.failed,
        workflow_run_context=context,
        workflow_run_id="wr_test",
        workflow_run_block_id="wrb_test",
        organization_id="o_test",
        browser_state=browser_state,
        page=page,
        engine="secure_runner",
        resolved_download_id=None,
        download_dir_before=None,
    )
    await asyncio.wait_for(capture_started.wait(), timeout=1)
    await context.cancel_failure_evidence_capture()

    assert result.failure_reason == "specific runner failure"
    assert capture_cancelled.is_set()
    create_artifact.assert_not_awaited()
    assert context.has_failure_evidence_capture is False


@pytest.mark.asyncio
@pytest.mark.parametrize("_sample", range(5))
async def test_concurrent_cleanup_and_continuation_settle_one_owned_capture(_sample: int) -> None:
    capture_started = asyncio.Event()

    async def capture(_: asyncio.Event) -> None:
        capture_started.set()
        await asyncio.Event().wait()

    context = _context()
    assert context.start_failure_evidence_capture("wrb_test", capture) is True
    await asyncio.wait_for(capture_started.wait(), timeout=1)

    drain = asyncio.create_task(context.drain_failure_evidence_capture())
    await asyncio.sleep(0)
    await context.cancel_failure_evidence_capture()
    await drain

    assert context.has_failure_evidence_capture is False


@pytest.mark.asyncio
@pytest.mark.parametrize("_sample", range(5))
async def test_cancelled_cleanup_cancels_and_joins_owned_capture(_sample: int) -> None:
    capture_started = asyncio.Event()
    capture_finished = asyncio.Event()

    async def capture(_: asyncio.Event) -> None:
        capture_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            capture_finished.set()

    context = _context()
    assert context.start_failure_evidence_capture("wrb_test", capture) is True
    await asyncio.wait_for(capture_started.wait(), timeout=1)

    cleanup = asyncio.create_task(context.drain_failure_evidence_capture())
    await asyncio.sleep(0)
    cleanup.cancel()

    with pytest.raises(asyncio.CancelledError):
        await cleanup
    assert capture_finished.is_set()
    assert context.has_failure_evidence_capture is False


@pytest.mark.asyncio
@pytest.mark.parametrize("_sample", range(5))
async def test_drain_timeout_still_restores_a_caller_cancellation(
    monkeypatch: pytest.MonkeyPatch, _sample: int
) -> None:
    capture_started = asyncio.Event()
    join_reached = asyncio.Event()
    release_join = asyncio.Event()

    async def capture(_: asyncio.Event) -> None:
        capture_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            join_reached.set()
            # Hold the drain inside its suppressed join until the test has issued the caller
            # cancel. That cancel is aimed at the draining task, not this one, so a plain wait
            # holds: this event only completes when the test releases it.
            await release_join.wait()
            raise

    monkeypatch.setattr(context_manager_module, "BROWSER_CLOSE_TIMEOUT", 0)
    context = _context()
    assert context.start_failure_evidence_capture("wrb_test", capture) is True
    await asyncio.wait_for(capture_started.wait(), timeout=1)

    cleanup = asyncio.create_task(context.drain_failure_evidence_capture())
    await asyncio.wait_for(join_reached.wait(), timeout=1)
    cleanup.cancel()
    release_join.set()

    with pytest.raises(asyncio.CancelledError):
        await cleanup
    assert context.has_failure_evidence_capture is False


@pytest.mark.asyncio
@pytest.mark.parametrize("_sample", range(5))
async def test_simultaneous_capture_and_cleanup_cancellation_preserves_cleanup_cancellation(_sample: int) -> None:
    capture_started = asyncio.Event()
    capture_finished = asyncio.Event()

    async def capture(_: asyncio.Event) -> None:
        capture_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            capture_finished.set()

    context = _context()
    assert context.start_failure_evidence_capture("wrb_test", capture) is True
    await asyncio.wait_for(capture_started.wait(), timeout=1)

    cleanup = asyncio.create_task(context.drain_failure_evidence_capture())
    await asyncio.sleep(0)
    owned_capture = context._failure_evidence_capture
    assert owned_capture is not None
    owned_capture.task.cancel()
    cleanup.cancel()

    with pytest.raises(asyncio.CancelledError):
        await cleanup
    assert capture_finished.is_set()
    assert owned_capture.task.done()
    assert context.has_failure_evidence_capture is False


@pytest.mark.asyncio
@pytest.mark.parametrize("_sample", range(5))
async def test_cancel_capture_joins_owned_task_before_preserving_caller_cancellation(_sample: int) -> None:
    capture_started = asyncio.Event()
    cancellation_cleanup_started = asyncio.Event()
    cancellation_cleanup_release = asyncio.Event()
    cancellation_cleanup_finished = asyncio.Event()

    async def capture(_: asyncio.Event) -> None:
        capture_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancellation_cleanup_started.set()
            await cancellation_cleanup_release.wait()
            cancellation_cleanup_finished.set()

    context = _context()
    assert context.start_failure_evidence_capture("wrb_test", capture) is True
    await asyncio.wait_for(capture_started.wait(), timeout=1)

    continuation = asyncio.create_task(context.cancel_failure_evidence_capture())
    await asyncio.wait_for(cancellation_cleanup_started.wait(), timeout=1)
    continuation.cancel()
    await asyncio.sleep(0)

    assert context.has_failure_evidence_capture is True
    assert cancellation_cleanup_finished.is_set() is False

    cancellation_cleanup_release.set()
    with pytest.raises(asyncio.CancelledError):
        await continuation
    assert cancellation_cleanup_finished.is_set()
    assert context.has_failure_evidence_capture is False


@pytest.mark.asyncio
async def test_successful_self_heal_never_starts_failure_capture(monkeypatch: pytest.MonkeyPatch) -> None:
    block = _block()
    context = _context()
    recorder = SimpleNamespace(recording_page=SimpleNamespace(), finalize=AsyncMock())
    healed = BlockResult(
        success=True,
        output_parameter=block.output_parameter,
        status=BlockStatus.completed,
        workflow_run_block_id="wrb_test",
    )
    create_artifact = AsyncMock()
    update_block = AsyncMock(return_value=SimpleNamespace(workflow_run_block_id="wrb_test"))
    # Only the patches needed to reach the heal arm; the evidence path itself runs for real so the
    # assertions below observe what was persisted rather than what a stub recorded.
    monkeypatch.setattr(block_module.SkyvernFrame, "take_scrolling_screenshot", AsyncMock(return_value=b"frame"))
    monkeypatch.setattr(block_module.app.DATABASE.observer, "update_workflow_run_block", update_block)
    monkeypatch.setattr(block_module.app.ARTIFACT_MANAGER, "create_workflow_run_block_artifact", create_artifact)
    monkeypatch.setattr(block, "_self_heal_enabled", AsyncMock(return_value=True))
    monkeypatch.setattr(block_module.app.AGENT_FUNCTION, "resolve_self_heal_api_key", AsyncMock(return_value=None))
    monkeypatch.setattr(block, "_write_heal_episode_safe", AsyncMock())
    monkeypatch.setattr(block, "_attempt_self_heal", AsyncMock(return_value=healed))
    monkeypatch.setattr(block, "_register_downloaded_files", AsyncMock(return_value=([], set())))
    monkeypatch.setattr(block, "_bind_and_grade_downloads", AsyncMock(return_value=(None, None)))
    monkeypatch.setattr(CodeBlock, "record_output_parameter_value", AsyncMock())

    result = await block._resolve_failure_with_heal(
        exception=RuntimeError("initial failure"),
        failing_line=1,
        build_failure_result=AsyncMock(),
        classification=HealClassification(healable=True, skip_reason=None),
        recorder=recorder,
        workflow_run_context=context,
        workflow_run_id="wr_test",
        workflow_run_block_id="wrb_test",
        organization_id="o_test",
        browser_session_id=None,
        browser_state=SimpleNamespace(
            browser_context=None,
            browser_artifacts=BrowserArtifacts(),
            engine_selection=None,
        ),
        page=SimpleNamespace(url="https://example.com/original"),
    )

    assert result.success is True
    # A healed block owns no capture and carries no at-failure evidence: the staged frame is
    # discarded and no failure URL is written to the row.
    assert context.has_failure_evidence_capture is False
    create_artifact.assert_not_awaited()
    assert not any(call.kwargs.get("final_url") for call in update_block.await_args_list)


@pytest.mark.asyncio
@pytest.mark.parametrize("_sample", range(5))
async def test_outer_deadline_during_staging_still_publishes_the_healable_failure(
    monkeypatch: pytest.MonkeyPatch, _sample: int
) -> None:
    """The frame is read before the heal runs, so an outer deadline can expire inside that
    optional read. The established failure must still be published, and the cancellation must
    still reach the caller."""
    block = _block()
    context = _context()
    recorder = SimpleNamespace(recording_page=SimpleNamespace(), finalize=AsyncMock())
    staging_entered = asyncio.Event()
    published: list[str] = []
    screenshot_attempts = 0

    async def cancelled_screenshot(**_: object) -> bytes:
        nonlocal screenshot_attempts
        screenshot_attempts += 1
        staging_entered.set()
        await asyncio.Event().wait()
        return b""

    async def build_failure_result() -> BlockResult:
        published.append("failure_published")
        return BlockResult(
            success=False,
            output_parameter=block.output_parameter,
            status=BlockStatus.failed,
            failure_reason="specific runner failure",
            workflow_run_block_id="wrb_test",
        )

    heal = AsyncMock()
    monkeypatch.setattr(block_module.SkyvernFrame, "take_scrolling_screenshot", cancelled_screenshot)
    monkeypatch.setattr(block, "_self_heal_enabled", AsyncMock(return_value=True))
    monkeypatch.setattr(block, "_attempt_self_heal", heal)
    monkeypatch.setattr(block_module.app.AGENT_FUNCTION, "resolve_self_heal_api_key", AsyncMock(return_value=None))
    monkeypatch.setattr(block, "_write_heal_episode_safe", AsyncMock())
    monkeypatch.setattr(block, "_register_downloaded_files", AsyncMock(return_value=([], set())))
    monkeypatch.setattr(block, "_bind_and_grade_downloads", AsyncMock(return_value=(None, None)))
    monkeypatch.setattr(block_module.app.DATABASE.observer, "update_workflow_run_block", AsyncMock())
    monkeypatch.setattr(CodeBlock, "record_output_parameter_value", AsyncMock())

    task = asyncio.create_task(
        block._resolve_failure_with_heal(
            exception=RuntimeError("initial failure"),
            failing_line=1,
            build_failure_result=build_failure_result,
            classification=HealClassification(healable=True, skip_reason=None),
            recorder=recorder,
            workflow_run_context=context,
            workflow_run_id="wr_test",
            workflow_run_block_id="wrb_test",
            organization_id="o_test",
            browser_session_id=None,
            browser_state=SimpleNamespace(
                browser_context=None, browser_artifacts=BrowserArtifacts(), engine_selection=None
            ),
            page=SimpleNamespace(url="https://example.com/original"),
        )
    )
    await asyncio.wait_for(staging_entered.wait(), timeout=1)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    # The failure fact survived the deadline; no heal was attempted after the cancel.
    assert published == ["failure_published"]
    heal.assert_not_awaited()
    # The published failure carries a staged-but-empty frame, so the owned capture must not take a
    # second screenshot that cleanup would then have to drain past the cancellation.
    await context.cancel_failure_evidence_capture()
    assert screenshot_attempts == 1
    assert context.has_failure_evidence_capture is False


@pytest.mark.asyncio
@pytest.mark.parametrize("_sample", range(5))
async def test_healable_failure_is_not_held_behind_a_stalled_frame(
    monkeypatch: pytest.MonkeyPatch, _sample: int
) -> None:
    """On the healable arm the frame is read before recovery, so a stalled read would hold both
    the heal and the failure fact. The read is bounded by the screenshot budget; on expiry the
    heal proceeds without a frame."""
    block = _block()
    context = _context()
    recorder = SimpleNamespace(recording_page=SimpleNamespace(), finalize=AsyncMock())
    heal_reached = asyncio.Event()

    async def stalled_screenshot(**_: object) -> bytes:
        await asyncio.Event().wait()
        return b""

    async def heal(*_: object, **__: object) -> BlockResult:
        heal_reached.set()
        return BlockResult(
            success=True,
            output_parameter=block.output_parameter,
            status=BlockStatus.completed,
            workflow_run_block_id="wrb_test",
        )

    monkeypatch.setattr(block_module.settings, "BROWSER_SCREENSHOT_TIMEOUT_MS", 10)
    monkeypatch.setattr(block_module.SkyvernFrame, "take_scrolling_screenshot", stalled_screenshot)
    monkeypatch.setattr(block, "_self_heal_enabled", AsyncMock(return_value=True))
    monkeypatch.setattr(block_module.app.AGENT_FUNCTION, "resolve_self_heal_api_key", AsyncMock(return_value=None))
    monkeypatch.setattr(block, "_write_heal_episode_safe", AsyncMock())
    monkeypatch.setattr(block, "_attempt_self_heal", heal)
    monkeypatch.setattr(block, "_register_downloaded_files", AsyncMock(return_value=([], set())))
    monkeypatch.setattr(block, "_bind_and_grade_downloads", AsyncMock(return_value=(None, None)))
    monkeypatch.setattr(CodeBlock, "record_output_parameter_value", AsyncMock())

    result = await asyncio.wait_for(
        block._resolve_failure_with_heal(
            exception=RuntimeError("initial failure"),
            failing_line=1,
            build_failure_result=AsyncMock(),
            classification=HealClassification(healable=True, skip_reason=None),
            recorder=recorder,
            workflow_run_context=context,
            workflow_run_id="wr_test",
            workflow_run_block_id="wrb_test",
            organization_id="o_test",
            browser_session_id=None,
            browser_state=SimpleNamespace(
                browser_context=None, browser_artifacts=BrowserArtifacts(), engine_selection=None
            ),
            page=SimpleNamespace(url="https://example.com/original"),
        ),
        timeout=2,
    )

    assert heal_reached.is_set()
    assert result.success is True


@pytest.mark.asyncio
async def test_failed_self_heal_is_not_persisted_twice_before_capture(monkeypatch: pytest.MonkeyPatch) -> None:
    block = _block()
    context = _context()
    recorder = SimpleNamespace(recording_page=SimpleNamespace(), finalize=AsyncMock())
    failed = BlockResult(
        success=False,
        failure_reason="specific healed-attempt failure",
        output_parameter=block.output_parameter,
        status=BlockStatus.failed,
        workflow_run_block_id="wrb_test",
    )
    update_block = AsyncMock(return_value=SimpleNamespace(workflow_run_block_id="wrb_test"))
    create_artifact = AsyncMock()
    monkeypatch.setattr(block_module.SkyvernFrame, "take_scrolling_screenshot", AsyncMock(return_value=b"frame"))
    monkeypatch.setattr(block_module.app.DATABASE.observer, "update_workflow_run_block", update_block)
    monkeypatch.setattr(
        block_module.app.DATABASE.observer,
        "get_workflow_run_block",
        AsyncMock(
            return_value=SimpleNamespace(
                workflow_run_block_id="wrb_test", workflow_run_id="wr_test", status=BlockStatus.failed
            )
        ),
    )
    monkeypatch.setattr(block_module.app.ARTIFACT_MANAGER, "create_workflow_run_block_artifact", create_artifact)
    monkeypatch.setattr(block, "_self_heal_enabled", AsyncMock(return_value=True))
    monkeypatch.setattr(block_module.app.AGENT_FUNCTION, "resolve_self_heal_api_key", AsyncMock(return_value=None))
    monkeypatch.setattr(block, "_write_heal_episode_safe", AsyncMock())
    monkeypatch.setattr(block, "_attempt_self_heal", AsyncMock(return_value=failed))
    monkeypatch.setattr(block, "_failure_output_with_downloads", AsyncMock(return_value=None))
    monkeypatch.setattr(CodeBlock, "record_output_parameter_value", AsyncMock())

    result = await block._resolve_failure_with_heal(
        exception=RuntimeError("initial failure"),
        failing_line=1,
        build_failure_result=AsyncMock(),
        classification=HealClassification(healable=True, skip_reason=None),
        recorder=recorder,
        workflow_run_context=context,
        workflow_run_id="wr_test",
        workflow_run_block_id="wrb_test",
        organization_id="o_test",
        browser_session_id=None,
        browser_state=SimpleNamespace(
            browser_context=None,
            browser_artifacts=BrowserArtifacts(),
            engine_selection=None,
        ),
        page=SimpleNamespace(url="https://example.com/original"),
    )
    await context.drain_failure_evidence_capture()

    # The heal already persisted this failure; the publisher returns it untouched rather than
    # writing a second row for the same attempt.
    assert result == failed
    assert [call.kwargs.get("status") for call in update_block.await_args_list].count(BlockStatus.failed) <= 1
    # The frame staged before the heal still reaches the failed row once the capture is authorized.
    create_artifact.assert_awaited_once()
    assert create_artifact.await_args.kwargs["data"] == b"frame"


@pytest.mark.asyncio
async def test_parameter_redacted_url_from_the_result_builder_lands_last(monkeypatch: pytest.MonkeyPatch) -> None:
    """Some failure arms restore a parameter-redacted URL inside the result builder. The
    secret-masked fallback the publisher writes must land before it, never over it, or a
    code-block parameter value in the query string persists raw."""
    updates: list[dict[str, object]] = []

    async def update_block(**kwargs: object) -> SimpleNamespace:
        updates.append(dict(kwargs))
        return SimpleNamespace(workflow_run_block_id="wrb_test", workflow_run_id="wr_test", organization_id="o_test")

    block = _block()
    context = _context()

    async def build_failure_result() -> BlockResult:
        await block._persist_captured_failure_final_url(
            final_url="https://example.com/checkout?account=*****",
            workflow_run_block_id="wrb_test",
            organization_id="o_test",
        )
        return BlockResult(
            success=False,
            output_parameter=block.output_parameter,
            status=BlockStatus.failed,
            failure_reason="runner failure",
            workflow_run_block_id="wrb_test",
        )

    monkeypatch.setattr(block_module.SkyvernFrame, "take_scrolling_screenshot", AsyncMock(return_value=None))
    monkeypatch.setattr(block_module.app.DATABASE.observer, "update_workflow_run_block", update_block)

    await block._publish_failure_result_with_evidence(
        build_failure_result=build_failure_result,
        workflow_run_context=context,
        workflow_run_id="wr_test",
        workflow_run_block_id="wrb_test",
        organization_id="o_test",
        browser_state=SimpleNamespace(engine_selection=None),
        page=SimpleNamespace(url="https://example.com/checkout?account=ACCT-4411"),
    )
    await context.drain_failure_evidence_capture()

    url_writes = [update["final_url"] for update in updates if update.get("final_url")]
    assert url_writes[-1] == "https://example.com/checkout?account=*****"
    assert "ACCT-4411" not in url_writes[-1]


@pytest.mark.asyncio
@pytest.mark.parametrize("_sample", range(5))
async def test_end_url_is_persisted_while_the_frame_is_still_stalled(
    monkeypatch: pytest.MonkeyPatch, _sample: int
) -> None:
    """The end URL is an execution fact and lands with the failure row; a frame that never
    returns must not withhold it, or copilot reports no persisted final URL for a failed row."""
    capture_started = asyncio.Event()
    updates: list[dict[str, object]] = []

    async def stalled_capture(**_: object) -> bytes:
        capture_started.set()
        await asyncio.Event().wait()
        return b""

    async def update_block(**kwargs: object) -> SimpleNamespace:
        updates.append(dict(kwargs))
        return SimpleNamespace(
            workflow_run_block_id="wrb_test",
            workflow_run_id="wr_test",
            organization_id="o_test",
            status=kwargs.get("status", BlockStatus.failed),
            final_url=kwargs.get("final_url"),
        )

    monkeypatch.setattr(block_module.SkyvernFrame, "take_scrolling_screenshot", stalled_capture)
    monkeypatch.setattr(block_module.app.DATABASE.observer, "update_workflow_run_block", update_block)
    monkeypatch.setattr(block_module.app.ARTIFACT_MANAGER, "create_workflow_run_block_artifact", AsyncMock())
    monkeypatch.setattr(CodeBlock, "_failure_output_with_downloads", AsyncMock(return_value=None))
    context = _context()

    result = await _block()._failed_result_with_evidence(
        failure_reason="specific runner failure",
        status=BlockStatus.failed,
        workflow_run_context=context,
        workflow_run_id="wr_test",
        workflow_run_block_id="wrb_test",
        organization_id="o_test",
        browser_state=SimpleNamespace(engine_selection=None, browser_artifacts=BrowserArtifacts()),
        page=SimpleNamespace(url="https://example.com/failure?otp=123456"),
        engine="secure_runner",
        resolved_download_id=None,
        download_dir_before=None,
    )
    await asyncio.wait_for(capture_started.wait(), timeout=1)

    # The result has returned and the frame is still hanging; the masked URL is already on the row.
    assert result.failure_reason == "specific runner failure"
    assert context.has_failure_evidence_capture is True
    assert [update.get("final_url") for update in updates if update.get("final_url")] == [
        "https://example.com/failure?otp=*****"
    ]

    await context.cancel_failure_evidence_capture()


@pytest.mark.asyncio
async def test_task_v2_block_settles_the_child_runs_capture_before_handing_the_page_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A child run shares the parent's browser, and the parent's next block settles only its own
    context; a failed child code block's capture must be settled where the page comes back."""
    settled: list[str] = []
    child_context = _context()

    async def cancel_child_capture() -> None:
        settled.append("wr_child")

    child_context.cancel_failure_evidence_capture = cancel_child_capture  # type: ignore[method-assign]
    monkeypatch.setattr(
        block_module.app.WORKFLOW_CONTEXT_MANAGER, "has_workflow_run_context", lambda rid: rid == "wr_child"
    )
    monkeypatch.setattr(block_module.app.WORKFLOW_CONTEXT_MANAGER, "get_workflow_run_context", lambda _: child_context)
    block = block_module.TaskV2Block.__new__(block_module.TaskV2Block)

    await block._settle_child_capture(SimpleNamespace(workflow_run_id="wr_child"))  # type: ignore[arg-type]
    await block._settle_child_capture(SimpleNamespace(workflow_run_id="wr_unknown"))  # type: ignore[arg-type]
    await block._settle_child_capture(None)

    assert settled == ["wr_child"]


@pytest.mark.asyncio
@pytest.mark.parametrize("_sample", range(5))
async def test_capture_failure_leaves_committed_failure_intact(monkeypatch: pytest.MonkeyPatch, _sample: int) -> None:
    capture_attempted = asyncio.Event()
    updates: list[dict[str, object]] = []

    async def capture(**_: object) -> bytes:
        capture_attempted.set()
        raise RuntimeError("capture unavailable")

    async def update_block(**kwargs: object) -> SimpleNamespace:
        updates.append(dict(kwargs))
        return SimpleNamespace(
            workflow_run_block_id="wrb_test",
            workflow_run_id="wr_test",
            organization_id="o_test",
            status=kwargs.get("status", BlockStatus.failed),
            final_url=kwargs.get("final_url"),
        )

    create_artifact = AsyncMock()
    monkeypatch.setattr(block_module.SkyvernFrame, "take_scrolling_screenshot", capture)
    monkeypatch.setattr(block_module.app.DATABASE.observer, "update_workflow_run_block", update_block)
    monkeypatch.setattr(
        block_module.app.DATABASE.observer,
        "get_workflow_run_block",
        AsyncMock(
            return_value=SimpleNamespace(
                workflow_run_block_id="wrb_test",
                workflow_run_id="wr_test",
                organization_id="o_test",
                status=BlockStatus.failed,
                final_url=None,
            )
        ),
    )
    monkeypatch.setattr(
        block_module.app.ARTIFACT_MANAGER,
        "create_workflow_run_block_artifact",
        create_artifact,
    )
    monkeypatch.setattr(CodeBlock, "_failure_output_with_downloads", AsyncMock(return_value=None))
    context = _context()

    result = await _block()._failed_result_with_evidence(
        failure_reason="specific runner failure",
        status=BlockStatus.failed,
        workflow_run_context=context,
        workflow_run_id="wr_test",
        workflow_run_block_id="wrb_test",
        organization_id="o_test",
        browser_state=SimpleNamespace(engine_selection=None, browser_artifacts=BrowserArtifacts()),
        page=SimpleNamespace(url="https://example.com/failure"),
        engine="secure_runner",
        resolved_download_id=None,
        download_dir_before=None,
    )
    await asyncio.wait_for(capture_attempted.wait(), timeout=1)
    await context.drain_failure_evidence_capture()

    assert result.failure_reason == "specific runner failure"
    assert any(update.get("status") is BlockStatus.failed for update in updates)
    assert any(update.get("final_url") == "https://example.com/failure" for update in updates)
    create_artifact.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_page_publishes_failure_without_starting_capture(monkeypatch: pytest.MonkeyPatch) -> None:
    block = _block()
    context = _context()
    failure_reason = "specific runner failure"
    build_result = AsyncMock(
        return_value=BlockResult(
            success=False,
            failure_reason=failure_reason,
            output_parameter=block.output_parameter,
            status=BlockStatus.failed,
            workflow_run_block_id="wrb_test",
        )
    )
    monkeypatch.setattr(CodeBlock, "build_block_result", build_result)
    monkeypatch.setattr(block, "_failure_output_with_downloads", AsyncMock(return_value=None))

    result = await block._failed_result_with_evidence(
        failure_reason=failure_reason,
        status=BlockStatus.failed,
        workflow_run_context=context,
        workflow_run_id="wr_test",
        workflow_run_block_id="wrb_test",
        organization_id="o_test",
        browser_state=None,
        page=None,
        engine="inline",
        resolved_download_id=None,
        download_dir_before=None,
    )

    assert result.failure_reason == failure_reason
    assert context.has_failure_evidence_capture is False


@pytest.mark.asyncio
async def test_late_capture_preserves_authoritative_failure_url(monkeypatch: pytest.MonkeyPatch) -> None:
    row = SimpleNamespace(
        workflow_run_block_id="wrb_test",
        workflow_run_id="wr_test",
        status=BlockStatus.failed,
        final_url="https://runner.example/failure",
    )
    update = AsyncMock()
    artifact = AsyncMock()
    monkeypatch.setattr(block_module.app.DATABASE.observer, "get_workflow_run_block", AsyncMock(return_value=row))
    monkeypatch.setattr(block_module.app.DATABASE.observer, "update_workflow_run_block", update)
    monkeypatch.setattr(block_module.app.ARTIFACT_MANAGER, "create_workflow_run_block_artifact", artifact)

    await _block()._attach_failure_screenshot(
        workflow_run_id="wr_test",
        workflow_run_block_id="wrb_test",
        organization_id="o_test",
        screenshot=b"original-page",
    )

    # The late path attaches a frame only; it has no URL writer to race the committed one.
    update.assert_not_awaited()
    artifact.assert_awaited_once()
    assert artifact.await_args.kwargs["workflow_run_block"] is row


@pytest.mark.asyncio
async def test_late_capture_attaches_to_a_terminated_block(monkeypatch: pytest.MonkeyPatch) -> None:
    updates: list[dict[str, object]] = []
    row = SimpleNamespace(
        workflow_run_block_id="wrb_test",
        workflow_run_id="wr_test",
        organization_id="o_test",
        status=BlockStatus.terminated,
        final_url=None,
    )

    async def update_block(**kwargs: object) -> SimpleNamespace:
        updates.append(dict(kwargs))
        return SimpleNamespace(
            workflow_run_block_id="wrb_test",
            workflow_run_id="wr_test",
            organization_id="o_test",
            status=BlockStatus.terminated,
            final_url=kwargs.get("final_url"),
        )

    create_artifact = AsyncMock()
    monkeypatch.setattr(block_module.app.DATABASE.observer, "get_workflow_run_block", AsyncMock(return_value=row))
    monkeypatch.setattr(block_module.app.DATABASE.observer, "update_workflow_run_block", update_block)
    monkeypatch.setattr(block_module.app.ARTIFACT_MANAGER, "create_workflow_run_block_artifact", create_artifact)

    await _block()._attach_failure_screenshot(
        workflow_run_id="wr_test",
        workflow_run_block_id="wrb_test",
        organization_id="o_test",
        screenshot=b"terminated-page-png",
    )

    assert updates == []
    create_artifact.assert_awaited_once()
    assert create_artifact.await_args.kwargs["data"] == b"terminated-page-png"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("row_run_id", "row_status"),
    [("wr_test", BlockStatus.completed), ("wr_newer", BlockStatus.failed)],
    ids=["healed-row", "superseded-run"],
)
async def test_late_capture_rejects_non_authoritative_attempt(
    monkeypatch: pytest.MonkeyPatch, row_run_id: str, row_status: BlockStatus
) -> None:
    row = SimpleNamespace(
        workflow_run_block_id="wrb_test",
        workflow_run_id=row_run_id,
        status=row_status,
        final_url=None,
    )
    update = AsyncMock()
    artifact = AsyncMock()
    monkeypatch.setattr(block_module.app.DATABASE.observer, "get_workflow_run_block", AsyncMock(return_value=row))
    monkeypatch.setattr(block_module.app.DATABASE.observer, "update_workflow_run_block", update)
    monkeypatch.setattr(block_module.app.ARTIFACT_MANAGER, "create_workflow_run_block_artifact", artifact)

    await _block()._attach_failure_screenshot(
        workflow_run_id="wr_test",
        workflow_run_block_id="wrb_test",
        organization_id="o_test",
        screenshot=b"stale-page",
    )

    update.assert_not_awaited()
    artifact.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("supplied_cleanup_result", [False, True])
async def test_terminal_cleanup_settles_capture_before_first_hook(
    monkeypatch: pytest.MonkeyPatch, supplied_cleanup_result: bool
) -> None:
    events: list[str] = []
    context = _context()

    async def drain() -> None:
        events.append("capture_drained")

    async def terminal_hook(**_: object) -> None:
        events.append("terminal_hook")
        raise asyncio.CancelledError

    context.drain_failure_evidence_capture = drain  # type: ignore[method-assign]
    monkeypatch.setattr(service_module.analytics, "capture", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(service_module.app.WORKFLOW_CONTEXT_MANAGER, "has_workflow_run_context", lambda _: True)
    monkeypatch.setattr(service_module.app.WORKFLOW_CONTEXT_MANAGER, "get_workflow_run_context", lambda _: context)
    monkeypatch.setattr(service_module.app.AGENT_FUNCTION, "on_workflow_run_terminal", terminal_hook)
    cleanup_result = (
        SimpleNamespace(
            browser_state=None,
            tasks=[],
            all_workflow_task_ids=[],
            child_workflow_run_ids=[],
            close_browser_on_completion=True,
        )
        if supplied_cleanup_result
        else None
    )

    with pytest.raises(asyncio.CancelledError):
        await WorkflowService().clean_up_workflow(
            workflow=SimpleNamespace(),
            workflow_run=SimpleNamespace(
                workflow_run_id="wr_test", organization_id="o_test", status=BlockStatus.failed
            ),
            browser_cleanup_result=cleanup_result,
        )

    assert events == ["capture_drained", "terminal_hook"]


@pytest.mark.asyncio
async def test_terminal_cleanup_finishes_teardown_before_propagating_capture_drain_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    context = _context()

    async def drain() -> None:
        events.append("capture_drained")
        raise asyncio.CancelledError

    async def terminal_hook(**_: object) -> None:
        events.append("terminal_hook")

    context.drain_failure_evidence_capture = drain  # type: ignore[method-assign]
    manager = block_module.app.WORKFLOW_CONTEXT_MANAGER
    monkeypatch.setattr(service_module.analytics, "capture", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(manager, "has_workflow_run_context", lambda _: True)
    monkeypatch.setattr(manager, "get_workflow_run_context", lambda _: context)
    monkeypatch.setattr(manager, "remove_workflow_run_context", lambda run_id: events.append(f"evicted:{run_id}"))
    monkeypatch.setattr(service_module.app.AGENT_FUNCTION, "on_workflow_run_terminal", terminal_hook)
    monkeypatch.setattr(service_module.app.ARTIFACT_MANAGER, "wait_for_upload_aiotasks", AsyncMock())
    monkeypatch.setattr(service_module.app.STORAGE, "save_downloaded_files", AsyncMock())
    monkeypatch.setattr(service_module.uploaded_file_service, "delete_files_attached_to_run", AsyncMock())
    cleanup_result = SimpleNamespace(
        browser_state=None,
        tasks=[],
        all_workflow_task_ids=[],
        child_workflow_run_ids=["wr_child"],
        close_browser_on_completion=True,
        browser_session_write_back_attempted=True,
    )

    with pytest.raises(asyncio.CancelledError):
        await WorkflowService().clean_up_workflow(
            workflow=SimpleNamespace(),
            workflow_run=SimpleNamespace(
                workflow_run_id="wr_test", organization_id="o_test", status=BlockStatus.failed
            ),
            browser_cleanup_result=cleanup_result,
            need_call_webhook=False,
            schedule_credential_fallback_retry=False,
        )

    assert events == ["capture_drained", "terminal_hook", "evicted:wr_test", "evicted:wr_child"]


@pytest.mark.asyncio
@pytest.mark.parametrize("_sample", range(5))
async def test_terminal_cleanup_without_prebuilt_result_finishes_teardown_when_a_child_drain_is_cancelled(
    monkeypatch: pytest.MonkeyPatch, _sample: int
) -> None:
    """Without a prebuilt cleanup result, child runs are discovered during cleanup. A real caller
    cancel that lands while the child's capture drains must still see browser teardown and both
    evictions complete, and the later drain inside browser cleanup must not re-raise on the
    already-consumed cancel."""
    events: list[str] = []
    parent_context = _context()
    child_context = _context()
    child_capture_started = asyncio.Event()
    child_drain_entered = asyncio.Event()

    async def child_capture(_: asyncio.Event) -> None:
        child_capture_started.set()
        await asyncio.Event().wait()

    assert child_context.start_failure_evidence_capture("wrb_child", child_capture) is True
    await asyncio.wait_for(child_capture_started.wait(), timeout=1)

    real_child_drain = child_context.drain_failure_evidence_capture

    async def child_drain() -> None:
        child_drain_entered.set()
        await real_child_drain()

    async def terminal_hook(**_: object) -> None:
        events.append("terminal_hook")

    async def cleanup(*_: object, **__: object) -> SimpleNamespace:
        events.append("browser_cleaned")
        return SimpleNamespace(browser_state=None, recording_finalized=True)

    child_context.drain_failure_evidence_capture = child_drain  # type: ignore[method-assign]
    contexts = {"wr_test": parent_context, "wr_child": child_context}
    manager = block_module.app.WORKFLOW_CONTEXT_MANAGER
    monkeypatch.setattr(service_module.analytics, "capture", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(manager, "has_workflow_run_context", contexts.__contains__)
    monkeypatch.setattr(manager, "get_workflow_run_context", contexts.__getitem__)
    monkeypatch.setattr(manager, "remove_workflow_run_context", lambda run_id: events.append(f"evicted:{run_id}"))
    monkeypatch.setattr(
        block_module.app.DATABASE.workflow_runs,
        "get_workflow_runs_by_parent_workflow_run_id",
        AsyncMock(return_value=[SimpleNamespace(workflow_run_id="wr_child")]),
    )
    monkeypatch.setattr(block_module.app.BROWSER_MANAGER, "cleanup_for_workflow_run", cleanup)
    monkeypatch.setattr(service_module.app.AGENT_FUNCTION, "on_workflow_run_terminal", terminal_hook)
    monkeypatch.setattr(service_module.app.ARTIFACT_MANAGER, "wait_for_upload_aiotasks", AsyncMock())
    monkeypatch.setattr(service_module.app.STORAGE, "save_downloaded_files", AsyncMock())
    monkeypatch.setattr(service_module.uploaded_file_service, "delete_files_attached_to_run", AsyncMock())
    service = WorkflowService()
    monkeypatch.setattr(service, "get_tasks_by_workflow_run_id", AsyncMock(return_value=[]))

    cleanup_task = asyncio.create_task(
        service.clean_up_workflow(
            workflow=SimpleNamespace(),
            workflow_run=SimpleNamespace(
                workflow_run_id="wr_test", organization_id="o_test", status=BlockStatus.failed, browser_address=None
            ),
            browser_cleanup_result=None,
            need_call_webhook=False,
            schedule_credential_fallback_retry=False,
        )
    )
    await asyncio.wait_for(child_drain_entered.wait(), timeout=1)
    cleanup_task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await cleanup_task

    assert child_context.has_failure_evidence_capture is False
    assert "browser_cleaned" in events
    assert "evicted:wr_test" in events and "evicted:wr_child" in events


@pytest.mark.asyncio
async def test_for_loop_settles_prior_capture_before_iteration_tab_reset(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []
    context = _context()
    inner = NavigationBlock(
        label="inner",
        output_parameter=_block().output_parameter.model_copy(update={"key": "inner_output"}),
        url="https://example.com",
        navigation_goal="continue",
        next_loop_on_failure=True,
    )
    loop = ForLoopBlock(
        label="loop",
        output_parameter=_block().output_parameter.model_copy(update={"key": "loop_output"}),
        loop_blocks=[inner],
    )

    async def execute_inner(**_: object) -> BlockResult:
        return BlockResult(
            success=False,
            output_parameter=inner.output_parameter,
            status=BlockStatus.failed,
            failure_reason="specific runner failure",
        )

    async def cancel_capture() -> None:
        events.append("capture_settled")

    async def reset_tabs(*_: object) -> None:
        events.append("tabs_reset")
        assert events[-2:] == ["capture_settled", "tabs_reset"]

    object.__setattr__(inner, "execute_safe", execute_inner)
    context.cancel_failure_evidence_capture = cancel_capture  # type: ignore[method-assign]
    monkeypatch.setattr(loop, "_snapshot_loop_baseline_pages", AsyncMock(return_value=set()))
    monkeypatch.setattr(loop, "_reset_browser_tabs_for_iteration", reset_tabs)
    monkeypatch.setattr(loop, "_persist_partial_loop_output", AsyncMock())
    monkeypatch.setattr(ForLoopBlock, "get_loop_block_context_parameters", lambda *_: [])

    await loop.execute_loop_helper(
        workflow_run_id="wr_test",
        workflow_run_block_id="wrb_loop",
        workflow_run_context=context,
        loop_over_values=["first", "second"],
        organization_id="o_test",
    )

    assert events == ["capture_settled", "tabs_reset"]


@pytest.mark.asyncio
async def test_while_loop_settles_failed_code_capture_before_condition_reevaluation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _context()
    capture_started = asyncio.Event()
    capture_cancelled = asyncio.Event()
    capture_release = asyncio.Event()
    condition_calls = 0
    inner = _block().model_copy(update={"next_loop_on_failure": True})
    loop = WhileLoopBlock(
        label="loop",
        output_parameter=_block().output_parameter.model_copy(update={"key": "loop_output"}),
        loop_blocks=[inner],
        condition=JinjaBranchCriteria(expression="true"),
    )

    async def capture(_: asyncio.Event) -> None:
        capture_started.set()
        try:
            await capture_release.wait()
        finally:
            capture_cancelled.set()

    async def execute_inner(**_: object) -> BlockResult:
        assert context.start_failure_evidence_capture("wrb_failed", capture) is True
        await capture_started.wait()
        return BlockResult(
            success=False,
            output_parameter=inner.output_parameter,
            status=BlockStatus.failed,
            failure_reason="specific runner failure",
            workflow_run_block_id="wrb_failed",
        )

    async def evaluate_condition(*_: object, **__: object) -> bool:
        nonlocal condition_calls
        condition_calls += 1
        if condition_calls == 1:
            return True
        assert capture_cancelled.is_set()
        return False

    object.__setattr__(inner, "execute_safe", execute_inner)
    monkeypatch.setattr(loop, "_evaluate_condition", evaluate_condition)
    monkeypatch.setattr(loop, "_persist_partial_loop_output", AsyncMock())

    result = await loop._execute_while_loop_helper(
        workflow_run_id="wr_test",
        workflow_run_block_id="wrb_loop",
        workflow_run_context=context,
        organization_id="o_test",
    )

    assert condition_calls == 2
    assert result.block_outputs[0].workflow_run_block_id == "wrb_failed"
    assert result.block_outputs[0].failure_reason == "specific runner failure"
    assert context.has_failure_evidence_capture is False


@pytest.mark.asyncio
async def test_ordinary_success_never_owns_a_capture(monkeypatch: pytest.MonkeyPatch) -> None:
    """The normal-run control: a block that succeeds through the real execute_safe leaves the
    context with no owned capture, writes no failure URL, and attaches no frame."""
    context = _context()
    updates: list[dict[str, object]] = []

    async def update_block(**kwargs: object) -> SimpleNamespace:
        updates.append(dict(kwargs))
        return SimpleNamespace(workflow_run_block_id="wrb_ok", workflow_run_id="wr_test", organization_id="o_test")

    create_artifact = AsyncMock()
    monkeypatch.setattr(block_module.app.WORKFLOW_CONTEXT_MANAGER, "get_workflow_run_context", lambda _: context)
    monkeypatch.setattr(
        block_module.app.DATABASE.observer,
        "create_workflow_run_block",
        AsyncMock(return_value=SimpleNamespace(workflow_run_block_id="wrb_ok")),
    )
    monkeypatch.setattr(block_module.app.DATABASE.observer, "update_workflow_run_block", update_block)
    monkeypatch.setattr(block_module.app.ARTIFACT_MANAGER, "create_workflow_run_block_artifact", create_artifact)
    monkeypatch.setattr(block_module.app.BROWSER_MANAGER, "get_for_workflow_run", lambda _: None)
    monkeypatch.setattr(
        block_module.skyvern_context, "current", lambda: SkyvernContext(workflow_run_id="wr_test", script_mode=True)
    )
    monkeypatch.setattr(
        CodeBlock,
        "execute",
        AsyncMock(
            return_value=BlockResult(
                success=True,
                output_parameter=_block().output_parameter,
                status=BlockStatus.completed,
                workflow_run_block_id="wrb_ok",
            )
        ),
    )

    result = await _block().execute_safe(workflow_run_id="wr_test", organization_id="o_test")

    assert result.success is True
    assert context.has_failure_evidence_capture is False
    assert not any(update.get("final_url") for update in updates)
    create_artifact.assert_not_awaited()


@pytest.mark.asyncio
async def test_successor_execute_safe_settles_capture_before_first_execution_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _context()
    events: list[str] = []

    async def cancel_capture() -> None:
        events.append("capture_settled")

    async def create_run_block(**_: object) -> SimpleNamespace:
        return SimpleNamespace(workflow_run_block_id="wrb_successor")

    def get_browser_state(_: str) -> None:
        events.append("browser_read")

    context.cancel_failure_evidence_capture = cancel_capture  # type: ignore[method-assign]
    monkeypatch.setattr(block_module.app.WORKFLOW_CONTEXT_MANAGER, "get_workflow_run_context", lambda _: context)
    monkeypatch.setattr(block_module.app.DATABASE.observer, "create_workflow_run_block", create_run_block)
    monkeypatch.setattr(block_module.app.BROWSER_MANAGER, "get_for_workflow_run", get_browser_state)
    monkeypatch.setattr(
        block_module.skyvern_context, "current", lambda: SkyvernContext(workflow_run_id="wr_test", script_mode=True)
    )
    monkeypatch.setattr(
        CodeBlock,
        "execute",
        AsyncMock(
            return_value=BlockResult(
                success=True,
                output_parameter=_block().output_parameter,
                status=BlockStatus.completed,
                workflow_run_block_id="wrb_successor",
            )
        ),
    )

    result = await _block().execute_safe(workflow_run_id="wr_test", organization_id="o_test")

    assert result.success is True
    assert events == ["capture_settled", "browser_read"]


@pytest.mark.asyncio
async def test_cached_script_successor_settles_capture_before_browser_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []
    context = _context()

    async def cancel_capture() -> None:
        events.append("capture_settled")

    def get_browser_state(*_: object, **__: object) -> None:
        events.append("browser_read")

    context.cancel_failure_evidence_capture = cancel_capture  # type: ignore[method-assign]
    monkeypatch.setattr(
        script_service_module.skyvern_context,
        "current",
        lambda: SkyvernContext(workflow_run_id="wr_test", organization_id="o_test"),
    )
    monkeypatch.setattr(script_service_module.app.WORKFLOW_CONTEXT_MANAGER, "has_workflow_run_context", lambda _: True)
    monkeypatch.setattr(
        script_service_module.app.WORKFLOW_CONTEXT_MANAGER, "get_workflow_run_context", lambda _: context
    )
    monkeypatch.setattr(script_service_module.app.BROWSER_MANAGER, "get_for_workflow_run", get_browser_state)
    monkeypatch.setattr(
        script_service_module.app.DATABASE.observer,
        "create_workflow_run_block",
        AsyncMock(
            return_value=SimpleNamespace(
                workflow_run_block_id="wrb_cached", workflow_run_id="wr_test", organization_id="o_test"
            )
        ),
    )

    await script_service_module._create_workflow_block_run_and_task(block_type=BlockType.CODE, label="run_code")

    assert events[:2] == ["capture_settled", "browser_read"]


@pytest.mark.asyncio
async def test_capture_settlement_failure_uses_protected_block_failure_path(monkeypatch: pytest.MonkeyPatch) -> None:
    context = _context()
    context.cancel_failure_evidence_capture = AsyncMock(side_effect=RuntimeError("capture settlement failed"))
    create_run_block = AsyncMock(return_value=SimpleNamespace(workflow_run_block_id="wrb_successor"))
    build_result = AsyncMock(
        return_value=BlockResult(
            success=False,
            output_parameter=_block().output_parameter,
            status=BlockStatus.failed,
            failure_reason="CodeBlock execution failed.",
            workflow_run_block_id="wrb_successor",
        )
    )
    browser_lookup = MagicMock()
    monkeypatch.setattr(block_module.app.WORKFLOW_CONTEXT_MANAGER, "get_workflow_run_context", lambda _: context)
    monkeypatch.setattr(block_module.app.DATABASE.observer, "create_workflow_run_block", create_run_block)
    monkeypatch.setattr(block_module.app.BROWSER_MANAGER, "get_for_workflow_run", browser_lookup)
    monkeypatch.setattr(CodeBlock, "_invalidate_stale_output_on_failure", AsyncMock())
    monkeypatch.setattr(CodeBlock, "build_block_result", build_result)

    result = await _block().execute_safe(workflow_run_id="wr_test", organization_id="o_test")

    assert result.failure_reason == "CodeBlock execution failed."
    assert build_result.await_args.kwargs["workflow_run_block_id"] == "wrb_successor"
    browser_lookup.assert_not_called()


@pytest.mark.asyncio
async def test_browser_cleanup_drains_owned_capture_before_teardown(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []
    context = _context()

    async def drain() -> None:
        events.append("capture_drained")

    async def cleanup(*_: object, **__: object) -> SimpleNamespace:
        events.append("browser_cleaned")
        return SimpleNamespace(browser_state=None, recording_finalized=True)

    context.drain_failure_evidence_capture = drain  # type: ignore[method-assign]
    monkeypatch.setattr(block_module.app.WORKFLOW_CONTEXT_MANAGER, "has_workflow_run_context", lambda _: True)
    monkeypatch.setattr(block_module.app.WORKFLOW_CONTEXT_MANAGER, "get_workflow_run_context", lambda _: context)
    monkeypatch.setattr(
        block_module.app.DATABASE.workflow_runs,
        "get_workflow_runs_by_parent_workflow_run_id",
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(block_module.app.BROWSER_MANAGER, "cleanup_for_workflow_run", cleanup)
    service = WorkflowService()
    monkeypatch.setattr(service, "get_tasks_by_workflow_run_id", AsyncMock(return_value=[]))

    await service._clean_up_workflow_browser(
        SimpleNamespace(workflow_run_id="wr_test", organization_id="o_test", browser_address=None)
    )

    assert events == ["capture_drained", "browser_cleaned"]


@pytest.mark.asyncio
async def test_browser_cleanup_drains_child_run_capture_before_shared_teardown(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []
    parent_context = _context()
    child_context = _context()

    async def drain_parent() -> None:
        events.append("parent_capture_drained")

    async def drain_child() -> None:
        events.append("child_capture_drained")

    async def cleanup(*_: object, **__: object) -> SimpleNamespace:
        events.append("browser_cleaned")
        return SimpleNamespace(browser_state=None, recording_finalized=True)

    parent_context.drain_failure_evidence_capture = drain_parent  # type: ignore[method-assign]
    child_context.drain_failure_evidence_capture = drain_child  # type: ignore[method-assign]
    contexts = {"wr_test": parent_context, "wr_child": child_context}
    monkeypatch.setattr(block_module.app.WORKFLOW_CONTEXT_MANAGER, "has_workflow_run_context", contexts.__contains__)
    monkeypatch.setattr(block_module.app.WORKFLOW_CONTEXT_MANAGER, "get_workflow_run_context", contexts.__getitem__)
    monkeypatch.setattr(
        block_module.app.DATABASE.workflow_runs,
        "get_workflow_runs_by_parent_workflow_run_id",
        AsyncMock(return_value=[SimpleNamespace(workflow_run_id="wr_child")]),
    )
    monkeypatch.setattr(block_module.app.BROWSER_MANAGER, "cleanup_for_workflow_run", cleanup)
    service = WorkflowService()
    monkeypatch.setattr(service, "get_tasks_by_workflow_run_id", AsyncMock(return_value=[]))

    await service._clean_up_workflow_browser(
        SimpleNamespace(workflow_run_id="wr_test", organization_id="o_test", browser_address=None)
    )

    assert events == ["parent_capture_drained", "child_capture_drained", "browser_cleaned"]


@pytest.mark.asyncio
@pytest.mark.parametrize("_sample", range(5))
async def test_capture_cancellation_does_not_skip_recorder_session_cleanup(
    monkeypatch: pytest.MonkeyPatch, _sample: int
) -> None:
    events: list[str] = []
    capture_started = asyncio.Event()
    context = _context()

    async def capture(_: asyncio.Event) -> None:
        capture_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            events.append("capture_cancelled")
            raise

    async def cleanup(*_: object, **kwargs: object) -> SimpleNamespace:
        assert kwargs["browser_session_id"] == "pbs_test"
        events.append("recorder_session_cleaned")
        return SimpleNamespace(browser_state=None, recording_finalized=False)

    assert context.start_failure_evidence_capture("wrb_test", capture) is True
    await asyncio.wait_for(capture_started.wait(), timeout=1)
    monkeypatch.setattr(block_module.app.WORKFLOW_CONTEXT_MANAGER, "has_workflow_run_context", lambda _: True)
    monkeypatch.setattr(block_module.app.WORKFLOW_CONTEXT_MANAGER, "get_workflow_run_context", lambda _: context)
    monkeypatch.setattr(
        block_module.app.DATABASE.workflow_runs,
        "get_workflow_runs_by_parent_workflow_run_id",
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(block_module.app.BROWSER_MANAGER, "cleanup_for_workflow_run", cleanup)
    service = WorkflowService()
    monkeypatch.setattr(service, "get_tasks_by_workflow_run_id", AsyncMock(return_value=[]))

    service_cleanup = asyncio.create_task(
        service._clean_up_workflow_browser(
            SimpleNamespace(workflow_run_id="wr_test", organization_id="o_test", browser_address=None),
            browser_session_id="pbs_test",
        )
    )
    await asyncio.sleep(0)
    await context.cancel_failure_evidence_capture()
    await service_cleanup

    assert events == ["capture_cancelled", "recorder_session_cleaned"]
    assert context.has_failure_evidence_capture is False


@pytest.mark.asyncio
@pytest.mark.parametrize("_sample", range(5))
async def test_real_soft_and_hard_deadlines_persist_and_emit_held_production_failure(
    monkeypatch: pytest.MonkeyPatch, _sample: int
) -> None:
    capture_started = asyncio.Event()
    capture_release = asyncio.Event()
    workflow_context = _context()
    block = _block()
    now = datetime.now(UTC)
    persisted_row = WorkflowRunBlock(
        workflow_run_block_id="wrb_test",
        workflow_run_id="wr_test",
        organization_id="o_test",
        block_type=block.block_type,
        label=block.label,
        created_at=now,
        modified_at=now,
    )

    async def capture(**_: object) -> None:
        capture_started.set()
        await capture_release.wait()

    async def update_row(**kwargs: object) -> WorkflowRunBlock:
        nonlocal persisted_row
        updates = {
            key: value
            for key, value in kwargs.items()
            if key in WorkflowRunBlock.model_fields and key not in {"workflow_run_block_id", "organization_id"}
        }
        persisted_row = persisted_row.model_copy(update={**updates, "modified_at": datetime.now(UTC)})
        return persisted_row

    async def publish_failure() -> BlockResult:
        return await block.build_block_result(
            success=False,
            output_parameter_value={"failure": "specific runner failure"},
            status=BlockStatus.failed,
            failure_reason="specific runner failure",
            workflow_run_block_id="wrb_test",
            organization_id="o_test",
            error_codes=["browser_operation_failed"],
        )

    monkeypatch.setattr(block, "_capture_failure_evidence", capture)
    monkeypatch.setattr(
        block_module.app.DATABASE,
        "observer",
        SimpleNamespace(
            update_workflow_run_block=update_row,
            get_workflow_run_blocks=AsyncMock(return_value=[]),
        ),
    )
    published = await block._publish_failure_result_with_evidence(
        build_failure_result=publish_failure,
        workflow_run_context=workflow_context,
        workflow_run_id="wr_test",
        workflow_run_block_id="wrb_test",
        organization_id="o_test",
        browser_state=None,
        page=SimpleNamespace(url="https://example.com/failure"),
    )
    await asyncio.wait_for(capture_started.wait(), timeout=1)
    assert published.failure_reason == "specific runner failure"
    assert workflow_context.has_failure_evidence_capture is True

    workflow = Workflow.model_construct(
        workflow_id="wf_test",
        workflow_definition=SimpleNamespace(blocks=[block]),
    )
    execution = _RunExecution(
        snapshot=CopilotExecutionSnapshot(
            provenance="canonical",
            workflow=workflow,
            workflow_parameters=(),
            output_parameters=(),
        ),
        workflow_yaml="",
        metadata={},
        associations={},
        source_at_start=None,
        unbound_keys=[],
        explicit_blank=False,
    )
    run_result = _ExecutionResult(
        build_run_blocks_response(
            False,
            {
                "workflow_run_id": "wr_test",
                "overall_status": "failed",
                "requested_block_labels": [block.label],
                "executed_block_labels": [block.label],
                "blocks": [_recorded_run_block_result(persisted_row)],
            },
        ),
        execution,
    )
    copilot_context = make_copilot_ctx()
    outcome = _record_run_blocks_result(copilot_context, run_result)
    finalize_build_test_result(
        copilot_context,
        source_tool="test_held_failure_deadline",
        result=run_result,
        diagnosis_shadow_eligible=False,
        recorded_outcome=run_result.execution.build_outcome,
    )
    assert outcome is not None
    assert outcome.display_reason == "specific runner failure"
    packet = run_result["data"][BUILD_TEST_PACKET_KEY]
    assert packet["failure"]["block_label"] == "run_code"
    assert packet["failure"]["block_type"] == "CODE"
    assert packet["failure"]["workflow_run_block_id"] == "wrb_test"
    assert packet["failure"]["reason"] == "specific runner failure"
    assert packet["failure"]["error_codes"] == ["browser_operation_failed"]
    assert persisted_row.output == {"failure": "specific runner failure"}

    dispatch_lookup = AsyncMock()
    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.tools.run_execution.app.DATABASE.workflows.get_workflow_by_permanent_id",
        dispatch_lookup,
    )
    first_result = SimpleNamespace(context_wrapper=SimpleNamespace(usage=None), cancel=MagicMock())
    unfinished_report = SimpleNamespace(
        context_wrapper=SimpleNamespace(usage=None),
        cancel=MagicMock(),
        final_output=None,
    )
    elapsed = 0.0
    runner_calls: list[dict[str, object]] = []
    stream_calls = 0
    report_started = asyncio.Event()
    report_interrupted = asyncio.Event()

    def run_streamed(*_: object, **kwargs: object) -> SimpleNamespace:
        runner_calls.append(dict(kwargs))
        return first_result if len(runner_calls) == 1 else unfinished_report

    async def consume_failure_during_drain(*_: object, **__: object) -> None:
        nonlocal elapsed, stream_calls
        stream_calls += 1
        if stream_calls == 1:
            assert workflow_context.has_failure_evidence_capture is True
            elapsed = 11.0
            return
        assert copilot_context.last_run_outcome is outcome
        assert workflow_context.has_failure_evidence_capture is True
        denial = await _run_blocks_and_collect_debug({"block_labels": ["run_code"]}, copilot_context)
        assert denial["data"] == {"budget_expired": True, "run_dispatched": False, "source": "deadline"}
        report_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            report_interrupted.set()
            raise

    monkeypatch.setattr(enforcement_module, "TOTAL_TIMEOUT_SECONDS", 10)
    monkeypatch.setattr(enforcement_module, "HARD_BACKSTOP_ALLOWANCE_SECONDS", 0)
    monkeypatch.setattr(enforcement_module, "MIN_DEADLINE_REMAINING_SECONDS", 0.01)
    monkeypatch.setattr(enforcement_module, "_elapsed_run_seconds", lambda *_: elapsed)
    monkeypatch.setattr(enforcement_module.Runner, "run_streamed", run_streamed)
    monkeypatch.setattr(enforcement_module.streaming_adapter, "stream_to_sse", consume_failure_during_drain)
    session = object()
    with pytest.raises(enforcement_module.CopilotTotalTimeoutError):
        await enforcement_module.run_with_enforcement(
            agent=MagicMock(),
            initial_input="report the held failure",
            ctx=copilot_context,
            stream=MagicMock(),
            session=session,
        )

    assert report_started.is_set()
    assert report_interrupted.is_set()
    assert len(runner_calls) == 2
    assert runner_calls[0]["session"] is session
    assert runner_calls[1]["session"] is session
    drain_input = json.loads(str(runner_calls[1]["input"]))
    assert drain_input["budget_expired"] is True
    assert drain_input["source"] == "deadline"
    assert copilot_context.budget_expiry_state.drain_attempted is True
    assert copilot_context.budget_expiry_state.hard_backstop_reached is True
    assert unfinished_report.final_output is None
    dispatch_lookup.assert_not_awaited()
    assert workflow_context.has_failure_evidence_capture is True

    hard_result = _build_timeout_exit_result(copilot_context, global_llm_context=None)
    assert hard_result.narrative_payload is not None
    assert hard_result.narrative_payload["turnFacts"]["runId"] == "wr_test"
    assert hard_result.narrative_payload["turnFacts"]["evaluationState"] == "not_demonstrated"
    # A zero-content drain is a typed terminal: the failure rides the facts, never manufactured prose.
    assert hard_result.user_response == ""
    assert hard_result.narrative_payload["turnFacts"]["recordedFailure"] == "specific runner failure"
    assert hard_result.narrative_payload["turnFacts"]["terminalCause"] == "browser_operation_failed"
    assert hard_result.turn_outcome is not None
    assert hard_result.turn_outcome.budget_expiry_report_produced is False

    chat = SimpleNamespace(
        organization_id="o_test",
        workflow_copilot_chat_id="chat_test",
        proposed_workflow=None,
        auto_accept=False,
    )
    original_workflow = SimpleNamespace(
        workflow_id="wf_test",
        title="Failure publication",
        description="",
        workflow_definition=None,
    )
    _, workflow_params = setup_new_copilot_mocks(monkeypatch, chat, original_workflow, hard_result)
    response_stream = MagicMock(send=AsyncMock(return_value=True))
    chat_request = WorkflowCopilotChatRequest(
        workflow_permanent_id="wpid_test",
        workflow_id="wf_test",
        workflow_copilot_chat_id="chat_test",
        message="report the held failure",
        workflow_yaml="title: Failure publication",
    )
    await workflow_copilot_route._finalise_normal_turn(
        stream=response_stream,
        chat=chat,
        organization_id="o_test",
        original_workflow=original_workflow,
        chat_request=chat_request,
        agent_result=hard_result,
        turn_id="turn_test",
        user_row_already_persisted=True,
    )

    saved_assistant = workflow_params.create_workflow_copilot_chat_message.await_args.kwargs
    assert saved_assistant["content"] == ""
    assert saved_assistant["turn_outcome"].budget_expiry_report_produced is False
    assert saved_assistant["narrative_payload"]["turnFacts"]["recordedFailure"] == "specific runner failure"
    assert saved_assistant["narrative_payload"]["turnFacts"]["runId"] == "wr_test"
    emitted = response_stream.send.await_args.args[0]
    assert emitted.message == ""
    assert emitted.narrative_payload is not None
    assert emitted.narrative_payload["turnFacts"]["recordedFailure"] == "specific runner failure"
    assert emitted.narrative_payload["turnFacts"]["runId"] == "wr_test"
    assert emitted.narrative_payload["turnFacts"]["evaluationState"] == "not_demonstrated"
    dispatch_lookup.assert_not_awaited()
    assert workflow_context.has_failure_evidence_capture is True

    capture_release.set()
    await workflow_context.drain_failure_evidence_capture()
    assert workflow_context.has_failure_evidence_capture is False


def test_hard_expiry_keeps_specific_recorded_failure_facts_without_empty_terminal() -> None:
    copilot_context = make_copilot_ctx()
    run_outcome = RecordedRunOutcome(
        verdict="not_demonstrated",
        workflow_run_id="wr_test",
        run_completed=False,
        display_reason="specific runner failure",
    )
    copilot_context.last_run_outcome = run_outcome
    copilot_context.run_outcome_trace.append(run_outcome)
    copilot_context.executed_block_labels.add("run_code")
    record_build_test_outcome(
        copilot_context,
        RecordedBuildTestOutcome(
            phase="persisted_block_run",
            attempted_tool="update_and_run_blocks",
            attempted_block_label="run_code",
            verdict="repairable_failure",
            reason_code="runtime_block_failure",
            workflow_run_id="wr_test",
            block_labels=["run_code"],
            structural_failure_identity="browser-operation",
            failed_operation=BuildTestFailedOperation(
                kind="browser_operation_failed",
                workflow_run_id="wr_test",
                workflow_run_block_id="wrb_test",
                block_label="run_code",
                failing_line=18,
            ),
        ),
    )

    result = _build_timeout_exit_result(copilot_context, global_llm_context=None)

    assert result.user_response == ""
    assert result.turn_outcome is not None
    assert result.turn_outcome.budget_expired is True
    assert result.turn_outcome.budget_expiry_report_produced is False
    assert result.narrative_payload is not None
    assert result.narrative_payload["turnFacts"]["recordedFailure"] == "specific runner failure"
    assert result.narrative_payload["turnFacts"] == {
        "factsAvailable": False,
        "evaluationState": "not_demonstrated",
        "runId": "wr_test",
        "runCompleted": False,
        "terminalCause": "browser_operation_failed",
        "blocksRunThisTurn": 1,
        "recordedFailure": "specific runner failure",
        "ranCleanOnCurrentSource": False,
    }
