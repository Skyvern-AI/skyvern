from __future__ import annotations

import abc
import ast
import asyncio
import builtins
import codecs
import copy
import csv
import hashlib
import html
import inspect
import io
import json
import keyword
import os
import re
import shutil
import smtplib
import socket
import ssl
import textwrap
import unicodedata
import uuid
import zipfile
from collections import defaultdict, deque
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import UTC, date, datetime, time
from email.message import EmailMessage
from enum import StrEnum
from functools import partial
from pathlib import Path, PurePosixPath
from time import monotonic
from types import SimpleNamespace
from typing import TYPE_CHECKING, Annotated, Any, Awaitable, Callable, ClassVar, Literal, TypeVar, Union, cast
from urllib.parse import quote, urlparse

import aiofiles
import aiohttp
import docx
import filetype
import pandas as pd
import structlog
from charset_normalizer import from_bytes
from email_validator import EmailNotValidError, validate_email
from jinja2 import StrictUndefined, TemplateSyntaxError, nodes
from jinja2.sandbox import SandboxedEnvironment
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError
from opentelemetry import trace as otel_trace
from playwright.async_api import Page
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from pydantic import BaseModel, Field, PrivateAttr, field_validator, model_validator
from sqlalchemy.exc import InterfaceError, OperationalError

from skyvern.config import settings
from skyvern.constants import (
    AZURE_BLOB_STORAGE_MAX_UPLOAD_FILE_COUNT,
    BROWSER_DOWNLOADING_SUFFIX,
    FILE_PARSE_STEP_TIMEOUT_SECONDS,
    GET_DOWNLOADED_FILES_TIMEOUT,
    MAX_FILE_PARSE_INPUT_TOKENS,
    MAX_PDF_OCR_PAGES,
    MAX_UPLOAD_FILE_COUNT,
    NAVIGATION_MAX_RETRY_TIME,
    PDF_OCR_PAGE_CONCURRENCY,
    SAVE_DOWNLOADED_FILES_TIMEOUT,
)
from skyvern.errors.errors import UserDefinedError
from skyvern.exceptions import (
    AzureConfigurationError,
    BlockedHost,
    BlockedNavigationDestination,
    BranchEvaluationContextTooLargeError,
    CodeBlockRunnerSelectionError,
    ConditionalBranchEvaluationError,
    ContextParameterValueNotFound,
    DownloadFileMaxSizeExceeded,
    DownloadSaveIncompleteError,
    FailedToGetTOTPVerificationCode,
    FailedToNavigateToUrl,
    MalformedBranchEvaluationError,
    MissingBrowserState,
    MissingBrowserStatePage,
    MissingStarterUrl,
    NoTOTPVerificationCodeFound,
    PDFParsingError,
    ScriptTerminationException,
    SkyvernException,
    SyncTriggeredSequentialCredentialUnsupported,
    TaskNotFound,
    UnexpectedTaskStatus,
    UnresolvableHost,
    get_user_facing_exception_message,
)
from skyvern.forge import app
from skyvern.forge.failure_classifier import classify_from_failure_reason
from skyvern.forge.prompts import prompt_engine
from skyvern.forge.sdk.api import email
from skyvern.forge.sdk.api.aws import AsyncAWSClient
from skyvern.forge.sdk.api.files import (
    calculate_sha256_for_file,
    classify_download_visibility,
    download_dir_path_for_run,
    download_file,
    get_download_dir,
    get_path_for_workflow_download_directory,
    is_remote_url,
    observe_download_dir,
    parse_uri_to_path,
    resolve_local_or_download_file,
    resolve_run_download_id,
    validate_local_file_path,
    wait_for_download_finished,
)
from skyvern.forge.sdk.api.llm.api_handler import LLMAPIHandler
from skyvern.forge.sdk.api.llm.api_handler_factory import (
    LLMAPIHandlerFactory,
    get_org_aware_primary_llm_api_handler,
    get_org_aware_secondary_llm_api_handler,
)
from skyvern.forge.sdk.api.llm.custom_llm_registry import is_custom_llm_model_name
from skyvern.forge.sdk.api.llm.exceptions import (
    EmptyLLMResponseError,
    InvalidLLMResponseFormat,
    InvalidLLMResponseType,
    LLMProviderErrorRetryableTask,
)
from skyvern.forge.sdk.api.llm.schema_validator import validate_schema
from skyvern.forge.sdk.artifact.models import ArtifactType
from skyvern.forge.sdk.copilot.block_goal_wrapping import compose_mini_goal
from skyvern.forge.sdk.copilot.reached_download_target import (
    REGISTERED_DOWNLOAD_OUTPUT_KEYS,
    block_output_has_registered_download,
    code_is_download_intent,
)
from skyvern.forge.sdk.copilot.runtime import _browser_context_is_attachable
from skyvern.forge.sdk.copilot.self_heal_recovery import SelfHealRecoveryResult, run_self_heal_recovery
from skyvern.forge.sdk.copilot.turn_origin import HealAdoptionFailed, TurnOrigin
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.aiohttp_helper import aiohttp_request
from skyvern.forge.sdk.core.hashing import diagnostic_fingerprint
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.db.datetime_utils import naive_utc_now
from skyvern.forge.sdk.db.enums import TaskType
from skyvern.forge.sdk.db.exceptions import NotFoundError
from skyvern.forge.sdk.db.id import generate_action_id
from skyvern.forge.sdk.experimentation.llm_prompt_config import get_llm_handler_for_prompt_type
from skyvern.forge.sdk.experimentation.workflow_block_engine import workflow_block_engine_override
from skyvern.forge.sdk.models import Step, StepStatus
from skyvern.forge.sdk.schemas.files import FileInfo
from skyvern.forge.sdk.schemas.task_v2 import TaskV2Status
from skyvern.forge.sdk.schemas.tasks import Task, TaskOutput, TaskStatus
from skyvern.forge.sdk.schemas.totp_codes import OTPType
from skyvern.forge.sdk.services import google_drive_service, google_oauth_service, sftp_service
from skyvern.forge.sdk.services.bitwarden import BitwardenConstants
from skyvern.forge.sdk.services.credentials import AzureVaultConstants, OnePasswordConstants, generate_totp_code
from skyvern.forge.sdk.settings_manager import SettingsManager
from skyvern.forge.sdk.trace import traced
from skyvern.forge.sdk.utils.pdf_parser import extract_pdf_file, render_pdf_pages_as_images, validate_pdf_file
from skyvern.forge.sdk.utils.sanitization import sanitize_postgres_text
from skyvern.forge.sdk.workflow import web_search
from skyvern.forge.sdk.workflow.code_block_safety import BLOCKED_ATTRS as CODE_BLOCK_BLOCKED_ATTRS
from skyvern.forge.sdk.workflow.code_block_safety import is_safe_code as _shared_is_safe_code
from skyvern.forge.sdk.workflow.constants import OUTPUT_PARAMETER_MAX_VALUE_BYTES
from skyvern.forge.sdk.workflow.context_manager import (
    NON_SECRET_CREDENTIAL_FIELDS,
    BlockMetadata,
    WorkflowRunContext,
)
from skyvern.forge.sdk.workflow.exceptions import (
    CustomizedCodeException,
    CustomSMTPAuthenticationFailed,
    CustomSMTPConnectionFailed,
    FailedToFormatJinjaStyleParameter,
    FileParseTimeout,
    InsecureCodeDetected,
    InvalidEmailClientConfiguration,
    InvalidFileType,
    InvalidWorkflowDefinition,
    MissingJinjaVariables,
    NoIterableValueFound,
    NoValidEmailRecipient,
    PayloadTemplateRenderError,
    PayloadTemplateSyntaxError,
)
from skyvern.forge.sdk.workflow.loop_download_filter import (
    DOWNLOADED_FILE_SIGS_KEY,
    filter_downloaded_files_for_current_iteration,
    to_downloaded_file_signature,
)
from skyvern.forge.sdk.workflow.models._jinja import (
    _JSON_TYPE_MARKER,
    _json_type_filter,
    jinja_json_finalize_required_binding_env,
    jinja_json_finalize_strict_env,
    mask_jinja_in_python_comments,
    render_templates_in_json_value,
    restore_jinja_masked_comments,
)
from skyvern.forge.sdk.workflow.models.code_block_recorder import (
    CODE_BLOCK_FILENAME,
    RECORDED_FAILURE_RESPONSE_MAX_CHARS,
    RecordingPage,
    json_safe_recorder_output,
    user_code_line_from_exception,
)
from skyvern.forge.sdk.workflow.models.code_block_recording import CodeBlockActionRecording
from skyvern.forge.sdk.workflow.models.credential_release import (
    CodeBlockCredentialReleaseError,
    CredentialReleaseGuard,
)
from skyvern.forge.sdk.workflow.models.parameter import (
    PARAMETER_TYPE,
    UNUSED_CUSTOM_SMTP_PLACEHOLDER_AWS_KEY,
    AWSSecretParameter,
    ContextParameter,
    OutputParameter,
    ParameterType,
    WorkflowParameter,
    WorkflowParameterType,
)
from skyvern.forge.sdk.workflow.secret_encryption import (
    SENSITIVE_DESTINATION_FIELDS,
    SENSITIVE_SEND_EMAIL_FIELDS,
    decrypt_secret_field_value,
    is_encrypted_secret,
    is_full_template_reference,
)
from skyvern.schemas.browser_session_close import BrowserSessionCloseReason
from skyvern.schemas.runs import RunEngine
from skyvern.schemas.self_heal import HealClassification, HealSkipReason, HealStatus, OutputObligation
from skyvern.schemas.workflows import (
    ERROR_CODE_MAPPING_MAX_ENTRIES,
    ERROR_CODE_MAPPING_MAX_UTF8_BYTES,
    ERROR_CODE_MAX_LENGTH,
    ERROR_CODE_REASONING_MAX_LENGTH,
    AIFallbackMode,
    BlockResult,
    BlockStatus,
    BlockType,
    FileDownloadTarget,
    FileStorageType,
    FileType,
    FileUploadDestination,
    _direct_code_block_error_code_raises,
    _normalize_optional_endpoint_url,
    _validate_code_block_error_code_calls,
    error_code_key_error,
    error_code_mapping_entry_error,
    normalize_error_code_description,
)
from skyvern.services import otp_email, otp_service, planner_levers
from skyvern.services.error_detection_service import detect_user_defined_errors_for_task
from skyvern.services.self_heal_cap import check_and_increment_self_heal_cap
from skyvern.utils.contained_effects import contained_effect
from skyvern.utils.parquet_export import ParquetExportError, export_parquet_records
from skyvern.utils.prompt_engine import PROMPT_HARD_CEILING_TOKENS
from skyvern.utils.secret_redaction import (
    MIN_NUMERIC_SECRET_LENGTH,
    MIN_SECRET_LENGTH,
    redact_secrets_from_text,
)
from skyvern.utils.strings import generate_random_string
from skyvern.utils.templating import get_available_keys, get_missing_variables
from skyvern.utils.token_counter import count_tokens, decode_tokens, encode_tokens
from skyvern.utils.url_validators import (
    prepend_scheme_and_validate_url,
    resolve_fetch_host_ips,
)
from skyvern.webeye.actions.action_types import ActionType
from skyvern.webeye.actions.actions import Action, ActionStatus
from skyvern.webeye.browser_artifacts import DownloadBinding
from skyvern.webeye.browser_driver_errors import is_driver_error
from skyvern.webeye.browser_factory import rebind_download_dir
from skyvern.webeye.browser_object_predicates import is_page_like
from skyvern.webeye.browser_state import BrowserState, get_browser_state_diagnostic
from skyvern.webeye.cdp_download_interceptor import normalize_download_filename, settle_browser_downloads_for_context
from skyvern.webeye.navigation import (
    default_navigation_settle,
    is_egress_attributable_navigation_error,
    navigate_with_retry,
    redact_url_secrets,
)
from skyvern.webeye.playwright_input import playwright_input_defaults_for_page
from skyvern.webeye.real_browser_state import RealBrowserState
from skyvern.webeye.utils.captcha_solver import CaptchaChallengeUnsolvedError, solve_challenge_ladder
from skyvern.webeye.utils.page import SkyvernFrame

if TYPE_CHECKING:
    from skyvern.forge.agent_functions import CodeBlockEngineFailure
    from skyvern.forge.sdk.workflow.models.workflow import WorkflowRun
    from skyvern.webeye.browser_engine import BrowserEngineSelection
    from skyvern.webeye.cdp_download_interceptor import CDPDownloadInterceptor

LOG = structlog.get_logger()


# SKY-8818: observability threshold for under-configured file_download blocks.
# Warning fires when `max_steps_per_run` is set below this value. Not a behavior change —
# purely a Datadog-searchable signal (`log_code=file_download_low_max_steps`).
MAX_STEPS_DOWNLOAD_WARNING_THRESHOLD = 5


def warn_if_file_download_max_steps_low(
    block: BaseTaskBlock,
    workflow_run_id: str | None = None,
) -> None:
    """Emit a structured warning if a file_download block has an under-configured step budget.

    A `max_steps_per_run` of None means "use org default", which is not a misconfiguration.
    Only configured values strictly below MAX_STEPS_DOWNLOAD_WARNING_THRESHOLD warn.
    """
    if block.block_type != BlockType.FILE_DOWNLOAD:
        return
    configured = block.max_steps_per_run
    if configured is None:
        return
    if configured >= MAX_STEPS_DOWNLOAD_WARNING_THRESHOLD:
        return
    LOG.warning(
        "file_download block configured with low max_steps_per_run",
        log_code="file_download_low_max_steps",
        block_label=block.label,
        max_steps_per_run=configured,
        recommended_minimum=MAX_STEPS_DOWNLOAD_WARNING_THRESHOLD,
        workflow_run_id=workflow_run_id,
    )


async def capture_block_download_baseline(
    context: SkyvernContext,
    organization_id: str,
    workflow_run_id: str,
    block_label: str,
) -> None:
    """Snapshot the files already downloaded before this block runs.

    Recorded in ``loop_internal_state`` so ``filter_downloaded_files_for_current_iteration``
    scopes the block's output to only the files it produced. Captured fresh for every
    block — including each block inside a loop iteration — so sibling download-producing
    blocks don't inherit one another's files. Best-effort: cleared on timeout/error.
    """
    try:
        async with asyncio.timeout(GET_DOWNLOADED_FILES_TIMEOUT):
            baseline_files = await app.STORAGE.get_downloaded_files(
                organization_id=organization_id,
                run_id=resolve_run_download_id(context, fallback_run_id=workflow_run_id),
            )
            context.loop_internal_state = {
                DOWNLOADED_FILE_SIGS_KEY: [to_downloaded_file_signature(fi) for fi in baseline_files],
            }
            LOG.debug(
                "Captured block download baseline",
                workflow_run_id=workflow_run_id,
                organization_id=organization_id,
                block_label=block_label,
                file_count=len(baseline_files),
            )
    except asyncio.TimeoutError:
        context.loop_internal_state = None
        LOG.warning(
            "Timeout capturing baseline downloaded files for task block",
            workflow_run_id=workflow_run_id,
            organization_id=organization_id,
            block_label=block_label,
        )
    except Exception:
        # Baseline capture is best-effort — transient S3/network errors should
        # not abort the block. Degrade to unscoped filtering (the pre-fix behavior).
        context.loop_internal_state = None
        LOG.warning(
            "Failed to capture baseline downloaded files for task block",
            workflow_run_id=workflow_run_id,
            organization_id=organization_id,
            block_label=block_label,
            exc_info=True,
        )


DOWNLOAD_BINDING_FAILURE_REASON = (
    "A file downloaded into the run directory but no registered download landed in the block output."
)
UNBOUND_DOWNLOAD_OUTPUT_KEY = "download_registration_failed"


def bind_downloaded_files_to_output(result: Any, downloaded_files: list[FileInfo]) -> Any:
    """Attach registration evidence to whatever the block's code returned.

    The host's ``FileInfo`` list is the evidence of record, so it overwrites any registration-shaped
    keys the executed code authored for itself. With no host evidence those keys are dropped rather
    than left to read as a registration that never happened. The caller's dict is never mutated —
    the secure arm passes the sidecar result's own payload."""
    if not downloaded_files:
        # Only a truthy authored value is a claim to drop; a None or empty field is schema, and
        # removing it would break templates that dereference it under strict rendering.
        if isinstance(result, dict) and any(result.get(key) for key in REGISTERED_DOWNLOAD_OUTPUT_KEYS):
            result = {
                key: value for key, value in result.items() if not (key in REGISTERED_DOWNLOAD_OUTPUT_KEYS and value)
            }
        # The completion grader reads a nested ``output`` mapping too, so a claim parked one level
        # down reads as a registration just as convincingly as one at the root.
        nested = result.get("output") if isinstance(result, dict) else None
        if isinstance(nested, dict) and any(nested.get(key) for key in REGISTERED_DOWNLOAD_OUTPUT_KEYS):
            result = dict(result) if isinstance(result, dict) else result
            result["output"] = {
                key: value for key, value in nested.items() if not (key in REGISTERED_DOWNLOAD_OUTPUT_KEYS and value)
            }
        return result
    if not isinstance(result, dict):
        result = {"value": result} if result is not None else {}
    else:
        result = dict(result)
    result["downloaded_files"] = [file_info.model_dump() for file_info in downloaded_files]
    result["downloaded_file_urls"] = [file_info.url for file_info in downloaded_files]
    result["downloaded_file_artifact_ids"] = [
        file_info.artifact_id for file_info in downloaded_files if file_info.artifact_id
    ]
    return result


def local_download_dir_file_identities(download_run_id: str | None) -> set[tuple[str, int, int]] | None:
    """(name, size, mtime_ns) of the files in the run's local download directory, so a same-name
    overwrite is visible in a before/after diff. ``None`` means the directory could not be read —
    an unknown snapshot, never an empty one, so a failed read cannot make pre-existing files look
    new; a single entry vanishing mid-scan is skipped rather than voiding the whole snapshot."""
    if not download_run_id:
        return None
    try:
        identities: set[tuple[str, int, int]] = set()
        for entry in Path(get_download_dir(download_run_id)).iterdir():
            try:
                if not entry.is_file():
                    continue
                stat = entry.stat()
            except OSError:
                continue
            identities.add((entry.name, stat.st_size, stat.st_mtime_ns))
        return identities
    except Exception:
        return None


def download_binding_of(browser_state: BrowserState | None) -> DownloadBinding:
    return browser_state.browser_artifacts.download_binding if browser_state else DownloadBinding.RUN_DIR


def session_download_lane_active(browser_state: BrowserState | None) -> bool:
    """A session-keyed DOWNLOAD row only reaches a run-scoped read on the artifact-first path; without
    HMAC signing that read falls back to an S3 listing under the run prefix, which never holds one."""
    return download_binding_of(browser_state) is DownloadBinding.SESSION_DIR and bool(
        settings.ARTIFACT_CONTENT_HMAC_KEYRING
    )


def unbound_download_output(result: Any) -> dict[str, Any]:
    """A non-null payload for a block whose download never bound, carrying no registration keys."""
    payload = dict(result) if isinstance(result, dict) else ({"value": result} if result is not None else {})
    payload[UNBOUND_DOWNLOAD_OUTPUT_KEY] = True
    return payload


if settings.WORKFLOW_TEMPLATING_STRICTNESS == "strict":
    jinja_sandbox_env = SandboxedEnvironment(undefined=StrictUndefined)
else:
    jinja_sandbox_env = SandboxedEnvironment()


# Date format used for the built-in {{current_date}} reserved parameter.
CURRENT_DATE_FORMAT = "%Y-%m-%d"

jinja_sandbox_env.filters["json"] = _json_type_filter


# Mapping from TaskV2Status to the corresponding BlockStatus. Declared once at
# import time so it is not recreated on each block execution.
TASKV2_TO_BLOCK_STATUS: dict[TaskV2Status, BlockStatus] = {
    TaskV2Status.completed: BlockStatus.completed,
    TaskV2Status.terminated: BlockStatus.terminated,
    TaskV2Status.failed: BlockStatus.failed,
    TaskV2Status.canceled: BlockStatus.canceled,
    TaskV2Status.timed_out: BlockStatus.timed_out,
}

TASK_TO_BLOCK_STATUS: dict[TaskStatus, BlockStatus] = {
    TaskStatus.completed: BlockStatus.completed,
    TaskStatus.terminated: BlockStatus.terminated,
    TaskStatus.failed: BlockStatus.failed,
    TaskStatus.canceled: BlockStatus.canceled,
    TaskStatus.timed_out: BlockStatus.timed_out,
}


def extract_file_url_from_block_output(value: Any) -> str | None:
    """Extract a file URL from block output values that wrap downloaded files."""
    if isinstance(value, dict):
        downloaded_files = value.get("downloaded_files")
        if isinstance(downloaded_files, list) and downloaded_files:
            first_file = downloaded_files[0]
            if isinstance(first_file, dict):
                return first_file.get("url") or first_file.get("file_path") or None

        for key in ("artifact_url", "file_url", "file_path"):
            extracted = value.get(key)
            if isinstance(extracted, str) and extracted:
                return extracted
        return None

    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return extract_file_url_from_block_output(parsed)
        except (json.JSONDecodeError, ValueError):
            pass
        try:
            parsed = ast.literal_eval(value)
            if isinstance(parsed, dict):
                return extract_file_url_from_block_output(parsed)
        except (ValueError, SyntaxError):
            pass
    return None


def sanitize_filename(filename: str, default: str = "document") -> str:
    sanitized = re.sub(r'[<>:"/\\|?*]', "_", filename).strip(". ")
    return sanitized[:200] if sanitized else default


class ParquetExportMixin:
    """Shared Parquet-export execution: schema-directed serialization, filename
    resolution (with loop-iteration suffixing), atomic file write, and download
    registration. Used by DataExportBlock and by ExtractionBlock's export option."""

    @staticmethod
    def parse_export_records(data: str) -> list[Any]:
        try:
            records = json.loads(data)
        except json.JSONDecodeError as exc:
            raise ParquetExportError("data must resolve to a JSON array") from exc
        if not isinstance(records, list):
            raise ParquetExportError("data must resolve to a JSON array of object records")
        return records

    def _resolve_export_file_name(
        self, file_name: str | None, label: str, workflow_run_context: WorkflowRunContext
    ) -> str:
        stem = sanitize_filename(file_name or label)
        if stem.lower().endswith(".parquet"):
            stem = stem[: -len(".parquet")]
        current_index = workflow_run_context.get_block_metadata(label).get("current_index")
        if isinstance(current_index, int) and not isinstance(current_index, bool):
            stem = f"{stem}-{current_index + 1:04d}"
        return f"{stem}.parquet"

    async def _register_export_download(
        self,
        *,
        organization_id: str | None,
        run_download_id: str | None,
        workflow_run_id: str,
        workflow_run_block_id: str,
    ) -> list[FileInfo]:
        if not organization_id:
            return []
        try:
            async with asyncio.timeout(SAVE_DOWNLOADED_FILES_TIMEOUT):
                await app.STORAGE.save_downloaded_files(organization_id=organization_id, run_id=run_download_id)
        except asyncio.TimeoutError:
            LOG.warning(
                "Timeout saving Parquet export; workflow finalization will retry",
                workflow_run_id=workflow_run_id,
                workflow_run_block_id=workflow_run_block_id,
            )
            return []
        except DownloadSaveIncompleteError:
            pass
        except Exception:
            LOG.warning(
                "Failed to register Parquet export; workflow finalization will retry",
                workflow_run_id=workflow_run_id,
                workflow_run_block_id=workflow_run_block_id,
                exc_info=True,
            )
            return []
        try:
            async with asyncio.timeout(GET_DOWNLOADED_FILES_TIMEOUT):
                return await app.STORAGE.get_downloaded_files(organization_id=organization_id, run_id=run_download_id)
        except Exception:
            LOG.warning(
                "Failed to read registered Parquet exports",
                workflow_run_id=workflow_run_id,
                workflow_run_block_id=workflow_run_block_id,
                exc_info=True,
            )
            return []

    async def write_parquet_export(
        self,
        *,
        records: Any,
        data_schema: dict[str, Any],
        file_name: str | None,
        label: str,
        workflow_run_context: WorkflowRunContext,
        workflow_run_id: str,
        workflow_run_block_id: str,
        organization_id: str | None,
    ) -> dict[str, Any]:
        """Writes records to Parquet and registers the download. Raises ParquetExportError
        on a bad schema/records or a file-write failure."""
        parquet_data = export_parquet_records(records, data_schema)

        filename = self._resolve_export_file_name(file_name, label, workflow_run_context)
        run_download_id = resolve_run_download_id(skyvern_context.current(), fallback_run_id=workflow_run_id)
        path = get_path_for_workflow_download_directory(run_download_id) / filename
        file_created = False
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            stem = path.stem
            suffix = 2
            while True:
                try:
                    with path.open("xb") as parquet_file:
                        file_created = True
                        parquet_file.write(parquet_data)
                    break
                except FileExistsError:
                    path = path.with_name(f"{stem}-{suffix:04d}{path.suffix}")
                    suffix += 1
        except OSError as exc:
            if file_created:
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
                except OSError:
                    LOG.warning("Failed to remove incomplete Parquet export", exc_info=True)
            raise ParquetExportError(f"failed to write {filename}: {exc}") from exc

        filename = path.name
        downloaded_files = await self._register_export_download(
            organization_id=organization_id,
            run_download_id=run_download_id,
            workflow_run_id=workflow_run_id,
            workflow_run_block_id=workflow_run_block_id,
        )
        downloaded_files = [file for file in downloaded_files if file.filename == filename]
        return {
            "file_name": filename,
            "file_path": str(path),
            "file_size": path.stat().st_size,
            "format": "parquet",
            "compression": "snappy",
            "row_count": len(records),
            "columns": list(data_schema.get("items", {}).get("properties", {})),
            "downloaded_files": [file.model_dump() for file in downloaded_files],
            "downloaded_file_urls": [file.url for file in downloaded_files],
        }


def _format_payload_path_segment(key: str) -> str:
    """Plain identifiers render as `.key`; anything else (dots, brackets, spaces,
    quotes) renders as a bracketed JSON-escaped string so paths stay unambiguous
    against keys that contain `.` or `[`."""
    if key.isidentifier():
        return f".{key}"
    return f"[{json.dumps(key)}]"


# ForLoop constants
DEFAULT_MAX_LOOP_ITERATIONS = 1000
# Persist accumulated loop output to DB every N iterations to survive timeouts.
# Trades up to N-1 iterations of data loss for O(N/K) writes instead of O(N).
PERSIST_LOOP_OUTPUT_INTERVAL = 10
DEFAULT_MAX_STEPS_PER_ITERATION = 50

# Per-field cap for DecisionBlock debug payload (rendered_expression, llm_response, llm_prompt).
# Same fields exist as branch_metadata debug surface for script-reviewer / UI display; their
# unbounded form has produced multi-hundred-MB output_parameter rows under recursive Jinja.
DECISION_BLOCK_FIELD_MAX_BYTES = 64 * 1024


def _maybe_truncate_loop_outputs(
    outputs_with_loop_values: list[list[dict[str, Any]]],
    *,
    workflow_run_id: str,
    output_parameter_id: str | None,
) -> None:
    """Fail-open in-memory cap for loop accumulators; preserves per-entry schema (SKY-9779)."""
    try:
        size_bytes = len(json.dumps(outputs_with_loop_values, default=str).encode("utf-8"))
    except Exception:
        LOG.warning(
            "Failed to measure loop output size; skipping truncation",
            workflow_run_id=workflow_run_id,
            output_parameter_id=output_parameter_id,
        )
        return

    if size_bytes <= OUTPUT_PARAMETER_MAX_VALUE_BYTES:
        return

    summarized_through = len(outputs_with_loop_values) - 1
    summary_entry = [
        {
            "loop_value": None,
            "output_parameter": None,
            "output_value": {
                "truncated": True,
                "reason": "loop_output_size_exceeded",
                "iterations_summarized_through": summarized_through,
            },
        }
    ]
    LOG.warning(
        "Truncating loop output accumulator",
        workflow_run_id=workflow_run_id,
        output_parameter_id=output_parameter_id,
        size_bytes=size_bytes,
        limit_bytes=OUTPUT_PARAMETER_MAX_VALUE_BYTES,
        iterations_summarized_through=summarized_through,
    )
    last = outputs_with_loop_values[-1]
    outputs_with_loop_values.clear()
    outputs_with_loop_values.append(summary_entry)
    outputs_with_loop_values.append(last)


def build_block_failure_output(failure_reason: str, error_codes: Sequence[str]) -> dict[str, Any]:
    # SKY-7939: also surface the failure in `outputs.<block_label>` so callers
    # can tell which block failed without cross-referencing the timeline.
    return {
        "status": BlockStatus.failed.value,
        "failure_reason": failure_reason,
        "errors": [{"error_code": code, "reasoning": failure_reason, "confidence_float": 1.0} for code in error_codes],
    }


def user_defined_failure_category(error: UserDefinedError) -> list[dict[str, Any]]:
    """A declared error code is the workflow author's own verdict on why the run stopped.
    ``_resolve_block_terminal_outcome`` prefers a block output's ``failure_category`` over
    keyword classification, so carrying it here keeps the run out of UNKNOWN."""
    return [
        {
            "category": error.error_code,
            "confidence_float": error.confidence_float,
            "reasoning": error.reasoning,
        }
    ]


def build_user_defined_error_output(error_code: str, reasoning: str) -> dict[str, Any]:
    """<label>_output payload for a declared ErrorCode raise (SKY-13668): a UserDefinedError
    entry with confidence 1.0, matching UserDefinedError's own serialized shape."""
    error = UserDefinedError(error_code=error_code, reasoning=reasoning, confidence_float=1.0)
    return {
        "status": BlockStatus.failed.value,
        "failure_reason": reasoning,
        "errors": [error.model_dump(mode="json")],
        "failure_category": user_defined_failure_category(error),
    }


class Block(BaseModel, abc.ABC):
    """Base class for workflow nodes (see branching spec [[s-4bnl]] for metadata semantics)."""

    # Must be unique within workflow definition
    label: str = Field(description="Author-facing identifier for a block; unique within a workflow.")
    next_block_label: str | None = Field(
        default=None,
        description="Optional pointer to the next block label when constructing a DAG. "
        "Defaults to sequential order when omitted.",
    )
    block_type: BlockType
    output_parameter: OutputParameter
    continue_on_failure: bool = False
    model: dict[str, Any] | None = None
    disable_cache: bool = False
    # Opt-out from workflow-level workflow_system_prompt inheritance (and, on a
    # WorkflowTriggerBlock, from propagating the parent chain's prompt into the
    # spawned child run). A no-op for deterministic blocks that don't call an LLM.
    ignore_workflow_system_prompt: bool = False
    # Runtime cache populated by ``Block._apply_workflow_system_prompt`` — not
    # user-settable. Excluded from serialization (``model_dump`` / JSON / API
    # responses) so the resolved prompt doesn't leak into logs, workflow
    # definition round-trips, or responses that weren't meant to carry it.
    # Deliberately absent from the BlockYAML schema so it can never be set
    # through YAML or the API. The user-facing opt-out is
    # ``ignore_workflow_system_prompt``. Only consumed by block types that call
    # an LLM; deterministic blocks ignore it.
    workflow_system_prompt: str | None = Field(default=None, exclude=True)

    # Only valid for blocks inside a for loop block
    # Whether to continue to the next iteration when the block fails
    next_loop_on_failure: bool = False

    # Set by record_output_parameter_value within a single execute_safe call; lets the
    # failure handler tell a value recorded during THIS execution apart from a stale
    # value left by a prior for-loop iteration.
    _output_recorded_this_execution: bool = PrivateAttr(default=False)

    # Fields this class renders as Jinja. Declared per class; the effective set is the
    # union over the MRO, so a subclass never shadows what its base renders.
    TEMPLATABLE_FIELDS: ClassVar[frozenset[str]] = frozenset()

    @classmethod
    def templatable_fields(cls) -> frozenset[str]:
        """Block fields rendered as Jinja. Branch-criteria expressions are templates by definition
        and render through the raw formatter, so they are not declared here."""
        declared: set[str] = set()
        for klass in cls.__mro__:
            declared.update(klass.__dict__.get("TEMPLATABLE_FIELDS", ()))
        return frozenset(declared)

    def render_templatable_field(
        self,
        field: str,
        value: str,
        workflow_run_context: WorkflowRunContext,
        *,
        force_include_secrets: bool = False,
        env: SandboxedEnvironment | None = None,
        skip_missing_variable_preflight: bool = False,
    ) -> str:
        if field not in type(self).model_fields:
            raise ValueError(f"{type(self).__name__} has no field named {field!r}")
        if field not in self.templatable_fields():
            LOG.debug("Skipping Jinja render for a non-templatable field", block_label=self.label, field=field)
            return value
        LOG.debug("Rendering templatable field", block_label=self.label, field=field)
        return self.format_block_parameter_template_from_workflow_run_context(
            value,
            workflow_run_context,
            force_include_secrets=force_include_secrets,
            env=env,
            skip_missing_variable_preflight=skip_missing_variable_preflight,
        )

    @staticmethod
    def _registered_secret_values(workflow_run_context: WorkflowRunContext) -> set[str]:
        return {value for value in workflow_run_context.secrets.values() if isinstance(value, str) and value}

    @classmethod
    def _contains_registered_secret(cls, value: str, workflow_run_context: WorkflowRunContext) -> bool:
        """Whether `value` carries a registered secret, at ANY length.

        Deliberately unfloored: the code-escalation path uses this to refuse an error code that would
        carry a secret into generated code and into persisted failure artifacts, where catching a
        short embedded credential matters more than the odd false positive. Callers that DELETE
        ordinary customer data on a hit want the floored form -- see _contains_registered_secret_floored.
        """
        return any(secret in value for secret in cls._registered_secret_values(workflow_run_context))

    @classmethod
    def _contains_registered_secret_floored(cls, value: str, workflow_run_context: WorkflowRunContext) -> bool:
        """The same test with the redaction stack's own length floors applied.

        For callers that DELETE on a hit. Unfloored, a two-character secret such as a card expiry
        makes HTTP_405_DECLINED look secret-bearing and removes a legitimate error code -- a guard
        destroying the output it exists to protect.
        """
        return any(
            secret in value
            for secret in cls._registered_secret_values(workflow_run_context)
            if len(secret) >= MIN_SECRET_LENGTH and not (secret.isdigit() and len(secret) < MIN_NUMERIC_SECRET_LENGTH)
        )

    @classmethod
    def _redact_registered_secrets(cls, value: str, workflow_run_context: WorkflowRunContext) -> str:
        for secret in sorted(cls._registered_secret_values(workflow_run_context), key=len, reverse=True):
            value = value.replace(secret, "[redacted]")
        return value

    def _render_error_code_mapping(
        self,
        block_mapping: dict[str, str] | None,
        workflow_mapping: dict[str, str] | None,
        workflow_run_context: WorkflowRunContext,
        *,
        for_generated_code: bool,
    ) -> dict[str, str] | None:
        """Render a block's error_code_mapping, inheriting the workflow's, and repair what it can.

        The MERGE ORDER is part of what the two callers must agree on, so it lives here rather than
        with each of them. Block entries are offered FIRST: a colliding key keeps the block's value
        (the documented block-over-workflow precedence), and when the aggregate cap binds it is the
        workflow's entries that are dropped rather than the block's own.

        error_code_mapping is templatable, so the author-time schema validates a string that is not
        yet the string the model will see. A rendered KEY that is unusable means the entry goes -- it
        is the identifier the model names and the customer matches on, and it cannot be repaired. A
        rendered DESCRIPTION is prose and IS repairable, so it is normalized, not dropped: deleting an
        entry over a trailing newline removes a customer's error code, and if it was the only entry
        the mapping goes falsy and error detection is skipped entirely.
        """
        ordered: list[tuple[str, str]] = list((block_mapping or {}).items())
        seen_source_keys = {code for code, _ in ordered}
        ordered += [(code, text) for code, text in (workflow_mapping or {}).items() if code not in seen_source_keys]

        rendered_mapping: dict[str, str] = {}
        rendered_mapping_utf8_bytes = 0
        dropped_reasons: list[str] = []
        for error_code, error_description in ordered:
            rendered_code = self.render_templatable_field("error_code_mapping", error_code, workflow_run_context)
            if rendered_code in rendered_mapping:
                # Block entries come first, so an earlier occupant is the one precedence keeps.
                continue
            rendered_description = self.render_templatable_field(
                "error_code_mapping", error_description, workflow_run_context
            )
            rendered_description = self._redact_registered_secrets(rendered_description, workflow_run_context)
            secret_bearing_key = (
                self._contains_registered_secret(rendered_code, workflow_run_context)
                if for_generated_code
                else self._contains_registered_secret_floored(rendered_code, workflow_run_context)
            )
            if secret_bearing_key or workflow_run_context.mask_secrets_in_data(rendered_code) != rendered_code:
                dropped_reasons.append("error code keys must not contain a registered secret")
                continue
            key_reason = error_code_key_error(rendered_code)
            if key_reason:
                # The reason, not the code: the key is customer-authored text and this log is not
                # covered by the run-secret scrub (SKY-15586 F2, pre-existing).
                dropped_reasons.append(key_reason)
                continue
            if not for_generated_code:
                normalized_description = normalize_error_code_description(rendered_description)
                if normalized_description is None:
                    dropped_reasons.append("error code descriptions must contain something after normalization")
                    continue
            else:
                description_reason = error_code_mapping_entry_error(rendered_code, rendered_description)
                if description_reason:
                    dropped_reasons.append(description_reason)
                    continue
                normalized_description = rendered_description
            rendered_entry_utf8_bytes = len(rendered_code.encode("utf-8")) + len(normalized_description.encode("utf-8"))
            candidate_utf8_bytes = rendered_mapping_utf8_bytes + rendered_entry_utf8_bytes
            if (
                len(rendered_mapping) + 1 > ERROR_CODE_MAPPING_MAX_ENTRIES
                or candidate_utf8_bytes > ERROR_CODE_MAPPING_MAX_UTF8_BYTES
            ):
                # Per-entry rules bound one string; only a running total bounds what rendering can
                # expand a whole mapping into, and this mapping is JSON-dumped into the prompt.
                dropped_reasons.append("error_code_mapping exceeded its aggregate entry or byte cap")
                continue
            rendered_mapping[rendered_code] = normalized_description
            rendered_mapping_utf8_bytes = candidate_utf8_bytes
        if dropped_reasons:
            LOG.warning(
                "error_code_mapping entries dropped after template rendering",
                block_label=self.label,
                dropped=len(dropped_reasons),
                kept=len(rendered_mapping),
                reasons=sorted(set(dropped_reasons)),
            )
        return rendered_mapping or None

    def _own_llm_key(self) -> str | None:
        return None

    @property
    def override_llm_key(self) -> str | None:
        return self.override_llm_key_for_organization(None)

    def override_llm_key_for_organization(self, organization_id: str | None) -> str | None:
        """Resolve an explicit model mapping or the block's own LLM key."""
        own_llm_key = self._own_llm_key() or None
        if self.model:
            model_name = self.model.get("model_name")
            if model_name:
                mapping = SettingsManager.get_settings().get_model_name_to_llm_key(organization_id=organization_id)
                llm_key = mapping.get(model_name, {}).get("llm_key")
                if llm_key:
                    return llm_key
                if is_custom_llm_model_name(model_name):
                    raise ValueError("Custom LLM model not found for organization")
                return own_llm_key

        return own_llm_key

    async def record_output_parameter_value(
        self,
        workflow_run_context: WorkflowRunContext,
        workflow_run_id: str,
        value: dict[str, Any] | list | str | None = None,
    ) -> None:
        await workflow_run_context.register_output_parameter_value_post_execution(
            parameter=self.output_parameter,
            value=value,
        )
        self._output_recorded_this_execution = True
        await app.DATABASE.workflow_runs.create_or_update_workflow_run_output_parameter(
            workflow_run_id=workflow_run_id,
            output_parameter_id=self.output_parameter.output_parameter_id,
            value=value,
        )
        LOG.info(
            "Registered output parameter value",
            sampling=True,
            output_parameter_id=self.output_parameter.output_parameter_id,
            workflow_run_id=workflow_run_id,
            output_parameter_value_present=value is not None,
        )

    async def _template_format_failure_result(
        self,
        exc: Exception,
        failure_reason: str,
        workflow_run_context: WorkflowRunContext,
        workflow_run_id: str,
        workflow_run_block_id: str | None,
        organization_id: str | None,
    ) -> BlockResult:
        failure_reason = self._redact_registered_secrets(failure_reason, workflow_run_context)
        error_codes = self.get_failure_error_codes()
        failure_output: dict[str, Any] = (
            build_block_failure_output(failure_reason, error_codes)
            if error_codes
            else {"failure_reason": failure_reason}
        )
        if isinstance(exc, FailedToFormatJinjaStyleParameter) and exc.available_keys:
            failure_output["available_keys"] = [
                self._redact_registered_secrets(key, workflow_run_context) for key in exc.available_keys
            ]
        await self.record_output_parameter_value(workflow_run_context, workflow_run_id, failure_output)
        return await self.build_block_result(
            success=False,
            failure_reason=failure_reason,
            output_parameter_value=failure_output,
            status=BlockStatus.failed,
            workflow_run_block_id=workflow_run_block_id,
            organization_id=organization_id,
            error_codes=error_codes or None,
        )

    async def build_block_result(
        self,
        success: bool,
        failure_reason: str | None,
        output_parameter_value: dict[str, Any] | list | str | None = None,
        status: BlockStatus | None = None,
        workflow_run_block_id: str | None = None,
        organization_id: str | None = None,
        executed_branch_id: str | None = None,
        executed_branch_expression: str | None = None,
        executed_branch_result: bool | None = None,
        executed_branch_next_block: str | None = None,
        error_codes: list[str] | None = None,
        is_synthetic_loop_failure: bool = False,
    ) -> BlockResult:
        # Every arm that reports a block failure lands here -- the raise path and the ones that
        # return an unsuccessful result -- so this is where the reason is scrubbed. It is persisted
        # to workflow_run_blocks.failure_reason and lifted onto the run, neither redacted downstream.
        if failure_reason:
            # Best-effort: this builder runs for every block result, including on paths that never
            # start a ForgeApp, and a secret lookup must never be what turns a block result into a
            # failure. Degrades to the unredacted reason, which is the pre-existing behaviour.
            try:
                context = skyvern_context.current()
                block_run_id = context.workflow_run_id if context is not None else None
                block_run_secrets = (
                    app.WORKFLOW_CONTEXT_MANAGER.get_secret_values_for_run(block_run_id)
                    if app.WORKFLOW_CONTEXT_MANAGER.artifact_redaction_enabled(block_run_id)
                    else app.WORKFLOW_CONTEXT_MANAGER.runtime_secret_values_for_artifacts()
                )
                if block_run_secrets:
                    failure_reason = redact_secrets_from_text(failure_reason, block_run_secrets)
            except Exception:
                LOG.warning("failed to redact a block failure reason", exc_info=True)

        # TODO: update workflow run block status and failure reason
        if isinstance(output_parameter_value, str):
            output_parameter_value = {"value": output_parameter_value}

        if workflow_run_block_id:
            await app.DATABASE.observer.update_workflow_run_block(
                workflow_run_block_id=workflow_run_block_id,
                output=output_parameter_value,
                status=status,
                failure_reason=failure_reason,
                organization_id=organization_id,
                executed_branch_id=executed_branch_id,
                executed_branch_expression=executed_branch_expression,
                executed_branch_result=executed_branch_result,
                executed_branch_next_block=executed_branch_next_block,
                error_codes=error_codes,
            )
        return BlockResult(
            success=success,
            failure_reason=failure_reason,
            error_codes=error_codes or [],
            output_parameter=self.output_parameter,
            output_parameter_value=output_parameter_value,
            status=status,
            workflow_run_block_id=workflow_run_block_id,
            is_synthetic_loop_failure=is_synthetic_loop_failure,
        )

    async def get_or_create_browser_state(
        self,
        workflow_run_id: str,
        organization_id: str | None = None,
        browser_session_id: str | None = None,
        download_run_id_override: str | None = None,
    ) -> BrowserState | None:
        """Acquire or create browser state for block execution.

        Persistent-session consumers delegate attachment and recovery to the browser manager so it
        can retain the runnable lease through normal run cleanup. Other consumers keep using the
        workflow-run browser cache and creation path.

        Returns BrowserState if successful, None if creation failed. A known disconnected
        state that cannot be rebuilt raises MissingBrowserState so the caller keeps its cause.
        """
        browser_state: BrowserState | None = None

        if browser_session_id and organization_id:
            context = skyvern_context.current()
            if context is not None and context.task_id:
                task = await app.DATABASE.tasks.get_task(
                    task_id=context.task_id,
                    organization_id=organization_id,
                )
                if task is None:
                    LOG.warning(
                        "Task not found while attaching browser session for block execution",
                        task_id=context.task_id,
                        browser_session_id=browser_session_id,
                        workflow_run_id=workflow_run_id,
                    )
                    return None
                browser_state = await app.BROWSER_MANAGER.get_or_create_for_task(
                    task=task,
                    browser_session_id=browser_session_id,
                )
            else:
                workflow_run = await app.WORKFLOW_SERVICE.get_workflow_run(
                    workflow_run_id=workflow_run_id,
                    organization_id=organization_id,
                )
                browser_state = await app.BROWSER_MANAGER.get_or_create_for_workflow_run(
                    workflow_run=workflow_run,
                    url=None,
                    browser_session_id=browser_session_id,
                    browser_profile_id=workflow_run.browser_profile_id,
                    browser_session_runnable_id=context.browser_session_runnable_id if context else None,
                    browser_session_runnable_generation_id=(
                        context.browser_session_runnable_generation_id if context else None
                    ),
                )
            return browser_state
        browser_state = app.BROWSER_MANAGER.get_for_workflow_run(workflow_run_id)

        # A cached workflow-run browser can have a dead Playwright driver after a prior owner
        # stopped it — reusing it makes the block's first page.goto raise "Connection closed
        # while reading from the driver". Rebuild a fresh connection to the same browser first.
        if browser_state is not None and not browser_state.is_connected():
            workflow_run = await app.WORKFLOW_SERVICE.get_workflow_run(
                workflow_run_id=workflow_run_id,
                organization_id=organization_id,
            )
            try:
                await browser_state.reconnect(
                    proxy_location=workflow_run.proxy_location,
                    workflow_run_id=workflow_run_id,
                    workflow_permanent_id=workflow_run.workflow_permanent_id,
                    organization_id=workflow_run.organization_id,
                    extra_http_headers=workflow_run.extra_http_headers,
                    cdp_connect_headers=workflow_run.cdp_connect_headers,
                    browser_address=workflow_run.browser_address,
                    browser_profile_id=workflow_run.browser_profile_id,
                )
                LOG.info(
                    "Rebuilt a disconnected browser state before block execution",
                    workflow_run_id=workflow_run_id,
                )
            except Exception as exc:
                LOG.warning(
                    "Failed to rebuild disconnected browser state",
                    workflow_run_id=workflow_run_id,
                )
                raise MissingBrowserState(
                    workflow_run_id=workflow_run_id,
                    diagnostic=get_browser_state_diagnostic(browser_state),
                    detected_at=datetime.now(UTC),
                    failure_reason=f"reconnect_failed:{type(exc).__name__}",
                ) from exc

        if not browser_state:
            workflow_run = await app.WORKFLOW_SERVICE.get_workflow_run(
                workflow_run_id=workflow_run_id,
                organization_id=organization_id,
            )
            try:
                browser_state = await app.BROWSER_MANAGER.get_or_create_for_workflow_run(
                    workflow_run=workflow_run,
                    url=None,
                    browser_session_id=browser_session_id,
                    browser_profile_id=workflow_run.browser_profile_id,
                )
                await browser_state.check_and_fix_state(
                    url=None,
                    proxy_location=workflow_run.proxy_location,
                    workflow_run_id=workflow_run_id,
                    workflow_permanent_id=workflow_run.workflow_permanent_id,
                    organization_id=workflow_run.organization_id,
                    extra_http_headers=workflow_run.extra_http_headers,
                    browser_address=workflow_run.browser_address,
                    browser_profile_id=workflow_run.browser_profile_id,
                )
            except Exception:
                LOG.warning(
                    "Failed to create browser state",
                    workflow_run_id=workflow_run_id,
                )
                return None

        if not (browser_session_id and organization_id) and browser_state is not None:
            rebind_run_id = download_run_id_override or resolve_run_download_id(
                skyvern_context.current(), fallback_run_id=workflow_run_id
            )
            try:
                if browser_state.browser_artifacts.download_binding == DownloadBinding.SESSION_DIR:
                    # Defense-in-depth: a mismatched caller (e.g. a cached adopted state fetched without a
                    # browser_session_id) must never rebind a provider-owned remote binding off its
                    # provider-selected destination.
                    LOG.info(
                        "Skipping workflow-run download-dir rebind: preserving provider-selected destination",
                        workflow_run_id=workflow_run_id,
                    )
                else:
                    owning_browser = browser_state.browser_context.browser if browser_state.browser_context else None
                    rebind_page = None if owning_browser is not None else await browser_state.get_working_page()
                    if owning_browser is not None or rebind_page is not None:
                        await rebind_download_dir(owning_browser, run_id=rebind_run_id, page=rebind_page)
                        LOG.info(
                            "Rebound download dir on workflow-run browser",
                            workflow_run_id=workflow_run_id,
                            run_id=rebind_run_id,
                        )
            except Exception:
                LOG.warning(
                    "Failed to rebind download dir on workflow-run browser",
                    workflow_run_id=workflow_run_id,
                    run_id=rebind_run_id,
                )

        return browser_state

    def format_block_parameter_template_from_workflow_run_context(
        self,
        potential_template: str,
        workflow_run_context: WorkflowRunContext,
        *,
        force_include_secrets: bool = False,
        env: SandboxedEnvironment | None = None,
        skip_missing_variable_preflight: bool = False,
    ) -> str:
        """
        Format a template string using the workflow run context.

        Security Note:
        Real secret values are ONLY resolved for blocks that do NOT expose data to the LLM
        (like HttpRequestBlock, CodeBlock), as determined by is_safe_block_for_secrets.
        """
        if not potential_template:
            return potential_template

        # Security: only allow real secret values for non-LLM blocks (HttpRequestBlock, CodeBlock)
        is_safe_block_for_secrets = self.block_type in [
            BlockType.CODE,
            BlockType.HTTP_REQUEST,
        ]

        try:
            template = (env or jinja_sandbox_env).from_string(potential_template)
        except Exception as exc:
            raise FailedToFormatJinjaStyleParameter(potential_template, str(exc)) from exc

        block_reference_data: dict[str, Any] = workflow_run_context.get_block_metadata(self.label)
        template_data = workflow_run_context.values.copy()

        include_secrets = workflow_run_context.include_secrets_in_templates or force_include_secrets

        # FORCE DISABLE if block is not safe (sends data to LLM)
        if include_secrets and not is_safe_block_for_secrets:
            include_secrets = False

        if include_secrets:
            # parameters is declared per block type, not on the Block base; get_all_parameters
            # is not a plain accessor (some overrides resolve run context) so read the field.
            declared_parameters: list[PARAMETER_TYPE] = getattr(self, "parameters", None) or []
            declared_keys = [parameter.key for parameter in declared_parameters if parameter.key]
            template_data.update(
                workflow_run_context.credential_template_entries(
                    declared_keys, resolve_credential_dicts=is_safe_block_for_secrets
                )
            )

        if self.label in template_data:
            current_value = template_data[self.label]
            if isinstance(current_value, dict):
                block_reference_data.update(current_value)
            else:
                LOG.debug(
                    f"Parameter {self.label} has a registered reference value, going to overwrite it by block metadata"
                )

        template_data[self.label] = block_reference_data

        # TODO (suchintan): This is pretty hacky - we should have a standard way to initialize the workflow run context
        # inject the forloop metadata as global variables
        if "current_index" in block_reference_data:
            template_data["current_index"] = block_reference_data["current_index"]
        if "current_item" in block_reference_data:
            template_data["current_item"] = block_reference_data["current_item"]
        if "current_value" in block_reference_data:
            template_data["current_value"] = block_reference_data["current_value"]

        # Initialize workflow-level parameters
        if "workflow_title" not in template_data:
            template_data["workflow_title"] = workflow_run_context.workflow_title
        if "workflow_id" not in template_data:
            template_data["workflow_id"] = workflow_run_context.workflow_id
        if "workflow_permanent_id" not in template_data:
            template_data["workflow_permanent_id"] = workflow_run_context.workflow_permanent_id
        if "workflow_run_id" not in template_data:
            template_data["workflow_run_id"] = workflow_run_context.workflow_run_id
        if "current_date" not in template_data:
            template_data["current_date"] = datetime.now(UTC).strftime(CURRENT_DATE_FORMAT)
        if "browser_session_id" not in template_data:
            template_data["browser_session_id"] = workflow_run_context.browser_session_id or ""

        template_data["workflow_run_outputs"] = workflow_run_context.workflow_run_outputs
        template_data["workflow_run_summary"] = workflow_run_context.build_workflow_run_summary()

        # A caller whose environment decides for itself what an absent binding means renders instead of
        # failing here, so `| default(...)` still reaches an undefined the preflight would reject.
        if settings.WORKFLOW_TEMPLATING_STRICTNESS == "strict" and not skip_missing_variable_preflight:
            if missing_variables := get_missing_variables(potential_template, template_data):
                raise MissingJinjaVariables(
                    template=potential_template,
                    variables=missing_variables,
                )

        try:
            return template.render(template_data)
        except SkyvernException:
            raise
        except Exception as exc:
            raise FailedToFormatJinjaStyleParameter(
                potential_template,
                str(exc),
                available_keys=get_available_keys(potential_template, template_data),
            ) from exc

    def _apply_workflow_system_prompt(
        self,
        workflow_run_context: WorkflowRunContext,
    ) -> None:
        """Resolve the workflow-level ``workflow_system_prompt`` for this block and
        materialize it onto ``self.workflow_system_prompt``.

        Concatenates any prompt inherited from ancestor workflows (propagated through
        ``WorkflowTriggerBlock``) with this workflow's own ``workflow_system_prompt``.
        Jinja substitutions on this workflow's own prompt are resolved against
        ``workflow_run_context``; the inherited portion is already resolved at the
        trigger boundary.

        Shared by every block type that needs to inherit the workflow system prompt
        into its own ``workflow_system_prompt`` runtime cache before dispatching an
        LLM call. Callers invoke this inside ``format_potential_template_parameters``
        so the value is available at execute time. ``workflow_system_prompt`` on each
        block is a runtime cache — it's deliberately absent from the BlockYAML schema
        and not user-settable.

        When a block opts out via ``ignore_workflow_system_prompt``, this leaves
        the block's own ``workflow_system_prompt`` untouched (falling back to the
        system default if none is set). The opt-out covers both this workflow's
        prompt and any inherited prompt from parent workflows.
        """
        if self.ignore_workflow_system_prompt:
            # Record the opt-out so the script path (``ai_extract``) reads the
            # same decision instead of re-resolving the flag from the
            # definition. See ``WorkflowRunContext.record_block_workflow_system_prompt``.
            workflow_run_context.record_block_workflow_system_prompt(self.label, None)
            return
        resolved = workflow_run_context.resolve_effective_workflow_system_prompt()
        if resolved is not None:
            self.workflow_system_prompt = resolved
        workflow_run_context.record_block_workflow_system_prompt(self.label, resolved)

    @classmethod
    def get_subclasses(cls) -> tuple[type[Block], ...]:
        return tuple(cls.__subclasses__())

    @staticmethod
    def get_workflow_run_context(workflow_run_id: str) -> WorkflowRunContext:
        return app.WORKFLOW_CONTEXT_MANAGER.get_workflow_run_context(workflow_run_id)

    @staticmethod
    def get_async_aws_client() -> AsyncAWSClient:
        return app.WORKFLOW_CONTEXT_MANAGER.aws_client

    @abc.abstractmethod
    async def execute(
        self,
        workflow_run_id: str,
        workflow_run_block_id: str,
        organization_id: str | None = None,
        browser_session_id: str | None = None,
        **kwargs: dict,
    ) -> BlockResult:
        pass

    async def _generate_workflow_run_block_description(
        self, workflow_run_block_id: str, organization_id: str | None = None
    ) -> None:
        if self.block_type in {BlockType.CODE, BlockType.FOR_LOOP, BlockType.WHILE_LOOP}:
            return
        description = None
        try:
            block_data = self.model_dump(
                exclude=SENSITIVE_DESTINATION_FIELDS
                | SENSITIVE_SEND_EMAIL_FIELDS
                | {
                    "workflow_run_block_id",
                    "organization_id",
                    "task_id",
                    "workflow_run_id",
                    "parent_workflow_run_block_id",
                    "label",
                    "status",
                    "output",
                    "continue_on_failure",
                    "failure_reason",
                    "actions",
                    "created_at",
                    "modified_at",
                },
                exclude_none=True,
            )
            # The description is a pure function of the rendered prompt, and a
            # workflow's blocks are identical run over run — cache on a hash of the
            # prompt itself so repeat runs skip the LLM call entirely and a template
            # change naturally invalidates the cache. output_parameter is reduced to
            # its key first: its row ids/timestamps vary per workflow revision
            # without changing what the description says. Cache failures fall
            # through to generation; only non-empty summaries are cached.
            prompt_block_data = dict(block_data)
            output_parameter_data = prompt_block_data.get("output_parameter")
            if isinstance(output_parameter_data, dict):
                prompt_block_data["output_parameter"] = output_parameter_data.get("key")
            description_generation_prompt = prompt_engine.load_prompt(
                "generate_workflow_run_block_description",
                block=prompt_block_data,
            )
            cache_key = "wrb-description:" + hashlib.sha256(description_generation_prompt.encode()).hexdigest()
            try:
                cached_description = await app.CACHE.get(cache_key)
            except Exception:
                LOG.debug("Failed to read cached workflow run block description", exc_info=True)
                cached_description = None
            if isinstance(cached_description, str) and cached_description:
                description = cached_description
            else:
                json_response = await get_org_aware_secondary_llm_api_handler(default=app.SECONDARY_LLM_API_HANDLER)(
                    prompt=description_generation_prompt,
                    prompt_name="generate-workflow-run-block-description",
                    workflow_run_block_id=workflow_run_block_id,
                    organization_id=organization_id,
                )
                description = json_response.get("summary")
                LOG.info(
                    "Generated description for the workflow run block",
                    sampling=True,
                    description=description,
                    workflow_run_block_id=workflow_run_block_id,
                )
                if isinstance(description, str) and description:
                    try:
                        await app.CACHE.set(cache_key, description)
                    except Exception:
                        LOG.debug("Failed to cache workflow run block description", exc_info=True)
        except Exception as e:
            LOG.exception("Failed to generate description for the workflow run block", error=e)

        if description:
            await app.DATABASE.observer.update_workflow_run_block(
                workflow_run_block_id=workflow_run_block_id,
                description=description,
                organization_id=organization_id,
            )

    def get_failure_error_codes(self) -> list[str]:
        """Return block-level error codes for unexpected failures. Override in subclasses."""
        return []

    async def _invalidate_stale_output_on_failure(
        self,
        workflow_run_id: str,
        current_index: int | None,
        *,
        include_missing_value_guard: bool,
    ) -> None:
        # On a failed execution, invalidate this block's output to None so a prior for-loop
        # iteration's value can't leak into the failed iteration's downstream blocks; a value
        # already recorded this execution is preserved and non-loop behavior is unchanged.
        if self._output_recorded_this_execution:
            return
        workflow_run_context = self.get_workflow_run_context(workflow_run_id)
        if current_index is not None or (
            include_missing_value_guard and not workflow_run_context.has_value(self.output_parameter.key)
        ):
            await self.record_output_parameter_value(workflow_run_context, workflow_run_id, None)

    @traced(name="skyvern.block.execute", role="wrapper")
    async def execute_safe(
        self,
        workflow_run_id: str,
        parent_workflow_run_block_id: str | None = None,
        organization_id: str | None = None,
        browser_session_id: str | None = None,
        current_value: str | None = None,
        current_index: int | None = None,
        **kwargs: dict,
    ) -> BlockResult:
        # block_type slices the 303s p95 by block kind — task/for_loop/code/extraction
        # have wildly different latency profiles. Set early so it's present even if
        # execute_safe raises before any child work.
        otel_trace.get_current_span().set_attribute("block_type", self.block_type.value)
        self._output_recorded_this_execution = False
        workflow_run_block_id = None
        engine: RunEngine | None = None
        try:
            if isinstance(self, BaseTaskBlock):
                engine = self.resolve_engine(workflow_run_id)

            workflow_run_block = await app.DATABASE.observer.create_workflow_run_block(
                workflow_run_id=workflow_run_id,
                organization_id=organization_id,
                parent_workflow_run_block_id=parent_workflow_run_block_id,
                label=self.label,
                block_type=self.block_type,
                continue_on_failure=self.continue_on_failure,
                engine=engine,
                current_value=current_value,
                current_index=current_index,
            )
            workflow_run_block_id = workflow_run_block.workflow_run_block_id

            current_context = skyvern_context.current()
            # Script-mode descriptions are cosmetic; loop iterations reuse the first description.
            if not (current_context and current_context.script_mode) and (current_index is None or current_index == 0):
                asyncio.create_task(
                    self._generate_workflow_run_block_description(workflow_run_block_id, organization_id)
                )

            # create a screenshot
            browser_state = app.BROWSER_MANAGER.get_for_workflow_run(workflow_run_id)
            if not browser_state:
                LOG.info(
                    "No browser state found when creating workflow_run_block",
                    workflow_run_id=workflow_run_id,
                    workflow_run_block_id=workflow_run_block_id,
                    browser_session_id=browser_session_id,
                    block_label=self.label,
                )
            else:
                try:
                    screenshot = await browser_state.take_fullpage_screenshot()
                except Exception:
                    LOG.warning(
                        "Failed to take screenshot before executing the block, ignoring the exception",
                        workflow_run_id=workflow_run_id,
                        workflow_run_block_id=workflow_run_block_id,
                    )
                    screenshot = None
                if screenshot:
                    await app.ARTIFACT_MANAGER.create_workflow_run_block_artifact(
                        workflow_run_block=workflow_run_block,
                        artifact_type=ArtifactType.SCREENSHOT_LLM,
                        data=screenshot,
                    )

            LOG.info(
                "Executing block",
                sampling=True,
                workflow_run_id=workflow_run_id,
                block_label=self.label,
                block_type=self.block_type,
            )
            result = await self.execute(
                workflow_run_id,
                workflow_run_block_id,
                organization_id=organization_id,
                browser_session_id=browser_session_id,
                **kwargs,
            )
            # Blocks that report failure by returning an unsuccessful BlockResult never reach the
            # except branch, and build_block_result does not touch WorkflowRunContext, so invalidate
            # stale loop output here too.
            if not result.success:
                await self._invalidate_stale_output_on_failure(
                    workflow_run_id, current_index, include_missing_value_guard=False
                )
            return result
        except Exception as e:
            if isinstance(e, (MissingBrowserState, MissingBrowserStatePage)):
                LOG.exception(
                    "Block execution failed because browser state is unavailable",
                    workflow_run_id=workflow_run_id,
                    block_label=self.label,
                    block_type=self.block_type,
                )
                failure_reason = get_user_facing_exception_message(e)
            elif isinstance(e, FailedToNavigateToUrl) and not isinstance(e, BlockedNavigationDestination):
                # The target site did not load. Site-caused, and already surfaced on the run through
                # failure_reason below. BlockedNavigationDestination is excluded on purpose: it is the
                # SSRF guard tripping, not a site failure, and it stays at error.
                LOG.warning(
                    "Block execution failed",
                    workflow_run_id=workflow_run_id,
                    block_label=self.label,
                    block_type=self.block_type,
                    url=e.url,
                    exc_info=True,
                )
                failure_reason = get_user_facing_exception_message(e)
            elif self.block_type in {BlockType.CODE, BlockType.FOR_LOOP, BlockType.WHILE_LOOP}:
                failure_label = "CodeBlock" if self.block_type == BlockType.CODE else "Loop block"
                LOG.error(
                    f"{failure_label} execution failed",
                    workflow_run_id=workflow_run_id,
                    block_label=self.label,
                    block_type=self.block_type,
                )
                failure_reason = f"{failure_label} execution failed."
            else:
                LOG.exception(
                    "Block execution failed",
                    workflow_run_id=workflow_run_id,
                    block_label=self.label,
                    block_type=self.block_type,
                )
                failure_reason = get_user_facing_exception_message(e)
            await self._invalidate_stale_output_on_failure(
                workflow_run_id, current_index, include_missing_value_guard=True
            )

            return await self.build_block_result(
                success=False,
                failure_reason=failure_reason,
                status=BlockStatus.failed,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
                error_codes=self.get_failure_error_codes() or None,
            )

    @abc.abstractmethod
    def get_all_parameters(
        self,
        workflow_run_id: str,
    ) -> list[PARAMETER_TYPE]:
        pass


def _should_skip_retry_on_anti_bot_detection(task: Task) -> bool:
    categories = task.failure_category
    if categories:
        return any(c.get("category") == "ANTI_BOT_DETECTION" for c in categories)

    if task.failure_reason:
        result = classify_from_failure_reason(task.failure_reason)
        if result and any(c.get("category") == "ANTI_BOT_DETECTION" for c in result):
            return True

    return False


class BaseTaskBlock(Block):
    task_type: str = TaskType.general
    url: str | None = None
    title: str = ""
    engine: RunEngine = RunEngine.skyvern_v1
    complete_criterion: str | None = None
    complete_criterion_is_untrusted: bool = False
    terminate_criterion: str | None = None
    navigation_goal: str | None = None
    data_extraction_goal: str | None = None
    data_schema: dict[str, Any] | list | str | None = None
    # error code to error description for the LLM
    error_code_mapping: dict[str, str] | None = None
    max_retries: int = 0
    max_steps_per_run: int | None = None
    parameters: list[PARAMETER_TYPE] = []
    complete_on_download: bool = False
    download_suffix: str | None = None
    totp_verification_url: str | None = None
    totp_identifier: str | None = None
    complete_verification: bool = True
    include_action_history_in_verification: bool = False
    download_timeout: float | None = None  # seconds
    include_extracted_text: bool = True
    _data_extraction_goal_is_prerendered: bool = PrivateAttr(default=False)

    TEMPLATABLE_FIELDS: ClassVar[frozenset[str]] = frozenset(
        {
            "complete_criterion",
            "data_extraction_goal",
            "data_schema",
            "download_suffix",
            "error_code_mapping",
            "navigation_goal",
            "terminate_criterion",
            "title",
            "totp_identifier",
            "totp_verification_url",
            "url",
        }
    )

    def mark_data_extraction_goal_prerendered(self) -> None:
        self._data_extraction_goal_is_prerendered = True

    # Blocks built at runtime for internal machinery the eligibility check never vetted (loop-value
    # extraction) must not be rerouted by the run-level engine A/B. Branch-condition extraction is
    # the deliberate exception: v3_ab_ineligibility_reason vets prompt-branch conditionals, so that
    # synthetic block follows the run's arm. Private so an internal experiment toggle stays out of
    # the published block schemas and out of stored workflow definitions.
    _exclude_from_engine_ab: bool = PrivateAttr(default=False)

    def resolve_engine(self, workflow_run_id: str | None) -> RunEngine:
        """The engine this block dispatches to, after the per-run A/B.

        Both the persisted workflow_run_blocks.engine and the execute_step dispatch read this, so
        the recorded engine cannot disagree with the one that ran. A block pinned to a non-default
        engine is honored as-authored, and a block the eligibility check never saw is left alone;
        neither is ever rerouted.
        """
        if (
            self.engine != RunEngine.skyvern_v1
            or self._exclude_from_engine_ab
            # Mirrors run_is_eligible_for_v3_ab: a block eligibility skipped as engine-inert must
            # not be labeled v3 here either, or its row claims an engine that never ran. It does not
            # re-check _task_block_supports_v3 because run-level eligibility already rejected the
            # whole run if any block failed it; loosening that predicate means revisiting this.
            or self.block_type in _ENGINE_INERT_BLOCK_TYPES
        ):
            return self.engine
        return workflow_block_engine_override(workflow_run_id) or self.engine

    def get_all_parameters(
        self,
        workflow_run_id: str,
    ) -> list[PARAMETER_TYPE]:
        parameters = self.parameters
        workflow_run_context = self.get_workflow_run_context(workflow_run_id)

        if self.url and workflow_run_context.has_parameter(self.url):
            if self.url not in [parameter.key for parameter in parameters]:
                parameters.append(workflow_run_context.get_parameter(self.url))

        return parameters

    def preflight_failure_reason(
        self, workflow_run_context: WorkflowRunContext, workflow_run: WorkflowRun
    ) -> str | None:
        """Precondition that makes the block unwinnable before any browser work happens."""
        return None

    def format_potential_template_parameters(self, workflow_run_context: WorkflowRunContext) -> None:
        self.title = self.render_templatable_field("title", self.title, workflow_run_context)

        if self.url:
            self.url = self.render_templatable_field("url", self.url, workflow_run_context)
            self.url = prepend_scheme_and_validate_url(self.url)

        if self.totp_identifier:
            self.totp_identifier = self.render_templatable_field(
                "totp_identifier", self.totp_identifier, workflow_run_context
            )

        if self.totp_verification_url:
            self.totp_verification_url = self.render_templatable_field(
                "totp_verification_url", self.totp_verification_url, workflow_run_context
            )
            self.totp_verification_url = prepend_scheme_and_validate_url(self.totp_verification_url)

        if self.download_suffix:
            self.download_suffix = self.render_templatable_field(
                "download_suffix", self.download_suffix, workflow_run_context
            )
            # encode the suffix to prevent invalid path style
            self.download_suffix = quote(string=self.download_suffix, safe="")
            LOG.info(
                "download_suffix_rendered",
                block_label=self.label,
                current_index=workflow_run_context.get_block_metadata(self.label).get("current_index"),
                download_suffix_fp=diagnostic_fingerprint(self.download_suffix),
            )

        if self.navigation_goal:
            self.navigation_goal = self.render_templatable_field(
                "navigation_goal", self.navigation_goal, workflow_run_context
            )

        if self.data_extraction_goal and not self._data_extraction_goal_is_prerendered:
            self.data_extraction_goal = self.render_templatable_field(
                "data_extraction_goal", self.data_extraction_goal, workflow_run_context
            )

        if isinstance(self.data_schema, str):
            self.data_schema = self.render_templatable_field("data_schema", self.data_schema, workflow_run_context)

        if self.complete_criterion:
            self.complete_criterion = self.render_templatable_field(
                "complete_criterion", self.complete_criterion, workflow_run_context
            )

        if self.terminate_criterion:
            self.terminate_criterion = self.render_templatable_field(
                "terminate_criterion", self.terminate_criterion, workflow_run_context
            )

        # Inherit workflow-level error_code_mapping; block-level entries override on key conflicts.
        workflow = getattr(workflow_run_context, "workflow", None)
        workflow_error_code_mapping: dict[str, str] | None = None
        if workflow is not None and workflow.workflow_definition is not None:
            workflow_error_code_mapping = workflow.workflow_definition.error_code_mapping

        if workflow_error_code_mapping or self.error_code_mapping:
            self.error_code_mapping = self._render_error_code_mapping(
                self.error_code_mapping,
                workflow_error_code_mapping,
                workflow_run_context,
                for_generated_code=False,
            )

        # Materialize the workflow-level workflow_system_prompt onto this block so
        # ForgeAgent.create_task can hand it off to the Task row verbatim.
        self._apply_workflow_system_prompt(workflow_run_context)

    @staticmethod
    async def get_task_order(workflow_run_id: str, current_retry: int) -> tuple[int, int]:
        """
        Returns the order and retry for the next task in the workflow run as a tuple.
        """
        last_task_for_workflow_run = await app.DATABASE.tasks.get_last_task_for_workflow_run(
            workflow_run_id=workflow_run_id
        )
        # If there is no previous task, the order will be 0 and the retry will be 0.
        if last_task_for_workflow_run is None:
            return 0, 0
        # If there is a previous task but the current retry is 0, the order will be the order of the last task + 1
        # and the retry will be 0.
        order = last_task_for_workflow_run.order or 0
        if current_retry == 0:
            return order + 1, 0
        # If there is a previous task and the current retry is not 0, the order will be the order of the last task
        # and the retry will be the retry of the last task + 1. (There is a validation that makes sure the retry
        # of the last task is equal to current_retry - 1) if it is not, we use last task retry + 1.
        retry = last_task_for_workflow_run.retry or 0
        if retry + 1 != current_retry:
            LOG.error(
                f"Last task for workflow run is retry number {last_task_for_workflow_run.retry}, "
                f"but current retry is {current_retry}. Could be race condition. Using last task retry + 1",
                workflow_run_id=workflow_run_id,
                last_task_id=last_task_for_workflow_run.task_id,
                last_task_retry=last_task_for_workflow_run.retry,
                current_retry=current_retry,
            )

        return order, retry + 1

    async def _handle_task_failure_with_error_detection(
        self,
        task: Task,
        step: Step,
        browser_state: BrowserState | None,
        failure_reason: str,
        organization_id: str,
    ) -> None:
        """
        Handle task failure by updating the task status and detecting user-defined errors.

        This helper method consolidates the error detection logic that was previously
        duplicated across multiple exception handlers in the execute method.
        """
        # Both fields below leave over the customer webhook, and neither downstream sanitizer covers
        # runtime-minted values: the task path does not redact at all, and the workflow-run
        # aggregation redacts against the static secrets dict only. Redact once here so the persist
        # and the detector see the same scrubbed string.
        run_secrets = (
            app.WORKFLOW_CONTEXT_MANAGER.get_secret_values_for_run(task.workflow_run_id)
            if app.WORKFLOW_CONTEXT_MANAGER.artifact_redaction_enabled(task.workflow_run_id)
            else app.WORKFLOW_CONTEXT_MANAGER.runtime_secret_values_for_artifacts()
        )
        if failure_reason and run_secrets:
            failure_reason = redact_secrets_from_text(failure_reason, run_secrets)

        await app.DATABASE.tasks.update_task(
            task.task_id,
            status=TaskStatus.failed,
            organization_id=organization_id,
            failure_reason=failure_reason,
        )
        # Detect user-defined errors if error_code_mapping is provided
        if self.error_code_mapping:
            try:
                detected_errors = await detect_user_defined_errors_for_task(
                    task=task,
                    step=step,
                    browser_state=browser_state,
                    failure_reason=failure_reason,
                )
                if detected_errors:
                    # The detector reads the page too, so redacting its input is not enough: a secret
                    # entered into the form can come back in its reasoning.
                    if run_secrets:
                        # model_copy skips the field validator, and redaction can LENGTHEN the
                        # string -- the placeholder is longer than a short code -- so re-apply the
                        # model's own bound rather than trusting the value that went in. No rstrip
                        # fallback: these reach bounded_reasoning, not the strict payload check that
                        # drops a row whose reasoning is not strip()-clean. A call site that DOES hit
                        # that check would need it.
                        detected_errors = [
                            error.model_copy(
                                update={
                                    "reasoning": redact_secrets_from_text(error.reasoning, run_secrets)[
                                        :ERROR_CODE_REASONING_MAX_LENGTH
                                    ]
                                }
                            )
                            for error in detected_errors
                        ]
                    # Only pass new errors — update_task() appends to existing errors
                    new_errors = [error.model_dump() for error in detected_errors]
                    await app.DATABASE.tasks.update_task(
                        task_id=task.task_id,
                        organization_id=organization_id,
                        errors=new_errors,
                    )
            except Exception:
                LOG.exception(
                    "Failed to detect or store user-defined errors during task failure",
                    task_id=task.task_id,
                )

    async def execute(
        self,
        workflow_run_id: str,
        workflow_run_block_id: str,
        organization_id: str | None = None,
        browser_session_id: str | None = None,
        **kwargs: dict,
    ) -> BlockResult:
        workflow_run_context = self.get_workflow_run_context(workflow_run_id)
        current_retry = 0
        # initial value for will_retry is True, so that the loop runs at least once
        will_retry = True
        current_running_task: Task | None = None
        workflow_run = await app.WORKFLOW_SERVICE.get_workflow_run(
            workflow_run_id=workflow_run_id,
            organization_id=organization_id,
        )

        # Scope downloaded files to this block only.
        block_context = skyvern_context.current()
        if block_context:
            await capture_block_download_baseline(block_context, organization_id or "", workflow_run_id, self.label)

        # Get workflow from context if available, otherwise query database
        workflow = workflow_run_context.workflow
        if workflow is None:
            workflow = await app.WORKFLOW_SERVICE.get_workflow_by_permanent_id(
                workflow_permanent_id=workflow_run.workflow_permanent_id,
            )
            # Cache the workflow back to context for future block executions
            workflow_run_context.set_workflow(workflow)

        preflight_failure_reason = self.preflight_failure_reason(workflow_run_context, workflow_run)
        if preflight_failure_reason:
            await self.record_output_parameter_value(
                workflow_run_context, workflow_run_id, {"failure_reason": preflight_failure_reason}
            )
            return await self.build_block_result(
                success=False,
                failure_reason=preflight_failure_reason,
                output_parameter_value=None,
                status=BlockStatus.failed,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )

        # if the task url is parameterized, we need to get the value from the workflow run context
        if self.url and workflow_run_context.has_parameter(self.url) and workflow_run_context.has_value(self.url):
            task_url_parameter_value = workflow_run_context.get_value(self.url)
            if task_url_parameter_value:
                LOG.info(
                    "Task URL is parameterized, using parameter value",
                    task_url_parameter_value=task_url_parameter_value,
                    task_url_parameter_key=self.url,
                )
                self.url = task_url_parameter_value
            else:
                # Absent optional parameter: fall back to the current page instead of
                # navigating to the literal parameter key.
                self.url = None

        if self.totp_identifier:
            if workflow_run_context.has_parameter(self.totp_identifier) and workflow_run_context.has_value(
                self.totp_identifier
            ):
                totp_identifier_parameter_value = workflow_run_context.get_value(self.totp_identifier)
                if totp_identifier_parameter_value:
                    self.totp_identifier = totp_identifier_parameter_value
                else:
                    # Absent optional parameter: disable OTP-to-email lookup instead of
                    # keeping the literal parameter key as the identifier.
                    self.totp_identifier = None
        else:
            for parameter in self.get_all_parameters(workflow_run_id):
                parameter_key = getattr(parameter, "key", None)
                if not parameter_key:
                    continue
                credential_totp_identifier = workflow_run_context.get_credential_totp_identifier(parameter_key)
                if credential_totp_identifier:
                    self.totp_identifier = credential_totp_identifier
                    break

        if self.download_suffix and workflow_run_context.has_parameter(self.download_suffix):
            download_suffix_parameter_value = workflow_run_context.get_value(self.download_suffix)
            if download_suffix_parameter_value:
                LOG.info(
                    "Download prefix is parameterized, using parameter value",
                    download_suffix_parameter_value=download_suffix_parameter_value,
                    download_suffix_parameter_key=self.download_suffix,
                )
                self.download_suffix = download_suffix_parameter_value
            else:
                # Absent optional parameter: run without a download suffix.
                self.download_suffix = None

        try:
            self.format_potential_template_parameters(workflow_run_context=workflow_run_context)
        except Exception as e:
            return await self._template_format_failure_result(
                e,
                f"Failed to format jinja template: {str(e)}",
                workflow_run_context,
                workflow_run_id,
                workflow_run_block_id,
                organization_id,
            )

        # SKY-8818: observability + wait_until override. Computed ONCE per block
        # execution — hoisted outside the retry loop so the Datadog signal counts
        # block runs, not retries, and `_navigate_wait_until` is a pure function of
        # self.block_type (which does not change between retries).
        warn_if_file_download_max_steps_low(self, workflow_run_id=workflow_run_id)
        _is_file_download = self.block_type == BlockType.FILE_DOWNLOAD
        _navigate_wait_until: Literal["load", "domcontentloaded", "commit"] = (
            "domcontentloaded" if _is_file_download else "load"
        )

        # TODO (kerem) we should always retry on terminated. We should make a distinction between retriable and
        # non-retryable terminations
        while will_retry:
            task_order, task_retry = await self.get_task_order(workflow_run_id, current_retry)
            is_first_task = task_order == 0
            task, step = await app.agent.create_task_and_step_from_block(
                task_block=self,
                workflow=workflow,
                workflow_run=workflow_run,
                workflow_run_context=workflow_run_context,
                task_order=task_order,
                task_retry=task_retry,
            )
            workflow_run_block = await app.DATABASE.observer.update_workflow_run_block(
                workflow_run_block_id=workflow_run_block_id,
                task_id=task.task_id,
                organization_id=organization_id,
            )
            current_running_task = task
            organization = await app.DATABASE.organizations.get_organization(
                organization_id=workflow_run.organization_id
            )
            if not organization:
                raise Exception(f"Organization is missing organization_id={workflow_run.organization_id}")

            browser_state: BrowserState | None = None
            if is_first_task:
                # the first task block will create the browser state and do the navigation
                try:
                    # SKY-8818: for file_download blocks, skip the browser factory's built-in
                    # goto (which uses wait_until='load' and stalls on slow subresources) and
                    # let the about:blank fallback below handle navigation with our override.
                    _bm_url = None if _is_file_download else self.url
                    browser_state = await app.BROWSER_MANAGER.get_or_create_for_workflow_run(
                        workflow_run=workflow_run,
                        url=_bm_url,
                        browser_session_id=browser_session_id,
                        browser_profile_id=workflow_run.browser_profile_id,
                    )
                    working_page = await browser_state.must_get_working_page()
                    # SKY-8818: for file_download we passed url=None above so the factory
                    # skipped its built-in goto. We must therefore navigate explicitly to
                    # self.url — not just when the page is about:blank, but whenever the
                    # working page is not already on the target URL (e.g. persistent
                    # browser sessions that carry state from a prior block).
                    if self.url:
                        _needs_navigation = working_page.url == "about:blank" or (
                            _is_file_download and working_page.url.rstrip("/") != self.url.rstrip("/")
                        )
                        if _needs_navigation:
                            await browser_state.navigate_to_url(
                                page=working_page,
                                url=self.url,
                                wait_until=_navigate_wait_until,
                            )

                    # When a browser profile is loaded, wait for the page to fully settle
                    # so that cookie-based authentication can redirect or restore the session
                    # BEFORE the agent starts interacting with the page.
                    if workflow_run.browser_profile_id:
                        applied_profile_id = browser_state.browser_artifacts.applied_browser_profile_id
                        if applied_profile_id != workflow_run.browser_profile_id and not browser_session_id:
                            LOG.warning(
                                "Stamped browser profile was not applied to this browser — continuing without saved state",
                                browser_profile_id=workflow_run.browser_profile_id,
                                applied_browser_profile_id=applied_profile_id,
                                workflow_run_id=workflow_run.workflow_run_id,
                            )
                        else:
                            # A persistent session loads its profile session-side, invisible to these
                            # artifacts — keep the settle wait so cookie redirects finish either way.
                            LOG.info(
                                "Browser profile loaded — waiting for page to settle before agent acts",
                                browser_profile_id=workflow_run.browser_profile_id,
                                applied_browser_profile_id=applied_profile_id,
                                workflow_run_id=workflow_run.workflow_run_id,
                            )
                            try:
                                await working_page.wait_for_load_state("networkidle", timeout=10000)
                            except Exception:
                                LOG.debug(
                                    "networkidle timeout after browser profile load (non-fatal)",
                                    workflow_run_id=workflow_run.workflow_run_id,
                                )

                except Exception as e:
                    if (
                        isinstance(e, FailedToNavigateToUrl)
                        and not isinstance(e, BlockedNavigationDestination)
                        and not is_egress_attributable_navigation_error(e.error_message)
                    ):
                        # The target site did not load. Site-caused, and already surfaced on the run
                        # through the failure_reason recorded below. Two classes stay at error
                        # because they are ours rather than the site's: BlockedNavigationDestination
                        # is the SSRF guard tripping, and EGRESS_ATTRIBUTABLE_NAV_ERRORS is our own
                        # egress failing, which get_or_create_page may recover from on a different
                        # proxy node. The block-execution handler above applies only the first of
                        # those two carve-outs; see the PR discussion.
                        LOG.warning(
                            "Failed to get browser state for first task",
                            task_id=task.task_id,
                            workflow_run_id=workflow_run_id,
                            url=e.url,
                            exc_info=True,
                        )
                    else:
                        LOG.exception(
                            "Failed to get browser state for first task",
                            task_id=task.task_id,
                            workflow_run_id=workflow_run_id,
                            url=getattr(e, "url", None),
                        )
                    await self._handle_task_failure_with_error_detection(
                        task=task,
                        step=step,
                        browser_state=browser_state,
                        failure_reason=str(e),
                        organization_id=workflow_run.organization_id,
                    )
                    raise e

                # Validate starter URL before downstream scraping on a blank page
                if not (self.url and self.url.strip()) and working_page.url in ("about:blank", "", ":"):
                    missing_url_exc = MissingStarterUrl(block_label=self.label)
                    LOG.warning(
                        "First browser block has no starter URL",
                        task_id=task.task_id,
                        workflow_run_id=workflow_run_id,
                        block_label=self.label,
                    )
                    await self._handle_task_failure_with_error_detection(
                        task=task,
                        step=step,
                        browser_state=browser_state,
                        failure_reason=str(missing_url_exc),
                        organization_id=workflow_run.organization_id,
                    )
                    raise missing_url_exc

                try:
                    # add screenshot artifact for the first task
                    screenshot = await browser_state.take_fullpage_screenshot()
                    if screenshot:
                        await app.ARTIFACT_MANAGER.create_workflow_run_block_artifact(
                            workflow_run_block=workflow_run_block,
                            artifact_type=ArtifactType.SCREENSHOT_LLM,
                            data=screenshot,
                        )
                except Exception:
                    LOG.warning(
                        "Failed to take screenshot for first task",
                        task_id=task.task_id,
                        workflow_run_id=workflow_run_id,
                        exc_info=True,
                    )
            else:
                # if not the first task block, need to navigate manually
                browser_state = app.BROWSER_MANAGER.get_for_workflow_run(workflow_run_id=workflow_run_id)
                if browser_state is None:
                    raise MissingBrowserState(
                        task_id=task.task_id,
                        workflow_run_id=workflow_run_id,
                        detected_at=datetime.now(UTC),
                        failure_reason="browser_state_registry_lookup_miss",
                    )

                working_page = await browser_state.must_get_working_page()

                if self.url:
                    LOG.info(
                        "Navigating to page",
                        url=self.url,
                        workflow_run_id=workflow_run_id,
                        task_id=task.task_id,
                        workflow_id=workflow.workflow_id,
                        organization_id=workflow_run.organization_id,
                        step_id=step.step_id,
                    )
                    try:
                        # SKY-8818: use the hoisted wait_until override so file_download
                        # pages with slow subresources can still resolve via domcontentloaded.
                        await browser_state.navigate_to_url(
                            page=working_page,
                            url=self.url,
                            wait_until=_navigate_wait_until,
                        )
                    except Exception as e:
                        await self._handle_task_failure_with_error_detection(
                            task=task,
                            step=step,
                            browser_state=browser_state,
                            failure_reason=str(e),
                            organization_id=workflow_run.organization_id,
                        )
                        raise e

            try:
                current_context = skyvern_context.ensure_context()
                current_context.task_id = task.task_id
                previous_complete_criterion_is_untrusted = current_context.complete_criterion_is_untrusted
                current_context.complete_criterion_is_untrusted = self.complete_criterion_is_untrusted
                close_browser_on_completion = browser_session_id is None and not workflow_run.browser_address
                await app.agent.execute_step(
                    organization=organization,
                    task=task,
                    step=step,
                    task_block=self,
                    browser_session_id=browser_session_id,
                    close_browser_on_completion=close_browser_on_completion,
                    complete_verification=self.complete_verification,
                    engine=self.resolve_engine(workflow_run.workflow_run_id),
                )
            except Exception as e:
                # Make sure the task is marked as failed in the database before raising the exception
                await self._handle_task_failure_with_error_detection(
                    task=task,
                    step=step,
                    browser_state=browser_state,
                    failure_reason=str(e),
                    organization_id=workflow_run.organization_id,
                )
                raise e
            finally:
                current_context.task_id = None
                current_context.complete_criterion_is_untrusted = previous_complete_criterion_is_untrusted

            # Check task status
            updated_task = await app.DATABASE.tasks.get_task(
                task_id=task.task_id, organization_id=workflow_run.organization_id
            )
            if not updated_task:
                raise TaskNotFound(task.task_id)
            if not updated_task.status.is_final():
                raise UnexpectedTaskStatus(task_id=updated_task.task_id, status=updated_task.status)
            current_running_task = updated_task

            block_status_mapping = TASK_TO_BLOCK_STATUS
            if updated_task.status == TaskStatus.completed or updated_task.status == TaskStatus.terminated:
                LOG.info(
                    "Task completed",
                    sampling=True,
                    task_id=updated_task.task_id,
                    task_status=updated_task.status,
                    workflow_run_id=workflow_run_id,
                    workflow_id=workflow.workflow_id,
                    organization_id=workflow_run.organization_id,
                )
                success = updated_task.status == TaskStatus.completed

                downloaded_files: list[FileInfo] = []
                try:
                    async with asyncio.timeout(GET_DOWNLOADED_FILES_TIMEOUT):
                        downloaded_files = await app.STORAGE.get_downloaded_files(
                            organization_id=workflow_run.organization_id,
                            run_id=current_context.run_id
                            if current_context and current_context.run_id
                            else workflow_run_id or updated_task.task_id,
                        )
                except asyncio.TimeoutError:
                    LOG.warning("Timeout getting downloaded files", task_id=updated_task.task_id)

                # SKY-7005: scope downloaded files to the current loop iteration
                downloaded_files = filter_downloaded_files_for_current_iteration(
                    downloaded_files,
                    current_context.loop_internal_state if current_context else None,
                )

                task_screenshot_artifacts = await app.WORKFLOW_SERVICE.get_recent_task_screenshot_artifacts(
                    organization_id=workflow_run.organization_id,
                    task_id=updated_task.task_id,
                )
                workflow_screenshot_artifacts = await app.WORKFLOW_SERVICE.get_recent_workflow_screenshot_artifacts(
                    workflow_run_id=workflow_run_id,
                    organization_id=workflow_run.organization_id,
                )

                task_output = TaskOutput.from_task(
                    updated_task,
                    downloaded_files,
                    task_screenshot_artifact_ids=[a.artifact_id for a in task_screenshot_artifacts],
                    workflow_screenshot_artifact_ids=[a.artifact_id for a in workflow_screenshot_artifacts],
                )
                output_parameter_value = task_output.model_dump()
                await self.record_output_parameter_value(workflow_run_context, workflow_run_id, output_parameter_value)
                return await self.build_block_result(
                    success=success,
                    failure_reason=(
                        updated_task.failure_reason
                        if success
                        else (
                            updated_task.failure_reason
                            or f"Task {updated_task.task_id} finished with status {updated_task.status}"
                        )
                    ),
                    output_parameter_value=output_parameter_value,
                    status=block_status_mapping[updated_task.status],
                    workflow_run_block_id=workflow_run_block_id,
                    organization_id=organization_id,
                )
            elif updated_task.status == TaskStatus.canceled:
                LOG.info(
                    "Task canceled, cancelling block",
                    task_id=updated_task.task_id,
                    task_status=updated_task.status,
                    workflow_run_id=workflow_run_id,
                    workflow_id=workflow.workflow_id,
                    organization_id=workflow_run.organization_id,
                )
                return await self.build_block_result(
                    success=False,
                    failure_reason=updated_task.failure_reason or f"Task {updated_task.task_id} was canceled",
                    output_parameter_value=None,
                    status=block_status_mapping[updated_task.status],
                    workflow_run_block_id=workflow_run_block_id,
                    organization_id=organization_id,
                )
            elif updated_task.status == TaskStatus.timed_out:
                LOG.info(
                    "Task timed out, making the block time out",
                    task_id=updated_task.task_id,
                    task_status=updated_task.status,
                    workflow_run_id=workflow_run_id,
                    workflow_id=workflow.workflow_id,
                    organization_id=workflow_run.organization_id,
                )
                return await self.build_block_result(
                    success=False,
                    failure_reason=updated_task.failure_reason or f"Task {updated_task.task_id} timed out",
                    output_parameter_value=None,
                    status=block_status_mapping[updated_task.status],
                    workflow_run_block_id=workflow_run_block_id,
                    organization_id=organization_id,
                )
            else:
                current_retry += 1
                will_retry = current_retry <= self.max_retries
                if will_retry and _should_skip_retry_on_anti_bot_detection(updated_task):
                    LOG.warning(
                        "Skipping retry - task failed due to anti-bot detection",
                        task_id=updated_task.task_id,
                        workflow_run_id=workflow_run_id,
                        workflow_id=workflow.workflow_id,
                        organization_id=workflow_run.organization_id,
                        current_retry=current_retry,
                        max_retries=self.max_retries,
                        failure_reason=updated_task.failure_reason,
                        failure_category=updated_task.failure_category,
                    )
                    will_retry = False
                retry_message = f", retrying task {current_retry}/{self.max_retries}" if will_retry else ""
                downloaded_files = []
                try:
                    async with asyncio.timeout(GET_DOWNLOADED_FILES_TIMEOUT):
                        downloaded_files = await app.STORAGE.get_downloaded_files(
                            organization_id=workflow_run.organization_id,
                            run_id=current_context.run_id
                            if current_context and current_context.run_id
                            else workflow_run_id or updated_task.task_id,
                        )

                except asyncio.TimeoutError:
                    LOG.warning("Timeout getting downloaded files", task_id=updated_task.task_id)

                # SKY-7005: scope downloaded files to the current loop iteration
                downloaded_files = filter_downloaded_files_for_current_iteration(
                    downloaded_files,
                    current_context.loop_internal_state if current_context else None,
                )

                task_screenshot_artifacts = await app.WORKFLOW_SERVICE.get_recent_task_screenshot_artifacts(
                    organization_id=workflow_run.organization_id,
                    task_id=updated_task.task_id,
                )
                workflow_screenshot_artifacts = await app.WORKFLOW_SERVICE.get_recent_workflow_screenshot_artifacts(
                    workflow_run_id=workflow_run_id,
                    organization_id=workflow_run.organization_id,
                )

                task_output = TaskOutput.from_task(
                    updated_task,
                    downloaded_files,
                    task_screenshot_artifact_ids=[a.artifact_id for a in task_screenshot_artifacts],
                    workflow_screenshot_artifact_ids=[a.artifact_id for a in workflow_screenshot_artifacts],
                )
                LOG.warning(
                    f"Task failed with status {updated_task.status}{retry_message}",
                    task_id=updated_task.task_id,
                    task_status=updated_task.status,
                    workflow_run_id=workflow_run_id,
                    workflow_id=workflow.workflow_id,
                    organization_id=workflow_run.organization_id,
                    current_retry=current_retry,
                    max_retries=self.max_retries,
                    task_output=task_output.model_dump_json(),
                )
                if not will_retry:
                    output_parameter_value = task_output.model_dump()
                    await self.record_output_parameter_value(
                        workflow_run_context, workflow_run_id, output_parameter_value
                    )
                    return await self.build_block_result(
                        success=False,
                        failure_reason=(
                            updated_task.failure_reason
                            or f"Task {updated_task.task_id} failed with status {updated_task.status}"
                        ),
                        output_parameter_value=output_parameter_value,
                        status=block_status_mapping[updated_task.status],
                        workflow_run_block_id=workflow_run_block_id,
                        organization_id=organization_id,
                    )

        await self.record_output_parameter_value(workflow_run_context, workflow_run_id)
        return await self.build_block_result(
            success=False,
            status=BlockStatus.failed,
            failure_reason=(
                (current_running_task.failure_reason or f"Task {current_running_task.task_id} failed")
                if current_running_task
                else "Task failed (no task reference available)"
            ),
            workflow_run_block_id=workflow_run_block_id,
            organization_id=organization_id,
        )


class TaskBlock(BaseTaskBlock):
    # There is a mypy bug with Literal. Without the type: ignore, mypy will raise an error:
    # Parameter 1 of Literal[...] cannot be of type "Any"
    block_type: Literal[BlockType.TASK] = BlockType.TASK  # type: ignore


class LoopBlockExecutedResult(BaseModel):
    outputs_with_loop_values: list[list[dict[str, Any]]]
    block_outputs: list[BlockResult]
    last_block: BlockTypeVar | None
    # True only when the loop exhausted all iterations naturally (for-loop) or the
    # condition turned false (while-loop). False on every early-return path
    # (cancel, structural error, max iterations, body failure with no swallow flag).
    natural_completion: bool = False

    def is_canceled(self) -> bool:
        return len(self.block_outputs) > 0 and self.block_outputs[-1].status == BlockStatus.canceled

    def is_synthetic_loop_failure(self) -> bool:
        """Last appended result is a loop-structural / safety-limit failure, not a child."""
        return bool(self.block_outputs) and self.block_outputs[-1].is_synthetic_loop_failure

    def is_completed(self) -> bool:
        if len(self.block_outputs) == 0:
            return False

        if self.last_block is None:
            return False

        if self.is_canceled():
            return False

        last_ouput = self.block_outputs[-1]
        if last_ouput.success:
            return True

        # Swallow flags apply only on natural-completion paths whose last result
        # is a real child failure; structural/safety synthetics must propagate.
        if not self.natural_completion or self.is_synthetic_loop_failure():
            return False

        if self.last_block.continue_on_failure:
            return True

        if self.last_block.next_loop_on_failure:
            return True

        return False

    def is_terminated(self) -> bool:
        return len(self.block_outputs) > 0 and self.block_outputs[-1].status == BlockStatus.terminated

    def get_failure_reason(self) -> str | None:
        if self.is_completed():
            return None

        if self.is_canceled():
            return f"Block({self.last_block.label if self.last_block else ''}) with type {self.last_block.block_type if self.last_block else ''} was canceled, canceling for loop"

        return self.block_outputs[-1].failure_reason if len(self.block_outputs) > 0 else "No block has been executed"

    def resolve_status(self, parent_next_loop_on_failure: bool) -> tuple[BlockStatus, bool, str | None]:
        """Decide the loop block's overall status, success flag, and failure_reason.

        ``parent_next_loop_on_failure`` is the parent loop's swallow flag; when
        set, body failures swallowed mid-loop must not re-surface as the loop's
        overall status. Synthetic safety/structural failures still propagate.
        """
        parent_swallow = (
            parent_next_loop_on_failure
            and self.natural_completion
            and not self.is_canceled()
            and not self.is_synthetic_loop_failure()
        )

        if self.is_canceled():
            block_status = BlockStatus.canceled
            success = False
        elif self.is_completed() or parent_swallow:
            block_status = BlockStatus.completed
            success = True
        elif self.is_terminated():
            block_status = BlockStatus.terminated
            success = False
        else:
            block_status = BlockStatus.failed
            success = False

        failure_reason = None if success else self.get_failure_reason()
        return block_status, success, failure_reason


def compute_conditional_scopes(
    label_to_block: dict[str, Any],
    default_next_map: dict[str, str | None],
) -> dict[str, str]:
    """Map each block label to the conditional block label whose scope it belongs to.

    For each conditional block, trace each branch's chain of blocks via
    ``default_next_map``.  Labels that appear in **all** branch chains are
    considered merge-point blocks (i.e. they come *after* the conditional
    reconverges) and are **not** scoped.  Labels that appear in fewer chains
    than the total number of branches **are** inside the conditional.

    Inner conditionals are themselves scoped to an outer conditional, but
    their *own* branch targets are handled by a recursive application of
    the same logic (inner wins via the ``if lbl not in scopes`` guard).
    """
    scopes: dict[str, str] = {}

    conditional_labels = [lbl for lbl, blk in label_to_block.items() if blk.block_type == BlockType.CONDITIONAL]

    for cond_label in conditional_labels:
        cond_block = label_to_block[cond_label]
        branch_targets: list[str | None] = [branch.next_block_label for branch in cond_block.ordered_branches]
        # Deduplicate while preserving order – two branches may point to the same target
        seen_targets: set[str | None] = set()
        unique_targets: list[str | None] = []
        for t in branch_targets:
            if t not in seen_targets:
                seen_targets.add(t)
                unique_targets.append(t)

        num_branches = len(unique_targets)
        if num_branches == 0:
            continue

        # For each unique branch target, trace the chain via default_next_map.
        # Stop at other conditional blocks (they handle their own branches).
        chain_sets: list[list[str]] = []
        for target in unique_targets:
            chain: list[str] = []
            cur = target
            while cur and cur in label_to_block:
                chain.append(cur)
                # Stop tracing when we hit another conditional – it owns its own sub-tree
                if label_to_block[cur].block_type == BlockType.CONDITIONAL:
                    break
                cur = default_next_map.get(cur)
            chain_sets.append(chain)

        # Count how many branch chains each label appears in
        label_count: dict[str, int] = {}
        for chain in chain_sets:
            for lbl in chain:
                label_count[lbl] = label_count.get(lbl, 0) + 1

        # Labels appearing in ALL branches are merge points (after the conditional).
        # Labels appearing in fewer branches are inside the conditional.
        for chain in chain_sets:
            for lbl in chain:
                if label_count[lbl] >= num_branches:
                    # This is a merge point – stop scoping further along this chain
                    break
                if lbl not in scopes:
                    scopes[lbl] = cond_label

    return scopes


async def _execute_parameter_observing_block_safe(
    self: Block,
    workflow_run_id: str,
    parent_workflow_run_block_id: str | None = None,
    organization_id: str | None = None,
    browser_session_id: str | None = None,
    current_value: str | None = None,
    current_index: int | None = None,
    **kwargs: dict,
) -> BlockResult:
    propagated_error: BaseException
    try:
        # The inherited trace records exceptions before this parameter-aware boundary can scrub them.
        return await Block.execute_safe.__wrapped__(
            self,
            workflow_run_id,
            parent_workflow_run_block_id,
            organization_id,
            browser_session_id,
            current_value,
            current_index,
            **kwargs,
        )
    except BaseException as exc:
        propagated_error = (
            exc.with_traceback(None)
            if app.AGENT_FUNCTION.prepare_codeblock_control_flow_exception(exc)
            else RuntimeError()
        )
        del self, workflow_run_id, parent_workflow_run_block_id, organization_id, browser_session_id
        del current_value, current_index, kwargs, exc
    raise propagated_error from None


class ForLoopBlock(Block):
    # There is a mypy bug with Literal. Without the type: ignore, mypy will raise an error:
    # Parameter 1 of Literal[...] cannot be of type "Any"
    block_type: Literal[BlockType.FOR_LOOP] = BlockType.FOR_LOOP  # type: ignore
    execute_safe = _execute_parameter_observing_block_safe
    TEMPLATABLE_FIELDS: ClassVar[frozenset[str]] = frozenset({"data_schema", "loop_variable_reference"})

    loop_blocks: list[BlockTypeVar]
    loop_over: PARAMETER_TYPE | None = None
    loop_variable_reference: str | None = None
    complete_if_empty: bool = False
    # Note: intentionally excludes `list` (unlike BaseTaskBlock.data_schema) because a list schema
    # does not describe the shape of individual loop items -- only dict schemas are meaningful here.
    data_schema: dict[str, Any] | str | None = None

    def get_all_parameters(
        self,
        workflow_run_id: str,
    ) -> list[PARAMETER_TYPE]:
        parameters = set()
        if self.loop_over is not None:
            parameters.add(self.loop_over)

        for loop_block in self.loop_blocks:
            for parameter in loop_block.get_all_parameters(workflow_run_id):
                parameters.add(parameter)
        return list(parameters)

    def get_loop_block_context_parameters(self, workflow_run_id: str, loop_data: Any) -> list[ContextParameter]:
        context_parameters = []

        for loop_block in self.loop_blocks:
            # todo: handle the case where the loop_block is a ForLoopBlock

            all_parameters = loop_block.get_all_parameters(workflow_run_id)
            for parameter in all_parameters:
                if isinstance(parameter, ContextParameter):
                    context_parameters.append(parameter)

        if self.loop_over is None:
            return context_parameters

        for context_parameter in context_parameters:
            if context_parameter.source.key != self.loop_over.key:
                continue
            # If the loop_data is a dict, we need to check if the key exists in the loop_data
            if isinstance(loop_data, dict):
                if context_parameter.key in loop_data:
                    context_parameter.value = loop_data[context_parameter.key]
                else:
                    raise ContextParameterValueNotFound(
                        parameter_key=context_parameter.key,
                        existing_keys=list(loop_data.keys()),
                        workflow_run_id=workflow_run_id,
                    )
            else:
                # If the loop_data is a list, we can directly assign the loop_data to the context_parameter value
                context_parameter.value = loop_data

        return context_parameters

    async def get_values_from_loop_variable_reference(
        self,
        workflow_run_context: WorkflowRunContext,
        workflow_run_id: str,
        workflow_run_block_id: str,
        organization_id: str | None = None,
    ) -> list[Any]:
        propagated_error: BaseException
        try:
            return await self._get_values_from_loop_variable_reference(
                workflow_run_context,
                workflow_run_id,
                workflow_run_block_id,
                organization_id,
            )
        except BaseException as exc:
            if app.AGENT_FUNCTION.prepare_codeblock_control_flow_exception(exc):
                propagated_error = exc.with_traceback(None)
            else:
                propagated_error = FailedToFormatJinjaStyleParameter("loop input", "Loop input could not be resolved.")
            del self, workflow_run_context, workflow_run_id, workflow_run_block_id, organization_id, exc
        raise propagated_error from None

    async def _get_values_from_loop_variable_reference(
        self,
        workflow_run_context: WorkflowRunContext,
        workflow_run_id: str,
        workflow_run_block_id: str,
        organization_id: str | None = None,
    ) -> list[Any]:
        parameter_value = None
        if self.loop_variable_reference:
            LOG.debug("Processing loop variable reference")

            # Check if this looks like a parameter path (contains dots and/or _output)
            is_likely_parameter_path = "extracted_information." in self.loop_variable_reference

            # Try parsing as Jinja template
            parameter_value = self.try_parse_jinja_template(workflow_run_context)

            if parameter_value is None and not is_likely_parameter_path:
                try:
                    # Create and execute extraction block using the current block's workflow_id
                    extraction_block = self._create_initial_extraction_block(
                        self.loop_variable_reference, workflow_run_context=workflow_run_context
                    )

                    LOG.info("Processing natural language loop input")

                    extraction_result = await extraction_block.execute(
                        workflow_run_id=workflow_run_id,
                        workflow_run_block_id=workflow_run_block_id,
                        organization_id=organization_id,
                    )

                    if not extraction_result.success:
                        LOG.error("Extraction block failed")
                        raise ValueError("Extraction block failed")

                    LOG.debug("Extraction block succeeded")

                    # Store the extraction result in the workflow context
                    await extraction_block.record_output_parameter_value(
                        workflow_run_context=workflow_run_context,
                        workflow_run_id=workflow_run_id,
                        value=extraction_result.output_parameter_value,
                    )

                    # Get the extracted information
                    if not isinstance(extraction_result.output_parameter_value, dict):
                        LOG.error("Extraction result output_parameter_value is not a dict")
                        raise ValueError("Extraction result output_parameter_value is not a dictionary")

                    if "extracted_information" not in extraction_result.output_parameter_value:
                        LOG.error("Extraction result missing extracted_information key")
                        raise ValueError("Extraction result missing extracted_information key")

                    extracted_info = extraction_result.output_parameter_value["extracted_information"]

                    # Handle different possible structures of extracted_info
                    if isinstance(extracted_info, list):
                        # If it's a list, take the first element
                        if len(extracted_info) > 0:
                            extracted_info = extracted_info[0]
                        else:
                            LOG.error("Extracted information list is empty")
                            raise ValueError("Extracted information list is empty")

                    # At this point, extracted_info should be a dict
                    if not isinstance(extracted_info, dict):
                        LOG.error("Invalid extraction result structure - not a dict")
                        raise ValueError("Extraction result is not a dictionary")

                    # Extract the loop values
                    loop_values = extracted_info.get("loop_values", [])

                    if not loop_values:
                        LOG.error("No loop values found in extraction result")
                        raise ValueError("No loop values found in extraction result")

                    LOG.info("Extracted loop values", count=len(loop_values))

                    # Update the loop variable reference to point to the extracted loop values
                    # We'll use a temporary key that we can reference
                    temp_key = f"extracted_loop_values_{generate_random_string()}"
                    workflow_run_context.set_value(temp_key, loop_values)
                    self.loop_variable_reference = temp_key

                    # Now try parsing again with the updated reference
                    parameter_value = self.try_parse_jinja_template(workflow_run_context)

                except Exception:
                    LOG.error("Failed to process natural language loop input")
                    raise FailedToFormatJinjaStyleParameter("loop input", "Loop input could not be resolved.")

            if parameter_value is None:
                # Fall back to the original Jinja template approach
                value_template = f"{{{{ {self.loop_variable_reference.strip(' {}')} | tojson }}}}"
                try:
                    value_json = self.render_templatable_field(
                        "loop_variable_reference", value_template, workflow_run_context
                    )
                except Exception:
                    raise FailedToFormatJinjaStyleParameter("loop input", "Loop input could not be resolved.")
                parameter_value = json.loads(value_json)

        if isinstance(parameter_value, list):
            return parameter_value
        else:
            return [parameter_value]

    async def get_loop_over_parameter_values(
        self,
        workflow_run_context: WorkflowRunContext,
        workflow_run_id: str,
        workflow_run_block_id: str,
        organization_id: str | None = None,
    ) -> list[Any]:
        # parse the value from self.loop_variable_reference and then from self.loop_over
        if self.loop_variable_reference:
            return await self.get_values_from_loop_variable_reference(
                workflow_run_context,
                workflow_run_id,
                workflow_run_block_id,
                organization_id,
            )
        elif self.loop_over is not None:
            if isinstance(self.loop_over, WorkflowParameter):
                parameter_value = workflow_run_context.get_value(self.loop_over.key)
            elif isinstance(self.loop_over, OutputParameter):
                # If the output parameter is for a TaskBlock, it will be a TaskOutput object. We need to extract the
                # value from the TaskOutput object's extracted_information field.
                output_parameter_value = workflow_run_context.get_value(self.loop_over.key)
                if isinstance(output_parameter_value, dict) and "extracted_information" in output_parameter_value:
                    parameter_value = output_parameter_value["extracted_information"]
                else:
                    parameter_value = output_parameter_value
            elif isinstance(self.loop_over, ContextParameter):
                parameter_value = self.loop_over.value
                if not parameter_value:
                    source_parameter_value = workflow_run_context.get_value(self.loop_over.source.key)
                    if isinstance(source_parameter_value, dict):
                        if "extracted_information" in source_parameter_value:
                            parameter_value = source_parameter_value["extracted_information"].get(self.loop_over.key)
                        else:
                            parameter_value = source_parameter_value.get(self.loop_over.key)
                    else:
                        raise ValueError("ContextParameter source value should be a dict")
            else:
                raise NotImplementedError()

        else:
            if self.complete_if_empty:
                return []
            else:
                raise NoIterableValueFound()

        if isinstance(parameter_value, list):
            return parameter_value
        else:
            # TODO (kerem): Should we raise an error here?
            return [parameter_value]

    def try_parse_jinja_template(self, workflow_run_context: WorkflowRunContext) -> Any | None:
        """Try to parse the loop variable reference as a Jinja template."""
        try:
            # Try the exact reference first
            try:
                if self.loop_variable_reference is None:
                    return None
                value_template = f"{{{{ {self.loop_variable_reference.strip(' {}')} | tojson }}}}"
                value_json = self.render_templatable_field(
                    "loop_variable_reference", value_template, workflow_run_context
                )
                parameter_value = json.loads(value_json)
                if parameter_value is not None:
                    return parameter_value
            except Exception:
                pass

            # If that fails, try common access patterns for extraction results
            if self.loop_variable_reference is None:
                return None
            access_patterns = [
                f"{self.loop_variable_reference}.extracted_information",
                f"{self.loop_variable_reference}.extracted_information.results",
                f"{self.loop_variable_reference}.results",
            ]

            for pattern in access_patterns:
                try:
                    value_template = f"{{{{ {pattern.strip(' {}')} | tojson }}}}"
                    value_json = self.render_templatable_field(
                        "loop_variable_reference", value_template, workflow_run_context
                    )
                    parameter_value = json.loads(value_json)
                    if parameter_value is not None:
                        return parameter_value
                except Exception:
                    continue

            return None
        except Exception:
            return None

    def _create_initial_extraction_block(
        self,
        natural_language_prompt: str,
        workflow_run_context: WorkflowRunContext | None = None,
    ) -> ExtractionBlock:
        """Create an extraction block to process natural language input."""

        # Determine the items schema for loop_values
        items_schema: dict[str, Any] | None = None
        if self.data_schema is not None:
            if isinstance(self.data_schema, dict):
                items_schema = self.data_schema
            elif isinstance(self.data_schema, str):
                # Interpolate Jinja templates before parsing, matching how BaseTaskBlock.setup_block_v2
                # handles data_schema strings (see line 652-654)
                schema_str = self.data_schema
                if workflow_run_context is not None:
                    schema_str = self.render_templatable_field("data_schema", schema_str, workflow_run_context)
                try:
                    parsed = json.loads(schema_str)
                    if isinstance(parsed, dict):
                        items_schema = parsed
                    else:
                        LOG.warning(
                            "Parsed data_schema is not a dict, falling back to default string schema",
                            block_label=self.label,
                            data_schema=self.data_schema,
                        )
                except (json.JSONDecodeError, TypeError):
                    LOG.warning(
                        "Failed to parse data_schema string, falling back to default string schema",
                        block_label=self.label,
                        data_schema=self.data_schema,
                    )

        if items_schema is not None:
            # User provided a custom schema — each loop iteration will produce a structured object
            data_schema: dict[str, Any] = {
                "type": "object",
                "properties": {
                    "loop_values": {
                        "type": "array",
                        "description": "Array of structured values to iterate over, matching the provided schema.",
                        "items": items_schema,
                    }
                },
            }
        else:
            # Default: extract simple string array
            data_schema = {
                "type": "object",
                "properties": {
                    "loop_values": {
                        "type": "array",
                        "description": "Array of values to iterate over. Each value should be the primary data needed for the loop blocks.",
                        "items": {
                            "type": "string",
                            "description": "The primary value to be used in the loop iteration (e.g., URL, text, identifier, etc.)",
                        },
                    }
                },
            }

        # Create extraction goal that includes the natural language prompt
        extraction_goal = prompt_engine.load_prompt(
            "extraction_prompt_for_nat_language_loops", natural_language_prompt=natural_language_prompt
        )

        # Create a temporary output parameter using the current block's workflow_id

        output_param = OutputParameter(
            output_parameter_id=str(uuid.uuid4()),
            key=f"natural_lang_extraction_{generate_random_string()}",
            workflow_id=self.output_parameter.workflow_id,
            created_at=datetime.now(),
            modified_at=datetime.now(),
            parameter_type=ParameterType.OUTPUT,
            description="Natural language extraction result",
        )

        extraction_block = ExtractionBlock(
            label=f"natural_lang_extraction_{generate_random_string()}",
            data_extraction_goal=extraction_goal,
            data_schema=data_schema,
            output_parameter=output_param,
        )
        extraction_block._exclude_from_engine_ab = True
        return extraction_block

    def _build_loop_graph(
        self,
        blocks: list[BlockTypeVar],
        skip_sequential_defaulting: bool = False,
    ) -> tuple[str, dict[str, BlockTypeVar], dict[str, str | None]]:
        label_to_block: dict[str, BlockTypeVar] = {}
        default_next_map: dict[str, str | None] = {}

        for block in blocks:
            if block.label in label_to_block:
                raise InvalidWorkflowDefinition(f"Duplicate block label detected in loop: {block.label}")
            label_to_block[block.label] = block
            default_next_map[block.label] = block.next_block_label

        if not skip_sequential_defaulting:
            has_conditional_blocks = any(block.block_type == BlockType.CONDITIONAL for block in blocks)
            if not has_conditional_blocks:
                for idx, block in enumerate(blocks[:-1]):
                    if default_next_map.get(block.label) is None:
                        default_next_map[block.label] = blocks[idx + 1].label

        # SKY-8571: connect conditional branch terminals to the conditional's merge-point successor.
        resolve_conditional_merge_edges(blocks, label_to_block, default_next_map)

        adjacency: dict[str, set[str]] = {label: set() for label in label_to_block}
        incoming: dict[str, int] = {label: 0 for label in label_to_block}

        def _add_edge(source: str, target: str | None) -> None:
            if not target:
                return
            if target not in label_to_block:
                raise InvalidWorkflowDefinition(
                    f"Block {source} references unknown next_block_label {target} inside loop {self.label}"
                )
            # Allow multiple branches of a conditional to point to the same target
            # without double-counting the incoming edge.
            if target not in adjacency[source]:
                adjacency[source].add(target)
                incoming[target] += 1

        for label, block in label_to_block.items():
            if block.block_type == BlockType.CONDITIONAL:
                for branch in block.ordered_branches:
                    _add_edge(label, branch.next_block_label)
            else:
                _add_edge(label, default_next_map.get(label))

        roots = [label for label, count in incoming.items() if count == 0]
        if not roots:
            raise InvalidWorkflowDefinition(
                f"Circular reference detected inside loop {self.label}: every block is the target of another"
                " block's next_block_label, so there is no starting block."
                " At least one block must not be the target of any next_block_label or branch condition."
            )
        if len(roots) > 1:
            raise InvalidWorkflowDefinition(
                f"Disconnected blocks detected inside loop {self.label}: blocks"
                f" ({', '.join(sorted(roots))}) are not reachable from any other block."
                " Every block must be reachable from the first block through next_block_label or"
                " conditional branch references."
                " Either connect them by setting another block's next_block_label to point to them, or remove them."
            )

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
                f"Circular reference detected inside loop {self.label}: some blocks form a loop through their"
                " next_block_label references, causing an infinite cycle."
                " Ensure that following next_block_label from any block eventually reaches a block"
                " with next_block_label set to null."
            )

        return roots[0], label_to_block, default_next_map

    def validate_loop_blocks(self) -> None:
        """Validate the loop_blocks graph for cycles, orphans, and dangling references.

        Skips sequential defaulting so that disconnected subgraphs are detected.
        Also recursively validates any nested loop block children.
        Raises InvalidWorkflowDefinition (422) on validation failure.
        """
        if not self.loop_blocks:
            return
        self._build_loop_graph(self.loop_blocks, skip_sequential_defaulting=True)
        for block in self.loop_blocks:
            if isinstance(block, (ForLoopBlock, WhileLoopBlock)):
                block.validate_loop_blocks()

    async def _persist_partial_loop_output(
        self,
        workflow_run_id: str,
        outputs_with_loop_values: list[list[dict[str, Any]]],
        loop_idx: int,
    ) -> None:
        """Persist partial for-loop output to DB so data survives Temporal
        activity timeouts. The timeout handler runs on a different node and
        reads from DB — without this, accumulated iteration data is lost when
        the loop is killed mid-execution.

        Uses the DB UPSERT directly instead of record_output_parameter_value
        to avoid re-registering context parameters and emitting spurious
        'already has a registered value' warnings on every call.

        On the normal iteration path, this is called every
        PERSIST_LOOP_OUTPUT_INTERVAL iterations and on the final iteration
        to balance durability vs DB load. Early-return paths (failure,
        cancellation) always persist since they are terminal."""
        if not self.output_parameter:
            return
        _maybe_truncate_loop_outputs(
            outputs_with_loop_values,
            workflow_run_id=workflow_run_id,
            output_parameter_id=self.output_parameter.output_parameter_id,
        )
        try:
            await app.DATABASE.workflow_runs.create_or_update_workflow_run_output_parameter(
                workflow_run_id=workflow_run_id,
                output_parameter_id=self.output_parameter.output_parameter_id,
                value=outputs_with_loop_values,
            )
        except Exception:
            LOG.warning(
                "Failed to incrementally persist for-loop output",
                workflow_run_id=workflow_run_id,
                output_parameter_id=self.output_parameter.output_parameter_id,
                loop_idx=loop_idx,
            )

    async def _get_loop_browser_state(
        self,
        workflow_run_id: str,
        organization_id: str | None,
        browser_session_id: str | None,
    ) -> BrowserState | None:
        if browser_session_id:
            return await app.PERSISTENT_SESSIONS_MANAGER.get_browser_state(browser_session_id, organization_id)
        return app.BROWSER_MANAGER.get_for_workflow_run(workflow_run_id)

    async def _snapshot_loop_baseline_pages(
        self,
        workflow_run_id: str,
        organization_id: str | None,
        browser_session_id: str | None,
    ) -> set[Page] | None:
        """Pre-loop tabs to preserve, or None when they could not be determined.

        None and an empty set must stay distinct: closing everything opened after an empty
        baseline closes every tab, so a failed snapshot has to suppress the reset entirely.
        """
        if not await planner_levers.reset_browser_tabs_between_loop_iterations(organization_id):
            return None
        try:
            browser_state = await self._get_loop_browser_state(workflow_run_id, organization_id, browser_session_id)
            if isinstance(browser_state, RealBrowserState):
                return set(browser_state.open_pages())
        except Exception:
            LOG.warning(
                "Failed to snapshot baseline browser tabs for loop",
                workflow_run_id=workflow_run_id,
            )
        return None

    async def _reset_browser_tabs_for_iteration(
        self,
        workflow_run_id: str,
        organization_id: str | None,
        browser_session_id: str | None,
        baseline_pages: set[Page] | None,
    ) -> None:
        if baseline_pages is None or not await planner_levers.reset_browser_tabs_between_loop_iterations(
            organization_id
        ):
            return
        try:
            browser_state = await self._get_loop_browser_state(workflow_run_id, organization_id, browser_session_id)
            if isinstance(browser_state, RealBrowserState):
                await browser_state.close_pages_opened_after(baseline_pages)
        except Exception:
            LOG.warning(
                "Failed to reset browser tabs between loop iterations",
                workflow_run_id=workflow_run_id,
            )

    async def execute_loop_helper(
        self,
        workflow_run_id: str,
        workflow_run_block_id: str,
        workflow_run_context: WorkflowRunContext,
        loop_over_values: list[Any],
        organization_id: str | None = None,
        browser_session_id: str | None = None,
    ) -> LoopBlockExecutedResult:
        outputs_with_loop_values: list[list[dict[str, Any]]] = []
        block_outputs: list[BlockResult] = []
        current_block: BlockTypeVar | None = None

        start_label, label_to_block, default_next_map = self._build_loop_graph(self.loop_blocks)
        conditional_scopes = compute_conditional_scopes(label_to_block, default_next_map)

        loop_baseline_pages = await self._snapshot_loop_baseline_pages(
            workflow_run_id, organization_id, browser_session_id
        )

        for loop_idx, loop_over_value in enumerate(loop_over_values):
            # Check max_iterations limit
            if loop_idx >= DEFAULT_MAX_LOOP_ITERATIONS:
                LOG.info(
                    f"ForLoopBlock Reached max_iterations limit ({DEFAULT_MAX_LOOP_ITERATIONS}), stopping loop",
                    workflow_run_id=workflow_run_id,
                    loop_idx=loop_idx,
                    max_iterations=DEFAULT_MAX_LOOP_ITERATIONS,
                )
                failure_block_result = await self.build_block_result(
                    success=False,
                    status=BlockStatus.failed,
                    failure_reason=f"Reached max_loop_iterations limit of {DEFAULT_MAX_LOOP_ITERATIONS}",
                    workflow_run_block_id=workflow_run_block_id,
                    organization_id=organization_id,
                    is_synthetic_loop_failure=True,
                )
                block_outputs.append(failure_block_result)
                await self._persist_partial_loop_output(workflow_run_id, outputs_with_loop_values, loop_idx)
                return LoopBlockExecutedResult(
                    outputs_with_loop_values=outputs_with_loop_values,
                    block_outputs=block_outputs,
                    last_block=current_block,
                )
            LOG.info("Starting loop iteration", loop_idx=loop_idx)

            if loop_idx > 0:
                await self._reset_browser_tabs_for_iteration(
                    workflow_run_id, organization_id, browser_session_id, loop_baseline_pages
                )

            # Capture baseline downloaded files for per-iteration scoping (SKY-7005).
            # Download-producing child blocks re-capture their own per-block baseline
            # at start; this seed only covers filtering before the first such capture.
            loop_context = skyvern_context.current()
            if loop_context:
                downloaded_file_sigs_before: list[tuple[str | None, str | None, str | None]] = []
                baseline_timed_out = False
                try:
                    async with asyncio.timeout(GET_DOWNLOADED_FILES_TIMEOUT):
                        downloaded_file_sigs_before = [
                            to_downloaded_file_signature(fi)
                            for fi in await app.STORAGE.get_downloaded_files(
                                organization_id=organization_id or "",
                                run_id=resolve_run_download_id(loop_context, fallback_run_id=workflow_run_id),
                            )
                        ]
                except asyncio.TimeoutError:
                    baseline_timed_out = True
                    LOG.warning(
                        "Timeout getting baseline downloaded files for loop iteration",
                        workflow_run_id=workflow_run_id,
                        loop_idx=loop_idx,
                    )
                if baseline_timed_out:
                    loop_context.loop_internal_state = None
                else:
                    loop_context.loop_internal_state = {
                        DOWNLOADED_FILE_SIGS_KEY: downloaded_file_sigs_before,
                    }

            # context parameter has been deprecated. However, it's still used by task v2 - we should migrate away from it.
            context_parameters_with_value = self.get_loop_block_context_parameters(workflow_run_id, loop_over_value)
            for context_parameter in context_parameters_with_value:
                workflow_run_context.set_value(context_parameter.key, context_parameter.value)

            each_loop_output_values: list[dict[str, Any]] = []

            iteration_step_count = 0
            LOG.debug(
                "ForLoopBlock starting iteration",
                workflow_run_id=workflow_run_id,
                loop_idx=loop_idx,
                max_steps_per_iteration=DEFAULT_MAX_STEPS_PER_ITERATION,
            )

            block_idx = 0
            current_label: str | None = start_label
            conditional_wrb_ids: dict[str, str] = {}
            while current_label:
                loop_block = label_to_block.get(current_label)
                if not loop_block:
                    LOG.error(
                        "Unable to find loop block with label in loop graph",
                        workflow_run_id=workflow_run_id,
                        loop_label=self.label,
                        current_label=current_label,
                    )
                    failure_block_result = await self.build_block_result(
                        success=False,
                        status=BlockStatus.failed,
                        failure_reason=f"Unable to find block with label {current_label} inside loop {self.label}",
                        workflow_run_block_id=workflow_run_block_id,
                        organization_id=organization_id,
                        is_synthetic_loop_failure=True,
                    )
                    block_outputs.append(failure_block_result)
                    outputs_with_loop_values.append(each_loop_output_values)
                    await self._persist_partial_loop_output(workflow_run_id, outputs_with_loop_values, loop_idx)
                    return LoopBlockExecutedResult(
                        outputs_with_loop_values=outputs_with_loop_values,
                        block_outputs=block_outputs,
                        last_block=current_block,
                    )

                metadata: BlockMetadata = {
                    "current_index": loop_idx,
                    "current_value": loop_over_value,
                    "current_item": loop_over_value,
                }
                workflow_run_context.update_block_metadata(self.label, metadata)
                workflow_run_context.update_block_metadata(loop_block.label, metadata)

                original_loop_block = loop_block
                loop_block = loop_block.model_copy(deep=True)
                current_block = loop_block

                # Determine the parent for timeline nesting: if this block is
                # inside a conditional's scope, parent it to that conditional's
                # workflow_run_block rather than the loop's.
                parent_wrb_id = workflow_run_block_id
                if current_label in conditional_scopes:
                    cond_label = conditional_scopes[current_label]
                    if cond_label in conditional_wrb_ids:
                        parent_wrb_id = conditional_wrb_ids[cond_label]

                block_output = await loop_block.execute_safe(
                    workflow_run_id=workflow_run_id,
                    parent_workflow_run_block_id=parent_wrb_id,
                    organization_id=organization_id,
                    browser_session_id=browser_session_id,
                    current_value=str(loop_over_value),
                    current_index=loop_idx,
                )

                # Track conditional workflow_run_block_ids so branch targets
                # can be parented to them.
                if loop_block.block_type == BlockType.CONDITIONAL and block_output.workflow_run_block_id:
                    conditional_wrb_ids[current_label] = block_output.workflow_run_block_id

                output_value = (
                    workflow_run_context.get_value(block_output.output_parameter.key)
                    if workflow_run_context.has_value(block_output.output_parameter.key)
                    else None
                )

                # Log the output value for debugging
                if block_output.output_parameter.key.endswith("_output"):
                    LOG.debug("Block output", block_type=loop_block.block_type, output_present=output_value is not None)

                # Log URL information for goto_url blocks
                if loop_block.block_type == BlockType.GOTO_URL:
                    LOG.info("Goto URL block executed", loop_idx=loop_idx)
                each_loop_output_values.append(
                    {
                        "loop_value": loop_over_value,
                        "output_parameter": block_output.output_parameter,
                        "output_value": output_value,
                    }
                )
                try:
                    if block_output.workflow_run_block_id:
                        await app.DATABASE.observer.update_workflow_run_block(
                            workflow_run_block_id=block_output.workflow_run_block_id,
                            organization_id=organization_id,
                            current_value=str(loop_over_value),
                            current_index=loop_idx,
                        )
                except Exception:
                    LOG.warning(
                        "Failed to update workflow run block",
                        workflow_run_block_id=block_output.workflow_run_block_id,
                        loop_idx=loop_idx,
                    )
                loop_block = original_loop_block
                block_outputs.append(block_output)

                # Check max_steps_per_iteration limit after each block execution
                iteration_step_count += 1  # Count each block execution as a step
                if iteration_step_count >= DEFAULT_MAX_STEPS_PER_ITERATION:
                    LOG.info(
                        f"ForLoopBlock Reached max_steps_per_iteration limit ({DEFAULT_MAX_STEPS_PER_ITERATION}) in iteration {loop_idx}, stopping iteration",
                        workflow_run_id=workflow_run_id,
                        loop_idx=loop_idx,
                        max_steps_per_iteration=DEFAULT_MAX_STEPS_PER_ITERATION,
                        iteration_step_count=iteration_step_count,
                    )
                    # Create a failure block result for this iteration
                    failure_block_result = await self.build_block_result(
                        success=False,
                        status=BlockStatus.failed,
                        failure_reason=f"Reached max_steps_per_iteration limit of {DEFAULT_MAX_STEPS_PER_ITERATION}",
                        workflow_run_block_id=workflow_run_block_id,
                        organization_id=organization_id,
                        is_synthetic_loop_failure=True,
                    )
                    block_outputs.append(failure_block_result)
                    # If next_loop_on_failure is False, stop the entire loop
                    if not self.next_loop_on_failure:
                        outputs_with_loop_values.append(each_loop_output_values)
                        await self._persist_partial_loop_output(workflow_run_id, outputs_with_loop_values, loop_idx)
                        return LoopBlockExecutedResult(
                            outputs_with_loop_values=outputs_with_loop_values,
                            block_outputs=block_outputs,
                            last_block=current_block,
                        )
                    # If next_loop_on_failure is True, break out of the block loop for this iteration
                    break

                if block_output.status == BlockStatus.canceled:
                    LOG.info(
                        f"ForLoopBlock Block with type {loop_block.block_type} at index {block_idx} during loop {loop_idx} was canceled for workflow run {workflow_run_id}, canceling for loop",
                        block_type=loop_block.block_type,
                        workflow_run_id=workflow_run_id,
                        block_idx=block_idx,
                        block_result_count=len(block_outputs),
                    )
                    outputs_with_loop_values.append(each_loop_output_values)
                    await self._persist_partial_loop_output(workflow_run_id, outputs_with_loop_values, loop_idx)
                    return LoopBlockExecutedResult(
                        outputs_with_loop_values=outputs_with_loop_values,
                        block_outputs=block_outputs,
                        last_block=current_block,
                    )

                if (
                    not block_output.success
                    and not loop_block.continue_on_failure
                    and not loop_block.next_loop_on_failure
                    and not self.next_loop_on_failure
                ):
                    LOG.info(
                        f"ForLoopBlock Encountered a failure processing block {block_idx} during loop {loop_idx}, terminating early",
                        block_output_count=len(block_outputs),
                        loop_idx=loop_idx,
                        block_idx=block_idx,
                        loop_block_continue_on_failure=loop_block.continue_on_failure,
                        next_loop_on_failure=loop_block.next_loop_on_failure or self.next_loop_on_failure,
                    )
                    outputs_with_loop_values.append(each_loop_output_values)
                    await self._persist_partial_loop_output(workflow_run_id, outputs_with_loop_values, loop_idx)
                    return LoopBlockExecutedResult(
                        outputs_with_loop_values=outputs_with_loop_values,
                        block_outputs=block_outputs,
                        last_block=current_block,
                    )

                if block_output.success or loop_block.continue_on_failure:
                    next_label: str | None = None
                    if loop_block.block_type == BlockType.CONDITIONAL:
                        branch_metadata = (
                            block_output.output_parameter_value
                            if isinstance(block_output.output_parameter_value, dict)
                            else None
                        )
                        next_label = (branch_metadata or {}).get("next_block_label")
                    else:
                        next_label = default_next_map.get(loop_block.label)

                    if not next_label:
                        break

                    if next_label not in label_to_block:
                        failure_block_result = await self.build_block_result(
                            success=False,
                            status=BlockStatus.failed,
                            failure_reason=f"Next block label {next_label} not found inside loop {self.label}",
                            workflow_run_block_id=workflow_run_block_id,
                            organization_id=organization_id,
                            is_synthetic_loop_failure=True,
                        )
                        block_outputs.append(failure_block_result)
                        outputs_with_loop_values.append(each_loop_output_values)
                        await self._persist_partial_loop_output(workflow_run_id, outputs_with_loop_values, loop_idx)
                        return LoopBlockExecutedResult(
                            outputs_with_loop_values=outputs_with_loop_values,
                            block_outputs=block_outputs,
                            last_block=current_block,
                        )

                    current_label = next_label
                    block_idx += 1
                    continue

                if loop_block.next_loop_on_failure or self.next_loop_on_failure:
                    LOG.info(
                        f"ForLoopBlock Block {block_idx} during loop {loop_idx} failed but will continue to next iteration",
                        block_output_count=len(block_outputs),
                        loop_idx=loop_idx,
                        block_idx=block_idx,
                        loop_block_next_loop_on_failure=loop_block.next_loop_on_failure or self.next_loop_on_failure,
                    )
                    break

                break

            outputs_with_loop_values.append(each_loop_output_values)
            is_last_iteration = loop_idx == len(loop_over_values) - 1
            if loop_idx % PERSIST_LOOP_OUTPUT_INTERVAL == 0 or is_last_iteration:
                await self._persist_partial_loop_output(workflow_run_id, outputs_with_loop_values, loop_idx)

        return LoopBlockExecutedResult(
            outputs_with_loop_values=outputs_with_loop_values,
            block_outputs=block_outputs,
            last_block=current_block,
            natural_completion=True,
        )

    async def execute(
        self,
        workflow_run_id: str,
        workflow_run_block_id: str,
        organization_id: str | None = None,
        browser_session_id: str | None = None,
        **kwargs: dict,
    ) -> BlockResult:
        # Save the caller's loop_internal_state so we can restore it after this
        # loop finishes. Supports nested loops (parent's state is preserved) and
        # ensures stale per-iteration baselines don't leak into subsequent blocks.
        outer_context = skyvern_context.current()
        outer_loop_state = outer_context.loop_internal_state if outer_context else None
        try:
            return await self._run_loop(
                workflow_run_id=workflow_run_id,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
                browser_session_id=browser_session_id,
                **kwargs,
            )
        finally:
            if outer_context:
                outer_context.loop_internal_state = outer_loop_state

    async def _run_loop(
        self,
        workflow_run_id: str,
        workflow_run_block_id: str,
        organization_id: str | None = None,
        browser_session_id: str | None = None,
        **kwargs: dict,
    ) -> BlockResult:
        workflow_run_context = self.get_workflow_run_context(workflow_run_id)
        try:
            loop_over_values = await self.get_loop_over_parameter_values(
                workflow_run_context=workflow_run_context,
                workflow_run_id=workflow_run_id,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )
        except Exception:
            return await self.build_block_result(
                success=False,
                failure_reason="Failed to get loop values.",
                status=BlockStatus.failed,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )

        await app.DATABASE.observer.update_workflow_run_block(
            workflow_run_block_id=workflow_run_block_id,
            organization_id=organization_id,
            loop_values=loop_over_values,
        )

        LOG.info(
            f"Number of loop_over values: {len(loop_over_values)}",
            block_type=self.block_type,
            workflow_run_id=workflow_run_id,
            num_loop_over_values=len(loop_over_values),
        )
        if not loop_over_values or len(loop_over_values) == 0:
            LOG.info(
                "No loop_over values found, terminating block",
                block_type=self.block_type,
                workflow_run_id=workflow_run_id,
                num_loop_over_values=len(loop_over_values),
                complete_if_empty=self.complete_if_empty,
            )
            await self.record_output_parameter_value(workflow_run_context, workflow_run_id, [])
            if self.complete_if_empty:
                return await self.build_block_result(
                    success=True,
                    failure_reason=None,
                    output_parameter_value=[],
                    status=BlockStatus.completed,
                    workflow_run_block_id=workflow_run_block_id,
                    organization_id=organization_id,
                )
            else:
                return await self.build_block_result(
                    success=False,
                    failure_reason="No iterable value found for the loop block",
                    status=BlockStatus.terminated,
                    workflow_run_block_id=workflow_run_block_id,
                    organization_id=organization_id,
                )

        if not self.loop_blocks or len(self.loop_blocks) == 0:
            LOG.info(
                "No defined blocks to loop, terminating block",
                block_type=self.block_type,
                workflow_run_id=workflow_run_id,
                num_loop_blocks=len(self.loop_blocks),
            )
            await self.record_output_parameter_value(workflow_run_context, workflow_run_id, [])
            return await self.build_block_result(
                success=False,
                failure_reason="No defined blocks to loop",
                status=BlockStatus.terminated,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )

        try:
            loop_executed_result = await self.execute_loop_helper(
                workflow_run_id=workflow_run_id,
                workflow_run_block_id=workflow_run_block_id,
                workflow_run_context=workflow_run_context,
                loop_over_values=loop_over_values,
                organization_id=organization_id,
                browser_session_id=browser_session_id,
            )
        except InvalidWorkflowDefinition:
            LOG.error(
                "Loop graph validation failed",
                workflow_run_id=workflow_run_id,
                loop_label=self.label,
            )
            return await self.build_block_result(
                success=False,
                failure_reason="Loop graph validation failed.",
                status=BlockStatus.failed,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )
        await self.record_output_parameter_value(
            workflow_run_context, workflow_run_id, loop_executed_result.outputs_with_loop_values
        )

        block_status, success, failure_reason = loop_executed_result.resolve_status(self.next_loop_on_failure)

        return await self.build_block_result(
            success=success,
            failure_reason=failure_reason,
            output_parameter_value=loop_executed_result.outputs_with_loop_values,
            status=block_status,
            workflow_run_block_id=workflow_run_block_id,
            organization_id=organization_id,
        )


class WhileLoopBlock(Block):
    """Loop block driven by a runtime condition. Iterates while ``condition`` evaluates truthy.

    Top-of-loop semantics: the condition is evaluated *before* each iteration (including the
    first). If the condition is false on the first check, the body never runs and the block
    returns success with an empty output list.

    Safety: the loop is capped at ``DEFAULT_MAX_LOOP_ITERATIONS`` (1000). Reaching the cap is
    treated as a failure so that a misbehaving condition can never spin forever.
    """

    block_type: Literal[BlockType.WHILE_LOOP] = BlockType.WHILE_LOOP  # type: ignore
    execute_safe = _execute_parameter_observing_block_safe

    loop_blocks: list[BlockTypeVar]
    # The discriminated union on ``criteria_type`` handles dict→typed coercion. Pydantic
    # rejects a dict missing ``criteria_type`` with ``union_tag_not_found`` before any
    # model_validator runs, so no extra coercion validator is needed here.
    condition: BranchCriteriaTypeVar

    def get_all_parameters(
        self,
        workflow_run_id: str,
    ) -> list[PARAMETER_TYPE]:
        parameters: set[PARAMETER_TYPE] = set()
        for loop_block in self.loop_blocks:
            for parameter in loop_block.get_all_parameters(workflow_run_id):
                parameters.add(parameter)
        return list(parameters)

    def _build_loop_graph(
        self,
        blocks: list[BlockTypeVar],
        skip_sequential_defaulting: bool = False,
    ) -> tuple[str, dict[str, BlockTypeVar], dict[str, str | None]]:
        # Duplicated from ForLoopBlock._build_loop_graph for PR 1; promotion to a shared
        # helper is tracked in PR 7 (refactor).
        label_to_block: dict[str, BlockTypeVar] = {}
        default_next_map: dict[str, str | None] = {}

        for block in blocks:
            if block.label in label_to_block:
                raise InvalidWorkflowDefinition(f"Duplicate block label detected in loop: {block.label}")
            label_to_block[block.label] = block
            default_next_map[block.label] = block.next_block_label

        if not skip_sequential_defaulting:
            has_conditional_blocks = any(block.block_type == BlockType.CONDITIONAL for block in blocks)
            if not has_conditional_blocks:
                for idx, block in enumerate(blocks[:-1]):
                    if default_next_map.get(block.label) is None:
                        default_next_map[block.label] = blocks[idx + 1].label

        # SKY-8571: connect conditional branch terminals to the conditional's merge-point successor.
        resolve_conditional_merge_edges(blocks, label_to_block, default_next_map)

        adjacency: dict[str, set[str]] = {label: set() for label in label_to_block}
        incoming: dict[str, int] = {label: 0 for label in label_to_block}

        def _add_edge(source: str, target: str | None) -> None:
            if not target:
                return
            if target not in label_to_block:
                raise InvalidWorkflowDefinition(
                    f"Block {source} references unknown next_block_label {target} inside loop {self.label}"
                )
            if target not in adjacency[source]:
                adjacency[source].add(target)
                incoming[target] += 1

        for label, block in label_to_block.items():
            if block.block_type == BlockType.CONDITIONAL:
                for branch in block.ordered_branches:
                    _add_edge(label, branch.next_block_label)
            else:
                _add_edge(label, default_next_map.get(label))

        roots = [label for label, count in incoming.items() if count == 0]
        if not roots:
            raise InvalidWorkflowDefinition(
                f"Circular reference detected inside loop {self.label}: every block is the target of another"
                " block's next_block_label, so there is no starting block."
                " At least one block must not be the target of any next_block_label or branch condition."
            )
        if len(roots) > 1:
            raise InvalidWorkflowDefinition(
                f"Disconnected blocks detected inside loop {self.label}: blocks"
                f" ({', '.join(sorted(roots))}) are not reachable from any other block."
                " Every block must be reachable from the first block through next_block_label or"
                " conditional branch references."
                " Either connect them by setting another block's next_block_label to point to them, or remove them."
            )

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
                f"Circular reference detected inside loop {self.label}: some blocks form a loop through their"
                " next_block_label references, causing an infinite cycle."
                " Ensure that following next_block_label from any block eventually reaches a block"
                " with next_block_label set to null."
            )

        return roots[0], label_to_block, default_next_map

    def validate_loop_blocks(self) -> None:
        """Validate the loop_blocks graph and recurse into nested loop blocks."""
        if not self.loop_blocks:
            return
        self._build_loop_graph(self.loop_blocks, skip_sequential_defaulting=True)
        for block in self.loop_blocks:
            if isinstance(block, (ForLoopBlock, WhileLoopBlock)):
                block.validate_loop_blocks()

    async def _persist_partial_loop_output(
        self,
        workflow_run_id: str,
        outputs_with_loop_values: list[list[dict[str, Any]]],
        loop_idx: int,
    ) -> None:
        """Persist partial while-loop output to DB so accumulated iteration data survives
        Temporal activity timeouts. Mirrors ``ForLoopBlock._persist_partial_loop_output``.
        """
        if not self.output_parameter:
            return
        _maybe_truncate_loop_outputs(
            outputs_with_loop_values,
            workflow_run_id=workflow_run_id,
            output_parameter_id=self.output_parameter.output_parameter_id,
        )
        try:
            await app.DATABASE.workflow_runs.create_or_update_workflow_run_output_parameter(
                workflow_run_id=workflow_run_id,
                output_parameter_id=self.output_parameter.output_parameter_id,
                value=outputs_with_loop_values,
            )
        except Exception:
            LOG.warning(
                "Failed to incrementally persist while-loop output",
                workflow_run_id=workflow_run_id,
                output_parameter_id=self.output_parameter.output_parameter_id,
                loop_idx=loop_idx,
            )

    async def _evaluate_condition(
        self,
        workflow_run_context: WorkflowRunContext,
        *,
        workflow_run_id: str,
        workflow_run_block_id: str,
        organization_id: str | None,
        browser_session_id: str | None,
    ) -> bool:
        """Evaluate the loop condition. Raises on rendering errors so the caller can convert
        the failure into a block result with a clear message.

        ``current_index`` (the 0-indexed iteration counter) is read from this block's own
        metadata via the existing for_loop injection in
        :meth:`format_block_parameter_template_from_workflow_run_context`. ``current_value``
        holds the same integer so ``{{ current_value }}`` caps work like For Each loops.
        The caller writes both onto ``self.label`` before invoking this method, so
        condition authors can bootstrap iteration 1 with
        ``{{ current_index == 0 or <body_output_ref> }}``.
        """
        evaluation_context = BranchEvaluationContext(
            workflow_run_context=workflow_run_context,
            block_label=self.label,
            template_renderer=lambda potential_template: self.format_block_parameter_template_from_workflow_run_context(
                potential_template,
                workflow_run_context,
            ),
        )
        if isinstance(self.condition, PromptBranchCriteria):
            synthetic_branch = BranchCondition(
                id=str(uuid.uuid4()),
                criteria=self.condition,
                next_block_label=None,
                is_default=False,
            )
            results, _, _, _ = await _evaluate_prompt_branch_conditions_batch(
                log_label=self.label,
                branches=[synthetic_branch],
                evaluation_context=evaluation_context,
                workflow_run_id=workflow_run_id,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
                browser_session_id=browser_session_id,
                workflow_id=self.output_parameter.workflow_id,
                extraction_description_suffix="while_loop condition",
            )
            return results[0]

        return await self.condition.evaluate(evaluation_context)

    async def _execute_while_loop_helper(
        self,
        workflow_run_id: str,
        workflow_run_block_id: str,
        workflow_run_context: WorkflowRunContext,
        organization_id: str | None = None,
        browser_session_id: str | None = None,
    ) -> LoopBlockExecutedResult:
        outputs_with_loop_values: list[list[dict[str, Any]]] = []
        block_outputs: list[BlockResult] = []
        current_block: BlockTypeVar | None = None

        start_label, label_to_block, default_next_map = self._build_loop_graph(self.loop_blocks)
        conditional_scopes = compute_conditional_scopes(label_to_block, default_next_map)

        loop_idx = 0
        while True:
            # Evaluate the condition at the top of every iteration (including the first).
            # The cap check fires *after* the condition check so that a loop which would
            # naturally exit on the (cap+1)-th check returns success rather than tripping
            # the cap one iteration early.
            #
            # Condition rendering errors always terminate the loop, regardless of
            # ``next_loop_on_failure``. The flag governs *body* failures (which can vary
            # iteration to iteration), but a Jinja render error means the condition itself
            # is malformed and will fail identically on the next iteration — there is no
            # forward progress to be made by retrying.
            # Expose ``current_index`` to the condition's template scope before evaluation
            # so authors can bootstrap iteration 0 or cap iterations. ``current_value`` and
            # ``current_item`` stay None so Jinja matches persisted timeline rows
            # (``execute_safe(..., current_value=None)``) and outer for-loop rows cannot leak.
            condition_metadata: BlockMetadata = {
                "current_index": loop_idx,
                "current_value": None,
                "current_item": None,
            }
            workflow_run_context.update_block_metadata(self.label, condition_metadata)

            try:
                should_continue = await self._evaluate_condition(
                    workflow_run_context,
                    workflow_run_id=workflow_run_id,
                    workflow_run_block_id=workflow_run_block_id,
                    organization_id=organization_id,
                    browser_session_id=browser_session_id,
                )
            except (FailedToFormatJinjaStyleParameter, MissingJinjaVariables, ValueError):
                LOG.error(
                    "WhileLoopBlock condition evaluation failed",
                    workflow_run_id=workflow_run_id,
                    block_label=self.label,
                )
                failure_block_result = await self.build_block_result(
                    success=False,
                    status=BlockStatus.failed,
                    failure_reason="Failed to evaluate while-loop condition.",
                    workflow_run_block_id=workflow_run_block_id,
                    organization_id=organization_id,
                )
                block_outputs.append(failure_block_result)
                await self._persist_partial_loop_output(workflow_run_id, outputs_with_loop_values, loop_idx)
                return LoopBlockExecutedResult(
                    outputs_with_loop_values=outputs_with_loop_values,
                    block_outputs=block_outputs,
                    last_block=current_block,
                )

            if not should_continue:
                LOG.info(
                    "WhileLoopBlock condition is false, exiting loop",
                    workflow_run_id=workflow_run_id,
                    loop_idx=loop_idx,
                )
                await self._persist_partial_loop_output(workflow_run_id, outputs_with_loop_values, loop_idx)
                break

            # Check max_iterations limit: only fires when the condition is still true at
            # iteration index ``cap``, i.e. the loop would have run a (cap+1)-th body.
            if loop_idx >= DEFAULT_MAX_LOOP_ITERATIONS:
                LOG.info(
                    "WhileLoopBlock reached max_iterations limit, stopping loop",
                    workflow_run_id=workflow_run_id,
                    loop_idx=loop_idx,
                    max_iterations=DEFAULT_MAX_LOOP_ITERATIONS,
                )
                failure_block_result = await self.build_block_result(
                    success=False,
                    status=BlockStatus.failed,
                    failure_reason=f"Reached max_loop_iterations limit of {DEFAULT_MAX_LOOP_ITERATIONS}",
                    workflow_run_block_id=workflow_run_block_id,
                    organization_id=organization_id,
                    is_synthetic_loop_failure=True,
                )
                block_outputs.append(failure_block_result)
                await self._persist_partial_loop_output(workflow_run_id, outputs_with_loop_values, loop_idx)
                return LoopBlockExecutedResult(
                    outputs_with_loop_values=outputs_with_loop_values,
                    block_outputs=block_outputs,
                    last_block=current_block,
                )

            # Capture baseline downloaded files for per-iteration scoping (SKY-7005).
            # Download-producing child blocks re-capture their own per-block baseline
            # at start; this seed only covers filtering before the first such capture.
            loop_context = skyvern_context.current()
            if loop_context:
                downloaded_file_sigs_before: list[tuple[str | None, str | None, str | None]] = []
                baseline_timed_out = False
                try:
                    async with asyncio.timeout(GET_DOWNLOADED_FILES_TIMEOUT):
                        downloaded_file_sigs_before = [
                            to_downloaded_file_signature(fi)
                            for fi in await app.STORAGE.get_downloaded_files(
                                organization_id=organization_id or "",
                                run_id=resolve_run_download_id(loop_context, fallback_run_id=workflow_run_id),
                            )
                        ]
                except asyncio.TimeoutError:
                    baseline_timed_out = True
                    LOG.warning(
                        "Timeout getting baseline downloaded files for loop iteration",
                        workflow_run_id=workflow_run_id,
                        loop_idx=loop_idx,
                    )
                if baseline_timed_out:
                    loop_context.loop_internal_state = None
                else:
                    loop_context.loop_internal_state = {
                        DOWNLOADED_FILE_SIGS_KEY: downloaded_file_sigs_before,
                    }

            each_loop_output_values: list[dict[str, Any]] = []

            iteration_step_count = 0
            LOG.debug(
                "WhileLoopBlock starting iteration",
                workflow_run_id=workflow_run_id,
                loop_idx=loop_idx,
                max_steps_per_iteration=DEFAULT_MAX_STEPS_PER_ITERATION,
            )

            block_idx = 0
            current_label: str | None = start_label
            conditional_wrb_ids: dict[str, str] = {}
            while current_label:
                loop_block = label_to_block.get(current_label)
                if not loop_block:
                    LOG.error(
                        "Unable to find loop block with label in loop graph",
                        workflow_run_id=workflow_run_id,
                        loop_label=self.label,
                        current_label=current_label,
                    )
                    failure_block_result = await self.build_block_result(
                        success=False,
                        status=BlockStatus.failed,
                        failure_reason=f"Unable to find block with label {current_label} inside loop {self.label}",
                        workflow_run_block_id=workflow_run_block_id,
                        organization_id=organization_id,
                        is_synthetic_loop_failure=True,
                    )
                    block_outputs.append(failure_block_result)
                    outputs_with_loop_values.append(each_loop_output_values)
                    await self._persist_partial_loop_output(workflow_run_id, outputs_with_loop_values, loop_idx)
                    return LoopBlockExecutedResult(
                        outputs_with_loop_values=outputs_with_loop_values,
                        block_outputs=block_outputs,
                        last_block=current_block,
                    )

                # ``current_index`` is the iteration counter. ``current_value`` stays None so
                # runtime matches ``execute_safe`` / timeline rows; use ``{{ current_index }}``
                # in Jinja. ``current_item`` stays None.
                metadata: BlockMetadata = {
                    "current_index": loop_idx,
                    "current_value": None,
                    "current_item": None,
                }
                workflow_run_context.update_block_metadata(self.label, metadata)
                workflow_run_context.update_block_metadata(loop_block.label, metadata)

                original_loop_block = loop_block
                loop_block = loop_block.model_copy(deep=True)
                current_block = loop_block

                parent_wrb_id = workflow_run_block_id
                if current_label in conditional_scopes:
                    cond_label = conditional_scopes[current_label]
                    if cond_label in conditional_wrb_ids:
                        parent_wrb_id = conditional_wrb_ids[cond_label]

                # ``current_value`` is None on persisted timeline rows and in block metadata;
                # iteration is available only as ``current_index``.
                block_output = await loop_block.execute_safe(
                    workflow_run_id=workflow_run_id,
                    parent_workflow_run_block_id=parent_wrb_id,
                    organization_id=organization_id,
                    browser_session_id=browser_session_id,
                    current_value=None,
                    current_index=loop_idx,
                )

                if loop_block.block_type == BlockType.CONDITIONAL and block_output.workflow_run_block_id:
                    conditional_wrb_ids[current_label] = block_output.workflow_run_block_id

                output_value = (
                    workflow_run_context.get_value(block_output.output_parameter.key)
                    if workflow_run_context.has_value(block_output.output_parameter.key)
                    else None
                )

                if block_output.output_parameter.key.endswith("_output"):
                    LOG.debug("Block output", block_type=loop_block.block_type, output_present=output_value is not None)

                if loop_block.block_type == BlockType.GOTO_URL:
                    LOG.info("Goto URL block executed", loop_idx=loop_idx)

                each_loop_output_values.append(
                    {
                        "output_parameter": block_output.output_parameter,
                        "output_value": output_value,
                    }
                )

                try:
                    if block_output.workflow_run_block_id:
                        await app.DATABASE.observer.update_workflow_run_block(
                            workflow_run_block_id=block_output.workflow_run_block_id,
                            organization_id=organization_id,
                            current_value=None,
                            current_index=loop_idx,
                        )
                except Exception:
                    LOG.warning(
                        "Failed to update workflow run block",
                        workflow_run_block_id=block_output.workflow_run_block_id,
                        loop_idx=loop_idx,
                    )
                loop_block = original_loop_block
                block_outputs.append(block_output)

                iteration_step_count += 1
                if iteration_step_count >= DEFAULT_MAX_STEPS_PER_ITERATION:
                    LOG.info(
                        "WhileLoopBlock reached max_steps_per_iteration limit, stopping iteration",
                        workflow_run_id=workflow_run_id,
                        loop_idx=loop_idx,
                        max_steps_per_iteration=DEFAULT_MAX_STEPS_PER_ITERATION,
                        iteration_step_count=iteration_step_count,
                    )
                    failure_block_result = await self.build_block_result(
                        success=False,
                        status=BlockStatus.failed,
                        failure_reason=f"Reached max_steps_per_iteration limit of {DEFAULT_MAX_STEPS_PER_ITERATION}",
                        workflow_run_block_id=workflow_run_block_id,
                        organization_id=organization_id,
                        is_synthetic_loop_failure=True,
                    )
                    block_outputs.append(failure_block_result)
                    if not self.next_loop_on_failure:
                        outputs_with_loop_values.append(each_loop_output_values)
                        await self._persist_partial_loop_output(workflow_run_id, outputs_with_loop_values, loop_idx)
                        return LoopBlockExecutedResult(
                            outputs_with_loop_values=outputs_with_loop_values,
                            block_outputs=block_outputs,
                            last_block=current_block,
                        )
                    break

                if block_output.status == BlockStatus.canceled:
                    LOG.info(
                        "WhileLoopBlock child block canceled, canceling while loop",
                        block_type=loop_block.block_type,
                        workflow_run_id=workflow_run_id,
                        block_idx=block_idx,
                        loop_idx=loop_idx,
                        block_result_count=len(block_outputs),
                    )
                    outputs_with_loop_values.append(each_loop_output_values)
                    await self._persist_partial_loop_output(workflow_run_id, outputs_with_loop_values, loop_idx)
                    return LoopBlockExecutedResult(
                        outputs_with_loop_values=outputs_with_loop_values,
                        block_outputs=block_outputs,
                        last_block=current_block,
                    )

                if (
                    not block_output.success
                    and not loop_block.continue_on_failure
                    and not loop_block.next_loop_on_failure
                    and not self.next_loop_on_failure
                ):
                    LOG.info(
                        "WhileLoopBlock encountered a failure processing block, terminating early",
                        block_output_count=len(block_outputs),
                        loop_idx=loop_idx,
                        block_idx=block_idx,
                        loop_block_continue_on_failure=loop_block.continue_on_failure,
                        next_loop_on_failure=loop_block.next_loop_on_failure or self.next_loop_on_failure,
                    )
                    outputs_with_loop_values.append(each_loop_output_values)
                    await self._persist_partial_loop_output(workflow_run_id, outputs_with_loop_values, loop_idx)
                    return LoopBlockExecutedResult(
                        outputs_with_loop_values=outputs_with_loop_values,
                        block_outputs=block_outputs,
                        last_block=current_block,
                    )

                if block_output.success or loop_block.continue_on_failure:
                    next_label: str | None = None
                    if loop_block.block_type == BlockType.CONDITIONAL:
                        branch_metadata = (
                            block_output.output_parameter_value
                            if isinstance(block_output.output_parameter_value, dict)
                            else None
                        )
                        next_label = (branch_metadata or {}).get("next_block_label")
                    else:
                        next_label = default_next_map.get(loop_block.label)

                    if not next_label:
                        break

                    if next_label not in label_to_block:
                        failure_block_result = await self.build_block_result(
                            success=False,
                            status=BlockStatus.failed,
                            failure_reason=f"Next block label {next_label} not found inside loop {self.label}",
                            workflow_run_block_id=workflow_run_block_id,
                            organization_id=organization_id,
                            is_synthetic_loop_failure=True,
                        )
                        block_outputs.append(failure_block_result)
                        outputs_with_loop_values.append(each_loop_output_values)
                        await self._persist_partial_loop_output(workflow_run_id, outputs_with_loop_values, loop_idx)
                        return LoopBlockExecutedResult(
                            outputs_with_loop_values=outputs_with_loop_values,
                            block_outputs=block_outputs,
                            last_block=current_block,
                        )

                    current_label = next_label
                    block_idx += 1
                    continue

                if loop_block.next_loop_on_failure or self.next_loop_on_failure:
                    LOG.info(
                        "WhileLoopBlock child block failed but will continue to next iteration",
                        block_output_count=len(block_outputs),
                        loop_idx=loop_idx,
                        block_idx=block_idx,
                        loop_block_next_loop_on_failure=loop_block.next_loop_on_failure or self.next_loop_on_failure,
                    )
                    break

                break

            outputs_with_loop_values.append(each_loop_output_values)
            # We don't know "is_last_iteration" for a while-loop ahead of time, so persist
            # every PERSIST_LOOP_OUTPUT_INTERVAL iterations and once again at the top of the
            # next iteration when the condition is false (handled at the break above).
            if loop_idx % PERSIST_LOOP_OUTPUT_INTERVAL == 0:
                await self._persist_partial_loop_output(workflow_run_id, outputs_with_loop_values, loop_idx)

            loop_idx += 1

        return LoopBlockExecutedResult(
            outputs_with_loop_values=outputs_with_loop_values,
            block_outputs=block_outputs,
            last_block=current_block,
            natural_completion=True,
        )

    async def execute(
        self,
        workflow_run_id: str,
        workflow_run_block_id: str,
        organization_id: str | None = None,
        browser_session_id: str | None = None,
        **kwargs: dict,
    ) -> BlockResult:
        # Save the caller's loop_internal_state so we can restore it after this loop
        # finishes. Mirrors ForLoopBlock.execute.
        outer_context = skyvern_context.current()
        outer_loop_state = outer_context.loop_internal_state if outer_context else None
        try:
            return await self._run_loop(
                workflow_run_id=workflow_run_id,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
                browser_session_id=browser_session_id,
                **kwargs,
            )
        finally:
            if outer_context:
                outer_context.loop_internal_state = outer_loop_state

    async def _run_loop(
        self,
        workflow_run_id: str,
        workflow_run_block_id: str,
        organization_id: str | None = None,
        browser_session_id: str | None = None,
        **kwargs: dict,
    ) -> BlockResult:
        workflow_run_context = self.get_workflow_run_context(workflow_run_id)

        if not self.loop_blocks:
            LOG.info(
                "No defined blocks to loop, terminating block",
                block_type=self.block_type,
                workflow_run_id=workflow_run_id,
                num_loop_blocks=len(self.loop_blocks),
            )
            await self.record_output_parameter_value(workflow_run_context, workflow_run_id, [])
            return await self.build_block_result(
                success=False,
                failure_reason="No defined blocks to loop",
                status=BlockStatus.terminated,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )

        try:
            loop_executed_result = await self._execute_while_loop_helper(
                workflow_run_id=workflow_run_id,
                workflow_run_block_id=workflow_run_block_id,
                workflow_run_context=workflow_run_context,
                organization_id=organization_id,
                browser_session_id=browser_session_id,
            )
        except InvalidWorkflowDefinition:
            LOG.error(
                "While-loop graph validation failed",
                workflow_run_id=workflow_run_id,
                loop_label=self.label,
            )
            return await self.build_block_result(
                success=False,
                failure_reason="While-loop graph validation failed.",
                status=BlockStatus.failed,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )

        await self.record_output_parameter_value(
            workflow_run_context, workflow_run_id, loop_executed_result.outputs_with_loop_values
        )

        # Special case: condition false on the very first check. The body never ran, so
        # there are no block_outputs. Return success with an empty output list — this is
        # the normal/expected "nothing to do" path for a while-loop.
        if not loop_executed_result.block_outputs:
            return await self.build_block_result(
                success=True,
                failure_reason=None,
                output_parameter_value=loop_executed_result.outputs_with_loop_values,
                status=BlockStatus.completed,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )

        block_status, success, failure_reason = loop_executed_result.resolve_status(self.next_loop_on_failure)

        return await self.build_block_result(
            success=success,
            failure_reason=failure_reason,
            output_parameter_value=loop_executed_result.outputs_with_loop_values,
            status=block_status,
            workflow_run_block_id=workflow_run_block_id,
            organization_id=organization_id,
        )


class Credential(SimpleNamespace):
    pass


class CodeBlockStep(BaseModel):
    title: str | None = None
    description: str | None = None
    action_type: ActionType = ActionType.NULL_ACTION
    line_start: int | None = None
    line_end: int | None = None


class CodeBlockOTPError(Exception):
    """Sanitized OTP-primitive error: never includes the identifier, URL, code, or seed."""


class CodeBlockCaptchaError(Exception):
    """Sanitized CAPTCHA-primitive error with no site, selector, vendor, or token details."""


class ErrorCode(Exception):
    """Intentional, author-declared CodeBlock failure signal.

    The error code must name a condition in the effective ``error_code_mapping`` manifest to be
    surfaced as a USER_DEFINED_ERROR; otherwise it fails closed like any unclassified exception.
    """

    def __init__(self, error_code: str, reasoning: str) -> None:
        if (
            type(error_code) is not str
            or not error_code
            or error_code != error_code.strip()
            or len(error_code) > ERROR_CODE_MAX_LENGTH
        ):
            raise ValueError("ErrorCode code must be a trimmed, non-empty string of at most 128 characters")
        if (
            type(reasoning) is not str
            or not reasoning
            or reasoning != reasoning.strip()
            or len(reasoning) > ERROR_CODE_REASONING_MAX_LENGTH
        ):
            raise ValueError("ErrorCode reasoning must be a trimmed, non-empty string of at most 2000 characters")
        self.error_code = error_code
        self.reasoning = reasoning
        super().__init__("CodeBlock raised a declared error")


class CodeBlockDownloadClaimError(Exception):
    """Sanitized download-claim error: never includes file paths, URLs, or file bytes."""


CODE_BLOCK_TAB_OPEN_FAILURE_REASON = "Code block could not open a tab in the browser session; the session's browser may be held by another Playwright client"
CODE_BLOCK_GENERIC_FAILURE_REASON = "Failed to execute code block."
# An exception can carry a whole page's text. Bound the persisted reason below the parameter
# scrubber's disclosure budget, past which it fails closed and returns nothing at all.
CODE_BLOCK_FAILURE_REASON_MAX_CHARS = 2000


def _code_block_failure_action(*, failing_line: int | None, action_order: int, response: str = "") -> Action:
    return Action(
        action_id=generate_action_id(),
        action_type=ActionType.NULL_ACTION,
        status=ActionStatus.failed,
        action_order=action_order,
        description=f"code error at line {failing_line}" if failing_line else "code error",
        response=response[:RECORDED_FAILURE_RESPONSE_MAX_CHARS],
        output={"code_line": failing_line},
    )


def _page_open_error_label(error: BaseException) -> str:
    # Playwright maps only TimeoutError/TargetClosedError to their own Python classes; every
    # other driver failure is the base Error whose .name carries the real class (e.g. TypeError).
    name = getattr(error, "name", None)
    if isinstance(name, str) and name:
        return name
    return type(error).__name__


# A download still in flight when the block returns leaves only its partial row, which the read-back
# filters out. Bounded because run finalization claims the file regardless; this only decides whether
# the block's own output carries it.
_CODE_BLOCK_SESSION_DOWNLOAD_WAIT_SECONDS = 60
_CODE_BLOCK_SESSION_DOWNLOAD_SETTLE_ATTEMPTS = 6
_CODE_BLOCK_SESSION_DOWNLOAD_SETTLE_INTERVAL_SECONDS = 0.5


async def _code_block_solve_captcha_builtin(
    page: Page | RecordingPage,
    *,
    organization_id: str | None = None,
    workflow_run_id: str | None = None,
    browser_session_id: str | None = None,
) -> bool:
    """Solve a detected challenge through the shared bounded ladder; True when an arm passed.

    Delegates to solve_challenge_ladder and translates its neutral unsolved-signal into the
    code-block-specific error the code-block callers expect.
    """
    try:
        return await solve_challenge_ladder(
            page,
            organization_id=organization_id,
            workflow_run_id=workflow_run_id,
            browser_session_id=browser_session_id,
        )
    except CaptchaChallengeUnsolvedError as exc:
        raise CodeBlockCaptchaError("CAPTCHA could not be solved.") from exc


def _register_code_block_secret(workflow_run_context: WorkflowRunContext, value: str) -> None:
    # Every caller registers a one-time code minted at runtime, so it goes through the runtime OTP
    # path: that set is what the copilot's origin-run handoff publishes, and a code minted here
    # would otherwise never reach the scrubber that guards the page this run leaves.
    workflow_run_context.register_runtime_otp_value(value)


def _code_block_safe_print(
    *values: Any,
    parameters: dict[str, Any],
    sep: str = " ",
    end: str = "\n",
    file: Any = None,
    flush: bool = False,
) -> None:
    del file
    rendered = io.StringIO()
    print(*values, sep=sep, end=end, file=rendered, flush=flush)
    print(app.AGENT_FUNCTION.redact_codeblock_parameter_values(rendered.getvalue(), parameters), end="", flush=flush)


def _redact_codeblock_failure_text(
    value: str | None,
    parameters: dict[str, Any],
    fallback: str = CODE_BLOCK_GENERIC_FAILURE_REASON,
) -> str | None:
    redacted = app.AGENT_FUNCTION.redact_codeblock_parameter_values(value, parameters)
    if not isinstance(redacted, str):
        return None
    # The scrubber fails closed by returning "" once a parameter budget is blown, which a run
    # cannot afford for the one field that says why it stopped. codeblock/workflow.py's
    # scrub_reason guards the runner-side pass the same way. Callers whose value is a code or a
    # telemetry token rather than user-facing prose pass fallback="" to keep the empty result.
    if isinstance(value, str) and value.strip() and not redacted.strip():
        return fallback
    return redacted


def _redact_codeblock_result(result: BlockResult, parameters: dict[str, Any]) -> BlockResult:
    redacted_error_codes = app.AGENT_FUNCTION.redact_codeblock_parameter_values(result.error_codes, parameters)
    return replace(
        result,
        failure_reason=_redact_codeblock_failure_text(result.failure_reason, parameters),
        output_parameter_value=(
            app.AGENT_FUNCTION.redact_codeblock_parameter_values(result.output_parameter_value, parameters)
            if not result.success
            else result.output_parameter_value
        ),
        error_codes=redacted_error_codes if isinstance(redacted_error_codes, list) else [],
    )


class OTPResult(str):
    """A fetched OTP that still knows whether it is a code to type or a link to open.

    Subclasses ``str`` so it can be typed into a field directly, while ``is_link`` lets
    authored code branch without pattern-matching the value.
    """

    otp_type: OTPType

    def __new__(cls, value: str, otp_type: OTPType) -> OTPResult:
        result = super().__new__(cls, value)
        result.otp_type = otp_type
        return result

    @property
    def is_link(self) -> bool:
        return self.otp_type is OTPType.MAGIC_LINK


_OTP_VERB_BY_TYPE = {
    OTPType.TOTP: "await <credential>.otp()",
    OTPType.MAGIC_LINK: "await <credential>.magic_link(page)",
}


def _otp_wait_failure(
    expected_otp_type: OTPType,
    observed_otp_types: set[OTPType],
    budget_seconds: int,
) -> CodeBlockOTPError:
    """Turn a wait that produced nothing into a mismatch report when the other kind of OTP arrived.

    A wrong verb otherwise looks exactly like a mailbox that stayed empty, which leaves the
    authoring retry with nothing to act on.
    """
    mismatched = sorted(observed for observed in observed_otp_types if observed is not expected_otp_type)
    if not mismatched:
        return CodeBlockOTPError(f"OTP was not received within {budget_seconds} seconds.")
    found = ", ".join(observed.value for observed in mismatched)
    # .get, not [], so a future OTPType cannot turn this error path into a KeyError.
    suggested = _OTP_VERB_BY_TYPE.get(mismatched[0], "the matching verb")
    attempted = _OTP_VERB_BY_TYPE.get(expected_otp_type, "this verb")
    return CodeBlockOTPError(
        f"Waited {budget_seconds}s for an OTP of type {expected_otp_type.value}, "
        f"but this mailbox delivered {found}. This sign-in uses the other flow: "
        f"author {suggested} instead of {attempted}."
    )


async def _poll_code_block_otp(
    totp_identifier: str,
    organization_id: str | None,
    workflow_run_id: str,
    workflow_run_context: WorkflowRunContext,
    *,
    budget_seconds: int,
    subject: str,
    expected_otp_type: OTPType,
) -> otp_service.OTPValue:
    if not organization_id:
        raise CodeBlockOTPError("OTP is unavailable: no organization is associated with this code block.")

    # Run-start anchor disqualifies codes that predate this run (identifiers are shared across runs).
    workflow_run = await app.DATABASE.workflow_runs.get_workflow_run(workflow_run_id)
    if workflow_run is None:
        raise CodeBlockOTPError("OTP is unavailable: the workflow run could not be loaded.")

    # A magic link is single-use, so one minted earlier in this run may already be spent. Anchoring
    # near call time narrows that window; it does not close it (SKY-13417 owns consumed-entry marking).
    # The grace interval keeps a link that landed while the triggering click was still settling
    # visible, and absorbs clock skew against the inbox's timestamps. Naive UTC, matching
    # workflow_runs.started_at and both downstream filters: a local-clock anchor would filter
    # every delivery east of UTC and widen the window west.
    if expected_otp_type is OTPType.MAGIC_LINK:
        created_after = naive_utc_now() - otp_service.MAGIC_LINK_ANCHOR_GRACE
    else:
        created_after = workflow_run.started_at

    email_context = otp_email.EmailOTPVerificationContext()
    raw_context = otp_service.RawOTPVerificationContext()
    try:
        polled = await asyncio.wait_for(
            otp_service.poll_otp_value(
                organization_id=organization_id,
                workflow_id=workflow_run.workflow_id,
                workflow_run_id=workflow_run_id,
                workflow_permanent_id=workflow_run.workflow_permanent_id,
                totp_identifier=totp_identifier,
                created_after=created_after,
                expected_otp_type=expected_otp_type,
                email_context=email_context,
                raw_context=raw_context,
            ),
            timeout=budget_seconds,
        )
    except asyncio.TimeoutError:
        raise _otp_wait_failure(
            expected_otp_type,
            email_context.observed_otp_types | raw_context.observed_otp_types,
            budget_seconds,
        )
    except (NoTOTPVerificationCodeFound, FailedToGetTOTPVerificationCode):
        raise CodeBlockOTPError(f"OTP could not be retrieved for {subject}.")

    if polled is None:
        raise CodeBlockOTPError(f"OTP could not be retrieved for {subject}.")

    _register_code_block_secret(workflow_run_context, polled.value)
    return polled


def _code_block_workflow_run_context(workflow_run_id: str) -> WorkflowRunContext:
    workflow_run_context = app.WORKFLOW_CONTEXT_MANAGER.get_workflow_run_context(workflow_run_id)
    if workflow_run_context is None:
        raise CodeBlockOTPError("OTP is unavailable: the workflow run context could not be resolved.")
    return workflow_run_context


async def _resolve_code_block_otp(
    credential_parameter_key: str,
    organization_id: str | None,
    workflow_run_id: str | None,
    *,
    budget_seconds: int,
) -> str:
    """Resolve a fresh OTP at call time for one credential: re-mint its TOTP (the staleness
    fix) or poll its email/SMS/magic-link, registering the value as a secret before return.
    The run context is re-resolved from workflow_run_id, never captured in the bound method's
    closure, so user code cannot reach the seed through the method's cells."""
    if not workflow_run_id:
        raise CodeBlockOTPError("OTP is unavailable: no workflow run is associated with this code block.")
    workflow_run_context = _code_block_workflow_run_context(workflow_run_id)

    otp_value = otp_service.try_generate_totp_for_credential(
        workflow_run_context, credential_parameter_key, workflow_run_id
    )
    if otp_value is not None:
        _register_code_block_secret(workflow_run_context, otp_value.value)
        return otp_value.value

    totp_identifier = workflow_run_context.get_credential_totp_identifier(credential_parameter_key)
    if not totp_identifier:
        raise CodeBlockOTPError(
            "No OTP source is configured for this credential. "
            "Add a TOTP secret or an email/SMS identifier to the credential."
        )

    polled = await _poll_code_block_otp(
        totp_identifier,
        organization_id,
        workflow_run_id,
        workflow_run_context,
        budget_seconds=budget_seconds,
        subject="this credential",
        expected_otp_type=OTPType.TOTP,
    )
    return polled.value


async def _resolve_code_block_magic_link(
    credential_parameter_key: str,
    organization_id: str | None,
    workflow_run_id: str | None,
    *,
    budget_seconds: int,
) -> str:
    """Resolve a fresh sign-in link for one credential, for the caller to navigate to.

    The authenticator-seed branch that ``_resolve_code_block_otp`` tries first is deliberately
    skipped: a seed can only mint a 6-digit code, and returning one here would navigate to it.
    """
    if not workflow_run_id:
        raise CodeBlockOTPError("A sign-in link is unavailable: no workflow run is associated with this code block.")
    workflow_run_context = _code_block_workflow_run_context(workflow_run_id)

    totp_identifier = workflow_run_context.get_credential_totp_identifier(credential_parameter_key)
    if not totp_identifier:
        raise CodeBlockOTPError(
            "No email or SMS identifier is configured for this credential, so no sign-in link can be "
            "received. Add an email/SMS identifier to the credential; an authenticator secret cannot "
            "produce a sign-in link."
        )

    polled = await _poll_code_block_otp(
        totp_identifier,
        organization_id,
        workflow_run_id,
        workflow_run_context,
        budget_seconds=budget_seconds,
        subject="this credential",
        expected_otp_type=OTPType.MAGIC_LINK,
    )
    return polled.value


async def _resolve_code_block_otp_for_identifier(
    totp_identifier: str,
    organization_id: str | None,
    workflow_run_id: str | None,
    *,
    budget_seconds: int,
) -> OTPResult:
    """Resolve a fresh OTP for a bare email/SMS identifier, with no credential in play."""
    if not workflow_run_id:
        raise CodeBlockOTPError("OTP is unavailable: no workflow run is associated with this code block.")
    workflow_run_context = _code_block_workflow_run_context(workflow_run_id)

    polled = await _poll_code_block_otp(
        totp_identifier,
        organization_id,
        workflow_run_id,
        workflow_run_context,
        budget_seconds=budget_seconds,
        subject="this address",
        expected_otp_type=OTPType.TOTP,
    )
    return OTPResult(polled.value, polled.get_otp_type())


def _bind_code_block_otp(
    credential_parameter_key: str,
    organization_id: str | None,
    workflow_run_id: str | None,
) -> Callable[[], Awaitable[str]]:
    """Build the awaitable ``otp`` method bound onto a code block's Credential, closing over
    only opaque ids so the seed stays unreachable from the method's cells."""

    async def otp() -> str:
        return await _resolve_code_block_otp(
            credential_parameter_key,
            organization_id,
            workflow_run_id,
            budget_seconds=settings.CODE_BLOCK_OTP_POLL_TIMEOUT_SECONDS,
        )

    return otp


def _bind_code_block_magic_link(
    credential_parameter_key: str,
    organization_id: str | None,
    workflow_run_id: str | None,
    expected_page: Page | RecordingPage | None = None,
) -> Callable[[Page | RecordingPage], Awaitable[None]]:
    """Build the awaitable ``magic_link`` method bound onto a code block's Credential.

    Takes the page as an argument rather than closing over it, mirroring ``solve_captcha(page)``.
    ``expected_page`` is the page the block will actually run on; the caller rebinds with it once
    that page exists, so the argument can be checked by identity rather than by capability.
    """

    async def magic_link(page: Page | RecordingPage) -> None:
        # Authored code hands the page in, and it receives the link through whatever goto() it
        # carries. Only identity is unforgeable here: a structural capability check is satisfiable
        # by a class the block defines itself. The structural fallback covers the bind-time window
        # before the page exists, which no authored code can reach.
        candidate: object = page
        if expected_page is not None:
            if candidate is not expected_page:
                raise CodeBlockOTPError("magic_link requires the code block's page.")
        elif not is_page_like(candidate):
            raise CodeBlockOTPError("magic_link requires the code block's page.")
        link = await _resolve_code_block_magic_link(
            credential_parameter_key,
            organization_id,
            workflow_run_id,
            budget_seconds=settings.CODE_BLOCK_OTP_POLL_TIMEOUT_SECONDS,
        )
        await navigate_with_retry(
            navigate=lambda strategy: page.goto(link, timeout=settings.BROWSER_LOADING_TIMEOUT_MS, wait_until=strategy),
            url=link,
            retry_times=NAVIGATION_MAX_RETRY_TIME,
            settle=default_navigation_settle,
            log_url=redact_url_secrets(link),
        )

    return magic_link


async def _code_block_otp_builtin(
    credential: object,
    *,
    organization_id: str | None = None,
    workflow_run_id: str | None = None,
) -> str:
    """Top-level ``await otp(...)`` sugar. Given a credential it forwards to that credential's
    bound otp(), which returns a bare code string; given a bare email/SMS address it resolves
    against the address alone and returns an OTPResult that knows whether it is a code or a link."""
    if isinstance(credential, str):
        return await _resolve_code_block_otp_for_identifier(
            credential,
            organization_id,
            workflow_run_id,
            budget_seconds=settings.CODE_BLOCK_OTP_POLL_TIMEOUT_SECONDS,
        )
    bound = getattr(credential, "otp", None)
    if not callable(bound):
        raise CodeBlockOTPError("otp() expects a credential with an OTP source, or an email address.")
    return await bound()


_NO_METACLASS = object()


def _code_block_build_class(func: Any, name: str, /, *bases: Any, metaclass: Any = _NO_METACLASS, **kwds: Any) -> Any:
    # Backs implicit `class` statements; any explicit class-head keyword (even metaclass=None)
    # stays out of the sandbox. The sentinel distinguishes "no metaclass" from an explicit None.
    if metaclass is not _NO_METACLASS or kwds:
        raise RuntimeError("Code blocks do not support metaclasses or keywords in class definitions")
    return builtins.__build_class__(func, name, *bases)


_DOWNLOAD_CLAIM_TIMEOUT_SECONDS = 60
_DOWNLOAD_CLAIM_SESSION_OBSERVE_SECONDS = 10
_DOWNLOAD_CLAIM_CLICK_TIMEOUT_SECONDS = 15
_DOWNLOAD_CLAIM_EVIDENCE_TIMEOUT_SECONDS = 10
_DOWNLOAD_CLAIM_FALLBACK_STEM = "downloaded_file"

DownloadEvidenceProbe = Callable[[], Awaitable[tuple[list[FileInfo] | None, set[str]]]]
_DownloadIdentity = tuple[str | None, str | None]


def _download_monitor_owns_binding(page: Page | RecordingPage) -> bool:
    """True when the CDP download monitor, not the browser, is delivering this context's downloads.

    The monitor holds ``{behavior: deny, eventsEnabled: True}`` and fetches files over HTTP itself,
    so Playwright reports a delivered download as cancelled (``cdp_download_interceptor.py``)."""
    interceptor: CDPDownloadInterceptor | None = getattr(page.context, "_skyvern_cdp_download_interceptor", None)
    if interceptor is None:
        return False
    return interceptor.is_monitoring_browser_downloads()


async def _registered_download_identities(
    download_evidence: DownloadEvidenceProbe | None,
) -> dict[_DownloadIdentity, str] | None:
    """Registered downloads keyed by an identity two reads agree on, or ``None`` when registration
    could not be read — never proof that nothing arrived. The probe re-runs registration, so it is
    bounded here rather than left to spend its own minutes-long budget on a failing claim."""
    if download_evidence is None:
        return None
    try:
        async with asyncio.timeout(_DOWNLOAD_CLAIM_EVIDENCE_TIMEOUT_SECONDS):
            registered, _ = await download_evidence()
    except Exception:
        LOG.warning("Code block download claim could not read registration evidence", exc_info=True)
        return None
    if registered is None:
        return None
    return {(file_info.filename, file_info.checksum): file_info.filename or "" for file_info in registered}


async def _newly_registered_download_name(
    download_evidence: DownloadEvidenceProbe | None,
    registered_before_click: dict[_DownloadIdentity, str] | None,
) -> str | None:
    """The name of a file this click registered, or ``None`` when no new registration can be proven.

    A block that downloaded earlier already has registered files, so only a delta is evidence that
    the click delivered anything."""
    if registered_before_click is None:
        return None
    registered_after_click = await _registered_download_identities(download_evidence)
    if registered_after_click is None:
        return None
    new_names = sorted(
        name for identity, name in registered_after_click.items() if identity not in registered_before_click and name
    )
    if not new_names:
        return None
    return normalize_download_filename(new_names[0]) or _DOWNLOAD_CLAIM_FALLBACK_STEM


async def _code_block_click_and_claim_download_builtin(
    page: Page | RecordingPage,
    selector: str,
    *,
    organization_id: str | None = None,
    workflow_run_id: str | None = None,
    download_binding: DownloadBinding | None = None,
    download_evidence: DownloadEvidenceProbe | None = None,
) -> str:
    """Click ``selector`` once and confirm the browser download it fires, returning the sanitized
    suggested filename as a summary.

    The helper never touches the bytes. Whichever destination this run is bound to already has a
    writer — the browser writing into the run download directory, or the session watcher on a
    provider-owned destination — and the execution layer registers from there. Copying or replaying
    here would add a second writer to a single-writer system (SKY-11371) and double-register the
    delivered file, so the only thing this operation owns is the click and the event."""
    if download_binding is None or download_binding not in (DownloadBinding.RUN_DIR, DownloadBinding.SESSION_DIR):
        raise CodeBlockDownloadClaimError(
            "click_and_claim_download is only supported when downloads are bound to a known destination."
        )
    resolved_selector = str(selector or "").strip()
    if not resolved_selector:
        raise CodeBlockDownloadClaimError("click_and_claim_download requires the selector of the affordance to click.")
    click_error: BaseException | None = None
    # Only a binding whose delivery this page cannot observe can reach the grace branch below, so
    # the extra registration read is taken for those claims alone.
    registered_before_click = (
        await _registered_download_identities(download_evidence)
        if download_binding is DownloadBinding.SESSION_DIR or _download_monitor_owns_binding(page)
        else None
    )
    # A provider-owned session delivers the bytes on its own connection, so this page may never see
    # the event at all (SKY-11371). Wait briefly for one rather than spending the full window on an
    # event that is not this binding's proof of delivery.
    observe_seconds = (
        _DOWNLOAD_CLAIM_SESSION_OBSERVE_SECONDS
        if download_binding is DownloadBinding.SESSION_DIR
        else _DOWNLOAD_CLAIM_TIMEOUT_SECONDS
    )
    try:
        async with page.expect_download(timeout=observe_seconds * 1000) as claim:
            try:
                await page.locator(resolved_selector).click(timeout=_DOWNLOAD_CLAIM_CLICK_TIMEOUT_SECONDS * 1000)
            except Exception as exc:
                click_error = exc
                raise
        download = await claim.value
    except CodeBlockDownloadClaimError:
        raise
    except Exception as exc:
        # Whether a monitor owned the binding is the first question asked of a claim that saw no
        # event, and it is unanswerable after the fact.
        claim_monitor_owns_binding = _download_monitor_owns_binding(page)
        LOG.info(
            "codeblock.download_claim_decision",
            engaged=False,
            binding=download_binding.value,
            monitor_owns_binding=claim_monitor_owns_binding,
            click_completed_without_error=click_error is None,
            workflow_run_id=workflow_run_id,
            organization_id=organization_id,
        )
        delivered_name = (
            await _newly_registered_download_name(download_evidence, registered_before_click)
            if download_binding is DownloadBinding.SESSION_DIR or claim_monitor_owns_binding
            else None
        )
        if click_error is not None:
            if delivered_name is None:
                # A selector that no longer matches is a page failure, not a download that failed to
                # fire. Re-raise Playwright's own error so the healing path still recognises its type.
                raise click_error
            # The click did not return, but this binding's writer registered a file it started, so
            # failing here would fail a download that arrived.
            return delivered_name
        if download_binding is DownloadBinding.SESSION_DIR or claim_monitor_owns_binding:
            # The click landed, and this binding's delivery belongs to the session watcher or to the
            # monitor -- which denies browser-native downloads and fetches the bytes itself, so no
            # Download event need ever reach this page. Failing here would fail a download that
            # succeeded; the execution layer still reports an unregistered intent when nothing
            # arrives.
            return delivered_name or _DOWNLOAD_CLAIM_FALLBACK_STEM
        raise CodeBlockDownloadClaimError("Clicking the affordance did not fire a browser download.") from exc

    monitor_owns_binding = _download_monitor_owns_binding(page)
    # An event that fired is not a download that finished: a cancelled or failed transfer would
    # otherwise be reported as a delivery the registration scan then cannot find.
    failure: str | None = None
    outcome_readable = True
    try:
        failure = await download.failure()
    except Exception:
        outcome_readable = False
        LOG.debug("Code block download claim could not read the download outcome")
    # The suggested filename can carry customer document metadata, so only its fingerprint is logged.
    LOG.info(
        "codeblock.download_claim_decision",
        engaged=outcome_readable and failure is None,
        binding=download_binding.value,
        monitor_owns_binding=monitor_owns_binding,
        suggested_filename_fp=diagnostic_fingerprint(download.suggested_filename),
        workflow_run_id=workflow_run_id,
        organization_id=organization_id,
    )
    summary = normalize_download_filename(download.suggested_filename or "") or _DOWNLOAD_CLAIM_FALLBACK_STEM
    if monitor_owns_binding:
        # The monitor denies browser-native downloads and fetches the file itself, so Playwright
        # reports a delivered download as cancelled. Delivery is the monitor's to prove here.
        return summary
    if not outcome_readable:
        raise CodeBlockDownloadClaimError("The browser download outcome could not be confirmed.")
    if failure:
        raise CodeBlockDownloadClaimError(f"The browser download did not complete: {failure}")
    return summary


def _bind_code_block_download_claim(
    *,
    organization_id: str | None,
    workflow_run_id: str | None,
    download_binding: DownloadBinding | None,
    download_evidence: DownloadEvidenceProbe | None = None,
) -> Callable[[Page | RecordingPage, str], Awaitable[str]]:
    """Two-argument closure rather than a `partial`: keyword
    defaults on a `partial` would let block code override the run identity it is bound to."""

    async def click_and_claim_download(page: Page | RecordingPage, selector: str) -> str:
        return await _code_block_click_and_claim_download_builtin(
            page,
            selector,
            organization_id=organization_id,
            workflow_run_id=workflow_run_id,
            download_binding=download_binding,
            download_evidence=download_evidence,
        )

    return click_and_claim_download


def _bind_code_block_search_web(
    page: Page | RecordingPage | None,
) -> Callable[[str, int], Awaitable[web_search.WebSearchObservation]]:
    """Closure rather than a `partial`: a keyword default on a `partial` would let block code
    override the page whose browser context the search is fetched through."""

    async def search_web(query: str, max_results: int = 10) -> web_search.WebSearchObservation:
        if page is None:
            raise RuntimeError("search_web is only supported while the run browser is open.")
        return await web_search.search_web(
            app.AGENT_FUNCTION.web_search_provider(),
            web_search.RunBrowserTransport(page.context),
            query,
            max_results,
        )

    return search_web


class CodeBlock(Block):
    # There is a mypy bug with Literal. Without the type: ignore, mypy will raise an error:
    # Parameter 1 of Literal[...] cannot be of type "Any"
    block_type: Literal[BlockType.CODE] = BlockType.CODE  # type: ignore

    code: str
    parameters: list[PARAMETER_TYPE] = []
    error_code_mapping: dict[str, str] | None = None
    prompt: str | None = None
    steps: list[CodeBlockStep] | None = None

    BLOCKED_ATTRS: ClassVar[frozenset[str]] = CODE_BLOCK_BLOCKED_ATTRS

    execute_safe = _execute_parameter_observing_block_safe

    TEMPLATABLE_FIELDS: ClassVar[frozenset[str]] = frozenset({"code", "error_code_mapping", "prompt"})

    def _effective_error_code_mapping(self, workflow_run_context: WorkflowRunContext) -> dict[str, str]:
        return dict(self.error_code_mapping or {})

    def _extract_declared_error(
        self, exception: Exception, workflow_run_context: WorkflowRunContext
    ) -> UserDefinedError | None:
        if type(exception) is not ErrorCode:
            return None
        failing_line = user_code_line_from_exception(exception)
        try:
            tree = ast.parse(self.code)
        except SyntaxError:
            return None
        raises = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Raise)
            and failing_line is not None
            and node.lineno <= failing_line <= (node.end_lineno or node.lineno)
        ]
        if len(raises) != 1:
            return None
        try:
            direct_raises = _direct_code_block_error_code_raises(self.code)
        except ValueError:
            # Imperfect authored drafts may reach runtime; invalid direct-raise syntax is not a declared user error.
            return None
        if (failing_line, exception.error_code) not in direct_raises:
            return None
        if exception.error_code not in self._effective_error_code_mapping(workflow_run_context):
            return None
        if self._contains_registered_secret(exception.error_code, workflow_run_context):
            return None
        reasoning = "".join(c for c in exception.reasoning if unicodedata.category(c)[0] != "C").strip()
        redacted = self._redact_registered_secrets(reasoning, workflow_run_context) or "[redacted]"
        masked = workflow_run_context.mask_secrets_in_data(redacted)
        if not isinstance(masked, str) or not masked:
            return None
        return UserDefinedError(error_code=exception.error_code, reasoning=masked, confidence_float=1.0)

    @staticmethod
    def is_safe_code(code: str) -> None:
        _shared_is_safe_code(code)

    @staticmethod
    def build_safe_vars() -> dict[str, Any]:
        return {
            "__builtins__": {
                # Only allow several builtins due to security concerns. LOAD_BUILD_CLASS and the
                # class body's implicit __module__ binding resolve these from here, not globals.
                "__build_class__": _code_block_build_class,
                "__name__": "skyvern.code_block",
            },
            "print": print,
            "len": len,
            "range": range,
            "str": str,
            "int": int,
            "float": float,
            "dict": dict,
            "list": list,
            "tuple": tuple,
            "set": set,
            "bool": bool,
            "isinstance": isinstance,
            "enumerate": enumerate,
            "any": any,
            "all": all,
            "max": max,
            "min": min,
            "sum": sum,
            "sorted": sorted,
            "sleep": asyncio.sleep,
            "asyncio": SimpleNamespace(sleep=asyncio.sleep),
            "re": SimpleNamespace(
                match=re.match,
                search=re.search,
                findall=re.findall,
                finditer=re.finditer,
                fullmatch=re.fullmatch,
                sub=re.sub,
                compile=re.compile,
                split=re.split,
                escape=re.escape,
                I=re.I,
                S=re.S,
                IGNORECASE=re.IGNORECASE,
                MULTILINE=re.MULTILINE,
                DOTALL=re.DOTALL,
            ),
            "json": SimpleNamespace(dumps=json.dumps, loads=json.loads),
            "html": SimpleNamespace(escape=html.escape),
            "Exception": Exception,
            "ErrorCode": ErrorCode,
            "otp": _code_block_otp_builtin,
            "solve_captcha": _code_block_solve_captcha_builtin,
            # Unbound: carries no run destination, so it fails closed. generate_async_user_function
            # replaces it with the run-bound helper; the name is present so preflight allows it.
            "click_and_claim_download": _bind_code_block_download_claim(
                organization_id=None, workflow_run_id=None, download_binding=None
            ),
            "search_web": _bind_code_block_search_web(None),
        }

    def generate_async_user_function(
        self,
        code: str,
        page: Page | RecordingPage,
        parameters: dict[str, Any] | None = None,
        *,
        workflow_run_id: str | None = None,
        organization_id: str | None = None,
        workflow_run_block_id: str | None = None,
        download_run_id: str | None = None,
        download_binding: DownloadBinding | None = None,
        download_evidence: DownloadEvidenceProbe | None = None,
    ) -> Callable[[], Awaitable[dict[str, Any]]]:
        # SECURITY: validate before exec(). The AST check must run on the raw
        # user code so it can block dunder identifiers like __capture_locals.
        self.is_safe_code(code)
        try:
            _validate_code_block_error_code_calls(textwrap.dedent(code))
        except (SyntaxError, ValueError) as exc:
            syntax_error = SyntaxError(str(exc))
            validation_error_cause = exc

            async def raise_rendered_validation_error() -> dict[str, Any]:
                if workflow_run_id is not None:
                    failure_reason = f"Failed to execute code block. Reason: SyntaxError: {syntax_error}"
                    failure_output = build_block_failure_output(failure_reason, [])
                    await self.record_output_parameter_value(
                        self.get_workflow_run_context(workflow_run_id), workflow_run_id, failure_output
                    )
                raise syntax_error from validation_error_cause

            return raise_rendered_validation_error
        code_sha256 = hashlib.sha256(code.encode("utf-8")).hexdigest()
        code_len = len(code)
        code = textwrap.indent(textwrap.dedent(code), "    ")
        runtime_variables: dict[str, Callable[[], Awaitable[dict[str, Any]]]] = {}
        safe_vars = self.build_safe_vars()
        safe_vars["print"] = partial(
            _code_block_safe_print,
            parameters=app.AGENT_FUNCTION.serialize_codeblock_parameters(parameters or {}),
        )
        safe_vars["solve_captcha"] = partial(
            _code_block_solve_captcha_builtin,
            organization_id=organization_id,
            workflow_run_id=workflow_run_id,
        )
        safe_vars["otp"] = partial(
            _code_block_otp_builtin,
            organization_id=organization_id,
            workflow_run_id=workflow_run_id,
        )
        safe_vars["click_and_claim_download"] = _bind_code_block_download_claim(
            organization_id=organization_id,
            workflow_run_id=workflow_run_id,
            download_binding=download_binding,
            download_evidence=download_evidence,
        )
        safe_vars["search_web"] = _bind_code_block_search_web(page)
        parameter_defaults: dict[str, Any] = {}
        if parameters:
            for key, value in parameters.items():
                if key not in safe_vars:
                    # Rebind against the page this block actually runs on. The bind above happens
                    # before the page exists, and only object identity is unforgeable: authored code
                    # can define a class carrying any capability a structural check looks for, and
                    # would then receive the sign-in link through its own goto().
                    if isinstance(value, Credential):
                        value.magic_link = _bind_code_block_magic_link(
                            key, organization_id, workflow_run_id, expected_page=page
                        )
                    safe_vars[key] = value
                    if key.isidentifier() and not keyword.iskeyword(key) and not key.startswith("__"):
                        parameter_defaults[key] = value
        default_args = ", ".join(f"{key}=__param_defaults[{key!r}]" for key in parameter_defaults)
        full_code = f"""
async def wrapper({default_args}):
{code}
    return __capture_locals()
"""
        safe_vars["page"] = page
        safe_vars["__capture_locals"] = locals
        safe_vars["__param_defaults"] = parameter_defaults
        # Compile under a recognizable filename so tracebacks map back to user code lines.
        compiled_code = compile(full_code, CODE_BLOCK_FILENAME, "exec")
        inline_exec_context = skyvern_context.current()
        LOG.info(
            "codeblock.inline_exec_entered",
            code_sha256=code_sha256,
            code_len=code_len,
            in_process=True,
            pid=os.getpid(),
            hostname=socket.gethostname(),
            organization_id=organization_id,
            workflow_run_id=workflow_run_id,
            workflow_run_block_id=workflow_run_block_id,
            trace_id=inline_exec_context.request_id if inline_exec_context else None,
        )
        exec(compiled_code, safe_vars, runtime_variables)  # nosemgrep
        user_function = runtime_variables["wrapper"]
        inner_function = user_function
        if parameter_defaults:
            excluded_parameter_keys = frozenset(parameter_defaults)

            async def filtered_user_function() -> dict[str, Any]:
                result: Any = await user_function()
                # An explicit `return <non-dict>` in user code yields that value directly,
                # not the __capture_locals() dict; only the implicit dict needs the injected
                # parameter keys stripped. SKY-10789: this guard avoids result.items() on a list.
                if not isinstance(result, dict):
                    return result
                return {key: value for key, value in result.items() if key not in excluded_parameter_keys}

            inner_function = filtered_user_function

        async def timed_user_function() -> dict[str, Any]:
            started_at = monotonic()
            success = False
            try:
                result: Any = await inner_function()
                success = True
                return result
            finally:
                # No await here: this runs during wait_for cancellation and must not block it.
                LOG.info(
                    "codeblock.inline_exec_completed",
                    duration_ms=round((monotonic() - started_at) * 1000, 1),
                    success=success,
                    organization_id=organization_id,
                    workflow_run_id=workflow_run_id,
                    workflow_run_block_id=workflow_run_block_id,
                )

        return timed_user_function

    @staticmethod
    async def execute_user_function_with_timeout(
        user_function: Callable[[], Awaitable[dict[str, Any]]],
        timeout_seconds: int,
    ) -> dict[str, Any]:
        if timeout_seconds <= 0:
            return await user_function()
        return await asyncio.wait_for(user_function(), timeout=timeout_seconds)

    async def _ensure_run_recording_artifact(
        self,
        browser_state: BrowserState,
        workflow_run_id: str,
        workflow_run_block_id: str,
        organization_id: str | None,
        browser_session_id: str | None = None,
    ) -> None:
        """Register the run-scoped RECORDING row a fresh-browser code block otherwise lacks; no-op when the
        browser stays open on completion (persistent session or pinned browser_address)."""
        if browser_session_id:
            return
        # get_video_artifacts returns the same VideoArtifact objects held on browser_state, so the id
        # written below is observed by the early-return guard on later blocks sharing the browser.
        # Idempotency assumes those blocks run sequentially (no concurrent first-time registration).
        browser_artifacts = browser_state.browser_artifacts
        if not browser_artifacts or all(va.video_artifact_id for va in browser_artifacts.video_artifacts):
            return
        try:
            # A pinned browser_address run also keeps its browser open, so its per-run webm never
            # finalizes — skip it like a session run and let the clip path serve the recording.
            workflow_run = await app.DATABASE.workflow_runs.get_workflow_run(workflow_run_id, organization_id)
            if workflow_run is not None and workflow_run.browser_address:
                return
            video_artifacts = await app.BROWSER_MANAGER.get_video_artifacts(
                workflow_run_id=workflow_run_id, browser_state=browser_state, finalize=False
            )
            pending_indexes = [idx for idx, va in enumerate(video_artifacts) if not va.video_artifact_id]
            if not pending_indexes:
                return
            workflow_run_block = await app.DATABASE.observer.get_workflow_run_block(
                workflow_run_block_id=workflow_run_block_id, organization_id=organization_id
            )
            for idx in pending_indexes:
                video_artifacts[idx].video_artifact_id = await app.ARTIFACT_MANAGER.create_workflow_run_block_artifact(
                    workflow_run_block=workflow_run_block,
                    artifact_type=ArtifactType.RECORDING,
                    data=video_artifacts[idx].video_data,
                )
        except Exception:
            LOG.warning(
                "Failed to register run-scoped recording artifact for code block",
                workflow_run_block_id=workflow_run_block_id,
            )

    def get_all_parameters(
        self,
        workflow_run_id: str,
    ) -> list[PARAMETER_TYPE]:
        return self.parameters

    def format_potential_template_parameters(self, workflow_run_context: WorkflowRunContext) -> None:
        masked_code, masked_comments = mask_jinja_in_python_comments(self.code)
        rendered_code = self.render_templatable_field("code", masked_code, workflow_run_context)
        self.code = restore_jinja_masked_comments(rendered_code, masked_comments)
        if self.prompt:
            self.prompt = self.render_templatable_field("prompt", self.prompt, workflow_run_context)

        # Match BaseTaskBlock: inherit first, let block entries override, then render both
        # keys and descriptions through the workflow context. Rendered entries are untrusted
        # runtime data, so invalid entries are omitted and cannot authorize a typed error.
        workflow = getattr(workflow_run_context, "workflow", None)
        definition = getattr(workflow, "workflow_definition", None) if workflow is not None else None
        workflow_mapping = (
            definition.get("error_code_mapping")
            if isinstance(definition, dict)
            else getattr(definition, "error_code_mapping", None)
        )
        self.error_code_mapping = self._render_error_code_mapping(
            self.error_code_mapping,
            workflow_mapping,
            workflow_run_context,
            for_generated_code=True,
        )

    async def _claim_session_download_artifacts(
        self,
        *,
        organization_id: str,
        workflow_run_id: str,
        workflow_run_block_id: str,
        run_id: str,
        session_bound: bool,
    ) -> None:
        """Reconcile this run's session-keyed DOWNLOAD artifacts so a run-scoped read can see them.
        The watcher binds the producing run when it observes a download; this picks up rows an older
        watcher left unbound, windowed on this block's own row so a co-tenant run's earlier downloads
        stay unclaimed."""
        context = skyvern_context.current()
        browser_session_id = context.browser_session_id if context else None
        if not session_bound:
            return
        if not browser_session_id:
            # The binding says the downloads live in a session directory but the context names no
            # session, so there is no namespace to claim from; finalization is the only backstop left.
            LOG.warning(
                "Session-bound code block has no browser session in context; skipping download claim",
                workflow_run_id=workflow_run_id,
                workflow_run_block_id=workflow_run_block_id,
            )
            return
        try:
            workflow_run_block = await app.DATABASE.observer.get_workflow_run_block(
                workflow_run_block_id=workflow_run_block_id, organization_id=organization_id
            )
            claimed = await app.DATABASE.artifacts.claim_session_download_artifacts_for_run(
                run_id=run_id,
                browser_session_id=browser_session_id,
                organization_id=organization_id,
                run_started_at=workflow_run_block.created_at,
            )
            if claimed:
                LOG.debug(
                    "Claimed session-scoped download artifacts for code block",
                    workflow_run_id=workflow_run_id,
                    workflow_run_block_id=workflow_run_block_id,
                    browser_session_id=browser_session_id,
                    claimed=claimed,
                )
        except Exception:
            LOG.warning(
                "Failed to claim session-scoped download artifacts for code block",
                workflow_run_id=workflow_run_id,
                workflow_run_block_id=workflow_run_block_id,
                browser_session_id=browser_session_id,
                exc_info=True,
            )

    def _bind_download_evidence_probe(
        self,
        *,
        engine: str,
        organization_id: str | None,
        workflow_run_id: str,
        workflow_run_block_id: str,
        session_bound: bool,
        resolved_download_id: str | None,
    ) -> DownloadEvidenceProbe:
        """The claim's read of what this run has registered, on the same authority the binding
        verdict consults — the claim never scans a directory of its own."""

        async def probe() -> tuple[list[FileInfo] | None, set[str]]:
            return await self._register_downloaded_files(
                engine=engine,
                organization_id=organization_id,
                workflow_run_id=workflow_run_id,
                workflow_run_block_id=workflow_run_block_id,
                session_bound=session_bound,
                download_run_id=resolved_download_id,
            )

        return probe

    async def _register_downloaded_files(
        self,
        *,
        engine: str,
        organization_id: str | None,
        workflow_run_id: str,
        workflow_run_block_id: str,
        session_bound: bool = False,
        download_run_id: str | None = None,
        download_binding_kind: str | None = None,
        download_operation_invoked: bool = False,
    ) -> tuple[list[FileInfo] | None, set[str]]:
        """Register downloads, recording what the registered directory held on either side.

        Bracketed here rather than in one engine's dispatch so the record is not engine-biased,
        for the same reason the unregistered-intent signal is emitted from every engine.
        """
        listed_run_id = download_run_id or workflow_run_id
        listed_dir = download_dir_path_for_run(listed_run_id)
        alt_dir = download_dir_path_for_run(workflow_run_id) if listed_run_id != workflow_run_id else None
        pre = observe_download_dir(listed_dir)
        alt_pre = observe_download_dir(alt_dir) if alt_dir is not None else None
        try:
            return await self._register_downloaded_files_inner(
                organization_id=organization_id,
                workflow_run_id=workflow_run_id,
                workflow_run_block_id=workflow_run_block_id,
                session_bound=session_bound,
                download_run_id=download_run_id,
                download_operation_invoked=download_operation_invoked,
            )
        finally:
            with contained_effect(
                "download registration visibility",
                workflow_run_id=workflow_run_id,
                workflow_run_block_id=workflow_run_block_id,
            ):
                LOG.info(
                    "codeblock.download_registration_visibility",
                    workflow_run_id=workflow_run_id,
                    workflow_run_block_id=workflow_run_block_id,
                    organization_id=organization_id,
                    engine=engine,
                    boundary="register",
                    session_bound=session_bound,
                    **classify_download_visibility(
                        pre=pre,
                        settled=None,
                        post=observe_download_dir(listed_dir),
                        alt_pre=alt_pre,
                        alt_post=observe_download_dir(alt_dir) if alt_dir is not None else None,
                        listed_run_id=listed_run_id,
                        workflow_run_id=workflow_run_id,
                        download_binding_kind=download_binding_kind,
                    ),
                )

    async def _register_downloaded_files_inner(
        self,
        *,
        organization_id: str | None,
        workflow_run_id: str,
        workflow_run_block_id: str,
        session_bound: bool = False,
        download_run_id: str | None = None,
        download_operation_invoked: bool = False,
    ) -> tuple[list[FileInfo] | None, set[str]]:
        """Registered downloads, plus the names of files whose save storage skipped.

        ``None`` is not an empty registration: workflow finalization retries the save, so a caller
        must not read a storage timeout as proof that nothing was downloaded. A partial save returns
        what did register plus the skipped names, so saved files keep their evidence while the
        binding verdict can abstain for exactly the files storage dropped."""
        # Register up front so the block output carries downloaded_file_urls for
        # downstream blocks in the same run; workflow finalization re-runs the save safely.
        if not organization_id:
            return None, set()
        storage_run_id = download_run_id or workflow_run_id
        try:
            async with asyncio.timeout(SAVE_DOWNLOADED_FILES_TIMEOUT):
                await self._claim_session_download_artifacts(
                    organization_id=organization_id,
                    workflow_run_id=workflow_run_id,
                    workflow_run_block_id=workflow_run_block_id,
                    run_id=storage_run_id,
                    session_bound=session_bound,
                )
                await app.STORAGE.save_downloaded_files(
                    organization_id=organization_id,
                    run_id=storage_run_id,
                )
        except asyncio.TimeoutError:
            LOG.warning(
                "Timeout to save downloaded files",
                workflow_run_id=workflow_run_id,
                workflow_run_block_id=workflow_run_block_id,
            )
            return None, set()
        except DownloadSaveIncompleteError as exc:
            LOG.warning(
                "Storage skipped saving some downloaded files; will retry at workflow finalization",
                workflow_run_id=workflow_run_id,
                workflow_run_block_id=workflow_run_block_id,
                skipped_file_count=len(exc.skipped_files),
            )
            partial_files = await self._read_back_downloaded_files(
                organization_id=organization_id,
                workflow_run_id=workflow_run_id,
                workflow_run_block_id=workflow_run_block_id,
                download_run_id=storage_run_id,
            )
            return partial_files, set(exc.skipped_files)
        except Exception:
            LOG.warning(
                "CodeBlock failed to register downloaded files; will retry at workflow finalization",
                workflow_run_id=workflow_run_id,
                workflow_run_block_id=workflow_run_block_id,
            )
            return None, set()
        registered_files = await self._read_back_downloaded_files(
            organization_id=organization_id,
            workflow_run_id=workflow_run_id,
            workflow_run_block_id=workflow_run_block_id,
            download_run_id=storage_run_id,
        )
        if session_bound and registered_files == []:
            registered_files = (
                await self._await_in_flight_session_download(
                    organization_id=organization_id,
                    workflow_run_id=workflow_run_id,
                    workflow_run_block_id=workflow_run_block_id,
                    run_id=storage_run_id,
                    download_operation_invoked=download_operation_invoked,
                )
                or registered_files
            )
        return registered_files, set()

    async def _await_in_flight_session_download(
        self,
        *,
        organization_id: str,
        workflow_run_id: str,
        workflow_run_block_id: str,
        run_id: str,
        download_operation_invoked: bool = False,
    ) -> list[FileInfo] | None:
        """Return a session download once its final watcher row becomes visible.

        The watcher drops the partial's artifact row on Chrome's rename and writes the final row from
        the same event batch, in either order. A visible partial or a typed broker receipt therefore
        authorizes the same bounded settle poll; neither a vanished partial nor completed browser
        bytes alone prove that the final artifact row is committed."""
        context = skyvern_context.current()
        browser_session_id = context.browser_session_id if context else None
        if not browser_session_id:
            return None
        try:
            in_flight = await app.STORAGE.list_downloading_files_in_browser_session(
                organization_id=organization_id,
                browser_session_id=browser_session_id,
            )
            if not in_flight and not download_operation_invoked:
                return None
            if in_flight:
                LOG.info(
                    "Code block finished with a session download still in flight; waiting for it",
                    workflow_run_id=workflow_run_id,
                    workflow_run_block_id=workflow_run_block_id,
                    browser_session_id=browser_session_id,
                    in_flight_count=len(in_flight),
                )
                await wait_for_download_finished(
                    downloading_files=in_flight, timeout=_CODE_BLOCK_SESSION_DOWNLOAD_WAIT_SECONDS
                )
            for _ in range(_CODE_BLOCK_SESSION_DOWNLOAD_SETTLE_ATTEMPTS):
                await self._claim_session_download_artifacts(
                    organization_id=organization_id,
                    workflow_run_id=workflow_run_id,
                    workflow_run_block_id=workflow_run_block_id,
                    run_id=run_id,
                    session_bound=True,
                )
                settled = await self._read_back_downloaded_files(
                    organization_id=organization_id,
                    workflow_run_id=workflow_run_id,
                    workflow_run_block_id=workflow_run_block_id,
                    download_run_id=run_id,
                )
                if settled:
                    return settled
                await asyncio.sleep(_CODE_BLOCK_SESSION_DOWNLOAD_SETTLE_INTERVAL_SECONDS)
        except Exception:
            LOG.warning(
                "Failed waiting for an in-flight session download; workflow finalization will claim it",
                workflow_run_id=workflow_run_id,
                workflow_run_block_id=workflow_run_block_id,
                browser_session_id=browser_session_id,
                exc_info=True,
            )
        return None

    async def _read_back_downloaded_files(
        self,
        *,
        organization_id: str | None,
        workflow_run_id: str,
        workflow_run_block_id: str,
        download_run_id: str | None = None,
    ) -> list[FileInfo] | None:
        """Registered downloads read back from storage, or ``None`` when the read could not complete.

        Excludes in-flight ``.crdownload`` partials: claiming mid-run can tag a row the browser is
        still writing, and the run-scoped read has no partial filter of its own the way the
        session-scoped one does."""
        if not organization_id:
            return None
        try:
            async with asyncio.timeout(GET_DOWNLOADED_FILES_TIMEOUT):
                files = await app.STORAGE.get_downloaded_files(
                    organization_id=organization_id,
                    run_id=download_run_id or workflow_run_id,
                )
                return [file for file in files if not (file.filename or "").endswith(BROWSER_DOWNLOADING_SUFFIX)]
        except asyncio.TimeoutError:
            LOG.warning(
                "Timeout getting downloaded files",
                workflow_run_id=workflow_run_id,
                workflow_run_block_id=workflow_run_block_id,
            )
            return None
        except Exception:
            LOG.warning(
                "CodeBlock failed to read back downloaded files; will retry at workflow finalization",
                workflow_run_id=workflow_run_id,
                workflow_run_block_id=workflow_run_block_id,
            )
            return None

    async def _bind_and_grade_downloads(
        self,
        *,
        engine: str,
        result: Any,
        downloaded_files: list[FileInfo] | None,
        download_dir_before: set[tuple[str, int, int]] | None,
        resolved_download_id: str | None,
        workflow_run_context: WorkflowRunContext,
        workflow_run_id: str,
        workflow_run_block_id: str,
        organization_id: str | None,
        skipped_file_names: set[str],
        authored_registration: bool = False,
    ) -> tuple[Any, str | None]:
        """Bind registration evidence into the block output, returning a failure reason if it cannot.

        A file that appeared in the run download directory while this block ran, with nothing
        registered to bind, is a download the run can never account for — the block reports that
        rather than persisting an output that reads as if no download happened. The verdict needs
        evidence on both sides: when registration or either directory snapshot could not be read,
        the block is not accused of swallowing a download that storage merely failed to record."""
        organization_id = organization_id or workflow_run_context.organization_id
        if downloaded_files is None:
            LOG.warning(
                "codeblock.download_binding_verdict_skipped",
                engine=engine,
                reason="registration_incomplete",
                workflow_run_id=workflow_run_id,
                workflow_run_block_id=workflow_run_block_id,
                block_label=self.label,
                organization_id=organization_id,
            )
            # No host verdict at all, so the payload is left exactly as the code returned it:
            # dropping its keys would assert an absence just as unproven as the claim.
            # Storage trouble is exactly when this diagnostic matters, so it fires here too.
            await self._record_unregistered_download_intent(
                engine=engine,
                registered=authored_registration,
                output=result,
                workflow_run_context=workflow_run_context,
                workflow_run_id=workflow_run_id,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )
            return result, None
        current_context = skyvern_context.current()
        registered_file_names = {file_info.filename for file_info in downloaded_files}
        downloaded_files = filter_downloaded_files_for_current_iteration(
            downloaded_files,
            current_context.loop_internal_state if current_context else None,
        )
        output = bind_downloaded_files_to_output(result, downloaded_files)
        await self._record_unregistered_download_intent(
            engine=engine,
            registered=bool(downloaded_files) or authored_registration,
            output=output,
            workflow_run_context=workflow_run_context,
            workflow_run_id=workflow_run_id,
            workflow_run_block_id=workflow_run_block_id,
            organization_id=organization_id,
        )

        def _log_verdict_skipped_for_partial_save() -> None:
            # A partial save may have skipped exactly the file a verdict would blame the code
            # for; emitted only on abstaining exits, so the log never coexists with an accusation.
            if skipped_file_names:
                LOG.warning(
                    "codeblock.download_binding_verdict_skipped",
                    engine=engine,
                    reason="registration_incomplete",
                    workflow_run_id=workflow_run_id,
                    workflow_run_block_id=workflow_run_block_id,
                    block_label=self.label,
                    organization_id=organization_id,
                )

        if downloaded_files:
            _log_verdict_skipped_for_partial_save()
            return output, None
        download_dir_after = local_download_dir_file_identities(resolved_download_id)
        if download_dir_after is None or download_dir_before is None:
            LOG.warning(
                "codeblock.download_binding_verdict_skipped",
                engine=engine,
                reason="download_dir_snapshot_unreadable",
                workflow_run_id=workflow_run_id,
                workflow_run_block_id=workflow_run_block_id,
                block_label=self.label,
                organization_id=organization_id,
            )
            return output, None
        new_files = download_dir_after - download_dir_before
        if not new_files:
            _log_verdict_skipped_for_partial_save()
            return output, None
        unaccounted = {name for name, _, _ in new_files} - registered_file_names - skipped_file_names
        if not unaccounted:
            # Every new file is either registered to this run (the filter above merely attributed
            # it elsewhere) or named by storage as skipped — neither is a download the code swallowed.
            _log_verdict_skipped_for_partial_save()
            return output, None
        LOG.error(
            "codeblock.download_on_disk_unbound",
            engine=engine,
            workflow_run_id=workflow_run_id,
            workflow_run_block_id=workflow_run_block_id,
            block_label=self.label,
            new_file_count=len(unaccounted),
            organization_id=organization_id,
        )
        return unbound_download_output(output), DOWNLOAD_BINDING_FAILURE_REASON

    async def _materialize_file_parameter_path(
        self,
        value: str | dict[str, Any] | None,
        *,
        workflow_run_id: str,
        organization_id: str | None,
    ) -> str | dict[str, Any] | None:
        uri: str | None = None
        if isinstance(value, str):
            uri = value
        elif isinstance(value, dict):
            uri = value.get("s3uri")
        if not uri or not str(uri).strip():
            return value
        if str(uri).startswith("/"):
            try:
                local_path = validate_local_file_path(str(uri), workflow_run_id)
                if not os.path.isfile(local_path):
                    raise FileNotFoundError(f"Local file not found: {uri}")
                return local_path
            except FileNotFoundError:
                pass
            except PermissionError:
                LOG.warning(
                    "CodeBlock file parameter path is outside the run's download directory; leaving the original value",
                    workflow_run_id=workflow_run_id,
                )
                return value
        try:
            output_dir = get_download_dir(workflow_run_id)
            local_path = await download_file(str(uri), output_dir=output_dir, organization_id=organization_id)
            # download_file routes managed storage (s3/azure) to a temp file outside the run
            # dir; copy it under the run dir so it passes validate_local_file_path containment.
            resolved = os.path.realpath(local_path)
            allowed_dir = os.path.realpath(output_dir)
            if not resolved.startswith(allowed_dir + os.sep):
                contained = os.path.join(output_dir, os.path.basename(local_path))
                shutil.copyfile(local_path, contained)
                local_path = contained
            return validate_local_file_path(local_path, workflow_run_id)
        except Exception:
            LOG.warning(
                "Failed to materialize file parameter to a local path; leaving the original value",
                workflow_run_id=workflow_run_id,
            )
            return value

    def _match_step_for_failing_line(self, failing_line: int) -> CodeBlockStep | None:
        """Advisory nearest-preceding-start match; a lone ``line_start`` (``line_end`` None) is
        open-ended. Step spans are display-oriented and can drift, so a miss never blocks the heal."""
        best: CodeBlockStep | None = None
        best_start = -1
        for step in self.steps or []:
            if step.line_start is None or step.line_start > failing_line:
                continue
            if step.line_end is not None and failing_line > step.line_end:
                continue
            if step.line_start > best_start:
                best = step
                best_start = step.line_start
        return best

    def _static_goto_url_from_line(self, line: str) -> str | None:
        # AST-only on purpose: substring/regex matching would also fire on comments and string
        # literals containing ".goto(", handing element-rot heals a URL they must never get.
        stripped = line.strip()
        if not stripped:
            return None
        try:
            # Parses one physical line at a time, so a goto(...) call whose args wrap onto another
            # line SyntaxErrors here and is treated as "no goto on this line" (safe, not a false match).
            tree = ast.parse(f"async def _probe():\n    {stripped}", mode="exec")
        except SyntaxError:
            return None
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not isinstance(node.func, ast.Attribute) or node.func.attr != "goto":
                continue
            # Supported authoring forms are positional (page.goto("...")) and keyword
            # (page.goto(url="...")); check positional first, then fall back to url=.
            if node.args:
                url_node: ast.expr | None = node.args[0]
            else:
                url_node = next((keyword.value for keyword in node.keywords if keyword.arg == "url"), None)
            if url_node is None:
                return ""
            if not isinstance(url_node, ast.Constant) or not isinstance(url_node.value, str):
                return ""
            parsed = urlparse(url_node.value)
            return url_node.value if parsed.scheme in {"http", "https"} and parsed.netloc else ""
        return None

    def _static_url_from_goal(self) -> str:
        # The goal is human free text (prompt + step descriptions), not code, so a URL regex is
        # appropriate here (unlike the AST-only code scan). This is the authored destination the
        # heal should reach when the block's own navigation rotted. Returns the first well-formed
        # absolute http(s) URL, else "".
        texts = [self.prompt or ""]
        if self.steps:
            texts.extend(step.description or "" for step in self.steps)
        for text in texts:
            match = re.search(r"https?://[^\s'\"<>)\]]+", text)
            if not match:
                continue
            candidate = match.group(0).rstrip(".,;")
            parsed = urlparse(candidate)
            if parsed.scheme in {"http", "https"} and parsed.netloc:
                return candidate
        return ""

    def _derive_escalation_navigation_url(self, failing_line: int, recording_page: RecordingPage) -> str:
        code_lines = self.code.splitlines()
        failing_idx = failing_line - 1
        failing_source_line = code_lines[failing_idx] if 0 <= failing_idx < len(code_lines) else ""

        # None = no goto on the failing line; "" = a goto with a non-static arg; a str = static url.
        failing_goto_url = self._static_goto_url_from_line(failing_source_line)
        failing_line_is_goto = failing_goto_url is not None

        try:
            page_url = recording_page.url
        except Exception:
            page_url = None
        current_url = page_url if isinstance(page_url, str) else None
        is_error_seat = bool(current_url and current_url.startswith("chrome-error://"))

        # Only a navigation-failure / error-page seat gets navigation recourse — element-rot heals
        # must never navigate away from live SPA state (H8).
        if not (failing_line_is_goto or is_error_seat):
            return ""

        # The block goal is the source of truth for the intended destination; the code's own goto is
        # what rotted (e.g. the host went dead), so prefer a URL the goal names over re-navigating to
        # the failing goto's possibly-dead target. Fall back to the code goto only when the goal
        # names no static URL (a real goto that merely failed transiently is still worth a retry).
        goal_url = self._static_url_from_goal()
        if goal_url:
            return goal_url

        if failing_line_is_goto:
            return failing_goto_url or ""

        # Error-page seat with a non-goto failing line: walk backward for the nearest STATIC goto,
        # not the first in the block — an earlier goto can be stale, a later one hasn't executed.
        for line in reversed(code_lines[:failing_idx]):
            line_url = self._static_goto_url_from_line(line)
            if line_url:
                return line_url
        return ""

    def _matched_step_index_for_failing_line(self, failing_line: int | None) -> int | None:
        if failing_line is None:
            return None
        matched_step = self._match_step_for_failing_line(failing_line)
        if matched_step is None:
            return None
        steps = self.steps or []
        for idx, step in enumerate(steps):
            if step is matched_step:
                return idx
        return None

    def _compose_heal_goal(self, *, workflow_run_context: WorkflowRunContext, failing_line: int | None) -> str:
        safe_main = workflow_run_context.mask_secrets_in_data(self.prompt or "")
        # Steps are a code-derived outline, not an authored goal, so they may only narrow one.
        # The harness path reaches here without the floor path's `if not self.prompt` gate, and
        # a goal-less block would otherwise aim the recovery agent at a live page with a bare
        # step description and no context.
        if not self.prompt:
            return safe_main
        matched_step = self._match_step_for_failing_line(failing_line) if failing_line is not None else None
        if matched_step is None or not matched_step.description:
            return safe_main
        steps = self.steps or []
        matched_index = next((idx for idx, step in enumerate(steps) if step is matched_step), None)
        if matched_index is None:
            matched_index = len(steps) - 1
        descriptions = [matched_step.description] + [
            step.description for step in steps[matched_index + 1 :] if step.description
        ]
        safe_mini = "\nThen: ".join(
            workflow_run_context.mask_secrets_in_data(description) for description in descriptions
        )
        return compose_mini_goal(main_goal=safe_main, mini_goal=safe_mini)

    async def _record_unregistered_download_intent(
        self,
        *,
        engine: str,
        registered: bool,
        output: object,
        workflow_run_context: WorkflowRunContext,
        workflow_run_id: str,
        workflow_run_block_id: str,
        organization_id: str | None,
    ) -> None:
        """Record a block that authors a browser download but registered no file.

        Telemetry only, and deliberately not outcome authority: the AST shape is evadable and
        authorship is not a promise. The outcome belongs to the workflow's persisted completion
        contract, graded at run finalization; this record's deletion condition is that coverage.
        Emitted from every execution engine so the prevalence it measures is not engine-biased.
        """
        if registered or not code_is_download_intent(self.code):
            return
        LOG.warning(
            "codeblock.download_intent_unregistered",
            engine=engine,
            copilot_authored=await self._workflow_is_copilot_authored(workflow_run_context),
            workflow_run_id=workflow_run_id,
            workflow_run_block_id=workflow_run_block_id,
            block_label=self.label,
            code_sha256=hashlib.sha256(self.code.encode()).hexdigest(),
            result_key_count=len(output) if isinstance(output, dict) else None,
            organization_id=organization_id,
        )

    async def _workflow_is_copilot_authored(self, workflow_run_context: WorkflowRunContext) -> bool:
        workflow = workflow_run_context.workflow
        if workflow is None:
            return False
        if "copilot" in (workflow.created_by, workflow.edited_by):
            return True
        # User saves re-stamp both fields with the user id, so the current version alone is
        # not durable; fall back to lineage (copilot stamps every version it writes). A lookup
        # failure must fail closed — callers gate copilot-only behavior on this, and it must
        # never mask a block failure.
        try:
            copilot_authored = await app.DATABASE.workflows.is_workflow_copilot_authored(
                workflow_permanent_id=workflow.workflow_permanent_id,
                organization_id=workflow.organization_id,
            )
            return copilot_authored is True
        except Exception:
            LOG.warning(
                "Copilot-lineage lookup failed; failing closed",
                workflow_permanent_id=workflow.workflow_permanent_id,
            )
            return False

    async def _self_heal_enabled(self, workflow_run_context: WorkflowRunContext) -> bool:
        # User-facing per-workflow setting, restricted to copilot-authored workflows —
        # pre-copilot code blocks must never gain agentic recovery from the toggle alone.
        # The env default stays as the OSS/standalone and local-dev override.
        if settings.ENABLE_CODE_BLOCK_SELF_HEALING:
            return True
        workflow = workflow_run_context.workflow
        if workflow is None or not workflow.enable_self_healing:
            return False
        return await self._workflow_is_copilot_authored(workflow_run_context)

    def _is_healable_page_failure(
        self,
        exception: Exception,
        recording_page: RecordingPage,
        engine_selection: BrowserEngineSelection | None = None,
    ) -> bool:
        """Heal genuine page failures only: a recorded page call raised, or an (unmapped) Playwright
        page error surfaced. A deliberate non-Playwright raise in user logic stays non-healable."""
        # A credential-site refusal is a safety decision, not a page defect. Healing it would hand
        # the off-site sign-in to an agent on the same live session and retry what was just refused.
        if isinstance(exception, CodeBlockCredentialReleaseError):
            return False
        if recording_page.last_recorded_exception() is exception:
            return True
        if engine_selection is not None:
            return engine_selection.is_engine_error(exception) is True
        return is_driver_error(exception)  # locator/timeout/navigation errors subclass the driver's base

    async def _finalize_recovery_block(
        self,
        recovery_block_id: str | None,
        status: BlockStatus,
        organization_id: str | None,
        failure_reason: str | None = None,
    ) -> None:
        # The child recovery block surfaces the heal's actions on the run timeline (parented to the code
        # block); keep its status synced with the heal outcome so it doesn't dangle in `running`.
        if recovery_block_id is None:
            return
        try:
            await app.DATABASE.observer.update_workflow_run_block(
                workflow_run_block_id=recovery_block_id,
                organization_id=organization_id,
                status=status,
                failure_reason=failure_reason,
            )
        except Exception:
            LOG.warning(
                "Failed to finalize self-heal recovery block",
                workflow_run_block_id=recovery_block_id,
            )

    async def _fail_escalation_task(
        self,
        escalation_task: Task | None,
        escalation_step: Step | None,
        recovery_block_id: str | None,
        organization_id: str | None,
    ) -> None:
        # Best-effort so an aborted heal never strands its escalation task/step/recovery block in `running`.
        await self._finalize_recovery_block(recovery_block_id, BlockStatus.failed, organization_id)
        if escalation_task is None:
            return
        try:
            await app.DATABASE.tasks.update_task(
                task_id=escalation_task.task_id,
                organization_id=organization_id,
                status=TaskStatus.failed,
            )
            if escalation_step is not None:
                await app.DATABASE.tasks.update_step(
                    task_id=escalation_task.task_id,
                    step_id=escalation_step.step_id,
                    status=StepStatus.failed,
                    is_last=True,
                    organization_id=organization_id,
                )
        except Exception:
            LOG.warning(
                "Failed to finalize stranded self-heal escalation task",
                task_id=escalation_task.task_id,
            )

    async def _attempt_self_heal(
        self,
        *,
        exception: Exception,
        failing_line: int | None,
        recording_page: RecordingPage,
        workflow_run_context: WorkflowRunContext,
        workflow_run_id: str,
        workflow_run_block_id: str,
        organization_id: str | None,
        browser_session_id: str | None,
        browser_state: BrowserState | None = None,
        page: Page | None = None,
        classification: HealClassification | None = None,
        record_output_parameter: bool = True,
        redaction_parameters: dict[str, Any] | None = None,
    ) -> BlockResult | None:
        """Run one bounded agent mini-run on the same workflow-run browser to finish the block's goal
        (narrowed to the failing step when one is confidently matched). Returns a BlockResult when a
        heal was attempted, or None to fall through to the caller's fail-closed path."""
        resolved_redaction_parameters = redaction_parameters or {}
        if not await self._self_heal_enabled(workflow_run_context):
            return None
        effective_classification = classification
        if effective_classification is None:
            engine_selection = (
                browser_state.engine_selection
                if browser_state and inspect.getattr_static(browser_state, "engine_selection", None) is not None
                else None
            )
            effective_classification = HealClassification(
                healable=self._is_healable_page_failure(
                    exception,
                    recording_page,
                    engine_selection,
                ),
                skip_reason=(
                    HealSkipReason.credential_off_site
                    if isinstance(exception, CodeBlockCredentialReleaseError)
                    else None
                ),
            )
        if not effective_classification.healable:
            return None
        if not self.prompt:
            return None
        if not organization_id:
            return None
        organization = await app.DATABASE.organizations.get_organization(organization_id=organization_id)
        if organization is None:
            return None

        escalation_task: Task | None = None
        escalation_step: Step | None = None
        recovery_block_id: str | None = None
        try:
            navigation_goal = self._compose_heal_goal(
                workflow_run_context=workflow_run_context,
                failing_line=failing_line,
            )

            workflow_system_prompt = (
                None
                if self.ignore_workflow_system_prompt
                else workflow_run_context.resolve_effective_workflow_system_prompt()
            )
            task_order, task_retry = await BaseTaskBlock.get_task_order(workflow_run_id, 0)
            # Bound by the global default but never above the org's per-run cap — execute_step gives
            # task.max_steps_per_run precedence over organization.max_steps_per_run.
            heal_max_steps = settings.MAX_STEPS_PER_RUN
            if organization.max_steps_per_run is not None:
                heal_max_steps = min(heal_max_steps, organization.max_steps_per_run)
            escalation_url = (
                self._derive_escalation_navigation_url(failing_line, recording_page) if failing_line is not None else ""
            )
            if escalation_url and browser_state is not None and page is not None:
                # BROWSER_MANAGER can early-return a cached browser state without ever reading
                # task.url, so escalation_task.url alone is not reliable navigation recourse.
                # Drive the live browser_state/page the heal will run on directly instead.
                try:
                    await browser_state.navigate_to_url(page=page, url=escalation_url)
                except Exception:
                    LOG.warning(
                        "Self-heal dead-nav escalation navigation failed; continuing from current page",
                        workflow_run_block_id=workflow_run_block_id,
                        workflow_run_id=workflow_run_id,
                    )
            # Kept in sync with the direct navigate above for task-record consistency; empty except
            # on dead-navigation seats (element-rot heals must never navigate — H8 same-session invariant).
            escalation_task = await app.DATABASE.tasks.create_task(
                url=escalation_url,
                title=self.label,
                navigation_goal=navigation_goal,
                data_extraction_goal=None,
                navigation_payload=None,
                organization_id=organization_id,
                workflow_run_id=workflow_run_id,
                order=task_order,
                retry=task_retry,
                max_steps_per_run=heal_max_steps,
                model=self.model,
                workflow_system_prompt=workflow_system_prompt,
                # Heal goals are action-phrased; after the page navigates, only the action
                # history can evidence completion.
                include_action_history_in_verification=True,
            )
            escalation_task = await app.DATABASE.tasks.update_task(
                task_id=escalation_task.task_id,
                organization_id=organization_id,
                status=TaskStatus.running,
            )
            escalation_step = await app.DATABASE.tasks.create_step(
                escalation_task.task_id,
                order=0,
                retry_index=0,
                organization_id=organization_id,
            )
            # Child block parented to the code block, linked to the escalation task: the timeline attaches
            # the heal's actions to this nested node (actions join by task_id), while the code block keeps
            # the seat task so its own pre-failure actions stay visible too.
            recovery_block = await app.DATABASE.observer.create_workflow_run_block(
                workflow_run_id=workflow_run_id,
                parent_workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
                task_id=escalation_task.task_id,
                label="Self-heal recovery",
                block_type=BlockType.TASK,
            )
            recovery_block_id = recovery_block.workflow_run_block_id

            # Attribute the heal's steps to the escalation task for its duration; restored in finally.
            current_context = skyvern_context.ensure_context()
            previous_task_id = current_context.task_id
            current_context.task_id = escalation_task.task_id
            try:
                # execute_step self-drives to a terminal across multiple steps (execute_all_steps),
                # bounded by the task's max_steps_per_run — not a single step.
                await app.agent.execute_step(
                    organization=organization,
                    task=escalation_task,
                    step=escalation_step,
                    task_block=None,
                    browser_session_id=browser_session_id,
                    close_browser_on_completion=False,
                    pre_resolved_browser_state=browser_state,
                )
            finally:
                current_context.task_id = previous_task_id

            updated_task = await app.DATABASE.tasks.get_task(
                task_id=escalation_task.task_id, organization_id=organization_id
            )
            if updated_task is None or not updated_task.status.is_final():
                await self._fail_escalation_task(escalation_task, escalation_step, recovery_block_id, organization_id)
                return await self.build_block_result(
                    success=False,
                    failure_reason=_redact_codeblock_failure_text(
                        f"Self-heal escalation did not reach a final status for block {self.label}",
                        resolved_redaction_parameters,
                    ),
                    output_parameter_value=None,
                    status=BlockStatus.failed,
                    workflow_run_block_id=workflow_run_block_id,
                    organization_id=organization_id,
                )

            if updated_task.status == TaskStatus.completed:
                downloaded_files: list[FileInfo] = []
                try:
                    async with asyncio.timeout(GET_DOWNLOADED_FILES_TIMEOUT):
                        downloaded_files = await app.STORAGE.get_downloaded_files(
                            organization_id=organization_id,
                            run_id=current_context.run_id if current_context.run_id else workflow_run_id,
                        )
                except asyncio.TimeoutError:
                    LOG.warning("Timeout getting downloaded files", task_id=updated_task.task_id)
                downloaded_files = filter_downloaded_files_for_current_iteration(
                    downloaded_files,
                    current_context.loop_internal_state,
                )
                task_output = TaskOutput.from_task(updated_task, downloaded_files)
                output_parameter_value = workflow_run_context.mask_secrets_in_data(task_output.model_dump())
                if record_output_parameter:
                    await self.record_output_parameter_value(
                        workflow_run_context, workflow_run_id, output_parameter_value
                    )
                await self._finalize_recovery_block(recovery_block_id, BlockStatus.completed, organization_id)
                return await self.build_block_result(
                    success=True,
                    failure_reason=None,
                    output_parameter_value=output_parameter_value,
                    status=BlockStatus.completed,
                    workflow_run_block_id=workflow_run_block_id,
                    organization_id=organization_id,
                )
            recovery_status = TASK_TO_BLOCK_STATUS.get(updated_task.status, BlockStatus.failed)
            recovery_failure_reason = _redact_codeblock_failure_text(
                updated_task.failure_reason or f"Self-heal escalation finished with status {updated_task.status}",
                resolved_redaction_parameters,
            )
            await self._finalize_recovery_block(
                recovery_block_id, recovery_status, organization_id, failure_reason=recovery_failure_reason
            )
            return await self.build_block_result(
                success=False,
                failure_reason=recovery_failure_reason,
                output_parameter_value=None,
                status=recovery_status,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )
        except asyncio.CancelledError:
            # CancelledError is BaseException, not Exception — finalize explicitly, then never swallow it.
            await self._fail_escalation_task(escalation_task, escalation_step, recovery_block_id, organization_id)
            raise
        except Exception:
            LOG.warning(
                "Code block self-heal escalation failed; falling back to fail-closed",
                workflow_run_block_id=workflow_run_block_id,
                workflow_run_id=workflow_run_id,
            )
            await self._fail_escalation_task(escalation_task, escalation_step, recovery_block_id, organization_id)
            return None

    @staticmethod
    def _is_playwright_exception_class_name(exception_class: str | None) -> bool:
        if exception_class is None:
            return False
        lowered = exception_class.lower()
        if not lowered.startswith("playwright."):
            return False
        return lowered.rsplit(".", 1)[-1].endswith("error")

    @staticmethod
    def _secure_skip_reason_for_error_code(error_code: str | None) -> HealSkipReason:
        if error_code == "timeout":
            return HealSkipReason.timeout_class
        if error_code == "insecure_code_detected":
            return HealSkipReason.insecure_code
        return HealSkipReason.unclassifiable

    def _classify_secure_runner_failure(self, failure: CodeBlockEngineFailure | None) -> HealClassification:
        if failure is None:
            return HealClassification(healable=False, skip_reason=HealSkipReason.unclassifiable)
        has_structured = (
            failure.exception_class is not None
            or failure.failing_line is not None
            or failure.healability_hint is not None
        )
        if not has_structured:
            # No healability_hint to disambiguate, so only the page-class codes heal blind;
            # user_code_error is ambiguous (could be a bare `raise`) and stays fail-closed.
            if failure.error_code in {"unsupported_page_operation", "browser_operation_failed"}:
                return HealClassification(healable=True, skip_reason=None)
            return HealClassification(
                healable=False,
                skip_reason=self._secure_skip_reason_for_error_code(failure.error_code),
            )
        if failure.healability_hint is True:
            return HealClassification(healable=True, skip_reason=None)
        if failure.healability_hint is None and self._is_playwright_exception_class_name(failure.exception_class):
            return HealClassification(healable=True, skip_reason=None)
        return HealClassification(
            healable=False,
            skip_reason=self._secure_skip_reason_for_error_code(failure.error_code),
        )

    @staticmethod
    def _workflow_definition_hash(workflow_definition: Any) -> str:
        if hasattr(workflow_definition, "model_dump"):
            payload = workflow_definition.model_dump(mode="json")
        else:
            payload = workflow_definition
        normalized = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    async def _self_heal_mutation_guard_snapshot(
        self,
        *,
        workflow_permanent_id: str,
        organization_id: str,
    ) -> tuple[int, str]:
        workflow = await app.DATABASE.workflows.get_workflow_by_permanent_id(
            workflow_permanent_id=workflow_permanent_id,
            organization_id=organization_id,
        )
        if workflow is None:
            raise RuntimeError("workflow_not_found_for_self_heal_guard")
        versions = await app.DATABASE.workflows.get_workflow_versions_by_permanent_id(
            workflow_permanent_id=workflow_permanent_id,
            organization_id=organization_id,
        )
        return len(versions), self._workflow_definition_hash(workflow.workflow_definition)

    @staticmethod
    def _output_obligation_for_heal_result(result: BlockResult | None) -> OutputObligation:
        if result is None or not result.success:
            return OutputObligation.none
        if result.output_parameter_value is not None:
            return OutputObligation.observed
        return OutputObligation.vestigial

    @staticmethod
    def _heal_parameter_binding_keys(workflow_run_context: WorkflowRunContext) -> list[str]:
        # Episode metadata is best-effort; it must never fail the heal path.
        try:
            return sorted(str(key) for key in workflow_run_context.parameters.keys())
        except Exception:
            return []

    async def _write_heal_episode_safe(
        self,
        *,
        organization_id: str,
        workflow_permanent_id: str,
        workflow_id: str,
        workflow_run_id: str,
        workflow_run_block_id: str,
        block_label: str,
        engine: str,
        status: Literal["fired_completed", "fired_failed", "fired_unverified", "skipped"],
        skip_reason: HealSkipReason | None,
        parameter_binding_keys: list[str],
        exception_class: str | None,
        failing_line: int | None,
        matched_step_index: int | None,
        failure_message: str | None,
        wall_clock_ms: int | None,
        action_count: int | None,
        output_obligation: OutputObligation,
    ) -> None:
        LOG.info(
            "self-heal episode",
            organization_id=organization_id,
            workflow_permanent_id=workflow_permanent_id,
            workflow_id=workflow_id,
            workflow_run_id=workflow_run_id,
            workflow_run_block_id=workflow_run_block_id,
            block_label=block_label,
            engine=engine,
            status=status,
            skip_reason=skip_reason.value if skip_reason is not None else None,
            origin=TurnOrigin.runtime_self_heal.value,
        )
        try:
            await app.DATABASE.self_heal.create_heal_episode(
                organization_id=organization_id,
                workflow_permanent_id=workflow_permanent_id,
                workflow_id=workflow_id,
                workflow_run_id=workflow_run_id,
                workflow_run_block_id=workflow_run_block_id,
                block_label=block_label,
                engine=engine,
                status=HealStatus(status),
                skip_reason=skip_reason,
                snapshot_available=False,
                parameter_binding_keys=parameter_binding_keys,
                exception_class=exception_class,
                failing_line=failing_line,
                matched_step_index=matched_step_index,
                failure_message=failure_message,
                wall_clock_ms=wall_clock_ms,
                action_count=action_count,
                output_obligation=output_obligation,
            )
        except Exception:
            LOG.warning(
                "self-heal episode persistence failed; continuing",
                organization_id=organization_id,
                workflow_permanent_id=workflow_permanent_id,
                workflow_run_id=workflow_run_id,
                workflow_run_block_id=workflow_run_block_id,
                block_label=block_label,
                engine=engine,
                status=status,
            )

    async def _capture_failure_evidence(
        self,
        *,
        workflow_run_context: WorkflowRunContext,
        workflow_run_id: str,
        workflow_run_block_id: str,
        organization_id: str | None,
        browser_state: BrowserState | None,
        page: Page | None,
    ) -> None:
        """Persist the page the block died on, so the copilot repair loop can see it.

        Entirely best-effort: this runs inside the failure path, so it must never raise and
        never change the block outcome.
        """
        if not organization_id:
            return
        live_browser_state = browser_state or app.BROWSER_MANAGER.get_for_workflow_run(workflow_run_id=workflow_run_id)
        if not live_browser_state:
            return
        try:
            screenshot = await live_browser_state.take_fullpage_screenshot()
        except Exception:
            LOG.warning(
                "Failed to capture the at-failure screenshot for a code block",
                workflow_run_id=workflow_run_id,
                workflow_run_block_id=workflow_run_block_id,
            )
            screenshot = None
        final_url: str | None = None
        try:
            failure_page = page or await live_browser_state.get_or_create_page()
            # A failing login/MFA step can leave a credential or a generated OTP in the query
            # string, and this URL is persisted and logged; mask before it leaves the page.
            final_url = workflow_run_context.mask_secrets_in_data(failure_page.url) if failure_page else None
        except Exception:
            LOG.warning(
                "Failed to read the at-failure URL for a code block",
                workflow_run_id=workflow_run_id,
                workflow_run_block_id=workflow_run_block_id,
            )

        try:
            workflow_run_block = await app.DATABASE.observer.update_workflow_run_block(
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
                final_url=final_url,
            )
            if screenshot:
                await app.ARTIFACT_MANAGER.create_workflow_run_block_artifact(
                    workflow_run_block=workflow_run_block,
                    artifact_type=ArtifactType.SCREENSHOT_LLM,
                    data=screenshot,
                )
        except Exception:
            LOG.warning(
                "Failed to persist the at-failure evidence for a code block",
                workflow_run_id=workflow_run_id,
                workflow_run_block_id=workflow_run_block_id,
            )

    async def _failure_output_with_downloads(
        self,
        *,
        engine: str,
        workflow_run_context: WorkflowRunContext,
        workflow_run_id: str,
        workflow_run_block_id: str,
        organization_id: str | None,
        resolved_download_id: str | None,
        download_dir_before: set[tuple[str, int, int]] | None,
        session_bound: bool,
        download_binding_kind: str | None = None,
        result: dict[str, Any] | list | str | None = None,
    ) -> dict[str, Any] | None:
        """Registration evidence for a block that failed after a download already landed.

        Returns None when nothing new reached the run directory, so a failure that downloaded
        nothing keeps whatever output the caller supplied. The block is already failing for its own
        reason, so the binding verdict is discarded here — only the evidence is kept, bound onto the
        caller's failure payload when one exists."""
        # A session-bound download lands in the provider's directory, never the run's, so the local
        # diff here is an unknown snapshot rather than an empty one and cannot end the lane.
        downloaded_files: list[FileInfo] | None = None
        skipped_file_names: set[str] = set()
        needs_registration = session_bound
        if not session_bound:
            download_dir_after = local_download_dir_file_identities(resolved_download_id)
            if download_dir_after is None or download_dir_before is None:
                return None
            new_files = download_dir_after - download_dir_before
            if not new_files:
                return None
            # Read back before saving so a sidecar run that already uploaded is not uploaded twice,
            # but skip the save only when the read-back already accounts for what this block added —
            # an earlier block's registration is not evidence for this one's file, and a
            # registration made before an overwrite is stale for the rewritten bytes.
            downloaded_files = await self._read_back_downloaded_files(
                organization_id=organization_id or workflow_run_context.organization_id,
                workflow_run_id=workflow_run_id,
                workflow_run_block_id=workflow_run_block_id,
                download_run_id=resolved_download_id,
            )
            registered_names = {file_info.filename for file_info in downloaded_files or []}
            new_names = {name for name, _, _ in new_files}
            before_names = {name for name, _, _ in download_dir_before}
            needs_registration = bool(new_names & before_names) or not new_names.issubset(registered_names)
        if needs_registration:
            downloaded_files, skipped_file_names = await self._register_downloaded_files(
                engine=engine,
                download_binding_kind=download_binding_kind,
                organization_id=organization_id or workflow_run_context.organization_id,
                workflow_run_id=workflow_run_id,
                workflow_run_block_id=workflow_run_block_id,
                session_bound=session_bound,
                download_run_id=resolved_download_id,
            )
        output, _ = await self._bind_and_grade_downloads(
            engine=engine,
            result=result,
            downloaded_files=downloaded_files,
            skipped_file_names=skipped_file_names,
            download_dir_before=download_dir_before,
            resolved_download_id=resolved_download_id,
            workflow_run_context=workflow_run_context,
            workflow_run_id=workflow_run_id,
            workflow_run_block_id=workflow_run_block_id,
            organization_id=organization_id,
        )
        if output is not None:
            await self.record_output_parameter_value(workflow_run_context, workflow_run_id, output)
        return output if isinstance(output, dict) else None

    async def _capture_inline_failure_page_state(
        self,
        *,
        page: Page,
        failure_locator: Any | None,
        workflow_run_context: WorkflowRunContext,
        redaction_parameters: dict[str, Any],
    ) -> dict[str, str]:
        """Read bounded, masked facts from the exact locator that raised inline."""
        from skyvern.forge.sdk.workflow.models.code_block_recorder import COVERING_ELEMENT_SCRIPT

        def mask_fact(value: Any) -> str | None:
            if not isinstance(value, str):
                return None
            masked = workflow_run_context.mask_secrets_in_data(value)
            redacted = app.AGENT_FUNCTION.redact_codeblock_parameter_values(masked, redaction_parameters)
            if not isinstance(redacted, str) or not redacted:
                return None
            return "".join(char for char in redacted if unicodedata.category(char)[0] != "C").strip()[:1000] or None

        state: dict[str, str] = {}
        try:
            async with asyncio.timeout(0.5):
                try:
                    final_url = mask_fact(page.url)
                    if final_url:
                        state["final_url"] = final_url
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 - failure evidence is best effort.
                    pass
                try:
                    page_title = mask_fact(await page.title())
                    if page_title:
                        state["page_title"] = page_title
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 - failure evidence is best effort.
                    pass
                try:
                    if failure_locator is not None:
                        coverer = mask_fact(await failure_locator.evaluate(COVERING_ELEMENT_SCRIPT))
                        if coverer:
                            state["covering_element"] = coverer
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 - failure evidence is best effort.
                    pass
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            pass
        return state

    async def _persist_captured_failure_final_url(
        self,
        *,
        final_url: str | None,
        workflow_run_block_id: str,
        organization_id: str | None,
    ) -> None:
        """Restore the terminal URL after screenshot and healing evidence reads the live page."""
        if not final_url:
            return
        try:
            await app.DATABASE.observer.update_workflow_run_block(
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
                final_url=final_url,
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - failure evidence is best effort.
            LOG.warning(
                "Failed to persist the captured code-block failure URL",
                workflow_run_block_id=workflow_run_block_id,
            )

    async def _failed_result_with_evidence(
        self,
        *,
        failure_reason: str,
        status: BlockStatus,
        workflow_run_context: WorkflowRunContext,
        workflow_run_id: str,
        workflow_run_block_id: str,
        organization_id: str | None,
        browser_state: BrowserState | None,
        page: Page | None,
        engine: str,
        resolved_download_id: str | None,
        download_dir_before: set[tuple[str, int, int]] | None,
        output_parameter_value: dict[str, Any] | list | str | None = None,
        error_codes: list[str] | None = None,
    ) -> BlockResult:
        """Fail a code block after recording the page it died on.

        Every non-healable exit routes through here so a new one inherits the capture instead of
        silently reopening the blindness this closes.
        """
        await self._capture_failure_evidence(
            workflow_run_context=workflow_run_context,
            workflow_run_id=workflow_run_id,
            workflow_run_block_id=workflow_run_block_id,
            organization_id=organization_id,
            browser_state=browser_state,
            page=page,
        )
        return await self.build_block_result(
            success=False,
            failure_reason=failure_reason,
            output_parameter_value=(
                await self._failure_output_with_downloads(
                    engine=engine,
                    workflow_run_context=workflow_run_context,
                    workflow_run_id=workflow_run_id,
                    workflow_run_block_id=workflow_run_block_id,
                    organization_id=organization_id,
                    resolved_download_id=resolved_download_id,
                    download_dir_before=download_dir_before,
                    session_bound=session_download_lane_active(browser_state),
                    download_binding_kind=download_binding_of(browser_state).value,
                    result=output_parameter_value,
                )
                or output_parameter_value
            ),
            status=status,
            workflow_run_block_id=workflow_run_block_id,
            organization_id=organization_id,
            error_codes=error_codes,
        )

    async def _resolve_failure_with_heal(
        self,
        *,
        exception: Exception | None,
        failing_line: int | None,
        build_failure_result: Callable[[], Awaitable[BlockResult]],
        classification: HealClassification,
        recorder: CodeBlockActionRecording,
        workflow_run_context: WorkflowRunContext,
        workflow_run_id: str,
        workflow_run_block_id: str,
        organization_id: str | None,
        browser_session_id: str | None,
        browser_state: BrowserState | None = None,
        page: Page | None = None,
        resolved_download_id: str | None = None,
        download_dir_before: set[tuple[str, int, int]] | None = None,
        redaction_parameters: dict[str, Any] | None = None,
    ) -> BlockResult:
        resolved_redaction_parameters = redaction_parameters or {}

        def scrub_failure_value(value: str | None, fallback: str = CODE_BLOCK_GENERIC_FAILURE_REASON) -> str | None:
            return _redact_codeblock_failure_text(value, resolved_redaction_parameters, fallback)

        # Capture before the healable branch: a non-healable failure is exactly the case the
        # repair loop has the least to go on.
        await self._capture_failure_evidence(
            workflow_run_context=workflow_run_context,
            workflow_run_id=workflow_run_id,
            workflow_run_block_id=workflow_run_block_id,
            organization_id=organization_id,
            browser_state=browser_state,
            page=page,
        )

        if (
            organization_id
            and not classification.healable
            and classification.skip_reason is HealSkipReason.user_defined_error
        ):
            await self._write_heal_episode_safe(
                organization_id=organization_id,
                workflow_permanent_id=workflow_run_context.workflow_permanent_id,
                workflow_id=workflow_run_context.workflow_id,
                workflow_run_id=workflow_run_id,
                workflow_run_block_id=workflow_run_block_id,
                block_label=self.label,
                engine="harness",
                status="skipped",
                skip_reason=HealSkipReason.user_defined_error,
                parameter_binding_keys=self._heal_parameter_binding_keys(workflow_run_context),
                exception_class=scrub_failure_value("Exception", fallback=""),
                failing_line=failing_line,
                matched_step_index=self._matched_step_index_for_failing_line(failing_line),
                failure_message=None,
                wall_clock_ms=None,
                action_count=None,
                output_obligation=OutputObligation.none,
            )

        async def _finalize_heal_result(result: BlockResult | None) -> BlockResult:
            # A healed block can still come out failed here: a download that landed with nothing
            # registered fails the binding check exactly as it would on the normal success exit.
            if result is not None:
                result = _redact_codeblock_result(result, resolved_redaction_parameters)
                if result.success:
                    # A healed block is still a block whose download must be accountable: the raise
                    # that triggered the heal fired after the file landed, and this path was the one
                    # exit that recorded the heal's own output without ever looking at the run
                    # directory (SKY-13694's completed-with-null arm).
                    try:
                        downloaded_files, skipped_file_names = await self._register_downloaded_files(
                            engine="inline",
                            download_binding_kind=download_binding_of(browser_state).value,
                            organization_id=organization_id or workflow_run_context.organization_id,
                            workflow_run_id=workflow_run_id,
                            workflow_run_block_id=workflow_run_block_id,
                            session_bound=session_download_lane_active(browser_state),
                            download_run_id=resolved_download_id,
                        )
                        bound_output, binding_failure_reason = await self._bind_and_grade_downloads(
                            engine="inline",
                            result=result.output_parameter_value,
                            downloaded_files=downloaded_files,
                            skipped_file_names=skipped_file_names,
                            download_dir_before=download_dir_before,
                            resolved_download_id=resolved_download_id,
                            workflow_run_context=workflow_run_context,
                            workflow_run_id=workflow_run_id,
                            workflow_run_block_id=workflow_run_block_id,
                            organization_id=organization_id,
                        )
                    except asyncio.CancelledError:
                        # A cancel inside the storage round-trips must not book the healed block
                        # as failed or skip its billing hook; the verdict is moot on teardown.
                        await recorder.finalize(success=True)
                        raise
                    if binding_failure_reason is not None:
                        binding_failure_reason = scrub_failure_value(binding_failure_reason)
                        bound_output = app.AGENT_FUNCTION.redact_codeblock_parameter_values(
                            bound_output, resolved_redaction_parameters
                        )
                        await self.record_output_parameter_value(workflow_run_context, workflow_run_id, bound_output)
                        await recorder.finalize(success=False)
                        return await self.build_block_result(
                            success=False,
                            failure_reason=binding_failure_reason,
                            output_parameter_value=bound_output,
                            status=BlockStatus.failed,
                            workflow_run_block_id=workflow_run_block_id,
                            organization_id=organization_id,
                        )
                    if bound_output is not result.output_parameter_value:
                        result = await self.build_block_result(
                            success=result.success,
                            failure_reason=result.failure_reason,
                            output_parameter_value=bound_output,
                            status=result.status,
                            workflow_run_block_id=workflow_run_block_id,
                            organization_id=organization_id,
                            error_codes=result.error_codes or None,
                        )
                else:
                    # A heal that fired and still failed keeps its verdict, but a download that
                    # landed before the failure is bound like every other failing exit.
                    failure_output = await self._failure_output_with_downloads(
                        engine="inline",
                        workflow_run_context=workflow_run_context,
                        workflow_run_id=workflow_run_id,
                        workflow_run_block_id=workflow_run_block_id,
                        organization_id=organization_id,
                        resolved_download_id=resolved_download_id,
                        download_dir_before=download_dir_before,
                        session_bound=session_download_lane_active(browser_state),
                        download_binding_kind=download_binding_of(browser_state).value,
                        result=result.output_parameter_value,
                    )
                    if failure_output is not None:
                        result = await self.build_block_result(
                            success=False,
                            failure_reason=result.failure_reason,
                            output_parameter_value=failure_output,
                            status=result.status or BlockStatus.failed,
                            workflow_run_block_id=workflow_run_block_id,
                            organization_id=organization_id,
                            error_codes=result.error_codes or None,
                        )
                result = _redact_codeblock_result(result, resolved_redaction_parameters)
                output_obligation = self._output_obligation_for_heal_result(result)
                # Record output before finalizing so a failed write fails closed, never leaving a
                # completed block without the output downstream consumers require.
                if output_obligation in {OutputObligation.observed, OutputObligation.vestigial}:
                    await self.record_output_parameter_value(
                        workflow_run_context,
                        workflow_run_id,
                        result.output_parameter_value,
                    )
                await recorder.finalize(success=result.success)
                return result
            await recorder.finalize(success=False)
            return _redact_codeblock_result(await build_failure_result(), resolved_redaction_parameters)

        if not classification.healable:
            return await _finalize_heal_result(None)
        if not await self._self_heal_enabled(workflow_run_context):
            return await _finalize_heal_result(None)
        if not organization_id:
            return await _finalize_heal_result(None)

        workflow_permanent_id = workflow_run_context.workflow_permanent_id
        workflow_id = workflow_run_context.workflow_id
        exception_for_heal = exception or RuntimeError("CodeBlock failed")
        exception_class = scrub_failure_value("Exception", fallback="")
        matched_step_index = self._matched_step_index_for_failing_line(failing_line)
        parameter_binding_keys = self._heal_parameter_binding_keys(workflow_run_context)
        live_browser_state = browser_state or app.BROWSER_MANAGER.get_for_workflow_run(workflow_run_id=workflow_run_id)

        async def _record_harness_skip(skip_reason: HealSkipReason, failure_message: str | None = None) -> None:
            await self._write_heal_episode_safe(
                organization_id=organization_id,
                workflow_permanent_id=workflow_permanent_id,
                workflow_id=workflow_id,
                workflow_run_id=workflow_run_id,
                workflow_run_block_id=workflow_run_block_id,
                block_label=self.label,
                engine="harness",
                status="skipped",
                skip_reason=skip_reason,
                parameter_binding_keys=parameter_binding_keys,
                exception_class=exception_class,
                failing_line=failing_line,
                matched_step_index=matched_step_index,
                failure_message=scrub_failure_value(failure_message),
                wall_clock_ms=None,
                action_count=None,
                output_obligation=OutputObligation.none,
            )

        async def _run_floor_recovery() -> BlockResult | None:
            floor_result = await self._attempt_self_heal(
                exception=exception_for_heal,
                failing_line=failing_line,
                recording_page=recorder.recording_page,
                classification=classification,
                workflow_run_context=workflow_run_context,
                workflow_run_id=workflow_run_id,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
                browser_session_id=browser_session_id,
                browser_state=live_browser_state,
                page=page,
                record_output_parameter=False,
                redaction_parameters=resolved_redaction_parameters,
            )
            floor_output_obligation = self._output_obligation_for_heal_result(floor_result)
            await self._write_heal_episode_safe(
                organization_id=organization_id,
                workflow_permanent_id=workflow_permanent_id,
                workflow_id=workflow_id,
                workflow_run_id=workflow_run_id,
                workflow_run_block_id=workflow_run_block_id,
                block_label=self.label,
                engine="floor",
                status="fired_completed" if floor_result is not None and floor_result.success else "fired_failed",
                skip_reason=None,
                parameter_binding_keys=parameter_binding_keys,
                exception_class=exception_class,
                failing_line=failing_line,
                matched_step_index=matched_step_index,
                failure_message=scrub_failure_value(
                    floor_result.failure_reason if floor_result is not None else "floor_no_result"
                ),
                wall_clock_ms=None,
                action_count=None,
                output_obligation=floor_output_obligation,
            )
            return floor_result

        api_key = await app.AGENT_FUNCTION.resolve_self_heal_api_key(organization_id)
        if not api_key:
            await _record_harness_skip(
                HealSkipReason.credential_unavailable, failure_message="self_heal_api_key_missing"
            )
            return await _finalize_heal_result(await _run_floor_recovery())

        browser_context = (
            getattr(live_browser_state, "browser_context", None) if live_browser_state is not None else None
        )
        if live_browser_state is None or not _browser_context_is_attachable(browser_context):
            await _record_harness_skip(HealSkipReason.adoption_failed, failure_message="self_heal_browser_unavailable")
            return await _finalize_heal_result(await _run_floor_recovery())
        # Reserve cap only once harness preconditions are satisfied so transient infra skips
        # do not consume slots; cap limits harness attempts, not floor fallback attempts.
        cap_reserved = await check_and_increment_self_heal_cap(
            workflow_permanent_id=workflow_permanent_id,
            organization_id=organization_id,
        )
        if cap_reserved is None:
            await _record_harness_skip(HealSkipReason.capped, failure_message="self_heal_daily_cap_exceeded")
            return await _finalize_heal_result(await _run_floor_recovery())

        recovery: SelfHealRecoveryResult | None = None
        try:
            before_snapshot = await self._self_heal_mutation_guard_snapshot(
                workflow_permanent_id=workflow_permanent_id,
                organization_id=organization_id,
            )
            recovery = await run_self_heal_recovery(
                block=self,
                workflow_run_context=workflow_run_context,
                workflow_run_id=workflow_run_id,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
                browser_state=live_browser_state,
                failing_line=failing_line,
                api_key=api_key,
                max_actions=settings.SELF_HEAL_MAX_ACTIONS,
                wall_clock_budget_seconds=settings.SELF_HEAL_WALL_CLOCK_BUDGET_SECONDS,
                redaction_parameters=resolved_redaction_parameters,
            )
            after_snapshot = await self._self_heal_mutation_guard_snapshot(
                workflow_permanent_id=workflow_permanent_id,
                organization_id=organization_id,
            )
            if before_snapshot != after_snapshot:
                raise RuntimeError("workflow_mutated_during_runtime_self_heal")

            if recovery.success:
                # Compute the obligation from a non-persisted result: an unverified or
                # fail-closed outcome must never leave the block row marked completed, so the
                # block row is written only on the verified path below.
                harness_result = await self.build_block_result(
                    success=True,
                    failure_reason=None,
                    output_parameter_value=None,
                    status=BlockStatus.completed,
                    workflow_run_block_id=None,
                    organization_id=organization_id,
                )
                harness_output_obligation = self._output_obligation_for_heal_result(harness_result)
                if not recovery.verified:
                    await self._write_heal_episode_safe(
                        organization_id=organization_id,
                        workflow_permanent_id=workflow_permanent_id,
                        workflow_id=workflow_id,
                        workflow_run_id=workflow_run_id,
                        workflow_run_block_id=workflow_run_block_id,
                        block_label=self.label,
                        engine="harness",
                        status="fired_unverified",
                        skip_reason=None,
                        parameter_binding_keys=parameter_binding_keys,
                        exception_class=exception_class,
                        failing_line=failing_line,
                        matched_step_index=matched_step_index,
                        failure_message=scrub_failure_value(recovery.failure_note),
                        wall_clock_ms=recovery.wall_clock_ms,
                        action_count=recovery.action_count,
                        output_obligation=harness_output_obligation,
                    )
                    # Fail closed when an unverified harness run already mutated controls:
                    # rerunning floor could duplicate side effects (submit/send/delete).
                    if recovery.performed_mutation:
                        LOG.info(
                            "Runtime self-heal unverified after mutating actions; suppressing floor fallback",
                            workflow_run_id=workflow_run_id,
                            workflow_run_block_id=workflow_run_block_id,
                            workflow_permanent_id=workflow_permanent_id,
                            organization_id=organization_id,
                            block_label=self.label,
                            origin=TurnOrigin.runtime_self_heal.value,
                        )
                        return await _finalize_heal_result(None)
                    return await _finalize_heal_result(await _run_floor_recovery())
                # Verified: only now write status=completed to the block row.
                harness_result = await self.build_block_result(
                    success=True,
                    failure_reason=None,
                    output_parameter_value=None,
                    status=BlockStatus.completed,
                    workflow_run_block_id=workflow_run_block_id,
                    organization_id=organization_id,
                )
                await self._write_heal_episode_safe(
                    organization_id=organization_id,
                    workflow_permanent_id=workflow_permanent_id,
                    workflow_id=workflow_id,
                    workflow_run_id=workflow_run_id,
                    workflow_run_block_id=workflow_run_block_id,
                    block_label=self.label,
                    engine="harness",
                    status="fired_completed",
                    skip_reason=None,
                    parameter_binding_keys=parameter_binding_keys,
                    exception_class=exception_class,
                    failing_line=failing_line,
                    matched_step_index=matched_step_index,
                    failure_message=None,
                    wall_clock_ms=recovery.wall_clock_ms,
                    action_count=recovery.action_count,
                    output_obligation=harness_output_obligation,
                )
                return await _finalize_heal_result(harness_result)

            await self._write_heal_episode_safe(
                organization_id=organization_id,
                workflow_permanent_id=workflow_permanent_id,
                workflow_id=workflow_id,
                workflow_run_id=workflow_run_id,
                workflow_run_block_id=workflow_run_block_id,
                block_label=self.label,
                engine="harness",
                status="fired_failed",
                skip_reason=None,
                parameter_binding_keys=parameter_binding_keys,
                exception_class=exception_class,
                failing_line=failing_line,
                matched_step_index=matched_step_index,
                failure_message=scrub_failure_value(recovery.failure_note),
                wall_clock_ms=recovery.wall_clock_ms,
                action_count=recovery.action_count,
                output_obligation=OutputObligation.none,
            )
            # A failed harness turn that already mutated controls must not hand off to floor:
            # rerunning could duplicate the side effect (submit/send/delete).
            if recovery.performed_mutation:
                LOG.info(
                    "Runtime self-heal failed after mutating actions; suppressing floor fallback",
                    workflow_run_id=workflow_run_id,
                    workflow_run_block_id=workflow_run_block_id,
                    workflow_permanent_id=workflow_permanent_id,
                    organization_id=organization_id,
                    block_label=self.label,
                    origin=TurnOrigin.runtime_self_heal.value,
                )
                return await _finalize_heal_result(None)
            return await _finalize_heal_result(await _run_floor_recovery())
        except HealAdoptionFailed:
            await self._write_heal_episode_safe(
                organization_id=organization_id,
                workflow_permanent_id=workflow_permanent_id,
                workflow_id=workflow_id,
                workflow_run_id=workflow_run_id,
                workflow_run_block_id=workflow_run_block_id,
                block_label=self.label,
                engine="harness",
                status="fired_failed",
                skip_reason=None,
                parameter_binding_keys=parameter_binding_keys,
                exception_class=exception_class,
                failing_line=failing_line,
                matched_step_index=matched_step_index,
                failure_message=scrub_failure_value("CodeBlock operation failed."),
                wall_clock_ms=None,
                action_count=None,
                output_obligation=OutputObligation.none,
            )
            return await _finalize_heal_result(await _run_floor_recovery())
        except Exception:
            LOG.error(
                "Runtime self-heal harness recovery failed",
                workflow_run_id=workflow_run_id,
                workflow_run_block_id=workflow_run_block_id,
                workflow_permanent_id=workflow_permanent_id,
                organization_id=organization_id,
                block_label=self.label,
                origin=TurnOrigin.runtime_self_heal.value,
            )
            await self._write_heal_episode_safe(
                organization_id=organization_id,
                workflow_permanent_id=workflow_permanent_id,
                workflow_id=workflow_id,
                workflow_run_id=workflow_run_id,
                workflow_run_block_id=workflow_run_block_id,
                block_label=self.label,
                engine="harness",
                status="fired_failed",
                skip_reason=None,
                parameter_binding_keys=parameter_binding_keys,
                exception_class=exception_class,
                failing_line=failing_line,
                matched_step_index=matched_step_index,
                failure_message=scrub_failure_value("recovery_failed", fallback=""),
                wall_clock_ms=None,
                action_count=None,
                output_obligation=OutputObligation.none,
            )
            # If the harness already mutated a control before the error (e.g. the workflow-mutation
            # guard tripped after a click), a floor rerun could duplicate the side effect.
            if recovery is not None and recovery.performed_mutation:
                LOG.info(
                    "Runtime self-heal errored after mutating actions; suppressing floor fallback",
                    workflow_run_id=workflow_run_id,
                    workflow_run_block_id=workflow_run_block_id,
                    workflow_permanent_id=workflow_permanent_id,
                    organization_id=organization_id,
                    block_label=self.label,
                    origin=TurnOrigin.runtime_self_heal.value,
                )
                return await _finalize_heal_result(None)
            return await _finalize_heal_result(await _run_floor_recovery())

    async def execute(
        self,
        workflow_run_id: str,
        workflow_run_block_id: str,
        organization_id: str | None = None,
        browser_session_id: str | None = None,
        **kwargs: dict,
    ) -> BlockResult:
        propagated_error: BaseException
        try:
            return await self._execute(
                workflow_run_id,
                workflow_run_block_id,
                organization_id=organization_id,
                browser_session_id=browser_session_id,
                **kwargs,
            )
        except BaseException as exc:
            if isinstance(exc, (MissingBrowserState, MissingBrowserStatePage)):
                propagated_error = exc.with_traceback(None)
            elif app.AGENT_FUNCTION.prepare_codeblock_control_flow_exception(exc):
                propagated_error = exc.with_traceback(None)
            else:
                propagated_error = RuntimeError()
            del self, workflow_run_id, workflow_run_block_id, organization_id, browser_session_id, kwargs, exc
        raise propagated_error from None

    async def _execute(
        self,
        workflow_run_id: str,
        workflow_run_block_id: str,
        organization_id: str | None = None,
        browser_session_id: str | None = None,
        **kwargs: dict,
    ) -> BlockResult:
        await app.AGENT_FUNCTION.validate_code_block(organization_id=organization_id)

        block_context = skyvern_context.current()
        workflow_run_context = self.get_workflow_run_context(workflow_run_id)
        if block_context:
            await capture_block_download_baseline(
                block_context,
                organization_id or workflow_run_context.organization_id or "",
                workflow_run_id,
                self.label,
            )

        resolved_download_id = resolve_run_download_id(block_context, fallback_run_id=workflow_run_id)
        download_dir_before = local_download_dir_file_identities(resolved_download_id)
        browser_state = await self.get_or_create_browser_state(
            workflow_run_id=workflow_run_id,
            organization_id=organization_id,
            browser_session_id=browser_session_id,
            download_run_id_override=resolved_download_id,
        )
        if not browser_state:
            return await self.build_block_result(
                success=False,
                failure_reason="No browser found to run the code block",
                output_parameter_value=None,
                status=BlockStatus.failed,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )

        page = await browser_state.get_working_page()
        if page is None:
            # A session can arrive holding a context with no tab, leaving nothing to adopt.
            # Opening one can still raise when the CDP path does not deliver this client the
            # target announcement for the tab it creates. The cloud CDP proxy backfills late
            # autoAttach clients, diverts createTarget, and fans out unsolicited targets, so
            # a failure means the announcement was still missed; see the cdp-proxy runbook's
            # multi-client autoAttach section.
            try:
                page = await browser_state.get_or_create_page()
            except Exception:
                LOG.warning(
                    "Failed to open a page to run the code block",
                    workflow_run_id=workflow_run_id,
                    workflow_run_block_id=workflow_run_block_id,
                    organization_id=organization_id,
                    browser_session_id=browser_session_id,
                    block_label=self.label,
                )
                return await self.build_block_result(
                    success=False,
                    failure_reason=CODE_BLOCK_TAB_OPEN_FAILURE_REASON,
                    output_parameter_value=None,
                    status=BlockStatus.failed,
                    workflow_run_block_id=workflow_run_block_id,
                    organization_id=organization_id,
                )

        await self._ensure_run_recording_artifact(
            browser_state=browser_state,
            workflow_run_id=workflow_run_id,
            workflow_run_block_id=workflow_run_block_id,
            organization_id=organization_id,
            browser_session_id=browser_session_id,
        )

        try:
            self.format_potential_template_parameters(workflow_run_context)
        except Exception as e:
            return await self._template_format_failure_result(
                e,
                "Failed to format CodeBlock parameters.",
                workflow_run_context,
                workflow_run_id,
                workflow_run_block_id,
                organization_id,
            )

        # get all parameters into a dictionary
        parameter_values = {}
        credential_parameter_keys: set[str] = set()
        credential_release_guard = CredentialReleaseGuard(workflow_run_id=workflow_run_id, block_label=self.label)
        for parameter in self.parameters:
            value = workflow_run_context.get_value(parameter.key)
            if not parameter.parameter_type.is_secret_or_credential() and not (
                # NOTE: skyvern credential is a 'credential_id' workflow parameter type
                parameter.parameter_type == ParameterType.WORKFLOW
                and parameter.workflow_parameter_type is not None
                and parameter.workflow_parameter_type.is_credential_type()
            ):
                if (
                    isinstance(parameter, WorkflowParameter)
                    and parameter.workflow_parameter_type == WorkflowParameterType.FILE_URL
                ):
                    value = await self._materialize_file_parameter_path(
                        value,
                        workflow_run_id=workflow_run_id,
                        organization_id=organization_id,
                    )
                parameter_values[parameter.key] = value
                continue
            credential_parameter_keys.add(parameter.key)
            if isinstance(value, dict):
                real_secret_values = {}
                for credential_field, credential_place_holder in value.items():
                    # "context" is a skyvern-defined field to reduce LLM hallucination
                    if credential_field == "context":
                        continue
                    secret_value = workflow_run_context.get_original_secret_value_or_none(credential_place_holder)
                    if (
                        secret_value == BitwardenConstants.TOTP
                        or secret_value == OnePasswordConstants.TOTP
                        or secret_value == AzureVaultConstants.TOTP
                    ):
                        totp_secret_key = workflow_run_context.totp_secret_value_key(credential_place_holder)
                        totp_secret = workflow_run_context.get_original_secret_value_or_none(totp_secret_key)
                        if totp_secret:
                            secret_value = generate_totp_code(totp_secret)
                            # The pre-minted .totp string is exposed to user code (legacy path),
                            # so register it for masking like any other resolved secret.
                            _register_code_block_secret(workflow_run_context, secret_value)
                        else:
                            LOG.warning(
                                "No TOTP secret found, returning the parameter value as is",
                                parameter_key=parameter.key,
                            )

                    real_secret_value = secret_value if secret_value is not None else credential_place_holder
                    parameter_values[credential_field] = real_secret_value
                    real_secret_values[credential_field] = real_secret_value
                credential_namespace = Credential(**real_secret_values)
                credential_namespace.otp = _bind_code_block_otp(parameter.key, organization_id, workflow_run_id)
                credential_namespace.magic_link = _bind_code_block_magic_link(
                    parameter.key, organization_id, workflow_run_id
                )
                parameter_values[parameter.key] = credential_namespace
                tested_url = workflow_run_context.credential_tested_urls.get(parameter.key)
                armed_any = False
                if tested_url:
                    for credential_field, secret in real_secret_values.items():
                        # card_brand and friends are stored as plain values on purpose; arming them
                        # would refuse an ordinary off-site field that happens to read "visa".
                        if credential_field in NON_SECRET_CREDENTIAL_FIELDS:
                            continue
                        armed_any |= credential_release_guard.arm(secret, tested_url, parameter.key)
                if not armed_any:
                    # Coverage is only measurable if the unarmed case says so: a credential with no
                    # tested_url has no release scope, so its secrets are unguarded on this path.
                    LOG.info(
                        "codeblock_credential_release_unarmed",
                        parameter_key=parameter.key,
                        reason="credential has no usable tested_url",
                        workflow_run_id=workflow_run_id,
                        block_label=self.label,
                    )
            else:
                secret_value = workflow_run_context.get_original_secret_value_or_none(value)
                parameter_values[parameter.key] = secret_value if secret_value is not None else value

        serialized_parameter_values = app.AGENT_FUNCTION.serialize_codeblock_parameters(parameter_values)

        def scrub_failure_reason(value: str | None, fallback: str = CODE_BLOCK_GENERIC_FAILURE_REASON) -> str | None:
            return _redact_codeblock_failure_text(value, serialized_parameter_values, fallback)

        def scrub_failed_block_result(result: BlockResult) -> BlockResult:
            return _redact_codeblock_result(result, serialized_parameter_values)

        try:
            use_codeblock_runner = await app.AGENT_FUNCTION.should_use_codeblock_runner(
                workflow_run_id=workflow_run_id,
                workflow_run_block_id=workflow_run_block_id,
                workflow_run_context=workflow_run_context,
                organization_id=organization_id,
                block_label=self.label,
                browser_session_id=browser_session_id,
                code=self.code,
            )
        except CodeBlockRunnerSelectionError as selection_error:
            return await self.build_block_result(
                success=False,
                failure_reason=scrub_failure_reason(str(selection_error)),
                output_parameter_value=None,
                status=BlockStatus.failed,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )
        LOG.info(
            "CodeBlock runner selection at block",
            use_codeblock_runner=use_codeblock_runner,
            workflow_run_id=workflow_run_id,
            workflow_run_block_id=workflow_run_block_id,
            block_label=self.label,
        )

        # Every code block gets a container task v1 + step so its recorded calls render through
        # the standard action/artifact timeline and are billable; on prompt-bearing blocks the
        # task also seats a later agent takeover on failure.
        strategy_aware_typing = await self._workflow_is_copilot_authored(workflow_run_context)
        playwright_input_defaults = playwright_input_defaults_for_page(page) if strategy_aware_typing else None
        recorder = CodeBlockActionRecording(
            code_block=self,
            page=page,
            workflow_run_id=workflow_run_id,
            workflow_run_block_id=workflow_run_block_id,
            organization_id=organization_id,
            workflow_run_context=workflow_run_context,
            redaction_parameters=serialized_parameter_values,
            credential_release_guard=credential_release_guard if credential_release_guard.is_armed else None,
            strategy_aware_typing=strategy_aware_typing,
            playwright_input_defaults=playwright_input_defaults,
        )
        if credential_release_guard.is_armed:
            credential_release_guard.log_armed()
        await recorder.create_task_and_step()
        recording_page = recorder.recording_page

        try:
            await recorder.link_block()
            download_evidence = self._bind_download_evidence_probe(
                engine="secure_runner" if use_codeblock_runner else "inline",
                organization_id=organization_id,
                workflow_run_id=workflow_run_id,
                workflow_run_block_id=workflow_run_block_id,
                session_bound=session_download_lane_active(browser_state),
                resolved_download_id=resolved_download_id,
            )
            if use_codeblock_runner:
                secure_code_block_result = await app.AGENT_FUNCTION.execute_code_block_override(
                    block=self,
                    workflow_run_id=workflow_run_id,
                    workflow_run_block_id=workflow_run_block_id,
                    organization_id=organization_id,
                    browser_session_id=browser_session_id,
                    workflow_run_context=workflow_run_context,
                    parameter_values=parameter_values,
                    credential_parameter_keys=credential_parameter_keys,
                    recording_page=recording_page,
                    download_run_id=resolved_download_id,
                    download_binding=download_binding_of(browser_state),
                    download_evidence=download_evidence,
                )
                LOG.info(
                    "Secure CodeBlock override returned",
                    override_returned_none=secure_code_block_result is None,
                    workflow_run_id=workflow_run_id,
                    workflow_run_block_id=workflow_run_block_id,
                    block_label=self.label,
                )
                if secure_code_block_result is not None:
                    recorded = recorder.recorded_actions()
                    secure_failure = secure_code_block_result.failure
                    if (
                        secure_failure is not None
                        and type(secure_failure.failing_line) is int
                        and secure_failure.failing_line > 0
                    ):
                        recorded.append(
                            _code_block_failure_action(
                                failing_line=secure_failure.failing_line,
                                action_order=len(recorded),
                            )
                        )
                    await recorder.persist(recorded)
                    if secure_failure is not None:
                        secure_classification = (
                            HealClassification(
                                healable=False,
                                skip_reason=HealSkipReason.user_defined_error,
                            )
                            if secure_failure.accepted_user_defined_error is not None
                            else self._classify_secure_runner_failure(secure_failure)
                        )

                        def scrub_secure_failure_fact(raw_value: str | None) -> str | None:
                            if not isinstance(raw_value, str):
                                return None
                            masked_value = workflow_run_context.mask_secrets_in_data(raw_value)
                            scrubbed_value = scrub_failure_reason(masked_value, fallback="")
                            return (
                                (
                                    "".join(
                                        char for char in scrubbed_value if unicodedata.category(char)[0] != "C"
                                    ).strip()[:1000]
                                    or None
                                )
                                if scrubbed_value
                                else None
                            )

                        secure_failure_page_state = {
                            field: value
                            for field, raw_value in (
                                ("final_url", secure_failure.final_url),
                                ("page_title", secure_failure.page_title),
                                ("covering_element", secure_failure.covering_element),
                            )
                            if (value := scrub_secure_failure_fact(raw_value)) is not None
                        }

                        async def build_secure_failure_result() -> BlockResult:
                            engine_block_result = secure_code_block_result.block_result
                            if engine_block_result is not None:
                                engine_block_result = scrub_failed_block_result(engine_block_result)
                                if secure_failure_page_state and isinstance(
                                    engine_block_result.output_parameter_value, dict
                                ):
                                    failure_output = dict(engine_block_result.output_parameter_value)
                                    failure_output["failure_page_state"] = secure_failure_page_state
                                    engine_block_result = replace(
                                        engine_block_result,
                                        output_parameter_value=failure_output,
                                    )
                                await self.record_output_parameter_value(
                                    workflow_run_context,
                                    workflow_run_id,
                                    engine_block_result.output_parameter_value,
                                )
                                await self._persist_captured_failure_final_url(
                                    final_url=secure_failure_page_state.get("final_url"),
                                    workflow_run_block_id=workflow_run_block_id,
                                    organization_id=organization_id,
                                )
                                return engine_block_result
                            # The runner already scrubbed this text against the same serialized
                            # parameters and guarded the fail-closed case, so re-scrubbing can only
                            # no-op or empty it; fall back to what the runner reported.
                            runner_reason = secure_failure.failure_reason or "Code block failed in the secure runner"
                            secure_failure_reason = scrub_failure_reason(runner_reason, fallback=runner_reason) or ""
                            secure_error_code = scrub_failure_reason(secure_failure.error_code, fallback="")
                            secure_error_codes = [secure_error_code] if secure_error_code else []
                            failure_output = build_block_failure_output(secure_failure_reason, secure_error_codes)
                            if secure_failure_page_state:
                                failure_output["failure_page_state"] = secure_failure_page_state
                            if secure_failure.denied_exception_class is not None:
                                failure_output["runner_exception_class"] = secure_failure.denied_exception_class
                            await self.record_output_parameter_value(
                                workflow_run_context, workflow_run_id, failure_output
                            )
                            await self._persist_captured_failure_final_url(
                                final_url=secure_failure_page_state.get("final_url"),
                                workflow_run_block_id=workflow_run_block_id,
                                organization_id=organization_id,
                            )
                            return await self.build_block_result(
                                success=False,
                                failure_reason=secure_failure_reason,
                                output_parameter_value=failure_output,
                                status=BlockStatus.failed,
                                workflow_run_block_id=workflow_run_block_id,
                                organization_id=organization_id,
                                error_codes=secure_error_codes or None,
                            )

                        return await self._resolve_failure_with_heal(
                            exception=None,
                            failing_line=secure_failure.failing_line,
                            build_failure_result=build_secure_failure_result,
                            classification=secure_classification,
                            recorder=recorder,
                            workflow_run_context=workflow_run_context,
                            workflow_run_id=workflow_run_id,
                            workflow_run_block_id=workflow_run_block_id,
                            organization_id=organization_id,
                            browser_session_id=browser_session_id,
                            browser_state=browser_state,
                            page=page,
                            resolved_download_id=resolved_download_id,
                            download_dir_before=download_dir_before,
                            redaction_parameters=serialized_parameter_values,
                        )
                    if secure_code_block_result.block_result is None:
                        LOG.warning(
                            "Secure CodeBlock runner returned success without block result",
                            workflow_run_id=workflow_run_id,
                            workflow_run_block_id=workflow_run_block_id,
                            block_label=self.label,
                        )
                        await recorder.finalize(success=False)
                        return await self._failed_result_with_evidence(
                            failure_reason=scrub_failure_reason("Secure code block runner returned no result") or "",
                            status=BlockStatus.failed,
                            workflow_run_context=workflow_run_context,
                            workflow_run_id=workflow_run_id,
                            workflow_run_block_id=workflow_run_block_id,
                            organization_id=organization_id,
                            browser_state=browser_state,
                            page=page,
                            engine="secure_runner",
                            resolved_download_id=resolved_download_id,
                            download_dir_before=download_dir_before,
                        )
                    # failure=None does not imply success: infra arms (no browser/page, runner raise,
                    # invalid output) return a failed block_result with no healable failure metadata.
                    if not secure_code_block_result.block_result.success:
                        # No healable metadata means the repair loop has even less to go on here
                        # than on the healable path, so the page evidence matters more, not less.
                        await self._capture_failure_evidence(
                            workflow_run_context=workflow_run_context,
                            workflow_run_id=workflow_run_id,
                            workflow_run_block_id=workflow_run_block_id,
                            organization_id=organization_id,
                            browser_state=browser_state,
                            page=page,
                        )
                        await recorder.finalize(success=False)
                        return scrub_failed_block_result(secure_code_block_result.block_result)
                    secure_output = secure_code_block_result.block_result.output_parameter_value
                    # The sidecar already saved what it downloaded, so read back rather than
                    # re-uploading; the save runs when the read-back does not account for the files
                    # this block added — an earlier block's registration is not evidence for them,
                    # and a registration made before an overwrite is stale for the rewritten bytes.
                    try:
                        host_files = await self._read_back_downloaded_files(
                            organization_id=organization_id or workflow_run_context.organization_id,
                            workflow_run_id=workflow_run_id,
                            workflow_run_block_id=workflow_run_block_id,
                            download_run_id=resolved_download_id,
                        )
                        secure_skipped_file_names: set[str] = set()
                        secure_dir_after = local_download_dir_file_identities(resolved_download_id)
                        secure_registered_names = {file_info.filename for file_info in host_files or []}
                        # An unreadable snapshot is unknown, never empty: coverage cannot be proven,
                        # so the save runs rather than being skipped on a coerced empty diff. A
                        # session-bound download never reaches the run directory, so it reads the same.
                        if (
                            secure_dir_after is None
                            or download_dir_before is None
                            or session_download_lane_active(browser_state)
                        ):
                            secure_new_names = None
                            secure_before_names = None
                        else:
                            secure_new_names = {name for name, _, _ in secure_dir_after - download_dir_before}
                            secure_before_names = {name for name, _, _ in download_dir_before}
                        if (
                            secure_new_names is None
                            or secure_before_names is None
                            or secure_new_names & secure_before_names
                            or not secure_new_names.issubset(secure_registered_names)
                        ):
                            host_files, secure_skipped_file_names = await self._register_downloaded_files(
                                engine="secure_runner",
                                download_binding_kind=download_binding_of(browser_state).value,
                                organization_id=organization_id or workflow_run_context.organization_id,
                                workflow_run_id=workflow_run_id,
                                workflow_run_block_id=workflow_run_block_id,
                                session_bound=session_download_lane_active(browser_state),
                                download_run_id=resolved_download_id,
                                download_operation_invoked=(
                                    secure_code_block_result.download_operation_receipt is not None
                                ),
                            )
                        secure_output, binding_failure_reason = await self._bind_and_grade_downloads(
                            engine="secure_runner",
                            result=secure_output,
                            downloaded_files=host_files,
                            skipped_file_names=secure_skipped_file_names,
                            download_dir_before=download_dir_before,
                            resolved_download_id=resolved_download_id,
                            workflow_run_context=workflow_run_context,
                            workflow_run_id=workflow_run_id,
                            workflow_run_block_id=workflow_run_block_id,
                            organization_id=organization_id,
                            authored_registration=block_output_has_registered_download(secure_output),
                        )
                    except asyncio.CancelledError:
                        # A cancel inside the storage round-trips must not book the executed block
                        # as failed or skip its billing hook; the verdict is moot on teardown.
                        await recorder.finalize(success=True)
                        raise
                    await recorder.finalize(success=binding_failure_reason is None)
                    await self.record_output_parameter_value(
                        workflow_run_context,
                        workflow_run_id,
                        secure_output,
                    )
                    if binding_failure_reason is not None:
                        return await self.build_block_result(
                            success=False,
                            failure_reason=scrub_failure_reason(binding_failure_reason),
                            output_parameter_value=secure_output,
                            status=BlockStatus.failed,
                            workflow_run_block_id=workflow_run_block_id,
                            organization_id=organization_id,
                        )
                    # The sidecar already wrote the block row with its own pre-binding return, and
                    # that row — not the output parameter — is what download-evidence readers use.
                    sidecar_result = secure_code_block_result.block_result
                    return await self.build_block_result(
                        success=sidecar_result.success,
                        failure_reason=scrub_failure_reason(sidecar_result.failure_reason),
                        output_parameter_value=secure_output,
                        status=sidecar_result.status,
                        workflow_run_block_id=workflow_run_block_id,
                        organization_id=organization_id,
                        error_codes=sidecar_result.error_codes or None,
                    )
                LOG.warning(
                    "codeblock.secure_runner_downgrade",
                    selection_reason="override_returned_none",
                    organization_id=organization_id,
                    workflow_run_id=workflow_run_id,
                    workflow_run_block_id=workflow_run_block_id,
                    block_label=self.label,
                )
                # The downgraded block now runs inline, so a probe still labelled secure_runner
                # would file this run's registrations under the engine that declined it.
                download_evidence = self._bind_download_evidence_probe(
                    engine="inline",
                    organization_id=organization_id,
                    workflow_run_id=workflow_run_id,
                    workflow_run_block_id=workflow_run_block_id,
                    session_bound=session_download_lane_active(browser_state),
                    resolved_download_id=resolved_download_id,
                )
            user_function = self.generate_async_user_function(
                self.code,
                recording_page,
                parameter_values,
                workflow_run_id=workflow_run_id,
                organization_id=organization_id,
                workflow_run_block_id=workflow_run_block_id,
                download_run_id=resolved_download_id,
                download_binding=download_binding_of(browser_state),
                download_evidence=download_evidence,
            )
            try:
                result = await self.execute_user_function_with_timeout(
                    user_function,
                    settings.CODE_BLOCK_EXECUTION_TIMEOUT_SECONDS,
                )
            finally:
                try:
                    async with asyncio.timeout(SAVE_DOWNLOADED_FILES_TIMEOUT):
                        async with settle_browser_downloads_for_context(page.context):
                            pass
                except asyncio.TimeoutError:
                    LOG.warning(
                        "Browser download settlement exceeded its CodeBlock registration budget",
                        settlement_timeout_seconds=SAVE_DOWNLOADED_FILES_TIMEOUT,
                        workflow_run_id=workflow_run_id,
                        workflow_run_block_id=workflow_run_block_id,
                        organization_id=organization_id,
                        engine="inline",
                    )
                except Exception:
                    LOG.warning(
                        "Browser download settlement failed before CodeBlock registration",
                        workflow_run_id=workflow_run_id,
                        workflow_run_block_id=workflow_run_block_id,
                        organization_id=organization_id,
                        engine="inline",
                    )
        except InsecureCodeDetected:
            await recorder.persist(recorder.recorded_actions())
            await recorder.finalize(success=False)
            return await self._failed_result_with_evidence(
                failure_reason=scrub_failure_reason("Insecure code detected.") or "",
                status=BlockStatus.failed,
                workflow_run_context=workflow_run_context,
                workflow_run_id=workflow_run_id,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
                browser_state=browser_state,
                page=page,
                engine="inline",
                resolved_download_id=resolved_download_id,
                download_dir_before=download_dir_before,
            )
        except asyncio.TimeoutError:
            await recorder.persist(recorder.recorded_actions())
            await recorder.finalize(success=False)
            return await self._failed_result_with_evidence(
                failure_reason=scrub_failure_reason(
                    "Failed to execute code block. Reason: TimeoutError: code block exceeded "
                    f"{settings.CODE_BLOCK_EXECUTION_TIMEOUT_SECONDS} seconds"
                )
                or "",
                status=BlockStatus.failed,
                workflow_run_context=workflow_run_context,
                workflow_run_id=workflow_run_id,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
                browser_state=browser_state,
                page=page,
                engine="inline",
                resolved_download_id=resolved_download_id,
                download_dir_before=download_dir_before,
            )
        except Exception as e:
            # Exact type, not isinstance: the IllegitCompleteScriptTermination subclass means the
            # complete-verifier rejected the block, which is a failure to heal, not an intentional stop.
            if type(e) is ScriptTerminationException:
                await recorder.persist(recorder.recorded_actions())
                await recorder.finalize(success=False)
                return await self._failed_result_with_evidence(
                    failure_reason=scrub_failure_reason("CodeBlock terminated.") or "",
                    status=BlockStatus.terminated,
                    workflow_run_context=workflow_run_context,
                    workflow_run_id=workflow_run_id,
                    workflow_run_block_id=workflow_run_block_id,
                    organization_id=organization_id,
                    browser_state=browser_state,
                    page=page,
                    engine="inline",
                    resolved_download_id=resolved_download_id,
                    download_dir_before=download_dir_before,
                )
            failing_line = user_code_line_from_exception(e)
            declared_error = self._extract_declared_error(e, workflow_run_context)
            if declared_error is not None:
                await recorder.persist(recorder.recorded_actions())
                declared_error_code = app.AGENT_FUNCTION.redact_codeblock_parameter_values(
                    declared_error.error_code, serialized_parameter_values
                )
                declared_reasoning = app.AGENT_FUNCTION.redact_codeblock_parameter_values(
                    declared_error.reasoning, serialized_parameter_values
                )
                failure_output = build_user_defined_error_output(declared_error_code, declared_reasoning)

                async def build_declared_failure_result() -> BlockResult:
                    download_aware_failure_output = await self._failure_output_with_downloads(
                        engine="inline",
                        workflow_run_context=workflow_run_context,
                        workflow_run_id=workflow_run_id,
                        workflow_run_block_id=workflow_run_block_id,
                        organization_id=organization_id,
                        resolved_download_id=resolved_download_id,
                        download_dir_before=download_dir_before,
                        session_bound=session_download_lane_active(browser_state),
                        download_binding_kind=download_binding_of(browser_state).value,
                        result=failure_output,
                    )
                    if download_aware_failure_output is None:
                        await self.record_output_parameter_value(workflow_run_context, workflow_run_id, failure_output)
                    return await self.build_block_result(
                        success=False,
                        failure_reason=declared_reasoning,
                        output_parameter_value=download_aware_failure_output or failure_output,
                        status=BlockStatus.failed,
                        workflow_run_block_id=workflow_run_block_id,
                        organization_id=organization_id,
                        error_codes=[declared_error_code],
                    )

                return await self._resolve_failure_with_heal(
                    exception=e,
                    failing_line=failing_line,
                    build_failure_result=build_declared_failure_result,
                    classification=HealClassification(healable=False, skip_reason=HealSkipReason.user_defined_error),
                    recorder=recorder,
                    workflow_run_context=workflow_run_context,
                    workflow_run_id=workflow_run_id,
                    workflow_run_block_id=workflow_run_block_id,
                    organization_id=organization_id,
                    browser_session_id=browser_session_id,
                    browser_state=browser_state,
                    page=page,
                    resolved_download_id=resolved_download_id,
                    download_dir_before=download_dir_before,
                    redaction_parameters=serialized_parameter_values,
                )
            if (
                type(e) is ErrorCode
                and e.error_code not in self._effective_error_code_mapping(workflow_run_context)
                and not self._contains_registered_secret(e.error_code, workflow_run_context)
            ):
                # The declaration is absent, so neither the caller-provided code nor reasoning is
                # trusted as a typed user error. Still identify the authoring defect in ordinary
                # run evidence so the test-run can guide the next edit.
                failure_reason = (
                    scrub_failure_reason(
                        "Failed to execute code block. Reason: ErrorCode is not declared in the "
                        "effective error_code_mapping"
                    )
                    or ""
                )
            elif type(e) is ErrorCode:
                failure_reason = (
                    scrub_failure_reason(
                        "Failed to execute code block. Reason: ErrorCode: CodeBlock raised a declared error"
                    )
                    or ""
                )
            elif isinstance(e, CodeBlockCredentialReleaseError):
                # The refusal names the credential's site and the page's site and carries no secret
                # value; it is the observation the next authoring iteration repairs from, so it is
                # the one exception whose own text is worth more than the generic reason.
                failure_reason = scrub_failure_reason(f"Failed to execute code block. Reason: {e}") or ""
            else:
                # User code can raise an exception carrying a resolved secret (e.g.
                # `raise Exception(await cred.otp())`) or a parameter value; mask both before the
                # cause reaches the persisted reason, and keep the bare reason when nothing survives.
                masked_message = workflow_run_context.mask_secrets_in_data(CustomizedCodeException(e).message)
                failure_reason = (
                    scrub_failure_reason(masked_message[:CODE_BLOCK_FAILURE_REASON_MAX_CHARS])
                    or CODE_BLOCK_GENERIC_FAILURE_REASON
                )
            inline_failure_page_state: dict[str, str] = {}
            if isinstance(e, PlaywrightTimeoutError):
                # Bind to the original exception before diagnostic proxy calls can alter recorder state.
                failed_locator = recording_page.failure_locator(e)
                inline_failure_page_state = await self._capture_inline_failure_page_state(
                    page=page,
                    failure_locator=failed_locator,
                    workflow_run_context=workflow_run_context,
                    redaction_parameters=serialized_parameter_values,
                )
            if inline_failure_page_state:
                from skyvern.forge.sdk.workflow.models.code_block_recorder import append_failure_page_state

                failure_reason = append_failure_page_state(failure_reason, **inline_failure_page_state)
            recorded = recorder.recorded_actions()
            if recorder.last_recorded_exception() is not e:
                # The exception did not come from a recorded page call; add a synthetic failure row.
                recorded.append(
                    _code_block_failure_action(
                        action_order=len(recorded),
                        response=failure_reason or "",
                        failing_line=failing_line,
                    )
                )
            await recorder.persist(recorded)
            engine_selection = (
                browser_state.engine_selection
                if browser_state and inspect.getattr_static(browser_state, "engine_selection", None) is not None
                else None
            )
            legacy_healable = self._is_healable_page_failure(
                e,
                recording_page,
                engine_selection,
            )
            legacy_classification = HealClassification(
                healable=legacy_healable,
                skip_reason=(
                    None
                    if legacy_healable
                    else (
                        HealSkipReason.credential_off_site
                        if isinstance(e, CodeBlockCredentialReleaseError)
                        else HealSkipReason.unclassifiable
                    )
                ),
            )

            async def build_legacy_failure_result() -> BlockResult:
                failure_output = None
                if inline_failure_page_state:
                    failure_output = build_block_failure_output(failure_reason, [])
                    failure_output["failure_page_state"] = inline_failure_page_state
                download_aware_failure_output = await self._failure_output_with_downloads(
                    engine="inline",
                    workflow_run_context=workflow_run_context,
                    workflow_run_id=workflow_run_id,
                    workflow_run_block_id=workflow_run_block_id,
                    organization_id=organization_id,
                    resolved_download_id=resolved_download_id,
                    download_dir_before=download_dir_before,
                    session_bound=session_download_lane_active(browser_state),
                    download_binding_kind=download_binding_of(browser_state).value,
                    result=failure_output,
                )
                if download_aware_failure_output is None and failure_output is not None:
                    await self.record_output_parameter_value(workflow_run_context, workflow_run_id, failure_output)
                # This deferred closure follows screenshot/heal evidence capture; restore the URL
                # observed at the original timeout so later page activity cannot overwrite it.
                await self._persist_captured_failure_final_url(
                    final_url=inline_failure_page_state.get("final_url"),
                    workflow_run_block_id=workflow_run_block_id,
                    organization_id=organization_id,
                )
                return await self.build_block_result(
                    success=False,
                    failure_reason=failure_reason,
                    output_parameter_value=download_aware_failure_output or failure_output,
                    status=BlockStatus.failed,
                    workflow_run_block_id=workflow_run_block_id,
                    organization_id=organization_id,
                )

            return await self._resolve_failure_with_heal(
                exception=e,
                failing_line=failing_line,
                build_failure_result=build_legacy_failure_result,
                classification=legacy_classification,
                recorder=recorder,
                workflow_run_context=workflow_run_context,
                workflow_run_id=workflow_run_id,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
                browser_session_id=browser_session_id,
                browser_state=browser_state,
                page=page,
                resolved_download_id=resolved_download_id,
                download_dir_before=download_dir_before,
                redaction_parameters=serialized_parameter_values,
            )

        else:
            await recorder.persist(recorder.recorded_actions())
            # A leaked recorder proxy (RecordingLocator/Page/Keyboard) is not JSON serializable and
            # would either crash registration or, via the default= fallback, replace the value with a
            # useless placeholder — normalize the wrapper family to its selector/marker first.
            result = json_safe_recorder_output(result)
            result = json.loads(
                json.dumps(result, default=lambda value: f"Object '{type(value)}' is not JSON serializable")
            )
            # Mask resolved secrets (OTP codes, passwords) a user assigned to a local before they
            # reach captured locals, the persisted output, or the logged value. Mirrors
            # HttpRequestBlock and is stronger than the name-based excluded_parameter_keys filter.
            result = workflow_run_context.mask_secrets_in_data(result)

            try:
                downloaded_files, skipped_file_names = await self._register_downloaded_files(
                    engine="inline",
                    download_binding_kind=download_binding_of(browser_state).value,
                    organization_id=organization_id or workflow_run_context.organization_id,
                    workflow_run_id=workflow_run_id,
                    workflow_run_block_id=workflow_run_block_id,
                    session_bound=session_download_lane_active(browser_state),
                    download_run_id=resolved_download_id,
                )
                result, binding_failure_reason = await self._bind_and_grade_downloads(
                    engine="inline",
                    result=result,
                    downloaded_files=downloaded_files,
                    skipped_file_names=skipped_file_names,
                    download_dir_before=download_dir_before,
                    resolved_download_id=resolved_download_id,
                    workflow_run_context=workflow_run_context,
                    workflow_run_id=workflow_run_id,
                    workflow_run_block_id=workflow_run_block_id,
                    organization_id=organization_id,
                )
            except asyncio.CancelledError:
                # A cancel inside the storage round-trips must not book the executed block as
                # failed or skip its billing hook; the verdict is moot on teardown.
                await recorder.finalize(success=True)
                raise
            await recorder.finalize(success=binding_failure_reason is None)
            await self.record_output_parameter_value(workflow_run_context, workflow_run_id, result)
            return await self.build_block_result(
                success=binding_failure_reason is None,
                failure_reason=scrub_failure_reason(binding_failure_reason),
                output_parameter_value=result,
                status=BlockStatus.completed if binding_failure_reason is None else BlockStatus.failed,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )
        finally:
            # Safety net for paths the except arms miss (CancelledError, link_block failure).
            await recorder.finalize(success=False)


SCHEMA_VALIDATION_MAX_ATTEMPTS = 2
SCHEMA_VALIDATION_MAX_ERRORS = 5


def _default_structured_output_schema(description: str) -> dict[str, Any]:
    # The output field is optional to preserve the legacy permissive default schema.
    return {
        "type": "object",
        "properties": {
            "output": {
                "type": "object",
                "description": description,
            }
        },
    }


def _default_text_prompt_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "llm_response": {
                "type": "string",
                "description": "Your response to the prompt",
            }
        },
    }


# Transient failures where a single bad response/connection should not fail the whole run.
# Mirrors the extraction-path treatment (SKY-8264): empty responses and retryable provider
# errors usually succeed on a second attempt, as do transient DB connection drops.
# DB errors are narrowed to the connection-loss family (OperationalError/InterfaceError) so a
# non-transient DB error (IntegrityError, DataError, ...) is not retried into a re-issued
# paid LLM call. Response-format errors are intentionally excluded — execute() already
# re-prompts on those with the validation feedback, which beats a blind retry.
TEXT_PROMPT_RETRIABLE_LLM_EXCEPTIONS: tuple[type[Exception], ...] = (
    EmptyLLMResponseError,
    LLMProviderErrorRetryableTask,
    OperationalError,
    InterfaceError,
)
TEXT_PROMPT_MAX_ATTEMPTS = 3


def _json_type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    if isinstance(value, str):
        return "string"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    return type(value).__name__


def _schema_type_description(schema_type: Any) -> str:
    if isinstance(schema_type, list):
        return " or ".join(str(t) for t in schema_type)
    return str(schema_type)


def _schema_path(error: ValidationError) -> str:
    schema_path = list(error.absolute_schema_path)
    path_parts: list[str] = []
    index = 0
    while index < len(schema_path):
        part = schema_path[index]
        if part == "properties" and index + 1 < len(schema_path):
            path_parts.append(str(schema_path[index + 1]))
            index += 2
            continue
        if part == "items":
            path_parts.append("[]")
            index += 1
            continue
        if part == "additionalProperties":
            path_parts.append("<map value>")
            index += 1
            continue
        if part == "patternProperties":
            path_parts.append("<map value>")
            index += 2 if index + 1 < len(schema_path) else 1
            continue
        index += 1

    return "root" + "".join(f"{part}" if part == "[]" else f".{part}" for part in path_parts)


def _format_schema_validation_error(error: ValidationError) -> str:
    path = _schema_path(error)
    actual_type = _json_type_name(error.instance)

    if error.validator == "type":
        expected_type = _schema_type_description(error.validator_value)
        return f"{path}: expected type {expected_type}, got {actual_type}"

    if error.validator == "required":
        match = re.match(r"'([^']+)' is a required property", error.message)
        if match:
            return f"{path}: missing required property {match.group(1)}"
        return f"{path}: missing required property"

    if error.validator == "additionalProperties":
        unexpected_count: int | None = None
        schema_properties = error.schema.get("properties", {}) if isinstance(error.schema, dict) else {}
        if isinstance(error.instance, dict) and isinstance(schema_properties, dict):
            unexpected_count = sum(1 for field in error.instance if field not in schema_properties)
        if unexpected_count is not None:
            return f"{path}: has {unexpected_count} unexpected properties"
        return f"{path}: has unexpected properties"

    if error.validator in {"minItems", "maxItems"} and isinstance(error.instance, list):
        return f"{path}: violates {error.validator}={error.validator_value}; item count={len(error.instance)}"

    if error.validator in {"minLength", "maxLength"} and isinstance(error.instance, str):
        return f"{path}: violates {error.validator}={error.validator_value}; string length={len(error.instance)}"

    if error.validator == "enum":
        allowed_count = len(error.validator_value) if isinstance(error.validator_value, list) else "configured"
        return f"{path}: value is not one of {allowed_count} allowed values; got {actual_type}"

    return f"{path}: violates {error.validator} constraint; got {actual_type}"


def _validate_response_against_json_schema(
    response: Any,
    json_schema: dict[str, Any] | None,
    schema_label: str,
    max_errors: int = SCHEMA_VALIDATION_MAX_ERRORS,
) -> str | None:
    if not json_schema:
        return None

    if not validate_schema(json_schema):
        return f"{schema_label} JSON schema is invalid."

    try:
        validator = Draft202012Validator(json_schema)
        validation_errors = [_format_schema_validation_error(error) for error in validator.iter_errors(response)]
    except Exception as e:
        LOG.warning(
            "Failed to validate LLM response against JSON schema",
            schema_label=schema_label,
            error_type=type(e).__name__,
            exc_info=True,
        )
        return f"{schema_label} JSON schema validation failed ({type(e).__name__})."

    validation_errors = list(dict.fromkeys(validation_errors))
    if not validation_errors:
        return None

    return f"LLM response does not match {schema_label.lower()} JSON schema: " + "; ".join(
        validation_errors[:max_errors]
    )


def _is_schema_configuration_failure(failure_reason: str) -> bool:
    return "JSON schema is invalid" in failure_reason or "JSON schema validation failed" in failure_reason


def _llm_response_format_failure_reason(error: Exception) -> str:
    return f"LLM response could not be parsed or coerced into the required JSON shape ({type(error).__name__})."


def _build_schema_validation_retry_prompt(prompt: str, failure_reason: str) -> str:
    return (
        f"{prompt}\n\n"
        "Your previous response failed JSON schema validation.\n"
        f"Validation error: {failure_reason}\n\n"
        "Retry the task. Return only valid JSON that exactly matches the schema. "
        "Do not include markdown, code fences, explanatory text, or extra fields."
    )


class TextPromptBlock(Block):
    # There is a mypy bug with Literal. Without the type: ignore, mypy will raise an error:
    # Parameter 1 of Literal[...] cannot be of type "Any"
    block_type: Literal[BlockType.TEXT_PROMPT] = BlockType.TEXT_PROMPT  # type: ignore

    llm_key: str | None = None
    prompt: str
    parameters: list[PARAMETER_TYPE] = []
    json_schema: dict[str, Any] | None = None
    schema_validation_max_attempts: ClassVar[int] = SCHEMA_VALIDATION_MAX_ATTEMPTS
    schema_validation_max_errors: ClassVar[int] = SCHEMA_VALIDATION_MAX_ERRORS

    TEMPLATABLE_FIELDS: ClassVar[frozenset[str]] = frozenset({"json_schema", "llm_key", "prompt"})

    def _own_llm_key(self) -> str | None:
        return self.llm_key

    def get_all_parameters(
        self,
        workflow_run_id: str,
    ) -> list[PARAMETER_TYPE]:
        return self.parameters

    def _render_schema_templates(self, obj: Any, workflow_run_context: WorkflowRunContext) -> Any:
        if isinstance(obj, str):
            try:
                return self.render_templatable_field("json_schema", obj, workflow_run_context)
            except Exception:
                LOG.warning(
                    "Failed to render Jinja template in json_schema value, using original value",
                    value=obj,
                    block_label=self.label,
                    exc_info=True,
                )
                return obj
        elif isinstance(obj, dict):
            return {k: self._render_schema_templates(v, workflow_run_context) for k, v in obj.items()}
        elif isinstance(obj, list):
            return [self._render_schema_templates(item, workflow_run_context) for item in obj]
        return obj

    def format_potential_template_parameters(self, workflow_run_context: WorkflowRunContext) -> None:
        if self.llm_key:
            self.llm_key = self.render_templatable_field("llm_key", self.llm_key, workflow_run_context)
        self.prompt = self.render_templatable_field("prompt", self.prompt, workflow_run_context)
        if self.json_schema:
            self.json_schema = self._render_schema_templates(self.json_schema, workflow_run_context)

        self._apply_workflow_system_prompt(workflow_run_context)

    def _validate_response_against_json_schema(self, response: Any) -> str | None:
        return _validate_response_against_json_schema(
            response,
            self.json_schema,
            "Text prompt",
            max_errors=self.schema_validation_max_errors,
        )

    async def send_prompt(
        self,
        prompt: str,
        workflow_run_id: str,
        organization_id: str | None = None,
        workflow_run_block_id: str | None = None,
        schema_validation_failure: str | None = None,
        json_schema: dict[str, Any] | None = None,
    ) -> dict[str, Any] | list | str | None:
        default_llm_handler = await self._resolve_default_llm_handler(workflow_run_id, organization_id)
        selected_llm_key = self.override_llm_key_for_organization(organization_id) or self.llm_key
        # When the block sets no key (org-default path), derive the effective key from the
        # resolved default handler (mirrors get_override_llm_api_handler) so the fallback
        # upgrade still fires for default-configured Gemini runs. Used for the lookup only —
        # selected_llm_key is overwritten solely when a fallback twin exists, so the
        # no-fallback path stays identical to passing the original key/None.
        fallback_lookup_key = (
            selected_llm_key
            or getattr(default_llm_handler, "llm_key", None)
            or getattr(getattr(default_llm_handler, "__self__", None), "llm_key", None)
        )
        fallback_llm_key = app.AGENT_FUNCTION.get_fallback_llm_key(fallback_lookup_key)
        if fallback_llm_key:
            LOG.info(
                "TextPromptBlock upgrading to fallback-capable LLM config",
                block_label=self.label,
                original_llm_key=fallback_lookup_key,
                fallback_llm_key=fallback_llm_key,
            )
            selected_llm_key = fallback_llm_key
        llm_api_handler = LLMAPIHandlerFactory.get_override_llm_api_handler(
            selected_llm_key, default=default_llm_handler
        )
        schema_to_use = json_schema or self.json_schema or _default_text_prompt_schema()

        # `prompt` is already fully rendered by format_potential_template_parameters().
        # Keep send_prompt focused on delivery/retry formatting so parameter values
        # stay literal after that render.
        if schema_validation_failure:
            prompt = _build_schema_validation_retry_prompt(prompt, schema_validation_failure)
        prompt += (
            "\n\n"
            + "Please respond to the prompt above using the following JSON definition:\n\n"
            + "```json\n"
            + json.dumps(schema_to_use, indent=2)
            + "\n```\n\n"
        )

        workflow_run_block = None
        artifacts_to_persist: list[tuple[ArtifactType, bytes]] = []
        if workflow_run_block_id:
            try:
                workflow_run_block = await app.DATABASE.observer.get_workflow_run_block(
                    workflow_run_block_id, organization_id
                )
                if workflow_run_block:
                    artifacts_to_persist.append((ArtifactType.LLM_PROMPT, prompt.encode("utf-8")))
            except Exception as e:
                LOG.error("Failed to fetch workflow_run_block for TextPromptBlock artifacts", error=e)

        LOG.info(
            "TextPromptBlock Sending prompt to LLM",
            prompt=prompt,
            llm_key=self.llm_key,
        )
        response: dict[str, Any] | list | str | None = None
        for attempt in range(TEXT_PROMPT_MAX_ATTEMPTS):
            try:
                response = await llm_api_handler(
                    prompt=prompt,
                    prompt_name="text-prompt",
                    system_prompt=self.workflow_system_prompt,
                    workflow_run_block_id=workflow_run_block_id,
                    organization_id=organization_id,
                    # Schema validation must inspect the raw parsed root; dict coercion can hide wrong-root responses.
                    force_dict=False,
                )
                break
            except TEXT_PROMPT_RETRIABLE_LLM_EXCEPTIONS as e:
                if attempt >= TEXT_PROMPT_MAX_ATTEMPTS - 1:
                    LOG.warning(
                        "TextPromptBlock LLM call failed after all retries",
                        block_label=self.label,
                        attempts=attempt + 1,
                        error=str(e),
                    )
                    raise
                backoff_time = 0.2 * (2**attempt)
                LOG.warning(
                    "Transient TextPromptBlock LLM/DB failure, retrying",
                    block_label=self.label,
                    attempt=attempt + 1,
                    max_attempts=TEXT_PROMPT_MAX_ATTEMPTS,
                    backoff_time=backoff_time,
                    error=str(e),
                )
                await asyncio.sleep(backoff_time)

        if workflow_run_block:
            artifacts_to_persist.append((ArtifactType.LLM_RESPONSE, json.dumps(response).encode("utf-8")))
            try:
                await app.ARTIFACT_MANAGER.create_workflow_run_block_artifacts(
                    workflow_run_block=workflow_run_block,
                    artifacts=artifacts_to_persist,
                )
            except Exception as e:
                LOG.error("Failed to save TextPromptBlock artifacts", error=e)

        LOG.info("TextPromptBlock Received response from LLM", response=response)
        return response

    async def _resolve_default_llm_handler(self, workflow_run_id: str, organization_id: str | None) -> LLMAPIHandler:
        prompt_config_handler = await get_llm_handler_for_prompt_type("text-prompt", workflow_run_id, organization_id)
        if prompt_config_handler:
            return prompt_config_handler

        secondary_handler = get_org_aware_secondary_llm_api_handler(default=app.SECONDARY_LLM_API_HANDLER)
        if secondary_handler:
            return secondary_handler

        LOG.warning(
            "Secondary LLM handler not configured; falling back to primary handler for TextPromptBlock",
            workflow_run_id=workflow_run_id,
            organization_id=organization_id,
        )
        return get_org_aware_primary_llm_api_handler(default=app.LLM_API_HANDLER)

    async def execute(
        self,
        workflow_run_id: str,
        workflow_run_block_id: str,
        organization_id: str | None = None,
        browser_session_id: str | None = None,
        **kwargs: dict,
    ) -> BlockResult:
        # Validate block execution
        await app.AGENT_FUNCTION.validate_block_execution(
            block=self,
            workflow_run_block_id=workflow_run_block_id,
            workflow_run_id=workflow_run_id,
            organization_id=organization_id,
        )
        # get workflow run context
        workflow_run_context = self.get_workflow_run_context(workflow_run_id)
        await app.DATABASE.observer.update_workflow_run_block(
            workflow_run_block_id=workflow_run_block_id,
            organization_id=organization_id,
            prompt=self.prompt,
        )
        try:
            self.format_potential_template_parameters(workflow_run_context)
        except Exception as e:
            return await self._template_format_failure_result(
                e,
                f"Failed to format jinja template: {str(e)}",
                workflow_run_context,
                workflow_run_id,
                workflow_run_block_id,
                organization_id,
            )
        for parameter in self.parameters:
            if not workflow_run_context.has_value(parameter.key):
                LOG.warning(
                    "TextPromptBlock missing required parameter",
                    block_label=self.label,
                    parameter_key=parameter.key,
                    workflow_run_id=workflow_run_id,
                )
                return await self.build_block_result(
                    success=False,
                    failure_reason=(
                        f"Parameter '{parameter.key}' is not available in the workflow context. "
                        f"An upstream block that produces this value may have failed or been skipped."
                    ),
                    output_parameter_value=None,
                    status=BlockStatus.failed,
                    workflow_run_block_id=workflow_run_block_id,
                    organization_id=organization_id,
                )

        response: dict[str, Any] | list | str | None = None
        schema_to_use = self.json_schema or _default_text_prompt_schema()
        if not validate_schema(schema_to_use):
            return await self.build_block_result(
                success=False,
                failure_reason="Text prompt JSON schema is invalid.",
                output_parameter_value=None,
                status=BlockStatus.failed,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )

        schema_validation_failure_for_retry: str | None = None
        for attempt in range(self.schema_validation_max_attempts):
            try:
                response = await self.send_prompt(
                    self.prompt,
                    workflow_run_id,
                    organization_id,
                    workflow_run_block_id=workflow_run_block_id,
                    schema_validation_failure=schema_validation_failure_for_retry,
                    json_schema=schema_to_use,
                )
            except (InvalidLLMResponseFormat, InvalidLLMResponseType) as e:
                response_format_failure_reason = _llm_response_format_failure_reason(e)
                will_retry = attempt + 1 < self.schema_validation_max_attempts
                LOG.warning(
                    "TextPromptBlock LLM response failed response-format validation",
                    block_label=self.label,
                    workflow_run_id=workflow_run_id,
                    workflow_run_block_id=workflow_run_block_id,
                    attempt=attempt + 1,
                    max_attempts=self.schema_validation_max_attempts,
                    will_retry=will_retry,
                    error_type=type(e).__name__,
                    schema_type=schema_to_use.get("type"),
                )
                if not will_retry:
                    return await self.build_block_result(
                        success=False,
                        failure_reason=response_format_failure_reason,
                        output_parameter_value=None,
                        status=BlockStatus.failed,
                        workflow_run_block_id=workflow_run_block_id,
                        organization_id=organization_id,
                    )

                schema_validation_failure_for_retry = response_format_failure_reason
                continue
            except Exception as e:
                try:
                    resolved_llm_key = self.override_llm_key_for_organization(organization_id) or self.llm_key
                except Exception:
                    resolved_llm_key = self.llm_key
                LOG.exception(
                    "TextPromptBlock LLM call failed",
                    block_label=self.label,
                    workflow_run_id=workflow_run_id,
                    llm_key=resolved_llm_key,
                )
                return await self.build_block_result(
                    success=False,
                    failure_reason=f"LLM call failed: {e}",
                    output_parameter_value=None,
                    status=BlockStatus.failed,
                    workflow_run_block_id=workflow_run_block_id,
                    organization_id=organization_id,
                )

            schema_validation_failure = _validate_response_against_json_schema(
                response,
                schema_to_use,
                "Text prompt",
                max_errors=self.schema_validation_max_errors,
            )
            if not schema_validation_failure:
                break

            is_schema_configuration_failure = _is_schema_configuration_failure(schema_validation_failure)
            will_retry = attempt + 1 < self.schema_validation_max_attempts and not is_schema_configuration_failure
            LOG.warning(
                "TextPromptBlock LLM response failed schema validation",
                block_label=self.label,
                workflow_run_id=workflow_run_id,
                workflow_run_block_id=workflow_run_block_id,
                attempt=attempt + 1,
                max_attempts=self.schema_validation_max_attempts,
                will_retry=will_retry,
                failure_reason=schema_validation_failure,
                schema_type=schema_to_use.get("type"),
            )
            if not will_retry:
                return await self.build_block_result(
                    success=False,
                    failure_reason=schema_validation_failure,
                    output_parameter_value=None,
                    status=BlockStatus.failed,
                    workflow_run_block_id=workflow_run_block_id,
                    organization_id=organization_id,
                )

            schema_validation_failure_for_retry = schema_validation_failure
            continue

        await self.record_output_parameter_value(workflow_run_context, workflow_run_id, response)
        return await self.build_block_result(
            success=True,
            failure_reason=None,
            output_parameter_value=response,
            status=BlockStatus.completed,
            workflow_run_block_id=workflow_run_block_id,
            organization_id=organization_id,
        )


class DownloadToS3Block(Block):
    # There is a mypy bug with Literal. Without the type: ignore, mypy will raise an error:
    # Parameter 1 of Literal[...] cannot be of type "Any"
    block_type: Literal[BlockType.DOWNLOAD_TO_S3] = BlockType.DOWNLOAD_TO_S3  # type: ignore

    url: str

    TEMPLATABLE_FIELDS: ClassVar[frozenset[str]] = frozenset({"url"})

    def get_all_parameters(
        self,
        workflow_run_id: str,
    ) -> list[PARAMETER_TYPE]:
        workflow_run_context = self.get_workflow_run_context(workflow_run_id)

        if self.url and workflow_run_context.has_parameter(self.url):
            return [workflow_run_context.get_parameter(self.url)]

        return []

    def format_potential_template_parameters(self, workflow_run_context: WorkflowRunContext) -> None:
        self.url = self.render_templatable_field("url", self.url, workflow_run_context)

    async def _upload_file_to_s3(self, uri: str, file_path: str, cleanup_file: bool = True) -> None:
        try:
            client = self.get_async_aws_client()
            await client.upload_file_from_path(uri=uri, file_path=file_path)
        finally:
            # Clean up the temporary file since it's created with delete=False
            if cleanup_file:
                os.unlink(file_path)

    async def execute(
        self,
        workflow_run_id: str,
        workflow_run_block_id: str,
        organization_id: str | None = None,
        browser_session_id: str | None = None,
        **kwargs: dict,
    ) -> BlockResult:
        # get workflow run context
        workflow_run_context = self.get_workflow_run_context(workflow_run_id)
        # get all parameters into a dictionary
        if self.url and workflow_run_context.has_parameter(self.url) and workflow_run_context.has_value(self.url):
            task_url_parameter_value = workflow_run_context.get_value(self.url)
            if task_url_parameter_value:
                LOG.info(
                    "DownloadToS3Block Task URL is parameterized, using parameter value",
                    task_url_parameter_value=task_url_parameter_value,
                    task_url_parameter_key=self.url,
                )
                self.url = task_url_parameter_value
            else:
                # Absent optional parameter: fail on the empty URL rather than
                # navigating to the literal parameter key.
                self.url = ""

        try:
            self.format_potential_template_parameters(workflow_run_context)
        except Exception as e:
            return await self._template_format_failure_result(
                e,
                f"Failed to format jinja template: {str(e)}",
                workflow_run_context,
                workflow_run_id,
                workflow_run_block_id,
                organization_id,
            )

        try:
            context = skyvern_context.current()
            run_id = context.run_id if context and context.run_id else workflow_run_id
            file_path = await resolve_local_or_download_file(
                self.url, run_id, organization_id=organization_id, max_size_mb=10
            )
        except Exception as e:
            LOG.error("DownloadToS3Block Failed to download file", url=self.url, error=str(e))
            raise e

        uri = None
        try:
            uri = f"s3://{settings.AWS_S3_BUCKET_UPLOADS}/{settings.ENV}/{workflow_run_id}/{uuid.uuid4()}"
            await self._upload_file_to_s3(uri, file_path, cleanup_file=not self.url.startswith("/"))
        except Exception as e:
            LOG.error("DownloadToS3Block Failed to upload file to S3", uri=uri, error=str(e))
            raise e

        LOG.info("DownloadToS3Block File downloaded and uploaded to S3", uri=uri)
        await self.record_output_parameter_value(workflow_run_context, workflow_run_id, uri)
        return await self.build_block_result(
            success=True,
            failure_reason=None,
            output_parameter_value=uri,
            status=BlockStatus.completed,
            workflow_run_block_id=workflow_run_block_id,
            organization_id=organization_id,
        )


class UploadToS3Block(Block):
    # There is a mypy bug with Literal. Without the type: ignore, mypy will raise an error:
    # Parameter 1 of Literal[...] cannot be of type "Any"
    block_type: Literal[BlockType.UPLOAD_TO_S3] = BlockType.UPLOAD_TO_S3  # type: ignore

    # TODO (kerem): A directory upload is supported but we should also support a list of files
    path: str | None = None

    TEMPLATABLE_FIELDS: ClassVar[frozenset[str]] = frozenset({"path"})

    def get_all_parameters(
        self,
        workflow_run_id: str,
    ) -> list[PARAMETER_TYPE]:
        workflow_run_context = self.get_workflow_run_context(workflow_run_id)

        if self.path and workflow_run_context.has_parameter(self.path):
            return [workflow_run_context.get_parameter(self.path)]

        return []

    def format_potential_template_parameters(self, workflow_run_context: WorkflowRunContext) -> None:
        if self.path:
            self.path = self.render_templatable_field("path", self.path, workflow_run_context)

    @staticmethod
    def _get_s3_uri(workflow_run_id: str, path: str) -> str:
        s3_bucket = settings.AWS_S3_BUCKET_UPLOADS
        s3_key = f"{settings.ENV}/{workflow_run_id}/{uuid.uuid4()}_{Path(path).name}"
        return f"s3://{s3_bucket}/{s3_key}"

    async def execute(
        self,
        workflow_run_id: str,
        workflow_run_block_id: str,
        organization_id: str | None = None,
        browser_session_id: str | None = None,
        **kwargs: dict,
    ) -> BlockResult:
        # get workflow run context
        workflow_run_context = self.get_workflow_run_context(workflow_run_id)
        # get all parameters into a dictionary
        if self.path and workflow_run_context.has_parameter(self.path) and workflow_run_context.has_value(self.path):
            file_path_parameter_value = workflow_run_context.get_value(self.path)
            if file_path_parameter_value:
                LOG.info(
                    "UploadToS3Block File path is parameterized, using parameter value",
                    file_path_parameter_value=file_path_parameter_value,
                    file_path_parameter_key=self.path,
                )
                self.path = file_path_parameter_value
            else:
                # Absent optional parameter: fail on the empty path rather than
                # treating the literal parameter key as a path.
                self.path = None
        # if the path is WORKFLOW_DOWNLOAD_DIRECTORY_PARAMETER_KEY, use the download directory for the workflow run
        elif self.path == settings.WORKFLOW_DOWNLOAD_DIRECTORY_PARAMETER_KEY:
            context = skyvern_context.current()
            self.path = str(
                get_path_for_workflow_download_directory(
                    resolve_run_download_id(context, fallback_run_id=workflow_run_id)
                ).absolute()
            )

        try:
            self.format_potential_template_parameters(workflow_run_context)
        except Exception as e:
            return await self._template_format_failure_result(
                e,
                f"Failed to format jinja template: {str(e)}",
                workflow_run_context,
                workflow_run_id,
                workflow_run_block_id,
                organization_id,
            )

        if not self.path:
            raise ValueError("UploadToS3Block path is required")

        context = skyvern_context.current()
        run_id = context.run_id if context and context.run_id else workflow_run_id
        resolved_path = validate_local_file_path(self.path, run_id)

        if not os.path.exists(resolved_path):
            raise FileNotFoundError(f"UploadToS3Block File not found at path: {resolved_path}")

        s3_uris = []
        try:
            client = self.get_async_aws_client()
            # is the file path a file or a directory?
            if os.path.isdir(resolved_path):
                files = os.listdir(resolved_path)
                if len(files) > MAX_UPLOAD_FILE_COUNT:
                    raise ValueError("Too many files in the directory, not uploading")
                for file in files:
                    # if the file is a directory, we will not upload it
                    if os.path.isdir(os.path.join(resolved_path, file)):
                        LOG.warning("UploadToS3Block Skipping directory", file=file)
                        continue
                    file_path = os.path.join(resolved_path, file)
                    s3_uri = self._get_s3_uri(workflow_run_id, file_path)
                    s3_uris.append(s3_uri)
                    await client.upload_file_from_path(uri=s3_uri, file_path=file_path)
            else:
                s3_uri = self._get_s3_uri(workflow_run_id, resolved_path)
                s3_uris.append(s3_uri)
                await client.upload_file_from_path(uri=s3_uri, file_path=resolved_path)
        except Exception as e:
            LOG.exception("UploadToS3Block Failed to upload file to S3", file_path=self.path)
            raise e

        LOG.info("UploadToS3Block File(s) uploaded to S3", file_path=self.path)
        await self.record_output_parameter_value(workflow_run_context, workflow_run_id, s3_uris)
        return await self.build_block_result(
            success=True,
            failure_reason=None,
            output_parameter_value=s3_uris,
            status=BlockStatus.completed,
            workflow_run_block_id=workflow_run_block_id,
            organization_id=organization_id,
        )


class UnsupportedStorageTypeError(Exception):
    pass


async def _resolve_sensitive_block_secret(
    workflow_run_context: WorkflowRunContext,
    value: str | None,
    field_name: str,
) -> str | None:
    """Resolve a sensitive block field: decrypt an at-rest encrypted value, map a secret
    parameter reference back to its original value, or fall back to the literal value."""
    if not value:
        return None
    if is_encrypted_secret(value):
        return await decrypt_secret_field_value(
            value,
            organization_id=workflow_run_context.organization_id,
            field_name=field_name,
        )
    resolved_value = workflow_run_context.get_original_secret_value_or_none(value)
    if resolved_value is not None:
        return resolved_value
    return value


def _resolve_block_secret_reference(workflow_run_context: WorkflowRunContext, value: str | None) -> str | None:
    """Map a masked secret placeholder back to its original value; anything else passes through.

    Destination blocks render templates with secrets excluded, so a field bound to a secret
    parameter arrives here as a placeholder token rather than the value it stands for.
    """
    if not value:
        return value
    resolved_value = workflow_run_context.get_original_secret_value_or_none(value)
    return value if resolved_value is None else resolved_value


class FileDestinationBlock(Block):
    s3_bucket: str | None = None
    aws_access_key_id: str | None = None
    aws_secret_access_key: str | None = None
    region_name: str | None = None
    endpoint_url: str | None = None
    azure_storage_account_name: str | None = None
    azure_storage_account_key: str | None = None
    azure_blob_container_name: str | None = None
    google_credential_id: str | None = None
    google_drive_folder_id: str | None = None
    sftp_host: str | None = None
    sftp_port: int | None = None
    sftp_username: str | None = None
    sftp_password: str | None = None
    sftp_private_key: str | None = None
    sftp_private_key_passphrase: str | None = None
    sftp_remote_path: str | None = None
    sftp_host_key: str | None = None
    prompt: str | None = Field(
        default=None,
        description="Optional natural-language control over which downloaded files are uploaded; empty means upload all.",
    )
    path: str | None = None
    continue_on_empty: bool = Field(
        default=False,
        description=(
            "When the run download directory has no files, allow the empty upload only after confirming no registered, "
            "browser-session, or alternate candidate downloads exist (False, default). Set True to always allow an "
            "empty upload."
        ),
    )

    _normalize_endpoint_url = field_validator("endpoint_url")(_normalize_optional_endpoint_url)

    TEMPLATABLE_FIELDS: ClassVar[frozenset[str]] = frozenset(
        {
            "aws_access_key_id",
            "aws_secret_access_key",
            "azure_blob_container_name",
            "azure_storage_account_key",
            "azure_storage_account_name",
            "endpoint_url",
            "google_credential_id",
            "google_drive_folder_id",
            "path",
            "prompt",
            "s3_bucket",
            "sftp_host",
            "sftp_host_key",
            "sftp_password",
            "sftp_private_key",
            "sftp_private_key_passphrase",
            "sftp_remote_path",
            "sftp_username",
        }
    )

    def _get_destination_parameters(self, workflow_run_context: WorkflowRunContext) -> list[PARAMETER_TYPE]:
        parameters = []

        if self.path and workflow_run_context.has_parameter(self.path):
            parameters.append(workflow_run_context.get_parameter(self.path))

        if self.prompt and workflow_run_context.has_parameter(self.prompt):
            parameters.append(workflow_run_context.get_parameter(self.prompt))

        if self.s3_bucket and workflow_run_context.has_parameter(self.s3_bucket):
            parameters.append(workflow_run_context.get_parameter(self.s3_bucket))

        if self.aws_access_key_id and workflow_run_context.has_parameter(self.aws_access_key_id):
            parameters.append(workflow_run_context.get_parameter(self.aws_access_key_id))

        if self.aws_secret_access_key and workflow_run_context.has_parameter(self.aws_secret_access_key):
            parameters.append(workflow_run_context.get_parameter(self.aws_secret_access_key))

        if self.endpoint_url and workflow_run_context.has_parameter(self.endpoint_url):
            parameters.append(workflow_run_context.get_parameter(self.endpoint_url))

        if self.azure_storage_account_name and workflow_run_context.has_parameter(self.azure_storage_account_name):
            parameters.append(workflow_run_context.get_parameter(self.azure_storage_account_name))

        if self.azure_storage_account_key and workflow_run_context.has_parameter(self.azure_storage_account_key):
            parameters.append(workflow_run_context.get_parameter(self.azure_storage_account_key))

        if self.azure_blob_container_name and workflow_run_context.has_parameter(self.azure_blob_container_name):
            parameters.append(workflow_run_context.get_parameter(self.azure_blob_container_name))

        if self.google_credential_id and workflow_run_context.has_parameter(self.google_credential_id):
            parameters.append(workflow_run_context.get_parameter(self.google_credential_id))

        if self.google_drive_folder_id and workflow_run_context.has_parameter(self.google_drive_folder_id):
            parameters.append(workflow_run_context.get_parameter(self.google_drive_folder_id))

        if self.sftp_host and workflow_run_context.has_parameter(self.sftp_host):
            parameters.append(workflow_run_context.get_parameter(self.sftp_host))

        if self.sftp_username and workflow_run_context.has_parameter(self.sftp_username):
            parameters.append(workflow_run_context.get_parameter(self.sftp_username))

        if self.sftp_password and workflow_run_context.has_parameter(self.sftp_password):
            parameters.append(workflow_run_context.get_parameter(self.sftp_password))

        if self.sftp_private_key and workflow_run_context.has_parameter(self.sftp_private_key):
            parameters.append(workflow_run_context.get_parameter(self.sftp_private_key))

        if self.sftp_private_key_passphrase and workflow_run_context.has_parameter(self.sftp_private_key_passphrase):
            parameters.append(workflow_run_context.get_parameter(self.sftp_private_key_passphrase))

        if self.sftp_remote_path and workflow_run_context.has_parameter(self.sftp_remote_path):
            parameters.append(workflow_run_context.get_parameter(self.sftp_remote_path))

        if self.sftp_host_key and workflow_run_context.has_parameter(self.sftp_host_key):
            parameters.append(workflow_run_context.get_parameter(self.sftp_host_key))

        return parameters

    def _format_destination_template_parameters(self, workflow_run_context: WorkflowRunContext) -> None:
        if self.path:
            self.path = self.render_templatable_field("path", self.path, workflow_run_context)

        if self.prompt:
            self.prompt = self.render_templatable_field("prompt", self.prompt, workflow_run_context)

        if self.s3_bucket:
            self.s3_bucket = self.render_templatable_field("s3_bucket", self.s3_bucket, workflow_run_context)
        if self.aws_access_key_id:
            self.aws_access_key_id = self.render_templatable_field(
                "aws_access_key_id", self.aws_access_key_id, workflow_run_context
            )
        if self.aws_secret_access_key:
            self.aws_secret_access_key = self.render_templatable_field(
                "aws_secret_access_key", self.aws_secret_access_key, workflow_run_context
            )
        if self.endpoint_url:
            self.endpoint_url = self.render_templatable_field("endpoint_url", self.endpoint_url, workflow_run_context)
        if self.azure_storage_account_name:
            self.azure_storage_account_name = self.render_templatable_field(
                "azure_storage_account_name", self.azure_storage_account_name, workflow_run_context
            )
        if self.azure_storage_account_key:
            self.azure_storage_account_key = self.render_templatable_field(
                "azure_storage_account_key", self.azure_storage_account_key, workflow_run_context
            )
        if self.azure_blob_container_name:
            self.azure_blob_container_name = self.render_templatable_field(
                "azure_blob_container_name", self.azure_blob_container_name, workflow_run_context
            )
        if self.google_credential_id:
            self.google_credential_id = self.render_templatable_field(
                "google_credential_id", self.google_credential_id, workflow_run_context
            )
        if self.google_drive_folder_id:
            self.google_drive_folder_id = self.render_templatable_field(
                "google_drive_folder_id", self.google_drive_folder_id, workflow_run_context
            )
        if self.sftp_host:
            self.sftp_host = self.render_templatable_field("sftp_host", self.sftp_host, workflow_run_context)
        if self.sftp_username:
            self.sftp_username = self.render_templatable_field(
                "sftp_username", self.sftp_username, workflow_run_context
            )
        if self.sftp_password:
            self.sftp_password = self.render_templatable_field(
                "sftp_password", self.sftp_password, workflow_run_context
            )
        if self.sftp_private_key:
            self.sftp_private_key = self.render_templatable_field(
                "sftp_private_key", self.sftp_private_key, workflow_run_context
            )
        if self.sftp_private_key_passphrase:
            self.sftp_private_key_passphrase = self.render_templatable_field(
                "sftp_private_key_passphrase", self.sftp_private_key_passphrase, workflow_run_context
            )
        if self.sftp_remote_path:
            self.sftp_remote_path = self.render_templatable_field(
                "sftp_remote_path", self.sftp_remote_path, workflow_run_context
            )
        if self.sftp_host_key:
            self.sftp_host_key = self.render_templatable_field(
                "sftp_host_key", self.sftp_host_key, workflow_run_context
            )

    def _validate_destination_fields(self, storage_type: FileStorageType) -> list[str]:
        missing_parameters = []
        if storage_type == FileStorageType.S3:
            if not self.s3_bucket:
                missing_parameters.append("s3_bucket")
            if not self.aws_access_key_id:
                missing_parameters.append("aws_access_key_id")
            if not self.aws_secret_access_key:
                missing_parameters.append("aws_secret_access_key")
        elif storage_type == FileStorageType.AZURE:
            if not self.azure_storage_account_name or self.azure_storage_account_name == "":
                missing_parameters.append("azure_storage_account_name")
            if not self.azure_storage_account_key or self.azure_storage_account_key == "":
                missing_parameters.append("azure_storage_account_key")
            if not self.azure_blob_container_name or self.azure_blob_container_name == "":
                missing_parameters.append("azure_blob_container_name")
        elif storage_type == FileStorageType.GOOGLE_DRIVE:
            if not self.google_credential_id:
                missing_parameters.append("google_credential_id")
        elif storage_type == FileStorageType.SFTP:
            if not self.sftp_host:
                missing_parameters.append("sftp_host")
            if not self.sftp_username:
                missing_parameters.append("sftp_username")
            if not self.sftp_password and not self.sftp_private_key:
                missing_parameters.append("sftp_password or sftp_private_key")
        else:
            raise UnsupportedStorageTypeError(storage_type)

        return missing_parameters

    def _get_s3_uri(self, workflow_run_id: str, path: str) -> str:
        folder_path = self.path or f"{workflow_run_id}"
        # Remove trailing slash from folder_path to avoid double slashes
        folder_path = folder_path.rstrip("/")
        # Remove any empty path segments to avoid double slashes
        folder_path = "/".join(segment for segment in folder_path.split("/") if segment)
        s3_suffix = f"{uuid.uuid4()}_{Path(path).name}"
        return f"s3://{self.s3_bucket}/{folder_path}/{s3_suffix}"

    def _get_azure_blob_name(self, workflow_run_id: str, file_path: str) -> str:
        blob_name = f"{uuid.uuid4()}_{Path(file_path).name}"
        folder_path = self.path or workflow_run_id
        # Remove trailing slash from folder_path to avoid double slashes
        folder_path = folder_path.rstrip("/")
        # Remove any empty path segments to avoid double slashes
        folder_path = "/".join(segment for segment in folder_path.split("/") if segment)
        return folder_path + "/" + blob_name

    @staticmethod
    def _validate_azure_storage_account_name(azure_storage_account_name: str) -> None:
        if re.fullmatch(r"[a-z0-9]{3,24}", azure_storage_account_name) is None:
            raise AzureConfigurationError("Azure Storage account name must match ^[a-z0-9]{3,24}$")

    def _get_azure_blob_uri(
        self,
        workflow_run_id: str,
        blob_name: str,
        azure_storage_account_name: str,
    ) -> str:
        self._validate_azure_storage_account_name(azure_storage_account_name)
        return (
            f"https://{azure_storage_account_name}.blob.core.windows.net/{self.azure_blob_container_name}/{blob_name}"
        )

    async def _resolve_validated_s3_endpoint(self) -> tuple[str | None, tuple[str, ...] | None]:
        """Normalize and SSRF-check a customer-supplied S3-compatible endpoint.

        Mirrors the SFTP/SMTP siblings: resolve the customer-supplied host, refuse private,
        loopback, link-local, and metadata targets, and return the validated addresses so the
        S3 client dials those instead of re-resolving a name that could rebind. The hostname
        stays the TLS and ``Host`` identity, so SigV4 signing is unaffected.

        Returns ``(None, None)`` when unset, which routes to AWS S3.
        """
        endpoint_url = _normalize_optional_endpoint_url(self.endpoint_url)
        if endpoint_url is None:
            return None, None

        parsed = urlparse(endpoint_url)
        host = parsed.hostname
        if settings.ALLOW_S3_ENDPOINT_INTERNAL_HOSTS:
            # Self-hosted deployments point at an object store on their own network, which is
            # commonly plaintext; the operator has accepted both risks by enabling this.
            if parsed.scheme not in ("http", "https"):
                raise ValueError("S3 endpoint_url must be an http:// or https:// URL")
            if not host:
                raise ValueError("S3 endpoint_url must include a hostname")
            return endpoint_url, None

        if parsed.scheme != "https":
            raise ValueError(
                "S3 endpoint_url must be an https:// URL: plaintext http would put the request "
                "signature on the wire in cleartext and is rejected by the upload proxy"
            )
        if not host:
            raise ValueError("S3 endpoint_url must include a hostname")

        try:
            resolved_ips = await asyncio.to_thread(resolve_fetch_host_ips, host)
        except UnresolvableHost:
            raise ValueError(f"S3 endpoint_url host could not be resolved: {host}") from None
        except BlockedHost:
            raise ValueError(
                f"S3 endpoint_url host resolves to a private or internal address, which is not allowed: {host}"
            ) from None
        return endpoint_url, resolved_ips

    def _build_s3_destination(
        self,
        workflow_run_id: str,
        file_path: str,
        aws_access_key_id: str | None,
        aws_secret_access_key: str | None,
        endpoint_url: str | None,
        endpoint_resolved_ips: tuple[str, ...] | None,
    ) -> FileUploadDestination:
        s3_uri = self._get_s3_uri(workflow_run_id, file_path)
        # ``_get_s3_uri`` returns ``s3://{bucket}/{key}`` — split it back out for
        # the destination so the cloud override can compute a presigned URL.
        without_scheme = s3_uri[len("s3://") :]
        bucket, _, key = without_scheme.partition("/")
        return FileUploadDestination(
            storage_type=FileStorageType.S3,
            customer_uri=s3_uri,
            sdk_uri=s3_uri,
            s3_bucket=bucket,
            s3_key=key,
            aws_access_key_id=aws_access_key_id,
            aws_secret_access_key=aws_secret_access_key,
            aws_region_name=self.region_name,
            endpoint_url=endpoint_url,
            endpoint_resolved_ips=endpoint_resolved_ips,
        )

    def _build_azure_destination(
        self,
        workflow_run_id: str,
        file_path: str,
        azure_storage_account_name: str,
        azure_storage_account_key: str,
    ) -> FileUploadDestination:
        blob_name = self._get_azure_blob_name(workflow_run_id, file_path)
        customer_uri = self._get_azure_blob_uri(workflow_run_id, blob_name, azure_storage_account_name)
        sdk_uri = f"azure://{self.azure_blob_container_name or ''}/{blob_name}"
        return FileUploadDestination(
            storage_type=FileStorageType.AZURE,
            customer_uri=customer_uri,
            sdk_uri=sdk_uri,
            azure_storage_account_name=azure_storage_account_name,
            azure_storage_account_key=azure_storage_account_key,
            azure_blob_container_name=self.azure_blob_container_name,
            azure_blob_name=blob_name,
        )

    @staticmethod
    def _build_google_drive_destination(
        *,
        access_token: str,
        folder_id: str | None,
    ) -> FileUploadDestination:
        uri = (
            f"https://drive.google.com/drive/folders/{folder_id}"
            if folder_id
            else "https://drive.google.com/drive/my-drive"
        )
        return FileUploadDestination(
            storage_type=FileStorageType.GOOGLE_DRIVE,
            customer_uri=uri,
            sdk_uri=uri,
            google_access_token=access_token,
            google_drive_folder_id=folder_id,
        )

    def _build_sftp_destination(
        self,
        *,
        file_path: str,
        host: str,
        port: int,
        username: str,
        password: str | None,
        private_key: str | None,
        private_key_passphrase: str | None,
        remote_path: str | None,
        host_key: str | None,
        uri_host: str,
        uri_remote_path: str | None,
    ) -> FileUploadDestination:
        filename = Path(file_path).name
        # The uri is recorded as the block's output and survives the run, so it is built
        # from the configured values. When a field is bound to a secret those are still
        # the placeholders, keeping the plaintext confined to the connection fields.
        uri_target = sftp_service.build_remote_target(uri_remote_path, filename)
        remote_uri = f"sftp://{uri_host}:{port}/{uri_target.lstrip('/')}"
        return FileUploadDestination(
            storage_type=FileStorageType.SFTP,
            customer_uri=remote_uri,
            sdk_uri=remote_uri,
            sftp_host=host,
            sftp_port=port,
            sftp_username=username,
            sftp_password=password,
            sftp_private_key=private_key,
            sftp_private_key_passphrase=private_key_passphrase,
            sftp_remote_path=remote_path,
            sftp_host_key=host_key,
        )

    async def _dispatch_files_to_storage(
        self,
        *,
        storage_type: FileStorageType,
        files_to_upload: list[str],
        workflow_run_id: str,
        workflow_run_block_id: str,
        organization_id: str | None,
        workflow_run_context: WorkflowRunContext,
        google_access_token: str | None = None,
    ) -> list[str]:
        uploaded_uris: list[str] = []
        if not files_to_upload:
            return uploaded_uris
        if storage_type == FileStorageType.S3:
            actual_aws_secret_access_key = await _resolve_sensitive_block_secret(
                workflow_run_context,
                self.aws_secret_access_key,
                "aws_secret_access_key",
            )
            actual_aws_access_key_id = (
                workflow_run_context.get_original_secret_value_or_none(self.aws_access_key_id) or self.aws_access_key_id
            )
            if (
                not isinstance(actual_aws_access_key_id, str)
                or not actual_aws_access_key_id.strip()
                or not isinstance(actual_aws_secret_access_key, str)
                or not actual_aws_secret_access_key.strip()
            ):
                raise ValueError("S3 is not configured: resolved AWS credentials are empty")
            endpoint_url, endpoint_resolved_ips = await self._resolve_validated_s3_endpoint()
            for file_path in files_to_upload:
                destination = self._build_s3_destination(
                    workflow_run_id=workflow_run_id,
                    file_path=file_path,
                    aws_access_key_id=actual_aws_access_key_id,
                    aws_secret_access_key=actual_aws_secret_access_key,
                    endpoint_url=endpoint_url,
                    endpoint_resolved_ips=endpoint_resolved_ips,
                )
                customer_uri = await app.AGENT_FUNCTION.upload_file_to_customer_storage(
                    file_path=file_path,
                    destination=destination,
                    organization_id=organization_id,
                    run_id=workflow_run_id,
                )
                uploaded_uris.append(customer_uri)
            LOG.info("Uploaded file(s) to S3 customer storage", file_path=self.path)
        elif storage_type == FileStorageType.AZURE:
            actual_azure_storage_account_key = await _resolve_sensitive_block_secret(
                workflow_run_context,
                self.azure_storage_account_key,
                "azure_storage_account_key",
            )
            resolved_azure_storage_account_name = workflow_run_context.get_original_secret_value_or_none(
                self.azure_storage_account_name
            )
            actual_azure_storage_account_name = (
                self.azure_storage_account_name
                if resolved_azure_storage_account_name is None
                else resolved_azure_storage_account_name
            )
            if (
                not isinstance(actual_azure_storage_account_name, str)
                or not actual_azure_storage_account_name.strip()
                or not isinstance(actual_azure_storage_account_key, str)
                or not actual_azure_storage_account_key.strip()
            ):
                raise AzureConfigurationError("Azure Storage is not configured")
            self._validate_azure_storage_account_name(actual_azure_storage_account_name)

            for file_path in files_to_upload:
                LOG.info("Uploading file to Azure Blob Storage customer storage", file_path=file_path)
                destination = self._build_azure_destination(
                    workflow_run_id=workflow_run_id,
                    file_path=file_path,
                    azure_storage_account_name=actual_azure_storage_account_name,
                    azure_storage_account_key=actual_azure_storage_account_key,
                )
                customer_uri = await app.AGENT_FUNCTION.upload_file_to_customer_storage(
                    file_path=file_path,
                    destination=destination,
                    organization_id=organization_id,
                    run_id=workflow_run_id,
                )
                uploaded_uris.append(customer_uri)
            LOG.info("Uploaded file(s) to Azure Blob Storage customer storage", file_path=self.path)
        elif storage_type == FileStorageType.GOOGLE_DRIVE:
            org_id = organization_id or workflow_run_context.organization_id
            if not org_id:
                raise ValueError("organization_id is required for Google Drive uploads")
            google_credential_id = (
                workflow_run_context.get_original_secret_value_or_none(self.google_credential_id)
                or self.google_credential_id
            )
            if not google_credential_id:
                raise ValueError("Google credential id is required")

            if google_access_token is None:
                google_credentials = await app.AGENT_FUNCTION.get_google_workspace_credentials(
                    organization_id=org_id,
                    credential_id=google_credential_id,
                    required_scopes=list(google_oauth_service.GOOGLE_DRIVE_SCOPES),
                )
                if not google_credentials or not google_credentials.token:
                    raise ValueError("Google Drive credential is not connected or is missing required scopes")
                google_access_token = google_credentials.token

            folder_id = google_drive_service.extract_folder_id(self.google_drive_folder_id)
            for file_path in files_to_upload:
                LOG.info("Uploading file to Google Drive customer storage", file_path=file_path)
                destination = self._build_google_drive_destination(
                    access_token=google_access_token,
                    folder_id=folder_id,
                )
                customer_uri = await app.AGENT_FUNCTION.upload_file_to_customer_storage(
                    file_path=file_path,
                    destination=destination,
                    organization_id=org_id,
                    run_id=workflow_run_id,
                )
                uploaded_uris.append(customer_uri)
            LOG.info("Uploaded file(s) to Google Drive customer storage", file_path=self.path)
        elif storage_type == FileStorageType.SFTP:
            actual_sftp_password = await _resolve_sensitive_block_secret(
                workflow_run_context,
                self.sftp_password,
                "sftp_password",
            )
            actual_sftp_private_key = await _resolve_sensitive_block_secret(
                workflow_run_context,
                self.sftp_private_key,
                "sftp_private_key",
            )
            actual_sftp_passphrase = await _resolve_sensitive_block_secret(
                workflow_run_context,
                self.sftp_private_key_passphrase,
                "sftp_private_key_passphrase",
            )
            actual_sftp_username = _resolve_block_secret_reference(workflow_run_context, self.sftp_username)
            actual_sftp_host = _resolve_block_secret_reference(workflow_run_context, self.sftp_host)
            actual_sftp_remote_path = _resolve_block_secret_reference(workflow_run_context, self.sftp_remote_path)
            actual_sftp_host_key = _resolve_block_secret_reference(workflow_run_context, self.sftp_host_key)
            if not actual_sftp_host or not actual_sftp_host.strip():
                raise ValueError("SFTP is not configured: resolved host is empty")
            if not actual_sftp_username or not actual_sftp_username.strip():
                raise ValueError("SFTP is not configured: resolved username is empty")
            sftp_port = 22 if self.sftp_port is None else self.sftp_port
            for file_path in files_to_upload:
                destination = self._build_sftp_destination(
                    file_path=file_path,
                    host=actual_sftp_host,
                    port=sftp_port,
                    username=actual_sftp_username,
                    password=actual_sftp_password,
                    private_key=actual_sftp_private_key,
                    private_key_passphrase=actual_sftp_passphrase,
                    remote_path=actual_sftp_remote_path,
                    host_key=actual_sftp_host_key,
                    uri_host=self.sftp_host or "",
                    uri_remote_path=self.sftp_remote_path,
                )
                customer_uri = await app.AGENT_FUNCTION.upload_file_to_customer_storage(
                    file_path=file_path,
                    destination=destination,
                    organization_id=organization_id,
                    run_id=workflow_run_id,
                )
                uploaded_uris.append(customer_uri)
            LOG.info("Uploaded file(s) to SFTP customer storage", file_path=self.path)
        else:
            raise ValueError(f"Unsupported storage type: {storage_type}")

        return uploaded_uris

    @staticmethod
    def _candidate_download_signal_run_ids(
        *,
        context: SkyvernContext | None,
        workflow_run_id: str,
        run_download_id: str | None,
    ) -> list[str]:
        candidate_run_ids: list[str] = []
        for candidate in (
            run_download_id,
            workflow_run_id,
            context.run_id if context else None,
            context.workflow_run_id if context else None,
        ):
            if candidate and candidate not in candidate_run_ids:
                candidate_run_ids.append(candidate)
        if not candidate_run_ids and context and context.task_id:
            candidate_run_ids.append(context.task_id)
        return candidate_run_ids

    def _get_files_to_upload_from_download_dir(
        self,
        *,
        download_files_path: str,
        max_file_count: int,
    ) -> list[str]:
        files_to_upload = []
        if os.path.isdir(download_files_path):
            files = os.listdir(download_files_path)
            if len(files) > max_file_count:
                raise ValueError(f"Too many files in the directory, not uploading. Max: {max_file_count}")
            for file in files:
                if os.path.isdir(os.path.join(download_files_path, file)):
                    LOG.warning("FileUploadBlock Skipping directory", file=file)
                    continue
                files_to_upload.append(os.path.join(download_files_path, file))
        return files_to_upload

    async def _select_files_to_upload_with_prompt(
        self,
        *,
        prompt: str,
        files_to_upload: list[str],
        workflow_run_block_id: str,
        organization_id: str | None,
    ) -> tuple[list[str], str]:
        candidate_paths_by_exact_name: dict[str, str] = {}
        candidate_paths_by_normalized_name: dict[str, str] = {}
        colliding_normalized_names: set[str] = set()
        candidate_file_names: list[str] = []
        for candidate_file_path in files_to_upload:
            candidate_name = Path(candidate_file_path).name
            candidate_paths_by_exact_name[candidate_name] = candidate_file_path
            candidate_file_names.append(candidate_name[:300])

            normalized_name = unicodedata.normalize("NFC", candidate_name)
            if normalized_name in colliding_normalized_names:
                continue
            existing_path = candidate_paths_by_normalized_name.get(normalized_name)
            if existing_path is not None and existing_path != candidate_file_path:
                # Distinct files that normalize to the same basename are only safe to resolve by exact spelling.
                colliding_normalized_names.add(normalized_name)
                candidate_paths_by_normalized_name.pop(normalized_name)
            else:
                candidate_paths_by_normalized_name[normalized_name] = candidate_file_path

        llm_prompt = prompt_engine.load_prompt(
            "file-upload-select-files",
            user_instructions=prompt,
            candidate_file_names=candidate_file_names,
        )
        llm_key = self.override_llm_key_for_organization(organization_id)
        llm_api_handler = LLMAPIHandlerFactory.get_override_llm_api_handler(
            llm_key, default=get_org_aware_primary_llm_api_handler()
        )

        workflow_run_block = None
        try:
            workflow_run_block = await app.DATABASE.observer.get_workflow_run_block(
                workflow_run_block_id, organization_id
            )
        except Exception as e:
            LOG.error(
                "Failed to fetch workflow_run_block for FileUploadBlock selection artifacts",
                block_label=self.label,
                workflow_run_block_id=workflow_run_block_id,
                error=e,
            )

        llm_response = await llm_api_handler(
            prompt=llm_prompt,
            prompt_name="file-upload-select-files",
            system_prompt=self.workflow_system_prompt,
            workflow_run_block_id=workflow_run_block_id,
            organization_id=organization_id,
            force_dict=False,
        )

        if workflow_run_block:
            try:
                await app.ARTIFACT_MANAGER.create_workflow_run_block_artifacts(
                    workflow_run_block=workflow_run_block,
                    artifacts=[
                        (ArtifactType.LLM_PROMPT, llm_prompt.encode("utf-8")),
                        (ArtifactType.LLM_RESPONSE, json.dumps(llm_response).encode("utf-8")),
                    ],
                )
            except Exception as e:
                LOG.error(
                    "Failed to save FileUploadBlock selection artifacts",
                    block_label=self.label,
                    workflow_run_block_id=workflow_run_block_id,
                    error=e,
                )

        if not isinstance(llm_response, dict):
            raise ValueError("File upload selection LLM response must be a JSON object")
        reasoning = llm_response.get("reasoning")
        if not isinstance(reasoning, str):
            raise ValueError("File upload selection LLM response reasoning must be a string")

        selected_names = llm_response.get("files_to_upload")
        if not isinstance(selected_names, list) or not all(isinstance(name, str) for name in selected_names):
            raise ValueError("File upload selection LLM response files_to_upload must be a list of strings")

        selected_paths: list[str] = []
        seen_paths: set[str] = set()
        unmatched_names: list[str] = []
        for selected_name in selected_names:
            resolved_candidate_path = candidate_paths_by_exact_name.get(selected_name)
            if resolved_candidate_path is None:
                normalized_name = unicodedata.normalize("NFC", selected_name)
                if normalized_name not in colliding_normalized_names:
                    resolved_candidate_path = candidate_paths_by_normalized_name.get(normalized_name)
            if resolved_candidate_path is None:
                unmatched_names.append(selected_name)
                continue
            if resolved_candidate_path in seen_paths:
                continue
            seen_paths.add(resolved_candidate_path)
            selected_paths.append(resolved_candidate_path)

        # Any selected name outside the candidate list means the response cannot be trusted; fail closed.
        if unmatched_names:
            LOG.warning(
                "FileUploadBlock prompt selected names that are not candidates",
                block_label=self.label,
                workflow_run_block_id=workflow_run_block_id,
                unmatched_name_count=len(unmatched_names),
                unmatched_names=[name[:100] for name in unmatched_names[:3]],
            )
            raise ValueError(
                f"File upload selection returned {len(unmatched_names)} name(s) that are not candidate files"
            )

        return selected_paths, reasoning

    def _get_files_in_alternate_candidate_download_dirs(
        self,
        *,
        context: SkyvernContext | None,
        workflow_run_id: str,
        run_download_id: str | None,
        download_files_path: str,
        max_file_count: int,
    ) -> tuple[list[str] | None, str]:
        """Return alternate local files plus a failure-reason count label.

        None means the local evidence could not be trusted, so execute() fails closed rather than no-oping.
        """
        for candidate_run_id in self._candidate_download_signal_run_ids(
            context=context,
            workflow_run_id=workflow_run_id,
            run_download_id=run_download_id,
        ):
            candidate_download_files_path = str(get_path_for_workflow_download_directory(candidate_run_id).absolute())
            if candidate_download_files_path == download_files_path:
                continue
            try:
                alternate_files = self._get_files_to_upload_from_download_dir(
                    download_files_path=candidate_download_files_path,
                    max_file_count=max_file_count,
                )
            except ValueError:
                LOG.warning(
                    "FileUploadBlock found too many files in an alternate candidate download directory",
                    workflow_run_id=workflow_run_id,
                    candidate_run_id=candidate_run_id,
                    download_files_path=download_files_path,
                    candidate_download_files_path=candidate_download_files_path,
                    exc_info=True,
                )
                return None, "too_many"
            if alternate_files:
                LOG.warning(
                    "FileUploadBlock found files in an alternate candidate download directory",
                    workflow_run_id=workflow_run_id,
                    candidate_run_id=candidate_run_id,
                    download_files_path=download_files_path,
                    candidate_download_files_path=candidate_download_files_path,
                    file_count=len(alternate_files),
                )
                return alternate_files, str(len(alternate_files))
        return [], "0"

    async def _get_browser_session_downloaded_files_for_empty_scan(
        self,
        *,
        organization_id: str | None,
        workflow_run_id: str,
        workflow_run_block_id: str,
        browser_session_id: str | None,
    ) -> list[str] | None:
        if not browser_session_id:
            # No persistent-session namespace exists to inspect, so this signal cannot contain downloads.
            return []
        if not organization_id:
            LOG.warning(
                "FileUploadBlock cannot check browser-session downloads without organization_id",
                workflow_run_id=workflow_run_id,
                workflow_run_block_id=workflow_run_block_id,
                browser_session_id=browser_session_id,
            )
            return None

        try:
            async with asyncio.timeout(GET_DOWNLOADED_FILES_TIMEOUT):
                return await app.STORAGE.list_downloaded_files_in_browser_session(
                    organization_id=organization_id,
                    browser_session_id=browser_session_id,
                )
        except asyncio.TimeoutError:
            LOG.warning(
                "Timeout checking browser-session downloads for empty FileUploadBlock scan",
                workflow_run_id=workflow_run_id,
                workflow_run_block_id=workflow_run_block_id,
                browser_session_id=browser_session_id,
            )
            return None
        except Exception:
            LOG.warning(
                "Failed to check browser-session downloads for empty FileUploadBlock scan",
                workflow_run_id=workflow_run_id,
                workflow_run_block_id=workflow_run_block_id,
                browser_session_id=browser_session_id,
                exc_info=True,
            )
            return None

    async def _get_registered_downloaded_files_for_empty_scan(
        self,
        *,
        organization_id: str | None,
        workflow_run_id: str,
        workflow_run_block_id: str,
        run_download_id: str | None,
        context: SkyvernContext | None,
    ) -> list[FileInfo] | None:
        """Return registered downloads, or None when the signal is unknown.

        A timeout on any candidate stays unknown even if later candidates might be empty; an empty later lookup cannot
        prove that the timed-out candidate had no downloads, so the caller fails closed.
        """
        if not organization_id:
            LOG.warning(
                "FileUploadBlock cannot check registered downloads without organization_id",
                workflow_run_id=workflow_run_id,
                workflow_run_block_id=workflow_run_block_id,
            )
            return None

        registered_downloaded_files: list[FileInfo] = []
        for candidate_run_id in self._candidate_download_signal_run_ids(
            context=context,
            workflow_run_id=workflow_run_id,
            run_download_id=run_download_id,
        ):
            try:
                async with asyncio.timeout(GET_DOWNLOADED_FILES_TIMEOUT):
                    registered_downloaded_files.extend(
                        await app.STORAGE.get_downloaded_files(
                            organization_id=organization_id,
                            run_id=candidate_run_id,
                        )
                    )
                    if registered_downloaded_files:
                        return registered_downloaded_files
            except asyncio.TimeoutError:
                LOG.warning(
                    "Timeout checking registered downloads for empty FileUploadBlock scan",
                    workflow_run_id=workflow_run_id,
                    workflow_run_block_id=workflow_run_block_id,
                    candidate_run_id=candidate_run_id,
                )
                return None
            except Exception:
                LOG.warning(
                    "Failed to check registered downloads for empty FileUploadBlock scan",
                    workflow_run_id=workflow_run_id,
                    workflow_run_block_id=workflow_run_block_id,
                    candidate_run_id=candidate_run_id,
                    exc_info=True,
                )
                return None

        return registered_downloaded_files


class FileUploadBlock(FileDestinationBlock):
    # There is a mypy bug with Literal. Without the type: ignore, mypy will raise an error:
    # Parameter 1 of Literal[...] cannot be of type "Any"
    block_type: Literal[BlockType.FILE_UPLOAD] = BlockType.FILE_UPLOAD  # type: ignore

    storage_type: FileStorageType = FileStorageType.S3

    def get_all_parameters(
        self,
        workflow_run_id: str,
    ) -> list[PARAMETER_TYPE]:
        return self._get_destination_parameters(self.get_workflow_run_context(workflow_run_id))

    def format_potential_template_parameters(self, workflow_run_context: WorkflowRunContext) -> None:
        self._format_destination_template_parameters(workflow_run_context)
        self._apply_workflow_system_prompt(workflow_run_context)

    async def execute(
        self,
        workflow_run_id: str,
        workflow_run_block_id: str,
        organization_id: str | None = None,
        browser_session_id: str | None = None,
        **kwargs: dict,
    ) -> BlockResult:
        # get workflow run context
        workflow_run_context = self.get_workflow_run_context(workflow_run_id)
        # get all parameters into a dictionary
        # data validate before uploading
        try:
            missing_parameters = self._validate_destination_fields(self.storage_type)
        except UnsupportedStorageTypeError:
            return await self.build_block_result(
                success=False,
                failure_reason=f"Unsupported storage type: {self.storage_type}",
                output_parameter_value=None,
                status=BlockStatus.failed,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )

        if missing_parameters:
            return await self.build_block_result(
                success=False,
                failure_reason=f"Required block values are missing in the FileUploadBlock (label: {self.label}): {', '.join(missing_parameters)}",
                output_parameter_value=None,
                status=BlockStatus.failed,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )

        try:
            self.format_potential_template_parameters(workflow_run_context)
        except Exception as e:
            return await self._template_format_failure_result(
                e,
                f"Failed to format jinja template: {str(e)}",
                workflow_run_context,
                workflow_run_id,
                workflow_run_block_id,
                organization_id,
            )

        context = skyvern_context.current()
        run_download_id = resolve_run_download_id(context, fallback_run_id=workflow_run_id)
        download_files_path = str(get_path_for_workflow_download_directory(run_download_id).absolute())

        uploaded_uris: list[str] = []
        try:
            workflow_run_context = self.get_workflow_run_context(workflow_run_id)
            files_to_upload = []
            max_file_count = (
                MAX_UPLOAD_FILE_COUNT
                if self.storage_type in {FileStorageType.S3, FileStorageType.GOOGLE_DRIVE, FileStorageType.SFTP}
                else AZURE_BLOB_STORAGE_MAX_UPLOAD_FILE_COUNT
            )
            files_to_upload = self._get_files_to_upload_from_download_dir(
                download_files_path=download_files_path,
                max_file_count=max_file_count,
            )

            if not files_to_upload and not self.continue_on_empty:
                (
                    (alternate_files, alternate_file_count),
                    browser_session_downloaded_files,
                    registered_downloaded_files,
                ) = await asyncio.gather(
                    asyncio.to_thread(
                        self._get_files_in_alternate_candidate_download_dirs,
                        context=context,
                        workflow_run_id=workflow_run_id,
                        run_download_id=run_download_id,
                        download_files_path=download_files_path,
                        max_file_count=max_file_count,
                    ),
                    self._get_browser_session_downloaded_files_for_empty_scan(
                        organization_id=organization_id or workflow_run_context.organization_id,
                        workflow_run_id=workflow_run_id,
                        workflow_run_block_id=workflow_run_block_id,
                        browser_session_id=browser_session_id or (context.browser_session_id if context else None),
                    ),
                    self._get_registered_downloaded_files_for_empty_scan(
                        organization_id=organization_id or workflow_run_context.organization_id,
                        workflow_run_id=workflow_run_id,
                        workflow_run_block_id=workflow_run_block_id,
                        run_download_id=run_download_id,
                        context=context,
                    ),
                )
                if (
                    registered_downloaded_files == []
                    and alternate_files == []
                    and browser_session_downloaded_files == []
                ):
                    LOG.info(
                        "FileUploadBlock empty scan has no registered downloads; treating as no-op",
                        workflow_run_id=workflow_run_id,
                        workflow_run_block_id=workflow_run_block_id,
                        download_files_path=download_files_path,
                        storage_type=self.storage_type,
                    )
                    await self.record_output_parameter_value(workflow_run_context, workflow_run_id, uploaded_uris)
                    return await self.build_block_result(
                        success=True,
                        failure_reason=None,
                        output_parameter_value=uploaded_uris,
                        status=BlockStatus.completed,
                        workflow_run_block_id=workflow_run_block_id,
                        organization_id=organization_id,
                    )

                registered_download_count = (
                    str(len(registered_downloaded_files)) if registered_downloaded_files is not None else "unknown"
                )
                browser_session_download_count = (
                    str(len(browser_session_downloaded_files))
                    if browser_session_downloaded_files is not None
                    else "unknown"
                )
                return await self.build_block_result(
                    success=False,
                    failure_reason=(
                        f"No files found to upload in the run download directory ({download_files_path}); "
                        f"registered_download_count={registered_download_count}; "
                        f"alternate_file_count={alternate_file_count}; "
                        f"browser_session_download_count={browser_session_download_count}; "
                        f"nothing was sent to {self.storage_type}."
                    ),
                    output_parameter_value=None,
                    status=BlockStatus.failed,
                    workflow_run_block_id=workflow_run_block_id,
                    organization_id=organization_id,
                )

            if files_to_upload and self.prompt and self.prompt.strip():
                candidate_count = len(files_to_upload)
                files_to_upload, selection_reasoning = await self._select_files_to_upload_with_prompt(
                    prompt=self.prompt,
                    files_to_upload=files_to_upload,
                    workflow_run_block_id=workflow_run_block_id,
                    organization_id=organization_id,
                )
                selected_count = len(files_to_upload)
                LOG.info(
                    "FileUploadBlock prompt selection completed",
                    block_label=self.label,
                    candidate_count=candidate_count,
                    selected_count=selected_count,
                )

                if not files_to_upload:
                    LOG.warning(
                        "FileUploadBlock prompt selected no files; treating as no-op",
                        block_label=self.label,
                        workflow_run_id=workflow_run_id,
                        workflow_run_block_id=workflow_run_block_id,
                        candidate_count=candidate_count,
                        selected_count=selected_count,
                        reasoning=selection_reasoning,
                    )
                    await self.record_output_parameter_value(workflow_run_context, workflow_run_id, uploaded_uris)
                    return await self.build_block_result(
                        success=True,
                        failure_reason=None,
                        output_parameter_value=uploaded_uris,
                        status=BlockStatus.completed,
                        workflow_run_block_id=workflow_run_block_id,
                        organization_id=organization_id,
                    )

            uploaded_uris = await self._dispatch_files_to_storage(
                storage_type=self.storage_type,
                files_to_upload=files_to_upload,
                workflow_run_id=workflow_run_id,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
                workflow_run_context=workflow_run_context,
            )

        except Exception as e:
            LOG.exception("FileUploadBlock Failed to upload file", file_path=self.path, storage_type=self.storage_type)
            return await self.build_block_result(
                success=False,
                failure_reason=f"Failed to upload file to {self.storage_type}: {str(e)}",
                output_parameter_value=None,
                status=BlockStatus.failed,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )

        await self.record_output_parameter_value(workflow_run_context, workflow_run_id, uploaded_uris)
        return await self.build_block_result(
            success=True,
            failure_reason=None,
            output_parameter_value=uploaded_uris,
            status=BlockStatus.completed,
            workflow_run_block_id=workflow_run_block_id,
            organization_id=organization_id,
        )


# Custom SMTP: port 465 is implicit TLS (SMTPS); every other port connects in plaintext and
# upgrades via STARTTLS before authenticating, so credentials are never sent in the clear.
_SMTP_IMPLICIT_TLS_PORT = 465
_CUSTOM_SMTP_DEFAULT_PORT = 587
_CUSTOM_SMTP_TIMEOUT_SECONDS = 30


class _HostnamePinnedSMTPSSL(smtplib.SMTP_SSL):
    """Implicit-TLS client that dials a pre-resolved IP while TLS-verifying the configured hostname.

    SSRF pinning fixes the TCP target to the address that passed validation; the certificate
    check stays bound to the user-configured name so a legitimate server still verifies.
    """

    def __init__(
        self, connect_host: str, port: int, *, server_hostname: str, timeout: float, context: ssl.SSLContext
    ) -> None:
        self._tls_server_hostname = server_hostname
        super().__init__(connect_host, port, timeout=timeout, context=context)

    def _get_socket(self, host: str, port: int, timeout: float | None) -> ssl.SSLSocket:  # type: ignore[override]
        new_socket = socket.create_connection((host, port), timeout, self.source_address)
        return self.context.wrap_socket(new_socket, server_hostname=self._tls_server_hostname)


def _custom_smtp_connect_failure_reason(error: Exception) -> str:
    # Connect-stage errors carry no credentials; include the OS/server detail (bounded) so
    # "Connection refused" vs a DNS failure is visible in the block's failure reason.
    detail = str(error).strip()
    reason = f"{type(error).__name__}: {detail}" if detail else type(error).__name__
    return reason[:200]


def _connect_custom_smtp_client(
    host: str, port: int, connect_hosts: tuple[str, ...], tls_context: ssl.SSLContext
) -> smtplib.SMTP:
    """Dial the validated addresses in order until one accepts the connection and TLS setup.

    Pinning to a resolved IP disables socket.create_connection's own multi-address
    fallback, so a dual-stack or pooled server with an unreachable or TLS-broken
    member must be retried here. STARTTLS runs per address so the next member is
    tried when one accepts TCP but fails the TLS upgrade. The last failure's
    detail is surfaced.
    """
    last_error: Exception | None = None
    for connect_host in connect_hosts:
        smtp_client: smtplib.SMTP | None = None
        try:
            if port == _SMTP_IMPLICIT_TLS_PORT:
                return _HostnamePinnedSMTPSSL(
                    connect_host, port, server_hostname=host, timeout=_CUSTOM_SMTP_TIMEOUT_SECONDS, context=tls_context
                )
            smtp_client = smtplib.SMTP(connect_host, port, timeout=_CUSTOM_SMTP_TIMEOUT_SECONDS)
            # smtplib uses `_host` as the TLS server_hostname in starttls(); keep certificate
            # verification bound to the configured hostname, not the pinned address.
            smtp_client._host = host  # type: ignore[attr-defined]
            smtp_client.starttls(context=tls_context)
            return smtp_client
        except (OSError, smtplib.SMTPException) as e:
            last_error = e
            if smtp_client is not None:
                try:
                    smtp_client.close()
                except Exception:
                    LOG.warning("SendEmailBlock Failed to close custom SMTP connection", exc_info=True)
    assert last_error is not None  # connect_hosts is never empty
    if isinstance(last_error, smtplib.SMTPNotSupportedError):
        raise CustomSMTPConnectionFailed(
            host=host,
            port=port,
            reason="the server does not support STARTTLS on this port; use port 465 for implicit TLS",
        ) from last_error
    raise CustomSMTPConnectionFailed(
        host=host, port=port, reason=_custom_smtp_connect_failure_reason(last_error)
    ) from last_error


def _send_via_custom_smtp(
    *,
    host: str,
    port: int,
    connect_hosts: tuple[str, ...],
    username: str | None,
    password: str | None,
    message: EmailMessage,
) -> None:
    """Connect, secure, authenticate, send, and close against a user-provided SMTP server.

    Blocking by design: the caller runs it in a worker thread (asyncio.to_thread) so a slow or
    stalling server cannot occupy the event loop and starve the workflow activity heartbeat.
    `connect_hosts` are the SSRF-validated addresses to dial; `host` stays the TLS identity.
    Raises exceptions with user-actionable messages that never include the password.
    """
    tls_context = ssl.create_default_context()
    smtp_client = _connect_custom_smtp_client(host, port, connect_hosts, tls_context)
    LOG.info("SendEmailBlock Connected to custom SMTP server", smtp_host=host, smtp_port=port)

    try:
        if username and password:
            try:
                smtp_client.login(username, password)
            except smtplib.SMTPAuthenticationError:
                # Deliberately drop the cause: the server's rejection text is
                # server-controlled and could echo the submitted credential into logs.
                raise CustomSMTPAuthenticationFailed(username=username) from None
            LOG.info("SendEmailBlock Logged in to custom SMTP server", smtp_host=host, smtp_port=port)
        smtp_client.send_message(message)
        LOG.info("SendEmailBlock Email sent via custom SMTP server", smtp_host=host, smtp_port=port)
    finally:
        # A user-provided server may drop the connection right after accepting the message;
        # a failing QUIT must not override the outcome (marking a delivered email as failed
        # invites duplicate sends). On setup errors this same path tears the socket down.
        try:
            smtp_client.quit()
        except (OSError, smtplib.SMTPException):
            try:
                smtp_client.close()
            except Exception:
                LOG.warning("SendEmailBlock Failed to close custom SMTP connection", exc_info=True)


class SendEmailBlock(Block):
    # There is a mypy bug with Literal. Without the type: ignore, mypy will raise an error:
    # Parameter 1 of Literal[...] cannot be of type "Any"
    block_type: Literal[BlockType.SEND_EMAIL] = BlockType.SEND_EMAIL  # type: ignore

    smtp_host: AWSSecretParameter
    smtp_port: AWSSecretParameter
    smtp_username: AWSSecretParameter
    # if you're using a Gmail account, you need to pass in an app password instead of your regular password
    smtp_password: AWSSecretParameter
    sender: str
    recipients: list[str]
    subject: str
    body: str
    file_attachments: list[str] = []
    # Optional custom SMTP settings. When custom_smtp_host is set, the block sends through
    # this server instead of the platform default sender (the smtp_* secret parameters above).
    custom_smtp_host: str | None = None
    custom_smtp_port: int | None = Field(default=None, ge=1, le=65535)
    custom_smtp_username: str | None = None
    # Encrypted at rest in the workflow definition (see secret_encryption.py); may also
    # reference a workflow secret parameter.
    custom_smtp_password: str | None = None

    TEMPLATABLE_FIELDS: ClassVar[frozenset[str]] = frozenset(
        {
            "body",
            "custom_smtp_host",
            "custom_smtp_username",
            "file_attachments",
            "recipients",
            "sender",
            "subject",
        }
    )

    def get_all_parameters(
        self,
        workflow_run_id: str,
    ) -> list[PARAMETER_TYPE]:
        workflow_run_context = self.get_workflow_run_context(workflow_run_id)
        parameters: list[PARAMETER_TYPE] = [
            parameter
            for parameter in (self.smtp_host, self.smtp_port, self.smtp_username, self.smtp_password)
            # Registering a parameter the context never resolved aborts the whole run before this
            # block can report which SMTP settings are missing — placeholders are never declared,
            # and platform secrets go unresolved wherever they are not configured.
            if parameter.aws_key != UNUSED_CUSTOM_SMTP_PLACEHOLDER_AWS_KEY
            and workflow_run_context.has_parameter(parameter.key)
        ]

        if self.file_attachments:
            for file_path in self.file_attachments:
                if workflow_run_context.has_parameter(file_path):
                    parameters.append(workflow_run_context.get_parameter(file_path))

        if self.subject and workflow_run_context.has_parameter(self.subject):
            parameters.append(workflow_run_context.get_parameter(self.subject))

        if self.body and workflow_run_context.has_parameter(self.body):
            parameters.append(workflow_run_context.get_parameter(self.body))

        for custom_smtp_value in (self.custom_smtp_host, self.custom_smtp_username, self.custom_smtp_password):
            if custom_smtp_value and workflow_run_context.has_parameter(custom_smtp_value):
                parameters.append(workflow_run_context.get_parameter(custom_smtp_value))

        return parameters

    def format_potential_template_parameters(self, workflow_run_context: WorkflowRunContext) -> None:
        self.sender = self.render_templatable_field("sender", self.sender, workflow_run_context)
        self.subject = self.render_templatable_field("subject", self.subject, workflow_run_context)
        self.body = self.render_templatable_field("body", self.body, workflow_run_context)

        # Format recipients
        formatted_recipients = []
        for recipient in self.recipients:
            formatted_recipient = self.render_templatable_field("recipients", recipient, workflow_run_context)
            formatted_recipients.append(formatted_recipient)
        self.recipients = formatted_recipients

        if self.custom_smtp_host:
            self.custom_smtp_host = self.render_templatable_field(
                "custom_smtp_host", self.custom_smtp_host, workflow_run_context
            )
        if self.custom_smtp_username:
            self.custom_smtp_username = self.render_templatable_field(
                "custom_smtp_username", self.custom_smtp_username, workflow_run_context
            )
        # Only a full "{{ param }}" reference is a template; a literal password that merely
        # contains Jinja-looking characters must never be rendered (it would corrupt the
        # password and could leak it through template-error messages).
        if self.custom_smtp_password and is_full_template_reference(self.custom_smtp_password):
            self.custom_smtp_password = self.format_block_parameter_template_from_workflow_run_context(
                self.custom_smtp_password, workflow_run_context
            )

    def _decrypt_smtp_parameters(self, workflow_run_context: WorkflowRunContext) -> tuple[str, int, str, str]:
        obfuscated_smtp_host_value = workflow_run_context.get_value_or_none(self.smtp_host.key)
        obfuscated_smtp_port_value = workflow_run_context.get_value_or_none(self.smtp_port.key)
        obfuscated_smtp_username_value = workflow_run_context.get_value_or_none(self.smtp_username.key)
        obfuscated_smtp_password_value = workflow_run_context.get_value_or_none(self.smtp_password.key)
        smtp_host_value = workflow_run_context.get_original_secret_value_or_none(obfuscated_smtp_host_value)
        smtp_port_value = workflow_run_context.get_original_secret_value_or_none(obfuscated_smtp_port_value)
        smtp_username_value = workflow_run_context.get_original_secret_value_or_none(obfuscated_smtp_username_value)
        smtp_password_value = workflow_run_context.get_original_secret_value_or_none(obfuscated_smtp_password_value)

        email_config_problems = []
        if smtp_host_value is None:
            email_config_problems.append("Missing SMTP server")
        if smtp_port_value is None:
            email_config_problems.append("Missing SMTP port")
        elif not smtp_port_value.isdigit():
            email_config_problems.append("SMTP port should be a number")
        if smtp_username_value is None:
            email_config_problems.append("Missing SMTP username")
        if smtp_password_value is None:
            email_config_problems.append("Missing SMTP password")

        if email_config_problems:
            raise InvalidEmailClientConfiguration(email_config_problems)

        return (
            smtp_host_value,
            smtp_port_value,
            smtp_username_value,
            smtp_password_value,
        )

    def has_custom_smtp(self) -> bool:
        return bool(self.custom_smtp_host and self.custom_smtp_host.strip())

    async def _resolve_custom_smtp_parameters(
        self, workflow_run_context: WorkflowRunContext
    ) -> tuple[str, int, str | None, str | None]:
        host = (
            workflow_run_context.get_original_secret_value_or_none(self.custom_smtp_host) or self.custom_smtp_host or ""
        ).strip()
        port = self.custom_smtp_port or _CUSTOM_SMTP_DEFAULT_PORT
        username = workflow_run_context.get_original_secret_value_or_none(self.custom_smtp_username) or (
            self.custom_smtp_username or None
        )
        password = await _resolve_sensitive_block_secret(
            workflow_run_context,
            self.custom_smtp_password,
            "custom_smtp_password",
        )

        email_config_problems = []
        if not host:
            email_config_problems.append("Missing custom SMTP host")
        if username and not password:
            email_config_problems.append("Missing custom SMTP password (a custom SMTP username is set)")
        if password and not username:
            email_config_problems.append("Missing custom SMTP username (a custom SMTP password is set)")
        if email_config_problems:
            raise InvalidEmailClientConfiguration(email_config_problems)

        return host, port, username, password

    @staticmethod
    async def _resolve_custom_smtp_connect_hosts(host: str, port: int) -> tuple[str, ...]:
        """SSRF guard mirroring the SFTP block: validate the hostname resolves to public
        addresses and return the validated IPs to dial, so a second DNS lookup cannot rebind
        the name to an internal target. Every address is kept (a dual-stack or pooled server
        may have unreachable members); TLS verification stays bound to the hostname."""
        if settings.ALLOW_SMTP_INTERNAL_HOSTS:
            return (host,)
        try:
            return await asyncio.to_thread(resolve_fetch_host_ips, host)
        except UnresolvableHost:
            raise CustomSMTPConnectionFailed(
                host=host,
                port=port,
                reason="the hostname could not be resolved; check the SMTP host",
            ) from None
        except BlockedHost:
            raise CustomSMTPConnectionFailed(
                host=host,
                port=port,
                reason="the hostname resolves to a private or internal address, which is not allowed",
            ) from None

    def _get_file_paths(self, workflow_run_context: WorkflowRunContext, workflow_run_id: str) -> list[str]:
        file_paths = []
        context = skyvern_context.current()
        run_id = context.run_id if context and context.run_id else workflow_run_id
        for path in self.file_attachments:
            # if the file path is a parameter, get the value from the workflow run context first
            if workflow_run_context.has_parameter(path):
                file_path_parameter_value = workflow_run_context.get_value(path)
                # if the file path is a secret, get the original secret value from the workflow run context
                file_path_parameter_secret_value = workflow_run_context.get_original_secret_value_or_none(
                    file_path_parameter_value
                )
                if file_path_parameter_secret_value:
                    path = file_path_parameter_secret_value
                else:
                    path = file_path_parameter_value

            if path == settings.WORKFLOW_DOWNLOAD_DIRECTORY_PARAMETER_KEY:
                # if the path is WORKFLOW_DOWNLOAD_DIRECTORY_PARAMETER_KEY, use download directory for the workflow run
                path = str(get_path_for_workflow_download_directory(run_id).absolute())
                LOG.info(
                    "SendEmailBlock Using download directory for the workflow run",
                    workflow_run_id=workflow_run_id,
                    file_path=path,
                )

            path = self.render_templatable_field("file_attachments", path, workflow_run_context)
            if not is_remote_url(path):
                path = validate_local_file_path(path, run_id)
            # if the file path is a directory, add all files in the directory, skip directories, limit to 10 files
            if os.path.exists(path):
                if os.path.isdir(path):
                    for file in os.listdir(path):
                        if os.path.isdir(os.path.join(path, file)):
                            LOG.warning("SendEmailBlock Skipping directory", file=file)
                            continue
                        file_path = os.path.join(path, file)
                        file_paths.append(file_path)
                else:
                    # covers the case where the file path is a single file
                    file_paths.append(path)
            elif is_remote_url(path):
                file_paths.append(path)
            else:
                LOG.warning("SendEmailBlock File not found", file_path=path)

        return file_paths

    def get_real_email_recipients(self, workflow_run_context: WorkflowRunContext) -> list[str]:
        recipients = []
        for recipient in self.recipients:
            # Check if the recipient is a parameter and get its value
            if workflow_run_context.has_parameter(recipient):
                maybe_recipient = workflow_run_context.get_value(recipient)
            else:
                maybe_recipient = recipient

            recipient = self.render_templatable_field("recipients", recipient, workflow_run_context)
            # check if maybe_recipient is a valid email address
            try:
                validate_email(maybe_recipient)
                recipients.append(maybe_recipient)
            except EmailNotValidError as e:
                LOG.warning(
                    "SendEmailBlock Invalid email address",
                    recipient=maybe_recipient,
                    reason=str(e),
                )

        if not recipients:
            raise NoValidEmailRecipient(recipients=recipients)

        return recipients

    async def _build_email_message(
        self,
        workflow_run_context: WorkflowRunContext,
        workflow_run_id: str,
        organization_id: str | None = None,
    ) -> EmailMessage:
        msg = EmailMessage()
        msg["Subject"] = (
            self.subject.strip().replace("\n", "").replace("\r", "") + f" - Workflow Run ID: {workflow_run_id}"
        )
        msg["To"] = ", ".join(self.get_real_email_recipients(workflow_run_context))
        msg["BCC"] = self.sender  # BCC the sender so there is a record of the email being sent
        msg["From"] = self.sender
        if self.body and workflow_run_context.has_parameter(self.body) and workflow_run_context.has_value(self.body):
            # We're purposely not decrypting the body parameter value here because we don't want to expose secrets
            body_parameter_value = workflow_run_context.get_value(self.body)
            msg.set_content(str(body_parameter_value))
        else:
            msg.set_content(self.body)

        file_names_by_hash: dict[str, list[str]] = defaultdict(list)

        for filename in self._get_file_paths(workflow_run_context, workflow_run_id):
            if filename.startswith(("s3://", "gs://", "azure://", "http://", "https://")):
                path = await download_file(filename, organization_id=organization_id)
            else:
                LOG.info("SendEmailBlock Looking for file locally", filename=filename)
                if not os.path.exists(filename):
                    raise FileNotFoundError(f"File not found: {filename}")
                if not os.path.isfile(filename):
                    raise IsADirectoryError(f"Path is a directory: {filename}")

                path = filename
                LOG.info("SendEmailBlock Found file locally", path=path)

            if not path:
                raise FileNotFoundError(f"File not found: {filename}")

            # Guess the content type based on the file's extension.  Encoding
            # will be ignored, although we should check for simple things like
            # gzip'd or compressed files.
            kind = filetype.guess(path)
            if kind:
                ctype = kind.mime
                extension = kind.extension
            else:
                # No guess could be made, or the file is encoded (compressed), so
                # use a generic bag-of-bits type.
                ctype = "application/octet-stream"
                extension = None

            maintype, subtype = ctype.split("/", 1)
            attachment_path = Path(path)
            attachment_filename = attachment_path.name

            # Check if the filename has an extension
            if not attachment_path.suffix:
                # If no extension, guess it based on the MIME type
                if extension:
                    attachment_filename += f".{extension}"

            LOG.info(
                "SendEmailBlock Adding attachment",
                filename=attachment_filename,
                maintype=maintype,
                subtype=subtype,
            )
            with open(path, "rb") as fp:
                msg.add_attachment(
                    fp.read(),
                    maintype=maintype,
                    subtype=subtype,
                    filename=attachment_filename,
                )
                file_hash = calculate_sha256_for_file(path)
                file_names_by_hash[file_hash].append(path)

        # Calculate file stats based on content hashes
        total_files = sum(len(files) for files in file_names_by_hash.values())
        unique_files = len(file_names_by_hash)
        duplicate_files_list = [files for files in file_names_by_hash.values() if len(files) > 1]

        # Log file statistics
        LOG.info("SendEmailBlock Total files attached", total_files=total_files)
        LOG.info("SendEmailBlock Unique files (based on content) attached", unique_files=unique_files)
        if duplicate_files_list:
            LOG.info(
                "SendEmailBlock Duplicate files (based on content) attached", duplicate_files_list=duplicate_files_list
            )

        return msg

    async def execute(
        self,
        workflow_run_id: str,
        workflow_run_block_id: str,
        organization_id: str | None = None,
        browser_session_id: str | None = None,
        **kwargs: dict,
    ) -> BlockResult:
        workflow_run_context = self.get_workflow_run_context(workflow_run_id)
        await app.DATABASE.observer.update_workflow_run_block(
            workflow_run_block_id=workflow_run_block_id,
            organization_id=organization_id,
            recipients=self.recipients,
            attachments=self.file_attachments,
            subject=self.subject,
            body=self.body,
        )
        try:
            self.format_potential_template_parameters(workflow_run_context)
        except Exception as e:
            return await self._template_format_failure_result(
                e,
                f"Failed to format jinja template: {str(e)}",
                workflow_run_context,
                workflow_run_id,
                workflow_run_block_id,
                organization_id,
            )
        use_custom_smtp = self.has_custom_smtp()
        if not use_custom_smtp:
            smtp_host_value, smtp_port_value, smtp_username_value, smtp_password_value = self._decrypt_smtp_parameters(
                workflow_run_context
            )

        smtp_client = None
        try:
            if use_custom_smtp:
                custom_host, custom_port, custom_username, custom_password = await self._resolve_custom_smtp_parameters(
                    workflow_run_context
                )
                connect_hosts = await self._resolve_custom_smtp_connect_hosts(custom_host, custom_port)
                message = await self._build_email_message(
                    workflow_run_context,
                    workflow_run_id,
                    organization_id=organization_id,
                )
                # The whole SMTP session runs in a worker thread: a slow or stalling
                # user-provided server must not block the event loop and starve the
                # workflow activity heartbeat.
                await asyncio.to_thread(
                    _send_via_custom_smtp,
                    host=custom_host,
                    port=custom_port,
                    connect_hosts=connect_hosts,
                    username=custom_username,
                    password=custom_password,
                    message=message,
                )
            else:
                smtp_client = smtplib.SMTP(smtp_host_value, smtp_port_value)
                LOG.info("SendEmailBlock Connected to SMTP server")
                smtp_client.starttls()
                smtp_client.login(smtp_username_value, smtp_password_value)
                LOG.info("SendEmailBlock Logged in to SMTP server")
                message = await self._build_email_message(
                    workflow_run_context,
                    workflow_run_id,
                    organization_id=organization_id,
                )
                smtp_client.send_message(message)
            LOG.info("SendEmailBlock Email sent")
        except Exception as e:
            LOG.error("SendEmailBlock Failed to send email", exc_info=True)
            result_dict = {"success": False, "error": str(e)}
            await self.record_output_parameter_value(workflow_run_context, workflow_run_id, result_dict)
            return await self.build_block_result(
                success=False,
                failure_reason=str(e),
                output_parameter_value=result_dict,
                status=BlockStatus.failed,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )
        finally:
            if smtp_client:
                smtp_client.quit()

        result_dict = {"success": True}
        await self.record_output_parameter_value(workflow_run_context, workflow_run_id, result_dict)
        return await self.build_block_result(
            success=True,
            failure_reason=None,
            output_parameter_value=result_dict,
            status=BlockStatus.completed,
            workflow_run_block_id=workflow_run_block_id,
            organization_id=organization_id,
        )


_ParseStepResult = TypeVar("_ParseStepResult")


async def _run_blocking_parse_step(
    step: str,
    file_url: str,
    func: Callable[..., _ParseStepResult],
    *args: Any,
    **kwargs: Any,
) -> _ParseStepResult:
    """Run a blocking file-parse step in a worker thread under a wall-clock ceiling.

    Parsing on the event loop starves the workflow activity's heartbeat task, and Temporal
    reaps a run whose activity stops heartbeating. On timeout the worker thread is left to
    run to completion — threads cannot be cancelled — so the ceiling bounds the run's
    exposure to a pathological document rather than the CPU work itself.
    """
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(func, *args, **kwargs),
            timeout=FILE_PARSE_STEP_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        LOG.warning(
            "File parse step exceeded its time budget",
            step=step,
            file_url=file_url,
            timeout_seconds=FILE_PARSE_STEP_TIMEOUT_SECONDS,
        )
        raise FileParseTimeout(file_url=file_url, step=step, timeout_seconds=FILE_PARSE_STEP_TIMEOUT_SECONDS)


# csv.field_size_limit is process-global. Raising it once at import (rather than
# set/restore around each parse) avoids a race between concurrent CSV parses on
# separate worker threads stepping on each other's limit.
_MAX_CSV_FIELD_SIZE_BYTES = 10 * 1024 * 1024
csv.field_size_limit(_MAX_CSV_FIELD_SIZE_BYTES)


class FileParserBlock(Block):
    # There is a mypy bug with Literal. Without the type: ignore, mypy will raise an error:
    # Parameter 1 of Literal[...] cannot be of type "Any"
    block_type: Literal[BlockType.FILE_URL_PARSER] = BlockType.FILE_URL_PARSER  # type: ignore

    # FileParserBlock CSV constants
    _CSV_SNIFF_LINES = 5
    _CSV_BINARY_PREFIX_BYTES = 4096
    _CSV_UTF_BOMS = (codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE, codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE)
    # ZIP extraction guards (zip-bomb protection; sizes from central-directory metadata).
    # ClassVar keeps these plain class attributes — without it pydantic wraps underscore
    # names in ModelPrivateAttr and class-level access breaks.
    _MAX_ZIP_ARCHIVE_BYTES: ClassVar[int] = 512 * 1024 * 1024
    _MAX_ZIP_ENTRIES: ClassVar[int] = 1000
    _MAX_ZIP_UNCOMPRESSED_BYTES: ClassVar[int] = 1024**3
    _ZIP_JUNK_DIRS: ClassVar[tuple[str, ...]] = ("__MACOSX",)
    _ZIP_JUNK_FILES: ClassVar[tuple[str, ...]] = (".DS_Store", "Thumbs.db")
    # Classic EOCD + max comment + ZIP64 locator + ZIP64 EOCD fixed part.
    _ZIP_EOCD_TAIL_BYTES: ClassVar[int] = 65_557 + 20 + 56

    file_url: str
    file_type: FileType = FileType.AUTO_DETECT
    json_schema: dict[str, Any] | None = None
    schema_validation_max_attempts: ClassVar[int] = SCHEMA_VALIDATION_MAX_ATTEMPTS
    ocr_validation_max_attempts: ClassVar[int] = SCHEMA_VALIDATION_MAX_ATTEMPTS

    TEMPLATABLE_FIELDS: ClassVar[frozenset[str]] = frozenset({"file_url"})

    def get_failure_error_codes(self) -> list[str]:
        return ["FILE_PARSER_ERROR"]

    def get_all_parameters(
        self,
        workflow_run_id: str,
    ) -> list[PARAMETER_TYPE]:
        workflow_run_context = self.get_workflow_run_context(workflow_run_id)
        if self.file_url and workflow_run_context.has_parameter(self.file_url):
            return [workflow_run_context.get_parameter(self.file_url)]
        return []

    def format_potential_template_parameters(self, workflow_run_context: WorkflowRunContext) -> None:
        self.file_url = self.render_templatable_field("file_url", self.file_url, workflow_run_context)

        self._apply_workflow_system_prompt(workflow_run_context)

    @staticmethod
    def _validate_ocr_llm_response(llm_response: Any) -> str | None:
        if not isinstance(llm_response, dict):
            return (
                f"OCR response must be a JSON object with extracted_text string; got {_json_type_name(llm_response)}."
            )
        if not isinstance(llm_response.get("extracted_text"), str):
            return (
                "OCR response must include extracted_text as a string; "
                f"got {_json_type_name(llm_response.get('extracted_text'))}."
            )
        return None

    @staticmethod
    def _build_ocr_validation_retry_prompt(prompt: str, failure_reason: str) -> str:
        return (
            f"{prompt}\n\n"
            "Your previous OCR response failed JSON validation.\n"
            f"Validation error: {failure_reason}\n\n"
            'Retry the task. Return only valid JSON with this exact shape: {"extracted_text": "..."} '
            "Do not include markdown, code fences, explanatory text, or extra fields."
        )

    @staticmethod
    def _validate_ai_response_against_json_schema(response: Any, json_schema: dict[str, Any]) -> str | None:
        return _validate_response_against_json_schema(
            response,
            json_schema,
            "File parser",
            max_errors=SCHEMA_VALIDATION_MAX_ERRORS,
        )

    def _detect_file_type_from_url(self, file_url: str, file_path: str | None = None) -> FileType:
        """Detect file type based on file extension in the URL, with magic-byte fallback."""
        url_parsed = urlparse(file_url)
        suffix = Path(url_parsed.path).suffix.lower()
        if suffix in (".xlsx", ".xls", ".xlsm"):
            return FileType.EXCEL
        elif suffix == ".pdf":
            return FileType.PDF
        elif suffix == ".tsv":
            return FileType.CSV  # TSV files are handled by the CSV parser
        elif suffix in (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tiff", ".tif"):
            return FileType.IMAGE
        elif suffix == ".docx":
            return FileType.DOCX
        elif suffix == ".doc":
            raise InvalidFileType(
                file_url=file_url,
                file_type=FileType.DOCX,
                error="Legacy .doc format (Word 97-2003) is not supported. Please convert the file to .docx format.",
            )
        elif suffix == ".zip":
            return FileType.ZIP
        elif suffix == ".csv":
            return FileType.CSV

        # URL extension is missing or unrecognized — try magic-byte detection on the downloaded file
        if file_path:
            detected = self._detect_file_type_from_magic_bytes(file_path)
            if detected is not None:
                LOG.info(
                    "FileParserBlock Detected file type from magic bytes (URL had no recognizable extension)",
                    file_url=file_url,
                    detected_file_type=detected,
                )
                return detected

        return FileType.CSV  # Final fallback for truly unknown files

    _OLE_CFB_MAGIC: ClassVar[bytes] = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"

    def _raise_if_legacy_ole_document(self, file_path: str) -> None:
        try:
            with open(file_path, "rb") as file:
                header = file.read(len(self._OLE_CFB_MAGIC))
        except OSError:
            return
        if header != self._OLE_CFB_MAGIC:
            return
        raise InvalidFileType(
            file_url=self.file_url,
            file_type=FileType.DOCX,
            error="Legacy .doc format (Word 97-2003) is not supported. Please convert the file to .docx format.",
        )

    def _detect_file_type_from_magic_bytes(self, file_path: str) -> FileType | None:
        """Detect file type from magic bytes using the filetype library. Returns None if unrecognized.

        Raises InvalidFileType for legacy OLE Office documents (e.g. Word 97-2003 .doc), which no
        parser here supports and which the CSV fallback would otherwise reject as binary data.
        """
        kind = filetype.guess(file_path)
        mime = kind.mime if kind else None
        if mime == "application/pdf":
            return FileType.PDF
        elif mime in (
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "application/vnd.ms-excel",
        ):
            return FileType.EXCEL
        elif mime == "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
            return FileType.DOCX
        elif mime == "application/zip":
            # OOXML files are ZIP containers matched before this generic branch, so only plain archives reach it.
            return FileType.ZIP
        elif mime is not None and mime.startswith("image/"):
            return FileType.IMAGE
        # Unrecognized or unmapped mime (e.g. application/msword): an OLE CFB container is a legacy
        # Office document nothing downstream can parse, so raise instead of falling back to CSV.
        self._raise_if_legacy_ole_document(file_path)
        return None

    def _detect_file_encoding(self, file_path: str) -> str:
        """Detect the encoding of a file using charset-normalizer with fallbacks.

        Reads a sample of the file (first 64KB) to detect encoding efficiently.
        Falls back through common encodings if detection fails.
        """
        sample_size = 65536  # 64KB sample for detection
        with open(file_path, "rb") as f:
            raw_data = f.read(sample_size)

        result = from_bytes(raw_data)
        best_match = result.best()
        if best_match and best_match.encoding:
            return best_match.encoding

        for encoding in ["utf-8", "cp1252", "latin-1"]:
            try:
                raw_data.decode(encoding)
                return encoding
            except UnicodeDecodeError:
                continue

        # latin-1 always succeeds (1:1 byte mapping), so this is a safety fallback
        return "latin-1"

    def _sniff_csv_delimiter(self, file_path: str) -> tuple[str, str]:
        """Return (delimiter, encoding). Samples full lines to avoid mid-row truncation."""
        # Read small raw byte prefix to quickly detect empty binary files before attempting text decoding/sniffing
        with open(file_path, "rb") as f:
            raw_prefix = f.read(self._CSV_BINARY_PREFIX_BYTES)
        # Reject files that contain no meaningful bytes
        if not raw_prefix.strip():
            raise csv.Error("File is empty")
        # Reject likely binary content:
        # - Presence of null bytes is a strong binary signal
        # - Exception: UTF-16/UTF-32 text often starts with BOM and may contain null bytes
        if b"\x00" in raw_prefix and not raw_prefix.startswith(self._CSV_UTF_BOMS):
            raise csv.Error("File contains binary data")

        # Detect best text encoding for file, then read only the first N full lines so csv.Sniffer sees complete rows
        encoding = self._detect_file_encoding(file_path)
        with open(file_path, encoding=encoding, errors="replace", newline="") as file:
            lines: list[str] = []
            for _ in range(self._CSV_SNIFF_LINES):
                line = file.readline()
                if not line:
                    break
                lines.append(line)

        # Build the sniffer sample from complete lines only
        sample = "".join(lines)
        # Guard against files that decode but still contain no meaningful text
        if not sample.strip():
            raise csv.Error("File is empty")

        try:
            delimiter = csv.Sniffer().sniff(sample).delimiter
        except csv.Error:
            delimiter = "\t" if file_path.lower().endswith(".tsv") else ","
        return delimiter, encoding

    def validate_file_type(self, file_url_used: str, file_path: str) -> None:
        if self.file_type == FileType.CSV:
            try:
                self._sniff_csv_delimiter(file_path)
            except csv.Error as e:
                raise InvalidFileType(file_url=file_url_used, file_type=self.file_type, error=str(e))
        elif self.file_type == FileType.EXCEL:
            try:
                # Try to read the file with pandas to validate it's a valid Excel file
                pd.read_excel(file_path, nrows=1, engine="calamine")
            except Exception as e:
                raise InvalidFileType(
                    file_url=file_url_used, file_type=self.file_type, error=f"Invalid Excel file format: {str(e)}"
                )
        elif self.file_type == FileType.PDF:
            try:
                validate_pdf_file(file_path, file_identifier=file_url_used)
            except PDFParsingError as e:
                raise InvalidFileType(file_url=file_url_used, file_type=self.file_type, error=str(e))
        elif self.file_type == FileType.IMAGE:
            kind = filetype.guess(file_path)
            if kind is None or not kind.mime.startswith("image/"):
                raise InvalidFileType(
                    file_url=file_url_used, file_type=self.file_type, error="File is not a valid image"
                )
        elif self.file_type == FileType.DOCX:
            try:
                # Try to open the file with python-docx to validate it's a valid DOCX file
                docx.Document(file_path)
            except Exception as e:
                raise InvalidFileType(
                    file_url=file_url_used, file_type=self.file_type, error=f"Invalid DOCX file format: {str(e)}"
                )
        elif self.file_type == FileType.ZIP:
            if not zipfile.is_zipfile(file_path):
                raise InvalidFileType(
                    file_url=file_url_used, file_type=self.file_type, error="File is not a valid ZIP archive"
                )

    async def _parse_csv_file(self, file_path: str) -> list[dict[str, Any]]:
        """Parse CSV/TSV file and return list of dictionaries."""
        return await _run_blocking_parse_step("CSV parsing", self.file_url, self._parse_csv_file_sync, file_path)

    def _parse_csv_file_sync(self, file_path: str) -> list[dict[str, Any]]:
        delimiter, encoding = self._sniff_csv_delimiter(file_path)
        with open(file_path, encoding=encoding, errors="replace", newline="") as file:
            reader = csv.DictReader(file, delimiter=delimiter)
            return list(reader)

    def _clean_dataframe_for_json(self, df: pd.DataFrame) -> list[dict[str, Any]]:
        """Clean DataFrame to ensure it can be serialized to JSON."""
        # Replace NaN and NaT values with "nan" string
        df_cleaned = df.replace({pd.NA: "nan", pd.NaT: "nan"})
        df_cleaned = df_cleaned.where(pd.notna(df_cleaned), "nan")

        # Convert to list of dictionaries
        records = df_cleaned.to_dict("records")

        # Additional cleaning for any remaining problematic values
        for record in records:
            for key, value in record.items():
                if pd.isna(value) or value == "NaN" or value == "NaT":
                    record[key] = "nan"
                elif isinstance(value, (pd.Timestamp, datetime, date, time)):
                    # NaT timestamps are already caught by pd.isna() above, so this is always valid
                    record[key] = value.isoformat()
                elif isinstance(value, pd.Timedelta):
                    record[key] = str(value)

        return records

    async def _parse_excel_file(self, file_path: str) -> list[dict[str, Any]]:
        """Parse Excel file and return list of dictionaries."""
        return await _run_blocking_parse_step("Excel parsing", self.file_url, self._parse_excel_file_sync, file_path)

    def _parse_excel_file_sync(self, file_path: str) -> list[dict[str, Any]]:
        try:
            # Read Excel file with pandas, specifying engine explicitly
            df = pd.read_excel(file_path, engine="calamine")
            # Clean and convert DataFrame to list of dictionaries
            return self._clean_dataframe_for_json(df)
        except ImportError as e:
            raise InvalidFileType(
                file_url=self.file_url,
                file_type=self.file_type,
                error=f"Missing required dependency for Excel parsing: {str(e)}. Please install calamine: pip install python-calamine",
            )
        except Exception as e:
            raise InvalidFileType(
                file_url=self.file_url, file_type=self.file_type, error=f"Failed to parse Excel file: {str(e)}"
            )

    async def _parse_pdf_file(
        self,
        file_path: str,
        workflow_run_block_id: str | None = None,
        organization_id: str | None = None,
    ) -> str:
        """Parse PDF file and return extracted text.

        Uses the shared PDF parsing utility that tries pypdf first,
        then falls back to pdfplumber if pypdf fails. If text extraction
        yields empty/minimal content (e.g. scanned or image-based PDFs),
        renders pages as images and sends them to a vision LLM for OCR.
        """
        try:
            extracted_text = await _run_blocking_parse_step(
                "PDF text extraction",
                self.file_url,
                extract_pdf_file,
                file_path,
                file_identifier=self.file_url,
            )
        except PDFParsingError as e:
            raise InvalidFileType(file_url=self.file_url, file_type=self.file_type, error=str(e))

        # If text extraction returned meaningful content, use it directly
        if extracted_text.strip():
            return extracted_text

        # Scanned / image-based PDF — render pages as images and OCR each page in
        # its own vision-LLM call. A single call covering every page collapses a
        # multi-page document down to the first page or two.
        LOG.info(
            "PDF text extraction returned empty content, falling back to vision LLM OCR",
            file_url=self.file_url,
        )
        try:
            page_images = await _run_blocking_parse_step(
                "PDF page rendering",
                self.file_url,
                render_pdf_pages_as_images,
                file_path,
                file_identifier=self.file_url,
                max_pages=MAX_PDF_OCR_PAGES,
            )
            if not page_images:
                return extracted_text
            return await self._ocr_pdf_pages(
                page_images,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )
        except Exception:
            LOG.exception(
                "Failed to extract text from PDF via vision LLM fallback",
                file_url=self.file_url,
            )
            raise

    async def _resolve_file_parser_handler(
        self, prompt_type: str, distinct_id: str | None, organization_id: str | None
    ) -> LLMAPIHandler:
        """Resolve the default handler for a file-parser prompt type.

        Honors the LLM_CONFIG_BY_PROMPT_TYPE PostHog flag (keyed by prompt type) so the
        OCR and extraction models can be set without a deploy; falls back to the primary
        handler. A block-level override_llm_key still takes precedence at the call site.
        """
        if distinct_id:
            posthog_handler = await get_llm_handler_for_prompt_type(prompt_type, distinct_id, organization_id)
            if posthog_handler:
                return posthog_handler
        return get_org_aware_primary_llm_api_handler(default=app.LLM_API_HANDLER)

    async def _ocr_pdf_pages(
        self,
        page_images: list[bytes],
        workflow_run_block_id: str | None = None,
        organization_id: str | None = None,
    ) -> str:
        """OCR each rendered PDF page in its own vision-LLM call and concatenate.

        Per-page transcription avoids the single-call collapse where a multi-page
        document is summarized down to its first page(s). Pages are transcribed with
        bounded concurrency, reassembled in page order with page markers, and
        truncated at a page boundary once MAX_FILE_PARSE_INPUT_TOKENS is reached.
        """
        if self.ocr_validation_max_attempts <= 0:
            raise ValueError("OCR validation max attempts must be greater than 0.")

        llm_prompt = prompt_engine.load_prompt("extract-text-from-image")
        default_handler = await self._resolve_file_parser_handler(
            "extract-text-from-image", workflow_run_block_id, organization_id
        )
        llm_api_handler = LLMAPIHandlerFactory.get_override_llm_api_handler(
            self.override_llm_key_for_organization(organization_id), default=default_handler
        )
        semaphore = asyncio.Semaphore(PDF_OCR_PAGE_CONCURRENCY)

        async def _ocr_page(page_image: bytes) -> str:
            async with semaphore:
                prompt_for_attempt = llm_prompt
                for attempt in range(self.ocr_validation_max_attempts):
                    try:
                        # OCR transcription intentionally skips system_prompt; it still applies
                        # to the downstream extract-information-from-file-text call.
                        llm_response = await llm_api_handler(
                            prompt=prompt_for_attempt,
                            prompt_name="extract-text-from-image",
                            screenshots=[page_image],
                            # Schema validation must inspect the raw parsed root; dict coercion can hide bad OCR JSON.
                            force_dict=False,
                            workflow_run_block_id=workflow_run_block_id,
                            organization_id=organization_id,
                        )
                    except (InvalidLLMResponseFormat, InvalidLLMResponseType) as e:
                        failure_reason = _llm_response_format_failure_reason(e)
                        will_retry = attempt + 1 < self.ocr_validation_max_attempts
                        LOG.warning(
                            "FileParserBlock PDF OCR LLM response failed response-format validation",
                            file_url=self.file_url,
                            attempt=attempt + 1,
                            max_attempts=self.ocr_validation_max_attempts,
                            will_retry=will_retry,
                            error_type=type(e).__name__,
                        )
                        if not will_retry:
                            raise ValueError(failure_reason) from e
                        prompt_for_attempt = self._build_ocr_validation_retry_prompt(llm_prompt, failure_reason)
                        continue

                    ocr_failure_reason = self._validate_ocr_llm_response(llm_response)
                    if not ocr_failure_reason:
                        return llm_response.get("extracted_text", "") or ""

                    will_retry = attempt + 1 < self.ocr_validation_max_attempts
                    LOG.warning(
                        "FileParserBlock PDF OCR LLM response failed schema validation",
                        file_url=self.file_url,
                        attempt=attempt + 1,
                        max_attempts=self.ocr_validation_max_attempts,
                        will_retry=will_retry,
                        failure_reason=ocr_failure_reason,
                    )
                    if not will_retry:
                        raise ValueError(ocr_failure_reason)
                    prompt_for_attempt = self._build_ocr_validation_retry_prompt(llm_prompt, ocr_failure_reason)
                raise RuntimeError("OCR retry loop exhausted without returning or raising.")

        page_results = await asyncio.gather(
            *(_ocr_page(page_image) for page_image in page_images),
            return_exceptions=True,
        )

        # A total OCR outage must fail the block, not record an empty success — match the
        # prior single-call path, which propagated OCR errors. Partial failures are skipped below.
        errors = [r for r in page_results if isinstance(r, BaseException)]
        if errors and len(errors) == len(page_results):
            raise errors[0]

        page_chunks: list[str] = []
        current_tokens = 0
        for page_number, result in enumerate(page_results, start=1):
            if isinstance(result, BaseException):
                LOG.warning(
                    "Failed to OCR a PDF page via vision LLM, skipping it",
                    file_url=self.file_url,
                    page_number=page_number,
                    error=str(result),
                )
                continue
            page_text = result.strip()
            if not page_text:
                continue
            chunk = f"--- Page {page_number} ---\n{page_text}"
            chunk_tokens = count_tokens(chunk)
            if current_tokens + chunk_tokens > MAX_FILE_PARSE_INPUT_TOKENS:
                LOG.warning(
                    "PDF OCR text exceeds token limit, truncating at page boundary",
                    file_url=self.file_url,
                    pages_included=page_number - 1,
                    total_pages=len(page_results),
                    max_tokens=MAX_FILE_PARSE_INPUT_TOKENS,
                )
                break
            current_tokens += chunk_tokens
            page_chunks.append(chunk)

        return "\n\n".join(page_chunks)

    async def _parse_image_file(
        self,
        file_path: str,
        workflow_run_block_id: str | None = None,
        organization_id: str | None = None,
    ) -> str:
        """Parse image file using vision LLM for OCR."""
        if self.ocr_validation_max_attempts <= 0:
            raise ValueError("OCR validation max attempts must be greater than 0.")

        try:
            with open(file_path, "rb") as f:
                image_bytes = f.read()

            llm_prompt = prompt_engine.load_prompt("extract-text-from-image")
            default_handler = await self._resolve_file_parser_handler(
                "extract-text-from-image", workflow_run_block_id, organization_id
            )
            llm_api_handler = LLMAPIHandlerFactory.get_override_llm_api_handler(
                self.override_llm_key_for_organization(organization_id), default=default_handler
            )
            # OCR transcription intentionally skips system_prompt — see
            # _parse_pdf_file_with_vision_ocr for rationale.
            prompt_for_attempt = llm_prompt
            for attempt in range(self.ocr_validation_max_attempts):
                try:
                    llm_response = await llm_api_handler(
                        prompt=prompt_for_attempt,
                        prompt_name="extract-text-from-image",
                        screenshots=[image_bytes],
                        # Schema validation must inspect the raw parsed root; dict coercion can hide bad OCR JSON.
                        force_dict=False,
                        workflow_run_block_id=workflow_run_block_id,
                        organization_id=organization_id,
                    )
                except (InvalidLLMResponseFormat, InvalidLLMResponseType) as e:
                    failure_reason = _llm_response_format_failure_reason(e)
                    will_retry = attempt + 1 < self.ocr_validation_max_attempts
                    LOG.warning(
                        "FileParserBlock image OCR LLM response failed response-format validation",
                        file_url=self.file_url,
                        attempt=attempt + 1,
                        max_attempts=self.ocr_validation_max_attempts,
                        will_retry=will_retry,
                        error_type=type(e).__name__,
                    )
                    if not will_retry:
                        raise ValueError(failure_reason) from e
                    prompt_for_attempt = self._build_ocr_validation_retry_prompt(llm_prompt, failure_reason)
                    continue

                ocr_failure_reason = self._validate_ocr_llm_response(llm_response)
                if not ocr_failure_reason:
                    return llm_response.get("extracted_text", "") or ""

                will_retry = attempt + 1 < self.ocr_validation_max_attempts
                LOG.warning(
                    "FileParserBlock image OCR LLM response failed schema validation",
                    file_url=self.file_url,
                    attempt=attempt + 1,
                    max_attempts=self.ocr_validation_max_attempts,
                    will_retry=will_retry,
                    failure_reason=ocr_failure_reason,
                )
                if not will_retry:
                    raise ValueError(ocr_failure_reason)
                prompt_for_attempt = self._build_ocr_validation_retry_prompt(llm_prompt, ocr_failure_reason)

            raise RuntimeError("OCR retry loop exhausted without returning or raising.")
        except Exception:
            LOG.exception("Failed to extract text from image via OCR", file_url=self.file_url)
            raise

    async def _parse_docx_file(self, file_path: str, max_tokens: int = MAX_FILE_PARSE_INPUT_TOKENS) -> str:
        """Parse DOCX file and return extracted text.

        Extracts text from all paragraphs and tables in the document,
        respecting the token limit.
        """
        return await _run_blocking_parse_step(
            "DOCX parsing", self.file_url, self._parse_docx_file_sync, file_path, max_tokens
        )

    def _parse_docx_file_sync(self, file_path: str, max_tokens: int = MAX_FILE_PARSE_INPUT_TOKENS) -> str:
        try:
            document = docx.Document(file_path)
            text_parts = []
            current_tokens = 0
            truncated = False

            # Extract text from paragraphs
            for paragraph in document.paragraphs:
                if paragraph.text.strip():
                    para_tokens = count_tokens(paragraph.text)
                    if max_tokens and current_tokens + para_tokens > max_tokens:
                        LOG.warning(
                            "DOCX text exceeds token limit, truncating",
                            file_url=self.file_url,
                            current_tokens=current_tokens,
                            max_tokens=max_tokens,
                        )
                        truncated = True
                        break
                    text_parts.append(paragraph.text)
                    current_tokens += para_tokens

            # Extract text from tables (only if not already truncated)
            if not truncated:
                for table in document.tables:
                    if truncated:
                        break
                    for row in table.rows:
                        row_text = []
                        for cell in row.cells:
                            cell_text = cell.text.strip()
                            if cell_text:
                                row_text.append(cell_text)
                        if row_text:
                            row_str = " | ".join(row_text)
                            row_tokens = count_tokens(row_str)
                            if max_tokens and current_tokens + row_tokens > max_tokens:
                                LOG.warning(
                                    "DOCX text exceeds token limit, truncating at table",
                                    file_url=self.file_url,
                                    current_tokens=current_tokens,
                                    max_tokens=max_tokens,
                                )
                                truncated = True
                                break
                            text_parts.append(row_str)
                            current_tokens += row_tokens

            extracted_text = "\n".join(text_parts)
            extracted_text = sanitize_postgres_text(extracted_text)
            LOG.info(
                "Successfully parsed DOCX file",
                file_url=self.file_url,
                paragraph_count=len(document.paragraphs),
                table_count=len(document.tables),
                text_length=len(extracted_text),
                truncated=truncated,
            )
            return extracted_text
        except Exception as e:
            raise InvalidFileType(
                file_url=self.file_url, file_type=self.file_type, error=f"Failed to parse DOCX file: {str(e)}"
            )

    @classmethod
    def _is_zip_junk_member(cls, member_name: str) -> bool:
        parts = PurePosixPath(member_name).parts
        if not parts:
            return True
        if any(part in cls._ZIP_JUNK_DIRS for part in parts):
            return True
        return parts[-1] in cls._ZIP_JUNK_FILES or parts[-1].startswith("._")

    @classmethod
    def _read_zip_total_entry_count(cls, file_path: str) -> int | None:
        try:
            file_size = os.path.getsize(file_path)
            tail_size = min(file_size, cls._ZIP_EOCD_TAIL_BYTES)
            with open(file_path, "rb") as file:
                file.seek(file_size - tail_size)
                tail = file.read(tail_size)

            eocd_index = tail.rfind(b"PK\x05\x06")
            if eocd_index < 0 or eocd_index + 22 > len(tail):
                return None

            count = int.from_bytes(tail[eocd_index + 10 : eocd_index + 12], "little")
            if count != 0xFFFF:
                return count

            zip64_eocd_index = tail.rfind(b"PK\x06\x06")
            if zip64_eocd_index < 0 or zip64_eocd_index + 40 > len(tail):
                return None
            return int.from_bytes(tail[zip64_eocd_index + 32 : zip64_eocd_index + 40], "little")
        except Exception:
            return None

    def _check_extracted_size_within_limit(self, total_bytes: int) -> None:
        if total_bytes > self._MAX_ZIP_UNCOMPRESSED_BYTES:
            raise InvalidFileType(
                file_url=self.file_url,
                file_type=self.file_type,
                error=f"ZIP archive uncompressed content exceeds the limit of {self._MAX_ZIP_UNCOMPRESSED_BYTES} bytes",
            )

    def _extract_zip_file(
        self, file_path: str, workflow_run_id: str, workflow_run_block_id: str
    ) -> list[dict[str, Any]]:
        """Extract a ZIP archive into the run's download directory.

        Returns the extracted files as {"file_name", "file_path", "file_size"} dicts sorted by
        file_name, so downstream blocks can consume the files from the local filesystem.
        """
        context = skyvern_context.current()
        run_id = context.run_id if context and context.run_id else workflow_run_id
        zip_stem = sanitize_filename(Path(file_path).stem, default="archive")
        extract_dir = (
            get_path_for_workflow_download_directory(run_id) / "unzipped" / f"{zip_stem}_{workflow_run_block_id}"
        )

        archive_size = os.path.getsize(file_path)
        if archive_size > self._MAX_ZIP_ARCHIVE_BYTES:
            raise InvalidFileType(
                file_url=self.file_url,
                file_type=self.file_type,
                error=f"ZIP archive size ({archive_size} bytes) exceeds the limit of {self._MAX_ZIP_ARCHIVE_BYTES} bytes",
            )

        declared_entry_count = self._read_zip_total_entry_count(file_path)
        if declared_entry_count is not None and declared_entry_count > self._MAX_ZIP_ENTRIES:
            raise InvalidFileType(
                file_url=self.file_url,
                file_type=self.file_type,
                error=f"ZIP archive declares {declared_entry_count} entries, exceeding the limit of {self._MAX_ZIP_ENTRIES}",
            )

        with zipfile.ZipFile(file_path) as zip_file:
            members = [
                member
                for member in zip_file.infolist()
                if not member.is_dir() and not self._is_zip_junk_member(member.filename)
            ]
            if len(members) > self._MAX_ZIP_ENTRIES:
                raise InvalidFileType(
                    file_url=self.file_url,
                    file_type=self.file_type,
                    error=f"ZIP archive contains {len(members)} files, exceeding the limit of {self._MAX_ZIP_ENTRIES}",
                )
            total_uncompressed_bytes = sum(member.file_size for member in members)
            # The declared-size check is advisory; measured bytes after extraction are authoritative.
            if total_uncompressed_bytes > self._MAX_ZIP_UNCOMPRESSED_BYTES:
                raise InvalidFileType(
                    file_url=self.file_url,
                    file_type=self.file_type,
                    error=f"ZIP archive uncompressed size ({total_uncompressed_bytes} bytes) exceeds the limit of {self._MAX_ZIP_UNCOMPRESSED_BYTES} bytes",
                )
            if any(member.flag_bits & 0x1 for member in members):
                raise InvalidFileType(
                    file_url=self.file_url,
                    file_type=self.file_type,
                    error="Password-protected ZIP archives are not supported",
                )

            extract_dir.mkdir(parents=True, exist_ok=True)
            # Keyed by destination path: member names that sanitize to the same destination
            # (e.g. "a.csv", "/a.csv", "../a.csv") overwrite on disk, and per ZIP semantics the
            # last entry wins — keep one list entry per file instead of duplicates.
            # measured_total_bytes intentionally counts every member's written bytes (including
            # overwritten collisions) because it guards total write I/O, not final disk usage.
            files_by_path: dict[str, dict[str, Any]] = {}
            measured_total_bytes = 0
            for member in members:
                if member.file_size > self._MAX_ZIP_UNCOMPRESSED_BYTES - measured_total_bytes:
                    raise InvalidFileType(
                        file_url=self.file_url,
                        file_type=self.file_type,
                        error=f"ZIP archive uncompressed content exceeds the limit of {self._MAX_ZIP_UNCOMPRESSED_BYTES} bytes",
                    )
                # ZipFile.extract sanitizes absolute paths and ".." components, so members cannot
                # escape extract_dir.
                extracted_path = zip_file.extract(member, path=extract_dir)
                extracted_size = Path(extracted_path).stat().st_size
                measured_total_bytes += extracted_size
                self._check_extracted_size_within_limit(measured_total_bytes)
                if extracted_path in files_by_path:
                    LOG.warning(
                        "FileParserBlock ZIP members collide after path sanitization, keeping the last one",
                        file_url=self.file_url,
                        member_name=member.filename,
                    )
                files_by_path[extracted_path] = {
                    "file_name": str(Path(extracted_path).relative_to(extract_dir)),
                    "file_path": extracted_path,
                    "file_size": extracted_size,
                }

        extracted_files = sorted(files_by_path.values(), key=lambda file_info: file_info["file_name"])
        LOG.info(
            "FileParserBlock Extracted ZIP archive",
            file_url=self.file_url,
            extract_dir=str(extract_dir),
            file_count=len(extracted_files),
        )
        return extracted_files

    async def _parse_file_of_type(
        self,
        file_type: FileType,
        file_path: str,
        workflow_run_block_id: str | None = None,
        organization_id: str | None = None,
    ) -> str | list[dict[str, Any]] | None:
        """Parse a file with the parser for its type; returns None for unsupported types."""
        if file_type == FileType.CSV:
            return await self._parse_csv_file(file_path)
        if file_type == FileType.EXCEL:
            return await self._parse_excel_file(file_path)
        if file_type == FileType.PDF:
            return await self._parse_pdf_file(
                file_path, workflow_run_block_id=workflow_run_block_id, organization_id=organization_id
            )
        if file_type == FileType.IMAGE:
            return await self._parse_image_file(
                file_path, workflow_run_block_id=workflow_run_block_id, organization_id=organization_id
            )
        if file_type == FileType.DOCX:
            return await self._parse_docx_file(file_path)
        return None

    def _bound_extraction_input_tokens(self, content_str: str) -> str:
        tokens = encode_tokens(content_str)
        if len(tokens) <= MAX_FILE_PARSE_INPUT_TOKENS:
            return content_str
        LOG.warning(
            "File parser extraction input exceeds token limit, truncating",
            file_url=self.file_url,
            content_tokens=len(tokens),
            max_tokens=MAX_FILE_PARSE_INPUT_TOKENS,
        )
        return decode_tokens(tokens[:MAX_FILE_PARSE_INPUT_TOKENS])

    async def _extract_with_ai(
        self,
        content: str | list[dict[str, Any]],
        workflow_run_context: WorkflowRunContext,
        workflow_run_block_id: str | None = None,
        organization_id: str | None = None,
    ) -> dict[str, Any] | list | str | None:
        """Extract structured data using AI based on json_schema."""
        # Use local variable to avoid mutating the instance
        schema_to_use = self.json_schema or _default_structured_output_schema("Information extracted from the file")
        if not validate_schema(schema_to_use):
            raise ValueError("File parser JSON schema is invalid.")

        # Convert content to string for AI processing
        if isinstance(content, list):
            content_str = json.dumps(content, separators=(",", ":"))
        else:
            content_str = content

        content_str = await _run_blocking_parse_step(
            "extraction input token bounding",
            self.file_url,
            self._bound_extraction_input_tokens,
            content_str,
        )

        llm_prompt = prompt_engine.load_prompt(
            "extract-information-from-file-text", extracted_text_content=content_str, json_schema=schema_to_use
        )

        llm_key = self.override_llm_key_for_organization(organization_id)
        default_handler = await self._resolve_file_parser_handler(
            "extract-information-from-file-text", workflow_run_block_id, organization_id
        )
        llm_api_handler = LLMAPIHandlerFactory.get_override_llm_api_handler(llm_key, default=default_handler)

        prompt_for_attempt = llm_prompt
        for attempt in range(self.schema_validation_max_attempts):
            try:
                llm_response = await llm_api_handler(
                    prompt=prompt_for_attempt,
                    prompt_name="extract-information-from-file-text",
                    # Schema validation must inspect the raw parsed root; dict coercion can hide wrong-root responses.
                    force_dict=False,
                    system_prompt=self.workflow_system_prompt,
                    workflow_run_block_id=workflow_run_block_id,
                    organization_id=organization_id,
                )
            except (InvalidLLMResponseFormat, InvalidLLMResponseType) as e:
                failure_reason = _llm_response_format_failure_reason(e)
                will_retry = attempt + 1 < self.schema_validation_max_attempts
                LOG.warning(
                    "FileParserBlock extraction LLM response failed response-format validation",
                    file_url=self.file_url,
                    attempt=attempt + 1,
                    max_attempts=self.schema_validation_max_attempts,
                    will_retry=will_retry,
                    error_type=type(e).__name__,
                    schema_type=schema_to_use.get("type"),
                )
                if not will_retry:
                    raise ValueError(failure_reason) from e
                prompt_for_attempt = _build_schema_validation_retry_prompt(llm_prompt, failure_reason)
                continue

            schema_validation_failure = self._validate_ai_response_against_json_schema(llm_response, schema_to_use)
            if not schema_validation_failure:
                return llm_response

            is_schema_configuration_failure = _is_schema_configuration_failure(schema_validation_failure)
            will_retry = attempt + 1 < self.schema_validation_max_attempts and not is_schema_configuration_failure
            LOG.warning(
                "FileParserBlock extraction LLM response failed schema validation",
                file_url=self.file_url,
                attempt=attempt + 1,
                max_attempts=self.schema_validation_max_attempts,
                will_retry=will_retry,
                failure_reason=schema_validation_failure,
                schema_type=schema_to_use.get("type"),
            )
            if not will_retry:
                raise ValueError(schema_validation_failure)
            prompt_for_attempt = _build_schema_validation_retry_prompt(
                llm_prompt,
                schema_validation_failure,
            )

        raise AssertionError("unreachable schema validation retry loop exit")

    async def _record_failure(
        self,
        workflow_run_context: WorkflowRunContext,
        workflow_run_id: str,
        workflow_run_block_id: str,
        organization_id: str | None,
        failure_reason: str,
    ) -> BlockResult:
        error_codes = self.get_failure_error_codes()
        failure_output = build_block_failure_output(failure_reason, error_codes)
        await self.record_output_parameter_value(workflow_run_context, workflow_run_id, failure_output)
        return await self.build_block_result(
            success=False,
            failure_reason=failure_reason,
            output_parameter_value=failure_output,
            status=BlockStatus.failed,
            workflow_run_block_id=workflow_run_block_id,
            organization_id=organization_id,
            error_codes=error_codes or None,
        )

    @staticmethod
    def _extract_file_url_from_block_output(value: Any) -> str | None:
        """Extract a file URL from a block output value.

        When users pass an entire block output (e.g. ``{{ block_8_output }}``) as the
        ``file_url``, the resolved value may be a dict or a string representation of a
        dict that contains a ``downloaded_files`` list.  This helper unwraps that
        structure and returns the URL of the first downloaded file.

        Handles three forms:
        - dict with a ``downloaded_files`` list
        - JSON string encoding such a dict
        - Python dict-repr string produced by Jinja's default ``str()`` rendering
        """
        return extract_file_url_from_block_output(value)

    async def execute(
        self,
        workflow_run_id: str,
        workflow_run_block_id: str,
        organization_id: str | None = None,
        browser_session_id: str | None = None,
        **kwargs: dict,
    ) -> BlockResult:
        workflow_run_context = self.get_workflow_run_context(workflow_run_id)

        if (
            self.file_url
            and workflow_run_context.has_parameter(self.file_url)
            and workflow_run_context.has_value(self.file_url)
        ):
            file_url_parameter_value = workflow_run_context.get_value(self.file_url)
            if file_url_parameter_value:
                extracted_url = self._extract_file_url_from_block_output(file_url_parameter_value)
                if extracted_url:
                    LOG.info(
                        "FileParserBlock Extracted file URL from block output parameter",
                        extracted_url=extracted_url,
                        file_url_parameter_key=self.file_url,
                    )
                    self.file_url = extracted_url
                else:
                    LOG.info(
                        "FileParserBlock File URL is parameterized, using parameter value",
                        file_url_parameter_value=file_url_parameter_value,
                        file_url_parameter_key=self.file_url,
                    )
                    self.file_url = file_url_parameter_value
            else:
                # Absent optional parameter: fail on the empty URL rather than
                # treating the literal parameter key as a URL.
                self.file_url = ""

        try:
            self.format_potential_template_parameters(workflow_run_context)
        except Exception as e:
            return await self._template_format_failure_result(
                e,
                f"Failed to format jinja template: {str(e)}",
                workflow_run_context,
                workflow_run_id,
                workflow_run_block_id,
                organization_id,
            )

        # After Jinja rendering, self.file_url may be a stringified block output
        # (e.g. when the user wrote ``{{ block_8_output }}``). Try to extract the
        # file URL from it before attempting the download.
        extracted_url = self._extract_file_url_from_block_output(self.file_url)
        if extracted_url:
            LOG.info(
                "FileParserBlock Extracted file URL from rendered block output",
                extracted_url=extracted_url,
                rendered_value=self.file_url,
            )
            self.file_url = extracted_url

        try:
            context = skyvern_context.current()
            run_id = context.run_id if context and context.run_id else workflow_run_id
            file_path = await resolve_local_or_download_file(self.file_url, run_id, organization_id=organization_id)

            # Resolve AUTO_DETECT (and legacy CSV-as-default) via URL/magic-byte detection;
            # IMAGE/EXCEL/PDF/DOCX/ZIP are honored as user overrides.
            auto_detected_csv_fallback = False
            if self.file_type not in (FileType.IMAGE, FileType.EXCEL, FileType.PDF, FileType.DOCX, FileType.ZIP):
                configured_file_type = self.file_type
                detected_file_type = self._detect_file_type_from_url(self.file_url, file_path=file_path)
                auto_detected_csv_fallback = (
                    configured_file_type in (FileType.AUTO_DETECT, FileType.CSV)
                    and detected_file_type == FileType.CSV
                    and Path(urlparse(self.file_url).path).suffix.lower() not in {".csv", ".tsv"}
                )
                self.file_type = detected_file_type

            # Validation opens the document, so on a large file it is as slow as the parse itself.
            try:
                await _run_blocking_parse_step(
                    "file type validation", self.file_url, self.validate_file_type, self.file_url, file_path
                )
            except InvalidFileType as e:
                if auto_detected_csv_fallback:
                    validation_error = str(e).partition("Error: ")[2] or str(e)
                    raise InvalidFileType(
                        file_url=self.file_url,
                        file_type="auto-detect",
                        error=(
                            "Unable to auto-detect a supported file type from the downloaded file. "
                            f"Underlying validation error: {validation_error}"
                        ),
                    ) from e
                raise
        except Exception as e:
            return await self._record_failure(
                workflow_run_context,
                workflow_run_id,
                workflow_run_block_id,
                organization_id,
                f"Failed to download or validate file: {str(e)}",
            )

        LOG.debug(
            "FileParserBlock After file type validation",
            file_type=self.file_type,
            json_schema_present=self.json_schema is not None,
            json_schema_type=type(self.json_schema),
        )

        # Parse the file based on type
        parsed_data: str | list[dict[str, Any]]
        try:
            if self.file_type == FileType.ZIP:
                extracted_zip_files = await asyncio.to_thread(
                    self._extract_zip_file, file_path, workflow_run_id, workflow_run_block_id
                )
                parsed_data = extracted_zip_files
            else:
                maybe_parsed = await self._parse_file_of_type(
                    self.file_type,
                    file_path,
                    workflow_run_block_id=workflow_run_block_id,
                    organization_id=organization_id,
                )
                if maybe_parsed is None:
                    return await self._record_failure(
                        workflow_run_context,
                        workflow_run_id,
                        workflow_run_block_id,
                        organization_id,
                        f"Unsupported file type: {self.file_type}",
                    )
                parsed_data = maybe_parsed
        except Exception as e:
            return await self._record_failure(
                workflow_run_context,
                workflow_run_id,
                workflow_run_block_id,
                organization_id,
                f"Failed to parse {self.file_type} file: {str(e)}",
            )

        final_data: Any
        LOG.debug(
            "FileParserBlock JSON schema check",
            has_json_schema=self.json_schema is not None,
            json_schema_type=type(self.json_schema),
            json_schema=self.json_schema,
        )

        if self.file_type == FileType.ZIP:
            if self.json_schema:
                LOG.warning(
                    "FileParserBlock json_schema is ignored for ZIP archives; returning extracted file list",
                    file_url=self.file_url,
                )
            final_data = parsed_data
        elif self.json_schema:
            try:
                ai_extracted_data = await self._extract_with_ai(
                    parsed_data,
                    workflow_run_context,
                    workflow_run_block_id=workflow_run_block_id,
                    organization_id=organization_id,
                )
                final_data = ai_extracted_data
            except Exception as e:
                return await self._record_failure(
                    workflow_run_context,
                    workflow_run_id,
                    workflow_run_block_id,
                    organization_id,
                    f"Failed to extract data with AI: {str(e)}",
                )
        else:
            # Return raw parsed data
            final_data = parsed_data

        # Record the parsed data
        await self.record_output_parameter_value(workflow_run_context, workflow_run_id, final_data)
        return await self.build_block_result(
            success=True,
            failure_reason=None,
            output_parameter_value=final_data,
            status=BlockStatus.completed,
            workflow_run_block_id=workflow_run_block_id,
            organization_id=organization_id,
        )


class PDFParserBlock(Block):
    """
    DEPRECATED: Use FileParserBlock with file_type=FileType.PDF instead.
    This block will be removed in a future version.
    """

    # There is a mypy bug with Literal. Without the type: ignore, mypy will raise an error:
    # Parameter 1 of Literal[...] cannot be of type "Any"
    block_type: Literal[BlockType.PDF_PARSER] = BlockType.PDF_PARSER  # type: ignore

    file_url: str
    json_schema: dict[str, Any] | None = None
    schema_validation_max_attempts: ClassVar[int] = SCHEMA_VALIDATION_MAX_ATTEMPTS

    TEMPLATABLE_FIELDS: ClassVar[frozenset[str]] = frozenset({"file_url"})

    def get_all_parameters(
        self,
        workflow_run_id: str,
    ) -> list[PARAMETER_TYPE]:
        workflow_run_context = self.get_workflow_run_context(workflow_run_id)
        if self.file_url and workflow_run_context.has_parameter(self.file_url):
            return [workflow_run_context.get_parameter(self.file_url)]
        return []

    def format_potential_template_parameters(self, workflow_run_context: WorkflowRunContext) -> None:
        self.file_url = self.render_templatable_field("file_url", self.file_url, workflow_run_context)

        self._apply_workflow_system_prompt(workflow_run_context)

    async def execute(
        self,
        workflow_run_id: str,
        workflow_run_block_id: str,
        organization_id: str | None = None,
        browser_session_id: str | None = None,
        **kwargs: dict,
    ) -> BlockResult:
        workflow_run_context = self.get_workflow_run_context(workflow_run_id)
        if (
            self.file_url
            and workflow_run_context.has_parameter(self.file_url)
            and workflow_run_context.has_value(self.file_url)
        ):
            file_url_parameter_value = workflow_run_context.get_value(self.file_url)
            if file_url_parameter_value:
                LOG.info(
                    "PDFParserBlock File URL is parameterized, using parameter value",
                    file_url_parameter_value=file_url_parameter_value,
                    file_url_parameter_key=self.file_url,
                )
                self.file_url = file_url_parameter_value
            else:
                # Absent optional parameter: fail on the empty URL rather than
                # treating the literal parameter key as a URL.
                self.file_url = ""

        try:
            self.format_potential_template_parameters(workflow_run_context)
        except Exception as e:
            return await self._template_format_failure_result(
                e,
                f"Failed to format jinja template: {str(e)}",
                workflow_run_context,
                workflow_run_id,
                workflow_run_block_id,
                organization_id,
            )

        try:
            context = skyvern_context.current()
            run_id = context.run_id if context and context.run_id else workflow_run_id
            file_path = await resolve_local_or_download_file(self.file_url, run_id, organization_id=organization_id)
        except Exception as e:
            return await self.build_block_result(
                success=False,
                failure_reason=f"Failed to download or validate file: {str(e)}",
                output_parameter_value=None,
                status=BlockStatus.failed,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )

        try:
            extracted_text = await _run_blocking_parse_step(
                "PDF text extraction",
                self.file_url,
                extract_pdf_file,
                file_path,
                file_identifier=self.file_url,
            )
        except FileParseTimeout as e:
            return await self.build_block_result(
                success=False,
                failure_reason=str(e),
                output_parameter_value=None,
                status=BlockStatus.failed,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )
        except PDFParsingError:
            return await self.build_block_result(
                success=False,
                failure_reason="Failed to parse PDF file",
                output_parameter_value=None,
                status=BlockStatus.failed,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )

        if not self.json_schema:
            self.json_schema = _default_structured_output_schema("Information extracted from the text")
        schema_to_use = self.json_schema
        assert schema_to_use is not None
        if not validate_schema(schema_to_use):
            return await self.build_block_result(
                success=False,
                failure_reason="File parser JSON schema is invalid.",
                output_parameter_value=None,
                status=BlockStatus.failed,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )

        llm_prompt = prompt_engine.load_prompt(
            "extract-information-from-file-text", extracted_text_content=extracted_text, json_schema=schema_to_use
        )

        llm_response: dict[str, Any] | list | str | None = None
        prompt_for_attempt = llm_prompt
        for attempt in range(self.schema_validation_max_attempts):
            try:
                llm_response = await get_org_aware_primary_llm_api_handler(default=app.LLM_API_HANDLER)(
                    prompt=prompt_for_attempt,
                    prompt_name="extract-information-from-file-text",
                    # Schema validation must inspect the raw parsed root; dict coercion can hide wrong-root responses.
                    force_dict=False,
                    system_prompt=self.workflow_system_prompt,
                    workflow_run_block_id=workflow_run_block_id,
                    organization_id=organization_id,
                )
            except (InvalidLLMResponseFormat, InvalidLLMResponseType) as e:
                failure_reason = _llm_response_format_failure_reason(e)
                will_retry = attempt + 1 < self.schema_validation_max_attempts
                LOG.warning(
                    "PDFParserBlock extraction LLM response failed response-format validation",
                    file_url=self.file_url,
                    attempt=attempt + 1,
                    max_attempts=self.schema_validation_max_attempts,
                    will_retry=will_retry,
                    error_type=type(e).__name__,
                    schema_type=schema_to_use.get("type"),
                )
                if not will_retry:
                    return await self.build_block_result(
                        success=False,
                        failure_reason=failure_reason,
                        output_parameter_value=None,
                        status=BlockStatus.failed,
                        workflow_run_block_id=workflow_run_block_id,
                        organization_id=organization_id,
                    )
                prompt_for_attempt = _build_schema_validation_retry_prompt(llm_prompt, failure_reason)
                continue

            schema_validation_failure = FileParserBlock._validate_ai_response_against_json_schema(
                llm_response,
                schema_to_use,
            )
            if not schema_validation_failure:
                break

            is_schema_configuration_failure = _is_schema_configuration_failure(schema_validation_failure)
            will_retry = attempt + 1 < self.schema_validation_max_attempts and not is_schema_configuration_failure
            LOG.warning(
                "PDFParserBlock extraction LLM response failed schema validation",
                file_url=self.file_url,
                attempt=attempt + 1,
                max_attempts=self.schema_validation_max_attempts,
                will_retry=will_retry,
                failure_reason=schema_validation_failure,
                schema_type=schema_to_use.get("type"),
            )
            if not will_retry:
                return await self.build_block_result(
                    success=False,
                    failure_reason=schema_validation_failure,
                    output_parameter_value=None,
                    status=BlockStatus.failed,
                    workflow_run_block_id=workflow_run_block_id,
                    organization_id=organization_id,
                )
            prompt_for_attempt = _build_schema_validation_retry_prompt(
                llm_prompt,
                schema_validation_failure,
            )

        # Record the parsed data
        await self.record_output_parameter_value(workflow_run_context, workflow_run_id, llm_response)
        return await self.build_block_result(
            success=True,
            failure_reason=None,
            output_parameter_value=llm_response,
            status=BlockStatus.completed,
            workflow_run_block_id=workflow_run_block_id,
            organization_id=organization_id,
        )


class WaitBlock(Block):
    # There is a mypy bug with Literal. Without the type: ignore, mypy will raise an error:
    # Parameter 1 of Literal[...] cannot be of type "Any"
    block_type: Literal[BlockType.WAIT] = BlockType.WAIT  # type: ignore

    wait_sec: int
    parameters: list[PARAMETER_TYPE] = []

    def get_all_parameters(
        self,
        workflow_run_id: str,
    ) -> list[PARAMETER_TYPE]:
        return self.parameters

    async def execute(
        self,
        workflow_run_id: str,
        workflow_run_block_id: str,
        organization_id: str | None = None,
        browser_session_id: str | None = None,
        **kwargs: dict,
    ) -> BlockResult:
        # TODO: we need to support to interrupt the sleep when the workflow run failed/cancelled/terminated
        await app.DATABASE.observer.update_workflow_run_block(
            workflow_run_block_id=workflow_run_block_id,
            organization_id=organization_id,
            wait_sec=self.wait_sec,
        )
        LOG.info(
            "Going to pause the workflow for a while",
            second=self.wait_sec,
            workflow_run_id=workflow_run_id,
        )
        await asyncio.sleep(self.wait_sec)
        workflow_run_context = self.get_workflow_run_context(workflow_run_id)
        result_dict = {"success": True}
        await self.record_output_parameter_value(workflow_run_context, workflow_run_id, result_dict)
        return await self.build_block_result(
            success=True,
            failure_reason=None,
            output_parameter_value=result_dict,
            status=BlockStatus.completed,
            workflow_run_block_id=workflow_run_block_id,
            organization_id=organization_id,
        )


class HumanInteractionBlock(BaseTaskBlock):
    """
    A block for human/agent interaction.

    For the first pass at this, the implicit behaviour is that the user is given a single binary
    choice (a go//no-go).

    If the human:
      - chooses positively, the workflow continues
      - chooses negatively, the workflow is terminated
      - does not respond within the timeout period, the workflow terminates
    """

    # There is a mypy bug with Literal. Without the type: ignore, mypy will raise an error:
    # Parameter 1 of Literal[...] cannot be of type "Any"
    block_type: Literal[BlockType.HUMAN_INTERACTION] = BlockType.HUMAN_INTERACTION  # type: ignore

    instructions: str = "Please review and approve or reject to continue the workflow."
    positive_descriptor: str = "Approve"
    negative_descriptor: str = "Reject"
    timeout_seconds: int = 60 * 60 * 2  # two hours

    # email options
    sender: str = "hello@skyvern.com"
    recipients: list[str] = []
    subject: str = "Human interaction required for workflow run"
    body: str = "Your interaction is required for a workflow run!"

    TEMPLATABLE_FIELDS: ClassVar[frozenset[str]] = frozenset(
        {
            "body",
            "instructions",
            "negative_descriptor",
            "positive_descriptor",
            "recipients",
            "subject",
        }
    )

    def format_potential_template_parameters(self, workflow_run_context: WorkflowRunContext) -> None:
        super().format_potential_template_parameters(workflow_run_context)

        self.instructions = self.render_templatable_field("instructions", self.instructions, workflow_run_context)

        self.body = self.render_templatable_field("body", self.body, workflow_run_context)

        self.subject = self.render_templatable_field("subject", self.subject, workflow_run_context)

        formatted: list[str] = []
        for recipient in self.recipients:
            formatted.append(self.render_templatable_field("recipients", recipient, workflow_run_context))

        self.recipients = formatted

        self.negative_descriptor = self.render_templatable_field(
            "negative_descriptor", self.negative_descriptor, workflow_run_context
        )

        self.positive_descriptor = self.render_templatable_field(
            "positive_descriptor", self.positive_descriptor, workflow_run_context
        )

    async def execute(
        self,
        workflow_run_id: str,
        workflow_run_block_id: str,
        organization_id: str | None = None,
        browser_session_id: str | None = None,
        **kwargs: dict,
    ) -> BlockResult:
        # avoid circular import
        from skyvern.forge.sdk.workflow.models.workflow import WorkflowRunStatus  # noqa: PLC0415

        workflow_run_context = self.get_workflow_run_context(workflow_run_id)

        try:
            self.format_potential_template_parameters(workflow_run_context)
        except Exception as e:
            return await self._template_format_failure_result(
                e,
                f"Failed to format jinja template: {str(e)}",
                workflow_run_context,
                workflow_run_id,
                workflow_run_block_id,
                organization_id,
            )

        await app.DATABASE.observer.update_workflow_run_block(
            workflow_run_block_id=workflow_run_block_id,
            organization_id=organization_id,
            recipients=self.recipients,
            subject=self.subject,
            body=self.body,
            instructions=self.instructions,
            positive_descriptor=self.positive_descriptor,
            negative_descriptor=self.negative_descriptor,
        )

        LOG.info(
            "Pausing workflow for human interaction",
            workflow_run_id=workflow_run_id,
            recipients=self.recipients,
            timeout=self.timeout_seconds,
            browser_session_id=browser_session_id,
        )

        await app.DATABASE.workflow_runs.update_workflow_run(
            workflow_run_id=workflow_run_id,
            status=WorkflowRunStatus.paused,
        )

        workflow_run = await app.DATABASE.workflow_runs.get_workflow_run(
            workflow_run_id=workflow_run_id,
            organization_id=organization_id,
        )

        if not workflow_run:
            return await self.build_block_result(
                success=False,
                failure_reason="Workflow run not found",
                output_parameter_value=None,
                status=BlockStatus.failed,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )

        app_url = f"{settings.SKYVERN_APP_URL}/runs/{workflow_run_id}/overview"
        body = f"{self.body}\n\nKindly visit {app_url}\n\n{self.instructions}\n\n"
        if browser_session_id:
            browser_session_url = f"{settings.SKYVERN_APP_URL}/browser-session/{browser_session_id}"
            body += f"To interact with the browser session directly, visit {browser_session_url}\n\n"
        subject = f"{self.subject} - Workflow Run ID: {workflow_run_id}"

        try:
            await email.send(
                body=body,
                sender=self.sender,
                subject=subject,
                recipients=self.recipients,
            )

            email_success = True
            email_failure_reason = None
        except Exception as ex:
            LOG.error(
                "Failed to send human interaction email",
                workflow_run_id=workflow_run_id,
                error=str(ex),
                browser_session_id=browser_session_id,
            )
            email_success = False
            email_failure_reason = str(ex)

        if not email_success:
            return await self.build_block_result(
                success=False,
                failure_reason=f"Failed to send human interaction email: {email_failure_reason or 'email failed'}",
                output_parameter_value=None,
                status=BlockStatus.failed,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )

        # Wait for the timeout_seconds or until the workflow run status changes from paused
        start_time = asyncio.get_event_loop().time()
        check_interval = 5  # Check every 5 seconds
        log_that_we_are_waiting = True
        log_wait = 0

        while True:
            if not log_that_we_are_waiting:
                log_wait += check_interval
                if log_wait >= 60:  # Log every 1 minute
                    log_that_we_are_waiting = True
                    log_wait = 0

            elapsed_time_seconds = asyncio.get_event_loop().time() - start_time

            if log_that_we_are_waiting:
                LOG.info(
                    "Waiting for human interaction...",
                    workflow_run_id=workflow_run_id,
                    elapsed_time_seconds=elapsed_time_seconds,
                    timeout_seconds=self.timeout_seconds,
                    browser_session_id=browser_session_id,
                )
                log_that_we_are_waiting = False

            # Check if timeout_seconds has elapsed
            if elapsed_time_seconds >= self.timeout_seconds:
                LOG.info(
                    "Human Interaction block timeout_seconds reached",
                    workflow_run_id=workflow_run_id,
                    elapsed_time_seconds=elapsed_time_seconds,
                    browser_session_id=browser_session_id,
                )

                workflow_run_context = self.get_workflow_run_context(workflow_run_id)
                success = False
                reason = "Timeout elapsed with no human interaction"
                result_dict = {"success": success, "reason": reason}

                await self.record_output_parameter_value(workflow_run_context, workflow_run_id, result_dict)

                return await self.build_block_result(
                    success=success,
                    failure_reason=reason,
                    output_parameter_value=result_dict,
                    status=BlockStatus.timed_out,
                    workflow_run_block_id=workflow_run_block_id,
                    organization_id=organization_id,
                )

            workflow_run = await app.DATABASE.workflow_runs.get_workflow_run(
                workflow_run_id=workflow_run_id,
                organization_id=organization_id,
            )

            if workflow_run and workflow_run.status != WorkflowRunStatus.paused:
                LOG.info(
                    "Workflow run status changed from paused",
                    workflow_run_id=workflow_run_id,
                    new_status=workflow_run.status,
                    browser_session_id=browser_session_id,
                )

                workflow_run_context = self.get_workflow_run_context(workflow_run_id)
                result_dict = {"success": True, "reason": f"status_changed:{workflow_run.status}"}

                await self.record_output_parameter_value(workflow_run_context, workflow_run_id, result_dict)

                return await self.build_block_result(
                    success=True,
                    failure_reason=None,
                    output_parameter_value=result_dict,
                    status=BlockStatus.completed,
                    workflow_run_block_id=workflow_run_block_id,
                    organization_id=organization_id,
                )

            await asyncio.sleep(min(check_interval, self.timeout_seconds - elapsed_time_seconds))


class ValidationBlock(BaseTaskBlock):
    # There is a mypy bug with Literal. Without the type: ignore, mypy will raise an error:
    # Parameter 1 of Literal[...] cannot be of type "Any"
    block_type: Literal[BlockType.VALIDATION] = BlockType.VALIDATION  # type: ignore

    # Opt-in: when True, the validation prompt excludes the page DOM/URL/screenshots and
    # evaluates the criterion against durable data only (prior block outputs, workflow inputs).
    # Default False keeps today's page-aware behavior.
    without_page_information: bool = False

    def get_all_parameters(
        self,
        workflow_run_id: str,
    ) -> list[PARAMETER_TYPE]:
        return self.parameters

    async def execute(
        self,
        workflow_run_id: str,
        workflow_run_block_id: str,
        organization_id: str | None = None,
        browser_session_id: str | None = None,
        **kwargs: dict,
    ) -> BlockResult:
        task_order, _ = await self.get_task_order(workflow_run_id, 0)
        is_first_task = task_order == 0
        if is_first_task:
            return await self.build_block_result(
                success=False,
                failure_reason="Validation block should not be the first block",
                output_parameter_value=None,
                status=BlockStatus.terminated,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )

        context = skyvern_context.current()
        prev_without_page_information = context.validation_without_page_information if context else False
        if context:
            context.validation_without_page_information = self.without_page_information
        try:
            return await super().execute(
                workflow_run_id=workflow_run_id,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
                kwargs=kwargs,
            )
        finally:
            if context:
                context.validation_without_page_information = prev_without_page_information


class ActionBlock(BaseTaskBlock):
    # There is a mypy bug with Literal. Without the type: ignore, mypy will raise an error:
    # Parameter 1 of Literal[...] cannot be of type "Any"
    block_type: Literal[BlockType.ACTION] = BlockType.ACTION  # type: ignore

    selector: str | None = None
    ai_fallback: AIFallbackMode = AIFallbackMode.FALLBACK


class NavigationBlock(BaseTaskBlock):
    # There is a mypy bug with Literal. Without the type: ignore, mypy will raise an error:
    # Parameter 1 of Literal[...] cannot be of type "Any"
    block_type: Literal[BlockType.NAVIGATION] = BlockType.NAVIGATION  # type: ignore

    navigation_goal: str


class ExtractionBlock(ParquetExportMixin, BaseTaskBlock):
    # There is a mypy bug with Literal. Without the type: ignore, mypy will raise an error:
    # Parameter 1 of Literal[...] cannot be of type "Any"
    block_type: Literal[BlockType.EXTRACTION] = BlockType.EXTRACTION  # type: ignore

    data_extraction_goal: str
    include_extracted_text: bool = False

    # Export the extracted data as a Parquet file -- an output option of this block
    # rather than a separate Data Export block. See ParquetExportMixin and
    # skyvern.forge.sdk.workflow.models.data_export_block.DataExportBlock.
    export_enabled: bool = False
    export_data_schema: dict[str, Any] | None = None
    export_file_name: str | None = None
    export_records: str | None = None

    TEMPLATABLE_FIELDS: ClassVar[frozenset[str]] = frozenset({"export_file_name", "export_records"})

    def format_potential_template_parameters(self, workflow_run_context: WorkflowRunContext) -> None:
        super().format_potential_template_parameters(workflow_run_context)
        # Export fields are only meaningful when export is on; rendering them
        # unconditionally would fail an otherwise-fine run over a stale
        # export_records left over from before export was disabled (or a
        # deleted-block reference), since the strict Jinja env raises on an
        # undefined binding regardless of the missing-variable preflight.
        if not self.export_enabled:
            return
        if self.export_file_name:
            self.export_file_name = self.render_templatable_field(
                "export_file_name", self.export_file_name, workflow_run_context
            )
        if self.export_records:
            self.export_records = self.render_templatable_field(
                "export_records",
                self.export_records,
                workflow_run_context,
                env=jinja_json_finalize_required_binding_env,
                skip_missing_variable_preflight=True,
            )

    async def execute(
        self,
        workflow_run_id: str,
        workflow_run_block_id: str,
        organization_id: str | None = None,
        browser_session_id: str | None = None,
        **kwargs: Any,
    ) -> BlockResult:
        result = await super().execute(
            workflow_run_id=workflow_run_id,
            workflow_run_block_id=workflow_run_block_id,
            organization_id=organization_id,
            browser_session_id=browser_session_id,
            **kwargs,
        )
        if not self.export_enabled or not result.success or not isinstance(result.output_parameter_value, dict):
            return result

        workflow_run_context = self.get_workflow_run_context(workflow_run_id)
        try:
            if not self.export_data_schema:
                raise ParquetExportError("export_data_schema is required when export is enabled")
            if self.export_records:
                records = self.parse_export_records(self.export_records)
            else:
                extracted = result.output_parameter_value.get("extracted_information")
                if isinstance(extracted, list):
                    records = extracted
                elif isinstance(extracted, Mapping):
                    records = [extracted]
                elif extracted is None:
                    # Nothing to export is a legitimate outcome (e.g. an optional
                    # extraction that found nothing) -- an honest empty file, not
                    # a fabricated all-null row.
                    records = []
                else:
                    raise ParquetExportError(
                        "extracted_information must be an object or a list of objects to export, got "
                        f"{type(extracted).__name__}"
                    )
            export_output = await self.write_parquet_export(
                records=records,
                data_schema=self.export_data_schema,
                file_name=self.export_file_name,
                label=self.label,
                workflow_run_context=workflow_run_context,
                workflow_run_id=workflow_run_id,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id or workflow_run_context.organization_id,
            )
        except ParquetExportError as exc:
            # Reuse the standard failure path (records output + error_codes +
            # secret redaction) rather than a bare failed result: the base
            # execute() already recorded the successful extraction's output,
            # so without this a downstream block under continue_on_failure
            # would read stale, pre-export-failure output as if nothing had
            # gone wrong.
            return await self._template_format_failure_result(
                exc,
                str(exc),
                workflow_run_context,
                workflow_run_id,
                workflow_run_block_id,
                organization_id,
            )

        merged_output = {**result.output_parameter_value, "export": export_output}
        await self.record_output_parameter_value(workflow_run_context, workflow_run_id, merged_output)
        return await self.build_block_result(
            success=True,
            failure_reason=None,
            output_parameter_value=merged_output,
            status=result.status,
            workflow_run_block_id=workflow_run_block_id,
            organization_id=organization_id,
        )


class LoginBlock(BaseTaskBlock):
    # There is a mypy bug with Literal. Without the type: ignore, mypy will raise an error:
    # Parameter 1 of Literal[...] cannot be of type "Any"
    block_type: Literal[BlockType.LOGIN] = BlockType.LOGIN  # type: ignore

    # Opt out of reusing the credential's saved browser profile so the run logs in fresh and
    # the captured session persists via the normal path (a reused profile is loaded read-only).
    skip_saved_profile: bool = False

    def preflight_failure_reason(
        self, workflow_run_context: WorkflowRunContext, workflow_run: WorkflowRun
    ) -> str | None:
        """An at-will credential parameter that resolved to null leaves a login block with nothing
        to sign in with -- unless the run is reusing a saved browser profile, whose session already
        carries the login (skip_saved_profile opts a block out of that reuse, e.g. a credential
        re-save that must log in fresh). Name the parameter instead of sending the agent at an
        empty login form."""
        if not self.skip_saved_profile and workflow_run.browser_profile_id:
            return None
        unresolved_keys: list[str] = []
        for parameter in self.parameters:
            if parameter.parameter_type.is_login_credential():
                return None
            if (
                not isinstance(parameter, WorkflowParameter)
                or parameter.workflow_parameter_type != WorkflowParameterType.CREDENTIAL_ID
            ):
                continue
            if workflow_run_context.get_resolved_credential_parameter_id(parameter.key):
                return None
            if workflow_run_context.values.get(parameter.key):
                return None
            unresolved_keys.append(parameter.key)

        if not unresolved_keys:
            return None
        return (
            f"No credential was provided for the '{unresolved_keys[0]}' parameter, so this login block "
            "has nothing to sign in with. Send a credential id under that exact parameter key in the run request."
        )


class FileDownloadBlock(BaseTaskBlock, FileDestinationBlock):
    # There is a mypy bug with Literal. Without the type: ignore, mypy will raise an error:
    # Parameter 1 of Literal[...] cannot be of type "Any"
    block_type: Literal[BlockType.FILE_DOWNLOAD] = BlockType.FILE_DOWNLOAD  # type: ignore
    download_target: FileDownloadTarget = FileDownloadTarget.WEBSITE

    def get_all_parameters(self, workflow_run_id: str) -> list[PARAMETER_TYPE]:
        parameters = super().get_all_parameters(workflow_run_id)
        workflow_run_context = self.get_workflow_run_context(workflow_run_id)
        existing_keys = {p.key for p in parameters}
        for destination_param in self._get_destination_parameters(workflow_run_context):
            if destination_param.key not in existing_keys:
                parameters.append(destination_param)
                existing_keys.add(destination_param.key)
        return parameters

    async def _register_authenticated_google_drive_download(
        self,
        *,
        organization_id: str,
        workflow_run_id: str,
        workflow_run_block_id: str,
        run_download_id: str,
        filename: str,
    ) -> list[FileInfo] | None:
        skipped_files: set[str] = set()
        try:
            async with asyncio.timeout(SAVE_DOWNLOADED_FILES_TIMEOUT):
                await app.STORAGE.save_downloaded_files(
                    organization_id=organization_id,
                    run_id=run_download_id,
                )
        except asyncio.TimeoutError:
            LOG.warning(
                "Timeout saving authenticated Google Drive download; workflow finalization will retry",
                workflow_run_id=workflow_run_id,
                workflow_run_block_id=workflow_run_block_id,
            )
            return None
        except DownloadSaveIncompleteError as exc:
            skipped_files = set(exc.skipped_files)
            LOG.warning(
                "Storage partially saved authenticated Google Drive download; workflow finalization will retry",
                workflow_run_id=workflow_run_id,
                workflow_run_block_id=workflow_run_block_id,
                skipped_file_count=len(skipped_files),
            )
        except Exception:
            LOG.warning(
                "Failed to save authenticated Google Drive download; workflow finalization will retry",
                workflow_run_id=workflow_run_id,
                workflow_run_block_id=workflow_run_block_id,
                exc_info=True,
            )
            return None

        try:
            async with asyncio.timeout(GET_DOWNLOADED_FILES_TIMEOUT):
                downloaded_files = await app.STORAGE.get_downloaded_files(
                    organization_id=organization_id,
                    run_id=run_download_id,
                )
        except asyncio.TimeoutError:
            LOG.warning(
                "Timeout reading back authenticated Google Drive download",
                workflow_run_id=workflow_run_id,
                workflow_run_block_id=workflow_run_block_id,
            )
            return None
        except Exception:
            LOG.warning(
                "Failed to read back authenticated Google Drive download",
                workflow_run_id=workflow_run_id,
                workflow_run_block_id=workflow_run_block_id,
                exc_info=True,
            )
            return None
        return None if filename in skipped_files else downloaded_files

    async def _download_authenticated_google_drive_source(
        self,
        *,
        workflow_run_context: WorkflowRunContext,
        organization_id: str | None,
        workflow_run_id: str,
        run_download_id: str,
        workflow_run_block_id: str,
        download_files_path: str,
    ) -> tuple[str, list[FileInfo] | None, str] | None:
        if not self.url or not self.google_credential_id:
            return None

        source_url = self.render_templatable_field(
            "url",
            self.url,
            workflow_run_context,
        )
        try:
            file_reference = google_drive_service.extract_file_reference(source_url)
        except ValueError:
            return None
        file_id = file_reference.file_id

        org_id = organization_id or workflow_run_context.organization_id
        if not org_id:
            raise ValueError("organization_id is required for authenticated Google Drive downloads")

        formatted_credential_id = self.render_templatable_field(
            "google_credential_id",
            self.google_credential_id,
            workflow_run_context,
        )
        google_credential_id = (
            workflow_run_context.get_original_secret_value_or_none(formatted_credential_id) or formatted_credential_id
        )
        if not google_credential_id:
            raise ValueError("Google credential id is required")

        google_credentials = await app.AGENT_FUNCTION.get_google_workspace_credentials(
            organization_id=org_id,
            credential_id=google_credential_id,
            required_scopes=list(google_oauth_service.GOOGLE_DRIVE_SCOPES),
        )
        if not google_credentials or not google_credentials.token:
            raise ValueError("Google Drive credential is not connected or is missing required scopes")

        try:
            file_path = await google_drive_service.download_file(
                access_token=google_credentials.token,
                file_id=file_id,
                resource_key=file_reference.resource_key,
                output_dir=download_files_path,
                max_size_mb=settings.MAX_HTTP_DOWNLOAD_FILE_SIZE // (1024 * 1024),
            )
        except google_drive_service.GoogleDriveNativeDocumentError:
            LOG.info(
                "Google Drive source requires browser download",
                block_label=self.label,
                file_id=file_id,
            )
            return None

        downloaded_files = await self._register_authenticated_google_drive_download(
            organization_id=org_id,
            workflow_run_id=workflow_run_id,
            workflow_run_block_id=workflow_run_block_id,
            run_download_id=run_download_id,
            filename=Path(file_path).name,
        )
        filename = Path(file_path).name
        matching_files: list[FileInfo] | None = None
        if downloaded_files is not None:
            checksum = calculate_sha256_for_file(file_path)
            filename_matches = [file_info for file_info in downloaded_files if file_info.filename == filename]
            checksum_matches = [file_info for file_info in filename_matches if file_info.checksum == checksum]
            if checksum_matches:
                matching_files = checksum_matches
            else:
                checksumless_matches = [file_info for file_info in filename_matches if file_info.checksum is None]
                matching_files = checksumless_matches if len(checksumless_matches) == 1 else []
        if matching_files == []:
            raise RuntimeError(DOWNLOAD_BINDING_FAILURE_REASON)
        return (
            file_path,
            matching_files[:1] if matching_files is not None else None,
            google_credentials.token,
        )

    async def execute(
        self,
        workflow_run_id: str,
        workflow_run_block_id: str,
        organization_id: str | None = None,
        browser_session_id: str | None = None,
        **kwargs: dict,
    ) -> BlockResult:
        download_files_path = ""
        run_download_id = workflow_run_id
        direct_google_access_token: str | None = None
        baseline_unknown = False
        pre_names: set[str] = set()
        pre_hashes: dict[str, str] = {}
        pre_mtimes: dict[str, int] = {}
        colliding_normalized_names: set[str] = set()
        if self.download_target != FileDownloadTarget.WEBSITE:
            # Fail fast on a misconfigured destination before running the (expensive) browser
            # download, so a missing required field does not waste a download that cannot be delivered.
            early_storage_type = FileStorageType(self.download_target.value)
            early_context = self.get_workflow_run_context(workflow_run_id)
            try:
                early_missing_parameters = self._validate_destination_fields(early_storage_type)
            except UnsupportedStorageTypeError as e:
                await self.record_output_parameter_value(early_context, workflow_run_id, None)
                return await self.build_block_result(
                    success=False,
                    failure_reason=f"Failed to send downloaded file(s) to {early_storage_type}: {e}",
                    output_parameter_value=None,
                    status=BlockStatus.failed,
                    workflow_run_block_id=workflow_run_block_id,
                    organization_id=organization_id,
                )
            if early_missing_parameters:
                await self.record_output_parameter_value(early_context, workflow_run_id, None)
                return await self.build_block_result(
                    success=False,
                    failure_reason=(
                        f"Required block values are missing in the FileDownloadBlock (label: {self.label}): "
                        f"{', '.join(early_missing_parameters)}"
                    ),
                    output_parameter_value=None,
                    status=BlockStatus.failed,
                    workflow_run_block_id=workflow_run_block_id,
                    organization_id=organization_id,
                )
            context = skyvern_context.current()
            run_download_id = resolve_run_download_id(context, fallback_run_id=workflow_run_id) or workflow_run_id
            download_files_path = str(get_path_for_workflow_download_directory(run_download_id).absolute())
            try:
                pre_download_filenames = os.listdir(download_files_path)
            except FileNotFoundError:
                pre_download_filenames = []
            except OSError:
                baseline_unknown = True
                pre_download_filenames = []
            for filename in pre_download_filenames:
                local_file = os.path.join(download_files_path, filename)
                if not os.path.isfile(local_file):
                    continue
                normalized_filename = unicodedata.normalize("NFC", filename)
                if normalized_filename in pre_names:
                    colliding_normalized_names.add(normalized_filename)
                pre_names.add(normalized_filename)
                try:
                    pre_hashes[normalized_filename] = calculate_sha256_for_file(local_file)
                    pre_mtimes[filename] = os.stat(local_file).st_mtime_ns
                except OSError:
                    continue
            if baseline_unknown:
                # Fail fast before the download: the baseline scan failed, so delivery would be
                # refused anyway (to avoid leaking earlier files) — do not download customer data.
                await self.record_output_parameter_value(early_context, workflow_run_id, None)
                return await self.build_block_result(
                    success=False,
                    failure_reason=(
                        "Could not establish the pre-download baseline; refusing to deliver files to "
                        f"{early_storage_type} to avoid leaking earlier files."
                    ),
                    output_parameter_value=None,
                    status=BlockStatus.failed,
                    workflow_run_block_id=workflow_run_block_id,
                    organization_id=organization_id,
                )

        direct_download: tuple[str, list[FileInfo] | None, str] | None = None
        if self.download_target != FileDownloadTarget.WEBSITE:
            workflow_run_context = self.get_workflow_run_context(workflow_run_id)
            try:
                direct_download = await self._download_authenticated_google_drive_source(
                    workflow_run_context=workflow_run_context,
                    organization_id=organization_id,
                    workflow_run_id=workflow_run_id,
                    run_download_id=run_download_id,
                    workflow_run_block_id=workflow_run_block_id,
                    download_files_path=download_files_path,
                )
            except Exception as exc:
                LOG.exception(
                    "FileDownloadBlock failed to download authenticated Google Drive source",
                    block_label=self.label,
                )
                failure_reason = f"Failed to download file from Google Drive: {exc}"
                await self.record_output_parameter_value(workflow_run_context, workflow_run_id, None)
                return await self.build_block_result(
                    success=False,
                    failure_reason=failure_reason,
                    output_parameter_value=None,
                    status=BlockStatus.failed,
                    workflow_run_block_id=workflow_run_block_id,
                    organization_id=organization_id,
                )

        if direct_download is None:
            result = await super().execute(
                workflow_run_id=workflow_run_id,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
                browser_session_id=browser_session_id,
                **kwargs,
            )
            if self.download_target == FileDownloadTarget.WEBSITE:
                return result
            if not result.success:
                return result
        else:
            file_path, downloaded_files, direct_google_access_token = direct_download
            output_parameter_value = bind_downloaded_files_to_output(
                {"file_name": Path(file_path).name},
                downloaded_files or [],
            )
            workflow_run_context = self.get_workflow_run_context(workflow_run_id)
            await self.record_output_parameter_value(
                workflow_run_context,
                workflow_run_id,
                output_parameter_value,
            )
            result = await self.build_block_result(
                success=True,
                failure_reason=None,
                output_parameter_value=output_parameter_value,
                status=BlockStatus.completed,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )

        storage_type = FileStorageType(self.download_target.value)
        workflow_run_context = self.get_workflow_run_context(workflow_run_id)
        try:
            self._format_destination_template_parameters(workflow_run_context)
            max_file_count = (
                MAX_UPLOAD_FILE_COUNT
                if storage_type in {FileStorageType.S3, FileStorageType.GOOGLE_DRIVE, FileStorageType.SFTP}
                else AZURE_BLOB_STORAGE_MAX_UPLOAD_FILE_COUNT
            )
            try:
                post_download_filenames = os.listdir(download_files_path)
            except FileNotFoundError:
                post_download_filenames = []
            except OSError as e:
                # Symmetric with the pre-download baseline: a real post-download scan failure (not a
                # missing directory) must fail closed rather than report success without delivering.
                raise RuntimeError("Could not scan the download directory after the download completed") from e

            files_to_upload: list[str] = []
            for filename in post_download_filenames:
                local_file = os.path.join(download_files_path, filename)
                try:
                    if not os.path.isfile(local_file):
                        if os.path.isdir(local_file):
                            LOG.warning("FileDownloadBlock skipping directory", file=filename)
                        continue
                    normalized_filename = unicodedata.normalize("NFC", filename)
                except OSError:
                    continue

                if normalized_filename in colliding_normalized_names:
                    # Multiple byte-distinct directory entries normalized to this name at baseline,
                    # so neither content hash nor write time can be attributed to a single entry;
                    # fail closed rather than risk delivering an earlier block's file.
                    continue

                if normalized_filename not in pre_names:
                    files_to_upload.append(local_file)
                    continue
                if normalized_filename not in pre_hashes:
                    continue
                try:
                    current_hash = calculate_sha256_for_file(local_file)
                except OSError:
                    continue
                if current_hash != pre_hashes[normalized_filename]:
                    files_to_upload.append(local_file)
                    continue
                # Identical content means hashing cannot distinguish a genuine re-download
                # from an earlier block's leftover; fall back to write time, keyed by the raw
                # directory-entry name so Unicode-normalization-equivalent names never share a
                # baseline. Deliver only if this block rewrote this exact entry in its own window.
                baseline_mtime_ns = pre_mtimes.get(filename)
                if baseline_mtime_ns is None:
                    continue
                try:
                    current_mtime_ns = os.stat(local_file).st_mtime_ns
                except OSError:
                    continue
                if current_mtime_ns > baseline_mtime_ns:
                    files_to_upload.append(local_file)

            if not files_to_upload:
                return result

            if len(files_to_upload) > max_file_count:
                raise ValueError(f"Too many scoped downloaded files to upload. Max: {max_file_count}")

            if files_to_upload and self.prompt and self.prompt.strip():
                candidate_count = len(files_to_upload)
                files_to_upload, selection_reasoning = await self._select_files_to_upload_with_prompt(
                    prompt=self.prompt,
                    files_to_upload=files_to_upload,
                    workflow_run_block_id=workflow_run_block_id,
                    organization_id=organization_id,
                )
                selected_count = len(files_to_upload)
                LOG.info(
                    "FileDownloadBlock prompt selection completed",
                    block_label=self.label,
                    candidate_count=candidate_count,
                    selected_count=selected_count,
                )

                if not files_to_upload:
                    LOG.warning(
                        "FileDownloadBlock prompt selected no files; treating as no-op",
                        block_label=self.label,
                        workflow_run_id=workflow_run_id,
                        workflow_run_block_id=workflow_run_block_id,
                        candidate_count=candidate_count,
                        selected_count=selected_count,
                        reasoning=selection_reasoning,
                    )
                    return result

            uploaded_uris = await self._dispatch_files_to_storage(
                storage_type=storage_type,
                files_to_upload=files_to_upload,
                workflow_run_id=workflow_run_id,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
                workflow_run_context=workflow_run_context,
                google_access_token=direct_google_access_token,
            )
        except Exception as e:
            LOG.exception(
                "FileDownloadBlock failed to send downloaded file(s)",
                block_label=self.label,
                storage_type=storage_type,
            )
            await self.record_output_parameter_value(workflow_run_context, workflow_run_id, None)
            return await self.build_block_result(
                success=False,
                failure_reason=f"Failed to send downloaded file(s) to {storage_type}: {e}",
                output_parameter_value=None,
                status=BlockStatus.failed,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )

        LOG.info(
            "FileDownloadBlock sent downloaded file(s) to customer storage",
            block_label=self.label,
            workflow_run_id=workflow_run_id,
            workflow_run_block_id=workflow_run_block_id,
            storage_type=storage_type,
            uploaded_file_count=len(uploaded_uris),
        )
        return result


class UrlBlock(BaseTaskBlock):
    # There is a mypy bug with Literal. Without the type: ignore, mypy will raise an error:
    # Parameter 1 of Literal[...] cannot be of type "Any"
    block_type: Literal[BlockType.GOTO_URL] = BlockType.GOTO_URL  # type: ignore
    url: str


class TaskV2Block(Block):
    # There is a mypy bug with Literal. Without the type: ignore, mypy will raise an error:
    # Parameter 1 of Literal[...] cannot be of type "Any"
    block_type: Literal[BlockType.TaskV2] = BlockType.TaskV2  # type: ignore
    prompt: str
    url: str | None = None
    totp_verification_url: str | None = None
    totp_identifier: str | None = None
    # These documented defaults must stay literals; reading the setting here would put an
    # environment value back into the published OpenAPI document.
    max_iterations: int = Field(
        default_factory=lambda: settings.MAX_ITERATIONS_PER_TASK_V2,
        json_schema_extra={"default": 50},
    )
    max_steps: int = Field(
        default_factory=lambda: settings.MAX_STEPS_PER_TASK_V2,
        json_schema_extra={"default": 25},
    )

    TEMPLATABLE_FIELDS: ClassVar[frozenset[str]] = frozenset(
        {
            "prompt",
            "totp_identifier",
            "totp_verification_url",
            "url",
        }
    )

    def _resolve_totp_identifier(self, workflow_run_context: WorkflowRunContext) -> str | None:
        if self.totp_identifier:
            return self.totp_identifier
        if workflow_run_context.credential_totp_identifiers:
            return next(iter(workflow_run_context.credential_totp_identifiers.values()), None)
        return None

    def get_all_parameters(
        self,
        workflow_run_id: str,
    ) -> list[PARAMETER_TYPE]:
        return []

    def format_potential_template_parameters(self, workflow_run_context: WorkflowRunContext) -> None:
        self.prompt = self.render_templatable_field("prompt", self.prompt, workflow_run_context)
        if self.url:
            self.url = self.render_templatable_field("url", self.url, workflow_run_context)

        if self.totp_identifier:
            self.totp_identifier = self.render_templatable_field(
                "totp_identifier", self.totp_identifier, workflow_run_context
            )

        if self.totp_verification_url:
            self.totp_verification_url = self.render_templatable_field(
                "totp_verification_url", self.totp_verification_url, workflow_run_context
            )
            self.totp_verification_url = prepend_scheme_and_validate_url(self.totp_verification_url)

        # Materialize the workflow-level workflow_system_prompt onto this block so
        # execute() can hand it off to the TaskV2 row verbatim.
        self._apply_workflow_system_prompt(workflow_run_context)

    async def execute(
        self,
        workflow_run_id: str,
        workflow_run_block_id: str,
        organization_id: str | None = None,
        browser_session_id: str | None = None,
        **kwargs: dict,
    ) -> BlockResult:
        from skyvern.forge.sdk.workflow.models.workflow import WorkflowRunStatus  # noqa: PLC0415
        from skyvern.services import task_v2_service  # noqa: PLC0415

        # Scope downloaded files to this block only.
        block_context = skyvern_context.current()
        if block_context:
            await capture_block_download_baseline(block_context, organization_id or "", workflow_run_id, self.label)

        workflow_run_context = self.get_workflow_run_context(workflow_run_id)

        # Simple template resolution - no complex dynamic resolution to prevent recursion
        try:
            self.format_potential_template_parameters(workflow_run_context)

            # Use the resolved values directly
            resolved_prompt = self.prompt
            resolved_url = self.url
            resolved_totp_identifier = self._resolve_totp_identifier(workflow_run_context)
            resolved_totp_verification_url = self.totp_verification_url

        except Exception as e:
            return await self._template_format_failure_result(
                e,
                f"Failed to format jinja template: {str(e)}",
                workflow_run_context,
                workflow_run_id,
                workflow_run_block_id,
                organization_id,
            )

        if not resolved_url:
            browser_state = app.BROWSER_MANAGER.get_for_workflow_run(workflow_run_id)
            if browser_state:
                page = await browser_state.get_working_page()
                if page:
                    current_url = await SkyvernFrame.get_url(frame=page)
                    if current_url != "about:blank":
                        resolved_url = current_url

        if not organization_id:
            raise ValueError("Running TaskV2Block requires organization_id")

        organization = await app.DATABASE.organizations.get_organization(organization_id)
        if not organization:
            raise ValueError(f"Organization not found {organization_id}")
        workflow_run = await app.DATABASE.workflow_runs.get_workflow_run(workflow_run_id, organization_id)
        if not workflow_run:
            raise ValueError(f"WorkflowRun not found {workflow_run_id} when running TaskV2Block")
        current_context = skyvern_context.current()
        download_lookup_run_id = (
            current_context.run_id if current_context and current_context.run_id else workflow_run_id
        )
        loop_internal_state = copy.deepcopy(current_context.loop_internal_state) if current_context else None
        try:
            # TaskV2Block child runs inherit the parent run's trigger_type so non-UI parents
            # don't silently drop flex-routing eligibility for their TaskV2 children.
            inherited_v2_trigger_type = current_context.trigger_type if current_context else None
            task_v2 = await task_v2_service.initialize_task_v2(
                organization=organization,
                user_prompt=resolved_prompt,
                user_url=resolved_url,
                parent_workflow_run_id=workflow_run_id,
                proxy_location=workflow_run.proxy_location,
                totp_identifier=resolved_totp_identifier,
                totp_verification_url=resolved_totp_verification_url,
                max_screenshot_scrolling_times=workflow_run.max_screenshot_scrolls,
                workflow_system_prompt=self.workflow_system_prompt,
                trigger_type=inherited_v2_trigger_type,
                # Pin the child to the parent's remote browser (CDP address + the
                # headers its handshake authenticates with) so a cloud-browser run
                # doesn't fall back to a fresh local Chrome.
                browser_address=workflow_run.browser_address,
                extra_http_headers=workflow_run.extra_http_headers,
                cdp_connect_headers=workflow_run.cdp_connect_headers,
            )
            await app.DATABASE.observer.update_task_v2(
                task_v2.observer_cruise_id, status=TaskV2Status.queued, organization_id=organization_id
            )
            if task_v2.workflow_run_id:
                await app.DATABASE.workflow_runs.update_workflow_run(
                    workflow_run_id=task_v2.workflow_run_id,
                    status=WorkflowRunStatus.queued,
                )
                await app.DATABASE.observer.update_workflow_run_block(
                    workflow_run_block_id=workflow_run_block_id,
                    organization_id=organization_id,
                    block_workflow_run_id=task_v2.workflow_run_id,
                )
        except Exception as e:
            LOG.exception("Failed to initialize or queue TaskV2", error=e)
            output_reason = f"Failed to initialize or queue TaskV2: {str(e)}"
            await self.record_output_parameter_value(
                workflow_run_context, workflow_run_id, {"failure_reason": output_reason}
            )
            return await self.build_block_result(
                success=False,
                failure_reason=output_reason,
                output_parameter_value=None,
                status=BlockStatus.failed,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )

        # run_task_v2 uses scoped() internally, so context is always restored
        # even if it raises. Its own exception handlers mark the task as
        # failed/terminated with proper status, so we let exceptions propagate
        # to the status-mapping logic below.
        task_v2 = await task_v2_service.run_task_v2(
            organization=organization,
            task_v2_id=task_v2.observer_cruise_id,
            request_id=None,
            max_steps_override=self.max_steps,
            max_iterations_override=self.max_iterations,
            browser_session_id=browser_session_id,
        )
        result_dict = None
        if task_v2:
            result_dict = task_v2.output

        # Determine block status from task status using module-level mapping
        block_status = TASKV2_TO_BLOCK_STATUS.get(task_v2.status, BlockStatus.failed)
        success = task_v2.status == TaskV2Status.completed
        failure_reason: str | None = None
        task_v2_workflow_run_id = task_v2.workflow_run_id
        if task_v2_workflow_run_id:
            task_v2_workflow_run = await app.DATABASE.workflow_runs.get_workflow_run(
                task_v2_workflow_run_id, organization_id
            )
            if task_v2_workflow_run:
                failure_reason = task_v2_workflow_run.failure_reason

        # If continue_on_failure is True, we treat the block as successful even if the task failed
        # This allows the workflow to continue execution despite this block's failure
        task_screenshot_artifacts = await app.WORKFLOW_SERVICE.get_recent_task_screenshot_artifacts(
            organization_id=organization_id,
            task_v2_id=task_v2.observer_cruise_id,
        )
        workflow_screenshot_artifacts = await app.WORKFLOW_SERVICE.get_recent_workflow_screenshot_artifacts(
            workflow_run_id=workflow_run_id,
            organization_id=organization_id,
        )

        # Attempt to get downloaded files for the current iteration
        downloaded_files: list[FileInfo] = []
        try:
            async with asyncio.timeout(GET_DOWNLOADED_FILES_TIMEOUT):
                downloaded_files = await app.STORAGE.get_downloaded_files(
                    organization_id=organization_id or "",
                    run_id=download_lookup_run_id,
                )
        except asyncio.TimeoutError:
            LOG.warning("Timeout getting downloaded files", task_v2_id=task_v2.observer_cruise_id)
        downloaded_files = filter_downloaded_files_for_current_iteration(
            downloaded_files,
            loop_internal_state,
        )

        task_v2_output = {
            "task_id": task_v2.observer_cruise_id,
            "status": task_v2.status,
            "summary": task_v2.summary,
            "extracted_information": result_dict,
            "failure_reason": failure_reason,
            "failure_category": task_v2.failure_category,
            "downloaded_files": [fi.model_dump() for fi in downloaded_files],
            "downloaded_file_urls": [fi.url for fi in downloaded_files],
            "task_screenshot_artifact_ids": [a.artifact_id for a in task_screenshot_artifacts],
            "workflow_screenshot_artifact_ids": [a.artifact_id for a in workflow_screenshot_artifacts],
        }
        await self.record_output_parameter_value(workflow_run_context, workflow_run_id, task_v2_output)
        return await self.build_block_result(
            success=success or self.continue_on_failure,
            failure_reason=failure_reason,
            output_parameter_value=task_v2_output,
            status=block_status,
            workflow_run_block_id=workflow_run_block_id,
            organization_id=organization_id,
        )


def _is_secret_scalar(value: Any) -> bool:
    return isinstance(value, str) and value != ""


def _secret_path_suffix(path: str) -> str | None:
    # The last non-numeric segment names the placeholder (placeholder_XXXX_ssn) so the
    # LLM gets the same field-matching signal credential stubs carry (_username, _password).
    for segment in reversed(path.split(".")):
        cleaned = re.sub(r"[^A-Za-z0-9_]", "_", segment).strip("_")
        if cleaned and not cleaned.isdigit():
            return cleaned[:32]
    return None


def _register_and_replace_secret_response_path(
    response_body: Any,
    path: str,
    workflow_run_context: WorkflowRunContext,
) -> bool:
    suffix = _secret_path_suffix(path)
    current = response_body
    segments = path.split(".")
    for index, segment in enumerate(segments):
        is_last = index == len(segments) - 1
        if isinstance(current, dict):
            if segment not in current:
                return False
            if is_last:
                value = current[segment]
                if not _is_secret_scalar(value):
                    return False
                current[segment] = workflow_run_context.register_secret_value(str(value), suffix=suffix)
                return True
            current = current[segment]
        elif isinstance(current, list):
            if not segment.isdigit():
                return False
            list_index = int(segment)
            if list_index >= len(current):
                return False
            if is_last:
                value = current[list_index]
                if not _is_secret_scalar(value):
                    return False
                current[list_index] = workflow_run_context.register_secret_value(str(value), suffix=suffix)
                return True
            current = current[list_index]
        else:
            return False
    return False


def _apply_secret_response_paths(
    response_body: Any,
    secret_response_paths: list[str],
    workflow_run_context: WorkflowRunContext,
) -> list[str]:
    invalid_paths: list[str] = []
    for path in dict.fromkeys(p.strip() for p in secret_response_paths if p.strip()):
        if not _register_and_replace_secret_response_path(response_body, path, workflow_run_context):
            invalid_paths.append(path)
    return invalid_paths


SECRET_RESPONSE_BODY_REDACTED = "<response body redacted: secret_response_paths did not fully resolve>"


class HttpRequestBlock(Block):
    # There is a mypy bug with Literal. Without the type: ignore, mypy will raise an error:
    # Parameter 1 of Literal[...] cannot be of type "Any"
    block_type: Literal[BlockType.HTTP_REQUEST] = BlockType.HTTP_REQUEST  # type: ignore

    # Individual HTTP parameters
    method: str = "GET"
    url: str | None = None
    headers: dict[str, str] | None = None
    body: dict[str, Any] | None = None  # Changed to consistently be dict only
    files: dict[str, str] | None = None  # Dictionary mapping field names to file paths for multipart file uploads
    timeout: int = 30
    follow_redirects: bool = True
    download_filename: str | None = None
    save_response_as_file: bool = False
    secret_response_paths: list[str] | None = None

    # Parameters for templating
    parameters: list[PARAMETER_TYPE] = []

    # Allowed directories for local file access (class variable, not a Pydantic field)
    _allowed_dirs: ClassVar[list[str] | None] = None

    TEMPLATABLE_FIELDS: ClassVar[frozenset[str]] = frozenset({"body", "download_filename", "files", "headers", "url"})

    @classmethod
    def get_allowed_dirs(cls) -> list[str]:
        """Get the list of allowed directories for local file access.
        Computed once and cached for performance.
        """
        if cls._allowed_dirs is None:
            allowed_dirs: list[str] = []
            if settings.ARTIFACT_STORAGE_PATH:
                allowed_dirs.append(os.path.abspath(settings.ARTIFACT_STORAGE_PATH))
            if settings.VIDEO_PATH:
                allowed_dirs.append(os.path.abspath(settings.VIDEO_PATH))
            if settings.HAR_PATH:
                allowed_dirs.append(os.path.abspath(settings.HAR_PATH))
            if settings.LOG_PATH:
                allowed_dirs.append(os.path.abspath(settings.LOG_PATH))
            if settings.DOWNLOAD_PATH:
                allowed_dirs.append(os.path.abspath(settings.DOWNLOAD_PATH))
            cls._allowed_dirs = allowed_dirs
        return cls._allowed_dirs or []

    def get_all_parameters(
        self,
        workflow_run_id: str,
    ) -> list[PARAMETER_TYPE]:
        parameters = self.parameters
        workflow_run_context = self.get_workflow_run_context(workflow_run_id)

        # Check if url is a parameter
        if self.url and workflow_run_context.has_parameter(self.url):
            if self.url not in [parameter.key for parameter in parameters]:
                parameters.append(workflow_run_context.get_parameter(self.url))

        return parameters

    def format_potential_template_parameters(self, workflow_run_context: WorkflowRunContext) -> None:
        """Format template parameters in the block fields"""

        def _render_string(field: str, value: str) -> str:
            rendered = self.render_templatable_field(field, value, workflow_run_context, force_include_secrets=True)
            # Boundary check so a longer id sharing a registered token's prefix is not partially replaced.
            for token in dict.fromkeys(workflow_run_context.find_embedded_placeholder_tokens(rendered)):
                secret_value = str(workflow_run_context.secrets[token])
                rendered = re.sub(
                    re.escape(token) + r"(?![A-Za-z0-9_])",
                    secret_value.replace("\\", "\\\\"),
                    rendered,
                )
            return rendered

        if self.url:
            self.url = _render_string("url", self.url)

        if self.body:
            self.body = cast(
                dict[str, Any],
                render_templates_in_json_value(self.body, lambda value: _render_string("body", value)),
            )

        if self.files:
            self.files = cast(
                dict[str, str],
                render_templates_in_json_value(self.files, lambda value: _render_string("files", value)),
            )

        if self.headers:
            self.headers = cast(
                dict[str, str],
                render_templates_in_json_value(self.headers, lambda value: _render_string("headers", value)),
            )

        if self.download_filename:
            self.download_filename = _render_string("download_filename", self.download_filename)

    def validate_url(self, url: str) -> bool:
        """Validate if the URL is properly formatted"""
        try:
            result = urlparse(url)
            return all([result.scheme, result.netloc])
        except Exception:
            return False

    async def _execute_file_download(
        self,
        workflow_run_context: WorkflowRunContext,
        workflow_run_id: str,
        workflow_run_block_id: str,
        organization_id: str | None,
    ) -> BlockResult:
        if not self.url:
            return await self.build_block_result(
                success=False,
                failure_reason="URL is required for file download",
                output_parameter_value=None,
                status=BlockStatus.failed,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )

        try:
            max_size_mb = settings.MAX_HTTP_DOWNLOAD_FILE_SIZE // (1024 * 1024)
            output_dir = get_download_dir(workflow_run_id)
            file_path = await download_file(
                self.url,
                max_size_mb=max_size_mb,
                headers=self.headers,
                output_dir=output_dir,
                filename=self.download_filename,
                organization_id=organization_id,
            )

            response_data = {
                "file_path": file_path,
                "file_name": os.path.basename(file_path),
                "file_size": os.path.getsize(file_path),
            }

            await self.record_output_parameter_value(workflow_run_context, workflow_run_id, response_data)

            return await self.build_block_result(
                success=True,
                failure_reason=None,
                output_parameter_value=response_data,
                status=BlockStatus.completed,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )

        except aiohttp.ClientResponseError as e:
            error_data = {"error": f"HTTP {e.status}", "error_type": "http_error"}
            await self.record_output_parameter_value(workflow_run_context, workflow_run_id, error_data)
            return await self.build_block_result(
                success=False,
                failure_reason=f"HTTP {e.status}",
                output_parameter_value=error_data,
                status=BlockStatus.failed,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )
        except DownloadFileMaxSizeExceeded as e:
            max_size_str = f"{e.max_size:.1f}"
            error_data = {"error": f"File exceeds maximum size of {max_size_str}MB", "error_type": "file_too_large"}
            await self.record_output_parameter_value(workflow_run_context, workflow_run_id, error_data)
            return await self.build_block_result(
                success=False,
                failure_reason=f"File exceeds maximum size of {max_size_str}MB",
                output_parameter_value=error_data,
                status=BlockStatus.failed,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )
        except Exception as e:
            masked_error = str(workflow_run_context.mask_secrets_in_data(str(e)))
            error_data = {"error": masked_error, "error_type": "unknown"}
            LOG.warning(
                "File download failed",
                error=masked_error,
                url=workflow_run_context.mask_secrets_in_data(self.url),
                workflow_run_id=workflow_run_id,
            )
            await self.record_output_parameter_value(workflow_run_context, workflow_run_id, error_data)
            return await self.build_block_result(
                success=False,
                failure_reason=f"File download failed: {masked_error}",
                output_parameter_value=error_data,
                status=BlockStatus.failed,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )

    async def execute(
        self,
        workflow_run_id: str,
        workflow_run_block_id: str,
        organization_id: str | None = None,
        browser_session_id: str | None = None,
        **kwargs: dict,
    ) -> BlockResult:
        """Execute the HTTP request and return the response"""

        workflow_run_context = self.get_workflow_run_context(workflow_run_id)

        try:
            self.format_potential_template_parameters(workflow_run_context)
        except Exception as e:
            return await self._template_format_failure_result(
                e,
                f"Failed to format jinja template: {str(e)}",
                workflow_run_context,
                workflow_run_id,
                workflow_run_block_id,
                organization_id,
            )

        if self.save_response_as_file and self.secret_response_paths:
            return await self.build_block_result(
                success=False,
                failure_reason="secret_response_paths cannot be combined with save_response_as_file",
                output_parameter_value=None,
                status=BlockStatus.failed,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )

        # Validate URL
        if not self.url:
            return await self.build_block_result(
                success=False,
                failure_reason="URL is required for HTTP request",
                output_parameter_value=None,
                status=BlockStatus.failed,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )

        if not self.validate_url(self.url):
            return await self.build_block_result(
                success=False,
                failure_reason=f"Invalid URL format: {workflow_run_context.mask_secrets_in_data(self.url)}",
                output_parameter_value=None,
                status=BlockStatus.failed,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )

        # Add default content-type as application/json if not provided (unless files are being uploaded)
        if not self.headers:
            self.headers = {}

        # If files are provided, don't set default Content-Type (aiohttp will set multipart/form-data)
        if not self.files:
            if not self.headers.get("Content-Type") and not self.headers.get("content-type"):
                LOG.info(
                    "Adding default content-type as application/json",
                    headers=workflow_run_context.mask_secrets_in_data(self.headers),
                )
                self.headers["Content-Type"] = "application/json"

        # Download files from HTTP URLs or S3 URIs if needed
        # Also allow local files from allowed directories (ARTIFACT_STORAGE_PATH, VIDEO_PATH, HAR_PATH, LOG_PATH)
        if self.files:
            downloaded_files: dict[str, str] = {}
            for field_name, file_path in self.files.items():
                masked_file_path = str(workflow_run_context.mask_secrets_in_data(file_path))
                # Parse file path (handle file:// URI format)
                actual_file_path: str | None = None
                is_file_uri = file_path.startswith("file://")

                if is_file_uri:
                    try:
                        actual_file_path = parse_uri_to_path(file_path)
                    except ValueError as e:
                        masked_error = str(workflow_run_context.mask_secrets_in_data(str(e)))
                        return await self.build_block_result(
                            success=False,
                            failure_reason=(f"Invalid file URI format: {masked_file_path}. Error: {masked_error}"),
                            output_parameter_value=None,
                            status=BlockStatus.failed,
                            workflow_run_block_id=workflow_run_block_id,
                            organization_id=organization_id,
                        )
                else:
                    actual_file_path = file_path

                # Check if file_path is a URL or managed storage URI
                is_url = (
                    file_path.startswith("http://") or file_path.startswith("https://") or file_path.startswith("www.")
                )
                is_managed_storage_uri = (
                    file_path.startswith("s3://") or file_path.startswith("gs://") or file_path.startswith("azure://")
                )

                # Check if file is in allowed directories
                is_allowed_local_file = False
                if actual_file_path:
                    # Convert to absolute path for comparison (handles both absolute and relative paths)
                    abs_file_path = os.path.abspath(actual_file_path)

                    # Get allowed directory paths (using class method for cached result)
                    allowed_dirs = self.get_allowed_dirs()
                    LOG.debug("HttpRequestBlock Allowed directories", allowed_dirs=allowed_dirs)

                    # Check if file is within any allowed directory
                    for allowed_dir in allowed_dirs:
                        # Use os.path.commonpath to check if file is within allowed directory
                        try:
                            common_path = os.path.commonpath([abs_file_path, allowed_dir])
                            if common_path == allowed_dir:
                                is_allowed_local_file = True
                                break
                        except ValueError:
                            # Paths are on different drives (Windows) or incompatible
                            continue

                # If not URL, managed storage URI, or allowed local file, reject
                if not (is_url or is_managed_storage_uri or is_allowed_local_file):
                    return await self.build_block_result(
                        success=False,
                        failure_reason=(
                            "No permission to access local file: "
                            f"{masked_file_path}. Only HTTP/HTTPS URLs, "
                            "managed storage URIs, or files in allowed directories are allowed."
                        ),
                        output_parameter_value=None,
                        status=BlockStatus.failed,
                        workflow_run_block_id=workflow_run_block_id,
                        organization_id=organization_id,
                    )

                # Handle different file sources
                if is_allowed_local_file:
                    # Use local file directly
                    local_file_path_str: str = cast(str, actual_file_path)
                    masked_local_file_path = str(workflow_run_context.mask_secrets_in_data(local_file_path_str))
                    if not os.path.exists(local_file_path_str):
                        return await self.build_block_result(
                            success=False,
                            failure_reason=f"File not found: {masked_local_file_path}",
                            output_parameter_value=None,
                            status=BlockStatus.failed,
                            workflow_run_block_id=workflow_run_block_id,
                            organization_id=organization_id,
                        )
                    downloaded_files[field_name] = local_file_path_str
                    LOG.info(
                        "HttpRequestBlock Using allowed local file",
                        field_name=field_name,
                        file_path=masked_local_file_path,
                    )
                else:
                    # Download from remote source
                    try:
                        LOG.info(
                            "HttpRequestBlock Downloading file from remote source",
                            field_name=field_name,
                            file_path=masked_file_path,
                            is_url=is_url,
                            is_managed_storage_uri=is_managed_storage_uri,
                        )
                        local_file_path = await download_file(file_path, organization_id=organization_id)
                        downloaded_files[field_name] = local_file_path
                        LOG.info(
                            "HttpRequestBlock File downloaded successfully",
                            field_name=field_name,
                            original_path=masked_file_path,
                            local_path=local_file_path,
                        )
                    except Exception as e:
                        masked_error = str(workflow_run_context.mask_secrets_in_data(str(e)))
                        return await self.build_block_result(
                            success=False,
                            failure_reason=(f"Failed to download file {masked_file_path}: {masked_error}"),
                            output_parameter_value=None,
                            status=BlockStatus.failed,
                            workflow_run_block_id=workflow_run_block_id,
                            organization_id=organization_id,
                        )

            # Update self.files with local file paths
            self.files = downloaded_files

        if self.save_response_as_file:
            return await self._execute_file_download(
                workflow_run_context=workflow_run_context,
                workflow_run_id=workflow_run_id,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )

        try:
            LOG.info(
                "Executing HTTP request",
                method=self.method,
                url=workflow_run_context.mask_secrets_in_data(self.url),
                headers=workflow_run_context.mask_secrets_in_data(self.headers),
                workflow_run_id=workflow_run_id,
                body=workflow_run_context.mask_secrets_in_data(self.body),
                files=workflow_run_context.mask_secrets_in_data(self.files),
            )

            status_code, response_headers, response_body = await aiohttp_request(
                method=self.method,
                url=self.url,
                headers=self.headers,
                data=self.body,
                files=self.files,
                timeout=self.timeout,
                follow_redirects=self.follow_redirects,
            )

            success = 200 <= status_code < 300
            failure_reason = None
            invalid_secret_response_paths: list[str] = []
            if self.secret_response_paths:
                # Extract on every status so a secret echoed in an error body never reaches outputs or logs.
                invalid_secret_response_paths = _apply_secret_response_paths(
                    response_body,
                    self.secret_response_paths,
                    workflow_run_context,
                )
                if success and invalid_secret_response_paths:
                    success = False
                    failure_reason = (
                        "secret_response_paths did not resolve to a non-empty string: "
                        f"{', '.join(invalid_secret_response_paths)}"
                    )

            response_data = {
                "status_code": status_code,
                "response_headers": response_headers,
                "response_body": response_body,
                "request_method": self.method,
                "request_url": self.url,
                "request_headers": self.headers,
                "request_body": self.body,
                "headers": response_headers,
                "body": response_body,
                "url": self.url,
            }

            if invalid_secret_response_paths:
                response_data["response_body"] = SECRET_RESPONSE_BODY_REDACTED
                response_data["body"] = SECRET_RESPONSE_BODY_REDACTED

            response_data = workflow_run_context.mask_secrets_in_data(response_data)

            LOG.info(
                "HTTP request completed",
                status_code=status_code,
                url=workflow_run_context.mask_secrets_in_data(self.url),
                method=self.method,
                workflow_run_id=workflow_run_id,
                response_data=response_data,
            )

            if failure_reason is None and not success:
                failure_reason = f"HTTP {status_code}: {response_data.get('response_body', '')}"

            await self.record_output_parameter_value(workflow_run_context, workflow_run_id, response_data)

            return await self.build_block_result(
                success=success,
                failure_reason=failure_reason,
                output_parameter_value=response_data,
                status=BlockStatus.completed if success else BlockStatus.failed,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )

        except asyncio.TimeoutError:
            error_data = {"error": "Request timed out", "error_type": "timeout"}
            await self.record_output_parameter_value(workflow_run_context, workflow_run_id, error_data)
            return await self.build_block_result(
                success=False,
                failure_reason=f"Request timed out after {self.timeout} seconds",
                output_parameter_value=error_data,
                status=BlockStatus.timed_out,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )
        except Exception as e:
            masked_error = str(workflow_run_context.mask_secrets_in_data(str(e)))
            error_data = {"error": masked_error, "error_type": "unknown"}
            LOG.warning(
                "HTTP request failed with unexpected error",
                error=masked_error,
                url=workflow_run_context.mask_secrets_in_data(self.url),
                method=self.method,
                workflow_run_id=workflow_run_id,
            )
            await self.record_output_parameter_value(workflow_run_context, workflow_run_id, error_data)
            return await self.build_block_result(
                success=False,
                failure_reason=f"HTTP request failed: {masked_error}",
                output_parameter_value=error_data,
                status=BlockStatus.failed,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )


class PrintPageBlock(Block):
    block_type: Literal[BlockType.PRINT_PAGE] = BlockType.PRINT_PAGE  # type: ignore

    include_timestamp: bool = True
    custom_filename: str | None = None
    format: str = "A4"
    landscape: bool = False
    print_background: bool = True
    parameters: list[PARAMETER_TYPE] = []

    VALID_FORMATS: ClassVar[set[str]] = {"A4", "Letter", "Legal", "Tabloid"}

    TEMPLATABLE_FIELDS: ClassVar[frozenset[str]] = frozenset({"custom_filename"})

    def get_all_parameters(self, workflow_run_id: str) -> list[PARAMETER_TYPE]:
        return self.parameters

    @staticmethod
    def _sanitize_filename(filename: str) -> str:
        return sanitize_filename(filename)

    def _build_pdf_options(self) -> dict[str, Any]:
        pdf_format = self.format if self.format in self.VALID_FORMATS else "A4"
        pdf_options: dict[str, Any] = {
            "format": pdf_format,
            "landscape": self.landscape,
            "print_background": self.print_background,
        }

        if self.include_timestamp:
            pdf_options["display_header_footer"] = True
            pdf_options["header_template"] = (
                '<div style="font-size:10px;width:100%;display:flex;justify-content:space-between;padding:0 10px;">'
                '<span class="date"></span><span class="title"></span><span></span></div>'
            )
            pdf_options["footer_template"] = (
                '<div style="font-size:10px;width:100%;display:flex;justify-content:space-between;padding:0 10px;">'
                '<span class="url"></span><span></span><span><span class="pageNumber"></span>/<span class="totalPages"></span></span></div>'
            )
            pdf_options["margin"] = {"top": "40px", "bottom": "40px"}

        return pdf_options

    async def _upload_pdf_artifact(
        self,
        *,
        pdf_bytes: bytes,
        workflow_run_id: str,
        workflow_run_block_id: str,
        workflow_run_context: WorkflowRunContext,
        organization_id: str | None,
    ) -> tuple[str | None, str | None]:
        artifact_org_id = organization_id or workflow_run_context.organization_id
        if not artifact_org_id:
            LOG.warning(
                "PrintPageBlock Missing organization_id, skipping artifact upload",
                workflow_run_id=workflow_run_id,
                workflow_run_block_id=workflow_run_block_id,
            )
            return None, None

        try:
            workflow_run_block = await app.DATABASE.observer.get_workflow_run_block(
                workflow_run_block_id,
                organization_id=artifact_org_id,
            )
        except NotFoundError:
            LOG.warning(
                "PrintPageBlock Workflow run block not found, skipping artifact upload",
                workflow_run_id=workflow_run_id,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=artifact_org_id,
            )
            return None, None

        artifact_id, artifact_uri = await app.ARTIFACT_MANAGER.create_workflow_run_block_artifact_with_uri(
            workflow_run_block=workflow_run_block,
            artifact_type=ArtifactType.PDF,
            data=pdf_bytes,
        )
        try:
            await app.ARTIFACT_MANAGER.wait_for_upload_aiotasks([workflow_run_block.workflow_run_block_id])
        except Exception:
            LOG.warning(
                "PrintPageBlock Failed to upload PDF artifact",
                workflow_run_id=workflow_run_id,
                workflow_run_block_id=workflow_run_block.workflow_run_block_id,
                exc_info=True,
            )
            return None, None

        # Generate a downloadable URL for the artifact
        artifact_url = None
        try:
            artifact = await app.DATABASE.artifacts.get_artifact_by_id(artifact_id, organization_id=artifact_org_id)
            if artifact:
                artifact_url = await app.ARTIFACT_MANAGER.get_share_link(artifact)
        except Exception:
            LOG.warning(
                "PrintPageBlock Failed to generate artifact download URL",
                artifact_id=artifact_id,
                exc_info=True,
            )

        return artifact_uri, artifact_url

    async def _register_pdf_as_downloaded_file(
        self,
        *,
        organization_id: str | None,
        workflow_run_id: str,
        workflow_run_block_id: str,
        download_run_id: str | None = None,
    ) -> list[FileInfo]:
        # Workflow finalization eventually runs save_downloaded_files, but the block
        # output snapshot is recorded now and the UI keys off downloaded_file_urls
        # on the block — so we register up front and let finalization re-run safely.
        if not organization_id:
            return []
        storage_run_id = download_run_id or workflow_run_id
        try:
            async with asyncio.timeout(SAVE_DOWNLOADED_FILES_TIMEOUT):
                await app.STORAGE.save_downloaded_files(
                    organization_id=organization_id,
                    run_id=storage_run_id,
                )
        except asyncio.TimeoutError:
            LOG.warning(
                "Timeout to save downloaded files",
                workflow_run_id=workflow_run_id,
                workflow_run_block_id=workflow_run_block_id,
            )
            return []
        except DownloadSaveIncompleteError as exc:
            # The PDF may be among the files that did save; read back what registered.
            LOG.warning(
                "Storage skipped saving some downloaded files; reading back what registered",
                workflow_run_id=workflow_run_id,
                workflow_run_block_id=workflow_run_block_id,
                skipped_file_count=len(exc.skipped_files),
            )
        except Exception:
            LOG.warning(
                "PrintPageBlock failed to register PDF as downloaded file; will retry at workflow finalization",
                workflow_run_id=workflow_run_id,
                workflow_run_block_id=workflow_run_block_id,
                exc_info=True,
            )
            return []
        try:
            async with asyncio.timeout(GET_DOWNLOADED_FILES_TIMEOUT):
                return await app.STORAGE.get_downloaded_files(
                    organization_id=organization_id,
                    run_id=storage_run_id,
                )
        except asyncio.TimeoutError:
            LOG.warning(
                "Timeout getting downloaded files",
                workflow_run_id=workflow_run_id,
                workflow_run_block_id=workflow_run_block_id,
            )
            return []

    async def execute(
        self,
        workflow_run_id: str,
        workflow_run_block_id: str,
        organization_id: str | None = None,
        browser_session_id: str | None = None,
        **kwargs: dict,
    ) -> BlockResult:
        workflow_run_context = self.get_workflow_run_context(workflow_run_id)

        # Scope downloaded files to this block only.
        block_context = skyvern_context.current()
        if block_context:
            await capture_block_download_baseline(block_context, organization_id or "", workflow_run_id, self.label)

        resolved_download_id = resolve_run_download_id(block_context, fallback_run_id=workflow_run_id)
        browser_state = await self.get_or_create_browser_state(
            workflow_run_id=workflow_run_id,
            organization_id=organization_id,
            browser_session_id=browser_session_id,
            download_run_id_override=resolved_download_id,
        )
        if not browser_state:
            return await self.build_block_result(
                success=False,
                failure_reason="No browser state available",
                status=BlockStatus.failed,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )

        page = await browser_state.get_working_page()
        if not page:
            return await self.build_block_result(
                success=False,
                failure_reason="No page available",
                status=BlockStatus.failed,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )

        pdf_options = self._build_pdf_options()

        try:
            pdf_bytes = await page.pdf(**pdf_options)
        except Exception as e:
            error_msg = str(e)
            if "pdf" in error_msg.lower() and ("not supported" in error_msg.lower() or "chromium" in error_msg.lower()):
                error_msg = "PDF generation requires Chromium browser. Current browser does not support page.pdf()."
            LOG.warning("PrintPageBlock Failed to generate PDF", error=error_msg, workflow_run_id=workflow_run_id)
            return await self.build_block_result(
                success=False,
                failure_reason=f"Failed to generate PDF: {error_msg}",
                status=BlockStatus.failed,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )

        timestamp_str = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        if self.custom_filename:
            filename = self.render_templatable_field("custom_filename", self.custom_filename, workflow_run_context)
            filename = self._sanitize_filename(filename)
            if not filename.endswith(".pdf"):
                filename += ".pdf"
        else:
            filename = f"page_{timestamp_str}.pdf"

        # Save PDF to download directory so it appears in runs UI
        download_dir = get_download_dir(resolved_download_id)
        file_path = os.path.join(download_dir, filename)
        async with aiofiles.open(file_path, "wb") as f:
            await f.write(pdf_bytes)

        # Upload to artifact storage for downstream block access (e.g., File Extraction Block)
        artifact_uri, artifact_url = await self._upload_pdf_artifact(
            pdf_bytes=pdf_bytes,
            workflow_run_id=workflow_run_id,
            workflow_run_block_id=workflow_run_block_id,
            workflow_run_context=workflow_run_context,
            organization_id=organization_id,
        )

        artifact_org_id = organization_id or workflow_run_context.organization_id
        downloaded_files = await self._register_pdf_as_downloaded_file(
            organization_id=artifact_org_id,
            workflow_run_id=workflow_run_id,
            workflow_run_block_id=workflow_run_block_id,
            download_run_id=resolved_download_id,
        )

        current_context = skyvern_context.current()
        downloaded_files = filter_downloaded_files_for_current_iteration(
            downloaded_files,
            current_context.loop_internal_state if current_context else None,
        )
        output = {
            "filename": filename,
            "file_path": file_path,
            "size_bytes": len(pdf_bytes),
            "artifact_uri": artifact_uri,
            "artifact_url": artifact_url,
            "downloaded_files": [fi.model_dump() for fi in downloaded_files],
            "downloaded_file_urls": [fi.url for fi in downloaded_files],
            "downloaded_file_artifact_ids": [fi.artifact_id for fi in downloaded_files if fi.artifact_id],
        }
        await self.record_output_parameter_value(workflow_run_context, workflow_run_id, output)

        return await self.build_block_result(
            success=True,
            failure_reason=None,
            output_parameter_value=output,
            status=BlockStatus.completed,
            workflow_run_block_id=workflow_run_block_id,
            organization_id=organization_id,
        )


# A `{` opening a Jinja delimiter ({{, {%, {#) and a `}` closing one (}}, %}, #}). Splitting on
# single characters (instead of replacing two-character delimiters) guarantees runs like `{{{`
# cannot re-form a delimiter after substitution.
_JINJA_DELIMITER_OPENER_RE = re.compile(r"\{(?=[{%#])")
_JINJA_DELIMITER_CLOSER_RE = re.compile(r"(?<=[}%#])\}")


def _neutralize_jinja_delimiters(value: Any) -> Any:
    """Space out Jinja delimiters in strings (`{{` -> `{ {`) so stored raw expressions stay
    readable for the LLM but can never execute in a downstream template render (SKY-14080)."""
    if isinstance(value, str):
        return _JINJA_DELIMITER_CLOSER_RE.sub(" }", _JINJA_DELIMITER_OPENER_RE.sub("{ ", value))
    if isinstance(value, dict):
        # Keys are left as-is: neutralization is not injective, so rewriting keys can collapse two
        # distinct keys into one and silently drop a value.
        return {key: _neutralize_jinja_delimiters(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_neutralize_jinja_delimiters(item) for item in value]
    return value


class BranchEvaluationContext:
    """Collection of runtime data that BranchCriteria evaluators can consume."""

    def __init__(
        self,
        *,
        workflow_run_context: WorkflowRunContext | None = None,
        block_label: str | None = None,
        template_renderer: Callable[[str], str] | None = None,
    ) -> None:
        self.workflow_run_context = workflow_run_context
        self.block_label = block_label
        self.template_renderer = template_renderer

    def build_llm_safe_context_snapshot(self) -> dict[str, Any]:
        """
        Build a minimal context blob for LLM-facing branch evaluation.

        Only includes essential data the LLM needs to evaluate conditions:
        - Parameter values (base_date, date_1, etc.)
        - Extracted information from previous blocks
        - Loop variables (current_value, current_index, current_item)
        """
        if self.workflow_run_context is None:
            return {}

        ctx = self.workflow_run_context
        raw_values: dict[str, Any] = ctx.values.copy()

        # Keys to skip - these are not useful for evaluating conditions
        keys_to_skip = {
            "blocks_metadata",
            "params",
            "outputs",
            "environment",
            "env",
            "llm",
            "workflow_title",
            "workflow_id",
            "workflow_permanent_id",
            "workflow_run_id",
        }

        snapshot: dict[str, Any] = {}
        for key, value in raw_values.items():
            # Skip noisy keys
            if key in keys_to_skip:
                continue

            # For block outputs (dicts with extracted_information), only include extracted_information
            if isinstance(value, dict) and "extracted_information" in value:
                extracted = value.get("extracted_information")
                if extracted is not None:
                    snapshot[key] = extracted
            else:
                # Include parameter values directly
                snapshot[key] = value

        # Copy loop variables (current_value, current_index, current_item) to top level
        # Required for pure NatLang expressions like "current_value['date']" to work
        if self.block_label:
            block_metadata = ctx.get_block_metadata(self.block_label)
            if "current_value" in block_metadata:
                snapshot["current_value"] = block_metadata["current_value"]
            if "current_index" in block_metadata:
                snapshot["current_index"] = block_metadata["current_index"]
            if "current_item" in block_metadata:
                snapshot["current_item"] = block_metadata["current_item"]

        # Mask any real secret values that may have leaked into values
        snapshot = ctx.mask_secrets_in_data(snapshot)

        # Stored block outputs can carry raw `{{...}}` expressions; a live delimiter here would
        # inject into any later Jinja render of a prompt that embeds this snapshot.
        return _neutralize_jinja_delimiters(snapshot)

    def _declared_parameter_keys(self) -> list[str]:
        ctx = self.workflow_run_context
        workflow = ctx.workflow if ctx is not None else None
        if workflow is None or workflow.workflow_definition is None:
            return []
        for block in get_all_blocks(workflow.workflow_definition.blocks):
            if block.label == self.block_label:
                # parameters is declared per block type, not on the Block base
                parameters = getattr(block, "parameters", None) or []
                return [parameter.key for parameter in parameters if parameter.key]
        return []

    def build_template_data(self) -> dict[str, Any]:
        """Build Jinja template data mirroring block parameter rendering context."""
        if self.workflow_run_context is None:
            return {
                "params": {},
                "outputs": {},
                "environment": {},
                "env": {},
                "llm": {},
            }

        ctx = self.workflow_run_context
        template_data = ctx.values.copy()
        if ctx.include_secrets_in_templates:
            template_data.update(
                ctx.credential_template_entries(self._declared_parameter_keys(), resolve_credential_dicts=False)
            )

        if self.block_label:
            block_reference_data: dict[str, Any] = ctx.get_block_metadata(self.block_label)
            if self.block_label in template_data:
                current_value = template_data[self.block_label]
                if isinstance(current_value, dict):
                    block_reference_data.update(current_value)
            template_data[self.block_label] = block_reference_data

            if "current_index" in block_reference_data:
                template_data["current_index"] = block_reference_data["current_index"]
            if "current_item" in block_reference_data:
                template_data["current_item"] = block_reference_data["current_item"]
            if "current_value" in block_reference_data:
                template_data["current_value"] = block_reference_data["current_value"]

        template_data.setdefault("workflow_title", ctx.workflow_title)
        template_data.setdefault("workflow_id", ctx.workflow_id)
        template_data.setdefault("workflow_permanent_id", ctx.workflow_permanent_id)
        template_data.setdefault("workflow_run_id", ctx.workflow_run_id)
        template_data.setdefault("current_date", datetime.now(UTC).strftime(CURRENT_DATE_FORMAT))

        template_data.setdefault("params", template_data.get("params", {}))
        template_data.setdefault("outputs", template_data.get("outputs", {}))
        template_data.setdefault("environment", template_data.get("environment", {}))
        template_data.setdefault("env", template_data.get("environment"))
        template_data.setdefault("llm", template_data.get("llm", {}))

        return template_data


class BranchCriteria(BaseModel, abc.ABC):
    """Abstract interface describing how a branch condition should be evaluated."""

    criteria_type: str
    expression: str
    description: str | None = None

    @abc.abstractmethod
    async def evaluate(self, context: BranchEvaluationContext) -> bool:
        """Return True when the branch should execute."""
        raise NotImplementedError

    def requires_llm(self) -> bool:
        """Whether the criteria relies on an LLM classification step."""
        return False


def _evaluate_truthy_string(value: str) -> bool:
    """
    Evaluate a string as a boolean, handling common truthy/falsy representations.

    Truthy: "true", "True", "TRUE", "1", "yes", "y", "on", non-zero numbers
    Falsy: "", "false", "False", "FALSE", "0", "no", "n", "off", "null", "None", whitespace-only

    For other strings, use Python's default bool() behavior (non-empty = truthy).
    """
    if not value or not value.strip():
        return False

    normalized = value.strip().lower()

    # Explicit falsy values
    if normalized in ("false", "0", "no", "n", "off", "null", "none"):
        return False

    # Explicit truthy values
    if normalized in ("true", "1", "yes", "y", "on"):
        return True

    # Try to parse as a number
    try:
        num = float(normalized)
        return num != 0.0
    except ValueError:
        pass

    # For any other non-empty string, consider it truthy
    # This allows expressions like "{{ 'some text' }}" to be truthy
    return True


class JinjaBranchCriteria(BranchCriteria):
    """Jinja2-templated branch criteria (only supported criteria type for now)."""

    criteria_type: Literal["jinja2_template"] = "jinja2_template"

    async def evaluate(self, context: BranchEvaluationContext) -> bool:
        # Prefer the renderer provided by the caller (matches block parameter rendering),
        # otherwise build a minimal sandboxed renderer using the evaluation context.
        if context.template_renderer:
            try:
                rendered = context.template_renderer(self.expression)
            except MissingJinjaVariables:
                # Let upstream MissingJinjaVariables bubble as-is.
                raise
            except Exception as exc:  # pragma: no cover - caught for robustness
                raise FailedToFormatJinjaStyleParameter(self.expression, str(exc)) from exc
        else:
            template_data = context.build_template_data()
            sandbox_env = (
                SandboxedEnvironment(undefined=StrictUndefined)
                if settings.WORKFLOW_TEMPLATING_STRICTNESS == "strict"
                else SandboxedEnvironment()
            )

            try:
                missing_vars = get_missing_variables(self.expression, template_data)
                if missing_vars:
                    raise MissingJinjaVariables(self.expression, missing_vars)

                template = sandbox_env.from_string(self.expression)
                rendered = template.render(template_data)
            except MissingJinjaVariables:
                raise
            except Exception as exc:
                # Covers syntax errors and rendering issues
                raise FailedToFormatJinjaStyleParameter(self.expression, str(exc)) from exc

        return _evaluate_truthy_string(rendered)


class PromptBranchCriteria(BranchCriteria):
    """Natural language branch criteria."""

    criteria_type: Literal["prompt"] = "prompt"

    async def evaluate(self, context: BranchEvaluationContext) -> bool:
        # Evaluated via ConditionalBlock.execute (batched) or WhileLoopBlock
        # _evaluate_condition (single-branch batch helper).
        raise NotImplementedError("PromptBranchCriteria is evaluated via extraction batch helpers, not per-branch.")

    def requires_llm(self) -> bool:
        return True


def _is_pure_jinja_expression(expression: str) -> bool:
    """
    Determine if an expression is a pure Jinja template (single block) vs Jinja+NatLang (mixed).

    Pure Jinja: "{{ A == B }}" - single Jinja block, should be evaluated server-side
    Jinja+NatLang: "{{ A }} is same as {{ B }}" - multiple Jinja blocks mixed with natural language

    Returns True only for pure Jinja expressions that can be evaluated to boolean server-side.
    """
    if not expression:
        return False

    stripped = expression.strip()

    # Must start with {{ and end with }}
    if not (stripped.startswith("{{") and stripped.endswith("}}")):
        return False

    # Count the number of {{ occurrences
    # If there's more than one, it's Jinja+NatLang (e.g., "{{ A }} is same as {{ B }}")
    jinja_open_count = stripped.count("{{")
    if jinja_open_count > 1:
        return False

    # Single {{ and ends with }} - this is pure Jinja
    return True


def _resolve_nested_path(value: Any, path: str) -> Any:
    """
    Resolve a dotted/bracket access path on a nested value.

    Examples:
        _resolve_nested_path({"a": {"b": 1}}, ".a.b") -> 1
        _resolve_nested_path([{"x": 2}], "[0].x") -> 2

    Args:
        value: The root value to traverse
        path: The access path (e.g., ".field1.field2[0].field3")

    Returns:
        The resolved leaf value

    Raises:
        LookupError: If the path cannot be resolved
    """
    segments = re.findall(r"\.([a-zA-Z_]\w*)|\[(\d+)\]", path)
    current = value
    for dot_key, bracket_idx in segments:
        if dot_key:
            if isinstance(current, dict):
                if dot_key not in current:
                    raise LookupError(f"Key {dot_key!r} not found")
                current = current[dot_key]
            else:
                raise LookupError(f"Cannot access .{dot_key} on {type(current).__name__}")
        elif bracket_idx:
            idx = int(bracket_idx)
            if isinstance(current, (list, tuple)):
                if idx >= len(current):
                    raise LookupError(f"Index [{idx}] out of range")
                current = current[idx]
            else:
                raise LookupError(f"Cannot index [{idx}] on {type(current).__name__}")
    return current


_JINJA_DISPLAY_FILTERS: dict[str, Callable[[Any], Any]] = {
    "lower": lambda v: str(v).lower(),
    "upper": lambda v: str(v).upper(),
    "trim": lambda v: str(v).strip(),
    "title": lambda v: str(v).title(),
    "capitalize": lambda v: str(v).capitalize(),
    "int": lambda v: int(v),
    "float": lambda v: float(v),
    "string": lambda v: str(v),
    "length": lambda v: len(v),
    "abs": lambda v: abs(v),
}


def _render_jinja_expression_for_display(
    expression: str,
    context_values: dict[str, Any],
    block_label: str | None = None,
) -> str:
    """
    Render a pure Jinja expression for UI display by substituting variable names with values.

    This is for display purposes only - it shows users what values were compared
    without actually evaluating the expression. For example:
    - Input: "{{ base_date == date_1 }}" with context {"base_date": "01-25-2026", "date_1": "01-25-2026"}
    - Output: '"01-25-2026" == "01-25-2026"'
    - Input: "{{ output.extracted_information.field != None }}" with nested dict context
    - Output: '"some_value" != None'
    - Input: "{{ output.status|lower == 'active' }}" with context {"output": {"status": "Active"}}
    - Output: '"active" == \'active\''

    Known Jinja filters (lower, upper, trim, etc.) are applied to the resolved value.
    Unknown filters are left as-is in the output.

    Returns the original expression if it's not a pure Jinja expression or if rendering fails.
    """
    if not _is_pure_jinja_expression(expression):
        return expression

    try:
        # Extract inner expression (strip {{ and }})
        inner_expr = expression.strip()[2:-2].strip()
        display_expr = inner_expr

        # Substitute variable references (including dotted/bracket access paths and filters)
        # with their values.
        # Match var_name optionally followed by .field or [index] segments,
        # then optionally followed by a |filter_name.
        # Sort by key length (longest first) to avoid partial matches.
        for var_name in sorted(context_values.keys(), key=len, reverse=True):
            pattern = r"\b" + re.escape(var_name) + r"((?:\.[a-zA-Z_]\w*|\[\d+\])*)(\|[a-zA-Z_]\w*)?"

            def _replacer(match: re.Match, _var_name: str = var_name) -> str:
                access_path = match.group(1)  # the dotted/bracket part after var_name
                filter_expr = match.group(2)  # e.g., "|lower" or None
                var_value = context_values[_var_name]

                if access_path:
                    try:
                        var_value = _resolve_nested_path(var_value, access_path)
                    except LookupError:
                        # Path couldn't be resolved — return original text unchanged
                        return match.group(0)

                if filter_expr:
                    filter_name = filter_expr[1:]  # strip the leading |
                    filter_fn = _JINJA_DISPLAY_FILTERS.get(filter_name)
                    if filter_fn is not None:
                        try:
                            var_value = filter_fn(var_value)
                        except Exception:
                            # Filter application failed — show value with filter text
                            if isinstance(var_value, str):
                                return f'"{var_value}"{filter_expr}'
                            return f"{var_value}{filter_expr}"
                    else:
                        # Unknown filter — show value with filter text preserved
                        if isinstance(var_value, str):
                            return f'"{var_value}"{filter_expr}'
                        return f"{var_value}{filter_expr}"

                if isinstance(var_value, str):
                    return f'"{var_value}"'
                return str(var_value)

            display_expr = re.sub(pattern, _replacer, display_expr)

        return display_expr
    except Exception as exc:
        LOG.debug(
            "Failed to render Jinja expression for display",
            block_label=block_label,
            expression=expression,
            error=str(exc),
        )
        return expression


def _find_evaluations_array(output_value: dict[str, Any]) -> list[Any]:
    """
    Extract the evaluations array from LLM output.

    ExtractionBlock wraps output in 'extracted_information', so we check there first.
    Falls back to direct access if not found in the nested structure.

    Args:
        output_value: The raw output from ExtractionBlock

    Returns:
        List of evaluation objects from the LLM

    Raises:
        ValueError: If evaluations array is not found or has wrong type
    """
    # Try standard ExtractionBlock format: output_value.extracted_information.evaluations
    extracted_info = output_value.get("extracted_information")
    if isinstance(extracted_info, dict):
        raw_evaluations = extracted_info.get("evaluations")
    else:
        # Fallback: try direct access at output_value.evaluations
        raw_evaluations = output_value.get("evaluations")

    if not isinstance(raw_evaluations, list):
        raise ValueError(f"Expected array of evaluations, got: {type(raw_evaluations)}")

    return raw_evaluations


def _parse_single_evaluation(
    evaluation: Any,
    idx: int,
    fallback_rendered_expressions: list[str],
) -> tuple[bool, str]:
    """
    Parse a single evaluation from the LLM response.

    Handles two formats:
    - Dict format: {result: bool, reasoning: str}
    - Legacy format: just a boolean value

    The rendered expression always comes from the Jinja pre-rendering step (fallback),
    not from the LLM response, to avoid the LLM re-interpreting already-resolved values.

    Args:
        evaluation: Single evaluation object from LLM (dict or bool)
        idx: Index of this evaluation (for fallback lookup)
        fallback_rendered_expressions: Pre-rendered expressions from Jinja rendering

    Returns:
        Tuple of (boolean_result, rendered_expression_string)
    """
    rendered_expression = fallback_rendered_expressions[idx] if idx < len(fallback_rendered_expressions) else ""

    if isinstance(evaluation, dict):
        result = evaluation.get("result")
        if isinstance(result, bool):
            bool_result = result
        else:
            bool_result = _evaluate_truthy_string(str(result))
            LOG.warning(
                "Conditional branch evaluation returned non-boolean result",
                branch_index=idx,
                result=result,
                evaluated_result=bool_result,
            )

        return (bool_result, rendered_expression)
    else:
        # Legacy format: just a boolean
        if isinstance(evaluation, bool):
            bool_result = evaluation
        else:
            bool_result = _evaluate_truthy_string(str(evaluation))

        return (bool_result, rendered_expression)


# Number of times to evaluate the prompt-based conditional branches before giving up.
# The dominant failure is a transient malformed/under-returned LLM batch; one re-roll
# recovers most of them while still failing loudly when the model is persistently wrong.
MAX_PROMPT_BRANCH_EVAL_ATTEMPTS = 2

# Reserve 30k downstream tokens, about 4.7x the measured 6.4k non-goal overhead; this is not a
# large-page bound, so the generic guard remains. Fail closed because truncation could silently change branch selection.
BRANCH_EVALUATION_DOWNSTREAM_TOKEN_RESERVE = 30_000
BRANCH_EVALUATION_GOAL_MAX_TOKENS = PROMPT_HARD_CEILING_TOKENS - BRANCH_EVALUATION_DOWNSTREAM_TOKEN_RESERVE
BRANCH_CONTEXT_CIRCUIT_BREAKER_EVENT = "conditional_branch_context_circuit_breaker_tripped"
BRANCH_CONTEXT_CONTRIBUTOR_LIMIT = 5


def _largest_branch_context_contributors(context_snapshot: dict[str, Any]) -> list[dict[str, int | str]]:
    """Return privacy-safe sizes for the largest snapshot values, never their keys or contents."""
    largest_values: list[tuple[int, str, str]] = []
    for key, value in context_snapshot.items():
        serialized_value = json.dumps(value, default=str)
        largest_values.append((len(serialized_value.encode("utf-8")), key, serialized_value))
        largest_values.sort(key=lambda item: item[0], reverse=True)
        del largest_values[BRANCH_CONTEXT_CONTRIBUTOR_LIMIT:]

    return [
        {
            "key_fingerprint": diagnostic_fingerprint(key),
            "serialized_bytes": serialized_bytes,
            "token_count": count_tokens(serialized_value),
        }
        for serialized_bytes, key, serialized_value in largest_values
    ]


def _build_branch_evaluation_schema(num_branches: int) -> dict[str, Any]:
    """Strict JSON schema for the batched branch-evaluation LLM call.

    ``additionalProperties: false`` plus a required ``condition_index`` stop the model from
    injecting hallucinated fields and force one self-identifying result per condition.
    """
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "evaluations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "condition_index": {
                            "type": "integer",
                            "description": (
                                "The 1-based index of the condition this object evaluates "
                                "(Condition 1 -> 1, Condition 2 -> 2, ...)."
                            ),
                        },
                        "reasoning": {
                            "type": "string",
                            "description": "Explanation of the reasoning behind evaluating the condition.",
                        },
                        "result": {
                            "type": "boolean",
                            "description": "TRUE if the condition is satisfied, FALSE otherwise.",
                        },
                    },
                    "required": ["condition_index", "reasoning", "result"],
                },
                "description": "Exactly one evaluation per condition, covering condition_index 1..N.",
                "minItems": num_branches,
                "maxItems": num_branches,
            }
        },
        "required": ["evaluations"],
    }


def _coerce_condition_index(raw: Any) -> int | None:
    """Read a 1-based ``condition_index`` the LLM may have typed loosely.

    Accepts ints, integral floats (``2.0``), and digit strings (``"2"``); returns ``None`` for
    bools (an int subclass that must not read as 0/1) and non-integral/garbage values. The schema
    is a prompt-level hint, not provider-enforced, so a model that under-returns can equally
    mistype the index; coercing keeps a loosely-typed index on the order-safe alignment path
    instead of misrouting via positional fallback.
    """
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    if isinstance(raw, float):
        return int(raw) if raw.is_integer() else None
    if isinstance(raw, str):
        try:
            return int(raw.strip())
        except ValueError:
            return None
    return None


def _align_branch_evaluations(
    *,
    output_value: Any,
    branches: list[BranchCondition],
    rendered_expressions: list[str],
) -> tuple[list[bool], list[str], dict[str, Any]]:
    """Align the LLM evaluations to ``branches``.

    Returns ``(results, rendered_expressions, normalized_output)``, where ``normalized_output``
    is the evaluations wrapped in a dict for recording/UI. Prefers the LLM-provided 1-based
    ``condition_index`` (coerced from ints, integral floats, or digit strings): this is order-safe
    and immune to hallucinated extra entries that would shift positional alignment. Falls back to
    positional parsing only when no usable index is present and the count matches exactly. A
    wrong-length or mis-indexed batch is discarded wholesale (never gap-filled) by raising
    ``MalformedBranchEvaluationError`` so the caller can retry or fail loudly.
    """
    n = len(branches)

    if isinstance(output_value, list):
        output_value = {"evaluations": output_value}
    if not isinstance(output_value, dict):
        raise MalformedBranchEvaluationError(f"unexpected output format: {type(output_value)}")
    try:
        raw_evaluations = _find_evaluations_array(output_value)
    except ValueError as exc:
        raise MalformedBranchEvaluationError(str(exc)) from exc

    by_index: dict[int, Any] = {}
    saw_condition_index = False
    for evaluation in raw_evaluations:
        if not isinstance(evaluation, dict):
            continue
        raw_index = _coerce_condition_index(evaluation.get("condition_index"))
        if raw_index is None:
            continue
        saw_condition_index = True
        if not (1 <= raw_index <= n):
            continue
        if raw_index in by_index:
            raise MalformedBranchEvaluationError(f"duplicate condition_index {raw_index}")
        by_index[raw_index] = evaluation

    if saw_condition_index:
        if set(by_index.keys()) != set(range(1, n + 1)):
            raise MalformedBranchEvaluationError(f"condition_index set {sorted(by_index.keys())} does not cover 1..{n}")
        ordered: list[Any] = [by_index[i] for i in range(1, n + 1)]
    else:
        # Legacy path (no condition_index): the LLM occasionally appends reasoning=None
        # placeholder entries. Strip them and accept only when the remainder is exactly N.
        well_formed = [e for e in raw_evaluations if not (isinstance(e, dict) and e.get("reasoning") is None)]
        ordered = well_formed if len(well_formed) == n else list(raw_evaluations)
        if len(ordered) != n:
            raise MalformedBranchEvaluationError(f"returned {len(ordered)} results for {n} branches")

    results_array: list[bool] = []
    llm_rendered_expressions: list[str] = []
    for idx, evaluation in enumerate(ordered):
        bool_result, rendered_expr = _parse_single_evaluation(
            evaluation=evaluation,
            idx=idx,
            fallback_rendered_expressions=rendered_expressions,
        )
        results_array.append(bool_result)
        llm_rendered_expressions.append(rendered_expr)
    return results_array, llm_rendered_expressions, output_value


# Pattern to find Jinja template blocks like {{ variable_name }}
_JINJA_BLOCK_RE = re.compile(r"\{\{(.*?)\}\}")
# Marker inserted into rendered expressions when a Jinja variable resolved to
# an empty/whitespace-only value.  The LLM uses this to reason about emptiness.
_EMPTY_VALUE_MARKER = "(empty value)"


def _make_empty_params_explicit(
    original_expression: str,
    rendered_expression: str,
) -> tuple[str, bool]:
    """
    Detect Jinja template variables that resolved to empty values and replace
    the empty gaps with explicit ``(empty value)`` markers.

    When ``{{test_parameter}}`` resolves to ``""``, the rendered expression becomes
    malformed (e.g., ``"if  is not empty"``).  This function detects such cases by
    comparing the *original* expression (with ``{{ }}`` blocks) against the
    *rendered* expression and rebuilds it with clear markers so the LLM can
    evaluate the condition correctly.

    Returns:
        ``(patched_expression, was_patched)``
    """
    if not original_expression or "{{" not in original_expression:
        return rendered_expression, False

    # Split the original expression into alternating [static, var, static, var, ...] parts.
    parts = _JINJA_BLOCK_RE.split(original_expression)
    if len(parts) <= 1:
        return rendered_expression, False

    # Extract static parts (even indices) and build a regex that captures what
    # each Jinja block rendered to by using the static text as anchors.
    static_parts = [parts[i] for i in range(0, len(parts), 2)]
    num_vars = len(parts) // 2

    # When two Jinja variables are adjacent (e.g. "{{a}}{{b}}") the interior
    # static separator is an empty string and the non-greedy regex cannot
    # reliably attribute rendered text to the correct variable.  Bail out.
    if num_vars > 1 and any(static == "" for static in static_parts[1:-1]):
        return rendered_expression, False

    # NOTE: if a rendered value happens to contain the same text as a static
    # anchor the regex may split on the wrong occurrence.  This is extremely
    # unlikely in user-authored conditional expressions and the worst-case
    # outcome is an unnecessary "(empty value)" marker, which still beats the
    # invisible empty-string that caused SKY-8073.

    regex_fragments: list[str] = []
    for i, static in enumerate(static_parts):
        regex_fragments.append(re.escape(static))
        if i < num_vars:
            regex_fragments.append("(.*?)")

    match = re.match("^" + "".join(regex_fragments) + "$", rendered_expression, re.DOTALL)
    if not match:
        return rendered_expression, False

    rendered_values = match.groups()
    has_empty = any(not v.strip() for v in rendered_values)
    if not has_empty:
        return rendered_expression, False

    # Rebuild the expression, replacing empty rendered values with an explicit marker.
    result_parts: list[str] = []
    for i, static in enumerate(static_parts):
        result_parts.append(static)
        if i < len(rendered_values):
            if not rendered_values[i].strip():
                result_parts.append(_EMPTY_VALUE_MARKER)
            else:
                result_parts.append(rendered_values[i])

    return "".join(result_parts), True


def _cap_debug_field(value: Any, *, limit_bytes: int = DECISION_BLOCK_FIELD_MAX_BYTES) -> Any:
    """Cap a string at ``limit_bytes`` UTF-8 bytes (suffix included); non-strings pass through (SKY-9779)."""
    if not isinstance(value, str):
        return value
    encoded = value.encode("utf-8")
    if len(encoded) <= limit_bytes:
        return value
    overflow_bytes = len(encoded) - limit_bytes
    suffix = f"…[truncated {overflow_bytes} bytes]"
    suffix_bytes = len(suffix.encode("utf-8"))
    head_budget = max(0, limit_bytes - suffix_bytes)
    return encoded[:head_budget].decode("utf-8", errors="ignore") + suffix


def _trim_branch_evaluations(branch_evaluations: list[dict] | None) -> list[dict] | None:
    """Drop ``rendered_expression`` on non-matched branches; cap the matched one (SKY-9779)."""
    if not branch_evaluations:
        return branch_evaluations
    trimmed: list[dict] = []
    for ev in branch_evaluations:
        if ev.get("is_matched"):
            ev = {**ev, "rendered_expression": _cap_debug_field(ev.get("rendered_expression"))}
        else:
            ev = {k: v for k, v in ev.items() if k != "rendered_expression"}
        trimmed.append(ev)
    return trimmed


class BranchCondition(BaseModel):
    """Represents a single conditional branch edge within a ConditionalBlock."""

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    criteria: BranchCriteriaTypeVar | None = None
    next_block_label: str | None = None
    description: str | None = None
    is_default: bool = False

    @model_validator(mode="after")
    def validate_condition(self) -> BranchCondition:
        if isinstance(self.criteria, dict):
            criteria_type = self.criteria.get("criteria_type")
            if criteria_type is None:
                # Infer criteria type from expression format
                expression = self.criteria.get("expression", "")
                if _is_pure_jinja_expression(expression):
                    criteria_type = "jinja2_template"
                else:
                    criteria_type = "prompt"
            if criteria_type == "prompt":
                self.criteria = PromptBranchCriteria(**self.criteria)
            else:
                self.criteria = JinjaBranchCriteria(**self.criteria)
        if self.criteria is None and not self.is_default:
            raise ValueError("Branches without criteria must be marked as default.")
        if self.criteria is not None and self.is_default:
            raise ValueError("Default branches may not define criteria.")
        if self.criteria and isinstance(self.criteria, BranchCriteria):
            expression = self.criteria.expression
            criteria_dict = self.criteria.model_dump()
            if _is_pure_jinja_expression(expression):
                criteria_dict["criteria_type"] = "jinja2_template"
                self.criteria = JinjaBranchCriteria(**criteria_dict)
            else:
                criteria_dict["criteria_type"] = "prompt"
                self.criteria = PromptBranchCriteria(**criteria_dict)
        return self


async def _evaluate_prompt_branch_conditions_batch(
    *,
    log_label: str,
    branches: list[BranchCondition],
    evaluation_context: BranchEvaluationContext,
    workflow_run_id: str,
    workflow_run_block_id: str,
    organization_id: str | None,
    browser_session_id: str | None,
    workflow_id: str,
    extraction_description_suffix: str = "",
) -> tuple[list[bool], list[str], str | None, dict | None]:
    if organization_id is None:
        raise ValueError("organization_id is required to evaluate natural language branches")

    if not branches:
        return ([], [], None, None)

    workflow_run_context = evaluation_context.workflow_run_context

    rendered_expressions: list[str] = []
    has_any_pure_natlang = False

    for idx, branch in enumerate(branches):
        expression = branch.criteria.expression if branch.criteria else ""
        has_jinja = "{{" in expression

        if has_jinja:
            try:
                rendered_expression = (
                    evaluation_context.template_renderer(expression)
                    if evaluation_context.template_renderer
                    else expression
                )
            except Exception as render_exc:
                LOG.error(
                    "Conditional branch expression rendering FAILED",
                    block_label=log_label,
                    branch_index=idx,
                    original_expression=expression,
                    error=str(render_exc),
                    exc_info=True,
                )
                rendered_expression = expression
                has_any_pure_natlang = True
            else:
                rendered_expression, was_patched = _make_empty_params_explicit(expression, rendered_expression)
                if was_patched:
                    LOG.info(
                        "Conditional branch expression patched for empty parameter(s)",
                        workflow_run_id=workflow_run_id,
                        block_label=log_label,
                        branch_index=idx,
                        original_expression=expression,
                        patched_expression=rendered_expression,
                    )
        else:
            rendered_expression = expression
            has_any_pure_natlang = True

        LOG.info(
            "Conditional branch expression rendering",
            block_label=log_label,
            branch_index=idx,
            original_expression=expression,
            rendered_expression=rendered_expression,
            has_jinja=has_jinja,
            expression_changed=expression != rendered_expression,
        )

        rendered_expressions.append(rendered_expression)

    context_snapshot: dict[str, Any] = {}
    if has_any_pure_natlang:
        context_snapshot = evaluation_context.build_llm_safe_context_snapshot()
        context_json = json.dumps(context_snapshot, default=str)
    else:
        context_json = None

    extraction_goal = prompt_engine.load_prompt(
        "conditional-prompt-branch-evaluation",
        conditions=rendered_expressions,
        context_json=context_json,
    )

    goal_token_count = count_tokens(extraction_goal)
    if goal_token_count > BRANCH_EVALUATION_GOAL_MAX_TOKENS:
        context_token_count = count_tokens(context_json) if context_json is not None else 0
        context_serialized_bytes = len(context_json.encode("utf-8")) if context_json is not None else 0
        LOG.warning(
            BRANCH_CONTEXT_CIRCUIT_BREAKER_EVENT,
            workflow_run_id=workflow_run_id,
            workflow_run_block_id=workflow_run_block_id,
            workflow_id=workflow_id,
            organization_id=organization_id,
            block_label=log_label,
            goal_token_count=goal_token_count,
            max_goal_tokens=BRANCH_EVALUATION_GOAL_MAX_TOKENS,
            reserved_tokens=BRANCH_EVALUATION_DOWNSTREAM_TOKEN_RESERVE,
            context_token_count=context_token_count,
            context_serialized_bytes=context_serialized_bytes,
            context_key_count=len(context_snapshot),
            top_context_contributors=_largest_branch_context_contributors(context_snapshot),
        )
        raise BranchEvaluationContextTooLargeError

    data_schema = _build_branch_evaluation_schema(len(branches))

    desc_suffix = extraction_description_suffix or f"{len(branches)} conditions"

    last_malformed: MalformedBranchEvaluationError | None = None
    for attempt in range(MAX_PROMPT_BRANCH_EVAL_ATTEMPTS):
        # Vary the goal on retries so the extraction cache key (which includes
        # data_extraction_goal) changes and we get a genuine re-roll instead of replaying
        # the malformed cached result that just failed validation.
        attempt_goal = extraction_goal
        if attempt > 0:
            attempt_goal = (
                f"{extraction_goal}\n\n"
                f"Re-evaluation attempt {attempt + 1}: return exactly {len(branches)} results, "
                f"one object per numbered condition, each tagged with its condition_index."
            )

        prompt_branch_eval_id = generate_random_string()
        output_param = OutputParameter(
            output_parameter_id=str(uuid.uuid4()),
            key=f"prompt_branch_eval_{prompt_branch_eval_id}",
            workflow_id=workflow_id,
            created_at=datetime.now(),
            modified_at=datetime.now(),
            parameter_type=ParameterType.OUTPUT,
            description=f"Conditional branch evaluation results ({desc_suffix})",
        )
        extraction_block = ExtractionBlock(
            label=f"prompt_branch_eval_{prompt_branch_eval_id}",
            data_extraction_goal=attempt_goal,
            data_schema=data_schema,
            output_parameter=output_param,
        )
        # The goal was fully rendered above; a second Jinja pass would resolve any `{{...}}` text
        # inlined from stored block outputs against the synthetic block's scope and fail (SKY-14080).
        # Unlike the loop-value synthetic, this block is NOT excluded from the engine A/B:
        # eligibility vets prompt-branch conditionals (v3_ab_ineligibility_reason), so the run's
        # resolved arm covers branch evaluation too.
        extraction_block.mark_data_extraction_goal_prerendered()

        LOG.info(
            "Conditional branch ExtractionBlock created (batched)",
            block_label=log_label,
            prompt_branch_eval_id=prompt_branch_eval_id,
            num_conditions=len(branches),
            resolved_engine=extraction_block.resolve_engine(workflow_run_id),
            attempt=attempt,
            extraction_goal_preview=attempt_goal[:500] if attempt_goal else None,
            has_browser_session=browser_session_id is not None,
            has_any_pure_natlang=has_any_pure_natlang,
            has_context=context_json is not None,
        )

        extraction_result = await extraction_block.execute(
            workflow_run_id=workflow_run_id,
            workflow_run_block_id=workflow_run_block_id,
            organization_id=organization_id,
            browser_session_id=browser_session_id,
        )

        if not extraction_result.success:
            # Extraction-level failures already retry at the step level inside the task;
            # surface them immediately rather than re-rolling the whole batch.
            LOG.error(
                "Conditional branch ExtractionBlock failed",
                block_label=log_label,
                failure_reason=extraction_result.failure_reason,
            )
            raise ConditionalBranchEvaluationError(
                f"Branch evaluation failed: "
                f"{extraction_result.failure_reason or 'Unknown error (no failure reason provided)'}"
            )

        raw_output_value = extraction_result.output_parameter_value
        try:
            results_array, llm_rendered_expressions, normalized_output = _align_branch_evaluations(
                output_value=raw_output_value,
                branches=branches,
                rendered_expressions=rendered_expressions,
            )
        except MalformedBranchEvaluationError as malformed:
            last_malformed = malformed
            LOG.warning(
                "Conditional branch evaluation output malformed",
                block_label=log_label,
                attempt=attempt,
                will_retry=attempt + 1 < MAX_PROMPT_BRANCH_EVAL_ATTEMPTS,
                error=str(malformed),
                raw_output=raw_output_value,
            )
            continue

        # Record the output parameter only for the attempt we actually accept, so a failed
        # attempt's payload never lands in the workflow context when a later attempt succeeds.
        if workflow_run_context:
            try:
                await extraction_block.record_output_parameter_value(
                    workflow_run_context=workflow_run_context,
                    workflow_run_id=workflow_run_id,
                    value=normalized_output,
                )
            except Exception:
                LOG.warning(
                    "Failed to record conditional branch evaluation output",
                    workflow_run_id=workflow_run_id,
                    block_label=log_label,
                    exc_info=True,
                )

        LOG.info(
            "Conditional branch evaluation results",
            block_label=log_label,
            results=results_array,
            llm_rendered_expressions=llm_rendered_expressions,
            attempt=attempt,
            raw_output=normalized_output,
        )
        return (results_array, llm_rendered_expressions, extraction_goal, normalized_output)

    LOG.error(
        "Conditional branch evaluation failed after retries",
        block_label=log_label,
        attempts=MAX_PROMPT_BRANCH_EVAL_ATTEMPTS,
        error=str(last_malformed),
    )
    raise ConditionalBranchEvaluationError(f"Conditional branch evaluation failed: {last_malformed}")


class ConditionalBlock(Block):
    """Branching block that selects the next block label based on list-ordered conditions."""

    # There is a mypy bug with Literal. Without the type: ignore, mypy will raise an error:
    # Parameter 1 of Literal[...] cannot be of type "Any"
    block_type: Literal[BlockType.CONDITIONAL] = BlockType.CONDITIONAL  # type: ignore

    branch_conditions: list[BranchCondition] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_branches(self) -> ConditionalBlock:
        if not self.branch_conditions:
            raise ValueError("Conditional blocks require at least one branch.")

        default_branches = [branch for branch in self.branch_conditions if branch.is_default]
        if len(default_branches) > 1:
            raise ValueError("Only one default branch is permitted per conditional block.")

        return self

    def get_all_parameters(
        self,
        workflow_run_id: str,  # noqa: ARG002 - preserved for interface compatibility
    ) -> list[PARAMETER_TYPE]:
        # BranchCriteria subclasses will surface their parameter dependencies once implemented.
        return []

    async def _evaluate_prompt_branches(
        self,
        *,
        branches: list[BranchCondition],
        evaluation_context: BranchEvaluationContext,
        workflow_run_id: str,
        workflow_run_block_id: str,
        organization_id: str | None = None,
        browser_session_id: str | None = None,
    ) -> tuple[list[bool], list[str], str | None, dict | None]:
        """
        Evaluate natural language branch conditions in batch.

        All prompt-based conditions are batched into ONE LLM call for performance.
        Jinja parts ({{ }}) are pre-rendered before sending to LLM.

        Evaluation strategy:
        - If any condition is pure natural language, use ExtractionBlock for browser/page context.
        - If all conditions contain Jinja and are pre-rendered, use direct LLM call (no browser context).

        Returns:
            A tuple of (results, rendered_expressions, extraction_goal, llm_response):
            - results: List of boolean results for each branch
            - rendered_expressions: List of expressions after Jinja pre-rendering
            - extraction_goal: The prompt sent to the LLM (for UI display)
            - llm_response: The raw LLM response for debugging
        """
        return await _evaluate_prompt_branch_conditions_batch(
            log_label=self.label,
            branches=branches,
            evaluation_context=evaluation_context,
            workflow_run_id=workflow_run_id,
            workflow_run_block_id=workflow_run_block_id,
            organization_id=organization_id,
            browser_session_id=browser_session_id,
            workflow_id=self.output_parameter.workflow_id,
            extraction_description_suffix=f"{len(branches)} conditions",
        )

    async def execute(  # noqa: D401
        self,
        workflow_run_id: str,
        workflow_run_block_id: str,
        organization_id: str | None = None,
        browser_session_id: str | None = None,
        **kwargs: dict,
    ) -> BlockResult:
        """
        Evaluate conditional branches and determine next block to execute.

        Returns a BlockResult with branch metadata in the output_parameter_value.
        """
        workflow_run_context = app.WORKFLOW_CONTEXT_MANAGER.get_workflow_run_context(workflow_run_id)
        evaluation_context = BranchEvaluationContext(
            workflow_run_context=workflow_run_context,
            block_label=self.label,
            template_renderer=(
                lambda potential_template: self.format_block_parameter_template_from_workflow_run_context(
                    potential_template,
                    workflow_run_context,
                )
            )
            if workflow_run_context
            else None,
        )

        matched_branch = None
        # A branch whose evaluation raised is treated as non-matching rather than as a stop signal,
        # so an unevaluated later branch can still win. The first error is kept for reporting.
        prompt_batch_error: str | None = None
        first_evaluation_error: str | None = None

        # Track all branch evaluations for UI display
        branch_evaluations_list: list[dict] = []
        prompt_rendered_by_id: dict[str, str] = {}

        natural_language_branches = [
            branch for branch in self.ordered_branches if isinstance(branch.criteria, PromptBranchCriteria)
        ]
        prompt_results_by_id: dict[str, bool] = {}
        prompt_llm_response: dict | None = None
        prompt_extraction_goal: str | None = None
        if natural_language_branches:
            try:
                (
                    prompt_results,
                    prompt_rendered_expressions,
                    prompt_extraction_goal,
                    prompt_llm_response,
                ) = await self._evaluate_prompt_branches(
                    branches=natural_language_branches,
                    evaluation_context=evaluation_context,
                    workflow_run_id=workflow_run_id,
                    workflow_run_block_id=workflow_run_block_id,
                    organization_id=organization_id,
                    browser_session_id=browser_session_id,
                )
                prompt_results_by_id = {
                    branch.id: result for branch, result in zip(natural_language_branches, prompt_results, strict=False)
                }
                prompt_rendered_by_id = {
                    branch.id: rendered
                    for branch, rendered in zip(natural_language_branches, prompt_rendered_expressions, strict=False)
                }
            except BranchEvaluationContextTooLargeError as exc:
                prompt_batch_error = get_user_facing_exception_message(exc)
            except Exception as exc:
                prompt_batch_error = f"Failed to evaluate natural language branches: {str(exc)}"
                LOG.error(
                    "Failed to evaluate natural language branches",
                    block_label=self.label,
                    error=str(exc),
                    exc_info=True,
                )

        for idx, branch in enumerate(self.ordered_branches):
            branch_eval: dict = {
                "branch_id": branch.id,
                "branch_index": idx,
                "criteria_type": branch.criteria.criteria_type if branch.criteria else None,
                "original_expression": branch.criteria.expression if branch.criteria else None,
                "rendered_expression": None,
                "result": None,
                "is_matched": False,
                "is_default": branch.is_default,
                "next_block_label": branch.next_block_label,
                "error": None,
            }

            # Handle default branch (no criteria to evaluate)
            if branch.criteria is None:
                # Default branch - only matched if no other branch matches
                branch_evaluations_list.append(branch_eval)
                continue

            if branch.criteria.criteria_type == "prompt":
                if prompt_batch_error:
                    branch_eval["error"] = prompt_batch_error
                    branch_evaluations_list.append(branch_eval)
                    if first_evaluation_error is None:
                        first_evaluation_error = prompt_batch_error
                    continue
                prompt_result = prompt_results_by_id.get(branch.id)
                rendered_expr = prompt_rendered_by_id.get(branch.id)
                branch_eval["rendered_expression"] = rendered_expr
                if prompt_result is None:
                    missing_result_error = "Missing result for natural language branch evaluation"
                    branch_eval["error"] = missing_result_error
                    LOG.error(
                        "Missing prompt evaluation result",
                        block_label=self.label,
                        branch_index=idx,
                        branch_id=branch.id,
                    )
                    branch_evaluations_list.append(branch_eval)
                    if first_evaluation_error is None:
                        first_evaluation_error = missing_result_error
                    continue
                branch_eval["result"] = prompt_result
                branch_evaluations_list.append(branch_eval)
                if prompt_result:
                    matched_branch = branch
                    branch_eval["is_matched"] = True
                    LOG.info(
                        "Conditional natural language branch matched",
                        block_label=self.label,
                        branch_index=idx,
                        next_block_label=branch.next_block_label,
                    )
                    break
                continue

            # Jinja template branch
            try:
                # Render the expression for UI display - substitute variables without evaluating
                rendered_expression = _render_jinja_expression_for_display(
                    expression=branch.criteria.expression,
                    context_values=evaluation_context.workflow_run_context.values
                    if evaluation_context.workflow_run_context
                    else {},
                    block_label=self.label,
                )
                branch_eval["rendered_expression"] = rendered_expression

                result = await branch.criteria.evaluate(evaluation_context)
                branch_eval["result"] = result
                branch_evaluations_list.append(branch_eval)

                if result:
                    matched_branch = branch
                    branch_eval["is_matched"] = True
                    LOG.info(
                        "Conditional branch matched",
                        block_label=self.label,
                        branch_index=idx,
                        next_block_label=branch.next_block_label,
                    )
                    break
            except Exception as exc:
                if first_evaluation_error is None:
                    first_evaluation_error = f"Failed to evaluate branch {idx} for {self.label}: {str(exc)}"
                branch_eval["error"] = str(exc)
                branch_eval["result"] = None
                branch_evaluations_list.append(branch_eval)
                LOG.error(
                    "Failed to evaluate conditional branch",
                    block_label=self.label,
                    branch_index=idx,
                    error=str(exc),
                    exc_info=True,
                )
                continue

        failure_reason: str | None = None
        evaluation_error: str | None = None

        if matched_branch is None:
            matched_branch = self.get_default_branch()
            if matched_branch is not None:
                for eval_entry in branch_evaluations_list:
                    if eval_entry["branch_id"] == matched_branch.id:
                        eval_entry["is_matched"] = True
                        break
            elif first_evaluation_error is not None:
                failure_reason = first_evaluation_error

        if matched_branch is not None and first_evaluation_error is not None:
            # Routed despite a branch that could not be evaluated: the taken branch may not be the
            # one the author's ordering intended, so surface the error alongside the route.
            evaluation_error = first_evaluation_error
            LOG.warning(
                "Conditional block routed despite a failed branch evaluation",
                workflow_run_id=workflow_run_id,
                block_label=self.label,
                evaluation_error=evaluation_error,
                next_block_label=matched_branch.next_block_label,
                took_default_branch=matched_branch.is_default,
            )

        matched_index = self.ordered_branches.index(matched_branch) if matched_branch in self.ordered_branches else None
        next_block_label = matched_branch.next_block_label if matched_branch else None
        executed_branch_id = matched_branch.id if matched_branch else None

        # Extract execution details for frontend display
        executed_branch_expression: str | None = None
        executed_branch_result: bool | None = None
        executed_branch_next_block: str | None = None

        if matched_branch:
            executed_branch_next_block = matched_branch.next_block_label
            if matched_branch.is_default:
                # Default/else branch - no expression to evaluate
                executed_branch_expression = None
                executed_branch_result = None
            elif matched_branch.criteria:
                # Regular condition branch - it matched
                executed_branch_expression = matched_branch.criteria.expression
                executed_branch_result = True

        branch_metadata: BlockMetadata = {
            "branch_taken": next_block_label,
            "branch_index": matched_index,
            "branch_id": executed_branch_id,
            "branch_description": matched_branch.description if matched_branch else None,
            "criteria_type": matched_branch.criteria.criteria_type
            if matched_branch and matched_branch.criteria
            else None,
            "criteria_expression": matched_branch.criteria.expression
            if matched_branch and matched_branch.criteria
            else None,
            "next_block_label": next_block_label,
            # Detailed evaluation info for all branches (rendered_expression trimmed/capped — SKY-9779)
            "evaluations": _trim_branch_evaluations(branch_evaluations_list) if branch_evaluations_list else None,
            # Raw LLM response for debugging prompt-based evaluations (masked for secrets, capped)
            "llm_response": _cap_debug_field(
                workflow_run_context.mask_secrets_in_data(prompt_llm_response)
                if workflow_run_context and prompt_llm_response
                else prompt_llm_response
            ),
            # The exact prompt sent to LLM for debugging (masked for secrets, capped)
            "llm_prompt": _cap_debug_field(
                workflow_run_context.mask_secrets_in_data(prompt_extraction_goal)
                if workflow_run_context and prompt_extraction_goal
                else prompt_extraction_goal
            ),
        }
        if evaluation_error is not None:
            branch_metadata["evaluation_error"] = evaluation_error

        status = BlockStatus.completed
        success = True

        if failure_reason:
            status = BlockStatus.failed
            success = False
        elif matched_branch is None:
            failure_reason = "No conditional branch matched and no default branch configured"
            status = BlockStatus.failed
            success = False

        if workflow_run_context:
            workflow_run_context.update_block_metadata(self.label, branch_metadata)
            try:
                await self.record_output_parameter_value(
                    workflow_run_context=workflow_run_context,
                    workflow_run_id=workflow_run_id,
                    value=branch_metadata,
                )
            except Exception as exc:
                LOG.warning(
                    "Failed to record branch metadata as output parameter",
                    workflow_run_id=workflow_run_id,
                    block_label=self.label,
                    error=str(exc),
                )

        block_result = await self.build_block_result(
            success=success,
            failure_reason=failure_reason,
            output_parameter_value=branch_metadata,
            status=status,
            workflow_run_block_id=workflow_run_block_id,
            organization_id=organization_id,
            executed_branch_id=executed_branch_id,
            executed_branch_expression=executed_branch_expression,
            executed_branch_result=executed_branch_result,
            executed_branch_next_block=executed_branch_next_block,
        )
        return block_result

    @property
    def ordered_branches(self) -> list[BranchCondition]:
        """Convenience accessor that returns branches in author-specified list order."""
        return list(self.branch_conditions)

    def get_default_branch(self) -> BranchCondition | None:
        """Return the default/else branch when configured."""
        return next((branch for branch in self.branch_conditions if branch.is_default), None)


class WorkflowTriggerBlock(Block):
    # There is a mypy bug with Literal. Without the type: ignore, mypy will raise an error:
    # Parameter 1 of Literal[...] cannot be of type "Any"
    block_type: Literal[BlockType.WORKFLOW_TRIGGER] = BlockType.WORKFLOW_TRIGGER  # type: ignore

    # The permanent ID of the target workflow to trigger
    workflow_permanent_id: str
    # Parameters/payload to pass to the triggered workflow
    payload: dict[str, Any] | None = None
    # Whether to wait for the triggered workflow to complete
    wait_for_completion: bool = True
    # Optional browser session ID for the triggered workflow
    browser_session_id: str | None = None
    # When True, the child workflow inherits the parent's browser session
    use_parent_browser_session: bool = False
    # Parameters for Jinja2 template interpolation
    parameters: list[PARAMETER_TYPE] = []

    MAX_TRIGGER_DEPTH: ClassVar[int] = 10

    TEMPLATABLE_FIELDS: ClassVar[frozenset[str]] = frozenset({"browser_session_id", "payload", "workflow_permanent_id"})

    def get_all_parameters(
        self,
        workflow_run_id: str,
    ) -> list[PARAMETER_TYPE]:
        return self.parameters

    async def _check_trigger_depth(self, workflow_run_id: str) -> int:
        """Check the nesting depth of workflow triggers to prevent infinite recursion.

        Note: This depth guard walks the parent_workflow_run_id chain, which is only
        populated for synchronous triggers. For async (fire-and-forget) dispatch, the
        parent may have already completed before the child runs, so circular async
        chains (A->B->A) are only blocked while A is still running. A full
        visited-workflow guard would require persistent state and is left as a future
        enhancement.
        """
        depth = 0
        current_run_id: str | None = workflow_run_id
        while current_run_id:
            if depth >= self.MAX_TRIGGER_DEPTH:
                raise InvalidWorkflowDefinition(
                    f"Workflow trigger depth exceeds maximum of {self.MAX_TRIGGER_DEPTH}. "
                    "This may indicate a circular workflow trigger chain."
                )
            run = await app.DATABASE.workflow_runs.get_workflow_run(current_run_id)
            if not run or not run.parent_workflow_run_id:
                break
            current_run_id = run.parent_workflow_run_id
            depth += 1
        return depth

    def _render_template_value(
        self,
        value: str,
        workflow_run_context: WorkflowRunContext,
    ) -> Any:
        """Render a single Jinja2 template string, handling the | json filter marker."""
        credential_id = self._resolve_exact_credential_id_payload_template(value, workflow_run_context)
        if credential_id is not None:
            return credential_id

        rendered = self.render_templatable_field(
            "payload", value, workflow_run_context, env=jinja_json_finalize_strict_env
        )
        if rendered.startswith(_JSON_TYPE_MARKER) and rendered.endswith(_JSON_TYPE_MARKER):
            json_str = rendered[len(_JSON_TYPE_MARKER) : -len(_JSON_TYPE_MARKER)]
            try:
                return json.loads(json_str)
            except json.JSONDecodeError:
                raise FailedToFormatJinjaStyleParameter(value, f"Raw JSON filter produced invalid JSON: {json_str}")
        elif _JSON_TYPE_MARKER in rendered:
            raise FailedToFormatJinjaStyleParameter(
                value,
                "The '| json' filter can only be used for complete value replacement. "
                "It cannot be combined with other text (e.g., 'prefix-{{ val | json }}'). "
                "Remove the surrounding text or remove the '| json' filter.",
            )
        return rendered

    def _resolve_exact_credential_id_payload_template(
        self,
        value: str,
        workflow_run_context: WorkflowRunContext,
    ) -> str | None:
        """Preserve raw credential IDs when a trigger payload forwards a credential input."""
        try:
            parsed = jinja_sandbox_env.parse(value)
        except TemplateSyntaxError:
            return None

        if len(parsed.body) != 1 or not isinstance(parsed.body[0], nodes.Output):
            return None

        rendered_nodes = [
            node
            for node in parsed.body[0].nodes
            if not (isinstance(node, nodes.TemplateData) and not node.data.strip())
        ]
        if len(rendered_nodes) != 1:
            return None
        expression = rendered_nodes[0]
        if isinstance(expression, nodes.Name):
            parameter_key = expression.name
        elif (
            isinstance(expression, nodes.Filter)
            and expression.name == "json"
            and isinstance(expression.node, nodes.Name)
        ):
            parameter_key = expression.node.name
        else:
            return None

        get_credential_id = getattr(workflow_run_context, "get_resolved_credential_parameter_id", None)
        if not callable(get_credential_id):
            return None

        credential_id = get_credential_id(parameter_key)
        if isinstance(credential_id, str) and credential_id:
            return credential_id

        # An at-will credential (credential_id type, no default) that was not provided has no
        # resolved id and its value is None. Forward it as an empty string so a trigger payload
        # renders "" rather than the literal text "None" (which the child would then try to
        # validate as a credential id).
        parameter = workflow_run_context.parameters.get(parameter_key)
        if (
            isinstance(parameter, WorkflowParameter)
            and parameter.workflow_parameter_type == WorkflowParameterType.CREDENTIAL_ID
            and workflow_run_context.values.get(parameter_key) is None
        ):
            return ""
        return None

    def _render_scalar_with_path(
        self,
        value: str,
        workflow_run_context: WorkflowRunContext,
        path: str,
    ) -> Any:
        # Wrap render errors with JSON-pointer-style payload path + template so
        # the failure reason surfaces where to look in the workflow.
        try:
            return self._render_template_value(value, workflow_run_context)
        except PayloadTemplateRenderError:
            raise
        except Exception as exc:
            raise PayloadTemplateRenderError(path=path, template=value, original=exc) from exc

    def _render_templates_in_payload(
        self,
        payload: dict[str, Any],
        workflow_run_context: WorkflowRunContext,
        _path: str = "payload",
    ) -> dict[str, Any]:
        """Recursively render Jinja2 templates in payload values."""
        resolved: dict[str, Any] = {}
        for key, value in payload.items():
            current_path = f"{_path}{_format_payload_path_segment(key)}"
            if isinstance(value, str):
                resolved[key] = self._render_scalar_with_path(value, workflow_run_context, current_path)
            elif isinstance(value, dict):
                resolved[key] = self._render_templates_in_payload(value, workflow_run_context, current_path)
            elif isinstance(value, list):
                resolved[key] = self._render_templates_in_list(value, workflow_run_context, current_path)
            else:
                resolved[key] = value
        return resolved

    def _render_templates_in_list(
        self,
        items: list[Any],
        workflow_run_context: WorkflowRunContext,
        _path: str = "payload",
    ) -> list[Any]:
        """Recursively render Jinja2 templates in list items (strings, nested dicts, and nested lists)."""
        result: list[Any] = []
        for idx, item in enumerate(items):
            current_path = f"{_path}[{idx}]"
            if isinstance(item, str):
                result.append(self._render_scalar_with_path(item, workflow_run_context, current_path))
            elif isinstance(item, dict):
                result.append(self._render_templates_in_payload(item, workflow_run_context, current_path))
            elif isinstance(item, list):
                result.append(self._render_templates_in_list(item, workflow_run_context, current_path))
            else:
                result.append(item)
        return result

    def validate_payload_templates(self) -> None:
        """Parse-check every Jinja2 template in self.payload at workflow save time.

        Walks the payload mirroring _render_templates_in_payload so paths match the
        runtime PayloadTemplateRenderError format. On TemplateSyntaxError raises
        PayloadTemplateSyntaxError with block label, JSON-pointer key path, and
        the offending template string.
        """
        if not self.payload:
            return

        def _walk(value: Any, path: str) -> None:
            if isinstance(value, dict):
                for key, sub in value.items():
                    _walk(sub, f"{path}{_format_payload_path_segment(key)}")
            elif isinstance(value, list):
                for idx, sub in enumerate(value):
                    _walk(sub, f"{path}[{idx}]")
            elif isinstance(value, str):
                try:
                    jinja_sandbox_env.parse(value)
                except TemplateSyntaxError as exc:
                    raise PayloadTemplateSyntaxError(
                        block_label=self.label, path=path, template=value, original=exc
                    ) from exc

        _walk(self.payload, "payload")

    def format_potential_template_parameters(self, workflow_run_context: WorkflowRunContext) -> None:
        self.workflow_permanent_id = self.render_templatable_field(
            "workflow_permanent_id", self.workflow_permanent_id, workflow_run_context
        )
        if self.payload:
            self.payload = self._render_templates_in_payload(self.payload, workflow_run_context)
        if self.browser_session_id:
            self.browser_session_id = self.render_templatable_field(
                "browser_session_id", self.browser_session_id, workflow_run_context
            )

    async def execute(
        self,
        workflow_run_id: str,
        workflow_run_block_id: str,
        organization_id: str | None = None,
        browser_session_id: str | None = None,
        **kwargs: dict,
    ) -> BlockResult:
        from skyvern.forge.sdk.workflow.models.workflow import WorkflowRequestBody, WorkflowRunStatus  # noqa: PLC0415

        workflow_run_context = self.get_workflow_run_context(workflow_run_id)

        # Helper to record output and build a failed block result in one step.
        # This ensures downstream blocks referencing block_X_output see the
        # failure reason instead of "parameter not found".
        async def _fail(failure_reason: str) -> BlockResult:
            error_output = {"failure_reason": failure_reason}
            await self.record_output_parameter_value(workflow_run_context, workflow_run_id, error_output)
            return await self.build_block_result(
                success=False,
                failure_reason=failure_reason,
                output_parameter_value=error_output,
                status=BlockStatus.failed,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )

        # 1. Resolve Jinja2 templates
        try:
            self.format_potential_template_parameters(workflow_run_context)
        except Exception as e:
            return await self._template_format_failure_result(
                e,
                f"Failed to resolve templates: {str(e)}",
                workflow_run_context,
                workflow_run_id,
                workflow_run_block_id,
                organization_id,
            )

        resolved_workflow_permanent_id = self.workflow_permanent_id
        resolved_payload = self.payload

        # 2. Check recursion depth
        try:
            await self._check_trigger_depth(workflow_run_id)
        except InvalidWorkflowDefinition as e:
            return await _fail(str(e))

        # 3. Get the organization
        if not organization_id:
            return await _fail("organization_id is required for WorkflowTriggerBlock")
        organization = await app.DATABASE.organizations.get_organization(organization_id)
        if not organization:
            return await _fail(f"Organization {organization_id} not found")

        # 4. Resolve browser session
        # Browser session priority:
        # 1. Explicit browser_session_id configured on the block
        # 2. use_parent_browser_session → inherit parent's session (persistent
        #    or in-memory via self.pages[parent_workflow_run_id] lookup)
        # 3. Neither → for sync (wait_for_completion), create a fresh persistent
        #    session; for async (fire-and-forget), let the child's Temporal worker
        #    handle its own browser.
        created_fresh_session = False
        if self.browser_session_id:
            resolved_browser_session_id = self.browser_session_id
        elif self.use_parent_browser_session and browser_session_id:
            resolved_browser_session_id = browser_session_id
        elif self.use_parent_browser_session:
            # Parent uses an in-memory browser (no persistent session).
            # Pass None so the child inherits via the parent_workflow_run_id
            # lookup in get_or_create_for_workflow_run.
            resolved_browser_session_id = None
        elif self.wait_for_completion:
            # Sync mode: child runs inline in the same process, so it needs
            # its own persistent session to avoid sharing the parent's browser.
            parent_workflow_run = await app.DATABASE.workflow_runs.get_workflow_run(workflow_run_id)
            proxy_location = parent_workflow_run.proxy_location if parent_workflow_run else None
            try:
                child_browser_session = await app.PERSISTENT_SESSIONS_MANAGER.create_session(
                    organization_id=organization_id,
                    proxy_location=proxy_location,
                    timeout_minutes=30,
                )
                resolved_browser_session_id = child_browser_session.persistent_browser_session_id
                created_fresh_session = True
                LOG.info(
                    "Created fresh browser session for triggered workflow",
                    parent_workflow_run_id=workflow_run_id,
                    child_browser_session_id=resolved_browser_session_id,
                )
            except Exception as e:
                return await _fail(f"Failed to create browser session for triggered workflow: {str(e)}")
        else:
            # Async (fire-and-forget): the child runs in its own Temporal worker
            # and will create its own browser. No pre-creation needed.
            resolved_browser_session_id = None

        # 5. Execute based on wait mode
        output_data: dict[str, Any] = {}
        success = False
        if self.wait_for_completion:
            # Synchronous: setup + execute inline in the same process.
            workflow_request = WorkflowRequestBody(
                data=resolved_payload,
                browser_session_id=resolved_browser_session_id,
            )

            # Isolate the synchronous child workflow in a placeholder scope so
            # setup_workflow_run() can replace the current context without
            # flushing the parent's pending workflow_feature_flags summary.
            parent_context = skyvern_context.current()
            inherited_trigger_type = parent_context.trigger_type if parent_context else None
            with skyvern_context.scoped(
                skyvern_context.SkyvernContext(
                    run_id=parent_context.run_id if parent_context else None,
                    root_workflow_run_id=parent_context.root_workflow_run_id if parent_context else None,
                    copilot_session_id=parent_context.copilot_session_id if parent_context else None,
                    trigger_type=inherited_trigger_type,
                )
            ):
                # A self-created fresh session must be closed on every exit from this block —
                # including the setup-failure and sequential-credential fence early returns — so the
                # cleanup lives in a finally rather than only on the normal execute path below.
                triggered_run_id: str | None = None
                try:
                    try:
                        triggered_workflow_run = await app.WORKFLOW_SERVICE.setup_workflow_run(
                            request_id=None,
                            workflow_request=workflow_request,
                            workflow_permanent_id=resolved_workflow_permanent_id,
                            organization=organization,
                            parent_workflow_run_id=workflow_run_id,
                            ignore_inherited_workflow_system_prompt=self.ignore_workflow_system_prompt,
                            trigger_type=inherited_trigger_type,
                        )
                    except Exception as e:
                        error_msg = get_user_facing_exception_message(e)
                        return await _fail(f"Failed to setup triggered workflow run: {error_msg}")

                    triggered_run_id = triggered_workflow_run.workflow_run_id

                    # A synchronous child runs inline via execute_workflow below: it never queues, never
                    # gets a queued_at, and never reaches the Temporal V2 serialization gate. setup stamped
                    # its sequential_credential_id, but the gate filters queued_at IS NOT NULL, so a
                    # concurrent run sharing that credential would not see this child as a blocker. Fail
                    # closed before it uses the credential — the same fence the scheduled path applies.
                    if triggered_workflow_run.sequential_credential_id:
                        fence = SyncTriggeredSequentialCredentialUnsupported(triggered_run_id)
                        await app.WORKFLOW_SERVICE.mark_workflow_run_as_failed_if_not_final(
                            workflow_run_id=triggered_run_id,
                            failure_reason=str(fence),
                        )
                        return await _fail(str(fence))

                    LOG.info(
                        "Triggered workflow run (sync)",
                        parent_workflow_run_id=workflow_run_id,
                        triggered_workflow_run_id=triggered_run_id,
                        triggered_workflow_permanent_id=resolved_workflow_permanent_id,
                    )

                    try:
                        # The opt-out flag is persisted on the child's workflow_run row at
                        # spawn time (setup_workflow_run above), so execute_workflow reads
                        # it from the DB. This works identically for sync and async triggers.
                        final_run = await app.WORKFLOW_SERVICE.execute_workflow(
                            workflow_run_id=triggered_run_id,
                            api_key=None,
                            organization=organization,
                            browser_session_id=resolved_browser_session_id,
                        )
                        success = final_run.status == WorkflowRunStatus.completed
                        output_data = {
                            "workflow_run_id": triggered_run_id,
                            "workflow_permanent_id": resolved_workflow_permanent_id,
                            "status": str(final_run.status),
                            "failure_reason": final_run.failure_reason,
                        }
                        # Include the child workflow's output parameters so downstream
                        # blocks can reference them (e.g. block_3_output.outputs.block_2_output)
                        try:
                            child_output_params = (
                                await app.WORKFLOW_SERVICE.get_output_parameter_workflow_run_output_parameter_tuples(
                                    workflow_id=final_run.workflow_id,
                                    workflow_run_id=triggered_run_id,
                                )
                            )
                            child_outputs: dict[str, Any] = {}
                            for output_param, run_output_param in child_output_params:
                                child_outputs[output_param.key] = run_output_param.value
                            output_data["outputs"] = child_outputs
                        except Exception:
                            LOG.warning(
                                "Failed to fetch child workflow outputs",
                                triggered_workflow_run_id=triggered_run_id,
                                exc_info=True,
                            )
                    except Exception as e:
                        error_msg = get_user_facing_exception_message(e)
                        output_data = {
                            "workflow_run_id": triggered_run_id,
                            "workflow_permanent_id": resolved_workflow_permanent_id,
                            "status": "failed",
                            "failure_reason": f"Triggered workflow execution failed: {error_msg}",
                        }
                        success = False
                finally:
                    if created_fresh_session and resolved_browser_session_id:
                        try:
                            await app.PERSISTENT_SESSIONS_MANAGER.close_session(
                                organization_id,
                                resolved_browser_session_id,
                                reason=(
                                    BrowserSessionCloseReason.user_requested
                                    if success
                                    else BrowserSessionCloseReason.aborted
                                ),
                            )
                        except Exception:
                            LOG.warning(
                                "Failed to close child browser session",
                                child_browser_session_id=resolved_browser_session_id,
                                triggered_workflow_run_id=triggered_run_id,
                                exc_info=True,
                            )
        else:
            # Fire and forget: dispatch the child workflow via Temporal so it
            # gets its own independent worker process. This ensures the child
            # survives even if the parent workflow finishes first.
            # NOTE: This path requires Temporal (cloud). On self-hosted
            # (BackgroundTaskExecutor), the workflow run record is created but
            # execution is silently skipped because background_tasks=None.
            from skyvern.services.workflow_service import run_workflow  # noqa: PLC0415

            workflow_request = WorkflowRequestBody(
                data=resolved_payload,
                browser_session_id=resolved_browser_session_id,
            )
            try:
                # ``run_workflow`` persists this flag to the child's
                # workflow_run row via its internal setup_workflow_run call,
                # then dispatches to Temporal without passing the flag
                # separately; the worker reads it back from the DB inside
                # ``execute_workflow``. Symmetric with the sync branch above
                # — the flag is written once, at spawn time, for both paths.
                async_parent_context = skyvern_context.current()
                async_inherited_trigger_type = async_parent_context.trigger_type if async_parent_context else None
                triggered_workflow_run = await run_workflow(
                    workflow_id=resolved_workflow_permanent_id,
                    organization=organization,
                    workflow_request=workflow_request,
                    request=None,
                    background_tasks=None,
                    parent_workflow_run_id=workflow_run_id,
                    ignore_inherited_workflow_system_prompt=self.ignore_workflow_system_prompt,
                    trigger_type=async_inherited_trigger_type,
                )
            except Exception as e:
                error_msg = get_user_facing_exception_message(e)
                return await _fail(f"Failed to dispatch triggered workflow: {error_msg}")

            triggered_run_id = triggered_workflow_run.workflow_run_id

            LOG.info(
                "Async workflow dispatch succeeded (via Temporal)",
                parent_workflow_run_id=workflow_run_id,
                triggered_workflow_run_id=triggered_run_id,
                triggered_workflow_permanent_id=resolved_workflow_permanent_id,
            )
            output_data = {
                "workflow_run_id": triggered_run_id,
                "workflow_permanent_id": resolved_workflow_permanent_id,
                "status": "queued",
            }
            success = True

        await self.record_output_parameter_value(workflow_run_context, workflow_run_id, output_data)

        return await self.build_block_result(
            success=success,
            failure_reason=output_data.get("failure_reason") if not success else None,
            output_parameter_value=output_data,
            status=BlockStatus.completed if success else BlockStatus.failed,
            workflow_run_block_id=workflow_run_block_id,
            organization_id=organization_id,
        )


_TASK_V3_SUPPORTED_BLOCK_TYPES = frozenset(
    {
        BlockType.TASK,
        BlockType.NAVIGATION,
        BlockType.LOGIN,
        BlockType.ACTION,
        BlockType.VALIDATION,
        BlockType.EXTRACTION,
        BlockType.FILE_DOWNLOAD,
    }
)


# Task blocks whose engine field never reaches a dispatch decision: a GOTO_URL block's task has no
# goal or criterion, so execute_step completes it before the v3 gate, and HumanInteractionBlock
# overrides execute and never calls execute_step at all. They neither qualify nor disqualify a run.
_ENGINE_INERT_BLOCK_TYPES = frozenset({BlockType.GOTO_URL, BlockType.HUMAN_INTERACTION})


def _task_block_supports_v3(task_block: BaseTaskBlock) -> bool:
    """Whether a workflow task block is eligible to dispatch to the Task V3 native engine."""
    if task_block.block_type == BlockType.VALIDATION and task_block.complete_on_download:
        # A validation block never acts on the page, so it has no way to trigger the download it
        # would complete on.
        return False
    return task_block.block_type in _TASK_V3_SUPPORTED_BLOCK_TYPES


class V3AbIneligibleReason(StrEnum):
    """The closed set of reasons a run is excluded from the workflow-block engine A/B.

    Parity with the bare-task route reason (``RunRouteReason`` in ``cloud/agent_functions.py``):
    a closed set an operator can slice logs by, instead of a bare boolean that answers "is
    eligible" but not "why not."
    """

    script_run = "script_run"
    pinned_engine = "pinned_engine"
    unsupported_block = "unsupported_block"
    block_totp_verification_url = "block_totp_verification_url"
    no_reroutable_blocks = "no_reroutable_blocks"


def v3_ab_ineligibility_reason(blocks: list[BlockTypeVar], *, is_script_run: bool) -> V3AbIneligibleReason | None:
    """Why a whole workflow run may not be rerouted onto v3 by the A/B, or None if it may.

    Eligibility is a property of the RUN, not of a block: a run whose blocks disagreed about the
    engine would drive one browser session with two engines, so no per-run outcome would be
    attributable to either arm. One participating block v3 cannot execute therefore disqualifies
    the run, and so does one block pinned to a non-default engine -- that block is honored
    as-authored in both arms, but it would leave the control arm mixed.

    ``blocks`` must be the flattened definition (``get_all_blocks``), i.e. the superset of task
    blocks the run could reach. A ``block_labels`` re-run executes only a whitelisted subset of
    that definition but is still judged against all of it, so one ineligible block elsewhere in the
    workflow keeps the re-run on control. That is deliberate: scoping eligibility to the subset
    would have to reproduce exactly which blocks the whitelist reaches, and getting it wrong is the
    mixed-arm run this predicate exists to prevent -- and a partial re-run is not comparable to a
    full run in the cohort anyway.

    Script runs are excluded: their blocks execute as cached code and never reach engine dispatch,
    so treatment would land only on the ai_fallback subset -- the blocks that already failed cached
    execution.
    """
    if is_script_run:
        return V3AbIneligibleReason.script_run
    reroutable_blocks = 0
    for block in blocks:
        if isinstance(block, ConditionalBlock):
            # A prompt-criteria branch evaluates through a synthetic extraction block that follows
            # the run's arm, so it is a rerouted surface this predicate must count; jinja-only
            # conditionals are pure control flow and stay invisible to the A/B. Both this type and
            # the while-loop below skip the pinned-engine/totp checks: neither exposes those fields.
            if any(isinstance(branch.criteria, PromptBranchCriteria) for branch in block.branch_conditions):
                reroutable_blocks += 1
            continue
        if isinstance(block, WhileLoopBlock):
            # A while-loop's prompt condition evaluates through the same synthetic extraction path
            # as a conditional's prompt branch, so it is counted the same way.
            if isinstance(block.condition, PromptBranchCriteria):
                reroutable_blocks += 1
            continue
        if not isinstance(block, BaseTaskBlock):
            continue
        if block.block_type in _ENGINE_INERT_BLOCK_TYPES:
            continue
        if block.engine != RunEngine.skyvern_v1:
            return V3AbIneligibleReason.pinned_engine
        if not _task_block_supports_v3(block):
            return V3AbIneligibleReason.unsupported_block
        # A/B rerouting never admits a run with a verification-URL block (a block explicitly pinned to
        # v3 is honored as requested); bare tasks with a verification URL are rerouted. Block-run
        # code/budget dynamics are unmeasured (SKY-14816).
        if block.totp_verification_url:
            return V3AbIneligibleReason.block_totp_verification_url
        reroutable_blocks += 1
    # A run with nothing to reroute would be bucketed and recorded as an exposure while both arms
    # execute identically, diluting the experiment.
    if reroutable_blocks == 0:
        return V3AbIneligibleReason.no_reroutable_blocks
    return None


def run_is_eligible_for_v3_ab(blocks: list[BlockTypeVar], *, is_script_run: bool) -> bool:
    """Whether a whole workflow run may be rerouted onto v3 by the A/B; see v3_ab_ineligibility_reason."""
    return v3_ab_ineligibility_reason(blocks, is_script_run=is_script_run) is None


def get_all_blocks(blocks: list[BlockTypeVar]) -> list[BlockTypeVar]:
    """
    Recursively get "all blocks" in a workflow definition.

    Blocks can be nested via ForLoop and WhileLoop blocks. This function returns
    all blocks, flattened.
    """

    all_blocks: list[BlockTypeVar] = []

    for block in blocks:
        all_blocks.append(block)

        if block.block_type in (BlockType.FOR_LOOP, BlockType.WHILE_LOOP):
            nested_blocks = get_all_blocks(block.loop_blocks)
            all_blocks.extend(nested_blocks)

    return all_blocks


# Late import: google_sheets_blocks imports Block from this module, so top-level import would cycle.
from skyvern.forge.sdk.workflow.models.data_export_block import DataExportBlock  # noqa: E402
from skyvern.forge.sdk.workflow.models.email_inbox_block import EmailInboxBlock  # noqa: E402
from skyvern.forge.sdk.workflow.models.google_sheets_blocks import (  # noqa: E402
    GoogleSheetsReadBlock,
    GoogleSheetsWriteBlock,
)
from skyvern.forge.sdk.workflow.models.pdf_fill_block import PdfFillBlock  # noqa: E402
from skyvern.forge.sdk.workflow.models.split_pdf_block import SplitPdfBlock  # noqa: E402

BlockSubclasses = Union[
    ConditionalBlock,
    ForLoopBlock,
    WhileLoopBlock,
    TaskBlock,
    CodeBlock,
    TextPromptBlock,
    DownloadToS3Block,
    UploadToS3Block,
    SendEmailBlock,
    FileParserBlock,
    PDFParserBlock,
    ValidationBlock,
    ActionBlock,
    NavigationBlock,
    ExtractionBlock,
    LoginBlock,
    WaitBlock,
    HumanInteractionBlock,
    FileDownloadBlock,
    UrlBlock,
    TaskV2Block,
    FileUploadBlock,
    HttpRequestBlock,
    PrintPageBlock,
    WorkflowTriggerBlock,
    GoogleSheetsReadBlock,
    EmailInboxBlock,
    GoogleSheetsWriteBlock,
    PdfFillBlock,
    SplitPdfBlock,
    DataExportBlock,
]
BlockTypeVar = Annotated[BlockSubclasses, Field(discriminator="block_type")]


BranchCriteriaSubclasses = Union[JinjaBranchCriteria, PromptBranchCriteria]
BranchCriteriaTypeVar = Annotated[BranchCriteriaSubclasses, Field(discriminator="criteria_type")]


def resolve_conditional_merge_edges(
    blocks: list[BlockTypeVar],
    label_to_block: dict[str, BlockTypeVar],
    default_next_map: dict[str, str | None],
) -> None:
    """Point each conditional branch chain's terminal block at the conditional's successor (merge point).

    SKY-8571: iterates to convergence so an outer conditional patched on one pass can let an inner
    conditional's branch terminals be patched on the next. Mutates default_next_map in place.
    """
    changed = True
    while changed:
        changed = False
        for block in blocks:
            if not isinstance(block, ConditionalBlock):
                continue
            successor = default_next_map.get(block.label)
            if not successor:
                continue
            for branch in block.ordered_branches:
                target = branch.next_block_label
                if not target or target == successor:
                    continue
                cur: str | None = target
                visited: set[str] = set()
                while cur and cur in label_to_block and cur not in visited:
                    if cur == successor:
                        break
                    visited.add(cur)
                    nxt = default_next_map.get(cur)
                    if nxt is None:
                        default_next_map[cur] = successor
                        changed = True
                        break
                    cur = nxt
