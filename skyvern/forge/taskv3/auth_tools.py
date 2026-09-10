"""Auth/verification tools for the native Task V3 engine.

The tool-loop is LLM-driven, so — like the CUA engine — verification codes are resolved on demand:
the model recognizes a code field, calls ``get_verification_code``, and types the returned value.
A source that answers with a sign-in URL instead of a code is handled by ``open_verification_link``,
which opens the link backend-side; the URL is registered as model-hidden, so the model only ever
learns that the link was opened. Resolution reuses the shared ``otp_service`` waterfall (payload ->
credential TOTP -> webhook/email/DB poll), which routes cloud behavior through the ``AGENT_FUNCTION``
seam, so this module stays OSS-clean. Resolved values are registered for redaction from this task's
artifacts/logs.
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Sequence
from urllib.parse import unquote_plus, urlsplit

import structlog

from skyvern.config import settings
from skyvern.exceptions import (
    FailedToGetTOTPVerificationCode,
    NoTOTPVerificationCodeFound,
    SkyvernHTTPException,
    UnresolvableHost,
)
from skyvern.forge import app
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.schemas.tasks import Task
from skyvern.forge.sdk.schemas.totp_codes import OTPType
from skyvern.forge.taskv3.loop import ToolResult, ToolSpec
from skyvern.forge.taskv3.tools import OBSERVE_URL_MAX_CHARS, PageProvider
from skyvern.services.otp_service import OTPValue, has_otp_source, resolve_otp_value
from skyvern.utils.url_validators import strip_query_params, validate_fetch_url
from skyvern.webeye.navigation import revalidate_redirect_chain

LOG = structlog.get_logger()

_CODE_TOOL = "get_verification_code"
_LINK_TOOL = "open_verification_link"

# One tool call polls at most this long, so a call made before the page has sent the code (or while
# the source is still empty) returns and lets the model act instead of blocking the loop for the
# whole budget.
_PER_CALL_WAIT_SECONDS = 120.0
# The poll loop fetches once per 10s sleep, so a slice shorter than that would never fetch; a tail
# that small counts as spent.
_MIN_SLICE_SECONDS = 10.0
# How long a "not yet" answer keeps the give-up gate armed. Past it the run has moved on and a
# non-complete verdict is about something else; same idiom (and duration order) as the loop's
# FAILURE_EVIDENCE_WINDOW_TURNS recency, expressed in seconds because the budget is.
_NOT_YET_RECENCY_SECONDS = 180.0
# Termination is guaranteed by the budget shrinking under every productive deferral, but a slice may
# advance spend by as little as _MIN_SLICE_SECONDS, which would admit ~90 holds. 8 >= ceil(900/120),
# so this never binds on the healthy path.
_MAX_GIVEUP_DEFERRALS = 8
# Shorter values are codes/flags (lang=en, v=2), not link secrets, and a real link secret is at
# least this long; redacting the short ones would blank harmless text across the run's artifacts.
_MIN_REDACTED_QUERY_VALUE_CHARS = 16
# The charset an opaque token draws from. Excludes emails, URLs, and prose, which are readable
# values the model needs and redaction is global for the run.
_OPAQUE_QUERY_VALUE_RE = re.compile(r"[A-Za-z0-9._~+/=-]+")
# A body-less landing must not hold the tool for a full navigation timeout.
_BODY_TEXT_TIMEOUT_MS = 5000

_BUDGET_EXHAUSTED = (
    "verification code polling budget exhausted: no code arrived from the configured verification "
    "source. Do not call get_verification_code again; finish the task as failed and say the "
    "verification code never became available."
)

_COMPLETION_BLOCKED = (
    "the verification step never completed: no verification code was received and no sign-in link was "
    "successfully opened (the source errored, refused, delivered nothing usable, or the polling budget "
    "ran out). Finish with status=failed and say the verification step never completed."
)

_LINK_BUDGET_EXHAUSTED = (
    "verification polling budget exhausted: no sign-in link arrived from the configured verification "
    "source. Do not call open_verification_link again; finish the task as failed and say the sign-in "
    "link never became available."
)

_MAGIC_LINK_UNSUPPORTED = (
    "the verification source returned a sign-in link, which this engine cannot follow. Do not call "
    "get_verification_code again; finish the task as failed and say the site verifies by sign-in link."
)

_MAGIC_LINK_REDIRECT = (
    "the verification source returned a sign-in link, not a code. Call open_verification_link to open "
    "it; do not call get_verification_code again unless the page then asks for a code."
)

_CODE_INSTEAD_OF_LINK = (
    "a verification code arrived instead of a sign-in link; call get_verification_code to receive it."
)

_NO_LINK_AVAILABLE = (
    "no sign-in link is available for this task (the verification source returned nothing to open). "
    "Finish the task as failed and say the sign-in link never became available."
)

_NO_LINK_ONCE = (
    "no sign-in link available for this task right now (the verification source returned nothing to "
    "open). If the page sent one, call open_verification_link again."
)

_NO_CODE_AVAILABLE = (
    "no verification code is available for this task (the verification source returned nothing). "
    "Finish the task as failed and say the verification code never became available."
)

_NO_CODE_ONCE = (
    "no verification code available for this task right now (the verification source returned nothing). "
    "If the page asked for one, call get_verification_code again."
)

_LINK_OPENED = (
    "opened the sign-in link in this tab. Observe the page to continue: it may be signed in already, it "
    "may still ask for a verification code (then call get_verification_code), or it may have rejected "
    "the link."
)

_LINK_REFUSED = (
    "the sign-in link was refused (its destination is not allowed); finish the task as failed and say "
    "the sign-in link could not be opened."
)

_LATER_HOP_REFUSED = (
    "a later hop of the sign-in link was refused (its destination is not allowed). Observe the page; if "
    "it is blank or did not sign in, finish the task as failed and say the sign-in link could not be "
    "opened."
)

_PAGE_UNAVAILABLE = (
    "browser page unavailable; the sign-in link could not be opened. Call open_verification_link again "
    "once the page is usable; if it still cannot be opened, finish the task as failed."
)

_GUIDANCE = (
    "\n- If the page asks for a one-time / 2FA / verification code, call `get_verification_code` and "
    "`type` the returned value into the field. Never invent or guess a code."
)

_LINK_GUIDANCE = (
    "\n- If the page says it sent / emailed a sign-in LINK (not a code), call `open_verification_link`; "
    "do not ask for a code and never try to read or type the link yourself."
)


def _tool_name(expected_otp_type: OTPType) -> str:
    return _LINK_TOOL if expected_otp_type == OTPType.MAGIC_LINK else _CODE_TOOL


def _is_token_shaped(value: str) -> bool:
    return (
        len(value) >= _MIN_REDACTED_QUERY_VALUE_CHARS
        and _OPAQUE_QUERY_VALUE_RE.fullmatch(value) is not None
        # An address, a URL, a path, or a name lacks one of the two; tokens, hex, and base64 have both.
        and any(char.isalpha() for char in value)
        and any(char.isdigit() for char in value)
    )


def _register_opaque_values(context: SkyvernContext, raw: str) -> None:
    # Whether a value is opaque is decided on its decoded form, because percent-encoding hides what
    # the value actually is (``user%40example.test`` reads as a token, ``user@example.test`` does not).
    for pair in raw.split("&"):
        key, sep, value = pair.partition("=")
        if not sep:
            # A segment with no "=" (a bare token in a hash-router fragment) is the value itself.
            value = key
        decoded = unquote_plus(value)
        if not _is_token_shaped(decoded):
            continue
        # The encoded form is what a page or log echoes, the decoded one is what the browser reports.
        context.register_secret_value(decoded, hide_from_model=True)
        if value != decoded:
            context.register_secret_value(value, hide_from_model=True)


def _loggable_url(url: str | None) -> str | None:
    """The polling endpoint without its query, which carries the caller's own secret. `Task` holds this
    as an unvalidated string, so a URL the parser rejects must cost the field, never the record."""
    if not url:
        return None
    try:
        return strip_query_params(url) or None
    except Exception:
        return None


def _register_link_for_redaction(url: str) -> None:
    """Register the sign-in URL and its opaque query/fragment values as model-hidden secrets. Exact
    values only, so redaction can never match anything the URL did not literally contain."""
    context = skyvern_context.current()
    if context is None:
        return
    context.register_secret_value(url, hide_from_model=True)
    if len(url) > OBSERVE_URL_MAX_CHARS:
        context.register_secret_value(url[:OBSERVE_URL_MAX_CHARS], hide_from_model=True)
    split = urlsplit(url)
    # SPA sign-in links carry the token in the fragment (#access_token=...), not the query.
    for part in (split.query, split.fragment):
        _register_opaque_values(context, part)


async def _navigate_back(page: Any, url: str, task_id: str) -> bool:
    """Best-effort return to ``url``; the failure is logged type-only because both the validator's and
    Playwright's messages embed the sign-in URL."""
    if page.url == url:
        return True
    try:
        await page.goto(url, timeout=settings.BROWSER_LOADING_TIMEOUT_MS)
    except Exception as exc:
        LOG.warning(
            "task_v3 open_verification_link return navigation failed",
            task_id=task_id,
            error_type=type(exc).__name__,
        )
        return False
    return True


