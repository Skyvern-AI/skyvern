from __future__ import annotations

import asyncio
import base64
import json
import time
import urllib.parse
from enum import StrEnum
from io import BytesIO
from typing import TYPE_CHECKING, Any

import structlog
from opentelemetry import trace as otel_trace
from PIL import Image
from playwright._impl._errors import Error as PlaywrightError
from playwright._impl._errors import TimeoutError
from playwright.async_api import ElementHandle, Frame, Page

from skyvern.constants import PAGE_CONTENT_TIMEOUT, SKYVERN_DIR
from skyvern.exceptions import FailedToTakeScreenshot
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.settings_manager import SettingsManager
from skyvern.forge.sdk.trace import apply_context_attrs, traced
from skyvern.webeye.main_world_eval import evaluate_in_main_world, get_main_world_prefix

if TYPE_CHECKING:
    from skyvern.webeye.browser_state import BrowserState

LOG = structlog.get_logger()


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


def load_js_script() -> str:
    # TODO: Handle file location better. This is a hacky way to find the file location.
    path = f"{SKYVERN_DIR}/webeye/scraper/domUtils.js"
    try:
        # TODO: Implement TS of domUtils.js and use the complied JS file instead of the raw JS file.
        # This will allow our code to be type safe.
        with open(path, encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError as e:
        LOG.exception("Failed to load the JS script", path=path)
        raise e


JS_FUNCTION_DEFS = load_js_script()

_NAVIGATION_RECOVERY_MAX_ATTEMPTS = 4
_NAVIGATION_SETTLE_TIMEOUT_MS = 3000


def _is_navigation_context_lost(error_msg: str) -> bool:
    if "Execution context was destroyed" in error_msg:
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


async def _dispatch_evaluate(frame: Page | Frame, expression: str, arg: Any | None) -> Any:
    # Page + prefix + JSON-safe arg → main-world hook (preserves the marker).
    # Iframe Frames and non-JSON args fall back to per-frame evaluate so iframe
    # contexts and Playwright handle-marshalling keep working.
    if not isinstance(frame, Page):
        return await frame.evaluate(expression=expression, arg=arg)
    if get_main_world_prefix(frame.context) is None:
        return await frame.evaluate(expression=expression, arg=arg)
    if arg is not None and not _is_json_inlinable(arg):
        return await frame.evaluate(expression=expression, arg=arg)
    return await evaluate_in_main_world(frame, expression, arg)


async def _wait_for_navigation_settle(frame: Page | Frame, timeout_ms: float) -> None:
    if timeout_ms <= 0:
        return
    try:
        await frame.wait_for_load_state("networkidle", timeout=timeout_ms)
    except PlaywrightError:
        return


async def _wait_for_screenshot_load_state(page: Page, timeout_ms: float) -> None:
    # Best-effort readiness guard before capturing. 'domcontentloaded' fires far
    # earlier than 'load'; pages with streaming/long-polling/SSE/websockets or a
    # persistent spinner may never fire 'load', so a timeout here must be
    # non-fatal — the capture has its own (separate) timeout budget.
    if timeout_ms <= 0:
        return
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
    except (PlaywrightError, TimeoutError):
        LOG.warning("Page did not reach domcontentloaded before screenshot; capturing current state anyway")


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
    except TimeoutError as timeout_error:
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
) -> bytes:
    if page.is_closed():
        raise FailedToTakeScreenshot(error_message="Page is closed")

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
                page, timeout_ms=SettingsManager.get_settings().BROWSER_SCREENSHOT_LOAD_STATE_TIMEOUT_MS
            )
        start_time = time.time()
        screenshot: bytes = b""
        if file_path:
            screenshot = await _page_screenshot_helper(
                page=page, file_path=file_path, full_page=full_page, timeout=timeout
            )
        else:
            screenshot = await _page_screenshot_helper(page=page, full_page=full_page, timeout=timeout)
        end_time = time.time()
        LOG.debug(
            "Screenshot taking time",
            screenshot_time=end_time - start_time,
            file_path=file_path,
        )
        return screenshot
    except TimeoutError as e:
        LOG.error(
            "Screenshot timeout",
            timeout_ms=timeout,
            url=url,
            viewport=viewport_info,
            full_page=full_page,
            mode=mode.value if hasattr(mode, "value") else str(mode),
            error=str(e),
        )
        raise FailedToTakeScreenshot(error_message=str(e)) from e
    except Exception as e:
        LOG.error(
            "Screenshot failed",
            url=url,
            viewport=viewport_info,
            full_page=full_page,
            error=str(e),
            exc_info=True,
        )
        raise FailedToTakeScreenshot(error_message=str(e)) from e


