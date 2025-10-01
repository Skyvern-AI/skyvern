from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any
from zoneinfo import ZoneInfo

from playwright.async_api import Frame


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

    # feature flags
    enable_parse_select_in_extract: bool = False
    use_prompt_caching: bool = False
    cached_static_prompt: str | None = None

    # script run context
    script_id: str | None = None
    script_revision_id: str | None = None
    action_order: int = 0
    prompt: str | None = None
    parent_workflow_run_block_id: str | None = None
    loop_metadata: dict[str, Any] | None = None
    loop_output_values: list[dict[str, Any]] | None = None
    script_run_parameters: dict[str, Any] = field(default_factory=dict)
    """
    Example output value:
    {"loop_value": "str", "output_parameter": "the key of the parameter", "output_value": Any}
    """
    published_workflow_script_id: str | None = None

    def __repr__(self) -> str:
        return f"SkyvernContext(request_id={self.request_id}, organization_id={self.organization_id}, task_id={self.task_id}, step_id={self.step_id}, workflow_id={self.workflow_id}, workflow_run_id={self.workflow_run_id}, task_v2_id={self.task_v2_id}, max_steps_override={self.max_steps_override}, run_id={self.run_id})"

    def __str__(self) -> str:
        return self.__repr__()

    def pop_totp_code(self, task_id: str) -> None:
        if task_id in self.totp_codes:
            self.totp_codes.pop(task_id)


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


def reset() -> None:
    """
    Reset the current context

    Returns:
        None
    """
    _context.set(None)
