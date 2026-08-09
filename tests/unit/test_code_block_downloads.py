"""CodeBlock registering browser downloads into its block output (SKY-10937).

Covers the three acceptance behaviors:
1. A download-producing code block surfaces ``downloaded_files`` / ``downloaded_file_urls`` /
   ``downloaded_file_artifact_ids`` in its output so a downstream block can chain from it in the
   same run; a non-download code block's output is unchanged.
2. Inside a loop iteration, only the file produced this iteration attributes to the block.
3. A ``FILE_URL`` workflow parameter is materialized to a run-scoped local path usable by
   ``set_input_files``; an empty URI leaves the value untouched.
"""

import asyncio
import os
from collections.abc import Callable
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from structlog.testing import capture_logs

from skyvern.exceptions import (
    DownloadSaveIncompleteError,
    IllegitCompleteScriptTermination,
    ScriptTerminationException,
)
from skyvern.forge.sdk.copilot.reached_download_target import (
    block_output_has_registered_download,
    code_is_download_intent,
)
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.db.utils import downloaded_file_count_from_output
from skyvern.forge.sdk.schemas.files import FileInfo
from skyvern.forge.sdk.workflow.models import block as block_module
from skyvern.forge.sdk.workflow.models.block import CodeBlock
from skyvern.forge.sdk.workflow.models.parameter import (
    OutputParameter,
    ParameterType,
    WorkflowParameter,
    WorkflowParameterType,
)
from skyvern.schemas.workflows import BlockResult, BlockStatus
from skyvern.webeye.browser_artifacts import BrowserArtifacts


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


def _file_url_parameter(key: str) -> WorkflowParameter:
    now = datetime.now(UTC)
    return WorkflowParameter(
        key=key,
        workflow_parameter_id=f"wp_{key}",
        workflow_parameter_type=WorkflowParameterType.FILE_URL,
        workflow_id="wf_test",
        created_at=now,
        modified_at=now,
    )


@pytest.fixture(autouse=True)
def _reset_context() -> None:
    skyvern_context.reset()
    yield
    skyvern_context.reset()


@pytest.fixture
def _isolated_download_path(tmp_path, monkeypatch: pytest.MonkeyPatch) -> str:
    download_root = tmp_path / "downloads"
    download_root.mkdir()
    monkeypatch.setattr(
        "skyvern.forge.sdk.api.files.settings.DOWNLOAD_PATH",
        str(download_root),
    )
    return str(download_root)


def _copilot_workflow() -> SimpleNamespace:
    return SimpleNamespace(
        created_by="copilot",
        edited_by=None,
        workflow_permanent_id="wpid_test",
        organization_id="o_1",
    )


def _wire_block_runtime(
    monkeypatch: pytest.MonkeyPatch,
    *,
    values: dict[str, object] | None = None,
    workflow: SimpleNamespace | None = None,
) -> None:
    page = SimpleNamespace()
    browser_state = SimpleNamespace(get_working_page=AsyncMock(return_value=page), browser_artifacts=BrowserArtifacts())
    monkeypatch.setattr(CodeBlock, "get_or_create_browser_state", AsyncMock(return_value=browser_state))

    context = SimpleNamespace(
        organization_id="o_1",
        workflow=workflow,
        workflow_permanent_id="wpid_test",
        workflow_id="w_test",
        get_value=lambda key: (values or {}).get(key),
        mask_secrets_in_data=lambda data, mask="*****": data,
    )
    monkeypatch.setattr(CodeBlock, "get_workflow_run_context", lambda self, workflow_run_id: context)
    monkeypatch.setattr(CodeBlock, "format_potential_template_parameters", lambda self, workflow_run_context: None)
    monkeypatch.setattr(CodeBlock, "record_output_parameter_value", AsyncMock())


def _wire_secure_runner(
    monkeypatch: pytest.MonkeyPatch, *, output: dict, on_execute: Callable[[], None] | None = None
) -> None:
    """Route execute() down the secure sidecar arm, whose returned payload is what the host binds."""
    block_result = BlockResult(
        success=True,
        output_parameter=_output_parameter("code_out"),
        output_parameter_value=output,
        status=BlockStatus.completed,
        workflow_run_block_id="",
    )

    async def _execute_override(**kwargs: object) -> SimpleNamespace:
        if on_execute is not None:
            on_execute()
        return SimpleNamespace(block_result=block_result, failure=None)

    fake_app = block_module.app
    fake_app.AGENT_FUNCTION.should_use_codeblock_runner = AsyncMock(return_value=True)
    fake_app.AGENT_FUNCTION.execute_code_block_override = AsyncMock(side_effect=_execute_override)


def _write_run_download(
    download_root: str, filename: str = "invoice.pdf", content: bytes = b"%PDF-1.4 statement"
) -> None:
    run_dir = os.path.join(download_root, "wr_1")
    os.makedirs(run_dir, exist_ok=True)
    with open(os.path.join(run_dir, filename), "wb") as handle:
        handle.write(content)


def _persisted_output() -> object:
    return CodeBlock.record_output_parameter_value.await_args.args[2]


def _fake_storage_app(monkeypatch: pytest.MonkeyPatch, *, save, get) -> None:
    fake_app = SimpleNamespace(
        STORAGE=SimpleNamespace(save_downloaded_files=save, get_downloaded_files=get),
        AGENT_FUNCTION=SimpleNamespace(
            validate_code_block=AsyncMock(),
            # Secure CodeBlock runner gating — match the OSS base no-op so execute() runs legacy.
            should_use_codeblock_runner=AsyncMock(return_value=False),
            execute_code_block_override=AsyncMock(return_value=None),
        ),
    )
    monkeypatch.setattr(block_module, "app", fake_app)


@pytest.mark.asyncio
async def test_code_block_registers_downloads_into_output(
    monkeypatch: pytest.MonkeyPatch, _isolated_download_path: str
) -> None:
    skyvern_context.set(SkyvernContext(organization_id="o_1", workflow_run_id="wr_1", run_id="wr_1"))

    file_info = FileInfo(
        url="https://api.example.com/v1/artifacts/a_dl_1/content?artifact_name=invoice.pdf",
        filename="invoice.pdf",
        checksum="deadbeef",
        artifact_id="a_dl_1",
        modified_at=datetime(2026, 6, 14, 12, 0, tzinfo=UTC),
    )
    # Baseline read at block start is empty; post-run read sees the new file.
    _fake_storage_app(
        monkeypatch,
        save=AsyncMock(),
        get=AsyncMock(side_effect=[[], [file_info]]),
    )
    _wire_block_runtime(monkeypatch)

    block = CodeBlock(
        label="code_download",
        code="saved = 'ok'",
        output_parameter=_output_parameter("code_out"),
    )
    result = await block.execute(workflow_run_id="wr_1", workflow_run_block_id="", organization_id="o_1")

    assert result.success is True
    output = result.output_parameter_value
    assert output["saved"] == "ok"
    assert output["downloaded_files"] == [file_info.model_dump()]
    assert output["downloaded_file_urls"] == [file_info.url]
    assert output["downloaded_file_artifact_ids"] == ["a_dl_1"]


@pytest.mark.asyncio
async def test_code_block_wraps_non_dict_output_before_attaching_downloads(
    monkeypatch: pytest.MonkeyPatch, _isolated_download_path: str
) -> None:
    skyvern_context.set(SkyvernContext(organization_id="o_1", workflow_run_id="wr_1", run_id="wr_1"))

    file_info = FileInfo(
        url="https://api.example.com/v1/artifacts/a_dl_1/content?artifact_name=invoice.pdf",
        filename="invoice.pdf",
        checksum="deadbeef",
        artifact_id="a_dl_1",
        modified_at=datetime(2026, 6, 14, 12, 0, tzinfo=UTC),
    )
    _fake_storage_app(
        monkeypatch,
        save=AsyncMock(),
        get=AsyncMock(side_effect=[[], [file_info]]),
    )
    _wire_block_runtime(monkeypatch)

    block = CodeBlock(
        label="code_download",
        code="return 'ok'",
        output_parameter=_output_parameter("code_out"),
    )
    result = await block.execute(workflow_run_id="wr_1", workflow_run_block_id="", organization_id="o_1")

    assert result.success is True
    output = result.output_parameter_value
    assert output["value"] == "ok"
    assert output["downloaded_files"] == [file_info.model_dump()]
    assert output["downloaded_file_urls"] == [file_info.url]
    assert output["downloaded_file_artifact_ids"] == ["a_dl_1"]


@pytest.mark.asyncio
async def test_code_block_without_downloads_has_no_download_keys(
    monkeypatch: pytest.MonkeyPatch, _isolated_download_path: str
) -> None:
    skyvern_context.set(SkyvernContext(organization_id="o_1", workflow_run_id="wr_1", run_id="wr_1"))

    _fake_storage_app(monkeypatch, save=AsyncMock(), get=AsyncMock(return_value=[]))
    _wire_block_runtime(monkeypatch)

    block = CodeBlock(
        label="code_no_download",
        code="value = 'ok'",
        output_parameter=_output_parameter("code_out"),
    )
    result = await block.execute(workflow_run_id="wr_1", workflow_run_block_id="", organization_id="o_1")

    assert result.success is True
    assert result.output_parameter_value == {"value": "ok"}


