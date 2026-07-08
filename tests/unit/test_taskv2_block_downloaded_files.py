"""Tests for TaskV2Block downloaded_files output with loop-scoped filtering (SKY-7005)."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.schemas.files import FileInfo
from skyvern.forge.sdk.schemas.task_v2 import TaskV2Status
from skyvern.forge.sdk.workflow.loop_download_filter import filter_downloaded_files_for_current_iteration
from skyvern.forge.sdk.workflow.models import block_base as block_base_module
from skyvern.forge.sdk.workflow.models import misc_blocks as block_module
from skyvern.forge.sdk.workflow.models.block import TaskV2Block
from skyvern.forge.sdk.workflow.models.parameter import OutputParameter, ParameterType


def _file(url: str, filename: str, checksum: str) -> FileInfo:
    return FileInfo(url=url, filename=filename, checksum=checksum)


def _output_parameter(key: str) -> OutputParameter:
    now = datetime.now(UTC)
    return OutputParameter(
        parameter_type=ParameterType.OUTPUT,
        key=key,
        output_parameter_id=f"op_{key}",
        workflow_id="wf_test",
        created_at=now,
        modified_at=now,
    )


def test_taskv2_context_loop_state_available_after_nested_task_execution() -> None:
    """Verify loop_internal_state survives nested task execution and remains available for filtering."""
    loop_state = {
        "downloaded_file_signatures_before_iteration": [
            ["a.pdf", "abc", "https://files/a.pdf"],
        ],
    }

    parent_context = SkyvernContext(
        organization_id="org_1",
        workflow_run_id="wr_1",
        run_id="wr_1",
        loop_internal_state=loop_state,
    )
    skyvern_context.set(parent_context)

    with skyvern_context.scoped(
        SkyvernContext(
            organization_id="org_1",
            workflow_run_id="wr_child",
            workflow_permanent_id="wfp_child",
            task_v2_id="tsk_v2_child",
            run_id="wr_1",
        )
    ):
        pass

    current_context = skyvern_context.current()
    assert current_context is not None
    assert current_context is parent_context
    assert current_context.loop_internal_state == loop_state

    all_files = [
        _file("https://files/a.pdf?sig=old", "a.pdf", "abc"),
        _file("https://files/b.pdf?sig=new", "b.pdf", "def"),
    ]
    filtered = filter_downloaded_files_for_current_iteration(
        all_files,
        current_context.loop_internal_state,
    )
    assert [f.filename for f in filtered] == ["b.pdf"]

    skyvern_context.reset()


@pytest.fixture(autouse=True)
def reset_context() -> None:
    skyvern_context.reset()
    yield
    skyvern_context.reset()


@pytest.mark.asyncio
async def test_taskv2_block_uses_pre_run_loop_state_for_download_filtering(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent_context = SkyvernContext(
        organization_id="org_1",
        workflow_run_id="wr_parent",
        run_id="wr_parent",
        loop_internal_state={
            "downloaded_file_signatures_before_iteration": [
                ("a.zip", "abc", "file:///app/downloads/wr_parent/a.zip"),
            ],
        },
    )
    skyvern_context.set(parent_context)

    outer_workflow_run = SimpleNamespace(
        proxy_location=None,
        max_screenshot_scrolls=None,
        browser_address=None,
        extra_http_headers=None,
        cdp_connect_headers=None,
    )
    child_workflow_run = SimpleNamespace(failure_reason=None)
    organization = SimpleNamespace(organization_id="org_1", organization_name="Org 1")
    a_zip = _file("file:///app/downloads/wr_parent/a.zip", "a.zip", "abc")
    b_zip = _file("file:///app/downloads/wr_parent/b.zip", "b.zip", "def")

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
        # First read is the block's own baseline capture (only a.zip exists yet); the
        # second is the post-run read (a.zip from before + b.zip downloaded this block).
        STORAGE=SimpleNamespace(get_downloaded_files=AsyncMock(side_effect=[[a_zip], [a_zip, b_zip]])),
    )
    monkeypatch.setattr(block_module, "app", fake_app)
    monkeypatch.setattr(block_base_module, "app", fake_app)

    from skyvern.services import task_v2_service

    initialized_task_v2 = SimpleNamespace(observer_cruise_id="tsk_v2_1", workflow_run_id="wr_child")
    completed_task_v2 = SimpleNamespace(
        observer_cruise_id="tsk_v2_1",
        workflow_run_id="wr_child",
        output={"result": "ok"},
        status=TaskV2Status.completed,
        summary="done",
        failure_category=None,
    )
    monkeypatch.setattr(task_v2_service, "initialize_task_v2", AsyncMock(return_value=initialized_task_v2))

    async def fake_run_task_v2(**_: object) -> SimpleNamespace:
        # Simulate nested task execution restoring a parent context that lost loop state.
        skyvern_context.set(
            SkyvernContext(
                organization_id="org_1",
                workflow_run_id="wr_parent",
                run_id="wr_parent",
                loop_internal_state=None,
            )
        )
        return completed_task_v2

    monkeypatch.setattr(task_v2_service, "run_task_v2", fake_run_task_v2)

    recorded_outputs: list[dict[str, object]] = []
    block = TaskV2Block(
        label="download_page",
        output_parameter=_output_parameter("download_page_output"),
        prompt="download the file",
        url="https://example.com",
    )
    monkeypatch.setattr(
        TaskV2Block,
        "get_workflow_run_context",
        lambda self, workflow_run_id: SimpleNamespace(credential_totp_identifiers={}),
    )
    monkeypatch.setattr(TaskV2Block, "format_potential_template_parameters", lambda self, _: None)

    async def fake_record_output_parameter_value(
        self: TaskV2Block,
        workflow_run_context: object,
        workflow_run_id: str,
        value: dict[str, object],
    ) -> None:
        recorded_outputs.append(value)

    async def fake_build_block_result(self: TaskV2Block, **kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(**kwargs)

    monkeypatch.setattr(TaskV2Block, "record_output_parameter_value", fake_record_output_parameter_value)
    monkeypatch.setattr(TaskV2Block, "build_block_result", fake_build_block_result)

    await block.execute(
        workflow_run_id="wr_parent",
        workflow_run_block_id="wrb_1",
        organization_id="org_1",
    )

    assert len(recorded_outputs) == 1
    assert [file_info["filename"] for file_info in recorded_outputs[0]["downloaded_files"]] == ["b.zip"]
    assert recorded_outputs[0]["downloaded_file_urls"] == ["file:///app/downloads/wr_parent/b.zip"]
