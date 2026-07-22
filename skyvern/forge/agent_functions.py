from __future__ import annotations

import asyncio
import copy
import hashlib
import os
import time
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Literal, TypedDict

import aiohttp
import httpx
import structlog
from cachetools import TTLCache
from google.oauth2.credentials import Credentials
from playwright.async_api import Frame, Page

from skyvern.config import settings
from skyvern.constants import CUSTOMER_STORAGE_UPLOAD_MAX_BYTES, SKYVERN_ID_ATTR
from skyvern.core.script_generations.fuzzy_matcher import match_option_exact_or_stem_with_tier
from skyvern.exceptions import (
    AzureConfigurationError,
    DisabledBlockExecutionError,
    StepUnableToExecuteError,
    TaskAlreadyTimeout,
    UploadFileMaxSizeExceeded,
)
from skyvern.forge import app
from skyvern.forge.async_operations import AsyncOperation
from skyvern.forge.prompts import prompt_engine
from skyvern.forge.sdk.api.aws import AsyncAWSClient
from skyvern.forge.sdk.api.azure import AzureClientFactory
from skyvern.forge.sdk.api.llm.exceptions import LLMProviderError
from skyvern.forge.sdk.cache.base import CACHE_EXPIRE_TIME
from skyvern.forge.sdk.copilot.config import CopilotConfig, block_authoring_policy_from_code_only_mode
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.db.agent_db import AgentDB
from skyvern.forge.sdk.models import Step, StepStatus
from skyvern.forge.sdk.schemas.organizations import Organization
from skyvern.forge.sdk.schemas.tasks import Task, TaskStatus
from skyvern.forge.sdk.services import (
    google_drive_service,
    google_gmail_service,
    google_oauth_service,
    google_sheets_service,
    microsoft_oauth_service,
    sftp_service,
)
from skyvern.forge.sdk.services.credentials import AuthenticatorTotpParseResult
from skyvern.forge.sdk.trace import traced
from skyvern.forge.sdk.workflow.models.block import BlockTypeVar
from skyvern.schemas.workflows import BlockResult, FileStorageType, FileUploadDestination
from skyvern.services.otp_gmail import GmailOTPVerificationContext
from skyvern.webeye.actions.actions import Action
from skyvern.webeye.browser_state import BrowserState
from skyvern.webeye.scraper.scraped_page import ELEMENT_NODE_ATTRIBUTES, CleanupElementTreeFunc, json_to_html
from skyvern.webeye.utils.dom import SkyvernElement
from skyvern.webeye.utils.page import SkyvernFrame

if TYPE_CHECKING:
    from playwright.async_api import BrowserContext

    from skyvern.forge.sdk.db.enums import WorkflowRunTriggerType
    from skyvern.forge.sdk.workflow.context_manager import WorkflowRunContext
    from skyvern.forge.sdk.workflow.models.code_block_recorder import RecordingPage
    from skyvern.forge.sdk.workflow.models.workflow import Workflow, WorkflowRun, WorkflowRunStatus
    from skyvern.services.otp_service import OTPValue

LOG = structlog.get_logger()

# Playwright's always-on ffmpeg VP8 encoder scales CPU with pixel count; 720p is the
# legibility / CPU tradeoff point that the BROWSER_RECORDING_720P flag opts a run into.
RECORDING_VIDEO_SIZE_720P: dict[str, int] = {"width": 1280, "height": 720}
GMAIL_OTP_CREDENTIAL_REFRESH_INTERVAL_SECONDS = 30
GMAIL_OTP_MAX_RESULTS = 5
GMAIL_OTP_SEARCH_INTERVAL_SECONDS = 30

_LLM_CALL_TIMEOUT_SECONDS = 30  # 30s
USELESS_SHAPE_ATTRIBUTE = [SKYVERN_ID_ATTR, "id", "aria-describedby"]
SVG_SHAPE_CONVERTION_ATTEMPTS = 3
CSS_SHAPE_CONVERTION_ATTEMPTS = 1
INVALID_SHAPE = "N/A"
DISABLE_SVG_CONVERT_CACHE_RESILIENCE_FLAG = "DISABLE_SVG_CONVERT_CACHE_RESILIENCE"
SVG_LOCAL_CACHE_MAX_ITEMS = 4096
SVG_LOCAL_NEGATIVE_CACHE_EXPIRE_TIME = timedelta(hours=1)
SVGLocalCacheValue = tuple[str, float | None]

# TTLCache has one global TTL, so each value also carries an optional shorter
# expiry timestamp for negative cache entries.
_SVG_LOCAL_SHAPE_CACHE: TTLCache[str, SVGLocalCacheValue] = TTLCache(
    maxsize=SVG_LOCAL_CACHE_MAX_ITEMS,
    ttl=CACHE_EXPIRE_TIME.total_seconds(),
)
# Best-effort single-flight cache: eviction under extreme distinct-key pressure can
# allow duplicate conversions, but does not affect conversion correctness.
_SVG_CONVERSION_LOCKS: TTLCache[str, asyncio.Lock] = TTLCache(
    maxsize=SVG_LOCAL_CACHE_MAX_ITEMS,
    ttl=CACHE_EXPIRE_TIME.total_seconds(),
)


@dataclass
class TOTPVerificationResponse:
    """Normalized response shape for the TOTP verification seam.

    Decouples the seam contract from any specific HTTP client so the OSS
    direct path (aiohttp) and the cloud proxy path (NATEgressProxyClient)
    can both produce a response the helper consumes the same way.
    """

    status_code: int
    body: str
    headers: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class CopilotSiteOriginAssociation:
    requested_name: str
    entity_id: str
    entity_label: str
    official_site_url: str
    origin: str
    source: str
    provider_relation_type: str
    provider_relation_text: str


class CopilotCandidateNetworkHop(TypedDict):
    url: str
    resource_type: str
    resolved_public_ips: list[str]
    connected_peer_ip: str
    enforcement_version: str


@dataclass(frozen=True)
class CopilotEntrypointCandidate:
    url: str
    source_rank: int
    association: CopilotSiteOriginAssociation


@dataclass(frozen=True)
class FieldOptionResolution:
    matched_index: int | None
    matched_label: str | None
    matched_value: str | None
    confidence: float
    fallback_to_llm: bool
    matched_tier: Literal["exact", "stem"] | None = None


@dataclass
class CodeBlockEngineFailure:
    error_code: str | None
    safe_message: str | None
    failure_reason: str | None
    exception_class: str | None
    failing_line: int | None
    healability_hint: bool | None


@dataclass
class CodeBlockEngineResult:
    block_result: BlockResult | None
    failure: CodeBlockEngineFailure | None


def _remove_rect(element: dict) -> None:
    if "rect" in element:
        del element["rect"]


def _should_css_shape_convert(element: dict) -> bool:
    if "id" not in element:
        return False

    tag_name = element.get("tagName")
    if tag_name not in ["a", "span", "i", "button"]:
        return False

    # should be without children
    if len(element.get("children", [])) > 0:
        return False

    # should be no text
    if element.get("text"):
        return False

    # if <span> and <i>  we try to convert the shape
    if tag_name in ["span", "i", "button"]:
        return True

    # if <a>, it should be no text, no href/target attribute
    if tag_name == "a":
        attributes = element.get("attributes", {})
        if "href" in attributes:
            return False

        if "target" in attributes:
            return False
        return True

    return False


def _get_svg_cache_key(hash: str) -> str:
    return f"skyvern:svg:{hash}"


def _get_shape_cache_key(hash: str) -> str:
    return f"skyvern:shape:{hash}"


def _get_svg_conversion_lock(svg_key: str) -> asyncio.Lock:
    lock = _SVG_CONVERSION_LOCKS.get(svg_key)
    if lock is None:
        lock = asyncio.Lock()
        _SVG_CONVERSION_LOCKS[svg_key] = lock
    return lock


@asynccontextmanager
async def _svg_conversion_lock_scope(svg_key: str, *, use_lock: bool) -> AsyncIterator[None]:
    if not use_lock:
        yield
        return

    async with _get_svg_conversion_lock(svg_key):
        yield


async def _is_svg_convert_cache_resilience_disabled() -> bool:
    context = skyvern_context.current()
    if context is None:
        return False

    distinct_id = context.run_id or context.workflow_run_id or context.task_id
    organization_id = context.organization_id
    if not distinct_id or not organization_id:
        return False

    experimentation_provider = getattr(app, "EXPERIMENTATION_PROVIDER", None)
    if not experimentation_provider:
        return False

    try:
        flag_enabled = await experimentation_provider.is_feature_enabled_cached(
            DISABLE_SVG_CONVERT_CACHE_RESILIENCE_FLAG,
            distinct_id,
            properties={"organization_id": organization_id},
        )
    except Exception:
        LOG.warning(
            "Failed to evaluate SVG convert cache resilience flag; defaulting to enabled",
            exc_info=True,
            distinct_id=distinct_id,
            organization_id=organization_id,
        )
        return False

    return bool(flag_enabled)