@pytest.mark.asyncio
async def test_code_block_scopes_downloads_to_current_loop_iteration(
    monkeypatch: pytest.MonkeyPatch, _isolated_download_path: str
) -> None:
    prev_file = FileInfo(
        url="https://api.example.com/v1/artifacts/a_prev/content?artifact_name=prev.pdf",
        filename="prev.pdf",
        checksum="abc",
        artifact_id="a_prev",
    )
    new_file = FileInfo(
        url="https://api.example.com/v1/artifacts/a_new/content?artifact_name=new.pdf",
        filename="new.pdf",
        checksum="def",
        artifact_id="a_new",
    )
    skyvern_context.set(
        SkyvernContext(
            organization_id="o_1",
            workflow_run_id="wr_1",
            run_id="wr_1",
            loop_internal_state={
                "downloaded_file_signatures_before_iteration": [
                    block_module.to_downloaded_file_signature(prev_file),
                ],
            },
        )
    )

    # Baseline read at block start sees the earlier iteration's file; post-run read sees both.
    _fake_storage_app(
        monkeypatch,
        save=AsyncMock(),
        get=AsyncMock(side_effect=[[prev_file], [prev_file, new_file]]),
    )
    _wire_block_runtime(monkeypatch)

    block = CodeBlock(
        label="code_download",
        code="saved = 'ok'",
        output_parameter=_output_parameter("code_out"),
    )
    result = await block.execute(workflow_run_id="wr_1", workflow_run_block_id="", organization_id="o_1")

    output = result.output_parameter_value
    assert [fi["filename"] for fi in output["downloaded_files"]] == ["new.pdf"]
    assert output["downloaded_file_urls"] == [new_file.url]
    assert output["downloaded_file_artifact_ids"] == ["a_new"]


@pytest.mark.asyncio
async def test_code_block_tolerates_save_failure(monkeypatch: pytest.MonkeyPatch, _isolated_download_path: str) -> None:
    skyvern_context.set(SkyvernContext(organization_id="o_1", workflow_run_id="wr_1", run_id="wr_1"))

    get_mock = AsyncMock(return_value=[])
    _fake_storage_app(
        monkeypatch,
        save=AsyncMock(side_effect=RuntimeError("S3 down")),
        get=get_mock,
    )
    _wire_block_runtime(monkeypatch)

    block = CodeBlock(
        label="code_download",
        code="value = 'ok'",
        output_parameter=_output_parameter("code_out"),
    )
    result = await block.execute(workflow_run_id="wr_1", workflow_run_block_id="", organization_id="o_1")

    assert result.success is True
    # Only the baseline read ran; the post-run fetch is skipped because save failed first.
    assert get_mock.await_count == 1
    assert result.output_parameter_value == {"value": "ok"}


@pytest.mark.asyncio
async def test_code_block_tolerates_get_failure(monkeypatch: pytest.MonkeyPatch, _isolated_download_path: str) -> None:
    skyvern_context.set(SkyvernContext(organization_id="o_1", workflow_run_id="wr_1", run_id="wr_1"))

    _fake_storage_app(
        monkeypatch,
        save=AsyncMock(),
        get=AsyncMock(side_effect=[[], RuntimeError("S3 down")]),
    )
    _wire_block_runtime(monkeypatch)

    block = CodeBlock(
        label="code_download",
        code="value = 'ok'",
        output_parameter=_output_parameter("code_out"),
    )
    result = await block.execute(workflow_run_id="wr_1", workflow_run_block_id="", organization_id="o_1")

    assert result.success is True
    assert result.output_parameter_value == {"value": "ok"}
    assert "downloaded_files" not in result.output_parameter_value


@pytest.mark.asyncio
async def test_code_block_materializes_file_parameter_to_local_path(
    monkeypatch: pytest.MonkeyPatch, _isolated_download_path: str
) -> None:
    run_dir = os.path.join(_isolated_download_path, "wr_1")
    os.makedirs(run_dir, exist_ok=True)
    local_path = os.path.join(run_dir, "upload.pdf")
    with open(local_path, "wb") as f:
        f.write(b"%PDF-1.4 upload")

    download_mock = AsyncMock(return_value=local_path)
    monkeypatch.setattr(block_module, "download_file", download_mock)

    _fake_storage_app(monkeypatch, save=AsyncMock(), get=AsyncMock(return_value=[]))
    _wire_block_runtime(monkeypatch, values={"resume": "https://files.example.com/resume.pdf"})

    block = CodeBlock(
        label="code_upload",
        code="resolved = resume",
        output_parameter=_output_parameter("code_out"),
        parameters=[_file_url_parameter("resume")],
    )
    result = await block.execute(workflow_run_id="wr_1", workflow_run_block_id="", organization_id="o_1")

    assert result.success is True
    resolved = result.output_parameter_value["resolved"]
    assert resolved == os.path.realpath(local_path)
    assert resolved.startswith(os.path.realpath(run_dir) + os.sep)
    download_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_code_block_file_parameter_empty_uri_left_unchanged(
    monkeypatch: pytest.MonkeyPatch, _isolated_download_path: str
) -> None:
    download_mock = AsyncMock()
    monkeypatch.setattr(block_module, "download_file", download_mock)

    _fake_storage_app(monkeypatch, save=AsyncMock(), get=AsyncMock(return_value=[]))
    _wire_block_runtime(monkeypatch, values={"resume": {"s3uri": ""}})

    block = CodeBlock(
        label="code_upload",
        code="resolved = resume",
        output_parameter=_output_parameter("code_out"),
        parameters=[_file_url_parameter("resume")],
    )
    result = await block.execute(workflow_run_id="wr_1", workflow_run_block_id="", organization_id="o_1")

    assert result.success is True
    assert result.output_parameter_value["resolved"] == {"s3uri": ""}
    download_mock.assert_not_awaited()


def _download_intent_events(logs: list[dict]) -> list[dict]:
    return [entry for entry in logs if entry.get("event") == "codeblock.download_intent_unregistered"]


# The shape generated code actually takes: the download terminal is present and does raise on
# timeout, but a bare ``except`` converts that raise into a returned blocker string, so the block
# returns normally and reports success.
_SWALLOWED_DOWNLOAD_TERMINAL = """
downloaded_files = []
blocker = ""
try:
    async with page.expect_download(timeout=15000) as download_info:
        await latest.click()
    download = await download_info.value
    downloaded_files.append({"file_name": download.suggested_filename})
except Exception:
    blocker = "the link opens a delivery form rather than a downloadable file"
return {"downloaded_files": downloaded_files, "blocker": blocker}
"""


@pytest.mark.asyncio
async def test_download_intent_block_registering_nothing_is_counted(
    monkeypatch: pytest.MonkeyPatch, _isolated_download_path: str
) -> None:
    skyvern_context.set(SkyvernContext(organization_id="o_1", workflow_run_id="wr_1", run_id="wr_1"))

    _fake_storage_app(monkeypatch, save=AsyncMock(), get=AsyncMock(return_value=[]))
    _wire_block_runtime(monkeypatch)

    block = CodeBlock(
        label="download_invoice",
        code='result = {"downloaded_files": [], "blocker": "no downloadable file"}\nreturn result',
        output_parameter=_output_parameter("code_out"),
    )
    with capture_logs() as logs:
        result = await block.execute(workflow_run_id="wr_1", workflow_run_block_id="", organization_id="o_1")

    events = _download_intent_events(logs)
    assert len(events) == 1
    assert events[0]["block_label"] == "download_invoice"
    assert events[0]["copilot_authored"] is False
    # Telemetry, not outcome authority: the outcome contract is the workflow's persisted
    # completion criteria, graded at run finalization.
    assert result.success is True


@pytest.mark.asyncio
async def test_copilot_authored_download_block_registering_nothing_is_counted_with_lineage(
    monkeypatch: pytest.MonkeyPatch, _isolated_download_path: str
) -> None:
    skyvern_context.set(SkyvernContext(organization_id="o_1", workflow_run_id="wr_1", run_id="wr_1"))

    _fake_storage_app(monkeypatch, save=AsyncMock(), get=AsyncMock(return_value=[]))
    _wire_block_runtime(monkeypatch, workflow=_copilot_workflow())

    # The production shape, executed: page is a stub, so page.expect_download raises inside the
    # try, the bare except converts it into the blocker string, and the code returns normally.
    block = CodeBlock(
        label="download_invoice",
        code=_SWALLOWED_DOWNLOAD_TERMINAL,
        output_parameter=_output_parameter("code_out"),
    )
    with capture_logs() as logs:
        result = await block.execute(workflow_run_id="wr_1", workflow_run_block_id="", organization_id="o_1")

    events = _download_intent_events(logs)
    assert len(events) == 1
    assert events[0]["copilot_authored"] is True
    assert result.success is True


