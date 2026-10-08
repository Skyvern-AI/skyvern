from __future__ import annotations

import asyncio

from skyvern.forge import app
from skyvern.forge.prompts import prompt_engine
from skyvern.forge.sdk.api.llm.api_handler_factory import get_org_aware_secondary_llm_api_handler
from skyvern.forge.sdk.copilot.secret_redaction import redact_raw_secrets_for_prompt
from skyvern.forge.sdk.copilot.tools.workflow_update import CodeArtifactMetadata

_GOAL_PROMPT_NAME = "workflow-copilot-goal-from-code"
_GOAL_TIMEOUT_SECONDS = 30.0


async def suggest_goal_from_code(
    organization_id: str, *, label: str, code: str, current_goal: str, parameter_keys: list[str]
) -> str | None:
    """One tool-less call that writes a Goal from the code text; nothing is persisted."""
    prompt = prompt_engine.load_prompt(
        template=_GOAL_PROMPT_NAME,
        goal_guidance=CodeArtifactMetadata.model_fields["declared_goal"].description,
        label=redact_raw_secrets_for_prompt(label),
        code=redact_raw_secrets_for_prompt(code),
        current_goal=redact_raw_secrets_for_prompt(current_goal),
        parameter_keys=[redact_raw_secrets_for_prompt(key) for key in parameter_keys],
    )
    handler = get_org_aware_secondary_llm_api_handler(default=app.SECONDARY_LLM_API_HANDLER)
    async with asyncio.timeout(_GOAL_TIMEOUT_SECONDS):
        response = await handler(prompt=prompt, prompt_name=_GOAL_PROMPT_NAME, organization_id=organization_id)
    goal = response.get("goal") if isinstance(response, dict) else None
    if not isinstance(goal, str):
        return None
    return goal.strip() or None
