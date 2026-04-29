import asyncio
import copy
import hashlib
from datetime import timedelta
from typing import Any, Dict, List

import httpx
import structlog
from playwright.async_api import Frame, Page

from skyvern.config import settings
from skyvern.constants import SKYVERN_ID_ATTR
from skyvern.exceptions import DisabledBlockExecutionError, StepUnableToExecuteError, TaskAlreadyTimeout
from skyvern.forge import app
from skyvern.forge.async_operations import AsyncOperation
from skyvern.forge.prompts import prompt_engine
from skyvern.forge.sdk.api.llm.exceptions import LLMProviderError
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.db.agent_db import AgentDB
from skyvern.forge.sdk.models import Step, StepStatus
from skyvern.forge.sdk.schemas.organizations import Organization
from skyvern.forge.sdk.schemas.tasks import Task, TaskStatus
from skyvern.forge.sdk.trace import traced
from skyvern.forge.sdk.workflow.models.block import BlockTypeVar
from skyvern.webeye.actions.actions import Action
from skyvern.webeye.browser_state import BrowserState
from skyvern.webeye.scraper.scraped_page import ELEMENT_NODE_ATTRIBUTES, CleanupElementTreeFunc, json_to_html
from skyvern.webeye.utils.dom import SkyvernElement
from skyvern.webeye.utils.page import SkyvernFrame

LOG = structlog.get_logger()

_LLM_CALL_TIMEOUT_SECONDS = 30  # 30s
USELESS_SHAPE_ATTRIBUTE = [SKYVERN_ID_ATTR, "id", "aria-describedby"]
SVG_SHAPE_CONVERTION_ATTEMPTS = 3
CSS_SHAPE_CONVERTION_ATTEMPTS = 1
INVALID_SHAPE = "N/A"


def _remove_rect(element: dict) -> None:
    if "rect" in element:
        del element["rect"]


def _should_css_shape_convert(element: Dict) -> bool:
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


def _remove_skyvern_attributes(element: Dict) -> Dict:
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

    children: List[Dict] | None = element_copied.get("children", None)
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
    element: Dict,
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
    element: Dict,
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
    try:
        svg_shape = await app.CACHE.get(svg_key)
    except Exception:
        LOG.warning(
            "Failed to loaded SVG cache",
            exc_info=True,
            key=svg_key,
        )

    if svg_shape:
        LOG.debug("SVG loaded from cache", element_id=element_id, key=svg_key, shape=svg_shape)
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
                await app.CACHE.set(svg_key, svg_shape)
                break
            except LLMProviderError:
                LOG.info(
                    "Failed to convert SVG to string due to llm error. Will retry if haven't met the max try attempt after 3s.",
                    exc_info=True,
                    element_id=element_id,
                    key=svg_key,
                    retry=retry,
                )
                if retry == SVG_SHAPE_CONVERTION_ATTEMPTS - 1:
                    # set the invalid css shape to cache to avoid retry in the near future
                    await app.CACHE.set(svg_key, INVALID_SHAPE, ex=timedelta(hours=1))
                await asyncio.sleep(3)
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
                    "Failed to convert SVG to string shape by secondary llm. Will retry if haven't met the max try attempt after 3s.",
                    exc_info=True,
                    element_id=element_id,
                    retry=retry,
                )
                if retry == SVG_SHAPE_CONVERTION_ATTEMPTS - 1:
                    # set the invalid css shape to cache to avoid retry in the near future
                    await app.CACHE.set(svg_key, INVALID_SHAPE, ex=timedelta(weeks=1))
                await asyncio.sleep(3)
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
        await app.CACHE.set(svg_key, svg_shape)
        element["attributes"]["alt"] = svg_shape
    if "children" in element:
        del element["children"]
    return


