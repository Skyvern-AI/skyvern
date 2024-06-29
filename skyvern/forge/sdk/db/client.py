from datetime import datetime
from typing import Any, Sequence

import structlog
from sqlalchemy import and_, delete, func, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from skyvern.exceptions import WorkflowParameterNotFound
from skyvern.forge.sdk.artifact.models import Artifact, ArtifactType
from skyvern.forge.sdk.db.enums import OrganizationAuthTokenType
from skyvern.forge.sdk.db.exceptions import NotFoundError
from skyvern.forge.sdk.db.models import (
    ArtifactModel,
    AWSSecretParameterModel,
    BitwardenLoginCredentialParameterModel,
    OrganizationAuthTokenModel,
    OrganizationModel,
    OutputParameterModel,
    StepModel,
    TaskGenerationModel,
    TaskModel,
    WorkflowModel,
    WorkflowParameterModel,
    WorkflowRunModel,
    WorkflowRunOutputParameterModel,
    WorkflowRunParameterModel,
)
from skyvern.forge.sdk.db.utils import (
    _custom_json_serializer,
    convert_to_artifact,
    convert_to_aws_secret_parameter,
    convert_to_bitwarden_login_credential_parameter,
    convert_to_organization,
    convert_to_organization_auth_token,
    convert_to_output_parameter,
    convert_to_step,
    convert_to_task,
    convert_to_workflow,
    convert_to_workflow_parameter,
    convert_to_workflow_run,
    convert_to_workflow_run_output_parameter,
    convert_to_workflow_run_parameter,
)
from skyvern.forge.sdk.models import Organization, OrganizationAuthToken, Step, StepStatus
from skyvern.forge.sdk.schemas.task_generations import TaskGeneration
from skyvern.forge.sdk.schemas.tasks import ProxyLocation, Task, TaskStatus
from skyvern.forge.sdk.workflow.models.parameter import (
    AWSSecretParameter,
    BitwardenLoginCredentialParameter,
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
)
from skyvern.webeye.actions.models import AgentStepOutput

LOG = structlog.get_logger()


