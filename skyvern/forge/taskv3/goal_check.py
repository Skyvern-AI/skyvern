"""LLM checks at the Task V3 finish gate: the goal check, which asks whether the evidence contradicts a
single-action block's step-cap completion, and the unlisted-outcome re-ask, which asks whether a failed or terminated finish stopped
only because the site skipped a screen the completion criterion expects. The goal-check judge never sees the agent's
reason or reported output, and neither prompt renders a trail entry's `entered` values."""

from __future__ import annotations

import asyncio
import re
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Collection, Iterable, Literal, Sequence

import structlog

from skyvern.forge.prompts import prompt_engine
from skyvern.forge.sdk.api.llm.exceptions import BaseLLMError
from skyvern.forge.sdk.workflow.context_manager import RANDOM_SECRET_ID_PREFIX
from skyvern.utils.strings import escape_code_fences, neutralize_untrusted_web_page_data_sentinels

LOG = structlog.get_logger()

GOAL_CHECK_PROMPT_NAME = "taskv3-goal-check"
# The goal-check prompt as asked on a step-cap block completion.
BLOCK_COMPLETION_CHECK_PROMPT_NAME = "taskv3-block-completion-check"
GOAL_CHECK_TIMEOUT_SECONDS = 20.0
TRAIL_SIZE = 8
RESULT_MAX_CHARS = 2000
RESULT_HEAD_CHARS = 1400
RESULT_TAIL_CHARS = 600
PAGE_READ_MAX_CHARS = 16000
INSTRUCTIONS_MAX_CHARS = 4000
TRUNCATION_MARKER = "…[truncated]…"
SCREENSHOT_QUOTE_PREFIX = "SCREENSHOT:"
UNLISTED_REASK_PROMPT_NAME = "taskv3-unlisted-outcome-check"
REASON_MAX_CHARS = 2000
CONVERTED_REASON_MAX_CHARS = 300
# Shorter values ("Yes", "1", a state code) occur on any page, so they cannot show that this block's action landed.
ENTERED_VALUE_MIN_CHARS = 4
_BOOLEAN_WORDS = frozenset({"true", "false"})
# observe prints at most this many URL characters; a click or navigate result prints the whole URL.
URL_COMPARE_MAX_CHARS = 300
# The URL a tool writes at the head of its own result. Only these tools, so page text can never supply one.
_REPORTED_URL_RES = {
    "observe": re.compile(r"url=(\S+)"),
    "navigate": re.compile(r"navigated to (\S+)"),
    "click": re.compile(r"clicked [^\n]* — now at (\S+)"),
}
_QUOTE_FOLD = str.maketrans({"\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"', "`": "'", "\u00b4": "'"})

GoalJudge = Callable[[str], Awaitable[dict[str, Any] | None]]
Redactor = Callable[[str], str]
Verdict = Literal["achieved", "not_achieved", "impossible"]
NonCompletedStatus = Literal["failed", "terminated"]


@dataclass(frozen=True)
class TrailEntry:
    tool: str
    status: Literal["ok", "error"]
    content: str
    perception: bool
    page_changing: bool
    secret_entered: bool = False
    # The values this call typed or chose. Never rendered to a judge; only the re-ask's entered-value check reads them.
    entered: tuple[str, ...] = ()
    # The tab URL a page-changing call started from, when the tool reported it (click, press_key, type+Enter).
    url_before: str | None = None


class ToolTrail:
    def __init__(self, size: int = TRAIL_SIZE) -> None:
        self.entries: deque[TrailEntry] = deque(maxlen=size)
        self.page_read: TrailEntry | None = None
        self.calls_since_page_read = 0
        self.page_changes_since_page_read = 0
        # Only this loop's own calls: the typed secret can still be on screen, possibly in the run's first screenshot.
        self.secret_entered_in_loop = False
        # Unbounded, unlike `entries`: a value typed early in the run is still one this block entered.
        self.entered_values: set[str] = set()
        self.last_page_change: TrailEntry | None = None
        # The latest reported URL, unless a page-changing call that reported none came after it: that call may
        # have navigated. A call that reports the URL it started from supersedes this for its own change.
        self._url_now: str | None = None
        self._urls_seen: set[str] = set()
        self._urls_before_last_page_change: frozenset[str] = frozenset()
        self.url_at_last_page_change: str | None = None
        self.url_after_last_page_change: str | None = None

    def record(self, entry: TrailEntry) -> None:
        self.entries.append(entry)
        url = _reported_url(entry)
        if entry.page_changing:
            self.last_page_change = entry
            self._urls_before_last_page_change = frozenset(self._urls_seen)
            started_at = entry.url_before[:URL_COMPARE_MAX_CHARS] if entry.url_before else None
            self.url_at_last_page_change = started_at or self._url_now or url
            self.url_after_last_page_change = None
            self._url_now = url
        elif url is not None:
            self._url_now = url
            if self.last_page_change is not None:
                self.url_after_last_page_change = url
        if url is not None:
            self._urls_seen.add(url)
        self.secret_entered_in_loop = self.secret_entered_in_loop or entry.secret_entered
        if entry.status == "ok" and not entry.secret_entered:
            self.entered_values.update(entry.entered)
        if entry.perception and entry.status == "ok":
            self.page_read = entry
            self.calls_since_page_read = 0
            self.page_changes_since_page_read = 0
            return
        self.calls_since_page_read += 1
        if entry.page_changing:
            self.page_changes_since_page_read += 1

    @property
    def url_changed_after_last_page_change(self) -> bool:
        """Fails closed: an unknown URL on either side is unchanged. A URL seen before the last page-changing call
        does not count either, so a frame's URL next to the tab's, or a return to an earlier page, is no change."""
        at, after = self.url_at_last_page_change, self.url_after_last_page_change
        return at is not None and after is not None and after != at and after not in self._urls_before_last_page_change


def _reported_url(entry: TrailEntry) -> str | None:
    pattern = _REPORTED_URL_RES.get(entry.tool)
    if entry.status != "ok" or pattern is None:
        return None
    match = pattern.match(entry.content)
    return match.group(1)[:URL_COMPARE_MAX_CHARS] if match else None


@dataclass
class GoalVerdict:
    verdict: Verdict
    quote: str
    missing: str
    skipped_reason: str | None
    latency_s: float


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


def _render_evidence(trail: ToolTrail, redact: Redactor) -> tuple[str, str, str, str, tuple[str, ...]]:
    """The trail block, the page-read age, the page read, the evidence text a quote must come from, and each
    rendered result recorded after the last page-changing call. Every input is redacted before it is truncated,
    so a cut cannot split a secret past the redactor."""
    blocks: list[str] = []
    evidence: list[str] = []
    post_action: list[str] = []
    # A change that rolled off the bounded trail precedes every entry still on it.
    after_change = trail.last_page_change is not None and not any(
        entry is trail.last_page_change for entry in trail.entries
    )
    for i, entry in enumerate(trail.entries, start=1):
        if entry is trail.page_read:
            body = "(see MOST RECENT PAGE READ)"
        else:
            body = _untrusted(_truncate_result(redact(entry.content)))
            evidence.append(body)
            if after_change:
                post_action.append(body)
        if entry is trail.last_page_change:
            after_change = True
        blocks.append(f"[{i}] {entry.tool}\n-> status: {entry.status}\n{body}")
    if trail.page_read is None:
        page_read = "(no page read available)"
        page_read_age = "no page read available"
    else:
        page_read = _untrusted(_head(redact(trail.page_read.content), PAGE_READ_MAX_CHARS))
        evidence.append(page_read)
        if trail.last_page_change is not None and trail.page_changes_since_page_read == 0:
            post_action.append(page_read)
        page_read_age = (
            f"taken {_plural(trail.calls_since_page_read + 1, 'tool call')} before finish; "
            f"{_plural(trail.page_changes_since_page_read, 'page-changing action')} happened after it"
        )
    return "\n\n".join(blocks), page_read_age, page_read, "\n".join(evidence), tuple(post_action)


def render_goal_check_prompt(
    goal: str, trail: ToolTrail, instructions: str = "", redact: Redactor | None = None
) -> tuple[str, str]:
    """The judge prompt, and the evidence text a contradicting quote must come from."""
    if redact is None:
        redact = str
    tool_trail, page_read_age, page_read, evidence, _ = _render_evidence(trail, redact)
    prompt = prompt_engine.load_prompt(
        GOAL_CHECK_PROMPT_NAME,
        goal=redact(goal),
        instructions=_head(redact(instructions), INSTRUCTIONS_MAX_CHARS),
        n_calls=len(trail.entries),
        tool_trail=tool_trail,
        page_read_age=page_read_age,
        page_read=page_read,
    )
    return prompt, evidence


def render_unlisted_reask_prompt(
    *,
    goal: str,
    complete_criterion: str,
    terminate_criterion: str | None,
    status: NonCompletedStatus,
    reason: str,
    trail: ToolTrail,
    instructions: str = "",
    redact: Redactor | None = None,
    criteria_untrusted: bool = False,
    instructions_untrusted: bool = False,
) -> tuple[str, tuple[str, ...]]:
    """The re-ask prompt for a failed or terminated finish, and the post-action evidence its quote must come from.
    Pure: the offline gate rebuilds the production request from archived inputs through this function."""
    if redact is None:
        redact = str
    tool_trail, page_read_age, page_read, _, post_action = _render_evidence(trail, redact)
    criterion = _untrusted if criteria_untrusted else str
    instruction = _untrusted if instructions_untrusted else str
    prompt = prompt_engine.load_prompt(
        UNLISTED_REASK_PROMPT_NAME,
        # Always fenced: the goal carries no provenance here, so a page value rendered into it reads as data.
        goal=_untrusted(redact(goal)),
        complete_criterion=criterion(redact(complete_criterion)),
        terminate_criterion=criterion(redact(terminate_criterion or "")),
        criteria_untrusted=criteria_untrusted,
        instructions=instruction(_head(redact(instructions), INSTRUCTIONS_MAX_CHARS)),
        instructions_untrusted=instructions_untrusted,
        status=status,
        reason=_untrusted(_head(redact(reason), REASON_MAX_CHARS)),
        n_calls=len(trail.entries),
        tool_trail=tool_trail,
        page_read_age=page_read_age,
        page_read=page_read,
    )
    return prompt, post_action


def _grounded(quote: str, evidence: str) -> bool:
    if quote.startswith(SCREENSHOT_QUOTE_PREFIX):
        return True
    collapsed = _collapse(quote)
    return bool(collapsed) and collapsed in _collapse(evidence)


async def _ask(
    prompt: str, judge: GoalJudge, timeout_seconds: float, failure_log: str
) -> tuple[dict[str, Any] | None, str | None]:
    """The judge's response, or None and why there is none. The caller decides what a non-answer means."""
    try:
        async with asyncio.timeout(timeout_seconds):
            response = await judge(prompt)
    except TimeoutError:
        return None, "timeout"
    except BaseLLMError as exc:
        # The class only: a malformed-response error carries the judge's raw output, quote included.
        LOG.warning(failure_log, error_type=type(exc).__name__)
        return None, "judge_error"
    except Exception:
        LOG.warning(failure_log, exc_info=True)
        return None, "judge_error"
    if response is None:
        return None, "judge_declined"
    if not isinstance(response, dict):
        return None, "unparseable"
    return response, None


async def run_goal_check(
    *,
    goal: str,
    trail: ToolTrail,
    judge: GoalJudge,
    timeout_seconds: float,
    instructions: str = "",
    redact: Redactor | None = None,
    failure_log: str = "taskv3 goal check judge failed",
) -> GoalVerdict:
    """Every failure to reach a grounded verdict accepts the completion (verdict "achieved") and says why."""
    started = time.monotonic()

    def _fail_open(reason: str, quote: str = "", missing: str = "") -> GoalVerdict:
        return GoalVerdict("achieved", quote, missing, reason, time.monotonic() - started)

    try:
        prompt, evidence = render_goal_check_prompt(goal, trail, instructions, redact)
    except Exception:
        LOG.warning(failure_log, exc_info=True)
        return _fail_open("judge_error")
    response, skipped_reason = await _ask(prompt, judge, timeout_seconds, failure_log)
    if response is None:
        return _fail_open(skipped_reason or "judge_declined")
    verdict = response.get("verdict")
    if verdict not in ("achieved", "not_achieved", "impossible"):
        return _fail_open("unparseable")
    quote = str(response.get("quote") or "")
    missing = str(response.get("missing") or "")
    if verdict != "achieved" and not _grounded(quote, evidence):
        return _fail_open("ungrounded_quote", quote, missing)
    return GoalVerdict(verdict, quote, missing, None, time.monotonic() - started)


def _normalize(text: str) -> str:
    return _collapse(text.translate(_QUOTE_FOLD)).casefold()


def reask_entered_values(
    typed: Iterable[str],
    *,
    is_secret: Callable[[str], bool] = lambda value: False,
    excluded: Collection[str] = (),
) -> frozenset[str]:
    """The values this block typed, minus every secret: a placeholder, a value the run's redactor masks, and
    anything in `excluded` (the payload's one-time codes). Payload values the block never typed do not count: a
    site shows context such as a company name whether or not anything was committed."""
    return frozenset(
        value
        for value in typed
        if RANDOM_SECRET_ID_PREFIX not in value and value not in excluded and not is_secret(value)
    )


def _carries_entered_value(normalized_quote: str, entered_values: Collection[str]) -> bool:
    for value in entered_values:
        normalized = _normalize(value)
        if len(normalized) < ENTERED_VALUE_MIN_CHARS or normalized in _BOOLEAN_WORDS:
            continue
        if re.search(rf"(?<!\w){re.escape(normalized)}(?!\w)", normalized_quote):
            return True
    return False


def reask_decline_reason(
    response: dict[str, Any] | None,
    post_action_evidence: Sequence[str],
    entered_values: Collection[str],
    *,
    url_changed: bool,
    last_change_was_navigate: bool,
) -> str | None:
    """Why a re-ask response keeps a failed or terminated finish, or None when it converts it to completed. A
    conversion needs a named skipped screen, a strict False on the terminate criterion, a text quote that one
    result recorded after the block's last page-changing call shows and that carries a value the block typed, and a
    page URL that changed after that call, which was not the model's own `navigate`."""
    if not isinstance(response, dict) or response.get("verdict") != "completed":
        return "not_completed"
    if response.get("terminate_criterion_holds") is not False:
        return "terminate_criterion_holds"
    skipped_screen = response.get("skipped_screen")
    if not isinstance(skipped_screen, str) or not skipped_screen.strip():
        return "no_skipped_screen"
    quote = response.get("quote")
    if not isinstance(quote, str) or quote.startswith(SCREENSHOT_QUOTE_PREFIX):
        return "no_text_quote"
    if not post_action_evidence:
        return "no_post_action_evidence"
    normalized_quote = _normalize(quote)
    if not normalized_quote or not any(normalized_quote in _normalize(text) for text in post_action_evidence):
        return "quote_not_in_evidence"
    if not _carries_entered_value(normalized_quote, entered_values):
        return "quote_carries_no_entered_value"
    # A form that fails its submit keeps the typed values in its inputs on the same URL.
    if not url_changed:
        return "url_unchanged_after_last_action"
    # A URL the model navigated to is not the site's response to the block's action.
    if last_change_was_navigate:
        return "last_change_was_model_navigate"
    return None


@dataclass
class UnlistedReask:
    """One re-ask of a failed or terminated finish. `converts` is the judge's grounded yes; `converted` is set by
    the finish handler once no completed-side gate vetoed it."""

    original_status: NonCompletedStatus
    converts: bool
    skipped_reason: str | None
    latency_s: float
    verdict: str | None = None
    terminate_criterion_holds: bool | None = None
    quote_chars: int = 0
    reason: str = ""
    veto: str | None = None
    converted: bool = False
    # The conversion's settle window: None when it did not run; rounds are fingerprint pairs.
    settled: bool | None = None
    settle_rounds: int = 0
    llm_key: str | None = None
    reask_llm_key: str | None = None


async def run_unlisted_reask(
    *,
    goal: str,
    complete_criterion: str,
    terminate_criterion: str | None,
    status: NonCompletedStatus,
    reason: str,
    trail: ToolTrail,
    judge: GoalJudge,
    timeout_seconds: float,
    entered_values: Collection[str],
    instructions: str = "",
    redact: Redactor | None = None,
    criteria_untrusted: bool = False,
    instructions_untrusted: bool = False,
) -> UnlistedReask:
    """Fails closed: every failure to reach a grounded "completed" keeps the original verdict and says why."""
    started = time.monotonic()

    def _keep(skipped_reason: str | None, response: dict[str, Any] | None = None) -> UnlistedReask:
        holds = response.get("terminate_criterion_holds") if response else None
        return UnlistedReask(
            original_status=status,
            converts=False,
            skipped_reason=skipped_reason,
            latency_s=time.monotonic() - started,
            verdict=str(response.get("verdict")) if response and response.get("verdict") is not None else None,
            terminate_criterion_holds=holds if isinstance(holds, bool) else None,
            quote_chars=len(str(response.get("quote") or "")) if response else 0,
        )

    try:
        prompt, evidence = render_unlisted_reask_prompt(
            goal=goal,
            complete_criterion=complete_criterion,
            terminate_criterion=terminate_criterion,
            status=status,
            reason=reason,
            trail=trail,
            instructions=instructions,
            redact=redact,
            criteria_untrusted=criteria_untrusted,
            instructions_untrusted=instructions_untrusted,
        )
    except Exception:
        LOG.warning("taskv3 unlisted reask render failed", exc_info=True)
        return _keep("judge_error")
    response, skipped_reason = await _ask(prompt, judge, timeout_seconds, "taskv3 unlisted reask judge failed")
    if response is None:
        return _keep(skipped_reason)
    if response.get("verdict") not in ("completed", "not_completed"):
        return _keep("unparseable", response)
    if response.get("verdict") == "completed":
        decline_reason = reask_decline_reason(
            response,
            evidence,
            entered_values,
            url_changed=trail.url_changed_after_last_page_change,
            last_change_was_navigate=trail.last_page_change is not None and trail.last_page_change.tool == "navigate",
        )
        if decline_reason is not None:
            return _keep(decline_reason, response)
    result = _keep(None, response)
    if result.verdict == "completed":
        result.converts = True
        converted_reason = (
            f"Completed: the site skipped {_collapse(str(response['skipped_screen']))}; "
            f"{_collapse(str(response.get('evidence') or ''))}"
        )
        result.reason = (redact or str)(converted_reason)[:CONVERTED_REASON_MAX_CHARS]
    return result
