from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Self
from unittest.mock import AsyncMock

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from skyvern.forge import app
from skyvern.forge.sdk.schemas.files import FileInfo
from skyvern.forge.sdk.workflow.context_manager import WorkflowRunContext
from skyvern.forge.sdk.workflow.models.data_export_block import DataExportBlock
from skyvern.forge.sdk.workflow.models.parameter import OutputParameter
from skyvern.forge.sdk.workflow.workflow_definition_converter import block_yaml_to_block
from skyvern.schemas.workflows import BlockStatus, DataExportBlockYAML

_RECORD_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "id": {"type": "integer"},
            "name": {"type": "string"},
        },
    },
}


def _output_parameter() -> OutputParameter:
    now = datetime.now(UTC)
    return OutputParameter(
        output_parameter_id="export_output_id",
        key="export_output",
        workflow_id="workflow-id",
        created_at=now,
        modified_at=now,
    )


def _block(**overrides: object) -> DataExportBlock:
    return DataExportBlock(
        **{
            "label": "export",
            "output_parameter": _output_parameter(),
            "data": "[]",
            "data_schema": _RECORD_SCHEMA,
            **overrides,
        }
    )


def test_yaml_conversion_builds_a_parquet_export_block() -> None:
    block = block_yaml_to_block(
        DataExportBlockYAML(
            label="export",
            data="{{ extract_output.extracted_information }}",
            data_schema=_RECORD_SCHEMA,
        ),
        {"export_output": _output_parameter()},
    )

    assert isinstance(block, DataExportBlock)
    assert block.data_schema == _RECORD_SCHEMA


