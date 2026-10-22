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
    NoTOTPSecretFound,
    NoTOTPVerificationCodeFound,
    SkyvernHTTPException,
    UnresolvableHost,
)
from skyvern.forge import app
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.schemas.tasks import Task
from skyvern.forge.sdk.schemas.totp_codes import OTPType
from skyvern.forge.sdk.services.credentials import wait_for_fresh_totp_window
from skyvern.forge.taskv3.loop import CONVERSION_VERIFICATION_STATUS, ToolRefusal, ToolResult, ToolSpec
from skyvern.forge.taskv3.opaque_refs import _MIN_REDACTED_QUERY_VALUE_CHARS, _OPAQUE_QUERY_VALUE_RE
from skyvern.forge.taskv3.tools import (
    NO_ONE_TIME_CODE_SOURCE,
    OBSERVE_URL_MAX_CHARS,
    PageProvider,
    TotpCodeAuthorizer,
)
from skyvern.services.otp_service import (
    MagicLinkSurfacing,
    OTPValue,
    extract_totp_from_navigation_inputs,
    has_otp_source,
    is_usable_credential_totp_placeholder,
    resolve_otp_value,
)
from skyvern.utils.url_validators import strip_query_params, validate_fetch_url
from skyvern.webeye.actions.handler import (
    _register_runtime_otp_value_best_effort,
    generate_totp_value_from_secret,
    get_totp_secret_with_task,
)
from skyvern.webeye.navigation import record_task_nav_error_code, revalidate_redirect_chain

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
# How long a "no sign-in link yet" answer keeps the give-up hold armed. Past it the run has moved on and a
# non-complete verdict is about something else.
_NOT_YET_RECENCY_SECONDS = 180.0
# Termination is guaranteed by the budget shrinking under every productive deferral, but a slice may advance
# spend by as little as _MIN_SLICE_SECONDS. 8 >= ceil(900/120), so this never binds on the healthy path.
_MAX_GIVEUP_DEFERRALS = 8
# A body-less landing must not hold the tool for a full navigation timeout.
_BODY_TEXT_TIMEOUT_MS = 5000

_CODE_BUDGET_SPENT = "the verification code polling time is spent; no code arrived from the configured source."

_CREDENTIAL_HAS_NO_CODE_SECRET = (
    "this credential has no usable one-time-code secret, so no code can be generated for it; "
    "get_verification_code fetches codes from this task's code source."
)

_CODE_CREDENTIAL_NOT_IN_USE = (
    "more than one saved credential in this task has a one-time-code field and none of them is in use yet; "
    "a code is generated for the credential in use."
)

_COMPLETION_BLOCKED = (
    "the sign-in link step never completed: no sign-in link was opened and no verification code was "
    "received (the source errored, refused, delivered nothing usable, or the polling budget ran out)."
)

_LINK_BUDGET_EXHAUSTED = (
    "verification polling budget exhausted: no sign-in link arrived from the configured verification "
    "source. Do not call open_verification_link again; finish the task as failed and say the sign-in "
    "link never became available."
)

_SIGN_IN_LINK_INSTEAD_OF_CODE = "it sent a sign-in link, which this run cannot open"

