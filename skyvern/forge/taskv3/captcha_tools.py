"""Captcha-solving tool for the native Task V3 engine.

The tool-loop is main-frame only: a captcha rendered in a cross-origin iframe (Cloudflare Turnstile,
reCAPTCHA) is not enumerated by ``observe``, so the model cannot perceive the gate and re-clicks a dead
submit. This tool gives the loop an explicit ``solve_captcha`` action that drives the shared solver
ladder (which detects the challenge via DOM/iframe markers and operates it), then returns an honest
tri-state so the model stops blind-hammering. Solving routes through the ``AGENT_FUNCTION`` seam, so
this module stays OSS-clean.

Solved and "no challenge detected" are both ``ok`` -- absent is not an error and must not force a
retry -- so every ``ok`` branch here names an ``ok_class``. That is what keeps the tri-state legible
on the tool-call record; ``tool_status`` alone cannot tell a working detector from a blind one.
"""

from __future__ import annotations

import asyncio
from typing import Any

import structlog

from skyvern.forge.sdk.schemas.tasks import Task
from skyvern.forge.taskv3.loop import ToolResult, ToolSpec
from skyvern.forge.taskv3.tools import PageProvider
from skyvern.webeye.utils.captcha_solver import CaptchaChallengeUnsolvedError, solve_challenge_ladder

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

_GUIDANCE = (
    "\n- If you click submit and the page does not advance (the same form is still shown), or you see a "
    "'verify you are human' / captcha challenge, call `solve_captcha` once BEFORE retrying submit, then "
    "re-observe. A captcha can sit in an iframe you cannot see directly. Do not repeatedly re-click a "
    "submit that is not advancing."
)


def build_captcha_tools(
    task: Task,
    page_provider: PageProvider,
    *,
    organization_id: str | None,
) -> tuple[list[ToolSpec], str]:
    """Return (tools, system-prompt guidance) for captcha handling. Always offered: a captcha can appear
    mid-run on any page, so there is no build-time source to gate on (unlike verification codes)."""
    failed_attempts = 0

    async def _solve_captcha(args: dict[str, Any]) -> ToolResult:
        nonlocal failed_attempts
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
            "no captcha challenge was detected in the page or its visible frames; nothing was solved. "
            "Re-observe before calling solve_captcha again.",
            ok_class="absent",
        )

    tool = ToolSpec(
        name="solve_captcha",
        description=(
            "Detect and solve a captcha / anti-bot challenge (Cloudflare Turnstile, reCAPTCHA, or hCaptcha) "
            "blocking the page, including one rendered inside an iframe you cannot otherwise interact with. Call "
            "this when a submit does not advance or a 'verify you are human' challenge is present, then "
            "re-observe. Returns whether a captcha was solved, was absent, or could not be solved."
        ),
        parameters={"type": "object", "properties": {}},
        handler=_solve_captcha,
        # Recordable, not billable: the solve persists an action row + screenshot for artifact parity
        # (invaluable for debugging a false "solved"), but a captcha solve is anti-bot overhead, not a
        # user-facing navigation step, so it must not consume the action-step budget or bill — and a
        # no-op "absent"/"max attempts" ok must never meter like a real page action.
        recordable=True,
    )
    return [tool], _GUIDANCE
