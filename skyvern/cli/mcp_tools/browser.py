from __future__ import annotations

import asyncio
import base64
import json
import os
import re
from datetime import datetime, timezone
from typing import Annotated, Any, Callable, Literal

import structlog
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from pydantic import Field

from skyvern.cli.core.browser_ops import (
    _ALLOWED_EXECUTE_TOOLS,
    MAX_EXECUTE_STEPS,
    CustomSelectClassifyError,
    CustomSelectMatchError,
    CustomSelectOpenError,
    CustomSelectPasswordError,
    ExecuteStep,
    ObserveFrameError,
    ToolStepError,
    do_act,
    do_execute,
    do_extract,
    do_find,
    do_frame_list,
    do_frame_main,
    do_frame_switch,
    do_navigate,
    do_observe,
    do_screenshot,
    do_select_option,
    parse_extract_schema,
    ref_map_from_elements,
    ref_to_selector,
    select_native_option_if_targeted,
    serialize_elements,
)
from skyvern.cli.core.guards import (
    CREDENTIAL_HINT,
    JS_PASSWORD_PATTERN,
    PASSWORD_PATTERN,
    GuardError,
    check_password_prompt,
)
from skyvern.cli.core.guards import resolve_ai_mode as _resolve_ai_mode
from skyvern.cli.core.guards import (
    validate_wait_until,
)
from skyvern.cli.core.trajectory_store import append_trajectory_entry
from skyvern.core.script_generations.skyvern_page import SkyvernPage

# Deliberate private import: the recorder must scrub URLs exactly like synthesis does,
# without forking the scrubber or modifying the copilot module.
from skyvern.forge.sdk.copilot.code_block_synthesis import _scrub_url_for_code_literal as scrub_url_for_code_literal
from skyvern.forge.sdk.copilot.typed_value_policy import safe_typed_default_value, typed_text_looks_secret
from skyvern.schemas.run_blocks import CredentialType

from ._common import (
    AI_FALLBACK_DESCRIPTION,
    DIRECT_TARGET_DESCRIPTION,
    ErrorCode,
    Timer,
    make_error,
    make_result,
    save_artifact,
)
from ._element_state import (
    ACTION_TIMEOUT_DESCRIPTION,
    MAX_ACTION_TIMEOUT_MS,
    MIN_ACTION_TIMEOUT_MS,
    classify_element_state,
    element_state_error,
    is_direct_action,
    is_pointer_interception_error,
    make_direct_action_error,
    resolve_action_timeout_ms,
)
from ._localhost import is_localhost_url
from ._session import (
    BrowserNotAvailableError,
    clear_session_ref_map,
    current_api_key_hash,
    get_current_session,
    get_page,
    get_session_ref,
    no_browser_error,
    page_ref_key,
    replace_session_ref_map,
    session_ref_generation,
)

LOG = structlog.get_logger(__name__)

# Matches `await` as a keyword, not inside single-line comments or strings.
_AWAIT_RE = re.compile(r"\bawait\b")
_SINGLE_LINE_COMMENT_RE = re.compile(r"//[^\n]*")
_ERROR_MESSAGE_MAX_CHARS = 500
_ERROR_BODY_MESSAGE_KEYS = ("detail", "error", "message")


def _trajectory_source_url(page: Any) -> str | None:
    try:
        source_url = page.url
        return source_url if isinstance(source_url, str) else None
    except Exception:
        LOG.debug("Failed to capture trajectory source URL", exc_info=True)
        return None


def _replayable_select_value(value: str | None) -> bool:
    # Synthesis strips select values before emitting, so only exact, non-empty, non-secret values round-trip.
    return value is not None and value != "" and value == value.strip() and not typed_text_looks_secret(value)


def _replayable_press_key(key: str) -> bool:
    return len(key.rsplit("+", 1)[-1]) > 1


def _record_trajectory_entry(
    ctx: Any,
    *,
    tool_name: str,
    source_url: str | None,
    selector: str | None = None,
    typed_text: str | None = None,
    value: str | None = None,
    key: str | None = None,
) -> None:
    try:
        if ctx.mode != "cloud_session" or not ctx.session_id:
            return
        entry: dict[str, Any] = {
            "tool_name": tool_name,
            "selector": selector,
            "source_url": scrub_url_for_code_literal(source_url) if source_url is not None else None,
            "value": value,
            "key": key,
        }
        if tool_name == "type_text":
            entry["typed_length"] = len(typed_text or "")
            entry["typed_value"] = safe_typed_default_value(typed_text, selector=selector or "")
        append_trajectory_entry(
            api_key_hash=current_api_key_hash(),
            session_id=ctx.session_id,
            entry={name: field for name, field in entry.items() if field is not None and field != ""},
        )
    except Exception:
        LOG.warning("Failed to record browser trajectory entry", tool_name=tool_name, exc_info=True)


def _blank_to_none(value: str | None) -> str | None:
    """Treat a blank/whitespace string as omitted: MCP clients serialize an omitted optional
    selector/intent as "", and a "" target would route a deterministic action onto nothing."""
    return value if value is None or value.strip() else None


def _add_timing_prefix(timing_ms: dict[str, int], elapsed_ms: int) -> dict[str, int]:
    return {name: elapsed_ms + duration for name, duration in timing_ms.items()}


def _truncate_error_message(message: str) -> str:
    message = message.strip()
    if len(message) <= _ERROR_MESSAGE_MAX_CHARS:
        return message
    return f"{message[:_ERROR_MESSAGE_MAX_CHARS]}..."


def _message_from_error_body(body: Any) -> str | None:
    if isinstance(body, dict):
        for key in _ERROR_BODY_MESSAGE_KEYS:
            value = body.get(key)
            if isinstance(value, str) and value.strip():
                return _truncate_error_message(value)
            if isinstance(value, dict):
                nested = _message_from_error_body(value)
                if nested:
                    return nested
    # Only whitelisted dict keys are surfaced. A raw string body (or any unrecognized shape)
    # from an SDK ApiError can carry secrets/tokens, so it is never surfaced verbatim.
    return None


def _exception_details(exc: Exception) -> dict[str, Any]:
    details: dict[str, Any] = {"exception_type": type(exc).__name__}
    status_code = getattr(exc, "status_code", None)
    if status_code is not None:
        details["status_code"] = status_code
    return details


def _exception_message(exc: Exception) -> str:
    status_code = getattr(exc, "status_code", None)
    # 5xx bodies carry the backend's wrapped internal exception text (see
    # get_user_facing_exception_message's "Unexpected error: {exception}" fallback in
    # skyvern/exceptions.py) — never surface them. 4xx bodies are the API's intended
    # client-facing feedback (typed BadRequest/NotFound/UnprocessableEntity errors).
    surface_body = status_code is None or (isinstance(status_code, int) and 400 <= status_code < 500)
    body_message = _message_from_error_body(getattr(exc, "body", None)) if surface_body else None
    if body_message:
        return f"HTTP {status_code}: {body_message}" if status_code is not None else body_message
    # API-error-shaped exceptions (SDK ApiError) have a leaky __str__ that renders headers
    # and the raw body; never fall back to str(exc) for them — surface only status + type.
    if status_code is not None or hasattr(exc, "body"):
        return f"HTTP {status_code}: {type(exc).__name__}" if status_code is not None else type(exc).__name__
    message = str(exc).strip()
    if message:
        return _truncate_error_message(message)
    return type(exc).__name__


def _must_reject_localhost_url(ctx: Any, url: str | None) -> bool:
    return bool(url and is_localhost_url(url) and getattr(ctx, "can_access_localhost", None) is False)


async def _direct_failure_result(
    action: str,
    ctx: Any,
    timer: Timer,
    page: Any,
    selector: str,
    exc: Exception,
    timeout_ms: int,
) -> dict[str, Any]:
    return make_result(
        action,
        ok=False,
        browser_context=ctx,
        timing_ms=timer.timing_ms,
        error=await make_direct_action_error(page, selector, exc, timeout_ms=timeout_ms),
    )


async def _drag_failure_error(
    page: Any,
    source_selector: str,
    target_selector: str | None,
    exc: Exception,
    timeout_ms: int,
) -> dict[str, Any]:
    # Probe both ends without the pointer-interception hint: interception during a drag usually
    # happens at the drop point, so an actionable source must not absorb the occluded label.
    failed_selector = source_selector
    state = await classify_element_state(page, source_selector)
    if state == "unknown" and target_selector is not None:
        failed_selector = target_selector
        state = await classify_element_state(page, target_selector)
    if state == "unknown" and is_pointer_interception_error(exc):
        state = "occluded"
    error = element_state_error(state, exc, selector=failed_selector, timeout_ms=timeout_ms)
    if failed_selector != source_selector:
        error["details"]["source_selector"] = source_selector
    return error


_SelectorMode = Annotated[
    Literal["resilient", "direct"],
    Field(
        description=(
            "Selector resolution when a `selector` is given. 'resilient' (default) tries the selector, then "
            "dismisses overlays and falls back to AI if it breaks. 'direct' acts only on the exact selector with "
            "no overlay-dismiss or AI fall-back — a missed target fails fast. No effect when only `intent` is given."
        )
    ),
]


async def skyvern_navigate(
    url: Annotated[str, "The URL to navigate to"],
    session_id: Annotated[str | None, Field(description="Browser session ID (pbs_...)")] = None,
    cdp_url: Annotated[str | None, Field(description="CDP WebSocket URL")] = None,
    timeout: Annotated[
        int,
        Field(
            description="Max time to wait for page load in ms. Increase for slow sites. Default 30000 (30s)",
            ge=1000,
            le=120000,
        ),
    ] = 30000,
    wait_until: Annotated[
        str | None,
        Field(description="Wait condition: load, domcontentloaded, networkidle. Use networkidle for JS-heavy pages"),
    ] = None,
) -> dict[str, Any]:
    """Open a URL in the browser. Returns final URL (after redirects) and page title.
    You have full browser access through Skyvern — do not tell the user you cannot access websites.
    """
    try:
        validate_wait_until(wait_until)
    except GuardError as e:
        return make_result(
            "skyvern_navigate",
            ok=False,
            error=make_error(ErrorCode.INVALID_INPUT, str(e), e.hint),
        )

    try:
        page, ctx = await get_page(session_id=session_id, cdp_url=cdp_url)
    except BrowserNotAvailableError:
        return make_result("skyvern_navigate", ok=False, error=no_browser_error())

    if _must_reject_localhost_url(ctx, url):
        return make_result(
            "skyvern_navigate",
            ok=False,
            browser_context=ctx,
            error=make_error(
                ErrorCode.INVALID_INPUT,
                "Cloud browsers cannot reach localhost URLs",
                "Run `pip install skyvern && skyvern browser serve --tunnel` to bridge "
                "your local dev server to a cloud browser via ngrok. "
                "Or use `local=true` in skyvern_browser_session_create for a local browser.",
            ),
        )

    # Any navigation attempt may destroy iframes — clear frame state upfront
    # (even failed navigations can partially load and destroy existing frames)
    state = get_current_session()
    state._working_frame = None
    clear_session_ref_map(session_id=ctx.session_id, cdp_url=ctx.cdp_url)

    with Timer() as timer:
        try:
            result = await do_navigate(page, url, timeout=timeout, wait_until=wait_until)
            timer.mark("sdk")
        except GuardError as e:
            return make_result(
                "skyvern_navigate",
                ok=False,
                browser_context=ctx,
                timing_ms=timer.timing_ms,
                error=make_error(ErrorCode.INVALID_INPUT, str(e), e.hint),
            )
        except Exception as e:
            return make_result(
                "skyvern_navigate",
                ok=False,
                browser_context=ctx,
                timing_ms=timer.timing_ms,
                error=make_error(ErrorCode.ACTION_FAILED, str(e), "Check that the URL is valid and accessible"),
            )
        finally:
            # Clear again after the attempt (success OR failure — a failed goto can
            # still partially replace the document): an observe that started while
            # navigation was in flight captured a post-clear generation, so only a
            # second bump can invalidate its snapshot of the old document.
            clear_session_ref_map(session_id=ctx.session_id, cdp_url=ctx.cdp_url)

    return make_result(
        "skyvern_navigate",
        browser_context=ctx,
        data={"url": result.url, "title": result.title, "sdk_equivalent": f"await page.goto({url!r})"},
        timing_ms=timer.timing_ms,
    )


