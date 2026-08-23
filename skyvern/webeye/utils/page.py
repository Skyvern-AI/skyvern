from __future__ import annotations

import asyncio
import base64
import gc
import json
import re
import time
import urllib.parse
from collections.abc import Awaitable, Callable
from enum import StrEnum
from io import BytesIO
from typing import TYPE_CHECKING, Any, NamedTuple

import structlog
from opentelemetry import trace as otel_trace
from PIL import Image
from playwright._impl._errors import Error as PlaywrightError
from playwright.async_api import ElementHandle, Frame, Locator, Page

from skyvern.constants import PAGE_CONTENT_TIMEOUT, SKYVERN_DIR
from skyvern.exceptions import (
    ElementTreeBuildFailed,
    FailedToTakeScreenshot,
    ScreenshotTargetClosed,
    SkyvernPageAnalysisTimeout,
)
from skyvern.forge.sdk.browser_action_preflight import policy_observation_enabled, record_observed_tabs
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.settings_manager import SettingsManager
from skyvern.forge.sdk.trace import apply_context_attrs, traced, traced_span
from skyvern.webeye.browser_driver_errors import is_driver_error, is_driver_timeout_error
from skyvern.webeye.browser_engine import BrowserEngineSelection
from skyvern.webeye.browser_errors import BrowserTargetClosedError
from skyvern.webeye.browser_health import BrowserOperation
from skyvern.webeye.browser_object_predicates import is_page_like
from skyvern.webeye.main_world_eval import evaluate_in_main_world, get_main_world_prefix
from skyvern.webeye.navigation import redact_url_secrets

if TYPE_CHECKING:
    from skyvern.webeye.browser_state import BrowserState

LOG = structlog.get_logger()

SECRET_VISUAL_MASK_STYLE_ID = "skyvern-secret-mask-style"
SECRET_VISUAL_MASK_ATTRIBUTE = "data-skyvern-secret-mask"
SECRET_VISUAL_MASK_CSS_RULE = '[data-skyvern-secret-mask="true"] { -webkit-text-security: disc !important; }'
SECRET_VISUAL_MASK_BLUR_FILTER = "blur(6px)"

_SECRET_VISUAL_MASK_BODY = f"""
    const ownerDocument = element.ownerDocument;
    if (!ownerDocument) {{
        return;
    }}

    const root = element.getRootNode();
    const isShadowRoot = root instanceof ShadowRoot;
    let style = root.querySelector({json.dumps(f"#{SECRET_VISUAL_MASK_STYLE_ID}")});
    if (!style) {{
        style = ownerDocument.createElement("style");
        style.id = {json.dumps(SECRET_VISUAL_MASK_STYLE_ID)};
        if (isShadowRoot) {{
            root.appendChild(style);
        }} else {{
            (ownerDocument.head || ownerDocument.documentElement).appendChild(style);
        }}
    }}
    style.textContent = {json.dumps(SECRET_VISUAL_MASK_CSS_RULE)};

    element.setAttribute({json.dumps(SECRET_VISUAL_MASK_ATTRIBUTE)}, "true");

    const elementTagName = element.tagName ? element.tagName.toLowerCase() : "";
    if (elementTagName !== "input" && elementTagName !== "textarea") {{
        element.style.filter = {json.dumps(SECRET_VISUAL_MASK_BLUR_FILTER)};
    }}
"""

SECRET_VISUAL_MASK_SCRIPT = f"""
    (element) => {{
        {_SECRET_VISUAL_MASK_BODY}
    }}
"""

_SECRET_VISUAL_MASK_ACTIVE_ELEMENT_SCRIPT = f"""
    () => {{
        const element = document.activeElement;
        if (!element) {{
            return;
        }}

        const activeElementTagName = element.tagName ? element.tagName.toLowerCase() : "";
        if (
            activeElementTagName === "input"
            && String(element.getAttribute("type") || "").toLowerCase() === "password"
        ) {{
            return;
        }}

        {_SECRET_VISUAL_MASK_BODY}
    }}
"""


async def apply_secret_visual_mask_to_active_element(page: Page) -> None:
    try:
        await SkyvernFrame.evaluate(page, expression=_SECRET_VISUAL_MASK_ACTIVE_ELEMENT_SCRIPT)
    except Exception:
        LOG.warning("Failed to apply secret visual mask to active element", exc_info=True)


_SCREENSHOT_TARGET_CLOSED_MESSAGE = "Target page, context or browser has been closed"
_SELECTED_SCREENSHOT_TARGET_CLOSED_MESSAGES = (
    "target page, context or browser has been closed",
    "target closed",
    "target is closed",
    "target was closed",
    "target has been closed",
    "target was disposed",
)


async def _safe_tab_title(page: Page) -> str:
    try:
        return await asyncio.wait_for(page.title(), timeout=1.0)
    except asyncio.CancelledError:
        raise
    except Exception:
        LOG.debug("tab_title_fetch_failed", url=page.url)
        return ""


async def build_open_tabs_context(
    browser_state: BrowserState,
    working_page: Page | None,
) -> str | None:
    if working_page is None:
        return None
    pages = await browser_state.list_valid_pages()
    # Recorded before the single-tab early return: a SWITCH_TAB judged against a stale record from
    # an earlier multi-tab prompt is exactly the mismatch the record exists to surface.
    record_observed_tabs([p.url for p in pages])
    if len(pages) <= 1:
        return None
    # Fetch titles concurrently so a few slow tabs don't add N×timeout latency to every iteration.
    titles = await asyncio.gather(*(_safe_tab_title(p) for p in pages))
    lines: list[str] = []
    for i, (p, title) in enumerate(zip(pages, titles)):
        marker = " [current]" if p == working_page else ""
        url = p.url
        if len(url) > 120:
            url = url[:117] + "..."
        if len(title) > 80:
            title = title[:77] + "..."
        entry = f"Tab {i}{marker}: {url}"
        if title:
            entry += f" ({title})"
        lines.append(entry)
    return "\n".join(lines)


async def capture_open_tab_screenshots(
    browser_state: BrowserState,
    *,
    persist: Callable[[bytes], Awaitable[None]],
    skip_single_tab: bool = False,
) -> int:
    """Screenshot every open tab and persist each frame via ``persist``; returns the count.

    Shared end-state per-tab capture. The caller's ``persist`` owns the artifact sink and type.
    ``skip_single_tab`` returns 0 when only one tab is open, for callers that already capture the
    active page separately. Best-effort: runs post-success, never raises.
    """
    config = SettingsManager.get_settings()
    captured = 0
    try:
        # max_pages=0 enumerates without list_valid_pages' close-oldest behavior, so we never
        # close the very tabs we are trying to capture.
        pages = await browser_state.list_valid_pages(max_pages=0)
        if not pages or (skip_single_tab and len(pages) <= 1):
            return 0
        per_tab_timeout = config.BROWSER_SCREENSHOT_TIMEOUT_MS / 1000
        # Overall budget so a few still-loading tabs can't stall the post-success path.
        async with asyncio.timeout(config.COMPLETION_TAB_SCREENSHOTS_TOTAL_TIMEOUT_SECONDS):
            # The cap setting is task_v2-named but governs every caller here.
            for page in pages[: config.MAX_COMPLETION_TAB_SCREENSHOTS_PER_TASK_V2]:
                frames: list[bytes] = []
                try:
                    async with asyncio.timeout(per_tab_timeout):
                        try:
                            # Front each tab so Chromium paints lazily-rendered background content.
                            await page.bring_to_front()
                        except Exception:
                            LOG.debug("Failed to bring tab to front before screenshot", exc_info=True)
                        frames = await SkyvernFrame.take_split_screenshots(
                            page=page,
                            scroll=False,
                            engine_selection=browser_state.engine_selection,
                        )
                except Exception:
                    LOG.warning("Failed to capture screenshot for an open tab", exc_info=True)
                    continue
                for frame in frames:
                    try:
                        await persist(frame)
                        captured += 1
                    except Exception:
                        LOG.warning("Failed to persist open-tab screenshot", exc_info=True)
    except Exception:
        LOG.warning("Aborted capturing open-tab screenshots", captured=captured, exc_info=True)
    return captured


_JS_TOP_LEVEL_DECL_RE = re.compile(
    r"^(?:async\s+function|function|class|let|const|var)\s+([A-Za-z_$][A-Za-z0-9_$]*)",
    re.MULTILINE,
)


def _as_element_tree_pair(result: Any) -> tuple[list[dict], list[dict]] | None:
    """The ``[elements, element_tree]`` pair a domUtils tree builder returns, or None if it didn't.

    Every builder in domUtils.js returns two lists or throws, so anything else means the JS that ran
    was not the injected bundle -- the execution context was replaced without an error reaching us,
    or the export was shadowed. Unpacking it blindly turns that into a bare TypeError at the
    assignment, which names neither the frame nor what came back. Both entries have to be lists:
    ``pop_destination_facts`` treats a non-list as empty, so a pair like ``[None, None]`` would
    otherwise reach callers as a successful build of a page with no elements.
    """
    if isinstance(result, (list, tuple)) and len(result) == 2 and all(isinstance(part, list) for part in result):
        return result[0], result[1]
    return None


def _describe_non_pair(result: Any) -> str:
    if isinstance(result, (list, tuple)):
        return f"{type(result).__name__} of {len(result)}: {[type(item).__name__ for item in result[:2]]}"
    return type(result).__name__


def _wrap_js_in_isolated_scope(script: str) -> str:
    # page.evaluate runs string scripts through a sloppy indirect eval, which hoists
    # top-level declarations into the page's global scope and throws
    # "Identifier 'X' has already been declared" when the site's own JS holds a global
    # lexical binding with the same name. Scope the script in an IIFE and export via
    # property writes, which never collide; typeof guards drop names the column-0 regex
    # matched inside block comments.
    names = sorted(set(_JS_TOP_LEVEL_DECL_RE.findall(script)))
    exports = "\n".join(f'if (typeof {name} !== "undefined") globalThis.{name} = {name};' for name in names)
    return f"(() => {{\n{script}\n{exports}\n}})();"


def load_js_script() -> str:
    # TODO: Handle file location better. This is a hacky way to find the file location.
    path = f"{SKYVERN_DIR}/webeye/scraper/domUtils.js"
    try:
        # TODO: Implement TS of domUtils.js and use the complied JS file instead of the raw JS file.
        # This will allow our code to be type safe.
        with open(path, encoding="utf-8") as f:
            return _wrap_js_in_isolated_scope(f.read())
    except FileNotFoundError as e:
        LOG.exception("Failed to load the JS script", path=path)
        raise e


JS_FUNCTION_DEFS = load_js_script()

_NAVIGATION_RECOVERY_MAX_ATTEMPTS = 4
_NAVIGATION_SETTLE_TIMEOUT_MS = 3000


def _is_engine_error(exc: BaseException, engine_selection: BrowserEngineSelection | None) -> bool:
    """Whether ``exc`` belongs to THIS run's selected browser-engine error family. Falls back to every
    installed Playwright-family driver's identity when no engine is pinned, because an unpinned caller
    can hold a page from either driver package the image installs; a foreign engine's error (or an
    unrelated exception) is rejected and left to propagate."""
    return engine_selection.is_engine_error(exc) if engine_selection is not None else is_driver_error(exc)


def is_engine_timeout(exc: BaseException, engine_selection: BrowserEngineSelection | None) -> bool:
    return (
        engine_selection.is_engine_timeout_error(exc) if engine_selection is not None else is_driver_timeout_error(exc)
    )


