from __future__ import annotations

import asyncio
from typing import Any

import structlog
from playwright.async_api import Page

from skyvern.forge import app
from skyvern.forge.sdk.copilot.build_test_connect_failure import BuildTestConnectFailure
from skyvern.forge.sdk.copilot.mcp_adapter import _browser_session_error_disposition, _browser_session_loss_result
from skyvern.forge.sdk.copilot.runtime import (
    SENSITIVE_ORIGIN_ACTIVE_RUN_PAGE_ERROR,
    SENSITIVE_ORIGIN_PAGE_ERROR,
    AgentContext,
    BrowserProbeOutcome,
    CopilotBrowserGenerationRetired,
    CopilotBrowserSessionUnavailable,
    _browser_context_attachability,
    _browser_session_acquisition_failure_result,
    browser_evidence_commit_lock,
    browser_page_custody_lock,
    live_working_page,
    mcp_browser_context,
    replace_browser_session,
    sensitive_origin_page_has_active_run,
    sensitive_origin_page_is_tainted,
)
from skyvern.webeye.utils.captcha_solver import CaptchaChallengeUnsolvedError, solve_challenge_ladder

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


async def solve_page_challenge(ctx: AgentContext) -> dict[str, Any]:
    session_id = ctx.browser_session_id
    if not session_id:
        return {"ok": False, "error": _NO_BROWSER}
    async with browser_page_custody_lock(ctx), browser_evidence_commit_lock(ctx):
        if sensitive_origin_page_has_active_run(ctx):
            return {"ok": False, "error": SENSITIVE_ORIGIN_ACTIVE_RUN_PAGE_ERROR}
        if sensitive_origin_page_is_tainted(ctx):
            return {"ok": False, "error": SENSITIVE_ORIGIN_PAGE_ERROR}
        try:
            async with mcp_browser_context(ctx):
                page = await live_working_page(ctx)
                if page is None:
                    return {"ok": False, "error": _NO_PAGE}
                result = await _run_ladder(ctx, page, session_id)
                if result["outcome"] == "unsolved" and (
                    page.is_closed()
                    or _browser_context_attachability(page.context) is BrowserProbeOutcome.positively_unreachable
                ):
                    raise CopilotBrowserSessionUnavailable(session_id)
        except (CopilotBrowserGenerationRetired, CopilotBrowserSessionUnavailable) as exc:
            disposition = await _browser_session_error_disposition(
                ctx, exc, tool_name=SOLVE_TOOL_NAME, call_path="model"
            )
            return _browser_session_loss_result(
                {}, disposition=disposition, deadline_expired=ctx.browser_session_continuity_deadline_expired
            )
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
