from datetime import datetime
from typing import Any

import structlog
from sqlalchemy import and_, create_engine, delete
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker

from skyvern.exceptions import WorkflowParameterNotFound
from skyvern.forge.sdk.api.chat_completion_price import ChatCompletionPrice
from skyvern.forge.sdk.artifact.models import Artifact, ArtifactType
from skyvern.forge.sdk.db.enums import OrganizationAuthTokenType
from skyvern.forge.sdk.db.exceptions import NotFoundError
from skyvern.forge.sdk.db.models import (
    ArtifactModel,
    AWSSecretParameterModel,
    OrganizationAuthTokenModel,
    OrganizationModel,
    StepModel,
    TaskModel,
    WorkflowModel,
    WorkflowParameterModel,
    WorkflowRunModel,
    WorkflowRunParameterModel,
)
from skyvern.forge.sdk.db.utils import (
    _custom_json_serializer,
    convert_to_artifact,
    convert_to_aws_secret_parameter,
    convert_to_organization,
    convert_to_organization_auth_token,
    convert_to_step,
    convert_to_task,
    convert_to_workflow,
    convert_to_workflow_parameter,
    convert_to_workflow_run,
    convert_to_workflow_run_parameter,
)
from skyvern.forge.sdk.models import Organization, OrganizationAuthToken, Step, StepStatus
from skyvern.forge.sdk.schemas.tasks import ProxyLocation, Task, TaskStatus
from skyvern.forge.sdk.workflow.models.parameter import AWSSecretParameter, WorkflowParameter, WorkflowParameterType
from skyvern.forge.sdk.workflow.models.workflow import Workflow, WorkflowRun, WorkflowRunParameter, WorkflowRunStatus
from skyvern.webeye.actions.models import AgentStepOutput

LOG = structlog.get_logger()


