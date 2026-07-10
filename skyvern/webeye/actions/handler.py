import asyncio
import copy
import json
import os
import re
import shutil
import time
import urllib.parse
import uuid
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, List, TypedDict

import structlog
from fuzzysearch import find_near_matches
from opentelemetry import trace as otel_trace
from playwright._impl._errors import Error as PlaywrightError
from playwright.async_api import Download, FileChooser, Frame, Locator, Page, Response, TimeoutError
from pydantic import BaseModel, field_validator

from skyvern.config import settings
from skyvern.constants import (
    AUTO_COMPLETION_POTENTIAL_VALUES_COUNT,
    BROWSER_DOWNLOAD_MAX_WAIT_TIME,
    BROWSER_DOWNLOAD_NO_SIGNAL_GRACE_TIME,
    BROWSER_DOWNLOAD_TIMEOUT,
    DROPDOWN_MENU_MAX_DISTANCE,
    SKYVERN_ID_ATTR,
)
from skyvern.core.script_generations.fuzzy_matcher import match_option_exact_or_stem
from skyvern.errors.errors import TOTPExpiredError, UserDefinedError, filter_to_user_defined_codes
from skyvern.exceptions import (
    CardNumberInputMismatch,
    EmptySelect,
    ErrEmptyTweakValue,
    ErrFoundSelectableElement,
    FailedToFetchSecret,
    FailToClick,
    FailToHover,
    FailToSelectByIndex,
    FailToSelectByLabel,
    FailToSelectByValue,
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
    OptionIndexOutOfBound,
    PhoneNumberInputMismatch,
    SkyvernException,
)
from skyvern.experimentation.wait_utils import get_or_create_wait_config, get_wait_time
from skyvern.forge import app
from skyvern.forge.prompts import prompt_engine
from skyvern.forge.sdk.api.files import (
    calculate_sha256_for_file,
    check_downloading_files_and_wait_for_download_to_complete,
    get_download_dir,
    list_files_in_directory,
    make_temp_directory,
    resolve_run_download_id,
)
from skyvern.forge.sdk.api.llm.api_handler_factory import LLMAPIHandlerFactory, LLMCallerManager
from skyvern.forge.sdk.api.llm.exceptions import LLMProviderError
from skyvern.forge.sdk.api.llm.schema_validator import validate_and_fill_extraction_result
from skyvern.forge.sdk.cache import extraction_cache, extraction_shadow
from skyvern.forge.sdk.copilot.block_goal_wrapping import unwrap_goal_fields
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import PendingFileChooserListener, ensure_context
from skyvern.forge.sdk.event.factory import EventStrategyFactory
from skyvern.forge.sdk.experimentation.llm_prompt_config import resolve_check_user_goal_handler
from skyvern.forge.sdk.experimentation.slim_llm_output import get_slim_output_template_value
from skyvern.forge.sdk.models import Step
from skyvern.forge.sdk.schemas.tasks import Task
from skyvern.forge.sdk.services.bitwarden import BitwardenConstants
from skyvern.forge.sdk.services.credentials import (
    AzureVaultConstants,
    OnePasswordConstants,
    generate_totp_code,
    parse_totp_config,
)
from skyvern.forge.sdk.settings_manager import SettingsManager
from skyvern.forge.sdk.trace import apply_context_attrs, traced
from skyvern.services import service_utils
from skyvern.services.action_service import get_action_history
from skyvern.utils.lean_html import apply_lean_to_tree
from skyvern.utils.prompt_engine import (
    CheckDateFormatResponse,
    CheckPhoneNumberFormatResponse,
    load_prompt_with_elements,
    load_prompt_with_elements_tracked,
)
from skyvern.utils.prompt_truncation import truncate_extraction_schema, truncate_previous_extracted_information
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
    ScrapeResult,
    SelectOption,
    SelectOptionAction,
    UploadFileAction,
    WebAction,
)
from skyvern.webeye.actions.responses import ActionAbort, ActionFailure, ActionResult, ActionSuccess
from skyvern.webeye.browser_factory import initialize_download_dir
from skyvern.webeye.cdp_download_interceptor import (
    DOWNLOAD_MIME_TYPES,
    MAX_FILE_SIZE_BYTES,
    download_filename_from_suffix,
    extract_filename,
    is_download_response,
    normalize_download_filename,
)
from skyvern.webeye.main_world_eval import evaluate_in_main_world
from skyvern.webeye.scraper.scraped_page import (
    CleanupElementTreeFunc,
    ElementTreeBuilder,
    ElementTreeFormat,
    ScrapedPage,
    json_to_html,
)
from skyvern.webeye.scraper.scraper import IncrementalScrapePage, hash_element, trim_element_tree
from skyvern.webeye.transient_page_observer import (
    TransientPageTextObserver,
    match_user_defined_errors_from_transient_text,
)
from skyvern.webeye.utils.dom import (
    COMMON_INPUT_TAGS,
    DomUtil,
    InteractiveElement,
    SkyvernElement,
    SkyvernOptionType,
    is_post_dispatch_click_timeout,
)
from skyvern.webeye.utils.page import SkyvernFrame

LOG = structlog.get_logger()

UPLOAD_PENDING_FOLLOWUP_MESSAGE = "Upload is not complete yet. Continue the upload flow."

FIX_TEL_INPUT_DIGIT_DROP_FLAG = "FIX_TEL_INPUT_DIGIT_DROP"
COLLAPSE_SELECT_FANOUT_FLAG = "COLLAPSE_SELECT_FANOUT"
COLLAPSE_CUSTOM_SELECT_FANOUT_FLAG = "COLLAPSE_CUSTOM_SELECT_FANOUT"
COLLAPSE_AUTOCOMPLETE_FANOUT_FLAG = "COLLAPSE_AUTOCOMPLETE_FANOUT"

DOWNLOAD_EVENT_ACTIVE_DIR_GRACE_SECONDS = 60
DOWNLOAD_DUPLICATE_STEM_SUFFIX_RE = re.compile(r"(?:\s+\(\d{1,3}\)|_\d{1,3})$")
SELECT_SHADOW_MATCH_APOSTROPHE_RE = re.compile(r"['`‘’]")
SELECT_SHADOW_MATCH_WORD_RE = re.compile(r"\w+")


