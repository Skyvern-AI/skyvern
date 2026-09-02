from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from skyvern.forge import app
from skyvern.forge.sdk.workflow.context_manager import WorkflowRunContext
from skyvern.forge.sdk.workflow.exceptions import InvalidWorkflowDefinition
from skyvern.forge.sdk.workflow.models.block import BaseTaskBlock, ExtractionBlock
from skyvern.forge.sdk.workflow.models.parameter import OutputParameter
from skyvern.forge.sdk.workflow.workflow_definition_converter import block_yaml_to_block
from skyvern.schemas.workflows import BlockResult, BlockStatus, ExtractionBlockYAML

_RECORD_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {"id": {"type": "integer"}, "name": {"type": "string"}},
    },
}


def _output_parameter() -> OutputParameter:
    now = datetime.now(UTC)
    return OutputParameter(
        output_parameter_id="extraction_output_id",
        key="extraction_output",
        workflow_id="workflow-id",
        created_at=now,
        modified_at=now,
    )


def _block(**overrides: object) -> ExtractionBlock:
    return ExtractionBlock(
        **{
            "label": "extract",
            "output_parameter": _output_parameter(),
            "data_extraction_goal": "extract records",
            **overrides,
        }
    )


def _extraction_result(extracted_information: object) -> BlockResult:
    return BlockResult(
        success=True,
        output_parameter=_output_parameter(),
        output_parameter_value={
            "task_id": "t1",
            "status": "completed",
            "extracted_information": extracted_information,
        },
        status=BlockStatus.completed,
    )


def _context() -> WorkflowRunContext:
    return WorkflowRunContext(
        workflow_title="Extraction export test",
        workflow_id="workflow-id",
        workflow_permanent_id="wpid",
        workflow_run_id="run-id",
        aws_client=AsyncMock(),
    )


@pytest.mark.asyncio
async def test_export_disabled_leaves_extraction_output_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    extraction_result = _extraction_result([{"id": 1, "name": "one"}])
    monkeypatch.setattr(BaseTaskBlock, "execute", AsyncMock(return_value=extraction_result))

    result = await _block().execute("run-id", "block-id")

    assert result is extraction_result
    assert "export" not in (result.output_parameter_value or {})