def _is_readiness_timeout(exc: BaseException, engine_selection: BrowserEngineSelection | None) -> bool:
    return isinstance(exc, (asyncio.TimeoutError, SkyvernPageAnalysisTimeout)) or is_engine_timeout(
        exc, engine_selection
    )


def _is_navigation_context_lost(error_msg: str) -> bool:
    if "Execution context was destroyed" in error_msg:
        return True
    if "Cannot find context with specified id" in error_msg:
        return True
    return "ReferenceError" in error_msg and "is not defined" in error_msg


def _is_json_inlinable(arg: Any) -> bool:
    # ElementHandle / JSHandle aren't JSON-serialisable; those must keep
    # Playwright's own marshalling instead of being inlined into Runtime.evaluate.
    try:
        json.dumps(arg)
    except (TypeError, ValueError):
        return False
    return True


async def _dispatch_evaluate(frame: Page | Frame, expression: str, arg: Any | None, *, force_cdp: bool = False) -> Any:
    # force_cdp callers require the CDP main-world path regardless of prefix, so
    # short-circuit before the page/frame/JSON heuristics below. That path
    # dereferences page-only APIs (``page.context``); a Frame here is a caller
    # contract violation, so reject it explicitly instead of failing with an
    # incidental AttributeError deep inside the main-world hook.
    if force_cdp:
        if not is_page_like(frame):
            raise TypeError("force_cdp evaluation requires a top-level Page, not a Frame")
        return await evaluate_in_main_world(frame, expression, arg, force_cdp=True)
    # Page + prefix + JSON-safe arg → main-world hook (preserves the marker).
    # Iframe Frames and non-JSON args fall back to per-frame evaluate so iframe
    # contexts and Playwright handle-marshalling keep working.
    if not is_page_like(frame):
        return await frame.evaluate(expression=expression, arg=arg)
    context = frame.context
    # A page whose context is None (an engine's pre-attach/edge state) can't key
    # the prefix WeakKeyDictionary — get_main_world_prefix(None) would raise
    # instead of returning the no-prefix fallback, so take direct evaluate here.
    if context is None or get_main_world_prefix(context) is None:
        return await frame.evaluate(expression=expression, arg=arg)
    if arg is not None and not _is_json_inlinable(arg):
        return await frame.evaluate(expression=expression, arg=arg)
    return await evaluate_in_main_world(frame, expression, arg)


async def _wait_for_navigation_settle(
    frame: Page | Frame,
    timeout_ms: float,
    engine_selection: BrowserEngineSelection | None = None,
) -> None:
    if timeout_ms <= 0:
        return
    try:
        await frame.wait_for_load_state("networkidle", timeout=timeout_ms)
    except Exception as e:
        if not _is_engine_error(e, engine_selection):
            raise
        return


async def _wait_for_screenshot_load_state(
    page: Page,
    timeout_ms: float,
    engine_selection: BrowserEngineSelection | None = None,
) -> None:
    # Best-effort readiness guard before capturing. 'domcontentloaded' fires far
    # earlier than 'load'; pages with streaming/long-polling/SSE/websockets or a
    # persistent spinner may never fire 'load', so a timeout here must be
    # non-fatal — the capture has its own (separate) timeout budget.
    if timeout_ms <= 0:
        return
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
    except Exception as exc:
        if not _is_engine_error(exc, engine_selection):
            raise
        LOG.warning("Page did not reach domcontentloaded before screenshot; capturing current state anyway")


def _is_screenshot_target_closed(
    error: BaseException,
    engine_selection: BrowserEngineSelection | None = None,
) -> bool:
    if engine_selection is not None:
        error_message = str(error).lower()
        if "crash" in error_message:
            return False
        if isinstance(engine_selection.classify_error(error), BrowserTargetClosedError) and any(
            message in error_message for message in _SELECTED_SCREENSHOT_TARGET_CLOSED_MESSAGES
        ):
            return True
        # A bound stock-Playwright selection surfaces its canonical target-close as a base Error, not the
        # rich TargetClosedError family, so classify_error returns BrowserAutomationError above. Fall back to
        # the single canonical message only — never a broad substring — so renderer crashes stay generic.
        return _is_engine_error(error, engine_selection) and _SCREENSHOT_TARGET_CLOSED_MESSAGE.lower() in error_message
    return _is_engine_error(error, engine_selection) and _SCREENSHOT_TARGET_CLOSED_MESSAGE in str(error)


def _load_cursor_overlay_js() -> str:
    path = f"{SKYVERN_DIR}/webeye/scraper/cursorOverlay.js"
    with open(path, encoding="utf-8") as f:
        return f.read()


_CURSOR_OVERLAY_JS = _load_cursor_overlay_js()


class ScreenshotMode(StrEnum):
    LITE = "lite"
    DETAILED = "detailed"


async def _page_screenshot_helper(
    page: Page,
    file_path: str | None = None,
    full_page: bool = False,
    timeout: float = SettingsManager.get_settings().BROWSER_SCREENSHOT_TIMEOUT_MS,
    engine_selection: BrowserEngineSelection | None = None,
) -> bytes:
    if SettingsManager.get_settings().BROWSER_CURSOR_VISUALIZATION:
        try:
            await SkyvernFrame.hide_cursor_overlay(page)
        except Exception:
            pass
    try:
        return await page.screenshot(
            path=file_path,
            timeout=timeout,
            full_page=full_page,
            animations="disabled",
        )
    except Exception as timeout_error:
        if not is_engine_timeout(timeout_error, engine_selection):
            raise
        LOG.info(
            f"Timeout error while taking screenshot: {str(timeout_error)}. Going to take a screenshot again with animation allowed."
        )
        return await page.screenshot(
            path=file_path,
            timeout=timeout,
            full_page=full_page,
            animations="allow",
        )
    finally:
        if SettingsManager.get_settings().BROWSER_CURSOR_VISUALIZATION:
            try:
                await SkyvernFrame.show_cursor_overlay(page)
            except Exception:
                pass


async def _current_viewpoint_screenshot_helper(
    page: Page,
    file_path: str | None = None,
    full_page: bool = False,
    timeout: float = SettingsManager.get_settings().BROWSER_SCREENSHOT_TIMEOUT_MS,
    mode: ScreenshotMode = ScreenshotMode.DETAILED,
    engine_selection: BrowserEngineSelection | None = None,
) -> bytes:
    if page.is_closed():
        LOG.info(
            "Skipping screenshot because target is closed",
            full_page=full_page,
            mode=mode.value if hasattr(mode, "value") else str(mode),
        )
        raise ScreenshotTargetClosed(error_message="Page is closed")

    # Capture page context for debugging screenshot issues
    url = page.url
    try:
        viewport = page.viewport_size
        viewport_info = f"{viewport['width']}x{viewport['height']}" if viewport else "unknown"
    except Exception:
        viewport_info = "unknown"

    try:
        if mode == ScreenshotMode.DETAILED:
            await _wait_for_screenshot_load_state(
                page,
                timeout_ms=SettingsManager.get_settings().BROWSER_SCREENSHOT_LOAD_STATE_TIMEOUT_MS,
                engine_selection=engine_selection,
            )
        start_time = time.time()
        screenshot: bytes = b""
        if file_path:
            screenshot = await _page_screenshot_helper(
                page=page,
                file_path=file_path,
                full_page=full_page,
                timeout=timeout,
                engine_selection=engine_selection,
            )
        else:
            screenshot = await _page_screenshot_helper(
                page=page,
                full_page=full_page,
                timeout=timeout,
                engine_selection=engine_selection,
            )
        end_time = time.time()
        LOG.debug(
            "Screenshot taking time",
            screenshot_time=end_time - start_time,
            file_path=file_path,
        )
        skyvern_context.record_browser_success()
        return screenshot
    except Exception as e:
        if engine_selection is not None and not _is_engine_error(e, engine_selection):
            raise
        if is_engine_timeout(e, engine_selection):
            skyvern_context.record_browser_timeout(BrowserOperation.SCREENSHOT)
            LOG.warning(
                "Screenshot timeout",
                timeout_ms=timeout,
                url=url,
                viewport=viewport_info,
                full_page=full_page,
                mode=mode.value if hasattr(mode, "value") else str(mode),
                error=str(e),
            )
            raise FailedToTakeScreenshot(error_message=str(e)) from e
        if _is_screenshot_target_closed(e, engine_selection):
            LOG.info(
                "Skipping screenshot because target closed during capture",
                url=url,
                viewport=viewport_info,
                full_page=full_page,
                mode=mode.value if hasattr(mode, "value") else str(mode),
            )
            raise ScreenshotTargetClosed(error_message=str(e)) from e
        LOG.error(
            "Screenshot failed",
            url=url,
            viewport=viewport_info,
            full_page=full_page,
            error=str(e),
            exc_info=True,
        )
        raise FailedToTakeScreenshot(error_message=str(e)) from e


async def take_element_screenshot(
    locator: Locator,
    timeout: float = SettingsManager.get_settings().BROWSER_SCREENSHOT_TIMEOUT_MS,
    engine_selection: BrowserEngineSelection | None = None,
) -> bytes:
    try:
        page = locator.page
    except AssertionError as e:
        raise FailedToTakeScreenshot(error_message="Page is unavailable") from e

    if page.is_closed():
        raise FailedToTakeScreenshot(error_message="Page is closed")
    try:
        return await locator.screenshot(timeout=timeout, animations="disabled")
    except Exception as error:
        if not _is_engine_error(error, engine_selection):
            raise
        if not is_engine_timeout(error, engine_selection):
            raise FailedToTakeScreenshot(error_message=str(error)) from error
        try:
            return await locator.screenshot(timeout=timeout, animations="allow")
        except Exception as retry_error:
            if not _is_engine_error(retry_error, engine_selection):
                raise
            raise FailedToTakeScreenshot(error_message=str(retry_error)) from retry_error