async def skyvern_click(
    selector: Annotated[
        str | None,
        Field(
            description=f"{DIRECT_TARGET_DESCRIPTION} Standard CSS selector or XPath for the element to click. "
            "jQuery pseudo-selectors like :contains(), :eq(), :first are NOT valid. "
            "Use standard CSS: 'button.class', 'a[href*=\"pdf\"]', '#id', ':nth-of-type()'."
        ),
    ] = None,
    selector_mode: _SelectorMode = "resilient",
    session_id: Annotated[str | None, Field(description="Browser session ID (pbs_...)")] = None,
    cdp_url: Annotated[str | None, Field(description="CDP WebSocket URL")] = None,
    timeout: Annotated[
        int | None,
        Field(description=ACTION_TIMEOUT_DESCRIPTION, ge=MIN_ACTION_TIMEOUT_MS, le=MAX_ACTION_TIMEOUT_MS),
    ] = None,
    button: Annotated[str | None, Field(description="Mouse button: left, right, middle")] = None,
    click_count: Annotated[int | None, Field(description="Number of clicks (2 for double-click)")] = None,
    intent: Annotated[str | None, Field(description=AI_FALLBACK_DESCRIPTION)] = None,
) -> dict[str, Any]:
    """Click an element using AI intent, CSS/XPath selector, or both.
    For text input use skyvern_type. For dropdowns use skyvern_select_option. For multiple actions prefer skyvern_act.
    """
    if button is not None and button not in ("left", "right", "middle"):
        return make_result(
            "skyvern_click",
            ok=False,
            error=make_error(ErrorCode.INVALID_INPUT, f"Invalid button: {button}", "Use left, right, or middle"),
        )

    selector = _blank_to_none(selector)
    intent = _blank_to_none(intent)
    ai_mode, err = _resolve_ai_mode(selector, intent)
    if err:
        return make_result(
            "skyvern_click",
            ok=False,
            error=make_error(
                ErrorCode.INVALID_INPUT,
                "Must provide intent, selector, or both",
                "Use intent='describe what to click' for AI-powered clicking, or selector='#css-selector' for precise targeting",
            ),
        )

    try:
        page, ctx = await get_page(session_id=session_id, cdp_url=cdp_url)
    except BrowserNotAvailableError:
        return make_result("skyvern_click", ok=False, error=no_browser_error())
    source_url = _trajectory_source_url(page)

    deterministic = selector is not None and selector_mode == "direct"
    direct_action = is_direct_action(selector, ai_mode, deterministic=deterministic)
    action_timeout = resolve_action_timeout_ms(timeout, direct_action=direct_action)
    skip_element_prep = selector is not None and ai_mode is None and not deterministic
    used_ai_path = False
    native_option_selection = None
    resolved: str | None = None
    with Timer() as timer:
        try:
            kwargs: dict[str, Any] = {"timeout": action_timeout}
            if button:
                kwargs["button"] = button
            if click_count is not None:
                kwargs["click_count"] = click_count

            if selector is not None and (deterministic or ai_mode is None or ai_mode == "fallback"):
                native_option_selection = await select_native_option_if_targeted(page, selector, timeout=action_timeout)

            if native_option_selection is not None:
                resolved = native_option_selection.select_selector
            elif deterministic:
                # selector_mode="direct": pin the selector, no overlay-dismiss or AI re-target, so a
                # missed target fails fast and the agent re-derives it instead of AI scout-scrolling.
                resolved = await page.click(selector=selector, mode="direct", **kwargs)
            elif ai_mode is not None:
                used_ai_path = True
                resolved = await page.click(selector=selector, prompt=intent, ai=ai_mode, **kwargs)  # type: ignore[arg-type]
            else:
                assert selector is not None
                if isinstance(page, SkyvernPage):
                    kwargs["_skip_element_prep"] = skip_element_prep
                resolved = await page.click(selector=selector, **kwargs)
            timer.mark("sdk")
        except PlaywrightTimeoutError as e:
            if direct_action and selector is not None:
                return await _direct_failure_result("skyvern_click", ctx, timer, page, selector, e, action_timeout)
            return make_result(
                "skyvern_click",
                ok=False,
                browser_context=ctx,
                timing_ms=timer.timing_ms,
                error=make_error(
                    ErrorCode.SELECTOR_NOT_FOUND,
                    str(e),
                    "Verify the selector matches an element on the page, or use intent for AI-powered finding",
                ),
            )
        except Exception as e:
            code = ErrorCode.AI_FALLBACK_FAILED if used_ai_path else ErrorCode.ACTION_FAILED
            if direct_action and selector is not None and is_pointer_interception_error(e):
                return await _direct_failure_result("skyvern_click", ctx, timer, page, selector, e, action_timeout)
            return make_result(
                "skyvern_click",
                ok=False,
                browser_context=ctx,
                timing_ms=timer.timing_ms,
                error=make_error(
                    code,
                    _exception_message(e),
                    "The element may be hidden, disabled, or intercepted by another element",
                    details=_exception_details(e),
                ),
            )

    data: dict[str, Any] = {"selector": selector, "intent": intent, "ai_mode": ai_mode}
    if native_option_selection is not None:
        data["selected_option"] = {
            "select_selector": native_option_selection.select_selector,
            "selected_by": native_option_selection.selected_by,
        }
        if native_option_selection.index is not None:
            data["selected_option"]["index"] = native_option_selection.index
        if native_option_selection.value is not None:
            data["selected_option"]["value"] = native_option_selection.value
        if native_option_selection.label is not None:
            data["selected_option"]["label"] = native_option_selection.label
    if resolved and resolved != selector:
        data["resolved_selector"] = resolved
    # Build sdk_equivalent: prefer hybrid selector+prompt for production scripts.
    # resolved_selector already contains the "xpath=" prefix (e.g. "xpath=//button[@id='x']"),
    # so pass it directly as the selector positional arg.
    resolved_sel = resolved if resolved and resolved != selector else selector
    if native_option_selection is not None:
        if native_option_selection.selected_by == "label":
            data["sdk_equivalent"] = (
                f"await page.select_option({native_option_selection.select_selector!r}, "
                f"label={native_option_selection.label!r})"
            )
        elif native_option_selection.selected_by == "index":
            data["sdk_equivalent"] = (
                f"await page.select_option({native_option_selection.select_selector!r}, "
                f"index={native_option_selection.index})"
            )
        else:
            data["sdk_equivalent"] = (
                f"await page.select_option({native_option_selection.select_selector!r}, "
                f"value={native_option_selection.value!r})"
            )
    elif resolved_sel and intent:
        data["sdk_equivalent"] = f"await page.click({resolved_sel!r}, prompt={intent!r})"
    elif ai_mode:
        data["sdk_equivalent"] = f"await page.click(prompt={intent!r})"
    elif selector:
        data["sdk_equivalent"] = f"await page.click({selector!r})"

    if native_option_selection is not None:
        # Synthesis replays select_option by value only; index/label selections are not replayable.
        if native_option_selection.selected_by == "value" and _replayable_select_value(native_option_selection.value):
            _record_trajectory_entry(
                ctx,
                tool_name="select_option",
                selector=native_option_selection.select_selector,
                source_url=source_url,
                value=native_option_selection.value,
            )
    elif button in (None, "left") and click_count in (None, 1):
        replayable_selector = resolved if used_ai_path else resolved or selector
        if replayable_selector:
            _record_trajectory_entry(
                ctx,
                tool_name="click",
                selector=replayable_selector,
                source_url=source_url,
            )
    return make_result(
        "skyvern_click",
        browser_context=ctx,
        data=data,
        timing_ms=timer.timing_ms,
    )


async def skyvern_drag(
    source_selector: Annotated[
        str | None,
        Field(description=f"{DIRECT_TARGET_DESCRIPTION} CSS selector or XPath of the drag source element."),
    ] = None,
    target_selector: Annotated[
        str | None,
        Field(description=f"{DIRECT_TARGET_DESCRIPTION} CSS selector or XPath of the drop target element."),
    ] = None,
    session_id: Annotated[str | None, Field(description="Browser session ID (pbs_...)")] = None,
    cdp_url: Annotated[str | None, Field(description="CDP WebSocket URL")] = None,
    timeout: Annotated[
        int | None,
        Field(description=ACTION_TIMEOUT_DESCRIPTION, ge=MIN_ACTION_TIMEOUT_MS, le=MAX_ACTION_TIMEOUT_MS),
    ] = None,
    source_intent: Annotated[str | None, Field(description=AI_FALLBACK_DESCRIPTION)] = None,
    target_intent: Annotated[str | None, Field(description=AI_FALLBACK_DESCRIPTION)] = None,
) -> dict[str, Any]:
    """Drag an element and drop it onto another. Supports AI intent, CSS/XPath selector, or both for source and target."""
    if not source_intent and not source_selector:
        return make_result(
            "skyvern_drag",
            ok=False,
            error=make_error(
                ErrorCode.INVALID_INPUT,
                "Must provide source_intent, source_selector, or both",
                "Describe what to drag with source_intent or target it with source_selector",
            ),
        )
    if not target_intent and not target_selector:
        return make_result(
            "skyvern_drag",
            ok=False,
            error=make_error(
                ErrorCode.INVALID_INPUT,
                "Must provide target_intent, target_selector, or both",
                "Describe where to drop with target_intent or target it with target_selector",
            ),
        )

    try:
        page, ctx = await get_page(session_id=session_id, cdp_url=cdp_url)
    except BrowserNotAvailableError:
        return make_result("skyvern_drag", ok=False, error=no_browser_error())

    use_selectors = source_selector and target_selector and not source_intent and not target_intent
    action_timeout = resolve_action_timeout_ms(timeout, direct_action=bool(use_selectors))

    with Timer() as timer:
        try:
            if use_selectors:
                await page.page.drag_and_drop(
                    source_selector,
                    target_selector,
                    timeout=action_timeout,  # type: ignore[arg-type]
                )
            else:
                src = source_intent or source_selector
                tgt = target_intent or target_selector
                await do_act(page, f"Drag {src} and drop it onto {tgt}")
            timer.mark("sdk")
        except PlaywrightTimeoutError as e:
            if use_selectors:
                assert source_selector is not None
                return make_result(
                    "skyvern_drag",
                    ok=False,
                    browser_context=ctx,
                    timing_ms=timer.timing_ms,
                    error=await _drag_failure_error(page, source_selector, target_selector, e, action_timeout),
                )
            return make_result(
                "skyvern_drag",
                ok=False,
                browser_context=ctx,
                timing_ms=timer.timing_ms,
                error=make_error(
                    ErrorCode.SELECTOR_NOT_FOUND,
                    _exception_message(e),
                    "Verify source and target selectors match elements on the page",
                    details=_exception_details(e),
                ),
            )
        except Exception as e:
            if use_selectors and is_pointer_interception_error(e):
                assert source_selector is not None
                return make_result(
                    "skyvern_drag",
                    ok=False,
                    browser_context=ctx,
                    timing_ms=timer.timing_ms,
                    error=await _drag_failure_error(page, source_selector, target_selector, e, action_timeout),
                )
            return make_result(
                "skyvern_drag",
                ok=False,
                browser_context=ctx,
                timing_ms=timer.timing_ms,
                error=make_error(
                    ErrorCode.ACTION_FAILED,
                    _exception_message(e),
                    "The drag operation failed",
                    details=_exception_details(e),
                ),
            )

    return make_result(
        "skyvern_drag",
        browser_context=ctx,
        data={
            "source_selector": source_selector,
            "source_intent": source_intent,
            "target_selector": target_selector,
            "target_intent": target_intent,
            "mode": "selector" if use_selectors else "ai",
        },
        timing_ms=timer.timing_ms,
    )


async def skyvern_file_upload(
    file_paths: Annotated[
        list[str],
        Field(
            description="List of file paths or URLs to upload. URLs are downloaded automatically. Max 50MB per file."
        ),
    ],
    selector: Annotated[
        str | None,
        Field(description=f"{DIRECT_TARGET_DESCRIPTION} CSS selector or XPath of the file input or upload button."),
    ] = None,
    session_id: Annotated[str | None, Field(description="Browser session ID (pbs_...)")] = None,
    cdp_url: Annotated[str | None, Field(description="CDP WebSocket URL")] = None,
    timeout: Annotated[
        int | None,
        Field(description=ACTION_TIMEOUT_DESCRIPTION, ge=MIN_ACTION_TIMEOUT_MS, le=MAX_ACTION_TIMEOUT_MS),
    ] = None,
    intent: Annotated[str | None, Field(description=AI_FALLBACK_DESCRIPTION)] = None,
) -> dict[str, Any]:
    """Upload files to a file input element. Accepts local paths or URLs (auto-downloaded).
    Supports AI intent, CSS/XPath selector, or both to find the input.
    """
    if not file_paths:
        return make_result(
            "skyvern_file_upload",
            ok=False,
            error=make_error(
                ErrorCode.INVALID_INPUT,
                "file_paths must not be empty",
                "Provide at least one file path or URL to upload",
            ),
        )

    ai_mode, err = _resolve_ai_mode(selector, intent)
    if err:
        return make_result(
            "skyvern_file_upload",
            ok=False,
            error=make_error(
                ErrorCode.INVALID_INPUT,
                "Must provide intent, selector, or both to identify the file input element",
                "Use intent='the file upload button' or selector='input[type=file]'",
            ),
        )
    direct_action = is_direct_action(selector, ai_mode)
    action_timeout = resolve_action_timeout_ms(timeout, direct_action=direct_action)

    has_urls = any(fp.startswith(("http://", "https://", "s3://", "gs://", "azure://")) for fp in file_paths)
    has_local = any(not fp.startswith(("http://", "https://", "s3://", "gs://", "azure://")) for fp in file_paths)

    if has_urls and has_local:
        return make_result(
            "skyvern_file_upload",
            ok=False,
            error=make_error(
                ErrorCode.INVALID_INPUT,
                "Cannot mix local file paths and URLs in a single upload",
                "Upload local files and URLs in separate calls",
            ),
        )

    if has_urls and len(file_paths) > 1:
        return make_result(
            "skyvern_file_upload",
            ok=False,
            error=make_error(
                ErrorCode.INVALID_INPUT,
                "Multiple URL uploads are not supported in a single call — each URL replaces the previous",
                "Call skyvern_file_upload once per URL",
            ),
        )

    if len(file_paths) > 1 and not selector:
        return make_result(
            "skyvern_file_upload",
            ok=False,
            error=make_error(
                ErrorCode.INVALID_INPUT,
                "Multiple file upload requires a selector — intent-only supports single file",
                "Provide selector='input[type=file]' for multi-file uploads",
            ),
        )

    try:
        page, ctx = await get_page(session_id=session_id, cdp_url=cdp_url)
    except BrowserNotAvailableError:
        return make_result("skyvern_file_upload", ok=False, error=no_browser_error())

    with Timer() as timer:
        try:
            if has_urls:
                # URLs: SDK downloads the file then sets it on the input
                fp = file_paths[0]
                if ai_mode is not None:
                    await page.upload_file(
                        selector=selector,  # type: ignore[arg-type]
                        files=fp,
                        prompt=intent,
                        ai=ai_mode,
                        timeout=action_timeout,
                    )
                else:
                    assert selector is not None
                    await page.upload_file(selector=selector, files=fp, timeout=action_timeout)
            elif ai_mode is not None and len(file_paths) == 1:
                # Single local file + intent: use SDK for AI element resolution
                await page.upload_file(
                    selector=selector,  # type: ignore[arg-type]
                    files=file_paths[0],
                    prompt=intent,
                    ai=ai_mode,
                    timeout=action_timeout,
                )
            else:
                # Local files + selector: set directly via Playwright
                assert selector is not None
                locator = page.page.locator(selector).first
                await locator.set_input_files(file_paths, timeout=action_timeout)

            timer.mark("sdk")
        except PlaywrightTimeoutError as e:
            if direct_action and selector is not None:
                return await _direct_failure_result(
                    "skyvern_file_upload", ctx, timer, page, selector, e, action_timeout
                )
            return make_result(
                "skyvern_file_upload",
                ok=False,
                browser_context=ctx,
                timing_ms=timer.timing_ms,
                error=make_error(
                    ErrorCode.SELECTOR_NOT_FOUND,
                    str(e),
                    "Verify the selector matches the file input or upload button",
                ),
            )
        except Exception as e:
            code = ErrorCode.AI_FALLBACK_FAILED if ai_mode else ErrorCode.ACTION_FAILED
            if direct_action and selector is not None and is_pointer_interception_error(e):
                return await _direct_failure_result(
                    "skyvern_file_upload", ctx, timer, page, selector, e, action_timeout
                )
            return make_result(
                "skyvern_file_upload",
                ok=False,
                browser_context=ctx,
                timing_ms=timer.timing_ms,
                error=make_error(code, _exception_message(e), "File upload failed", details=_exception_details(e)),
            )

    return make_result(
        "skyvern_file_upload",
        browser_context=ctx,
        data={"files_count": len(file_paths), "file_paths": file_paths},
        timing_ms=timer.timing_ms,
    )