@pytest.mark.asyncio
async def test_copilot_authored_download_block_that_registers_a_file_succeeds(
    monkeypatch: pytest.MonkeyPatch, _isolated_download_path: str
) -> None:
    skyvern_context.set(SkyvernContext(organization_id="o_1", workflow_run_id="wr_1", run_id="wr_1"))

    file_info = FileInfo(
        url="https://api.example.com/v1/artifacts/a_dl_1/content?artifact_name=invoice.pdf",
        filename="invoice.pdf",
        checksum="deadbeef",
        artifact_id="a_dl_1",
        modified_at=datetime(2026, 6, 14, 12, 0, tzinfo=UTC),
    )
    _fake_storage_app(monkeypatch, save=AsyncMock(), get=AsyncMock(side_effect=[[], [file_info]]))
    _wire_block_runtime(monkeypatch, workflow=_copilot_workflow())

    block = CodeBlock(
        label="download_invoice",
        code='result = {"downloaded_files": []}\nreturn result',
        output_parameter=_output_parameter("code_out"),
    )
    with capture_logs() as logs:
        result = await block.execute(workflow_run_id="wr_1", workflow_run_block_id="", organization_id="o_1")

    assert _download_intent_events(logs) == []
    assert result.success is True
    assert result.output_parameter_value["downloaded_file_artifact_ids"] == ["a_dl_1"]


@pytest.mark.asyncio
async def test_download_intent_block_that_registers_a_file_is_not_counted(
    monkeypatch: pytest.MonkeyPatch, _isolated_download_path: str
) -> None:
    skyvern_context.set(SkyvernContext(organization_id="o_1", workflow_run_id="wr_1", run_id="wr_1"))

    file_info = FileInfo(
        url="https://api.example.com/v1/artifacts/a_dl_1/content?artifact_name=invoice.pdf",
        filename="invoice.pdf",
        checksum="deadbeef",
        artifact_id="a_dl_1",
        modified_at=datetime(2026, 6, 14, 12, 0, tzinfo=UTC),
    )
    _fake_storage_app(monkeypatch, save=AsyncMock(), get=AsyncMock(side_effect=[[], [file_info]]))
    _wire_block_runtime(monkeypatch)

    block = CodeBlock(
        label="download_invoice",
        code='result = {"downloaded_files": []}\nreturn result',
        output_parameter=_output_parameter("code_out"),
    )
    with capture_logs() as logs:
        result = await block.execute(workflow_run_id="wr_1", workflow_run_block_id="", organization_id="o_1")

    assert _download_intent_events(logs) == []
    assert result.success is True
    assert result.output_parameter_value["downloaded_file_artifact_ids"] == ["a_dl_1"]


@pytest.mark.asyncio
async def test_block_without_download_intent_is_not_counted(
    monkeypatch: pytest.MonkeyPatch, _isolated_download_path: str
) -> None:
    skyvern_context.set(SkyvernContext(organization_id="o_1", workflow_run_id="wr_1", run_id="wr_1"))

    _fake_storage_app(monkeypatch, save=AsyncMock(), get=AsyncMock(return_value=[]))
    _wire_block_runtime(monkeypatch)

    block = CodeBlock(
        label="extract_only",
        code='return {"blocker": "no downloadable file", "rows": []}',
        output_parameter=_output_parameter("code_out"),
    )
    with capture_logs() as logs:
        result = await block.execute(workflow_run_id="wr_1", workflow_run_block_id="", organization_id="o_1")

    assert _download_intent_events(logs) == []
    assert result.success is True


@pytest.mark.asyncio
async def test_terminate_maps_to_terminated_not_failed(
    monkeypatch: pytest.MonkeyPatch, _isolated_download_path: str
) -> None:
    """``page.terminate()`` is the sanctioned way for a block to report it cannot reach the file."""
    skyvern_context.set(SkyvernContext(organization_id="o_1", workflow_run_id="wr_1", run_id="wr_1"))

    _fake_storage_app(monkeypatch, save=AsyncMock(), get=AsyncMock(return_value=[]))
    _wire_block_runtime(monkeypatch)
    monkeypatch.setattr(
        CodeBlock,
        "execute_user_function_with_timeout",
        AsyncMock(side_effect=ScriptTerminationException("Terminate called: no downloadable file")),
    )

    block = CodeBlock(
        label="download_invoice",
        code="value = 'unused'",
        output_parameter=_output_parameter("code_out"),
    )
    result = await block.execute(workflow_run_id="wr_1", workflow_run_block_id="", organization_id="o_1")

    assert result.success is False
    assert result.status == BlockStatus.terminated
    assert "no downloadable file" in (result.failure_reason or "")


@pytest.mark.asyncio
async def test_complete_verify_rejection_still_fails(
    monkeypatch: pytest.MonkeyPatch, _isolated_download_path: str
) -> None:
    """The IllegitCompleteScriptTermination subclass must not be read as an intentional stop."""
    skyvern_context.set(SkyvernContext(organization_id="o_1", workflow_run_id="wr_1", run_id="wr_1"))

    _fake_storage_app(monkeypatch, save=AsyncMock(), get=AsyncMock(return_value=[]))
    _wire_block_runtime(monkeypatch)
    monkeypatch.setattr(
        CodeBlock,
        "execute_user_function_with_timeout",
        AsyncMock(side_effect=IllegitCompleteScriptTermination("verifier rejected")),
    )

    block = CodeBlock(
        label="download_invoice",
        code="value = 'unused'",
        output_parameter=_output_parameter("code_out"),
    )
    result = await block.execute(workflow_run_id="wr_1", workflow_run_block_id="", organization_id="o_1")

    assert result.success is False
    assert result.status != BlockStatus.terminated


def test_swallowed_download_terminal_still_reads_as_download_intent() -> None:
    """A caught raise hides the failure from the block result but not from the AST predicate."""
    assert code_is_download_intent(_SWALLOWED_DOWNLOAD_TERMINAL) is True


@pytest.mark.asyncio
async def test_secure_runner_download_intent_is_counted_too(
    monkeypatch: pytest.MonkeyPatch, _isolated_download_path: str
) -> None:
    """Engine parity: the sidecar path returns before the inline diff, so it emits its own record."""
    skyvern_context.set(SkyvernContext(organization_id="o_1", workflow_run_id="wr_1", run_id="wr_1"))

    _fake_storage_app(monkeypatch, save=AsyncMock(), get=AsyncMock(return_value=[]))
    _wire_block_runtime(monkeypatch, workflow=_copilot_workflow())
    _wire_secure_runner(monkeypatch, output={"downloaded_files": [], "blocker": "no downloadable file"})

    block = CodeBlock(
        label="download_invoice",
        code=_SWALLOWED_DOWNLOAD_TERMINAL,
        output_parameter=_output_parameter("code_out"),
    )
    with capture_logs() as logs:
        result = await block.execute(workflow_run_id="wr_1", workflow_run_block_id="", organization_id="o_1")

    events = _download_intent_events(logs)
    assert len(events) == 1
    assert events[0]["engine"] == "secure_runner"
    assert result.success is True


@pytest.mark.asyncio
async def test_secure_runner_registered_download_is_not_counted(
    monkeypatch: pytest.MonkeyPatch, _isolated_download_path: str
) -> None:
    skyvern_context.set(SkyvernContext(organization_id="o_1", workflow_run_id="wr_1", run_id="wr_1"))

    _fake_storage_app(monkeypatch, save=AsyncMock(), get=AsyncMock(return_value=[]))
    _wire_block_runtime(monkeypatch, workflow=_copilot_workflow())
    _wire_secure_runner(monkeypatch, output={"downloaded_files": [{"filename": "invoice.pdf"}]})

    block = CodeBlock(
        label="download_invoice",
        code=_SWALLOWED_DOWNLOAD_TERMINAL,
        output_parameter=_output_parameter("code_out"),
    )
    with capture_logs() as logs:
        result = await block.execute(workflow_run_id="wr_1", workflow_run_block_id="", organization_id="o_1")

    assert _download_intent_events(logs) == []
    assert result.success is True


@pytest.mark.asyncio
async def test_inline_file_on_disk_with_no_registration_fails_loudly(
    monkeypatch: pytest.MonkeyPatch, _isolated_download_path: str
) -> None:
    """A file this block put in the run directory that never registered cannot persist as an output
    that reads like nothing was downloaded."""
    skyvern_context.set(SkyvernContext(organization_id="o_1", workflow_run_id="wr_1", run_id="wr_1"))

    _fake_storage_app(monkeypatch, save=AsyncMock(), get=AsyncMock(return_value=[]))
    _wire_block_runtime(monkeypatch)

    async def _download_then_return_unexpected_shape(*args: object, **kwargs: object) -> object:
        _write_run_download(_isolated_download_path)
        return None

    monkeypatch.setattr(
        CodeBlock,
        "execute_user_function_with_timeout",
        AsyncMock(side_effect=_download_then_return_unexpected_shape),
    )

    block = CodeBlock(
        label="download_invoice",
        code="value = 'unused'",
        output_parameter=_output_parameter("code_out"),
    )
    result = await block.execute(workflow_run_id="wr_1", workflow_run_block_id="", organization_id="o_1")

    assert result.success is False
    assert result.status == BlockStatus.failed
    assert result.failure_reason == block_module.DOWNLOAD_BINDING_FAILURE_REASON
    persisted = _persisted_output()
    assert persisted is not None
    assert persisted[block_module.UNBOUND_DOWNLOAD_OUTPUT_KEY] is True
    assert "downloaded_files" not in persisted
    assert persisted == result.output_parameter_value


