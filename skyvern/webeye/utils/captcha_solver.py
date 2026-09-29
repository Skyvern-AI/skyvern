"""Shared captcha-challenge solving ladder.

A bounded, engine-agnostic ladder that detects a visible captcha challenge and drives the platform
solver arms (DOM checkbox, reCAPTCHA anchor in-frame click, the solver extension, and the reCAPTCHA
token route). Detection keys off DOM/iframe markers, so it sees the challenge even when the widget
renders in a cross-origin iframe (the ``<iframe>`` element is a main-frame node a targeted locator can
find), and a visible ``challenges.cloudflare.com`` frame counts too, since a Turnstile mounted under a closed
shadow root is invisible to CSS locators; a caller that opts in with ``probe_child_frames`` also has visible
child frames' documents probed.

Solving routes through the ``AGENT_FUNCTION`` seam (``auto_solve_captchas`` / ``solve_recaptcha_token``),
so this module stays OSS-clean: the OSS bases return False and the cloud overrides do the real solve.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import urlparse

import structlog
from playwright.async_api import ElementHandle
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Frame, Locator, Page

from skyvern.config import settings
from skyvern.forge import app
from skyvern.webeye.utils.challenge_signature import ChallengeVendor, rendered_challenge_vendor

if TYPE_CHECKING:
    from skyvern.forge.sdk.workflow.models.code_block_recorder import RecordingPage

LOG = structlog.get_logger()


class CaptchaChallengeUnsolvedError(Exception):
    """A captcha challenge was present on the page but no solver arm resolved it."""


class ChallengeStatus(StrEnum):
    ABSENT = "absent"
    SOLVED = "solved"
    UNSOLVED = "unsolved"
    UNSUPPORTED = "unsupported"


ChallengeArm = Literal["vendor_handler", "dom_checkbox", "recaptcha_anchor_frame", "extension", "token"]
# How the ladder clicks a widget; a caller that routes its input elsewhere supplies its own.
ChallengeClick = Callable[[Locator], Awaitable[None]]
ChallengeHandleClick = Callable[[ElementHandle], Awaitable[None]]
ChallengePageState = Literal["clear", "challenged", "not_rechecked"]


@dataclass(frozen=True)
class ChallengeOutcome:
    """What a solve found and did. ``vendor`` is a ``ChallengeVendor`` value or a challenge-signature literal, never
    page text, and ``page_state`` is what a look at the page after the last arm showed."""

    status: ChallengeStatus
    vendor: str | None = None
    arm: ChallengeArm | None = None
    page_state: ChallengePageState = "not_rechecked"

    def receipt(self) -> dict[str, str | None]:
        return {"status": self.status.value, "vendor": self.vendor, "arm": self.arm, "page_state": self.page_state}


_CAPTCHA_CHECKBOX_SELECTOR = ", ".join(
    (
        'input[type="checkbox"][id*="captcha" i]',
        'input[type="checkbox"][id*="robot" i]',
        'input[type="checkbox"][name*="captcha" i]',
        '[role="checkbox"][aria-label*="robot" i]',
        '[role="checkbox"][aria-label*="verify" i]',
    )
)
_CAPTCHA_MARKER_SELECTOR = ", ".join(
    (
        ".g-recaptcha",
        ".cf-turnstile",
        ".g-recaptcha[data-sitekey]",
        ".cf-turnstile[data-sitekey]",
        ".h-captcha",
        ".h-captcha[data-sitekey]",
        'iframe[src*="recaptcha" i]',
        'iframe[src*="turnstile" i]',
        'iframe[src*="hcaptcha" i]',
        'iframe[title*="recaptcha" i]',
        'iframe[title*="challenge" i]',
    )
)
_HCAPTCHA_MARKER_SELECTOR = ", ".join((".h-captcha", ".h-captcha[data-sitekey]", 'iframe[src*="hcaptcha" i]'))
_RECAPTCHA_MARKER_SELECTOR = ", ".join(
    (
        ".g-recaptcha",
        '[data-sitekey][class*="recaptcha" i]',
        'iframe[src*="recaptcha" i]',
        'iframe[title*="recaptcha" i]',
    )
)
_RECAPTCHA_RESPONSE_SELECTOR = 'textarea[name="g-recaptcha-response"], textarea[id^="g-recaptcha-response"]'
_RECAPTCHA_ANCHOR_HOSTS = ("www.google.com", "www.recaptcha.net")
_RECAPTCHA_ANCHOR_PATHS = ("/recaptcha/api2/anchor", "/recaptcha/enterprise/anchor")
_CLOUDFLARE_CHALLENGE_HOST = "challenges.cloudflare.com"
_RECAPTCHA_ANCHOR_ARM_TIMEOUT_SECONDS = 5
# The extension arm polls a solver over the network; the scout caller has no enclosing bound.
_EXTENSION_ARM_TIMEOUT_SECONDS = 12
# hCaptcha is solved by the extension through an image challenge that takes tens of seconds, so the
# Turnstile-sized bound would cut nearly every solve.
_HCAPTCHA_ARM_TIMEOUT_SECONDS = 90
# A correct solver task returns in ~25s, so this covers one with headroom while cutting the losing task
# of the pair, which only ends at its own 180s timeout.
_TOKEN_ARM_TIMEOUT_SECONDS = 90
_WIDGET_RESET_TIMEOUT_SECONDS = 3
# Whole child-frame scan, on top of each frame's own 1s bound: a frame-heavy page must not spend the ladder's
# budget probing frames before any solver arm starts.
_CHILD_FRAME_SCAN_BUDGET_SECONDS = 3
# Sum of the bounded arms on a non-hCaptcha page (anchor 5 + reset 3 + extension 12 + token 90); the token
# arm is clamped to whatever global budget remains, and a deployment may widen the extension arm
# (resolve_captcha_solver_extension_timeout); a widened extension can never push the bounded arms past the
# v3 tool's 120s ceiling — the token arm then gets less than a full solve needs, and the wider arm gates.
_LADDER_BUDGET_SECONDS = 110
# Above the few seconds a deployment's probe may wait for a vendor to draw a challenge it just served.
_VENDOR_PROBE_TIMEOUT_SECONDS = 5
_VENDOR_SETTLE_TIMEOUT_MS = 5_000
_VENDOR_CLEAR_WAIT_SECONDS = 10
_VENDOR_CLEAR_POLL_SECONDS = 0.5
# Rendered frames of these vendors have no solver arm, so a failed ladder over one is unsupported, not unsolved.
_UNSUPPORTED_FRAME_VENDORS = frozenset({"arkoselabs", "funcaptcha"})
# Google's widget flips aria-checked after its own animation; a shorter wait reads as unsolved.
_RECAPTCHA_ANCHOR_SETTLE_MS = 2_000
_CAPTCHA_CONTINUE_SELECTOR = ", ".join(
    (
        "[data-challenge-state] button[type='submit']",
        "[data-challenge-state] button.btn-primary",
        "[data-challenge-state] [data-action='verify']",
        "[data-captcha-widget] button[type='submit']",
        "[data-captcha-widget] button.btn-primary",
        "[data-captcha-widget] [data-action='verify']",
    )
)


async def _bounded_locator_count(locator: Any) -> int:
    try:
        return await asyncio.wait_for(locator.count(), timeout=1.0)
    except Exception:
        LOG.info("CAPTCHA locator count did not complete", arm="presence", exc_info=True)
        return 0


def _intersects_viewport(box: Any, viewport: Any) -> bool:
    """Whether an element box (top-level viewport coordinates) overlaps the viewport; `is_visible()` alone
    accepts an element parked off-screen. An unknown viewport keeps the visibility verdict."""
    if box is None:
        return False
    if viewport is None:
        return True
    return (
        box["x"] < viewport["width"]
        and box["x"] + box["width"] > 0
        and box["y"] < viewport["height"]
        and box["y"] + box["height"] > 0
    )


async def _has_presented_match(locator: Any, viewport: dict[str, float] | None, *, arm: str = "presence") -> bool:
    """Whether the locator has a match laid out where a user could act on it. Every match is walked, not a
    prefix: a stale hidden widget can precede the live one, and a candidate that raises is skipped rather than
    read as evidence the page carries no challenge."""
    try:
        async with asyncio.timeout(1.0):
            for index in range(await locator.count()):
                candidate = locator.nth(index)
                try:
                    if not await candidate.is_visible():
                        continue
                    if viewport is None or _intersects_viewport(await candidate.bounding_box(), viewport):
                        return True
                except Exception:
                    LOG.info("CAPTCHA presented-match candidate could not be read", arm=arm, exc_info=True)
    except Exception:
        LOG.info("CAPTCHA presented-match probe did not complete", arm=arm, exc_info=True)
    return False


async def _frame_has_visible_match(
    frame: Any, selector: str | None, viewport: Any, *, challenge_frames: bool = False
) -> bool:
    challenge_candidate = challenge_frames and (
        _matches_cloudflare_challenge_host(frame.url) or frame.url in ("", "about:blank")
    )
    if selector is None and not challenge_candidate:
        return False
    async with asyncio.timeout(1.0):
        element = await frame.frame_element()
        if not await element.is_visible():
            return False
        # A frame that has not committed its cross-origin navigation still reports the challenge host in the
        # iframe element's `src`, so an uncommitted frame is judged on the attribute instead of `frame.url`.
        if challenge_candidate and (
            _matches_cloudflare_challenge_host(frame.url)
            or _matches_cloudflare_challenge_host(await element.get_attribute("src"))
        ):
            # Turnstile's invisible mode mounts a 1x1 frame that `is_visible()` still accepts; there is nothing
            # to solve in it, so only an on-screen frame with room for an interactive widget counts.
            box = await element.bounding_box()
            return box is not None and box["width"] > 1 and box["height"] > 1 and _intersects_viewport(box, viewport)
        if selector is None:
            return False
        return await _has_presented_match(frame.locator(selector), viewport, arm="nested_presence")


async def _visible_child_frame_match(
    page: Page | RecordingPage, selector: str | None, *, challenge_frames: bool = False
) -> bool:
    """True when a visible child frame holds a visible `selector` match; each frame's probe is bounded, and a
    frame that fails it (detached, wedged, no element) is skipped so one broken frame cannot blind the rest.
    With ``challenge_frames``, a visible challenge-host frame counts on its own, by its committed URL or, before
    that navigation commits, by its iframe ``src``."""
    # A frame probe must never be able to break the ladder: an unreadable page reads as "no nested challenge",
    # logged apart from a clean scan so a systematically blind page type stays countable.
    try:
        main_frame, frames = page.main_frame, list(page.frames)
    except Exception:
        LOG.info("CAPTCHA child-frame scan could not enumerate frames", arm="nested_presence", exc_info=True)
        return False
    try:
        viewport = page.viewport_size
    except Exception:
        viewport = None
    # A display-fitted `no_viewport` context reports None; without a size every off-screen frame would pass.
    if not viewport:
        viewport = {"width": settings.BROWSER_WIDTH, "height": settings.BROWSER_HEIGHT}
    # Frames are probed concurrently under one deadline, so the frame that matters is found regardless of
    # where it sits in frame order; a sequential walk let earlier wedged frames spend the budget first.
    probes = [
        asyncio.ensure_future(_frame_has_visible_match(f, selector, viewport, challenge_frames=challenge_frames))
        for f in frames
        if f is not main_frame
    ]
    try:
        for probe in asyncio.as_completed(probes, timeout=_CHILD_FRAME_SCAN_BUDGET_SECONDS):
            try:
                if await probe:
                    return True
            except Exception:
                continue
    except TimeoutError:
        LOG.info("CAPTCHA child-frame scan stopped at its budget", arm="nested_presence", frames=len(frames))
    finally:
        for probe in probes:
            probe.cancel()
        await asyncio.gather(*probes, return_exceptions=True)
    return False


def _matches_cloudflare_challenge_host(frame_url: str | None) -> bool:
    if not frame_url:
        return False
    try:
        hostname = (urlparse(frame_url).hostname or "").lower()
    except ValueError:
        return False
    return hostname == _CLOUDFLARE_CHALLENGE_HOST or hostname.endswith("." + _CLOUDFLARE_CHALLENGE_HOST)


def _is_trusted_recaptcha_anchor_url(frame_url: str | None) -> bool:
    if not frame_url:
        return False
    try:
        parsed = urlparse(frame_url)
    except ValueError:
        return False
    hostname = (parsed.hostname or "").lower()
    return parsed.scheme == "https" and hostname in _RECAPTCHA_ANCHOR_HOSTS and parsed.path in _RECAPTCHA_ANCHOR_PATHS


async def _recaptcha_token_populated(scope: Frame | Page | RecordingPage) -> bool | None:
    try:
        async with asyncio.timeout(1):
            fields = scope.locator(_RECAPTCHA_RESPONSE_SELECTOR)
            for index in range(await fields.count()):
                value = await fields.nth(index).input_value()
                if value and value.strip().lower() not in {"undefined", "null"}:
                    return True
    except (PlaywrightError, TimeoutError):
        return None
    return False


CAPTCHA_IMAGE_TAGS = ("img", "svg", "canvas")
# Each read ships one element's screenshot to a paid OCR vendor, so every caller caps reads per task or block.
# v1 averages about three reads per run on image-captcha forms.
MAX_IMAGE_CAPTCHA_READS = 8


async def resolve_captcha_image(target: Any) -> Any | None:
    """``target`` when it is an image, else its single descendant image; None when it holds no single image.

    ``target`` is a Playwright locator or the CodeBlock recorder's proxy of one."""
    tag = await target.evaluate("el => el.tagName.toLowerCase()", timeout=settings.BROWSER_ACTION_TIMEOUT_MS)
    if tag in CAPTCHA_IMAGE_TAGS:
        return target
    inner = target.locator(", ".join(CAPTCHA_IMAGE_TAGS))
    return inner.first if await inner.count() == 1 else None


