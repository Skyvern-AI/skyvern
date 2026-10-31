from __future__ import annotations

from typing import Any, ClassVar, Literal

from pydantic import Field, field_validator

from skyvern.forge import app
from skyvern.forge.sdk.services.powerbi_query_service import PowerBIQueryError
from skyvern.forge.sdk.workflow.context_manager import WorkflowRunContext
from skyvern.forge.sdk.workflow.models.block import Block
from skyvern.forge.sdk.workflow.models.parameter import PARAMETER_TYPE
from skyvern.schemas.workflows import BlockResult, BlockStatus, BlockType
from skyvern.utils.templating import (
    get_caller_supplied_workflow_parameter_keys,
    get_jinja_template_variable_roots,
)


class PowerBIQueryBlock(Block):
    block_type: Literal[BlockType.POWERBI_QUERY] = BlockType.POWERBI_QUERY  # type: ignore

    connection_id: str
    dataset_id: str
    query: str
    max_rows: int = Field(default=1_000, ge=1, le=100_000, strict=True)
    parameters: list[PARAMETER_TYPE] = []

    TEMPLATABLE_FIELDS: ClassVar[frozenset[str]] = frozenset({"dataset_id", "query"})

    @field_validator("connection_id")
    @classmethod
    def validate_literal_connection_id(cls, value: str) -> str:
        if not value.strip() or "{{" in value or "{%" in value:
            raise ValueError("Power BI connection_id must be a literal credential ID.")
        return value

    def get_all_parameters(self, workflow_run_id: str) -> list[PARAMETER_TYPE]:
        return self.parameters

    def _render_templates(self, workflow_run_context: WorkflowRunContext) -> None:
        for field in sorted(self.TEMPLATABLE_FIELDS):
            value = getattr(self, field)
            setattr(self, field, self.render_templatable_field(field, value, workflow_run_context))

    async def _failure(
        self,
        *,
        workflow_run_context: WorkflowRunContext,
        workflow_run_id: str,
        workflow_run_block_id: str,
        organization_id: str | None,
        error_code: str,
        failure_reason: str,
    ) -> BlockResult:
        output = {"error": error_code}
        await self.record_output_parameter_value(workflow_run_context, workflow_run_id, output)
        return await self.build_block_result(
            success=False,
            failure_reason=failure_reason,
            output_parameter_value=output,
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
        effective_org_id = organization_id or workflow_run_context.organization_id

        if "{{" in self.connection_id or "{%" in self.connection_id:
            return await self._failure(
                workflow_run_context=workflow_run_context,
                workflow_run_id=workflow_run_id,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=effective_org_id,
                error_code="connection_id_must_be_literal",
                failure_reason="Power BI connection_id must be a literal credential ID.",
            )

        caller_parameter_keys = get_caller_supplied_workflow_parameter_keys(workflow_run_context.parameters.values())
        try:
            template_roots = set().union(
                *(get_jinja_template_variable_roots(getattr(self, field)) for field in ("dataset_id", "query"))
            )
            block_metadata = workflow_run_context.get_block_metadata(self.label)
            shadowed_parameter_keys = (
                set(workflow_run_context.workflow_run_outputs)
                | workflow_run_context.carried_block_labels
                | {self.label, "workflow_run_outputs", "workflow_run_summary"}
                | ({"current_index", "current_item", "current_value"} & block_metadata.keys())
            )
            unsafe_template_source = bool(
                template_roots - caller_parameter_keys or template_roots & shadowed_parameter_keys
            )
        except Exception:
            unsafe_template_source = True
        if unsafe_template_source:
            return await self._failure(
                workflow_run_context=workflow_run_context,
                workflow_run_id=workflow_run_id,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=effective_org_id,
                error_code="unsafe_query_input_source",
                failure_reason="Power BI dataset_id and query may use only workflow input parameters or literals.",
            )

        try:
            self._render_templates(workflow_run_context)
        except Exception:
            return await self._failure(
                workflow_run_context=workflow_run_context,
                workflow_run_id=workflow_run_id,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=effective_org_id,
                error_code="template_format_failed",
                failure_reason="Could not render Power BI query fields.",
            )

        if not effective_org_id:
            return await self._failure(
                workflow_run_context=workflow_run_context,
                workflow_run_id=workflow_run_id,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=None,
                error_code="execution_context_unavailable",
                failure_reason="Power BI query execution context is unavailable.",
            )

        try:
            rows = await app.AGENT_FUNCTION.execute_powerbi_query_for_run(
                organization_id=effective_org_id,
                workflow_run_id=workflow_run_id,
                workflow_run_block_id=workflow_run_block_id,
                connection_id=self.connection_id,
                dataset_id=self.dataset_id,
                query=self.query,
                max_rows=self.max_rows,
            )
        except NotImplementedError:
            return await self._failure(
                workflow_run_context=workflow_run_context,
                workflow_run_id=workflow_run_id,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=effective_org_id,
                error_code="unsupported_runtime",
                failure_reason="Power BI queries are not supported in this runtime.",
            )
        except PowerBIQueryError as exc:
            if exc.code in {"principal_unavailable", "event_trigger_run"}:
                return await self._failure(
                    workflow_run_context=workflow_run_context,
                    workflow_run_id=workflow_run_id,
                    workflow_run_block_id=workflow_run_block_id,
                    organization_id=effective_org_id,
                    error_code="human_run_required",
                    failure_reason="Power BI queries require a human-started run by the connection owner.",
                )
            if exc.code == "copilot_test_run_unsupported":
                return await self._failure(
                    workflow_run_context=workflow_run_context,
                    workflow_run_id=workflow_run_id,
                    workflow_run_block_id=workflow_run_block_id,
                    organization_id=effective_org_id,
                    error_code="copilot_test_run_unsupported",
                    failure_reason="Power BI blocks can't run in Copilot test runs yet; run the saved workflow instead.",
                )
            if exc.code == "integration_unavailable":
                return await self._failure(
                    workflow_run_context=workflow_run_context,
                    workflow_run_id=workflow_run_id,
                    workflow_run_block_id=workflow_run_block_id,
                    organization_id=effective_org_id,
                    error_code="integration_unavailable",
                    failure_reason="Power BI queries are not currently available.",
                )
            if exc.code == "workflow_owner_resave_required":
                return await self._failure(
                    workflow_run_context=workflow_run_context,
                    workflow_run_id=workflow_run_id,
                    workflow_run_block_id=workflow_run_block_id,
                    organization_id=effective_org_id,
                    error_code="workflow_owner_resave_required",
                    failure_reason="Re-save this workflow as the Power BI connection owner before running it.",
                )
            if exc.code == "trigger_parent_owner_mismatch":
                return await self._failure(
                    workflow_run_context=workflow_run_context,
                    workflow_run_id=workflow_run_id,
                    workflow_run_block_id=workflow_run_block_id,
                    organization_id=effective_org_id,
                    error_code="trigger_chain_owner_required",
                    failure_reason="Power BI queries require every triggering workflow to be saved by the connection owner.",
                )
            if exc.code in {"trigger_parent_source_unconfirmed", "unsafe_query_input_source"}:
                return await self._failure(
                    workflow_run_context=workflow_run_context,
                    workflow_run_id=workflow_run_id,
                    workflow_run_block_id=workflow_run_block_id,
                    organization_id=effective_org_id,
                    error_code="unsafe_query_input_source",
                    failure_reason="Power BI dataset_id and query may use only workflow input parameters or literals.",
                )
            return await self._failure(
                workflow_run_context=workflow_run_context,
                workflow_run_id=workflow_run_id,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=effective_org_id,
                error_code="query_failed",
                failure_reason="Power BI query failed. Check the connection, dataset ID, query, and row limit.",
            )
        except Exception:
            return await self._failure(
                workflow_run_context=workflow_run_context,
                workflow_run_id=workflow_run_id,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=effective_org_id,
                error_code="query_failed",
                failure_reason="Power BI query failed. Check the connection, dataset ID, query, and row limit.",
            )

        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            return await self._failure(
                workflow_run_context=workflow_run_context,
                workflow_run_id=workflow_run_id,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=effective_org_id,
                error_code="invalid_response",
                failure_reason="Power BI query returned an invalid result.",
            )

        await self.record_output_parameter_value(workflow_run_context, workflow_run_id, rows)
        return await self.build_block_result(
            success=True,
            failure_reason=None,
            output_parameter_value=rows,
            status=BlockStatus.completed,
            workflow_run_block_id=workflow_run_block_id,
            organization_id=effective_org_id,
        )
