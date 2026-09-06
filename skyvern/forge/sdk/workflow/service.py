import asyncio
import copy
import difflib
import importlib.util
import json
import os
import random
import re
import shutil
import sys
import textwrap
import time
import unicodedata
import uuid
from collections import deque
from collections.abc import Awaitable, Callable, Iterable, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from http import HTTPStatus
from typing import Any, Literal, TypeVar, cast, overload

import structlog
from jinja2 import meta as jinja2_meta
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
from skyvern.constants import (
    DEFAULT_WORKFLOW_TITLES,
    GET_DOWNLOADED_FILES_TIMEOUT,
    SAVE_DOWNLOADED_FILES_TIMEOUT,
)
from skyvern.exceptions import (
    BlockedHost,
    BlockNotFound,
    BrowserActionPolicyNotEnforceable,
    BrowserProfileNotApplied,
    BrowserProfileNotFound,
    BrowserSessionAlreadyOccupiedError,
    BrowserSessionClosed,
    BrowserSessionNotFound,
    BrowserSessionNotRenewable,
    BrowserSessionStartupTimeout,
    DisabledBlockExecutionError,
    DownloadSaveIncompleteError,
    InProcessScriptExecutionDenied,
    InvalidCredentialId,
    InvalidWorkflowParameter,
    MissingValueForParameter,
    ScriptTerminationException,
    SequentialCredentialLimitExceeded,
    SkyvernException,
    SkyvernHTTPException,
    UnrecognizedWorkflowParameters,
    WorkflowNotFound,
    WorkflowNotFoundForWorkflowRun,
    WorkflowRunNotFound,
    WorkflowRunParameterPersistenceError,
    get_user_facing_exception_message,
)
from skyvern.forge import app
from skyvern.forge.failure_classifier import classify_from_failure_reason
from skyvern.forge.prompts import prompt_engine
from skyvern.forge.sdk.api.files import is_temp_working_dir, resolve_run_download_id
from skyvern.forge.sdk.artifact.models import Artifact, ArtifactType
from skyvern.forge.sdk.artifact.storage.base import _file_infos_from_download_artifacts
from skyvern.forge.sdk.browser_action_policy import BrowserActionPolicy
from skyvern.forge.sdk.cache import extraction_cache
from skyvern.forge.sdk.cache.factory import CacheFactory
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.security import generate_skyvern_webhook_signature
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.db._sentinels import _UNSET
from skyvern.forge.sdk.db.enums import BrowserSeedSource, OrganizationAuthTokenType, WorkflowRunTriggerType
from skyvern.forge.sdk.db.id import generate_output_parameter_id, generate_workflow_id, generate_workflow_parameter_id
from skyvern.forge.sdk.enterprise_features import collect_enterprise_gated_run_features
from skyvern.forge.sdk.experimentation.enrich_tree import resolve_enrich_tree_for_context
from skyvern.forge.sdk.experimentation.transient_ui_capture import resolve_transient_ui_capture_arm
from skyvern.forge.sdk.experimentation.workflow_block_engine import (
    resolve_workflow_block_engine_arm,
    resolved_workflow_block_engine_arm_label,
)
from skyvern.forge.sdk.forge_log import exception_log_fields
from skyvern.forge.sdk.models import Step, StepStatus
from skyvern.forge.sdk.schemas.browser_profiles import BrowserProfile
from skyvern.forge.sdk.schemas.credentials import Credential, credential_auto_profile_disabled
from skyvern.forge.sdk.schemas.files import FileInfo
from skyvern.forge.sdk.schemas.organizations import Organization
from skyvern.forge.sdk.schemas.persistent_browser_sessions import (
    FORCED_WORKFLOW_SESSION_RUNNABLE_TYPE,
    SESSION_RETIREMENT_RUNNABLE_TYPE,
    PersistentBrowserSession,
    is_final_status,
)
from skyvern.forge.sdk.schemas.tasks import Task, TaskStatus
from skyvern.forge.sdk.schemas.workflow_runs import WorkflowRunBlock, WorkflowRunTimeline, WorkflowRunTimelineType
from skyvern.forge.sdk.streaming.registries import mark_stream_closing
from skyvern.forge.sdk.submission import shadow as submission_shadow
from skyvern.forge.sdk.trace import traced
from skyvern.forge.sdk.workflow.browser_action_policy_enrollment import bind_policy_to_context, rejection_reasons
from skyvern.forge.sdk.workflow.browser_profile_key import (
    build_browser_profile_key_digest,
    build_workflow_browser_session_storage_key,
    render_browser_profile_key,
)
from skyvern.forge.sdk.workflow.context_manager import (
    WorkflowRunContext,
    jinja_sandbox_env,
    resolve_credential_parameter_binding,
)
from skyvern.forge.sdk.workflow.credential_fallback import (
    VALID_FALLBACK_TRIGGERS,
    maybe_start_credential_fallback_retry,
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
    v3_ab_ineligibility_reason,
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
    COPILOT_TEST_WORKFLOW_CREATOR,
    Workflow,
    WorkflowDefinition,
    WorkflowRequestBody,
    WorkflowRun,
    WorkflowRunOutputParameter,
    WorkflowRunParameter,
    WorkflowRunResponseBase,
    WorkflowRunStatus,
    is_adaptive_caching,
    resolve_reuse_browser_session,
    should_acquire_reused_session,
)
from skyvern.forge.sdk.workflow.runtime_completion import (
    ContractVerdict,
    grade_completion_contract,
    parse_completion_contract,
)
from skyvern.forge.sdk.workflow.runtime_secret_bridge import publish_copilot_runtime_secret_values
from skyvern.forge.sdk.workflow.secret_encryption import encrypt_workflow_definition_secrets
from skyvern.forge.sdk.workflow.sequential_key import (
    REUSE_ADMISSION_OFF_DISABLED,
    REUSE_ADMISSION_OFF_KILL_SWITCH,
    REUSE_ADMISSION_OFF_UNRESOLVABLE,
    is_reuse_admission_off,
    resolve_reuse_bound_key,
)
from skyvern.forge.sdk.workflow.status_mapping import (
    BLOCK_STATUS_MAP,
    NONFINAL_BLOCK_STATUSES,
    STEP_STATUS_MAP,
    TASK_STATUS_MAP,
)
from skyvern.forge.sdk.workflow.workflow_definition_converter import convert_workflow_definition
from skyvern.schemas.browser_session_close import BrowserSessionCloseReason
from skyvern.schemas.browser_session_timeouts import REUSE_MIN_REMAINING_LIFETIME_SECONDS
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
    resolve_start_fresh,
    should_suppress_memory_write,
)
from skyvern.schemas.scripts import Script, ScriptBlock, ScriptFallbackEpisode, ScriptStatus, WorkflowScript
from skyvern.schemas.workflows import (
    BLOCK_YAML_TYPES,
    ERROR_CODE_MAX_LENGTH,
    ERROR_CODE_REASONING_MAX_LENGTH,
    BlockResult,
    BlockStatus,
    BlockType,
    WorkflowCreateYAMLRequest,
    WorkflowDefinitionYAML,
    WorkflowStatus,
)
from skyvern.services import script_service, uploaded_file_service, workflow_script_service
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
from skyvern.services.webhook_delivery import (
    PreparedWorkflowWebhook,
    deliver_webhook_with_retries,
    describe_delivery_error,
)
from skyvern.services.workflow_script_service import (  # noqa: F401 -- re-exported; several tests import it from this module
    BLOCK_TYPES_THAT_SHOULD_BE_CACHED,
    is_block_type_cacheable,
)
from skyvern.utils.contained_effects import contained_effect
from skyvern.utils.css_selector import build_action_summaries_with_timing  # shared with script_service
from skyvern.utils.secret_headers import merge_masked_headers
from skyvern.utils.secret_redaction import redact_console_log_bytes, redact_har_bytes
from skyvern.utils.strings import is_uuid
from skyvern.utils.url_validators import prepend_scheme_and_validate_url
from skyvern.utils.url_validators import validate_url as validate_url_with_blocked_host_check
from skyvern.utils.url_validators import validate_webhook_url
from skyvern.webeye.actions.action_types import ActionType
from skyvern.webeye.actions.actions import Action
from skyvern.webeye.browser_state import BrowserState
from skyvern.webeye.profile_cookie_merge import cookie_delta, seed_cookie_values, union_cookies_into_profile_dir
from skyvern.webeye.session_cookies import (
    persist_session_cookies,
    read_persisted_session_cookies,
    refresh_banked_cookies,
)

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
MAX_REPORTED_PARAMETER_KEYS = 20
MAX_REPORTED_PARAMETER_KEY_LENGTH = 64

# Empirical S3 upload SLA; no start buffer (back-to-back leakage is worse than late uploads to the next run).
RECORDING_WINDOW_END_BUFFER = timedelta(minutes=15)
# Skip post-run work when only a sub-millisecond budget remains; asyncio.timeout would fire on the first await.
POST_RUN_TIMEOUT_EXHAUSTED_THRESHOLD_SECONDS = 0.001

# Bound pre-finalization write-back so a stuck profile upload cannot leave the run in a non-terminal state.
BROWSER_SESSION_WRITE_BACK_TIMEOUT = SAVE_DOWNLOADED_FILES_TIMEOUT
# Bound cancellation cleanup so an unresponsive release cannot stall worker shutdown indefinitely.
UNSTAMPED_REUSED_SESSION_RELEASE_TIMEOUT_SECONDS = 30.0
# The router admits one activity touch per 30-second window, but the async sink is best-effort:
# it suppresses in-flight writes and swallows failures, so 60 seconds covers only the throttle.
# Ten windows (300 seconds), plus the timestamp CAS pin, require five full minutes of continuously
# failed activity writes before a live client can lose its browser.
REUSED_SESSION_ROUTER_ACTIVITY_STALE_AFTER = timedelta(seconds=300)

# Limit burst concurrency for status-path fetches that are typically hit by client polling.
WORKFLOW_STATUS_RESPONSE_MAX_IN_FLIGHT = 3
_T1 = TypeVar("_T1")
_T2 = TypeVar("_T2")
_T3 = TypeVar("_T3")
_T4 = TypeVar("_T4")
_T5 = TypeVar("_T5")
_T6 = TypeVar("_T6")


_USER_DEFINED_ERROR_KEYS = {"error_code", "reasoning", "confidence_float", "error_type"}


def _strict_user_defined_error_payload(value: Any) -> dict[str, Any] | None:
    if type(value) is not dict or set(value) != _USER_DEFINED_ERROR_KEYS:
        return None
    if (
        type(value["error_code"]) is not str
        or not value["error_code"]
        or value["error_code"] != value["error_code"].strip()
        or len(value["error_code"]) > ERROR_CODE_MAX_LENGTH
        or type(value["reasoning"]) is not str
        or not value["reasoning"]
        or value["reasoning"] != value["reasoning"].strip()
        or len(value["reasoning"]) > ERROR_CODE_REASONING_MAX_LENGTH
        or type(value["confidence_float"]) is not float
        or not 0 <= value["confidence_float"] <= 1
        or type(value["error_type"]) is not str
        or value["error_type"] != "USER_DEFINED_ERROR"
    ):
        return None
    return dict(value)


def _merge_workflow_run_errors(
    task_errors: list[dict[str, Any]],
    block_errors: list[tuple[str, list[str], str | None, Any, str]],
    mask_reasoning: Callable[[Any], Any] | None = None,
    registered_secret_values: Iterable[Any] = (),
    workflow_run_id: str | None = None,
) -> list[dict[str, Any]]:
    errors: list[dict[str, Any]] = []
    exact_errors: set[tuple[str, str, str, str, str | None]] = set()
    legacy_positions: dict[tuple[str, str], int] = {}
    secret_values = sorted(
        {value for value in registered_secret_values if isinstance(value, str) and value}, key=len, reverse=True
    )

    def redact_registered_secrets(value: str) -> str:
        secret_replaced = False
        for secret in secret_values:
            if secret in value:
                value = value.replace(secret, "[redacted]")
                secret_replaced = True
        return value or ("[redacted]" if secret_replaced else value)

    def without_category_c(value: str) -> str:
        return "".join(character for character in value if unicodedata.category(character)[0] != "C")

    def bounded_reasoning(reasoning: Any) -> str:
        value = reasoning if isinstance(reasoning, str) else str(reasoning)
        normalized = without_category_c(value)
        redacted = redact_registered_secrets(normalized)
        masked = mask_reasoning(redacted) if mask_reasoning is not None else redacted
        if redacted == normalized and masked == normalized:
            return value[:2000]
        return (masked if isinstance(masked, str) else str(masked))[:2000]

    def code_has_no_secrets(code: str, provenance: str, origin: str) -> bool:
        # Once run context is evicted, pre-existing codes cannot be vetted; new inline and secure
        # writes reject secret-bearing codes at ingress, bounding this residual to historical rows.
        normalized = without_category_c(code)
        is_safe = (mask_reasoning is None or mask_reasoning(normalized) == normalized) and not any(
            secret in normalized for secret in secret_values
        )
        if not is_safe:
            LOG.warning(
                "Dropped workflow error row because its code contains a registered secret",
                workflow_run_id=workflow_run_id,
                workflow_run_block_id=provenance if provenance != "task" else None,
                block_label=None,
                row_origin=origin,
            )
        return is_safe

    def signature(error: dict[str, Any]) -> tuple[str, str, str | None]:
        error_type = error.get("error_type")
        return (
            error["error_code"],
            error.get("reasoning", ""),
            error_type if isinstance(error_type, str) else None,
        )

    def append(error: dict[str, Any], provenance: str, kind: str) -> int | None:
        code = error.get("error_code")
        if not isinstance(code, str) or not code or len(code) > 128 or len(errors) >= 100:
            return None
        if not code_has_no_secrets(code, provenance, kind):
            return None
        if "reasoning" in error:
            error["reasoning"] = bounded_reasoning(error["reasoning"])
        key = (provenance, kind, *signature(error))
        if key in exact_errors:
            return None
        position = len(errors)
        errors.append(error)
        exact_errors.add(key)
        return position

    for task_error in task_errors:
        if len(errors) >= 100:
            break
        error = dict(task_error)
        append(error, "task", "legacy")

    for block_id, error_codes, failure_reason, output, block_type in block_errors:
        if len(errors) >= 100 and not legacy_positions:
            break
        block_has_upgrade = any(provenance == block_id for provenance, _ in legacy_positions)
        if len(errors) >= 100 and not block_has_upgrade:
            continue
        # Legitimate ingress emits only a handful of entries, with typed upgrades near the front.
        # Ignore corrupt/adversarial row data beyond the per-source scan cap.
        for code in (error_codes or [])[:100]:
            if len(errors) >= 100:
                break
            provenance = (block_id, code)
            if provenance in legacy_positions:
                continue
            position = append(
                {
                    "error_code": code,
                    "reasoning": failure_reason or "",
                    "confidence_float": 1.0,
                },
                block_id,
                "legacy",
            )
            if position is not None:
                legacy_positions[provenance] = position

        if block_type != BlockType.CODE or type(output) is not dict or type(output.get("errors")) is not list:
            continue
        # Persisted typed errors were checked against the manifest at ingress. Do not
        # re-check here because workflow definitions can drift after a run completes.
        for candidate in output["errors"][:100]:
            if len(errors) >= 100 and not any(provenance == block_id for provenance, _ in legacy_positions):
                break
            accepted = _strict_user_defined_error_payload(candidate)
            if accepted is None:
                continue
            code = accepted["error_code"]
            legacy_position = legacy_positions.get((block_id, code))
            if legacy_position is not None:
                accepted["reasoning"] = bounded_reasoning(accepted["reasoning"])
            accepted_signature = (block_id, "typed", *signature(accepted))
            if accepted_signature in exact_errors:
                continue
            if legacy_position is not None:
                exact_errors.discard((block_id, "legacy", *signature(errors[legacy_position])))
                errors[legacy_position] = accepted
                exact_errors.add(accepted_signature)
                del legacy_positions[(block_id, code)]
            else:
                append(accepted, block_id, "typed")
    return errors


_T_OP = TypeVar("_T_OP")

# Failure reasons stamped when execute_workflow exits before it captures a terminal intent
# (pre_finally_status stays None).
WORKFLOW_RUN_INTERRUPTED_FAILURE_REASON = (
    "Workflow run was interrupted before completion; the interruption cause was unavailable. "
    "Finalized as failed by execute_workflow's in-band cleanup."
)
WORKFLOW_RUN_FAILED_FAILURE_REASON_TEMPLATE = (
    "Workflow run failed: workflow execution raised before completion ({cause_type}); "
    "finalized by execute_workflow's in-band cleanup."
)
_WORKFLOW_RUN_ESCAPED_EXCEPTION_FAILURE_CATEGORY = [
    {
        "category": "UNKNOWN",
        "confidence_float": 0.5,
        "reasoning": "No keyword match found",
    }
]

# Structured warning emitted when a debug-session run's visible PBS profile is
# incompatible with the LoginBlock credential's saved profile. Observability
# dashboards key on these strings — do not rename without updating monitors.
DEBUG_SESSION_PROFILE_INCOMPATIBLE_CODE = "debug_session_profile_incompatible"
DEBUG_SESSION_PROFILE_REASON_NO_PROFILE = "pbs_no_profile"
DEBUG_SESSION_PROFILE_REASON_DIFFERENT = "pbs_different_profile"

# Per-value cap for run-detail API responses (SKY-13015). One block output — a sheet or
# file read — is echoed verbatim into every run-detail read (app run page, MCP get_run,
# run timeline); observed values reach 78MB, which wedges the browser's JSON view and
# overruns MCP's result limits. OUTPUT_PARAMETER_MAX_VALUE_BYTES stays the storage-side
# net; this is the far lower bound a synchronous JSON response can actually carry.
RUN_RESPONSE_MAX_VALUE_BYTES = 2 * 1024 * 1024

# Each level of sibling-preserving trim re-serializes its whole subtree, so unbounded
# recursion costs O(depth x size): a 2.5MB value nested 400 deep serializes 1.27GB and
# blocks the event loop for ~2s. Real block outputs need one or two levels; past this
# the whole value is marked instead of descending further.
RUN_RESPONSE_MAX_TRIM_DEPTH = 4

LOOP_VALUE_TRUNCATED_PLACEHOLDER = "[truncated]"