class _ResponseWatch:
    """Records whether a main-frame navigation response arrived while a sign-in link was being opened.
    `seen` is None when the page offers no event hooks, so the caller fails open."""

    def __init__(self, page: Any) -> None:
        self._page = page
        self.seen: bool | None = None

    def _on_response(self, response: Any) -> None:
        # The origin document keeps issuing its own requests while the navigation is pending, so only
        # a main-frame navigation counts; an unreadable response fails open like an unreadable jar.
        try:
            request = response.request
            if not request.is_navigation_request() or request.frame != self._page.main_frame:
                return
        except Exception:
            pass
        self.seen = True

    def start(self) -> None:
        try:
            self._page.on("response", self._on_response)
        except Exception:
            self.seen = None
            return
        self.seen = False

    def stop(self) -> None:
        try:
            self._page.remove_listener("response", self._on_response)
        except Exception:
            pass


async def _cookie_jar(page: Any) -> list[tuple[str, str, str, str]] | None:
    """The context's cookies as comparable tuples, or None when they cannot be read."""
    try:
        cookies = await page.context.cookies()
    except Exception:
        return None
    return sorted((c.get("domain", ""), c.get("path", ""), c.get("name", ""), c.get("value", "")) for c in cookies)


class VerificationFailure(StrEnum):
    """Every condition that arms the completion refusal. This closes the VOCABULARY, which is what the
    earlier hand-kept inventory could not do — it was missing six sites. Members are not one-per-site:
    BUDGET_EXHAUSTED is written both where a slice spends the last of the budget and where a later
    call finds it already spent. Closing the vocabulary is what this buys; a member-to-site bijection
    is not, and nothing here depends on one."""

    BUDGET_EXHAUSTED = "budget_exhausted"
    NO_CODE_TWICE = "no_code_twice"
    NO_LINK_TWICE = "no_link_twice"
    SOURCE_ERRORED_TWICE = "source_errored_twice"
    LOOKUP_FAILED_TWICE = "lookup_failed_twice"
    MAGIC_LINK_UNSUPPORTED = "magic_link_unsupported"
    MAGIC_LINK_UNSUPPORTED_CACHED = "magic_link_unsupported_cached"
    VALUE_NOT_A_CODE = "value_not_a_code"
    VALUE_NOT_A_LINK = "value_not_a_link"
    PAGE_UNAVAILABLE = "page_unavailable"
    LINK_REFUSED = "link_refused"
    LINK_URL_UNUSABLE = "link_url_unusable"
    LINK_OPEN_FAILED = "link_open_failed"
    LINK_REJECTED_BY_SITE = "link_rejected_by_site"