async def _scrolling_screenshots_helper(
    page: Page,
    url: str | None = None,
    draw_boxes: bool = False,
    max_number: int = SettingsManager.get_settings().MAX_NUM_SCREENSHOTS,
    mode: ScreenshotMode = ScreenshotMode.DETAILED,
) -> tuple[list[bytes], list[int]]:
    # page is the main frame and the index must be 0
    skyvern_page = await SkyvernFrame.create_instance(frame=page)
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

            screenshot = await _current_viewpoint_screenshot_helper(page=page, mode=mode)
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
        screenshot = await _current_viewpoint_screenshot_helper(page=page, mode=mode)
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

        merged_img.paste(cropped, (0, current_y))
        current_y += cropped.height

    return merged_img


# FileReader keeps the payload binary-safe without arrayBuffer/Uint8Array
# transcoding back across CDP.
_BLOB_FETCH_JS = """
async (args) => {
    try {
        const { blobUrl, maxSizeBytes } = args;
        const response = await fetch(blobUrl);
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


def _frames_for_blob_origin(page: Page, blob_origin: str) -> list[Frame]:
    """Return frames whose origin matches the blob's origin, main frame first."""
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
        if _frame_origin(frame_url) == blob_origin:
            matches.append(frame)
    return matches


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


def is_browser_crashed_error(exc: BaseException) -> bool:
    """True for an environmental renderer/target crash or a closed page/context/browser
    (e.g. ``Page.content: Target crashed``, ``Target closed``). These are not Skyvern
    defects and every ``get_content`` caller already degrades gracefully, so they warrant
    a warning rather than an error in tracking. SKY-12344."""
    msg = str(exc).lower()
    return "target crashed" in msg or "page crashed" in msg or "target closed" in msg or "has been closed" in msg