_MAGIC_LINK_REDIRECT = (
    "the verification source returned a sign-in link, not a code; open_verification_link opens it in this tab."
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


def _budget_spent(remaining_seconds: float) -> bool:
    return remaining_seconds <= 0 or remaining_seconds < _MIN_SLICE_SECONDS


def _time_left(remaining_seconds: float) -> str:
    if _budget_spent(remaining_seconds):
        return "no polling time remains"
    return f"about {remaining_seconds / 60.0:.1f} minutes of polling time remain"


def _code_not_yet(remaining_seconds: float) -> str:
    if _budget_spent(remaining_seconds):
        return _CODE_BUDGET_SPENT
    return f"no verification code has arrived yet; {_time_left(remaining_seconds)}."


def _code_source_error(detail: str, remaining_seconds: float) -> str:
    return f"the verification code source returned an error ({detail}); {_time_left(remaining_seconds)}."


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
    """Every condition `arm` narrates. Members are not one-per-site: BUDGET_EXHAUSTED is written both
    where a slice spends the last of the budget and where a later call finds it already spent."""

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
    """Verification facts for one task: the polling budget, deliveries, and the last status the code tool
    reported. It gives the model no orders about codes; it refuses a completion on a run whose sign-in link
    never arrived or opened, and holds a give-up while the run still waits on a sign-in link."""

    # Excluded from the repr: the bound `block_finish` handed to the loop would otherwise carry
    # the task's navigation payload into any message that formats the callback.
    task: Task = field(repr=False)
    # A credential-generated code with less than this left in its TOTP step waits for the next step.
    totp_min_remaining_seconds: int = 0
    values_delivered: int = field(default=0, init=False)
    # The last code-tool answer that carried no code, or the type tool's refusal on a task with no code source. A
    # verification tool call, a delivery or a page action clears it; a model give-up reports it as its reason.
    last_status: str | None = field(default=None, init=False)
    # The block's credential scope, set by `build_auth_tools`; the type tool's refusal reads it.
    allowed_credential_parameter_keys: Sequence[str] | None = field(default=None, init=False)
    # Set when the latest verification answer asked the model to keep waiting for a sign-in link.
    link_wait_since: float | None = field(default=None, init=False)
    giveup_deferrals: int = field(default=0, init=False)
    # A finish that arrives with spend unchanged since the last hold means the model did not re-poll, so it stands.
    spend_at_last_giveup_deferral: float | None = field(default=None, init=False, repr=False)
    # The shared polling budget the tools draw down. `build_auth_tools` advances `polling_spent_seconds`
    # from its poll accounting.
    budget_seconds: float = field(
        default_factory=lambda: settings.VERIFICATION_CODE_POLLING_TIMEOUT_MINS * 60.0, init=False
    )
    polling_spent_seconds: float = field(default=0.0, init=False)
    # Set by the link tool's retryable answers and cleared when the source produces a value, so a re-ask
    # conversion can tell a sign-in link screen that may still be waiting from one the site skipped.
    awaiting_link_since: float | None = field(default=None, init=False)
    _armings: list[tuple[VerificationFailure, str]] = field(default_factory=list, init=False, repr=False)

    @property
    def link_failed(self) -> bool:
        return any(tool == _LINK_TOOL for _, tool in self._armings)

    @property
    def remaining_budget_seconds(self) -> float:
        return self.budget_seconds - self.polling_spent_seconds

    def arm(self, reason: VerificationFailure, tool: str) -> None:
        self._armings.append((reason, tool))
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
                arming_count=len(self._armings),
                # Masked to **** by `log_redaction.SENSITIVE_FIELDS` before rendering, as v1's is.
                # Carried for field parity with the step engine; the stripped URL is the triage handle.
                totp_identifier=self.task.totp_identifier,
                totp_verification_url=_loggable_url(self.task.totp_verification_url),
                # `strip_query_params` answers "" for a scheme-less or host-less string, which is
                # indistinguishable from an absent URL; this keeps a malformed polling URL visible.
                totp_verification_url_configured=bool(self.task.totp_verification_url),
            )
        except Exception:
            # Narration must never cost the tool its answer: an escape here would reach the model as a
            # bare tool_error, dropping the status and the page-state payload the link paths attach.
            pass

    def page_engaged(self) -> None:
        self.last_status = None

    def record_delivery(self, tool: str) -> None:
        self.values_delivered += 1
        self.last_status = None
        LOG.info(
            "task_v3 verification value delivered",
            task_id=self.task.task_id,
            organization_id=self.task.organization_id,
            workflow_run_id=self.task.workflow_run_id,
            tool=tool,
            values_delivered=self.values_delivered,
            polling_spent_seconds=round(self.polling_spent_seconds, 1),
            budget_seconds=self.budget_seconds,
        )

    async def resolve_totp_placeholder(self, placeholder: str, authorize: TotpCodeAuthorizer | None = None) -> str:
        """Turn a credential's TOTP placeholder, which resolves to a vault marker rather than a code, into
        the code its secret generates now. Like the step engine, any run credential's placeholder qualifies:
        the block's credential scope bounds the code tools, not typing. Refuses when the credential holds no
        usable TOTP secret, with the no-source status only when the task has no other code source. A code
        `authorize` refuses is neither registered nor counted as delivered."""
        code: str | None = None
        workflow_run_id = self.task.workflow_run_id
        usable = False
        owner_key: str | None = None
        if workflow_run_id and app.WORKFLOW_CONTEXT_MANAGER.has_workflow_run_context(workflow_run_id):
            workflow_run_context = app.WORKFLOW_CONTEXT_MANAGER.get_workflow_run_context(workflow_run_id)
            usable = is_usable_credential_totp_placeholder(
                workflow_run_context,
                placeholder,
                active_credential_parameter_key=None,
                allowed_credential_parameter_keys=None,
            )
            owner_key = workflow_run_context.find_credential_parameter_key_for_secret(placeholder)
        if usable and owner_key is not None:
            try:
                totp_secret = get_totp_secret_with_task(self.task, placeholder)
                await wait_for_fresh_totp_window(totp_secret, min_remaining_seconds=self.totp_min_remaining_seconds)
                code = generate_totp_value_from_secret(totp_secret)
            except NoTOTPSecretFound:
                code = None
        if code is None:
            LOG.warning(
                "task_v3 credential one-time code unavailable",
                task_id=self.task.task_id,
                organization_id=self.task.organization_id,
                workflow_run_id=workflow_run_id,
                credential_usable=usable,
            )
            allowed = self.allowed_credential_parameter_keys
            if _code_source_can_produce(self.task, allowed) or _code_credential_not_in_use(self.task, allowed):
                raise ToolRefusal(_CREDENTIAL_HAS_NO_CODE_SECRET, error_class="no_one_time_code_source")
            self.last_status = NO_ONE_TIME_CODE_SOURCE
            raise ToolRefusal(NO_ONE_TIME_CODE_SOURCE, error_class="no_one_time_code_source")
        if authorize is not None and owner_key is not None:
            await authorize(code, owner_key)
        context = skyvern_context.current()
        if context is not None:
            context.register_secret_value(code)
        # The workflow-run registry is what redacts the code from this run's other blocks and artifacts.
        _register_runtime_otp_value_best_effort(workflow_run_id, code)
        self.record_delivery("type")
        return code

    async def block_finish(self, status: str) -> str | None:
        """Refuses a completed claim once the sign-in link source terminally failed, and a re-ask conversion
        also while a sign-in link is still awaited; a value delivered at any point lifts both. Any other
        verdict is a give-up, held while a sign-in link is still awaited."""
        if status not in ("completed", CONVERSION_VERIFICATION_STATUS):
            return self._hold_link_giveup(status)
        if self.values_delivered:
            return None
        if self.link_failed or (status == CONVERSION_VERIFICATION_STATUS and self.awaiting_link_since is not None):
            return _COMPLETION_BLOCKED
        return None

    def _hold_link_giveup(self, status: str) -> str | None:
        if self.link_wait_since is None:
            return None
        remaining = self.remaining_budget_seconds
        reason: str | None = None
        if self._armings:
            reason = "source_failed"
        elif time.monotonic() - self.link_wait_since > _NOT_YET_RECENCY_SECONDS:
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
        if reason is None:
            self.giveup_deferrals += 1
            self.spend_at_last_giveup_deferral = self.polling_spent_seconds
        try:
            LOG.info(
                "task_v3 verification give-up gate",
                task_id=self.task.task_id,
                organization_id=self.task.organization_id,
                workflow_run_id=self.task.workflow_run_id,
                tool=_LINK_TOOL,
                held=reason is None,
                reason=reason or "held",
                finish_status=status,
                polling_spent_seconds=round(self.polling_spent_seconds, 1),
                remaining_seconds=round(remaining, 1),
                budget_seconds=self.budget_seconds,
                giveup_deferrals=self.giveup_deferrals,
                values_delivered=self.values_delivered,
            )
        except Exception:
            # Narration must never cost the hold its verdict, for the same reason `arm` swallows.
            pass
        if reason is not None:
            return None
        return (
            f"about {remaining / 60.0:.1f} minutes of sign-in link polling budget remain and the sign-in link has "
            f"not arrived yet. Call {_LINK_TOOL} again to keep waiting for it. If the page never sent one you may "
            "re-request it; otherwise do not act on the page for this. Once the budget is spent the tool will say "
            "so; finish then and the verdict will stand."
        )