@pytest.mark.asyncio
async def test_secure_runner_file_on_disk_with_no_registration_fails_loudly(
    monkeypatch: pytest.MonkeyPatch, _isolated_download_path: str
) -> None:
    """Engine parity: authored registration-shaped keys are not registration evidence."""
    skyvern_context.set(SkyvernContext(organization_id="o_1", workflow_run_id="wr_1", run_id="wr_1"))

    _fake_storage_app(monkeypatch, save=AsyncMock(), get=AsyncMock(return_value=[]))
    _wire_block_runtime(monkeypatch)
    _wire_secure_runner(
        monkeypatch,
        output={"downloaded_files": [{"file_name": "invoice.pdf"}]},
        on_execute=lambda: _write_run_download(_isolated_download_path),
    )

    block = CodeBlock(
        label="download_invoice",
        code=_SWALLOWED_DOWNLOAD_TERMINAL,
        output_parameter=_output_parameter("code_out"),
    )
    result = await block.execute(workflow_run_id="wr_1", workflow_run_block_id="", organization_id="o_1")

    assert result.success is False
    assert result.status == BlockStatus.failed
    assert result.failure_reason == block_module.DOWNLOAD_BINDING_FAILURE_REASON
    persisted = _persisted_output()
    assert persisted[block_module.UNBOUND_DOWNLOAD_OUTPUT_KEY] is True
    # The authored key is the one that must not survive — asserting only on a key the code never
    # wrote would pass no matter what the binder did.
    assert "downloaded_files" not in persisted
    assert "downloaded_file_urls" not in persisted
    assert block_output_has_registered_download(persisted) is False
    assert persisted == result.output_parameter_value


@pytest.mark.asyncio
async def test_secure_runner_binds_host_registration_over_authored_keys(
    monkeypatch: pytest.MonkeyPatch, _isolated_download_path: str
) -> None:
    skyvern_context.set(SkyvernContext(organization_id="o_1", workflow_run_id="wr_1", run_id="wr_1"))

    file_info = FileInfo(
        url="https://api.example.com/v1/artifacts/a_dl_1/content?artifact_name=invoice.pdf",
        filename="invoice.pdf",
        checksum="deadbeef",
        artifact_id="a_dl_1",
        modified_at=datetime(2026, 6, 14, 12, 0, tzinfo=UTC),
    )
    save_mock = AsyncMock()
    _fake_storage_app(monkeypatch, save=save_mock, get=AsyncMock(side_effect=[[], [file_info]]))
    _wire_block_runtime(monkeypatch)
    _wire_secure_runner(
        monkeypatch,
        output={"downloaded_files": [{"file_name": "not-the-registered-file.pdf"}], "status": "ok"},
        on_execute=lambda: _write_run_download(_isolated_download_path),
    )

    block = CodeBlock(
        label="download_invoice",
        code=_SWALLOWED_DOWNLOAD_TERMINAL,
        output_parameter=_output_parameter("code_out"),
    )
    result = await block.execute(workflow_run_id="wr_1", workflow_run_block_id="", organization_id="o_1")

    assert result.success is True
    output = result.output_parameter_value
    assert output["downloaded_files"] == [file_info.model_dump()]
    assert output["downloaded_file_artifact_ids"] == ["a_dl_1"]
    assert output["status"] == "ok"
    assert _persisted_output() == output
    # The sidecar already uploaded what it downloaded; a host save here would double-upload.
    save_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_loop_iteration_filter_emptying_the_list_is_not_a_binding_failure(
    monkeypatch: pytest.MonkeyPatch, _isolated_download_path: str
) -> None:
    """A file carried in from an earlier iteration is filtered out legitimately, and this block put
    nothing new in the run directory."""
    prev_file = FileInfo(
        url="https://api.example.com/v1/artifacts/a_prev/content?artifact_name=prev.pdf",
        filename="prev.pdf",
        checksum="abc",
        artifact_id="a_prev",
    )
    skyvern_context.set(
        SkyvernContext(
            organization_id="o_1",
            workflow_run_id="wr_1",
            run_id="wr_1",
            loop_internal_state={
                "downloaded_file_signatures_before_iteration": [
                    block_module.to_downloaded_file_signature(prev_file),
                ],
            },
        )
    )
    _write_run_download(_isolated_download_path, filename="prev.pdf")

    _fake_storage_app(monkeypatch, save=AsyncMock(), get=AsyncMock(return_value=[prev_file]))
    _wire_block_runtime(monkeypatch)

    block = CodeBlock(
        label="code_download",
        code="saved = 'ok'",
        output_parameter=_output_parameter("code_out"),
    )
    result = await block.execute(workflow_run_id="wr_1", workflow_run_block_id="", organization_id="o_1")

    assert result.success is True
    assert result.output_parameter_value == {"saved": "ok"}


@pytest.mark.asyncio
async def test_registration_binds_onto_an_unexpected_return_shape(
    monkeypatch: pytest.MonkeyPatch, _isolated_download_path: str
) -> None:
    """The pass path: a legitimate download lands in the declared output whatever the generated code
    chose to return."""
    skyvern_context.set(SkyvernContext(organization_id="o_1", workflow_run_id="wr_1", run_id="wr_1"))

    file_info = FileInfo(
        url="https://api.example.com/v1/artifacts/a_dl_1/content?artifact_name=invoice.pdf",
        filename="invoice.pdf",
        checksum="deadbeef",
        artifact_id="a_dl_1",
        modified_at=datetime(2026, 6, 14, 12, 0, tzinfo=UTC),
    )
    _fake_storage_app(monkeypatch, save=AsyncMock(), get=AsyncMock(side_effect=[[], [file_info]]))
    _wire_block_runtime(monkeypatch)

    async def _download_then_return_none(*args: object, **kwargs: object) -> object:
        _write_run_download(_isolated_download_path)
        return None

    monkeypatch.setattr(
        CodeBlock,
        "execute_user_function_with_timeout",
        AsyncMock(side_effect=_download_then_return_none),
    )

    block = CodeBlock(
        label="download_invoice",
        code="value = 'unused'",
        output_parameter=_output_parameter("code_out"),
    )
    result = await block.execute(workflow_run_id="wr_1", workflow_run_block_id="", organization_id="o_1")

    assert result.success is True
    output = result.output_parameter_value
    assert output["downloaded_files"] == [file_info.model_dump()]
    assert output["downloaded_file_urls"] == [file_info.url]
    assert output["downloaded_file_artifact_ids"] == ["a_dl_1"]
    assert _persisted_output() == output


def _raise_after_download(download_root: str, error: Exception) -> Callable[..., object]:
    """User code that lands a file in the run directory and then dies."""

    async def _run(*args: object, **kwargs: object) -> object:
        _write_run_download(download_root)
        raise error

    return _run


@pytest.mark.asyncio
async def test_raising_block_binds_download_that_already_landed(
    monkeypatch: pytest.MonkeyPatch, _isolated_download_path: str
) -> None:
    """A block that downloads and then raises still carries the registration evidence out.

    The download reached the run directory, so it is accountable regardless of how the block
    exited; the block's own failure reason is what survives, not the binding verdict."""
    skyvern_context.set(SkyvernContext(organization_id="o_1", workflow_run_id="wr_1", run_id="wr_1"))

    file_info = FileInfo(
        url="https://api.example.com/v1/artifacts/a_dl_1/content?artifact_name=invoice.pdf",
        filename="invoice.pdf",
        checksum="deadbeef",
        artifact_id="a_dl_1",
        modified_at=datetime(2026, 6, 14, 12, 0, tzinfo=UTC),
    )
    _fake_storage_app(monkeypatch, save=AsyncMock(), get=AsyncMock(side_effect=[[], [file_info]]))
    _wire_block_runtime(monkeypatch)
    monkeypatch.setattr(
        CodeBlock,
        "execute_user_function_with_timeout",
        AsyncMock(side_effect=_raise_after_download(_isolated_download_path, RuntimeError("Download is starting"))),
    )

    block = CodeBlock(
        label="download_invoice",
        code="value = 'unused'",
        output_parameter=_output_parameter("code_out"),
    )
    result = await block.execute(workflow_run_id="wr_1", workflow_run_block_id="", organization_id="o_1")

    assert result.success is False
    output = result.output_parameter_value
    assert output is not None
    assert output["downloaded_files"] == [file_info.model_dump()]
    assert output["downloaded_file_urls"] == [file_info.url]
    assert output["downloaded_file_artifact_ids"] == ["a_dl_1"]
    assert "Download is starting" in (result.failure_reason or "")
    assert result.failure_reason != block_module.DOWNLOAD_BINDING_FAILURE_REASON
    assert _persisted_output() == output


