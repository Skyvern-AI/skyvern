from __future__ import annotations

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Callable, Iterator, TypedDict
from zoneinfo import ZoneInfo

import structlog

from skyvern.config import settings

if TYPE_CHECKING:
    from playwright.async_api import FileChooser, Frame, Page

    from skyvern.forge.sdk.db.enums import WorkflowRunTriggerType

    # Deferred import: skyvern_context.py sits below the service layer and
    # must not pull a service module at import time. String annotation below.
    from skyvern.services.script_reviewer_v3.budget import RunBudget

LOG = structlog.get_logger()

# Cap on entries kept in `recent_dialog_messages` so a chatty page (e.g. validation
# alerts firing on every keystroke) cannot inflate the next prompt unboundedly.
MAX_RECENT_DIALOG_MESSAGES = 5
# Per-message length cap so a single pathological alert (multi-KB page-stack
# trace, etc.) cannot dominate the prompt budget.
MAX_DIALOG_MESSAGE_CHARS = 500


class DialogEntry(TypedDict):
    type: str
    message: str
    count: int


class EnrichTreeMode(StrEnum):
    CONTROL = "control"
    ENRICHED_TREE = "enriched_tree"
    ENRICHED_TREE_NO_IMAGES = "enriched_tree_no_images"
    ENRICHED_TREE_NO_IMAGES_FALLBACK = "enriched_tree_no_images_fallback"


def parse_enrich_tree_mode(value: Any) -> EnrichTreeMode:
    if isinstance(value, EnrichTreeMode):
        return value
    if isinstance(value, str):
        try:
            return EnrichTreeMode(value)
        except ValueError:
            LOG.warning("Unknown enrich_tree mode value, defaulting to control", enrich_tree_mode=value)
    return EnrichTreeMode.CONTROL


@dataclass
class PendingFileChooserListener:
    page: Page
    file_paths: list[str] | str
    handler: Callable[[FileChooser], Any] | None = None
    triggered: bool = False

    def cleanup(self) -> None:
        if self.handler is not None:
            try:
                self.page.remove_listener("filechooser", self.handler)
            except Exception:
                LOG.debug("Failed to remove filechooser listener during cleanup", exc_info=True)
            self.handler = None