async def _convert_css_shape_to_string(
    skyvern_frame: SkyvernFrame,
    element: Dict,
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
                    await app.CACHE.set(shape_key, css_shape)
                    break
                except LLMProviderError:
                    LOG.info(
                        "Failed to convert css shape due to llm error. Will retry if haven't met the max try attempt after 3s.",
                        exc_info=True,
                        element_id=element_id,
                        retry=retry,
                        key=shape_key,
                    )
                    if retry == CSS_SHAPE_CONVERTION_ATTEMPTS - 1:
                        # set the invalid css shape to cache to avoid retry in the near future
                        await app.CACHE.set(shape_key, INVALID_SHAPE, ex=timedelta(hours=1))
                    await asyncio.sleep(3)
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
                        "Failed to convert css shape to string shape by secondary llm. Will retry if haven't met the max try attempt after 3s.",
                        exc_info=True,
                        element_id=element_id,
                        retry=retry,
                        key=shape_key,
                    )
                    if retry == CSS_SHAPE_CONVERTION_ATTEMPTS - 1:
                        # set the invalid css shape to cache to avoid retry in the near future
                        await app.CACHE.set(shape_key, INVALID_SHAPE, ex=timedelta(weeks=1))
                    await asyncio.sleep(3)
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
        await app.CACHE.set(shape_key, css_shape)
        element["attributes"]["shape-description"] = css_shape
    return None


