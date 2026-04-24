from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable

import structlog
from sqlalchemy import and_, delete, func, select, update
from sqlalchemy.exc import SQLAlchemyError

from skyvern.forge.sdk.db._error_handling import db_operation
from skyvern.forge.sdk.db.base_alchemy_db import read_retry
from skyvern.forge.sdk.db.base_repository import BaseRepository
from skyvern.forge.sdk.db.exceptions import NotFoundError

if TYPE_CHECKING:
    from skyvern.forge.sdk.db.base_alchemy_db import _SessionFactory

from skyvern.forge.sdk.db.models import (
    TaskV2Model,
    ThoughtModel,
    WorkflowRunBlockModel,
)
from skyvern.forge.sdk.db.protocols import TaskReader
from skyvern.forge.sdk.db.utils import (
    convert_to_task_v2,
    convert_to_workflow_run_block,
    serialize_proxy_location,
)
from skyvern.forge.sdk.schemas.task_v2 import TaskV2, TaskV2Status, Thought, ThoughtType
from skyvern.forge.sdk.schemas.workflow_runs import WorkflowRunBlock
from skyvern.schemas.runs import ProxyLocationInput, RunEngine
from skyvern.schemas.workflows import BlockStatus, BlockType

LOG = structlog.get_logger()


class ObserverRepository(BaseRepository):
    """Database operations for observer tasks (TaskV2), thoughts, and workflow run blocks."""

    def __init__(
        self,
        session_factory: _SessionFactory,
        debug_enabled: bool = False,
        is_retryable_error_fn: Callable[[SQLAlchemyError], bool] | None = None,
        task_reader: TaskReader | None = None,
    ) -> None:
        super().__init__(session_factory, debug_enabled, is_retryable_error_fn)
        self._task_reader = task_reader

    @read_retry()
    @db_operation("get_task_v2", log_errors=False)
    async def get_task_v2(self, task_v2_id: str, organization_id: str | None = None) -> TaskV2 | None:
        async with self.Session() as session:
            if task_v2 := (
                await session.scalars(
                    select(TaskV2Model)
                    .filter_by(observer_cruise_id=task_v2_id)
                    .filter_by(organization_id=organization_id)
                )
            ).first():
                return convert_to_task_v2(task_v2, debug_enabled=self.debug_enabled)
            return None

    @db_operation("delete_thoughts")
    async def delete_thoughts(self, task_v2_id: str, organization_id: str | None = None) -> None:
        async with self.Session() as session:
            stmt = delete(ThoughtModel).where(
                and_(
                    ThoughtModel.observer_cruise_id == task_v2_id,
                    ThoughtModel.organization_id == organization_id,
                )
            )
            await session.execute(stmt)
            await session.commit()

    @db_operation("get_task_v2_by_workflow_run_id")
    async def get_task_v2_by_workflow_run_id(
        self,
        workflow_run_id: str,
        organization_id: str | None = None,
    ) -> TaskV2 | None:
        async with self.Session() as session:
            if task_v2 := (
                await session.scalars(
                    select(TaskV2Model)
                    .filter_by(organization_id=organization_id)
                    .filter_by(workflow_run_id=workflow_run_id)
                )
            ).first():
                return convert_to_task_v2(task_v2, debug_enabled=self.debug_enabled)
            return None

    @db_operation("get_thought")
    async def get_thought(self, thought_id: str, organization_id: str | None = None) -> Thought | None:
        async with self.Session() as session:
            if thought := (
                await session.scalars(
                    select(ThoughtModel)
                    .filter_by(observer_thought_id=thought_id)
                    .filter_by(organization_id=organization_id)
                )
            ).first():
                return Thought.model_validate(thought)
            return None

    @db_operation("get_thoughts")
    async def get_thoughts(
        self,
        *,
        task_v2_id: str,
        thought_types: list[ThoughtType],
        organization_id: str,
    ) -> list[Thought]:
        async with self.Session() as session:
            query = (
                select(ThoughtModel)
                .filter_by(observer_cruise_id=task_v2_id)
                .filter_by(organization_id=organization_id)
                .order_by(ThoughtModel.created_at)
            )
            if thought_types:
                query = query.filter(ThoughtModel.observer_thought_type.in_(thought_types))
            thoughts = (await session.scalars(query)).all()
            return [Thought.model_validate(thought) for thought in thoughts]

    @db_operation("get_thought_cost_sum_by_workflow_run_id")
    async def get_thought_cost_sum_by_workflow_run_id(self, workflow_run_id: str, organization_id: str) -> float:
        """Sum `thought_cost` across all thoughts for the given workflow_run_id.

        Returns 0.0 for runs without task_v2 planning.
        """
        async with self.Session() as session:
            query = (
                select(func.coalesce(func.sum(ThoughtModel.thought_cost), 0))
                .where(ThoughtModel.workflow_run_id == workflow_run_id)
                .where(ThoughtModel.organization_id == organization_id)
            )
            total = (await session.execute(query)).scalar_one()
            return float(total)

    @db_operation("get_block_llm_cost_sum_by_workflow_run_id")
    async def get_block_llm_cost_sum_by_workflow_run_id(self, workflow_run_id: str, organization_id: str) -> float:
        """Sum `llm_cost` across all workflow_run_blocks for this workflow_run_id."""
        async with self.Session() as session:
            query = (
                select(func.coalesce(func.sum(WorkflowRunBlockModel.llm_cost), 0))
                .where(WorkflowRunBlockModel.workflow_run_id == workflow_run_id)
                .where(WorkflowRunBlockModel.organization_id == organization_id)
            )
            total = (await session.execute(query)).scalar_one()
            return float(total)

    @db_operation("increment_workflow_run_block_llm_cost")
    async def increment_workflow_run_block_llm_cost(
        self,
        workflow_run_block_id: str,
        organization_id: str,
        amount: float,
    ) -> None:
        """Atomically add `amount` to `workflow_run_blocks.llm_cost`.

        Single SQL UPDATE so concurrent writers don't lose increments.
        No-op for non-positive `amount`.
        """
        if amount <= 0:
            return
        async with self.Session() as session:
            stmt = (
                update(WorkflowRunBlockModel)
                .where(WorkflowRunBlockModel.workflow_run_block_id == workflow_run_block_id)
                .where(WorkflowRunBlockModel.organization_id == organization_id)
                .values(llm_cost=WorkflowRunBlockModel.llm_cost + amount)
            )
            result = await session.execute(stmt)
            await session.commit()
            if result.rowcount == 0:
                LOG.warning(
                    "Block LLM cost increment matched zero rows — cost dropped",
                    workflow_run_block_id=workflow_run_block_id,
                    organization_id=organization_id,
                    amount=amount,
                )

    @db_operation("create_task_v2")
    async def create_task_v2(
        self,
        workflow_run_id: str | None = None,
        workflow_id: str | None = None,
        workflow_permanent_id: str | None = None,
        prompt: str | None = None,
        url: str | None = None,
        organization_id: str | None = None,
        proxy_location: ProxyLocationInput = None,
        totp_identifier: str | None = None,
        totp_verification_url: str | None = None,
        webhook_callback_url: str | None = None,
        extracted_information_schema: dict | list | str | None = None,
        error_code_mapping: dict | None = None,
        model: dict[str, Any] | None = None,
        max_screenshot_scrolling_times: int | None = None,
        extra_http_headers: dict[str, str] | None = None,
        browser_address: str | None = None,
        run_with: str | None = None,
    ) -> TaskV2:
        async with self.Session() as session:
            new_task_v2 = TaskV2Model(
                workflow_run_id=workflow_run_id,
                workflow_id=workflow_id,
                workflow_permanent_id=workflow_permanent_id,
                prompt=prompt,
                url=url,
                proxy_location=serialize_proxy_location(proxy_location),
                totp_identifier=totp_identifier,
                totp_verification_url=totp_verification_url,
                webhook_callback_url=webhook_callback_url,
                extracted_information_schema=extracted_information_schema,
                error_code_mapping=error_code_mapping,
                organization_id=organization_id,
                model=model,
                max_screenshot_scrolling_times=max_screenshot_scrolling_times,
                extra_http_headers=extra_http_headers,
                browser_address=browser_address,
                run_with=run_with,
            )
            session.add(new_task_v2)
            await session.commit()
            await session.refresh(new_task_v2)
            return convert_to_task_v2(new_task_v2, debug_enabled=self.debug_enabled)

    @db_operation("create_thought")
    async def create_thought(
        self,
        task_v2_id: str,
        workflow_run_id: str | None = None,
        workflow_id: str | None = None,
        workflow_permanent_id: str | None = None,
        workflow_run_block_id: str | None = None,
        user_input: str | None = None,
        observation: str | None = None,
        thought: str | None = None,
        answer: str | None = None,
        thought_scenario: str | None = None,
        thought_type: str = ThoughtType.plan,
        output: dict[str, Any] | None = None,
        input_token_count: int | None = None,
        output_token_count: int | None = None,
        reasoning_token_count: int | None = None,
        cached_token_count: int | None = None,
        thought_cost: float | None = None,
        organization_id: str | None = None,
    ) -> Thought:
        async with self.Session() as session:
            new_thought = ThoughtModel(
                observer_cruise_id=task_v2_id,
                workflow_run_id=workflow_run_id,
                workflow_id=workflow_id,
                workflow_permanent_id=workflow_permanent_id,
                workflow_run_block_id=workflow_run_block_id,
                user_input=user_input,
                observation=observation,
                thought=thought,
                answer=answer,
                observer_thought_scenario=thought_scenario,
                observer_thought_type=thought_type,
                output=output,
                input_token_count=input_token_count,
                output_token_count=output_token_count,
                reasoning_token_count=reasoning_token_count,
                cached_token_count=cached_token_count,
                thought_cost=thought_cost,
                organization_id=organization_id,
            )
            session.add(new_thought)
            await session.commit()
            await session.refresh(new_thought)
            return Thought.model_validate(new_thought)

    @db_operation("update_thought")
    async def update_thought(
        self,
        thought_id: str,
        workflow_run_block_id: str | None = None,
        workflow_run_id: str | None = None,
        workflow_id: str | None = None,
        workflow_permanent_id: str | None = None,
        observation: str | None = None,
        thought: str | None = None,
        answer: str | None = None,
        output: dict[str, Any] | None = None,
        input_token_count: int | None = None,
        output_token_count: int | None = None,
        reasoning_token_count: int | None = None,
        cached_token_count: int | None = None,
        thought_cost: float | None = None,
        organization_id: str | None = None,
        last_llm_model: str | None = None,
    ) -> Thought:
        async with self.Session() as session:
            thought_obj = (
                await session.scalars(
                    select(ThoughtModel)
                    .filter_by(observer_thought_id=thought_id)
                    .filter_by(organization_id=organization_id)
                )
            ).first()
            if thought_obj:
                if workflow_run_block_id:
                    thought_obj.workflow_run_block_id = workflow_run_block_id
                if workflow_run_id:
                    thought_obj.workflow_run_id = workflow_run_id
                if workflow_id:
                    thought_obj.workflow_id = workflow_id
                if workflow_permanent_id:
                    thought_obj.workflow_permanent_id = workflow_permanent_id
                if observation:
                    thought_obj.observation = observation
                if thought:
                    thought_obj.thought = thought
                if answer:
                    thought_obj.answer = answer
                if output:
                    thought_obj.output = output
                if input_token_count is not None:
                    thought_obj.input_token_count = input_token_count
                if output_token_count is not None:
                    thought_obj.output_token_count = output_token_count
                if reasoning_token_count is not None:
                    thought_obj.reasoning_token_count = reasoning_token_count
                if cached_token_count is not None:
                    thought_obj.cached_token_count = cached_token_count
                if thought_cost is not None:
                    thought_obj.thought_cost = thought_cost
                if last_llm_model is not None:
                    thought_obj.last_llm_model = last_llm_model
                await session.commit()
                await session.refresh(thought_obj)
                return Thought.model_validate(thought_obj)
            raise NotFoundError(f"Thought {thought_id}")

    @db_operation("update_task_v2")
    async def update_task_v2(
        self,
        task_v2_id: str,
        status: TaskV2Status | None = None,
        workflow_run_id: str | None = None,
        workflow_id: str | None = None,
        workflow_permanent_id: str | None = None,
        url: str | None = None,
        prompt: str | None = None,
        summary: str | None = None,
        output: dict[str, Any] | None = None,
        organization_id: str | None = None,
        webhook_failure_reason: str | None = None,
        failure_category: list[dict[str, Any]] | None = None,
    ) -> TaskV2:
        async with self.Session() as session:
            task_v2 = (
                await session.scalars(
                    select(TaskV2Model)
                    .filter_by(observer_cruise_id=task_v2_id)
                    .filter_by(organization_id=organization_id)
                )
            ).first()
            if task_v2:
                if status:
                    task_v2.status = status
                    if status == TaskV2Status.queued and task_v2.queued_at is None:
                        task_v2.queued_at = datetime.now(timezone.utc)
                    if status == TaskV2Status.running and task_v2.started_at is None:
                        task_v2.started_at = datetime.now(timezone.utc)
                    if status.is_final() and task_v2.finished_at is None:
                        task_v2.finished_at = datetime.now(timezone.utc)
                if workflow_run_id:
                    task_v2.workflow_run_id = workflow_run_id
                if workflow_id:
                    task_v2.workflow_id = workflow_id
                if workflow_permanent_id:
                    task_v2.workflow_permanent_id = workflow_permanent_id
                if url:
                    task_v2.url = url
                if prompt:
                    task_v2.prompt = prompt
                if summary:
                    task_v2.summary = summary
                if output:
                    task_v2.output = output
                if webhook_failure_reason is not None:
                    task_v2.webhook_failure_reason = webhook_failure_reason
                if failure_category is not None:
                    task_v2.failure_category = failure_category
                await session.commit()
                await session.refresh(task_v2)
                return convert_to_task_v2(task_v2, debug_enabled=self.debug_enabled)
            raise NotFoundError(f"TaskV2 {task_v2_id} not found")

    @db_operation("create_workflow_run_block")
    async def create_workflow_run_block(
        self,
        workflow_run_id: str,
        parent_workflow_run_block_id: str | None = None,
        organization_id: str | None = None,
        task_id: str | None = None,
        label: str | None = None,
        block_type: BlockType | None = None,
        status: BlockStatus = BlockStatus.running,
        output: dict | list | str | None = None,
        continue_on_failure: bool = False,
        engine: RunEngine | None = None,
        current_value: str | None = None,
        current_index: int | None = None,
    ) -> WorkflowRunBlock:
        async with self.Session() as session:
            new_workflow_run_block = WorkflowRunBlockModel(
                workflow_run_id=workflow_run_id,
                parent_workflow_run_block_id=parent_workflow_run_block_id,
                organization_id=organization_id,
                task_id=task_id,
                label=label,
                block_type=block_type,
                status=status,
                output=output,
                continue_on_failure=continue_on_failure,
                engine=engine,
                current_value=current_value,
                current_index=current_index,
            )
            session.add(new_workflow_run_block)
            await session.commit()
            await session.refresh(new_workflow_run_block)

        task = None
        if task_id:
            if self._task_reader is None:
                raise RuntimeError("task_reader dependency not set")
            task = await self._task_reader.get_task(task_id, organization_id=organization_id)
        return convert_to_workflow_run_block(new_workflow_run_block, task=task)

    @db_operation("delete_workflow_run_blocks")
    async def delete_workflow_run_blocks(self, workflow_run_id: str, organization_id: str | None = None) -> None:
        async with self.Session() as session:
            stmt = delete(WorkflowRunBlockModel).where(
                and_(
                    WorkflowRunBlockModel.workflow_run_id == workflow_run_id,
                    WorkflowRunBlockModel.organization_id == organization_id,
                )
            )
            await session.execute(stmt)
            await session.commit()

    @db_operation("update_workflow_run_block")
    async def update_workflow_run_block(
        self,
        workflow_run_block_id: str,
        organization_id: str | None = None,
        status: BlockStatus | None = None,
        output: dict | list | str | None = None,
        failure_reason: str | None = None,
        task_id: str | None = None,
        loop_values: list | None = None,
        current_value: str | None = None,
        current_index: int | None = None,
        recipients: list[str] | None = None,
        attachments: list[str] | None = None,
        subject: str | None = None,
        body: str | None = None,
        prompt: str | None = None,
        wait_sec: int | None = None,
        description: str | None = None,
        block_workflow_run_id: str | None = None,
        engine: str | None = None,
        # HTTP request block parameters
        http_request_method: str | None = None,
        http_request_url: str | None = None,
        http_request_headers: dict[str, str] | None = None,
        http_request_body: dict[str, Any] | None = None,
        http_request_parameters: dict[str, Any] | None = None,
        http_request_timeout: int | None = None,
        http_request_follow_redirects: bool | None = None,
        ai_fallback_triggered: bool | None = None,
        # block-level error codes (e.g. ["FILE_PARSER_ERROR"])
        error_codes: list[str] | None = None,
        # human interaction block
        instructions: str | None = None,
        positive_descriptor: str | None = None,
        negative_descriptor: str | None = None,
        # conditional block
        executed_branch_id: str | None = None,
        executed_branch_expression: str | None = None,
        executed_branch_result: bool | None = None,
        executed_branch_next_block: str | None = None,
    ) -> WorkflowRunBlock:
        async with self.Session() as session:
            workflow_run_block = (
                await session.scalars(
                    select(WorkflowRunBlockModel)
                    .filter_by(workflow_run_block_id=workflow_run_block_id)
                    .filter_by(organization_id=organization_id)
                )
            ).first()
            if workflow_run_block:
                if status:
                    workflow_run_block.status = status
                if output:
                    workflow_run_block.output = output
                if task_id:
                    workflow_run_block.task_id = task_id
                if failure_reason:
                    workflow_run_block.failure_reason = failure_reason
                # Use `is not None` instead of truthiness checks so that falsy
                # values like current_index=0, empty loop_values=[], or
                # current_value="" are correctly persisted. Without this,
                # the first loop iteration (index 0) loses its metadata.
                if loop_values is not None:
                    workflow_run_block.loop_values = loop_values
                if current_value is not None:
                    workflow_run_block.current_value = current_value
                if current_index is not None:
                    workflow_run_block.current_index = current_index
                if recipients:
                    workflow_run_block.recipients = recipients
                if attachments:
                    workflow_run_block.attachments = attachments
                if subject:
                    workflow_run_block.subject = subject
                if body:
                    workflow_run_block.body = body
                if prompt:
                    workflow_run_block.prompt = prompt
                if wait_sec:
                    workflow_run_block.wait_sec = wait_sec
                if description:
                    workflow_run_block.description = description
                if block_workflow_run_id:
                    workflow_run_block.block_workflow_run_id = block_workflow_run_id
                if engine:
                    workflow_run_block.engine = engine
                # HTTP request block fields
                if http_request_method:
                    workflow_run_block.http_request_method = http_request_method
                if http_request_url:
                    workflow_run_block.http_request_url = http_request_url
                if http_request_headers:
                    workflow_run_block.http_request_headers = http_request_headers
                if http_request_body:
                    workflow_run_block.http_request_body = http_request_body
                if http_request_parameters:
                    workflow_run_block.http_request_parameters = http_request_parameters
                if http_request_timeout:
                    workflow_run_block.http_request_timeout = http_request_timeout
                if http_request_follow_redirects is not None:
                    workflow_run_block.http_request_follow_redirects = http_request_follow_redirects
                if ai_fallback_triggered is not None:
                    workflow_run_block.script_run = {"ai_fallback_triggered": ai_fallback_triggered}
                if error_codes is not None:
                    workflow_run_block.error_codes = error_codes
                # human interaction block fields
                if instructions:
                    workflow_run_block.instructions = instructions
                if positive_descriptor:
                    workflow_run_block.positive_descriptor = positive_descriptor
                if negative_descriptor:
                    workflow_run_block.negative_descriptor = negative_descriptor
                # conditional block fields
                if executed_branch_id:
                    workflow_run_block.executed_branch_id = executed_branch_id
                if executed_branch_expression is not None:
                    workflow_run_block.executed_branch_expression = executed_branch_expression
                if executed_branch_result is not None:
                    workflow_run_block.executed_branch_result = executed_branch_result
                if executed_branch_next_block is not None:
                    workflow_run_block.executed_branch_next_block = executed_branch_next_block
                await session.commit()
                await session.refresh(workflow_run_block)
            else:
                raise NotFoundError(f"WorkflowRunBlock {workflow_run_block_id} not found")
        task = None
        task_id = workflow_run_block.task_id
        if task_id:
            if self._task_reader is None:
                raise RuntimeError("task_reader dependency not set")
            task = await self._task_reader.get_task(task_id, organization_id=workflow_run_block.organization_id)
        return convert_to_workflow_run_block(workflow_run_block, task=task)

    @db_operation("get_workflow_run_block")
    async def get_workflow_run_block(
        self,
        workflow_run_block_id: str,
        organization_id: str | None = None,
    ) -> WorkflowRunBlock:
        async with self.Session() as session:
            workflow_run_block = (
                await session.scalars(
                    select(WorkflowRunBlockModel)
                    .filter_by(workflow_run_block_id=workflow_run_block_id)
                    .filter_by(organization_id=organization_id)
                )
            ).first()
            if workflow_run_block:
                task = None
                task_id = workflow_run_block.task_id
                if task_id:
                    if self._task_reader is None:
                        raise RuntimeError("task_reader dependency not set")
                    task = await self._task_reader.get_task(task_id, organization_id=organization_id)
                return convert_to_workflow_run_block(workflow_run_block, task=task)
            raise NotFoundError(f"WorkflowRunBlock {workflow_run_block_id} not found")

    @db_operation("get_workflow_run_block_by_task_id")
    async def get_workflow_run_block_by_task_id(
        self,
        task_id: str,
        organization_id: str | None = None,
    ) -> WorkflowRunBlock:
        async with self.Session() as session:
            workflow_run_block = (
                await session.scalars(
                    select(WorkflowRunBlockModel).filter_by(task_id=task_id).filter_by(organization_id=organization_id)
                )
            ).first()
            if workflow_run_block:
                task = None
                task_id = workflow_run_block.task_id
                if task_id:
                    if self._task_reader is None:
                        raise RuntimeError("task_reader dependency not set")
                    task = await self._task_reader.get_task(task_id, organization_id=organization_id)
                return convert_to_workflow_run_block(workflow_run_block, task=task)
            raise NotFoundError(f"WorkflowRunBlock not found by {task_id}")

    @db_operation("get_workflow_run_blocks")
    async def get_workflow_run_blocks(
        self,
        workflow_run_id: str,
        organization_id: str | None = None,
    ) -> list[WorkflowRunBlock]:
        async with self.Session() as session:
            workflow_run_blocks = (
                await session.scalars(
                    select(WorkflowRunBlockModel)
                    .filter_by(workflow_run_id=workflow_run_id)
                    .filter_by(organization_id=organization_id)
                    .order_by(WorkflowRunBlockModel.created_at.desc())
                )
            ).all()
            if self._task_reader is None:
                raise RuntimeError("task_reader dependency not set")
            tasks = await self._task_reader.get_tasks_by_workflow_run_id(workflow_run_id)
            tasks_dict = {task.task_id: task for task in tasks}
            return [
                convert_to_workflow_run_block(workflow_run_block, task=tasks_dict.get(workflow_run_block.task_id))
                for workflow_run_block in workflow_run_blocks
            ]
