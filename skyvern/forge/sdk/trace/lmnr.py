from typing import Any, Awaitable, Callable, ParamSpec, TypeVar

import litellm
from lmnr import Instruments, Laminar, LaminarLiteLLMCallback, observe

from skyvern.forge.sdk.trace.base import BaseTrace

P = ParamSpec("P")
R = TypeVar("R")


class LaminarTrace(BaseTrace):
    def __init__(self, api_key: str) -> None:
        Laminar.initialize(project_api_key=api_key, disabled_instruments={Instruments.SKYVERN})
        litellm.callbacks.append(LaminarLiteLLMCallback())

    def traced(
        self,
        name: str | None = None,
        metadata: dict[str, Any] | None = None,
        tags: list[str] | None = None,
        **kwargs: Any,
    ) -> Callable[[Callable[P, R]], Callable[P, R]]:
        return observe(name=name, ignore_output=True, metadata=metadata, tags=tags, **kwargs)

    def traced_async(
        self,
        name: str | None = None,
        metadata: dict[str, Any] | None = None,
        tags: list[str] | None = None,
        **kwargs: Any,
    ) -> Callable[[Callable[P, Awaitable[R]]], Callable[P, Awaitable[R]]]:
        return observe(name=name, ignore_output=True, metadata=metadata, tags=tags, **kwargs)
