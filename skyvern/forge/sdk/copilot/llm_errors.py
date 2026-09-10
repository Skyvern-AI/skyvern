from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING

import anthropic
import litellm
import openai
from anthropic import AuthenticationError as AnthropicAuthenticationError
from anthropic import BadRequestError as AnthropicBadRequestError
from anthropic import NotFoundError as AnthropicNotFoundError
from anthropic import PermissionDeniedError as AnthropicPermissionDeniedError
from anthropic import UnprocessableEntityError as AnthropicUnprocessableEntityError
from litellm.exceptions import MidStreamFallbackError
from openai import AuthenticationError as OpenAIAuthenticationError
from openai import BadRequestError as OpenAIBadRequestError
from openai import NotFoundError as OpenAINotFoundError
from openai import PermissionDeniedError as OpenAIPermissionDeniedError
from openai import UnprocessableEntityError as OpenAIUnprocessableEntityError

if TYPE_CHECKING:
    from skyvern.forge.sdk.copilot.model_telemetry import CopilotModelStopMetadata


class CopilotEmptyCompletionError(RuntimeError):
    reason = "empty_completion"

    def __init__(
        self,
        *,
        llm_key: str,
        stop_metadata: CopilotModelStopMetadata,
        retry_allowed: bool = True,
    ) -> None:
        super().__init__(f"{self.reason}: {llm_key}")
        self.llm_key = llm_key
        self.stop_metadata = stop_metadata
        self.retry_allowed = retry_allowed


def _iter_classification_chain(exc: BaseException) -> Iterator[BaseException]:
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, MidStreamFallbackError) and current.original_exception is not None:
            current = current.original_exception
            continue
        yield current
        current = current.__cause__ or current.__context__


_RETRIABLE_LLM_ERROR_NAMES = {
    "APIConnectionError",
    "APIError",
    "APITimeoutError",
    "InternalServerError",
    "RateLimitError",
    "ServiceUnavailableError",
    "Timeout",
}
_RETRIABLE_LLM_ERROR_TEXT = (
    "connection error",
    "connection reset",
    "internal server error",
    "overloaded",
    "rate limit",
    "server error",
    "service unavailable",
    "temporarily unavailable",
    "timed out",
    "timeout",
)
_LLM_ERROR_MODULE_MARKERS = ("openai", "litellm", "anthropic")
_PERMANENT_LLM_ERRORS = (
    AnthropicAuthenticationError,
    AnthropicBadRequestError,
    AnthropicNotFoundError,
    AnthropicPermissionDeniedError,
    AnthropicUnprocessableEntityError,
    OpenAIAuthenticationError,
    OpenAIBadRequestError,
    OpenAINotFoundError,
    OpenAIPermissionDeniedError,
    OpenAIUnprocessableEntityError,
)


def is_retriable_llm_error(exc: BaseException) -> bool:
    chain = list(_iter_classification_chain(exc))
    for item in chain:
        if isinstance(item, CopilotEmptyCompletionError):
            return (
                item.retry_allowed
                and item.stop_metadata.refusal is not True
                and item.stop_metadata.content_filter is not True
            )
    for item in chain:
        if isinstance(item, _PERMANENT_LLM_ERRORS):
            return False
        if isinstance(
            item,
            (
                litellm.APIConnectionError,
                litellm.Timeout,
                litellm.RateLimitError,
                litellm.ServiceUnavailableError,
                litellm.InternalServerError,
                openai.APIConnectionError,
                openai.RateLimitError,
                openai.InternalServerError,
                anthropic.APIConnectionError,
                anthropic.RateLimitError,
                anthropic.InternalServerError,
            ),
        ):
            return True
        module = type(item).__module__.lower()
        name = type(item).__name__
        text = str(item).lower()
        module_has_llm_marker = any(marker in module for marker in _LLM_ERROR_MODULE_MARKERS)
        if name in _RETRIABLE_LLM_ERROR_NAMES and module_has_llm_marker:
            return True
        if module_has_llm_marker and any(phrase in text for phrase in _RETRIABLE_LLM_ERROR_TEXT):
            return True
    return False
