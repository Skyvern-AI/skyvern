from typing import Any, Awaitable, Protocol

from skyvern.forge.sdk.models import Step
from skyvern.forge.sdk.schemas.ai_suggestions import AISuggestion
from skyvern.forge.sdk.schemas.task_v2 import TaskV2, Thought
from skyvern.utils.image_resizer import Resolution


class LLMAPIHandler(Protocol):
    def __call__(
        self,
        prompt: str,
        prompt_name: str,
        step: Step | None = None,
        task_v2: TaskV2 | None = None,
        thought: Thought | None = None,
        ai_suggestion: AISuggestion | None = None,
        screenshots: list[bytes] | None = None,
        parameters: dict[str, Any] | None = None,
        organization_id: str | None = None,
        tools: list | None = None,
        use_message_history: bool = False,
        raw_response: bool = False,
        window_dimension: Resolution | None = None,
        force_dict: bool = True,
    ) -> Awaitable[dict[str, Any] | Any]: ...


async def dummy_llm_api_handler(
    prompt: str,
    prompt_name: str,
    step: Step | None = None,
    task_v2: TaskV2 | None = None,
    thought: Thought | None = None,
    ai_suggestion: AISuggestion | None = None,
    screenshots: list[bytes] | None = None,
    parameters: dict[str, Any] | None = None,
    organization_id: str | None = None,
    tools: list | None = None,
    use_message_history: bool = False,
    raw_response: bool = False,
    window_dimension: Resolution | None = None,
    force_dict: bool = True,
) -> dict[str, Any] | Any:
    raise NotImplementedError("Your LLM provider is not configured. Please configure it in the .env file.")
