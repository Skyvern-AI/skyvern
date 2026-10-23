"""TaskV2Block.execute must forward the parent run's remote-browser connection
fields into the child TaskV2 it spawns. Without this, a workflow pinned to a
cloud browser (CDP browser_address + auth headers) silently launches a fresh
local Chrome for the TaskV2 child instead of reusing the cloud session."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from skyvern.exceptions import BROWSER_CONNECTION_LOST_ERROR_CODE
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.schemas.task_v2 import TaskV2Status
from skyvern.forge.sdk.workflow.models import block as block_module
from skyvern.forge.sdk.workflow.models.block import TaskV2Block
from skyvern.forge.sdk.workflow.models.parameter import OutputParameter, ParameterType
from skyvern.forge.sdk.workflow.models.workflow import WorkflowRunStatus
from skyvern.forge.sdk.workflow.retry_policy import evaluate_retry_policy
from skyvern.schemas.workflows import WorkflowRetryPolicy
from skyvern.webeye.real_browser_manager import RealBrowserManager
from skyvern.webeye.real_browser_state import RealBrowserState
from tests.unit._task_block_fakes import run_bound_browser_state


@pytest.fixture(autouse=True)
def reset_context() -> None:
    skyvern_context.reset()
    yield
    skyvern_context.reset()


def _output_parameter(key: str) -> OutputParameter:
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    return OutputParameter(
        parameter_type=ParameterType.OUTPUT,
        key=key,
        output_parameter_id=f"op_{key}",
        workflow_id="wf_test",
        created_at=now,
        modified_at=now,
    )


@pytest.mark.asyncio
async def test_taskv2_block_forwards_browser_connection_fields_to_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    skyvern_context.set(SkyvernContext(organization_id="org_1", workflow_run_id="wr_parent", run_id="wr_parent"))

    # Parent run is pinned to a cloud browser: a CDP address plus the auth
    # headers the handshake needs. All three must reach the child.
    outer_workflow_run = SimpleNamespace(
        proxy_location=None,
        max_screenshot_scrolls=None,
        browser_address="wss://sessions.skyvern.com/abc123",
        extra_http_headers={"x-api-key": "sk-test"},
        cdp_connect_headers={"x-cdp-auth": "tok"},
    )
    child_workflow_run = SimpleNamespace(failure_reason=None)
    organization = SimpleNamespace(
        organization_id="org_1",
        organization_name="Org 1",
        default_llm_key=None,
        default_secondary_llm_key=None,
    )

    fake_app = SimpleNamespace(
        DATABASE=SimpleNamespace(
            organizations=SimpleNamespace(get_organization=AsyncMock(return_value=organization)),
            workflow_runs=SimpleNamespace(
                get_workflow_run=AsyncMock(side_effect=[outer_workflow_run, child_workflow_run]),
                update_workflow_run=AsyncMock(),
            ),
            observer=SimpleNamespace(
                update_task_v2=AsyncMock(),
                update_workflow_run_block=AsyncMock(),
            ),
        ),
        WORKFLOW_SERVICE=SimpleNamespace(
            get_recent_task_screenshot_artifacts=AsyncMock(return_value=[]),
            get_recent_workflow_screenshot_artifacts=AsyncMock(return_value=[]),
        ),
        STORAGE=SimpleNamespace(
            get_current_attempt_downloaded_files=AsyncMock(side_effect=[[], []]),
            get_downloaded_file_signature_aliases=lambda _: [],
        ),
        WORKFLOW_CONTEXT_MANAGER=SimpleNamespace(has_workflow_run_context=lambda _run_id: False),
    )
    monkeypatch.setattr(block_module, "app", fake_app)

    from skyvern.services import task_v2_service

    init_mock = AsyncMock(return_value=SimpleNamespace(observer_cruise_id="tsk_v2_1", workflow_run_id="wr_child"))
    monkeypatch.setattr(task_v2_service, "initialize_task_v2", init_mock)

    completed_task_v2 = SimpleNamespace(
        observer_cruise_id="tsk_v2_1",
        workflow_run_id="wr_child",
        output={"result": "ok"},
        status=TaskV2Status.completed,
        summary="done",
        failure_category=None,
    )

    async def fake_run_task_v2(**_: object) -> SimpleNamespace:
        return completed_task_v2

    monkeypatch.setattr(task_v2_service, "run_task_v2", fake_run_task_v2)

    monkeypatch.setattr(
        TaskV2Block,
        "get_workflow_run_context",
        lambda self, workflow_run_id: SimpleNamespace(credential_totp_identifiers={}),
    )
    monkeypatch.setattr(TaskV2Block, "format_potential_template_parameters", lambda self, _: None)

    async def fake_record_output_parameter_value(self: TaskV2Block, *_: object, **__: object) -> None:
        return None

    async def fake_build_block_result(self: TaskV2Block, **kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(**kwargs)

    monkeypatch.setattr(TaskV2Block, "record_output_parameter_value", fake_record_output_parameter_value)
    monkeypatch.setattr(TaskV2Block, "build_block_result", fake_build_block_result)

    block = TaskV2Block(
        label="task1",
        output_parameter=_output_parameter("task1_output"),
        prompt="do the thing",
        url="https://example.com",
    )

    await block.execute(
        workflow_run_id="wr_parent",
        workflow_run_block_id="wrb_1",
        organization_id="org_1",
    )

    assert init_mock.await_count == 1
    kwargs = init_mock.await_args.kwargs
    assert kwargs["browser_address"] == "wss://sessions.skyvern.com/abc123"
    assert kwargs["extra_http_headers"] == {"x-api-key": "sk-test"}
    assert kwargs["cdp_connect_headers"] == {"x-cdp-auth": "tok"}


@pytest.mark.asyncio
async def test_taskv2_block_settles_the_child_capture_when_the_runner_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A raise out of run_task_v2 still hands the shared page back to the parent, and a
    continue_on_failure parent can start its next browser block, so a failed child code block's
    owned capture must be settled on that path too."""
    skyvern_context.set(SkyvernContext(organization_id="org_1", workflow_run_id="wr_parent", run_id="wr_parent"))
    settled: list[str] = []

    async def cancel_child_capture() -> None:
        settled.append("wr_child")

    child_context = SimpleNamespace(cancel_failure_evidence_capture=cancel_child_capture)
    organization = SimpleNamespace(
        organization_id="org_1",
        max_steps_per_run=None,
        default_llm_key=None,
        default_secondary_llm_key=None,
    )
    fake_app = SimpleNamespace(
        DATABASE=SimpleNamespace(
            organizations=SimpleNamespace(get_organization=AsyncMock(return_value=organization)),
            workflow_runs=SimpleNamespace(
                get_workflow_run=AsyncMock(
                    return_value=SimpleNamespace(
                        proxy_location=None,
                        max_screenshot_scrolls=None,
                        browser_address=None,
                        extra_http_headers=None,
                        cdp_connect_headers=None,
                        failure_reason=None,
                    )
                ),
                update_workflow_run=AsyncMock(),
            ),
            observer=SimpleNamespace(update_task_v2=AsyncMock(), update_workflow_run_block=AsyncMock()),
        ),
        WORKFLOW_CONTEXT_MANAGER=SimpleNamespace(
            has_workflow_run_context=lambda run_id: run_id == "wr_child",
            get_workflow_run_context=lambda _run_id: child_context,
        ),
    )
    monkeypatch.setattr(block_module, "app", fake_app)

    from skyvern.services import task_v2_service

    monkeypatch.setattr(
        task_v2_service,
        "initialize_task_v2",
        AsyncMock(return_value=SimpleNamespace(observer_cruise_id="tsk_v2_1", workflow_run_id="wr_child")),
    )

    async def exploding_run_task_v2(**_: object) -> SimpleNamespace:
        raise RuntimeError("child finalization failed")

    monkeypatch.setattr(task_v2_service, "run_task_v2", exploding_run_task_v2)
    monkeypatch.setattr(
        TaskV2Block,
        "get_workflow_run_context",
        lambda self, workflow_run_id: SimpleNamespace(credential_totp_identifiers={}),
    )
    monkeypatch.setattr(TaskV2Block, "format_potential_template_parameters", lambda self, _: None)

    block = TaskV2Block(
        label="task1",
        output_parameter=_output_parameter("task1_output"),
        prompt="do the thing",
        url="https://example.com",
    )

    with pytest.raises(RuntimeError, match="child finalization failed"):
        await block.execute(
            workflow_run_id="wr_parent",
            workflow_run_block_id="wrb_1",
            organization_id="org_1",
        )

    assert settled == ["wr_child"]


