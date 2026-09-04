import asyncio
import contextlib
import copy
import json
import math
import os
import re
import shutil
import tempfile
import time
import urllib.parse
import uuid
from collections import deque
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable, List, NamedTuple, TypedDict, TypeGuard, cast

import structlog
from cachetools import TTLCache
from fuzzysearch import find_near_matches
from opentelemetry import trace as otel_trace
from playwright._impl._errors import Error as PlaywrightError
from playwright.async_api import Download, FileChooser, Frame, Locator, Page, Request, Response
from pydantic import BaseModel, field_validator

from skyvern.config import settings
from skyvern.constants import (
    AUTO_COMPLETION_POTENTIAL_VALUES_COUNT,
    BROWSER_DOWNLOAD_MAX_WAIT_TIME,
    BROWSER_DOWNLOAD_NO_SIGNAL_GRACE_TIME,
    BROWSER_DOWNLOAD_TIMEOUT,
    BROWSER_DOWNLOADING_SUFFIX,
    DROPDOWN_MENU_MAX_DISTANCE,
    SKYVERN_ID_ATTR,
    TEXT_PRESS_MAX_LENGTH,
)
from skyvern.core.script_generations.fuzzy_matcher import match_option_exact_or_stem
from skyvern.errors.errors import TOTPExpiredError, UserDefinedError, filter_to_user_defined_codes
from skyvern.exceptions import (
    ActionExecutionTimeout,
    BlockedHost,
    CaptchaSolveError,
    CardNumberInputMismatch,
    EmptySelect,
    ErrEmptyTweakValue,
    ErrFoundSelectableElement,
    FailedToClearInputField,
    FailedToFetchSecret,
    FailedToTakeScreenshot,
    FailToClick,
    FailToHover,
    FailToSelectByIndex,
    FailToSelectByLabel,
    FailToSelectByValue,
    FreeTextInputMismatch,
    HttpException,
    IllegitComplete,
    ImaginaryFileUrl,
    ImaginarySecretValue,
    InputToInvisibleElement,
    InputToReadonlyElement,
    InteractWithDisabledElement,
    InteractWithDropdownContainer,
    InvalidElementForTextInput,
    MissingElement,
    MissingElementDict,
    MissingElementInCSSMap,
    MissingFileUrl,
    MultipleElementsFound,
    NoAutoCompleteOptionMeetCondition,
    NoAvailableOptionFoundForCustomSelection,
    NoElementMatchedForTargetOption,
    NoIncrementalElementFoundForAutoCompletion,
    NoIncrementalElementFoundForCustomSelection,
    NoSuitableAutoCompleteOption,
    NoTOTPSecretFound,
    OptionIndexOutOfBound,
    PhoneNumberInputBrowserInteractionFailed,
    PhoneNumberInputBrowserValidityMismatch,
    PhoneNumberInputMismatch,
    ScreenshotTargetClosed,
    SecretInputMismatch,
    SkyvernException,
    SkyvernHTTPException,
    SkyvernPageAnalysisTimeout,
    UnresolvableHost,
)
from skyvern.experimentation.wait_utils import get_or_create_wait_config, get_wait_time
from skyvern.forge import app
from skyvern.forge.prompts import prompt_engine
from skyvern.forge.sdk.api.files import (
    GuardedFileFetchHopResult,
    calculate_sha256_for_file,
    check_downloading_files_and_wait_for_download_to_complete,
    fetch_file_bytes,
    get_download_dir,
    get_run_temp_dir,
    list_files_in_directory,
    resolve_run_download_id,
)
from skyvern.forge.sdk.api.llm.api_handler_factory import (
    LLMAPIHandlerFactory,
    LLMCallerManager,
    get_org_aware_primary_llm_api_handler,
    get_org_aware_secondary_llm_api_handler,
)
from skyvern.forge.sdk.api.llm.exceptions import LLMProviderError
from skyvern.forge.sdk.api.llm.schema_validator import extraction_shape_matches, validate_and_fill_extraction_result
from skyvern.forge.sdk.browser_action_preflight import preflight_action, preflight_derived_action
from skyvern.forge.sdk.cache import extraction_cache, extraction_shadow
from skyvern.forge.sdk.copilot.block_goal_wrapping import unwrap_goal_fields
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.hashing import diagnostic_fingerprint
from skyvern.forge.sdk.core.http_request_authorization import RedirectHopAuthorizer, deny_unenrolled_redirect_hop
from skyvern.forge.sdk.core.skyvern_context import PendingFileChooserListener, ensure_context
from skyvern.forge.sdk.db.datetime_utils import naive_utc_now
from skyvern.forge.sdk.event.factory import EventStrategyFactory
from skyvern.forge.sdk.experimentation.llm_prompt_config import (
    resolve_check_user_goal_handler,
    resolve_prompt_type_handler_with_override,
)
from skyvern.forge.sdk.experimentation.providers import BaseExperimentationProvider
from skyvern.forge.sdk.experimentation.slim_llm_output import get_slim_output_template_value
from skyvern.forge.sdk.models import Step
from skyvern.forge.sdk.schemas.tasks import Task
from skyvern.forge.sdk.services.bitwarden import BitwardenConstants
from skyvern.forge.sdk.services.credentials import (
    AzureVaultConstants,
    OnePasswordConstants,
    generate_totp_code,
    is_unresolved_totp_placeholder,
    is_unresolved_totp_value,
    parse_totp_config,
)
from skyvern.forge.sdk.settings_manager import SettingsManager
from skyvern.forge.sdk.trace import apply_context_attrs, traced, traced_span
from skyvern.services import service_utils
from skyvern.services.action_service import get_action_history
from skyvern.utils.contained_effects import contained_effect
from skyvern.utils.lean_html import apply_lean_to_tree
from skyvern.utils.prompt_engine import (
    CheckDateFormatResponse,
    CheckPhoneNumberFormatResponse,
    load_prompt_with_elements,
    load_prompt_with_elements_tracked,
)
from skyvern.utils.prompt_truncation import truncate_extraction_schema, truncate_previous_extracted_information
from skyvern.utils.url_validators import validate_fetch_url
from skyvern.webeye.actions import actions, handler_utils
from skyvern.webeye.actions.action_types import ActionType
from skyvern.webeye.actions.actions import (
    Action,
    ActionStatus,
    CheckboxAction,
    ClickAction,
    CompleteVerifyResult,
    DownloadFileAction,
    InputOrSelectContext,
    InputTextAction,
    PasteTextAction,
    ScrapeResult,
    SelectOption,
    SelectOptionAction,
    UploadFileAction,
    WebAction,
)
from skyvern.webeye.actions.responses import (
    STALE_TARGET_TOOL_RESULT,
    ActionAbort,
    ActionFailure,
    ActionResult,
    ActionSuccess,
    StaleActionAbort,
)
from skyvern.webeye.browser_artifacts import ActionDownloadObservation, DownloadBinding
from skyvern.webeye.browser_driver_errors import is_driver_error, is_driver_timeout_error
from skyvern.webeye.browser_engine import UNSET_SELECTION, BrowserEngineSelection, resolve_engine_selection_for_task
from skyvern.webeye.browser_factory import initialize_download_dir, read_download_failure, resolve_artifact_path
from skyvern.webeye.browser_state import BLANK_PAGE_URLS, BrowserState
from skyvern.webeye.cdp_download_interceptor import (
    BROWSER_DOWNLOAD_EVENT_ADMISSION_GRACE_SECONDS,
    DOWNLOAD_MIME_TYPES,
    MAX_FILE_SIZE_BYTES,
    begin_requested_download_for_context,
    download_filename_from_suffix,
    extract_filename,
    finish_requested_download_for_context,
    has_download_interceptor_for_context,
    is_download_response,
    normalize_download_filename,
    publish_download_bytes_for_context,
    redacted_exception_origin,
    settle_browser_downloads_for_context,
)
from skyvern.webeye.main_world_eval import evaluate_in_main_world
from skyvern.webeye.navigation import revalidate_redirect_chain
from skyvern.webeye.scraper.scraped_page import (
    CleanupElementTreeFunc,
    ElementTreeBuilder,
    ElementTreeFormat,
    ScrapedPage,
    json_to_html,
)
from skyvern.webeye.scraper.scraper import (
    IncrementalScrapePage,
    hash_element,
    structural_identity,
    trim_element_tree,
)
from skyvern.webeye.transient_page_observer import (
    TransientPageTextObserver,
    match_user_defined_errors_from_transient_text,
)
from skyvern.webeye.utils.document import get_main_document_loader_id
from skyvern.webeye.utils.dom import (
    COMMON_INPUT_TAGS,
    DomUtil,
    InteractiveElement,
    SkyvernElement,
    SkyvernOptionType,
    is_element_detached_error,
    is_incompatible_text_input_error,
    is_post_dispatch_click_timeout,
    resolve_locator,
)
from skyvern.webeye.utils.page import (
    SkyvernFrame,
    _all_page_frames,
    _blob_url_origin,
    apply_secret_visual_mask_to_active_element,
    install_blob_url_retention,
    probe_blob_action_freshness,
    take_element_screenshot,
    teardown_blob_url_retention,
)

LOG = structlog.get_logger()
_DISPATCHER_OWNED_INPUT_EXCEPTIONS = (
    MissingElement,
    MultipleElementsFound,
    LLMProviderError,
    ImaginarySecretValue,
    CaptchaSolveError,
    asyncio.TimeoutError,
)


async def _totp_window_sleep(delay: float) -> None:
    await asyncio.sleep(delay)


async def _upload_settle_sleep(delay: float) -> None:
    await asyncio.sleep(delay)


UPLOAD_PENDING_FOLLOWUP_MESSAGE = "Upload is not complete yet. Continue the upload flow."

DOWNLOAD_NOT_TRIGGERED_FOLLOWUP_MESSAGE = (
    "No file download was observed or credited after this action. "
    "If the goal still requires this file, keep trying to download it rather than reporting the goal complete."
)
DOWNLOAD_ABORTED_FAILURE_MESSAGE = (
    "The browser started this download but aborted it before any file was saved. "
    "The download link may have expired; regenerate it before trying the download again."
)
DOWNLOAD_OBSERVED_BUT_EMPTY_FOLLOWUP_MESSAGE = (
    "A file download was observed but no file could be saved from it. "
    "If the goal still requires this file, keep trying to download it rather than reporting the goal complete."
)
# Arming/tearing down blob URL retention is best-effort and must never stall a download action.
_BLOB_RETENTION_ARMING_TIMEOUT_SECONDS = 5.0
SENSITIVE_CLIPBOARD_CLEAR_FAILED_FOLLOWUP_MESSAGE = (
    "The sensitive paste completed, but the clipboard could not be cleared. "
    "Do not repeat the paste; stop and report the clipboard safety failure."
)
_PASTE_TEXT_CLIPBOARD_LOCK = asyncio.Lock()

FIX_TEL_INPUT_DIGIT_DROP_FLAG = "FIX_TEL_INPUT_DIGIT_DROP"
COLLAPSE_SELECT_FANOUT_FLAG = "COLLAPSE_SELECT_FANOUT"
COLLAPSE_CUSTOM_SELECT_FANOUT_FLAG = "COLLAPSE_CUSTOM_SELECT_FANOUT"
COLLAPSE_AUTOCOMPLETE_FANOUT_FLAG = "COLLAPSE_AUTOCOMPLETE_FANOUT"
COLLAPSE_XP_ASSIGNMENT_FLAG = "COLLAPSE_XP_ASSIGNMENT"
# Nested dispatch replaces contexts, so run-stickiness is process-local and keyed by run ID.
# Cross-process re-resolution is deterministic under stable flag config.
_COLLAPSE_XP_ASSIGNMENT_MEMO: TTLCache[str, bool] = TTLCache(maxsize=100_000, ttl=86_400)


def _is_selected_engine_timeout(exc: BaseException, engine_selection: BrowserEngineSelection | None) -> bool:
    """A driver-native timeout under THIS run's selected engine; any installed Playwright-family
    driver's timeout identity when no engine is pinned."""
    if engine_selection is not None:
        return engine_selection.is_engine_timeout_error(exc)
    return is_driver_timeout_error(exc)


def _is_selected_engine_error(exc: BaseException, engine_selection: BrowserEngineSelection | None) -> bool:
    """A driver-native error under THIS run's selected engine; any installed Playwright-family
    driver's error identity when no engine is pinned."""
    if engine_selection is not None:
        return engine_selection.is_engine_error(exc)
    return is_driver_error(exc)


class _CollapseGateResult(NamedTuple):
    family_enabled: bool
    assigned: bool | None
    gate_error: bool


class CustomSelectFamilyOutcome(StrEnum):
    llm_fallback_gate_error = "llm_fallback_gate_error"
    llm_fallback_eval_error = "llm_fallback_eval_error"
    llm_fallback_family_off = "llm_fallback_family_off"
    llm_fallback_control = "llm_fallback_control"
    llm_fallback_no_match = "llm_fallback_no_match"
    llm_fallback_match_unactionable = "llm_fallback_match_unactionable"
    llm_fallback_tier_excluded = "llm_fallback_tier_excluded"
    llm_fallback_execution_disabled = "llm_fallback_execution_disabled"
    llm_fallback_pre_click_error = "llm_fallback_pre_click_error"
    llm_fallback_reset_verified = "llm_fallback_reset_verified"
    llm_fallback_post_click_unverified = "llm_fallback_post_click_unverified"
    success_precommit = "success_precommit"
    success_verified = "success_verified"
    terminal_llm_fallback_exception = "terminal_llm_fallback_exception"
    terminal_post_click_exception = "terminal_post_click_exception"
    terminal_unverified_reset = "terminal_unverified_reset"
    terminal_unverified_click = "terminal_unverified_click"
    terminal_unverified_toggle = "terminal_unverified_toggle"


DOWNLOAD_EVENT_ACTIVE_DIR_GRACE_SECONDS = 60
DOWNLOAD_IN_FLIGHT_EXTENSION_MAX_SECONDS = 120
DOWNLOAD_IN_FLIGHT_POLL_INTERVAL_SECONDS = 1.0
# Pre-click provider-download baseline is a single metadata listing; cap it well under the action's
# own download budget so a slow provider cannot delay the click itself.
PROVIDER_DOWNLOAD_BASELINE_TIMEOUT_SECONDS = 10.0
# Cap the event-time blob read so a stalled read never consumes the whole download-wait budget;
# on timeout the save_as + fan-out fallback still gets its chance.
EAGER_BLOB_READ_TIMEOUT_SECONDS = 5.0
DOWNLOAD_DUPLICATE_STEM_SUFFIX_RE = re.compile(r"(?:\s+\(\d{1,3}\)|_\d{1,3})$")
SELECT_SHADOW_MATCH_APOSTROPHE_RE = re.compile(r"['`‘’]")
SELECT_SHADOW_MATCH_WORD_RE = re.compile(r"\w+")


def _select_shadow_match_enabled() -> bool:
    return settings.SKYVERN_SELECT_SHADOW_MATCH


def _is_totp_sentinel(value: Any) -> bool:
    return value in {BitwardenConstants.TOTP, OnePasswordConstants.TOTP, AzureVaultConstants.TOTP}


async def _apply_secret_visual_mask_if_needed(
    skyvern_element: SkyvernElement,
    *,
    workflow_run_id: str | None,
    is_secret_value: bool,
    is_totp_value: bool,
    is_totp_sequence: bool = False,
) -> None:
    if not settings.ENABLE_SECRET_VISUAL_MASKING or not app.WORKFLOW_CONTEXT_MANAGER.mask_secrets_enabled_for_run(
        workflow_run_id
    ):
        return
    if is_secret_value or is_totp_value or is_totp_sequence:
        await skyvern_element.apply_secret_visual_mask()


async def _apply_active_element_secret_visual_mask_if_needed(
    page: Page, text: str | None, workflow_run_id: str | None
) -> None:
    if (
        not settings.ENABLE_SECRET_VISUAL_MASKING
        or not text
        or not app.WORKFLOW_CONTEXT_MANAGER.mask_secrets_enabled_for_run(workflow_run_id)
    ):
        return
    try:
        secret_values = app.WORKFLOW_CONTEXT_MANAGER.get_secret_values_for_run(
            workflow_run_id,
            respect_artifact_redaction_flag=False,
        )
    except Exception:
        LOG.warning("Failed to resolve secret values for active element masking", exc_info=True)
        return
    if isinstance(secret_values, set) and text in secret_values:
        await apply_secret_visual_mask_to_active_element(page)


def _normalize_select_shadow_text(text: Any | None) -> str:
    if text is None:
        return ""
    return " ".join(SELECT_SHADOW_MATCH_APOSTROPHE_RE.sub("", str(text).lower()).split())


def _stem_select_shadow_text(normalized_text: str) -> str:
    stems = []
    for word in normalized_text.split():
        if word.endswith("s") and not word.endswith("ss"):
            stems.append(word[:-1])
        else:
            stems.append(word)
    return " ".join(stems)


def _unique_select_shadow_index(indices: list[int]) -> int | None:
    return indices[0] if len(indices) == 1 else None


def _best_select_shadow_index(scored_indices: list[tuple[int, float]]) -> int | None:
    if not scored_indices:
        return None
    best_score = max(score for _, score in scored_indices)
    best_indices = [index for index, score in scored_indices if score == best_score]
    return _unique_select_shadow_index(best_indices)


def classify_option_match(target_value: str | None, option_labels: list[str]) -> tuple[int | None, str]:
    target_norm = _normalize_select_shadow_text(target_value)
    option_norms = [_normalize_select_shadow_text(label) for label in option_labels]
    if not target_norm or not any(option_norms):
        return None, "miss"

    exact_indices = [index for index, option_norm in enumerate(option_norms) if option_norm == target_norm]
    if exact_indices:
        return _unique_select_shadow_index(exact_indices), "exact"

    target_stem = _stem_select_shadow_text(target_norm)
    stem_indices = [
        index
        for index, option_norm in enumerate(option_norms)
        if option_norm and _stem_select_shadow_text(option_norm) == target_stem
    ]
    if stem_indices:
        return _unique_select_shadow_index(stem_indices), "stem"

    substring_scores = [
        (index, float(min(len(target_norm), len(option_norm))))
        for index, option_norm in enumerate(option_norms)
        if len(target_norm) >= 3
        and len(option_norm) >= 3
        and (target_norm in option_norm or option_norm in target_norm)
    ]
    if substring_scores:
        return _best_select_shadow_index(substring_scores), "fuzzy"

    target_words = set(SELECT_SHADOW_MATCH_WORD_RE.findall(target_norm))
    overlap_scores: list[tuple[int, float]] = []
    if target_words:
        for index, option_norm in enumerate(option_norms):
            option_words = set(SELECT_SHADOW_MATCH_WORD_RE.findall(option_norm))
            if option_words and target_words & option_words:
                overlap_scores.append(
                    (index, len(target_words & option_words) / max(len(target_words), len(option_words)))
                )
    if overlap_scores:
        return _best_select_shadow_index(overlap_scores), "fuzzy"

    return None, "miss"


def _select_shadow_candidate(
    label: str | None,
    *,
    element_id: str | None = None,
    value: str | None = None,
    keep_empty: bool = False,
) -> dict[str, str | None] | None:
    label_norm = " ".join((label or "").split())
    value_norm = " ".join((value or "").split())
    if not keep_empty and not label_norm and not value_norm:
        return None
    return {
        "label": label_norm or value_norm,
        "element_id": element_id,
        "value": value_norm or None,
    }


def _select_shadow_candidates_from_select_options(options: list[Any]) -> list[dict[str, str | None]]:
    candidates: list[dict[str, str | None]] = []
    for option in options:
        if isinstance(option, dict):
            candidate = _select_shadow_candidate(
                str(option.get("text") or ""),
                value=str(option.get("value") or ""),
                keep_empty=True,
            )
        else:
            candidate = _select_shadow_candidate(str(option), keep_empty=True)
        if candidate is not None:
            candidates.append(candidate)
    return candidates


def _select_shadow_label_from_node(node: dict) -> str | None:
    attrs = node.get("attributes") or {}
    for raw_label in (
        node.get("text"),
        attrs.get("aria-label"),
        attrs.get("title"),
    ):
        label = " ".join(str(raw_label or "").split())
        if label:
            return label
    return None


def _select_shadow_candidates_from_elements(elements: list[dict]) -> list[dict[str, str | None]]:
    queue: deque[dict] = deque(elements)
    candidates: list[dict[str, str | None]] = []
    while queue:
        node = queue.popleft()
        if not isinstance(node, dict):
            continue

        attrs = node.get("attributes") or {}
        role = str(attrs.get("role") or "").lower()
        tag = str(node.get("tagName") or "").lower()
        element_id = str(node.get("id") or "") or None
        label = _select_shadow_label_from_node(node)
        if label and (role == "option" or tag in ("li", "option") or bool(node.get("interactable"))):
            candidate = _select_shadow_candidate(label, element_id=element_id)
            if candidate is not None:
                candidates.append(candidate)

        for option in node.get("options") or []:
            if not isinstance(option, dict):
                continue
            candidate = _select_shadow_candidate(
                str(option.get("text") or ""),
                element_id=element_id,
                value=str(option.get("value") or ""),
            )
            if candidate is not None:
                candidates.append(candidate)

        for child in node.get("children") or []:
            queue.append(child)
    return candidates


SELECT_SHADOW_MATCH_FIELD_MAX_CHARS = 120


def _truncate_select_shadow_field(text: str | None) -> str | None:
    if text is None:
        return None
    if len(text) <= SELECT_SHADOW_MATCH_FIELD_MAX_CHARS:
        return text
    return text[:SELECT_SHADOW_MATCH_FIELD_MAX_CHARS] + "…"


def _normalized_select_shadow_field(text: str | None) -> str | None:
    if text is None:
        return None
    return _truncate_select_shadow_field(_normalize_select_shadow_text(text))


class SelectShadowAgreement(BaseModel):
    agrees: bool | None
    llm_index: int | None = None
    llm_value: str | None = None
    llm_element_id: str | None = None

    # Fields come straight from LLM JSON; malformed values must never drop the shadow event.
    @field_validator("llm_value", "llm_element_id", mode="before")
    @classmethod
    def _coerce_llm_text(cls, value: Any) -> str | None:
        return None if value is None else str(value)

    @field_validator("llm_index", mode="before")
    @classmethod
    def _coerce_llm_index(cls, value: Any) -> int | None:
        if value is None or isinstance(value, int):
            return value
        try:
            return int(str(value).strip())
        except ValueError:
            return None


def _autocomplete_candidates_from_elements(elements: list[dict]) -> list[dict[str, str | None]]:
    candidates: list[dict[str, str | None]] = []
    seen: set[tuple[str | None, str]] = set()
    for candidate in _select_shadow_candidates_from_elements(elements):
        element_id = candidate.get("element_id")
        label = candidate.get("label") or candidate.get("value")
        if not element_id or not label:
            continue
        dedupe_key = (element_id, _normalize_select_shadow_text(label))
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        candidates.append({"element_id": element_id, "label": label, "value": candidate.get("value")})
    return candidates


def _resolve_autocomplete_candidate(
    target_value: str,
    elements: list[dict],
) -> tuple[int, dict[str, str | None]] | None:
    candidates = _autocomplete_candidates_from_elements(elements)
    matched_index = match_option_exact_or_stem(target_value, [candidate.get("label") or "" for candidate in candidates])
    if matched_index is None:
        return None
    return matched_index, candidates[matched_index]


async def _read_autocomplete_option_identity(
    *,
    skyvern_frame: SkyvernFrame,
    locator: Locator,
) -> dict[str, Any] | None:
    try:
        element_handle = await locator.element_handle(timeout=settings.BROWSER_ACTION_TIMEOUT_MS)
        if element_handle is None:
            return None
        return await skyvern_frame.read_autocomplete_option_identity(element_handle)
    except Exception:
        LOG.info("Failed to read autocomplete option identity", exc_info=True)
        return None


async def _verify_autocomplete_option_identity(
    *,
    skyvern_frame: SkyvernFrame,
    locator: Locator,
    matched_index: int,
    matched_label: str,
) -> bool:
    identity = await _read_autocomplete_option_identity(skyvern_frame=skyvern_frame, locator=locator)
    if identity is None:
        return False

    actual_index = identity.get("index")
    actual_label = identity.get("label")
    label_matches = _normalize_select_shadow_text(actual_label) == _normalize_select_shadow_text(matched_label)
    # Advisory only: typeahead DOMs can detach or rerender options, so label
    # identity is required and index is retained only for diagnostics.
    if label_matches:
        if actual_index not in (matched_index, None, -1):
            LOG.info(
                "Autocomplete option index differed from deterministic candidate; accepting label match",
                expected_index=matched_index,
                expected_label=matched_label,
                actual_index=actual_index,
                actual_label=actual_label,
            )
        return True

    LOG.info(
        "Autocomplete option identity did not match deterministic candidate",
        expected_index=matched_index,
        expected_label=matched_label,
        actual_index=actual_index,
        actual_label=actual_label,
    )
    return False


async def _verify_autocomplete_input_readback(
    *,
    skyvern_element: SkyvernElement,
    matched_index: int,
    matched_label: str,
    engine_selection: BrowserEngineSelection | None = None,
) -> bool:
    actual_value = await get_input_value(
        skyvern_element.get_tag_name(), skyvern_element.get_locator(), engine_selection=engine_selection
    )
    if _normalize_select_shadow_text(actual_value) == _normalize_select_shadow_text(matched_label):
        return True

    LOG.info(
        "Autocomplete read-back did not match deterministic option",
        expected_index=matched_index,
        expected_label=matched_label,
        actual_value=actual_value,
    )
    return False


def _is_boundary_fragment(fragment: str, whole: str) -> bool:
    """Whether ``fragment`` occurs in ``whole`` as a word-boundary-delimited contiguous run.

    Both are expected to already be normalized. A fragment carrying no alphanumeric character can
    never anchor a meaningful boundary match, so it is rejected. Default Unicode ``\\w`` semantics
    apply, so accented Latin behaves and unsegmented CJK interiors fail closed.
    """
    if not any(ch.isalnum() for ch in fragment):
        return False
    return re.search(rf"(?<!\w){re.escape(fragment)}(?!\w)", whole) is not None


def _autocomplete_commit_evidence(
    pre_value: str | None,
    post_value: str | None,
    option_label: str | None,
) -> tuple[str, str] | None:
    """Observational commit evidence for an autocomplete selection, or None.

    Emits ``(committed_option, committed_value)`` — both truncated to the shared field cap — only
    when the clicked option's label and both control read-backs are nonempty *after normalization*,
    the normalized post-click value differs from the normalized pre-click value, and BOTH the
    normalized pre and post are boundary-delimited fragments of the normalized option label. That
    last relation is what makes the transition selection-specific: it rejects unrelated blur,
    masking, formatting, validation, and restoration transforms whose output is not a fragment of
    the clicked option. The relation runs on the full normalized strings before any truncation.
    Whitespace-only fields normalize to empty and fail closed; equality (a no-op or highlight-only
    click) yields None. Secret suppression is the caller's responsibility.
    """
    normalized_pre = _normalize_select_shadow_text(pre_value)
    normalized_post = _normalize_select_shadow_text(post_value)
    normalized_label = _normalize_select_shadow_text(option_label)
    if not normalized_label or not normalized_pre or not normalized_post:
        return None
    if normalized_post == normalized_pre:
        return None
    if not _is_boundary_fragment(normalized_pre, normalized_label):
        return None
    if not _is_boundary_fragment(normalized_post, normalized_label):
        return None
    committed_option = _truncate_select_shadow_field(option_label)
    committed_value = _truncate_select_shadow_field(post_value)
    if not committed_option or not committed_value:
        return None
    return committed_option, committed_value


async def _read_autocomplete_control_value(
    skyvern_element: SkyvernElement,
    engine_selection: BrowserEngineSelection | None = None,
) -> str | None:
    try:
        return await get_input_value(
            skyvern_element.get_tag_name(),
            skyvern_element.get_locator(),
            engine_selection=engine_selection,
            read_timeout_ms=settings.BROWSER_ACTION_TIMEOUT_MS,
        )
    except Exception:
        LOG.info("Failed to read autocomplete control value for commit evidence", exc_info=True)
        return None


async def _read_clicked_option_label(
    *,
    skyvern_frame: SkyvernFrame,
    option_locator: Locator,
    option_static_element: dict | None,
) -> str | None:
    identity = await _read_autocomplete_option_identity(skyvern_frame=skyvern_frame, locator=option_locator)
    label = identity.get("label") if identity else None
    if not label and option_static_element:
        # Fall back to the scraped node text; it is tied to this exact option by construction.
        label = option_static_element.get("text")
    label = (label or "").strip()
    return label or None


async def _click_autocomplete_option_with_commit_evidence(
    *,
    skyvern_element: SkyvernElement,
    option_locator: Locator,
    option_static_element: dict | None,
    skyvern_frame: SkyvernFrame,
    click: Callable[[], Awaitable[None]],
    is_secret_value: bool,
    engine_selection: BrowserEngineSelection | None = None,
) -> ActionResult:
    """Click the LLM-selected option and return ``ActionSuccess``, enriched with commit evidence
    only when the target control's read-back changes into a value that — like the pre-click value —
    is a boundary-delimited fragment of the clicked option's label (see
    ``_autocomplete_commit_evidence``) AND that transitioned value survives a next-render settle
    reread. The render-driven settle guards against an optimistic control that paints the selected
    label and then reverts it after async validation/rerender, which would otherwise commit a
    transient value.

    Evidence capture is best-effort and fail-closed: a failed read (before, after, or on the settle
    reread), a missing label, an unchanged control, an unrelated transform, a value that drifts on
    the settled reread, or a secret value all leave a bare ``ActionSuccess``. Only the click itself
    can fail the action — every evidence read is exception-isolated so a successful click never
    regresses to ``ActionFailure`` because capture failed.
    """
    option_label: str | None = None
    pre_value: str | None = None
    if not is_secret_value:
        try:
            option_label = await _read_clicked_option_label(
                skyvern_frame=skyvern_frame,
                option_locator=option_locator,
                option_static_element=option_static_element,
            )
        except Exception:
            LOG.info("Failed to read clicked autocomplete option label for commit evidence", exc_info=True)
        pre_value = await _read_autocomplete_control_value(skyvern_element, engine_selection)

    await click()

    if is_secret_value:
        return ActionSuccess()

    try:
        post_value = await _read_autocomplete_control_value(skyvern_element, engine_selection)
        evidence = _autocomplete_commit_evidence(pre_value, post_value, option_label)
        if evidence is None:
            return ActionSuccess()
        # The candidate transition passed, but an optimistic control can paint the selected label and
        # then revert it after async validation/rerender. Reconcile on the next render turn via the
        # existing render-settle helper (double-rAF, with a 250ms liveness cap) and reread once; only
        # record evidence when the next-render reread still holds the first post value, so a transient
        # paint cannot be committed as stale evidence.
        await _wait_custom_select_render_settle(skyvern_element)
        confirm_value = await _read_autocomplete_control_value(skyvern_element, engine_selection)
        if confirm_value is None or _normalize_select_shadow_text(confirm_value) != _normalize_select_shadow_text(
            post_value
        ):
            return ActionSuccess()
    except Exception:
        LOG.info("Autocomplete commit-evidence capture failed after a successful click", exc_info=True)
        return ActionSuccess()

    committed_option, committed_value = evidence
    return ActionSuccess(committed_option=committed_option, committed_value=committed_value)


async def _reset_autocomplete_for_llm_fallback(
    *,
    current_incremental_scraped: IncrementalScrapePage,
    skyvern_frame: SkyvernFrame,
    skyvern_element: SkyvernElement,
    page: Page,
    scraped_page: ScrapedPage,
    dom: DomUtil,
    text: str,
    task: Task,
    step: Step,
    engine_selection: BrowserEngineSelection | None = UNSET_SELECTION,
) -> tuple[IncrementalScrapePage, list[dict], list[dict], str, list[str]]:
    if engine_selection is UNSET_SELECTION:
        engine_selection = resolve_engine_selection_for_task(task, app.BROWSER_MANAGER)
    await current_incremental_scraped.stop_listen_dom_increment()
    await skyvern_element.input_clear()

    incremental_scraped = IncrementalScrapePage(
        skyvern_frame=skyvern_frame,
        engine_selection=engine_selection,
    )
    await incremental_scraped.start_listen_dom_increment(await skyvern_element.get_element_handler())
    await skyvern_element.press_fill(text)
    await skyvern_frame.safe_wait_for_animation_end(before_wait_sec=1, caller="autocomplete.fallback_refill")
    incremental_element = await incremental_scraped.get_incremental_element_tree(
        clean_and_remove_element_tree_factory(
            task=task,
            step=step,
            check_filter_funcs=[check_existed_but_not_option_element_in_dom_factory(dom)],
            engine_selection=engine_selection,
        ),
    )

    if len(incremental_element) > 0:
        cleaned_incremental_element = remove_duplicated_HTML_element(incremental_element)
        html = incremental_scraped.build_html_tree(cleaned_incremental_element)
        return incremental_scraped, incremental_element, cleaned_incremental_element, html, []

    scraped_page_after_open = await scraped_page.generate_scraped_page_without_screenshots()
    new_element_ids = set(scraped_page_after_open.id_to_css_dict.keys()) - set(scraped_page.id_to_css_dict.keys())

    dom_after_open = DomUtil(scraped_page=scraped_page_after_open, page=page)
    new_interactable_element_ids = [
        element_id
        for element_id in new_element_ids
        if (await dom_after_open.get_skyvern_element_by_id(element_id)).is_interactable()
    ]
    if len(new_interactable_element_ids) == 0:
        raise NoIncrementalElementFoundForAutoCompletion(element_id=skyvern_element.get_id(), text=text)

    LOG.info(
        "New elements detected after resetting autocomplete fallback input",
        new_elements_ids=new_interactable_element_ids,
    )
    fallback_elements = [
        scraped_page_after_open.id_to_element_dict[element_id] for element_id in new_interactable_element_ids
    ]
    return (
        incremental_scraped,
        fallback_elements,
        fallback_elements,
        scraped_page_after_open.build_element_tree(),
        new_interactable_element_ids,
    )


def _select_shadow_agrees_with_native_choice(
    candidates: list[dict[str, str | None]],
    matched_index: int | None,
    *,
    llm_index: int | None,
    llm_value: str | None,
) -> SelectShadowAgreement:
    agreement = SelectShadowAgreement(agrees=None, llm_index=llm_index, llm_value=llm_value)
    if matched_index is None:
        agreement.agrees = False
        return agreement

    llm_value_norm = _normalize_select_shadow_text(llm_value)
    if llm_value_norm:
        if matched_index < len(candidates):
            matched_candidate = candidates[matched_index]
            agreement.agrees = llm_value_norm in {
                _normalize_select_shadow_text(matched_candidate.get("label")),
                _normalize_select_shadow_text(matched_candidate.get("value")),
            }
    elif llm_index is not None:
        agreement.agrees = matched_index == llm_index
    return agreement


def _select_shadow_agrees_with_element_choice(
    candidates: list[dict[str, str | None]],
    matched_index: int | None,
    *,
    llm_element_id: str | None,
    llm_value: str | None,
) -> SelectShadowAgreement:
    agreement = SelectShadowAgreement(agrees=None, llm_value=llm_value, llm_element_id=llm_element_id)
    if matched_index is None:
        agreement.agrees = False
        return agreement
    if matched_index >= len(candidates):
        return agreement

    matched_candidate = candidates[matched_index]
    llm_value_norm = _normalize_select_shadow_text(llm_value)
    # Element ids are unstable across incremental scrapes, so id equality never decides
    # agreement — ids stay in the logged detail fields as metadata only.
    if llm_value_norm:
        agreement.agrees = llm_value_norm in {
            _normalize_select_shadow_text(matched_candidate.get("label")),
            _normalize_select_shadow_text(matched_candidate.get("value")),
        }
    return agreement


def _log_select_shadow_match(
    *,
    prompt_name: str,
    target_value: str | None,
    get_candidates: Callable[[], list[dict[str, str | None]]],
    agreement: Callable[[list[dict[str, str | None]], int | None], SelectShadowAgreement],
) -> None:
    if not _select_shadow_match_enabled():
        return

    try:
        candidates = get_candidates()
        option_labels = [candidate["label"] or "" for candidate in candidates]
        matched_index, tier = classify_option_match(target_value, option_labels)
        result = agreement(candidates, matched_index)
        disagreement_fields: dict[str, Any] = {}
        if matched_index is not None and result.agrees is not True:
            matched_candidate = candidates[matched_index] if matched_index < len(candidates) else {}
            disagreement_fields = {
                "target_value": _truncate_select_shadow_field(target_value),
                "matched_index": matched_index,
                "matched_label": _truncate_select_shadow_field(matched_candidate.get("label")),
                "matched_value": _truncate_select_shadow_field(matched_candidate.get("value")),
                "matched_element_id": matched_candidate.get("element_id"),
                "llm_index": result.llm_index,
                "llm_value": _truncate_select_shadow_field(result.llm_value),
                "llm_element_id": result.llm_element_id,
                "normalized_target_value": _normalized_select_shadow_field(target_value),
                "normalized_matched_label": _normalized_select_shadow_field(matched_candidate.get("label")),
                "normalized_matched_value": _normalized_select_shadow_field(matched_candidate.get("value")),
                "normalized_llm_value": _normalized_select_shadow_field(result.llm_value),
            }
            disagreement_fields = {key: value for key, value in disagreement_fields.items() if value is not None}
        LOG.info(
            "select_shadow_match",
            prompt_name=prompt_name,
            option_count=len(option_labels),
            match_tier=tier,
            match_found=matched_index is not None,
            match_agrees_with_llm=result.agrees,
            **disagreement_fields,
        )
    except Exception:
        LOG.debug("select_shadow_match failed", exc_info=True)


def _download_target_path(download_dir: Path, suggested_filename: str | None) -> Path:
    filename = Path(suggested_filename or "download").name
    stem, suffix = os.path.splitext(filename)
    context = skyvern_context.current()
    download_suffix = context.download_suffix if context else None
    if download_suffix:
        # Name the file by the block-configured download_suffix so the watcher syncs the
        # request-based name instead of the site's suggested name.
        existing = {p.name for p in download_dir.iterdir()} if download_dir.exists() else set()
        target_name = download_filename_from_suffix(download_suffix, suffix, existing)
        LOG.info(
            "download_suffix_target_named",
            context_task_id=context.task_id if context else None,
            context_download_suffix_fp=diagnostic_fingerprint(download_suffix),
            suggested_filename_fp=diagnostic_fingerprint(suggested_filename),
            desired_name_fp=diagnostic_fingerprint(target_name),
        )
        return download_dir / target_name
    return download_dir / f"{uuid.uuid4()}-{stem or 'download'}{suffix}"


def _blob_download_candidate_pages(download: Download, page: Page) -> list[Page]:
    """Pages to try when reading a blob: download's bytes, owner-first and deduped.

    A blob: URL only resolves inside the document that minted it, and a download-triggering
    click frequently opens that document in a new tab, so the owning page may not be the one
    the action ran on. Fan out over every open page the way the CDP download monitor does.
    """
    candidates: list[Page] = []
    seen: set[int] = set()

    def _add(candidate: Page | None) -> None:
        if candidate is None or id(candidate) in seen:
            return
        seen.add(id(candidate))
        candidates.append(candidate)

    _add(download.page)
    _add(page)
    try:
        context_pages = list(page.context.pages)
    except Exception:
        context_pages = []
    for context_page in context_pages:
        _add(context_page)
    return candidates


async def _read_adopted_session_blob_bytes(
    download: Download,
    page: Page,
    workflow_run_id: str | None = None,
) -> bytes | None:
    """Read a blob: download's bytes by fanning out over its candidate pages, owner first.

    Returns the bytes from the first page that owns the blob (``b""`` is a valid zero-byte
    read), or ``None`` when no open page can resolve it.
    """
    for candidate in _blob_download_candidate_pages(download, page):
        blob_bytes = await SkyvernFrame.read_blob_url_bytes(
            page=candidate,
            blob_url=download.url,
            workflow_run_id=workflow_run_id,
            max_size_bytes=MAX_FILE_SIZE_BYTES,
            probe=True,
        )
        if blob_bytes is not None:
            return blob_bytes
    return None


class _EagerAdoptedBlobCapture:
    """Read an adopted-session blob download's bytes the instant the download event fires.

    A blob: URL only resolves inside the document that minted it, and that document is
    frequently torn down (navigation, ``revokeObjectURL``, tab close) before the ~1s download
    poll runs ``_save_adopted_session_download`` — so the post-hoc fan-out reads a context in
    which the owner is already gone. Reading here, at the download event, captures the bytes
    while the owner is still live. Armed only for adopted/persistent sessions and blob: URLs.
    """

    def __init__(self, *, enabled: bool, clicked_page: Page, workflow_run_id: str | None) -> None:
        self._enabled = enabled
        self._clicked_page = clicked_page
        self._workflow_run_id = workflow_run_id
        self._task: asyncio.Task[None] | None = None
        self._bytes: bytes | None = None

    def maybe_start(self, download: Download) -> None:
        if not self._enabled or self._task is not None:
            return
        if not (download.url or "").startswith("blob:"):
            return
        self._task = asyncio.create_task(self._run(download))

    async def _run(self, download: Download) -> None:
        try:
            self._bytes = await _read_adopted_session_blob_bytes(
                download, self._clicked_page, workflow_run_id=self._workflow_run_id
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            LOG.debug(
                "Eager adopted-session blob capture failed",
                workflow_run_id=self._workflow_run_id,
                exc_info=True,
            )

    async def result(self, timeout: float) -> bytes | None:
        if self._task is None:
            return None
        try:
            # Shield so the timeout unblocks us without tearing the read down mid-flight; on timeout
            # we then cancel+drain below so save_as/fan-out never run while the read still holds bytes.
            await asyncio.wait_for(asyncio.shield(self._task), timeout=timeout)
        except asyncio.TimeoutError:
            LOG.warning(
                "Eager adopted-session blob capture did not finish before it was needed",
                workflow_run_id=self._workflow_run_id,
            )
            await self.aclose()
        except Exception:
            LOG.debug(
                "Eager adopted-session blob capture result raised",
                workflow_run_id=self._workflow_run_id,
                exc_info=True,
            )
        return self._bytes

    async def aclose(self) -> None:
        task = self._task
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            # Swallow the cancellation we requested, but if this coroutine is itself being
            # cancelled, re-raise so the enclosing timeout/cancel scope still observes it.
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                raise
        except Exception:
            LOG.debug(
                "Eager adopted-session blob capture cleanup raised",
                workflow_run_id=self._workflow_run_id,
                exc_info=True,
            )


async def _close_eager_capture_then_teardown_retention(
    eager_blob_capture: _EagerAdoptedBlobCapture,
    page: Page,
    *,
    retention_armed: bool,
    workflow_run_id: str | None,
) -> None:
    # aclose() can re-raise CancelledError when the enclosing action is cancelled; the retention
    # wrapper patches page-realm URL.createObjectURL/revokeObjectURL and must be torn down anyway, or
    # a cancelled session leaks the patched globals. Teardown runs whenever arming was attempted
    # (a partial install still patches the globals). The original cancellation still propagates after
    # the finally, and a teardown failure stays fail-open/debug-only.
    try:
        await eager_blob_capture.aclose()
    finally:
        if retention_armed:
            try:
                async with asyncio.timeout(_BLOB_RETENTION_ARMING_TIMEOUT_SECONDS):
                    await teardown_blob_url_retention(page, workflow_run_id=workflow_run_id)
            except Exception:
                LOG.debug("Failed to tear down blob URL retention", workflow_run_id=workflow_run_id)


@contextlib.asynccontextmanager
async def _adopted_session_download_binding(
    download: Download,
    active_page: Page,
    *,
    download_binding: DownloadBinding = DownloadBinding.RUN_DIR,
) -> AsyncIterator[tuple[Any, "RedirectHopAuthorizer[GuardedFileFetchHopResult]", str | None]]:
    """Lease the exact context binding that owns an adopted-session download."""
    download_page = download.page
    if download_page is None:
        raise RuntimeError("Adopted-session download has no owning page")
    download_context = download_page.context
    if download_context is not active_page.context:
        raise RuntimeError("Adopted-session download page context does not match the active page context")

    if download_binding == DownloadBinding.SESSION_DIR:
        # A provider-owned remote binding never has an interceptor to lease: the creator that stamps
        # SESSION_DIR binds none, and every download-dir rebind — the only other installer of the
        # ownership lock — is skipped for this binding. Its branches forbid URL replay anyway, so
        # deny the hop rather than yielding an authority nothing here can honour.
        yield None, deny_unenrolled_redirect_hop, None
        return

    try:
        bind_lock = download_context._skyvern_cdp_download_interceptor_bind_lock  # type: ignore[attr-defined]
    except AttributeError as exc:
        raise RuntimeError("Adopted-session download context has no interceptor ownership lock") from exc
    if not isinstance(bind_lock, asyncio.Lock):
        raise RuntimeError("Adopted-session download context has an invalid interceptor ownership lock")

    # Narrow exception to the usual explicit-injection rule: bind_to_context already stores the
    # constructor-injected interceptor on its owned context. Holding its established bind lock
    # keeps that exact Page -> BrowserContext -> interceptor association live through the fallback.
    async with bind_lock:
        try:
            download_interceptor = download_context._skyvern_cdp_download_interceptor  # type: ignore[attr-defined]
        except AttributeError as exc:
            raise RuntimeError(
                "Adopted-session download recovery requires the page context's CDP download interceptor"
            ) from exc
        try:
            interceptor_context = download_interceptor._page_context
        except AttributeError as exc:
            raise RuntimeError("Bound CDP download interceptor has no page-context ownership binding") from exc
        if interceptor_context is not download_context:
            raise RuntimeError("Bound CDP download interceptor does not own the adopted-session download page context")
        try:
            authorize_request_hop = download_interceptor._redirect_hop_authorizer
        except AttributeError as exc:
            raise RuntimeError("Bound CDP download interceptor has no redirect hop authorizer") from exc
        if not callable(authorize_request_hop):
            raise RuntimeError("Bound CDP download interceptor has an invalid redirect hop authorizer")
        try:
            download_scope = download_interceptor.download_scope
        except AttributeError as exc:
            raise RuntimeError("Bound CDP download interceptor has no download scope contract") from exc
        if download_scope is not None and (not isinstance(download_scope, str) or not download_scope.strip()):
            raise RuntimeError("Bound CDP download interceptor has an invalid download scope")
        yield (
            download_interceptor,
            cast(
                "RedirectHopAuthorizer[GuardedFileFetchHopResult]",
                authorize_request_hop,
            ),
            download_scope,
        )


async def _save_adopted_session_download(
    download: Download,
    page: Page,
    download_dir: Path,
    *,
    authorize_request_hop: "RedirectHopAuthorizer[GuardedFileFetchHopResult]",
    request_headers: dict[str, str],
    download_scope: str | None = None,
    workflow_run_id: str | None = None,
    eager_blob_bytes: bytes | None = None,
    download_binding: DownloadBinding = DownloadBinding.RUN_DIR,
) -> Path | None:
    """Land an adopted-session download's bytes into download_dir, returning the file path or None.

    A provider-owned remote non-blob event is signal-only and returns None; blob and default behavior
    are unchanged.
    """
    download_target = _download_target_path(download_dir, download.suggested_filename)
    if download_binding == DownloadBinding.SESSION_DIR and not download.url.startswith("blob:"):
        # Signal-only for a provider-owned remote binding: the run connection holds no bytes to save_as
        # and a URL replay would run through the wrong identity, so defer to the provider destination.
        LOG.info(
            "Provider-owned remote download: suppressing local save/replay, deferring to provider destination",
            workflow_run_id=workflow_run_id,
        )
        return None
    # Non-empty bytes captured at download-event time (blob owner still alive) win outright: skip
    # save_as, which returns empty for blobs anyway. A zero-byte eager capture is indistinguishable
    # from an unreadable one and would be a false success, so fall through to save_as + fan-out
    # (matching _persist_captured_download's empty-file handling and the CDP interceptor).
    if download.url.startswith("blob:") and eager_blob_bytes:
        download_target.write_bytes(eager_blob_bytes)
        return download_target
    if download.url.startswith("blob:") and eager_blob_bytes == b"":
        LOG.warning(
            "Eager adopted-session blob capture returned zero bytes; falling through to save_as/fan-out",
            download_dir=str(download_dir),
            workflow_run_id=workflow_run_id,
        )
    persisted = await _persist_captured_download(
        download, target=download_target, timeout=BROWSER_DOWNLOAD_MAX_WAIT_TIME
    )
    if persisted.path is not None:
        return persisted.path
    if persisted.outcome == "empty":
        LOG.warning(
            "Adopted-session eager save_as produced an empty file; re-fetching download url",
            download_dir=str(download_dir),
            workflow_run_id=workflow_run_id,
        )
    else:
        LOG.warning(
            "Adopted-session eager save_as failed; re-fetching download url",
            download_dir=str(download_dir),
            workflow_run_id=workflow_run_id,
            exc_info=True,
        )

    # Ordering: ``save_as`` above has already run and failed (empty or raised).
    # ``blob:`` URLs cannot be fetched by the guarded HTTP client, so route them
    # through an in-page fetch from the document that owns the blob. That document may be a different tab, so
    # probe every open page (owner first) rather than reading from ``page`` alone.
    if download.url.startswith("blob:"):
        blob_bytes = await _read_adopted_session_blob_bytes(download, page, workflow_run_id=workflow_run_id)
        if blob_bytes is None:
            # The download event's own blob is unreadable (owner torn down / remote-CDP), but the
            # statement is often still displayed in a live same-origin blob: PDF iframe whose bytes
            # are recoverable. Bounded by the same budget as blocked-inline-PDF recovery.
            recovered_bytes: bytes | None = None
            try:
                async with asyncio.timeout(_BLOCKED_INLINE_PDF_RECOVERY_TIMEOUT_SECONDS):
                    recovered_bytes = await _recover_adopted_session_blob_pdf_iframe(page, download, workflow_run_id)
            except asyncio.TimeoutError:
                LOG.warning(
                    "Adopted-session blob PDF iframe recovery exceeded its budget; treating as no recovery",
                    workflow_run_id=workflow_run_id,
                    recovery_budget_seconds=_BLOCKED_INLINE_PDF_RECOVERY_TIMEOUT_SECONDS,
                )
            if recovered_bytes is not None:
                download_target.write_bytes(recovered_bytes)
                LOG.info(
                    "Recovered adopted-session statement from a live blob: PDF iframe",
                    download_dir=str(download_dir),
                    workflow_run_id=workflow_run_id,
                    recovered_bytes=len(recovered_bytes),
                    download_target=str(download_target),
                )
                return download_target
            LOG.warning(
                "Adopted-session blob download could not be read from any open page",
                download_dir=str(download_dir),
                workflow_run_id=workflow_run_id,
                candidate_page_count=len(_blob_download_candidate_pages(download, page)),
            )
            return None
        download_target.write_bytes(blob_bytes)
        return download_target

    try:
        response = await fetch_file_bytes(
            download.url,
            headers=request_headers,
            authorize_request_hop=authorize_request_hop,
            download_scope=download_scope,
            approved_initial_url=download.url,
        )
        body = response.body
        if not body:
            LOG.error(
                "Adopted-session download url re-fetch returned an empty body",
                workflow_run_id=workflow_run_id,
            )
            return None
        download_target.write_bytes(body)
        return download_target
    except (SkyvernHTTPException, HttpException) as exc:
        LOG.error(
            "Adopted-session download destination refused",
            download_dir=str(download_dir),
            workflow_run_id=workflow_run_id,
            error_type=type(exc).__name__,
            error_origin=redacted_exception_origin(exc),
        )
        return None
    except Exception as exc:
        LOG.error(
            "Adopted-session download url re-fetch failed",
            download_dir=str(download_dir),
            workflow_run_id=workflow_run_id,
            error_type=type(exc).__name__,
            error_origin=redacted_exception_origin(exc),
        )
        return None


# Set for the duration of a file-download block's non-download click that is authorized as a
# false-click candidate. Read by handle_click_action to enable the same-action download bypass of
# the expensive dropdown/custom-select rescrape; never gates persistence or task finalization.
_false_click_download_eligible: ContextVar[bool] = ContextVar("false_click_download_eligible", default=False)


def _remove_download_listener(page: Page, callback: Callable[[Download], None]) -> None:
    off = getattr(page, "off", None)
    if callable(off):
        off("download", callback)
        return

    remove_listener = getattr(page, "remove_listener", None)
    if callable(remove_listener):
        remove_listener("download", callback)
        return

    LOG.warning("Page does not support removing download listeners")


def _register_false_click_download_probe(page: Page, observed: asyncio.Event) -> Callable[[], None]:
    """Flag ``observed`` when a download is minted on the clicked page or a popup it spawns
    during the click window. Returns a cleanup that removes every listener it installed."""
    download_handles: list[tuple[Page, Callable[[Download], None]]] = []

    def _flag_download(_download: Download) -> None:
        observed.set()

    def _on_popup(popup_page: Page) -> None:
        popup_page.on("download", _flag_download)
        download_handles.append((popup_page, _flag_download))

    page.on("download", _flag_download)
    download_handles.append((page, _flag_download))
    page.on("popup", _on_popup)

    def _cleanup() -> None:
        try:
            _remove_popup_listener(page, _on_popup)
        except Exception:
            LOG.warning("Failed to remove false-click download popup listener", exc_info=True)
        for observed_page, callback in download_handles:
            try:
                _remove_download_listener(observed_page, callback)
            except Exception:
                LOG.warning("Failed to remove false-click download listener", exc_info=True)

    return _cleanup


def _remove_popup_listener(page: Page, callback: Callable[[Page], None]) -> None:
    off = getattr(page, "off", None)
    if callable(off):
        off("popup", callback)
        return
    page.remove_listener("popup", callback)


class _CapturedDownloadPersistence(NamedTuple):
    path: Path | None
    outcome: str


async def _persist_captured_download(
    download: Download, *, target: Path | None, timeout: float, owned_dir: Path | None = None
) -> _CapturedDownloadPersistence:
    try:
        async with asyncio.timeout(timeout):
            try:
                failure = await download.failure()
            except TypeError:
                failure = None
            if failure is not None:
                return _CapturedDownloadPersistence(None, "download_failed")
            if target is None:
                try:
                    local_path_value = await resolve_artifact_path(download, timeout)
                    if local_path_value and (local_path := Path(local_path_value)).is_file():
                        if local_path.stat().st_size:
                            return _CapturedDownloadPersistence(local_path, "local_path")
                        if owned_dir is not None and local_path.parent.resolve() == owned_dir.resolve():
                            local_path.unlink(missing_ok=True)
                        return _CapturedDownloadPersistence(None, "empty")
                except Exception:
                    return _CapturedDownloadPersistence(None, "path_unavailable")
                return _CapturedDownloadPersistence(None, "path_unavailable")
            await download.save_as(target)
            if target.is_file() and target.stat().st_size:
                return _CapturedDownloadPersistence(target, "saved")
            target.unlink(missing_ok=True)
            return _CapturedDownloadPersistence(None, "empty")
    except (asyncio.TimeoutError, asyncio.CancelledError) as error:
        if target is not None:
            target.unlink(missing_ok=True)
        if isinstance(error, asyncio.CancelledError):
            raise
        return _CapturedDownloadPersistence(None, "timeout")
    except Exception:
        if target is not None:
            target.unlink(missing_ok=True)
        return _CapturedDownloadPersistence(None, "save_failed")


async def _finalize_download_artifacts(
    *,
    download_dir: Path,
    task: Task,
    list_files_before: list[str],
    list_observed_download_files: Callable[[], Awaitable[list[str]]],
) -> tuple[list[str], set[str]]:
    await check_downloading_files_and_wait_for_download_to_complete(
        download_dir=download_dir,
        organization_id=task.organization_id,
        browser_session_id=task.browser_session_id,
        timeout=task.download_timeout or BROWSER_DOWNLOAD_TIMEOUT,
    )
    list_files_after = await list_observed_download_files()
    new_file_paths = set(list_files_after) - set(list_files_before)
    paths = _deduplicate_new_downloaded_file_paths(
        new_file_paths,
        workflow_run_id=task.workflow_run_id,
        observed_file_paths=set(list_files_after),
    )
    return [os.path.basename(path) for path in paths], new_file_paths


_INLINE_IFRAME_SRC_JS = "() => Array.from(document.querySelectorAll('iframe')).map((f) => f.src || '')"

# Whole-operation cap for blocked-inline-PDF recovery (all candidate same-origin fetches share it,
# so up to _collect's cap of candidates can't multiply the budget). A ~3 MB same-origin PDF fetches
# in well under a second; 30s is generous headroom for a slow-but-alive server while a hung one can
# no longer out-wait the download loop this recovery backstops.
_BLOCKED_INLINE_PDF_RECOVERY_TIMEOUT_SECONDS = 30.0


def _looks_like_pdf(data: bytes) -> bool:
    # A real PDF starts with %PDF-, optionally after a single exact UTF-8 BOM. Match the whole
    # 3-byte BOM sequence (not individual BOM bytes) and anchor at the start so HTML/JSON error
    # pages that merely mention the marker further down are rejected.
    header = data[3:] if data[:3] == b"\xef\xbb\xbf" else data
    return header[:5] == b"%PDF-"


_BLOB_IFRAME_SRC_TITLE_JS = (
    "() => Array.from(document.querySelectorAll('iframe')).map((f) => [f.src || '', f.title || ''])"
)


def _strip_url_fragment(url: str) -> str:
    return url.split("#", 1)[0]


async def _blob_iframe_src_titles(page: Page) -> dict[str, str]:
    """Map each blob: <iframe> src (fragment-stripped) to its title attribute, across every frame."""
    mapping: dict[str, str] = {}
    main_frame = page.main_frame
    for frame in _all_page_frames(page):
        target: Page | Frame = page if frame is main_frame else frame
        try:
            pairs = await SkyvernFrame.evaluate(frame=target, expression=_BLOB_IFRAME_SRC_TITLE_JS)
        except Exception:
            continue
        if not isinstance(pairs, list):
            continue
        for pair in pairs:
            if not isinstance(pair, (list, tuple)) or len(pair) != 2:
                continue
            src, title = pair
            if isinstance(src, str) and src.startswith("blob:"):
                mapping.setdefault(_strip_url_fragment(src), title if isinstance(title, str) else "")
    return mapping


async def _read_blob_pdf_bytes(page: Page, blob_url: str, workflow_run_id: str | None) -> bytes | None:
    """Read a blob: URL and return its bytes only when they are a non-empty PDF, else None."""
    data = await SkyvernFrame.read_blob_url_bytes(
        page=page,
        blob_url=blob_url,
        workflow_run_id=workflow_run_id,
        max_size_bytes=MAX_FILE_SIZE_BYTES,
        probe=True,
    )
    if data and _looks_like_pdf(data):
        return data
    return None


async def _recover_adopted_session_blob_pdf_iframe(
    page: Page, download: Download, workflow_run_id: str | None
) -> bytes | None:
    """Best-effort recovery of a statement PDF from a live same-origin blob: <iframe> when the download's own bytes are lost.

    An adopted/persistent session can fire a client-side blob download whose bytes are unrecoverable
    (empty ``save_as`` and an unfetchable download URL) while the statement is still displayed in a
    same-origin ``blob:`` PDF iframe — a different, still-live object readable from the document that
    minted it. Candidates come from the iframe ``src`` attribute, not ``frame.url``: Chromium's built-in
    PDF viewer reports the framed document's URL as ``about:blank`` while the element keeps its blob src.

    Matching the download's suggested filename against exactly one iframe title basename is a
    conservative correlation, not proof that the iframe holds the requested document — a stale iframe
    left from an earlier same-named document could still match. It is chosen because it is far safer
    than trusting whatever single PDF happens to be on screen. No suggested filename, no match, or
    several equally-titled matches all fail closed (return None) — an ambiguous or unmatched viewer is
    never saved. The matched candidate is scoped to the download's blob origin, capped at
    ``MAX_FILE_SIZE_BYTES``, and must carry PDF magic. Because this is a best-effort backstop, any
    page/frame access error during candidate discovery or reading also fails closed.
    """
    download_origin = _blob_url_origin(download.url)
    if download_origin is None:
        return None

    suggested = os.path.basename(download.suggested_filename or "").strip().lower()
    if not suggested:
        # Non-sensitive: reason + booleans/counts only, never filename/title/blob URL/domain.
        LOG.info(
            "Adopted-session blob PDF iframe recovery skipped",
            workflow_run_id=workflow_run_id,
            reason="missing_suggested_filename",
        )
        return None

    try:
        src_titles = await _blob_iframe_src_titles(page)
        same_origin_candidates = [src for src in src_titles if _blob_url_origin(src) == download_origin]
        named = [
            src for src in same_origin_candidates if os.path.basename(src_titles[src]).strip().lower() == suggested
        ]

        if len(named) == 1:
            # Freshness gate: the candidate blob must be a live key in this action window's retention
            # Map, proving it was minted through the pre-click wrapper. A lingering iframe reusing a
            # common filename would otherwise pass the title/PDF gates and save the wrong document.
            freshness = await probe_blob_action_freshness(page, named[0], workflow_run_id=workflow_run_id)
            if not freshness.retained:
                LOG.info(
                    "Adopted-session blob PDF iframe recovery skipped",
                    workflow_run_id=workflow_run_id,
                    reason="not_action_fresh" if freshness.state_observed else "retention_state_unobservable",
                )
                return None
            # The single filename match must itself be a real PDF; otherwise fail closed rather than
            # fall back to any other on-screen blob iframe.
            return await _read_blob_pdf_bytes(page, named[0], workflow_run_id)

        if len(named) > 1:
            LOG.warning(
                "Adopted-session blob PDF iframe recovery skipped",
                workflow_run_id=workflow_run_id,
                reason="duplicate_filename_match",
                candidate_count=len(same_origin_candidates),
                match_count=len(named),
            )
        elif not same_origin_candidates:
            LOG.info(
                "Adopted-session blob PDF iframe recovery skipped",
                workflow_run_id=workflow_run_id,
                reason="no_same_origin_blob_iframe",
                candidate_count=0,
            )
        else:
            LOG.info(
                "Adopted-session blob PDF iframe recovery skipped",
                workflow_run_id=workflow_run_id,
                reason="no_filename_title_match",
                candidate_count=len(same_origin_candidates),
            )
        return None
    except Exception as exc:
        # Best-effort backstop: a page/frame torn down mid-discovery (e.g. remote session closing)
        # must fail closed, not raise. CancelledError (BaseException) still propagates.
        LOG.warning(
            "Adopted-session blob PDF iframe recovery skipped",
            workflow_run_id=workflow_run_id,
            reason="recovery_error",
            error_type=type(exc).__name__,
        )
        return None


async def _collect_inline_iframe_src_candidates(page: Page) -> list[str]:
    """Every http(s) <iframe> src on the page, deduped, order-preserving.

    Uncapped on purpose: this feeds the before/after action-window comparison, so an arbitrary cap
    (e.g. the first N in DOM order) could drop a newly-appended target that sits past many pre-existing
    ad/tracker iframes, silently no-opping recovery. Enumeration is cheap (one evaluate per frame);
    the fetch work it gates is bounded by the whole-operation budget in handle_action instead.
    """
    candidates: list[str] = []
    seen: set[str] = set()
    main_frame = page.main_frame
    for frame in _all_page_frames(page):
        # Route the main frame through the Page so SkyvernFrame.evaluate can attach any
        # context-level main-world prefix; child frames evaluate in-frame (prefixes are page-scoped).
        target: Page | Frame = page if frame is main_frame else frame
        try:
            srcs = await SkyvernFrame.evaluate(frame=target, expression=_INLINE_IFRAME_SRC_JS)
        except Exception:
            continue
        if not isinstance(srcs, list):
            continue
        for src in srcs:
            if not isinstance(src, str) or not src.startswith(("http://", "https://")) or src in seen:
                continue
            seen.add(src)
            candidates.append(src)
    return candidates


async def _recover_blocked_inline_pdf_download(
    page: Page,
    download_dir: Path,
    workflow_run_id: str | None,
    *,
    iframe_srcs_before: list[str],
) -> Path | None:
    """Recover a PDF whose inline iframe render was refused by a browser frame-embedding policy.

    A download-intent click can leave the statement in an <iframe> the browser refuses to display
    (its bytes still download fine, they just can't be framed). To tie the recovery to *this* click
    rather than any PDF on the page, only iframes that appeared in the action window are candidates:
    an iframe whose src is absent from ``iframe_srcs_before`` (the pre-action snapshot, required) is
    either newly attached or an existing iframe navigated to a new src. Pre-existing frames (adverts,
    a reCAPTCHA anchor, a previously opened statement) are excluded because their src is already in
    the baseline. Recovery only fires when exactly one candidate src is a PDF; zero or several
    equally-plausible candidate srcs fail closed (return None) so we never save an unrelated file —
    two distinct candidate URLs are ambiguous even when their bytes happen to match. The recovered
    bytes are written to ``download_dir`` to rejoin the normal finalize / dedupe / filename / upload
    lifecycle.
    """
    after = await _collect_inline_iframe_src_candidates(page)
    if not after:
        return None
    baseline = set(iframe_srcs_before)
    action_window_candidates = [src for src in after if src not in baseline]
    if not action_window_candidates:
        return None

    pdf_candidate: tuple[str, bytes] | None = None
    for src in action_window_candidates:
        try:
            data = await SkyvernFrame.read_http_url_bytes(
                page,
                src,
                workflow_run_id=workflow_run_id,
                max_size_bytes=MAX_FILE_SIZE_BYTES,
                timeout_ms=_BLOCKED_INLINE_PDF_RECOVERY_TIMEOUT_SECONDS * 1000,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            LOG.debug(
                "Blocked-iframe recovery fetch raised; trying next candidate",
                workflow_run_id=workflow_run_id,
                exc_info=True,
            )
            continue
        if not data or not _looks_like_pdf(data):
            continue
        if pdf_candidate is not None:
            # A second distinct candidate src is a PDF: the action window is ambiguous, fail closed.
            LOG.warning(
                "Multiple inline PDF candidates appeared in the action window; not recovering any",
                workflow_run_id=workflow_run_id,
            )
            return None
        pdf_candidate = (src, data)

    if pdf_candidate is None:
        return None

    src, data = pdf_candidate
    base_name = normalize_download_filename(os.path.basename(urllib.parse.urlparse(src).path), "application/pdf")
    if not base_name:
        # A URL with no path basename (e.g. ".../?token=...") yields no name; the bytes are a
        # confirmed PDF, so give the persisted file a .pdf extension instead of an extension-less one.
        base_name = "statement.pdf"
    handled_by_interceptor, intercepted_target = publish_download_bytes_for_context(
        page.context,
        data,
        base_name,
        "application/pdf",
    )
    if handled_by_interceptor:
        return intercepted_target
    target = _download_target_path(download_dir, base_name)
    try:
        download_dir.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    except OSError:
        LOG.warning(
            "Failed to persist recovered blocked-iframe PDF",
            workflow_run_id=workflow_run_id,
            exc_info=True,
        )
        return None
    LOG.info(
        "Recovered a browser-blocked inline PDF via same-origin fetch",
        workflow_run_id=workflow_run_id,
        recovered_bytes=len(data),
        download_target=str(target),
    )
    return target


def _needs_blank_page_restore(page: Page, page_url_before_download: str) -> bool:
    return page.url in BLANK_PAGE_URLS and page_url_before_download not in BLANK_PAGE_URLS


async def _restore_page_url_after_download(
    browser_state: BrowserState, page: Page, page_url_before_download: str
) -> bool:
    """Return whether the page was navigated, so callers can stop a batch built on the old document."""
    if not _needs_blank_page_restore(page, page_url_before_download):
        return False
    LOG.warning(
        "Working page navigated to blank after download action, navigating back to original URL",
        original_url=page_url_before_download,
    )
    try:
        await browser_state.navigate_to_url(page=page, url=page_url_before_download)
    except Exception:
        LOG.warning(
            "Failed to navigate back to original URL after blank page from download",
            original_url=page_url_before_download,
            exc_info=True,
        )
    return True


async def _cleanup_captured_download_popup(
    popup: Page, browser_state: BrowserState, page: Page, page_url_before_download: str
) -> None:
    cleanup = [("popup_close", popup.close())]
    if _needs_blank_page_restore(page, page_url_before_download):
        cleanup.append(
            ("working_page_recovery", browser_state.navigate_to_url(page=page, url=page_url_before_download))
        )
    results = await asyncio.gather(*(operation for _, operation in cleanup), return_exceptions=True)
    for (operation, _), result in zip(cleanup, results, strict=True):
        if isinstance(result, BaseException):
            LOG.warning(
                "Captured download popup cleanup operation failed",
                operation=operation,
                exception_type=type(result).__name__,
                exc_info=(type(result), result, result.__traceback__),
            )


async def _recover_download_page(
    browser_state: BrowserState,
    task: Task,
    page_url_before_download: str,
    timeout_seconds: float,
    recovery_site: str,
) -> Page | None:
    LOG.warning(
        "Working page closed during download action; recreating it before continuing",
        workflow_run_id=task.workflow_run_id,
        recovery_site=recovery_site,
    )
    recovered_page: Page | None = None
    try:
        async with asyncio.timeout(timeout_seconds):
            try:
                recovered_page = await browser_state.new_page()
            except Exception:
                # A context that cannot open a page is unusable regardless of what the driver
                # reports: the browser-close event has often not propagated yet when the pending
                # newPage call rejects, so is_connected() still answers True here.
                browser_address = task.browser_address
                if task.browser_session_id:
                    browser_address = await app.PERSISTENT_SESSIONS_MANAGER.get_browser_address_if_ready(
                        session_id=task.browser_session_id,
                        organization_id=task.organization_id,
                    )
                    if not browser_address:
                        raise RuntimeError("Persistent browser address is unavailable for download recovery")
                await browser_state.reconnect(
                    proxy_location=task.proxy_location,
                    workflow_run_id=task.workflow_run_id,
                    workflow_permanent_id=task.workflow_permanent_id,
                    organization_id=task.organization_id,
                    extra_http_headers=task.extra_http_headers,
                    cdp_connect_headers=task.cdp_connect_headers,
                    browser_address=browser_address,
                    browser_profile_id=browser_state.browser_artifacts.applied_browser_profile_id,
                )
                recovered_page = await browser_state.get_working_page()
                if recovered_page is None:
                    raise RuntimeError("Browser reconnect did not create a working page")
            await browser_state.navigate_to_url(page=recovered_page, url=page_url_before_download)
            await browser_state.set_active_page(recovered_page)
    except Exception:
        LOG.warning(
            "Failed to recreate working page after download action closed it",
            workflow_run_id=task.workflow_run_id,
            recovery_site=recovery_site,
            exc_info=True,
        )
        if recovered_page is not None:
            try:
                await recovered_page.close()
            except Exception:
                LOG.warning(
                    "Failed to close replacement page after working page recovery failed",
                    workflow_run_id=task.workflow_run_id,
                    recovery_site=recovery_site,
                    exc_info=True,
                )
        return None
    return recovered_page


def _canonical_download_duplicate_stem(stem: str) -> str:
    """Return a stem with common browser duplicate suffixes removed."""
    return DOWNLOAD_DUPLICATE_STEM_SUFFIX_RE.sub("", stem)


def _has_download_duplicate_suffix(stem: str) -> bool:
    """Return whether a stem carries a browser duplicate suffix."""
    return _canonical_download_duplicate_stem(stem) != stem


def _is_empty_duplicate_download_placeholder(file_path: str, non_empty_file_paths: set[str]) -> bool:
    """Return whether a 0-byte local file is a duplicate-name placeholder.

    Empty exports can be valid artifacts, so only remove a 0-byte file when a
    file carrying a browser duplicate suffix has the same extension and
    canonical stem as a non-empty local file, such as ``report_1.pdf`` next to
    ``report.pdf``.
    """
    file_dir = os.path.dirname(file_path)
    file_stem, file_suffix = os.path.splitext(os.path.basename(file_path))
    if not _has_download_duplicate_suffix(file_stem):
        return False

    file_canonical_stem = _canonical_download_duplicate_stem(file_stem)

    for non_empty_file_path in non_empty_file_paths:
        if os.path.dirname(non_empty_file_path) != file_dir:
            continue

        non_empty_stem, non_empty_suffix = os.path.splitext(os.path.basename(non_empty_file_path))
        if non_empty_suffix != file_suffix:
            continue

        non_empty_canonical_stem = _canonical_download_duplicate_stem(non_empty_stem)
        if file_stem != non_empty_stem and file_canonical_stem == non_empty_canonical_stem:
            return True

    return False


def _deduplicate_new_downloaded_file_paths(
    new_file_paths: set[str],
    workflow_run_id: str | None,
    observed_file_paths: set[str] | None = None,
) -> list[str]:
    """Filter junk local downloads and remove checksum duplicates.

    Remote browser-session URIs are returned untouched because the action
    process cannot hash or delete them locally. Local 0-byte files are removed
    only when they look like duplicate-name placeholders for a non-empty file
    observed in the run directory.
    """
    non_empty_file_paths: set[str] = set()
    for fp in observed_file_paths or new_file_paths:
        if not os.path.isfile(fp):
            continue
        try:
            if os.path.getsize(fp) > 0:
                non_empty_file_paths.add(fp)
        except OSError:
            continue

    seen_checksums: dict[str, str] = {}
    deduplicated_paths: list[str] = []
    for fp in sorted(new_file_paths):
        if not os.path.isfile(fp):
            deduplicated_paths.append(fp)
            continue

        try:
            file_size = os.path.getsize(fp)
            if file_size == 0:
                if _is_empty_duplicate_download_placeholder(fp, non_empty_file_paths):
                    LOG.warning(
                        "Removing 0-byte duplicate downloaded file placeholder",
                        file=os.path.basename(fp),
                        workflow_run_id=workflow_run_id,
                    )
                    os.remove(fp)
                else:
                    deduplicated_paths.append(fp)
                continue
            checksum = calculate_sha256_for_file(fp)
        except OSError:
            LOG.warning(
                "Downloaded file disappeared before deduplication",
                file=os.path.basename(fp),
                workflow_run_id=workflow_run_id,
                exc_info=True,
            )
            continue

        if checksum in seen_checksums:
            LOG.info(
                "Removing duplicate downloaded file from single action",
                file=os.path.basename(fp),
                duplicate_of=os.path.basename(seen_checksums[checksum]),
                checksum=checksum,
            )
            os.remove(fp)
        else:
            seen_checksums[checksum] = fp
            deduplicated_paths.append(fp)
    return deduplicated_paths


async def _screenshot_without_cursor(page: Page, **kwargs: Any) -> bytes:
    """Take a screenshot with cursor overlay hidden so it doesn't interfere with LLM analysis."""
    if SettingsManager.get_settings().BROWSER_CURSOR_VISUALIZATION:
        try:
            await SkyvernFrame.hide_cursor_overlay(page)
        except Exception:
            pass
        try:
            return await page.screenshot(**kwargs)
        finally:
            try:
                await SkyvernFrame.show_cursor_overlay(page)
            except Exception:
                pass
    return await page.screenshot(**kwargs)


class CustomSingleSelectResult:
    def __init__(self, skyvern_frame: SkyvernFrame) -> None:
        self.reasoning: str | None = None
        self.action_result: ActionResult | None = None
        self.action_type: ActionType | None = None
        self.value: str | None = None
        self.dropdown_menu: SkyvernElement | None = None
        self.skyvern_frame = skyvern_frame

    async def is_done(self) -> bool:
        # check if the dropdown menu is still on the page
        # if it still exists, might mean there might be multi-level selection
        # FIXME: only able to execute multi-level selection logic when dropdown menu detected
        if self.dropdown_menu is None:
            return True

        if not isinstance(self.action_result, ActionSuccess):
            return True

        if await self.dropdown_menu.get_locator().count() == 0:
            return True

        return not await self.skyvern_frame.get_element_visible(self.dropdown_menu.get_locator())


def is_ul_or_listbox_element_factory(
    incremental_scraped: IncrementalScrapePage, task: Task, step: Step
) -> Callable[[dict], Awaitable[bool]]:
    async def wrapper(element_dict: dict) -> bool:
        element_id: str = element_dict.get("id", "")
        try:
            element = await SkyvernElement.create_from_incremental(incremental_scraped, element_id)
        except Exception:
            LOG.debug(
                "Failed to element in the incremental page",
                element_id=element_id,
                exc_info=True,
            )
            return False

        if element.get_tag_name() == "ul":
            return True

        if await element.get_attr("role") == "listbox":
            return True

        return False

    return wrapper


CheckFilterOutElementIDFunc = Callable[[dict, Page | Frame], Awaitable[bool]]


def check_existed_but_not_option_element_in_dom_factory(
    dom: DomUtil,
) -> CheckFilterOutElementIDFunc:
    async def helper(element_dict: dict, frame: Page | Frame) -> bool:
        element_id: str = element_dict.get("id", "")
        if not element_id:
            return False
        try:
            locator = frame.locator(f"[{SKYVERN_ID_ATTR}={element_id}]")
            current_element = SkyvernElement(
                locator=locator,
                frame=frame,
                static_element=element_dict,
                engine_selection=dom.engine_selection,
            )
            if await current_element.is_custom_option():
                return False
            return await dom.check_id_in_dom(element_id)
        except Exception:
            LOG.debug(
                "Failed to check if the element is a custom option, going to keep the element in the incremental tree",
                exc_info=True,
                element_id=element_id,
            )
            return False

    return helper


def check_disappeared_element_id_in_incremental_factory(
    incremental_scraped: IncrementalScrapePage,
) -> CheckFilterOutElementIDFunc:
    current_element_to_dict = copy.deepcopy(incremental_scraped.id_to_css_dict)

    async def helper(element_dict: dict, frame: Page | Frame) -> bool:
        element_id: str = element_dict.get("id", "")
        if not current_element_to_dict.get(element_id, ""):
            return False

        try:
            skyvern_element = await SkyvernElement.create_from_incremental(
                incre_page=incremental_scraped, element_id=element_id
            )
        except Exception:
            LOG.debug(
                "Failed to create skyvern element, going to drop the element from incremental tree",
                exc_info=True,
                element_id=element_id,
            )
            return True

        skyvern_frame = incremental_scraped.skyvern_frame
        return not await skyvern_frame.get_element_visible(skyvern_element.get_locator())

    return helper


async def filter_out_elements(
    frame: Page | Frame, element_tree: list[dict], check_filter: CheckFilterOutElementIDFunc
) -> list[dict]:
    new_element_tree = []
    for element in element_tree:
        children_elements = element.get("children", [])
        if len(children_elements) > 0:
            children_elements = await filter_out_elements(
                frame=frame, element_tree=children_elements, check_filter=check_filter
            )
        if await check_filter(element, frame):
            new_element_tree.extend(children_elements)
        else:
            element["children"] = children_elements
            new_element_tree.append(element)
    return new_element_tree


def clean_and_remove_element_tree_factory(
    task: Task,
    step: Step,
    check_filter_funcs: list[CheckFilterOutElementIDFunc],
    engine_selection: BrowserEngineSelection | None = UNSET_SELECTION,
) -> CleanupElementTreeFunc:
    async def helper_func(frame: Page | Frame, url: str, element_tree: list[dict]) -> list[dict]:
        element_tree = await app.AGENT_FUNCTION.cleanup_element_tree_factory(
            task=task, step=step, engine_selection=engine_selection
        )(frame, url, element_tree)
        for check_filter in check_filter_funcs:
            element_tree = await filter_out_elements(frame=frame, element_tree=element_tree, check_filter=check_filter)

        return element_tree

    return helper_func


async def check_phone_number_format(
    value: str,
    action: actions.InputTextAction,
    skyvern_element: SkyvernElement,
    scraped_page: ScrapedPage,
    task: Task,
    step: Step,
) -> str:
    # check the phone number format
    LOG.info(
        "Input is a tel input, trigger phone number format checking",
        action=action,
        element_id=skyvern_element.get_id(),
    )

    new_scraped_page = await scraped_page.generate_scraped_page_without_screenshots()
    html = new_scraped_page.build_element_tree(html_need_skyvern_attrs=False)
    prompt = prompt_engine.load_prompt(
        template="check-phone-number-format",
        context=action.intention,
        current_value=value,
        navigation_goal=task.navigation_goal,
        navigation_payload_str=json.dumps(task.navigation_payload),
        elements=html,
        local_datetime=datetime.now(skyvern_context.ensure_context().tz_info).isoformat(),
    )

    json_response = await get_org_aware_secondary_llm_api_handler(default=app.SECONDARY_LLM_API_HANDLER)(
        prompt=prompt, step=step, prompt_name="check-phone-number-format"
    )

    check_phone_number_format_response = CheckPhoneNumberFormatResponse.model_validate(json_response)
    if (
        not check_phone_number_format_response.is_phone_number_input
        or check_phone_number_format_response.is_current_format_correct
        or not check_phone_number_format_response.recommended_phone_number
    ):
        return value

    LOG.info(
        "The current phone number format is incorrect, using the recommended phone number",
        element_id=skyvern_element.get_id(),
        source_digit_count=len(_phone_digits(value)),
        recommended_digit_count=len(_phone_digits(check_phone_number_format_response.recommended_phone_number)),
        digit_count_changed=len(_phone_digits(value))
        != len(_phone_digits(check_phone_number_format_response.recommended_phone_number)),
    )
    return check_phone_number_format_response.recommended_phone_number


def _phone_digits(value: str | None) -> str:
    return re.sub(r"\D", "", value or "")


def _input_target_log_fields(*, is_tel: bool, text: str) -> dict[str, str | int]:
    if is_tel:
        return {"target_digit_count": len(_phone_digits(text))}
    return {"target_value": text}


def _browser_error_log_fields(exc: Exception, *, is_tel: bool) -> dict[str, str]:
    fields = {"error_type": type(exc).__name__}
    if not is_tel:
        fields["error_message"] = str(exc)
    return fields


def _phone_readback_digits_match(
    expected_digits: str,
    actual_digits: str,
    *,
    allow_nanp_country_prefix: bool = False,
) -> bool:
    if actual_digits == expected_digits:
        return True
    if not allow_nanp_country_prefix:
        return False
    return len(expected_digits) == 10 and actual_digits == f"1{expected_digits}"


def _has_explicit_nanp_country_code(value: str | None) -> bool:
    return re.match(r"\+1|1[ \-.(]", (value or "").strip()) is not None


def _nanp_national_digits(value: str | None) -> str | None:
    text = (value or "").strip()
    if re.search(r"[A-Za-z]", text):
        return None

    digits = _phone_digits(text)
    # A leading 1 counts as a country code only when written as one (+1, or 1 set off by a
    # separator); bare 11-digit strings can be non-NANP numbers whose first digit is 1.
    if re.fullmatch(r"1[0-9]{10}", digits) and _has_explicit_nanp_country_code(text):
        national_digits = digits[-10:]
    elif re.fullmatch(
        r"\([0-9]{3}\) [0-9]{3}-[0-9]{4}|[0-9]{3}-[0-9]{3}-[0-9]{4}|"
        r"[0-9]{3}\.[0-9]{3}\.[0-9]{4}|[0-9]{3} [0-9]{3} [0-9]{4}",
        text,
    ):
        national_digits = digits
    else:
        return None

    if national_digits[0] not in "23456789" or national_digits[3] not in "23456789":
        return None
    return national_digits


def _tel_pattern_allows_bare_digits(pattern: str | None, bare_digits: str) -> bool:
    # An HTML `pattern` is an implicitly-anchored constraint on the field's value. If the bare national
    # digits do not satisfy it, the field requires a specific mask (e.g. "(ddd) ddd-dddd") and bare
    # digits would fail validation, so they must not be used. A missing or unparseable pattern is
    # treated as permissive.
    if not pattern:
        return True
    try:
        return re.fullmatch(pattern, bare_digits) is not None
    except re.error:
        return True


def _tel_constraints_accept(value: str, *, pattern: str | None, maxlength: str | None) -> bool:
    # Unlike the required bare-digit first attempt, optional +1 handling fails closed when the
    # browser's pattern cannot be interpreted safely by Python. A declared-but-empty pattern
    # matches only the empty string in HTML, so it must not be treated as absent.
    if pattern is not None:
        try:
            if re.fullmatch(pattern, value) is None:
                return False
        except re.error:
            return False
    if maxlength:
        try:
            max_chars = int(maxlength)
        except ValueError:
            return False
        if max_chars < 0 or len(value) > max_chars:
            return False
    return True


def _nanp_e164_fallback(value: str, *, pattern: str | None, maxlength: str | None) -> str | None:
    """Return a canonical E.164 retry only with explicit +1 evidence and permissive field constraints."""
    national_digits = _nanp_national_digits(value)
    if national_digits is None or not _has_explicit_nanp_country_code(value):
        return None
    e164_value = f"+1{national_digits}"
    if _tel_constraints_accept(e164_value, pattern=pattern, maxlength=maxlength):
        return e164_value
    return None


def _plan_tel_text(*, is_tel: bool, is_secret: bool, value: str, pattern: str | None) -> tuple[str, bool, bool]:
    # Decide how to fill a tel field. Returns (text_to_type, used_bare_nanp, run_format_check).
    # Bare national digits avoid the fill()-split behavior that can drop a digit in self-formatting fields.
    # This local transform is safe for secrets; the format-check LLM remains limited to non-secret fallbacks.
    national_digits = _nanp_national_digits(value) if is_tel else None
    if national_digits and _tel_pattern_allows_bare_digits(pattern, national_digits):
        return national_digits, True, False
    return value, False, is_tel and not is_secret


async def _is_tel_digit_fix_enabled(task: Task) -> bool:
    organization_id = task.organization_id
    if not organization_id:
        return False
    experimentation_provider = getattr(app, "EXPERIMENTATION_PROVIDER", None)
    if not experimentation_provider:
        return False
    try:
        # Bucket by org (not per-run) for a stable, monitorable ramp and clean rollback.
        return bool(
            await experimentation_provider.is_feature_enabled_cached(
                FIX_TEL_INPUT_DIGIT_DROP_FLAG,
                organization_id,
                properties={"organization_id": organization_id},
            )
        )
    except Exception:
        LOG.warning(
            "Failed to evaluate tel-digit-fix flag; defaulting to disabled",
            organization_id=organization_id,
            exc_info=True,
        )
        return False


async def _resolve_collapse_xp_assignment(
    experimentation_provider: BaseExperimentationProvider,
    task: Task,
    organization_id: str,
) -> bool:
    distinct_id = task.workflow_run_id or task.task_id
    if distinct_id in _COLLAPSE_XP_ASSIGNMENT_MEMO:
        return _COLLAPSE_XP_ASSIGNMENT_MEMO[distinct_id]

    try:
        assignment = bool(
            await experimentation_provider.resolve_feature_enabled_unrecorded(
                COLLAPSE_XP_ASSIGNMENT_FLAG,
                distinct_id,
                properties={"organization_id": organization_id},
            )
        )
    except Exception:
        if distinct_id in _COLLAPSE_XP_ASSIGNMENT_MEMO:
            return _COLLAPSE_XP_ASSIGNMENT_MEMO[distinct_id]
        _COLLAPSE_XP_ASSIGNMENT_MEMO[distinct_id] = False
        LOG.info(
            "collapse_xp_assignment",
            workflow_run_id=task.workflow_run_id,
            task_id=task.task_id,
            organization_id=organization_id,
            assigned=False,
            pinned_on_error=True,
        )
        raise

    if distinct_id in _COLLAPSE_XP_ASSIGNMENT_MEMO:
        return _COLLAPSE_XP_ASSIGNMENT_MEMO[distinct_id]
    _COLLAPSE_XP_ASSIGNMENT_MEMO[distinct_id] = assignment
    LOG.info(
        "collapse_xp_assignment",
        workflow_run_id=task.workflow_run_id,
        task_id=task.task_id,
        organization_id=organization_id,
        assigned=assignment,
        pinned_on_error=False,
    )
    return assignment


async def _resolve_collapse_gate(
    task: Task,
    family_flag: str,
    log_label: str,
    *,
    consult_assignment: bool = True,
) -> _CollapseGateResult:
    organization_id = task.organization_id
    if not organization_id:
        return _CollapseGateResult(False, None, False)
    experimentation_provider = getattr(app, "EXPERIMENTATION_PROVIDER", None)
    if not experimentation_provider:
        return _CollapseGateResult(False, None, False)
    try:
        # task.workflow_permanent_id is None on most fetch paths; fall back to context (SKY-8992).
        context = skyvern_context.current()
        workflow_permanent_id = task.workflow_permanent_id or (context.workflow_permanent_id if context else None)
        script_mode_run = bool(context and context.script_mode)
        # PostHog local evaluation cannot match exclusions when the property is absent.
        properties = {
            "organization_id": organization_id,
            "workflow_permanent_id": workflow_permanent_id or "",
            "script_mode": "true" if script_mode_run else "false",
        }
        family_enabled = bool(
            await experimentation_provider.is_feature_enabled_cached(
                family_flag,
                organization_id,
                properties=properties,
            )
        )
        if not family_enabled:
            return _CollapseGateResult(False, None, False)
        if not consult_assignment:
            # Family-only mode skips the umbrella (and its memo seed) so sibling families keep their control arms.
            return _CollapseGateResult(family_enabled, None, False)
        # Cached-script runs skip only the umbrella randomization: they must always be
        # in-treatment when the family is on, while the family flag stays the kill switch.
        if script_mode_run:
            return _CollapseGateResult(True, True, False)
        # PostHog hashes per flag key, so this umbrella is the only randomization source.
        # Family flags are kill switches and must never use percentage rollouts.
        assigned = await _resolve_collapse_xp_assignment(experimentation_provider, task, organization_id)
        return _CollapseGateResult(True, assigned, False)
    except Exception:
        LOG.warning(
            f"Failed to evaluate {log_label} flag; defaulting to disabled",
            organization_id=organization_id,
            exc_info=True,
        )
        return _CollapseGateResult(False, None, True)


async def _is_collapse_fanout_enabled(task: Task, family_flag: str, log_label: str) -> bool:
    gate = await _resolve_collapse_gate(task, family_flag, log_label)
    return gate.family_enabled and bool(gate.assigned)


async def _is_collapse_select_fanout_enabled(task: Task) -> bool:
    gate = await _resolve_collapse_gate(
        task,
        COLLAPSE_SELECT_FANOUT_FLAG,
        "collapse-select-fanout",
        consult_assignment=False,
    )
    return gate.family_enabled


async def _is_collapse_custom_select_fanout_enabled(task: Task) -> bool:
    return await _is_collapse_fanout_enabled(
        task,
        COLLAPSE_CUSTOM_SELECT_FANOUT_FLAG,
        "collapse-custom-select-fanout",
    )


async def _is_collapse_autocomplete_fanout_enabled(task: Task) -> bool:
    return await _is_collapse_fanout_enabled(
        task,
        COLLAPSE_AUTOCOMPLETE_FANOUT_FLAG,
        "collapse-autocomplete-fanout",
    )


async def _probe_tel_browser_validity(locator: Locator) -> bool | None:
    try:
        result = await locator.evaluate("(element) => element.validity?.valid ?? null")
    except Exception:
        return None
    return result if isinstance(result, bool) else None


async def verify_phone_input_digits(
    *,
    tag_name: str,
    locator: Locator,
    expected_value: str,
    allow_nanp_country_prefix: bool = False,
    pattern: str | None = None,
    maxlength: str | None = None,
    engine_selection: BrowserEngineSelection | None = None,
) -> int:
    # Compare normalized digits only — never the raw value, which may be a secret.
    actual_value = await get_input_value(tag_name=tag_name, locator=locator, engine_selection=engine_selection)
    expected_digits = _phone_digits(expected_value)
    actual_digits = _phone_digits(actual_value)
    # A field rendering a literal "+1" over the typed digits asserts NANP for itself; trust it only
    # when that rendered value also satisfies the field's declared constraints. Weaker markers
    # (bare or trunk "1") stay fail-loud.
    field_asserts_nanp = (actual_value or "").strip().startswith("+1") and _tel_constraints_accept(
        actual_value or "", pattern=pattern, maxlength=maxlength
    )
    if not _phone_readback_digits_match(
        expected_digits,
        actual_digits,
        allow_nanp_country_prefix=allow_nanp_country_prefix or field_asserts_nanp,
    ):
        raise PhoneNumberInputMismatch(
            expected_digit_count=len(expected_digits),
            actual_digit_count=len(actual_digits),
        )
    LOG.info(
        "Phone input read-back verified",
        expected_digit_count=len(expected_digits),
        actual_digit_count=len(actual_digits),
    )
    return len(actual_digits)


async def _verify_tel_input_after_fill(
    *,
    skyvern_element: SkyvernElement,
    tag_name: str,
    expected_value: str,
    allow_nanp_country_prefix: bool,
    pattern: str | None = None,
    maxlength: str | None = None,
    engine_selection: BrowserEngineSelection | None = None,
) -> int:
    return await verify_phone_input_digits(
        tag_name=tag_name,
        locator=skyvern_element.get_locator(),
        expected_value=expected_value,
        allow_nanp_country_prefix=allow_nanp_country_prefix,
        pattern=pattern,
        maxlength=maxlength,
        engine_selection=engine_selection,
    )


async def _fill_nanp_tel_with_readback(
    *,
    skyvern_element: SkyvernElement,
    tag_name: str,
    national_digits: str,
    e164_fallback: str | None,
    pattern: str | None = None,
    maxlength: str | None = None,
    engine_selection: BrowserEngineSelection | None = None,
    outcome: actions.TelInputOutcome | None = None,
    enforce_browser_validity: bool = False,
) -> (
    PhoneNumberInputMismatch
    | PhoneNumberInputBrowserValidityMismatch
    | PhoneNumberInputBrowserInteractionFailed
    | InvalidElementForTextInput
    | None
):
    """Fill affirmative NANP digits and verify every attempt.
    Retry atomically with national digits before constraint-safe E.164 for the least invasive recovery.
    """
    attempts = [("sequential_national", national_digits), ("atomic_national", national_digits)]
    if e164_fallback is not None:
        attempts.append(("atomic_e164", e164_fallback))

    for attempt_index, (strategy, value) in enumerate(attempts):
        strategy_enum = actions.TelInputStrategy(strategy)
        if outcome is not None:
            outcome.strategy = strategy_enum
            outcome.attempt_count = attempt_index + 1

        try:
            if strategy == "sequential_national":
                await skyvern_element.input_sequentially(text=value)
            else:
                await skyvern_element.input_clear()
                await skyvern_element.input_fill(text=value)
        except InvalidElementForTextInput as exc:
            return exc
        except Exception as exc:
            LOG.warning("Phone input browser interaction failed", error_type=type(exc).__name__)
            return PhoneNumberInputBrowserInteractionFailed()

        try:
            actual_digit_count = await _verify_tel_input_after_fill(
                skyvern_element=skyvern_element,
                tag_name=tag_name,
                expected_value=national_digits,
                allow_nanp_country_prefix=e164_fallback is not None,
                pattern=pattern,
                maxlength=maxlength,
                engine_selection=engine_selection,
            )
        except PhoneNumberInputMismatch as mismatch:
            browser_valid = await _probe_tel_browser_validity(skyvern_element.get_locator())
            if outcome is not None:
                outcome.actual_digit_count = mismatch.actual_digit_count
                outcome.browser_valid = browser_valid
            if attempt_index == len(attempts) - 1:
                return mismatch
            LOG.info(
                "Phone input read-back mismatch; trying next fill strategy",
                element_id=skyvern_element.get_id(),
                failed_strategy=strategy,
                next_strategy=attempts[attempt_index + 1][0],
                expected_digit_count=mismatch.expected_digit_count,
                actual_digit_count=mismatch.actual_digit_count,
            )
            continue
        except InvalidElementForTextInput as exc:
            return exc
        except Exception as exc:
            LOG.warning("Phone input read-back failed", error_type=type(exc).__name__)
            return PhoneNumberInputBrowserInteractionFailed()

        browser_valid = await _probe_tel_browser_validity(skyvern_element.get_locator())
        if outcome is not None:
            outcome.actual_digit_count = actual_digit_count
            outcome.browser_valid = browser_valid
        if enforce_browser_validity and browser_valid is False:
            if attempt_index == len(attempts) - 1:
                return PhoneNumberInputBrowserValidityMismatch()
            LOG.info(
                "Phone input failed browser validity; trying next fill strategy",
                element_id=skyvern_element.get_id(),
                failed_strategy=strategy,
                next_strategy=attempts[attempt_index + 1][0],
                expected_digit_count=len(_phone_digits(national_digits)),
                actual_digit_count=actual_digit_count,
            )
            continue
        return None
    return None


async def _log_tel_fallback_fill_digit_counts(
    *,
    skyvern_element: SkyvernElement,
    tag_name: str,
    expected_value: str,
    task_id: str | None,
    step_id: str | None,
    engine_selection: BrowserEngineSelection | None = None,
) -> tuple[int, int | None]:
    # Observability only: the LLM-fallback tel fill has no raising read-back, so a digit drop there is
    # otherwise invisible. Count-only (values may be secrets) and never fails the action.
    expected_digit_count = len(_phone_digits(expected_value))
    try:
        actual_value = await get_input_value(
            tag_name=tag_name,
            locator=skyvern_element.get_locator(),
            engine_selection=engine_selection,
        )
        actual_digit_count = len(_phone_digits(actual_value))
        LOG.info(
            "Tel fallback fill digit counts",
            expected_digit_count=expected_digit_count,
            actual_digit_count=actual_digit_count,
            digit_count_match=expected_digit_count == actual_digit_count,
            element_id=skyvern_element.get_id(),
            task_id=task_id,
            step_id=step_id,
        )
        return expected_digit_count, actual_digit_count
    except Exception as exc:
        LOG.warning(
            "Failed to read back tel fallback fill",
            task_id=task_id,
            step_id=step_id,
            error_type=type(exc).__name__,
        )
        return expected_digit_count, None


_CARD_NUMBER_MIN_DIGITS = 13
_CARD_NUMBER_MAX_DIGITS = 19


def _card_number_digits(value: str | None) -> str:
    return re.sub(r"\D", "", value or "")


def _luhn_valid(digits: str) -> bool:
    total = 0
    for index, char in enumerate(reversed(digits)):
        digit = ord(char) - ord("0")
        if index % 2 == 1:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0


def _is_probable_card_number(digits: str) -> bool:
    # A bare digit string that is card-length (13-19) and Luhn-valid. Luhn plus length is a strong,
    # self-limiting gate: phone numbers, order IDs, and free text almost never satisfy both, so the
    # read-back path stays off non-card fields.
    if not (_CARD_NUMBER_MIN_DIGITS <= len(digits) <= _CARD_NUMBER_MAX_DIGITS):
        return False
    return _luhn_valid(digits)


def _has_card_number_token(value: str | None) -> bool:
    # Lower-case first, then drop all separators, so camelCase and unseparated forms match too:
    # "card.number" / "card_number" / "cardNumber" / "cardnumber" / "cc-number" all count, while
    # "number" / "phone" / "cardholder" do not.
    normalized = re.sub(r"[^a-z0-9]", "", (value or "").lower())
    return "cardnumber" in normalized or "ccnumber" in normalized


_CARD_READBACK_SEPARATORS = r"[\s\-./]"


def _readable_card_digits(actual_value: str | None) -> str | None:
    # The rendered value reduced to a clean ASCII digit string, or None when it cannot be compared
    # (empty, masked with bullets/asterisks, or non-digit after stripping common group separators).
    # Python \s covers NBSP, which some auto-formatters emit between groups.
    if not actual_value:
        return None
    stripped = re.sub(_CARD_READBACK_SEPARATORS, "", actual_value)
    if not (stripped.isascii() and stripped.isdigit()):
        return None
    return stripped


def _card_readback_is_mismatch(expected_digits: str, actual_value: str | None) -> bool:
    # True only when the rendered value is a clean digit string that differs from the expected card
    # digits. An unreadable read-back (empty/masked) is not a mismatch: before clearing, the field is
    # left as typed rather than risking a wrong retype on a field we cannot read.
    actual_digits = _readable_card_digits(actual_value)
    return actual_digits is not None and actual_digits != expected_digits


def _card_readback_matches(expected_digits: str, actual_value: str | None) -> bool:
    # True only on a positive digit match. Used after clearing a known-bad value and atomically
    # re-entering it: success must be positively confirmed, so an unreadable/masked/mismatched retry
    # read-back is NOT a match and forces a loud failure rather than a silent wrong card.
    return _readable_card_digits(actual_value) == expected_digits


async def _is_card_number_field(skyvern_element: SkyvernElement) -> bool:
    # Deterministic, live-attr detection of a card-number field: an explicit cc-number autocomplete
    # token, or a numeric-only field. Paired with a Luhn-valid 13-19 digit value at the call site,
    # this stays off phone numbers, quantities, and other numeric inputs.
    autocomplete = (await skyvern_element.get_attr("autocomplete") or "").lower()
    if "cc-number" in autocomplete:
        return True
    for attr_name in ("name", "id"):
        if _has_card_number_token(await skyvern_element.get_attr(attr_name)):
            return True
    inputmode = (await skyvern_element.get_attr("inputmode") or "").lower()
    return inputmode == "numeric"


async def _fill_card_number_with_readback(
    *,
    skyvern_element: SkyvernElement,
    tag_name: str,
    text: str,
    expected_digits: str,
    engine_selection: BrowserEngineSelection | None = None,
) -> ActionFailure | None:
    # Type the card number, then read the rendered digits back. Character-by-character typing races an
    # auto-formatting field's caret restore and can scramble the value (SKY-11720); a single atomic
    # value-set formats once, without the race, so a mismatch is re-entered atomically before failing.
    await skyvern_element.input_sequentially(text=text)
    actual_value = await get_input_value(
        tag_name=tag_name, locator=skyvern_element.get_locator(), engine_selection=engine_selection
    )
    if not _card_readback_is_mismatch(expected_digits, actual_value):
        return None

    await skyvern_element.input_clear()
    await skyvern_element.input_fill(text=text)
    actual_value = await get_input_value(
        tag_name=tag_name, locator=skyvern_element.get_locator(), engine_selection=engine_selection
    )
    # Success after re-entry must be positively confirmed: a clean digit match. An empty/masked/
    # unreadable or still-mismatched retry read-back is NOT success -- fail loudly rather than silently
    # proceed with a value we deleted-and-could-not-verify.
    if _card_readback_matches(expected_digits, actual_value):
        return None

    actual_digits = _card_number_digits(actual_value)
    LOG.warning(
        "Card number read-back mismatch after retry",
        element_id=skyvern_element.get_id(),
        expected_digit_count=len(expected_digits),
        actual_digit_count=len(actual_digits),
    )
    return ActionFailure(
        CardNumberInputMismatch(
            expected_digit_count=len(expected_digits),
            actual_digit_count=len(actual_digits),
        )
    )


# Native input types whose DOM .value round-trips typed text exactly, so an exact read-back is meaningful.
# password/text/email/search/url and an untyped input default to a text field; tel/number/date-like inputs
# normalize or auto-format their value and are excluded, as are textarea/contenteditable/select (non-input
# sinks whose read-back is trimmed).
_EXACT_VALUE_INPUT_TYPES = frozenset({"password", "text", "email", "search", "url", ""})
# Input types whose caret Playwright's per-character type() can reset. setSelectionRange succeeds on these,
# so a field that drops focus on the input event lays the typed tail down reordered (SKY-13821). email and
# number are structurally immune -- setSelectionRange raises InvalidStateError on them and Playwright swallows
# it -- and Playwright's caret reset is <input>-only, so <textarea>/other tags are not eligible either. An
# untyped input ("") defaults to text and is vulnerable.
_CARET_VULNERABLE_INPUT_TYPES = frozenset({"password", "text", "search", "url", "tel", ""})
# Native terminal inputs whose ordinary free-text write is a single atomic fill (SKY-13821); everything else at
# the free-text seam (non-native editable sinks, tel formatting, combobox/search-bar) keeps per-character typing.
_NATIVE_FILL_TAGS = frozenset({"input", "textarea"})
# Mask glyphs a reveal/obfuscation widget may render into a non-password .value, optionally grouped by
# these separators (e.g. "•••• ••••" / "****-****").
_SECRET_MASK_CHARS = frozenset("•●·*∗＊")
_SECRET_MASK_SEPARATORS = re.compile(r"[\s\-./]")


def _exact_value_input_type(input_type: str | None) -> str:
    return (input_type or "").strip().lower()


_DATE_VALUE_SEPARATORS = re.compile(r"[^0-9]+")
_DATE_MASK_SEPARATORS = re.compile(r"[^a-z]+")


def _strict_date_mask_order(placeholder: str | None) -> tuple[str, ...] | None:
    # The day/month/year order a strict placeholder mask declares ("mm/dd/yyyy" -> ("m","d","y")), or None
    # when it is not a fully-specified mask: each separator-delimited token must be a pure run of one date
    # letter (d/dd, m/mm, yyyy), so prose, first-letter lookalikes, and partial years never define an order.
    if not placeholder:
        return None
    tokens = [token for token in _DATE_MASK_SEPARATORS.split(placeholder.strip().lower()) if token]
    if len(tokens) != 3:
        return None
    order: list[str] = []
    for token in tokens:
        if re.fullmatch(r"d{1,2}", token):
            order.append("d")
        elif re.fullmatch(r"m{1,2}", token):
            order.append("m")
        elif re.fullmatch(r"y{4}", token):
            order.append("y")
        else:
            return None
    if sorted(order) != ["d", "m", "y"]:
        return None
    return tuple(order)


def _canonical_iso_date(text: str, placeholder: str | None) -> str | None:
    # ``text`` as the YYYY-MM-DD an <input type=date> accepts, or None when it is not a date or the order
    # cannot be trusted. Order comes from the field's own strict mask; without a mask only an unambiguous
    # reading (four-digit year first, or a component above 12 pinning the day) is taken, and datetime()
    # rejects impossible calendar dates -- so an ambiguous value is refused, never written as a wrong date.
    parts = [part for part in _DATE_VALUE_SEPARATORS.split(text.strip()) if part]
    if len(parts) != 3 or not all(part.isdigit() for part in parts):
        return None
    order = _strict_date_mask_order(placeholder)
    if order is None:
        if len(parts[0]) == 4:
            order = ("y", "m", "d")
        elif len(parts[2]) == 4 and int(parts[0]) > 12:
            order = ("d", "m", "y")
        elif len(parts[2]) == 4 and int(parts[1]) > 12:
            order = ("m", "d", "y")
        else:
            return None
    fields = dict(zip(order, parts))
    if len(fields) != 3 or len(fields["y"]) != 4:
        return None
    try:
        return datetime(int(fields["y"]), int(fields["m"]), int(fields["d"])).strftime("%Y-%m-%d")
    except ValueError:
        return None


def _is_malformed_value_error(exc: BaseException) -> bool:
    # locator.fill() raises "Malformed value" when the live node is a structured input (a date input takes
    # only YYYY-MM-DD) and the value is not canonical; it validates before committing, so the field is left
    # untouched and the write can be retried in canonical form.
    return "malformed value" in str(exc).lower()


async def _live_date_input_canonical_value(
    skyvern_element: SkyvernElement,
    text: str,
    fill_error: BaseException,
    engine_selection: BrowserEngineSelection | None,
) -> str | None:
    # The canonical YYYY-MM-DD to re-fill after locator.fill() rejected ``text`` as malformed, or None when
    # the failure is not a live date input rejecting a recoverable value. The LIVE type and placeholder --
    # not the stale scraped type -- decide recovery, so a non-date field or an unrelated error yields None
    # and the caller re-raises unchanged.
    if not (_is_selected_engine_error(fill_error, engine_selection) and _is_malformed_value_error(fill_error)):
        return None
    try:
        if _exact_value_input_type(await skyvern_element.get_attr("type", mode="dynamic")) != "date":
            return None
        placeholder = await skyvern_element.get_attr("placeholder", mode="dynamic")
    except Exception:
        # A live read can itself fail when a navigation/DOM race destroys the node; without recovery
        # evidence return None so the caller re-raises the original malformed-value failure rather than
        # letting this secondary read error mask it (and be tolerated elsewhere as a false success).
        return None
    return _canonical_iso_date(text, placeholder)


async def _recover_atomic_fill_as_live_date(
    skyvern_element: SkyvernElement,
    text: str,
    fill_error: BaseException,
    engine_selection: BrowserEngineSelection | None,
) -> str | None:
    # Single owner of the malformed-value recovery shared by every path that atomically fills a native
    # exact-value input: the ordinary branch and the secret read-back branch. After an atomic fill raised,
    # a field scraped as text but live type=date rejects a displayed locale value as malformed, so re-fill
    # in canonical YYYY-MM-DD read from the live DOM. Returns the committed canonical value -- the field then
    # holds the ISO value, not ``text``, so the caller reads that value back, not ``text``. Returns None for an
    # ambiguous, non-date, or unrelated failure so the caller re-raises it unchanged and keeps its existing
    # semantics. No value is logged.
    canonical = await _live_date_input_canonical_value(skyvern_element, text, fill_error, engine_selection)
    if canonical is None:
        return None
    await skyvern_element.input_fill(text=canonical)
    return canonical


def _secret_readback_is_unreadable_mask(actual_value: str | None, *, is_password: bool) -> bool:
    # Unreadable only when the read-back is ENTIRELY mask glyphs (optionally separator-grouped, e.g.
    # "•••• ••••" / "****-****"): a custom reveal/mask widget is rendering only bullets, not the real
    # .value, so it cannot be compared and the field is left as typed. A value with any real character is
    # readable and compared exactly -- a revealed secret that merely contains a "*"/"•" must NOT be skipped
    # (that would silently defeat the fix for exactly the "show password" text fields this covers). Callers
    # check exact equality first, so a legitimately all-glyph secret that round-trips is a match, not a
    # skip. A native password input's .value is always the real typed value (mask is visual only).
    if is_password or not actual_value:
        return False
    stripped = _SECRET_MASK_SEPARATORS.sub("", actual_value)
    return bool(stripped) and all(char in _SECRET_MASK_CHARS for char in stripped)


def _maxlength_truncates_value(text: str, maxlength: str | None) -> bool:
    # A positive maxlength shorter than the value: the field holds only a prefix, so an atomic fill truncates
    # it. On an auto-advancing split field (SSN/account/OTP boxes) the per-character seam instead carries the
    # remaining characters to the sibling boxes, so route these to sequential entry rather than the atomic
    # write/read-back (SKY-13821). An unparseable/empty/absent maxlength is not a truncation.
    if not maxlength:
        return False
    try:
        limit = int(maxlength)
    except ValueError:
        return False
    return 0 <= limit < len(text)


def _secret_input_cannot_round_trip(text: str, *, maxlength: str | None) -> bool:
    # Some fields cannot hold the intended value exactly by their declared browser contract: a single-line
    # input strips CR/LF, and a positive maxlength shorter than the value truncates it. A read-back would
    # never equal the intended value there, so skip the exact-readback recovery rather than false-failing
    # an as-correct-as-possible fill.
    if "\r" in text or "\n" in text:
        return True
    return _maxlength_truncates_value(text, maxlength)


def _secret_readback_is_mismatch(expected: str, actual_value: str | None) -> bool:
    # A confirmed rendered value that differs from the intended secret, INCLUDING an empty read-back (the
    # fill was rejected/async-cleared): an empty credential field must be re-entered atomically rather than
    # left as a silent empty submit (SKY-12143). Masked/unreadable and can't-round-trip values are filtered
    # out by the caller before this comparison.
    return actual_value != expected


def _secret_readback_matches(expected: str, actual_value: str | None) -> bool:
    # Positive confirmation after clearing a known-bad value: only an exact, non-empty readable match is
    # success. An empty or still-mismatched retry read-back forces a loud failure over an unverified secret.
    return bool(actual_value) and actual_value == expected


def _is_navigation_teardown_error(exc: BaseException, engine_selection: BrowserEngineSelection | None) -> bool:
    # A read-back that races a form auto-submit sees the execution context torn down by navigation; the value
    # was accepted and submitted, so the caller treats it as success rather than a mismatch. Only THIS run's
    # engine errors qualify, and the message set matches the incremental handler's navigation tolerance.
    if not _is_selected_engine_error(exc, engine_selection):
        return False
    message = str(exc).lower()
    return "execution context was destroyed" in message or "navigation" in message or "target closed" in message


async def _fill_secret_with_readback(
    *,
    skyvern_element: SkyvernElement,
    tag_name: str,
    text: str,
    input_type: str,
    maxlength: str | None,
    engine_selection: BrowserEngineSelection | None = None,
    sequential_first: bool = False,
) -> ActionFailure | None:
    # A credential entered across the fill/type seam can race a hardened field's caret restore and rotate the
    # value, or be dropped by a controlled field and truncate it, submitting a wrong/empty credential with no
    # visible error (SKY-12143 rotation; SKY-12597/12579 truncation; same family as the card-number read-back,
    # SKY-11720). Plain native fields use a single atomic first fill, while typed widgets use the sequential
    # transport needed to emit keyboard events. On the native exact-value inputs this runs for, read the value
    # back and, on an empty or mismatched read-back, re-enter once more with the sequential transport (which can
    # advance a JS-auto-advancing widget's siblings where an atomic fill cannot), verifying again before failing
    # closed (SKY-13821). Fields that cannot round-trip the value by their
    # declared contract, or whose .value renders only mask glyphs, are left as filled. Logs carry only the
    # element id, never the secret, its length, or its character classes.
    is_password = input_type == "password"

    # Parity with the ordinary atomic-fill branch: re-resolve a locator that went stale between scrape and
    # write so a re-mounted controlled input is filled instead of timing out on a zero-match cached target.
    # The value the field is expected to hold after the first write and the transport the retry uses. Both stay
    # the intended secret unless a stale-scraped date recovered to canonical ISO below, in which case the field
    # holds YYYY-MM-DD and the retry must re-fill that ISO atomically (never the locale text, never the
    # per-character seam, which corrupts a structured date value).
    readback_expected = text
    date_recovered = False
    await skyvern_element.refresh_locator_if_stale()
    if sequential_first:
        await skyvern_element.input_sequentially(text=text)
    else:
        try:
            await skyvern_element.input_fill(text=text)
        except Exception as fill_error:
            # A stale-scraped text field that is live type=date rejects the displayed locale value as
            # malformed; recover in canonical ISO form. It then holds YYYY-MM-DD, not ``text`` -- but a
            # controlled date node can still accept that fill and asynchronously clear/rewrite it, so the
            # read-back below verifies the canonical value rather than trusting the accepted write. A
            # non-date/ambiguous failure re-raises unchanged.
            canonical = await _recover_atomic_fill_as_live_date(skyvern_element, text, fill_error, engine_selection)
            if canonical is None:
                raise
            readback_expected = canonical
            date_recovered = True

    if _secret_input_cannot_round_trip(readback_expected, maxlength=maxlength):
        LOG.info(
            "Leaving credential as filled: field cannot round-trip the value by its declared contract",
            element_id=skyvern_element.get_id(),
        )
        return None

    async def _read_back() -> tuple[str | None, bool]:
        # Returns (value, navigated). A read-back that races a form auto-submit sees the context torn down;
        # navigated=True means the value was submitted, so the caller stops and treats it as success.
        try:
            value = await get_input_value(
                tag_name=tag_name, locator=skyvern_element.get_locator(), engine_selection=engine_selection
            )
            return value, False
        except Exception as read_error:
            if _is_navigation_teardown_error(read_error, engine_selection):
                return None, True
            raise

    actual_value, navigated = await _read_back()
    if navigated:
        LOG.info("Credential field navigated after fill; treating as submitted", element_id=skyvern_element.get_id())
        return None
    # Exact equality first: a value that round-trips exactly is confirmed, even one made only of mask-like
    # characters -- so an all-"*" secret is a match, never misclassified as an unreadable mask.
    if not _secret_readback_is_mismatch(readback_expected, actual_value):
        return None

    if _secret_readback_is_unreadable_mask(actual_value, is_password=is_password):
        LOG.info(
            "Leaving credential as filled: rendered value is masked and cannot be verified",
            element_id=skyvern_element.get_id(),
        )
        return None

    # The mismatch can come from a JS-enforced auto-advancing widget (its per-box capacity is not a maxlength
    # attr, so it stayed atomic-fill eligible): repeating the same atomic fill can never emit the key events
    # that advance through the sibling boxes. Re-resolve a possibly re-mounted locator, then retry with the
    # sequential transport instead of another identical fill. The read-back below still verifies the target and
    # fails closed -- a sequential write that merely did not raise is not success (SKY-13821). A recovered date
    # instead re-fills the canonical ISO atomically: the per-character seam hard-throws on a structured date
    # input and the locale text is not what the field accepts.
    await skyvern_element.refresh_locator_if_stale()
    await skyvern_element.input_clear()
    if date_recovered:
        await skyvern_element.input_fill(text=readback_expected)
    else:
        await skyvern_element.input_sequentially(text=text)
    actual_value, navigated = await _read_back()
    if navigated:
        LOG.info(
            "Credential field navigated after the sequential retry; treating as submitted",
            element_id=skyvern_element.get_id(),
        )
        return None
    if _secret_readback_matches(readback_expected, actual_value):
        return None

    LOG.warning(
        "Secret input read-back mismatch after retry",
        element_id=skyvern_element.get_id(),
    )
    return ActionFailure(SecretInputMismatch())


def _caret_readback_eligible(*, tag_name: str, input_type: str | None, text: str, maxlength: str | None = None) -> bool:
    # A value can be reordered by the caret race only on a real <input> whose type keeps a caret
    # setSelectionRange can move (_CARET_VULNERABLE_INPUT_TYPES); a single character cannot be order-scrambled.
    # tel is caret-vulnerable but reformats its value (separators/spacing), so an exact read-back would
    # false-fail a correctly-submitted code -- the non-TOTP secret path excludes tel from exact round-trip for
    # the same reason. A tel-formatted single-field TOTP stays a typed residual rather than being read back.
    # A field that cannot hold the whole code (a split-code first box, maxlength shorter than the code) must
    # stay on the per-character seam whose key events advance focus across the boxes -- an atomic fill would
    # confine the code to the first box and falsely report success. Gates the single-field TOTP read-back
    # (SKY-13821).
    if _secret_input_cannot_round_trip(text, maxlength=maxlength):
        return False
    return (
        len(text) > 1
        and tag_name == InteractiveElement.INPUT
        and input_type in _CARET_VULNERABLE_INPUT_TYPES
        and input_type != "tel"
    )


def _normalize_textarea_line_endings(value: str) -> str:
    return value.replace("\r\n", "\n").replace("\r", "\n")


def _is_prefix_loss_truncation(*, tag_name: str, intended: str, rendered: str | None) -> bool:
    # input_sequentially sets intended[:-TEXT_PRESS_MAX_LENGTH] in one atomic fill, then types the last
    # TEXT_PRESS_MAX_LENGTH characters individually. A field that resets on the input event can wipe that
    # leading fill and keep only the per-character tail, so the rendered value is a proper suffix of the
    # intended text no longer than that tail (SKY-13631). The comparison is case-folded so a field that
    # transforms case still matches -- which also means a fully-present, only-case-changed value is NOT a
    # truncation. A longer, equal, or non-suffix value (e.g. an autocomplete expansion) is not a match.
    if rendered is None or not rendered or len(rendered) > TEXT_PRESS_MAX_LENGTH:
        return False
    if tag_name == "textarea":
        intended = _normalize_textarea_line_endings(intended)
        rendered = _normalize_textarea_line_endings(rendered)
    intended_cf = intended.casefold()
    rendered_cf = rendered.casefold()
    if not rendered_cf or len(rendered_cf) >= len(intended_cf):
        return False
    return intended_cf.endswith(rendered_cf)


async def _observe_input_value(
    *,
    skyvern_element: SkyvernElement,
    tag_name: str,
    engine_selection: BrowserEngineSelection | None = None,
) -> tuple[bool, str | None]:
    # Bounded, best-effort read-back for the truncation heal. Re-resolves a stale/re-mounted locator the same
    # way the neighbouring fill paths do, then caps the read at BROWSER_ACTION_TIMEOUT_MS so a field that
    # re-mounts on input -- the exact population this heal targets -- cannot stall on Playwright's 30s default
    # or raise out of an INPUT_TEXT action that already succeeded. Returns (observed, value); observed is
    # False when the value could not be obtained (stale, timeout, or driver error), and the caller must then
    # neither heal nor re-fill. Never logs the value.
    async def _read() -> str | None:
        await skyvern_element.refresh_locator_if_stale()
        return await get_input_value(
            tag_name=tag_name, locator=skyvern_element.get_locator(), engine_selection=engine_selection
        )

    try:
        value = await asyncio.wait_for(_read(), timeout=settings.BROWSER_ACTION_TIMEOUT_MS / 1000)
        return True, value
    except Exception:
        LOG.warning(
            "Free-text truncation read-back could not be obtained; leaving field as typed",
            element_id=skyvern_element.get_id(),
            exc_info=True,
        )
        return False, None


def _freetext_mismatch_failure(exc: FreeTextInputMismatch) -> ActionFailure:
    # Single builder for every FreeTextInputMismatch failure this heal seam returns, so the batch-stop shape
    # cannot drift between return points. stop_execution_on_failure (default True) halts a distinct-element
    # batch; skip_remaining_actions=True additionally makes the agent's duplicate-element-id branch terminal,
    # so a queued Submit targeting the SAME field is never dispatched (SKY-13631).
    failure = ActionFailure(exc)
    failure.skip_remaining_actions = True
    return failure


# Whole-helper budget (both attribute reads + the detached-clone evaluate). A timeout or any error returns None
# so the caller falls back to the generic fail-closed reason.
_STATIC_PROBE_TIMEOUT_S = 2.0
# HTML input types on which `maxlength` is NOT applicable. An absent or unknown type retains browser-default
# text semantics (so maxlength DOES apply); a known non-textual type (including `number`) disables it. `number`
# is the only type used here beyond maxlength -- its value sanitization is the sole type-based retention signal.
_STATIC_NON_TEXTUAL_INPUT_TYPES = frozenset(
    {
        "number",
        "range",
        "date",
        "datetime-local",
        "month",
        "week",
        "time",
        "color",
        "checkbox",
        "radio",
        "file",
        "hidden",
        "image",
        "button",
        "submit",
        "reset",
    }
)

# Detached-clone RETENTION check: builds a fresh tag-faithful clone -- an actual <textarea> for a textarea,
# else an <input> -- and reports only the two things that affect value RETENTION in this seam: whether a
# number input's value "stuck" (a number input sanitizes a non-numeric value to "" instead of retaining it)
# and the browser-NORMALIZED length `clone.value.length` (a textarea normalizes CRLF/lone-CR to a single LF
# and an input strips line breaks, so raw string length would over- or under-count). It does NOT read pattern,
# email/url validity, or `multiple`: those affect only HTML form validity, not whether the value is retained,
# so they must fall through to the generic fail-closed reason. The raw input-type attribute is set via setAttribute and the
# browser-normalized `clone.type` is read back (typeReflected; a textarea returns the marker "textarea") only
# to identify a number input and maxlength applicability. Maxlength is resolved by the browser: the raw
# attribute is set on the clone and `clone.maxLength` is read back, which reflects the HTML "rules for parsing
# non-negative integers" and equals the field's actually-enforced limit (-1 when absent or not a valid
# maximum). The raw type, raw maxlength, and isTextarea flag are passed as arguments, never interpolated into
# this source. The live field is untouched.
_STATIC_CONSTRAINT_CHECK_JS = """
([typeAttr, maxlengthRaw, isTextarea, candidate]) => {
  var clone = document.createElement(isTextarea ? "textarea" : "input");
  var typeReflected = isTextarea ? "textarea" : "text";
  if (!isTextarea && typeAttr !== null && typeAttr !== undefined) {
    clone.setAttribute("type", typeAttr);
    typeReflected = clone.type;
  }
  var maxLengthReflected = -1;
  if (maxlengthRaw !== null && maxlengthRaw !== undefined) {
    clone.setAttribute("maxlength", maxlengthRaw);
    maxLengthReflected = clone.maxLength;
  }
  clone.value = candidate;
  return {valueStuck: (clone.value === candidate), utf16Len: clone.value.length,
          maxLengthReflected: maxLengthReflected, typeReflected: typeReflected};
}
"""


async def _static_declared_constraint_evidence(
    *,
    skyvern_element: SkyvernElement,
    text: str,
    tag_name: str,
    engine_selection: BrowserEngineSelection | None = None,
) -> FreeTextInputMismatch | None:
    # Cheap, non-mutating RETENTION check run when a persistent refill mismatch is observed, bounded by a whole-helper budget. Only
    # two browser-declared constraints demonstrably affect whether the value is RETAINED in this seam: maxlength
    # and a number input's value sanitization. It reads just the LIVE maxlength and type (mode="dynamic"; the
    # post-refill scraped cache can be stale), evaluates a DETACHED tag-faithful clone (never the live field),
    # and returns a privacy-safe FreeTextInputMismatch only for an exact maxlength overflow or a number input
    # that sanitized the value -- never the raw candidate, characters, positions, or page text. HTML pattern,
    # email/url validity, and `multiple` do NOT prevent retention and are neither read nor diagnosed: they fall
    # through to the generic fail-closed reason. Anything else -- nothing applicable declared, the value is retained, or any
    # read/evaluate failure/timeout -- returns None. The incident's custom JS filter declares nothing, so it
    # falls through.
    tag = (tag_name or "").strip().lower()
    is_textarea = tag == "textarea"

    async def _inner() -> FreeTextInputMismatch | None:
        maxlength_attr = await skyvern_element.get_attr("maxlength", mode="dynamic")
        raw_type_attr = await skyvern_element.get_attr("type", mode="dynamic")
        has_max = maxlength_attr not in (None, "")
        # A lone type="text" (trimmed/case-insensitive) never sanitizes and never disables maxlength, so it
        # alone does not warrant the evaluate. Any other non-empty declared type MIGHT be `number` (only the
        # browser can normalize a stray-whitespace/mixed-case/unknown keyword), so the clone reflects it.
        type_declared = raw_type_attr is not None and raw_type_attr.strip() != ""
        type_maybe_number = type_declared and raw_type_attr.strip().lower() != "text"
        if is_textarea:
            if not has_max:  # only maxlength can apply on a textarea (no type / number sanitization)
                return None
        elif not has_max and not type_maybe_number:
            return None
        result = await SkyvernFrame.evaluate(
            frame=skyvern_element.get_frame(),
            expression=_STATIC_CONSTRAINT_CHECK_JS,
            arg=[raw_type_attr, maxlength_attr, is_textarea, text],
            engine_selection=engine_selection,
        )
        if not isinstance(result, dict):
            return None
        type_reflected = result.get("typeReflected")
        if not isinstance(type_reflected, str):
            return None
        # Applicability from the BROWSER-reflected IDL type (clone.type), not a Python-normalized string. A raw
        # attribute with stray whitespace/casing/unknown keyword reflects "text", so maxlength still applies and
        # it is not treated as a number input.
        textual_input = (not is_textarea) and type_reflected not in _STATIC_NON_TEXTUAL_INPUT_TYPES
        max_applies = has_max and (is_textarea or textual_input)
        is_number_input = (not is_textarea) and type_reflected == "number"
        element_id = skyvern_element.get_id()
        if max_applies:
            # Browser-authoritative: clone.maxLength reflects the HTML parse and equals the field's enforced
            # limit; a malformed/negative/absent attribute reflects -1 and must never invent a constraint (it
            # falls through to the generic fail-closed reason). Python int() is not used -- it diverges from Chromium (e.g.
            # int("1_0") == 10 but the field enforces 1; int("10.0") raises but the field enforces 10).
            reflected = result.get("maxLengthReflected")
            utf16 = result.get("utf16Len")
            if (
                isinstance(reflected, int)
                and not isinstance(reflected, bool)
                and reflected >= 0
                and isinstance(utf16, (int, float))
                and utf16 > reflected
            ):
                return FreeTextInputMismatch(
                    element_id=element_id, intended_length=len(text), declared_max_length=reflected
                )
        if is_number_input and result.get("valueStuck") is False:
            # A number input sanitizes a non-numeric value to "" -- it does not RETAIN it. This is the only
            # retention signal from an input type; email/url typeMismatch does NOT sanitize the value (it is
            # retained), so it is excluded and falls through to the generic fail-closed reason.
            return FreeTextInputMismatch(element_id=element_id, intended_length=len(text), declared_constraint="number")
        return None

    try:
        return await asyncio.wait_for(_inner(), timeout=_STATIC_PROBE_TIMEOUT_S)
    except Exception:
        LOG.info(
            "Static declared-constraint check aborted or timed out; caller will use the generic fail-closed mismatch reason",
            element_id=skyvern_element.get_id(),
            exc_info=True,
        )
        return None


async def _heal_truncated_freetext_input(
    *,
    skyvern_element: SkyvernElement,
    tag_name: str,
    text: str,
    is_secret_value: bool = False,
    engine_selection: BrowserEngineSelection | None = None,
) -> ActionFailure | None:
    # Preserves the deployed SKY-13631 coverage for the residual per-character seam: after the fill-first
    # default (SKY-13821) an ordinary native input fills atomically and cannot lose a prefix, but the paths
    # still typed character-by-character (tel formatting, a combobox/search-bar/in-context input) keep this
    # observational truncation guard. Only values longer than the split boundary can lose a prefix, so shorter
    # values and non-free-text tags are skipped without a read-back. Secret values are excluded outright --
    # their exact length must not reach the logs and an unmasked secret must not be rewritten from this generic
    # path; _fill_secret_with_readback owns that recovery. On the exact prefix-loss signature, re-enter the
    # value once with a single atomic fill; a matching or autocomplete-expanded value is left as typed so the
    # normal keystroke/autocomplete behavior is preserved. The pre-refill read-back is observational only:
    # bounded and best-effort, an unobtainable read-back detects no truncation and heals nothing. Once a refill
    # has run, the seam is integrity-gated:
    # confirmation requires a full case-folded match with the intended value (not merely the absence of the
    # loss signature). The sole accepted normalization is a textarea's browser-defined CRLF/lone-CR to LF
    # canonicalization. An unconfirmed or unobservable post-refill value fails closed with a structured
    # ActionFailure so a persistent partial value is never reported as success or followed by a batched Submit
    # (SKY-13631). No second write is ever attempted. Logs carry only lengths.
    if is_secret_value or tag_name not in _NATIVE_FILL_TAGS or len(text) <= TEXT_PRESS_MAX_LENGTH:
        return None
    observed, rendered = await _observe_input_value(
        skyvern_element=skyvern_element, tag_name=tag_name, engine_selection=engine_selection
    )
    if not observed or not _is_prefix_loss_truncation(tag_name=tag_name, intended=text, rendered=rendered):
        return None
    LOG.warning(
        "Free-text input lost its leading fill and kept only a trailing suffix; re-entering atomically",
        element_id=skyvern_element.get_id(),
        intended_length=len(text),
        rendered_length=len(rendered or ""),
    )
    await skyvern_element.refresh_locator_if_stale()
    await skyvern_element.input_fill(text=text)
    observed_after, confirmed_value = await _observe_input_value(
        skyvern_element=skyvern_element, tag_name=tag_name, engine_selection=engine_selection
    )
    confirmed_candidate = confirmed_value
    expected_candidate = text
    if tag_name == "textarea":
        confirmed_candidate = (
            _normalize_textarea_line_endings(confirmed_candidate) if confirmed_candidate is not None else None
        )
        expected_candidate = _normalize_textarea_line_endings(expected_candidate)
    refill_confirmed = (
        observed_after
        and confirmed_candidate is not None
        and confirmed_candidate.casefold() == expected_candidate.casefold()
    )
    LOG.info(
        "Free-text truncation refill read-back",
        element_id=skyvern_element.get_id(),
        intended_length=len(text),
        rendered_length=len(confirmed_value or ""),
        refill_confirmed=refill_confirmed,
    )
    if refill_confirmed:
        return None
    # The refill did not restore the full value; the action is already doomed and fails closed. When the
    # post-refill read-back was observable, consult a cheap, NON-MUTATING page-declared RETENTION check on a
    # detached clone (maxlength overflow / number-input value sanitization); a declared violation yields a
    # privacy-safe reason. There is NO live-field diagnostic afterward: clearing and re-typing the candidate
    # into a field whose action is already failing could trigger site-side input/autocomplete/XHR/auto-submit
    # effects that same-batch Submit blocking cannot contain (SKY-13631, r3755222701). Anything the static
    # check cannot explain -- including the incident, which declares nothing -- fails closed with the generic,
    # privacy-safe reason. Either way the ActionFailure stops the rest of the same batch, including a queued
    # Submit, and the site's own post-refill partial value is left untouched.
    if observed_after:
        static_failure = await _static_declared_constraint_evidence(
            skyvern_element=skyvern_element, text=text, tag_name=tag_name, engine_selection=engine_selection
        )
        if static_failure is not None:
            return _freetext_mismatch_failure(static_failure)
    return _freetext_mismatch_failure(
        FreeTextInputMismatch(element_id=skyvern_element.get_id(), intended_length=len(text))
    )


def _select_option_target_value(option: SelectOption) -> str | None:
    if option.label:
        return option.label
    if option.value:
        return option.value
    return None


def _select_option_labels_and_values(options: list[SkyvernOptionType]) -> tuple[list[str], list[str | None]]:
    labels: list[str] = []
    values: list[str | None] = []
    for option in options:
        labels.append(option.get("text") or option.get("value") or "")
        values.append(option.get("value"))
    return labels, values


def _normal_select_successful(action_results: list[ActionResult]) -> bool:
    return any(isinstance(action_result, ActionSuccess) for action_result in action_results)


def _select_value_is_ambiguous(options: list[SkyvernOptionType], value: str | None) -> bool:
    if value is None:
        return False
    return sum(1 for option in options if option.get("value") == value) > 1


# A focus-click before select_option only needs to focus a visible <select>; if an overlay
# intercepts it we want to bail fast and let select_option commit via the DOM, not stall for the
# full BROWSER_ACTION_TIMEOUT_MS on every covered select. (SKY-11618)
_SELECT_FOCUS_CLICK_TIMEOUT_MS = 1000


async def _best_effort_focus_click_before_select(*, locator: Locator, action: actions.SelectOptionAction) -> None:
    """Click a native <select> to focus it before select_option.

    Best-effort: an overlay (e.g. a consent/opt-out modal) can intercept this click, but
    select_option commits the value via the DOM regardless — a failed focus-click must not
    abort the selection. Uses a short timeout so an intercepted click bails fast. (SKY-11618)
    """
    try:
        await locator.click(timeout=_SELECT_FOCUS_CLICK_TIMEOUT_MS)
    except Exception:
        LOG.info(
            "Failed to click before select action; continuing to select_option",
            exc_info=True,
            action=action,
            locator=locator,
        )


_DROPDOWN_SURROGATE_ROLES = frozenset({"combobox", "listbox", "option"})
# aria-haspopup values that advertise a dropdown-style popup (a styled-select trigger). Per ARIA,
# "true" is equivalent to "menu"; only "dialog" (and "false") denote a non-dropdown popup, so those
# are excluded — a consent/opt-out modal trigger uses "dialog" and must not qualify.
_DROPDOWN_SURROGATE_HASPOPUP = frozenset({"listbox", "menu", "tree", "grid", "true"})


async def _is_dropdown_surrogate_blocker(element: SkyvernElement) -> bool:
    """True when a blocking element looks like a styled-dropdown surrogate for a native <select>
    (another <select>, a combobox/listbox/option widget, or a trigger with a dropdown
    aria-haspopup) rather than an unrelated overlay such as a consent/opt-out modal.

    Used only on the fallback after native select_option fails: a failed native selection does not
    prove the overlapping element is a real dropdown, so we require this evidence before retargeting
    the select action onto it — otherwise an unrelated modal could hijack the selection. Rejecting a
    genuine-but-unrecognized trigger here is safe: it returns the honest native-select failure rather
    than click-navigating the wrong element. (SKY-11618)
    """
    if element.get_tag_name() == InteractiveElement.SELECT:
        return True
    try:
        role = (await element.get_attr("role") or "").strip().lower()
        if role in _DROPDOWN_SURROGATE_ROLES:
            return True
        haspopup = (await element.get_attr("aria-haspopup") or "").strip().lower()
    except Exception:
        return False
    return haspopup in _DROPDOWN_SURROGATE_HASPOPUP


async def _select_deterministic_normal_option(
    *,
    action: actions.SelectOptionAction,
    skyvern_element: SkyvernElement,
    locator: Locator,
    matched_label: str | None,
    matched_value: str | None,
    matched_index: int | None,
) -> list[ActionResult]:
    action_result: list[ActionResult] = []
    is_success = False

    await _best_effort_focus_click_before_select(locator=locator, action=action)

    value = matched_value if matched_value is not None else matched_label
    if value is not None and not _select_value_is_ambiguous(skyvern_element.get_options(), value):
        try:
            await locator.select_option(
                value=value,
                timeout=settings.BROWSER_ACTION_TIMEOUT_MS,
            )
            is_success = True
            action_result.append(ActionSuccess())
        except Exception:
            action_result.append(ActionFailure(FailToSelectByValue(action.element_id)))
            LOG.info(
                "Failed to take select action by value",
                exc_info=True,
                action=action,
                locator=locator,
            )

    if not is_success and matched_label is not None and matched_label != value:
        try:
            await locator.select_option(
                label=matched_label,
                timeout=settings.BROWSER_ACTION_TIMEOUT_MS,
            )
            is_success = True
            action_result.append(ActionSuccess())
        except Exception:
            action_result.append(ActionFailure(FailToSelectByLabel(action.element_id)))
            LOG.info(
                "Failed to take select action by label",
                exc_info=True,
                action=action,
                locator=locator,
            )

    if not is_success and matched_index is not None:
        if matched_index >= len(skyvern_element.get_options()):
            action_result.append(ActionFailure(OptionIndexOutOfBound(action.element_id)))
            LOG.info(
                "option index is out of bound",
                action=action,
                locator=locator,
            )
        else:
            try:
                await locator.select_option(
                    index=matched_index,
                    timeout=settings.BROWSER_ACTION_TIMEOUT_MS,
                )
                is_success = True
                action_result.append(ActionSuccess())
            except Exception:
                action_result.append(ActionFailure(FailToSelectByIndex(action.element_id)))
                LOG.info(
                    "Failed to click on the option by index",
                    exc_info=True,
                    action=action,
                    locator=locator,
                )

    if len(action_result) == 0:
        action_result.append(ActionFailure(EmptySelect(element_id=action.element_id)))

    return action_result


async def _normal_select_readback_contradicts(
    *,
    locator: Locator,
    matched_index: int,
    matched_label: str | None,
    matched_value: str | None,
) -> bool:
    """True only when a readable read-back disagrees with the deterministic selection.

    An unreadable read-back is not a contradiction. select_option has already reported the value
    committed, and a select's own change event can rerender or navigate the page away, detaching the
    element; re-running the LLM fallback against that stale locator only fails again by value, label,
    and index. Same reasoning as `is_post_dispatch_click_timeout`: the side effect landed, so a
    timed-out post-action read must not trigger a duplicating fallback chain.
    """
    try:
        selection = await locator.evaluate(
            r"""
            (select) => {
                const normalize = (value) => (value ?? "").replace(/\s+/g, " ").trim();
                if (!(select instanceof HTMLSelectElement)) {
                    return { index: null, label: null, value: normalize(select?.value) };
                }
                const option = select.options[select.selectedIndex] ?? null;
                return {
                    index: select.selectedIndex,
                    label: option ? normalize(option.textContent) : null,
                    value: option ? normalize(option.value) : normalize(select.value),
                };
            }
            """,
            timeout=settings.BROWSER_ACTION_TIMEOUT_MS,
        )
    except Exception:
        LOG.info(
            "Failed to read normal select option after deterministic selection; keeping select_option success",
            expected_index=matched_index,
            expected_label=matched_label,
            expected_value=matched_value,
            exc_info=True,
        )
        return False

    if not isinstance(selection, dict):
        LOG.info(
            "Normal select read-back returned unexpected payload; keeping select_option success",
            expected_index=matched_index,
            expected_label=matched_label,
            expected_value=matched_value,
            actual_selection=selection,
        )
        return False

    # The read-back JS trims, but scraped option text does not: domUtils.removeMultipleSpaces()
    # collapses whitespace runs without trimming, and returns early when there is no double
    # space/tab/newline, so a single edge space survives verbatim into matched_label/matched_value.
    expected_label = " ".join(matched_label.split()) if matched_label is not None else None
    expected_value = " ".join(matched_value.split()) if matched_value is not None else None

    actual_index = selection.get("index")
    actual_value = selection.get("value")
    actual_label = selection.get("label") or actual_value
    if (
        actual_index == matched_index
        and actual_label == expected_label
        and (expected_value is None or actual_value == expected_value)
    ):
        return False

    LOG.info(
        "Normal select read-back did not match deterministic option",
        expected_index=matched_index,
        expected_label=expected_label,
        expected_value=expected_value,
        actual_index=actual_index,
        actual_label=actual_label,
        actual_value=actual_value,
    )
    return True


async def check_date_format(
    value: str,
    action: actions.InputTextAction,
    skyvern_element: SkyvernElement,
    task: Task,
    step: Step,
) -> str:
    # check the date format
    LOG.info(
        "Input is a date input, trigger date format checking",
        action=action,
        element_id=skyvern_element.get_id(),
    )

    prompt = prompt_engine.load_prompt(
        template="check-date-format",
        current_value=value,
        navigation_goal=task.navigation_goal,
        navigation_payload_str=json.dumps(task.navigation_payload),
        local_datetime=datetime.now(skyvern_context.ensure_context().tz_info).isoformat(),
    )

    json_response = await get_org_aware_secondary_llm_api_handler(default=app.SECONDARY_LLM_API_HANDLER)(
        prompt=prompt, step=step, prompt_name="check-date-format"
    )

    check_date_format_response = CheckDateFormatResponse.model_validate(json_response)
    if check_date_format_response.is_current_format_correct or not check_date_format_response.recommended_date:
        return value

    LOG.info(
        "The current date format is incorrect, using the recommended date",
        action=action,
        element_id=skyvern_element.get_id(),
        recommended_date=check_date_format_response.recommended_date,
    )
    return check_date_format_response.recommended_date


class AutoCompletionResult(BaseModel):
    auto_completion_attempt: bool = False
    incremental_elements: list[dict] = []
    action_result: ActionResult = ActionSuccess()


class ScopedXhrDownloadCapture:
    """Install on a page before a download action; remove after the polling window.

    Response-body capture is skipped when CDPDownloadInterceptor is active on
    the browser context, while request lifecycle tracking remains enabled for
    the bounded download wait.

    Automatically attaches to new pages opened during the action window
    (e.g. target="_blank" links) so XHR responses on child tabs are captured.
    """

    def __init__(self, page: Page, download_dir: Path, timeout_seconds: float = BROWSER_DOWNLOAD_TIMEOUT) -> None:
        self._page = page
        self._download_dir = download_dir
        self._timeout_seconds = timeout_seconds
        self._saved: set[str] = set()
        self._extra_pages: list[Page] = []
        self._capture_responses = False
        self._active = False
        self._accept_new_requests = False
        # Response-body drain count is separate from request-lifecycle tracking for the bounded wait extension.
        self._in_flight = 0
        self._response_tasks: set[asyncio.Task[None]] = set()
        self._in_flight_requests: set[Request] = set()
        self._admitted_requests: set[Request] = set()
        self._child_pages_with_bootstrap_allowance: set[Page] = set()
        self._drained = asyncio.Event()
        self._drained.set()

    @property
    def has_in_flight_requests(self) -> bool:
        return bool(self._in_flight_requests)

    def _on_request(self, request: Request) -> None:
        redirected_from_admitted_request = request.redirected_from in self._admitted_requests
        if not self._active or request.resource_type not in ("xhr", "fetch"):
            return

        child_page_has_bootstrap_allowance = False
        if not self._accept_new_requests and request.redirected_from is None:
            try:
                request_page = request.frame.page
                child_page_has_bootstrap_allowance = request_page in self._child_pages_with_bootstrap_allowance
            except Exception:
                pass

        if child_page_has_bootstrap_allowance:
            self._child_pages_with_bootstrap_allowance.discard(request_page)

        if self._accept_new_requests or redirected_from_admitted_request or child_page_has_bootstrap_allowance:
            self._in_flight_requests.add(request)
            self._admitted_requests.add(request)

    def _on_request_finished(self, request: Request) -> None:
        self._in_flight_requests.discard(request)

    def seal_in_flight_requests(self) -> None:
        self._accept_new_requests = False

    def _is_xhr_download(self, headers: dict[str, str], status: int) -> bool:
        """Check if an XHR response carries a downloadable file body.

        Reuses ``is_download_response`` for attachment cases. For inline
        responses, additionally accepts download MIME + explicit filename
        (the case ``is_download_response`` intentionally rejects for the
        global CDP path to avoid false positives on PDF previews).
        """
        if is_download_response(headers, status, resource_type="XHR"):
            return True
        if status >= 400:
            return False
        content_type = headers.get("content-type", "").split(";")[0].strip().lower()
        content_disposition = headers.get("content-disposition", "")
        if content_type not in DOWNLOAD_MIME_TYPES:
            return False
        return bool(re.search(r"filename\s*[*]?\s*=", content_disposition, re.IGNORECASE))

    def _on_response_event(self, response: Response) -> None:
        self._in_flight += 1
        self._drained.clear()
        task = asyncio.create_task(self._on_response(response))
        self._response_tasks.add(task)
        task.add_done_callback(self._on_response_done)

    def _on_response_done(self, task: asyncio.Task[None]) -> None:
        self._response_tasks.discard(task)
        self._in_flight -= 1
        if self._in_flight == 0:
            self._drained.set()
        if task.cancelled():
            return
        exception = task.exception()
        if exception is not None:
            LOG.warning("Unhandled XHR download response capture failure", exc_info=exception)

    async def _on_response(self, response: Response) -> None:
        try:
            if response.request not in self._admitted_requests:
                return
            headers = response.headers
            if not self._is_xhr_download(headers, response.status):
                return
            response_url = response.url
            raw_filename = extract_filename(
                {"content-disposition": headers.get("content-disposition", "")}, response_url
            )
            filename = normalize_download_filename(raw_filename, headers.get("content-type", ""))
            if not filename or filename in self._saved:
                return
            content_length = headers.get("content-length", "")
            if content_length:
                try:
                    if int(content_length) > MAX_FILE_SIZE_BYTES:
                        return
                except ValueError:
                    pass
            save_path = self._download_dir / filename
            body = await response.body()
            if len(body) > MAX_FILE_SIZE_BYTES:
                return
            try:
                with open(save_path, "xb") as f:
                    f.write(body)
            except FileExistsError:
                pass
            self._saved.add(filename)
            LOG.info(
                "XHR download captured during download action",
                filename=filename,
                size=len(body),
            )
        except Exception:
            LOG.warning("Failed to capture XHR download response", exc_info=True)

    async def _cancel_response_tasks(self) -> None:
        tasks = list(self._response_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _cancel_response_tasks_safely(self) -> None:
        cleanup_task = asyncio.create_task(self._cancel_response_tasks())
        try:
            await asyncio.shield(cleanup_task)
        except asyncio.CancelledError:
            await cleanup_task
            raise

    async def drain(self, timeout_seconds: float | None = None) -> bool:
        """Wait for owned XHR captures, cancelling and awaiting them at the deadline."""
        budget_seconds = self._timeout_seconds if timeout_seconds is None else timeout_seconds
        timed_out = False
        try:
            async with asyncio.timeout(budget_seconds):
                await self._drained.wait()
        except asyncio.TimeoutError:
            timed_out = True
            LOG.warning(
                "Timed out waiting for XHR download response capture drainage",
                timeout_seconds=budget_seconds,
                in_flight_response_count=self._in_flight,
            )
        finally:
            if self._response_tasks:
                await self._cancel_response_tasks_safely()
        return not timed_out

    def _on_new_page(self, page: Page) -> None:
        if not self._active:
            return
        self._child_pages_with_bootstrap_allowance.add(page)
        self._attach_page(page)
        self._extra_pages.append(page)

    def _attach_page(self, page: Page) -> None:
        if self._capture_responses:
            page.on("response", self._on_response_event)
        page.on("request", self._on_request)
        page.on("requestfinished", self._on_request_finished)
        page.on("requestfailed", self._on_request_finished)

    def _detach_page(self, page: Page) -> None:
        if self._capture_responses:
            page.remove_listener("response", self._on_response_event)
        page.remove_listener("request", self._on_request)
        page.remove_listener("requestfinished", self._on_request_finished)
        page.remove_listener("requestfailed", self._on_request_finished)

    def enable(self) -> None:
        self._capture_responses = not getattr(self._page.context, "_skyvern_cdp_download_active", False)
        self._active = True
        self._accept_new_requests = True
        self._attach_page(self._page)
        self._page.context.on("page", self._on_new_page)

    def disable(self) -> None:
        if not self._active:
            return
        self._active = False
        self._accept_new_requests = False
        self._detach_page(self._page)
        self._page.context.remove_listener("page", self._on_new_page)
        for page in self._extra_pages:
            try:
                self._detach_page(page)
            except Exception:
                pass
        self._extra_pages.clear()
        self._child_pages_with_bootstrap_allowance.clear()
        self._in_flight_requests.clear()


class ActionHandler:
    _handled_action_types: dict[
        ActionType,
        Callable[[Action, Page, ScrapedPage, Task, Step], Awaitable[list[ActionResult]]],
    ] = {}

    _setup_action_types: dict[
        ActionType,
        Callable[[Action, Page, ScrapedPage, Task, Step], Awaitable[list[ActionResult]]],
    ] = {}

    _teardown_action_types: dict[
        ActionType,
        Callable[[Action, Page, ScrapedPage, Task, Step], Awaitable[list[ActionResult]]],
    ] = {}

    @classmethod
    def register_action_type(
        cls,
        action_type: ActionType,
        handler: Callable[[Action, Page, ScrapedPage, Task, Step], Awaitable[list[ActionResult]]],
    ) -> None:
        cls._handled_action_types[action_type] = handler

    @classmethod
    def register_setup_for_action_type(
        cls,
        action_type: ActionType,
        handler: Callable[[Action, Page, ScrapedPage, Task, Step], Awaitable[list[ActionResult]]],
    ) -> None:
        cls._setup_action_types[action_type] = handler

    @classmethod
    def get_setup_for_action_type(
        cls,
        action_type: ActionType,
    ) -> Callable[[Action, Page, ScrapedPage, Task, Step], Awaitable[list[ActionResult]]] | None:
        return cls._setup_action_types.get(action_type)

    @classmethod
    def register_teardown_for_action_type(
        cls,
        action_type: ActionType,
        handler: Callable[[Action, Page, ScrapedPage, Task, Step], Awaitable[list[ActionResult]]],
    ) -> None:
        cls._teardown_action_types[action_type] = handler

    @staticmethod
    @traced(name="skyvern.agent.action", role="wrapper")
    async def handle_action(
        scraped_page: ScrapedPage,
        task: Task,
        step: Step,
        page: Page,
        action: Action,
        *,
        file_download_false_click_eligible: bool = False,
        allow_stale_refresh: bool = False,
    ) -> list[ActionResult]:
        # task_id, step_id auto-attached by @traced from SkyvernContext
        _action_span = otel_trace.get_current_span()
        _action_span.set_attribute("action_type", str(action.action_type))
        _action_span.set_attribute("step_order", step.order)
        if getattr(action, "element_id", None):
            _action_span.set_attribute("element_id", action.element_id)
        # Re-evaluated here, against the page as it is now, before anything downstream looks up a
        # browser, chooses the download-capturing path or persists a row.
        preflight_action(action, page, site="handle_action")
        action.started_at = naive_utc_now()
        # Hydrated/cached actions can arrive with a prior finished_at; clear it so the
        # exceptional-exit fallback below stamps this execution, not the previous one.
        action.finished_at = None
        browser_state = app.BROWSER_MANAGER.get_for_task(task.task_id, workflow_run_id=task.workflow_run_id)
        # TODO: maybe support all action types in the future(?)
        trigger_download_action = (
            isinstance(action, (SelectOptionAction, ClickAction, DownloadFileAction)) and action.download
        )
        # Without an explicit timeout, download actions can use a 120s no-signal grace plus a bounded 120s
        # in-flight extension (up to 240s total); an explicit action timeout remains the hard cap.
        _action_span.set_attribute("triggers_download", trigger_download_action)
        _tracer = otel_trace.get_tracer("skyvern")
        if not trigger_download_action:
            # Authorizes the same-action download bypass in handle_click_action. This is decoupled
            # from popup grace: the bypass only skips the dead dropdown rescrape and never persists,
            # so a FileDownloadBlock false-click candidate arms it regardless of the grace setting.
            false_click_bypass_eligible = (
                file_download_false_click_eligible and isinstance(action, ClickAction) and action.download is False
            )
            # Popup cleanup runs whenever an eligible click could mint a download on a popup, so the
            # marker popup never lingers as the working page. Only persistence -- and its grace wait --
            # stays gated on grace > 0, since capturing/persisting the download is what carries the cost.
            capture_false_click_popup = false_click_bypass_eligible and browser_state is not None
            persist_false_click_download = (
                capture_false_click_popup and settings.FILE_DOWNLOAD_FALSE_CLICK_POPUP_GRACE_SECONDS > 0
            )
            if not capture_false_click_popup:
                false_click_eligible_token = (
                    _false_click_download_eligible.set(True) if false_click_bypass_eligible else None
                )
                try:
                    with traced_span(_tracer, "skyvern.agent.action.handle_inner") as _hi_span:
                        apply_context_attrs(_hi_span)
                        results = await ActionHandler._handle_action(
                            scraped_page=scraped_page,
                            task=task,
                            step=step,
                            page=page,
                            action=action,
                            allow_stale_refresh=allow_stale_refresh,
                        )
                finally:
                    if false_click_eligible_token is not None:
                        _false_click_download_eligible.reset(false_click_eligible_token)
            else:
                assert browser_state is not None
                page_url_before_download = page.url
                with traced_span(_tracer, "skyvern.agent.action.false_click_download"):
                    false_click_download_event: asyncio.Future[tuple[Download, Page]] = (
                        asyncio.get_running_loop().create_future()
                    )
                    download_callbacks: list[tuple[Page, Callable[[Download], None]]] = []

                    def on_popup(download_page: Page) -> None:
                        def capture_download(download: Download) -> None:
                            if not false_click_download_event.done():
                                false_click_download_event.set_result((download, download_page))

                        download_page.on("download", capture_download)
                        download_callbacks.append((download_page, capture_download))
                        # Record identity for the task-scoped late cleanup too: if the download is
                        # credited only after this seam returns, the credit path can still close the
                        # marker popup. In-seam cleanup below stays the primary close; the claim is a
                        # superset backstop, deduped by exact Page identity.
                        _claim_context = skyvern_context.current()
                        if _claim_context is not None:
                            _claim_context.record_download_popup_claim(task.task_id, download_page)

                    page.on("popup", on_popup)

                    async def process_captured_download(results: list[ActionResult] | None) -> None:
                        await asyncio.sleep(0)
                        captured: tuple[Download, Page] | None = None
                        if false_click_download_event.done():
                            captured = false_click_download_event.result()
                        elif download_callbacks and settings.FILE_DOWNLOAD_FALSE_CLICK_POPUP_GRACE_SECONDS > 0:
                            # grace>0 only: wait briefly for a late download event. At grace=0 we never
                            # introduce a new wait -- cleanup acts only on an already-resolved capture.
                            try:
                                captured = await asyncio.wait_for(
                                    asyncio.shield(false_click_download_event),
                                    timeout=settings.FILE_DOWNLOAD_FALSE_CLICK_POPUP_GRACE_SECONDS,
                                )
                            except asyncio.TimeoutError:
                                pass
                        if captured is None:
                            return

                        false_click_download, download_popup = captured
                        try:
                            # Persistence and every side effect it needs -- run id, download dir, storage
                            # listing, directory creation, persist, finalize -- stay grace-gated. At
                            # grace=0 we skip all of it and only the popup cleanup/restore below runs.
                            if not persist_false_click_download:
                                return
                            context = skyvern_context.current()
                            run_id = resolve_run_download_id(context, task.workflow_run_id or task.task_id)
                            download_dir = Path(get_download_dir(run_id=run_id))

                            async def list_false_click_files(extra: Path | None = None) -> list[str]:
                                files = list_files_in_directory(download_dir)
                                if task.browser_session_id:
                                    files += await app.STORAGE.list_downloaded_files_in_browser_session(
                                        organization_id=task.organization_id,
                                        browser_session_id=task.browser_session_id,
                                    )
                                if extra and extra.is_file():
                                    files.append(str(extra))
                                return files

                            browser_artifacts = getattr(browser_state, "browser_artifacts", None)
                            remote_session_id = getattr(browser_artifacts, "remote_browser_session_id", None)
                            remote_session = isinstance(remote_session_id, str) and bool(remote_session_id)
                            eager_save = (
                                getattr(browser_state, "release_driver_on_close", False) is True
                                or remote_session
                                or getattr(browser_artifacts, "needs_cdp_frame_publisher", False) is True
                            )
                            if eager_save:
                                download_dir.mkdir(parents=True, exist_ok=True)
                            persisted = await _persist_captured_download(
                                false_click_download,
                                target=_download_target_path(download_dir, false_click_download.suggested_filename)
                                if eager_save
                                else None,
                                timeout=task.download_timeout or BROWSER_DOWNLOAD_MAX_WAIT_TIME,
                                owned_dir=download_dir,
                            )
                            if persisted.path is not None:
                                observed_after_persist = await list_false_click_files()
                                baseline = [path for path in observed_after_persist if path != str(persisted.path)]
                                names, _ = await _finalize_download_artifacts(
                                    download_dir=download_dir,
                                    task=task,
                                    list_files_before=baseline,
                                    list_observed_download_files=lambda: list_false_click_files(persisted.path),
                                )
                                try:
                                    persisted_artifact_qualified = (
                                        persisted.path.is_file() and persisted.path.stat().st_size > 0
                                    )
                                except OSError:
                                    persisted_artifact_qualified = False
                                result = results[-1] if results else None
                                if names and persisted_artifact_qualified and isinstance(result, ActionResult):
                                    result.downloaded_files = action.downloaded_files = names
                                    result.download_triggered = action.download_triggered = True
                        finally:
                            await _cleanup_captured_download_popup(
                                download_popup, browser_state, page, page_url_before_download
                            )

                    false_click_eligible_token = _false_click_download_eligible.set(True)
                    try:
                        with traced_span(_tracer, "skyvern.agent.action.handle_inner") as _hi_span:
                            apply_context_attrs(_hi_span)
                            try:
                                results = await ActionHandler._handle_action(
                                    scraped_page=scraped_page,
                                    task=task,
                                    step=step,
                                    page=page,
                                    action=action,
                                    allow_stale_refresh=allow_stale_refresh,
                                )
                            except asyncio.CancelledError:
                                raise
                            except BaseException:
                                try:
                                    await process_captured_download(None)
                                except asyncio.CancelledError:
                                    raise
                                except BaseException:
                                    LOG.warning(
                                        "Captured download processing failed after action exception",
                                        exc_info=True,
                                    )
                                raise
                        await process_captured_download(results)
                    finally:
                        _false_click_download_eligible.reset(false_click_eligible_token)
                        try:
                            _remove_popup_listener(page, on_popup)
                        except Exception:
                            LOG.warning("Failed to remove captured download popup listener", exc_info=True)
                        for observed_page, callback in download_callbacks:
                            try:
                                _remove_download_listener(observed_page, callback)
                            except Exception:
                                LOG.warning("Failed to remove captured download listener", exc_info=True)
                        if not false_click_download_event.done():
                            false_click_download_event.cancel()
            action.finished_at = naive_utc_now()
            persisted_action = await app.DATABASE.workflow_params.create_action(action=action)
            action.action_id = persisted_action.action_id
            return results

        context = skyvern_context.current()
        run_id = resolve_run_download_id(context, fallback_run_id=task.workflow_run_id or task.task_id)
        download_dir = Path(get_download_dir(run_id=run_id))
        download_event: asyncio.Future[Download] = asyncio.get_running_loop().create_future()
        eager_blob_capture = _EagerAdoptedBlobCapture(
            enabled=bool(task.browser_session_id),
            clicked_page=page,
            workflow_run_id=task.workflow_run_id,
        )
        download_popup_callbacks: list[tuple[Page, Callable[[Download], None]]] = []

        def _capture_download_event(download: Download) -> None:
            if not download_event.done():
                download_event.set_result(download)
            eager_blob_capture.maybe_start(download)

        def _register_download_popup(popup_page: Page) -> None:
            # A blob download frequently mints its document in a new tab, so the download event
            # fires on the popup, not the clicked page. Capture there too so the owner is read live.
            popup_page.on("download", _capture_download_event)
            download_popup_callbacks.append((popup_page, _capture_download_event))

        def _record_download_popup_claim(popup_page: Page) -> None:
            # Record popup identity only (no download-listener wiring, so managed-session download
            # behavior is unchanged). Ungated by browser_session_id: a dynamic/remote-CDP run mints the
            # download through the CDP monitor + file-scan task credit, which fires no Playwright popup
            # download event and lands after this seam returns, so the never-committed marker popup must
            # be recorded here for the task's credit seam to close it later.
            _claim_context = skyvern_context.current()
            if _claim_context is not None:
                _claim_context.record_download_popup_claim(task.task_id, popup_page)

        def _download_signal_identity(file: str) -> str:
            return file.removesuffix(BROWSER_DOWNLOADING_SUFFIX)

        async def _list_download_signal_files() -> list[str]:
            files = list_files_in_directory(download_dir)
            if task.browser_session_id:
                downloading_files_in_browser_session = await app.STORAGE.list_downloading_files_in_browser_session(
                    organization_id=task.organization_id, browser_session_id=task.browser_session_id
                )
                downloaded_files_in_browser_session = await app.STORAGE.list_downloaded_files_in_browser_session(
                    organization_id=task.organization_id, browser_session_id=task.browser_session_id
                )
                files = files + downloaded_files_in_browser_session + downloading_files_in_browser_session
            return files

        async def _list_final_download_files() -> list[str]:
            files = [
                file for file in list_files_in_directory(download_dir) if not file.endswith(BROWSER_DOWNLOADING_SUFFIX)
            ]
            if task.browser_session_id:
                files += await app.STORAGE.list_downloaded_files_in_browser_session(
                    organization_id=task.organization_id, browser_session_id=task.browser_session_id
                )
            return files

        # Provider-owned downloads (vendor remote sessions) are observed per action. The source is
        # deliberately private/non-serialized on BrowserArtifacts; absent sources preserve existing
        # PBS/CDP/local behavior unchanged.
        action_download_observation: ActionDownloadObservation | None = None
        provider_source = browser_state.browser_artifacts.get_action_download_source() if browser_state else None
        if provider_source is not None:
            baseline_budget_seconds = min(
                PROVIDER_DOWNLOAD_BASELINE_TIMEOUT_SECONDS,
                float(task.download_timeout) if task.download_timeout is not None else BROWSER_DOWNLOAD_TIMEOUT,
            )
            try:
                action_download_observation = await provider_source.begin_observation(
                    deadline=time.monotonic() + baseline_budget_seconds
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Same secret-leak guard as the poll catches: a provider list/schema error can embed the
                # secret-bearing presigned URL, so log only its type -- never the exception/traceback. A
                # missing baseline disables provider-diff for this action but leaves every download path intact.
                LOG.warning(
                    "Provider download baseline unavailable; continuing existing download paths",
                    error_type=type(exc).__name__,
                )

        async def _drain_and_move_staged_xhr(xhr_fallback_moved_paths: set[str], timeout_seconds: float) -> bool:
            await xhr_capture.drain(timeout_seconds=timeout_seconds)
            if not staging_dir.exists():
                return False
            staged_files = [f for f in staging_dir.iterdir() if f.is_file()]
            if not staged_files:
                return False
            moved_count = 0
            for sf in staged_files:
                target = download_dir / sf.name
                if not target.exists():
                    try:
                        shutil.move(sf, target)
                        xhr_fallback_moved_paths.add(str(target))
                        moved_count += 1
                    except OSError:
                        LOG.warning(
                            "Failed to move staged XHR file to download dir",
                            file=sf.name,
                            workflow_run_id=task.workflow_run_id,
                            exc_info=True,
                        )
            if moved_count > 0:
                LOG.info(
                    "XHR staging fallback: moved staged files to download dir",
                    staged_count=moved_count,
                    workflow_run_id=task.workflow_run_id,
                )
                return True
            return False

        initial_page_count = 0
        page_url_before_download = page.url
        # get the initial page count
        if browser_state:
            initial_page_count = len(await browser_state.list_valid_pages())

        signal_file_identities_before = {
            _download_signal_identity(file) for file in await _list_download_signal_files()
        }
        list_files_before = list(signal_file_identities_before)
        LOG.info(
            "Number of files in download directory before action",
            num_downloaded_files_before=len(list_files_before),
            download_dir=download_dir,
        )
        # Baseline of inline iframe srcs before the action, so a blocked-inline-PDF recovery can
        # admit only frames that appeared in this action's window (see _recover_blocked_inline_pdf_download).
        inline_iframe_srcs_before = await _collect_inline_iframe_src_candidates(page)

        # Run-scoped so teardown and the stale sweep reclaim it by run identity (SKY-14159).
        staging_dir = Path(
            tempfile.mkdtemp(prefix="xhr_staging_", dir=get_run_temp_dir(task.organization_id, run_id or task.task_id))
        )
        xhr_capture = ScopedXhrDownloadCapture(
            page,
            staging_dir,
            timeout_seconds=float(task.download_timeout)
            if task.download_timeout is not None
            else BROWSER_DOWNLOAD_TIMEOUT,
        )
        download_triggered = False
        working_page_recovery_attempted = False
        working_page_replaced_after_close = False
        xhr_fallback_moved_paths: set[str] = set()
        transient_text_observer = TransientPageTextObserver(
            page,
            task_id=task.task_id,
            step_id=step.step_id,
            workflow_run_id=task.workflow_run_id,
        )
        page.on("download", _capture_download_event)
        # Identity-only recorder for the task-scoped late cleanup, armed for every download click.
        page.on("popup", _record_download_popup_claim)
        # Popup-owned blob downloads only matter for adopted/persistent sessions; managed sessions
        # keep their existing single-page download behavior with no popup-download wiring.
        if task.browser_session_id:
            page.on("popup", _register_download_popup)
        requested_download_token = begin_requested_download_for_context(page.context)
        # Arm blob URL retention for every structurally download-capturing context: adopted/persistent
        # sessions and any context bound to a CDPDownloadInterceptor (which includes pooled sessions).
        # A page that mints a PDF blob and synchronously revokes it drops the object URL before the
        # interceptor's post-event in-page read; retention defers the revoke so the read can recover it.
        retention_armed = bool(task.browser_session_id) or has_download_interceptor_for_context(page.context)
        try:
            if retention_armed:
                # Install before the interaction so the createObjectURL/revokeObjectURL patch is in place
                # when the click/select mints the blob. Fail-open and time-bounded: retention is a
                # recovery aid, never a gate on the action itself.
                try:
                    async with asyncio.timeout(_BLOB_RETENTION_ARMING_TIMEOUT_SECONDS):
                        await install_blob_url_retention(page, workflow_run_id=task.workflow_run_id)
                except Exception:
                    LOG.debug(
                        "Failed to install blob URL retention before download action",
                        workflow_run_id=task.workflow_run_id,
                    )
            await transient_text_observer.start(scan_initial_visible_state=False)
            xhr_capture.enable()
            with traced_span(_tracer, "skyvern.agent.action.handle_inner") as _hi_span:
                apply_context_attrs(_hi_span)
                results = await ActionHandler._handle_action(
                    scraped_page=scraped_page,
                    task=task,
                    step=step,
                    page=page,
                    action=action,
                    allow_stale_refresh=allow_stale_refresh,
                )
            # The execution window ends when the inner action completes: the download wait
            # below (up to BROWSER_DOWNLOAD_TIMEOUT) is settle observation, excluded to match
            # the cached-script writer's semantics.
            action.finished_at = naive_utc_now()
            if not results:
                return results
            if isinstance(results[-1], ActionAbort) and results[-1].skip_remaining_actions:
                return results
            # Let request events already queued by the action enter before closing admission.
            await asyncio.sleep(0)
            xhr_capture.seal_in_flight_requests()
            if browser_state is not None and page.is_closed():
                working_page_recovery_attempted = True
                xhr_capture.disable()
                recovery_timeout_seconds = (
                    float(task.download_timeout) if task.download_timeout is not None else BROWSER_DOWNLOAD_TIMEOUT
                )
                recovered_page = await _recover_download_page(
                    browser_state,
                    task,
                    page_url_before_download,
                    recovery_timeout_seconds,
                    recovery_site="post_click",
                )
                if recovered_page is not None:
                    working_page_replaced_after_close = True
                    try:
                        _remove_download_listener(page, _capture_download_event)
                    except Exception:
                        LOG.warning("Failed to remove download listener from closed page", exc_info=True)
                    page = recovered_page
                    page.on("download", _capture_download_event)
                    recovered_text_observer = TransientPageTextObserver(
                        page,
                        task_id=task.task_id,
                        step_id=step.step_id,
                        workflow_run_id=task.workflow_run_id,
                    )
                    recovered_text_observer.events.extend(transient_text_observer.events)
                    await transient_text_observer.stop()
                    transient_text_observer = recovered_text_observer
            # Deliberately reinstall and rescan in case the action replaced the document or exposed initial
            # visible text.
            await transient_text_observer.start(scan_initial_visible_state=True)
            if task.download_timeout is not None:
                download_wait_hard_timeout_seconds = float(task.download_timeout)
                no_signal_grace_seconds = min(download_wait_hard_timeout_seconds, BROWSER_DOWNLOAD_NO_SIGNAL_GRACE_TIME)
            else:
                no_signal_grace_seconds = BROWSER_DOWNLOAD_NO_SIGNAL_GRACE_TIME
                download_wait_hard_timeout_seconds = no_signal_grace_seconds + DOWNLOAD_IN_FLIGHT_EXTENSION_MAX_SECONDS
            download_wait_started_at = time.monotonic()
            download_wait_deadline = download_wait_started_at + download_wait_hard_timeout_seconds

            def _remaining_download_wait_seconds() -> float:
                return max(0.0, download_wait_deadline - time.monotonic())

            _download_completion_timeout = task.download_timeout or BROWSER_DOWNLOAD_TIMEOUT
            _download_event_grace_seconds = min(
                DOWNLOAD_EVENT_ACTIVE_DIR_GRACE_SECONDS, download_wait_hard_timeout_seconds
            )
            with traced_span(_tracer, "skyvern.agent.action.download_wait") as _dl_wait_span:
                apply_context_attrs(_dl_wait_span)
                _dl_wait_span.set_attribute("timeout_seconds", download_wait_hard_timeout_seconds)
                _dl_wait_span.set_attribute("download_event_grace_seconds", _download_event_grace_seconds)
                _dl_wait_span.set_attribute("in_flight_extension_max_seconds", DOWNLOAD_IN_FLIGHT_EXTENSION_MAX_SECONDS)
                _dl_wait_span.set_attribute("no_signal_grace_seconds", no_signal_grace_seconds)
                _poll_iterations = 0
                captured_download: Download | None = None
                download_event_captured = False
                download_event_captured_at: float | None = None
                download_event_fallback_attempted = False
                download_event_fallback_used = False
                download_event_fallback_failed = False
                download_signal_observed = False
                download_signal_source: str | None = None
                download_signal_elapsed_seconds: float | None = None
                download_signal_poll_iterations: int | None = None
                download_wait_matched_errors: list[UserDefinedError] = []
                download_wait_extended_for_in_flight_request = False

                def _record_download_signal(source: str) -> None:
                    nonlocal download_signal_observed
                    nonlocal download_signal_source
                    nonlocal download_signal_elapsed_seconds
                    nonlocal download_signal_poll_iterations

                    if download_signal_elapsed_seconds is not None:
                        return
                    download_signal_observed = True
                    download_signal_source = source
                    download_signal_elapsed_seconds = time.monotonic() - download_wait_started_at
                    download_signal_poll_iterations = _poll_iterations

                try:
                    LOG.info(
                        "Checking if there is any new files after click",
                        download_dir=download_dir,
                    )
                    async with asyncio.timeout(download_wait_hard_timeout_seconds):
                        while True:
                            _poll_iterations += 1
                            if download_event.done() and captured_download is None:
                                captured_download = download_event.result()
                                download_event_captured = True
                                download_event_captured_at = time.monotonic()
                                _record_download_signal("browser_download_event")
                                LOG.info(
                                    "Captured download event; waiting for active run directory file",
                                    download_dir=download_dir,
                                    workflow_run_id=task.workflow_run_id,
                                    download_signal_elapsed_seconds=download_signal_elapsed_seconds,
                                    download_signal_poll_iterations=download_signal_poll_iterations,
                                )

                            if (
                                task.browser_session_id
                                and captured_download is not None
                                and not download_event_fallback_attempted
                            ):
                                resolved_download_binding = (
                                    browser_state.browser_artifacts.download_binding
                                    if browser_state is not None
                                    else DownloadBinding.RUN_DIR
                                )
                                download_event_fallback_attempted = True
                                eager_blob_bytes = await eager_blob_capture.result(
                                    timeout=min(
                                        EAGER_BLOB_READ_TIMEOUT_SECONDS,
                                        _remaining_download_wait_seconds(),
                                    )
                                )
                                async with _adopted_session_download_binding(
                                    captured_download,
                                    page,
                                    download_binding=resolved_download_binding,
                                ) as (
                                    download_interceptor,
                                    authorize_request_hop,
                                    download_scope,
                                ):
                                    cookie_header = (
                                        await download_interceptor._cookie_header_for_url(captured_download.url)
                                        if download_interceptor is not None
                                        else None
                                    )
                                    request_headers = {"Cookie": cookie_header} if cookie_header else {}
                                    saved_path = await _save_adopted_session_download(
                                        captured_download,
                                        page,
                                        download_dir,
                                        authorize_request_hop=authorize_request_hop,
                                        request_headers=request_headers,
                                        download_scope=download_scope,
                                        workflow_run_id=task.workflow_run_id,
                                        eager_blob_bytes=eager_blob_bytes,
                                        download_binding=resolved_download_binding,
                                    )
                                if saved_path is not None:
                                    download_event_fallback_used = True
                                    download_triggered = True
                                    LOG.info(
                                        "Saved adopted-session download to active run directory",
                                        download_dir=download_dir,
                                        download_target=str(saved_path),
                                        workflow_run_id=task.workflow_run_id,
                                    )
                                    break
                                if (
                                    resolved_download_binding == DownloadBinding.SESSION_DIR
                                    and not captured_download.url.startswith("blob:")
                                ):
                                    # Expected signal-only deferral for a provider-owned remote binding: no
                                    # save_as/replay was attempted, so this is not a failure. Keep polling.
                                    LOG.info(
                                        "Provider-owned remote download deferred to provider-destination observation",
                                        download_dir=download_dir,
                                        workflow_run_id=task.workflow_run_id,
                                    )
                                else:
                                    download_event_fallback_failed = True
                                    LOG.warning(
                                        "Adopted-session download could not be saved or re-fetched; falling through to browser-session folder poll",
                                        download_dir=download_dir,
                                        workflow_run_id=task.workflow_run_id,
                                    )
                                # Keep polling: the shared browser may still land the file in the session folder.
                                if await _drain_and_move_staged_xhr(
                                    xhr_fallback_moved_paths, _remaining_download_wait_seconds()
                                ):
                                    download_triggered = True
                                    break

                            list_files_after = await _list_download_signal_files()
                            local_signal_delta = {
                                _download_signal_identity(file) for file in list_files_after
                            } - signal_file_identities_before

                            # Only reach for the provider when no local artifact already accounts for this
                            # action. An existing CDP/local/session file is authoritative, so polling the
                            # provider would be pure duplication -- it could stall to the shared deadline or
                            # materialize a collision-suffixed copy of a file already saved.
                            if not local_signal_delta and action_download_observation is not None:
                                try:
                                    await action_download_observation.poll_and_materialize(
                                        destination_dir=download_dir,
                                        deadline=download_wait_deadline,
                                    )
                                except asyncio.CancelledError:
                                    raise
                                except Exception as exc:
                                    # Provider-list schema/validation errors can embed the secret-bearing
                                    # presigned URL; log only its type, never the exception/traceback.
                                    LOG.debug(
                                        "Provider download poll failed; continuing existing paths",
                                        error_type=type(exc).__name__,
                                    )
                                list_files_after = await _list_download_signal_files()
                                local_signal_delta = {
                                    _download_signal_identity(file) for file in list_files_after
                                } - signal_file_identities_before

                            if local_signal_delta:
                                _record_download_signal("download_file_detected")
                                LOG.info(
                                    "Found new files in download directory after action",
                                    num_downloaded_files_after=len(list_files_after),
                                    download_dir=download_dir,
                                    workflow_run_id=task.workflow_run_id,
                                    download_signal_elapsed_seconds=download_signal_elapsed_seconds,
                                    download_signal_poll_iterations=download_signal_poll_iterations,
                                )
                                download_triggered = True
                                break

                            if (
                                not task.browser_session_id
                                and captured_download is not None
                                and download_event_captured_at is not None
                                and not download_event_fallback_attempted
                                and time.monotonic() - download_event_captured_at >= _download_event_grace_seconds
                            ):
                                download_event_fallback_attempted = True
                                persisted = await _persist_captured_download(
                                    captured_download,
                                    target=_download_target_path(download_dir, captured_download.suggested_filename),
                                    timeout=task.download_timeout or BROWSER_DOWNLOAD_MAX_WAIT_TIME,
                                )
                                if persisted.outcome == "empty":
                                    LOG.warning(
                                        "Captured download event fallback produced an empty file; marking download triggered without artifact",
                                        download_dir=download_dir,
                                        workflow_run_id=task.workflow_run_id,
                                    )
                                    list_files_after = await _list_final_download_files()
                                    download_triggered = True
                                    break
                                if persisted.path is not None:
                                    list_files_after = await _list_final_download_files()
                                    LOG.info(
                                        "Copied captured download event to active run directory",
                                        download_dir=download_dir,
                                        download_target=str(persisted.path),
                                        workflow_run_id=task.workflow_run_id,
                                    )
                                    download_triggered = True
                                    download_event_fallback_used = True
                                    break
                                LOG.warning(
                                    "Failed to copy captured download event to active run directory",
                                    download_dir=download_dir,
                                    workflow_run_id=task.workflow_run_id,
                                    outcome=persisted.outcome,
                                )
                                download_event_fallback_failed = True
                                break
                            elapsed_since_action = time.monotonic() - download_wait_started_at
                            if not download_signal_observed:
                                download_wait_matched_errors = match_user_defined_errors_from_transient_text(
                                    task,
                                    step,
                                    transient_text_observer.events,
                                )
                                if download_wait_matched_errors:
                                    action.errors = (action.errors or []) + download_wait_matched_errors
                                    action.terminal_user_errors = True
                                    LOG.warning(
                                        "Stopping download wait after transient user-defined error text",
                                        task_id=task.task_id,
                                        step_id=step.step_id,
                                        workflow_run_id=task.workflow_run_id,
                                        error_codes=[error.error_code for error in download_wait_matched_errors],
                                    )
                                    break

                            if elapsed_since_action >= download_wait_hard_timeout_seconds:
                                raise asyncio.TimeoutError

                            if not download_signal_observed and elapsed_since_action >= no_signal_grace_seconds:
                                if xhr_capture.has_in_flight_requests:
                                    download_wait_extended_for_in_flight_request = True
                                else:
                                    LOG.warning(
                                        "No download signal observed after action",
                                        workflow_run_id=task.workflow_run_id,
                                        no_signal_grace_seconds=no_signal_grace_seconds,
                                    )
                                    break
                            sleep_seconds = DOWNLOAD_IN_FLIGHT_POLL_INTERVAL_SECONDS
                            if not download_signal_observed and elapsed_since_action < no_signal_grace_seconds:
                                sleep_seconds = min(1, max(0.0, no_signal_grace_seconds - elapsed_since_action))
                            await asyncio.sleep(sleep_seconds)

                except asyncio.TimeoutError:
                    LOG.warning(
                        "No file to download after action",
                        workflow_run_id=task.workflow_run_id,
                    )
                finally:
                    _dl_wait_span.set_attribute("download_signal_observed", download_signal_observed)
                    if download_signal_source:
                        _dl_wait_span.set_attribute("download_signal_source", download_signal_source)
                    if download_signal_elapsed_seconds is not None:
                        _dl_wait_span.set_attribute("download_signal_elapsed_seconds", download_signal_elapsed_seconds)
                    if download_signal_poll_iterations is not None:
                        _dl_wait_span.set_attribute("download_signal_poll_iterations", download_signal_poll_iterations)
                    _dl_wait_span.set_attribute("download_triggered", download_triggered)
                    _dl_wait_span.set_attribute("poll_iterations", _poll_iterations)
                    _dl_wait_span.set_attribute("download_event_captured", download_event_captured)
                    _dl_wait_span.set_attribute("download_event_fallback_attempted", download_event_fallback_attempted)
                    _dl_wait_span.set_attribute("download_event_fallback_used", download_event_fallback_used)
                    _dl_wait_span.set_attribute("download_event_fallback_failed", download_event_fallback_failed)
                    _dl_wait_span.set_attribute(
                        "download_wait_observed_text_count",
                        len(transient_text_observer.events),
                    )
                    _dl_wait_span.set_attribute(
                        "download_wait_user_error_detected",
                        bool(download_wait_matched_errors),
                    )
                    _dl_wait_span.set_attribute(
                        "download_wait_extended_for_in_flight_request",
                        download_wait_extended_for_in_flight_request,
                    )
                    LOG.info(
                        "Transient download observation completed",
                        workflow_run_id=task.workflow_run_id,
                        observer_event_count=len(transient_text_observer.events),
                        user_error_matched=bool(download_wait_matched_errors),
                        extended_for_in_flight_request=download_wait_extended_for_in_flight_request,
                        elapsed_seconds=time.monotonic() - download_wait_started_at,
                    )
                    if download_wait_matched_errors:
                        _dl_wait_span.set_attribute(
                            "download_wait_user_error_codes",
                            ",".join(error.error_code for error in download_wait_matched_errors),
                        )

            if not download_triggered:
                if download_wait_matched_errors:
                    await _drain_and_move_staged_xhr(xhr_fallback_moved_paths, 0)
                elif await _drain_and_move_staged_xhr(xhr_fallback_moved_paths, _remaining_download_wait_seconds()):
                    download_triggered = True

            if browser_state is not None and not working_page_recovery_attempted and page.is_closed():
                working_page_recovery_attempted = True
                recovered_page = await _recover_download_page(
                    browser_state,
                    task,
                    page_url_before_download,
                    float(task.download_timeout) if task.download_timeout is not None else BROWSER_DOWNLOAD_TIMEOUT,
                    recovery_site="post_wait",
                )
                if recovered_page is not None:
                    working_page_replaced_after_close = True
                    page = recovered_page

            # A download-intent click can render the target PDF inline in a frame the browser refuses
            # to display, so no download event ever fires. If the bytes are still same-origin
            # retrievable, recover them and rejoin the normal finalize path. Skipped when the workflow
            # matched a user-defined terminal error, so configured stop conditions stay authoritative.
            # Also skipped after page replacement because the iframe baseline belongs to the destroyed
            # document; candidates in the reloaded document are not attributable to this action.
            if not download_triggered and not download_wait_matched_errors and not working_page_replaced_after_close:
                recovered_path = None
                try:
                    # One whole-operation budget over every candidate fetch: a hung same-origin
                    # server must not out-wait the download loop this backstops. On timeout, fall
                    # through to the normal download-not-triggered follow-up — never hang or hard-fail.
                    async with asyncio.timeout(_BLOCKED_INLINE_PDF_RECOVERY_TIMEOUT_SECONDS):
                        recovered_path = await _recover_blocked_inline_pdf_download(
                            page,
                            download_dir,
                            workflow_run_id=task.workflow_run_id,
                            iframe_srcs_before=inline_iframe_srcs_before,
                        )
                except asyncio.TimeoutError:
                    LOG.warning(
                        "Blocked-inline PDF recovery exceeded its budget; treating as no recovery",
                        workflow_run_id=task.workflow_run_id,
                        recovery_budget_seconds=_BLOCKED_INLINE_PDF_RECOVERY_TIMEOUT_SECONDS,
                    )
                if recovered_path is not None:
                    download_triggered = True

            if not download_triggered:
                if action.errors:
                    results[-1] = ActionFailure(
                        Exception("; ".join(error.reasoning for error in action.errors)),
                        download_triggered=False,
                    )
                else:
                    results[-1].download_triggered = False
                    if isinstance(results[-1], ActionSuccess):
                        results[-1].needs_followup = True
                        results[-1].followup_message = DOWNLOAD_NOT_TRIGGERED_FOLLOWUP_MESSAGE
                if working_page_replaced_after_close:
                    results[-1].skip_remaining_actions = True
                action.download_triggered = False
                # A download-intent click strands the tab on a blank document whether or not a file
                # arrives. The triggered path restores it below; without this the untriggered path
                # returns first and every later block scrapes a blank page. Restoring replaces the
                # document, so the rest of this batch was planned against elements that no longer
                # exist — stop it and let the next step rescrape.
                if browser_state is not None and not page.is_closed():
                    if await _restore_page_url_after_download(browser_state, page, page_url_before_download):
                        results[-1].skip_remaining_actions = True
                return results
            results[-1].download_triggered = True
            action.download_triggered = True

            async with asyncio.timeout(_download_completion_timeout):
                async with settle_browser_downloads_for_context(page.context):
                    if action_download_observation is not None:
                        # Mirror the mid-wait rule at finalize: poll the provider only when no local
                        # artifact already accounts for this action. A file the CDP/local/session path
                        # already saved must not be duplicated by a second, collision-suffixed provider
                        # copy here. The listing is done only when a provider is attached, so the
                        # legacy no-provider path issues no extra directory read.
                        local_signal_accounts_for_action = bool(
                            {_download_signal_identity(file) for file in await _list_download_signal_files()}
                            - signal_file_identities_before
                        )
                        if not local_signal_accounts_for_action:
                            try:
                                await action_download_observation.poll_and_materialize(
                                    destination_dir=download_dir,
                                    deadline=download_wait_deadline,
                                )
                            except asyncio.CancelledError:
                                raise
                            except Exception as exc:
                                # Same secret-leak guard as the mid-poll catch: metadata only, no traceback.
                                LOG.debug("Final provider download poll failed", error_type=type(exc).__name__)
                    downloaded_file_names, new_file_paths = await _finalize_download_artifacts(
                        download_dir=download_dir,
                        task=task,
                        list_files_before=list_files_before,
                        list_observed_download_files=_list_final_download_files,
                    )
            if downloaded_file_names:
                results[-1].downloaded_files = action.downloaded_files = downloaded_file_names
            elif (
                captured_download is not None
                and (aborted_reason := await read_download_failure(captured_download)) is not None
            ):
                # The partial file appearing is what credited download_triggered, and the browser
                # deletes it on abort, so the settle above reads an aborted transfer as a completed
                # one. Without this the action reports success with no file and the agent retries
                # the already-consumed link instead of regenerating it.
                LOG.warning(
                    "Browser aborted the download after it was credited; no file was saved",
                    workflow_run_id=task.workflow_run_id,
                    download_dir=download_dir,
                    failure=aborted_reason,
                )
                results[-1] = ActionFailure(
                    Exception(f"{DOWNLOAD_ABORTED_FAILURE_MESSAGE} (browser reported: {aborted_reason})"),
                    download_triggered=True,
                )
            elif isinstance(results[-1], ActionSuccess):
                # A download was observed/credited but finalization produced no artifact and the
                # browser reported no abort reason. Returning a plain success implies a file exists;
                # flag needs_followup so the agent keeps trying rather than treating a missing file as
                # a completed download.
                results[-1].needs_followup = True
                results[-1].followup_message = DOWNLOAD_OBSERVED_BUT_EMPTY_FOLLOWUP_MESSAGE
            if xhr_fallback_moved_paths:
                post_settle_extra_paths = new_file_paths - xhr_fallback_moved_paths
                if post_settle_extra_paths:
                    LOG.warning(
                        "XHR staging fallback used but additional download files appeared after settle",
                        workflow_run_id=task.workflow_run_id,
                        download_dir=download_dir,
                        xhr_fallback_file_count=len(xhr_fallback_moved_paths),
                        xhr_fallback_files=sorted(os.path.basename(fp) for fp in xhr_fallback_moved_paths),
                        post_settle_extra_file_count=len(post_settle_extra_paths),
                        post_settle_extra_files=sorted(os.path.basename(fp) for fp in post_settle_extra_paths),
                    )
            if working_page_replaced_after_close:
                results[-1].skip_remaining_actions = True
            return results
        finally:
            # Fallback for exceptional exits that never reached the post-action stamp.
            if action.finished_at is None:
                action.finished_at = naive_utc_now()
            await _close_eager_capture_then_teardown_retention(
                eager_blob_capture,
                page,
                retention_armed=retention_armed,
                workflow_run_id=task.workflow_run_id,
            )
            for observed_popup, popup_callback in download_popup_callbacks:
                try:
                    _remove_download_listener(observed_popup, popup_callback)
                except Exception:
                    LOG.warning("Failed to remove download popup listener", exc_info=True)
            try:
                _remove_popup_listener(page, _record_download_popup_claim)
            except Exception:
                with contained_effect("remove download popup claim recorder"):
                    LOG.warning("Failed to remove download popup claim recorder", exc_info=True)
            if task.browser_session_id:
                try:
                    _remove_popup_listener(page, _register_download_popup)
                except Exception:
                    LOG.warning("Failed to remove download popup registrar", exc_info=True)
            try:
                await transient_text_observer.stop()
            finally:
                xhr_capture.disable()
                try:
                    await xhr_capture.drain(timeout_seconds=0)
                finally:
                    if staging_dir.exists():
                        shutil.rmtree(staging_dir, ignore_errors=True)
            if browser_state is not None and download_triggered:
                # get the page count after download
                pages_after_download = await browser_state.list_valid_pages()
                page_count_after_download = len(pages_after_download)
                LOG.info(
                    "Page count after download file action",
                    initial_page_count=initial_page_count,
                    page_count_after_download=page_count_after_download,
                )
                extra_page_count = page_count_after_download - initial_page_count
                if extra_page_count > 0:
                    LOG.info(
                        "Download triggered, closing extra pages",
                        extra_page_count=extra_page_count,
                    )

                    for extra_page in reversed(pages_after_download):
                        if extra_page_count <= 0:
                            break
                        if extra_page == page:
                            continue
                        await extra_page.close()
                        extra_page_count -= 1

                if await _restore_page_url_after_download(browser_state, page, page_url_before_download):
                    # Safe to touch results here: download_triggered only ever becomes True after
                    # results is bound, and mutating a member of the already-returned list still
                    # reaches the caller.
                    results[-1].skip_remaining_actions = True

            try:
                _remove_download_listener(page, _capture_download_event)
            except Exception:
                LOG.warning("Failed to remove one-shot download event listener", exc_info=True)

            download_error = finish_requested_download_for_context(page.context, requested_download_token)
            if download_error is not None and "results" in locals() and results:
                results[-1] = ActionFailure(
                    Exception(download_error["reasoning"]),
                    download_triggered=download_triggered,
                )
            persisted_action = await app.DATABASE.workflow_params.create_action(action=action)
            action.action_id = persisted_action.action_id

    @staticmethod
    async def _handle_action(
        scraped_page: ScrapedPage,
        task: Task,
        step: Step,
        page: Page,
        action: Action,
        allow_stale_refresh: bool = False,
    ) -> list[ActionResult]:
        action.tel_input_outcome = None
        await app.AGENT_FUNCTION.wait_for_challenge_solver(page=page)
        LOG.info(
            "Handling action",
            sampling=True,
            action_type=action.action_type,
            action_id=action.action_id,
            status=action.status,
            step_order=action.step_order,
            action_order=action.action_order,
            element_id=action.element_id,
            errors=action.errors,
        )
        actions_result: list[ActionResult] = []
        llm_caller = LLMCallerManager.get_llm_caller(task.task_id)
        execution_timeout_seconds = _resolve_action_execution_timeout(action)
        execution_timeout_scope: asyncio.Timeout | None = None
        try:
            async with asyncio.timeout(execution_timeout_seconds) as execution_timeout_scope:
                if action.action_type in ActionHandler._handled_action_types:
                    if isinstance(action, PasteTextAction) and not await _is_paste_text_action_enabled(task):
                        actions_result.append(ActionFailure(Exception("PASTE_TEXT action is disabled")))
                        return actions_result

                    invalid_web_action_check = check_for_invalid_web_action(action, page, scraped_page, task, step)
                    if invalid_web_action_check:
                        actions_result.extend(invalid_web_action_check)
                        return actions_result

                    # A preceding action in this batch may have remounted/reflowed this action's
                    # target, leaving the pre-batch reference stale. Opportunistically remap it here,
                    # before the handler runs, so any remap is free of a half-applied side effect. Only
                    # enabled for non-first batch actions (see the step owner seam). When a remap cannot
                    # be established this is a no-op and the original binding falls through to the
                    # existing handler and owner-loop control flow unchanged.
                    if allow_stale_refresh:
                        refreshed = await _refresh_stale_web_action_before_dispatch(scraped_page, page, action)
                        if refreshed is not None:
                            scraped_page, action = refreshed
                            LOG.info(
                                "Re-resolved a stale web action to a remounted control before dispatch",
                                action_type=action.action_type,
                                fresh_element_id=action.element_id,
                            )
                        elif await _batched_target_stale_beyond_remap(scraped_page, page, action):
                            # The target was remounted by a preceding action in this same batch and could
                            # not be safely remapped (anchorless / ambiguous / volatile identity).
                            # Dispatching the stale pre-batch binding would act on a positional look-alike
                            # or a dead stub, and a later Save in the same batch would then serialize a
                            # form this batch never fully applied. Stop the batch instead so the next step
                            # re-plans and re-dispatches the remaining actions against a fresh scrape.
                            LOG.info(
                                "Stale batched action could not be safely remapped; stopping the batch to re-plan",
                                action_type=action.action_type,
                            )
                            stop_result = StaleActionAbort()
                            stop_result.skip_remaining_actions = True
                            actions_result.append(stop_result)
                            return actions_result

                    # do setup before action handler
                    if setup := ActionHandler._setup_action_types.get(action.action_type):
                        results = await setup(action, page, scraped_page, task, step)
                        actions_result.extend(results)
                        if results and results[-1] != ActionSuccess:
                            return actions_result

                    # do the handler
                    handler = ActionHandler._handled_action_types[action.action_type]
                    results = await handler(action, page, scraped_page, task, step)
                    actions_result.extend(results)
                    await app.AGENT_FUNCTION.wait_for_challenge_solver(page=page)
                    # do the teardown
                    teardown = ActionHandler._teardown_action_types.get(action.action_type)
                    if teardown:
                        results = await teardown(action, page, scraped_page, task, step)
                        actions_result.extend(results)

                    return actions_result

                else:
                    LOG.error(
                        "Unsupported action type in handler",
                        action=action,
                        type=type(action),
                    )
                    actions_result.append(ActionFailure(Exception(f"Unsupported action type: {type(action)}")))
                    return actions_result
        except MissingElement as e:
            LOG.info(
                "Known exceptions",
                action=action,
                exception_type=type(e),
                exception_message=str(e),
            )
            actions_result.append(ActionFailure(e))
        except MultipleElementsFound as e:
            LOG.exception(
                "Cannot handle multiple elements with the same selector in one action.",
                action=action,
            )
            actions_result.append(ActionFailure(e))
        except LLMProviderError as e:
            LOG.exception("LLM error in action handler", action=action, exc_info=True)
            actions_result.append(ActionFailure(e))
        except ImaginarySecretValue as e:
            # The model referenced a secret placeholder that is not in the run's secrets. Handled:
            # it becomes an ActionFailure below and reaches the run that way. Warning rather than
            # info because it is secret-adjacent.
            LOG.warning("Imaginary secret value", action=action, exc_info=True)
            actions_result.append(ActionFailure(e))
        except CaptchaSolveError as e:
            LOG.warning(
                "Captcha solve failed",
                action=action,
                exception_type=type(e).__name__,
                exception_message=str(e),
            )
            actions_result.append(ActionFailure(e))
        except asyncio.TimeoutError as e:
            if execution_timeout_scope is not None and execution_timeout_scope.expired():
                LOG.error(
                    "Action execution exceeded the max duration and was aborted",
                    action=action,
                    timeout_seconds=execution_timeout_seconds,
                )
                actions_result.append(
                    ActionFailure(ActionExecutionTimeout(action.action_type, execution_timeout_seconds))
                )
            else:
                LOG.exception("Unhandled exception in action handler", action=action)
                actions_result.append(ActionFailure(e))
        except ScreenshotTargetClosed as e:
            LOG.warning(
                "Browser target closed while handling action",
                action=action,
                exception_message=str(e),
            )
            actions_result.append(ActionFailure(e))
        except Exception as e:
            LOG.exception("Unhandled exception in action handler", action=action)
            actions_result.append(ActionFailure(e))
        finally:
            tool_result_content = ""

            if actions_result and isinstance(actions_result[-1], ActionSuccess):
                action.status = ActionStatus.completed
                tool_result_content = "Tool executed successfully"
            elif actions_result and isinstance(actions_result[-1], ActionAbort):
                action.status = ActionStatus.skipped
                if isinstance(actions_result[-1], StaleActionAbort):
                    # The action did NOT run (its target went stale). Tell the tool caller the truth so
                    # the next planning turn re-observes, rather than reporting a false success.
                    tool_result_content = STALE_TARGET_TOOL_RESULT
                else:
                    tool_result_content = "Tool executed successfully"
            else:
                tool_result_content = "Tool execution failed"
                # either actions_result is empty or the last action is a failure
                if not actions_result:
                    LOG.warning("Action failed to execute, setting status to failed", action=action)
                action.status = ActionStatus.failed

            _emit_tel_input_outcome(action, actions_result)

            if llm_caller and action.tool_call_id:
                tool_call_result = {
                    "type": "tool_result",
                    "tool_use_id": action.tool_call_id,
                    "content": tool_result_content,
                }
                llm_caller.add_tool_result(tool_call_result)

        return actions_result


def _resolve_action_execution_timeout(action: actions.Action) -> float:
    base = float(settings.BROWSER_ACTION_MAX_EXECUTION_SECONDS)
    # WaitAction sleeps action.seconds by design; give it that budget on top of the cap.
    if isinstance(action, actions.WaitAction):
        return base + action.seconds
    return base


async def _is_paste_text_action_enabled(task: Task) -> bool:
    if settings.PLANNER_MINI_GOAL_IMPROVEMENTS:
        return True
    try:
        return await app.EXPERIMENTATION_PROVIDER.is_feature_enabled_cached(
            "PLANNER_MINI_GOAL_IMPROVEMENTS",
            task.organization_id,
            properties={"organization_id": task.organization_id},
        )
    except Exception:
        LOG.warning(
            "Failed to resolve PASTE_TEXT execution gate; refusing execution",
            organization_id=task.organization_id,
            exc_info=True,
        )
        return False


def check_for_invalid_web_action(
    action: actions.Action,
    page: Page,
    scraped_page: ScrapedPage,
    task: Task,
    step: Step,
) -> list[ActionResult]:
    if isinstance(action, ClickAction) and action.x is not None and action.y is not None:
        return []

    if isinstance(action, (InputTextAction, PasteTextAction)) and not action.element_id:
        return []

    if isinstance(action, WebAction) and action.element_id not in scraped_page.id_to_element_dict:
        return [ActionFailure(MissingElement(element_id=action.element_id), stop_execution_on_failure=False)]

    return []


def _is_identity_anchor_key(key: str) -> bool:
    # The HTML ``id`` is the only per-instance identity we trust: it is document-unique by spec, so a
    # structural match on it is the same instance. Everything else is excluded -- ``name`` is NOT
    # reliably per-instance (repeated rows / wizard states expose one same-name control per snapshot),
    # and generic role/state attributes (any ``aria-*`` such as aria-label / aria-expanded, any
    # ``data-*`` such as data-state or a generic data-testid, ``class``, ``role``) can be unique in one
    # snapshot yet name a DIFFERENT repeated-component instance after a transition. (The incident's
    # select controls carry a stable inner form ``id``, so ticket recovery still remaps; a same-name or
    # generic-anchor decoy correctly declines to the legacy path.)
    return key == "id"


def _has_identity_anchor(element: dict) -> bool:
    """True when the element (or a descendant) carries a per-instance identity attribute, so a
    structural match reflects a real, stable instance identity rather than a generic role/state
    attribute shared across repeated component instances. Without such an anchor we decline to remap
    (and let the legacy path stay authoritative) rather than risk binding a different instance."""
    if not isinstance(element, dict):
        return False
    for key, value in (element.get("attributes") or {}).items():
        if key == SKYVERN_ID_ATTR or not isinstance(value, str) or not value.strip():
            continue
        if _is_identity_anchor_key(key):
            return True
    return any(_has_identity_anchor(child) for child in (element.get("children") or []) if isinstance(child, dict))


async def _batched_target_stale_beyond_remap(
    scraped_page: ScrapedPage,
    page: Page,
    action: Action,
) -> bool:
    """Fail-closed probe: ``True`` only when a non-first batched WebAction's target was remounted away by
    a preceding action in the SAME batch (its injected ``unique_id`` marker is gone) on the SAME, intact
    document, so dispatching its pre-batch binding would act on a positional look-alike / dead stub
    rather than the live control. It is consulted only after ``_refresh_stale_web_action_before_dispatch``
    has already declined to remap, and it never re-scrapes or mutates anything. It returns ``False`` for
    anything it cannot positively confirm -- a coordinate click, a non-main-frame or missing target, a
    navigated / wholly-replaced document, a still-live node, or an indeterminate probe -- so the legacy
    dispatch path stays authoritative in every ambiguous case.

    A separate probe (rather than a richer return from the remap) is deliberate: the remap declines
    anchorless / ambiguous targets BEFORE it probes liveness, yet those are exactly the targets this must
    catch. The precondition set (coordinate / URL-continuity / main-frame / liveness / marker-survival) is
    intentionally identical to the remap's; keep the two in sync -- widening one without the other would
    desync "can we remap?" from "must we stop the batch?".
    """
    if not isinstance(action, WebAction) or not action.element_id:
        return False
    if isinstance(action, ClickAction) and action.x is not None and action.y is not None:
        return False
    if await _document_continuity(scraped_page, page) is not True:
        return False
    css = scraped_page.id_to_css_dict.get(action.element_id)
    frame = scraped_page.id_to_frame_dict.get(action.element_id)
    if not css or frame != "main.frame":
        return False  # only the main frame has a scrape-stable identity to reason about
    try:
        locator, frame_content = await resolve_locator(scraped_page, page, frame, css)
        if await locator.count() == 1:
            return False  # the exact injected node is still live -> not stale
    except Exception:
        return False  # cannot confirm staleness -> decline; legacy dispatch stays authoritative
    return True


async def _refresh_stale_web_action_before_dispatch(
    scraped_page: ScrapedPage,
    page: Page,
    action: Action,
) -> tuple[ScrapedPage, Action] | None:
    """Opportunistic, side-effect-free remap for a WebAction whose element -- present in this batch's
    scrape -- may have been remounted/reflowed by a preceding action in the same batch (a fresh DOM
    node without the injected ``unique_id`` and a shifted tag-name xpath). It runs BEFORE the handler.

    Returns a ``(scraped_page, action)`` pair ONLY when the exact scraped node is no longer live (by
    the injected ``unique_id`` marker, never the positional xpath fallback) AND the same control can be
    re-resolved by a position-independent structural identity that is anchored by a real identity
    attribute and unique -- WITHIN the target's own frame -- both before and after one bounded refresh,
    the target is in the main frame (the only frame identity that is stable across scrapes), the
    document did not change (URL continuity), and at least one injected marker survives (the document
    was not wholly replaced). In every other case -- the node is still live, the target is a coordinate
    click or lives in an iframe, there is no anchor, the identity is ambiguous or volatile, the element
    was removed, a match exists only in another frame, the document navigated / was replaced, or the
    probe/refresh is indeterminate -- it returns ``None`` so
    the caller dispatches the ORIGINAL scraped_page/action unchanged. Nothing here synthesizes a
    failure, skip, or retry: the pre-existing handler (including its own xpath fallback and
    MissingElement handling) and the owner loop's control flow remain authoritative. This intentionally
    preserves the pre-existing positional-xpath residual risk whenever a remap cannot be established.
    """
    if not isinstance(action, WebAction) or not action.element_id:
        return None
    if isinstance(action, ClickAction) and action.x is not None and action.y is not None:
        return None
    element_dict = scraped_page.id_to_element_dict
    if not isinstance(element_dict, dict):
        return None
    original = element_dict.get(action.element_id)
    if not isinstance(original, dict) or not _has_identity_anchor(original):
        return None

    # Document continuity: the batch was planned against scraped_page.url. If an earlier action in the
    # batch navigated / switched document, the planned action does not belong to the live page -- so
    # decline (before spending a re-scrape) rather than remap it onto an identically-structured control
    # on the destination page.
    if await _document_continuity(scraped_page, page) is not True:
        return None

    css = scraped_page.id_to_css_dict.get(action.element_id)
    frame = scraped_page.id_to_frame_dict.get(action.element_id)
    if not css or not frame:
        return None
    # Frame continuity: only the main frame has an identity that is stable across scrapes
    # ("main.frame"); an iframe's frame token is a per-scrape skyvern id, so a target inside one cannot
    # be matched across a refresh. Decline any non-main-frame target, and (in _unique_match below) only
    # accept candidates in that same frame, so a stale main-frame target is never rebound to an
    # identically id-anchored control in another document.
    if frame != "main.frame":
        return None
    try:
        locator, frame_content = await resolve_locator(scraped_page, page, frame, css)
        if await locator.count() == 1:
            return None  # the exact injected node is still live -> not stale -> dispatch unchanged
    except Exception:
        return None  # cannot confirm liveness / marker survival -> decline; legacy path authoritative

    def _unique_match(
        elements_by_id: dict[str, Any], frame_by_id: dict[str, Any], signature: str, tag_name: Any
    ) -> str | None:
        # Restrict to the target's own frame (an HTML id is only document-unique), and require the same
        # tag -- both necessary for the same instance, and cheap, so they prune the per-element hashing
        # on this (rare) stale-resolution path.
        matches = [
            element_id
            for element_id, element in elements_by_id.items()
            if isinstance(element, dict)
            and frame_by_id.get(element_id) == frame
            and element.get("tagName") == tag_name
            and structural_identity(element) == signature
        ]
        return matches[0] if len(matches) == 1 else None

    try:
        tag_name = original.get("tagName")
        signature = structural_identity(original)
        # Two structurally-identical controls cannot be told apart safely; require the target's
        # identity to be unique in the batch's own scrape (and frame) before trusting a re-scrape.
        if _unique_match(element_dict, scraped_page.id_to_frame_dict, signature, tag_name) != action.element_id:
            return None
    except Exception:
        return None

    try:
        fresh_scraped_page = await scraped_page.generate_scraped_page_without_screenshots()
        fresh_dict = fresh_scraped_page.id_to_element_dict
        fresh_frames = getattr(fresh_scraped_page, "id_to_frame_dict", None)
        fresh_element_id = (
            _unique_match(fresh_dict, fresh_frames, signature, tag_name)
            if isinstance(fresh_dict, dict) and isinstance(fresh_frames, dict)
            else None
        )
    except Exception:
        LOG.warning("Failed to refresh the scraped page for a stale web action", exc_info=True)
        return None

    # The refresh re-scrapes the current page; if that landed on a different document than the batch was
    # planned on, the destination's identically-structured control is not our target -- decline.
    if await _document_continuity(scraped_page, page) is not True:
        return None

    if not fresh_element_id or fresh_element_id == action.element_id:
        LOG.info(
            "No unique anchored structural match after a bounded refresh; leaving the legacy path authoritative",
            element_id=action.element_id,
        )
        return None
    # Rebind the caller's own Action in place (not a copy) so the effective remapped element_id, and
    # every field the normal handler goes on to set, are the ones recorded in the persisted step
    # output and passed to post_action_execution -- the original object never lingers as stale/pending.
    # The owner loop's duplicate-id chain and ordering were already computed from the planned ids
    # before dispatch, so this does not alter action-list ordering or failure policy.
    action.element_id = fresh_element_id
    # Refresh the fresh element's provenance the same way parse_actions builds it, so
    # skyvern_element_hash (cached-action matching) and skyvern_element_data (Action.get_xpath()) stay
    # consistent with the remapped element rather than pointing at the stale one.
    fresh_hash_map = getattr(fresh_scraped_page, "id_to_element_hash", None)
    action.skyvern_element_hash = fresh_hash_map.get(fresh_element_id) if isinstance(fresh_hash_map, dict) else None
    fresh_url = getattr(fresh_scraped_page, "url", None)
    fresh_element = fresh_dict.get(fresh_element_id)
    action.skyvern_element_data = (
        {**fresh_element, "page_url": fresh_url} if isinstance(fresh_element, dict) else {"page_url": fresh_url}
    )
    return fresh_scraped_page, action


async def _document_continuity(scraped_page: ScrapedPage, page: Page) -> bool | None:
    """Return whether the live page still has the batch's original document.

    ``None`` is deliberately indeterminate: destroyed execution contexts and failed probes must
    fall through to the legacy dispatch path rather than being treated as continuity.
    """
    stored_loader_id = getattr(scraped_page, "_document_loader_id", None)
    if stored_loader_id is None:
        return None
    current_loader_id = await get_main_document_loader_id(page)
    return current_loader_id == stored_loader_id if current_loader_id is not None else None


@traced(name="skyvern.agent.action.solve_captcha")
async def handle_solve_captcha_action(
    action: actions.SolveCaptchaAction,
    page: Page,
    scraped_page: ScrapedPage,
    task: Task,
    step: Step,
) -> list[ActionResult]:
    LOG.warning(
        "Please solve the captcha on the page, you have 30 seconds",
        action=action,
    )
    await asyncio.sleep(30)
    return [ActionSuccess()]


async def _retarget_disabled_element_for_click(
    dom: DomUtil,
    skyvern_element: SkyvernElement,
    action: actions.ClickAction,
) -> SkyvernElement | None:
    child_id = skyvern_element.find_deepest_interactable_descendant_in_single_chain()
    if not child_id:
        LOG.debug(
            "No unambiguous single-chain descendant; preserving disabled-element failure",
            parent_id=skyvern_element.get_id(),
        )
        return None
    LOG.info(
        "Re-targeting click from disabled wrapper to deepest single-chain descendant",
        parent_id=skyvern_element.get_id(),
        child_id=child_id,
    )
    child_element = await dom.safe_get_skyvern_element_by_id(child_id)
    if not child_element or await child_element.is_disabled(dynamic=True):
        LOG.debug(
            "Single-chain descendant not found or dynamically disabled; preserving failure",
            parent_id=skyvern_element.get_id(),
            child_id=child_id,
        )
        return None
    # Mutate only after DOM resolution + dynamic disabled validation.
    action.element_id = child_id
    return child_element


def _parse_aria_boolean(value: str | None) -> bool | None:
    """Interpret an ARIA boolean-state string. Returns True/False only for an exact "true"/"false"
    (case-insensitive, trimmed); anything else -- absent, "mixed", malformed -- returns None so the
    caller treats the state as unreadable and falls open to an ordinary click."""
    if value is None:
        return None
    normalized = value.strip().casefold()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    return None


async def _is_single_select_option_highlight(element: SkyvernElement) -> bool:
    """True when a live aria-selected="true" belongs to a single-select option, where ARIA treats it
    as the pre-commit keyboard highlight rather than committed state (mirrors
    _custom_select_matched_state_confirms_pre_click): explicit role=option with no
    aria-multiselectable="true" ancestor-or-self. In a multiselectable container aria-selected is
    committed, so this returns False and the value stays readable."""
    if (await element.get_attr("role", mode="dynamic") or "").strip().casefold() != "option":
        return False
    multiselectable = element.get_locator().locator("xpath=ancestor-or-self::*[@aria-multiselectable][1]")
    if await multiselectable.count() == 0:
        return True
    return (await multiselectable.get_attribute("aria-multiselectable") or "").strip().casefold() != "true"


class _GridRowSelection(StrEnum):
    """Row-selection state of a checkbox's grid context. NOT_GRID_ROW: not a grid row-selection
    control, so native ``checked`` is the whole truth. SELECTED/UNSELECTED: the closest row's
    app-controlled selection *positively* read (aria-selected true/false, or a known selected class for
    SELECTED); it can diverge from the input's native ``checked``. UNMARKED: a well-formed grid
    row-selection row that exposes no positive selected signal (no aria-selected, no known class) --
    absence, which is NOT a proof of unselected, so it never suppresses a needed click and is never
    cell-driven (no readable post-state exists to prove), only handled by the ordinary click. UNREADABLE:
    the snapshot could not be read (malformed
    or a bad value), so the caller falls open to an ordinary click rather than trusting native state."""

    NOT_GRID_ROW = "not_grid_row"
    SELECTED = "selected"
    UNSELECTED = "unselected"
    UNMARKED = "unmarked"
    UNREADABLE = "unreadable"


# A selectable ARIA grid tracks row selection as app state on the closest row, independent of a
# selection checkbox's native `checked`. This is a dumb DOM extractor; every decision lives in the pure
# `_classify_grid_row_selection`, so classification is unit-tested without a browser and a snapshot that
# is not the exact shape it promises is UNREADABLE (fail open to an ordinary click). Detection is purely
# structural and framework-token-free: the checkbox's closest cell must be the row's unique selection
# cell -- an exact role=gridcell that is the ONLY direct role=gridcell cell in the row. An ordinary
# boolean data-column checkbox sits among several role=gridcell cells (or in a bare td), so it stays a
# native checkbox rather than a row-selection control.
_GRID_ROW_SNAPSHOT_JS = """
(el, selectedTokens) => {
  const container = el.closest('[role="grid"], [role="treegrid"]');
  const row = el.closest('[role="row"], tr');
  const cell = el.closest('[role="gridcell"], td');
  const inRow = row !== null && row !== container;
  const inCell = cell !== null && cell !== container && cell !== row;
  // The row and cell must belong to the same closest grid as the checkbox, so a nested grid cannot
  // pair an inner selection cell with an outer row.
  const sameGridChain = container !== null && inRow && inCell && container.contains(row) && row.contains(cell);
  const candidateExactGridcell = cell !== null && cell.getAttribute('role') === 'gridcell';
  const candidateDirectRowChild = cell !== null && inRow && cell.parentElement === row;
  let gridCellCount = 0;
  if (inRow) {
    const children = row.children;
    for (let i = 0; i < children.length; i++) {
      if (children[i].getAttribute('role') === 'gridcell') { gridCellCount++; }
    }
  }
  // Whether any OTHER row of this same grid holds a POSITIVE selection (aria-selected="true" or a known
  // selected class token). A cell click can replace/clear the whole selection, so this proves whether a
  // recovery could destroy another row's selection; the same positive vocabulary as the per-row read.
  const tokens = Array.isArray(selectedTokens) ? selectedTokens : [];
  let otherRowSelected = false;
  if (container !== null && row !== null) {
    const rows = container.querySelectorAll('[role="row"], tr');
    for (let i = 0; i < rows.length && !otherRowSelected; i++) {
      const r = rows[i];
      if (r === row) { continue; }
      if (r.closest('[role="grid"], [role="treegrid"]') !== container) { continue; }
      const asel = r.getAttribute('aria-selected');
      if (asel !== null && asel.trim().toLowerCase() === 'true') { otherRowSelected = true; break; }
      const cls = Array.prototype.slice.call(r.classList).map(function (c) { return c.trim().toLowerCase(); });
      for (let j = 0; j < tokens.length; j++) {
        if (cls.indexOf(tokens[j]) !== -1) { otherRowSelected = true; break; }
      }
    }
  }
  return {
    inHeader: el.closest('thead, [role="columnheader"]') !== null,
    hasGrid: container !== null,
    hasRow: inRow,
    hasCell: inCell,
    sameGridChain: sameGridChain,
    isCheckbox: typeof el.matches === 'function' && el.matches('input[type="checkbox"]'),
    candidateExactGridcell: candidateExactGridcell,
    candidateDirectRowChild: candidateDirectRowChild,
    gridCellCount: gridCellCount,
    rowAriaSelected: inRow ? row.getAttribute('aria-selected') : null,
    rowClasses: inRow ? Array.prototype.slice.call(row.classList) : null,
    otherRowSelected: otherRowSelected,
  };
}
"""

# Atomic recovery-targeting read. Finds one locator-relative point on the selection cell whose ACTUAL
# hit target (`document.elementFromPoint`) is the cell itself -- not the checkbox, a label, any other
# descendant (interactive or not), an overlay, an iframe, or null. Candidates are inside the cell's
# padding box clipped to the viewport and to every clipping ancestor, so a partially-scrolled cell is
# probed only where it is actually visible. Returns a point relative to the cell padding-box top-left
# (the Playwright `position` origin) or null when no candidate resolves to the cell (fail closed). A
# selector allow-list of descendant types is necessarily incomplete; hit-testing is the only proof that
# a point is clear cell space.
_SAFE_CELL_POINT_JS = """
(el) => {
  const cell = el.closest('[role="gridcell"], td');
  if (cell === null) { return null; }
  const rect = cell.getBoundingClientRect();
  const padLeft = rect.left + cell.clientLeft;
  const padTop = rect.top + cell.clientTop;
  let left = Math.max(padLeft, 0);
  let top = Math.max(padTop, 0);
  let right = Math.min(padLeft + cell.clientWidth, window.innerWidth);
  let bottom = Math.min(padTop + cell.clientHeight, window.innerHeight);
  for (let a = cell.parentElement; a !== null; a = a.parentElement) {
    const style = window.getComputedStyle(a);
    if (style.overflowX !== 'visible' || style.overflowY !== 'visible') {
      const ar = a.getBoundingClientRect();
      left = Math.max(left, ar.left);
      top = Math.max(top, ar.top);
      right = Math.min(right, ar.right);
      bottom = Math.min(bottom, ar.bottom);
    }
  }
  const w = right - left;
  const h = bottom - top;
  if (w <= 0 || h <= 0) { return null; }
  const fractions = [
    [0.5, 0.5],
    [0.85, 0.5], [0.15, 0.5], [0.5, 0.15], [0.5, 0.85],
    [0.85, 0.15], [0.15, 0.15], [0.85, 0.85], [0.15, 0.85],
    [0.7, 0.5], [0.3, 0.5], [0.5, 0.7], [0.5, 0.3]
  ];
  for (let i = 0; i < fractions.length; i++) {
    const vx = left + w * fractions[i][0];
    const vy = top + h * fractions[i][1];
    if (document.elementFromPoint(vx, vy) === cell) {
      return { x: vx - padLeft, y: vy - padTop };
    }
  }
  return null;
}
"""

_ROW_SELECTION_SETTLE_JS = (
    "() => new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(() => requestAnimationFrame(r))))"
)

# Common selected-row class conventions, consulted only when the row exposes no aria-selected. Exact
# lowercased-token membership only (never a substring/suffix match).
_SELECTED_ROW_CLASS_TOKENS = frozenset({"selected", "is-selected", "k-selected"})
_REQUIRED_SNAPSHOT_BOOL_KEYS = (
    "inHeader",
    "hasGrid",
    "hasRow",
    "hasCell",
    "sameGridChain",
    "isCheckbox",
    "candidateExactGridcell",
    "candidateDirectRowChild",
)


def _is_str_list_or_none(value: object) -> bool:
    return value is None or (isinstance(value, list) and all(isinstance(item, str) for item in value))


def _classify_grid_row_selection(snapshot: object) -> _GridRowSelection:
    """Classify the ``_GRID_ROW_SNAPSHOT_JS`` result. A snapshot that is not the exact shape the
    extractor promises -- a non-dict, a missing required key, or a wrong field type -- is UNREADABLE and
    never silently treated as an ordinary checkbox. NOT_GRID_ROW is returned only when a well-formed
    snapshot positively proves a non-grid-row context: a header cell, not a checkbox inside its own
    grid -> row -> cell chain, or a row without the unique selection-cell signature (the checkbox's
    closest cell must be an exact role=gridcell and the ONLY direct role=gridcell cell in the row, so an
    ordinary data-column checkbox among several gridcells stays a native checkbox). Otherwise the closest
    row's selection: aria-selected is authoritative when present (exact true/false after normalizing; any
    other token is UNREADABLE), else an exact selected-row class token is SELECTED; absent both, the row
    is UNMARKED -- absence of a positive selected signal, which the caller must not read as UNSELECTED."""
    if not isinstance(snapshot, dict):
        return _GridRowSelection.UNREADABLE
    for key in _REQUIRED_SNAPSHOT_BOOL_KEYS:
        if not isinstance(snapshot.get(key), bool):
            return _GridRowSelection.UNREADABLE
    grid_cell_count = snapshot.get("gridCellCount")
    if not isinstance(grid_cell_count, int) or isinstance(grid_cell_count, bool):
        return _GridRowSelection.UNREADABLE
    for key in ("rowClasses", "rowAriaSelected"):
        if key not in snapshot:
            return _GridRowSelection.UNREADABLE
    row_classes = snapshot["rowClasses"]
    aria_selected = snapshot["rowAriaSelected"]
    if not _is_str_list_or_none(row_classes):
        return _GridRowSelection.UNREADABLE
    if not (aria_selected is None or isinstance(aria_selected, str)):
        return _GridRowSelection.UNREADABLE

    if snapshot["inHeader"]:
        return _GridRowSelection.NOT_GRID_ROW
    if not (snapshot["hasGrid"] and snapshot["hasRow"] and snapshot["hasCell"] and snapshot["isCheckbox"]):
        return _GridRowSelection.NOT_GRID_ROW
    if not snapshot["sameGridChain"]:
        return _GridRowSelection.NOT_GRID_ROW
    # The unique selection-cell signature: the checkbox's closest cell is an exact role=gridcell and the
    # only direct role=gridcell cell in the row. Absent it -- a bare td, a nested gridcell, or several
    # data cells with role=gridcell -- the checkbox is an ordinary data control, not a row-selection one.
    if not (snapshot["candidateExactGridcell"] and snapshot["candidateDirectRowChild"] and grid_cell_count == 1):
        return _GridRowSelection.NOT_GRID_ROW

    if aria_selected is not None:
        token = aria_selected.strip().lower()
        if token == "true":
            return _GridRowSelection.SELECTED
        if token == "false":
            return _GridRowSelection.UNSELECTED
        return _GridRowSelection.UNREADABLE
    if row_classes is not None and not _SELECTED_ROW_CLASS_TOKENS.isdisjoint(
        cls.strip().lower() for cls in row_classes
    ):
        return _GridRowSelection.SELECTED
    # No aria-selected and no known selected class: absence, not a positive UNSELECTED. Return UNMARKED
    # so the caller never suppresses a needed click from it and never cell-drives it (no readable
    # post-state exists to prove) -- it is handled by the ordinary click.
    return _GridRowSelection.UNMARKED


def _grid_other_row_selected(snapshot: object) -> bool:
    """Whether the snapshot proves that another row of the same grid holds a positive selection. Only a
    literal ``True`` counts, so a missing/malformed field is treated as 'not proven', matching the
    fail-open direction of the classifier."""
    return isinstance(snapshot, dict) and snapshot.get("otherRowSelected") is True


class _GridRowRead(NamedTuple):
    """A single snapshot read of a grid row-selection checkbox: its classified ``state`` and whether any
    OTHER row of the same grid is positively selected (``other_row_selected``), so a cell recovery can be
    proven not to clear another row's selection without a second, racy read."""

    state: _GridRowSelection
    other_row_selected: bool


async def _read_grid_row_selection(element: SkyvernElement) -> _GridRowRead:
    """The checkbox's grid row-selection state plus the grid-wide other-row-selected flag from ONE
    snapshot read, or UNREADABLE when the snapshot cannot be read -- a failed read falls open to an
    ordinary click, never to a native-only read that could report a checked-but-unselected row as
    already selected."""
    try:
        snapshot = await element.get_locator().evaluate(_GRID_ROW_SNAPSHOT_JS, sorted(_SELECTED_ROW_CLASS_TOKENS))
    except Exception:
        LOG.debug("Grid row-selection snapshot read failed; treating as unreadable", element_id=element.get_id())
        return _GridRowRead(_GridRowSelection.UNREADABLE, False)
    return _GridRowRead(_classify_grid_row_selection(snapshot), _grid_other_row_selected(snapshot))


def _is_finite_number(value: object) -> TypeGuard[float]:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _coerce_click_position(point: object) -> dict[str, float] | None:
    """Validate the locator-relative point returned by ``_SAFE_CELL_POINT_JS`` -- a finite {x, y}
    padding-box offset hit-tested to land on the selection cell -- before it is handed to Playwright, or
    None when no cell-space point was proven (fail closed)."""
    if not isinstance(point, dict):
        return None
    x = point.get("x")
    y = point.get("y")
    if not _is_finite_number(x) or not _is_finite_number(y):
        return None
    return {"x": float(x), "y": float(y)}


async def _grid_row_reached_state(element: SkyvernElement, desired_state: bool) -> bool:
    """After a bounded settle so the framework can commit selection, whether the closest row reached the
    desired selection state. Desired-true requires a POSITIVE SELECTED read. Desired-false (only ever
    driven from a positively-SELECTED row) requires the positive selected signal to be gone -- UNSELECTED
    (aria-selected="false") or UNMARKED (the signal removed). A read failure or the opposite state is a
    failure (fail closed)."""
    try:
        await element.get_locator().evaluate(_ROW_SELECTION_SETTLE_JS)
    except Exception:
        pass
    reached = (await _read_grid_row_selection(element)).state
    if desired_state:
        return reached is _GridRowSelection.SELECTED
    return reached is _GridRowSelection.UNSELECTED or reached is _GridRowSelection.UNMARKED


async def _drive_grid_row_selection(
    action: actions.ClickAction, element: SkyvernElement, desired_state: bool
) -> list[ActionResult] | None:
    """Drive a grid row-selection checkbox to the desired row-selection state by clicking a proven-clear
    point on the selection cell (hit-tested via ``document.elementFromPoint``), then re-reading the
    row's selection after a bounded settle. Entered only from a positively-readable start (an UNSELECTED
    row to select, a SELECTED row to deselect), so the re-read after the drive can actually prove the
    result; never entered for an UNMARKED row, whose selection is unreadable in both directions. A native
    check()/uncheck() flips the input's ``checked`` without entering the framework's row selection (the
    divergence), so it is never used here. Returns
    [ActionAbort()] when the row reaches the desired selection, or None to fall through to a single
    ordinary click when the cell nests an interactive control, no proven cell-space point exists, or the
    click does not achieve the desired selection. Never loops, force-clicks, or mutates the DOM."""
    cell_locator = element.get_locator().locator('xpath=ancestor-or-self::*[@role="gridcell" or self::td][1]').first
    try:
        if await cell_locator.count() == 0:
            return None
        if await SkyvernElement._label_click_forwards_to_descendant(cell_locator, fail_closed=True):
            LOG.warning(
                "Grid row-selection cell nests a link/button; continuing the normal click",
                element_id=element.get_id(),
            )
            return None
        point = _coerce_click_position(await cell_locator.evaluate(_SAFE_CELL_POINT_JS))
        if point is None:
            LOG.warning(
                "No proven cell-space point clear of controls for grid row selection, continuing the normal click",
                element_id=element.get_id(),
            )
            return None
        await cell_locator.click(timeout=settings.BROWSER_ACTION_TIMEOUT_MS, position=point)
    except Exception:
        LOG.warning(
            "Grid row-selection click failed, continuing the normal click", element_id=element.get_id(), exc_info=True
        )
        return None
    if await _grid_row_reached_state(element, desired_state):
        LOG.info("Grid row reached the desired selection state", action=action, desired_state=desired_state)
        return [ActionAbort()]
    LOG.warning(
        "Grid row selection did not reach the desired state, continuing the normal click",
        element_id=element.get_id(),
    )
    return None


async def _checkbox_live_state(element: SkyvernElement, grid_state: _GridRowSelection) -> bool | None:
    """A checkbox's live selected state from an already-read grid classification: the closest row's
    selection when it is a grid row-selection control (native ``checked`` can diverge from app row
    selection), else the input's native ``checked``. None -- fall open -- when the row state is
    unreadable, or is UNMARKED (a well-formed row with no positive selected signal: absence is not a
    proof of unselected), or the native read fails."""
    if grid_state is _GridRowSelection.SELECTED:
        return True
    if grid_state is _GridRowSelection.UNSELECTED:
        return False
    if grid_state is _GridRowSelection.UNREADABLE or grid_state is _GridRowSelection.UNMARKED:
        return None
    try:
        return await element.is_checked()
    except Exception:
        LOG.debug(
            "Failed to read native checkbox state; continuing with an ordinary click", element_id=element.get_id()
        )
        return None


async def _resolve_live_selected_state(element: SkyvernElement) -> bool | None:
    """Read one generic, live observable of a control's selected/checked state, or None when no
    boolean observable is readable (unknown control, malformed value, or a detached/unreadable
    element) so the caller falls open to an ordinary click. Native radio inputs report through
    is_checked(); a checkbox reports its grid row's selection when it is a grid row-selection control
    (native `checked` can diverge from app row selection) and otherwise its native is_checked(); other
    controls expose an exact aria-checked/aria-pressed/aria-selected boolean read live. Role only
    chooses which observable to read and whether a bare aria-selected value is trustworthy; it never
    implies desired intent."""
    try:
        if element.get_tag_name() == "input":
            input_type = (await element.get_attr("type") or "").strip().casefold()
            if input_type == "checkbox":
                return await _checkbox_live_state(element, (await _read_grid_row_selection(element)).state)
            if input_type == "radio":
                return await element.is_checked()
            return None
        for aria_attr in ("aria-checked", "aria-pressed", "aria-selected"):
            parsed = _parse_aria_boolean(await element.get_attr(aria_attr, mode="dynamic"))
            if parsed is None:
                continue
            if aria_attr == "aria-selected" and parsed and await _is_single_select_option_highlight(element):
                # A single-select option's aria-selected="true" is a keyboard highlight, not committed
                # state, so leave it unreadable and let the physical click commit the selection.
                return None
            return parsed
        return None
    except Exception:
        LOG.debug("Failed to read live selected state; continuing with an ordinary click", element_id=element.get_id())
        return None


async def _get_associated_checkbox_label_locator(element: SkyvernElement) -> Locator | None:
    ancestor_label_locator = element.get_locator().locator("xpath=ancestor::label[1]")
    if await ancestor_label_locator.count() > 0:
        return ancestor_label_locator

    input_id = await element.get_attr("id", mode="dynamic")
    if not input_id:
        return None

    explicit_label_locator = element.get_frame().locator(f"label[for={json.dumps(input_id)}]")
    if await explicit_label_locator.count() > 0:
        return explicit_label_locator.first

    return None


async def _set_native_checkbox_state(element: SkyvernElement, should_check: bool) -> bool:
    """Drive a native checkbox/radio input to ``should_check`` and report whether the final state
    matches. Sets through the input, falling back to a visible associated label when the input is
    hidden/non-actionable (skipping a label that forwards its click to an interactive descendant)."""
    locator = element.get_locator()
    try:
        if await locator.is_checked(timeout=settings.BROWSER_ACTION_TIMEOUT_MS) == should_check:
            return True
    except Exception:
        # Keep moving: check()/uncheck() are state-setting operations for actionable inputs,
        # and the label fallback below verifies the final state for hidden inputs.
        LOG.warning("Failed to read checkbox state before setting it", element_id=element.get_id(), exc_info=True)

    try:
        if should_check:
            await element.check()
        else:
            await element.uncheck()
        return True
    except Exception:
        LOG.warning(
            "Failed to set checkbox state through input, trying associated label",
            element_id=element.get_id(),
            should_check=should_check,
            exc_info=True,
        )

    label_locator = await _get_associated_checkbox_label_locator(element)
    if label_locator is None:
        return False

    try:
        if not await label_locator.is_visible():
            return False
        if await SkyvernElement._label_click_forwards_to_descendant(label_locator, fail_closed=True):
            LOG.warning(
                "Associated checkbox label contains an interactive descendant",
                element_id=element.get_id(),
                should_check=should_check,
            )
            return False
        await label_locator.click(timeout=settings.BROWSER_ACTION_TIMEOUT_MS)
        return await locator.is_checked(timeout=settings.BROWSER_ACTION_TIMEOUT_MS) == should_check
    except Exception:
        LOG.warning(
            "Failed to set checkbox state through associated label",
            element_id=element.get_id(),
            should_check=should_check,
            exc_info=True,
        )
        return False


_LABEL_CONTROL_STATE_JS = r"""
(el) => {
    const control = el.control;
    if (!control) return null;
    if (!el.hasAttribute("for")) {
        const controls = Array.from(
            el.querySelectorAll("button, input, meter, output, progress, select, textarea")
        ).filter((c) => !(c instanceof HTMLInputElement && c.type === "hidden"));
        if (controls.length !== 1 || control !== controls[0]) return null;
    }
    if (control instanceof HTMLInputElement && (control.type === "checkbox" || control.type === "radio")) {
        return control.checked;
    }
    return null;
}
"""


async def _read_label_control_state(label: SkyvernElement) -> bool | None:
    """Read the live checked state of a label's bound control the way browser label activation
    resolves it, via the browser's own ``HTMLLabelElement.control``. For an explicit ``for=`` label
    the id-referenced labelable element is used directly (a for= label never falls back to
    descendants); for an implicit label the single labelable descendant at any depth or visibility
    (no scraped-interactability filter) is cross-checked against ``el.control``. Returns None -- fall
    open -- when no control resolves, when an implicit label has zero or several labelable descendants
    or the browser disagrees with the unique candidate, when the control is not a native
    checkbox/radio, or when the read fails. State is read in-page rather than via a mapped
    SkyvernElement because a display:none control is never scraped and has no unique_id."""
    try:
        state = await _evaluate_element_scoped(label, _LABEL_CONTROL_STATE_JS)
    except Exception:
        LOG.debug(
            "Failed to read label control state; continuing with an ordinary click",
            element_id=label.get_id(),
        )
        return None
    return state if isinstance(state, bool) else None


async def _resolve_effective_click_state(element: SkyvernElement, dom: DomUtil) -> bool | None:
    """Live selected/checked state of the control a click on ``element`` actually toggles: the
    element's own observable, or, for a <label>, its spec-bound control (mapped explicit for=
    control first, else the in-page label.control read). None = unreadable; callers fall open."""
    if element.get_tag_name() != "label":
        return await _resolve_live_selected_state(element)
    control = await element.find_label_for(dom)
    if control is not None:
        return await _resolve_live_selected_state(control)
    return await _read_label_control_state(element)


async def _apply_label_desired_click_state(
    action: actions.ClickAction, label: SkyvernElement, desired_state: bool, dom: DomUtil
) -> list[ActionResult] | None:
    """A <label> click forwards activation to its spec-bound control, so observe that control's
    live state rather than the label's (labels carry no checked/selected observable). Suppress the
    redundant click when the bound control already holds the desired state; on a mismatch, or when
    no bound control resolves or its state is unreadable, fall through to a single ordinary label
    click, whose forwarding performs the one toggle. The control is resolved deterministically via
    the spec-defined association (explicit for=-id, else the wrapped labelable descendant read
    in-page); state is never driven through the label."""
    control_state = await _resolve_effective_click_state(label, dom)
    if control_state is None:
        LOG.info("Label click has no readable bound-control state, continuing the normal click", action=action)
        return None
    if control_state == desired_state:
        LOG.info(
            "Label's bound control already in the desired state, suppressing the redundant click",
            action=action,
            desired_state=desired_state,
        )
        return [ActionAbort()]
    LOG.info(
        "Label's bound control differs from the desired state, continuing with a single normal click",
        action=action,
        desired_state=desired_state,
    )
    return None


async def _apply_checkbox_desired_click_state(
    action: actions.ClickAction, element: SkyvernElement, desired_state: bool
) -> list[ActionResult] | None:
    """Drive a checkbox to an explicit terminal state from a SINGLE grid row-selection snapshot read
    (state + grid-wide other-row-selected). An ordinary checkbox (NOT_GRID_ROW) is set natively. A grid
    row-selection checkbox drives app row selection through its selection cell -- a native set flips
    ``checked`` while the row stays unselected, the divergence this guards against -- so it is recovered
    on the cell and never check()/uncheck(). Returns [ActionAbort()] only from a positively-proven state,
    or None to fall through to a single ordinary click. Invariants: absence of a positive selected signal
    (UNMARKED) never suppresses a needed click; the cell is driven ONLY from a positively-readable start
    (UNSELECTED to select, SELECTED to deselect) whose result the drive can then read, and only when no
    OTHER row of the grid holds a readable selection, so a recovery can never clear another row's
    selection; a drive aborts only after a positively-readable post-state; and an UNMARKED row -- no
    positive selected signal, DOM-indistinguishable from a foreign-vocabulary selected row -- is never
    cell-driven, only handled by the single ordinary click."""
    grid = await _read_grid_row_selection(element)
    state = grid.state

    if state is _GridRowSelection.NOT_GRID_ROW:
        native = await _checkbox_live_state(element, state)
        if native is None:
            LOG.info("No readable selected state, continuing the normal click", action=action)
            return None
        if native == desired_state:
            LOG.info("Control already in the desired state, suppressing the redundant click", action=action)
            return [ActionAbort()]
        LOG.info("Setting the native control to the desired state", action=action, desired_state=desired_state)
        if await _set_native_checkbox_state(element, should_check=desired_state):
            return [ActionAbort()]
        LOG.warning("Failed to set the native control to the desired state, continuing the normal click", action=action)
        return None

    if state is _GridRowSelection.UNREADABLE:
        LOG.info("Grid row selection unreadable, continuing the normal click", action=action)
        return None

    # A grid row-selection checkbox with a well-formed snapshot: SELECTED / UNSELECTED / UNMARKED.
    positively_selected = state is _GridRowSelection.SELECTED
    positively_unselected = state is _GridRowSelection.UNSELECTED
    if positively_selected and desired_state:
        LOG.info("Row already selected, suppressing the redundant click", action=action)
        return [ActionAbort()]
    if positively_unselected and not desired_state:
        LOG.info("Row already unselected, suppressing the redundant click", action=action)
        return [ActionAbort()]

    if not desired_state:
        # Deselect intent. Only a positively-SELECTED row is driven off its selection; an UNMARKED row is
        # never suppressed from absence, and is only treated as already-unselected when native `checked`
        # positively agrees the box is off. Otherwise fall open without claiming success.
        if positively_selected:
            if grid.other_row_selected:
                LOG.info("Another row is selected; not driving the cell, continuing the normal click", action=action)
                return None
            return await _drive_grid_row_selection(action, element, desired_state)
        try:
            native_checked = await element.is_checked()
        except Exception:
            native_checked = None
        if native_checked is False:
            LOG.info("Unmarked row with the box positively off already matches desired unselected", action=action)
            return [ActionAbort()]
        LOG.info("Unmarked row can't be proven unselected, continuing the normal click", action=action)
        return None

    # Select intent from a row not positively selected. Only a positively-UNSELECTED row is driven through
    # the selection cell: both its start state and the SELECTED post-state the drive must prove are
    # readable, and only when no OTHER row holds a readable selection so the recovery can never
    # replace/clear it. An UNMARKED row exposes no positive selected signal, so neither its start nor a
    # post-drive selection can be read -- a cell drive there would be an unverifiable state-changing probe
    # on an ambiguous row (and could clear a selection the gate cannot see). Never drive it: fall open to
    # the single ordinary click, whose real activation enters the framework's own selection.
    if positively_unselected:
        if grid.other_row_selected:
            LOG.info("Another row is selected; not driving the cell, continuing the normal click", action=action)
            return None
        return await _drive_grid_row_selection(action, element, desired_state)
    LOG.info("Unmarked row can't be proven selected by a cell drive, continuing the normal click", action=action)
    return None


async def _input_type_or_none(element: SkyvernElement) -> str | None:
    """The casefolded ``type`` of an input, or None when the read raises (a detached/unreadable
    element). Reading it outside a guard would propagate; the merge base falls open to one ordinary
    click, so a failed read must too."""
    try:
        return (await element.get_attr("type") or "").strip().casefold()
    except Exception:
        LOG.debug("Failed to read input type; continuing with an ordinary click", element_id=element.get_id())
        return None


async def _apply_desired_click_state(
    action: actions.ClickAction, element: SkyvernElement, desired_state: bool, dom: DomUtil
) -> list[ActionResult] | None:
    """Drive a selectable control to an explicit terminal state idempotently. Returns
    [ActionAbort()] to suppress the physical click -- when the control already matches the desired
    state, or after a native checkbox/radio is set here -- or None to fall through to a single
    ordinary click when the live state is unreadable (fail open) or a custom control must be clicked
    once to change. Never converts an explicit desired_state=False into a check."""
    if element.get_tag_name() == "label":
        return await _apply_label_desired_click_state(action, element, desired_state, dom)
    if element.get_tag_name() == "input" and await _input_type_or_none(element) == "checkbox":
        return await _apply_checkbox_desired_click_state(action, element, desired_state)
    live_state = await _resolve_live_selected_state(element)
    if live_state is None:
        LOG.info("No readable selected state, continuing the normal click", action=action)
        return None
    if live_state == desired_state:
        LOG.info(
            "Control already in the desired state, suppressing the redundant click",
            action=action,
            desired_state=desired_state,
        )
        return [ActionAbort()]
    if element.get_tag_name() == "input":
        # Only a native radio reaches here (checkbox is handled above; a non-toggle input has no
        # readable state and already fell open), and a radio can't be turned off by clicking it -- only
        # selecting another radio in the group clears it -- so skip the doomed uncheck() on desired=False.
        if not desired_state:
            LOG.info("A radio can't be unchecked in place, continuing with a single normal click", action=action)
            return None
        LOG.info("Setting the native control to the desired state", action=action, desired_state=desired_state)
        if await _set_native_checkbox_state(element, should_check=desired_state):
            return [ActionAbort()]
        LOG.warning("Failed to set the native control to the desired state, continuing the normal click", action=action)
        return None
    LOG.info(
        "Custom control differs from the desired state, continuing with a single normal click",
        action=action,
        desired_state=desired_state,
    )
    return None


@traced(name="skyvern.agent.action.click")
async def handle_click_action(
    action: actions.ClickAction,
    page: Page,
    scraped_page: ScrapedPage,
    task: Task,
    step: Step,
) -> list[ActionResult]:
    # Get wait config once for this handler
    wait_config = await get_or_create_wait_config(task.task_id, task.workflow_run_id, task.organization_id)

    dom = DomUtil(scraped_page=scraped_page, page=page)
    original_url = page.url
    if action.x is not None and action.y is not None:
        # Find the element at the clicked location using JavaScript evaluation
        element_id: str | None = await page.evaluate(
            """data => {
            const element = document.elementFromPoint(data.x, data.y);
            if (!element) return null;

            // Function to get the unique_id attribute of an element
            function getElementUniqueId(element) {
                if (element && element.nodeType === 1) {
                    // Check if the element has the unique_id attribute
                    if (element.hasAttribute('unique_id')) {
                        return element.getAttribute('unique_id');
                    }

                    // If no unique_id attribute is found, return null
                    return null;
                }
                return null;
            }

            return getElementUniqueId(element);
        }""",
            {"x": action.x, "y": action.y},
        )
        LOG.info("Clicked element at location", x=action.x, y=action.y, element_id=element_id, button=action.button)
        if element_id:
            if skyvern_element := await dom.safe_get_skyvern_element_by_id(element_id):
                if await skyvern_element.navigate_to_a_href(page=page):
                    return [ActionSuccess()]

        await EventStrategyFactory.move_cursor(page, action.x, action.y)
        if action.repeat == 1:
            await page.mouse.click(x=action.x, y=action.y, button=action.button)
        elif action.repeat == 2:
            await page.mouse.dblclick(x=action.x, y=action.y, button=action.button)
        elif action.repeat == 3:
            await page.mouse.click(x=action.x, y=action.y, button=action.button, click_count=3)
        else:
            raise ValueError(f"Invalid repeat value: {action.repeat}")

        return [ActionSuccess()]

    skyvern_element = await dom.get_skyvern_element_by_id(action.element_id)

    # Wait after getting element to allow any dynamic changes
    await asyncio.sleep(get_wait_time(wait_config, "post_click_delay", default=0.3))

    # Level-triggered toggle intent (ClickContext.desired_state) is resolved here, with the live
    # element in hand and before any physical click: suppress a redundant click when the control
    # already holds the desired state, drive a native checkbox/radio to it, or fall through to a
    # single ordinary click for a custom-control mismatch or an unreadable state.
    if action.click_context is not None and action.click_context.desired_state is not None:
        desired_state_result = await _apply_desired_click_state(
            action, skyvern_element, action.click_context.desired_state, dom
        )
        if desired_state_result is not None:
            return desired_state_result

    # dynamically validate the attr, since it could change into enabled after the previous actions
    if await skyvern_element.is_disabled(dynamic=True):
        child = await _retarget_disabled_element_for_click(
            dom=dom,
            skyvern_element=skyvern_element,
            action=action,
        )
        if child is not None:
            skyvern_element = child
            # Retarget moved the click to the descendant that will actually receive it, so re-run
            # the same guard on the child: the pre-retarget pass observed the (unreadable) wrapper
            # and fell open, and without this the retargeted child could still be re-toggled.
            if action.click_context is not None and action.click_context.desired_state is not None:
                desired_state_result = await _apply_desired_click_state(
                    action, skyvern_element, action.click_context.desired_state, dom
                )
                if desired_state_result is not None:
                    return desired_state_result
        elif not await SkyvernElement.wait_until_enabled(skyvern_element):
            LOG.warning(
                "Try to click on a disabled element",
                action_type=action.action_type,
                element_id=skyvern_element.get_id(),
            )
            return [ActionFailure(InteractWithDisabledElement(skyvern_element.get_id()))]

    # Skip scroll_into_view when a SCROLL action just completed on THIS element.
    # The scroll may have positioned the page or a container at the bottom to enable
    # T&C buttons; element.scrollIntoView() would undo that positioning.
    # Uses element ID matching (not a boolean) so unrelated clicks aren't affected.
    skip_scroll_into_view = await page.evaluate(
        "(id) => { const v = window.__skyvernScrolledElementId;"
        " window.__skyvernScrolledElementId = null; return v === id; }",
        action.element_id,
    )
    if skip_scroll_into_view:
        LOG.info(
            "Skipping scroll_into_view after deliberate scroll action to preserve scroll position",
            element_id=skyvern_element.get_id(),
        )
    else:
        try:
            await skyvern_element.scroll_into_view()
        except Exception:
            LOG.info(
                "Failed to scroll into view, ignore it and continue executing",
                element_id=skyvern_element.get_id(),
            )

    if action.download:
        results = await handle_click_to_download_file_action(action, page, scraped_page, task, step)

    elif action.file_url:
        upload_file_action = UploadFileAction(
            reasoning=action.reasoning,
            intention=action.intention,
            element_id=action.element_id,
            file_url=action.file_url,
        )
        preflight_derived_action(upload_file_action, page, parent=action, site="click_to_upload")
        return await handle_upload_file_action(upload_file_action, page, scraped_page, task, step)
    else:
        incremental_scraped: IncrementalScrapePage | None = None
        # Inside a file-download block, a non-download click authorized as a false-click candidate can
        # still mint the file. If it does, the post-click dropdown/custom-select rescrape below is dead
        # work that costs ~90-120s; observing the download lets us skip straight to the click result.
        false_click_download_observed: asyncio.Event | None = (
            asyncio.Event() if _false_click_download_eligible.get() else None
        )
        remove_download_probe: Callable[[], None] | None = None
        try:
            engine_selection = resolve_engine_selection_for_task(task, app.BROWSER_MANAGER)
            skyvern_frame = await SkyvernFrame.create_instance(
                skyvern_element.get_frame(), engine_selection=engine_selection
            )
            incremental_scraped = IncrementalScrapePage(
                skyvern_frame=skyvern_frame,
                engine_selection=engine_selection,
            )
            await incremental_scraped.start_listen_dom_increment(await skyvern_element.get_element_handler())
            if false_click_download_observed is not None:
                remove_download_probe = _register_false_click_download_probe(page, false_click_download_observed)

            has_onclick_attr = await skyvern_element.has_attr("onclick", mode="static")
            results = await chain_click(
                task,
                scraped_page,
                page,
                action,
                skyvern_element,
                timeout=settings.BROWSER_ACTION_TIMEOUT_MS,
                incremental_scraped=incremental_scraped,
                skyvern_frame=skyvern_frame,
            )
            if page.url != original_url:
                return results

            if results and not isinstance(results[-1], ActionSuccess):
                return results

            try:
                if false_click_download_observed is not None and false_click_download_observed.is_set():
                    LOG.info(
                        "Same-action download observed for a file-download click; bypassing dropdown rescrape",
                        element_id=skyvern_element.get_id(),
                    )
                    return results

                if has_onclick_attr:
                    LOG.info(
                        "The element has onclick attribute, waiting for 1 second to load new elements", action=action
                    )
                    await skyvern_frame.safe_wait_for_animation_end(before_wait_sec=1, caller="click.onclick")

                if false_click_download_observed is not None:
                    # Browser.downloadWillBegin can arrive on a later loop turn, just after the
                    # click await resolves; give the real probe event a narrow admission window
                    # before paying for the sequential rescrape.
                    try:
                        await asyncio.wait_for(
                            false_click_download_observed.wait(),
                            timeout=BROWSER_DOWNLOAD_EVENT_ADMISSION_GRACE_SECONDS,
                        )
                    except asyncio.TimeoutError:
                        pass
                    if false_click_download_observed.is_set():
                        LOG.info(
                            "Late same-action download observed for a file-download click; bypassing dropdown rescrape",
                            element_id=skyvern_element.get_id(),
                        )
                        return results

                if sequential_click_result := await handle_sequential_click_with_submit_bypass(
                    action=action,
                    action_history=results,
                    anchor_element=skyvern_element,
                    dom=dom,
                    page=page,
                    skyvern_frame=skyvern_frame,
                    scraped_page=scraped_page,
                    incremental_scraped=incremental_scraped,
                    task=task,
                    step=step,
                ):
                    results.append(sequential_click_result)
                    return results

            except NoAvailableOptionFoundForCustomSelection as exc:
                failure = ActionFailure(exc)
                failure.skip_remaining_actions = True
                results.append(failure)
                return results
            except Exception:
                LOG.warning(
                    "Failed to do sequential logic for the click action, skipping",
                    exc_info=True,
                    element_id=skyvern_element.get_id(),
                )
                return results

        finally:
            if remove_download_probe is not None:
                remove_download_probe()
            if incremental_scraped:
                try:
                    await incremental_scraped.stop_listen_dom_increment()
                except Exception:
                    LOG.warning(
                        "stop_listen_dom_increment failed after click, ignoring",
                        exc_info=True,
                        element_id=skyvern_element.get_id(),
                    )

    return results


async def _build_after_click_verify_prompt(
    task: Task,
    scraped_page_after_open: ScrapedPage,
    new_element_ids: set[str],
    action_history_str: str,
) -> str:
    # SKY-9718 Layer 1: sequential-click after-dropdown verifier path. Keep
    # Skyvern IDs (default html_need_skyvern_attrs=True) because
    # `new_elements_ids` is threaded and the LLM compares those IDs to what's
    # rendered. Gate lean on the PostHog flag.
    _ctx = skyvern_context.current()
    lean_enabled = bool(_ctx and _ctx.enable_lean_element_tree)
    slim_output = await get_slim_output_template_value("check-user-goal")
    # SKY-11295: verify against the mini goal when the task goal is
    # MINI_GOAL_TEMPLATE-wrapped; see ForgeAgent.complete_verify. Only
    # navigation_goal is unwrapped — this render passes no criteria fields.
    unwrapped_goals = unwrap_goal_fields(task.navigation_goal)
    return load_prompt_with_elements(
        element_tree_builder=scraped_page_after_open,
        prompt_engine=prompt_engine,
        template_name="check-user-goal",
        navigation_goal=unwrapped_goals.navigation_goal,
        big_goal_context=unwrapped_goals.big_goal_context,
        navigation_payload=task.navigation_payload,
        new_elements_ids=new_element_ids,
        without_screenshots=True,
        # No action_history_evidence: this call site judges mid-action continuation, and the
        # history here is the menu-opening click — evidence-shortcutting it would certify
        # the dropdown before the actual selection.
        action_history=action_history_str,
        slim_output=slim_output,
        local_datetime=datetime.now(skyvern_context.ensure_context().tz_info).isoformat(),
        lean_compress_long_href=lean_enabled,
        lean_compress_image_src=lean_enabled,
        lean_strip_url_query_strings=lean_enabled,
        lean_compress_nonnavigable_href=lean_enabled,
    )


async def handle_sequential_click_with_submit_bypass(
    action: actions.ClickAction,
    action_history: list[ActionResult],
    anchor_element: SkyvernElement,
    dom: DomUtil,
    page: Page,
    skyvern_frame: SkyvernFrame,
    scraped_page: ScrapedPage,
    incremental_scraped: IncrementalScrapePage,
    task: Task,
    step: Step,
) -> ActionResult | None:
    """Skip only the dropdown-specific post-click full rescrape for exact explicit
    submit controls; every other click still runs ``handle_sequential_click_for_dropdown``.

    ``anchor_element`` is the final click target after any disabled-wrapper
    retargeting, so the explicit-submit check is evaluated here and never stale.
    """
    if await anchor_element.is_explicit_submit():
        LOG.info(
            "Explicit submit click; bypassing the dropdown sequential-click rescrape",
            element_id=anchor_element.get_id(),
        )
        return None

    return await handle_sequential_click_for_dropdown(
        action=action,
        action_history=action_history,
        anchor_element=anchor_element,
        dom=dom,
        page=page,
        skyvern_frame=skyvern_frame,
        scraped_page=scraped_page,
        incremental_scraped=incremental_scraped,
        task=task,
        step=step,
    )


@traced(name="skyvern.agent.action.click_dropdown_sequential")
async def handle_sequential_click_for_dropdown(
    action: actions.ClickAction,
    action_history: list[ActionResult],
    anchor_element: SkyvernElement,
    dom: DomUtil,
    page: Page,
    skyvern_frame: SkyvernFrame,
    scraped_page: ScrapedPage,
    incremental_scraped: IncrementalScrapePage,
    task: Task,
    step: Step,
) -> ActionResult | None:
    if await incremental_scraped.get_incremental_elements_num() == 0:
        return None

    await skyvern_frame.safe_wait_for_animation_end(caller="click.dropdown")
    if page.url != scraped_page.url:
        LOG.info("Page URL changed after clicking, exiting the sequential click logic")
        return None

    incremental_elements = await incremental_scraped.get_incremental_element_tree(
        clean_and_remove_element_tree_factory(
            task=task,
            step=step,
            check_filter_funcs=[check_existed_but_not_option_element_in_dom_factory(dom)],
            engine_selection=skyvern_frame.engine_selection,
        ),
    )

    if len(incremental_elements) == 0:
        return None

    LOG.info("Detected new element after clicking", action=action, sampling=True)
    scraped_page_after_open = await scraped_page.generate_scraped_page_without_screenshots()
    new_element_ids = set(scraped_page_after_open.id_to_css_dict.keys()) - set(scraped_page.id_to_css_dict.keys())

    dom_after_open = DomUtil(scraped_page=scraped_page_after_open, page=page)
    new_interactable_element_ids = [
        element_id
        for element_id in new_element_ids
        if (await dom_after_open.get_skyvern_element_by_id(element_id)).is_interactable()
    ]

    if len(new_interactable_element_ids) == 0:
        LOG.info("No new interactable elements found, exiting the sequential click logic")
        return None

    # Settle what the click revealed before spending an LLM call on it. This walk is DOM-only, and
    # a click that opened no menu returns below either way -- so running the goal check first could
    # not change that outcome, only the bill. The check itself is unchanged for menus that do open:
    # exiting the sequential click logic on an achieved goal is the reason it exists (#5599).
    dropdown_menu_element = await locate_dropdown_menu(
        current_anchor_element=anchor_element,
        incremental_scraped=incremental_scraped,
        step=step,
        task=task,
    )

    if dropdown_menu_element is None:
        return None

    action_history_str = ""
    if action_history and len(action_history) > 0:
        result = action_history[-1]
        action_result = {
            "action_type": action.action_type,
            "reasoning": action.reasoning,
            "result": result.success,
        }
        action_history_str = json.dumps(action_result)

    prompt = await _build_after_click_verify_prompt(task, scraped_page_after_open, new_element_ids, action_history_str)
    distinct_id_for_override = task.workflow_run_id if task.workflow_run_id else task.task_id
    check_user_goal_handler = await resolve_check_user_goal_handler(
        distinct_id_for_override,
        task.organization_id,
        get_org_aware_secondary_llm_api_handler(default=app.CHECK_USER_GOAL_LLM_API_HANDLER),
    )
    response = await check_user_goal_handler(
        prompt=prompt,
        step=step,
        prompt_name="check-user-goal-after-click",
    )
    verify_result = CompleteVerifyResult.model_validate(response)
    if verify_result.user_goal_achieved:
        LOG.info("User goal achieved, exiting the sequential click logic")
        return None

    dropdown_select_context = await _get_input_or_select_context(
        action=AbstractActionForContextParse(
            reasoning=action.reasoning, intention=action.intention, element_id=action.element_id
        ),
        skyvern_element=anchor_element,
        element_tree_builder=scraped_page,
        task=task,
        step=step,
        engine_selection=skyvern_frame.engine_selection,
    )

    if dropdown_select_context.is_date_related:
        LOG.info(
            "The dropdown is date related, exiting the sequential click logic and skipping the remaining actions",
        )
        result = ActionSuccess()
        result.skip_remaining_actions = True
        return result

    LOG.info(
        "Found the dropdown menu element after clicking, triggering the sequential click logic",
        element_id=dropdown_menu_element.get_id(),
    )

    return await select_from_emerging_elements(
        current_element_id=anchor_element.get_id(),
        options=CustomSelectPromptOptions(
            field_information=dropdown_select_context.intention
            if dropdown_select_context.intention
            else dropdown_select_context.field,
            is_date_related=dropdown_select_context.is_date_related,
            required_field=dropdown_select_context.is_required,
        ),
        page=page,
        scraped_page=scraped_page,
        step=step,
        task=task,
        engine_selection=skyvern_frame.engine_selection,
        entry_action_type="click",
        scraped_page_after_open=scraped_page_after_open,
        new_interactable_element_ids=new_interactable_element_ids,
    )


@traced(name="skyvern.agent.action.click_to_download")
async def handle_click_to_download_file_action(
    action: actions.ClickAction,
    page: Page,
    scraped_page: ScrapedPage,
    task: Task,
    step: Step,
) -> list[ActionResult]:
    dom = DomUtil(scraped_page=scraped_page, page=page)
    skyvern_element = await dom.get_skyvern_element_by_id(action.element_id)

    results = await chain_click(task, scraped_page, page, action, skyvern_element)
    try:
        await page.wait_for_load_state(timeout=settings.BROWSER_LOADING_TIMEOUT_MS)
    except Exception:
        LOG.warning(
            "wait_for_load_state timed out after download click",
            action=action,
            workflow_run_id=task.workflow_run_id,
        )
    return results


# TOTP timing constants
TOTP_EXPIRY_THRESHOLD_SECONDS = 20


async def _handle_multi_field_totp_sequence(
    timing_info: dict[str, Any],
    task: Task,
) -> list[ActionResult] | None:
    """
    Handle TOTP generation and caching for multi-field TOTP sequences.

    Returns:
        ActionFailure if TOTP handling failed, None if successful
    """
    action_index = timing_info["action_index"]
    cache_key = f"{task.task_id}_totp_cache"
    valid_from_key = f"{cache_key}_valid_from"
    valid_until_key = f"{cache_key}_valid_until"
    current_context = skyvern_context.ensure_context()

    if action_index == 0:
        # First digit: generate TOTP and cache it
        totp_secret = timing_info["totp_secret"]
        totp = parse_totp_config(totp_secret)
        if not totp:
            raise ValueError("Invalid TOTP secret or otpauth URI")

        # Check current TOTP expiry time
        current_time = int(time.time())
        current_totp_valid_until = ((current_time // totp.interval) + 1) * totp.interval
        seconds_until_expiry = current_totp_valid_until - current_time

        # If less than threshold seconds until expiry, use the next TOTP
        if seconds_until_expiry < TOTP_EXPIRY_THRESHOLD_SECONDS:
            # Force generation of next TOTP by advancing time
            totp_valid_from = current_totp_valid_until
            totp_valid_until = current_totp_valid_until + totp.interval
            current_totp = totp.at(totp_valid_from)

            LOG.debug(
                "Using multi-field TOTP flow - using NEXT TOTP due to <20s expiry",
                action_idx=action_index,
                current_totp=totp.now(),
                next_totp=current_totp,
                seconds_until_expiry=seconds_until_expiry,
                is_retry=timing_info.get("is_retry", False),
            )
        else:
            # Use current TOTP
            totp_valid_from = current_totp_valid_until - totp.interval
            totp_valid_until = current_totp_valid_until
            current_totp = totp.now()

        current_context.totp_codes[cache_key] = current_totp
        current_context.totp_codes[valid_from_key] = str(totp_valid_from)
        current_context.totp_codes[valid_until_key] = str(totp_valid_until)
    else:
        # Subsequent digits: reuse cached TOTP
        current_totp = current_context.totp_codes.get(cache_key)
        if not current_totp:
            # TOTP cache missing for subsequent digit - this should not happen
            # If it does, something went wrong with the first digit, so fail the action
            LOG.error(
                "TOTP cache missing for subsequent digit - first digit may have failed",
                action_idx=action_index,
                cache_key=cache_key,
            )
            return [ActionFailure(TOTPExpiredError())]

        # Check if cached TOTP has expired
        totp_secret = timing_info["totp_secret"]
        totp = parse_totp_config(totp_secret)
        if not totp:
            raise ValueError("Invalid TOTP secret or otpauth URI")

        cached_valid_from = current_context.totp_codes.get(valid_from_key)
        cached_valid_until = current_context.totp_codes.get(valid_until_key)
        if not cached_valid_from or not cached_valid_until:
            LOG.error(
                "TOTP cache metadata missing for subsequent digit",
                action_idx=action_index,
                cache_key=cache_key,
            )
            return [ActionFailure(TOTPExpiredError())]

        try:
            totp_valid_from = int(cached_valid_from)
            totp_valid_until = int(cached_valid_until)
        except ValueError:
            LOG.error(
                "TOTP cache metadata invalid for subsequent digit",
                action_idx=action_index,
                cache_key=cache_key,
                cached_valid_from=cached_valid_from,
                cached_valid_until=cached_valid_until,
            )
            return [ActionFailure(TOTPExpiredError())]

        # Get current time and check against the cached TOTP window.
        current_time = int(time.time())

        if current_time >= totp_valid_until:
            LOG.error(
                "Cached TOTP has expired during multi-field sequence",
                action_idx=action_index,
                current_time=current_time,
                totp_valid_until=totp_valid_until,
                cached_totp=current_totp,
            )
            return [ActionFailure(TOTPExpiredError())]

        LOG.debug(
            "Using multi-field TOTP flow - reusing cached TOTP",
            action_idx=action_index,
            totp=current_totp,
            current_time=current_time,
            totp_valid_until=totp_valid_until,
        )

    # Special handling for the 6th digit (action_index=5): wait if TOTP is not yet valid
    if action_index == 5:
        if current_time < totp_valid_from:
            # TOTP is not yet valid, wait until it becomes valid
            wait_seconds = totp_valid_from - current_time

            LOG.debug(
                "6th digit: TOTP not yet valid, waiting until valid_from",
                action_idx=action_index,
                current_time=current_time,
                totp_valid_from=totp_valid_from,
                wait_seconds=wait_seconds,
                totp=current_totp,
            )

            await _totp_window_sleep(wait_seconds)

            LOG.debug(
                "6th digit: Finished waiting, TOTP is now valid",
                action_idx=action_index,
            )

    return None  # Success


def _normalize_dropdown_match_text(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]+", "", value).lower()


def _custom_select_node_is_disabled(attributes: dict) -> bool:
    # Reuse the canonical static disabled convention (disabled / aria-disabled, any non-"false" value is disabled)
    # so an empty-state placeholder rendered as an aria-disabled option cannot admit the force-select option gate.
    # An enabled option (no disabled attribute, or aria-disabled="false") stays eligible.
    return SkyvernElement._disabled_attrs_indicate_disabled(attributes)


def _incremental_tree_contains_option_with_target_value(elements: list[dict], target_value: str) -> bool:
    # Match the target only against real option candidates (what the selector would click) by their label,
    # so a "No results for <target>" banner cannot admit a selection.
    normalized_target = _normalize_dropdown_match_text(target_value)
    if not normalized_target:
        return False
    for candidate in _custom_select_candidates_from_elements(elements):
        label = candidate.get("label")
        if isinstance(label, str) and normalized_target in _normalize_dropdown_match_text(label):
            return True
    return False


def _option_subtree_text(node: dict) -> str:
    # A large search combobox renders an option's value in nested spans, so the row's text is not on the
    # option node itself; gather the node's own text plus all descendants' in document order (so a value
    # split across sibling text nodes, e.g. <b>12345</b><b>67890</b>, is not scrambled).
    parts: list[str] = []
    stack: list[dict] = [node]
    while stack:
        current = stack.pop()
        if not isinstance(current, dict):
            continue
        text = current.get("text")
        if isinstance(text, str) and text.strip():
            parts.append(text)
        children = current.get("children")
        if isinstance(children, list):
            stack.extend(reversed(children))
    return " ".join(parts)


def _incremental_tree_contains_option_subtree_with_target_value(elements: list[dict], target_value: str) -> bool:
    # Gate = selector's label-candidate match OR an option/choice subtree-text extension. The first arm
    # delegates to _custom_select_candidates_from_elements (via the label matcher), so the gate is a superset
    # of every family the selector can commit -- role/tag/<li>-in-choice-surface options, clickable choices,
    # checkbox/radio inputs, <label>-wrapped inputs, and attribute-only (aria-label/title/value) labels -- by
    # construction. The second arm adds only what a label match genuinely misses: an option whose value
    # renders in nested spans (empty own text), gated on option/choice nodes so a status/banner echo stays
    # excluded, and reading its subtree text in document order.
    normalized_target = _normalize_dropdown_match_text(target_value)
    if not normalized_target:
        return False
    if _incremental_tree_contains_option_with_target_value(elements, target_value):
        return True
    queue: deque[tuple[dict, bool, bool]] = deque((element, False, False) for element in elements)
    while queue:
        node, in_choice_surface, in_disabled_subtree = queue.popleft()
        if not isinstance(node, dict):
            continue
        attributes = node.get("attributes") or {}
        role = str(attributes.get("role") or "").lower()
        tag = str(node.get("tagName") or "").lower()
        label = _select_shadow_label_from_node(node) or _custom_select_choice_value(node)
        is_option_node = role in _CUSTOM_SELECT_CHOICE_ROLES or tag == "option" or (tag == "li" and in_choice_surface)
        has_choice_state = "aria-selected" in attributes or "aria-checked" in attributes
        is_clickable_choice = (
            bool(node.get("interactable"))
            and bool(label)
            and role not in _CUSTOM_SELECT_CONTAINER_ROLES
            and tag not in {"input", "select", "textarea"}
            and not (tag == "a" and bool(attributes.get("href")))
            and (in_choice_surface or has_choice_state)
        )
        # A disabled ancestor (aria-disabled=true, disabled fieldset/wrapper) disables the whole subtree; a
        # descendant aria-disabled=false cannot re-enable it, so inheritance is monotonic (OR, never reset).
        node_in_disabled_subtree = in_disabled_subtree or _custom_select_node_is_disabled(attributes)
        if (
            (is_option_node or is_clickable_choice)
            and not node_in_disabled_subtree
            and normalized_target in _normalize_dropdown_match_text(_option_subtree_text(node))
        ):
            return True
        child_in_choice_surface = in_choice_surface or _is_custom_select_choice_surface(role)
        for child in node.get("children") or []:
            queue.append((child, child_in_choice_surface, node_in_disabled_subtree))
    return False


def _incremental_tree_has_enabled_selectable_option(elements: list[dict]) -> bool:
    # An "enabled selectable option candidate" is exactly what the selector could click:
    # _custom_select_candidates_from_elements already excludes disabled options and disabled subtrees, so a
    # non-empty candidate list means the dropdown is already populated -- the deferred-empty render race is
    # over. A disabled-only snapshot yields no candidate, so it stays settle-eligible.
    return bool(_custom_select_candidates_from_elements(elements))


def _attr_indicates_aria_invalid(raw: object) -> bool:
    # Compare as a normalized string so a literal False (bool or "false") reads as valid, not truthy;
    # aria-invalid is "true"/"grammar"/"spelling" when rejected, "false"/absent when accepted.
    if raw is None:
        return False
    return str(raw).strip().casefold() not in ("", "false")


def _has_exact_class_token(class_attr: str | None, token: str) -> bool:
    return class_attr is not None and token in str(class_attr).split()


# Owner-scoped ui-select state read before and after Enter (`owned` reachable for a ui-select nested in another's
# dropdown). Disabled = stock 0.19.8 forms only: `disabled` attr/class, `select2-disabled`, non-"false" `aria-disabled`.
_UI_SELECT_STATE_JS = """
(el) => {
  const container = el.closest('.ui-select-container');
  if (container === null) { return null; }
  const owned = (n) => n.closest('.ui-select-container') === container;
  const rows = [...container.querySelectorAll('.ui-select-choices-row')].filter((r) => owned(r) && r.getClientRects().length > 0);
  const isDisabled = (r) => r.hasAttribute('disabled') || r.classList.contains('disabled') || r.classList.contains('select2-disabled') || (r.hasAttribute('aria-disabled') && (r.getAttribute('aria-disabled') || '').trim().toLowerCase() !== 'false');
  const matches = [...container.querySelectorAll('.ui-select-match-text, .select2-chosen, .ui-select-match-item, .ui-select-match')].filter((m) => owned(m) && m.getClientRects().length > 0).slice(0, 20).map((m) => (m.textContent || '').trim());
  return { enabledRowCount: rows.filter((r) => !isDisabled(r)).length, firstVisibleEnabled: rows.length > 0 && !isDisabled(rows[0]), firstVisibleLabel: rows.length > 0 ? (rows[0].textContent || '').trim() : '', choicesOpen: rows.length > 0 || [...container.querySelectorAll('.ui-select-choices')].some((c) => owned(c) && c.getClientRects().length > 0), matchTexts: matches, searchValue: typeof el.value === 'string' ? el.value : '' };
}
"""


def _ui_select_commit_result(
    action: InputTextAction,
    pre: dict,
    post: Any,
    candidate: str,
    text: str,
) -> ActionResult | None:
    """Adjudicate a ui-select Enter from the same-owner post-settle state: ``ActionSuccess`` (with evidence) only
    on a proven commit (choices closed + search emptied + a match display for the candidate); ``None`` on a proven
    clean no-op (byte-identical to pre-Enter) → fall through; else ``NoAvailableOptionFoundForCustomSelection``."""
    if isinstance(post, dict) and not post.get("choicesOpen") and post.get("searchValue") == "":
        normalized_candidate = _normalize_select_shadow_text(candidate)
        for observed in post.get("matchTexts") or []:
            if normalized_candidate and _normalize_select_shadow_text(observed) == normalized_candidate:
                result = ActionSuccess(
                    committed_option=_truncate_select_shadow_field(candidate),
                    committed_value=_truncate_select_shadow_field(str(observed)),
                )
                action.set_has_mini_agent()
                if action.stop_batch_after_dropdown_select:
                    result.skip_remaining_actions = True
                return result
    if (
        isinstance(post, dict)
        and post.get("choicesOpen")
        and post.get("searchValue") == text
        and post.get("matchTexts") == pre.get("matchTexts")
    ):
        return None
    return ActionFailure(
        NoAvailableOptionFoundForCustomSelection(
            reason="ui-select Enter commit could not be verified", target_value=candidate
        )
    )


async def _is_combobox_or_typeahead(skyvern_element: SkyvernElement) -> bool:
    # role=combobox or aria-autocomplete list/both/inline marks a control whose options surface only as characters
    # are entered. This structural identity -- not the post-input aria-invalid state -- decides whether the
    # per-character seam must be kept, so a combobox that is valid on load still enters per character, never
    # filled atomically (which would emit no key events and surface no option) (SKY-13821).
    role = await skyvern_element.get_attr("role")
    aria_autocomplete = await skyvern_element.get_attr("aria-autocomplete")
    return str(role or "").strip().casefold() == "combobox" or str(aria_autocomplete or "").strip().casefold() in (
        "list",
        "both",
        "inline",
    )


async def _is_commit_required_combobox(skyvern_element: SkyvernElement) -> bool:
    if not await _is_combobox_or_typeahead(skyvern_element):
        return False
    # aria-invalid is read live (dynamic) because it reflects post-input state, not the pre-input scrape.
    aria_invalid = await skyvern_element.get_attr("aria-invalid", mode="dynamic")
    return _attr_indicates_aria_invalid(aria_invalid)


async def _retarget_wrapper_for_input_text(
    dom: DomUtil,
    skyvern_element: SkyvernElement,
    action: actions.InputTextAction,
) -> SkyvernElement | None:
    # Normalize a secure card-entry wrapper (<div>/<iframe>) to its single unambiguous nested <input>
    # so the pipeline below (including wait_until_enabled) runs against the real field. include_disabled
    # keeps a transiently-disabled unique input as the target -- the downstream enabled-wait then waits on
    # it. Sibling/decoy inputs make the helper bail, so card data can never land in a CVV or decoy field.
    child_id = skyvern_element.find_deepest_interactable_descendant_in_single_chain(include_disabled=True)
    if not child_id:
        return None
    child_element = await dom.safe_get_skyvern_element_by_id(child_id)
    if child_element is None:
        return None
    if await child_element.has_hidden_attr():
        return None
    # This path can carry card data; has_hidden_attr only reads attributes, so a candidate that went
    # CSS-hidden (display:none/visibility:hidden) between scrape and action would slip through. Fail
    # closed on rendered visibility before committing the retarget.
    if not await child_element.is_visible():
        return None
    if not await child_element.supports_text_input():
        return None
    LOG.info(
        "Re-targeting input_text from wrapper to nested interactable input",
        parent_id=skyvern_element.get_id(),
        child_id=child_id,
    )
    action.element_id = child_id
    return child_element


def _emit_tel_input_outcome(
    action: actions.Action,
    results: list[ActionResult],
    *,
    exception_type: str | None = None,
) -> None:
    outcome = action.tel_input_outcome
    if outcome is None:
        return

    try:
        with contained_effect("emit tel input outcome"):
            final_result = results[-1] if results else None
            if isinstance(final_result, ActionSuccess):
                terminal_result = ActionStatus.completed.value
            elif isinstance(final_result, ActionAbort):
                terminal_result = ActionStatus.skipped.value
            else:
                terminal_result = ActionStatus.failed.value
            if exception_type is None and final_result is not None:
                exception_type = final_result.exception_type
            LOG.info(
                "tel_input_outcome",
                sampling=False,
                **outcome.model_dump(mode="json"),
                terminal_result=terminal_result,
                exception_type=exception_type,
            )
    finally:
        action.tel_input_outcome = None


@traced(name="skyvern.agent.action.input_text")
async def handle_input_text_action(
    action: actions.InputTextAction,
    page: Page,
    scraped_page: ScrapedPage,
    task: Task,
    step: Step,
) -> list[ActionResult]:
    action.tel_input_outcome = None
    return await _handle_input_text_action(
        action=action,
        page=page,
        scraped_page=scraped_page,
        task=task,
        step=step,
    )


async def handle_input_text_action_direct(
    action: actions.InputTextAction,
    page: Page,
    scraped_page: ScrapedPage,
    task: Task,
    step: Step,
) -> list[ActionResult]:
    try:
        results = await handle_input_text_action(action, page, scraped_page, task, step)
    except Exception as exc:
        _emit_tel_input_outcome(action, [], exception_type=type(exc).__name__)
        raise
    _emit_tel_input_outcome(action, results)
    return results


async def _handle_input_text_action(
    action: actions.InputTextAction,
    page: Page,
    scraped_page: ScrapedPage,
    task: Task,
    step: Step,
) -> list[ActionResult]:
    initial_action_target_id = action.element_id
    if not action.element_id:
        # This is a CUA type action
        text_result = get_actual_value_of_parameter_if_secret_with_task(task, action.text)
        if text_result is None:
            return [ActionFailure(FailedToFetchSecret())]
        if is_unresolved_totp_placeholder(text_result):
            return [ActionFailure(NoTOTPSecretFound())]
        cua_text = text_result
        if cua_text in (BitwardenConstants.TOTP, OnePasswordConstants.TOTP, AzureVaultConstants.TOTP):
            try:
                cua_totp_secret = get_totp_secret_with_task(task=task, parameter=action.text)
                cua_text = generate_totp_value_from_secret(cua_totp_secret)
            except NoTOTPSecretFound as exc:
                return [ActionFailure(exc)]
            _register_runtime_otp_value_best_effort(task.workflow_run_id, cua_text)
        elif is_unresolved_totp_value(cua_text):
            return [ActionFailure(NoTOTPSecretFound())]
        await _apply_active_element_secret_visual_mask_if_needed(page, cua_text, task.workflow_run_id)
        await EventStrategyFactory.type_text(page, None, cua_text)
        return [ActionSuccess()]

    totp_secret: str | None = None
    is_multi_field_totp = bool(action.totp_timing_info and action.totp_timing_info.get("is_totp_sequence"))
    if is_multi_field_totp:
        text = ""
        current_text_target = action.text
        is_totp_value = False
        is_secret_value = True
    else:
        text_result = get_actual_value_of_parameter_if_secret_with_task(task, action.text)
        if text_result is None:
            return [ActionFailure(FailedToFetchSecret())]
        if is_unresolved_totp_placeholder(text_result):
            return [ActionFailure(NoTOTPSecretFound())]
        is_totp_value = text_result in (
            BitwardenConstants.TOTP,
            OnePasswordConstants.TOTP,
            AzureVaultConstants.TOTP,
        )
        if not is_totp_value and is_unresolved_totp_value(text_result):
            return [ActionFailure(NoTOTPSecretFound())]
        if is_totp_value:
            try:
                totp_secret = get_totp_secret_with_task(task=task, parameter=action.text)
            except NoTOTPSecretFound as exc:
                return [ActionFailure(exc)]
            text = ""
        else:
            text = text_result
        current_text_target = text_result
        is_secret_value = is_totp_value or text != action.text

    dom = DomUtil(scraped_page, page)
    skyvern_element = await dom.get_skyvern_element_by_id(action.element_id)

    # Normalize a wrapper target -- a visible <div>/<iframe> whose real editable <input> is nested one
    # frame deeper (e.g. cross-origin card-entry widgets) -- to that input so the pipeline below runs
    # against the real field. Selectable targets are left for the select-option conversion below.
    # Fail-closed: sibling/decoy descendants make the single-chain helper bail, so card data can never
    # land in a CVV or anti-autofill decoy field.
    can_input_text = await skyvern_element.supports_text_input()
    # Cache the wrapper's dynamic hidden state read for the retarget gate; the reject block below reuses it
    # (the target is unchanged when no retarget occurs) instead of a second dynamic get_attribute round-trip.
    target_hidden: bool | None = None
    if not can_input_text:
        target_hidden = await skyvern_element.has_hidden_attr()
        if not target_hidden and not await skyvern_element.get_selectable():
            retargeted_element = await _retarget_wrapper_for_input_text(dom, skyvern_element, action)
            if retargeted_element is not None:
                skyvern_element = retargeted_element
                can_input_text = True

    engine_selection = resolve_engine_selection_for_task(task, app.BROWSER_MANAGER)
    skyvern_frame = await SkyvernFrame.create_instance(skyvern_element.get_frame(), engine_selection=engine_selection)
    incremental_scraped = IncrementalScrapePage(skyvern_frame=skyvern_frame, engine_selection=engine_selection)
    timeout = settings.BROWSER_ACTION_TIMEOUT_MS
    tag_name = scraped_page.id_to_element_dict[action.element_id]["tagName"].lower()
    is_tel = await skyvern_element.get_attr("type") == "tel"
    candidate_card_digits = _card_number_digits(text)
    is_card_number_input = _is_probable_card_number(candidate_card_digits) and await _is_card_number_field(
        skyvern_element
    )
    phone_bearing = is_tel and not is_card_number_input
    tel_fix_enabled: bool | None = None
    tel_source_text: str | None = None
    tel_outcome = action.tel_input_outcome
    if is_tel and not is_card_number_input:
        tel_source_text = text
        tel_fix_enabled = await _is_tel_digit_fix_enabled(task)
        tel_outcome = actions.TelInputOutcome(
            flag_enabled=tel_fix_enabled,
            final_element_id=skyvern_element.get_id(),
            strategy=actions.TelInputStrategy.legacy_sequential,
            expected_digit_count=len(_phone_digits(tel_source_text)),
            attempt_count=0,
            retargeted=skyvern_element.get_id() != initial_action_target_id,
        )
        action.tel_input_outcome = tel_outcome

    try:
        current_text = await get_input_value(
            skyvern_element.get_tag_name(), skyvern_element.get_locator(), engine_selection=engine_selection
        )
    except Exception as exc:
        if phone_bearing:
            LOG.warning("Phone input read-back failed", error_type=type(exc).__name__)
            return [ActionFailure(PhoneNumberInputBrowserInteractionFailed())]
        raise
    if not is_totp_value and current_text == current_text_target:
        if tel_outcome is not None:
            tel_outcome.actual_digit_count = len(_phone_digits(current_text))
            tel_outcome.browser_valid = await _probe_tel_browser_validity(skyvern_element.get_locator())
            if tel_outcome.flag_enabled and tel_outcome.browser_valid is False:
                pattern = await skyvern_element.get_attr("pattern")
                _, eligible_bare_nanp, _ = _plan_tel_text(
                    is_tel=True,
                    is_secret=is_secret_value,
                    value=tel_source_text or text,
                    pattern=pattern,
                )
                if eligible_bare_nanp:
                    return [ActionFailure(PhoneNumberInputBrowserValidityMismatch())]
        return [ActionSuccess()]

    # dynamically validate the attr, since it could change into enabled after the previous actions
    if not await SkyvernElement.wait_until_enabled(skyvern_element):
        LOG.warning(
            "Try to input text on a disabled element",
            action_type=action.action_type,
            element_id=skyvern_element.get_id(),
        )
        return [ActionFailure(InteractWithDisabledElement(skyvern_element.get_id()))]

    if await skyvern_element.get_selectable():
        select_text = text
        if is_totp_value:
            try:
                select_text = generate_totp_value_from_secret(totp_secret)
            except NoTOTPSecretFound as exc:
                return [ActionFailure(exc)]
            _register_runtime_otp_value_best_effort(task.workflow_run_id, select_text)
        select_action = SelectOptionAction(
            reasoning=action.reasoning,
            element_id=skyvern_element.get_id(),
            option=SelectOption(label=select_text),
            intention=action.intention,
            input_or_select_context=action.input_or_select_context,
        )
        LOG.info(
            "Input element is selectable, doing select actions",
            element_id=skyvern_element.get_id(),
            action=action,
        )
        action.set_has_mini_agent()
        return await handle_select_option_action(
            select_action,
            page,
            scraped_page,
            task,
            step,
            entry_action_type="input_text_converted",
        )

    select_action = SelectOptionAction(
        reasoning=action.reasoning,
        element_id=skyvern_element.get_id(),
        option=SelectOption(label=text),
        intention=action.intention,
        input_or_select_context=action.input_or_select_context,
    )

    incremental_element: list[dict] = []
    auto_complete_hacky_flag: bool = False
    # Set when the typeahead prefilter (below) types the target into the field. If that pre-input
    # selection does not commit, the terminal fill must clear first so the typed-but-uncommitted value
    # is not doubled. `prefilter_attempted` stays set even when the typing raises mid-dispatch (leaving a
    # dirty prefix), so the terminal clear still fires on the ArrowDown fall-through path.
    prefilter_typeahead: bool = False
    prefilter_attempted: bool = False

    input_or_select_context = await _get_input_or_select_context(
        action=action,
        element_tree_builder=scraped_page,
        skyvern_element=skyvern_element,
        task=task,
        step=step,
        engine_selection=engine_selection,
    )
    if not can_input_text:
        target_is_hidden = target_hidden if target_hidden is not None else await skyvern_element.has_hidden_attr()
        if target_is_hidden:
            return [ActionFailure(InputToInvisibleElement(skyvern_element.get_id()), stop_execution_on_failure=False)]

        is_date_related = input_or_select_context is not None and input_or_select_context.is_date_related is True
        LOG.warning(
            "Target element does not support text input, rejecting input text action",
            action_type=action.action_type,
            element_id=skyvern_element.get_id(),
            tag_name=tag_name,
            is_date_related=is_date_related,
        )
        return [
            ActionFailure(
                InvalidElementForTextInput(
                    element_id=action.element_id,
                    tag_name=tag_name,
                    is_date_related=is_date_related,
                )
            )
        ]

    # ui-select (AngularJS) resets activeIndex to the first visible row per keystroke, so Enter commits rows[0].
    # Press it only when that first visible row is the unique enabled one, then prove the commit landed before
    # recording it (see _ui_select_commit_result). Run before the generic probe.
    if (
        text
        and tag_name == InteractiveElement.INPUT
        and not is_secret_value
        and not is_totp_value
        and (input_or_select_context is None or input_or_select_context.is_date_related is not True)
        and _has_exact_class_token(await skyvern_element.get_attr("class"), "ui-select-search")
    ):
        await incremental_scraped.start_listen_dom_increment(await skyvern_element.get_element_handler())
        try:
            try:
                await skyvern_element.input_clear()
            except Exception:
                LOG.info(
                    "Failed to clear ui-select search before filtering; failing closed",
                    element_id=skyvern_element.get_id(),
                )
                return [ActionFailure(FailedToClearInputField(element_id=action.element_id, tag_name=tag_name))]
            pre: Any = None
            try:
                await skyvern_element.input_sequentially(text=text)
                await skyvern_frame.safe_wait_for_animation_end(caller="input_text.ui_select")
                if await get_input_value(tag_name, skyvern_element.get_locator()) == text:
                    pre = await _evaluate_element_scoped(skyvern_element, _UI_SELECT_STATE_JS)
            except Exception:
                LOG.info("Failed to filter/probe ui-select rows, falling back", element_id=skyvern_element.get_id())
            if (
                isinstance(pre, dict)
                and pre.get("enabledRowCount") == 1
                and pre.get("firstVisibleEnabled")
                and pre.get("firstVisibleLabel")
            ):
                candidate = str(pre["firstVisibleLabel"])
                await skyvern_element.press_key("Enter")
                post: Any = None
                try:
                    await _wait_custom_select_render_settle(skyvern_element)
                    post = await _evaluate_element_scoped(skyvern_element, _UI_SELECT_STATE_JS)
                except Exception:
                    LOG.info("Failed to read ui-select state after Enter", element_id=skyvern_element.get_id())
                commit_result = _ui_select_commit_result(action, pre, post, candidate, text)
                if commit_result is not None:
                    return [commit_result]  # None → proven clean no-op: fall through to the generic path
        finally:
            await incremental_scraped.stop_listen_dom_increment()
        # Not commit-ready or a proven no-op: clear the probe so the ordinary path does not double the typed value.
        try:
            await skyvern_element.input_clear()
        except Exception:
            LOG.warning("Failed to clear ui-select probe before fallthrough", element_id=skyvern_element.get_id())
            return [ActionFailure(FailedToClearInputField(element_id=action.element_id, tag_name=tag_name))]

    # check if it's selectable
    if (
        input_or_select_context is not None
        and not input_or_select_context.is_search_bar  # no need to to trigger selection logic for search bar
        and not is_totp_value
        and not is_secret_value
        and skyvern_element.get_tag_name() == InteractiveElement.INPUT
        and not await skyvern_element.is_raw_input()
    ):
        has_onclick_attr = await skyvern_element.has_attr("onclick", mode="static")
        await skyvern_element.scroll_into_view()
        # press arrowdown to watch if there's any options popping up
        await incremental_scraped.start_listen_dom_increment(await skyvern_element.get_element_handler())
        try:
            await skyvern_element.input_clear()
        except Exception:
            LOG.info(
                "Failed to clear up the input, but continue to input",
                element_id=skyvern_element.get_id(),
            )

        # When the deployment's Setup flagged this action (a wrapper-marked typeahead), type the target to
        # filter the listbox before matching instead of the unfiltered ArrowDown probe. Fall back to the
        # ArrowDown probe when the flag is off, the text is empty/date-related, or the prefilter typing fails.
        prefilter_typeahead = bool(text) and not input_or_select_context.is_date_related and action.prefilter_typeahead
        if prefilter_typeahead:
            # Mark before typing: a mid-dispatch failure can leave a dirty prefix, and the terminal clear
            # gate must still fire even after prefilter_typeahead is reset for the ArrowDown fall-through.
            prefilter_attempted = True
            try:
                await skyvern_element.input_sequentially(text)
            except Exception:
                LOG.info(
                    "Failed to pre-filter typeahead combobox by typing the target, falling back to ArrowDown probe",
                    element_id=skyvern_element.get_id(),
                )
                prefilter_typeahead = False

        if not prefilter_typeahead:
            try:
                await skyvern_element.press_key("ArrowDown")
            except Exception as exc:
                if not _is_selected_engine_timeout(exc, engine_selection):
                    raise
                # sometimes we notice `press_key()` raise a timeout but actually the dropdown is opened.
                LOG.info(
                    "Timeout to press ArrowDown to open dropdown, ignore the timeout and continue to execute the action",
                    element_id=skyvern_element.get_id(),
                    action=action,
                )

        wait_sec = 0
        if has_onclick_attr:
            LOG.info("The element has onclick attribute, waiting for 1 second to load new elements", action=action)
            wait_sec = 1

        await skyvern_frame.safe_wait_for_animation_end(before_wait_sec=wait_sec, caller="input_text.autocomplete")
        incremental_element = await incremental_scraped.get_incremental_element_tree(
            clean_and_remove_element_tree_factory(
                task=task,
                step=step,
                check_filter_funcs=[check_existed_but_not_option_element_in_dom_factory(dom)],
                engine_selection=engine_selection,
            ),
        )
        if len(incremental_element) == 0:
            LOG.info(
                "No new element detected, indicating it couldn't be a selectable auto-completion input",
                sampling=True,
                element_id=skyvern_element.get_id(),
                action=action,
            )
            await incremental_scraped.stop_listen_dom_increment()
        else:
            auto_complete_hacky_flag = True
            try_to_quit_dropdown = True
            try:
                # TODO: we don't select by value for the auto completion detect case
                action.set_has_mini_agent()

                select_result = await sequentially_select_from_dropdown(
                    action=select_action,
                    input_or_select_context=input_or_select_context,
                    page=page,
                    dom=dom,
                    skyvern_element=skyvern_element,
                    skyvern_frame=skyvern_frame,
                    incremental_scraped=incremental_scraped,
                    step=step,
                    task=task,
                    target_value=text,
                    entry_action_type="input_text",
                )

                if select_result is not None:
                    if select_result.action_result and select_result.action_result.success:
                        try_to_quit_dropdown = False
                        return [select_result.action_result]

                    if _is_terminal_custom_select_failure(select_result.action_result):
                        auto_complete_hacky_flag = False
                        return [select_result.action_result]

                    if select_result.dropdown_menu is None:
                        try_to_quit_dropdown = False

                    if select_result.action_result is None:
                        LOG.info(
                            "It might not be a selectable auto-completion input, exit the custom selection mode",
                            element_id=skyvern_element.get_id(),
                            action=action,
                        )
                    else:
                        LOG.warning(
                            "Custom selection returned an error, continue to input text",
                            element_id=skyvern_element.get_id(),
                            action=action,
                            err_msg=select_result.action_result.exception_message,
                        )

            except Exception:
                LOG.warning(
                    "Failed to do custom selection transformed from input action, continue to input text",
                    exc_info=True,
                )
                await skyvern_element.scroll_into_view()
            finally:
                if await skyvern_element.is_visible():
                    blocking_element, exist = await skyvern_element.find_blocking_element(
                        dom=dom, incremental_page=incremental_scraped
                    )
                    if blocking_element and exist:
                        LOG.info(
                            "Find a blocking element to the current element, going to blur the blocking element first",
                            blocking_element=blocking_element.get_locator(),
                        )
                        if await blocking_element.get_locator().count():
                            await blocking_element.press_key("Escape")
                        if await blocking_element.get_locator().count():
                            await blocking_element.blur()

                if try_to_quit_dropdown and await skyvern_element.is_visible():
                    await skyvern_element.press_key("Escape")
                    await skyvern_element.blur()
                await incremental_scraped.stop_listen_dom_increment()

    ### Start filling text logic
    # check if the element has hidden attribute
    if await skyvern_element.has_hidden_attr():
        return [ActionFailure(InputToInvisibleElement(skyvern_element.get_id()), stop_execution_on_failure=False)]

    # force to move focus back to the element
    await skyvern_element.get_locator().focus(timeout=timeout)

    # check if the element is readonly(some elements will be non-readonly after focused)
    if await skyvern_element.is_readonly(dynamic=True):
        LOG.warning(
            "Try to input text on a readonly element",
            task_id=task.task_id,
            step_id=step.step_id,
            element_id=skyvern_element.get_id(),
            action=action,
        )
        return [ActionFailure(InputToReadonlyElement(element_id=skyvern_element.get_id()))]

    used_bare_nanp = False
    run_phone_format_check = False
    tel_pattern: str | None = None
    tel_maxlength: str | None = None
    tel_e164_fallback: str | None = None

    if is_tel and not is_card_number_input and tel_fix_enabled is False and not is_secret_value:
        run_phone_format_check = True
        if tel_outcome is not None:
            tel_outcome.strategy = actions.TelInputStrategy.legacy_sequential
    if run_phone_format_check:
        try:
            action.set_has_mini_agent()
            text = await check_phone_number_format(
                value=text,
                action=action,
                skyvern_element=skyvern_element,
                scraped_page=scraped_page,
                task=task,
                step=step,
            )
        except Exception as exc:
            LOG.warning(
                "Failed to check the phone number format, using the original text",
                error_type=type(exc).__name__,
            )
    await _apply_secret_visual_mask_if_needed(
        skyvern_element,
        workflow_run_id=task.workflow_run_id,
        is_secret_value=is_secret_value,
        is_totp_value=is_totp_value,
        is_totp_sequence=is_multi_field_totp,
    )

    # TODO: some elements are supported to use `locator.press_sequentially()` to fill in the data
    # we need find a better way to detect the attribute in the future
    class_name: str | None = await skyvern_element.get_attr("class")
    if class_name and "blinking-cursor" in class_name.lower():
        if tel_outcome is not None and is_tel and tel_outcome.flag_enabled:
            assert tel_source_text is not None
            tel_pattern = await skyvern_element.get_attr("pattern")
            tel_maxlength = await skyvern_element.get_attr("maxlength")
            tel_e164_fallback = _nanp_e164_fallback(
                tel_source_text,
                pattern=tel_pattern,
                maxlength=tel_maxlength,
            )
            text, used_bare_nanp, run_phone_format_check = _plan_tel_text(
                is_tel=True,
                is_secret=is_secret_value,
                value=tel_source_text,
                pattern=tel_pattern,
            )
            if used_bare_nanp:
                tel_outcome.strategy = actions.TelInputStrategy.sequential_national
                phone_mismatch = await _fill_nanp_tel_with_readback(
                    skyvern_element=skyvern_element,
                    tag_name=tag_name,
                    national_digits=text,
                    e164_fallback=tel_e164_fallback,
                    pattern=tel_pattern,
                    maxlength=tel_maxlength,
                    engine_selection=engine_selection,
                    outcome=tel_outcome,
                    enforce_browser_validity=True,
                )
                if phone_mismatch is not None:
                    if isinstance(phone_mismatch, PhoneNumberInputMismatch):
                        LOG.warning(
                            "Phone input read-back mismatch after retry",
                            element_id=skyvern_element.get_id(),
                            expected_digit_count=phone_mismatch.expected_digit_count,
                            actual_digit_count=phone_mismatch.actual_digit_count,
                        )
                    return [ActionFailure(phone_mismatch)]
                return [ActionSuccess()]
            tel_outcome.strategy = actions.TelInputStrategy.formatted_sequential
            if run_phone_format_check:
                try:
                    action.set_has_mini_agent()
                    text = await check_phone_number_format(
                        value=text,
                        action=action,
                        skyvern_element=skyvern_element,
                        scraped_page=scraped_page,
                        task=task,
                        step=step,
                    )
                except Exception as exc:
                    LOG.warning(
                        "Failed to check the phone number format, using the original text",
                        error_type=type(exc).__name__,
                    )
        if is_totp_value:
            try:
                text = generate_totp_value_from_secret(totp_secret)
            except NoTOTPSecretFound as exc:
                return [ActionFailure(exc)]
            _register_runtime_otp_value_best_effort(task.workflow_run_id, text)
        try:
            await skyvern_element.press_fill(text=text)
        except Exception as exc:
            if phone_bearing:
                LOG.warning("Phone input browser interaction failed", error_type=type(exc).__name__)
                return [ActionFailure(PhoneNumberInputBrowserInteractionFailed())]
            raise
        if tel_outcome is not None and is_tel:
            expected_digit_count, actual_digit_count = await _log_tel_fallback_fill_digit_counts(
                skyvern_element=skyvern_element,
                tag_name=tag_name,
                expected_value=tel_source_text or text,
                task_id=task.task_id,
                step_id=step.step_id,
                engine_selection=engine_selection,
            )
            tel_outcome.attempt_count = max(tel_outcome.attempt_count, 1)
            tel_outcome.actual_digit_count = actual_digit_count
            tel_outcome.browser_valid = await _probe_tel_browser_validity(skyvern_element.get_locator())
        return [ActionSuccess()]

    # `Locator.clear()` on a spin button could cause the cursor moving away, and never be back
    # run `Locator.clear()` when:
    # 1. the element is not a spin button
    #   1.1. the element has a value attribute
    #   1.2. the element is not editable and not common input tag
    if not await skyvern_element.is_spinbtn_input() and (
        current_text
        or prefilter_attempted
        or (not await skyvern_element.is_editable() and tag_name not in COMMON_INPUT_TAGS)
    ):
        is_date_related = input_or_select_context is not None and input_or_select_context.is_date_related is True
        try:
            await skyvern_element.input_clear()
        except Exception as exc:
            if phone_bearing:
                LOG.warning("Phone input browser interaction failed", error_type=type(exc).__name__)
                return [ActionFailure(PhoneNumberInputBrowserInteractionFailed())]
            # The target already passed supports_text_input() above, so only a failure that names the
            # live node as incompatible may be reported as one. A timeout or any other driver error
            # carries no element-type evidence, and claiming otherwise both contradicts itself on a
            # real field and sends the agent hunting for a date picker via the hint.
            if isinstance(exc, InvalidElementForTextInput) or (
                _is_selected_engine_error(exc, engine_selection) and is_incompatible_text_input_error(exc)
            ):
                LOG.warning("Live node cannot accept text input while clearing", action=action, exc_info=True)
                return [
                    ActionFailure(
                        InvalidElementForTextInput(
                            element_id=action.element_id, tag_name=tag_name, is_date_related=is_date_related
                        )
                    )
                ]
            if _is_selected_engine_timeout(exc, engine_selection):
                LOG.info("Input field clear timeout", action=action)
            else:
                LOG.warning("Failed to clear the input field", action=action, exc_info=True)
            return [ActionFailure(FailedToClearInputField(element_id=action.element_id, tag_name=tag_name))]

    await skyvern_frame.safe_wait_for_animation_end(caller="input_text.blocking_check")
    retargeted = skyvern_element.get_id() != initial_action_target_id
    try:
        blocking_element, exist = await skyvern_element.find_blocking_element(
            dom=dom, incremental_page=incremental_scraped
        )
        if blocking_element and exist and await blocking_element.is_editable():
            LOG.warning(
                "Find a blocking element to the current element, going to input on the blocking element",
            )
            skyvern_element = blocking_element
            tag_name = blocking_element.get_tag_name().lower()
            retargeted = True
            is_tel = await skyvern_element.get_attr("type") == "tel"
            is_card_number_input = _is_probable_card_number(candidate_card_digits) and await _is_card_number_field(
                skyvern_element
            )
            phone_bearing = is_tel and not is_card_number_input
            await _apply_secret_visual_mask_if_needed(
                skyvern_element,
                workflow_run_id=task.workflow_run_id,
                is_secret_value=is_secret_value,
                is_totp_value=is_totp_value,
                is_totp_sequence=is_multi_field_totp,
            )
    except Exception:
        LOG.info(
            "Failed to find the blocking element, continue with the original element",
            exc_info=True,
        )

    if is_card_number_input:
        if tel_outcome is not None:
            text = tel_source_text or text
            action.tel_input_outcome = None
            tel_outcome = None
            tel_fix_enabled = None
            tel_source_text = None
        used_bare_nanp = False
        run_phone_format_check = False
        tel_pattern = None
        tel_maxlength = None
        tel_e164_fallback = None
    elif not is_tel:
        if tel_outcome is not None:
            text = tel_source_text or text
            action.tel_input_outcome = None
            tel_outcome = None
            tel_fix_enabled = None
            tel_source_text = None
        used_bare_nanp = False
        run_phone_format_check = False
        tel_pattern = None
        tel_maxlength = None
        tel_e164_fallback = None
    else:
        if tel_outcome is None:
            tel_source_text = text
            tel_fix_enabled = await _is_tel_digit_fix_enabled(task)
            tel_outcome = actions.TelInputOutcome(
                flag_enabled=tel_fix_enabled,
                final_element_id=skyvern_element.get_id(),
                strategy=actions.TelInputStrategy.legacy_sequential,
                expected_digit_count=len(_phone_digits(tel_source_text)),
                attempt_count=0,
                retargeted=retargeted,
            )
            action.tel_input_outcome = tel_outcome
        else:
            tel_outcome.final_element_id = skyvern_element.get_id()
            tel_outcome.retargeted = retargeted

        if tel_fix_enabled:
            assert tel_source_text is not None
            tel_pattern = await skyvern_element.get_attr("pattern")
            tel_maxlength = await skyvern_element.get_attr("maxlength")
            tel_e164_fallback = _nanp_e164_fallback(tel_source_text, pattern=tel_pattern, maxlength=tel_maxlength)
            text, used_bare_nanp, run_phone_format_check = _plan_tel_text(
                is_tel=True,
                is_secret=is_secret_value,
                value=tel_source_text,
                pattern=tel_pattern,
            )
            if used_bare_nanp:
                tel_outcome.strategy = actions.TelInputStrategy.sequential_national
                LOG.info(
                    "Tel bare-digit fill using national digits",
                    used_bare_nanp=True,
                    expected_digit_count=len(text),
                    element_id=skyvern_element.get_id(),
                    task_id=task.task_id,
                    step_id=step.step_id,
                )
            else:
                tel_outcome.strategy = actions.TelInputStrategy.formatted_sequential
            if run_phone_format_check:
                try:
                    action.set_has_mini_agent()
                    text = await check_phone_number_format(
                        value=text,
                        action=action,
                        skyvern_element=skyvern_element,
                        scraped_page=scraped_page,
                        task=task,
                        step=step,
                    )
                except Exception as exc:
                    LOG.warning(
                        "Failed to check the phone number format, using the original text",
                        error_type=type(exc).__name__,
                    )

    if is_totp_value:
        LOG.info("Skipping the auto completion logic since it's a TOTP input")
        try:
            text = generate_totp_value_from_secret(totp_secret)
        except NoTOTPSecretFound as exc:
            return [ActionFailure(exc)]
        _register_runtime_otp_value_best_effort(task.workflow_run_id, text)
        # A single-field TOTP is typed character-by-character across the same fill/type seam and, on a
        # caret-resetting field, submits reordered (123456 -> 654321) with no verification -- it sits below
        # every existing read-back (SKY-13821). On a caret-vulnerable <input>, read it back and re-enter
        # atomically on a mismatch, failing closed rather than submitting a wrong code. Reuses the secret
        # read-back path (a TOTP is a secret); multi-field TOTP sequences keep their own handling below.
        totp_input_type = _exact_value_input_type(await skyvern_element.get_attr("type"))
        totp_maxlength = await skyvern_element.get_attr("maxlength")
        if _caret_readback_eligible(tag_name=tag_name, input_type=totp_input_type, text=text, maxlength=totp_maxlength):
            totp_failure = await _fill_secret_with_readback(
                skyvern_element=skyvern_element,
                tag_name=tag_name,
                text=text,
                input_type=totp_input_type,
                maxlength=totp_maxlength,
                engine_selection=engine_selection,
            )
            if totp_failure is not None:
                return [totp_failure]
        else:
            await skyvern_element.input(text)
        return [ActionSuccess()]

    # Handle TOTP generation for multi-field TOTP sequences
    if action.totp_timing_info:
        timing_info = action.totp_timing_info
        if timing_info.get("is_totp_sequence"):
            action.set_has_mini_agent()
            result = await _handle_multi_field_totp_sequence(timing_info, task)
            if result is not None:
                return result  # Return ActionFailure if TOTP handling failed

            # Extract the digit for this action index
            current_totp = skyvern_context.ensure_context().totp_codes.get(f"{task.task_id}_totp_cache")
            action_index = timing_info["action_index"]

            if current_totp and len(current_totp) > action_index:
                digit = current_totp[action_index]
                action.text = digit
                # Also update the text variable that will be used later
                text = digit
            else:
                LOG.error(
                    "TOTP too short for action index",
                    action_idx=action_index,
                    totp_length=len(current_totp) if current_totp else 0,
                )
                return [ActionFailure(TOTPExpiredError())]

    try:
        # TODO: not sure if this case will trigger auto-completion
        if not await skyvern_element.is_editable() and tag_name not in COMMON_INPUT_TAGS:
            await skyvern_element.input_fill(text)
            return [ActionSuccess()]

        if len(text) == 0:
            return [ActionSuccess()]

        if tag_name == InteractiveElement.INPUT and await skyvern_element.get_attr("type") == "date":
            try:
                action.set_has_mini_agent()
                text = await check_date_format(
                    value=text,
                    action=action,
                    skyvern_element=skyvern_element,
                    task=task,
                    step=step,
                )
            except Exception:
                LOG.warning(
                    "Failed to check the date format, using the original text to fill in the date input",
                    text=text,
                    action=action,
                    exc_info=True,
                )

            await skyvern_element.input_fill(text=text)
            return [ActionSuccess()]

        if not await skyvern_element.is_raw_input():
            is_location_input = input_or_select_context.is_location_input if input_or_select_context else False
            if input_or_select_context and (await skyvern_element.is_auto_completion_input() or is_location_input):
                collapse_autocomplete_fanout_enabled = await _is_collapse_autocomplete_fanout_enabled(task)
                if not collapse_autocomplete_fanout_enabled:
                    action.set_has_mini_agent()
                if result := await input_or_auto_complete_input(
                    input_or_select_context=input_or_select_context,
                    scraped_page=scraped_page,
                    page=page,
                    dom=dom,
                    text=text,
                    skyvern_element=skyvern_element,
                    step=step,
                    task=task,
                    action=action,
                    collapse_autocomplete_fanout_enabled=collapse_autocomplete_fanout_enabled,
                    is_secret_value=is_secret_value,
                ):
                    auto_complete_hacky_flag = False
                    return [result]

        # Only the bare-digit NANP path uses the verified fallback ladder; other tel shapes remain
        # observational to preserve flag-off behavior.
        verify_tel_input_after_fill = used_bare_nanp

        # SKY-11720: an auto-formatting card-number field (a space every 4 digits) restores its caret
        # naively, racing character-by-character entry so the rendered value can silently differ from
        # the provided card while the block still completes. Deterministic card-number read-back runs
        # only when the value is Luhn-valid 13-19 digits and live field attrs identify a card-like
        # numeric field; mismatches are re-entered atomically before failing loudly.
        card_expected_digits = ""
        if is_card_number_input:
            card_expected_digits = candidate_card_digits

        # SKY-12143 / SKY-12597 / SKY-12579: character-by-character credential entry can race a hardened
        # field's caret restore (rotating the value) or be dropped by a controlled field (truncating it),
        # submitting a wrong or empty credential while the block still completes. On native inputs whose DOM
        # .value round-trips typed text exactly, read the value back and re-enter atomically on an empty or
        # mismatched read-back, compared character-for-character. Scoped to non-TOTP secrets on exact-value
        # input types (password/text/email/search/url/untyped); tel and card keep their digit-normalized
        # paths above, and number/date/textarea/contenteditable/select cannot round-trip exactly so they are
        # excluded. Evaluated on the actual element being filled, which find_blocking_element may have
        # retargeted above. A single character cannot be order-scrambled, so it is skipped.
        secret_input_type = _exact_value_input_type(await skyvern_element.get_attr("type"))
        element_maxlength = await skyvern_element.get_attr("maxlength")
        # A field whose options surface only as the value is typed -- a search-bar or location context, an
        # autocomplete input, or a combobox/typeahead -- must keep the per-character seam even for a secret:
        # an atomic write emits no key events, so the option tree stays empty and the action reports success
        # with uncommitted display text. Computed once here so it both excludes secret typed-widgets from the
        # first-write transport below and drives the ordinary-branch keeps_typing decision (SKY-13821).
        is_typed_widget = (
            (
                input_or_select_context is not None
                and bool(input_or_select_context.is_search_bar or input_or_select_context.is_location_input)
            )
            or await skyvern_element.is_auto_completion_input()
            or await _is_combobox_or_typeahead(skyvern_element)
        )
        # A positive maxlength shorter than the secret is an auto-advancing split field; an atomic write leaves
        # a truncated prefix and, since it cannot round-trip, reports success unverified. Route it to the seam
        # (below) so the per-character focus advance carries the rest to the sibling boxes (SKY-13821).
        verify_secret_input = (
            is_secret_value
            and not is_totp_value
            and len(text) > 1
            and not is_card_number_input
            and tag_name == InteractiveElement.INPUT
            and secret_input_type in _EXACT_VALUE_INPUT_TYPES
            and not _maxlength_truncates_value(text, element_maxlength)
        )
        secret_maxlength = element_maxlength if verify_secret_input else None
        await incremental_scraped.start_listen_dom_increment(await skyvern_element.get_element_handler())

        try:
            # Read-back/recover priority: card > tel > secret > masked > plain fill (each gate is mutually exclusive).
            if card_expected_digits:
                card_failure = await _fill_card_number_with_readback(
                    skyvern_element=skyvern_element,
                    tag_name=tag_name,
                    text=text,
                    expected_digits=card_expected_digits,
                    engine_selection=engine_selection,
                )
                if card_failure is not None:
                    return [card_failure]
            elif verify_tel_input_after_fill:
                phone_mismatch = await _fill_nanp_tel_with_readback(
                    skyvern_element=skyvern_element,
                    tag_name=tag_name,
                    national_digits=text,
                    e164_fallback=tel_e164_fallback,
                    pattern=tel_pattern,
                    maxlength=tel_maxlength,
                    engine_selection=engine_selection,
                    outcome=tel_outcome,
                    enforce_browser_validity=tel_fix_enabled is True,
                )
                if phone_mismatch is not None:
                    if isinstance(phone_mismatch, PhoneNumberInputMismatch):
                        LOG.warning(
                            "Phone input read-back mismatch after retry",
                            element_id=skyvern_element.get_id(),
                            expected_digit_count=phone_mismatch.expected_digit_count,
                            actual_digit_count=phone_mismatch.actual_digit_count,
                        )
                    return [ActionFailure(phone_mismatch)]
            elif verify_secret_input:
                secret_failure = await _fill_secret_with_readback(
                    skyvern_element=skyvern_element,
                    tag_name=tag_name,
                    text=text,
                    input_type=secret_input_type,
                    maxlength=secret_maxlength,
                    engine_selection=engine_selection,
                    sequential_first=is_typed_widget,
                )
                if secret_failure is not None:
                    return [secret_failure]
            else:
                contenteditable = await skyvern_element.get_attr("contenteditable", mode="static")
                is_contenteditable = contenteditable is not None and str(contenteditable).lower() != "false"
                # SKY-13821: an ordinary native input is populated with one atomic fill instead of the
                # input_sequentially fill(prefix)+type(tail) seam, so a caret-resetting field cannot reorder or
                # truncate the value. A typed-widget (the is_typed_widget signal computed above -- search-bar or
                # location context, an autocomplete input, or a combobox/typeahead) keeps the per-character seam
                # so its options surface. Non-native editable sinks and tel formatting also keep the seam; a
                # contenteditable rich-text editor fills atomically so a URL auto-linkifier cannot wrap the
                # prefix before the tail arrives (SKY-13014).
                keeps_typing = is_typed_widget
                # Only native input types whose .value round-trips typed text exactly are safe for one atomic
                # fill; number, date/time-like and other structured types hard-throw in locator.fill() on a
                # non-canonical value, so they keep the per-character seam that tolerated them (SKY-13821).
                fill_atomically = is_contenteditable or (
                    tag_name in _NATIVE_FILL_TAGS
                    and (tag_name != InteractiveElement.INPUT or secret_input_type in _EXACT_VALUE_INPUT_TYPES)
                    and not is_tel
                    and not keeps_typing
                    and not _maxlength_truncates_value(text, element_maxlength)
                )
                if fill_atomically:
                    await skyvern_element.refresh_locator_if_stale()
                    try:
                        await skyvern_element.input_fill(text)
                    except Exception as fill_error:
                        # A field scraped as text but live type=date rejects a locale value here; recover in
                        # canonical form from the live DOM, else re-raise so an ambiguous or non-date failure
                        # keeps its existing semantics.
                        if (
                            await _recover_atomic_fill_as_live_date(skyvern_element, text, fill_error, engine_selection)
                            is None
                        ):
                            raise
                        return [ActionSuccess()]
                else:
                    await skyvern_element.input_sequentially(text=text)
                    # The residual per-character seam can still lose a leading prefix on a caret-resetting
                    # field; preserve the deployed SKY-13631 truncation heal here (SKY-13821).
                    truncation_failure = await _heal_truncated_freetext_input(
                        skyvern_element=skyvern_element,
                        tag_name=tag_name,
                        text=text,
                        is_secret_value=is_secret_value,
                        engine_selection=engine_selection,
                    )
                    if isinstance(truncation_failure, ActionFailure):
                        return [truncation_failure]
                if tel_outcome is not None and is_tel and not verify_tel_input_after_fill:
                    expected_digit_count, actual_digit_count = await _log_tel_fallback_fill_digit_counts(
                        skyvern_element=skyvern_element,
                        tag_name=tag_name,
                        expected_value=tel_source_text or text,
                        task_id=task.task_id,
                        step_id=step.step_id,
                        engine_selection=engine_selection,
                    )
                    tel_outcome.attempt_count = max(tel_outcome.attempt_count, 1)
                    tel_outcome.actual_digit_count = actual_digit_count
                    tel_outcome.browser_valid = await _probe_tel_browser_validity(skyvern_element.get_locator())

            incremental_cleanup = clean_and_remove_element_tree_factory(
                task=task,
                step=step,
                check_filter_funcs=[check_existed_but_not_option_element_in_dom_factory(dom)],
                engine_selection=engine_selection,
            )
            incremental_element = await incremental_scraped.get_incremental_element_tree(incremental_cleanup)
            # A search combobox may render its filtered options a frame or two after the keystroke, so the
            # first incremental read can contain zero options; give it exactly one bounded render settle
            # (double-rAF, 250ms-liveness-capped) and re-read. Entered only for a live combobox/typeahead
            # whose first snapshot has neither the enabled target option nor any enabled selectable option --
            # the observed deferred-empty race. If the enabled target is already present (even as an option
            # whose value renders in nested spans, which the label-candidate predicate alone misses) or any
            # enabled option is already rendered, that is a populated state, not the race, so it adds no
            # settle; the target-option gate below still guards force selection.
            if (
                input_or_select_context is not None
                and input_or_select_context.is_search_bar
                and not is_secret_value
                and await _is_combobox_or_typeahead(skyvern_element)
                and not _incremental_tree_contains_option_subtree_with_target_value(incremental_element, text)
                and not _incremental_tree_has_enabled_selectable_option(incremental_element)
            ):
                await _wait_custom_select_render_settle(skyvern_element)
                incremental_element = await incremental_scraped.get_incremental_element_tree(incremental_cleanup)
            if len(incremental_element) > 0:
                auto_complete_hacky_flag = True
                if (
                    input_or_select_context
                    and input_or_select_context.is_search_bar
                    and not is_secret_value
                    and _incremental_tree_contains_option_subtree_with_target_value(incremental_element, text)
                ):
                    LOG.info(
                        "Detected target-matching dropdown after search-bar input; attempting custom selection",
                        element_id=skyvern_element.get_id(),
                        **_input_target_log_fields(is_tel=phone_bearing, text=text),
                    )
                    action.set_has_mini_agent()
                    select_result = await sequentially_select_from_dropdown(
                        action=select_action,
                        input_or_select_context=input_or_select_context,
                        page=page,
                        dom=dom,
                        skyvern_element=skyvern_element,
                        skyvern_frame=skyvern_frame,
                        incremental_scraped=incremental_scraped,
                        step=step,
                        task=task,
                        force_select=True,
                        target_value=text,
                        entry_action_type="input_text",
                    )
                    if select_result and select_result.action_result and select_result.action_result.success:
                        auto_complete_hacky_flag = False
                        # A matching option was committed during this INPUT_TEXT. Stop the batch only when
                        # the next queued action would clobber it (a trailing Enter/Return); next step re-scrapes.
                        if action.stop_batch_after_dropdown_select:
                            select_result.action_result.skip_remaining_actions = True
                        return [select_result.action_result]
                    if select_result and _is_terminal_custom_select_failure(select_result.action_result):
                        auto_complete_hacky_flag = False
                        return [select_result.action_result]
                elif (
                    input_or_select_context is not None
                    and not input_or_select_context.is_search_bar
                    and not input_or_select_context.is_location_input
                    and not is_secret_value
                    and _incremental_tree_contains_option_with_target_value(incremental_element, text)
                    and await _is_commit_required_combobox(skyvern_element)
                ):
                    # A role=combobox / aria-autocomplete field that is still aria-invalid after typing
                    # only commits by picking a rendered option; the Tab hack below won't do that. Force one
                    # deterministic selection against the surfaced option. This does not touch
                    # is_auto_completion_input() or the speculative pre-input fanout.
                    LOG.info(
                        "Detected target-matching option after typing into an invalid combobox; committing selection",
                        element_id=skyvern_element.get_id(),
                        **_input_target_log_fields(is_tel=phone_bearing, text=text),
                    )
                    action.set_has_mini_agent()
                    select_result = await sequentially_select_from_dropdown(
                        action=select_action,
                        input_or_select_context=input_or_select_context,
                        page=page,
                        dom=dom,
                        skyvern_element=skyvern_element,
                        skyvern_frame=skyvern_frame,
                        incremental_scraped=incremental_scraped,
                        step=step,
                        task=task,
                        force_select=True,
                        target_value=text,
                        entry_action_type="input_text",
                    )
                    if select_result and select_result.action_result and select_result.action_result.success:
                        auto_complete_hacky_flag = False
                        if action.stop_batch_after_dropdown_select:
                            select_result.action_result.skip_remaining_actions = True
                        return [select_result.action_result]
                    if select_result and _is_terminal_custom_select_failure(select_result.action_result):
                        auto_complete_hacky_flag = False
                        return [select_result.action_result]
        except SkyvernPageAnalysisTimeout as inc_error:
            # A page-analysis timeout after both incremental attempts previously arrived here as a
            # Playwright TimeoutError (a PlaywrightError) and was re-raised; the neutral
            # SkyvernPageAnalysisTimeout is not a PlaywrightError, so re-raise explicitly instead of
            # letting the broad handler below swallow it and falsely return ActionSuccess.
            LOG.warning(
                "Page-analysis timeout during incremental element processing",
                error_type=type(inc_error).__name__,
                error_message=str(inc_error),
            )
            raise inc_error
        except InvalidElementForTextInput as invalid_element_error:
            # input_fill/input_clear raise this (a SkyvernException, not a PlaywrightError) when the live node
            # disagrees with the scraped tag and cannot accept text. It is not an engine error, so the broad
            # handler below would swallow it and falsely return ActionSuccess with the value never written;
            # re-raise to fail closed, matching the SkyvernPageAnalysisTimeout treatment above (SKY-13821).
            LOG.warning(
                "Element cannot accept text input; failing closed instead of reporting success",
                error_type=type(invalid_element_error).__name__,
                error_message=str(invalid_element_error),
            )
            raise invalid_element_error
        except Exception as inc_error:
            # Driver-native errors during incremental processing (e.g. TOTP form auto-submit, or a
            # search-dropdown selection triggering navigation) are classified against THIS run's
            # selected engine, so a non-stock driver's navigation errors are tolerated identically;
            # missing selection keeps the stock Playwright identity. Non-engine errors stay swallowed.
            if _is_selected_engine_error(inc_error, engine_selection):
                error_message = str(inc_error).lower()
                if (
                    "execution context was destroyed" in error_message
                    or "navigation" in error_message
                    or "target closed" in error_message
                ):
                    # These are expected during page navigation/auto-submit, silently continue
                    LOG.debug(
                        "Engine error during incremental element processing (likely page navigation)",
                        **_browser_error_log_fields(inc_error, is_tel=phone_bearing),
                    )
                else:
                    LOG.warning(
                        "Unexpected engine error during incremental element processing",
                        **_browser_error_log_fields(inc_error, is_tel=phone_bearing),
                    )
                    raise inc_error
            elif isinstance(inc_error, PlaywrightError):
                # A foreign stock-Playwright driver error under a pinned non-stock engine. Under the
                # stock default this arm is unreachable (a PlaywrightError satisfies the engine
                # predicate above), so this is a no-op there. It is not one of the selected engine's
                # tolerances, so propagate it exactly as the pre-PR ``except PlaywrightError`` did
                # instead of falling through to a false ActionSuccess.
                LOG.warning(
                    "Foreign driver error during incremental element processing under a pinned engine",
                    **_browser_error_log_fields(inc_error, is_tel=phone_bearing),
                )
                raise inc_error
            else:
                # Handle any other unexpected errors during incremental element processing
                LOG.warning(
                    "Unexpected error during incremental element processing",
                    **_browser_error_log_fields(inc_error, is_tel=phone_bearing),
                )
        finally:
            # Always stop listening
            await incremental_scraped.stop_listen_dom_increment()

        return [ActionSuccess()]
    except (SkyvernPageAnalysisTimeout, InvalidElementForTextInput):
        raise
    except _DISPATCHER_OWNED_INPUT_EXCEPTIONS:
        raise
    except Exception as exc:
        if phone_bearing:
            LOG.warning("Phone input browser interaction failed", error_type=type(exc).__name__)
            return [ActionFailure(PhoneNumberInputBrowserInteractionFailed())]
        LOG.exception("Failed to input the value or finish the auto completion")
        raise
    finally:
        if tel_outcome is not None and is_tel and tel_outcome.actual_digit_count is None:
            try:
                actual_value = await get_input_value(
                    tag_name=tag_name,
                    locator=skyvern_element.get_locator(),
                    engine_selection=engine_selection,
                )
                tel_outcome.actual_digit_count = len(_phone_digits(actual_value))
            except Exception:
                pass
            tel_outcome.browser_valid = await _probe_tel_browser_validity(skyvern_element.get_locator())

        # HACK: force to finish missing auto completion input
        if (
            auto_complete_hacky_flag
            and await skyvern_element.is_visible()
            and not await skyvern_element.is_raw_input()
            and not action.skip_auto_complete_tab
        ):
            LOG.debug(
                "Trigger input-selection hack, pressing Tab to choose one",
                action=action,
            )
            await skyvern_element.press_key("Tab")


_URL_RECOVERY_EDIT_DISTANCE_FRACTION = 0.1
_URL_RECOVERY_MAX_EDIT_DISTANCE = 10


def _origin_key(parsed: urllib.parse.ParseResult) -> tuple[str, str | None, int | None, str]:
    return (parsed.scheme.lower(), parsed.hostname, parsed.port, parsed.path)


def _find_similar_url_in_text(candidate_url: str, text: str) -> str | None:
    # Bounded-edit-distance substring search via fuzzysearch (Bitap /
    # Levenshtein-automaton kernel). Recovers a verbatim user-supplied URL when
    # the LLM flips a few characters inside a long pre-signed token. The
    # origin-key gate prevents any cross-origin swap.
    if not candidate_url or not text:
        return None
    normalized = candidate_url.strip()
    try:
        candidate = urllib.parse.urlparse(normalized)
    except ValueError:
        return None
    if not candidate.scheme or not candidate.hostname:
        return None

    max_dist = min(max(1, int(len(normalized) * _URL_RECOVERY_EDIT_DISTANCE_FRACTION)), _URL_RECOVERY_MAX_EDIT_DISTANCE)
    # Case-insensitive match so scheme/hostname casing doesn't consume the edit-distance budget.
    matches = find_near_matches(normalized.lower(), text.lower(), max_l_dist=max_dist)
    if not matches:
        return None

    best = min(matches, key=lambda m: m.dist)
    matched = text[best.start : best.end]
    try:
        parsed = urllib.parse.urlparse(matched)
    except ValueError:
        return None
    if _origin_key(parsed) != _origin_key(candidate):
        return None
    return matched


async def _wait_for_upload_processing(page: Page, engine_selection: BrowserEngineSelection | None = None) -> None:
    """Wait for page readiness signals after a file upload.

    Covers upload-processing UI (spinners, progress bars, DOM updates) beyond
    bare networkidle by reusing SkyvernFrame.wait_for_page_ready with
    upload-tuned timeouts that keep worst-case well below the old 10-15 s sleep.
    """
    try:
        # Settle delay: let the page react to the file-input change and mount
        # upload UI (spinner, progress bar, XHR) before polling for readiness.
        await _upload_settle_sleep(0.5)
        skyvern_frame = await SkyvernFrame.create_instance(page, engine_selection=engine_selection)
        await skyvern_frame.wait_for_page_ready(
            loading_indicator_timeout_ms=3000,
            network_idle_timeout_ms=3000,
            dom_stable_ms=300,
            dom_stability_timeout_ms=2000,
        )
    except (asyncio.TimeoutError, SkyvernPageAnalysisTimeout):
        LOG.info("Upload processing page-ready wait timed out, continuing")
    except Exception as exc:
        # Classify against THIS run's selected engine so a non-stock driver's timeout/error is
        # tolerated identically; missing selection keeps the stock Playwright identity. Anything
        # that is not an engine error propagates, as before.
        if _is_selected_engine_timeout(exc, engine_selection):
            LOG.info("Upload processing page-ready wait timed out, continuing")
        elif _is_selected_engine_error(exc, engine_selection):
            LOG.warning(
                "Upload processing page-ready wait interrupted by browser engine error, continuing", exc_info=True
            )
        else:
            raise


@traced(name="skyvern.agent.action.upload_file")
async def handle_upload_file_action(
    action: actions.UploadFileAction,
    page: Page,
    scraped_page: ScrapedPage,
    task: Task,
    step: Step,
) -> list[ActionResult]:
    if not action.file_url:
        LOG.warning("InputFileAction has no file_url", action=action)
        return [ActionFailure(MissingFileUrl())]
    # ************************************************************************************************************** #
    # After this point if the file_url is a secret, it will be replaced with the actual value
    # In order to make sure we don't log the secret value, we log the action with the original value action.file_url
    # ************************************************************************************************************** #
    file_url = get_actual_value_of_parameter_if_secret_with_task(task, action.file_url)
    decoded_url = urllib.parse.unquote(file_url)
    if (
        file_url not in str(task.navigation_payload)
        and file_url not in str(task.navigation_goal)
        and decoded_url not in str(task.navigation_payload)
        and decoded_url not in str(task.navigation_goal)
    ):
        user_sources = f"{task.navigation_goal or ''}\n{task.navigation_payload or ''}"
        recovered_url = _find_similar_url_in_text(file_url, user_sources) or _find_similar_url_in_text(
            decoded_url, user_sources
        )
        if recovered_url:
            LOG.warning(
                "LLM-returned file_url appears to be a corrupted copy of a user-provided URL; using the verbatim URL",
                action=action,
            )
            file_url = recovered_url
            decoded_url = urllib.parse.unquote(file_url)
        else:
            LOG.warning(
                "LLM might be imagining the file url, which is not in navigation payload",
                action=action,
                file_url=action.file_url,
            )
            return [ActionFailure(ImaginaryFileUrl(action.file_url))]

    dom = DomUtil(scraped_page=scraped_page, page=page)
    skyvern_element = await dom.get_skyvern_element_by_id(action.element_id)

    # dynamically validate the attr, since it could change into enabled after the previous actions
    if not await SkyvernElement.wait_until_enabled(skyvern_element):
        LOG.warning(
            "Try to upload file on a disabled element",
            action_type=action.action_type,
            element_id=skyvern_element.get_id(),
        )
        return [ActionFailure(InteractWithDisabledElement(skyvern_element.get_id()))]

    locator = skyvern_element.locator

    file_path = await handler_utils.download_file(file_url, action.model_dump(), task.organization_id)
    is_file_input = await skyvern_element.is_file_input()

    if not is_file_input:
        LOG.info("Trying to find file input in children", action=action)
        file_input_locator = await skyvern_element.find_file_input_in_children()
        if file_input_locator:
            LOG.info("Found file input in children", action=action)
            locator = file_input_locator
            is_file_input = True

    if is_file_input:
        LOG.info("Taking UploadFileAction. Found file input tag", action=action)
        if file_path:
            engine_selection = resolve_engine_selection_for_task(task, app.BROWSER_MANAGER)
            await locator.set_input_files(
                file_path,
                timeout=settings.BROWSER_ACTION_TIMEOUT_MS,
            )

            await _wait_for_upload_processing(page, engine_selection=engine_selection)

            return [ActionSuccess()]
        else:
            return [ActionFailure(Exception(f"Failed to download file from {action.file_url}"))]
    else:
        LOG.info("Taking UploadFileAction. Found non file input tag", action=action)
        # treat it as a click action
        action.is_upload_file_tag = False
        # The action itself changed shape; re-project it before it takes the click path.
        preflight_action(action, page, site="upload_to_click")
        return await chain_click(
            task,
            scraped_page,
            page,
            action,
            skyvern_element,
            pending_upload_files=file_path,
            timeout=settings.BROWSER_ACTION_TIMEOUT_MS,
        )


# This function is deprecated in 'extract-actions' prompt. Downloads are handled by the click action handler now.
# Currently, it's only used for the download action triggered by the code.
@traced(name="skyvern.agent.action.download_file")
async def handle_download_file_action(
    action: actions.DownloadFileAction,
    page: Page,
    scraped_page: ScrapedPage,
    task: Task,
    step: Step,
) -> list[ActionResult]:
    file_name = f"{action.file_name or uuid.uuid4()}"
    download_folder = initialize_download_dir()
    full_file_path = f"{download_folder}/{file_name}"

    try:
        # Priority 1: If byte data is provided, save it directly
        if action.byte is not None:
            with open(full_file_path, "wb") as f:
                f.write(action.byte)

            LOG.info(
                "DownloadFileAction: Saved file from byte data",
                action=action,
                full_file_path=full_file_path,
                file_size=len(action.byte),
            )
            return [ActionSuccess()]

        # Priority 2: If download_url is provided, download from URL
        if action.download_url is not None:
            # the URL is usally requiring login credentials/cookides, so we should use browser navigation to access the URL instead of downloading the file directly
            validated_url = await asyncio.to_thread(validate_fetch_url, action.download_url)
            try:
                response = await page.goto(validated_url, timeout=settings.BROWSER_LOADING_TIMEOUT_MS)
            except Exception as e:
                error = str(e)
                # some cases use this method to download a file. but it will be redirected away soon
                # and agent will run into ABORTED error.
                # some cases playwright will raise error like "Page.goto: Download is starting"
                if "net::ERR_ABORTED" not in error and "Page.goto: Download is starting" not in error:
                    raise e
            else:
                await revalidate_redirect_chain(response, validate_fetch_url, page.goto)

            LOG.info(
                "DownloadFileAction: Downloaded file from URL",
                action=action,
                full_file_path=full_file_path,
                download_url=action.download_url,
            )
            return [ActionSuccess()]

        return [ActionSuccess()]

    except Exception as e:
        LOG.exception(
            "DownloadFileAction: Failed to download file",
            action=action,
            full_file_path=full_file_path,
            download_url=action.download_url,
            has_byte=action.byte is not None,
        )
        return [ActionFailure(e)]


@traced(name="skyvern.agent.action.null")
async def handle_null_action(
    action: actions.NullAction,
    page: Page,
    scraped_page: ScrapedPage,
    task: Task,
    step: Step,
) -> list[ActionResult]:
    return [ActionSuccess(data=action.output)]


@traced(name="skyvern.agent.action.select_option")
async def handle_select_option_action(
    action: actions.SelectOptionAction,
    page: Page,
    scraped_page: ScrapedPage,
    task: Task,
    step: Step,
    entry_action_type: str = "select_option",
) -> list[ActionResult]:
    dom = DomUtil(scraped_page, page)
    skyvern_element = await dom.get_skyvern_element_by_id(action.element_id)
    engine_selection = resolve_engine_selection_for_task(task, app.BROWSER_MANAGER)

    tag_name = skyvern_element.get_tag_name()
    element_dict = scraped_page.id_to_element_dict[action.element_id]
    LOG.info(
        "SelectOptionAction",
        sampling=True,
        action=action,
        tag_name=tag_name,
        element_dict=element_dict,
    )

    # Handle the edge case:
    # Sometimes our custom select logic could fail, and leaving the dropdown being opened.
    # Confirm if the select action is on the custom option element
    if await skyvern_element.is_custom_option():
        if not await SkyvernElement.wait_until_enabled(skyvern_element):
            LOG.warning(
                "Try to select on a disabled custom option",
                action_type=action.action_type,
                element_id=skyvern_element.get_id(),
            )
            return [ActionFailure(InteractWithDisabledElement(skyvern_element.get_id()))]
        click_action = ClickAction(element_id=action.element_id)
        action.set_has_mini_agent()
        preflight_derived_action(click_action, page, parent=action, site="select_option_to_click")
        return await chain_click(task, scraped_page, page, click_action, skyvern_element)

    if not await skyvern_element.is_selectable():
        # 1. find from children
        # TODO: 2. find from siblings and their children
        LOG.info(
            "Element is not selectable, try to find the selectable element in the children",
            tag_name=tag_name,
            action=action,
        )

        selectable_child: SkyvernElement | None = None
        try:
            selectable_child = await skyvern_element.find_selectable_child(dom=dom)
        except Exception as e:
            LOG.error(
                "Failed to find selectable element in children",
                exc_info=True,
                tag_name=tag_name,
                action=action,
            )
            return [ActionFailure(ErrFoundSelectableElement(action.element_id, e))]

        if selectable_child:
            LOG.info(
                "Found selectable element in the children",
                tag_name=selectable_child.get_tag_name(),
                element_id=selectable_child.get_id(),
            )
            select_action = SelectOptionAction(
                reasoning=action.reasoning,
                element_id=selectable_child.get_id(),
                option=action.option,
                intention=action.intention,
                input_or_select_context=action.input_or_select_context,
            )
            action = select_action
            skyvern_element = selectable_child

    # dynamically validate the attr, since it could change into enabled after the previous actions
    if not await SkyvernElement.wait_until_enabled(skyvern_element):
        LOG.warning(
            "Try to select on a disabled element",
            action_type=action.action_type,
            element_id=skyvern_element.get_id(),
        )
        return [ActionFailure(InteractWithDisabledElement(skyvern_element.get_id()))]

    if skyvern_element.get_tag_name() == InteractiveElement.SELECT:
        LOG.info(
            "SelectOptionAction is on <select>",
            action=action,
        )

        # Idempotent no-op: if the requested value is already selected, we're done — regardless of
        # visibility. (normal_select does this check too, but the hidden-select path below skips
        # normal_select, so run it here to avoid a false failure on an already-correct hidden select.)
        try:
            current_selected = await skyvern_element.get_attr("selected")
            if current_selected and current_selected in (action.option.label, action.option.value):
                return [ActionSuccess()]
        except Exception:
            LOG.info("Failed to confirm current <select> value; proceeding with the select action")

        # A VISIBLE native <select> is the real control: drive it via select_option (which commits
        # the value through the DOM even under an overlay such as a consent/opt-out modal). Try that
        # first and, on failure, only click-navigate a genuine dropdown surrogate so an unrelated
        # modal can't hijack it. A hidden native <select> (display:none behind a styled dropdown)
        # can't be driven by select_option — the overlapping element IS the styled widget that
        # replaced it, so click-navigate that widget directly (the pre-refactor behavior). (SKY-11618)
        select_is_visible = await skyvern_element.is_visible()
        normal_select_result: list[ActionResult] | None = None
        if select_is_visible:
            try:
                normal_select_result = await normal_select(
                    action=action,
                    skyvern_element=skyvern_element,
                    builder=dom.scraped_page,
                    task=task,
                    step=step,
                    engine_selection=engine_selection,
                )
            except Exception as e:
                # normal_select can raise before returning (e.g. an LLM/provider error). Don't lose
                # the styled-dropdown fallback below — record the failure and keep going.
                LOG.warning("normal_select raised; falling back to blocking-element detection", exc_info=True)
                normal_select_result = [ActionFailure(exception=e)]
            if _normal_select_successful(normal_select_result):
                return normal_select_result

        blocking_element: SkyvernElement | None = None
        exist = False
        try:
            await skyvern_element.scroll_into_view()
            blocking_element, exist = await skyvern_element.find_blocking_element(dom=dom)
            if not exist or blocking_element is None:
                await skyvern_element.scroll_into_view()
                blocking_element, exist = await skyvern_element.find_blocking_element(dom=dom)
        except Exception:
            LOG.warning("Failed to find the blocking element for <select>", action=action, exc_info=True)
            blocking_element, exist = None, False

        if not exist or blocking_element is None:
            # No styled widget to click-navigate. Return the visible native failure if we ran it; a
            # hidden <select> with no surrogate can't be driven at all, so fail fast (don't run
            # select_option on a hidden node — it would only burn the visibility timeout).
            if normal_select_result is not None:
                return normal_select_result
            return [ActionFailure(EmptySelect(element_id=action.element_id))]

        # For a VISIBLE <select>, require dropdown-surrogate evidence before retargeting, so an
        # unrelated overlay (e.g. a consent/opt-out modal) can't hijack a failed selection. For a
        # hidden backing <select>, the overlapping element is the styled widget that replaced it —
        # click-navigate it regardless (its dropdown role may sit on an ancestor of the hit node).
        if select_is_visible and not await _is_dropdown_surrogate_blocker(blocking_element):
            LOG.info(
                "<select> blocked by a non-dropdown element (likely an overlay); returning the native "
                "select result instead of click-navigating it",
                blocking_element=blocking_element.get_id(),
            )
            return (
                normal_select_result
                if normal_select_result is not None
                else [ActionFailure(EmptySelect(element_id=action.element_id))]
            )

        LOG.info(
            "<select> not set via select_option; selecting on the blocking (styled-dropdown) element",
            blocking_element=blocking_element.get_id(),
        )
        select_action = SelectOptionAction(
            reasoning=action.reasoning,
            element_id=blocking_element.get_id(),
            option=action.option,
            intention=action.intention,
            input_or_select_context=action.input_or_select_context,
        )
        action = select_action
        skyvern_element = blocking_element

    if await skyvern_element.is_checkbox():
        LOG.info(
            "SelectOptionAction is on <input> checkbox",
            action=action,
        )
        check_action = CheckboxAction(element_id=action.element_id, is_checked=True)
        action.set_has_mini_agent()
        preflight_derived_action(check_action, page, parent=action, site="select_option_to_checkbox")
        return await handle_checkbox_action(check_action, page, scraped_page, task, step)

    if await skyvern_element.is_radio():
        LOG.info(
            "SelectOptionAction is on <input> radio",
            action=action,
        )
        click_action = ClickAction(element_id=action.element_id)
        action.set_has_mini_agent()
        preflight_derived_action(click_action, page, parent=action, site="select_option_to_click")
        return await chain_click(task, scraped_page, page, click_action, skyvern_element)

    # FIXME: maybe there's a case where <input type="button"> could trigger dropdown menu?
    if await skyvern_element.is_btn_input():
        LOG.info(
            "SelectOptionAction is on <input> button",
            action=action,
        )
        click_action = ClickAction(element_id=action.element_id)
        action.set_has_mini_agent()
        preflight_derived_action(click_action, page, parent=action, site="select_option_to_click")
        return await chain_click(task, scraped_page, page, click_action, skyvern_element)

    LOG.info(
        "Trigger custom select",
        action=action,
        element_id=skyvern_element.get_id(),
    )

    timeout = settings.BROWSER_ACTION_TIMEOUT_MS
    skyvern_frame = await SkyvernFrame.create_instance(skyvern_element.get_frame(), engine_selection=engine_selection)
    incremental_scraped = IncrementalScrapePage(skyvern_frame=skyvern_frame, engine_selection=engine_selection)
    is_open = False
    suggested_value: str | None = None
    results: list[ActionResult] = []
    input_or_select_context: InputOrSelectContext | None = None

    try:
        await incremental_scraped.start_listen_dom_increment(await skyvern_element.get_element_handler())
        await skyvern_element.scroll_into_view()

        await skyvern_element.click(
            page=page,
            dom=dom,
            timeout=timeout,
            engine_selection=engine_selection,
        )
        # The click opens the widget: mark it open now (not only on the incremental path below) so the
        # finally cleanup dismisses it on every exit — including an emerging-path optional miss that
        # returns ActionAbort before reaching the incremental branch.
        is_open = True
        # wait for options to load
        await skyvern_frame.safe_wait_for_animation_end(before_wait_sec=0.5, caller="select_option.open")

        incremental_element = await incremental_scraped.get_incremental_element_tree(
            clean_and_remove_element_tree_factory(
                task=task,
                step=step,
                check_filter_funcs=[check_existed_but_not_option_element_in_dom_factory(dom)],
                engine_selection=engine_selection,
            ),
        )

        if len(incremental_element) == 0 and skyvern_element.get_tag_name() == InteractiveElement.INPUT:
            LOG.info(
                "No incremental elements detected for the input element, trying to press Arrowdown to trigger the dropdown",
                element_id=skyvern_element.get_id(),
            )
            await skyvern_element.scroll_into_view()
            await skyvern_element.press_key("ArrowDown")
            # wait for options to load
            await skyvern_frame.safe_wait_for_animation_end(before_wait_sec=0.5, caller="select_option.arrowdown")
            incremental_element = await incremental_scraped.get_incremental_element_tree(
                clean_and_remove_element_tree_factory(
                    task=task,
                    step=step,
                    check_filter_funcs=[check_existed_but_not_option_element_in_dom_factory(dom)],
                    engine_selection=engine_selection,
                ),
            )

        input_or_select_context = await _get_input_or_select_context(
            action=action,
            element_tree_builder=scraped_page,
            task=task,
            step=step,
            skyvern_element=skyvern_element,
            engine_selection=engine_selection,
        )

        if len(incremental_element) == 0:
            LOG.info(
                "No incremental elements detected by MutationObserver, using re-scraping the page to find the match element"
            )
            results.append(
                await select_from_emerging_elements(
                    current_element_id=skyvern_element.get_id(),
                    options=CustomSelectPromptOptions(
                        is_date_related=input_or_select_context.is_date_related or False,
                        field_information=input_or_select_context.intention or input_or_select_context.field or "",
                        required_field=input_or_select_context.is_required or False,
                        target_value=action.option.label or action.option.value or "",
                    ),
                    page=page,
                    scraped_page=scraped_page,
                    task=task,
                    step=step,
                    entry_action_type=entry_action_type,
                    engine_selection=engine_selection,
                )
            )
            return results

        # TODO: support sequetially select from dropdown by value, just support single select now
        result = await sequentially_select_from_dropdown(
            action=action,
            input_or_select_context=input_or_select_context,
            page=page,
            dom=dom,
            skyvern_element=skyvern_element,
            skyvern_frame=skyvern_frame,
            incremental_scraped=incremental_scraped,
            step=step,
            task=task,
            force_select=True,
            target_value=action.option.label or action.option.value or "",
            entry_action_type=entry_action_type,
        )
        # force_select won't return None result
        assert result is not None
        assert result.action_result is not None
        results.append(result.action_result)
        if result.action_result.skip_remaining_actions:
            return results
        if isinstance(result.action_result, ActionSuccess) or result.value is None:
            return results
        suggested_value = result.value

    except NoAvailableOptionFoundForCustomSelection as e:
        # Skip only a field known to be optional whose widget was left untouched. Requiredness is
        # LLM-populated and may be None (undetermined) — fail closed there. Also fail closed when an
        # earlier cascade level already committed a click (e.widget_mutated): a partially-selected
        # widget must surface the typed OPTION_NOT_AVAILABLE failure/retry, not a clean skip.
        if (
            input_or_select_context is not None
            and input_or_select_context.is_required is False
            and not e.widget_mutated
        ):
            LOG.info(
                "Optional custom-select found no matching option; recording an optional miss and skipping the step",
                target_value=action.option.label or action.option.value,
            )
            results.append(ActionAbort())
            return results
        LOG.warning(
            "Custom-select found no matching option for a required, unknown-requiredness, or partially-mutated field",
            exc_info=True,
        )
        results.append(ActionFailure(exception=e))
        return results
    except SkyvernException as e:
        # Expected selection outcomes on non-standard dropdowns (no matching option,
        # no incremental elements); recorded as ActionFailure like any other miss.
        LOG.warning("Custom select error", exc_info=True)
        results.append(ActionFailure(exception=e))
        return results
    except Exception as e:
        LOG.exception("Custom select error")
        results.append(ActionFailure(exception=e))
        return results
    finally:
        if (
            await skyvern_element.is_visible()
            and is_open
            and len(results) > 0
            and not isinstance(results[-1], ActionSuccess)
        ):
            await skyvern_element.scroll_into_view()
            await skyvern_element.coordinate_click(page=page)
            await skyvern_element.press_key("Escape")
        is_open = False
        await skyvern_element.blur()
        await incremental_scraped.stop_listen_dom_increment()

    LOG.info(
        "Try to select by value in custom select",
        element_id=skyvern_element.get_id(),
        value=suggested_value,
    )
    try:
        await incremental_scraped.start_listen_dom_increment(await skyvern_element.get_element_handler())
        timeout = settings.BROWSER_ACTION_TIMEOUT_MS
        await skyvern_element.scroll_into_view()

        try:
            await EventStrategyFactory.move_to_element(page, skyvern_element.get_locator())
            await skyvern_element.get_locator().click(timeout=timeout)
        except Exception:
            LOG.info(
                "fail to open dropdown by clicking, try to press arrow down to open",
                element_id=skyvern_element.get_id(),
            )
            await skyvern_element.scroll_into_view()
            await skyvern_element.press_key("ArrowDown")

        await skyvern_frame.safe_wait_for_animation_end(before_wait_sec=0.5, caller="select_option.fallback")
        is_open = True

        result = await select_from_dropdown_by_value(
            value=suggested_value,
            page=page,
            dom=dom,
            skyvern_element=skyvern_element,
            skyvern_frame=skyvern_frame,
            incremental_scraped=incremental_scraped,
            task=task,
            step=step,
        )
        results.append(result)
        return results

    except Exception as e:
        LOG.exception("Custom select by value error")
        results.append(ActionFailure(exception=e))
        return results

    finally:
        if (
            await skyvern_element.is_visible()
            and is_open
            and len(results) > 0
            and not isinstance(results[-1], ActionSuccess)
        ):
            await skyvern_element.scroll_into_view()
            await skyvern_element.coordinate_click(page=page)
            await skyvern_element.press_key("Escape")
        is_open = False
        await skyvern_element.blur()
        await incremental_scraped.stop_listen_dom_increment()


@traced(name="skyvern.agent.action.checkbox")
async def handle_checkbox_action(
    action: actions.CheckboxAction,
    page: Page,
    scraped_page: ScrapedPage,
    task: Task,
    step: Step,
) -> list[ActionResult]:
    """
    ******* NOT REGISTERED *******
    This action causes more harm than it does good.
    It frequently mis-behaves, or gets stuck in click loops.
    Treating checkbox actions as click actions seem to perform way more reliably
    Developers who tried this and failed: 2 (Suchintan and Shu 😂)
    """

    dom = DomUtil(scraped_page=scraped_page, page=page)
    skyvern_element = await dom.get_skyvern_element_by_id(action.element_id)
    locator = skyvern_element.locator

    if action.is_checked:
        await locator.check(timeout=settings.BROWSER_ACTION_TIMEOUT_MS)
    else:
        await locator.uncheck(timeout=settings.BROWSER_ACTION_TIMEOUT_MS)

    # TODO (suchintan): Why does checking the label work, but not the actual input element?
    return [ActionSuccess()]


@traced(name="skyvern.agent.action.wait")
async def handle_wait_action(
    action: actions.WaitAction,
    page: Page,
    scraped_page: ScrapedPage,
    task: Task,
    step: Step,
) -> list[ActionResult]:
    await asyncio.sleep(action.seconds)
    return [ActionFailure(exception=Exception("Wait action is treated as a failure"))]


@traced(name="skyvern.agent.action.hover")
async def handle_hover_action(
    action: actions.HoverAction,
    page: Page,
    scraped_page: ScrapedPage,
    task: Task,
    step: Step,
) -> list[ActionResult]:
    dom = DomUtil(scraped_page=scraped_page, page=page)
    try:
        skyvern_element = await dom.get_skyvern_element_by_id(action.element_id)
    except Exception as exc:
        LOG.warning(
            "Failed to resolve element for hover action",
            action=action,
            workflow_run_id=task.workflow_run_id,
            exc_info=True,
        )
        return [ActionFailure(exception=exc)]

    try:
        await skyvern_element.hover_to_reveal()
        await skyvern_element.get_locator().scroll_into_view_if_needed()
        await EventStrategyFactory.move_to_element(page, skyvern_element.get_locator())
        await skyvern_element.get_locator().hover(timeout=settings.BROWSER_ACTION_TIMEOUT_MS)

        if action.hold_seconds and action.hold_seconds > 0:
            await asyncio.sleep(action.hold_seconds)
        return [ActionSuccess()]
    except Exception as exc:
        LOG.warning(
            "Hover action failed",
            action=action,
            workflow_run_id=task.workflow_run_id,
            exc_info=True,
        )
        return [ActionFailure(FailToHover(skyvern_element.get_id(), msg=str(exc)))]


@traced(name="skyvern.agent.action.terminate")
async def handle_terminate_action(
    action: actions.TerminateAction,
    page: Page,
    scraped_page: ScrapedPage,
    task: Task,
    step: Step,
) -> list[ActionResult]:
    if task.error_code_mapping:
        try:
            action.errors = await extract_user_defined_errors(
                task=task, step=step, scraped_page=scraped_page, reasoning=action.reasoning
            )
        except Exception:
            LOG.warning(
                "extract_user_defined_errors failed, using errors from action reasoning",
                task_id=task.task_id,
                step_id=step.step_id,
                action_errors=action.errors,
                exc_info=True,
            )
    return [ActionSuccess()]


@traced(name="skyvern.agent.complete_verification")
async def handle_complete_action(
    action: actions.CompleteAction,
    page: Page,
    scraped_page: ScrapedPage,
    task: Task,
    step: Step,
) -> list[ActionResult]:
    # verification_path labels the handler-internal outcome of this span
    # (already_verified / needs_llm_* / terminate_requested). Caller-side
    # attribution (periodic vs handler-forced) lives on the child
    # complete_verify span as `verification.trigger`.
    _span = otel_trace.get_current_span()
    if action.verified or not task.navigation_goal:
        _span.set_attribute("verification_path", "already_verified")
        return [ActionSuccess()]

    LOG.info(
        "CompleteAction hasn't been verified, going to verify the user goal",
        workflow_run_id=task.workflow_run_id,
    )
    try:
        verification_result = await app.agent.complete_verify(
            page, scraped_page, task, step, verification_trigger="complete_action_forced"
        )
    except ScreenshotTargetClosed as e:
        _span.set_attribute("verification_path", "needs_llm_error")
        LOG.warning(
            "Browser target closed while verifying the complete action",
            workflow_run_id=task.workflow_run_id,
            exception_message=str(e),
        )
        return [ActionFailure(exception=e)]
    except Exception as e:
        _span.set_attribute("verification_path", "needs_llm_error")
        LOG.exception(
            "Failed to verify the complete action",
            workflow_run_id=task.workflow_run_id,
        )
        return [ActionFailure(exception=e)]

    # Check if we should terminate instead of complete
    # Note: This requires the USE_TERMINATION_AWARE_COMPLETE_VERIFICATION experiment to be enabled
    if verification_result.is_terminate:
        _span.set_attribute("verification_path", "terminate_requested")
        LOG.warning(
            "CompleteAction verification determined task should terminate instead (termination-aware experiment)",
            workflow_run_id=task.workflow_run_id,
            thoughts=verification_result.thoughts,
            status=verification_result.status if verification_result.status else "legacy",
        )
        # Create a TerminateAction and execute it
        terminate_action = actions.TerminateAction(
            reasoning=verification_result.thoughts,
            organization_id=action.organization_id,
            workflow_run_id=action.workflow_run_id,
            task_id=action.task_id,
            step_id=action.step_id,
            step_order=action.step_order,
            action_order=action.action_order,
        )
        # Before the terminate runs, not after the rewrite below: once `action_type` is reassigned
        # in place the original projection is gone, and it is the TerminateAction that executes.
        preflight_derived_action(terminate_action, page, parent=action, site="complete_to_terminate")
        results = await handle_terminate_action(terminate_action, page, scraped_page, task, step)
        action.action_type = ActionType.TERMINATE
        action.reasoning = terminate_action.reasoning
        action.errors = terminate_action.errors
        return results

    if not verification_result.is_complete:
        _span.set_attribute("verification_path", "needs_llm_rejected")
        return [ActionFailure(exception=IllegitComplete(data={"error": verification_result.thoughts}))]

    _span.set_attribute("verification_path", "needs_llm_verified")
    LOG.info(
        "CompleteAction has been verified successfully",
        workflow_run_id=task.workflow_run_id,
    )
    action.verified = True

    return [ActionSuccess()]


@traced(name="skyvern.agent.action.extract")
async def handle_extract_action(
    action: actions.ExtractAction,
    page: Page,
    scraped_page: ScrapedPage,
    task: Task,
    step: Step,
) -> list[ActionResult]:
    extracted_data = None
    if task.data_extraction_goal:
        scrape_action_result = await extract_information_for_navigation_goal(
            scraped_page=scraped_page,
            task=task,
            step=step,
            page=page,
        )
        extracted_data = scrape_action_result.scraped_data
        return [ActionSuccess(data=extracted_data)]
    else:
        LOG.warning("No data extraction goal, skipping extract action")
        return [ActionFailure(exception=Exception("No data extraction goal"))]


@traced(name="skyvern.agent.action.scroll")
async def handle_scroll_action(
    action: actions.ScrollAction,
    page: Page,
    scraped_page: ScrapedPage,
    task: Task,
    step: Step,
) -> list[ActionResult]:
    if action.scroll_x is None or action.scroll_y is None:
        return [ActionFailure(Exception("ScrollAction is missing scroll_x/scroll_y coordinates"))]
    if action.element_id:
        # Element-based scrolling from extract-action prompt. Uses
        # scrollNearestScrollableContainer() from domUtils.js which walks the DOM to find
        # the nearest scrollable ancestor or sibling container relative to the element.
        # Returns: truthy value if scrolled (true for sub-container, "page" for page-level),
        # false if nothing was scrollable.
        scroll_direction = "down" if action.scroll_y >= 0 else "up"
        scroll_result = False
        dom = DomUtil(scraped_page=scraped_page, page=page)
        skyvern_element = await dom.safe_get_skyvern_element_by_id(action.element_id)
        if skyvern_element:
            try:
                scroll_result = await skyvern_element.locator.evaluate(
                    "(el, direction) => scrollNearestScrollableContainer(el, direction)",
                    scroll_direction,
                )
            except Exception:
                LOG.warning(
                    "JavaScript scroll evaluation failed, falling back to mouse wheel",
                    element_id=action.element_id,
                    exc_info=True,
                )
        else:
            LOG.warning("Could not resolve element for scroll action", element_id=action.element_id)

        if scroll_result == "page":
            # No scrollable sub-container found, but the page itself is scrollable.
            # Use incremental mouse.wheel events at the center of the viewport to
            # simulate natural user scrolling. This fires native wheel/scroll events
            # that page JavaScript (IntersectionObserver, scroll listeners, etc.) can
            # detect — unlike programmatic window.scrollTo() or keyboard shortcuts
            # which many pages ignore.
            LOG.info(
                "Page-level scroll, using mouse wheel at viewport center",
                element_id=action.element_id,
                direction=scroll_direction,
            )
            viewport = page.viewport_size
            center_x = viewport["width"] // 2 if viewport else 640
            center_y = viewport["height"] // 2 if viewport else 360
            await EventStrategyFactory.move_cursor(page, center_x, center_y)
            wheel_delta = 500 if scroll_direction == "down" else -500
            # Dynamically compute iterations based on remaining scrollable distance
            # so we reach the bottom even on very long T&C pages.
            scroll_info = await page.evaluate(
                "() => ({ scrollHeight: document.documentElement.scrollHeight,"
                " scrollTop: window.pageYOffset, innerHeight: window.innerHeight })"
            )
            if scroll_direction == "down":
                remaining = scroll_info["scrollHeight"] - scroll_info["scrollTop"] - scroll_info["innerHeight"]
            else:
                remaining = scroll_info["scrollTop"]
            iterations = max(1, min(int(remaining / abs(wheel_delta)) + 1, 50))
            LOG.info(
                "Page-level scroll iterations",
                remaining_px=remaining,
                iterations=iterations,
                wheel_delta=wheel_delta,
            )
            # Scroll per-iteration with page-reaction pauses between each chunk
            # (e.g. lazy-load, infinite scroll, dynamically enabled buttons).
            # Use raw page.mouse.wheel() here — the chunking + 100ms pauses already
            # provide a natural pattern, and applying the custom event strategy
            # per-iteration would add excessive latency per chunk.
            for _ in range(iterations):
                await page.mouse.wheel(0, wheel_delta)
                await page.wait_for_timeout(100)
            # Wait for page JS to process scroll events (e.g. enabling buttons)
            await page.wait_for_timeout(500)

            # Record which element was just deliberately scrolled. The click handler
            # checks this to skip scroll_into_view() for the SAME element, which
            # would use element.scrollIntoView() to center it — undoing the
            # scroll position that enables buttons on T&C pages. Using the element
            # ID (not a boolean) ensures unrelated clicks aren't affected.
            await page.evaluate(
                "(id) => { window.__skyvernScrolledElementId = id; }",
                action.element_id,
            )
            return [ActionSuccess(data={"page_level_scroll": True})]
        elif scroll_result:
            # Sub-container was scrolled successfully. Record the element ID so
            # the click handler skips scroll_into_view() for this element — same
            # protection as page-level scrolls. Without this, element.scrollIntoView()
            # would re-center the container and undo the deliberate scroll (e.g.,
            # scrolling a T&C modal to the bottom to enable an accept button).
            await page.evaluate(
                "(id) => { window.__skyvernScrolledElementId = id; }",
                action.element_id,
            )
            return [ActionSuccess(data={"container_scroll": True})]
        else:
            LOG.warning(
                "Could not find scrollable container near element, falling back to mouse wheel",
                element_id=action.element_id,
            )
            await EventStrategyFactory.scroll_by(page, action.scroll_x, action.scroll_y)
    elif action.x and action.y:
        # Coordinate-based scrolling from CUA/UI-TARS agents
        await EventStrategyFactory.move_cursor(page, action.x, action.y)
        await EventStrategyFactory.scroll_by(page, action.scroll_x, action.scroll_y)
    else:
        await EventStrategyFactory.scroll_by(page, action.scroll_x, action.scroll_y)
    return [ActionSuccess()]


@traced(name="skyvern.agent.action.keypress")
async def handle_keypress_action(
    action: actions.KeypressAction,
    page: Page,
    scraped_page: ScrapedPage,
    task: Task,
    step: Step,
) -> list[ActionResult]:
    await handler_utils.keypress(page, action.keys, hold=action.hold, duration=action.duration, repeat=action.repeat)
    return [ActionSuccess()]


async def _write_clipboard_text_in_isolated_world(page: Page, text: str) -> None:
    cdp_session = await page.context.new_cdp_session(page)
    try:
        frame_tree = await cdp_session.send("Page.getFrameTree")
        frame_id = frame_tree["frameTree"]["frame"]["id"]
        isolated_world = await cdp_session.send(
            "Page.createIsolatedWorld",
            {"frameId": frame_id, "worldName": "skyvern-paste-text"},
        )
        result = await cdp_session.send(
            "Runtime.callFunctionOn",
            {
                "functionDeclaration": (
                    "function(text) {"
                    "if (!navigator.clipboard) { throw new Error('navigator.clipboard is undefined'); }"
                    "return navigator.clipboard.writeText(text);"
                    "}"
                ),
                "arguments": [{"value": text}],
                "executionContextId": isolated_world["executionContextId"],
                "awaitPromise": True,
                "returnByValue": True,
            },
        )
        if "exceptionDetails" in result:
            exception_details = result["exceptionDetails"]
            description = (
                (exception_details.get("exception") or {}).get("description")
                or result.get("result", {}).get("description")
                or exception_details.get("text")
            )
            raise RuntimeError(description or "clipboard write failed")
    finally:
        with contextlib.suppress(Exception):
            await cdp_session.detach()


async def _clear_clipboard_after_paste(page: Page) -> bool:
    try:
        await _write_clipboard_text_in_isolated_world(page, "")
        return True
    except Exception:
        LOG.warning("paste_text: clipboard clear failed; retrying after navigation settles", exc_info=True)

    try:
        await page.wait_for_load_state("domcontentloaded", timeout=settings.BROWSER_ACTION_TIMEOUT_MS)
    except Exception:
        LOG.debug("paste_text: page did not settle before clipboard clear retry", exc_info=True)

    try:
        parsed = urllib.parse.urlparse(page.url)
        origin = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else None
        if origin is not None:
            await page.context.grant_permissions(["clipboard-write"], origin=origin)
        await _write_clipboard_text_in_isolated_world(page, "")
        return True
    except Exception:
        LOG.error(
            "paste_text: clipboard clear failed after navigation-aware retry; pasted text may remain on the clipboard",
            exc_info=True,
        )
        return False


@traced(name="skyvern.agent.action.paste_text")
async def handle_paste_text_action(
    action: actions.PasteTextAction,
    page: Page,
    scraped_page: ScrapedPage,
    task: Task,
    step: Step,
) -> list[ActionResult]:
    resolved_sensitive_text = False
    text_result = get_actual_value_of_parameter_if_secret_with_task(task, action.text)
    if text_result is None:
        return [ActionFailure(FailedToFetchSecret())]
    paste_text = text_result
    if task.workflow_run_id is not None:
        workflow_run_context = app.WORKFLOW_CONTEXT_MANAGER.get_workflow_run_context(task.workflow_run_id)
        if workflow_run_context is not None:
            resolved_sensitive_text = workflow_run_context.get_original_secret_value_or_none(action.text) is not None
            placeholder_tokens = workflow_run_context.find_embedded_placeholder_tokens(action.text)
            if placeholder_tokens:
                paste_text = action.text
                for token in placeholder_tokens:
                    if workflow_run_context.get_original_secret_value_or_none(token) is not None:
                        resolved_sensitive_text = True
                    token_value = get_actual_value_of_parameter_if_secret_with_task(task, token)
                    if token_value is None:
                        return [ActionFailure(FailedToFetchSecret())]
                    if is_unresolved_totp_placeholder(token_value):
                        return [ActionFailure(NoTOTPSecretFound())]
                    if is_unresolved_totp_value(token_value):
                        resolved_sensitive_text = True
                        try:
                            token_value = generate_totp_value_with_task(task, token)
                        except NoTOTPSecretFound as exc:
                            return [ActionFailure(exc)]
                    paste_text = paste_text.replace(token, token_value, 1)

    if is_unresolved_totp_placeholder(paste_text):
        return [ActionFailure(NoTOTPSecretFound())]
    if is_unresolved_totp_value(paste_text):
        resolved_sensitive_text = True
        try:
            paste_text = generate_totp_value_with_task(task, action.text)
        except NoTOTPSecretFound as exc:
            return [ActionFailure(exc)]

    if resolved_sensitive_text and not action.element_id:
        return [ActionFailure(MissingElement(element_id=action.element_id))]

    # Focus the anchor cell so the paste lands at the intended top-left position.
    if action.element_id:
        dom = DomUtil(scraped_page, page)
        skyvern_element = await dom.get_skyvern_element_by_id(action.element_id)
        locator = skyvern_element.get_locator()
        try:
            await locator.scroll_into_view_if_needed(timeout=settings.BROWSER_ACTION_TIMEOUT_MS)
        except Exception:
            LOG.debug("paste_text: scroll_into_view_if_needed failed", exc_info=True)
        # Best-effort focus: a grid's inline cell-editing box intercepts pointer events, so a strict
        # click times out. force clicks through, and a failure still lets the paste land at the
        # current selection.
        try:
            await locator.click(timeout=settings.BROWSER_ACTION_TIMEOUT_MS, force=True)
        except Exception as exc:
            if resolved_sensitive_text:
                LOG.warning("paste_text: refusing sensitive paste because target focus failed", exc_info=True)
                return [ActionFailure(exc)]
            LOG.debug("paste_text: focus click failed; pasting at current selection", exc_info=True)
        # Exit any inline cell editor the focus opened, then anchor the grid selection at the
        # top-left cell (Ctrl+Home) so the block distributes across cells from A1, rather than
        # landing in the formula/name bar as a single merged value.
        await handler_utils.keypress(page, ["Escape"])
        await handler_utils.keypress(page, ["ctrl", "Home"])

    # Canvas grid editors expose no per-cell DOM, so cell-by-cell typing truncates.
    # Set the clipboard and paste so a tab/newline-separated block fills the grid in one atomic operation.
    # Grant only clipboard write and scope it to this page's origin; skip when no origin can be determined
    # so later navigations in the same context never inherit a context-wide clipboard grant.
    try:
        parsed = urllib.parse.urlparse(page.url)
        origin = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else None
        if origin is None:
            LOG.debug(
                "paste_text: skipping clipboard permissions grant because page origin is unavailable", url=page.url
            )
        else:
            await page.context.grant_permissions(["clipboard-write"], origin=origin)
    except Exception:
        LOG.debug("paste_text: grant clipboard permissions failed (may already be granted)", exc_info=True)
    async with _PASTE_TEXT_CLIPBOARD_LOCK:
        # navigator.clipboard is undefined outside a secure context, so this throws on plain-http pages.
        try:
            await _write_clipboard_text_in_isolated_world(page, paste_text)
        except Exception as e:
            LOG.info("paste_text: clipboard write unavailable on this page", exc_info=True)
            return [ActionFailure(e)]
        try:
            await handler_utils.keypress(page, ["ControlOrMeta", "v"])
        finally:
            clipboard_cleared = await _clear_clipboard_after_paste(page)

    if resolved_sensitive_text and not clipboard_cleared:
        return [
            ActionResult(
                success=True,
                needs_followup=True,
                followup_message=SENSITIVE_CLIPBOARD_CLEAR_FAILED_FOLLOWUP_MESSAGE,
                skip_remaining_actions=True,
            )
        ]
    return [ActionSuccess()]


@traced(name="skyvern.agent.action.move")
async def handle_move_action(
    action: actions.MoveAction,
    page: Page,
    scraped_page: ScrapedPage,
    task: Task,
    step: Step,
) -> list[ActionResult]:
    if action.x is None or action.y is None:
        return [ActionFailure(Exception("MoveAction is missing x/y coordinates"))]
    await EventStrategyFactory.move_cursor(page, action.x, action.y)
    return [ActionSuccess()]


@traced(name="skyvern.agent.action.drag")
async def handle_drag_action(
    action: actions.DragAction,
    page: Page,
    scraped_page: ScrapedPage,
    task: Task,
    step: Step,
) -> list[ActionResult]:
    await handler_utils.drag(page, action.start_x, action.start_y, action.path)
    return [ActionSuccess()]


@traced(name="skyvern.agent.action.verification_code")
async def handle_verification_code_action(
    action: actions.VerificationCodeAction,
    page: Page,
    scraped_page: ScrapedPage,
    task: Task,
    step: Step,
) -> list[ActionResult]:
    LOG.info(
        "Setting verification code in skyvern context",
        verification_code=action.verification_code,
    )
    current_context = skyvern_context.ensure_context()
    current_context.totp_codes[task.task_id] = action.verification_code
    return [ActionSuccess()]


@traced(name="skyvern.agent.action.left_mouse")
async def handle_left_mouse_action(
    action: actions.LeftMouseAction,
    page: Page,
    scraped_page: ScrapedPage,
    task: Task,
    step: Step,
) -> list[ActionResult]:
    await handler_utils.left_mouse(page, action.x, action.y, action.direction)
    return [ActionSuccess()]


@traced(name="skyvern.agent.action.goto_url")
async def handle_goto_url_action(
    action: actions.GotoUrlAction,
    page: Page,
    scraped_page: ScrapedPage,
    task: Task,
    step: Step,
) -> list[ActionResult]:
    validated_url = await asyncio.to_thread(validate_fetch_url, action.url)
    response = await page.goto(validated_url, timeout=settings.BROWSER_LOADING_TIMEOUT_MS)
    await revalidate_redirect_chain(response, validate_fetch_url, page.goto)
    # Navigation invalidates the current scraped page's element ids; stop the batch so the
    # next step re-scrapes before any later actions run against the new DOM.
    result = ActionSuccess()
    result.skip_remaining_actions = True
    return [result]


async def handle_go_back_action(
    action: actions.GoBackAction,
    page: Page,
    scraped_page: ScrapedPage,
    task: Task,
    step: Step,
) -> list[ActionResult]:
    await page.go_back(timeout=settings.BROWSER_LOADING_TIMEOUT_MS)
    return [ActionSuccess()]


async def handle_go_forward_action(
    action: actions.GoForwardAction,
    page: Page,
    scraped_page: ScrapedPage,
    task: Task,
    step: Step,
) -> list[ActionResult]:
    await page.go_forward(timeout=settings.BROWSER_LOADING_TIMEOUT_MS)
    return [ActionSuccess()]


async def handle_reload_page_action(
    action: actions.ReloadPageAction,
    page: Page,
    scraped_page: ScrapedPage,
    task: Task,
    step: Step,
) -> list[ActionResult]:
    await page.reload(timeout=settings.BROWSER_LOADING_TIMEOUT_MS)
    # Reloading re-renders the DOM and invalidates the scraped page's element ids; stop the
    # batch so the next step re-scrapes before any later actions run.
    result = ActionSuccess()
    result.skip_remaining_actions = True
    return [result]


@traced(name="skyvern.agent.action.close_page")
async def handle_close_page_action(
    action: actions.ClosePageAction,
    page: Page,
    scraped_page: ScrapedPage,
    task: Task,
    step: Step,
) -> list[ActionResult]:
    target_page = page
    if action.tab_index is not None:
        browser_state = app.BROWSER_MANAGER.get_for_task(task.task_id, workflow_run_id=task.workflow_run_id)
        if browser_state is None:
            return [ActionFailure(Exception("No browser state found for the task"), stop_execution_on_failure=False)]
        pages = await browser_state.list_valid_pages()
        if action.tab_index < 0 or action.tab_index >= len(pages):
            return [
                ActionFailure(
                    Exception(f"CLOSE_PAGE tab_index {action.tab_index} is out of range (0-{len(pages) - 1})"),
                    stop_execution_on_failure=False,
                )
            ]
        target_page = pages[action.tab_index]
    await target_page.close(reason=action.reasoning)
    # Closing a tab shifts the remaining tab indices; stop the batch so the next step re-scrapes
    # and re-indexes the open tabs before any further close/switch action runs against stale indices.
    result = ActionSuccess()
    result.skip_remaining_actions = True
    return [result]


@traced(name="skyvern.agent.action.new_tab")
async def handle_new_tab_action(
    action: actions.NewTabAction,
    page: Page,
    scraped_page: ScrapedPage,
    task: Task,
    step: Step,
) -> list[ActionResult]:
    browser_state = app.BROWSER_MANAGER.get_for_task(task.task_id, workflow_run_id=task.workflow_run_id)
    if browser_state is None:
        return [ActionFailure(Exception("No browser state found for the task"), stop_execution_on_failure=False)]
    validated_url = await asyncio.to_thread(validate_fetch_url, action.url)
    new_page = await browser_state.new_page()
    try:
        await browser_state.navigate_to_url(page=new_page, url=validated_url)
    except Exception as e:
        # Don't leave a blank/failed tab as the newest page — the next scrape would fail it.
        try:
            await new_page.close()
        except Exception:
            LOG.debug("Failed to close new tab after navigation failure", exc_info=True)
        return [ActionFailure(e, stop_execution_on_failure=False)]
    await browser_state.set_active_page(new_page)
    try:
        await new_page.bring_to_front()
    except Exception:
        LOG.debug("Failed to bring new tab to front", exc_info=True)
    # The remaining batch was planned against the old tab's scraped page; stop here so the
    # next step re-scrapes the newly active tab.
    result = ActionSuccess()
    result.skip_remaining_actions = True
    return [result]


@traced(name="skyvern.agent.action.switch_tab")
async def handle_switch_tab_action(
    action: actions.SwitchTabAction,
    page: Page,
    scraped_page: ScrapedPage,
    task: Task,
    step: Step,
) -> list[ActionResult]:
    browser_state = app.BROWSER_MANAGER.get_for_task(task.task_id, workflow_run_id=task.workflow_run_id)
    if browser_state is None:
        return [ActionFailure(Exception("No browser state found for the task"), stop_execution_on_failure=False)]
    pages = await browser_state.list_valid_pages()
    if action.tab_index < 0 or action.tab_index >= len(pages):
        return [
            ActionFailure(
                Exception(f"SWITCH_TAB tab_index {action.tab_index} is out of range (0-{len(pages) - 1})"),
                stop_execution_on_failure=False,
            )
        ]
    target_page = pages[action.tab_index]
    await browser_state.set_active_page(target_page)
    try:
        await target_page.bring_to_front()
    except Exception:
        LOG.debug("Failed to bring switched tab to front", exc_info=True)
    # The remaining batch was planned against the previous tab; stop so the next step
    # re-scrapes the now-active tab.
    result = ActionSuccess()
    result.skip_remaining_actions = True
    return [result]


async def handle_execute_js_action(
    action: actions.ExecuteJsAction,
    page: Page,
    scraped_page: ScrapedPage,
    task: Task,
    step: Step,
) -> list[ActionResult]:
    import json as _json

    result = await evaluate_in_main_world(page, action.js_code)
    if result is None:
        return [ActionSuccess(data="undefined")]
    if isinstance(result, str):
        return [ActionSuccess(data=result)]
    return [ActionSuccess(data=_json.dumps(result))]


ActionHandler.register_action_type(ActionType.SOLVE_CAPTCHA, handle_solve_captcha_action)
ActionHandler.register_action_type(ActionType.CLICK, handle_click_action)
ActionHandler.register_action_type(ActionType.INPUT_TEXT, handle_input_text_action)
ActionHandler.register_action_type(ActionType.PASTE_TEXT, handle_paste_text_action)
ActionHandler.register_action_type(ActionType.UPLOAD_FILE, handle_upload_file_action)
ActionHandler.register_action_type(ActionType.DOWNLOAD_FILE, handle_download_file_action)
ActionHandler.register_action_type(ActionType.NULL_ACTION, handle_null_action)
ActionHandler.register_action_type(ActionType.SELECT_OPTION, handle_select_option_action)
ActionHandler.register_action_type(ActionType.WAIT, handle_wait_action)
ActionHandler.register_action_type(ActionType.HOVER, handle_hover_action)
ActionHandler.register_action_type(ActionType.TERMINATE, handle_terminate_action)
ActionHandler.register_action_type(ActionType.COMPLETE, handle_complete_action)
ActionHandler.register_action_type(ActionType.EXTRACT, handle_extract_action)
ActionHandler.register_action_type(ActionType.SCROLL, handle_scroll_action)
ActionHandler.register_action_type(ActionType.KEYPRESS, handle_keypress_action)
ActionHandler.register_action_type(ActionType.MOVE, handle_move_action)
ActionHandler.register_action_type(ActionType.DRAG, handle_drag_action)
ActionHandler.register_action_type(ActionType.VERIFICATION_CODE, handle_verification_code_action)
ActionHandler.register_action_type(ActionType.LEFT_MOUSE, handle_left_mouse_action)
ActionHandler.register_action_type(ActionType.GOTO_URL, handle_goto_url_action)
ActionHandler.register_action_type(ActionType.CLOSE_PAGE, handle_close_page_action)
ActionHandler.register_action_type(ActionType.NEW_TAB, handle_new_tab_action)
ActionHandler.register_action_type(ActionType.SWITCH_TAB, handle_switch_tab_action)
ActionHandler.register_action_type(ActionType.GO_BACK, handle_go_back_action)
ActionHandler.register_action_type(ActionType.GO_FORWARD, handle_go_forward_action)
ActionHandler.register_action_type(ActionType.RELOAD_PAGE, handle_reload_page_action)
ActionHandler.register_action_type(ActionType.EXECUTE_JS, handle_execute_js_action)


def get_actual_value_of_parameter_if_secret(workflow_run_id: str, parameter: str) -> Any:
    workflow_run_context = app.WORKFLOW_CONTEXT_MANAGER.get_workflow_run_context(workflow_run_id)
    secret_value = workflow_run_context.get_original_secret_value_or_none(parameter)
    if secret_value is not None:
        credential_parameter_key = workflow_run_context.find_credential_parameter_key_for_secret(parameter)
        if credential_parameter_key is None and secret_value != parameter:
            credential_parameter_key = _find_credential_key_for_embedded_placeholders(workflow_run_context, parameter)
        if credential_parameter_key is not None:
            current_context = skyvern_context.current()
            if current_context is not None:
                current_context.active_credential_parameter_key = credential_parameter_key
    return secret_value if secret_value is not None else parameter


def _find_credential_key_for_embedded_placeholders(workflow_run_context: Any, parameter: str) -> str | None:
    tokens = workflow_run_context.find_embedded_placeholder_tokens(parameter)
    if not tokens:
        return None
    keys: set[str | None] = set()
    for token in tokens:
        key = workflow_run_context.find_credential_parameter_key_for_secret(token)
        keys.add(key)
    keys.discard(None)
    return keys.pop() if len(keys) == 1 else None


def get_actual_value_of_parameter_if_secret_with_task(task: Task, parameter: str) -> Any:
    """
    Get the actual value of a parameter if it's a secret. If it's not a secret, return the parameter value as is.

    Just return the parameter value if the task isn't a workflow's task.

    This is only used for InputTextAction, UploadFileAction, and ClickAction (if it has a file_url).
    """
    if task.workflow_run_id is None:
        return parameter

    return get_actual_value_of_parameter_if_secret(task.workflow_run_id, parameter)


def get_totp_secret(workflow_run_id: str, parameter: str) -> str:
    workflow_run_context = app.WORKFLOW_CONTEXT_MANAGER.get_workflow_run_context(workflow_run_id)
    if not workflow_run_context:
        raise NoTOTPSecretFound()
    totp_secret_key = workflow_run_context.totp_secret_value_key(parameter)
    totp_secret = workflow_run_context.get_original_secret_value_or_none(totp_secret_key)
    if not totp_secret:
        LOG.warning("No TOTP secret found")
        raise NoTOTPSecretFound()
    if parse_totp_config(totp_secret) is None:
        LOG.warning("Failed to parse TOTP credential secret")
        raise NoTOTPSecretFound()
    return totp_secret


def get_totp_secret_with_task(task: Task, parameter: str) -> str:
    if task.workflow_run_id is None:
        raise NoTOTPSecretFound()
    return get_totp_secret(task.workflow_run_id, parameter)


def generate_totp_value_from_secret(totp_secret: str | None) -> str:
    if not totp_secret:
        raise NoTOTPSecretFound()
    try:
        return generate_totp_code(totp_secret)
    except Exception as exc:
        LOG.warning("Failed to generate TOTP from credential secret", exception_type=type(exc).__name__)
        raise NoTOTPSecretFound() from exc


def _register_runtime_otp_value_best_effort(workflow_run_id: str | None, code: str) -> None:
    if not workflow_run_id or not code:
        return
    try:
        app.WORKFLOW_CONTEXT_MANAGER.get_workflow_run_context(workflow_run_id).register_runtime_otp_value(code)
    except Exception:
        LOG.debug(
            "Failed to register runtime TOTP for redaction",
            workflow_run_id=workflow_run_id,
            exc_info=True,
        )


def generate_totp_value(workflow_run_id: str, parameter: str) -> str:
    code = generate_totp_value_from_secret(get_totp_secret(workflow_run_id, parameter))
    _register_runtime_otp_value_best_effort(workflow_run_id, code)
    return code


def generate_totp_value_with_task(task: Task, parameter: str) -> str:
    if task.workflow_run_id is None:
        raise NoTOTPSecretFound()
    return generate_totp_value(task.workflow_run_id, parameter)


async def _did_page_respond(
    incremental_scraped: IncrementalScrapePage,
    skyvern_frame: SkyvernFrame | None = None,
) -> bool:
    try:
        if skyvern_frame:
            await skyvern_frame.safe_wait_for_animation_end(caller="page_respond")
        return (await incremental_scraped.get_incremental_elements_num()) > 0
    except Exception:
        LOG.debug("Failed to check incremental elements after click", exc_info=True)
        return True


def _get_click_count(action: ClickAction | UploadFileAction) -> int:
    if isinstance(action, ClickAction):
        return action.repeat
    return 1


def _is_policy_blocked_host(error: Exception) -> bool:
    return isinstance(error, BlockedHost) and not isinstance(error, UnresolvableHost)


async def _locator_click(
    locator: Locator,
    click_count: int,
    timeout: int = settings.BROWSER_ACTION_TIMEOUT_MS,
    **kwargs: Any,
) -> None:
    if click_count == 2:
        await locator.dblclick(timeout=timeout, **kwargs)
    elif click_count >= 3:
        await locator.click(timeout=timeout, click_count=click_count, **kwargs)
    else:
        await locator.click(timeout=timeout, **kwargs)


async def chain_click(
    task: Task,
    scraped_page: ScrapedPage,
    page: Page,
    action: ClickAction | UploadFileAction,
    skyvern_element: SkyvernElement,
    pending_upload_files: list[str] | str | None = None,
    timeout: int = settings.BROWSER_ACTION_TIMEOUT_MS,
    incremental_scraped: IncrementalScrapePage | None = None,
    skyvern_frame: SkyvernFrame | None = None,
) -> List[ActionResult]:
    # Add a defensive page handler here in case a click action opens a file chooser.
    # This automatically dismisses the dialog
    # File choosers are impossible to close if you don't expect one. Instead of dealing with it, close it!

    dom = DomUtil(scraped_page=scraped_page, page=page)
    composite_source = skyvern_element
    composite_target_id = composite_source.get_id()
    skyvern_element = await dom.resolve_effective_click_target(composite_source)
    if skyvern_element.get_id() != composite_target_id and await skyvern_element.is_disabled(dynamic=True):
        return [ActionFailure(InteractWithDisabledElement(skyvern_element.get_id()))]
    locator = skyvern_element.locator
    click_count = _get_click_count(action)
    # TODO (suchintan): This should likely result in an ActionFailure -- we can figure out how to do this later!
    LOG.info("Chain click starts", action=action, locator=locator, sampling=True)
    file = pending_upload_files or []
    if not file and action.file_url:
        file_url = get_actual_value_of_parameter_if_secret_with_task(task, action.file_url)
        file = await handler_utils.download_file(file_url, action.model_dump(), task.organization_id)

    is_filechooser_trigger = False
    is_upload_action = bool(action.file_url)
    context = skyvern_context.current()
    has_pending = (
        context is not None and context.pending_file_chooser is not None and context.pending_file_chooser.page is page
    )

    if is_upload_action and has_pending and context is not None:
        LOG.info("New UPLOAD_FILE action arrived, cleaning up stale pending file chooser listener")
        context.cleanup_pending_file_chooser()
        has_pending = False

    async def fc_func(fc: FileChooser) -> None:
        nonlocal is_filechooser_trigger
        is_filechooser_trigger = True
        await fc.set_files(files=file)

    if not has_pending:
        page.on("filechooser", fc_func)
        LOG.info("Registered file chooser listener", action=action, path=file, sampling=True)
    else:
        LOG.info(
            "Skipping defensive file chooser listener — pending deferred listener exists",
            action=action,
        )

    """
    Clicks on an element identified by the css and its parent if failed.
    :param css: css of the element to click
    """
    # Pin the run's selected engine before the first dispatch so every
    # post-dispatch classification below uses one stable authority — a later
    # browser-state removal/replacement must not drift it.
    engine_selection = resolve_engine_selection_for_task(task, app.BROWSER_MANAGER)
    # Tracks the return value so the finally block can inspect click success.
    action_results: list[ActionResult] = []
    try:
        if not await skyvern_element.navigate_to_a_href(page=page):
            if resolved_href := await skyvern_element.resolve_http_href(page):
                await asyncio.to_thread(validate_fetch_url, resolved_href)
            if click_count == 1:
                # Route through the active cursor strategy so alternate profiles can
                # dispatch their own click sequence (explicit mouse.down/up).
                # Multi-click variants (dblclick / triple-click) still go through
                # _locator_click because they rely on Playwright's click_count arg.
                await EventStrategyFactory.click_element(page, locator, timeout=timeout)
            else:
                await EventStrategyFactory.move_to_element(page, locator)
                await _locator_click(locator, click_count, timeout=timeout)
            LOG.info("Chain click: main element click succeeded", action=action, locator=locator, sampling=True)
        action_results = [ActionSuccess()]
        return action_results

    except Exception as e:
        if is_post_dispatch_click_timeout(e, engine_selection):
            LOG.info(
                "Chain click: physical click dispatched; navigation-wait timed out — skipping fallback",
                action=action,
                locator=locator,
            )
            action_results = [ActionSuccess()]
            return action_results

        # The browser resolves through the run proxy and may reach hosts the worker cannot;
        # worker resolution failure is not a policy signal.
        if _is_policy_blocked_host(e):
            action_results = [ActionFailure(FailToClick(action.element_id, msg=str(e)))]
            return action_results

        action_results = [ActionFailure(FailToClick(action.element_id, msg=str(e)))]

        if skyvern_element.get_tag_name() == "label":
            try:
                LOG.info(
                    "Chain click: it's a label element. going to try for-click",
                    action=action,
                    element=str(skyvern_element),
                    locator=locator,
                )
                if bound_element := await skyvern_element.find_label_for(dom=dom):
                    await _locator_click(bound_element.get_locator(), click_count, timeout=timeout)
                    action_results.append(ActionSuccess())
                    return action_results
            except Exception as e:
                if is_post_dispatch_click_timeout(e, engine_selection):
                    LOG.info(
                        "Chain click: for-label fallback dispatched; navigation-wait timed out — skipping fallback",
                        action=action,
                        locator=locator,
                    )
                    action_results.append(ActionSuccess())
                    return action_results
                action_results.append(ActionFailure(FailToClick(action.element_id, anchor="for", msg=str(e))))

            try:
                # sometimes the element is the direct children of the label, instead of using for="xx" attribute
                # since it's a click action, the target element we're searching should only be INPUT
                LOG.info(
                    "Chain click: it's a label element. going to check for input of the direct children",
                    action=action,
                    element=str(skyvern_element),
                    locator=locator,
                )
                if bound_element := await skyvern_element.find_element_in_label_children(
                    dom=dom, element_type=InteractiveElement.INPUT
                ):
                    await _locator_click(bound_element.get_locator(), click_count, timeout=timeout)
                    action_results.append(ActionSuccess())
                    return action_results
            except Exception as e:
                if is_post_dispatch_click_timeout(e, engine_selection):
                    LOG.info(
                        "Chain click: label-children fallback dispatched; navigation-wait timed out — skipping fallback",
                        action=action,
                        locator=locator,
                    )
                    action_results.append(ActionSuccess())
                    return action_results
                action_results.append(
                    ActionFailure(FailToClick(action.element_id, anchor="direct_children", msg=str(e)))
                )

        else:
            try:
                LOG.info(
                    "Chain click: it's a non-label element. going to find the bound label element by attribute id and click",
                    action=action,
                    element=str(skyvern_element),
                    locator=locator,
                )
                if bound_locator := await skyvern_element.find_bound_label_by_attr_id():
                    # click on (0, 0) to avoid playwright clicking on the wrong element by accident
                    await _locator_click(bound_locator, click_count, timeout=timeout, position={"x": 0, "y": 0})
                    action_results.append(ActionSuccess())
                    return action_results
            except Exception as e:
                if is_post_dispatch_click_timeout(e, engine_selection):
                    LOG.info(
                        "Chain click: attr-id label fallback dispatched; navigation-wait timed out — skipping fallback",
                        action=action,
                        locator=locator,
                    )
                    action_results.append(ActionSuccess())
                    return action_results
                action_results.append(ActionFailure(FailToClick(action.element_id, anchor="attr_id", msg=str(e))))

            try:
                # sometimes the element is the direct children of the label, instead of using for="xx" attribute
                # so we check the direct parent if it's a label element
                LOG.info(
                    "Chain click: it's a non-label element. going to find the bound label element by direct parent",
                    action=action,
                    element=str(skyvern_element),
                    locator=locator,
                )
                if bound_locator := await skyvern_element.find_bound_label_by_direct_parent():
                    # click on (0, 0) to avoid playwright clicking on the wrong element by accident
                    await _locator_click(bound_locator, click_count, timeout=timeout, position={"x": 0, "y": 0})
                    action_results.append(ActionSuccess())
                    return action_results
            except Exception as e:
                if is_post_dispatch_click_timeout(e, engine_selection):
                    LOG.info(
                        "Chain click: direct-parent label fallback dispatched; navigation-wait timed out — skipping fallback",
                        action=action,
                        locator=locator,
                    )
                    action_results.append(ActionSuccess())
                    return action_results
                action_results.append(ActionFailure(FailToClick(action.element_id, anchor="direct_parent", msg=str(e))))

        if not await skyvern_element.is_visible():
            LOG.info(
                "Chain click: exit since the element is not visible on the page anymore",
                action=action,
                element=str(skyvern_element),
                locator=locator,
            )
            return action_results

        blocking_element, blocked = await skyvern_element.find_blocking_element(
            dom=DomUtil(scraped_page=scraped_page, page=page)
        )
        verify_checkbox_toggle = click_count == 1 and await skyvern_element.is_checkbox()
        skip_coordinate_click = False
        if verify_checkbox_toggle and blocking_element is not None:
            if not await blocking_element.is_safe_for_checkbox_direct_click():
                LOG.info(
                    "Chain click: skipping unsafe or unknown blocker click for checkbox",
                    action=action,
                    element=str(blocking_element),
                    locator=locator,
                )
                blocking_element = None
                skip_coordinate_click = True

        if blocking_element is None:
            if blocked:
                LOG.info(
                    "Chain click: element is blocked by a non-interactable element, evaluating fallback",
                    action=action,
                    element=str(skyvern_element),
                    locator=locator,
                )
                # An untracked overlay is intercepting an anchor: dispatching a
                # coordinate click can trigger overlay JS that navigates away.
                # Follow the anchor's ``href`` directly when it is a plain http
                # link; skip uploads, explicit coordinate clicks, and downloads
                # (JS-driven downloads may build a blob/POST on click and would
                # fetch the wrong static resource via ``frame.goto(href)``).
                if (
                    isinstance(action, ClickAction)
                    and not action.file_url
                    and not action.download
                    and action.x is None
                    and action.y is None
                    and skyvern_element.get_tag_name() == InteractiveElement.A
                ):
                    try:
                        navigated_href = await skyvern_element.try_navigate_via_href(page=page)
                    except BlockedHost as e:
                        if _is_policy_blocked_host(e):
                            action_results = [ActionFailure(FailToClick(action.element_id, msg=str(e)))]
                            return action_results
                    else:
                        if navigated_href:
                            LOG.info(
                                "Chain click: bypassed coordinate fallback via direct href navigation",
                                action=action,
                                element=str(skyvern_element),
                                href=navigated_href,
                            )
                            action_results.append(ActionSuccess())
                            return action_results
            else:
                # Element is visible and elementFromPoint returns the target itself,
                # but Playwright's click still failed (e.g. element transiently
                # unstable due to React re-render or CSS animation).  Fall through
                # to coordinate click which bypasses Playwright's actionability
                # checks while still dispatching a real mouse event.
                LOG.info(
                    "Chain click: element is visible and not blocked, but Playwright click failed — trying coordinate click",
                    action=action,
                    element=str(skyvern_element),
                    locator=locator,
                )

            # Only for a single click does "state unchanged" reliably mean "the
            # click was a no-op": any repeated click toggles the checkbox more
            # than once, so its final state is not a dependable no-op signal.
            # Gate the checkbox verification on a single click and fold it into
            # the shared coordinate -> JS ladder below instead of a parallel one.
            checked_before = await skyvern_element.is_checked(timeout=timeout) if verify_checkbox_toggle else None

            coordinate_error: Exception | None = None
            if not skip_coordinate_click:
                try:
                    skyvern_element = await dom.resolve_effective_click_target(composite_source)
                    await skyvern_element.coordinate_click(page=page, click_count=click_count)
                except Exception as e:
                    coordinate_error = e

            if verify_checkbox_toggle:
                checked_after = await skyvern_element.is_checked(timeout=timeout)
                state_known = checked_before is not None and checked_after is not None
                if state_known and checked_after != checked_before:
                    action_results.append(ActionSuccess())
                    return action_results
                if not state_known:
                    # Unknown post-click state (detached/navigated): a second
                    # click risks a double toggle, so never fall through to JS.
                    # A real coordinate click that then lost the element is the
                    # legacy success case; when the coordinate click was skipped
                    # (unsafe blocker) or errored, fail closed instead.
                    if coordinate_error is None and not skip_coordinate_click:
                        action_results.append(ActionSuccess())
                    else:
                        action_results.append(
                            ActionFailure(
                                FailToClick(
                                    action.element_id,
                                    anchor="coordinate_click",
                                    msg=(
                                        str(coordinate_error)
                                        if coordinate_error is not None
                                        else "checkbox state unknown after coordinate click"
                                    ),
                                )
                            )
                        )
                    return action_results
                # State known and unchanged: a provable no-op, safe to JS-click.
            elif coordinate_error is None:
                action_results.append(ActionSuccess())
                return action_results
            else:
                action_results.append(
                    ActionFailure(FailToClick(action.element_id, anchor="coordinate_click", msg=str(coordinate_error)))
                )

            LOG.info(
                "Chain click: coordinate click failed, going to use javascript click instead of playwright click",
                action=action,
                element=str(skyvern_element),
                locator=locator,
            )
            try:
                skyvern_element = await dom.resolve_effective_click_target(composite_source)
                await skyvern_element.click_in_javascript()
            except Exception as e:
                action_results.append(ActionFailure(FailToClick(action.element_id, anchor="self_js", msg=str(e))))
                return action_results

            if verify_checkbox_toggle:
                checked_after_js = await skyvern_element.is_checked(timeout=timeout)
                if checked_after_js is None or checked_after_js == checked_before:
                    action_results.append(
                        ActionFailure(
                            FailToClick(
                                action.element_id,
                                anchor="self_js",
                                msg="checkbox state unchanged after coordinate and JS click",
                            )
                        )
                    )
                    return action_results

            action_results.append(ActionSuccess())
            return action_results

        try:
            LOG.debug(
                "Chain click: verifying the blocking element is parent or sibling of the target element",
                action=action,
                element=str(blocking_element),
                locator=locator,
            )
            if await blocking_element.is_parent_of(
                await skyvern_element.get_element_handler()
            ) or await blocking_element.is_sibling_of(await skyvern_element.get_element_handler()):
                LOG.info(
                    "Chain click: element is blocked by other elements, going to click on the blocking element",
                    action=action,
                    element=str(blocking_element),
                    locator=locator,
                )

                await blocking_element.get_locator().click(timeout=timeout)
                action_results.append(ActionSuccess())
                return action_results
        except Exception as e:
            if is_post_dispatch_click_timeout(e, engine_selection):
                LOG.info(
                    "Chain click: blocking-element fallback dispatched; navigation-wait timed out — skipping fallback",
                    action=action,
                    locator=locator,
                )
                action_results.append(ActionSuccess())
                return action_results
            action_results.append(ActionFailure(FailToClick(action.element_id, anchor="blocking_element", msg=str(e))))

        # Only attempt JS click when the caller provided an observer to verify
        # the result.  Without one we can't distinguish success from a no-op,
        # so preserve the old behavior (return accumulated failures).
        if incremental_scraped is None:
            return action_results

        # JS click dispatches directly on the DOM node, bypassing hit-testing.
        LOG.info(
            "Chain click: blocker is not parent/sibling, trying JS click on original element",
            action=action,
            element=str(skyvern_element),
            locator=locator,
        )
        try:
            await skyvern_element.click_in_javascript()
            if await _did_page_respond(incremental_scraped, skyvern_frame):
                action_results.append(ActionSuccess())
                return action_results
            LOG.info(
                "Chain click: JS click did not trigger a page response",
                action=action,
                element=str(skyvern_element),
            )
            action_results.append(
                ActionFailure(FailToClick(action.element_id, anchor="self_js", msg="no page response after click"))
            )
            return action_results
        except Exception as e:
            action_results.append(ActionFailure(FailToClick(action.element_id, anchor="self_js", msg=str(e))))
            return action_results

    finally:
        click_succeeded = any(isinstance(r, ActionSuccess) for r in action_results)

        if is_filechooser_trigger:
            # File chooser opened during this click — upload completed normally
            LOG.info("File chooser triggered during this click", action=action)
            if file:
                await _wait_for_upload_processing(page, engine_selection=engine_selection)
            if not has_pending:
                page.remove_listener("filechooser", fc_func)
            if context is not None and context.pending_file_chooser is not None:
                context.cleanup_pending_file_chooser()

        elif is_upload_action and file and click_succeeded and context is not None:
            # UPLOAD_FILE click succeeded but file chooser didn't open (e.g. popup intercepted).
            # Defer the listener so a subsequent click can trigger it.
            if not has_pending:
                page.remove_listener("filechooser", fc_func)
            LOG.warning(
                "UPLOAD_FILE click succeeded but file chooser was not triggered — deferring listener",
                action=action,
            )
            # Clean up any existing pending listener (may be on a different page)
            if context.pending_file_chooser is not None:
                context.cleanup_pending_file_chooser()

            pending = PendingFileChooserListener(page=page, file_paths=file)

            async def deferred_fc_handler(fc: FileChooser) -> None:
                pending.triggered = True
                await fc.set_files(files=pending.file_paths)
                # Auto-remove after firing to prevent double-consumption
                pending.cleanup()

            pending.handler = deferred_fc_handler
            page.on("filechooser", deferred_fc_handler)
            context.pending_file_chooser = pending

        elif (
            context is not None and context.pending_file_chooser is not None and context.pending_file_chooser.triggered
        ):
            # A previous UPLOAD_FILE's deferred listener was consumed by this click
            LOG.info("Pending file chooser from previous UPLOAD_FILE was consumed by this click", action=action)
            await _wait_for_upload_processing(page, engine_selection=engine_selection)
            context.cleanup_pending_file_chooser()

        else:
            # No file chooser involved — just clean up the defensive listener
            if not has_pending:
                page.remove_listener("filechooser", fc_func)

        if is_upload_action:
            for r in action_results:
                if isinstance(r, ActionSuccess):
                    r.upload_file_triggered = is_filechooser_trigger
                    if not is_filechooser_trigger:
                        r.needs_followup = True
                        r.followup_message = UPLOAD_PENDING_FOLLOWUP_MESSAGE


@traced(name="skyvern.agent.dropdown.auto_completion")
async def choose_auto_completion_dropdown(
    context: InputOrSelectContext,
    page: Page,
    scraped_page: ScrapedPage,
    dom: DomUtil,
    text: str,
    skyvern_element: SkyvernElement,
    step: Step,
    task: Task,
    preserved_elements: list[dict] | None = None,
    relevance_threshold: float = 0.8,
    is_location_input: bool = False,
    collapse_autocomplete_fanout_enabled: bool = False,
    action: InputTextAction | None = None,
    *,
    is_secret_value: bool,
) -> AutoCompletionResult:
    preserved_elements = preserved_elements or []
    clear_input = True
    result = AutoCompletionResult()
    engine_selection = resolve_engine_selection_for_task(task, app.BROWSER_MANAGER)

    current_frame = skyvern_element.get_frame()
    skyvern_frame = await SkyvernFrame.create_instance(current_frame, engine_selection=engine_selection)
    incremental_scraped = IncrementalScrapePage(skyvern_frame=skyvern_frame, engine_selection=engine_selection)
    await incremental_scraped.start_listen_dom_increment(await skyvern_element.get_element_handler())

    try:
        await skyvern_element.press_fill(text)
        # wait for new elemnts to load
        await skyvern_frame.safe_wait_for_animation_end(before_wait_sec=1, caller="autocomplete.fill")
        incremental_element = await incremental_scraped.get_incremental_element_tree(
            clean_and_remove_element_tree_factory(
                task=task,
                step=step,
                check_filter_funcs=[check_existed_but_not_option_element_in_dom_factory(dom)],
                engine_selection=engine_selection,
            ),
        )

        # check if elements in preserve list are still on the page
        confirmed_preserved_list: list[dict] = []
        for element in preserved_elements:
            element_id = element.get("id")
            if not element_id:
                continue
            locator = current_frame.locator(f'[{SKYVERN_ID_ATTR}="{element_id}"]')
            cnt = await locator.count()
            if cnt == 0:
                continue

            element_handler = await locator.element_handle(timeout=settings.BROWSER_ACTION_TIMEOUT_MS)
            if not element_handler:
                continue

            current_element = await skyvern_frame.parse_element_from_html(
                skyvern_element.get_frame_id(),
                element_handler,
                skyvern_element.is_interactable(),
            )
            confirmed_preserved_list.append(current_element)

        if len(confirmed_preserved_list) > 0:
            confirmed_preserved_list = await app.AGENT_FUNCTION.cleanup_element_tree_factory(
                task=task, step=step, engine_selection=engine_selection
            )(skyvern_frame.get_frame(), skyvern_frame.get_frame().url, copy.deepcopy(confirmed_preserved_list))
            confirmed_preserved_list = trim_element_tree(copy.deepcopy(confirmed_preserved_list))

        incremental_element.extend(confirmed_preserved_list)

        result.incremental_elements = copy.deepcopy(incremental_element)
        html = ""
        new_interactable_element_ids: list[str] = []
        shadow_candidate_elements: list[dict] = []
        if len(incremental_element) > 0:
            cleaned_incremental_element = remove_duplicated_HTML_element(incremental_element)
            shadow_candidate_elements = cleaned_incremental_element

            if collapse_autocomplete_fanout_enabled and not context.is_search_bar:
                # Resolve against the raw elements so duplicate labels under distinct
                # element IDs remain ambiguous instead of being collapsed away.
                deterministic_match = _resolve_autocomplete_candidate(text, incremental_element)
                if deterministic_match is not None:
                    matched_index, matched_candidate = deterministic_match
                    matched_element_id = matched_candidate.get("element_id") or ""
                    matched_label = matched_candidate.get("label") or ""
                    matched_locator = current_frame.locator(f'[{SKYVERN_ID_ATTR}="{matched_element_id}"]')
                    if matched_element_id and matched_label and await matched_locator.count() > 0:
                        option_identity_matches = await _verify_autocomplete_option_identity(
                            skyvern_frame=skyvern_frame,
                            locator=matched_locator,
                            matched_index=matched_index,
                            matched_label=matched_label,
                        )
                        if not option_identity_matches:
                            LOG.info(
                                "Autocomplete deterministic option identity failed, resetting input before LLM fallback",
                                element_id=matched_element_id,
                                matched_index=matched_index,
                                matched_label=matched_label,
                            )
                            (
                                incremental_scraped,
                                fallback_incremental_elements,
                                shadow_candidate_elements,
                                html,
                                new_interactable_element_ids,
                            ) = await _reset_autocomplete_for_llm_fallback(
                                current_incremental_scraped=incremental_scraped,
                                skyvern_frame=skyvern_frame,
                                skyvern_element=skyvern_element,
                                page=page,
                                scraped_page=scraped_page,
                                dom=dom,
                                text=text,
                                task=task,
                                step=step,
                                engine_selection=engine_selection,
                            )
                            result.incremental_elements = copy.deepcopy(fallback_incremental_elements)
                            cleaned_incremental_element = shadow_candidate_elements
                        else:
                            LOG.info(
                                "Autocomplete deterministic fast path: exact/stem option found, skipping LLM",
                                element_id=matched_element_id,
                                input_value=text,
                                matched_index=matched_index,
                                matched_label=matched_label,
                            )
                            try:
                                await matched_locator.click(timeout=settings.BROWSER_ACTION_TIMEOUT_MS)
                                if await _verify_autocomplete_input_readback(
                                    skyvern_element=skyvern_element,
                                    matched_index=matched_index,
                                    matched_label=matched_label,
                                    engine_selection=engine_selection,
                                ):
                                    clear_input = False
                                    result.action_result = ActionSuccess()
                                    return result
                                LOG.info(
                                    "Autocomplete deterministic read-back failed, resetting input before LLM fallback",
                                    element_id=matched_element_id,
                                    matched_index=matched_index,
                                    matched_label=matched_label,
                                )
                            except Exception:
                                LOG.info(
                                    "Autocomplete deterministic fast-path click/read-back failed, falling through to LLM",
                                    element_id=matched_element_id,
                                    matched_index=matched_index,
                                    matched_label=matched_label,
                                    exc_info=True,
                                )
                            (
                                incremental_scraped,
                                fallback_incremental_elements,
                                shadow_candidate_elements,
                                html,
                                new_interactable_element_ids,
                            ) = await _reset_autocomplete_for_llm_fallback(
                                current_incremental_scraped=incremental_scraped,
                                skyvern_frame=skyvern_frame,
                                skyvern_element=skyvern_element,
                                page=page,
                                scraped_page=scraped_page,
                                dom=dom,
                                text=text,
                                task=task,
                                step=step,
                                engine_selection=engine_selection,
                            )
                            result.incremental_elements = copy.deepcopy(fallback_incremental_elements)
                            cleaned_incremental_element = shadow_candidate_elements
                    else:
                        # The deterministic candidate detached before it could be clicked;
                        # re-open the dropdown so the LLM fallback sees the live options
                        # instead of the stale captured scrape that still lists it.
                        LOG.info(
                            "Autocomplete deterministic option detached before click, resetting input before LLM fallback",
                            element_id=matched_element_id,
                            matched_index=matched_index,
                            matched_label=matched_label,
                        )
                        (
                            incremental_scraped,
                            fallback_incremental_elements,
                            shadow_candidate_elements,
                            html,
                            new_interactable_element_ids,
                        ) = await _reset_autocomplete_for_llm_fallback(
                            current_incremental_scraped=incremental_scraped,
                            skyvern_frame=skyvern_frame,
                            skyvern_element=skyvern_element,
                            page=page,
                            scraped_page=scraped_page,
                            dom=dom,
                            text=text,
                            task=task,
                            step=step,
                            engine_selection=engine_selection,
                        )
                        result.incremental_elements = copy.deepcopy(fallback_incremental_elements)
                        cleaned_incremental_element = shadow_candidate_elements

            # Fast path for location inputs: if exactly one option appeared and it contains
            # what the user typed, click it directly without an LLM call. Preserve the legacy
            # location behavior when the broader collapse flag is disabled.
            if not collapse_autocomplete_fanout_enabled and is_location_input and len(cleaned_incremental_element) == 1:
                only_element = cleaned_incremental_element[0]
                fast_path_element_id = only_element.get("id", "")
                # Normalize whitespace for comparison (handles double spaces, etc.)
                option_text = " ".join((only_element.get("text") or "").lower().split())
                input_normalized = " ".join(text.lower().split())
                if fast_path_element_id and input_normalized and input_normalized in option_text:
                    fast_path_locator = current_frame.locator(f'[{SKYVERN_ID_ATTR}="{fast_path_element_id}"]')
                    if await fast_path_locator.count() > 0:
                        LOG.info(
                            "Location auto-completion fast path: single option found, skipping LLM",
                            element_id=fast_path_element_id,
                            input_value=text,
                        )
                        try:
                            await fast_path_locator.click(timeout=settings.BROWSER_ACTION_TIMEOUT_MS)
                            clear_input = False
                            result.action_result = ActionSuccess()
                            return result
                        except Exception:
                            LOG.info(
                                "Location fast-path click failed, falling through to LLM",
                                element_id=fast_path_element_id,
                            )

            if not html:
                html = incremental_scraped.build_html_tree(cleaned_incremental_element)
        else:
            scraped_page_after_open = await scraped_page.generate_scraped_page_without_screenshots()
            new_element_ids = set(scraped_page_after_open.id_to_css_dict.keys()) - set(
                scraped_page.id_to_css_dict.keys()
            )

            dom_after_open = DomUtil(scraped_page=scraped_page_after_open, page=page)
            new_interactable_element_ids = [
                element_id
                for element_id in new_element_ids
                if (await dom_after_open.get_skyvern_element_by_id(element_id)).is_interactable()
            ]
            if len(new_interactable_element_ids) == 0:
                raise NoIncrementalElementFoundForAutoCompletion(element_id=skyvern_element.get_id(), text=text)
            LOG.info(
                "New elements detected after the input",
                new_elements_ids=new_interactable_element_ids,
            )
            result.incremental_elements = copy.deepcopy(
                [scraped_page_after_open.id_to_element_dict[element_id] for element_id in new_interactable_element_ids]
            )
            shadow_candidate_elements = result.incremental_elements
            html = scraped_page_after_open.build_element_tree()

        if collapse_autocomplete_fanout_enabled and action is not None:
            action.set_has_mini_agent()

        slim_output = await get_slim_output_template_value("auto-completion-choose-option")
        auto_completion_confirm_prompt = prompt_engine.load_prompt(
            "auto-completion-choose-option",
            is_search=context.is_search_bar,
            field_information=context.field if not context.intention else context.intention,
            filled_value=text,
            navigation_goal=task.navigation_goal,
            navigation_payload_str=json.dumps(task.navigation_payload),
            elements=html,
            new_elements_ids=new_interactable_element_ids,
            local_datetime=datetime.now(skyvern_context.ensure_context().tz_info).isoformat(),
            slim_output=slim_output,
        )
        LOG.info("Confirm if it's an auto completion dropdown", sampling=True)
        json_response = await get_org_aware_secondary_llm_api_handler(default=app.AUTO_COMPLETION_LLM_API_HANDLER)(
            prompt=auto_completion_confirm_prompt, step=step, prompt_name="auto-completion-choose-option"
        )
        element_id = json_response.get("id", "")
        relevance_float = json_response.get("relevance_float", 0)
        _log_select_shadow_match(
            prompt_name="auto-completion-choose-option",
            target_value=text,
            get_candidates=lambda: _select_shadow_candidates_from_elements(shadow_candidate_elements),
            agreement=lambda candidates, matched_index: _select_shadow_agrees_with_element_choice(
                candidates,
                matched_index,
                llm_element_id=element_id or None,
                llm_value=json_response.get("value"),
            ),
        )
        if json_response.get("direct_searching", False):
            LOG.info(
                "Decided to directly search with the current value",
                value=text,
            )
            await skyvern_element.press_key("Enter")
            clear_input = False
            return result

        if not element_id:
            reasoning = json_response.get("reasoning")
            raise NoSuitableAutoCompleteOption(reasoning=reasoning, target_value=text)

        if relevance_float < relevance_threshold:
            LOG.info(
                f"The closest option doesn't meet the condition(relevance_float>={relevance_threshold})",
                element_id=element_id,
                relevance_float=relevance_float,
            )
            reasoning = json_response.get("reasoning")
            raise NoAutoCompleteOptionMeetCondition(
                reasoning=reasoning,
                required_relevance=relevance_threshold,
                target_value=text,
                closest_relevance=relevance_float,
            )

        LOG.info(
            "Find a suitable option to choose",
            element_id=element_id,
        )

        locator = current_frame.locator(f'[{SKYVERN_ID_ATTR}="{element_id}"]')
        if await locator.count() == 0:
            raise MissingElement(element_id=element_id)

        # Use SkyvernElement.click() so we get the full fallback chain
        # (Playwright click → coordinate click → JavaScript click).  Plain
        # locator.click() can fail when the item or one of its ancestors has
        # pointer-events:none, which is common in React/Vue dropdown lists.
        selected_element = SkyvernElement(
            locator=locator,
            frame=current_frame,
            static_element=incremental_scraped.id_to_element_dict.get(element_id, {}),
            engine_selection=engine_selection,
        )

        async def _click_selected_option() -> None:
            await selected_element.scroll_into_view()
            await selected_element.click(page=page, engine_selection=engine_selection)

        result.action_result = await _click_autocomplete_option_with_commit_evidence(
            skyvern_element=skyvern_element,
            option_locator=locator,
            option_static_element=incremental_scraped.id_to_element_dict.get(element_id),
            skyvern_frame=skyvern_frame,
            click=_click_selected_option,
            is_secret_value=is_secret_value,
            engine_selection=engine_selection,
        )
        clear_input = False
        return result

    except Exception as e:
        LOG.info(
            "Failed to choose the auto completion dropdown",
            sampling=True,
            exc_info=True,
            input_value=text,
        )
        result.action_result = ActionFailure(exception=e)
        return result
    finally:
        await incremental_scraped.stop_listen_dom_increment()
        if clear_input and await skyvern_element.is_visible():
            try:
                await skyvern_element.input_clear()
            except Exception:
                # Best-effort cleanup of the probe text typed above; an exception raised here
                # (e.g. InvalidElementForTextInput when the live node no longer matches the
                # scraped tag) would otherwise escape this finally block and clobber whatever
                # `result`/exception the try/except above already produced, denying the
                # caller's designed fallback chain (retry -> full-dropdown discovery -> plain fill).
                LOG.info(
                    "Failed to clear the auto-completion probe text, but continue",
                    element_id=skyvern_element.get_id(),
                )


def remove_duplicated_HTML_element(elements: list[dict]) -> list[dict]:
    cache_map = set()
    new_elements: list[dict] = []
    for element in elements:
        key = hash_element(element=element)
        if key in cache_map:
            continue
        cache_map.add(key)
        new_elements.append(element)
    return new_elements


async def input_or_auto_complete_input(
    input_or_select_context: InputOrSelectContext,
    scraped_page: ScrapedPage,
    page: Page,
    dom: DomUtil,
    text: str,
    skyvern_element: SkyvernElement,
    step: Step,
    task: Task,
    action: InputTextAction | None = None,
    collapse_autocomplete_fanout_enabled: bool = False,
    *,
    is_secret_value: bool,
) -> ActionResult | None:
    LOG.info(
        "Trigger auto completion",
        element_id=skyvern_element.get_id(),
    )

    # 1. press the original text to see if there's a match
    # 2. call LLM to find 5 potential values based on the orginal text
    # 3. try each potential values from #2
    # 4. call LLM to tweak the original text according to the information from #3, then start #1 again

    # FIXME: try the whole loop for once now, to speed up skyvern
    MAX_AUTO_COMPLETE_ATTEMP = 1
    current_attemp = 0
    current_value = text
    result = AutoCompletionResult()

    while current_attemp < MAX_AUTO_COMPLETE_ATTEMP:
        current_attemp += 1
        whole_new_elements: list[dict] = []
        tried_values: list[str] = []

        LOG.info(
            "Try the potential value for auto completion",
            sampling=True,
            input_value=current_value,
        )
        is_location = input_or_select_context.is_location_input or False
        result = await choose_auto_completion_dropdown(
            context=input_or_select_context,
            page=page,
            scraped_page=scraped_page,
            dom=dom,
            text=current_value,
            preserved_elements=result.incremental_elements,
            skyvern_element=skyvern_element,
            step=step,
            task=task,
            is_location_input=is_location,
            collapse_autocomplete_fanout_enabled=collapse_autocomplete_fanout_enabled,
            action=action,
            is_secret_value=is_secret_value,
        )
        if isinstance(result.action_result, ActionSuccess):
            return result.action_result

        if input_or_select_context.is_search_bar:
            LOG.info(
                "Stop generating potential values for the auto-completion since it's a search bar",
                context=input_or_select_context,
            )
            return None

        tried_values.append(current_value)
        whole_new_elements.extend(result.incremental_elements)

        field_information = (
            input_or_select_context.field
            if not input_or_select_context.intention
            else input_or_select_context.intention
        )

        prompt = prompt_engine.load_prompt(
            "auto-completion-potential-answers",
            potential_value_count=AUTO_COMPLETION_POTENTIAL_VALUES_COUNT,
            field_information=field_information,
            current_value=current_value,
            navigation_goal=task.navigation_goal,
            navigation_payload_str=json.dumps(task.navigation_payload),
            local_datetime=datetime.now(skyvern_context.ensure_context().tz_info).isoformat(),
        )

        LOG.info(
            "Ask LLM to give potential values based on the current value",
            current_value=current_value,
            potential_value_count=AUTO_COMPLETION_POTENTIAL_VALUES_COUNT,
        )
        if collapse_autocomplete_fanout_enabled and action is not None:
            action.set_has_mini_agent()
        json_respone = await get_org_aware_secondary_llm_api_handler(default=app.SECONDARY_LLM_API_HANDLER)(
            prompt=prompt, step=step, prompt_name="auto-completion-potential-answers"
        )
        values: list[dict] = json_respone.get("potential_values", [])

        for each_value in values:
            value: str = each_value.get("value", "")
            if not value:
                LOG.info(
                    "Empty potential value, skip this attempt",
                    value=each_value,
                )
                continue
            LOG.info(
                "Try the potential value for auto completion",
                sampling=True,
                input_value=value,
            )
            result = await choose_auto_completion_dropdown(
                context=input_or_select_context,
                page=page,
                scraped_page=scraped_page,
                dom=dom,
                text=value,
                preserved_elements=result.incremental_elements,
                skyvern_element=skyvern_element,
                step=step,
                task=task,
                is_location_input=is_location,
                collapse_autocomplete_fanout_enabled=collapse_autocomplete_fanout_enabled,
                action=action,
                is_secret_value=is_secret_value,
            )
            if isinstance(result.action_result, ActionSuccess):
                return result.action_result

            tried_values.append(value)
            whole_new_elements.extend(result.incremental_elements)

        # WARN: currently, we don't trigger this logic because MAX_AUTO_COMPLETE_ATTEMP is 1, to speed up skyvern
        if current_attemp < MAX_AUTO_COMPLETE_ATTEMP:
            LOG.info(
                "Ask LLM to tweak the current value based on tried input values",
                current_value=current_value,
                current_attemp=current_attemp,
            )
            cleaned_new_elements = remove_duplicated_HTML_element(whole_new_elements)
            prompt = prompt_engine.load_prompt(
                "auto-completion-tweak-value",
                field_information=field_information,
                current_value=current_value,
                navigation_goal=task.navigation_goal,
                navigation_payload_str=json.dumps(task.navigation_payload),
                tried_values=json.dumps(tried_values),
                popped_up_elements="".join([json_to_html(element) for element in cleaned_new_elements]),
                local_datetime=datetime.now(skyvern_context.ensure_context().tz_info).isoformat(),
            )
            json_respone = await get_org_aware_secondary_llm_api_handler(default=app.SECONDARY_LLM_API_HANDLER)(
                prompt=prompt, step=step, prompt_name="auto-completion-tweak-value"
            )
            context_reasoning = json_respone.get("reasoning")
            new_current_value = json_respone.get("tweaked_value", "")
            if not new_current_value:
                return ActionFailure(ErrEmptyTweakValue(reasoning=context_reasoning, current_value=current_value))
            LOG.info(
                "Ask LLM tweaked the current value with a new value",
                field_information=input_or_select_context.field,
                current_value=current_value,
                new_value=new_current_value,
            )
            current_value = new_current_value

    else:
        if not input_or_select_context.is_search_bar:
            LOG.info(
                "Auto completion attempts exhausted, trying discover-all-options fallback",
                element_id=skyvern_element.get_id(),
                original_text=text,
            )
            fallback_result = await discover_and_select_from_full_dropdown(
                context=input_or_select_context,
                page=page,
                scraped_page=scraped_page,
                dom=dom,
                original_text=text,
                skyvern_element=skyvern_element,
                step=step,
                task=task,
            )
            if fallback_result is not None:
                return fallback_result

        LOG.info(
            "Auto completion didn't finish, this might leave the input value to be empty.",
            sampling=True,
            context=input_or_select_context,
        )
        return None


@traced(name="skyvern.agent.dropdown.discover_and_select")
async def discover_and_select_from_full_dropdown(
    context: InputOrSelectContext,
    page: Page,
    scraped_page: ScrapedPage,
    dom: DomUtil,
    original_text: str,
    skyvern_element: SkyvernElement,
    step: Step,
    task: Task,
    relevance_threshold: float = 0.6,
) -> ActionResult | None:
    """Fallback for auto-completion: clear input, click/ArrowDown to reveal all options,
    then ask LLM to pick the best semantic match from actual dropdown values."""
    if not await skyvern_element.is_visible():
        return None

    current_frame = skyvern_element.get_frame()
    engine_selection = resolve_engine_selection_for_task(task, app.BROWSER_MANAGER)
    skyvern_frame = await SkyvernFrame.create_instance(current_frame, engine_selection=engine_selection)
    incremental_scraped = IncrementalScrapePage(
        skyvern_frame=skyvern_frame,
        engine_selection=engine_selection,
    )
    await incremental_scraped.start_listen_dom_increment(await skyvern_element.get_element_handler())

    try:
        await skyvern_element.scroll_into_view()
        await skyvern_element.input_clear()

        # Try click first to open the dropdown (most combobox components respond to click)
        try:
            await skyvern_element.get_locator().click(timeout=settings.BROWSER_ACTION_TIMEOUT_MS)
        except Exception:
            LOG.info(
                "Click failed in discover fallback, continuing to ArrowDown",
                element_id=skyvern_element.get_id(),
            )

        await skyvern_frame.safe_wait_for_animation_end(before_wait_sec=1, caller="dropdown_discover.click")

        cleanup_func = clean_and_remove_element_tree_factory(
            task=task,
            step=step,
            check_filter_funcs=[check_existed_but_not_option_element_in_dom_factory(dom)],
            engine_selection=engine_selection,
        )
        incremental_element = await incremental_scraped.get_incremental_element_tree(cleanup_func)

        # If click didn't produce options, try ArrowDown as fallback
        if not incremental_element:
            LOG.info(
                "Discover fallback: no options after click, trying ArrowDown",
                element_id=skyvern_element.get_id(),
            )
            try:
                await skyvern_element.press_key("ArrowDown")
            except Exception as exc:
                if not _is_selected_engine_timeout(exc, engine_selection):
                    raise
                LOG.info(
                    "Timeout pressing ArrowDown in discover fallback, continuing",
                    element_id=skyvern_element.get_id(),
                )

            await skyvern_frame.safe_wait_for_animation_end(before_wait_sec=1, caller="dropdown_discover.arrowdown")
            incremental_element = await incremental_scraped.get_incremental_element_tree(cleanup_func)

        # If incremental detection failed (e.g. options in a different shadow root),
        # try a full page re-scrape diff as last resort
        if not incremental_element:
            LOG.info(
                "Discover fallback: no options from incremental detection, trying re-scrape diff",
                element_id=skyvern_element.get_id(),
            )
            scraped_page_after = await scraped_page.generate_scraped_page_without_screenshots()
            new_element_ids_from_rescrape = list(
                set(scraped_page_after.id_to_css_dict.keys()) - set(scraped_page.id_to_css_dict.keys())
            )
            if new_element_ids_from_rescrape:
                # Feed re-scrape results back into incremental_element so the unified
                # auto-completion-choose-option path below handles them (best-effort,
                # with relevance_threshold). This avoids select_from_emerging_elements
                # which uses the more aggressive custom-select prompt.
                rescrape_elements = [
                    scraped_page_after.id_to_element_dict[eid]
                    for eid in new_element_ids_from_rescrape
                    if eid in scraped_page_after.id_to_element_dict
                ]
                if rescrape_elements:
                    LOG.info(
                        "Discover fallback: re-scrape diff found new elements",
                        new_element_count=len(rescrape_elements),
                    )
                    incremental_element = rescrape_elements
                    incremental_scraped.id_to_element_dict.update(scraped_page_after.id_to_element_dict)

        if not incremental_element:
            LOG.info(
                "Discover fallback: no options found after all attempts",
                element_id=skyvern_element.get_id(),
            )
            return None

        cleaned_elements = remove_duplicated_HTML_element(incremental_element)
        html = incremental_scraped.build_html_tree(cleaned_elements)
        new_element_ids = [e.get("id", "") for e in cleaned_elements if e.get("id")]

        field_information = context.field if not context.intention else context.intention
        slim_output = await get_slim_output_template_value("auto-completion-choose-option")
        prompt = prompt_engine.load_prompt(
            "auto-completion-choose-option",
            is_search=context.is_search_bar,
            field_information=field_information,
            filled_value=original_text,
            navigation_goal=task.navigation_goal,
            navigation_payload_str=json.dumps(task.navigation_payload),
            elements=html,
            new_elements_ids=new_element_ids,
            local_datetime=datetime.now(skyvern_context.ensure_context().tz_info).isoformat(),
            slim_output=slim_output,
        )

        LOG.info(
            "Discover fallback: asking LLM to pick from actual options",
            element_id=skyvern_element.get_id(),
            original_text=original_text,
        )
        json_response = await get_org_aware_secondary_llm_api_handler(default=app.AUTO_COMPLETION_LLM_API_HANDLER)(
            prompt=prompt, step=step, prompt_name="auto-completion-choose-option"
        )

        element_id = json_response.get("id", "")
        relevance_float = json_response.get("relevance_float", 0)

        if not element_id or relevance_float < relevance_threshold:
            LOG.info(
                "Discover fallback: no suitable option found",
                element_id=element_id,
                relevance_float=relevance_float,
                threshold=relevance_threshold,
            )
            return None

        discovered_value = json_response.get("value", "")
        LOG.info(
            "Discover fallback: found suitable option, typing discovered value to trigger auto-completion",
            element_id=element_id,
            relevance_float=relevance_float,
            discovered_value=discovered_value,
        )

        if not discovered_value:
            # FIXME: when element_id is valid and the dropdown is still open (incremental path),
            # we could try clicking the element directly instead of requiring the value text.
            # Currently this only affects the re-scrape path where the dropdown is closed.
            return None

        # Instead of clicking the option directly (dropdown may have closed during re-scrape),
        # input the discovered value into the combobox. Since it's an exact match, the combobox's
        # filter will show it as the only option. Then find and click it directly via Playwright.
        await skyvern_element.input_clear()
        await skyvern_element.press_fill(discovered_value)
        await skyvern_frame.safe_wait_for_animation_end(before_wait_sec=1, caller="dropdown_discover.exact_match")

        # Select the first matching option via keyboard: ArrowDown highlights it, Enter confirms.
        # This avoids needing to locate the option element in shadow DOM.
        try:
            await skyvern_element.press_key("ArrowDown")
            await skyvern_element.press_key("Enter")
            LOG.info(
                "Discover fallback: selected option via keyboard",
                discovered_value=discovered_value,
            )
            return ActionSuccess()
        except Exception:
            LOG.info(
                "Discover fallback: keyboard selection failed",
                exc_info=True,
                discovered_value=discovered_value,
            )
            return None

    except Exception:
        LOG.warning(
            "Discover fallback failed",
            exc_info=True,
            original_text=original_text,
        )
        return None
    finally:
        await incremental_scraped.stop_listen_dom_increment()


@traced(name="skyvern.agent.dropdown.select_sequential")
async def sequentially_select_from_dropdown(
    action: SelectOptionAction,
    input_or_select_context: InputOrSelectContext,
    page: Page,
    dom: DomUtil,
    skyvern_element: SkyvernElement,
    skyvern_frame: SkyvernFrame,
    incremental_scraped: IncrementalScrapePage,
    step: Step,
    task: Task,
    dropdown_menu_element: SkyvernElement | None = None,
    force_select: bool = False,
    target_value: str = "",
    continue_until_close: bool = False,
    entry_action_type: str = "select_option",
) -> CustomSingleSelectResult | None:
    """
    TODO: support to return all values retrieved from the sequentially select
    Only return the last value today
    """
    if not force_select and input_or_select_context.is_search_bar:
        LOG.info(
            "Exit custom selection mode since it's a non-force search bar",
            context=input_or_select_context,
        )
        return None

    # TODO: only support the third-level dropdown selection now, but for date picker, we need to support more levels as it will move the month, year, etc.
    MAX_DATEPICKER_DEPTH = 30
    MAX_SELECT_DEPTH = 3
    max_depth = MAX_DATEPICKER_DEPTH if input_or_select_context.is_date_related else MAX_SELECT_DEPTH
    values: list[str | None] = []
    select_history: list[CustomSingleSelectResult] = []
    single_select_result: CustomSingleSelectResult | None = None
    selection_group_id = str(uuid.uuid4())

    check_filter_funcs: list[CheckFilterOutElementIDFunc] = [check_existed_but_not_option_element_in_dom_factory(dom)]
    for i in range(max_depth):
        try:
            single_select_result = await select_from_dropdown(
                context=input_or_select_context,
                page=page,
                skyvern_element=skyvern_element,
                skyvern_frame=skyvern_frame,
                incremental_scraped=incremental_scraped,
                check_filter_funcs=check_filter_funcs,
                step=step,
                task=task,
                dropdown_menu_element=dropdown_menu_element,
                select_history=select_history,
                force_select=force_select,
                target_value=target_value,
                entry_action_type=entry_action_type,
                selection_group_id=selection_group_id,
            )
        except NoAvailableOptionFoundForCustomSelection as e:
            # The loop only advances past a level whose click succeeded (is_done() gates on
            # ActionSuccess), so any prior history here means an earlier cascade level already
            # mutated the widget — mark it so the caller can't report this miss as a clean skip.
            if any(isinstance(prior.action_result, ActionSuccess) for prior in select_history):
                e.widget_mutated = True
            raise
        assert single_select_result is not None
        select_history.append(single_select_result)
        values.append(single_select_result.value)
        # wait 1s until DOM finished updating
        await skyvern_frame.safe_wait_for_animation_end(before_wait_sec=0.5, caller="sequential_select.pick")

        if await single_select_result.is_done():
            return single_select_result

        if i == max_depth - 1:
            LOG.warning(
                "Reaching the max selection depth",
                depth=i,
            )
            break

        LOG.info(
            "Seems to be a multi-level selection, continue to select until it finishes",
            selected_time=i + 1,
        )
        # wait to load new options
        await skyvern_frame.safe_wait_for_animation_end(before_wait_sec=0.5, caller="sequential_select.next_level")

        check_filter_funcs.append(
            check_disappeared_element_id_in_incremental_factory(incremental_scraped=incremental_scraped)
        )

        secondary_increment_element = await incremental_scraped.get_incremental_element_tree(
            clean_and_remove_element_tree_factory(
                task=task,
                step=step,
                check_filter_funcs=check_filter_funcs,
                engine_selection=skyvern_frame.engine_selection,
            )
        )
        if len(secondary_increment_element) == 0:
            LOG.info(
                "No incremental element detected for the next level selection, going to quit the custom select mode",
                selected_time=i + 1,
            )
            return single_select_result

        # it's for typing. it's been verified in `single_select_result.is_done()`
        assert single_select_result.dropdown_menu is not None

        if single_select_result.action_type is not None and single_select_result.action_type == ActionType.INPUT_TEXT:
            LOG.info(
                "It's an input mini action, going to continue the select action",
            )
            continue

        if continue_until_close:
            LOG.info(
                "Continue the selecting until the dropdown menu is closed",
            )
            continue

        screenshot = await _screenshot_without_cursor(page, timeout=settings.BROWSER_SCREENSHOT_TIMEOUT_MS)
        mini_goal = (
            input_or_select_context.field
            if not input_or_select_context.intention
            else input_or_select_context.intention
        )
        prompt = prompt_engine.load_prompt(
            "confirm-multi-selection-finish",
            mini_goal=mini_goal,
            navigation_goal=task.navigation_goal,
            navigation_payload_str=json.dumps(task.navigation_payload),
            elements="".join(json_to_html(element) for element in secondary_increment_element),
            select_history=json.dumps(build_sequential_select_history(select_history)),
            local_datetime=datetime.now(ensure_context().tz_info).isoformat(),
        )
        # Fall back to the secondary (vision-capable) handler so this screenshot-based
        # verification stays sighted when the main model is vision-less.
        llm_api_handler = await resolve_prompt_type_handler_with_override(
            "confirm-multi-selection-finish",
            task.llm_key,
            task.workflow_run_id if task.workflow_run_id else task.task_id,
            task.organization_id,
            LLMAPIHandlerFactory.get_override_llm_api_handler(
                task.llm_key,
                default=get_org_aware_secondary_llm_api_handler(default=app.SECONDARY_LLM_API_HANDLER),
            ),
        )
        json_response = await llm_api_handler(
            prompt=prompt, screenshots=[screenshot], step=step, prompt_name="confirm-multi-selection-finish"
        )
        if json_response.get("is_mini_goal_finished", False):
            LOG.info("The user has finished the selection for the current opened dropdown")
            return single_select_result
    else:
        if input_or_select_context.is_date_related:
            if skyvern_element.get_tag_name() == InteractiveElement.INPUT and action.option.label:
                try:
                    LOG.info("Try to input the date directly")
                    await skyvern_element.input_sequentially(action.option.label)
                    result = CustomSingleSelectResult(skyvern_frame=skyvern_frame)
                    result.action_result = ActionSuccess()
                    return result

                except Exception:
                    LOG.warning(
                        "Failed to input the date directly",
                        exc_info=True,
                    )

            if single_select_result and single_select_result.action_result:
                single_select_result.action_result.skip_remaining_actions = True
                return single_select_result

    return select_history[-1] if len(select_history) > 0 else None


def build_sequential_select_history(history_list: list[CustomSingleSelectResult]) -> list[dict[str, Any]]:
    result = [
        {
            "reasoning": select_result.reasoning,
            "value": select_result.value,
            "result": "success" if isinstance(select_result.action_result, ActionSuccess) else "failed",
        }
        for select_result in history_list
    ]
    return result


class CustomSelectPromptOptions(BaseModel):
    """
    This is the options for the custom select prompt.
    It's used to generate the prompt for the custom select action.
    is_date_related: whether the field is date related
    required_field: whether the field is required
    field_information: the description about the field, could be field name, action intention, action reasoning about the field, etc.
    target_value: the target value of the field (generated by the LLM in the main prompt).
    """

    is_date_related: bool = False
    required_field: bool = False
    field_information: str = ""
    target_value: str | None = None


def _collect_option_texts(elements: list[dict]) -> list[str]:
    """BFS over an element tree, returning option-like text in document order with duplicates removed.

    Native ``<select>`` options live on the element's ``options`` field
    (``[{text, value, optionIndex}, ...]``); the scraper skips their child
    ``<option>`` nodes, so this walker must inspect that field directly.
    Radio/checkbox-based custom selects (e.g. ``role="radiogroup"``) have no
    ``<option>``/``<li>`` nodes either; they're recognized the same way
    ``_custom_select_candidates_from_elements`` recognizes them so a
    radio-group miss doesn't misreport zero observed options.
    """
    queue: deque[dict] = deque(elements)
    seen: set[str] = set()
    out: list[str] = []
    # Mirrors _custom_select_candidates_from_elements' covered_choice_input_ids: a <label>
    # wrapping a radio/checkbox is recorded once via its own label text, so the descendant
    # input(s) it already covers must be skipped when the BFS reaches them directly — otherwise
    # a labelled radio's raw `value` attribute is recorded again as an unrelated second entry.
    covered_choice_input_ids: set[str] = set()

    def _record(text: str) -> None:
        if text and text not in seen:
            seen.add(text)
            out.append(text)

    while queue:
        node = queue.popleft()
        if not isinstance(node, dict):
            continue
        attrs = node.get("attributes") or {}
        role = str(attrs.get("role") or "").lower()
        tag = str(node.get("tagName") or "").lower()
        input_type = str(attrs.get("type") or "").lower()
        element_id = str(node.get("id") or "") or None
        is_choice_input = tag == "input" and input_type in ("checkbox", "radio")
        # Only compute the descendant walk for <label> nodes (its one consumer below) — calling
        # it unconditionally for every queued node makes this walker quadratic on large DOMs.
        if role == "option" or tag in ("li", "option"):
            _record(str(node.get("text") or "").strip())
        elif is_choice_input and element_id in covered_choice_input_ids:
            pass
        elif is_choice_input or role in _CUSTOM_SELECT_CHOICE_INPUT_ROLES:
            _record(_select_shadow_label_from_node(node) or _custom_select_choice_value(node) or "")
        elif tag == "label":
            choice_input_ids, contains_choice_input = _custom_select_descendant_choice_inputs(node)
            if contains_choice_input:
                _record(_select_shadow_label_from_node(node) or _custom_select_choice_value(node) or "")
                covered_choice_input_ids.update(choice_input_ids)
        for option in node.get("options") or []:
            if not isinstance(option, dict):
                continue
            # Strip text before falling back to value so whitespace-only text
            # (e.g. "   ") is treated as missing rather than recorded as empty.
            option_text = str(option.get("text") or "").strip()
            if not option_text:
                option_text = str(option.get("value") or "").strip()
            _record(option_text)
        for child in node.get("children") or []:
            queue.append(child)
    return out


def _custom_select_descendant_choice_inputs(node: dict) -> tuple[set[str], bool]:
    input_ids: set[str] = set()
    contains_choice_input = False
    queue: deque[dict] = deque(node.get("children") or [])
    while queue:
        child = queue.popleft()
        if not isinstance(child, dict):
            continue
        tag = str(child.get("tagName") or "").lower()
        attrs = child.get("attributes") or {}
        input_type = str(attrs.get("type") or "").lower()
        element_id = str(child.get("id") or "")
        if tag == "input" and input_type in ("checkbox", "radio"):
            contains_choice_input = True
            if element_id:
                input_ids.add(element_id)
        for grandchild in child.get("children") or []:
            queue.append(grandchild)
    return input_ids, contains_choice_input


def _custom_select_choice_value(node: dict) -> str | None:
    attrs = node.get("attributes") or {}
    value = " ".join(str(attrs.get("value") or "").split())
    if value:
        return value
    queue: deque[dict] = deque(node.get("children") or [])
    while queue:
        child = queue.popleft()
        if not isinstance(child, dict):
            continue
        child_attrs = child.get("attributes") or {}
        child_value = " ".join(str(child_attrs.get("value") or "").split())
        if child_value:
            return child_value
        for grandchild in child.get("children") or []:
            queue.append(grandchild)
    return None


_CUSTOM_SELECT_CONTAINER_ROLES = frozenset({"combobox", "listbox", "menu", "radiogroup", "tree"})
_CUSTOM_SELECT_CHOICE_ROLES = frozenset({"menuitem", "menuitemcheckbox", "menuitemradio", "option", "treeitem"})
_CUSTOM_SELECT_CHOICE_INPUT_ROLES = frozenset({"checkbox", "radio", "menuitemcheckbox", "menuitemradio"})


def _is_custom_select_choice_surface(role: str) -> bool:
    return role in {"listbox", "menu", "radiogroup", "tree"}


class _CustomSelectCandidate(TypedDict):
    label: str | None
    element_id: str | None
    value: str | None
    is_choice_input: bool


def _custom_select_candidates_from_elements(elements: list[dict]) -> list[_CustomSelectCandidate]:
    queue: deque[tuple[dict, bool, bool, bool]] = deque((element, False, False, False) for element in elements)
    candidates: list[_CustomSelectCandidate] = []
    seen: set[tuple[str | None, str | None, str | None]] = set()
    covered_choice_input_ids: set[str] = set()

    while queue:
        node, in_choice_surface, in_multiselectable, in_disabled_subtree = queue.popleft()
        if not isinstance(node, dict):
            continue

        attrs = node.get("attributes") or {}
        role = str(attrs.get("role") or "").lower()
        tag = str(node.get("tagName") or "").lower()
        input_type = str(attrs.get("type") or "").lower()
        element_id = str(node.get("id") or "") or None
        value = _custom_select_choice_value(node)
        label = _select_shadow_label_from_node(node) or value
        choice_input_ids, contains_choice_input = _custom_select_descendant_choice_inputs(node)
        is_choice_input = tag == "input" and input_type in ("checkbox", "radio")
        is_option_node = role in _CUSTOM_SELECT_CHOICE_ROLES or tag == "option" or (tag == "li" and in_choice_surface)
        is_label_choice = tag == "label" and contains_choice_input
        # Only the full tree walk can see toggle semantics inherited from wrappers or an ancestor
        # multiselect container; the resolved candidate element alone does not carry that context.
        is_choice_input_shape = (
            is_choice_input
            or contains_choice_input
            or role in _CUSTOM_SELECT_CHOICE_INPUT_ROLES
            or (in_multiselectable and role in {"option", "treeitem"})
        )
        has_choice_state = "aria-selected" in attrs or "aria-checked" in attrs
        is_clickable_choice = (
            bool(node.get("interactable"))
            and label
            and (
                role not in _CUSTOM_SELECT_CONTAINER_ROLES
                and tag not in {"input", "select", "textarea"}
                and not (tag == "a" and bool(attrs.get("href")))
                and (in_choice_surface or has_choice_state)
            )
        )

        # A disabled ancestor (aria-disabled=true, disabled fieldset/wrapper, disabled <label>) disables the
        # whole subtree; a descendant aria-disabled=false cannot re-enable it, so inheritance is monotonic.
        node_in_disabled_subtree = in_disabled_subtree or _custom_select_node_is_disabled(attrs)

        if is_choice_input and element_id in covered_choice_input_ids:
            pass
        elif (
            element_id
            and label
            and not node_in_disabled_subtree
            and (is_option_node or is_choice_input or is_label_choice or is_clickable_choice)
        ):
            candidate = _select_shadow_candidate(label, element_id=element_id, value=value)
            if candidate is not None:
                key = (candidate.get("element_id"), candidate.get("label"), candidate.get("value"))
                if key not in seen:
                    seen.add(key)
                    candidates.append(
                        _CustomSelectCandidate(
                            label=candidate.get("label"),
                            element_id=candidate.get("element_id"),
                            value=candidate.get("value"),
                            is_choice_input=is_choice_input_shape,
                        )
                    )
                    if is_label_choice:
                        covered_choice_input_ids.update(choice_input_ids)

        child_in_choice_surface = in_choice_surface or _is_custom_select_choice_surface(role)
        aria_multiselectable = attrs.get("aria-multiselectable")
        child_in_multiselectable = in_multiselectable or (
            isinstance(aria_multiselectable, str) and aria_multiselectable.lower() == "true"
        )
        for child in node.get("children") or []:
            queue.append((child, child_in_choice_surface, child_in_multiselectable, node_in_disabled_subtree))

    return candidates


def _split_selected_label_values(value: str) -> set[str]:
    normalized = _normalize_select_shadow_text(value)
    if not normalized:
        return set()
    parts = {part.strip() for part in normalized.split(",") if part.strip()} | {normalized}
    # Widgets that reflect a committed pick by relabelling a trigger/wrapper (e.g. aria-label
    # flips from "Search sources" to "Selected WAKE") should still match the bare option label.
    for prefix in _SELECTED_LABEL_PREFIXES:
        if normalized.startswith(prefix):
            parts.add(normalized[len(prefix) :].strip())
    return {part for part in parts if part}


_SELECTED_LABEL_PREFIXES = ("selected ", "selected:")

_CUSTOM_SELECT_MATCHED_STATE_JS = r"""
(el) => {
    const normalize = (value) => String(value ?? "").replace(/\s+/g, " ").trim().toLowerCase();
    const label = [
        el.textContent,
        el.getAttribute("aria-label"),
        el.getAttribute("title"),
        el.getAttribute("value"),
        el.value
    ].map(normalize).find(Boolean) || "";
    const role = normalize(el.getAttribute("role"));
    const nestedChoice = el.querySelector?.("input[type='checkbox'], input[type='radio']");
    const multiselectable = el.closest?.("[aria-multiselectable]");
    const inMultiselectable = normalize(multiselectable?.getAttribute("aria-multiselectable")) === "true";
    const ariaSelected = el.getAttribute("aria-selected") === "true";
    const ariaChecked = el.getAttribute("aria-checked") === "true";
    const selectedAttr = el.hasAttribute("selected") || el.selected === true;
    const checked = Boolean(
        (el.matches?.("input[type='checkbox'], input[type='radio']") && el.checked)
        || nestedChoice?.checked
    );
    return {
        label,
        role,
        nestedChoice: nestedChoice != null,
        inMultiselectable,
        ariaSelected,
        ariaChecked,
        selectedAttr,
        checked
    };
}
"""

_CUSTOM_SELECT_COMMITTED_STATE_JS = r"""
([anchor, args]) => {
    const expectedLabel = args.expectedLabel;
    const anchorIsComboboxInput = args.anchorIsComboboxInput;
    const allowAriaSelectedOptionTokens = args.allowAriaSelectedOptionTokens !== false;
    const allowSingleValueScope = args.allowSingleValueScope === true;
    const normalize = (value) => String(value ?? "").replace(/\s+/g, " ").trim().toLowerCase();
    const splitValues = (value) => {
        const normalized = normalize(value);
        if (!normalized) return [];
        const parts = [normalized, ...normalized.split(",").map((part) => part.trim()).filter(Boolean)];
        for (const prefix of ["selected ", "selected:"]) {
            if (normalized.startsWith(prefix)) parts.push(normalized.slice(prefix.length).trim());
        }
        return parts.filter(Boolean);
    };
    const matchesExpected = (value) => splitValues(value).includes(expectedLabel);
    const triggerSelector = [
        "[role='combobox']",
        "[aria-haspopup='listbox']",
        "[aria-haspopup='menu']",
        "[aria-haspopup='true']",
        "button[aria-expanded]",
        "input[role='combobox']",
        "select"
    ].join(",");
    const scopeSelectors = [
        "[data-uxi-widget-type]",
        "[data-automation-id*='formField']",
        "[role='group']",
        "fieldset",
        ".field"
    ];
    const singleValueSelector = [
        "[class*='single-value']",
        "[class*='singleValue']",
        "[class*='multi-value__label']"
    ].join(",");
    // Nearest matching ancestor wins; never scope to the whole form or a bare
    // parent container — sibling fields showing the target label must not
    // pre-confirm this one. With no recognized field wrapper, fall back to the
    // anchor itself (a miss routes to the LLM path, never a cross-field match).
    const scopeCandidates = scopeSelectors
        .map((selector) => anchor.closest?.(selector))
        .filter(Boolean);
    // Some combobox libraries render the committed value beside the input, invisible to
    // input-value read-back.
    if (allowSingleValueScope && anchorIsComboboxInput) {
        let ancestor = anchor.parentElement;
        for (let hops = 1; ancestor && hops <= 4; hops += 1, ancestor = ancestor.parentElement) {
            const triggers = ancestor.querySelectorAll?.(triggerSelector);
            if (
                ancestor.querySelector?.(singleValueSelector)
                && triggers?.length === 1
                && (triggers[0] === anchor || anchor.contains?.(triggers[0]))
            ) {
                scopeCandidates.push(ancestor);
                break;
            }
        }
    }
    const scopeRoot = (
        scopeCandidates.reduce((closest, el) => (!closest || closest.contains(el) ? el : closest), null)
        || anchor
    );
    const expandedState = anchor.getAttribute?.("aria-expanded")
        || anchor.closest?.("[aria-expanded]")?.getAttribute("aria-expanded");
    // While the widget reports open, every reflected surface (tokens, single-value nodes,
    // hidden inputs, trigger text) can mirror the typed filter rather than a commitment;
    // the strict posture trusts read-back only once closed.
    const strictReflectionClosed = !(allowSingleValueScope && anchorIsComboboxInput) || expandedState === "false";
    const tokenSelectors = [
        ...(allowAriaSelectedOptionTokens ? ["[role='option'][aria-selected='true']"] : []),
        "[data-automation-id='selectedItem']",
        ".pill",
        ".chip",
        "[class*='token']"
    ].join(",");
    if (strictReflectionClosed) {
        for (const token of scopeRoot.querySelectorAll(tokenSelectors)) {
            if (matchesExpected(token.textContent) || matchesExpected(token.getAttribute("aria-label"))) {
                return {matched: true, branch: "scope_token"};
            }
        }
    }
    if (strictReflectionClosed && allowSingleValueScope && anchorIsComboboxInput) {
        for (const singleValue of scopeRoot.querySelectorAll(singleValueSelector)) {
            if (
                matchesExpected(singleValue.textContent)
                || matchesExpected(singleValue.getAttribute("aria-label"))
            ) {
                return {matched: true, branch: "scope_single_value"};
            }
        }
    }
    if (strictReflectionClosed) {
        for (const hidden of scopeRoot.querySelectorAll("input[type='hidden']")) {
            if (matchesExpected(hidden.value)) return {matched: true, branch: "scope_hidden_input"};
        }
    }
    const activeId = anchor.getAttribute?.("aria-activedescendant");
    if (strictReflectionClosed && allowAriaSelectedOptionTokens && activeId) {
        const active = scopeRoot.querySelector(`#${CSS.escape(activeId)}`);
        if (active && active.getAttribute("aria-selected") === "true") {
            if (matchesExpected(active.textContent) || matchesExpected(active.getAttribute("aria-label"))) {
                return {matched: true, branch: "scope_token"};
            }
        }
    }
    const reflectedValues = (el) => [
        el.textContent,
        el.getAttribute("aria-label"),
        el.getAttribute("aria-valuetext"),
        el.getAttribute("title"),
    ];
    const seen = new Set();
    const triggerCandidates = [
        anchor,
        anchor.closest?.(triggerSelector),
        ...(scopeRoot.matches?.(triggerSelector) ? [scopeRoot] : []),
        ...scopeRoot.querySelectorAll(triggerSelector)
    ];
    if (strictReflectionClosed) {
        for (const el of triggerCandidates) {
            if (!el || seen.has(el) || !scopeRoot.contains(el)) continue;
            seen.add(el);
            if (reflectedValues(el).some(matchesExpected)) {
                return {matched: true, branch: "scope_trigger_text"};
            }
        }
    }
    // A combobox <input> may still hold the user-typed filter text; raw value equality alone is not
    // a committed signal. Only trust it when the dropdown has closed (aria-expanded=false).
    if (anchorIsComboboxInput) {
        const valueMatches = matchesExpected(anchor.value) || matchesExpected(anchor.getAttribute("value"));
        if (valueMatches && expandedState === "false") {
            return {matched: true, branch: "scope_input_value"};
        }
        return {matched: false, branch: "none"};
    }
    for (const el of seen) {
        if (reflectedValues(el).some((value) => normalize(value))) return {matched: false, branch: "none"};
    }
    return {matched: false, branch: "none"};
}
"""


async def _evaluate_element_scoped(
    element: SkyvernElement,
    expression: str,
    arg: Any | None = None,
) -> Any:
    handler = await element.get_element_handler()
    payload = handler if arg is None else [handler, arg]
    return await SkyvernFrame.evaluate(frame=element.get_frame(), expression=expression, arg=payload)


async def _read_custom_select_matched_state(element: SkyvernElement) -> dict | None:
    try:
        if await element.get_locator().count() != 1:
            return None
        state = await _evaluate_element_scoped(element, _CUSTOM_SELECT_MATCHED_STATE_JS)
    except Exception:
        LOG.info(
            "Failed to read custom-select matched element state",
            exc_info=True,
        )
        return None
    return state if isinstance(state, dict) else None


def _custom_select_matched_state_confirms(state: dict | None, expected_label: str) -> bool:
    if not isinstance(state, dict):
        return False
    label_matches = expected_label in _split_selected_label_values(str(state.get("label") or ""))
    return label_matches and any(
        bool(state.get(field)) for field in ("ariaSelected", "ariaChecked", "selectedAttr", "checked")
    )


def _custom_select_matched_state_confirms_pre_click(state: dict | None, expected_label: str) -> bool:
    if not isinstance(state, dict):
        return False
    label_matches = expected_label in _split_selected_label_values(str(state.get("label") or ""))
    if not label_matches:
        return False
    if any(bool(state.get(field)) for field in ("ariaChecked", "selectedAttr", "checked")):
        return True
    # In an aria-multiselectable container aria-selected IS the committed state (clicking would
    # toggle it off); only single-select options treat bare aria-selected as a keyboard highlight.
    if str(state.get("role") or "").lower() == "option" and not bool(state.get("inMultiselectable")):
        return False
    return bool(state.get("ariaSelected"))


async def _custom_select_committed_readback_confirms(
    selected_element: SkyvernElement, requested_value: str | None
) -> bool:
    # A strict scope read can miss a commit the chosen option itself reflects, so re-read the option's
    # own matched state before ownership recovery. Exact normalized label plus a committed signal only:
    # a bare single-select highlight, a mismatch, or an unreadable state is never success (SKY-14909).
    expected_label = _normalize_select_shadow_text(requested_value)
    if not expected_label:
        return False
    return _custom_select_matched_state_confirms_pre_click(
        await _read_custom_select_matched_state(selected_element), expected_label
    )


async def _custom_select_scope_confirms_committed(
    *,
    readback_scope_element: SkyvernElement | None,
    anchor_is_combobox_input: bool,
    matched_element_id: str,
    matched_label: str | None,
    expected_label: str,
    allow_aria_selected_option_tokens: bool,
    allow_single_value_scope: bool,
) -> tuple[bool, str]:
    if readback_scope_element is None:
        return False, "none"

    try:
        committed = await _evaluate_element_scoped(
            readback_scope_element,
            _CUSTOM_SELECT_COMMITTED_STATE_JS,
            {
                "expectedLabel": expected_label,
                "anchorIsComboboxInput": anchor_is_combobox_input,
                "allowAriaSelectedOptionTokens": allow_aria_selected_option_tokens,
                "allowSingleValueScope": allow_single_value_scope,
            },
        )
    except Exception:
        LOG.info(
            "Failed to read custom-select committed label",
            matched_element_id=matched_element_id,
            matched_label=matched_label,
            exc_info=True,
        )
        return False, "none"

    if not isinstance(committed, dict):
        return False, "none"
    return bool(committed.get("matched")), str(committed.get("branch") or "none")


async def _verify_custom_select_option(
    *,
    matched_element: SkyvernElement,
    readback_scope_element: SkyvernElement | None,
    anchor_is_combobox_input: bool,
    matched_element_id: str,
    matched_label: str | None,
    use_strict_verification: bool,
) -> tuple[bool, str]:
    expected_label = _normalize_select_shadow_text(matched_label)
    if not expected_label:
        return False, "none"

    matched_state_confirms = (
        _custom_select_matched_state_confirms_pre_click
        if use_strict_verification
        else _custom_select_matched_state_confirms
    )
    if matched_state_confirms(await _read_custom_select_matched_state(matched_element), expected_label):
        return True, "matched_state"

    return await _custom_select_scope_confirms_committed(
        readback_scope_element=readback_scope_element,
        anchor_is_combobox_input=anchor_is_combobox_input,
        matched_element_id=matched_element_id,
        matched_label=matched_label,
        expected_label=expected_label,
        allow_aria_selected_option_tokens=not use_strict_verification,
        allow_single_value_scope=use_strict_verification,
    )


# input_text_converted is excluded: its anchor is frequently not an <input>, so the reset path that
# contains an unverified click does not exist for it.
_EXECUTABLE_CUSTOM_SELECT_ENTRIES = ("select_option", "input_text")
# The timer is only a liveness cap for frame-starved/background pages and vendor CDP engines. Double-rAF
# remains the normal render-driven path; the timer does not claim that rendering has become stable.
_CUSTOM_SELECT_RENDER_SETTLE_JS = (
    "() => Promise.race(["
    "new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve('raf')))),"
    "new Promise((resolve) => setTimeout(() => resolve('liveness_fallback'), 250))"
    "])"
)


class _CustomSelectRenderSettle(NamedTuple):
    source: str
    elapsed_ms: int


async def _wait_custom_select_render_settle(element: SkyvernElement) -> _CustomSelectRenderSettle:
    started_at = time.monotonic()
    source = await _evaluate_element_scoped(element, _CUSTOM_SELECT_RENDER_SETTLE_JS)
    return _CustomSelectRenderSettle(
        source=source if source in {"raf", "liveness_fallback"} else "unknown",
        elapsed_ms=int((time.monotonic() - started_at) * 1000),
    )


def _custom_select_settle_summary(settles: list[_CustomSelectRenderSettle]) -> tuple[str, int]:
    source = "liveness_fallback" if any(settle.source == "liveness_fallback" for settle in settles) else "raf"
    if not settles:
        source = "not_run"
    elif any(settle.source == "unknown" for settle in settles):
        source = "unknown"
    return source, sum(settle.elapsed_ms for settle in settles)


def _log_custom_select_verification_outcome(
    event: str,
    *,
    phase: str,
    settles: list[_CustomSelectRenderSettle],
    committed: bool,
    verification_branch: str,
    verification_reason: str,
    recovery_attempted: bool,
    recovery_succeeded: bool,
) -> None:
    render_settle_source, render_settle_elapsed_ms = _custom_select_settle_summary(settles)
    LOG.info(
        event,
        phase=phase,
        render_settle_source=render_settle_source,
        render_settle_count=len(settles),
        render_settle_elapsed_ms=render_settle_elapsed_ms,
        committed=committed,
        verification_branch=verification_branch,
        verification_reason=verification_reason,
        recovery_attempted=recovery_attempted,
        recovery_succeeded=recovery_succeeded,
    )


async def _verify_custom_select_option_with_settle(
    *,
    matched_element: SkyvernElement,
    readback_scope_element: SkyvernElement | None,
    anchor_is_combobox_input: bool,
    matched_element_id: str,
    matched_label: str | None,
    use_strict_verification: bool,
    settle_outcomes: list[_CustomSelectRenderSettle] | None = None,
) -> tuple[bool, str]:
    """Read back after bounded render turns so framework reconciliation is causally observable."""
    settle_element = readback_scope_element or matched_element
    for _ in range(2):
        settle = await _wait_custom_select_render_settle(settle_element)
        if settle_outcomes is not None and isinstance(settle, _CustomSelectRenderSettle):
            settle_outcomes.append(settle)
        verified, branch = await _verify_custom_select_option(
            matched_element=matched_element,
            readback_scope_element=readback_scope_element,
            anchor_is_combobox_input=anchor_is_combobox_input,
            matched_element_id=matched_element_id,
            matched_label=matched_label,
            use_strict_verification=use_strict_verification,
        )
        if verified:
            return True, branch
    return False, "none"


async def _resolve_custom_select_readback_scope_element(
    *,
    get_readback_scope_element: Callable[[], Awaitable[SkyvernElement | None]] | None,
    target_value: str,
    matched_element_id: str,
    matched_label: str | None,
) -> SkyvernElement | None:
    if get_readback_scope_element is None:
        return None

    try:
        return await get_readback_scope_element()
    except Exception:
        LOG.info(
            "Failed to resolve custom-select read-back scope element; continuing with matched-element read-back",
            target_value=target_value,
            matched_element_id=matched_element_id,
            matched_label=matched_label,
            exc_info=True,
        )
        return None


def _readback_scope_element_provider(
    element: SkyvernElement,
) -> Callable[[], Awaitable[SkyvernElement | None]]:
    async def _provide() -> SkyvernElement | None:
        return element

    return _provide


async def _anchor_is_combobox_input(element: SkyvernElement | None) -> bool:
    if element is None:
        return False
    try:
        return str(element.get_tag_name() or "").lower() == "input"
    except Exception:
        return False


def _terminal_custom_select_failure(
    *, target_value: str, matched_label: str | None
) -> tuple[ActionFailure, str | None]:
    action_failure = _no_element_matched_failure(
        target_value,
        "Deterministic custom-select click could not be verified by matched element read-back",
    )
    action_failure.skip_remaining_actions = True
    action_failure.data = {"_terminal_custom_select_failure": True}
    return action_failure, matched_label


def _is_terminal_custom_select_failure(action_result: ActionResult | None) -> bool:
    return (
        isinstance(action_result, ActionFailure)
        and bool(action_result.skip_remaining_actions)
        and isinstance(action_result.data, dict)
        and action_result.data.get("_terminal_custom_select_failure") is True
    )


async def _select_deterministic_custom_option(
    *,
    target_value: str | None,
    get_option_candidates: Callable[[], list[_CustomSelectCandidate]],
    field_context: Any,
    page: Page,
    get_skyvern_element: Callable[[str], Awaitable[SkyvernElement]],
    get_readback_scope_element: Callable[[], Awaitable[SkyvernElement | None]] | None = None,
    task: Task,
    execute: bool,
    step: Step | None = None,
    entry_action_type: str = "select_option",
    selection_group_id: str | None = None,
    select_depth: int = 0,
    on_click_attempted: Callable[[], None] | None = None,
    on_reset_fallback: Callable[[Callable[[CustomSelectFamilyOutcome], None]], None] | None = None,
    settle_outcomes: list[_CustomSelectRenderSettle] | None = None,
    post_failed_click_commit_recovery: bool = False,
    engine_selection: BrowserEngineSelection | None = UNSET_SELECTION,
) -> tuple[ActionResult, str | None] | None:
    if engine_selection is UNSET_SELECTION:
        engine_selection = resolve_engine_selection_for_task(task, app.BROWSER_MANAGER)

    started_at = time.monotonic()
    selection_group_id = selection_group_id or str(uuid.uuid4())
    option_count: int | None = None
    eligible = False
    match_tier: str | None = None
    attempted = False
    # emit can run before these values are otherwise assigned and swallows exceptions, so initialize
    # both before the closure is defined.
    anchor_is_combobox_input = False
    verify_branch: str | None = None
    click_attempted = False
    script_mode_run = False

    def emit(outcome: CustomSelectFamilyOutcome) -> None:
        try:
            LOG.info(
                "custom_select_family_outcome",
                family="custom_select",
                workflow_run_id=task.workflow_run_id,
                task_id=task.task_id,
                organization_id=task.organization_id,
                step_id=getattr(step, "step_id", None),
                entry_action_type=entry_action_type,
                selection_group_id=selection_group_id,
                select_depth=select_depth,
                script_mode=script_mode_run,
                family_gate_enabled=gate.family_enabled,
                assigned=gate.assigned,
                gate_error=gate.gate_error,
                encountered=True,
                eligible=eligible,
                match_tier=match_tier,
                option_count=option_count,
                attempted=attempted,
                click_attempted=click_attempted,
                anchor_is_combobox_input=anchor_is_combobox_input,
                verify_branch=verify_branch,
                verified_success=outcome
                in {CustomSelectFamilyOutcome.success_precommit, CustomSelectFamilyOutcome.success_verified},
                outcome=outcome.value,
                llm_fallback_requested=(
                    outcome.value.startswith("llm_fallback_")
                    or outcome == CustomSelectFamilyOutcome.terminal_llm_fallback_exception
                ),
                duration_ms=int((time.monotonic() - started_at) * 1000),
            )
        except Exception:
            LOG.debug("custom_select_family_outcome failed", exc_info=True)

    if not target_value:
        return None
    if isinstance(field_context, dict) and field_context.get("is_date_related") is True:
        return None

    context = skyvern_context.current()
    script_mode_run = bool(context and context.script_mode)
    gate = await _resolve_collapse_gate(
        task,
        COLLAPSE_CUSTOM_SELECT_FANOUT_FLAG,
        "collapse-custom-select-fanout",
    )
    if gate.gate_error and not post_failed_click_commit_recovery:
        emit(CustomSelectFamilyOutcome.llm_fallback_gate_error)
        return None

    try:
        option_candidates = get_option_candidates()
        if not option_candidates:
            return None
        option_labels = [str(candidate.get("label") or "") for candidate in option_candidates]
        option_values = [candidate.get("value") for candidate in option_candidates]
        resolution = await app.AGENT_FUNCTION.resolve_field_option(
            target_value=target_value,
            option_labels=option_labels,
            option_values=option_values,
            field_context=field_context,
            url=task.url,
            organization_id=task.organization_id,
        )
    except Exception:
        emit(CustomSelectFamilyOutcome.llm_fallback_eval_error)
        return None

    option_count = len(option_candidates)
    eligible = not resolution.fallback_to_llm and resolution.matched_index is not None
    match_tier = resolution.matched_tier
    if not gate.family_enabled and not post_failed_click_commit_recovery:
        emit(CustomSelectFamilyOutcome.llm_fallback_family_off)
        return None
    if gate.assigned is False and not post_failed_click_commit_recovery:
        emit(CustomSelectFamilyOutcome.llm_fallback_control)
        return None
    if resolution.fallback_to_llm or resolution.matched_index is None:
        emit(CustomSelectFamilyOutcome.llm_fallback_no_match)
        return None
    if resolution.matched_index >= len(option_candidates):
        emit(CustomSelectFamilyOutcome.llm_fallback_match_unactionable)
        return None
    if resolution.matched_tier != "exact":
        emit(CustomSelectFamilyOutcome.llm_fallback_tier_excluded)
        return None

    matched_candidate = option_candidates[resolution.matched_index]
    element_id = matched_candidate.get("element_id")
    matched_label = resolution.matched_label
    # Computed by the tree walk, which sees wrapper and multiselect-container toggle semantics
    # that are not necessarily present on the resolved element itself.
    matched_option_is_choice_input = matched_candidate["is_choice_input"]
    if not element_id:
        emit(CustomSelectFamilyOutcome.llm_fallback_match_unactionable)
        return None

    readback_scope_element: SkyvernElement | None = None
    try:
        selected_element = await get_skyvern_element(element_id)
        if await selected_element.get_attr("role") == "listbox":
            emit(CustomSelectFamilyOutcome.llm_fallback_match_unactionable)
            return None

        attempted = True
        matched_state = await _read_custom_select_matched_state(selected_element)
        live_role = str((matched_state or {}).get("role") or "").lower()
        live_toggle_shaped = (
            (bool((matched_state or {}).get("inMultiselectable")) and live_role in {"option", "treeitem"})
            or live_role in _CUSTOM_SELECT_CHOICE_INPUT_ROLES
            or bool((matched_state or {}).get("nestedChoice"))
        )
        matched_option_is_choice_input = matched_option_is_choice_input or live_toggle_shaped

        readback_scope_element = await _resolve_custom_select_readback_scope_element(
            get_readback_scope_element=get_readback_scope_element,
            target_value=target_value,
            matched_element_id=element_id,
            matched_label=matched_label,
        )
        anchor_is_combobox_input = await _anchor_is_combobox_input(readback_scope_element)
        allow_single_value_scope = (
            entry_action_type in _EXECUTABLE_CUSTOM_SELECT_ENTRIES and entry_action_type != "select_option"
        )

        if entry_action_type == "input_text" and not anchor_is_combobox_input:
            execute = False

        expected_label = _normalize_select_shadow_text(matched_label)
        if expected_label:
            if _custom_select_matched_state_confirms_pre_click(matched_state, expected_label):
                verify_branch = "matched_state"
                emit(CustomSelectFamilyOutcome.success_precommit)
                return ActionSuccess(), matched_label
            committed, verify_branch = await _custom_select_scope_confirms_committed(
                readback_scope_element=readback_scope_element,
                anchor_is_combobox_input=anchor_is_combobox_input,
                matched_element_id=element_id,
                matched_label=matched_label,
                expected_label=expected_label,
                allow_aria_selected_option_tokens=False,
                allow_single_value_scope=allow_single_value_scope,
            )
            if committed:
                emit(CustomSelectFamilyOutcome.success_precommit)
                return ActionSuccess(), matched_label

        if not execute:
            emit(CustomSelectFamilyOutcome.llm_fallback_execution_disabled)
            return None

        await selected_element.scroll_into_view()
        # Captured before the click: the click itself can mutate the anchor, and the reset must
        # restore the pre-click text, not the mutation.
        pre_click_anchor_value: str | None = None
        if allow_single_value_scope and anchor_is_combobox_input and readback_scope_element is not None:
            pre_click_anchor_value = await get_input_value(
                readback_scope_element.get_tag_name(),
                readback_scope_element.get_locator(),
                engine_selection=engine_selection,
            )
            if pre_click_anchor_value is None:
                LOG.info(
                    "Anchor value unreadable before deterministic click; falling back to LLM path",
                    target_value=target_value,
                    matched_element_id=element_id,
                )
                emit(CustomSelectFamilyOutcome.llm_fallback_pre_click_error)
                return None
        click_attempted = True
        if on_click_attempted is not None:
            on_click_attempted()
        await selected_element.click(page=page, engine_selection=engine_selection)
        verified, verify_branch = await _verify_custom_select_option_with_settle(
            matched_element=selected_element,
            readback_scope_element=readback_scope_element,
            anchor_is_combobox_input=anchor_is_combobox_input,
            matched_element_id=element_id,
            matched_label=matched_label,
            use_strict_verification=anchor_is_combobox_input and allow_single_value_scope,
            settle_outcomes=settle_outcomes,
        )
        if verified:
            emit(CustomSelectFamilyOutcome.success_verified)
            return ActionSuccess(), matched_label
    except Exception as exc:
        if not click_attempted or isinstance(exc, InteractWithDisabledElement):
            LOG.info(
                "Deterministic custom-select failed; falling back to LLM path",
                target_value=target_value,
                matched_element_id=element_id,
                matched_label=matched_label,
                exc_info=True,
            )
            emit(CustomSelectFamilyOutcome.llm_fallback_pre_click_error)
            return None
        LOG.info(
            "Deterministic custom-select failed after click; returning failure to avoid replaying over mutated widget",
            target_value=target_value,
            matched_element_id=element_id,
            matched_label=matched_label,
            exc_info=True,
        )
        emit(CustomSelectFamilyOutcome.terminal_post_click_exception)
        return _terminal_custom_select_failure(target_value=target_value, matched_label=matched_label)

    if anchor_is_combobox_input:
        # Text-input comboboxes can be safely reset, so an unconfirmed read-back routes to the LLM
        # mini-agent (which clears/reopens the field) instead of hard-failing the whole action.
        reset_verified = await _reset_custom_select_combobox_input(
            readback_scope_element,
            page,
            engine_selection=engine_selection,
            restore_value=pre_click_anchor_value if entry_action_type != "select_option" else None,
        )
        if reset_verified:
            if on_reset_fallback is not None:
                on_reset_fallback(emit)
            else:
                emit(CustomSelectFamilyOutcome.llm_fallback_reset_verified)
            LOG.info(
                "Deterministic custom-select read-back inconclusive on combobox input; routing to LLM fallback",
                target_value=target_value,
                matched_element_id=element_id,
                matched_label=matched_label,
            )
            return None
        LOG.info(
            "Deterministic custom-select combobox reset failed; returning failure to avoid replaying over mutated widget",
            target_value=target_value,
            matched_element_id=element_id,
            matched_label=matched_label,
        )
        emit(CustomSelectFamilyOutcome.terminal_unverified_reset)
        return _terminal_custom_select_failure(target_value=target_value, matched_label=matched_label)

    if not matched_option_is_choice_input:
        LOG.info(
            "Deterministic custom-select read-back inconclusive on non-choice-input option; returning terminal failure",
            target_value=target_value,
            matched_element_id=element_id,
            matched_label=matched_label,
        )
        emit(CustomSelectFamilyOutcome.terminal_unverified_click)
        return _terminal_custom_select_failure(target_value=target_value, matched_label=matched_label)

    LOG.info(
        "Deterministic custom-select read-back failed after click; returning failure to avoid replaying over mutated widget",
        target_value=target_value,
        matched_element_id=element_id,
        matched_label=matched_label,
    )
    emit(CustomSelectFamilyOutcome.terminal_unverified_toggle)
    return _terminal_custom_select_failure(target_value=target_value, matched_label=matched_label)


async def _reset_custom_select_combobox_input(
    element: SkyvernElement | None,
    page: Page,
    engine_selection: BrowserEngineSelection | None = None,
    restore_value: str | None = None,
) -> bool:
    if element is None:
        return False
    try:
        locator = element.get_locator()
        await locator.fill("")
        await element.click(page=page, engine_selection=engine_selection)
        reset_verified = await get_input_value(element.get_tag_name(), locator, engine_selection=engine_selection) == ""
        if not reset_verified:
            return False
        if restore_value:
            await locator.fill(restore_value)
            return (
                await get_input_value(element.get_tag_name(), locator, engine_selection=engine_selection)
                == restore_value
            )
        return True
    except Exception:
        LOG.info(
            "Failed to reset custom-select combobox input before LLM fallback",
            exc_info=True,
        )
        return False


def _no_match_exception_for_dropdown(
    *,
    reasoning: str | None,
    target_value: str | None,
    observed_options: list[str],
    transient_fallback_element_id: str | None,
    widget_mutated: bool = False,
) -> Exception:
    """Return the right no-match exception: transient when the dropdown opened with zero options, permanent otherwise."""
    if not observed_options and transient_fallback_element_id is not None:
        return NoIncrementalElementFoundForCustomSelection(element_id=transient_fallback_element_id)
    exc = NoAvailableOptionFoundForCustomSelection(
        reason=reasoning,
        target_value=target_value or None,
        observed_options=observed_options,
    )
    exc.widget_mutated = widget_mutated
    return exc


def _extract_new_subtrees(elements: list[dict], new_ids: set[str]) -> list[dict]:
    """Walk *elements* and return the minimal set of subtrees rooted at new IDs.

    A "new root" is a node whose ``id`` is in *new_ids* but whose parent is
    not.  This avoids including the entire page tree when a new dropdown is
    injected inside an existing container — only the dropdown subtree (and its
    children, which may also be new) is returned.

    For portal-style dropdowns (appended as a direct ``<body>`` child), this
    behaves identically to a top-level filter.
    """
    result: list[dict] = []
    for element in elements:
        _collect_new_roots(element, new_ids, result)
    return result


def _collect_new_roots(element: dict, new_ids: set[str], out: list[dict]) -> None:
    if element.get("id") in new_ids:
        out.append(element)
        return
    for child in element.get("children", []):
        _collect_new_roots(child, new_ids, out)


def _custom_select_anchor_ownership(element: dict | None) -> tuple[str, frozenset[str]] | None:
    if not element:
        return None
    attributes = element.get("attributes") or {}
    role = str(attributes.get("role") or "").lower()
    owned_ids = frozenset(
        token
        for attribute in ("aria-controls", "aria-owns")
        for token in str(attributes.get(attribute) or "").split()
        if token
    )
    if role != "combobox" or not owned_ids:
        return None
    return role, owned_ids


def _resolve_owned_custom_select_recovery(
    *,
    original_anchor: dict | None,
    refreshed_page: ScrapedPage,
) -> tuple[str, list[dict]] | None:
    ownership = _custom_select_anchor_ownership(original_anchor)
    if ownership is None:
        return None

    matching_anchor_ids = _matching_custom_select_anchor_ids(ownership, refreshed_page)
    if len(matching_anchor_ids) != 1:
        return None

    owned_ids = ownership[1]
    matching_roots = [
        element
        for element in refreshed_page.elements
        if str((element.get("attributes") or {}).get("id") or "") in owned_ids
        and str((element.get("attributes") or {}).get("role") or "").lower() == "listbox"
        and element.get("id")
    ]
    if len(matching_roots) != 1:
        return None

    owned_subtrees = _extract_new_subtrees(
        refreshed_page.element_tree_trimmed,
        {str(matching_roots[0]["id"])},
    )
    if len(owned_subtrees) != 1:
        return None
    return matching_anchor_ids[0], owned_subtrees


def _matching_custom_select_anchor_ids(
    ownership: tuple[str, frozenset[str]],
    refreshed_page: ScrapedPage,
) -> list[str]:
    return [
        str(element["id"])
        for element in refreshed_page.elements
        if _custom_select_anchor_ownership(element) == ownership and element.get("id")
    ]


def _collect_subtree_element_ids(subtrees: list[dict]) -> list[str]:
    ids: list[str] = []
    stack = list(subtrees)
    while stack:
        node = stack.pop()
        node_id = node.get("id")
        if node_id:
            ids.append(str(node_id))
        stack.extend(node.get("children", []) or [])
    return ids


def _resolve_already_open_owned_listbox(
    *,
    current_element_id: str,
    scraped_page_after_open: ScrapedPage,
) -> tuple[str, list[dict]] | None:
    """Resolve the anchor's single aria-owned listbox when the strict new-element diff is empty
    because the combobox was already open.

    Returns ``(anchor_id, owned_subtrees)`` only when the anchor is a ``role=combobox`` that is
    currently ``aria-expanded=true`` and uniquely owns exactly one listbox subtree; otherwise
    ``None`` so the caller stays fail-closed and raises no-incremental.
    """
    original_anchor = scraped_page_after_open.id_to_element_dict.get(current_element_id)
    if original_anchor is None:
        return None
    attributes = original_anchor.get("attributes") or {}
    if str(attributes.get("aria-expanded") or "").lower() != "true":
        return None
    return _resolve_owned_custom_select_recovery(
        original_anchor=original_anchor,
        refreshed_page=scraped_page_after_open,
    )


@traced(name="skyvern.agent.dropdown.select_emerging")
async def select_from_emerging_elements(
    current_element_id: str,
    options: CustomSelectPromptOptions,
    page: Page,
    scraped_page: ScrapedPage,
    step: Step,
    task: Task,
    entry_action_type: str = "select_option",
    scraped_page_after_open: ScrapedPage | None = None,
    new_interactable_element_ids: list[str] | None = None,
    engine_selection: BrowserEngineSelection | None = UNSET_SELECTION,
) -> ActionResult:
    """
    This is the function to select an element from the new showing elements.
    Currently mainly used for the dropdown menu selection.
    """

    if engine_selection is UNSET_SELECTION:
        engine_selection = resolve_engine_selection_for_task(task, app.BROWSER_MANAGER)
    selection_group_id = str(uuid.uuid4())
    # TODO: support to handle the case when options are loaded by scroll
    scraped_page_after_open = scraped_page_after_open or await scraped_page.generate_scraped_page_without_screenshots()
    new_element_ids = set(scraped_page_after_open.id_to_css_dict.keys()) - set(scraped_page.id_to_css_dict.keys())

    dom_after_open = DomUtil(scraped_page=scraped_page_after_open, page=page)
    new_interactable_element_ids = new_interactable_element_ids or [
        element_id
        for element_id in new_element_ids
        if (await dom_after_open.get_skyvern_element_by_id(element_id)).is_interactable()
    ]

    if len(new_interactable_element_ids) == 0:
        already_open = _resolve_already_open_owned_listbox(
            current_element_id=current_element_id,
            scraped_page_after_open=scraped_page_after_open,
        )
        if already_open is None:
            raise NoIncrementalElementFoundForCustomSelection(element_id=current_element_id)
        _anchor_id, owned_subtrees = already_open
        new_element_ids = set(_collect_subtree_element_ids(owned_subtrees))
        new_interactable_element_ids = [
            element_id
            for element_id in new_element_ids
            if (await dom_after_open.get_skyvern_element_by_id(element_id)).is_interactable()
        ]
        if len(new_interactable_element_ids) == 0:
            raise NoIncrementalElementFoundForCustomSelection(element_id=current_element_id)
        LOG.info(
            "Custom-select combobox already open; selecting from its aria-owned listbox",
            current_element_id=current_element_id,
            owned_option_count=len(new_interactable_element_ids),
        )

    # Extract minimal subtrees rooted at new elements — avoids sending the full page DOM
    # which gets truncated on large pages, losing portal-rendered dropdown items.
    new_element_subtrees = _extract_new_subtrees(scraped_page_after_open.element_tree_trimmed, new_element_ids)
    shadow_candidate_elements: list[dict] = []
    _ctx = skyvern_context.current()
    lean_enabled = bool(_ctx and _ctx.enable_lean_element_tree)
    if new_element_subtrees:
        if lean_enabled:
            new_element_subtrees = apply_lean_to_tree(
                new_element_subtrees,
                compress_image_src=True,
                strip_url_query_strings=True,
                compress_nonnavigable_href=True,
            )
        shadow_candidate_elements = new_element_subtrees
        incremental_html = "".join(json_to_html(element, need_skyvern_attrs=True) for element in new_element_subtrees)
    else:
        LOG.warning(
            "No subtrees matched new element IDs; falling back to full element tree",
            current_element_id=current_element_id,
            new_element_id_count=len(new_element_ids),
        )
        # Keep the recipe consistent under the one flag (SKY-10076): apply lean to
        # the full trimmed tree on the fallback path too, mirroring the branch above.
        fallback_tree = scraped_page_after_open.element_tree_trimmed
        if lean_enabled:
            fallback_tree = apply_lean_to_tree(
                fallback_tree,
                compress_image_src=True,
                strip_url_query_strings=True,
                compress_nonnavigable_href=True,
            )
        shadow_candidate_elements = fallback_tree
        incremental_html = "".join(json_to_html(element, need_skyvern_attrs=True) for element in fallback_tree)
    LOG.debug(
        "Built HTML for emerging-element custom-select",
        current_element_id=current_element_id,
        new_interactable_count=len(new_interactable_element_ids),
        subtree_count=len(new_element_subtrees),
        html_length=len(incremental_html),
    )

    async def get_readback_scope_element() -> SkyvernElement | None:
        return await dom_after_open.get_skyvern_element_by_id(current_element_id)

    widget_mutated = False

    def _mark_widget_mutated() -> None:
        nonlocal widget_mutated
        widget_mutated = True

    deterministic_result = await _select_deterministic_custom_option(
        execute=False,
        target_value=options.target_value,
        get_option_candidates=lambda: _custom_select_candidates_from_elements(shadow_candidate_elements),
        field_context=options.model_dump(),
        page=page,
        get_skyvern_element=dom_after_open.get_skyvern_element_by_id,
        get_readback_scope_element=get_readback_scope_element,
        task=task,
        step=step,
        entry_action_type=entry_action_type,
        selection_group_id=selection_group_id,
        select_depth=0,
        on_click_attempted=_mark_widget_mutated,
        engine_selection=engine_selection,
    )
    if deterministic_result is not None:
        action_result, _matched_label = deterministic_result
        return action_result

    prompt = prompt_engine.load_prompt(
        "custom-select",
        is_date_related=options.is_date_related,
        field_information=options.field_information,
        required_field=options.required_field,
        target_value=options.target_value,
        navigation_goal=task.navigation_goal,
        new_elements_ids=new_interactable_element_ids,
        navigation_payload_str=json.dumps(task.navigation_payload),
        elements=incremental_html,
        local_datetime=datetime.now(skyvern_context.ensure_context().tz_info).isoformat(),
    )
    LOG.info("Calling LLM to find the match element", sampling=True)

    json_response = await get_org_aware_secondary_llm_api_handler(default=app.CUSTOM_SELECT_AGENT_LLM_API_HANDLER)(
        prompt=prompt, step=step, prompt_name="custom-select"
    )
    value: str | None = json_response.get("value", None)
    LOG.info(
        "LLM response for the matched element",
        sampling=True,
        matched_value=value,
        response=json_response,
    )

    # Check the no-match shape before ``ActionType`` coercion — coercing an empty
    # string raises ValueError and would mask the OPTION_NOT_AVAILABLE signal.
    raw_action_type: str = (json_response.get("action_type") or "").lower()
    element_id: str | None = json_response.get("id", None)
    requested_value = options.target_value if _normalize_select_shadow_text(options.target_value) else None
    if requested_value is None and _normalize_select_shadow_text(value):
        requested_value = value
    if requested_value is None and raw_action_type == ActionType.CLICK.value and element_id:
        clicked_candidates = [
            candidate
            for candidate in _custom_select_candidates_from_elements(shadow_candidate_elements)
            if candidate["element_id"] == element_id and _normalize_select_shadow_text(candidate["label"])
        ]
        if len(clicked_candidates) == 1:
            requested_value = clicked_candidates[0]["label"]
    _log_select_shadow_match(
        prompt_name="custom-select/emerging",
        target_value=options.target_value,
        get_candidates=lambda: _select_shadow_candidates_from_elements(shadow_candidate_elements),
        agreement=lambda candidates, matched_index: _select_shadow_agrees_with_element_choice(
            candidates,
            matched_index,
            llm_element_id=element_id,
            llm_value=value,
        ),
    )
    if not element_id or raw_action_type not in (ActionType.CLICK.value, ActionType.INPUT_TEXT.value):
        raise _no_match_exception_for_dropdown(
            reasoning=json_response.get("reasoning"),
            target_value=options.target_value,
            observed_options=_collect_option_texts(new_element_subtrees),
            transient_fallback_element_id=None,
            widget_mutated=widget_mutated,
        )
    action_type = ActionType(raw_action_type)

    new_ids_set = set(new_interactable_element_ids)
    if element_id not in new_ids_set:
        LOG.warning(
            "custom-select returned element outside new_interactable_element_ids",
            selected_element_id=element_id,
            new_interactable_count=len(new_ids_set),
        )

    if value is not None and action_type == ActionType.INPUT_TEXT:
        actual_value = get_actual_value_of_parameter_if_secret_with_task(task, value)
        is_dropdown_secret_value = actual_value != value
        is_dropdown_totp_value = _is_totp_sentinel(actual_value)
        LOG.info(
            "No clickable option found, but found input element to search",
            element_id=element_id,
        )
        input_element = await dom_after_open.get_skyvern_element_by_id(element_id)
        await input_element.scroll_into_view()
        await _apply_secret_visual_mask_if_needed(
            input_element,
            workflow_run_id=task.workflow_run_id,
            is_secret_value=is_dropdown_secret_value,
            is_totp_value=is_dropdown_totp_value,
        )
        current_text = await get_input_value(
            input_element.get_tag_name(),
            input_element.get_locator(),
            engine_selection=engine_selection,
        )
        if current_text == actual_value:
            return ActionSuccess()

        if await input_element.is_readonly(dynamic=True):
            LOG.warning(
                "Try to input text on a readonly element",
                element_id=element_id,
            )
            return ActionFailure(InputToReadonlyElement(element_id=element_id))

        await input_element.input_clear()
        await input_element.input_sequentially(actual_value)
        return ActionSuccess()

    else:
        selected_element = await dom_after_open.get_skyvern_element_by_id(element_id)
        if await selected_element.get_attr("role") == "listbox":
            return ActionFailure(exception=InteractWithDropdownContainer(element_id=element_id))

    original_anchor = scraped_page_after_open.id_to_element_dict.get(current_element_id)
    await selected_element.scroll_into_view()
    await selected_element.click(
        page=page,
        engine_selection=engine_selection,
        intercept_js_fallback_label=requested_value,
    )
    readback_scope_element = await _resolve_custom_select_readback_scope_element(
        get_readback_scope_element=get_readback_scope_element,
        target_value=options.target_value or "",
        matched_element_id=element_id,
        matched_label=requested_value,
    )
    anchor_is_combobox_input = await _anchor_is_combobox_input(readback_scope_element)
    initial_settles: list[_CustomSelectRenderSettle] = []
    verification_branch = "none"
    try:
        verified, verification_branch = await _verify_custom_select_option_with_settle(
            matched_element=selected_element,
            readback_scope_element=readback_scope_element,
            anchor_is_combobox_input=anchor_is_combobox_input,
            matched_element_id=element_id,
            matched_label=requested_value,
            use_strict_verification=True,
            settle_outcomes=initial_settles,
        )
        # The settle preceding each read-back inside _verify_custom_select_option_with_settle already
        # spans the render turn on which frameworks reconcile, so a next-render reset lands as a
        # non-committed read-back there and routes to recovery without a second confirming round.
    except Exception:
        LOG.info(
            "Custom-select primary commit verification exception",
            phase="initial_click",
            exc_info=True,
        )
        _log_custom_select_verification_outcome(
            "Custom-select commit verification outcome",
            phase="initial_click",
            settles=initial_settles,
            committed=False,
            verification_branch="none",
            verification_reason="verification_error",
            recovery_attempted=False,
            recovery_succeeded=False,
        )
        return _terminal_custom_select_failure(
            target_value=options.target_value or requested_value or "",
            matched_label=requested_value,
        )[0]
    _log_custom_select_verification_outcome(
        "Custom-select commit verification outcome",
        phase="initial_click",
        settles=initial_settles,
        committed=verified,
        verification_branch=verification_branch,
        verification_reason="verified" if verified else "not_committed",
        recovery_attempted=not verified,
        recovery_succeeded=False,
    )
    if verified:
        return ActionSuccess()

    recovery_settles: list[_CustomSelectRenderSettle] = []
    recovery_reason = "not_committed"
    try:
        refreshed_page = await scraped_page_after_open.generate_scraped_page_without_screenshots()
        refreshed_dom = DomUtil(scraped_page=refreshed_page, page=page, engine_selection=engine_selection)
        # A strict scope verify can read not-committed while the chosen option itself reflects the commit;
        # honor that BEFORE any ownership-dependent branch (missing, ambiguous, or no owned listbox) so a
        # set field is not failed just because recovery ownership cannot be resolved.
        if await _custom_select_committed_readback_confirms(selected_element, requested_value):
            _log_custom_select_verification_outcome(
                "Custom-select committed readback outcome",
                phase="committed_readback",
                settles=recovery_settles,
                committed=True,
                verification_branch="matched_state",
                verification_reason="committed_readback",
                recovery_attempted=False,
                recovery_succeeded=False,
            )
            return ActionSuccess()
        ownership = _custom_select_anchor_ownership(original_anchor)
        if ownership is None:
            raise ValueError("Custom-select recovery ownership is missing")
        matching_anchor_ids = _matching_custom_select_anchor_ids(ownership, refreshed_page)
        if len(matching_anchor_ids) != 1:
            raise ValueError("Custom-select recovery anchor is missing or ambiguous")
        recovered_anchor_id = matching_anchor_ids[0]
        recovered_anchor = await refreshed_dom.get_skyvern_element_by_id(recovered_anchor_id)
        if await recovered_anchor.get_locator().get_attribute("aria-expanded") != "true":
            await recovered_anchor.click(page=page, engine_selection=engine_selection)
            refreshed_page = await scraped_page_after_open.generate_scraped_page_without_screenshots()
            refreshed_dom = DomUtil(scraped_page=refreshed_page, page=page, engine_selection=engine_selection)
        owned_recovery = _resolve_owned_custom_select_recovery(
            original_anchor=original_anchor,
            refreshed_page=refreshed_page,
        )
        if owned_recovery is None:
            raise ValueError("Custom-select recovery ownership is missing or ambiguous")
        recovered_anchor_id, refreshed_option_elements = owned_recovery
        recovery_result = await _select_deterministic_custom_option(
            execute=True,
            target_value=requested_value,
            get_option_candidates=lambda: _custom_select_candidates_from_elements(refreshed_option_elements),
            field_context=options.model_dump(),
            page=page,
            get_skyvern_element=refreshed_dom.get_skyvern_element_by_id,
            get_readback_scope_element=lambda: refreshed_dom.get_skyvern_element_by_id(recovered_anchor_id),
            task=task,
            step=step,
            entry_action_type=entry_action_type,
            selection_group_id=selection_group_id,
            select_depth=1,
            settle_outcomes=recovery_settles,
            post_failed_click_commit_recovery=True,
            engine_selection=engine_selection,
        )
    except Exception:
        LOG.info(
            "Custom-select deterministic recovery exception",
            phase="recovery",
            exc_info=True,
        )
        recovery_reason = "recovery_error"
        recovery_result = None

    recovery_committed = False
    recovery_branch = "none"
    if recovery_result is not None and isinstance(recovery_result[0], ActionSuccess):
        try:
            recovered_label = requested_value if requested_value is not None else recovery_result[1]
            expected_label = _normalize_select_shadow_text(recovered_label)
            recovered_scope = await refreshed_dom.get_skyvern_element_by_id(recovered_anchor_id)
            recovery_settle = await _wait_custom_select_render_settle(recovered_scope)
            if isinstance(recovery_settle, _CustomSelectRenderSettle):
                recovery_settles.append(recovery_settle)
            recovery_committed, recovery_branch = await _custom_select_scope_confirms_committed(
                readback_scope_element=recovered_scope,
                anchor_is_combobox_input=await _anchor_is_combobox_input(recovered_scope),
                matched_element_id=element_id,
                matched_label=recovered_label,
                expected_label=expected_label,
                allow_aria_selected_option_tokens=False,
                allow_single_value_scope=True,
            )
        except Exception:
            LOG.info(
                "Custom-select recovery commit verification exception",
                phase="recovery",
                exc_info=True,
            )
            recovery_reason = "verification_error"
            recovery_committed = False
    _log_custom_select_verification_outcome(
        "Custom-select recovery outcome",
        phase="recovery",
        settles=recovery_settles,
        committed=recovery_committed,
        verification_branch=recovery_branch,
        verification_reason="verified" if recovery_committed else recovery_reason,
        recovery_attempted=True,
        recovery_succeeded=recovery_committed,
    )
    if recovery_committed and recovery_result is not None:
        return recovery_result[0]
    return _terminal_custom_select_failure(
        target_value=requested_value or "",
        matched_label=requested_value,
    )[0]


@traced(name="skyvern.agent.dropdown.select")
async def select_from_dropdown(
    context: InputOrSelectContext,
    page: Page,
    skyvern_element: SkyvernElement,
    skyvern_frame: SkyvernFrame,
    incremental_scraped: IncrementalScrapePage,
    check_filter_funcs: list[CheckFilterOutElementIDFunc],
    step: Step,
    task: Task,
    dropdown_menu_element: SkyvernElement | None = None,
    select_history: list[CustomSingleSelectResult] | None = None,
    force_select: bool = False,
    target_value: str = "",
    entry_action_type: str = "select_option",
    selection_group_id: str | None = None,
) -> CustomSingleSelectResult:
    """
    force_select: is used to choose an element to click even there's no dropdown menu;
    targe_value: only valid when force_select is "False". When target_value is not empty, the matched option must be relevant to target value;
    None will be only returned when:
        1. force_select is false and no dropdown menu popped
        2. force_select is false and match value is not relevant to the target value
    """
    select_history = [] if select_history is None else select_history
    single_select_result = CustomSingleSelectResult(skyvern_frame=skyvern_frame)

    timeout = settings.BROWSER_ACTION_TIMEOUT_MS

    if dropdown_menu_element is None:
        dropdown_menu_element = await locate_dropdown_menu(
            current_anchor_element=skyvern_element,
            incremental_scraped=incremental_scraped,
            step=step,
            task=task,
        )
    single_select_result.dropdown_menu = dropdown_menu_element

    if not force_select and dropdown_menu_element is None:
        return single_select_result

    if dropdown_menu_element:
        potential_scrollable_element = await try_to_find_potential_scrollable_element(
            skyvern_element=dropdown_menu_element,
            incremental_scraped=incremental_scraped,
            step=step,
            task=task,
        )

        if await skyvern_frame.get_element_scrollable(await potential_scrollable_element.get_element_handler()):
            await scroll_down_to_load_all_options(
                scrollable_element=potential_scrollable_element,
                skyvern_frame=skyvern_frame,
                page=page,
                incremental_scraped=incremental_scraped,
                step=step,
                task=task,
            )

    trimmed_element_tree = await incremental_scraped.get_incremental_element_tree(
        clean_and_remove_element_tree_factory(
            task=task,
            step=step,
            check_filter_funcs=check_filter_funcs,
            engine_selection=skyvern_frame.engine_selection,
        ),
    )
    incremental_scraped.set_element_tree_trimmed(trimmed_element_tree)
    html = incremental_scraped.build_element_tree(html_need_skyvern_attrs=True)

    widget_mutated = False
    post_reset_fallback = False
    post_reset_fallback_emit: Callable[[CustomSelectFamilyOutcome], None] | None = None
    selection_group_id = selection_group_id or str(uuid.uuid4())

    def _mark_widget_mutated() -> None:
        nonlocal widget_mutated
        widget_mutated = True

    def _mark_post_reset_fallback(emit: Callable[[CustomSelectFamilyOutcome], None]) -> None:
        nonlocal post_reset_fallback, post_reset_fallback_emit
        post_reset_fallback = True
        post_reset_fallback_emit = emit

    def _emit_post_reset_fallback_outcome(outcome: CustomSelectFamilyOutcome) -> None:
        if post_reset_fallback_emit is not None:
            post_reset_fallback_emit(outcome)

    def _terminal_post_reset_fallback_result() -> CustomSingleSelectResult:
        _emit_post_reset_fallback_outcome(CustomSelectFamilyOutcome.terminal_llm_fallback_exception)
        action_failure, _ = _terminal_custom_select_failure(
            target_value=target_value,
            matched_label=single_select_result.value,
        )
        single_select_result.reasoning = "LLM fallback failed after deterministic combobox reset"
        single_select_result.value = single_select_result.value or target_value
        single_select_result.action_type = ActionType.CLICK
        single_select_result.action_result = action_failure
        return single_select_result

    def _proceeded_post_reset_fallback_result() -> CustomSingleSelectResult:
        _emit_post_reset_fallback_outcome(CustomSelectFamilyOutcome.llm_fallback_reset_verified)
        return single_select_result

    deterministic_result = await _select_deterministic_custom_option(
        execute=entry_action_type in _EXECUTABLE_CUSTOM_SELECT_ENTRIES,
        target_value=target_value,
        get_option_candidates=lambda: _custom_select_candidates_from_elements(trimmed_element_tree),
        field_context=context.model_dump(),
        page=page,
        get_skyvern_element=lambda element_id: SkyvernElement.create_from_incremental(incremental_scraped, element_id),
        get_readback_scope_element=_readback_scope_element_provider(skyvern_element),
        task=task,
        step=step,
        entry_action_type=entry_action_type,
        selection_group_id=selection_group_id,
        select_depth=len(select_history),
        on_click_attempted=_mark_widget_mutated,
        on_reset_fallback=(
            _mark_post_reset_fallback
            if entry_action_type in _EXECUTABLE_CUSTOM_SELECT_ENTRIES and entry_action_type != "select_option"
            else None
        ),
        engine_selection=skyvern_frame.engine_selection,
    )
    if deterministic_result is not None:
        action_result, matched_label = deterministic_result
        single_select_result.reasoning = "Deterministic exact custom-select match"
        single_select_result.value = matched_label or target_value
        single_select_result.action_type = ActionType.CLICK
        single_select_result.action_result = action_result
        if isinstance(action_result, ActionSuccess):
            single_select_result.dropdown_menu = None
        return single_select_result

    skyvern_context = ensure_context()
    try:
        prompt = prompt_engine.load_prompt(
            "custom-select",
            is_date_related=context.is_date_related,
            field_information=context.field if not context.intention else context.intention,
            required_field=context.is_required,
            target_value=target_value,
            navigation_goal=task.navigation_goal,
            navigation_payload_str=json.dumps(task.navigation_payload),
            elements=html,
            select_history=json.dumps(build_sequential_select_history(select_history)) if select_history else "",
            local_datetime=datetime.now(skyvern_context.tz_info).isoformat(),
        )
        LOG.info("Calling LLM to find the match element", sampling=True)
        json_response = await get_org_aware_secondary_llm_api_handler(default=app.CUSTOM_SELECT_AGENT_LLM_API_HANDLER)(
            prompt=prompt,
            step=step,
            prompt_name="custom-select",
        )

        if post_reset_fallback and not isinstance(json_response, dict):
            raise TypeError("Custom-select LLM response must be a dictionary")
        value: str | None = json_response.get("value", None)
        single_select_result.value = value
        select_reason: str | None = json_response.get("reasoning", None)
        single_select_result.reasoning = select_reason

        LOG.info(
            "LLM response for the matched element",
            sampling=True,
            matched_value=value,
            response=json_response,
        )

        # Check the no-match shape before ``ActionType`` coercion — coercing an empty
        # string raises ValueError and would mask the OPTION_NOT_AVAILABLE signal.
        raw_action_type: str = (json_response.get("action_type") or "").lower()
        element_id: str | None = json_response.get("id", None)
        _log_select_shadow_match(
            prompt_name="custom-select/dropdown",
            target_value=target_value,
            get_candidates=lambda: _select_shadow_candidates_from_elements(trimmed_element_tree),
            agreement=lambda candidates, matched_index: _select_shadow_agrees_with_element_choice(
                candidates,
                matched_index,
                llm_element_id=element_id,
                llm_value=value,
            ),
        )
        if not element_id or raw_action_type not in (ActionType.CLICK.value, ActionType.INPUT_TEXT.value):
            raise _no_match_exception_for_dropdown(
                reasoning=json_response.get("reasoning"),
                target_value=target_value,
                observed_options=_collect_option_texts(trimmed_element_tree),
                transient_fallback_element_id=skyvern_element.get_id(),
                widget_mutated=widget_mutated,
            )
        single_select_result.action_type = ActionType(raw_action_type)
        action_type = single_select_result.action_type

        if not force_select and target_value and not json_response.get("relevant", False):
            LOG.info(
                "The selected option is not relevant to the target value",
                element_id=element_id,
            )
            if post_reset_fallback:
                return _terminal_post_reset_fallback_result()
            return single_select_result

        # A value-less or empty-value input_text response would either clear the anchor and type
        # nothing, or fall through to a click that focuses without committing — never a post-reset
        # success.
        if post_reset_fallback and action_type == ActionType.INPUT_TEXT and not value:
            return _terminal_post_reset_fallback_result()

        if value is not None and action_type == ActionType.INPUT_TEXT:
            LOG.info(
                "No clickable option found, but found input element to search",
                element_id=element_id,
            )
            try:
                actual_value = get_actual_value_of_parameter_if_secret_with_task(task, value)
                is_dropdown_secret_value = actual_value != value
                is_dropdown_totp_value = _is_totp_sentinel(actual_value)
                input_element = await SkyvernElement.create_from_incremental(incremental_scraped, element_id)
                await input_element.scroll_into_view()
                await _apply_secret_visual_mask_if_needed(
                    input_element,
                    workflow_run_id=task.workflow_run_id,
                    is_secret_value=is_dropdown_secret_value,
                    is_totp_value=is_dropdown_totp_value,
                )
                current_text = await get_input_value(
                    input_element.get_tag_name(),
                    input_element.get_locator(),
                    engine_selection=skyvern_frame.engine_selection,
                )
                if current_text == actual_value and not post_reset_fallback:
                    single_select_result.action_result = ActionSuccess()
                    return single_select_result

                if await input_element.is_readonly(dynamic=True):
                    LOG.warning(
                        "Try to input text on a readonly element",
                        element_id=element_id,
                        task_id=task.task_id,
                        step_id=step.step_id,
                    )
                    single_select_result.action_result = ActionFailure(InputToReadonlyElement(element_id=element_id))
                    if post_reset_fallback:
                        return _terminal_post_reset_fallback_result()
                    return single_select_result

                await input_element.input_clear()
                await input_element.input_sequentially(actual_value)
                single_select_result.action_result = ActionSuccess()
                return _proceeded_post_reset_fallback_result()
            except Exception as e:
                single_select_result.action_result = ActionFailure(exception=e)
                if post_reset_fallback:
                    return _terminal_post_reset_fallback_result()
                return single_select_result

        try:
            selected_element = await SkyvernElement.create_from_incremental(incremental_scraped, element_id)
            # TODO Some popup dropdowns include <select> element, we only handle the <select> element now, to prevent infinite recursion. Need to support more types of dropdowns.
            if selected_element.get_tag_name() == InteractiveElement.SELECT and value:
                await selected_element.scroll_into_view()
                action = SelectOptionAction(
                    reasoning=select_reason,
                    element_id=element_id,
                    option=SelectOption(label=value),
                    input_or_select_context=context,
                )
                results = await normal_select(
                    action=action,
                    skyvern_element=selected_element,
                    task=task,
                    step=step,
                    builder=incremental_scraped,
                    engine_selection=skyvern_frame.engine_selection,
                )
                assert len(results) > 0
                single_select_result.action_result = results[0]
                if post_reset_fallback and not isinstance(results[0], ActionSuccess):
                    return _terminal_post_reset_fallback_result()
                return _proceeded_post_reset_fallback_result()

            if await selected_element.get_attr("role") == "listbox":
                single_select_result.action_result = ActionFailure(
                    exception=InteractWithDropdownContainer(element_id=element_id)
                )
                if post_reset_fallback:
                    return _terminal_post_reset_fallback_result()
                return single_select_result

            await selected_element.scroll_into_view()
            await selected_element.click(
                page=page,
                timeout=timeout,
                engine_selection=skyvern_frame.engine_selection,
            )
            single_select_result.action_result = ActionSuccess()
            return _proceeded_post_reset_fallback_result()
        except (MissingElement, MissingElementDict, MissingElementInCSSMap, MultipleElementsFound):
            if not value:
                raise

        # sometimes we have multiple elements pointed to the same value,
        # but only one option is clickable on the page
        LOG.debug(
            "Searching option with the same value in incremental elements",
            value=value,
            elements=incremental_scraped.element_tree,
        )
        locator = await incremental_scraped.select_one_element_by_value(value=value)
        if not locator:
            single_select_result.action_result = ActionFailure(exception=MissingElement())
            if post_reset_fallback:
                return _terminal_post_reset_fallback_result()
            return single_select_result

        try:
            LOG.info(
                "Find an alternative option with the same value. Try to select the option.",
                value=value,
            )
            await EventStrategyFactory.move_to_element(page, locator)
            await locator.click(timeout=timeout)
            single_select_result.action_result = ActionSuccess()
            return _proceeded_post_reset_fallback_result()
        except Exception as e:
            single_select_result.action_result = ActionFailure(exception=e)
            if post_reset_fallback:
                return _terminal_post_reset_fallback_result()
            return single_select_result
    except Exception:
        if post_reset_fallback:
            return _terminal_post_reset_fallback_result()
        raise


def _no_element_matched_failure(value: str, reason: str) -> ActionFailure:
    return ActionFailure(NoElementMatchedForTargetOption(target=value, reason=reason))


@traced(name="skyvern.agent.dropdown.select_by_value")
async def select_from_dropdown_by_value(
    value: str,
    page: Page,
    skyvern_element: SkyvernElement,
    skyvern_frame: SkyvernFrame,
    dom: DomUtil,
    incremental_scraped: IncrementalScrapePage,
    task: Task,
    step: Step,
    dropdown_menu_element: SkyvernElement | None = None,
) -> ActionResult:
    timeout = settings.BROWSER_ACTION_TIMEOUT_MS
    await incremental_scraped.get_incremental_element_tree(
        clean_and_remove_element_tree_factory(
            task=task,
            step=step,
            check_filter_funcs=[check_existed_but_not_option_element_in_dom_factory(dom)],
            engine_selection=skyvern_frame.engine_selection,
        ),
    )

    element_locator = await incremental_scraped.select_one_element_by_value(value=value)
    if element_locator is not None:
        await element_locator.click(timeout=timeout)
        return ActionSuccess()

    if dropdown_menu_element is None:
        dropdown_menu_element = await locate_dropdown_menu(
            current_anchor_element=skyvern_element,
            incremental_scraped=incremental_scraped,
            step=step,
            task=task,
        )

    if not dropdown_menu_element:
        return _no_element_matched_failure(value=value, reason="No value matched")

    potential_scrollable_element = await try_to_find_potential_scrollable_element(
        skyvern_element=dropdown_menu_element,
        incremental_scraped=incremental_scraped,
        task=task,
        step=step,
    )
    if not await skyvern_frame.get_element_scrollable(await potential_scrollable_element.get_element_handler()):
        return _no_element_matched_failure(
            value=value,
            reason="No value matched and element can't scroll to find more options",
        )

    selected: bool = False

    async def continue_callback(incre_scraped: IncrementalScrapePage) -> bool:
        await incre_scraped.get_incremental_element_tree(
            clean_and_remove_element_tree_factory(
                task=task,
                step=step,
                check_filter_funcs=[check_existed_but_not_option_element_in_dom_factory(dom)],
                engine_selection=skyvern_frame.engine_selection,
            ),
        )

        element_locator = await incre_scraped.select_one_element_by_value(value=value)
        if element_locator is not None:
            await element_locator.click(timeout=timeout)
            nonlocal selected
            selected = True
            return False

        return True

    await scroll_down_to_load_all_options(
        scrollable_element=potential_scrollable_element,
        page=page,
        skyvern_frame=skyvern_frame,
        incremental_scraped=incremental_scraped,
        step=step,
        task=task,
        page_by_page=True,
        is_continue=continue_callback,
    )

    if selected:
        return ActionSuccess()

    return _no_element_matched_failure(value=value, reason="No value matched after scrolling")


async def locate_dropdown_menu(
    current_anchor_element: SkyvernElement,
    incremental_scraped: IncrementalScrapePage,
    step: Step,
    task: Task,
) -> SkyvernElement | None:
    # the anchor must exist in the DOM, but no need to be visible css style
    if not await current_anchor_element.is_visible(must_visible_style=False):
        return None

    skyvern_frame = incremental_scraped.skyvern_frame

    for idx, element_dict in enumerate(incremental_scraped.element_tree):
        # FIXME: confirm max to 10 nodes for now, preventing sendindg too many requests to LLM
        if idx >= 10:
            break

        element_id = element_dict.get("id")
        if not element_id:
            LOG.debug(
                "Skip the element without id for the dropdown menu confirm",
                element=element_dict,
            )
            continue

        try:
            head_element = await SkyvernElement.create_from_incremental(incremental_scraped, element_id)
        except Exception:
            LOG.debug(
                "Failed to get head element in the incremental page",
                element_id=element_id,
                exc_info=True,
            )
            continue

        try:
            if not await head_element.is_next_to_element(
                target_locator=current_anchor_element.get_locator(),
                max_x_distance=DROPDOWN_MENU_MAX_DISTANCE,
                max_y_distance=DROPDOWN_MENU_MAX_DISTANCE,
            ):
                LOG.debug(
                    "Skip the element since it's too far away from the anchor element",
                    element_id=element_id,
                )
                continue

        except Exception:
            LOG.info(
                "Failed to calculate the distance between the elements",
                element_id=element_id,
                exc_info=True,
            )
            continue

        if not await skyvern_frame.get_element_visible(head_element.get_locator()):
            LOG.debug(
                "Skip the element since it's invisible",
                element_id=element_id,
            )
            continue

        ul_or_listbox_element_id = await head_element.find_children_element_id_by_callback(
            cb=is_ul_or_listbox_element_factory(incremental_scraped=incremental_scraped, task=task, step=step),
        )

        if ul_or_listbox_element_id:
            try:
                await SkyvernElement.create_from_incremental(incremental_scraped, ul_or_listbox_element_id)
                LOG.info(
                    "Confirm it's an opened dropdown menu since it includes <ul> or <role='listbox'>",
                    sampling=True,
                    element_id=element_id,
                )
                return await SkyvernElement.create_from_incremental(
                    incre_page=incremental_scraped, element_id=element_id
                )
            except Exception:
                LOG.debug(
                    "Failed to get <ul> or <role='listbox'> element in the incremental page",
                    element_id=element_id,
                    exc_info=True,
                )
        # check if opening react-datetime datepicker: https://github.com/arqex/react-datetime
        class_name = await head_element.get_attr("class", mode="static")
        if class_name and "rdtOpen" in class_name:
            LOG.info(
                "Confirm it's an opened React-Datetime datepicker",
                element_id=element_id,
            )
            return head_element

        # sometimes taking screenshot might scroll away, need to scroll back after the screenshot
        x, y = await skyvern_frame.get_scroll_x_y()
        try:
            screenshot = await take_element_screenshot(
                head_element.get_locator(),
                timeout=settings.BROWSER_SCREENSHOT_TIMEOUT_MS,
                engine_selection=skyvern_frame.engine_selection,
            )
        except FailedToTakeScreenshot:
            LOG.debug(
                "Failed to screenshot dropdown candidate, skipping it",
                element_id=element_id,
                exc_info=True,
            )
            # capture may have scrolled the candidate into view; restore before the next candidate
            await skyvern_frame.safe_scroll_to_x_y(x, y)
            continue
        await skyvern_frame.scroll_to_x_y(x, y)

        # TODO: better to send untrimmed HTML without skyvern attributes in the future
        dropdown_confirm_prompt = prompt_engine.load_prompt(
            "opened-dropdown-confirm",
        )
        LOG.debug(
            "Confirm if it's an opened dropdown menu",
            element=element_dict,
        )
        json_response = await get_org_aware_secondary_llm_api_handler(default=app.SECONDARY_LLM_API_HANDLER)(
            prompt=dropdown_confirm_prompt, screenshots=[screenshot], step=step, prompt_name="opened-dropdown-confirm"
        )
        is_opened_dropdown_menu = json_response.get("is_opened_dropdown_menu")
        if is_opened_dropdown_menu:
            LOG.info(
                "Opened dropdown menu found",
                element_id=element_id,
            )
            return await SkyvernElement.create_from_incremental(incre_page=incremental_scraped, element_id=element_id)
    return None


async def try_to_find_potential_scrollable_element(
    skyvern_element: SkyvernElement,
    incremental_scraped: IncrementalScrapePage,
    task: Task,
    step: Step,
) -> SkyvernElement:
    """
    check any <ul> or <role="listbox"> element in the chidlren.
    if yes, return the found element,
    else, return the orginal one
    """
    found_element_id = await skyvern_element.find_children_element_id_by_callback(
        cb=is_ul_or_listbox_element_factory(incremental_scraped=incremental_scraped, task=task, step=step),
    )
    if found_element_id and found_element_id != skyvern_element.get_id():
        LOG.debug(
            "Found 'ul or listbox' element in children list",
            element_id=found_element_id,
        )

        try:
            skyvern_element = await SkyvernElement.create_from_incremental(incremental_scraped, found_element_id)
        except Exception:
            LOG.debug(
                "Failed to get head element by found element id, use the original element id",
                element_id=found_element_id,
                exc_info=True,
            )
    return skyvern_element


@traced(name="skyvern.agent.dropdown.scroll_load_options")
async def scroll_down_to_load_all_options(
    scrollable_element: SkyvernElement,
    page: Page,
    skyvern_frame: SkyvernFrame,
    incremental_scraped: IncrementalScrapePage,
    step: Step | None = None,
    task: Task | None = None,
    page_by_page: bool = False,
    is_continue: Callable[[IncrementalScrapePage], Awaitable[bool]] | None = None,
) -> None:
    LOG.info("Scroll down the dropdown menu to load all options")
    timeout = settings.BROWSER_ACTION_TIMEOUT_MS

    dropdown_menu_element_handle = await scrollable_element.get_locator().element_handle(timeout=timeout)
    if dropdown_menu_element_handle is None:
        LOG.info("element handle is None, using focus to move the cursor", element_id=scrollable_element.get_id())
        await scrollable_element.get_locator().focus(timeout=timeout)
    else:
        try:
            await dropdown_menu_element_handle.scroll_into_view_if_needed(timeout=timeout)
        except Exception as exc:
            if not _is_selected_engine_timeout(exc, skyvern_frame.engine_selection) and not is_element_detached_error(
                exc
            ):
                raise
            # A detached handle can't be reused below either, so null it out to take the
            # existing None-handle fallback path for the rest of this function.
            LOG.info(
                "Dropdown-menu element detached mid-scroll, falling back to focus",
                element_id=scrollable_element.get_id(),
            )
            await scrollable_element.get_locator().focus(timeout=timeout)
            dropdown_menu_element_handle = None

    await scrollable_element.move_mouse_to_safe(page=page)

    scroll_pace = 0
    previous_num = await incremental_scraped.get_incremental_elements_num()

    deadline = datetime.now(timezone.utc) + timedelta(milliseconds=settings.OPTION_LOADING_TIMEOUT_MS)
    while datetime.now(timezone.utc) < deadline:
        # make sure we can scroll to the bottom
        scroll_interval = settings.BROWSER_HEIGHT * 5
        if dropdown_menu_element_handle is None:
            LOG.info("element handle is None, using mouse to scroll down", element_id=scrollable_element.get_id())
            await page.mouse.wheel(0, scroll_interval)
            scroll_pace += scroll_interval
        else:
            await skyvern_frame.scroll_to_element_bottom(dropdown_menu_element_handle, page_by_page)
            # wait until animation ends, otherwise the scroll operation could be overwritten
            await skyvern_frame.safe_wait_for_animation_end(before_wait_sec=0.5, caller="scroll_options.scroll")

        # scroll a little back and scroll down to trigger the loading
        await page.mouse.wheel(0, -1e-5)
        await page.mouse.wheel(0, 1e-5)
        # wait for while to load new options
        await skyvern_frame.safe_wait_for_animation_end(before_wait_sec=0.5, caller="scroll_options.trigger")

        current_num = await incremental_scraped.get_incremental_elements_num()
        LOG.info(
            "Current incremental elements count during the scrolling",
            num=current_num,
        )

        if is_continue is not None and not await is_continue(incremental_scraped):
            return

        if previous_num == current_num:
            break
        previous_num = current_num
    else:
        LOG.warning("Timeout to load all options, maybe some options will be missed")

    # scroll back to the start point and wait for a while to make all options invisible on the page
    if dropdown_menu_element_handle is None:
        LOG.info("element handle is None, using mouse to scroll back", element_id=scrollable_element.get_id())
        await page.mouse.wheel(0, -scroll_pace)
    else:
        await skyvern_frame.scroll_to_element_top(dropdown_menu_element_handle)
    await skyvern_frame.safe_wait_for_animation_end(before_wait_sec=0.5, caller="scroll_options.top")


async def normal_select(
    action: actions.SelectOptionAction,
    skyvern_element: SkyvernElement,
    task: Task,
    step: Step,
    builder: ElementTreeBuilder,
    engine_selection: BrowserEngineSelection | None = UNSET_SELECTION,
) -> List[ActionResult]:
    collapse_select_fanout_enabled = await _is_collapse_select_fanout_enabled(task)
    if not collapse_select_fanout_enabled:
        action.set_has_mini_agent()

    try:
        current_text = await skyvern_element.get_attr("selected")
        if current_text and (current_text == action.option.label or current_text == action.option.value):
            return [ActionSuccess()]
    except Exception:
        LOG.info("failed to confirm if the select option has been done, force to take the action again.")

    action_result: List[ActionResult] = []
    is_success = False
    locator = skyvern_element.get_locator()

    input_or_select_context = await _get_input_or_select_context(
        action=action,
        element_tree_builder=builder,
        task=task,
        step=step,
        skyvern_element=skyvern_element,
        engine_selection=engine_selection,
    )
    LOG.debug(
        "Parsed input/select context",
        context=input_or_select_context,
    )

    select_options_result = await skyvern_element.refresh_select_options()
    select_options = select_options_result[0] if select_options_result else skyvern_element.get_options()
    target_value = _select_option_target_value(action.option)
    if target_value and select_options and collapse_select_fanout_enabled:
        option_labels, option_values = _select_option_labels_and_values(select_options)
        resolution = await app.AGENT_FUNCTION.resolve_field_option(
            target_value=target_value,
            option_labels=option_labels,
            option_values=option_values,
            field_context=input_or_select_context.model_dump(),
            url=task.url,
            organization_id=task.organization_id,
        )
        if not resolution.fallback_to_llm and resolution.matched_index is not None:
            deterministic_result = await _select_deterministic_normal_option(
                action=action,
                skyvern_element=skyvern_element,
                locator=locator,
                matched_label=resolution.matched_label,
                matched_value=resolution.matched_value,
                matched_index=resolution.matched_index,
            )
            if _normal_select_successful(deterministic_result) and not await _normal_select_readback_contradicts(
                locator=locator,
                matched_index=resolution.matched_index,
                matched_label=resolution.matched_label,
                matched_value=resolution.matched_value,
            ):
                return deterministic_result

            LOG.info(
                "Deterministic normal-select failed; falling back to LLM path",
                action=action,
                target_value=target_value,
                matched_index=resolution.matched_index,
                matched_label=resolution.matched_label,
            )

    if collapse_select_fanout_enabled:
        action.set_has_mini_agent()
    options_html = skyvern_element.build_HTML()
    field_information = (
        input_or_select_context.field if not input_or_select_context.intention else input_or_select_context.intention
    )
    prompt = prompt_engine.load_prompt(
        "normal-select",
        field_information=field_information,
        required_field=input_or_select_context.is_required,
        navigation_goal=task.navigation_goal,
        navigation_payload_str=json.dumps(task.navigation_payload),
        options=options_html,
        local_datetime=datetime.now(skyvern_context.ensure_context().tz_info).isoformat(),
    )

    json_response = await get_org_aware_secondary_llm_api_handler(default=app.NORMAL_SELECT_AGENT_LLM_API_HANDLER)(
        prompt=prompt, step=step, prompt_name="normal-select"
    )
    index: int | None = json_response.get("index")
    value: str | None = json_response.get("value")
    _log_select_shadow_match(
        prompt_name="normal-select",
        target_value=action.option.label or action.option.value,
        get_candidates=lambda: _select_shadow_candidates_from_select_options(select_options),
        agreement=lambda candidates, matched_index: _select_shadow_agrees_with_native_choice(
            candidates,
            matched_index,
            llm_index=index,
            llm_value=value,
        ),
    )

    await _best_effort_focus_click_before_select(locator=locator, action=action)

    if not is_success and value is not None:
        try:
            # click by value (if it matches)
            await locator.select_option(
                value=value,
                timeout=settings.BROWSER_ACTION_TIMEOUT_MS,
            )
            is_success = True
            action_result.append(ActionSuccess())
        except Exception:
            action_result.append(ActionFailure(FailToSelectByValue(action.element_id)))
            LOG.info(
                "Failed to take select action by value",
                exc_info=True,
                action=action,
                locator=locator,
            )

    if not is_success and value is not None:
        try:
            # click by label (if it matches)
            await locator.select_option(
                label=value,
                timeout=settings.BROWSER_ACTION_TIMEOUT_MS,
            )
            is_success = True
            action_result.append(ActionSuccess())
        except Exception:
            action_result.append(ActionFailure(FailToSelectByLabel(action.element_id)))
            LOG.info(
                "Failed to take select action by label",
                exc_info=True,
                action=action,
                locator=locator,
            )

    if not is_success and index is not None:
        if index >= len(skyvern_element.get_options()):
            action_result.append(ActionFailure(OptionIndexOutOfBound(action.element_id)))
            LOG.info(
                "option index is out of bound",
                action=action,
                locator=locator,
            )
        else:
            try:
                # This means the supplied index was for the select element, not a reference to the css dict
                await locator.select_option(
                    index=index,
                    timeout=settings.BROWSER_ACTION_TIMEOUT_MS,
                )
                is_success = True
                action_result.append(ActionSuccess())
            except Exception:
                action_result.append(ActionFailure(FailToSelectByIndex(action.element_id)))
                LOG.info(
                    "Failed to click on the option by index",
                    exc_info=True,
                    action=action,
                    locator=locator,
                )

    if len(action_result) == 0:
        action_result.append(ActionFailure(EmptySelect(element_id=action.element_id)))

    return action_result


def get_anchor_to_click(scraped_page: ScrapedPage, element_id: str) -> str | None:
    """
    Get the anchor tag under the label to click
    """
    LOG.info("Getting anchor tag to click", element_id=element_id)
    for ele in scraped_page.elements:
        if "id" in ele and ele["id"] == element_id:
            for child in ele["children"]:
                if "tagName" in child and child["tagName"] == "a":
                    return scraped_page.id_to_css_dict[child["id"]]
    return None


def get_select_id_in_label_children(scraped_page: ScrapedPage, element_id: str) -> str | None:
    """
    search <select> in the children of <label>
    """
    LOG.info("Searching select in the label children", element_id=element_id)
    element = scraped_page.id_to_element_dict.get(element_id, None)
    if element is None:
        return None

    for child in element.get("children", []):
        if child.get("tagName", "") == "select":
            return child.get("id", None)

    return None


def get_checkbox_id_in_label_children(scraped_page: ScrapedPage, element_id: str) -> str | None:
    """
    search checkbox/radio in the children of <label>
    """
    LOG.info("Searching checkbox/radio in the label children", element_id=element_id)
    element = scraped_page.id_to_element_dict.get(element_id, None)
    if element is None:
        return None

    for child in element.get("children", []):
        if child.get("tagName", "") == "input" and child.get("attributes", {}).get("type") in ["checkbox", "radio"]:
            return child.get("id", None)

    return None


def _schedule_extraction_shadow_check_for_hit(
    *,
    task: Task,
    workflow_run_id: str,
    cache_key: str,
    cached_value: Any,
    cached_age_seconds: float,
    scraped_page: ScrapedPage,
    llm_key_override: str | None,
    extract_information_prompt: str,
) -> None:
    shadow_llm_api_handler = LLMAPIHandlerFactory.get_override_llm_api_handler(
        llm_key_override,
        default=get_org_aware_primary_llm_api_handler(default=app.EXTRACTION_LLM_API_HANDLER),
    )
    shadow_schema = task.extracted_information_schema
    # Snapshot screenshots at schedule time — scraped_page is mutable
    # and may be refreshed before the background task runs.
    shadow_screenshots = list(scraped_page.screenshots)

    async def _shadow_gate() -> bool:
        # Captures `task` by reference — safe because the cloud override
        # only reads immutable identifiers (workflow_run_id, organization_id,
        # workflow_permanent_id, task_id) set at construction.
        return await app.AGENT_FUNCTION.should_shadow_extraction_cache_hit(task)

    async def _shadow_llm_call() -> Any:
        fresh = await shadow_llm_api_handler(
            prompt=extract_information_prompt,
            # step=None suppresses both update_step (token/cost accounting)
            # and artifact persistence in LLMAPIHandlerFactory. Shadow calls
            # are an observability side-channel — the user-visible request
            # was served from cache, so they must not inflate step usage,
            # billing, or artifact counts.
            step=None,
            screenshots=shadow_screenshots,
            # Use the same prompt_name as the miss path so prompt-level
            # LLM tuning (e.g. thinking-budget overrides) matches — otherwise
            # cached (tuned) vs fresh (untuned) would diverge for config
            # reasons unrelated to cache correctness.
            prompt_name="extract-information",
            force_dict=False,
            system_prompt=task.workflow_system_prompt,
        )
        # Apply the same post-processing the miss path applies so the
        # comparison is apples-to-apples against the cached value.
        if shadow_schema and extraction_shape_matches(fresh, shadow_schema):
            fresh = validate_and_fill_extraction_result(
                extraction_result=fresh,
                schema=shadow_schema,
            )
        return fresh

    # Bind prompt_name + cache_path so Datadog can split the shared
    # extract_information.shadow_comparison stream by call site.
    shadow_logger = structlog.get_logger().bind(
        prompt_name="extract-information",
        cache_path="handler",
    )
    extraction_shadow.schedule_shadow_check(
        gate=_shadow_gate,
        cache_key=cache_key,
        workflow_run_id=workflow_run_id,
        cached_value=cached_value,
        cached_age_seconds=cached_age_seconds,
        llm_call=_shadow_llm_call,
        schema=shadow_schema,
        logger=shadow_logger,
    )


async def extract_information_for_navigation_goal(
    task: Task,
    step: Step,
    scraped_page: ScrapedPage,
    *,
    page: Page,
) -> ScrapeResult:
    """
    Scrapes a webpage and returns the scraped response, including:
    1. JSON representation of what the user is seeing
    2. The scraped page

    Extraction-result cache
    --------------------------------
    Many workflows re-extract the same page on every iteration of a loop
    (e.g. navigate back to a documents list, extract, click one row, repeat).
    When the page content, data-extraction goal, and output schema are
    identical to a previous call within the same workflow run, reuse the
    prior LLM result instead of paying for another extract-information call.
    """
    context = ensure_context()
    context.scrape_trigger = "extraction"
    context.scrape_screenshots_consumed = True
    scraped_page_refreshed = await scraped_page.refresh()
    # Complete-row harvest for a server-windowed virtualized data grid, injected into
    # the prompt below. Behind an AgentFunction seam (framework-specific collectors are
    # deployment overrides) and a fail-open boundary so collection never blocks extraction.
    virtualized_grid_rows: str | None = None
    try:
        virtualized_grid_rows = await app.AGENT_FUNCTION.collect_virtualized_grid_rows(task=task, page=page)
    except Exception:
        virtualized_grid_rows = None
        LOG.warning("virtualized_grid_collection_failed")

    # task.workflow_permanent_id is None on most fetch paths (tasks table has
    # no such column); fall back to context. SKY-8992.
    wpid_for_cache = task.workflow_permanent_id or context.workflow_permanent_id

    # Compute llm key up-front so the cache key includes it.
    llm_key_override = task.llm_key
    if await service_utils.is_cua_task(task=task):
        # CUA tasks should use the default data extraction llm key
        llm_key_override = None

    # Rendered into the prompt as ``{{ local_datetime }}``. Intentionally not
    # part of the cache key — content-hash alone defines cache identity, so
    # two calls on byte-identical pages hit the cache regardless of wall clock.
    local_datetime_str = datetime.now(context.tz_info).isoformat()

    extracted_text_for_prompt = scraped_page_refreshed.extracted_text if task.include_extracted_text else None

    previous_info_capped = truncate_previous_extracted_information(task.extracted_information)
    capped_schema = truncate_extraction_schema(task.extracted_information_schema)
    # Normalize error_code_mapping to the exact string the prompt will render
    # (None when falsy). Hashing this value below — instead of the raw dict —
    # means None and {} collapse to one key since both drop the prompt block.
    error_code_mapping_str = json.dumps(task.error_code_mapping) if task.error_code_mapping else None

    # Render the prompt FIRST so the cache key hashes the exact string that
    # will be sent to the LLM (captures economy-tree swaps and 2/3 truncation
    # inside load_prompt_with_elements). Use the _tracked variant so the cache
    # key below can hash the post-ceiling values — when the prompt exceeds the
    # hard ceiling, `enforce_prompt_ceiling` drops fields to None, and two
    # requests that render to the same final LLM prompt must share a key.
    extract_information_prompt, post_ceiling_kwargs = load_prompt_with_elements_tracked(
        element_tree_builder=scraped_page_refreshed,
        prompt_engine=prompt_engine,
        template_name="extract-information",
        html_need_skyvern_attrs=False,
        navigation_goal=task.navigation_goal,
        navigation_payload=task.navigation_payload,
        previous_extracted_information=previous_info_capped,
        data_extraction_goal=task.data_extraction_goal,
        extracted_information_schema=capped_schema,
        current_url=scraped_page_refreshed.url,
        extracted_text=extracted_text_for_prompt,
        error_code_mapping_str=error_code_mapping_str,
        local_datetime=local_datetime_str,
        virtualized_grid_rows=virtualized_grid_rows,
    )
    post_ceiling_grid_rows = post_ceiling_kwargs.get("virtualized_grid_rows")
    if virtualized_grid_rows is not None and post_ceiling_grid_rows is None:
        LOG.warning("virtualized_grid_rows_dropped_from_prompt")

    # Self-heal guard: on the second retry onward (``retry_index > 1``) the
    # previous attempts' cached result is suspect — the first retry already
    # failed to complete, so continuing to hand the same cached value back
    # is not going to recover. Bypass both cache tiers on retry #2+ and
    # force a fresh LLM call; the dual-write after extraction overwrites
    # both the in-run entry and the cross-run Redis entry.
    # Retry #1 still uses the cache: transient failures (network blip,
    # downstream flake) often recover without the extraction itself being
    # the cause, and paying the LLM cost on every first retry would burn
    # hit rate for no self-heal benefit.
    is_retry_step = step.retry_index > 1

    # Best-effort cache lookup — any failure falls through to LLM. The `try`
    # is narrowed to just compute_cache_key + lookup so a downstream log
    # failure can't re-enter the except block and double-count the call as
    # both a hit/miss and a `lookup_error` in the Datadog miss-reason metric.
    cache_key: str | None = None
    lookup_result: extraction_cache.LookupResult | None = None
    try:
        # Use the variant of the element tree that load_prompt_with_elements
        # actually rendered (could be economy or 2/3-truncated under token
        # pressure). Falls back to a fresh HTML build when the prior build
        # used fmt=JSON (field is None in that case). The fallback call
        # mutates `last_used_element_tree{_html}` on scraped_page_refreshed;
        # this is intentional — nothing downstream reads those fields after
        # the cache key is computed.
        # Hash the post-ceiling values for fields that enforce_prompt_ceiling
        # may drop (previous_extracted_information / extracted_information_schema /
        # extracted_text). When those fields are dropped, two requests that
        # differ only in the dropped values render identical final prompts and
        # must share a cache key. `extracted_text` also respects
        # include_extracted_text (None when disabled). Only `element_tree` is
        # hashed post-sanitization; the other fields hash pre-filter, which can
        # cost an extra miss but never a wrong hit.
        cache_key = extraction_cache.compute_cache_key(
            call_path="handler",
            element_tree=scraped_page_refreshed.last_used_element_tree_html
            or scraped_page_refreshed.build_element_tree(html_need_skyvern_attrs=False),
            extracted_text=post_ceiling_kwargs["extracted_text"],
            current_url=scraped_page_refreshed.url,
            data_extraction_goal=task.data_extraction_goal,
            extracted_information_schema=post_ceiling_kwargs["extracted_information_schema"],
            navigation_payload=task.navigation_payload,
            error_code_mapping=error_code_mapping_str,
            previous_extracted_information=post_ceiling_kwargs["previous_extracted_information"],
            llm_key=llm_key_override,
            workflow_system_prompt=task.workflow_system_prompt,
            virtualized_grid_rows=post_ceiling_grid_rows,
        )
        if is_retry_step:
            # Proactively evict the in-run entry. The cross-run tier will be
            # overwritten by the store() after the LLM call below.
            evicted = extraction_cache.invalidate_key(task.workflow_run_id, cache_key)
            LOG.info(
                "extract_information cache bypassed on retry (self-heal)",
                task_id=task.task_id,
                workflow_run_id=task.workflow_run_id,
                step_id=step.step_id,
                retry_index=step.retry_index,
                cache_key=cache_key,
                cache_hit=False,
                # Covers both tiers — the in-run entry is evicted here and the
                # cross-run entry will be overwritten by the store() below.
                cache_scope=extraction_cache.SCOPE_RUN,
                cache_age_seconds=None,
                fallback_reason="retry_bypass",
                in_run_entry_evicted=evicted,
                cache_path="handler",
            )
        else:
            lookup_result = extraction_cache.lookup(task.workflow_run_id, cache_key)
    except Exception:
        LOG.warning(
            "extract_information cache lookup failed; falling through to LLM",
            task_id=task.task_id,
            workflow_run_id=task.workflow_run_id,
            cache_key=cache_key,
            cache_hit=False,
            cache_scope=extraction_cache.SCOPE_RUN,
            cache_age_seconds=None,
            fallback_reason=extraction_cache.FALLBACK_LOOKUP_ERROR,
            cache_path="handler",
            exc_info=True,
        )
        # Preserve cache_key so the downstream store() can still warm the cache
        # for subsequent identical calls even when lookup() fails transiently.

    if lookup_result is not None and lookup_result.hit and isinstance(lookup_result.value, (dict, list, str)):
        LOG.info(
            "extract_information cache hit — skipping LLM call",
            task_id=task.task_id,
            workflow_run_id=task.workflow_run_id,
            cache_key=cache_key,
            cache_hit=True,
            cache_scope=lookup_result.scope,
            cache_age_seconds=lookup_result.age_seconds,
            fallback_reason=None,
            cache_path="handler",
        )
        # Fire-and-forget shadow sampling on sampled hits. Flag lookup happens
        # inside the background task so the cache-hit return is not blocked
        # by the flag provider (e.g. PostHog latency on the first hit per run).
        if cache_key is not None and task.workflow_run_id is not None:
            _schedule_extraction_shadow_check_for_hit(
                task=task,
                workflow_run_id=task.workflow_run_id,
                cache_key=cache_key,
                cached_value=lookup_result.value,
                cached_age_seconds=lookup_result.age_seconds
                if lookup_result.age_seconds is not None
                else extraction_shadow.UNKNOWN_CACHE_AGE_SENTINEL,
                scraped_page=scraped_page,
                llm_key_override=llm_key_override,
                extract_information_prompt=extract_information_prompt,
            )
        return ScrapeResult(scraped_data=lookup_result.value)
    if lookup_result is not None and lookup_result.hit:
        LOG.warning(
            "extract_information cache hit returned non-cacheable value type; falling through to LLM",
            task_id=task.task_id,
            workflow_run_id=task.workflow_run_id,
            cache_key=cache_key,
            value_type=type(lookup_result.value).__name__,
            cache_path="handler",
        )
    elif lookup_result is not None:
        LOG.info(
            "extract_information cache miss",
            task_id=task.task_id,
            workflow_run_id=task.workflow_run_id,
            cache_key=cache_key,
            cache_hit=False,
            cache_scope=lookup_result.scope,
            cache_age_seconds=None,
            fallback_reason=lookup_result.fallback_reason,
            cache_path="handler",
        )

    # Cross-run (wpid-scoped) cache lookup (SKY-8873). Consulted after an
    # in-run miss so the tight in-process dict stays the hot path. Returns
    # None in OSS; the cloud override hits Redis and is gated behind the
    # EXTRACT_INFORMATION_CACHE_REDIS PostHog flag. All errors are swallowed
    # by the backend so a Redis hiccup just falls through to the LLM call.
    # Skipped on retry — the subsequent dual-write overwrites any stale
    # Redis entry for this key with the fresh LLM result.
    cross_run_value: Any | None = None
    if cache_key is not None and not is_retry_step:
        try:
            cross_run_value = await app.AGENT_FUNCTION.lookup_cross_run_extraction_cache(wpid_for_cache, cache_key)
        except Exception:
            LOG.warning(
                "extract_information cross-run cache lookup raised",
                task_id=task.task_id,
                workflow_run_id=task.workflow_run_id,
                workflow_permanent_id=task.workflow_permanent_id,
                organization_id=task.organization_id,
                cache_key=cache_key,
                exc_info=True,
            )
            cross_run_value = None

    # Cross-run hit with a non-cacheable value type (e.g. a Redis payload
    # that decoded to a bool or number). Mirror the in-run warning so the
    # cross-run tier has the same diagnostic surface during rollout —
    # without it, a corrupt-but-decodable entry would silently fall
    # through to the LLM with no trail for post-hoc investigation.
    if cache_key is not None and cross_run_value is not None and not isinstance(cross_run_value, (dict, list, str)):
        LOG.warning(
            "extract_information cross-run cache hit returned non-cacheable value type; falling through to LLM",
            task_id=task.task_id,
            workflow_run_id=task.workflow_run_id,
            workflow_permanent_id=task.workflow_permanent_id,
            organization_id=task.organization_id,
            cache_key=cache_key,
            value_type=type(cross_run_value).__name__,
            cache_path="handler",
        )
        cross_run_value = None

    if cache_key is not None and cross_run_value is not None and isinstance(cross_run_value, (dict, list, str)):
        LOG.info(
            "extract_information cache hit — skipping LLM call (cross-run)",
            task_id=task.task_id,
            workflow_run_id=task.workflow_run_id,
            workflow_permanent_id=task.workflow_permanent_id,
            cache_key=cache_key,
            cache_hit=True,
            cache_scope=extraction_cache.SCOPE_WPID,
            cache_age_seconds=None,
            fallback_reason=None,
            cache_path="handler",
        )
        # Backfill the in-run cache so subsequent identical lookups in this
        # run short-circuit without crossing the Redis boundary.
        try:
            extraction_cache.store(task.workflow_run_id, cache_key, cross_run_value)
        except Exception:
            LOG.warning(
                "extract_information cross-run cache backfill to in-run failed",
                exc_info=True,
            )
        # Fire-and-forget shadow sampling on cross-run hits. Mirrors the
        # in-run path above; uses the -1.0 cached_age_seconds sentinel
        # because the Redis tier does not track per-key write time.
        if task.workflow_run_id is not None:
            _schedule_extraction_shadow_check_for_hit(
                task=task,
                workflow_run_id=task.workflow_run_id,
                cache_key=cache_key,
                cached_value=cross_run_value,
                cached_age_seconds=extraction_shadow.UNKNOWN_CACHE_AGE_SENTINEL,
                scraped_page=scraped_page,
                llm_key_override=llm_key_override,
                extract_information_prompt=extract_information_prompt,
            )
        return ScrapeResult(scraped_data=cross_run_value)

    # Cross-run miss log — INFO so the wpid-tier hit rate is computable
    # from logs alone once the read flag starts ramping. Earlier drafts kept
    # this at DEBUG specifically to avoid flooding INFO during the
    # post-merge 0%-read window; promoted to INFO in SKY-8992 before the
    # first read-flag flip so Datadog has both sides of the ratio without a
    # log-level backfill.
    if cache_key is not None and not is_retry_step and cross_run_value is None:
        LOG.info(
            "extract_information cache miss (cross-run)",
            task_id=task.task_id,
            workflow_run_id=task.workflow_run_id,
            workflow_permanent_id=task.workflow_permanent_id,
            cache_key=cache_key,
            cache_hit=False,
            cache_scope=extraction_cache.SCOPE_WPID,
            cache_age_seconds=None,
            # The wpid tier doesn't distinguish "flag disabled" from
            # "key not found" at the handler — both surface as ``None`` —
            # so label as ``cross_run_miss`` and let downstream metrics
            # split by ``workflow_permanent_id`` populated vs empty.
            fallback_reason="cross_run_miss",
            cache_path="handler",
        )

    # Use the appropriate LLM handler based on the feature flag
    llm_api_handler = LLMAPIHandlerFactory.get_override_llm_api_handler(
        llm_key_override, default=get_org_aware_primary_llm_api_handler(default=app.EXTRACTION_LLM_API_HANDLER)
    )
    json_response = await llm_api_handler(
        prompt=extract_information_prompt,
        step=step,
        screenshots=scraped_page.screenshots,
        prompt_name="extract-information",
        force_dict=False,
        system_prompt=task.workflow_system_prompt,
    )

    # Fill fields only after the model has produced the schema's root shape.
    # Otherwise `fill_missing_fields` replaces a populated but mismatched
    # response with an all-default stub (for example, `{"records": []}`),
    # which makes an extraction failure look like authoritative empty data.
    if task.extracted_information_schema and extraction_shape_matches(json_response, task.extracted_information_schema):
        json_response = validate_and_fill_extraction_result(
            extraction_result=json_response,
            schema=task.extracted_information_schema,
        )

    # Cache the post-validation result so cache hits return the same shape as
    # a fresh LLM call (schema-validated with missing fields filled). Accept
    # dict / list / str — the `extract-information` prompt uses
    # `force_dict=False`, so root `type: array` or scalar schemas are valid
    # return shapes (matches ``ScrapeResult.scraped_data``).
    # TEMPORARY INSTRUMENTATION (SKY-8992): the dual-write block below appears
    # to never populate Redis in production despite the code being deployed
    # and the cloud override verified. Log the gate inputs every call so we
    # can see which guard is closing the block. Revert after root-cause is
    # identified.
    LOG.info(
        "extract_information cache store gate",
        task_id=task.task_id,
        workflow_run_id=task.workflow_run_id,
        workflow_permanent_id=task.workflow_permanent_id,
        cache_key_present=cache_key is not None,
        json_response_type=type(json_response).__name__,
        json_response_is_cacheable=isinstance(json_response, (dict, list, str)),
        cache_path="handler",
    )
    if cache_key is not None and isinstance(json_response, (dict, list, str)):
        # TEMPORARY INSTRUMENTATION (SKY-8992): confirm the dual-write block is entered.
        LOG.info(
            "extract_information cache store block entered",
            task_id=task.task_id,
            workflow_run_id=task.workflow_run_id,
            workflow_permanent_id=task.workflow_permanent_id,
            cache_key=cache_key,
            cache_path="handler",
        )
        try:
            extraction_cache.store(task.workflow_run_id, cache_key, json_response)
        except Exception:
            LOG.warning("extract_information cache store failed; ignoring", exc_info=True)
        # Dual-write to the cross-run (Redis) tier. Ungated so the cache is
        # warm before the read flag rolls out. OSS returns immediately; cloud
        # writes to Redis with a long TTL and swallows backend errors.
        try:
            await app.AGENT_FUNCTION.store_cross_run_extraction_cache(wpid_for_cache, cache_key, json_response)
        except Exception:
            LOG.warning(
                "extract_information cross-run cache store raised; ignoring",
                task_id=task.task_id,
                workflow_run_id=task.workflow_run_id,
                workflow_permanent_id=task.workflow_permanent_id,
                organization_id=task.organization_id,
                cache_key=cache_key,
                exc_info=True,
            )

    return ScrapeResult(
        scraped_data=json_response,
    )


async def click_listbox_option(
    scraped_page: ScrapedPage,
    page: Page,
    action: actions.SelectOptionAction,
    listbox_element_id: str,
) -> bool:
    listbox_element = scraped_page.id_to_element_dict.get(listbox_element_id)
    if listbox_element is None:
        return False
    # this is a listbox element, get all the children
    if "children" not in listbox_element:
        return False

    LOG.info("starting bfs", listbox_element_id=listbox_element_id)
    bfs_queue = [child for child in listbox_element["children"]]
    while bfs_queue:
        child = bfs_queue.pop(0)
        LOG.info("popped child", element_id=child["id"])
        if "attributes" in child and "role" in child["attributes"] and child["attributes"]["role"] == "option":
            LOG.info("found option", element_id=child["id"])
            text = child["text"] if "text" in child else ""
            if text and (text == action.option.label or text == action.option.value):
                dom = DomUtil(scraped_page=scraped_page, page=page)
                try:
                    skyvern_element = await dom.get_skyvern_element_by_id(child["id"])
                    locator = skyvern_element.locator
                    await locator.click(timeout=1000)

                    return True
                except Exception:
                    LOG.error(
                        "Failed to click on the option",
                        action=action,
                        exc_info=True,
                    )
        if "children" in child:
            bfs_queue.extend(child["children"])
    return False


async def get_input_value(
    tag_name: str,
    locator: Locator,
    engine_selection: BrowserEngineSelection | None = None,
    read_timeout_ms: float | None = None,
) -> str | None:
    # input_value() rejects non-<input>/<textarea>/<select> nodes and inner_text() rejects
    # non-HTMLElement nodes; the live node can disagree with the scraped tag_name after a
    # re-render. Treat an incompatible read as "value unknown" so the caller's own
    # element-type classification runs instead of a raw driver exception escaping here. The
    # incompatible-node identity is matched against THIS run's selected engine; missing selection
    # keeps the stock Playwright identity (unchanged default). read_timeout_ms is opt-in: when unset
    # the read keeps Playwright's default wait; callers that must not stall pass an explicit bound.
    read_kwargs = {} if read_timeout_ms is None else {"timeout": read_timeout_ms}
    try:
        if tag_name in COMMON_INPUT_TAGS:
            return await locator.input_value(**read_kwargs)
        # for span, div, p or other tags:
        # we need to trim the unicode space for these tags
        return (await locator.inner_text(**read_kwargs)).replace("\xa0", " ").strip()
    except Exception as exc:
        if not _is_selected_engine_error(exc, engine_selection):
            raise
        if is_incompatible_text_input_error(exc):
            LOG.info("Skipping value read on an incompatible element", tag_name=tag_name, error=str(exc))
            return None
        raise


class AbstractActionForContextParse(BaseModel):
    reasoning: str | None
    element_id: str
    intention: str | None


async def _get_input_or_select_context(
    action: InputTextAction | SelectOptionAction | AbstractActionForContextParse,
    skyvern_element: SkyvernElement,
    element_tree_builder: ElementTreeBuilder,
    step: Step,
    ancestor_depth: int = 5,
    task: Task | None = None,
    engine_selection: BrowserEngineSelection | None = UNSET_SELECTION,
) -> InputOrSelectContext:
    # Early return optimization: if action already has input_or_select_context, use it
    if not isinstance(action, AbstractActionForContextParse) and action.input_or_select_context is not None:
        return action.input_or_select_context

    # Ancestor depth optimization: use ancestor element for deep DOM structures
    if engine_selection is UNSET_SELECTION:
        engine_selection = resolve_engine_selection_for_task(task, app.BROWSER_MANAGER)
    skyvern_frame = await SkyvernFrame.create_instance(skyvern_element.get_frame(), engine_selection=engine_selection)
    try:
        depth = await skyvern_frame.get_element_dom_depth(await skyvern_element.get_element_handler())
    except Exception:
        LOG.warning("Failed to get element depth, using the original element tree", exc_info=True)
        depth = 0

    if depth > ancestor_depth:
        # use ancestor to build the context
        path = "/".join([".."] * ancestor_depth)
        locator = skyvern_element.get_locator().locator(path)
        try:
            element_handle = await locator.element_handle(timeout=settings.BROWSER_ACTION_TIMEOUT_MS)
            if element_handle is not None:
                elements, element_tree = await skyvern_frame.build_tree_from_element(
                    starter=element_handle,
                    frame=skyvern_element.get_frame_id(),
                )
                clean_up_func = app.AGENT_FUNCTION.cleanup_element_tree_factory(
                    step=step, engine_selection=engine_selection
                )
                element_tree = await clean_up_func(skyvern_element.get_frame(), "", copy.deepcopy(element_tree))
                element_tree_trimmed = trim_element_tree(copy.deepcopy(element_tree))
                element_tree_builder = ScrapedPage(
                    elements=elements,
                    element_tree=element_tree,
                    element_tree_trimmed=element_tree_trimmed,
                    _browser_state=None,
                    _clean_up_func=None,
                    _scrape_exclude=None,
                )
        except Exception:
            LOG.warning("Failed to get sub element tree, using the original element tree", exc_info=True, path=path)

    slim_output = await get_slim_output_template_value("parse-input-or-select-context")
    prompt = load_prompt_with_elements(
        element_tree_builder=element_tree_builder,
        prompt_engine=prompt_engine,
        template_name="parse-input-or-select-context",
        action_reasoning=action.reasoning,
        element_id=action.element_id,
        slim_output=slim_output,
    )
    # Use centralized parse-select handler (set at init or via scripts)
    json_response = await get_org_aware_secondary_llm_api_handler(default=app.PARSE_SELECT_LLM_API_HANDLER)(
        prompt=prompt, step=step, prompt_name="parse-input-or-select-context"
    )

    # Handle edge case where LLM returns list instead of dict
    if isinstance(json_response, list):
        LOG.warning(
            "LLM returned list instead of dict for input/select context parsing",
            original_response_type=type(json_response).__name__,
            original_response_length=len(json_response) if json_response else 0,
            first_item_type=type(json_response[0]).__name__ if json_response else None,
            first_item_keys=list(json_response[0].keys())
            if json_response and isinstance(json_response[0], dict)
            else None,
        )
        json_response = json_response[0] if json_response else {}

    json_response["intention"] = action.intention
    input_or_select_context = InputOrSelectContext.model_validate(json_response)
    LOG.debug(
        "Parsed input/select context",
        context=input_or_select_context,
    )
    return input_or_select_context


def _match_user_defined_error_from_reasoning(task: Task, step: Step, reasoning: str) -> list[UserDefinedError]:
    # If the LLM returns no structured errors but its terminate reasoning
    # explicitly mentions a configured code or description, preserve that
    # machine-readable code for task/run/webhook error aggregation.
    normalized_reasoning = reasoning.lower()
    matched_errors: list[UserDefinedError] = []
    for error_code, error_description in (task.error_code_mapping or {}).items():
        # Only match structured codes directly. Generic single-word codes like
        # "timeout" can appear in unrelated reasoning and look falsely authoritative.
        code_matches = (
            "_" in error_code and re.search(rf"\b{re.escape(error_code.lower())}\b", normalized_reasoning) is not None
        )
        description_matches = isinstance(error_description, str) and error_description.lower() in normalized_reasoning
        if code_matches or description_matches:
            matched_errors.append(
                UserDefinedError(
                    error_code=error_code,
                    reasoning=reasoning,
                    confidence_float=1.0,
                )
            )
    if matched_errors:
        if len(matched_errors) > 1:
            LOG.warning(
                "Multiple user-defined error mappings matched terminate reasoning; using first match",
                task_id=task.task_id,
                step_id=step.step_id,
                matched_error_codes=[error.error_code for error in matched_errors],
                selected_error_code=matched_errors[0].error_code,
            )
        return [matched_errors[0]]
    return []


async def extract_user_defined_errors(
    task: Task, step: Step, scraped_page: ScrapedPage, reasoning: str | None = None
) -> list[UserDefinedError]:
    action_history = await get_action_history(task=task, current_step=step)
    scraped_page_refreshed = await scraped_page.refresh(draw_boxes=False)
    prompt = prompt_engine.load_prompt(
        "surface-user-defined-errors",
        navigation_goal=task.navigation_goal,
        navigation_payload_str=json.dumps(task.navigation_payload),
        elements=scraped_page_refreshed.build_element_tree(fmt=ElementTreeFormat.HTML),
        current_url=scraped_page_refreshed.url,
        action_history=json.dumps(action_history),
        error_code_mapping_str=json.dumps(task.error_code_mapping) if task.error_code_mapping else "{}",
        local_datetime=datetime.now(skyvern_context.ensure_context().tz_info).isoformat(),
        reasoning=reasoning,
    )
    json_response = await get_org_aware_primary_llm_api_handler(default=app.EXTRACTION_LLM_API_HANDLER)(
        prompt=prompt,
        screenshots=scraped_page_refreshed.screenshots,
        step=step,
        prompt_name="surface-user-defined-errors",
    )
    parsed = [UserDefinedError.model_validate(error) for error in json_response.get("errors", [])]
    kept, dropped = filter_to_user_defined_codes(parsed, task.error_code_mapping)
    if dropped:
        LOG.warning(
            "Dropped LLM-returned error codes not in user error_code_mapping",
            task_id=task.task_id,
            step_id=step.step_id,
            dropped_codes=dropped,
            allowed_codes=sorted((task.error_code_mapping or {}).keys()),
        )
    if not kept and reasoning:
        return _match_user_defined_error_from_reasoning(task=task, step=step, reasoning=reasoning)
    return kept
