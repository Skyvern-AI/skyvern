from __future__ import annotations

import warnings
from typing import TYPE_CHECKING, Any

from skyvern.schemas import llm as _llm

__all__ = [
    "LiteLLMParams",
    "LLMAllowedFailsPolicy",
    "LLMConfig",
    "LLMConfigBase",
    "LLMRouterConfig",
    "LLMRouterModelConfig",
]

_DEPRECATED_EXPORTS = frozenset(__all__)
_DEPRECATION_MESSAGE = (
    "skyvern.forge.sdk.api.llm.models is deprecated; import LLM types from skyvern.schemas.llm instead."
)

if TYPE_CHECKING:
    from skyvern.schemas.llm import (  # noqa: F401
        LiteLLMParams,
        LLMAllowedFailsPolicy,
        LLMConfig,
        LLMConfigBase,
        LLMRouterConfig,
        LLMRouterModelConfig,
    )


def __getattr__(name: str) -> Any:
    if name in _DEPRECATED_EXPORTS:
        warnings.warn(_DEPRECATION_MESSAGE, DeprecationWarning, stacklevel=2)
        return getattr(_llm, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
