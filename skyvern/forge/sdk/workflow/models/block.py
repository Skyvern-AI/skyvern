import abc
from enum import StrEnum
from typing import Annotated, Any, Literal, Union

import structlog
from pydantic import BaseModel, Field

from skyvern.exceptions import (
    ContextParameterValueNotFound,
    MissingBrowserStatePage,
    TaskNotFound,
    UnexpectedTaskStatus,
)
from skyvern.forge import app
from skyvern.forge.sdk.schemas.tasks import TaskStatus
from skyvern.forge.sdk.workflow.context_manager import WorkflowRunContext
from skyvern.forge.sdk.workflow.models.parameter import PARAMETER_TYPE, ContextParameter, WorkflowParameter

LOG = structlog.get_logger()


class BlockType(StrEnum):
    TASK = "task"
    FOR_LOOP = "for_loop"


class Block(BaseModel, abc.ABC):
    block_type: BlockType
    parent_block_id: str | None = None
    next_block_id: str | None = None

    @classmethod
    def get_subclasses(cls) -> tuple[type["Block"], ...]:
        return tuple(cls.__subclasses__())

    @staticmethod
    def get_workflow_run_context(workflow_run_id: str) -> WorkflowRunContext:
        return app.WORKFLOW_CONTEXT_MANAGER.get_workflow_run_context(workflow_run_id)

    @abc.abstractmethod
    async def execute(self, workflow_run_id: str, **kwargs: dict) -> Any:
        pass

    @abc.abstractmethod
    def get_all_parameters(
        self,
    ) -> list[PARAMETER_TYPE]:
        pass


class TaskBlock(Block):
    block_type: Literal[BlockType.TASK] = BlockType.TASK

    url: str | None = None
    title: str = "Untitled Task"
    navigation_goal: str | None = None
    data_extraction_goal: str | None = None
    data_schema: dict[str, Any] | None = None
    # error code to error description for the LLM
    error_code_mapping: dict[str, str] | None = None
    max_retries: int = 0
    parameters: list[PARAMETER_TYPE] = []

    def get_all_parameters(
        self,
    ) -> list[PARAMETER_TYPE]:
        return self.parameters

    @staticmethod
    async def get_task_order(workflow_run_id: str, current_retry: int) -> tuple[int, int]:
        """
        Returns the order and retry for the next task in the workflow run as a tuple.
        """
        last_task_for_workflow_run = await app.DATABASE.get_last_task_for_workflow_run(workflow_run_id=workflow_run_id)
        # If there is no previous task, the order will be 0 and the retry will be 0.
        if last_task_for_workflow_run is None:
            return 0, 0
        # If there is a previous task but the current retry is 0, the order will be the order of the last task + 1
        # and the retry will be 0.
        order = last_task_for_workflow_run.order or 0
        if current_retry == 0:
            return order + 1, 0
        # If there is a previous task and the current retry is not 0, the order will be the order of the last task
        # and the retry will be the retry of the last task + 1. (There is a validation that makes sure the retry
        # of the last task is equal to current_retry - 1) if it is not, we use last task retry + 1.
        retry = last_task_for_workflow_run.retry or 0
        if retry + 1 != current_retry:
            LOG.error(
                f"Last task for workflow run is retry number {last_task_for_workflow_run.retry}, "
                f"but current retry is {current_retry}. Could be race condition. Using last task retry + 1",
                workflow_run_id=workflow_run_id,
                last_task_id=last_task_for_workflow_run.task_id,
                last_task_retry=last_task_for_workflow_run.retry,
                current_retry=current_retry,
            )

        return order, retry + 1

    async def execute(self, workflow_run_id: str, **kwargs: dict) -> Any:
        workflow_run_context = self.get_workflow_run_context(workflow_run_id)
        current_retry = 0
        # initial value for will_retry is True, so that the loop runs at least once
        will_retry = True
        workflow_run = await app.WORKFLOW_SERVICE.get_workflow_run(workflow_run_id=workflow_run_id)
        workflow = await app.WORKFLOW_SERVICE.get_workflow(workflow_id=workflow_run.workflow_id)
        # TODO (kerem) we should always retry on terminated. We should make a distinction between retriable and
        # non-retryable terminations
        while will_retry:
            task_order, task_retry = await self.get_task_order(workflow_run_id, current_retry)
            task, step = await app.agent.create_task_and_step_from_block(
                task_block=self,
                workflow=workflow,
                workflow_run=workflow_run,
                workflow_run_context=workflow_run_context,
                task_order=task_order,
                task_retry=task_retry,
            )
            organization = await app.DATABASE.get_organization(organization_id=workflow.organization_id)
            if not organization:
                raise Exception(f"Organization is missing organization_id={workflow.organization_id}")
            browser_state = await app.BROWSER_MANAGER.get_or_create_for_workflow_run(
                workflow_run=workflow_run, url=self.url
            )
            if not browser_state.page:
                LOG.error("BrowserState has no page", workflow_run_id=workflow_run.workflow_run_id)
                raise MissingBrowserStatePage(workflow_run_id=workflow_run.workflow_run_id)

            LOG.info(
                f"Navigating to page",
                url=self.url,
                workflow_run_id=workflow_run_id,
                task_id=task.task_id,
                workflow_id=workflow.workflow_id,
                organization_id=workflow.organization_id,
                step_id=step.step_id,
            )

            if self.url:
                await browser_state.page.goto(self.url)

            try:
                await app.agent.execute_step(organization=organization, task=task, step=step, workflow_run=workflow_run)
            except Exception as e:
                # Make sure the task is marked as failed in the database before raising the exception
                await app.DATABASE.update_task(
                    task.task_id,
                    status=TaskStatus.failed,
                    organization_id=workflow.organization_id,
                    failure_reason=str(e),
                )
                raise e

            # Check task status
            updated_task = await app.DATABASE.get_task(task_id=task.task_id, organization_id=workflow.organization_id)
            if not updated_task:
                raise TaskNotFound(task.task_id)
            if not updated_task.status.is_final():
                raise UnexpectedTaskStatus(task_id=updated_task.task_id, status=updated_task.status)
            if updated_task.status == TaskStatus.completed:
                will_retry = False
            else:
                current_retry += 1
                will_retry = current_retry <= self.max_retries
                retry_message = f", retrying task {current_retry}/{self.max_retries}" if will_retry else ""
                LOG.warning(
                    f"Task failed with status {updated_task.status}{retry_message}",
                    task_id=updated_task.task_id,
                    status=updated_task.status,
                    workflow_run_id=workflow_run_id,
                    workflow_id=workflow.workflow_id,
                    organization_id=workflow.organization_id,
                    current_retry=current_retry,
                    max_retries=self.max_retries,
                )


