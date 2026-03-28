"""Typed Protocol contracts for cross-repository dependencies."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from skyvern.forge.sdk.schemas.tasks import Task
    from skyvern.forge.sdk.workflow.models.parameter import WorkflowParameter
    from skyvern.forge.sdk.workflow.models.workflow import Workflow, WorkflowRun


@runtime_checkable
class TaskReader(Protocol):
    async def get_task(self, task_id: str, organization_id: str | None = None) -> Task | None: ...
    async def get_tasks_by_workflow_run_id(self, workflow_run_id: str) -> list[Task]: ...


@runtime_checkable
class WorkflowReader(Protocol):
    async def get_workflow_by_permanent_id(
        self,
        workflow_permanent_id: str,
        organization_id: str | None = None,
        version: int | None = None,
        ignore_version: int | None = None,
        filter_deleted: bool = True,
    ) -> Workflow | None: ...


@runtime_checkable
class WorkflowParameterReader(Protocol):
    async def get_workflow_parameter(
        self,
        workflow_parameter_id: str,
        organization_id: str | None = None,
    ) -> WorkflowParameter | None: ...


@runtime_checkable
class RunReader(Protocol):
    async def get_run(self, run_id: str, organization_id: str | None = None) -> WorkflowRun | None: ...