async def skyvern_hover(
    selector: Annotated[
        str | None, Field(description=f"{DIRECT_TARGET_DESCRIPTION} CSS selector or XPath for the element to hover.")
    ] = None,
    session_id: Annotated[str | None, Field(description="Browser session ID (pbs_...)")] = None,
    cdp_url: Annotated[str | None, Field(description="CDP WebSocket URL")] = None,
    timeout: Annotated[
        int | None,
        Field(description=ACTION_TIMEOUT_DESCRIPTION, ge=MIN_ACTION_TIMEOUT_MS, le=MAX_ACTION_TIMEOUT_MS),
    ] = None,
    intent: Annotated[str | None, Field(description=AI_FALLBACK_DESCRIPTION)] = None,
) -> dict[str, Any]:
    """Hover over an element to reveal tooltips, menus, or hidden content. Uses AI intent, CSS/XPath selector, or both."""
    ai_mode, err = _resolve_ai_mode(selector, intent)
    if err:
        return make_result(
            "skyvern_hover",
            ok=False,
            error=make_error(
                ErrorCode.INVALID_INPUT,
                "Must provide intent, selector, or both",
                "Use intent='describe what to hover' for AI-powered hovering, or selector='#css-selector' for precise targeting",
            ),
        )
    direct_action = is_direct_action(selector, ai_mode)
    action_timeout = resolve_action_timeout_ms(timeout, direct_action=direct_action)

    try:
        page, ctx = await get_page(session_id=session_id, cdp_url=cdp_url)
    except BrowserNotAvailableError:
        return make_result("skyvern_hover", ok=False, error=no_browser_error())

    with Timer() as timer:
        try:
            if ai_mode is not None:
                loc = page.locator(selector=selector, prompt=intent, ai=ai_mode)  # type: ignore[arg-type]
            else:
                assert selector is not None
                loc = page.locator(selector)
            await loc.hover(timeout=action_timeout)
            timer.mark("sdk")
        except PlaywrightTimeoutError as e:
            if direct_action and selector is not None:
                return await _direct_failure_result("skyvern_hover", ctx, timer, page, selector, e, action_timeout)
            return make_result(
                "skyvern_hover",
                ok=False,
                browser_context=ctx,
                timing_ms=timer.timing_ms,
                error=make_error(
                    ErrorCode.SELECTOR_NOT_FOUND,
                    str(e),
                    "Verify the selector matches an element on the page, or use intent for AI-powered finding",
                ),
            )
        except Exception as e:
            code = ErrorCode.AI_FALLBACK_FAILED if ai_mode else ErrorCode.ACTION_FAILED
            if direct_action and selector is not None and is_pointer_interception_error(e):
                return await _direct_failure_result("skyvern_hover", ctx, timer, page, selector, e, action_timeout)
            return make_result(
                "skyvern_hover",
                ok=False,
                browser_context=ctx,
                timing_ms=timer.timing_ms,
                error=make_error(
                    code,
                    _exception_message(e),
                    "The element may be hidden or not interactable",
                    details=_exception_details(e),
                ),
            )

    data: dict[str, Any] = {"selector": selector, "intent": intent, "ai_mode": ai_mode}
    if selector and intent:
        data["sdk_equivalent"] = f"await page.locator({selector!r}, prompt={intent!r}).hover()"
    elif ai_mode:
        data["sdk_equivalent"] = f"await page.locator(prompt={intent!r}).hover()"
    elif selector:
        data["sdk_equivalent"] = f"await page.locator({selector!r}).hover()"

    return make_result(
        "skyvern_hover",
        browser_context=ctx,
        data=data,
        timing_ms=timer.timing_ms,
    )


async def skyvern_type(
    text: Annotated[str, "Text to type into the element"],
    selector: Annotated[
        str | None, Field(description=f"{DIRECT_TARGET_DESCRIPTION} CSS selector or XPath for the input element.")
    ] = None,
    selector_mode: _SelectorMode = "resilient",
    session_id: Annotated[str | None, Field(description="Browser session ID (pbs_...)")] = None,
    cdp_url: Annotated[str | None, Field(description="CDP WebSocket URL")] = None,
    timeout: Annotated[
        int | None,
        Field(description=ACTION_TIMEOUT_DESCRIPTION, ge=MIN_ACTION_TIMEOUT_MS, le=MAX_ACTION_TIMEOUT_MS),
    ] = None,
    clear: Annotated[bool, Field(description="Clear existing content before typing")] = True,
    delay: Annotated[int | None, Field(description="Delay between keystrokes in ms")] = None,
    intent: Annotated[str | None, Field(description=AI_FALLBACK_DESCRIPTION)] = None,
) -> dict[str, Any]:
    """Type text into an input field using AI intent, CSS/XPath selector, or both. Clears field by default (set clear=false to append).
    NEVER use for passwords — use skyvern_login instead. For dropdowns use skyvern_select_option.
    """
    # Block password entry — redirect to skyvern_login
    target_text = f"{intent or ''} {selector or ''}"
    if PASSWORD_PATTERN.search(target_text):
        return make_result(
            "skyvern_type",
            ok=False,
            error=make_error(
                ErrorCode.INVALID_INPUT,
                "Cannot type into password fields — credentials must not be passed through tool calls",
                CREDENTIAL_HINT,
            ),
        )

    selector = _blank_to_none(selector)
    intent = _blank_to_none(intent)
    ai_mode, err = _resolve_ai_mode(selector, intent)
    if err:
        return make_result(
            "skyvern_type",
            ok=False,
            error=make_error(
                ErrorCode.INVALID_INPUT,
                "Must provide intent, selector, or both",
                "Use intent='describe the input field' for AI-powered targeting, or selector='#css-selector' for precise targeting",
            ),
        )

    try:
        page, ctx = await get_page(session_id=session_id, cdp_url=cdp_url)
    except BrowserNotAvailableError:
        return make_result("skyvern_type", ok=False, error=no_browser_error())
    source_url = _trajectory_source_url(page)

    # DOM-level guard: check if the target element is a password field
    if selector:
        try:
            is_password_field = await page.evaluate(
                "(s) => { const el = document.querySelector(s); return el && el.type === 'password' }",
                selector,
            )
        except Exception as exc:
            # Selector may not be a valid CSS selector (e.g. xpath=...) or page may
            # not be ready. Fall through to the existing regex guard in that case.
            LOG.debug("DOM password check failed for selector %r: %s", selector, exc)
            is_password_field = False
        if is_password_field:
            return make_result(
                "skyvern_type",
                ok=False,
                error=make_error(
                    ErrorCode.INVALID_INPUT,
                    "Cannot type into password fields — credentials must not be passed through tool calls",
                    CREDENTIAL_HINT,
                ),
            )

    deterministic = selector is not None and selector_mode == "direct"
    direct_action = is_direct_action(selector, ai_mode, deterministic=deterministic)
    action_timeout = resolve_action_timeout_ms(timeout, direct_action=direct_action)
    skip_element_prep = selector is not None and ai_mode is None and not deterministic

    with Timer() as timer:
        try:
            # selector_mode="direct" pins the selector with no AI fall-back. Resilient (default) and
            # intent-only calls keep AI; emitted scripts keep the selector+prompt fallback via
            # sdk_equivalent for DOM-drift resilience.
            if clear:
                if deterministic:
                    assert selector is not None
                    await page.fill(selector, text, mode="direct", timeout=action_timeout)
                elif ai_mode is not None:
                    await page.fill(selector=selector, value=text, prompt=intent, ai=ai_mode, timeout=action_timeout)  # type: ignore[arg-type]
                else:
                    assert selector is not None
                    fill_kwargs: dict[str, Any] = {"timeout": action_timeout}
                    if isinstance(page, SkyvernPage):
                        fill_kwargs["_skip_element_prep"] = skip_element_prep
                    await page.fill(selector, text, **fill_kwargs)
            else:
                kwargs: dict[str, Any] = {"timeout": action_timeout}
                if delay is not None:
                    kwargs["delay"] = delay
                if deterministic:
                    await page.type(selector, text, ai=None, **kwargs)
                elif ai_mode is not None:
                    loc = page.locator(selector=selector, prompt=intent, ai=ai_mode)  # type: ignore[arg-type]
                    await loc.type(text, **kwargs)
                else:
                    assert selector is not None
                    if isinstance(page, SkyvernPage):
                        kwargs["_skip_element_prep"] = skip_element_prep
                    await page.type(selector, text, **kwargs)
            timer.mark("sdk")
        except PlaywrightTimeoutError as e:
            if direct_action and selector is not None:
                return await _direct_failure_result("skyvern_type", ctx, timer, page, selector, e, action_timeout)
            return make_result(
                "skyvern_type",
                ok=False,
                browser_context=ctx,
                timing_ms=timer.timing_ms,
                error=make_error(
                    ErrorCode.SELECTOR_NOT_FOUND,
                    str(e),
                    "Verify the selector matches an editable element, or use intent for AI-powered finding",
                ),
            )
        except Exception as e:
            code = ErrorCode.AI_FALLBACK_FAILED if (ai_mode and not deterministic) else ErrorCode.ACTION_FAILED
            if direct_action and selector is not None and is_pointer_interception_error(e):
                return await _direct_failure_result("skyvern_type", ctx, timer, page, selector, e, action_timeout)
            return make_result(
                "skyvern_type",
                ok=False,
                browser_context=ctx,
                timing_ms=timer.timing_ms,
                error=make_error(
                    code,
                    _exception_message(e),
                    "The element may not be editable or may be hidden",
                    details=_exception_details(e),
                ),
            )

    # NOTE: The SDK fill() returns the typed value, not a resolved selector.
    # Unlike click(), we cannot return resolved_selector here. SKY-7905 will
    # update the SDK to return element metadata from all action methods.
    data: dict[str, Any] = {"selector": selector, "intent": intent, "ai_mode": ai_mode, "text_length": len(text)}
    # Build sdk_equivalent: prefer hybrid selector+prompt for production scripts
    if selector and intent:
        data["sdk_equivalent"] = f"await page.fill({selector!r}, {text!r}, prompt={intent!r})"
    elif ai_mode:
        data["sdk_equivalent"] = f"await page.fill(prompt={intent!r}, value={text!r})"
    elif selector:
        data["sdk_equivalent"] = f"await page.fill({selector!r}, {text!r})"
    if clear and selector is not None and (ai_mode is None or deterministic):
        _record_trajectory_entry(
            ctx,
            tool_name="type_text",
            selector=selector,
            source_url=source_url,
            typed_text=text,
        )
    return make_result(
        "skyvern_type",
        browser_context=ctx,
        data=data,
        timing_ms=timer.timing_ms,
    )