@pytest.mark.asyncio
async def test_raising_block_with_unregistered_download_persists_non_null_output(
    monkeypatch: pytest.MonkeyPatch, _isolated_download_path: str
) -> None:
    """The file landed but nothing registered: the output still cannot read as 'no download'."""
    skyvern_context.set(SkyvernContext(organization_id="o_1", workflow_run_id="wr_1", run_id="wr_1"))

    _fake_storage_app(monkeypatch, save=AsyncMock(), get=AsyncMock(return_value=[]))
    _wire_block_runtime(monkeypatch)
    monkeypatch.setattr(
        CodeBlock,
        "execute_user_function_with_timeout",
        AsyncMock(side_effect=_raise_after_download(_isolated_download_path, RuntimeError("boom"))),
    )

    block = CodeBlock(
        label="download_invoice",
        code="value = 'unused'",
        output_parameter=_output_parameter("code_out"),
    )
    result = await block.execute(workflow_run_id="wr_1", workflow_run_block_id="", organization_id="o_1")

    assert result.success is False
    persisted = _persisted_output()
    assert persisted is not None
    assert persisted[block_module.UNBOUND_DOWNLOAD_OUTPUT_KEY] is True
    assert persisted == result.output_parameter_value


@pytest.mark.asyncio
async def test_raising_block_without_download_still_persists_no_output(
    monkeypatch: pytest.MonkeyPatch, _isolated_download_path: str
) -> None:
    """A failure that downloaded nothing is unchanged: no registration, no output."""
    skyvern_context.set(SkyvernContext(organization_id="o_1", workflow_run_id="wr_1", run_id="wr_1"))

    _fake_storage_app(monkeypatch, save=AsyncMock(), get=AsyncMock(return_value=[]))
    _wire_block_runtime(monkeypatch)

    async def _just_raise(*args: object, **kwargs: object) -> object:
        raise RuntimeError("boom")

    monkeypatch.setattr(CodeBlock, "execute_user_function_with_timeout", AsyncMock(side_effect=_just_raise))

    block = CodeBlock(
        label="no_download",
        code="value = 'unused'",
        output_parameter=_output_parameter("code_out"),
    )
    result = await block.execute(workflow_run_id="wr_1", workflow_run_block_id="", organization_id="o_1")

    assert result.success is False
    assert result.output_parameter_value is None
    CodeBlock.record_output_parameter_value.assert_not_awaited()


@pytest.mark.asyncio
async def test_timed_out_block_binds_download_that_already_landed(
    monkeypatch: pytest.MonkeyPatch, _isolated_download_path: str
) -> None:
    """Engine parity for the timeout exit — the slow download that did land is still accountable."""
    skyvern_context.set(SkyvernContext(organization_id="o_1", workflow_run_id="wr_1", run_id="wr_1"))

    file_info = FileInfo(
        url="https://api.example.com/v1/artifacts/a_dl_1/content?artifact_name=invoice.pdf",
        filename="invoice.pdf",
        checksum="deadbeef",
        artifact_id="a_dl_1",
        modified_at=datetime(2026, 6, 14, 12, 0, tzinfo=UTC),
    )
    _fake_storage_app(monkeypatch, save=AsyncMock(), get=AsyncMock(side_effect=[[], [file_info]]))
    _wire_block_runtime(monkeypatch)
    monkeypatch.setattr(
        CodeBlock,
        "execute_user_function_with_timeout",
        AsyncMock(side_effect=_raise_after_download(_isolated_download_path, asyncio.TimeoutError())),
    )

    block = CodeBlock(
        label="download_invoice",
        code="value = 'unused'",
        output_parameter=_output_parameter("code_out"),
    )
    result = await block.execute(workflow_run_id="wr_1", workflow_run_block_id="", organization_id="o_1")

    assert result.success is False
    output = result.output_parameter_value
    assert output is not None
    assert output["downloaded_file_urls"] == [file_info.url]
    assert "exceeded" in (result.failure_reason or "")


@pytest.mark.asyncio
async def test_authored_registration_keys_are_dropped_without_host_evidence(
    monkeypatch: pytest.MonkeyPatch, _isolated_download_path: str
) -> None:
    """Keys the executed code wrote for itself are not registration evidence.

    The host registered nothing, so no consumer reading this output may find a download in it."""
    skyvern_context.set(SkyvernContext(organization_id="o_1", workflow_run_id="wr_1", run_id="wr_1"))

    _fake_storage_app(monkeypatch, save=AsyncMock(), get=AsyncMock(return_value=[]))
    _wire_block_runtime(monkeypatch)

    async def _claim_a_download_that_never_happened(*args: object, **kwargs: object) -> object:
        return {
            "downloaded_files": [{"filename": "invoice.pdf"}],
            "downloaded_file_urls": ["file:///tmp/invoice.pdf"],
            "downloaded_file_artifact_ids": ["a_made_up"],
            "note": "model wrote these itself",
        }

    monkeypatch.setattr(
        CodeBlock,
        "execute_user_function_with_timeout",
        AsyncMock(side_effect=_claim_a_download_that_never_happened),
    )

    block = CodeBlock(
        label="download_invoice",
        code="value = 'unused'",
        output_parameter=_output_parameter("code_out"),
    )
    result = await block.execute(workflow_run_id="wr_1", workflow_run_block_id="", organization_id="o_1")

    persisted = _persisted_output()
    assert persisted["note"] == "model wrote these itself"
    assert "downloaded_files" not in persisted
    assert "downloaded_file_urls" not in persisted
    assert "downloaded_file_artifact_ids" not in persisted
    assert block_output_has_registered_download(persisted) is False
    assert downloaded_file_count_from_output(result.output_parameter_value) is None


@pytest.mark.asyncio
async def test_registration_timeout_does_not_fail_a_block_that_downloaded(
    monkeypatch: pytest.MonkeyPatch, _isolated_download_path: str
) -> None:
    """A storage timeout is not evidence that the code swallowed a download.

    Workflow finalization retries the save, so failing the block here would destroy a run whose
    download actually succeeded."""
    skyvern_context.set(SkyvernContext(organization_id="o_1", workflow_run_id="wr_1", run_id="wr_1"))

    _fake_storage_app(
        monkeypatch,
        save=AsyncMock(side_effect=asyncio.TimeoutError()),
        get=AsyncMock(return_value=[]),
    )
    _wire_block_runtime(monkeypatch)

    async def _download_then_return(*args: object, **kwargs: object) -> object:
        _write_run_download(_isolated_download_path)
        return {"ok": True}

    monkeypatch.setattr(
        CodeBlock,
        "execute_user_function_with_timeout",
        AsyncMock(side_effect=_download_then_return),
    )

    block = CodeBlock(
        label="download_invoice",
        code="value = 'unused'",
        output_parameter=_output_parameter("code_out"),
    )
    result = await block.execute(workflow_run_id="wr_1", workflow_run_block_id="", organization_id="o_1")

    assert result.success is True
    assert result.status == BlockStatus.completed
    assert result.failure_reason != block_module.DOWNLOAD_BINDING_FAILURE_REASON
    assert block_module.UNBOUND_DOWNLOAD_OUTPUT_KEY not in (result.output_parameter_value or {})


def test_unreadable_download_dir_is_an_unknown_snapshot_not_an_empty_one() -> None:
    """An unreadable directory must not read as empty, or pre-existing files look new."""
    assert block_module.local_download_dir_file_identities(None) is None


def test_same_name_overwrite_changes_download_dir_identity(_isolated_download_path: str) -> None:
    """The snapshot must see a rewrite under an existing name, not just new names."""
    _write_run_download(_isolated_download_path)
    before = block_module.local_download_dir_file_identities("wr_1")
    _write_run_download(_isolated_download_path, content=b"%PDF-1.4 a longer, different statement body")
    after = block_module.local_download_dir_file_identities("wr_1")
    assert before != after
    assert {identity[0] for identity in after} == {"invoice.pdf"}


@pytest.mark.asyncio
async def test_storage_skip_reaches_binder_as_registration_incomplete(
    monkeypatch: pytest.MonkeyPatch, _isolated_download_path: str
) -> None:
    """A per-file storage skip is 'registration could not complete', not proof the code swallowed
    a download: the block completes and the binder abstains instead of accusing (SKY-13782)."""
    skyvern_context.set(SkyvernContext(organization_id="o_1", workflow_run_id="wr_1", run_id="wr_1"))

    _fake_storage_app(
        monkeypatch,
        save=AsyncMock(side_effect=DownloadSaveIncompleteError(["invoice.pdf"])),
        get=AsyncMock(return_value=[]),
    )
    _wire_block_runtime(monkeypatch)

    async def _download_then_return(*args: object, **kwargs: object) -> object:
        _write_run_download(_isolated_download_path)
        return {"ok": True}

    monkeypatch.setattr(
        CodeBlock,
        "execute_user_function_with_timeout",
        AsyncMock(side_effect=_download_then_return),
    )

    block = CodeBlock(
        label="download_invoice",
        code="value = 'unused'",
        output_parameter=_output_parameter("code_out"),
    )
    with capture_logs() as logs:
        result = await block.execute(workflow_run_id="wr_1", workflow_run_block_id="", organization_id="o_1")

    assert result.success is True
    assert result.status == BlockStatus.completed
    assert result.failure_reason != block_module.DOWNLOAD_BINDING_FAILURE_REASON
    assert block_module.UNBOUND_DOWNLOAD_OUTPUT_KEY not in (result.output_parameter_value or {})
    skipped = [log for log in logs if log.get("event") == "codeblock.download_binding_verdict_skipped"]
    assert skipped
    assert skipped[0]["reason"] == "registration_incomplete"