def _get_local_cache_ttl_seconds(svg_shape: str, ex: int | timedelta | None) -> float | None:
    if ex is None:
        ttl_seconds = None
    else:
        ttl_seconds = ex.total_seconds() if isinstance(ex, timedelta) else float(ex)

    if svg_shape == INVALID_SHAPE:
        negative_ttl_seconds = SVG_LOCAL_NEGATIVE_CACHE_EXPIRE_TIME.total_seconds()
        if ttl_seconds is None:
            return negative_ttl_seconds
        return min(ttl_seconds, negative_ttl_seconds)
    return ttl_seconds


def _get_local_cache_expires_at(svg_shape: str, ex: int | timedelta | None) -> float | None:
    ttl_seconds = _get_local_cache_ttl_seconds(svg_shape, ex)
    if ttl_seconds is None:
        return None
    return time.monotonic() + max(ttl_seconds, 0.0)


def _get_svg_shape_from_local_cache(svg_key: str) -> str | None:
    cached_value = _SVG_LOCAL_SHAPE_CACHE.get(svg_key)
    if cached_value is None:
        return None

    svg_shape, expires_at = cached_value
    if expires_at is not None and expires_at <= time.monotonic():
        _SVG_LOCAL_SHAPE_CACHE.pop(svg_key, None)
        return None
    return svg_shape


def _cache_svg_shape_locally(
    svg_key: str,
    svg_shape: str | None,
    *,
    ex: int | timedelta | None = CACHE_EXPIRE_TIME,
) -> None:
    if svg_shape:
        _SVG_LOCAL_SHAPE_CACHE[svg_key] = (svg_shape, _get_local_cache_expires_at(svg_shape, ex))


async def _get_cached_svg_shape(svg_key: str, *, use_local_cache: bool = True) -> str | None:
    if use_local_cache:
        local_shape = _get_svg_shape_from_local_cache(svg_key)
        if local_shape:
            return local_shape

    try:
        cached_svg_shape = await app.CACHE.get(svg_key)
    except Exception:
        LOG.warning(
            "Failed to loaded SVG cache",
            exc_info=True,
            key=svg_key,
        )
        if use_local_cache:
            return _get_svg_shape_from_local_cache(svg_key)
        return None

    svg_shape = cached_svg_shape if isinstance(cached_svg_shape, str) else None
    if use_local_cache:
        _cache_svg_shape_locally(svg_key, svg_shape)
    return svg_shape


async def _set_svg_cache(
    svg_key: str,
    svg_shape: str,
    *,
    ex: int | timedelta | None = CACHE_EXPIRE_TIME,
    cache_locally: bool = True,
) -> None:
    if cache_locally:
        _cache_svg_shape_locally(svg_key, svg_shape, ex=ex)
    try:
        await app.CACHE.set(svg_key, svg_shape, ex=ex)
    except Exception:
        LOG.warning(
            "Failed to store SVG cache",
            exc_info=True,
            key=svg_key,
        )


async def _set_css_shape_cache(
    shape_key: str,
    css_shape: str,
    *,
    ex: int | timedelta | None = CACHE_EXPIRE_TIME,
) -> None:
    try:
        await app.CACHE.set(shape_key, css_shape, ex=ex)
    except Exception:
        LOG.warning(
            "Failed to store CSS shape cache",
            exc_info=True,
            key=shape_key,
        )


def _remove_skyvern_attributes(element: dict) -> dict:
    """
    To get the original HTML element without skyvern attributes
    """
    element_copied = copy.deepcopy(element)
    for attr in ELEMENT_NODE_ATTRIBUTES:
        if element_copied.get(attr):
            del element_copied[attr]

    if "attributes" in element_copied:
        attributes: dict = copy.deepcopy(element_copied.get("attributes", {}))
        for key in attributes.keys():
            if key in USELESS_SHAPE_ATTRIBUTE:
                del element_copied["attributes"][key]

    children: list[dict] | None = element_copied.get("children", None)
    if children is None:
        return element_copied

    trimmed_children = []
    for child in children:
        trimmed_children.append(_remove_skyvern_attributes(child))

    element_copied["children"] = trimmed_children
    return element_copied


def _add_to_dropped_css_svg_element_map(hashed_key: str | None) -> None:
    context = skyvern_context.ensure_context()
    if hashed_key:
        context.dropped_css_svg_element_map[hashed_key] = True


def _is_element_already_dropped(hashed_key: str) -> bool:
    context = skyvern_context.ensure_context()
    return hashed_key in context.dropped_css_svg_element_map


def _mark_element_as_dropped(element: dict, *, hashed_key: str | None) -> None:
    _add_to_dropped_css_svg_element_map(hashed_key)
    if "children" in element:
        del element["children"]
    element["isDropped"] = True


async def _check_svg_eligibility(
    skyvern_frame: SkyvernFrame,
    element: dict,
    task: Task | None = None,
    step: Step | None = None,
    always_drop: bool = False,
) -> bool:
    """Check if an SVG element is eligible for conversion."""
    if element.get("tagName") != "svg":
        return False

    if element.get("isDropped", False):
        return False

    if always_drop:
        _mark_element_as_dropped(element, hashed_key=None)
        return False

    element_id = element.get("id", "")

    try:
        locater = skyvern_frame.get_frame().locator(f'[{SKYVERN_ID_ATTR}="{element_id}"]')
        if await locater.count() == 0:
            _mark_element_as_dropped(element, hashed_key=None)
            return False

        if not await locater.is_visible(timeout=settings.BROWSER_ACTION_TIMEOUT_MS):
            _mark_element_as_dropped(element, hashed_key=None)
            return False

        skyvern_element = SkyvernElement(locator=locater, frame=skyvern_frame.get_frame(), static_element=element)

        _, blocked = await skyvern_frame.get_blocking_element_id(
            await skyvern_element.get_element_handler(timeout=1000)
        )
        if not skyvern_element.is_interactable() and blocked:
            _mark_element_as_dropped(element, hashed_key=None)
            return False
    except Exception:
        LOG.warning(
            "Failed to get the blocking element for the svg, going to continue parsing the svg",
            exc_info=True,
        )

    return True


