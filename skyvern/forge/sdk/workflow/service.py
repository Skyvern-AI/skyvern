import asyncio
import importlib.util
import json
import os
import textwrap
import uuid
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any, Literal, cast

import httpx
import structlog
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

# Import LockError for specific exception handling; fallback for OSS without redis
try:
    from redis.exceptions import LockError
except ImportError:
    # redis not installed (OSS deployment) - create placeholder that's never raised
    class LockError(Exception):  # type: ignore[no-redef]
        pass


from opentelemetry import trace as otel_trace

import skyvern
from skyvern import analytics
from skyvern.client.types.output_parameter import OutputParameter as BlockOutputParameter
from skyvern.config import settings
from skyvern.constants import GET_DOWNLOADED_FILES_TIMEOUT, SAVE_DOWNLOADED_FILES_TIMEOUT
from skyvern.exceptions import (
    BlockNotFound,
    BrowserProfileNotFound,
    BrowserSessionNotFound,
    FailedToSendWebhook,
    InvalidCredentialId,
    MissingValueForParameter,
    ScriptTerminationException,
    SkyvernException,
    SkyvernHTTPException,
    WorkflowNotFound,
    WorkflowNotFoundForWorkflowRun,
    WorkflowRunNotFound,
    WorkflowRunParameterPersistenceError,
)
from skyvern.forge import app
from skyvern.forge.prompts import prompt_engine
from skyvern.forge.sdk.artifact.models import Artifact, ArtifactType
from skyvern.forge.sdk.cache.factory import CacheFactory
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.security import generate_skyvern_webhook_signature
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.models import Step, StepStatus
from skyvern.forge.sdk.schemas.files import FileInfo
from skyvern.forge.sdk.schemas.organizations import Organization
from skyvern.forge.sdk.schemas.persistent_browser_sessions import PersistentBrowserSession
from skyvern.forge.sdk.schemas.tasks import Task
from skyvern.forge.sdk.schemas.workflow_runs import WorkflowRunBlock, WorkflowRunTimeline, WorkflowRunTimelineType
from skyvern.forge.sdk.trace import traced
from skyvern.forge.sdk.workflow.exceptions import (
    InvalidWorkflowDefinition,
    WorkflowVersionConflict,
)
from skyvern.forge.sdk.workflow.models.block import (
    BlockTypeVar,
    ConditionalBlock,
    ExtractionBlock,
    NavigationBlock,
    TaskV2Block,
    compute_conditional_scopes,
    get_all_blocks,
)
from skyvern.forge.sdk.workflow.models.parameter import (
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
from skyvern.forge.sdk.workflow.models.workflow import (
    Workflow,
    WorkflowDefinition,
    WorkflowRequestBody,
    WorkflowRun,
    WorkflowRunOutputParameter,
    WorkflowRunParameter,
    WorkflowRunResponseBase,
    WorkflowRunStatus,
)
from skyvern.forge.sdk.workflow.workflow_definition_converter import convert_workflow_definition
from skyvern.schemas.runs import (
    ProxyLocationInput,
    RunStatus,
    RunType,
    WorkflowRunRequest,
    WorkflowRunResponse,
)
from skyvern.schemas.scripts import Script, ScriptBlock, ScriptStatus, WorkflowScript
from skyvern.schemas.workflows import (
    BLOCK_YAML_TYPES,
    BlockResult,
    BlockStatus,
    BlockType,
    WorkflowCreateYAMLRequest,
    WorkflowDefinitionYAML,
    WorkflowStatus,
)
from skyvern.services import script_service, workflow_script_service
from skyvern.webeye.browser_state import BrowserState

LOG = structlog.get_logger()

DEFAULT_FIRST_BLOCK_LABEL = "block_1"
DEFAULT_WORKFLOW_TITLE = "New Workflow"

CacheInvalidationReason = Literal["updated_block", "new_block", "removed_block"]
BLOCK_TYPES_THAT_SHOULD_BE_CACHED = {
    BlockType.TASK,
    BlockType.TaskV2,
    BlockType.ACTION,
    BlockType.NAVIGATION,
    BlockType.EXTRACTION,
    BlockType.LOGIN,
    BlockType.FILE_DOWNLOAD,
    BlockType.FOR_LOOP,
}


def _extract_blocks_info(blocks: list[BLOCK_YAML_TYPES]) -> list[dict[str, str]]:
    """Extract lightweight info from blocks for title generation (limit to first 5)."""
    blocks_info: list[dict[str, str]] = []
    for block in blocks[:5]:
        info: dict[str, str] = {"block_type": block.block_type.value}

        # Extract URL if present
        if hasattr(block, "url") and block.url:
            info["url"] = block.url

        # Extract goal/prompt
        goal = None
        if hasattr(block, "navigation_goal") and block.navigation_goal:
            goal = block.navigation_goal
        elif hasattr(block, "data_extraction_goal") and block.data_extraction_goal:
            goal = block.data_extraction_goal
        elif hasattr(block, "prompt") and block.prompt:
            goal = block.prompt

        if goal:
            # Truncate long goals
            info["goal"] = goal[:150] if len(goal) > 150 else goal

        blocks_info.append(info)
    return blocks_info


async def generate_title_from_blocks_info(
    organization_id: str,
    blocks_info: list[dict[str, Any]],
) -> str | None:
    """Call LLM to generate a workflow title from pre-extracted block info."""
    if not blocks_info:
        return None

    try:
        llm_prompt = prompt_engine.load_prompt(
            "generate-workflow-title",
            blocks=blocks_info,
        )

        response = await app.SECONDARY_LLM_API_HANDLER(
            prompt=llm_prompt,
            prompt_name="generate-workflow-title",
            organization_id=organization_id,
        )

        if isinstance(response, dict) and "title" in response:
            title = str(response["title"]).strip()
            if title and len(title) <= 100:  # Sanity check on length
                return title

        return None
    except Exception:
        LOG.exception("Failed to generate workflow title")
        return None


async def generate_workflow_title(
    organization_id: str,
    blocks: list[BLOCK_YAML_TYPES],
) -> str | None:
    """Generate a meaningful workflow title based on block content using LLM."""
    if not blocks:
        return None

    blocks_info = _extract_blocks_info(blocks)
    return await generate_title_from_blocks_info(organization_id, blocks_info)


@dataclass
class CacheInvalidationPlan:
    reason: CacheInvalidationReason | None = None
    label: str | None = None
    previous_index: int | None = None
    new_index: int | None = None
    block_labels_to_disable: list[str] = field(default_factory=list)

    @property
    def has_targets(self) -> bool:
        return bool(self.block_labels_to_disable)


@dataclass
class CachedScriptBlocks:
    workflow_script: WorkflowScript
    script: Script
    blocks_to_clear: list[ScriptBlock]


def _get_workflow_definition_core_data(workflow_definition: WorkflowDefinition) -> dict[str, Any]:
    """
    This function dumps the workflow definition and removes the irrelevant data to the definition, like created_at and modified_at fields inside:
    - list of blocks
    - list of parameters
    And return the dumped workflow definition as a python dictionary.
    """
    # Convert the workflow definition to a dictionary
    workflow_dict = workflow_definition.model_dump()
    fields_to_remove = [
        "created_at",
        "modified_at",
        "deleted_at",
        "output_parameter_id",
        "workflow_id",
        "workflow_parameter_id",
        "aws_secret_parameter_id",
        "bitwarden_login_credential_parameter_id",
        "bitwarden_sensitive_information_parameter_id",
        "bitwarden_credit_card_data_parameter_id",
        "credential_parameter_id",
        "onepassword_credential_parameter_id",
        "azure_vault_credential_parameter_id",
        "disable_cache",
        "next_block_label",
        "version",
        "model",
    ]

    # Use BFS to recursively remove fields from all nested objects

    # Queue to store objects to process
    queue = deque([workflow_dict])

    while queue:
        current_obj = queue.popleft()

        if isinstance(current_obj, dict):
            # Remove specified fields from current dictionary
            for field in fields_to_remove:
                if field:  # Skip empty string
                    current_obj.pop(field, None)

            # Add all nested dictionaries and lists to queue for processing
            for value in current_obj.values():
                if isinstance(value, (dict, list)):
                    queue.append(value)

        elif isinstance(current_obj, list):
            # Add all items in the list to queue for processing
            for item in current_obj:
                if isinstance(item, (dict, list)):
                    queue.append(item)

    return workflow_dict


class WorkflowService:
    @staticmethod
    def _determine_cache_invalidation(
        previous_blocks: list[dict[str, Any]],
        new_blocks: list[dict[str, Any]],
    ) -> CacheInvalidationPlan:
        """Return which block index triggered the change and the labels that need cache invalidation."""
        plan = CacheInvalidationPlan()

        prev_labels: list[str] = []
        for blocks in previous_blocks:
            label = blocks.get("label")
            if label and isinstance(label, str):
                prev_labels.append(label)
        new_labels: list[str] = []
        for blocks in new_blocks:
            label = blocks.get("label")
            if label and isinstance(label, str):
                new_labels.append(label)

        for idx, (prev_block, new_block) in enumerate(zip(previous_blocks, new_blocks)):
            prev_label = prev_block.get("label")
            new_label = new_block.get("label")
            if prev_label and prev_label == new_label and prev_block != new_block:
                plan.reason = "updated_block"
                plan.label = new_label
                plan.previous_index = idx
                break

        if plan.reason is None:
            previous_label_set = set(prev_labels)
            for idx, label in enumerate(new_labels):
                if label and label not in previous_label_set:
                    plan.reason = "new_block"
                    plan.label = label
                    plan.new_index = idx
                    plan.previous_index = min(idx, len(prev_labels))
                    break

        if plan.reason is None:
            new_label_set = set(new_labels)
            for idx, label in enumerate(prev_labels):
                if label not in new_label_set:
                    plan.reason = "removed_block"
                    plan.label = label
                    plan.previous_index = idx
                    break

        if plan.reason == "removed_block":
            new_label_set = set(new_labels)
            plan.block_labels_to_disable = [label for label in prev_labels if label and label not in new_label_set]
        elif plan.previous_index is not None:
            plan.block_labels_to_disable = prev_labels[plan.previous_index :]

        return plan

    async def _partition_cached_blocks(
        self,
        *,
        organization_id: str,
        candidates: Sequence[WorkflowScript],
        block_labels_to_disable: Sequence[str],
    ) -> tuple[list[CachedScriptBlocks], list[CachedScriptBlocks]]:
        """Split cached scripts into published vs draft buckets and collect blocks that should be cleared."""
        cached_groups: list[CachedScriptBlocks] = []
        published_groups: list[CachedScriptBlocks] = []
        target_labels = set(block_labels_to_disable)

        for candidate in candidates:
            script = await app.DATABASE.get_script(
                script_id=candidate.script_id,
                organization_id=organization_id,
            )
            if not script:
                continue

            script_blocks = await app.DATABASE.get_script_blocks_by_script_revision_id(
                script_revision_id=script.script_revision_id,
                organization_id=organization_id,
            )
            blocks_to_clear = [
                block for block in script_blocks if block.script_block_label in target_labels and block.run_signature
            ]
            if not blocks_to_clear:
                continue

            group = CachedScriptBlocks(workflow_script=candidate, script=script, blocks_to_clear=blocks_to_clear)
            if candidate.status == ScriptStatus.published:
                published_groups.append(group)
            else:
                cached_groups.append(group)

        return cached_groups, published_groups

    async def _clear_cached_block_groups(
        self,
        *,
        organization_id: str,
        workflow: Workflow,
        previous_workflow: Workflow,
        plan: CacheInvalidationPlan,
        groups: Sequence[CachedScriptBlocks],
    ) -> None:
        """Remove cached run signatures for the supplied block groups to force regeneration."""
        for group in groups:
            for block in group.blocks_to_clear:
                await app.DATABASE.update_script_block(
                    script_block_id=block.script_block_id,
                    organization_id=organization_id,
                    clear_run_signature=True,
                )

            LOG.info(
                "Cleared cached script blocks after workflow block change",
                workflow_id=workflow.workflow_id,
                workflow_permanent_id=previous_workflow.workflow_permanent_id,
                organization_id=organization_id,
                previous_version=previous_workflow.version,
                new_version=workflow.version,
                invalidate_reason=plan.reason,
                invalidate_label=plan.label,
                invalidate_index_prev=plan.previous_index,
                invalidate_index_new=plan.new_index,
                script_id=group.script.script_id,
                script_revision_id=group.script.script_revision_id,
                cleared_block_labels=[block.script_block_label for block in group.blocks_to_clear],
                cleared_block_count=len(group.blocks_to_clear),
            )

    @staticmethod
    def _collect_extracted_information(value: Any) -> list[Any]:
        """Recursively collect extracted_information values from nested outputs."""
        results: list[Any] = []
        if isinstance(value, dict):
            if "extracted_information" in value and value["extracted_information"] is not None:
                extracted = value["extracted_information"]
                if isinstance(extracted, list):
                    results.extend(extracted)
                else:
                    results.append(extracted)
            else:
                for v in value.values():
                    results.extend(WorkflowService._collect_extracted_information(v))
        elif isinstance(value, list):
            for item in value:
                results.extend(WorkflowService._collect_extracted_information(item))
        return results

    async def _generate_urls_from_artifact_ids(
        self,
        artifact_ids: list[str],
        organization_id: str | None,
    ) -> list[str]:
        """Generate presigned URLs from artifact IDs."""
        if not artifact_ids or not organization_id:
            return []

        artifacts = await app.DATABASE.get_artifacts_by_ids(artifact_ids, organization_id)
        if not artifacts:
            return []

        return await app.ARTIFACT_MANAGER.get_share_links(artifacts) or []

    async def _refresh_output_screenshot_urls(
        self,
        value: Any,
        organization_id: str | None,
        workflow_run_id: str,
    ) -> Any:
        """
        Recursively walk through output values and generate presigned URLs for screenshots.

        TaskOutput dicts stored in workflow_run_output_parameters contain artifact IDs.
        This method finds any TaskOutput-like dicts and generates fresh presigned URLs
        from the stored artifact IDs.

        For backwards compatibility with old data that stored URLs directly (now expired),
        we also check for task_id and regenerate URLs using the task_id lookup.
        """
        if isinstance(value, dict):
            # Check if this looks like a TaskOutput with screenshot artifact IDs (new format)
            has_artifact_ids = "task_screenshot_artifact_ids" in value or "workflow_screenshot_artifact_ids" in value
            # Also check for old format (URLs stored directly) for backwards compat
            has_old_format = "task_id" in value and ("task_screenshots" in value or "workflow_screenshots" in value)

            if has_artifact_ids:
                # New format: generate URLs from artifact IDs
                if value.get("task_screenshot_artifact_ids"):
                    value["task_screenshots"] = await self._generate_urls_from_artifact_ids(
                        value["task_screenshot_artifact_ids"],
                        organization_id,
                    )
                if value.get("workflow_screenshot_artifact_ids"):
                    value["workflow_screenshots"] = await self._generate_urls_from_artifact_ids(
                        value["workflow_screenshot_artifact_ids"],
                        organization_id,
                    )
            elif has_old_format:
                # Old format (backwards compat): regenerate URLs using task_id lookup
                task_id = value.get("task_id")
                if value.get("task_screenshots"):
                    value["task_screenshots"] = await self.get_recent_task_screenshot_urls(
                        organization_id=organization_id,
                        task_id=task_id,
                    )
                if value.get("workflow_screenshots"):
                    value["workflow_screenshots"] = await self.get_recent_workflow_screenshot_urls(
                        workflow_run_id=workflow_run_id,
                        organization_id=organization_id,
                    )
            else:
                # Recurse into nested dicts
                for k, v in value.items():
                    value[k] = await self._refresh_output_screenshot_urls(v, organization_id, workflow_run_id)
        elif isinstance(value, list):
            # Recurse into list items
            for i, item in enumerate(value):
                value[i] = await self._refresh_output_screenshot_urls(item, organization_id, workflow_run_id)
        return value

    async def _validate_credential_id(self, credential_id: str, organization: Organization) -> None:
        credential = await app.DATABASE.get_credential(credential_id, organization_id=organization.organization_id)
        if credential is None:
            raise InvalidCredentialId(credential_id)

    async def setup_workflow_run(
        self,
        request_id: str | None,
        workflow_request: WorkflowRequestBody,
        workflow_permanent_id: str,
        organization: Organization,
        is_template_workflow: bool = False,
        version: int | None = None,
        max_steps_override: int | None = None,
        parent_workflow_run_id: str | None = None,
        debug_session_id: str | None = None,
        code_gen: bool | None = None,
    ) -> WorkflowRun:
        """
        Create a workflow run and its parameters. Validate the workflow and the organization. If there are missing
        parameters with no default value, mark the workflow run as failed.
        :param request_id: The request id for the workflow run.
        :param workflow_request: The request body for the workflow run, containing the parameters and the config.
        :param workflow_id: The workflow id to run.
        :param organization_id: The organization id for the workflow.
        :param max_steps_override: The max steps override for the workflow run, if any.
        :return: The created workflow run.
        """
        # Validate the workflow and the organization
        workflow = await self.get_workflow_by_permanent_id(
            workflow_permanent_id=workflow_permanent_id,
            organization_id=None if is_template_workflow else organization.organization_id,
            version=version,
        )
        if workflow is None:
            LOG.error(f"Workflow {workflow_permanent_id} not found", workflow_version=version)
            raise WorkflowNotFound(workflow_permanent_id=workflow_permanent_id, version=version)
        workflow_id = workflow.workflow_id
        if workflow_request.proxy_location is None and workflow.proxy_location is not None:
            workflow_request.proxy_location = workflow.proxy_location
        if workflow_request.webhook_callback_url is None and workflow.webhook_callback_url is not None:
            workflow_request.webhook_callback_url = workflow.webhook_callback_url

        # Create the workflow run and set skyvern context
        workflow_run = await self.create_workflow_run(
            workflow_request=workflow_request,
            workflow_permanent_id=workflow_permanent_id,
            workflow_id=workflow_id,
            organization_id=organization.organization_id,
            parent_workflow_run_id=parent_workflow_run_id,
            sequential_key=workflow.sequential_key,
            debug_session_id=debug_session_id,
            code_gen=code_gen,
        )
        LOG.info(
            f"Created workflow run {workflow_run.workflow_run_id} for workflow {workflow.workflow_id}",
            request_id=request_id,
            workflow_run_id=workflow_run.workflow_run_id,
            workflow_id=workflow.workflow_id,
            organization_id=workflow.organization_id,
            proxy_location=workflow_request.proxy_location,
            webhook_callback_url=workflow_request.webhook_callback_url,
            max_screenshot_scrolling_times=workflow_request.max_screenshot_scrolls,
            ai_fallback=workflow_request.ai_fallback,
            run_with=workflow_request.run_with,
            code_gen=code_gen,
        )
        context: skyvern_context.SkyvernContext | None = skyvern_context.current()
        current_run_id = context.run_id if context and context.run_id else workflow_run.workflow_run_id
        root_workflow_run_id = (
            context.root_workflow_run_id if context and context.root_workflow_run_id else workflow_run.workflow_run_id
        )
        skyvern_context.set(
            SkyvernContext(
                organization_id=organization.organization_id,
                organization_name=organization.organization_name,
                request_id=request_id,
                workflow_id=workflow_id,
                workflow_run_id=workflow_run.workflow_run_id,
                root_workflow_run_id=root_workflow_run_id,
                run_id=current_run_id,
                workflow_permanent_id=workflow_run.workflow_permanent_id,
                max_steps_override=max_steps_override,
                max_screenshot_scrolls=workflow_request.max_screenshot_scrolls,
            )
        )

        # Create all the workflow run parameters, AWSSecretParameter won't have workflow run parameters created.
        all_workflow_parameters = await self.get_workflow_parameters(workflow_id=workflow.workflow_id)
        try:
            missing_parameters: list[str] = []
            for workflow_parameter in all_workflow_parameters:
                if workflow_request.data and workflow_parameter.key in workflow_request.data:
                    request_body_value = workflow_request.data[workflow_parameter.key]
                    # Fall back to default value if the request explicitly sends null
                    # This supports API clients (e.g., n8n) that include the key with null value
                    if request_body_value is None and workflow_parameter.default_value is not None:
                        request_body_value = workflow_parameter.default_value
                    if self._is_missing_required_value(workflow_parameter, request_body_value):
                        missing_parameters.append(workflow_parameter.key)
                        continue
                    if workflow_parameter.workflow_parameter_type == WorkflowParameterType.CREDENTIAL_ID:
                        await self._validate_credential_id(str(request_body_value), organization)
                    try:
                        await self.create_workflow_run_parameter(
                            workflow_run_id=workflow_run.workflow_run_id,
                            workflow_parameter=workflow_parameter,
                            value=request_body_value,
                        )
                    except SQLAlchemyError as parameter_error:
                        raise WorkflowRunParameterPersistenceError(
                            parameter_key=workflow_parameter.key,
                            workflow_id=workflow.workflow_permanent_id,
                            workflow_run_id=workflow_run.workflow_run_id,
                            reason=self._format_parameter_persistence_error(parameter_error),
                        ) from parameter_error
                elif workflow_parameter.default_value is not None:
                    if workflow_parameter.workflow_parameter_type == WorkflowParameterType.CREDENTIAL_ID:
                        await self._validate_credential_id(str(workflow_parameter.default_value), organization)
                    try:
                        await self.create_workflow_run_parameter(
                            workflow_run_id=workflow_run.workflow_run_id,
                            workflow_parameter=workflow_parameter,
                            value=workflow_parameter.default_value,
                        )
                    except SQLAlchemyError as parameter_error:
                        raise WorkflowRunParameterPersistenceError(
                            parameter_key=workflow_parameter.key,
                            workflow_id=workflow.workflow_permanent_id,
                            workflow_run_id=workflow_run.workflow_run_id,
                            reason=self._format_parameter_persistence_error(parameter_error),
                        ) from parameter_error
                else:
                    missing_parameters.append(workflow_parameter.key)

            if missing_parameters:
                missing_list = ", ".join(sorted(missing_parameters))
                raise MissingValueForParameter(
                    parameter_key=missing_list,
                    workflow_id=workflow.workflow_permanent_id,
                    workflow_run_id=workflow_run.workflow_run_id,
                )
        except Exception as e:
            LOG.exception(
                f"Error while setting up workflow run {workflow_run.workflow_run_id}",
                workflow_run_id=workflow_run.workflow_run_id,
            )

            failure_reason = f"Setup workflow failed due to an unexpected exception: {str(e)}"
            if isinstance(e, SkyvernException):
                failure_reason = f"Setup workflow failed due to an SkyvernException({e.__class__.__name__}): {str(e)}"

            workflow_run = await self.mark_workflow_run_as_failed(
                workflow_run_id=workflow_run.workflow_run_id, failure_reason=failure_reason
            )
            raise e

        return workflow_run

    @staticmethod
    def _format_parameter_persistence_error(error: SQLAlchemyError) -> str:
        if isinstance(error, IntegrityError):
            return "value cannot be null"
        return "database error while saving parameter value"

    @staticmethod
    def _is_missing_required_value(workflow_parameter: WorkflowParameter, value: Any) -> bool:
        """
        Determine if a provided value should be treated as missing for a required parameter.

        Rules:
        - None/null is always missing.
        - String parameters may be empty strings (per UI behavior).
        - JSON parameters treat empty/whitespace-only strings as missing.
        - Boolean/integer/float parameters treat empty strings as missing.
        - File URL treats empty strings, empty dicts, or dicts with empty s3uri as missing.
        - Credential ID treats empty/whitespace-only strings as missing.
        """

        if value is None:
            return True

        param_type = workflow_parameter.workflow_parameter_type

        if param_type == WorkflowParameterType.STRING:
            return False

        if param_type == WorkflowParameterType.JSON:
            return isinstance(value, str) and value.strip() == ""

        if param_type in (
            WorkflowParameterType.BOOLEAN,
            WorkflowParameterType.INTEGER,
            WorkflowParameterType.FLOAT,
        ):
            return isinstance(value, str) and value.strip() == ""

        if param_type == WorkflowParameterType.FILE_URL:
            if isinstance(value, str):
                return value.strip() == ""
            if isinstance(value, dict):
                if not value:
                    return True
                if "s3uri" in value:
                    return not bool(value.get("s3uri"))
            return False

        if param_type == WorkflowParameterType.CREDENTIAL_ID:
            return isinstance(value, str) and value.strip() == ""

        return False

    async def auto_create_browser_session_if_needed(
        self,
        organization_id: str,
        workflow: Workflow,
        *,
        browser_session_id: str | None = None,
        proxy_location: ProxyLocationInput = None,
    ) -> PersistentBrowserSession | None:
        if browser_session_id:  # the user has supplied an id, so no need to create one
            return None

        workflow_definition = workflow.workflow_definition
        blocks = workflow_definition.blocks
        human_interaction_blocks = [block for block in blocks if block.block_type == BlockType.HUMAN_INTERACTION]

        if human_interaction_blocks:
            timeouts = [getattr(block, "timeout_seconds", 60 * 60) for block in human_interaction_blocks]
            timeout_seconds = sum(timeouts) + 60 * 60

            browser_session = await app.PERSISTENT_SESSIONS_MANAGER.create_session(
                organization_id=organization_id,
                timeout_minutes=timeout_seconds // 60,
                proxy_location=proxy_location,
            )

            return browser_session

        return None

    @traced()
    async def execute_workflow(
        self,
        workflow_run_id: str,
        api_key: str,
        organization: Organization,
        block_labels: list[str] | None = None,
        block_outputs: dict[str, Any] | None = None,
        browser_session_id: str | None = None,
    ) -> WorkflowRun:
        """Execute a workflow."""
        organization_id = organization.organization_id

        LOG.info(
            "Executing workflow",
            workflow_run_id=workflow_run_id,
            organization_id=organization_id,
            browser_session_id=browser_session_id,
            block_labels=block_labels,
            block_outputs=block_outputs,
        )
        workflow_run = await self.get_workflow_run(workflow_run_id=workflow_run_id, organization_id=organization_id)
        workflow = await self.get_workflow_by_permanent_id(workflow_permanent_id=workflow_run.workflow_permanent_id)
        has_conditionals = workflow_script_service.workflow_has_conditionals(workflow)
        browser_profile_id = workflow_run.browser_profile_id
        close_browser_on_completion = browser_session_id is None and not workflow_run.browser_address

        # Set workflow run status to running, create workflow run parameters
        workflow_run = await self.mark_workflow_run_as_running(workflow_run_id=workflow_run_id)

        # Get all context parameters from the workflow definition
        context_parameters = [
            parameter
            for parameter in workflow.workflow_definition.parameters
            if isinstance(parameter, ContextParameter)
        ]

        secret_parameters = [
            parameter
            for parameter in workflow.workflow_definition.parameters
            if isinstance(
                parameter,
                (
                    AWSSecretParameter,
                    BitwardenLoginCredentialParameter,
                    BitwardenCreditCardDataParameter,
                    BitwardenSensitiveInformationParameter,
                    OnePasswordCredentialParameter,
                    AzureVaultCredentialParameter,
                    CredentialParameter,
                ),
            )
        ]

        # Get all <workflow parameter, workflow run parameter> tuples
        wp_wps_tuples = await self.get_workflow_run_parameter_tuples(workflow_run_id=workflow_run_id)
        workflow_output_parameters = await self.get_workflow_output_parameters(workflow_id=workflow.workflow_id)
        try:
            await app.WORKFLOW_CONTEXT_MANAGER.initialize_workflow_run_context(
                organization,
                workflow_run_id,
                workflow.title,
                workflow.workflow_id,
                workflow.workflow_permanent_id,
                wp_wps_tuples,
                workflow_output_parameters,
                context_parameters,
                secret_parameters,
                block_outputs,
                workflow,
            )
        except Exception as e:
            LOG.exception(
                f"Error while initializing workflow run context for workflow run {workflow_run_id}",
                workflow_run_id=workflow_run_id,
            )

            exception_message = f"Unexpected error: {str(e)}"
            if isinstance(e, SkyvernException):
                exception_message = f"unexpected SkyvernException({e.__class__.__name__}): {str(e)}"

            failure_reason = f"Failed to initialize workflow run context. failure reason: {exception_message}"
            workflow_run = await self.mark_workflow_run_as_failed(
                workflow_run_id=workflow_run_id, failure_reason=failure_reason
            )
            await self.clean_up_workflow(
                workflow=workflow,
                workflow_run=workflow_run,
                api_key=api_key,
                browser_session_id=browser_session_id,
                close_browser_on_completion=close_browser_on_completion,
            )
            return workflow_run

        browser_session = None
        if not browser_profile_id:
            browser_session = await self.auto_create_browser_session_if_needed(
                organization.organization_id,
                workflow,
                browser_session_id=browser_session_id,
                proxy_location=workflow_run.proxy_location,
            )

        if browser_session:
            browser_session_id = browser_session.persistent_browser_session_id
            close_browser_on_completion = True
            await app.DATABASE.update_workflow_run(
                workflow_run_id=workflow_run.workflow_run_id,
                browser_session_id=browser_session_id,
            )

        if browser_session_id:
            try:
                await app.PERSISTENT_SESSIONS_MANAGER.begin_session(
                    browser_session_id=browser_session_id,
                    runnable_type="workflow_run",
                    runnable_id=workflow_run_id,
                    organization_id=organization.organization_id,
                )
            except Exception as e:
                LOG.exception(
                    "Failed to begin browser session for workflow run",
                    browser_session_id=browser_session_id,
                    workflow_run_id=workflow_run_id,
                )
                failure_reason = f"Failed to begin browser session for workflow run: {str(e)}"
                workflow_run = await self.mark_workflow_run_as_failed(
                    workflow_run_id=workflow_run_id,
                    failure_reason=failure_reason,
                )
                await self.clean_up_workflow(
                    workflow=workflow,
                    workflow_run=workflow_run,
                    api_key=api_key,
                    browser_session_id=browser_session_id,
                    close_browser_on_completion=close_browser_on_completion,
                )
                return workflow_run

        # Check if there's a related workflow script that should be used instead
        workflow_script, _ = await workflow_script_service.get_workflow_script(workflow, workflow_run, block_labels)
        current_context = skyvern_context.current()
        if current_context:
            if workflow_script:
                current_context.generate_script = False
            if workflow_run.code_gen:
                current_context.generate_script = True
        workflow_run, blocks_to_update = await self._execute_workflow_blocks(
            workflow=workflow,
            workflow_run=workflow_run,
            organization=organization,
            browser_session_id=browser_session_id,
            browser_profile_id=browser_profile_id,
            block_labels=block_labels,
            block_outputs=block_outputs,
            script=workflow_script,
        )

        # Check if there's a finally block configured
        finally_block_label = workflow.workflow_definition.finally_block_label

        if refreshed_workflow_run := await app.DATABASE.get_workflow_run(
            workflow_run_id=workflow_run_id,
            organization_id=organization_id,
        ):
            workflow_run = refreshed_workflow_run

        pre_finally_status = workflow_run.status
        pre_finally_failure_reason = workflow_run.failure_reason

        if pre_finally_status not in (
            WorkflowRunStatus.canceled,
            WorkflowRunStatus.failed,
            WorkflowRunStatus.terminated,
            WorkflowRunStatus.timed_out,
        ):
            await self.generate_script_if_needed(
                workflow=workflow,
                workflow_run=workflow_run,
                block_labels=block_labels,
                blocks_to_update=blocks_to_update,
                finalize=True,  # Force regeneration to ensure field mappings have complete action data
                has_conditionals=has_conditionals,
            )

        # Execute finally block if configured. Skip for: canceled (user explicitly stopped)
        should_run_finally = finally_block_label and pre_finally_status != WorkflowRunStatus.canceled
        if should_run_finally:
            # Temporarily set to running for terminal workflows (for frontend UX)
            if pre_finally_status in (
                WorkflowRunStatus.failed,
                WorkflowRunStatus.terminated,
                WorkflowRunStatus.timed_out,
            ):
                workflow_run = await self._update_workflow_run_status(
                    workflow_run_id=workflow_run_id,
                    status=WorkflowRunStatus.running,
                    failure_reason=None,
                )
            await self._execute_finally_block_if_configured(
                workflow=workflow,
                workflow_run=workflow_run,
                organization=organization,
                browser_session_id=browser_session_id,
            )

        workflow_run = await self._finalize_workflow_run_status(
            workflow_run_id=workflow_run_id,
            workflow_run=workflow_run,
            pre_finally_status=pre_finally_status,
            pre_finally_failure_reason=pre_finally_failure_reason,
        )

        await self.clean_up_workflow(
            workflow=workflow,
            workflow_run=workflow_run,
            api_key=api_key,
            browser_session_id=browser_session_id,
            close_browser_on_completion=close_browser_on_completion,
        )

        return workflow_run

    async def _execute_workflow_blocks(
        self,
        workflow: Workflow,
        workflow_run: WorkflowRun,
        organization: Organization,
        browser_session_id: str | None = None,
        browser_profile_id: str | None = None,
        block_labels: list[str] | None = None,
        block_outputs: dict[str, Any] | None = None,
        script: Script | None = None,
    ) -> tuple[WorkflowRun, set[str]]:
        organization_id = organization.organization_id
        workflow_run_id = workflow_run.workflow_run_id
        top_level_blocks = workflow.workflow_definition.blocks
        all_blocks = get_all_blocks(top_level_blocks)

        # Load script blocks if script is provided
        script_blocks_by_label: dict[str, Any] = {}
        loaded_script_module = None
        blocks_to_update: set[str] = set()

        is_script_run = self.should_run_script(workflow, workflow_run)

        if script:
            LOG.info(
                "Loading script blocks for workflow execution",
                workflow_run_id=workflow_run_id,
                script_id=script.script_id,
                script_revision_id=script.script_revision_id,
            )
            context = skyvern_context.ensure_context()
            context.script_id = script.script_id
            context.script_revision_id = script.script_revision_id
            try:
                script_blocks = await app.DATABASE.get_script_blocks_by_script_revision_id(
                    script_revision_id=script.script_revision_id,
                    organization_id=organization_id,
                )

                # Create mapping from block label to script block
                for script_block in script_blocks:
                    if script_block.run_signature:
                        script_blocks_by_label[script_block.script_block_label] = script_block

                if is_script_run:
                    # load the script files
                    script_files = await app.DATABASE.get_script_files(
                        script_revision_id=script.script_revision_id,
                        organization_id=organization_id,
                    )
                    await script_service.load_scripts(script, script_files)

                    script_path = os.path.join(settings.TEMP_PATH, script.script_id, "main.py")
                    if os.path.exists(script_path):
                        # setup script run
                        parameter_tuples = await app.DATABASE.get_workflow_run_parameters(
                            workflow_run_id=workflow_run.workflow_run_id
                        )
                        script_parameters = {wf_param.key: run_param.value for wf_param, run_param in parameter_tuples}

                        spec = importlib.util.spec_from_file_location("user_script", script_path)
                        if spec and spec.loader:
                            loaded_script_module = importlib.util.module_from_spec(spec)
                            spec.loader.exec_module(loaded_script_module)
                            await skyvern.setup(
                                script_parameters,
                                generated_parameter_cls=loaded_script_module.GeneratedWorkflowParameters,
                            )
                            LOG.info(
                                "Successfully loaded script module",
                                script_id=script.script_id,
                                block_count=len(script_blocks_by_label),
                            )
                    else:
                        LOG.warning(
                            "Script file not found at path",
                            script_path=script_path,
                            script_id=script.script_id,
                        )
            except Exception as e:
                LOG.warning(
                    "Failed to load script blocks, will fallback to normal execution",
                    error=str(e),
                    exc_info=True,
                    workflow_run_id=workflow_run_id,
                    script_id=script.script_id,
                )
                script_blocks_by_label = {}
                loaded_script_module = None

        # Mark workflow as running with appropriate engine
        run_with = "code" if script and is_script_run and script_blocks_by_label else "agent"
        await self.mark_workflow_run_as_running(workflow_run_id=workflow_run_id, run_with=run_with)

        if block_labels and len(block_labels):
            blocks: list[BlockTypeVar] = []
            all_labels = {block.label: block for block in all_blocks}
            for label in block_labels:
                if label not in all_labels:
                    raise BlockNotFound(block_label=label)

                blocks.append(all_labels[label])

            LOG.info(
                "Executing workflow blocks via whitelist",
                workflow_run_id=workflow_run_id,
                block_cnt=len(blocks),
                block_labels=block_labels,
                block_outputs=block_outputs,
            )

        else:
            blocks = top_level_blocks
            # Exclude the finally block from normal traversal — it runs separately via _execute_finally_block_if_configured
            finally_block_label = workflow.workflow_definition.finally_block_label
            if finally_block_label:
                blocks = self._strip_finally_block_references(blocks, finally_block_label)

        if not blocks:
            raise SkyvernException(f"No blocks found for the given block labels: {block_labels}")

        workflow_version = workflow.workflow_definition.version or 1
        if workflow_version >= 2 and not block_labels:
            return await self._execute_workflow_blocks_dag(
                workflow=workflow,
                workflow_run=workflow_run,
                organization=organization,
                browser_session_id=browser_session_id,
                script_blocks_by_label=script_blocks_by_label,
                loaded_script_module=loaded_script_module,
                is_script_run=is_script_run,
                blocks_to_update=blocks_to_update,
            )

        #
        # Execute workflow blocks
        blocks_cnt = len(blocks)
        block_result = None
        for block_idx, block in enumerate(blocks):
            (
                workflow_run,
                blocks_to_update,
                block_result,
                should_stop,
                _,
            ) = await self._execute_single_block(
                workflow=workflow,
                block=block,
                block_idx=block_idx,
                blocks_cnt=blocks_cnt,
                workflow_run=workflow_run,
                organization=organization,
                workflow_run_id=workflow_run_id,
                browser_session_id=browser_session_id,
                script_blocks_by_label=script_blocks_by_label,
                loaded_script_module=loaded_script_module,
                is_script_run=is_script_run,
                blocks_to_update=blocks_to_update,
            )

            if should_stop:
                break
        return workflow_run, blocks_to_update

    async def _generate_pending_script_for_block(
        self,
        workflow: Workflow,
        workflow_run: WorkflowRun,
        block_result: BlockResult | None,
    ) -> None:
        """Generate pending script after a block completes successfully.

        This is called after each block execution instead of after each action,
        reducing script generation frequency while maintaining progressive updates.
        Uses asyncio.create_task() to avoid adding latency between blocks.
        """
        if not block_result or block_result.status != BlockStatus.completed:
            return

        context = skyvern_context.current()
        if not context or not context.generate_script:
            return

        disable_script_generation = await app.EXPERIMENTATION_PROVIDER.is_feature_enabled_cached(
            "DISABLE_GENERATE_SCRIPT_AFTER_BLOCK",
            workflow_run.workflow_run_id,
            properties={"organization_id": workflow_run.organization_id},
        )
        if disable_script_generation:
            return

        asyncio.create_task(
            self._do_generate_pending_script(workflow, workflow_run),
            name=f"script_gen_{workflow_run.workflow_run_id}",
        )

    async def _do_generate_pending_script(
        self,
        workflow: Workflow,
        workflow_run: WorkflowRun,
    ) -> None:
        """Fire-and-forget wrapper for pending script generation with error handling."""
        try:
            await workflow_script_service.generate_or_update_pending_workflow_script(
                workflow_run=workflow_run,
                workflow=workflow,
            )
        except Exception:
            LOG.warning(
                "Failed to generate pending script after block completion",
                workflow_run_id=workflow_run.workflow_run_id,
                exc_info=True,
            )

    async def _execute_workflow_blocks_dag(
        self,
        *,
        workflow: Workflow,
        workflow_run: WorkflowRun,
        organization: Organization,
        browser_session_id: str | None,
        script_blocks_by_label: dict[str, Any],
        loaded_script_module: Any,
        is_script_run: bool,
        blocks_to_update: set[str],
    ) -> tuple[WorkflowRun, set[str]]:
        finally_block_label = workflow.workflow_definition.finally_block_label
        dag_blocks = workflow.workflow_definition.blocks
        if finally_block_label:
            dag_blocks = self._strip_finally_block_references(dag_blocks, finally_block_label)

        try:
            start_label, label_to_block, default_next_map = self._build_workflow_graph(dag_blocks)
        except InvalidWorkflowDefinition as exc:
            LOG.error("Workflow graph validation failed", error=str(exc), workflow_id=workflow.workflow_id)
            workflow_run = await self.mark_workflow_run_as_failed(
                workflow_run_id=workflow_run.workflow_run_id,
                failure_reason=str(exc),
            )
            return workflow_run, blocks_to_update

        conditional_scopes = compute_conditional_scopes(label_to_block, default_next_map)
        conditional_wrb_ids: dict[str, str] = {}

        visited_labels: set[str] = set()
        current_label = start_label
        block_idx = 0
        total_blocks = len(label_to_block)

        while current_label:
            block = label_to_block.get(current_label)
            if not block:
                LOG.error(
                    "Unable to find block with label in workflow graph",
                    workflow_run_id=workflow_run.workflow_run_id,
                    current_label=current_label,
                )
                workflow_run = await self.mark_workflow_run_as_failed(
                    workflow_run_id=workflow_run.workflow_run_id,
                    failure_reason=f"Unable to find block with label {current_label}",
                )
                break

            # Determine the parent for timeline nesting: if this block is
            # inside a conditional's scope, parent it to that conditional's
            # workflow_run_block rather than the root.
            parent_wrb_id: str | None = None
            if current_label in conditional_scopes:
                cond_label = conditional_scopes[current_label]
                if cond_label in conditional_wrb_ids:
                    parent_wrb_id = conditional_wrb_ids[cond_label]

            (
                workflow_run,
                blocks_to_update,
                block_result,
                should_stop,
                branch_metadata,
            ) = await self._execute_single_block(
                workflow=workflow,
                block=block,
                block_idx=block_idx,
                blocks_cnt=total_blocks,
                workflow_run=workflow_run,
                organization=organization,
                workflow_run_id=workflow_run.workflow_run_id,
                browser_session_id=browser_session_id,
                script_blocks_by_label=script_blocks_by_label,
                loaded_script_module=loaded_script_module,
                is_script_run=is_script_run,
                blocks_to_update=blocks_to_update,
                parent_workflow_run_block_id=parent_wrb_id,
            )

            # Track conditional workflow_run_block_ids so branch targets
            # can be parented to them.
            if block.block_type == BlockType.CONDITIONAL and block_result and block_result.workflow_run_block_id:
                conditional_wrb_ids[block.label] = block_result.workflow_run_block_id

            visited_labels.add(current_label)
            if should_stop:
                break

            next_label = None
            if block.block_type == BlockType.CONDITIONAL:
                next_label = (branch_metadata or {}).get("next_block_label")
            else:
                next_label = default_next_map.get(block.label)

            if not next_label:
                LOG.info(
                    "DAG traversal reached terminal node",
                    workflow_run_id=workflow_run.workflow_run_id,
                    block_label=block.label,
                )
                break

            if next_label not in label_to_block:
                workflow_run = await self.mark_workflow_run_as_failed(
                    workflow_run_id=workflow_run.workflow_run_id,
                    failure_reason=f"Next block label {next_label} not found in workflow definition",
                )
                break

            if next_label in visited_labels:
                workflow_run = await self.mark_workflow_run_as_failed(
                    workflow_run_id=workflow_run.workflow_run_id,
                    failure_reason=f"Cycle detected while traversing workflow definition at block {next_label}",
                )
                break

            block_idx += 1
            current_label = next_label

        return workflow_run, blocks_to_update

    async def _execute_single_block(
        self,
        *,
        workflow: Workflow,
        block: BlockTypeVar,
        block_idx: int,
        blocks_cnt: int,
        workflow_run: WorkflowRun,
        organization: Organization,
        workflow_run_id: str,
        browser_session_id: str | None,
        script_blocks_by_label: dict[str, Any],
        loaded_script_module: Any,
        is_script_run: bool,
        blocks_to_update: set[str],
        parent_workflow_run_block_id: str | None = None,
    ) -> tuple[WorkflowRun, set[str], BlockResult | None, bool, dict[str, Any] | None]:
        organization_id = organization.organization_id
        workflow_run_block_result: BlockResult | None = None
        branch_metadata: dict[str, Any] | None = None
        block_executed_with_code = False

        try:
            if refreshed_workflow_run := await app.DATABASE.get_workflow_run(
                workflow_run_id=workflow_run_id,
                organization_id=organization_id,
            ):
                workflow_run = refreshed_workflow_run
                if workflow_run.status == WorkflowRunStatus.canceled:
                    LOG.info(
                        "Workflow run is canceled, stopping execution inside workflow execution loop",
                        workflow_run_id=workflow_run_id,
                        block_idx=block_idx,
                        block_type=block.block_type,
                        block_label=block.label,
                    )
                    return workflow_run, blocks_to_update, workflow_run_block_result, True, branch_metadata

                if workflow_run.status == WorkflowRunStatus.timed_out:
                    LOG.info(
                        "Workflow run is timed out, stopping execution inside workflow execution loop",
                        workflow_run_id=workflow_run_id,
                        block_idx=block_idx,
                        block_type=block.block_type,
                        block_label=block.label,
                    )
                    return workflow_run, blocks_to_update, workflow_run_block_result, True, branch_metadata

            parameters = block.get_all_parameters(workflow_run_id)
            await app.WORKFLOW_CONTEXT_MANAGER.register_block_parameters_for_workflow_run(
                workflow_run_id, parameters, organization
            )
            LOG.info(
                f"Executing root block {block.block_type} at index {block_idx}/{blocks_cnt - 1} for workflow run {workflow_run_id}",
                block_type=block.block_type,
                workflow_run_id=workflow_run_id,
                block_idx=block_idx,
                block_type_var=block.block_type,
                block_label=block.label,
                model=block.model,
            )

            valid_to_run_code = (
                is_script_run and block.label and block.label in script_blocks_by_label and not block.disable_cache
            )
            if valid_to_run_code:
                script_block = script_blocks_by_label[block.label]
                LOG.info(
                    "Attempting to execute block with script code",
                    block_label=block.label,
                    run_signature=script_block.run_signature,
                )
                try:
                    vars_dict = vars(loaded_script_module) if loaded_script_module else {}
                    exec_globals = {
                        **vars_dict,
                        "skyvern": skyvern,
                        "__builtins__": __builtins__,
                    }

                    assert script_block.run_signature is not None
                    normalized_signature = textwrap.dedent(script_block.run_signature).strip()
                    indented_signature = textwrap.indent(normalized_signature, "        ")
                    wrapper_code = f"async def __run_signature_wrapper():\n    return (\n{indented_signature}\n    )\n"

                    LOG.debug("Executing run_signature wrapper", wrapper_code=wrapper_code)

                    try:
                        exec_code = compile(wrapper_code, "<run_signature>", "exec")
                        exec(exec_code, exec_globals)
                        output_value = await exec_globals["__run_signature_wrapper"]()
                    except ScriptTerminationException as e:
                        LOG.warning(
                            "Script termination",
                            block_label=block.label,
                            error=str(e),
                            exc_info=True,
                        )

                    workflow_run_blocks = await app.DATABASE.get_workflow_run_blocks(
                        workflow_run_id=workflow_run_id,
                        organization_id=organization_id,
                    )
                    matching_blocks = [b for b in workflow_run_blocks if b.label == block.label]
                    if matching_blocks:
                        latest_block = max(matching_blocks, key=lambda b: b.created_at)
                        workflow_run_block_result = BlockResult(
                            success=latest_block.status == BlockStatus.completed,
                            failure_reason=latest_block.failure_reason,
                            output_parameter=block.output_parameter,
                            output_parameter_value=latest_block.output,
                            status=BlockStatus(latest_block.status) if latest_block.status else BlockStatus.failed,
                            workflow_run_block_id=latest_block.workflow_run_block_id,
                        )
                        block_executed_with_code = True
                        LOG.info(
                            "Successfully executed block with script code",
                            block_label=block.label,
                            block_status=workflow_run_block_result.status,
                            has_output=output_value is not None,
                        )
                    else:
                        LOG.warning(
                            "Block executed with code but no workflow run block found",
                            block_label=block.label,
                        )
                        block_executed_with_code = False
                except Exception as e:
                    LOG.warning(
                        "Failed to execute block with script code, falling back to AI",
                        block_label=block.label,
                        error=str(e),
                        exc_info=True,
                    )
                    block_executed_with_code = False

            if not block_executed_with_code:
                LOG.info(
                    "Executing block",
                    block_label=block.label,
                    block_type=block.block_type,
                )
                workflow_run_block_result = await block.execute_safe(
                    workflow_run_id=workflow_run_id,
                    parent_workflow_run_block_id=parent_workflow_run_block_id,
                    organization_id=organization_id,
                    browser_session_id=browser_session_id,
                )

            # Extract branch metadata for conditional blocks
            if isinstance(block, ConditionalBlock) and workflow_run_block_result:
                branch_metadata = cast(dict[str, Any] | None, workflow_run_block_result.output_parameter_value)

            if not workflow_run_block_result:
                workflow_run = await self.mark_workflow_run_as_failed(
                    workflow_run_id=workflow_run_id, failure_reason="Block result is None"
                )
                return workflow_run, blocks_to_update, workflow_run_block_result, True, branch_metadata

            if (
                not block_executed_with_code
                and block.label
                and block.label not in script_blocks_by_label
                and workflow_run_block_result.status == BlockStatus.completed
                and block.block_type in BLOCK_TYPES_THAT_SHOULD_BE_CACHED
            ):
                blocks_to_update.add(block.label)

            # Invalidate cache for blocks with continue_on_failure=True that failed
            # This ensures the block runs fresh with AI on the next cached run
            if (
                block.label
                and block.continue_on_failure
                and workflow_run_block_result.status != BlockStatus.completed
                and block.block_type in BLOCK_TYPES_THAT_SHOULD_BE_CACHED
                and block.label in script_blocks_by_label
            ):
                blocks_to_update.add(block.label)
                LOG.info(
                    "Block with continue_on_failure failed during cached execution, marking for regeneration",
                    block_label=block.label,
                    block_status=workflow_run_block_result.status,
                    workflow_run_id=workflow_run_id,
                )

            workflow_run, should_stop = await self._handle_block_result_status(
                block=block,
                block_idx=block_idx,
                blocks_cnt=blocks_cnt,
                block_result=workflow_run_block_result,
                workflow_run=workflow_run,
                workflow_run_id=workflow_run_id,
            )

            # Generate pending script after block completes successfully
            await self._generate_pending_script_for_block(workflow, workflow_run, workflow_run_block_result)

            return workflow_run, blocks_to_update, workflow_run_block_result, should_stop, branch_metadata

        except Exception as e:
            LOG.exception(
                f"Error while executing workflow run {workflow_run_id}",
                workflow_run_id=workflow_run_id,
                block_idx=block_idx,
                block_type=block.block_type,
                block_label=block.label,
            )

            exception_message = f"Unexpected error: {str(e)}"
            if isinstance(e, SkyvernException):
                exception_message = f"unexpected SkyvernException({e.__class__.__name__}): {str(e)}"

            failure_reason = f"{block.block_type} block failed. failure reason: {exception_message}"
            workflow_run = await self.mark_workflow_run_as_failed(
                workflow_run_id=workflow_run_id, failure_reason=failure_reason
            )
            return workflow_run, blocks_to_update, workflow_run_block_result, True, branch_metadata

    async def _handle_block_result_status(
        self,
        *,
        block: BlockTypeVar,
        block_idx: int,
        blocks_cnt: int,
        block_result: BlockResult,
        workflow_run: WorkflowRun,
        workflow_run_id: str,
    ) -> tuple[WorkflowRun, bool]:
        if block_result.status == BlockStatus.canceled:
            LOG.info(
                f"Block with type {block.block_type} at index {block_idx}/{blocks_cnt - 1} was canceled for workflow run {workflow_run_id}, cancelling workflow run",
                block_type=block.block_type,
                workflow_run_id=workflow_run_id,
                block_idx=block_idx,
                block_result=block_result,
                block_type_var=block.block_type,
                block_label=block.label,
            )
            workflow_run = await self.mark_workflow_run_as_canceled(workflow_run_id=workflow_run_id)
            return workflow_run, True
        if block_result.status == BlockStatus.failed:
            LOG.error(
                f"Block with type {block.block_type} at index {block_idx}/{blocks_cnt - 1} failed for workflow run {workflow_run_id}",
                block_type=block.block_type,
                workflow_run_id=workflow_run_id,
                block_idx=block_idx,
                block_result=block_result,
                block_type_var=block.block_type,
                block_label=block.label,
            )
            if not block.continue_on_failure:
                failure_reason = f"{block.block_type} block failed. failure reason: {block_result.failure_reason}"
                workflow_run = await self.mark_workflow_run_as_failed(
                    workflow_run_id=workflow_run_id, failure_reason=failure_reason
                )
                return workflow_run, True

            LOG.warning(
                f"Block with type {block.block_type} at index {block_idx}/{blocks_cnt - 1} failed but will continue executing the workflow run {workflow_run_id}",
                block_type=block.block_type,
                workflow_run_id=workflow_run_id,
                block_idx=block_idx,
                block_result=block_result,
                continue_on_failure=block.continue_on_failure,
                block_type_var=block.block_type,
                block_label=block.label,
            )
            return workflow_run, False

        if block_result.status == BlockStatus.terminated:
            LOG.info(
                f"Block with type {block.block_type} at index {block_idx}/{blocks_cnt - 1} was terminated for workflow run {workflow_run_id}, marking workflow run as terminated",
                block_type=block.block_type,
                workflow_run_id=workflow_run_id,
                block_idx=block_idx,
                block_result=block_result,
                block_type_var=block.block_type,
                block_label=block.label,
            )

            if not block.continue_on_failure:
                failure_reason = f"{block.block_type} block terminated. Reason: {block_result.failure_reason}"
                workflow_run = await self.mark_workflow_run_as_terminated(
                    workflow_run_id=workflow_run_id, failure_reason=failure_reason
                )
                return workflow_run, True

            LOG.warning(
                f"Block with type {block.block_type} at index {block_idx}/{blocks_cnt - 1} was terminated for workflow run {workflow_run_id}, but will continue executing the workflow run",
                block_type=block.block_type,
                workflow_run_id=workflow_run_id,
                block_idx=block_idx,
                block_result=block_result,
                continue_on_failure=block.continue_on_failure,
                block_type_var=block.block_type,
                block_label=block.label,
            )
            return workflow_run, False

        if block_result.status == BlockStatus.timed_out:
            LOG.info(
                f"Block with type {block.block_type} at index {block_idx}/{blocks_cnt - 1} timed out for workflow run {workflow_run_id}, marking workflow run as failed",
                block_type=block.block_type,
                workflow_run_id=workflow_run_id,
                block_idx=block_idx,
                block_result=block_result,
                block_type_var=block.block_type,
                block_label=block.label,
            )

            if not block.continue_on_failure:
                failure_reason = f"{block.block_type} block timed out. Reason: {block_result.failure_reason}"
                workflow_run = await self.mark_workflow_run_as_failed(
                    workflow_run_id=workflow_run_id, failure_reason=failure_reason
                )
                return workflow_run, True

            LOG.warning(
                f"Block with type {block.block_type} at index {block_idx}/{blocks_cnt - 1} timed out for workflow run {workflow_run_id}, but will continue executing the workflow run",
                block_type=block.block_type,
                workflow_run_id=workflow_run_id,
                block_idx=block_idx,
                block_result=block_result,
                continue_on_failure=block.continue_on_failure,
                block_type_var=block.block_type,
                block_label=block.label,
            )
            return workflow_run, False

        return workflow_run, False

    async def _execute_finally_block_if_configured(
        self,
        workflow: Workflow,
        workflow_run: WorkflowRun,
        organization: Organization,
        browser_session_id: str | None,
    ) -> None:
        finally_block_label = workflow.workflow_definition.finally_block_label
        if not finally_block_label:
            return

        label_to_block: dict[str, BlockTypeVar] = {block.label: block for block in workflow.workflow_definition.blocks}

        block = label_to_block.get(finally_block_label)
        if not block:
            LOG.warning(
                "Finally block label not found",
                workflow_run_id=workflow_run.workflow_run_id,
                finally_block_label=finally_block_label,
            )
            return

        try:
            parameters = block.get_all_parameters(workflow_run.workflow_run_id)
            await app.WORKFLOW_CONTEXT_MANAGER.register_block_parameters_for_workflow_run(
                workflow_run.workflow_run_id, parameters, organization
            )
            await block.execute_safe(
                workflow_run_id=workflow_run.workflow_run_id,
                organization_id=organization.organization_id,
                browser_session_id=browser_session_id,
            )
        except Exception as e:
            LOG.warning(
                "Finally block execution failed",
                workflow_run_id=workflow_run.workflow_run_id,
                block_label=block.label,
                error=str(e),
            )

    @staticmethod
    def _strip_finally_block_references(
        blocks: list[BlockTypeVar],
        finally_block_label: str,
    ) -> list[BlockTypeVar]:
        """Remove the finally block and nullify any edges that point to it.

        This prevents _build_workflow_graph from raising InvalidWorkflowDefinition
        when a block's next_block_label references the (now-excluded) finally block.
        """
        result: list[BlockTypeVar] = []
        for block in blocks:
            if block.label == finally_block_label:
                continue
            if isinstance(block, ConditionalBlock):
                patched_branches = [
                    branch.model_copy(update={"next_block_label": None})
                    if branch.next_block_label == finally_block_label
                    else branch
                    for branch in block.branch_conditions
                ]
                if patched_branches != block.branch_conditions:
                    block = block.model_copy(update={"branch_conditions": patched_branches})
            elif block.next_block_label == finally_block_label:
                block = block.model_copy(update={"next_block_label": None})
            result.append(block)
        return result

    def _build_workflow_graph(
        self,
        blocks: list[BlockTypeVar],
    ) -> tuple[str, dict[str, BlockTypeVar], dict[str, str | None]]:
        all_blocks = blocks
        label_to_block: dict[str, BlockTypeVar] = {}
        default_next_map: dict[str, str | None] = {}

        for block in all_blocks:
            if block.label in label_to_block:
                raise InvalidWorkflowDefinition(f"Duplicate block label detected: {block.label}")
            label_to_block[block.label] = block
            default_next_map[block.label] = block.next_block_label

        # Only apply sequential defaulting if there are no conditional blocks
        # Conditional blocks break sequential ordering since they have multiple branches
        has_conditional_blocks = any(isinstance(block, ConditionalBlock) for block in all_blocks)
        if not has_conditional_blocks:
            for idx, block in enumerate(blocks[:-1]):
                if default_next_map.get(block.label) is None:
                    default_next_map[block.label] = blocks[idx + 1].label

        adjacency: dict[str, set[str]] = {label: set() for label in label_to_block}
        incoming: dict[str, int] = {label: 0 for label in label_to_block}

        def _add_edge(source: str, target: str | None) -> None:
            if not target:
                return
            if target not in label_to_block:
                raise InvalidWorkflowDefinition(f"Block {source} references unknown next_block_label {target}")
            # Only increment incoming count if this is a new edge
            # (multiple branches of a conditional block may point to the same target)
            if target not in adjacency[source]:
                adjacency[source].add(target)
                incoming[target] += 1

        for label, block in label_to_block.items():
            if isinstance(block, ConditionalBlock):
                for branch in block.ordered_branches:
                    _add_edge(label, branch.next_block_label)
            else:
                _add_edge(label, default_next_map.get(label))

        roots = [label for label, count in incoming.items() if count == 0]
        if not roots:
            raise InvalidWorkflowDefinition("No entry block found for workflow definition")
        if len(roots) > 1:
            raise InvalidWorkflowDefinition(
                f"Multiple entry blocks detected ({', '.join(sorted(roots))}); only one entry block is supported."
            )

        # Kahn's algorithm for cycle detection
        queue: deque[str] = deque([roots[0]])
        visited_count = 0
        in_degree = dict(incoming)
        while queue:
            node = queue.popleft()
            visited_count += 1
            for neighbor in adjacency[node]:
                in_degree[neighbor] -= 1
                if in_degree[neighbor] == 0:
                    queue.append(neighbor)

        if visited_count != len(label_to_block):
            raise InvalidWorkflowDefinition("Workflow definition contains a cycle; DAG traversal is required.")

        return roots[0], label_to_block, default_next_map

    async def create_workflow(
        self,
        organization_id: str,
        title: str,
        workflow_definition: WorkflowDefinition,
        description: str | None = None,
        proxy_location: ProxyLocationInput = None,
        max_screenshot_scrolling_times: int | None = None,
        webhook_callback_url: str | None = None,
        totp_verification_url: str | None = None,
        totp_identifier: str | None = None,
        persist_browser_session: bool = False,
        model: dict[str, Any] | None = None,
        workflow_permanent_id: str | None = None,
        version: int | None = None,
        is_saved_task: bool = False,
        status: WorkflowStatus = WorkflowStatus.published,
        extra_http_headers: dict[str, str] | None = None,
        run_with: str | None = None,
        cache_key: str | None = None,
        ai_fallback: bool | None = None,
        run_sequentially: bool = False,
        sequential_key: str | None = None,
        folder_id: str | None = None,
    ) -> Workflow:
        try:
            return await app.DATABASE.create_workflow(
                title=title,
                workflow_definition=workflow_definition.model_dump(),
                organization_id=organization_id,
                description=description,
                proxy_location=proxy_location,
                webhook_callback_url=webhook_callback_url,
                max_screenshot_scrolling_times=max_screenshot_scrolling_times,
                totp_verification_url=totp_verification_url,
                totp_identifier=totp_identifier,
                persist_browser_session=persist_browser_session,
                model=model,
                workflow_permanent_id=workflow_permanent_id,
                version=version,
                is_saved_task=is_saved_task,
                status=status,
                extra_http_headers=extra_http_headers,
                run_with=run_with,
                cache_key=cache_key,
                ai_fallback=False if ai_fallback is None else ai_fallback,
                run_sequentially=run_sequentially,
                sequential_key=sequential_key,
                folder_id=folder_id,
            )
        except IntegrityError as e:
            if "uc_org_permanent_id_version" in str(e) and workflow_permanent_id:
                raise WorkflowVersionConflict(workflow_permanent_id) from e
            raise

    async def create_workflow_from_prompt(
        self,
        organization: Organization,
        user_prompt: str,
        totp_identifier: str | None = None,
        totp_verification_url: str | None = None,
        webhook_callback_url: str | None = None,
        proxy_location: ProxyLocationInput = None,
        max_screenshot_scrolling_times: int | None = None,
        extra_http_headers: dict[str, str] | None = None,
        max_iterations: int | None = None,
        max_steps: int | None = None,
        status: WorkflowStatus = WorkflowStatus.auto_generated,
        run_with: str | None = None,
        ai_fallback: bool = True,
        task_version: Literal["v1", "v2"] = "v2",
    ) -> Workflow:
        metadata_prompt = prompt_engine.load_prompt(
            "conversational_ui_goal",
            user_goal=user_prompt,
        )

        metadata_response = await app.LLM_API_HANDLER(
            prompt=metadata_prompt,
            prompt_name="conversational_ui_goal",
            organization_id=organization.organization_id,
        )

        block_label: str = metadata_response.get("block_label", None) or DEFAULT_FIRST_BLOCK_LABEL
        title: str = metadata_response.get("title", None) or DEFAULT_WORKFLOW_TITLE

        if task_version == "v1":
            task_prompt = prompt_engine.load_prompt(
                "generate-task",
                user_prompt=user_prompt,
            )

            task_response = await app.LLM_API_HANDLER(
                prompt=task_prompt,
                prompt_name="generate-task",
                organization_id=organization.organization_id,
            )

            data_extraction_goal: str | None = task_response.get("data_extraction_goal")
            navigation_goal: str = task_response.get("navigation_goal", None) or user_prompt
            url: str = task_response.get("url", None) or ""

            blocks = [
                NavigationBlock(
                    url=url,
                    label=block_label,
                    title=title,
                    navigation_goal=navigation_goal,
                    max_steps_per_run=max_steps or settings.MAX_STEPS_PER_RUN,
                    totp_verification_url=totp_verification_url,
                    totp_identifier=totp_identifier,
                    output_parameter=OutputParameter(
                        output_parameter_id=str(uuid.uuid4()),
                        key=f"{block_label}_output",
                        workflow_id="",
                        created_at=datetime.now(UTC),
                        modified_at=datetime.now(UTC),
                    ),
                ),
            ]

            if data_extraction_goal:
                blocks.append(
                    ExtractionBlock(
                        label="extract_data",
                        title="Extract Data",
                        data_extraction_goal=data_extraction_goal,
                        output_parameter=OutputParameter(
                            output_parameter_id=str(uuid.uuid4()),
                            key="extract_data_output",
                            workflow_id="",
                            created_at=datetime.now(UTC),
                            modified_at=datetime.now(UTC),
                        ),
                        max_steps_per_run=max_steps or settings.MAX_STEPS_PER_RUN,
                        totp_verification_url=totp_verification_url,
                        totp_identifier=totp_identifier,
                    )
                )

        elif task_version == "v2":
            blocks = [
                TaskV2Block(
                    prompt=user_prompt,
                    totp_identifier=totp_identifier,
                    totp_verification_url=totp_verification_url,
                    label=block_label,
                    max_iterations=max_iterations or settings.MAX_ITERATIONS_PER_TASK_V2,
                    max_steps=max_steps or settings.MAX_STEPS_PER_TASK_V2,
                    output_parameter=OutputParameter(
                        output_parameter_id=str(uuid.uuid4()),
                        key=f"{block_label}_output",
                        workflow_id="",
                        created_at=datetime.now(UTC),
                        modified_at=datetime.now(UTC),
                    ),
                )
            ]

        new_workflow = await self.create_workflow(
            title=title,
            workflow_definition=WorkflowDefinition(parameters=[], blocks=blocks),
            organization_id=organization.organization_id,
            proxy_location=proxy_location,
            webhook_callback_url=webhook_callback_url,
            totp_verification_url=totp_verification_url,
            totp_identifier=totp_identifier,
            max_screenshot_scrolling_times=max_screenshot_scrolling_times,
            extra_http_headers=extra_http_headers,
            status=status,
            run_with=run_with,
            ai_fallback=ai_fallback,
        )

        return new_workflow

    async def get_workflow(self, workflow_id: str, organization_id: str | None = None) -> Workflow:
        workflow = await app.DATABASE.get_workflow(workflow_id=workflow_id, organization_id=organization_id)
        if not workflow:
            raise WorkflowNotFound(workflow_id=workflow_id)
        return workflow

    async def get_workflow_by_permanent_id(
        self,
        workflow_permanent_id: str,
        organization_id: str | None = None,
        version: int | None = None,
        exclude_deleted: bool = True,
    ) -> Workflow:
        workflow = await app.DATABASE.get_workflow_by_permanent_id(
            workflow_permanent_id,
            organization_id=organization_id,
            version=version,
            exclude_deleted=exclude_deleted,
        )
        if not workflow:
            raise WorkflowNotFound(workflow_permanent_id=workflow_permanent_id, version=version)

        return workflow

    async def set_template_status(
        self,
        organization_id: str,
        workflow_permanent_id: str,
        is_template: bool,
    ) -> dict[str, Any]:
        """
        Set or unset a workflow as a template.

        Template status is stored in a separate workflow_templates table keyed by
        workflow_permanent_id, since template status is a property of the workflow
        identity, not a specific version.

        Returns a dict with the result since we're not updating the workflow itself.
        """
        # Verify workflow exists and belongs to org
        await self.get_workflow_by_permanent_id(
            workflow_permanent_id=workflow_permanent_id,
            organization_id=organization_id,
        )

        if is_template:
            await app.DATABASE.add_workflow_template(
                workflow_permanent_id=workflow_permanent_id,
                organization_id=organization_id,
            )
        else:
            await app.DATABASE.remove_workflow_template(
                workflow_permanent_id=workflow_permanent_id,
                organization_id=organization_id,
            )

        return {"workflow_permanent_id": workflow_permanent_id, "is_template": is_template}

    async def get_workflow_versions_by_permanent_id(
        self,
        workflow_permanent_id: str,
        organization_id: str | None = None,
        exclude_deleted: bool = True,
    ) -> list[Workflow]:
        """
        Get all versions of a workflow by its permanent ID.
        Returns an empty list if no workflow is found with that permanent ID.
        """
        workflows = await app.DATABASE.get_workflow_versions_by_permanent_id(
            workflow_permanent_id,
            organization_id=organization_id,
            exclude_deleted=exclude_deleted,
        )
        return workflows

    async def get_workflow_by_workflow_run_id(
        self,
        workflow_run_id: str,
        organization_id: str | None = None,
        exclude_deleted: bool = True,
    ) -> Workflow:
        workflow = await app.DATABASE.get_workflow_for_workflow_run(
            workflow_run_id,
            organization_id=organization_id,
            exclude_deleted=exclude_deleted,
        )

        if not workflow:
            raise WorkflowNotFoundForWorkflowRun(workflow_run_id=workflow_run_id)

        return workflow

    async def get_block_outputs_for_debug_session(
        self,
        workflow_permanent_id: str,
        user_id: str,
        organization_id: str,
        exclude_deleted: bool = True,
        version: int | None = None,
    ) -> dict[str, dict[str, Any]]:
        workflow = await app.DATABASE.get_workflow_by_permanent_id(
            workflow_permanent_id,
            organization_id=organization_id,
            version=version,
            exclude_deleted=exclude_deleted,
        )

        if not workflow:
            raise WorkflowNotFound(workflow_permanent_id=workflow_permanent_id, version=version)

        labels_to_outputs: dict[str, BlockOutputParameter] = {}

        for block in workflow.workflow_definition.blocks:
            label = block.label

            block_run = await app.DATABASE.get_latest_completed_block_run(
                organization_id=organization_id,
                user_id=user_id,
                block_label=label,
                workflow_permanent_id=workflow_permanent_id,
            )

            if not block_run:
                continue

            output_parameter = await app.DATABASE.get_workflow_run_output_parameter_by_id(
                workflow_run_id=block_run.workflow_run_id, output_parameter_id=block_run.output_parameter_id
            )

            if not output_parameter:
                continue

            block_output_parameter = output_parameter.value

            if not isinstance(block_output_parameter, dict):
                continue

            block_output_parameter["created_at"] = output_parameter.created_at
            labels_to_outputs[label] = block_output_parameter  # type: ignore[assignment]

        return labels_to_outputs  # type: ignore[return-value]

    async def get_workflows_by_permanent_ids(
        self,
        workflow_permanent_ids: list[str],
        organization_id: str | None = None,
        page: int = 1,
        page_size: int = 10,
        search_key: str = "",
        statuses: list[WorkflowStatus] | None = None,
    ) -> list[Workflow]:
        return await app.DATABASE.get_workflows_by_permanent_ids(
            workflow_permanent_ids,
            organization_id=organization_id,
            page=page,
            page_size=page_size,
            title=search_key,
            statuses=statuses,
        )

    async def get_workflows_by_organization_id(
        self,
        organization_id: str,
        page: int = 1,
        page_size: int = 10,
        only_saved_tasks: bool = False,
        only_workflows: bool = False,
        only_templates: bool = False,
        search_key: str | None = None,
        folder_id: str | None = None,
        statuses: list[WorkflowStatus] | None = None,
    ) -> list[Workflow]:
        """
        Get all workflows with the latest version for the organization.

        Args:
            search_key: Unified search term for title, folder name, and parameter metadata.
            folder_id: Filter workflows by folder ID.
        """
        return await app.DATABASE.get_workflows_by_organization_id(
            organization_id=organization_id,
            page=page,
            page_size=page_size,
            only_saved_tasks=only_saved_tasks,
            only_workflows=only_workflows,
            only_templates=only_templates,
            search_key=search_key,
            folder_id=folder_id,
            statuses=statuses,
        )

    async def update_workflow_definition(
        self,
        workflow_id: str,
        organization_id: str | None = None,
        title: str | None = None,
        description: str | None = None,
        workflow_definition: WorkflowDefinition | None = None,
    ) -> Workflow:
        updated_workflow = await app.DATABASE.update_workflow(
            workflow_id=workflow_id,
            title=title,
            organization_id=organization_id,
            description=description,
            workflow_definition=(workflow_definition.model_dump() if workflow_definition else None),
        )

        return updated_workflow

    async def maybe_delete_cached_code(
        self,
        workflow: Workflow,
        workflow_definition: WorkflowDefinition,
        organization_id: str,
        delete_script: bool = True,
    ) -> None:
        if workflow_definition:
            workflow_definition.validate()

        previous_valid_workflow = await app.DATABASE.get_workflow_by_permanent_id(
            workflow_permanent_id=workflow.workflow_permanent_id,
            organization_id=organization_id,
            exclude_deleted=True,
            ignore_version=workflow.version,
        )

        current_definition: dict[str, Any] = {}
        new_definition: dict[str, Any] = {}
        if previous_valid_workflow:
            current_definition = _get_workflow_definition_core_data(previous_valid_workflow.workflow_definition)
            new_definition = _get_workflow_definition_core_data(workflow_definition)
            has_changes = current_definition != new_definition

            # Log definition changes for debugging cache invalidation issues (SKY-7016)
            if has_changes:
                LOG.debug(
                    "Workflow definition has changes, checking for cache invalidation",
                    workflow_id=workflow.workflow_id,
                    workflow_permanent_id=workflow.workflow_permanent_id,
                    organization_id=organization_id,
                    previous_version=previous_valid_workflow.version,
                    new_version=workflow.version,
                    current_block_count=len(current_definition.get("blocks", [])),
                    new_block_count=len(new_definition.get("blocks", [])),
                    current_param_count=len(current_definition.get("parameters", [])),
                    new_param_count=len(new_definition.get("parameters", [])),
                )
            else:
                LOG.debug(
                    "Workflow definition unchanged, skipping cache invalidation check",
                    workflow_id=workflow.workflow_id,
                    workflow_permanent_id=workflow.workflow_permanent_id,
                    organization_id=organization_id,
                )
        else:
            has_changes = False

        if previous_valid_workflow and has_changes and delete_script:
            plan = self._determine_cache_invalidation(
                previous_blocks=current_definition.get("blocks", []),
                new_blocks=new_definition.get("blocks", []),
            )
            candidates = await app.DATABASE.get_workflow_scripts_by_permanent_id(
                organization_id=organization_id,
                workflow_permanent_id=previous_valid_workflow.workflow_permanent_id,
            )

            if plan.has_targets:
                cached_groups, published_groups = await self._partition_cached_blocks(
                    organization_id=organization_id,
                    candidates=candidates,
                    block_labels_to_disable=plan.block_labels_to_disable,
                )

                if not cached_groups and not published_groups:
                    LOG.info(
                        "Workflow definition changed, no cached script blocks found after workflow block change",
                        workflow_id=workflow.workflow_id,
                        workflow_permanent_id=previous_valid_workflow.workflow_permanent_id,
                        organization_id=organization_id,
                        previous_version=previous_valid_workflow.version,
                        new_version=workflow.version,
                        invalidate_reason=plan.reason,
                        invalidate_label=plan.label,
                        invalidate_index_prev=plan.previous_index,
                        invalidate_index_new=plan.new_index,
                        block_labels_to_disable=plan.block_labels_to_disable,
                    )
                    return

                try:
                    groups_to_clear = [*cached_groups, *published_groups]
                    await self._clear_cached_block_groups(
                        organization_id=organization_id,
                        workflow=workflow,
                        previous_workflow=previous_valid_workflow,
                        plan=plan,
                        groups=groups_to_clear,
                    )
                except Exception as e:
                    LOG.error(
                        "Failed to clear cached script blocks after workflow block change",
                        workflow_id=workflow.workflow_id,
                        workflow_permanent_id=previous_valid_workflow.workflow_permanent_id,
                        organization_id=organization_id,
                        previous_version=previous_valid_workflow.version,
                        new_version=workflow.version,
                        invalidate_reason=plan.reason,
                        invalidate_label=plan.label,
                        invalidate_index_prev=plan.previous_index,
                        invalidate_index_new=plan.new_index,
                        error=str(e),
                    )

                return

            if plan.previous_index is not None:
                LOG.info(
                    "Workflow definition changed, no cached script blocks exist to clear for workflow block change",
                    workflow_id=workflow.workflow_id,
                    workflow_permanent_id=previous_valid_workflow.workflow_permanent_id,
                    organization_id=organization_id,
                    previous_version=previous_valid_workflow.version,
                    new_version=workflow.version,
                    invalidate_reason=plan.reason,
                    invalidate_label=plan.label,
                    invalidate_index_prev=plan.previous_index,
                    invalidate_index_new=plan.new_index,
                )
                return

            to_delete = candidates

            if len(to_delete) > 0:
                try:
                    await app.DATABASE.delete_workflow_scripts_by_permanent_id(
                        organization_id=organization_id,
                        workflow_permanent_id=previous_valid_workflow.workflow_permanent_id,
                        script_ids=[s.script_id for s in to_delete],
                    )
                except Exception as e:
                    LOG.error(
                        "Failed to delete workflow scripts after workflow definition change",
                        workflow_id=workflow.workflow_id,
                        workflow_permanent_id=previous_valid_workflow.workflow_permanent_id,
                        organization_id=organization_id,
                        previous_version=previous_valid_workflow.version,
                        new_version=workflow.version,
                        error=str(e),
                        to_delete_ids=[script.script_id for script in to_delete],
                        to_delete_cnt=len(to_delete),
                    )

    async def delete_workflow_by_permanent_id(
        self,
        workflow_permanent_id: str,
        organization_id: str | None = None,
    ) -> None:
        await app.DATABASE.soft_delete_workflow_by_permanent_id(
            workflow_permanent_id=workflow_permanent_id,
            organization_id=organization_id,
        )

    async def delete_workflow_by_id(
        self,
        workflow_id: str,
        organization_id: str,
    ) -> None:
        await app.DATABASE.soft_delete_workflow_by_id(
            workflow_id=workflow_id,
            organization_id=organization_id,
        )

    async def get_workflow_runs(
        self,
        organization_id: str,
        page: int = 1,
        page_size: int = 10,
        status: list[WorkflowRunStatus] | None = None,
        ordering: tuple[str, str] | None = None,
        search_key: str | None = None,
        error_code: str | None = None,
    ) -> list[WorkflowRun]:
        return await app.DATABASE.get_workflow_runs(
            organization_id=organization_id,
            page=page,
            page_size=page_size,
            status=status,
            ordering=ordering,
            search_key=search_key,
            error_code=error_code,
        )

    async def get_workflow_runs_count(
        self,
        organization_id: str,
        status: list[WorkflowRunStatus] | None = None,
    ) -> int:
        return await app.DATABASE.get_workflow_runs_count(
            organization_id=organization_id,
            status=status,
        )

    async def get_workflow_runs_for_workflow_permanent_id(
        self,
        workflow_permanent_id: str,
        organization_id: str,
        page: int = 1,
        page_size: int = 10,
        status: list[WorkflowRunStatus] | None = None,
        search_key: str | None = None,
        error_code: str | None = None,
    ) -> list[WorkflowRun]:
        return await app.DATABASE.get_workflow_runs_for_workflow_permanent_id(
            workflow_permanent_id=workflow_permanent_id,
            organization_id=organization_id,
            page=page,
            page_size=page_size,
            status=status,
            search_key=search_key,
            error_code=error_code,
        )

    async def create_workflow_run(
        self,
        workflow_request: WorkflowRequestBody,
        workflow_permanent_id: str,
        workflow_id: str,
        organization_id: str,
        parent_workflow_run_id: str | None = None,
        sequential_key: str | None = None,
        debug_session_id: str | None = None,
        code_gen: bool | None = None,
    ) -> WorkflowRun:
        # validate the browser session or profile id
        if workflow_request.browser_session_id:
            browser_session = await app.DATABASE.get_persistent_browser_session(
                session_id=workflow_request.browser_session_id,
                organization_id=organization_id,
            )
            if not browser_session:
                raise BrowserSessionNotFound(browser_session_id=workflow_request.browser_session_id)

        if workflow_request.browser_profile_id:
            browser_profile = await app.DATABASE.get_browser_profile(
                workflow_request.browser_profile_id,
                organization_id=organization_id,
            )
            if not browser_profile:
                raise BrowserProfileNotFound(
                    profile_id=workflow_request.browser_profile_id,
                    organization_id=organization_id,
                )

        return await app.DATABASE.create_workflow_run(
            workflow_permanent_id=workflow_permanent_id,
            workflow_id=workflow_id,
            organization_id=organization_id,
            browser_session_id=workflow_request.browser_session_id,
            browser_profile_id=workflow_request.browser_profile_id,
            proxy_location=workflow_request.proxy_location,
            webhook_callback_url=workflow_request.webhook_callback_url,
            totp_verification_url=workflow_request.totp_verification_url,
            totp_identifier=workflow_request.totp_identifier,
            parent_workflow_run_id=parent_workflow_run_id,
            max_screenshot_scrolling_times=workflow_request.max_screenshot_scrolls,
            extra_http_headers=workflow_request.extra_http_headers,
            browser_address=workflow_request.browser_address,
            sequential_key=sequential_key,
            run_with=workflow_request.run_with,
            debug_session_id=debug_session_id,
            ai_fallback=workflow_request.ai_fallback,
            code_gen=code_gen,
        )

    async def _update_workflow_run_status(
        self,
        workflow_run_id: str,
        status: WorkflowRunStatus,
        failure_reason: str | None = None,
        run_with: str | None = None,
        ai_fallback: bool | None = None,
    ) -> WorkflowRun:
        workflow_run = await app.DATABASE.update_workflow_run(
            workflow_run_id=workflow_run_id,
            status=status,
            failure_reason=failure_reason,
            run_with=run_with,
            ai_fallback=ai_fallback,
        )
        if status in [WorkflowRunStatus.completed, WorkflowRunStatus.failed, WorkflowRunStatus.terminated]:
            start_time = (
                workflow_run.started_at.replace(tzinfo=UTC)
                if workflow_run.started_at
                else workflow_run.created_at.replace(tzinfo=UTC)
            )
            queued_seconds = (start_time - workflow_run.created_at.replace(tzinfo=UTC)).total_seconds()
            duration_seconds = (datetime.now(UTC) - start_time).total_seconds()
            LOG.info(
                "Workflow run duration metrics",
                workflow_run_id=workflow_run_id,
                workflow_id=workflow_run.workflow_id,
                queued_seconds=queued_seconds,
                duration_seconds=duration_seconds,
                workflow_run_status=workflow_run.status,
                organization_id=workflow_run.organization_id,
                run_with=workflow_run.run_with,
                ai_fallback=workflow_run.ai_fallback,
            )
        return workflow_run

    async def mark_workflow_run_as_completed(self, workflow_run_id: str, run_with: str | None = None) -> WorkflowRun:
        LOG.info(
            f"Marking workflow run {workflow_run_id} as completed",
            workflow_run_id=workflow_run_id,
            workflow_status="completed",
        )

        # Add workflow completion tag to trace
        otel_trace.get_current_span().set_attribute("task.completion_status", WorkflowRunStatus.completed)

        return await self._update_workflow_run_status(
            workflow_run_id=workflow_run_id,
            status=WorkflowRunStatus.completed,
            run_with=run_with,
        )

    async def _finalize_workflow_run_status(
        self,
        workflow_run_id: str,
        workflow_run: WorkflowRun,
        pre_finally_status: WorkflowRunStatus,
        pre_finally_failure_reason: str | None,
    ) -> WorkflowRun:
        """
        Set final workflow run status based on pre-finally state.
        Called unconditionally to ensure unified flow.
        """
        if pre_finally_status not in (
            WorkflowRunStatus.canceled,
            WorkflowRunStatus.failed,
            WorkflowRunStatus.terminated,
            WorkflowRunStatus.timed_out,
        ):
            return await self.mark_workflow_run_as_completed(workflow_run_id)

        if workflow_run.status == WorkflowRunStatus.running:
            # We temporarily set to running for finally block, restore terminal status
            return await self._update_workflow_run_status(
                workflow_run_id=workflow_run_id,
                status=pre_finally_status,
                failure_reason=pre_finally_failure_reason,
            )

        return workflow_run

    async def mark_workflow_run_as_failed(
        self,
        workflow_run_id: str,
        failure_reason: str | None,
        run_with: str | None = None,
    ) -> WorkflowRun:
        LOG.info(
            f"Marking workflow run {workflow_run_id} as failed",
            workflow_run_id=workflow_run_id,
            workflow_status="failed",
            failure_reason=failure_reason,
        )

        # Add workflow failure tag to trace
        otel_trace.get_current_span().set_attribute("task.completion_status", WorkflowRunStatus.failed)

        return await self._update_workflow_run_status(
            workflow_run_id=workflow_run_id,
            status=WorkflowRunStatus.failed,
            failure_reason=failure_reason,
            run_with=run_with,
        )

    async def mark_workflow_run_as_running(self, workflow_run_id: str, run_with: str | None = None) -> WorkflowRun:
        workflow_run = await self._update_workflow_run_status(
            workflow_run_id=workflow_run_id,
            status=WorkflowRunStatus.running,
            run_with=run_with,
        )
        start_time = (
            workflow_run.started_at.replace(tzinfo=UTC)
            if workflow_run.started_at
            else workflow_run.created_at.replace(tzinfo=UTC)
        )
        queued_seconds = (start_time - workflow_run.created_at.replace(tzinfo=UTC)).total_seconds()
        LOG.info(
            f"Marked workflow run {workflow_run_id} as running",
            workflow_run_id=workflow_run_id,
            workflow_status="running",
            run_with=run_with,
            queued_seconds=queued_seconds,
        )
        return workflow_run

    async def mark_workflow_run_as_terminated(
        self,
        workflow_run_id: str,
        failure_reason: str | None,
        run_with: str | None = None,
    ) -> WorkflowRun:
        LOG.info(
            f"Marking workflow run {workflow_run_id} as terminated",
            workflow_run_id=workflow_run_id,
            workflow_status="terminated",
            failure_reason=failure_reason,
        )

        # Add workflow terminated tag to trace
        otel_trace.get_current_span().set_attribute("task.completion_status", WorkflowRunStatus.terminated)

        return await self._update_workflow_run_status(
            workflow_run_id=workflow_run_id,
            status=WorkflowRunStatus.terminated,
            failure_reason=failure_reason,
            run_with=run_with,
        )

    async def mark_workflow_run_as_canceled(self, workflow_run_id: str, run_with: str | None = None) -> WorkflowRun:
        LOG.info(
            f"Marking workflow run {workflow_run_id} as canceled",
            workflow_run_id=workflow_run_id,
            workflow_status="canceled",
        )

        # Add workflow canceled tag to trace
        otel_trace.get_current_span().set_attribute("task.completion_status", WorkflowRunStatus.canceled)

        return await self._update_workflow_run_status(
            workflow_run_id=workflow_run_id,
            status=WorkflowRunStatus.canceled,
            run_with=run_with,
        )

    async def mark_workflow_run_as_timed_out(
        self,
        workflow_run_id: str,
        failure_reason: str | None = None,
        run_with: str | None = None,
    ) -> WorkflowRun:
        LOG.info(
            f"Marking workflow run {workflow_run_id} as timed out",
            workflow_run_id=workflow_run_id,
            workflow_status="timed_out",
        )

        # Add workflow timed out tag to trace
        otel_trace.get_current_span().set_attribute("task.completion_status", WorkflowRunStatus.timed_out)

        return await self._update_workflow_run_status(
            workflow_run_id=workflow_run_id,
            status=WorkflowRunStatus.timed_out,
            failure_reason=failure_reason,
            run_with=run_with,
        )

    async def get_workflow_run(self, workflow_run_id: str, organization_id: str | None = None) -> WorkflowRun:
        workflow_run = await app.DATABASE.get_workflow_run(
            workflow_run_id=workflow_run_id,
            organization_id=organization_id,
        )
        if not workflow_run:
            raise WorkflowRunNotFound(workflow_run_id)
        return workflow_run

    async def create_workflow_parameter(
        self,
        workflow_id: str,
        workflow_parameter_type: WorkflowParameterType,
        key: str,
        default_value: bool | int | float | str | dict | list | None = None,
        description: str | None = None,
    ) -> WorkflowParameter:
        return await app.DATABASE.create_workflow_parameter(
            workflow_id=workflow_id,
            workflow_parameter_type=workflow_parameter_type,
            key=key,
            description=description,
            default_value=default_value,
        )

    async def create_aws_secret_parameter(
        self, workflow_id: str, aws_key: str, key: str, description: str | None = None
    ) -> AWSSecretParameter:
        return await app.DATABASE.create_aws_secret_parameter(
            workflow_id=workflow_id, aws_key=aws_key, key=key, description=description
        )

    async def create_output_parameter(
        self, workflow_id: str, key: str, description: str | None = None
    ) -> OutputParameter:
        return await app.DATABASE.create_output_parameter(workflow_id=workflow_id, key=key, description=description)

    async def get_workflow_parameters(self, workflow_id: str) -> list[WorkflowParameter]:
        return await app.DATABASE.get_workflow_parameters(workflow_id=workflow_id)

    async def create_workflow_run_parameter(
        self,
        workflow_run_id: str,
        workflow_parameter: WorkflowParameter,
        value: Any,
    ) -> WorkflowRunParameter:
        value = json.dumps(value) if isinstance(value, (dict, list)) else value
        # InvalidWorkflowParameter will be raised if the validation fails
        workflow_parameter.workflow_parameter_type.convert_value(value)

        return await app.DATABASE.create_workflow_run_parameter(
            workflow_run_id=workflow_run_id,
            workflow_parameter=workflow_parameter,
            value=value,
        )

    async def get_workflow_run_parameter_tuples(
        self, workflow_run_id: str
    ) -> list[tuple[WorkflowParameter, WorkflowRunParameter]]:
        return await app.DATABASE.get_workflow_run_parameters(workflow_run_id=workflow_run_id)

    @staticmethod
    async def get_workflow_output_parameters(workflow_id: str) -> list[OutputParameter]:
        return await app.DATABASE.get_workflow_output_parameters(workflow_id=workflow_id)

    @staticmethod
    async def get_workflow_run_output_parameters(
        workflow_run_id: str,
    ) -> list[WorkflowRunOutputParameter]:
        return await app.DATABASE.get_workflow_run_output_parameters(workflow_run_id=workflow_run_id)

    @staticmethod
    async def get_output_parameter_workflow_run_output_parameter_tuples(
        workflow_id: str,
        workflow_run_id: str,
    ) -> list[tuple[OutputParameter, WorkflowRunOutputParameter]]:
        workflow_run_output_parameters = await app.DATABASE.get_workflow_run_output_parameters(
            workflow_run_id=workflow_run_id
        )
        output_parameters = await app.DATABASE.get_workflow_output_parameters_by_ids(
            output_parameter_ids=[
                workflow_run_output_parameter.output_parameter_id
                for workflow_run_output_parameter in workflow_run_output_parameters
            ]
        )

        return [
            (output_parameter, workflow_run_output_parameter)
            for workflow_run_output_parameter in workflow_run_output_parameters
            for output_parameter in output_parameters
            if output_parameter.output_parameter_id == workflow_run_output_parameter.output_parameter_id
        ]

    async def get_last_task_for_workflow_run(self, workflow_run_id: str) -> Task | None:
        return await app.DATABASE.get_last_task_for_workflow_run(workflow_run_id=workflow_run_id)

    async def get_tasks_by_workflow_run_id(self, workflow_run_id: str) -> list[Task]:
        return await app.DATABASE.get_tasks_by_workflow_run_id(workflow_run_id=workflow_run_id)

    async def get_recent_task_screenshot_artifacts(
        self,
        *,
        organization_id: str | None,
        task_id: str | None = None,
        task_v2_id: str | None = None,
        limit: int = 3,
    ) -> list[Artifact]:
        """Return the latest action/final screenshot artifacts for a task (v1 or v2)."""

        artifact_types = [ArtifactType.SCREENSHOT_ACTION, ArtifactType.SCREENSHOT_FINAL]

        artifacts: list[Artifact] = []
        if task_id:
            artifacts = (
                await app.DATABASE.get_latest_n_artifacts(
                    task_id=task_id,
                    artifact_types=artifact_types,
                    organization_id=organization_id,
                    n=limit,
                )
                or []
            )
        elif task_v2_id:
            action_artifacts = await app.DATABASE.get_artifacts_by_entity_id(
                organization_id=organization_id,
                artifact_type=ArtifactType.SCREENSHOT_ACTION,
                task_v2_id=task_v2_id,
                limit=limit,
            )
            final_artifacts = await app.DATABASE.get_artifacts_by_entity_id(
                organization_id=organization_id,
                artifact_type=ArtifactType.SCREENSHOT_FINAL,
                task_v2_id=task_v2_id,
                limit=limit,
            )
            artifacts = sorted(
                (action_artifacts or []) + (final_artifacts or []),
                key=lambda artifact: artifact.created_at,
                reverse=True,
            )[:limit]

        return artifacts

    async def get_recent_task_screenshot_urls(
        self,
        *,
        organization_id: str | None,
        task_id: str | None = None,
        task_v2_id: str | None = None,
        limit: int = 3,
    ) -> list[str]:
        """Return the latest action/final screenshot URLs for a task (v1 or v2)."""
        artifacts = await self.get_recent_task_screenshot_artifacts(
            organization_id=organization_id,
            task_id=task_id,
            task_v2_id=task_v2_id,
            limit=limit,
        )
        if not artifacts:
            return []
        return await app.ARTIFACT_MANAGER.get_share_links(artifacts) or []

    async def get_recent_workflow_screenshot_artifacts(
        self,
        workflow_run_id: str,
        organization_id: str | None = None,
        limit: int = 3,
        workflow_run_tasks: list[Task] | None = None,
    ) -> list[Artifact]:
        """Return latest screenshot artifacts across recent tasks in a workflow run."""

        screenshot_artifacts: list[Artifact] = []
        seen_artifact_ids: set[str] = set()

        if workflow_run_tasks is None:
            workflow_run_tasks = await app.DATABASE.get_tasks_by_workflow_run_id(workflow_run_id=workflow_run_id)

        for task in workflow_run_tasks[::-1]:
            artifact = await app.DATABASE.get_latest_artifact(
                task_id=task.task_id,
                artifact_types=[ArtifactType.SCREENSHOT_ACTION, ArtifactType.SCREENSHOT_FINAL],
                organization_id=organization_id,
            )
            if artifact:
                screenshot_artifacts.append(artifact)
                seen_artifact_ids.add(artifact.artifact_id)
            if len(screenshot_artifacts) >= limit:
                break

        if len(screenshot_artifacts) < limit:
            action_artifacts = await app.DATABASE.get_artifacts_by_entity_id(
                organization_id=organization_id,
                artifact_type=ArtifactType.SCREENSHOT_ACTION,
                workflow_run_id=workflow_run_id,
                limit=limit,
            )
            final_artifacts = await app.DATABASE.get_artifacts_by_entity_id(
                organization_id=organization_id,
                artifact_type=ArtifactType.SCREENSHOT_FINAL,
                workflow_run_id=workflow_run_id,
                limit=limit,
            )
            # Support runs that may not have Task rows (e.g., task_v2-only executions)
            for artifact in sorted(
                (action_artifacts or []) + (final_artifacts or []),
                key=lambda artifact: artifact.created_at,
                reverse=True,
            ):
                if artifact.artifact_id in seen_artifact_ids:
                    continue
                screenshot_artifacts.append(artifact)
                seen_artifact_ids.add(artifact.artifact_id)
                if len(screenshot_artifacts) >= limit:
                    break

        return screenshot_artifacts

    async def get_recent_workflow_screenshot_urls(
        self,
        workflow_run_id: str,
        organization_id: str | None = None,
        limit: int = 3,
        workflow_run_tasks: list[Task] | None = None,
    ) -> list[str]:
        """Return latest screenshot URLs across recent tasks in a workflow run."""
        artifacts = await self.get_recent_workflow_screenshot_artifacts(
            workflow_run_id=workflow_run_id,
            organization_id=organization_id,
            limit=limit,
            workflow_run_tasks=workflow_run_tasks,
        )
        if not artifacts:
            return []
        return await app.ARTIFACT_MANAGER.get_share_links(artifacts) or []

    async def build_workflow_run_status_response_by_workflow_id(
        self,
        workflow_run_id: str,
        organization_id: str | None = None,
        include_cost: bool = False,
        include_step_count: bool = False,
    ) -> WorkflowRunResponseBase:
        workflow_run = await self.get_workflow_run(workflow_run_id=workflow_run_id, organization_id=organization_id)
        if workflow_run is None:
            LOG.error(f"Workflow run {workflow_run_id} not found")
            raise WorkflowRunNotFound(workflow_run_id=workflow_run_id)
        workflow_permanent_id = workflow_run.workflow_permanent_id
        return await self.build_workflow_run_status_response(
            workflow_permanent_id=workflow_permanent_id,
            workflow_run_id=workflow_run_id,
            organization_id=organization_id,
            include_cost=include_cost,
            include_step_count=include_step_count,
        )

    async def build_workflow_run_status_response(
        self,
        workflow_permanent_id: str,
        workflow_run_id: str,
        organization_id: str | None = None,
        include_cost: bool = False,
        include_step_count: bool = False,
    ) -> WorkflowRunResponseBase:
        workflow = await self.get_workflow_by_permanent_id(workflow_permanent_id)
        if workflow is None:
            LOG.error(f"Workflow {workflow_permanent_id} not found")
            raise WorkflowNotFound(workflow_permanent_id=workflow_permanent_id)

        workflow_run = await self.get_workflow_run(workflow_run_id=workflow_run_id, organization_id=organization_id)

        task_v2 = await app.DATABASE.get_task_v2_by_workflow_run_id(
            workflow_run_id=workflow_run_id,
            organization_id=organization_id,
        )
        workflow_run_tasks = await app.DATABASE.get_tasks_by_workflow_run_id(workflow_run_id=workflow_run_id)
        screenshot_urls: list[str] | None = await self.get_recent_workflow_screenshot_urls(
            workflow_run_id=workflow_run_id,
            organization_id=organization_id,
            workflow_run_tasks=workflow_run_tasks,
        )
        screenshot_urls = screenshot_urls or None

        recording_url = None
        # Get recording url from browser session first,
        # if not found, get the recording url from the artifacts
        if workflow_run.browser_session_id:
            try:
                async with asyncio.timeout(GET_DOWNLOADED_FILES_TIMEOUT):
                    recordings = await app.STORAGE.get_shared_recordings_in_browser_session(
                        organization_id=workflow_run.organization_id,
                        browser_session_id=workflow_run.browser_session_id,
                    )
                    # FIXME: we only support one recording for now
                    recording_url = recordings[0].url if recordings else None
            except asyncio.TimeoutError:
                LOG.warning("Timeout getting recordings", browser_session_id=workflow_run.browser_session_id)

        if recording_url is None:
            recording_artifact = await app.DATABASE.get_artifact_for_run(
                run_id=task_v2.observer_cruise_id if task_v2 else workflow_run_id,
                artifact_type=ArtifactType.RECORDING,
                organization_id=organization_id,
            )
            if recording_artifact:
                recording_url = await app.ARTIFACT_MANAGER.get_share_link(recording_artifact)

        downloaded_files: list[FileInfo] = []
        downloaded_file_urls: list[str] | None = None
        try:
            async with asyncio.timeout(GET_DOWNLOADED_FILES_TIMEOUT):
                context = skyvern_context.current()
                downloaded_files = await app.STORAGE.get_downloaded_files(
                    organization_id=workflow_run.organization_id,
                    run_id=context.run_id if context and context.run_id else workflow_run.workflow_run_id,
                )
                if task_v2:
                    task_v2_downloaded_files = await app.STORAGE.get_downloaded_files(
                        organization_id=workflow_run.organization_id,
                        run_id=task_v2.observer_cruise_id,
                    )
                    if task_v2_downloaded_files:
                        downloaded_files.extend(task_v2_downloaded_files)
                if downloaded_files:
                    downloaded_file_urls = [file_info.url for file_info in downloaded_files]
        except asyncio.TimeoutError:
            LOG.warning(
                "Timeout to get downloaded files",
                workflow_run_id=workflow_run.workflow_run_id,
            )
        except Exception:
            LOG.warning(
                "Failed to get downloaded files",
                exc_info=True,
                workflow_run_id=workflow_run.workflow_run_id,
            )

        workflow_parameter_tuples = await app.DATABASE.get_workflow_run_parameters(workflow_run_id=workflow_run_id)
        parameters_with_value = {wfp.key: wfrp.value for wfp, wfrp in workflow_parameter_tuples}
        output_parameter_tuples: list[
            tuple[OutputParameter, WorkflowRunOutputParameter]
        ] = await self.get_output_parameter_workflow_run_output_parameter_tuples(
            workflow_id=workflow_run.workflow_id, workflow_run_id=workflow_run_id
        )

        outputs = None
        EXTRACTED_INFORMATION_KEY = "extracted_information"
        if output_parameter_tuples:
            outputs = {output_parameter.key: output.value for output_parameter, output in output_parameter_tuples}
            extracted_information: list[Any] = []
            for _, output in output_parameter_tuples:
                if output.value is not None:
                    extracted_information.extend(WorkflowService._collect_extracted_information(output.value))
            outputs[EXTRACTED_INFORMATION_KEY] = extracted_information
            # Refresh any expired presigned screenshot URLs in the outputs
            outputs = await self._refresh_output_screenshot_urls(
                outputs, organization_id=organization_id, workflow_run_id=workflow_run_id
            )

        errors: list[dict[str, Any]] = []
        for task in workflow_run_tasks:
            errors.extend(task.errors)

        total_steps = None
        total_cost = None
        if include_step_count or include_cost:
            workflow_run_steps = await app.DATABASE.get_steps_by_task_ids(
                task_ids=[task.task_id for task in workflow_run_tasks], organization_id=organization_id
            )
            total_steps = len(workflow_run_steps)

            if include_cost:
                workflow_run_blocks = await app.DATABASE.get_workflow_run_blocks(
                    workflow_run_id=workflow_run_id, organization_id=organization_id
                )
                text_prompt_blocks = [
                    block for block in workflow_run_blocks if block.block_type == BlockType.TEXT_PROMPT
                ]
                # TODO: This is a temporary cost calculation. We need to implement a more accurate cost calculation.
                # successful steps are the ones that have a status of completed and the total count of unique step.order
                successful_steps = [step for step in workflow_run_steps if step.status == StepStatus.completed]
                total_cost = 0.05 * (len(successful_steps) + len(text_prompt_blocks))
        return WorkflowRunResponseBase(
            workflow_id=workflow.workflow_permanent_id,
            workflow_run_id=workflow_run_id,
            status=workflow_run.status,
            failure_reason=workflow_run.failure_reason,
            proxy_location=workflow_run.proxy_location,
            webhook_callback_url=workflow_run.webhook_callback_url,
            webhook_failure_reason=workflow_run.webhook_failure_reason,
            totp_verification_url=workflow_run.totp_verification_url,
            totp_identifier=workflow_run.totp_identifier,
            extra_http_headers=workflow_run.extra_http_headers,
            queued_at=workflow_run.queued_at,
            started_at=workflow_run.started_at,
            finished_at=workflow_run.finished_at,
            created_at=workflow_run.created_at,
            modified_at=workflow_run.modified_at,
            parameters=parameters_with_value,
            screenshot_urls=screenshot_urls,
            recording_url=recording_url,
            downloaded_files=downloaded_files,
            downloaded_file_urls=downloaded_file_urls,
            outputs=outputs,
            total_steps=total_steps,
            total_cost=total_cost,
            workflow_title=workflow.title,
            browser_session_id=workflow_run.browser_session_id,
            browser_profile_id=workflow_run.browser_profile_id,
            max_screenshot_scrolls=workflow_run.max_screenshot_scrolls,
            task_v2=task_v2,
            browser_address=workflow_run.browser_address,
            script_run=workflow_run.script_run,
            errors=errors,
        )

    async def clean_up_workflow(
        self,
        workflow: Workflow,
        workflow_run: WorkflowRun,
        api_key: str | None = None,
        close_browser_on_completion: bool = True,
        need_call_webhook: bool = True,
        browser_session_id: str | None = None,
    ) -> None:
        analytics.capture("skyvern-oss-agent-workflow-status", {"status": workflow_run.status})
        tasks = await self.get_tasks_by_workflow_run_id(workflow_run.workflow_run_id)
        all_workflow_task_ids = [task.task_id for task in tasks]
        close_browser_on_completion = (
            close_browser_on_completion and browser_session_id is None and not workflow_run.browser_address
        )
        browser_state = await app.BROWSER_MANAGER.cleanup_for_workflow_run(
            workflow_run.workflow_run_id,
            all_workflow_task_ids,
            close_browser_on_completion=close_browser_on_completion,
            browser_session_id=browser_session_id,
            organization_id=workflow_run.organization_id,
        )
        if browser_state:
            await self.persist_video_data(browser_state, workflow, workflow_run)
            if tasks:
                await self.persist_debug_artifacts(browser_state, tasks[-1], workflow, workflow_run)
            # Skip workflow-scoped session save when using browser_profile_id to avoid conflicts
            # (profile persistence is handled separately via the profile storage)
            if (
                workflow.persist_browser_session
                and browser_state.browser_artifacts.browser_session_dir
                and not workflow_run.browser_profile_id
            ):
                await app.STORAGE.store_browser_session(
                    workflow_run.organization_id,
                    workflow.workflow_permanent_id,
                    browser_state.browser_artifacts.browser_session_dir,
                )
                LOG.info("Persisted browser session for workflow run", workflow_run_id=workflow_run.workflow_run_id)

        await app.ARTIFACT_MANAGER.wait_for_upload_aiotasks(all_workflow_task_ids)

        try:
            async with asyncio.timeout(SAVE_DOWNLOADED_FILES_TIMEOUT):
                context = skyvern_context.current()
                await app.STORAGE.save_downloaded_files(
                    organization_id=workflow_run.organization_id,
                    run_id=context.run_id if context and context.run_id else workflow_run.workflow_run_id,
                )
        except asyncio.TimeoutError:
            LOG.warning(
                "Timeout to save downloaded files",
                workflow_run_id=workflow_run.workflow_run_id,
            )
        except Exception:
            LOG.warning(
                "Failed to save downloaded files",
                exc_info=True,
                workflow_run_id=workflow_run.workflow_run_id,
            )

        if not need_call_webhook:
            return

        await self.execute_workflow_webhook(workflow_run, api_key)

    async def execute_workflow_webhook(
        self,
        workflow_run: WorkflowRun,
        api_key: str | None = None,
    ) -> None:
        workflow_id = workflow_run.workflow_id
        workflow_run_status_response = await self.build_workflow_run_status_response(
            workflow_permanent_id=workflow_run.workflow_permanent_id,
            workflow_run_id=workflow_run.workflow_run_id,
            organization_id=workflow_run.organization_id,
            include_step_count=True,
        )
        if not workflow_run.webhook_callback_url:
            LOG.warning(
                "Workflow has no webhook callback url. Not sending workflow response",
                workflow_id=workflow_id,
                workflow_run_id=workflow_run.workflow_run_id,
            )
            return

        if not api_key:
            LOG.warning(
                "Request has no api key. Not sending workflow response",
                workflow_id=workflow_id,
                workflow_run_id=workflow_run.workflow_run_id,
            )
            return

        # build new schema for backward compatible webhook payload
        app_url = f"{settings.SKYVERN_APP_URL.rstrip('/')}/runs/{workflow_run.workflow_run_id}"

        workflow_run_response = WorkflowRunResponse(
            run_id=workflow_run.workflow_run_id,
            run_type=RunType.workflow_run,
            status=RunStatus(workflow_run_status_response.status),
            output=workflow_run_status_response.outputs,
            downloaded_files=workflow_run_status_response.downloaded_files,
            recording_url=workflow_run_status_response.recording_url,
            screenshot_urls=workflow_run_status_response.screenshot_urls,
            failure_reason=workflow_run_status_response.failure_reason,
            app_url=app_url,
            script_run=workflow_run_status_response.script_run,
            created_at=workflow_run_status_response.created_at,
            modified_at=workflow_run_status_response.modified_at,
            run_request=WorkflowRunRequest(
                workflow_id=workflow_run.workflow_permanent_id,
                title=workflow_run_status_response.workflow_title,
                parameters=workflow_run_status_response.parameters,
                proxy_location=workflow_run.proxy_location,
                webhook_url=workflow_run.webhook_callback_url or None,
                totp_url=workflow_run.totp_verification_url or None,
                totp_identifier=workflow_run.totp_identifier,
            ),
            errors=workflow_run_status_response.errors,
            step_count=workflow_run_status_response.total_steps,
        )
        payload_dict: dict = json.loads(workflow_run_status_response.model_dump_json())
        workflow_run_response_dict = json.loads(workflow_run_response.model_dump_json())
        payload_dict.update(workflow_run_response_dict)
        signed_data = generate_skyvern_webhook_signature(
            payload=payload_dict,
            api_key=api_key,
        )
        LOG.info(
            "Sending webhook run status to webhook callback url",
            workflow_id=workflow_id,
            workflow_run_id=workflow_run.workflow_run_id,
            webhook_callback_url=workflow_run.webhook_callback_url,
            payload=signed_data.payload_for_log,
            headers=signed_data.headers,
        )
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    url=workflow_run.webhook_callback_url,
                    data=signed_data.signed_payload,
                    headers=signed_data.headers,
                    timeout=httpx.Timeout(30.0),
                )
            if resp.status_code >= 200 and resp.status_code < 300:
                LOG.info(
                    "Webhook sent successfully",
                    workflow_id=workflow_id,
                    workflow_run_id=workflow_run.workflow_run_id,
                    resp_code=resp.status_code,
                    resp_text=resp.text,
                )
                await app.DATABASE.update_workflow_run(
                    workflow_run_id=workflow_run.workflow_run_id,
                    webhook_failure_reason="",
                )
            else:
                LOG.info(
                    "Webhook failed",
                    workflow_id=workflow_id,
                    workflow_run_id=workflow_run.workflow_run_id,
                    webhook_data=signed_data.payload_for_log,
                    resp=resp,
                    resp_code=resp.status_code,
                    resp_text=resp.text,
                )
                await app.DATABASE.update_workflow_run(
                    workflow_run_id=workflow_run.workflow_run_id,
                    webhook_failure_reason=f"Webhook failed with status code {resp.status_code}, error message: {resp.text}",
                )
        except Exception as e:
            raise FailedToSendWebhook(
                workflow_id=workflow_id,
                workflow_run_id=workflow_run.workflow_run_id,
            ) from e

    async def persist_video_data(
        self, browser_state: BrowserState, workflow: Workflow, workflow_run: WorkflowRun
    ) -> None:
        # Create recording artifact after closing the browser, so we can get an accurate recording
        video_artifacts = await app.BROWSER_MANAGER.get_video_artifacts(
            workflow_id=workflow.workflow_id,
            workflow_run_id=workflow_run.workflow_run_id,
            browser_state=browser_state,
        )
        LOG.debug("Persisting video data", number_of_video_artifacts=len(video_artifacts))
        for video_artifact in video_artifacts:
            await app.ARTIFACT_MANAGER.update_artifact_data(
                artifact_id=video_artifact.video_artifact_id,
                organization_id=workflow_run.organization_id,
                data=video_artifact.video_data,
            )

    async def persist_har_data(
        self,
        browser_state: BrowserState,
        last_step: Step,
        workflow: Workflow,
        workflow_run: WorkflowRun,
    ) -> None:
        har_data = await app.BROWSER_MANAGER.get_har_data(
            workflow_id=workflow.workflow_id,
            workflow_run_id=workflow_run.workflow_run_id,
            browser_state=browser_state,
        )
        LOG.debug("Persisting har data", har_size=len(har_data))
        if har_data:
            await app.ARTIFACT_MANAGER.create_artifact(
                step=last_step,
                artifact_type=ArtifactType.HAR,
                data=har_data,
            )

    async def persist_browser_console_log(
        self,
        browser_state: BrowserState,
        last_step: Step,
        workflow: Workflow,
        workflow_run: WorkflowRun,
    ) -> None:
        browser_log = await app.BROWSER_MANAGER.get_browser_console_log(
            workflow_id=workflow.workflow_id,
            workflow_run_id=workflow_run.workflow_run_id,
            browser_state=browser_state,
        )
        LOG.debug("Persisting browser log", browser_log_size=len(browser_log))
        if browser_log:
            await app.ARTIFACT_MANAGER.create_artifact(
                step=last_step,
                artifact_type=ArtifactType.BROWSER_CONSOLE_LOG,
                data=browser_log,
            )

    async def persist_tracing_data(
        self, browser_state: BrowserState, last_step: Step, workflow_run: WorkflowRun
    ) -> None:
        if browser_state.browser_context is None or browser_state.browser_artifacts.traces_dir is None:
            return

        trace_path = f"{browser_state.browser_artifacts.traces_dir}/{workflow_run.workflow_run_id}.zip"
        await app.ARTIFACT_MANAGER.create_artifact(step=last_step, artifact_type=ArtifactType.TRACE, path=trace_path)

    async def persist_debug_artifacts(
        self,
        browser_state: BrowserState,
        last_task: Task,
        workflow: Workflow,
        workflow_run: WorkflowRun,
    ) -> None:
        last_step = await app.DATABASE.get_latest_step(
            task_id=last_task.task_id, organization_id=last_task.organization_id
        )
        if not last_step:
            return

        await self.persist_browser_console_log(browser_state, last_step, workflow, workflow_run)
        await self.persist_har_data(browser_state, last_step, workflow, workflow_run)
        await self.persist_tracing_data(browser_state, last_step, workflow_run)

    async def make_workflow_definition(
        self,
        workflow_id: str,
        workflow_definition_yaml: WorkflowDefinitionYAML,
    ) -> WorkflowDefinition:
        workflow_definition = convert_workflow_definition(
            workflow_definition_yaml=workflow_definition_yaml,
            workflow_id=workflow_id,
        )

        await app.DATABASE.save_workflow_definition_parameters(workflow_definition.parameters)

        return workflow_definition

    async def create_workflow_from_request(
        self,
        organization: Organization,
        request: WorkflowCreateYAMLRequest,
        workflow_permanent_id: str | None = None,
        delete_script: bool = True,
    ) -> Workflow:
        organization_id = organization.organization_id

        # Generate meaningful title if using default and has blocks
        title = request.title
        if title == DEFAULT_WORKFLOW_TITLE and request.workflow_definition.blocks:
            generated_title = await generate_workflow_title(
                organization_id=organization_id,
                blocks=request.workflow_definition.blocks,
            )
            if generated_title:
                title = generated_title
                LOG.info(
                    "Generated workflow title",
                    organization_id=organization_id,
                    generated_title=title,
                )

        LOG.info(
            "Creating workflow from request",
            organization_id=organization_id,
            title=title,
        )
        new_workflow_id: str | None = None

        if workflow_permanent_id:
            # Would return 404: WorkflowNotFound to the client if wpid does not match the organization
            existing_latest_workflow = await self.get_workflow_by_permanent_id(
                workflow_permanent_id=workflow_permanent_id,
                organization_id=organization_id,
                exclude_deleted=False,
            )
        else:
            existing_latest_workflow = None

        try:
            if existing_latest_workflow:
                existing_version = existing_latest_workflow.version

                # NOTE: it's only potential, as it may be immediately deleted!
                potential_workflow = await self.create_workflow(
                    title=title,
                    workflow_definition=WorkflowDefinition(parameters=[], blocks=[]),
                    description=request.description,
                    organization_id=organization_id,
                    proxy_location=request.proxy_location,
                    webhook_callback_url=request.webhook_callback_url,
                    totp_verification_url=request.totp_verification_url,
                    totp_identifier=request.totp_identifier,
                    persist_browser_session=request.persist_browser_session,
                    model=request.model,
                    max_screenshot_scrolling_times=request.max_screenshot_scrolls,
                    extra_http_headers=request.extra_http_headers,
                    workflow_permanent_id=existing_latest_workflow.workflow_permanent_id,
                    version=existing_version + 1,
                    is_saved_task=request.is_saved_task,
                    status=request.status,
                    run_with=request.run_with,
                    cache_key=request.cache_key,
                    ai_fallback=request.ai_fallback,
                    run_sequentially=request.run_sequentially,
                    sequential_key=request.sequential_key,
                    folder_id=existing_latest_workflow.folder_id,
                )
            else:
                # NOTE: it's only potential, as it may be immediately deleted!
                potential_workflow = await self.create_workflow(
                    title=title,
                    workflow_definition=WorkflowDefinition(parameters=[], blocks=[]),
                    description=request.description,
                    organization_id=organization_id,
                    proxy_location=request.proxy_location,
                    webhook_callback_url=request.webhook_callback_url,
                    totp_verification_url=request.totp_verification_url,
                    totp_identifier=request.totp_identifier,
                    persist_browser_session=request.persist_browser_session,
                    model=request.model,
                    max_screenshot_scrolling_times=request.max_screenshot_scrolls,
                    extra_http_headers=request.extra_http_headers,
                    is_saved_task=request.is_saved_task,
                    status=request.status,
                    run_with=request.run_with,
                    cache_key=request.cache_key,
                    ai_fallback=request.ai_fallback,
                    run_sequentially=request.run_sequentially,
                    sequential_key=request.sequential_key,
                    folder_id=request.folder_id,
                )
            # Keeping track of the new workflow id to delete it if an error occurs during the creation process
            new_workflow_id = potential_workflow.workflow_id

            workflow_definition = await self.make_workflow_definition(
                potential_workflow.workflow_id,
                request.workflow_definition,
            )

            updated_workflow = await self.update_workflow_definition(
                workflow_id=potential_workflow.workflow_id,
                organization_id=organization_id,
                title=title,
                description=request.description,
                workflow_definition=workflow_definition,
            )

            await self.maybe_delete_cached_code(
                updated_workflow,
                workflow_definition=workflow_definition,
                organization_id=organization_id,
                delete_script=delete_script,
            )

            return updated_workflow
        except SkyvernHTTPException:
            # Bubble up well-formed client errors (e.g. WorkflowNotFound 404)
            # so they are not wrapped in a 500 by the caller.
            if new_workflow_id:
                await self.delete_workflow_by_id(workflow_id=new_workflow_id, organization_id=organization_id)
            raise
        except Exception as e:
            if new_workflow_id:
                LOG.error(
                    f"Failed to create workflow from request, deleting workflow {new_workflow_id}",
                    organization_id=organization_id,
                )
                await self.delete_workflow_by_id(workflow_id=new_workflow_id, organization_id=organization_id)
            else:
                LOG.exception(f"Failed to create workflow from request, title: {title}")
            raise e

    @staticmethod
    async def create_output_parameter_for_block(workflow_id: str, block_yaml: BLOCK_YAML_TYPES) -> OutputParameter:
        output_parameter_key = f"{block_yaml.label}_output"
        return await app.DATABASE.create_output_parameter(
            workflow_id=workflow_id,
            key=output_parameter_key,
            description=f"Output parameter for block {block_yaml.label}",
        )

    async def create_empty_workflow(
        self,
        organization: Organization,
        title: str,
        proxy_location: ProxyLocationInput = None,
        max_screenshot_scrolling_times: int | None = None,
        extra_http_headers: dict[str, str] | None = None,
        run_with: str | None = None,
        status: WorkflowStatus = WorkflowStatus.published,
    ) -> Workflow:
        """
        Create a blank workflow with no blocks
        """
        # create a new workflow
        workflow_create_request = WorkflowCreateYAMLRequest(
            title=title,
            workflow_definition=WorkflowDefinitionYAML(
                parameters=[],
                blocks=[],
            ),
            proxy_location=proxy_location,
            status=status,
            max_screenshot_scrolls=max_screenshot_scrolling_times,
            extra_http_headers=extra_http_headers,
            run_with=run_with,
        )
        return await app.WORKFLOW_SERVICE.create_workflow_from_request(
            organization=organization,
            request=workflow_create_request,
        )

    async def get_workflow_run_timeline(
        self,
        workflow_run_id: str,
        organization_id: str | None = None,
    ) -> list[WorkflowRunTimeline]:
        """
        build the tree structure of the workflow run timeline
        """
        workflow_run_blocks = await app.DATABASE.get_workflow_run_blocks(
            workflow_run_id=workflow_run_id,
            organization_id=organization_id,
        )
        # get all the actions for all workflow run blocks
        task_ids = [block.task_id for block in workflow_run_blocks if block.task_id]
        task_id_to_block: dict[str, WorkflowRunBlock] = {
            block.task_id: block for block in workflow_run_blocks if block.task_id
        }
        actions = await app.DATABASE.get_tasks_actions(task_ids=task_ids, organization_id=organization_id)
        for action in actions:
            if not action.task_id:
                continue
            task_block = task_id_to_block[action.task_id]
            task_block.actions.append(action)

        result = []
        block_map: dict[str, WorkflowRunTimeline] = {}
        counter = 0
        while workflow_run_blocks:
            counter += 1
            block = workflow_run_blocks.pop(0)
            workflow_run_timeline = WorkflowRunTimeline(
                type=WorkflowRunTimelineType.block,
                block=block,
                created_at=block.created_at,
                modified_at=block.modified_at,
            )
            if block.parent_workflow_run_block_id:
                if block.parent_workflow_run_block_id in block_map:
                    block_map[block.parent_workflow_run_block_id].children.append(workflow_run_timeline)
                    block_map[block.workflow_run_block_id] = workflow_run_timeline
                else:
                    # put the block back to the queue
                    workflow_run_blocks.append(block)
            else:
                result.append(workflow_run_timeline)
                block_map[block.workflow_run_block_id] = workflow_run_timeline

            if counter > 1000:
                LOG.error("Too many blocks in the workflow run", workflow_run_id=workflow_run_id)
                break

        return result

    async def generate_script_if_needed(
        self,
        workflow: Workflow,
        workflow_run: WorkflowRun,
        block_labels: list[str] | None = None,
        blocks_to_update: set[str] | None = None,
        finalize: bool = False,
        has_conditionals: bool | None = None,
    ) -> None:
        """
        Generate or regenerate workflow script if needed.

        Args:
            workflow: The workflow definition
            workflow_run: The workflow run instance
            block_labels: Optional list of specific block labels to generate
            blocks_to_update: Set of block labels that need regeneration
            finalize: If True, check if any actions were skipped during script generation
                     due to missing data (race condition). Only regenerate if needed.
                     This fixes SKY-7653 while avoiding unnecessary regeneration costs.
            has_conditionals: Whether the workflow has conditional blocks. If None, will be computed.
        """
        code_gen = workflow_run.code_gen
        blocks_to_update = set(blocks_to_update or [])

        # When finalizing, only regenerate if script generation had incomplete actions.
        # This addresses the race condition (SKY-7653) while avoiding unnecessary
        # regeneration costs when the script is already complete.
        if finalize:
            current_context = skyvern_context.current()
            if current_context and current_context.script_gen_had_incomplete_actions:
                LOG.info(
                    "Finalize: regenerating script due to incomplete actions during generation",
                    workflow_run_id=workflow_run.workflow_run_id,
                )
                task_block_labels = {
                    block.label
                    for block in workflow.workflow_definition.blocks
                    if block.label and block.block_type in BLOCK_TYPES_THAT_SHOULD_BE_CACHED
                }
                blocks_to_update.update(task_block_labels)
                blocks_to_update.add(settings.WORKFLOW_START_BLOCK_LABEL)
                # Reset flag after triggering regeneration to prevent stale state
                current_context.script_gen_had_incomplete_actions = False
            else:
                LOG.debug(
                    "Finalize: skipping regeneration - no incomplete actions detected",
                    workflow_run_id=workflow_run.workflow_run_id,
                )

        LOG.info(
            "Generate script?",
            block_labels=block_labels,
            code_gen=code_gen,
            workflow_run_id=workflow_run.workflow_run_id,
            blocks_to_update=list(blocks_to_update),
        )

        if block_labels and not code_gen:
            # Do not generate script if block_labels is provided, and an explicit code_gen
            # request is not made
            return None

        existing_script, rendered_cache_key_value = await workflow_script_service.get_workflow_script(
            workflow,
            workflow_run,
            block_labels,
        )

        if existing_script:
            cached_block_labels: set[str] = set()
            script_blocks = await app.DATABASE.get_script_blocks_by_script_revision_id(
                script_revision_id=existing_script.script_revision_id,
                organization_id=workflow.organization_id,
            )
            for script_block in script_blocks:
                if script_block.script_block_label:
                    cached_block_labels.add(script_block.script_block_label)

            should_cache_block_labels = {
                block.label
                for block in workflow.workflow_definition.blocks
                if block.label and block.block_type in BLOCK_TYPES_THAT_SHOULD_BE_CACHED
            }
            should_cache_block_labels.add(settings.WORKFLOW_START_BLOCK_LABEL)
            cached_block_labels.add(settings.WORKFLOW_START_BLOCK_LABEL)

            # For workflows with conditional blocks, "missing" labels from unexecuted branches
            # should NOT trigger regeneration. They will be cached when those branches execute.
            # This prevents the bug where every run triggers unnecessary regeneration because
            # blocks from unexecuted branches are always "missing".
            if has_conditionals is None:
                has_conditionals = workflow_script_service.workflow_has_conditionals(workflow)

            if cached_block_labels != should_cache_block_labels:
                missing_labels = should_cache_block_labels - cached_block_labels
                if missing_labels and not has_conditionals:
                    # Only add missing labels for workflows WITHOUT conditionals.
                    # For workflows WITH conditionals, missing labels are expected (unexecuted branches).
                    blocks_to_update.update(missing_labels)
                    # Always rebuild the orchestrator if the definition changed
                    blocks_to_update.add(settings.WORKFLOW_START_BLOCK_LABEL)
                elif missing_labels and has_conditionals:
                    LOG.debug(
                        "Skipping regeneration for missing labels in workflow with conditionals",
                        workflow_id=workflow.workflow_id,
                        workflow_run_id=workflow_run.workflow_run_id,
                        missing_labels=list(missing_labels),
                    )

            should_regenerate = bool(blocks_to_update) or bool(code_gen)

            if not should_regenerate:
                LOG.info(
                    "Workflow script already up to date; skipping regeneration",
                    workflow_id=workflow.workflow_id,
                    workflow_run_id=workflow_run.workflow_run_id,
                    cache_key_value=rendered_cache_key_value,
                    script_id=existing_script.script_id,
                    script_revision_id=existing_script.script_revision_id,
                    run_with=workflow_run.run_with,
                )
                return

            async def _regenerate_script() -> None:
                """Delete old script and generate new one.

                Uses double-check pattern: re-verify regeneration is needed after acquiring lock
                to handle race conditions where another process regenerated while we waited.
                """
                # Double-check: another process may have regenerated while we waited for lock
                fresh_script = await workflow_script_service.get_workflow_script_by_cache_key_value(
                    organization_id=workflow.organization_id,
                    workflow_permanent_id=workflow.workflow_permanent_id,
                    cache_key_value=rendered_cache_key_value,
                    statuses=[ScriptStatus.published],
                    use_cache=False,
                )
                if fresh_script and fresh_script.script_id != existing_script.script_id:
                    LOG.info(
                        "Script already regenerated by another process, skipping",
                        workflow_id=workflow.workflow_id,
                        workflow_run_id=workflow_run.workflow_run_id,
                        cache_key_value=rendered_cache_key_value,
                        existing_script_id=existing_script.script_id,
                        fresh_script_id=fresh_script.script_id,
                    )
                    return

                LOG.info(
                    "deleting old workflow script and generating new script",
                    workflow_id=workflow.workflow_id,
                    workflow_run_id=workflow_run.workflow_run_id,
                    cache_key_value=rendered_cache_key_value,
                    script_id=existing_script.script_id,
                    script_revision_id=existing_script.script_revision_id,
                    run_with=workflow_run.run_with,
                    blocks_to_update=list(blocks_to_update),
                    code_gen=code_gen,
                )

                await app.DATABASE.delete_workflow_scripts_by_permanent_id(
                    organization_id=workflow.organization_id,
                    workflow_permanent_id=workflow.workflow_permanent_id,
                    script_ids=[existing_script.script_id],
                )

                regenerated_script = await app.DATABASE.create_script(
                    organization_id=workflow.organization_id,
                    run_id=workflow_run.workflow_run_id,
                )

                await workflow_script_service.generate_workflow_script(
                    workflow_run=workflow_run,
                    workflow=workflow,
                    script=regenerated_script,
                    rendered_cache_key_value=rendered_cache_key_value,
                    cached_script=existing_script,
                    updated_block_labels=blocks_to_update,
                )
                aio_task_primary_key = f"{regenerated_script.script_id}_{regenerated_script.version}"
                if aio_task_primary_key in app.ARTIFACT_MANAGER.upload_aiotasks_map:
                    aio_tasks = app.ARTIFACT_MANAGER.upload_aiotasks_map[aio_task_primary_key]
                    if aio_tasks:
                        await asyncio.gather(*aio_tasks)
                    else:
                        LOG.warning(
                            "No upload aio tasks found for regenerated script",
                            script_id=regenerated_script.script_id,
                            version=regenerated_script.version,
                        )

            # Use distributed redis lock to prevent concurrent regenerations
            cache = CacheFactory.get_cache()
            lock = None
            if cache is not None:
                try:
                    digest = sha256(rendered_cache_key_value.encode("utf-8")).hexdigest()
                    lock_name = f"workflow_script_regen:{workflow.workflow_permanent_id}:{digest}"
                    # blocking_timeout=60s to wait for lock, timeout=60s for lock TTL (per wintonzheng: p99=44s)
                    lock = cache.get_lock(lock_name, blocking_timeout=60, timeout=60)
                except AttributeError:
                    LOG.debug("Cache doesn't support locking, proceeding without lock")

            if lock is not None:
                try:
                    async with lock:
                        await _regenerate_script()
                except LockError as exc:
                    # Lock acquisition failed (e.g., another process holds the lock, timeout)
                    # Skip regeneration and trust the lock holder to complete the work.
                    # The double-check pattern in _regenerate_script() will handle it on the next call.
                    LOG.info(
                        "Skipping regeneration - lock acquisition failed, another process may be regenerating",
                        workflow_id=workflow.workflow_id,
                        workflow_permanent_id=workflow.workflow_permanent_id,
                        error=str(exc),
                    )
            else:
                # No Redis/cache available - proceed without lock (graceful degradation for OSS)
                await _regenerate_script()
            return

        created_script = await app.DATABASE.create_script(
            organization_id=workflow.organization_id,
            run_id=workflow_run.workflow_run_id,
        )

        await workflow_script_service.generate_workflow_script(
            workflow_run=workflow_run,
            workflow=workflow,
            script=created_script,
            rendered_cache_key_value=rendered_cache_key_value,
            cached_script=None,
            updated_block_labels=None,
        )
        aio_task_primary_key = f"{created_script.script_id}_{created_script.version}"
        if aio_task_primary_key in app.ARTIFACT_MANAGER.upload_aiotasks_map:
            aio_tasks = app.ARTIFACT_MANAGER.upload_aiotasks_map[aio_task_primary_key]
            if aio_tasks:
                await asyncio.gather(*aio_tasks)
            else:
                LOG.warning(
                    "No upload aio tasks found for script",
                    script_id=created_script.script_id,
                    version=created_script.version,
                )

    def should_run_script(
        self,
        workflow: Workflow,
        workflow_run: WorkflowRun,
    ) -> bool:
        if workflow_run.run_with == "code":
            return True
        if workflow_run.run_with == "agent":
            return False
        if workflow.run_with == "code":
            return True
        return False
