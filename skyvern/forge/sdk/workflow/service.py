import asyncio
import copy
import importlib.util
import json
import os
import random
import textwrap
import time
import uuid
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any, Literal, cast

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
    BlockedHost,
    BlockNotFound,
    BrowserProfileNotFound,
    BrowserSessionNotFound,
    BrowserSessionNotRenewable,
    DisabledBlockExecutionError,
    InvalidCredentialId,
    MissingValueForParameter,
    ScriptTerminationException,
    SkyvernException,
    SkyvernHTTPException,
    WorkflowNotFound,
    WorkflowNotFoundForWorkflowRun,
    WorkflowRunNotFound,
    WorkflowRunParameterPersistenceError,
    get_user_facing_exception_message,
)
from skyvern.forge import app
from skyvern.forge.failure_classifier import classify_from_failure_reason
from skyvern.forge.prompts import prompt_engine
from skyvern.forge.sdk.artifact.models import Artifact, ArtifactType
from skyvern.forge.sdk.artifact.storage.base import _file_infos_from_download_artifacts
from skyvern.forge.sdk.cache import extraction_cache
from skyvern.forge.sdk.cache.factory import CacheFactory
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.security import generate_skyvern_webhook_signature
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.db._sentinels import _UNSET
from skyvern.forge.sdk.db.enums import OrganizationAuthTokenType, WorkflowRunTriggerType
from skyvern.forge.sdk.db.id import generate_output_parameter_id, generate_workflow_parameter_id
from skyvern.forge.sdk.enterprise_features import collect_enterprise_gated_run_features
from skyvern.forge.sdk.experimentation.enrich_tree import resolve_enrich_tree_for_context
from skyvern.forge.sdk.models import Step, StepStatus
from skyvern.forge.sdk.schemas.browser_profiles import BrowserProfile
from skyvern.forge.sdk.schemas.files import FileInfo
from skyvern.forge.sdk.schemas.organizations import Organization
from skyvern.forge.sdk.schemas.persistent_browser_sessions import (
    FORCED_WORKFLOW_SESSION_RUNNABLE_TYPE,
    PersistentBrowserSession,
)
from skyvern.forge.sdk.schemas.tasks import Task, TaskStatus
from skyvern.forge.sdk.schemas.workflow_runs import WorkflowRunBlock, WorkflowRunTimeline, WorkflowRunTimelineType
from skyvern.forge.sdk.submission import shadow as submission_shadow
from skyvern.forge.sdk.trace import traced
from skyvern.forge.sdk.workflow.browser_profile_key import (
    build_browser_profile_key_digest,
    build_workflow_browser_session_storage_key,
    render_browser_profile_key,
)
from skyvern.forge.sdk.workflow.credential_selection import (
    VALID_SELECTION_STRATEGIES,
    normalize_selection_strategy,
    select_credential_for_run,
)
from skyvern.forge.sdk.workflow.exceptions import (
    InvalidWorkflowDefinition,
    WorkflowVersionConflict,
)
from skyvern.forge.sdk.workflow.models.block import (
    BaseTaskBlock,
    Block,
    BlockTypeVar,
    ConditionalBlock,
    ExtractionBlock,
    FileParserBlock,
    ForLoopBlock,
    LoginBlock,
    NavigationBlock,
    PdfFillBlock,
    PDFParserBlock,
    SplitPdfBlock,
    TaskV2Block,
    TextPromptBlock,
    WhileLoopBlock,
    WorkflowTriggerBlock,
    compute_conditional_scopes,
    get_all_blocks,
    resolve_conditional_merge_edges,
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
from skyvern.forge.sdk.workflow.models.run_limits import get_effective_workflow_run_max_elapsed_time_minutes
from skyvern.forge.sdk.workflow.models.tags import CallerType, TagSource, TagWriteContext
from skyvern.forge.sdk.workflow.models.workflow import (
    Workflow,
    WorkflowDefinition,
    WorkflowRequestBody,
    WorkflowRun,
    WorkflowRunOutputParameter,
    WorkflowRunParameter,
    WorkflowRunResponseBase,
    WorkflowRunStatus,
    is_adaptive_caching,
)
from skyvern.forge.sdk.workflow.status_mapping import (
    BLOCK_STATUS_MAP,
    NONFINAL_BLOCK_STATUSES,
    STEP_STATUS_MAP,
    TASK_STATUS_MAP,
)
from skyvern.forge.sdk.workflow.workflow_definition_converter import convert_workflow_definition
from skyvern.schemas.proxy_pinning import (
    derive_proxy_session_id,
    redact_proxy_session_id,
    should_generate_proxy_session_id,
)
from skyvern.schemas.run_enums import RunEngine
from skyvern.schemas.runs import (
    ProxyLocation,
    ProxyLocationInput,
    RunStatus,
    RunType,
    WorkflowRunRequest,
    WorkflowRunResponse,
)
from skyvern.schemas.scripts import Script, ScriptBlock, ScriptFallbackEpisode, ScriptStatus, WorkflowScript
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
from skyvern.services.script_review_cap import (
    check_and_increment_cap_v3,
    increment_script_review_counter_v2,
    is_script_review_cap_exceeded_v2,
    is_script_review_cap_exceeded_v3,
    try_increment_script_review_counter_v3,
    v2_script_review_cap_key,
    v3_script_review_cap_key,
)
from skyvern.services.script_reviewer_v3.cohort import is_v3_cohort
from skyvern.services.script_reviewer_v3.postrun import v3_review_post_run
from skyvern.services.webhook_delivery import PreparedWorkflowWebhook, deliver_webhook_with_retries
from skyvern.utils.css_selector import build_action_summaries_with_timing  # shared with script_service
from skyvern.utils.secret_headers import merge_masked_headers
from skyvern.utils.url_validators import validate_url as validate_url_with_blocked_host_check
from skyvern.webeye.browser_state import BrowserState
from skyvern.webeye.session_cookies import persist_session_cookies

LOG = structlog.get_logger()

DEFAULT_FIRST_BLOCK_LABEL = "block_1"
DEFAULT_WORKFLOW_TITLE = "New Workflow"
MANAGED_BROWSER_PROFILE_NAME_MAX_LENGTH = 120
MANAGED_BROWSER_PROFILE_KEY_MAX_LENGTH = 40
DETECTED_PLATFORM_RUN_TAG_KEY = "skyvern.platform"
TRIGGER_RUN_TAG_KEY = "skyvern.trigger"
TARGET_DOMAIN_RUN_TAG_KEY = "skyvern.target_domain"
CREATION_RUN_TAG_CALLER_ID = "system:creation-tagging"
COMPLETION_RUN_TAG_CALLER_ID = "system:completion-tagging"

# Empirical S3 upload SLA; no start buffer (back-to-back leakage is worse than late uploads to the next run).
RECORDING_WINDOW_END_BUFFER = timedelta(minutes=15)
# Skip post-run work when only a sub-millisecond budget remains; asyncio.timeout would fire on the first await.
POST_RUN_TIMEOUT_EXHAUSTED_THRESHOLD_SECONDS = 0.001

# Bound pre-finalization write-back so a stuck profile upload cannot leave the run in a non-terminal state.
BROWSER_SESSION_WRITE_BACK_TIMEOUT = SAVE_DOWNLOADED_FILES_TIMEOUT

# Short lifespan for auto-provisioned CodeBlock sessions; the renewal loop extends it while the
# run is active, so this only bounds how long a leaked session lingers if cleanup never runs.
CODE_BLOCK_SESSION_TIMEOUT_MINUTES = 20

# Structured warning emitted when a debug-session run's visible PBS profile is
# incompatible with the LoginBlock credential's saved profile. Observability
# dashboards key on these strings — do not rename without updating monitors.
DEBUG_SESSION_PROFILE_INCOMPATIBLE_CODE = "debug_session_profile_incompatible"
DEBUG_SESSION_PROFILE_REASON_NO_PROFILE = "pbs_no_profile"
DEBUG_SESSION_PROFILE_REASON_DIFFERENT = "pbs_different_profile"


@dataclass(frozen=True)
class DebugSessionProfileDecision:
    """Asymmetric decision for LoginBlock credential-profile flow.

    attach_browser_session_id: the PBS id to thread into
        BROWSER_MANAGER.get_or_create_for_workflow_run so the visible browser
        is the one the agent acts on. None for non-debug runs (preserve
        existing behavior — credential-profile launches a fresh browser).

    incompatible_reason: None when compatible / non-debug; otherwise the
        structured reason for the warning emit. Callers branch on this:
        None → take the existing skip-login fast path, set → emit the
        structured warning, attach the PBS, fall through to ordinary login.
    """

    attach_browser_session_id: str | None
    incompatible_reason: str | None


def _as_utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def _get_workflow_run_max_elapsed_timeout_seconds(workflow_run: WorkflowRun) -> float:
    effective_minutes = get_effective_workflow_run_max_elapsed_time_minutes(workflow_run.max_elapsed_time_minutes)
    started_at = _as_utc(workflow_run.started_at or workflow_run.created_at)
    elapsed_seconds = (datetime.now(UTC) - started_at).total_seconds()
    return max(0.0, effective_minutes * 60 - elapsed_seconds)


def _format_workflow_run_elapsed_timeout_failure_reason(effective_minutes: int) -> str:
    minute_label = "minute" if effective_minutes == 1 else "minutes"
    return f"Workflow run exceeded max elapsed runtime limit of {effective_minutes} {minute_label}."


def _require_elapsed_timeout_failure_reason(timeout_failure_reason: str | None) -> str:
    if timeout_failure_reason is None:
        LOG.error("timeout_failure_reason missing when workflow elapsed timeout is set")
        return "Workflow run exceeded max elapsed runtime limit."
    return timeout_failure_reason


def _collect_enterprise_gated_workflow_features(
    workflow: Workflow,
    *,
    block_labels: list[str] | None = None,
) -> set[str]:
    # workflow.model is editor metadata today; executable blocks read their own model fields.
    feature_names: set[str] = set()

    all_blocks = get_all_blocks(workflow.workflow_definition.blocks)
    if block_labels:
        blocks_by_label = {block.label: block for block in all_blocks}
        blocks_to_check = get_all_blocks([blocks_by_label[label] for label in block_labels if label in blocks_by_label])
    else:
        blocks_to_check = all_blocks

    for block in blocks_to_check:
        # TaskV2Block deliberately stays out of this set: its inherited model field is not consumed at runtime.
        engine: RunEngine | None = None
        task_block_uses_engine_and_model = False
        if isinstance(block, BaseTaskBlock) and block.block_type != BlockType.HUMAN_INTERACTION:
            task_block_uses_engine_and_model = True
            engine = block.engine
        block_uses_model = task_block_uses_engine_and_model or isinstance(
            block,
            (TextPromptBlock, FileParserBlock, PDFParserBlock, PdfFillBlock, SplitPdfBlock),
        )
        model = block.model if block_uses_model else None
        feature_names.update(
            collect_enterprise_gated_run_features(
                engine=engine,
                model=model,
            )
        )

    return feature_names


def _select_recording_urls_in_window(
    recordings: Sequence[FileInfo],
    lower_bound: datetime,
    upper_bound: datetime | None = None,
) -> list[str]:
    """Filter recordings to [lower_bound, upper_bound] by modified_at (UTC), sort oldest-first.

    ``upper_bound=None`` keeps every recording from ``lower_bound`` on. A persistent browser
    session finalizes its single continuous recording at session close — possibly hours after
    a run ends — so the bounded window misses it; the unbounded form is the fallback.
    """
    in_window: list[tuple[datetime, str]] = []
    for r in recordings:
        if r.modified_at is None:
            continue
        modified_utc = _as_utc(r.modified_at)
        if modified_utc >= lower_bound and (upper_bound is None or modified_utc <= upper_bound):
            in_window.append((modified_utc, r.url))
    in_window.sort(key=lambda pair: pair[0])
    return [url for _, url in in_window]


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
    BlockType.WHILE_LOOP,
}


def _collect_uncached_loop_children(
    block: ForLoopBlock | WhileLoopBlock,
    script_blocks_by_label: dict[str, object],
    blocks_to_update: set[str],
) -> None:
    """Recursively collect uncached cacheable children from nested loop blocks.

    Loop block children execute via block.py's execute_*_loop_helper(),
    bypassing _execute_single_block() where blocks_to_update tracking lives.
    This function walks all nesting levels so the script generator produces
    cached functions for deeply nested blocks (e.g., file_download inside
    a double-nested for-loop).
    """
    for child in block.loop_blocks:
        if (
            child.label
            and child.label not in script_blocks_by_label
            and child.block_type in BLOCK_TYPES_THAT_SHOULD_BE_CACHED
        ):
            blocks_to_update.add(child.label)
        # Recurse into nested loop blocks regardless of whether the loop
        # itself is cached — its children may not be.
        if isinstance(child, (ForLoopBlock, WhileLoopBlock)):
            _collect_uncached_loop_children(child, script_blocks_by_label, blocks_to_update)


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


@dataclass
class WorkflowBrowserCleanupResult:
    browser_state: BrowserState | None
    tasks: list[Task]
    all_workflow_task_ids: list[str]
    child_workflow_run_ids: list[str]
    close_browser_on_completion: bool
    browser_session_write_back_attempted: bool = False


def _get_workflow_definition_core_data(workflow_definition: WorkflowDefinition) -> dict[str, Any]:
    """
    This function dumps the workflow definition and removes the irrelevant data to the definition, like created_at and modified_at fields inside:
    - list of blocks
    - list of parameters
    And return the dumped workflow definition as a python dictionary.
    """
    # Convert the workflow definition to a dictionary
    workflow_dict = workflow_definition.model_dump(mode="json")
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
    # `steps` is a plain-language annotation, not execution input, so editing it must not bust the cached script.
    code_block_annotation_fields = ("steps",)

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
            if current_obj.get("block_type") == BlockType.CODE.value:
                for field in code_block_annotation_fields:
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


def _resolve_first_block_url(
    blocks: list[BlockTypeVar],
    workflow_run: "WorkflowRun",
) -> str | None:
    """Resolve the URL of the first block that has one, for proxy selection.

    In script mode ``skyvern.setup()`` creates the browser before any block runs;
    threading this URL lets the ``SKYVERN_PROXY`` flag (which matches on ``task_url``)
    resolve instead of falling back to the org default. Best-effort: any failure
    returns None so proxy selection degrades to the previous (url=None) behavior.
    """
    try:
        wrc = app.WORKFLOW_CONTEXT_MANAGER.get_workflow_run_context(workflow_run.workflow_run_id)
        for block in blocks:
            url = getattr(block, "url", None)
            if not url:
                continue
            resolved_url = block.format_block_parameter_template_from_workflow_run_context(url, wrc)
            if resolved_url and isinstance(resolved_url, str):
                return resolved_url
    except Exception:
        LOG.warning("Failed to resolve first block URL for proxy selection", exc_info=True)
    return None


def _truncate_managed_browser_profile_part(value: str, max_length: int) -> str:
    if len(value) <= max_length:
        return value
    if max_length <= 3:
        return value[:max_length]
    return f"{value[: max_length - 3]}..."


def _build_managed_browser_profile_name(workflow_title: str | None, rendered_key: str | None) -> str:
    title = (workflow_title or DEFAULT_WORKFLOW_TITLE).strip() or DEFAULT_WORKFLOW_TITLE
    if not rendered_key:
        suffix = " (auto-saved session)"
        title = _truncate_managed_browser_profile_part(title, MANAGED_BROWSER_PROFILE_NAME_MAX_LENGTH - len(suffix))
        return f"{title}{suffix}"

    key = _truncate_managed_browser_profile_part(rendered_key, MANAGED_BROWSER_PROFILE_KEY_MAX_LENGTH)
    suffix = f" (auto-saved: {key})"
    title = _truncate_managed_browser_profile_part(title, MANAGED_BROWSER_PROFILE_NAME_MAX_LENGTH - len(suffix))
    return f"{title}{suffix}"


