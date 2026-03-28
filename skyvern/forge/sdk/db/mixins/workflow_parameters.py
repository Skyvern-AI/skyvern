from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy import select

from skyvern.config import settings
from skyvern.forge.sdk.db._error_handling import db_operation
from skyvern.forge.sdk.db.exceptions import NotFoundError
from skyvern.forge.sdk.db.models import (
    ActionModel,
    AISuggestionModel,
    AWSSecretParameterModel,
    AzureVaultCredentialParameterModel,
    Base,
    BitwardenCreditCardDataParameterModel,
    BitwardenLoginCredentialParameterModel,
    BitwardenSensitiveInformationParameterModel,
    CredentialParameterModel,
    OnePasswordCredentialParameterModel,
    OutputParameterModel,
    TaskGenerationModel,
    TaskModel,
    TaskRunModel,
    WorkflowCopilotChatMessageModel,
    WorkflowCopilotChatModel,
    WorkflowParameterModel,
)
from skyvern.forge.sdk.db.utils import (
    convert_to_aws_secret_parameter,
    convert_to_output_parameter,
    convert_to_workflow_copilot_chat_message,
    convert_to_workflow_parameter,
    hydrate_action,
)
from skyvern.forge.sdk.schemas.ai_suggestions import AISuggestion
from skyvern.forge.sdk.schemas.runs import Run
from skyvern.forge.sdk.schemas.task_generations import TaskGeneration
from skyvern.forge.sdk.schemas.tasks import Task, TaskStatus
from skyvern.forge.sdk.schemas.workflow_copilot import (
    WorkflowCopilotChat,
    WorkflowCopilotChatMessage,
    WorkflowCopilotChatSender,
)
from skyvern.forge.sdk.workflow.models.parameter import (
    PARAMETER_TYPE,
    AWSSecretParameter,
    AzureVaultCredentialParameter,
    BitwardenCreditCardDataParameter,
    BitwardenLoginCredentialParameter,
    BitwardenSensitiveInformationParameter,
    ContextParameter,
    CredentialParameter,
    OnePasswordCredentialParameter,
    OutputParameter,
    WorkflowParameter,
    WorkflowParameterType,
)
from skyvern.schemas.runs import RunType
from skyvern.webeye.actions.actions import Action

if TYPE_CHECKING:
    from skyvern.forge.sdk.db.base_alchemy_db import _SessionFactory

from skyvern.forge.sdk.db._sentinels import _UNSET

LOG = structlog.get_logger()


