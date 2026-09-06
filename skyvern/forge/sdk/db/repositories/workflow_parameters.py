from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

import structlog
from sqlalchemy import cast, func, select
from sqlalchemy.dialects.postgresql import JSONB, JSONPATH
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer

from skyvern.config import settings
from skyvern.forge.sdk.copilot.ask_user import (
    QUESTION_CLIENT_GRACE,
    QuestionInteraction,
    QuestionResponse,
    question_wait_is_live,
    resolve_question_response,
)
from skyvern.forge.sdk.copilot.completion_criteria_store import criteria_from_json, criterion_authority_projection
from skyvern.forge.sdk.copilot.context import TurnNarrativePayload
from skyvern.forge.sdk.db._error_handling import db_operation
from skyvern.forge.sdk.db._sentinels import _UNSET
from skyvern.forge.sdk.db.base_repository import BaseRepository
from skyvern.forge.sdk.db.exceptions import DuplicateCopilotTurnError, NotFoundError
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
    WorkflowCopilotCompletionCriteriaSetModel,
    WorkflowModel,
    WorkflowParameterModel,
)
from skyvern.forge.sdk.db.utils import (
    convert_to_aws_secret_parameter,
    convert_to_output_parameter,
    convert_to_workflow_copilot_chat_message,
    convert_to_workflow_parameter,
    escape_like_term,
    hydrate_action,
    summarize_copilot_chat_title,
)
from skyvern.forge.sdk.schemas.ai_suggestions import AISuggestion
from skyvern.forge.sdk.schemas.copilot_turn_outcome import TurnOutcome
from skyvern.forge.sdk.schemas.task_generations import TaskGeneration
from skyvern.forge.sdk.schemas.tasks import Task, TaskStatus
from skyvern.forge.sdk.schemas.workflow_copilot import (
    CopilotPendingTurn,
    NonAdoptableCriteriaSet,
    WorkflowCopilotChat,
    WorkflowCopilotChatMessage,
    WorkflowCopilotChatSender,
    WorkflowCopilotChatSummary,
    WorkflowCopilotCompletionCriteriaSet,
)
from skyvern.forge.sdk.trace import traced
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
from skyvern.utils.action_redaction import redact_action_for_log
from skyvern.webeye.actions.actions import Action

LOG = structlog.get_logger()

PENDING_TURN_RETENTION = timedelta(days=30)


def _pending_turn_id_for_idempotency_digest(
    pending_turns: Mapping[str, object], idempotency_digest: str | None
) -> str | None:
    if not idempotency_digest:
        return None
    for turn_id, value in pending_turns.items():
        if isinstance(value, Mapping) and value.get("idempotency_digest") == idempotency_digest:
            return turn_id
    return None


def _completed_turn_id_for_idempotency_digest(
    turn_outcomes: list[object], idempotency_digest: str | None
) -> str | None:
    if not idempotency_digest:
        return None
    for value in turn_outcomes:
        if not isinstance(value, Mapping) or value.get("idempotency_digest") != idempotency_digest:
            continue
        turn_id = value.get("copilot_turn_id")
        return turn_id if isinstance(turn_id, str) and turn_id else None
    return None


def _floor_rekeyed_association_is_coherent(item: Mapping[str, object]) -> bool:
    marker_present = "requested_output_floor_rekeyed" in item
    path_present = "floor_rekeyed_from_path" in item
    if not marker_present and not path_present:
        return True
    return (
        marker_present
        and path_present
        and item["requested_output_floor_rekeyed"] is True
        and isinstance(item["floor_rekeyed_from_path"], str)
    )


