from typing import Any, Awaitable, Callable, ParamSpec, TypeVar

import litellm
from lmnr import Instruments, Laminar, LaminarLiteLLMCallback, observe

from skyvern.forge.sdk.trace.base import BaseTrace

P = ParamSpec("P")
R = TypeVar("R")


class LaminarTrace(BaseTrace):
    def __init__(self, api_key: str) -> None:
        Laminar.initialize(project_api_key=api_key, disabled_instruments={Instruments.SKYVERN, Instruments.PATCHRIGHT})
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

    def add_task_completion_tag(self, status: str) -> None:
        """Add a completion tag to the current trace based on task/workflow status."""
        try:
            # Get the current trace ID
            trace_id = Laminar.get_trace_id()
            if trace_id is None:
                return

            # Map status to appropriate tag
            status_tag_map = {
                "completed": "COMPLETED",
                "failed": "FAILURE",
                "timed_out": "TIMEOUT",
                "canceled": "CANCELED",
                "terminated": "TERMINATED",
            }

            tag = status_tag_map.get(status, "FAILURE")
            Laminar.set_span_tags([tag])
        except Exception:
            # Silently fail if tracing is not available or there's an error
            pass

    def add_experiment_metadata(self, experiment_data: dict[str, Any]) -> None:
        """Add experiment metadata to the current trace."""
        try:
            # Add experiment metadata to the current trace
            Laminar.set_trace_metadata(experiment_data)
        except Exception:
            # Silently fail if tracing is not available or there's an error
            pass