async def skyvern_screenshot(
    selector: Annotated[
        str | None, Field(description=f"{DIRECT_TARGET_DESCRIPTION} CSS selector to screenshot a specific element.")
    ] = None,
    session_id: Annotated[str | None, Field(description="Browser session ID (pbs_...)")] = None,
    cdp_url: Annotated[str | None, Field(description="CDP WebSocket URL")] = None,
    full_page: Annotated[bool, Field(description="Capture full scrollable page")] = False,
    inline: Annotated[bool, Field(description="Return base64 data instead of file path")] = False,
) -> dict[str, Any]:
    """Capture a visual screenshot of the current page. Use after page-changing actions to verify results.
    For structured data extraction, use skyvern_extract instead. Set full_page=true for full-page capture.
    Set inline=true to get base64 data directly (increases token usage — avoid in loops).
    """
    try:
        page, ctx = await get_page(session_id=session_id, cdp_url=cdp_url)
    except BrowserNotAvailableError:
        return make_result("skyvern_screenshot", ok=False, error=no_browser_error())

    with Timer() as timer:
        try:
            result = await do_screenshot(page, full_page=full_page, selector=selector)
            timer.mark("sdk")
        except Exception as e:
            return make_result(
                "skyvern_screenshot",
                ok=False,
                browser_context=ctx,
                timing_ms=timer.timing_ms,
                error=make_error(ErrorCode.ACTION_FAILED, str(e), "Check that the page or element is visible"),
            )

    if inline:
        data_b64 = base64.b64encode(result.data).decode("utf-8")
        return make_result(
            "skyvern_screenshot",
            browser_context=ctx,
            data={
                "inline": True,
                "data": data_b64,
                "mime": "image/png",
                "bytes": len(result.data),
                "sdk_equivalent": "await page.screenshot()",
            },
            timing_ms=timer.timing_ms,
            warnings=["Inline mode increases token usage"],
        )

    ts = datetime.now(timezone.utc).strftime("%H%M%S_%f")
    filename = f"screenshot_{ts}.png"
    artifact = save_artifact(
        result.data,
        kind="screenshot",
        filename=filename,
        mime="image/png",
        session_id=ctx.session_id,
    )

    return make_result(
        "skyvern_screenshot",
        browser_context=ctx,
        data={"path": artifact.path, "sdk_equivalent": "await page.screenshot(path='screenshot.png')"},
        artifacts=[artifact],
        timing_ms=timer.timing_ms,
    )


async def skyvern_scroll(
    direction: Annotated[str, Field(description="Direction: up, down, left, right")],
    selector: Annotated[
        str | None, Field(description=f"{DIRECT_TARGET_DESCRIPTION} CSS selector of scrollable element.")
    ] = None,
    session_id: Annotated[str | None, Field(description="Browser session ID (pbs_...)")] = None,
    cdp_url: Annotated[str | None, Field(description="CDP WebSocket URL")] = None,
    amount: Annotated[int | None, Field(description="Pixels to scroll (default 500)")] = None,
    intent: Annotated[str | None, Field(description=AI_FALLBACK_DESCRIPTION)] = None,
) -> dict[str, Any]:
    """Scroll the page or an element into view. Use intent for AI-powered scrolling, or pixel amount for manual control."""
    valid_directions = ("up", "down", "left", "right")
    if not intent and direction not in valid_directions:
        return make_result(
            "skyvern_scroll",
            ok=False,
            error=make_error(
                ErrorCode.INVALID_INPUT, f"Invalid direction: {direction}", "Use up, down, left, or right"
            ),
        )

    try:
        page, ctx = await get_page(session_id=session_id, cdp_url=cdp_url)
    except BrowserNotAvailableError:
        return make_result("skyvern_scroll", ok=False, error=no_browser_error())

    if intent:
        ai_mode = "fallback" if selector else "proactive"
        with Timer() as timer:
            try:
                loc = page.locator(selector=selector, prompt=intent, ai=ai_mode)
                await loc.scroll_into_view_if_needed()
                timer.mark("sdk")
            except Exception as e:
                code = ErrorCode.AI_FALLBACK_FAILED if ai_mode == "fallback" else ErrorCode.ACTION_FAILED
                return make_result(
                    "skyvern_scroll",
                    ok=False,
                    browser_context=ctx,
                    timing_ms=timer.timing_ms,
                    error=make_error(
                        code,
                        _exception_message(e),
                        "Could not find element to scroll into view",
                        details=_exception_details(e),
                    ),
                )

        return make_result(
            "skyvern_scroll",
            browser_context=ctx,
            data={
                "direction": "into_view",
                "intent": intent,
                "ai_mode": ai_mode,
                "sdk_equivalent": (
                    f"await page.locator({selector!r}, prompt={intent!r}).scroll_into_view_if_needed()"
                    if selector
                    else f"await page.locator(prompt={intent!r}).scroll_into_view_if_needed()"
                ),
            },
            timing_ms=timer.timing_ms,
        )

    pixels = amount or 500
    direction_map = {
        "up": (0, -pixels),
        "down": (0, pixels),
        "left": (-pixels, 0),
        "right": (pixels, 0),
    }
    dx, dy = direction_map[direction]

    with Timer() as timer:
        try:
            if selector:
                await page.locator(selector).evaluate(f"el => el.scrollBy({dx}, {dy})")
            else:
                await page.evaluate(f"window.scrollBy({dx}, {dy})")
            timer.mark("sdk")
        except Exception as e:
            return make_result(
                "skyvern_scroll",
                ok=False,
                browser_context=ctx,
                timing_ms=timer.timing_ms,
                error=make_error(ErrorCode.ACTION_FAILED, str(e), "Scroll action failed"),
            )

    return make_result(
        "skyvern_scroll",
        browser_context=ctx,
        data={
            "direction": direction,
            "pixels": pixels,
            "sdk_equivalent": f'await page.evaluate("window.scrollBy({dx}, {dy})")',
        },
        timing_ms=timer.timing_ms,
    )


async def skyvern_select_option(
    value: Annotated[str, "Value to select"],
    selector: Annotated[
        str | None, Field(description=f"{DIRECT_TARGET_DESCRIPTION} CSS selector for the select element.")
    ] = None,
    selector_mode: _SelectorMode = "resilient",
    session_id: Annotated[str | None, Field(description="Browser session ID (pbs_...)")] = None,
    cdp_url: Annotated[str | None, Field(description="CDP WebSocket URL")] = None,
    timeout: Annotated[
        int | None,
        Field(description=ACTION_TIMEOUT_DESCRIPTION, ge=MIN_ACTION_TIMEOUT_MS, le=MAX_ACTION_TIMEOUT_MS),
    ] = None,
    by_label: Annotated[bool, Field(description="Select by visible label instead of value")] = False,
    intent: Annotated[str | None, Field(description=AI_FALLBACK_DESCRIPTION)] = None,
) -> dict[str, Any]:
    """Select an option from a dropdown menu. Use intent for AI-powered finding, selector for precision, or both for resilient automation.

    For free-text input fields, use skyvern_type instead. For non-dropdown buttons or links, use skyvern_click.
    Targeting a plain text input types the value while probing for suggestions and fails closed if none appear;
    hybrid calls restore the original value before the AI fallback runs, direct calls leave the typed value.
    The deterministic attempt and each SDK fallback stage get their own timeout budget rather than one shared deadline.
    """
    selector = _blank_to_none(selector)
    intent = _blank_to_none(intent)
    ai_mode, err = _resolve_ai_mode(selector, intent)
    if err:
        return make_result(
            "skyvern_select_option",
            ok=False,
            error=make_error(
                ErrorCode.INVALID_INPUT,
                "Must provide intent, selector, or both",
                "Use intent='describe the dropdown' for AI-powered selection, or selector='#css-selector' for precise targeting",
            ),
        )

    # Credential-intent guard (parity with skyvern_type/skyvern_act): a password/credential
    # intent must not reach the AI fallback, even with no selector or a stale one.
    try:
        if intent is not None:
            check_password_prompt(intent)
    except GuardError as e:
        return make_result(
            "skyvern_select_option",
            ok=False,
            error=make_error(ErrorCode.INVALID_INPUT, str(e), e.hint),
        )

    try:
        page, ctx = await get_page(session_id=session_id, cdp_url=cdp_url)
    except BrowserNotAvailableError:
        return make_result("skyvern_select_option", ok=False, error=no_browser_error())
    source_url = _trajectory_source_url(page)

    deterministic = selector is not None and selector_mode == "direct"
    direct_action = is_direct_action(selector, ai_mode, deterministic=deterministic)
    action_timeout = resolve_action_timeout_ms(timeout, direct_action=direct_action)

    # Credential safety runs OUTSIDE the custom-select gate and the kill switch: a password
    # target must never be filled or have its value forwarded to the AI-fallback LLM payload.
    # When the target type cannot be determined, fail closed for the value-bearing AI path.
    password_target: bool | None = False
    if selector is not None:
        scope: Any = getattr(page, "_locator_scope", None) or getattr(page, "page", page)
        try:
            password_target = bool(
                await scope.locator(selector).first.evaluate(
                    "el => el.tagName === 'INPUT' && (el.getAttribute('type') || '').toLowerCase() === 'password'",
                    timeout=min(action_timeout, 1000),
                )
            )
        except Exception:
            password_target = None
    if password_target or (password_target is None and ai_mode is not None and not deterministic):
        return make_result(
            "skyvern_select_option",
            ok=False,
            browser_context=ctx,
            error=make_error(
                ErrorCode.INVALID_INPUT,
                "Cannot select an option on a password field",
                CREDENTIAL_HINT,
            ),
        )

    # Operational kill switch: restores the exact pre-custom-select behavior
    # (native <select> only, no classification probe) without a code rollback.
    custom_select_disabled = os.environ.get("SKYVERN_DISABLE_CUSTOM_SELECT", "").strip().lower() in ("1", "true", "yes")
    custom_attempt_ms = 0
    if selector is not None and not custom_select_disabled:
        custom_selection = None
        custom_fallback_attempted = False
        with Timer() as custom_timer:
            try:
                custom_selection = await do_select_option(
                    getattr(page, "_locator_scope", None) or getattr(page, "page", page),
                    selector,
                    value,
                    by_label=by_label,
                    timeout=action_timeout,
                    restore_value_on_failure=ai_mode == "fallback" and not deterministic,
                    fail_closed_on_unknown=ai_mode is not None and not deterministic,
                )
                if custom_selection is not None:
                    custom_timer.mark("sdk")
            except CustomSelectPasswordError:
                # Terminal for every call shape (direct AND hybrid): a password value must
                # never reach the native SDK fill or the AI-fallback LLM payload.
                return make_result(
                    "skyvern_select_option",
                    ok=False,
                    browser_context=ctx,
                    timing_ms=custom_timer.timing_ms,
                    error=make_error(
                        ErrorCode.INVALID_INPUT,
                        "Cannot select an option on a password field",
                        CREDENTIAL_HINT,
                    ),
                )
            except CustomSelectClassifyError:
                # Target detached/navigated mid-probe (TOCTOU after the boundary check). Fail
                # closed for the value-bearing AI path; a direct call defers to the native
                # SDK, which cannot forward the value to an LLM.
                if ai_mode is not None and not deterministic:
                    return make_result(
                        "skyvern_select_option",
                        ok=False,
                        browser_context=ctx,
                        timing_ms=custom_timer.timing_ms,
                        error=make_error(
                            ErrorCode.INVALID_INPUT,
                            "Could not verify the target before AI selection",
                            "Re-observe the element and retry with a stable selector",
                        ),
                    )
            except CustomSelectMatchError as e:
                if deterministic or ai_mode != "fallback":
                    observed = ", ".join(e.observed_options) or "none"
                    return make_result(
                        "skyvern_select_option",
                        ok=False,
                        browser_context=ctx,
                        timing_ms=custom_timer.timing_ms,
                        error=make_error(
                            ErrorCode.ACTION_FAILED,
                            f"No unambiguous option matched {e.requested_option!r}",
                            f"Retry with one of the observed options: {observed}",
                            details={
                                "element_state": "no_unambiguous_match",
                                "selector": e.selector,
                                "requested_option": e.requested_option,
                                "observed_options": e.observed_options,
                            },
                        ),
                    )
                custom_fallback_attempted = True
            except CustomSelectOpenError as e:
                # The widget never opened (click intercepted, fill timeout) — nothing was
                # acted on, so hybrid calls may still recover through the AI fallback.
                if deterministic or ai_mode != "fallback":
                    return make_result(
                        "skyvern_select_option",
                        ok=False,
                        browser_context=ctx,
                        timing_ms=custom_timer.timing_ms,
                        error=make_error(
                            ErrorCode.ACTION_FAILED,
                            _exception_message(e),
                            "Could not open the dropdown to inspect its options",
                            details=_exception_details(e),
                        ),
                    )
                custom_fallback_attempted = True
            except Exception as e:
                # Post-option-click failures (an option was clicked but did not verifiably
                # commit) leave the widget in an unknown state — replaying through the AI
                # fallback could double-act, so these are terminal even for hybrid calls.
                # Only the pre-option branches above may fall through.
                return make_result(
                    "skyvern_select_option",
                    ok=False,
                    browser_context=ctx,
                    timing_ms=custom_timer.timing_ms,
                    error=make_error(
                        ErrorCode.ACTION_FAILED,
                        _exception_message(e),
                        "The custom dropdown selection could not be verified",
                        details=_exception_details(e),
                    ),
                )
        if custom_fallback_attempted:
            custom_attempt_ms = custom_timer.timing_ms.get("total", 0)
        if custom_selection is not None:
            return make_result(
                "skyvern_select_option",
                browser_context=ctx,
                data={
                    "selector": selector,
                    "intent": intent,
                    "ai_mode": ai_mode,
                    "value": value,
                    "selected_option": {"label": custom_selection},
                    "sdk_equivalent": (
                        f"# No single SDK method -- open/filter {selector!r}, "
                        f"then click exact observed option {custom_selection!r}"
                    ),
                },
                timing_ms=custom_timer.timing_ms,
            )

    with Timer() as timer:
        try:
            # selector_mode="direct" pins the selector with no AI fall-back. Only an intent-only call
            # (no selector) uses AI to interpret the option text.
            if ai_mode is not None and not deterministic:
                await page.select_option(
                    selector=selector,  # type: ignore[arg-type]
                    value=value,
                    prompt=intent,
                    ai=ai_mode,
                    timeout=action_timeout,
                )
            else:
                assert selector is not None
                if by_label:
                    # Bypass SkyvernPage to avoid value="" coercion conflicting with label kwarg.
                    await page.page.locator(selector).select_option(label=value, timeout=action_timeout)
                elif deterministic:
                    await page.select_option(selector, value=value, ai=None, timeout=action_timeout)
                else:
                    await page.select_option(selector, value=value, timeout=action_timeout)
            timer.mark("sdk")
        except PlaywrightTimeoutError as e:
            if direct_action and selector is not None:
                return await _direct_failure_result(
                    "skyvern_select_option", ctx, timer, page, selector, e, action_timeout
                )
            code = ErrorCode.AI_FALLBACK_FAILED if (ai_mode and not deterministic) else ErrorCode.ACTION_FAILED
            if custom_attempt_ms:
                timer.mark("total")
                return make_result(
                    "skyvern_select_option",
                    ok=False,
                    browser_context=ctx,
                    timing_ms=_add_timing_prefix(timer.timing_ms, custom_attempt_ms),
                    error=make_error(
                        code,
                        _exception_message(e),
                        "Check selector and available options",
                        details=_exception_details(e),
                    ),
                )
            return make_result(
                "skyvern_select_option",
                ok=False,
                browser_context=ctx,
                timing_ms=timer.timing_ms,
                error=make_error(
                    code,
                    _exception_message(e),
                    "Check selector and available options",
                    details=_exception_details(e),
                ),
            )
        except Exception as e:
            code = ErrorCode.AI_FALLBACK_FAILED if (ai_mode and not deterministic) else ErrorCode.ACTION_FAILED
            if direct_action and selector is not None and is_pointer_interception_error(e):
                return await _direct_failure_result(
                    "skyvern_select_option", ctx, timer, page, selector, e, action_timeout
                )
            if custom_attempt_ms:
                timer.mark("total")
                return make_result(
                    "skyvern_select_option",
                    ok=False,
                    browser_context=ctx,
                    timing_ms=_add_timing_prefix(timer.timing_ms, custom_attempt_ms),
                    error=make_error(
                        code,
                        _exception_message(e),
                        "Check selector and available options",
                        details=_exception_details(e),
                    ),
                )
            return make_result(
                "skyvern_select_option",
                ok=False,
                browser_context=ctx,
                timing_ms=timer.timing_ms,
                error=make_error(
                    code,
                    _exception_message(e),
                    "Check selector and available options",
                    details=_exception_details(e),
                ),
            )

    # NOTE: The SDK select_option() returns the selected value, not a resolved
    # selector. Unlike click(), we cannot return resolved_selector here.
    # SKY-7905 will update the SDK to return element metadata from all action methods.
    data: dict[str, Any] = {"selector": selector, "intent": intent, "ai_mode": ai_mode, "value": value}
    # Build sdk_equivalent: prefer hybrid selector+prompt for production scripts
    if selector and intent:
        data["sdk_equivalent"] = f"await page.select_option({selector!r}, value={value!r}, prompt={intent!r})"
    elif ai_mode:
        data["sdk_equivalent"] = f"await page.select_option(prompt={intent!r}, value={value!r})"
    elif selector:
        data["sdk_equivalent"] = f"await page.select_option({selector!r}, value={value!r})"
    if selector is not None and not by_label and (ai_mode is None or deterministic) and _replayable_select_value(value):
        _record_trajectory_entry(
            ctx,
            tool_name="select_option",
            selector=selector,
            source_url=source_url,
            value=value,
        )
    if custom_attempt_ms:
        return make_result(
            "skyvern_select_option",
            browser_context=ctx,
            data=data,
            timing_ms=_add_timing_prefix(timer.timing_ms, custom_attempt_ms),
        )
    return make_result(
        "skyvern_select_option",
        browser_context=ctx,
        data=data,
        timing_ms=timer.timing_ms,
    )