class WorkflowService:
    # Prevent GC of fire-and-forget asyncio tasks (e.g. task_run sync).
    _background_tasks: set[asyncio.Task] = set()  # noqa: RUF012

    @staticmethod
    async def _record_workflow_run_metadata_best_effort(
        *,
        workflow_run_id: str,
        organization_id: str,
        run_metadata: dict[str, str] | None,
    ) -> None:
        """Persist optional workflow-run metadata while swallowing write failures."""
        # Dormant legacy hook retained until the workflow_run_metadata table drop follow-up.
        if not run_metadata:
            return

        try:
            await app.AGENT_FUNCTION.record_workflow_run_metadata(
                workflow_run_id=workflow_run_id,
                organization_id=organization_id,
                run_metadata=run_metadata,
            )
        except Exception:
            LOG.warning(
                "Failed to record workflow run metadata",
                workflow_run_id=workflow_run_id,
                organization_id=organization_id,
                exc_info=True,
            )

    def _record_workflow_run_metadata_in_background(
        self,
        *,
        workflow_run_id: str,
        organization_id: str,
        run_metadata: dict[str, str] | None,
    ) -> None:
        """Schedule optional workflow-run metadata persistence off the run-creation path."""
        # Dormant legacy hook retained until the workflow_run_metadata table drop follow-up.
        if not run_metadata:
            return

        task = asyncio.create_task(
            self._record_workflow_run_metadata_best_effort(
                workflow_run_id=workflow_run_id,
                organization_id=organization_id,
                run_metadata=run_metadata,
            )
        )
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    @staticmethod
    async def _apply_initial_run_metadata_tags(
        *,
        workflow_run_id: str,
        organization_id: str,
        run_metadata: dict[str, str] | None,
        context: TagWriteContext | None,
    ) -> None:
        if not run_metadata:
            return

        write_context = context or TagWriteContext(
            caller_id=organization_id,
            source=TagSource.SYSTEM,
            caller_type=CallerType.SYSTEM,
        )
        for attempt in range(2):
            try:
                await app.DATABASE.tags.apply_run_tag_changes(
                    workflow_run_id=workflow_run_id,
                    organization_id=organization_id,
                    sets=run_metadata,
                    deletes=set(),
                    context=write_context,
                )
                return
            except IntegrityError:
                if attempt == 0:
                    await asyncio.sleep(random.uniform(0.01, 0.05))
                    continue
                raise

    @staticmethod
    async def _apply_creation_run_tags_best_effort(
        *,
        workflow: Workflow,
        workflow_run_id: str,
        organization_id: str,
        parameters: dict[str, Any],
        trigger_type: WorkflowRunTriggerType,
    ) -> None:
        try:
            tags = {TRIGGER_RUN_TAG_KEY: trigger_type.value}
            domain = workflow_script_service.resolve_target_domain_for_run_provenance(workflow, parameters)
            if domain:
                tags[TARGET_DOMAIN_RUN_TAG_KEY] = domain
            try:
                platform = workflow_script_service.detect_workflow_platform_for_tagging(
                    workflow, parameters, domain_override=domain
                )
                if platform:
                    tags[DETECTED_PLATFORM_RUN_TAG_KEY] = platform
            except Exception:
                LOG.warning(
                    "Failed to detect platform for creation workflow run tags",
                    workflow_run_id=workflow_run_id,
                    organization_id=organization_id,
                    exc_info=True,
                )
            await app.DATABASE.tags.apply_system_run_tag_changes(
                workflow_run_id=workflow_run_id,
                organization_id=organization_id,
                sets=tags,
                caller_id=CREATION_RUN_TAG_CALLER_ID,
            )
        except Exception:
            LOG.warning(
                "Failed to apply creation workflow run tags",
                workflow_run_id=workflow_run_id,
                organization_id=organization_id,
                exc_info=True,
            )

    @staticmethod
    async def _apply_completion_run_tags_best_effort(workflow_run: WorkflowRun) -> None:
        try:
            tags = {"skyvern.status": str(workflow_run.status)}
            if workflow_run.run_with:
                tags["skyvern.execution_mode"] = workflow_run.run_with
            if workflow_run.failure_category:
                primary_category = workflow_run.failure_category[0].get("category")
                if isinstance(primary_category, str):
                    tags["skyvern.failure_category"] = primary_category

            await app.DATABASE.tags.apply_system_run_tag_changes(
                workflow_run_id=workflow_run.workflow_run_id,
                organization_id=workflow_run.organization_id,
                sets=tags,
                caller_id=COMPLETION_RUN_TAG_CALLER_ID,
            )
        except Exception:
            LOG.warning(
                "Failed to apply completion workflow run tags",
                workflow_run_id=workflow_run.workflow_run_id,
                organization_id=workflow_run.organization_id,
                exc_info=True,
            )

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
        seen_group_keys: set[tuple[ScriptStatus | str, str, tuple[str, ...]]] = set()

        scripts_by_id = await app.DATABASE.scripts.get_latest_scripts_by_ids(
            organization_id=organization_id,
            script_ids=[candidate.script_id for candidate in candidates],
        )
        blocks_by_revision = await app.DATABASE.scripts.get_script_blocks_by_script_revision_ids(
            organization_id=organization_id,
            script_revision_ids=[script.script_revision_id for script in scripts_by_id.values()],
        )

        for candidate in candidates:
            script = scripts_by_id.get(candidate.script_id)
            if not script:
                continue

            script_blocks = blocks_by_revision.get(script.script_revision_id, [])
            blocks_to_clear = [
                block for block in script_blocks if block.script_block_label in target_labels and block.run_signature
            ]
            if not blocks_to_clear:
                continue

            group_key = (
                candidate.status,
                script.script_revision_id,
                tuple(block.script_block_id for block in blocks_to_clear),
            )
            if group_key in seen_group_keys:
                continue
            seen_group_keys.add(group_key)

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
        blocks_by_id = {block.script_block_id: block for group in groups for block in group.blocks_to_clear}
        if not blocks_by_id:
            return

        cleared_count = await app.DATABASE.scripts.clear_script_block_run_signatures(
            organization_id=organization_id,
            script_block_ids=list(blocks_by_id),
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
            cached_group_count=len(groups),
            script_count=len({group.script.script_id for group in groups}),
            script_revision_count=len({group.script.script_revision_id for group in groups}),
            cleared_block_labels=list(dict.fromkeys(block.script_block_label for block in blocks_by_id.values())),
            cleared_block_count=cleared_count,
            deduped_block_count=len(blocks_by_id),
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

        artifacts = await app.DATABASE.artifacts.get_artifacts_by_ids(artifact_ids, organization_id)
        if not artifacts:
            return []

        urls = await app.ARTIFACT_MANAGER.get_share_links_with_bundle_support(artifacts)
        return [u for u in urls if u is not None]

    async def _file_infos_from_download_artifact_ids(
        self,
        artifact_ids: list[str],
        organization_id: str | None,
    ) -> list[FileInfo]:
        """Rebuild ``FileInfo`` objects from DOWNLOAD artifact IDs.

        Used to refresh persisted block-output ``downloaded_files`` snapshots:
        the URL captured at execution time may be a legacy presigned S3 URL,
        but the artifact row has everything we need to mint a fresh signed
        ``/v1/artifacts/{id}/content`` URL on each API fetch.
        """
        if not artifact_ids or not organization_id:
            return []
        artifacts = await app.DATABASE.artifacts.get_artifacts_by_ids(artifact_ids, organization_id)
        if not artifacts:
            return []
        # Preserve the input order so block outputs render files in save order.
        by_id = {a.artifact_id: a for a in artifacts}
        ordered = [by_id[aid] for aid in artifact_ids if aid in by_id]
        return await _file_infos_from_download_artifacts(ordered)

    async def _file_infos_for_workflow_run_filtered_by_filenames(
        self,
        workflow_run_id: str,
        organization_id: str,
        filenames: set[str],
    ) -> list[FileInfo]:
        """Look up DOWNLOAD artifact rows for the workflow run and filter to
        the given filename set.

        Used as a backwards-compat fallback for block-output snapshots that
        were persisted without ``downloaded_file_artifact_ids`` — typically
        because the block's ``get_downloaded_files`` ran before
        ``save_downloaded_files`` finished creating the artifact rows. We
        match by filename so a multi-block run doesn't merge sibling blocks'
        downloads into one another's snapshots.

        Filenames are matched case-sensitively against ``Artifact.uri``'s
        basename, mirroring how ``_file_infos_from_download_artifacts``
        derives ``filename`` from the URI.
        """
        if not workflow_run_id or not organization_id or not filenames:
            return []
        try:
            artifacts = await app.DATABASE.artifacts.list_artifacts_for_run_by_type(
                run_id=workflow_run_id,
                organization_id=organization_id,
                artifact_type=ArtifactType.DOWNLOAD,
            )
        except Exception:
            LOG.warning(
                "Failed to refresh block-output downloaded_files via run-id lookup",
                workflow_run_id=workflow_run_id,
                organization_id=organization_id,
                exc_info=True,
            )
            return []
        if not artifacts:
            return []
        matched: list[Artifact] = []
        seen: set[str] = set()
        for artifact in artifacts:
            basename = artifact.uri.rsplit("/", 1)[-1] if artifact.uri else ""
            if basename in filenames and basename not in seen:
                matched.append(artifact)
                seen.add(basename)
        return await _file_infos_from_download_artifacts(matched)

    @staticmethod
    def _collect_artifact_ids(value: Any) -> tuple[list[str], list[str]]:
        """Sync scan of the block-output tree → (screenshot_ids, download_ids). No DB calls."""
        screenshot_ids: list[str] = []
        download_ids: list[str] = []
        if isinstance(value, dict):
            has_artifact_ids = "task_screenshot_artifact_ids" in value or "workflow_screenshot_artifact_ids" in value
            if has_artifact_ids:
                screenshot_ids.extend(value.get("task_screenshot_artifact_ids") or [])
                screenshot_ids.extend(value.get("workflow_screenshot_artifact_ids") or [])
                download_ids.extend(value.get("downloaded_file_artifact_ids") or [])
            else:
                for v in value.values():
                    s, d = WorkflowService._collect_artifact_ids(v)
                    screenshot_ids.extend(s)
                    download_ids.extend(d)
        elif isinstance(value, list):
            for item in value:
                s, d = WorkflowService._collect_artifact_ids(item)
                screenshot_ids.extend(s)
                download_ids.extend(d)
        return screenshot_ids, download_ids

    async def _refresh_output_urls(
        self,
        value: Any,
        organization_id: str | None,
        workflow_run_id: str,
    ) -> Any:
        """Two-pass batch URL refresh: scan artifact IDs (sync), fetch all in 2 parallel DB calls, substitute in-place."""
        if not organization_id:
            return value

        screenshot_ids, download_ids = WorkflowService._collect_artifact_ids(value)
        all_ids = list(dict.fromkeys(screenshot_ids + download_ids))

        url_map: dict[str, str] = {}
        fileinfo_map: dict[str, FileInfo] = {}

        if all_ids:
            artifacts, expiry_seconds = await asyncio.gather(
                app.DATABASE.artifacts.get_artifacts_by_ids(all_ids, organization_id),
                app.ARTIFACT_MANAGER.resolve_artifact_url_expiry_seconds(organization_id),
            )
            download_id_set = set(download_ids)
            for artifact in artifacts:
                url = await app.ARTIFACT_MANAGER.resolve_share_url(artifact, expiry_seconds=expiry_seconds)
                if url is None:
                    continue
                url_map[artifact.artifact_id] = url
                if artifact.artifact_id in download_id_set:
                    filename = artifact.uri.rsplit("/", 1)[-1] if artifact.uri else ""
                    fileinfo_map[artifact.artifact_id] = FileInfo(
                        url=url,
                        checksum=artifact.checksum,
                        filename=filename,
                        file_size=artifact.file_size,
                        modified_at=artifact.created_at,
                        artifact_id=artifact.artifact_id,
                    )

        return await self._substitute_artifact_urls(value, url_map, fileinfo_map, workflow_run_id, organization_id)

    async def _substitute_artifact_urls(
        self,
        value: Any,
        url_map: dict[str, str],
        fileinfo_map: dict[str, FileInfo],
        workflow_run_id: str,
        organization_id: str | None,
    ) -> Any:
        """Substitute artifact IDs with pre-built URLs and FileInfos (no DB in the common path).

        Mirrors the structure of the old _refresh_output_urls recursive walk but reads
        from the pre-fetched maps instead of issuing per-block DB queries.
        """
        if isinstance(value, dict):
            has_artifact_ids = "task_screenshot_artifact_ids" in value or "workflow_screenshot_artifact_ids" in value
            has_old_format = "task_id" in value and ("task_screenshots" in value or "workflow_screenshots" in value)

            if has_artifact_ids:
                if value.get("task_screenshot_artifact_ids"):
                    value["task_screenshots"] = [
                        url_map[aid] for aid in value["task_screenshot_artifact_ids"] if aid in url_map
                    ]
                if value.get("workflow_screenshot_artifact_ids"):
                    value["workflow_screenshots"] = [
                        url_map[aid] for aid in value["workflow_screenshot_artifact_ids"] if aid in url_map
                    ]
                if value.get("downloaded_file_artifact_ids"):
                    refreshed = [
                        fileinfo_map[aid] for aid in value["downloaded_file_artifact_ids"] if aid in fileinfo_map
                    ]
                    if refreshed:
                        value["downloaded_files"] = [fi.model_dump(mode="json") for fi in refreshed]
                        value["downloaded_file_urls"] = [fi.url for fi in refreshed]
                elif value.get("downloaded_files") and organization_id:
                    # Fallback for snapshots persisted without downloaded_file_artifact_ids:
                    # one query for the whole run, filtered by filename.
                    stored_filenames: set[str] = set()
                    for fi in value["downloaded_files"]:
                        if isinstance(fi, dict):
                            filename = fi.get("filename")
                            if isinstance(filename, str) and filename:
                                stored_filenames.add(filename)
                    if stored_filenames:
                        refreshed = await self._file_infos_for_workflow_run_filtered_by_filenames(
                            workflow_run_id=workflow_run_id,
                            organization_id=organization_id,
                            filenames=stored_filenames,
                        )
                        if refreshed:
                            value["downloaded_files"] = [fi.model_dump(mode="json") for fi in refreshed]
                            value["downloaded_file_urls"] = [fi.url for fi in refreshed]
            elif has_old_format:
                # Legacy snapshots without artifact IDs — one query per run (not per block), already O(1).
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
                for k, v in value.items():
                    value[k] = await self._substitute_artifact_urls(
                        v, url_map, fileinfo_map, workflow_run_id, organization_id
                    )
        elif isinstance(value, list):
            for i, item in enumerate(value):
                value[i] = await self._substitute_artifact_urls(
                    item, url_map, fileinfo_map, workflow_run_id, organization_id
                )
        return value

    async def _validate_credential_ids(self, credential_ids: list[str], organization: Organization) -> None:
        if not credential_ids:
            return
        unique_ids = list(dict.fromkeys(credential_ids))
        existing = await app.DATABASE.credentials.get_credentials_by_ids(
            unique_ids, organization_id=organization.organization_id
        )
        found = {credential.credential_id for credential in existing}
        missing = [credential_id for credential_id in unique_ids if credential_id not in found]
        if missing:
            raise InvalidCredentialId(", ".join(missing))

    async def _validate_and_normalize_credential_rotation_parameters(
        self,
        parameters: list[Any],
        organization: Organization,
    ) -> None:
        credential_ids_to_validate: list[str] = []
        for parameter in parameters:
            credential_ids = getattr(parameter, "credential_ids", None)
            if credential_ids is None:
                continue
            key = getattr(parameter, "key", None) or "<unknown>"
            if not credential_ids:
                raise SkyvernHTTPException(
                    message=f"credential_ids for credential parameter {key} must be non-empty.",
                    status_code=400,
                )
            credential_ids = list(dict.fromkeys(credential_ids))
            parameter.credential_ids = credential_ids
            selection_strategy = getattr(parameter, "selection_strategy", None)
            if normalize_selection_strategy(selection_strategy) not in VALID_SELECTION_STRATEGIES:
                raise SkyvernHTTPException(
                    message=(
                        f"selection_strategy for credential parameter {key} must be one of: "
                        f"{', '.join(sorted(VALID_SELECTION_STRATEGIES))}."
                    ),
                    status_code=400,
                )
            parameter.credential_id = credential_ids[0]
            credential_ids_to_validate.extend(credential_ids)

        await self._validate_credential_ids(credential_ids_to_validate, organization)

    @staticmethod
    def _get_rotating_credential_parameters(workflow: Workflow) -> list[CredentialParameter]:
        workflow_definition = getattr(workflow, "workflow_definition", None)
        if workflow_definition is None:
            return []
        return [
            parameter
            for parameter in workflow_definition.parameters
            if isinstance(parameter, CredentialParameter) and bool(parameter.credential_ids)
        ]

    def _get_run_credential_parameter_overrides(
        self,
        *,
        workflow: Workflow,
        request_data: dict[str, Any] | None,
    ) -> dict[str, str]:
        if not request_data:
            return {}

        overrides: dict[str, str] = {}
        for parameter in self._get_rotating_credential_parameters(workflow):
            if parameter.key not in request_data:
                continue

            override = request_data[parameter.key]
            if override is None or override == "":
                continue
            if not isinstance(override, str):
                raise InvalidCredentialId(f"<non-string value of type {type(override).__name__}>")

            credential_ids = parameter.credential_ids or []
            if override not in credential_ids:
                raise SkyvernHTTPException(
                    message=(
                        f"Credential override for parameter {parameter.key} must be one of the configured "
                        "rotation credentials."
                    ),
                    status_code=400,
                )

            overrides[parameter.key] = override

        return overrides

    async def _apply_run_credential_parameter_overrides(
        self,
        *,
        workflow: Workflow,
        workflow_run: WorkflowRun,
        organization_id: str,
        request_data: dict[str, Any] | None,
    ) -> dict[str, str]:
        overrides = self._get_run_credential_parameter_overrides(
            workflow=workflow,
            request_data=request_data,
        )
        if not overrides:
            return {}

        repo = app.DATABASE.workflow_run_credential_selections
        for parameter_key, credential_id in overrides.items():
            existing = await repo.get_selection(
                workflow_run_id=workflow_run.workflow_run_id,
                parameter_key=parameter_key,
            )
            if existing:
                if existing != credential_id:
                    raise SkyvernHTTPException(
                        message=(
                            f"Credential override for parameter {parameter_key} conflicts with an existing "
                            "credential selection for this run."
                        ),
                        status_code=400,
                    )
                continue

            try:
                await repo.create_selection(
                    organization_id=organization_id,
                    workflow_run_id=workflow_run.workflow_run_id,
                    workflow_permanent_id=workflow.workflow_permanent_id,
                    parameter_key=parameter_key,
                    credential_id=credential_id,
                )
            except IntegrityError:
                existing = await repo.get_selection(
                    workflow_run_id=workflow_run.workflow_run_id,
                    parameter_key=parameter_key,
                )
                if existing == credential_id:
                    continue
                raise

        return overrides

    async def _select_rotating_credential_parameters_for_render(
        self,
        *,
        workflow: Workflow,
        workflow_run: WorkflowRun,
        organization_id: str,
        credential_parameter_overrides: dict[str, str] | None = None,
    ) -> dict[str, str]:
        selections = dict(credential_parameter_overrides or {})
        try:
            for parameter in self._get_rotating_credential_parameters(workflow):
                if parameter.key in selections:
                    continue
                credential_ids = parameter.credential_ids
                if not credential_ids:
                    continue
                selections[parameter.key] = await select_credential_for_run(
                    workflow_run_id=workflow_run.workflow_run_id,
                    organization_id=organization_id,
                    workflow_permanent_id=workflow.workflow_permanent_id,
                    parameter_key=parameter.key,
                    credential_ids=credential_ids,
                    selection_strategy=parameter.selection_strategy,
                )
            return selections
        except Exception:
            LOG.warning(
                "Failed to select rotating credentials for workflow render parameters",
                workflow_run_id=workflow_run.workflow_run_id,
                workflow_permanent_id=workflow.workflow_permanent_id,
                exc_info=True,
            )
            if workflow.browser_profile_key:
                raise
            return selections

    async def validate_schedule_parameters(
        self,
        workflow: Workflow,
        organization: Organization,
        request_data: dict[str, Any] | None,
    ) -> None:
        all_workflow_parameters = await self.get_workflow_parameters(workflow_id=workflow.workflow_id)
        schedule_parameters = [
            cast(WorkflowParameter, workflow_parameter)
            for workflow_parameter in all_workflow_parameters
            if self._is_schedule_input_parameter(workflow_parameter)
        ]
        request_data = request_data or {}

        defined_keys = {workflow_parameter.key for workflow_parameter in schedule_parameters}
        unknown_keys = sorted(set(request_data) - defined_keys)
        if unknown_keys:
            unknown_keys_str = ", ".join(unknown_keys)
            raise SkyvernHTTPException(
                message=(
                    f"Unknown schedule parameters for workflow {workflow.workflow_permanent_id}: {unknown_keys_str}"
                )
            )

        missing_parameters: list[str] = []
        credential_ids_to_validate: list[str] = []
        for workflow_parameter in schedule_parameters:
            if workflow_parameter.key in request_data:
                request_value = request_data[workflow_parameter.key]
                # Treat explicit None as "use the default at execution time". Validate the
                # default value instead so the check matches what actually runs.
                if request_value is None and workflow_parameter.default_value is not None:
                    request_value = workflow_parameter.default_value
                if self._is_missing_required_value(workflow_parameter, request_value):
                    missing_parameters.append(workflow_parameter.key)
                    continue
                if workflow_parameter.workflow_parameter_type == WorkflowParameterType.CREDENTIAL_ID:
                    if not isinstance(request_value, str):
                        raise InvalidCredentialId(f"Credential ID must be a string, got {type(request_value).__name__}")
                    credential_ids_to_validate.append(request_value)
            elif workflow_parameter.default_value is not None:
                if workflow_parameter.workflow_parameter_type == WorkflowParameterType.CREDENTIAL_ID:
                    if not isinstance(workflow_parameter.default_value, str):
                        raise InvalidCredentialId(
                            f"Credential ID must be a string, got {type(workflow_parameter.default_value).__name__}"
                        )
                    credential_ids_to_validate.append(workflow_parameter.default_value)
            else:
                missing_parameters.append(workflow_parameter.key)

        if missing_parameters:
            missing_keys_str = ", ".join(sorted(missing_parameters))
            raise SkyvernHTTPException(
                message=(
                    f"Missing schedule parameters for workflow {workflow.workflow_permanent_id}: {missing_keys_str}"
                )
            )

        await self._validate_credential_ids(credential_ids_to_validate, organization)

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
        workflow_run_id: str | None = None,
        trigger_type: WorkflowRunTriggerType | None = None,
        workflow_schedule_id: str | None = None,
        ignore_inherited_workflow_system_prompt: bool = False,
        copilot_session_id: str | None = None,
        resolved_workflow_id: str | None = None,
        tag_write_context: TagWriteContext | None = None,
    ) -> WorkflowRun:
        """
        Create a workflow run and its parameters. Validate the workflow and the organization. If there are missing
        parameters with no default value, mark the workflow run as failed.
        :param request_id: The request id for the workflow run.
        :param workflow_request: The request body for the workflow run, containing the parameters and the config.
        :param workflow_id: The workflow id to run.
        :param organization_id: The organization id for the workflow.
        :param max_steps_override: The max steps override for the workflow run, if any.
        :param resolved_workflow_id: Pin the exact workflow version row to run against, resolved by
            workflow_id. Used when the (permanent_id, version) index is non-unique and a version=
            lookup could resolve the wrong row. When None, resolve by permanent id + version.
        :return: The created workflow run.
        """
        async with app.DATABASE.workflow_runs.Session() as outer_session:
            # Validate the workflow and the organization
            if resolved_workflow_id is not None:
                workflow = await app.DATABASE.workflows.get_workflow(
                    workflow_id=resolved_workflow_id,
                    organization_id=None if is_template_workflow else organization.organization_id,
                )
            else:
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
            if workflow_request.extra_http_headers is None and workflow.extra_http_headers is not None:
                workflow_request.extra_http_headers = workflow.extra_http_headers
            if (
                workflow_request.browser_profile_id is None
                and workflow_request.browser_session_id is None
                and workflow.browser_profile_id is not None
            ):
                workflow_request.browser_profile_id = workflow.browser_profile_id
            if workflow_request.cdp_connect_headers is None:
                if workflow.cdp_connect_headers is not None:
                    workflow_request.cdp_connect_headers = workflow.cdp_connect_headers
            else:
                workflow_request.cdp_connect_headers = merge_masked_headers(
                    workflow_request.cdp_connect_headers, workflow.cdp_connect_headers
                )
            if workflow_request.run_with is None:
                workflow_request.run_with = workflow.run_with

            # Force ai_fallback=True for adaptive caching (code_version >= 2) runs.
            # Adaptive caching requires AI fallback to self-heal when cached scripts break.
            # Without this, a caller sending ai_fallback=false would silently disable recovery.
            effective_code_version = (
                workflow.code_version
                if workflow.code_version is not None
                else (2 if workflow.adaptive_caching else None)
            )
            if (effective_code_version or 0) >= 2 and (workflow_request.run_with == "code"):
                if workflow_request.ai_fallback is False:
                    LOG.info(
                        "Overriding ai_fallback to True for adaptive caching run",
                        workflow_permanent_id=workflow_permanent_id,
                        request_run_with=workflow_request.run_with,
                        workflow_code_version=workflow.code_version,
                    )
                    workflow_request.ai_fallback = True

            # Inherit from ambient context so descendant runs (TriggerWorkflowBlock children)
            # carry the parent's chat id forward without per-call plumbing. Resolved here so
            # the same value reaches both the DB row and the new SkyvernContext below.
            ambient_context: skyvern_context.SkyvernContext | None = skyvern_context.current()
            resolved_copilot_session_id = (
                copilot_session_id
                if copilot_session_id is not None
                else (ambient_context.copilot_session_id if ambient_context else None)
            )
            resolved_trigger_type = trigger_type
            if resolved_trigger_type is None and ambient_context:
                resolved_trigger_type = ambient_context.trigger_type
            if resolved_trigger_type is None:
                resolved_trigger_type = WorkflowRunTriggerType.api

            # Create the workflow run and set skyvern context
            workflow_run = await self.create_workflow_run(
                workflow_request=workflow_request,
                workflow_permanent_id=workflow_permanent_id,
                workflow_id=workflow_id,
                organization_id=organization.organization_id,
                parent_workflow_run_id=parent_workflow_run_id,
                sequential_key=workflow.sequential_key,
                # Run-request nulls intentionally inherit the workflow default; workflow definition updates
                # use model_fields_set so explicit null can clear the saved workflow-level setting.
                max_elapsed_time_minutes=workflow_request.max_elapsed_time_minutes
                if workflow_request.max_elapsed_time_minutes is not None
                else workflow.max_elapsed_time_minutes,
                debug_session_id=debug_session_id,
                code_gen=code_gen,
                workflow_run_id=workflow_run_id,
                trigger_type=resolved_trigger_type,
                workflow_schedule_id=workflow_schedule_id,
                ignore_inherited_workflow_system_prompt=ignore_inherited_workflow_system_prompt,
                copilot_session_id=resolved_copilot_session_id,
            )
            try:
                await self._apply_initial_run_metadata_tags(
                    workflow_run_id=workflow_run.workflow_run_id,
                    organization_id=organization.organization_id,
                    run_metadata=workflow_request.run_metadata,
                    context=tag_write_context,
                )
            except Exception:
                LOG.warning(
                    "Failed to apply initial workflow run metadata tags",
                    workflow_run_id=workflow_run.workflow_run_id,
                    organization_id=organization.organization_id,
                    exc_info=True,
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
                context.root_workflow_run_id
                if context and context.root_workflow_run_id
                else workflow_run.workflow_run_id
            )
            skyvern_context.replace(
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
                    loop_internal_state=copy.deepcopy(context.loop_internal_state) if context else None,
                    copilot_session_id=resolved_copilot_session_id,
                    trigger_type=resolved_trigger_type,
                )
            )

            # Check artifact bundling flag at workflow level so it applies to both agent and cached paths.
            # See also: skyvern/forge/agent.py Agent.agent_step() checks per-task for standalone task runs.
            new_context = skyvern_context.current()
            if new_context:
                try:
                    new_context.use_artifact_bundling = await app.EXPERIMENTATION_PROVIDER.is_feature_enabled_cached(
                        "USE_ARTIFACT_BUNDLING",
                        workflow_run.workflow_run_id,
                        properties={"organization_id": organization.organization_id},
                    )
                    LOG.debug(
                        "USE_ARTIFACT_BUNDLING flag resolved for workflow",
                        use_artifact_bundling=new_context.use_artifact_bundling,
                        workflow_run_id=workflow_run.workflow_run_id,
                        organization_id=organization.organization_id,
                    )
                except Exception:
                    LOG.warning("Failed to check USE_ARTIFACT_BUNDLING flag for workflow", exc_info=True)
                    new_context.use_artifact_bundling = False

                # Resolve flex routing eligibility once per run boot. Site B
                # (scripts/run_workflow.py) re-resolves in the Temporal worker — both go
                # through the same AgentFunction hook so the cloud side is the single
                # owner of the flag name and property shape.
                new_context.use_flex_llm_routing = await app.AGENT_FUNCTION.should_use_flex_llm_routing(
                    trigger_type=resolved_trigger_type,
                    organization_id=organization.organization_id,
                    workflow_permanent_id=workflow_run.workflow_permanent_id,
                    workflow_run_id=workflow_run.workflow_run_id,
                )

                await resolve_enrich_tree_for_context(
                    new_context,
                    workflow_run.workflow_run_id,
                    organization.organization_id,
                    workflow_permanent_id=workflow_run.workflow_permanent_id,
                    log_context={"workflow_run_id": workflow_run.workflow_run_id},
                )

            # Create all the workflow run parameters, AWSSecretParameter won't have workflow run parameters created.
            all_workflow_parameters = await self.get_workflow_parameters(workflow_id=workflow.workflow_id)
            try:
                missing_parameters: list[str] = []
                workflow_parameter_values: list[tuple[WorkflowParameter, Any]] = []
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
                            if not isinstance(request_body_value, str):
                                raise InvalidCredentialId(
                                    f"<non-string value of type {type(request_body_value).__name__}>"
                                )
                        workflow_parameter_values.append((workflow_parameter, request_body_value))
                    elif workflow_parameter.default_value is not None:
                        if workflow_parameter.workflow_parameter_type == WorkflowParameterType.CREDENTIAL_ID:
                            if not isinstance(workflow_parameter.default_value, str):
                                raise InvalidCredentialId(
                                    f"<non-string value of type {type(workflow_parameter.default_value).__name__}>"
                                )
                        workflow_parameter_values.append((workflow_parameter, workflow_parameter.default_value))
                    else:
                        missing_parameters.append(workflow_parameter.key)

                if missing_parameters:
                    missing_list = ", ".join(sorted(missing_parameters))
                    raise MissingValueForParameter(
                        parameter_key=missing_list,
                        workflow_id=workflow.workflow_permanent_id,
                        workflow_run_id=workflow_run.workflow_run_id,
                    )

                await self._validate_credential_ids(
                    [
                        value
                        for (workflow_parameter, value) in workflow_parameter_values
                        if workflow_parameter.workflow_parameter_type == WorkflowParameterType.CREDENTIAL_ID
                    ],
                    organization,
                )

                parameter_values = {param.key: value for param, value in workflow_parameter_values}
                run_credential_parameter_overrides = await self._apply_run_credential_parameter_overrides(
                    workflow=workflow,
                    workflow_run=workflow_run,
                    organization_id=organization.organization_id,
                    request_data=workflow_request.data,
                )
                rotating_credential_selections = await self._select_rotating_credential_parameters_for_render(
                    workflow=workflow,
                    workflow_run=workflow_run,
                    organization_id=organization.organization_id,
                    credential_parameter_overrides=run_credential_parameter_overrides,
                )
                parameter_values.update(rotating_credential_selections)
                workflow_run = await self._prepare_persisted_workflow_browser_profile(
                    workflow=workflow,
                    workflow_run=workflow_run,
                    parameter_values=parameter_values,
                    # Keyless workflows keep best-effort rotation selection; keyed workflows re-raise above.
                    allow_missing_browser_profile_key=(
                        bool(self._get_rotating_credential_parameters(workflow)) and not rotating_credential_selections
                    ),
                )

                if workflow_parameter_values:
                    try:
                        await self.create_workflow_run_parameters(
                            workflow_run_id=workflow_run.workflow_run_id,
                            workflow_parameter_values=workflow_parameter_values,
                        )
                    except SQLAlchemyError as batch_error:
                        # Roll back the failed transaction so the per-parameter fallback
                        # (and any later mark_workflow_run_as_failed) can reuse the outer session.
                        await outer_session.rollback()
                        # Batch failed — retry one-by-one to identify the exact failing parameter
                        for workflow_parameter, value in workflow_parameter_values:
                            try:
                                await self.create_workflow_run_parameter(
                                    workflow_run_id=workflow_run.workflow_run_id,
                                    workflow_parameter=workflow_parameter,
                                    value=value,
                                )
                            except SQLAlchemyError as parameter_error:
                                raise WorkflowRunParameterPersistenceError(
                                    parameter_key=workflow_parameter.key,
                                    workflow_id=workflow.workflow_permanent_id,
                                    workflow_run_id=workflow_run.workflow_run_id,
                                    reason=self._format_parameter_persistence_error(parameter_error),
                                ) from parameter_error
                        # All individual inserts succeeded — the batch failure was transient
                        LOG.warning(
                            "Batch parameter insert failed but individual inserts succeeded",
                            workflow_run_id=workflow_run.workflow_run_id,
                            batch_error=str(batch_error),
                        )
                await self._apply_creation_run_tags_best_effort(
                    workflow=workflow,
                    workflow_run_id=workflow_run.workflow_run_id,
                    organization_id=organization.organization_id,
                    parameters=parameter_values,
                    trigger_type=resolved_trigger_type,
                )
            except Exception as e:
                LOG.exception(
                    f"Error while setting up workflow run {workflow_run.workflow_run_id}",
                    workflow_run_id=workflow_run.workflow_run_id,
                )

                # Discard any failed transaction state on the shared outer session before
                # mark_workflow_run_as_failed reuses it.
                try:
                    await outer_session.rollback()
                except SQLAlchemyError:
                    LOG.warning("Failed to rollback outer session during setup failure", exc_info=True)

                failure_reason = f"Setup workflow failed. failure reason: {get_user_facing_exception_message(e)}"

                workflow_run = await self.mark_workflow_run_as_failed(
                    workflow_run_id=workflow_run.workflow_run_id, failure_reason=failure_reason
                )
                raise

            return workflow_run

    @staticmethod
    def _format_parameter_persistence_error(error: SQLAlchemyError) -> str:
        if isinstance(error, IntegrityError):
            return "value cannot be null"
        return "database error while saving parameter value"

    async def _prepare_persisted_workflow_browser_profile(
        self,
        *,
        workflow: Workflow,
        workflow_run: WorkflowRun,
        parameter_values: dict[str, Any],
        allow_missing_browser_profile_key: bool = False,
    ) -> WorkflowRun:
        if not workflow.persist_browser_session:
            return workflow_run

        if workflow_run.browser_profile_id:
            return workflow_run

        try:
            rendered_key = await self._render_workflow_browser_profile_key(
                workflow=workflow,
                workflow_run=workflow_run,
                parameter_values=parameter_values,
            )
        except MissingValueForParameter:
            if not allow_missing_browser_profile_key:
                raise
            LOG.warning(
                "Falling back to keyless managed browser profile after missing browser profile key",
                workflow_run_id=workflow_run.workflow_run_id,
                workflow_permanent_id=workflow.workflow_permanent_id,
                browser_profile_key=workflow.browser_profile_key,
            )
            rendered_key = None
        digest = build_browser_profile_key_digest(rendered_key)
        profile, created = await app.DATABASE.browser_sessions.get_or_create_managed_browser_profile(
            organization_id=workflow_run.organization_id,
            workflow_permanent_id=workflow.workflow_permanent_id,
            browser_profile_key_digest=digest,
            name=_build_managed_browser_profile_name(workflow.title, rendered_key),
        )
        if created:
            try:
                await self._seed_managed_browser_profile_from_legacy_session(
                    workflow=workflow,
                    organization_id=workflow_run.organization_id,
                    workflow_run_id=workflow_run.workflow_run_id,
                    rendered_key=rendered_key,
                    browser_profile_id=profile.browser_profile_id,
                )
            except Exception:
                # Roll back the empty row so the next run re-attempts creation + seed;
                # this run degrades to the legacy session path instead of failing setup.
                LOG.warning(
                    "Failed to seed managed browser profile from legacy session; rolling back and using legacy path",
                    workflow_run_id=workflow_run.workflow_run_id,
                    browser_profile_id=profile.browser_profile_id,
                    exc_info=True,
                )
                await app.DATABASE.browser_sessions.hard_delete_browser_profile(
                    profile_id=profile.browser_profile_id,
                    organization_id=workflow_run.organization_id,
                )
                return workflow_run

        await self._reconcile_managed_browser_profile_proxy_pin(
            workflow=workflow,
            profile=profile,
            browser_profile_key_digest=digest,
            organization_id=workflow_run.organization_id,
            proxy_location=workflow_run.proxy_location,
            workflow_run_id=workflow_run.workflow_run_id,
        )
        updated_workflow_run = await app.DATABASE.workflow_runs.update_workflow_run(
            workflow_run_id=workflow_run.workflow_run_id,
            browser_profile_id=profile.browser_profile_id,
        )
        LOG.info(
            "Stamped workflow run with managed browser profile",
            workflow_run_id=workflow_run.workflow_run_id,
            workflow_permanent_id=workflow.workflow_permanent_id,
            browser_profile_id=profile.browser_profile_id,
            browser_profile_key_digest=digest,
        )
        return updated_workflow_run

    async def _reconcile_managed_browser_profile_proxy_pin(
        self,
        *,
        workflow: Workflow,
        profile: BrowserProfile,
        browser_profile_key_digest: str,
        organization_id: str,
        proxy_location: ProxyLocationInput,
        workflow_run_id: str | None = None,
    ) -> None:
        should_pin = workflow.pin_saved_session_ip and should_generate_proxy_session_id(proxy_location)
        try:
            existing_pin = profile.proxy_session_id
            if should_pin:
                if browser_profile_key_digest:
                    proxy_session_id = derive_proxy_session_id(
                        organization_id,
                        workflow.workflow_permanent_id,
                        browser_profile_key_digest,
                    )
                else:
                    proxy_session_id = derive_proxy_session_id(
                        organization_id,
                        workflow.workflow_permanent_id,
                    )
                # Re-derive rather than trust existing values so a drifted or corrupted
                # pin or location self-heals to the deterministic per-segment state.
                if existing_pin == proxy_session_id and should_generate_proxy_session_id(profile.proxy_location):
                    return
                await app.DATABASE.browser_sessions.update_browser_profile(
                    profile_id=profile.browser_profile_id,
                    organization_id=organization_id,
                    proxy_location=ProxyLocation.RESIDENTIAL_ISP,
                    proxy_session_id=proxy_session_id,
                )
                LOG.info(
                    "Pinned managed browser profile proxy session",
                    workflow_run_id=workflow_run_id,
                    workflow_permanent_id=workflow.workflow_permanent_id,
                    browser_profile_id=profile.browser_profile_id,
                    proxy_session_id=redact_proxy_session_id(proxy_session_id),
                )
                return

            if profile.is_managed and existing_pin:
                await app.DATABASE.browser_sessions.update_browser_profile(
                    profile_id=profile.browser_profile_id,
                    organization_id=organization_id,
                    proxy_location=None,
                    proxy_session_id=None,
                )
                LOG.info(
                    "Cleared managed browser profile proxy session pin",
                    workflow_run_id=workflow_run_id,
                    workflow_permanent_id=workflow.workflow_permanent_id,
                    browser_profile_id=profile.browser_profile_id,
                    proxy_session_id=redact_proxy_session_id(existing_pin),
                )
        except Exception:
            LOG.warning(
                "Failed to reconcile managed browser profile proxy pin",
                workflow_run_id=workflow_run_id,
                workflow_permanent_id=workflow.workflow_permanent_id,
                browser_profile_id=profile.browser_profile_id,
                exc_info=True,
            )
            if should_pin:
                raise

    async def _resolve_managed_browser_profile_for_run_request(
        self,
        *,
        workflow: Workflow,
        organization_id: str,
        workflow_request: WorkflowRequestBody,
        effective_proxy_location: ProxyLocationInput,
        extra_parameter_values: dict[str, str] | None = None,
    ) -> str | None:
        try:
            if not workflow.persist_browser_session or workflow_request.browser_profile_id:
                return None

            rendered_key = None
            if workflow.browser_profile_key:
                parameter_values: dict[str, Any] = {}
                for parameter in workflow.workflow_definition.parameters:
                    if isinstance(parameter, WorkflowParameter) and parameter.default_value is not None:
                        parameter_values[parameter.key] = parameter.default_value
                if workflow_request.data:
                    parameter_values.update(
                        {key: value for key, value in workflow_request.data.items() if value is not None}
                    )
                if extra_parameter_values:
                    parameter_values.update(extra_parameter_values)
                rendered_key = render_browser_profile_key(workflow.browser_profile_key, parameter_values)
                if not rendered_key:
                    return None

            digest = build_browser_profile_key_digest(rendered_key)
            profile, created = await app.DATABASE.browser_sessions.get_or_create_managed_browser_profile(
                organization_id=organization_id,
                workflow_permanent_id=workflow.workflow_permanent_id,
                browser_profile_key_digest=digest,
                name=_build_managed_browser_profile_name(workflow.title, rendered_key),
            )
            if created:
                try:
                    await self._seed_managed_browser_profile_from_legacy_session(
                        workflow=workflow,
                        organization_id=organization_id,
                        rendered_key=rendered_key,
                        browser_profile_id=profile.browser_profile_id,
                    )
                except Exception:
                    # Roll back the empty row so the next run re-attempts creation + seed;
                    # this run degrades to the legacy session path instead of failing setup.
                    LOG.warning(
                        "Failed to seed managed browser profile from legacy session; rolling back and using legacy path",
                        workflow_run_id=None,
                        browser_profile_id=profile.browser_profile_id,
                        exc_info=True,
                    )
                    await app.DATABASE.browser_sessions.hard_delete_browser_profile(
                        profile_id=profile.browser_profile_id,
                        organization_id=organization_id,
                    )
                    return None
            await self._reconcile_managed_browser_profile_proxy_pin(
                workflow=workflow,
                profile=profile,
                browser_profile_key_digest=digest,
                organization_id=organization_id,
                proxy_location=effective_proxy_location,
                workflow_run_id=None,
            )
            return profile.browser_profile_id
        except Exception:
            LOG.warning(
                "Failed to resolve managed browser profile for workflow run request",
                organization_id=organization_id,
                workflow_permanent_id=workflow.workflow_permanent_id,
                exc_info=True,
            )
            return None

    async def _seed_managed_browser_profile_from_legacy_session(
        self,
        *,
        workflow: Workflow,
        organization_id: str,
        rendered_key: str | None,
        browser_profile_id: str,
        workflow_run_id: str | None = None,
    ) -> None:
        # Lazy migration: carry a workflow's accumulated Save & Reuse login state onto its new
        # managed profile the first time we create it, so the next run starts logged in. The OSS
        # launch path has no legacy-archive fallback, so this copy must happen here, not at launch.
        storage_key = build_workflow_browser_session_storage_key(workflow.workflow_permanent_id, rendered_key)
        legacy_session_dir = await app.STORAGE.retrieve_browser_session(organization_id, storage_key)
        if not legacy_session_dir:
            return
        await app.STORAGE.store_browser_profile(
            organization_id,
            profile_id=browser_profile_id,
            directory=legacy_session_dir,
        )
        LOG.info(
            "Seeded managed browser profile from legacy session archive",
            workflow_run_id=workflow_run_id,
            browser_profile_id=browser_profile_id,
            browser_session_storage_key=storage_key,
        )

    async def _render_workflow_browser_profile_key(
        self,
        *,
        workflow: Workflow,
        workflow_run: WorkflowRun,
        parameter_values: dict[str, Any] | None,
    ) -> str | None:
        if not workflow.browser_profile_key:
            return None

        if parameter_values is None:
            try:
                parameter_tuples = await app.DATABASE.workflow_runs.get_workflow_run_parameters(
                    workflow_run_id=workflow_run.workflow_run_id,
                )
                parameter_values = {wf_param.key: run_param.value for wf_param, run_param in parameter_tuples}
            except Exception as exc:
                raise SkyvernHTTPException(
                    message=("Failed to read workflow run parameters while resolving the browser profile segment key")
                ) from exc

        try:
            rendered_key = render_browser_profile_key(workflow.browser_profile_key, parameter_values)
        except Exception as exc:
            raise SkyvernHTTPException(
                message=f"Failed to render browser profile segment key: {get_user_facing_exception_message(exc)}"
            ) from exc

        if not rendered_key:
            raise MissingValueForParameter(
                parameter_key=workflow.browser_profile_key,
                workflow_id=workflow.workflow_permanent_id,
                workflow_run_id=workflow_run.workflow_run_id,
            )

        return rendered_key

    async def get_workflow_browser_session_storage_key(
        self,
        *,
        workflow: Workflow,
        workflow_run: WorkflowRun,
        parameter_values: dict[str, Any] | None = None,
    ) -> str:
        if not workflow.browser_profile_key:
            return workflow.workflow_permanent_id

        rendered_key = await self._render_workflow_browser_profile_key(
            workflow=workflow,
            workflow_run=workflow_run,
            parameter_values=parameter_values,
        )
        storage_key = build_workflow_browser_session_storage_key(workflow.workflow_permanent_id, rendered_key)
        LOG.info(
            "Resolved workflow browser session storage key",
            workflow_run_id=workflow_run.workflow_run_id,
            workflow_permanent_id=workflow.workflow_permanent_id,
            browser_profile_key=workflow.browser_profile_key,
            storage_key=storage_key,
        )
        return storage_key

    @staticmethod
    def _is_schedule_input_parameter(workflow_parameter: Any) -> bool:
        """Check whether a parameter is user-configurable input for scheduled runs.

        Filters to WorkflowParameter instances only — excludes ContextParameter,
        OutputParameter, and CredentialParameter (the model class, whose credentials
        are resolved at runtime).  Note that a WorkflowParameter whose
        workflow_parameter_type is CREDENTIAL_ID *is* included here because the
        user supplies the credential ID string at schedule time; only the actual
        CredentialParameter objects are excluded.
        """
        return isinstance(workflow_parameter, WorkflowParameter)

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
        browser_profile_id: str | None = None,
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
                browser_profile_id=browser_profile_id,
                proxy_location=proxy_location,
                inherit_profile_proxy=True,
            )

            return browser_session

        return None

    async def _browser_profile_is_managed(self, *, organization_id: str, browser_profile_id: str | None) -> bool:
        if not browser_profile_id:
            return False
        profile = await app.DATABASE.browser_sessions.get_browser_profile(
            profile_id=browser_profile_id,
            organization_id=organization_id,
        )
        return bool(profile and profile.is_managed)

    async def auto_create_browser_session_for_code_block_if_needed(
        self,
        organization_id: str,
        workflow: Workflow,
        *,
        workflow_run_id: str,
        browser_session_id: str | None = None,
        browser_profile_id: str | None = None,
        proxy_location: ProxyLocationInput = None,
    ) -> PersistentBrowserSession | None:
        """Auto-provision a persistent browser session for runs that contain a CodeBlock.

        The secure CodeBlock runner brokers page operations against a live persistent browser
        session, so a CodeBlock with no caller-supplied session would otherwise fall back to
        the legacy in-process executor. When the org is enabled for the secure runner, provision
        a session here so the CodeBlock routes to the runner. A run's browser_profile_id is loaded
        into the session so profile-backed CodeBlock workflows still reach the runner. Returns None
        (run continues on the legacy path) when no CodeBlock is present, the org is not enabled, a
        session was already supplied, or session creation fails.
        """
        if browser_session_id:  # the caller supplied a session; respect it unchanged
            return None

        all_blocks = get_all_blocks(workflow.workflow_definition.blocks)
        if not any(block.block_type == BlockType.CODE for block in all_blocks):
            return None

        should_create = await app.AGENT_FUNCTION.should_auto_create_browser_session_for_code_block(
            workflow_run_id=workflow_run_id,
            organization_id=organization_id,
            workflow_permanent_id=workflow.workflow_permanent_id,
            workflow_id=workflow.workflow_id,
        )
        if not should_create:
            return None

        try:
            browser_session = await app.PERSISTENT_SESSIONS_MANAGER.create_session(
                organization_id=organization_id,
                timeout_minutes=CODE_BLOCK_SESSION_TIMEOUT_MINUTES,
                browser_profile_id=browser_profile_id,
                proxy_location=proxy_location,
                inherit_profile_proxy=True,
            )
        except Exception:
            LOG.warning(
                "Failed to auto-create browser session for CodeBlock run; falling back to legacy executor",
                workflow_run_id=workflow_run_id,
                organization_id=organization_id,
                workflow_permanent_id=workflow.workflow_permanent_id,
                exc_info=True,
            )
            return None

        LOG.info(
            "Auto-created browser session for CodeBlock run",
            workflow_run_id=workflow_run_id,
            organization_id=organization_id,
            workflow_permanent_id=workflow.workflow_permanent_id,
            browser_session_id=browser_session.persistent_browser_session_id,
        )
        return browser_session

    async def _collect_inherited_workflow_system_prompt(
        self,
        parent_workflow_run_id: str | None,
    ) -> str | None:
        """Walk up the parent workflow-run chain and join each ancestor's raw
        ``workflow_system_prompt`` (outermost first). Returns None when no ancestor
        has one set. A depth cap matches ``WorkflowTriggerBlock.MAX_TRIGGER_DEPTH``
        to keep the traversal bounded against malformed chains.

        This reads raw prompt strings from each ancestor's ``workflow_definition``
        without Jinja rendering — the child's context will render them later via
        ``WorkflowRunContext.resolve_effective_workflow_system_prompt``. Using raw
        strings here avoids depending on the parent's live ``WorkflowRunContext``,
        which isn't available for async/fire-and-forget child runs.

        Chain-break on opt-out: when an ancestor has ``ignore_inherited_workflow_system_prompt``
        set, its own prompt is still included (it ran without its own ancestors'
        prompts, but its own rules remain its statement to descendants), but the
        traversal stops there. A workflow explicitly opting out of its parents'
        rules creates a clean boundary for itself and everything it triggers —
        otherwise descendants would silently reintroduce prompts the opted-out
        workflow rejected.
        """
        # Two-phase walk to keep DB round trips bounded. Phase 1 is an
        # inherently sequential chain walk (each ``parent_workflow_run_id`` is
        # only known after fetching the previous run), capped at
        # ``MAX_TRIGGER_DEPTH``. Phase 2 batches the independent workflow-
        # definition fetches with ``asyncio.gather`` so all N definition
        # lookups happen in one concurrent burst instead of N sequential
        # awaits — brings the worst case from 2N round trips down to
        # N + 1 (depth-bounded at 10). A deeper optimization (single
        # recursive CTE across workflow_runs + workflows) is possible if
        # trigger chains ever get deep enough to matter.
        chain: list[tuple[str, bool]] = []  # [(workflow_id, ignore_inherited), ...] outermost child first
        current_parent_id: str | None = parent_workflow_run_id
        visited: set[str] = set()
        depth = 0
        while current_parent_id and depth < WorkflowTriggerBlock.MAX_TRIGGER_DEPTH:
            if current_parent_id in visited:
                break
            visited.add(current_parent_id)
            parent_run = await app.DATABASE.workflow_runs.get_workflow_run(current_parent_id)
            if parent_run is None:
                break
            chain.append((parent_run.workflow_id, parent_run.ignore_inherited_workflow_system_prompt))
            if parent_run.ignore_inherited_workflow_system_prompt:
                break
            current_parent_id = parent_run.parent_workflow_run_id
            depth += 1

        if not chain:
            return None

        # Fetch all ancestor workflow definitions concurrently.
        ancestor_workflows = await asyncio.gather(
            *(self.get_workflow(workflow_id=workflow_id) for workflow_id, _ in chain),
            return_exceptions=False,
        )

        prompts: list[str] = []
        for workflow in ancestor_workflows:
            if workflow is None or workflow.workflow_definition is None:
                continue
            raw = workflow.workflow_definition.workflow_system_prompt
            if raw:
                prompts.append(raw)

        if not prompts:
            return None
        # Outermost ancestor first so child-local rules appear after broader rules.
        prompts.reverse()
        return "\n\n".join(prompts)

    async def _handle_post_run_elapsed_timeout(
        self,
        *,
        workflow_run_id: str,
        organization_id: str | None,
        workflow_run: WorkflowRun,
        pre_finally_status: WorkflowRunStatus | None,
        pre_finally_failure_reason: str | None,
        timeout_failure_reason: str,
    ) -> tuple[WorkflowRun, WorkflowRunStatus | None, str | None]:
        if pre_finally_status is None:
            if refreshed_workflow_run := await app.DATABASE.workflow_runs.get_workflow_run(
                workflow_run_id=workflow_run_id,
                organization_id=organization_id,
            ):
                workflow_run = refreshed_workflow_run
                if workflow_run.status.is_final():
                    pre_finally_status = workflow_run.status
                    pre_finally_failure_reason = workflow_run.failure_reason
        if pre_finally_status and pre_finally_status.is_final():
            LOG.info(
                "Preserving terminal workflow run status after post-run elapsed timeout",
                workflow_run_id=workflow_run_id,
                pre_finally_status=pre_finally_status,
            )
        else:
            workflow_run = await self.mark_workflow_run_as_timed_out(
                workflow_run_id=workflow_run_id,
                failure_reason=timeout_failure_reason,
            )
            pre_finally_status = WorkflowRunStatus.timed_out
            pre_finally_failure_reason = timeout_failure_reason
        return workflow_run, pre_finally_status, pre_finally_failure_reason

    async def _shield_post_run_elapsed_timeout(
        self,
        *,
        workflow_run_id: str,
        organization_id: str | None,
        workflow_run: WorkflowRun,
        pre_finally_status: WorkflowRunStatus | None,
        pre_finally_failure_reason: str | None,
        timeout_failure_reason: str,
    ) -> tuple[WorkflowRun, WorkflowRunStatus | None, str | None]:
        handle_timeout_task = asyncio.create_task(
            self._handle_post_run_elapsed_timeout(
                workflow_run_id=workflow_run_id,
                organization_id=organization_id,
                workflow_run=workflow_run,
                pre_finally_status=pre_finally_status,
                pre_finally_failure_reason=pre_finally_failure_reason,
                timeout_failure_reason=timeout_failure_reason,
            ),
            name=f"post_run_elapsed_timeout_{workflow_run_id}",
        )
        try:
            return await asyncio.shield(handle_timeout_task)
        except asyncio.CancelledError:
            LOG.warning(
                "Cancellation received while handling post-run elapsed timeout; waiting for status write",
                workflow_run_id=workflow_run_id,
            )
            try:
                return await handle_timeout_task
            except Exception:
                LOG.exception(
                    "Post-run elapsed timeout handler failed after cancellation; falling back to direct timeout write",
                    workflow_run_id=workflow_run_id,
                )
                fallback_workflow_run = await asyncio.shield(
                    self.mark_workflow_run_as_timed_out(
                        workflow_run_id=workflow_run_id,
                        failure_reason=timeout_failure_reason,
                    )
                )
                return fallback_workflow_run, WorkflowRunStatus.timed_out, timeout_failure_reason

    @traced(name="skyvern.workflow.execute", role="wrapper")
    async def execute_workflow(
        self,
        workflow_run_id: str,
        api_key: str | None,
        organization: Organization,
        block_labels: list[str] | None = None,
        block_outputs: dict[str, Any] | None = None,
        browser_session_id: str | None = None,
        need_call_webhook: bool = True,
        workflow_override: Workflow | None = None,
    ) -> WorkflowRun:
        """Execute a workflow.

        When the workflow_run row has ``ignore_inherited_workflow_system_prompt``
        set (populated at spawn time by a ``WorkflowTriggerBlock`` whose
        ``ignore_workflow_system_prompt`` flag is True), the child workflow
        starts with a clean slate — no inherited prompt from the ancestor
        chain. Persisting the intent on the row means the flag is honored for
        both sync and async (Temporal-dispatched) trigger modes.
        """
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

        # Guard: if the run was canceled while queued (before Temporal picked it up), don't
        # overwrite the canceled status with running. Checked BEFORE workflow resolution so a run
        # whose stamped workflow version was deleted after cancellation does not raise
        # WorkflowNotFound on a late worker pickup.
        if workflow_run.status == WorkflowRunStatus.canceled:
            LOG.info(
                "Workflow run was canceled before execution started, skipping",
                workflow_run_id=workflow_run_id,
                organization_id=organization_id,
            )
            return workflow_run

        # Resolve the exact version stamped on the run (run.workflow_id is always set), not the
        # latest published version, so a publish between run creation and execution does not change
        # what executes.
        workflow = workflow_override or await self.get_workflow(workflow_id=workflow_run.workflow_id)
        has_conditionals = workflow_script_service.workflow_has_conditionals(workflow)
        browser_profile_id = workflow_run.browser_profile_id
        close_browser_on_completion = browser_session_id is None and not workflow_run.browser_address

        enterprise_gated_features = _collect_enterprise_gated_workflow_features(workflow, block_labels=block_labels)
        if enterprise_gated_features:
            try:
                await app.AGENT_FUNCTION.validate_enterprise_feature_access(
                    organization_id=organization_id,
                    feature_names=enterprise_gated_features,
                )
            except DisabledBlockExecutionError as e:
                LOG.warning(
                    "Workflow uses enterprise-gated features without access",
                    workflow_run_id=workflow_run_id,
                    organization_id=organization_id,
                    workflow_permanent_id=workflow.workflow_permanent_id,
                    enterprise_gated_features=sorted(enterprise_gated_features),
                    exc_info=True,
                )
                workflow_run = await self.mark_workflow_run_as_failed(
                    workflow_run_id=workflow_run_id,
                    failure_reason=get_user_facing_exception_message(e),
                )
                await self.clean_up_workflow(
                    workflow=workflow,
                    workflow_run=workflow_run,
                    api_key=api_key,
                    browser_session_id=browser_session_id,
                    close_browser_on_completion=close_browser_on_completion,
                    need_call_webhook=need_call_webhook,
                )
                return workflow_run

        # Set workflow run status to running, create workflow run parameters
        workflow_run = await self.mark_workflow_run_as_running(workflow_run_id=workflow_run_id)
        # Short-circuit when the conditional transition was refused (cron beat us
        # to finalization). Falling through would otherwise hit the finally-block
        # path below, which writes ``running`` again and emits orphan children.
        if workflow_run.status.is_final():
            LOG.info(
                "execute_workflow aborting — workflow_run already in final state",
                workflow_run_id=workflow_run_id,
                current_status=workflow_run.status,
            )
            return workflow_run

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
        # Collect resolved workflow_system_prompt from every ancestor workflow so child
        # blocks inherit them (SKY-9147). We read each parent's workflow_definition from
        # the DB because the parent's in-memory WorkflowRunContext may be gone by the
        # time a fire-and-forget child runs on its own worker. Jinja placeholders in
        # ancestor prompts are rendered against this run's values; parent-only
        # parameters will simply render empty in non-strict mode.
        inherited_workflow_system_prompt = (
            None
            if workflow_run.ignore_inherited_workflow_system_prompt
            else await self._collect_inherited_workflow_system_prompt(
                parent_workflow_run_id=workflow_run.parent_workflow_run_id,
            )
        )
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
                inherited_workflow_system_prompt=inherited_workflow_system_prompt,
            )
        except Exception as e:
            LOG.exception(
                f"Error while initializing workflow run context for workflow run {workflow_run_id}",
                workflow_run_id=workflow_run_id,
            )

            exception_message = get_user_facing_exception_message(e)

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
                need_call_webhook=need_call_webhook,
            )
            return workflow_run

        browser_session = None
        using_managed_browser_profile = await self._browser_profile_is_managed(
            organization_id=organization.organization_id,
            browser_profile_id=browser_profile_id,
        )
        if not browser_profile_id or using_managed_browser_profile:
            browser_session = await self.auto_create_browser_session_if_needed(
                organization.organization_id,
                workflow,
                browser_session_id=browser_session_id,
                browser_profile_id=browser_profile_id if using_managed_browser_profile else None,
                proxy_location=workflow_run.proxy_location,
            )

        # The CodeBlock auto-PBS path is intentionally NOT gated on browser_profile_id: a
        # profile-backed CodeBlock workflow still needs a session for the secure runner, so the
        # profile is loaded into the auto-created session instead of skipping it.
        if browser_session is None and browser_session_id is None:
            browser_session = await self.auto_create_browser_session_for_code_block_if_needed(
                organization.organization_id,
                workflow,
                workflow_run_id=workflow_run_id,
                browser_session_id=browser_session_id,
                browser_profile_id=browser_profile_id,
                proxy_location=workflow_run.proxy_location,
            )

        if browser_session:
            browser_session_id = browser_session.persistent_browser_session_id
            close_browser_on_completion = True
            await app.DATABASE.workflow_runs.update_workflow_run(
                workflow_run_id=workflow_run.workflow_run_id,
                browser_session_id=browser_session_id,
            )

        # Make browser_session_id available in Jinja templates via {{ browser_session_id }}.
        # IMPORTANT: This must happen before _execute_workflow_blocks, which is where
        # template rendering occurs. If this assignment moves after block execution,
        # browser_session_id will silently resolve to empty string in templates.
        workflow_run_context = app.WORKFLOW_CONTEXT_MANAGER.get_workflow_run_context(workflow_run_id)
        workflow_run_context.browser_session_id = browser_session_id

        renewal_task: asyncio.Task[None] | None = None
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
                failure_reason = (
                    f"Failed to begin browser session for workflow run: {get_user_facing_exception_message(e)}"
                )
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
                    need_call_webhook=need_call_webhook,
                )
                return workflow_run
            # Start background task to periodically renew the browser session
            renewal_task = asyncio.create_task(
                self._renew_browser_session_loop(browser_session_id, organization.organization_id),
                name=f"browser_session_renewal_{workflow_run_id}",
            )

        # Captured inside the try and consumed in the outer finally so status
        # finalization runs even when the body is cancelled or raises. Stays
        # None if we were cancelled before the block-execution step completed;
        # in that case there's no terminal-state intent to restore.
        pre_finally_status: WorkflowRunStatus | None = None
        pre_finally_failure_reason: str | None = None

        try:
            effective_max_elapsed_minutes = get_effective_workflow_run_max_elapsed_time_minutes(
                workflow_run.max_elapsed_time_minutes
            )
            timeout_failure_reason = _format_workflow_run_elapsed_timeout_failure_reason(effective_max_elapsed_minutes)
            max_elapsed_timeout_seconds = _get_workflow_run_max_elapsed_timeout_seconds(workflow_run)
            LOG.debug(
                "Workflow run elapsed timeout computed",
                workflow_run_id=workflow_run_id,
                max_elapsed_time_minutes_raw=workflow_run.max_elapsed_time_minutes,
                effective_max_elapsed_minutes=effective_max_elapsed_minutes,
                max_elapsed_timeout_seconds=max_elapsed_timeout_seconds,
                started_at=workflow_run.started_at,
            )
            blocks_to_update: set[str] = set()

            async def execute_workflow_blocks() -> None:
                nonlocal workflow_run, blocks_to_update
                # Check if there's a related workflow script that should be used instead.
                workflow_script, _, script_is_pinned = await workflow_script_service.get_workflow_script(
                    workflow, workflow_run, block_labels
                )
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
                    script_is_pinned=script_is_pinned,
                )

            if max_elapsed_timeout_seconds <= 0:
                workflow_run = await self.mark_workflow_run_as_timed_out(
                    workflow_run_id=workflow_run_id,
                    failure_reason=_require_elapsed_timeout_failure_reason(timeout_failure_reason),
                )
                return workflow_run
            else:
                timeout_context = asyncio.timeout(max_elapsed_timeout_seconds)
                try:
                    async with timeout_context:
                        await execute_workflow_blocks()
                except TimeoutError:
                    if not timeout_context.expired():
                        raise
                    workflow_run = await self.mark_workflow_run_as_timed_out(
                        workflow_run_id=workflow_run_id,
                        failure_reason=_require_elapsed_timeout_failure_reason(timeout_failure_reason),
                    )
                    return workflow_run

            post_run_timeout_seconds = _get_workflow_run_max_elapsed_timeout_seconds(workflow_run)
            if post_run_timeout_seconds <= POST_RUN_TIMEOUT_EXHAUSTED_THRESHOLD_SECONDS:
                (
                    workflow_run,
                    pre_finally_status,
                    pre_finally_failure_reason,
                ) = await self._shield_post_run_elapsed_timeout(
                    workflow_run_id=workflow_run_id,
                    organization_id=organization_id,
                    workflow_run=workflow_run,
                    pre_finally_status=pre_finally_status,
                    pre_finally_failure_reason=pre_finally_failure_reason,
                    timeout_failure_reason=_require_elapsed_timeout_failure_reason(timeout_failure_reason),
                )
                return workflow_run

            post_run_timeout_context = asyncio.timeout(post_run_timeout_seconds)
            try:
                async with post_run_timeout_context:
                    # Check if there's a finally block configured
                    finally_block_label = workflow.workflow_definition.finally_block_label

                    # Refresh workflow_run from DB to pick up status/failure_reason
                    # set by _execute_workflow_blocks.
                    if refreshed_workflow_run := await app.DATABASE.workflow_runs.get_workflow_run(
                        workflow_run_id=workflow_run_id,
                        organization_id=organization_id,
                    ):
                        workflow_run = refreshed_workflow_run

                    pre_finally_status = workflow_run.status
                    pre_finally_failure_reason = workflow_run.failure_reason

                    # Statuses that always skip script generation
                    skip_statuses = {WorkflowRunStatus.canceled, WorkflowRunStatus.failed, WorkflowRunStatus.timed_out}
                    # When generate_script_on_terminal is enabled, allow terminated runs to generate scripts
                    if not workflow.generate_script_on_terminal:
                        skip_statuses.add(WorkflowRunStatus.terminated)

                    if pre_finally_status not in skip_statuses:
                        await self.generate_script_if_needed(
                            workflow=workflow,
                            workflow_run=workflow_run,
                            block_labels=block_labels,
                            blocks_to_update=blocks_to_update,
                            finalize=True,  # Force regeneration to ensure field mappings have complete action data
                            has_conditionals=has_conditionals,
                        )
                    else:
                        LOG.info(
                            "Skipping post-run script generation due to run status",
                            workflow_run_id=workflow_run_id,
                            workflow_permanent_id=workflow.workflow_permanent_id,
                            pre_finally_status=pre_finally_status,
                            blocks_to_update_count=len(blocks_to_update),
                        )

                    # Trigger AI Script Reviewer for adaptive caching workflows
                    # Include terminated and failed runs — the reviewer filters to only
                    # episodes where the AI fallback succeeded (actionable signal).
                    # Skip canceled (user stopped) and timed_out (infrastructure issue)
                    # Only trigger if the script was actually executed this run — reviewing based on
                    # agent-only runs provides no signal about script quality and wastes LLM tokens.
                    # Only trigger if this run used the latest script version — stale runs produce
                    # episodes that may already be fixed in newer versions, and reviewing them creates
                    # redundant/regressive versions.
                    is_script_execution = await self.should_run_script(workflow, workflow_run)
                    if (
                        is_adaptive_caching(workflow, workflow_run)
                        and is_script_execution
                        and pre_finally_status
                        not in (
                            WorkflowRunStatus.canceled,
                            WorkflowRunStatus.timed_out,
                        )
                    ):
                        should_trigger_reviewer = True
                        current_ctx = skyvern_context.current()
                        if current_ctx and current_ctx.script_id:
                            latest_script = await app.DATABASE.scripts.get_latest_script_version(
                                script_id=current_ctx.script_id,
                                organization_id=workflow.organization_id,
                            )
                            if latest_script and latest_script.script_revision_id != current_ctx.script_revision_id:
                                should_trigger_reviewer = False
                                LOG.info(
                                    "Skipping script reviewer - run used stale script version",
                                    workflow_run_id=workflow_run.workflow_run_id,
                                    used_revision=current_ctx.script_revision_id,
                                    latest_revision=latest_script.script_revision_id,
                                    latest_version=latest_script.version,
                                )
                        if should_trigger_reviewer:
                            asyncio.create_task(
                                self._trigger_script_reviewer(
                                    workflow, workflow_run, pre_finally_status=pre_finally_status
                                ),
                                name=f"script_reviewer_{workflow_run.workflow_run_id}",
                            )
                    elif is_adaptive_caching(workflow, workflow_run):
                        LOG.info(
                            "Skipping script reviewer - script was not executed this run",
                            workflow_run_id=workflow_run.workflow_run_id,
                            run_with=workflow_run.run_with,
                        )

                    # Execute finally block if configured. Skip only for canceled runs; elapsed-time timeouts return
                    # before post-run work, while other timeout statuses can still run cleanup within the remaining cap.
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
            except TimeoutError:
                if not post_run_timeout_context.expired():
                    raise
                (
                    workflow_run,
                    pre_finally_status,
                    pre_finally_failure_reason,
                ) = await self._shield_post_run_elapsed_timeout(
                    workflow_run_id=workflow_run_id,
                    organization_id=organization_id,
                    workflow_run=workflow_run,
                    pre_finally_status=pre_finally_status,
                    pre_finally_failure_reason=pre_finally_failure_reason,
                    timeout_failure_reason=_require_elapsed_timeout_failure_reason(timeout_failure_reason),
                )
        finally:
            # Shielded finalize runs even when the try body was cancelled
            # mid-flight (e.g. the copilot tool's orphan-task cancel path, or
            # any outer caller that cancels execute_workflow). Without this,
            # cancellation between the temporary ``running`` write above and
            # the original finalize call leaked ``running``/``canceled`` rows
            # in place of the real terminal reason. When pre_finally_status is
            # still ``None`` (cancellation landed before block execution
            # completed), there's no captured intent to restore and we skip.
            browser_cleanup_result: WorkflowBrowserCleanupResult | None = None
            if pre_finally_status is not None:
                if workflow.persist_browser_session and pre_finally_status not in (
                    WorkflowRunStatus.canceled,
                    WorkflowRunStatus.failed,
                    WorkflowRunStatus.terminated,
                    WorkflowRunStatus.timed_out,
                ):
                    try:
                        # Sequential dependency gates clear on terminal status, so persisted profile state must land first.
                        browser_cleanup_result = await self._clean_up_workflow_browser(
                            workflow_run=workflow_run,
                            close_browser_on_completion=close_browser_on_completion,
                            browser_session_id=browser_session_id,
                        )
                    except BaseException:
                        LOG.warning(
                            "Pre-finalization browser cleanup failed during execute_workflow cleanup",
                            workflow_run_id=workflow_run_id,
                            exc_info=True,
                        )
                    if browser_cleanup_result is not None:
                        try:
                            async with asyncio.timeout(BROWSER_SESSION_WRITE_BACK_TIMEOUT):
                                await self._persist_workflow_browser_session_if_needed(
                                    workflow=workflow,
                                    workflow_run=workflow_run,
                                    browser_state=browser_cleanup_result.browser_state,
                                    close_browser_on_completion=browser_cleanup_result.close_browser_on_completion,
                                    workflow_run_status=WorkflowRunStatus.completed,
                                )
                            # Only suppress clean_up_workflow's write-back once this one has
                            # actually persisted; a failed/timed-out attempt must still be retried.
                            browser_cleanup_result.browser_session_write_back_attempted = True
                        except BaseException:
                            LOG.warning(
                                "Pre-finalization browser session write-back failed during execute_workflow cleanup",
                                workflow_run_id=workflow_run_id,
                                exc_info=True,
                            )
                try:
                    workflow_run = await asyncio.shield(
                        self._finalize_workflow_run_status(
                            workflow_run_id=workflow_run_id,
                            workflow_run=workflow_run,
                            pre_finally_status=pre_finally_status,
                            pre_finally_failure_reason=pre_finally_failure_reason,
                        )
                    )
                except BaseException:
                    # Catch BaseException (not Exception) so a second
                    # ``CancelledError`` arriving during the shielded await —
                    # plausible when the copilot's detached cancellation
                    # fallback re-cancels ``run_task`` — does not escape this
                    # block and skip ``clean_up_workflow`` below.
                    LOG.warning(
                        "Finalize failed during execute_workflow cleanup",
                        workflow_run_id=workflow_run_id,
                        exc_info=True,
                    )

            if renewal_task is not None and not renewal_task.done():
                renewal_task.cancel()
                try:
                    await renewal_task
                except asyncio.CancelledError:
                    pass

            await self.clean_up_workflow(
                workflow=workflow,
                workflow_run=workflow_run,
                api_key=api_key,
                browser_session_id=browser_session_id,
                close_browser_on_completion=close_browser_on_completion,
                need_call_webhook=need_call_webhook,
                browser_cleanup_result=browser_cleanup_result,
            )

        return workflow_run

    async def _renew_browser_session_loop(self, browser_session_id: str, organization_id: str) -> None:
        """Periodically renew a browser session to prevent timeout during long-running workflows."""
        max_renewal_seconds = 2 * 60 * 60  # 2 hours
        start_time = asyncio.get_event_loop().time()
        while True:
            try:
                await asyncio.sleep(300)  # 5 minutes — ensures 2+ attempts within the 10-min renewal threshold
                elapsed = asyncio.get_event_loop().time() - start_time
                if elapsed >= max_renewal_seconds:
                    LOG.info(
                        "Browser session renewal loop reached 2-hour cap, stopping",
                        browser_session_id=browser_session_id,
                        organization_id=organization_id,
                        elapsed_seconds=elapsed,
                    )
                    return
                await app.PERSISTENT_SESSIONS_MANAGER.renew_or_close_session(browser_session_id, organization_id)
                LOG.debug(
                    "Browser session renewal check completed",
                    browser_session_id=browser_session_id,
                    organization_id=organization_id,
                )
            except asyncio.CancelledError:
                LOG.info(
                    "Browser session renewal loop cancelled",
                    browser_session_id=browser_session_id,
                )
                return
            except BrowserSessionNotRenewable:
                LOG.warning(
                    "Browser session is no longer renewable, stopping renewal loop",
                    browser_session_id=browser_session_id,
                    organization_id=organization_id,
                )
                return
            except Exception:
                LOG.exception(
                    "Error renewing browser session, will retry",
                    browser_session_id=browser_session_id,
                    organization_id=organization_id,
                )

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
        script_is_pinned: bool = False,
    ) -> tuple[WorkflowRun, set[str]]:
        organization_id = organization.organization_id
        workflow_run_id = workflow_run.workflow_run_id
        top_level_blocks = workflow.workflow_definition.blocks
        all_blocks = get_all_blocks(top_level_blocks)

        # Load script blocks if script is provided
        script_blocks_by_label: dict[str, Any] = {}
        loaded_script_module = None
        blocks_to_update: set[str] = set()

        is_script_run = await self.should_run_script(workflow, workflow_run)

        # skyvern.setup() below creates the browser before any block runs, so resolve the
        # first block's URL here to drive proxy selection from task_url (see SKYVERN_PROXY).
        first_block_url = _resolve_first_block_url(all_blocks, workflow_run) if is_script_run else None

        if script:
            LOG.info(
                "Loading script blocks for workflow execution",
                sampling=True,
                workflow_run_id=workflow_run_id,
                script_id=script.script_id,
                script_revision_id=script.script_revision_id,
            )
            context = skyvern_context.ensure_context()
            context.script_id = script.script_id
            context.script_revision_id = script.script_revision_id
            context.code_version = workflow.code_version
            try:
                script_blocks = await app.DATABASE.scripts.get_script_blocks_by_script_revision_id(
                    script_revision_id=script.script_revision_id,
                    organization_id=organization_id,
                )

                # Create mapping from block label to script block.
                # Include blocks with run_signature (code-executable) AND blocks
                # with requires_agent=True (must run via agent even when ai_fallback=False).
                for script_block in script_blocks:
                    if script_block.run_signature or script_block.requires_agent:
                        script_blocks_by_label[script_block.script_block_label] = script_block

                if is_script_run:
                    # load the script files
                    script_files = await app.DATABASE.scripts.get_script_files(
                        script_revision_id=script.script_revision_id,
                        organization_id=organization_id,
                    )
                    await script_service.load_scripts(script, script_files)

                    script_path = os.path.join(settings.TEMP_PATH, script.script_id, "main.py")
                    if os.path.exists(script_path):
                        # setup script run
                        parameter_tuples = await app.DATABASE.workflow_runs.get_workflow_run_parameters(
                            workflow_run_id=workflow_run.workflow_run_id
                        )
                        script_parameters = {wf_param.key: run_param.value for wf_param, run_param in parameter_tuples}

                        spec = importlib.util.spec_from_file_location("user_script", script_path)
                        if spec and spec.loader:
                            loaded_script_module = importlib.util.module_from_spec(spec)
                            try:
                                spec.loader.exec_module(loaded_script_module)
                            except Exception:
                                # Static scripts may fail with spec_from_file_location
                                # due to circular imports. Delegate to AgentFunction for
                                # platform-specific fallback loading.
                                LOG.warning("exec_module failed, trying import_module fallback", exc_info=True)
                                loaded_script_module = app.AGENT_FUNCTION.try_import_static_script(script_path)
                            param_cls = (
                                getattr(loaded_script_module, "GeneratedWorkflowParameters", None)
                                if loaded_script_module
                                else None
                            )
                            await skyvern.setup(
                                script_parameters,
                                generated_parameter_cls=param_cls,
                                url=first_block_url,
                            )
                            if loaded_script_module:
                                # Mark static (pinned) scripts so complete() skips LLM verification
                                if script_is_pinned:
                                    pinned_ctx = skyvern_context.current()
                                    if pinned_ctx:
                                        pinned_ctx.is_static_script = True
                                LOG.info(
                                    "Successfully loaded script module",
                                    sampling=True,
                                    script_id=script.script_id,
                                    block_count=len(script_blocks_by_label),
                                )
                            else:
                                LOG.warning(
                                    "Script module failed to load, blocks will fall back to agent",
                                    script_id=script.script_id,
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

        # If no cached script exists, check if a static pre-built script
        # should be created for this platform (e.g., ATS).  This persists the
        # script to DB (pinned) on first run so it shows in the Code tab.
        if is_script_run and not script_blocks_by_label:
            try:
                static_result = await app.AGENT_FUNCTION.ensure_static_script(
                    workflow=workflow,
                    workflow_run=workflow_run,
                    organization_id=organization_id,
                )
                if static_result:
                    script, script_blocks_by_label, loaded_script_module = static_result
                    is_script_run = True
                    # Initialize RunContext with the browser page + parameters,
                    # same as the normal script loading path at line 1310.
                    parameter_tuples = await app.DATABASE.workflow_runs.get_workflow_run_parameters(
                        workflow_run_id=workflow_run.workflow_run_id,
                    )
                    script_parameters = {wf_param.key: run_param.value for wf_param, run_param in parameter_tuples}
                    param_cls = getattr(loaded_script_module, "GeneratedWorkflowParameters", None)
                    await skyvern.setup(
                        script_parameters,
                        generated_parameter_cls=param_cls,
                        url=first_block_url,
                    )
                    # Mark context so static scripts skip LLM completion verification
                    static_ctx = skyvern_context.current()
                    if static_ctx:
                        static_ctx.is_static_script = True
                    LOG.info(
                        "Static script loaded successfully",
                        script_id=script.script_id if script else None,
                        blocks=list(script_blocks_by_label.keys()),
                    )
                else:
                    LOG.info("No static script available for this workflow")
            except Exception:
                LOG.error("Failed to load static script", exc_info=True)

        # Mark workflow as running, preserving the user's original run_with intent.
        # The run_with field records what the user requested (e.g. "code"),
        # not whether a script was actually found. Execution mode is determined
        # separately by is_script_run and script_mode below.
        await self.mark_workflow_run_as_running(workflow_run_id=workflow_run_id, run_with=workflow_run.run_with)

        # Set script_mode on context so downstream code can skip expensive LLM calls
        # Only enable when we actually have a script to run
        script_mode_active = bool(script and is_script_run and script_blocks_by_label)
        if script_mode_active:
            ctx = skyvern_context.current()
            if ctx:
                ctx.script_mode = True

        # SKY-8684: Detect empty-block scripts and ensure regeneration.
        # When a WorkflowScript exists but has zero usable ScriptBlock records,
        # the run correctly falls through to code_generation mode. However,
        # generate_script was set to False (in execute_workflow) because the
        # script exists. Override it to True so per-block generation fires
        # and post-run finalize can regenerate the script.
        if script and is_script_run and not script_blocks_by_label:
            LOG.warning(
                "Script exists but has zero usable blocks — will regenerate",
                workflow_permanent_id=workflow.workflow_permanent_id,
                workflow_run_id=workflow_run_id,
                script_id=script.script_id,
                script_revision_id=script.script_revision_id,
            )
            regen_ctx = skyvern_context.current()
            if regen_ctx:
                regen_ctx.generate_script = True

        # Single source-of-truth log for how this run will execute.
        # Three modes:
        #   "code"            — cached script loaded, executing code
        #   "code_generation" — configured for code but no script yet,
        #                       running as agent and will generate a script
        #   "agent"           — not configured for code, pure agent run
        if script_mode_active:
            execution_mode = "code"
        elif is_script_run:
            execution_mode = "code_generation"
        else:
            execution_mode = "agent"
        LOG.info(
            "Workflow run execution mode resolved",
            execution_mode=execution_mode,
            workflow_run_id=workflow_run_id,
            workflow_id=workflow.workflow_id,
            workflow_permanent_id=workflow.workflow_permanent_id,
            organization_id=organization_id,
            run_level_run_with=workflow_run.run_with,
            workflow_level_run_with=workflow.run_with,
            code_version=workflow.code_version,
            ai_fallback=workflow_run.ai_fallback,
            should_run_script=is_script_run,
            has_script=script is not None,
            script_id=script.script_id if script else None,
            script_revision_id=script.script_revision_id if script else None,
            script_block_count=len(script_blocks_by_label),
            empty_blocks_detected=script is not None and is_script_run and not script_blocks_by_label,
        )

        if script_mode_active and script is not None:
            # Regression-locked by tests/unit/workflow/test_mark_script_run_loaded.py
            # ::test_mark_script_run_loaded_calls_update_with_script_identity.
            # If you modify this branch, update that test.
            await self._mark_script_run_loaded(workflow_run_id, script)

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

    async def _record_fallback_episode(
        self,
        workflow_run: WorkflowRun,
        workflow: Workflow,
        block: Block,
        organization_id: str,
        workflow_run_id: str,
        error_message: str,
        script_revision_id: str | None = None,
        classify_result: str | None = None,
    ) -> tuple[str | None, list | None]:
        """Record a fallback episode for adaptive caching.

        Captures page state (URL, text snapshot, form fields) and creates a
        fallback episode in the database.  Returns (episode_id, form_fields_snapshot)
        so the caller can attach them to the workflow run block later.

        Wrapped in try/except so failures never break the caller.
        """
        episode_id: str | None = None
        form_fields_snapshot: list | None = None
        try:
            page_url = None
            page_text_snapshot = None
            working_page = None
            try:
                browser_state = await app.BROWSER_MANAGER.get_or_create_for_workflow_run(
                    workflow_run=workflow_run,
                )
                working_page = await browser_state.get_working_page()
                if working_page:
                    page_url = working_page.url
                    page_text_snapshot = (await working_page.inner_text("body"))[:5000]
            except Exception:
                LOG.debug("Failed to capture page state for fallback episode", exc_info=True)

            # Extract structured form field metadata from the DOM
            try:
                if working_page:
                    form_fields_snapshot = await working_page.evaluate("""() => {
                        const fields = [];
                        for (const el of document.querySelectorAll('input, select, textarea')) {
                            if (el.type === 'hidden') continue;
                            const labelEl = el.closest('label')
                                || (el.id && document.querySelector('label[for="' + el.id + '"]'));
                            const label = labelEl ? labelEl.textContent.trim().substring(0, 100) : '';
                            const ariaLabel = el.getAttribute('aria-label') || '';
                            const placeholder = el.getAttribute('placeholder') || '';
                            if (!label && !ariaLabel && !placeholder && !el.name) continue;
                            fields.push({
                                tag: el.tagName.toLowerCase(),
                                type: el.getAttribute('type') || el.tagName.toLowerCase(),
                                label: label,
                                name: el.getAttribute('name') || '',
                                required: el.required || el.getAttribute('aria-required') === 'true',
                                placeholder: placeholder,
                            });
                        }
                        return fields.slice(0, 50);
                    }""")
            except Exception:
                LOG.debug("Failed to extract form field metadata for fallback episode", exc_info=True)

            # Conditional blocks must use "conditional_agent" fallback type so the
            # script reviewer routes them to the simpler conditional-specific prompt
            # instead of the general reviewer (which would generate inappropriate
            # browser-automation code like page.classify for pure-Python conditionals).
            fallback_type = "conditional_agent" if isinstance(block, ConditionalBlock) else "full_block"

            episode = await app.DATABASE.scripts.create_fallback_episode(
                organization_id=organization_id,
                workflow_permanent_id=workflow.workflow_permanent_id,
                workflow_run_id=workflow_run_id,
                block_label=block.label,
                fallback_type=fallback_type,
                script_revision_id=script_revision_id,
                error_message=error_message[:2000],
                classify_result=classify_result,
                page_url=page_url,
                page_text_snapshot=page_text_snapshot,
            )
            episode_id = episode.episode_id
        except Exception:
            LOG.warning(
                "Failed to record fallback episode",
                block_label=block.label,
                exc_info=True,
            )
        return episode_id, form_fields_snapshot

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
        if not block_result:
            return
        if block_result.status == BlockStatus.completed:
            pass  # Always generate for completed blocks
        elif block_result.status == BlockStatus.terminated and workflow.generate_script_on_terminal:
            pass  # Generate for terminated blocks when flag is set
        else:
            return

        context = skyvern_context.current()
        if not context or not context.generate_script:
            return

        # Skip script generation for static (pinned) scripts
        if context.is_static_script:
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
            LOG.error(
                "DAG execution failed: workflow graph validation error",
                workflow_run_id=workflow_run.workflow_run_id,
                workflow_permanent_id=workflow.workflow_permanent_id,
                organization_id=organization.organization_id,
                workflow_id=workflow.workflow_id,
                error=str(exc),
                exc_info=True,
            )
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
                    "DAG execution failed: block label not found in workflow graph",
                    workflow_run_id=workflow_run.workflow_run_id,
                    workflow_permanent_id=workflow.workflow_permanent_id,
                    organization_id=organization.organization_id,
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
                if not next_label:
                    # SKY-8571: Fall back to the conditional block's own
                    # next_block_label when the matched branch has no target
                    # (e.g., default branch with no redirect, failed evaluation
                    # with continue_on_failure, or finally-block stripping).
                    next_label = default_next_map.get(block.label)
                    if next_label:
                        LOG.info(
                            "Conditional branch has no next_block_label, falling back to block's own next_block_label",
                            workflow_run_id=workflow_run.workflow_run_id,
                            block_label=block.label,
                            fallback_next_label=next_label,
                        )
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
                LOG.error(
                    "DAG execution failed: next block label not found in workflow definition",
                    workflow_run_id=workflow_run.workflow_run_id,
                    workflow_permanent_id=workflow.workflow_permanent_id,
                    organization_id=organization.organization_id,
                    current_block_label=block.label,
                    missing_block_label=next_label,
                )
                workflow_run = await self.mark_workflow_run_as_failed(
                    workflow_run_id=workflow_run.workflow_run_id,
                    failure_reason=f"Next block label {next_label} not found in workflow definition",
                )
                break

            if next_label in visited_labels:
                LOG.error(
                    "DAG execution failed: cycle detected during traversal",
                    workflow_run_id=workflow_run.workflow_run_id,
                    workflow_permanent_id=workflow.workflow_permanent_id,
                    organization_id=organization.organization_id,
                    current_block_label=block.label,
                    cycle_block_label=next_label,
                )
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
            if refreshed_workflow_run := await app.DATABASE.workflow_runs.get_workflow_run(
                workflow_run_id=workflow_run_id,
                organization_id=organization_id,
            ):
                workflow_run = refreshed_workflow_run
                if workflow_run.status.is_final():
                    LOG.info(
                        "Workflow run is in a final state, stopping execution inside workflow execution loop",
                        workflow_run_id=workflow_run_id,
                        workflow_run_status=workflow_run.status,
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
                sampling=True,
                block_type=block.block_type,
                workflow_run_id=workflow_run_id,
                block_idx=block_idx,
                block_type_var=block.block_type,
                block_label=block.label,
                model=block.model,
            )

            if block.block_type == BlockType.LOGIN:
                await self._apply_login_block_credential_proxy_pin(
                    block=block,
                    workflow_run=workflow_run,
                    workflow_run_id=workflow_run_id,
                    organization_id=organization_id,
                )
                resolved_browser_profile_id = await self._resolve_login_block_browser_profile_id(
                    block=block,
                    workflow_run_id=workflow_run_id,
                    organization_id=organization_id,
                    workflow_permanent_id=workflow_run.workflow_permanent_id,
                )
                # Save the original navigation goal before any mutation so
                # retries don't stack the browser-session prefix repeatedly.
                original_navigation_goal = block.navigation_goal
                if resolved_browser_profile_id:
                    decision = await self._evaluate_debug_session_profile_decision(
                        workflow_run=workflow_run,
                        browser_session_id=browser_session_id,
                        resolved_browser_profile_id=resolved_browser_profile_id,
                        organization_id=organization_id,
                    )

                    if decision.incompatible_reason is not None:
                        # Debug session whose visible PBS is profile-incompatible.
                        # Stream fidelity wins: attach the visible PBS so the user
                        # can watch the action, but DO NOT write
                        # workflow_run.browser_profile_id, DO NOT rewrite the
                        # navigation_goal, and DO NOT emit "skipping login agent".
                        # Fall through to ordinary LoginBlock execution below.
                        LOG.warning(
                            "Debug session profile incompatible with LoginBlock credential",
                            code=DEBUG_SESSION_PROFILE_INCOMPATIBLE_CODE,
                            reason=decision.incompatible_reason,
                            workflow_run_id=workflow_run_id,
                            block_label=block.label,
                            debug_session_id=workflow_run.debug_session_id,
                            browser_session_id=browser_session_id,
                            credential_browser_profile_id=resolved_browser_profile_id,
                        )
                        if decision.attach_browser_session_id and block.url:
                            try:
                                await app.BROWSER_MANAGER.get_or_create_for_workflow_run(
                                    workflow_run=workflow_run,
                                    url=block.url,
                                    browser_session_id=decision.attach_browser_session_id,
                                )
                            except Exception:
                                LOG.warning(
                                    "PBS attach failed for debug incompatible profile; continuing",
                                    workflow_run_id=workflow_run_id,
                                    block_label=block.label,
                                    browser_session_id=decision.attach_browser_session_id,
                                    exc_info=True,
                                )
                    else:
                        LOG.info(
                            "LoginBlock has credential with browser profile — skipping login agent",
                            workflow_run_id=workflow_run_id,
                            block_label=block.label,
                            browser_profile_id=resolved_browser_profile_id,
                            url=block.url,
                        )
                        # Persist the browser_profile_id on the workflow_run so
                        # subsequent blocks create / reuse a browser with the
                        # saved profile (cookies, localStorage, etc.).
                        await app.DATABASE.workflow_runs.update_workflow_run(
                            workflow_run_id=workflow_run_id,
                            browser_profile_id=resolved_browser_profile_id,
                        )
                        workflow_run = (
                            await app.DATABASE.workflow_runs.get_workflow_run(
                                workflow_run_id=workflow_run_id,
                                organization_id=organization_id,
                            )
                            or workflow_run
                        )

                        # Create the browser with the saved profile and navigate
                        # to the login block's URL.  When a saved-profile credential
                        # is selected, the user is guided to enter the post-login
                        # target URL (e.g. homepage/dashboard) rather than the
                        # login page.  The saved cookies will authenticate the
                        # session once the page loads.
                        profile_loaded = bool(block.url)
                        if block.url:
                            try:
                                browser_state = await app.BROWSER_MANAGER.get_or_create_for_workflow_run(
                                    workflow_run=workflow_run,
                                    url=block.url,
                                    browser_profile_id=resolved_browser_profile_id,
                                    browser_session_id=decision.attach_browser_session_id,
                                )
                                working_page = await browser_state.get_working_page()
                                if working_page and working_page.url == "about:blank":
                                    await browser_state.navigate_to_url(page=working_page, url=block.url)
                                # Wait for the page to settle so cookies/redirects complete
                                if working_page:
                                    try:
                                        await working_page.wait_for_load_state("networkidle", timeout=10000)
                                    except Exception:
                                        LOG.debug(
                                            "networkidle timeout after browser profile navigation (non-fatal)",
                                            workflow_run_id=workflow_run_id,
                                        )
                            except Exception:
                                LOG.warning(
                                    "Saved browser profile failed to load, falling back to normal login",
                                    workflow_run_id=workflow_run_id,
                                    block_label=block.label,
                                    browser_profile_id=resolved_browser_profile_id,
                                    exc_info=True,
                                )
                                profile_loaded = False
                                # Clear the profile so the normal login path doesn't reuse it
                                await app.DATABASE.workflow_runs.update_workflow_run(
                                    workflow_run_id=workflow_run_id,
                                    browser_profile_id=None,
                                )

                        if not profile_loaded:
                            # Fall through to normal block execution below
                            pass
                        else:
                            # Browser profile loaded — the session may still be
                            # valid or may have expired (common with bank sites).
                            # Instead of skipping the login block, modify the
                            # navigation goal so the AI checks whether the user is
                            # already logged in and only performs login if needed.
                            if original_navigation_goal:
                                block.navigation_goal = (
                                    "A saved browser session has been loaded. "
                                    "Check if the user is already logged in. "
                                    "If already logged in, complete this task immediately without taking any action. "
                                    "If not logged in (e.g. the session expired), "
                                    "proceed to log in with the provided credentials.\n\n"
                                    f"Original goal: {original_navigation_goal}"
                                )

            valid_to_run_code = (
                is_script_run and block.label and block.label in script_blocks_by_label and not block.disable_cache
            )
            # requires_agent blocks must execute via agent, not code — skip code path
            block_requires_agent = False
            if valid_to_run_code and script_blocks_by_label[block.label].requires_agent:
                valid_to_run_code = False
                block_requires_agent = True

            # Log the execution mode decision for every block in a script run
            if is_script_run and block.label:
                LOG.info(
                    "Block execution mode resolved",
                    block_label=block.label,
                    execution_mode="script" if valid_to_run_code else "ai",
                    has_label=True,
                    in_cache=block.label in script_blocks_by_label,
                    disable_cache=block.disable_cache,
                    requires_agent=block_requires_agent,
                )

            fallback_episode_id: str | None = None
            form_fields_for_episode: list | None = None
            script_exception: Exception | None = None
            if valid_to_run_code:
                script_block = script_blocks_by_label[block.label]
                LOG.info(
                    "Attempting to execute block with script code",
                    block_label=block.label,
                    run_signature=script_block.run_signature,
                )
                # Script path skips the block's own execute() (which is where
                # format_potential_template_parameters runs in the agent path),
                # so we apply the workflow_system_prompt here to thread the
                # block-resolved value into the ``WorkflowRunContext`` cache.
                # ``ai_extract`` reads from that cache so the script-generated
                # extraction honors ``ignore_workflow_system_prompt`` the same
                # way the agent path does — same string, same cache key.
                try:
                    workflow_run_context = app.WORKFLOW_CONTEXT_MANAGER.get_workflow_run_context(workflow_run_id)
                    block._apply_workflow_system_prompt(workflow_run_context)
                except Exception:
                    LOG.warning(
                        "Failed to apply workflow_system_prompt for script-path block; continuing",
                        block_label=block.label,
                        exc_info=True,
                    )

                block_exec_start = time.monotonic()
                try:
                    vars_dict = vars(loaded_script_module) if loaded_script_module else {}
                    exec_globals = {
                        **vars_dict,
                        "skyvern": skyvern,
                        "__builtins__": __builtins__,
                    }

                    assert script_block.run_signature is not None
                    normalized_signature = textwrap.dedent(script_block.run_signature).strip()

                    # Compound statements (async for, for, if, while) can't be
                    # wrapped in `return (...)` — they must be inlined directly
                    # into the async wrapper function body.
                    _COMPOUND_PREFIXES = ("async for ", "for ", "if ", "while ", "with ", "async with ")
                    is_compound = normalized_signature.startswith(_COMPOUND_PREFIXES)

                    if is_compound:
                        indented_signature = textwrap.indent(normalized_signature, "    ")
                        wrapper_code = f"async def __run_signature_wrapper():\n{indented_signature}\n"
                    else:
                        indented_signature = textwrap.indent(normalized_signature, "        ")
                        wrapper_code = (
                            f"async def __run_signature_wrapper():\n    return (\n{indented_signature}\n    )\n"
                        )

                    LOG.debug("Executing run_signature wrapper", wrapper_code=wrapper_code)

                    # Pre-init so the success log can reference output_value
                    # when ScriptTerminationException was caught (terminated
                    # is now in script_success_statuses).
                    output_value: Any = None
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

                    workflow_run_blocks = await app.DATABASE.observer.get_workflow_run_blocks(
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
                        # Terminate() is an explicit signal to stop, not a
                        # failure to retry. `generate_script_on_terminal` is
                        # orthogonal — it gates script generation, not fallback.
                        script_success_statuses = {BlockStatus.completed, BlockStatus.terminated}

                        block_exec_duration_ms = round((time.monotonic() - block_exec_start) * 1000, 1)
                        if workflow_run_block_result.status in script_success_statuses:
                            block_executed_with_code = True
                            LOG.info(
                                "Successfully executed block with script code",
                                block_label=block.label,
                                block_status=workflow_run_block_result.status,
                                has_output=output_value is not None,
                                duration_ms=block_exec_duration_ms,
                            )
                        else:
                            # Script ran but the task/block failed (e.g., wrong xpaths for a
                            # different page layout). Treat this as a script failure: record a
                            # fallback episode and let AI retry the block.
                            block_executed_with_code = False
                            LOG.warning(
                                "Script executed but block failed, falling back to AI",
                                block_label=block.label,
                                block_status=workflow_run_block_result.status,
                                failure_reason=workflow_run_block_result.failure_reason,
                                duration_ms=block_exec_duration_ms,
                            )
                            # Reset the block result so AI fallback produces a fresh one
                            workflow_run_block_result = None

                            # Record fallback episode for adaptive caching
                            if is_adaptive_caching(workflow, workflow_run) and block.label:
                                context = skyvern_context.current()
                                fallback_episode_id, form_fields_for_episode = await self._record_fallback_episode(
                                    workflow_run=workflow_run,
                                    workflow=workflow,
                                    block=block,
                                    organization_id=organization_id,
                                    workflow_run_id=workflow_run_id,
                                    error_message=f"Script completed but block failed: {latest_block.failure_reason}",
                                    script_revision_id=context.script_revision_id if context else None,
                                    classify_result=context.last_classify_result if context else None,
                                )
                    else:
                        block_exec_duration_ms = round((time.monotonic() - block_exec_start) * 1000, 1)
                        LOG.warning(
                            "Block executed with code but no workflow run block found",
                            block_label=block.label,
                            duration_ms=block_exec_duration_ms,
                        )
                        block_executed_with_code = False
                except Exception as e:
                    block_exec_duration_ms = round((time.monotonic() - block_exec_start) * 1000, 1)
                    script_exception = e
                    LOG.warning(
                        "Failed to execute block with script code, falling back to AI",
                        block_label=block.label,
                        error_type=type(e).__name__,
                        error=str(e),
                        duration_ms=block_exec_duration_ms,
                        exc_info=True,
                    )
                    block_executed_with_code = False

                    # Record fallback episode for the script reviewer (adaptive caching)
                    if is_adaptive_caching(workflow, workflow_run) and block.label:
                        context = skyvern_context.current()
                        fallback_episode_id, form_fields_for_episode = await self._record_fallback_episode(
                            workflow_run=workflow_run,
                            workflow=workflow,
                            block=block,
                            organization_id=organization_id,
                            workflow_run_id=workflow_run_id,
                            error_message=str(e),
                            script_revision_id=context.script_revision_id if context else None,
                        )

            if not block_executed_with_code:
                # Check if this block is designated as requires_agent by the script reviewer.
                # These blocks must execute via agent even when ai_fallback=False.
                block_requires_agent = bool(
                    is_script_run
                    and block.label
                    and block.label in script_blocks_by_label
                    and script_blocks_by_label[block.label].requires_agent
                )
                # Check if this block has never been cached (e.g. from an unexecuted
                # conditional branch) or is a non-cacheable block type (goto_url,
                # for_loop, conditional, code, wait, etc.). These blocks must run
                # via agent even when ai_fallback=False.
                block_is_uncached = bool(
                    is_script_run
                    and block.label
                    and block.label not in script_blocks_by_label
                    and block.block_type in BLOCK_TYPES_THAT_SHOULD_BE_CACHED
                )
                block_is_non_cacheable = bool(
                    is_script_run and block.block_type not in BLOCK_TYPES_THAT_SHOULD_BE_CACHED
                )
                # If ai_fallback is explicitly disabled, skip the agent fallback entirely —
                # UNLESS this block requires_agent, has never been cached, or is a
                # non-cacheable block type that must always run via agent.
                if (
                    is_script_run
                    and workflow_run.ai_fallback is False
                    and not block_requires_agent
                    and not block_is_uncached
                    and not block_is_non_cacheable
                ):
                    LOG.info(
                        "ai_fallback disabled: skipping agent fallback, keeping script failure",
                        block_label=block.label,
                        failure_reason=str(workflow_run_block_result.failure_reason)[:200]
                        if workflow_run_block_result
                        else "script exception",
                    )
                else:
                    agent_reason = (
                        "requires_agent"
                        if block_requires_agent
                        else "uncached_block"
                        if block_is_uncached
                        else "non_cacheable_block_type"
                        if block_is_non_cacheable
                        else "normal"
                    )
                    LOG.info(
                        "Executing block via agent",
                        sampling=True,
                        block_label=block.label,
                        block_type=block.block_type,
                        agent_reason=agent_reason,
                    )
                    workflow_run_block_result = await block.execute_safe(
                        workflow_run_id=workflow_run_id,
                        parent_workflow_run_block_id=parent_workflow_run_block_id,
                        organization_id=organization_id,
                        browser_session_id=browser_session_id,
                    )
                    # Record that this run experienced a script → AI fallback if
                    # the agent execution we just ran was a consequence of a failed
                    # script attempt. The gate correctly excludes:
                    #   - ai_fallback=False kept-the-failure (execute_safe not reached)
                    #   - requires_agent / uncached / disable_cache / non-cacheable
                    #     routes (valid_to_run_code is False for these)
                    #   - agent-only workflows (is_script_run=False → valid_to_run_code=False)
                    # and correctly covers all three script-failure modes: script-
                    # block failed, script threw, and script-ran-but-no-block-found.
                    # Complements the task-block AI-fallback writers in `services/script_service.py`
                    # writers which handle a separate task-block AI-fallback surface.
                    # Perf: a fallback-heavy run issues N writes for N fallbacks.
                    # Typical runs have 0-3. `_merge_script_run` is idempotent at the
                    # DB layer. If observed latency regresses, add a context-scoped
                    # already-flipped cache (tracked separately).
                    await self._mark_script_fallback_triggered(
                        workflow_run_id=workflow_run_id,
                        # `valid_to_run_code` is computed as a chain of `and`s that
                        # includes `block.label` (str | None), so its static type is
                        # `Literal[''] | bool` when block.label is an empty string.
                        # The helper treats it as a pure truthiness gate; `bool(...)`
                        # narrows the type cleanly for mypy without changing runtime
                        # semantics.
                        valid_to_run_code=bool(valid_to_run_code),
                        block_executed_with_code=block_executed_with_code,
                        block_label=block.label,
                    )

                # Update fallback episode with agent actions for both success and failure.
                # Failed fallbacks are kept for triage — the reviewer will determine
                # if the failure is code-fixable.
                if fallback_episode_id and workflow_run_block_result:
                    try:
                        # None = unknown count (taskless wrb / fetch error);
                        # only a confirmed zero downgrades fallback_succeeded.
                        fallback_wrb_id = workflow_run_block_result.workflow_run_block_id
                        agent_action_count: int | None = None
                        action_summaries: list[dict] | None = None
                        if fallback_wrb_id:
                            try:
                                wrb = await app.DATABASE.observer.get_workflow_run_block(
                                    workflow_run_block_id=fallback_wrb_id,
                                    organization_id=organization_id,
                                )
                                if wrb and wrb.task_id:
                                    actions = await app.DATABASE.tasks.get_task_actions(
                                        task_id=wrb.task_id,
                                        organization_id=organization_id,
                                    )
                                    agent_action_count = len(actions)
                                    action_summaries = build_action_summaries_with_timing(actions)
                            except Exception:
                                LOG.debug(
                                    "Could not fetch rich actions for fallback episode",
                                    fallback_wrb_id=fallback_wrb_id,
                                    exc_info=True,
                                )

                        # `completed` with confirmed 0 actions = the agent's
                        # complete-verify accepting what the script's rejected.
                        fallback_succeeded = workflow_run_block_result.status == BlockStatus.completed
                        if fallback_succeeded and agent_action_count == 0:
                            fallback_succeeded = False

                        # Build agent actions summary for both success and failure
                        agent_actions_summary: dict = {
                            "block_status": str(workflow_run_block_result.status),
                            "output_value": str(workflow_run_block_result.output_parameter_value)[:500]
                            if workflow_run_block_result.output_parameter_value
                            else None,
                        }
                        if form_fields_for_episode:
                            agent_actions_summary["form_fields"] = form_fields_for_episode
                        if action_summaries is not None:
                            agent_actions_summary["actions"] = action_summaries

                        if not fallback_succeeded:
                            if workflow_run_block_result.failure_reason:
                                agent_actions_summary["failure_reason"] = str(workflow_run_block_result.failure_reason)[
                                    :2000
                                ]
                            elif workflow_run_block_result.status == BlockStatus.completed and agent_action_count == 0:
                                agent_actions_summary["failure_reason"] = script_service.VERIFIER_SWAP_FAILURE_REASON
                            LOG.info(
                                "AI fallback failed, keeping episode for triage",
                                episode_id=fallback_episode_id,
                                block_status=workflow_run_block_result.status,
                                block_label=block.label,
                                agent_action_count=agent_action_count,
                            )

                        await app.DATABASE.scripts.update_fallback_episode(
                            episode_id=fallback_episode_id,
                            organization_id=organization_id,
                            agent_actions=agent_actions_summary,
                            fallback_succeeded=fallback_succeeded,
                        )
                    except Exception:
                        LOG.warning(
                            "Failed to update fallback episode with agent actions",
                            episode_id=fallback_episode_id,
                            exc_info=True,
                        )

            # Extract branch metadata for conditional blocks
            if isinstance(block, ConditionalBlock) and workflow_run_block_result:
                branch_metadata = cast(dict[str, Any] | None, workflow_run_block_result.output_parameter_value)

                # Record conditional episode so the script reviewer can learn the
                # expression→result mapping and potentially convert it to Python code.
                # This fires both when the block requires_agent (first run) and when
                # cached code failed and agent fallback re-ran the conditional
                # (fallback_episode_id is set when the script path failed).
                if (
                    is_script_run
                    and (block_requires_agent or fallback_episode_id)
                    and workflow_run_block_result.status == BlockStatus.completed
                    and branch_metadata
                    and is_adaptive_caching(workflow, workflow_run)
                ):
                    try:
                        # Extract the branch expressions and results for the reviewer.
                        # Evaluations from ConditionalBlock.execute() stop at the first
                        # matched branch (break on match), so unevaluated branches
                        # (including the default) may be missing. We merge runtime
                        # evaluations with the full branch definitions so the script
                        # reviewer always sees every branch — this is critical for the
                        # branch-return validator which checks that generated code only
                        # returns labels/indices from the defined branches.
                        evaluations = branch_metadata.get("evaluations", [])
                        eval_by_index: dict[int, dict] = {}
                        for ev in evaluations:
                            idx = ev.get("branch_index")
                            if idx is not None:
                                eval_by_index[idx] = ev

                        expressions = []
                        if hasattr(block, "ordered_branches"):
                            for idx, b in enumerate(block.ordered_branches):
                                ev = eval_by_index.get(idx)
                                expr_info = {
                                    "original_expression": (
                                        ev.get("original_expression")
                                        if ev
                                        else (b.criteria.expression if b.criteria else None)
                                    ),
                                    "rendered_expression": ev.get("rendered_expression") if ev else None,
                                    "result": ev.get("result") if ev else None,
                                    "is_default": ev.get("is_default", b.is_default) if ev else b.is_default,
                                    "next_block_label": b.next_block_label,
                                }
                                expressions.append(expr_info)
                        else:
                            # Fallback: no ordered_branches, use evaluations as-is
                            for ev in evaluations:
                                expressions.append(
                                    {
                                        "original_expression": ev.get("original_expression"),
                                        "rendered_expression": ev.get("rendered_expression"),
                                        "result": ev.get("result"),
                                        "is_default": ev.get("is_default", False),
                                        "next_block_label": ev.get("next_block_label"),
                                    }
                                )
                        cond_context = skyvern_context.current()
                        cond_episode = await app.DATABASE.scripts.create_fallback_episode(
                            organization_id=organization_id,
                            workflow_permanent_id=workflow.workflow_permanent_id,
                            workflow_run_id=workflow_run_id,
                            block_label=block.label,
                            fallback_type="conditional_agent",
                            error_message=None,
                            script_revision_id=cond_context.script_revision_id if cond_context else None,
                            agent_actions={
                                "block_type": "conditional",
                                "branch_taken": branch_metadata.get("branch_taken"),
                                "branch_index": branch_metadata.get("branch_index"),
                                "expressions": expressions,
                            },
                        )
                        await app.DATABASE.scripts.update_fallback_episode(
                            episode_id=cond_episode.episode_id,
                            organization_id=organization_id,
                            fallback_succeeded=True,
                        )
                    except Exception:
                        LOG.warning(
                            "Failed to record conditional episode",
                            block_label=block.label,
                            exc_info=True,
                        )

            if not workflow_run_block_result:
                if script_exception is not None:
                    exc_message = str(script_exception) or "<no message>"
                    no_block_result_reason = f"Script error ({type(script_exception).__name__}): {exc_message}"
                else:
                    no_block_result_reason = "Block result is None"
                workflow_run = await self.mark_workflow_run_as_failed(
                    workflow_run_id=workflow_run_id, failure_reason=no_block_result_reason
                )
                return workflow_run, blocks_to_update, workflow_run_block_result, True, branch_metadata

            # Determine which block statuses are eligible for caching
            cacheable_statuses = {BlockStatus.completed}
            if workflow.generate_script_on_terminal:
                cacheable_statuses.add(BlockStatus.terminated)

            if (
                not block_executed_with_code
                and block.label
                and block.label not in script_blocks_by_label
                and workflow_run_block_result.status in cacheable_statuses
                and block.block_type in BLOCK_TYPES_THAT_SHOULD_BE_CACHED
                # For traditional caching (code_version < 2), only track blocks
                # for regeneration when actually running with code. Agent-mode runs
                # should not trigger regeneration — doing so creates an infinite loop
                # where every run deletes and regenerates the script because blocks
                # always execute via agent and are never in script_blocks_by_label.
                and (is_adaptive_caching(workflow, workflow_run) or is_script_run)
            ):
                blocks_to_update.add(block.label)

            # NOTE: continue_on_failure block failures are handled by the Script
            # Reviewer (triggered at end-of-run, capped at 5/day via Redis), NOT by
            # regenerating the entire script here. The fallback episode is already
            # recorded and the reviewer will patch the specific block that failed.
            # See _trigger_script_reviewer() for the capped reviewer flow.

            # Track uncached loop block children for regeneration.
            # Loop block children execute via block.py's execute_*_loop_helper(),
            # bypassing _execute_single_block. Recursively walk all nesting levels
            # so deeply nested blocks (e.g., file_download inside a double-nested
            # loop) get cached functions generated.
            if (
                isinstance(block, (ForLoopBlock, WhileLoopBlock))
                and (is_adaptive_caching(workflow, workflow_run) or is_script_run)
                and workflow_run_block_result.status in cacheable_statuses
            ):
                previous_labels = set(blocks_to_update)
                _collect_uncached_loop_children(block, script_blocks_by_label, blocks_to_update)
                new_labels = sorted(blocks_to_update - previous_labels)
                if new_labels:
                    LOG.info(
                        "Loop block child blocks marked for caching",
                        parent_label=block.label,
                        child_labels=new_labels,
                        child_count=len(new_labels),
                        workflow_run_id=workflow_run_id,
                        workflow_permanent_id=workflow.workflow_permanent_id,
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

            exception_message = get_user_facing_exception_message(e)

            failure_reason = f"{block.block_type} block failed. failure reason: {exception_message}"
            workflow_run = await self.mark_workflow_run_as_failed(
                workflow_run_id=workflow_run_id, failure_reason=failure_reason
            )
            return workflow_run, blocks_to_update, workflow_run_block_result, True, branch_metadata

    async def resolve_login_block_browser_profile_id_pre_run(
        self,
        *,
        block: Block,
        organization_id: str,
    ) -> str | None:
        """Pre-run variant: no workflow_run_id, so CREDENTIAL_ID workflow
        parameters resolve via their ``default_value`` fallback inside the
        resolver."""
        return await self._resolve_login_block_browser_profile_id(
            block=block,
            workflow_run_id=None,
            organization_id=organization_id,
            workflow_permanent_id=None,
        )

    async def _resolve_login_block_browser_profile_id(
        self,
        block: Block,
        workflow_run_id: str | None,
        organization_id: str | None,
        workflow_permanent_id: str | None,
    ) -> str | None:
        """Inspect the block-level parameters and return the browser_profile_id
        from the credential parameter bound to this specific block."""
        # A credential re-save logs in fresh so the new session persists via the normal path,
        # so it must not reuse (and boot read-only from) the credential's saved profile.
        if isinstance(block, LoginBlock) and block.skip_saved_profile:
            return None
        credential_ids = await self._resolve_login_block_credential_ids(
            block=block,
            workflow_run_id=workflow_run_id,
            organization_id=organization_id,
            workflow_permanent_id=workflow_permanent_id,
        )

        for credential_id in credential_ids:
            if not organization_id:
                continue
            try:
                db_cred = await app.DATABASE.credentials.get_credential(
                    credential_id=credential_id,
                    organization_id=organization_id,
                )
                if db_cred and db_cred.browser_profile_id:
                    # Verify the browser profile still exists before using it
                    profile = await app.DATABASE.browser_sessions.get_browser_profile(
                        profile_id=db_cred.browser_profile_id,
                        organization_id=organization_id,
                    )
                    if not profile:
                        LOG.warning(
                            "Credential has browser_profile_id but profile not found, ignoring",
                            credential_id=credential_id,
                            browser_profile_id=db_cred.browser_profile_id,
                            workflow_run_id=workflow_run_id,
                        )
                        continue
                    LOG.info(
                        "Resolved browser_profile_id from LoginBlock credential",
                        credential_id=credential_id,
                        browser_profile_id=db_cred.browser_profile_id,
                        workflow_run_id=workflow_run_id,
                    )
                    return db_cred.browser_profile_id
            except Exception:
                LOG.warning(
                    "Failed to look up credential for browser profile",
                    credential_id=credential_id,
                    workflow_run_id=workflow_run_id,
                    exc_info=True,
                )
        return None

    async def _resolve_login_block_credential_ids(
        self,
        block: Block,
        workflow_run_id: str | None,
        organization_id: str | None = None,
        workflow_permanent_id: str | None = None,
    ) -> list[str]:
        """Return credential ids bound to this block, preserving parameter order."""
        params = block.parameters

        # Pre-fetch run parameters once (used by WorkflowParameter/CREDENTIAL_ID style).
        run_param_tuples: list[tuple[Any, Any]] | None = None
        credential_ids: list[str] = []

        for param in params:
            credential_id: str | None = None

            # Style 1: CredentialParameter (has credential_id directly)
            if isinstance(param, CredentialParameter):
                credential_id = await self._resolve_credential_parameter_id(
                    parameter=param,
                    workflow_run_id=workflow_run_id,
                    organization_id=organization_id,
                    workflow_permanent_id=workflow_permanent_id,
                )

            # Style 2: WorkflowParameter with type CREDENTIAL_ID
            elif (
                isinstance(param, WorkflowParameter)
                and getattr(param, "workflow_parameter_type", None) == WorkflowParameterType.CREDENTIAL_ID
            ):
                # The credential_id is stored as the run-parameter value (or
                # falls back to default_value on the workflow parameter).
                if workflow_run_id is None:
                    run_param_tuples = []
                elif run_param_tuples is None:
                    try:
                        run_param_tuples = await app.DATABASE.workflow_runs.get_workflow_run_parameters(
                            workflow_run_id=workflow_run_id,
                        )
                    except Exception:
                        LOG.warning(
                            "Failed to fetch workflow run parameters for credential resolution",
                            workflow_run_id=workflow_run_id,
                            exc_info=True,
                        )
                        run_param_tuples = []

                for wf_param, run_param in run_param_tuples:
                    if wf_param.key == param.key:
                        if isinstance(run_param.value, str) and run_param.value:
                            credential_id = run_param.value
                        break

                # Fallback to default_value
                if not credential_id:
                    dv = getattr(param, "default_value", None)
                    if isinstance(dv, str) and dv:
                        credential_id = dv

            if not credential_id:
                continue
            credential_ids.append(credential_id)

        return credential_ids

    async def _resolve_credential_parameter_id(
        self,
        *,
        parameter: CredentialParameter,
        workflow_run_id: str | None,
        organization_id: str | None,
        workflow_permanent_id: str | None,
    ) -> str:
        if not parameter.credential_ids or not workflow_run_id or not organization_id or not workflow_permanent_id:
            return parameter.credential_id

        workflow_run_context = app.WORKFLOW_CONTEXT_MANAGER.workflow_run_contexts.get(workflow_run_id)
        if workflow_run_context:
            return await workflow_run_context.resolve_credential_parameter_id(parameter, organization_id)

        return await select_credential_for_run(
            workflow_run_id=workflow_run_id,
            organization_id=organization_id,
            workflow_permanent_id=workflow_permanent_id,
            parameter_key=parameter.key,
            credential_ids=parameter.credential_ids,
            selection_strategy=parameter.selection_strategy,
        )

    async def _apply_login_block_credential_proxy_pin(
        self,
        *,
        block: Block,
        workflow_run: WorkflowRun,
        workflow_run_id: str,
        organization_id: str | None,
    ) -> None:
        if not organization_id:
            return
        headers = dict(workflow_run.extra_http_headers or {})
        if app.AGENT_FUNCTION.has_proxy_session_extra_http_headers(headers):
            return

        credential_ids = await self._resolve_login_block_credential_ids(
            block=block,
            workflow_run_id=workflow_run_id,
            organization_id=organization_id,
            workflow_permanent_id=getattr(workflow_run, "workflow_permanent_id", None),
        )
        for credential_id in credential_ids:
            if not organization_id:
                continue
            try:
                db_cred = await app.DATABASE.credentials.get_credential(
                    credential_id=credential_id,
                    organization_id=organization_id,
                )
                if not db_cred:
                    continue
                proxy_session_id = db_cred.proxy_session_id
                proxy_location = db_cred.proxy_location
                browser_profile_id = getattr(db_cred, "browser_profile_id", None)
                if browser_profile_id:
                    profile = await app.DATABASE.browser_sessions.get_browser_profile(
                        profile_id=browser_profile_id,
                        organization_id=organization_id,
                    )
                    if profile:
                        if not profile.proxy_session_id:
                            LOG.info(
                                "Skipping LoginBlock credential proxy pin because linked browser profile is unpinned",
                                credential_id=credential_id,
                                browser_profile_id=browser_profile_id,
                                workflow_run_id=workflow_run_id,
                            )
                            return
                        proxy_session_id = profile.proxy_session_id
                        proxy_location = profile.proxy_location or ProxyLocation.RESIDENTIAL_ISP
                        LOG.info(
                            "Using linked browser profile proxy pin for LoginBlock",
                            credential_id=credential_id,
                            browser_profile_id=browser_profile_id,
                            workflow_run_id=workflow_run_id,
                        )
                if proxy_session_id:
                    proxy_location_update: ProxyLocationInput | None = None
                    if workflow_run.proxy_location is None:
                        proxy_location_update = proxy_location or ProxyLocation.RESIDENTIAL_ISP
                    if not await self._persist_proxy_pin_headers(
                        workflow_run=workflow_run,
                        headers=headers,
                        proxy_session_id=proxy_session_id,
                        proxy_location_update=proxy_location_update,
                    ):
                        return
                    LOG.info(
                        "Applied LoginBlock credential proxy pin to workflow run",
                        credential_id=credential_id,
                        workflow_run_id=workflow_run_id,
                        proxy_location=str(workflow_run.proxy_location),
                    )
                    return
            except Exception:
                LOG.error(
                    "Failed to apply LoginBlock credential proxy pin",
                    credential_id=credential_id,
                    workflow_run_id=workflow_run_id,
                    exc_info=True,
                )

    async def _persist_proxy_pin_headers(
        self,
        *,
        workflow_run: WorkflowRun,
        headers: dict[str, str],
        proxy_session_id: str,
        proxy_location_update: ProxyLocationInput | None = None,
    ) -> bool:
        updated_headers = app.AGENT_FUNCTION.merge_proxy_session_extra_http_headers(headers, proxy_session_id)
        # The OSS AgentFunction stub returns the original headers; only persist when cloud injected a pin.
        if updated_headers == headers:
            return False
        update_kwargs: dict[str, Any] = {
            "workflow_run_id": workflow_run.workflow_run_id,
            "extra_http_headers": updated_headers,
        }
        if proxy_location_update is not None:
            update_kwargs["proxy_location"] = proxy_location_update
        await app.DATABASE.workflow_runs.update_workflow_run(**update_kwargs)
        workflow_run.extra_http_headers = updated_headers
        if proxy_location_update is not None:
            workflow_run.proxy_location = proxy_location_update
        return True

    async def _evaluate_debug_session_profile_decision(
        self,
        *,
        workflow_run: WorkflowRun,
        browser_session_id: str | None,
        resolved_browser_profile_id: str,
        organization_id: str,
    ) -> DebugSessionProfileDecision:
        """Asymmetric LoginBlock credential-profile decision: debug-session runs
        attach the visible PBS only when its saved profile matches the credential
        profile; mismatches surface a reason for downstream warning + fall-through."""
        is_debug_run = workflow_run.debug_session_id is not None
        if not is_debug_run:
            return DebugSessionProfileDecision(
                attach_browser_session_id=None,
                incompatible_reason=None,
            )

        if not browser_session_id:
            return DebugSessionProfileDecision(
                attach_browser_session_id=None,
                incompatible_reason=None,
            )

        try:
            pbs = await app.DATABASE.browser_sessions.get_persistent_browser_session(
                browser_session_id,
                organization_id,
            )
        except Exception:
            # Fail safe: treat as no profile so the decision lands in
            # pbs_no_profile rather than crashing the workflow run.
            LOG.warning(
                "Persistent browser session lookup failed during debug LoginBlock decision; treating as no profile",
                workflow_run_id=workflow_run.workflow_run_id,
                browser_session_id=browser_session_id,
                organization_id=organization_id,
                exc_info=True,
            )
            pbs = None
        pbs_profile_id = pbs.browser_profile_id if pbs is not None else None

        if pbs_profile_id == resolved_browser_profile_id:
            return DebugSessionProfileDecision(
                attach_browser_session_id=browser_session_id,
                incompatible_reason=None,
            )

        reason = (
            DEBUG_SESSION_PROFILE_REASON_NO_PROFILE
            if pbs_profile_id is None
            else DEBUG_SESSION_PROFILE_REASON_DIFFERENT
        )
        return DebugSessionProfileDecision(
            attach_browser_session_id=browser_session_id,
            incompatible_reason=reason,
        )

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
            # Run-level outcome, recorded as the run's failure_reason below; not a platform fault.
            LOG.warning(
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
                task_failure_category = (
                    block_result.output_parameter_value.get("failure_category")
                    if isinstance(block_result.output_parameter_value, dict)
                    else None
                )
                workflow_run = await self.mark_workflow_run_as_failed(
                    workflow_run_id=workflow_run_id,
                    failure_reason=failure_reason,
                    failure_category=task_failure_category,
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
                task_failure_category = (
                    block_result.output_parameter_value.get("failure_category")
                    if isinstance(block_result.output_parameter_value, dict)
                    else None
                )
                workflow_run = await self.mark_workflow_run_as_terminated(
                    workflow_run_id=workflow_run_id,
                    failure_reason=failure_reason,
                    failure_category=task_failure_category,
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
                task_failure_category = (
                    block_result.output_parameter_value.get("failure_category")
                    if isinstance(block_result.output_parameter_value, dict)
                    else None
                )
                workflow_run = await self.mark_workflow_run_as_failed(
                    workflow_run_id=workflow_run_id,
                    failure_reason=failure_reason,
                    failure_category=task_failure_category,
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
        skip_sequential_defaulting: bool = False,
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
        if not skip_sequential_defaulting:
            has_conditional_blocks = any(isinstance(block, ConditionalBlock) for block in all_blocks)
            if not has_conditional_blocks:
                for idx, block in enumerate(blocks[:-1]):
                    if default_next_map.get(block.label) is None:
                        default_next_map[block.label] = blocks[idx + 1].label

        # SKY-8571: connect conditional branch terminals to the conditional's merge-point successor.
        resolve_conditional_merge_edges(all_blocks, label_to_block, default_next_map)

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
            raise InvalidWorkflowDefinition(
                "Circular reference detected: every block is the target of another block's next_block_label,"
                " so there is no starting block."
                " At least one block must not be the target of any next_block_label or branch condition."
            )
        if len(roots) > 1:
            raise InvalidWorkflowDefinition(
                f"Disconnected blocks detected: blocks ({', '.join(sorted(roots))}) are not reachable from any"
                " other block. Every block must be reachable from the first block through next_block_label or"
                " conditional branch references."
                " Either connect them by setting another block's next_block_label to point to them, or remove them."
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
            raise InvalidWorkflowDefinition(
                "Circular reference detected: some blocks form a loop through their next_block_label references,"
                " causing an infinite cycle."
                " Ensure that following next_block_label from any block eventually reaches a block"
                " with next_block_label set to null."
            )

        return roots[0], label_to_block, default_next_map

    def validate_workflow_block_graph(self, workflow_definition: WorkflowDefinition) -> None:
        """Validate the block graph before persisting.

        Detects orphaned blocks, circular references, and dangling next_block_label references.
        Recursively validates nested ForLoopBlock graphs at all nesting depths.
        Raises InvalidWorkflowDefinition (422) on validation failure.

        For v2 workflow definitions (blocks have explicit next_block_label), sequential
        defaulting is skipped so that disconnected subgraphs are detected.
        v1 workflows (no next_block_label on any block) are skipped since they use
        purely sequential execution.
        """
        blocks = list(workflow_definition.blocks)
        if not blocks:
            return

        # v1 workflows have no explicit next_block_label and run sequentially — skip DAG validation
        version = workflow_definition.version or 1
        if version < 2:
            return

        finally_block_label = workflow_definition.finally_block_label
        if finally_block_label:
            blocks = self._strip_finally_block_references(blocks, finally_block_label)

        if not blocks:
            return

        self._build_workflow_graph(blocks, skip_sequential_defaulting=True)

        # Recursively validate nested ForLoopBlock graphs (including the finally block)
        self._validate_nested_blocks(workflow_definition.blocks)

    @staticmethod
    def _validate_nested_blocks(blocks: list[BlockTypeVar]) -> None:
        """Recursively validate loop block graphs at all nesting depths."""
        for block in blocks:
            if isinstance(block, (ForLoopBlock, WhileLoopBlock)):
                block.validate_loop_blocks()

    @staticmethod
    def _validate_payload_templates(workflow_definition: WorkflowDefinition) -> None:
        """Reject workflow_trigger blocks whose payload has malformed Jinja2 templates.

        Surfaces the same JSON-pointer key path + raw template that the runtime
        PayloadTemplateRenderError reports - shifted left from execute() to save().
        """
        for block in workflow_definition.blocks:
            if isinstance(block, WorkflowTriggerBlock):
                block.validate_payload_templates()

    async def create_workflow(
        self,
        organization_id: str,
        title: str,
        workflow_definition: WorkflowDefinition,
        description: str | None = None,
        proxy_location: ProxyLocationInput = None,
        max_screenshot_scrolling_times: int | None = None,
        max_elapsed_time_minutes: int | None = None,
        webhook_callback_url: str | None = None,
        totp_verification_url: str | None = None,
        totp_identifier: str | None = None,
        persist_browser_session: bool = False,
        pin_saved_session_ip: bool = False,
        browser_profile_id: str | None = None,
        browser_profile_key: str | None = None,
        model: dict[str, Any] | None = None,
        workflow_permanent_id: str | None = None,
        version: int | None = None,
        is_saved_task: bool = False,
        status: WorkflowStatus = WorkflowStatus.published,
        extra_http_headers: dict[str, str] | None = None,
        cdp_connect_headers: dict[str, str] | None = None,
        run_with: str | None = None,
        cache_key: str | None = None,
        ai_fallback: bool | None = None,
        run_sequentially: bool = False,
        sequential_key: str | None = None,
        folder_id: str | None = None,
        adaptive_caching: bool = False,
        enable_self_healing: bool = False,
        code_version: int | None = None,
        generate_script_on_terminal: bool = False,
        created_by: str | None = None,
        edited_by: str | None = None,
    ) -> Workflow:
        try:
            return await app.DATABASE.workflows.create_workflow(
                title=title,
                workflow_definition=workflow_definition.model_dump(mode="json"),
                organization_id=organization_id,
                description=description,
                proxy_location=proxy_location,
                webhook_callback_url=webhook_callback_url,
                max_screenshot_scrolling_times=max_screenshot_scrolling_times,
                max_elapsed_time_minutes=max_elapsed_time_minutes,
                totp_verification_url=totp_verification_url,
                totp_identifier=totp_identifier,
                persist_browser_session=persist_browser_session,
                pin_saved_session_ip=pin_saved_session_ip,
                browser_profile_id=browser_profile_id,
                browser_profile_key=browser_profile_key,
                model=model,
                workflow_permanent_id=workflow_permanent_id,
                version=version,
                is_saved_task=is_saved_task,
                status=status,
                extra_http_headers=extra_http_headers,
                cdp_connect_headers=cdp_connect_headers,
                run_with=run_with,
                cache_key=cache_key,
                ai_fallback=True if ai_fallback is None else ai_fallback,
                run_sequentially=run_sequentially,
                sequential_key=sequential_key,
                folder_id=folder_id,
                adaptive_caching=adaptive_caching,
                enable_self_healing=enable_self_healing,
                code_version=code_version,
                generate_script_on_terminal=generate_script_on_terminal,
                created_by=created_by,
                edited_by=edited_by,
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
        cdp_connect_headers: dict[str, str] | None = None,
        max_iterations: int | None = None,
        max_steps: int | None = None,
        status: WorkflowStatus = WorkflowStatus.auto_generated,
        run_with: str | None = None,
        ai_fallback: bool = True,
        task_version: Literal["v1", "v2"] = "v1",
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
            if url:
                try:
                    url = validate_url_with_blocked_host_check(url) or ""
                except BlockedHost:
                    raise
                except Exception:
                    LOG.warning("LLM returned invalid URL in generate-task response, falling back to empty", url=url)
                    url = ""

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

        # Track task_generation for observability (SKY-8842)
        try:
            user_prompt_hash = sha256(user_prompt.encode("utf-8")).hexdigest()
            v1_kwargs: dict[str, Any] = {}
            if task_version == "v1":
                v1_kwargs = {
                    "url": url,
                    "navigation_goal": navigation_goal,
                    "navigation_payload": task_response.get("navigation_payload"),
                    "data_extraction_goal": data_extraction_goal,
                    "suggested_title": task_response.get("suggested_title"),
                    "llm": settings.LLM_KEY,
                    "llm_prompt": task_prompt,
                    "llm_response": str(task_response),
                }
            await app.DATABASE.workflow_params.create_task_generation(
                organization_id=organization.organization_id,
                user_prompt=user_prompt,
                user_prompt_hash=user_prompt_hash,
                **v1_kwargs,
            )
        except Exception:
            LOG.warning(
                "Failed to create task_generation record",
                exc_info=True,
                organization_id=organization.organization_id,
            )

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
            cdp_connect_headers=cdp_connect_headers,
            status=status,
            run_with=run_with,
            ai_fallback=ai_fallback,
        )

        return new_workflow

    async def get_workflow(self, workflow_id: str, organization_id: str | None = None) -> Workflow:
        workflow = await app.DATABASE.workflows.get_workflow(workflow_id=workflow_id, organization_id=organization_id)
        if not workflow:
            raise WorkflowNotFound(workflow_id=workflow_id)
        return workflow

    async def get_workflow_by_permanent_id(
        self,
        workflow_permanent_id: str,
        organization_id: str | None = None,
        version: int | None = None,
        filter_deleted: bool = True,
    ) -> Workflow:
        workflow = await app.DATABASE.workflows.get_workflow_by_permanent_id(
            workflow_permanent_id,
            organization_id=organization_id,
            version=version,
            filter_deleted=filter_deleted,
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
            await app.DATABASE.workflows.add_workflow_template(
                workflow_permanent_id=workflow_permanent_id,
                organization_id=organization_id,
            )
        else:
            await app.DATABASE.workflows.remove_workflow_template(
                workflow_permanent_id=workflow_permanent_id,
                organization_id=organization_id,
            )

        return {"workflow_permanent_id": workflow_permanent_id, "is_template": is_template}

    async def get_workflow_versions_by_permanent_id(
        self,
        workflow_permanent_id: str,
        organization_id: str | None = None,
        filter_deleted: bool = True,
    ) -> list[Workflow]:
        """
        Get all versions of a workflow by its permanent ID.
        Returns an empty list if no workflow is found with that permanent ID.
        """
        workflows = await app.DATABASE.workflows.get_workflow_versions_by_permanent_id(
            workflow_permanent_id,
            organization_id=organization_id,
            filter_deleted=filter_deleted,
        )
        return workflows

    async def get_workflow_by_workflow_run_id(
        self,
        workflow_run_id: str,
        organization_id: str | None = None,
        filter_deleted: bool = True,
    ) -> Workflow:
        workflow = await app.DATABASE.workflows.get_workflow_for_workflow_run(
            workflow_run_id,
            organization_id=organization_id,
            filter_deleted=filter_deleted,
        )

        if not workflow:
            raise WorkflowNotFoundForWorkflowRun(workflow_run_id=workflow_run_id)

        return workflow

    async def get_block_outputs_for_debug_session(
        self,
        workflow_permanent_id: str,
        user_id: str,
        organization_id: str,
        filter_deleted: bool = True,
        version: int | None = None,
    ) -> dict[str, dict[str, Any]]:
        workflow = await app.DATABASE.workflows.get_workflow_by_permanent_id(
            workflow_permanent_id,
            organization_id=organization_id,
            version=version,
            filter_deleted=filter_deleted,
        )

        if not workflow:
            raise WorkflowNotFound(workflow_permanent_id=workflow_permanent_id, version=version)

        labels_to_outputs: dict[str, BlockOutputParameter] = {}

        for block in workflow.workflow_definition.blocks:
            label = block.label

            block_run = await app.DATABASE.debug.get_latest_completed_block_run(
                organization_id=organization_id,
                user_id=user_id,
                block_label=label,
                workflow_permanent_id=workflow_permanent_id,
            )

            if not block_run:
                continue

            output_parameter = await app.DATABASE.workflow_runs.get_workflow_run_output_parameter_by_id(
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
        return await app.DATABASE.workflows.get_workflows_by_permanent_ids(
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
        workflow_tags: list[tuple[str | None, str | None]] | None = None,
    ) -> list[Workflow]:
        """
        Get all workflows with the latest version for the organization.

        Args:
            search_key: Unified search term for title, folder name, and parameter metadata.
            folder_id: Filter workflows by folder ID.
            workflow_tags: tag filter terms — exact (key, value), group-only (key, None),
                or label-only (None, value). AND across distinct terms; OR within a key.
        """
        return await app.DATABASE.workflows.get_workflows_by_organization_id(
            organization_id=organization_id,
            page=page,
            page_size=page_size,
            only_saved_tasks=only_saved_tasks,
            only_workflows=only_workflows,
            only_templates=only_templates,
            search_key=search_key,
            folder_id=folder_id,
            statuses=statuses,
            workflow_tags=workflow_tags,
        )

    async def update_workflow_definition(
        self,
        workflow_id: str,
        organization_id: str | None = None,
        title: str | None = None,
        description: str | None = None,
        workflow_definition: WorkflowDefinition | None = None,
        proxy_location: ProxyLocationInput | object = _UNSET,
        webhook_callback_url: str | None | object = _UNSET,
        totp_verification_url: str | None | object = _UNSET,
        totp_identifier: str | None | object = _UNSET,
        persist_browser_session: bool | None = None,
        pin_saved_session_ip: bool | None = None,
        browser_profile_id: str | None | object = _UNSET,
        browser_profile_key: str | None | object = _UNSET,
        model: dict[str, Any] | None | object = _UNSET,
        max_screenshot_scrolling_times: int | None | object = _UNSET,
        max_elapsed_time_minutes: int | None | object = _UNSET,
        extra_http_headers: dict[str, str] | None | object = _UNSET,
        cdp_connect_headers: dict[str, str] | None | object = _UNSET,
        run_with: str | None = None,
        ai_fallback: bool | None = None,
        cache_key: str | None = None,
        adaptive_caching: bool | object = _UNSET,
        enable_self_healing: bool | object = _UNSET,
        code_version: int | None | object = _UNSET,
        run_sequentially: bool | None = None,
        sequential_key: str | None | object = _UNSET,
        created_by: str | None | object = _UNSET,
        edited_by: str | None | object = _UNSET,
    ) -> Workflow:
        if workflow_definition is not None:
            if organization_id is not None:
                organization = await app.DATABASE.organizations.get_organization(organization_id=organization_id)
                if organization is not None:
                    await self._validate_and_normalize_credential_rotation_parameters(
                        workflow_definition.parameters,
                        organization,
                    )
            updated_workflow = await app.DATABASE.workflows.update_workflow_and_reconcile_definition_params(
                workflow_id=workflow_id,
                title=title,
                organization_id=organization_id,
                description=description,
                workflow_definition=workflow_definition,
                proxy_location=proxy_location,
                webhook_callback_url=webhook_callback_url,
                totp_verification_url=totp_verification_url,
                totp_identifier=totp_identifier,
                persist_browser_session=persist_browser_session,
                pin_saved_session_ip=pin_saved_session_ip,
                browser_profile_id=browser_profile_id,
                browser_profile_key=browser_profile_key,
                model=model,
                max_screenshot_scrolling_times=max_screenshot_scrolling_times,
                max_elapsed_time_minutes=max_elapsed_time_minutes,
                extra_http_headers=extra_http_headers,
                cdp_connect_headers=cdp_connect_headers,
                run_with=run_with,
                ai_fallback=ai_fallback,
                cache_key=cache_key,
                adaptive_caching=adaptive_caching,
                enable_self_healing=enable_self_healing,
                code_version=code_version,
                run_sequentially=run_sequentially,
                sequential_key=sequential_key,
                created_by=created_by,
                edited_by=edited_by,
            )
        else:
            updated_workflow = await app.DATABASE.workflows.update_workflow(
                workflow_id=workflow_id,
                title=title,
                organization_id=organization_id,
                description=description,
                workflow_definition=None,
                proxy_location=proxy_location,
                webhook_callback_url=webhook_callback_url,
                totp_verification_url=totp_verification_url,
                totp_identifier=totp_identifier,
                persist_browser_session=persist_browser_session,
                pin_saved_session_ip=pin_saved_session_ip,
                browser_profile_id=browser_profile_id,
                browser_profile_key=browser_profile_key,
                model=model,
                max_screenshot_scrolling_times=max_screenshot_scrolling_times,
                max_elapsed_time_minutes=max_elapsed_time_minutes,
                extra_http_headers=extra_http_headers,
                cdp_connect_headers=cdp_connect_headers,
                run_with=run_with,
                ai_fallback=ai_fallback,
                cache_key=cache_key,
                adaptive_caching=adaptive_caching,
                enable_self_healing=enable_self_healing,
                code_version=code_version,
                run_sequentially=run_sequentially,
                sequential_key=sequential_key,
                created_by=created_by,
                edited_by=edited_by,
            )

        _edited_by: str | None = cast("str | None", edited_by) if edited_by is not _UNSET else None
        task = asyncio.create_task(
            app.AGENT_FUNCTION.on_workflow_saved(
                organization_id=updated_workflow.organization_id,
                edited_by=_edited_by,
            ),
        )
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

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

        previous_valid_workflow = await app.DATABASE.workflows.get_workflow_by_permanent_id(
            workflow_permanent_id=workflow.workflow_permanent_id,
            organization_id=organization_id,
            filter_deleted=True,
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
            candidates = await app.DATABASE.scripts.get_workflow_scripts_by_permanent_id(
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
                    await app.DATABASE.scripts.delete_workflow_scripts_by_permanent_id(
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
        # Delete workflow and schedules in one DB transaction so we do not leave
        # the workflow active if a process exits between separate commits.
        deleted_schedule_ids = await app.DATABASE.workflows.soft_delete_workflow_and_schedules_by_permanent_id(
            workflow_permanent_id=workflow_permanent_id,
            organization_id=organization_id,
        )
        if deleted_schedule_ids:
            LOG.info(
                "Cascade-deleted schedules during workflow deletion",
                workflow_permanent_id=workflow_permanent_id,
                organization_id=organization_id,
                deleted_schedule_ids=deleted_schedule_ids,
                count=len(deleted_schedule_ids),
            )

    async def delete_workflow_by_id(
        self,
        workflow_id: str,
        organization_id: str,
    ) -> None:
        # This path is rollback-only for a single workflow version created during
        # save/update flows. It must stay version-scoped and non-cascading because
        # schedules belong to the permanent workflow and should remain attached to
        # the previously valid version if the new version creation fails.
        await app.DATABASE.workflows.soft_delete_workflow_by_id(
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
        return await app.DATABASE.workflow_runs.get_workflow_runs(
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
        return await app.DATABASE.workflow_runs.get_workflow_runs_count(
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
        exclude_child_runs: bool = False,
        created_at_start: datetime | None = None,
        created_at_end: datetime | None = None,
    ) -> list[WorkflowRun]:
        return await app.DATABASE.workflow_runs.get_workflow_runs_for_workflow_permanent_id(
            workflow_permanent_id=workflow_permanent_id,
            organization_id=organization_id,
            page=page,
            page_size=page_size,
            status=status,
            search_key=search_key,
            error_code=error_code,
            exclude_child_runs=exclude_child_runs,
            created_at_start=created_at_start,
            created_at_end=created_at_end,
        )

    async def get_workflow_runs_for_browser_session(
        self,
        browser_session_id: str,
        organization_id: str,
        page: int = 1,
        page_size: int = 10,
    ) -> list[WorkflowRun]:
        return await app.DATABASE.workflow_runs.get_workflow_runs_for_browser_session(
            browser_session_id=browser_session_id,
            organization_id=organization_id,
            page=page,
            page_size=page_size,
        )

    async def create_workflow_run(
        self,
        workflow_request: WorkflowRequestBody,
        workflow_permanent_id: str,
        workflow_id: str,
        organization_id: str,
        parent_workflow_run_id: str | None = None,
        sequential_key: str | None = None,
        max_elapsed_time_minutes: int | None = None,
        debug_session_id: str | None = None,
        code_gen: bool | None = None,
        workflow_run_id: str | None = None,
        trigger_type: WorkflowRunTriggerType | None = None,
        workflow_schedule_id: str | None = None,
        ignore_inherited_workflow_system_prompt: bool = False,
        copilot_session_id: str | None = None,
    ) -> WorkflowRun:
        # validate the browser session or profile id
        browser_profile_id = workflow_request.browser_profile_id
        if workflow_request.browser_session_id:
            browser_session = await app.DATABASE.browser_sessions.get_persistent_browser_session(
                session_id=workflow_request.browser_session_id,
                organization_id=organization_id,
            )
            if not browser_session:
                raise BrowserSessionNotFound(browser_session_id=workflow_request.browser_session_id)
            # Auto-propagate profile from session when not explicitly provided
            if not browser_profile_id and browser_session.browser_profile_id:
                browser_profile_id = browser_session.browser_profile_id
                LOG.info(
                    "Auto-propagated browser_profile_id from browser session",
                    browser_session_id=workflow_request.browser_session_id,
                    browser_profile_id=browser_profile_id,
                )

        if browser_profile_id:
            browser_profile = await app.DATABASE.browser_sessions.get_browser_profile(
                browser_profile_id,
                organization_id=organization_id,
            )
            if not browser_profile:
                # If the profile was auto-propagated from session but has been deleted, skip it
                if browser_profile_id != workflow_request.browser_profile_id:
                    LOG.warning(
                        "Browser session has browser_profile_id but profile not found, ignoring",
                        browser_session_id=workflow_request.browser_session_id,
                        browser_profile_id=browser_profile_id,
                    )
                    browser_profile_id = None
                else:
                    raise BrowserProfileNotFound(
                        profile_id=browser_profile_id,
                        organization_id=organization_id,
                    )

        # Check if this workflow/org should use browser sessions (anti-bot detection mitigation)
        browser_session_id = workflow_request.browser_session_id
        if not browser_session_id:
            force_browser_session = await app.EXPERIMENTATION_PROVIDER.is_feature_enabled_cached(
                "FORCE_BROWSER_SESSION",
                workflow_permanent_id,
                properties={
                    "organization_id": organization_id,
                    "workflow_permanent_id": workflow_permanent_id,
                },
            )
            if force_browser_session:
                workflow = await self.get_workflow(workflow_id=workflow_id)
                # Forced session creation happens before setup_workflow_run persists
                # run-level credential selections. This validates override inputs
                # before best-effort profile-key rendering, then setup_workflow_run
                # persists the same override after the workflow_run_id exists.
                run_credential_parameter_overrides = self._get_run_credential_parameter_overrides(
                    workflow=workflow,
                    request_data=workflow_request.data,
                )
                workflow_run = await app.DATABASE.workflow_runs.create_workflow_run(
                    workflow_permanent_id=workflow_permanent_id,
                    workflow_id=workflow_id,
                    organization_id=organization_id,
                    browser_session_id=workflow_request.browser_session_id,
                    browser_profile_id=browser_profile_id,
                    proxy_location=workflow_request.proxy_location,
                    webhook_callback_url=workflow_request.webhook_callback_url,
                    totp_verification_url=workflow_request.totp_verification_url,
                    totp_identifier=workflow_request.totp_identifier,
                    parent_workflow_run_id=parent_workflow_run_id,
                    max_screenshot_scrolling_times=workflow_request.max_screenshot_scrolls,
                    max_elapsed_time_minutes=max_elapsed_time_minutes,
                    extra_http_headers=workflow_request.extra_http_headers,
                    cdp_connect_headers=workflow_request.cdp_connect_headers,
                    browser_address=workflow_request.browser_address,
                    sequential_key=sequential_key,
                    run_with=workflow_request.run_with,
                    debug_session_id=debug_session_id,
                    ai_fallback=workflow_request.ai_fallback,
                    code_gen=code_gen,
                    workflow_run_id=workflow_run_id,
                    trigger_type=trigger_type,
                    workflow_schedule_id=workflow_schedule_id,
                    ignore_inherited_workflow_system_prompt=ignore_inherited_workflow_system_prompt,
                    copilot_session_id=copilot_session_id,
                )
                LOG.info(
                    "Force-creating browser session for workflow run",
                    workflow_permanent_id=workflow_permanent_id,
                    workflow_run_id=workflow_run.workflow_run_id,
                    organization_id=organization_id,
                )
                forced_browser_profile_id = None
                effective_proxy_location = workflow_request.proxy_location
                pin_required = False
                try:
                    # pin_required must be known before any awaited call that can raise, so the
                    # except path never falls through to creating an unprofiled pinned session.
                    effective_proxy_location = (
                        workflow_request.proxy_location
                        if workflow_request.proxy_location is not None
                        else workflow.proxy_location
                    )
                    pin_required = (
                        workflow.persist_browser_session
                        and workflow.pin_saved_session_ip
                        and should_generate_proxy_session_id(effective_proxy_location)
                    )
                    # Rotating credential profile keys need the persisted run id before rendering.
                    rotating_credential_selections = await self._select_rotating_credential_parameters_for_render(
                        workflow=workflow,
                        workflow_run=workflow_run,
                        organization_id=organization_id,
                        credential_parameter_overrides=run_credential_parameter_overrides,
                    )
                    forced_browser_profile_id = await self._resolve_managed_browser_profile_for_run_request(
                        workflow=workflow,
                        organization_id=organization_id,
                        workflow_request=workflow_request,
                        effective_proxy_location=effective_proxy_location,
                        extra_parameter_values=rotating_credential_selections,
                    )
                except Exception:
                    LOG.warning(
                        "Failed to resolve managed browser profile for forced browser session",
                        workflow_permanent_id=workflow_permanent_id,
                        workflow_id=workflow_id,
                        workflow_run_id=workflow_run.workflow_run_id,
                        organization_id=organization_id,
                        exc_info=True,
                    )
                if pin_required and forced_browser_profile_id is None:
                    LOG.info(
                        "Skipping forced browser session without managed browser profile for pinned workflow",
                        workflow_permanent_id=workflow_permanent_id,
                        workflow_id=workflow_id,
                        workflow_run_id=workflow_run.workflow_run_id,
                        organization_id=organization_id,
                    )
                    browser_session = None
                else:
                    try:
                        browser_session = await app.PERSISTENT_SESSIONS_MANAGER.create_session(
                            organization_id=organization_id,
                            proxy_location=workflow_request.proxy_location,
                            timeout_minutes=60,  # 60 minutes default timeout for forced browser sessions
                            runnable_type=FORCED_WORKFLOW_SESSION_RUNNABLE_TYPE,
                            browser_profile_id=forced_browser_profile_id,
                            inherit_profile_proxy=True,
                        )
                        browser_session_id = browser_session.persistent_browser_session_id
                        LOG.info(
                            "Browser session created for workflow run",
                            workflow_permanent_id=workflow_permanent_id,
                            workflow_run_id=workflow_run.workflow_run_id,
                            browser_session_id=browser_session_id,
                        )
                        workflow_run = await app.DATABASE.workflow_runs.update_workflow_run(
                            workflow_run_id=workflow_run.workflow_run_id,
                            browser_session_id=browser_session_id,
                        )
                    except Exception:
                        LOG.warning(
                            "Failed to force-create browser session for workflow run",
                            workflow_permanent_id=workflow_permanent_id,
                            workflow_id=workflow_id,
                            workflow_run_id=workflow_run.workflow_run_id,
                            organization_id=organization_id,
                            exc_info=True,
                        )

                return workflow_run

        return await app.DATABASE.workflow_runs.create_workflow_run(
            workflow_permanent_id=workflow_permanent_id,
            workflow_id=workflow_id,
            organization_id=organization_id,
            browser_session_id=browser_session_id,
            browser_profile_id=browser_profile_id,
            proxy_location=workflow_request.proxy_location,
            webhook_callback_url=workflow_request.webhook_callback_url,
            totp_verification_url=workflow_request.totp_verification_url,
            totp_identifier=workflow_request.totp_identifier,
            parent_workflow_run_id=parent_workflow_run_id,
            max_screenshot_scrolling_times=workflow_request.max_screenshot_scrolls,
            max_elapsed_time_minutes=max_elapsed_time_minutes,
            extra_http_headers=workflow_request.extra_http_headers,
            cdp_connect_headers=workflow_request.cdp_connect_headers,
            browser_address=workflow_request.browser_address,
            sequential_key=sequential_key,
            run_with=workflow_request.run_with,
            debug_session_id=debug_session_id,
            ai_fallback=workflow_request.ai_fallback,
            code_gen=code_gen,
            workflow_run_id=workflow_run_id,
            trigger_type=trigger_type,
            workflow_schedule_id=workflow_schedule_id,
            ignore_inherited_workflow_system_prompt=ignore_inherited_workflow_system_prompt,
            copilot_session_id=copilot_session_id,
        )

    async def _cascade_child_entities_on_terminal(self, workflow_run_id: str, status: WorkflowRunStatus) -> None:
        if status != WorkflowRunStatus.timed_out:
            return

        try:
            await self._do_cascade_child_entities(workflow_run_id, status)
        except Exception:
            LOG.exception("Failed to cascade child entity status", workflow_run_id=workflow_run_id)

    async def _do_cascade_child_entities(self, workflow_run_id: str, status: WorkflowRunStatus) -> None:
        block_status = BLOCK_STATUS_MAP[status]
        task_status = TASK_STATUS_MAP[status]
        step_status = STEP_STATUS_MAP[status]
        failure_reason = f"Workflow run {status.value}: child entity cascade cleanup."

        blocks_updated = await app.DATABASE.observer.bulk_update_workflow_run_blocks_by_workflow_run_id(
            workflow_run_id=workflow_run_id,
            new_status=block_status.value,
            only_if_status_in=NONFINAL_BLOCK_STATUSES,
            failure_reason=failure_reason,
        )
        tasks_updated = await app.DATABASE.tasks.bulk_update_tasks_by_workflow_run_ids(
            workflow_run_ids=[workflow_run_id],
            new_status=task_status,
            only_if_status_in=[TaskStatus.created, TaskStatus.queued, TaskStatus.running],
            failure_reason=failure_reason,
        )
        steps_updated = await app.DATABASE.tasks.bulk_update_steps_by_workflow_run_ids(
            workflow_run_ids=[workflow_run_id],
            new_status=step_status,
            only_if_status_in=[StepStatus.created, StepStatus.running],
        )
        if blocks_updated or tasks_updated or steps_updated:
            LOG.info(
                "Cascaded terminal status to child entities",
                workflow_run_id=workflow_run_id,
                workflow_run_status=status,
                block_target_status=block_status,
                task_target_status=task_status,
                step_target_status=step_status,
                blocks_updated=blocks_updated,
                tasks_updated=tasks_updated,
                steps_updated=steps_updated,
            )

    async def _update_workflow_run_status(
        self,
        workflow_run_id: str,
        status: WorkflowRunStatus,
        failure_reason: str | None = None,
        run_with: str | None = None,
        ai_fallback: bool | None = None,
        failure_category: list[dict] | None = None,
    ) -> WorkflowRun:
        workflow_run = await app.DATABASE.workflow_runs.update_workflow_run(
            workflow_run_id=workflow_run_id,
            status=status,
            failure_reason=failure_reason,
            run_with=run_with,
            ai_fallback=ai_fallback,
            failure_category=failure_category,
        )
        if status.is_final():
            # Free extraction-cache entries for this run.
            extraction_cache.clear_workflow_run(workflow_run_id)
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
                trigger_type=workflow_run.trigger_type,
                workflow_schedule_id=workflow_run.workflow_schedule_id,
            )
            await self._apply_completion_run_tags_best_effort(workflow_run)
            run_completed_task = asyncio.create_task(
                app.AGENT_FUNCTION.on_workflow_run_completed(
                    organization_id=workflow_run.organization_id,
                    workflow_id=workflow_run.workflow_id,
                    status=status,
                ),
            )
            self._background_tasks.add(run_completed_task)
            run_completed_task.add_done_callback(self._background_tasks.discard)
        # Best-effort fire-and-forget write-through to task_runs table.
        # Runs off the hot path so workflow status transitions stay fast.
        bg = asyncio.create_task(
            self._sync_task_run_from_workflow_run(workflow_run, workflow_run_id, status),
        )
        self._background_tasks.add(bg)
        bg.add_done_callback(self._background_tasks.discard)

        return workflow_run

    async def _sync_task_run_from_workflow_run(
        self,
        workflow_run: WorkflowRun,
        workflow_run_id: str,
        status: WorkflowRunStatus,
    ) -> None:
        """Fire-and-forget: propagate workflow_run status to task_runs."""
        try:
            await app.DATABASE.tasks.sync_task_run_status(
                organization_id=workflow_run.organization_id,
                run_id=workflow_run_id,
                status=status.value,
                started_at=workflow_run.started_at,
                finished_at=workflow_run.finished_at,
            )
            # Also sync task_v2 if this workflow_run backs an observer_cruise
            task_v2 = await app.DATABASE.observer.get_task_v2_by_workflow_run_id(
                workflow_run_id=workflow_run_id,
                organization_id=workflow_run.organization_id,
            )
            if task_v2:
                await app.DATABASE.tasks.sync_task_run_status(
                    organization_id=workflow_run.organization_id,
                    run_id=task_v2.observer_cruise_id,
                    status=status.value,
                    started_at=workflow_run.started_at,
                    finished_at=workflow_run.finished_at,
                )
        except Exception:
            LOG.warning(
                "Failed to sync task_run status from workflow_run",
                workflow_run_id=workflow_run_id,
                exc_info=True,
            )

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
            workflow_run = await self._update_workflow_run_status(
                workflow_run_id=workflow_run_id,
                status=pre_finally_status,
                failure_reason=pre_finally_failure_reason,
            )
            if pre_finally_status == WorkflowRunStatus.timed_out:
                await self._cascade_child_entities_on_terminal(workflow_run_id, WorkflowRunStatus.timed_out)
            return workflow_run

        return workflow_run

    async def mark_workflow_run_as_failed(
        self,
        workflow_run_id: str,
        failure_reason: str | None,
        run_with: str | None = None,
        failure_category: list[dict] | None = None,
    ) -> WorkflowRun:
        LOG.info(
            f"Marking workflow run {workflow_run_id} as failed",
            workflow_run_id=workflow_run_id,
            workflow_status="failed",
            failure_reason=failure_reason,
        )

        # Add workflow failure tag to trace
        otel_trace.get_current_span().set_attribute("task.completion_status", WorkflowRunStatus.failed)

        # Auto-classify if no explicit category provided
        failure_category_source = "inherited_from_task" if failure_category is not None else "code_level"
        if failure_category is None:
            failure_category = classify_from_failure_reason(failure_reason, fallback_to_unknown=True)

        LOG.info(
            "Workflow run failure classified",
            workflow_run_id=workflow_run_id,
            workflow_status="failed",
            failure_category=failure_category,
            primary_failure_category=failure_category[0].get("category") if failure_category else None,
            failure_category_source=failure_category_source,
        )

        return await self._update_workflow_run_status(
            workflow_run_id=workflow_run_id,
            status=WorkflowRunStatus.failed,
            failure_reason=failure_reason,
            run_with=run_with,
            failure_category=failure_category,
        )

    async def mark_workflow_run_as_running(self, workflow_run_id: str, run_with: str | None = None) -> WorkflowRun:
        # Conditional UPDATE refuses to resurrect a finalized wr — prevents the
        # cleanup cron from racing with re-entry paths and stomping timed_out
        # back to running.
        workflow_run = await app.DATABASE.workflow_runs.update_workflow_run_if_not_final(
            workflow_run_id=workflow_run_id,
            status=WorkflowRunStatus.running,
            run_with=run_with,
        )
        if workflow_run is None:
            existing = await app.DATABASE.workflow_runs.get_workflow_run(
                workflow_run_id=workflow_run_id, organization_id=None
            )
            if existing is None:
                raise WorkflowRunNotFound(workflow_run_id)
            LOG.info(
                "Refusing to mark workflow_run as running — already in final state",
                workflow_run_id=workflow_run_id,
                current_status=existing.status,
                run_with=run_with,
            )
            return existing

        # Best-effort fire-and-forget write-through to task_runs table, matching
        # the side effect that _update_workflow_run_status would have triggered.
        bg = asyncio.create_task(
            self._sync_task_run_from_workflow_run(workflow_run, workflow_run_id, WorkflowRunStatus.running),
        )
        self._background_tasks.add(bg)
        bg.add_done_callback(self._background_tasks.discard)
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
        failure_category: list[dict] | None = None,
    ) -> WorkflowRun:
        LOG.info(
            f"Marking workflow run {workflow_run_id} as terminated",
            workflow_run_id=workflow_run_id,
            workflow_status="terminated",
            failure_reason=failure_reason,
        )

        # Add workflow terminated tag to trace
        otel_trace.get_current_span().set_attribute("task.completion_status", WorkflowRunStatus.terminated)

        # Auto-classify if no explicit category provided.
        # Intentionally uses fallback_to_unknown=False (the default) — terminated workflows
        # may be user-guided (e.g. terminate_criterion matched), so None is acceptable.
        failure_category_source = "inherited_from_task" if failure_category is not None else "code_level"
        if failure_category is None:
            failure_category = classify_from_failure_reason(failure_reason)

        LOG.info(
            "Workflow run failure classified",
            workflow_run_id=workflow_run_id,
            workflow_status="terminated",
            failure_category=failure_category,
            primary_failure_category=failure_category[0].get("category") if failure_category else None,
            failure_category_source=failure_category_source,
        )

        return await self._update_workflow_run_status(
            workflow_run_id=workflow_run_id,
            status=WorkflowRunStatus.terminated,
            failure_reason=failure_reason,
            run_with=run_with,
            failure_category=failure_category,
        )

    async def mark_workflow_run_as_canceled(self, workflow_run_id: str) -> WorkflowRun:
        """Cancel a workflow run, rejecting the transition if the run has already
        reached a terminal state.

        Previously this wrote ``canceled`` unconditionally, which let a late
        cancel call (for example the extraction-block retry path in
        ``_handle_block_result_status`` racing the run's own finalization) clobber
        a prior ``completed``/``failed``/``terminated`` status and inflate the
        cancel rate. SKY-9188.
        """
        LOG.info(
            f"Marking workflow run {workflow_run_id} as canceled",
            workflow_run_id=workflow_run_id,
            workflow_status="canceled",
        )

        updated = await self.mark_workflow_run_as_canceled_if_not_final(
            workflow_run_id=workflow_run_id,
        )
        if updated is not None:
            return updated

        current = await app.DATABASE.workflow_runs.get_workflow_run(
            workflow_run_id=workflow_run_id,
        )
        if current is None:
            raise WorkflowRunNotFound(workflow_run_id)
        LOG.info(
            "Rejecting cancel: workflow run already in terminal state",
            workflow_run_id=workflow_run_id,
            workflow_run_status=current.status,
        )
        return current

    async def mark_workflow_run_as_canceled_if_not_final(
        self,
        workflow_run_id: str,
    ) -> WorkflowRun | None:
        """Conditional cancel that is a no-op when the run has already reached a
        terminal state. Safe to call from cancellation cleanup paths (e.g. the
        copilot tool's timeout branch) that race with the run's own
        ``_finalize_workflow_run_status`` writes.
        """
        updated = await app.DATABASE.workflow_runs.update_workflow_run_if_not_final(
            workflow_run_id=workflow_run_id,
            status=WorkflowRunStatus.canceled,
        )
        if updated is None:
            return None

        LOG.info(
            f"Marked workflow run {workflow_run_id} as canceled (conditional)",
            workflow_run_id=workflow_run_id,
            workflow_status="canceled",
        )
        otel_trace.get_current_span().set_attribute("task.completion_status", WorkflowRunStatus.canceled)

        # Mirror ``_update_workflow_run_status`` side effects on a terminal transition:
        # extraction-cache clear, duration-metrics log, and task_runs write-through.
        extraction_cache.clear_workflow_run(workflow_run_id)

        start_time = (
            updated.started_at.replace(tzinfo=UTC) if updated.started_at else updated.created_at.replace(tzinfo=UTC)
        )
        queued_seconds = (start_time - updated.created_at.replace(tzinfo=UTC)).total_seconds()
        duration_seconds = (datetime.now(UTC) - start_time).total_seconds()
        LOG.info(
            "Workflow run duration metrics",
            workflow_run_id=workflow_run_id,
            workflow_id=updated.workflow_id,
            queued_seconds=queued_seconds,
            duration_seconds=duration_seconds,
            workflow_run_status=updated.status,
            organization_id=updated.organization_id,
            run_with=updated.run_with,
            ai_fallback=updated.ai_fallback,
            trigger_type=updated.trigger_type,
            workflow_schedule_id=updated.workflow_schedule_id,
        )
        await self._apply_completion_run_tags_best_effort(updated)

        bg = asyncio.create_task(
            self._sync_task_run_from_workflow_run(updated, workflow_run_id, WorkflowRunStatus.canceled),
        )
        self._background_tasks.add(bg)
        bg.add_done_callback(self._background_tasks.discard)

        return updated

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

        failure_category = classify_from_failure_reason(failure_reason, fallback_to_unknown=True)
        LOG.info(
            "Workflow run failure classified",
            workflow_run_id=workflow_run_id,
            workflow_status="timed_out",
            failure_category=failure_category,
            primary_failure_category=failure_category[0].get("category") if failure_category else None,
            failure_category_source="code_level",
        )

        workflow_run = await self._update_workflow_run_status(
            workflow_run_id=workflow_run_id,
            status=WorkflowRunStatus.timed_out,
            failure_reason=failure_reason,
            run_with=run_with,
            failure_category=failure_category,
        )
        await self._cascade_child_entities_on_terminal(workflow_run_id, WorkflowRunStatus.timed_out)
        return workflow_run

    async def get_workflow_run(self, workflow_run_id: str, organization_id: str | None = None) -> WorkflowRun:
        workflow_run = await app.DATABASE.workflow_runs.get_workflow_run(
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
        return await app.DATABASE.workflow_params.create_workflow_parameter(
            workflow_id=workflow_id,
            workflow_parameter_type=workflow_parameter_type,
            key=key,
            description=description,
            default_value=default_value,
        )

    async def create_aws_secret_parameter(
        self, workflow_id: str, aws_key: str, key: str, description: str | None = None
    ) -> AWSSecretParameter:
        return await app.DATABASE.workflow_params.create_aws_secret_parameter(
            workflow_id=workflow_id, aws_key=aws_key, key=key, description=description
        )

    async def create_output_parameter(
        self, workflow_id: str, key: str, description: str | None = None
    ) -> OutputParameter:
        return await app.DATABASE.workflow_params.create_output_parameter(
            workflow_id=workflow_id, key=key, description=description
        )

    async def get_workflow_parameters(self, workflow_id: str) -> list[WorkflowParameter]:
        return await app.DATABASE.workflow_params.get_workflow_parameters(workflow_id=workflow_id)

    async def create_workflow_run_parameter(
        self,
        workflow_run_id: str,
        workflow_parameter: WorkflowParameter,
        value: Any,
    ) -> WorkflowRunParameter:
        value = self._serialize_workflow_run_parameter_value(workflow_parameter, value)

        return await app.DATABASE.workflow_runs.create_workflow_run_parameter(
            workflow_run_id=workflow_run_id,
            workflow_parameter=workflow_parameter,
            value=value,
        )

    async def create_workflow_run_parameters(
        self,
        workflow_run_id: str,
        workflow_parameter_values: list[tuple[WorkflowParameter, Any]],
    ) -> list[WorkflowRunParameter]:
        serialized_workflow_parameter_values = [
            (workflow_parameter, self._serialize_workflow_run_parameter_value(workflow_parameter, value))
            for workflow_parameter, value in workflow_parameter_values
        ]

        return await app.DATABASE.workflow_runs.create_workflow_run_parameters(
            workflow_run_id=workflow_run_id,
            workflow_parameter_values=serialized_workflow_parameter_values,
        )

    @staticmethod
    def _serialize_workflow_run_parameter_value(workflow_parameter: WorkflowParameter, value: Any) -> Any:
        value = json.dumps(value) if isinstance(value, (dict, list)) else value
        # InvalidWorkflowParameter will be raised if the validation fails
        workflow_parameter.workflow_parameter_type.convert_value(value)
        return value

    async def get_workflow_run_parameter_tuples(
        self, workflow_run_id: str
    ) -> list[tuple[WorkflowParameter, WorkflowRunParameter]]:
        return await app.DATABASE.workflow_runs.get_workflow_run_parameters(workflow_run_id=workflow_run_id)

    @staticmethod
    async def get_workflow_output_parameters(workflow_id: str) -> list[OutputParameter]:
        return await app.DATABASE.workflow_params.get_workflow_output_parameters(workflow_id=workflow_id)

    @staticmethod
    async def get_workflow_run_output_parameters(
        workflow_run_id: str,
    ) -> list[WorkflowRunOutputParameter]:
        return await app.DATABASE.workflow_runs.get_workflow_run_output_parameters(workflow_run_id=workflow_run_id)

    @staticmethod
    async def get_output_parameter_workflow_run_output_parameter_tuples(
        workflow_id: str,
        workflow_run_id: str,
    ) -> list[tuple[OutputParameter, WorkflowRunOutputParameter]]:
        workflow_run_output_parameters = await app.DATABASE.workflow_runs.get_workflow_run_output_parameters(
            workflow_run_id=workflow_run_id
        )
        output_parameters = await app.DATABASE.workflow_params.get_workflow_output_parameters_by_ids(
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
        return await app.DATABASE.tasks.get_last_task_for_workflow_run(workflow_run_id=workflow_run_id)

    async def get_tasks_by_workflow_run_id(self, workflow_run_id: str) -> list[Task]:
        return await app.DATABASE.tasks.get_tasks_by_workflow_run_id(workflow_run_id=workflow_run_id)

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
                await app.DATABASE.artifacts.get_latest_n_artifacts(
                    task_id=task_id,
                    artifact_types=artifact_types,
                    organization_id=organization_id,
                    n=limit,
                )
                or []
            )
        elif task_v2_id:
            action_artifacts = await app.DATABASE.artifacts.get_artifacts_by_entity_id(
                organization_id=organization_id,
                artifact_type=ArtifactType.SCREENSHOT_ACTION,
                task_v2_id=task_v2_id,
                limit=limit,
            )
            final_artifacts = await app.DATABASE.artifacts.get_artifacts_by_entity_id(
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
        urls = await app.ARTIFACT_MANAGER.get_share_links_with_bundle_support(artifacts)
        return [u for u in urls if u is not None]

    async def get_recent_workflow_screenshot_artifacts(
        self,
        workflow_run_id: str,
        organization_id: str | None = None,
        limit: int = 3,
        workflow_run_tasks: list[Task] | None = None,
    ) -> list[Artifact]:
        """Return latest screenshot artifacts across recent tasks in a workflow run."""

        if workflow_run_tasks is None:
            workflow_run_tasks = await app.DATABASE.tasks.get_tasks_by_workflow_run_id(workflow_run_id=workflow_run_id)

        screenshot_artifacts: list[Artifact] = []
        seen_artifact_ids: set[str] = set()

        task_ids = [task.task_id for task in workflow_run_tasks]
        if task_ids:
            per_task = await app.DATABASE.artifacts.get_latest_artifact_per_task_ids(
                task_ids=task_ids,
                artifact_types=[ArtifactType.SCREENSHOT_ACTION, ArtifactType.SCREENSHOT_FINAL],
                organization_id=organization_id,
            )
            # Re-order newest-task-first to match the original [::-1] loop semantics
            task_order = {task_id: i for i, task_id in enumerate(reversed(task_ids))}
            per_task.sort(key=lambda a: task_order.get(a.task_id or "", len(task_ids)))
            screenshot_artifacts = per_task[:limit]
            seen_artifact_ids = {a.artifact_id for a in screenshot_artifacts}

        if len(screenshot_artifacts) < limit:
            action_artifacts = await app.DATABASE.artifacts.get_artifacts_by_entity_id(
                organization_id=organization_id,
                artifact_type=ArtifactType.SCREENSHOT_ACTION,
                workflow_run_id=workflow_run_id,
                limit=limit,
            )
            final_artifacts = await app.DATABASE.artifacts.get_artifacts_by_entity_id(
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
        urls = await app.ARTIFACT_MANAGER.get_share_links_with_bundle_support(artifacts)
        return [u for u in urls if u is not None]

    async def get_workflow_run_llm_cost_sum(
        self,
        workflow_run_id: str,
        organization_id: str,
    ) -> float:
        """Sum per-LLM-call cost across step_cost, thought_cost, and
        workflow_run_blocks.llm_cost for this workflow_run.

        `organization_id` is required: passing None makes repo filters
        evaluate as `IS NULL` and silently return 0.0.
        """
        if not organization_id:
            raise ValueError(
                "get_workflow_run_llm_cost_sum requires organization_id; "
                "passing None would compile to IS NULL and silently return 0.0"
            )
        # thought + block sums are independent of the task list; run them in
        # parallel with the task fetch + step sum (which depends on task ids).
        thought_task = app.DATABASE.observer.get_thought_cost_sum_by_workflow_run_id(
            workflow_run_id=workflow_run_id,
            organization_id=organization_id,
        )
        block_task = app.DATABASE.observer.get_block_llm_cost_sum_by_workflow_run_id(
            workflow_run_id=workflow_run_id,
            organization_id=organization_id,
        )
        workflow_run_tasks = await app.DATABASE.tasks.get_tasks_by_workflow_run_id(workflow_run_id=workflow_run_id)
        step_cost_sum, thought_cost_sum, block_llm_cost_sum = await asyncio.gather(
            app.DATABASE.tasks.get_step_cost_sum_by_task_ids(
                task_ids=[task.task_id for task in workflow_run_tasks],
                organization_id=organization_id,
            ),
            thought_task,
            block_task,
        )
        return step_cost_sum + thought_cost_sum + block_llm_cost_sum

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

    async def _fetch_recording_urls(
        self,
        workflow_run: WorkflowRun,
        task_v2: Any | None,
        organization_id: str | None,
    ) -> tuple[list[str], bool]:
        """Fetch recording URLs, preferring precise run-scoped recordings over the shared
        browser-session recording.

        Returns (recording_urls, recording_archived).
        """
        recording_urls: list[str] = []
        recording_archived = False

        # Prefer precise per-run clips over the shared browser-session recording (for a persistent
        # session the latter is one continuous, often multi-hour video spanning every run). A
        # non-clip run recording may be an in-progress per-step snapshot, so it does NOT pre-empt
        # the finalized session recording below — only run_recordings/ clips do.
        run_id = task_v2.observer_cruise_id if task_v2 else workflow_run.workflow_run_id
        run_recording_artifacts = await app.DATABASE.artifacts.list_artifacts_for_run_by_type(
            run_id=run_id,
            artifact_type=ArtifactType.RECORDING,
            organization_id=workflow_run.organization_id,
        )
        # "/run_recordings/" mirrors run_recording_clips.RUN_RECORDING_PATH_SEGMENT.
        clip_artifacts = [a for a in run_recording_artifacts if a.uri and "/run_recordings/" in a.uri]
        if clip_artifacts:
            recording_archived = await app.ARTIFACT_MANAGER.is_recording_archived(clip_artifacts[0])
            if not recording_archived:
                urls = await app.ARTIFACT_MANAGER.get_share_links_with_bundle_support(clip_artifacts)
                recording_urls = [u for u in urls if u is not None]

        # Fall back to the shared browser-session recording (windowed, then unbounded for a
        # persistent session whose continuous recording finalizes long after the run ends).
        if not recording_urls and not recording_archived and workflow_run.browser_session_id:
            if workflow_run.started_at is None:
                LOG.warning(
                    "Skipping recording fan-out: workflow run has browser_session_id but no started_at",
                    workflow_run_id=workflow_run.workflow_run_id,
                    browser_session_id=workflow_run.browser_session_id,
                )
            else:
                try:
                    async with asyncio.timeout(GET_DOWNLOADED_FILES_TIMEOUT):
                        recordings = await app.STORAGE.get_shared_recordings_in_browser_session(
                            organization_id=workflow_run.organization_id,
                            browser_session_id=workflow_run.browser_session_id,
                        )
                        lower_bound = _as_utc(workflow_run.started_at)
                        run_end = _as_utc(workflow_run.finished_at) if workflow_run.finished_at else datetime.now(UTC)
                        upper_bound = run_end + RECORDING_WINDOW_END_BUFFER
                        recording_urls = _select_recording_urls_in_window(recordings, lower_bound, upper_bound)
                        if not recording_urls and recordings:
                            # Persistent sessions upload one continuous recording at session
                            # close, often long after run_end, so it never lands in the bounded
                            # window. Drop the upper bound rather than show "no recording".
                            recording_urls = _select_recording_urls_in_window(recordings, lower_bound)
                except asyncio.TimeoutError:
                    LOG.warning("Timeout getting recordings", browser_session_id=workflow_run.browser_session_id)

        # Last resort: a run's own recording, only when its browser closes on completion. A run that
        # keeps its browser open (persistent session or pinned browser_address) never finalizes its
        # per-run webm, so it has no Duration/Cues and won't play; the clip / session-recording paths
        # above cover those runs instead.
        closes_on_completion = not workflow_run.browser_session_id and not workflow_run.browser_address
        if (
            not recording_urls
            and not recording_archived
            and run_recording_artifacts
            and not clip_artifacts
            and closes_on_completion
        ):
            recording_archived = await app.ARTIFACT_MANAGER.is_recording_archived(run_recording_artifacts[0])
            if not recording_archived:
                urls = await app.ARTIFACT_MANAGER.get_share_links_with_bundle_support(run_recording_artifacts)
                recording_urls = [u for u in urls if u is not None]

        return recording_urls, recording_archived

    async def _fetch_downloaded_files(
        self,
        workflow_run: WorkflowRun,
        task_v2: Any | None,
    ) -> tuple[list[FileInfo], list[str] | None]:
        """Fetch downloaded files for a workflow run, including task_v2 files when present."""
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
        return downloaded_files, downloaded_file_urls

    async def build_workflow_run_status_response(
        self,
        workflow_permanent_id: str,
        workflow_run_id: str,
        organization_id: str | None = None,
        include_cost: bool = False,
        include_step_count: bool = False,
        allow_deleted: bool = False,
    ) -> WorkflowRunResponseBase:
        # ``allow_deleted=True`` is used by the cleanup/webhook path after a
        # long-running run completes: the workflow row may have been
        # soft-deleted (e.g. eval harness teardown fired while the orphan
        # workflow was still executing). We still need to build a status
        # response so the webhook gets delivered with whatever state exists.

        # Batch 1: all independent DB fetches run concurrently.
        (
            workflow,
            workflow_run,
            task_v2,
            workflow_run_tasks,
            workflow_parameter_tuples,
            block_errors,
        ) = await asyncio.gather(
            app.DATABASE.workflows.get_workflow_for_workflow_run(
                workflow_run_id,
                organization_id=organization_id,
                filter_deleted=not allow_deleted,
            ),
            self.get_workflow_run(workflow_run_id=workflow_run_id, organization_id=organization_id),
            app.DATABASE.observer.get_task_v2_by_workflow_run_id(
                workflow_run_id=workflow_run_id,
                organization_id=organization_id,
            ),
            app.DATABASE.tasks.get_tasks_by_workflow_run_id(workflow_run_id=workflow_run_id),
            app.DATABASE.workflow_runs.get_workflow_run_parameters(workflow_run_id=workflow_run_id),
            app.DATABASE.workflow_runs.get_workflow_run_block_errors(
                workflow_run_id=workflow_run_id, organization_id=organization_id
            ),
        )

        if workflow is None:
            LOG.error(f"Workflow {workflow_permanent_id} not found")
            raise WorkflowNotFound(workflow_permanent_id=workflow_permanent_id)

        # Batch 2: fetches that depend on batch 1 results, all run concurrently.
        (
            screenshot_urls_raw,
            output_parameter_tuples,
            (recording_urls, recording_archived),
            (downloaded_files, downloaded_file_urls),
        ) = await asyncio.gather(
            self.get_recent_workflow_screenshot_urls(
                workflow_run_id=workflow_run_id,
                organization_id=organization_id,
                workflow_run_tasks=workflow_run_tasks,
            ),
            self.get_output_parameter_workflow_run_output_parameter_tuples(
                workflow_id=workflow_run.workflow_id, workflow_run_id=workflow_run_id
            ),
            self._fetch_recording_urls(workflow_run, task_v2, organization_id),
            self._fetch_downloaded_files(workflow_run, task_v2),
        )
        screenshot_urls: list[str] | None = screenshot_urls_raw or None
        # Preserve legacy singular contract: last element is the newest.
        recording_url = recording_urls[-1] if recording_urls else None

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
            outputs = await self._refresh_output_urls(
                outputs, organization_id=organization_id, workflow_run_id=workflow_run_id
            )

        errors: list[dict[str, Any]] = []
        for task in workflow_run_tasks:
            errors.extend(task.errors)

        # Also collect block-level error codes (e.g. FILE_PARSER_ERROR) into the
        # same errors array so they appear in the top-level workflow run response,
        # matching the task-level error format. Uses a lightweight query that only
        # fetches blocks with non-null error_codes to avoid a full block load on
        # every status poll.
        for error_codes, failure_reason in block_errors:
            for code in error_codes:
                errors.append(
                    {
                        "error_code": code,
                        "reasoning": failure_reason or "",
                        "confidence_float": 1.0,
                    }
                )

        parameters_with_value = {wfp.key: wfrp.value for wfp, wfrp in workflow_parameter_tuples}

        total_steps = None
        total_cost: float | None = None
        if include_step_count or include_cost:
            step_count, _ = await app.DATABASE.tasks.get_step_counts_by_task_ids(
                task_ids=[task.task_id for task in workflow_run_tasks], organization_id=organization_id
            )
            total_steps = step_count

            if include_cost:
                total_cost = await app.AGENT_FUNCTION.calculate_workflow_run_total_cost(
                    organization_id=organization_id,
                    credits_used=workflow_run.credits_used,
                    cached_credits_used=workflow_run.cached_credits_used,
                )
        return WorkflowRunResponseBase(
            workflow_id=workflow.workflow_permanent_id,
            workflow_run_id=workflow_run_id,
            status=workflow_run.status,
            failure_reason=workflow_run.failure_reason,
            failure_category=workflow_run.failure_category,
            proxy_location=workflow_run.proxy_location,
            webhook_callback_url=workflow_run.webhook_callback_url,
            webhook_failure_reason=workflow_run.webhook_failure_reason,
            totp_verification_url=workflow_run.totp_verification_url,
            totp_identifier=workflow_run.totp_identifier,
            extra_http_headers=workflow_run.extra_http_headers,
            cdp_connect_headers=workflow_run.cdp_connect_headers,
            queued_at=workflow_run.queued_at,
            started_at=workflow_run.started_at,
            finished_at=workflow_run.finished_at,
            created_at=workflow_run.created_at,
            modified_at=workflow_run.modified_at,
            parameters=parameters_with_value,
            screenshot_urls=screenshot_urls,
            recording_url=recording_url,
            recording_urls=recording_urls or None,  # omit field when empty
            recording_archived=recording_archived,
            downloaded_files=downloaded_files,
            downloaded_file_urls=downloaded_file_urls,
            outputs=outputs,
            total_steps=total_steps,
            total_cost=total_cost,
            credits_used=workflow_run.credits_used,
            cached_credits_used=workflow_run.cached_credits_used,
            workflow_title=workflow.title,
            browser_session_id=workflow_run.browser_session_id,
            browser_profile_id=workflow_run.browser_profile_id,
            max_screenshot_scrolls=workflow_run.max_screenshot_scrolls,
            task_v2=task_v2,
            browser_address=workflow_run.browser_address,
            run_with=workflow_run.run_with,
            script_run=workflow_run.script_run,
            script_id=workflow_run.script_run.script_id if workflow_run.script_run else None,
            errors=errors,
        )

    async def _clean_up_workflow_browser(
        self,
        workflow_run: WorkflowRun,
        close_browser_on_completion: bool = True,
        browser_session_id: str | None = None,
    ) -> WorkflowBrowserCleanupResult:
        tasks = await self.get_tasks_by_workflow_run_id(workflow_run.workflow_run_id)

        # Look up child workflow runs (e.g. from task_v2 blocks) to flatten their
        # tasks into the parent list for debug artifact persistence, and collect
        # child workflow_run IDs so cleanup_for_workflow_run can pop their orphaned
        # entries from self.pages (child skips clean_up_workflow).
        child_workflow_runs = await app.DATABASE.workflow_runs.get_workflow_runs_by_parent_workflow_run_id(
            parent_workflow_run_id=workflow_run.workflow_run_id,
            organization_id=workflow_run.organization_id,
        )
        child_workflow_run_ids = [cwr.workflow_run_id for cwr in child_workflow_runs]
        if child_workflow_runs:
            LOG.info(
                "Found child workflow runs for cleanup",
                parent_workflow_run_id=workflow_run.workflow_run_id,
                child_count=len(child_workflow_run_ids),
            )
            for child_run in child_workflow_runs:
                child_tasks = await self.get_tasks_by_workflow_run_id(child_run.workflow_run_id)
                tasks.extend(child_tasks)

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
            child_workflow_run_ids=child_workflow_run_ids,
        )
        return WorkflowBrowserCleanupResult(
            browser_state=browser_state,
            tasks=tasks,
            all_workflow_task_ids=all_workflow_task_ids,
            child_workflow_run_ids=child_workflow_run_ids,
            close_browser_on_completion=close_browser_on_completion,
        )

    async def _persist_workflow_browser_session_if_needed(
        self,
        workflow: Workflow,
        workflow_run: WorkflowRun,
        browser_state: BrowserState | None,
        close_browser_on_completion: bool,
        workflow_run_status: WorkflowRunStatus | None = None,
    ) -> None:
        if not (
            browser_state and workflow.persist_browser_session and browser_state.browser_artifacts.browser_session_dir
        ):
            return

        effective_workflow_run_status = workflow_run_status or workflow_run.status
        if effective_workflow_run_status != WorkflowRunStatus.completed:
            LOG.info(
                "Skipped persisting browser session for non-completed workflow run",
                workflow_run_id=workflow_run.workflow_run_id,
                workflow_run_status=effective_workflow_run_status,
            )
            return

        browser_profile = None
        if workflow_run.browser_profile_id:
            browser_profile = await app.DATABASE.browser_sessions.get_browser_profile(
                workflow_run.browser_profile_id,
                organization_id=workflow_run.organization_id,
            )

        if (
            browser_profile
            and browser_profile.is_managed
            # Only write back to a managed profile this workflow owns; a foreign
            # managed id supplied explicitly is treated like a user profile.
            and browser_profile.workflow_permanent_id == workflow_run.workflow_permanent_id
        ):
            if not close_browser_on_completion:
                await persist_session_cookies(
                    browser_state.browser_context,
                    browser_state.browser_artifacts.browser_session_dir,
                )
            await app.STORAGE.store_browser_profile(
                workflow_run.organization_id,
                profile_id=browser_profile.browser_profile_id,
                directory=browser_state.browser_artifacts.browser_session_dir,
            )
            LOG.info(
                "Persisted managed browser profile for workflow run",
                workflow_run_id=workflow_run.workflow_run_id,
                browser_profile_id=browser_profile.browser_profile_id,
            )
        elif browser_profile is None:
            # No managed profile to write back to: either none was stamped, or a
            # stamped managed profile was deleted between setup and finalization.
            # Persist to the legacy session archive so the run's state isn't lost —
            # the next run reseeds a managed profile from it. Only the deleted-mid-run
            # case is unexpected, so warn there.
            if workflow_run.browser_profile_id:
                LOG.warning(
                    "Managed browser profile missing at finalization; persisting to legacy session archive instead",
                    workflow_run_id=workflow_run.workflow_run_id,
                    browser_profile_id=workflow_run.browser_profile_id,
                )
            browser_session_storage_key = await self.get_workflow_browser_session_storage_key(
                workflow=workflow,
                workflow_run=workflow_run,
            )
            if not close_browser_on_completion:
                await persist_session_cookies(
                    browser_state.browser_context,
                    browser_state.browser_artifacts.browser_session_dir,
                )
            await app.STORAGE.store_browser_session(
                workflow_run.organization_id,
                browser_session_storage_key,
                browser_state.browser_artifacts.browser_session_dir,
            )
            LOG.info(
                "Persisted browser session for workflow run",
                workflow_run_id=workflow_run.workflow_run_id,
                browser_session_storage_key=browser_session_storage_key,
            )

    async def clean_up_workflow(
        self,
        workflow: Workflow,
        workflow_run: WorkflowRun,
        api_key: str | None = None,
        close_browser_on_completion: bool = True,
        need_call_webhook: bool = True,
        browser_session_id: str | None = None,
        browser_cleanup_result: WorkflowBrowserCleanupResult | None = None,
    ) -> None:
        analytics.capture("skyvern-oss-agent-workflow-status", {"status": workflow_run.status})
        if browser_cleanup_result is None:
            browser_cleanup_result = await self._clean_up_workflow_browser(
                workflow_run=workflow_run,
                close_browser_on_completion=close_browser_on_completion,
                browser_session_id=browser_session_id,
            )

        browser_state = browser_cleanup_result.browser_state
        tasks = browser_cleanup_result.tasks
        all_workflow_task_ids = browser_cleanup_result.all_workflow_task_ids
        child_workflow_run_ids = browser_cleanup_result.child_workflow_run_ids
        close_browser_on_completion = browser_cleanup_result.close_browser_on_completion
        if browser_state:
            await self.persist_video_data(
                browser_state, workflow, workflow_run, close_browser_on_completion=close_browser_on_completion
            )
            if tasks:
                await self.persist_debug_artifacts(browser_state, tasks[-1], workflow, workflow_run)
            if not browser_cleanup_result.browser_session_write_back_attempted:
                await self._persist_workflow_browser_session_if_needed(
                    workflow=workflow,
                    workflow_run=workflow_run,
                    browser_state=browser_state,
                    close_browser_on_completion=close_browser_on_completion,
                )

        await app.ARTIFACT_MANAGER.wait_for_upload_aiotasks(all_workflow_task_ids)

        try:
            async with asyncio.timeout(SAVE_DOWNLOADED_FILES_TIMEOUT):
                context = skyvern_context.current()
                finalization_run_id = context.run_id if context and context.run_id else workflow_run.workflow_run_id
                await app.STORAGE.save_downloaded_files(
                    organization_id=workflow_run.organization_id,
                    run_id=finalization_run_id,
                )
                # Tag any session-scoped DOWNLOAD artifacts created during this
                # workflow run with run_id (see
                # cloud_docs/BROWSER_SESSION_DOWNLOAD_ARTIFACTS.md).
                browser_session_id = context.browser_session_id if context else None
                if browser_session_id and finalization_run_id:
                    try:
                        claimed = await app.DATABASE.artifacts.claim_session_download_artifacts_for_run(
                            run_id=finalization_run_id,
                            browser_session_id=browser_session_id,
                            organization_id=workflow_run.organization_id,
                            run_started_at=workflow_run.created_at,
                        )
                        if claimed:
                            LOG.debug(
                                "Claimed session-scoped download artifacts for workflow run",
                                workflow_run_id=workflow_run.workflow_run_id,
                                browser_session_id=browser_session_id,
                                claimed=claimed,
                            )
                    except Exception:
                        LOG.warning(
                            "Failed to claim session-scoped download artifacts for workflow run",
                            workflow_run_id=workflow_run.workflow_run_id,
                            browser_session_id=browser_session_id,
                            exc_info=True,
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

        try:
            if need_call_webhook:
                await self.execute_workflow_webhook(workflow_run, api_key)
        finally:
            # Run contexts hold parameters/secrets/outputs and are keyed per
            # run; without eviction they accumulate for the process lifetime.
            app.WORKFLOW_CONTEXT_MANAGER.remove_workflow_run_context(workflow_run.workflow_run_id)
            for child_workflow_run_id in child_workflow_run_ids:
                app.WORKFLOW_CONTEXT_MANAGER.remove_workflow_run_context(child_workflow_run_id)

    async def prepare_workflow_webhook(
        self,
        workflow_run: WorkflowRun,
        api_key: str | None = None,
    ) -> PreparedWorkflowWebhook | None:
        workflow_id = workflow_run.workflow_id
        # Cleanup path: tolerate soft-deleted workflows. If the workflow row
        # has been deleted between run start and cleanup (common when an eval
        # harness tears down a workflow while the run is still executing in
        # the background), we still want webhook delivery to succeed.
        try:
            workflow_run_status_response = await self.build_workflow_run_status_response(
                workflow_permanent_id=workflow_run.workflow_permanent_id,
                workflow_run_id=workflow_run.workflow_run_id,
                organization_id=workflow_run.organization_id,
                include_step_count=True,
                allow_deleted=True,
            )
        except WorkflowNotFound:
            LOG.warning(
                "Workflow missing during webhook build; skipping webhook delivery",
                workflow_id=workflow_id,
                workflow_run_id=workflow_run.workflow_run_id,
                workflow_permanent_id=workflow_run.workflow_permanent_id,
            )
            return None
        if not workflow_run.webhook_callback_url:
            LOG.warning(
                "Workflow has no webhook callback url. Not sending workflow response",
                workflow_id=workflow_id,
                workflow_run_id=workflow_run.workflow_run_id,
            )
            return None

        # Strip whitespace from the webhook URL to handle user input with leading/trailing spaces
        workflow_run.webhook_callback_url = workflow_run.webhook_callback_url.strip()

        signing_api_key = api_key
        if not signing_api_key:
            org_api_key = await app.DATABASE.organizations.get_valid_org_auth_token(
                workflow_run.organization_id,
                OrganizationAuthTokenType.api.value,
            )
            if org_api_key:
                signing_api_key = org_api_key.token

        if not signing_api_key:
            LOG.warning(
                "No API key available for workflow webhook signature. Not sending workflow response",
                workflow_id=workflow_id,
                workflow_run_id=workflow_run.workflow_run_id,
                organization_id=workflow_run.organization_id,
            )
            return None

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
            queued_at=workflow_run_status_response.queued_at,
            started_at=workflow_run_status_response.started_at,
            finished_at=workflow_run_status_response.finished_at,
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
            api_key=signing_api_key,
        )
        LOG.info(
            "Prepared webhook run status for webhook callback url",
            sampling=True,
            workflow_id=workflow_id,
            workflow_run_id=workflow_run.workflow_run_id,
            webhook_callback_url=workflow_run.webhook_callback_url,
            payload=signed_data.payload_for_log,
            headers=signed_data.headers,
        )
        return PreparedWorkflowWebhook(
            workflow_id=workflow_id,
            workflow_run_id=workflow_run.workflow_run_id,
            organization_id=workflow_run.organization_id,
            webhook_callback_url=workflow_run.webhook_callback_url,
            signed_payload=signed_data.signed_payload,
            headers=signed_data.headers,
            payload_for_log=signed_data.payload_for_log,
        )

    async def deliver_prepared_workflow_webhook(self, webhook: PreparedWorkflowWebhook) -> None:
        LOG.info(
            "Sending webhook run status to webhook callback url",
            sampling=True,
            workflow_id=webhook.workflow_id,
            workflow_run_id=webhook.workflow_run_id,
            webhook_callback_url=webhook.webhook_callback_url,
            payload=webhook.payload_for_log,
            headers=webhook.headers,
        )
        try:
            resp = await deliver_webhook_with_retries(
                url=webhook.webhook_callback_url,
                payload=webhook.signed_payload,
                headers=webhook.headers,
                timeout_seconds=30.0,
                organization_id=webhook.organization_id,
                run_id=webhook.workflow_run_id,
            )
        except Exception as e:
            LOG.warning(
                "Workflow webhook delivery failed after attempting delivery",
                workflow_id=webhook.workflow_id,
                workflow_run_id=webhook.workflow_run_id,
                error=str(e),
                exc_info=True,
            )
            try:
                await app.DATABASE.workflow_runs.update_workflow_run(
                    workflow_run_id=webhook.workflow_run_id,
                    webhook_failure_reason=f"Webhook delivery failed before receiving a response: {e}",
                )
            except Exception:
                LOG.warning(
                    "Failed to record workflow webhook delivery error",
                    workflow_id=webhook.workflow_id,
                    workflow_run_id=webhook.workflow_run_id,
                    exc_info=True,
                )
            return

        if resp.status_code >= 200 and resp.status_code < 300:
            LOG.info(
                "Webhook sent successfully",
                sampling=True,
                workflow_id=webhook.workflow_id,
                workflow_run_id=webhook.workflow_run_id,
                resp_code=resp.status_code,
                resp_text=resp.text,
            )
            try:
                await app.DATABASE.workflow_runs.update_workflow_run(
                    workflow_run_id=webhook.workflow_run_id,
                    webhook_failure_reason="",
                )
            except Exception:
                LOG.warning(
                    "Failed to record successful workflow webhook delivery",
                    workflow_id=webhook.workflow_id,
                    workflow_run_id=webhook.workflow_run_id,
                    exc_info=True,
                )
        else:
            LOG.info(
                "Webhook failed",
                workflow_id=webhook.workflow_id,
                workflow_run_id=webhook.workflow_run_id,
                webhook_data=webhook.payload_for_log,
                resp=resp,
                resp_code=resp.status_code,
                resp_text=resp.text,
            )
            try:
                await app.DATABASE.workflow_runs.update_workflow_run(
                    workflow_run_id=webhook.workflow_run_id,
                    webhook_failure_reason=f"Webhook failed with status code {resp.status_code}, error message: {resp.text}",
                )
            except Exception:
                LOG.warning(
                    "Failed to record failed workflow webhook delivery",
                    workflow_id=webhook.workflow_id,
                    workflow_run_id=webhook.workflow_run_id,
                    exc_info=True,
                )

    async def execute_workflow_webhook(
        self,
        workflow_run: WorkflowRun,
        api_key: str | None = None,
    ) -> None:
        webhook = await self.prepare_workflow_webhook(workflow_run, api_key)
        if webhook is None:
            return
        await self.deliver_prepared_workflow_webhook(webhook)

    async def persist_video_data(
        self,
        browser_state: BrowserState,
        workflow: Workflow,
        workflow_run: WorkflowRun,
        close_browser_on_completion: bool = True,
    ) -> None:
        # Only remux via ffmpeg when the browser was actually closed — otherwise
        # the recording file is still open (persistent sessions, shared browser,
        # remote address) and the remux would fail on the partial container.
        video_artifacts = await app.BROWSER_MANAGER.get_video_artifacts(
            workflow_id=workflow.workflow_id,
            workflow_run_id=workflow_run.workflow_run_id,
            browser_state=browser_state,
            finalize=close_browser_on_completion,
        )
        LOG.debug("Persisting video data", number_of_video_artifacts=len(video_artifacts))
        # Flush here: code-block recordings key on the block/run id, which clean_up_workflow's task-id drain skips.
        upload_keys: set[str] = set()
        last_step: Step | None = None
        last_step_resolved = False
        for video_artifact in video_artifacts:
            if video_artifact.video_artifact_id:
                upload_key = await app.ARTIFACT_MANAGER.update_artifact_data(
                    artifact_id=video_artifact.video_artifact_id,
                    organization_id=workflow_run.organization_id,
                    data=video_artifact.video_data,
                )
                if upload_key:
                    upload_keys.add(upload_key)
                continue

            # No pre-registered artifact row: a recording attached at browser teardown
            # (remote-CDP path) needs a RECORDING artifact created from the on-disk file.
            # Upload by path so the bytes stream straight from disk to storage.
            video_path = video_artifact.video_path
            if not video_path or not os.path.exists(video_path):
                continue
            if not last_step_resolved:
                last_step_resolved = True
                tasks = await app.DATABASE.tasks.get_tasks_by_workflow_run_id(workflow_run.workflow_run_id)
                if tasks:
                    last_step = await app.DATABASE.tasks.get_latest_step(
                        task_id=tasks[-1].task_id, organization_id=workflow_run.organization_id
                    )
            if last_step is None:
                LOG.warning(
                    "Cannot persist path-based recording: no latest step for workflow run",
                    workflow_run_id=workflow_run.workflow_run_id,
                    video_path=video_path,
                )
                continue
            artifact_id = await app.ARTIFACT_MANAGER.create_artifact(
                step=last_step,
                artifact_type=ArtifactType.RECORDING,
                path=video_path,
            )
            video_artifact.video_artifact_id = artifact_id
            upload_keys.add(last_step.task_id)
        if upload_keys:
            await app.ARTIFACT_MANAGER.wait_for_upload_aiotasks(list(upload_keys))

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
        if settings.SKYVERN_SUBMISSION_SIGNAL_SHADOW:
            submission_shadow.schedule_submission_signal_shadow(
                har_data=har_data,
                browser_state=browser_state,
                last_step=last_step,
                workflow_run=workflow_run,
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
        last_step = await app.DATABASE.tasks.get_latest_step(
            task_id=last_task.task_id, organization_id=last_task.organization_id
        )
        if not last_step:
            return

        context = skyvern_context.current()
        if context and context.use_artifact_bundling:
            await self._persist_debug_artifacts_bundled(browser_state, last_step, workflow, workflow_run)
        else:
            await self.persist_browser_console_log(browser_state, last_step, workflow, workflow_run)
            await self.persist_har_data(browser_state, last_step, workflow, workflow_run)
            await self.persist_tracing_data(browser_state, last_step, workflow_run)

    async def _persist_debug_artifacts_bundled(
        self,
        browser_state: BrowserState,
        last_step: Step,
        workflow: Workflow,
        workflow_run: WorkflowRun,
    ) -> None:
        """Bundle HAR, browser console log, and trace into a single task archive ZIP."""
        task_archive_entries: dict[str, tuple[ArtifactType, bytes]] = {}

        browser_log = await app.BROWSER_MANAGER.get_browser_console_log(
            workflow_id=workflow.workflow_id,
            workflow_run_id=workflow_run.workflow_run_id,
            browser_state=browser_state,
        )
        LOG.debug("Persisting browser log (bundled)", browser_log_size=len(browser_log))
        if browser_log:
            task_archive_entries["browser_console.log"] = (ArtifactType.BROWSER_CONSOLE_LOG, browser_log)

        har_data = await app.BROWSER_MANAGER.get_har_data(
            workflow_id=workflow.workflow_id,
            workflow_run_id=workflow_run.workflow_run_id,
            browser_state=browser_state,
        )
        if settings.SKYVERN_SUBMISSION_SIGNAL_SHADOW:
            submission_shadow.schedule_submission_signal_shadow(
                har_data=har_data,
                browser_state=browser_state,
                last_step=last_step,
                workflow_run=workflow_run,
            )
        LOG.debug("Persisting har data (bundled)", har_size=len(har_data))
        if har_data:
            task_archive_entries["har.har"] = (ArtifactType.HAR, har_data)

        if browser_state.browser_context is not None and browser_state.browser_artifacts.traces_dir is not None:
            trace_path = f"{browser_state.browser_artifacts.traces_dir}/{workflow_run.workflow_run_id}.zip"
            try:
                with open(trace_path, "rb") as f:
                    trace_data = f.read()
                task_archive_entries["trace.zip"] = (ArtifactType.TRACE, trace_data)
            except Exception:
                LOG.warning("Failed to read workflow trace file", trace_path=trace_path, exc_info=True)

        if task_archive_entries:
            await app.ARTIFACT_MANAGER.create_task_archive(
                step=last_step,
                entries=task_archive_entries,
                workflow_run_id=workflow_run.workflow_run_id,
            )

    @staticmethod
    def _regenerate_dispatch_draft_parameter_ids(definition: WorkflowDefinition, workflow_id: str) -> None:
        """Regenerate the persisted-row parameter ids (WorkflowParameter, OutputParameter) on a
        definition so they can be inserted under a new workflow_id without colliding on the global
        parameter primary keys (the source workflow shares those ids). The subsequent
        update_workflow_definition reconcile creates the rows from these ids and rebinds each
        block's output_parameter to the regenerated instance by key. Other parameter kinds
        (credential / secret / context) live only in the JSON definition and are resolved by
        field/key at runtime, so they keep their ids.

        The caller passes a deep copy of the definition; the shared ctx.staged_workflow /
        runtime_workflow object must not be mutated.
        """
        for parameter in definition.parameters:
            if isinstance(parameter, WorkflowParameter):
                parameter.workflow_id = workflow_id
                parameter.workflow_parameter_id = generate_workflow_parameter_id()
            elif isinstance(parameter, OutputParameter):
                parameter.workflow_id = workflow_id
                parameter.output_parameter_id = generate_output_parameter_id()

    async def create_copilot_dispatch_draft_version(
        self,
        *,
        runtime_workflow: Workflow,
        organization_id: str,
    ) -> Workflow:
        """Persist ``runtime_workflow`` (the copilot's wrapped test definition) as a real new
        workflow version that the dispatched run resolves by ``run.workflow_id``.

        Uses the normal create machinery: an empty version row is created, then
        update_workflow_definition reconciles fresh WorkflowParameter / OutputParameter ROWS for
        the new version and aligns block output-parameter references. Parameter ids are
        regenerated per-version on a DEEP COPY of the definition (the source ids would collide on
        the global parameter PKs, and the shared ctx.staged_workflow / runtime_workflow object
        must not be mutated). The returned (reloaded) version carries the regenerated ids; the
        caller maps post-run output values against it. The version is created as auto_generated
        and is soft-deleted by the caller once the run reaches a terminal state.
        """
        # next_version is computed including soft-deleted rows: a soft-deleted version still
        # reserves its number under the unique (org, permanent_id, version) constraint, so
        # filtering deleted rows here would recompute a taken number and IntegrityError.
        latest = await app.DATABASE.workflows.get_workflow_by_permanent_id(
            workflow_permanent_id=runtime_workflow.workflow_permanent_id,
            organization_id=organization_id,
            filter_deleted=False,
        )
        next_version = (latest.version if latest else 0) + 1
        dispatch_definition = runtime_workflow.workflow_definition.model_copy(deep=True)
        placeholder = await self.create_workflow(
            title=runtime_workflow.title,
            workflow_definition=WorkflowDefinition(parameters=[], blocks=[]),
            organization_id=organization_id,
            workflow_permanent_id=runtime_workflow.workflow_permanent_id,
            version=next_version,
            status=WorkflowStatus.auto_generated,
            description=runtime_workflow.description,
            proxy_location=runtime_workflow.proxy_location,
            webhook_callback_url=runtime_workflow.webhook_callback_url,
            totp_verification_url=runtime_workflow.totp_verification_url,
            totp_identifier=runtime_workflow.totp_identifier,
            persist_browser_session=runtime_workflow.persist_browser_session,
            pin_saved_session_ip=runtime_workflow.pin_saved_session_ip,
            browser_profile_id=runtime_workflow.browser_profile_id,
            browser_profile_key=runtime_workflow.browser_profile_key,
            model=runtime_workflow.model,
            max_screenshot_scrolling_times=runtime_workflow.max_screenshot_scrolls,
            max_elapsed_time_minutes=runtime_workflow.max_elapsed_time_minutes,
            extra_http_headers=runtime_workflow.extra_http_headers,
            cdp_connect_headers=runtime_workflow.cdp_connect_headers,
            run_with=runtime_workflow.run_with,
            ai_fallback=runtime_workflow.ai_fallback,
            code_version=runtime_workflow.code_version,
            run_sequentially=runtime_workflow.run_sequentially or False,
            sequential_key=runtime_workflow.sequential_key,
            adaptive_caching=runtime_workflow.adaptive_caching,
            enable_self_healing=runtime_workflow.enable_self_healing,
            generate_script_on_terminal=runtime_workflow.generate_script_on_terminal,
            folder_id=runtime_workflow.folder_id,
            is_saved_task=runtime_workflow.is_saved_task,
        )
        try:
            self._regenerate_dispatch_draft_parameter_ids(dispatch_definition, placeholder.workflow_id)
            return await self.update_workflow_definition(
                workflow_id=placeholder.workflow_id,
                organization_id=organization_id,
                workflow_definition=dispatch_definition,
            )
        except Exception:
            # The placeholder row already exists as the latest version; if definition
            # persistence fails, soft-delete it so it does not linger as the latest pointer.
            try:
                await app.DATABASE.workflows.soft_delete_workflow_by_id(
                    workflow_id=placeholder.workflow_id,
                    organization_id=organization_id,
                )
            except Exception:
                LOG.warning(
                    "Failed to clean up partial copilot dispatch version after persistence error",
                    workflow_id=placeholder.workflow_id,
                    organization_id=organization_id,
                    exc_info=True,
                )
            raise

    async def make_workflow_definition(
        self,
        workflow_id: str,
        workflow_definition_yaml: WorkflowDefinitionYAML,
    ) -> WorkflowDefinition:
        workflow_definition = convert_workflow_definition(
            workflow_definition_yaml=workflow_definition_yaml,
            workflow_id=workflow_id,
        )

        await app.DATABASE.workflow_params.save_workflow_definition_parameters(workflow_definition.parameters)

        return workflow_definition

    async def create_workflow_from_request(
        self,
        organization: Organization,
        request: WorkflowCreateYAMLRequest,
        workflow_permanent_id: str | None = None,
        delete_script: bool = True,
        created_by: str | None = None,
        edited_by: str | None = None,
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
        await self._validate_and_normalize_credential_rotation_parameters(
            request.workflow_definition.parameters,
            organization,
        )
        new_workflow_id: str | None = None
        refresh_schedule_runtime_limits = False
        effective_max_elapsed_time_minutes: int | None = None

        if workflow_permanent_id:
            # Would return 404: WorkflowNotFound to the client if wpid does not match the organization
            existing_latest_workflow = await self.get_workflow_by_permanent_id(
                workflow_permanent_id=workflow_permanent_id,
                organization_id=organization_id,
                filter_deleted=False,
            )
        else:
            existing_latest_workflow = None

        try:
            if existing_latest_workflow:
                existing_version = existing_latest_workflow.version

                # Missing field inherits the stored dict; an explicit dict (possibly
                # with mask sentinels for unedited keys) is resolved entry-by-entry
                # against the stored value so newly-added keys aren't dropped.
                if request.cdp_connect_headers is None:
                    effective_cdp_connect_headers = existing_latest_workflow.cdp_connect_headers
                else:
                    effective_cdp_connect_headers = merge_masked_headers(
                        request.cdp_connect_headers,
                        existing_latest_workflow.cdp_connect_headers,
                    )
                effective_max_elapsed_time_minutes = (
                    request.max_elapsed_time_minutes
                    if "max_elapsed_time_minutes" in request.model_fields_set
                    else existing_latest_workflow.max_elapsed_time_minutes
                )
                refresh_schedule_runtime_limits = (
                    effective_max_elapsed_time_minutes != existing_latest_workflow.max_elapsed_time_minutes
                )

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
                    pin_saved_session_ip=request.pin_saved_session_ip
                    if "pin_saved_session_ip" in request.model_fields_set
                    else existing_latest_workflow.pin_saved_session_ip,
                    browser_profile_id=request.browser_profile_id,
                    browser_profile_key=request.browser_profile_key,
                    model=request.model,
                    max_screenshot_scrolling_times=request.max_screenshot_scrolls,
                    max_elapsed_time_minutes=effective_max_elapsed_time_minutes,
                    extra_http_headers=request.extra_http_headers,
                    cdp_connect_headers=effective_cdp_connect_headers,
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
                    adaptive_caching=request.adaptive_caching,
                    enable_self_healing=request.enable_self_healing
                    if request.enable_self_healing is not None
                    else existing_latest_workflow.enable_self_healing,
                    code_version=request.code_version
                    if request.code_version is not None
                    else existing_latest_workflow.code_version,
                    generate_script_on_terminal=request.generate_script_on_terminal,
                    created_by=created_by,
                    edited_by=edited_by,
                )
            else:
                # No existing workflow to inherit from; merge_masked_headers drops
                # any keys whose value is the mask sentinel so we never persist the
                # literal "***" placeholder from a misbehaving client.
                new_cdp_connect_headers = merge_masked_headers(request.cdp_connect_headers, None)
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
                    pin_saved_session_ip=request.pin_saved_session_ip,
                    browser_profile_id=request.browser_profile_id,
                    browser_profile_key=request.browser_profile_key,
                    model=request.model,
                    max_screenshot_scrolling_times=request.max_screenshot_scrolls,
                    max_elapsed_time_minutes=request.max_elapsed_time_minutes,
                    extra_http_headers=request.extra_http_headers,
                    cdp_connect_headers=new_cdp_connect_headers,
                    is_saved_task=request.is_saved_task,
                    status=request.status,
                    run_with=request.run_with,
                    cache_key=request.cache_key,
                    ai_fallback=request.ai_fallback,
                    run_sequentially=request.run_sequentially,
                    sequential_key=request.sequential_key,
                    folder_id=request.folder_id,
                    adaptive_caching=request.adaptive_caching,
                    enable_self_healing=request.enable_self_healing
                    if request.enable_self_healing is not None
                    else False,
                    code_version=request.code_version,
                    generate_script_on_terminal=request.generate_script_on_terminal,
                    created_by=created_by,
                    edited_by=edited_by,
                )
            # Keeping track of the new workflow id to delete it if an error occurs during the creation process
            new_workflow_id = potential_workflow.workflow_id

            workflow_definition = await self.make_workflow_definition(
                potential_workflow.workflow_id,
                request.workflow_definition,
            )

            # Validate the block graph before persisting (detects orphans, cycles, dangling references)
            self.validate_workflow_block_graph(workflow_definition)

            # Reject workflow_trigger.payload entries with malformed Jinja2 (matches runtime PayloadTemplateRenderError)
            self._validate_payload_templates(workflow_definition)

            updated_workflow = await self.update_workflow_definition(
                workflow_id=potential_workflow.workflow_id,
                organization_id=organization_id,
                title=title,
                description=request.description,
                workflow_definition=workflow_definition,
                edited_by=edited_by,
            )

            await self.maybe_delete_cached_code(
                updated_workflow,
                workflow_definition=workflow_definition,
                organization_id=organization_id,
                delete_script=delete_script,
            )

            if refresh_schedule_runtime_limits:
                await self._refresh_workflow_schedule_runtime_limits(
                    workflow_permanent_id=updated_workflow.workflow_permanent_id,
                    organization_id=organization_id,
                    max_elapsed_time_minutes=effective_max_elapsed_time_minutes,
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

    async def _refresh_workflow_schedule_runtime_limits(
        self,
        *,
        workflow_permanent_id: str,
        organization_id: str,
        max_elapsed_time_minutes: int | None,
    ) -> None:
        schedules = await app.DATABASE.schedules.get_workflow_schedules(
            workflow_permanent_id=workflow_permanent_id,
            organization_id=organization_id,
        )
        for schedule in schedules:
            if not schedule.backend_schedule_id:
                continue
            try:
                await app.AGENT_FUNCTION.upsert_workflow_schedule(
                    backend_schedule_id=schedule.backend_schedule_id,
                    organization_id=organization_id,
                    workflow_permanent_id=workflow_permanent_id,
                    workflow_schedule_id=schedule.workflow_schedule_id,
                    cron_expression=schedule.cron_expression,
                    timezone=schedule.timezone,
                    enabled=schedule.enabled,
                    parameters=schedule.parameters,
                    max_elapsed_time_minutes=max_elapsed_time_minutes,
                )
            except Exception:
                LOG.exception(
                    "Failed to refresh workflow schedule runtime limit",
                    workflow_permanent_id=workflow_permanent_id,
                    workflow_schedule_id=schedule.workflow_schedule_id,
                )

    @staticmethod
    async def create_output_parameter_for_block(workflow_id: str, block_yaml: BLOCK_YAML_TYPES) -> OutputParameter:
        output_parameter_key = f"{block_yaml.label}_output"
        return await app.DATABASE.workflow_params.create_output_parameter(
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
        cdp_connect_headers: dict[str, str] | None = None,
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
            cdp_connect_headers=cdp_connect_headers,
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
        workflow_run_blocks = await app.DATABASE.observer.get_workflow_run_blocks(
            workflow_run_id=workflow_run_id,
            organization_id=organization_id,
        )
        # get all the actions for all workflow run blocks
        task_ids = [block.task_id for block in workflow_run_blocks if block.task_id]
        task_id_to_block: dict[str, WorkflowRunBlock] = {
            block.task_id: block for block in workflow_run_blocks if block.task_id
        }
        actions = await app.DATABASE.tasks.get_tasks_actions(task_ids=task_ids, organization_id=organization_id)
        for action in actions:
            if not action.task_id:
                continue
            task_block = task_id_to_block[action.task_id]
            task_block.actions.append(action)

        block_map: dict[str, WorkflowRunTimeline] = {}
        for block in workflow_run_blocks:
            if block.workflow_run_block_id in block_map:
                LOG.warning(
                    "Duplicate workflow_run_block_id in timeline; later occurrence wins",
                    workflow_run_id=workflow_run_id,
                    workflow_run_block_id=block.workflow_run_block_id,
                )
            block_map[block.workflow_run_block_id] = WorkflowRunTimeline(
                type=WorkflowRunTimelineType.block,
                block=block,
                created_at=block.created_at,
                modified_at=block.modified_at,
            )

        result: list[WorkflowRunTimeline] = []
        for timeline in block_map.values():
            if timeline.block is None:
                continue
            parent_id = timeline.block.parent_workflow_run_block_id
            if parent_id and parent_id in block_map:
                block_map[parent_id].children.append(timeline)
                continue
            if parent_id:
                LOG.warning(
                    "Workflow run block references missing parent; surfacing as root",
                    workflow_run_id=workflow_run_id,
                    workflow_run_block_id=timeline.block.workflow_run_block_id,
                    parent_workflow_run_block_id=parent_id,
                )
            result.append(timeline)

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
            sampling=True,
            block_labels=block_labels,
            code_gen=code_gen,
            workflow_run_id=workflow_run.workflow_run_id,
            workflow_permanent_id=workflow.workflow_permanent_id,
            blocks_to_update_count=len(blocks_to_update),
        )

        if block_labels and not code_gen:
            # Do not generate script if block_labels is provided, and an explicit code_gen
            # request is not made
            return None

        existing_script, rendered_cache_key_value, _is_pinned = await workflow_script_service.get_workflow_script(
            workflow,
            workflow_run,
            block_labels,
        )

        # Manages cached workflow script regeneration with conditional-aware locking and versioning
        if existing_script:
            # Pinned static scripts (created by ensure_static_script) should
            # never be regenerated — they are hand-written and authoritative.
            # Only check for pinned scripts when running a static script (avoids
            # an extra DB query for every non-static cached-script workflow).
            ctx = skyvern_context.current()
            if ctx and ctx.is_static_script:
                LOG.info(
                    "Skipping script generation for pinned static script",
                    script_id=existing_script.script_id,
                    workflow_run_id=workflow_run.workflow_run_id,
                )
                return None

            cached_block_labels: set[str] = set()
            script_blocks = await app.DATABASE.scripts.get_script_blocks_by_script_revision_id(
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
                    # Only add missing labels that actually executed in this run.
                    # Unexecuted missing blocks have no action data and can't be generated —
                    # adding them causes an infinite regeneration loop when runs terminate early.
                    executable_missing = missing_labels & blocks_to_update
                    if executable_missing:
                        blocks_to_update.add(settings.WORKFLOW_START_BLOCK_LABEL)
                    else:
                        # All missing blocks are unexecuted — don't regenerate
                        blocks_to_update -= missing_labels  # no-op but defensive
                    if missing_labels - executable_missing:
                        LOG.info(
                            "Skipping unexecuted missing labels to avoid regeneration loop",
                            workflow_id=workflow.workflow_id,
                            workflow_run_id=workflow_run.workflow_run_id,
                            skipped_labels=list(missing_labels - executable_missing),
                            executed_labels=list(executable_missing),
                        )
                elif missing_labels and has_conditionals:
                    LOG.debug(
                        "Skipping regeneration for missing labels in workflow with conditionals",
                        workflow_id=workflow.workflow_id,
                        workflow_run_id=workflow_run.workflow_run_id,
                        missing_labels=list(missing_labels),
                    )

            # Don't regenerate blocks already in the cached script — doing so
            # just churns the version number without producing a different script.
            already_cached = blocks_to_update & cached_block_labels
            if already_cached:
                blocks_to_update -= already_cached
                if not blocks_to_update:
                    LOG.info(
                        "All blocks in blocks_to_update are already cached; skipping regeneration",
                        workflow_id=workflow.workflow_id,
                        workflow_run_id=workflow_run.workflow_run_id,
                        already_cached=sorted(already_cached),
                        script_id=existing_script.script_id,
                    )
                else:
                    LOG.debug(
                        "Removed already-cached blocks from blocks_to_update",
                        workflow_id=workflow.workflow_id,
                        workflow_run_id=workflow_run.workflow_run_id,
                        removed=sorted(already_cached),
                        remaining=sorted(blocks_to_update),
                    )

            should_regenerate = bool(blocks_to_update) or bool(code_gen)

            if not should_regenerate:
                LOG.info(
                    "Workflow script already up to date; skipping regeneration",
                    sampling=True,
                    workflow_id=workflow.workflow_id,
                    workflow_run_id=workflow_run.workflow_run_id,
                    cache_key_value=rendered_cache_key_value,
                    script_id=existing_script.script_id,
                    script_revision_id=existing_script.script_revision_id,
                    run_with=workflow_run.run_with,
                )
                return

            async def _regenerate_script() -> None:
                """Create a new version of the existing script, preserving version history.

                Uses double-check pattern: re-verify regeneration is needed after acquiring lock
                to handle race conditions where another process regenerated while we waited.
                """
                # Double-check: another process may have regenerated while we waited for lock
                fresh_script, _is_pinned = await workflow_script_service.get_workflow_script_by_cache_key_value(
                    organization_id=workflow.organization_id,
                    workflow_permanent_id=workflow.workflow_permanent_id,
                    cache_key_value=rendered_cache_key_value,
                    statuses=[ScriptStatus.published],
                    use_cache=False,
                )
                if fresh_script and fresh_script.script_revision_id != existing_script.script_revision_id:
                    LOG.info(
                        "Script already regenerated by another process, skipping",
                        workflow_id=workflow.workflow_id,
                        workflow_run_id=workflow_run.workflow_run_id,
                        cache_key_value=rendered_cache_key_value,
                        existing_revision=existing_script.script_revision_id,
                        fresh_revision=fresh_script.script_revision_id,
                    )
                    return

                # Get the latest version number so we can increment it
                version_stats = await app.DATABASE.scripts.get_script_version_stats(
                    organization_id=workflow.organization_id,
                    script_ids=[existing_script.script_id],
                )
                latest_version, _ = version_stats.get(existing_script.script_id, (0, 0))
                next_version = latest_version + 1

                LOG.info(
                    "Regenerating script as new version (preserving history)",
                    workflow_id=workflow.workflow_id,
                    workflow_run_id=workflow_run.workflow_run_id,
                    cache_key_value=rendered_cache_key_value,
                    script_id=existing_script.script_id,
                    old_version=latest_version,
                    new_version=next_version,
                    run_with=workflow_run.run_with,
                    blocks_to_update=list(blocks_to_update),
                    code_gen=code_gen,
                )

                # Create a new version of the SAME script_id instead of a new script
                regenerated_script = await app.DATABASE.scripts.create_script(
                    organization_id=workflow.organization_id,
                    run_id=workflow_run.workflow_run_id,
                    script_id=existing_script.script_id,
                    version=next_version,
                )

                await workflow_script_service.generate_workflow_script(
                    workflow_run=workflow_run,
                    workflow=workflow,
                    script=regenerated_script,
                    rendered_cache_key_value=rendered_cache_key_value,
                    cached_script=existing_script,
                    updated_block_labels=blocks_to_update,
                )

                # If generation failed (e.g. syntax error, S3/DB contention), clean up
                # the empty script row to avoid orphaned versions that skip version
                # numbers AND to prevent later runs from finding a published revision
                # with zero blocks (the empty_blocks_detected regression from SKY-8757).
                # Check BOTH files and blocks — a revision with main.py but zero
                # script_block rows still fails code-mode execution.
                script_files = await app.DATABASE.scripts.get_script_files(
                    script_revision_id=regenerated_script.script_revision_id,
                    organization_id=workflow.organization_id,
                )
                script_blocks = await app.DATABASE.scripts.get_script_blocks_by_script_revision_id(
                    script_revision_id=regenerated_script.script_revision_id,
                    organization_id=workflow.organization_id,
                )
                if not script_files or not script_blocks:
                    LOG.warning(
                        "Script generation produced no files or no blocks, soft-deleting empty version",
                        script_id=regenerated_script.script_id,
                        version=regenerated_script.version,
                        script_file_count=len(script_files),
                        script_block_count=len(script_blocks),
                    )
                    await app.DATABASE.scripts.soft_delete_script_by_revision(
                        script_revision_id=regenerated_script.script_revision_id,
                        organization_id=workflow.organization_id,
                    )
                    return

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

        LOG.debug(
            "Creating new cached script (first run for this cache key)",
            workflow_permanent_id=workflow.workflow_permanent_id,
            workflow_run_id=workflow_run.workflow_run_id,
            cache_key_value=rendered_cache_key_value,
            blocks_to_update_count=len(blocks_to_update),
        )

        created_script = await app.DATABASE.scripts.create_script(
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

        # Mirror the regeneration path's post-write guard: if this first-time
        # generation produced no files or no blocks, soft-delete the empty revision
        # so it can't be observed by subsequent runs. (SKY-8757 follow-up.)
        script_files = await app.DATABASE.scripts.get_script_files(
            script_revision_id=created_script.script_revision_id,
            organization_id=workflow.organization_id,
        )
        script_blocks = await app.DATABASE.scripts.get_script_blocks_by_script_revision_id(
            script_revision_id=created_script.script_revision_id,
            organization_id=workflow.organization_id,
        )
        if not script_files or not script_blocks:
            LOG.warning(
                "First-time script generation produced no files or no blocks, soft-deleting empty version",
                script_id=created_script.script_id,
                version=created_script.version,
                script_file_count=len(script_files),
                script_block_count=len(script_blocks),
            )
            await app.DATABASE.scripts.soft_delete_script_by_revision(
                script_revision_id=created_script.script_revision_id,
                organization_id=workflow.organization_id,
            )
            return

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

    async def _trigger_script_reviewer(
        self,
        workflow: Workflow,
        workflow_run: WorkflowRun,
        pre_finally_status: WorkflowRunStatus | None = None,
    ) -> None:
        """Trigger the AI Script Reviewer with Redis lock to prevent concurrent reviews per script family."""
        try:
            context = skyvern_context.current()
            script_revision_id = context.script_revision_id if context else None
            script_id = context.script_id if context else None
            if not script_revision_id or not script_id:
                return

            # Check if the script is pinned — skip auto-review for pinned scripts.
            # Query by script_id (not workflow_run_id) because pinning is applied
            # at the cache_key_value level and may not be on this run's row.
            if await app.DATABASE.scripts.is_script_pinned(
                organization_id=workflow.organization_id,
                script_id=script_id,
            ):
                LOG.info(
                    "Skipping script review — script is pinned",
                    workflow_permanent_id=workflow.workflow_permanent_id,
                    script_id=script_id,
                )
                return

            # Resolve cohort FIRST so the cap check + post-dispatch increment
            # can route to the cohort's own counter.
            use_v3 = await is_v3_cohort(
                workflow_permanent_id=workflow.workflow_permanent_id,
                organization_id=workflow.organization_id,
                workflow_run_id=workflow_run.workflow_run_id,
            )

            # Cap ALL script reviews (fallback + failure) per wpid per day to prevent
            # runaway revision churn when the same issue repeats every run.
            if use_v3:
                cap_exceeded = await self._check_script_review_cap_v3(
                    workflow_permanent_id=workflow.workflow_permanent_id,
                    organization_id=workflow.organization_id,
                )
            else:
                cap_exceeded = await self._check_script_review_cap(
                    workflow_permanent_id=workflow.workflow_permanent_id,
                    organization_id=workflow.organization_id,
                )
            if cap_exceeded:
                LOG.info(
                    "Skipping script review — daily cap exceeded for wpid",
                    workflow_permanent_id=workflow.workflow_permanent_id,
                    workflow_run_id=workflow_run.workflow_run_id,
                    pre_finally_status=pre_finally_status,
                    cohort="v3" if use_v3 else "v2",
                )
                return

            # Fast-path skip: v3 cohort + zero fallback episodes = nothing
            # to review. Saves an LLM round-trip and a cap-counter increment
            # on cold-mint runs, which produce no episodes by construction.
            # v2 keeps its existing behavior (reviews whether or not episodes
            # exist) — only the v3 path adds this gate.
            if use_v3:
                v3_episodes = await app.DATABASE.scripts.get_all_episodes_by_workflow_run_id(
                    workflow_run_id=workflow_run.workflow_run_id,
                    organization_id=workflow.organization_id,
                )
                if not v3_episodes:
                    LOG.info(
                        "v3_postrun_skipped_no_episodes",
                        workflow_run_id=workflow_run.workflow_run_id,
                        workflow_permanent_id=workflow.workflow_permanent_id,
                        script_revision_id=script_revision_id,
                    )
                    return

            # Non-blocking lock per script family
            cache = CacheFactory.get_cache()
            lock = None
            if cache is not None:
                try:
                    lock_name = f"script_reviewer:{script_id}"
                    lock = cache.get_lock(lock_name, blocking_timeout=0, timeout=120)
                except AttributeError:
                    LOG.debug("Cache doesn't support locking for script reviewer")

            async def _run_reviewer_dispatch() -> Any | None:
                if use_v3:
                    return await v3_review_post_run(
                        organization_id=workflow.organization_id,
                        workflow_run=workflow_run,
                        workflow_permanent_id=workflow.workflow_permanent_id,
                        script_revision_id=script_revision_id,
                    )
                await self._run_reviewer_locked(workflow, workflow_run, script_revision_id, script_id)
                return None

            review_ran = False
            review_result: Any | None = None
            if lock is not None:
                try:
                    async with lock:
                        review_result = await _run_reviewer_dispatch()
                        review_ran = True
                except LockError:
                    LOG.info(
                        "Skipping script review — another process is reviewing this script",
                        script_id=script_id,
                        script_revision_id=script_revision_id,
                    )
            else:
                # No Redis/cache available - proceed without lock (graceful degradation for OSS)
                review_result = await _run_reviewer_dispatch()
                review_ran = True

            if review_ran:
                if use_v3:
                    v3_persist_cap_consumed = bool(getattr(review_result, "v3_persist_cap_consumed", False))
                    if not v3_persist_cap_consumed:
                        await self._increment_script_review_counter_v3(
                            workflow_permanent_id=workflow.workflow_permanent_id,
                            organization_id=workflow.organization_id,
                        )
                else:
                    await self._increment_script_review_counter(
                        workflow_permanent_id=workflow.workflow_permanent_id,
                    )
        except Exception:
            LOG.warning(
                "Failed to trigger script reviewer",
                workflow_permanent_id=workflow.workflow_permanent_id,
                exc_info=True,
            )

    @staticmethod
    def _script_review_cap_key(workflow_permanent_id: str) -> str:
        """Build the Redis key for the daily script-review counter (v2 cohort)."""
        return v2_script_review_cap_key(workflow_permanent_id)

    @staticmethod
    def _v3_script_review_cap_key(workflow_permanent_id: str) -> str:
        """Build the Redis key for the daily script-review counter (v3 cohort).

        Separate from the v2 key so cohort comparisons stay clean and v3
        traffic doesn't contaminate v2's counter.
        """
        return v3_script_review_cap_key(workflow_permanent_id)

    async def _check_script_review_cap(self, workflow_permanent_id: str, organization_id: str | None = None) -> bool:
        """Check if the daily script-review cap has been reached for this wpid (v2 cohort).

        Returns True if the cap is exceeded and the review should be skipped.
        Uses Redis get/set to maintain a per-wpid daily counter.
        """
        return await is_script_review_cap_exceeded_v2(
            workflow_permanent_id=workflow_permanent_id,
            organization_id=organization_id,
        )

    async def _check_script_review_cap_v3(self, workflow_permanent_id: str, organization_id: str | None = None) -> bool:
        """v3 cohort mirror of ``_check_script_review_cap`` reading the v3 key."""
        return await is_script_review_cap_exceeded_v3(
            workflow_permanent_id=workflow_permanent_id,
            organization_id=organization_id,
        )

    async def _increment_script_review_counter(self, workflow_permanent_id: str) -> None:
        """Increment the daily script-review counter for this wpid (v2 cohort).

        Uses Redis get+set with a 48-hour TTL (covers timezone edge cases).
        Note: get+set is not atomic, so concurrent reviews for the same wpid
        (different script_ids, different lock keys) may both read the same count
        and overwrite each other, allowing up to ~2x the cap in the worst case.
        Acceptable because the cap is a spam guard, not a hard limit, and the
        repo restricts Redis to get/set/lock only.
        """
        await increment_script_review_counter_v2(workflow_permanent_id)

    async def _increment_script_review_counter_v3(
        self,
        workflow_permanent_id: str,
        organization_id: str | None = None,
    ) -> None:
        """v3 cohort mirror of ``_increment_script_review_counter`` writing v3 key.

        Called from ``_trigger_script_reviewer`` after a successful v3 review_ran.
        """
        await try_increment_script_review_counter_v3(workflow_permanent_id, organization_id=organization_id)

    async def _check_and_increment_cap_v3(
        self,
        workflow_permanent_id: str,
        organization_id: str | None = None,
    ) -> int | None:
        """Atomic check-and-increment for the v3 daily cap counter.

        v3 persist skills (``persist_block_edit``, ``persist_script_rewrite``) call
        this instead of the separate ``_check_script_review_cap`` +
        ``_increment_script_review_counter`` pattern. A wpid-level Redis lock wraps
        the get-check-set sequence, so concurrent v3 persists across different
        script_ids (same wpid) serialize through it — fixes the race documented on
        the v2 helpers.

        Returns:
            - the new counter value (1-based) on success, i.e. cap NOT exceeded.
            - ``None`` if the cap is exceeded OR if cache is unavailable (caller
              should treat ``None`` the same as cap-exceeded; fail closed).

        Uses only the allowed Redis primitives: ``get`` / ``set`` / ``get_lock``;
        no INCR/DECR.

        v2 helpers above are intentionally unchanged; fixing v2's latent race is
        out of scope for this PR.
        """
        return await check_and_increment_cap_v3(
            workflow_permanent_id=workflow_permanent_id,
            organization_id=organization_id,
        )

    async def _run_reviewer_locked(
        self,
        workflow: Workflow,
        workflow_run: WorkflowRun,
        script_revision_id: str,
        script_id: str,
    ) -> None:
        """Run the script reviewer inside a lock. Episodes are scoped to the script version."""
        # Double-check: re-query episodes after acquiring lock (another process may have reviewed them)
        all_episodes = await app.DATABASE.scripts.get_unreviewed_episodes(
            workflow_permanent_id=workflow.workflow_permanent_id,
            organization_id=workflow.organization_id,
            script_revision_id=script_revision_id,
        )
        if not all_episodes:
            return

        # Only review episodes where the AI fallback succeeded — those carry
        # actionable signal (working selectors, agent actions) the reviewer can
        # learn from.  When both the script AND the AI fail, there's nothing to
        # improve and reviewing wastes LLM tokens.
        episodes = [ep for ep in all_episodes if ep.fallback_succeeded is not False]
        if not episodes:
            LOG.info(
                "Skipping script review — all fallback episodes failed (no actionable signal)",
                workflow_permanent_id=workflow.workflow_permanent_id,
                total_episodes=len(all_episodes),
                failed_labels=[ep.block_label for ep in all_episodes][:20],
            )
            return

        LOG.info(
            "Triggering AI Script Reviewer (locked)",
            script_id=script_id,
            script_revision_id=script_revision_id,
            episode_count=len(episodes),
        )

        # Query stale branches for TTL-based pruning
        stale_branches: list = []
        try:
            stale_branches = await app.DATABASE.scripts.get_stale_branches(
                organization_id=workflow.organization_id,
                workflow_permanent_id=workflow.workflow_permanent_id,
                stale_days=90,
            )
            if stale_branches:
                LOG.info(
                    "Found stale branches for pruning",
                    workflow_permanent_id=workflow.workflow_permanent_id,
                    stale_count=len(stale_branches),
                    stale_labels=[f"{b.block_label}/{b.branch_key}" for b in stale_branches],
                )
        except Exception:
            LOG.debug("Failed to query stale branches", exc_info=True)

        # Use the latest version as the base (not the potentially-stale run revision)
        reviewer_base_revision_id = script_revision_id
        try:
            latest = await app.DATABASE.scripts.get_latest_script_version(
                script_id=script_id,
                organization_id=workflow.organization_id,
            )
            if latest:
                reviewer_base_revision_id = latest.script_revision_id
        except Exception:
            LOG.debug("Failed to get latest script version, using run revision", exc_info=True)

        # Fetch historical (already-reviewed) episodes for cross-run context
        historical_episodes: list = []
        try:
            historical_episodes = await app.DATABASE.scripts.get_recent_reviewed_episodes(
                workflow_permanent_id=workflow.workflow_permanent_id,
                organization_id=workflow.organization_id,
                limit=20,
            )
            if historical_episodes:
                LOG.info(
                    "Loaded historical episodes for reviewer context",
                    workflow_permanent_id=workflow.workflow_permanent_id,
                    history_count=len(historical_episodes),
                )
        except Exception:
            LOG.debug("Failed to load historical episodes", exc_info=True)

        await self._run_script_reviewer(
            workflow,
            workflow_run,
            episodes,
            reviewer_base_revision_id,
            stale_branches=stale_branches,
            historical_episodes=historical_episodes,
        )

    async def _run_script_reviewer(
        self,
        workflow: Workflow,
        workflow_run: WorkflowRun,
        episodes: list[ScriptFallbackEpisode],
        script_revision_id: str | None = None,
        stale_branches: list | None = None,
        historical_episodes: list | None = None,
    ) -> None:
        """Run the AI Script Reviewer and create a new script version if successful."""
        # Imports are method-local to defer the script_reviewer module load to
        # the rare path where the reviewer is actually triggered (most workflow
        # runs never invoke it). Pre-existing convention in this method.
        from skyvern.services.script_reviewer import (
            BlockReviewResult,
            ScriptReviewer,
            load_filtered_run_param_values,
            store_review_artifacts,
        )
        from skyvern.services.workflow_script_service import create_script_version_from_review

        LOG.info(
            "Script reviewer async task starting",
            workflow_permanent_id=workflow.workflow_permanent_id,
            script_revision_id=script_revision_id,
            episode_count=len(episodes),
            episode_labels=[ep.block_label for ep in episodes],
        )

        try:
            reviewer = ScriptReviewer()

            # Load the workflow run's parameter values so the reviewer can detect
            # hardcoded values in generated code. The shared loader filters
            # secret/credential params before passing to the validator.
            run_parameter_values = await load_filtered_run_param_values(workflow_run.workflow_run_id)

            # Split episodes by type: regular fallback vs conditional_agent
            regular_episodes = [ep for ep in episodes if ep.fallback_type != "conditional_agent"]
            conditional_episodes = [ep for ep in episodes if ep.fallback_type == "conditional_agent"]

            review_results: dict[str, BlockReviewResult] = {}
            conditional_code: dict[str, str] = {}

            # Review regular fallback episodes (code failures, new page variants)
            if regular_episodes:
                regular_updates = await reviewer.review_fallback_episodes(
                    organization_id=workflow.organization_id,
                    workflow_permanent_id=workflow.workflow_permanent_id,
                    script_revision_id=script_revision_id,
                    episodes=regular_episodes,
                    stale_branches=stale_branches,
                    historical_episodes=historical_episodes,
                    run_parameter_values=run_parameter_values,
                )
                if regular_updates:
                    review_results.update(regular_updates)

            # Review conditional blocks that ran via agent — try to convert to code
            if conditional_episodes:
                conditional_updates = await reviewer.review_conditional_blocks(
                    organization_id=workflow.organization_id,
                    workflow_permanent_id=workflow.workflow_permanent_id,
                    conditional_episodes=conditional_episodes,
                    run_parameter_values=run_parameter_values,
                )
                if conditional_updates:
                    conditional_code.update(conditional_updates)

            # Build code-only dicts for create_script_version_from_review
            updated_blocks: dict[str, str] = {label: r.code for label, r in review_results.items()}
            updated_blocks.update(conditional_code)

            if not updated_blocks:
                LOG.info(
                    "Script reviewer produced no updates",
                    workflow_permanent_id=workflow.workflow_permanent_id,
                )
                # Still mark episodes as reviewed
                for episode in episodes:
                    await app.DATABASE.scripts.mark_episode_reviewed(
                        episode_id=episode.episode_id,
                        organization_id=workflow.organization_id,
                        reviewer_output=None,
                        reviewer_version="v2",
                    )
                return

            # Get the base script to create a new version from
            base_script = None
            if script_revision_id:
                base_script = await app.DATABASE.scripts.get_script_revision(
                    script_revision_id=script_revision_id,
                    organization_id=workflow.organization_id,
                )

            new_script = None
            if base_script:
                new_script = await create_script_version_from_review(
                    organization_id=workflow.organization_id,
                    workflow_permanent_id=workflow.workflow_permanent_id,
                    base_script=base_script,
                    updated_blocks=updated_blocks,
                    workflow=workflow,
                    workflow_run=workflow_run,
                    conditional_blocks=conditional_code,
                )

                if new_script:
                    LOG.info(
                        "Script reviewer created new version",
                        workflow_permanent_id=workflow.workflow_permanent_id,
                        new_version=new_script.version,
                        conditional_coded=list(conditional_code.keys()) if conditional_code else [],
                    )

                    # Store reviewer prompt/response artifacts alongside the new script version
                    await store_review_artifacts(
                        organization_id=workflow.organization_id,
                        script_id=new_script.script_id,
                        script_version=new_script.version,
                        review_results=review_results,
                    )

            # Mark all episodes as reviewed
            for episode in episodes:
                await app.DATABASE.scripts.mark_episode_reviewed(
                    episode_id=episode.episode_id,
                    organization_id=workflow.organization_id,
                    reviewer_output=str(updated_blocks) if updated_blocks else None,
                    new_script_revision_id=new_script.script_revision_id if new_script else None,
                    reviewer_version="v2",
                )

        except Exception:
            LOG.exception(
                "Script reviewer failed",
                workflow_permanent_id=workflow.workflow_permanent_id,
            )

    async def should_run_script(
        self,
        workflow: Workflow,
        workflow_run: WorkflowRun,
    ) -> bool:
        """Whether this run should attempt cached-script execution.

        Priority: run-level run_with > workflow-level run_with. Intended-code
        runs are then passed through the app-level code-mode gate.
        """
        if workflow_run.run_with is not None:
            intended_code = workflow_run.run_with == "code"
        else:
            intended_code = workflow.run_with == "code"
        if not intended_code:
            return False
        return await app.AGENT_FUNCTION.should_keep_code_mode_for_workflow_run(
            workflow=workflow,
            workflow_run=workflow_run,
        )

    async def _mark_script_run_loaded(self, workflow_run_id: str, script: Script) -> None:
        """Record that a cached script was loaded for this workflow run.

        Populates `workflow_run.script_run` with the script's identity at
        workflow setup time so API consumers can detect cache use. Sets
        `ai_fallback_triggered=False` as the initial state; if a fallback
        fires mid-execution, other writers (`services/script_service.py`
        and `_mark_script_fallback_triggered` below) merge the flipped
        `ai_fallback_triggered=True` on top without clobbering identity
        via the merge-on-write behavior in `update_workflow_run`.

        Semantic: `script_run != null` after this runs means "a cached
        script was loaded for this run at setup time." It does NOT imply
        that every (or any) block actually executed from that cache —
        `block_labels` filtering, `requires_agent`, `disable_cache`, and
        non-cacheable block types can still route individual blocks to AI.
        See `ScriptRunResponse` docstrings for the full semantic.

        Wrapped in try/except (matching `_mark_script_fallback_triggered`) so
        a transient DB error on the metadata write doesn't abort workflow
        setup. The `script_run` payload is informational — reporting state
        to API consumers — not load-bearing for the run's own execution.
        """
        try:
            await app.DATABASE.workflow_runs.update_workflow_run(
                workflow_run_id=workflow_run_id,
                ai_fallback_triggered=False,
                script_id=script.script_id,
                script_revision_id=script.script_revision_id,
            )
        except Exception:
            LOG.warning(
                "Failed to mark script_run loaded at workflow setup",
                workflow_run_id=workflow_run_id,
                script_id=script.script_id,
                script_revision_id=script.script_revision_id,
                exc_info=True,
            )

    async def _mark_script_fallback_triggered(
        self,
        workflow_run_id: str,
        valid_to_run_code: bool,
        block_executed_with_code: bool,
        block_label: str | None,
    ) -> None:
        """Flip `ai_fallback_triggered=True` on the run iff the just-executed
        agent block was a script→AI fallback (not an always-agent route).

        Gate semantics:
        - `valid_to_run_code=True` ⇒ we attempted script execution for this
          block. False rules out always-agent routes (requires_agent,
          disable_cache, uncached, non-cacheable block types, agent-only
          workflows).
        - `block_executed_with_code=False` ⇒ script didn't succeed. True means
          script ran cleanly; no fallback occurred; no flag flip.

        Together, a True/False combination means "we tried script, it failed,
        we then ran agent." That's the precise definition of a mid-execution
        script→AI fallback.

        Caller must only invoke this AFTER `block.execute_safe` actually ran
        (i.e., the fallback agent execution happened). Calling before would
        risk false positives in the `ai_fallback=False` kept-the-failure
        case where `execute_safe` is never reached.

        Wrapped in try/except so a transient DB error on the flag flip can't
        abort downstream block post-processing. Regression-locked by
        `tests/unit/workflow/test_mark_script_fallback_triggered.py`.
        """
        if not (valid_to_run_code and not block_executed_with_code):
            return
        try:
            await app.DATABASE.workflow_runs.update_workflow_run(
                workflow_run_id=workflow_run_id,
                ai_fallback_triggered=True,
            )
        except Exception:
            LOG.warning(
                "Failed to mark ai_fallback_triggered after script→AI fallback",
                workflow_run_id=workflow_run_id,
                block_label=block_label,
                exc_info=True,
            )