async def solve_challenge_ladder(
    page: Page | RecordingPage,
    *,
    organization_id: str | None = None,
    workflow_run_id: str | None = None,
    browser_session_id: str | None = None,
    probe_child_frames: bool = False,
    click: ChallengeClick | None = None,
    click_handle: ChallengeHandleClick | None = None,
) -> bool:
    """``solve_challenge`` as a bool: True when solved, False when no challenge is on the page, and
    CaptchaChallengeUnsolvedError when one is present but unsolved or unsupported."""
    outcome = await solve_challenge(
        page,
        organization_id=organization_id,
        workflow_run_id=workflow_run_id,
        browser_session_id=browser_session_id,
        probe_child_frames=probe_child_frames,
        click=click,
        click_handle=click_handle,
    )
    if outcome.status in (ChallengeStatus.UNSOLVED, ChallengeStatus.UNSUPPORTED):
        raise CaptchaChallengeUnsolvedError("CAPTCHA could not be solved.")
    return outcome.status is ChallengeStatus.SOLVED


async def solve_challenge(
    page: Page | RecordingPage,
    *,
    organization_id: str | None = None,
    workflow_run_id: str | None = None,
    browser_session_id: str | None = None,
    probe_child_frames: bool = False,
    click: ChallengeClick | None = None,
    click_handle: ChallengeHandleClick | None = None,
) -> ChallengeOutcome:
    """Solve a detected challenge inside one captcha-solver lifecycle scope and report what happened. A vendor page
    goes only to its handler and counts as cleared only when a fresh probe finds neither the vendor nor a widget."""
    async with app.AGENT_FUNCTION.captcha_solver_lifecycle_scope(page):
        start = time.monotonic()
        try:
            vendor = await _detect_vendor_challenge_retrying(page)
            vendor_read = True
        except NotImplementedError:
            vendor, vendor_read = None, False
        except Exception as exc:
            LOG.info("CAPTCHA vendor probe did not complete", arm="vendor_handler", error_type=type(exc).__name__)
            vendor, vendor_read = None, False
        routed = vendor.value if vendor is not None else None
        if vendor is not None:
            vendor_state: ChallengePageState
            try:
                async with asyncio.timeout(_LADDER_BUDGET_SECONDS - (time.monotonic() - start)):
                    await app.AGENT_FUNCTION.run_vendor_challenge_handler(page, vendor)
                    vendor_state = await _await_vendor_clear(page)
            except Exception as exc:
                LOG.info(
                    "CAPTCHA vendor recheck did not complete",
                    arm="vendor_handler",
                    vendor=routed,
                    error_type=type(exc).__name__,
                )
                vendor_state = "not_rechecked"
            LOG.info("CAPTCHA vendor handler finished", arm="vendor_handler", vendor=routed, page_state=vendor_state)
            if vendor_state != "clear":
                return ChallengeOutcome(ChallengeStatus.UNSOLVED, routed, "vendor_handler", vendor_state)

        try:
            # The arms are each bounded to fit the budget from ``start``; a vendor handler that spent most of it
            # would otherwise let the remaining unclamped probes and arms run past the caller's ceiling.
            async with asyncio.timeout(_LADDER_BUDGET_SECONDS - (time.monotonic() - start)):
                arm = await _solve_challenge_ladder_impl(
                    page,
                    start=start,
                    organization_id=organization_id,
                    workflow_run_id=workflow_run_id,
                    browser_session_id=browser_session_id,
                    probe_child_frames=probe_child_frames,
                    click=click or _click_directly,
                    click_handle=click_handle or _click_directly,
                )
        except TimeoutError:
            LOG.info("CAPTCHA ladder stopped at its budget", arm="ladder")
            return ChallengeOutcome(ChallengeStatus.UNSOLVED)
        except CaptchaChallengeUnsolvedError:
            rendered = await _rendered_frame_vendor(page)
            if rendered in _UNSUPPORTED_FRAME_VENDORS:
                return ChallengeOutcome(ChallengeStatus.UNSUPPORTED, rendered, page_state="challenged")
            return ChallengeOutcome(ChallengeStatus.UNSOLVED, rendered)
        if arm is not None:
            return ChallengeOutcome(ChallengeStatus.SOLVED, arm=arm)
        rendered = await _rendered_frame_vendor(page)
        if rendered is not None:
            status = ChallengeStatus.UNSUPPORTED if rendered in _UNSUPPORTED_FRAME_VENDORS else ChallengeStatus.UNSOLVED
            return ChallengeOutcome(status, rendered, page_state="challenged")
        if vendor is not None:
            return ChallengeOutcome(ChallengeStatus.SOLVED, routed, "vendor_handler", "clear")
        return ChallengeOutcome(ChallengeStatus.ABSENT, page_state="clear" if vendor_read else "not_rechecked")