def _offered_code_source_can_produce(task: Task, allowed_credential_parameter_keys: Sequence[str] | None) -> bool:
    """Whether the code source `has_otp_source` found could yield a code: a payload code, a polling
    source, or a credential TOTP backed by a parseable secret. `has_otp_source` checks only the
    credential's shape, which a missing or invalid seed still passes."""
    payload_otp_value = extract_totp_from_navigation_inputs(task.navigation_payload)
    if payload_otp_value is not None and payload_otp_value.get_otp_type() == OTPType.TOTP:
        return True
    if (task.totp_verification_url or task.totp_identifier) and task.organization_id:
        return True
    workflow_run_id = task.workflow_run_id
    if not workflow_run_id or not app.WORKFLOW_CONTEXT_MANAGER.has_workflow_run_context(workflow_run_id):
        return False
    workflow_run_context = app.WORKFLOW_CONTEXT_MANAGER.get_workflow_run_context(workflow_run_id)
    context = skyvern_context.current()
    active_key = context.active_credential_parameter_key if context else None
    if active_key is not None and allowed_credential_parameter_keys is not None:
        active_key = active_key if active_key in allowed_credential_parameter_keys else None
    # has_otp_source already narrowed the candidates to exactly one credential (the active one, or the
    # sole TOTP-bearing one), so any usable candidate here is that credential.
    return any(
        isinstance(value, dict)
        and is_usable_credential_totp_placeholder(
            workflow_run_context,
            value.get("totp"),
            active_credential_parameter_key=active_key,
            allowed_credential_parameter_keys=allowed_credential_parameter_keys,
        )
        for value in workflow_run_context.values.values()
    )


