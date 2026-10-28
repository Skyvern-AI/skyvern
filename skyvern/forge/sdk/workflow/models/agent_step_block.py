from __future__ import annotations

from typing import Any, ClassVar, Literal

import structlog

from skyvern.forge import app
from skyvern.forge.sdk.api.llm.config_registry import LLMConfigRegistry
from skyvern.forge.sdk.api.llm.custom_llm_registry import is_custom_llm_key
from skyvern.forge.sdk.workflow.models.block import Block
from skyvern.forge.sdk.workflow.models.parameter import PARAMETER_TYPE
from skyvern.schemas.workflows import BlockResult, BlockStatus, BlockType

LOG = structlog.get_logger()

LLM_FLAG = "AGENT_BLOCK_LLM_NAME"


def usable_agent_llm_key(variant: str | None) -> bool:
    if not variant:
        return False
    config = LLMConfigRegistry.get_config(variant) if LLMConfigRegistry.is_registered(variant) else None
    return (
        not is_custom_llm_key(variant)
        and config is not None
        and bool(config.pin_reasoning_effort and config.reasoning_effort)
    )


async def read_flag_llm_key(workflow_run_id: str, organization_id: str | None, workflow_permanent_id: str) -> str:
    """Raises ValueError naming the flag unless it gives a registered platform key with a pinned effort."""
    try:
        variant = await app.EXPERIMENTATION_PROVIDER.get_value_cached(
            LLM_FLAG,
            workflow_run_id,
            properties={"organization_id": organization_id, "workflow_permanent_id": workflow_permanent_id},
        )
    except Exception as e:
        LOG.warning("Failed to read the agent block flag", flag=LLM_FLAG, exc_info=True)
        raise ValueError(f"The agent block could not read {LLM_FLAG}.") from e
    if not isinstance(variant, str) or not variant:
        raise ValueError(f"The agent block is not enabled for this run: {LLM_FLAG} has no variant for it.")
    if not usable_agent_llm_key(variant):
        LOG.warning("Agent block flag variant is not usable", flag=LLM_FLAG, variant=variant)
        raise ValueError(
            f"{LLM_FLAG} variant {variant!r} is not a registered platform llm_key that pins a reasoning effort."
        )
    return variant


class AgentStepBlock(Block):
    block_type: Literal[BlockType.AGENT_STEP] = BlockType.AGENT_STEP  # type: ignore

    prompt: str
    data_schema: dict[str, Any]
    max_turns: int = 25
    parameters: list[PARAMETER_TYPE] = []

    TEMPLATABLE_FIELDS: ClassVar[frozenset[str]] = frozenset({"prompt"})

    def get_all_parameters(self, workflow_run_id: str) -> list[PARAMETER_TYPE]:
        return self.parameters

    async def execute(
        self,
        workflow_run_id: str,
        workflow_run_block_id: str,
        organization_id: str | None = None,
        browser_session_id: str | None = None,
        **kwargs: dict,
    ) -> BlockResult:
        run_context = self.get_workflow_run_context(workflow_run_id)
        try:
            llm_key = await read_flag_llm_key(workflow_run_id, organization_id, run_context.workflow_permanent_id)
        except ValueError as e:
            return await self.build_block_result(
                success=False,
                failure_reason=str(e),
                status=BlockStatus.failed,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )
        await app.AGENT_FUNCTION.validate_block_execution(
            block=self,
            workflow_run_block_id=workflow_run_block_id,
            workflow_run_id=workflow_run_id,
            organization_id=organization_id,
        )
        await app.DATABASE.observer.update_workflow_run_block(
            workflow_run_block_id=workflow_run_block_id, organization_id=organization_id, prompt=self.prompt
        )
        try:
            prompt = self.render_templatable_field("prompt", self.prompt, run_context)
            self._apply_workflow_system_prompt(run_context)
        except Exception as e:
            return await self._template_format_failure_result(
                e,
                f"Failed to format jinja template: {e}",
                run_context,
                workflow_run_id,
                workflow_run_block_id,
                organization_id,
            )
        # openai-agents is a server-only dependency, and embedded local mode imports every block class.
        from skyvern.forge.sdk.workflow.models.agent_step_loop import run_agent_step

        return await run_agent_step(
            self, llm_key, prompt, workflow_run_id, workflow_run_block_id, organization_id, browser_session_id
        )