def _select_shadow_match_enabled() -> bool:
    return settings.SKYVERN_SELECT_SHADOW_MATCH


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
) -> bool:
    actual_value = await get_input_value(skyvern_element.get_tag_name(), skyvern_element.get_locator())
    if _normalize_select_shadow_text(actual_value) == _normalize_select_shadow_text(matched_label):
        return True

    LOG.info(
        "Autocomplete read-back did not match deterministic option",
        expected_index=matched_index,
        expected_label=matched_label,
        actual_value=actual_value,
    )
    return False


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
) -> tuple[IncrementalScrapePage, list[dict], list[dict], str, list[str]]:
    await current_incremental_scraped.stop_listen_dom_increment()
    await skyvern_element.input_clear()

    incremental_scraped = IncrementalScrapePage(skyvern_frame=skyvern_frame)
    await incremental_scraped.start_listen_dom_increment(await skyvern_element.get_element_handler())
    await skyvern_element.press_fill(text)
    await skyvern_frame.safe_wait_for_animation_end(before_wait_sec=1, caller="autocomplete.fallback_refill")
    incremental_element = await incremental_scraped.get_incremental_element_tree(
        clean_and_remove_element_tree_factory(
            task=task,
            step=step,
            check_filter_funcs=[check_existed_but_not_option_element_in_dom_factory(dom)],
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
        return download_dir / download_filename_from_suffix(download_suffix, suffix, existing)
    return download_dir / f"{uuid.uuid4()}-{stem or 'download'}{suffix}"


async def _save_adopted_session_download(
    download: Download,
    page: Page,
    download_dir: Path,
    workflow_run_id: str | None = None,
) -> Path | None:
    """Land an adopted-session download's bytes into download_dir, returning the file path or None.

    Eager save_as is the only protection against the worker pod tearing the shared browser down before a
    deferred save_as runs.
    """
    download_target = _download_target_path(download_dir, download.suggested_filename)
    try:
        await download.save_as(download_target)
        if download_target.exists() and download_target.stat().st_size > 0:
            return download_target
        download_target.unlink(missing_ok=True)
        LOG.warning(
            "Adopted-session eager save_as produced an empty file; re-fetching download url",
            download_dir=str(download_dir),
            workflow_run_id=workflow_run_id,
        )
    except Exception:
        download_target.unlink(missing_ok=True)
        LOG.warning(
            "Adopted-session eager save_as failed; re-fetching download url",
            download_dir=str(download_dir),
            workflow_run_id=workflow_run_id,
            exc_info=True,
        )

    # Ordering: ``save_as`` above has already run and failed (empty or raised).
    # ``blob:`` URLs cannot be fetched via APIRequestContext (Playwright rejects
    # the scheme), so route them through an in-page fetch from a same-origin frame.
    if download.url.startswith("blob:"):
        blob_bytes = await SkyvernFrame.read_blob_url_bytes(
            page=page, blob_url=download.url, workflow_run_id=workflow_run_id
        )
        if blob_bytes is None:
            return None
        download_target.write_bytes(blob_bytes)
        return download_target

    try:
        response = await page.context.request.get(download.url)
        if response.status != 200:
            LOG.error(
                "Adopted-session download url re-fetch returned non-200 status",
                status=response.status,
                workflow_run_id=workflow_run_id,
            )
            return None
        # APIResponse.body() has no streaming variant, so a large download peaks at 2x its size in RSS.
        body = await response.body()
        if not body:
            LOG.error(
                "Adopted-session download url re-fetch returned an empty body",
                workflow_run_id=workflow_run_id,
            )
            return None
        download_target.write_bytes(body)
        return download_target
    except Exception:
        LOG.error(
            "Adopted-session download url re-fetch failed",
            download_dir=str(download_dir),
            workflow_run_id=workflow_run_id,
            exc_info=True,
        )
        return None


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

        return not await self.skyvern_frame.get_element_visible(await self.dropdown_menu.get_element_handler())


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
            current_element = SkyvernElement(locator=locator, frame=frame, static_element=element_dict)
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
        return not await skyvern_frame.get_element_visible(await skyvern_element.get_element_handler())

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
    task: Task, step: Step, check_filter_funcs: list[CheckFilterOutElementIDFunc]
) -> CleanupElementTreeFunc:
    async def helper_func(frame: Page | Frame, url: str, element_tree: list[dict]) -> list[dict]:
        element_tree = await app.AGENT_FUNCTION.cleanup_element_tree_factory(task=task, step=step)(
            frame, url, element_tree
        )
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

    json_response = await app.SECONDARY_LLM_API_HANDLER(
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
        action=action,
        element_id=skyvern_element.get_id(),
        recommended_phone_number=check_phone_number_format_response.recommended_phone_number,
    )
    return check_phone_number_format_response.recommended_phone_number


def _phone_digits(value: str | None) -> str:
    return re.sub(r"\D", "", value or "")


def _nanp_readback_national_digits(digits: str) -> str | None:
    if len(digits) == 10:
        return digits
    if len(digits) in {11, 12} and digits[:-10] == "1" * (len(digits) - 10):
        return digits[-10:]
    return None


def _phone_readback_digits_match(expected_digits: str, actual_digits: str) -> bool:
    if actual_digits == expected_digits:
        return True
    if len(expected_digits) == 10 and actual_digits == f"1{expected_digits}":
        return True

    expected_nanp_digits = _nanp_readback_national_digits(expected_digits)
    actual_nanp_digits = _nanp_readback_national_digits(actual_digits)
    return expected_nanp_digits is not None and expected_nanp_digits == actual_nanp_digits


def _is_plain_nanp_number(value: str | None) -> bool:
    # A 10-digit North American number with no '+'/country-code/extension markers. There is no
    # international handling, so a value carrying a '+' or letters (an extension) is excluded.
    text = value or ""
    if "+" in text or re.search(r"[A-Za-z]", text):
        return False
    return len(_phone_digits(text)) == 10


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


def _plan_tel_text(*, is_tel: bool, is_secret: bool, value: str, pattern: str | None) -> tuple[str, bool, bool]:
    # Decide how to fill a tel field. Returns (text_to_type, used_bare_nanp, run_format_check).
    # A separator-formatted value is long enough to fill()-split into a half-open "(ddd" that a
    # self-formatting field collapses, dropping a digit; bare national digits avoid that. Stripping is a
    # local transform, so it is applied to secret values too. The format-check LLM is reserved for
    # non-secret values that the bare-digit path does not handle.
    if is_tel and _is_plain_nanp_number(value) and _tel_pattern_allows_bare_digits(pattern, _phone_digits(value)):
        return _phone_digits(value), True, False
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


async def _is_collapse_select_fanout_enabled(task: Task) -> bool:
    organization_id = task.organization_id
    if not organization_id:
        return False
    experimentation_provider = getattr(app, "EXPERIMENTATION_PROVIDER", None)
    if not experimentation_provider:
        return False
    try:
        return bool(
            await experimentation_provider.is_feature_enabled_cached(
                COLLAPSE_SELECT_FANOUT_FLAG,
                organization_id,
                properties={"organization_id": organization_id},
            )
        )
    except Exception:
        LOG.warning(
            "Failed to evaluate collapse-select-fanout flag; defaulting to disabled",
            organization_id=organization_id,
            exc_info=True,
        )
        return False


async def _is_collapse_custom_select_fanout_enabled(task: Task) -> bool:
    organization_id = task.organization_id
    if not organization_id:
        return False
    experimentation_provider = getattr(app, "EXPERIMENTATION_PROVIDER", None)
    if not experimentation_provider:
        return False
    try:
        return bool(
            await experimentation_provider.is_feature_enabled_cached(
                COLLAPSE_CUSTOM_SELECT_FANOUT_FLAG,
                organization_id,
                properties={"organization_id": organization_id},
            )
        )
    except Exception:
        LOG.warning(
            "Failed to evaluate collapse-custom-select-fanout flag; defaulting to disabled",
            organization_id=organization_id,
            exc_info=True,
        )
        return False


async def _is_collapse_autocomplete_fanout_enabled(task: Task) -> bool:
    organization_id = task.organization_id
    if not organization_id:
        return False
    experimentation_provider = getattr(app, "EXPERIMENTATION_PROVIDER", None)
    if not experimentation_provider:
        return False
    try:
        return bool(
            await experimentation_provider.is_feature_enabled_cached(
                COLLAPSE_AUTOCOMPLETE_FANOUT_FLAG,
                organization_id,
                properties={"organization_id": organization_id},
            )
        )
    except Exception:
        LOG.warning(
            "Failed to evaluate collapse-autocomplete-fanout flag; defaulting to disabled",
            organization_id=organization_id,
            exc_info=True,
        )
        return False


async def verify_phone_input_digits(*, tag_name: str, locator: Locator, expected_value: str) -> None:
    # Compare normalized digits only — never the raw value, which may be a secret.
    actual_value = await get_input_value(tag_name=tag_name, locator=locator)
    expected_digits = _phone_digits(expected_value)
    actual_digits = _phone_digits(actual_value)
    if not _phone_readback_digits_match(expected_digits, actual_digits):
        raise PhoneNumberInputMismatch(
            expected_digit_count=len(expected_digits),
            actual_digit_count=len(actual_digits),
        )


async def _verify_tel_input_after_fill(*, skyvern_element: SkyvernElement, tag_name: str, expected_value: str) -> None:
    await verify_phone_input_digits(
        tag_name=tag_name,
        locator=skyvern_element.get_locator(),
        expected_value=expected_value,
    )


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
    *, skyvern_element: SkyvernElement, tag_name: str, text: str, expected_digits: str
) -> ActionFailure | None:
    # Type the card number, then read the rendered digits back. Character-by-character typing races an
    # auto-formatting field's caret restore and can scramble the value (SKY-11720); a single atomic
    # value-set formats once, without the race, so a mismatch is re-entered atomically before failing.
    await skyvern_element.input_sequentially(text=text)
    actual_value = await get_input_value(tag_name=tag_name, locator=skyvern_element.get_locator())
    if not _card_readback_is_mismatch(expected_digits, actual_value):
        return None

    await skyvern_element.input_clear()
    await skyvern_element.input_fill(text=text)
    actual_value = await get_input_value(tag_name=tag_name, locator=skyvern_element.get_locator())
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

    try:
        await locator.click(
            timeout=settings.BROWSER_ACTION_TIMEOUT_MS,
        )
    except Exception as e:
        LOG.info(
            "Failed to click before select action",
            exc_info=True,
            action=action,
            locator=locator,
        )
        action_result.append(ActionFailure(e))
        return action_result

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