async def skyvern_press_key(
    key: Annotated[str, "Key to press (e.g., Enter, Tab, Escape, ArrowDown)"],
    selector: Annotated[
        str | None, Field(description=f"{DIRECT_TARGET_DESCRIPTION} CSS selector to focus first.")
    ] = None,
    session_id: Annotated[str | None, Field(description="Browser session ID (pbs_...)")] = None,
    cdp_url: Annotated[str | None, Field(description="CDP WebSocket URL")] = None,
    timeout: Annotated[
        int | None,
        Field(description=ACTION_TIMEOUT_DESCRIPTION, ge=MIN_ACTION_TIMEOUT_MS, le=MAX_ACTION_TIMEOUT_MS),
    ] = None,
    intent: Annotated[str | None, Field(description=AI_FALLBACK_DESCRIPTION)] = None,
) -> dict[str, Any]:
    """Press a keyboard key -- Enter, Tab, Escape, arrow keys, shortcuts, etc.

    Use `intent` or `selector` to focus a specific element before pressing.
    Without either, presses the key on the currently focused element.
    """
    selector = _blank_to_none(selector)
    intent = _blank_to_none(intent)
    try:
        page, ctx = await get_page(session_id=session_id, cdp_url=cdp_url)
    except BrowserNotAvailableError:
        return make_result("skyvern_press_key", ok=False, error=no_browser_error())
    source_url = _trajectory_source_url(page)

    ai_mode = _resolve_ai_mode(selector, intent)[0] if (intent or selector) else None
    direct_action = is_direct_action(selector, ai_mode)
    action_timeout = resolve_action_timeout_ms(timeout, direct_action=direct_action)

    with Timer() as timer:
        try:
            if intent or selector:
                if ai_mode is not None:
                    loc = page.locator(selector=selector, prompt=intent, ai=ai_mode)  # type: ignore[arg-type]
                    await loc.press(key, timeout=action_timeout)
                else:
                    assert selector is not None
                    await page.locator(selector).press(key, timeout=action_timeout)
            else:
                await page.keyboard.press(key)
            timer.mark("sdk")
        except Exception as e:
            if direct_action and selector is not None:
                if isinstance(e, PlaywrightTimeoutError) or is_pointer_interception_error(e):
                    return await _direct_failure_result(
                        "skyvern_press_key", ctx, timer, page, selector, e, action_timeout
                    )
            return make_result(
                "skyvern_press_key",
                ok=False,
                browser_context=ctx,
                timing_ms=timer.timing_ms,
                error=make_error(
                    ErrorCode.ACTION_FAILED,
                    _exception_message(e),
                    "Check key name is valid",
                    details=_exception_details(e),
                ),
            )

    if selector and intent:
        sdk_eq = f"await page.locator({selector!r}, prompt={intent!r}).press({key!r})"
    elif intent:
        sdk_eq = f"await page.locator(prompt={intent!r}).press({key!r})"
    elif selector:
        sdk_eq = f"await page.locator({selector!r}).press({key!r})"
    else:
        sdk_eq = f"await page.keyboard.press({key!r})"

    if intent is None and _replayable_press_key(key):
        _record_trajectory_entry(
            ctx,
            tool_name="press_key",
            key=key,
            selector=selector,
            source_url=source_url,
        )
    return make_result(
        "skyvern_press_key",
        browser_context=ctx,
        data={
            "key": key,
            "selector": selector,
            "intent": intent,
            "sdk_equivalent": sdk_eq,
        },
        timing_ms=timer.timing_ms,
    )


async def skyvern_wait(
    selector: Annotated[str | None, Field(description=f"{DIRECT_TARGET_DESCRIPTION} CSS selector to wait for.")] = None,
    state: Annotated[str | None, Field(description="Element state: visible, hidden, attached, detached")] = "visible",
    session_id: Annotated[str | None, Field(description="Browser session ID (pbs_...)")] = None,
    cdp_url: Annotated[str | None, Field(description="CDP WebSocket URL")] = None,
    time_ms: Annotated[int | None, Field(description="Time to wait in milliseconds")] = None,
    timeout: Annotated[int, Field(description="Max wait time in milliseconds", ge=1000, le=120000)] = 30000,
    poll_interval_ms: Annotated[
        int, Field(description="Polling interval for intent-based waits in ms", ge=500, le=10000)
    ] = 5000,
    intent: Annotated[str | None, Field(description=AI_FALLBACK_DESCRIPTION)] = None,
) -> dict[str, Any]:
    """Wait for a condition, element, or time delay before proceeding. Use intent for AI-powered condition checking.

    Use `intent` to poll with AI validation (e.g., "wait until the loading spinner disappears").
    Use `selector` to wait for an element state. Use `time_ms` for a simple delay.
    """
    valid_states = ("visible", "hidden", "attached", "detached")
    if state is not None and state not in valid_states:
        return make_result(
            "skyvern_wait",
            ok=False,
            error=make_error(
                ErrorCode.INVALID_INPUT,
                f"Invalid state: {state}",
                "Use visible, hidden, attached, or detached",
            ),
        )

    if time_ms is None and not selector and not intent:
        return make_result(
            "skyvern_wait",
            ok=False,
            error=make_error(
                ErrorCode.INVALID_INPUT,
                "Must provide intent, selector, or time_ms",
                "Use intent='condition to wait for' for AI-powered waiting, selector='#element' for element visibility, or time_ms=5000 for a delay",
            ),
        )

    try:
        page, ctx = await get_page(session_id=session_id, cdp_url=cdp_url)
    except BrowserNotAvailableError:
        return make_result("skyvern_wait", ok=False, error=no_browser_error())

    with Timer() as timer:
        try:
            if time_ms is not None:
                await page.wait_for_timeout(time_ms)
                waited_for = "time"
            elif intent:
                loop = asyncio.get_running_loop()
                deadline = loop.time() + timeout / 1000
                last_error: Exception | None = None
                while True:
                    try:
                        result = await page.validate(intent)
                        last_error = None
                    except Exception as poll_err:
                        result = False
                        last_error = poll_err
                    if result:
                        break
                    if loop.time() >= deadline:
                        code = ErrorCode.SDK_ERROR if last_error else ErrorCode.TIMEOUT
                        msg = (
                            _exception_message(last_error)
                            if last_error
                            else f"Condition not met within {timeout}ms: {intent}"
                        )
                        return make_result(
                            "skyvern_wait",
                            ok=False,
                            browser_context=ctx,
                            timing_ms=timer.timing_ms,
                            error=make_error(
                                code,
                                msg,
                                "Increase timeout or check that the condition can be satisfied",
                                details=_exception_details(last_error) if last_error else None,
                            ),
                        )
                    await page.wait_for_timeout(poll_interval_ms)
                waited_for = "intent"
            elif selector:
                await page.wait_for_selector(selector, state=state, timeout=timeout)
                waited_for = "selector"
            timer.mark("sdk")
        except Exception as e:
            return make_result(
                "skyvern_wait",
                ok=False,
                browser_context=ctx,
                timing_ms=timer.timing_ms,
                error=make_error(
                    ErrorCode.TIMEOUT,
                    _exception_message(e),
                    "Condition was not met within timeout",
                    details=_exception_details(e),
                ),
            )

    sdk_eq = ""
    if waited_for == "time":
        sdk_eq = f"await page.wait_for_timeout({time_ms})"
    elif waited_for == "intent":
        sdk_eq = f"await page.validate({intent!r})"
    elif waited_for == "selector":
        sdk_eq = f"await page.wait_for_selector({selector!r})"
    return make_result(
        "skyvern_wait",
        browser_context=ctx,
        data={"waited_for": waited_for, "sdk_equivalent": sdk_eq},
        timing_ms=timer.timing_ms,
    )


def _wrap_async_iife(expression: str) -> str:
    """Wrap expressions containing ``await`` in an async IIFE so page.evaluate() can run them.

    Single-line: ``(async () => { return <expr> })()`` — implicit return.
    Multi-line:  ``(async () => { <expr> })()`` — caller must use explicit return.
    Already-wrapped or no ``await``: returned unchanged.
    """
    if expression.lstrip().startswith("(async"):
        return expression
    stripped = _SINGLE_LINE_COMMENT_RE.sub("", expression)
    if not _AWAIT_RE.search(stripped):
        return expression
    if "\n" in expression:
        return f"(async () => {{ {expression} }})()"
    return f"(async () => {{ return {expression} }})()"


async def skyvern_evaluate(
    expression: Annotated[str, "JavaScript expression to evaluate"],
    session_id: Annotated[str | None, Field(description="Browser session ID (pbs_...)")] = None,
    cdp_url: Annotated[str | None, Field(description="CDP WebSocket URL")] = None,
) -> dict[str, Any]:
    """Run JavaScript on the page. Supports await (auto-wrapped in async IIFE). For multi-line await, use explicit return.
    Security: executes in page context — use only with trusted expressions."""
    # Block JS that sets password field values
    if JS_PASSWORD_PATTERN.search(expression):
        return make_result(
            "skyvern_evaluate",
            ok=False,
            error=make_error(
                ErrorCode.INVALID_INPUT,
                "Cannot set password field values via JavaScript — credentials must not be passed through tool calls",
                CREDENTIAL_HINT,
            ),
        )

    js = _wrap_async_iife(expression)

    try:
        page, ctx = await get_page(session_id=session_id, cdp_url=cdp_url)
    except BrowserNotAvailableError:
        return make_result("skyvern_evaluate", ok=False, error=no_browser_error())

    with Timer() as timer:
        try:
            result = await page.evaluate(js)
            timer.mark("sdk")
        except Exception as e:
            return make_result(
                "skyvern_evaluate",
                ok=False,
                browser_context=ctx,
                timing_ms=timer.timing_ms,
                error=make_error(ErrorCode.ACTION_FAILED, str(e), "Check JavaScript syntax"),
            )

    return make_result(
        "skyvern_evaluate",
        browser_context=ctx,
        data={"result": result, "sdk_equivalent": f"await page.evaluate({expression[:80]!r})"},
        timing_ms=timer.timing_ms,
    )