@pytest.mark.asyncio
async def test_failing_block_overwriting_existing_download_still_registers_it(
    monkeypatch: pytest.MonkeyPatch, _isolated_download_path: str
) -> None:
    """A failing block that replaces an existing download under the same name still carries the
    registration evidence out — a name-only snapshot diff would miss the overwrite (SKY-13782)."""
    skyvern_context.set(SkyvernContext(organization_id="o_1", workflow_run_id="wr_1", run_id="wr_1"))
    _write_run_download(_isolated_download_path)

    file_info = FileInfo(
        url="https://api.example.com/v1/artifacts/a_dl_1/content?artifact_name=invoice.pdf",
        filename="invoice.pdf",
        checksum="deadbeef",
        artifact_id="a_dl_1",
        modified_at=datetime(2026, 6, 14, 12, 0, tzinfo=UTC),
    )
    _fake_storage_app(monkeypatch, save=AsyncMock(), get=AsyncMock(side_effect=[[], [], [file_info]]))
    _wire_block_runtime(monkeypatch)

    async def _overwrite_then_raise(*args: object, **kwargs: object) -> object:
        _write_run_download(_isolated_download_path, content=b"%PDF-1.4 a longer, different statement body")
        raise RuntimeError("boom after overwrite")

    monkeypatch.setattr(
        CodeBlock,
        "execute_user_function_with_timeout",
        AsyncMock(side_effect=_overwrite_then_raise),
    )

    block = CodeBlock(
        label="download_invoice",
        code="value = 'unused'",
        output_parameter=_output_parameter("code_out"),
    )
    result = await block.execute(workflow_run_id="wr_1", workflow_run_block_id="", organization_id="o_1")

    assert result.success is False
    assert "boom after overwrite" in (result.failure_reason or "")
    output = result.output_parameter_value
    assert output is not None
    assert output["downloaded_files"] == [file_info.model_dump()]
    assert output["downloaded_file_urls"] == [file_info.url]


@pytest.mark.asyncio
async def test_loop_re_download_of_registered_file_is_not_accused(
    monkeypatch: pytest.MonkeyPatch, _isolated_download_path: str
) -> None:
    """A later iteration re-downloading a file this run already registered must not fail the block:
    the identity diff sees the rewrite, but the registration read-back accounts for it (SKY-13782)."""
    prev_file = FileInfo(
        url="https://api.example.com/v1/artifacts/a_prev/content?artifact_name=prev.pdf",
        filename="prev.pdf",
        checksum="abc",
        artifact_id="a_prev",
    )
    skyvern_context.set(
        SkyvernContext(
            organization_id="o_1",
            workflow_run_id="wr_1",
            run_id="wr_1",
            loop_internal_state={
                "downloaded_file_signatures_before_iteration": [
                    block_module.to_downloaded_file_signature(prev_file),
                ],
            },
        )
    )
    _write_run_download(_isolated_download_path, filename="prev.pdf")

    _fake_storage_app(monkeypatch, save=AsyncMock(), get=AsyncMock(return_value=[prev_file]))
    _wire_block_runtime(monkeypatch)

    async def _rewrite_same_file(*args: object, **kwargs: object) -> object:
        path = os.path.join(_isolated_download_path, "wr_1", "prev.pdf")
        stat = os.stat(path)
        _write_run_download(_isolated_download_path, filename="prev.pdf")
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 2_000_000))
        return {"ok": True}

    monkeypatch.setattr(
        CodeBlock,
        "execute_user_function_with_timeout",
        AsyncMock(side_effect=_rewrite_same_file),
    )

    block = CodeBlock(
        label="code_download",
        code="value = 'unused'",
        output_parameter=_output_parameter("code_out"),
    )
    result = await block.execute(workflow_run_id="wr_1", workflow_run_block_id="", organization_id="o_1")

    assert result.success is True
    assert result.failure_reason != block_module.DOWNLOAD_BINDING_FAILURE_REASON
    assert block_module.UNBOUND_DOWNLOAD_OUTPUT_KEY not in (result.output_parameter_value or {})


@pytest.mark.asyncio
async def test_partial_storage_skip_still_binds_the_files_that_saved(
    monkeypatch: pytest.MonkeyPatch, _isolated_download_path: str
) -> None:
    """One skipped file strips neither the binding evidence of the files that saved nor the
    block's success; the verdict abstains instead of accusing (SKY-13782)."""
    skyvern_context.set(SkyvernContext(organization_id="o_1", workflow_run_id="wr_1", run_id="wr_1"))

    file_info = FileInfo(
        url="https://api.example.com/v1/artifacts/a_dl_1/content?artifact_name=invoice.pdf",
        filename="invoice.pdf",
        checksum="deadbeef",
        artifact_id="a_dl_1",
        modified_at=datetime(2026, 6, 14, 12, 0, tzinfo=UTC),
    )
    _fake_storage_app(
        monkeypatch,
        save=AsyncMock(side_effect=DownloadSaveIncompleteError(["statement.pdf"])),
        get=AsyncMock(side_effect=[[], [file_info]]),
    )
    _wire_block_runtime(monkeypatch)

    async def _download_two_files(*args: object, **kwargs: object) -> object:
        _write_run_download(_isolated_download_path)
        _write_run_download(_isolated_download_path, filename="statement.pdf")
        return {"ok": True}

    monkeypatch.setattr(
        CodeBlock,
        "execute_user_function_with_timeout",
        AsyncMock(side_effect=_download_two_files),
    )

    block = CodeBlock(
        label="download_invoice",
        code="value = 'unused'",
        output_parameter=_output_parameter("code_out"),
    )
    with capture_logs() as logs:
        result = await block.execute(workflow_run_id="wr_1", workflow_run_block_id="", organization_id="o_1")

    assert result.success is True
    assert result.status == BlockStatus.completed
    output = result.output_parameter_value
    assert output["downloaded_files"] == [file_info.model_dump()]
    assert output["downloaded_file_urls"] == [file_info.url]
    skipped = [log for log in logs if log.get("event") == "codeblock.download_binding_verdict_skipped"]
    assert skipped
    assert skipped[0]["reason"] == "registration_incomplete"


@pytest.mark.asyncio
async def test_failing_block_overwrite_of_registered_name_forces_resave(
    monkeypatch: pytest.MonkeyPatch, _isolated_download_path: str
) -> None:
    """A registration made before the overwrite is stale for the rewritten bytes: the failure path
    re-saves instead of treating the old row as coverage (SKY-13782)."""
    skyvern_context.set(SkyvernContext(organization_id="o_1", workflow_run_id="wr_1", run_id="wr_1"))
    _write_run_download(_isolated_download_path)

    stale = FileInfo(
        url="https://api.example.com/v1/artifacts/a_dl_1/content?artifact_name=invoice.pdf",
        filename="invoice.pdf",
        checksum="stale",
        artifact_id="a_dl_1",
    )
    fresh = FileInfo(
        url="https://api.example.com/v1/artifacts/a_dl_1/content?artifact_name=invoice.pdf",
        filename="invoice.pdf",
        checksum="fresh",
        artifact_id="a_dl_1",
        modified_at=datetime(2026, 6, 14, 12, 0, tzinfo=UTC),
    )
    save_mock = AsyncMock()
    _fake_storage_app(monkeypatch, save=save_mock, get=AsyncMock(side_effect=[[stale], [stale], [fresh]]))
    _wire_block_runtime(monkeypatch)

    async def _overwrite_then_raise(*args: object, **kwargs: object) -> object:
        _write_run_download(_isolated_download_path, content=b"%PDF-1.4 a longer, different statement body")
        raise RuntimeError("boom after overwrite")

    monkeypatch.setattr(
        CodeBlock,
        "execute_user_function_with_timeout",
        AsyncMock(side_effect=_overwrite_then_raise),
    )

    block = CodeBlock(
        label="download_invoice",
        code="value = 'unused'",
        output_parameter=_output_parameter("code_out"),
    )
    result = await block.execute(workflow_run_id="wr_1", workflow_run_block_id="", organization_id="o_1")

    assert result.success is False
    assert "boom after overwrite" in (result.failure_reason or "")
    save_mock.assert_awaited()
    output = result.output_parameter_value
    assert output is not None
    assert output["downloaded_files"] == [fresh.model_dump()]