def _child_browser_state(*, lost: bool) -> RealBrowserState:
    state, context = run_bound_browser_state("wr_child")
    if lost:
        state._on_browser_context_closed(context)
    return state


@pytest.mark.asyncio
@pytest.mark.parametrize("mapping", ["persistent_session_child_only", "shared_with_parent"])
@pytest.mark.parametrize("continue_on_failure", [True, False])
@pytest.mark.parametrize("child_lost_browser", [True, False], ids=["child_lost_browser", "ordinary_child_failure"])
async def test_taskv2_block_reports_a_child_browser_loss_from_the_child_run_entry(
    monkeypatch: pytest.MonkeyPatch, mapping: str, continue_on_failure: bool, child_lost_browser: bool
) -> None:
    skyvern_context.set(SkyvernContext(organization_id="org_1", workflow_run_id="wr_parent", run_id="wr_parent"))
    child_state = _child_browser_state(lost=child_lost_browser)
    browser_manager = RealBrowserManager()
    # An explicit persistent session registers only the child run; a shared browser is also aliased to the parent.
    browser_manager.pages = (
        {"wr_child": child_state}
        if mapping == "persistent_session_child_only"
        else {"wr_child": child_state, "wr_parent": child_state}
    )
    workflow_run = SimpleNamespace(
        proxy_location=None,
        max_screenshot_scrolls=None,
        browser_address=None,
        extra_http_headers=None,
        cdp_connect_headers=None,
        failure_reason="Browser state page is missing.",
    )
    update_block = AsyncMock()
    fake_app = SimpleNamespace(
        DATABASE=SimpleNamespace(
            organizations=SimpleNamespace(get_organization=AsyncMock(return_value=SimpleNamespace())),
            workflow_runs=SimpleNamespace(
                get_workflow_run=AsyncMock(return_value=workflow_run), update_workflow_run=AsyncMock()
            ),
            observer=SimpleNamespace(update_task_v2=AsyncMock(), update_workflow_run_block=update_block),
        ),
        BROWSER_MANAGER=browser_manager,
        WORKFLOW_SERVICE=SimpleNamespace(
            get_recent_task_screenshot_artifacts=AsyncMock(return_value=[]),
            get_recent_workflow_screenshot_artifacts=AsyncMock(return_value=[]),
        ),
        STORAGE=SimpleNamespace(
            get_current_attempt_downloaded_files=AsyncMock(return_value=[]),
            get_downloaded_file_signature_aliases=lambda _: [],
        ),
        WORKFLOW_CONTEXT_MANAGER=SimpleNamespace(has_workflow_run_context=lambda _run_id: False),
    )
    monkeypatch.setattr(block_module, "app", fake_app)

    from skyvern.services import task_v2_service

    child = SimpleNamespace(
        observer_cruise_id="tsk_v2_1",
        workflow_run_id="wr_child",
        output=None,
        status=TaskV2Status.failed,
        summary=None,
        failure_category=None,
    )
    monkeypatch.setattr(task_v2_service, "initialize_task_v2", AsyncMock(return_value=child))
    monkeypatch.setattr(task_v2_service, "run_task_v2", AsyncMock(return_value=child))
    monkeypatch.setattr(
        TaskV2Block,
        "get_workflow_run_context",
        lambda self, workflow_run_id: SimpleNamespace(credential_totp_identifiers={}),
    )
    monkeypatch.setattr(TaskV2Block, "format_potential_template_parameters", lambda self, _: None)
    monkeypatch.setattr(TaskV2Block, "record_output_parameter_value", AsyncMock())

    block = TaskV2Block(
        label="task1",
        output_parameter=_output_parameter("task1_output"),
        prompt="do the thing",
        url="https://example.com",
        continue_on_failure=continue_on_failure,
    )

    result = await block.execute(workflow_run_id="wr_parent", workflow_run_block_id="wrb_1", organization_id="org_1")

    persisted_codes = {
        code
        for call in update_block.await_args_list
        if call.kwargs.get("workflow_run_block_id") == "wrb_1"
        for code in call.kwargs.get("error_codes") or []
    }
    retry_policy = WorkflowRetryPolicy.model_validate({"max_retries": 2, "retry_on": [{"status": "failed"}]})
    retry = evaluate_retry_policy(retry_policy, WorkflowRunStatus.failed, persisted_codes, 0)
    if child_lost_browser:
        assert result.success is False
        assert result.can_continue_after_failure is False
        assert result.error_codes == [BROWSER_CONNECTION_LOST_ERROR_CODE]
        assert persisted_codes == {BROWSER_CONNECTION_LOST_ERROR_CODE}
        assert retry == (False, BROWSER_CONNECTION_LOST_ERROR_CODE)
    else:
        assert result.success is continue_on_failure
        assert result.can_continue_after_failure is True
        assert result.error_codes == []
        assert persisted_codes == set()
        assert retry == (True, "matched")
