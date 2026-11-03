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
                update_workflow_run_if_not_final=AsyncMock(),
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
    monkeypatch.setattr(task_v2_service, "settle_child_of_ended_parent", AsyncMock(return_value=None))

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
                update_workflow_run_if_not_final=AsyncMock(),
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
    monkeypatch.setattr(task_v2_service, "settle_child_of_ended_parent", AsyncMock(return_value=None))
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
                get_workflow_run=AsyncMock(return_value=workflow_run),
                update_workflow_run=AsyncMock(),
                update_workflow_run_if_not_final=AsyncMock(),
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
    monkeypatch.setattr(task_v2_service, "settle_child_of_ended_parent", AsyncMock(return_value=None))
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


class _ChildBodyStarted(BaseException):
    """Raised where the child's browser would be acquired; a BaseException so no handler on the way absorbs it."""


def _nested_rows(
    monkeypatch: pytest.MonkeyPatch, *, on_child_created=None, on_child_running=None, on_session_begun=None
) -> SimpleNamespace:
    """Parent run plus the rows a nested task v2 creates, with conditional writes that honor their control kwargs."""
    from datetime import UTC, datetime

    from skyvern.services import task_v2_service

    rows = SimpleNamespace(runs={}, tasks={}, browser_requests=[], started=[], sessions_begun=[])
    rows.runs["wr_parent"] = SimpleNamespace(
        workflow_run_id="wr_parent",
        status=WorkflowRunStatus.running,
        parent_workflow_run_id=None,
        proxy_location=None,
        max_screenshot_scrolls=None,
        browser_address=None,
        extra_http_headers=None,
        cdp_connect_headers=None,
        failure_reason=None,
    )
    organization = SimpleNamespace(
        organization_id="org_1",
        organization_name="Org 1",
        default_llm_key=None,
        default_secondary_llm_key=None,
        created_at=datetime.now(UTC),
    )

    async def get_run(workflow_run_id: str, organization_id: str | None = None, **_: object) -> object:
        return rows.runs.get(workflow_run_id)

    async def update_run(workflow_run_id: str, status: WorkflowRunStatus, **_: object) -> object:
        rows.runs[workflow_run_id].status = status
        return rows.runs[workflow_run_id]

    async def update_run_if_not_final(
        workflow_run_id: str, status: WorkflowRunStatus, only_from: list | None = None, **_: object
    ) -> object:
        run = rows.runs[workflow_run_id]
        if run.status.is_final() or (only_from is not None and run.status not in only_from):
            return None
        run.status = status
        return run

    async def cancel_through_service(workflow_run_id: str, **_: object) -> object:
        # Only the workflow service's terminal path runs the run's terminal bookkeeping.
        run = await update_run_if_not_final(workflow_run_id, WorkflowRunStatus.canceled)
        if run is not None:
            run.terminal_bookkeeping_done = True
        return run

    async def mark_running(workflow_run_id: str, **_: object) -> object:
        rows.started.append(workflow_run_id)
        if on_child_running is not None:
            on_child_running(rows)
        return await update_run_if_not_final(workflow_run_id, WorkflowRunStatus.running) or rows.runs[workflow_run_id]

    async def update_task(
        task_v2_id: str, status: TaskV2Status | None = None, keep_final_status: bool = False, **_: object
    ) -> object:
        task = rows.tasks[task_v2_id]
        if status is not None and not (keep_final_status and task.status.is_final()):
            task.status = status
        return task

    async def update_task_if_not_final(task_v2_id: str, status: TaskV2Status, **_: object) -> bool:
        task = rows.tasks[task_v2_id]
        if task.status.is_final():
            return False
        task.status = status
        return True

    async def get_task(task_v2_id: str, **_: object) -> object:
        return rows.tasks.get(task_v2_id)

    async def initialize_child(parent_workflow_run_id: str, **_: object) -> object:
        rows.runs["wr_child"] = SimpleNamespace(
            workflow_run_id="wr_child",
            workflow_id="wf_child",
            status=WorkflowRunStatus.created,
            parent_workflow_run_id=parent_workflow_run_id,
            browser_profile_id=None,
            browser_address=None,
            failure_reason=None,
            terminal_bookkeeping_done=False,
        )
        rows.tasks["tsk_child"] = SimpleNamespace(
            observer_cruise_id="tsk_child",
            workflow_run_id="wr_child",
            workflow_id="wf_child",
            organization_id="org_1",
            status=TaskV2Status.created,
            prompt="do the thing",
            url="https://example.com",
            model=None,
            webhook_callback_url=None,
            max_screenshot_scrolls=None,
            output=None,
            summary=None,
            failure_category=None,
        )
        if on_child_created is not None:
            on_child_created(rows)
        return rows.tasks["tsk_child"]

    async def begin_session(browser_session_id: str, **_: object) -> str:
        rows.sessions_begun.append(browser_session_id)
        if on_session_begun is not None:
            on_session_begun(rows)
        return "gen_1"

    async def acquire_child_browser(**kwargs: object) -> object:
        rows.browser_requests.append(kwargs.get("workflow_run"))
        raise _ChildBodyStarted

    async def no_rows(*_: object, **__: object) -> list:
        return []

    workflow = SimpleNamespace(
        workflow_id="wf_child", workflow_permanent_id="wpid_child", title="child", workflow_definition=None
    )
    fake_app = SimpleNamespace(
        DATABASE=SimpleNamespace(
            organizations=SimpleNamespace(get_organization=AsyncMock(return_value=organization)),
            workflow_runs=SimpleNamespace(
                get_workflow_run=get_run,
                update_workflow_run=update_run,
                update_workflow_run_if_not_final=update_run_if_not_final,
            ),
            observer=SimpleNamespace(
                get_task_v2=get_task,
                update_task_v2=update_task,
                update_task_v2_status_if_not_final=update_task_if_not_final,
                update_workflow_run_block=AsyncMock(),
            ),
        ),
        WORKFLOW_SERVICE=SimpleNamespace(
            get_workflow_run=get_run,
            get_workflow=AsyncMock(return_value=workflow),
            mark_workflow_run_as_running=mark_running,
            mark_workflow_run_as_canceled_if_not_final=cancel_through_service,
            get_workflow_run_parameter_tuples=no_rows,
            get_workflow_output_parameters=no_rows,
            get_recent_task_screenshot_artifacts=no_rows,
            get_recent_workflow_screenshot_artifacts=no_rows,
        ),
        WORKFLOW_CONTEXT_MANAGER=SimpleNamespace(
            initialize_workflow_run_context=AsyncMock(),
            remove_workflow_run_context=lambda _run_id: None,
            has_workflow_run_context=lambda _run_id: False,
        ),
        BROWSER_MANAGER=SimpleNamespace(
            get_or_create_for_workflow_run=acquire_child_browser, get_for_workflow_run=lambda *_: None
        ),
        PERSISTENT_SESSIONS_MANAGER=SimpleNamespace(begin_session=begin_session),
        STORAGE=SimpleNamespace(
            get_current_attempt_downloaded_files=no_rows,
            get_downloaded_file_signature_aliases=lambda _: [],
        ),
    )
    monkeypatch.setattr(block_module, "app", fake_app)
    monkeypatch.setattr(task_v2_service, "app", fake_app)
    monkeypatch.setattr(task_v2_service, "initialize_task_v2", initialize_child)
    monkeypatch.setattr(task_v2_service, "_validate_task_v2_model_for_org", AsyncMock())
    monkeypatch.setattr(task_v2_service, "workflow_definition_sha256", lambda _definition: "sha")
    monkeypatch.setattr(block_module, "capture_block_download_baseline", AsyncMock())
    monkeypatch.setattr(TaskV2Block, "_settle_child_capture", AsyncMock())
    monkeypatch.setattr(
        TaskV2Block,
        "get_workflow_run_context",
        lambda self, workflow_run_id: SimpleNamespace(credential_totp_identifiers={}),
    )
    monkeypatch.setattr(TaskV2Block, "format_potential_template_parameters", lambda self, _: None)

    async def keep_output(self: TaskV2Block, *_: object, **__: object) -> None:
        return None

    async def block_result(self: TaskV2Block, **kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(**kwargs)

    monkeypatch.setattr(TaskV2Block, "record_output_parameter_value", keep_output)
    monkeypatch.setattr(TaskV2Block, "build_block_result", block_result)
    skyvern_context.set(
        SkyvernContext(organization_id="org_1", workflow_run_id="wr_parent", run_id="wr_parent", org_age=1)
    )
    return rows


async def _execute_nested_block(browser_session_id: str | None = None) -> SimpleNamespace:
    block = TaskV2Block(
        label="nested", output_parameter=_output_parameter("nested_output"), prompt="do the thing", url="https://e.com"
    )
    return await block.execute(
        workflow_run_id="wr_parent",
        workflow_run_block_id="wrb_1",
        organization_id="org_1",
        browser_session_id=browser_session_id,
    )


def _reap_parent(rows: SimpleNamespace) -> None:
    rows.runs["wr_parent"].status = WorkflowRunStatus.failed


@pytest.mark.asyncio
async def test_child_of_a_parent_reaped_during_child_initialization_never_starts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = _nested_rows(monkeypatch, on_child_created=_reap_parent)

    result = await _execute_nested_block()

    assert rows.started == []
    assert rows.browser_requests == []
    assert rows.tasks["tsk_child"].status == TaskV2Status.canceled
    assert rows.runs["wr_child"].status == WorkflowRunStatus.canceled
    assert rows.runs["wr_child"].terminal_bookkeeping_done is True
    assert result.success is False


@pytest.mark.asyncio
async def test_child_of_a_parent_reaped_during_child_startup_never_reaches_its_browser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = _nested_rows(monkeypatch, on_child_running=_reap_parent)

    result = await _execute_nested_block()

    assert rows.browser_requests == []
    assert rows.tasks["tsk_child"].status == TaskV2Status.canceled
    assert rows.runs["wr_child"].status == WorkflowRunStatus.canceled
    assert rows.runs["wr_child"].terminal_bookkeeping_done is True
    assert result.success is False


@pytest.mark.asyncio
async def test_child_on_a_shared_session_whose_parent_ended_while_the_session_began_never_reaches_its_browser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = _nested_rows(monkeypatch, on_session_begun=_reap_parent)

    result = await _execute_nested_block(browser_session_id="pbs_shared")

    assert rows.sessions_begun == ["pbs_shared"]
    assert rows.browser_requests == []
    assert rows.tasks["tsk_child"].status == TaskV2Status.canceled
    assert rows.runs["wr_child"].status == WorkflowRunStatus.canceled
    assert result.success is False


@pytest.mark.asyncio
async def test_child_of_a_parent_that_lost_its_run_lease_never_starts(monkeypatch: pytest.MonkeyPatch) -> None:
    from skyvern.services import task_v2_service

    lost: set[str] = set()
    rows = _nested_rows(monkeypatch, on_child_created=lambda _rows: lost.add("wr_parent"))

    async def lease_lost(run_id: str) -> bool:
        return run_id in lost

    monkeypatch.setattr(task_v2_service, "run_lease_lost", lease_lost)

    result = await _execute_nested_block()

    assert rows.started == []
    assert rows.browser_requests == []
    assert rows.runs["wr_parent"].status == WorkflowRunStatus.running
    assert rows.tasks["tsk_child"].status == TaskV2Status.canceled
    assert rows.runs["wr_child"].status == WorkflowRunStatus.canceled
    assert result.success is False


@pytest.mark.asyncio
async def test_child_finished_elsewhere_before_queueing_is_not_queued_again(monkeypatch: pytest.MonkeyPatch) -> None:
    def finish_child(rows: SimpleNamespace) -> None:
        rows.tasks["tsk_child"].status = TaskV2Status.completed
        rows.runs["wr_child"].status = WorkflowRunStatus.completed

    rows = _nested_rows(monkeypatch, on_child_created=finish_child)

    await _execute_nested_block()

    assert rows.browser_requests == []
    assert rows.tasks["tsk_child"].status == TaskV2Status.completed
    assert rows.runs["wr_child"].status == WorkflowRunStatus.completed


@pytest.mark.asyncio
async def test_child_of_a_live_parent_reaches_its_browser(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = _nested_rows(monkeypatch)

    with pytest.raises(_ChildBodyStarted):
        await _execute_nested_block()

    assert [run.workflow_run_id for run in rows.browser_requests] == ["wr_child"]
    assert rows.runs["wr_parent"].status == WorkflowRunStatus.running
