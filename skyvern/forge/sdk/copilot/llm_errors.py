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
        # ``__context__`` only records that another error was being handled when this one was
        # raised, so a failure inherited from an earlier attempt is not evidence about this one.
        current = current.__cause__


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
_TRANSIENT_LLM_ERRORS = (
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
)


def _is_transient_provider_item(exc: BaseException) -> bool:
    if isinstance(exc, _TRANSIENT_LLM_ERRORS):
        return True
    if not any(marker in type(exc).__module__.lower() for marker in _LLM_ERROR_MODULE_MARKERS):
        return False
    if type(exc).__name__ in _RETRIABLE_LLM_ERROR_NAMES:
        return True
    return any(phrase in str(exc).lower() for phrase in _RETRIABLE_LLM_ERROR_TEXT)


def is_transient_provider_error(exc: BaseException) -> bool:
    for item in _iter_classification_chain(exc):
        if isinstance(item, _PERMANENT_LLM_ERRORS):
            return False
        if _is_transient_provider_item(item):
            return True
    return False


def is_retriable_llm_error(exc: BaseException) -> bool:
    for item in _iter_classification_chain(exc):
        if isinstance(item, CopilotEmptyCompletionError):
            return (
                item.retry_allowed
                and item.stop_metadata.refusal is not True
                and item.stop_metadata.content_filter is not True
            )
    return is_transient_provider_error(exc)