async def _verify_normal_select_option(
    *,
    locator: Locator,
    matched_index: int,
    matched_label: str | None,
    matched_value: str | None,
) -> bool:
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
            """
        )
    except Exception:
        LOG.info(
            "Failed to read normal select option after deterministic selection",
            expected_index=matched_index,
            expected_label=matched_label,
            expected_value=matched_value,
            exc_info=True,
        )
        return False

    if not isinstance(selection, dict):
        LOG.info(
            "Normal select read-back returned unexpected payload",
            expected_index=matched_index,
            expected_label=matched_label,
            expected_value=matched_value,
            actual_selection=selection,
        )
        return False

    actual_index = selection.get("index")
    actual_value = selection.get("value")
    actual_label = selection.get("label") or actual_value
    if (
        actual_index == matched_index
        and actual_label == matched_label
        and (matched_value is None or actual_value == matched_value)
    ):
        return True

    LOG.info(
        "Normal select read-back did not match deterministic option",
        expected_index=matched_index,
        expected_label=matched_label,
        expected_value=matched_value,
        actual_index=actual_index,
        actual_label=actual_label,
        actual_value=actual_value,
    )
    return False


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

    json_response = await app.SECONDARY_LLM_API_HANDLER(prompt=prompt, step=step, prompt_name="check-date-format")

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

    Skipped when CDPDownloadInterceptor is active on the browser context
    (detected via ``_skyvern_cdp_download_active`` flag) because the CDP path
    already handles downloads at the Fetch domain level.

    Automatically attaches to new pages opened during the action window
    (e.g. target="_blank" links) so XHR responses on child tabs are captured.
    """

    def __init__(self, page: Page, download_dir: Path) -> None:
        self._page = page
        self._download_dir = download_dir
        self._saved: set[str] = set()
        self._extra_pages: list[Page] = []
        self._active = False
        self._in_flight = 0
        self._drained = asyncio.Event()
        self._drained.set()

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

    async def _on_response(self, response: Response) -> None:
        self._in_flight += 1
        self._drained.clear()
        try:
            try:
                if response.request.resource_type not in ("xhr", "fetch"):
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
        finally:
            self._in_flight -= 1
            if self._in_flight == 0:
                self._drained.set()

    async def drain(self) -> None:
        """Wait for in-flight XHR captures to finish. Best-effort: late events
        after drain returns are cleaned up by the caller's finally block."""
        await self._drained.wait()

    def _on_new_page(self, page: Page) -> None:
        if not self._active:
            return
        page.on("response", self._on_response)
        self._extra_pages.append(page)

    def enable(self) -> None:
        if getattr(self._page.context, "_skyvern_cdp_download_active", False):
            return
        self._page.on("response", self._on_response)
        self._page.context.on("page", self._on_new_page)
        self._active = True

    def disable(self) -> None:
        if not self._active:
            return
        self._page.remove_listener("response", self._on_response)
        self._page.context.remove_listener("page", self._on_new_page)
        for page in self._extra_pages:
            try:
                page.remove_listener("response", self._on_response)
            except Exception:
                pass
        self._extra_pages.clear()
        self._active = False


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
    ) -> list[ActionResult]:
        # task_id, step_id auto-attached by @traced from SkyvernContext
        _action_span = otel_trace.get_current_span()
        _action_span.set_attribute("action_type", str(action.action_type))
        _action_span.set_attribute("step_order", step.order)
        if getattr(action, "element_id", None):
            _action_span.set_attribute("element_id", action.element_id)
        browser_state = app.BROWSER_MANAGER.get_for_task(task.task_id, workflow_run_id=task.workflow_run_id)
        # TODO: maybe support all action types in the future(?)
        trigger_download_action = (
            isinstance(action, (SelectOptionAction, ClickAction, DownloadFileAction)) and action.download
        )
        # triggers_download splits the bimodal distribution: non-download actions
        # finish in ~1s while download actions can burn up to BROWSER_DOWNLOAD_MAX_WAIT_TIME
        # (120s) polling for the file. Explains the 36s p95 on this wrapper.
        _action_span.set_attribute("triggers_download", trigger_download_action)
        _tracer = otel_trace.get_tracer("skyvern")
        if not trigger_download_action:
            with _tracer.start_as_current_span("skyvern.agent.action.handle_inner") as _hi_span:
                apply_context_attrs(_hi_span)
                results = await ActionHandler._handle_action(
                    scraped_page=scraped_page,
                    task=task,
                    step=step,
                    page=page,
                    action=action,
                )
            persisted_action = await app.DATABASE.workflow_params.create_action(action=action)
            action.action_id = persisted_action.action_id
            return results

        context = skyvern_context.current()
        run_id = resolve_run_download_id(context, fallback_run_id=task.workflow_run_id or task.task_id)
        download_dir = Path(get_download_dir(run_id=run_id))
        download_event: asyncio.Future[Download] = asyncio.get_running_loop().create_future()

        def _capture_download_event(download: Download) -> None:
            if not download_event.done():
                download_event.set_result(download)

        async def _list_observed_download_files() -> list[str]:
            files = list_files_in_directory(download_dir)
            if task.browser_session_id:
                files_in_browser_session = await app.STORAGE.list_downloaded_files_in_browser_session(
                    organization_id=task.organization_id, browser_session_id=task.browser_session_id
                )
                files = files + files_in_browser_session
            return files

        async def _drain_and_move_staged_xhr(xhr_fallback_moved_paths: set[str]) -> bool:
            await xhr_capture.drain()
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

        list_files_before = await _list_observed_download_files()
        LOG.info(
            "Number of files in download directory before action",
            num_downloaded_files_before=len(list_files_before),
            download_dir=download_dir,
        )

        staging_dir = Path(make_temp_directory(prefix=f"{run_id}_xhr_staging_"))
        xhr_capture = ScopedXhrDownloadCapture(page, staging_dir)
        download_triggered = False
        xhr_fallback_moved_paths: set[str] = set()
        transient_text_observer = TransientPageTextObserver(
            page,
            task_id=task.task_id,
            step_id=step.step_id,
            workflow_run_id=task.workflow_run_id,
        )
        page.on("download", _capture_download_event)
        try:
            await transient_text_observer.start()
            xhr_capture.enable()
            with _tracer.start_as_current_span("skyvern.agent.action.handle_inner") as _hi_span:
                apply_context_attrs(_hi_span)
                results = await ActionHandler._handle_action(
                    scraped_page=scraped_page,
                    task=task,
                    step=step,
                    page=page,
                    action=action,
                )
            if not results:
                return results
            await transient_text_observer.start()
            _download_timeout = task.download_timeout or BROWSER_DOWNLOAD_MAX_WAIT_TIME
            _download_event_grace_seconds = min(DOWNLOAD_EVENT_ACTIVE_DIR_GRACE_SECONDS, _download_timeout)
            with _tracer.start_as_current_span("skyvern.agent.action.download_wait") as _dl_wait_span:
                apply_context_attrs(_dl_wait_span)
                _dl_wait_span.set_attribute("timeout_seconds", _download_timeout)
                _dl_wait_span.set_attribute("download_event_grace_seconds", _download_event_grace_seconds)
                no_signal_grace_seconds = min(_download_timeout, BROWSER_DOWNLOAD_NO_SIGNAL_GRACE_TIME)
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
                download_wait_started_at = time.monotonic()

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
                    async with asyncio.timeout(_download_timeout):
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
                                download_event_fallback_attempted = True
                                saved_path = await _save_adopted_session_download(
                                    captured_download,
                                    page,
                                    download_dir,
                                    workflow_run_id=task.workflow_run_id,
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
                                download_event_fallback_failed = True
                                LOG.warning(
                                    "Adopted-session download could not be saved or re-fetched; falling through to browser-session folder poll",
                                    download_dir=download_dir,
                                    workflow_run_id=task.workflow_run_id,
                                )
                                # Keep polling: the shared browser may still land the file in the session folder.
                                if await _drain_and_move_staged_xhr(xhr_fallback_moved_paths):
                                    download_triggered = True
                                    break

                            list_files_after = await _list_observed_download_files()

                            if len(list_files_after) > len(list_files_before):
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
                                download_target = _download_target_path(
                                    download_dir, captured_download.suggested_filename
                                )
                                try:
                                    await captured_download.save_as(download_target)
                                    if download_target.exists() and download_target.stat().st_size == 0:
                                        download_target.unlink(missing_ok=True)
                                        LOG.warning(
                                            "Captured download event fallback produced an empty file; marking download triggered without artifact",
                                            download_dir=download_dir,
                                            download_target=str(download_target),
                                            workflow_run_id=task.workflow_run_id,
                                        )
                                        list_files_after = await _list_observed_download_files()
                                        download_triggered = True
                                        break

                                    list_files_after = await _list_observed_download_files()
                                    LOG.info(
                                        "Copied captured download event to active run directory",
                                        download_dir=download_dir,
                                        download_target=str(download_target),
                                        workflow_run_id=task.workflow_run_id,
                                    )
                                    download_triggered = True
                                    download_event_fallback_used = True
                                    break
                                except Exception:
                                    LOG.warning(
                                        "Failed to copy captured download event to active run directory",
                                        download_dir=download_dir,
                                        workflow_run_id=task.workflow_run_id,
                                        exc_info=True,
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

                            if not download_signal_observed and elapsed_since_action >= no_signal_grace_seconds:
                                LOG.warning(
                                    "No download signal observed after action",
                                    workflow_run_id=task.workflow_run_id,
                                    no_signal_grace_seconds=no_signal_grace_seconds,
                                )
                                break
                            sleep_seconds: float = 1.0
                            if not download_signal_observed:
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
                    if download_wait_matched_errors:
                        _dl_wait_span.set_attribute(
                            "download_wait_user_error_codes",
                            ",".join(error.error_code for error in download_wait_matched_errors),
                        )

            if not download_triggered:
                if await _drain_and_move_staged_xhr(xhr_fallback_moved_paths):
                    download_triggered = True

            if not download_triggered:
                if action.errors:
                    results[-1] = ActionFailure(
                        Exception("; ".join(error.reasoning for error in action.errors)),
                        download_triggered=False,
                    )
                else:
                    results[-1].download_triggered = False
                action.download_triggered = False
                return results
            results[-1].download_triggered = True
            action.download_triggered = True

            await check_downloading_files_and_wait_for_download_to_complete(
                download_dir=download_dir,
                organization_id=task.organization_id,
                browser_session_id=task.browser_session_id,
                timeout=task.download_timeout or BROWSER_DOWNLOAD_TIMEOUT,
            )

            # Re-scan after waiting for .crdownload files to settle. The first
            # snapshot stops at the earliest download signal, while late browser
            # artifacts can still appear before task cleanup persists files.
            list_files_after = await _list_observed_download_files()
            new_file_paths = set(list_files_after) - set(list_files_before)
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
            deduplicated_paths = _deduplicate_new_downloaded_file_paths(
                new_file_paths,
                workflow_run_id=task.workflow_run_id,
                observed_file_paths=set(list_files_after),
            )
            downloaded_file_names = [os.path.basename(fp) for fp in deduplicated_paths]
            if downloaded_file_names:
                results[-1].downloaded_files = downloaded_file_names
                action.downloaded_files = downloaded_file_names
                LOG.info(
                    "Downloaded files captured",
                    downloaded_files=downloaded_file_names,
                    workflow_run_id=task.workflow_run_id,
                )

            return results
        finally:
            await transient_text_observer.stop()
            xhr_capture.disable()
            await xhr_capture.drain()
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
                if page_count_after_download > initial_page_count:
                    LOG.info(
                        "Download triggered, closing the extra page",
                    )

                    if page == pages_after_download[-1]:
                        LOG.warning("The extra page is the current page, closing it")
                    # close the extra page
                    await pages_after_download[-1].close()

                # After a print/download action the working page sometimes navigates to
                # about:blank (e.g. when the browser follows a download URL that yields no
                # renderable content). Detect this and navigate back to the original URL so
                # subsequent steps are not stuck on a blank page.
                blank_page_urls = {"about:blank", ":"}
                if page.url in blank_page_urls and page_url_before_download not in blank_page_urls:
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

            try:
                _remove_download_listener(page, _capture_download_event)
            except Exception:
                LOG.warning("Failed to remove one-shot download event listener", exc_info=True)

            persisted_action = await app.DATABASE.workflow_params.create_action(action=action)
            action.action_id = persisted_action.action_id

    @staticmethod
    async def _handle_action(
        scraped_page: ScrapedPage,
        task: Task,
        step: Step,
        page: Page,
        action: Action,
    ) -> list[ActionResult]:
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
        try:
            if action.action_type in ActionHandler._handled_action_types:
                invalid_web_action_check = check_for_invalid_web_action(action, page, scraped_page, task, step)
                if invalid_web_action_check:
                    actions_result.extend(invalid_web_action_check)
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
            LOG.exception("Imaginary secret value", action=action, exc_info=True)
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
                tool_result_content = "Tool executed successfully"
            else:
                tool_result_content = "Tool execution failed"
                # either actions_result is empty or the last action is a failure
                if not actions_result:
                    LOG.warning("Action failed to execute, setting status to failed", action=action)
                action.status = ActionStatus.failed

            if llm_caller and action.tool_call_id:
                tool_call_result = {
                    "type": "tool_result",
                    "tool_use_id": action.tool_call_id,
                    "content": tool_result_content,
                }
                llm_caller.add_tool_result(tool_call_result)

        return actions_result


def check_for_invalid_web_action(
    action: actions.Action,
    page: Page,
    scraped_page: ScrapedPage,
    task: Task,
    step: Step,
) -> list[ActionResult]:
    if isinstance(action, ClickAction) and action.x is not None and action.y is not None:
        return []

    if isinstance(action, InputTextAction) and not action.element_id:
        return []

    if isinstance(action, WebAction) and action.element_id not in scraped_page.id_to_element_dict:
        return [ActionFailure(MissingElement(element_id=action.element_id), stop_execution_on_failure=False)]

    return []


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

    # dynamically validate the attr, since it could change into enabled after the previous actions
    if await skyvern_element.is_disabled(dynamic=True):
        child = await _retarget_disabled_element_for_click(
            dom=dom,
            skyvern_element=skyvern_element,
            action=action,
        )
        if child is not None:
            skyvern_element = child
        else:
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
        return await handle_upload_file_action(upload_file_action, page, scraped_page, task, step)
    else:
        incremental_scraped: IncrementalScrapePage | None = None
        try:
            skyvern_frame = await SkyvernFrame.create_instance(skyvern_element.get_frame())
            incremental_scraped = IncrementalScrapePage(skyvern_frame=skyvern_frame)
            await incremental_scraped.start_listen_dom_increment(await skyvern_element.get_element_handler())

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
                if has_onclick_attr:
                    LOG.info(
                        "The element has onclick attribute, waiting for 1 second to load new elements", action=action
                    )
                    await skyvern_frame.safe_wait_for_animation_end(before_wait_sec=1, caller="click.onclick")

                if sequential_click_result := await handle_sequential_click_for_dropdown(
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

            except Exception:
                LOG.warning(
                    "Failed to do sequential logic for the click action, skipping",
                    exc_info=True,
                    element_id=skyvern_element.get_id(),
                )
                return results

        finally:
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
            task=task, step=step, check_filter_funcs=[check_existed_but_not_option_element_in_dom_factory(dom)]
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
        distinct_id_for_override, task.organization_id, app.CHECK_USER_GOAL_LLM_API_HANDLER
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

    dropdown_menu_element = await locate_dropdown_menu(
        current_anchor_element=anchor_element,
        incremental_scraped=incremental_scraped,
        step=step,
        task=task,
    )

    if dropdown_menu_element is None:
        return None

    dropdown_select_context = await _get_input_or_select_context(
        action=AbstractActionForContextParse(
            reasoning=action.reasoning, intention=action.intention, element_id=action.element_id
        ),
        skyvern_element=anchor_element,
        element_tree_builder=scraped_page,
        step=step,
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

            await asyncio.sleep(wait_seconds)

            LOG.debug(
                "6th digit: Finished waiting, TOTP is now valid",
                action_idx=action_index,
            )

    return None  # Success


def _normalize_dropdown_match_text(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]+", "", value).lower()


def _incremental_tree_contains_target_value(elements: list[dict], target_value: str) -> bool:
    """Return True when newly surfaced elements contain the requested value.

    Search-combobox results often render formatted labels like ``(CODE) 12345678``
    while the action text is just ``12345678``. Normalize punctuation and
    whitespace so post-input dropdown handling is gated on a concrete target
    match instead of any arbitrary search suggestion.
    """

    normalized_target = _normalize_dropdown_match_text(target_value)
    if not normalized_target:
        return False

    stack = list(elements)
    while stack:
        element = stack.pop()
        for key in (
            "text",
            "value",
            "label",
            "ariaLabel",
            "placeholder",
            "title",
            "beforePseudoText",
            "afterPseudoText",
        ):
            value = element.get(key)
            if isinstance(value, str) and normalized_target in _normalize_dropdown_match_text(value):
                return True
        attributes = element.get("attributes")
        if isinstance(attributes, dict):
            for attr_value in attributes.values():
                if isinstance(attr_value, str) and normalized_target in _normalize_dropdown_match_text(attr_value):
                    return True
        children = element.get("children", [])
        if isinstance(children, list):
            stack.extend(children)
    return False


@traced(name="skyvern.agent.action.input_text")
async def handle_input_text_action(
    action: actions.InputTextAction,
    page: Page,
    scraped_page: ScrapedPage,
    task: Task,
    step: Step,
) -> list[ActionResult]:
    if not action.element_id:
        # This is a CUA type action
        await EventStrategyFactory.type_text(page, None, action.text)
        return [ActionSuccess()]

    dom = DomUtil(scraped_page, page)
    skyvern_element = await dom.get_skyvern_element_by_id(action.element_id)
    skyvern_frame = await SkyvernFrame.create_instance(skyvern_element.get_frame())
    incremental_scraped = IncrementalScrapePage(skyvern_frame=skyvern_frame)
    timeout = settings.BROWSER_ACTION_TIMEOUT_MS

    current_text = await get_input_value(skyvern_element.get_tag_name(), skyvern_element.get_locator())
    if current_text == action.text:
        return [ActionSuccess()]

    # before filling text, we need to validate if the element can be filled if it's not one of COMMON_INPUT_TAGS
    tag_name = scraped_page.id_to_element_dict[action.element_id]["tagName"].lower()

    # Check if this is multi-field TOTP first - if so, skip secret resolution
    if action.totp_timing_info and action.totp_timing_info.get("is_totp_sequence"):
        # For multi-field TOTP, we'll set text directly in the TOTP logic below
        text: str = ""
    else:
        # For regular inputs, resolve secrets
        text_result = get_actual_value_of_parameter_if_secret_with_task(task, action.text)
        if text_result is None:
            return [ActionFailure(FailedToFetchSecret())]
        text = text_result

    is_totp_value = (
        text == BitwardenConstants.TOTP or text == OnePasswordConstants.TOTP or text == AzureVaultConstants.TOTP
    )
    is_secret_value = text != action.text

    # dynamically validate the attr, since it could change into enabled after the previous actions
    if await skyvern_element.is_disabled(dynamic=True):
        LOG.warning(
            "Try to input text on a disabled element",
            action_type=action.action_type,
            element_id=skyvern_element.get_id(),
        )
        return [ActionFailure(InteractWithDisabledElement(skyvern_element.get_id()))]

    select_action = SelectOptionAction(
        reasoning=action.reasoning,
        element_id=skyvern_element.get_id(),
        option=SelectOption(label=text),
        intention=action.intention,
        input_or_select_context=action.input_or_select_context,
    )
    if await skyvern_element.get_selectable():
        LOG.info(
            "Input element is selectable, doing select actions",
            element_id=skyvern_element.get_id(),
            action=action,
        )
        action.set_has_mini_agent()
        return await handle_select_option_action(select_action, page, scraped_page, task, step)

    incremental_element: list[dict] = []
    auto_complete_hacky_flag: bool = False

    input_or_select_context = await _get_input_or_select_context(
        action=action,
        element_tree_builder=scraped_page,
        skyvern_element=skyvern_element,
        step=step,
    )
    if not await skyvern_element.supports_text_input():
        if await skyvern_element.has_hidden_attr():
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

        try:
            await skyvern_element.press_key("ArrowDown")
        except TimeoutError:
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
                task=task, step=step, check_filter_funcs=[check_existed_but_not_option_element_in_dom_factory(dom)]
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
                )

                if select_result is not None:
                    if select_result.action_result and select_result.action_result.success:
                        try_to_quit_dropdown = False
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

    is_tel = await skyvern_element.get_attr("type") == "tel"
    candidate_card_digits = _card_number_digits(text)
    is_card_number_input = _is_probable_card_number(candidate_card_digits) and await _is_card_number_field(
        skyvern_element
    )
    is_plain_nanp_tel = False
    run_phone_format_check = False
    if is_tel and not is_card_number_input and await _is_tel_digit_fix_enabled(task):
        # SKY-11315 fix, behind FIX_TEL_INPUT_DIGIT_DROP. Flag-off keeps the original behavior below
        # byte-for-byte. Plain-NANP tel is typed as bare digits (skipping the format-check LLM) unless
        # the field's pattern requires a mask; secrets are eligible (local strip, no LLM).
        tel_pattern = await skyvern_element.get_attr("pattern")
        text, is_plain_nanp_tel, run_phone_format_check = _plan_tel_text(
            is_tel=True, is_secret=is_secret_value, value=text, pattern=tel_pattern
        )
    elif is_tel and not is_card_number_input and not is_secret_value:
        run_phone_format_check = True
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
        except Exception:
            LOG.warning(
                "Failed to check the phone number format, using the original text",
                action=action,
                exc_info=True,
            )

    # TODO: some elements are supported to use `locator.press_sequentially()` to fill in the data
    # we need find a better way to detect the attribute in the future
    class_name: str | None = await skyvern_element.get_attr("class")
    if class_name and "blinking-cursor" in class_name.lower():
        if is_totp_value:
            text = generate_totp_value_with_task(task=task, parameter=action.text)
        await skyvern_element.press_fill(text=text)
        return [ActionSuccess()]

    # `Locator.clear()` on a spin button could cause the cursor moving away, and never be back
    # run `Locator.clear()` when:
    # 1. the element is not a spin button
    #   1.1. the element has a value attribute
    #   1.2. the element is not editable and not common input tag
    if not await skyvern_element.is_spinbtn_input() and (
        current_text or (not await skyvern_element.is_editable() and tag_name not in COMMON_INPUT_TAGS)
    ):
        is_date_related = input_or_select_context is not None and input_or_select_context.is_date_related is True
        try:
            await skyvern_element.input_clear()
        except TimeoutError:
            LOG.info("None input tag clear timeout", action=action)
            return [
                ActionFailure(
                    InvalidElementForTextInput(
                        element_id=action.element_id, tag_name=tag_name, is_date_related=is_date_related
                    )
                )
            ]
        except Exception:
            LOG.warning("Failed to clear the input field", action=action, exc_info=True)
            return [
                ActionFailure(
                    InvalidElementForTextInput(
                        element_id=action.element_id, tag_name=tag_name, is_date_related=is_date_related
                    )
                )
            ]

    # wait for blocking element to show up
    await skyvern_frame.safe_wait_for_animation_end(caller="input_text.blocking_check")
    try:
        blocking_element, exist = await skyvern_element.find_blocking_element(
            dom=dom, incremental_page=incremental_scraped
        )
        if blocking_element and exist:
            LOG.warning(
                "Find a blocking element to the current element, going to input on the blocking element",
            )
            if await blocking_element.is_editable():
                skyvern_element = blocking_element
                tag_name = blocking_element.get_tag_name()
    except Exception:
        LOG.info(
            "Failed to find the blocking element, continue with the original element",
            exc_info=True,
        )

    if is_totp_value:
        LOG.info("Skipping the auto completion logic since it's a TOTP input")
        text = generate_totp_value_with_task(task=task, parameter=action.text)
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
                ):
                    auto_complete_hacky_flag = False
                    return [result]

        # Only the bare-digit NANP fill is read back to verify; other tel shapes are left unverified.
        verify_tel_input_after_fill = is_plain_nanp_tel

        # SKY-11720: an auto-formatting card-number field (a space every 4 digits) restores its caret
        # naively, racing character-by-character entry so the rendered value can silently differ from
        # the provided card while the block still completes. Deterministic card-number read-back runs
        # only when the value is Luhn-valid 13-19 digits and live field attrs identify a card-like
        # numeric field; mismatches are re-entered atomically before failing loudly.
        card_expected_digits = ""
        if is_card_number_input:
            card_expected_digits = candidate_card_digits

        await incremental_scraped.start_listen_dom_increment(await skyvern_element.get_element_handler())

        try:
            if card_expected_digits:
                card_failure = await _fill_card_number_with_readback(
                    skyvern_element=skyvern_element,
                    tag_name=tag_name,
                    text=text,
                    expected_digits=card_expected_digits,
                )
                if card_failure is not None:
                    return [card_failure]
            else:
                await skyvern_element.input_sequentially(text=text)
                if verify_tel_input_after_fill:
                    # Read the typed digits back; on mismatch, clear and retype once. A second mismatch
                    # fails the action here rather than letting it surface as a silent wrong fill.
                    try:
                        await _verify_tel_input_after_fill(
                            skyvern_element=skyvern_element,
                            tag_name=tag_name,
                            expected_value=text,
                        )
                    except PhoneNumberInputMismatch:
                        await skyvern_element.input_clear()
                        await skyvern_element.input_sequentially(text=text)
                        try:
                            await _verify_tel_input_after_fill(
                                skyvern_element=skyvern_element,
                                tag_name=tag_name,
                                expected_value=text,
                            )
                        except PhoneNumberInputMismatch as mismatch:
                            LOG.warning(
                                "Phone input read-back mismatch after retry",
                                action=action,
                                element_id=skyvern_element.get_id(),
                                expected_digit_count=mismatch.expected_digit_count,
                                actual_digit_count=mismatch.actual_digit_count,
                            )
                            return [ActionFailure(mismatch)]

            incremental_element = await incremental_scraped.get_incremental_element_tree(
                clean_and_remove_element_tree_factory(
                    task=task,
                    step=step,
                    check_filter_funcs=[check_existed_but_not_option_element_in_dom_factory(dom)],
                ),
            )
            if len(incremental_element) > 0:
                auto_complete_hacky_flag = True
                if (
                    input_or_select_context
                    and input_or_select_context.is_search_bar
                    and _incremental_tree_contains_target_value(incremental_element, text)
                ):
                    LOG.info(
                        "Detected target-matching dropdown after search-bar input; attempting custom selection",
                        element_id=skyvern_element.get_id(),
                        target_value=text,
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
                    )
                    if select_result and select_result.action_result and select_result.action_result.success:
                        auto_complete_hacky_flag = False
                        # A matching option was committed during this INPUT_TEXT. Stop the batch only when
                        # the next queued action would clobber it (a trailing Enter/Return); next step re-scrapes.
                        if action.stop_batch_after_dropdown_select:
                            select_result.action_result.skip_remaining_actions = True
                        return [select_result.action_result]
        except PlaywrightError as inc_error:
            # Handle Playwright-specific errors during incremental element processing
            # (e.g., TOTP form auto-submit, or search-dropdown selection triggering navigation)
            error_message = str(inc_error).lower()
            if (
                "execution context was destroyed" in error_message
                or "navigation" in error_message
                or "target closed" in error_message
            ):
                # These are expected during page navigation/auto-submit, silently continue
                LOG.debug(
                    "Playwright error during incremental element processing (likely page navigation)",
                    error_type=type(inc_error).__name__,
                    error_message=error_message,
                )
            else:
                LOG.warning(
                    "Unexpected Playwright error during incremental element processing",
                    error_type=type(inc_error).__name__,
                    error_message=str(inc_error),
                )
                raise inc_error
        except Exception as inc_error:
            # Handle any other unexpected errors during incremental element processing
            LOG.warning(
                "Unexpected error during incremental element processing",
                error_type=type(inc_error).__name__,
                error_message=str(inc_error),
            )
        finally:
            # Always stop listening
            await incremental_scraped.stop_listen_dom_increment()

        return [ActionSuccess()]
    except Exception as e:
        # Handle any other unexpected errors during text input

        LOG.exception("Failed to input the value or finish the auto completion")
        raise e
    finally:
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


async def _wait_for_upload_processing(page: Page) -> None:
    """Wait for page readiness signals after a file upload.

    Covers upload-processing UI (spinners, progress bars, DOM updates) beyond
    bare networkidle by reusing SkyvernFrame.wait_for_page_ready with
    upload-tuned timeouts that keep worst-case well below the old 10-15 s sleep.
    """
    try:
        # Settle delay: let the page react to the file-input change and mount
        # upload UI (spinner, progress bar, XHR) before polling for readiness.
        await asyncio.sleep(0.5)
        skyvern_frame = await SkyvernFrame.create_instance(page)
        await skyvern_frame.wait_for_page_ready(
            loading_indicator_timeout_ms=3000,
            network_idle_timeout_ms=3000,
            dom_stable_ms=300,
            dom_stability_timeout_ms=2000,
        )
    except (TimeoutError, asyncio.TimeoutError):
        LOG.info("Upload processing page-ready wait timed out, continuing")
    except PlaywrightError:
        LOG.warning("Upload processing page-ready wait interrupted by Playwright error, continuing", exc_info=True)


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
    if await skyvern_element.is_disabled(dynamic=True):
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
            await locator.set_input_files(
                file_path,
                timeout=settings.BROWSER_ACTION_TIMEOUT_MS,
            )

            await _wait_for_upload_processing(page)

            return [ActionSuccess()]
        else:
            return [ActionFailure(Exception(f"Failed to download file from {action.file_url}"))]
    else:
        LOG.info("Taking UploadFileAction. Found non file input tag", action=action)
        # treat it as a click action
        action.is_upload_file_tag = False
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
            try:
                await page.goto(action.download_url, timeout=settings.BROWSER_LOADING_TIMEOUT_MS)
            except Exception as e:
                error = str(e)
                # some cases use this method to download a file. but it will be redirected away soon
                # and agent will run into ABORTED error.
                # some cases playwright will raise error like "Page.goto: Download is starting"
                if "net::ERR_ABORTED" not in error and "Page.goto: Download is starting" not in error:
                    raise e

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
) -> list[ActionResult]:
    dom = DomUtil(scraped_page, page)
    skyvern_element = await dom.get_skyvern_element_by_id(action.element_id)

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
        click_action = ClickAction(element_id=action.element_id)
        action.set_has_mini_agent()
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
    if await skyvern_element.is_disabled(dynamic=True):
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

        try:
            await skyvern_element.scroll_into_view()
            blocking_element, exist = await skyvern_element.find_blocking_element(dom=dom)
        except Exception:
            LOG.warning(
                "Failed to find the blocking element, continue to select on the original <select>",
                exc_info=True,
            )
            return await normal_select(
                action=action, skyvern_element=skyvern_element, builder=dom.scraped_page, task=task, step=step
            )

        if not exist:
            return await normal_select(
                action=action, skyvern_element=skyvern_element, builder=dom.scraped_page, task=task, step=step
            )

        if blocking_element is None:
            LOG.info("Try to scroll the element into view, then detecting the blocking element")
            try:
                await skyvern_element.scroll_into_view()
                blocking_element, exist = await skyvern_element.find_blocking_element(dom=dom)
            except Exception:
                LOG.warning(
                    "Failed to find the blocking element when scrolling into view, fallback to normal select",
                    action=action,
                    exc_info=True,
                )
                return await normal_select(
                    action=action, skyvern_element=skyvern_element, builder=dom.scraped_page, task=task, step=step
                )

        if not exist or blocking_element is None:
            return await normal_select(
                action=action, skyvern_element=skyvern_element, builder=dom.scraped_page, task=task, step=step
            )
        LOG.info(
            "<select> is blocked by another element, going to select on the blocking element",
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
        return await handle_checkbox_action(check_action, page, scraped_page, task, step)

    if await skyvern_element.is_radio():
        LOG.info(
            "SelectOptionAction is on <input> radio",
            action=action,
        )
        click_action = ClickAction(element_id=action.element_id)
        action.set_has_mini_agent()
        return await chain_click(task, scraped_page, page, click_action, skyvern_element)

    # FIXME: maybe there's a case where <input type="button"> could trigger dropdown menu?
    if await skyvern_element.is_btn_input():
        LOG.info(
            "SelectOptionAction is on <input> button",
            action=action,
        )
        click_action = ClickAction(element_id=action.element_id)
        action.set_has_mini_agent()
        return await chain_click(task, scraped_page, page, click_action, skyvern_element)

    LOG.info(
        "Trigger custom select",
        action=action,
        element_id=skyvern_element.get_id(),
    )

    timeout = settings.BROWSER_ACTION_TIMEOUT_MS
    skyvern_frame = await SkyvernFrame.create_instance(skyvern_element.get_frame())
    incremental_scraped = IncrementalScrapePage(skyvern_frame=skyvern_frame)
    is_open = False
    suggested_value: str | None = None
    results: list[ActionResult] = []

    try:
        await incremental_scraped.start_listen_dom_increment(await skyvern_element.get_element_handler())
        await skyvern_element.scroll_into_view()

        await skyvern_element.click(page=page, dom=dom, timeout=timeout)
        # wait for options to load
        await skyvern_frame.safe_wait_for_animation_end(before_wait_sec=0.5, caller="select_option.open")

        incremental_element = await incremental_scraped.get_incremental_element_tree(
            clean_and_remove_element_tree_factory(
                task=task, step=step, check_filter_funcs=[check_existed_but_not_option_element_in_dom_factory(dom)]
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
                    task=task, step=step, check_filter_funcs=[check_existed_but_not_option_element_in_dom_factory(dom)]
                ),
            )

        input_or_select_context = await _get_input_or_select_context(
            action=action, element_tree_builder=scraped_page, step=step, skyvern_element=skyvern_element
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
                )
            )
            return results

        is_open = True
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
    await page.goto(action.url, timeout=settings.BROWSER_LOADING_TIMEOUT_MS)
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
    new_page = await browser_state.new_page()
    try:
        await browser_state.navigate_to_url(page=new_page, url=action.url)
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


def generate_totp_value(workflow_run_id: str, parameter: str) -> str:
    workflow_run_context = app.WORKFLOW_CONTEXT_MANAGER.get_workflow_run_context(workflow_run_id)
    totp_secret_key = workflow_run_context.totp_secret_value_key(parameter)
    totp_secret = workflow_run_context.get_original_secret_value_or_none(totp_secret_key)
    if not totp_secret:
        LOG.warning("No TOTP secret found, returning the parameter value as is", parameter=parameter)
        return parameter
    return generate_totp_code(totp_secret)


def generate_totp_value_with_task(task: Task, parameter: str) -> str:
    if task.workflow_run_id is None:
        return parameter
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
    # Tracks the return value so the finally block can inspect click success.
    action_results: list[ActionResult] = []
    try:
        if not await skyvern_element.navigate_to_a_href(page=page):
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
        if is_post_dispatch_click_timeout(e):
            LOG.info(
                "Chain click: physical click dispatched; navigation-wait timed out — skipping fallback",
                action=action,
                locator=locator,
            )
            action_results = [ActionSuccess()]
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
                    navigated_href = await skyvern_element.try_navigate_via_href(page=page)
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

            try:
                await skyvern_element.coordinate_click(page=page, click_count=click_count)
                action_results.append(ActionSuccess())
                return action_results
            except Exception as e:
                action_results.append(
                    ActionFailure(FailToClick(action.element_id, anchor="coordinate_click", msg=str(e)))
                )

            LOG.info(
                "Chain click: coordinate click failed, going to use javascript click instead of playwright click",
                action=action,
                element=str(skyvern_element),
                locator=locator,
            )
            try:
                await skyvern_element.click_in_javascript()
                action_results.append(ActionSuccess())
                return action_results
            except Exception as e:
                action_results.append(ActionFailure(FailToClick(action.element_id, anchor="self_js", msg=str(e))))
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
                await _wait_for_upload_processing(page)
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
            await _wait_for_upload_processing(page)
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
) -> AutoCompletionResult:
    preserved_elements = preserved_elements or []
    clear_input = True
    result = AutoCompletionResult()

    current_frame = skyvern_element.get_frame()
    skyvern_frame = await SkyvernFrame.create_instance(current_frame)
    incremental_scraped = IncrementalScrapePage(skyvern_frame=skyvern_frame)
    await incremental_scraped.start_listen_dom_increment(await skyvern_element.get_element_handler())

    try:
        await skyvern_element.press_fill(text)
        # wait for new elemnts to load
        await skyvern_frame.safe_wait_for_animation_end(before_wait_sec=1, caller="autocomplete.fill")
        incremental_element = await incremental_scraped.get_incremental_element_tree(
            clean_and_remove_element_tree_factory(
                task=task, step=step, check_filter_funcs=[check_existed_but_not_option_element_in_dom_factory(dom)]
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
            confirmed_preserved_list = await app.AGENT_FUNCTION.cleanup_element_tree_factory(task=task, step=step)(
                skyvern_frame.get_frame(), skyvern_frame.get_frame().url, copy.deepcopy(confirmed_preserved_list)
            )
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
        json_response = await app.AUTO_COMPLETION_LLM_API_HANDLER(
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
        )
        await selected_element.scroll_into_view()
        await selected_element.click(page=page)
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
            await skyvern_element.input_clear()


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
        )
        if isinstance(result.action_result, ActionSuccess):
            return ActionSuccess()

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
        json_respone = await app.SECONDARY_LLM_API_HANDLER(
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
            )
            if isinstance(result.action_result, ActionSuccess):
                return ActionSuccess()

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
            json_respone = await app.SECONDARY_LLM_API_HANDLER(
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
    skyvern_frame = await SkyvernFrame.create_instance(current_frame)
    incremental_scraped = IncrementalScrapePage(skyvern_frame=skyvern_frame)
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
            except TimeoutError:
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
        json_response = await app.AUTO_COMPLETION_LLM_API_HANDLER(
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

    check_filter_funcs: list[CheckFilterOutElementIDFunc] = [check_existed_but_not_option_element_in_dom_factory(dom)]
    for i in range(max_depth):
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
        )
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
        llm_api_handler = LLMAPIHandlerFactory.get_override_llm_api_handler(task.llm_key, default=app.LLM_API_HANDLER)
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
    """
    queue: deque[dict] = deque(elements)
    seen: set[str] = set()
    out: list[str] = []

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
        if role == "option" or tag in ("li", "option"):
            _record(str(node.get("text") or "").strip())
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
    queue: deque[tuple[dict, bool, bool]] = deque((element, False, False) for element in elements)
    candidates: list[_CustomSelectCandidate] = []
    seen: set[tuple[str | None, str | None, str | None]] = set()
    covered_choice_input_ids: set[str] = set()

    while queue:
        node, in_choice_surface, in_multiselectable = queue.popleft()
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

        if is_choice_input and element_id in covered_choice_input_ids:
            pass
        elif element_id and label and (is_option_node or is_choice_input or is_label_choice or is_clickable_choice):
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
            queue.append((child, child_in_choice_surface, child_in_multiselectable))

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
    const normalize = (value) => (value ?? "").replace(/\s+/g, " ").trim().toLowerCase();
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
    const normalize = (value) => (value ?? "").replace(/\s+/g, " ").trim().toLowerCase();
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
    // Nearest matching ancestor wins; never scope to the whole form or a bare
    // parent container — sibling fields showing the target label must not
    // pre-confirm this one. With no recognized field wrapper, fall back to the
    // anchor itself (a miss routes to the LLM path, never a cross-field match).
    const scopeCandidates = scopeSelectors
        .map((selector) => anchor.closest?.(selector))
        .filter(Boolean);
    const scopeRoot = (
        scopeCandidates.reduce((closest, el) => (!closest || closest.contains(el) ? el : closest), null)
        || anchor
    );
    const tokenSelectors = [
        ...(allowAriaSelectedOptionTokens ? ["[role='option'][aria-selected='true']"] : []),
        "[data-automation-id='selectedItem']",
        ".pill",
        ".chip",
        "[class*='token']"
    ].join(",");
    for (const token of scopeRoot.querySelectorAll(tokenSelectors)) {
        if (matchesExpected(token.textContent) || matchesExpected(token.getAttribute("aria-label"))) return true;
    }
    for (const hidden of scopeRoot.querySelectorAll("input[type='hidden']")) {
        if (matchesExpected(hidden.value)) return true;
    }
    const activeId = anchor.getAttribute?.("aria-activedescendant");
    if (allowAriaSelectedOptionTokens && activeId) {
        const active = scopeRoot.querySelector(`#${CSS.escape(activeId)}`);
        if (active && active.getAttribute("aria-selected") === "true") {
            if (matchesExpected(active.textContent) || matchesExpected(active.getAttribute("aria-label"))) {
                return true;
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
    for (const el of triggerCandidates) {
        if (!el || seen.has(el) || !scopeRoot.contains(el)) continue;
        seen.add(el);
        if (reflectedValues(el).some(matchesExpected)) return true;
    }
    // A combobox <input> may still hold the user-typed filter text; raw value equality alone is not
    // a committed signal. Only trust it when the dropdown has closed (aria-expanded=false).
    if (anchorIsComboboxInput) {
        const valueMatches = matchesExpected(anchor.value) || matchesExpected(anchor.getAttribute("value"));
        if (valueMatches) {
            const expanded = anchor.getAttribute("aria-expanded")
                || anchor.closest?.("[aria-expanded]")?.getAttribute("aria-expanded");
            if (expanded === "false") return true;
        }
        return false;
    }
    for (const el of seen) {
        if (reflectedValues(el).some((value) => normalize(value))) return false;
    }
    return false;
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


async def _custom_select_scope_confirms_committed(
    *,
    readback_scope_element: SkyvernElement | None,
    anchor_is_combobox_input: bool,
    matched_element_id: str,
    matched_label: str | None,
    expected_label: str,
    allow_aria_selected_option_tokens: bool,
) -> bool:
    if readback_scope_element is None:
        return False

    try:
        committed = await _evaluate_element_scoped(
            readback_scope_element,
            _CUSTOM_SELECT_COMMITTED_STATE_JS,
            {
                "expectedLabel": expected_label,
                "anchorIsComboboxInput": anchor_is_combobox_input,
                "allowAriaSelectedOptionTokens": allow_aria_selected_option_tokens,
            },
        )
    except Exception:
        LOG.info(
            "Failed to read custom-select committed label",
            matched_element_id=matched_element_id,
            matched_label=matched_label,
            exc_info=True,
        )
        return False

    return committed is True


async def _verify_custom_select_option(
    *,
    matched_element: SkyvernElement,
    readback_scope_element: SkyvernElement | None,
    anchor_is_combobox_input: bool,
    matched_element_id: str,
    matched_label: str | None,
) -> bool:
    expected_label = _normalize_select_shadow_text(matched_label)
    if not expected_label:
        return False

    if _custom_select_matched_state_confirms(await _read_custom_select_matched_state(matched_element), expected_label):
        return True

    return await _custom_select_scope_confirms_committed(
        readback_scope_element=readback_scope_element,
        anchor_is_combobox_input=anchor_is_combobox_input,
        matched_element_id=matched_element_id,
        matched_label=matched_label,
        expected_label=expected_label,
        allow_aria_selected_option_tokens=True,
    )


_CUSTOM_SELECT_VERIFY_SETTLE_RETRY_DELAYS_SECONDS = (0.15, 0.15)


async def _verify_custom_select_option_with_settle(
    *,
    matched_element: SkyvernElement,
    readback_scope_element: SkyvernElement | None,
    anchor_is_combobox_input: bool,
    matched_element_id: str,
    matched_label: str | None,
) -> bool:
    """Retry the read-back a couple of times before giving up.

    Some frameworks commit ``aria-selected``/trigger-text reflection on the next render tick
    rather than synchronously on click, so an immediate read-back can read stale state. Retries
    only fire on the failure path; a confirmed read-back returns immediately with no added delay.
    """
    for delay_seconds in (0.0, *_CUSTOM_SELECT_VERIFY_SETTLE_RETRY_DELAYS_SECONDS):
        if delay_seconds:
            await asyncio.sleep(delay_seconds)
        if await _verify_custom_select_option(
            matched_element=matched_element,
            readback_scope_element=readback_scope_element,
            anchor_is_combobox_input=anchor_is_combobox_input,
            matched_element_id=matched_element_id,
            matched_label=matched_label,
        ):
            return True
    return False


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


async def _select_deterministic_custom_option(
    *,
    target_value: str | None,
    get_option_candidates: Callable[[], list[_CustomSelectCandidate]],
    field_context: Any,
    page: Page,
    get_skyvern_element: Callable[[str], Awaitable[SkyvernElement]],
    get_readback_scope_element: Callable[[], Awaitable[SkyvernElement | None]] | None = None,
    task: Task,
) -> tuple[ActionResult, str | None] | None:
    if not target_value:
        return None
    if isinstance(field_context, dict) and field_context.get("is_date_related") is True:
        return None
    if not await _is_collapse_custom_select_fanout_enabled(task):
        return None

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
    if resolution.fallback_to_llm or resolution.matched_index is None:
        return None
    if resolution.matched_index >= len(option_candidates):
        return None

    matched_candidate = option_candidates[resolution.matched_index]
    element_id = matched_candidate.get("element_id")
    matched_label = resolution.matched_label
    # Computed by the tree walk, which sees wrapper and multiselect-container toggle semantics
    # that are not necessarily present on the resolved element itself.
    matched_option_is_choice_input = matched_candidate["is_choice_input"]
    if not element_id:
        return None

    readback_scope_element: SkyvernElement | None = None
    anchor_is_combobox_input = False
    try:
        selected_element = await get_skyvern_element(element_id)
        if await selected_element.get_attr("role") == "listbox":
            return None

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

        expected_label = _normalize_select_shadow_text(matched_label)
        if expected_label:
            if _custom_select_matched_state_confirms_pre_click(matched_state, expected_label):
                return ActionSuccess(), matched_label
            if await _custom_select_scope_confirms_committed(
                readback_scope_element=readback_scope_element,
                anchor_is_combobox_input=anchor_is_combobox_input,
                matched_element_id=element_id,
                matched_label=matched_label,
                expected_label=expected_label,
                allow_aria_selected_option_tokens=False,
            ):
                return ActionSuccess(), matched_label

        await selected_element.scroll_into_view()
        await selected_element.click(page=page)
        verified = await _verify_custom_select_option_with_settle(
            matched_element=selected_element,
            readback_scope_element=readback_scope_element,
            anchor_is_combobox_input=anchor_is_combobox_input,
            matched_element_id=element_id,
            matched_label=matched_label,
        )
        if verified:
            return ActionSuccess(), matched_label
    except Exception:
        LOG.info(
            "Deterministic custom-select failed; falling back to LLM path",
            target_value=target_value,
            matched_element_id=element_id,
            matched_label=matched_label,
            exc_info=True,
        )
        return None

    if anchor_is_combobox_input:
        # Text-input comboboxes can be safely reset, so an unconfirmed read-back routes to the LLM
        # mini-agent (which clears/reopens the field) instead of hard-failing the whole action.
        await _reset_custom_select_combobox_input(readback_scope_element, page)
        LOG.info(
            "Deterministic custom-select read-back inconclusive on combobox input; routing to LLM fallback",
            target_value=target_value,
            matched_element_id=element_id,
            matched_label=matched_label,
        )
        return None

    if not matched_option_is_choice_input:
        # A non-toggle option (e.g. a button/div-anchored single-select listbox) can be safely
        # replayed by the LLM mini-agent. Toggle-shaped options hard-fail below instead.
        LOG.info(
            "Deterministic custom-select read-back inconclusive on non-choice-input option; routing to LLM fallback",
            target_value=target_value,
            matched_element_id=element_id,
            matched_label=matched_label,
        )
        return None

    LOG.info(
        "Deterministic custom-select read-back failed after click; returning failure to avoid replaying over mutated widget",
        target_value=target_value,
        matched_element_id=element_id,
        matched_label=matched_label,
    )
    action_failure = ActionFailure(
        NoElementMatchedForTargetOption(
            target=target_value,
            reason="Deterministic custom-select click could not be verified by matched element read-back",
        )
    )
    action_failure.skip_remaining_actions = True
    return action_failure, matched_label


async def _reset_custom_select_combobox_input(element: SkyvernElement | None, page: Page) -> None:
    if element is None:
        return
    try:
        locator = element.get_locator()
        await locator.fill("")
        await element.click(page=page)
    except Exception:
        LOG.info(
            "Failed to reset custom-select combobox input before LLM fallback",
            exc_info=True,
        )


def _no_match_exception_for_dropdown(
    *,
    reasoning: str | None,
    target_value: str | None,
    observed_options: list[str],
    transient_fallback_element_id: str | None,
) -> Exception:
    """Return the right no-match exception: transient when the dropdown opened with zero options, permanent otherwise."""
    if not observed_options and transient_fallback_element_id is not None:
        return NoIncrementalElementFoundForCustomSelection(element_id=transient_fallback_element_id)
    return NoAvailableOptionFoundForCustomSelection(
        reason=reasoning,
        target_value=target_value or None,
        observed_options=observed_options,
    )


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


@traced(name="skyvern.agent.dropdown.select_emerging")
async def select_from_emerging_elements(
    current_element_id: str,
    options: CustomSelectPromptOptions,
    page: Page,
    scraped_page: ScrapedPage,
    step: Step,
    task: Task,
    scraped_page_after_open: ScrapedPage | None = None,
    new_interactable_element_ids: list[str] | None = None,
) -> ActionResult:
    """
    This is the function to select an element from the new showing elements.
    Currently mainly used for the dropdown menu selection.
    """

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
        raise NoIncrementalElementFoundForCustomSelection(element_id=current_element_id)

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

    deterministic_result = await _select_deterministic_custom_option(
        target_value=options.target_value,
        get_option_candidates=lambda: _custom_select_candidates_from_elements(shadow_candidate_elements),
        field_context=options.model_dump(),
        page=page,
        get_skyvern_element=dom_after_open.get_skyvern_element_by_id,
        get_readback_scope_element=get_readback_scope_element,
        task=task,
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

    llm_api_handler = LLMAPIHandlerFactory.get_override_llm_api_handler(task.llm_key, default=app.LLM_API_HANDLER)
    json_response = await llm_api_handler(prompt=prompt, step=step, prompt_name="custom-select")
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
        LOG.info(
            "No clickable option found, but found input element to search",
            element_id=element_id,
        )
        input_element = await dom_after_open.get_skyvern_element_by_id(element_id)
        await input_element.scroll_into_view()
        current_text = await get_input_value(input_element.get_tag_name(), input_element.get_locator())
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

    await selected_element.scroll_into_view()
    await selected_element.click(page=page)
    return ActionSuccess()


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
        clean_and_remove_element_tree_factory(task=task, step=step, check_filter_funcs=check_filter_funcs),
    )
    incremental_scraped.set_element_tree_trimmed(trimmed_element_tree)
    html = incremental_scraped.build_element_tree(html_need_skyvern_attrs=True)

    deterministic_result = await _select_deterministic_custom_option(
        target_value=target_value,
        get_option_candidates=lambda: _custom_select_candidates_from_elements(trimmed_element_tree),
        field_context=context.model_dump(),
        page=page,
        get_skyvern_element=lambda element_id: SkyvernElement.create_from_incremental(incremental_scraped, element_id),
        get_readback_scope_element=_readback_scope_element_provider(skyvern_element),
        task=task,
    )
    if deterministic_result is not None:
        action_result, matched_label = deterministic_result
        single_select_result.reasoning = "Deterministic exact/stem custom-select match"
        single_select_result.value = matched_label or target_value
        single_select_result.action_type = ActionType.CLICK
        single_select_result.action_result = action_result
        if isinstance(action_result, ActionSuccess):
            single_select_result.dropdown_menu = None
        return single_select_result

    skyvern_context = ensure_context()
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
    json_response = await app.CUSTOM_SELECT_AGENT_LLM_API_HANDLER(prompt=prompt, step=step, prompt_name="custom-select")
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
        )
    single_select_result.action_type = ActionType(raw_action_type)
    action_type = single_select_result.action_type

    if not force_select and target_value:
        if not json_response.get("relevant", False):
            LOG.info(
                "The selected option is not relevant to the target value",
                element_id=element_id,
            )
            return single_select_result

    if value is not None and action_type == ActionType.INPUT_TEXT:
        LOG.info(
            "No clickable option found, but found input element to search",
            element_id=element_id,
        )
        try:
            actual_value = get_actual_value_of_parameter_if_secret_with_task(task, value)
            input_element = await SkyvernElement.create_from_incremental(incremental_scraped, element_id)
            await input_element.scroll_into_view()
            current_text = await get_input_value(input_element.get_tag_name(), input_element.get_locator())
            if current_text == actual_value:
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
                return single_select_result

            await input_element.input_clear()
            await input_element.input_sequentially(actual_value)
            single_select_result.action_result = ActionSuccess()
            return single_select_result
        except Exception as e:
            single_select_result.action_result = ActionFailure(exception=e)
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
                action=action, skyvern_element=selected_element, task=task, step=step, builder=incremental_scraped
            )
            assert len(results) > 0
            single_select_result.action_result = results[0]
            return single_select_result

        if await selected_element.get_attr("role") == "listbox":
            single_select_result.action_result = ActionFailure(
                exception=InteractWithDropdownContainer(element_id=element_id)
            )
            return single_select_result

        await selected_element.scroll_into_view()
        await selected_element.click(page=page, timeout=timeout)
        single_select_result.action_result = ActionSuccess()
        return single_select_result
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
        return single_select_result

    try:
        LOG.info(
            "Find an alternative option with the same value. Try to select the option.",
            value=value,
        )
        await EventStrategyFactory.move_to_element(page, locator)
        await locator.click(timeout=timeout)
        single_select_result.action_result = ActionSuccess()
        return single_select_result
    except Exception as e:
        single_select_result.action_result = ActionFailure(exception=e)
        return single_select_result


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
            task=task, step=step, check_filter_funcs=[check_existed_but_not_option_element_in_dom_factory(dom)]
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
                task=task, step=step, check_filter_funcs=[check_existed_but_not_option_element_in_dom_factory(dom)]
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

        if not await skyvern_frame.get_element_visible(await head_element.get_element_handler()):
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
        screenshot = await head_element.get_locator().screenshot(timeout=settings.BROWSER_SCREENSHOT_TIMEOUT_MS)
        await skyvern_frame.scroll_to_x_y(x, y)

        # TODO: better to send untrimmed HTML without skyvern attributes in the future
        dropdown_confirm_prompt = prompt_engine.load_prompt(
            "opened-dropdown-confirm",
        )
        LOG.debug(
            "Confirm if it's an opened dropdown menu",
            element=element_dict,
        )
        json_response = await app.SECONDARY_LLM_API_HANDLER(
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
        await dropdown_menu_element_handle.scroll_into_view_if_needed(timeout=timeout)

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
        step=step,
        skyvern_element=skyvern_element,
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
            if _normal_select_successful(deterministic_result) and await _verify_normal_select_option(
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

    json_response = await app.NORMAL_SELECT_AGENT_LLM_API_HANDLER(prompt=prompt, step=step, prompt_name="normal-select")
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

    try:
        await locator.click(
            timeout=settings.BROWSER_ACTION_TIMEOUT_MS,
        )
    except Exception as e:
        LOG.info(
            "Failed to click before select action",
            exc_info=True,
            action=action,
            locator=locator,
        )
        action_result.append(ActionFailure(e))
        return action_result

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
        llm_key_override, default=app.EXTRACTION_LLM_API_HANDLER
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
        if shadow_schema:
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
    )

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
        llm_key_override, default=app.EXTRACTION_LLM_API_HANDLER
    )
    json_response = await llm_api_handler(
        prompt=extract_information_prompt,
        step=step,
        screenshots=scraped_page.screenshots,
        prompt_name="extract-information",
        force_dict=False,
        system_prompt=task.workflow_system_prompt,
    )

    # Validate and fill missing fields based on schema
    if task.extracted_information_schema:
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


async def get_input_value(tag_name: str, locator: Locator) -> str | None:
    if tag_name in COMMON_INPUT_TAGS:
        return await locator.input_value()
    # for span, div, p or other tags:
    # we need to trim the unicode space for these tags
    return (await locator.inner_text()).replace("\xa0", " ").strip()


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
) -> InputOrSelectContext:
    # Early return optimization: if action already has input_or_select_context, use it
    if not isinstance(action, AbstractActionForContextParse) and action.input_or_select_context is not None:
        return action.input_or_select_context

    # Ancestor depth optimization: use ancestor element for deep DOM structures
    skyvern_frame = await SkyvernFrame.create_instance(skyvern_element.get_frame())
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
                clean_up_func = app.AGENT_FUNCTION.cleanup_element_tree_factory(step=step)
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
    json_response = await app.PARSE_SELECT_LLM_API_HANDLER(
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
    json_response = await app.EXTRACTION_LLM_API_HANDLER(
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