async def _detect_vendor_challenge(page: Page | RecordingPage) -> ChallengeVendor | None:
    async with asyncio.timeout(_VENDOR_PROBE_TIMEOUT_SECONDS):
        return await app.AGENT_FUNCTION.detect_vendor_challenge(page)


async def _detect_vendor_challenge_retrying(page: Page | RecordingPage) -> ChallengeVendor | None:
    try:
        return await _detect_vendor_challenge(page)
    except NotImplementedError:
        raise
    except Exception as exc:
        LOG.info("CAPTCHA vendor probe failed; retrying once", arm="vendor_handler", error_type=type(exc).__name__)
    return await _detect_vendor_challenge(page)


async def _await_vendor_clear(page: Page | RecordingPage) -> ChallengePageState:
    """Poll the vendor probe until two reads in a row are clear or the wait ends. A vendor may reload some time after
    a passed hold, and a fresh block page's challenge element is empty until its script fills it, so one clear read
    is not enough; a probe that fails mid-navigation reads as not yet."""
    state: ChallengePageState = "not_rechecked"
    clear_reads = 0
    deadline = time.monotonic() + _VENDOR_CLEAR_WAIT_SECONDS
    while True:
        with contextlib.suppress(PlaywrightError):
            await page.wait_for_load_state("load", timeout=_VENDOR_SETTLE_TIMEOUT_MS)
        try:
            if await _detect_vendor_challenge(page) is None:
                clear_reads += 1
                if clear_reads == 2:
                    return "clear"
            else:
                clear_reads = 0
                state = "challenged"
        except Exception as exc:
            clear_reads = 0
            LOG.info("CAPTCHA vendor recheck read failed", arm="vendor_handler", error_type=type(exc).__name__)
        if time.monotonic() >= deadline and clear_reads == 0:
            return state
        await asyncio.sleep(_VENDOR_CLEAR_POLL_SECONDS)


