from __future__ import annotations

import asyncio
from typing import Any

import structlog
from playwright.async_api import Page

from skyvern.forge import app
from skyvern.forge.sdk.copilot.build_test_connect_failure import BuildTestConnectFailure
from skyvern.forge.sdk.copilot.runtime import (
    SENSITIVE_ORIGIN_ACTIVE_RUN_PAGE_ERROR,
    AgentContext,
    CopilotBrowserSessionUnavailable,
    _browser_session_acquisition_failure_result,
    replace_browser_session,
    sensitive_origin_page_has_active_run,
)
from skyvern.forge.sdk.workflow.models.block import CodeBlockCaptchaError, _code_block_solve_captcha_builtin
from skyvern.webeye.utils.captcha_solver import (
    MAX_IMAGE_CAPTCHA_READS,
    CaptchaChallengeUnsolvedError,
    solve_challenge_ladder,
)

from ._shared import browser_is_lost, on_working_page
from .scouting import rendered_challenge_vendor

LOG = structlog.get_logger()

SOLVE_TOOL_NAME = "solve_page_challenge"
FRESH_BROWSER_TOOL_NAME = "start_fresh_browser"

# The ladder bounds its own arms; this is the hard stop above them, matching Task V3's solve_captcha.
SOLVE_CEILING_SECONDS = 120

_FRESH_BROWSER_FACT = "a new browser session with no cookies, storage or challenge history"
_NO_BROWSER = "No browser is open in this chat yet. Navigate to the page first, then solve its challenge."
_NO_PAGE = "This chat's browser has no page open. Navigate to the page first, then solve its challenge."

_OUTCOME_TEXT = {
    "solved": (
        "The solver reported the challenge solved. That is its report, not proof the page moved on: look at "
        "the page again before continuing."
    ),
    "none": "No challenge was detected on the page or its visible frames, so nothing was solved.",
    "unsupported": (
        "A challenge frame is on screen, but the solver found no challenge it can operate, so nothing was attempted."
    ),
    "unsolved": "A challenge is present and the solver could not clear it in this browser session.",
    "unavailable": "Challenge solving is not available for this organization or page, so nothing was attempted.",
}

_IMAGE_OUTCOME_TEXT = {
    "typed": (
        "The image's text was read and typed into the answer field. It is unconfirmed until the page accepts it: "
        "submit, then look at the page again. If the page rejects it, load a new image before reading again."
    ),
    "unsolved": (
        "Nothing was typed: the image yielded no text, the image selector does not name one <img>, <svg> or "
        "<canvas> (or a container holding exactly one), or the input selector matched no fillable field."
    ),
    "unavailable": "Image OCR is not enabled for this organization, so the image was not read.",
    "read_limit_reached": (
        f"This request has already made {MAX_IMAGE_CAPTCHA_READS} image read attempts, so this one was not read."
    ),
}
_IMAGE_FORM_ARGUMENTS = (
    "Pass both image and input as non-empty selectors for an image CAPTCHA, or neither for a widget."
)


async def solve_page_challenge(
    ctx: AgentContext, *, image: str | None = None, input: str | None = None
) -> dict[str, Any]:
    if (image is None) != (input is None) or any(value is not None and not value.strip() for value in (image, input)):
        return {"ok": False, "error": _IMAGE_FORM_ARGUMENTS}
    session_id = ctx.browser_session_id
    if not session_id:
        return {"ok": False, "error": _NO_BROWSER}

    async def _solve(page: Page) -> dict[str, Any]:
        if image is not None and input is not None:
            result = await _read_image(ctx, page, session_id, image, input)
        else:
            result = await _run_ladder(ctx, page, session_id)
        if result["outcome"] == "unsolved" and browser_is_lost(page):
            raise CopilotBrowserSessionUnavailable(session_id)
        # A fresh browser changes whether a site shows a challenge, not whether its image can be read.
        if image is not None:
            return result
        if result["outcome"] == "unsolved":
            ctx.unsolved_page_challenges_by_session_id[session_id] = (
                ctx.unsolved_page_challenges_by_session_id.get(session_id, 0) + 1
            )
        if result["outcome"] in ("unsolved", "unsupported"):
            result["unsolved_challenges_in_this_browser_session"] = ctx.unsolved_page_challenges_by_session_id.get(
                session_id, 0
            )
            if session_id in ctx.browser_session_replacements.values():
                result["fresh_browser_tried_for_this_request"] = True
            else:
                result["untried_in_this_request"] = [f"{FRESH_BROWSER_TOOL_NAME}: {_FRESH_BROWSER_FACT}"]
        return result

    return await on_working_page(ctx, tool_name=SOLVE_TOOL_NAME, no_page_error=_NO_PAGE, act=_solve)


