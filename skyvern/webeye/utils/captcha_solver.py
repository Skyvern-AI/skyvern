"""Shared captcha-challenge solving ladder.

A bounded, engine-agnostic ladder that detects a visible captcha challenge and drives the platform
solver arms (DOM checkbox, reCAPTCHA anchor in-frame click, the solver extension, and the reCAPTCHA
token route). Detection keys off DOM/iframe markers, so it sees the challenge even when the widget
renders in a cross-origin iframe (the ``<iframe>`` element is a main-frame node a targeted locator
can find, though its content is not enumerable as an interactive element).

Solving routes through the ``AGENT_FUNCTION`` seam (``auto_solve_captchas`` / ``solve_recaptcha_token``),
so this module stays OSS-clean: the OSS bases return False and the cloud overrides do the real solve.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

import structlog
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Frame, Page

from skyvern.forge import app

if TYPE_CHECKING:
    from skyvern.forge.sdk.workflow.models.code_block_recorder import RecordingPage

LOG = structlog.get_logger()


class CaptchaChallengeUnsolvedError(Exception):
    """A captcha challenge was present on the page but no solver arm resolved it."""


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
# Sum of the bounded arms on a non-hCaptcha page (anchor 5 + reset 3 + extension 12 + token 90); the token
# arm is clamped to what remains so a 90s hCaptcha extension arm cannot push the bounded arms past the v3
# tool's 120s ceiling. On a page carrying both hCaptcha and reCAPTCHA markers the token arm may therefore
# get less than a full solve needs; hCaptcha is the visible gate there.
_LADDER_BUDGET_SECONDS = 110
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
        return 0


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


async def solve_challenge_ladder(
    page: Page | RecordingPage,
    *,
    organization_id: str | None = None,
    workflow_run_id: str | None = None,
    browser_session_id: str | None = None,
) -> bool:
    """Solve a detected challenge through the bounded platform ladder; True when an arm passed.

    The initial structural probes are intentionally cheap. Solver routes are never called when neither
    a challenge control nor vendor marker is present, and False distinguishes that no-op from a solve so
    callers do not re-perceive a page nothing touched. Raises CaptchaChallengeUnsolvedError when a
    challenge was present but no arm resolved it.
    """
    start = time.monotonic()
    checkbox = page.locator(_CAPTCHA_CHECKBOX_SELECTOR)
    checkbox_count = await _bounded_locator_count(checkbox)
    marker = page.locator(_CAPTCHA_MARKER_SELECTOR)
    marker_count = await _bounded_locator_count(marker)
    if checkbox_count == 0 and marker_count == 0:
        return False

    if checkbox_count == 1:
        candidate = checkbox.first
        try:
            if await candidate.is_visible() and await candidate.is_enabled():
                await candidate.click()
                await page.wait_for_timeout(100)
                if await candidate.is_checked() or await _bounded_locator_count(checkbox) == 0:
                    continuation = page.locator(_CAPTCHA_CONTINUE_SELECTOR)
                    if await _bounded_locator_count(continuation) == 1:
                        continuation_candidate = continuation.first
                        if await continuation_candidate.is_visible() and await continuation_candidate.is_enabled():
                            await continuation_candidate.click()
                            await page.wait_for_timeout(100)
                            if await _bounded_locator_count(checkbox) == 0:
                                return True
                    else:
                        # Checkbox challenges commonly complete on the checkbox
                        # interaction itself and expose no associated continuation.
                        return True
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
                await candidate.click()
                anchor_clicked = True
                LOG.info("CAPTCHA anchor frame clicked", arm="recaptcha_anchor_frame")
                await page.wait_for_timeout(_RECAPTCHA_ANCHOR_SETTLE_MS)
                if frame.is_detached() and urlparse(page.url)._replace(fragment="").geturl() != page_url_before_click:
                    LOG.info("CAPTCHA anchor frame solved after navigation", arm="recaptcha_anchor_frame")
                    return True
                token_is_populated = await _recaptcha_token_populated(token_scope)
                if (
                    await candidate.get_attribute("aria-checked") == "true"
                    and token_was_populated is False
                    and token_is_populated is True
                ):
                    LOG.info("CAPTCHA anchor frame solved", arm="recaptcha_anchor_frame")
                    return True
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

    hcaptcha_present = await _bounded_locator_count(page.locator(_HCAPTCHA_MARKER_SELECTOR)) > 0
    extension_timeout = _HCAPTCHA_ARM_TIMEOUT_SECONDS if hcaptcha_present else _EXTENSION_ARM_TIMEOUT_SECONDS
    try:
        async with asyncio.timeout(extension_timeout):
            if await app.AGENT_FUNCTION.auto_solve_captchas(page):
                return True
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
                        return True
            except Exception:
                LOG.info("CAPTCHA token arm did not solve", arm="token")

    raise CaptchaChallengeUnsolvedError("CAPTCHA could not be solved.")