def _decode_completion_criteria_set(
    row: WorkflowCopilotCompletionCriteriaSetModel,
) -> WorkflowCopilotCompletionCriteriaSet | NonAdoptableCriteriaSet:
    """A current or v1 row adopts only when every recorded criterion decodes without loss."""
    raw_criteria = row.criteria
    try:
        if isinstance(raw_criteria, list):
            current = WorkflowCopilotCompletionCriteriaSet.model_validate(row)
            inner = current.criteria
            is_v1_envelope = False
        elif (
            isinstance(raw_criteria, dict)
            and raw_criteria.get("contract_version") == 1
            and isinstance(raw_criteria.get("criteria"), list)
        ):
            inner = raw_criteria["criteria"]
            current = None
            is_v1_envelope = True
        else:
            inner = None
            current = None
            is_v1_envelope = False

        decoded = criteria_from_json(inner)
        if inner is not None and (
            (is_v1_envelope and not inner)
            or len(decoded) != len(inner)
            or any(
                not _floor_rekeyed_association_is_coherent(item)
                or any(
                    field in item and item[field] != canonical_value
                    for field, canonical_value in criterion_authority_projection(
                        criterion,
                        stored_item=item,
                    ).items()
                )
                or ("antecedent_family" in item and item["antecedent_family"] is None)
                for item, criterion in zip(inner, decoded)
            )
        ):
            return NonAdoptableCriteriaSet(
                reason="undecodable_v1_criteria",
                completion_criteria_set_id=row.completion_criteria_set_id,
                goal_epoch=row.goal_epoch,
            )
        if current is not None:
            return current
        if is_v1_envelope:
            return WorkflowCopilotCompletionCriteriaSet(
                completion_criteria_set_id=row.completion_criteria_set_id,
                organization_id=row.organization_id,
                workflow_copilot_chat_id=row.workflow_copilot_chat_id,
                goal_epoch=row.goal_epoch,
                status=row.status,
                criteria=inner,
                source_turn_id=row.source_turn_id,
                source_goal_text=row.source_goal_text,
                consecutive_all_no_evidence=row.consecutive_all_no_evidence,
                tripwire_fired=row.tripwire_fired,
                last_fully_satisfied_workflow_yaml=row.last_fully_satisfied_workflow_yaml,
                superseded_by_set_id=row.superseded_by_set_id,
                superseded_at=row.superseded_at,
                supersede_reason=row.supersede_reason,
                created_at=row.created_at,
                modified_at=row.modified_at,
            )
    except Exception:
        LOG.warning(
            "copilot completion criteria set decode raised; treating row as unknown shape",
            completion_criteria_set_id=row.completion_criteria_set_id,
            goal_epoch=row.goal_epoch,
            exc_info=True,
        )
    return NonAdoptableCriteriaSet(
        reason="unknown_shape",
        completion_criteria_set_id=row.completion_criteria_set_id,
        goal_epoch=row.goal_epoch,
    )