async def _scrolling_screenshots_helper(
    page: Page,
    url: str | None = None,
    draw_boxes: bool = False,
    max_number: int = SettingsManager.get_settings().MAX_NUM_SCREENSHOTS,
    mode: ScreenshotMode = ScreenshotMode.DETAILED,
    engine_selection: BrowserEngineSelection | None = None,
) -> tuple[list[bytes], list[int]]:
    # page is the main frame and the index must be 0
    skyvern_page = await SkyvernFrame.create_instance(frame=page, engine_selection=engine_selection)
    frame = "main.frame"
    frame_index = 0

    # DEPRECATED: visual bounding box overlays are no longer rendered during scraping.
    # ``draw_boxes`` is False by default for all scrape callers; the ``if draw_boxes:``
    # branches below are retained briefly for backwards compatibility and are
    # scheduled for removal. The LITE-mode override is kept as a defensive guard.
    if mode == ScreenshotMode.LITE:
        draw_boxes = False

    screenshots: list[bytes] = []
    positions: list[int] = []
    if await skyvern_page.is_window_scrollable():
        scroll_y_px_old = -30.0
        _, initial_scroll_height = await skyvern_page.get_scroll_width_and_height()
        scroll_y_px = await skyvern_page.scroll_to_top(draw_boxes=draw_boxes, frame=frame, frame_index=frame_index)
        # Checking max number of screenshots to prevent infinite loop
        # We are checking the difference between the old and new scroll_y_px to determine if we have reached the end of the
        # page. If the difference is less than 25, we assume we have reached the end of the page.
        while abs(scroll_y_px_old - scroll_y_px) > 25 and len(screenshots) < max_number:
            # check if the scroll height changed, if so, rebuild the element tree
            _, scroll_height = await skyvern_page.get_scroll_width_and_height()
            if scroll_height != initial_scroll_height:
                LOG.debug(
                    "Scroll height changed, rebuild the element tree",
                    scroll_height=scroll_height,
                    initial_scroll_height=initial_scroll_height,
                )
                await skyvern_page.build_tree_from_body(frame_name=frame, frame_index=frame_index)
                initial_scroll_height = scroll_height

            screenshot = await _current_viewpoint_screenshot_helper(
                page=page,
                mode=mode,
                engine_selection=engine_selection,
            )
            screenshots.append(screenshot)
            positions.append(int(scroll_y_px))
            scroll_y_px_old = scroll_y_px
            LOG.debug("Scrolling to next page", url=url, num_screenshots=len(screenshots))
            scroll_y_px = await skyvern_page.scroll_to_next_page(
                draw_boxes=draw_boxes,
                frame=frame,
                frame_index=frame_index,
                need_overlap=(mode == ScreenshotMode.DETAILED),
            )
            LOG.debug(
                "Scrolled to next page",
                scroll_y_px=scroll_y_px,
                scroll_y_px_old=scroll_y_px_old,
            )
        if draw_boxes:
            await skyvern_page.remove_bounding_boxes()
        await skyvern_page.scroll_to_top(draw_boxes=False, frame=frame, frame_index=frame_index)

        if mode == ScreenshotMode.DETAILED:
            # wait until animation ends, which is triggered by scrolling
            await skyvern_page.safe_wait_for_animation_end(caller="scrolling_screenshot")
    else:
        if draw_boxes:
            await skyvern_page.build_elements_and_draw_bounding_boxes(frame=frame, frame_index=frame_index)

        LOG.debug("Page is not scrollable", url=url, num_screenshots=len(screenshots))
        screenshot = await _current_viewpoint_screenshot_helper(
            page=page,
            mode=mode,
            engine_selection=engine_selection,
        )
        screenshots.append(screenshot)
        positions.append(0)

        if draw_boxes:
            await skyvern_page.remove_bounding_boxes()

    return screenshots, positions


def _merge_images_by_position(images: list[Image.Image], positions: list[int]) -> Image.Image:
    """Merge screenshots vertically using scroll positions to remove overlaps."""
    if not images:
        raise ValueError("no images to merge")
    if len(images) != len(positions):
        raise ValueError("images and positions length mismatch")

    if len(images) == 1:
        return images[0]

    max_width = max(img.width for img in images)

    merged_height = images[0].height
    for i in range(1, len(images)):
        merged_height += positions[i] - positions[i - 1]

    merged_img = Image.new("RGB", (max_width, merged_height), color=(255, 255, 255))
    merged_complete = False
    try:
        current_y = 0
        merged_img.paste(images[0], (0, current_y))
        current_y += images[0].height

        for i in range(1, len(images)):
            step = positions[i] - positions[i - 1]
            overlap = images[i].height - step
            if overlap > 0:
                cropped = images[i].crop((0, overlap, images[i].width, images[i].height))
            else:
                cropped = images[i]

            try:
                merged_img.paste(cropped, (0, current_y))
                current_y += cropped.height
            finally:
                # paste copies the pixels, so a freshly cropped temporary is dead here; close it to
                # release the decode eagerly, even if the paste raised. Aliases of ``images[i]`` (no
                # overlap) are left for the caller to close, since it still owns every input image.
                if cropped is not images[i]:
                    cropped.close()
        merged_complete = True
    finally:
        # On failure the caller never receives ``merged_img`` and cannot hand it to
        # ``_close_screenshot_stitch_resources``, so release the stitched canvas here before the
        # exception unwinds through the fallback/OOM path.
        if not merged_complete:
            merged_img.close()

    return merged_img


def _close_screenshot_stitch_resources(
    images: list[Image.Image],
    merged_image: Image.Image | None,
    buffer: BytesIO | None,
) -> None:
    """Deterministically release the decoded viewport images, the stitched image, and the PNG buffer.

    ``_merge_images_by_position`` returns the sole input image for single-viewport input, so the
    merged image can alias ``images[0]``; dedupe by identity to avoid an invalid double-close.
    """
    seen_ids: set[int] = set()
    closeables: list[Image.Image] = list(images)
    if merged_image is not None:
        closeables.append(merged_image)
    for image in closeables:
        if id(image) in seen_ids:
            continue
        seen_ids.add(id(image))
        image.close()
    if buffer is not None:
        buffer.close()


# FileReader keeps the payload binary-safe without arrayBuffer/Uint8Array
# transcoding back across CDP.
_SAME_ORIGIN_FETCH_JS = """
async (args) => {
    try {
        const { url, maxSizeBytes } = args;
        const response = await fetch(url);
        if (!response.ok) {
            return { ok: false, status: response.status };
        }
        const blob = await response.blob();
        // Reject oversized blobs before serializing them to a data URL, so a huge
        // client-side blob can't be read fully into memory / base64-transcoded.
        if (maxSizeBytes != null && blob.size > maxSizeBytes) {
            return { ok: false, error: 'too_large', size: blob.size };
        }
        return await new Promise((resolve) => {
            const reader = new FileReader();
            reader.onloadend = () => {
                const result = reader.result || '';
                const comma = result.indexOf(',');
                if (comma === -1) {
                    resolve({ ok: false, error: 'no_data_url_payload' });
                    return;
                }
                resolve({ ok: true, base64: result.substring(comma + 1) });
            };
            reader.onerror = () => resolve({ ok: false, error: 'file_reader_error' });
            reader.readAsDataURL(blob);
        });
    } catch (err) {
        return { ok: false, error: String(err) };
    }
}
"""


def _blob_url_origin(blob_url: str) -> str | None:
    if not blob_url.startswith("blob:"):
        return None
    parsed = urllib.parse.urlparse(blob_url[len("blob:") :])
    if not parsed.scheme or not parsed.netloc:
        return None
    return f"{parsed.scheme}://{parsed.netloc}"


def _frame_origin(frame_url: str | None) -> str | None:
    if not frame_url:
        return None
    if frame_url.startswith("blob:"):
        return _blob_url_origin(frame_url)
    parsed = urllib.parse.urlparse(frame_url)
    if not parsed.scheme or not parsed.netloc:
        return None
    return f"{parsed.scheme}://{parsed.netloc}"


def _frames_for_origin(page: Page, origin: str) -> list[Frame]:
    """Return frames whose origin matches the given origin, main frame first."""
    seen: set[int] = set()
    matches: list[Frame] = []
    candidates: list[Frame] = [page.main_frame, *page.frames]
    for frame in candidates:
        frame_id = id(frame)
        if frame_id in seen:
            continue
        seen.add(frame_id)
        try:
            frame_url = frame.url
        except Exception:
            continue
        if _frame_origin(frame_url) == origin:
            matches.append(frame)
    return matches


def _frames_for_blob_origin(page: Page, blob_origin: str) -> list[Frame]:
    """Return frames whose origin matches the blob's origin, main frame first."""
    return _frames_for_origin(page, blob_origin)


def _all_page_frames(page: Page) -> list[Frame]:
    """All frames on the page, main frame first, deduped."""
    seen: set[int] = set()
    frames: list[Frame] = []
    for frame in [page.main_frame, *page.frames]:
        frame_id = id(frame)
        if frame_id in seen:
            continue
        seen.add(frame_id)
        frames.append(frame)
    return frames


MAX_RETAINED_BLOB_COUNT = 8
MAX_RETAINED_BLOB_TOTAL_BYTES = 100 * 1024 * 1024

_BLOB_RETENTION_STATE_KEY = "__skyvernBlobRetention"
# Stable ownership brand + schema version. A same-name page global is only ever mutated, restored, or
# trusted as fresh when it carries this brand/version AND the full member schema below; anything else
# (a page's own global, a corrupt/partial state, or one whose wrappers are no longer the active URL
# methods) is left untouched and never read as owned.
_BLOB_RETENTION_BRAND = "skyvern.blobRetention"
_BLOB_RETENTION_VERSION = 1

# Injected into every retention JS block. `isOwned` validates the complete schema needed for safe
# exact-native restoration (origCreate/origRevoke), retained-map clearing/revocation (retained/deferred),
# wrapper identity (wrapCreate/wrapRevoke), and closure neutralization (active). Restores compare each
# URL method to its exact Skyvern wrapper independently, so a page hook is never clobbered.
_BLOB_RETENTION_OWNERSHIP_JS = f"""
    const KEY = '{_BLOB_RETENTION_STATE_KEY}';
    const BRAND = '{_BLOB_RETENTION_BRAND}';
    const VERSION = {_BLOB_RETENTION_VERSION};
    const isOwned = (s) => !!s && s.brand === BRAND && s.version === VERSION
        && typeof s.origCreate === 'function' && typeof s.origRevoke === 'function'
        && (s.retained instanceof Map) && (s.deferred instanceof Set)
        && typeof s.wrapCreate === 'function' && typeof s.wrapRevoke === 'function'
        && typeof s.active === 'boolean';
"""

# Wrap URL.createObjectURL/revokeObjectURL in the page realm so a PDF-shaped object URL minted during
# the action window survives a synchronous revoke long enough for the in-page read to recover its bytes.
# Both wrappers return their native values; only PDF-typed (or untyped) blobs are retained, bounded by
# count and total size; every other blob revokes natively so unrelated downloads are unaffected.
_BLOB_RETENTION_INSTALL_JS = (
    "(config) => {"
    + _BLOB_RETENTION_OWNERSHIP_JS
    + """
    if (Object.prototype.hasOwnProperty.call(window, KEY)) {
        const existing = window[KEY];
        // Any existing own property that is not a valid owned state is foreign (a page's own global,
        // including a null/undefined placeholder): never overwrite, delete, or modify it, and never touch
        // the URL methods. Fail open (no retention).
        if (!isOwned(existing)) return false;
        // Valid owned stale state (a prior window that never tore down), possibly with the page having
        // rewrapped one or both URL methods. Neutralize the closures first, settle the deferred revokes,
        // restore ONLY each method that still directly equals our wrapper (preserve any page hook), clear
        // the maps, and drop the state — regardless of any wrapper mismatch — so no prior key stays fresh.
        existing.active = false;
        try {
            if (URL.createObjectURL === existing.wrapCreate) URL.createObjectURL = existing.origCreate;
            if (URL.revokeObjectURL === existing.wrapRevoke) URL.revokeObjectURL = existing.origRevoke;
            for (const url of existing.deferred) { try { Reflect.apply(existing.origRevoke, URL, [url]); } catch (e) {} }
            existing.retained.clear();
            existing.deferred.clear();
        } catch (e) { return false; }
        try { delete window[KEY]; } catch (e) {}
        // If the owned state could not be dropped, do not overwrite it or patch the URL methods.
        if (Object.prototype.hasOwnProperty.call(window, KEY)) return false;
    }
    // Capture whatever the current methods are now — a restored native, or a page hook we preserved. Keep
    // the exact function objects (not .bind(URL) wrappers) so teardown restores them by identity; call
    // them with an explicit URL receiver via Reflect.apply at the call sites instead.
    const origCreate = URL.createObjectURL;
    const origRevoke = URL.revokeObjectURL;
    const state = {
        brand: BRAND,
        version: VERSION,
        active: true,
        origCreate: origCreate,
        origRevoke: origRevoke,
        retained: new Map(),
        deferred: new Set(),
        totalBytes: 0,
        maxCount: config.maxCount,
        maxTotalBytes: config.maxTotalBytes,
    };
    // The wrappers retain/defer only while state.active. Once teardown or a stale rearm flips it off,
    // a closure orphaned inside a third-party wrapper chain degrades to a pure native pass-through.
    const wrapCreate = function (obj) {
        const url = Reflect.apply(origCreate, URL, arguments);
        try {
            if (state.active && typeof Blob !== 'undefined' && obj instanceof Blob) {
                const type = (obj.type || '').toLowerCase();
                const size = obj.size || 0;
                const pdfish = type === 'application/pdf' || type === '';
                if (pdfish && state.retained.size < state.maxCount && (state.totalBytes + size) <= state.maxTotalBytes) {
                    state.retained.set(url, obj);
                    state.totalBytes += size;
                }
            }
        } catch (e) {}
        return url;
    };
    const wrapRevoke = function (url) {
        if (state.active && state.retained.has(url)) {
            state.deferred.add(url);
            return undefined;
        }
        return Reflect.apply(origRevoke, URL, arguments);
    };
    state.wrapCreate = wrapCreate;
    state.wrapRevoke = wrapRevoke;
    // Publish and verify ownership BEFORE patching either URL method. A non-writable same-name property
    // makes this assignment a silent no-op; if the state did not take, change nothing else and fail open.
    try { window[KEY] = state; } catch (e) { return false; }
    if (window[KEY] !== state) return false;
    URL.createObjectURL = wrapCreate;
    URL.revokeObjectURL = wrapRevoke;
    // Partial patch (e.g. a frozen URL method): roll back only the method that actually took, neutralize
    // and drop the just-published state, and fail open with the methods at their pre-install identities.
    if (URL.createObjectURL !== wrapCreate || URL.revokeObjectURL !== wrapRevoke) {
        state.active = false;
        if (URL.createObjectURL === wrapCreate) URL.createObjectURL = origCreate;
        if (URL.revokeObjectURL === wrapRevoke) URL.revokeObjectURL = origRevoke;
        try { delete window[KEY]; } catch (e) { window[KEY] = undefined; }
        return false;
    }
    return true;
}
"""
)

