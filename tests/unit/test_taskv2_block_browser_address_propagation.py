"""TaskV2Block.execute must forward the parent run's remote-browser connection
fields into the child TaskV2 it spawns. Without this, a workflow pinned to a
cloud browser (CDP browser_address + auth headers) silently launches a fresh
local Chrome for the TaskV2 child instead of reusing the cloud session."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.schemas.task_v2 import TaskV2Status
from skyvern.forge.sdk.workflow.models import block as block_module
from skyvern.forge.sdk.workflow.models.block import TaskV2Block
from skyvern.forge.sdk.workflow.models.parameter import OutputParameter, ParameterType


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
    organization = SimpleNamespace(organization_id="org_1", organization_name="Org 1")

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
        STORAGE=SimpleNamespace(get_downloaded_files=AsyncMock(side_effect=[[], []])),
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
