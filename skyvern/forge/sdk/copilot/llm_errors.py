from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING

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


def iter_exception_chain(exc: BaseException) -> Iterator[BaseException]:
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
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


def is_retriable_llm_error(exc: BaseException) -> bool:
    for item in iter_exception_chain(exc):
        if isinstance(item, CopilotEmptyCompletionError):
            return (
                item.retry_allowed
                and item.stop_metadata.refusal is not True
                and item.stop_metadata.content_filter is not True
            )
        module = type(item).__module__.lower()
        name = type(item).__name__
        text = str(item).lower()
        module_has_llm_marker = any(marker in module for marker in _LLM_ERROR_MODULE_MARKERS)
        if name in _RETRIABLE_LLM_ERROR_NAMES and module_has_llm_marker:
            return True
        if module_has_llm_marker and any(phrase in text for phrase in _RETRIABLE_LLM_ERROR_TEXT):
            return True
    return False