def _code_source_can_produce(task: Task, allowed_credential_parameter_keys: Sequence[str] | None) -> bool:
    return has_otp_source(
        task, expected_otp_type=OTPType.TOTP, allowed_credential_parameter_keys=allowed_credential_parameter_keys
    ) and _offered_code_source_can_produce(task, allowed_credential_parameter_keys)


def _code_credential_not_in_use(task: Task, allowed_credential_parameter_keys: Sequence[str] | None) -> bool:
    """Several credentials in scope carry a TOTP field and none is active, so `has_otp_source` picks none of them,
    yet at least one could produce a code once it is the credential in use."""
    workflow_run_id = task.workflow_run_id
    if not workflow_run_id or not app.WORKFLOW_CONTEXT_MANAGER.has_workflow_run_context(workflow_run_id):
        return False
    context = skyvern_context.current()
    active_key = context.active_credential_parameter_key if context else None
    if active_key is not None and (
        allowed_credential_parameter_keys is None or active_key in allowed_credential_parameter_keys
    ):
        return False
    workflow_run_context = app.WORKFLOW_CONTEXT_MANAGER.get_workflow_run_context(workflow_run_id)
    placeholders = [
        value["totp"]
        for key, value in workflow_run_context.values.items()
        if isinstance(value, dict)
        and isinstance(value.get("totp"), str)
        and workflow_run_context.is_registered_credential_parameter_key(key)
        and (allowed_credential_parameter_keys is None or key in allowed_credential_parameter_keys)
    ]
    return len(placeholders) > 1 and any(
        is_usable_credential_totp_placeholder(
            workflow_run_context,
            placeholder,
            active_credential_parameter_key=None,
            allowed_credential_parameter_keys=allowed_credential_parameter_keys,
        )
        for placeholder in placeholders
    )


