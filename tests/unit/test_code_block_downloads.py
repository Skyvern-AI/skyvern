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
import hashlib
import json
import logging
import os
from collections.abc import Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import structlog
from structlog.testing import capture_logs

from skyvern.constants import BROWSER_DOWNLOADING_SUFFIX
from skyvern.exceptions import (
    DownloadFileMaxWaitingTime,
    DownloadSaveIncompleteError,
    IllegitCompleteScriptTermination,
    ScriptTerminationException,
)
from skyvern.forge.agent_functions import AgentFunction
from skyvern.forge.sdk.api.files import classify_download_visibility, observe_download_dir
from skyvern.forge.sdk.artifact.storage import s3 as s3_module
from skyvern.forge.sdk.artifact.storage.s3 import S3Storage
from skyvern.forge.sdk.browser_network_egress_monitor import BrowserNetworkEgressMonitor
from skyvern.forge.sdk.copilot.reached_download_target import (
    block_output_has_registered_download,
    code_is_download_intent,
)
from skyvern.forge.sdk.copilot.tools.run_execution import build_test_evidence_packet
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.http_request_authorization import RunScopedRedirectHopAuthorizer
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
from skyvern.webeye.browser_artifacts import BrowserArtifacts, DownloadBinding
from skyvern.webeye.cdp_download_interceptor import CDPDownloadInterceptor
from tests.unit.copilot_test_helpers import make_copilot_ctx
from tests.unit.scoped_asyncio import ScopedAsyncio

_BLOCK_CREATED_AT = datetime(2026, 6, 14, 11, 0, tzinfo=UTC)


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
    download_binding: DownloadBinding = DownloadBinding.RUN_DIR,
) -> SimpleNamespace:
    page = SimpleNamespace(context=SimpleNamespace(), url="https://example.test/")
    browser_state = SimpleNamespace(
        get_working_page=AsyncMock(return_value=page),
        browser_artifacts=BrowserArtifacts(download_binding=download_binding),
    )
    monkeypatch.setattr(CodeBlock, "get_or_create_browser_state", AsyncMock(return_value=browser_state))

    context = SimpleNamespace(
        organization_id="o_1",
        workflow=workflow,
        workflow_permanent_id="wpid_test",
        workflow_id="w_test",
        secrets={},
        get_value=lambda key: (values or {}).get(key),
        mask_secrets_in_data=lambda data, mask="*****": data,
    )
    monkeypatch.setattr(CodeBlock, "get_workflow_run_context", lambda self, workflow_run_id: context)
    monkeypatch.setattr(CodeBlock, "format_potential_template_parameters", lambda self, workflow_run_context: None)
    monkeypatch.setattr(CodeBlock, "record_output_parameter_value", AsyncMock())
    return page


def _wire_secure_runner(
    monkeypatch: pytest.MonkeyPatch,
    *,
    output: dict,
    on_execute: Callable[[], None] | None = None,
    downgrade: bool = False,
    download_operation_invoked: bool = False,
) -> None:
    """Route execute() down the secure sidecar arm, whose returned payload is what the host binds.

    ``downgrade`` returns no result from the override, the one way the runner arm falls through to
    inline execution."""
    block_result = BlockResult(
        success=True,
        output_parameter=_output_parameter("code_out"),
        output_parameter_value=output,
        status=BlockStatus.completed,
        workflow_run_block_id="",
    )

    async def _execute_override(**kwargs: object) -> SimpleNamespace | None:
        if on_execute is not None:
            on_execute()
        if downgrade:
            return None
        return SimpleNamespace(
            block_result=block_result,
            failure=None,
            download_operation_receipt=object() if download_operation_invoked else None,
        )

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


def _arm_delayed_download(
    page: SimpleNamespace,
    download_root: str,
    execution_finished: asyncio.Event,
) -> asyncio.Event:
    interceptor = CDPDownloadInterceptor(
        output_dir=os.path.join(download_root, "wr_1"),
        network_egress_monitor=BrowserNetworkEgressMonitor.unenrolled(),
        redirect_hop_authorizer=RunScopedRedirectHopAuthorizer("wr_1"),
    )
    page.context._skyvern_cdp_download_interceptor = interceptor
    transfer_finished = asyncio.Event()

    async def land_after_execution() -> None:
        await execution_finished.wait()
        await asyncio.sleep(0)
        _write_run_download(download_root)
        transfer_finished.set()

    interceptor._browser_download_generation += 1
    task = asyncio.create_task(land_after_execution())
    interceptor._browser_download_tasks.add(task)
    task.add_done_callback(interceptor._browser_download_done)
    return transfer_finished


def _persisted_output() -> object:
    return CodeBlock.record_output_parameter_value.await_args.args[2]