class SkyvernFrame:
    @staticmethod
    async def evaluate(
        frame: Page | Frame,
        expression: str,
        arg: Any | None = None,
        timeout_ms: float = SettingsManager.get_settings().BROWSER_ACTION_TIMEOUT_MS,
    ) -> Any:
        try:
            async with asyncio.timeout(timeout_ms / 1000):
                return await _dispatch_evaluate(frame, expression, arg)
        except PlaywrightError as e:
            error_msg = str(e)
            if not _is_navigation_context_lost(error_msg):
                raise
            return await SkyvernFrame._evaluate_with_navigation_recovery(
                frame=frame,
                expression=expression,
                arg=arg,
                timeout_ms=timeout_ms,
                initial_error=error_msg,
            )
        except RuntimeError as e:
            # `evaluate_in_main_world` raises RuntimeError on Runtime.evaluate
            # exception payloads; only navigation-context-lost text recovers here.
            error_msg = str(e)
            if not _is_navigation_context_lost(error_msg):
                raise
            return await SkyvernFrame._evaluate_with_navigation_recovery(
                frame=frame,
                expression=expression,
                arg=arg,
                timeout_ms=timeout_ms,
                initial_error=error_msg,
            )
        except asyncio.TimeoutError:
            # Re-raised and handled by the caller (scrape retries / failure classification),
            # so this is not the failure boundary; log without a traceback at warning.
            LOG.warning("Skyvern timed out trying to analyze the page", expression=expression)
            raise TimeoutError("Skyvern timed out trying to analyze the page")

    @staticmethod
    async def _evaluate_with_navigation_recovery(
        frame: Page | Frame,
        expression: str,
        arg: Any | None,
        timeout_ms: float,
        initial_error: str,
    ) -> Any:
        # Multi-hop SSO/OIDC flows (especially response_mode=form_post) can destroy
        # the JS execution context several times in a row as the page auto-submits
        # through redirects. Wait for the page to settle between attempts instead
        # of racing the next navigation. The whole recovery shares one monotonic
        # deadline so retries can't compound into many multiples of timeout_ms.
        per_attempt_seconds = timeout_ms / 1000
        loop = asyncio.get_running_loop()
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
                raise TimeoutError("Skyvern timed out trying to analyze the page")

            LOG.warning(
                "JS execution context lost (likely due to page navigation), re-injecting domUtils.js and retrying",
                attempt=attempt,
                expression=expression[:200],
                error=last_error_msg[:200],
            )
            settle_ms = min(_NAVIGATION_SETTLE_TIMEOUT_MS, _remaining_seconds() * 1000)
            await _wait_for_navigation_settle(frame, timeout_ms=settle_ms)

            inject_budget = min(per_attempt_seconds, _remaining_seconds())
            if inject_budget <= 0:
                LOG.error(
                    "Skyvern timed out trying to analyze the page after navigation recovery",
                    expression=expression,
                )
                raise TimeoutError("Skyvern timed out trying to analyze the page")
            try:
                async with asyncio.timeout(inject_budget):
                    # Same dispatch helper so a prefixed Page re-injects
                    # JS_FUNCTION_DEFS via Runtime.evaluate (preserving the marker).
                    await _dispatch_evaluate(frame, JS_FUNCTION_DEFS, None)
            except asyncio.TimeoutError:
                LOG.exception(
                    "Skyvern timed out trying to analyze the page during domUtils.js re-injection",
                    expression=expression,
                )
                raise TimeoutError("Skyvern timed out trying to analyze the page")
            except (PlaywrightError, RuntimeError) as inject_err:
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
                raise TimeoutError("Skyvern timed out trying to analyze the page")
            try:
                async with asyncio.timeout(retry_budget):
                    return await _dispatch_evaluate(frame, expression, arg)
            except asyncio.TimeoutError:
                LOG.exception("Skyvern timed out on retry after JS context re-injection", expression=expression)
                raise TimeoutError("Skyvern timed out trying to analyze the page")
            except (PlaywrightError, RuntimeError) as retry_err:
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
        blob_arg = {"blobUrl": blob_url, "maxSizeBytes": max_size_bytes}
        main_frame = page.main_frame
        for frame in frames:
            try:
                # Main-frame routes through evaluate_in_main_world so any
                # context-level main-world prefix stays attached; sub-frames use
                # frame.evaluate (main-world prefixes are page-scoped).
                if frame is main_frame:
                    result = await evaluate_in_main_world(page, _BLOB_FETCH_JS, blob_arg)
                else:
                    result = await frame.evaluate(_BLOB_FETCH_JS, blob_arg)
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
    ) -> bytes:
        if scrolling_number <= 0:
            return await _current_viewpoint_screenshot_helper(
                page=page, file_path=file_path, timeout=timeout, mode=mode
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
        skyvern_frame = await SkyvernFrame.create_instance(frame=page)
        x: int | None = None
        y: int | None = None
        try:
            x, y = await skyvern_frame.get_scroll_x_y()
            async with asyncio.timeout(timeout):
                screenshots, positions = await _scrolling_screenshots_helper(
                    page=page, mode=mode, max_number=scrolling_number
                )
                images = []

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
        except Exception:
            LOG.warning(
                "Failed to take full page screenshot, fallback to use playwright full page screenshot",
                exc_info=True,
            )
            # reset x and y to None to avoid the scroll_to_x_y call in finally block
            x = None
            y = None
            return await _current_viewpoint_screenshot_helper(
                page=page, file_path=file_path, timeout=timeout, full_page=True
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
    ) -> list[bytes]:
        if not scroll:
            return [await _current_viewpoint_screenshot_helper(page=page, mode=ScreenshotMode.DETAILED)]

        screenshots, _ = await _scrolling_screenshots_helper(
            page=page,
            url=url,
            max_number=max_number,
            draw_boxes=draw_boxes,
            mode=ScreenshotMode.DETAILED,
        )
        return screenshots

    @classmethod
    async def create_instance(cls, frame: Page | Frame) -> SkyvernFrame:
        instance = cls(frame=frame)
        await cls.evaluate(frame=instance.frame, expression=JS_FUNCTION_DEFS)
        if SettingsManager.get_settings().ENABLE_EXP_ALL_TEXTUAL_ELEMENTS_INTERACTABLE:
            await instance.evaluate(
                frame=instance.frame, expression="() => window.GlobalEnableAllTextualElements = true"
            )
        return instance

    def __init__(self, frame: Page | Frame) -> None:
        self.frame = frame

    def get_frame(self) -> Page | Frame:
        return self.frame

    @traced(name="skyvern.browser.get_content")
    async def get_content(self, timeout: float = PAGE_CONTENT_TIMEOUT) -> str:
        async with asyncio.timeout(timeout):
            return await self.frame.content()

    async def get_scroll_x_y(self) -> tuple[int, int]:
        js_script = "() => getScrollXY()"
        return await self.evaluate(frame=self.frame, expression=js_script)

    async def get_scroll_width_and_height(self) -> tuple[int, int]:
        js_script = "() => getScrollWidthAndHeight()"
        return await self.evaluate(frame=self.frame, expression=js_script)

    async def scroll_to_x_y(self, x: int, y: int) -> None:
        js_script = "([x, y]) => scrollToXY(x, y)"
        return await self.evaluate(frame=self.frame, expression=js_script, arg=[x, y])

    async def safe_scroll_to_x_y(self, x: int, y: int) -> None:
        try:
            await self.scroll_to_x_y(x, y)
        except Exception:
            LOG.warning("Failed to scroll to x, y, ignore it", x=x, y=y, exc_info=True)

    async def scroll_into_view(self, element: ElementHandle) -> None:
        """Scroll all ancestor containers (including nested ones with overflow-y: auto)
        so that the element is centered in the viewport."""
        js_script = "(element) => element.scrollIntoView({block: 'center', inline: 'center', behavior: 'instant'})"
        return await self.evaluate(frame=self.frame, expression=js_script, arg=element)

    async def scroll_to_element_bottom(self, element: ElementHandle, page_by_page: bool = False) -> None:
        js_script = "([element, page_by_page]) => scrollToElementBottom(element, page_by_page)"
        return await self.evaluate(frame=self.frame, expression=js_script, arg=[element, page_by_page])

    async def scroll_to_element_top(self, element: ElementHandle) -> None:
        js_script = "(element) => scrollToElementTop(element)"
        return await self.evaluate(frame=self.frame, expression=js_script, arg=element)

    async def parse_element_from_html(self, frame: str, element: ElementHandle, interactable: bool) -> dict:
        js_script = "async ([frame, element, interactable]) => await buildElementObject(frame, element, interactable)"
        return await self.evaluate(frame=self.frame, expression=js_script, arg=[frame, element, interactable])

    async def get_element_scrollable(self, element: ElementHandle) -> bool:
        js_script = "(element) => isScrollable(element)"
        return await self.evaluate(frame=self.frame, expression=js_script, arg=element)

    async def get_element_visible(self, element: ElementHandle) -> bool:
        js_script = "(element) => isElementVisible(element) && !isHidden(element)"
        return await self.evaluate(frame=self.frame, expression=js_script, arg=element)

    async def get_disabled_from_style(self, element: ElementHandle) -> bool:
        js_script = "(element) => checkDisabledFromStyle(element)"
        return await self.evaluate(frame=self.frame, expression=js_script, arg=element)

    async def get_blocking_element_id(self, element: ElementHandle) -> tuple[str, bool]:
        js_script = "(element) => getBlockElementUniqueID(element)"
        return await self.evaluate(frame=self.frame, expression=js_script, arg=element)

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
            expression=js_script,
            timeout_ms=SettingsManager.get_settings().BROWSER_SCRAPING_BUILDING_ELEMENT_TREE_TIMEOUT_MS,
        )

    async def build_elements_and_draw_bounding_boxes(self, frame: str, frame_index: int) -> None:
        js_script = "async ([frame, frame_index]) => await buildElementsAndDrawBoundingBoxes(frame, frame_index)"
        await self.evaluate(
            frame=self.frame,
            expression=js_script,
            timeout_ms=SettingsManager.get_settings().BROWSER_SCRAPING_BUILDING_ELEMENT_TREE_TIMEOUT_MS,
            arg=[frame, frame_index],
        )

    async def is_window_scrollable(self) -> bool:
        js_script = "() => isWindowScrollable()"
        return await self.evaluate(frame=self.frame, expression=js_script)

    async def is_parent(self, parent: ElementHandle, child: ElementHandle) -> bool:
        js_script = "([parent, child]) => isParent(parent, child)"
        return await self.evaluate(frame=self.frame, expression=js_script, arg=[parent, child])

    async def is_sibling(self, el1: ElementHandle, el2: ElementHandle) -> bool:
        js_script = "([el1, el2]) => isSibling(el1, el2)"
        return await self.evaluate(frame=self.frame, expression=js_script, arg=[el1, el2])

    async def has_ASP_client_control(self) -> bool:
        js_script = "() => hasASPClientControl()"
        return await self.evaluate(frame=self.frame, expression=js_script)

    async def click_element_in_javascript(self, element: ElementHandle) -> None:
        js_script = "(element) => element.click()"
        return await self.evaluate(frame=self.frame, expression=js_script, arg=element)

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
        identity = await self.evaluate(frame=self.frame, expression=js_script, arg=element)
        return identity if isinstance(identity, dict) else None

    async def remove_target_attr(self, element: ElementHandle) -> None:
        js_script = "(element) => element.removeAttribute('target')"
        return await self.evaluate(frame=self.frame, expression=js_script, arg=element)

    async def get_select_options(self, element: ElementHandle) -> tuple[list, str]:
        js_script = "([element]) => getSelectOptions(element)"
        return await self.evaluate(frame=self.frame, expression=js_script, arg=[element])

    async def get_element_dom_depth(self, element: ElementHandle) -> int:
        js_script = "([element]) => getElementDomDepth(element)"
        return await self.evaluate(frame=self.frame, expression=js_script, arg=[element])

    async def remove_all_unique_ids(self) -> None:
        js_script = "() => removeAllUniqueIds()"
        await self.evaluate(frame=self.frame, expression=js_script)

    async def _set_enriched_element_tree_flag(self) -> None:
        context = skyvern_context.current()
        enriched_enabled = bool(context and context.enriched_tree_enabled())
        await self.evaluate(
            frame=self.frame,
            expression="([enabled]) => { window.GlobalEnableEnrichedElementTree = enabled; }",
            arg=[enriched_enabled],
        )

    @traced(name="skyvern.browser.element_tree_from_body")
    async def build_tree_from_body(
        self,
        frame_name: str | None,
        frame_index: int,
        must_included_tags: list[str] | None = None,
        timeout_ms: float = SettingsManager.get_settings().BROWSER_SCRAPING_BUILDING_ELEMENT_TREE_TIMEOUT_MS,
    ) -> tuple[list[dict], list[dict]]:
        must_included_tags = must_included_tags or []
        await self._set_enriched_element_tree_flag()
        js_script = "async ([frame_name, frame_index, must_included_tags]) => await buildTreeFromBody(frame_name, frame_index, must_included_tags)"
        return await self.evaluate(
            frame=self.frame,
            expression=js_script,
            timeout_ms=timeout_ms,
            arg=[frame_name, frame_index, must_included_tags],
        )

    @traced(name="skyvern.browser.incremental_element_tree")
    async def get_incremental_element_tree(
        self,
        wait_until_finished: bool = True,
        timeout_ms: float = SettingsManager.get_settings().BROWSER_SCRAPING_BUILDING_ELEMENT_TREE_TIMEOUT_MS,
    ) -> tuple[list[dict], list[dict]]:
        await self._set_enriched_element_tree_flag()
        js_script = "async ([wait_until_finished]) => await getIncrementElements(wait_until_finished)"
        return await self.evaluate(
            frame=self.frame, expression=js_script, timeout_ms=timeout_ms, arg=[wait_until_finished]
        )

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
        return await self.evaluate(
            frame=self.frame, expression=js_script, timeout_ms=timeout_ms, arg=[starter, frame, full_tree]
        )

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
        except (TimeoutError, asyncio.TimeoutError):
            _span.set_attribute("animation_result", "timeout")
            LOG.debug("Timed out waiting for animation end, but ignore it", exc_info=True)
            return
        except Exception:
            _span.set_attribute("animation_result", "error")
            LOG.debug("Failed to wait for animation end, but ignore it", exc_info=True)
            return

    async def wait_for_animation_end(self, timeout_ms: float = 3000) -> None:
        async with asyncio.timeout(timeout_ms / 1000):
            while True:
                is_finished = await self.evaluate(
                    frame=self.frame,
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
        with _tracer.start_as_current_span("skyvern.browser.page_ready.loading_indicators") as _li_span:
            apply_context_attrs(_li_span)
            _li_span.set_attribute("timeout_ms", loading_indicator_timeout_ms)
            try:
                await self._wait_for_loading_indicators_gone(timeout_ms=loading_indicator_timeout_ms)
            except (TimeoutError, asyncio.TimeoutError):
                loading_indicator_result = "timeout"
                LOG.info("Loading indicator timeout - some indicators may still be present, proceeding", sampling=True)
            except Exception:
                loading_indicator_result = "error"
                LOG.warning("Failed to check loading indicators, proceeding", exc_info=True)
            finally:
                _li_span.set_attribute("result", loading_indicator_result)

        # 2. Wait for network idle (with short timeout - some pages never go idle)
        network_idle_result = "success"
        with _tracer.start_as_current_span("skyvern.browser.page_ready.network_idle") as _ni_span:
            apply_context_attrs(_ni_span)
            _ni_span.set_attribute("timeout_ms", network_idle_timeout_ms)
            try:
                await self.frame.wait_for_load_state("networkidle", timeout=network_idle_timeout_ms)
            except (TimeoutError, asyncio.TimeoutError):
                network_idle_result = "timeout"
                LOG.info("Network idle timeout - page may have constant activity, proceeding", sampling=True)
            except Exception:
                network_idle_result = "error"
                LOG.warning("Failed to check network idle, proceeding", exc_info=True)
            finally:
                _ni_span.set_attribute("result", network_idle_result)

        # 3. Wait for DOM to stabilize
        dom_stability_result = "success"
        with _tracer.start_as_current_span("skyvern.browser.page_ready.dom_stability") as _ds_span:
            apply_context_attrs(_ds_span)
            _ds_span.set_attribute("timeout_ms", dom_stability_timeout_ms)
            _ds_span.set_attribute("stable_ms", dom_stable_ms)
            try:
                await self._wait_for_dom_stable(stable_ms=dom_stable_ms, timeout_ms=dom_stability_timeout_ms)
            except (TimeoutError, asyncio.TimeoutError):
                dom_stability_result = "timeout"
                LOG.warning("DOM stability timeout - DOM may still be changing, proceeding")
            except Exception:
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
            expression=dom_stability_js,
            timeout_ms=timeout_ms,
        )