async def _rendered_frame_vendor(page: Page | RecordingPage) -> str | None:
    return await rendered_challenge_vendor([frame for frame in page.frames if frame.parent_frame is not None])


async def _click_directly(target: Locator | ElementHandle) -> None:
    await target.click()


async def _solve_challenge_ladder_impl(
    page: Page | RecordingPage,
    *,
    start: float,
    organization_id: str | None = None,
    workflow_run_id: str | None = None,
    browser_session_id: str | None = None,
    probe_child_frames: bool = False,
    click: ChallengeClick = _click_directly,
    click_handle: ChallengeHandleClick = _click_directly,
) -> ChallengeArm | None:
    """The arm that solved a detected widget, or None when the cheap probes find none and no solver is called.
    Raises CaptchaChallengeUnsolvedError when no arm, each clamped to the budget left since ``start``, clears it."""
    checkbox = page.locator(_CAPTCHA_CHECKBOX_SELECTOR)
    marker = page.locator(_CAPTCHA_MARKER_SELECTOR)
    # A challenge nested in a child frame only widens this presence check: the DOM-checkbox arm below
    # clicks through a page-level locator and can never drive a nested checkbox.
    if (
        not await _has_presented_match(checkbox, None)
        # No viewport at page level: a marker below the fold of a long form is still a challenge.
        and not await _has_presented_match(marker, None)
        and not await _visible_child_frame_match(
            page,
            f"{_CAPTCHA_CHECKBOX_SELECTOR}, {_CAPTCHA_MARKER_SELECTOR}" if probe_child_frames else None,
            challenge_frames=True,
        )
    ):
        return None

    if await _bounded_locator_count(checkbox) == 1:
        candidate = checkbox.first
        try:
            if await candidate.is_visible() and await candidate.is_enabled():
                await click(candidate)
                await page.wait_for_timeout(100)
                if await candidate.is_checked() or await _bounded_locator_count(checkbox) == 0:
                    continuation = page.locator(_CAPTCHA_CONTINUE_SELECTOR)
                    if await _bounded_locator_count(continuation) == 1:
                        continuation_candidate = continuation.first
                        if await continuation_candidate.is_visible() and await continuation_candidate.is_enabled():
                            await click(continuation_candidate)
                            await page.wait_for_timeout(100)
                            if await _bounded_locator_count(checkbox) == 0:
                                return "dom_checkbox"
                    else:
                        # Checkbox challenges commonly complete on the checkbox
                        # interaction itself and expose no associated continuation.
                        return "dom_checkbox"
        except Exception:
            LOG.info("CAPTCHA checkbox arm did not solve", arm="dom_checkbox")

    anchor_clicked = False
    anchor_left_token = False
    # A page-level locator cannot cross into reCAPTCHA's anchor iframe. Click the checkbox in-frame.
    try:
        async with asyncio.timeout(_RECAPTCHA_ANCHOR_ARM_TIMEOUT_SECONDS):
            for frame in page.frames:
                if not _is_trusted_recaptcha_anchor_url(frame.url):
                    continue
                anchor = frame.locator("#recaptcha-anchor")
                if await _bounded_locator_count(anchor) != 1:
                    continue
                candidate = await anchor.first.element_handle()
                if candidate is None or not (await candidate.is_visible()):
                    continue
                # The handle is bound to the validated document. If the frame navigates after this
                # re-check, Playwright detaches the handle instead of clicking the replacement page.
                if not _is_trusted_recaptcha_anchor_url(frame.url):
                    continue
                token_scope = frame.parent_frame or page
                token_was_populated = await _recaptcha_token_populated(token_scope)
                if await candidate.get_attribute("aria-checked") == "true":
                    break
                page_url_before_click = urlparse(page.url)._replace(fragment="").geturl()
                await click_handle(candidate)
                anchor_clicked = True
                LOG.info("CAPTCHA anchor frame clicked", arm="recaptcha_anchor_frame")
                await page.wait_for_timeout(_RECAPTCHA_ANCHOR_SETTLE_MS)
                if frame.is_detached() and urlparse(page.url)._replace(fragment="").geturl() != page_url_before_click:
                    LOG.info("CAPTCHA anchor frame solved after navigation", arm="recaptcha_anchor_frame")
                    return "recaptcha_anchor_frame"
                token_is_populated = await _recaptcha_token_populated(token_scope)
                if (
                    await candidate.get_attribute("aria-checked") == "true"
                    and token_was_populated is False
                    and token_is_populated is True
                    and await app.AGENT_FUNCTION.is_captcha_solver_completion_confirmed(page, default_result=True)
                ):
                    LOG.info("CAPTCHA anchor frame solved", arm="recaptcha_anchor_frame")
                    return "recaptcha_anchor_frame"
                # An inconclusive baseline fails the test above even when the click earned a token,
                # so read the widget rather than the verdict before deciding a reset is free.
                anchor_left_token = token_is_populated is True
                break
    except Exception:
        LOG.info("CAPTCHA anchor frame arm did not solve", arm="recaptcha_anchor_frame")

    # Clicking the anchor escalates to an image challenge whose overlay covers the page and
    # outlives the arm, so every later click lands on it instead of the form. Resetting is the only
    # thing that closes it (Escape does not), and it is skipped when the click left a token behind,
    # because a reset discards one.
    if anchor_clicked and not anchor_left_token:
        # The settle window is approximate and the reset is destructive, so look once more: a solve
        # that landed just past it would otherwise be discarded and escalated all over again.
        anchor_left_token = await _recaptcha_token_populated(page) is True
    if anchor_clicked and not anchor_left_token:
        try:
            async with asyncio.timeout(_WIDGET_RESET_TIMEOUT_SECONDS):
                await page.evaluate(
                    "() => { const g = window.grecaptcha;"
                    " const api = g && g.enterprise && g.enterprise.reset ? g.enterprise : g;"
                    " if (api && api.reset) api.reset(); }"
                )
        except Exception:
            LOG.info("CAPTCHA widget reset did not run", arm="recaptcha_anchor_frame")

    hcaptcha_present = await _bounded_locator_count(page.locator(_HCAPTCHA_MARKER_SELECTOR)) > 0 or (
        probe_child_frames and await _visible_child_frame_match(page, _HCAPTCHA_MARKER_SELECTOR)
    )
    default_extension_timeout = _HCAPTCHA_ARM_TIMEOUT_SECONDS if hcaptcha_present else _EXTENSION_ARM_TIMEOUT_SECONDS
    # Resolved inside the already-entered lifecycle scope so a deployment can widen this arm for a solver
    # it armed on scope entry, instead of cutting a slow legitimate solve at the generic bound.
    extension_timeout = min(
        app.AGENT_FUNCTION.resolve_captcha_solver_extension_timeout(page, default_extension_timeout),
        _LADDER_BUDGET_SECONDS - (time.monotonic() - start),
    )
    if extension_timeout <= 0:
        LOG.info("CAPTCHA extension arm skipped: ladder budget exhausted", arm="extension")
    else:
        try:
            async with asyncio.timeout(extension_timeout):
                if await app.AGENT_FUNCTION.auto_solve_captchas(page):
                    return "extension"
        except Exception:
            LOG.info("CAPTCHA extension arm did not solve", arm="extension")

    recaptcha = page.locator(_RECAPTCHA_MARKER_SELECTOR)
    if await _bounded_locator_count(recaptcha) > 0:
        remaining = _LADDER_BUDGET_SECONDS - (time.monotonic() - start)
        token_timeout = min(_TOKEN_ARM_TIMEOUT_SECONDS, remaining)
        if token_timeout <= 0:
            LOG.info("CAPTCHA token arm skipped: ladder budget exhausted", arm="token")
        else:
            try:
                async with asyncio.timeout(token_timeout):
                    if await app.AGENT_FUNCTION.solve_recaptcha_token(
                        page,
                        organization_id=organization_id,
                        workflow_run_id=workflow_run_id,
                        browser_session_id=browser_session_id,
                    ):
                        return "token"
            except Exception:
                LOG.info("CAPTCHA token arm did not solve", arm="token")

    raise CaptchaChallengeUnsolvedError("CAPTCHA could not be solved.")