class ForLoopBlock(Block):
    block_type: Literal[BlockType.FOR_LOOP] = BlockType.FOR_LOOP

    # TODO (kerem): Add support for ContextParameter
    loop_over: PARAMETER_TYPE
    loop_block: "BlockTypeVar"

    def get_all_parameters(
        self,
    ) -> list[PARAMETER_TYPE]:
        return self.loop_block.get_all_parameters() + [self.loop_over]

    def get_loop_block_context_parameters(self, workflow_run_id: str, loop_data: Any) -> list[ContextParameter]:
        if not isinstance(loop_data, dict):
            # TODO (kerem): Should we add support for other types?
            raise ValueError("loop_data should be a dictionary")

        loop_block_parameters = self.loop_block.get_all_parameters()
        context_parameters = [
            parameter for parameter in loop_block_parameters if isinstance(parameter, ContextParameter)
        ]
        for context_parameter in context_parameters:
            if context_parameter.key not in loop_data:
                raise ContextParameterValueNotFound(
                    parameter_key=context_parameter.key,
                    existing_keys=list(loop_data.keys()),
                    workflow_run_id=workflow_run_id,
                )
            context_parameter.value = loop_data[context_parameter.key]

        return context_parameters

    def get_loop_over_parameter_values(self, workflow_run_context: WorkflowRunContext) -> list[Any]:
        if isinstance(self.loop_over, WorkflowParameter):
            parameter_value = workflow_run_context.get_value(self.loop_over.key)
            if isinstance(parameter_value, list):
                return parameter_value
            else:
                # TODO (kerem): Should we raise an error here?
                return [parameter_value]
        else:
            # TODO (kerem): Implement this for context parameters
            raise NotImplementedError

    async def execute(self, workflow_run_id: str, **kwargs: dict) -> Any:
        workflow_run_context = self.get_workflow_run_context(workflow_run_id)
        loop_over_values = self.get_loop_over_parameter_values(workflow_run_context)
        LOG.info(
            f"Number of loop_over values: {len(loop_over_values)}",
            block_type=self.block_type,
            workflow_run_id=workflow_run_id,
            num_loop_over_values=len(loop_over_values),
        )
        for loop_over_value in loop_over_values:
            context_parameters_with_value = self.get_loop_block_context_parameters(workflow_run_id, loop_over_value)
            for context_parameter in context_parameters_with_value:
                workflow_run_context.set_value(context_parameter.key, context_parameter.value)
            await self.loop_block.execute(workflow_run_id=workflow_run_id)

        return None


BlockSubclasses = Union[ForLoopBlock, TaskBlock]
BlockTypeVar = Annotated[BlockSubclasses, Field(discriminator="block_type")]