@pytest.mark.asyncio
async def test_export_block_writes_and_registers_one_parquet_file_per_loop_iteration(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    download_directory = tmp_path / "workflow-downloads"
    context = WorkflowRunContext(
        workflow_title="Parquet test",
        workflow_id="workflow-id",
        workflow_permanent_id="wpid",
        workflow_run_id="run-id",
        aws_client=AsyncMock(),
    )
    context.organization_id = "org-id"
    context.values["extract_output"] = {"extracted_information": [{"id": 1, "name": "one"}, {"id": 2}]}
    context.update_block_metadata("export", {"current_index": 2})
    monkeypatch.setattr(DataExportBlock, "get_workflow_run_context", staticmethod(lambda _run_id: context))
    monkeypatch.setattr(app.DATABASE.workflow_runs, "create_or_update_workflow_run_output_parameter", AsyncMock())
    monkeypatch.setattr(
        "skyvern.forge.sdk.workflow.models.block.get_path_for_workflow_download_directory",
        lambda _run_id: download_directory,
    )
    monkeypatch.setattr(app.STORAGE, "save_downloaded_files", AsyncMock())
    monkeypatch.setattr(
        app.STORAGE,
        "get_downloaded_files",
        AsyncMock(
            return_value=[FileInfo(url="https://example.test/items-0003.parquet", filename="items-0003.parquet")]
        ),
    )

    result = await _block(
        data="{{ extract_output.extracted_information }}",
        file_name="items",
    ).execute("run-id", "block-id", organization_id="org-id")

    assert result.success is True
    assert result.status is BlockStatus.completed
    assert result.output_parameter_value == {
        "file_name": "items-0003.parquet",
        "file_path": str(download_directory / "items-0003.parquet"),
        "file_size": (download_directory / "items-0003.parquet").stat().st_size,
        "format": "parquet",
        "compression": "snappy",
        "row_count": 2,
        "columns": ["id", "name"],
        "downloaded_files": [
            FileInfo(url="https://example.test/items-0003.parquet", filename="items-0003.parquet").model_dump()
        ],
        "downloaded_file_urls": ["https://example.test/items-0003.parquet"],
    }
    table = pq.read_table(pa.BufferReader((download_directory / "items-0003.parquet").read_bytes()))
    assert table.to_pylist() == [{"id": 1, "name": "one"}, {"id": 2, "name": None}]


@pytest.mark.asyncio
async def test_export_block_preserves_parquet_files_with_repeated_loop_indexes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    download_directory = tmp_path / "workflow-downloads"
    context = WorkflowRunContext(
        workflow_title="Parquet test",
        workflow_id="workflow-id",
        workflow_permanent_id="wpid",
        workflow_run_id="run-id",
        aws_client=AsyncMock(),
    )
    context.update_block_metadata("export", {"current_index": 0})
    monkeypatch.setattr(DataExportBlock, "get_workflow_run_context", staticmethod(lambda _run_id: context))
    monkeypatch.setattr(app.DATABASE.workflow_runs, "create_or_update_workflow_run_output_parameter", AsyncMock())
    monkeypatch.setattr(
        "skyvern.forge.sdk.workflow.models.block.get_path_for_workflow_download_directory",
        lambda _run_id: download_directory,
    )

    block = _block(file_name="items")
    first_result = await block.execute("run-id", "block-id")
    second_result = await block.execute("run-id", "block-id")

    assert first_result.output_parameter_value["file_name"] == "items-0001.parquet"
    assert second_result.output_parameter_value["file_name"] == "items-0001-0002.parquet"
    assert sorted(path.name for path in download_directory.glob("*.parquet")) == [
        "items-0001-0002.parquet",
        "items-0001.parquet",
    ]


@pytest.mark.asyncio
async def test_export_block_returns_clean_failure_when_writing_parquet_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    context = WorkflowRunContext(
        workflow_title="Parquet test",
        workflow_id="workflow-id",
        workflow_permanent_id="wpid",
        workflow_run_id="run-id",
        aws_client=AsyncMock(),
    )
    download_directory = tmp_path / "workflow-downloads"
    monkeypatch.setattr(DataExportBlock, "get_workflow_run_context", staticmethod(lambda _run_id: context))
    monkeypatch.setattr(
        "skyvern.forge.sdk.workflow.models.block.get_path_for_workflow_download_directory",
        lambda _run_id: download_directory,
    )

    def fail_write(_path: Path, _mode: str = "r", **_kwargs: object) -> object:
        raise OSError("disk full")

    monkeypatch.setattr(Path, "open", fail_write)

    result = await _block().execute("run-id", "block-id")

    assert download_directory.is_dir()
    assert result.success is False
    assert result.status is BlockStatus.failed
    assert result.failure_reason == "failed to write export.parquet: disk full"


@pytest.mark.asyncio
async def test_export_block_removes_partial_parquet_file_after_write_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    context = WorkflowRunContext(
        workflow_title="Parquet test",
        workflow_id="workflow-id",
        workflow_permanent_id="wpid",
        workflow_run_id="run-id",
        aws_client=AsyncMock(),
    )
    download_directory = tmp_path / "workflow-downloads"
    monkeypatch.setattr(DataExportBlock, "get_workflow_run_context", staticmethod(lambda _run_id: context))
    monkeypatch.setattr(
        "skyvern.forge.sdk.workflow.models.block.get_path_for_workflow_download_directory",
        lambda _run_id: download_directory,
    )

    original_open = Path.open

    class PartiallyWrittenFile:
        def __init__(self, parquet_file: Any) -> None:
            self.parquet_file = parquet_file

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_args: object) -> None:
            self.parquet_file.close()

        def write(self, data: bytes) -> int:
            self.parquet_file.write(data[:1])
            raise OSError("disk full")

    def fail_after_partial_write(path: Path, mode: str = "r", **kwargs: object) -> PartiallyWrittenFile:
        return PartiallyWrittenFile(original_open(path, mode, **kwargs))

    monkeypatch.setattr(Path, "open", fail_after_partial_write)

    result = await _block().execute("run-id", "block-id")

    assert result.success is False
    assert result.status is BlockStatus.failed
    assert result.failure_reason == "failed to write export.parquet: disk full"
    assert not (download_directory / "export.parquet").exists()