async def _convert_svg_to_string(
    element: dict,
    task: Task | None = None,
    step: Step | None = None,
) -> None:
    """Convert an SVG element to a string description. Assumes element has already passed eligibility checks."""
    element_id = element.get("id", "")

    svg_element = _remove_skyvern_attributes(element)
    svg_html = json_to_html(svg_element)
    hash_object = hashlib.sha256()
    hash_object.update(svg_html.encode("utf-8"))
    svg_hash = hash_object.hexdigest()
    svg_key = _get_svg_cache_key(svg_hash)

    svg_shape: str | None = None
    refresh_svg_cache = False
    use_cache_resilience = not await _is_svg_convert_cache_resilience_disabled()
    async with _svg_conversion_lock_scope(svg_key, use_lock=use_cache_resilience):
        svg_shape = await _get_cached_svg_shape(svg_key, use_local_cache=use_cache_resilience)

        if svg_shape:
            LOG.debug("SVG loaded from cache", element_id=element_id, key=svg_key, shape=svg_shape)
            refresh_svg_cache = True
        else:
            if _is_element_already_dropped(svg_key):
                LOG.debug("SVG is already dropped, going to abort conversion", element_id=element_id, key=svg_key)
                _mark_element_as_dropped(element, hashed_key=svg_key)
                return

            if len(svg_html) > settings.SVG_MAX_LENGTH:
                # TODO: implement a fallback solution for "too large" case, maybe convert by screenshot
                LOG.warning(
                    "SVG element is too large to convert, going to drop the svg element.",
                    element_id=element_id,
                    length=len(svg_html),
                    key=svg_key,
                )
                _mark_element_as_dropped(element, hashed_key=svg_key)
                return

            LOG.debug("call LLM to convert SVG to string shape", element_id=element_id)
            svg_convert_prompt = prompt_engine.load_prompt("svg-convert", svg_element=svg_html)

            for retry in range(SVG_SHAPE_CONVERTION_ATTEMPTS):
                try:
                    async with asyncio.timeout(_LLM_CALL_TIMEOUT_SECONDS):
                        if app.SVG_CSS_CONVERTER_LLM_API_HANDLER is None:
                            raise Exception("To enable svg shape conversion, please set the Secondary LLM key")
                        json_response = await app.SVG_CSS_CONVERTER_LLM_API_HANDLER(
                            prompt=svg_convert_prompt, step=step, prompt_name="svg-convert"
                        )
                    svg_shape = json_response.get("shape", "")
                    recognized = json_response.get("recognized", False)
                    if not svg_shape or not recognized:
                        raise Exception("Empty or unrecognized SVG shape replied by secondary llm")
                    LOG.info("SVG converted by LLM", element_id=element_id, key=svg_key, shape=svg_shape)
                    await _set_svg_cache(svg_key, svg_shape, cache_locally=use_cache_resilience)
                    break
                except LLMProviderError:
                    LOG.info(
                        "Failed to convert SVG to string due to llm error. Will retry if haven't met the max try attempt.",
                        exc_info=True,
                        element_id=element_id,
                        key=svg_key,
                        retry=retry,
                    )
                    if retry == SVG_SHAPE_CONVERTION_ATTEMPTS - 1:
                        # set the invalid css shape to cache to avoid retry in the near future
                        await _set_svg_cache(
                            svg_key,
                            INVALID_SHAPE,
                            ex=timedelta(hours=1),
                            cache_locally=use_cache_resilience,
                        )
                    else:
                        await asyncio.sleep(0.5 * (2**retry))
                except asyncio.TimeoutError:
                    LOG.warning(
                        "Timeout to call LLM to parse SVG. Going to drop the svg element directly.",
                        element_id=element_id,
                        key=svg_key,
                    )
                    _mark_element_as_dropped(element, hashed_key=svg_key)
                    return
                except Exception:
                    LOG.info(
                        "Failed to convert SVG to string shape by secondary llm. Will retry if haven't met the max try attempt.",
                        exc_info=True,
                        element_id=element_id,
                        retry=retry,
                    )
                    if retry == SVG_SHAPE_CONVERTION_ATTEMPTS - 1:
                        # set the invalid css shape to cache to avoid retry in the near future
                        await _set_svg_cache(
                            svg_key,
                            INVALID_SHAPE,
                            ex=timedelta(weeks=1),
                            cache_locally=use_cache_resilience,
                        )
                    else:
                        await asyncio.sleep(0.5 * (2**retry))
            else:
                LOG.warning(
                    "Reaching the max try to convert svg element, going to drop the svg element.",
                    element_id=element_id,
                    key=svg_key,
                    length=len(svg_html),
                )
                _mark_element_as_dropped(element, hashed_key=svg_key)
                return

    element["attributes"] = dict()
    if svg_shape != INVALID_SHAPE:
        # refresh the cache expiration
        if refresh_svg_cache:
            await _set_svg_cache(svg_key, svg_shape, cache_locally=use_cache_resilience)
        element["attributes"]["alt"] = svg_shape
    if "children" in element:
        del element["children"]
    return


async def _convert_css_shape_to_string(
    skyvern_frame: SkyvernFrame,
    element: dict,
    task: Task | None = None,
    step: Step | None = None,
) -> None:
    element_id: str = element.get("id", "")

    shape_element = _remove_skyvern_attributes(element)
    svg_html = json_to_html(shape_element)
    hash_object = hashlib.sha256()
    hash_object.update(svg_html.encode("utf-8"))
    shape_hash = hash_object.hexdigest()
    shape_key = _get_shape_cache_key(shape_hash)

    css_shape: str | None = None
    try:
        css_shape = await app.CACHE.get(shape_key)
    except Exception:
        LOG.warning(
            "Failed to loaded CSS shape cache",
            exc_info=True,
            key=shape_key,
        )

    if css_shape:
        LOG.debug("CSS shape loaded from cache", element_id=element_id, key=shape_key, shape=css_shape)
    else:
        if _is_element_already_dropped(shape_key):
            LOG.debug("CSS shape is already dropped, going to abort conversion", element_id=element_id, key=shape_key)
            return None
        try:
            locater = skyvern_frame.get_frame().locator(f'[{SKYVERN_ID_ATTR}="{element_id}"]')
            if await locater.count() == 0:
                LOG.info(
                    "No locater found to convert css shape",
                    element_id=element_id,
                    key=shape_key,
                )
                return None

            skyvern_element = SkyvernElement(locator=locater, frame=skyvern_frame.get_frame(), static_element=element)

            _, blocked = await skyvern_frame.get_blocking_element_id(await skyvern_element.get_element_handler())
            if blocked:
                LOG.debug(
                    "element is blocked by another element, going to abort conversion",
                    element_id=element_id,
                    key=shape_key,
                )
                return None

            try:
                await locater.scroll_into_view_if_needed(timeout=settings.BROWSER_ACTION_TIMEOUT_MS)
                await locater.wait_for(state="visible", timeout=settings.BROWSER_ACTION_TIMEOUT_MS)
            except Exception:
                LOG.info(
                    "Failed to make the element visible, going to abort conversion",
                    exc_info=True,
                    element_id=element_id,
                    key=shape_key,
                )
                return None

            LOG.debug("call LLM to convert css shape to string shape", element_id=element_id)
            screenshot = await locater.screenshot(timeout=settings.BROWSER_ACTION_TIMEOUT_MS, animations="disabled")
            prompt = prompt_engine.load_prompt("css-shape-convert")

            # TODO: we don't retry the css shape conversion today
            for retry in range(CSS_SHAPE_CONVERTION_ATTEMPTS):
                try:
                    async with asyncio.timeout(_LLM_CALL_TIMEOUT_SECONDS):
                        if app.SVG_CSS_CONVERTER_LLM_API_HANDLER is None:
                            raise Exception("To enable css shape conversion, please set the Secondary LLM key")
                        json_response = await app.SVG_CSS_CONVERTER_LLM_API_HANDLER(
                            prompt=prompt, screenshots=[screenshot], step=step, prompt_name="css-shape-convert"
                        )
                    css_shape = json_response.get("shape", "")
                    recognized = json_response.get("recognized", False)
                    if not css_shape or not recognized:
                        raise Exception("Empty or unrecognized css shape replied by secondary llm")
                    LOG.info("CSS Shape converted by LLM", element_id=element_id, key=shape_key, shape=css_shape)
                    await _set_css_shape_cache(shape_key, css_shape)
                    break
                except LLMProviderError:
                    LOG.info(
                        "Failed to convert css shape due to llm error. Will retry if haven't met the max try attempt.",
                        exc_info=True,
                        element_id=element_id,
                        retry=retry,
                        key=shape_key,
                    )
                    if retry == CSS_SHAPE_CONVERTION_ATTEMPTS - 1:
                        # set the invalid css shape to cache to avoid retry in the near future
                        await _set_css_shape_cache(shape_key, INVALID_SHAPE, ex=timedelta(hours=1))
                except asyncio.TimeoutError:
                    LOG.warning(
                        "Timeout to call LLM to parse css shape. Going to abort the convertion directly.",
                        element_id=element_id,
                        key=shape_key,
                    )
                    _add_to_dropped_css_svg_element_map(shape_key)
                    return None
                except Exception:
                    LOG.info(
                        "Failed to convert css shape to string shape by secondary llm. Will retry if haven't met the max try attempt.",
                        exc_info=True,
                        element_id=element_id,
                        retry=retry,
                        key=shape_key,
                    )
                    if retry == CSS_SHAPE_CONVERTION_ATTEMPTS - 1:
                        # set the invalid css shape to cache to avoid retry in the near future
                        await _set_css_shape_cache(shape_key, INVALID_SHAPE, ex=timedelta(weeks=1))
            else:
                LOG.info(
                    "Max css shape convertion retry, going to abort the convertion.",
                    element_id=element_id,
                    key=shape_key,
                )
                _add_to_dropped_css_svg_element_map(shape_key)
                return None
        except Exception:
            LOG.warning(
                "Failed to convert css shape to string shape by LLM",
                key=shape_key,
                element_id=element_id,
                exc_info=True,
            )
            _add_to_dropped_css_svg_element_map(shape_key)
            return None

    if "attributes" not in element:
        element["attributes"] = dict()
    if css_shape != INVALID_SHAPE:
        # refresh the cache expiration
        await _set_css_shape_cache(shape_key, css_shape)
        element["attributes"]["shape-description"] = css_shape
    return None