_BLOB_RETENTION_TEARDOWN_JS = (
    "() => {"
    + _BLOB_RETENTION_OWNERSHIP_JS
    + """
    const state = window[KEY];
    if (state === undefined || state === null) return false;
    // Foreign / corrupt same-name global: never touch a property we do not own.
    if (!isOwned(state)) return false;
    // Neutralize first: any wrapper closure still captured in a third-party chain must stop retaining
    // and deferring the moment we tear down, even though we cannot pull it out of that chain.
    state.active = false;
    try {
        // Restore each URL method independently: if one still directly equals our wrapper, restore that
        // saved exact method; if the other was replaced later by the page, preserve that later method so
        // we never clobber it.
        if (URL.createObjectURL === state.wrapCreate) URL.createObjectURL = state.origCreate;
        if (URL.revokeObjectURL === state.wrapRevoke) URL.revokeObjectURL = state.origRevoke;
        for (const url of state.deferred) { try { Reflect.apply(state.origRevoke, URL, [url]); } catch (e) {} }
        state.retained.clear();
        state.deferred.clear();
    } finally {
        try { delete window[KEY]; } catch (e) { window[KEY] = undefined; }
    }
    return true;
}
"""
)


async def _evaluate_in_all_frames(page: Page, expression: str, arg: Any, *, workflow_run_id: str | None) -> None:
    main_frame = page.main_frame
    for frame in _all_page_frames(page):
        try:
            if frame is main_frame:
                await evaluate_in_main_world(page, expression, arg)
            else:
                await frame.evaluate(expression, arg)
        except Exception:
            # Best effort: a torn-down or navigating frame just misses this window. No URL/customer
            # data is logged, and a failure here must never break the action.
            LOG.debug("blob URL retention could not reach a frame", workflow_run_id=workflow_run_id)


async def install_blob_url_retention(
    page: Page,
    *,
    max_retained_count: int = MAX_RETAINED_BLOB_COUNT,
    max_total_bytes: int = MAX_RETAINED_BLOB_TOTAL_BYTES,
    workflow_run_id: str | None = None,
) -> None:
    """Retain PDF-shaped object URLs minted during the action window against a synchronous revoke.

    Installed into the page's realms before the download-triggering click; paired with
    ``teardown_blob_url_retention`` which performs the deferred revokes and restores the originals.
    """
    config = {"maxCount": max_retained_count, "maxTotalBytes": max_total_bytes}
    await _evaluate_in_all_frames(page, _BLOB_RETENTION_INSTALL_JS, config, workflow_run_id=workflow_run_id)


async def teardown_blob_url_retention(page: Page, *, workflow_run_id: str | None = None) -> None:
    await _evaluate_in_all_frames(page, _BLOB_RETENTION_TEARDOWN_JS, None, workflow_run_id=workflow_run_id)


# Read-only probe of the retention state installed by _BLOB_RETENTION_INSTALL_JS. Returns only
# booleans: whether this realm exposes a Skyvern-owned retention state, and whether the passed URL is a
# live key in its retained Map. A foreign / corrupt same-name global fails closed (never trusted). The
# URL crosses into page JS but never comes back out and is never logged.
_BLOB_RETENTION_PROBE_JS = (
    "(url) => {"
    + _BLOB_RETENTION_OWNERSHIP_JS
    + """
    const state = window[KEY];
    // Require a valid owned AND active state: a stale cleanup can flip active off while retained keys
    // linger, and those must never be trusted as action-fresh.
    if (!isOwned(state) || state.active !== true) {
        return { observed: false, retained: false };
    }
    return { observed: true, retained: state.retained.has(url) };
}
"""
)


class BlobActionFreshness(NamedTuple):
    state_observed: bool
    retained: bool


def _blob_freshness_probe_frames(page: Page, blob_origin: str) -> list[Frame]:
    """Frames to probe for a blob's retention state: those at the blob origin (the creator realm is
    same-origin as the blob) plus indeterminate-origin frames (``about:blank``/``srcdoc``) that may
    inherit the creator origin. Main frame first, deduped."""
    seen: set[int] = set()
    frames: list[Frame] = []
    for frame in [page.main_frame, *page.frames]:
        frame_id = id(frame)
        if frame_id in seen:
            continue
        seen.add(frame_id)
        try:
            origin = _frame_origin(frame.url)
        except Exception:
            # An unreadable frame url is indeterminate; include it and let the probe best-effort.
            frames.append(frame)
            continue
        if origin == blob_origin or origin is None:
            frames.append(frame)
    return frames


async def probe_blob_action_freshness(
    page: Page,
    blob_url: str,
    *,
    workflow_run_id: str | None = None,
) -> BlobActionFreshness:
    """Whether ``blob_url`` (fragment-stripped) is a live key in a ``__skyvernBlobRetention.retained``
    Map in any realm relevant to its origin — proof it was minted through the pre-click retention
    wrapper during this action window.

    The blob creator realm is not necessarily the frame displaying it (main/parent may mint the blob
    and assign it to an iframe ``src``), so this probes the blob-origin frames plus indeterminate-origin
    frames. Main frame routes through ``evaluate_in_main_world``; sub-frames through ``frame.evaluate``.
    Only booleans cross the boundary; the URL is passed in but never logged or returned. Best-effort per
    realm: a probe failure in one frame never raises and the verdict rests on the other realms. Returns
    ``(state_observed, retained)`` — both False when no realm exposes retention state, so the caller
    fails closed.
    """
    blob_origin = _blob_url_origin(blob_url)
    if blob_origin is None:
        return BlobActionFreshness(state_observed=False, retained=False)

    frames = _blob_freshness_probe_frames(page, blob_origin)
    main_frame = page.main_frame
    state_observed = False
    retained = False
    for frame in frames:
        try:
            if frame is main_frame:
                result = await evaluate_in_main_world(page, _BLOB_RETENTION_PROBE_JS, blob_url)
            else:
                result = await frame.evaluate(_BLOB_RETENTION_PROBE_JS, blob_url)
        except Exception:
            LOG.debug("blob action-freshness probe could not reach a frame", workflow_run_id=workflow_run_id)
            continue
        if not isinstance(result, dict):
            continue
        if result.get("observed"):
            state_observed = True
            if result.get("retained"):
                retained = True
    return BlobActionFreshness(state_observed=state_observed, retained=retained)


def pop_destination_facts(nodes: object) -> dict[str, dict]:
    """Strip SKY-12875 destination facts out of scraper payloads, in place, at the JS->Python
    boundary. Every downstream consumer — element hashes and cached-action matching, persisted
    skyvern_element_data (DB rows and the public SDK Action type), the element-tree artifact,
    prompt building, incremental dropdown dedup — then sees dicts byte-identical to a build that
    never captured facts.

    Only a MAPPING-valued ``destination`` is ours. ``<div destination="shipping">`` is ordinary
    markup on ordinary sites, and every attribute is collected verbatim into ``attributes``, so a
    key-name-only match deleted page content — changing hashes, cached matching, prompts and
    persisted rows on pages that have nothing to do with this feature, and in disabled mode it
    also let the page seed the sidecar with an arbitrary id. DOM attribute values are always
    strings, so the page cannot forge the mapping shape through that channel; our facts are always
    mappings.

    The walk is DEEP: every nested dict and list at any depth is visited, because a hostile
    wrapper around the page-global builder can relocate a fact into a nested position (an
    attribute value, a grandchild) where a children-only walk would miss it. A wrapper that
    RENAMES the key is fabricating arbitrary payload content, which was possible before facts
    existed and is out of this strip's scope. The strip itself stays unconditional in both policy
    modes — it is protection against wrapper INJECTION, not capture cost — while capture is
    flag-gated so disabled mode does no fact work. The sidecar is keyed by element id and is
    consumed only by the observation epoch; the payload is page-controlled, so any shape is
    tolerated and cycles (reconstructable via Playwright's ref protocol) terminate via the
    seen-set instead of hanging the worker.
    """
    facts: dict[str, dict] = {}
    stack = list(nodes) if isinstance(nodes, list) else []
    seen: set[int] = set()
    while stack:
        node = stack.pop()
        if id(node) in seen:
            continue
        seen.add(id(node))
        if isinstance(node, list):
            stack.extend(node)
            continue
        if not isinstance(node, dict):
            continue
        if isinstance(node.get("destination"), dict):
            destination = node.pop("destination")
            element_id = node.get("id")
            if isinstance(element_id, str) and element_id:
                facts[element_id] = destination
        stack.extend(node.values())
    return facts