def _parse_pending_turn_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _prune_pending_turns(pending_turns: object) -> dict[str, Any]:
    """Drop markers too old to still be worth reconciling, so the column cannot grow without bound."""
    if not isinstance(pending_turns, dict):
        return {}
    now = datetime.now(timezone.utc)
    cutoff = now - PENDING_TURN_RETENTION
    kept: dict[str, Any] = {}
    for turn_id, entry in pending_turns.items():
        if not isinstance(entry, dict):
            continue
        started_at = _parse_pending_turn_timestamp(entry.get("started_at"))
        last_activity = started_at
        live_question = False
        for question in entry.get("question_interactions") or []:
            if not isinstance(question, dict):
                continue
            resolved_at = _parse_pending_turn_timestamp(question.get("resolved_at"))
            if resolved_at is not None:
                last_activity = max(last_activity, resolved_at) if last_activity else resolved_at
            client_seen = _parse_pending_turn_timestamp(
                entry.get("question_client_seen_at")
            ) or _parse_pending_turn_timestamp(question.get("created_at"))
            live_question = live_question or (
                question.get("status") == "pending"
                and question_wait_is_live(_parse_pending_turn_timestamp(entry.get("question_heartbeat_at")), now)
                and client_seen is not None
                and now - client_seen < QUESTION_CLIENT_GRACE
            )
        if last_activity is not None and last_activity < cutoff and not live_question:
            continue
        kept[turn_id] = entry
    return kept


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
                credential_ids=parameter.credential_ids,
                selection_strategy=parameter.selection_strategy,
                fallback_credential_ids=parameter.fallback_credential_ids,
                fallback_trigger=parameter.fallback_trigger,
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

    @db_operation("get_workflow_parameters_by_ids")
    async def get_workflow_parameters_by_ids(self, workflow_parameter_ids: list[str]) -> list[WorkflowParameter]:
        # Batch equivalent of get_workflow_parameter: matches on id only, without a
        # deleted_at filter, so historical workflow-run lookups still resolve soft-deleted params.
        if not workflow_parameter_ids:
            return []
        async with self.Session() as session:
            workflow_parameters = (
                await session.scalars(
                    select(WorkflowParameterModel).where(
                        WorkflowParameterModel.workflow_parameter_id.in_(workflow_parameter_ids)
                    )
                )
            ).all()
            return [convert_to_workflow_parameter(parameter, self.debug_enabled) for parameter in workflow_parameters]

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
        audio_artifact_id: str | None = None,
        global_llm_context: str | None = None,
        turn_outcome: TurnOutcome | None = None,
        narrative_payload: TurnNarrativePayload | dict[str, Any] | None = None,
    ) -> WorkflowCopilotChatMessage:
        async with self.Session() as session:
            new_message = WorkflowCopilotChatMessageModel(
                workflow_copilot_chat_id=workflow_copilot_chat_id,
                organization_id=organization_id,
                sender=sender,
                content=content,
                audio_artifact_id=audio_artifact_id,
                global_llm_context=global_llm_context,
                turn_outcome=turn_outcome.model_dump(mode="json") if turn_outcome is not None else None,
                narrative_payload=narrative_payload,
            )
            session.add(new_message)
            await session.commit()
            await session.refresh(new_message)
            return convert_to_workflow_copilot_chat_message(new_message, self.debug_enabled)

    @db_operation("start_copilot_turn")
    async def start_copilot_turn(
        self,
        organization_id: str,
        workflow_copilot_chat_id: str,
        pending_turn: CopilotPendingTurn,
        user_message: str,
        audio_artifact_id: str | None = None,
        sender: WorkflowCopilotChatSender = WorkflowCopilotChatSender.USER,
    ) -> WorkflowCopilotChatMessage:
        """Write the turn's opening row and its pending marker in one transaction.

        Two separate commits would leave a crash window in which the user
        message exists with no marker to reconcile it.
        """
        async with self.Session() as session:
            chat = (
                await session.scalars(
                    select(WorkflowCopilotChatModel)
                    .where(WorkflowCopilotChatModel.organization_id == organization_id)
                    .where(WorkflowCopilotChatModel.workflow_copilot_chat_id == workflow_copilot_chat_id)
                    .with_for_update()
                )
            ).first()
            if chat is None:
                raise NotFoundError(f"workflow copilot chat {workflow_copilot_chat_id}")
            pending = _prune_pending_turns(chat.pending_turns)
            existing_turn_id = _pending_turn_id_for_idempotency_digest(
                pending,
                pending_turn.idempotency_digest,
            )
            if existing_turn_id is not None:
                raise DuplicateCopilotTurnError(existing_turn_id)
            if pending_turn.idempotency_digest is not None:
                completed_outcomes = list(
                    await session.scalars(
                        select(WorkflowCopilotChatMessageModel.turn_outcome)
                        .where(WorkflowCopilotChatMessageModel.organization_id == organization_id)
                        .where(WorkflowCopilotChatMessageModel.workflow_copilot_chat_id == workflow_copilot_chat_id)
                        .where(WorkflowCopilotChatMessageModel.sender == WorkflowCopilotChatSender.AI)
                        .where(WorkflowCopilotChatMessageModel.turn_outcome.is_not(None))
                    )
                )
                completed_turn_id = _completed_turn_id_for_idempotency_digest(
                    completed_outcomes,
                    pending_turn.idempotency_digest,
                )
                if completed_turn_id is not None:
                    raise DuplicateCopilotTurnError(completed_turn_id)
            new_message = WorkflowCopilotChatMessageModel(
                workflow_copilot_chat_id=workflow_copilot_chat_id,
                organization_id=organization_id,
                sender=sender,
                content=user_message,
                audio_artifact_id=audio_artifact_id,
            )
            session.add(new_message)
            await session.flush()
            pending[pending_turn.turn_id] = pending_turn.model_copy(
                update={"user_message_id": new_message.workflow_copilot_chat_message_id}
            ).model_dump(mode="json")
            chat.pending_turns = pending
            await session.commit()
            await session.refresh(new_message)
            return convert_to_workflow_copilot_chat_message(new_message, self.debug_enabled)

    async def _locked_question_chat(
        self, session: AsyncSession, organization_id: str, chat_id: str
    ) -> WorkflowCopilotChatModel:
        chat = (
            await session.scalars(
                select(WorkflowCopilotChatModel)
                .where(WorkflowCopilotChatModel.organization_id == organization_id)
                .where(WorkflowCopilotChatModel.workflow_copilot_chat_id == chat_id)
                .with_for_update()
            )
        ).first()
        if chat is None:
            raise NotFoundError("Unknown Copilot chat")
        return chat

    @staticmethod
    def _question_in_pending(
        chat: WorkflowCopilotChatModel, interaction_id: str
    ) -> tuple[CopilotPendingTurn, QuestionInteraction]:
        for raw in (chat.pending_turns or {}).values():
            entry = CopilotPendingTurn.model_validate(raw)
            for item in entry.question_interactions:
                if item.interaction_id == interaction_id:
                    return entry, item
        raise NotFoundError("Unknown pending Copilot question")

    @staticmethod
    def _store_question_turn(chat: WorkflowCopilotChatModel, entry: CopilotPendingTurn) -> None:
        chat.pending_turns = {**(chat.pending_turns or {}), entry.turn_id: entry.model_dump(mode="json")}

    @staticmethod
    def _expire_absent_question_client(entry: CopilotPendingTurn, now: datetime) -> bool:
        expired = False
        for item in entry.question_interactions:
            if (
                item.status == "pending"
                and now - (entry.question_client_seen_at or item.created_at) >= QUESTION_CLIENT_GRACE
            ):
                item.status = "interrupted"
                expired = True
        return expired

    @db_operation("refresh_copilot_question_client")
    async def refresh_copilot_question_client(
        self, organization_id: str, chat_id: str
    ) -> dict[str, CopilotPendingTurn]:
        async with self.Session() as session:
            chat = await self._locked_question_chat(session, organization_id, chat_id)
            now = datetime.now(timezone.utc)
            entries = {key: CopilotPendingTurn.model_validate(raw) for key, raw in (chat.pending_turns or {}).items()}
            for entry in entries.values():
                expired = self._expire_absent_question_client(entry, now)
                pending = any(item.status == "pending" for item in entry.question_interactions)
                if pending:
                    entry.question_client_seen_at = now
                if expired or pending:
                    self._store_question_turn(chat, entry)
            await session.commit()
            return entries

    @db_operation("start_copilot_question")
    async def start_copilot_question(
        self, organization_id: str, chat_id: str, interaction: QuestionInteraction
    ) -> None:
        async with self.Session() as session:
            chat = await self._locked_question_chat(session, organization_id, chat_id)
            raw = (chat.pending_turns or {}).get(interaction.turn_id)
            if raw is None:
                raise ValueError("The Copilot execution has ended")
            entry = CopilotPendingTurn.model_validate(raw)
            if any(item.tool_call_id == interaction.tool_call_id for item in entry.question_interactions):
                raise ValueError("This tool call already has a question")
            entry.question_interactions.append(interaction)
            entry.question_heartbeat_at = datetime.now(timezone.utc)
            entry.question_client_seen_at = entry.question_heartbeat_at
            self._store_question_turn(chat, entry)
            await session.commit()

    @db_operation("poll_copilot_question")
    async def poll_copilot_question(
        self, organization_id: str, chat_id: str, interaction_id: str
    ) -> QuestionInteraction:
        async with self.Session() as session:
            chat = await self._locked_question_chat(session, organization_id, chat_id)
            entry, item = self._question_in_pending(chat, interaction_id)
            now = datetime.now(timezone.utc)
            if self._expire_absent_question_client(entry, now):
                self._store_question_turn(chat, entry)
                await session.commit()
                return item
            if item.status == "pending" and (
                entry.question_heartbeat_at is None or now - entry.question_heartbeat_at >= timedelta(seconds=5)
            ):
                entry.question_heartbeat_at = now
                self._store_question_turn(chat, entry)
                await session.commit()
            return item

    @db_operation("resolve_copilot_question")
    async def resolve_copilot_question(
        self,
        organization_id: str,
        chat_id: str,
        interaction_id: str,
        response: QuestionResponse,
        *,
        preflight_only: bool = False,
    ) -> QuestionInteraction:
        """Validate before external screening, then recheck and commit the screened reply."""
        async with self.Session() as session:
            chat = await self._locked_question_chat(session, organization_id, chat_id)
            try:
                entry, item = self._question_in_pending(chat, interaction_id)
            except NotFoundError:
                payloads = await session.scalars(
                    select(WorkflowCopilotChatMessageModel.narrative_payload)
                    .where(WorkflowCopilotChatMessageModel.organization_id == organization_id)
                    .where(WorkflowCopilotChatMessageModel.workflow_copilot_chat_id == chat_id)
                )
                for payload in payloads:
                    for raw in (payload or {}).get("questionInteractions", []):
                        recorded = QuestionInteraction.model_validate(raw)
                        if recorded.interaction_id == interaction_id:
                            if recorded.status != "resolved":
                                raise ValueError("The question is no longer active")
                            return recorded
                raise
            if item.status == "resolved":
                return item
            if self._expire_absent_question_client(entry, datetime.now(timezone.utc)):
                self._store_question_turn(chat, entry)
                await session.commit()
            if item.status != "pending":
                raise ValueError("The question is no longer active")
            if not question_wait_is_live(entry.question_heartbeat_at, datetime.now(timezone.utc)):
                item.status = "interrupted"
                self._store_question_turn(chat, entry)
                await session.commit()
                raise ValueError("The question's execution was interrupted")
            resolved = resolve_question_response(item, response)
            if preflight_only:
                entry.question_client_seen_at = datetime.now(timezone.utc)
                self._store_question_turn(chat, entry)
                await session.commit()
                return item
            entry.question_interactions = [
                resolved if prior.interaction_id == interaction_id else prior for prior in entry.question_interactions
            ]
            self._store_question_turn(chat, entry)
            await session.commit()
            return resolved

    @db_operation("interrupt_copilot_question")
    async def interrupt_copilot_question(
        self, organization_id: str, chat_id: str, interaction_id: str, *, stale_only: bool = False
    ) -> QuestionInteraction | None:
        async with self.Session() as session:
            chat = await self._locked_question_chat(session, organization_id, chat_id)
            try:
                entry, item = self._question_in_pending(chat, interaction_id)
            except NotFoundError:
                return None
            if stale_only and question_wait_is_live(entry.question_heartbeat_at, datetime.now(timezone.utc)):
                return item
            if item.status == "pending":
                item.status = "interrupted"
                self._store_question_turn(chat, entry)
                await session.commit()

            return item

    @db_operation("cancel_copilot_questions")
    async def cancel_copilot_questions(self, organization_id: str, chat_id: str, cancel_token: str) -> bool:
        async with self.Session() as session:
            chat = await self._locked_question_chat(session, organization_id, chat_id)
            changed = False
            for raw in list((chat.pending_turns or {}).values()):
                entry = CopilotPendingTurn.model_validate(raw)
                if entry.cancel_token != cancel_token:
                    continue
                for item in entry.question_interactions:
                    if item.status == "pending":
                        item.status = "cancelled"
                        changed = True
                self._store_question_turn(chat, entry)
            await session.commit()
            return changed

    @db_operation("claim_pending_copilot_turn")
    async def claim_pending_copilot_turn(
        self,
        organization_id: str,
        workflow_copilot_chat_id: str,
        turn_id: str,
        claim_before: datetime,
    ) -> bool:
        """Stamp ``recovering_at`` on one pending turn, returning whether this caller won it.

        A recovery that itself crashes leaves the stamp behind; a later reader
        reclaims it once the stamp predates ``claim_before``.
        """
        async with self.Session() as session:
            chat = (
                await session.scalars(
                    select(WorkflowCopilotChatModel)
                    .where(WorkflowCopilotChatModel.organization_id == organization_id)
                    .where(WorkflowCopilotChatModel.workflow_copilot_chat_id == workflow_copilot_chat_id)
                    .with_for_update()
                )
            ).first()
            if chat is None:
                return False
            pending = dict(chat.pending_turns or {})
            entry = pending.get(turn_id)
            if not isinstance(entry, dict):
                return False
            question_turn = CopilotPendingTurn.model_validate(entry)
            if any(item.status == "pending" for item in question_turn.question_interactions) and question_wait_is_live(
                question_turn.question_heartbeat_at, datetime.now(timezone.utc)
            ):
                return False
            if any(
                item.resolved_at is not None and item.resolved_at > claim_before
                for item in question_turn.question_interactions
            ):
                return False
            recovering_at = _parse_pending_turn_timestamp(entry.get("recovering_at"))
            if recovering_at is not None and recovering_at > claim_before:
                return False
            pending[turn_id] = {**entry, "recovering_at": datetime.now(timezone.utc).isoformat()}
            chat.pending_turns = pending
            await session.commit()
            return True

    @db_operation("clear_pending_copilot_turn")
    async def clear_pending_copilot_turn(
        self,
        organization_id: str,
        workflow_copilot_chat_id: str,
        turn_id: str,
    ) -> None:
        async with self.Session() as session:
            chat = (
                await session.scalars(
                    select(WorkflowCopilotChatModel)
                    .where(WorkflowCopilotChatModel.organization_id == organization_id)
                    .where(WorkflowCopilotChatModel.workflow_copilot_chat_id == workflow_copilot_chat_id)
                    .with_for_update()
                )
            ).first()
            if chat is None:
                return
            pending = dict(chat.pending_turns or {})
            if pending.pop(turn_id, None) is None:
                return
            chat.pending_turns = pending
            await session.commit()

    @db_operation("record_superseded_build_test_run")
    async def record_superseded_build_test_run(
        self,
        organization_id: str,
        workflow_copilot_chat_id: str,
        turn_id: str,
        workflow_run_id: str,
    ) -> None:
        """Record that this turn ended an older build-test run to take the chat's browser.

        Copilot-owned, so the superseded run's own finalizer cannot overwrite it the way it can
        overwrite the reason on the run row.
        """
        async with self.Session() as session:
            chat = (
                await session.scalars(
                    select(WorkflowCopilotChatModel)
                    .where(WorkflowCopilotChatModel.organization_id == organization_id)
                    .where(WorkflowCopilotChatModel.workflow_copilot_chat_id == workflow_copilot_chat_id)
                    .with_for_update()
                )
            ).first()
            if chat is None:
                return
            pending = dict(chat.pending_turns or {})
            entry = pending.get(turn_id)
            if not isinstance(entry, dict):
                return
            recorded = entry.get("superseded_build_test_run_ids")
            recorded = list(recorded) if isinstance(recorded, list) else []
            if workflow_run_id in recorded:
                return
            recorded.append(workflow_run_id)
            pending[turn_id] = {**entry, "superseded_build_test_run_ids": recorded}
            chat.pending_turns = pending
            await session.commit()

    @db_operation("build_test_run_was_superseded")
    async def build_test_run_was_superseded(
        self,
        organization_id: str,
        workflow_copilot_chat_id: str,
        workflow_run_id: str,
    ) -> bool:
        """Whether any turn of this chat recorded ending that build-test run."""
        async with self.Session() as session:
            chat = (
                await session.scalars(
                    select(WorkflowCopilotChatModel)
                    .where(WorkflowCopilotChatModel.organization_id == organization_id)
                    .where(WorkflowCopilotChatModel.workflow_copilot_chat_id == workflow_copilot_chat_id)
                )
            ).first()
            if chat is None:
                return False
            for entry in (chat.pending_turns or {}).values():
                if not isinstance(entry, dict):
                    continue
                recorded = entry.get("superseded_build_test_run_ids")
                if isinstance(recorded, list) and workflow_run_id in recorded:
                    return True
            return False

    @db_operation("record_pending_copilot_turn_canonical_write")
    async def record_pending_copilot_turn_canonical_write(
        self,
        organization_id: str,
        workflow_copilot_chat_id: str,
        turn_id: str,
        fingerprint: str,
    ) -> None:
        """Stamp what this turn left canonical as, so reconcile can tell its own write from anyone else's."""
        async with self.Session() as session:
            chat = (
                await session.scalars(
                    select(WorkflowCopilotChatModel)
                    .where(WorkflowCopilotChatModel.organization_id == organization_id)
                    .where(WorkflowCopilotChatModel.workflow_copilot_chat_id == workflow_copilot_chat_id)
                    .with_for_update()
                )
            ).first()
            if chat is None:
                return
            pending = dict(chat.pending_turns or {})
            entry = pending.get(turn_id)
            if not isinstance(entry, dict):
                return
            pending[turn_id] = {**entry, "canonical_write_fingerprint": fingerprint}
            chat.pending_turns = pending
            await session.commit()

    @db_operation("replace_workflow_copilot_chat_message")
    async def replace_workflow_copilot_chat_message(
        self,
        organization_id: str,
        workflow_copilot_chat_message_id: str,
        content: str,
        global_llm_context: str | None,
        turn_outcome: TurnOutcome | None,
        narrative_payload: TurnNarrativePayload | dict[str, Any] | None,
    ) -> WorkflowCopilotChatMessage | None:
        async with self.Session() as session:
            message = (
                await session.scalars(
                    select(WorkflowCopilotChatMessageModel)
                    .where(WorkflowCopilotChatMessageModel.organization_id == organization_id)
                    .where(
                        WorkflowCopilotChatMessageModel.workflow_copilot_chat_message_id
                        == workflow_copilot_chat_message_id
                    )
                )
            ).first()
            if message is None:
                return None
            message.content = content
            message.global_llm_context = global_llm_context
            message.turn_outcome = turn_outcome.model_dump(mode="json") if turn_outcome else None
            message.narrative_payload = narrative_payload
            await session.commit()
            await session.refresh(message)
            return WorkflowCopilotChatMessage.model_validate(message)

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

    @db_operation("get_workflow_copilot_chats")
    async def get_workflow_copilot_chats(
        self,
        organization_id: str,
        workflow_permanent_id: str | None = None,
        page: int = 1,
        page_size: int = 20,
        search: str | None = None,
    ) -> list[WorkflowCopilotChatSummary]:
        page = max(page, 1)
        page_size = max(min(page_size, 100), 1)
        async with self.Session() as session:
            # Each chat's first message (earliest created_at) is its title source and proves the
            # chat is non-empty; DISTINCT ON keeps one opening row per chat.
            first_message = (
                select(
                    WorkflowCopilotChatMessageModel.workflow_copilot_chat_id.label("chat_id"),
                    WorkflowCopilotChatMessageModel.content.label("title"),
                )
                .where(WorkflowCopilotChatMessageModel.organization_id == organization_id)
                .distinct(WorkflowCopilotChatMessageModel.workflow_copilot_chat_id)
                .order_by(
                    WorkflowCopilotChatMessageModel.workflow_copilot_chat_id,
                    WorkflowCopilotChatMessageModel.created_at.asc(),
                    WorkflowCopilotChatMessageModel.workflow_copilot_chat_message_id.asc(),
                )
                .subquery()
            )
            query = (
                select(
                    WorkflowCopilotChatModel,
                    first_message.c.title,
                    func.jsonb_path_query_array(
                        cast(WorkflowCopilotChatModel.pending_turns, JSONB),
                        cast('$.* ? (@.question_interactions[*].status == "pending").question_heartbeat_at', JSONPATH),
                    ).label("question_heartbeats"),
                )
                .options(defer(WorkflowCopilotChatModel.pending_turns))
                .join(first_message, first_message.c.chat_id == WorkflowCopilotChatModel.workflow_copilot_chat_id)
                .where(WorkflowCopilotChatModel.organization_id == organization_id)
            )
            if workflow_permanent_id is not None:
                query = query.where(WorkflowCopilotChatModel.workflow_permanent_id == workflow_permanent_id)
            if search and search.strip():
                # Unindexed substring match over each chat's opening message (one row per chat);
                # add a pg_trgm index on content if per-org chat counts grow large.
                escaped = escape_like_term(search.strip())
                query = query.where(first_message.c.title.ilike(f"%{escaped}%", escape="\\"))
            query = (
                query.order_by(WorkflowCopilotChatModel.created_at.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
            rows = (await session.execute(query)).all()

            workflow_titles: dict[str, str] = {}
            permanent_ids = {chat.workflow_permanent_id for chat, _, _ in rows}
            if permanent_ids:
                title_rows = (
                    await session.execute(
                        select(WorkflowModel.workflow_permanent_id, WorkflowModel.title)
                        .where(WorkflowModel.organization_id == organization_id)
                        .where(WorkflowModel.workflow_permanent_id.in_(permanent_ids))
                        .where(WorkflowModel.deleted_at.is_(None))
                        .order_by(WorkflowModel.created_at.desc())
                    )
                ).all()
                for permanent_id, title in title_rows:
                    workflow_titles.setdefault(permanent_id, title)

            return [
                WorkflowCopilotChatSummary(
                    workflow_copilot_chat_id=chat.workflow_copilot_chat_id,
                    workflow_permanent_id=chat.workflow_permanent_id,
                    workflow_title=workflow_titles.get(chat.workflow_permanent_id),
                    title=summarize_copilot_chat_title(content),
                    created_at=chat.created_at,
                    modified_at=chat.modified_at,
                    awaiting_user_input=any(
                        question_wait_is_live(_parse_pending_turn_timestamp(heartbeat), datetime.now(timezone.utc))
                        for heartbeat in (question_heartbeats or [])
                    ),
                )
                for chat, content, question_heartbeats in rows
            ]

    @db_operation("get_latest_workflow_copilot_completion_criteria_set")
    async def get_latest_workflow_copilot_completion_criteria_set(
        self,
        organization_id: str,
        workflow_copilot_chat_id: str,
    ) -> WorkflowCopilotCompletionCriteriaSet | NonAdoptableCriteriaSet | None:
        async with self.Session() as session:
            query = (
                select(WorkflowCopilotCompletionCriteriaSetModel)
                .filter(WorkflowCopilotCompletionCriteriaSetModel.organization_id == organization_id)
                .filter(WorkflowCopilotCompletionCriteriaSetModel.workflow_copilot_chat_id == workflow_copilot_chat_id)
                .order_by(
                    WorkflowCopilotCompletionCriteriaSetModel.goal_epoch.desc(),
                    WorkflowCopilotCompletionCriteriaSetModel.created_at.desc(),
                )
                .limit(1)
            )
            row = (await session.scalars(query)).first()
            if not row:
                return None
            return _decode_completion_criteria_set(row)

    @db_operation("create_workflow_copilot_completion_criteria_set")
    async def create_workflow_copilot_completion_criteria_set(
        self,
        organization_id: str,
        workflow_copilot_chat_id: str,
        goal_epoch: int,
        criteria: list[dict[str, Any]],
        source_turn_id: str | None = None,
        source_goal_text: str | None = None,
        consecutive_all_no_evidence: int = 0,
        last_fully_satisfied_workflow_yaml: str | None = None,
    ) -> WorkflowCopilotCompletionCriteriaSet:
        async with self.Session() as session:
            new_set = WorkflowCopilotCompletionCriteriaSetModel(
                organization_id=organization_id,
                workflow_copilot_chat_id=workflow_copilot_chat_id,
                goal_epoch=goal_epoch,
                status="active",
                criteria=criteria,
                source_turn_id=source_turn_id,
                source_goal_text=source_goal_text,
                consecutive_all_no_evidence=consecutive_all_no_evidence,
                tripwire_fired=False,
                last_fully_satisfied_workflow_yaml=last_fully_satisfied_workflow_yaml,
            )
            session.add(new_set)
            await session.commit()
            await session.refresh(new_set)
            return WorkflowCopilotCompletionCriteriaSet.model_validate(new_set)

    @db_operation("supersede_workflow_copilot_completion_criteria_set")
    async def supersede_workflow_copilot_completion_criteria_set(
        self,
        organization_id: str,
        completion_criteria_set_id: str,
        supersede_reason: str,
        superseded_by_set_id: str | None = None,
    ) -> None:
        async with self.Session() as session:
            row = (
                await session.scalars(
                    select(WorkflowCopilotCompletionCriteriaSetModel)
                    .where(WorkflowCopilotCompletionCriteriaSetModel.organization_id == organization_id)
                    .where(
                        WorkflowCopilotCompletionCriteriaSetModel.completion_criteria_set_id
                        == completion_criteria_set_id
                    )
                )
            ).first()
            if not row:
                return
            row.status = "superseded"
            row.supersede_reason = supersede_reason
            row.superseded_by_set_id = superseded_by_set_id
            row.superseded_at = datetime.now(timezone.utc).replace(tzinfo=None)
            if supersede_reason == "tripwire":
                row.tripwire_fired = True
            await session.commit()

    @db_operation("update_workflow_copilot_completion_criteria_set_state")
    async def update_workflow_copilot_completion_criteria_set_state(
        self,
        organization_id: str,
        completion_criteria_set_id: str,
        consecutive_all_no_evidence: int | None = None,
        tripwire_fired: bool | None = None,
        last_fully_satisfied_workflow_yaml: str | None = None,
    ) -> None:
        async with self.Session() as session:
            row = (
                await session.scalars(
                    select(WorkflowCopilotCompletionCriteriaSetModel)
                    .where(WorkflowCopilotCompletionCriteriaSetModel.organization_id == organization_id)
                    .where(
                        WorkflowCopilotCompletionCriteriaSetModel.completion_criteria_set_id
                        == completion_criteria_set_id
                    )
                )
            ).first()
            if not row:
                return
            if consecutive_all_no_evidence is not None:
                row.consecutive_all_no_evidence = consecutive_all_no_evidence
            if tripwire_fired is not None:
                row.tripwire_fired = tripwire_fired
            if last_fully_satisfied_workflow_yaml is not None:
                row.last_fully_satisfied_workflow_yaml = last_fully_satisfied_workflow_yaml
            await session.commit()

    @db_operation("get_task_generation_by_prompt_hash")
    async def get_task_generation_by_prompt_hash(
        self,
        organization_id: str,
        user_prompt_hash: str,
        query_window_hours: int = settings.PROMPT_CACHE_WINDOW_HOURS,
    ) -> TaskGeneration | None:
        before_time = datetime.now(timezone.utc) - timedelta(hours=query_window_hours)
        async with self.Session() as session:
            query = (
                select(TaskGenerationModel)
                .filter_by(organization_id=organization_id, user_prompt_hash=user_prompt_hash)
                .filter(TaskGenerationModel.llm.is_not(None))
                .filter(TaskGenerationModel.created_at > before_time)
                .order_by(TaskGenerationModel.created_at.desc())
                .limit(1)
            )
            task_generation = (await session.scalars(query)).first()
            if not task_generation:
                return None
            return TaskGeneration.model_validate(task_generation)

    @traced(name="skyvern.db.create_action")
    @db_operation("create_action")
    async def create_action(self, action: Action) -> Action:
        async with self.Session() as session:
            raw_action_payload = action.model_dump()
            action_log_payload = redact_action_for_log(action)
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
                response=action_log_payload.get("response"),
                element_id=action.element_id,
                skyvern_element_hash=action.skyvern_element_hash,
                skyvern_element_data=action.skyvern_element_data,
                screenshot_artifact_id=action.screenshot_artifact_id,
                action_json=raw_action_payload,
                confidence_float=action.confidence_float,
                started_at=action.started_at,
                finished_at=action.finished_at,
                created_by=action.created_by,
            )
            session.add(new_action)
            await session.commit()
            await session.refresh(new_action)
            return hydrate_action(new_action)

    @traced(name="skyvern.db.upsert_recorded_action")
    @db_operation("upsert_recorded_action")
    async def upsert_recorded_action(self, action: Action) -> None:
        # Idempotent on action_id: a code block's streamed write (mid-execution, screenshot not yet
        # uploaded) and its end-of-block batch converge on the same row, so the batch backfills the
        # screenshot instead of inserting a duplicate. Isolated from create_action to leave the agent
        # write path untouched.
        action_log_payload = redact_action_for_log(action)
        values = {
            "action_id": action.action_id,
            "action_type": action.action_type,
            "source_action_id": action.source_action_id,
            "organization_id": action.organization_id,
            "workflow_run_id": action.workflow_run_id,
            "task_id": action.task_id,
            "step_id": action.step_id,
            "step_order": action.step_order,
            "action_order": action.action_order,
            "status": action.status,
            "reasoning": action.reasoning,
            "intention": action.intention,
            "response": action_log_payload.get("response"),
            "element_id": action.element_id,
            "skyvern_element_hash": action.skyvern_element_hash,
            "skyvern_element_data": action.skyvern_element_data,
            "screenshot_artifact_id": action.screenshot_artifact_id,
            "action_json": action.model_dump(),
            "confidence_float": action.confidence_float,
            "started_at": action.started_at,
            "finished_at": action.finished_at,
            "created_by": action.created_by,
        }
        async with self.Session() as session:
            stmt = pg_insert(ActionModel).values(**values)
            stmt = stmt.on_conflict_do_update(
                index_elements=["action_id"],
                set_={
                    "screenshot_artifact_id": stmt.excluded.screenshot_artifact_id,
                    "action_json": stmt.excluded.action_json,
                    # coalesce: an upsert writer that carries no execution timestamps must
                    # never overwrite real stamps with NULL.
                    "started_at": func.coalesce(stmt.excluded.started_at, ActionModel.started_at),
                    "finished_at": func.coalesce(stmt.excluded.finished_at, ActionModel.finished_at),
                    "modified_at": stmt.excluded.modified_at,
                },
            )
            await session.execute(stmt)
            await session.commit()

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
            # hydrate_action, not Action.model_validate: the base model has no action_json merge, so
            # validating the row directly drops every subclass field a cached action was recorded
            # with. Matches every other retrieval site.
            return [hydrate_action(action) for action in actions]
