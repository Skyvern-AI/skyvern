from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import Any, Iterator
from zoneinfo import ZoneInfo

import structlog
from playwright.async_api import Frame, Page

from skyvern.config import settings

LOG = structlog.get_logger()


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
    tz_info: ZoneInfo | None = None
    run_id: str | None = None
    totp_codes: dict[str, str | None] = field(default_factory=dict)
    log: list[dict] = field(default_factory=list)
    hashed_href_map: dict[str, str] = field(default_factory=dict)
    refresh_working_page: bool = False
    frame_index_map: dict[Frame, int] = field(default_factory=dict)
    dropped_css_svg_element_map: dict[str, bool] = field(default_factory=dict)
    max_screenshot_scrolls: int | None = None
    browser_container_ip: str | None = None
    browser_container_task_arn: str | None = None
    feature_flag_entries: dict[str, bool | str | None] = field(default_factory=dict)

    # feature flags
    enable_page_ready_wait: bool = False
    enable_parse_select_in_extract: bool = False
    use_prompt_caching: bool = False
    cached_static_prompt: str | None = None
    vertex_cache_name: str | None = None  # Vertex AI cache resource name for explicit caching
    vertex_cache_key: str | None = None  # Logical cache key (includes variant + llm key)
    vertex_cache_variant: str | None = None  # Variant identifier used when creating the cache
    prompt_caching_settings: dict[str, bool] | None = None
    enable_speed_optimizations: bool = False
    use_artifact_bundling: bool = False

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
    loop_output_values: list[dict[str, Any]] | None = None
    script_run_parameters: dict[str, Any] = field(default_factory=dict)
    script_mode: bool = False
    is_static_script: bool = False
    sensitive_values: set[str] = field(default_factory=set)
    ai_mode_override: str | None = None
    script_llm_call_count: int = 0
    last_classify_result: str | None = None
    skip_complete_verification: bool = False

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

    def __repr__(self) -> str:
        return f"SkyvernContext(request_id={self.request_id}, organization_id={self.organization_id}, task_id={self.task_id}, step_id={self.step_id}, workflow_id={self.workflow_id}, workflow_run_id={self.workflow_run_id}, task_v2_id={self.task_v2_id}, max_steps_override={self.max_steps_override}, run_id={self.run_id})"

    def __str__(self) -> str:
        return self.__repr__()

    def pop_totp_code(self, task_id: str) -> None:
        if task_id in self.totp_codes:
            self.totp_codes.pop(task_id)

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
    _flush_feature_flags_if_needed(current())
    _context.set(context)


def _flush_feature_flags_if_needed(context: SkyvernContext | None) -> None:
    if context is not None and context.feature_flag_entries:
        context.flush_feature_flags()


def _restore(token: Token[SkyvernContext | None]) -> None:
    """
    Flush the current context summary and restore the previous context using a token.

    Args:
        token: ContextVar token returned by ContextVar.set()

    Returns:
        None
    """
    _flush_feature_flags_if_needed(current())
    _context.reset(token)


@contextmanager
def scoped(context: SkyvernContext) -> Iterator[SkyvernContext]:
    """
    Temporarily scope the current context to a fresh child context.

    Args:
        context: The child context to set for the scope

    Yields:
        The child context
    """
    token = _context.set(context)
    try:
        yield context
    finally:
        _restore(token)


def reset() -> None:
    """
    Reset the current context

    Returns:
        None
    """
    _flush_feature_flags_if_needed(current())
    _context.set(None)
