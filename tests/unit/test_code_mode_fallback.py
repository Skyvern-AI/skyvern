from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyvern.forge.sdk.db.enums import WorkflowRunTriggerType
from skyvern.forge.sdk.workflow.code_mode_fallback import (
    _trigger_matches,
    maybe_start_code_mode_fallback_retry,
)
from skyvern.forge.sdk.workflow.models.workflow import WorkflowRun, WorkflowRunStatus

MODULE = "skyvern.forge.sdk.workflow.code_mode_fallback"


def _workflow_run(
    *,
    status: WorkflowRunStatus = WorkflowRunStatus.failed,
    run_with: str | None = "code",
    fallback_attempt: int | None = None,
    parent_workflow_run_id: str | None = None,
    debug_session_id: str | None = None,
    copilot_session_id: str | None = None,
    retried_from_workflow_run_id: str | None = None,
) -> WorkflowRun:
    now = datetime.now(timezone.utc)
    return WorkflowRun(
        workflow_run_id="wr_failed",
        workflow_id="wf_test",
        workflow_permanent_id="wpid_test",
        organization_id="org_test",
        status=status,
        run_with=run_with,
        failure_reason="element not found",
        fallback_attempt=fallback_attempt,
        parent_workflow_run_id=parent_workflow_run_id,
        debug_session_id=debug_session_id,
        copilot_session_id=copilot_session_id,
        retried_from_workflow_run_id=retried_from_workflow_run_id,
        trigger_type=WorkflowRunTriggerType.api,
        created_at=now,
        modified_at=now,
    )


def _mock_app(*, flag_enabled: bool = True, existing_retry: str | None = None, block_scoped: bool = False) -> MagicMock:
    mock_app = MagicMock()
    provider = SimpleNamespace(is_feature_enabled_cached=AsyncMock(return_value=flag_enabled))
    mock_app.EXPERIMENTATION_PROVIDER = provider
    mock_app.AGENT_FUNCTION.is_block_scoped_workflow_run = AsyncMock(return_value=block_scoped)
    mock_app.AGENT_FUNCTION.strip_proxy_session_extra_http_headers = MagicMock(return_value=None)
    mock_app.DATABASE.debug.has_block_run_for_workflow_run = AsyncMock(return_value=False)
    mock_app.DATABASE.workflow_runs.get_workflow_run_retried_by = AsyncMock(return_value=existing_retry)
    mock_app.DATABASE.workflow_runs.get_workflow_run_parameters = AsyncMock(return_value=[])
    mock_app.DATABASE.organizations.get_organization = AsyncMock(
        return_value=SimpleNamespace(organization_id="org_test")
    )
    mock_app.DATABASE.tags.get_active_grouped_tags_for_run = AsyncMock(return_value={})
    mock_app.WORKFLOW_SERVICE.get_workflow = AsyncMock(
        return_value=SimpleNamespace(version=7, workflow_permanent_id="wpid_test")
    )
    return mock_app


@pytest.mark.parametrize(
    "status,run_with,expected",
    [
        (WorkflowRunStatus.failed, "code", True),
        (WorkflowRunStatus.terminated, "code", True),
        (WorkflowRunStatus.timed_out, "code", False),
        (WorkflowRunStatus.completed, "code", False),
        (WorkflowRunStatus.failed, "agent", False),
        (WorkflowRunStatus.failed, None, False),
    ],
)
def test_trigger_matches(status: WorkflowRunStatus, run_with: str | None, expected: bool) -> None:
    assert _trigger_matches(_workflow_run(status=status, run_with=run_with)) is expected


@pytest.mark.asyncio
async def test_spawns_agent_retry_for_failed_code_run() -> None:
    workflow_run = _workflow_run()
    request = MagicMock()
    with (
        patch(f"{MODULE}.app", _mock_app()),
        patch(
            "skyvern.services.workflow_service.workflow_request_body_from_existing_run",
            MagicMock(return_value=request),
        ),
        patch(
            "skyvern.services.workflow_service.run_workflow",
            AsyncMock(return_value=SimpleNamespace(workflow_run_id="wr_retry")),
        ) as run_workflow,
    ):
        result = await maybe_start_code_mode_fallback_retry(workflow_run, "org_test")

    assert result == "wr_retry"
    # The retry must run as a pure agent and drop the code run's browser handles.
    assert request.run_with == "agent"
    assert request.browser_session_id is None
    assert request.browser_profile_id is None
    _, kwargs = run_workflow.call_args
    assert kwargs["retried_from_workflow_run_id"] == "wr_failed"
    assert kwargs["fallback_attempt"] == 1


@pytest.mark.asyncio
async def test_no_retry_when_flag_disabled() -> None:
    with (
        patch(f"{MODULE}.app", _mock_app(flag_enabled=False)),
        patch("skyvern.services.workflow_service.run_workflow", AsyncMock()) as run_workflow,
    ):
        result = await maybe_start_code_mode_fallback_retry(_workflow_run(), "org_test")

    assert result is None
    run_workflow.assert_not_called()


@pytest.mark.asyncio
async def test_returns_existing_retry_without_spawning() -> None:
    with (
        patch(f"{MODULE}.app", _mock_app(existing_retry="wr_prior")),
        patch("skyvern.services.workflow_service.run_workflow", AsyncMock()) as run_workflow,
    ):
        result = await maybe_start_code_mode_fallback_retry(_workflow_run(), "org_test")

    assert result == "wr_prior"
    run_workflow.assert_not_called()


@pytest.mark.asyncio
async def test_no_retry_when_block_scoped() -> None:
    with (
        patch(f"{MODULE}.app", _mock_app(block_scoped=True)),
        patch("skyvern.services.workflow_service.run_workflow", AsyncMock()) as run_workflow,
    ):
        result = await maybe_start_code_mode_fallback_retry(_workflow_run(), "org_test")

    assert result is None
    run_workflow.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kwargs",
    [
        {"run_with": "agent"},
        {"run_with": None},
        {"status": WorkflowRunStatus.timed_out},
        {"fallback_attempt": 1},
        {"retried_from_workflow_run_id": "wr_orig"},
        {"parent_workflow_run_id": "wr_parent"},
        {"debug_session_id": "dbg_1"},
        {"copilot_session_id": "cop_1"},
    ],
)
async def test_ineligible_runs_never_spawn(kwargs: dict) -> None:
    with (
        patch(f"{MODULE}.app", _mock_app()),
        patch("skyvern.services.workflow_service.run_workflow", AsyncMock()) as run_workflow,
    ):
        result = await maybe_start_code_mode_fallback_retry(_workflow_run(**kwargs), "org_test")

    assert result is None
    run_workflow.assert_not_called()
