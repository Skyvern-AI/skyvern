"""Goal check at the Task V3 finish gate (SKY-16928).

A judge model decides whether the evidence contradicts a finish(status=completed). The evidence is what the
page and the tools showed: the last tool results, the latest page read and a finish-time screenshot. The judge
never sees the agent's reason, its reported output or its tool arguments (a trail entry has no field for them);
tool results and page reads may still show field values, except for secret fields the tools withhold.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Literal

import structlog

from skyvern.forge.prompts import prompt_engine
from skyvern.forge.sdk.api.llm.exceptions import BaseLLMError
from skyvern.utils.strings import escape_code_fences, neutralize_untrusted_web_page_data_sentinels

LOG = structlog.get_logger()

GOAL_CHECK_PROMPT_NAME = "taskv3-goal-check"
GOAL_CHECK_TIMEOUT_SECONDS = 20.0
TRAIL_SIZE = 8
RESULT_MAX_CHARS = 2000
RESULT_HEAD_CHARS = 1400
RESULT_TAIL_CHARS = 600
PAGE_READ_MAX_CHARS = 16000
INSTRUCTIONS_MAX_CHARS = 4000
TRUNCATION_MARKER = "…[truncated]…"
SCREENSHOT_QUOTE_PREFIX = "SCREENSHOT:"
# Skips decided before the judge is called: no screenshot is captured for them.
PRE_JUDGE_SKIP_REASONS = frozenset({"deadline", "secret_entered", "instructions_too_long"})
MISSING_MAX_CHARS = 300
DEFAULT_MISSING = "the page or recent tool results contradict it"

GoalJudge = Callable[[str], Awaitable[dict[str, Any] | None]]
Redactor = Callable[[str], str]
Verdict = Literal["achieved", "not_achieved", "impossible"]
GoalCheckAction = Literal["accept", "hold", "fail", "terminate"]


@dataclass(frozen=True)
class TrailEntry:
    tool: str
    status: Literal["ok", "error"]
    content: str
    perception: bool
    page_changing: bool
    secret_entered: bool = False


class ToolTrail:
    def __init__(self, size: int = TRAIL_SIZE, *, secret_entered: bool = False) -> None:
        self.entries: deque[TrailEntry] = deque(maxlen=size)
        self.page_read: TrailEntry | None = None
        self.calls_since_page_read = 0
        self.page_changes_since_page_read = 0
        # Sticky: once a secret reached the page, a finish-time screenshot can show it (v3 tools apply
        # no visual secret mask), so the judge is not called for the rest of the run. Seeded when one
        # may already be on the page before this loop starts.
        self.secret_entered = secret_entered

    def record(self, entry: TrailEntry) -> None:
        self.entries.append(entry)
        self.secret_entered = self.secret_entered or entry.secret_entered
        if entry.perception and entry.status == "ok":
            self.page_read = entry
            self.calls_since_page_read = 0
            self.page_changes_since_page_read = 0
            return
        self.calls_since_page_read += 1
        if entry.page_changing:
            self.page_changes_since_page_read += 1


@dataclass
class GoalVerdict:
    verdict: Verdict
    quote: str
    missing: str
    skipped_reason: str | None
    latency_s: float
    action: GoalCheckAction | None = None
    # A hold the run had no budget left to fund, so the completion was accepted instead.
    no_headroom: bool = False
    # Shadow only: whether an immediate second check of a would-be hold also contradicted the goal,
    # i.e. whether enforce would have failed the block. None when there was no usable second verdict.
    would_fail: bool | None = None
    # The shadow re-check of a would-be hold, as opposed to a finish gate's own decision.
    recheck: bool = False

    @property
    def quote_source(self) -> str:
        if self.quote.startswith(SCREENSHOT_QUOTE_PREFIX):
            return "screenshot"
        return "text" if self.quote else "none"

    @property
    def is_contradiction(self) -> bool:
        return self.verdict in ("not_achieved", "impossible")

    @property
    def bounded_missing(self) -> str:
        return (self.missing.strip() or DEFAULT_MISSING)[:MISSING_MAX_CHARS]


def goal_check_eligible(*, page_free: bool, completion_blocker_present: bool, extraction_requested: bool) -> bool:
    """Blocks that verify their own completion (page-free validation, a download gate, the
    missing-extraction veto) are left to that verifier."""
    return not page_free and not completion_blocker_present and not extraction_requested


def _truncate_result(text: str) -> str:
    if len(text) <= RESULT_MAX_CHARS:
        return text
    return text[:RESULT_HEAD_CHARS] + TRUNCATION_MARKER + text[-RESULT_TAIL_CHARS:]


def _head(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + TRUNCATION_MARKER


def _plural(count: int, word: str) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


def _collapse(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _untrusted(text: str) -> str:
    """Page-derived text as fenced prompt data: it cannot open a code fence or close its own data block."""
    return neutralize_untrusted_web_page_data_sentinels(escape_code_fences(text))


def render_goal_check_prompt(
    goal: str, trail: ToolTrail, instructions: str = "", redact: Redactor | None = None
) -> tuple[str, str]:
    """The judge prompt, and the evidence text a contradicting quote must come from. Every input is
    redacted before it is truncated, so a cut cannot split a secret past the redactor, and the evidence is
    the redacted text the judge quotes from."""
    if redact is None:
        redact = str
    blocks: list[str] = []
    evidence: list[str] = []
    for i, entry in enumerate(trail.entries, start=1):
        if entry is trail.page_read:
            body = "(see MOST RECENT PAGE READ)"
        else:
            body = _untrusted(_truncate_result(redact(entry.content)))
            evidence.append(body)
        blocks.append(f"[{i}] {entry.tool}\n-> status: {entry.status}\n{body}")
    if trail.page_read is None:
        page_read = "(no page read available)"
        page_read_age = "no page read available"
    else:
        page_read = _untrusted(_head(redact(trail.page_read.content), PAGE_READ_MAX_CHARS))
        evidence.append(page_read)
        page_read_age = (
            f"taken {_plural(trail.calls_since_page_read + 1, 'tool call')} before finish; "
            f"{_plural(trail.page_changes_since_page_read, 'page-changing action')} happened after it"
        )
    prompt = prompt_engine.load_prompt(
        GOAL_CHECK_PROMPT_NAME,
        goal=redact(goal),
        instructions=_head(redact(instructions), INSTRUCTIONS_MAX_CHARS),
        n_calls=len(trail.entries),
        tool_trail="\n\n".join(blocks),
        page_read_age=page_read_age,
        page_read=page_read,
    )
    return prompt, "\n".join(evidence)


def _grounded(quote: str, evidence: str) -> bool:
    if quote.startswith(SCREENSHOT_QUOTE_PREFIX):
        return True
    collapsed = _collapse(quote)
    return bool(collapsed) and collapsed in _collapse(evidence)


async def run_goal_check(
    *,
    goal: str,
    trail: ToolTrail,
    judge: GoalJudge,
    timeout_seconds: float,
    instructions: str = "",
    redact: Redactor | None = None,
) -> GoalVerdict:
    """Every failure to reach a grounded verdict accepts the completion (verdict "achieved") and says why."""
    started = time.monotonic()

    def _fail_open(reason: str, quote: str = "", missing: str = "") -> GoalVerdict:
        return GoalVerdict("achieved", quote, missing, reason, time.monotonic() - started)

    try:
        prompt, evidence = render_goal_check_prompt(goal, trail, instructions, redact)
        async with asyncio.timeout(timeout_seconds):
            response = await judge(prompt)
    except TimeoutError:
        return _fail_open("timeout")
    except BaseLLMError as exc:
        # The class only: a malformed-response error carries the judge's raw output, quote included.
        LOG.warning("taskv3 goal check judge failed", error_type=type(exc).__name__)
        return _fail_open("judge_error")
    except Exception:
        LOG.warning("taskv3 goal check judge failed", exc_info=True)
        return _fail_open("judge_error")
    if response is None:
        return _fail_open("judge_declined")
    verdict = response.get("verdict") if isinstance(response, dict) else None
    if verdict not in ("achieved", "not_achieved", "impossible"):
        return _fail_open("unparseable")
    quote = str(response.get("quote") or "")
    missing = str(response.get("missing") or "")
    if verdict != "achieved" and not _grounded(quote, evidence):
        return _fail_open("ungrounded_quote", quote, missing)
    return GoalVerdict(verdict, quote, missing, None, time.monotonic() - started)