def build_auth_tools(
    task: Task,
    page_provider: PageProvider | None = None,
    state: VerificationState | None = None,
    allowed_credential_parameter_keys: Sequence[str] | None = None,
) -> tuple[list[ToolSpec], str]:
    """Return (tools, system-prompt guidance) for verification handling. The code tool is always offered and
    answers with a status when no code source can produce a code; the link tool needs a link source and a
    page to navigate. `state`, if given, is mutated as the tools poll and deliver values; pass its
    `block_finish` to `make_finish_tool`. A caller with no use for that gate can omit `state` entirely."""
    if state is None:
        state = VerificationState(task=task)
    state.allowed_credential_parameter_keys = allowed_credential_parameter_keys
    offer_link_tool = page_provider is not None and has_otp_source(
        task, expected_otp_type=OTPType.MAGIC_LINK, allowed_credential_parameter_keys=allowed_credential_parameter_keys
    )

    # The model re-calls after every empty answer, so the cumulative polling across this task's calls
    # is capped at VERIFICATION_CODE_POLLING_TIMEOUT_MINS (the step engine's single poll window). One
    # budget for both tools: they drain one source. Re-derived here rather than taken from the state's
    # default because a caller may have built the state before the setting was read.
    # Scope is this task, so each block of a workflow run draws its own budget. Deliberate, not
    # emergent: v1's poll sites start a fresh full window on every invocation (`poll_otp_value`
    # callers pass no `max_wait_seconds`), so a per-run cap here would be stricter than v1.
    state.budget_seconds = settings.VERIFICATION_CODE_POLLING_TIMEOUT_MINS * 60.0
    # A value one tool resolved that the other tool owns (the webhook source does not filter by type).
    cached_otp_value: OTPValue | None = None
    # Links opened or passed over, so a code poll never hands one of them, or an older link, back.
    spent_links: set[str] = set()
    first_poll_started_at: datetime | None = None
    # Consecutive answers that delivered nothing: an exception, a bare None, or a slice whose webhook
    # polls were failing at its timeout. One says little about the source; a second in a row is a
    # verdict on it. Shared by both tools, like the budget, because they drain one source.
    empty_answer_streak = 0

    def _code_answer(text: str, *, arm: VerificationFailure | None = None) -> ToolResult:
        """A code-tool status that carries no code. Stays a ToolResult.error: the loop sends the model
        `content` only, and the error status routes a call that blocked for up to 120s through the
        batch-poisoning check, so a stale queued click cannot dispatch against a page that moved."""
        if arm is not None:
            state.arm(arm, _CODE_TOOL)
        state.last_status = text
        return ToolResult.error(text)

    def _budget_exhausted(expected_otp_type: OTPType) -> ToolResult:
        if expected_otp_type == OTPType.TOTP:
            return _code_answer(_CODE_BUDGET_SPENT, arm=VerificationFailure.BUDGET_EXHAUSTED)
        state.arm(VerificationFailure.BUDGET_EXHAUSTED, _LINK_TOOL)
        return ToolResult.error(_LINK_BUDGET_EXHAUSTED)

    def _failed(
        message: str, *, reason: VerificationFailure, tool: str, data: dict[str, Any] | None = None
    ) -> ToolResult:
        """A terminal link-tool answer: the finish tool refuses a completed verdict from here on."""
        state.arm(reason, tool)
        return ToolResult.error(message, data=data)

    def _not_yet(expected_otp_type: OTPType, detail: str, elapsed_seconds: float, *, errored: bool) -> ToolResult:
        """A slice that timed out with nothing to hand over. `elapsed_seconds` is this slice's wait, which
        the caller's `finally` has not yet added to the running spend."""
        spent = state.polling_spent_seconds + elapsed_seconds
        remaining = max(0.0, state.budget_seconds - spent)
        tool = _tool_name(expected_otp_type)
        LOG.warning(
            "task_v3 verification poll slice timed out",
            task_id=task.task_id,
            organization_id=task.organization_id,
            workflow_run_id=task.workflow_run_id,
            tool=tool,
            polling_spent_seconds=round(spent, 1),
            remaining_seconds=round(remaining, 1),
            budget_seconds=state.budget_seconds,
        )
        spent_out = _budget_spent(remaining)
        if expected_otp_type == OTPType.TOTP:
            text = _code_source_error(detail, remaining) if errored else _code_not_yet(remaining)
            return _code_answer(text, arm=VerificationFailure.BUDGET_EXHAUSTED if spent_out else None)
        state.awaiting_link_since = state.link_wait_since = time.monotonic()
        if spent_out:
            return _failed(
                f"no sign-in link available yet ({detail}), and the polling budget is now spent. Do not "
                f"call {tool} again; finish the task as failed and say the sign-in link never arrived.",
                reason=VerificationFailure.BUDGET_EXHAUSTED,
                tool=tool,
            )
        return ToolResult.error(
            f"no sign-in link available yet ({detail}); about {remaining / 60.0:.1f} minutes of polling "
            f"budget remain. If the page has not sent one, trigger it first, then call {tool} again."
        )

    def _code_empty(text: str, *, reason: VerificationFailure) -> ToolResult:
        nonlocal empty_answer_streak
        empty_answer_streak += 1
        return _code_answer(text, arm=reason if empty_answer_streak > 1 else None)

    def _empty_answer(once: str, terminal: str, *, reason: VerificationFailure) -> ToolResult:
        nonlocal empty_answer_streak
        empty_answer_streak += 1
        if empty_answer_streak > 1:
            return _failed(terminal, reason=reason, tool=_LINK_TOOL)
        state.awaiting_link_since = state.link_wait_since = time.monotonic()
        return ToolResult.error(once)

    async def _poll(expected_otp_type: OTPType) -> ToolResult | OTPValue:
        """One budget-accounted polling slice. A ToolResult is the model-facing answer to return as-is."""
        nonlocal first_poll_started_at, empty_answer_streak
        remaining = state.remaining_budget_seconds
        if remaining < _MIN_SLICE_SECONDS:
            return _budget_exhausted(expected_otp_type)
        if first_poll_started_at is None:
            first_poll_started_at = datetime.utcnow()
        is_code = expected_otp_type == OTPType.TOTP
        # A code poll may hand back a sign-in link stored for this run since this task began, but only when
        # the link tool can open it.
        surface_magic_link = (
            MagicLinkSurfacing(created_after=task.created_at, spent_values=frozenset(spent_links))
            if is_code and offer_link_tool
            else None
        )
        started = time.monotonic()

        def _charged() -> float:
            # An instant answer still costs a slice, so repeated calls reach the spent status in a bounded number.
            return max(time.monotonic() - started, _MIN_SLICE_SECONDS)

        try:
            otp_value = await resolve_otp_value(
                task,
                expected_otp_type=expected_otp_type,
                max_wait_seconds=min(remaining, _PER_CALL_WAIT_SECONDS),
                poll_started_at=first_poll_started_at,
                allowed_credential_parameter_keys=allowed_credential_parameter_keys,
                min_remaining_seconds=state.totp_min_remaining_seconds,
                surface_magic_link=surface_magic_link,
            )
            if otp_value is None:
                if is_code:
                    return _code_empty(
                        _code_not_yet(state.remaining_budget_seconds - _charged()),
                        reason=VerificationFailure.NO_CODE_TWICE,
                    )
                return _empty_answer(_NO_LINK_ONCE, _NO_LINK_AVAILABLE, reason=VerificationFailure.NO_LINK_TWICE)
            empty_answer_streak = 0
            # Cleared here, not at delivery: several paths reach a value that is never handed to the
            # model -- a link when a code was asked for, a code when a link was, a link the browser
            # could not open -- and on all of them the run has stopped waiting on the SOURCE.
            state.awaiting_link_since = None
            return otp_value
        except (NoTOTPVerificationCodeFound, FailedToGetTOTPVerificationCode) as exc:
            elapsed = _charged()
            if not is_code and state.polling_spent_seconds + elapsed >= state.budget_seconds:
                return _budget_exhausted(expected_otp_type)
            detail = type(exc).__name__
            if isinstance(exc, FailedToGetTOTPVerificationCode):
                # The webhook was erroring when the slice timed out: an empty answer from a failing
                # source, not a "not yet" from a healthy one.
                if exc.reason:
                    detail = f"{detail}: {exc.reason}"
                if is_code:
                    return _code_empty(
                        _code_source_error(detail, state.remaining_budget_seconds - elapsed),
                        reason=VerificationFailure.SOURCE_ERRORED_TWICE,
                    )
                return _empty_answer(
                    f"the verification source errored ({detail}). Call {_LINK_TOOL} again; do not re-trigger the page.",
                    f"the verification source kept failing ({detail}). Finish the task as failed and say the "
                    "verification step never completed.",
                    reason=VerificationFailure.SOURCE_ERRORED_TWICE,
                )
            empty_answer_streak = 0
            if exc.webhook_diagnostics:
                detail = f"{detail}: {exc.webhook_diagnostics}"
            return _not_yet(expected_otp_type, detail, elapsed, errored=bool(exc.webhook_diagnostics))
        except Exception as exc:
            LOG.warning(
                "task_v3 verification tool lookup failed",
                task_id=task.task_id,
                tool=_tool_name(expected_otp_type),
                exc_info=True,
            )
            message = f"verification lookup failed: {type(exc).__name__}"
            if is_code:
                return _code_empty(
                    _code_source_error(message, state.remaining_budget_seconds - _charged()),
                    reason=VerificationFailure.LOOKUP_FAILED_TWICE,
                )
            return _empty_answer(
                f"{message}. If the page is waiting on a sign-in link, call open_verification_link again.",
                f"{message} repeatedly. Finish the task as failed and say the verification step never completed.",
                reason=VerificationFailure.LOOKUP_FAILED_TWICE,
            )
        finally:
            state.polling_spent_seconds += _charged()

    def _new_verification_call() -> None:
        state.last_status = None
        state.link_wait_since = None

    async def _get_verification_code(args: dict[str, Any]) -> ToolResult:
        nonlocal cached_otp_value
        _new_verification_call()
        # Per call, not at build time: the active credential can change between calls.
        if not _code_source_can_produce(task, allowed_credential_parameter_keys):
            if _code_credential_not_in_use(task, allowed_credential_parameter_keys):
                return _code_answer(_CODE_CREDENTIAL_NOT_IN_USE)
            return _code_answer(NO_ONE_TIME_CODE_SOURCE)
        cached = cached_otp_value
        if cached is not None:
            if cached.get_otp_type() == OTPType.MAGIC_LINK:
                if not offer_link_tool:
                    return _code_answer(
                        _code_source_error(_SIGN_IN_LINK_INSTEAD_OF_CODE, state.remaining_budget_seconds),
                        arm=VerificationFailure.MAGIC_LINK_UNSUPPORTED_CACHED,
                    )
                # Asked for a code again instead of opening the link: the page may want a code after all, so
                # poll for one rather than repeat the redirect, which would starve a code that arrives later.
                spent_links.add(cached.value)
                cached_otp_value = None
            else:
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
            return _code_answer(
                _code_source_error(_SIGN_IN_LINK_INSTEAD_OF_CODE, state.remaining_budget_seconds),
                arm=VerificationFailure.MAGIC_LINK_UNSUPPORTED,
            )
        if otp_value.get_otp_type() != OTPType.TOTP:
            return _code_answer(
                _code_source_error("it returned a value that is not a code", state.remaining_budget_seconds),
                arm=VerificationFailure.VALUE_NOT_A_CODE,
            )
        return _deliver_code(otp_value.value)

    def _deliver_code(code: str) -> ToolResult:
        context = skyvern_context.current()
        if context is not None:
            # Redact the code from this task's artifacts/logs (task-scoped, so bare tasks are covered).
            context.register_secret_value(code)
        state.record_delivery("get_verification_code")
        return ToolResult.ok(f"verification_code: {code}")

    async def _open_verification_link(args: dict[str, Any]) -> ToolResult:
        nonlocal cached_otp_value, first_poll_started_at
        _new_verification_call()
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
        spent_links.add(url)
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
            if navigated:
                await record_task_nav_error_code(task.task_id, exc, validated_url)
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
        state.record_delivery("open_verification_link")
        return ToolResult.ok(_LINK_OPENED, data={"page_state_changed": True})

    tools: list[ToolSpec] = [
        ToolSpec(
            name="get_verification_code",
            description=(
                "Fetch the one-time / 2FA verification code for this task (from the connected email inbox, "
                "the configured verification webhook, or the saved credential's authenticator). A call waits up "
                "to a couple of minutes for the code and the total wait per task is limited. It returns the "
                "code, or says that no code has arrived yet (with the polling time left), that no code source "
                "is configured, that the polling time is spent, or that the code source returned an error. "
                "Never invent or guess a code."
            ),
            parameters={"type": "object", "properties": {}},
            handler=_get_verification_code,
            billable=False,
        )
    ]
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
                # It calls page.goto() on the live tab, the same class of thing `navigate` does.
                # Not billable (it must not consume the action-step budget) and not recordable (no
                # action row), but a run that opened a link HAS engaged with the page.
                engages_page=True,
            )
        )
    return tools, _GUIDANCE + (_LINK_GUIDANCE if offer_link_tool else "")
