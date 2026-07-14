from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import orjson
import pytest
from fastapi import BackgroundTasks, HTTPException

from skyvern.exceptions import WorkflowNotFound
from skyvern.forge.sdk.db.enums import WorkflowRunTriggerType
from skyvern.forge.sdk.routes import agent_protocol
from skyvern.forge.sdk.workflow.models.tags import CallerType, TagSource
from skyvern.forge.sdk.workflow.models.workflow import WorkflowRequestBody, WorkflowRunStatus
from skyvern.schemas.run_enums import RunType
from skyvern.schemas.runs import MAX_SEARCH_FETCH_LIMIT


def _caller(org_id: str = "org_123") -> SimpleNamespace:
    return SimpleNamespace(
        organization=SimpleNamespace(organization_id=org_id),
        caller_id="user_123",
        caller_type=CallerType.USER,
    )


@pytest.mark.asyncio
async def test_get_runs_v2_serializes_mapping_rows_from_database(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_workflow_runs = SimpleNamespace(
        get_all_runs_v2=AsyncMock(
            return_value=[
                {
                    "task_run_id": "tr_123",
                    "run_id": "wr_123",
                    "task_run_type": "workflow_run",
                    "status": "completed",
                    "title": "Workflow run",
                    "started_at": None,
                    "finished_at": None,
                    "created_at": "2026-04-01T00:00:00Z",
                    "workflow_permanent_id": "wpid_123",
                    "workflow_deleted": False,
                    "script_run": False,
                    "trigger_type": "mcp",
                    "searchable_text": "Workflow run",
                }
            ]
        )
    )
    mock_database = SimpleNamespace(workflow_runs=mock_workflow_runs)
    monkeypatch.setattr(agent_protocol.app, "DATABASE", mock_database)

    response = await agent_protocol.get_runs_v2(
        current_org=SimpleNamespace(organization_id="org_123"),
        page=2,
        page_size=5,
        search_key="abc",
        run_type=[RunType.workflow_run, RunType.task_v1],
    )

    mock_workflow_runs.get_all_runs_v2.assert_awaited_once_with(
        "org_123",
        page=2,
        page_size=5,
        status=None,
        search_key="abc",
        run_type=["workflow_run", "task_v1"],
        run_tags=None,
    )
    assert orjson.loads(response.body) == [
        {
            "task_run_id": "tr_123",
            "run_id": "wr_123",
            "task_run_type": "workflow_run",
            "status": "completed",
            "title": "Workflow run",
            "started_at": None,
            "finished_at": None,
            "created_at": "2026-04-01T00:00:00Z",
            "workflow_permanent_id": "wpid_123",
            "workflow_deleted": False,
            "script_run": False,
            "trigger_type": "mcp",
        }
    ]


@pytest.mark.asyncio
async def test_get_runs_v2_rejects_search_page_beyond_fetch_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_workflow_runs = SimpleNamespace(get_all_runs_v2=AsyncMock(return_value=[]))
    mock_database = SimpleNamespace(workflow_runs=mock_workflow_runs)
    monkeypatch.setattr(agent_protocol.app, "DATABASE", mock_database)

    page_size = 100
    page = (MAX_SEARCH_FETCH_LIMIT // page_size) + 1

    with pytest.raises(HTTPException) as exc_info:
        await agent_protocol.get_runs_v2(
            current_org=SimpleNamespace(organization_id="org_123"),
            page=page,
            page_size=page_size,
            search_key="wr_abc123",
        )

    assert exc_info.value.status_code == 400
    assert str(MAX_SEARCH_FETCH_LIMIT) in exc_info.value.detail
    mock_workflow_runs.get_all_runs_v2.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("handler", "expected_exclude_child_runs"),
    [
        (agent_protocol.get_workflow_runs_by_id, True),
        (agent_protocol.get_workflow_runs_by_id_legacy, False),
    ],
)
async def test_get_workflow_runs_by_id_child_filter_depends_on_route(
    monkeypatch: pytest.MonkeyPatch,
    handler: Any,
    expected_exclude_child_runs: bool,
) -> None:
    mock_service = SimpleNamespace(
        get_workflow_runs_for_workflow_permanent_id=AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(agent_protocol.app, "WORKFLOW_SERVICE", mock_service)
    monkeypatch.setattr(agent_protocol.analytics, "capture", lambda *args, **kwargs: None)

    response = await handler(
        workflow_id="wpid_123",
        page=2,
        page_size=5,
        status=[WorkflowRunStatus.failed],
        search_key="login",
        error_code="LOGIN_FAILED",
        current_org=SimpleNamespace(organization_id="org_123"),
    )

    assert response == []
    mock_service.get_workflow_runs_for_workflow_permanent_id.assert_awaited_once_with(
        workflow_permanent_id="wpid_123",
        organization_id="org_123",
        page=2,
        page_size=5,
        status=[WorkflowRunStatus.failed],
        search_key="login",
        error_code="LOGIN_FAILED",
        exclude_child_runs=expected_exclude_child_runs,
        created_at_start=None,
        created_at_end=None,
        run_tags=None,
    )


@pytest.mark.asyncio
async def test_retry_workflow_run_replays_original_run_parameters(monkeypatch: pytest.MonkeyPatch) -> None:
    created_at = datetime(2026, 4, 1, tzinfo=timezone.utc)
    original_run = SimpleNamespace(
        workflow_run_id="wr_original",
        workflow_id="wf_original",
        workflow_permanent_id="wpid_123",
        status=WorkflowRunStatus.failed,
        proxy_location=None,
        webhook_callback_url="https://example.com/webhook",
        totp_verification_url="https://example.com/totp",
        totp_identifier="account@example.com",
        browser_session_id="pbs_123",
        browser_profile_id="bprof_123",
        max_screenshot_scrolls=3,
        extra_http_headers={"X-Test": "1"},
        cdp_connect_headers={"X-CDP-Auth": "secret"},
        browser_address="http://127.0.0.1:9222",
        run_with="code",
        ai_fallback=True,
        debug_session_id=None,
        code_gen=None,
        ignore_inherited_workflow_system_prompt=True,
    )
    retried_run = SimpleNamespace(
        workflow_run_id="wr_retry",
        workflow_id="wf_original",
        status=WorkflowRunStatus.created,
        failure_reason=None,
        created_at=created_at,
        modified_at=created_at,
        browser_session_id="pbs_123",
        browser_profile_id="bprof_123",
        run_with="code",
        ai_fallback=True,
    )

    mock_workflow_runs = SimpleNamespace(
        get_workflow_run=AsyncMock(return_value=original_run),
        get_workflow_run_parameters=AsyncMock(
            return_value=[(SimpleNamespace(key="customer"), SimpleNamespace(value="acme"))]
        ),
    )
    mock_tags = SimpleNamespace(
        get_active_grouped_tags_for_run=AsyncMock(return_value={"env": "prod", "skyvern.platform": "example-platform"})
    )
    mock_debug = SimpleNamespace(has_block_run_for_workflow_run=AsyncMock(return_value=False))
    mock_database = SimpleNamespace(workflow_runs=mock_workflow_runs, debug=mock_debug, tags=mock_tags)
    mock_workflow_service = SimpleNamespace(
        get_workflow=AsyncMock(
            return_value=SimpleNamespace(version=7, title="Original workflow title", organization_id="org_123")
        ),
    )
    mock_rate_limiter = SimpleNamespace(rate_limit_submit_run=AsyncMock())
    monkeypatch.setattr(agent_protocol.app, "DATABASE", mock_database)
    monkeypatch.setattr(agent_protocol.app, "WORKFLOW_SERVICE", mock_workflow_service)
    app_instance = object.__getattribute__(agent_protocol.app, "_inst")
    monkeypatch.setattr(app_instance, "RATE_LIMITER", mock_rate_limiter, raising=False)
    monkeypatch.setattr(agent_protocol.analytics, "capture", lambda *args, **kwargs: None)
    monkeypatch.setattr(agent_protocol.skyvern_context, "ensure_context", lambda: SimpleNamespace(request_id="req_123"))
    mock_agent_function = SimpleNamespace(
        is_block_scoped_workflow_run=AsyncMock(return_value=False),
    )
    monkeypatch.setattr(app_instance, "AGENT_FUNCTION", mock_agent_function, raising=False)

    mock_permission_checker = SimpleNamespace(check=AsyncMock())
    monkeypatch.setattr(
        agent_protocol.PermissionCheckerFactory,
        "get_instance",
        lambda: mock_permission_checker,
    )

    run_workflow_mock = AsyncMock(return_value=retried_run)
    monkeypatch.setattr(agent_protocol.workflow_service, "run_workflow", run_workflow_mock)

    caller = _caller()
    response = await agent_protocol.retry_workflow_run(
        request=SimpleNamespace(),
        background_tasks=BackgroundTasks(),
        workflow_run_id="wr_original",
        caller=caller,
        x_api_key="api-key",
        x_max_steps_override=10,
        x_user_agent="skyvern-ui",
    )

    mock_workflow_runs.get_workflow_run.assert_awaited_once_with(
        workflow_run_id="wr_original",
        organization_id="org_123",
    )
    mock_debug.has_block_run_for_workflow_run.assert_awaited_once_with(
        organization_id="org_123",
        workflow_run_id="wr_original",
    )
    mock_agent_function.is_block_scoped_workflow_run.assert_awaited_once_with(original_run)
    mock_permission_checker.check.assert_awaited_once_with(caller.organization, browser_session_id="pbs_123")
    mock_rate_limiter.rate_limit_submit_run.assert_awaited_once_with("org_123")
    mock_workflow_service.get_workflow.assert_awaited_once_with(
        workflow_id="wf_original",
        organization_id=None,
    )
    mock_workflow_runs.get_workflow_run_parameters.assert_awaited_once_with(
        workflow_run_id="wr_original",
    )
    mock_tags.get_active_grouped_tags_for_run.assert_awaited_once_with(
        workflow_run_id="wr_original",
        organization_id="org_123",
    )

    run_workflow_mock.assert_awaited_once()
    call_kwargs = run_workflow_mock.call_args.kwargs
    assert call_kwargs["workflow_id"] == "wpid_123"
    assert call_kwargs["template"] is False
    assert call_kwargs["version"] == 7
    assert call_kwargs["max_steps"] == 10
    assert call_kwargs["api_key"] == "api-key"
    assert call_kwargs["request_id"] == "req_123"
    assert call_kwargs["trigger_type"] == WorkflowRunTriggerType.manual
    assert call_kwargs["ignore_inherited_workflow_system_prompt"] is True
    assert call_kwargs["tag_write_context"].caller_id == "user_123"
    assert call_kwargs["tag_write_context"].source == TagSource.MANUAL
    assert call_kwargs["tag_write_context"].caller_type == CallerType.USER
    assert isinstance(call_kwargs["workflow_request"], WorkflowRequestBody)
    assert call_kwargs["workflow_request"].data == {"customer": "acme"}
    assert call_kwargs["workflow_request"].webhook_callback_url == "https://example.com/webhook"
    assert call_kwargs["workflow_request"].totp_verification_url == "https://example.com/totp"
    assert call_kwargs["workflow_request"].totp_identifier == "account@example.com"
    assert call_kwargs["workflow_request"].browser_session_id == "pbs_123"
    assert call_kwargs["workflow_request"].browser_profile_id == "bprof_123"
    assert call_kwargs["workflow_request"].max_screenshot_scrolls == 3
    assert call_kwargs["workflow_request"].extra_http_headers == {"X-Test": "1"}
    assert call_kwargs["workflow_request"].cdp_connect_headers == {"X-CDP-Auth": "secret"}
    assert call_kwargs["workflow_request"].browser_address == "http://127.0.0.1:9222"
    assert call_kwargs["workflow_request"].run_with == "code"
    assert call_kwargs["workflow_request"].ai_fallback is True
    assert call_kwargs["workflow_request"].run_metadata == {"env": "prod"}

    assert response.run_id == "wr_retry"
    assert response.run_request is not None
    assert response.run_request.workflow_id == "wpid_123"
    assert response.run_request.title == "Original workflow title"
    assert response.run_request.parameters == {"customer": "acme"}


@pytest.mark.asyncio
async def test_retry_workflow_run_rejects_block_scoped_run(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_debug = SimpleNamespace(has_block_run_for_workflow_run=AsyncMock(return_value=True))
    mock_workflow_runs = SimpleNamespace(
        get_workflow_run=AsyncMock(
            return_value=SimpleNamespace(
                workflow_run_id="wr_block",
                status=WorkflowRunStatus.failed,
                debug_session_id=None,
                code_gen=None,
            )
        )
    )
    monkeypatch.setattr(
        agent_protocol.app,
        "DATABASE",
        SimpleNamespace(workflow_runs=mock_workflow_runs, debug=mock_debug),
    )
    app_instance = object.__getattribute__(agent_protocol.app, "_inst")
    monkeypatch.setattr(
        app_instance,
        "AGENT_FUNCTION",
        SimpleNamespace(is_block_scoped_workflow_run=AsyncMock(return_value=False)),
        raising=False,
    )
    monkeypatch.setattr(agent_protocol.analytics, "capture", lambda *args, **kwargs: None)

    with pytest.raises(HTTPException) as exc_info:
        await agent_protocol.retry_workflow_run(
            request=SimpleNamespace(),
            background_tasks=BackgroundTasks(),
            workflow_run_id="wr_block",
            caller=_caller(),
            x_api_key=None,
            x_max_steps_override=None,
            x_user_agent=None,
        )

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "Block-scoped workflow runs cannot be retried with this endpoint"
    mock_debug.has_block_run_for_workflow_run.assert_awaited_once_with(
        organization_id="org_123",
        workflow_run_id="wr_block",
    )


@pytest.mark.asyncio
async def test_retry_workflow_run_replays_template_runs_as_templates(monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime(2026, 4, 1, tzinfo=timezone.utc)
    original_run = SimpleNamespace(
        workflow_run_id="wr_template",
        workflow_id="wf_template",
        workflow_permanent_id="wpid_template",
        status=WorkflowRunStatus.completed,
        proxy_location=None,
        webhook_callback_url=None,
        totp_verification_url=None,
        totp_identifier=None,
        browser_session_id=None,
        browser_profile_id=None,
        max_screenshot_scrolls=None,
        extra_http_headers=None,
        cdp_connect_headers=None,
        browser_address=None,
        run_with=None,
        ai_fallback=None,
        debug_session_id=None,
        code_gen=None,
        ignore_inherited_workflow_system_prompt=False,
    )
    retried_run = SimpleNamespace(
        workflow_run_id="wr_template_retry",
        workflow_id="wf_template",
        status=WorkflowRunStatus.created,
        failure_reason=None,
        created_at=now,
        modified_at=now,
        browser_session_id=None,
        browser_profile_id=None,
        run_with=None,
        ai_fallback=None,
    )
    mock_workflow_runs = SimpleNamespace(
        get_workflow_run=AsyncMock(return_value=original_run),
        get_workflow_run_parameters=AsyncMock(return_value=[]),
    )
    mock_debug = SimpleNamespace(has_block_run_for_workflow_run=AsyncMock(return_value=False))
    mock_tags = SimpleNamespace(get_active_grouped_tags_for_run=AsyncMock(side_effect=RuntimeError("tags unavailable")))
    mock_database = SimpleNamespace(workflow_runs=mock_workflow_runs, debug=mock_debug, tags=mock_tags)
    mock_workflow_service = SimpleNamespace(
        get_workflow=AsyncMock(
            return_value=SimpleNamespace(version=3, title="Template title", organization_id="template_org")
        ),
    )
    mock_rate_limiter = SimpleNamespace(rate_limit_submit_run=AsyncMock())
    monkeypatch.setattr(agent_protocol.app, "DATABASE", mock_database)
    monkeypatch.setattr(agent_protocol.app, "WORKFLOW_SERVICE", mock_workflow_service)
    app_instance = object.__getattribute__(agent_protocol.app, "_inst")
    monkeypatch.setattr(app_instance, "RATE_LIMITER", mock_rate_limiter, raising=False)
    monkeypatch.setattr(
        app_instance,
        "AGENT_FUNCTION",
        SimpleNamespace(
            is_block_scoped_workflow_run=AsyncMock(return_value=False),
        ),
        raising=False,
    )
    monkeypatch.setattr(agent_protocol.analytics, "capture", lambda *args, **kwargs: None)
    monkeypatch.setattr(agent_protocol.skyvern_context, "ensure_context", lambda: SimpleNamespace(request_id="req_123"))
    monkeypatch.setattr(
        agent_protocol.PermissionCheckerFactory,
        "get_instance",
        lambda: SimpleNamespace(check=AsyncMock()),
    )

    run_workflow_mock = AsyncMock(return_value=retried_run)
    monkeypatch.setattr(agent_protocol.workflow_service, "run_workflow", run_workflow_mock)

    await agent_protocol.retry_workflow_run(
        request=SimpleNamespace(),
        background_tasks=BackgroundTasks(),
        workflow_run_id="wr_template",
        caller=_caller(),
        x_api_key=None,
        x_max_steps_override=None,
        x_user_agent=None,
    )

    run_workflow_mock.assert_awaited_once()
    assert run_workflow_mock.call_args.kwargs["template"] is True
    assert run_workflow_mock.call_args.kwargs["version"] == 3
    assert run_workflow_mock.call_args.kwargs["ignore_inherited_workflow_system_prompt"] is False
    assert run_workflow_mock.call_args.kwargs["workflow_request"].run_metadata is None
    assert run_workflow_mock.call_args.kwargs["tag_write_context"].caller_id == "user_123"
    mock_workflow_service.get_workflow.assert_awaited_once_with(
        workflow_id="wf_template",
        organization_id=None,
    )
    mock_workflow_runs.get_workflow_run_parameters.assert_awaited_once_with(
        workflow_run_id="wr_template",
    )
    app_instance.AGENT_FUNCTION.is_block_scoped_workflow_run.assert_awaited_once_with(original_run)


@pytest.mark.asyncio
async def test_retry_workflow_run_rejects_missing_workflow(monkeypatch: pytest.MonkeyPatch) -> None:
    original_run = SimpleNamespace(
        workflow_run_id="wr_missing_workflow",
        workflow_id="wf_missing",
        workflow_permanent_id="wpid_missing",
        status=WorkflowRunStatus.failed,
        browser_session_id=None,
        debug_session_id=None,
        code_gen=None,
    )
    mock_database = SimpleNamespace(
        workflow_runs=SimpleNamespace(
            get_workflow_run=AsyncMock(return_value=original_run),
            get_workflow_run_parameters=AsyncMock(),
        ),
        debug=SimpleNamespace(has_block_run_for_workflow_run=AsyncMock(return_value=False)),
    )
    mock_workflow_service = SimpleNamespace(
        get_workflow=AsyncMock(side_effect=WorkflowNotFound(workflow_id="wf_missing")),
    )
    monkeypatch.setattr(agent_protocol.app, "DATABASE", mock_database)
    monkeypatch.setattr(agent_protocol.app, "WORKFLOW_SERVICE", mock_workflow_service)
    app_instance = object.__getattribute__(agent_protocol.app, "_inst")
    monkeypatch.setattr(
        app_instance,
        "AGENT_FUNCTION",
        SimpleNamespace(is_block_scoped_workflow_run=AsyncMock(return_value=False)),
        raising=False,
    )
    monkeypatch.setattr(
        agent_protocol.PermissionCheckerFactory,
        "get_instance",
        lambda: SimpleNamespace(check=AsyncMock()),
    )
    monkeypatch.setattr(app_instance, "RATE_LIMITER", SimpleNamespace(rate_limit_submit_run=AsyncMock()), raising=False)
    monkeypatch.setattr(agent_protocol.analytics, "capture", lambda *args, **kwargs: None)

    with pytest.raises(HTTPException) as exc_info:
        await agent_protocol.retry_workflow_run(
            request=SimpleNamespace(),
            background_tasks=BackgroundTasks(),
            workflow_run_id="wr_missing_workflow",
            caller=_caller(),
            x_api_key=None,
            x_max_steps_override=None,
            x_user_agent=None,
        )

    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == "Workflow not found for run wr_missing_workflow"
    mock_database.workflow_runs.get_workflow_run_parameters.assert_not_awaited()


@pytest.mark.asyncio
async def test_retry_workflow_run_rejects_active_run(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_workflow_runs = SimpleNamespace(
        get_workflow_run=AsyncMock(
            return_value=SimpleNamespace(workflow_run_id="wr_running", status=WorkflowRunStatus.running)
        )
    )
    monkeypatch.setattr(agent_protocol.app, "DATABASE", SimpleNamespace(workflow_runs=mock_workflow_runs))
    monkeypatch.setattr(agent_protocol.analytics, "capture", lambda *args, **kwargs: None)

    with pytest.raises(HTTPException) as exc_info:
        await agent_protocol.retry_workflow_run(
            request=SimpleNamespace(),
            background_tasks=BackgroundTasks(),
            workflow_run_id="wr_running",
            caller=_caller(),
            x_api_key=None,
            x_max_steps_override=None,
            x_user_agent=None,
        )

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "Only terminal workflow runs can be retried"
