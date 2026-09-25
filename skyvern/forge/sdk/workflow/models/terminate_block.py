from __future__ import annotations

from typing import Any, ClassVar, Literal

from skyvern.forge.sdk.workflow.models.block import Block
from skyvern.forge.sdk.workflow.models.parameter import PARAMETER_TYPE
from skyvern.schemas.workflows import BlockResult, BlockStatus, BlockType
from skyvern.utils.secret_redaction import redact_secrets_from_text


class TerminateBlock(Block):
    block_type: Literal[BlockType.TERMINATE] = BlockType.TERMINATE  # type: ignore

    reason: str

    TEMPLATABLE_FIELDS: ClassVar[frozenset[str]] = frozenset({"reason"})
    FAILURE_IS_CONTINUABLE: ClassVar[bool] = False

    def get_all_parameters(self, workflow_run_id: str) -> list[PARAMETER_TYPE]:
        return []

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
            reason = self.render_templatable_field("reason", self.reason, workflow_run_context)
        except Exception as exc:
            return await self._template_format_failure_result(
                exc, str(exc), workflow_run_context, workflow_run_id, workflow_run_block_id, organization_id
            )

        reason = redact_secrets_from_text(reason, self._registered_secret_values(workflow_run_context))
        if not reason.strip():
            # A template such as "{{ code }}" can render blank; the run still stops, with a reason that says where.
            reason = f"Terminated by the {self.label} block"
        output = {"reason": reason}
        await self.record_output_parameter_value(workflow_run_context, workflow_run_id, output)
        return await self.build_block_result(
            success=False,
            failure_reason=reason,
            output_parameter_value=output,
            status=BlockStatus.terminated,
            workflow_run_block_id=workflow_run_block_id,
            organization_id=organization_id,
        )
