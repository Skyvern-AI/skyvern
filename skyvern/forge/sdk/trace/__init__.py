from functools import wraps
from typing import Any, Awaitable, Callable, ParamSpec, TypeVar

from skyvern.forge import app
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.settings_manager import SettingsManager
from skyvern.forge.sdk.trace.base import BaseTrace, NoOpTrace
from skyvern.forge.sdk.trace.experiment_utils import collect_experiment_metadata_safely

P = ParamSpec("P")
R = TypeVar("R")


class TraceManager:
    __instance: BaseTrace = NoOpTrace()

    @staticmethod
    def traced_async(
        *,
        name: str | None = None,
        metadata: dict[str, Any] | None = None,
        tags: list[str] | None = None,
        **trace_parameters: Any,
    ) -> Callable[[Callable[P, Awaitable[R]]], Callable[P, Awaitable[R]]]:
        def decorator(func: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
            @wraps(func)
            async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
                new_metadata: dict[str, Any] = metadata or {}
                user_id: str | None = None
                context = skyvern_context.current()
                if context is not None:
                    new_metadata["request_id"] = context.request_id
                    new_metadata["organization_id"] = context.organization_id
                    new_metadata["task_id"] = context.task_id
                    new_metadata["workflow_id"] = context.workflow_id
                    new_metadata["workflow_run_id"] = context.workflow_run_id
                    new_metadata["task_v2_id"] = context.task_v2_id
                    new_metadata["run_id"] = context.run_id
                    new_metadata["organization_name"] = context.organization_name
                    user_id = context.run_id

                    # Collect experiment metadata and include it in the span metadata
                    experiment_metadata = await collect_experiment_metadata_safely(app.EXPERIMENTATION_PROVIDER)
                    if experiment_metadata:
                        new_metadata.update(experiment_metadata)

                new_tags: list[str] = tags or []
                new_tags.append(SettingsManager.get_settings().ENV)

                return await TraceManager.__instance.traced_async(
                    name=name, metadata=new_metadata, tags=new_tags, user_id=user_id, **trace_parameters
                )(func)(*args, **kwargs)

            return wrapper

        return decorator

    @staticmethod
    def traced(
        *,
        name: str | None = None,
        metadata: dict[str, Any] | None = None,
        tags: list[str] | None = None,
        **trace_parameters: Any,
    ) -> Callable[[Callable[P, R]], Callable[P, R]]:
        def decorator(func: Callable[P, R]) -> Callable[P, R]:
            def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
                new_metadata: dict[str, Any] = metadata or {}
                user_id: str | None = None
                context = skyvern_context.current()
                if context is not None:
                    new_metadata["request_id"] = context.request_id
                    new_metadata["organization_id"] = context.organization_id
                    new_metadata["task_id"] = context.task_id
                    new_metadata["workflow_id"] = context.workflow_id
                    new_metadata["workflow_run_id"] = context.workflow_run_id
                    new_metadata["task_v2_id"] = context.task_v2_id
                    new_metadata["run_id"] = context.run_id
                    new_metadata["organization_name"] = context.organization_name
                    user_id = context.run_id

                new_tags: list[str] = tags or []
                new_tags.append(SettingsManager.get_settings().ENV)

                return TraceManager.__instance.traced(
                    name=name, metadata=new_metadata, tags=new_tags, user_id=user_id, **trace_parameters
                )(func)(*args, **kwargs)

            return wrapper

        return decorator

    @staticmethod
    def get_trace_provider() -> BaseTrace:
        return TraceManager.__instance

    @staticmethod
    def set_trace_provider(trace_provider: BaseTrace) -> None:
        TraceManager.__instance = trace_provider

    @staticmethod
    def add_task_completion_tag(status: str) -> None:
        """Add a completion tag to the current trace based on task/workflow status."""
        TraceManager.__instance.add_task_completion_tag(status)

    @staticmethod
    def add_experiment_metadata(experiment_data: dict[str, Any]) -> None:
        """Add experiment metadata to the current trace."""
        TraceManager.__instance.add_experiment_metadata(experiment_data)
