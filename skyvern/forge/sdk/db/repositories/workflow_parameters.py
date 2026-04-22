from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

import structlog
from sqlalchemy import select

from skyvern.config import settings
from skyvern.forge.sdk.db._error_handling import db_operation
from skyvern.forge.sdk.db._sentinels import _UNSET
from skyvern.forge.sdk.db.base_repository import BaseRepository
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
from skyvern.webeye.actions.actions import Action

LOG = structlog.get_logger()


class WorkflowParametersRepository(BaseRepository):
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

    @staticmethod
    def _encode_workflow_parameter_default(parameter: WorkflowParameter) -> str | None:
        if parameter.default_value is None:
            return None
        if parameter.workflow_parameter_type == WorkflowParameterType.JSON:
            return json.dumps(parameter.default_value)
        return str(parameter.default_value)

    @staticmethod
    async def _reconcile_definition_parameters_in_session(
        session: Any,
        workflow_id: str,
        parameters: list[PARAMETER_TYPE],
    ) -> None:
        """Reconcile persisted WorkflowParameter + OutputParameter rows against ``parameters``.

        Preserves primary keys on in-place updates (workflow-run FKs reference
        them) and mutates each matched incoming parameter so its ID equals the
        DB row's ID — the caller must re-serialize ``workflow_definition``
        AFTER this call so the JSON carries the preserved IDs.
        """
        desired_workflow_params: list[WorkflowParameter] = [p for p in parameters if isinstance(p, WorkflowParameter)]
        desired_output_params: list[OutputParameter] = [p for p in parameters if isinstance(p, OutputParameter)]

        existing_workflow_rows = (
            await session.scalars(select(WorkflowParameterModel).filter_by(workflow_id=workflow_id))
        ).all()
        existing_by_identity: dict[tuple[str, str], WorkflowParameterModel] = {}
        existing_by_key_all_types: dict[str, list[WorkflowParameterModel]] = {}
        for row in existing_workflow_rows:
            existing_by_identity[(row.key, row.workflow_parameter_type)] = row
            existing_by_key_all_types.setdefault(row.key, []).append(row)

        desired_workflow_keys: set[str] = set()
        # Naive UTC to match the column's `datetime.utcnow` default.
        now = datetime.now(timezone.utc).replace(tzinfo=None)

        for parameter in desired_workflow_params:
            desired_workflow_keys.add(parameter.key)
            encoded_default = WorkflowParametersRepository._encode_workflow_parameter_default(parameter)
            type_value = parameter.workflow_parameter_type.value
            existing = existing_by_identity.get((parameter.key, type_value))
            if existing is not None:
                existing.description = parameter.description
                existing.default_value = encoded_default
                if existing.deleted_at is not None:
                    existing.deleted_at = None
                parameter.workflow_parameter_id = existing.workflow_parameter_id
                for other in existing_by_key_all_types.get(parameter.key, []):
                    if other is existing:
                        continue
                    if other.deleted_at is None:
                        other.deleted_at = now
                continue

            for other in existing_by_key_all_types.get(parameter.key, []):
                if other.deleted_at is None:
                    other.deleted_at = now
            new_row = WorkflowParameterModel(
                workflow_parameter_id=parameter.workflow_parameter_id,
                workflow_parameter_type=type_value,
                key=parameter.key,
                description=parameter.description,
                workflow_id=workflow_id,
                default_value=encoded_default,
            )
            session.add(new_row)

        for row in existing_workflow_rows:
            if row.key in desired_workflow_keys:
                continue
            if row.deleted_at is None:
                row.deleted_at = now

        existing_output_rows = (
            await session.scalars(select(OutputParameterModel).filter_by(workflow_id=workflow_id))
        ).all()
        existing_output_by_key: dict[str, OutputParameterModel] = {row.key: row for row in existing_output_rows}
        desired_output_keys: set[str] = set()
        for parameter in desired_output_params:
            desired_output_keys.add(parameter.key)
            existing = existing_output_by_key.get(parameter.key)
            if existing is not None:
                existing.description = parameter.description
                if existing.deleted_at is not None:
                    existing.deleted_at = None
                # Blocks in workflow_definition hold the same OutputParameter
                # instance (see workflow_definition_converter.block_yaml_to_block),
                # so this patch aligns every block reference on re-serialize.
                parameter.output_parameter_id = existing.output_parameter_id
                continue
            new_row = OutputParameterModel(
                output_parameter_id=parameter.output_parameter_id,
                key=parameter.key,
                description=parameter.description,
                workflow_id=workflow_id,
            )
            session.add(new_row)

        for row in existing_output_rows:
            if row.key in desired_output_keys:
                continue
            if row.deleted_at is None:
                row.deleted_at = now

    @db_operation("get_workflow_output_parameters")
    async def get_workflow_output_parameters(self, workflow_id: str) -> list[OutputParameter]:
        async with self.Session() as session:
            output_parameters = (
                await session.scalars(
                    select(OutputParameterModel)
                    .filter_by(workflow_id=workflow_id)
                    .where(OutputParameterModel.deleted_at.is_(None))
                )
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
                await session.scalars(
                    select(WorkflowParameterModel)
                    .filter_by(workflow_id=workflow_id)
                    .where(WorkflowParameterModel.deleted_at.is_(None))
                )
            ).all()
            return [convert_to_workflow_parameter(parameter) for parameter in workflow_parameters]

    @db_operation("get_workflow_parameter")
    async def get_workflow_parameter(
        self, workflow_parameter_id: str, organization_id: str | None = None
    ) -> WorkflowParameter | None:
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
        before_time = datetime.now(timezone.utc) - timedelta(hours=query_window_hours)
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