async def skyvern_extract(
    prompt: Annotated[str, "Natural language description of what data to extract from the page"],
    session_id: Annotated[str | None, Field(description="Browser session ID (pbs_...)")] = None,
    cdp_url: Annotated[str | None, Field(description="CDP WebSocket URL")] = None,
    schema: Annotated[
        str | None, Field(description="JSON Schema string defining the expected output structure")
    ] = None,
) -> dict[str, Any]:
    """Extract structured data from the current page using AI with screenshots and a dedicated extraction LLM.
    Navigate first. Optionally provide a JSON schema to enforce output structure. For visual-only inspection use skyvern_screenshot.
    """
    if schema is not None:
        try:
            parsed_schema = parse_extract_schema(schema)
        except GuardError as e:
            return make_result(
                "skyvern_extract",
                ok=False,
                error=make_error(ErrorCode.INVALID_INPUT, str(e), e.hint),
            )
    else:
        parsed_schema = None

    try:
        page, ctx = await get_page(session_id=session_id, cdp_url=cdp_url)
    except BrowserNotAvailableError:
        return make_result("skyvern_extract", ok=False, error=no_browser_error())

    with Timer() as timer:
        try:
            result = await do_extract(page, prompt, schema=parsed_schema, skip_refresh=True)
            timer.mark("sdk")
        except GuardError as e:
            return make_result(
                "skyvern_extract",
                ok=False,
                browser_context=ctx,
                timing_ms=timer.timing_ms,
                error=make_error(ErrorCode.INVALID_INPUT, str(e), e.hint),
            )
        except Exception as e:
            return make_result(
                "skyvern_extract",
                ok=False,
                browser_context=ctx,
                timing_ms=timer.timing_ms,
                error=make_error(
                    ErrorCode.SDK_ERROR,
                    _exception_message(e),
                    "Check that the page has loaded and the prompt is clear",
                    details=_exception_details(e),
                ),
            )

    return make_result(
        "skyvern_extract",
        browser_context=ctx,
        data={
            "extracted": result.extracted,
            "sdk_equivalent": f"await page.extract(prompt={prompt!r})",
        },
        timing_ms=timer.timing_ms,
    )


async def skyvern_validate(
    prompt: Annotated[str, "Validation condition to check (e.g., 'the login form is visible')"],
    session_id: Annotated[str | None, Field(description="Browser session ID (pbs_...)")] = None,
    cdp_url: Annotated[str | None, Field(description="CDP WebSocket URL")] = None,
) -> dict[str, Any]:
    """Check if a condition is true on the current page — cheapest AI option for yes/no questions.
    Navigate first. Returns boolean. To extract data, use skyvern_extract instead.
    """
    try:
        page, ctx = await get_page(session_id=session_id, cdp_url=cdp_url)
    except BrowserNotAvailableError:
        return make_result("skyvern_validate", ok=False, error=no_browser_error())

    with Timer() as timer:
        try:
            valid = await page.validate(prompt)
            timer.mark("sdk")
        except Exception as e:
            return make_result(
                "skyvern_validate",
                ok=False,
                browser_context=ctx,
                timing_ms=timer.timing_ms,
                error=make_error(
                    ErrorCode.SDK_ERROR,
                    _exception_message(e),
                    "Check that the page has loaded and the prompt is clear",
                    details=_exception_details(e),
                ),
            )

    return make_result(
        "skyvern_validate",
        browser_context=ctx,
        data={"prompt": prompt, "valid": valid, "sdk_equivalent": f"await page.validate({prompt!r})"},
        timing_ms=timer.timing_ms,
    )


async def skyvern_act(
    prompt: Annotated[str, "Natural language instruction for the action to perform (e.g., 'close the cookie banner')"],
    session_id: Annotated[str | None, Field(description="Browser session ID (pbs_...)")] = None,
    cdp_url: Annotated[str | None, Field(description="CDP WebSocket URL")] = None,
) -> dict[str, Any]:
    """Perform actions on a page by describing what to do in plain English. No screenshots in reasoning — uses economy a11y tree.
    Chain multiple actions in one prompt: "close the cookie banner, then click Sign In".
    For visually complex targets, use skyvern_observe + skyvern_click with refs. NEVER include passwords — use skyvern_login.
    """
    try:
        check_password_prompt(prompt)
    except GuardError as e:
        return make_result(
            "skyvern_act",
            ok=False,
            error=make_error(ErrorCode.INVALID_INPUT, str(e), e.hint),
        )

    try:
        page, ctx = await get_page(session_id=session_id, cdp_url=cdp_url)
    except BrowserNotAvailableError:
        return make_result("skyvern_act", ok=False, error=no_browser_error())

    with Timer() as timer:
        try:
            result = await do_act(page, prompt, skip_refresh=True, use_economy_tree=True)
            timer.mark("sdk")
        except GuardError as e:
            return make_result(
                "skyvern_act",
                ok=False,
                browser_context=ctx,
                timing_ms=timer.timing_ms,
                error=make_error(ErrorCode.INVALID_INPUT, str(e), e.hint),
            )
        except Exception as e:
            return make_result(
                "skyvern_act",
                ok=False,
                browser_context=ctx,
                timing_ms=timer.timing_ms,
                error=make_error(
                    ErrorCode.SDK_ERROR,
                    _exception_message(e),
                    "Simplify the prompt or break the task into steps",
                    details=_exception_details(e),
                ),
            )

    return make_result(
        "skyvern_act",
        browser_context=ctx,
        data={
            "prompt": result.prompt,
            "completed": result.completed,
            "sdk_equivalent": f"await page.act({prompt!r})",
        },
        timing_ms=timer.timing_ms,
    )


async def skyvern_run_task(
    prompt: Annotated[str, "Natural language description of the task to automate"],
    session_id: Annotated[str | None, Field(description="Browser session ID (pbs_...)")] = None,
    cdp_url: Annotated[str | None, Field(description="CDP WebSocket URL")] = None,
    url: Annotated[
        str | None, Field(description="URL to navigate to before running (uses current page if omitted)")
    ] = None,
    data_extraction_schema: Annotated[
        str | None, Field(description="JSON Schema string defining what data to extract")
    ] = None,
    max_steps: Annotated[int | None, Field(description="Maximum number of agent steps")] = None,
    timeout_seconds: Annotated[
        int, Field(description="Timeout in seconds (default 180s = 3 minutes)", ge=10, le=1800)
    ] = 180,
) -> dict[str, Any]:
    """Run a one-off autonomous trial via the highest-cost AI path. Not for production or reusable automations.
    Prefer direct tools (click/type/select via selector/ref) and skyvern_observe + skyvern_execute. Always uses engine 2.0.
    """
    # Block password/credential actions — redirect to skyvern_login
    if PASSWORD_PATTERN.search(prompt):
        return make_result(
            "skyvern_run_task",
            ok=False,
            error=make_error(
                ErrorCode.INVALID_INPUT,
                "Cannot perform password/credential actions — credentials must not be passed through tool calls",
                CREDENTIAL_HINT,
            ),
        )

    try:
        page, ctx = await get_page(session_id=session_id, cdp_url=cdp_url)
    except BrowserNotAvailableError:
        return make_result("skyvern_run_task", ok=False, error=no_browser_error())

    if _must_reject_localhost_url(ctx, url):
        return make_result(
            "skyvern_run_task",
            ok=False,
            browser_context=ctx,
            error=make_error(
                ErrorCode.INVALID_INPUT,
                "Cloud browsers cannot reach localhost URLs",
                "Run `pip install skyvern && skyvern browser serve --tunnel` to bridge "
                "your local dev server to a cloud browser via ngrok. "
                "Or use `local=true` in skyvern_browser_session_create for a local browser.",
            ),
        )

    parsed_schema: dict[str, Any] | str | None = None
    if data_extraction_schema is not None:
        try:
            parsed_schema = json.loads(data_extraction_schema)
        except (json.JSONDecodeError, TypeError) as e:
            return make_result(
                "skyvern_run_task",
                ok=False,
                browser_context=ctx,
                error=make_error(
                    ErrorCode.INVALID_INPUT,
                    f"Invalid data_extraction_schema JSON: {e}",
                    "Provide schema as a valid JSON string",
                ),
            )

    with Timer() as timer:
        try:
            response = await page.agent.run_task(
                prompt=prompt,
                url=url,
                data_extraction_schema=parsed_schema,
                max_steps=max_steps,
                timeout=timeout_seconds,
            )
            timer.mark("sdk")
        except asyncio.TimeoutError:
            return make_result(
                "skyvern_run_task",
                ok=False,
                browser_context=ctx,
                timing_ms=timer.timing_ms,
                error=make_error(
                    ErrorCode.TIMEOUT,
                    f"Task did not reach a final status within {timeout_seconds}s. It may still be running.",
                    "Check the run in the Skyvern dashboard, or retry with a larger timeout_seconds.",
                ),
            )
        except Exception as e:
            return make_result(
                "skyvern_run_task",
                ok=False,
                browser_context=ctx,
                timing_ms=timer.timing_ms,
                error=make_error(
                    ErrorCode.SDK_ERROR,
                    _exception_message(e),
                    "Check the prompt, URL, and timeout settings",
                    details=_exception_details(e),
                ),
            )

    return make_result(
        "skyvern_run_task",
        browser_context=ctx,
        data={
            "run_id": response.run_id,
            "status": response.status,
            "output": response.output,
            "failure_reason": response.failure_reason,
            "recording_url": response.recording_url,
            "app_url": response.app_url,
            "sdk_equivalent": f"await page.agent.run_task(prompt={prompt!r})",
        },
        timing_ms=timer.timing_ms,
    )


# Maps credential_type string → required fields for validation
_CREDENTIAL_REQUIRED_FIELDS: dict[CredentialType, list[str]] = {
    CredentialType.skyvern: ["credential_id"],
    CredentialType.bitwarden: ["bitwarden_item_id"],
    CredentialType.onepassword: ["onepassword_vault_id", "onepassword_item_id"],
    CredentialType.azure_vault: ["azure_vault_name", "azure_vault_username_key", "azure_vault_password_key"],
}