@pytest.mark.asyncio
async def test_unrelated_storage_skip_does_not_disarm_the_swallow_verdict(
    monkeypatch: pytest.MonkeyPatch, _isolated_download_path: str
) -> None:
    """A skip on a file the block never touched must not silence the loud verdict for a download
    the code genuinely swallowed — the abstention is per skipped name, not global (SKY-13782)."""
    skyvern_context.set(SkyvernContext(organization_id="o_1", workflow_run_id="wr_1", run_id="wr_1"))

    _fake_storage_app(
        monkeypatch,
        save=AsyncMock(side_effect=DownloadSaveIncompleteError(["unrelated_leftover.pdf"])),
        get=AsyncMock(side_effect=[[], []]),
    )
    _wire_block_runtime(monkeypatch)

    async def _download_then_return_none(*args: object, **kwargs: object) -> object:
        _write_run_download(_isolated_download_path)
        return None

    monkeypatch.setattr(
        CodeBlock,
        "execute_user_function_with_timeout",
        AsyncMock(side_effect=_download_then_return_none),
    )

    block = CodeBlock(
        label="download_invoice",
        code="value = 'unused'",
        output_parameter=_output_parameter("code_out"),
    )
    result = await block.execute(workflow_run_id="wr_1", workflow_run_block_id="", organization_id="o_1")

    assert result.success is False
    assert result.status == BlockStatus.failed
    assert result.failure_reason == block_module.DOWNLOAD_BINDING_FAILURE_REASON
    persisted = _persisted_output()
    assert persisted is not None
    assert persisted[block_module.UNBOUND_DOWNLOAD_OUTPUT_KEY] is True


@pytest.mark.asyncio
async def test_stale_registration_with_failed_refresh_abstains_loudly(
    monkeypatch: pytest.MonkeyPatch, _isolated_download_path: str
) -> None:
    """When the checksum refresh fails on a same-name overwrite, the file arrives as skipped, so
    the abstain is logged rather than silently granted by the stale registered name (SKY-13782)."""
    skyvern_context.set(SkyvernContext(organization_id="o_1", workflow_run_id="wr_1", run_id="wr_1"))
    _write_run_download(_isolated_download_path)

    stale = FileInfo(
        url="https://api.example.com/v1/artifacts/a_dl_1/content?artifact_name=invoice.pdf",
        filename="invoice.pdf",
        checksum="stale",
        artifact_id="a_dl_1",
    )
    _fake_storage_app(
        monkeypatch,
        save=AsyncMock(side_effect=DownloadSaveIncompleteError(["invoice.pdf"])),
        get=AsyncMock(side_effect=[[stale], [stale]]),
    )
    _wire_block_runtime(monkeypatch)

    async def _overwrite_then_return(*args: object, **kwargs: object) -> object:
        _write_run_download(_isolated_download_path, content=b"%PDF-1.4 a longer, different statement body")
        return {"ok": True}

    monkeypatch.setattr(
        CodeBlock,
        "execute_user_function_with_timeout",
        AsyncMock(side_effect=_overwrite_then_return),
    )

    block = CodeBlock(
        label="download_invoice",
        code="value = 'unused'",
        output_parameter=_output_parameter("code_out"),
    )
    with capture_logs() as logs:
        result = await block.execute(workflow_run_id="wr_1", workflow_run_block_id="", organization_id="o_1")

    assert result.success is True
    assert result.failure_reason != block_module.DOWNLOAD_BINDING_FAILURE_REASON
    skipped = [log for log in logs if log.get("event") == "codeblock.download_binding_verdict_skipped"]
    assert skipped
    assert skipped[0]["reason"] == "registration_incomplete"


def test_none_valued_registration_fields_are_schema_not_claims() -> None:
    """A None or empty registration field survives the strip — removing it breaks strict templates."""
    payload = {"downloaded_files": None, "downloaded_file_urls": [], "note": "schema fields"}
    bound = block_module.bind_downloaded_files_to_output(dict(payload), [])
    assert bound == payload


@pytest.mark.asyncio
async def test_successful_self_heal_binds_the_download_that_preceded_the_raise(
    monkeypatch: pytest.MonkeyPatch, _isolated_download_path: str
) -> None:
    """The completed-with-null arm: code downloads, raises, the heal succeeds with no output.

    Recording the heal's own None without ever looking at the run directory is what persisted a
    completed block with a null output while the file sat on disk (SKY-13694's failing arm)."""
    skyvern_context.set(SkyvernContext(organization_id="o_1", workflow_run_id="wr_1", run_id="wr_1"))

    file_info = FileInfo(
        url="https://api.example.com/v1/artifacts/a_dl_1/content?artifact_name=invoice.pdf",
        filename="invoice.pdf",
        checksum="deadbeef",
        artifact_id="a_dl_1",
        modified_at=datetime(2026, 6, 14, 12, 0, tzinfo=UTC),
    )
    _fake_storage_app(monkeypatch, save=AsyncMock(), get=AsyncMock(side_effect=[[], [file_info]]))
    block_module.app.AGENT_FUNCTION.resolve_self_heal_api_key = AsyncMock(return_value=None)
    _wire_block_runtime(monkeypatch)
    monkeypatch.setattr(
        CodeBlock,
        "execute_user_function_with_timeout",
        AsyncMock(side_effect=_raise_after_download(_isolated_download_path, Exception("Download is starting"))),
    )
    monkeypatch.setattr(CodeBlock, "_self_heal_enabled", AsyncMock(return_value=True))
    monkeypatch.setattr(CodeBlock, "_is_healable_page_failure", lambda self, e, page, engine_selection: True)

    healed = BlockResult(
        success=True,
        output_parameter=_output_parameter("code_out"),
        output_parameter_value=None,
        status=BlockStatus.completed,
        workflow_run_block_id="",
    )
    monkeypatch.setattr(CodeBlock, "_attempt_self_heal", AsyncMock(return_value=healed))

    block = CodeBlock(
        label="download_invoice",
        code="value = 'unused'",
        output_parameter=_output_parameter("code_out"),
    )
    result = await block.execute(workflow_run_id="wr_1", workflow_run_block_id="", organization_id="o_1")

    assert result.success is True
    output = result.output_parameter_value
    assert output is not None
    assert output["downloaded_files"] == [file_info.model_dump()]
    assert output["downloaded_file_urls"] == [file_info.url]
    assert _persisted_output() == output


@pytest.mark.asyncio
async def test_secure_success_registers_its_own_file_even_when_an_earlier_block_registered(
    monkeypatch: pytest.MonkeyPatch, _isolated_download_path: str
) -> None:
    """An earlier block's registration is not evidence for the file this secure block added."""
    skyvern_context.set(SkyvernContext(organization_id="o_1", workflow_run_id="wr_1", run_id="wr_1"))

    earlier = FileInfo(
        url="https://api.example.com/v1/artifacts/a_dl_0/content?artifact_name=earlier.pdf",
        filename="earlier.pdf",
        checksum="beefdead",
        artifact_id="a_dl_0",
        modified_at=datetime(2026, 6, 13, 12, 0, tzinfo=UTC),
    )
    this_block = FileInfo(
        url="https://api.example.com/v1/artifacts/a_dl_1/content?artifact_name=invoice.pdf",
        filename="invoice.pdf",
        checksum="deadbeef",
        artifact_id="a_dl_1",
        modified_at=datetime(2026, 6, 14, 12, 0, tzinfo=UTC),
    )
    save_mock = AsyncMock()
    # Baseline empty; read-back sees only the earlier block's file; post-save read sees both.
    _fake_storage_app(
        monkeypatch,
        save=save_mock,
        get=AsyncMock(side_effect=[[], [earlier], [earlier, this_block]]),
    )
    _wire_block_runtime(monkeypatch)
    _wire_secure_runner(
        monkeypatch,
        output={"status": "ok"},
        on_execute=lambda: _write_run_download(_isolated_download_path),
    )

    block = CodeBlock(
        label="download_invoice",
        code=_SWALLOWED_DOWNLOAD_TERMINAL,
        output_parameter=_output_parameter("code_out"),
    )
    result = await block.execute(workflow_run_id="wr_1", workflow_run_block_id="", organization_id="o_1")

    save_mock.assert_awaited()
    assert result.success is True
    assert result.failure_reason != block_module.DOWNLOAD_BINDING_FAILURE_REASON
    output = result.output_parameter_value
    assert this_block.url in output["downloaded_file_urls"]


@pytest.mark.asyncio
async def test_secure_backstop_runs_when_the_directory_snapshot_is_unreadable(
    monkeypatch: pytest.MonkeyPatch, _isolated_download_path: str
) -> None:
    """An unreadable snapshot is unknown, never empty — coverage cannot be proven, so the save runs."""
    skyvern_context.set(SkyvernContext(organization_id="o_1", workflow_run_id="wr_1", run_id="wr_1"))

    earlier = FileInfo(
        url="https://api.example.com/v1/artifacts/a_dl_0/content?artifact_name=earlier.pdf",
        filename="earlier.pdf",
        checksum="beefdead",
        artifact_id="a_dl_0",
        modified_at=datetime(2026, 6, 13, 12, 0, tzinfo=UTC),
    )
    save_mock = AsyncMock()
    _fake_storage_app(monkeypatch, save=save_mock, get=AsyncMock(return_value=[earlier]))
    _wire_block_runtime(monkeypatch)
    _wire_secure_runner(monkeypatch, output={"status": "ok"})
    monkeypatch.setattr(block_module, "local_download_dir_file_identities", lambda download_run_id: None)

    block = CodeBlock(
        label="download_invoice",
        code=_SWALLOWED_DOWNLOAD_TERMINAL,
        output_parameter=_output_parameter("code_out"),
    )
    result = await block.execute(workflow_run_id="wr_1", workflow_run_block_id="", organization_id="o_1")

    save_mock.assert_awaited()
    assert result.success is True