def _fake_storage_app(
    monkeypatch: pytest.MonkeyPatch,
    *,
    save,
    get,
    claim: AsyncMock | None = None,
    in_flight: AsyncMock | None = None,
) -> SimpleNamespace:
    agent_function = AgentFunction()
    fake_app = SimpleNamespace(
        DATABASE=SimpleNamespace(
            artifacts=SimpleNamespace(
                claim_session_download_artifacts_for_run=claim or AsyncMock(return_value=0),
            ),
            observer=SimpleNamespace(
                get_workflow_run_block=AsyncMock(
                    return_value=SimpleNamespace(created_at=_BLOCK_CREATED_AT),
                ),
            ),
        ),
        STORAGE=SimpleNamespace(
            save_downloaded_files=save,
            get_downloaded_files=get,
            list_downloading_files_in_browser_session=in_flight or AsyncMock(return_value=[]),
        ),
        AGENT_FUNCTION=SimpleNamespace(
            validate_code_block=AsyncMock(),
            # Secure CodeBlock runner gating — match the OSS base no-op so execute() runs legacy.
            should_use_codeblock_runner=AsyncMock(return_value=False),
            execute_code_block_override=AsyncMock(return_value=None),
            serialize_codeblock_parameters=agent_function.serialize_codeblock_parameters,
            redact_codeblock_parameter_values=agent_function.redact_codeblock_parameter_values,
            prepare_codeblock_control_flow_exception=agent_function.prepare_codeblock_control_flow_exception,
        ),
    )
    monkeypatch.setattr(block_module, "app", fake_app)
    return fake_app


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
async def test_adopted_in_process_download_settles_and_registers(
    monkeypatch: pytest.MonkeyPatch, _isolated_download_path: str
) -> None:
    skyvern_context.set(SkyvernContext(organization_id="o_1", workflow_run_id="wr_1", run_id="wr_1"))
    payload = b"%PDF-1.4 statement"
    checksum = hashlib.sha256(payload).hexdigest()
    file_info = FileInfo(
        url="https://api.example.com/v1/artifacts/a_dl_1/content?artifact_name=invoice.pdf",
        filename="invoice.pdf",
        checksum=checksum,
        file_size=len(payload),
        artifact_id="a_dl_1",
    )
    execution_finished = asyncio.Event()

    async def save_downloaded_files(**_kwargs: object) -> None:
        assert transfer_finished.is_set()
        run_dir = os.path.join(_isolated_download_path, "wr_1")
        assert os.listdir(run_dir) == ["invoice.pdf"]
        with open(os.path.join(run_dir, "invoice.pdf"), "rb") as handle:
            stored_bytes = handle.read()
        assert stored_bytes == payload
        assert hashlib.sha256(stored_bytes).hexdigest() == checksum
        assert len(stored_bytes) == file_info.file_size

    _fake_storage_app(
        monkeypatch,
        save=save_downloaded_files,
        get=AsyncMock(side_effect=[[], [file_info]]),
    )
    page = _wire_block_runtime(monkeypatch)
    block = CodeBlock(
        label="download_invoice",
        code="saved = 'ok'",
        output_parameter=_output_parameter("code_out"),
    )
    transfer_finished = _arm_delayed_download(page, _isolated_download_path, execution_finished)
    execute_user_function = CodeBlock.execute_user_function_with_timeout

    async def execute_and_mark_finished(user_function, timeout_seconds):
        try:
            return await execute_user_function(user_function, timeout_seconds)
        finally:
            execution_finished.set()

    monkeypatch.setattr(CodeBlock, "execute_user_function_with_timeout", staticmethod(execute_and_mark_finished))

    result = await block.execute(workflow_run_id="wr_1", workflow_run_block_id="", organization_id="o_1")

    assert result.success is True
    assert downloaded_file_count_from_output(result.output_parameter_value) == 1
    assert result.output_parameter_value["downloaded_files"] == [file_info.model_dump()]


@pytest.mark.asyncio
async def test_in_process_settlement_timeout_preserves_success_and_registers(
    monkeypatch: pytest.MonkeyPatch, _isolated_download_path: str
) -> None:
    skyvern_context.set(SkyvernContext(organization_id="o_1", workflow_run_id="wr_1", run_id="wr_1"))
    save_downloaded_files = AsyncMock()
    _fake_storage_app(monkeypatch, save=save_downloaded_files, get=AsyncMock(return_value=[]))
    _wire_block_runtime(monkeypatch)

    @asynccontextmanager
    async def never_settles(_browser_context: object):
        await asyncio.Event().wait()
        yield

    monkeypatch.setattr(block_module, "settle_browser_downloads_for_context", never_settles)
    monkeypatch.setattr(block_module, "SAVE_DOWNLOADED_FILES_TIMEOUT", 0.01)
    monkeypatch.setattr(CodeBlock, "execute_user_function_with_timeout", AsyncMock(return_value={"ok": True}))

    block = CodeBlock(
        label="code_download",
        code="saved = 'ok'",
        output_parameter=_output_parameter("code_out"),
    )
    result = await asyncio.wait_for(
        block.execute(workflow_run_id="wr_1", workflow_run_block_id="", organization_id="o_1"),
        timeout=1,
    )

    assert result.success is True
    assert result.output_parameter_value["ok"] is True
    save_downloaded_files.assert_awaited()