class AgentDB:
    def __init__(self, database_string: str, debug_enabled: bool = False) -> None:
        super().__init__()
        self.debug_enabled = debug_enabled
        self.engine = create_async_engine(database_string, json_serializer=_custom_json_serializer)
        self.Session = async_sessionmaker(bind=self.engine)

    async def create_task(
        self,
        url: str,
        title: str | None,
        navigation_goal: str | None,
        data_extraction_goal: str | None,
        navigation_payload: dict[str, Any] | list | str | None,
        webhook_callback_url: str | None = None,
        organization_id: str | None = None,
        proxy_location: ProxyLocation | None = None,
        extracted_information_schema: dict[str, Any] | list | str | None = None,
        workflow_run_id: str | None = None,
        order: int | None = None,
        retry: int | None = None,
        max_steps_per_run: int | None = None,
        error_code_mapping: dict[str, str] | None = None,
    ) -> Task:
        try:
            async with self.Session() as session:
                new_task = TaskModel(
                    status="created",
                    url=url,
                    title=title,
                    webhook_callback_url=webhook_callback_url,
                    navigation_goal=navigation_goal,
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
        step_id: str,
        task_id: str,
        artifact_type: str,
        uri: str,
        organization_id: str | None = None,
    ) -> Artifact:
        try:
            async with self.Session() as session:
                new_artifact = ArtifactModel(
                    artifact_id=artifact_id,
                    task_id=task_id,
                    step_id=step_id,
                    artifact_type=artifact_type,
                    uri=uri,
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

    async def get_latest_step(self, task_id: str, organization_id: str | None = None) -> Step | None:
        try:
            async with self.Session() as session:
                if step := (
                    await session.scalars(
                        select(StepModel)
                        .filter_by(task_id=task_id)
                        .filter_by(organization_id=organization_id)
                        .order_by(StepModel.order.desc())
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
        organization_id: str | None = None,
    ) -> Task:
        if status is None and extracted_information is None and failure_reason is None and errors is None:
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
        organization_id: str | None = None,
    ) -> list[Task]:
        """
        Get all tasks.
        :param page: Starts at 1
        :param page_size:
        :return:
        """
        if page < 1:
            raise ValueError(f"Page must be greater than 0, got {page}")

        try:
            async with self.Session() as session:
                db_page = page - 1  # offset logic is 0 based
                query = select(TaskModel).filter_by(organization_id=organization_id)
                if task_status:
                    query = query.filter(TaskModel.status.in_(task_status))
                query = query.order_by(TaskModel.created_at.desc()).limit(page_size).offset(db_page * page_size)
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
    ) -> OrganizationAuthToken | None:
        try:
            async with self.Session() as session:
                if token_obj := (
                    await session.scalars(
                        select(OrganizationAuthTokenModel)
                        .filter_by(organization_id=organization_id)
                        .filter_by(token_type=token_type)
                        .filter_by(token=token)
                        .filter_by(valid=True)
                    )
                ).first():
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
        workflow_permanent_id: str | None = None,
        version: int | None = None,
        is_saved_task: bool = False,
    ) -> Workflow:
        async with self.Session() as session:
            workflow = WorkflowModel(
                organization_id=organization_id,
                title=title,
                description=description,
                workflow_definition=workflow_definition,
                proxy_location=proxy_location,
                webhook_callback_url=webhook_callback_url,
                is_saved_task=is_saved_task,
            )
            if workflow_permanent_id:
                workflow.workflow_permanent_id = workflow_permanent_id
            if version:
                workflow.version = version
            session.add(workflow)
            await session.commit()
            await session.refresh(workflow)
            return convert_to_workflow(workflow, self.debug_enabled)

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
    ) -> Workflow | None:
        try:
            get_workflow_query = (
                select(WorkflowModel)
                .filter_by(workflow_permanent_id=workflow_permanent_id)
                .filter(WorkflowModel.deleted_at.is_(None))
            )
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

    async def get_workflows_by_organization_id(
        self,
        organization_id: str,
        page: int = 1,
        page_size: int = 10,
        only_saved_tasks: bool = False,
        only_workflows: bool = False,
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
        workflow_id: str,
        proxy_location: ProxyLocation | None = None,
        webhook_callback_url: str | None = None,
    ) -> WorkflowRun:
        try:
            async with self.Session() as session:
                workflow_run = WorkflowRunModel(
                    workflow_id=workflow_id,
                    proxy_location=proxy_location,
                    status="created",
                    webhook_callback_url=webhook_callback_url,
                )
                session.add(workflow_run)
                await session.commit()
                await session.refresh(workflow_run)
                return convert_to_workflow_run(workflow_run)
        except SQLAlchemyError:
            LOG.error("SQLAlchemyError", exc_info=True)
            raise

    async def update_workflow_run(self, workflow_run_id: str, status: WorkflowRunStatus) -> WorkflowRun | None:
        async with self.Session() as session:
            workflow_run = (
                await session.scalars(select(WorkflowRunModel).filter_by(workflow_run_id=workflow_run_id))
            ).first()
            if workflow_run:
                workflow_run.status = status
                await session.commit()
                await session.refresh(workflow_run)
                return convert_to_workflow_run(workflow_run)
            LOG.error(
                "WorkflowRun not found, nothing to update",
                workflow_run_id=workflow_run_id,
            )
            return None

    async def get_workflow_run(self, workflow_run_id: str) -> WorkflowRun | None:
        try:
            async with self.Session() as session:
                if workflow_run := (
                    await session.scalars(select(WorkflowRunModel).filter_by(workflow_run_id=workflow_run_id))
                ).first():
                    return convert_to_workflow_run(workflow_run)
                return None
        except SQLAlchemyError:
            LOG.error("SQLAlchemyError", exc_info=True)
            raise

    async def get_workflow_runs(self, workflow_id: str) -> list[WorkflowRun]:
        try:
            async with self.Session() as session:
                workflow_runs = (
                    await session.scalars(select(WorkflowRunModel).filter_by(workflow_id=workflow_id))
                ).all()
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
        url_parameter_key: str,
        key: str,
        description: str | None = None,
        bitwarden_collection_id: str | None = None,
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
            )
            session.add(bitwarden_login_credential_parameter)
            await session.commit()
            await session.refresh(bitwarden_login_credential_parameter)
            return convert_to_bitwarden_login_credential_parameter(bitwarden_login_credential_parameter)

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

    async def create_workflow_run_output_parameter(
        self,
        workflow_run_id: str,
        output_parameter_id: str,
        value: dict[str, Any] | list | str | None,
    ) -> WorkflowRunOutputParameter:
        try:
            async with self.Session() as session:
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
        self, workflow_run_id: str, workflow_parameter_id: str, value: Any
    ) -> WorkflowRunParameter:
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
                workflow_parameter = await self.get_workflow_parameter(workflow_parameter_id)
                if not workflow_parameter:
                    raise WorkflowParameterNotFound(workflow_parameter_id)
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
        url: str | None = None,
        navigation_goal: str | None = None,
        navigation_payload: dict[str, Any] | None = None,
        data_extraction_goal: str | None = None,
        extracted_information_schema: dict[str, Any] | None = None,
        llm: str | None = None,
        llm_prompt: str | None = None,
        llm_response: str | None = None,
    ) -> TaskGeneration:
        async with self.Session() as session:
            new_task_generation = TaskGenerationModel(
                organization_id=organization_id,
                user_prompt=user_prompt,
                url=url,
                navigation_goal=navigation_goal,
                navigation_payload=navigation_payload,
                data_extraction_goal=data_extraction_goal,
                extracted_information_schema=extracted_information_schema,
                llm=llm,
                llm_prompt=llm_prompt,
                llm_response=llm_response,
            )
            session.add(new_task_generation)
            await session.commit()
            await session.refresh(new_task_generation)
            return TaskGeneration.model_validate(new_task_generation)
