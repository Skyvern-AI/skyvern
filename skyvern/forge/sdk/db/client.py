import json
from datetime import datetime, timedelta
from typing import Any, List, Optional, Sequence

import structlog
from sqlalchemy import and_, delete, distinct, func, select, tuple_, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from skyvern.config import settings
from skyvern.exceptions import WorkflowParameterNotFound
from skyvern.forge.sdk.artifact.models import Artifact, ArtifactType
from skyvern.forge.sdk.db.enums import OrganizationAuthTokenType, TaskType
from skyvern.forge.sdk.db.exceptions import NotFoundError
from skyvern.forge.sdk.db.models import (
    ActionModel,
    AISuggestionModel,
    ArtifactModel,
    AWSSecretParameterModel,
    BitwardenCreditCardDataParameterModel,
    BitwardenLoginCredentialParameterModel,
    BitwardenSensitiveInformationParameterModel,
    CredentialModel,
    CredentialParameterModel,
    OrganizationAuthTokenModel,
    OrganizationBitwardenCollectionModel,
    OrganizationModel,
    OutputParameterModel,
    PersistentBrowserSessionModel,
    StepModel,
    TaskGenerationModel,
    TaskModel,
    TaskRunModel,
    TaskV2Model,
    ThoughtModel,
    TOTPCodeModel,
    WorkflowModel,
    WorkflowParameterModel,
    WorkflowRunBlockModel,
    WorkflowRunModel,
    WorkflowRunOutputParameterModel,
    WorkflowRunParameterModel,
)
from skyvern.forge.sdk.db.utils import (
    _custom_json_serializer,
    convert_to_artifact,
    convert_to_aws_secret_parameter,
    convert_to_bitwarden_login_credential_parameter,
    convert_to_bitwarden_sensitive_information_parameter,
    convert_to_organization,
    convert_to_organization_auth_token,
    convert_to_output_parameter,
    convert_to_step,
    convert_to_task,
    convert_to_workflow,
    convert_to_workflow_parameter,
    convert_to_workflow_run,
    convert_to_workflow_run_block,
    convert_to_workflow_run_output_parameter,
    convert_to_workflow_run_parameter,
)
from skyvern.forge.sdk.log_artifacts import save_workflow_run_logs
from skyvern.forge.sdk.models import Step, StepStatus
from skyvern.forge.sdk.schemas.ai_suggestions import AISuggestion
from skyvern.forge.sdk.schemas.credentials import Credential, CredentialType
from skyvern.forge.sdk.schemas.organization_bitwarden_collections import OrganizationBitwardenCollection
from skyvern.forge.sdk.schemas.organizations import Organization, OrganizationAuthToken
from skyvern.forge.sdk.schemas.persistent_browser_sessions import PersistentBrowserSession
from skyvern.forge.sdk.schemas.task_generations import TaskGeneration
from skyvern.forge.sdk.schemas.task_runs import TaskRun, TaskRunType
from skyvern.forge.sdk.schemas.task_v2 import TaskV2, TaskV2Status, Thought, ThoughtType
from skyvern.forge.sdk.schemas.tasks import OrderBy, ProxyLocation, SortDirection, Task, TaskStatus
from skyvern.forge.sdk.schemas.totp_codes import TOTPCode
from skyvern.forge.sdk.schemas.workflow_runs import WorkflowRunBlock
from skyvern.forge.sdk.workflow.models.block import BlockStatus, BlockType
from skyvern.forge.sdk.workflow.models.parameter import (
    AWSSecretParameter,
    BitwardenCreditCardDataParameter,
    BitwardenLoginCredentialParameter,
    BitwardenSensitiveInformationParameter,
    CredentialParameter,
    OutputParameter,
    WorkflowParameter,
    WorkflowParameterType,
)
from skyvern.forge.sdk.workflow.models.workflow import (
    Workflow,
    WorkflowRun,
    WorkflowRunOutputParameter,
    WorkflowRunParameter,
    WorkflowRunStatus,
    WorkflowStatus,
)
from skyvern.webeye.actions.actions import Action
from skyvern.webeye.actions.models import AgentStepOutput

LOG = structlog.get_logger()

DB_CONNECT_ARGS: dict[str, Any] = {}

if "postgresql+psycopg" in settings.DATABASE_STRING:
    DB_CONNECT_ARGS = {"options": f"-c statement_timeout={settings.DATABASE_STATEMENT_TIMEOUT_MS}"}
elif "postgresql+asyncpg" in settings.DATABASE_STRING:
    DB_CONNECT_ARGS = {"server_settings": {"statement_timeout": str(settings.DATABASE_STATEMENT_TIMEOUT_MS)}}