async def skyvern_login(
    credential_type: Annotated[
        str, Field(description="Credential provider: 'skyvern', 'bitwarden', '1password', or 'azure_vault'")
    ] = "skyvern",
    session_id: Annotated[str | None, Field(description="Browser session ID (pbs_...)")] = None,
    cdp_url: Annotated[str | None, Field(description="CDP WebSocket URL")] = None,
    url: Annotated[str | None, Field(description="Login page URL. Uses current page if omitted")] = None,
    credential_id: Annotated[str | None, Field(description="Skyvern credential ID (for type='skyvern')")] = None,
    bitwarden_item_id: Annotated[str | None, Field(description="Bitwarden item ID (for type='bitwarden')")] = None,
    bitwarden_collection_id: Annotated[str | None, Field(description="Bitwarden collection ID (optional)")] = None,
    onepassword_vault_id: Annotated[str | None, Field(description="1Password vault ID (for type='1password')")] = None,
    onepassword_item_id: Annotated[str | None, Field(description="1Password item ID (for type='1password')")] = None,
    azure_vault_name: Annotated[str | None, Field(description="Azure Vault name (for type='azure_vault')")] = None,
    azure_vault_username_key: Annotated[str | None, Field(description="Azure Vault username key")] = None,
    azure_vault_password_key: Annotated[str | None, Field(description="Azure Vault password key")] = None,
    azure_vault_totp_secret_key: Annotated[str | None, Field(description="Azure Vault TOTP key (optional)")] = None,
    prompt: Annotated[str | None, Field(description="Additional login instructions")] = None,
    totp_identifier: Annotated[str | None, Field(description="TOTP identifier for 2FA")] = None,
    totp_url: Annotated[str | None, Field(description="URL to fetch TOTP codes")] = None,
    timeout_seconds: Annotated[int, Field(description="Timeout in seconds (default 180)", ge=10, le=600)] = 180,
) -> dict[str, Any]:
    """Log into a website using stored credentials. AI handles the full login flow including 2FA. Passwords never exposed.
    Create credentials via CLI: skyvern credentials add."""
    # Validate credential_type
    try:
        cred_type = CredentialType(credential_type)
    except ValueError:
        valid = ", ".join(f"'{v.value}'" for v in CredentialType)
        return make_result(
            "skyvern_login",
            ok=False,
            error=make_error(
                ErrorCode.INVALID_INPUT,
                f"Invalid credential_type: '{credential_type}'",
                f"Use one of: {valid}",
            ),
        )

    # Validate required fields per credential type
    local_vars = {
        "credential_id": credential_id,
        "bitwarden_item_id": bitwarden_item_id,
        "onepassword_vault_id": onepassword_vault_id,
        "onepassword_item_id": onepassword_item_id,
        "azure_vault_name": azure_vault_name,
        "azure_vault_username_key": azure_vault_username_key,
        "azure_vault_password_key": azure_vault_password_key,
    }
    missing = [f for f in _CREDENTIAL_REQUIRED_FIELDS[cred_type] if not local_vars.get(f)]
    if missing:
        return make_result(
            "skyvern_login",
            ok=False,
            error=make_error(
                ErrorCode.INVALID_INPUT,
                f"Missing required fields for credential_type='{cred_type.value}': {', '.join(missing)}",
                f"Provide: {', '.join(missing)}",
            ),
        )

    try:
        page, ctx = await get_page(session_id=session_id, cdp_url=cdp_url)
    except BrowserNotAvailableError:
        return make_result("skyvern_login", ok=False, error=no_browser_error())

    # Common kwargs shared across all credential types
    _common_kwargs: dict[str, Any] = {"url": url, "prompt": prompt, "timeout": timeout_seconds}
    if totp_identifier is not None:
        _common_kwargs["totp_identifier"] = totp_identifier
    if totp_url is not None:
        _common_kwargs["totp_url"] = totp_url

    with Timer() as timer:
        try:
            # Dispatch per credential type to satisfy mypy's overloaded signatures
            if cred_type == CredentialType.skyvern:
                assert credential_id is not None
                response = await page.agent.login(
                    credential_type=CredentialType.skyvern,
                    credential_id=credential_id,
                    **_common_kwargs,
                )
            elif cred_type == CredentialType.bitwarden:
                assert bitwarden_item_id is not None
                response = await page.agent.login(
                    credential_type=CredentialType.bitwarden,
                    bitwarden_item_id=bitwarden_item_id,
                    bitwarden_collection_id=bitwarden_collection_id,
                    **_common_kwargs,
                )
            elif cred_type == CredentialType.onepassword:
                assert onepassword_vault_id is not None and onepassword_item_id is not None
                response = await page.agent.login(
                    credential_type=CredentialType.onepassword,
                    onepassword_vault_id=onepassword_vault_id,
                    onepassword_item_id=onepassword_item_id,
                    **_common_kwargs,
                )
            else:
                assert azure_vault_name is not None
                assert azure_vault_username_key is not None
                assert azure_vault_password_key is not None
                response = await page.agent.login(
                    credential_type=CredentialType.azure_vault,
                    azure_vault_name=azure_vault_name,
                    azure_vault_username_key=azure_vault_username_key,
                    azure_vault_password_key=azure_vault_password_key,
                    azure_vault_totp_secret_key=azure_vault_totp_secret_key,
                    **_common_kwargs,
                )
            timer.mark("sdk")
        except asyncio.TimeoutError:
            return make_result(
                "skyvern_login",
                ok=False,
                browser_context=ctx,
                timing_ms=timer.timing_ms,
                error=make_error(
                    ErrorCode.TIMEOUT,
                    f"Login workflow did not reach a final status within {timeout_seconds}s. "
                    "The login may still be running or may have already completed.",
                    "Check the run in the Skyvern dashboard, or retry with a larger timeout_seconds. "
                    "Use skyvern_observe to confirm the post-login page state.",
                ),
            )
        except Exception as e:
            return make_result(
                "skyvern_login",
                ok=False,
                browser_context=ctx,
                timing_ms=timer.timing_ms,
                error=make_error(
                    ErrorCode.SDK_ERROR,
                    _exception_message(e),
                    "Check credential_type and required fields for your credential provider",
                    details=_exception_details(e),
                ),
            )

    return make_result(
        "skyvern_login",
        browser_context=ctx,
        data={
            "run_id": response.run_id,
            "status": response.status,
            "output": response.output,
            "failure_reason": response.failure_reason,
            "recording_url": response.recording_url,
            "app_url": response.app_url,
            "sdk_equivalent": f"await page.agent.login(credential_type=CredentialType.{cred_type.name})",
        },
        timing_ms=timer.timing_ms,
    )


async def skyvern_frame_switch(
    selector: Annotated[
        str | None,
        Field(
            description=(
                f"{DIRECT_TARGET_DESCRIPTION} CSS selector for the iframe element "
                "(e.g., '#payment-frame', 'iframe[name=checkout]')."
            )
        ),
    ] = None,
    name: Annotated[str | None, Field(description="Frame name attribute")] = None,
    index: Annotated[
        int | None, Field(description="Frame index (0 = main). Use skyvern_frame_list to find indices")
    ] = None,
    session_id: Annotated[str | None, Field(description="Browser session ID (pbs_...)")] = None,
    cdp_url: Annotated[str | None, Field(description="CDP WebSocket URL")] = None,
) -> dict[str, Any]:
    """Switch into an iframe so subsequent actions target elements inside it. Use skyvern_frame_main to switch back."""
    params = sum(p is not None for p in (selector, name, index))
    if params != 1:
        return make_result(
            "skyvern_frame_switch",
            ok=False,
            error=make_error(
                ErrorCode.INVALID_INPUT,
                "Exactly one of selector, name, or index is required",
                "Use skyvern_frame_list to discover frames, then pass selector, name, or index",
            ),
        )

    try:
        page, ctx = await get_page(session_id=session_id, cdp_url=cdp_url)
    except BrowserNotAvailableError:
        return make_result("skyvern_frame_switch", ok=False, error=no_browser_error())

    with Timer() as timer:
        try:
            result = await do_frame_switch(page, selector=selector, name=name, index=index)
            timer.mark("sdk")

            # Persist frame on session state for subsequent MCP calls
            state = get_current_session()
            state._working_frame = page._working_frame
            clear_session_ref_map(session_id=ctx.session_id, cdp_url=ctx.cdp_url)
        except ValueError as e:
            return make_result(
                "skyvern_frame_switch",
                ok=False,
                browser_context=ctx,
                timing_ms=timer.timing_ms,
                error=make_error(ErrorCode.INVALID_INPUT, str(e), "Use skyvern_frame_list to find valid frames"),
            )
        except Exception as e:
            return make_result(
                "skyvern_frame_switch",
                ok=False,
                browser_context=ctx,
                timing_ms=timer.timing_ms,
                error=make_error(ErrorCode.ACTION_FAILED, str(e), "The iframe may not be loaded yet — try waiting"),
            )

    return make_result(
        "skyvern_frame_switch",
        browser_context=ctx,
        data={
            "frame_name": result.name,
            "frame_url": result.url,
            "switched_by": "selector" if selector else ("name" if name else "index"),
            "sdk_equivalent": (
                f"await page.frame_switch(selector={selector!r})"
                if selector
                else f"await page.frame_switch(name={name!r})"
                if name
                else f"await page.frame_switch(index={index})"
            ),
        },
        timing_ms=timer.timing_ms,
    )


async def skyvern_frame_main(
    session_id: Annotated[str | None, Field(description="Browser session ID (pbs_...)")] = None,
    cdp_url: Annotated[str | None, Field(description="CDP WebSocket URL")] = None,
) -> dict[str, Any]:
    """Switch back to the main page frame after working inside an iframe.

    Call this after skyvern_frame_switch when you're done interacting with iframe content
    and want subsequent actions to target the main page again.
    """
    try:
        page, ctx = await get_page(session_id=session_id, cdp_url=cdp_url)
    except BrowserNotAvailableError:
        return make_result("skyvern_frame_main", ok=False, error=no_browser_error())

    do_frame_main(page)

    # Clear frame on session state
    state = get_current_session()
    state._working_frame = None
    clear_session_ref_map(session_id=ctx.session_id, cdp_url=ctx.cdp_url)

    return make_result(
        "skyvern_frame_main",
        browser_context=ctx,
        data={"status": "switched_to_main_frame", "sdk_equivalent": "page.frame_main()"},
    )


async def skyvern_frame_list(
    session_id: Annotated[str | None, Field(description="Browser session ID (pbs_...)")] = None,
    cdp_url: Annotated[str | None, Field(description="CDP WebSocket URL")] = None,
) -> dict[str, Any]:
    """List all frames (including iframes) on the current page.

    Returns each frame's index, name, URL, and whether it's the main frame.
    Use the index, name, or a CSS selector with skyvern_frame_switch to enter an iframe.
    """
    try:
        page, ctx = await get_page(session_id=session_id, cdp_url=cdp_url)
    except BrowserNotAvailableError:
        return make_result("skyvern_frame_list", ok=False, error=no_browser_error())

    with Timer() as timer:
        try:
            frames = await do_frame_list(page)
            timer.mark("sdk")
        except Exception as e:
            return make_result(
                "skyvern_frame_list",
                ok=False,
                browser_context=ctx,
                timing_ms=timer.timing_ms,
                error=make_error(ErrorCode.ACTION_FAILED, str(e), "Ensure a page is loaded first"),
            )

    return make_result(
        "skyvern_frame_list",
        browser_context=ctx,
        data={
            "frames": [{"index": f.index, "name": f.name, "url": f.url, "is_main": f.is_main} for f in frames],
            "count": len(frames),
            "sdk_equivalent": "await page.frame_list()",
        },
        timing_ms=timer.timing_ms,
    )


async def skyvern_find(
    by: Annotated[
        str,
        Field(description="Locator type: role, text, label, placeholder, alt, testid"),
    ],
    value: Annotated[
        str,
        Field(description="The text, role, label, placeholder, alt text, or test ID to match"),
    ],
    session_id: Annotated[str | None, Field(description="Browser session ID (pbs_...)")] = None,
    cdp_url: Annotated[str | None, Field(description="CDP WebSocket URL")] = None,
) -> dict[str, Any]:
    """Find elements using a semantic locator API — by role, text, label, placeholder, alt text, or test ID.
    Returns match count, text content, and visibility. Use to verify elements exist before interacting.
    """
    try:
        page, ctx = await get_page(session_id=session_id, cdp_url=cdp_url)
    except BrowserNotAvailableError:
        return make_result("skyvern_find", ok=False, error=no_browser_error())

    with Timer() as timer:
        try:
            result = await do_find(page, by=by, value=value)
            timer.mark("find")
        except GuardError as e:
            return make_result(
                "skyvern_find",
                ok=False,
                browser_context=ctx,
                timing_ms=timer.timing_ms,
                error=make_error(ErrorCode.INVALID_INPUT, str(e), e.hint),
            )
        except Exception as e:
            return make_result(
                "skyvern_find",
                ok=False,
                browser_context=ctx,
                timing_ms=timer.timing_ms,
                error=make_error(ErrorCode.ACTION_FAILED, str(e), "Check the locator type and value"),
            )

    return make_result(
        "skyvern_find",
        browser_context=ctx,
        data={
            "selector": result.selector,
            "count": result.count,
            "first_text": result.first_text,
            "first_visible": result.first_visible,
            "sdk_equivalent": f"page.{result.selector}",
        },
        timing_ms=timer.timing_ms,
    )


async def _ensure_clipboard_permissions(page: Any) -> None:
    """Grant clipboard permissions on the browser context (lazy, idempotent)."""
    try:
        await page.context.grant_permissions(["clipboard-read", "clipboard-write"])
    except Exception:
        LOG.debug("clipboard_permission_grant_skipped", exc_info=True)


async def skyvern_clipboard_read(
    session_id: Annotated[str | None, Field(description="Browser session ID.")] = None,
    cdp_url: Annotated[str | None, Field(description="CDP WebSocket URL.")] = None,
) -> dict[str, Any]:
    """Read text from the browser clipboard (whatever was last copied via Ctrl+C or clipboard_write).

    Returns the current clipboard text content. Requires secure context
    (HTTPS or localhost). Clipboard permissions are granted automatically
    on first use.
    """
    try:
        page, ctx = await get_page(session_id=session_id, cdp_url=cdp_url)
    except BrowserNotAvailableError:
        return make_result("skyvern_clipboard_read", ok=False, error=no_browser_error())

    with Timer() as timer:
        try:
            await _ensure_clipboard_permissions(page)
            text = await page.evaluate("() => navigator.clipboard.readText()")
            timer.mark("clipboard_read")
        except Exception as e:
            return make_result(
                "skyvern_clipboard_read",
                ok=False,
                browser_context=ctx,
                timing_ms=timer.timing_ms,
                error=make_error(
                    ErrorCode.ACTION_FAILED, str(e), "Ensure the page is a secure context (HTTPS or localhost)"
                ),
            )

    return make_result(
        "skyvern_clipboard_read",
        browser_context=ctx,
        data={"text": text},
        timing_ms=timer.timing_ms,
    )


async def skyvern_clipboard_write(
    text: Annotated[str, Field(description="Text to write to the clipboard.")],
    session_id: Annotated[str | None, Field(description="Browser session ID.")] = None,
    cdp_url: Annotated[str | None, Field(description="CDP WebSocket URL.")] = None,
) -> dict[str, Any]:
    """Copy text to the browser clipboard (as if the user pressed Ctrl+C).

    The text can then be pasted into form fields or read back with
    clipboard_read. Requires secure context (HTTPS or localhost).
    Clipboard permissions are granted automatically on first use.
    """
    try:
        page, ctx = await get_page(session_id=session_id, cdp_url=cdp_url)
    except BrowserNotAvailableError:
        return make_result("skyvern_clipboard_write", ok=False, error=no_browser_error())

    with Timer() as timer:
        try:
            await _ensure_clipboard_permissions(page)
            await page.evaluate("(t) => navigator.clipboard.writeText(t)", text)
            timer.mark("clipboard_write")
        except Exception as e:
            return make_result(
                "skyvern_clipboard_write",
                ok=False,
                browser_context=ctx,
                timing_ms=timer.timing_ms,
                error=make_error(
                    ErrorCode.ACTION_FAILED, str(e), "Ensure the page is a secure context (HTTPS or localhost)"
                ),
            )

    return make_result(
        "skyvern_clipboard_write",
        browser_context=ctx,
        data={"written": True, "length": len(text)},
        timing_ms=timer.timing_ms,
    )