class WorkflowParametersMixin:
    Session: _SessionFactory
    debug_enabled: bool
    """Database operations for workflow parameters, copilot chat, task generation, actions, and runs."""

    @db_operation("create_workflow_parameter")
    async def create_workflow_parameter(
        self,
        workflow_id: str,
        workflow_parameter_type: WorkflowParameterType,
        key: str,
        default_value: Any,
        description: str | None = None,
    ) -> WorkflowParameter:
        async with self.Session() as session:
            if default_value is None:
                pass
            elif workflow_parameter_type == WorkflowParameterType.JSON:
                default_value = json.dumps(default_value)
            else:
                default_value = str(default_value)
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

    @db_operation("create_aws_secret_parameter")
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

    @db_operation("create_output_parameter")
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

    @staticmethod
    def _convert_parameter_to_model(parameter: PARAMETER_TYPE) -> Base:
        """Convert a parameter object to its corresponding SQLAlchemy model."""
        if isinstance(parameter, WorkflowParameter):
            if parameter.default_value is None:
                default_value = None
            elif parameter.workflow_parameter_type == WorkflowParameterType.JSON:
                default_value = json.dumps(parameter.default_value)
            else:
                default_value = str(parameter.default_value)
            return WorkflowParameterModel(
                workflow_parameter_id=parameter.workflow_parameter_id,
                workflow_parameter_type=parameter.workflow_parameter_type.value,
                key=parameter.key,
                description=parameter.description,
                workflow_id=parameter.workflow_id,
                default_value=default_value,
                deleted_at=parameter.deleted_at,
            )
        elif isinstance(parameter, OutputParameter):
            return OutputParameterModel(
                output_parameter_id=parameter.output_parameter_id,
                key=parameter.key,
                description=parameter.description,
                workflow_id=parameter.workflow_id,
                deleted_at=parameter.deleted_at,
            )
        elif isinstance(parameter, AWSSecretParameter):
            return AWSSecretParameterModel(
                aws_secret_parameter_id=parameter.aws_secret_parameter_id,
                workflow_id=parameter.workflow_id,
                key=parameter.key,
                description=parameter.description,
                aws_key=parameter.aws_key,
                deleted_at=parameter.deleted_at,
            )
        elif isinstance(parameter, BitwardenLoginCredentialParameter):
            return BitwardenLoginCredentialParameterModel(
                bitwarden_login_credential_parameter_id=parameter.bitwarden_login_credential_parameter_id,
                workflow_id=parameter.workflow_id,
                key=parameter.key,
                description=parameter.description,
                bitwarden_client_id_aws_secret_key=parameter.bitwarden_client_id_aws_secret_key,
                bitwarden_client_secret_aws_secret_key=parameter.bitwarden_client_secret_aws_secret_key,
                bitwarden_master_password_aws_secret_key=parameter.bitwarden_master_password_aws_secret_key,
                bitwarden_collection_id=parameter.bitwarden_collection_id,
                bitwarden_item_id=parameter.bitwarden_item_id,
                url_parameter_key=parameter.url_parameter_key,
                deleted_at=parameter.deleted_at,
            )
        elif isinstance(parameter, BitwardenSensitiveInformationParameter):
            return BitwardenSensitiveInformationParameterModel(
                bitwarden_sensitive_information_parameter_id=parameter.bitwarden_sensitive_information_parameter_id,
                workflow_id=parameter.workflow_id,
                key=parameter.key,
                description=parameter.description,
                bitwarden_client_id_aws_secret_key=parameter.bitwarden_client_id_aws_secret_key,
                bitwarden_client_secret_aws_secret_key=parameter.bitwarden_client_secret_aws_secret_key,
                bitwarden_master_password_aws_secret_key=parameter.bitwarden_master_password_aws_secret_key,
                bitwarden_collection_id=parameter.bitwarden_collection_id,
                bitwarden_identity_key=parameter.bitwarden_identity_key,
                bitwarden_identity_fields=parameter.bitwarden_identity_fields,
                deleted_at=parameter.deleted_at,
            )
        elif isinstance(parameter, BitwardenCreditCardDataParameter):
            return BitwardenCreditCardDataParameterModel(
                bitwarden_credit_card_data_parameter_id=parameter.bitwarden_credit_card_data_parameter_id,
                workflow_id=parameter.workflow_id,
                key=parameter.key,
                description=parameter.description,
                bitwarden_client_id_aws_secret_key=parameter.bitwarden_client_id_aws_secret_key,
                bitwarden_client_secret_aws_secret_key=parameter.bitwarden_client_secret_aws_secret_key,
                bitwarden_master_password_aws_secret_key=parameter.bitwarden_master_password_aws_secret_key,
                bitwarden_collection_id=parameter.bitwarden_collection_id,
                bitwarden_item_id=parameter.bitwarden_item_id,
                deleted_at=parameter.deleted_at,
            )
        elif isinstance(parameter, CredentialParameter):
            return CredentialParameterModel(
                credential_parameter_id=parameter.credential_parameter_id,
                workflow_id=parameter.workflow_id,
                key=parameter.key,
                description=parameter.description,
                credential_id=parameter.credential_id,
                deleted_at=parameter.deleted_at,
            )
        elif isinstance(parameter, OnePasswordCredentialParameter):
            return OnePasswordCredentialParameterModel(
                onepassword_credential_parameter_id=parameter.onepassword_credential_parameter_id,
                workflow_id=parameter.workflow_id,
                key=parameter.key,
                description=parameter.description,
                vault_id=parameter.vault_id,
                item_id=parameter.item_id,
                deleted_at=parameter.deleted_at,
            )
        elif isinstance(parameter, AzureVaultCredentialParameter):
            return AzureVaultCredentialParameterModel(
                azure_vault_credential_parameter_id=parameter.azure_vault_credential_parameter_id,
                workflow_id=parameter.workflow_id,
                key=parameter.key,
                description=parameter.description,
                vault_name=parameter.vault_name,
                username_key=parameter.username_key,
                password_key=parameter.password_key,
                totp_secret_key=parameter.totp_secret_key,
                deleted_at=parameter.deleted_at,
            )
        else:
            raise ValueError(f"Unsupported workflow definition parameter type: {type(parameter).__name__}")

    @db_operation("save_workflow_definition_parameters")
    async def save_workflow_definition_parameters(self, parameters: list[PARAMETER_TYPE]) -> None:
        """Save multiple workflow definition parameters in a single transaction."""

        # ContextParameter is not persisted
        parameters_to_save = [p for p in parameters if not isinstance(p, ContextParameter)]
        if not parameters_to_save:
            return

        async with self.Session() as session:
            for parameter in parameters_to_save:
                model = self._convert_parameter_to_model(parameter)
                session.add(model)
            await session.commit()

    @db_operation("get_workflow_output_parameters")
    async def get_workflow_output_parameters(self, workflow_id: str) -> list[OutputParameter]:
        async with self.Session() as session:
            output_parameters = (
                await session.scalars(select(OutputParameterModel).filter_by(workflow_id=workflow_id))
            ).all()
            return [convert_to_output_parameter(parameter) for parameter in output_parameters]

    @db_operation("get_workflow_output_parameters_by_ids")
    async def get_workflow_output_parameters_by_ids(self, output_parameter_ids: list[str]) -> list[OutputParameter]:
        async with self.Session() as session:
            output_parameters = (
                await session.scalars(
                    select(OutputParameterModel).filter(
                        OutputParameterModel.output_parameter_id.in_(output_parameter_ids)
                    )
                )
            ).all()
            return [convert_to_output_parameter(parameter) for parameter in output_parameters]

    @db_operation("get_workflow_parameters")
    async def get_workflow_parameters(self, workflow_id: str) -> list[WorkflowParameter]:
        async with self.Session() as session:
            workflow_parameters = (
                await session.scalars(select(WorkflowParameterModel).filter_by(workflow_id=workflow_id))
            ).all()
            return [convert_to_workflow_parameter(parameter) for parameter in workflow_parameters]

    @db_operation("get_workflow_parameter")
    async def get_workflow_parameter(self, workflow_parameter_id: str) -> WorkflowParameter | None:
        async with self.Session() as session:
            if workflow_parameter := (
                await session.scalars(
                    select(WorkflowParameterModel).filter_by(workflow_parameter_id=workflow_parameter_id)
                )
            ).first():
                return convert_to_workflow_parameter(workflow_parameter, self.debug_enabled)
            return None

    @db_operation("create_task_generation")
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

    @db_operation("create_ai_suggestion")
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

    @db_operation("create_workflow_copilot_chat")
    async def create_workflow_copilot_chat(
        self,
        organization_id: str,
        workflow_permanent_id: str,
    ) -> WorkflowCopilotChat:
        async with self.Session() as session:
            new_chat = WorkflowCopilotChatModel(
                organization_id=organization_id,
                workflow_permanent_id=workflow_permanent_id,
            )
            session.add(new_chat)
            await session.commit()
            await session.refresh(new_chat)
            return WorkflowCopilotChat.model_validate(new_chat)

    @db_operation("update_workflow_copilot_chat")
    async def update_workflow_copilot_chat(
        self,
        organization_id: str,
        workflow_copilot_chat_id: str,
        proposed_workflow: dict | None | object = _UNSET,
        auto_accept: bool | None = None,
    ) -> WorkflowCopilotChat | None:
        async with self.Session() as session:
            chat = (
                await session.scalars(
                    select(WorkflowCopilotChatModel)
                    .where(WorkflowCopilotChatModel.organization_id == organization_id)
                    .where(WorkflowCopilotChatModel.workflow_copilot_chat_id == workflow_copilot_chat_id)
                )
            ).first()
            if not chat:
                return None

            if proposed_workflow is not _UNSET:
                chat.proposed_workflow = proposed_workflow
            if auto_accept is not None:
                chat.auto_accept = auto_accept

            await session.commit()
            await session.refresh(chat)
            return WorkflowCopilotChat.model_validate(chat)

    @db_operation("create_workflow_copilot_chat_message")
    async def create_workflow_copilot_chat_message(
        self,
        organization_id: str,
        workflow_copilot_chat_id: str,
        sender: WorkflowCopilotChatSender,
        content: str,
        global_llm_context: str | None = None,
    ) -> WorkflowCopilotChatMessage:
        async with self.Session() as session:
            new_message = WorkflowCopilotChatMessageModel(
                workflow_copilot_chat_id=workflow_copilot_chat_id,
                organization_id=organization_id,
                sender=sender,
                content=content,
                global_llm_context=global_llm_context,
            )
            session.add(new_message)
            await session.commit()
            await session.refresh(new_message)
            return convert_to_workflow_copilot_chat_message(new_message, self.debug_enabled)

    @db_operation("get_workflow_copilot_chat_messages")
    async def get_workflow_copilot_chat_messages(
        self,
        workflow_copilot_chat_id: str,
    ) -> list[WorkflowCopilotChatMessage]:
        async with self.Session() as session:
            query = (
                select(WorkflowCopilotChatMessageModel)
                .filter(WorkflowCopilotChatMessageModel.workflow_copilot_chat_id == workflow_copilot_chat_id)
                .order_by(WorkflowCopilotChatMessageModel.workflow_copilot_chat_message_id.asc())
            )
            messages = (await session.scalars(query)).all()
            return [convert_to_workflow_copilot_chat_message(message, self.debug_enabled) for message in messages]

    @db_operation("get_workflow_copilot_chat_by_id")
    async def get_workflow_copilot_chat_by_id(
        self,
        organization_id: str,
        workflow_copilot_chat_id: str,
    ) -> WorkflowCopilotChat | None:
        async with self.Session() as session:
            query = (
                select(WorkflowCopilotChatModel)
                .filter(WorkflowCopilotChatModel.organization_id == organization_id)
                .filter(WorkflowCopilotChatModel.workflow_copilot_chat_id == workflow_copilot_chat_id)
                .order_by(WorkflowCopilotChatModel.created_at.desc())
                .limit(1)
            )
            chat = (await session.scalars(query)).first()
            if not chat:
                return None
            return WorkflowCopilotChat.model_validate(chat)

    @db_operation("get_latest_workflow_copilot_chat")
    async def get_latest_workflow_copilot_chat(
        self,
        organization_id: str,
        workflow_permanent_id: str,
    ) -> WorkflowCopilotChat | None:
        async with self.Session() as session:
            query = (
                select(WorkflowCopilotChatModel)
                .filter(WorkflowCopilotChatModel.organization_id == organization_id)
                .filter(WorkflowCopilotChatModel.workflow_permanent_id == workflow_permanent_id)
                .order_by(WorkflowCopilotChatModel.created_at.desc())
                .limit(1)
            )
            chat = (await session.scalars(query)).first()
            if not chat:
                return None
            return WorkflowCopilotChat.model_validate(chat)

    @db_operation("get_task_generation_by_prompt_hash")
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

    @db_operation("create_action")
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
                screenshot_artifact_id=action.screenshot_artifact_id,
                action_json=action.model_dump(),
                confidence_float=action.confidence_float,
                created_by=action.created_by,
            )
            session.add(new_action)
            await session.commit()
            await session.refresh(new_action)
            return hydrate_action(new_action)

    @db_operation("update_action_reasoning")
    async def update_action_reasoning(
        self,
        organization_id: str,
        action_id: str,
        reasoning: str,
    ) -> Action:
        async with self.Session() as session:
            action = (
                await session.scalars(
                    select(ActionModel).filter_by(action_id=action_id).filter_by(organization_id=organization_id)
                )
            ).first()
            if action:
                action.reasoning = reasoning
                await session.commit()
                await session.refresh(action)
                return Action.model_validate(action)
            raise NotFoundError(f"Action {action_id}")

    @db_operation("retrieve_action_plan")
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

    @db_operation("create_task_run")
    async def create_task_run(
        self,
        task_run_type: RunType,
        organization_id: str,
        run_id: str,
        title: str | None = None,
        url: str | None = None,
        url_hash: str | None = None,
    ) -> Run:
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
            return Run.model_validate(task_run)

    @db_operation("update_task_run")
    async def update_task_run(
        self,
        organization_id: str,
        run_id: str,
        title: str | None = None,
        url: str | None = None,
        url_hash: str | None = None,
    ) -> None:
        async with self.Session() as session:
            task_run = (
                await session.scalars(
                    select(TaskRunModel).filter_by(run_id=run_id).filter_by(organization_id=organization_id)
                )
            ).first()
            if not task_run:
                raise NotFoundError(f"TaskRun {run_id} not found")

            if title:
                task_run.title = title
            if url:
                task_run.url = url
            if url_hash:
                task_run.url_hash = url_hash
            await session.commit()

    @db_operation("update_job_run_compute_cost")
    async def update_job_run_compute_cost(
        self,
        organization_id: str,
        run_id: str,
        instance_type: str | None = None,
        vcpu_millicores: int | None = None,
        memory_mb: int | None = None,
        duration_ms: int | None = None,
        compute_cost: float | None = None,
    ) -> None:
        """Update compute cost metrics for a job run."""
        async with self.Session() as session:
            task_run = (
                await session.scalars(
                    select(TaskRunModel).filter_by(run_id=run_id).filter_by(organization_id=organization_id)
                )
            ).first()
            if not task_run:
                LOG.warning(
                    "TaskRun not found for compute cost update",
                    run_id=run_id,
                    organization_id=organization_id,
                )
                return

            if instance_type is not None:
                task_run.instance_type = instance_type
            if vcpu_millicores is not None:
                task_run.vcpu_millicores = vcpu_millicores
            if memory_mb is not None:
                task_run.memory_mb = memory_mb
            if duration_ms is not None:
                task_run.duration_ms = duration_ms
            if compute_cost is not None:
                task_run.compute_cost = compute_cost
            await session.commit()

    @db_operation("cache_task_run")
    async def cache_task_run(self, run_id: str, organization_id: str | None = None) -> Run:
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
                return Run.model_validate(task_run)
            raise NotFoundError(f"Run {run_id} not found")

    @db_operation("get_cached_task_run")
    async def get_cached_task_run(
        self, task_run_type: RunType, url_hash: str | None = None, organization_id: str | None = None
    ) -> Run | None:
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
            return Run.model_validate(task_run) if task_run else None

    @db_operation("get_run")
    async def get_run(
        self,
        run_id: str,
        organization_id: str | None = None,
    ) -> Run | None:
        async with self.Session() as session:
            query = select(TaskRunModel).filter_by(run_id=run_id)
            if organization_id:
                query = query.filter_by(organization_id=organization_id)
            task_run = (await session.scalars(query)).first()
            return Run.model_validate(task_run) if task_run else None
