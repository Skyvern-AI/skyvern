from abc import ABC, abstractmethod
from typing import Any, Awaitable, Callable, ParamSpec, TypeVar

P = ParamSpec("P")
R = TypeVar("R")


class BaseTrace(ABC):
    @abstractmethod
    def traced(
        self,
        name: str | None = None,
        metadata: dict[str, Any] | None = None,
        tags: list[str] | None = None,
        **kwargs: Any,
    ) -> Callable[[Callable[P, R]], Callable[P, R]]:
        pass

    @abstractmethod
    def traced_async(
        self,
        name: str | None = None,
        metadata: dict[str, Any] | None = None,
        tags: list[str] | None = None,
        **kwargs: Any,
    ) -> Callable[[Callable[P, Awaitable[R]]], Callable[P, Awaitable[R]]]:
        pass

    def add_task_completion_tag(self, status: str) -> None:
        """Add a completion tag to the current trace based on task/workflow status."""

    def add_experiment_metadata(self, experiment_data: dict[str, Any]) -> None:
        """Add experiment metadata to the current trace."""


class NoOpTrace(BaseTrace):
    def traced(
        self,
        name: str | None = None,
        metadata: dict[str, Any] | None = None,
        tags: list[str] | None = None,
        **kwargs: Any,
    ) -> Callable[[Callable[P, R]], Callable[P, R]]:
        return lambda func: func

    def traced_async(
        self,
        name: str | None = None,
        metadata: dict[str, Any] | None = None,
        tags: list[str] | None = None,
        **kwargs: Any,
    ) -> Callable[[Callable[P, Awaitable[R]]], Callable[P, Awaitable[R]]]:
        return lambda func: func