def _response_value_size_bytes(value: Any, log_context: dict[str, Any]) -> int | None:
    """Serialized size as FastAPI would put it on the wire, or None if it can't be measured."""
    try:
        # Mirrors FastAPI's JSONResponse.render. ensure_ascii=False because the default
        # escapes non-ASCII to \uXXXX (double-counting CJK); the compact separators because
        # the defaults add ", " and ": ", ~12% on a wide row-array.
        return len(json.dumps(value, default=str, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    except Exception:
        LOG.warning("Failed to measure run response value size; passing through", exc_info=True, **log_context)
        return None


# Room reserved so the trailing marker still fits inside the cap alongside kept entries.
_TRUNCATION_MARKER_RESERVE_BYTES = 512

TRUNCATION_MARKER_KEY = "_truncated"


def _truncation_marker(
    size_bytes: int,
    *,
    limit_bytes: int | None = None,
    original_count: int | None = None,
    kept_count: int | None = None,
) -> dict:
    effective_limit = RUN_RESPONSE_MAX_VALUE_BYTES if limit_bytes is None else limit_bytes
    marker = {
        "truncated": True,
        "reason": "exceeded_max_run_response_value_size",
        "original_size_bytes": size_bytes,
        "limit_bytes": effective_limit,
    }
    if original_count is not None:
        marker["original_count"] = original_count
        marker["kept_count"] = kept_count
    return marker


def _string_wire_size(value: str) -> int:
    """Serialized size of a string including JSON quoting and escapes."""
    return len(json.dumps(value, ensure_ascii=False).encode("utf-8"))


def capped_task_v2(task_v2: Any) -> Any:
    """Cap the output on an embedded TaskV2, which rides the workflow run response."""
    if task_v2 is None:
        return task_v2
    capped_output = truncate_oversized_response_value(
        task_v2.output, task_v2_id=getattr(task_v2, "observer_cruise_id", None), field="output"
    )
    if capped_output is task_v2.output:
        return task_v2
    return task_v2.model_copy(update={"output": capped_output})


def capped_task_v1_response(task_response: Any) -> Any:
    """Cap the per-value task-v1 fields returned to the interactive run-detail page."""
    capped_extracted_information = truncate_oversized_response_value(
        task_response.extracted_information,
        task_id=task_response.task_id,
        field="extracted_information",
    )
    capped_failure_reason = truncate_oversized_response_text(task_response.failure_reason)
    if (
        capped_extracted_information is task_response.extracted_information
        and capped_failure_reason is task_response.failure_reason
    ):
        return task_response
    return task_response.model_copy(
        update={
            "extracted_information": capped_extracted_information,
            "failure_reason": capped_failure_reason,
        }
    )


ACTION_TEXT_FIELDS = ("response", "value", "text", "reasoning", "intention", "file_url")


def _cap_action_payloads(action: Action, **log_context: Any) -> None:
    """Cap the unbounded fields an action carries into a timeline block, in place."""
    for text_field in ACTION_TEXT_FIELDS:
        if hasattr(action, text_field):
            setattr(action, text_field, truncate_oversized_response_text(getattr(action, text_field)))
    for action_field in ("output", "skyvern_element_data"):
        setattr(
            action,
            action_field,
            truncate_oversized_response_value(getattr(action, action_field), field=action_field, **log_context),
        )


# Every WorkflowRunBlock field that can hold unbounded model- or user-supplied content.
# Enumerated rather than extended one reviewer comment at a time; test_timeline_block_field
# _coverage fails if a new field on the model is neither capped here nor explicitly exempt.
UNBOUNDED_BLOCK_JSON_FIELDS = (
    "output",
    "navigation_payload",
    "data_schema",
)

# Typed list[str]: the JSON trim would replace an oversized element with a marker dict and
# break the declared type, so these cap element-wise and keep every element a string.
UNBOUNDED_BLOCK_TEXT_LIST_FIELDS = (
    "recipients",
    "attachments",
)

UNBOUNDED_BLOCK_TEXT_FIELDS = (
    "description",
    "failure_reason",
    "finish_reason",
    "navigation_goal",
    "data_extraction_goal",
    "terminate_criterion",
    "complete_criterion",
    "subject",
    "body",
    "prompt",
    "instructions",
    "positive_descriptor",
    "negative_descriptor",
    "executed_branch_expression",
    "url",
    "current_value",
    "final_url",
)


@overload
def truncate_oversized_response_text(current_value: None, *, limit_bytes: int | None = None) -> None: ...


@overload
def truncate_oversized_response_text(current_value: str, *, limit_bytes: int | None = None) -> str: ...


def truncate_oversized_response_text(current_value: str | None, *, limit_bytes: int | None = None) -> str | None:
    """Cap one unbounded text field on a timeline block.

    Prompts, email bodies, rendered branch expressions and ``str(loop_over_value)`` are all
    persisted without a size guard. Stays a string, since every one of these is typed
    ``str | None``, keeping a readable prefix in the same shape as the DecisionBlock cap.
    """
    if current_value is None:
        return current_value
    effective_limit = RUN_RESPONSE_MAX_VALUE_BYTES if limit_bytes is None else limit_bytes
    if _string_wire_size(current_value) <= effective_limit:
        return current_value
    # JSON escaping inflates quotes, backslashes and control characters — up to 6x for a
    # control character — so a raw byte count understates the serialized size by as much as
    # half. Shrink until the encoded form actually fits rather than trusting len().
    keep = min(len(current_value), effective_limit)
    while keep > 0:
        candidate = f"{current_value[:keep]}...[truncated {len(current_value) - keep} chars]"
        if _string_wire_size(candidate) <= effective_limit:
            return candidate
        keep //= 2
    return "...[truncated]"


def _capped_text_list(values: list[str] | None, **log_context: Any) -> list[str] | None:
    """Cap a ``list[str]`` without inserting a JSON marker object into it."""
    if not values:
        return values
    size_bytes = _response_value_size_bytes(values, log_context)
    if size_bytes is None or size_bytes <= RUN_RESPONSE_MAX_VALUE_BYTES:
        return values

    kept: list[str] = []
    running_size = 2
    for value in values:
        values_after_current = len(values) - len(kept) - 1
        if values_after_current == 0:
            item_separator = 1 if kept else 0
            available_for_item = RUN_RESPONSE_MAX_VALUE_BYTES - running_size - item_separator
            capped_value = truncate_oversized_response_text(value, limit_bytes=available_for_item)
            item_size = _string_wire_size(capped_value)
            if running_size + item_separator + item_size <= RUN_RESPONSE_MAX_VALUE_BYTES:
                kept.append(capped_value)
            break

        remaining_count = len(values) - len(kept)
        marker = f"...[truncated {remaining_count} values]"
        marker_size = _string_wire_size(marker)
        item_separator = 1 if kept else 0
        available_for_item = RUN_RESPONSE_MAX_VALUE_BYTES - running_size - item_separator - marker_size - 1
        if available_for_item <= _string_wire_size("...[truncated]"):
            break
        capped_value = truncate_oversized_response_text(value, limit_bytes=available_for_item)
        item_size = _string_wire_size(capped_value)
        if running_size + item_separator + item_size + 1 + marker_size > RUN_RESPONSE_MAX_VALUE_BYTES:
            break
        kept.append(capped_value)
        running_size += item_separator + item_size

    if len(kept) == len(values):
        return kept

    marker = f"...[truncated {len(values) - len(kept)} values]"
    LOG.warning(
        "Trimming oversized run response string list",
        original_size_bytes=size_bytes,
        kept_count=len(kept),
        original_count=len(values),
        **log_context,
    )
    return [*kept, marker]


def _capped_loop_values(loop_values: list[Any] | None, **log_context: Any) -> list[Any] | None:
    """Cap ``loop_values`` without changing how many iterations it reports.

    The run-detail page reads the iteration count off ``loop_values.length`` and resolves a
    selected iteration by index, so collapsing the list would render a finished 200-iteration
    loop as 200/1 and strand every selection past the first.
    """
    if not loop_values:
        return loop_values

    capped = truncate_oversized_response_value(loop_values, field="loop_values", **log_context)
    # A same-length list means every iteration survived (some trimmed in place). A shorter
    # one is the partial-plus-marker shape, which would misreport the iteration count here.
    if isinstance(capped, list) and len(capped) == len(loop_values):
        return capped
    # Per-element trimming could not fit either. Repeat a compact placeholder so the count
    # and index lookups still line up; repeating the full marker would re-exceed the cap on
    # a long list. The sizes are on the warning the call above already emitted.
    placeholders = [LOOP_VALUE_TRUNCATED_PLACEHOLDER] * len(loop_values)
    placeholder_size = _response_value_size_bytes(placeholders, log_context)
    if placeholder_size is not None and placeholder_size <= RUN_RESPONSE_MAX_VALUE_BYTES:
        return placeholders
    # Even one placeholder per iteration overruns the cap (~150k entries). The count has to
    # move into bounded metadata rather than stay encoded as list length.
    size_bytes = _response_value_size_bytes(loop_values, log_context) or 0
    return [_truncation_marker(size_bytes, original_count=len(loop_values), kept_count=0)]


def truncate_oversized_response_value(
    value: Any,
    _remaining_trim_depth: int | None = None,
    _loop_payload_budget_granted: bool = False,
    _limit_bytes: int | None = None,
    **log_context: Any,
) -> Any:
    """Fail-open cap for one value echoed into a run-detail API response (SKY-13015).

    A dict or list keeps every element that fits; only oversized ones are replaced, so
    structure the UI reads survives alongside the one huge field — a block's
    ``downloaded_file_urls``, a trigger block's ``workflow_run_id``, and the per-iteration
    entries of a for-loop's ``list[list[dict]]`` output. A replaced element becomes an
    opaque marker rather than a partial value: a truncated list is indistinguishable from
    a complete one to an SDK caller.
    """
    if value is None or isinstance(value, (bool, int, float)):
        return value

    effective_limit = RUN_RESPONSE_MAX_VALUE_BYTES if _limit_bytes is None else _limit_bytes
    remaining_trim_depth = RUN_RESPONSE_MAX_TRIM_DEPTH if _remaining_trim_depth is None else _remaining_trim_depth
    size_bytes = _response_value_size_bytes(value, log_context)
    if size_bytes is None or size_bytes <= effective_limit:
        return value

    if isinstance(value, (dict, list)) and remaining_trim_depth > 0:
        is_loop_output_envelope = (
            isinstance(value, dict)
            and "output_value" in value
            and ("output_parameter" in value or "loop_value" in value)
        )
        if is_loop_output_envelope and isinstance(value, dict):
            # The transport wrapper is cheap and the UI needs all of it. Reserve its
            # encoded bytes, then trim only output_value to what remains; otherwise a
            # marginally oversized entry can keep loop metadata and drop the payload.
            trimmed_envelope = {
                key: truncate_oversized_response_value(
                    entry,
                    _remaining_trim_depth=remaining_trim_depth - 1,
                    _loop_payload_budget_granted=_loop_payload_budget_granted,
                    _limit_bytes=effective_limit,
                    **log_context,
                )
                for key, entry in value.items()
                if key != "output_value"
            }
            envelope_size = _response_value_size_bytes(trimmed_envelope, log_context)
            output_key_size = _response_value_size_bytes("output_value", log_context)
            if envelope_size is not None and output_key_size is not None:
                output_overhead = (1 if trimmed_envelope else 0) + output_key_size + 1
                output_limit = effective_limit - envelope_size - output_overhead
                if output_limit > 0:
                    grants_loop_payload_budget = not _loop_payload_budget_granted
                    trimmed_output = truncate_oversized_response_value(
                        value["output_value"],
                        _remaining_trim_depth=(
                            RUN_RESPONSE_MAX_TRIM_DEPTH if grants_loop_payload_budget else remaining_trim_depth - 1
                        ),
                        _loop_payload_budget_granted=True,
                        _limit_bytes=output_limit,
                        **log_context,
                    )
                    candidate = {
                        key: trimmed_output if key == "output_value" else trimmed_envelope[key] for key in value
                    }
                    candidate_size = _response_value_size_bytes(candidate, log_context)
                    if candidate_size is not None and candidate_size <= effective_limit:
                        return candidate

        # Reserve the encoded size of every sibling before trimming one child. Without this,
        # a trimmable child can consume nearly the entire parent budget, after which the
        # parent falls back to one bare marker and drops UI metadata such as screenshot URLs.
        # This mirrors the loop-envelope allocation above, but applies to ordinary dicts
        # and lists too. The direct formula is the child size that would make the original
        # collection fit: cap - original collection size + original child size. Attempt it
        # lazily while accumulating: pre-scanning a many-row list would reintroduce the
        # synchronous breadth work this cap is meant to avoid.
        items: Iterable[tuple[Any, Any]]
        if isinstance(value, dict):
            items = value.items()
            is_dict = True
        else:
            items = enumerate(value)
            is_dict = False

        # Accumulate as we go and stop once the kept elements fill the cap. Entries that
        # fit are kept and a trailing marker records what was dropped, so a table 1% over
        # the limit still renders instead of vanishing. The marker is what keeps this from
        # being a silent truncation — a bare prefix would be indistinguishable from the
        # whole value to an SDK caller.
        # Start at the enclosing braces/brackets and charge each entry its separators —
        # and, for a dict, its quoted key. Summing child values alone under-counts a
        # key-heavy dict by roughly half, which would let a payload through at ~2x the cap.
        running_size = 2
        trimmed_pairs: list[tuple[Any, Any]] = []
        dropped_any = False
        # The loop's list/list/entry wrappers are transport structure, not payload depth.
        # Give output_value one fresh, bounded trim budget so nested extracted data and
        # links survive while the one heavy sub-value is replaced. The grant flag prevents
        # nested or model-produced envelopes from resetting the budget repeatedly.
        for key, entry in items:
            raw_entry_size = _response_value_size_bytes(entry, log_context)
            entry_limit = effective_limit - size_bytes + raw_entry_size if raw_entry_size is not None else 0
            grants_loop_payload_budget = (
                is_loop_output_envelope and key == "output_value" and not _loop_payload_budget_granted
            )
            child_remaining_trim_depth = (
                RUN_RESPONSE_MAX_TRIM_DEPTH if grants_loop_payload_budget else remaining_trim_depth - 1
            )
            if raw_entry_size is not None and 0 < entry_limit < raw_entry_size:
                sibling_reserved_entry = truncate_oversized_response_value(
                    entry,
                    _remaining_trim_depth=child_remaining_trim_depth,
                    _loop_payload_budget_granted=_loop_payload_budget_granted or grants_loop_payload_budget,
                    _limit_bytes=entry_limit,
                    **log_context,
                )
                sibling_candidate: dict[Any, Any] | list[Any]
                if isinstance(value, dict):
                    sibling_candidate = {**value, key: sibling_reserved_entry}
                else:
                    sibling_candidate = list(value)
                    sibling_candidate[key] = sibling_reserved_entry
                candidate_size = _response_value_size_bytes(sibling_candidate, log_context)
                if candidate_size is not None and candidate_size <= effective_limit:
                    return sibling_candidate
            trimmed_entry = truncate_oversized_response_value(
                entry,
                _remaining_trim_depth=child_remaining_trim_depth,
                _loop_payload_budget_granted=_loop_payload_budget_granted or grants_loop_payload_budget,
                _limit_bytes=effective_limit,
                **log_context,
            )
            entry_size = _response_value_size_bytes(trimmed_entry, log_context)
            if entry_size is None:
                dropped_any = True
                break
            # +1 for the comma; for a dict also the serialized key and its colon.
            running_size += entry_size + 1
            if is_dict:
                key_size = _response_value_size_bytes(key, log_context)
                if key_size is None:
                    dropped_any = True
                    break
                running_size += key_size + 1
            if running_size > effective_limit - _TRUNCATION_MARKER_RESERVE_BYTES:
                dropped_any = True
                break
            trimmed_pairs.append((key, trimmed_entry))
        # `dropped_any`, not the running total: the loop stops a marker's width below the
        # cap, so a size check here would call a partial result complete and drop entries
        # with nothing recording it.
        if not dropped_any:
            if is_dict:
                return dict(trimmed_pairs)
            return [entry for _, entry in trimmed_pairs]
        if trimmed_pairs:
            marker = _truncation_marker(
                size_bytes,
                limit_bytes=effective_limit,
                original_count=len(value),
                kept_count=len(trimmed_pairs),
            )
            LOG.warning(
                "Trimming oversized run response collection",
                original_size_bytes=size_bytes,
                kept_count=len(trimmed_pairs),
                original_count=len(value),
                **log_context,
            )
            if is_dict:
                return {**dict(trimmed_pairs), TRUNCATION_MARKER_KEY: marker}
            return [*(entry for _, entry in trimmed_pairs), marker]
        # Nothing fit at all — fall through to replacing the whole value.

    LOG.warning(
        "Truncating oversized run response value",
        original_size_bytes=size_bytes,
        limit_bytes=effective_limit,
        **log_context,
    )
    return _truncation_marker(size_bytes, limit_bytes=effective_limit)


@dataclass(frozen=True)
class DebugSessionProfileDecision:
    """Decision for LoginBlock credential-profile flow.

    attach_browser_session_id: the PBS id to thread into
        BROWSER_MANAGER.get_or_create_for_workflow_run so the visible/supplied
        browser is the one the agent acts on. None when the run has no explicit
        browser_session_id (preserve existing behavior — credential-profile
        launches a fresh browser).

    incompatible_reason: None when compatible / no explicit session; otherwise
        the structured reason for the warning emit. Callers branch on this:
        None → take the existing skip-login fast path, set → emit the
        structured warning, attach the session, fall through to ordinary login.
    """

    attach_browser_session_id: str | None
    incompatible_reason: str | None


def _as_utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def _task_v3_ab_arm_for_duration_log(workflow_run_id: str) -> str | None:
    """Failure-safe wrapper around ``resolved_workflow_block_engine_arm_label`` (which owns the
    three-way contract): telemetry only, so a lookup failure must never break run finalization —
    it logs a warning and returns "unknown" (attribution lost) rather than propagating.
    """
    try:
        return resolved_workflow_block_engine_arm_label(workflow_run_id)
    except Exception:
        LOG.warning(
            "task_v3_ab_arm resolution for duration metrics failed",
            workflow_run_id=workflow_run_id,
            exc_info=True,
        )
        # A failed read is attribution-loss, not "never entered the A/B" — same bucket as the
        # out-of-band finalizers.
        return "unknown"


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


def run_selection_is_partial(workflow: Workflow, block_labels: list[str] | None) -> bool:
    """Whether the executed selection left any of the workflow's blocks unrun.

    A selection naming every top-level block runs the whole workflow, so it owes the workflow's
    declared deliverable even though it was dispatched as a block selection. The finally block is
    excluded because execute_workflow runs it on its own path, so a full selection never names it."""
    if not block_labels:
        return False
    definition = workflow.workflow_definition
    definition_labels = {block.label for block in definition.blocks} - {definition.finally_block_label}
    return not definition_labels.issubset(set(block_labels))


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
        if child.label and child.label not in script_blocks_by_label and is_block_type_cacheable(child):
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


@dataclass(frozen=True)
class ScriptBlockAttempt:
    block_result: BlockResult | None
    executed_with_code: bool
    valid_to_run_code: bool
    block_requires_agent: bool
    fallback_episode_id: str | None
    form_fields_for_episode: list | None
    script_exception: Exception | None


@dataclass(frozen=True)
class WorkflowRunDispatchStopped:
    workflow_run: WorkflowRun


class _WorkflowRunDispatchScopeError(RuntimeError):
    pass


@dataclass
class WorkflowBrowserCleanupResult:
    browser_state: BrowserState | None
    tasks: list[Task]
    all_workflow_task_ids: list[str]
    child_workflow_run_ids: list[str]
    close_browser_on_completion: bool
    browser_session_write_back_attempted: bool = False


@dataclass(frozen=True)
class ReusedSessionOwnerProof:
    is_terminal: bool
    runnable_generation_id: str | None = None
    browser_state: BrowserState | None = None
    router_activity_is_stale: bool = False
    observed_last_activity_at: datetime | None = None

    @property
    def can_release(self) -> bool:
        return (
            self.is_terminal
            and self.runnable_generation_id is not None
            and self.browser_state is not None
            and self.router_activity_is_stale
            and self.observed_last_activity_at is not None
        )


class ReusedSessionBelowLifetimeFloor(Exception):
    def __init__(self, *, browser_session: PersistentBrowserSession, shortfall: dict[str, object]) -> None:
        super().__init__(browser_session.persistent_browser_session_id)
        self.browser_session = browser_session
        self.shortfall = shortfall


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
        # Graded at finalization, not executed: its presence must not invalidate cached scripts.
        "completion_contract",
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


# Written by ensure_static_script as the first line of a static pin's main.py;
# must stay in sync with try_import_static_script's marker parsing.
_STATIC_MODULE_MARKER = "# __static_module__: "


def _script_has_static_module_marker(script_path: str) -> bool:
    try:
        with open(script_path) as script_file:
            return script_file.readline().startswith(_STATIC_MODULE_MARKER)
    except OSError:
        return False


async def _load_user_script_module(
    script_path: str,
    spec: "importlib.machinery.ModuleSpec",
    *,
    organization_id: str | None = None,
    workflow_run_id: str | None = None,
    workflow_permanent_id: str | None = None,
    workflow_id: str | None = None,
    script_id: str | None = None,
    script_revision_id: str | None = None,
) -> Any | None:
    """Load a cached script's main.py for execution.

    Static (marker) pins must import the live platform module FIRST: the stored
    main.py is a point-in-time copy of the platform script, and exec-ing it
    silently shadows every fix shipped since the pin was created
    (``_pinned_script_is_current_static`` keeps such pins on the promise that
    the loader imports the deployed module). A marker pin whose live import
    fails loads as None — blocks drop to the agent — because exec-ing its
    stored body would resurrect the same staleness, and the marker-only pin
    check would never supersede it. Only a markerless pin (a generated script)
    executes its stored body.
    """
    loaded_script_module = app.AGENT_FUNCTION.try_import_static_script(script_path)
    if loaded_script_module is not None:
        return loaded_script_module

    await script_service.ensure_in_process_script_execution_allowed(
        seam="workflow.cached_script_module_load",
        organization_id=organization_id,
        workflow_run_id=workflow_run_id,
        workflow_permanent_id=workflow_permanent_id,
        workflow_id=workflow_id,
        script_id=script_id,
        script_revision_id=script_revision_id,
    )

    if _script_has_static_module_marker(script_path):
        LOG.warning(
            "Static pin's live module import failed; refusing to exec the stale stored body",
            script_path=script_path,
        )
        return None
    if not spec.loader:
        return None
    loaded_script_module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(loaded_script_module)
    except Exception:
        LOG.warning("exec_module failed for stored script body", script_path=script_path, exc_info=True)
        return None
    return loaded_script_module


_RUN_SIGNATURE_CACHE_KEY_RE = re.compile(r"""cache_key\s*=\s*(['"])(?P<key>.+?)\1""")


def _run_signature_cache_key(run_signature: str | None) -> str | None:
    if not run_signature:
        return None
    match = _RUN_SIGNATURE_CACHE_KEY_RE.search(run_signature)
    return match.group("key") if match else None


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


def _browser_lease_failure_category(exc: Exception) -> list[dict] | None:
    """The lease seam still holds the typed exception; persist its identity so a reader does not
    have to rediscover it from prose or from the session row, which closes the same way after
    every run."""
    if isinstance(exc, BrowserSessionClosed):
        reason_code = "browser_session_closed"
        reasoning = "The browser session had already closed before the run could lease it"
    elif isinstance(exc, BrowserSessionStartupTimeout):
        reason_code = "browser_session_startup_timeout"
        reasoning = "The browser session did not start within its startup timeout"
    else:
        return None
    return [
        {
            "category": "BROWSER_ERROR",
            "confidence_float": 1.0,
            "reason_code": reason_code,
            "reasoning": reasoning,
        }
    ]


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

    @overload
    async def _gather_with_max_in_flight(
        self,
        operations: tuple[
            Callable[[], Awaitable[_T1]],
            Callable[[], Awaitable[_T2]],
            Callable[[], Awaitable[_T3]],
            Callable[[], Awaitable[_T4]],
            Callable[[], Awaitable[_T5]],
            Callable[[], Awaitable[_T6]],
        ],
        max_in_flight: int = WORKFLOW_STATUS_RESPONSE_MAX_IN_FLIGHT,
    ) -> tuple[_T1, _T2, _T3, _T4, _T5, _T6]: ...

    @overload
    async def _gather_with_max_in_flight(
        self,
        operations: tuple[
            Callable[[], Awaitable[_T1]],
            Callable[[], Awaitable[_T2]],
            Callable[[], Awaitable[_T3]],
            Callable[[], Awaitable[_T4]],
            Callable[[], Awaitable[_T5]],
        ],
        max_in_flight: int = WORKFLOW_STATUS_RESPONSE_MAX_IN_FLIGHT,
    ) -> tuple[_T1, _T2, _T3, _T4, _T5]: ...

    @overload
    async def _gather_with_max_in_flight(
        self,
        operations: tuple[
            Callable[[], Awaitable[_T1]],
            Callable[[], Awaitable[_T2]],
            Callable[[], Awaitable[_T3]],
            Callable[[], Awaitable[_T4]],
        ],
        max_in_flight: int = WORKFLOW_STATUS_RESPONSE_MAX_IN_FLIGHT,
    ) -> tuple[_T1, _T2, _T3, _T4]: ...

    @overload
    async def _gather_with_max_in_flight(
        self,
        operations: Sequence[Callable[[], Awaitable[Any]]],
        max_in_flight: int = WORKFLOW_STATUS_RESPONSE_MAX_IN_FLIGHT,
    ) -> tuple[Any, ...]: ...

    async def _gather_with_max_in_flight(
        self,
        operations: Sequence[Callable[[], Awaitable[Any]]],
        max_in_flight: int = WORKFLOW_STATUS_RESPONSE_MAX_IN_FLIGHT,
    ) -> tuple[Any, ...]:
        """Run async operations with bounded concurrency.

        Status endpoints are often called by polling clients; this prevents a
        single request from exhausting database sessions through large immediate
        parallel bursts while preserving gather ordering and error propagation.
        """

        semaphore = asyncio.Semaphore(max(max_in_flight, 1))

        async def _bounded(operation: Callable[[], Awaitable[_T_OP]]) -> _T_OP:
            async with semaphore:
                return await operation()

        return tuple(await asyncio.gather(*(_bounded(operation) for operation in operations)))

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
                    workflow, parameters, domain_override=(domain or None)
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
    async def _start_credential_fallback_retry_best_effort(workflow_run: WorkflowRun) -> None:
        try:
            await maybe_start_credential_fallback_retry(workflow_run, workflow_run.organization_id)
        except Exception:
            LOG.warning(
                "Credential fallback retry hook failed",
                workflow_run_id=workflow_run.workflow_run_id,
                organization_id=workflow_run.organization_id,
                exc_info=True,
            )

    def _schedule_credential_fallback_retry(self, workflow_run: WorkflowRun) -> None:
        task = asyncio.create_task(self._start_credential_fallback_retry_best_effort(workflow_run))
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

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
        workflow_permanent_id: str,
        block_labels_to_disable: Sequence[str],
    ) -> tuple[list[CachedScriptBlocks], list[CachedScriptBlocks]]:
        """Split cached scripts into published vs draft buckets and collect blocks that should be cleared.

        Looks up matching blocks by label directly in SQL (``get_cached_block_groups_by_labels``)
        rather than loading every cached script for the workflow — some workflows accumulate tens
        of thousands of cached scripts, and loading them all to filter in Python made saves time
        out (SKY-15102).
        """
        cached_groups: list[CachedScriptBlocks] = []
        published_groups: list[CachedScriptBlocks] = []
        seen_group_keys: set[tuple[ScriptStatus | str, str, tuple[str, ...]]] = set()

        rows = await app.DATABASE.scripts.get_cached_block_groups_by_labels(
            organization_id=organization_id,
            workflow_permanent_id=workflow_permanent_id,
            block_labels=block_labels_to_disable,
        )

        blocks_by_candidate: dict[str, list[Any]] = {}
        candidate_by_id: dict[str, tuple[Any, Any]] = {}
        for workflow_script, script, script_block in rows:
            blocks_by_candidate.setdefault(workflow_script.workflow_script_id, []).append(script_block)
            candidate_by_id[workflow_script.workflow_script_id] = (workflow_script, script)

        for workflow_script_id, (candidate, script) in candidate_by_id.items():
            blocks_to_clear = blocks_by_candidate[workflow_script_id]

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

    async def _validate_credential_ids(self, credential_ids: list[str], organization: Organization) -> list[Credential]:
        if not credential_ids:
            return []
        unique_ids = list(dict.fromkeys(credential_ids))
        existing = await app.DATABASE.credentials.get_credentials_by_ids(
            unique_ids, organization_id=organization.organization_id
        )
        found = {credential.credential_id for credential in existing}
        missing = [credential_id for credential_id in unique_ids if credential_id not in found]
        if missing:
            raise InvalidCredentialId(", ".join(missing))
        return existing

    async def _resolve_sequential_credential_id(
        self,
        *,
        workflow: Workflow,
        workflow_run: WorkflowRun,
        organization: Organization,
        parameter_values: dict[str, Any],
        credential_selections: dict[str, str],
    ) -> str | None:
        """Resolve the run's single sequential credential from the actually-selected credential of
        every credential-bearing parameter. Persist the one id when present; leave it NULL (no write)
        for a credential-less run; fail closed before publication when 2+ distinct opted-in credentials
        resolve — the MVP serializes at most one credential per run. The id is a carried identity, never
        a setup-completion sentinel."""
        bound_credential_ids: list[str] = []
        runtime_only_parameter_keys = {
            parameter.key
            for parameter in workflow.workflow_definition.parameters
            if isinstance(parameter, (ContextParameter, OutputParameter))
        }
        at_will_absent_credential_keys = {
            parameter.key
            for parameter in workflow.workflow_definition.parameters
            if isinstance(parameter, WorkflowParameter)
            and self._is_optional_credential_parameter(parameter)
            and parameter.key not in parameter_values
        }
        for parameter in workflow.workflow_definition.parameters:
            credential_id: Any
            if isinstance(parameter, CredentialParameter):
                if (
                    parameter.key not in credential_selections
                    and parameter.credential_id in at_will_absent_credential_keys
                ):
                    # At-will credential indirection is not supported for credential
                    # parameters: without this check the literal parameter key would be
                    # validated as a credential id, producing a baffling error.
                    raise SkyvernHTTPException(
                        message=(
                            f"Credential parameter '{parameter.key}' references credential parameter"
                            f" '{parameter.credential_id}', which has no default and was not provided."
                            " Provide a value for it or set a default credential."
                        ),
                        status_code=HTTPStatus.BAD_REQUEST,
                    )
                if (
                    parameter.key not in credential_selections
                    and parameter.credential_id in runtime_only_parameter_keys
                    and parameter.credential_id not in parameter_values
                ):
                    continue
                credential_id = resolve_credential_parameter_binding(
                    parameter,
                    parameter_values,
                    credential_selections.get(parameter.key),
                )
            elif (
                isinstance(parameter, WorkflowParameter)
                and parameter.workflow_parameter_type == WorkflowParameterType.CREDENTIAL_ID
            ):
                credential_id = parameter_values.get(parameter.key)
            else:
                continue
            if isinstance(credential_id, str) and credential_id:
                bound_credential_ids.append(credential_id)
        validated_bound_credentials = await self._validate_credential_ids(bound_credential_ids, organization)
        sequential_ids = sorted(
            {credential.credential_id for credential in validated_bound_credentials if credential.run_sequentially}
        )
        if not sequential_ids:
            return None
        if len(sequential_ids) > 1:
            raise SequentialCredentialLimitExceeded(sequential_ids)
        sequential_credential_id = sequential_ids[0]
        await app.DATABASE.workflow_runs.update_workflow_run(
            workflow_run_id=workflow_run.workflow_run_id,
            sequential_credential_id=sequential_credential_id,
        )
        return sequential_credential_id

    async def _validate_and_normalize_credential_rotation_parameters(
        self,
        parameters: list[Any],
        organization: Organization,
    ) -> None:
        credential_ids_to_validate: list[str] = []
        for parameter in parameters:
            key = getattr(parameter, "key", None) or "<unknown>"
            credential_ids = getattr(parameter, "credential_ids", None)
            if credential_ids is not None:
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

            fallback_credential_ids = getattr(parameter, "fallback_credential_ids", None)
            if fallback_credential_ids is not None:
                fallback_credential_ids = list(dict.fromkeys(fallback_credential_ids))
                if credential_ids is None:
                    primary_credential_id = getattr(parameter, "credential_id", None)
                    fallback_credential_ids = [
                        credential_id
                        for credential_id in fallback_credential_ids
                        if credential_id != primary_credential_id
                    ]
                parameter.fallback_credential_ids = fallback_credential_ids or None
                credential_ids_to_validate.extend(fallback_credential_ids)

            if (
                getattr(parameter, "credential_ids", None) is not None
                and getattr(parameter, "fallback_credential_ids", None) is not None
            ):
                raise SkyvernHTTPException(
                    message=(
                        f"credential parameter {key} cannot combine credential_ids rotation with "
                        "fallback_credential_ids; configure one or the other."
                    ),
                    status_code=400,
                )

            fallback_trigger = getattr(parameter, "fallback_trigger", None)
            if fallback_trigger is not None and fallback_trigger not in VALID_FALLBACK_TRIGGERS:
                raise SkyvernHTTPException(
                    message=(
                        f"fallback_trigger for credential parameter {key} must be one of: "
                        f"{', '.join(sorted(VALID_FALLBACK_TRIGGERS))}."
                    ),
                    status_code=400,
                )
            if fallback_trigger is not None and not getattr(parameter, "fallback_credential_ids", None):
                raise SkyvernHTTPException(
                    message=f"fallback_trigger for credential parameter {key} requires fallback_credential_ids.",
                    status_code=400,
                )

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

    @staticmethod
    def _get_credential_parameters_with_configured_selection(workflow: Workflow) -> list[CredentialParameter]:
        workflow_definition = getattr(workflow, "workflow_definition", None)
        if workflow_definition is None:
            return []
        return [
            parameter
            for parameter in workflow_definition.parameters
            if isinstance(parameter, CredentialParameter)
            and (bool(parameter.credential_ids) or bool(parameter.fallback_credential_ids))
        ]

    def _rotating_credential_profile_segment(self, workflow: Workflow, parameter_values: dict[str, Any]) -> str | None:
        """Segment folded into a managed browser profile's key so each credential in a rotation pool
        gets its own profile automatically, even when browser_profile_key doesn't reference it or
        isn't set (SKY-15192). Reads the resolved selection already in parameter_values; a parameter
        with no resolved selection (fail-open legacy pool) is skipped, not guessed. Joining multiple
        selections with "," is unambiguous because a resolved value is always a generate_credential_id
        row (cred_<int>), never free text that could itself contain a comma."""
        segments = [
            selected
            for parameter in self._get_rotating_credential_parameters(workflow)
            if isinstance(selected := parameter_values.get(parameter.key), str) and selected
        ]
        return ",".join(segments) or None

    def _managed_browser_profile_digest_key(
        self, workflow: Workflow, parameter_values: dict[str, Any], rendered_key: str | None
    ) -> tuple[str | None, str | None]:
        """(digest_key, credential_segment) for a managed browser profile. rendered_key is an
        unrestricted Jinja render of workflow-parameter values (browser_profile_key.py) — it can
        contain any character, so joining it with credential_segment by a plain delimiter would let
        two different (key, credential) pairs collide onto the same digest (e.g. key renders
        "acct|1" with no credential vs. key "acct" with credential "1"). Collapse rendered_key to its
        fixed-width digest before combining, so the split point is never ambiguous. When the two are
        already identical (the customer's key already IS the credential value), skip combining
        entirely and keep today's exact digest rather than gratuitously reseeding."""
        credential_segment = self._rotating_credential_profile_segment(workflow, parameter_values)
        if not credential_segment or rendered_key == credential_segment:
            return rendered_key or credential_segment, credential_segment
        if not rendered_key:
            return credential_segment, credential_segment
        return f"{build_browser_profile_key_digest(rendered_key)}:{credential_segment}", credential_segment

    def _get_run_credential_parameter_overrides(
        self,
        *,
        workflow: Workflow,
        request_data: dict[str, Any] | None,
    ) -> dict[str, str]:
        if not request_data:
            return {}

        overrides: dict[str, str] = {}
        for parameter in self._get_credential_parameters_with_configured_selection(workflow):
            if parameter.key not in request_data:
                continue

            override = request_data[parameter.key]
            if override is None or override == "":
                continue
            if not isinstance(override, str):
                raise InvalidCredentialId(f"<non-string value of type {type(override).__name__}>")

            credential_ids = list(
                dict.fromkeys(
                    (parameter.credential_ids or [])
                    + (parameter.fallback_credential_ids or [])
                    + [parameter.credential_id]
                )
            )
            if override not in credential_ids:
                raise SkyvernHTTPException(
                    message=(
                        f"Credential override for parameter {parameter.key} must be one of the configured "
                        "rotation or fallback credentials."
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

    async def _rotation_candidates_may_require_serialization(self, workflow: Workflow, organization_id: str) -> bool:
        """Conservatively decide whether a failed rotating-credential selection could still have
        resolved to a credential opted into sequential execution. Any candidate flagged
        run_sequentially — or any candidate we cannot verify — fails closed so a serialized lane is
        never silently skipped. A pool that is provably all non-sequential preserves the legacy
        best-effort partial selection for keyless workflows outside the serialization feature."""
        candidate_ids: set[str] = set()
        for parameter in self._get_credential_parameters_with_configured_selection(workflow):
            candidate_ids.update(parameter.credential_ids or [])
            candidate_ids.update(parameter.fallback_credential_ids or [])
        if not candidate_ids:
            return False
        organization = await app.DATABASE.organizations.get_organization(organization_id)
        if organization is None:
            return True
        try:
            credentials = await self._validate_credential_ids(sorted(candidate_ids), organization)
        except Exception:
            # Cannot verify the candidate pool (missing/invalid id or lookup error) — fail closed.
            return True
        return any(credential.run_sequentially for credential in credentials)

    async def _select_rotating_credential_parameters_for_render(
        self,
        *,
        workflow: Workflow,
        workflow_run: WorkflowRun,
        organization_id: str,
        credential_parameter_overrides: dict[str, str] | None = None,
        parameter_values: dict[str, Any] | None = None,
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
            for parameter in self._get_credential_parameters_with_configured_selection(workflow):
                # Fallback-only parameters have no rotation pool, so the loop above skips them, but a
                # browser_profile_key referencing this login parameter still needs a render value.
                # The initial run pins the primary credential; a fallback retry overrides it above.
                if parameter.key in selections or parameter.credential_ids:
                    continue
                primary = parameter.credential_id
                if not primary:
                    continue
                # credential_id may indirectly reference another workflow parameter that carries the
                # real credential value (mirrors WorkflowRunContext.resolve_credential_parameter_id).
                # Render the resolved value so distinct accounts get distinct browser profiles.
                if parameter_values:
                    referenced = parameter_values.get(primary)
                    if isinstance(referenced, str) and referenced:
                        primary = referenced
                selections[parameter.key] = primary
            return selections
        except Exception:
            LOG.warning(
                "Failed to select rotating credentials for workflow render parameters",
                workflow_run_id=workflow_run.workflow_run_id,
                workflow_permanent_id=workflow.workflow_permanent_id,
                exc_info=True,
            )
            # Scope fail-closed to runs that could actually resolve to a sequential credential: a keyed
            # workflow (browser_profile_key), or a rotation pool with any opted-in or unverifiable
            # candidate. A keyless workflow whose pool is provably non-sequential keeps the legacy
            # best-effort partial selection — the pre-feature behavior for runs outside serialization.
            if workflow.browser_profile_key or await self._rotation_candidates_may_require_serialization(
                workflow, organization_id
            ):
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
                    # A missing-shaped value for an at-will credential (credential_id type,
                    # no default) is allowed: the scheduled run proceeds without a credential.
                    if self._is_optional_credential_parameter(workflow_parameter):
                        continue
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
            elif not self._is_optional_credential_parameter(workflow_parameter):
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
        retried_from_workflow_run_id: str | None = None,
        fallback_attempt: int | None = None,
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
            # Capture the caller-supplied browser_profile_id BEFORE the legacy-pin copy below, so the
            # seed resolver can distinguish an explicit per-run override from a workflow's legacy pin.
            explicit_request_browser_profile_id = workflow_request.browser_profile_id
            # A credential-fallback retry clears the browser handles so the replacement credential
            # gets a clean session; re-inheriting the workflow's profile/cdp headers here would
            # reconnect the retry to the failed account's persistent-browser-session profile.
            is_fallback_retry = retried_from_workflow_run_id is not None
            if (
                not is_fallback_retry
                and workflow_request.browser_profile_id is None
                and workflow_request.browser_session_id is None
                and workflow.browser_profile_id is not None
            ):
                workflow_request.browser_profile_id = workflow.browser_profile_id
            if workflow_request.cdp_connect_headers is None:
                if not is_fallback_retry and workflow.cdp_connect_headers is not None:
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
                retried_from_workflow_run_id=retried_from_workflow_run_id,
                fallback_attempt=fallback_attempt,
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
                    org_default_llm_key=organization.default_llm_key,
                    org_default_secondary_llm_key=organization.default_secondary_llm_key,
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

            new_context = skyvern_context.current()
            if new_context:
                # Resolve flex routing eligibility once per run boot. Site B
                # (scripts/run_workflow.py) re-resolves in the Temporal worker — both go
                # through the same AgentFunction hook so the cloud side is the single
                # owner of the flag name and property shape.
                new_context.use_flex_llm_routing = await app.AGENT_FUNCTION.should_use_flex_llm_routing(
                    trigger_type=resolved_trigger_type,
                    organization=organization,
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

                # Bind the resolved version's browser action policy onto the run's own context.
                # execute_workflow re-binds because a Temporal worker boots a fresh context; this
                # binding covers the paths that drive the run in-process from here.
                try:
                    await self.bind_browser_action_policy(workflow, run_with=workflow_request.run_with)
                except BrowserActionPolicyNotEnforceable as e:
                    # The row already exists, so fail it rather than leave a run stuck in `created`.
                    await self.mark_workflow_run_as_failed(
                        workflow_run_id=workflow_run.workflow_run_id,
                        failure_reason=get_user_facing_exception_message(e),
                    )
                    raise

            # Create all the workflow run parameters, AWSSecretParameter won't have workflow run parameters created.
            all_workflow_parameters = await self.get_workflow_parameters(workflow_id=workflow.workflow_id)
            try:
                missing_parameters: list[str] = []
                unresolved_credential_parameters: list[str] = []
                # Only the credentials the request never mentioned. A key sent as null or blank is a
                # caller who knows the key and means "run without a credential".
                absent_credential_parameters: list[str] = []
                workflow_parameter_values: list[tuple[WorkflowParameter, Any]] = []
                for workflow_parameter in all_workflow_parameters:
                    if workflow_request.data and workflow_parameter.key in workflow_request.data:
                        request_body_value = workflow_request.data[workflow_parameter.key]
                        # Fall back to default value if the request explicitly sends null
                        # This supports API clients (e.g., n8n) that include the key with null value
                        if request_body_value is None and workflow_parameter.default_value is not None:
                            request_body_value = workflow_parameter.default_value
                        if self._is_missing_required_value(workflow_parameter, request_body_value):
                            # A missing-shaped value for an at-will credential means "run without a
                            # credential": no run parameter row is written and the run context
                            # backfills it as None.
                            if self._is_optional_credential_parameter(workflow_parameter):
                                unresolved_credential_parameters.append(workflow_parameter.key)
                                continue
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
                    elif not self._is_optional_credential_parameter(workflow_parameter):
                        missing_parameters.append(workflow_parameter.key)
                    else:
                        unresolved_credential_parameters.append(workflow_parameter.key)
                        absent_credential_parameters.append(workflow_parameter.key)

                declared_parameter_keys = self._declared_request_parameter_keys(workflow, all_workflow_parameters)
                unknown_parameter_keys = sorted(set(workflow_request.data or {}) - declared_parameter_keys)
                if unknown_parameter_keys:
                    # Keys only, never values: an unknown key is the whole diagnosis and a value may
                    # be a credential id or customer data.
                    LOG.info(
                        "Workflow run request sent parameter keys the workflow does not declare",
                        workflow_run_id=workflow_run.workflow_run_id,
                        workflow_permanent_id=workflow.workflow_permanent_id,
                        unknown_parameter_keys=self._bound_parameter_keys(unknown_parameter_keys),
                        declared_parameter_key_count=len(declared_parameter_keys),
                    )
                # Only raise when an unknown key looks like it was meant for one of the credentials
                # the request omitted -- a caller who legitimately runs without a credential must not
                # 400 just because their request also carries an unrelated extra key (a stale field,
                # a client-side sentinel, an internal marker).
                misdirected_credential_keys = [
                    unknown_key
                    for unknown_key in unknown_parameter_keys
                    if difflib.get_close_matches(unknown_key, absent_credential_parameters, n=1)
                ]
                if misdirected_credential_keys:
                    raise UnrecognizedWorkflowParameters(
                        unknown_keys=self._bound_parameter_keys(unknown_parameter_keys),
                        expected_keys=self._bound_parameter_keys(sorted(declared_parameter_keys)),
                        unresolved_credential_keys=sorted(absent_credential_parameters),
                    )
                if unresolved_credential_parameters:
                    LOG.info(
                        "Workflow run is starting without an at-will credential",
                        workflow_run_id=workflow_run.workflow_run_id,
                        workflow_permanent_id=workflow.workflow_permanent_id,
                        credential_parameter_keys=sorted(unresolved_credential_parameters),
                    )

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
                await self._validate_bitwarden_item_ids(workflow=workflow, parameter_values=parameter_values)
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
                    parameter_values=parameter_values,
                )
                parameter_values.update(rotating_credential_selections)
                workflow_run.sequential_credential_id = await self._resolve_sequential_credential_id(
                    workflow=workflow,
                    workflow_run=workflow_run,
                    organization=organization,
                    parameter_values=parameter_values,
                    credential_selections=rotating_credential_selections,
                )
                workflow_run = await self._resolve_and_stamp_run_seed(
                    workflow=workflow,
                    workflow_run=workflow_run,
                    parameter_values=parameter_values,
                    explicit_request_browser_profile_id=explicit_request_browser_profile_id,
                    start_fresh=resolve_start_fresh(
                        workflow_request.start_fresh_browser, explicit_request_browser_profile_id
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
                # Client 4xx (e.g. missing param, invalid credential id) is expected user input and
                # already surfaced via the failed run + failure_reason below, so it needs no operator
                # action. Log it at warning without a traceback (keeping the error_type/exception_hash
                # dashboard fields), and reserve error+traceback for genuine setup defects.
                if isinstance(e, SkyvernHTTPException) and e.status_code < 500:
                    LOG.warning(
                        f"Error while setting up workflow run {workflow_run.workflow_run_id}",
                        workflow_run_id=workflow_run.workflow_run_id,
                        **exception_log_fields(e),
                    )
                else:
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

    async def _validate_bitwarden_item_ids(self, workflow: Workflow, parameter_values: dict[str, Any]) -> None:
        # BitwardenService rejects non-UUID item IDs, so a value already known at run creation and
        # not a UUID can only produce a run that fails at context init — reject it here instead,
        # mirroring WorkflowRunContext._resolve_parameter_value (which needs a worker-side context).
        # Values only resolvable at run time (outputs, context values) are skipped.
        defined_keys = {parameter.key for parameter in workflow.workflow_definition.parameters}
        output_keys: set[str] | None = None
        for parameter in workflow.workflow_definition.parameters:
            if not isinstance(parameter, (BitwardenLoginCredentialParameter, BitwardenCreditCardDataParameter)):
                continue
            source = parameter.bitwarden_item_id
            if not source:
                continue
            if source in parameter_values:
                candidate = parameter_values[source]
            elif source in defined_keys:
                continue
            else:
                if source.endswith("_output"):
                    if output_keys is None:
                        output_keys = {
                            output_parameter.key
                            for output_parameter in await self.get_workflow_output_parameters(
                                workflow_id=workflow.workflow_id
                            )
                        }
                    if source in output_keys:
                        continue
                try:
                    referenced_keys = jinja2_meta.find_undeclared_variables(jinja_sandbox_env.parse(source))
                    if not referenced_keys <= parameter_values.keys():
                        continue
                    candidate = jinja_sandbox_env.from_string(source).render(parameter_values)
                except Exception:
                    continue
            if not candidate:
                # A falsy login item ID means "no item filter" at secret-fetch time, but the
                # credit-card path requires an item ID and fails context init on a falsy value.
                if isinstance(parameter, BitwardenLoginCredentialParameter):
                    continue
            if not isinstance(candidate, str) or not is_uuid(candidate):
                raise InvalidWorkflowParameter(
                    expected_parameter_type="Bitwarden item ID (UUID)",
                    value=str(candidate),
                    workflow_permanent_id=workflow.workflow_permanent_id,
                )

    async def _resolve_and_stamp_run_seed(
        self,
        *,
        workflow: Workflow,
        workflow_run: WorkflowRun,
        parameter_values: dict[str, Any],
        explicit_request_browser_profile_id: str | None,
        start_fresh: bool = False,
        allow_missing_browser_profile_key: bool = False,
    ) -> WorkflowRun:
        """Resolve the seed-precedence chain at run setup — before any browser creation, for every run
        type — and stamp the resolved profile plus its provenance (browser_seed_source) on the run.
        This is where the credential's saved profile is resolved (the mid-run login-block stamp lands
        too late for code/cached and early-block runs)."""
        # Resolve once: a run whose seed was already stamped is never re-resolved, so storage state or
        # a fresh credential selection can never change a settled seed.
        if workflow_run.browser_seed_source is not None:
            return workflow_run
        # A run bound to a live browser session is governed by that session: the browser manager
        # returns the already-running session rather than loading workflow_run.browser_profile_id, so
        # resolving/stamping a profile seed here would only record provenance for a profile that was
        # never loaded. Leave the session (and any profile it propagated) untouched.
        if workflow_run.browser_session_id:
            return workflow_run
        engine_enabled = await app.AGENT_FUNCTION.is_browser_memory_engine_enabled(workflow_run)
        browser_profile_id, seed_source, sink_browser_profile_id = await self._resolve_run_seed(
            workflow=workflow,
            workflow_run=workflow_run,
            parameter_values=parameter_values,
            explicit_request_browser_profile_id=explicit_request_browser_profile_id,
            start_fresh=start_fresh,
            allow_missing_browser_profile_key=allow_missing_browser_profile_key,
            engine_enabled=engine_enabled,
        )
        updated_workflow_run = await app.DATABASE.workflow_runs.update_workflow_run(
            workflow_run_id=workflow_run.workflow_run_id,
            browser_profile_id=browser_profile_id,
            browser_seed_source=seed_source,
            browser_sink_profile_id=sink_browser_profile_id,
        )
        LOG.info(
            "Resolved run browser seed",
            workflow_run_id=workflow_run.workflow_run_id,
            workflow_permanent_id=workflow.workflow_permanent_id,
            browser_profile_id=browser_profile_id,
            browser_seed_source=seed_source,
            browser_sink_profile_id=sink_browser_profile_id,
        )
        # "Keep the same IP" for sign-ins with a credential: if the resolved seed is some credential's
        # profile and that credential pins its IP, apply its dedicated-IP headers to this run (covers an
        # explicit pick of a credential profile and rotation, not just seed_source=credential). This is a
        # new runtime effect (a proxy change), so it rides the engine kill-switch — flag-off keeps today's
        # proxy behavior exactly and a rollback disables the pin.
        if not engine_enabled:
            return updated_workflow_run
        return await self._maybe_pin_credential_profile_ip(
            workflow=workflow,
            workflow_run=updated_workflow_run,
            parameter_values=parameter_values,
            seed_profile_id=browser_profile_id,
            organization_id=workflow_run.organization_id,
        )

    async def _resolve_run_seed(
        self,
        *,
        workflow: Workflow,
        workflow_run: WorkflowRun,
        parameter_values: dict[str, Any],
        explicit_request_browser_profile_id: str | None,
        start_fresh: bool = False,
        allow_missing_browser_profile_key: bool = False,
        engine_enabled: bool = False,
    ) -> tuple[str | None, BrowserSeedSource, str | None]:
        """Resolve which profile SEEDS a run and which profile the run WRITES TO (the sink), returning
        (seed profile id, source, sink). C-semantics: an explicit pick
        "always starts there" and never forks into a hidden own profile; template+accumulate applies
        only when nothing is picked. The sink is None whenever no workflow write should happen (owned /
        read-only / override / fresh). The Browser Memory engine consumes the sink (never re-derives it)."""
        if start_fresh:
            return None, BrowserSeedSource.fresh, None
        if explicit_request_browser_profile_id:
            # v32: a run-form override is a pick FOR THAT RUN — under the engine a plain override writes
            # on success (sink = the override, like any living pick), a credential-owned / foreign override
            # stays heal-only (no workflow sink). Flag-off keeps the legacy read-only one-run override.
            override_sink: str | None = None
            if engine_enabled:
                override_role, _ = await self._resolve_picked_profile_role(
                    browser_profile_id=explicit_request_browser_profile_id,
                    workflow=workflow,
                    organization_id=workflow_run.organization_id,
                )
                override_sink = explicit_request_browser_profile_id if override_role == "plain" else None
            return explicit_request_browser_profile_id, BrowserSeedSource.override, override_sink

        # A credential-fallback retry deliberately sheds every browser handle for a clean session
        # (credential_fallback.py) so the fallback credential doesn't reconnect to the failed run's
        # account. Re-seeding the workflow's config pick / own memory / credential profile here would
        # undo that, so a fallback retry always seeds fresh.
        if workflow_run.retried_from_workflow_run_id:
            return None, BrowserSeedSource.fresh, None

        organization_id = workflow_run.organization_id

        # Explicit workflow pick (workflows.browser_profile_id) wins over browser_profile_key: a key
        # only selects WHICH own auto-profile applies in the no-pick+persist rows, so returning here —
        # before the persist/key branch below — makes a key sent alongside a pick deterministically
        # ignored (the UI keeps them mutually exclusive; the backend stays deterministic).
        pick = workflow.browser_profile_id
        if pick:
            role, _owner_credential_id = await self._resolve_picked_profile_role(
                browser_profile_id=pick, workflow=workflow, organization_id=organization_id
            )
            if role != "missing":
                # v32: an explicit pick is always LIVING under the engine — a plain pick is seed AND sink
                # (writes on success); the persist toggle no longer gates the pick sink (read-only picks
                # cease flag-on). Credential-owned, foreign-auto, and a transient-lookup "error" still take
                # no workflow sink (heal-only / read-only preserve). Flag-off keeps the legacy persist-
                # gated behavior byte-for-byte.
                if engine_enabled:
                    sink = pick if role == "plain" else None
                else:
                    sink = pick if (workflow.persist_browser_session and role == "plain") else None
                return pick, BrowserSeedSource.picked, sink
            # A genuinely deleted / cross-org pick: flag-ON falls through to the workflow's own managed
            # memory below (v32). Flag-OFF must stay byte-for-byte legacy — it kept the configured pick as
            # the seed and wrote the legacy session archive (sink None), never building the managed profile.
            if not engine_enabled:
                return pick, BrowserSeedSource.picked, None

        # No pick + Save ON: the workflow's own auto-profile accumulates (the only place the
        # credential fall-through survives). The sink is always the own profile — the run forks its own
        # and never cross-writes the credential.
        if workflow.persist_browser_session or resolve_reuse_browser_session(
            run_override=workflow_run.reuse_browser_session,
            workflow_default=workflow.reuse_browser_session,
        ):
            own_browser_profile_id = await self._ensure_managed_browser_profile(
                workflow=workflow,
                workflow_run=workflow_run,
                parameter_values=parameter_values,
                allow_missing_browser_profile_key=allow_missing_browser_profile_key,
            )
            if own_browser_profile_id:
                if await self._managed_browser_profile_has_content(
                    browser_profile_id=own_browser_profile_id, organization_id=organization_id
                ):
                    return own_browser_profile_id, BrowserSeedSource.own_memory, own_browser_profile_id
                # The first-run credential fall-through (row 5) is a flag-OFF fleet behavior change, so it
                # is engine-gated. Flag-off resolves to the own auto-profile (fresh run 1) and the mid-run
                # stamp keeps today's path.
                credential_browser_profile_id = await self._resolve_setup_credential_seed(
                    workflow=workflow,
                    workflow_run_id=workflow_run.workflow_run_id,
                    organization_id=organization_id,
                    engine_enabled=engine_enabled,
                    parameter_values=parameter_values,
                )
                if credential_browser_profile_id:
                    return credential_browser_profile_id, BrowserSeedSource.credential, own_browser_profile_id
                # Own memory exists but has no content yet. Engine era: seed fresh (run 1) and let the
                # sink-driven write populate own. Flag-off (legacy compat): seed the managed profile
                # itself so the legacy finalization writes it — today's Save & Reuse first-run behavior,
                # byte-for-byte. Returning None flag-off would divert the write to the session archive and
                # leave the managed profile permanently empty.
                if not engine_enabled:
                    return own_browser_profile_id, BrowserSeedSource.own_memory, own_browser_profile_id
                return None, BrowserSeedSource.own_memory, own_browser_profile_id
            # own ensure rolled back: fall through to the read-only resolution below.

        # No pick + Save OFF (or own ensure failed): the login credential's profile, read-only, else
        # fresh. No workflow sink either way (the heal engine keeps the credential profile current).
        # Engine-gated: seeding a credential's profile at setup for a pre-login block is a flag-OFF
        # behavior change, so flag-off falls to fresh and the preserved mid-run stamp keeps today's path.
        credential_browser_profile_id = await self._resolve_setup_credential_seed(
            workflow=workflow,
            workflow_run_id=workflow_run.workflow_run_id,
            organization_id=organization_id,
            engine_enabled=engine_enabled,
            parameter_values=parameter_values,
        )
        if credential_browser_profile_id:
            return credential_browser_profile_id, BrowserSeedSource.credential, None

        return None, BrowserSeedSource.fresh, None

    async def _resolve_setup_credential_seed(
        self,
        *,
        workflow: Workflow,
        workflow_run_id: str,
        organization_id: str,
        engine_enabled: bool,
        parameter_values: dict[str, Any],
    ) -> str | None:
        """Setup-time credential-profile seed, gated on the browser-memory engine. Flag-off returns None
        so the fleet keeps today's behavior (fresh until the login block's mid-run stamp); the engine era
        resolves it up front so code/cached and early-block runs seed correctly."""
        if not engine_enabled:
            return None
        return await self._resolve_credential_browser_profile_id_for_setup(
            workflow=workflow,
            workflow_run_id=workflow_run_id,
            organization_id=organization_id,
            parameter_values=parameter_values,
        )

    async def _resolve_picked_profile_role(
        self, *, browser_profile_id: str, workflow: Workflow, organization_id: str
    ) -> tuple[str, str | None]:
        """Classify an explicitly picked profile for the sink decision: "plain" (writable when Save is
        ON), "credential" (a credential owns it — no workflow sink, but IP-pin may apply), "foreign_auto"
        (another workflow's own auto — no workflow sink), "missing" (genuinely deleted / cross-org — falls
        through), or "error" (a lookup FAILURE — the caller preserves the pick read-only rather than
        silently rerouting an explicit pick to own-auto on a transient DB blip). Returns (role, owner)."""
        try:
            profile = await app.DATABASE.browser_sessions.get_browser_profile(
                profile_id=browser_profile_id, organization_id=organization_id
            )
            if not profile:
                return "missing", None
            owning_credentials = await app.DATABASE.credentials.get_credentials_by_browser_profile_id(
                browser_profile_id=browser_profile_id, organization_id=organization_id
            )
            if owning_credentials:
                return "credential", owning_credentials[0].credential_id
            if profile.is_managed and profile.workflow_permanent_id != workflow.workflow_permanent_id:
                return "foreign_auto", None
            return "plain", None
        except Exception:
            LOG.warning(
                "Failed to classify picked browser profile; preserving the pick (error, not missing)",
                browser_profile_id=browser_profile_id,
                workflow_permanent_id=workflow.workflow_permanent_id,
                exc_info=True,
            )
            return "error", None

    async def _resolve_single_login_credential_ids_for_setup(
        self, *, workflow: Workflow, workflow_run_id: str, organization_id: str, parameter_values: dict[str, Any]
    ) -> list[str]:
        """Credential ids for the run's single unambiguous login block, resolved via the SAME rich path
        as the mid-run login-block stamp (workflow_run_context / rotation pool / DB fallback selection /
        static credential id). Resolving pool- and DB-selected credentials here is what lets setup seed
        and pin the same account the block signs into — the only loader that can win when a cached-script
        or pre-navigating block opens the browser before the login block runs, which locks the mid-run
        loader out. parameter_values carries the in-memory render values so a request-supplied
        WorkflowParameter/CREDENTIAL_ID resolves at setup, before run parameters are persisted.

        Returns an empty list when resolution must defer to the mid-run stamp: any conditional branching
        (the executing branch is unknown at setup) or not exactly one non-skip login block. Best-effort:
        a resolution failure defers rather than aborting setup."""
        all_blocks = get_all_blocks(workflow.workflow_definition.blocks)
        if any(isinstance(block, ConditionalBlock) for block in all_blocks):
            return []
        login_blocks = [b for b in all_blocks if isinstance(b, LoginBlock) and not b.skip_saved_profile]
        if len(login_blocks) != 1:
            return []
        try:
            return await self._resolve_login_block_credential_ids(
                login_blocks[0],
                workflow_run_id,
                organization_id,
                workflow.workflow_permanent_id,
                run_parameter_values=parameter_values,
            )
        except Exception:
            LOG.warning(
                "Failed to resolve setup login credential ids",
                workflow_permanent_id=workflow.workflow_permanent_id,
                exc_info=True,
            )
            return []

    async def _resolve_active_credential_pin_for_setup(
        self, *, workflow: Workflow, workflow_run_id: str, organization_id: str, parameter_values: dict[str, Any]
    ) -> tuple[str, str] | None:
        """The run's active single-login credential's dedicated-IP pin at setup — (credential_id,
        proxy_session_id) if that credential pins its IP, else None. Same single-unambiguous-login guard
        as the credential-profile seed (branches / multiple logins defer). B4: this pin wins over the
        seed profile's own pin (the site tracks IP per account)."""
        credential_ids = await self._resolve_single_login_credential_ids_for_setup(
            workflow=workflow,
            workflow_run_id=workflow_run_id,
            organization_id=organization_id,
            parameter_values=parameter_values,
        )
        for credential_id in credential_ids:
            try:
                db_cred = await app.DATABASE.credentials.get_credential(
                    credential_id=credential_id, organization_id=organization_id
                )
            except Exception:
                LOG.warning(
                    "Failed to resolve active credential pin for setup",
                    credential_id=credential_id,
                    workflow_permanent_id=workflow.workflow_permanent_id,
                    exc_info=True,
                )
                continue
            if db_cred and db_cred.pin_saved_session_ip and db_cred.proxy_session_id:
                return db_cred.credential_id, db_cred.proxy_session_id
        return None

    async def _maybe_pin_credential_profile_ip(
        self,
        *,
        workflow: Workflow,
        workflow_run: WorkflowRun,
        parameter_values: dict[str, Any],
        seed_profile_id: str | None,
        organization_id: str,
    ) -> WorkflowRun:
        """ "Keep the same IP" for sign-ins with a credential. B4: the run's active login credential's
        pin WINS over the seed profile's own pin (the site tracks IP per account); if the active
        credential isn't pinned, fall back to the seed profile's owning-credential pin. Best-effort — a
        failure never blocks setup."""
        try:
            if app.AGENT_FUNCTION.has_proxy_session_extra_http_headers(workflow_run.extra_http_headers):
                return workflow_run
            pin_source = "credential"
            proxy_session_id: str | None = None
            pinned_credential_id: str | None = None
            active = await self._resolve_active_credential_pin_for_setup(
                workflow=workflow,
                workflow_run_id=workflow_run.workflow_run_id,
                organization_id=organization_id,
                parameter_values=parameter_values,
            )
            if active:
                pinned_credential_id, proxy_session_id = active
            elif seed_profile_id:
                owners = await app.DATABASE.credentials.get_credentials_by_browser_profile_id(
                    browser_profile_id=seed_profile_id, organization_id=organization_id
                )
                owner = next((c for c in owners if c.pin_saved_session_ip and c.proxy_session_id), None)
                if owner:
                    proxy_session_id, pinned_credential_id, pin_source = (
                        owner.proxy_session_id,
                        owner.credential_id,
                        "seed_profile",
                    )
            if not proxy_session_id:
                return workflow_run
            headers = app.AGENT_FUNCTION.merge_proxy_session_extra_http_headers(
                dict(workflow_run.extra_http_headers or {}), proxy_session_id
            )
            LOG.info(
                "browser_memory.credential_ip_pin",
                workflow_run_id=workflow_run.workflow_run_id,
                pin_source=pin_source,
                credential_id=pinned_credential_id,
            )
            return await app.DATABASE.workflow_runs.update_workflow_run(
                workflow_run_id=workflow_run.workflow_run_id,
                extra_http_headers=headers,
                proxy_location=ProxyLocation.RESIDENTIAL_ISP,
            )
        except Exception:
            LOG.warning(
                "Failed to pin credential dedicated IP for run",
                workflow_run_id=workflow_run.workflow_run_id,
                exc_info=True,
            )
            return workflow_run

    async def _ensure_managed_browser_profile(
        self,
        *,
        workflow: Workflow,
        workflow_run: WorkflowRun,
        parameter_values: dict[str, Any],
        allow_missing_browser_profile_key: bool = False,
    ) -> str | None:
        """Get-or-create the workflow's managed browser profile (own memory), lazily seeding it from
        the legacy Save & Reuse archive on first creation and reconciling its proxy pin. Returns the
        profile id, or None when a freshly created row had to be rolled back (seed failed)."""
        if not (
            workflow.persist_browser_session
            or resolve_reuse_browser_session(
                run_override=workflow_run.reuse_browser_session,
                workflow_default=workflow.reuse_browser_session,
            )
        ):
            return None
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
        digest_key, credential_segment = self._managed_browser_profile_digest_key(
            workflow, parameter_values, rendered_key
        )
        digest = build_browser_profile_key_digest(digest_key)
        profile, created = await app.DATABASE.browser_sessions.get_or_create_managed_browser_profile(
            organization_id=workflow_run.organization_id,
            workflow_permanent_id=workflow.workflow_permanent_id,
            browser_profile_key_digest=digest,
            name=_build_managed_browser_profile_name(workflow.title, rendered_key or credential_segment),
        )
        # A credential-segmented profile has no legacy counterpart (the wpid_ archive predates
        # per-credential rotation and was never scoped to one credential) — seeding it would risk
        # loading a different credential's saved state onto this one, the exact bug this segment
        # exists to prevent. Skip seeding; the profile just starts empty and accumulates normally.
        if created and not credential_segment:
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
                return None

        await self._reconcile_managed_browser_profile_proxy_pin(
            workflow=workflow,
            profile=profile,
            browser_profile_key_digest=digest,
            organization_id=workflow_run.organization_id,
            proxy_location=workflow_run.proxy_location,
            workflow_run_id=workflow_run.workflow_run_id,
        )
        return profile.browser_profile_id

    async def _managed_browser_profile_has_content(self, *, browser_profile_id: str, organization_id: str) -> bool:
        """Whether the managed profile has a stored archive (a successful write happened). A row with
        no archive does not count — the seed profile keeps seeding until content exists. Best-effort:
        on a storage error treat as HAS-content (fail safe), since the probe (browser_profile_exists)
        returns False on any ClientError, not just 404 — a flaky read must never reseed a Save & Reuse
        run to fresh and overwrite its real accumulated own-memory at completion."""
        try:
            return await app.STORAGE.browser_profile_exists(organization_id, browser_profile_id)
        except Exception:
            LOG.warning(
                "Failed to check managed browser profile content; treating as has-content (fail safe)",
                browser_profile_id=browser_profile_id,
                exc_info=True,
            )
            return True

    async def _resolve_credential_browser_profile_id_for_setup(
        self,
        *,
        workflow: Workflow,
        workflow_run_id: str,
        organization_id: str,
        parameter_values: dict[str, Any],
    ) -> str | None:
        """Setup-time (pre-persist) variant of the login-block credential-profile resolution: resolves
        the run's login credential via the same rich path as the mid-run stamp (see
        _resolve_single_login_credential_ids_for_setup) so the credential's saved profile can seed the
        run before any browser is created.

        Only resolves when a single login block makes the credential unambiguous. With multiple login
        blocks (e.g. one per conditional branch) the executing block is unknown at setup, so resolution
        is deferred to the mid-run login-block stamp rather than risk seeding the wrong account."""
        credential_ids = await self._resolve_single_login_credential_ids_for_setup(
            workflow=workflow,
            workflow_run_id=workflow_run_id,
            organization_id=organization_id,
            parameter_values=parameter_values,
        )
        for credential_id in credential_ids:
            try:
                db_cred = await app.DATABASE.credentials.get_credential(
                    credential_id=credential_id,
                    organization_id=organization_id,
                )
                if not (db_cred and db_cred.browser_profile_id):
                    continue
                # An opt-out makes this credential seed like one with no linked profile, so a sibling
                # credential in the same pool still resolves normally.
                if credential_auto_profile_disabled(db_cred):
                    continue
                # Verify the profile still exists (mirrors the mid-run resolver). Best-effort: a
                # transient repository failure degrades to a fresh seed rather than failing setup.
                profile = await app.DATABASE.browser_sessions.get_browser_profile(
                    profile_id=db_cred.browser_profile_id,
                    organization_id=organization_id,
                )
            except Exception:
                LOG.warning(
                    "Failed to resolve credential browser profile for setup seed",
                    credential_id=credential_id,
                    workflow_permanent_id=workflow.workflow_permanent_id,
                    exc_info=True,
                )
                continue
            if profile:
                return db_cred.browser_profile_id
        return None

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

    @staticmethod
    def _profile_key_render_values(workflow: Workflow, workflow_request: WorkflowRequestBody) -> dict[str, Any]:
        """Workflow-parameter values used to render browser_profile_key at run-request time:
        workflow defaults overlaid with the request's provided values."""
        values: dict[str, Any] = {}
        for parameter in workflow.workflow_definition.parameters:
            if isinstance(parameter, WorkflowParameter) and parameter.default_value is not None:
                values[parameter.key] = parameter.default_value
        if workflow_request.data:
            values.update({key: value for key, value in workflow_request.data.items() if value is not None})
        return values

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

            parameter_values = self._profile_key_render_values(workflow, workflow_request)
            if extra_parameter_values:
                parameter_values.update(extra_parameter_values)

            rendered_key = None
            if workflow.browser_profile_key:
                rendered_key = render_browser_profile_key(workflow.browser_profile_key, parameter_values)
                if not rendered_key:
                    return None

            digest_key, credential_segment = self._managed_browser_profile_digest_key(
                workflow, parameter_values, rendered_key
            )
            digest = build_browser_profile_key_digest(digest_key)
            profile, created = await app.DATABASE.browser_sessions.get_or_create_managed_browser_profile(
                organization_id=organization_id,
                workflow_permanent_id=workflow.workflow_permanent_id,
                browser_profile_key_digest=digest,
                name=_build_managed_browser_profile_name(workflow.title, rendered_key or credential_segment),
            )
            # See _ensure_managed_browser_profile: a credential-segmented profile has no legacy
            # counterpart, so seeding it from the un-credentialed archive would risk leaking a
            # different credential's saved state onto it.
            if created and not credential_segment:
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
    def _declared_request_parameter_keys(
        workflow: Workflow,
        workflow_parameters: list[WorkflowParameter],
    ) -> set[str]:
        """Every request-data key the run path can consume: the workflow's parameter rows plus the
        definition's other parameter types, whose keys drive the per-run credential overrides."""
        declared_keys = {workflow_parameter.key for workflow_parameter in workflow_parameters}
        declared_keys.update(parameter.key for parameter in workflow.workflow_definition.parameters)
        return declared_keys

    @staticmethod
    def _bound_parameter_keys(keys: list[str]) -> list[str]:
        """Unknown keys come straight from the request, so bound them before they reach a log line
        or an error message: a caller can put an entire value where a parameter name belongs."""
        bounded = [
            key if len(key) <= MAX_REPORTED_PARAMETER_KEY_LENGTH else f"{key[:MAX_REPORTED_PARAMETER_KEY_LENGTH]}..."
            for key in keys[:MAX_REPORTED_PARAMETER_KEYS]
        ]
        if len(keys) > MAX_REPORTED_PARAMETER_KEYS:
            bounded.append(f"(+{len(keys) - MAX_REPORTED_PARAMETER_KEYS} more)")
        return bounded

    @staticmethod
    def _is_optional_credential_parameter(workflow_parameter: WorkflowParameter) -> bool:
        """A credential_id parameter with no default is an at-will credential: absence is
        allowed and the run proceeds without a credential, mirroring how an input parameter
        with no default value is treated. A credential with a default keeps requiring one."""
        return (
            workflow_parameter.workflow_parameter_type == WorkflowParameterType.CREDENTIAL_ID
            and workflow_parameter.default_value is None
        )

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

    @staticmethod
    async def _close_reused_session_best_effort(
        *,
        organization_id: str,
        session_id: str,
        reason: BrowserSessionCloseReason = BrowserSessionCloseReason.aborted,
    ) -> None:
        try:
            await app.PERSISTENT_SESSIONS_MANAGER.close_session(organization_id, session_id, reason=reason)
        except Exception:
            LOG.warning(
                "Failed to close unusable reused browser session",
                organization_id=organization_id,
                browser_session_id=session_id,
                exc_info=True,
            )

    @staticmethod
    async def _read_reused_session_owner_proof(
        *,
        organization_id: str,
        browser_session: PersistentBrowserSession,
    ) -> ReusedSessionOwnerProof:
        runnable_type = browser_session.runnable_type
        runnable_id = browser_session.runnable_id
        if runnable_id is None:
            return ReusedSessionOwnerProof(is_terminal=False)
        try:
            if runnable_type in ("workflow_run", SESSION_RETIREMENT_RUNNABLE_TYPE):
                owner = await app.DATABASE.workflow_runs.get_workflow_run(
                    workflow_run_id=runnable_id,
                    organization_id=organization_id,
                )
            elif runnable_type == "task":
                owner = await app.DATABASE.tasks.get_task(
                    task_id=runnable_id,
                    organization_id=organization_id,
                )
            else:
                LOG.warning(
                    "Refusing to reclaim reusable browser session from unknown runnable type",
                    organization_id=organization_id,
                    runnable_type=runnable_type,
                    runnable_id=runnable_id,
                )
                return ReusedSessionOwnerProof(is_terminal=False)
            if owner is not None and not owner.status.is_final():
                return ReusedSessionOwnerProof(is_terminal=False)

            # Capture the immutable generation and exact cached wrapper synchronously with the
            # terminal-owner observation. The trusted release must CAS both identities later.
            runnable_generation_id = browser_session.runnable_generation_id
            browser_state = (
                app.PERSISTENT_SESSIONS_MANAGER.get_cached_browser_state_for_release(
                    browser_session.persistent_browser_session_id,
                    expected_runnable_id=runnable_id,
                    expected_runnable_generation_id=runnable_generation_id,
                )
                if runnable_generation_id is not None
                else None
            )
            last_activity_at = browser_session.last_activity_at
            router_activity_is_stale = (
                last_activity_at is not None
                and datetime.now(UTC) - _as_utc(last_activity_at) > REUSED_SESSION_ROUTER_ACTIVITY_STALE_AFTER
            )
            return ReusedSessionOwnerProof(
                is_terminal=True,
                runnable_generation_id=runnable_generation_id,
                browser_state=browser_state,
                router_activity_is_stale=router_activity_is_stale,
                observed_last_activity_at=last_activity_at,
            )
        except Exception:
            LOG.warning(
                "Unable to read reusable browser session owner; treating it as live",
                organization_id=organization_id,
                runnable_type=runnable_type,
                runnable_id=runnable_id,
                exc_info=True,
            )
            return ReusedSessionOwnerProof(is_terminal=False)

    async def resolve_and_persist_reuse_bound_key(
        self,
        *,
        workflow: Workflow,
        workflow_run: WorkflowRun,
        effective_reuse: bool,
    ) -> tuple[WorkflowRun, bool]:
        """Resolve and persist the authoritative browser-reuse admission decision."""
        reuse_bound_key = (
            workflow_run.reuse_bound_key if workflow_run.reuse_bound_key is not None else REUSE_ADMISSION_OFF_DISABLED
        )
        colliding_keys: list[str] = []
        if workflow_run.browser_address:
            effective_reuse = False
            reuse_bound_key = REUSE_ADMISSION_OFF_DISABLED
            LOG.info(
                "Disabled reusable browser admission for workflow run with pinned browser address",
                organization_id=workflow_run.organization_id,
                workflow_run_id=workflow_run.workflow_run_id,
                workflow_permanent_id=workflow.workflow_permanent_id,
            )
        elif workflow_run.reuse_bound_key is not None:
            # A replay adopts the already-resolved tuple. The CAS below then succeeds only when
            # that complete tuple is unchanged, or re-reads a concurrently installed winner.
            effective_reuse = bool(workflow_run.reuse_browser_session)
        elif effective_reuse:
            try:
                parameter_tuples = await app.DATABASE.workflow_runs.get_workflow_run_parameters(
                    workflow_run_id=workflow_run.workflow_run_id,
                )
                parameter_values = {
                    workflow_parameter.key: run_parameter.value
                    for workflow_parameter, run_parameter in parameter_tuples
                }
                persisted_selections = await app.DATABASE.workflow_run_credential_selections.get_selections_for_run(
                    workflow_run.workflow_run_id
                )
                reuse_bound_key, colliding_keys = resolve_reuse_bound_key(
                    workflow,
                    parameter_values,
                    persisted_selections,
                )
            except Exception:
                LOG.warning(
                    "Reusable browser identity is unresolvable; using a fresh browser for this run",
                    organization_id=workflow_run.organization_id,
                    workflow_run_id=workflow_run.workflow_run_id,
                    workflow_permanent_id=workflow.workflow_permanent_id,
                    exc_info=True,
                )
                effective_reuse = False
                reuse_bound_key = REUSE_ADMISSION_OFF_UNRESOLVABLE
        if colliding_keys:
            LOG.warning(
                "Workflow run parameters collide with credential selection keys for reusable browser identity",
                workflow_run_id=workflow_run.workflow_run_id,
                parameter_keys=colliding_keys,
            )
        workflow_run = await app.DATABASE.workflow_runs.compare_and_set_reuse_admission(
            workflow_run_id=workflow_run.workflow_run_id,
            effective_reuse=effective_reuse,
            reuse_bound_key=reuse_bound_key,
        )
        admitted_reuse = bool(workflow_run.reuse_browser_session)
        LOG.info(
            "Persisted reusable browser admission before publication",
            organization_id=workflow_run.organization_id,
            workflow_run_id=workflow_run.workflow_run_id,
            workflow_permanent_id=workflow.workflow_permanent_id,
            effective_reuse=admitted_reuse,
            has_reuse_bound_key=(
                workflow_run.reuse_bound_key is not None and not is_reuse_admission_off(workflow_run.reuse_bound_key)
            ),
        )
        return workflow_run, admitted_reuse

    async def _claim_reused_session(
        self,
        *,
        organization_id: str,
        workflow_run_id: str,
        browser_session: PersistentBrowserSession,
    ) -> str:
        session_id = browser_session.persistent_browser_session_id
        if is_final_status(browser_session.status):
            raise BrowserSessionClosed(session_id)
        status = getattr(browser_session.status, "value", browser_session.status)
        readiness_timeout = (
            app.PERSISTENT_SESSIONS_MANAGER.get_browser_session_startup_timeout_seconds()
            if status == "created"
            else 0.0
        )
        readiness_started_at = time.monotonic()
        browser_address = await app.PERSISTENT_SESSIONS_MANAGER.get_browser_address_if_ready(
            session_id,
            organization_id,
            timeout=readiness_timeout,
        )
        if browser_address is None:
            if status == "created" and time.monotonic() - readiness_started_at < readiness_timeout:
                raise BrowserSessionAlreadyOccupiedError(
                    session_id,
                    browser_session.runnable_id or "starting",
                )
            raise BrowserSessionClosed(session_id)
        lease_generation_id = await app.PERSISTENT_SESSIONS_MANAGER.begin_session(
            browser_session_id=session_id,
            runnable_type="workflow_run",
            runnable_id=workflow_run_id,
            organization_id=organization_id,
        )
        current_context = skyvern_context.ensure_context()
        current_context.browser_session_id = session_id
        current_context.browser_session_runnable_id = workflow_run_id
        current_context.browser_session_runnable_generation_id = lease_generation_id
        return session_id

    async def _reused_session_lifetime_shortfall(
        self,
        *,
        organization_id: str,
        workflow_run_id: str,
        workflow_permanent_id: str,
        browser_session: PersistentBrowserSession,
    ) -> dict[str, object] | None:
        try:
            remaining_lifetime_seconds = await app.PERSISTENT_SESSIONS_MANAGER.remaining_lifetime_seconds(
                browser_session.persistent_browser_session_id,
                organization_id,
            )
        except Exception:
            LOG.warning(
                "Could not evaluate reusable browser session remaining lifetime; leaving reuse enabled",
                organization_id=organization_id,
                workflow_run_id=workflow_run_id,
                workflow_permanent_id=workflow_permanent_id,
                browser_session_id=browser_session.persistent_browser_session_id,
                exc_info=True,
            )
            return None
        if remaining_lifetime_seconds is None or remaining_lifetime_seconds >= REUSE_MIN_REMAINING_LIFETIME_SECONDS:
            return None
        return {
            "organization_id": organization_id,
            "workflow_run_id": workflow_run_id,
            "workflow_permanent_id": workflow_permanent_id,
            "bound_key": browser_session.bound_key,
            "browser_session_id": browser_session.persistent_browser_session_id,
            "retirement_reason": "below_lifetime_floor",
            "remaining_lifetime_seconds": remaining_lifetime_seconds,
            "min_remaining_lifetime_seconds": REUSE_MIN_REMAINING_LIFETIME_SECONDS,
        }

    async def _reused_session_renewal_failure(
        self,
        *,
        organization_id: str,
        workflow_run_id: str,
        workflow_permanent_id: str,
        browser_session: PersistentBrowserSession,
    ) -> dict[str, object] | None:
        try:
            await app.PERSISTENT_SESSIONS_MANAGER.renew_or_close_session(
                browser_session.persistent_browser_session_id,
                organization_id,
                workflow_run_id=workflow_run_id,
            )
        except BrowserSessionNotRenewable as error:
            return {
                "organization_id": organization_id,
                "workflow_run_id": workflow_run_id,
                "workflow_permanent_id": workflow_permanent_id,
                "bound_key": browser_session.bound_key,
                "browser_session_id": browser_session.persistent_browser_session_id,
                "retirement_reason": "not_renewable",
                "not_renewable_reason": str(error),
            }
        except Exception:
            LOG.warning(
                "Could not renew reusable browser session budget; leaving reuse enabled",
                organization_id=organization_id,
                workflow_run_id=workflow_run_id,
                workflow_permanent_id=workflow_permanent_id,
                browser_session_id=browser_session.persistent_browser_session_id,
                exc_info=True,
            )
        return None

    async def _adopt_reused_session(
        self,
        *,
        organization_id: str,
        workflow_run_id: str,
        expected_workflow_permanent_id: str,
        expected_bound_key: str | None,
        browser_session: PersistentBrowserSession,
        lifetime_floor_session_id: str | None = None,
    ) -> str | None:
        session_id = browser_session.persistent_browser_session_id
        workflow_identity_matches = browser_session.bound_workflow_permanent_id == expected_workflow_permanent_id or (
            browser_session.runnable_type == SESSION_RETIREMENT_RUNNABLE_TYPE
            and browser_session.bound_workflow_permanent_id is None
        )
        if not workflow_identity_matches or browser_session.bound_key != expected_bound_key:
            LOG.warning(
                "Refusing reusable browser session with mismatched binding identity",
                organization_id=organization_id,
                workflow_run_id=workflow_run_id,
                browser_session_id=session_id,
                expected_workflow_permanent_id=expected_workflow_permanent_id,
                expected_bound_key=expected_bound_key,
                actual_workflow_permanent_id=browser_session.bound_workflow_permanent_id,
                actual_bound_key=browser_session.bound_key,
            )
            return None
        if is_final_status(browser_session.status):
            latest = await app.DATABASE.browser_sessions.get_persistent_browser_session(
                session_id=session_id,
                organization_id=organization_id,
            )
            if latest is None or is_final_status(latest.status):
                return None
            raise BrowserSessionAlreadyOccupiedError(session_id, latest.runnable_id or "unknown")
        stale_owner_released = False
        stale_owner_id = browser_session.runnable_id
        if stale_owner_id and stale_owner_id != workflow_run_id:
            owner_proof = await self._read_reused_session_owner_proof(
                organization_id=organization_id,
                browser_session=browser_session,
            )
            if not owner_proof.is_terminal:
                raise BrowserSessionAlreadyOccupiedError(session_id, stale_owner_id)
            released = False
            if owner_proof.can_release:
                assert owner_proof.runnable_generation_id is not None
                assert owner_proof.browser_state is not None
                assert owner_proof.observed_last_activity_at is not None
                released = await app.PERSISTENT_SESSIONS_MANAGER.release_stale_browser_session(
                    session_id,
                    organization_id,
                    expected_runnable_id=stale_owner_id,
                    expected_runnable_generation_id=owner_proof.runnable_generation_id,
                    expected_browser_state=owner_proof.browser_state,
                    observed_last_activity_at=owner_proof.observed_last_activity_at,
                )
            if not released:
                LOG.info(
                    "Deferring terminal-owner lease release and respawning on a replacement session",
                    organization_id=organization_id,
                    workflow_run_id=workflow_run_id,
                    stale_runnable_type=browser_session.runnable_type,
                    stale_runnable_id=stale_owner_id,
                    browser_session_id=session_id,
                    router_activity_is_stale=owner_proof.router_activity_is_stale,
                )
                return None
            LOG.info(
                "Reclaimed reused browser session from stale runnable",
                organization_id=organization_id,
                workflow_run_id=workflow_run_id,
                stale_runnable_type=browser_session.runnable_type,
                stale_runnable_id=stale_owner_id,
                browser_session_id=session_id,
            )
            # A trusted stale-owner release clears runnable_id in the stored row; mirror that state for retirement.
            stale_owner_released = True

        if lifetime_floor_session_id == session_id and (browser_session.runnable_id is None or stale_owner_released):
            retirement = await self._reused_session_lifetime_shortfall(
                organization_id=organization_id,
                workflow_run_id=workflow_run_id,
                workflow_permanent_id=expected_workflow_permanent_id,
                browser_session=browser_session,
            )
            # A session's budget starts at started_at, so an unstarted session has nothing to renew.
            # The OSS manager rejects renewal before that point.
            if retirement is None and browser_session.started_at is not None:
                retirement = await self._reused_session_renewal_failure(
                    organization_id=organization_id,
                    workflow_run_id=workflow_run_id,
                    workflow_permanent_id=expected_workflow_permanent_id,
                    browser_session=browser_session,
                )
            if retirement is not None:
                raise ReusedSessionBelowLifetimeFloor(
                    browser_session=(
                        browser_session.model_copy(update={"runnable_id": None})
                        if stale_owner_released
                        else browser_session
                    ),
                    shortfall=retirement,
                )

        try:
            return await self._claim_reused_session(
                organization_id=organization_id,
                workflow_run_id=workflow_run_id,
                browser_session=browser_session,
            )
        except BrowserSessionAlreadyOccupiedError:
            raise
        except BrowserSessionClosed:
            latest = await app.DATABASE.browser_sessions.get_persistent_browser_session(
                session_id=session_id,
                organization_id=organization_id,
            )
            if latest is None or is_final_status(latest.status):
                return None
            if latest.runnable_id is not None:
                raise BrowserSessionAlreadyOccupiedError(session_id, latest.runnable_id)
            # A fresh read proves the failed liveness probe belongs to an ownerless session.
            return None

    async def _adopt_reused_session_with_occupancy_retry(
        self,
        *,
        organization_id: str,
        workflow_run_id: str,
        workflow_permanent_id: str,
        bound_key: str | None,
        browser_session: PersistentBrowserSession,
        lifetime_floor_session_id: str | None = None,
    ) -> tuple[str | None, PersistentBrowserSession]:
        candidate = browser_session
        for attempt in range(2):
            try:
                return (
                    await self._adopt_reused_session(
                        organization_id=organization_id,
                        workflow_run_id=workflow_run_id,
                        expected_workflow_permanent_id=workflow_permanent_id,
                        expected_bound_key=bound_key,
                        browser_session=candidate,
                        lifetime_floor_session_id=lifetime_floor_session_id,
                    ),
                    candidate,
                )
            except BrowserSessionAlreadyOccupiedError:
                if attempt > 0:
                    raise
                latest = await app.DATABASE.browser_sessions.get_live_bound_persistent_browser_session(
                    organization_id=organization_id,
                    workflow_permanent_id=workflow_permanent_id,
                    bound_key=bound_key,
                )
                if latest is None:
                    raise
                candidate = latest
                LOG.info(
                    "Retrying reusable browser session adoption after occupancy race",
                    organization_id=organization_id,
                    workflow_run_id=workflow_run_id,
                    browser_session_id=candidate.persistent_browser_session_id,
                )
        raise RuntimeError("Unreachable reusable browser session adoption state")

    async def _retire_reused_session_for_respawn(
        self,
        *,
        organization_id: str,
        workflow_run_id: str,
        workflow_permanent_id: str,
        bound_key: str | None,
        browser_session: PersistentBrowserSession,
        reason: BrowserSessionCloseReason = BrowserSessionCloseReason.aborted,
        lifetime_floor_session_id: str | None = None,
    ) -> str | None:
        session_id = browser_session.persistent_browser_session_id
        observed_workflow_permanent_id = browser_session.bound_workflow_permanent_id
        if observed_workflow_permanent_id is None:
            await self._close_reused_session_best_effort(
                organization_id=organization_id,
                session_id=session_id,
                reason=reason,
            )
            return None
        occupied_owner_id = browser_session.runnable_id
        # Binding retirement never clears the immutable lease. Match the owner ID so a same-owner
        # generation rotation can still move this lane to a replacement session.
        occupied_generation_id = None
        cleared = await app.DATABASE.browser_sessions.clear_persistent_browser_session_binding(
            session_id=session_id,
            organization_id=organization_id,
            expected_workflow_permanent_id=observed_workflow_permanent_id,
            expected_bound_key=browser_session.bound_key,
            retiring_workflow_run_id=workflow_run_id,
            expected_runnable_id=occupied_owner_id,
            expected_runnable_generation_id=occupied_generation_id,
        )
        if not cleared:
            latest = await app.DATABASE.browser_sessions.get_live_bound_persistent_browser_session(
                organization_id=organization_id,
                workflow_permanent_id=workflow_permanent_id,
                bound_key=bound_key,
            )
            if latest is None:
                return None
            try:
                adopted_session_id, latest = await self._adopt_reused_session_with_occupancy_retry(
                    organization_id=organization_id,
                    workflow_run_id=workflow_run_id,
                    workflow_permanent_id=workflow_permanent_id,
                    bound_key=bound_key,
                    browser_session=latest,
                    lifetime_floor_session_id=lifetime_floor_session_id,
                )
                latest_reason = (
                    reason if latest.persistent_browser_session_id == session_id else BrowserSessionCloseReason.aborted
                )
            except ReusedSessionBelowLifetimeFloor as below_floor:
                adopted_session_id, latest = None, below_floor.browser_session
                latest_reason = BrowserSessionCloseReason.expired
            if adopted_session_id is not None:
                return adopted_session_id
            latest_workflow_permanent_id = latest.bound_workflow_permanent_id
            if latest_workflow_permanent_id is None:
                return None
            latest_owner_id = latest.runnable_id
            # Lease columns are monotonic and never cleared. This retry runs only after adoption proves
            # the latest owner terminal, so omitting the generation pin is intentional.
            cleared = await app.DATABASE.browser_sessions.clear_persistent_browser_session_binding(
                session_id=latest.persistent_browser_session_id,
                organization_id=organization_id,
                expected_workflow_permanent_id=latest_workflow_permanent_id,
                expected_bound_key=latest.bound_key,
                retiring_workflow_run_id=workflow_run_id,
                expected_runnable_id=latest_owner_id,
                expected_runnable_generation_id=None,
            )
            if not cleared:
                return None
            if latest_owner_id is None:
                await self._close_reused_session_best_effort(
                    organization_id=organization_id,
                    session_id=latest.persistent_browser_session_id,
                    reason=latest_reason,
                )
            return None
        if occupied_owner_id is not None:
            LOG.info(
                "Unbound terminal-owner browser session without clearing its active lease",
                organization_id=organization_id,
                workflow_run_id=workflow_run_id,
                stale_runnable_id=occupied_owner_id,
                browser_session_id=session_id,
            )
            return None
        await self._close_reused_session_best_effort(
            organization_id=organization_id,
            session_id=session_id,
            reason=reason,
        )
        return None

    @staticmethod
    async def _reuse_browser_session_disabled_by_kill_switch(
        *,
        organization_id: str,
        workflow_permanent_id: str,
        workflow_run_id: str | None = None,
    ) -> bool:
        try:
            return await app.EXPERIMENTATION_PROVIDER.is_feature_enabled_cached(
                "REUSE_BROWSER_SESSION_KILL_SWITCH",
                workflow_permanent_id,
                properties={
                    "organization_id": organization_id,
                    "workflow_permanent_id": workflow_permanent_id,
                },
            )
        except Exception:
            LOG.warning(
                "Could not evaluate reusable browser kill switch; leaving reuse enabled",
                organization_id=organization_id,
                workflow_run_id=workflow_run_id,
                workflow_permanent_id=workflow_permanent_id,
                exc_info=True,
            )
            return False

    async def acquire_reused_session(
        self,
        organization: Organization,
        workflow: Workflow,
        workflow_run: WorkflowRun,
    ) -> str | None:
        """Find or create and claim the browser session bound to this workflow identity."""
        organization_id = organization.organization_id
        workflow_run_id = workflow_run.workflow_run_id
        workflow_permanent_id = workflow.workflow_permanent_id
        bound_key = workflow_run.reuse_bound_key
        if bound_key is None or is_reuse_admission_off(bound_key):
            return None
        browser_session = await app.DATABASE.browser_sessions.get_live_bound_persistent_browser_session(
            organization_id=organization_id,
            workflow_permanent_id=workflow_permanent_id,
            bound_key=bound_key,
        )
        if browser_session is not None:
            lifetime_floor_session_id = browser_session.persistent_browser_session_id
            close_reason = BrowserSessionCloseReason.aborted
            try:
                adopted_session_id, browser_session = await self._adopt_reused_session_with_occupancy_retry(
                    organization_id=organization_id,
                    workflow_run_id=workflow_run_id,
                    workflow_permanent_id=workflow_permanent_id,
                    bound_key=bound_key,
                    browser_session=browser_session,
                    lifetime_floor_session_id=lifetime_floor_session_id,
                )
            except ReusedSessionBelowLifetimeFloor as below_floor:
                LOG.info("Retiring reusable browser session before claim", **below_floor.shortfall)
                adopted_session_id, browser_session = None, below_floor.browser_session
                close_reason = BrowserSessionCloseReason.expired
            if adopted_session_id is not None:
                return adopted_session_id
            LOG.info(
                "Respawning unusable workflow-bound browser session",
                organization_id=organization_id,
                workflow_run_id=workflow_run_id,
                workflow_permanent_id=workflow_permanent_id,
                bound_key=bound_key,
                browser_session_id=browser_session.persistent_browser_session_id,
                browser_session_status=browser_session.status,
            )
            adopted_after_unbind_race = await self._retire_reused_session_for_respawn(
                organization_id=organization_id,
                workflow_run_id=workflow_run_id,
                workflow_permanent_id=workflow_permanent_id,
                bound_key=bound_key,
                browser_session=browser_session,
                reason=close_reason,
                lifetime_floor_session_id=lifetime_floor_session_id,
            )
            if adopted_after_unbind_race is not None:
                return adopted_after_unbind_race

        last_unusable_session_id: str | None = None
        for _ in range(2):
            try:
                browser_session = await app.PERSISTENT_SESSIONS_MANAGER.create_session(
                    organization_id=organization_id,
                    timeout_minutes=30,
                    proxy_location=workflow.proxy_location,
                    browser_profile_id=workflow_run.browser_profile_id,
                    inherit_profile_proxy=True,
                    bound_workflow_permanent_id=workflow_permanent_id,
                    bound_key=bound_key,
                )
            except IntegrityError:
                browser_session = await app.DATABASE.browser_sessions.get_live_bound_persistent_browser_session(
                    organization_id=organization_id,
                    workflow_permanent_id=workflow_permanent_id,
                    bound_key=bound_key,
                )
                if browser_session is None:
                    raise
                LOG.info(
                    "Adopting browser session created by concurrent reuse acquisition",
                    organization_id=organization_id,
                    workflow_run_id=workflow_run_id,
                    browser_session_id=browser_session.persistent_browser_session_id,
                )

            adopted_session_id, browser_session = await self._adopt_reused_session_with_occupancy_retry(
                organization_id=organization_id,
                workflow_run_id=workflow_run_id,
                workflow_permanent_id=workflow_permanent_id,
                bound_key=bound_key,
                browser_session=browser_session,
            )
            if adopted_session_id is not None:
                return adopted_session_id
            last_unusable_session_id = browser_session.persistent_browser_session_id
            adopted_after_unbind_race = await self._retire_reused_session_for_respawn(
                organization_id=organization_id,
                workflow_run_id=workflow_run_id,
                workflow_permanent_id=workflow_permanent_id,
                bound_key=bound_key,
                browser_session=browser_session,
            )
            if adopted_after_unbind_race is not None:
                return adopted_after_unbind_race

        raise BrowserSessionClosed(last_unusable_session_id or "unknown")

    @staticmethod
    async def _release_unstamped_reused_session_lease(
        *,
        organization_id: str,
        workflow_run_id: str,
        browser_session_id: str,
        lease_generation_id: str,
    ) -> None:
        release_task = asyncio.create_task(
            asyncio.wait_for(
                app.PERSISTENT_SESSIONS_MANAGER.release_browser_session(
                    browser_session_id,
                    organization_id,
                    expected_runnable_id=workflow_run_id,
                    expected_runnable_generation_id=lease_generation_id,
                ),
                timeout=UNSTAMPED_REUSED_SESSION_RELEASE_TIMEOUT_SECONDS,
            )
        )
        cancellation: asyncio.CancelledError | None = None
        released: bool | None = None
        while True:
            try:
                released = await asyncio.shield(release_task)
                break
            except asyncio.CancelledError as error:
                if release_task.cancelled():
                    LOG.warning(
                        "Unstamped reusable browser session lease release was cancelled",
                        organization_id=organization_id,
                        workflow_run_id=workflow_run_id,
                        browser_session_id=browser_session_id,
                    )
                    break
                if cancellation is None:
                    cancellation = error
            except TimeoutError:
                LOG.warning(
                    "Timed out releasing unstamped reusable browser session lease",
                    organization_id=organization_id,
                    workflow_run_id=workflow_run_id,
                    browser_session_id=browser_session_id,
                    timeout_seconds=UNSTAMPED_REUSED_SESSION_RELEASE_TIMEOUT_SECONDS,
                )
                break
            except Exception:
                LOG.warning(
                    "Failed to release unstamped reusable browser session lease",
                    organization_id=organization_id,
                    workflow_run_id=workflow_run_id,
                    browser_session_id=browser_session_id,
                    exc_info=True,
                )
                break
        if released is False:
            LOG.warning(
                "Unstamped reusable browser session lease no longer matched its owner and generation",
                organization_id=organization_id,
                workflow_run_id=workflow_run_id,
                browser_session_id=browser_session_id,
                lease_generation_id=lease_generation_id,
            )
        if cancellation is not None:
            raise cancellation

    async def _acquire_and_stamp_reused_session(
        self,
        *,
        organization: Organization,
        workflow: Workflow,
        workflow_run: WorkflowRun,
    ) -> tuple[WorkflowRun, str | None]:
        browser_session_id = await self.acquire_reused_session(organization, workflow, workflow_run)
        if browser_session_id is None:
            return workflow_run, None
        current_context = skyvern_context.ensure_context()
        lease_generation_id = current_context.browser_session_runnable_generation_id
        stamp_succeeded = False
        original_cancellation: asyncio.CancelledError | None = None
        try:
            if (
                current_context.browser_session_id != browser_session_id
                or current_context.browser_session_runnable_id != workflow_run.workflow_run_id
                or lease_generation_id is None
            ):
                raise RuntimeError("Reusable browser session acquisition did not produce a complete lease identity")
            workflow_run = await app.DATABASE.workflow_runs.update_workflow_run(
                workflow_run_id=workflow_run.workflow_run_id,
                browser_session_id=browser_session_id,
            )
            stamp_succeeded = True
            return workflow_run, browser_session_id
        except asyncio.CancelledError as error:
            original_cancellation = error
            raise
        finally:
            if not stamp_succeeded and lease_generation_id is not None:
                cleanup_cancellation: asyncio.CancelledError | None = None
                try:
                    await self._release_unstamped_reused_session_lease(
                        organization_id=organization.organization_id,
                        workflow_run_id=workflow_run.workflow_run_id,
                        browser_session_id=browser_session_id,
                        lease_generation_id=lease_generation_id,
                    )
                except asyncio.CancelledError as error:
                    cleanup_cancellation = error
                if (
                    current_context.browser_session_id == browser_session_id
                    and current_context.browser_session_runnable_id == workflow_run.workflow_run_id
                    and current_context.browser_session_runnable_generation_id == lease_generation_id
                ):
                    current_context.browser_session_id = None
                    current_context.browser_session_runnable_id = None
                    current_context.browser_session_runnable_generation_id = None
                if original_cancellation is not None:
                    raise original_cancellation
                if cleanup_cancellation is not None:
                    raise cleanup_cancellation

    @staticmethod
    async def _ensure_browser_session_lease(
        *,
        organization_id: str,
        workflow_run_id: str,
        browser_session_id: str,
    ) -> str | None:
        current_context = skyvern_context.ensure_context()
        if (
            current_context.browser_session_id == browser_session_id
            and current_context.browser_session_runnable_id == workflow_run_id
            and current_context.browser_session_runnable_generation_id is not None
        ):
            return current_context.browser_session_runnable_generation_id
        lease_generation_id = await app.PERSISTENT_SESSIONS_MANAGER.begin_session(
            browser_session_id=browser_session_id,
            runnable_type="workflow_run",
            runnable_id=workflow_run_id,
            organization_id=organization_id,
        )
        current_context.browser_session_id = browser_session_id
        current_context.browser_session_runnable_id = workflow_run_id
        current_context.browser_session_runnable_generation_id = lease_generation_id
        return lease_generation_id

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
        if pre_finally_status is None or not pre_finally_status.is_final():
            if refreshed_workflow_run := await app.DATABASE.workflow_runs.get_workflow_run(
                workflow_run_id=workflow_run_id,
                organization_id=organization_id,
            ):
                workflow_run = refreshed_workflow_run
                if workflow_run.status.is_final():
                    pre_finally_status = workflow_run.status
                    pre_finally_failure_reason = workflow_run.failure_reason
        if pre_finally_status is not None and pre_finally_status.is_final():
            LOG.info(
                "Preserving terminal workflow run status after post-run elapsed timeout",
                workflow_run_id=workflow_run_id,
                pre_finally_status=pre_finally_status,
            )
        else:
            workflow_run = await self.mark_workflow_run_as_timed_out(
                workflow_run_id=workflow_run_id,
                failure_reason=timeout_failure_reason,
                fallback_workflow_run=workflow_run,
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
                        fallback_workflow_run=workflow_run,
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
        requested_completion_contract: dict[str, Any] | None = None,
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
        browser_profile_id = workflow_run.browser_profile_id
        browser_session_id = browser_session_id or workflow_run.browser_session_id
        close_browser_on_completion = browser_session_id is None and not workflow_run.browser_address

        if not workflow.workflow_definition.blocks:
            failure_reason = "Workflow has no executable blocks."
            LOG.warning(
                "Workflow has no executable blocks",
                workflow_run_id=workflow_run_id,
                workflow_id=workflow.workflow_id,
                workflow_permanent_id=workflow.workflow_permanent_id,
                organization_id=organization_id,
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

        has_conditionals = workflow_script_service.workflow_has_conditionals(workflow)
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

        # Bind before anything can open a browser or a code worker. Re-bound here rather than
        # inherited from setup: a Temporal worker boots a fresh context, and a run must always
        # execute under the policy of the exact version stamped on it.
        try:
            await self.bind_browser_action_policy(workflow, run_with=workflow_run.run_with)
        except BrowserActionPolicyNotEnforceable as e:
            LOG.warning(
                "Workflow version cannot run under its browser action policy",
                workflow_run_id=workflow_run_id,
                organization_id=organization_id,
                workflow_permanent_id=workflow.workflow_permanent_id,
                policy_rejection_reasons=list(e.reasons),
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
                mask_secrets=getattr(workflow, "mask_secrets", False),
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

        if should_acquire_reused_session(
            browser_session_id=browser_session_id,
            start_fresh_browser=workflow_run.start_fresh_browser,
            run_override=workflow_run.reuse_browser_session,
            workflow_default=workflow.reuse_browser_session,
        ):
            try:
                workflow_run, browser_session_id = await self._acquire_and_stamp_reused_session(
                    organization=organization,
                    workflow=workflow,
                    workflow_run=workflow_run,
                )
                if browser_session_id is not None:
                    close_browser_on_completion = False
            except Exception as e:
                LOG.exception(
                    "Failed to acquire reusable browser session for workflow run",
                    workflow_run_id=workflow_run_id,
                    organization_id=organization_id,
                    workflow_permanent_id=workflow.workflow_permanent_id,
                )
                failure_reason = (
                    f"Failed to acquire reusable browser session for workflow run: "
                    f"{get_user_facing_exception_message(e)}"
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
                    close_browser_on_completion=False,
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
                await self._ensure_browser_session_lease(
                    organization_id=organization.organization_id,
                    workflow_run_id=workflow_run_id,
                    browser_session_id=browser_session_id,
                )
            except Exception as e:
                # An expired session is the caller's to resolve, and the run record already carries
                # the same message as failure_reason. Every other lease failure keeps its traceback.
                if isinstance(e, BrowserSessionClosed):
                    LOG.warning(
                        "Browser session expired before the workflow run could lease it",
                        browser_session_id=browser_session_id,
                        workflow_run_id=workflow_run_id,
                    )
                else:
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
                    failure_category=_browser_lease_failure_category(e),
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
                self._renew_browser_session_loop(browser_session_id, organization.organization_id, workflow_run_id),
                name=f"browser_session_renewal_{workflow_run_id}",
            )

        # Captured inside the try and consumed in the outer finally so status
        # finalization runs even when the body is cancelled or raises. Stays
        # None if we were cancelled before the block-execution step completed;
        # in that case there's no terminal-state intent to restore.
        pre_finally_status: WorkflowRunStatus | None = None
        pre_finally_failure_reason: str | None = None
        pre_finally_failure_category: list[dict] | None = None
        # Freeze the body outcome used by browser write-back; a later finally-block
        # failure must not discard valid session state produced by a successful body.
        pre_finally_browser_persistence_status: WorkflowRunStatus | None = None
        # A finally-only failure is a cleanup outcome, not evidence that replaying the
        # workflow with another credential can help.
        finally_block_set_terminal_outcome = False
        # When a terminal row is re-opened so its finally block can run, the write that
        # terminalized it already recorded the run's minutes up to that instant, and carrying
        # that instant makes the in-band re-finalization below record only what ran after it.
        # Out-of-band writers that terminalize the row mid-block (API cancel, the
        # heartbeat-timeout activity, the durable fail finalizer) carry no such instant and
        # still record now - started_at.
        run_minutes_recorded_through: datetime | None = None

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
                    # Assign the transient-UI capture arm at execution start — before any block
                    # capture, and for cached/code runs that never reach agent_step — using the run
                    # being executed, so an inline child workflow whose scoped context lacks full
                    # identity is attributed to its own run rather than the parent's.
                    await resolve_transient_ui_capture_arm(
                        current_context,
                        distinct_id=workflow_run.workflow_run_id,
                        organization_id=organization.organization_id,
                        workflow_permanent_id=workflow_run.workflow_permanent_id,
                    )
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
                # Capture the terminal intent so the finally's interrupted-body backstop
                # doesn't treat this deliberate return as an interruption.
                pre_finally_failure_reason = _require_elapsed_timeout_failure_reason(timeout_failure_reason)
                workflow_run = await self.mark_workflow_run_as_timed_out(
                    workflow_run_id=workflow_run_id,
                    failure_reason=pre_finally_failure_reason,
                    fallback_workflow_run=workflow_run,
                )
                pre_finally_status = WorkflowRunStatus.timed_out
                pre_finally_browser_persistence_status = pre_finally_status
                return workflow_run
            else:
                timeout_context = asyncio.timeout(max_elapsed_timeout_seconds)
                # Publish the same deadline the timeout enforces, so work that can block for
                # a long time can give up in time to still do something useful instead of
                # being cancelled with nothing done.
                if body_context := skyvern_context.current():
                    body_context.max_elapsed_deadline = asyncio.get_running_loop().time() + max_elapsed_timeout_seconds
                try:
                    async with timeout_context:
                        await execute_workflow_blocks()
                except TimeoutError:
                    if not timeout_context.expired():
                        raise
                    pre_finally_failure_reason = _require_elapsed_timeout_failure_reason(timeout_failure_reason)
                    workflow_run = await self.mark_workflow_run_as_timed_out(
                        workflow_run_id=workflow_run_id,
                        failure_reason=pre_finally_failure_reason,
                        fallback_workflow_run=workflow_run,
                    )
                    pre_finally_status = WorkflowRunStatus.timed_out
                    pre_finally_browser_persistence_status = pre_finally_status
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
                pre_finally_browser_persistence_status = pre_finally_status
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
                    pre_finally_browser_persistence_status = pre_finally_status

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
                            # A terminal write stamps ``finished_at`` in the same breath as it
                            # records run minutes, so the stamp is the instant those minutes
                            # were measured to. A row that reached a terminal status without one
                            # (bulk stuck-run cleanup writes status only) recorded nothing, and
                            # the re-finalization is then the run's first and only sample.
                            run_minutes_recorded_through = workflow_run.finished_at
                            workflow_run = await self._update_workflow_run_status(
                                workflow_run_id=workflow_run_id,
                                status=WorkflowRunStatus.running,
                                failure_reason=None,
                            )
                        finally_block_execution = await self._execute_finally_block_if_configured(
                            workflow=workflow,
                            workflow_run=workflow_run,
                            organization=organization,
                            browser_session_id=browser_session_id,
                        )
                        if isinstance(finally_block_execution, WorkflowRunDispatchStopped):
                            workflow_run = finally_block_execution.workflow_run
                            pre_finally_status = workflow_run.status
                            pre_finally_failure_reason = workflow_run.failure_reason
                            pre_finally_failure_category = workflow_run.failure_category
                        elif finally_block_execution is not None:
                            finally_block, finally_block_result = finally_block_execution
                            assert pre_finally_status is not None
                            status_before_finally_result = pre_finally_status
                            failure_reason_before_finally_result = pre_finally_failure_reason
                            (
                                finally_target_status,
                                finally_failure_reason,
                                finally_failure_category,
                            ) = self._resolve_block_terminal_outcome(
                                block=finally_block,
                                block_result=finally_block_result,
                            )
                            if not status_before_finally_result.is_final() and finally_target_status is not None:
                                # Keep the terminal outcome as intent until browser
                                # cleanup/write-back has landed. Sequential dependents
                                # remain blocked while this row is still running.
                                pre_finally_status = finally_target_status
                                pre_finally_failure_reason = finally_failure_reason
                                pre_finally_failure_category = (
                                    finally_failure_category
                                    if finally_failure_category is not None
                                    else self._classify_workflow_terminal_failure(
                                        finally_target_status,
                                        finally_failure_reason,
                                    )
                                )
                                finally_block_set_terminal_outcome = True
                            (
                                workflow_run,
                                pre_finally_status,
                                pre_finally_failure_reason,
                            ) = await self._apply_finally_block_result(
                                block=finally_block,
                                block_result=finally_block_result,
                                workflow_run=workflow_run,
                                pre_finally_status=status_before_finally_result,
                                pre_finally_failure_reason=failure_reason_before_finally_result,
                                defer_status_write=True,
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
                if pre_finally_browser_persistence_status is None:
                    # A timeout before the body outcome was captured must retain
                    # the terminal guard instead of treating ``None`` as success.
                    pre_finally_browser_persistence_status = pre_finally_status
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
            browser_persistence_status: WorkflowRunStatus | None = None
            browser_write_back_exhausted = False
            # Install the no-await tombstone before any final status becomes observable. Trusted
            # stale-owner release must defer from this point until browser teardown releases it.
            mark_stream_closing(workflow_run_id)
            if pre_finally_status is not None:
                browser_persistence_status = pre_finally_browser_persistence_status
                if browser_persistence_status is not None and not browser_persistence_status.is_final():
                    browser_persistence_status = WorkflowRunStatus.completed
                if browser_persistence_status == WorkflowRunStatus.completed:
                    try:
                        current_workflow_run = await self.get_workflow_run(workflow_run_id=workflow_run_id)
                    except BaseException:
                        # Healthy-only persistence (including credential banking)
                        # must never trust a stale nonterminal snapshot.
                        LOG.warning(
                            "Failed to refresh workflow run before healthy browser write-back",
                            workflow_run_id=workflow_run_id,
                            exc_info=True,
                        )
                        browser_persistence_status = None
                        browser_write_back_exhausted = True
                    else:
                        workflow_run = current_workflow_run
                        if current_workflow_run.status.is_final():
                            # An out-of-band timeout/cancellation won before
                            # cleanup. Preserve its status and do not bank state
                            # as if this were a healthy completion.
                            browser_persistence_status = current_workflow_run.status

                if browser_persistence_status == WorkflowRunStatus.completed:
                    # Sequential dependency gates clear on terminal status, so every
                    # browser write-back attempt (including the retry) must finish first.
                    for write_back_attempt in range(2):
                        if browser_cleanup_result is None:
                            try:
                                browser_cleanup_result = await self._clean_up_workflow_browser(
                                    workflow_run=workflow_run,
                                    close_browser_on_completion=close_browser_on_completion,
                                    browser_session_id=browser_session_id,
                                )
                            except BaseException:
                                LOG.warning(
                                    "Pre-finalization browser cleanup failed during execute_workflow cleanup",
                                    workflow_run_id=workflow_run_id,
                                    write_back_attempt=write_back_attempt + 1,
                                    exc_info=True,
                                )
                                if write_back_attempt == 1:
                                    browser_write_back_exhausted = True
                                continue
                        try:
                            current_workflow_run = await self.get_workflow_run(workflow_run_id=workflow_run_id)
                            workflow_run = current_workflow_run
                            if current_workflow_run.status.is_final():
                                browser_persistence_status = current_workflow_run.status
                                break
                            async with asyncio.timeout(BROWSER_SESSION_WRITE_BACK_TIMEOUT):
                                await self._persist_workflow_browser_session_if_needed(
                                    workflow=workflow,
                                    workflow_run=workflow_run,
                                    browser_state=browser_cleanup_result.browser_state,
                                    close_browser_on_completion=browser_cleanup_result.close_browser_on_completion,
                                    workflow_run_status=browser_persistence_status,
                                )
                            # Only suppress clean_up_workflow's write-back once this one has
                            # actually persisted.
                            browser_cleanup_result.browser_session_write_back_attempted = True
                            break
                        except BaseException:
                            LOG.warning(
                                "Pre-finalization browser session write-back failed during execute_workflow cleanup",
                                workflow_run_id=workflow_run_id,
                                write_back_attempt=write_back_attempt + 1,
                                exc_info=True,
                            )
                            if write_back_attempt == 1:
                                # Do not start another write-back after terminalizing the
                                # run; a sequential dependent could otherwise read while
                                # this predecessor is still updating its profile.
                                browser_write_back_exhausted = True
                try:
                    workflow_run = await asyncio.shield(
                        self._finalize_workflow_run_status(
                            workflow_run_id=workflow_run_id,
                            workflow_run=workflow_run,
                            is_partial_run=run_selection_is_partial(workflow, block_labels),
                            requested_completion_contract=requested_completion_contract,
                            pre_finally_status=pre_finally_status,
                            pre_finally_failure_reason=pre_finally_failure_reason,
                            pre_finally_failure_category=pre_finally_failure_category,
                            run_minutes_recorded_through=run_minutes_recorded_through,
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
            elif isinstance(sys.exc_info()[1], asyncio.CancelledError):
                # Cancellation ownership is decided at the activity layer: server-driven
                # cancellations (timeout/cancel/pause/reset) have their own finalizers with
                # concrete statuses, and the interrupted-activity backstop covers the rest
                # with cancellation-details discrimination and an INFRASTRUCTURE_ERROR
                # category. Finalizing here would race all of them with a generic failed.
                LOG.info(
                    "Skipping interrupted-run finalize for cancellation; owned at the activity layer",
                    workflow_run_id=workflow_run_id,
                )
            else:
                try:
                    escaped_error = sys.exc_info()[1]
                    cause_type = type(escaped_error).__name__ if isinstance(escaped_error, Exception) else None
                    if cause_type:
                        LOG.warning(
                            "Workflow execution raised before completion; finalizing escaped exception",
                            workflow_run_id=workflow_run_id,
                            error_type=cause_type,
                            exc_info=True,
                        )
                    # Keep cause_type in failure_reason so escaped app errors retain full
                    # diagnostic context.
                    failure_reason = (
                        WORKFLOW_RUN_FAILED_FAILURE_REASON_TEMPLATE.format(cause_type=cause_type)
                        if cause_type
                        else WORKFLOW_RUN_INTERRUPTED_FAILURE_REASON
                    )
                    # Exception types such as TimeoutError must not be mistaken for a page-load
                    # timeout when the reason classifier scans failure_reason.
                    failure_category = _WORKFLOW_RUN_ESCAPED_EXCEPTION_FAILURE_CATEGORY if cause_type else None
                    finalize_task = asyncio.ensure_future(
                        self.mark_workflow_run_as_failed_if_not_final(
                            workflow_run_id=workflow_run_id,
                            failure_reason=failure_reason,
                            failure_category=failure_category,
                            cascade_children=True,
                        )
                    )
                    while not finalize_task.done():
                        try:
                            await asyncio.shield(finalize_task)
                        except asyncio.CancelledError:
                            continue
                    finalized = finalize_task.result()
                    if finalized is not None:
                        workflow_run = finalized
                    else:
                        workflow_run = await self._current_row_after_lost_finalize(workflow_run_id, workflow_run)
                except BaseException:
                    LOG.warning(
                        "Failed to finalize interrupted workflow run during cleanup",
                        workflow_run_id=workflow_run_id,
                        exc_info=True,
                    )

            if getattr(workflow_run, "copilot_session_id", None):
                with contained_effect(
                    "publish Copilot origin-run runtime secrets",
                    workflow_run_id=workflow_run.workflow_run_id,
                ):
                    workflow_run_context = app.WORKFLOW_CONTEXT_MANAGER.get_workflow_run_context(
                        workflow_run.workflow_run_id
                    )
                    await publish_copilot_runtime_secret_values(
                        organization_id=workflow_run.organization_id,
                        workflow_run_id=workflow_run.workflow_run_id,
                        workflow_run_context=workflow_run_context,
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
                browser_persistence_status=browser_persistence_status,
                skip_browser_session_write_back=browser_write_back_exhausted,
                schedule_credential_fallback_retry=not finally_block_set_terminal_outcome,
            )

        return workflow_run

    async def _renew_browser_session_loop(
        self, browser_session_id: str, organization_id: str, workflow_run_id: str
    ) -> None:
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
                await app.PERSISTENT_SESSIONS_MANAGER.renew_or_close_session(
                    browser_session_id,
                    organization_id,
                    workflow_run_id=workflow_run_id,
                )
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
        run_context_for_selection = skyvern_context.current()
        if run_context_for_selection is not None:
            run_context_for_selection.run_block_labels = block_labels

        # Load script blocks if script is provided
        script_blocks_by_label: dict[str, Any] = {}
        loaded_script_module = None
        blocks_to_update: set[str] = set()
        in_process_script_execution_denied = False

        is_script_run = await self.should_run_script(workflow, workflow_run)

        # Resolve the workflow-block engine A/B once, before any block runs: eligibility is a
        # property of the whole run (see v3_ab_ineligibility_reason), and every block of a run must
        # share an arm. Covers the DAG executor too, which this method delegates to.
        current_context = skyvern_context.current()
        if current_context:
            await resolve_workflow_block_engine_arm(
                current_context,
                workflow_run_id=workflow_run_id,
                organization_id=organization_id,
                workflow_permanent_id=workflow_run.workflow_permanent_id,
                ineligibility_reason=v3_ab_ineligibility_reason(all_blocks, is_script_run=is_script_run),
            )
        else:
            LOG.warning(
                "No context to pin the workflow-block engine arm on; the run stays on control",
                workflow_run_id=workflow_run_id,
            )

        # A cached script can be superseded (e.g. a stale pin replaced by a platform static
        # script) — drop it here so the ensure_static_script path below re-creates the pin.
        if script and is_script_run:
            if await app.AGENT_FUNCTION.should_replace_cached_script(
                workflow=workflow,
                workflow_run=workflow_run,
                script=script,
            ):
                LOG.info(
                    "Cached script superseded; ignoring cached blocks for this run",
                    workflow_run_id=workflow_run_id,
                    script_id=script.script_id,
                )
                script = None
                script_is_pinned = False

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
                    written_paths = await script_service.load_scripts(script, script_files)

                    script_path = os.path.join(settings.TEMP_PATH, script.script_id, "main.py")
                    # Gate on what this run wrote, not on what is on disk. TEMP_PATH is reused
                    # across runs and keyed by script_id rather than revision, so an earlier run's
                    # copy can outlive the code it came from and make os.path.exists lie.
                    if script_path in written_paths:
                        # setup script run
                        parameter_tuples = await app.DATABASE.workflow_runs.get_workflow_run_parameters(
                            workflow_run_id=workflow_run.workflow_run_id
                        )
                        script_parameters = {wf_param.key: run_param.value for wf_param, run_param in parameter_tuples}

                        spec = importlib.util.spec_from_file_location("user_script", script_path)
                        if spec and spec.loader:
                            loaded_script_module = await _load_user_script_module(
                                script_path,
                                spec,
                                organization_id=organization_id,
                                workflow_run_id=workflow_run_id,
                                workflow_permanent_id=workflow.workflow_permanent_id,
                                workflow_id=workflow.workflow_id,
                                script_id=script.script_id,
                                script_revision_id=script.script_revision_id,
                            )
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
                                    # Static (marker) pins re-import the live platform module, so a
                                    # block persisted against a cached function that module no longer
                                    # defines would cache-miss and run the agent with the placeholder
                                    # goal "Static script: <cache_key>" instead of the block's real
                                    # prompt. Drop those stale blocks so they fall through to a
                                    # normal agent block, matching how a fresh pin resolves them
                                    # today. hasattr mirrors ensure_static_script's pin-time check.
                                    for stale_label in [
                                        label
                                        for label, sb in script_blocks_by_label.items()
                                        if not sb.requires_agent
                                        and (ck := _run_signature_cache_key(sb.run_signature)) is not None
                                        and not hasattr(loaded_script_module, ck)
                                    ]:
                                        LOG.info(
                                            "Dropping stale static-script block; cached function no "
                                            "longer exists, block will run via agent",
                                            block_label=stale_label,
                                            cache_key=_run_signature_cache_key(
                                                script_blocks_by_label[stale_label].run_signature
                                            ),
                                            script_id=script.script_id,
                                        )
                                        del script_blocks_by_label[stale_label]
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
                        # This run has no code for the script: either the revision's stored code
                        # is gone, or the fetch failed. Either way every run_signature would resolve
                        # against an empty namespace, and keeping the block map would report the run
                        # as "code" and suppress the regeneration below.
                        LOG.warning(
                            "Script file not found at path",
                            script_path=script_path,
                            script_id=script.script_id,
                        )
                        script_blocks_by_label = {}
            except InProcessScriptExecutionDenied as denial:
                if denial.fail_closed:
                    raise
                # Not logged again here: the gate already emitted the denial with its reason and
                # full run identity, and one line per denial is what the denial monitor counts.
                script_blocks_by_label = {}
                loaded_script_module = None
                in_process_script_execution_denied = True
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
        # should be created for this platform (e.g., ATS). This persists the
        # script to DB (pinned) on first run so it shows in the Code tab.
        # Partial runs must stay agent-only, matching get_workflow_script's
        # existing contract: bootstrapping here would re-open run_signature
        # compile/exec after that lookup deliberately returned no script.
        if is_script_run and not block_labels and not script_blocks_by_label:
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

        # A degradable tenant-module denial with no trusted static module recovered above must
        # not fall through to code_generation mode below (SKY-14323): that would regenerate a
        # cached revision the next run would deny again. Route the whole run to the agent instead.
        if in_process_script_execution_denied and (not script_blocks_by_label or loaded_script_module is None):
            is_script_run = False

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

        if in_process_script_execution_denied and not script_mode_active:
            await self._mark_script_fallback_triggered(
                workflow_run_id=workflow_run_id,
                valid_to_run_code=True,
                block_executed_with_code=False,
                block_label=None,
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

        # Per-block mints exist to cache block functions incrementally. A workflow
        # with no cacheable block types has nothing block-level to cache — its
        # script is fully derivable at end of run, so mint once there (SKY-13659).
        if not any(is_block_type_cacheable(block) for block in workflow.workflow_definition.blocks):
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
                workflow_run = await self._prepare_login_block_browser_profile(
                    block=block,
                    workflow_run=workflow_run,
                    workflow_run_id=workflow_run_id,
                    organization_id=organization_id,
                    browser_session_id=browser_session_id,
                )

            attempt_or_stop = await self._try_execute_block_with_script(
                workflow=workflow,
                workflow_run=workflow_run,
                block=block,
                workflow_run_id=workflow_run_id,
                organization_id=organization_id,
                script_blocks_by_label=script_blocks_by_label,
                loaded_script_module=loaded_script_module,
                is_script_run=is_script_run,
            )
            if isinstance(attempt_or_stop, WorkflowRunDispatchStopped):
                return attempt_or_stop.workflow_run, blocks_to_update, None, True, branch_metadata
            attempt = attempt_or_stop
            workflow_run_block_result = attempt.block_result
            block_executed_with_code = attempt.executed_with_code
            block_requires_agent = attempt.block_requires_agent

            if not block_executed_with_code:
                agent_execution = await self._execute_block_via_agent_if_allowed(
                    block=block,
                    workflow_run=workflow_run,
                    workflow_run_id=workflow_run_id,
                    organization_id=organization_id,
                    browser_session_id=browser_session_id,
                    parent_workflow_run_block_id=parent_workflow_run_block_id,
                    is_script_run=is_script_run,
                    script_blocks_by_label=script_blocks_by_label,
                    attempt=attempt,
                )
                if isinstance(agent_execution, WorkflowRunDispatchStopped):
                    return agent_execution.workflow_run, blocks_to_update, None, True, branch_metadata
                workflow_run_block_result, block_requires_agent = agent_execution
                if attempt.fallback_episode_id and workflow_run_block_result:
                    await self._enrich_fallback_episode_with_agent_actions(
                        block=block,
                        workflow_run_block_result=workflow_run_block_result,
                        fallback_episode_id=attempt.fallback_episode_id,
                        form_fields_for_episode=attempt.form_fields_for_episode,
                        organization_id=organization_id,
                    )

            await self._bank_login_block_credential_profile(
                block=block,
                workflow_run=workflow_run,
                workflow_run_block_result=workflow_run_block_result,
                block_executed_with_code=block_executed_with_code,
                organization_id=organization_id,
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
                    and (block_requires_agent or attempt.fallback_episode_id)
                    and workflow_run_block_result.status == BlockStatus.completed
                    and branch_metadata
                    and is_adaptive_caching(workflow, workflow_run)
                ):
                    await self._record_conditional_agent_episode(
                        workflow=workflow,
                        block=block,
                        branch_metadata=branch_metadata,
                        workflow_run_id=workflow_run_id,
                        organization_id=organization_id,
                    )

            if not workflow_run_block_result:
                if attempt.script_exception is not None:
                    exc_message = str(attempt.script_exception) or "<no message>"
                    no_block_result_reason = f"Script error ({type(attempt.script_exception).__name__}): {exc_message}"
                else:
                    no_block_result_reason = "Block result is None"
                updated_workflow_run = await self.mark_workflow_run_as_failed_if_not_final(
                    workflow_run_id=workflow_run_id, failure_reason=no_block_result_reason
                )
                workflow_run = updated_workflow_run or await self._current_row_after_lost_finalize(
                    workflow_run_id, workflow_run
                )
                return workflow_run, blocks_to_update, workflow_run_block_result, True, branch_metadata

            self._track_blocks_for_script_generation(
                workflow=workflow,
                workflow_run=workflow_run,
                workflow_run_id=workflow_run_id,
                block=block,
                workflow_run_block_result=workflow_run_block_result,
                block_executed_with_code=block_executed_with_code,
                is_script_run=is_script_run,
                script_blocks_by_label=script_blocks_by_label,
                blocks_to_update=blocks_to_update,
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

        except _WorkflowRunDispatchScopeError:
            raise
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
            updated_workflow_run = await self.mark_workflow_run_as_failed_if_not_final(
                workflow_run_id=workflow_run_id, failure_reason=failure_reason
            )
            workflow_run = updated_workflow_run or await self._current_row_after_lost_finalize(
                workflow_run_id, workflow_run
            )
            return workflow_run, blocks_to_update, workflow_run_block_result, True, branch_metadata

    async def _dispatch_workflow_run_block(
        self,
        workflow_run_id: str,
        execute: Callable[[], Awaitable[_T1]],
    ) -> _T1 | WorkflowRunDispatchStopped:
        start_gate = asyncio.Event()
        executor_task: asyncio.Task[_T1] | None = None

        async def execute_after_admission() -> _T1:
            await start_gate.wait()
            return await execute()

        try:
            async with app.DATABASE.workflow_runs.admit_workflow_run_block_dispatch(workflow_run_id) as workflow_run:
                if workflow_run.status.is_final():
                    return WorkflowRunDispatchStopped(workflow_run=workflow_run)
                executor_task = asyncio.create_task(execute_after_admission())
        except BaseException as exc:
            if executor_task is not None:
                executor_task.cancel()
                with suppress(asyncio.CancelledError):
                    await executor_task
            if not isinstance(exc, Exception):
                raise
            raise _WorkflowRunDispatchScopeError(str(exc)) from exc

        start_gate.set()
        assert executor_task is not None
        return await executor_task

    async def _record_conditional_agent_episode(
        self,
        *,
        workflow: Workflow,
        block: ConditionalBlock,
        branch_metadata: dict[str, Any],
        workflow_run_id: str,
        organization_id: str,
    ) -> None:
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
                            ev.get("original_expression") if ev else (b.criteria.expression if b.criteria else None)
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

    def _track_blocks_for_script_generation(
        self,
        *,
        workflow: Workflow,
        workflow_run: WorkflowRun,
        workflow_run_id: str,
        block: BlockTypeVar,
        workflow_run_block_result: BlockResult,
        block_executed_with_code: bool,
        is_script_run: bool,
        script_blocks_by_label: dict[str, Any],
        blocks_to_update: set[str],
    ) -> None:
        # Determine which block statuses are eligible for caching
        cacheable_statuses = {BlockStatus.completed}
        if workflow.generate_script_on_terminal:
            cacheable_statuses.add(BlockStatus.terminated)

        if (
            not block_executed_with_code
            and block.label
            and block.label not in script_blocks_by_label
            and workflow_run_block_result.status in cacheable_statuses
            and is_block_type_cacheable(block)
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

    async def _try_execute_block_with_script(
        self,
        *,
        workflow: Workflow,
        workflow_run: WorkflowRun,
        block: BlockTypeVar,
        workflow_run_id: str,
        organization_id: str,
        script_blocks_by_label: dict[str, Any],
        loaded_script_module: Any,
        is_script_run: bool,
    ) -> ScriptBlockAttempt | WorkflowRunDispatchStopped:
        workflow_run_block_result: BlockResult | None = None
        block_executed_with_code = False
        valid_to_run_code = (
            is_script_run
            and loaded_script_module is not None
            and block.label
            and block.label in script_blocks_by_label
            and not block.disable_cache
            # A stale script_blocks_by_label entry can predate export being turned
            # on for this block (or predate this guard existing at all); the
            # generated code path only knows prompt/schema/url/model, so it would
            # silently skip the Parquet export. Force such blocks through the
            # agent path, which is where is_block_type_cacheable already keeps
            # them from being (re-)minted in the first place.
            and is_block_type_cacheable(block)
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
                    wrapper_code = f"async def __run_signature_wrapper():\n    return (\n{indented_signature}\n    )\n"

                LOG.debug("Executing run_signature wrapper", wrapper_code=wrapper_code)

                # Pre-init so the success log can reference output_value
                # when ScriptTerminationException was caught (terminated
                # is now in script_success_statuses).
                output_value: Any = None
                try:
                    exec_code = compile(wrapper_code, "<run_signature>", "exec")
                    exec(exec_code, exec_globals)
                    script_execution = await self._dispatch_workflow_run_block(
                        workflow_run_id,
                        exec_globals["__run_signature_wrapper"],
                    )
                    if isinstance(script_execution, WorkflowRunDispatchStopped):
                        return script_execution
                    output_value = script_execution
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
            except _WorkflowRunDispatchScopeError:
                raise
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
        return ScriptBlockAttempt(
            block_result=workflow_run_block_result,
            executed_with_code=block_executed_with_code,
            valid_to_run_code=bool(valid_to_run_code),
            block_requires_agent=block_requires_agent,
            fallback_episode_id=fallback_episode_id,
            form_fields_for_episode=form_fields_for_episode,
            script_exception=script_exception,
        )

    async def _execute_block_via_agent_if_allowed(
        self,
        *,
        block: BlockTypeVar,
        workflow_run: WorkflowRun,
        workflow_run_id: str,
        organization_id: str,
        browser_session_id: str | None,
        parent_workflow_run_block_id: str | None,
        is_script_run: bool,
        script_blocks_by_label: dict[str, Any],
        attempt: ScriptBlockAttempt,
    ) -> tuple[BlockResult | None, bool] | WorkflowRunDispatchStopped:
        workflow_run_block_result = attempt.block_result
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
            and is_block_type_cacheable(block)
        )
        block_is_non_cacheable = bool(is_script_run and not is_block_type_cacheable(block))
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
            agent_execution = await self._dispatch_workflow_run_block(
                workflow_run_id,
                lambda: block.execute_safe(
                    workflow_run_id=workflow_run_id,
                    parent_workflow_run_block_id=parent_workflow_run_block_id,
                    organization_id=organization_id,
                    browser_session_id=browser_session_id,
                ),
            )
            if isinstance(agent_execution, WorkflowRunDispatchStopped):
                return agent_execution
            workflow_run_block_result = agent_execution
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
                valid_to_run_code=attempt.valid_to_run_code,
                block_executed_with_code=attempt.executed_with_code,
                block_label=block.label,
            )
        return workflow_run_block_result, block_requires_agent

    async def _enrich_fallback_episode_with_agent_actions(
        self,
        *,
        block: BlockTypeVar,
        workflow_run_block_result: BlockResult,
        fallback_episode_id: str,
        form_fields_for_episode: list | None,
        organization_id: str,
    ) -> None:
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
                        # Decision rows are verdicts, not agent interactions.
                        countable_actions = [
                            a for a in actions if a.action_type not in (ActionType.COMPLETE, ActionType.TERMINATE)
                        ]
                        agent_action_count = len(countable_actions)
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
                    agent_actions_summary["failure_reason"] = str(workflow_run_block_result.failure_reason)[:2000]
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

    async def _login_block_performed_fresh_login(
        self,
        *,
        workflow_run_block_result: BlockResult,
        organization_id: str,
    ) -> bool:
        """A fresh sign-in typed a credential (INPUT_TEXT) or a 2FA code (VERIFICATION_CODE); a run
        already logged in via the seeded profile completes the check-if-logged-in goal with neither."""
        wrb_id = workflow_run_block_result.workflow_run_block_id
        if not wrb_id:
            return False
        wrb = await app.DATABASE.observer.get_workflow_run_block(
            workflow_run_block_id=wrb_id, organization_id=organization_id
        )
        if not wrb or not wrb.task_id:
            return False
        actions = await app.DATABASE.tasks.get_task_actions(task_id=wrb.task_id, organization_id=organization_id)
        return any(action.action_type in (ActionType.INPUT_TEXT, ActionType.VERIFICATION_CODE) for action in actions)

    async def _bank_login_block_credential_profile(
        self,
        *,
        block: BlockTypeVar,
        workflow_run: WorkflowRun,
        workflow_run_block_result: BlockResult | None,
        block_executed_with_code: bool,
        organization_id: str,
    ) -> None:
        """After an agent-executed LoginBlock completes, bank the run's selected credential profile
        (cloud-only via AGENT_FUNCTION; no-op in OSS). Cached/code replays do not bank in v1."""
        if (
            not isinstance(block, LoginBlock)
            or block_executed_with_code
            or workflow_run_block_result is None
            or workflow_run_block_result.status != BlockStatus.completed
        ):
            return
        try:
            browser_state = app.BROWSER_MANAGER.get_for_workflow_run(workflow_run.workflow_run_id)
            if browser_state is None:
                return
            credential_ids = await self._resolve_login_block_credential_ids(
                block=block,
                workflow_run_id=workflow_run.workflow_run_id,
                organization_id=organization_id,
                workflow_permanent_id=workflow_run.workflow_permanent_id,
            )
            if not credential_ids:
                return
            performed_fresh_login = await self._login_block_performed_fresh_login(
                workflow_run_block_result=workflow_run_block_result,
                organization_id=organization_id,
            )
            if performed_fresh_login:
                # A verified sign-in this run makes its end-state authoritative: the sink write-back
                # freshness guard then keeps the full write instead of a delta-merge.
                browser_state.browser_artifacts.mark_run_performed_fresh_login()
            # A login block binds one selected credential (rotation resolves to one);
            # multi-distinct-credential blocks bank the first — revisit if that shape appears.
            await app.AGENT_FUNCTION.bank_credential_profile_after_login(
                workflow_run=workflow_run,
                browser_state=browser_state,
                credential_id=credential_ids[0],
                performed_fresh_login=performed_fresh_login,
                login_url=block.url,
            )
        except Exception:
            LOG.warning(
                "Credential living-profile banking after login failed",
                workflow_run_id=workflow_run.workflow_run_id,
                exc_info=True,
            )

    def _rendered_login_block_url(self, block: BlockTypeVar, workflow_run_id: str) -> str | None:
        # The profile boot resolves the url before the block does, so mirror the block's own order: a
        # direct parameter key, then jinja. The blocked-host check still runs downstream at navigation.
        if not block.url:
            return block.url

        try:
            workflow_run_context = app.WORKFLOW_CONTEXT_MANAGER.get_workflow_run_context(workflow_run_id)
            raw_url = block.url
            if workflow_run_context.has_parameter(raw_url) and workflow_run_context.has_value(raw_url):
                raw_url = workflow_run_context.get_value(raw_url) or raw_url
            rendered_url = block.format_block_parameter_template_from_workflow_run_context(
                raw_url, workflow_run_context
            )
            if not rendered_url:
                return block.url
            # A no-op resolution stays byte-identical, except for a scheme-less value: those never boot
            # today, so prepending the scheme the block would have added cannot change working behavior.
            if rendered_url == block.url and rendered_url.lower().startswith(("http://", "https://")):
                return block.url
            return prepend_scheme_and_validate_url(rendered_url)
        except Exception:
            LOG.warning(
                "Login block URL did not render for the profile boot; using the raw block URL",
                workflow_run_id=workflow_run_id,
                block_label=block.label,
                exc_info=True,
            )
            return block.url

    async def _prepare_login_block_browser_profile(
        self,
        *,
        block: BlockTypeVar,
        workflow_run: WorkflowRun,
        workflow_run_id: str,
        organization_id: str,
        browser_session_id: str | None,
    ) -> WorkflowRun:
        # An explicit per-run override wins over the login block's credential profile. Short-circuit
        # BEFORE the credential proxy-pin so an override profile is never loaded through the credential's
        # IP-bound proxy, and never clobbered mid-run (retries key on browser_seed_source == override).
        # Engine-gated like the own_memory/picked guard below: flag-off keeps the legacy credential-load
        # path byte-for-byte; the credential mid-run path stays for the other, behavior-neutral sources.
        if (
            workflow_run.browser_seed_source == BrowserSeedSource.override
            and await app.AGENT_FUNCTION.is_browser_memory_engine_enabled(workflow_run)
        ):
            return workflow_run

        # A start_fresh_browser run boots empty and reads no saved memory by contract, so the mid-run
        # login-block must not load the credential's profile. Keyed on the persisted request flag, not
        # seed_source==fresh (which also covers ordinary no-credential and deferred multi-login runs
        # where the mid-run load is designed). The field is new, so this holds flag-off too — no legacy
        # run sets it.
        if workflow_run.start_fresh_browser:
            return workflow_run

        # Engine era: the seed decision is settled at setup. A run already seeded from a real
        # non-credential source (own memory or an explicit pick) must NOT run the mid-run credential
        # machinery — flipping the settled seed to `credential` would re-arm the healthy-run whole-dir
        # bank against the SHARED credential profile (a cross-profile write). Flag-off is untouched.
        if workflow_run.browser_seed_source in (
            BrowserSeedSource.own_memory,
            BrowserSeedSource.picked,
        ) and await app.AGENT_FUNCTION.is_browser_memory_engine_enabled(workflow_run):
            return workflow_run

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
            workflow_run=workflow_run,
        )
        # Save the original navigation goal before any mutation so
        # retries don't stack the browser-session prefix repeatedly.
        original_navigation_goal = block.navigation_goal
        if resolved_browser_profile_id:
            login_url = self._rendered_login_block_url(block, workflow_run_id)
            decision = await self._evaluate_debug_session_profile_decision(
                workflow_run=workflow_run,
                browser_session_id=browser_session_id,
                resolved_browser_profile_id=resolved_browser_profile_id,
                organization_id=organization_id,
            )

            if decision.incompatible_reason is not None:
                # The run's explicit browser session is profile-incompatible.
                # Live-session fidelity wins: attach that session so the run (or
                # the user watching a debug session) sees it, but DO NOT write
                # workflow_run.browser_profile_id, DO NOT rewrite the
                # navigation_goal, and DO NOT emit "skipping login agent".
                # Fall through to ordinary LoginBlock execution below.
                LOG.warning(
                    "Explicit browser session profile incompatible with LoginBlock credential",
                    code=DEBUG_SESSION_PROFILE_INCOMPATIBLE_CODE,
                    reason=decision.incompatible_reason,
                    workflow_run_id=workflow_run_id,
                    block_label=block.label,
                    debug_session_id=workflow_run.debug_session_id,
                    browser_session_id=browser_session_id,
                    credential_browser_profile_id=resolved_browser_profile_id,
                )
                if decision.attach_browser_session_id and login_url:
                    try:
                        await app.BROWSER_MANAGER.get_or_create_for_workflow_run(
                            workflow_run=workflow_run,
                            url=login_url,
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
                    url=login_url,
                )
                # A prior block may already have opened this run's browser; get_or_create_for_workflow_run
                # then returns that cached context and ignores browser_profile_id. Stamping the run
                # credential-seeded when that context did NOT boot from this profile would let the
                # healthy-run bank whole-dir an unrelated context into the shared credential profile.
                # Credential-seed only when the profile is genuinely in the browser: either we load it
                # into a fresh browser below, or the open browser already booted from it.
                browser_state_open = app.BROWSER_MANAGER.get_for_workflow_run(workflow_run_id)
                browser_already_open = browser_state_open is not None
                # A script/setup boot applies the setup-stamped seed itself; when the open browser was
                # booted from exactly the resolved profile, the credential seed is genuinely loaded.
                open_browser_booted_this_profile = (
                    browser_state_open is not None
                    and browser_state_open.browser_artifacts.applied_browser_profile_id == resolved_browser_profile_id
                )
                if browser_already_open and open_browser_booted_this_profile:
                    LOG.info(
                        "Credential login block reached with a browser already booted from the resolved "
                        "profile — keeping the credential seed",
                        workflow_run_id=workflow_run_id,
                        block_label=block.label,
                        browser_profile_id=resolved_browser_profile_id,
                    )
                if browser_already_open and not open_browser_booted_this_profile:
                    LOG.warning(
                        "Credential login block reached with a browser already open; running fresh login "
                        "instead of credential-seeding to avoid banking an unrelated context",
                        workflow_run_id=workflow_run_id,
                        block_label=block.label,
                        credential_browser_profile_id=resolved_browser_profile_id,
                    )
                    await app.DATABASE.workflow_runs.update_workflow_run(
                        workflow_run_id=workflow_run_id,
                        browser_profile_id=None,
                        browser_seed_source=BrowserSeedSource.degraded_fresh,
                    )
                    # Reload so the returned object reflects the degraded_fresh downgrade (mirrors the
                    # else-branch) — the healthy-run bank reads browser_seed_source off this object, and a
                    # stale "credential" would bank an unrelated context into the shared credential profile.
                    workflow_run = (
                        await app.DATABASE.workflow_runs.get_workflow_run(
                            workflow_run_id=workflow_run_id,
                            organization_id=organization_id,
                        )
                        or workflow_run
                    )
                else:
                    # Persist the browser_profile_id on the workflow_run so subsequent blocks create /
                    # reuse a browser with the saved profile. The seed is normally resolved at setup now;
                    # this mid-run stamp records credential provenance.
                    await app.DATABASE.workflow_runs.update_workflow_run(
                        workflow_run_id=workflow_run_id,
                        browser_profile_id=resolved_browser_profile_id,
                        browser_seed_source=BrowserSeedSource.credential,
                    )
                    workflow_run = (
                        await app.DATABASE.workflow_runs.get_workflow_run(
                            workflow_run_id=workflow_run_id,
                            organization_id=organization_id,
                        )
                        or workflow_run
                    )

                # Create the browser with the saved profile and navigate to the login block's URL. The
                # user enters the post-login target URL; the saved cookies authenticate once it loads.
                profile_loaded = open_browser_booted_this_profile or (bool(login_url) and not browser_already_open)
                if login_url and not browser_already_open:
                    try:
                        browser_state = await app.BROWSER_MANAGER.get_or_create_for_workflow_run(
                            workflow_run=workflow_run,
                            url=login_url,
                            browser_profile_id=resolved_browser_profile_id,
                            browser_session_id=decision.attach_browser_session_id,
                        )
                        if (
                            not decision.attach_browser_session_id
                            and browser_state.browser_artifacts.applied_browser_profile_id
                            != resolved_browser_profile_id
                        ):
                            # The chosen browser creator (e.g. a remote/vendor browser) accepted the
                            # profile id but never loaded it; the run must not stay credential-stamped.
                            raise BrowserProfileNotApplied(resolved_browser_profile_id)
                        working_page = await browser_state.get_working_page()
                        if working_page and working_page.url == "about:blank":
                            await browser_state.navigate_to_url(page=working_page, url=login_url)
                        # Wait for the page to settle so cookies/redirects complete
                        if working_page:
                            try:
                                await working_page.wait_for_load_state("networkidle", timeout=10000)
                            except Exception:
                                LOG.debug(
                                    "networkidle timeout after browser profile navigation (non-fatal)",
                                    workflow_run_id=workflow_run_id,
                                )
                    except BrowserProfileNotApplied:
                        LOG.info(
                            "Saved browser profile was not applied; falling back to normal login",
                            workflow_run_id=workflow_run_id,
                            block_label=block.label,
                            browser_profile_id=resolved_browser_profile_id,
                        )
                        profile_loaded = False
                    except Exception:
                        LOG.warning(
                            "Saved browser profile failed to load, falling back to normal login",
                            workflow_run_id=workflow_run_id,
                            block_label=block.label,
                            browser_profile_id=resolved_browser_profile_id,
                            exc_info=True,
                        )
                        profile_loaded = False

                    if not profile_loaded:
                        # Clear the profile so the normal login path doesn't reuse it, and record the
                        # degraded state (a resolved profile failed to load; the run continues fresh)
                        # so the run detail can surface it instead of the old silent fallback.
                        await app.DATABASE.workflow_runs.update_workflow_run(
                            workflow_run_id=workflow_run_id,
                            browser_profile_id=None,
                            browser_seed_source=BrowserSeedSource.degraded_fresh,
                        )
                        workflow_run = (
                            await app.DATABASE.workflow_runs.get_workflow_run(
                                workflow_run_id=workflow_run_id,
                                organization_id=organization_id,
                            )
                            or workflow_run
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
        return workflow_run

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
        workflow_run: WorkflowRun | None = None,
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
                    if workflow_run is not None:
                        engine_enabled = await app.AGENT_FUNCTION.is_browser_memory_engine_enabled(workflow_run)
                    else:
                        engine_enabled = await app.AGENT_FUNCTION.is_browser_memory_engine_enabled_for_org(
                            organization_id
                        )
                    if engine_enabled and credential_auto_profile_disabled(db_cred):
                        continue
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
        run_parameter_values: dict[str, Any] | None = None,
    ) -> list[str]:
        """Return credential ids bound to this block, preserving parameter order.

        run_parameter_values, when provided, supplies the in-memory render values (rotation selection,
        dereferenced indirection, or request value) and wins for both credential styles. Setup resolves
        the seed BEFORE the run context and run parameters are persisted, so the persisted reads below are
        empty then; mid-run (block execution) passes None and the persisted state is authoritative."""
        params = block.parameters

        # Pre-fetch run parameters once (used by WorkflowParameter/CREDENTIAL_ID style).
        run_param_tuples: list[tuple[Any, Any]] | None = None
        credential_ids: list[str] = []

        for param in params:
            credential_id: str | None = None

            # Style 1: CredentialParameter (has credential_id directly)
            if isinstance(param, CredentialParameter):
                # In-memory render values win when provided (setup): they already carry the rotation
                # selection and any dereferenced indirection, which the persisted-state resolver below
                # can't see until the run context / selections are persisted.
                if run_parameter_values is not None:
                    selected = run_parameter_values.get(param.key)
                    if isinstance(selected, str) and selected:
                        credential_id = selected
                if not credential_id:
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
                # In-memory render values win when provided: setup resolves the seed before run
                # parameters are persisted, so the DB read below is empty at that point.
                if run_parameter_values is not None:
                    selected = run_parameter_values.get(param.key)
                    if isinstance(selected, str) and selected:
                        credential_id = selected

                # Otherwise the credential_id is the persisted run-parameter value.
                if not credential_id:
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
        if not workflow_run_id or not organization_id or not workflow_permanent_id:
            return parameter.credential_id

        workflow_run_context = app.WORKFLOW_CONTEXT_MANAGER.workflow_run_contexts.get(workflow_run_id)
        if workflow_run_context:
            return await workflow_run_context.resolve_credential_parameter_id(parameter, organization_id)

        if parameter.credential_ids:
            return await select_credential_for_run(
                workflow_run_id=workflow_run_id,
                organization_id=organization_id,
                workflow_permanent_id=workflow_permanent_id,
                parameter_key=parameter.key,
                credential_ids=parameter.credential_ids,
                selection_strategy=parameter.selection_strategy,
            )
        if parameter.fallback_credential_ids:
            selected = await app.DATABASE.workflow_run_credential_selections.get_selection(
                workflow_run_id=workflow_run_id,
                parameter_key=parameter.key,
            )
            return selected or parameter.credential_id
        return parameter.credential_id

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
        """LoginBlock credential-profile decision: a run with an explicit
        browser_session_id (a debug session, or one supplied by a caller such as
        MCP's skyvern_login) attaches that live session and only skips the
        login agent when its saved profile matches the credential profile;
        mismatches surface a reason for downstream warning + fall-through. A
        run with no explicit session keeps the legacy behavior: the credential
        profile boots a fresh browser."""
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
        if block_result.status not in (
            BlockStatus.canceled,
            BlockStatus.failed,
            BlockStatus.terminated,
            BlockStatus.timed_out,
        ):
            return workflow_run, False
        if block_result.status == BlockStatus.canceled:
            LOG.info(
                f"Block with type {block.block_type} at index {block_idx}/{blocks_cnt - 1} was canceled for workflow run {workflow_run_id}, cancelling workflow run",
                block_type=block.block_type,
                workflow_run_id=workflow_run_id,
                block_idx=block_idx,
                block_status=block_result.status,
                block_type_var=block.block_type,
                block_label=block.label,
            )
            workflow_run = await self.mark_workflow_run_as_canceled(workflow_run_id=workflow_run_id)
            return workflow_run, True
        target_status, failure_reason, task_failure_category = self._resolve_block_terminal_outcome(
            block=block,
            block_result=block_result,
        )
        # The resolver returns no target for ``continue_on_failure``. Keep the
        # status-specific early returns below so each case retains its useful log.
        if block_result.status == BlockStatus.failed:
            # Run-level outcome, recorded as the run's failure_reason below; not a platform fault.
            LOG.warning(
                f"Block with type {block.block_type} at index {block_idx}/{blocks_cnt - 1} failed for workflow run {workflow_run_id}",
                block_type=block.block_type,
                workflow_run_id=workflow_run_id,
                block_idx=block_idx,
                block_status=block_result.status,
                block_type_var=block.block_type,
                block_label=block.label,
            )
            if block.continue_on_failure:
                LOG.warning(
                    f"Block with type {block.block_type} at index {block_idx}/{blocks_cnt - 1} failed but will continue executing the workflow run {workflow_run_id}",
                    block_type=block.block_type,
                    workflow_run_id=workflow_run_id,
                    block_idx=block_idx,
                    block_status=block_result.status,
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
                block_status=block_result.status,
                block_type_var=block.block_type,
                block_label=block.label,
            )

            if block.continue_on_failure:
                LOG.warning(
                    f"Block with type {block.block_type} at index {block_idx}/{blocks_cnt - 1} was terminated for workflow run {workflow_run_id}, but will continue executing the workflow run",
                    block_type=block.block_type,
                    workflow_run_id=workflow_run_id,
                    block_idx=block_idx,
                    block_status=block_result.status,
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
                block_status=block_result.status,
                block_type_var=block.block_type,
                block_label=block.label,
            )

            if block.continue_on_failure:
                LOG.warning(
                    f"Block with type {block.block_type} at index {block_idx}/{blocks_cnt - 1} timed out for workflow run {workflow_run_id}, but will continue executing the workflow run",
                    block_type=block.block_type,
                    workflow_run_id=workflow_run_id,
                    block_idx=block_idx,
                    block_status=block_result.status,
                    continue_on_failure=block.continue_on_failure,
                    block_type_var=block.block_type,
                    block_label=block.label,
                )
                return workflow_run, False

        if target_status == WorkflowRunStatus.failed:
            updated_workflow_run = await self.mark_workflow_run_as_failed_if_not_final(
                workflow_run_id=workflow_run_id,
                failure_reason=failure_reason,
                failure_category=task_failure_category,
            )
            workflow_run = updated_workflow_run or await self._current_row_after_lost_finalize(
                workflow_run_id, workflow_run
            )
        elif target_status == WorkflowRunStatus.terminated:
            updated_workflow_run = await self.mark_workflow_run_as_terminated_if_not_final(
                workflow_run_id=workflow_run_id,
                failure_reason=failure_reason,
                failure_category=task_failure_category,
            )
            workflow_run = updated_workflow_run or await self._current_row_after_lost_finalize(
                workflow_run_id, workflow_run
            )
        else:
            LOG.warning(
                "Block terminal outcome did not map to a workflow terminal status",
                workflow_run_id=workflow_run_id,
                block_status=block_result.status,
                target_status=target_status,
            )
            return workflow_run, False
        return workflow_run, True

    @staticmethod
    def _resolve_block_terminal_outcome(
        *,
        block: BlockTypeVar,
        block_result: BlockResult,
    ) -> tuple[WorkflowRunStatus | None, str | None, list[dict] | None]:
        failure_category = (
            block_result.output_parameter_value.get("failure_category")
            if isinstance(block_result.output_parameter_value, dict)
            else None
        )

        if block_result.status == BlockStatus.canceled:
            # Cancellation is never recoverable via continue_on_failure, matching normal block execution.
            return WorkflowRunStatus.canceled, None, failure_category
        if block.continue_on_failure:
            return None, None, None
        if block_result.status == BlockStatus.failed:
            failure_reason = f"{block.block_type} block failed. failure reason: {block_result.failure_reason}"
            return WorkflowRunStatus.failed, failure_reason, failure_category
        if block_result.status == BlockStatus.terminated:
            failure_reason = f"{block.block_type} block terminated. Reason: {block_result.failure_reason}"
            return WorkflowRunStatus.terminated, failure_reason, failure_category
        if block_result.status == BlockStatus.timed_out:
            # A block timeout is a block failure; timed_out is reserved for the workflow's elapsed-time limit.
            failure_reason = f"{block.block_type} block timed out. Reason: {block_result.failure_reason}"
            return WorkflowRunStatus.failed, failure_reason, failure_category
        return None, None, None

    @staticmethod
    def _classify_workflow_terminal_failure(
        status: WorkflowRunStatus,
        failure_reason: str | None,
    ) -> list[dict] | None:
        if status in (WorkflowRunStatus.failed, WorkflowRunStatus.timed_out):
            return classify_from_failure_reason(failure_reason, fallback_to_unknown=True)
        if status == WorkflowRunStatus.terminated:
            # Termination may be user-guided, so an unknown category is optional.
            return classify_from_failure_reason(failure_reason)
        return None

    async def _apply_finally_block_result(
        self,
        *,
        block: BlockTypeVar,
        block_result: BlockResult,
        workflow_run: WorkflowRun,
        pre_finally_status: WorkflowRunStatus,
        pre_finally_failure_reason: str | None,
        defer_status_write: bool = False,
    ) -> tuple[WorkflowRun, WorkflowRunStatus, str | None]:
        if pre_finally_status.is_final():
            return workflow_run, pre_finally_status, pre_finally_failure_reason

        target_status, failure_reason, failure_category = self._resolve_block_terminal_outcome(
            block=block,
            block_result=block_result,
        )
        if target_status is None:
            return workflow_run, pre_finally_status, pre_finally_failure_reason

        if failure_category is None:
            failure_category = self._classify_workflow_terminal_failure(target_status, failure_reason)

        LOG.info(
            "Resolved finally block terminal outcome",
            workflow_run_id=workflow_run.workflow_run_id,
            block_type=block.block_type,
            block_label=block.label,
            block_status=block_result.status,
            resolved_terminal_status=target_status,
            deferred=defer_status_write,
        )
        if defer_status_write:
            return workflow_run, target_status, failure_reason

        updated_workflow_run = await self._update_workflow_run_status_if_not_final(
            workflow_run_id=workflow_run.workflow_run_id,
            status=target_status,
            failure_reason=failure_reason,
            failure_category=failure_category,
        )
        if updated_workflow_run is not None:
            otel_trace.get_current_span().set_attribute("task.completion_status", target_status)
            return updated_workflow_run, target_status, updated_workflow_run.failure_reason

        current_workflow_run = await self._current_row_after_lost_finalize(
            workflow_run.workflow_run_id,
            workflow_run,
        )
        return current_workflow_run, current_workflow_run.status, current_workflow_run.failure_reason

    async def _execute_finally_block_if_configured(
        self,
        workflow: Workflow,
        workflow_run: WorkflowRun,
        organization: Organization,
        browser_session_id: str | None,
    ) -> tuple[BlockTypeVar, BlockResult] | WorkflowRunDispatchStopped | None:
        finally_block_label = workflow.workflow_definition.finally_block_label
        if not finally_block_label:
            return None

        label_to_block: dict[str, BlockTypeVar] = {block.label: block for block in workflow.workflow_definition.blocks}

        block = label_to_block.get(finally_block_label)
        if not block:
            LOG.warning(
                "Finally block label not found",
                workflow_run_id=workflow_run.workflow_run_id,
                finally_block_label=finally_block_label,
            )
            return None

        try:
            parameters = block.get_all_parameters(workflow_run.workflow_run_id)
            await app.WORKFLOW_CONTEXT_MANAGER.register_block_parameters_for_workflow_run(
                workflow_run.workflow_run_id, parameters, organization
            )
            finally_execution = await self._dispatch_workflow_run_block(
                workflow_run.workflow_run_id,
                lambda: block.execute_safe(
                    workflow_run_id=workflow_run.workflow_run_id,
                    organization_id=organization.organization_id,
                    browser_session_id=browser_session_id,
                ),
            )
            if isinstance(finally_execution, WorkflowRunDispatchStopped):
                return finally_execution
            block_result = finally_execution
            return block, block_result
        except Exception as e:
            LOG.warning(
                "Finally block execution failed",
                workflow_run_id=workflow_run.workflow_run_id,
                block_label=block.label,
                error=str(e),
            )
            return block, BlockResult(
                success=False,
                output_parameter=block.output_parameter,
                status=BlockStatus.failed,
                failure_reason=get_user_facing_exception_message(e),
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
            if block.next_block_label == finally_block_label:
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
        reuse_browser_session: bool = False,
        mask_secrets: bool = False,
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
        workflow_id: str | None = None,
        encrypt_secrets: bool = True,
    ) -> Workflow:
        try:
            if encrypt_secrets:
                await encrypt_workflow_definition_secrets(workflow_definition, organization_id)
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
                reuse_browser_session=reuse_browser_session,
                mask_secrets=mask_secrets,
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
                workflow_id=workflow_id,
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
        extracted_information_schema: dict[str, Any] | list | str | None = None,
        generate_script: bool = False,
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
                        data_schema=extracted_information_schema,
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
            generate_script_on_terminal=generate_script,
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

    def schedule_workflow_saved_hook(
        self,
        *,
        organization_id: str,
        edited_by: str | None,
        workflow_permanent_id: str,
    ) -> None:
        task = asyncio.create_task(
            app.AGENT_FUNCTION.on_workflow_saved(
                organization_id=organization_id,
                edited_by=edited_by,
                workflow_permanent_id=workflow_permanent_id,
            ),
        )
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

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
        reuse_browser_session: bool | None = None,
        mask_secrets: bool | None = None,
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
        notify_workflow_saved: bool = True,
        preserve_completion_contract: bool = True,
    ) -> Workflow:
        if workflow_definition is not None:
            if organization_id is not None:
                organization = await app.DATABASE.organizations.get_organization(organization_id=organization_id)
                if organization is not None:
                    await self._validate_and_normalize_credential_rotation_parameters(
                        workflow_definition.parameters,
                        organization,
                    )
            await encrypt_workflow_definition_secrets(workflow_definition, organization_id)
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
                reuse_browser_session=reuse_browser_session,
                mask_secrets=mask_secrets,
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
                preserve_completion_contract=preserve_completion_contract,
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
                reuse_browser_session=reuse_browser_session,
                mask_secrets=mask_secrets,
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

        if notify_workflow_saved:
            self.schedule_workflow_saved_hook(
                organization_id=updated_workflow.organization_id,
                edited_by=cast("str | None", edited_by) if edited_by is not _UNSET else None,
                workflow_permanent_id=updated_workflow.workflow_permanent_id,
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

            if plan.has_targets:
                cached_groups, published_groups = await self._partition_cached_blocks(
                    organization_id=organization_id,
                    workflow_permanent_id=previous_valid_workflow.workflow_permanent_id,
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

            to_delete = await app.DATABASE.scripts.get_workflow_scripts_by_permanent_id(
                organization_id=organization_id,
                workflow_permanent_id=previous_valid_workflow.workflow_permanent_id,
            )

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
        run_tags: Sequence[tuple[str | None, str | None]] | None = None,
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
            run_tags=run_tags,
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
        retried_from_workflow_run_id: str | None = None,
        fallback_attempt: int | None = None,
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

        # Sample the kill switch exactly once, before request-time precedence. This persisted
        # admission decision is authoritative: later flag changes stop new runs but never revoke an
        # in-flight run or strip the forced-session fallback that was selected at admission.
        browser_session_id = workflow_request.browser_session_id
        workflow: Workflow | None = None
        will_acquire_reused_session = False
        persisted_reuse_bound_key: str | None = None
        persisted_reuse_browser_session = workflow_request.reuse_browser_session
        if not browser_session_id:
            workflow = await self.get_workflow(workflow_id=workflow_id)
            configured_reuse = should_acquire_reused_session(
                browser_session_id=None,
                start_fresh_browser=workflow_request.start_fresh_browser,
                run_override=workflow_request.reuse_browser_session,
                workflow_default=workflow.reuse_browser_session,
            )
            if configured_reuse and await self._reuse_browser_session_disabled_by_kill_switch(
                organization_id=organization_id,
                workflow_permanent_id=workflow_permanent_id,
            ):
                persisted_reuse_browser_session = False
                persisted_reuse_bound_key = REUSE_ADMISSION_OFF_KILL_SWITCH
                LOG.warning(
                    "Reusable browser session disabled before workflow run creation",
                    organization_id=organization_id,
                    workflow_permanent_id=workflow_permanent_id,
                )
            else:
                will_acquire_reused_session = configured_reuse
        if not browser_session_id and not will_acquire_reused_session:
            force_browser_session = await app.EXPERIMENTATION_PROVIDER.is_feature_enabled_cached(
                "FORCE_BROWSER_SESSION",
                workflow_permanent_id,
                properties={
                    "organization_id": organization_id,
                    "workflow_permanent_id": workflow_permanent_id,
                },
            )
            if force_browser_session:
                assert workflow is not None
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
                    start_fresh_browser=workflow_request.start_fresh_browser,
                    reuse_browser_session=persisted_reuse_browser_session,
                    reuse_bound_key=persisted_reuse_bound_key,
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
                    retried_from_workflow_run_id=retried_from_workflow_run_id,
                    fallback_attempt=fallback_attempt,
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
                        parameter_values=self._profile_key_render_values(workflow, workflow_request),
                    )
                    # A start_fresh_browser run boots an empty browser and reads no saved memory, so the
                    # forced session must not load the workflow's managed profile (an explicit per-run
                    # override still wins over the fresh flag). Leaving forced_browser_profile_id None
                    # creates the forced session without a profile.
                    if not resolve_start_fresh(workflow_request.start_fresh_browser, browser_profile_id):
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
            start_fresh_browser=workflow_request.start_fresh_browser,
            reuse_browser_session=persisted_reuse_browser_session,
            reuse_bound_key=persisted_reuse_bound_key,
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
            retried_from_workflow_run_id=retried_from_workflow_run_id,
            fallback_attempt=fallback_attempt,
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

    def _schedule_workflow_run_terminal_hooks(
        self,
        *,
        workflow_run_id: str,
        workflow_id: str,
        organization_id: str,
        status: WorkflowRunStatus,
        workflow_run: WorkflowRun,
    ) -> None:
        hook_coroutines = (
            app.AGENT_FUNCTION.on_workflow_run_completed(
                organization_id=organization_id,
                workflow_id=workflow_id,
                workflow_run_id=workflow_run_id,
                status=status,
                workflow_run=workflow_run,
            ),
        )
        for hook_coroutine in hook_coroutines:
            task = asyncio.create_task(hook_coroutine)
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)

    async def _update_workflow_run_status(
        self,
        workflow_run_id: str,
        status: WorkflowRunStatus,
        failure_reason: str | None = None,
        run_with: str | None = None,
        ai_fallback: bool | None = None,
        failure_category: list[dict] | None = None,
    ) -> WorkflowRun:
        # Final transitions are claimed conditionally so run-minutes emit exactly once
        # per run even when a cancel and the run's own finalizer race; the loser still
        # performs the unconditional overwrite the finalizer has always done, silently.
        flipped_to_final = False
        workflow_run: WorkflowRun | None = None
        if status.is_final():
            workflow_run = await app.DATABASE.workflow_runs.update_workflow_run_if_not_final(
                workflow_run_id=workflow_run_id,
                status=status,
                failure_reason=failure_reason,
                run_with=run_with,
                ai_fallback=ai_fallback,
                failure_category=failure_category,
            )
            flipped_to_final = workflow_run is not None
        if workflow_run is None:
            workflow_run = await app.DATABASE.workflow_runs.update_workflow_run(
                workflow_run_id=workflow_run_id,
                status=status,
                failure_reason=failure_reason,
                run_with=run_with,
                ai_fallback=ai_fallback,
                failure_category=failure_category,
            )
        await self._after_workflow_run_status_write(workflow_run, status, emit_run_minutes=flipped_to_final)
        return workflow_run

    async def _update_workflow_run_status_if_not_final(
        self,
        workflow_run_id: str,
        status: WorkflowRunStatus,
        failure_reason: str | None = None,
        run_with: str | None = None,
        failure_category: list[dict] | None = None,
        finalized_by: str | None = None,
        run_minutes_recorded_through: datetime | None = None,
    ) -> WorkflowRun | None:
        """:meth:`_update_workflow_run_status` for writers that must lose a race against the
        run's own finalizer. Returns ``None`` when the row was already terminal."""
        workflow_run = await app.DATABASE.workflow_runs.update_workflow_run_if_not_final(
            workflow_run_id=workflow_run_id,
            status=status,
            failure_reason=failure_reason,
            run_with=run_with,
            failure_category=failure_category,
        )
        if workflow_run is None:
            return None
        await self._after_workflow_run_status_write(
            workflow_run,
            status,
            finalized_by=finalized_by,
            run_minutes_recorded_through=run_minutes_recorded_through,
        )
        return workflow_run

    async def _finish_preexisting_timed_out_workflow_run(
        self,
        workflow_run_id: str,
        failure_reason: str | None = None,
        run_with: str | None = None,
        failure_category: list[dict] | None = None,
        finalized_by: str | None = None,
    ) -> WorkflowRun | None:
        workflow_run = await app.DATABASE.workflow_runs.finish_preexisting_timed_out_workflow_run(
            workflow_run_id=workflow_run_id,
            failure_reason=failure_reason,
            run_with=run_with,
            failure_category=failure_category,
        )
        if workflow_run is None:
            return None
        # Bulk stuck-run cleanup writes only the status. Complete that deferred
        # terminal transition exactly once, when ``finished_at`` is still null.
        await self._after_workflow_run_status_write(
            workflow_run, WorkflowRunStatus.timed_out, finalized_by=finalized_by
        )
        return workflow_run

    async def _after_workflow_run_status_write(
        self,
        workflow_run: WorkflowRun,
        status: WorkflowRunStatus,
        emit_run_minutes: bool = True,
        finalized_by: str | None = None,
        run_minutes_recorded_through: datetime | None = None,
    ) -> None:
        workflow_run_id = workflow_run.workflow_run_id
        if status.is_final():
            # Free extraction-cache entries for this run.
            extraction_cache.clear_workflow_run(workflow_run_id)
            now = datetime.now(UTC)
            start_time = (
                workflow_run.started_at.replace(tzinfo=UTC)
                if workflow_run.started_at
                else workflow_run.created_at.replace(tzinfo=UTC)
            )
            queued_seconds = (start_time - workflow_run.created_at.replace(tzinfo=UTC)).total_seconds()
            duration_seconds = (now - start_time).total_seconds()
            # A run can reach a terminal status twice: the finally-block path terminalizes,
            # re-opens the row to `running` so the block can execute, then terminalizes again.
            # Both writes are real non-terminal -> terminal flips, so the second one records
            # only the compute the first one had not measured yet.
            recorded_seconds = (
                duration_seconds
                if run_minutes_recorded_through is None
                else (now - run_minutes_recorded_through.replace(tzinfo=UTC)).total_seconds()
            )
            LOG.info(
                "Workflow run duration metrics",
                workflow_run_id=workflow_run_id,
                workflow_id=workflow_run.workflow_id,
                queued_seconds=queued_seconds,
                duration_seconds=duration_seconds,
                recorded_seconds=recorded_seconds,
                workflow_run_status=workflow_run.status,
                organization_id=workflow_run.organization_id,
                run_with=workflow_run.run_with,
                ai_fallback=workflow_run.ai_fallback,
                trigger_type=workflow_run.trigger_type,
                workflow_schedule_id=workflow_run.workflow_schedule_id,
                task_v3_ab_arm=_task_v3_ab_arm_for_duration_log(workflow_run_id),
            )
            # Run minutes measure compute. A run finalized without ever reaching
            # `running` held no pod, and the created_at fallback above would bill its
            # whole queue age instead -- so it records as an exclusion (a zero-minute
            # sample tagged excluded=never_started) rather than silently vanishing.
            if emit_run_minutes and workflow_run.parent_workflow_run_id is None:
                await app.AGENT_FUNCTION.record_run_duration(
                    run_type="workflow_run",
                    status=str(status),
                    duration_seconds=recorded_seconds,
                    workflow_run_id=workflow_run_id,
                    organization_id=workflow_run.organization_id,
                    excluded_reason=None if workflow_run.started_at else "never_started",
                    finalized_by=finalized_by,
                )
            await self._apply_completion_run_tags_best_effort(workflow_run)
            self._schedule_workflow_run_terminal_hooks(
                workflow_run_id=workflow_run_id,
                organization_id=workflow_run.organization_id,
                workflow_id=workflow_run.workflow_id,
                status=status,
                workflow_run=workflow_run,
            )
        # Best-effort fire-and-forget write-through to task_runs table.
        # Runs off the hot path so workflow status transitions stay fast.
        bg = asyncio.create_task(
            self._sync_task_run_from_workflow_run(workflow_run, workflow_run_id, status),
        )
        self._background_tasks.add(bg)
        bg.add_done_callback(self._background_tasks.discard)

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
        pre_finally_failure_category: list[dict] | None = None,
        is_partial_run: bool = False,
        requested_completion_contract: dict[str, Any] | None = None,
        run_minutes_recorded_through: datetime | None = None,
    ) -> WorkflowRun:
        """
        Set final workflow run status based on pre-finally state.
        Called unconditionally to ensure unified flow.

        Terminal writes are conditional: an out-of-band finalizer (the interrupted-activity
        backstop) may have already terminalized the run and cascaded its children, and this
        shielded write can land after the activity's own cancellation — an unconditional
        write here would restore the parent while the children stay failed.
        """
        if pre_finally_status not in (
            WorkflowRunStatus.canceled,
            WorkflowRunStatus.failed,
            WorkflowRunStatus.terminated,
            WorkflowRunStatus.timed_out,
        ):
            contract_verdict = await self._grade_completion_contract(
                workflow_run,
                is_partial_run=is_partial_run,
                requested_completion_contract=requested_completion_contract,
            )
            if contract_verdict is not None and not contract_verdict.satisfied:
                updated = await self._update_workflow_run_status_if_not_final(
                    workflow_run_id=workflow_run_id,
                    status=WorkflowRunStatus.terminated,
                    failure_reason=contract_verdict.reason,
                )
                if updated is None:
                    return await self._current_row_after_lost_finalize(workflow_run_id, workflow_run)
                return updated
            updated = await self._update_workflow_run_status_if_not_final(
                workflow_run_id=workflow_run_id,
                status=WorkflowRunStatus.completed,
            )
            if updated is not None:
                otel_trace.get_current_span().set_attribute("task.completion_status", WorkflowRunStatus.completed)
                return updated
            return await self._current_row_after_lost_finalize(workflow_run_id, workflow_run)

        if not workflow_run.status.is_final():
            updated = await self._update_workflow_run_status_if_not_final(
                workflow_run_id=workflow_run_id,
                status=pre_finally_status,
                failure_reason=pre_finally_failure_reason,
                failure_category=pre_finally_failure_category,
                run_minutes_recorded_through=run_minutes_recorded_through,
            )
            if updated is None:
                return await self._current_row_after_lost_finalize(workflow_run_id, workflow_run)
            if pre_finally_status == WorkflowRunStatus.timed_out:
                await self._cascade_child_entities_on_terminal(workflow_run_id, WorkflowRunStatus.timed_out)
            return updated

        return workflow_run

    async def _session_download_artifact_ids(
        self, workflow_run: WorkflowRun, context: SkyvernContext | None, download_run_id: str | None
    ) -> set[str]:
        """This run's downloads still scoped to the browser session, whether the watcher already
        bound them to it or cleanup has yet to. Failure-tolerant: an unavailable read must not
        manufacture a shortfall."""
        browser_session_id = context.browser_session_id if context else None
        if not browser_session_id or not download_run_id:
            return set()
        try:
            # This run as producer plus still-unbound rows in its window: a reused session's
            # earlier downloads would otherwise satisfy a run that produced nothing.
            return await app.DATABASE.artifacts.list_session_download_artifact_ids_for_run(
                run_id=download_run_id,
                browser_session_id=browser_session_id,
                organization_id=workflow_run.organization_id,
                run_started_at=workflow_run.created_at,
            )
        except Exception:
            LOG.warning(
                "Session download read failed while grading; counting zero from this source",
                workflow_run_id=workflow_run.workflow_run_id,
                exc_info=True,
            )
            return set()

    async def _grade_completion_contract(
        self,
        workflow_run: WorkflowRun,
        *,
        is_partial_run: bool = False,
        requested_completion_contract: dict[str, Any] | None = None,
    ) -> ContractVerdict | None:
        """Grade the workflow's declared completion contract, or None when it declares nothing.

        Runs before the status write and after blocks have registered their files, so the verdict
        reflects what the run produced rather than what its code reported. Failure-tolerant: a
        grading error must never turn a good run into a bad one."""
        if is_partial_run:
            # A subset run was never asked to produce the whole workflow's deliverable.
            return None
        try:
            # The run is pinned to one workflow version; grading the permanent id would read
            # whatever version exists now, so an edit mid-run could judge this run by a contract
            # it never executed.
            workflow = await self.get_workflow(
                workflow_id=workflow_run.workflow_id,
                organization_id=workflow_run.organization_id,
            )
            criteria = parse_completion_contract(workflow.workflow_definition if workflow else None)
            if not criteria and requested_completion_contract is not None:
                # A copilot test run executes a version the request's obligation has not been
                # written onto yet — it attaches when the proposal is accepted, after this run.
                criteria = parse_completion_contract({"completion_contract": requested_completion_contract})
            if not criteria:
                return None
            context = skyvern_context.current()
            # Producer and consumer must resolve the same download key: a sub-workflow registers
            # files under the parent's run id, so the raw workflow_run_id would read an empty dir.
            download_run_id = resolve_run_download_id(context, fallback_run_id=workflow_run.workflow_run_id)
            files = await app.STORAGE.get_downloaded_files(
                organization_id=workflow_run.organization_id,
                run_id=download_run_id,
            )
            # Session-scoped downloads may not be registered under the run dir; including them
            # keeps a real download from reading as zero.
            session_download_ids = await self._session_download_artifact_ids(workflow_run, context, download_run_id)
            registered = files or []
            # The sources overlap on the same resolved run key once rows carry ids, so subtracting
            # the ids already present in `registered` counts a stamped file once. Without ids the
            # two reads address different storage prefixes (run dir vs browser_sessions/<id>/
            # downloads), so they are separate files and both count.
            registered_ids = {file.artifact_id for file in registered if file.artifact_id}
            download_count = len(registered) + len(session_download_ids - registered_ids)
            verdict = grade_completion_contract(criteria, registered_download_count=download_count)
            LOG.info(
                "workflow_completion_contract_graded",
                workflow_run_id=workflow_run.workflow_run_id,
                satisfied=verdict.satisfied,
                unmet_criterion_ids=list(verdict.unmet_criterion_ids),
                registered_download_count=len(registered),
                session_download_count=len(session_download_ids),
                graded_download_count=download_count,
            )
            return verdict
        except Exception:
            LOG.warning(
                "Completion-contract grading failed; leaving the run status unchanged",
                workflow_run_id=workflow_run.workflow_run_id,
                exc_info=True,
            )
            return None

    async def _current_row_after_lost_finalize(self, workflow_run_id: str, fallback: WorkflowRun) -> WorkflowRun:
        LOG.info(
            "In-band finalize lost to an out-of-band finalizer; leaving terminal status intact",
            workflow_run_id=workflow_run_id,
        )
        try:
            current = await self.get_workflow_run(workflow_run_id=workflow_run_id)
        except Exception:
            LOG.warning(
                "Failed to re-read workflow run after lost finalize; using in-memory row",
                workflow_run_id=workflow_run_id,
                exc_info=True,
            )
            return fallback
        return current or fallback

    async def mark_workflow_run_as_failed(
        self,
        workflow_run_id: str,
        failure_reason: str | None,
        run_with: str | None = None,
        failure_category: list[dict] | None = None,
        cascade_children: bool = False,
    ) -> WorkflowRun:
        LOG.info(
            f"Marking workflow run {workflow_run_id} as failed",
            workflow_run_id=workflow_run_id,
            failure_reason=failure_reason,
        )

        # Add workflow failure tag to trace
        otel_trace.get_current_span().set_attribute("task.completion_status", WorkflowRunStatus.failed)

        # Auto-classify if no explicit category provided
        failure_category_source = "inherited_from_task" if failure_category is not None else "code_level"
        if failure_category is None:
            failure_category = self._classify_workflow_terminal_failure(
                WorkflowRunStatus.failed,
                failure_reason,
            )

        LOG.info(
            "Workflow run failure classified",
            workflow_run_id=workflow_run_id,
            failure_category=failure_category,
            primary_failure_category=failure_category[0].get("category") if failure_category else None,
            failure_category_source=failure_category_source,
        )

        workflow_run = await self._update_workflow_run_status(
            workflow_run_id=workflow_run_id,
            status=WorkflowRunStatus.failed,
            failure_reason=failure_reason,
            run_with=run_with,
            failure_category=failure_category,
        )
        if cascade_children:
            # Opt-in: run-level failures normally leave child rows to their own executors,
            # but out-of-band finalization (an interrupted worker) has no executor left to do it.
            try:
                await self._do_cascade_child_entities(workflow_run_id, WorkflowRunStatus.failed)
            except Exception:
                LOG.exception("Failed to cascade child entity status", workflow_run_id=workflow_run_id)
        return workflow_run

    async def mark_workflow_run_as_failed_if_not_final(
        self,
        workflow_run_id: str,
        failure_reason: str | None,
        failure_category: list[dict] | None = None,
        cascade_children: bool = False,
    ) -> WorkflowRun | None:
        """Conditional failure finalize that no-ops when the run has already reached a terminal
        state. Out-of-band finalizers (the interrupted-activity backstop) run concurrently with
        ``execute_workflow``'s shielded ``_finalize_workflow_run_status`` write, so a
        read-then-write would clobber a real ``completed``/``timed_out`` status with ``failed``.
        Children cascade only on the transition this call actually won.
        """
        if failure_category is None:
            failure_category = self._classify_workflow_terminal_failure(
                WorkflowRunStatus.failed,
                failure_reason,
            )

        workflow_run = await self._update_workflow_run_status_if_not_final(
            workflow_run_id=workflow_run_id,
            status=WorkflowRunStatus.failed,
            failure_reason=failure_reason,
            failure_category=failure_category,
        )
        if workflow_run is None:
            return None

        otel_trace.get_current_span().set_attribute("task.completion_status", WorkflowRunStatus.failed)
        LOG.info(
            f"Marked workflow run {workflow_run_id} as failed (conditional)",
            workflow_run_id=workflow_run_id,
            failure_category=failure_category,
        )
        if cascade_children:
            try:
                await self._do_cascade_child_entities(workflow_run_id, WorkflowRunStatus.failed)
            except Exception:
                LOG.exception("Failed to cascade child entity status", workflow_run_id=workflow_run_id)
        return workflow_run

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
            failure_reason=failure_reason,
        )

        # Add workflow terminated tag to trace
        otel_trace.get_current_span().set_attribute("task.completion_status", WorkflowRunStatus.terminated)

        # Auto-classify if no explicit category provided.
        # Intentionally uses fallback_to_unknown=False (the default) — terminated workflows
        # may be user-guided (e.g. terminate_criterion matched), so None is acceptable.
        failure_category_source = "inherited_from_task" if failure_category is not None else "code_level"
        if failure_category is None:
            failure_category = self._classify_workflow_terminal_failure(
                WorkflowRunStatus.terminated,
                failure_reason,
            )

        LOG.info(
            "Workflow run failure classified",
            workflow_run_id=workflow_run_id,
            failure_category=failure_category,
            primary_failure_category=failure_category[0].get("category") if failure_category else None,
            failure_category_source=failure_category_source,
        )

        workflow_run = await self._update_workflow_run_status(
            workflow_run_id=workflow_run_id,
            status=WorkflowRunStatus.terminated,
            failure_reason=failure_reason,
            run_with=run_with,
            failure_category=failure_category,
        )
        return workflow_run

    async def mark_workflow_run_as_terminated_if_not_final(
        self,
        workflow_run_id: str,
        failure_reason: str | None,
        failure_category: list[dict] | None = None,
    ) -> WorkflowRun | None:
        if failure_category is None:
            failure_category = self._classify_workflow_terminal_failure(
                WorkflowRunStatus.terminated,
                failure_reason,
            )

        workflow_run = await self._update_workflow_run_status_if_not_final(
            workflow_run_id=workflow_run_id,
            status=WorkflowRunStatus.terminated,
            failure_reason=failure_reason,
            failure_category=failure_category,
        )
        if workflow_run is None:
            return None

        otel_trace.get_current_span().set_attribute("task.completion_status", WorkflowRunStatus.terminated)
        LOG.info(
            f"Marked workflow run {workflow_run_id} as terminated (conditional)",
            workflow_run_id=workflow_run_id,
            failure_category=failure_category,
        )
        return workflow_run

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
        failure_reason: str | None = None,
    ) -> WorkflowRun | None:
        """Conditional cancel that is a no-op when the run has already reached a
        terminal state. Safe to call from cancellation cleanup paths (e.g. the
        copilot tool's timeout branch) that race with the run's own
        ``_finalize_workflow_run_status`` writes.
        """
        updated = await app.DATABASE.workflow_runs.update_workflow_run_if_not_final(
            workflow_run_id=workflow_run_id,
            status=WorkflowRunStatus.canceled,
            failure_reason=failure_reason,
        )
        if updated is None:
            return None

        LOG.info(
            f"Marked workflow run {workflow_run_id} as canceled (conditional)",
            workflow_run_id=workflow_run_id,
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
        # This path finalizes once and never re-opens the run, so the recorded sample is
        # the whole duration. It is logged anyway so every emission site carries the field.
        LOG.info(
            "Workflow run duration metrics",
            workflow_run_id=workflow_run_id,
            workflow_id=updated.workflow_id,
            queued_seconds=queued_seconds,
            duration_seconds=duration_seconds,
            recorded_seconds=duration_seconds,
            workflow_run_status=updated.status,
            organization_id=updated.organization_id,
            run_with=updated.run_with,
            ai_fallback=updated.ai_fallback,
            trigger_type=updated.trigger_type,
            workflow_schedule_id=updated.workflow_schedule_id,
            task_v3_ab_arm=_task_v3_ab_arm_for_duration_log(workflow_run_id),
        )
        # Same compute gate as ``_after_workflow_run_status_write``: cancelling a run
        # that never started bills queue age, not compute, so it records as a tagged
        # zero-minute exclusion instead.
        if updated.parent_workflow_run_id is None:
            await app.AGENT_FUNCTION.record_run_duration(
                run_type="workflow_run",
                status=str(WorkflowRunStatus.canceled),
                duration_seconds=duration_seconds,
                workflow_run_id=workflow_run_id,
                organization_id=updated.organization_id,
                excluded_reason=None if updated.started_at else "never_started",
            )
        await self._apply_completion_run_tags_best_effort(updated)

        self._schedule_workflow_run_terminal_hooks(
            workflow_run_id=workflow_run_id,
            organization_id=updated.organization_id,
            workflow_id=updated.workflow_id,
            status=WorkflowRunStatus.canceled,
            workflow_run=updated,
        )

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
        fallback_workflow_run: WorkflowRun | None = None,
        finalized_by: str | None = None,
    ) -> WorkflowRun:
        LOG.info(
            f"Marking workflow run {workflow_run_id} as timed out",
            workflow_run_id=workflow_run_id,
        )

        failure_category = self._classify_workflow_terminal_failure(
            WorkflowRunStatus.timed_out,
            failure_reason,
        )
        LOG.info(
            "Workflow run failure classified",
            workflow_run_id=workflow_run_id,
            failure_category=failure_category,
            primary_failure_category=failure_category[0].get("category") if failure_category else None,
            failure_category_source="code_level",
        )

        updated_workflow_run = await self._update_workflow_run_status_if_not_final(
            workflow_run_id=workflow_run_id,
            status=WorkflowRunStatus.timed_out,
            failure_reason=failure_reason,
            run_with=run_with,
            failure_category=failure_category,
            finalized_by=finalized_by,
        )
        if updated_workflow_run is None:
            # The CAS lost to a terminal writer, so only a fresh row can identify
            # the winner. A supplied fallback is necessarily stale here; if the
            # refresh fails, propagate so the timeout activity retries the repair
            # and child cascade instead of accepting a nonterminal fallback.
            current_workflow_run = await self.get_workflow_run(workflow_run_id=workflow_run_id)
            if current_workflow_run.status != WorkflowRunStatus.timed_out:
                return current_workflow_run

            enriched_workflow_run = await self._finish_preexisting_timed_out_workflow_run(
                workflow_run_id=workflow_run_id,
                failure_reason=failure_reason,
                run_with=run_with,
                failure_category=failure_category,
                finalized_by=finalized_by,
            )
            if enriched_workflow_run is not None:
                updated_workflow_run = enriched_workflow_run
            else:
                updated_workflow_run = await self._current_row_after_lost_finalize(
                    workflow_run_id,
                    current_workflow_run,
                )
                if updated_workflow_run.status != WorkflowRunStatus.timed_out:
                    return updated_workflow_run

        otel_trace.get_current_span().set_attribute("task.completion_status", WorkflowRunStatus.timed_out)
        await self._cascade_child_entities_on_terminal(workflow_run_id, WorkflowRunStatus.timed_out)
        return updated_workflow_run

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
        workflow: Workflow | None = None,
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

        output_parameters_by_id = {
            output_parameter.output_parameter_id: output_parameter for output_parameter in output_parameters
        }
        if workflow is not None:
            for parameter in workflow.workflow_definition.parameters:
                if isinstance(parameter, OutputParameter):
                    output_parameters_by_id.setdefault(parameter.output_parameter_id, parameter)

        return [
            (output_parameters_by_id[workflow_run_output_parameter.output_parameter_id], workflow_run_output_parameter)
            for workflow_run_output_parameter in workflow_run_output_parameters
            if workflow_run_output_parameter.output_parameter_id in output_parameters_by_id
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
        cap_output_values: bool = False,
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
            cap_output_values=cap_output_values,
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
        cap_output_values: bool = False,
    ) -> WorkflowRunResponseBase:
        # ``cap_output_values`` defaults off so webhook delivery and replay keep full
        # fidelity; only the interactive read surfaces that must fit in one JSON
        # response opt in. See RUN_RESPONSE_MAX_VALUE_BYTES.
        #
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
        ) = await self._gather_with_max_in_flight(
            (
                lambda: app.DATABASE.workflows.get_workflow_for_workflow_run(
                    workflow_run_id,
                    organization_id=organization_id,
                    filter_deleted=not allow_deleted,
                ),
                lambda: self.get_workflow_run(workflow_run_id=workflow_run_id, organization_id=organization_id),
                lambda: app.DATABASE.observer.get_task_v2_by_workflow_run_id(
                    workflow_run_id=workflow_run_id,
                    organization_id=organization_id,
                ),
                lambda: app.DATABASE.tasks.get_tasks_by_workflow_run_id(workflow_run_id=workflow_run_id),
                lambda: app.DATABASE.workflow_runs.get_workflow_run_parameters(workflow_run_id=workflow_run_id),
                lambda: app.DATABASE.workflow_runs.get_workflow_run_block_errors(
                    workflow_run_id=workflow_run_id, organization_id=organization_id
                ),
            )
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
            retried_by_workflow_run_id,
        ) = await asyncio.gather(
            self.get_recent_workflow_screenshot_urls(
                workflow_run_id=workflow_run_id,
                organization_id=organization_id,
                workflow_run_tasks=workflow_run_tasks,
            ),
            self.get_output_parameter_workflow_run_output_parameter_tuples(
                workflow_id=workflow_run.workflow_id,
                workflow_run_id=workflow_run_id,
                workflow=workflow,
            ),
            self._fetch_recording_urls(workflow_run, task_v2, organization_id),
            self._fetch_downloaded_files(workflow_run, task_v2),
            app.DATABASE.workflow_runs.get_workflow_run_retried_by(
                workflow_run_id=workflow_run_id,
                organization_id=workflow_run.organization_id,
            ),
        )
        screenshot_urls: list[str] | None = screenshot_urls_raw or None
        # Preserve legacy singular contract: last element is the newest.
        recording_url = recording_urls[-1] if recording_urls else None

        outputs = None
        EXTRACTED_INFORMATION_KEY = "extracted_information"
        if output_parameter_tuples:

            def _cap(value: Any, output_key: str) -> Any:
                if not cap_output_values:
                    return value
                return truncate_oversized_response_value(value, workflow_run_id=workflow_run_id, output_key=output_key)

            # Collect from the raw values, not the capped ones: a loop output whose entries
            # are individually small but collectively over the cap still has every
            # iteration's extracted_information, and only the collected list gets capped.
            extracted_information: list[Any] = []
            for _, output in output_parameter_tuples:
                if output.value is not None:
                    extracted_information.extend(WorkflowService._collect_extracted_information(output.value))

            outputs = {
                output_parameter.key: _cap(output.value, output_parameter.key)
                for output_parameter, output in output_parameter_tuples
            }
            outputs[EXTRACTED_INFORMATION_KEY] = _cap(extracted_information, EXTRACTED_INFORMATION_KEY)
            # Refresh any expired presigned screenshot URLs in the outputs
            outputs = await self._refresh_output_urls(
                outputs, organization_id=organization_id, workflow_run_id=workflow_run_id
            )
            # The refresh expands artifact IDs into presigned URLs and FileInfo objects —
            # ~25x on an ID-dense value — so a value that fit before it can exceed the cap
            # after. Gating this on artifact IDs is not safe: the legacy `has_old_format`
            # branch rewrites task/workflow screenshots from a `task_id` alone, with no IDs
            # to detect. The pre-refresh pass above still stands: it bounds the refresh walk.
            if cap_output_values:
                outputs = {key: _cap(value, key) for key, value in outputs.items()}

        task_errors: list[dict[str, Any]] = []
        for task in workflow_run_tasks:
            task_errors.extend(task.errors)

        # Also collect block-level error codes (e.g. FILE_PARSER_ERROR) into the
        # same errors array so they appear in the top-level workflow run response,
        # matching the task-level error format. Uses a lightweight query that only
        # fetches blocks with non-null error_codes to avoid a full block load on
        # every status poll.
        workflow_run_context = app.WORKFLOW_CONTEXT_MANAGER.workflow_run_contexts.get(workflow_run_id)
        # Error reasoning is masked before persistence at both inline and secure-host ingress.
        # A completed run may have evicted its context, so aggregation masking is best-effort defense-in-depth.
        errors = _merge_workflow_run_errors(
            task_errors,
            block_errors,
            mask_reasoning=(
                workflow_run_context.mask_secrets_in_data
                if isinstance(workflow_run_context, WorkflowRunContext)
                else None
            ),
            registered_secret_values=(
                workflow_run_context.secrets.values() if isinstance(workflow_run_context, WorkflowRunContext) else ()
            ),
            workflow_run_id=workflow_run_id,
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
            failure_reason=(
                truncate_oversized_response_text(workflow_run.failure_reason)
                if cap_output_values
                else workflow_run.failure_reason
            ),
            failure_category=workflow_run.failure_category,
            retried_from_workflow_run_id=workflow_run.retried_from_workflow_run_id,
            retried_by_workflow_run_id=retried_by_workflow_run_id,
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
            browser_seed_source=workflow_run.browser_seed_source,
            max_screenshot_scrolls=workflow_run.max_screenshot_scrolls,
            task_v2=capped_task_v2(task_v2) if cap_output_values else task_v2,
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
        browser_cleanup_result = await app.BROWSER_MANAGER.cleanup_for_workflow_run(
            workflow_run.workflow_run_id,
            all_workflow_task_ids,
            close_browser_on_completion=close_browser_on_completion,
            browser_session_id=browser_session_id,
            organization_id=workflow_run.organization_id,
            child_workflow_run_ids=child_workflow_run_ids,
        )
        return WorkflowBrowserCleanupResult(
            browser_state=browser_cleanup_result.browser_state,
            tasks=tasks,
            all_workflow_task_ids=all_workflow_task_ids,
            child_workflow_run_ids=child_workflow_run_ids,
            close_browser_on_completion=browser_cleanup_result.recording_finalized,
        )

    async def _persist_workflow_browser_session_if_needed(
        self,
        workflow: Workflow,
        workflow_run: WorkflowRun,
        browser_state: BrowserState | None,
        close_browser_on_completion: bool,
        workflow_run_status: WorkflowRunStatus | None = None,
    ) -> None:
        effective_workflow_run_status = workflow_run_status or workflow_run.status

        if browser_state and effective_workflow_run_status == WorkflowRunStatus.completed:
            # Credential living-profile healthy-run write (cloud-only, seed==sink only, kill-switched).
            # Runs regardless of persist_browser_session so the zero-config credential path also banks.
            await app.AGENT_FUNCTION.bank_credential_profile_on_healthy_run(
                workflow_run=workflow_run,
                browser_state=browser_state,
            )

        # A debug (Studio) play must not write back through the legacy own-memory seam and overwrite
        # known-good memory. Gated on the browser-memory engine flag so flag-off orgs keep today's
        # behavior until broad rollout (staged semantics); both call sites route through here.
        if await app.AGENT_FUNCTION.should_skip_debug_profile_writeback(workflow_run):
            LOG.info(
                "Skipped legacy browser-session write-back for debug session (browser memory engine)",
                workflow_run_id=workflow_run.workflow_run_id,
            )
            return

        # A run that opted into a fresh browser (start_fresh_browser) boots without saved state and,
        # by contract, writes none of its own memory back. The engine era enforces this via a NULL sink;
        # this gate enforces the same for the flag-off legacy path. Credential banking above is
        # deliberately unaffected (a verified fresh sign-in still banks).
        if should_suppress_memory_write(workflow_run.start_fresh_browser):
            return

        # Engine era: the seed resolver already decided this run's sink (the picked plain profile or the
        # workflow's own auto-profile, or None for read-only/heal-only/fresh runs). Consume it, never
        # re-derive from the seed. Flag-off orgs fall through to the byte-for-byte legacy write-back.
        if await app.AGENT_FUNCTION.is_browser_memory_engine_enabled(workflow_run):
            await self._persist_run_sink_profile_if_needed(
                workflow_run=workflow_run,
                browser_state=browser_state,
                close_browser_on_completion=close_browser_on_completion,
                effective_workflow_run_status=effective_workflow_run_status,
            )
            await self._materialize_own_profile_pick_if_needed(
                workflow=workflow,
                workflow_run=workflow_run,
                effective_workflow_run_status=effective_workflow_run_status,
            )
            return

        if not (
            browser_state and workflow.persist_browser_session and browser_state.browser_artifacts.browser_session_dir
        ):
            return

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

    async def _persist_run_sink_profile_if_needed(
        self,
        *,
        workflow_run: WorkflowRun,
        browser_state: BrowserState | None,
        close_browser_on_completion: bool,
        effective_workflow_run_status: WorkflowRunStatus,
    ) -> None:
        """Engine-era workflow write-back: whole-dir persist the run's RESOLVED sink profile
        (workflow_run.browser_sink_profile_id — the picked plain profile with save on, or the workflow's
        own auto-profile). A None sink means the run has no workflow sink (read-only pick, a credential
        or foreign-workflow heal target, an API override, or fresh) and nothing is written here — the
        credential heal engine, when it applies, already ran above."""
        sink_profile_id = workflow_run.browser_sink_profile_id
        if not (sink_profile_id and browser_state and browser_state.browser_artifacts.browser_session_dir):
            return
        if browser_state.browser_artifacts._seed_load_failed:
            # The seed profile failed to launch (corruption/stale lock) and the run fell back to a blank
            # dir; its end-state is not this profile's, so writing it back would erase the saved archive.
            LOG.warning(
                "Skipped persisting sink browser profile — seed failed to load, run used a fallback dir",
                workflow_run_id=workflow_run.workflow_run_id,
                browser_profile_id=sink_profile_id,
            )
            return
        if (
            workflow_run.browser_profile_id
            and browser_state.browser_artifacts.applied_browser_profile_id != workflow_run.browser_profile_id
        ):
            # The stamped seed was never applied (e.g. a vendor-routed boot) — this directory does not
            # extend the sink's accumulated state, so writing it back would erase the saved archive.
            # Seedless accumulate rows (browser_profile_id None, sink set) still write by design.
            LOG.warning(
                "Skipped persisting sink browser profile — stamped seed profile was not applied to this browser",
                workflow_run_id=workflow_run.workflow_run_id,
                browser_profile_id=sink_profile_id,
                seed_browser_profile_id=workflow_run.browser_profile_id,
            )
            return
        if effective_workflow_run_status != WorkflowRunStatus.completed:
            LOG.info(
                "Skipped persisting browser sink profile for non-completed workflow run",
                workflow_run_id=workflow_run.workflow_run_id,
                workflow_run_status=effective_workflow_run_status,
            )
            return
        if not close_browser_on_completion:
            await persist_session_cookies(
                browser_state.browser_context,
                browser_state.browser_artifacts.browser_session_dir,
            )
        artifacts = browser_state.browser_artifacts
        if artifacts._seed_capture_failed and not artifacts._run_performed_fresh_login:
            # The seed fingerprint is UNKNOWN (capture errored) and this run did not itself perform a
            # verified login, so we can't tell whether a concurrent run moved the sink — never full-
            # overwrite (mirrors the write-time etag-read skip). Delta-merge only our own cookie changes
            # if a seed snapshot exists; otherwise skip the write rather than clobber a concurrent one.
            if await self._delta_merge_sink_profile(
                workflow_run=workflow_run, sink_profile_id=sink_profile_id, browser_state=browser_state
            ):
                return
            LOG.warning(
                "Sink profile write-back skipped — seed fingerprint uncaptured, cannot safely full-write",
                workflow_run_id=workflow_run.workflow_run_id,
                browser_profile_id=sink_profile_id,
            )
            return
        try:
            changed = await self._sink_profile_changed_under_run(workflow_run, sink_profile_id, browser_state)
        except Exception:
            # A storage error reading the current fingerprint means we can't tell whether a concurrent
            # run wrote the profile. Skip this run's write-back — no write beats a possibly-clobbering
            # full overwrite (delta-merge is unavailable when storage is flaky); the profile self-corrects
            # on the next healthy run.
            LOG.warning(
                "Sink profile write-back skipped — storage error reading the current fingerprint",
                workflow_run_id=workflow_run.workflow_run_id,
                browser_profile_id=sink_profile_id,
                exc_info=True,
            )
            return
        if changed:
            if await self._delta_merge_sink_profile(
                workflow_run=workflow_run, sink_profile_id=sink_profile_id, browser_state=browser_state
            ):
                return
            # The sink archive moved under this run (a concurrent writer) but the delta-merge was
            # unavailable (retrieve returned None on a swallowed transient error, or an empty sidecar).
            # A full write here would clobber that concurrent write, so skip — the profile self-corrects
            # on the next healthy run.
            LOG.warning(
                "Sink profile write-back skipped — sink moved under run and merge unavailable",
                workflow_run_id=workflow_run.workflow_run_id,
                browser_profile_id=sink_profile_id,
            )
            return
        try:
            # The seed archive's banked-cookie sidecar rides this directory into the write; refresh it
            # from the live jar, or drop it, so a later boot can't replay seed-era cookies over fresher.
            await refresh_banked_cookies(
                None if close_browser_on_completion else browser_state.browser_context,
                browser_state.browser_artifacts.browser_session_dir,
            )
        except Exception:
            LOG.warning(
                "Failed to refresh the banked-cookies sidecar before the sink write-back",
                workflow_run_id=workflow_run.workflow_run_id,
                browser_profile_id=sink_profile_id,
                exc_info=True,
            )
        await app.STORAGE.store_browser_profile(
            workflow_run.organization_id,
            profile_id=sink_profile_id,
            directory=browser_state.browser_artifacts.browser_session_dir,
        )
        LOG.info(
            "Persisted resolved sink browser profile for workflow run",
            workflow_run_id=workflow_run.workflow_run_id,
            browser_profile_id=sink_profile_id,
            browser_seed_source=workflow_run.browser_seed_source,
        )

    async def _sink_profile_changed_under_run(
        self, workflow_run: WorkflowRun, sink_profile_id: str, browser_state: BrowserState
    ) -> bool:
        """B2 freshness guard, part 1: did the sink profile's stored archive move since this run seeded it,
        while this run did NOT itself perform a verified login? True means a concurrent run wrote the
        profile (e.g. a fresher login) and our end-state may carry a stale session — so the caller must
        merge only our own cookie changes instead of clobbering the whole archive. A verified login this
        run, an unknown seed fingerprint, or an unchanged archive all keep the existing full write."""
        artifacts = browser_state.browser_artifacts
        if artifacts._run_performed_fresh_login:
            return False
        seed_etag = artifacts._seed_profile_etag
        if seed_etag is None:
            return False
        current_etag = await app.STORAGE.get_browser_profile_etag(workflow_run.organization_id, sink_profile_id)
        return current_etag is not None and current_etag != seed_etag

    async def _delta_merge_sink_profile(
        self, *, workflow_run: WorkflowRun, sink_profile_id: str, browser_state: BrowserState
    ) -> bool:
        """B2 freshness guard, part 2: contribute only the cookies THIS run changed (end-state vs our seed
        snapshot) into the profile's CURRENT stored state, preserving the concurrent write. Returns True
        when the merge handled the write-back (caller skips the full write); False to fall back to it."""
        seed_cookies = browser_state.browser_artifacts._seed_cookie_snapshot
        if seed_cookies is None:
            return False
        browser_context = browser_state.browser_context
        live_cookies: list[dict] | None = None
        try:
            if browser_context is not None:
                live_cookies = [dict(cookie) for cookie in await browser_context.cookies()]
        except Exception:
            live_cookies = None
        if live_cookies is not None:
            end_state_cookies = live_cookies
        else:
            # The context was already closed on completion (the common terminal path). close() persists
            # the end-state session cookies to the session-dir sidecar first, so read those rather than
            # skipping to a clobbering full write. Session cookies only — a run that changed a
            # persistent cookie without touching a session cookie still full-writes; tighten if observed.
            end_state_cookies = read_persisted_session_cookies(browser_state.browser_artifacts.browser_session_dir)
            if not end_state_cookies:
                return False
        delta = cookie_delta(end_state_cookies, seed_cookies)
        if not delta:
            # Our stale session changed nothing the concurrent write didn't already carry — leave its
            # archive intact rather than overwrite it with ours.
            LOG.info(
                "Sink profile moved under run with no own cookie changes; skipped write-back",
                workflow_run_id=workflow_run.workflow_run_id,
                browser_profile_id=sink_profile_id,
            )
            return True
        current_dir = await app.STORAGE.retrieve_browser_profile(workflow_run.organization_id, sink_profile_id)
        if not current_dir:
            return False
        try:
            # base_values = our seed → per-key three-way so a fresher concurrent write to the same
            # cookie key survives instead of being clobbered by our (stale) delta value.
            union_cookies_into_profile_dir(delta, current_dir, base_values=seed_cookie_values(seed_cookies))
            await app.STORAGE.store_browser_profile(
                workflow_run.organization_id,
                profile_id=sink_profile_id,
                directory=current_dir,
            )
        finally:
            # retrieve_browser_profile extracts a cookie-bearing archive into TEMP_PATH; drop it so a
            # conflicting merge doesn't leak a full profile onto worker disk. Guard against LocalStorage,
            # which returns the LIVE profile dir (outside TEMP_PATH) — never delete that.
            if is_temp_working_dir(current_dir):
                shutil.rmtree(current_dir, ignore_errors=True)
        LOG.info(
            "Delta-merged run cookie changes into concurrently-updated sink profile",
            workflow_run_id=workflow_run.workflow_run_id,
            browser_profile_id=sink_profile_id,
            delta_cookie_count=len(delta),
        )
        return True

    async def _materialize_own_profile_pick_if_needed(
        self,
        *,
        workflow: Workflow,
        workflow_run: WorkflowRun,
        effective_workflow_run_status: WorkflowRunStatus,
    ) -> None:
        """B3 virtual-then-real: on the FIRST successful engine run of a persist-ON, no-pick workflow
        whose sink is its own auto-profile, promote that profile to the workflow's real pick (set
        workflows.browser_profile_id) so the FE's virtual "<agent>'s profile" becomes a concrete pick. No
        backfill and no storage copy — the row and its own-memory storage already exist. Concurrent-safe
        and idempotent (atomic set-if-unset); best-effort — a failure never blocks completion."""
        if effective_workflow_run_status != WorkflowRunStatus.completed:
            return
        if workflow.browser_profile_id is not None or not workflow.persist_browser_session:
            return
        if workflow_run.browser_seed_source != BrowserSeedSource.own_memory:
            return
        sink_profile_id = workflow_run.browser_sink_profile_id
        if not sink_profile_id:
            return
        try:
            materialized = await app.DATABASE.workflows.link_workflow_browser_profile_if_unset(
                workflow_permanent_id=workflow.workflow_permanent_id,
                organization_id=workflow_run.organization_id,
                browser_profile_id=sink_profile_id,
            )
            if materialized:
                LOG.info(
                    "browser_memory.own_profile_materialized",
                    workflow_permanent_id=workflow.workflow_permanent_id,
                    browser_profile_id=sink_profile_id,
                )
        except Exception:
            LOG.warning(
                "Failed to materialize own-profile pick",
                workflow_permanent_id=workflow.workflow_permanent_id,
                exc_info=True,
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
        browser_persistence_status: WorkflowRunStatus | None = None,
        skip_browser_session_write_back: bool = False,
        schedule_credential_fallback_retry: bool = True,
    ) -> None:
        # Direct cleanup callers can enter with a terminal row. When browser cleanup is still
        # pending, install the tombstone before the first awaited terminal hook. A pre-finalization
        # cleanup result has already reached complete_stream_teardown and must not be tombstoned again.
        if browser_cleanup_result is None:
            mark_stream_closing(workflow_run.workflow_run_id)
        analytics.capture("skyvern-oss-agent-workflow-status", {"status": workflow_run.status})
        # Tear down passkey material in the worker, after the finally block, while the context is still alive.
        await app.AGENT_FUNCTION.on_workflow_run_terminal(
            workflow_run_id=workflow_run.workflow_run_id,
            organization_id=workflow_run.organization_id,
            status=workflow_run.status,
        )
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
        try:
            if browser_state:
                await self.persist_video_data(
                    browser_state, workflow, workflow_run, close_browser_on_completion=close_browser_on_completion
                )
                if tasks:
                    await self.persist_debug_artifacts(browser_state, tasks[-1], workflow, workflow_run)
                if (
                    not browser_cleanup_result.browser_session_write_back_attempted
                    and not skip_browser_session_write_back
                ):
                    await self._persist_workflow_browser_session_if_needed(
                        workflow=workflow,
                        workflow_run=workflow_run,
                        browser_state=browser_state,
                        close_browser_on_completion=close_browser_on_completion,
                        workflow_run_status=browser_persistence_status,
                    )

            await app.ARTIFACT_MANAGER.wait_for_upload_aiotasks(all_workflow_task_ids)

            try:
                async with asyncio.timeout(SAVE_DOWNLOADED_FILES_TIMEOUT):
                    context = skyvern_context.current()
                    finalization_run_id = resolve_run_download_id(context, fallback_run_id=workflow_run.workflow_run_id)
                    try:
                        await app.STORAGE.save_downloaded_files(
                            organization_id=workflow_run.organization_id,
                            run_id=finalization_run_id,
                        )
                    except DownloadSaveIncompleteError as exc:
                        # Session-artifact claiming below must still run for the files that saved.
                        LOG.warning(
                            "Some downloaded files were skipped during finalization save",
                            workflow_run_id=workflow_run.workflow_run_id,
                            skipped_file_count=len(exc.skipped_files),
                        )
                    # Reconcile session-scoped DOWNLOAD rows the watcher left unbound (see
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

            # Before the webhook, so a caller acting on the run's completion already sees the
            # attached files gone.
            await uploaded_file_service.delete_files_attached_to_run(run_id=workflow_run.workflow_run_id)

            if need_call_webhook:
                await self.execute_workflow_webhook(workflow_run, api_key)
        finally:
            # Run contexts hold parameters/secrets/outputs and are keyed per
            # run; without eviction they accumulate for the process lifetime.
            app.WORKFLOW_CONTEXT_MANAGER.remove_workflow_run_context(workflow_run.workflow_run_id)
            for child_workflow_run_id in child_workflow_run_ids:
                app.WORKFLOW_CONTEXT_MANAGER.remove_workflow_run_context(child_workflow_run_id)

            # When eligible, schedule the credential fallback retry only after this run's cleanup (artifact/video
            # persistence, browser/session write-back, webhook) has run, so the replacement run
            # cannot overlap it and race browser-profile/session writes. In the finally wrapping the
            # whole cleanup sequence — not after it — so an exception in any cleanup step can't skip
            # it; scheduling was removed from the status markers, which fired before cleanup and for
            # cascade/reaper failures that never reach cleanup. Finally-only failures explicitly
            # suppress it because replaying the workflow cannot repair post-run cleanup.
            if schedule_credential_fallback_retry:
                self._schedule_credential_fallback_retry(workflow_run)

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
            browser_seed_source=workflow_run.browser_seed_source,
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
                start_fresh_browser=bool(workflow_run.start_fresh_browser),
                reuse_browser_session=workflow_run.reuse_browser_session,
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
            headers=signed_data.headers,
        )
        return PreparedWorkflowWebhook(
            workflow_id=workflow_id,
            workflow_run_id=workflow_run.workflow_run_id,
            organization_id=workflow_run.organization_id,
            webhook_callback_url=workflow_run.webhook_callback_url,
            signed_payload=signed_data.signed_payload,
            headers=signed_data.headers,
        )

    async def deliver_prepared_workflow_webhook(self, webhook: PreparedWorkflowWebhook) -> None:
        LOG.info(
            "Sending webhook run status to webhook callback url",
            sampling=True,
            workflow_id=webhook.workflow_id,
            workflow_run_id=webhook.workflow_run_id,
            webhook_callback_url=webhook.webhook_callback_url,
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
                organization_id=webhook.organization_id,
                error=describe_delivery_error(e),
                exc_info=True,
            )
            try:
                await app.DATABASE.workflow_runs.update_workflow_run(
                    workflow_run_id=webhook.workflow_run_id,
                    webhook_failure_reason=f"Webhook delivery failed before receiving a response: {describe_delivery_error(e)}",
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
                registered_video_path = video_artifact.video_path
                if registered_video_path and not os.path.exists(registered_video_path):
                    # The local file is gone (path raced teardown) so get_video_artifacts could not
                    # refresh the cached bytes; writing them would clobber the newer streamed prefix.
                    LOG.info(
                        "Registered recording path missing; preserving latest stored prefix",
                        workflow_id=workflow.workflow_id,
                        workflow_run_id=workflow_run.workflow_run_id,
                        organization_id=workflow_run.organization_id,
                        video_artifact_id=video_artifact.video_artifact_id,
                        video_path=registered_video_path,
                    )
                    continue
                try:
                    # Only a true terminal finalize (workflow / task-v2 / code-block, browser closed ->
                    # recording complete) may supersede queued prefixes. On an intermediate persist the
                    # recording is still growing and its prefixes are still legitimately streaming, so
                    # sealing the live key would kill per-step visibility (see manager.update_artifact_data).
                    if video_artifact.video_file_extension:
                        upload_key = await app.ARTIFACT_MANAGER.update_artifact_data(
                            artifact_id=video_artifact.video_artifact_id,
                            organization_id=workflow_run.organization_id,
                            data=video_artifact.video_data,
                            file_extension=video_artifact.video_file_extension,
                            supersede_queued_prefixes=close_browser_on_completion,
                        )
                    else:
                        upload_key = await app.ARTIFACT_MANAGER.update_artifact_data(
                            artifact_id=video_artifact.video_artifact_id,
                            organization_id=workflow_run.organization_id,
                            data=video_artifact.video_data,
                            supersede_queued_prefixes=close_browser_on_completion,
                        )
                except Exception:
                    LOG.warning(
                        "Failed to persist workflow video artifact",
                        workflow_id=workflow.workflow_id,
                        workflow_run_id=workflow_run.workflow_run_id,
                        organization_id=workflow_run.organization_id,
                        video_artifact_id=video_artifact.video_artifact_id,
                        exc_info=True,
                    )
                    continue
                if upload_key:
                    upload_keys.add(upload_key)
                continue

            video_path = video_artifact.video_path
            if not video_artifact.video_data and (not video_path or not os.path.exists(video_path)):
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
                    "Cannot persist recording: no latest step for workflow run",
                    workflow_run_id=workflow_run.workflow_run_id,
                    video_path=video_path,
                )
                continue
            if video_artifact.video_data:
                if video_artifact.video_file_extension:
                    artifact_id = await app.ARTIFACT_MANAGER.create_artifact(
                        step=last_step,
                        artifact_type=ArtifactType.RECORDING,
                        data=video_artifact.video_data,
                        file_extension=video_artifact.video_file_extension,
                    )
                else:
                    artifact_id = await app.ARTIFACT_MANAGER.create_artifact(
                        step=last_step,
                        artifact_type=ArtifactType.RECORDING,
                        data=video_artifact.video_data,
                    )
            else:
                if video_artifact.video_file_extension:
                    artifact_id = await app.ARTIFACT_MANAGER.create_artifact(
                        step=last_step,
                        artifact_type=ArtifactType.RECORDING,
                        path=video_path,
                        file_extension=video_artifact.video_file_extension,
                    )
                else:
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
        if app.WORKFLOW_CONTEXT_MANAGER.secret_redaction_enabled_for_run(workflow_run.workflow_run_id):
            secret_values = app.WORKFLOW_CONTEXT_MANAGER.get_secret_values_for_run(workflow_run.workflow_run_id)
            har_data = await asyncio.to_thread(redact_har_bytes, har_data, secret_values)
        else:
            runtime_secret_values = app.WORKFLOW_CONTEXT_MANAGER.runtime_secret_values_for_artifacts()
            if runtime_secret_values:
                har_data = await asyncio.to_thread(redact_har_bytes, har_data, runtime_secret_values)
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
        if app.WORKFLOW_CONTEXT_MANAGER.secret_redaction_enabled_for_run(workflow_run.workflow_run_id):
            secret_values = app.WORKFLOW_CONTEXT_MANAGER.get_secret_values_for_run(workflow_run.workflow_run_id)
            browser_log = await asyncio.to_thread(redact_console_log_bytes, browser_log, secret_values)
        else:
            runtime_secret_values = app.WORKFLOW_CONTEXT_MANAGER.runtime_secret_values_for_artifacts()
            if runtime_secret_values:
                browser_log = await asyncio.to_thread(redact_console_log_bytes, browser_log, runtime_secret_values)
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

        await self._persist_debug_artifacts_bundled(browser_state, last_step, workflow, workflow_run)

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
        redaction_enabled = app.WORKFLOW_CONTEXT_MANAGER.secret_redaction_enabled_for_run(workflow_run.workflow_run_id)
        secret_values = (
            app.WORKFLOW_CONTEXT_MANAGER.get_secret_values_for_run(workflow_run.workflow_run_id)
            if redaction_enabled
            else app.WORKFLOW_CONTEXT_MANAGER.runtime_secret_values_for_artifacts()
        )
        # Opted-in runs always redact (even an empty full set); opted-out runs redact only when a
        # runtime secret was actually registered, so the no-secrets case skips the redact call.
        should_redact = redaction_enabled or bool(secret_values)
        if should_redact:
            browser_log = await asyncio.to_thread(redact_console_log_bytes, browser_log, secret_values)
        LOG.debug("Persisting browser log (bundled)", browser_log_size=len(browser_log))
        if browser_log:
            task_archive_entries["browser_console.log"] = (ArtifactType.BROWSER_CONSOLE_LOG, browser_log)

        har_data = await app.BROWSER_MANAGER.get_har_data(
            workflow_id=workflow.workflow_id,
            workflow_run_id=workflow_run.workflow_run_id,
            browser_state=browser_state,
        )
        if should_redact:
            har_data = await asyncio.to_thread(redact_har_bytes, har_data, secret_values)
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
        caller maps post-run output values against it. The version is marked with the copilot_test creator
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
        cdp_connect_headers = runtime_workflow.cdp_connect_headers
        if cdp_connect_headers:
            source = await app.DATABASE.workflows.get_workflow(
                workflow_id=runtime_workflow.workflow_id,
                organization_id=organization_id,
            )
            cdp_connect_headers = merge_masked_headers(
                cdp_connect_headers, source.cdp_connect_headers if source is not None else None
            )
        placeholder = await self.create_workflow(
            title=runtime_workflow.title,
            workflow_definition=WorkflowDefinition(parameters=[], blocks=[]),
            organization_id=organization_id,
            workflow_permanent_id=runtime_workflow.workflow_permanent_id,
            version=next_version,
            status=WorkflowStatus.auto_generated,
            created_by=COPILOT_TEST_WORKFLOW_CREATOR,
            edited_by="copilot",
            description=runtime_workflow.description,
            proxy_location=runtime_workflow.proxy_location,
            webhook_callback_url=runtime_workflow.webhook_callback_url,
            totp_verification_url=runtime_workflow.totp_verification_url,
            totp_identifier=runtime_workflow.totp_identifier,
            persist_browser_session=runtime_workflow.persist_browser_session,
            reuse_browser_session=runtime_workflow.reuse_browser_session,
            mask_secrets=runtime_workflow.mask_secrets,
            pin_saved_session_ip=runtime_workflow.pin_saved_session_ip,
            browser_profile_id=runtime_workflow.browser_profile_id,
            browser_profile_key=runtime_workflow.browser_profile_key,
            model=runtime_workflow.model,
            max_screenshot_scrolling_times=runtime_workflow.max_screenshot_scrolls,
            max_elapsed_time_minutes=runtime_workflow.max_elapsed_time_minutes,
            extra_http_headers=runtime_workflow.extra_http_headers,
            cdp_connect_headers=cdp_connect_headers,
            run_with=runtime_workflow.run_with,
            ai_fallback=runtime_workflow.ai_fallback,
            cache_key=runtime_workflow.cache_key,
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

    async def resolve_workflow_creation_title(
        self,
        organization_id: str,
        request: WorkflowCreateYAMLRequest,
    ) -> str:
        title = request.title
        if title in DEFAULT_WORKFLOW_TITLES and request.workflow_definition.blocks:
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
        return title

    async def _create_initial_workflow_from_request(
        self,
        *,
        organization_id: str,
        request: WorkflowCreateYAMLRequest,
        title: str,
        workflow_definition: WorkflowDefinition,
        cdp_connect_headers: dict[str, str] | None,
        created_by: str | None,
        edited_by: str | None,
        workflow_permanent_id: str | None = None,
        workflow_id: str | None = None,
        encrypt_secrets: bool = True,
    ) -> Workflow:
        return await self.create_workflow(
            title=title,
            workflow_definition=workflow_definition,
            description=request.description,
            organization_id=organization_id,
            proxy_location=request.proxy_location,
            webhook_callback_url=request.webhook_callback_url,
            totp_verification_url=request.totp_verification_url,
            totp_identifier=request.totp_identifier,
            persist_browser_session=request.persist_browser_session,
            reuse_browser_session=request.reuse_browser_session,
            mask_secrets=request.mask_secrets if request.mask_secrets is not None else False,
            pin_saved_session_ip=request.pin_saved_session_ip,
            browser_profile_id=request.browser_profile_id,
            browser_profile_key=request.browser_profile_key,
            model=request.model,
            max_screenshot_scrolling_times=request.max_screenshot_scrolls,
            max_elapsed_time_minutes=request.max_elapsed_time_minutes,
            extra_http_headers=request.extra_http_headers,
            cdp_connect_headers=cdp_connect_headers,
            workflow_permanent_id=workflow_permanent_id,
            version=1,
            is_saved_task=request.is_saved_task,
            status=request.status,
            run_with=request.run_with,
            cache_key=request.cache_key,
            ai_fallback=request.ai_fallback,
            run_sequentially=request.run_sequentially,
            sequential_key=request.sequential_key,
            folder_id=request.folder_id,
            adaptive_caching=request.adaptive_caching,
            enable_self_healing=request.enable_self_healing if request.enable_self_healing is not None else False,
            code_version=request.code_version,
            generate_script_on_terminal=request.generate_script_on_terminal,
            created_by=created_by,
            edited_by=edited_by,
            workflow_id=workflow_id,
            encrypt_secrets=encrypt_secrets,
        )

    async def _create_idempotent_workflow_from_request(
        self,
        *,
        organization: Organization,
        request: WorkflowCreateYAMLRequest,
        workflow_permanent_id: str,
        title: str,
        delete_script: bool,
        created_by: str | None,
        edited_by: str | None,
    ) -> Workflow:
        organization_id = organization.organization_id
        await self._validate_and_normalize_credential_rotation_parameters(
            request.workflow_definition.parameters,
            organization,
        )
        workflow_id = generate_workflow_id()
        workflow_definition = convert_workflow_definition(
            workflow_definition_yaml=request.workflow_definition,
            workflow_id=workflow_id,
        )
        workflow_definition.validate()
        self.validate_workflow_block_graph(workflow_definition)
        self._validate_payload_templates(workflow_definition)
        await encrypt_workflow_definition_secrets(workflow_definition, organization_id)
        cdp_connect_headers = merge_masked_headers(request.cdp_connect_headers, None)

        async with app.DATABASE.workflows.acquire_workflow_creation_lock(workflow_permanent_id):
            try:
                return await self.get_workflow_by_permanent_id(
                    workflow_permanent_id=workflow_permanent_id,
                    organization_id=organization_id,
                    version=1,
                    filter_deleted=False,
                )
            except WorkflowNotFound:
                pass

            created_workflow = await self._create_initial_workflow_from_request(
                organization_id=organization_id,
                request=request,
                title=title,
                workflow_definition=workflow_definition,
                cdp_connect_headers=cdp_connect_headers,
                workflow_permanent_id=workflow_permanent_id,
                created_by=created_by,
                edited_by=edited_by,
                workflow_id=workflow_id,
                encrypt_secrets=False,
            )
            await app.DATABASE.workflow_params.save_workflow_definition_parameters(workflow_definition.parameters)

        self.schedule_workflow_saved_hook(
            organization_id=organization_id,
            edited_by=edited_by,
            workflow_permanent_id=workflow_permanent_id,
        )
        await self.maybe_delete_cached_code(
            created_workflow,
            workflow_definition=workflow_definition,
            organization_id=organization_id,
            delete_script=delete_script,
        )
        return created_workflow

    async def create_workflow_from_request(
        self,
        organization: Organization,
        request: WorkflowCreateYAMLRequest,
        workflow_permanent_id: str | None = None,
        delete_script: bool = True,
        created_by: str | None = None,
        edited_by: str | None = None,
        new_workflow_permanent_id: str | None = None,
        resolved_title: str | None = None,
    ) -> Workflow:
        organization_id = organization.organization_id
        title = resolved_title
        if title is None:
            title = await self.resolve_workflow_creation_title(organization_id, request)

        LOG.info(
            "Creating workflow from request",
            organization_id=organization_id,
            title=title,
        )
        if new_workflow_permanent_id:
            return await self._create_idempotent_workflow_from_request(
                organization=organization,
                request=request,
                workflow_permanent_id=new_workflow_permanent_id,
                title=title,
                delete_script=delete_script,
                created_by=created_by,
                edited_by=edited_by,
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
            existing_version = existing_latest_workflow.version
            if existing_latest_workflow.created_by == COPILOT_TEST_WORKFLOW_CREATOR:
                # Test versions reserve numbers but never supply settings for a normal save.
                existing_latest_workflow = await self.get_workflow_by_permanent_id(
                    workflow_permanent_id=workflow_permanent_id,
                    organization_id=organization_id,
                )
        else:
            existing_latest_workflow = None

        if request.webhook_callback_url and (
            existing_latest_workflow is None
            or request.webhook_callback_url != existing_latest_workflow.webhook_callback_url
        ):
            request.webhook_callback_url = validate_webhook_url(request.webhook_callback_url)

        try:
            if existing_latest_workflow:
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
                    reuse_browser_session=request.reuse_browser_session,
                    mask_secrets=request.mask_secrets
                    if request.mask_secrets is not None
                    else getattr(existing_latest_workflow, "mask_secrets", False),
                    pin_saved_session_ip=request.pin_saved_session_ip
                    if "pin_saved_session_ip" in request.model_fields_set
                    else existing_latest_workflow.pin_saved_session_ip,
                    browser_profile_id=request.browser_profile_id,
                    # Inherit the configured seed profile when a client omits the field (e.g. a schema
                    # predating it); explicit null still clears it. Matches pin_saved_session_ip above.
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
                    created_by=created_by if created_by is not None else existing_latest_workflow.created_by,
                    edited_by=edited_by,
                )
            else:
                # No existing workflow to inherit from; merge_masked_headers drops
                # any keys whose value is the mask sentinel so we never persist the
                # literal "***" placeholder from a misbehaving client.
                new_cdp_connect_headers = merge_masked_headers(request.cdp_connect_headers, None)
                # NOTE: it's only potential, as it may be immediately deleted!
                potential_workflow = await self._create_initial_workflow_from_request(
                    organization_id=organization_id,
                    request=request,
                    title=title,
                    workflow_definition=WorkflowDefinition(parameters=[], blocks=[]),
                    cdp_connect_headers=new_cdp_connect_headers,
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
        cap_output_values: bool = False,
    ) -> list[WorkflowRunTimeline]:
        """
        build the tree structure of the workflow run timeline
        """
        workflow_run_blocks = await app.DATABASE.observer.get_workflow_run_blocks(
            workflow_run_id=workflow_run_id,
            organization_id=organization_id,
        )
        if cap_output_values:
            for block in workflow_run_blocks:
                # The jsonb write guard covers only `output`, so every other unbounded jsonb
                # field on the block reaches this response uncapped.
                for block_field in UNBOUNDED_BLOCK_JSON_FIELDS:
                    setattr(
                        block,
                        block_field,
                        truncate_oversized_response_value(
                            getattr(block, block_field),
                            workflow_run_id=workflow_run_id,
                            workflow_run_block_id=block.workflow_run_block_id,
                            field=block_field,
                        ),
                    )
                for text_field in UNBOUNDED_BLOCK_TEXT_FIELDS:
                    setattr(block, text_field, truncate_oversized_response_text(getattr(block, text_field)))
                for list_field in UNBOUNDED_BLOCK_TEXT_LIST_FIELDS:
                    setattr(
                        block,
                        list_field,
                        _capped_text_list(
                            getattr(block, list_field),
                            workflow_run_id=workflow_run_id,
                            field=list_field,
                        ),
                    )
                block.loop_values = _capped_loop_values(
                    block.loop_values,
                    workflow_run_id=workflow_run_id,
                    workflow_run_block_id=block.workflow_run_block_id,
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
            # Actions ride the block they hydrate, and a completion/extraction action can
            # carry a multi-megabyte response or output of its own.
            if cap_output_values:
                _cap_action_payloads(action, workflow_run_id=workflow_run_id)
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
                    if block.label and is_block_type_cacheable(block)
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
                if block.label and is_block_type_cacheable(block)
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

        # The published lookup above cannot see the pending script this run's
        # per-block mints created. Reuse it (regenerate + promote) instead of
        # minting a duplicate script with identical content (SKY-13659). The
        # regeneration also closes the race with the fire-and-forget last
        # per-block mint, which may still be in flight.
        pending_script = None
        pending_workflow_script = await app.DATABASE.scripts.get_workflow_script(
            organization_id=workflow.organization_id,
            workflow_permanent_id=workflow.workflow_permanent_id,
            workflow_run_id=workflow_run.workflow_run_id,
            statuses=[ScriptStatus.pending],
        )
        if pending_workflow_script:
            pending_script = await app.DATABASE.scripts.get_script(
                script_id=pending_workflow_script.script_id,
                organization_id=workflow.organization_id,
            )

        if pending_script:
            # Mint a NEW revision under the pending script_id rather than writing
            # into the pending revision itself: script_files is unique on
            # (script_revision_id, file_path) and create_script_file conflict-noops,
            # so regenerating in place would keep the stale pending main.py — which
            # the still-in-flight per-block mint can overwrite. A fresh revision
            # persists the final source and wins the cache lookup (latest version).
            version_stats = await app.DATABASE.scripts.get_script_version_stats(
                organization_id=workflow.organization_id,
                script_ids=[pending_script.script_id],
            )
            latest_version, _ = version_stats.get(pending_script.script_id, (pending_script.version, 0))
            created_script = await app.DATABASE.scripts.create_script(
                organization_id=workflow.organization_id,
                run_id=workflow_run.workflow_run_id,
                script_id=pending_script.script_id,
                version=latest_version + 1,
            )
            LOG.info(
                "Reusing run's pending script id for final mint instead of creating a new script",
                workflow_permanent_id=workflow.workflow_permanent_id,
                workflow_run_id=workflow_run.workflow_run_id,
                script_id=created_script.script_id,
                pending_revision_id=pending_script.script_revision_id,
                new_revision_id=created_script.script_revision_id,
                new_version=created_script.version,
                cache_key_value=rendered_cache_key_value,
            )
        else:
            created_script = await app.DATABASE.scripts.create_script(
                organization_id=workflow.organization_id,
                run_id=workflow_run.workflow_run_id,
            )

        await workflow_script_service.generate_workflow_script(
            workflow_run=workflow_run,
            workflow=workflow,
            script=created_script,
            rendered_cache_key_value=rendered_cache_key_value,
            cached_script=pending_script,
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

    async def bind_browser_action_policy(
        self,
        workflow: Workflow,
        *,
        run_with: str | None,
    ) -> BrowserActionPolicy | None:
        """Bind the exact resolved workflow version's policy to the current run.

        Every path that executes a workflow version calls this before the run's browser can exist.
        Raises BrowserActionPolicyNotEnforceable when the version is enrolled but configured in a
        way the action-level firewall cannot cover.
        """
        policy = await app.DATABASE.workflows.get_browser_action_policy(
            workflow_id=workflow.workflow_id,
            organization_id=workflow.organization_id,
        )
        bind_policy_to_context(policy, workflow, run_with=run_with)
        return policy

    async def replace_browser_action_policy(
        self,
        *,
        workflow_permanent_id: str,
        organization_id: str,
        allowed_origin_urls: list[str] | None,
    ) -> BrowserActionPolicy | None:
        """Control-plane enrollment: replace (or, with None, clear) the latest version's policy.

        The only writer of policy. Enrolling a version the firewall cannot cover is refused here so
        an operator learns at enrollment time rather than when the next run fails.
        """
        workflow = await self.get_workflow_by_permanent_id(
            workflow_permanent_id=workflow_permanent_id,
            organization_id=organization_id,
        )
        if workflow is None:
            raise WorkflowNotFound(workflow_permanent_id=workflow_permanent_id)
        if allowed_origin_urls is not None:
            reasons = rejection_reasons(workflow, run_with=None)
            if reasons:
                raise BrowserActionPolicyNotEnforceable(reasons)

        policy = await app.DATABASE.workflows.set_browser_action_policy(
            workflow_id=workflow.workflow_id,
            organization_id=organization_id,
            allowed_origin_urls=allowed_origin_urls,
        )
        LOG.info(
            "Browser action policy replaced",
            workflow_permanent_id=workflow_permanent_id,
            workflow_id=workflow.workflow_id,
            workflow_version=workflow.version,
            organization_id=organization_id,
            policy_version=policy.version if policy else None,
            allowed_origin_count=len(policy.allowed_origins) if policy else 0,
        )
        return policy

    async def should_run_script(
        self,
        workflow: Workflow,
        workflow_run: WorkflowRun,
    ) -> bool:
        """Whether this run should attempt cached-script execution.

        Priority: run-level run_with > workflow-level run_with. Intended-code
        runs are then passed through the app-level code-mode gate. Runs not
        already intended as code may be upgraded by a flag-gated rollout;
        an upgraded run has no matching script for a non-eligible workflow
        and so degrades cleanly to agent per block.
        """
        context = skyvern_context.current()
        if context and context.browser_action_policy is not None:
            # An enrolled run must not reach script execution: generated code drives the browser
            # without passing an action sink, so the flag-gated upgrade below would silently
            # produce a run the firewall cannot see.
            return False
        if workflow_run.run_with is not None:
            intended_code = workflow_run.run_with == "code"
        else:
            intended_code = workflow.run_with == "code"
        # A fallback retry has already made a deliberate execution-mode choice; the rollout must not
        # re-upgrade it to code. Gate on the retry marker rather than `run_with is None`: normal runs
        # inherit run_with="agent" from the workflow, so keying on None would disable the rollout for
        # every ordinary run.
        if not intended_code and workflow_run.retried_from_workflow_run_id is None:
            intended_code = await app.AGENT_FUNCTION.should_upgrade_to_code_mode(
                workflow=workflow,
                workflow_run=workflow_run,
            )
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
