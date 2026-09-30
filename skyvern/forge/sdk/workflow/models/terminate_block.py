from __future__ import annotations

import re
from typing import Any, ClassVar, Literal

from skyvern.forge.sdk.workflow.models.block import Block
from skyvern.forge.sdk.workflow.models.parameter import PARAMETER_TYPE
from skyvern.schemas.workflows import BlockResult, BlockStatus, BlockType, error_code_key_error
from skyvern.utils.secret_redaction import redact_secrets_from_text


class TerminateBlock(Block):
    block_type: Literal[BlockType.TERMINATE] = BlockType.TERMINATE  # type: ignore

    reason: str
    error_code: str | None = None

    TEMPLATABLE_FIELDS: ClassVar[frozenset[str]] = frozenset({"reason", "error_code"})
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
        secrets = self._registered_secret_values(workflow_run_context)
        error_code = ""
        try:
            reason = self.render_templatable_field("reason", self.reason, workflow_run_context)
        except Exception as exc:
            return await self._template_format_failure_result(
                exc, str(exc), workflow_run_context, workflow_run_id, workflow_run_block_id, organization_id
            )

        error_code_dropped = False
        if self.error_code:
            try:
                rendered_error_code = self.render_templatable_field("error_code", self.error_code, workflow_run_context)
                error_code = rendered_error_code.strip()
                if error_code and (
                    error_code_key_error(error_code)
                    or re.fullmatch(r"[A-Za-z0-9_.:-]+", error_code) is None
                    or redact_secrets_from_text(rendered_error_code, secrets, match_inside_words=True)
                    != rendered_error_code
                ):
                    error_code_dropped = True
                    error_code = ""
            except Exception:
                error_code_dropped = True
                error_code = ""

        reason = redact_secrets_from_text(reason, secrets)
        if not reason.strip():
            # A template such as "{{ code }}" can render blank; the run still stops, with a reason that says where.
            reason = f"Terminated by the {self.label} block"
        if error_code_dropped:
            reason += " Error code was dropped because it was unusable after rendering."
        output: dict[str, Any] = {"reason": reason}
        if error_code_dropped:
            output["error_code_dropped"] = True
        await self.record_output_parameter_value(workflow_run_context, workflow_run_id, output)
        return await self.build_block_result(
            success=False,
            failure_reason=reason,
            output_parameter_value=output,
            status=BlockStatus.terminated,
            workflow_run_block_id=workflow_run_block_id,
            organization_id=organization_id,
            error_codes=[error_code] if error_code else None,
        )