@dataclass
class SkyvernContext:
    request_id: str | None = None
    organization_id: str | None = None
    organization_name: str | None = None
    task_id: str | None = None
    step_id: str | None = None
    workflow_id: str | None = None
    workflow_permanent_id: str | None = None
    workflow_run_id: str | None = None
    root_workflow_run_id: str | None = None
    task_v2_id: str | None = None
    max_steps_override: int | None = None
    browser_session_id: str | None = None
    browser_runtime: str | None = None
    tz_info: ZoneInfo | None = None
    run_id: str | None = None
    copilot_session_id: str | None = None
    navigation_goal: str | None = None
    navigation_payload: dict[str, Any] | list | str | None = None
    download_suffix: str | None = None
    totp_codes: dict[str, str | None] = field(default_factory=dict)
    active_credential_parameter_key: str | None = None
    log: list[dict] = field(default_factory=list)
    hashed_href_map: dict[str, str] = field(default_factory=dict)
    downloaded_pdf_sources: set[str] = field(default_factory=set)
    refresh_working_page: bool = False
    frame_index_map: dict[Frame, int] = field(default_factory=dict)
    dropped_css_svg_element_map: dict[str, bool] = field(default_factory=dict)
    max_screenshot_scrolls: int | None = None
    browser_container_ip: str | None = None
    browser_container_task_arn: str | None = None
    feature_flag_entries: dict[str, bool | str | None] = field(default_factory=dict)

    # feature flags
    enable_page_ready_wait: bool = False
    use_prompt_caching: bool = False
    cached_static_prompt: str | None = None
    vertex_cache_name: str | None = None  # Vertex AI cache resource name for explicit caching
    vertex_cache_key: str | None = None  # Logical cache key (includes variant + llm key)
    vertex_cache_variant: str | None = None  # Variant identifier used when creating the cache
    prompt_caching_settings: dict[str, bool] | None = None
    use_artifact_bundling: bool = False
    # SKY-9718 Layer 1 — gates apply_lean_recipe in prompt_engine + agent.
    # PostHog flag ENABLE_LEAN_ELEMENT_TREE, evaluated once per run at scrape time
    # and read sync from prompt-build sites.
    enable_lean_element_tree: bool = False
    enrich_tree_mode: EnrichTreeMode = EnrichTreeMode.CONTROL
    step_retry_index: int = 0

    # Run-level SLIM_LLM_OUTPUT_PROMPTS assignment, resolved once by slim_llm_output.
    # The lock makes first-use resolution single-flight under parallel prompt builds.
    slim_output_variant_assigned: str | None = None
    slim_output_variant_resolved: bool = False
    slim_output_variant_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    # Trigger type of the enclosing workflow run (manual/api/scheduled/webhook).
    # Routed through SkyvernContext so non-API entry points (workers, scripts) can populate it
    # without taking a dependency on the public-API request shape.
    trigger_type: WorkflowRunTriggerType | None = None

    # Screenshot attribution: set by the agent before calling scrape so the
    # scraper can tag screenshot spans with the originating workflow phase
    # and whether the LLM will consume the screenshots.
    scrape_trigger: str | None = None
    scrape_screenshots_consumed: bool | None = None
    # When true, downstream LLM handler selection may swap the resolved handler to a
    # flex-tier router. Cloud sets this at run boot via a PostHog flag for non-UI runs;
    # OSS keeps it False because OSS has no flex routers registered.
    use_flex_llm_routing: bool = False

    # script run context
    code_version: int | None = None
    script_id: str | None = None
    script_revision_id: str | None = None
    action_order: int = 0
    prompt: str | None = None
    parent_workflow_run_block_id: str | None = None
    workflow_run_block_id: str | None = None
    loop_metadata: dict[str, Any] | None = None
    loop_internal_state: dict[str, Any] | None = None
    loop_output_values: list[list[dict[str, Any]]] | None = None
    script_run_parameters: dict[str, Any] = field(default_factory=dict)
    script_mode: bool = False
    is_static_script: bool = False
    sensitive_values: set[str] = field(default_factory=set)
    ai_mode_override: str | None = None
    script_llm_call_count: int = 0
    last_classify_result: str | None = None
    last_classify_meta: dict[str, Any] | None = None
    current_step_actions: list[dict[str, Any]] | None = None
    skip_complete_verification: bool = False

    # Set by ValidationBlock.execute() for the duration of the block so the prompt builder
    # can drop page DOM/URL/screenshots from the validation prompt. Restored after the block.
    validation_without_page_information: bool = False

    # v3 agentic reviewer — per-run cumulative budget. Initialized at workflow
    # run start for v3-cohort workflows; None for v2-cohort runs. SKY-7676.
    v3_run_budget: RunBudget | None = None

    # magic link handling
    # task_id is the key, page is the value
    # we only consider the page is a magic link page in the same task scope
    # for example, login block has a magic link page,
    # but it will only be considered as a magic link page in the login block scope
    # next blocks won't consider the page as a magic link page
    magic_link_pages: dict[str, Page] = field(default_factory=dict)

    # parallel verification optimization
    # stores pre-scraped data for next step to avoid re-scraping
    next_step_pre_scraped_data: dict[str, Any] | None = None
    speculative_plans: dict[str, Any] = field(default_factory=dict)

    """
    Example output value:
    {"loop_value": "str", "output_parameter": "the key of the parameter", "output_value": Any}
    """
    generate_script: bool = True
    action_ai_overrides: dict[str, dict[int, str]] = field(default_factory=dict)
    action_counters: dict[str, int] = field(default_factory=dict)

    # Track if script generation skipped any actions due to missing data (race condition)
    # Used to determine if finalize regeneration is needed at workflow completion
    script_gen_had_incomplete_actions: bool = False

    # Track task_ids where proactive captcha injection has already been attempted,
    # preventing repeated injection loops when the captcha solver succeeds but the page doesn't change
    proactive_captcha_task_ids: set[str] = field(default_factory=set)

    # Circuit breaker: consecutive captcha solve timeouts for this workflow run.
    # When this reaches the threshold, further captcha solve attempts are short-circuited.
    consecutive_captcha_timeouts: int = 0

    # Browser dialogs captured since the last agent prompt build, surfaced into the
    # next extract-action prompt so the LLM can react to validation rejections.
    recent_dialog_messages: list[DialogEntry] = field(default_factory=list)

    # Per-step prompt token breakdown (SKY-9718). Written by prompt-build sites
    # (prompt_engine.load_prompt_with_elements_tracked + the cached extract-action
    # path in agent.py); read + cleared by the LLM API handler when emitting the
    # "LLM API handler duration metrics" log so html_token_count / html_pct land
    # alongside the existing input_tokens / llm_cost on the same row.
    last_prompt_breakdown: dict[str, Any] | None = None

    # Deferred file chooser listener — survives across steps so a popup-intercepted upload
    # can be completed when a subsequent click triggers the actual file chooser.
    pending_file_chooser: PendingFileChooserListener | None = None

    def set_enrich_tree_mode(self, mode: Any) -> None:
        self.enrich_tree_mode = parse_enrich_tree_mode(mode)

    def enriched_tree_enabled(self) -> bool:
        return self.enrich_tree_mode != EnrichTreeMode.CONTROL

    def enrich_tree_fallback_active(self, *, retry_index: int | None = None) -> bool:
        effective_retry_index = self.step_retry_index if retry_index is None else retry_index
        return self.enrich_tree_mode == EnrichTreeMode.ENRICHED_TREE_NO_IMAGES_FALLBACK and effective_retry_index > 0

    def llm_screenshots_enabled_for_prompt(
        self,
        *,
        is_vision_fallback_prompt: bool = False,
        retry_index: int | None = None,
    ) -> bool:
        if is_vision_fallback_prompt:
            return True

        mode = self.enrich_tree_mode
        if mode in {EnrichTreeMode.CONTROL, EnrichTreeMode.ENRICHED_TREE}:
            return True
        if mode == EnrichTreeMode.ENRICHED_TREE_NO_IMAGES:
            return False

        effective_retry_index = self.step_retry_index if retry_index is None else retry_index
        return effective_retry_index > 0

    def cleanup_pending_file_chooser(self) -> None:
        if self.pending_file_chooser is not None:
            if not self.pending_file_chooser.triggered:
                LOG.warning("Cleaning up unconsumed pending file chooser listener")
            self.pending_file_chooser.cleanup()
            self.pending_file_chooser = None

    def __repr__(self) -> str:
        return f"SkyvernContext(request_id={self.request_id}, organization_id={self.organization_id}, task_id={self.task_id}, step_id={self.step_id}, workflow_id={self.workflow_id}, workflow_run_id={self.workflow_run_id}, task_v2_id={self.task_v2_id}, max_steps_override={self.max_steps_override}, run_id={self.run_id}, copilot_session_id={self.copilot_session_id})"

    def __str__(self) -> str:
        return self.__repr__()

    def pop_totp_code(self, task_id: str) -> None:
        if task_id in self.totp_codes:
            self.totp_codes.pop(task_id)

    def record_dialog_message(self, dialog_type: str, dialog_message: str) -> None:
        """Buffer a dialog with FIFO cap; identical entries bump a count instead of duplicating."""
        if not dialog_message:
            return
        if len(dialog_message) > MAX_DIALOG_MESSAGE_CHARS:
            dialog_message = dialog_message[:MAX_DIALOG_MESSAGE_CHARS] + "…"
        for entry in self.recent_dialog_messages:
            if entry["type"] == dialog_type and entry["message"] == dialog_message:
                entry["count"] += 1
                return
        self.recent_dialog_messages.append({"type": dialog_type, "message": dialog_message, "count": 1})
        if len(self.recent_dialog_messages) > MAX_RECENT_DIALOG_MESSAGES:
            del self.recent_dialog_messages[0]

    def format_recent_dialog_messages(self) -> str | None:
        """Render the buffered dialogs into prompt-ready text without clearing; None when empty."""
        if not self.recent_dialog_messages:
            return None
        lines: list[str] = []
        for entry in self.recent_dialog_messages:
            suffix = f" (x{entry['count']})" if entry["count"] > 1 else ""
            lines.append(f"[{entry['type']}{suffix}] {entry['message']}")
        return "\n".join(lines)

    def clear_recent_dialog_messages(self) -> None:
        """Drop the buffered dialogs once the prompt has consumed them."""
        self.recent_dialog_messages.clear()

    def add_magic_link_page(self, task_id: str, page: Page) -> None:
        self.magic_link_pages[task_id] = page

    def has_magic_link_page(self, task_id: str) -> bool:
        if task_id not in self.magic_link_pages:
            return False

        page = self.magic_link_pages[task_id]
        if page.is_closed():
            self.magic_link_pages.pop(task_id)
            return False
        return True

    def flush_feature_flags(self) -> None:
        if not self.feature_flag_entries:
            return

        has_workflow = bool(self.workflow_run_id)
        has_task = bool(self.task_id or self.task_v2_id or self.run_id)

        if not (has_workflow or has_task):
            LOG.debug(
                "Discarding feature flag entries for non-run context",
                count=len(self.feature_flag_entries),
            )
            self.feature_flag_entries.clear()
            return

        feature_resolutions = dict(sorted(self.feature_flag_entries.items()))
        log_fields: dict[str, Any] = {
            "organization_id": str(self.organization_id or ""),
            "feature_resolutions": feature_resolutions,
            "service_name": settings.OTEL_SERVICE_NAME,
        }
        if self.workflow_run_id:
            log_fields["workflow_run_id"] = str(self.workflow_run_id)
        if self.workflow_permanent_id:
            log_fields["workflow_permanent_id"] = str(self.workflow_permanent_id)
        if self.task_id:
            log_fields["task_id"] = str(self.task_id)
        if self.task_v2_id:
            log_fields["task_v2_id"] = str(self.task_v2_id)
        if self.run_id:
            log_fields["run_id"] = str(self.run_id)
        if self.browser_session_id:
            log_fields["browser_session_id"] = str(self.browser_session_id)
        if self.request_id:
            log_fields["request_id"] = str(self.request_id)

        event_name = "workflow_feature_flags" if has_workflow else "task_feature_flags"
        LOG.info(event_name, **log_fields)
        self.feature_flag_entries.clear()