class AgentFunction:
    workflow_schedules_enabled: bool = False
    """Whether the workflow scheduler routes should serve traffic on this build.

    OSS Skyvern has no scheduling backend wired up by default, so the routes return 501.
    Cloud overrides this to True and provides the Temporal-backed implementations below.
    """

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

    async def should_shadow_extraction_cache_hit(self, task: Task) -> bool:
        """Cloud-overridable sample gate for extract-information shadow mode. OSS no-op."""
        return False

    async def lookup_cross_run_extraction_cache(
        self,
        workflow_permanent_id: str | None,
        cache_key: str,
    ) -> Any | None:
        """Cross-run (wpid-scoped) extraction-cache read. OSS no-op.

        Cloud overrides this to consult the Redis tier (SKY-8873). Returns the
        cached extraction value on a hit or None on a miss / error / disabled
        flag. Implementations MUST swallow backend errors and return None so
        the extract path always falls through to a fresh LLM call rather than
        failing loud.
        """
        return None

    async def store_cross_run_extraction_cache(
        self,
        workflow_permanent_id: str | None,
        cache_key: str,
        value: Any,
    ) -> None:
        """Cross-run (wpid-scoped) extraction-cache write. OSS no-op.

        Cloud overrides this to write to the Redis tier (SKY-8873) with a
        long TTL. Called after a fresh LLM extraction so subsequent runs of
        the same workflow against the same page content skip the LLM call.
        Implementations MUST swallow backend errors — write-path failures
        must never fail the user-visible request.
        """
        return None

    def build_workflow_schedule_id(self, workflow_schedule_id: str) -> str | None:
        """Return the backend-specific schedule id used by the execution engine.

        OSS has no execution backend, so this returns None and the schedule simply
        lives in the database. Cloud overrides this to derive a Temporal schedule id.
        """
        return None

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
    ) -> None:
        """Upsert a recurring schedule with the execution backend (e.g. Temporal).

        OSS base is a no-op so the route layer can stay backend-agnostic.
        Cloud overrides this to register the schedule with Temporal.
        Implementations must be idempotent.
        """
        return None

    async def set_workflow_schedule_enabled(self, backend_schedule_id: str, enabled: bool) -> None:
        """Pause or resume a schedule on the execution backend. OSS no-op."""
        return None

    async def delete_workflow_schedule(self, backend_schedule_id: str) -> None:
        """Delete a schedule from the execution backend. OSS no-op.

        Implementations must be idempotent — deleting an already-absent schedule
        should succeed silently rather than raising.
        """
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
        """Get a Google Sheets access token for the given credential.

        Returns None in OSS. Cloud override uses the OAuth service to
        decrypt the stored refresh token and exchange it for an access token.
        """
        return None

    async def get_google_workspace_credentials(
        self,
        organization_id: str,
        credential_id: str,
        required_scopes: list[str] | None = None,
    ) -> object | None:
        """OSS no-op; cloud override returns a refreshed google.oauth2.credentials.Credentials or None."""
        return None

    async def ensure_sheet_tab(
        self,
        *,
        access_token: str,
        spreadsheet_id: str,
        title: str,
    ) -> int | None:
        """Ensure a sheet tab with the given title exists in the spreadsheet.

        Returns the sheet_id of the newly created tab, or None if the caller
        should fall back to its own lookup (e.g. a concurrent creator won the
        race). OSS base is a no-op that returns None; cloud override calls the
        Sheets v4 batchUpdate addSheet endpoint.
        """
        return None

    async def google_sheets_values_get(
        self,
        *,
        access_token: str,
        spreadsheet_id: str,
        ranges: str,
        fields: str | None = None,
    ) -> dict[str, Any] | None:
        """Read ranges from a spreadsheet via spreadsheets.get. OSS no-op."""
        return None

    async def google_sheets_values_append(
        self,
        *,
        access_token: str,
        spreadsheet_id: str,
        range_: str,
        values: list[list[Any]],
    ) -> dict[str, Any] | None:
        """Append rows via spreadsheets.values.append. OSS no-op."""
        return None

    async def google_sheets_values_update(
        self,
        *,
        access_token: str,
        spreadsheet_id: str,
        range_: str,
        values: list[list[Any]],
    ) -> dict[str, Any] | None:
        """Update rows via spreadsheets.values.update. OSS no-op."""
        return None

    async def google_sheets_batch_update(
        self,
        *,
        access_token: str,
        spreadsheet_id: str,
        requests: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        """Apply a batchUpdate to a spreadsheet. OSS no-op."""
        return None

    async def google_sheets_get_sheet_id(
        self,
        *,
        access_token: str,
        spreadsheet_id: str,
        sheet_title: str,
    ) -> int | None:
        """Resolve a tab title to its numeric sheetId. OSS no-op."""
        return None

    async def google_sheets_get_grid_properties(
        self,
        *,
        access_token: str,
        spreadsheet_id: str,
        sheet_title: str,
    ) -> Any | None:
        """Return the named tab's grid dimensions (sheet_id, column_count, row_count). OSS no-op."""
        return None

    async def google_sheets_get_grid_properties_by_id(
        self,
        *,
        access_token: str,
        spreadsheet_id: str,
        sheet_id: int,
    ) -> Any | None:
        """Return grid dimensions for a sheet matched by numeric sheetId. OSS no-op."""
        return None

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

            for element in element_tree:
                queue.append(element)

            while queue:
                queue_ele = queue.pop(0)

                element_cnt += 1
                if element_cnt == MAX_ELEMENT_CNT:
                    LOG.warning(
                        f"Element reached max count {MAX_ELEMENT_CNT}, will stop converting svg and css element."
                    )
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
                    await _convert_css_shape_to_string(
                        skyvern_frame=skyvern_frame,
                        element=queue_ele,
                        task=task,
                        step=step,
                    )

                # TODO: we can come back to test removing the unique_id
                # from element attributes to make sure this won't increase hallucination
                # _remove_unique_id(queue_ele)
                if "children" in queue_ele:
                    queue.extend(queue_ele["children"])

            # SPEED OPTIMIZATION: Skip SVG conversion when using economy tree
            # Economy tree removes SVGs, so no point converting them
            #
            # COORDINATION: Use the same enable_speed_optimizations decision from context
            # that was set in agent.py BEFORE scraping. This ensures SVG conversion skip
            # is perfectly coordinated with economy tree selection.
            skip_svg_conversion = False
            if eligible_svgs and task and step:
                # Get the optimization decision from context (set before scraping in agent.py)
                current_context = skyvern_context.current()
                enable_speed_optimizations = current_context.enable_speed_optimizations if current_context else False

                if enable_speed_optimizations and step.retry_index == 0:
                    skip_svg_conversion = True
                    LOG.info(
                        "Speed optimization: Skipping SVG conversion (will use economy tree)",
                        step_order=step.order,
                        step_retry=step.retry_index,
                        workflow_run_id=task.workflow_run_id,
                        svg_count=len(eligible_svgs),
                    )

            # Convert all eligible SVGs in parallel (unless skipped by optimization)
            if eligible_svgs and not skip_svg_conversion:
                await asyncio.gather(*[_convert_svg_to_string(element, task, step) for element, frame in eligible_svgs])

            return element_tree

        return cleanup_element_tree_func

    async def validate_code_block(self, organization_id: str | None = None) -> None:
        if not settings.ENABLE_CODE_BLOCK:
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

    def get_copilot_security_rules(self) -> str:
        """Return security guardrails for the workflow copilot system prompt.

        Override in cloud to inject prompt injection defenses.
        OSS returns empty string (no hardening).
        """
        return ""

    def detect_ats_platform(self, url_or_domain: str | None) -> str | None:
        """Detect if a URL belongs to a known ATS platform.

        Returns a platform key string or None.
        Override in cloud to provide platform detection.
        """
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