@dataclass
class VerificationState:
    """Lets the finish tool refuse a completed verdict once the source terminally failed to deliver, and
    hold a non-complete one while the run is still awaiting a code it has unspent budget for.

    The latch is armed by every terminal non-delivery answer (budget exhaustion, repeated lookup
    empty answers, refused or unopenable link), never by a retryable "not yet" or by a link failure
    that may have received a response (the URL moved, a cookie landed, or the jar could not be read).
    Only the tools count as delivery, so a code read off the page after the source failed still
    blocks; a value delivered at any point wins.

    `source_failed` is derived from the recorded reason rather than settable on its own, so arming is
    only reachable through `arm`, which narrates. The step engine narrates the same failure with the
    same fields (`skyvern/forge/agent.py`, "TOTP polling timed out — terminating task"); without this
    the v3 side latched silently and the failure could not be found in production at all."""

    # Excluded from the repr: the bound `block_finish` handed to the loop would otherwise carry
    # the task's navigation payload into any message that formats the callback.
    task: Task = field(repr=False)
    values_delivered: int = field(default=0, init=False)
    # The shared polling budget the tools draw down, lifted onto the state so the finish gate can
    # read it. `build_auth_tools` advances `polling_spent_seconds` from its poll accounting.
    budget_seconds: float = field(
        default_factory=lambda: settings.VERIFICATION_CODE_POLLING_TIMEOUT_MINS * 60.0, init=False
    )
    polling_spent_seconds: float = field(default=0.0, init=False)
    # Monotonic timestamp of the last answer that invited another call, cleared where the source
    # produces a value. Written by the two retryable answers -- "not yet", and a first empty or
    # errored lookup -- so "the run is awaiting a code right now" is a tool-call fact rather than a
    # reading of the page. An arming does NOT clear it: `source_failed` is the conjunct that stops a
    # hold once the source is judged, and it has to keep working when a later poll re-arms this latch.
    awaiting_code_since: float | None = field(default=None, init=False)
    # Which tool the latch belongs to. Both tools drain one source and share the budget, so a
    # magic-link run must not be sent back to `get_verification_code`.
    awaiting_code_tool: str = field(default=_CODE_TOOL, init=False)
    giveup_deferrals: int = field(default=0, init=False)
    # Spend at the previous honored deferral. A finish that arrives with spend unchanged means the
    # model did not re-poll, so the next give-up stands rather than ping-ponging against the gate.
    spend_at_last_giveup_deferral: float | None = field(default=None, init=False, repr=False)
    _armings: list[VerificationFailure] = field(default_factory=list, init=False, repr=False)

    @property
    def source_failed(self) -> bool:
        return bool(self._armings)

    @property
    def remaining_budget_seconds(self) -> float:
        return self.budget_seconds - self.polling_spent_seconds

    def arm(self, reason: VerificationFailure, tool: str) -> None:
        self._armings.append(reason)
        try:
            LOG.warning(
                "task_v3 verification source failed",
                task_id=self.task.task_id,
                organization_id=self.task.organization_id,
                workflow_run_id=self.task.workflow_run_id,
                tool=tool,
                # str(), not .value: the swallow below must not be able to hide a caller that passed
                # something other than a member, which would otherwise stop narration silently.
                reason=str(reason),
                # The count at THIS arming, not a verdict: a value delivered later unblocks the run,
                # and no record is written when that happens. Reads as a bound, not as an outcome.
                values_delivered=self.values_delivered,
                # Every arming reports, so this is the true running total. The record this replaces
                # was gated to one per task and could not be counted at all.
                arming_count=len(self._armings),
                # Masked to **** by `log_redaction.SENSITIVE_FIELDS` before rendering, as v1's is.
                # Carried for field parity with the step engine; the stripped URL is the triage handle.
                totp_identifier=self.task.totp_identifier,
                totp_verification_url=_loggable_url(self.task.totp_verification_url),
                # `strip_query_params` answers "" for a scheme-less or host-less string, which is
                # indistinguishable from an absent URL; this pair keeps the two apart.
                # Separates a source with no URL configured from one whose URL cannot be parsed —
                # a malformed polling URL is a plausible root cause of the source never delivering.
                totp_verification_url_configured=bool(self.task.totp_verification_url),
            )
        except Exception:
            # The latch is already set above. Narration must never cost the tool its answer: an
            # escape here would reach the model as a bare tool_error, dropping both the "finish as
            # failed" guidance and the page-state payload the link paths attach.
            pass

    async def block_completion(self) -> str | None:
        if self.source_failed and self.values_delivered == 0:
            return _COMPLETION_BLOCKED
        return None

    async def block_giveup(self, status: str) -> str | None:
        """Defer a non-complete verdict exactly while the run is actively awaiting a verification
        code it has unspent budget for. The first four conditions operationalize that one invariant;
        the last two bound the hold, so a model that ignores the deferral still ends within two
        finishes and one that polls minimally within `_MAX_GIVEUP_DEFERRALS`."""
        remaining = self.remaining_budget_seconds
        reason: str | None = None
        if self.awaiting_code_since is None:
            reason = "not_awaiting"
        elif self.source_failed:
            reason = "source_failed"
        elif time.monotonic() - self.awaiting_code_since > _NOT_YET_RECENCY_SECONDS:
            reason = "stale"
        elif remaining < _MIN_SLICE_SECONDS:
            reason = "budget_spent"
        elif self.giveup_deferrals >= _MAX_GIVEUP_DEFERRALS:
            reason = "deferral_cap"
        elif (
            self.spend_at_last_giveup_deferral is not None
            and self.polling_spent_seconds <= self.spend_at_last_giveup_deferral
        ):
            reason = "unproductive"
        if reason is not None:
            self._record_giveup(held=False, reason=reason, remaining=remaining, status=status)
            return None
        self.giveup_deferrals += 1
        self.spend_at_last_giveup_deferral = self.polling_spent_seconds
        self._record_giveup(held=True, reason="held", remaining=remaining, status=status)
        # Both tools drain one source, so the artifact has to come off the same latch the tool does:
        # telling a run that polled for a sign-in link that "the code has not arrived" contradicts the
        # page and the answer the tool just gave it.
        subject = "sign-in link" if self.awaiting_code_tool == _LINK_TOOL else "verification code"
        return (
            f"about {remaining / 60.0:.1f} minutes of {subject} polling budget remain and the "
            f"{subject} has not arrived yet. Call {self.awaiting_code_tool} again to keep waiting for "
            "it. If the page never sent one you may re-request it; otherwise do not act on the page "
            "for this. Once the budget is spent the tool will say so; finish then and the verdict "
            "will stand."
        )

    async def block_finish(self, status: str) -> str | None:
        """The one verification gate the finish tool consults, for every verdict: a completed claim is
        refused once the source terminally failed, a non-complete one is deferred while budget the
        run is still waiting on remains."""
        if status == "completed":
            return await self.block_completion()
        return await self.block_giveup(status)

    def _record_giveup(self, *, held: bool, reason: str, remaining: float, status: str) -> None:
        # The pre-registered probe: today the unspent residual at a give-up is only reconstructible by
        # differencing poll timestamps, so it cannot be counted directly. `finish_status` is carried
        # because failed and terminated are separate populations in that read and the record is the
        # only place they can be told apart.
        if not held and self.polling_spent_seconds <= 0:
            return
        try:
            LOG.info(
                "task_v3 verification give-up gate",
                task_id=self.task.task_id,
                organization_id=self.task.organization_id,
                workflow_run_id=self.task.workflow_run_id,
                held=held,
                reason=reason,
                finish_status=status,
                polling_spent_seconds=round(self.polling_spent_seconds, 1),
                remaining_seconds=round(remaining, 1),
                budget_seconds=self.budget_seconds,
                giveup_deferrals=self.giveup_deferrals,
            )
        except Exception:
            # Narration must never cost the gate its verdict, for the same reason `arm` swallows.
            pass


