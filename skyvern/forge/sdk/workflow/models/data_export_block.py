"""Workflow block that exports schema-defined records as Parquet."""

from __future__ import annotations

import asyncio
import json
from typing import Any, Literal

import structlog

from skyvern.constants import GET_DOWNLOADED_FILES_TIMEOUT, SAVE_DOWNLOADED_FILES_TIMEOUT
from skyvern.exceptions import DownloadSaveIncompleteError
from skyvern.forge import app
from skyvern.forge.sdk.api.files import get_path_for_workflow_download_directory, resolve_run_download_id
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.schemas.files import FileInfo
from skyvern.forge.sdk.workflow.context_manager import WorkflowRunContext
from skyvern.forge.sdk.workflow.exceptions import FailedToFormatJinjaStyleParameter
from skyvern.forge.sdk.workflow.models._jinja import jinja_json_finalize_required_binding_env
from skyvern.forge.sdk.workflow.models.block import Block, sanitize_filename
from skyvern.forge.sdk.workflow.models.parameter import PARAMETER_TYPE
from skyvern.schemas.workflows import BlockResult, BlockStatus, BlockType
from skyvern.utils.parquet_export import ParquetExportError, export_parquet_records

LOG = structlog.get_logger()


class DataExportBlock(Block):
    block_type: Literal[BlockType.DATA_EXPORT] = BlockType.DATA_EXPORT  # type: ignore

    data: str
    data_schema: dict[str, Any]
    file_name: str | None = None
    parameters: list[PARAMETER_TYPE] = []

    def get_all_parameters(self, workflow_run_id: str) -> list[PARAMETER_TYPE]:
        return self.parameters

    def format_potential_template_parameters(self, workflow_run_context: WorkflowRunContext) -> None:
        self.data = self.format_block_parameter_template_from_workflow_run_context(
            self.data,
            workflow_run_context,
            env=jinja_json_finalize_required_binding_env,
            skip_missing_variable_preflight=True,
        )
        if self.file_name:
            self.file_name = self.format_block_parameter_template_from_workflow_run_context(
                self.file_name, workflow_run_context
            )

    def _resolve_file_name(self, workflow_run_context: WorkflowRunContext) -> str:
        stem = sanitize_filename(self.file_name or self.label)
        if stem.lower().endswith(".parquet"):
            stem = stem[: -len(".parquet")]
        current_index = workflow_run_context.get_block_metadata(self.label).get("current_index")
        if isinstance(current_index, int) and not isinstance(current_index, bool):
            stem = f"{stem}-{current_index + 1:04d}"
        return f"{stem}.parquet"

    @staticmethod
    def _parse_records(data: str) -> list[Any]:
        try:
            records = json.loads(data)
        except json.JSONDecodeError as exc:
            raise ParquetExportError("data must resolve to a JSON array") from exc
        if not isinstance(records, list):
            raise ParquetExportError("data must resolve to a JSON array of object records")
        return records

    async def _register_download(
        self,
        *,
        organization_id: str | None,
        run_download_id: str | None,
        workflow_run_id: str,
        workflow_run_block_id: str,
    ) -> list[FileInfo]:
        if not organization_id:
            return []
        try:
            async with asyncio.timeout(SAVE_DOWNLOADED_FILES_TIMEOUT):
                await app.STORAGE.save_downloaded_files(organization_id=organization_id, run_id=run_download_id)
        except asyncio.TimeoutError:
            LOG.warning(
                "Timeout saving Parquet export; workflow finalization will retry",
                workflow_run_id=workflow_run_id,
                workflow_run_block_id=workflow_run_block_id,
            )
            return []
        except DownloadSaveIncompleteError:
            pass
        except Exception:
            LOG.warning(
                "Failed to register Parquet export; workflow finalization will retry",
                workflow_run_id=workflow_run_id,
                workflow_run_block_id=workflow_run_block_id,
                exc_info=True,
            )
            return []
        try:
            async with asyncio.timeout(GET_DOWNLOADED_FILES_TIMEOUT):
                return await app.STORAGE.get_downloaded_files(organization_id=organization_id, run_id=run_download_id)
        except Exception:
            LOG.warning(
                "Failed to read registered Parquet exports",
                workflow_run_id=workflow_run_id,
                workflow_run_block_id=workflow_run_block_id,
                exc_info=True,
            )
            return []

    async def _failure(
        self,
        reason: str,
        workflow_run_block_id: str,
        organization_id: str | None,
    ) -> BlockResult:
        return await self.build_block_result(
            success=False,
            failure_reason=reason,
            status=BlockStatus.failed,
            workflow_run_block_id=workflow_run_block_id,
            organization_id=organization_id,
        )

    async def execute(
        self,
        workflow_run_id: str,
        workflow_run_block_id: str,
        organization_id: str | None = None,
        browser_session_id: str | None = None,
        **kwargs: Any,
    ) -> BlockResult:
        workflow_run_context = self.get_workflow_run_context(workflow_run_id)
        try:
            self.format_potential_template_parameters(workflow_run_context)
            records = self._parse_records(self.data)
            parquet_data = export_parquet_records(records, self.data_schema)
        except (FailedToFormatJinjaStyleParameter, ParquetExportError) as exc:
            return await self._failure(str(exc), workflow_run_block_id, organization_id)

        filename = self._resolve_file_name(workflow_run_context)
        run_download_id = resolve_run_download_id(skyvern_context.current(), fallback_run_id=workflow_run_id)
        path = get_path_for_workflow_download_directory(run_download_id) / filename
        file_created = False
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            stem = path.stem
            suffix = 2
            while True:
                try:
                    with path.open("xb") as parquet_file:
                        file_created = True
                        parquet_file.write(parquet_data)
                    break
                except FileExistsError:
                    path = path.with_name(f"{stem}-{suffix:04d}{path.suffix}")
                    suffix += 1
        except OSError as exc:
            if file_created:
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
                except OSError:
                    LOG.warning("Failed to remove incomplete Parquet export", exc_info=True)
            return await self._failure(f"failed to write {filename}: {exc}", workflow_run_block_id, organization_id)

        filename = path.name
        downloaded_files = await self._register_download(
            organization_id=organization_id or workflow_run_context.organization_id,
            run_download_id=run_download_id,
            workflow_run_id=workflow_run_id,
            workflow_run_block_id=workflow_run_block_id,
        )
        downloaded_files = [file for file in downloaded_files if file.filename == filename]
        output = {
            "file_name": filename,
            "file_path": str(path),
            "file_size": path.stat().st_size,
            "format": "parquet",
            "compression": "snappy",
            "row_count": len(records),
            "columns": list(self.data_schema.get("items", {}).get("properties", {})),
            "downloaded_files": [file.model_dump() for file in downloaded_files],
            "downloaded_file_urls": [file.url for file in downloaded_files],
        }
        await self.record_output_parameter_value(workflow_run_context, workflow_run_id, output)
        return await self.build_block_result(
            success=True,
            failure_reason=None,
            output_parameter_value=output,
            status=BlockStatus.completed,
            workflow_run_block_id=workflow_run_block_id,
            organization_id=organization_id,
        )
