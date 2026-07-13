from __future__ import annotations

from collections.abc import Iterator


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
        module = type(item).__module__.lower()
        name = type(item).__name__
        text = str(item).lower()
        module_has_llm_marker = any(marker in module for marker in _LLM_ERROR_MODULE_MARKERS)
        if name in _RETRIABLE_LLM_ERROR_NAMES and module_has_llm_marker:
            return True
        if module_has_llm_marker and any(phrase in text for phrase in _RETRIABLE_LLM_ERROR_TEXT):
            return True
    return False
