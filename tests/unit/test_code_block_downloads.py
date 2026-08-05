"""CodeBlock registering browser downloads into its block output (SKY-10937).

Covers the three acceptance behaviors:
1. A download-producing code block surfaces ``downloaded_files`` / ``downloaded_file_urls`` /
   ``downloaded_file_artifact_ids`` in its output so a downstream block can chain from it in the
   same run; a non-download code block's output is unchanged.
2. Inside a loop iteration, only the file produced this iteration attributes to the block.
3. A ``FILE_URL`` workflow parameter is materialized to a run-scoped local path usable by
   ``set_input_files``; an empty URI leaves the value untouched.
"""

import os
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from structlog.testing import capture_logs

from skyvern.exceptions import IllegitCompleteScriptTermination, ScriptTerminationException
from skyvern.forge.sdk.copilot.reached_download_target import code_is_download_intent
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
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
        get_value=lambda key: (values or {}).get(key),
        mask_secrets_in_data=lambda data, mask="*****": data,
    )
    monkeypatch.setattr(CodeBlock, "get_workflow_run_context", lambda self, workflow_run_id: context)
    monkeypatch.setattr(CodeBlock, "format_potential_template_parameters", lambda self, workflow_run_context: None)
    monkeypatch.setattr(CodeBlock, "record_output_parameter_value", AsyncMock())


def _wire_secure_runner(monkeypatch: pytest.MonkeyPatch, *, output: dict) -> None:
    """Route execute() down the secure sidecar arm, which returns before the inline directory diff."""
    block_result = BlockResult(
        success=True,
        output_parameter=_output_parameter("code_out"),
        output_parameter_value=output,
        status=BlockStatus.completed,
        workflow_run_block_id="",
    )
    fake_app = block_module.app
    fake_app.AGENT_FUNCTION.should_use_codeblock_runner = AsyncMock(return_value=True)
    fake_app.AGENT_FUNCTION.execute_code_block_override = AsyncMock(
        return_value=SimpleNamespace(block_result=block_result, failure=None)
    )


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