@pytest.mark.asyncio
async def test_export_enabled_defaults_to_this_blocks_own_extraction(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    download_directory = tmp_path / "workflow-downloads"
    context = _context()
    context.organization_id = "org-id"

    # A single-object extraction result becomes a one-row export, not a
    # required-list authoring step.
    extraction_result = _extraction_result({"id": 1, "name": "one"})
    monkeypatch.setattr(BaseTaskBlock, "execute", AsyncMock(return_value=extraction_result))
    monkeypatch.setattr(ExtractionBlock, "get_workflow_run_context", staticmethod(lambda _run_id: context))
    monkeypatch.setattr(app.DATABASE.workflow_runs, "create_or_update_workflow_run_output_parameter", AsyncMock())
    monkeypatch.setattr(
        "skyvern.forge.sdk.workflow.models.block.get_path_for_workflow_download_directory",
        lambda _run_id: download_directory,
    )
    monkeypatch.setattr(app.STORAGE, "save_downloaded_files", AsyncMock())
    monkeypatch.setattr(app.STORAGE, "get_downloaded_files", AsyncMock(return_value=[]))

    block = _block(export_enabled=True, export_data_schema=_RECORD_SCHEMA, export_file_name="records")
    result = await block.execute("run-id", "block-id", organization_id="org-id")

    assert result.success is True
    assert result.output_parameter_value["extracted_information"] == {"id": 1, "name": "one"}
    export = result.output_parameter_value["export"]
    assert export["row_count"] == 1
    assert export["file_name"] == "records.parquet"
    assert (download_directory / "records.parquet").exists()


@pytest.mark.asyncio
async def test_export_enabled_without_schema_fails_the_block(monkeypatch: pytest.MonkeyPatch) -> None:
    extraction_result = _extraction_result([{"id": 1}])
    monkeypatch.setattr(BaseTaskBlock, "execute", AsyncMock(return_value=extraction_result))
    monkeypatch.setattr(ExtractionBlock, "get_workflow_run_context", staticmethod(lambda _run_id: _context()))

    block = _block(export_enabled=True)
    result = await block.execute("run-id", "block-id")

    assert result.success is False
    assert result.status is BlockStatus.failed
    assert result.failure_reason == "export_data_schema is required when export is enabled"


@pytest.mark.asyncio
async def test_extraction_failure_skips_export_entirely(monkeypatch: pytest.MonkeyPatch) -> None:
    failed_result = BlockResult(
        success=False,
        output_parameter=_output_parameter(),
        output_parameter_value=None,
        status=BlockStatus.failed,
        failure_reason="the LLM never found the data",
    )
    monkeypatch.setattr(BaseTaskBlock, "execute", AsyncMock(return_value=failed_result))

    block = _block(export_enabled=True, export_data_schema=_RECORD_SCHEMA)
    result = await block.execute("run-id", "block-id")

    assert result is failed_result


@pytest.mark.asyncio
async def test_export_enabled_with_null_extraction_exports_zero_rows(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A null extraction (e.g. an optional field the run legitimately didn't find)
    # must produce an honest empty export, not a fabricated one-row all-null file
    # that silently reports success (review: block.py:12067).
    download_directory = tmp_path / "workflow-downloads"
    context = _context()
    context.organization_id = "org-id"

    extraction_result = _extraction_result(None)
    monkeypatch.setattr(BaseTaskBlock, "execute", AsyncMock(return_value=extraction_result))
    monkeypatch.setattr(ExtractionBlock, "get_workflow_run_context", staticmethod(lambda _run_id: context))
    monkeypatch.setattr(app.DATABASE.workflow_runs, "create_or_update_workflow_run_output_parameter", AsyncMock())
    monkeypatch.setattr(
        "skyvern.forge.sdk.workflow.models.block.get_path_for_workflow_download_directory",
        lambda _run_id: download_directory,
    )
    monkeypatch.setattr(app.STORAGE, "save_downloaded_files", AsyncMock())
    monkeypatch.setattr(app.STORAGE, "get_downloaded_files", AsyncMock(return_value=[]))

    block = _block(export_enabled=True, export_data_schema=_RECORD_SCHEMA)
    result = await block.execute("run-id", "block-id", organization_id="org-id")

    assert result.success is True
    assert result.output_parameter_value["export"]["row_count"] == 0


@pytest.mark.asyncio
async def test_export_enabled_with_a_bare_string_extraction_fails_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    # A scalar (not an object or list of objects) can't honestly become export
    # rows either -- surface it instead of silently writing an all-null row.
    extraction_result = _extraction_result("just some text")
    monkeypatch.setattr(BaseTaskBlock, "execute", AsyncMock(return_value=extraction_result))
    monkeypatch.setattr(ExtractionBlock, "get_workflow_run_context", staticmethod(lambda _run_id: _context()))

    block = _block(export_enabled=True, export_data_schema=_RECORD_SCHEMA)
    result = await block.execute("run-id", "block-id")

    assert result.success is False
    assert result.status is BlockStatus.failed
    assert "extracted_information must be an object or a list of objects to export" in (result.failure_reason or "")


def test_export_records_does_not_render_when_export_is_disabled() -> None:
    # A stale export_records referencing a deleted block must not fail an
    # otherwise-fine run when export is off (review: block.py:12034).
    block = _block(export_enabled=False, export_records="{{ deleted_block.extracted_information }}")

    block.format_potential_template_parameters(_context())

    assert block.export_records == "{{ deleted_block.extracted_information }}"


def test_export_records_does_render_and_can_fail_when_export_is_enabled() -> None:
    block = _block(
        export_enabled=True,
        export_data_schema=_RECORD_SCHEMA,
        export_records="{{ deleted_block.extracted_information }}",
    )

    with pytest.raises(Exception):  # noqa: B017 -- FailedToFormatJinjaStyleParameter, exact type is call-site detail
        block.format_potential_template_parameters(_context())


@pytest.mark.asyncio
async def test_export_failure_replaces_stale_successful_output(monkeypatch: pytest.MonkeyPatch) -> None:
    # The base execute() already recorded the successful extraction's output
    # (_output_recorded_this_execution is already true), so an export failure
    # that doesn't re-record would leave a downstream continue_on_failure
    # reader seeing clean output for a block that actually failed
    # (review: block.py:12099).
    extraction_result = _extraction_result([{"id": 1}])
    monkeypatch.setattr(BaseTaskBlock, "execute", AsyncMock(return_value=extraction_result))
    monkeypatch.setattr(ExtractionBlock, "get_workflow_run_context", staticmethod(lambda _run_id: _context()))
    record_mock = AsyncMock()
    monkeypatch.setattr(app.DATABASE.workflow_runs, "create_or_update_workflow_run_output_parameter", record_mock)

    # export_data_schema missing is the simplest way to hit the ParquetExportError branch.
    block = _block(export_enabled=True)
    result = await block.execute("run-id", "block-id")

    assert result.success is False
    assert result.output_parameter_value is not None
    assert "extracted_information" not in result.output_parameter_value
    assert result.output_parameter_value["failure_reason"] == "export_data_schema is required when export is enabled"
    record_mock.assert_awaited_once()
    assert record_mock.await_args.kwargs["value"] == result.output_parameter_value


def test_yaml_conversion_rejects_export_enabled_without_a_schema() -> None:
    # Author-time feedback instead of a run-time-only failure (review: block.py:12063
    # -- a UI checkbox can null out the schema with export still enabled).
    with pytest.raises(InvalidWorkflowDefinition):
        block_yaml_to_block(
            ExtractionBlockYAML(
                label="extract",
                data_extraction_goal="extract records",
                export_enabled=True,
                export_data_schema=None,
            ),
            {"extract_output": _output_parameter()},
        )


def test_yaml_conversion_carries_export_fields_onto_the_extraction_block() -> None:
    block = block_yaml_to_block(
        ExtractionBlockYAML(
            label="extract",
            data_extraction_goal="extract records",
            export_enabled=True,
            export_data_schema=_RECORD_SCHEMA,
            export_file_name="records",
        ),
        {"extract_output": _output_parameter()},
    )

    assert isinstance(block, ExtractionBlock)
    assert block.export_enabled is True
    assert block.export_data_schema == _RECORD_SCHEMA
    assert block.export_file_name == "records"