_context: ContextVar[SkyvernContext | None] = ContextVar(
    "Global context",
    default=None,
)


def current() -> SkyvernContext | None:
    """
    Get the current context

    Returns:
        The current context, or None if there is none
    """
    return _context.get()


def ensure_context() -> SkyvernContext:
    """
    Get the current context, or raise an error if there is none

    Returns:
        The current context if there is one

    Raises:
        RuntimeError: If there is no current context
    """
    context = current()
    if context is None:
        raise RuntimeError("No skyvern context")
    return context


def set(context: SkyvernContext) -> None:
    """
    Set the current context

    Args:
        context: The context to set

    Returns:
        None
    """
    _context.set(context)


def replace(context: SkyvernContext) -> None:
    """
    Flush the current context summary, then replace it with a new context.

    Args:
        context: The context to set

    Returns:
        None
    """
    _cleanup_outgoing_context(current())
    _context.set(context)


def _cleanup_outgoing_context(context: SkyvernContext | None) -> None:
    if context is None:
        return
    if context.feature_flag_entries:
        context.flush_feature_flags()
    context.cleanup_pending_file_chooser()


def _restore(token: Token[SkyvernContext | None]) -> None:
    """
    Flush the current context summary and restore the previous context using a token.

    Args:
        token: ContextVar token returned by ContextVar.set()

    Returns:
        None
    """
    _cleanup_outgoing_context(current())
    _context.reset(token)


@contextmanager
def scoped(
    context: SkyvernContext,
    *,
    propagate_captcha_timeout: bool = False,
) -> Iterator[SkyvernContext]:
    """
    Temporarily scope the current context to a fresh child context.

    Args:
        context: The child context to set for the scope
        propagate_captcha_timeout: When True, copy the child's
            ``consecutive_captcha_timeouts`` back to the parent on exit.
            Only enable for scopes that represent real task executions
            (e.g. run_task_v2), not placeholder contexts.

    Yields:
        The child context
    """
    parent = _context.get() if propagate_captcha_timeout else None
    token = _context.set(context)
    try:
        yield context
    finally:
        if parent is not None:
            parent.consecutive_captcha_timeouts = context.consecutive_captcha_timeouts
        _restore(token)


def reset() -> None:
    """
    Reset the current context

    Returns:
        None
    """
    _cleanup_outgoing_context(current())
    _context.set(None)