def build_auth_tools(
    task: Task,
    page_provider: PageProvider | None = None,
    state: VerificationState | None = None,
    allowed_credential_parameter_keys: Sequence[str] | None = None,
) -> tuple[list[ToolSpec], str]:
    """Return (tools, system-prompt guidance) for verification handling, or ([], "") when the task has
    no verification source configured (so the tools aren't offered needlessly). The link tool also needs
    a page to navigate, so a page-free run never gets it. `state`, if given, is mutated as the tools
    poll and deliver values; pass its `block_finish` to `make_finish_tool` to gate every finish verdict
    on it. A caller with no use for that gate can omit `state` entirely."""
    if state is None:
        state = VerificationState(task=task)
    offer_code_tool = has_otp_source(
        task, expected_otp_type=OTPType.TOTP, allowed_credential_parameter_keys=allowed_credential_parameter_keys
    )
    offer_link_tool = page_provider is not None and has_otp_source(
        task, expected_otp_type=OTPType.MAGIC_LINK, allowed_credential_parameter_keys=allowed_credential_parameter_keys
    )
    if not offer_code_tool and not offer_link_tool:
        return [], ""

    # The model re-calls after every empty answer, so the cumulative polling across this task's calls
    # is capped at VERIFICATION_CODE_POLLING_TIMEOUT_MINS (the step engine's single poll window) and
    # then the tools refuse with stop guidance. One budget for both tools: they drain one source; it
    # lives on `state` so the finish gate can see what is left of it. Re-derived here rather than
    # taken from the state's default because a caller may have built the state before the setting was
    # read, and the budget must be the one in force where it is actually drawn down.
    state.budget_seconds = settings.VERIFICATION_CODE_POLLING_TIMEOUT_MINS * 60.0
    # A value one tool resolved that the other tool owns (the webhook source does not filter by type).
    cached_otp_value: OTPValue | None = None
    first_poll_started_at: datetime | None = None
    # Consecutive answers that delivered nothing: an exception, a bare None, or a slice whose webhook
    # polls were failing at its timeout. One says little about the source; a second in a row is a
    # verdict on it. Shared by both tools, like the budget, because they drain one source.
    empty_answer_streak = 0

    def _budget_exhausted(expected_otp_type: OTPType) -> ToolResult:
        state.arm(VerificationFailure.BUDGET_EXHAUSTED, _tool_name(expected_otp_type))
        if expected_otp_type == OTPType.MAGIC_LINK:
            return ToolResult.error(_LINK_BUDGET_EXHAUSTED)
        return ToolResult.error(_BUDGET_EXHAUSTED)

    def _failed(
        message: str, *, reason: VerificationFailure, tool: str, data: dict[str, Any] | None = None
    ) -> ToolResult:
        """A terminal non-delivery answer: the finish tool must refuse a completed verdict from here on."""
        state.arm(reason, tool)
        return ToolResult.error(message, data=data)

    def _not_yet(expected_otp_type: OTPType, detail: str, elapsed_seconds: float) -> ToolResult:
        """A healthy source that has not delivered yet: the run is still awaiting a code, and the
        message says how much budget is left to wait with — the old one gave no indication any
        remained. Stays a ToolResult.error: the status is never serialized to the model (the loop
        sends `content` only), and it is what routes a call that blocked for up to 120s through the
        batch-poisoning check, so flipping it would let a stale queued click dispatch against a page
        that moved during the wait. `elapsed_seconds` is this slice's wait, which the caller's
        `finally` has not yet added to the running spend."""
        state.awaiting_code_since = time.monotonic()
        state.awaiting_code_tool = _tool_name(expected_otp_type)
        spent = state.polling_spent_seconds + elapsed_seconds
        remaining = max(0.0, state.budget_seconds - spent)
        LOG.warning(
            "task_v3 verification poll slice timed out",
            task_id=task.task_id,
            organization_id=task.organization_id,
            workflow_run_id=task.workflow_run_id,
            tool=_tool_name(expected_otp_type),
            polling_spent_seconds=round(spent, 1),
            remaining_seconds=round(remaining, 1),
            budget_seconds=state.budget_seconds,
        )
        tool = _tool_name(expected_otp_type)
        subject = "sign-in link" if expected_otp_type == OTPType.MAGIC_LINK else "verification code"
        if remaining < _MIN_SLICE_SECONDS:
            # Nothing left to buy: the next call refuses without polling, so inviting one would spend
            # a tool call and a turn to be told that -- and near the loop's own limits that trades a
            # truthful verification failure for a generic budget-exhausted exit. Terminal, so it arms:
            # the budget is exhausted here whether or not the model makes that pointless call, and
            # leaving the latch to be set by the call itself is what let a model that obeys the
            # instruction go on to claim the verification step completed.
            return _failed(
                f"no {subject} available yet ({detail}), and the polling budget is now spent. Do not "
                f"call {tool} again; finish the task as failed and say the {subject} never arrived.",
                reason=VerificationFailure.BUDGET_EXHAUSTED,
                tool=tool,
            )
        return ToolResult.error(
            f"no {subject} available yet ({detail}); about {remaining / 60.0:.1f} minutes of polling "
            f"budget remain. If the page has not sent one, trigger it first, then call {tool} again."
        )

    def _empty_answer(once: str, terminal: str, *, reason: VerificationFailure, tool: str) -> ToolResult:
        nonlocal empty_answer_streak
        empty_answer_streak += 1
        if empty_answer_streak > 1:
            return _failed(terminal, reason=reason, tool=tool)
        # Retryable, so it arms the give-up gate for the same reason `_not_yet` does: this answer
        # invites another call, and a run that gives up right after it has exactly as much unspent
        # budget as one that gave up after a "not yet". Arming only the "not yet" path would leave the
        # gate holding on one of the four answers that ask the model to wait.
        state.awaiting_code_since = time.monotonic()
        state.awaiting_code_tool = tool
        return ToolResult.error(once)

    async def _poll(expected_otp_type: OTPType) -> ToolResult | OTPValue:
        """One budget-accounted polling slice. A ToolResult is the model-facing answer to return as-is."""
        nonlocal first_poll_started_at, empty_answer_streak
        remaining = state.remaining_budget_seconds
        if remaining < _MIN_SLICE_SECONDS:
            return _budget_exhausted(expected_otp_type)
        if first_poll_started_at is None:
            first_poll_started_at = datetime.utcnow()
        started = time.monotonic()
        try:
            otp_value = await resolve_otp_value(
                task,
                expected_otp_type=expected_otp_type,
                max_wait_seconds=min(remaining, _PER_CALL_WAIT_SECONDS),
                poll_started_at=first_poll_started_at,
                allowed_credential_parameter_keys=allowed_credential_parameter_keys,
            )
            if otp_value is None:
                if expected_otp_type == OTPType.MAGIC_LINK:
                    return _empty_answer(
                        _NO_LINK_ONCE,
                        _NO_LINK_AVAILABLE,
                        reason=VerificationFailure.NO_LINK_TWICE,
                        tool=_tool_name(expected_otp_type),
                    )
                return _empty_answer(
                    _NO_CODE_ONCE,
                    _NO_CODE_AVAILABLE,
                    reason=VerificationFailure.NO_CODE_TWICE,
                    tool=_tool_name(expected_otp_type),
                )
            empty_answer_streak = 0
            # Cleared here, not at delivery: several paths reach a value that is never handed to the
            # model -- a link when a code was asked for, a code when a link was, a link the browser
            # could not open -- and on all of them the run has stopped waiting on the SOURCE. Holding
            # a give-up there would contradict the very answer those paths return.
            state.awaiting_code_since = None
            return otp_value
        except (NoTOTPVerificationCodeFound, FailedToGetTOTPVerificationCode) as exc:
            if state.polling_spent_seconds + (time.monotonic() - started) >= state.budget_seconds:
                return _budget_exhausted(expected_otp_type)
            detail = type(exc).__name__
            if isinstance(exc, FailedToGetTOTPVerificationCode):
                # The webhook was erroring when the slice timed out: an empty answer from a failing
                # source, not a "not yet" from a healthy one.
                if exc.reason:
                    detail = f"{detail}: {exc.reason}"
                return _empty_answer(
                    f"the verification source errored ({detail}). Call {_tool_name(expected_otp_type)} again; "
                    "do not re-trigger the page.",
                    f"the verification source kept failing ({detail}). Finish the task as failed and say the "
                    "verification step never completed.",
                    reason=VerificationFailure.SOURCE_ERRORED_TWICE,
                    tool=_tool_name(expected_otp_type),
                )
            empty_answer_streak = 0
            if exc.webhook_diagnostics:
                detail = f"{detail}: {exc.webhook_diagnostics}"
            return _not_yet(expected_otp_type, detail, time.monotonic() - started)
        except Exception as exc:
            LOG.warning(
                "task_v3 verification tool lookup failed",
                task_id=task.task_id,
                tool=_tool_name(expected_otp_type),
                exc_info=True,
            )
            message = f"verification lookup failed: {type(exc).__name__}"
            retry_hint = (
                "If the page is waiting on a sign-in link, call open_verification_link again."
                if expected_otp_type == OTPType.MAGIC_LINK
                else "If the page asked for a code, call get_verification_code again."
            )
            return _empty_answer(
                f"{message}. {retry_hint}",
                f"{message} repeatedly. Finish the task as failed and say the verification step never completed.",
                reason=VerificationFailure.LOOKUP_FAILED_TWICE,
                tool=_tool_name(expected_otp_type),
            )
        finally:
            state.polling_spent_seconds += time.monotonic() - started

    async def _get_verification_code(args: dict[str, Any]) -> ToolResult:
        nonlocal cached_otp_value
        cached = cached_otp_value
        if cached is not None:
            if cached.get_otp_type() == OTPType.MAGIC_LINK:
                if offer_link_tool:
                    return ToolResult.error(_MAGIC_LINK_REDIRECT)
                return _failed(
                    _MAGIC_LINK_UNSUPPORTED,
                    reason=VerificationFailure.MAGIC_LINK_UNSUPPORTED_CACHED,
                    tool="get_verification_code",
                )
            cached_otp_value = None
            return _deliver_code(cached.value)

        polled = await _poll(OTPType.TOTP)
        if isinstance(polled, ToolResult):
            return polled
        otp_value = polled

        if otp_value.get_otp_type() == OTPType.MAGIC_LINK:
            cached_otp_value = otp_value
            if offer_link_tool:
                _register_link_for_redaction(otp_value.value)
                LOG.info(
                    "task_v3 verification code tool redirected to open_verification_link",
                    task_id=task.task_id,
                    tool="get_verification_code",
                    otp_type=OTPType.MAGIC_LINK.value,
                )
                return ToolResult.error(_MAGIC_LINK_REDIRECT)
            LOG.warning(
                "task_v3 verification source returned a magic link",
                task_id=task.task_id,
                tool="get_verification_code",
                otp_type=OTPType.MAGIC_LINK.value,
            )
            return _failed(
                _MAGIC_LINK_UNSUPPORTED, reason=VerificationFailure.MAGIC_LINK_UNSUPPORTED, tool="get_verification_code"
            )
        if otp_value.get_otp_type() != OTPType.TOTP:
            return _failed(
                _NO_CODE_AVAILABLE, reason=VerificationFailure.VALUE_NOT_A_CODE, tool="get_verification_code"
            )
        return _deliver_code(otp_value.value)

    def _deliver_code(code: str) -> ToolResult:
        context = skyvern_context.current()
        if context is not None:
            # Redact the code from this task's artifacts/logs (task-scoped, so bare tasks are covered).
            context.register_secret_value(code)
        _record_delivery("get_verification_code")
        return ToolResult.ok(f"verification_code: {code}")

    def _record_delivery(tool: str) -> None:
        state.values_delivered += 1
        LOG.info(
            "task_v3 verification value delivered",
            task_id=task.task_id,
            organization_id=task.organization_id,
            workflow_run_id=task.workflow_run_id,
            tool=tool,
            values_delivered=state.values_delivered,
            polling_spent_seconds=round(state.polling_spent_seconds, 1),
            budget_seconds=state.budget_seconds,
        )

    async def _open_verification_link(args: dict[str, Any]) -> ToolResult:
        nonlocal cached_otp_value, first_poll_started_at
        otp_value: OTPValue | None = cached_otp_value
        if otp_value is None:
            polled = await _poll(OTPType.MAGIC_LINK)
            if isinstance(polled, ToolResult):
                return polled
            otp_value = polled

        if otp_value.get_otp_type() == OTPType.TOTP:
            cached_otp_value = otp_value
            return ToolResult.error(_CODE_INSTEAD_OF_LINK)
        if otp_value.get_otp_type() != OTPType.MAGIC_LINK:
            return _failed(
                _NO_LINK_AVAILABLE, reason=VerificationFailure.VALUE_NOT_A_LINK, tool="open_verification_link"
            )

        url = otp_value.value
        # Held only while nothing has been attempted: once a link is handed to the browser or refused,
        # it is treated as spent whatever the outcome, so a retry polls for a fresh one.
        cached_otp_value = otp_value
        _register_link_for_redaction(url)
        page = None
        if page_provider is not None:
            try:
                page = await page_provider()
            except Exception as exc:
                LOG.warning(
                    "task_v3 open_verification_link page unavailable",
                    task_id=task.task_id,
                    error_type=type(exc).__name__,
                )
                page = None
        if page is None:
            return _failed(
                _PAGE_UNAVAILABLE, reason=VerificationFailure.PAGE_UNAVAILABLE, tool="open_verification_link"
            )

        cached_otp_value = None
        # The link is spent on any attempt, so a retry must poll for a newer one. Only affects bare
        # tasks: a workflow run's email poll stays anchored at run start inside resolve_otp_value,
        # where a retry simply returns the newest link after run start.
        first_poll_started_at = datetime.utcnow()
        pre_url = page.url
        navigated = False
        cookies_before = await _cookie_jar(page)
        response_watch = _ResponseWatch(page)
        # The link comes from an email the target site controls, so it clears the same SSRF gate as the
        # step engine's goto, redirect chain included.
        try:
            validated_url = await asyncio.to_thread(validate_fetch_url, url)
            if validated_url != url:
                _register_link_for_redaction(validated_url)
            navigated = True
            response_watch.start()
            response = await page.goto(validated_url, timeout=settings.BROWSER_LOADING_TIMEOUT_MS)
            await revalidate_redirect_chain(response, validate_fetch_url, page.goto)
        except Exception as exc:
            # Both the validator's message and Playwright's embed the target URL, so neither the
            # exception text nor its traceback may be logged.
            LOG.warning(
                "task_v3 open_verification_link navigation failed",
                task_id=task.task_id,
                error_type=type(exc).__name__,
            )
            session_possible = False
            failure_data: dict[str, Any] | None = None
            if navigated:
                # A sign-in needs a response: one the watch saw, a committed navigation, or a new cookie
                # (Chromium leaves the URL untouched when a cookie landed before goto raised). With none
                # of those the failure arms the finish gate; an unreadable signal fails open to observing.
                cookies_after = await _cookie_jar(page)
                saw_response = response_watch.seen
                session_possible = (
                    saw_response is not False
                    or page.url != pre_url
                    or cookies_before is None
                    or cookies_after is None
                    or cookies_before != cookies_after
                )
                # A refused hop leaves the tab on about:blank and a failed goto can leave it anywhere.
                await _navigate_back(page, pre_url, task.task_id)
                failure_data = {"page_state_changed": True}
            # UnresolvableHost is a BlockedHost subclass but means worker-side DNS failure, not policy.
            policy_refusal = isinstance(exc, SkyvernHTTPException) and not isinstance(exc, UnresolvableHost)
            if session_possible:
                if policy_refusal:
                    return ToolResult.error(_LATER_HOP_REFUSED, data=failure_data)
                return ToolResult.error(
                    f"failed to open the sign-in link ({type(exc).__name__}). Observe the page; if it did "
                    "not sign in, finish the task as failed and say the sign-in link could not be opened.",
                    data=failure_data,
                )
            if policy_refusal:
                return _failed(
                    _LINK_REFUSED,
                    reason=VerificationFailure.LINK_REFUSED,
                    tool="open_verification_link",
                    data=failure_data,
                )
            return _failed(
                f"failed to open the sign-in link ({type(exc).__name__}); nothing was signed in. Finish the "
                "task as failed and say the sign-in link could not be opened.",
                reason=VerificationFailure.LINK_OPEN_FAILED if navigated else VerificationFailure.LINK_URL_UNUSABLE,
                tool="open_verification_link",
                data=failure_data,
            )

        finally:
            response_watch.stop()

        status = response.status if response is not None else None
        rejected = status is not None and status >= 400
        returned_to_origin = False
        if not rejected:
            visible_text = ""
            try:
                visible_text = await page.inner_text("body", timeout=_BODY_TEXT_TIMEOUT_MS)
            except Exception:
                visible_text = ""
            lowered = (visible_text or "").lower()
            if any(signal in lowered for signal in app.AGENT_FUNCTION.MAGIC_LINK_CLOSE_SIGNALS):
                returned_to_origin = await _navigate_back(page, pre_url, task.task_id)
        LOG.info(
            "task_v3 open_verification_link opened",
            task_id=task.task_id,
            tool="open_verification_link",
            status=status,
            returned_to_origin=returned_to_origin,
        )
        if rejected:
            return _failed(
                f"the site rejected the sign-in link (HTTP {status}); it may be expired or already used. "
                "Do not claim to be signed in. If the page offers to send a new link you may request one "
                "and call open_verification_link again; otherwise finish the task as failed.",
                reason=VerificationFailure.LINK_REJECTED_BY_SITE,
                tool="open_verification_link",
                data={"page_state_changed": True},
            )
        _record_delivery("open_verification_link")
        return ToolResult.ok(_LINK_OPENED, data={"page_state_changed": True})

    tools: list[ToolSpec] = []
    if offer_code_tool:
        tools.append(
            ToolSpec(
                name="get_verification_code",
                description=(
                    "Fetch the one-time / 2FA verification code for this task (from the connected email inbox, "
                    "the configured verification webhook, or the saved credential's authenticator). Call this "
                    "after the page has sent or asked for a verification / OTP / 2FA code, then type the returned "
                    "value. The call waits up to a couple of minutes for the code to arrive and the total wait per "
                    "task is limited, so do not call it before the code has been requested. Never invent a code."
                ),
                parameters={"type": "object", "properties": {}},
                handler=_get_verification_code,
                billable=False,
            )
        )
    if offer_link_tool:
        tools.append(
            ToolSpec(
                name="open_verification_link",
                description=(
                    "Fetch the sign-in / magic link the site emailed for this task and open it in the current "
                    "tab. The link is opened backend-side: you will never see the URL, so do not try to read, "
                    "type, or navigate to it yourself. Call this after the page says it sent a sign-in link, then "
                    "observe the page to see what it became. The call waits up to a couple of minutes for the "
                    "link to arrive and the total wait per task is limited, so do not call it before the site has "
                    "sent one."
                ),
                parameters={"type": "object", "properties": {}},
                handler=_open_verification_link,
                billable=False,
            )
        )
    guidance = (_GUIDANCE if offer_code_tool else "") + (_LINK_GUIDANCE if offer_link_tool else "")
    return tools, guidance
