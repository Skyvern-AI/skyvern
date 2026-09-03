"""Workflow block that exports schema-defined records as Parquet."""

from __future__ import annotations

from typing import Any, ClassVar, Literal

from skyvern.forge.sdk.workflow.context_manager import WorkflowRunContext
from skyvern.forge.sdk.workflow.exceptions import FailedToFormatJinjaStyleParameter
from skyvern.forge.sdk.workflow.models._jinja import jinja_json_finalize_required_binding_env
from skyvern.forge.sdk.workflow.models.block import Block, ParquetExportMixin
from skyvern.forge.sdk.workflow.models.parameter import PARAMETER_TYPE
from skyvern.schemas.workflows import BlockResult, BlockStatus, BlockType
from skyvern.utils.parquet_export import ParquetExportError


class DataExportBlock(ParquetExportMixin, Block):
    block_type: Literal[BlockType.DATA_EXPORT] = BlockType.DATA_EXPORT  # type: ignore

    data: str
    data_schema: dict[str, Any]
    file_name: str | None = None
    parameters: list[PARAMETER_TYPE] = []

    TEMPLATABLE_FIELDS: ClassVar[frozenset[str]] = frozenset({"data", "file_name"})

    def get_all_parameters(self, workflow_run_id: str) -> list[PARAMETER_TYPE]:
        return self.parameters

    def format_potential_template_parameters(self, workflow_run_context: WorkflowRunContext) -> None:
        self.data = self.render_templatable_field(
            "data",
            self.data,
            workflow_run_context,
            env=jinja_json_finalize_required_binding_env,
            skip_missing_variable_preflight=True,
        )
        if self.file_name:
            self.file_name = self.render_templatable_field("file_name", self.file_name, workflow_run_context)

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
        except Exception as exc:
            return await self._template_format_failure_result(
                exc,
                str(exc),
                workflow_run_context,
                workflow_run_id,
                workflow_run_block_id,
                organization_id,
            )

        try:
            records = self.parse_export_records(self.data)
            output = await self.write_parquet_export(
                records=records,
                data_schema=self.data_schema,
                file_name=self.file_name,
                label=self.label,
                workflow_run_context=workflow_run_context,
                workflow_run_id=workflow_run_id,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id or workflow_run_context.organization_id,
            )
        except (FailedToFormatJinjaStyleParameter, ParquetExportError) as exc:
            return await self._failure(str(exc), workflow_run_block_id, organization_id)

        await self.record_output_parameter_value(workflow_run_context, workflow_run_id, output)
        return await self.build_block_result(
            success=True,
            failure_reason=None,
            output_parameter_value=output,
            status=BlockStatus.completed,
            workflow_run_block_id=workflow_run_block_id,
            organization_id=organization_id,
        )
