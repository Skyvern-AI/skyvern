"""Captcha-solving tool for the native Task V3 engine.

The tool-loop is main-frame only: a captcha rendered in a cross-origin iframe (Cloudflare Turnstile,
reCAPTCHA) is not enumerated by ``observe``, so the model cannot perceive the gate and re-clicks a dead
submit. This tool gives the loop an explicit ``solve_captcha`` action that drives the shared solver
ladder (which detects the challenge via DOM/iframe markers and operates it), then returns an honest
tri-state so the model stops blind-hammering. Solving routes through the ``AGENT_FUNCTION`` seam, so
this module stays OSS-clean.

An image-text captcha (distorted characters in an image beside a text box) has no widget for the ladder to
find. For that one the model names the image with ``image_selector``; the tool screenshots only that element,
reads it through the OCR seam, and hands the characters back for the model to type -- the model still picks
the image and the field.

Solved and "no challenge detected" are both ``ok`` -- absent is not an error and must not force a
retry -- so every ``ok`` branch here names an ``ok_class``. That is what keeps the tri-state legible
on the tool-call record; ``tool_status`` alone cannot tell a working detector from a blind one.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any
from urllib.parse import urlsplit

import structlog

from skyvern.config import settings
from skyvern.forge import app
from skyvern.forge.sdk.schemas.tasks import Task
from skyvern.forge.taskv3.loop import REF_SELECTOR_RE, ToolResult, ToolSpec
from skyvern.forge.taskv3.tools import PageProvider, _invalid_selector_result, _normalize_selector
from skyvern.webeye.utils.captcha_solver import (
    CAPTCHA_IMAGE_TAGS,
    MAX_IMAGE_CAPTCHA_READS,
    CaptchaChallengeUnsolvedError,
    resolve_captcha_image,
    solve_challenge_ladder,
)

LOG = structlog.get_logger()

# The ladder clamps its bounded arms to _LADDER_BUDGET_SECONDS (110s), but its DOM-checkbox arm relies on
# Playwright's own click timeouts rather than that budget; this ceiling is the hard stop above both, well
# under the run deadline, so a wedged solver cannot outlive the tool. NB: should_cancel is checked by the
# loop BETWEEN tool calls, not inside a handler, so a solve in flight blocks cancellation for up to this
# ceiling (accepted; v1's cascade blocks up to 600s).
_SOLVE_CAPTCHA_CEILING_SECONDS = 120

# A pathological loop must not keep running the solver on an unsolvable gate. Bounds CONSECUTIVE FAILED
# solve attempts per task, reset by any successful solve — so a task with several real captchas is not
# disabled by earlier failures. Mirrors v1's consecutive-timeout circuit breaker intent.
_MAX_SOLVE_ATTEMPTS = 3

_PAGE_UNAVAILABLE = "browser page unavailable; cannot attempt a captcha solve right now"

# observe gives no ref to an image, so an ambiguous image_selector doubles as discovery: the refusal lists the
# matches with a selector for each, and the model picks the captcha. Bounded so a page of thumbnails stays short.
_MAX_LISTED_IMAGES = 12
# A listed image is addressed by a token for the element handle the listing captured, not by its position: images
# added or removed between the listing and the read must not shift the address onto a different image.
_LISTED_IMAGE_RE = re.compile(r"^\s*image=(\d+)\s*$")
_DESCRIBE_ELEMENT_JS = """el => {
  const r = el.getBoundingClientRect();
  return {tag: el.tagName.toLowerCase(), src: el.getAttribute('src') || '', alt: el.getAttribute('alt') || '',
          w: Math.round(r.width), h: Math.round(r.height)};
}"""
_GUIDANCE = (
    "\n- If you click submit and the page does not advance (the same form is still shown), or you see a "
    "'verify you are human' / captcha challenge, call `solve_captcha` once BEFORE retrying submit, then "
    "re-observe. A captcha can sit in an iframe you cannot see directly. Do not repeatedly re-click a "
    "submit that is not advancing."
)

_IMAGE_GUIDANCE = (
    "\n- If the captcha is an image of distorted characters with a text box to type them into, read it with "
    "the solver before trying yourself: call `solve_captcha` with `image_selector` set to `img` to list the "
    "page's images, each with an `image=N` address, call it again with the captcha image's `image=N`, and type "
    "the characters it returns into that box."
)

_DESCRIPTION = (
    "Detect and solve a captcha / anti-bot challenge (Cloudflare Turnstile, reCAPTCHA, or hCaptcha) "
    "blocking the page, including one rendered inside an iframe you cannot otherwise interact with. Call "
    "this when a submit does not advance or a 'verify you are human' challenge is present, then "
    "re-observe. Returns whether a captcha was solved, was absent, or could not be solved."
)

_ABSENT = (
    "no captcha challenge was detected in the page or its visible frames; nothing was solved. "
    "Re-observe before calling solve_captcha again."
)


async def _describe_element(locator: Any) -> str:
    info = await locator.evaluate(_DESCRIBE_ELEMENT_JS)
    # The path only: a query string can carry tokens the model has no use for.
    src = str(info["src"])
    src_path = src.split(";", 1)[0].split(",", 1)[0] if src.startswith("data:") else urlsplit(src).path[:80]
    return f'<{info["tag"]}> {info["w"]}x{info["h"]}px alt="{str(info["alt"])[:40]}" src="{src_path}"'


def build_captcha_tools(
    task: Task,
    page_provider: PageProvider,
    *,
    organization_id: str | None,
) -> tuple[list[ToolSpec], str]:
    """Return (tools, system-prompt guidance) for captcha handling. Always offered: a captcha can appear
    mid-run on any page, so there is no build-time source to gate on (unlike verification codes)."""
    failed_attempts = 0
    # Image reads keep their own failure streak: nothing that stops an image read (a switched-off or down solver, a
    # timeout, an unreadable image) may spend the widget ladder's cap, and widget failures must not block image reads.
    image_failures = 0
    image_reads = 0
    # token -> (page it was listed on, element handle); a read from another page or tab must not use it.
    listed_images: dict[int, tuple[Any, Any]] = {}
    next_image_token = 1
    # Without a solver the argument is not offered at all, so the tool and prompt are exactly the widget-only ones.
    reads_images = app.AGENT_FUNCTION.supports_image_captcha_ocr()

    def _too_many(selector: str, count: int) -> ToolResult:
        return ToolResult.error(
            f"image_selector {selector!r} matches {count} elements, too many to list; call again with a narrower "
            "selector, e.g. `form img`.",
            error_class="ambiguous_selector",
        )

    async def _read_image_text(page: Any, selector: str) -> ToolResult:
        nonlocal next_image_token
        if listed := _LISTED_IMAGE_RE.match(selector):
            entry = listed_images.get(int(listed.group(1)))
            if entry is None:
                return ToolResult.error(
                    f"{selector!r} is not in the latest image listing; list the images again.",
                    error_class="ref_not_in_latest",
                )
            listed_page, handle = entry
            if listed_page is not page:
                return ToolResult.error(
                    f"{selector!r} was listed on a different page or tab; list the images on this one.",
                    error_class="stale_ref",
                )
            try:
                connected = bool(await handle.evaluate("el => el.isConnected"))
            except Exception:
                connected = False
            if not connected:
                return ToolResult.error(
                    f"{selector!r} is no longer on the page (it may have been refreshed); list the images again.",
                    error_class="stale_ref",
                )
            tag = str(await handle.evaluate("el => el.tagName.toLowerCase()"))
            if tag not in CAPTCHA_IMAGE_TAGS:
                return ToolResult.error(
                    f"{selector!r} is a <{tag}>, not an image; pick the captcha image from the listing.",
                    error_class="other",
                )
            return await _read_target(page, handle)
        if REF_SELECTOR_RE.match(selector):
            return ToolResult.error(
                "image_selector takes a CSS selector or a listed image=N; an observe ref names a control, not an "
                "image. Pass image_selector='img' to list the page's images.",
                error_class="invalid_selector",
            )
        selector = _normalize_selector(selector)
        locator = page.locator(selector)
        try:
            count = await locator.count()
        except Exception as exc:
            if invalid := _invalid_selector_result(selector, exc):
                return invalid
            raise
        if count == 0:
            return ToolResult.error(f"no element for image_selector {selector!r}", error_class="stale_selector")
        # Refused before any handle exists, so a gallery or infinite-scroll page costs one count, not a handle each.
        if count > _MAX_LISTED_IMAGES:
            return _too_many(selector, count)
        if count > 1:
            # One snapshot of every match: re-resolving the locator per index could skip or repeat an image that was
            # inserted or removed mid-listing.
            matched = await locator.element_handles()
            # Released: a superseded listing's handles, or this snapshot if the page grew past the bound after the
            # count. Handles pin browser-side objects until navigation.
            released = [handle for _, handle in listed_images.values()]
            listed_images.clear()
            if len(matched) > _MAX_LISTED_IMAGES:
                released += matched
            for stale in released:
                try:
                    await stale.dispose()
                except Exception:
                    pass
            if len(matched) > _MAX_LISTED_IMAGES:
                return _too_many(selector, len(matched))
            rows = []
            for handle in matched:
                listed_images[next_image_token] = (page, handle)
                rows.append(f"- image={next_image_token}: {await _describe_element(handle)}")
                next_image_token += 1
            count = len(matched)
            return ToolResult.error(
                f"image_selector {selector!r} matches {count} elements (a 0x0 one is not rendered). Call again "
                "with image_selector set to the captcha image's `image=N`:\n" + "\n".join(rows),
                error_class="ambiguous_selector",
            )
        target = await resolve_captcha_image(locator.first)
        if target is None:
            return ToolResult.error(
                f"image_selector {selector!r} holds no single image, so there is nothing to read; point it at the "
                "captcha image itself.",
                error_class="other",
            )
        return await _read_target(page, target)

    async def _read_target(page: Any, target: Any) -> ToolResult:
        nonlocal image_failures, image_reads
        if not await app.AGENT_FUNCTION.image_captcha_ocr_enabled(organization_id=organization_id, url=page.url):
            return ToolResult.error(
                "reading captcha images is turned off for this organization; read the image yourself or report "
                "the captcha as blocking.",
                error_class="other",
            )
        if image_reads >= MAX_IMAGE_CAPTCHA_READS:
            return ToolResult.ok(
                "the captcha image has been read the maximum number of times for this task; do not pass "
                "image_selector again -- report the captcha as blocking or try another approach.",
                ok_class="attempts_exhausted",
            )
        image_reads += 1
        # Named in the result so a read of the wrong image (the only img before a form opens is often a logo) is
        # visible to the model rather than silently typed.
        described = await _describe_element(target)
        png = await target.screenshot(timeout=settings.BROWSER_SCREENSHOT_TIMEOUT_MS)
        text = await app.AGENT_FUNCTION.read_image_captcha_text(png, organization_id=organization_id, url=page.url)
        if not text:
            image_failures += 1
            return ToolResult.error(
                "the solver could not read the captcha image; read it yourself from a screenshot, or load a new "
                "image and try once more."
            )
        image_failures = 0
        return ToolResult.ok(
            f"read {described}: {text}\nIf that is the captcha image, these are the characters to type into its text "
            "box, replacing anything there. If the page rejects them, load a new image before reading again.",
            ok_class="image_text_read",
        )

    async def _solve_image(image_selector: str) -> ToolResult:
        nonlocal image_failures
        if image_failures >= _MAX_SOLVE_ATTEMPTS:
            return ToolResult.ok(
                "reading the captcha image has already failed the maximum number of times for this task; do not "
                "pass image_selector again -- read the image yourself or report the captcha as blocking.",
                ok_class="attempts_exhausted",
            )
        try:
            page = await page_provider()
        except Exception:
            return ToolResult.error(_PAGE_UNAVAILABLE)
        if page is None:
            return ToolResult.error(_PAGE_UNAVAILABLE)
        try:
            async with asyncio.timeout(_SOLVE_CAPTCHA_CEILING_SECONDS):
                return await _read_image_text(page, image_selector)
        except TimeoutError:
            image_failures += 1
            return ToolResult.error(
                f"reading the captcha image timed out after {_SOLVE_CAPTCHA_CEILING_SECONDS}s; re-check the "
                "page, or report the captcha as blocking."
            )
        except Exception:
            image_failures += 1
            LOG.warning("task_v3 image captcha read failed", task_id=task.task_id, exc_info=True)
            return ToolResult.error("reading the captcha image failed unexpectedly; re-observe the page and continue.")

    async def _solve_captcha(args: dict[str, Any]) -> ToolResult:
        nonlocal failed_attempts
        image_selector = args.get("image_selector") if reads_images else None
        if isinstance(image_selector, str) and image_selector.strip():
            return await _solve_image(image_selector.strip())
        if failed_attempts >= _MAX_SOLVE_ATTEMPTS:
            return ToolResult.ok(
                "captcha solve has already failed the maximum number of times for this task; do not call "
                "solve_captcha again — report the captcha as blocking or try another approach.",
                ok_class="attempts_exhausted",
            )
        try:
            page = await page_provider()
        except Exception:
            return ToolResult.error(_PAGE_UNAVAILABLE)
        if page is None:
            return ToolResult.error(_PAGE_UNAVAILABLE)

        try:
            async with asyncio.timeout(_SOLVE_CAPTCHA_CEILING_SECONDS):
                solved = await solve_challenge_ladder(
                    page,
                    organization_id=organization_id,
                    workflow_run_id=task.workflow_run_id,
                    browser_session_id=task.browser_session_id,
                    probe_child_frames=True,
                )
        except CaptchaChallengeUnsolvedError:
            failed_attempts += 1
            return ToolResult.error(
                "a captcha challenge is present but could not be solved this attempt; wait briefly and "
                "re-check, or report the captcha as blocking if it persists."
            )
        except TimeoutError:
            failed_attempts += 1
            return ToolResult.error(
                f"captcha solve timed out after {_SOLVE_CAPTCHA_CEILING_SECONDS}s; the widget may need a "
                "moment or is unsolvable — re-check the page, or report the captcha as blocking."
            )
        except Exception:
            failed_attempts += 1
            LOG.warning("task_v3 solve_captcha failed", task_id=task.task_id, exc_info=True)
            return ToolResult.error("captcha solve failed unexpectedly; re-observe the page and continue.")

        if solved:
            # A real solve is progress; clear the failure streak so a later genuine captcha isn't disabled.
            failed_attempts = 0
            return ToolResult.ok(
                "captcha solved; re-observe the page and continue (e.g. retry submit).",
                ok_class="solved",
            )
        # Absent: no challenge is presented, including a widget the page loaded but never showed. No solver
        # arm ran, so this cheap structural no-op does not count toward the failure cap.
        return ToolResult.ok(
            _ABSENT
            + (
                " This detects widget captchas only: an image of distorted characters is not detected here -- "
                "pass image_selector='img' to list the page's images and read the captcha one."
                if reads_images
                else ""
            ),
            ok_class="absent",
        )

    image_parameter = {
        "image_selector": {
            "type": "string",
            "description": (
                "An image-text captcha's image: 'img' (or a narrower CSS selector) lists the main document's images "
                "as image=N, then pass the captcha's image=N. Omit for widget captchas."
            ),
        }
    }
    tool = ToolSpec(
        name="solve_captcha",
        description=_DESCRIPTION
        + (
            " For an image-text captcha, pass image_selector instead: it returns the characters for you to type."
            if reads_images
            else ""
        ),
        parameters={"type": "object", "properties": image_parameter if reads_images else {}},
        handler=_solve_captcha,
        # Recordable, not billable: the solve persists an action row + screenshot for artifact parity
        # (invaluable for debugging a false "solved"), but a captcha solve is anti-bot overhead, not a
        # user-facing navigation step, so it must not consume the action-step budget or bill — and a
        # no-op "absent"/"max attempts" ok must never meter like a real page action.
        recordable=True,
    )
    return [tool], _GUIDANCE + (_IMAGE_GUIDANCE if reads_images else "")