class AgentDB:
    def __init__(self, database_string: str, debug_enabled: bool = False) -> None:
        super().__init__()
        self.debug_enabled = debug_enabled
        self.engine = create_async_engine(
            database_string,
            json_serializer=_custom_json_serializer,
            connect_args=DB_CONNECT_ARGS,
        )
        self.Session = async_sessionmaker(bind=self.engine)

    async def create_task(
        self,
        url: str,
        title: str | None,
        complete_criterion: str | None,
        terminate_criterion: str | None,
        navigation_goal: str | None,
        data_extraction_goal: str | None,
        navigation_payload: dict[str, Any] | list | str | None,
        webhook_callback_url: str | None = None,
        totp_verification_url: str | None = None,
        totp_identifier: str | None = None,
        organization_id: str | None = None,
        proxy_location: ProxyLocation | None = None,
        extracted_information_schema: dict[str, Any] | list | str | None = None,
        workflow_run_id: str | None = None,
        order: int | None = None,
        retry: int | None = None,
        max_steps_per_run: int | None = None,
        error_code_mapping: dict[str, str] | None = None,
        task_type: str = TaskType.general,
        application: str | None = None,
    ) -> Task:
        try:
            async with self.Session() as session:
                new_task = TaskModel(
                    status="created",
                    task_type=task_type,
                    url=url,
                    title=title,
                    webhook_callback_url=webhook_callback_url,
                    totp_verification_url=totp_verification_url,
                    totp_identifier=totp_identifier,
                    navigation_goal=navigation_goal,
                    complete_criterion=complete_criterion,
                    terminate_criterion=terminate_criterion,
                    data_extraction_goal=data_extraction_goal,
                    navigation_payload=navigation_payload,
                    organization_id=organization_id,
                    proxy_location=proxy_location,
                    extracted_information_schema=extracted_information_schema,
                    workflow_run_id=workflow_run_id,
                    order=order,
                    retry=retry,
                    max_steps_per_run=max_steps_per_run,
                    error_code_mapping=error_code_mapping,
                    application=application,
                )
                session.add(new_task)
                await session.commit()
                await session.refresh(new_task)
                return convert_to_task(new_task, self.debug_enabled)
        except SQLAlchemyError:
            LOG.error("SQLAlchemyError", exc_info=True)
            raise
        except Exception:
            LOG.error("UnexpectedError", exc_info=True)
            raise

    async def create_step(
        self,
        task_id: str,
        order: int,
        retry_index: int,
        organization_id: str | None = None,
    ) -> Step:
        try:
            async with self.Session() as session:
                new_step = StepModel(
                    task_id=task_id,
                    order=order,
                    retry_index=retry_index,
                    status="created",
                    organization_id=organization_id,
                )
                session.add(new_step)
                await session.commit()
                await session.refresh(new_step)
                return convert_to_step(new_step, debug_enabled=self.debug_enabled)
        except SQLAlchemyError:
            LOG.error("SQLAlchemyError", exc_info=True)
            raise
        except Exception:
            LOG.error("UnexpectedError", exc_info=True)
            raise

    async def create_artifact(
        self,
        artifact_id: str,
        artifact_type: str,
        uri: str,
        step_id: str | None = None,
        task_id: str | None = None,
        workflow_run_id: str | None = None,
        workflow_run_block_id: str | None = None,
        task_v2_id: str | None = None,
        thought_id: str | None = None,
        ai_suggestion_id: str | None = None,
        organization_id: str | None = None,
    ) -> Artifact:
        try:
            async with self.Session() as session:
                new_artifact = ArtifactModel(
                    artifact_id=artifact_id,
                    artifact_type=artifact_type,
                    uri=uri,
                    task_id=task_id,
                    step_id=step_id,
                    workflow_run_id=workflow_run_id,
                    workflow_run_block_id=workflow_run_block_id,
                    observer_cruise_id=task_v2_id,
                    observer_thought_id=thought_id,
                    ai_suggestion_id=ai_suggestion_id,
                    organization_id=organization_id,
                )
                session.add(new_artifact)
                await session.commit()
                await session.refresh(new_artifact)
                return convert_to_artifact(new_artifact, self.debug_enabled)
        except SQLAlchemyError:
            LOG.exception("SQLAlchemyError")
            raise
        except Exception:
            LOG.exception("UnexpectedError")
            raise

    async def get_task(self, task_id: str, organization_id: str | None = None) -> Task | None:
        """Get a task by its id"""
        try:
            async with self.Session() as session:
                if task_obj := (
                    await session.scalars(
                        select(TaskModel).filter_by(task_id=task_id).filter_by(organization_id=organization_id)
                    )
                ).first():
                    return convert_to_task(task_obj, self.debug_enabled)
                else:
                    LOG.info(
                        "Task not found",
                        task_id=task_id,
                        organization_id=organization_id,
                    )
                    return None
        except SQLAlchemyError:
            LOG.error("SQLAlchemyError", exc_info=True)
            raise
        except Exception:
            LOG.error("UnexpectedError", exc_info=True)
            raise

    async def get_tasks_by_ids(
        self,
        task_ids: list[str],
        organization_id: str | None = None,
    ) -> list[Task]:
        try:
            async with self.Session() as session:
                tasks = (
                    await session.scalars(
                        select(TaskModel)
                        .filter(TaskModel.task_id.in_(task_ids))
                        .filter_by(organization_id=organization_id)
                    )
                ).all()
                return [convert_to_task(task, debug_enabled=self.debug_enabled) for task in tasks]
        except SQLAlchemyError:
            LOG.error("SQLAlchemyError", exc_info=True)
            raise
        except Exception:
            LOG.error("UnexpectedError", exc_info=True)
            raise

    async def get_step(self, task_id: str, step_id: str, organization_id: str | None = None) -> Step | None:
        try:
            async with self.Session() as session:
                if step := (
                    await session.scalars(
                        select(StepModel).filter_by(step_id=step_id).filter_by(organization_id=organization_id)
                    )
                ).first():
                    return convert_to_step(step, debug_enabled=self.debug_enabled)

                else:
                    return None
        except SQLAlchemyError:
            LOG.error("SQLAlchemyError", exc_info=True)
            raise
        except Exception:
            LOG.error("UnexpectedError", exc_info=True)
            raise

    async def get_task_steps(self, task_id: str, organization_id: str | None = None) -> list[Step]:
        try:
            async with self.Session() as session:
                if steps := (
                    await session.scalars(
                        select(StepModel)
                        .filter_by(task_id=task_id)
                        .filter_by(organization_id=organization_id)
                        .order_by(StepModel.order)
                        .order_by(StepModel.retry_index)
                    )
                ).all():
                    return [convert_to_step(step, debug_enabled=self.debug_enabled) for step in steps]
                else:
                    return []
        except SQLAlchemyError:
            LOG.error("SQLAlchemyError", exc_info=True)
            raise
        except Exception:
            LOG.error("UnexpectedError", exc_info=True)
            raise

    async def get_steps_by_task_ids(self, task_ids: list[str], organization_id: str | None = None) -> list[Step]:
        try:
            async with self.Session() as session:
                steps = (
                    await session.scalars(
                        select(StepModel)
                        .filter(StepModel.task_id.in_(task_ids))
                        .filter_by(organization_id=organization_id)
                    )
                ).all()
                return [convert_to_step(step, debug_enabled=self.debug_enabled) for step in steps]
        except SQLAlchemyError:
            LOG.error("SQLAlchemyError", exc_info=True)
            raise

    async def get_total_unique_step_order_count_by_task_ids(
        self,
        task_ids: list[str],
        organization_id: str | None = None,
    ) -> int:
        """
        Get the total count of unique (step.task_id, step.order) pairs of StepModel for the given task ids
        Basically translate this sql query into a SQLAlchemy query: select count(distinct(s.task_id, s.order)) from steps s
        where s.task_id in task_ids
        """
        try:
            async with self.Session() as session:
                query = (
                    select(func.count(distinct(tuple_(StepModel.task_id, StepModel.order))))
                    .where(StepModel.task_id.in_(task_ids))
                    .where(StepModel.organization_id == organization_id)
                )
                return (await session.execute(query)).scalar()
        except SQLAlchemyError:
            LOG.error("SQLAlchemyError", exc_info=True)
            raise
        except Exception:
            LOG.error("UnexpectedError", exc_info=True)
            raise

    async def get_task_step_models(self, task_id: str, organization_id: str | None = None) -> Sequence[StepModel]:
        try:
            async with self.Session() as session:
                return (
                    await session.scalars(
                        select(StepModel)
                        .filter_by(task_id=task_id)
                        .filter_by(organization_id=organization_id)
                        .order_by(StepModel.order)
                        .order_by(StepModel.retry_index)
                    )
                ).all()
        except SQLAlchemyError:
            LOG.error("SQLAlchemyError", exc_info=True)
            raise
        except Exception:
            LOG.error("UnexpectedError", exc_info=True)
            raise

    async def get_task_actions(self, task_id: str, organization_id: str | None = None) -> list[Action]:
        try:
            async with self.Session() as session:
                query = (
                    select(ActionModel)
                    .filter(ActionModel.organization_id == organization_id)
                    .filter(ActionModel.task_id == task_id)
                    .order_by(ActionModel.created_at)
                )

                actions = (await session.scalars(query)).all()
                return [Action.model_validate(action) for action in actions]

        except SQLAlchemyError:
            LOG.error("SQLAlchemyError", exc_info=True)
            raise
        except Exception:
            LOG.error("UnexpectedError", exc_info=True)
            raise

    async def get_tasks_actions(self, task_ids: list[str], organization_id: str | None = None) -> list[Action]:
        try:
            async with self.Session() as session:
                query = (
                    select(ActionModel)
                    .filter(ActionModel.organization_id == organization_id)
                    .filter(ActionModel.task_id.in_(task_ids))
                    .order_by(ActionModel.created_at.desc())
                )
                actions = (await session.scalars(query)).all()
                return [Action.model_validate(action) for action in actions]

        except SQLAlchemyError:
            LOG.error("SQLAlchemyError", exc_info=True)
            raise
        except Exception:
            LOG.error("UnexpectedError", exc_info=True)
            raise

    async def get_first_step(self, task_id: str, organization_id: str | None = None) -> Step | None:
        try:
            async with self.Session() as session:
                if step := (
                    await session.scalars(
                        select(StepModel)
                        .filter_by(task_id=task_id)
                        .filter_by(organization_id=organization_id)
                        .order_by(StepModel.order.asc())
                        .order_by(StepModel.retry_index.asc())
                    )
                ).first():
                    return convert_to_step(step, debug_enabled=self.debug_enabled)
                else:
                    LOG.info(
                        "Latest step not found",
                        task_id=task_id,
                        organization_id=organization_id,
                    )
                    return None
        except SQLAlchemyError:
            LOG.error("SQLAlchemyError", exc_info=True)
            raise
        except Exception:
            LOG.error("UnexpectedError", exc_info=True)
            raise

    async def get_latest_step(self, task_id: str, organization_id: str | None = None) -> Step | None:
        try:
            async with self.Session() as session:
                if step := (
                    await session.scalars(
                        select(StepModel)
                        .filter_by(task_id=task_id)
                        .filter_by(organization_id=organization_id)
                        .order_by(StepModel.order.desc())
                        .order_by(StepModel.retry_index.desc())
                    )
                ).first():
                    return convert_to_step(step, debug_enabled=self.debug_enabled)
                else:
                    LOG.info(
                        "Latest step not found",
                        task_id=task_id,
                        organization_id=organization_id,
                    )
                    return None
        except SQLAlchemyError:
            LOG.error("SQLAlchemyError", exc_info=True)
            raise
        except Exception:
            LOG.error("UnexpectedError", exc_info=True)
            raise

    async def update_step(
        self,
        task_id: str,
        step_id: str,
        status: StepStatus | None = None,
        output: AgentStepOutput | None = None,
        is_last: bool | None = None,
        retry_index: int | None = None,
        organization_id: str | None = None,
        incremental_cost: float | None = None,
        incremental_input_tokens: int | None = None,
        incremental_output_tokens: int | None = None,
    ) -> Step:
        try:
            async with self.Session() as session:
                if step := (
                    await session.scalars(
                        select(StepModel)
                        .filter_by(task_id=task_id)
                        .filter_by(step_id=step_id)
                        .filter_by(organization_id=organization_id)
                    )
                ).first():
                    if status is not None:
                        step.status = status
                    if output is not None:
                        step.output = output.model_dump(exclude_none=True)
                    if is_last is not None:
                        step.is_last = is_last
                    if retry_index is not None:
                        step.retry_index = retry_index
                    if incremental_cost is not None:
                        step.step_cost = incremental_cost + float(step.step_cost or 0)
                    if incremental_input_tokens is not None:
                        step.input_token_count = incremental_input_tokens + (step.input_token_count or 0)
                    if incremental_output_tokens is not None:
                        step.output_token_count = incremental_output_tokens + (step.output_token_count or 0)

                    await session.commit()
                    updated_step = await self.get_step(task_id, step_id, organization_id)
                    if not updated_step:
                        raise NotFoundError("Step not found")
                    return updated_step
                else:
                    raise NotFoundError("Step not found")
        except SQLAlchemyError:
            LOG.error("SQLAlchemyError", exc_info=True)
            raise
        except NotFoundError:
            LOG.error("NotFoundError", exc_info=True)
            raise
        except Exception:
            LOG.error("UnexpectedError", exc_info=True)
            raise

    async def clear_task_failure_reason(self, organization_id: str, task_id: str) -> Task:
        try:
            async with self.Session() as session:
                if task := (
                    await session.scalars(
                        select(TaskModel).filter_by(task_id=task_id).filter_by(organization_id=organization_id)
                    )
                ).first():
                    task.failure_reason = None
                    await session.commit()
                    await session.refresh(task)
                    return convert_to_task(task, debug_enabled=self.debug_enabled)
                else:
                    raise NotFoundError("Task not found")
        except SQLAlchemyError:
            LOG.error("SQLAlchemyError", exc_info=True)
            raise
        except NotFoundError:
            LOG.error("NotFoundError", exc_info=True)
            raise
        except Exception:
            LOG.error("UnexpectedError", exc_info=True)
            raise

    async def update_task(
        self,
        task_id: str,
        status: TaskStatus | None = None,
        extracted_information: dict[str, Any] | list | str | None = None,
        failure_reason: str | None = None,
        errors: list[dict[str, Any]] | None = None,
        max_steps_per_run: int | None = None,
        organization_id: str | None = None,
    ) -> Task:
        if (
            status is None
            and extracted_information is None
            and failure_reason is None
            and errors is None
            and max_steps_per_run is None
        ):
            raise ValueError(
                "At least one of status, extracted_information, or failure_reason must be provided to update the task"
            )
        try:
            async with self.Session() as session:
                if task := (
                    await session.scalars(
                        select(TaskModel).filter_by(task_id=task_id).filter_by(organization_id=organization_id)
                    )
                ).first():
                    if status is not None:
                        task.status = status
                    if extracted_information is not None:
                        task.extracted_information = extracted_information
                    if failure_reason is not None:
                        task.failure_reason = failure_reason
                    if errors is not None:
                        task.errors = errors
                    if max_steps_per_run is not None:
                        task.max_steps_per_run = max_steps_per_run
                    await session.commit()
                    updated_task = await self.get_task(task_id, organization_id=organization_id)
                    if not updated_task:
                        raise NotFoundError("Task not found")
                    return updated_task
                else:
                    raise NotFoundError("Task not found")
        except SQLAlchemyError:
            LOG.error("SQLAlchemyError", exc_info=True)
            raise
        except NotFoundError:
            LOG.error("NotFoundError", exc_info=True)
            raise
        except Exception:
            LOG.error("UnexpectedError", exc_info=True)
            raise

    async def get_tasks(
        self,
        page: int = 1,
        page_size: int = 10,
        task_status: list[TaskStatus] | None = None,
        workflow_run_id: str | None = None,
        organization_id: str | None = None,
        only_standalone_tasks: bool = False,
        application: str | None = None,
        order_by_column: OrderBy = OrderBy.created_at,
        order: SortDirection = SortDirection.desc,
    ) -> list[Task]:
        """
        Get all tasks.
        :param page: Starts at 1
        :param page_size:
        :param task_status:
        :param workflow_run_id:
        :param only_standalone_tasks:
        :param order_by_column:
        :param order:
        :return:
        """
        if page < 1:
            raise ValueError(f"Page must be greater than 0, got {page}")

        try:
            async with self.Session() as session:
                db_page = page - 1  # offset logic is 0 based
                query = select(TaskModel).filter(TaskModel.organization_id == organization_id)
                if task_status:
                    query = query.filter(TaskModel.status.in_(task_status))
                if workflow_run_id:
                    query = query.filter(TaskModel.workflow_run_id == workflow_run_id)
                if only_standalone_tasks:
                    query = query.filter(TaskModel.workflow_run_id.is_(None))
                if application:
                    query = query.filter(TaskModel.application == application)
                order_by_col = getattr(TaskModel, order_by_column)
                query = (
                    query.order_by(order_by_col.desc() if order == SortDirection.desc else order_by_col.asc())
                    .limit(page_size)
                    .offset(db_page * page_size)
                )
                tasks = (await session.scalars(query)).all()
                return [convert_to_task(task, debug_enabled=self.debug_enabled) for task in tasks]
        except SQLAlchemyError:
            LOG.error("SQLAlchemyError", exc_info=True)
            raise
        except Exception:
            LOG.error("UnexpectedError", exc_info=True)
            raise

    async def get_organization(self, organization_id: str) -> Organization | None:
        try:
            async with self.Session() as session:
                if organization := (
                    await session.scalars(select(OrganizationModel).filter_by(organization_id=organization_id))
                ).first():
                    return convert_to_organization(organization)
                else:
                    return None
        except SQLAlchemyError:
            LOG.error("SQLAlchemyError", exc_info=True)
            raise
        except Exception:
            LOG.error("UnexpectedError", exc_info=True)
            raise

    async def get_organization_by_domain(self, domain: str) -> Organization | None:
        async with self.Session() as session:
            if organization := (await session.scalars(select(OrganizationModel).filter_by(domain=domain))).first():
                return convert_to_organization(organization)
            return None

    async def create_organization(
        self,
        organization_name: str,
        webhook_callback_url: str | None = None,
        max_steps_per_run: int | None = None,
        max_retries_per_step: int | None = None,
        domain: str | None = None,
    ) -> Organization:
        async with self.Session() as session:
            org = OrganizationModel(
                organization_name=organization_name,
                webhook_callback_url=webhook_callback_url,
                max_steps_per_run=max_steps_per_run,
                max_retries_per_step=max_retries_per_step,
                domain=domain,
            )
            session.add(org)
            await session.commit()
            await session.refresh(org)

        return convert_to_organization(org)

    async def update_organization(
        self,
        organization_id: str,
        organization_name: str | None = None,
        webhook_callback_url: str | None = None,
        max_steps_per_run: int | None = None,
        max_retries_per_step: int | None = None,
    ) -> Organization:
        async with self.Session() as session:
            organization = (
                await session.scalars(select(OrganizationModel).filter_by(organization_id=organization_id))
            ).first()
            if not organization:
                raise NotFoundError
            if organization_name:
                organization.organization_name = organization_name
            if webhook_callback_url:
                organization.webhook_callback_url = webhook_callback_url
            if max_steps_per_run:
                organization.max_steps_per_run = max_steps_per_run
            if max_retries_per_step:
                organization.max_retries_per_step = max_retries_per_step
            await session.commit()
            await session.refresh(organization)
            return Organization.model_validate(organization)

    async def get_valid_org_auth_token(
        self,
        organization_id: str,
        token_type: OrganizationAuthTokenType,
    ) -> OrganizationAuthToken | None:
        try:
            async with self.Session() as session:
                if token := (
                    await session.scalars(
                        select(OrganizationAuthTokenModel)
                        .filter_by(organization_id=organization_id)
                        .filter_by(token_type=token_type)
                        .filter_by(valid=True)
                        .order_by(OrganizationAuthTokenModel.created_at.desc())
                    )
                ).first():
                    return convert_to_organization_auth_token(token)
                else:
                    return None
        except SQLAlchemyError:
            LOG.error("SQLAlchemyError", exc_info=True)
            raise
        except Exception:
            LOG.error("UnexpectedError", exc_info=True)
            raise

    async def get_valid_org_auth_tokens(
        self,
        organization_id: str,
        token_type: OrganizationAuthTokenType,
    ) -> list[OrganizationAuthToken]:
        try:
            async with self.Session() as session:
                tokens = (
                    await session.scalars(
                        select(OrganizationAuthTokenModel)
                        .filter_by(organization_id=organization_id)
                        .filter_by(token_type=token_type)
                        .filter_by(valid=True)
                        .order_by(OrganizationAuthTokenModel.created_at.desc())
                    )
                ).all()
                return [convert_to_organization_auth_token(token) for token in tokens]
        except SQLAlchemyError:
            LOG.error("SQLAlchemyError", exc_info=True)
            raise
        except Exception:
            LOG.error("UnexpectedError", exc_info=True)
            raise

    async def validate_org_auth_token(
        self,
        organization_id: str,
        token_type: OrganizationAuthTokenType,
        token: str,
        valid: bool | None = True,
    ) -> OrganizationAuthToken | None:
        try:
            async with self.Session() as session:
                query = (
                    select(OrganizationAuthTokenModel)
                    .filter_by(organization_id=organization_id)
                    .filter_by(token_type=token_type)
                    .filter_by(token=token)
                )
                if valid is not None:
                    query = query.filter_by(valid=valid)
                if token_obj := (await session.scalars(query)).first():
                    return convert_to_organization_auth_token(token_obj)
                else:
                    return None
        except SQLAlchemyError:
            LOG.error("SQLAlchemyError", exc_info=True)
            raise
        except Exception:
            LOG.error("UnexpectedError", exc_info=True)
            raise

    async def create_org_auth_token(
        self,
        organization_id: str,
        token_type: OrganizationAuthTokenType,
        token: str,
    ) -> OrganizationAuthToken:
        async with self.Session() as session:
            auth_token = OrganizationAuthTokenModel(
                organization_id=organization_id,
                token_type=token_type,
                token=token,
            )
            session.add(auth_token)
            await session.commit()
            await session.refresh(auth_token)

        return convert_to_organization_auth_token(auth_token)

    async def get_artifacts_for_task_v2(
        self,
        task_v2_id: str,
        organization_id: str | None = None,
        artifact_types: list[ArtifactType] | None = None,
    ) -> list[Artifact]:
        try:
            async with self.Session() as session:
                query = (
                    select(ArtifactModel)
                    .filter_by(observer_cruise_id=task_v2_id)
                    .filter_by(organization_id=organization_id)
                )
                if artifact_types:
                    query = query.filter(ArtifactModel.artifact_type.in_(artifact_types))

                query = query.order_by(ArtifactModel.created_at)
                if artifacts := (await session.scalars(query)).all():
                    return [convert_to_artifact(artifact, self.debug_enabled) for artifact in artifacts]
                else:
                    return []
        except SQLAlchemyError:
            LOG.error("SQLAlchemyError", exc_info=True)
            raise
        except Exception:
            LOG.error("UnexpectedError", exc_info=True)
            raise

    async def get_artifacts_for_task_step(
        self,
        task_id: str,
        step_id: str,
        organization_id: str | None = None,
    ) -> list[Artifact]:
        try:
            async with self.Session() as session:
                if artifacts := (
                    await session.scalars(
                        select(ArtifactModel)
                        .filter_by(task_id=task_id)
                        .filter_by(step_id=step_id)
                        .filter_by(organization_id=organization_id)
                        .order_by(ArtifactModel.created_at)
                    )
                ).all():
                    return [convert_to_artifact(artifact, self.debug_enabled) for artifact in artifacts]
                else:
                    return []
        except SQLAlchemyError:
            LOG.error("SQLAlchemyError", exc_info=True)
            raise
        except Exception:
            LOG.error("UnexpectedError", exc_info=True)
            raise

    async def get_artifact_by_id(
        self,
        artifact_id: str,
        organization_id: str,
    ) -> Artifact | None:
        try:
            async with self.Session() as session:
                if artifact := (
                    await session.scalars(
                        select(ArtifactModel)
                        .filter_by(artifact_id=artifact_id)
                        .filter_by(organization_id=organization_id)
                    )
                ).first():
                    return convert_to_artifact(artifact, self.debug_enabled)
                else:
                    return None
        except SQLAlchemyError:
            LOG.exception("SQLAlchemyError")
            raise
        except Exception:
            LOG.exception("UnexpectedError")
            raise

    async def get_artifacts_by_entity_id(
        self,
        artifact_type: ArtifactType | None = None,
        task_id: str | None = None,
        step_id: str | None = None,
        workflow_run_id: str | None = None,
        workflow_run_block_id: str | None = None,
        thought_id: str | None = None,
        task_v2_id: str | None = None,
        organization_id: str | None = None,
    ) -> list[Artifact]:
        try:
            async with self.Session() as session:
                query = select(ArtifactModel)

                if artifact_type is not None:
                    query = query.filter_by(artifact_type=artifact_type)
                if task_id is not None:
                    query = query.filter_by(task_id=task_id)
                if step_id is not None:
                    query = query.filter_by(step_id=step_id)
                if workflow_run_id is not None:
                    query = query.filter_by(workflow_run_id=workflow_run_id)
                if workflow_run_block_id is not None:
                    query = query.filter_by(workflow_run_block_id=workflow_run_block_id)
                if thought_id is not None:
                    query = query.filter_by(observer_thought_id=thought_id)
                if task_v2_id is not None:
                    query = query.filter_by(observer_cruise_id=task_v2_id)
                if organization_id is not None:
                    query = query.filter_by(organization_id=organization_id)

                query = query.order_by(ArtifactModel.created_at.desc())
                if artifacts := (await session.scalars(query)).all():
                    return [convert_to_artifact(artifact, self.debug_enabled) for artifact in artifacts]
                else:
                    return []
        except SQLAlchemyError:
            LOG.error("SQLAlchemyError", exc_info=True)
            raise
        except Exception:
            LOG.error("UnexpectedError", exc_info=True)
            raise

    async def get_artifact_by_entity_id(
        self,
        artifact_type: ArtifactType,
        task_id: str | None = None,
        step_id: str | None = None,
        workflow_run_id: str | None = None,
        workflow_run_block_id: str | None = None,
        thought_id: str | None = None,
        task_v2_id: str | None = None,
        organization_id: str | None = None,
    ) -> Artifact | None:
        artifacts = await self.get_artifacts_by_entity_id(
            artifact_type=artifact_type,
            task_id=task_id,
            step_id=step_id,
            workflow_run_id=workflow_run_id,
            workflow_run_block_id=workflow_run_block_id,
            thought_id=thought_id,
            task_v2_id=task_v2_id,
            organization_id=organization_id,
        )
        return artifacts[0] if artifacts else None

    async def get_artifact(
        self,
        task_id: str,
        step_id: str,
        artifact_type: ArtifactType,
        organization_id: str | None = None,
    ) -> Artifact | None:
        try:
            async with self.Session() as session:
                artifact = (
                    await session.scalars(
                        select(ArtifactModel)
                        .filter_by(task_id=task_id)
                        .filter_by(step_id=step_id)
                        .filter_by(organization_id=organization_id)
                        .filter_by(artifact_type=artifact_type)
                        .order_by(ArtifactModel.created_at.desc())
                    )
                ).first()
                if artifact:
                    return convert_to_artifact(artifact, self.debug_enabled)
                return None
        except SQLAlchemyError:
            LOG.error("SQLAlchemyError", exc_info=True)
            raise
        except Exception:
            LOG.error("UnexpectedError", exc_info=True)
            raise

    async def get_artifact_for_workflow_run(
        self,
        workflow_run_id: str,
        artifact_type: ArtifactType,
        organization_id: str | None = None,
    ) -> Artifact | None:
        try:
            async with self.Session() as session:
                artifact = (
                    await session.scalars(
                        select(ArtifactModel)
                        .join(TaskModel, TaskModel.task_id == ArtifactModel.task_id)
                        .filter(TaskModel.workflow_run_id == workflow_run_id)
                        .filter(ArtifactModel.artifact_type == artifact_type)
                        .filter(ArtifactModel.organization_id == organization_id)
                        .order_by(ArtifactModel.created_at.desc())
                    )
                ).first()
                if artifact:
                    return convert_to_artifact(artifact, self.debug_enabled)
                return None
        except SQLAlchemyError:
            LOG.error("SQLAlchemyError", exc_info=True)
            raise
        except Exception:
            LOG.error("UnexpectedError", exc_info=True)
            raise

    async def get_latest_artifact(
        self,
        task_id: str,
        step_id: str | None = None,
        artifact_types: list[ArtifactType] | None = None,
        organization_id: str | None = None,
    ) -> Artifact | None:
        try:
            artifacts = await self.get_latest_n_artifacts(
                task_id=task_id,
                step_id=step_id,
                artifact_types=artifact_types,
                organization_id=organization_id,
                n=1,
            )
            if artifacts:
                return artifacts[0]
            return None
        except SQLAlchemyError:
            LOG.exception("SQLAlchemyError")
            raise
        except Exception:
            LOG.exception("UnexpectedError")
            raise

    async def get_latest_n_artifacts(
        self,
        task_id: str,
        step_id: str | None = None,
        artifact_types: list[ArtifactType] | None = None,
        organization_id: str | None = None,
        n: int = 1,
    ) -> list[Artifact] | None:
        try:
            async with self.Session() as session:
                artifact_query = select(ArtifactModel).filter_by(task_id=task_id)
                if organization_id:
                    artifact_query = artifact_query.filter_by(organization_id=organization_id)
                if step_id:
                    artifact_query = artifact_query.filter_by(step_id=step_id)
                if artifact_types:
                    artifact_query = artifact_query.filter(ArtifactModel.artifact_type.in_(artifact_types))

                artifacts = (await session.scalars(artifact_query.order_by(ArtifactModel.created_at.desc()))).fetchmany(
                    n
                )
                if artifacts:
                    return [convert_to_artifact(artifact, self.debug_enabled) for artifact in artifacts]
                return None
        except SQLAlchemyError:
            LOG.exception("SQLAlchemyError")
            raise
        except Exception:
            LOG.exception("UnexpectedError")
            raise

    async def get_latest_task_by_workflow_id(
        self,
        organization_id: str,
        workflow_id: str,
        before: datetime | None = None,
    ) -> Task | None:
        try:
            async with self.Session() as session:
                query = select(TaskModel).filter_by(organization_id=organization_id).filter_by(workflow_id=workflow_id)
                if before:
                    query = query.filter(TaskModel.created_at < before)
                task = (await session.scalars(query.order_by(TaskModel.created_at.desc()))).first()
                if task:
                    return convert_to_task(task, debug_enabled=self.debug_enabled)
                return None
        except SQLAlchemyError:
            LOG.error("SQLAlchemyError", exc_info=True)
            raise

    async def create_workflow(
        self,
        title: str,
        workflow_definition: dict[str, Any],
        organization_id: str | None = None,
        description: str | None = None,
        proxy_location: ProxyLocation | None = None,
        webhook_callback_url: str | None = None,
        totp_verification_url: str | None = None,
        totp_identifier: str | None = None,
        persist_browser_session: bool = False,
        workflow_permanent_id: str | None = None,
        version: int | None = None,
        is_saved_task: bool = False,
        status: WorkflowStatus = WorkflowStatus.published,
    ) -> Workflow:
        async with self.Session() as session:
            workflow = WorkflowModel(
                organization_id=organization_id,
                title=title,
                description=description,
                workflow_definition=workflow_definition,
                proxy_location=proxy_location,
                webhook_callback_url=webhook_callback_url,
                totp_verification_url=totp_verification_url,
                totp_identifier=totp_identifier,
                persist_browser_session=persist_browser_session,
                is_saved_task=is_saved_task,
                status=status,
            )
            if workflow_permanent_id:
                workflow.workflow_permanent_id = workflow_permanent_id
            if version:
                workflow.version = version
            session.add(workflow)
            await session.commit()
            await session.refresh(workflow)
            return convert_to_workflow(workflow, self.debug_enabled)

    async def soft_delete_workflow_by_id(self, workflow_id: str, organization_id: str) -> None:
        try:
            async with self.Session() as session:
                # soft delete the workflow by setting the deleted_at field to the current time
                update_deleted_at_query = (
                    update(WorkflowModel)
                    .where(WorkflowModel.workflow_id == workflow_id)
                    .where(WorkflowModel.organization_id == organization_id)
                    .where(WorkflowModel.deleted_at.is_(None))
                    .values(deleted_at=datetime.utcnow())
                )
                await session.execute(update_deleted_at_query)
                await session.commit()
        except SQLAlchemyError:
            LOG.error("SQLAlchemyError in soft_delete_workflow_by_id", exc_info=True)
            raise

    async def get_workflow(self, workflow_id: str, organization_id: str | None = None) -> Workflow | None:
        try:
            async with self.Session() as session:
                get_workflow_query = (
                    select(WorkflowModel).filter_by(workflow_id=workflow_id).filter(WorkflowModel.deleted_at.is_(None))
                )
                if organization_id:
                    get_workflow_query = get_workflow_query.filter_by(organization_id=organization_id)
                if workflow := (await session.scalars(get_workflow_query)).first():
                    return convert_to_workflow(workflow, self.debug_enabled)
                return None
        except SQLAlchemyError:
            LOG.error("SQLAlchemyError", exc_info=True)
            raise

    async def get_workflow_by_permanent_id(
        self,
        workflow_permanent_id: str,
        organization_id: str | None = None,
        version: int | None = None,
        exclude_deleted: bool = True,
    ) -> Workflow | None:
        try:
            get_workflow_query = select(WorkflowModel).filter_by(workflow_permanent_id=workflow_permanent_id)
            if exclude_deleted:
                get_workflow_query = get_workflow_query.filter(WorkflowModel.deleted_at.is_(None))
            if organization_id:
                get_workflow_query = get_workflow_query.filter_by(organization_id=organization_id)
            if version:
                get_workflow_query = get_workflow_query.filter_by(version=version)
            get_workflow_query = get_workflow_query.order_by(WorkflowModel.version.desc())
            async with self.Session() as session:
                if workflow := (await session.scalars(get_workflow_query)).first():
                    return convert_to_workflow(workflow, self.debug_enabled)
                return None
        except SQLAlchemyError:
            LOG.error("SQLAlchemyError", exc_info=True)
            raise

    async def get_workflows_by_permanent_ids(
        self,
        workflow_permanent_ids: list[str],
        organization_id: str | None = None,
        page: int = 1,
        page_size: int = 10,
        title: str = "",
        statuses: list[WorkflowStatus] | None = None,
    ) -> list[Workflow]:
        """
        Get all workflows with the latest version for the organization.
        """
        if page < 1:
            raise ValueError(f"Page must be greater than 0, got {page}")
        db_page = page - 1
        try:
            async with self.Session() as session:
                subquery = (
                    select(
                        WorkflowModel.workflow_permanent_id,
                        func.max(WorkflowModel.version).label("max_version"),
                    )
                    .where(WorkflowModel.workflow_permanent_id.in_(workflow_permanent_ids))
                    .where(WorkflowModel.deleted_at.is_(None))
                    .group_by(
                        WorkflowModel.workflow_permanent_id,
                    )
                    .subquery()
                )
                main_query = select(WorkflowModel).join(
                    subquery,
                    (WorkflowModel.workflow_permanent_id == subquery.c.workflow_permanent_id)
                    & (WorkflowModel.version == subquery.c.max_version),
                )
                if organization_id:
                    main_query = main_query.where(WorkflowModel.organization_id == organization_id)
                if title:
                    main_query = main_query.where(WorkflowModel.title.ilike(f"%{title}%"))
                if statuses:
                    main_query = main_query.where(WorkflowModel.status.in_(statuses))
                main_query = (
                    main_query.order_by(WorkflowModel.created_at.desc()).limit(page_size).offset(db_page * page_size)
                )
                workflows = (await session.scalars(main_query)).all()
                return [convert_to_workflow(workflow, self.debug_enabled) for workflow in workflows]
        except SQLAlchemyError:
            LOG.error("SQLAlchemyError", exc_info=True)
            raise

    async def get_workflows_by_organization_id(
        self,
        organization_id: str,
        page: int = 1,
        page_size: int = 10,
        only_saved_tasks: bool = False,
        only_workflows: bool = False,
        title: str = "",
        statuses: list[WorkflowStatus] | None = None,
    ) -> list[Workflow]:
        """
        Get all workflows with the latest version for the organization.
        """
        if page < 1:
            raise ValueError(f"Page must be greater than 0, got {page}")
        db_page = page - 1
        try:
            async with self.Session() as session:
                subquery = (
                    select(
                        WorkflowModel.organization_id,
                        WorkflowModel.workflow_permanent_id,
                        func.max(WorkflowModel.version).label("max_version"),
                    )
                    .where(WorkflowModel.organization_id == organization_id)
                    .where(WorkflowModel.deleted_at.is_(None))
                    .group_by(
                        WorkflowModel.organization_id,
                        WorkflowModel.workflow_permanent_id,
                    )
                    .subquery()
                )
                main_query = select(WorkflowModel).join(
                    subquery,
                    (WorkflowModel.organization_id == subquery.c.organization_id)
                    & (WorkflowModel.workflow_permanent_id == subquery.c.workflow_permanent_id)
                    & (WorkflowModel.version == subquery.c.max_version),
                )
                if only_saved_tasks:
                    main_query = main_query.where(WorkflowModel.is_saved_task.is_(True))
                elif only_workflows:
                    main_query = main_query.where(WorkflowModel.is_saved_task.is_(False))
                if title:
                    main_query = main_query.where(WorkflowModel.title.ilike(f"%{title}%"))
                if statuses:
                    main_query = main_query.where(WorkflowModel.status.in_(statuses))
                main_query = (
                    main_query.order_by(WorkflowModel.created_at.desc()).limit(page_size).offset(db_page * page_size)
                )
                workflows = (await session.scalars(main_query)).all()
                return [convert_to_workflow(workflow, self.debug_enabled) for workflow in workflows]
        except SQLAlchemyError:
            LOG.error("SQLAlchemyError", exc_info=True)
            raise

    async def update_workflow(
        self,
        workflow_id: str,
        organization_id: str | None = None,
        title: str | None = None,
        description: str | None = None,
        workflow_definition: dict[str, Any] | None = None,
        version: int | None = None,
    ) -> Workflow:
        try:
            async with self.Session() as session:
                get_workflow_query = (
                    select(WorkflowModel).filter_by(workflow_id=workflow_id).filter(WorkflowModel.deleted_at.is_(None))
                )
                if organization_id:
                    get_workflow_query = get_workflow_query.filter_by(organization_id=organization_id)
                if workflow := (await session.scalars(get_workflow_query)).first():
                    if title:
                        workflow.title = title
                    if description:
                        workflow.description = description
                    if workflow_definition:
                        workflow.workflow_definition = workflow_definition
                    if version:
                        workflow.version = version
                    await session.commit()
                    await session.refresh(workflow)
                    return convert_to_workflow(workflow, self.debug_enabled)
                else:
                    raise NotFoundError("Workflow not found")
        except SQLAlchemyError:
            LOG.error("SQLAlchemyError", exc_info=True)
            raise
        except NotFoundError:
            LOG.error("No workflow found to update", workflow_id=workflow_id)
            LOG.error("NotFoundError", exc_info=True)
            raise
        except Exception:
            LOG.error("UnexpectedError", exc_info=True)
            raise

    async def soft_delete_workflow_by_permanent_id(
        self,
        workflow_permanent_id: str,
        organization_id: str | None = None,
    ) -> None:
        async with self.Session() as session:
            # soft delete the workflow by setting the deleted_at field
            update_deleted_at_query = (
                update(WorkflowModel)
                .where(WorkflowModel.workflow_permanent_id == workflow_permanent_id)
                .where(WorkflowModel.deleted_at.is_(None))
            )
            if organization_id:
                update_deleted_at_query = update_deleted_at_query.filter_by(organization_id=organization_id)
            update_deleted_at_query = update_deleted_at_query.values(deleted_at=datetime.utcnow())
            await session.execute(update_deleted_at_query)
            await session.commit()

    async def create_workflow_run(
        self,
        workflow_permanent_id: str,
        workflow_id: str,
        organization_id: str,
        proxy_location: ProxyLocation | None = None,
        webhook_callback_url: str | None = None,
        totp_verification_url: str | None = None,
        totp_identifier: str | None = None,
        parent_workflow_run_id: str | None = None,
    ) -> WorkflowRun:
        try:
            async with self.Session() as session:
                workflow_run = WorkflowRunModel(
                    workflow_permanent_id=workflow_permanent_id,
                    workflow_id=workflow_id,
                    organization_id=organization_id,
                    proxy_location=proxy_location,
                    status="created",
                    webhook_callback_url=webhook_callback_url,
                    totp_verification_url=totp_verification_url,
                    totp_identifier=totp_identifier,
                    parent_workflow_run_id=parent_workflow_run_id,
                )
                session.add(workflow_run)
                await session.commit()
                await session.refresh(workflow_run)
                return convert_to_workflow_run(workflow_run)
        except SQLAlchemyError:
            LOG.error("SQLAlchemyError", exc_info=True)
            raise

    async def update_workflow_run(
        self, workflow_run_id: str, status: WorkflowRunStatus, failure_reason: str | None = None
    ) -> WorkflowRun | None:
        async with self.Session() as session:
            workflow_run = (
                await session.scalars(select(WorkflowRunModel).filter_by(workflow_run_id=workflow_run_id))
            ).first()
            if workflow_run:
                workflow_run.status = status
                workflow_run.failure_reason = failure_reason
                await session.commit()
                await session.refresh(workflow_run)
                await save_workflow_run_logs(workflow_run_id)
                return convert_to_workflow_run(workflow_run)
            LOG.error(
                "WorkflowRun not found, nothing to update",
                workflow_run_id=workflow_run_id,
            )
            return None

    async def get_all_runs(
        self, organization_id: str, page: int = 1, page_size: int = 10, status: list[WorkflowRunStatus] | None = None
    ) -> list[WorkflowRun | Task]:
        try:
            async with self.Session() as session:
                # temporary limit to 10 pages
                if page > 10:
                    return []

                limit = page * page_size

                workflow_run_query = (
                    select(WorkflowRunModel, WorkflowModel.title)
                    .join(WorkflowModel, WorkflowModel.workflow_id == WorkflowRunModel.workflow_id)
                    .filter(WorkflowRunModel.organization_id == organization_id)
                    .filter(WorkflowRunModel.parent_workflow_run_id.is_(None))
                )
                if status:
                    workflow_run_query = workflow_run_query.filter(WorkflowRunModel.status.in_(status))
                workflow_run_query = workflow_run_query.order_by(WorkflowRunModel.created_at.desc()).limit(limit)
                workflow_run_query_result = (await session.execute(workflow_run_query)).all()
                workflow_runs = [
                    convert_to_workflow_run(run, workflow_title=title, debug_enabled=self.debug_enabled)
                    for run, title in workflow_run_query_result
                ]

                task_query = (
                    select(TaskModel)
                    .filter(TaskModel.organization_id == organization_id)
                    .filter(TaskModel.workflow_run_id.is_(None))
                )
                if status:
                    task_query = task_query.filter(TaskModel.status.in_(status))
                task_query = task_query.order_by(TaskModel.created_at.desc()).limit(limit)
                task_query_result = (await session.scalars(task_query)).all()
                tasks = [convert_to_task(task, debug_enabled=self.debug_enabled) for task in task_query_result]

                runs = workflow_runs + tasks

                runs.sort(key=lambda x: x.created_at, reverse=True)

                lower = (page - 1) * page_size
                upper = page * page_size

                return runs[lower:upper]

        except SQLAlchemyError:
            LOG.error("SQLAlchemyError", exc_info=True)
            raise

    async def get_workflow_run(self, workflow_run_id: str, organization_id: str | None = None) -> WorkflowRun | None:
        try:
            async with self.Session() as session:
                get_workflow_run_query = select(WorkflowRunModel).filter_by(workflow_run_id=workflow_run_id)
                if organization_id:
                    get_workflow_run_query = get_workflow_run_query.filter_by(organization_id=organization_id)
                if workflow_run := (await session.scalars(get_workflow_run_query)).first():
                    return convert_to_workflow_run(workflow_run)
                return None
        except SQLAlchemyError:
            LOG.error("SQLAlchemyError", exc_info=True)
            raise

    async def get_workflow_runs(
        self, organization_id: str, page: int = 1, page_size: int = 10, status: list[WorkflowRunStatus] | None = None
    ) -> list[WorkflowRun]:
        try:
            async with self.Session() as session:
                db_page = page - 1  # offset logic is 0 based
                query = (
                    select(WorkflowRunModel, WorkflowModel.title)
                    .join(WorkflowModel, WorkflowModel.workflow_id == WorkflowRunModel.workflow_id)
                    .filter(WorkflowRunModel.organization_id == organization_id)
                    .filter(WorkflowRunModel.parent_workflow_run_id.is_(None))
                )
                if status:
                    query = query.filter(WorkflowRunModel.status.in_(status))
                query = query.order_by(WorkflowRunModel.created_at.desc()).limit(page_size).offset(db_page * page_size)
                workflow_runs = (await session.execute(query)).all()
                return [
                    convert_to_workflow_run(run, workflow_title=title, debug_enabled=self.debug_enabled)
                    for run, title in workflow_runs
                ]
        except SQLAlchemyError:
            LOG.error("SQLAlchemyError", exc_info=True)
            raise

    async def get_workflow_runs_for_workflow_permanent_id(
        self,
        workflow_permanent_id: str,
        organization_id: str,
        page: int = 1,
        page_size: int = 10,
        status: list[WorkflowRunStatus] | None = None,
    ) -> list[WorkflowRun]:
        try:
            async with self.Session() as session:
                db_page = page - 1  # offset logic is 0 based
                query = (
                    select(WorkflowRunModel, WorkflowModel.title)
                    .join(WorkflowModel, WorkflowModel.workflow_id == WorkflowRunModel.workflow_id)
                    .filter(WorkflowRunModel.workflow_permanent_id == workflow_permanent_id)
                    .filter(WorkflowRunModel.organization_id == organization_id)
                )
                if status:
                    query = query.filter(WorkflowRunModel.status.in_(status))
                query = query.order_by(WorkflowRunModel.created_at.desc()).limit(page_size).offset(db_page * page_size)
                workflow_runs_and_titles_tuples = (await session.execute(query)).all()
                workflow_runs = [
                    convert_to_workflow_run(run, workflow_title=title, debug_enabled=self.debug_enabled)
                    for run, title in workflow_runs_and_titles_tuples
                ]
                return workflow_runs

        except SQLAlchemyError:
            LOG.error("SQLAlchemyError", exc_info=True)
            raise

    async def get_workflow_runs_by_parent_workflow_run_id(
        self,
        organization_id: str,
        parent_workflow_run_id: str,
    ) -> list[WorkflowRun]:
        try:
            async with self.Session() as session:
                query = (
                    select(WorkflowRunModel)
                    .filter(WorkflowRunModel.organization_id == organization_id)
                    .filter(WorkflowRunModel.parent_workflow_run_id == parent_workflow_run_id)
                )
                workflow_runs = (await session.scalars(query)).all()
                return [convert_to_workflow_run(run) for run in workflow_runs]
        except SQLAlchemyError:
            LOG.error("SQLAlchemyError", exc_info=True)
            raise

    async def create_workflow_parameter(
        self,
        workflow_id: str,
        workflow_parameter_type: WorkflowParameterType,
        key: str,
        default_value: Any,
        description: str | None = None,
    ) -> WorkflowParameter:
        try:
            async with self.Session() as session:
                default_value = (
                    json.dumps(default_value)
                    if workflow_parameter_type == WorkflowParameterType.JSON
                    else default_value
                )
                workflow_parameter = WorkflowParameterModel(
                    workflow_id=workflow_id,
                    workflow_parameter_type=workflow_parameter_type,
                    key=key,
                    default_value=default_value,
                    description=description,
                )
                session.add(workflow_parameter)
                await session.commit()
                await session.refresh(workflow_parameter)
                return convert_to_workflow_parameter(workflow_parameter, self.debug_enabled)
        except SQLAlchemyError:
            LOG.error("SQLAlchemyError", exc_info=True)
            raise

    async def create_aws_secret_parameter(
        self,
        workflow_id: str,
        key: str,
        aws_key: str,
        description: str | None = None,
    ) -> AWSSecretParameter:
        async with self.Session() as session:
            aws_secret_parameter = AWSSecretParameterModel(
                workflow_id=workflow_id,
                key=key,
                aws_key=aws_key,
                description=description,
            )
            session.add(aws_secret_parameter)
            await session.commit()
            await session.refresh(aws_secret_parameter)
            return convert_to_aws_secret_parameter(aws_secret_parameter)

    async def create_bitwarden_login_credential_parameter(
        self,
        workflow_id: str,
        bitwarden_client_id_aws_secret_key: str,
        bitwarden_client_secret_aws_secret_key: str,
        bitwarden_master_password_aws_secret_key: str,
        key: str,
        url_parameter_key: str | None = None,
        description: str | None = None,
        bitwarden_collection_id: str | None = None,
        bitwarden_item_id: str | None = None,
    ) -> BitwardenLoginCredentialParameter:
        async with self.Session() as session:
            bitwarden_login_credential_parameter = BitwardenLoginCredentialParameterModel(
                workflow_id=workflow_id,
                bitwarden_client_id_aws_secret_key=bitwarden_client_id_aws_secret_key,
                bitwarden_client_secret_aws_secret_key=bitwarden_client_secret_aws_secret_key,
                bitwarden_master_password_aws_secret_key=bitwarden_master_password_aws_secret_key,
                url_parameter_key=url_parameter_key,
                key=key,
                description=description,
                bitwarden_collection_id=bitwarden_collection_id,
                bitwarden_item_id=bitwarden_item_id,
            )
            session.add(bitwarden_login_credential_parameter)
            await session.commit()
            await session.refresh(bitwarden_login_credential_parameter)
            return convert_to_bitwarden_login_credential_parameter(bitwarden_login_credential_parameter)

    async def create_bitwarden_sensitive_information_parameter(
        self,
        workflow_id: str,
        bitwarden_client_id_aws_secret_key: str,
        bitwarden_client_secret_aws_secret_key: str,
        bitwarden_master_password_aws_secret_key: str,
        bitwarden_collection_id: str,
        bitwarden_identity_key: str,
        bitwarden_identity_fields: list[str],
        key: str,
        description: str | None = None,
    ) -> BitwardenSensitiveInformationParameter:
        async with self.Session() as session:
            bitwarden_sensitive_information_parameter = BitwardenSensitiveInformationParameterModel(
                workflow_id=workflow_id,
                bitwarden_client_id_aws_secret_key=bitwarden_client_id_aws_secret_key,
                bitwarden_client_secret_aws_secret_key=bitwarden_client_secret_aws_secret_key,
                bitwarden_master_password_aws_secret_key=bitwarden_master_password_aws_secret_key,
                bitwarden_collection_id=bitwarden_collection_id,
                bitwarden_identity_key=bitwarden_identity_key,
                bitwarden_identity_fields=bitwarden_identity_fields,
                key=key,
                description=description,
            )
            session.add(bitwarden_sensitive_information_parameter)
            await session.commit()
            await session.refresh(bitwarden_sensitive_information_parameter)
            return convert_to_bitwarden_sensitive_information_parameter(bitwarden_sensitive_information_parameter)

    async def create_bitwarden_credit_card_data_parameter(
        self,
        workflow_id: str,
        bitwarden_client_id_aws_secret_key: str,
        bitwarden_client_secret_aws_secret_key: str,
        bitwarden_master_password_aws_secret_key: str,
        bitwarden_collection_id: str,
        bitwarden_item_id: str,
        key: str,
        description: str | None = None,
    ) -> BitwardenCreditCardDataParameter:
        async with self.Session() as session:
            bitwarden_credit_card_data_parameter = BitwardenCreditCardDataParameterModel(
                workflow_id=workflow_id,
                bitwarden_client_id_aws_secret_key=bitwarden_client_id_aws_secret_key,
                bitwarden_client_secret_aws_secret_key=bitwarden_client_secret_aws_secret_key,
                bitwarden_master_password_aws_secret_key=bitwarden_master_password_aws_secret_key,
                bitwarden_collection_id=bitwarden_collection_id,
                bitwarden_item_id=bitwarden_item_id,
                key=key,
                description=description,
            )
            session.add(bitwarden_credit_card_data_parameter)
            await session.commit()
            await session.refresh(bitwarden_credit_card_data_parameter)
            return BitwardenCreditCardDataParameter.model_validate(bitwarden_credit_card_data_parameter)

    async def create_output_parameter(
        self,
        workflow_id: str,
        key: str,
        description: str | None = None,
    ) -> OutputParameter:
        async with self.Session() as session:
            output_parameter = OutputParameterModel(
                key=key,
                description=description,
                workflow_id=workflow_id,
            )
            session.add(output_parameter)
            await session.commit()
            await session.refresh(output_parameter)
            return convert_to_output_parameter(output_parameter)

    async def get_workflow_output_parameters(self, workflow_id: str) -> list[OutputParameter]:
        try:
            async with self.Session() as session:
                output_parameters = (
                    await session.scalars(select(OutputParameterModel).filter_by(workflow_id=workflow_id))
                ).all()
                return [convert_to_output_parameter(parameter) for parameter in output_parameters]
        except SQLAlchemyError:
            LOG.error("SQLAlchemyError", exc_info=True)
            raise

    async def create_credential_parameter(
        self, workflow_id: str, key: str, credential_id: str, description: str | None = None
    ) -> CredentialParameter:
        async with self.Session() as session:
            credential_parameter = CredentialParameterModel(
                workflow_id=workflow_id,
                key=key,
                description=description,
                credential_id=credential_id,
            )
            session.add(credential_parameter)
            await session.commit()
            await session.refresh(credential_parameter)
            return CredentialParameter(
                credential_parameter_id=credential_parameter.credential_parameter_id,
                workflow_id=credential_parameter.workflow_id,
                key=credential_parameter.key,
                description=credential_parameter.description,
                credential_id=credential_parameter.credential_id,
                created_at=credential_parameter.created_at,
                modified_at=credential_parameter.modified_at,
                deleted_at=credential_parameter.deleted_at,
            )

    async def get_workflow_run_output_parameters(self, workflow_run_id: str) -> list[WorkflowRunOutputParameter]:
        try:
            async with self.Session() as session:
                workflow_run_output_parameters = (
                    await session.scalars(
                        select(WorkflowRunOutputParameterModel)
                        .filter_by(workflow_run_id=workflow_run_id)
                        .order_by(WorkflowRunOutputParameterModel.created_at)
                    )
                ).all()
                return [
                    convert_to_workflow_run_output_parameter(parameter, self.debug_enabled)
                    for parameter in workflow_run_output_parameters
                ]
        except SQLAlchemyError:
            LOG.error("SQLAlchemyError", exc_info=True)
            raise

    async def create_or_update_workflow_run_output_parameter(
        self,
        workflow_run_id: str,
        output_parameter_id: str,
        value: dict[str, Any] | list | str | None,
    ) -> WorkflowRunOutputParameter:
        try:
            async with self.Session() as session:
                # check if the workflow run output parameter already exists
                # if it does, update the value
                if workflow_run_output_parameter := (
                    await session.scalars(
                        select(WorkflowRunOutputParameterModel)
                        .filter_by(workflow_run_id=workflow_run_id)
                        .filter_by(output_parameter_id=output_parameter_id)
                    )
                ).first():
                    LOG.info(
                        f"Updating existing workflow run output parameter with {workflow_run_output_parameter.workflow_run_id} - {workflow_run_output_parameter.output_parameter_id}"
                    )
                    workflow_run_output_parameter.value = value
                    await session.commit()
                    await session.refresh(workflow_run_output_parameter)
                    return convert_to_workflow_run_output_parameter(workflow_run_output_parameter, self.debug_enabled)

                # if it does not exist, create a new one
                workflow_run_output_parameter = WorkflowRunOutputParameterModel(
                    workflow_run_id=workflow_run_id,
                    output_parameter_id=output_parameter_id,
                    value=value,
                )
                session.add(workflow_run_output_parameter)
                await session.commit()
                await session.refresh(workflow_run_output_parameter)
                return convert_to_workflow_run_output_parameter(workflow_run_output_parameter, self.debug_enabled)

        except SQLAlchemyError:
            LOG.error("SQLAlchemyError", exc_info=True)
            raise

    async def update_workflow_run_output_parameter(
        self,
        workflow_run_id: str,
        output_parameter_id: str,
        value: dict[str, Any] | list | str | None,
    ) -> WorkflowRunOutputParameter:
        try:
            async with self.Session() as session:
                workflow_run_output_parameter = (
                    await session.scalars(
                        select(WorkflowRunOutputParameterModel)
                        .filter_by(workflow_run_id=workflow_run_id)
                        .filter_by(output_parameter_id=output_parameter_id)
                    )
                ).first()
                if not workflow_run_output_parameter:
                    raise NotFoundError(
                        f"WorkflowRunOutputParameter not found for {workflow_run_id} and {output_parameter_id}"
                    )
                workflow_run_output_parameter.value = value
                await session.commit()
                await session.refresh(workflow_run_output_parameter)
                return convert_to_workflow_run_output_parameter(workflow_run_output_parameter, self.debug_enabled)
        except SQLAlchemyError:
            LOG.error("SQLAlchemyError", exc_info=True)
            raise

    async def get_workflow_parameters(self, workflow_id: str) -> list[WorkflowParameter]:
        try:
            async with self.Session() as session:
                workflow_parameters = (
                    await session.scalars(select(WorkflowParameterModel).filter_by(workflow_id=workflow_id))
                ).all()
                return [convert_to_workflow_parameter(parameter) for parameter in workflow_parameters]
        except SQLAlchemyError:
            LOG.error("SQLAlchemyError", exc_info=True)
            raise

    async def get_workflow_parameter(self, workflow_parameter_id: str) -> WorkflowParameter | None:
        try:
            async with self.Session() as session:
                if workflow_parameter := (
                    await session.scalars(
                        select(WorkflowParameterModel).filter_by(workflow_parameter_id=workflow_parameter_id)
                    )
                ).first():
                    return convert_to_workflow_parameter(workflow_parameter, self.debug_enabled)
                return None
        except SQLAlchemyError:
            LOG.error("SQLAlchemyError", exc_info=True)
            raise

    async def create_workflow_run_parameter(
        self, workflow_run_id: str, workflow_parameter: WorkflowParameter, value: Any
    ) -> WorkflowRunParameter:
        workflow_parameter_id = workflow_parameter.workflow_parameter_id
        try:
            async with self.Session() as session:
                workflow_run_parameter = WorkflowRunParameterModel(
                    workflow_run_id=workflow_run_id,
                    workflow_parameter_id=workflow_parameter_id,
                    value=value,
                )
                session.add(workflow_run_parameter)
                await session.commit()
                await session.refresh(workflow_run_parameter)
                return convert_to_workflow_run_parameter(workflow_run_parameter, workflow_parameter, self.debug_enabled)
        except SQLAlchemyError:
            LOG.error("SQLAlchemyError", exc_info=True)
            raise

    async def get_workflow_run_parameters(
        self, workflow_run_id: str
    ) -> list[tuple[WorkflowParameter, WorkflowRunParameter]]:
        try:
            async with self.Session() as session:
                workflow_run_parameters = (
                    await session.scalars(select(WorkflowRunParameterModel).filter_by(workflow_run_id=workflow_run_id))
                ).all()
                results = []
                for workflow_run_parameter in workflow_run_parameters:
                    workflow_parameter = await self.get_workflow_parameter(workflow_run_parameter.workflow_parameter_id)
                    if not workflow_parameter:
                        raise WorkflowParameterNotFound(
                            workflow_parameter_id=workflow_run_parameter.workflow_parameter_id
                        )
                    results.append(
                        (
                            workflow_parameter,
                            convert_to_workflow_run_parameter(
                                workflow_run_parameter,
                                workflow_parameter,
                                self.debug_enabled,
                            ),
                        )
                    )
                return results
        except SQLAlchemyError:
            LOG.error("SQLAlchemyError", exc_info=True)
            raise

    async def get_last_task_for_workflow_run(self, workflow_run_id: str) -> Task | None:
        try:
            async with self.Session() as session:
                if task := (
                    await session.scalars(
                        select(TaskModel)
                        .filter_by(workflow_run_id=workflow_run_id)
                        .order_by(TaskModel.created_at.desc())
                    )
                ).first():
                    return convert_to_task(task, debug_enabled=self.debug_enabled)
                return None
        except SQLAlchemyError:
            LOG.error("SQLAlchemyError", exc_info=True)
            raise

    async def get_tasks_by_workflow_run_id(self, workflow_run_id: str) -> list[Task]:
        try:
            async with self.Session() as session:
                tasks = (
                    await session.scalars(
                        select(TaskModel).filter_by(workflow_run_id=workflow_run_id).order_by(TaskModel.created_at)
                    )
                ).all()
                return [convert_to_task(task, debug_enabled=self.debug_enabled) for task in tasks]
        except SQLAlchemyError:
            LOG.error("SQLAlchemyError", exc_info=True)
            raise

    async def delete_task_artifacts(self, organization_id: str, task_id: str) -> None:
        async with self.Session() as session:
            # delete artifacts by filtering organization_id and task_id
            stmt = delete(ArtifactModel).where(
                and_(
                    ArtifactModel.organization_id == organization_id,
                    ArtifactModel.task_id == task_id,
                )
            )
            await session.execute(stmt)
            await session.commit()

    async def delete_task_v2_artifacts(self, task_v2_id: str, organization_id: str | None = None) -> None:
        async with self.Session() as session:
            stmt = delete(ArtifactModel).where(
                and_(
                    ArtifactModel.observer_cruise_id == task_v2_id,
                    ArtifactModel.organization_id == organization_id,
                )
            )
            await session.execute(stmt)
            await session.commit()

    async def delete_task_steps(self, organization_id: str, task_id: str) -> None:
        async with self.Session() as session:
            # delete artifacts by filtering organization_id and task_id
            stmt = delete(StepModel).where(
                and_(
                    StepModel.organization_id == organization_id,
                    StepModel.task_id == task_id,
                )
            )
            await session.execute(stmt)
            await session.commit()

    async def create_task_generation(
        self,
        organization_id: str,
        user_prompt: str,
        user_prompt_hash: str,
        url: str | None = None,
        navigation_goal: str | None = None,
        navigation_payload: dict[str, Any] | None = None,
        data_extraction_goal: str | None = None,
        extracted_information_schema: dict[str, Any] | None = None,
        suggested_title: str | None = None,
        llm: str | None = None,
        llm_prompt: str | None = None,
        llm_response: str | None = None,
        source_task_generation_id: str | None = None,
    ) -> TaskGeneration:
        async with self.Session() as session:
            new_task_generation = TaskGenerationModel(
                organization_id=organization_id,
                user_prompt=user_prompt,
                user_prompt_hash=user_prompt_hash,
                url=url,
                navigation_goal=navigation_goal,
                navigation_payload=navigation_payload,
                data_extraction_goal=data_extraction_goal,
                extracted_information_schema=extracted_information_schema,
                llm=llm,
                llm_prompt=llm_prompt,
                llm_response=llm_response,
                suggested_title=suggested_title,
                source_task_generation_id=source_task_generation_id,
            )
            session.add(new_task_generation)
            await session.commit()
            await session.refresh(new_task_generation)
            return TaskGeneration.model_validate(new_task_generation)

    async def create_ai_suggestion(
        self,
        organization_id: str,
        ai_suggestion_type: str,
    ) -> AISuggestion:
        async with self.Session() as session:
            new_ai_suggestion = AISuggestionModel(
                organization_id=organization_id,
                ai_suggestion_type=ai_suggestion_type,
            )
            session.add(new_ai_suggestion)
            await session.commit()
            await session.refresh(new_ai_suggestion)
            return AISuggestion.model_validate(new_ai_suggestion)

    async def get_task_generation_by_prompt_hash(
        self,
        user_prompt_hash: str,
        query_window_hours: int = settings.PROMPT_CACHE_WINDOW_HOURS,
    ) -> TaskGeneration | None:
        before_time = datetime.utcnow() - timedelta(hours=query_window_hours)
        async with self.Session() as session:
            query = (
                select(TaskGenerationModel)
                .filter_by(user_prompt_hash=user_prompt_hash)
                .filter(TaskGenerationModel.llm.is_not(None))
                .filter(TaskGenerationModel.created_at > before_time)
            )
            task_generation = (await session.scalars(query)).first()
            if not task_generation:
                return None
            return TaskGeneration.model_validate(task_generation)

    async def get_totp_codes(
        self,
        organization_id: str,
        totp_identifier: str,
        valid_lifespan_minutes: int = settings.TOTP_LIFESPAN_MINUTES,
    ) -> list[TOTPCode]:
        """
        1. filter by:
        - organization_id
        - totp_identifier
        2. make sure created_at is within the valid lifespan
        3. sort by created_at desc
        """
        async with self.Session() as session:
            query = (
                select(TOTPCodeModel)
                .filter_by(organization_id=organization_id)
                .filter_by(totp_identifier=totp_identifier)
                .filter(TOTPCodeModel.created_at > datetime.utcnow() - timedelta(minutes=valid_lifespan_minutes))
                .order_by(TOTPCodeModel.created_at.desc())
            )
            totp_code = (await session.scalars(query)).all()
            return [TOTPCode.model_validate(totp_code) for totp_code in totp_code]

    async def create_totp_code(
        self,
        organization_id: str,
        totp_identifier: str,
        content: str,
        code: str,
        task_id: str | None = None,
        workflow_id: str | None = None,
        source: str | None = None,
        expired_at: datetime | None = None,
    ) -> TOTPCode:
        async with self.Session() as session:
            new_totp_code = TOTPCodeModel(
                organization_id=organization_id,
                totp_identifier=totp_identifier,
                content=content,
                code=code,
                task_id=task_id,
                workflow_id=workflow_id,
                source=source,
                expired_at=expired_at,
            )
            session.add(new_totp_code)
            await session.commit()
            await session.refresh(new_totp_code)
            return TOTPCode.model_validate(new_totp_code)

    async def create_action(self, action: Action) -> Action:
        async with self.Session() as session:
            new_action = ActionModel(
                action_type=action.action_type,
                source_action_id=action.source_action_id,
                organization_id=action.organization_id,
                workflow_run_id=action.workflow_run_id,
                task_id=action.task_id,
                step_id=action.step_id,
                step_order=action.step_order,
                action_order=action.action_order,
                status=action.status,
                reasoning=action.reasoning,
                intention=action.intention,
                response=action.response,
                element_id=action.element_id,
                skyvern_element_hash=action.skyvern_element_hash,
                skyvern_element_data=action.skyvern_element_data,
                action_json=action.model_dump(),
                confidence_float=action.confidence_float,
            )
            session.add(new_action)
            await session.commit()
            await session.refresh(new_action)
            return Action.model_validate(new_action)

    async def retrieve_action_plan(self, task: Task) -> list[Action]:
        async with self.Session() as session:
            subquery = (
                select(TaskModel.task_id)
                .filter(TaskModel.url == task.url)
                .filter(TaskModel.navigation_goal == task.navigation_goal)
                .filter(TaskModel.status == TaskStatus.completed)
                .order_by(TaskModel.created_at.desc())
                .limit(1)
                .subquery()
            )

            query = (
                select(ActionModel)
                .filter(ActionModel.task_id == subquery.c.task_id)
                .order_by(ActionModel.step_order, ActionModel.action_order, ActionModel.created_at)
            )

            actions = (await session.scalars(query)).all()
            return [Action.model_validate(action) for action in actions]

    async def get_previous_actions_for_task(self, task_id: str) -> list[Action]:
        async with self.Session() as session:
            query = (
                select(ActionModel)
                .filter_by(task_id=task_id)
                .order_by(ActionModel.step_order, ActionModel.action_order, ActionModel.created_at)
            )
            actions = (await session.scalars(query)).all()
            return [Action.model_validate(action) for action in actions]

    async def delete_task_actions(self, organization_id: str, task_id: str) -> None:
        async with self.Session() as session:
            # delete actions by filtering organization_id and task_id
            stmt = delete(ActionModel).where(
                and_(
                    ActionModel.organization_id == organization_id,
                    ActionModel.task_id == task_id,
                )
            )
            await session.execute(stmt)
            await session.commit()

    async def get_task_v2(self, task_v2_id: str, organization_id: str | None = None) -> TaskV2 | None:
        async with self.Session() as session:
            if task_v2 := (
                await session.scalars(
                    select(TaskV2Model)
                    .filter_by(observer_cruise_id=task_v2_id)
                    .filter_by(organization_id=organization_id)
                )
            ).first():
                return TaskV2.model_validate(task_v2)
            return None

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
                return TaskV2.model_validate(task_v2)
            return None

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

    async def get_thoughts(
        self,
        task_v2_id: str,
        thought_types: list[ThoughtType] | None = None,
        organization_id: str | None = None,
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

    async def create_task_v2(
        self,
        workflow_run_id: str | None = None,
        workflow_id: str | None = None,
        workflow_permanent_id: str | None = None,
        prompt: str | None = None,
        url: str | None = None,
        organization_id: str | None = None,
        proxy_location: ProxyLocation | None = None,
        totp_identifier: str | None = None,
        totp_verification_url: str | None = None,
        webhook_callback_url: str | None = None,
    ) -> TaskV2:
        async with self.Session() as session:
            new_task_v2 = TaskV2Model(
                workflow_run_id=workflow_run_id,
                workflow_id=workflow_id,
                workflow_permanent_id=workflow_permanent_id,
                prompt=prompt,
                url=url,
                proxy_location=proxy_location,
                totp_identifier=totp_identifier,
                totp_verification_url=totp_verification_url,
                webhook_callback_url=webhook_callback_url,
                organization_id=organization_id,
            )
            session.add(new_task_v2)
            await session.commit()
            await session.refresh(new_task_v2)
            return TaskV2.model_validate(new_task_v2)

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
                thought_cost=thought_cost,
                organization_id=organization_id,
            )
            session.add(new_thought)
            await session.commit()
            await session.refresh(new_thought)
            return Thought.model_validate(new_thought)

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
        thought_cost: float | None = None,
        organization_id: str | None = None,
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
                if input_token_count:
                    thought_obj.input_token_count = input_token_count
                if output_token_count:
                    thought_obj.output_token_count = output_token_count
                if thought_cost:
                    thought_obj.thought_cost = thought_cost
                await session.commit()
                await session.refresh(thought_obj)
                return Thought.model_validate(thought_obj)
            raise NotFoundError(f"Thought {thought_id}")

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
                await session.commit()
                await session.refresh(task_v2)
                return TaskV2.model_validate(task_v2)
            raise NotFoundError(f"TaskV2 {task_v2_id} not found")

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
            )
            session.add(new_workflow_run_block)
            await session.commit()
            await session.refresh(new_workflow_run_block)

        task = None
        if task_id:
            task = await self.get_task(task_id, organization_id=organization_id)
        return convert_to_workflow_run_block(new_workflow_run_block, task=task)

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
                if loop_values:
                    workflow_run_block.loop_values = loop_values
                if current_value:
                    workflow_run_block.current_value = current_value
                if current_index:
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
                await session.commit()
                await session.refresh(workflow_run_block)
            else:
                raise NotFoundError(f"WorkflowRunBlock {workflow_run_block_id} not found")
        task = None
        task_id = workflow_run_block.task_id
        if task_id:
            task = await self.get_task(task_id, organization_id=workflow_run_block.organization_id)
        return convert_to_workflow_run_block(workflow_run_block, task=task)

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
                    task = await self.get_task(task_id, organization_id=organization_id)
                return convert_to_workflow_run_block(workflow_run_block, task=task)
            raise NotFoundError(f"WorkflowRunBlock {workflow_run_block_id} not found")

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
            tasks = await self.get_tasks_by_workflow_run_id(workflow_run_id)
            tasks_dict = {task.task_id: task for task in tasks}
            return [
                convert_to_workflow_run_block(workflow_run_block, task=tasks_dict.get(workflow_run_block.task_id))
                for workflow_run_block in workflow_run_blocks
            ]

    async def get_active_persistent_browser_sessions(self, organization_id: str) -> List[PersistentBrowserSession]:
        """Get all active persistent browser sessions for an organization."""
        try:
            async with self.Session() as session:
                result = await session.execute(
                    select(PersistentBrowserSessionModel)
                    .filter_by(organization_id=organization_id)
                    .filter_by(deleted_at=None)
                )
                sessions = result.scalars().all()
                return [PersistentBrowserSession.model_validate(session) for session in sessions]
        except SQLAlchemyError:
            LOG.error("SQLAlchemyError", exc_info=True)
            raise
        except Exception:
            LOG.error("UnexpectedError", exc_info=True)
            raise

    async def get_persistent_browser_session_by_id(self, session_id: str) -> Optional[PersistentBrowserSession]:
        """Get a specific persistent browser session."""
        try:
            async with self.Session() as session:
                persistent_browser_session = (
                    await session.scalars(
                        select(PersistentBrowserSessionModel)
                        .filter_by(persistent_browser_session_id=session_id)
                        .filter_by(deleted_at=None)
                    )
                ).first()
                if persistent_browser_session:
                    return PersistentBrowserSession.model_validate(persistent_browser_session)
                raise NotFoundError(f"PersistentBrowserSession {session_id} not found")
        except NotFoundError:
            LOG.error("NotFoundError", exc_info=True)
            raise
        except SQLAlchemyError:
            LOG.error("SQLAlchemyError", exc_info=True)
            raise
        except Exception:
            LOG.error("UnexpectedError", exc_info=True)
            raise

    async def get_persistent_browser_session(
        self, session_id: str, organization_id: str
    ) -> Optional[PersistentBrowserSessionModel]:
        """Get a specific persistent browser session."""
        try:
            async with self.Session() as session:
                persistent_browser_session = (
                    await session.scalars(
                        select(PersistentBrowserSessionModel)
                        .filter_by(persistent_browser_session_id=session_id)
                        .filter_by(organization_id=organization_id)
                        .filter_by(deleted_at=None)
                    )
                ).first()
                if persistent_browser_session:
                    return PersistentBrowserSession.model_validate(persistent_browser_session)
                raise NotFoundError(f"PersistentBrowserSession {session_id} not found")
        except NotFoundError:
            LOG.error("NotFoundError", exc_info=True)
            raise
        except SQLAlchemyError:
            LOG.error("SQLAlchemyError", exc_info=True)
            raise
        except Exception:
            LOG.error("UnexpectedError", exc_info=True)
            raise

    async def create_persistent_browser_session(
        self,
        organization_id: str,
        runnable_type: str | None = None,
        runnable_id: str | None = None,
    ) -> PersistentBrowserSessionModel:
        """Create a new persistent browser session."""
        try:
            async with self.Session() as session:
                browser_session = PersistentBrowserSessionModel(
                    organization_id=organization_id,
                    runnable_type=runnable_type,
                    runnable_id=runnable_id,
                )
                session.add(browser_session)
                await session.commit()
                await session.refresh(browser_session)
                return PersistentBrowserSession.model_validate(browser_session)
        except SQLAlchemyError:
            LOG.error("SQLAlchemyError", exc_info=True)
            raise
        except Exception:
            LOG.error("UnexpectedError", exc_info=True)
            raise

    async def mark_persistent_browser_session_deleted(self, session_id: str, organization_id: str) -> None:
        """Mark a persistent browser session as deleted."""
        try:
            async with self.Session() as session:
                persistent_browser_session = (
                    await session.scalars(
                        select(PersistentBrowserSessionModel)
                        .filter_by(persistent_browser_session_id=session_id)
                        .filter_by(organization_id=organization_id)
                    )
                ).first()
                if persistent_browser_session:
                    persistent_browser_session.deleted_at = datetime.utcnow()
                    await session.commit()
                    await session.refresh(persistent_browser_session)
                else:
                    raise NotFoundError(f"PersistentBrowserSession {session_id} not found")
        except NotFoundError:
            LOG.error("NotFoundError", exc_info=True)
            raise
        except SQLAlchemyError:
            LOG.error("SQLAlchemyError", exc_info=True)
            raise
        except Exception:
            LOG.error("UnexpectedError", exc_info=True)
            raise

    async def occupy_persistent_browser_session(
        self, session_id: str, runnable_type: str, runnable_id: str, organization_id: str
    ) -> None:
        """Occupy a specific persistent browser session."""
        try:
            async with self.Session() as session:
                persistent_browser_session = (
                    await session.scalars(
                        select(PersistentBrowserSessionModel)
                        .filter_by(persistent_browser_session_id=session_id)
                        .filter_by(organization_id=organization_id)
                        .filter_by(deleted_at=None)
                    )
                ).first()
                if persistent_browser_session:
                    persistent_browser_session.runnable_type = runnable_type
                    persistent_browser_session.runnable_id = runnable_id
                    await session.commit()
                    await session.refresh(persistent_browser_session)
                else:
                    raise NotFoundError(f"PersistentBrowserSession {session_id} not found")
        except NotFoundError:
            LOG.error("NotFoundError", exc_info=True)
            raise
        except SQLAlchemyError:
            LOG.error("SQLAlchemyError", exc_info=True)
            raise
        except Exception:
            LOG.error("UnexpectedError", exc_info=True)
            raise

    async def release_persistent_browser_session(self, session_id: str, organization_id: str) -> None:
        """Release a specific persistent browser session."""
        try:
            async with self.Session() as session:
                persistent_browser_session = (
                    await session.scalars(
                        select(PersistentBrowserSessionModel)
                        .filter_by(persistent_browser_session_id=session_id)
                        .filter_by(organization_id=organization_id)
                        .filter_by(deleted_at=None)
                    )
                ).first()
                if persistent_browser_session:
                    persistent_browser_session.runnable_type = None
                    persistent_browser_session.runnable_id = None
                    await session.commit()
                    await session.refresh(persistent_browser_session)
                else:
                    raise NotFoundError(f"PersistentBrowserSession {session_id} not found")
        except SQLAlchemyError:
            LOG.error("SQLAlchemyError", exc_info=True)
            raise
        except NotFoundError:
            LOG.error("NotFoundError", exc_info=True)
            raise
        except Exception:
            LOG.error("UnexpectedError", exc_info=True)
            raise

    async def get_all_active_persistent_browser_sessions(self) -> List[PersistentBrowserSessionModel]:
        """Get all active persistent browser sessions across all organizations."""
        try:
            async with self.Session() as session:
                result = await session.execute(select(PersistentBrowserSessionModel).filter_by(deleted_at=None))
                return result.scalars().all()
        except SQLAlchemyError:
            LOG.error("SQLAlchemyError", exc_info=True)
            raise
        except Exception:
            LOG.error("UnexpectedError", exc_info=True)
            raise

    async def create_task_run(
        self,
        task_run_type: TaskRunType,
        organization_id: str,
        run_id: str,
        title: str | None = None,
        url: str | None = None,
        url_hash: str | None = None,
    ) -> TaskRun:
        async with self.Session() as session:
            task_run = TaskRunModel(
                task_run_type=task_run_type,
                organization_id=organization_id,
                run_id=run_id,
                title=title,
                url=url,
                url_hash=url_hash,
            )
            session.add(task_run)
            await session.commit()
            await session.refresh(task_run)
            return TaskRun.model_validate(task_run)

    async def create_credential(
        self,
        name: str,
        credential_type: CredentialType,
        organization_id: str,
        item_id: str,
    ) -> Credential:
        async with self.Session() as session:
            credential = CredentialModel(
                organization_id=organization_id,
                name=name,
                credential_type=credential_type,
                item_id=item_id,
            )
            session.add(credential)
            await session.commit()
            await session.refresh(credential)
            return Credential.model_validate(credential)

    async def get_credential(self, credential_id: str, organization_id: str) -> Credential:
        async with self.Session() as session:
            credential = (
                await session.scalars(
                    select(CredentialModel)
                    .filter_by(credential_id=credential_id)
                    .filter_by(organization_id=organization_id)
                    .filter(CredentialModel.deleted_at.is_(None))
                )
            ).first()
            if credential:
                return Credential.model_validate(credential)
            raise NotFoundError(f"Credential {credential_id} not found")

    async def get_credentials(self, organization_id: str, page: int = 1, page_size: int = 10) -> list[Credential]:
        async with self.Session() as session:
            credentials = (
                await session.scalars(
                    select(CredentialModel)
                    .filter_by(organization_id=organization_id)
                    .filter(CredentialModel.deleted_at.is_(None))
                    .order_by(CredentialModel.created_at.desc())
                    .offset((page - 1) * page_size)
                    .limit(page_size)
                )
            ).all()
            return [Credential.model_validate(credential) for credential in credentials]

    async def update_credential(
        self, credential_id: str, organization_id: str, name: str | None = None, website_url: str | None = None
    ) -> Credential:
        async with self.Session() as session:
            credential = (
                await session.scalars(
                    select(CredentialModel)
                    .filter_by(credential_id=credential_id)
                    .filter_by(organization_id=organization_id)
                )
            ).first()
            if not credential:
                raise NotFoundError(f"Credential {credential_id} not found")
            if name:
                credential.name = name
            if website_url:
                credential.website_url = website_url
            await session.commit()
            await session.refresh(credential)
            return Credential.model_validate(credential)

    async def delete_credential(self, credential_id: str, organization_id: str) -> None:
        async with self.Session() as session:
            credential = (
                await session.scalars(
                    select(CredentialModel)
                    .filter_by(credential_id=credential_id)
                    .filter_by(organization_id=organization_id)
                )
            ).first()
            if not credential:
                raise NotFoundError(f"Credential {credential_id} not found")
            credential.deleted_at = datetime.utcnow()
            await session.commit()
            await session.refresh(credential)
            return None

    async def create_organization_bitwarden_collection(
        self,
        organization_id: str,
        collection_id: str,
    ) -> OrganizationBitwardenCollection:
        async with self.Session() as session:
            organization_bitwarden_collection = OrganizationBitwardenCollectionModel(
                organization_id=organization_id, collection_id=collection_id
            )
            session.add(organization_bitwarden_collection)
            await session.commit()
            await session.refresh(organization_bitwarden_collection)
            return OrganizationBitwardenCollection.model_validate(organization_bitwarden_collection)

    async def get_organization_bitwarden_collection(
        self,
        organization_id: str,
    ) -> OrganizationBitwardenCollection | None:
        async with self.Session() as session:
            organization_bitwarden_collection = (
                await session.scalars(
                    select(OrganizationBitwardenCollectionModel).filter_by(organization_id=organization_id)
                )
            ).first()
            if organization_bitwarden_collection:
                return OrganizationBitwardenCollection.model_validate(organization_bitwarden_collection)
            return None

    async def cache_task_run(self, run_id: str, organization_id: str | None = None) -> TaskRun:
        async with self.Session() as session:
            task_run = (
                await session.scalars(
                    select(TaskRunModel).filter_by(organization_id=organization_id).filter_by(run_id=run_id)
                )
            ).first()
            if task_run:
                task_run.cached = True
                await session.commit()
                await session.refresh(task_run)
                return TaskRun.model_validate(task_run)
            raise NotFoundError(f"TaskRun {run_id} not found")

    async def get_cached_task_run(
        self, task_run_type: TaskRunType, url_hash: str | None = None, organization_id: str | None = None
    ) -> TaskRun | None:
        async with self.Session() as session:
            query = select(TaskRunModel)
            if task_run_type:
                query = query.filter_by(task_run_type=task_run_type)
            if url_hash:
                query = query.filter_by(url_hash=url_hash)
            if organization_id:
                query = query.filter_by(organization_id=organization_id)
            query = query.filter_by(cached=True).order_by(TaskRunModel.created_at.desc())
            task_run = (await session.scalars(query)).first()
            return TaskRun.model_validate(task_run) if task_run else None

    async def get_task_run(
        self,
        run_id: str,
        organization_id: str | None = None,
    ) -> TaskRun | None:
        async with self.Session() as session:
            query = select(TaskRunModel).filter_by(run_id=run_id)
            if organization_id:
                query = query.filter_by(organization_id=organization_id)
            task_run = (await session.scalars(query)).first()
            return TaskRun.model_validate(task_run) if task_run else None