class SkyvernFrame:
    engine_selection: BrowserEngineSelection | None = None

    @staticmethod
    async def evaluate(
        frame: Page | Frame,
        expression: str,
        arg: Any | None = None,
        timeout_ms: float = SettingsManager.get_settings().BROWSER_ACTION_TIMEOUT_MS,
        engine_selection: BrowserEngineSelection | None = None,
        *,
        force_cdp: bool = False,
        deadline: float | None = None,
    ) -> Any:
        async def evaluate_expression() -> Any:
            return await _dispatch_evaluate(frame, expression, arg, force_cdp=force_cdp)

        return await SkyvernFrame._evaluate_expression(
            frame=frame,
            expression=expression,
            evaluate_expression=evaluate_expression,
            timeout_ms=timeout_ms,
            engine_selection=engine_selection,
            **({"deadline": deadline} if deadline is not None else {}),
        )

    @staticmethod
    async def _evaluate_expression(
        frame: Page | Frame,
        expression: str,
        evaluate_expression: Callable[[], Awaitable[Any]],
        timeout_ms: float,
        engine_selection: BrowserEngineSelection | None = None,
        deadline: float | None = None,
    ) -> Any:
        loop = asyncio.get_running_loop()
        deadline = deadline if deadline is not None else loop.time() + timeout_ms / 1000
        try:
            async with asyncio.timeout_at(deadline):
                result = await evaluate_expression()
        except asyncio.TimeoutError as error:
            skyvern_context.record_browser_timeout(BrowserOperation.EVALUATE)
            # Re-raised and handled by the caller (scrape retries / failure classification),
            # so this is not the failure boundary; log without a traceback at warning.
            LOG.warning("Skyvern timed out trying to analyze the page", expression=expression)
            raise SkyvernPageAnalysisTimeout("Skyvern timed out trying to analyze the page") from error
        except RuntimeError as e:
            # `evaluate_in_main_world` raises RuntimeError on Runtime.evaluate
            # exception payloads; only navigation-context-lost text recovers here.
            error_msg = str(e)
            if not _is_navigation_context_lost(error_msg):
                raise
            return await SkyvernFrame._evaluate_with_navigation_recovery(
                frame=frame,
                expression=expression,
                evaluate_expression=evaluate_expression,
                timeout_ms=timeout_ms,
                initial_error=error_msg,
                engine_selection=engine_selection,
                deadline=deadline,
            )
        except Exception as e:
            # A driver-native error from THIS run's engine (stock Playwright when no engine is
            # pinned). A foreign engine's error, or an unrelated exception, is not ours: re-raise it
            # unchanged. Cancellation (BaseException, not Exception) is never caught here.
            if not _is_engine_error(e, engine_selection):
                raise
            error_msg = str(e)
            if not _is_navigation_context_lost(error_msg):
                raise
            return await SkyvernFrame._evaluate_with_navigation_recovery(
                frame=frame,
                expression=expression,
                evaluate_expression=evaluate_expression,
                timeout_ms=timeout_ms,
                initial_error=error_msg,
                engine_selection=engine_selection,
                deadline=deadline,
            )
        skyvern_context.record_browser_success()
        return result

    @staticmethod
    async def _evaluate_with_navigation_recovery(
        frame: Page | Frame,
        expression: str,
        evaluate_expression: Callable[[], Awaitable[Any]],
        timeout_ms: float,
        initial_error: str,
        engine_selection: BrowserEngineSelection | None = None,
        deadline: float | None = None,
    ) -> Any:
        # Multi-hop SSO/OIDC flows (especially response_mode=form_post) can destroy
        # the JS execution context several times in a row as the page auto-submits
        # through redirects. Wait for the page to settle between attempts instead
        # of racing the next navigation. The whole recovery shares one monotonic
        # deadline so retries can't compound into many multiples of timeout_ms.
        per_attempt_seconds = timeout_ms / 1000
        loop = asyncio.get_running_loop()
        if deadline is None:
            deadline = loop.time() + per_attempt_seconds * _NAVIGATION_RECOVERY_MAX_ATTEMPTS

        def _remaining_seconds() -> float:
            return max(0.0, deadline - loop.time())

        last_error_msg = initial_error
        for attempt in range(1, _NAVIGATION_RECOVERY_MAX_ATTEMPTS + 1):
            if _remaining_seconds() <= 0:
                LOG.warning(
                    "Skyvern timed out trying to analyze the page after navigation recovery",
                    expression=expression,
                )
                raise SkyvernPageAnalysisTimeout("Skyvern timed out trying to analyze the page")

            LOG.warning(
                "JS execution context lost (likely due to page navigation), re-injecting domUtils.js and retrying",
                attempt=attempt,
                expression=expression[:200],
                error=last_error_msg[:200],
            )
            settle_ms = min(_NAVIGATION_SETTLE_TIMEOUT_MS, _remaining_seconds() * 1000)
            await _wait_for_navigation_settle(frame, timeout_ms=settle_ms, engine_selection=engine_selection)

            # The bootstrap call already IS the domUtils.js injection, so the retry below
            # re-injects it anyway; a separate injection pass would spend a second attempt
            # budget on identical work and halve the attempts that fit in the deadline.
            if expression != JS_FUNCTION_DEFS:
                inject_budget = min(per_attempt_seconds, _remaining_seconds())
                if inject_budget <= 0:
                    LOG.error(
                        "Skyvern timed out trying to analyze the page after navigation recovery",
                        expression=expression,
                    )
                    raise SkyvernPageAnalysisTimeout("Skyvern timed out trying to analyze the page")
                try:
                    async with asyncio.timeout(inject_budget):
                        # Same dispatch helper so a prefixed Page re-injects
                        # JS_FUNCTION_DEFS via Runtime.evaluate (preserving the marker).
                        await _dispatch_evaluate(frame, JS_FUNCTION_DEFS, None)
                except asyncio.TimeoutError as error:
                    LOG.exception(
                        "Skyvern timed out trying to analyze the page during domUtils.js re-injection",
                        expression=expression,
                    )
                    raise SkyvernPageAnalysisTimeout("Skyvern timed out trying to analyze the page") from error
                except Exception as inject_err:
                    # RuntimeError (main-world Runtime.evaluate payloads) is engine-agnostic; the
                    # driver-native failure routes through the per-run engine identity. A foreign
                    # error is re-raised; cancellation never reaches here.
                    if not (isinstance(inject_err, RuntimeError) or _is_engine_error(inject_err, engine_selection)):
                        raise
                    last_error_msg = str(inject_err)
                    if attempt == _NAVIGATION_RECOVERY_MAX_ATTEMPTS or not _is_navigation_context_lost(last_error_msg):
                        LOG.warning(
                            "Re-injection of domUtils.js also failed, page may still be navigating",
                            attempts=attempt,
                        )
                        raise
                    continue

            retry_budget = min(per_attempt_seconds, _remaining_seconds())
            if retry_budget <= 0:
                LOG.error(
                    "Skyvern timed out trying to analyze the page after navigation recovery",
                    expression=expression,
                )
                raise SkyvernPageAnalysisTimeout("Skyvern timed out trying to analyze the page")
            try:
                async with asyncio.timeout(retry_budget):
                    result = await evaluate_expression()
                # The final evaluate answered, so this run-wide health tally is no longer stale.
                skyvern_context.record_browser_success()
                return result
            except asyncio.TimeoutError as error:
                LOG.exception("Skyvern timed out on retry after JS context re-injection", expression=expression)
                raise SkyvernPageAnalysisTimeout("Skyvern timed out trying to analyze the page") from error
            except Exception as retry_err:
                if not (isinstance(retry_err, RuntimeError) or _is_engine_error(retry_err, engine_selection)):
                    raise
                last_error_msg = str(retry_err)
                if attempt == _NAVIGATION_RECOVERY_MAX_ATTEMPTS or not _is_navigation_context_lost(last_error_msg):
                    raise

        # The loop either returns or raises; this is unreachable but keeps mypy happy.
        raise PlaywrightError(last_error_msg)

    @staticmethod
    async def get_url(frame: Page | Frame) -> str:
        return await SkyvernFrame.evaluate(frame=frame, expression="() => document.location.href")

    @staticmethod
    async def read_blob_url_bytes(
        page: Page,
        blob_url: str,
        workflow_run_id: str | None = None,
        max_size_bytes: int | None = None,
        probe: bool = False,
    ) -> bytes | None:
        # probe=True is for best-effort multi-page fallback where the caller tries every open
        # page; expected misses on non-owning pages shouldn't spam ERROR/WARN logs, so downgrade
        # give-up/retry logging to debug. The final failure signal stays with the caller.
        give_up_log = LOG.debug if probe else LOG.error
        retry_log = LOG.debug if probe else LOG.warning

        blob_origin = _blob_url_origin(blob_url)
        if blob_origin is not None:
            frames = _frames_for_blob_origin(page, blob_origin)
        elif blob_url.startswith("blob:"):
            # Opaque-origin blobs (blob:null/...) from sandboxed iframes or data: documents have
            # no matchable origin — probe every frame since we can't identify the owner by origin.
            frames = _all_page_frames(page)
        else:
            give_up_log("blob URL read aborted: not a blob URL", workflow_run_id=workflow_run_id)
            return None

        if not frames:
            give_up_log("blob URL read found no candidate frame", workflow_run_id=workflow_run_id)
            return None

        # blob.size is checked in-page against this before the payload is serialized.
        blob_arg = {"url": blob_url, "maxSizeBytes": max_size_bytes}
        main_frame = page.main_frame
        for frame in frames:
            try:
                # Main-frame routes through evaluate_in_main_world so any
                # context-level main-world prefix stays attached; sub-frames use
                # frame.evaluate (main-world prefixes are page-scoped).
                if frame is main_frame:
                    result = await evaluate_in_main_world(page, _SAME_ORIGIN_FETCH_JS, blob_arg)
                else:
                    result = await frame.evaluate(_SAME_ORIGIN_FETCH_JS, blob_arg)
            except Exception:
                retry_log(
                    "blob URL in-frame fetch raised; trying next frame if any",
                    workflow_run_id=workflow_run_id,
                    exc_info=True,
                )
                continue
            if isinstance(result, dict) and result.get("error") == "too_large":
                LOG.warning(
                    "blob URL exceeds max size; not reading",
                    workflow_run_id=workflow_run_id,
                    size=result.get("size"),
                    max_size_bytes=max_size_bytes,
                )
                return None
            if not isinstance(result, dict) or not result.get("ok"):
                retry_log(
                    "blob URL in-frame fetch returned not-ok; trying next frame if any",
                    workflow_run_id=workflow_run_id,
                    result=result if isinstance(result, dict) else None,
                )
                continue
            b64_payload = result.get("base64")
            if not isinstance(b64_payload, str):
                retry_log(
                    "blob URL in-frame fetch returned non-string payload; trying next frame if any",
                    workflow_run_id=workflow_run_id,
                )
                continue
            try:
                return base64.b64decode(b64_payload, validate=True)
            except Exception:
                retry_log(
                    "blob URL in-frame fetch payload was not valid base64; trying next frame if any",
                    workflow_run_id=workflow_run_id,
                    exc_info=True,
                )
                continue

        give_up_log(
            "blob URL read could not retrieve bytes from any matching frame",
            workflow_run_id=workflow_run_id,
        )
        return None

    @staticmethod
    async def read_http_url_bytes(
        page: Page,
        url: str,
        workflow_run_id: str | None = None,
        max_size_bytes: int | None = None,
        timeout_ms: float = SettingsManager.get_settings().BROWSER_ACTION_TIMEOUT_MS,
    ) -> bytes | None:
        """Fetch an http(s) URL's bytes via an in-page fetch() inside a same-origin frame.

        Recovers a resource whose inline iframe render was refused by a frame-embedding policy
        while its bytes stay retrievable same-origin (session cookies + connect-src apply to a
        same-origin fetch). Returns None for non-http(s) URLs, when no same-origin frame exists,
        or when every candidate frame's fetch fails.

        ``timeout_ms`` is the per-fetch evaluate timeout (default matches SkyvernFrame.evaluate);
        a caller with a larger whole-operation budget can widen it so a slow-but-alive server
        isn't rejected by the generic action timeout.
        """
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            return None
        frames = _frames_for_origin(page, f"{parsed.scheme}://{parsed.netloc}")
        if not frames:
            LOG.debug("same-origin URL read found no matching frame", workflow_run_id=workflow_run_id)
            return None

        # _SAME_ORIGIN_FETCH_JS is a plain fetch(); reused here for http(s) recovery.
        fetch_arg = {"url": url, "maxSizeBytes": max_size_bytes}
        main_frame = page.main_frame
        for frame in frames:
            # Route the main frame through the Page so SkyvernFrame.evaluate can attach any
            # context-level main-world prefix; sub-frames evaluate in-frame (prefixes are
            # page-scoped). SkyvernFrame.evaluate centralizes the main-world dispatch, timeout,
            # and navigation-context recovery.
            target: Page | Frame = page if frame is main_frame else frame
            try:
                result = await SkyvernFrame.evaluate(
                    frame=target, expression=_SAME_ORIGIN_FETCH_JS, arg=fetch_arg, timeout_ms=timeout_ms
                )
            except Exception:
                LOG.debug(
                    "same-origin URL in-frame fetch raised; trying next frame if any",
                    workflow_run_id=workflow_run_id,
                    exc_info=True,
                )
                continue
            if isinstance(result, dict) and result.get("error") == "too_large":
                LOG.warning(
                    "same-origin URL exceeds max size; not reading",
                    workflow_run_id=workflow_run_id,
                    size=result.get("size"),
                    max_size_bytes=max_size_bytes,
                )
                return None
            if not isinstance(result, dict) or not result.get("ok"):
                continue
            b64_payload = result.get("base64")
            if not isinstance(b64_payload, str):
                continue
            try:
                return base64.b64decode(b64_payload, validate=True)
            except Exception:
                continue
        return None

    # -- cursor overlay helpers ------------------------------------------------

    @staticmethod
    async def ensure_cursor_overlay_loaded(page: Page) -> None:
        """Inject ``cursorOverlay.js`` into *page* if not already present."""
        is_loaded = await SkyvernFrame.evaluate(page, "() => !!window.__pwCursorInit")
        if not is_loaded:
            await SkyvernFrame.evaluate(page, _CURSOR_OVERLAY_JS)

    @staticmethod
    async def cursor_init(page: Page) -> None:
        """Create the cursor dot and inject CSS keyframes."""
        await SkyvernFrame.evaluate(page, "() => __pwCursorInit()")

    @staticmethod
    async def cursor_move(page: Page, x: float, y: float) -> None:
        """Move cursor to *(x, y)* and leave interpolated trail dots."""
        await SkyvernFrame.evaluate(page, "(pos) => __pwCursorMove(pos)", [x, y])

    @staticmethod
    async def cursor_click_ring(page: Page, x: float, y: float) -> None:
        """Spawn an expanding ring animation at *(x, y)*."""
        await SkyvernFrame.evaluate(page, "(pos) => __pwCursorClickRing(pos)", [x, y])

    @staticmethod
    async def hide_cursor_overlay(page: Page) -> None:
        """Hide all ``[data-pw-overlay]`` elements (for screenshots)."""
        await SkyvernFrame.evaluate(page, "() => { if (window.__pwCursorHide) __pwCursorHide(); }")

    @staticmethod
    async def show_cursor_overlay(page: Page) -> None:
        """Re-show all ``[data-pw-overlay]`` elements after screenshots."""
        await SkyvernFrame.evaluate(page, "() => { if (window.__pwCursorShow) __pwCursorShow(); }")

    @staticmethod
    @traced(name="skyvern.browser.scrolling_screenshot")
    async def take_scrolling_screenshot(
        page: Page,
        file_path: str | None = None,
        timeout: float = SettingsManager.get_settings().BROWSER_SCREENSHOT_TIMEOUT_MS,
        mode: ScreenshotMode = ScreenshotMode.DETAILED,
        scrolling_number: int = SettingsManager.get_settings().MAX_NUM_SCREENSHOTS,
        engine_selection: BrowserEngineSelection | None = None,
    ) -> bytes:
        if scrolling_number <= 0:
            return await _current_viewpoint_screenshot_helper(
                page=page,
                file_path=file_path,
                timeout=timeout,
                mode=mode,
                engine_selection=engine_selection,
            )

        if scrolling_number > SettingsManager.get_settings().MAX_NUM_SCREENSHOTS:
            LOG.warning(
                "scrolling_number is greater than the max number of screenshots, setting it to the max number of screenshots",
                scrolling_number=scrolling_number,
                max_number=SettingsManager.get_settings().MAX_NUM_SCREENSHOTS,
            )
            scrolling_number = SettingsManager.get_settings().MAX_NUM_SCREENSHOTS

        # use spilt screenshot with lite mode, isntead of fullpage screenshot from playwright
        LOG.debug("Page is fully loaded, agent is about to generate the full page screenshot")
        start_time = time.time()
        skyvern_frame = await SkyvernFrame.create_instance(frame=page, engine_selection=engine_selection)
        x: int | None = None
        y: int | None = None
        try:
            x, y = await skyvern_frame.get_scroll_x_y()
            async with asyncio.timeout(timeout):
                screenshots, positions = await _scrolling_screenshots_helper(
                    page=page,
                    mode=mode,
                    max_number=scrolling_number,
                    engine_selection=engine_selection,
                )
                images: list[Image.Image] = []
                merged_img: Image.Image | None = None
                buffer: BytesIO | None = None
                try:
                    for screenshot in screenshots:
                        with Image.open(BytesIO(screenshot)) as img:
                            img.load()
                            images.append(img)

                    merged_img = _merge_images_by_position(images, positions)

                    buffer = BytesIO()
                    merged_img.save(buffer, format="PNG")
                    buffer.seek(0)

                    img_data = buffer.read()
                    if file_path is not None:
                        with open(file_path, "wb") as f:
                            f.write(img_data)

                    end_time = time.time()
                    LOG.debug(
                        "Full page screenshot taking time",
                        screenshot_time=end_time - start_time,
                        file_path=file_path,
                    )
                    return img_data
                finally:
                    # The decoded images, stitched image, and PNG buffer land in reference cycles that
                    # gen-0 GC defers, leaving ~100 MB/event resident until a full collection; release
                    # them explicitly then force one, scoped to the multi-viewport stitch that accumulates them.
                    _close_screenshot_stitch_resources(images, merged_img, buffer)
                    if len(images) > 1:
                        gc.collect()
        except ScreenshotTargetClosed:
            # The fallback below captures the same page, so a closed target can only fail there too.
            x = None
            y = None
            raise
        except Exception:
            LOG.warning(
                "Failed to take full page screenshot, fallback to use playwright full page screenshot",
                exc_info=True,
            )
            # reset x and y to None to avoid the scroll_to_x_y call in finally block
            x = None
            y = None
            return await _current_viewpoint_screenshot_helper(
                page=page,
                file_path=file_path,
                timeout=timeout,
                full_page=True,
                engine_selection=engine_selection,
            )
        finally:
            if x is not None and y is not None:
                await skyvern_frame.safe_scroll_to_x_y(x, y)

    @staticmethod
    @traced(name="skyvern.browser.split_screenshots")
    async def take_split_screenshots(
        page: Page,
        url: str | None = None,
        draw_boxes: bool = False,
        max_number: int = SettingsManager.get_settings().MAX_NUM_SCREENSHOTS,
        scroll: bool = True,
        engine_selection: BrowserEngineSelection | None = None,
    ) -> list[bytes]:
        if not scroll:
            return [
                await _current_viewpoint_screenshot_helper(
                    page=page,
                    mode=ScreenshotMode.DETAILED,
                    engine_selection=engine_selection,
                )
            ]

        screenshots, _ = await _scrolling_screenshots_helper(
            page=page,
            url=url,
            max_number=max_number,
            draw_boxes=draw_boxes,
            mode=ScreenshotMode.DETAILED,
            engine_selection=engine_selection,
        )
        return screenshots

    @classmethod
    async def create_instance(
        cls, frame: Page | Frame, engine_selection: BrowserEngineSelection | None = None
    ) -> SkyvernFrame:
        instance = cls(frame=frame, engine_selection=engine_selection)
        await cls.evaluate(frame=instance.frame, expression=JS_FUNCTION_DEFS, engine_selection=engine_selection)
        if SettingsManager.get_settings().ENABLE_EXP_ALL_TEXTUAL_ELEMENTS_INTERACTABLE:
            await instance.evaluate(
                frame=instance.frame,
                expression="() => window.GlobalEnableAllTextualElements = true",
                engine_selection=engine_selection,
            )
        return instance

    def __init__(self, frame: Page | Frame, engine_selection: BrowserEngineSelection | None = None) -> None:
        self.frame = frame
        # The logical run's pinned engine (None when the frame was created outside the per-run engine
        # seam), forwarded to every evaluate so navigation-context-loss recovery keys on THIS engine's
        # native error family; None preserves the exact stock-Playwright behavior.
        self.engine_selection = engine_selection

    def get_frame(self) -> Page | Frame:
        return self.frame

    @traced(name="skyvern.browser.get_content")
    async def get_content(self, timeout: float = PAGE_CONTENT_TIMEOUT) -> str:
        async with asyncio.timeout(timeout):
            return await self.frame.content()

    async def get_scroll_x_y(self) -> tuple[int, int]:
        js_script = "() => getScrollXY()"
        return await self.evaluate(frame=self.frame, engine_selection=self.engine_selection, expression=js_script)

    async def get_open_aria_popup_trigger(self) -> dict | None:
        """Return structural details of an open ARIA popup trigger on this frame, or None.

        Fails open (returns None) on any evaluation error so screenshot-scroll policy keeps the
        current scrolling behavior rather than breaking the scrape/action loop. Detection
        semantics live in getOpenAriaPopupTrigger in domUtils.js.
        """
        try:
            result = await self.evaluate(frame=self.frame, expression="() => getOpenAriaPopupTrigger()")
        except Exception:
            LOG.warning(
                "Failed to detect open ARIA popup trigger; using default scrolling behavior",
                exc_info=True,
            )
            return None
        return result if isinstance(result, dict) else None

    async def get_scroll_width_and_height(self) -> tuple[int, int]:
        js_script = "() => getScrollWidthAndHeight()"
        return await self.evaluate(frame=self.frame, engine_selection=self.engine_selection, expression=js_script)

    async def scroll_to_x_y(self, x: int, y: int) -> None:
        js_script = "([x, y]) => scrollToXY(x, y)"
        return await self.evaluate(
            frame=self.frame, engine_selection=self.engine_selection, expression=js_script, arg=[x, y]
        )

    async def safe_scroll_to_x_y(self, x: int, y: int) -> None:
        try:
            await self.scroll_to_x_y(x, y)
        except Exception:
            LOG.warning("Failed to scroll to x, y, ignore it", x=x, y=y, exc_info=True)

    async def scroll_into_view(self, element: ElementHandle) -> None:
        """Scroll all ancestor containers (including nested ones with overflow-y: auto)
        so that the element is centered in the viewport."""
        js_script = "(element) => element.scrollIntoView({block: 'center', inline: 'center', behavior: 'instant'})"
        return await self.evaluate(
            frame=self.frame, engine_selection=self.engine_selection, expression=js_script, arg=element
        )

    async def scroll_to_element_bottom(self, element: ElementHandle, page_by_page: bool = False) -> None:
        js_script = "([element, page_by_page]) => scrollToElementBottom(element, page_by_page)"
        return await self.evaluate(
            frame=self.frame, engine_selection=self.engine_selection, expression=js_script, arg=[element, page_by_page]
        )

    async def scroll_to_element_top(self, element: ElementHandle) -> None:
        js_script = "(element) => scrollToElementTop(element)"
        return await self.evaluate(
            frame=self.frame, engine_selection=self.engine_selection, expression=js_script, arg=element
        )

    async def parse_element_from_html(self, frame: str, element: ElementHandle, interactable: bool) -> dict:
        js_script = "async ([frame, element, interactable]) => await buildElementObject(frame, element, interactable)"
        parsed = await self.evaluate(
            frame=self.frame,
            engine_selection=self.engine_selection,
            expression=js_script,
            arg=[frame, element, interactable],
        )
        pop_destination_facts([parsed])
        return parsed

    async def get_element_scrollable(self, element: ElementHandle) -> bool:
        js_script = "(element) => isScrollable(element)"
        return await self.evaluate(
            frame=self.frame, engine_selection=self.engine_selection, expression=js_script, arg=element
        )

    async def get_element_visible(self, locator: Locator) -> bool:
        js_script = "(element) => isElementVisible(element) && !isHidden(element)"

        async def evaluate_expression() -> bool:
            if await locator.count() == 0:
                return False
            return await locator.evaluate(js_script)

        return await self._evaluate_expression(
            frame=self.frame,
            engine_selection=self.engine_selection,
            expression=js_script,
            evaluate_expression=evaluate_expression,
            timeout_ms=SettingsManager.get_settings().BROWSER_ACTION_TIMEOUT_MS,
        )

    async def get_disabled_from_style(self, element: ElementHandle) -> bool:
        js_script = "(element) => checkDisabledFromStyle(element)"
        return await self.evaluate(
            frame=self.frame, engine_selection=self.engine_selection, expression=js_script, arg=element
        )

    async def get_blocking_element_id(self, element: ElementHandle) -> tuple[str, bool]:
        js_script = "(element) => getBlockElementUniqueID(element)"
        return await self.evaluate(
            frame=self.frame, engine_selection=self.engine_selection, expression=js_script, arg=element
        )

    async def scroll_to_top(self, draw_boxes: bool, frame: str, frame_index: int) -> float:
        """
        Scroll to the top of the page and take a screenshot.
        :param drow_boxes: If True, draw bounding boxes around the elements.
        :param page: Page instance to take the screenshot from.
        :return: Screenshot of the page.
        """
        js_script = "async ([draw_boxes, frame, frame_index]) => await safeScrollToTop(draw_boxes, frame, frame_index)"
        scroll_y_px = await self.evaluate(
            frame=self.frame,
            engine_selection=self.engine_selection,
            expression=js_script,
            timeout_ms=SettingsManager.get_settings().BROWSER_SCRAPING_BUILDING_ELEMENT_TREE_TIMEOUT_MS,
            arg=[draw_boxes, frame, frame_index],
        )
        if not isinstance(scroll_y_px, (int, float)):
            LOG.warning(
                "scroll_to_top returned non-numeric value, falling back to 0.0",
                scroll_y_px=scroll_y_px,
            )
            return 0.0
        return float(scroll_y_px)

    async def scroll_to_next_page(
        self, draw_boxes: bool, frame: str, frame_index: int, need_overlap: bool = True
    ) -> float:
        """
        Scroll to the next page and take a screenshot.
        :param drow_boxes: If True, draw bounding boxes around the elements.
        :param page: Page instance to take the screenshot from.
        :return: Screenshot of the page.
        """
        js_script = "async ([draw_boxes, frame, frame_index, need_overlap]) => await scrollToNextPage(draw_boxes, frame, frame_index, need_overlap)"
        scroll_y_px = await self.evaluate(
            frame=self.frame,
            engine_selection=self.engine_selection,
            expression=js_script,
            timeout_ms=SettingsManager.get_settings().BROWSER_SCRAPING_BUILDING_ELEMENT_TREE_TIMEOUT_MS,
            arg=[draw_boxes, frame, frame_index, need_overlap],
        )
        if not isinstance(scroll_y_px, (int, float)):
            LOG.warning(
                "scroll_to_next_page returned non-numeric value, falling back to 0.0",
                scroll_y_px=scroll_y_px,
            )
            return 0.0
        return float(scroll_y_px)

    async def remove_bounding_boxes(self) -> None:
        """
        Remove the bounding boxes from the page.
        :param page: Page instance to remove the bounding boxes from.
        """
        js_script = "() => removeBoundingBoxes()"
        await self.evaluate(
            frame=self.frame,
            engine_selection=self.engine_selection,
            expression=js_script,
            timeout_ms=SettingsManager.get_settings().BROWSER_SCRAPING_BUILDING_ELEMENT_TREE_TIMEOUT_MS,
        )

    async def build_elements_and_draw_bounding_boxes(self, frame: str, frame_index: int) -> None:
        js_script = "async ([frame, frame_index]) => await buildElementsAndDrawBoundingBoxes(frame, frame_index)"
        await self.evaluate(
            frame=self.frame,
            engine_selection=self.engine_selection,
            expression=js_script,
            timeout_ms=SettingsManager.get_settings().BROWSER_SCRAPING_BUILDING_ELEMENT_TREE_TIMEOUT_MS,
            arg=[frame, frame_index],
        )

    async def is_window_scrollable(self) -> bool:
        js_script = "() => isWindowScrollable()"
        return await self.evaluate(frame=self.frame, engine_selection=self.engine_selection, expression=js_script)

    async def is_parent(self, parent: ElementHandle, child: ElementHandle) -> bool:
        js_script = "([parent, child]) => isParent(parent, child)"
        return await self.evaluate(
            frame=self.frame, engine_selection=self.engine_selection, expression=js_script, arg=[parent, child]
        )

    async def is_sibling(self, el1: ElementHandle, el2: ElementHandle) -> bool:
        js_script = "([el1, el2]) => isSibling(el1, el2)"
        return await self.evaluate(
            frame=self.frame, engine_selection=self.engine_selection, expression=js_script, arg=[el1, el2]
        )

    async def has_ASP_client_control(self) -> bool:
        js_script = "() => hasASPClientControl()"
        return await self.evaluate(frame=self.frame, engine_selection=self.engine_selection, expression=js_script)

    async def click_element_in_javascript(self, element: ElementHandle) -> None:
        js_script = "(element) => element.click()"
        return await self.evaluate(
            frame=self.frame, engine_selection=self.engine_selection, expression=js_script, arg=element
        )

    async def read_autocomplete_option_identity(self, element: ElementHandle) -> dict[str, Any] | None:
        js_script = r"""
        (node) => {
            const normalize = (value) => (value ?? "").replace(/\s+/g, " ").trim();
            const attrs = node.getAttributeNames
                ? Object.fromEntries(node.getAttributeNames().map((name) => [name, node.getAttribute(name)]))
                : {};
            const label = normalize(
                node.textContent ||
                attrs["aria-label"] ||
                attrs.title ||
                attrs["data-value"] ||
                attrs.value
            );
            const parent = node.parentElement;
            const optionNodes = parent
                ? Array.from(parent.children).filter((element) => {
                    const role = (element.getAttribute("role") || "").toLowerCase();
                    const tag = element.tagName.toLowerCase();
                    return role === "option" || tag === "li" || element.hasAttribute("data-value");
                })
                : [];
            return { index: optionNodes.indexOf(node), label };
        }
        """
        identity = await self.evaluate(
            frame=self.frame, engine_selection=self.engine_selection, expression=js_script, arg=element
        )
        return identity if isinstance(identity, dict) else None

    async def remove_target_attr(self, element: ElementHandle) -> None:
        js_script = "(element) => element.removeAttribute('target')"
        return await self.evaluate(
            frame=self.frame, engine_selection=self.engine_selection, expression=js_script, arg=element
        )

    async def get_select_options(self, element: ElementHandle) -> tuple[list, str]:
        js_script = "([element]) => getSelectOptions(element)"
        return await self.evaluate(
            frame=self.frame, engine_selection=self.engine_selection, expression=js_script, arg=[element]
        )

    async def get_element_dom_depth(self, element: ElementHandle) -> int:
        js_script = "([element]) => getElementDomDepth(element)"
        return await self.evaluate(
            frame=self.frame, engine_selection=self.engine_selection, expression=js_script, arg=[element]
        )

    async def remove_all_unique_ids(self) -> None:
        js_script = "() => removeAllUniqueIds()"
        await self.evaluate(frame=self.frame, engine_selection=self.engine_selection, expression=js_script)

    async def _set_enriched_element_tree_flag(
        self,
        timeout_ms: float = SettingsManager.get_settings().BROWSER_ACTION_TIMEOUT_MS,
        *,
        deadline: float | None = None,
    ) -> None:
        context = skyvern_context.current()
        enriched_enabled = bool(context and context.enriched_tree_enabled())
        await self.evaluate(
            frame=self.frame,
            engine_selection=self.engine_selection,
            expression="([enabled]) => { window.GlobalEnableEnrichedElementTree = enabled; }",
            arg=[enriched_enabled],
            timeout_ms=timeout_ms,
            deadline=deadline,
        )

    @traced(name="skyvern.browser.element_tree_from_body")
    async def build_tree_from_body(
        self,
        frame_name: str | None,
        frame_index: int,
        must_included_tags: list[str] | None = None,
        timeout_ms: float = SettingsManager.get_settings().BROWSER_SCRAPING_BUILDING_ELEMENT_TREE_TIMEOUT_MS,
    ) -> tuple[list[dict], list[dict], dict[str, dict]]:
        must_included_tags = must_included_tags or []
        # Capture is flag-gated so a disabled-mode build does no destination-fact work on any page
        # that has not interposed on the builder global; a page that has can still force the flag,
        # and the per-build budget in domUtils is what bounds that. The strip below stays
        # unconditional: it is protection against a hostile wrapper injecting the key, not capture
        # cost.
        capture_destination_facts = policy_observation_enabled()
        js_script = "async ([frame_name, frame_index, must_included_tags, capture_destination_facts]) => await buildTreeFromBody(frame_name, frame_index, must_included_tags, capture_destination_facts)"

        # One monotonic budget across the flag write, both build attempts and the re-injection
        # between them -- as _evaluate_with_navigation_recovery does -- so the retry cannot double
        # what a stuck frame costs. A step that starts with nothing left times out immediately,
        # which is the SkyvernPageAnalysisTimeout the un-retried build already raised.
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_ms / 1000

        def remaining_ms() -> float:
            return max(0.0, (deadline - loop.time()) * 1000)

        async def build() -> Any:
            await self._set_enriched_element_tree_flag(timeout_ms=remaining_ms(), deadline=deadline)
            return await self.evaluate(
                frame=self.frame,
                engine_selection=self.engine_selection,
                expression=js_script,
                timeout_ms=remaining_ms(),
                arg=[frame_name, frame_index, must_included_tags, capture_destination_facts],
                deadline=deadline,
            )

        tree = _as_element_tree_pair(await build())
        if tree is None:
            # Callers scraping an iframe swallow a raise here and drop the whole frame from the tree,
            # so spend one re-injection before giving up: a silent non-pair is the same lost JS world
            # the raised-ReferenceError path already recovers from, minus an error to key on.
            LOG.warning(
                "Element tree builder returned no tree, re-injecting domUtils.js and retrying",
                url=redact_url_secrets(self.frame.url),
            )
            await self.evaluate(
                frame=self.frame,
                engine_selection=self.engine_selection,
                expression=JS_FUNCTION_DEFS,
                timeout_ms=remaining_ms(),
                deadline=deadline,
            )
            retried = await build()
            tree = _as_element_tree_pair(retried)
            if tree is None:
                raise ElementTreeBuildFailed(returned=_describe_non_pair(retried))

        elements, element_tree = tree
        destinations = pop_destination_facts(elements)
        destinations.update(pop_destination_facts(element_tree))
        return elements, element_tree, destinations

    @traced(name="skyvern.browser.incremental_element_tree")
    async def get_incremental_element_tree(
        self,
        wait_until_finished: bool = True,
        timeout_ms: float = SettingsManager.get_settings().BROWSER_SCRAPING_BUILDING_ELEMENT_TREE_TIMEOUT_MS,
    ) -> tuple[list[dict], list[dict]]:
        await self._set_enriched_element_tree_flag()
        js_script = "async ([wait_until_finished]) => await getIncrementElements(wait_until_finished)"
        result = await self.evaluate(
            frame=self.frame,
            engine_selection=self.engine_selection,
            expression=js_script,
            timeout_ms=timeout_ms,
            arg=[wait_until_finished],
        )
        # No re-injection retry here, unlike build_tree_from_body: that one rebuilds from the live
        # DOM so a fresh JS world still answers correctly, while this one reports mutations an
        # earlier startGlobalIncrementalObserver accumulated, and a world that lost them answers
        # [[], []] -- "nothing appeared" -- rather than raising. A world that still has them
        # re-answers identically, so the retry buys nothing either way.
        tree = _as_element_tree_pair(result)
        if tree is None:
            raise ElementTreeBuildFailed(returned=_describe_non_pair(result))
        elements, element_tree = tree
        pop_destination_facts(elements)
        pop_destination_facts(element_tree)
        return elements, element_tree

    @traced(name="skyvern.browser.element_tree_from_element")
    async def build_tree_from_element(
        self,
        starter: ElementHandle,
        frame: str,
        full_tree: bool = False,
        timeout_ms: float = SettingsManager.get_settings().BROWSER_SCRAPING_BUILDING_ELEMENT_TREE_TIMEOUT_MS,
    ) -> tuple[list[dict], list[dict]]:
        await self._set_enriched_element_tree_flag()
        js_script = "async ([starter, frame, full_tree]) => await buildElementTree(starter, frame, full_tree)"
        result = await self.evaluate(
            frame=self.frame,
            engine_selection=self.engine_selection,
            expression=js_script,
            timeout_ms=timeout_ms,
            arg=[starter, frame, full_tree],
        )
        tree = _as_element_tree_pair(result)
        if tree is None:
            raise ElementTreeBuildFailed(returned=_describe_non_pair(result))
        elements, element_tree = tree
        pop_destination_facts(elements)
        pop_destination_facts(element_tree)
        return elements, element_tree

    @traced(name="skyvern.browser.wait_for_animation")
    async def safe_wait_for_animation_end(
        self,
        before_wait_sec: float = 0,
        timeout_ms: float = 3000,
        caller: str = "unknown",
    ) -> None:
        # Fast finished-quickly path vs timeout/error paths that burn the full
        # timeout budget — the 124x p95/p50 ratio in production traces.
        _span = otel_trace.get_current_span()
        _span.set_attribute("before_wait_sec", before_wait_sec)
        _span.set_attribute("timeout_ms", timeout_ms)
        _span.set_attribute("caller", caller)
        try:
            await asyncio.sleep(before_wait_sec)
            await self.frame.wait_for_load_state("load", timeout=timeout_ms)
            await self.wait_for_animation_end(timeout_ms=timeout_ms)
            _span.set_attribute("animation_result", "finished")
        except Exception as exc:
            if _is_readiness_timeout(exc, self.engine_selection):
                _span.set_attribute("animation_result", "timeout")
                LOG.debug("Timed out waiting for animation end, but ignore it", exc_info=True)
                return
            _span.set_attribute("animation_result", "error")
            LOG.debug("Failed to wait for animation end, but ignore it", exc_info=True)
            return

    async def wait_for_animation_end(self, timeout_ms: float = 3000) -> None:
        async with asyncio.timeout(timeout_ms / 1000):
            while True:
                is_finished = await self.evaluate(
                    frame=self.frame,
                    engine_selection=self.engine_selection,
                    expression="() => isAnimationFinished()",
                    timeout_ms=timeout_ms,
                )
                if is_finished:
                    return
                await asyncio.sleep(0.1)

    @traced(name="skyvern.browser.page_ready", role="wrapper")
    async def wait_for_page_ready(
        self,
        network_idle_timeout_ms: float = 3000,
        loading_indicator_timeout_ms: float = 5000,
        dom_stable_ms: float = 300,
        dom_stability_timeout_ms: float = 3000,
    ) -> None:
        """
        Wait for page to be ready for interaction by checking multiple signals:
        1. Loading indicators gone (spinners, skeletons, progress bars) - highest timeout first
        2. Network idle (no pending requests for 500ms)
        3. DOM stability (no significant mutations for dom_stable_ms)

        Checks are ordered by timeout (highest first) so the longest timeout
        acts as the primary upper bound when checks complete early.

        This is designed for cached action execution to ensure the page is ready
        before attempting to interact with elements.
        """
        _tracer = otel_trace.get_tracer("skyvern")

        # 1. Wait for loading indicators to disappear (longest timeout first)
        loading_indicator_result = "success"
        with traced_span(_tracer, "skyvern.browser.page_ready.loading_indicators") as _li_span:
            apply_context_attrs(_li_span)
            _li_span.set_attribute("timeout_ms", loading_indicator_timeout_ms)
            try:
                await self._wait_for_loading_indicators_gone(timeout_ms=loading_indicator_timeout_ms)
            except Exception as exc:
                if _is_readiness_timeout(exc, self.engine_selection):
                    loading_indicator_result = "timeout"
                    LOG.info(
                        "Loading indicator timeout - some indicators may still be present, proceeding", sampling=True
                    )
                else:
                    loading_indicator_result = "error"
                    LOG.warning("Failed to check loading indicators, proceeding", exc_info=True)
            finally:
                _li_span.set_attribute("result", loading_indicator_result)

        # 2. Wait for network idle (with short timeout - some pages never go idle)
        network_idle_result = "success"
        with traced_span(_tracer, "skyvern.browser.page_ready.network_idle") as _ni_span:
            apply_context_attrs(_ni_span)
            _ni_span.set_attribute("timeout_ms", network_idle_timeout_ms)
            try:
                await self.frame.wait_for_load_state("networkidle", timeout=network_idle_timeout_ms)
            except Exception as exc:
                if _is_readiness_timeout(exc, self.engine_selection):
                    network_idle_result = "timeout"
                    LOG.info("Network idle timeout - page may have constant activity, proceeding", sampling=True)
                else:
                    network_idle_result = "error"
                    LOG.warning("Failed to check network idle, proceeding", exc_info=True)
            finally:
                _ni_span.set_attribute("result", network_idle_result)

        # 3. Wait for DOM to stabilize
        dom_stability_result = "success"
        with traced_span(_tracer, "skyvern.browser.page_ready.dom_stability") as _ds_span:
            apply_context_attrs(_ds_span)
            _ds_span.set_attribute("timeout_ms", dom_stability_timeout_ms)
            _ds_span.set_attribute("stable_ms", dom_stable_ms)
            try:
                await self._wait_for_dom_stable(stable_ms=dom_stable_ms, timeout_ms=dom_stability_timeout_ms)
            except Exception as exc:
                if _is_readiness_timeout(exc, self.engine_selection):
                    dom_stability_result = "timeout"
                    LOG.warning("DOM stability timeout - DOM may still be changing, proceeding")
                else:
                    dom_stability_result = "error"
                    LOG.warning("Failed to check DOM stability, proceeding", exc_info=True)
            finally:
                _ds_span.set_attribute("result", dom_stability_result)

    async def _wait_for_loading_indicators_gone(self, timeout_ms: float = 5000) -> None:
        """
        Wait for common loading indicators to disappear from the page.
        Checks for spinners, skeletons, progress bars, and loading overlays.
        """
        # JavaScript to detect loading indicators
        loading_indicator_js = """
        () => {
            // Common loading indicator selectors
            const selectors = [
                // Class-based spinners and loaders
                '[class*="spinner"]',
                '[class*="loading"]',
                '[class*="loader"]',
                '[class*="skeleton"]',
                '[class*="progress"]',
                '[class*="shimmer"]',
                // Role-based
                '[role="progressbar"]',
                '[role="status"][aria-busy="true"]',
                // Aria attributes
                '[aria-busy="true"]',
                '[aria-live="polite"][aria-busy="true"]',
                // Common loading overlay patterns
                '.loading-overlay',
                '.page-loading',
                '.content-loading',
                // SVG spinners
                'svg[class*="spin"]',
                'svg[class*="loading"]',
            ];

            for (const selector of selectors) {
                try {
                    const elements = document.querySelectorAll(selector);
                    for (const el of elements) {
                        // Check if element is visible
                        const style = window.getComputedStyle(el);
                        const rect = el.getBoundingClientRect();
                        const isVisible = (
                            style.display !== 'none' &&
                            style.visibility !== 'hidden' &&
                            style.opacity !== '0' &&
                            rect.width > 0 &&
                            rect.height > 0
                        );
                        if (isVisible) {
                            return true;  // Loading indicator found
                        }
                    }
                } catch (e) {
                    // Ignore selector errors
                }
            }
            return false;  // No loading indicators found
        }
        """

        async with asyncio.timeout(timeout_ms / 1000):
            while True:
                has_loading_indicator = await self.evaluate(
                    frame=self.frame,
                    engine_selection=self.engine_selection,
                    expression=loading_indicator_js,
                    timeout_ms=timeout_ms,
                )
                if not has_loading_indicator:
                    LOG.debug("No loading indicators detected")
                    return
                await asyncio.sleep(0.1)

    async def _wait_for_dom_stable(self, stable_ms: float = 300, timeout_ms: float = 3000) -> None:
        """
        Wait for DOM to stabilize (no significant mutations for stable_ms milliseconds).
        Uses MutationObserver to detect DOM changes.
        """
        dom_stability_js = f"""
        () => new Promise((resolve) => {{
            let lastMutationTime = Date.now();
            let resolved = false;

            const observer = new MutationObserver((mutations) => {{
                // Filter out insignificant mutations (attribute changes on non-visible elements)
                const significantMutations = mutations.filter(m => {{
                    if (m.type === 'childList') return true;
                    if (m.type === 'characterData') return true;
                    if (m.type === 'attributes') {{
                        const el = m.target;
                        if (el.nodeType !== 1) return false;
                        const rect = el.getBoundingClientRect();
                        // Only count attribute changes on visible elements
                        return rect.width > 0 && rect.height > 0;
                    }}
                    return false;
                }});

                if (significantMutations.length > 0) {{
                    lastMutationTime = Date.now();
                }}
            }});

            observer.observe(document.body, {{
                childList: true,
                subtree: true,
                attributes: true,
                characterData: true,
            }});

            const checkStability = () => {{
                if (resolved) return;
                const timeSinceLastMutation = Date.now() - lastMutationTime;
                if (timeSinceLastMutation >= {stable_ms}) {{
                    resolved = true;
                    observer.disconnect();
                    resolve(true);
                }} else {{
                    setTimeout(checkStability, 50);
                }}
            }};

            // Start checking after a brief delay to catch initial mutations
            setTimeout(checkStability, 50);
        }})
        """

        await self.evaluate(
            frame=self.frame,
            engine_selection=self.engine_selection,
            expression=dom_stability_js,
            timeout_ms=timeout_ms,
        )