class AgentFunction:
    workflow_schedules_enabled: bool = settings.ENABLE_WORKFLOW_SCHEDULES
    """Whether the workflow scheduler routes should serve traffic on this build.

    OSS Skyvern uses a local background scheduler by default. Set
    ENABLE_WORKFLOW_SCHEDULES=false to disable the routes and scheduler.
    Cloud overrides the local scheduler flag and provides the Temporal-backed
    implementations below.
    """
    workflow_schedules_use_local_scheduler: bool = settings.ENABLE_WORKFLOW_SCHEDULES
    """Whether the API process should run the built-in local scheduler loop."""

    def is_wait_time_optimization_enabled(self) -> bool:
        return False

    def build_proxy_session_extra_http_headers(self, proxy_session_id: str | None) -> dict[str, str] | None:
        return None

    def has_proxy_session_extra_http_headers(self, extra_http_headers: dict[str, str] | None) -> bool:
        return False

    def strip_proxy_session_extra_http_headers(
        self,
        extra_http_headers: dict[str, str] | None,
    ) -> dict[str, str] | None:
        return extra_http_headers

    def merge_proxy_session_extra_http_headers(
        self,
        extra_http_headers: dict[str, str] | None,
        proxy_session_id: str | None,
    ) -> dict[str, str] | None:
        proxy_session_headers = self.build_proxy_session_extra_http_headers(proxy_session_id)
        if not proxy_session_headers:
            return extra_http_headers

        headers = dict(extra_http_headers or {})
        for key, value in proxy_session_headers.items():
            headers.setdefault(key, value)
        return headers

    def get_flex_llm_key(self, llm_key: str | None) -> str | None:
        """Return a flex-tier router key for the given LLM key, or None if no flex twin exists.

        Cloud overrides this with the Gemini family → flex+GPT-5-fallback mapping.
        OSS no-op so self-hosted users without flex routers see no behavior change.
        """
        return None

    def get_non_flex_llm_key(self, llm_key: str | None) -> str | None:
        """Return a non-flex router twin for the given LLM key, or None if no twin exists.

        Cloud overrides this with the flex-router → standard-router mapping.
        OSS no-op so self-hosted users without flex routers see no behavior change.
        """
        return None

    def get_fallback_llm_key(self, llm_key: str | None) -> str | None:
        """Return a provider-fallback router twin for the given LLM key, or None if none exists.

        Cloud overrides this with the bare-Gemini-key → *_WITH_FALLBACK mapping so a
        provider-specific failure falls over to another provider. OSS has no fallback
        routers, so the default no-op leaves the caller's key untouched.
        """
        return None

    async def should_use_flex_llm_routing(
        self,
        *,
        trigger_type: WorkflowRunTriggerType | None,
        organization: Organization,
        workflow_permanent_id: str,
        workflow_run_id: str,
    ) -> bool:
        """Decide whether a given workflow run is eligible for flex-tier LLM routing.

        Receives the full Organization so implementations can gate on org attributes.
        Cloud overrides this to consult its experimentation provider; OSS has no flex
        routers so the default returns False."""
        return False

    async def resolve_recording_video_size(
        self,
        current_size: dict[str, int] | None,
        *,
        distinct_id: str | None,
        organization_id: str | None,
        workflow_permanent_id: str | None = None,
    ) -> dict[str, int] | None:
        """Resolve the browser recording resolution for this run.

        Returns ``current_size`` unchanged. Cloud overrides this to opt runs into
        an elevated resolution behind a feature flag.
        """
        return current_size

    async def should_keep_code_mode_for_workflow_run(
        self,
        *,
        workflow: Workflow,
        workflow_run: WorkflowRun,
    ) -> bool:
        return True

    async def should_upgrade_to_code_mode(
        self,
        *,
        workflow: "Workflow",
        workflow_run: "WorkflowRun",
    ) -> bool:
        return False

    async def resolve_mcp_code_only_mode(
        self,
        organization_id: str | None,
        request_override: bool | None,
    ) -> bool:
        return request_override if request_override is not None else settings.MCP_CODE_ONLY_MODE

    async def resolve_copilot_author_time_gate_log_only_ids(
        self,
        *,
        turn_id: str,
        organization_id: str,
    ) -> frozenset[str]:
        return frozenset()

    async def should_use_codeblock_runner(
        self,
        *,
        workflow_run_id: str,
        workflow_run_block_id: str,
        workflow_run_context: WorkflowRunContext,
        organization_id: str | None,
        block_label: str | None,
        browser_session_id: str | None,
    ) -> bool:
        """Whether a workflow CodeBlock run should execute in the secure runner sidecar.

        Gating lives here, at the block-execution call site, rather than inside
        execute_code_block_override so the override only runs the runner. OSS has no
        runner and returns False; cloud overrides to consult SECURE_CODEBLOCK_ENABLED and
        only routes runs that have a browser session for the runner to broker against.
        """
        return False

    async def should_auto_create_browser_session_for_code_block(
        self,
        *,
        workflow_run_id: str,
        organization_id: str | None,
        workflow_permanent_id: str | None = None,
        workflow_id: str | None = None,
    ) -> bool:
        """Whether a run containing a CodeBlock should get an auto-provisioned browser session.

        The secure CodeBlock runner brokers page operations against a live persistent browser
        session, so a run that has a CodeBlock but no caller-supplied session needs one created
        for it before block execution. OSS has no runner and returns False; cloud overrides to
        consult the same env/flag gate as should_use_codeblock_runner.
        """
        return False

    async def execute_code_block_override(
        self,
        *,
        block: Any,
        workflow_run_id: str,
        workflow_run_block_id: str,
        organization_id: str | None,
        browser_session_id: str | None,
        workflow_run_context: WorkflowRunContext,
        parameter_values: dict[str, Any],
        credential_parameter_keys: set[str],
        recording_page: RecordingPage | None = None,
    ) -> CodeBlockEngineResult | None:
        """Run a CodeBlock through the secure runner sidecar, or return None for legacy.

        OSS no-op returns None so callers fall through to in-process execution. Cloud
        overrides to dispatch to the runner. Callers must gate on
        should_use_codeblock_runner first.
        """
        return None

    async def should_dispatch_copilot_block_run_to_worker(
        self,
        *,
        organization_id: str,
        workflow_permanent_id: str,
    ) -> bool:
        """Base no-op (copilot runs its block test inline); overridden per deployment."""
        return False

    def resolve_copilot_dispatch_trigger_type(self) -> WorkflowRunTriggerType | None:
        """Base no-op (no dispatch routing hint); overridden per deployment."""
        return None

    async def is_workflow_tagging_enabled(self, organization_id: str) -> bool:
        """OSS always-on; cloud overrides to gate per-org for staged rollout."""
        return True

    async def get_analytics_warmable_organization_ids(self, statement_timeout_seconds: int = 10) -> list[str]:
        """Return org IDs whose analytics summary cache should be kept warm.

        OSS returns all org IDs. Cloud overrides with enterprise pricing filter.
        """
        orgs = await app.DATABASE.organizations.get_all_organizations()
        return [org.organization_id for org in orgs]

    async def record_workflow_run_metadata(
        self,
        *,
        workflow_run_id: str,
        organization_id: str,
        run_metadata: dict[str, str] | None,
    ) -> None:
        """Persist per-run analytics metadata. OSS builds have no sidecar table."""
        return None

    async def get_workflow_run_metadata(
        self,
        *,
        workflow_run_id: str,
        organization_id: str,
    ) -> dict[str, str] | None:
        """Fetch per-run analytics metadata. OSS builds have no sidecar table."""
        return None

    async def is_block_scoped_workflow_run(self, workflow_run: WorkflowRun) -> bool:
        """Return whether this workflow run was created for scoped block execution."""
        return workflow_run.debug_session_id is not None

    # Phrases that indicate a magic-link confirmation page meant to be closed.
    # Keep lowercase; matching is case-insensitive.
    MAGIC_LINK_CLOSE_SIGNALS: tuple[str, ...] = (
        "close this page",
        "close this tab",
        "close this window",
        "you can close",
        "you may now close",
        "safe to close",
        "return to the original page",
        "return to the original tab",
    )

    def get_mcp_oauth_issuer_url(self) -> str | None:
        """Return the cloud OAuth issuer URL when the build provides one.

        OSS builds do not ship a remote OAuth issuer, so the base implementation
        returns None and callers should treat OAuth validation as unavailable.
        """
        return None

    async def get_mcp_oauth_jwt_key(self) -> Any | None:
        """Return the current signing key/JWK for MCP OAuth token validation.

        Cloud builds override this to provide the identity-provider signing key.
        OSS builds return None.
        """
        return None

    def build_mcp_auth_db(self, database_string: str, *, debug_enabled: bool) -> Any:
        """Return the DB instance used by MCP auth middleware.

        OSS builds use the base ``AgentDB``. Cloud overrides this to provide
        the encryption-aware ``CloudAgentDB`` implementation without importing
        cloud modules from the OSS-synced ``skyvern/`` tree.
        """
        return AgentDB(database_string, debug_enabled=debug_enabled)

    def build_azure_client_factory(self, factory: AzureClientFactory) -> AzureClientFactory:
        return factory

    def resolve_mcp_oauth_org_lookups(self, db: object) -> tuple[Any, Any] | None:
        """Return ``(get_organization_entities, get_valid_org_auth_token)`` callables
        bound to the cloud DB's nested organizations repository.

        OSS no-op — the OSS path in ``_get_oauth_org_auth_methods`` probes the
        flat shape directly. Cloud overrides this to expose the
        ``CloudAgentDB.organizations`` repository's methods so the OSS-synced
        ``mcp_http_auth`` module doesn't need to know about cloud-specific
        DB layout.
        """
        return None

    async def resolve_org_api_key(self, organization_id: str) -> str | None:
        """Return an org-scoped API key; returns None in the base implementation."""
        return None

    async def resolve_self_heal_api_key(self, organization_id: str) -> str | None:
        del organization_id
        api_key = settings.SKYVERN_API_KEY
        return api_key if api_key and api_key != "PLACEHOLDER" else None

    async def setup_browser_context_extensions(self, browser_context: Any, **kwargs: Any) -> None:
        """Attach cloud-only listeners/route handlers to a fresh BrowserContext. OSS no-op."""

    async def validate_step_execution(
        self,
        task: Task,
        step: Step,
    ) -> None:
        """
        Checks if the step can be executed. It is called before the step is executed.
        :return: A tuple of whether the step can be executed and a list of reasons why it can't be executed.
        """
        reasons = []
        if task.status == TaskStatus.timed_out:
            raise TaskAlreadyTimeout(task_id=task.task_id)

        # can't execute if task status is not running
        has_valid_task_status = task.status == TaskStatus.running
        if not has_valid_task_status:
            reasons.append(f"invalid_task_status:{task.status}")
        # can't execute if the step is already running or completed
        has_valid_step_status = step.status in [StepStatus.created, StepStatus.failed]
        if not has_valid_step_status:
            reasons.append(f"invalid_step_status:{step.status}")
        # can't execute if the task has another step that is running
        steps = await app.DATABASE.tasks.get_task_steps(task_id=task.task_id, organization_id=task.organization_id)
        has_no_running_steps = not any(step.status == StepStatus.running for step in steps)
        if not has_no_running_steps:
            reasons.append(f"another_step_is_running_for_task:{task.task_id}")

        can_execute = has_valid_task_status and has_valid_step_status and has_no_running_steps
        if not can_execute:
            raise StepUnableToExecuteError(step_id=step.step_id, reason=f"Cannot execute step. Reasons: {reasons}")

    async def validate_block_execution(
        self, block: BlockTypeVar, workflow_run_id: str, workflow_run_block_id: str, organization_id: str | None
    ) -> None:
        return

    async def validate_task_execution(
        self, organization_id: str, task_id: str | None = None, task_version: str | None = None
    ) -> None:
        return

    async def validate_enterprise_feature_access(
        self,
        organization_id: str | None = None,
        feature_names: set[str] | None = None,
    ) -> None:
        return

    async def parse_enterprise_totp_secret(
        self,
        totp_secret: str,
        organization_id: str | None = None,
    ) -> str | None:
        return None

    async def parse_enterprise_totp_secret_result(
        self,
        totp_secret: str,
        organization_id: str | None = None,
    ) -> AuthenticatorTotpParseResult:
        return AuthenticatorTotpParseResult()

    async def prepare_step_execution(
        self,
        organization: Organization | None,
        task: Task,
        step: Step,
        browser_state: BrowserState,
    ) -> list[Action] | None:
        """
        Get prepared for the step execution. It's called at the first beginning when step running.

        Returns:
            A list of actions to inject into the step (skipping LLM), or None for normal flow.
        """
        return None

    async def post_step_execution(self, task: Task, step: Step) -> None:
        if step.status == StepStatus.completed:
            await self._maybe_close_magic_link_page(task)

    async def _maybe_close_magic_link_page(self, task: Task) -> None:
        """Close a magic-link confirmation page if it shows close/return signals.

        Some magic-link flows open a new tab for verification.
        After the user clicks "Allow", the page says "close this page and return
        to the original page".  The LLM may miss the CLOSE_PAGE action, leaving
        the tab open.  This fallback detects such confirmation pages and closes
        them so subsequent workflow blocks see the original page.
        """
        context = skyvern_context.current()
        if not context:
            return

        if not context.has_magic_link_page(task.task_id):
            return

        page = context.magic_link_pages[task.task_id]

        try:
            visible_text = (await page.inner_text("body", timeout=5000)).lower()
        except Exception:
            LOG.warning(
                "Failed to read magic link page content, skipping auto-close",
                task_id=task.task_id,
                exc_info=True,
            )
            return

        matched_signal = next(
            (signal for signal in self.MAGIC_LINK_CLOSE_SIGNALS if signal in visible_text),
            None,
        )
        if matched_signal is None:
            LOG.debug(
                "Magic link page does not contain close signals, keeping open",
                task_id=task.task_id,
                page_url=page.url,
            )
            return

        LOG.info(
            "Magic link confirmation page detected, auto-closing",
            task_id=task.task_id,
            page_url=page.url,
            matched_signal=matched_signal,
        )
        try:
            async with asyncio.timeout(5):
                await page.close()
        except Exception:
            # Intentionally keep the stale reference so the next completed step
            # retries the close.  Eventually page.is_closed() will return True
            # and the entry will be cleaned up at the top of this method.
            LOG.warning(
                "Failed to close magic link page, will retry on next step",
                task_id=task.task_id,
                exc_info=True,
            )
            return

        context.magic_link_pages.pop(task.task_id, None)
        LOG.info(
            "Magic link page closed successfully",
            task_id=task.task_id,
        )

    async def post_cache_step_execution(self, task: Task, step: Step) -> None:
        return

    async def post_code_block_execution(self, task: Task, step: Step) -> None:
        """Billing seam for a code block's container task; called only after a successful execution."""
        return

    async def wait_for_challenge_solver(self, page: Page) -> None:
        """Wait for a cloud-managed challenge solver if one is attached to the page."""
        return None

    async def should_shadow_extraction_cache_hit(self, task: Task) -> bool:
        """Cloud-overridable sample gate for extract-information shadow mode. OSS no-op."""
        return False

    async def lookup_cross_run_extraction_cache(
        self,
        workflow_permanent_id: str | None,
        cache_key: str,
    ) -> Any | None:
        """Cross-run (wpid-scoped) extraction-cache read. OSS no-op."""
        return None

    async def store_cross_run_extraction_cache(
        self,
        workflow_permanent_id: str | None,
        cache_key: str,
        value: Any,
    ) -> None:
        """Cross-run (wpid-scoped) extraction-cache write. OSS no-op."""
        return None

    def build_workflow_schedule_id(self, workflow_schedule_id: str) -> str | None:
        """Return the backend-specific schedule id used by the execution engine.

        OSS uses a deterministic local backend id. Cloud overrides this to derive
        a Temporal schedule id.
        """
        return f"local-wf-sched-{workflow_schedule_id}"

    async def upsert_workflow_schedule(
        self,
        backend_schedule_id: str,
        organization_id: str,
        workflow_permanent_id: str,
        workflow_schedule_id: str,
        cron_expression: str,
        timezone: str,
        enabled: bool,
        parameters: dict[str, Any] | None = None,
        max_elapsed_time_minutes: int | None = None,
    ) -> None:
        """Upsert a recurring schedule with the execution backend (e.g. Temporal).

        OSS base is a no-op because the local scheduler scans the database.
        Cloud overrides this to register the schedule with Temporal.
        Implementations must be idempotent.
        """
        return None

    async def set_workflow_schedule_enabled(self, backend_schedule_id: str, enabled: bool) -> None:
        """Pause or resume a schedule on the execution backend. OSS no-op."""
        return None

    async def delete_workflow_schedule(self, backend_schedule_id: str) -> None:
        """Delete a schedule from the execution backend. OSS no-op."""
        return None

    async def calculate_workflow_run_total_cost(
        self,
        organization_id: str | None,
        credits_used: int,
        cached_credits_used: int,
    ) -> float | None:
        """Compute the user-facing ``total_cost`` for a workflow run. OSS returns None."""
        return None

    async def auto_solve_captchas(self, page: Page) -> bool:
        """Proactively detect and solve captchas on the current page.
        Returns True if a captcha was detected and solved.
        Cloud override provides actual solving; OSS base is a no-op."""
        return False

    async def get_google_sheets_credentials(
        self,
        organization_id: str,
        credential_id: str,
    ) -> str | None:
        """Mint a Google Sheets access token from the stored refresh token.

        Returns None on any failure so callers can surface a reconnect prompt
        instead of crashing. Cloud overrides this with an access-token cache
        on top of the same backend.
        """
        try:
            secrets = await google_oauth_service.load_credential_secrets(
                organization_id=organization_id,
                credential_id=credential_id,
            )
            return await google_oauth_service.access_token_from_secrets(secrets, organization_id=organization_id)
        except google_oauth_service.EncryptionNotConfiguredError:
            LOG.error(
                "Google credential encryption is not configured; operators must enable ENABLE_ENCRYPTION",
                organization_id=organization_id,
                credential_id=credential_id,
            )
            return None
        except Exception:
            LOG.exception(
                "Failed to get Google Sheets credentials",
                organization_id=organization_id,
                credential_id=credential_id,
            )
            return None

    async def get_microsoft_credentials(
        self,
        organization_id: str,
        credential_id: str,
        required_scopes: list[str] | None = None,
    ) -> str | None:
        try:
            secrets = await microsoft_oauth_service.load_credential_secrets(
                organization_id=organization_id,
                credential_id=credential_id,
            )
            if required_scopes and not microsoft_oauth_service.has_required_scopes(secrets.scopes, required_scopes):
                LOG.info(
                    "Microsoft OAuth credential missing required scopes",
                    organization_id=organization_id,
                    credential_id=credential_id,
                    required_scopes=required_scopes,
                )
                return None
            return await microsoft_oauth_service.refresh_and_rotate(
                organization_id=organization_id,
                credential_id=credential_id,
                credential_secrets=secrets,
            )
        except Exception:
            LOG.exception(
                "Failed to get Microsoft credentials",
                organization_id=organization_id,
                credential_id=credential_id,
            )
            return None

    async def get_google_workspace_credentials(
        self,
        organization_id: str,
        credential_id: str,
        required_scopes: list[str] | None = None,
    ) -> Credentials | None:
        """Return a refreshed ``google.oauth2.credentials.Credentials``, or None on failure.

        ``required_scopes`` gates use of a credential whose grant does not cover
        the API the caller is about to use.
        """
        try:
            secrets = await google_oauth_service.load_credential_secrets(
                organization_id=organization_id,
                credential_id=credential_id,
            )
            if required_scopes and not google_oauth_service.has_required_scopes(secrets.scopes, required_scopes):
                LOG.info(
                    "Google OAuth credential missing required scopes",
                    organization_id=organization_id,
                    credential_id=credential_id,
                    required_scopes=required_scopes,
                )
                return None
            return await google_oauth_service.credentials_from_secrets(secrets, organization_id=organization_id)
        except google_oauth_service.EncryptionNotConfiguredError:
            LOG.error(
                "Google credential encryption is not configured; operators must enable ENABLE_ENCRYPTION",
                organization_id=organization_id,
                credential_id=credential_id,
            )
            return None
        except Exception:
            LOG.exception(
                "Failed to get Google Workspace credentials",
                organization_id=organization_id,
                credential_id=credential_id,
            )
            return None

    async def get_otp_value_from_gmail(
        self,
        *,
        organization_id: str,
        totp_identifier: str,
        workflow_id: str | None = None,
        workflow_run_id: str | None = None,
        created_after: datetime | None = None,
        context: GmailOTPVerificationContext | None = None,
    ) -> OTPValue | None:
        """Find an OTP in connected Gmail inboxes for a single polling window."""
        if "@" not in totp_identifier:
            return None

        from skyvern.services.otp_service import parse_otp_login

        lookup_context = context or GmailOTPVerificationContext()
        now = datetime.now(timezone.utc)
        required_scopes = list(google_oauth_service.GOOGLE_GMAIL_SCOPES)
        credential_cache_age = (
            (now - lookup_context.credential_ids_loaded_at).total_seconds()
            if lookup_context.credential_ids_loaded_at
            else None
        )
        if (
            lookup_context.credential_ids is None
            or credential_cache_age is None
            or credential_cache_age >= GMAIL_OTP_CREDENTIAL_REFRESH_INTERVAL_SECONDS
        ):
            try:
                lookup_context.credential_ids = [
                    credential.id
                    for credential in await google_oauth_service.get_credentials_for_org(organization_id)
                    if google_oauth_service.has_required_scopes(credential.scopes_granted, required_scopes)
                ]
                lookup_context.credential_ids_loaded_at = now
            except Exception:
                LOG.warning("Failed to list Google OAuth credentials for Gmail OTP lookup", exc_info=True)
                return None

        async with httpx.AsyncClient(timeout=20.0) as gmail_client:
            for credential_id in lookup_context.credential_ids or []:
                last_searched_at = lookup_context.last_searched_at_by_credential.get(credential_id)
                if last_searched_at and (now - last_searched_at).total_seconds() < GMAIL_OTP_SEARCH_INTERVAL_SECONDS:
                    continue
                lookup_context.last_searched_at_by_credential[credential_id] = now
                try:
                    google_credentials = await self.get_google_workspace_credentials(
                        organization_id=organization_id,
                        credential_id=credential_id,
                        required_scopes=required_scopes,
                    )
                    if not google_credentials or not google_credentials.token:
                        continue
                    candidates = await google_gmail_service.search_recent_otp_messages(
                        access_token=google_credentials.token,
                        totp_identifier=totp_identifier,
                        created_after=created_after,
                        max_results=GMAIL_OTP_MAX_RESULTS,
                        client=gmail_client,
                    )
                except google_gmail_service.GmailAPIError as exc:
                    LOG.warning(
                        "Gmail OTP lookup failed",
                        credential_id=credential_id,
                        status=exc.status,
                        code=exc.code,
                    )
                    continue
                except Exception:
                    LOG.warning(
                        "Unexpected Gmail OTP lookup failure",
                        credential_id=credential_id,
                        exc_info=True,
                    )
                    continue

                for candidate in candidates:
                    if lookup_context.has_seen_message_id(candidate.message_id):
                        continue
                    try:
                        otp_value = await parse_otp_login(candidate.content, organization_id)
                    except Exception:
                        LOG.warning(
                            "Failed to parse Gmail OTP candidate",
                            credential_id=credential_id,
                            message_id=candidate.message_id,
                            exc_info=True,
                        )
                        continue
                    lookup_context.remember_message_id(candidate.message_id)
                    if otp_value:
                        try:
                            await app.DATABASE.otp.create_otp_code(
                                organization_id,
                                totp_identifier,
                                otp_value.value,
                                otp_value.value,
                                otp_value.get_otp_type(),
                                workflow_id=workflow_id,
                                workflow_run_id=workflow_run_id,
                                source="gmail",
                            )
                        except Exception:
                            LOG.warning("Failed to persist Gmail OTP code", credential_id=credential_id, exc_info=True)
                        return otp_value

        return None

    async def ensure_sheet_tab(
        self,
        *,
        access_token: str,
        spreadsheet_id: str,
        title: str,
    ) -> int | None:
        """Ensure a sheet tab with the given title exists in the spreadsheet."""
        try:
            tab = await google_sheets_service.create_sheet_tab(
                access_token=access_token,
                spreadsheet_id=spreadsheet_id,
                title=title,
            )
            return tab.sheet_id
        except google_sheets_service.GoogleSheetsAPIError as exc:
            if exc.status == 400 and exc.code in {"duplicate", "duplicateSheetTitle"}:
                return None
            raise

    async def google_sheets_values_get(
        self,
        *,
        access_token: str,
        spreadsheet_id: str,
        ranges: str,
        fields: str | None = None,
    ) -> dict[str, Any] | None:
        return await google_sheets_service.values_get(
            access_token=access_token,
            spreadsheet_id=spreadsheet_id,
            ranges=ranges,
            fields=fields,
        )

    async def google_sheets_values_append(
        self,
        *,
        access_token: str,
        spreadsheet_id: str,
        range_: str,
        values: list[list[Any]],
    ) -> dict[str, Any] | None:
        return await google_sheets_service.values_append(
            access_token=access_token,
            spreadsheet_id=spreadsheet_id,
            range_=range_,
            values=values,
        )

    async def google_sheets_values_update(
        self,
        *,
        access_token: str,
        spreadsheet_id: str,
        range_: str,
        values: list[list[Any]],
    ) -> dict[str, Any] | None:
        return await google_sheets_service.values_update(
            access_token=access_token,
            spreadsheet_id=spreadsheet_id,
            range_=range_,
            values=values,
        )

    async def google_sheets_batch_update(
        self,
        *,
        access_token: str,
        spreadsheet_id: str,
        requests: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        return await google_sheets_service.batch_update(
            access_token=access_token,
            spreadsheet_id=spreadsheet_id,
            requests=requests,
        )

    async def google_sheets_get_sheet_id(
        self,
        *,
        access_token: str,
        spreadsheet_id: str,
        sheet_title: str,
    ) -> int | None:
        return await google_sheets_service.get_sheet_id_by_title(
            access_token=access_token,
            spreadsheet_id=spreadsheet_id,
            sheet_title=sheet_title,
        )

    async def google_sheets_get_grid_properties(
        self,
        *,
        access_token: str,
        spreadsheet_id: str,
        sheet_title: str,
    ) -> Any | None:
        return await google_sheets_service.get_sheet_grid_properties(
            access_token=access_token,
            spreadsheet_id=spreadsheet_id,
            sheet_title=sheet_title,
        )

    async def google_sheets_get_grid_properties_by_id(
        self,
        *,
        access_token: str,
        spreadsheet_id: str,
        sheet_id: int,
    ) -> Any | None:
        return await google_sheets_service.get_sheet_grid_properties_by_id(
            access_token=access_token,
            spreadsheet_id=spreadsheet_id,
            sheet_id=sheet_id,
        )

    async def generate_async_operations(
        self,
        organization: Organization,
        task: Task,
        page: Page,
    ) -> list[AsyncOperation]:
        return []

    def cleanup_element_tree_factory(
        self,
        task: Task | None = None,
        step: Step | None = None,
    ) -> CleanupElementTreeFunc:
        MAX_ELEMENT_CNT = settings.SVG_MAX_PARSING_ELEMENT_CNT

        @traced(name="skyvern.agent.cleanup_element_tree")
        async def cleanup_element_tree_func(frame: Page | Frame, url: str, element_tree: list[dict]) -> list[dict]:
            """
            Remove rect and attribute.unique_id from the elements.
            The reason we're doing it is to
            1. reduce unnecessary data so that llm get less distrction
            TODO later: 2. reduce tokens sent to llm to save money
            :param elements: List of elements to remove xpaths from.
            :return: List of elements without xpaths.
            """
            context = skyvern_context.ensure_context()
            # page won't be in the context.frame_index_map, so the index is going to be 0
            skyvern_frame = await SkyvernFrame.create_instance(frame=frame)
            current_frame_index = context.frame_index_map.get(frame, 0)

            queue = []
            element_cnt = 0
            eligible_svgs = []  # List to store eligible SVGs and their frames
            eligible_css_shapes = []  # List to store eligible CSS shapes for parallel conversion

            for element in element_tree:
                queue.append(element)

            while queue:
                queue_ele = queue.pop(0)

                element_cnt += 1
                if element_cnt == MAX_ELEMENT_CNT:
                    LOG.debug(f"Element reached max count {MAX_ELEMENT_CNT}, will stop converting svg and css element.")
                disable_conversion = element_cnt > MAX_ELEMENT_CNT
                if app.SVG_CSS_CONVERTER_LLM_API_HANDLER is None or not settings.ENABLE_CSS_SVG_PARSING:
                    disable_conversion = True

                if queue_ele.get("frame_index") != current_frame_index:
                    new_frame = next(
                        (k for k, v in context.frame_index_map.items() if v == queue_ele.get("frame_index")), frame
                    )
                    skyvern_frame = await SkyvernFrame.create_instance(frame=new_frame)
                    current_frame_index = queue_ele.get("frame_index", 0)

                _remove_rect(queue_ele)

                # Check SVG eligibility and store for later conversion
                if await _check_svg_eligibility(skyvern_frame, queue_ele, task, step, always_drop=disable_conversion):
                    eligible_svgs.append((queue_ele, skyvern_frame))

                if not disable_conversion and _should_css_shape_convert(element=queue_ele):
                    eligible_css_shapes.append((queue_ele, skyvern_frame))

                # TODO: we can come back to test removing the unique_id
                # from element attributes to make sure this won't increase hallucination
                # _remove_unique_id(queue_ele)
                if "children" in queue_ele:
                    queue.extend(queue_ele["children"])

            if eligible_css_shapes and task and step:
                await asyncio.gather(
                    *[
                        _convert_css_shape_to_string(skyvern_frame=sf, element=elem, task=task, step=step)
                        for elem, sf in eligible_css_shapes
                    ]
                )

            if eligible_svgs:
                await asyncio.gather(*[_convert_svg_to_string(element, task, step) for element, frame in eligible_svgs])

            return element_tree

        return cleanup_element_tree_func

    async def has_code_block_access(self, organization_id: str | None = None) -> bool:
        return settings.ENABLE_CODE_BLOCK

    async def validate_code_block(self, organization_id: str | None = None) -> None:
        if not await self.has_code_block_access(organization_id):
            raise DisabledBlockExecutionError("CodeBlock is disabled")

    # TODO: Remove these methods if nothing calls them after verifying in production
    async def _post_action_execution(self, action: Action) -> None:
        """Post-action hook - now a no-op.

        Script generation moved to block-level via _generate_pending_script_for_block() in service.py.
        """

    async def post_action_execution(self, action: Action) -> None:
        pass

    async def deliver_webhook(
        self,
        url: str,
        payload: str,
        headers: dict[str, str],
        timeout_seconds: float = 30.0,
        organization_id: str | None = None,
        run_id: str | None = None,
    ) -> httpx.Response:
        """Deliver a webhook POST request to *url*.

        Returns the upstream ``httpx.Response``.  Cloud override routes NAT-org
        traffic through the egress proxy so it egresses from a static IP.
        """
        async with httpx.AsyncClient() as client:
            return await client.post(
                url,
                content=payload,
                headers=headers,
                timeout=httpx.Timeout(timeout_seconds),
            )

    async def post_totp_verification_request(
        self,
        url: str,
        payload: str,
        headers: dict[str, str],
        timeout_seconds: float = 30.0,
        organization_id: str | None = None,
    ) -> TOTPVerificationResponse:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout_seconds)) as session:
            async with session.post(url, data=payload, headers=headers) as response:
                body = await response.text()
                return TOTPVerificationResponse(
                    status_code=response.status,
                    body=body,
                    headers=dict(response.headers),
                )

    async def upload_file_to_customer_storage(
        self,
        file_path: str,
        destination: FileUploadDestination,
        organization_id: str | None = None,
        run_id: str | None = None,
    ) -> str:
        """Upload a single file to customer-specified cloud storage.

        Returns the customer-facing URI (``destination.customer_uri``).  The
        cloud override routes NAT-org traffic through the egress proxy so it
        egresses from a static IP; the OSS base path uploads directly via the
        AWS / Azure SDK.

        Enforces ``CUSTOMER_STORAGE_UPLOAD_MAX_BYTES`` (1 GB) regardless of
        route so the proxy pod and the customer's quota are both protected
        from runaway uploads.
        """
        await self._enforce_upload_size_cap(file_path)

        if destination.storage_type == FileStorageType.S3:
            aws_client = AsyncAWSClient(
                aws_access_key_id=destination.aws_access_key_id,
                aws_secret_access_key=destination.aws_secret_access_key,
                region_name=destination.aws_region_name,
            )
            await aws_client.upload_file_from_path(
                uri=destination.sdk_uri,
                file_path=file_path,
                raise_exception=True,
            )
            return destination.customer_uri

        if destination.storage_type == FileStorageType.AZURE:
            if not destination.azure_storage_account_name or not destination.azure_storage_account_key:
                raise AzureConfigurationError("Azure Storage is not configured")
            azure_client = app.AZURE_CLIENT_FACTORY.create_storage_client(
                storage_account_name=destination.azure_storage_account_name,
                storage_account_key=destination.azure_storage_account_key,
            )
            await azure_client.upload_file_from_path(destination.sdk_uri, file_path)
            return destination.customer_uri

        if destination.storage_type == FileStorageType.GOOGLE_DRIVE:
            if not destination.google_access_token or not destination.google_drive_folder_id:
                raise ValueError("Google Drive destination is missing required fields")
            uploaded_file = await google_drive_service.upload_file(
                access_token=destination.google_access_token,
                file_path=file_path,
                folder_id=destination.google_drive_folder_id,
            )
            return uploaded_file.web_view_link or f"https://drive.google.com/file/d/{uploaded_file.id}/view"

        if destination.storage_type == FileStorageType.SFTP:
            if not destination.sftp_host or not destination.sftp_username:
                raise ValueError("SFTP destination is missing required fields")
            if not destination.sftp_password and not destination.sftp_private_key:
                raise ValueError("SFTP destination requires a password or private key")
            await sftp_service.upload_file(
                file_path=file_path,
                host=destination.sftp_host,
                port=destination.sftp_port if destination.sftp_port is not None else 22,
                username=destination.sftp_username,
                remote_path=destination.sftp_remote_path,
                password=destination.sftp_password,
                private_key=destination.sftp_private_key,
                private_key_passphrase=destination.sftp_private_key_passphrase,
                host_key=destination.sftp_host_key,
            )
            return destination.customer_uri

        raise ValueError(f"Unsupported storage type: {destination.storage_type}")

    @staticmethod
    async def _enforce_upload_size_cap(file_path: str) -> None:
        """Reject files larger than CUSTOMER_STORAGE_UPLOAD_MAX_BYTES.

        Centralized so direct and proxied paths share the same cap. Stat the
        file (fast, no IO of contents) and raise a typed exception so callers
        translate it into a clean block failure.
        """
        try:
            size = await asyncio.to_thread(os.path.getsize, file_path)
        except FileNotFoundError:
            raise
        if size > CUSTOMER_STORAGE_UPLOAD_MAX_BYTES:
            raise UploadFileMaxSizeExceeded(file_size_bytes=size, max_size_bytes=CUSTOMER_STORAGE_UPLOAD_MAX_BYTES)

    def get_copilot_security_rules(self) -> str:
        """Return security guardrails for the workflow copilot system prompt.

        Override in cloud to inject prompt injection defenses.
        OSS returns empty string (no hardening).
        """
        return ""

    async def acquire_copilot_entrypoint_candidates(
        self,
        *,
        site_name: str,
    ) -> list[CopilotEntrypointCandidate]:
        del site_name
        return []

    def copilot_candidate_network_guard(
        self,
        browser_context: BrowserContext,
        *,
        expected_origin: str,
    ) -> AbstractAsyncContextManager[list[CopilotCandidateNetworkHop]]:
        return self._unavailable_copilot_candidate_network_guard(browser_context, expected_origin=expected_origin)

    @asynccontextmanager
    async def _unavailable_copilot_candidate_network_guard(
        self,
        browser_context: BrowserContext,
        *,
        expected_origin: str,
    ) -> AsyncIterator[list[CopilotCandidateNetworkHop]]:
        del browser_context, expected_origin
        raise RuntimeError("Copilot candidate pre-connect enforcement is unavailable")
        yield []  # pragma: no cover

    async def wait_for_copilot_candidate_network_idle(self, browser_context: BrowserContext) -> None:
        del browser_context
        raise RuntimeError("Copilot candidate pre-connect enforcement is unavailable")

    def get_copilot_config(self, code_block_mode: bool | None = None) -> CopilotConfig | None:
        """Return an optional workflow copilot config override."""
        resolved = settings.WORKFLOW_COPILOT_CODE_BLOCK_MODE if code_block_mode is None else code_block_mode
        return CopilotConfig(
            block_authoring_policy=block_authoring_policy_from_code_only_mode(resolved),
            impose_synthesized_code_block=True,
        )

    async def get_copilot_config_for_request(
        self, organization_id: str | None = None, code_block_mode: bool | None = None
    ) -> CopilotConfig | None:
        """Return a request-scoped workflow copilot config override."""
        del organization_id
        return self.get_copilot_config(code_block_mode)

    def detect_ats_platform(self, url_or_domain: str | None) -> str | None:
        """Detect if a URL belongs to a known ATS platform.

        Returns a platform key string or None.
        Override in cloud to provide platform detection.
        """
        return None

    def detect_platform_for_tagging(self, url_or_domain: str | None) -> str | None:
        """Detect a cloud platform for run tagging. OSS intentionally does not tag."""
        return None

    def match_field_to_canonical_category(self, field_label: str) -> Any:
        """Match a form field label to a canonical category for zero-LLM matching.

        Returns a CanonicalCategory object or None.
        Override in cloud to provide canonical field matching.
        """
        return None

    def get_canonical_category(self, name: str) -> Any:
        """Look up a canonical category by name.

        Returns a CanonicalCategory object or None.
        Override in cloud to provide canonical category lookup.
        """
        return None

    def try_import_static_script(self, script_path: str) -> Any | None:
        """Try to import a static script module as a fallback when spec_from_file_location fails.

        Override in subclass for platform-specific import logic.
        Returns the loaded module or None.
        """
        return None

    async def ensure_static_script(
        self,
        workflow: Any,
        workflow_run: Any,
        organization_id: str,
    ) -> tuple[Any, dict[str, Any], Any] | None:
        """Ensure a static pre-built script exists in the DB for this platform.

        If the workflow targets a known platform (detected from block URLs),
        creates a pinned script in the DB from the static source file on first
        run.  On subsequent runs the cached script is returned normally.

        Returns ``(script, script_blocks_by_label, loaded_module)`` if a
        static script was created/loaded, or None if no static script applies.

        Override in cloud.  OSS returns None.
        """
        return None

    def build_ats_pipeline_block_fn(self, block: dict, ats_platform: str) -> Any:
        """Build an ATS-optimized script block for a detected platform.

        Returns a libcst FunctionDef or None.
        Override in cloud to provide the pipeline code generator.
        """
        return None

    def get_canonical_categories(self) -> list:
        """Return the list of all canonical categories.

        Returns an empty list in OSS. Override in cloud.
        """
        return []

    def get_form_field_mapper_hints(self) -> str | None:
        """Return platform-specific hints for the form-field-mapper LLM prompt.

        These hints are injected into the prompt template under
        ``{{ platform_hints }}``.  Use them to guide the LLM on
        field-to-data key mappings specific to a platform (e.g., ATS
        job applications).

        Returns None in OSS.  Override in cloud.
        """
        return None

    def get_form_field_extraction_js(self, url: str | None = None) -> str | None:
        """Return platform-specific JS to extend the base form field scanner.

        The returned JS is concatenated into the base extract_form_fields.js
        IIFE.  It can reference ``fields``, ``seen``, ``isVisible``,
        ``getLabel``, ``buildSelector``, ``buildOptionSelector``, and
        ``getGroupLabel`` defined in the base script.

        Args:
            url: Current page URL, used to select the right platform JS.

        Returns None in OSS (no platform-specific extraction).
        Override in cloud to inject platform-specific passes.
        """
        return None

    async def resolve_field_option(
        self,
        *,
        target_value: str,
        option_labels: list[str],
        option_values: list[str | None],
        field_context: Any,
        url: str | None,
        organization_id: str | None,
    ) -> FieldOptionResolution:
        """Resolve a requested field value against option labels.

        Only high-precision exact or whole-stem singular/plural matches are
        resolvable. A ``None`` match or ``fallback_to_llm=True`` means the
        caller must defer to the LLM path.
        """
        matched_index, matched_tier = match_option_exact_or_stem_with_tier(target_value, option_labels)
        if matched_index is None:
            return FieldOptionResolution(
                matched_index=None,
                matched_label=None,
                matched_value=None,
                confidence=0.0,
                fallback_to_llm=True,
                matched_tier=None,
            )

        matched_value = option_values[matched_index] if matched_index < len(option_values) else None
        return FieldOptionResolution(
            matched_index=matched_index,
            matched_label=option_labels[matched_index],
            matched_value=matched_value,
            confidence=1.0,
            fallback_to_llm=False,
            matched_tier=matched_tier,
        )

    async def fill_custom_widget(
        self,
        page: Any,
        field: dict,
        value: Any,
        label: str,
    ) -> bool | None:
        """Fill a platform-specific custom widget (e.g., hierarchical multiselect).

        Returns:
            True if the widget was filled successfully.
            False if the widget was recognized but filling failed.
            None if this field type is not a custom widget — caller should
            use default handling.

        Override in cloud to dispatch to platform-specific widget fillers.
        """
        return None

    async def validate_user_organization_membership(
        self,
        user_id: str,
        organization_id: str,
        bearer_token: str | None = None,
    ) -> bool | None:
        """Return whether the user belongs to the organization, or None when membership cannot be determined."""
        return None

    async def on_workflow_saved(
        self,
        organization_id: str,
        edited_by: str | None,
    ) -> None:
        """Fired after a workflow is saved. Overrides must be best-effort and never raise."""
        return None

    async def on_workflow_run_completed(
        self,
        organization_id: str,
        workflow_id: str,
        status: WorkflowRunStatus | None = None,
    ) -> None:
        """Fired after a workflow run reaches a final status. Overrides must be best-effort and never raise."""
        return None