async def _run_ladder(ctx: AgentContext, page: Page, session_id: str) -> dict[str, Any]:
    if not await app.AGENT_FUNCTION.captcha_solving_available(ctx.organization_id, page.url):
        return {"ok": True, "outcome": "unavailable", "detail": _OUTCOME_TEXT["unavailable"]}
    unsolved: dict[str, Any] = {"ok": True, "outcome": "unsolved", "detail": _OUTCOME_TEXT["unsolved"]}
    try:
        async with asyncio.timeout(SOLVE_CEILING_SECONDS):
            solved = await solve_challenge_ladder(
                page,
                organization_id=ctx.organization_id,
                browser_session_id=session_id,
                probe_child_frames=True,
            )
    except CaptchaChallengeUnsolvedError:
        return unsolved
    except TimeoutError:
        return {**unsolved, "timed_out": True, "detail": f"The solver did not finish in {SOLVE_CEILING_SECONDS}s."}
    except Exception:
        LOG.warning("copilot solve_page_challenge solver failed", exc_info=True)
        return {**unsolved, "solver_failed": True, "detail": "The solver failed with an internal error."}
    if solved:
        return {"ok": True, "outcome": "solved", "detail": _OUTCOME_TEXT["solved"]}
    vendor = await rendered_challenge_vendor([frame for frame in page.frames if frame.parent_frame is not None])
    if vendor is not None:
        return {
            "ok": True,
            "outcome": "unsupported",
            "challenge_vendor": vendor,
            "detail": _OUTCOME_TEXT["unsupported"],
        }
    return {"ok": True, "outcome": "none", "detail": _OUTCOME_TEXT["none"]}


async def _read_image(ctx: AgentContext, page: Page, session_id: str, image: str, input: str) -> dict[str, Any]:
    if not await app.AGENT_FUNCTION.image_captcha_ocr_enabled(organization_id=ctx.organization_id, url=page.url):
        return {"ok": True, "outcome": "unavailable", "detail": _IMAGE_OUTCOME_TEXT["unavailable"]}
    unsolved: dict[str, Any] = {"ok": True, "outcome": "unsolved", "detail": _IMAGE_OUTCOME_TEXT["unsolved"]}
    # Each read ships a screenshot to a paid OCR vendor, capped per request like a saved block's reads.
    if ctx.image_captcha_reads >= MAX_IMAGE_CAPTCHA_READS:
        return {**unsolved, "read_limit_reached": True, "detail": _IMAGE_OUTCOME_TEXT["read_limit_reached"]}
    ctx.image_captcha_reads += 1
    try:
        async with asyncio.timeout(SOLVE_CEILING_SECONDS):
            await _code_block_solve_captcha_builtin(
                page, organization_id=ctx.organization_id, browser_session_id=session_id, image=image, input=input
            )
    except CodeBlockCaptchaError:
        return unsolved
    except TimeoutError:
        return {**unsolved, "timed_out": True, "detail": f"The image was not read in {SOLVE_CEILING_SECONDS}s."}
    except Exception:
        LOG.warning("copilot solve_page_challenge image read failed", exc_info=True)
        return {**unsolved, "solver_failed": True, "detail": "Reading the image failed with an internal error."}
    return {"ok": True, "outcome": "typed", "detail": _IMAGE_OUTCOME_TEXT["typed"]}


async def start_fresh_browser(ctx: AgentContext) -> dict[str, Any]:
    if sensitive_origin_page_has_active_run(ctx):
        return {"ok": False, "error": SENSITIVE_ORIGIN_ACTIVE_RUN_PAGE_ERROR}
    replacement = await replace_browser_session(ctx)
    if isinstance(replacement, BuildTestConnectFailure):
        failure = _browser_session_acquisition_failure_result(replacement)
        return {**failure, "previous_browser_kept": True}
    result: dict[str, Any] = {
        "ok": True,
        "browser_state_lost": (
            "This chat now uses a new browser that starts empty: the old browser's cookies, sign-ins, open tabs "
            "and page are gone."
        ),
        "old_browser_closed": replacement.old_closed,
        "pane_kept_old": replacement.pane_kept_old,
    }
    if replacement.pane_kept_old:
        result["pane_note"] = (
            "The studio browser pane keeps showing the old browser; screenshots from your browser tools show the new one."
        )
    return result