class AgentDB:
    def __init__(self, database_string: str, debug_enabled: bool = False) -> None:
        super().__init__()
        self.debug_enabled = debug_enabled
        self.engine = create_engine(database_string, json_serializer=_custom_json_serializer)
        self.Session = sessionmaker(bind=self.engine)

    async def create_task(
        self,
        url: str,
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
    ) -> Task:
        try:
            with self.Session() as session:
                new_task = TaskModel(
                    status="created",
                    url=url,
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
                )
                session.add(new_task)
                session.commit()
                session.refresh(new_task)
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
            with self.Session() as session:
                new_step = StepModel(
                    task_id=task_id,
                    order=order,
                    retry_index=retry_index,
                    status="created",
                    organization_id=organization_id,
                )
                session.add(new_step)
                session.commit()
                session.refresh(new_step)
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
            with self.Session() as session:
                new_artifact = ArtifactModel(
                    artifact_id=artifact_id,
                    task_id=task_id,
                    step_id=step_id,
                    artifact_type=artifact_type,
                    uri=uri,
                    organization_id=organization_id,
                )
                session.add(new_artifact)
                session.commit()
                session.refresh(new_artifact)
                return convert_to_artifact(new_artifact, self.debug_enabled)
        except SQLAlchemyError:
            LOG.exception("SQLAlchemyError", exc_info=True)
            raise
        except Exception:
            LOG.exception("UnexpectedError", exc_info=True)
            raise

    async def get_task(self, task_id: str, organization_id: str | None = None) -> Task | None:
        """Get a task by its id"""
        try:
            with self.Session() as session:
                if task_obj := (
                    session.query(TaskModel)
                    .filter_by(task_id=task_id)
                    .filter_by(organization_id=organization_id)
                    .first()
                ):
                    return convert_to_task(task_obj, self.debug_enabled)
                else:
                    LOG.info("Task not found", task_id=task_id, organization_id=organization_id)
                    return None
        except SQLAlchemyError:
            LOG.error("SQLAlchemyError", exc_info=True)
            raise
        except Exception:
            LOG.error("UnexpectedError", exc_info=True)
            raise

    async def get_step(self, task_id: str, step_id: str, organization_id: str | None = None) -> Step | None:
        try:
            with self.Session() as session:
                if step := (
                    session.query(StepModel)
                    .filter_by(step_id=step_id)
                    .filter_by(organization_id=organization_id)
                    .first()
                ):
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
            with self.Session() as session:
                if (
                    steps := session.query(StepModel)
                    .filter_by(task_id=task_id)
                    .filter_by(organization_id=organization_id)
                    .order_by(StepModel.order)
                    .order_by(StepModel.retry_index)
                    .all()
                ):
                    return [convert_to_step(step, debug_enabled=self.debug_enabled) for step in steps]
                else:
                    return []
        except SQLAlchemyError:
            LOG.error("SQLAlchemyError", exc_info=True)
            raise
        except Exception:
            LOG.error("UnexpectedError", exc_info=True)
            raise

    async def get_task_step_models(self, task_id: str, organization_id: str | None = None) -> list[StepModel]:
        try:
            with self.Session() as session:
                return (
                    session.query(StepModel)
                    .filter_by(task_id=task_id)
                    .filter_by(organization_id=organization_id)
                    .order_by(StepModel.order)
                    .order_by(StepModel.retry_index)
                    .all()
                )
        except SQLAlchemyError:
            LOG.error("SQLAlchemyError", exc_info=True)
            raise
        except Exception:
            LOG.error("UnexpectedError", exc_info=True)
            raise

    async def get_latest_step(self, task_id: str, organization_id: str | None = None) -> Step | None:
        try:
            with self.Session() as session:
                if step := (
                    session.query(StepModel)
                    .filter_by(task_id=task_id)
                    .filter_by(organization_id=organization_id)
                    .order_by(StepModel.order.desc())
                    .first()
                ):
                    return convert_to_step(step, debug_enabled=self.debug_enabled)
                else:
                    LOG.info("Latest step not found", task_id=task_id, organization_id=organization_id)
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
        chat_completion_price: ChatCompletionPrice | None = None,
    ) -> Step:
        try:
            with self.Session() as session:
                if (
                    step := session.query(StepModel)
                    .filter_by(task_id=task_id)
                    .filter_by(step_id=step_id)
                    .filter_by(organization_id=organization_id)
                    .first()
                ):
                    if status is not None:
                        step.status = status
                    if output is not None:
                        step.output = output.model_dump()
                    if is_last is not None:
                        step.is_last = is_last
                    if retry_index is not None:
                        step.retry_index = retry_index
                    if chat_completion_price is not None:
                        if step.input_token_count is None:
                            step.input_token_count = 0

                        if step.output_token_count is None:
                            step.output_token_count = 0

                        step.input_token_count += chat_completion_price.input_token_count
                        step.output_token_count += chat_completion_price.output_token_count
                        step.step_cost = chat_completion_price.openai_model_to_price_lambda(
                            step.input_token_count, step.output_token_count
                        )

                    session.commit()
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

    async def update_task(
        self,
        task_id: str,
        status: TaskStatus,
        extracted_information: dict[str, Any] | list | str | None = None,
        failure_reason: str | None = None,
        organization_id: str | None = None,
    ) -> Task:
        try:
            with self.Session() as session:
                if (
                    task := session.query(TaskModel)
                    .filter_by(task_id=task_id)
                    .filter_by(organization_id=organization_id)
                    .first()
                ):
                    task.status = status
                    if extracted_information is not None:
                        task.extracted_information = extracted_information
                    if failure_reason is not None:
                        task.failure_reason = failure_reason
                    session.commit()
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

    async def get_tasks(self, page: int = 1, page_size: int = 10, organization_id: str | None = None) -> list[Task]:
        """
        Get all tasks.
        :param page: Starts at 1
        :param page_size:
        :return:
        """
        if page < 1:
            raise ValueError(f"Page must be greater than 0, got {page}")

        try:
            with self.Session() as session:
                db_page = page - 1  # offset logic is 0 based
                tasks = (
                    session.query(TaskModel)
                    .filter_by(organization_id=organization_id)
                    .order_by(TaskModel.created_at.desc())
                    .limit(page_size)
                    .offset(db_page * page_size)
                    .all()
                )
                return [convert_to_task(task, debug_enabled=self.debug_enabled) for task in tasks]
        except SQLAlchemyError:
            LOG.error("SQLAlchemyError", exc_info=True)
            raise
        except Exception:
            LOG.error("UnexpectedError", exc_info=True)
            raise

    async def get_organization(self, organization_id: str) -> Organization | None:
        try:
            with self.Session() as session:
                if organization := (
                    session.query(OrganizationModel).filter_by(organization_id=organization_id).first()
                ):
                    return convert_to_organization(organization)
                else:
                    return None
        except SQLAlchemyError:
            LOG.error("SQLAlchemyError", exc_info=True)
            raise
        except Exception:
            LOG.error("UnexpectedError", exc_info=True)
            raise

    async def create_organization(
        self,
        organization_name: str,
        webhook_callback_url: str | None = None,
        max_steps_per_run: int | None = None,
    ) -> Organization:
        with self.Session() as session:
            org = OrganizationModel(
                organization_name=organization_name,
                webhook_callback_url=webhook_callback_url,
                max_steps_per_run=max_steps_per_run,
            )
            session.add(org)
            session.commit()
            session.refresh(org)

        return convert_to_organization(org)

    async def get_valid_org_auth_token(
        self,
        organization_id: str,
        token_type: OrganizationAuthTokenType,
    ) -> OrganizationAuthToken | None:
        try:
            with self.Session() as session:
                if token := (
                    session.query(OrganizationAuthTokenModel)
                    .filter_by(organization_id=organization_id)
                    .filter_by(token_type=token_type)
                    .filter_by(valid=True)
                    .first()
                ):
                    return convert_to_organization_auth_token(token)
                else:
                    return None
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
            with self.Session() as session:
                if token_obj := (
                    session.query(OrganizationAuthTokenModel)
                    .filter_by(organization_id=organization_id)
                    .filter_by(token_type=token_type)
                    .filter_by(token=token)
                    .filter_by(valid=True)
                    .first()
                ):
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
        with self.Session() as session:
            token = OrganizationAuthTokenModel(
                organization_id=organization_id,
                token_type=token_type,
                token=token,
            )
            session.add(token)
            session.commit()
            session.refresh(token)

        return convert_to_organization_auth_token(token)

    async def get_artifacts_for_task_step(
        self,
        task_id: str,
        step_id: str,
        organization_id: str | None = None,
    ) -> list[Artifact]:
        try:
            with self.Session() as session:
                if artifacts := (
                    session.query(ArtifactModel)
                    .filter_by(task_id=task_id)
                    .filter_by(step_id=step_id)
                    .filter_by(organization_id=organization_id)
                    .all()
                ):
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
            with self.Session() as session:
                if artifact := (
                    session.query(ArtifactModel)
                    .filter_by(artifact_id=artifact_id)
                    .filter_by(organization_id=organization_id)
                    .first()
                ):
                    return convert_to_artifact(artifact, self.debug_enabled)
                else:
                    return None
        except SQLAlchemyError:
            LOG.exception("SQLAlchemyError", exc_info=True)
            raise
        except Exception:
            LOG.exception("UnexpectedError", exc_info=True)
            raise

    async def get_artifact(
        self,
        task_id: str,
        step_id: str,
        artifact_type: ArtifactType,
        organization_id: str | None = None,
    ) -> Artifact | None:
        try:
            with self.Session() as session:
                artifact = (
                    session.query(ArtifactModel)
                    .filter_by(task_id=task_id)
                    .filter_by(step_id=step_id)
                    .filter_by(organization_id=organization_id)
                    .filter_by(artifact_type=artifact_type)
                    .order_by(ArtifactModel.created_at.desc())
                    .first()
                )
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
            with self.Session() as session:
                artifact = (
                    session.query(ArtifactModel)
                    .join(TaskModel, TaskModel.task_id == ArtifactModel.task_id)
                    .filter(TaskModel.workflow_run_id == workflow_run_id)
                    .filter(ArtifactModel.artifact_type == artifact_type)
                    .filter(ArtifactModel.organization_id == organization_id)
                    .order_by(ArtifactModel.created_at.desc())
                    .first()
                )
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
            with self.Session() as session:
                artifact_query = session.query(ArtifactModel).filter_by(task_id=task_id)
                if step_id:
                    artifact_query = artifact_query.filter_by(step_id=step_id)
                if organization_id:
                    artifact_query = artifact_query.filter_by(organization_id=organization_id)
                if artifact_types:
                    artifact_query = artifact_query.filter(ArtifactModel.artifact_type.in_(artifact_types))

                artifact = artifact_query.order_by(ArtifactModel.created_at.desc()).first()
                if artifact:
                    return convert_to_artifact(artifact, self.debug_enabled)
                return None
        except SQLAlchemyError:
            LOG.exception("SQLAlchemyError", exc_info=True)
            raise
        except Exception:
            LOG.exception("UnexpectedError", exc_info=True)
            raise

    async def get_latest_task_by_workflow_id(
        self,
        organization_id: str,
        workflow_id: str,
        before: datetime | None = None,
    ) -> Task | None:
        try:
            with self.Session() as session:
                query = (
                    session.query(TaskModel)
                    .filter_by(organization_id=organization_id)
                    .filter_by(workflow_id=workflow_id)
                )
                if before:
                    query = query.filter(TaskModel.created_at < before)
                task = query.order_by(TaskModel.created_at.desc()).first()
                if task:
                    return convert_to_task(task, debug_enabled=self.debug_enabled)
                return None
        except SQLAlchemyError:
            LOG.error("SQLAlchemyError", exc_info=True)
            raise

    async def create_workflow(
        self,
        organization_id: str,
        title: str,
        workflow_definition: dict[str, Any],
        description: str | None = None,
    ) -> Workflow:
        with self.Session() as session:
            workflow = WorkflowModel(
                organization_id=organization_id,
                title=title,
                description=description,
                workflow_definition=workflow_definition,
            )
            session.add(workflow)
            session.commit()
            session.refresh(workflow)
            return convert_to_workflow(workflow, self.debug_enabled)

    async def get_workflow(self, workflow_id: str) -> Workflow | None:
        try:
            with self.Session() as session:
                if workflow := session.query(WorkflowModel).filter_by(workflow_id=workflow_id).first():
                    return convert_to_workflow(workflow, self.debug_enabled)
                return None
        except SQLAlchemyError:
            LOG.error("SQLAlchemyError", exc_info=True)
            raise

    async def update_workflow(
        self,
        workflow_id: str,
        title: str | None = None,
        description: str | None = None,
        workflow_definition: dict[str, Any] | None = None,
    ) -> Workflow | None:
        with self.Session() as session:
            workflow = session.query(WorkflowModel).filter_by(workflow_id=workflow_id).first()
            if workflow:
                if title:
                    workflow.title = title
                if description:
                    workflow.description = description
                if workflow_definition:
                    workflow.workflow_definition = workflow_definition
                session.commit()
                session.refresh(workflow)
                return convert_to_workflow(workflow, self.debug_enabled)
            LOG.error("Workflow not found, nothing to update", workflow_id=workflow_id)
            return None

    async def create_workflow_run(
        self, workflow_id: str, proxy_location: ProxyLocation | None = None, webhook_callback_url: str | None = None
    ) -> WorkflowRun:
        try:
            with self.Session() as session:
                workflow_run = WorkflowRunModel(
                    workflow_id=workflow_id,
                    proxy_location=proxy_location,
                    status="created",
                    webhook_callback_url=webhook_callback_url,
                )
                session.add(workflow_run)
                session.commit()
                session.refresh(workflow_run)
                return convert_to_workflow_run(workflow_run)
        except SQLAlchemyError:
            LOG.error("SQLAlchemyError", exc_info=True)
            raise

    async def update_workflow_run(self, workflow_run_id: str, status: WorkflowRunStatus) -> WorkflowRun | None:
        with self.Session() as session:
            workflow_run = session.query(WorkflowRunModel).filter_by(workflow_run_id=workflow_run_id).first()
            if workflow_run:
                workflow_run.status = status
                session.commit()
                session.refresh(workflow_run)
                return convert_to_workflow_run(workflow_run)
            LOG.error("WorkflowRun not found, nothing to update", workflow_run_id=workflow_run_id)
            return None

    async def get_workflow_run(self, workflow_run_id: str) -> WorkflowRun | None:
        try:
            with self.Session() as session:
                if workflow_run := session.query(WorkflowRunModel).filter_by(workflow_run_id=workflow_run_id).first():
                    return convert_to_workflow_run(workflow_run)
                return None
        except SQLAlchemyError:
            LOG.error("SQLAlchemyError", exc_info=True)
            raise

    async def get_workflow_runs(self, workflow_id: str) -> list[WorkflowRun]:
        try:
            with self.Session() as session:
                workflow_runs = session.query(WorkflowRunModel).filter_by(workflow_id=workflow_id).all()
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
            with self.Session() as session:
                workflow_parameter = WorkflowParameterModel(
                    workflow_id=workflow_id,
                    workflow_parameter_type=workflow_parameter_type,
                    key=key,
                    default_value=default_value,
                    description=description,
                )
                session.add(workflow_parameter)
                session.commit()
                session.refresh(workflow_parameter)
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
        with self.Session() as session:
            aws_secret_parameter = AWSSecretParameterModel(
                workflow_id=workflow_id,
                key=key,
                aws_key=aws_key,
                description=description,
            )
            session.add(aws_secret_parameter)
            session.commit()
            session.refresh(aws_secret_parameter)
            return convert_to_aws_secret_parameter(aws_secret_parameter)

    async def get_workflow_parameters(self, workflow_id: str) -> list[WorkflowParameter]:
        try:
            with self.Session() as session:
                workflow_parameters = session.query(WorkflowParameterModel).filter_by(workflow_id=workflow_id).all()
                return [convert_to_workflow_parameter(parameter) for parameter in workflow_parameters]
        except SQLAlchemyError:
            LOG.error("SQLAlchemyError", exc_info=True)
            raise

    async def get_workflow_parameter(self, workflow_parameter_id: str) -> WorkflowParameter | None:
        try:
            with self.Session() as session:
                if workflow_parameter := (
                    session.query(WorkflowParameterModel).filter_by(workflow_parameter_id=workflow_parameter_id).first()
                ):
                    return convert_to_workflow_parameter(workflow_parameter, self.debug_enabled)
                return None
        except SQLAlchemyError:
            LOG.error("SQLAlchemyError", exc_info=True)
            raise

    async def create_workflow_run_parameter(
        self, workflow_run_id: str, workflow_parameter_id: str, value: Any
    ) -> WorkflowRunParameter:
        try:
            with self.Session() as session:
                workflow_run_parameter = WorkflowRunParameterModel(
                    workflow_run_id=workflow_run_id,
                    workflow_parameter_id=workflow_parameter_id,
                    value=value,
                )
                session.add(workflow_run_parameter)
                session.commit()
                session.refresh(workflow_run_parameter)
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
            with self.Session() as session:
                workflow_run_parameters = (
                    session.query(WorkflowRunParameterModel).filter_by(workflow_run_id=workflow_run_id).all()
                )
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
                                workflow_run_parameter, workflow_parameter, self.debug_enabled
                            ),
                        )
                    )
                return results
        except SQLAlchemyError:
            LOG.error("SQLAlchemyError", exc_info=True)
            raise

    async def get_last_task_for_workflow_run(self, workflow_run_id: str) -> Task | None:
        try:
            with self.Session() as session:
                if task := (
                    session.query(TaskModel)
                    .filter_by(workflow_run_id=workflow_run_id)
                    .order_by(TaskModel.created_at.desc())
                    .first()
                ):
                    return convert_to_task(task, debug_enabled=self.debug_enabled)
                return None
        except SQLAlchemyError:
            LOG.error("SQLAlchemyError", exc_info=True)
            raise

    async def get_tasks_by_workflow_run_id(self, workflow_run_id: str) -> list[Task]:
        try:
            with self.Session() as session:
                tasks = (
                    session.query(TaskModel)
                    .filter_by(workflow_run_id=workflow_run_id)
                    .order_by(TaskModel.created_at)
                    .all()
                )
                return [convert_to_task(task, debug_enabled=self.debug_enabled) for task in tasks]
        except SQLAlchemyError:
            LOG.error("SQLAlchemyError", exc_info=True)
            raise

    async def delete_task_artifacts(self, organization_id: str, task_id: str) -> None:
        with self.Session() as session:
            # delete artifacts by filtering organization_id and task_id
            stmt = delete(ArtifactModel).where(
                and_(
                    ArtifactModel.organization_id == organization_id,
                    ArtifactModel.task_id == task_id,
                )
            )
            session.execute(stmt)
            session.commit()

    async def delete_task_steps(self, organization_id: str, task_id: str) -> None:
        with self.Session() as session:
            # delete artifacts by filtering organization_id and task_id
            stmt = delete(StepModel).where(
                and_(
                    StepModel.organization_id == organization_id,
                    StepModel.task_id == task_id,
                )
            )
            session.execute(stmt)
            session.commit()
