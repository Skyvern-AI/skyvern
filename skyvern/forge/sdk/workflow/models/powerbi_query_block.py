from __future__ import annotations

import re
from typing import Any, ClassVar, Literal
from uuid import UUID

from pydantic import Field, field_validator

from skyvern.forge import app
from skyvern.forge.sdk.services.powerbi_query_service import PowerBIQueryError
from skyvern.forge.sdk.workflow.context_manager import WorkflowRunContext
from skyvern.forge.sdk.workflow.models.block import Block, get_all_blocks
from skyvern.forge.sdk.workflow.models.parameter import PARAMETER_TYPE, WorkflowParameter, WorkflowParameterType
from skyvern.schemas.workflows import BlockResult, BlockStatus, BlockType


def normalize_powerbi_query(query: str) -> str:
    return query.replace("\r\n", "\n").replace("\r", "\n").removesuffix("\n")


class PowerBIQueryBlock(Block):
    block_type: Literal[BlockType.POWERBI_QUERY] = BlockType.POWERBI_QUERY  # type: ignore

    connection_id: str
    credential_source: Literal["nango"] = "nango"
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

    def has_valid_input_shape(self) -> bool:
        if not self.query or len(self.query) > 100_000 or any(m in self.query for m in ("{{", "{%", "{#")):
            return False
        if not normalize_powerbi_query(self.query):
            return False
        if len(self.dataset_id) > 255:
            return False
        if re.fullmatch(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}", self.dataset_id):
            return True
        try:
            return len(self.dataset_id) == 36 and str(UUID(self.dataset_id)) == self.dataset_id.lower()
        except ValueError:
            return False

    async def _resolve_dataset_id(self, workflow_run_id: str, organization_id: str) -> str | None:
        try:
            source = self.dataset_id
            if len(source) > 255:
                return None
            match = re.fullmatch(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}", source)
            if match is not None:
                key = match.group(1)
                workflow = await app.DATABASE.workflows.get_workflow_for_workflow_run(
                    workflow_run_id, organization_id=organization_id
                )
                if workflow is None or workflow.organization_id != organization_id:
                    return None
                labels = {block.label for block in get_all_blocks(workflow.workflow_definition.blocks)}
                reserved = (
                    labels
                    | {f"{label}_output" for label in labels}
                    | {
                        "current_index",
                        "current_item",
                        "current_value",
                        "workflow_run_outputs",
                        "workflow_run_summary",
                    }
                )
                if key in reserved:
                    return None
                definitions = [
                    parameter
                    for parameter in workflow.workflow_definition.parameters
                    if isinstance(parameter, WorkflowParameter) and parameter.key == key
                ]
                if len(definitions) != 1:
                    return None
                definition = definitions[0]
                default = definition.default_value
                if (
                    definition.workflow_id != workflow.workflow_id
                    or definition.workflow_parameter_type == WorkflowParameterType.CREDENTIAL_ID
                    or (default is not None and not isinstance(default, str))
                    or (isinstance(default, str) and any(marker in default for marker in ("{{", "{%", "{#")))
                ):
                    return None
                values = [
                    run_parameter.value
                    for parameter, run_parameter in await app.DATABASE.workflow_runs.get_workflow_run_parameters(
                        workflow_run_id
                    )
                    if parameter.workflow_parameter_id == definition.workflow_parameter_id
                    and parameter.workflow_id == definition.workflow_id
                    and parameter.key == definition.key
                    and parameter.workflow_parameter_type == definition.workflow_parameter_type
                    and run_parameter.workflow_run_id == workflow_run_id
                    and run_parameter.workflow_parameter_id == definition.workflow_parameter_id
                ]
                if len(values) != 1 or not isinstance(values[0], str):
                    return None
                source = values[0]
            if len(source) != 36 or str(UUID(source)) != source.lower():
                return None
            return source
        except Exception:  # noqa: BLE001 - unverified run inputs must fail closed.
            return None

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
        output: dict[str, Any] = {"error": error_code}
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
        return await self._execute_powerbi_query(
            workflow_run_id,
            workflow_run_block_id,
            organization_id=organization_id,
            browser_session_id=browser_session_id,
            **kwargs,
        )

    async def _execute_powerbi_query(
        self,
        workflow_run_id: str,
        workflow_run_block_id: str,
        organization_id: str | None = None,
        browser_session_id: str | None = None,
        **kwargs: Any,
    ) -> BlockResult:
        workflow_run_context = self.get_workflow_run_context(workflow_run_id)
        effective_org_id = organization_id or workflow_run_context.organization_id

        if not self.has_valid_input_shape():
            return await self._failure(
                workflow_run_context=workflow_run_context,
                workflow_run_id=workflow_run_id,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=effective_org_id,
                error_code="unsafe_query_input_source",
                failure_reason="Power BI dataset_id and query may use only workflow input parameters or literals.",
            )

        hop_token = workflow_run_context.get_powerbi_hop_token(self.label)
        if not hop_token:
            return await self._failure(
                workflow_run_context=workflow_run_context,
                workflow_run_id=workflow_run_id,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=effective_org_id,
                error_code="integration_unavailable",
                failure_reason="Power BI queries are not currently available.",
            )

        if "{{" in self.connection_id or "{%" in self.connection_id:
            return await self._failure(
                workflow_run_context=workflow_run_context,
                workflow_run_id=workflow_run_id,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=effective_org_id,
                error_code="connection_id_must_be_literal",
                failure_reason="Power BI connection_id must be a literal credential ID.",
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

        dataset_id = await self._resolve_dataset_id(workflow_run_id, effective_org_id)
        if dataset_id is None:
            return await self._failure(
                workflow_run_context=workflow_run_context,
                workflow_run_id=workflow_run_id,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=effective_org_id,
                error_code="unsafe_query_input_source",
                failure_reason="Power BI dataset_id must resolve to a UUID from a workflow input or literal.",
            )
        query = normalize_powerbi_query(self.query)
        self.dataset_id = dataset_id
        self.query = query

        try:
            rows = await app.AGENT_FUNCTION.execute_powerbi_query_for_run(
                organization_id=effective_org_id,
                workflow_run_id=workflow_run_id,
                workflow_run_block_id=workflow_run_block_id,
                connection_id=self.connection_id,
                hop_token=hop_token,
                credential_source=self.credential_source,
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
            if exc.code == "row_limit_exceeded":
                return await self._failure(
                    workflow_run_context=workflow_run_context,
                    workflow_run_id=workflow_run_id,
                    workflow_run_block_id=workflow_run_block_id,
                    organization_id=effective_org_id,
                    error_code="row_limit_exceeded",
                    failure_reason="Power BI query exceeded the row limit.",
                )
            if exc.code in {"integration_unavailable", "invalid_credential_source"}:
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