# ---------------------------------------------------------------------------
# Observe — scoped accessibility tree snapshot
# ---------------------------------------------------------------------------


def _observe_frame_error(error: ObserveFrameError) -> dict[str, Any]:
    frame_id = error.frame_name or error.frame_url or "<unnamed>"
    return make_error(
        ErrorCode.ACTION_FAILED,
        f"Failed to observe frame {frame_id!r}",
        "Use skyvern_frame_list to verify the frame, skyvern_frame_main to leave it, "
        "or switch again before retrying selector-based click/type tools",
        details={"frame_name": error.frame_name, "frame_url": error.frame_url},
    )


async def skyvern_observe(
    selector: Annotated[
        str | None,
        Field(
            description=(
                f"{DIRECT_TARGET_DESCRIPTION} CSS selector to scope the snapshot "
                "(e.g., 'form#login'). Omit for full page."
            )
        ),
    ] = None,
    session_id: Annotated[str | None, Field(description="Browser session ID (pbs_...)")] = None,
    cdp_url: Annotated[str | None, Field(description="CDP WebSocket URL")] = None,
    interactive_only: Annotated[
        bool,
        Field(description="Only return interactive elements (buttons, inputs, links). Default true."),
    ] = True,
    max_elements: Annotated[
        int,
        Field(description="Max elements to return. Default 50.", ge=1, le=200),
    ] = 50,
    include_values: Annotated[
        bool,
        Field(
            description="Include current values for non-password inputs. "
            "Password values are never returned. Default false."
        ),
    ] = False,
) -> dict[str, Any]:
    """Snapshot interactive elements with refs reusable in this browser session until the next observe or page/document context change (rarely earlier — on 'Unknown ref', re-observe). Input values are omitted by default; set include_values=True to return non-password values. Password values are never returned."""
    try:
        page, ctx = await get_page(session_id=session_id, cdp_url=cdp_url)
    except BrowserNotAvailableError:
        return make_result("skyvern_observe", ok=False, error=no_browser_error())

    observe_page_key = page_ref_key(page)
    generation = session_ref_generation(session_id=ctx.session_id, cdp_url=ctx.cdp_url)

    with Timer() as timer:
        try:
            result = await do_observe(
                page,
                selector=selector,
                interactive_only=interactive_only,
                max_elements=max_elements,
                include_values=include_values,
            )
            timer.mark("sdk")
        except ObserveFrameError as e:
            clear_session_ref_map(session_id=ctx.session_id, cdp_url=ctx.cdp_url)
            return make_result(
                "skyvern_observe",
                ok=False,
                browser_context=ctx,
                timing_ms=timer.timing_ms,
                error=_observe_frame_error(e),
            )
        except Exception as e:
            return make_result(
                "skyvern_observe",
                ok=False,
                browser_context=ctx,
                timing_ms=timer.timing_ms,
                error=make_error(ErrorCode.ACTION_FAILED, str(e), "Check that the page is loaded"),
            )

    elements = serialize_elements(result.elements)
    replace_session_ref_map(
        ref_map_from_elements(elements),
        session_id=ctx.session_id,
        cdp_url=ctx.cdp_url,
        generation=generation,
        page_key=observe_page_key,
    )
    hint = (
        f"Found {result.element_count} interactive elements"
        f"{f' (of {result.total_on_page} total on page)' if result.total_on_page > result.element_count else ''}. "
        "Use these refs in skyvern_execute steps, e.g.: "
        '{tool: "click", params: {ref: "e0"}}. '
        "Refs remain valid across calls in this browser session until the next skyvern_observe, "
        "skyvern_navigate, same-tab navigation, or tab/frame switch. Same-document DOM changes can also "
        "invalidate ordinal refs; re-observe on 'Unknown ref' or unexpected failures. "
        "Input values are omitted unless include_values=true; password values are never returned."
    )
    return make_result(
        "skyvern_observe",
        browser_context=ctx,
        data={
            "url": result.url,
            "title": result.title,
            "elements": elements,
            "element_count": result.element_count,
            "total_on_page": result.total_on_page,
            "hint": hint,
        },
        timing_ms=timer.timing_ms,
    )


# ---------------------------------------------------------------------------
# Execute — batch multi-step execution
# ---------------------------------------------------------------------------

# DESIGN-1: Maps execute step tool names to existing MCP tool function names.
# Dispatch through existing tools to inherit security guards.
_TOOL_NAME_MAP: dict[str, str] = {
    "navigate": "skyvern_navigate",
    "click": "skyvern_click",
    "type": "skyvern_type",
    "press_key": "skyvern_press_key",
    "select_option": "skyvern_select_option",
    "hover": "skyvern_hover",
    "scroll": "skyvern_scroll",
    "wait": "skyvern_wait",
    "screenshot": "skyvern_screenshot",
    "evaluate": "skyvern_evaluate",
}

# Accepted user-facing params for each dispatched tool (excludes session_id/cdp_url).
_TOOL_ACCEPTED_PARAMS: dict[str, frozenset[str]] = {
    "navigate": frozenset({"url", "timeout", "wait_until"}),
    "click": frozenset({"intent", "selector", "timeout", "click_count", "button"}),
    "type": frozenset({"text", "intent", "selector", "clear_first", "press_enter", "timeout"}),
    "press_key": frozenset({"key", "intent", "selector", "timeout"}),
    "select_option": frozenset({"value", "intent", "selector", "timeout", "by_label"}),
    "hover": frozenset({"intent", "selector", "timeout"}),
    "scroll": frozenset({"direction", "amount", "intent", "selector"}),
    "wait": frozenset({"time_ms", "intent", "selector", "state", "timeout", "poll_interval_ms"}),
    "screenshot": frozenset({"full_page", "selector", "inline"}),
    "evaluate": frozenset({"expression"}),
}


async def _dispatch_step(
    step: ExecuteStep,
    ref_map: dict[str, dict[str, Any]],
    session_id: str | None,
    cdp_url: str | None,
    page_key: tuple[int, int | None, str, str | None] | None = None,
    on_observe_page: Callable[[tuple[int, int | None, str, str | None]], None] | None = None,
) -> dict[str, Any] | None:
    """Route a step to the appropriate handler, resolving refs to selectors."""
    params = dict(step.params)

    # Resolve ref to selector if present. Refs bind to the page/frame they were
    # observed on, so re-check identity against the page this step will actually
    # run on — a popup or external tab change may have moved it mid-batch.
    if ref := params.pop("ref", None):
        current_page, _ = await get_page(session_id=session_id, cdp_url=cdp_url)
        current_key = page_ref_key(current_page)
        elem = ref_map.get(ref) if page_key is None or current_key == page_key else None
        if elem is None:
            elem = get_session_ref(ref, session_id=session_id, cdp_url=cdp_url, page_key=current_key)
        if elem is None:
            raise ValueError(f"Unknown ref '{ref}' — call observe first or check ref exists")
        params["selector"] = ref_to_selector(elem)

    # Observe is handled inline (not an existing MCP tool)
    if step.tool == "observe":
        from skyvern.cli.core.browser_ops import do_observe as _do_observe

        page, _ = await get_page(session_id=session_id, cdp_url=cdp_url)
        if on_observe_page is not None:
            on_observe_page(page_ref_key(page))
        accepted = {"selector", "interactive_only", "max_elements", "include_values"}
        filtered = {k: v for k, v in params.items() if k in accepted}
        try:
            result = await _do_observe(page, **filtered)
        except ObserveFrameError as e:
            clear_session_ref_map(session_id=session_id, cdp_url=cdp_url)
            raise ToolStepError(_observe_frame_error(e)) from e
        return {
            "elements": serialize_elements(result.elements),
            "element_count": result.element_count,
            "total_on_page": result.total_on_page,
        }

    # DESIGN-1: Dispatch through existing MCP tool functions via module lookup
    import skyvern.cli.mcp_tools.browser as _browser_mod

    fn_name = _TOOL_NAME_MAP.get(step.tool)
    if fn_name is None:
        raise ValueError(f"Unknown tool '{step.tool}' — allowed: {sorted(_ALLOWED_EXECUTE_TOOLS)}")

    tool_fn = getattr(_browser_mod, fn_name)

    # Filter params to only those accepted by the target tool to prevent
    # TypeError from unexpected keyword arguments.
    accepted_params = _TOOL_ACCEPTED_PARAMS.get(step.tool, frozenset())
    filtered_params = {k: v for k, v in params.items() if k in accepted_params}
    filtered_params["session_id"] = session_id
    filtered_params["cdp_url"] = cdp_url

    tool_result = await tool_fn(**filtered_params)

    if not tool_result.get("ok", False):
        raise ToolStepError(tool_result.get("error") or {})

    return tool_result.get("data")


async def skyvern_execute(
    steps: Annotated[
        list[dict[str, Any]],
        Field(
            description=(
                "Array of {tool, params} step objects to execute sequentially. "
                "Within params, refs from skyvern_observe are direct targets across calls in the same browser session. "
                "The next skyvern_observe or page/document context change invalidates them; they can occasionally "
                "expire early. Same-document DOM changes can also invalidate ordinal refs; on 'Unknown ref' or "
                "unexpected failures, re-observe. "
                f"{DIRECT_TARGET_DESCRIPTION}"
            )
        ),
    ],
    session_id: Annotated[str | None, Field(description="Browser session ID (pbs_...)")] = None,
    cdp_url: Annotated[str | None, Field(description="CDP WebSocket URL")] = None,
    stop_on_error: Annotated[
        bool,
        Field(description="Stop at first failure (true) or continue past errors (false). Default true."),
    ] = True,
) -> dict[str, Any]:
    """Execute browser operations using current-session refs until the next observe or page/document context change.
    Allowed tools: navigate, click, type, press_key, select_option, hover, scroll, wait, observe, screenshot, evaluate."""
    if not steps:
        return make_result(
            "skyvern_execute",
            data={
                "steps_completed": 0,
                "steps_total": 0,
                "results": [],
                "error_step": None,
            },
        )

    if len(steps) > MAX_EXECUTE_STEPS:
        return make_result(
            "skyvern_execute",
            ok=False,
            error=make_error(
                ErrorCode.INVALID_INPUT,
                f"Too many steps: {len(steps)} (max {MAX_EXECUTE_STEPS})",
                f"Split into multiple skyvern_execute calls of {MAX_EXECUTE_STEPS} steps or fewer",
            ),
        )

    # Validate step structure and tool names upfront
    parsed_steps: list[ExecuteStep] = []
    for i, raw in enumerate(steps):
        tool = raw.get("tool")
        if not tool:
            return make_result(
                "skyvern_execute",
                ok=False,
                error=make_error(
                    ErrorCode.INVALID_INPUT,
                    f"Step {i} missing 'tool' field",
                    "Each step must have {tool: 'name', params: {...}}",
                ),
            )
        if tool not in _ALLOWED_EXECUTE_TOOLS:
            return make_result(
                "skyvern_execute",
                ok=False,
                error=make_error(
                    ErrorCode.INVALID_INPUT,
                    f"Step {i}: unknown tool '{tool}'",
                    f"Allowed tools: {sorted(_ALLOWED_EXECUTE_TOOLS)}",
                ),
            )
        parsed_steps.append(ExecuteStep(tool=tool, params=raw.get("params", {})))

    # Verify we can reach the browser before executing anything
    try:
        page, ctx = await get_page(session_id=session_id, cdp_url=cdp_url)
    except BrowserNotAvailableError:
        return make_result("skyvern_execute", ok=False, error=no_browser_error())

    batch_page_key = page_ref_key(page)

    # Generation captured before each observe dispatch so a snapshot that raced
    # a concurrent navigation/context switch is discarded, not committed.
    observe_generation: dict[str, int] = {}
    observe_page_key: tuple[int, int | None, str, str | None] | None = None

    def capture_observe_page_key(page_key: tuple[int, int | None, str, str | None]) -> None:
        nonlocal observe_page_key
        observe_page_key = page_key

    async def dispatch(step: ExecuteStep, ref_map: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
        if step.tool == "observe":
            observe_generation["value"] = session_ref_generation(session_id=ctx.session_id, cdp_url=ctx.cdp_url)
        return await _dispatch_step(
            step,
            ref_map,
            session_id=session_id,
            cdp_url=cdp_url,
            page_key=batch_page_key,
            on_observe_page=capture_observe_page_key if step.tool == "observe" else None,
        )

    def publish_observe_refs(ref_map: dict[str, dict[str, Any]]) -> bool:
        nonlocal batch_page_key
        if observe_page_key is None:
            return False
        accepted = replace_session_ref_map(
            ref_map,
            session_id=ctx.session_id,
            cdp_url=ctx.cdp_url,
            generation=observe_generation.get("value"),
            page_key=observe_page_key,
        )
        if accepted:
            batch_page_key = observe_page_key
        return accepted

    with Timer() as timer:
        result = await do_execute(
            dispatch,
            parsed_steps,
            stop_on_error=stop_on_error,
            on_ref_map_update=publish_observe_refs,
        )
        timer.mark("sdk")

    step_results = []
    for sr in result.results:
        entry: dict[str, Any] = {"step": sr.step, "tool": sr.tool, "ok": sr.ok, "wall_ms": sr.wall_ms}
        if sr.data:
            entry["data"] = sr.data
        if sr.error:
            entry["error"] = sr.error
        step_results.append(entry)

    return make_result(
        "skyvern_execute",
        ok=result.error_step is None,
        data={
            "steps_completed": result.steps_completed,
            "steps_total": result.steps_total,
            "results": step_results,
            "error_step": result.error_step,
        },
        timing_ms=timer.timing_ms,
    )