@pytest.mark.asyncio
async def test_cancel_during_registration_does_not_book_the_block_failed(
    monkeypatch: pytest.MonkeyPatch, _isolated_download_path: str
) -> None:
    """A cancel inside the storage round-trips keeps the executed block's seat successful.

    Booking it failed would also skip the billing hook for code that ran to completion."""
    skyvern_context.set(SkyvernContext(organization_id="o_1", workflow_run_id="wr_1", run_id="wr_1"))

    _fake_storage_app(
        monkeypatch,
        save=AsyncMock(side_effect=asyncio.CancelledError()),
        get=AsyncMock(return_value=[]),
    )
    _wire_block_runtime(monkeypatch)
    monkeypatch.setattr(
        CodeBlock,
        "execute_user_function_with_timeout",
        AsyncMock(return_value={"ok": True}),
    )

    finalize_calls: list[bool] = []
    original_finalize = block_module.CodeBlockActionRecording.finalize

    async def _spy_finalize(self, success: bool) -> None:
        finalize_calls.append(success)
        await original_finalize(self, success)

    monkeypatch.setattr(block_module.CodeBlockActionRecording, "finalize", _spy_finalize)

    block = CodeBlock(
        label="download_invoice",
        code="value = 'unused'",
        output_parameter=_output_parameter("code_out"),
    )
    with pytest.raises(asyncio.CancelledError):
        await block.execute(workflow_run_id="wr_1", workflow_run_block_id="", organization_id="o_1")

    # The first finalize wins; it must book the executed block successful, not failed.
    assert finalize_calls
    assert finalize_calls[0] is True


@pytest.mark.asyncio
async def test_failed_self_heal_still_binds_the_download_that_preceded_the_raise(
    monkeypatch: pytest.MonkeyPatch, _isolated_download_path: str
) -> None:
    """A heal that fired and lost keeps its failure verdict, but not the file's evidence."""
    skyvern_context.set(SkyvernContext(organization_id="o_1", workflow_run_id="wr_1", run_id="wr_1"))

    file_info = FileInfo(
        url="https://api.example.com/v1/artifacts/a_dl_1/content?artifact_name=invoice.pdf",
        filename="invoice.pdf",
        checksum="deadbeef",
        artifact_id="a_dl_1",
        modified_at=datetime(2026, 6, 14, 12, 0, tzinfo=UTC),
    )
    _fake_storage_app(monkeypatch, save=AsyncMock(), get=AsyncMock(side_effect=[[], [file_info]]))
    block_module.app.AGENT_FUNCTION.resolve_self_heal_api_key = AsyncMock(return_value=None)
    _wire_block_runtime(monkeypatch)
    monkeypatch.setattr(
        CodeBlock,
        "execute_user_function_with_timeout",
        AsyncMock(side_effect=_raise_after_download(_isolated_download_path, Exception("Download is starting"))),
    )
    monkeypatch.setattr(CodeBlock, "_self_heal_enabled", AsyncMock(return_value=True))
    monkeypatch.setattr(CodeBlock, "_is_healable_page_failure", lambda self, e, page, engine_selection: True)

    healed = BlockResult(
        success=False,
        output_parameter=_output_parameter("code_out"),
        output_parameter_value=None,
        status=BlockStatus.failed,
        failure_reason="heal fired and lost",
        workflow_run_block_id="",
    )
    monkeypatch.setattr(CodeBlock, "_attempt_self_heal", AsyncMock(return_value=healed))

    block = CodeBlock(
        label="download_invoice",
        code="value = 'unused'",
        output_parameter=_output_parameter("code_out"),
    )
    result = await block.execute(workflow_run_id="wr_1", workflow_run_block_id="", organization_id="o_1")

    assert result.success is False
    assert result.failure_reason == "heal fired and lost"
    output = result.output_parameter_value
    assert output is not None
    assert output["downloaded_files"] == [file_info.model_dump()]
    assert output["downloaded_file_urls"] == [file_info.url]


@pytest.mark.asyncio
async def test_unreachable_storage_leaves_the_authored_payload_alone(
    monkeypatch: pytest.MonkeyPatch, _isolated_download_path: str
) -> None:
    """With no host verdict, neither the claim nor its absence is proven, so nothing is asserted.

    Stripping here would delete a real download whenever storage is briefly unreachable; the strip
    belongs to the verdict that the host registered nothing, not to the absence of a verdict."""
    skyvern_context.set(SkyvernContext(organization_id="o_1", workflow_run_id="wr_1", run_id="wr_1"))

    _fake_storage_app(
        monkeypatch,
        save=AsyncMock(side_effect=asyncio.TimeoutError()),
        get=AsyncMock(side_effect=asyncio.TimeoutError()),
    )
    _wire_block_runtime(monkeypatch)

    async def _return_authored_registration(*args: object, **kwargs: object) -> object:
        return {"downloaded_file_urls": ["https://api.example.com/real.pdf"], "note": "sidecar"}

    monkeypatch.setattr(
        CodeBlock,
        "execute_user_function_with_timeout",
        AsyncMock(side_effect=_return_authored_registration),
    )

    block = CodeBlock(
        label="download_invoice",
        code="value = 'unused'",
        output_parameter=_output_parameter("code_out"),
    )
    result = await block.execute(workflow_run_id="wr_1", workflow_run_block_id="", organization_id="o_1")

    assert result.success is True
    assert result.output_parameter_value["downloaded_file_urls"] == ["https://api.example.com/real.pdf"]
    assert result.output_parameter_value["note"] == "sidecar"


@pytest.mark.asyncio
async def test_a_failing_block_registers_its_own_file_even_when_an_earlier_one_registered(
    monkeypatch: pytest.MonkeyPatch, _isolated_download_path: str
) -> None:
    """An earlier block's registration is not evidence for this block's file.

    Reading back a non-empty list says nothing about whether the file this block just added is in
    it, so the save cannot be skipped on that alone."""
    skyvern_context.set(SkyvernContext(organization_id="o_1", workflow_run_id="wr_1", run_id="wr_1"))

    earlier = FileInfo(
        url="https://api.example.com/v1/artifacts/a_dl_0/content?artifact_name=earlier.pdf",
        filename="earlier.pdf",
        checksum="beefdead",
        artifact_id="a_dl_0",
        modified_at=datetime(2026, 6, 13, 12, 0, tzinfo=UTC),
    )
    save_mock = AsyncMock()
    _fake_storage_app(monkeypatch, save=save_mock, get=AsyncMock(return_value=[earlier]))
    _wire_block_runtime(monkeypatch)
    monkeypatch.setattr(
        CodeBlock,
        "execute_user_function_with_timeout",
        AsyncMock(side_effect=_raise_after_download(_isolated_download_path, Exception("Download is starting"))),
    )

    block = CodeBlock(
        label="download_invoice",
        code="value = 'unused'",
        output_parameter=_output_parameter("code_out"),
    )
    await block.execute(workflow_run_id="wr_1", workflow_run_block_id="", organization_id="o_1")

    save_mock.assert_awaited()


@pytest.mark.asyncio
async def test_secure_runner_bound_output_reaches_the_block_row(
    monkeypatch: pytest.MonkeyPatch, _isolated_download_path: str
) -> None:
    """Download evidence is read from the block row, so the secure path must write it there.

    Returning the sidecar's own result would leave the row holding its pre-binding value."""
    skyvern_context.set(SkyvernContext(organization_id="o_1", workflow_run_id="wr_1", run_id="wr_1"))

    file_info = FileInfo(
        url="https://api.example.com/v1/artifacts/a_dl_1/content?artifact_name=invoice.pdf",
        filename="invoice.pdf",
        checksum="deadbeef",
        artifact_id="a_dl_1",
        modified_at=datetime(2026, 6, 14, 12, 0, tzinfo=UTC),
    )
    _fake_storage_app(monkeypatch, save=AsyncMock(), get=AsyncMock(side_effect=[[], [file_info]]))
    _wire_block_runtime(monkeypatch)
    _wire_secure_runner(
        monkeypatch,
        output={"downloaded_file_urls": ["file:///authored/by/the/code.pdf"], "note": "sidecar"},
        on_execute=lambda: _write_run_download(_isolated_download_path),
    )

    row_writes: list[object] = []
    original_build = CodeBlock.build_block_result

    async def _record_row_write(self, *args: object, **kwargs: object) -> BlockResult:
        row_writes.append(kwargs.get("output_parameter_value"))
        return await original_build(self, *args, **kwargs)

    monkeypatch.setattr(CodeBlock, "build_block_result", _record_row_write)

    block = CodeBlock(
        label="download_invoice",
        code=_SWALLOWED_DOWNLOAD_TERMINAL,
        output_parameter=_output_parameter("code_out"),
    )
    result = await block.execute(workflow_run_id="wr_1", workflow_run_block_id="", organization_id="o_1")

    assert result.success is True
    # build_block_result is the only writer of workflow_run_blocks.output.
    assert row_writes, "the secure success path never wrote the block row"
    row_output = row_writes[-1]
    assert row_output["downloaded_files"] == [file_info.model_dump()]
    assert row_output["downloaded_file_urls"] == [file_info.url]
    assert block_output_has_registered_download(row_output) is True
    assert row_output == result.output_parameter_value