@pytest.mark.asyncio
async def test_adopted_in_process_declared_error_settles_before_failure_registration(
    monkeypatch: pytest.MonkeyPatch, _isolated_download_path: str
) -> None:
    skyvern_context.set(SkyvernContext(organization_id="o_1", workflow_run_id="wr_1", run_id="wr_1"))
    file_info = FileInfo(
        url="https://api.example.com/v1/artifacts/a_dl_1/content?artifact_name=invoice.pdf",
        filename="invoice.pdf",
        checksum="deadbeef",
        file_size=len(b"%PDF-1.4 statement"),
        artifact_id="a_dl_1",
    )
    execution_finished = asyncio.Event()

    async def save_downloaded_files(**_kwargs: object) -> None:
        assert transfer_finished.is_set()

    _fake_storage_app(
        monkeypatch,
        save=save_downloaded_files,
        get=AsyncMock(side_effect=[[], [file_info]]),
    )
    page = _wire_block_runtime(monkeypatch)
    block = CodeBlock(
        label="download_invoice",
        code="raise ErrorCode('report_unavailable', 'report generation failed')",
        output_parameter=_output_parameter("code_out"),
        error_code_mapping={"report_unavailable": "The report could not be generated"},
    )
    transfer_finished = _arm_delayed_download(page, _isolated_download_path, execution_finished)
    execute_user_function = CodeBlock.execute_user_function_with_timeout

    async def execute_and_mark_finished(user_function, timeout_seconds):
        try:
            return await execute_user_function(user_function, timeout_seconds)
        finally:
            execution_finished.set()

    monkeypatch.setattr(CodeBlock, "execute_user_function_with_timeout", staticmethod(execute_and_mark_finished))

    result = await block.execute(workflow_run_id="wr_1", workflow_run_block_id="", organization_id="o_1")

    assert result.success is False
    assert result.failure_reason == "report generation failed"
    assert result.error_codes == ["report_unavailable"]
    assert downloaded_file_count_from_output(result.output_parameter_value) == 1
    assert result.output_parameter_value["downloaded_files"] == [file_info.model_dump()]


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
    assert result.failure_reason == "CodeBlock terminated."


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
async def test_downgraded_secure_runner_hands_inline_an_inline_labelled_evidence_probe(
    monkeypatch: pytest.MonkeyPatch, _isolated_download_path: str
) -> None:
    """A runner that declines leaves the block running inline, so the probe it handed on must
    register under the engine that actually executed."""
    skyvern_context.set(SkyvernContext(organization_id="o_1", workflow_run_id="wr_1", run_id="wr_1"))

    _fake_storage_app(monkeypatch, save=AsyncMock(), get=AsyncMock(return_value=[]))
    _wire_block_runtime(monkeypatch)
    _wire_secure_runner(monkeypatch, output={"downloaded_files": []}, downgrade=True)

    handed_off: dict[str, object] = {}
    generate = CodeBlock.generate_async_user_function

    def _spy(self: CodeBlock, *args: object, **kwargs: object) -> object:
        handed_off["probe"] = kwargs.get("download_evidence")
        return generate(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(CodeBlock, "generate_async_user_function", _spy)

    block = CodeBlock(
        label="download_invoice",
        code='return {"downloaded_files": []}',
        output_parameter=_output_parameter("code_out"),
    )
    await block.execute(workflow_run_id="wr_1", workflow_run_block_id="", organization_id="o_1")

    probe = handed_off["probe"]
    assert callable(probe)
    with capture_logs() as logs:
        await probe()

    engines = [
        entry.get("engine") for entry in logs if entry.get("event") == "codeblock.download_registration_visibility"
    ]
    assert engines == ["inline"]


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
    assert result.failure_reason == "Failed to execute code block. Reason: RuntimeError: Download is starting"
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
    assert result.failure_reason == "Failed to execute code block. Reason: RuntimeError: boom after overwrite"
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
    assert result.failure_reason == "Failed to execute code block. Reason: RuntimeError: boom after overwrite"
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


def test_authored_registration_keys_are_dropped_one_level_down_too() -> None:
    """The completion grader reads a nested ``output`` mapping as well as the root, so a claim
    parked one level down reads as a registration just as convincingly as one at the root."""
    forged = {
        "output": {
            "downloaded_files": [{"filename": "invoice.pdf"}],
            "downloaded_file_urls": ["file:///tmp/invoice.pdf"],
            "kept": "value",
        },
        "note": "model wrote these itself",
    }

    bound = block_module.bind_downloaded_files_to_output(forged, [])

    assert bound["output"] == {"kept": "value"}
    assert bound["note"] == "model wrote these itself"
    assert block_output_has_registered_download(bound["output"]) is False
    assert block_output_has_registered_download(bound) is False


def test_nested_schema_placeholders_survive_the_drop() -> None:
    """An empty or None field is schema a template may dereference, not a claim to drop."""
    schema_only = {"output": {"downloaded_files": [], "downloaded_file_urls": None, "kept": "value"}}

    bound = block_module.bind_downloaded_files_to_output(schema_only, [])

    assert bound["output"] == {"downloaded_files": [], "downloaded_file_urls": None, "kept": "value"}


_SESSION_FILE = FileInfo(
    url="https://api.example.com/v1/artifacts/a_dl_9/content?artifact_name=session.pdf",
    filename="session.pdf",
    checksum="cafebabe",
    artifact_id="a_dl_9",
    modified_at=datetime(2026, 6, 14, 12, 0, tzinfo=UTC),
)


def _session_context() -> SkyvernContext:
    return SkyvernContext(
        organization_id="o_1",
        workflow_run_id="wr_1",
        run_id="wr_1",
        browser_session_id="pbs_1",
    )


def _artifact_first_downloads(monkeypatch: pytest.MonkeyPatch, *, enabled: bool) -> None:
    monkeypatch.setattr(block_module.settings, "ARTIFACT_CONTENT_HMAC_KEYRING", "k1:secret" if enabled else None)


def _claim_gated_read(claim: AsyncMock, *, before: list[FileInfo], after: list[FileInfo]) -> AsyncMock:
    """A run-scoped read that surfaces a session-keyed artifact only once the run has claimed it,
    matching the ``run_id`` NULL row nothing keyed to the run can list until the claim tags it."""

    async def _get(**_kwargs: object) -> list[FileInfo]:
        return list(after) if claim.await_count else list(before)

    return AsyncMock(side_effect=_get)


@pytest.mark.asyncio
async def test_session_download_is_claimed_into_the_block_output(
    monkeypatch: pytest.MonkeyPatch, _isolated_download_path: str
) -> None:
    """A code block downloading on an adopted session sees its own file in its output."""
    skyvern_context.set(_session_context())

    claim = AsyncMock(return_value=1)
    read = _claim_gated_read(claim, before=[], after=[_SESSION_FILE])
    fake_app = _fake_storage_app(monkeypatch, save=AsyncMock(), get=read, claim=claim)
    _artifact_first_downloads(monkeypatch, enabled=True)
    _wire_block_runtime(monkeypatch, download_binding=DownloadBinding.SESSION_DIR)

    block = CodeBlock(
        label="download_statement",
        code="saved = 'ok'",
        output_parameter=_output_parameter("code_out"),
    )
    result = await block.execute(workflow_run_id="wr_1", workflow_run_block_id="", organization_id="o_1")

    assert result.success is True
    output = result.output_parameter_value
    assert output["downloaded_file_urls"] == [_SESSION_FILE.url]
    assert output["downloaded_file_artifact_ids"] == ["a_dl_9"]
    # A downstream block in the same run reads the persisted output parameter, not the return value.
    assert _persisted_output() == output
    claim_kwargs = claim.await_args.kwargs
    assert claim_kwargs["browser_session_id"] == "pbs_1"
    assert claim_kwargs["run_started_at"] == _BLOCK_CREATED_AT
    assert claim_kwargs["run_id"] == read.await_args.kwargs["run_id"]
    fake_app.DATABASE.observer.get_workflow_run_block.assert_awaited()


@pytest.mark.asyncio
async def test_second_session_downloader_claims_its_own_file(
    monkeypatch: pytest.MonkeyPatch, _isolated_download_path: str
) -> None:
    """An earlier block's registration is not evidence for this one's file, so the claim still runs."""
    skyvern_context.set(_session_context())

    earlier = FileInfo(
        url="https://api.example.com/v1/artifacts/a_dl_0/content?artifact_name=earlier.pdf",
        filename="earlier.pdf",
        checksum="beefdead",
        artifact_id="a_dl_0",
        modified_at=datetime(2026, 6, 13, 12, 0, tzinfo=UTC),
    )
    claim = AsyncMock(return_value=1)
    read = _claim_gated_read(claim, before=[earlier], after=[earlier, _SESSION_FILE])
    _fake_storage_app(monkeypatch, save=AsyncMock(), get=read, claim=claim)
    _artifact_first_downloads(monkeypatch, enabled=True)
    _wire_block_runtime(monkeypatch, download_binding=DownloadBinding.SESSION_DIR)

    block = CodeBlock(
        label="download_statement",
        code="saved = 'ok'",
        output_parameter=_output_parameter("code_out"),
    )
    result = await block.execute(workflow_run_id="wr_1", workflow_run_block_id="", organization_id="o_1")

    assert result.success is True
    assert _SESSION_FILE.url in result.output_parameter_value["downloaded_file_urls"]


@pytest.mark.asyncio
async def test_run_scoped_lane_never_claims(monkeypatch: pytest.MonkeyPatch, _isolated_download_path: str) -> None:
    """A freshly minted, run-scoped session behaves exactly as before: no claim, no run lookup."""
    skyvern_context.set(SkyvernContext(organization_id="o_1", workflow_run_id="wr_1", run_id="wr_1"))

    file_info = FileInfo(
        url="https://api.example.com/v1/artifacts/a_dl_1/content?artifact_name=invoice.pdf",
        filename="invoice.pdf",
        checksum="deadbeef",
        artifact_id="a_dl_1",
        modified_at=datetime(2026, 6, 14, 12, 0, tzinfo=UTC),
    )
    claim = AsyncMock(return_value=0)
    fake_app = _fake_storage_app(
        monkeypatch,
        save=AsyncMock(),
        get=AsyncMock(side_effect=[[], [file_info]]),
        claim=claim,
    )
    _wire_block_runtime(monkeypatch, download_binding=DownloadBinding.RUN_DIR)

    block = CodeBlock(
        label="code_download",
        code="saved = 'ok'",
        output_parameter=_output_parameter("code_out"),
    )
    result = await block.execute(workflow_run_id="wr_1", workflow_run_block_id="", organization_id="o_1")

    claim.assert_not_awaited()
    fake_app.DATABASE.observer.get_workflow_run_block.assert_not_awaited()
    output = result.output_parameter_value
    assert output["saved"] == "ok"
    assert output["downloaded_files"] == [file_info.model_dump()]
    assert output["downloaded_file_urls"] == [file_info.url]
    assert output["downloaded_file_artifact_ids"] == ["a_dl_1"]


@pytest.mark.asyncio
async def test_failing_session_block_still_carries_its_download(
    monkeypatch: pytest.MonkeyPatch, _isolated_download_path: str
) -> None:
    """Nothing reaches the run directory on a session binding, so an empty diff cannot end the lane."""
    skyvern_context.set(_session_context())

    claim = AsyncMock(return_value=1)
    read = _claim_gated_read(claim, before=[], after=[_SESSION_FILE])
    _fake_storage_app(monkeypatch, save=AsyncMock(), get=read, claim=claim)
    _artifact_first_downloads(monkeypatch, enabled=True)
    _wire_block_runtime(monkeypatch, download_binding=DownloadBinding.SESSION_DIR)
    monkeypatch.setattr(
        CodeBlock,
        "execute_user_function_with_timeout",
        AsyncMock(side_effect=RuntimeError("Download is starting")),
    )

    block = CodeBlock(
        label="download_statement",
        code="value = 'unused'",
        output_parameter=_output_parameter("code_out"),
    )
    result = await block.execute(workflow_run_id="wr_1", workflow_run_block_id="", organization_id="o_1")

    assert result.success is False
    claim.assert_awaited()
    output = result.output_parameter_value
    assert output is not None
    assert output["downloaded_file_urls"] == [_SESSION_FILE.url]


@pytest.mark.asyncio
async def test_session_lane_is_inert_without_artifact_content_signing(
    monkeypatch: pytest.MonkeyPatch, _isolated_download_path: str
) -> None:
    """Without HMAC signing the run-scoped read cannot see a session-keyed row, so the whole lane
    stays off: no claim, no block-row read, and the failure output is what it was before."""
    skyvern_context.set(_session_context())

    claim = AsyncMock(return_value=1)
    save_mock = AsyncMock()
    fake_app = _fake_storage_app(monkeypatch, save=save_mock, get=AsyncMock(return_value=[]), claim=claim)
    _artifact_first_downloads(monkeypatch, enabled=False)
    _wire_block_runtime(monkeypatch, download_binding=DownloadBinding.SESSION_DIR)
    monkeypatch.setattr(
        CodeBlock,
        "execute_user_function_with_timeout",
        AsyncMock(side_effect=RuntimeError("Download is starting")),
    )

    block = CodeBlock(
        label="download_statement",
        code="value = 'unused'",
        output_parameter=_output_parameter("code_out"),
    )
    result = await block.execute(workflow_run_id="wr_1", workflow_run_block_id="", organization_id="o_1")

    assert result.success is False
    claim.assert_not_awaited()
    fake_app.DATABASE.observer.get_workflow_run_block.assert_not_awaited()
    save_mock.assert_not_awaited()
    assert result.output_parameter_value is None


@pytest.mark.asyncio
async def test_secure_runner_session_download_reaches_registration(
    monkeypatch: pytest.MonkeyPatch, _isolated_download_path: str
) -> None:
    """A session binding always leaves the run directory empty, so the sidecar arm's coverage check
    cannot be what decides whether registration runs."""
    skyvern_context.set(_session_context())

    claim = AsyncMock(return_value=1)
    read = _claim_gated_read(claim, before=[], after=[_SESSION_FILE])
    save_mock = AsyncMock()
    _fake_storage_app(monkeypatch, save=save_mock, get=read, claim=claim)
    _artifact_first_downloads(monkeypatch, enabled=True)
    _wire_block_runtime(monkeypatch, download_binding=DownloadBinding.SESSION_DIR)
    _wire_secure_runner(monkeypatch, output={"status": "ok"})

    block = CodeBlock(
        label="download_statement",
        code="saved = 'ok'",
        output_parameter=_output_parameter("code_out"),
    )
    result = await block.execute(workflow_run_id="wr_1", workflow_run_block_id="", organization_id="o_1")

    save_mock.assert_awaited()
    claim.assert_awaited()
    assert result.success is True
    assert result.output_parameter_value["downloaded_file_urls"] == [_SESSION_FILE.url]


@pytest.mark.asyncio
async def test_secure_runner_typed_download_waits_for_delayed_final_session_row(
    monkeypatch: pytest.MonkeyPatch, _isolated_download_path: str
) -> None:
    """A completed typed download is sufficient reason to settle a delayed final watcher row even
    after its partial marker has disappeared. The first empty read must not end the custody handoff."""
    skyvern_context.set(_session_context())

    # Baseline read, host pre-registration read, and first registration read are empty. The
    # watcher row becomes visible only to the receipt-gated settle attempt.
    reads = [[], [], [], [_SESSION_FILE]]

    async def _get(**_kwargs: object) -> list[FileInfo]:
        return reads.pop(0) if len(reads) > 1 else list(reads[0])

    claim = AsyncMock(return_value=1)
    in_flight = AsyncMock(return_value=[])
    _fake_storage_app(
        monkeypatch,
        save=AsyncMock(),
        get=AsyncMock(side_effect=_get),
        claim=claim,
        in_flight=in_flight,
    )
    _artifact_first_downloads(monkeypatch, enabled=True)
    _wire_block_runtime(monkeypatch, download_binding=DownloadBinding.SESSION_DIR)
    _wire_secure_runner(
        monkeypatch,
        output={"status": "ok"},
        download_operation_invoked=True,
    )

    block = CodeBlock(
        label="download_statement",
        code='await click_and_claim_download(page, "a.download")',
        output_parameter=_output_parameter("code_out"),
    )
    result = await block.execute(workflow_run_id="wr_1", workflow_run_block_id="", organization_id="o_1")

    in_flight.assert_awaited_once()
    assert claim.await_count == 2
    assert result.success is True
    assert result.output_parameter_value["downloaded_file_urls"] == [_SESSION_FILE.url]
    assert result.output_parameter_value["downloaded_file_artifact_ids"] == [_SESSION_FILE.artifact_id]
    persisted = _persisted_output()
    assert isinstance(persisted, dict)
    assert persisted["downloaded_file_artifact_ids"] == [_SESSION_FILE.artifact_id]

    packet = build_test_evidence_packet(
        make_copilot_ctx(),
        {
            "ok": True,
            "data": {
                "workflow_run_id": "wr_1",
                "overall_status": "completed",
                "requested_block_labels": ["download_statement"],
                "executed_block_labels": ["download_statement"],
                "blocks": [
                    {
                        "label": "download_statement",
                        "status": "completed",
                        "extracted_data": persisted,
                    }
                ],
            },
        },
    )
    assert [download.artifact_id for download in packet.downloads] == [_SESSION_FILE.artifact_id]


@pytest.mark.asyncio
async def test_secure_runner_without_typed_download_does_not_settle_an_empty_session_row(
    monkeypatch: pytest.MonkeyPatch, _isolated_download_path: str
) -> None:
    skyvern_context.set(_session_context())

    sleep = AsyncMock()
    monkeypatch.setattr(block_module, "asyncio", ScopedAsyncio(sleep=sleep))
    claim = AsyncMock(return_value=0)
    _fake_storage_app(
        monkeypatch,
        save=AsyncMock(),
        get=AsyncMock(return_value=[]),
        claim=claim,
        in_flight=AsyncMock(return_value=[]),
    )
    _artifact_first_downloads(monkeypatch, enabled=True)
    _wire_block_runtime(monkeypatch, download_binding=DownloadBinding.SESSION_DIR)
    _wire_secure_runner(monkeypatch, output={"status": "ok"})

    block = CodeBlock(
        label="read_page",
        code="return {'status': 'ok'}",
        output_parameter=_output_parameter("code_out"),
    )
    result = await block.execute(workflow_run_id="wr_1", workflow_run_block_id="", organization_id="o_1")

    sleep.assert_not_awaited()
    assert claim.await_count == 1
    assert result.success is True
    assert "downloaded_file_artifact_ids" not in result.output_parameter_value


@pytest.mark.asyncio
async def test_secure_runner_typed_download_with_no_final_row_is_bounded_and_invents_nothing(
    monkeypatch: pytest.MonkeyPatch, _isolated_download_path: str
) -> None:
    skyvern_context.set(_session_context())

    sleep = AsyncMock()
    monkeypatch.setattr(block_module, "asyncio", ScopedAsyncio(sleep=sleep))
    claim = AsyncMock(return_value=0)
    _fake_storage_app(
        monkeypatch,
        save=AsyncMock(),
        get=AsyncMock(return_value=[]),
        claim=claim,
        in_flight=AsyncMock(return_value=[]),
    )
    _artifact_first_downloads(monkeypatch, enabled=True)
    _wire_block_runtime(monkeypatch, download_binding=DownloadBinding.SESSION_DIR)
    _wire_secure_runner(
        monkeypatch,
        output={"status": "ok"},
        download_operation_invoked=True,
    )

    block = CodeBlock(
        label="download_statement",
        code='await click_and_claim_download(page, "a.download")',
        output_parameter=_output_parameter("code_out"),
    )
    result = await block.execute(workflow_run_id="wr_1", workflow_run_block_id="", organization_id="o_1")

    assert claim.await_count == 1 + block_module._CODE_BLOCK_SESSION_DOWNLOAD_SETTLE_ATTEMPTS
    assert sleep.await_count == block_module._CODE_BLOCK_SESSION_DOWNLOAD_SETTLE_ATTEMPTS
    assert result.success is True
    assert "downloaded_file_artifact_ids" not in result.output_parameter_value


@pytest.mark.asyncio
async def test_in_flight_partial_is_never_bound_as_the_users_file(
    monkeypatch: pytest.MonkeyPatch, _isolated_download_path: str
) -> None:
    """Claiming mid-run can tag a row the browser is still writing; a `.crdownload` partial must
    not reach downloaded_file_urls, where it would present truncated bytes as the finished file."""
    skyvern_context.set(_session_context())

    partial = FileInfo(
        url="https://api.example.com/v1/artifacts/a_dl_p/content?artifact_name=session.pdf.crdownload",
        filename="session.pdf.crdownload",
        checksum=None,
        artifact_id="a_dl_p",
        modified_at=datetime(2026, 6, 14, 12, 0, tzinfo=UTC),
    )
    claim = AsyncMock(return_value=1)
    read = _claim_gated_read(claim, before=[], after=[partial, _SESSION_FILE])
    _fake_storage_app(monkeypatch, save=AsyncMock(), get=read, claim=claim)
    _artifact_first_downloads(monkeypatch, enabled=True)
    _wire_block_runtime(monkeypatch, download_binding=DownloadBinding.SESSION_DIR)

    block = CodeBlock(
        label="download_statement",
        code="saved = 'ok'",
        output_parameter=_output_parameter("code_out"),
    )
    result = await block.execute(workflow_run_id="wr_1", workflow_run_block_id="", organization_id="o_1")

    assert result.success is True
    output = result.output_parameter_value
    assert output["downloaded_file_urls"] == [_SESSION_FILE.url]
    assert output["downloaded_file_artifact_ids"] == ["a_dl_9"]


@pytest.mark.asyncio
async def test_run_dir_block_carrying_a_session_id_never_claims(
    monkeypatch: pytest.MonkeyPatch, _isolated_download_path: str
) -> None:
    """A run-directory binding is the lane even when a browser session is attached, so the claim
    must key on the binding: claiming here would pull a co-tenant session's downloads into this run."""
    skyvern_context.set(_session_context())

    file_info = FileInfo(
        url="https://api.example.com/v1/artifacts/a_dl_2/content?artifact_name=invoice.pdf",
        filename="invoice.pdf",
        checksum="deadbeef",
        artifact_id="a_dl_2",
        modified_at=datetime(2026, 6, 14, 12, 0, tzinfo=UTC),
    )
    claim = AsyncMock(return_value=1)
    fake_app = _fake_storage_app(
        monkeypatch,
        save=AsyncMock(),
        get=AsyncMock(side_effect=[[], [file_info]]),
        claim=claim,
    )
    _artifact_first_downloads(monkeypatch, enabled=True)
    _wire_block_runtime(monkeypatch, download_binding=DownloadBinding.RUN_DIR)

    block = CodeBlock(
        label="code_download",
        code="saved = 'ok'",
        output_parameter=_output_parameter("code_out"),
    )
    result = await block.execute(workflow_run_id="wr_1", workflow_run_block_id="", organization_id="o_1")

    claim.assert_not_awaited()
    fake_app.DATABASE.observer.get_workflow_run_block.assert_not_awaited()
    assert result.output_parameter_value["downloaded_file_urls"] == [file_info.url]


@pytest.mark.asyncio
async def test_partial_only_window_waits_for_the_file_to_land(
    monkeypatch: pytest.MonkeyPatch, _isolated_download_path: str
) -> None:
    """A download still in flight when the block returns leaves only its partial row, which the
    read-back filters out; the block waits for the real file rather than reporting nothing."""
    skyvern_context.set(_session_context())

    partial = FileInfo(
        url="https://api.example.com/v1/artifacts/a_dl_p/content?artifact_name=session.pdf.crdownload",
        filename="session.pdf.crdownload",
        checksum=None,
        artifact_id="a_dl_p",
        modified_at=datetime(2026, 6, 14, 12, 0, tzinfo=UTC),
    )
    reads = [[partial], [], [_SESSION_FILE]]

    async def _get(**_kwargs: object) -> list[FileInfo]:
        return reads.pop(0) if len(reads) > 1 else list(reads[0])

    in_flight = AsyncMock(return_value=["s3://bucket/session.pdf.crdownload"])
    wait = AsyncMock()
    monkeypatch.setattr(block_module, "wait_for_download_finished", wait)
    _fake_storage_app(
        monkeypatch,
        save=AsyncMock(),
        get=AsyncMock(side_effect=_get),
        claim=AsyncMock(return_value=1),
        in_flight=in_flight,
    )
    _artifact_first_downloads(monkeypatch, enabled=True)
    _wire_block_runtime(monkeypatch, download_binding=DownloadBinding.SESSION_DIR)

    block = CodeBlock(
        label="download_statement",
        code="saved = 'ok'",
        output_parameter=_output_parameter("code_out"),
    )
    result = await block.execute(workflow_run_id="wr_1", workflow_run_block_id="", organization_id="o_1")

    wait.assert_awaited()
    assert result.success is True
    assert result.output_parameter_value["downloaded_file_urls"] == [_SESSION_FILE.url]


@pytest.mark.asyncio
async def test_download_that_never_lands_falls_through_to_finalization(
    monkeypatch: pytest.MonkeyPatch, _isolated_download_path: str
) -> None:
    """A download that never finishes must not hang or fail the block; the run's finalization claim
    stays the backstop, so the block records what it has and moves on."""
    skyvern_context.set(_session_context())

    in_flight = AsyncMock(return_value=["s3://bucket/session.pdf.crdownload"])
    wait = AsyncMock(side_effect=DownloadFileMaxWaitingTime(downloading_files=["session.pdf.crdownload"]))
    monkeypatch.setattr(block_module, "wait_for_download_finished", wait)
    monkeypatch.setattr(block_module, "asyncio", ScopedAsyncio(sleep=AsyncMock()))
    _fake_storage_app(
        monkeypatch,
        save=AsyncMock(),
        get=AsyncMock(return_value=[]),
        claim=AsyncMock(return_value=1),
        in_flight=in_flight,
    )
    _artifact_first_downloads(monkeypatch, enabled=True)
    _wire_block_runtime(monkeypatch, download_binding=DownloadBinding.SESSION_DIR)

    block = CodeBlock(
        label="download_statement",
        code="saved = 'ok'",
        output_parameter=_output_parameter("code_out"),
    )
    result = await block.execute(workflow_run_id="wr_1", workflow_run_block_id="", organization_id="o_1")

    wait.assert_awaited()
    assert result.success is True
    assert result.output_parameter_value["saved"] == "ok"


@pytest.mark.asyncio
async def test_failing_session_block_that_never_downloaded_invents_no_evidence(
    monkeypatch: pytest.MonkeyPatch, _isolated_download_path: str
) -> None:
    """A session binding leaves no local diff to gate on, so every failing block on a reused session
    consults storage. It must come back with nothing rather than borrowing another block's file."""
    skyvern_context.set(_session_context())

    claim = AsyncMock(return_value=0)
    save_mock = AsyncMock()
    _fake_storage_app(monkeypatch, save=save_mock, get=AsyncMock(return_value=[]), claim=claim)
    _artifact_first_downloads(monkeypatch, enabled=True)
    _wire_block_runtime(monkeypatch, download_binding=DownloadBinding.SESSION_DIR)
    monkeypatch.setattr(
        CodeBlock,
        "execute_user_function_with_timeout",
        AsyncMock(side_effect=RuntimeError("unrelated failure")),
    )

    block = CodeBlock(
        label="not_a_download",
        code="value = 1 / 0",
        output_parameter=_output_parameter("code_out"),
    )
    result = await block.execute(workflow_run_id="wr_1", workflow_run_block_id="", organization_id="o_1")

    assert result.success is False
    claim.assert_awaited()
    output = result.output_parameter_value
    assert output is None or not output.get("downloaded_file_urls")


_DOWNLOAD_ENTRY_SENTINEL = "quarterly-policy-summary-sentinel.pdf"


def _keyed_fingerprints(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("skyvern.forge.sdk.core.hashing.settings.SECRET_KEY", "download-observation-fingerprint-key")


def test_observe_download_dir_separates_landed_bytes_from_in_flight(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    _keyed_fingerprints(monkeypatch)
    (tmp_path / _DOWNLOAD_ENTRY_SENTINEL).write_bytes(b"%PDF-1.4 sentinel")
    (tmp_path / f"partial-{_DOWNLOAD_ENTRY_SENTINEL}{BROWSER_DOWNLOADING_SUFFIX}").write_bytes(b"%PDF")

    observation = observe_download_dir(tmp_path)

    assert observation.entry_count == 2
    assert observation.total_bytes == 21
    assert observation.in_flight_count == 1
    assert observation.dir_missing is False
    assert observation.read_failed is False
    assert observation.entry_fps_truncated is False
    assert len(set(observation.entry_fps)) == 2
    assert "unkeyed" not in observation.entry_fps
    rendered = repr(observation)
    assert _DOWNLOAD_ENTRY_SENTINEL not in rendered
    assert "policy-summary" not in rendered
    assert ".pdf" not in rendered


def test_observe_download_dir_caps_and_flags_truncated_fingerprints(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    _keyed_fingerprints(monkeypatch)
    for index in range(11):
        (tmp_path / f"{index}-{_DOWNLOAD_ENTRY_SENTINEL}").write_bytes(b"x")

    observation = observe_download_dir(tmp_path)

    assert observation.entry_count == 11
    assert len(observation.entry_fps) == 10
    assert observation.entry_fps_truncated is True
    assert _DOWNLOAD_ENTRY_SENTINEL not in repr(observation)


def test_observe_download_dir_survives_a_path_scandir_rejects(tmp_path) -> None:
    """The helper promises never to raise; scandir rejects an embedded null with ValueError."""
    observation = observe_download_dir(tmp_path / "bad\x00name")

    assert observation.read_failed is True
    assert observation.entry_count == 0


def test_observe_download_dir_reports_missing_directory(tmp_path) -> None:
    observation = observe_download_dir(tmp_path / "never-created")

    assert observation.dir_missing is True
    assert observation.read_failed is False
    assert observation.entry_count == 0


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores directory permissions")
def test_observe_download_dir_reports_unreadable_directory_instead_of_zero(tmp_path) -> None:
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    (blocked / _DOWNLOAD_ENTRY_SENTINEL).write_bytes(b"x")
    os.chmod(blocked, 0o000)
    try:
        observation = observe_download_dir(blocked)
    finally:
        os.chmod(blocked, 0o700)

    assert observation.read_failed is True
    assert observation.dir_missing is False
    assert observation.entry_count == 0
    assert _DOWNLOAD_ENTRY_SENTINEL not in repr(observation)


def _empty_read_rows(logs: list[dict]) -> list[dict]:
    return [entry for entry in logs if entry.get("event") == "downloads.empty_read"]


@contextmanager
def _capture_empty_read_logs() -> Iterator[list[dict]]:
    # downloads.empty_read logs at DEBUG; the suite's bound logger filters at INFO and
    # capture_logs only swaps processors, not the level gate, so lower the level for the
    # capture window and restore it after.
    previous_config = structlog.get_config()
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.DEBUG))
    try:
        with capture_logs() as logs:
            yield logs
    finally:
        structlog.configure(**previous_config)


def _artifact_row_storage(
    monkeypatch: pytest.MonkeyPatch, *, keyring: str | None, file_infos: list[FileInfo]
) -> S3Storage:
    monkeypatch.setattr(s3_module.settings, "ARTIFACT_CONTENT_HMAC_KEYRING", keyring)
    monkeypatch.setattr(s3_module, "_file_infos_from_download_artifacts", AsyncMock(return_value=file_infos))
    return S3Storage()


@pytest.mark.asyncio
async def test_downloads_read_stays_silent_when_artifact_rows_resolve(monkeypatch: pytest.MonkeyPatch) -> None:
    resolved = [FileInfo(url="https://example.test/a")]
    storage = _artifact_row_storage(monkeypatch, keyring="k1:secret", file_infos=resolved)
    monkeypatch.setattr(
        storage,
        "_list_download_artifacts_safe",
        AsyncMock(return_value=([SimpleNamespace(browser_session_id=None, checksum=None)], False)),
    )

    with capture_logs() as logs:
        assert await storage.get_downloaded_files("o_1", "wr_1") == resolved

    assert _empty_read_rows(logs) == []


@pytest.mark.asyncio
async def test_downloads_empty_read_reports_unresolvable_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    storage = _artifact_row_storage(monkeypatch, keyring="k1:secret", file_infos=[])
    monkeypatch.setattr(
        storage,
        "_list_download_artifacts_safe",
        AsyncMock(return_value=([SimpleNamespace(browser_session_id=None, checksum=None)], False)),
    )

    with _capture_empty_read_logs() as logs:
        assert await storage.get_downloaded_files("o_1", "wr_1") == []

    row = _empty_read_rows(logs)[0]
    assert row["download_row_count"] == 1
    assert row["rows_present_but_unresolvable"] is True
    assert row["rows_lookup_failed"] is False
    assert row["skip_fired"] is False
    assert row["listed"] is False


@pytest.mark.asyncio
async def test_downloads_empty_read_reports_listing_skip(monkeypatch: pytest.MonkeyPatch) -> None:
    storage = _artifact_row_storage(monkeypatch, keyring="k1:secret", file_infos=[])
    monkeypatch.setattr(storage, "_list_download_artifacts_safe", AsyncMock(return_value=([], False)))
    monkeypatch.setattr(storage, "_skip_empty_downloads_listing", AsyncMock(return_value=True))
    listing = AsyncMock(return_value=[])
    monkeypatch.setattr(storage, "_get_downloaded_files_via_s3_listing", listing)

    with _capture_empty_read_logs() as logs:
        assert await storage.get_downloaded_files("o_1", "wr_1") == []

    row = _empty_read_rows(logs)[0]
    assert row["download_row_count"] == 0
    assert row["skip_fired"] is True
    assert row["listed"] is False
    listing.assert_not_awaited()


@pytest.mark.asyncio
async def test_downloads_empty_read_reports_failed_row_lookup_as_unknown_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = _artifact_row_storage(monkeypatch, keyring="k1:secret", file_infos=[])
    monkeypatch.setattr(
        s3_module.app.DATABASE.artifacts,
        "list_artifacts_for_run_by_type",
        AsyncMock(side_effect=RuntimeError("database unavailable")),
    )
    monkeypatch.setattr(storage, "_skip_empty_downloads_listing", AsyncMock(return_value=False))
    monkeypatch.setattr(storage, "_get_downloaded_files_via_s3_listing", AsyncMock(return_value=[]))

    with _capture_empty_read_logs() as logs:
        assert await storage.get_downloaded_files("o_1", "wr_1") == []

    row = _empty_read_rows(logs)[0]
    assert row["download_row_count"] is None
    assert row["rows_lookup_failed"] is True
    assert row["listed"] is True


@pytest.mark.asyncio
async def test_downloads_empty_read_reports_unqueried_rows_on_legacy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    storage = _artifact_row_storage(monkeypatch, keyring="", file_infos=[])
    monkeypatch.setattr(storage, "_get_downloaded_files_via_s3_listing", AsyncMock(return_value=[]))

    with _capture_empty_read_logs() as logs:
        assert await storage.get_downloaded_files("o_1", "wr_1") == []

    row = _empty_read_rows(logs)[0]
    assert row["download_row_count"] is None
    assert row["rows_lookup_failed"] is False
    assert row["listed"] is True


def test_observe_download_dir_moves_on_same_name_same_size_overwrite(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    _keyed_fingerprints(monkeypatch)
    target = tmp_path / _DOWNLOAD_ENTRY_SENTINEL
    target.write_bytes(b"%PDF-1.4 sentinel")
    before = observe_download_dir(tmp_path)
    rewritten_at = before.newest_mtime_ns + 1_000_000_000
    target.write_bytes(b"%PDF-1.4 replaced")
    os.utime(target, ns=(rewritten_at, rewritten_at))

    after = observe_download_dir(tmp_path)

    assert after.entry_count == before.entry_count
    assert after.total_bytes == before.total_bytes
    assert after.in_flight_count == before.in_flight_count
    assert after.newest_mtime_ns > before.newest_mtime_ns
    fields = classify_download_visibility(
        pre=before,
        settled=after,
        post=after,
        alt_pre=None,
        alt_post=None,
        listed_run_id="wr_1",
        workflow_run_id="wr_1",
        download_binding_kind=None,
    )
    assert fields["landed_during_settle"] is True


def test_classify_reports_unknown_movement_when_a_snapshot_could_not_be_read(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _keyed_fingerprints(monkeypatch)
    listed = tmp_path / "listed"
    listed.mkdir()
    (listed / _DOWNLOAD_ENTRY_SENTINEL).write_bytes(b"%PDF-1.4 already here")
    os.chmod(listed, 0o000)
    try:
        pre = observe_download_dir(listed)
    finally:
        os.chmod(listed, 0o700)
    readable = observe_download_dir(listed)

    assert pre.read_failed is True
    assert readable.total_bytes > 0

    fields = classify_download_visibility(
        pre=pre,
        settled=readable,
        post=readable,
        alt_pre=None,
        alt_post=None,
        listed_run_id="wr_1",
        workflow_run_id="wr_1",
        download_binding_kind=None,
    )

    assert fields["landed_during_settle"] is None
    assert fields["landed_after_settle"] is False


def test_classify_reports_unknown_alt_deltas_when_the_alternate_scan_failed(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _keyed_fingerprints(monkeypatch)
    listed = tmp_path / "listed"
    listed.mkdir()
    alt = tmp_path / "alt"
    alt.mkdir()
    (alt / _DOWNLOAD_ENTRY_SENTINEL).write_bytes(b"%PDF-1.4 sentinel")
    os.chmod(alt, 0o000)
    try:
        alt_pre = observe_download_dir(alt)
    finally:
        os.chmod(alt, 0o700)
    alt_post = observe_download_dir(alt)
    listed_observation = observe_download_dir(listed)

    assert alt_pre.read_failed is True
    assert alt_post.entry_count == 1

    fields = classify_download_visibility(
        pre=listed_observation,
        settled=listed_observation,
        post=listed_observation,
        alt_pre=alt_pre,
        alt_post=alt_post,
        listed_run_id="wr_dl_1",
        workflow_run_id="wr_1",
        download_binding_kind=None,
    )

    assert fields["alt_entry_delta"] is None
    assert fields["alt_bytes_delta"] is None
    assert fields["alt_moved"] is None


def test_classify_reads_alternate_dir_as_a_delta_not_an_absolute_count(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _keyed_fingerprints(monkeypatch)
    listed = tmp_path / "listed"
    listed.mkdir()
    alt = tmp_path / "alt"
    alt.mkdir()
    (alt / f"sibling-block-{_DOWNLOAD_ENTRY_SENTINEL}").write_bytes(b"left behind earlier")
    listed_observation = observe_download_dir(listed)
    alt_pre = observe_download_dir(alt)

    def classify(alt_post):
        return classify_download_visibility(
            pre=listed_observation,
            settled=listed_observation,
            post=listed_observation,
            alt_pre=alt_pre,
            alt_post=alt_post,
            listed_run_id="dr_1",
            workflow_run_id="wr_1",
            download_binding_kind=None,
        )

    stale_only = classify(observe_download_dir(alt))
    assert stale_only["alt_entry_delta"] == 0
    assert stale_only["alt_moved"] is False
    assert stale_only["download_run_id_differs"] is True

    (alt / _DOWNLOAD_ENTRY_SENTINEL).write_bytes(b"%PDF-1.4 sentinel")
    moved = classify(observe_download_dir(alt))

    assert moved["alt_entry_delta"] == 1
    assert moved["alt_moved"] is True
    assert moved["landed_during_settle"] is False
    assert _DOWNLOAD_ENTRY_SENTINEL not in json.dumps(moved)
    assert "sibling-block" not in json.dumps(moved)


@pytest.mark.asyncio
async def test_failed_row_lookup_still_lists_instead_of_reporting_no_downloads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A DB blip must not be answered with an empty download list.

    The cutover skip exists to avoid listing when a run provably has no rows; a lookup that
    failed proves nothing, so the legitimate case has to keep its route through the listing.
    """
    listed = [FileInfo(url="https://example.test/real")]
    storage = _artifact_row_storage(monkeypatch, keyring="k1:secret", file_infos=[])
    monkeypatch.setattr(storage, "_list_download_artifacts_safe", AsyncMock(return_value=([], True)))
    skip = AsyncMock(return_value=True)
    monkeypatch.setattr(storage, "_skip_empty_downloads_listing", skip)
    monkeypatch.setattr(storage, "_get_downloaded_files_via_s3_listing", AsyncMock(return_value=listed))

    assert await storage.get_downloaded_files("o_1", "wr_blip") == listed
    skip.assert_not_awaited()


@pytest.mark.asyncio
async def test_downloads_empty_read_reports_every_empty_read_for_a_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """A run reads empty once at block baseline and again after registration; both must be reported.

    Suppressing the repeat would silence the post-registration read this signal exists to explain.
    """
    storage = _artifact_row_storage(monkeypatch, keyring="", file_infos=[])
    monkeypatch.setattr(storage, "_get_downloaded_files_via_s3_listing", AsyncMock(return_value=[]))

    with _capture_empty_read_logs() as repeated:
        await storage.get_downloaded_files("o_1", "wr_repeat")
        await storage.get_downloaded_files("o_1", "wr_repeat")

    assert len(_empty_read_rows(repeated)) == 2
