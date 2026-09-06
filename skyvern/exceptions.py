import difflib
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from http import HTTPStatus
from importlib.util import find_spec
from typing import NoReturn

# Representative modules that indicate the local extra is installed enough for
# embedded/browser import graphs. Keep this list intentionally small, but include
# the heavy modules users commonly have partially installed.
_LOCAL_EXTRA_SENTINELS = (
    "fastapi",
    "fuzzysearch",
    "jinja2",
    "libcst",
    "litellm",
    "openai",
    "playwright",
    "sqlalchemy",
    "starlette",
    "starlette_context",
)

# Server installs are a superset of local installs, but embedded local mode still
# imports some Forge/API modules such as skyvern.forge.api_app. Keep the default
# server sentinels local-compatible so those imports continue to work in
# skyvern[local]. Full server entrypoints pass server-only module_names such as
# "uvicorn" when they need to fail for local-only installs.
_SERVER_EXTRA_SENTINELS = tuple(dict.fromkeys((*_LOCAL_EXTRA_SENTINELS, "alembic", "anthropic")))

_EXTRA_SUPPORT_LABELS = {
    "local": "local embedded/browser support",
    "server": "server support",
}


def _missing_extra_dependency(module_name: str, sentinels: tuple[str, ...]) -> bool:
    root_module = module_name.split(".", maxsplit=1)[0]
    if root_module in sentinels:
        return find_spec(root_module) is None
    if root_module == "skyvern":
        return False
    # Unknown missing modules may be genuine dependency bugs, so only known
    # extra sentinels are rewritten to the install hint.
    return False


class SkyvernException(Exception):
    def __init__(self, message: str | None = None):
        self.message = message
        self._user_facing_message: str | None = None
        super().__init__(message)

    @property
    def user_facing_type_name(self) -> str:
        # Class name safe to render in a user-facing message. Subclasses whose real class
        # name carries sensitive info (e.g. a remote-browser vendor identity) override this
        # so the concrete name stays in logs/monitoring but never reaches end users.
        return type(self).__name__

    @property
    def message_is_user_facing(self) -> bool:
        return False

    @property
    def user_facing_message(self) -> str:
        return self._user_facing_message or self.message or str(self)


class SkyvernPageAnalysisTimeout(SkyvernException):
    pass


class SkyvernExtraNotInstalled(ImportError):
    def __init__(self, feature: str, extra: str = "server"):
        self.feature = feature
        self.extra = extra
        support_label = _EXTRA_SUPPORT_LABELS.get(extra, f"{extra} support")
        super().__init__(f'{feature} requires {support_label}. Install it with `pip install "skyvern[{extra}]"`.')


def _raise_extra_required(
    feature: str,
    exc: ImportError,
    *,
    extra: str,
    sentinels: tuple[str, ...],
) -> NoReturn:
    if isinstance(exc, SkyvernExtraNotInstalled):
        raise SkyvernExtraNotInstalled(feature, extra=extra) from exc
    if isinstance(exc, ModuleNotFoundError) and exc.name is not None and _missing_extra_dependency(exc.name, sentinels):
        raise SkyvernExtraNotInstalled(feature, extra=extra) from exc
    raise exc


def raise_local_extra_required(feature: str, exc: ImportError) -> NoReturn:
    _raise_extra_required(feature, exc, extra="local", sentinels=_LOCAL_EXTRA_SENTINELS)


def raise_server_extra_required(feature: str, exc: ImportError) -> NoReturn:
    _raise_extra_required(feature, exc, extra="server", sentinels=_SERVER_EXTRA_SENTINELS)


def _require_extra_modules(feature: str, extra: str, sentinels: tuple[str, ...], module_names: tuple[str, ...]) -> None:
    required_modules = dict.fromkeys((*sentinels, *module_names))
    for module_name in required_modules:
        if find_spec(module_name) is None:
            missing = ModuleNotFoundError(f"No module named '{module_name}'", name=module_name)
            raise SkyvernExtraNotInstalled(feature, extra=extra) from missing


def require_local_extra_modules(feature: str, module_names: tuple[str, ...] = ()) -> None:
    # Embedded/local browser APIs require the local extra, not a partial Playwright-only install.
    _require_extra_modules(feature, "local", _LOCAL_EXTRA_SENTINELS, module_names)


def require_server_extra_modules(feature: str, module_names: tuple[str, ...] = ()) -> None:
    # With no module_names, this only guards against base installs. Pass server-only
    # modules when a path must discriminate between local and full server extras.
    _require_extra_modules(feature, "server", _SERVER_EXTRA_SENTINELS, module_names)


class SkyvernClientException(SkyvernException):
    def __init__(self, message: str | None = None, status_code: int | None = None):
        self.status_code = status_code
        super().__init__(message)


class SkyvernHTTPException(SkyvernException):
    def __init__(self, message: str | None = None, status_code: int | HTTPStatus = HTTPStatus.BAD_REQUEST):
        self.status_code = int(status_code)
        super().__init__(message)


_BROWSER_CONNECTION_GUIDANCE = "Please try re-running. If this continues, contact support@skyvern.com."

# Patterns that indicate a browser session connection failure (e.g. CDP WebSocket errors).
# These errors contain internal URLs and raw HTML that should never be shown to end users.
_BROWSER_CONNECTION_PATTERNS = (
    "connect_over_cdp",
    "WebSocket error",
    "WebSocket was closed",
    "ws connecting",
    "ws unexpected response",
    "ws error",
)


def _is_browser_connection_error(message: str) -> bool:
    return any(pattern in message for pattern in _BROWSER_CONNECTION_PATTERNS)


def _is_session_closed_error(message: str) -> bool:
    # The session router closes with (4410, "session closed") when the session was already closed;
    # match the reason text, not the bare code — 4410 is reused elsewhere with other reasons.
    # Message selection only: redaction routing must keep the broad _is_browser_connection_error net.
    return "session closed" in message.lower()


# A raw CDP connect failure (e.g. from playwright.chromium.connect_over_cdp) echoes the
# endpoint URL, which can carry the remote-browser vendor host, a session-bearing path/query,
# or credentials embedded as user:pass@host. The devtools socket is always ws/wss, so a ws/wss
# URL in a browser error is unambiguously a CDP endpoint and safe to redact anywhere. The
# /json/version discovery endpoint is http/https and carries the same host/token, but so does an
# ordinary navigation/target/proxy URL — an http(s) URL is only known to be a CDP endpoint in a
# CDP-connection context, so http(s) redaction is scoped to that context (see redact_cdp_endpoint_urls).
_WS_ENDPOINT_URL_RE = re.compile(r"wss?://\S+", re.IGNORECASE)
_CDP_ENDPOINT_URL_RE = re.compile(r"(?:wss?|https?)://\S+", re.IGNORECASE)


def redact_ws_endpoint_urls(message: str) -> str:
    return _WS_ENDPOINT_URL_RE.sub("[remote browser endpoint]", message)


def redact_cdp_endpoint_urls(message: str) -> str:
    return _CDP_ENDPOINT_URL_RE.sub("[remote browser endpoint]", message)


def get_user_facing_exception_message(exception: Exception) -> str:
    if isinstance(exception, SkyvernException):
        return exception.user_facing_message

    raw = str(exception)
    if _is_browser_connection_error(raw):
        if _is_session_closed_error(raw):
            return (
                "Failed to connect to the browser session because the session is already closed. "
                "Start a new browser session to continue. "
                "If this is unexpected, contact support@skyvern.com."
            )
        return (
            f"Failed to connect to the browser session. "
            f"This is usually caused by high demand and is transient. {_BROWSER_CONNECTION_GUIDANCE}"
        )

    return f"Unexpected error: {exception}"


class DisabledBlockExecutionError(SkyvernHTTPException):
    def __init__(self, message: str | None = None):
        super().__init__(message, status_code=HTTPStatus.BAD_REQUEST)


class BrowserActionPolicyNotEnforceable(SkyvernHTTPException):
    """A workflow version was enrolled in a browser action policy the runtime cannot uphold.

    `reasons` are stable codes describing the configuration only — never workflow content, origins
    or identifiers — because they are rendered to callers and stamped on failed runs.
    """

    def __init__(self, reasons: Sequence[str]):
        self.reasons = tuple(reasons)
        super().__init__(
            f"Workflow cannot run under a browser action policy: {', '.join(self.reasons)}",
            status_code=HTTPStatus.BAD_REQUEST,
        )


class RateLimitExceeded(SkyvernHTTPException):
    def __init__(self, organization_id: str, max_requests: int, window_seconds: int):
        message = (
            f"Rate limit exceeded for organization {organization_id}. "
            f"Maximum {max_requests} requests per {window_seconds} seconds allowed."
        )
        super().__init__(message, status_code=HTTPStatus.TOO_MANY_REQUESTS)


class ConcurrencyLimitExceeded(SkyvernHTTPException):
    def __init__(self, organization_id: str, operation: str, limit: int):
        self.organization_id = organization_id
        self.operation = operation
        self.limit = limit
        message = (
            f"Concurrency limit exceeded for organization {organization_id}. "
            f"At most {limit} {operation} requests may be in flight at once. "
            "Retry once an in-flight request finishes."
        )
        super().__init__(message, status_code=HTTPStatus.TOO_MANY_REQUESTS)


class InvalidOpenAIResponseFormat(SkyvernException):
    def __init__(self, message: str | None = None):
        super().__init__(f"Invalid response format: {message}")


class PhoneNumberInputMismatch(SkyvernException):
    def __init__(self, *, expected_digit_count: int, actual_digit_count: int):
        self.expected_digit_count = expected_digit_count
        self.actual_digit_count = actual_digit_count
        super().__init__(
            "Phone input read-back mismatch: "
            f"expected {expected_digit_count} digits, found {actual_digit_count} digits."
        )


class PhoneNumberInputBrowserValidityMismatch(SkyvernException):
    def __init__(self) -> None:
        super().__init__("Phone input failed the browser validity check.")


class PhoneNumberInputBrowserInteractionFailed(SkyvernException):
    def __init__(self) -> None:
        super().__init__("Phone input browser interaction failed.")


class CardNumberInputMismatch(SkyvernException):
    def __init__(self, *, expected_digit_count: int, actual_digit_count: int):
        self.expected_digit_count = expected_digit_count
        self.actual_digit_count = actual_digit_count
        super().__init__(
            "Card number input read-back mismatch: "
            f"expected {expected_digit_count} digits, found {actual_digit_count} digits."
        )


class SecretInputMismatch(SkyvernException):
    def __init__(self) -> None:
        # No secret material in the message: not the value, its length, or its character classes.
        super().__init__("Secret input read-back mismatch after atomic re-entry.")


class FreeTextInputMismatch(SkyvernException):
    def __init__(
        self,
        *,
        element_id: str,
        intended_length: int,
        declared_max_length: int | None = None,
        declared_constraint: str | None = None,
    ):
        self.element_id = element_id
        self.intended_length = intended_length
        self.declared_max_length = declared_max_length
        self.declared_constraint = declared_constraint
        # Safe metadata only -- an element id, one intended length, and (on the static path) one declared
        # length or a coarse declared-constraint label. Never the raw intended/rendered value, a substring, a
        # rejected character, a position, or category counts.
        if declared_max_length is not None or declared_constraint is not None:
            # Static retention-only fast path: only a browser-declared constraint that demonstrably affects
            # value RETENTION in this seam -- a maxlength, or a number input sanitizing a non-numeric value --
            # produces this branch. HTML pattern / email-url validity do NOT prevent retention and never reach it.
            if declared_max_length is not None:
                # HTML maxlength counts UTF-16 code units, so state the unit explicitly (a supplementary code
                # point such as an emoji is two units).
                detail = f"the field declares a maximum length of {declared_max_length} UTF-16 code units"
                guidance = f"Propose a value within {declared_max_length} UTF-16 code units."
            elif declared_constraint == "number":
                detail = "the field is a number input, which does not retain a non-numeric value"
                guidance = "Propose a valid number."
            else:
                detail = "the value does not satisfy the field's declared constraints"
                guidance = "Propose a value that satisfies the field's declared constraints."
            message = (
                f"Free-text input for element(id={element_id}) did not retain the intended value after "
                f"re-entry. The field's declared constraints explain the rejection: {detail}. {guidance}"
            )
        else:
            # No declared retention constraint explains the rejection (including the incident, which declares
            # nothing). No live-field diagnostic probe is run, so there are no per-candidate character
            # observations -- fail closed with a generic, privacy-safe, still-actionable reason.
            message = (
                f"Free-text input for element(id={element_id}) did not retain the intended "
                f"{intended_length}-character value after re-entry; the field likely rejects this value's "
                "format, so re-entering the same value is unlikely to succeed."
            )
        super().__init__(message)


class ConditionalBranchEvaluationError(SkyvernException):
    """A conditional block could not resolve which branch to take."""


class BranchEvaluationContextTooLargeError(ConditionalBranchEvaluationError):
    """Branch evaluation cannot proceed without silently dropping required context."""

    def __init__(self) -> None:
        super().__init__(
            "Workflow branch evaluation context is too large to process safely. "
            "Reduce the workflow input or prior block output size, then retry."
        )


class MalformedBranchEvaluationError(ConditionalBranchEvaluationError):
    """The LLM's branch-evaluation output could not be safely aligned to the branches.

    Raised for a wrong result count, a missing/duplicate condition_index, or an
    unparseable shape. A distinct type lets the batch evaluator re-roll a fresh LLM call
    instead of hard-failing immediately or silently routing to the wrong branch.
    """


class FailedToSendWebhook(SkyvernException):
    def __init__(
        self,
        task_id: str | None = None,
        workflow_run_id: str | None = None,
        workflow_id: str | None = None,
        task_v2_id: str | None = None,
    ):
        workflow_run_str = f"workflow_run_id={workflow_run_id}" if workflow_run_id else ""
        workflow_str = f"workflow_id={workflow_id}" if workflow_id else ""
        task_str = f"task_id={task_id}" if task_id else ""
        task_v2_str = f"task_v2_id={task_v2_id}" if task_v2_id else ""
        super().__init__(f"Failed to send webhook. {workflow_run_str} {workflow_str} {task_str} {task_v2_str}")


class ProxyLocationNotSupportedError(SkyvernException):
    def __init__(self, proxy_location: str | None = None):
        super().__init__(f"Unknown proxy location: {proxy_location}")


class WebhookReplayError(SkyvernHTTPException):
    def __init__(
        self,
        message: str | None = None,
        *,
        status_code: int | HTTPStatus = HTTPStatus.BAD_REQUEST,
    ):
        super().__init__(message=message or "Webhook replay failed.", status_code=status_code)


class MissingWebhookTarget(WebhookReplayError):
    def __init__(self, message: str | None = None):
        super().__init__(message or "No webhook URL configured for the run.")


class MissingApiKey(WebhookReplayError):
    def __init__(self, message: str | None = None):
        super().__init__(message or "Organization does not have a valid API key configured.")


class TaskNotFound(SkyvernHTTPException):
    def __init__(self, task_id: str | None = None):
        super().__init__(f"Task {task_id} not found", status_code=HTTPStatus.NOT_FOUND)


class MissingElement(SkyvernException):
    def __init__(self, selector: str | None = None, element_id: str | None = None):
        super().__init__(
            f"Found no elements. Might be due to previous actions which removed this element."
            f" selector={selector} element_id={element_id}",
        )


class MissingExtractActionsResponse(SkyvernException):
    def __init__(self) -> None:
        super().__init__("extract-actions response missing")


class MultipleElementsFound(SkyvernException):
    def __init__(self, num: int, selector: str | None = None, element_id: str | None = None):
        super().__init__(
            f"Found {num} elements. Expected 1. num_elements={num} selector={selector} element_id={element_id}",
        )


class MissingFileUrl(SkyvernException):
    def __init__(self) -> None:
        super().__init__("File url is missing.")


class ImaginaryFileUrl(SkyvernException):
    def __init__(self, file_url: str) -> None:
        super().__init__(f"File url {file_url} is imaginary.")


@dataclass(frozen=True)
class BrowserStateDiagnostic:
    reason: str
    disconnect_observed_at: datetime
    browser_session_id: str | None = None
    event: str = "browser_context_disconnected"
    observation_source: str = "liveness_probe"

    def describe(self, detected_at: datetime) -> str:
        """Describe the disconnect observation and delay before missing-state detection.

        Playwright gives us an event timestamp when its disconnect event fires. If no event was
        delivered, the first failed liveness probe is the fallback. The gap is therefore
        observation-to-detection latency, not the browser's actual outage duration.
        """
        observation_gap_seconds = max(0.0, (detected_at - self.disconnect_observed_at).total_seconds())
        session_str = f" browser_session_id={self.browser_session_id}" if self.browser_session_id else ""
        return (
            f" Browser event={self.event} reason={self.reason}{session_str}"
            f" disconnect_observed_at={self.disconnect_observed_at.astimezone(UTC).isoformat()}"
            f" detected_at={detected_at.astimezone(UTC).isoformat()}"
            f" observation_gap_seconds={observation_gap_seconds:.3f} observation_source={self.observation_source}."
        )


def _browser_state_diagnostic_suffix(
    diagnostic: BrowserStateDiagnostic | None,
    detected_at: datetime | None,
    failure_reason: str | None,
) -> str:
    if diagnostic is not None and detected_at is not None:
        suffix = diagnostic.describe(detected_at)
    elif detected_at is not None:
        suffix = (
            f" No browser-context disconnect event was observed; detected_at={detected_at.astimezone(UTC).isoformat()}."
        )
    else:
        suffix = ""
    if failure_reason:
        suffix += f" failure_reason={failure_reason}."
    return suffix


class MissingBrowserState(SkyvernException):
    def __init__(
        self,
        task_id: str | None = None,
        workflow_run_id: str | None = None,
        *,
        diagnostic: BrowserStateDiagnostic | None = None,
        detected_at: datetime | None = None,
        failure_reason: str | None = None,
    ) -> None:
        task_str = f"task_id={task_id}" if task_id else ""
        workflow_run_str = f"workflow_run_id={workflow_run_id}" if workflow_run_id else ""
        self.diagnostic = diagnostic
        self.detected_at = detected_at
        user_facing_message = f"Browser state for {task_str} {workflow_run_str} is missing."
        super().__init__(
            f"{user_facing_message}{_browser_state_diagnostic_suffix(diagnostic, detected_at, failure_reason)}"
        )
        self._user_facing_message = user_facing_message


class MissingBrowserStatePage(SkyvernException):
    def __init__(
        self,
        task_id: str | None = None,
        workflow_run_id: str | None = None,
        *,
        diagnostic: BrowserStateDiagnostic | None = None,
        detected_at: datetime | None = None,
        failure_reason: str | None = None,
    ):
        task_str = f"task_id={task_id}" if task_id else ""
        workflow_run_str = f"workflow_run_id={workflow_run_id}" if workflow_run_id else ""
        self.diagnostic = diagnostic
        self.detected_at = detected_at
        user_facing_message = f"Browser state page is missing. {task_str} {workflow_run_str}"
        super().__init__(
            f"{user_facing_message}{_browser_state_diagnostic_suffix(diagnostic, detected_at, failure_reason)}"
        )
        self._user_facing_message = user_facing_message


class BrowserSessionDegraded(SkyvernException):
    def __init__(self, consecutive_timeouts: int, stuck_operations: str) -> None:
        self.consecutive_timeouts = consecutive_timeouts
        self.stuck_operations = stuck_operations
        super().__init__(
            f"The browser stopped responding to Skyvern after {consecutive_timeouts} consecutive "
            f"unanswered operations ({stuck_operations})"
        )


class BrowserProfileNotApplied(SkyvernException):
    def __init__(self, browser_profile_id: str) -> None:
        self.browser_profile_id = browser_profile_id
        super().__init__(f"Browser profile {browser_profile_id} was not applied by the created browser")


class MissingWorkflowRunBrowserState(SkyvernException):
    def __init__(self, workflow_run_id: str, task_id: str) -> None:
        super().__init__(f"Browser state for workflow run {workflow_run_id} and task {task_id} is missing.")


class CaptchaSolveError(SkyvernException):
    """Base for captcha-solve failures.

    Shared marker so the action handler can catch captcha-solve failures with a
    dedicated typed arm (logged as a handled failure) instead of the generic
    "Unhandled exception" arm. Cloud captcha-solve exceptions subclass this too.
    """


class CaptchaNotSolvedInTime(CaptchaSolveError):
    def __init__(self, task_id: str, final_state: str) -> None:
        super().__init__(f"Captcha not solved in time for task {task_id}. Final state: {final_state}")


class EnablingCaptchaSolver(SkyvernException):
    def __init__(self) -> None:
        super().__init__("Enabling captcha solver. Reload the page and try again.")


class ContextParameterValueNotFound(SkyvernException):
    def __init__(self, parameter_key: str, existing_keys: list[str], workflow_run_id: str) -> None:
        super().__init__(
            f"Context parameter value not found during workflow run {workflow_run_id}. "
            f"Parameter key: {parameter_key}. Existing keys: {existing_keys}"
        )


class UnknownBlockType(SkyvernException):
    def __init__(self, block_type: str) -> None:
        super().__init__(f"Unknown block type {block_type}")


class BlockNotFound(SkyvernException):
    def __init__(self, block_label: str) -> None:
        super().__init__(f"Block {block_label} not found")


class WorkflowNotFound(SkyvernHTTPException):
    def __init__(
        self,
        workflow_id: str | None = None,
        workflow_permanent_id: str | None = None,
        version: int | None = None,
    ) -> None:
        workflow_repr = ""
        if workflow_id:
            workflow_repr = f"workflow_id={workflow_id}"
        if workflow_permanent_id:
            if version:
                workflow_repr = f"workflow_permanent_id={workflow_permanent_id}, version={version}"
            else:
                workflow_repr = f"workflow_permanent_id={workflow_permanent_id}"

        super().__init__(
            f"Workflow not found. {workflow_repr}",
            status_code=HTTPStatus.NOT_FOUND,
        )


class WorkflowNotFoundForWorkflowRun(SkyvernHTTPException):
    def __init__(
        self,
        workflow_run_id: str | None = None,
    ) -> None:
        super().__init__(
            f"Workflow not found for workflow run {workflow_run_id}",
            status_code=HTTPStatus.NOT_FOUND,
        )


class WorkflowRunNotFound(SkyvernHTTPException):
    def __init__(self, workflow_run_id: str) -> None:
        super().__init__(f"WorkflowRun {workflow_run_id} not found", status_code=HTTPStatus.NOT_FOUND)


class MissingValueForParameter(SkyvernHTTPException):
    def __init__(self, parameter_key: str, workflow_id: str, workflow_run_id: str) -> None:
        super().__init__(
            f"Missing value for parameter {parameter_key} in workflow run {workflow_run_id} of workflow {workflow_id}",
            status_code=HTTPStatus.BAD_REQUEST,
        )


class UnrecognizedWorkflowParameters(SkyvernHTTPException):
    def __init__(
        self,
        unknown_keys: list[str],
        expected_keys: list[str],
        unresolved_credential_keys: list[str],
    ) -> None:
        unknown = ", ".join(unknown_keys)
        unresolved = ", ".join(unresolved_credential_keys)
        expected = ", ".join(expected_keys) or "no parameters"
        message = (
            f"The run request sent parameter(s) this workflow does not declare: {unknown}. "
            f"No credential resolved for {unresolved}, so the run would have started without one. "
            f"This workflow accepts: {expected}."
        )
        hints = [
            f"'{unknown_key}' -> '{matches[0]}'"
            for unknown_key in unknown_keys
            if (matches := difflib.get_close_matches(unknown_key, expected_keys, n=1))
        ]
        if hints:
            message += f" Did you mean {'; '.join(hints)}?"
        super().__init__(message, status_code=HTTPStatus.BAD_REQUEST)


class WorkflowRunParameterPersistenceError(SkyvernException):
    def __init__(self, parameter_key: str, workflow_id: str, workflow_run_id: str, reason: str) -> None:
        super().__init__(
            f"Failed to persist workflow parameter '{parameter_key}' for workflow run {workflow_run_id} "
            f"of workflow {workflow_id}. Reason: {reason}"
        )


# Covers the credential dict fields from SKY-8222 (password, username, secret_value, totp).
# Not exhaustive — this is defense-in-depth; the root cause is fixed in the frontend.
_SENSITIVE_CREDENTIAL_KEYS = ("password", "username", "secret", "totp", "secret_value")


def sanitize_credential_for_error(credential_id: object) -> str:
    """Prevent credential values from leaking into error messages.

    When a credential dict is accidentally stringified and passed as a credential ID,
    this ensures the raw values (passwords, usernames, etc.) are never included in
    user-facing error messages, failure reasons, or logs.
    """
    if not isinstance(credential_id, str):
        return f"<redacted - non-string type: {type(credential_id).__name__}>"
    lower = credential_id.lower()
    for key in _SENSITIVE_CREDENTIAL_KEYS:
        if key in lower:
            return "<redacted - contains credential data>"
    if len(credential_id) > 200:
        return "<redacted - value too long>"
    return credential_id


class InvalidCredentialId(SkyvernHTTPException):
    def __init__(self, credential_id: str) -> None:
        super().__init__(
            f"Invalid credential ID: {sanitize_credential_for_error(credential_id)}."
            " Failed to resolve to a valid credential.",
            status_code=HTTPStatus.BAD_REQUEST,
        )


class SequentialCredentialLimitExceeded(SkyvernHTTPException):
    def __init__(self, credential_ids: list[str]) -> None:
        sanitized = ", ".join(sanitize_credential_for_error(credential_id) for credential_id in credential_ids)
        super().__init__(
            f"This workflow run resolves to {len(credential_ids)} credentials marked run_sequentially "
            f"({sanitized}), but a run may use at most one sequential credential. Mark only one of them "
            "run_sequentially, or split the workflow so each run resolves to a single sequential credential.",
            status_code=HTTPStatus.BAD_REQUEST,
        )


class SequentialCredentialWorkerUnavailable(SkyvernHTTPException):
    def __init__(self, credential_id: str) -> None:
        super().__init__(
            f"This run resolves to a sequential credential ({sanitize_credential_for_error(credential_id)}), whose "
            "org-wide serialization gate lives only in the Temporal V2 worker, but workers are globally disabled "
            "(ENABLE_WORKER). Routing it to the legacy engine would run it unserialized, so the run fails closed. "
            "Re-enable workers, or clear run_sequentially on the credential to run it without the sequential lane.",
            status_code=HTTPStatus.SERVICE_UNAVAILABLE,
        )


class RuntimeSequentialCredentialUnsupported(SkyvernException):
    def __init__(self, workflow_run_id: str) -> None:
        super().__init__(
            f"Workflow run {workflow_run_id} resolved a sequential credential only at runtime, after publication "
            "without that credential's serialization lane. The run fails closed before loading the credential "
            "rather than using it outside the gate."
        )


class BackgroundSequentialCredentialUnsupported(SkyvernException):
    def __init__(self, workflow_run_id: str) -> None:
        super().__init__(
            f"Workflow run {workflow_run_id} resolves to a sequential credential, but the background executor "
            "has no org-wide serialization gate. The run fails closed before execution rather than using the "
            "credential outside its lane."
        )


class SyncTriggeredSequentialCredentialUnsupported(SkyvernException):
    def __init__(self, workflow_run_id: str) -> None:
        super().__init__(
            f"Triggered run {workflow_run_id} resolves to a sequential credential, but a synchronous "
            "workflow_trigger child runs inline and never reaches the Temporal V2 serialization gate, so "
            "it cannot be serialized against other runs using the same credential. The child fails closed "
            "rather than running unserialized. Clear run_sequentially on the credential, or run the child "
            "as an async (fire-and-forget) trigger so it publishes through the gate."
        )


class CopilotInlineSequentialCredentialUnsupported(SkyvernException):
    def __init__(self, workflow_run_id: str) -> None:
        super().__init__(
            f"Copilot run {workflow_run_id} resolves to a sequential credential, but the copilot inline "
            "block-run path executes in-process and never reaches the Temporal V2 serialization gate, so it "
            "cannot be serialized against other runs using the same credential. The run fails closed rather "
            "than running unserialized. Clear run_sequentially on the credential, or enable the copilot "
            "dispatch flag so the run publishes through the gate."
        )


class WorkflowParameterNotFound(SkyvernHTTPException):
    def __init__(self, workflow_parameter_id: str) -> None:
        super().__init__(
            f"Workflow parameter {workflow_parameter_id} not found",
            status_code=HTTPStatus.NOT_FOUND,
        )


class FailedToNavigateToUrl(SkyvernException):
    def __init__(self, url: str, error_message: str) -> None:
        self.url = url
        self.error_message = error_message
        super().__init__(f"Failed to navigate to url {url}. Error message: {error_message}")


class BlockedNavigationDestination(FailedToNavigateToUrl):
    """A navigation target (or one of its redirect hops) resolves to a private, link-local,
    loopback, metadata, or local-resource destination. A subclass of FailedToNavigateToUrl so
    existing navigation error handling treats it as a permanent failure and never retries it."""

    def __init__(self, url: str, reason: str) -> None:
        self.reason = reason
        super().__init__(url=url, error_message=f"blocked navigation destination: {reason}")


class FailedToReloadPage(SkyvernException):
    def __init__(self, url: str, error_message: str) -> None:
        self.url = url
        self.error_message = error_message
        super().__init__(f"Failed to reload page url {url}. Error message: {error_message}")


class FailedToStopLoadingPage(SkyvernException):
    def __init__(self, url: str, error_message: str) -> None:
        self.url = url
        self.error_message = error_message
        super().__init__(f"Failed to stop loading page url {url}. Error message: {error_message}")


class EmptyBrowserContext(SkyvernException):
    def __init__(self) -> None:
        super().__init__("Browser context is empty")


class UnexpectedTaskStatus(SkyvernException):
    def __init__(self, task_id: str, status: str) -> None:
        super().__init__(f"Unexpected task status {status} for task {task_id}")


class InvalidWorkflowTaskURLState(SkyvernException):
    def __init__(self, workflow_run_id: str) -> None:
        super().__init__(f"No Valid URL found in the first task of workflow run {workflow_run_id}")


class DisabledFeature(SkyvernException):
    def __init__(self, feature: str) -> None:
        super().__init__(f"Feature {feature} is disabled")


class UnknownBrowserType(SkyvernException):
    def __init__(self, browser_type: str) -> None:
        super().__init__(f"Unknown browser type {browser_type}")


class CdpConnectionConfigurationError(SkyvernException):
    """Raised when a configured CDP endpoint is reachable but not usable by Skyvern."""


class UnknownErrorWhileCreatingBrowserContext(SkyvernException):
    SUPPORT_GUIDANCE = "Please try re-running. If this continues, contact support@skyvern.com."

    def __init__(self, browser_type: str, exception: Exception) -> None:
        # browser_type can be a concrete remote-browser vendor identity (settings.BROWSER_TYPE);
        # keep it on the exception for structured logs but never surface it in the user message.
        self.browser_type = browser_type
        # A SkyvernException may redact its own class name (e.g. a vendor-named rate-limit
        # error whose real name is kept for logs/monitoring but must not reach end users).
        exception_type = (
            exception.user_facing_type_name if isinstance(exception, SkyvernException) else type(exception).__name__
        )
        detail = self._get_detail(exception)
        super().__init__(f"Failed to create browser context ({exception_type}). {detail}")

    @staticmethod
    def _get_detail(exception: Exception) -> str:
        if isinstance(exception, SkyvernException) and exception.message_is_user_facing:
            return exception.message or "Unexpected browser creation failure."

        if isinstance(exception, CdpConnectionConfigurationError):
            return exception.message or str(exception)

        # BrowserFactory.create_browser_context wraps every creator/setup failure, so an http(s) URL
        # here is only known to be a CDP discovery endpoint (rather than an ordinary proxy/public-IP
        # probe URL the user needs) when the error carries a CDP-connection signal. Default to ws/wss
        # redaction and escalate to http(s)+ws(s) redaction only for connect_over_cdp/WebSocket errors.
        raw = str(exception).strip()
        raw_message = (
            redact_cdp_endpoint_urls(raw) if _is_browser_connection_error(raw) else redact_ws_endpoint_urls(raw)
        )
        raw_lower = raw_message.lower()

        # Browser launch environment errors: worker cannot initialize the
        # headed browser display/graphics stack (X display or EGL/SwiftShader).
        if any(
            indicator in raw_lower
            for indicator in (
                "missing x server",
                "xserver running",
                "no display",
                "$display",
                "the platform failed to initialize",
                "no suitable egl configs found",
                "failed to get config for surface",
                "collectgraphicsinfo failed",
                "glcontext::createoffscreenglsurface failed",
                "exiting gpu process due to errors during initialization",
            )
        ):
            return (
                "Browser launch failed: worker node could not initialize the browser display/graphics stack "
                "(X display/EGL). This is an infrastructure or browser-environment issue on the worker node, "
                "not a browser profile problem. "
                f"{UnknownErrorWhileCreatingBrowserContext.SUPPORT_GUIDANCE}"
            )

        # Patchright timeout errors include a verbose "Call log" section with launch args.
        trimmed_message = raw_message.split("Call log:")[0].strip()
        # Browser launch errors include a "Browser logs" section with the binary path and flags.
        trimmed_message = trimmed_message.split("Browser logs:")[0].strip()
        normalized_message = " ".join(trimmed_message.split())

        if (
            "launch_persistent_context" in normalized_message
            and "target page, context or browser has been closed" in normalized_message.lower()
        ):
            return (
                "The browser closed unexpectedly during launch. This is usually transient. "
                f"{UnknownErrorWhileCreatingBrowserContext.SUPPORT_GUIDANCE}"
            )

        timeout_match = re.search(r"Timeout\s+(\d+)ms\s+exceeded", normalized_message, flags=re.IGNORECASE)
        if timeout_match and "launch_persistent_context" in normalized_message:
            timeout_seconds = int(timeout_match.group(1)) // 1000
            if timeout_seconds > 0:
                return (
                    f"Browser launch timed out after {timeout_seconds} seconds. "
                    f"This is usually transient. {UnknownErrorWhileCreatingBrowserContext.SUPPORT_GUIDANCE}"
                )
            return (
                "Browser launch timed out. "
                f"This is usually transient. {UnknownErrorWhileCreatingBrowserContext.SUPPORT_GUIDANCE}"
            )

        if normalized_message:
            if len(normalized_message) > 280:
                normalized_message = f"{normalized_message[:277]}..."
            return f"{normalized_message} {UnknownErrorWhileCreatingBrowserContext.SUPPORT_GUIDANCE}"

        return f"Unknown browser startup error. {UnknownErrorWhileCreatingBrowserContext.SUPPORT_GUIDANCE}"


class OrganizationNotFound(SkyvernHTTPException):
    def __init__(self, organization_id: str) -> None:
        super().__init__(
            f"Organization {organization_id} not found",
            status_code=HTTPStatus.NOT_FOUND,
        )


class StepNotFound(SkyvernHTTPException):
    def __init__(self, organization_id: str, task_id: str, step_id: str | None = None) -> None:
        super().__init__(
            f"Step {step_id or 'latest'} not found. organization_id={organization_id} task_id={task_id}",
            status_code=HTTPStatus.NOT_FOUND,
        )


class FailedToTakeScreenshot(SkyvernException):
    def __init__(self, error_message: str) -> None:
        super().__init__(f"Failed to take screenshot. Error message: {error_message}")


class ScreenshotTargetClosed(FailedToTakeScreenshot):
    pass


class EmptyScrapePage(SkyvernException):
    def __init__(self) -> None:
        super().__init__("Failed to scrape the page, returned an NONE result")


class ElementTreeBuildFailed(SkyvernException):
    def __init__(self, *, returned: str) -> None:
        # Says what reached Python, not what the page produced: on the main-world lane a
        # RemoteObject with no `value` key also arrives as None from a build that succeeded.
        self.returned = returned
        super().__init__(f"Element tree build returned {returned}, not [elements, element_tree]")


class ScrapingFailed(SkyvernException):
    def __init__(self, *, reason: str | None = None) -> None:
        self.reason = reason
        super().__init__("Scraping failed.")


class SkyvernActionFailed(SkyvernException):
    """Operationally-expected failure during an SDK action execution."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class StaleFrameSelectionError(RuntimeError):
    """The selected iframe belongs to a different page than the one now resolved."""

    def __init__(self, name: str, url: str) -> None:
        super().__init__(
            f"Stale frame selection: the selected iframe (name={name[:80]!r}, url={url!r}) belongs to a "
            "different page than the one now in focus."
        )


class ScrapingFailedBlankPage(ScrapingFailed):
    def __init__(self) -> None:
        super().__init__(reason="It's a blank page. Please ensure there is a non-blank page for Skyvern to work with.")


class MissingStarterUrl(SkyvernException):
    def __init__(self, block_label: str | None = None) -> None:
        self.block_label = block_label
        location = f"block '{block_label}'" if block_label else "the first browser block"
        super().__init__(
            f"{location} has no starting URL set. The first browser block must have a URL to navigate to. "
            "Set a URL on the block, or reference a workflow parameter (e.g. '{{ starting_url }}')."
        )


class WorkflowRunContextNotInitialized(SkyvernException):
    def __init__(self, workflow_run_id: str) -> None:
        super().__init__(f"WorkflowRunContext not initialized for workflow run {workflow_run_id}")


class DownloadFileMaxSizeExceeded(SkyvernException):
    def __init__(self, max_size: int) -> None:
        self.max_size = max_size
        super().__init__(f"Download file size exceeded the maximum allowed size of {max_size} MB.")


class GoogleDriveFileNotAccessible(SkyvernException):
    def __init__(self, url: str) -> None:
        self.url = url
        super().__init__(
            f"Google Drive returned a sign-in or permission page instead of the file content for {url}. "
            "The file is not publicly accessible. Share it so anyone with the link can view it, "
            "or use a direct-download link."
        )


class UploadFileMaxSizeExceeded(SkyvernException):
    def __init__(self, file_size_bytes: int, max_size_bytes: int) -> None:
        self.file_size_bytes = file_size_bytes
        self.max_size_bytes = max_size_bytes
        super().__init__(
            f"Upload file size {file_size_bytes / 1024 / 1024:.1f} MB exceeded the maximum "
            f"allowed size of {max_size_bytes / 1024 / 1024:.0f} MB."
        )


class DownloadFileMaxWaitingTime(SkyvernException):
    def __init__(self, downloading_files: list[str]) -> None:
        self.downloading_files = downloading_files
        super().__init__(f"Long-time downloading files [{downloading_files}].")


class NoFileDownloadTriggered(SkyvernException):
    def __init__(self, element_id: str) -> None:
        super().__init__(f"Clicking on element doesn't trigger the file download. element_id={element_id}")


class CachedDownloadError(SkyvernException):
    """Raised when a cached download block fails to produce a file on the local filesystem."""

    def __init__(self, message: str) -> None:
        super().__init__(f"Cached download error: {message}")


class BitwardenSecretError(SkyvernException):
    def __init__(self, message: str) -> None:
        super().__init__(f"Bitwarden secret error: {message}")


class BitwardenBaseError(SkyvernException):
    def __init__(self, message: str) -> None:
        super().__init__(f"Bitwarden error: {message}")


class BitwardenLoginError(BitwardenBaseError):
    def __init__(self, message: str) -> None:
        super().__init__(f"Error logging in to Bitwarden: {message}")


class BitwardenUnlockError(BitwardenBaseError):
    def __init__(self, message: str) -> None:
        super().__init__(f"Error unlocking Bitwarden: {message}")


class BitwardenCreateCollectionError(BitwardenBaseError):
    def __init__(self, message: str) -> None:
        super().__init__(f"Error creating collection in Bitwarden: {message}")


class BitwardenCreateLoginItemError(BitwardenBaseError):
    def __init__(self, message: str) -> None:
        super().__init__(f"Error creating login item in Bitwarden: {message}")


class BitwardenCreateCreditCardItemError(BitwardenBaseError):
    def __init__(self, message: str) -> None:
        super().__init__(f"Error creating credit card item in Bitwarden: {message}")


class BitwardenCreateFolderError(BitwardenBaseError):
    def __init__(self, message: str) -> None:
        super().__init__(f"Error creating folder in Bitwarden: {message}")


class BitwardenGetItemError(BitwardenBaseError):
    def __init__(self, message: str) -> None:
        super().__init__(f"Error getting item in Bitwarden: {message}")


class BitwardenListItemsError(BitwardenBaseError):
    def __init__(self, message: str) -> None:
        super().__init__(f"Error listing items in Bitwarden: {message}")


class BitwardenTOTPError(BitwardenBaseError):
    def __init__(self, message: str) -> None:
        super().__init__(f"Error generating TOTP in Bitwarden: {message}")


class BitwardenLogoutError(BitwardenBaseError):
    def __init__(self, message: str) -> None:
        super().__init__(f"Error logging out of Bitwarden: {message}")


class BitwardenSyncError(BitwardenBaseError):
    def __init__(self, message: str) -> None:
        super().__init__(f"Error syncing Bitwarden: {message}")


class BitwardenAccessDeniedError(BitwardenBaseError):
    def __init__(self) -> None:
        super().__init__(
            "Current organization does not have access to the specified Bitwarden collection. "
            "Contact Skyvern support to enable access. This is a security layer on top of Bitwarden, "
            "Skyvern team needs to let your Skyvern account access the Bitwarden collection."
        )


class OnePasswordBaseError(SkyvernException):
    def __init__(self, message: str) -> None:
        super().__init__(f"1Password error: {message}")


class OnePasswordServiceUnavailableError(OnePasswordBaseError):
    def __init__(self, status_code: int | None = None, lookup_context: str | None = None) -> None:
        suffix = f" (HTTP {status_code})" if status_code else ""
        message = (
            f"1Password is currently unavailable{suffix}. "
            "This is an upstream outage on 1Password's side, not a Skyvern issue. "
            "Please retry in a few minutes."
        )
        if lookup_context:
            message = f"{message} {lookup_context}"
        super().__init__(message)


class OnePasswordRateLimitError(OnePasswordBaseError):
    def __init__(self, message: str) -> None:
        super().__init__(f"1Password rate limit exceeded: {message}. Please retry in a few minutes.")


class OnePasswordSessionExpiredError(OnePasswordBaseError):
    def __init__(self, message: str) -> None:
        super().__init__(f"1Password service account session expired: {message}.")


class OnePasswordGetItemError(OnePasswordBaseError):
    def __init__(self, message: str) -> None:
        super().__init__(f"Error getting item from 1Password: {message}")


class CredentialParameterParsingError(SkyvernException):
    def __init__(self, message: str) -> None:
        super().__init__(f"Error parsing credential parameter: {message}")


class CredentialParameterNotFoundError(SkyvernException):
    def __init__(self, credential_parameter_id: str | None) -> None:
        super().__init__(
            f"Could not find credential parameter: {sanitize_credential_for_error(credential_parameter_id)}"
        )


class CredentialVaultShapeMismatchError(SkyvernHTTPException):
    def __init__(self, credential_id: str, stored_credential_type: str) -> None:
        super().__init__(
            f"Credential {credential_id} is recorded as a password credential but the vault holds a "
            f"{stored_credential_type}. Refusing to update it, because an omitted password would "
            "overwrite the stored secret with an empty one.",
            status_code=HTTPStatus.CONFLICT,
        )


class CredentialVaultNotConfiguredError(SkyvernException):
    def __init__(self, vault_type: str, credential_id: str) -> None:
        super().__init__(
            f"Credential vault service '{vault_type}' is not configured. "
            f"Credential {credential_id} was found in DB but cannot be resolved."
        )


class UnknownElementTreeFormat(SkyvernException):
    def __init__(self, fmt: str) -> None:
        super().__init__(f"Unknown element tree format {fmt}")


class TerminationError(SkyvernException):
    def __init__(self, reason: str, step_id: str | None = None, task_id: str | None = None) -> None:
        super().__init__(f"Termination error. Reason: {reason}")


class StepTerminationError(TerminationError):
    def __init__(self, reason: str, step_id: str | None = None, task_id: str | None = None) -> None:
        super().__init__(f"Step {step_id} cannot be executed and task is failed. Reason: {reason}")


class TaskTerminationError(TerminationError):
    def __init__(self, reason: str, step_id: str | None = None, task_id: str | None = None) -> None:
        super().__init__(f"Task {task_id} failed. Reason: {reason}")


class BlockTerminationError(SkyvernException):
    def __init__(self, workflow_run_block_id: str, workflow_run_id: str, reason: str) -> None:
        super().__init__(
            f"Block {workflow_run_block_id} cannot be executed and workflow run {workflow_run_id} is failed. Reason: {reason}"
        )


class StepUnableToExecuteError(SkyvernException):
    def __init__(self, step_id: str, reason: str) -> None:
        super().__init__(f"Step {step_id} cannot be executed and task execution is stopped. Reason: {reason}")


class SVGConversionFailed(SkyvernException):
    def __init__(self, svg_html: str) -> None:
        super().__init__(f"Failed to convert SVG after max retries. svg_html={svg_html}")


class UnsupportedActionType(SkyvernException):
    def __init__(self, action_type: str):
        super().__init__(f"Unsupport action type: {action_type}")


_INVALID_ELEMENT_FOR_TEXT_INPUT_DATE_HINT = (
    " The element appears to be a non-input segment of a custom date widget. "
    "Look for a calendar icon, date picker trigger, or stepper button near this "
    "element and click that instead of typing into the segment."
)


class InvalidElementForTextInput(SkyvernException):
    def __init__(self, element_id: str, tag_name: str, *, is_date_related: bool = False):
        message = f"The {tag_name} element with id={element_id} doesn't support text input."
        if is_date_related:
            message += _INVALID_ELEMENT_FOR_TEXT_INPUT_DATE_HINT
        super().__init__(message)


class FailedToClearInputField(SkyvernException):
    def __init__(self, element_id: str, tag_name: str):
        super().__init__(
            f"Failed to clear the existing value of the {tag_name} element with id={element_id} before typing."
        )


class ElementIsNotLabel(SkyvernException):
    def __init__(self, tag_name: str):
        super().__init__(f"<{tag_name}> element is not <label>")


class NoneFrameError(SkyvernException):
    def __init__(self, frame_id: str):
        super().__init__(f"frame content is none. frame_id={frame_id}")


class MissingElementDict(SkyvernException):
    def __init__(self, element_id: str) -> None:
        super().__init__(f"Invalid element id. element_id={element_id}")


class MissingElementInIframe(SkyvernException):
    def __init__(self, element_id: str) -> None:
        super().__init__(f"Found no iframe includes the element. element_id={element_id}")


class MissingElementInCSSMap(SkyvernException):
    def __init__(self, element_id: str) -> None:
        super().__init__(f"Found no css selector in the CSS map for the element. element_id={element_id}")


class InputActionOnSelect2Dropdown(SkyvernException):
    def __init__(self, element_id: str):
        super().__init__(
            f"Input action on a select element, please try to use select action on this element. element_id={element_id}"
        )


class FailToClick(SkyvernException):
    def __init__(self, element_id: str, msg: str, anchor: str = "self"):
        super().__init__(f"Failed to click({anchor}). element_id={element_id}, error_msg={msg}")


class FailToHover(SkyvernException):
    def __init__(self, element_id: str, msg: str):
        super().__init__(f"Failed to hover. element_id={element_id}, error_msg={msg}")


class FailToSelectByLabel(SkyvernException):
    def __init__(self, element_id: str):
        super().__init__(f"Failed to select by label. element_id={element_id}")


class FailToSelectByIndex(SkyvernException):
    def __init__(self, element_id: str):
        super().__init__(f"Failed to select by index. element_id={element_id}")


class EmptyDomOrHtmlTree(SkyvernException):
    def __init__(self) -> None:
        super().__init__("Empty dom or html tree")


class OptionIndexOutOfBound(SkyvernException):
    def __init__(self, element_id: str):
        super().__init__(f"Option index is out of bound. element_id={element_id}")


class FailToSelectByValue(SkyvernException):
    def __init__(self, element_id: str):
        super().__init__(f"Failed to select by value. element_id={element_id}")


class EmptySelect(SkyvernException):
    def __init__(self, element_id: str):
        super().__init__(
            f"nothing is selected, try to select again. element_id={element_id}",
        )


class TaskAlreadyCanceled(SkyvernHTTPException):
    def __init__(self, new_status: str, task_id: str):
        super().__init__(
            f"Invalid task status transition to {new_status} for {task_id} because task is already canceled"
        )


class TaskAlreadyTimeout(SkyvernException):
    def __init__(self, task_id: str):
        super().__init__(f"Task {task_id} is timed out")


class InvalidTaskStatusTransition(SkyvernHTTPException):
    def __init__(self, old_status: str, new_status: str, task_id: str):
        super().__init__(f"Invalid task status transition from {old_status} to {new_status} for {task_id}")


class ErrFoundSelectableElement(SkyvernException):
    def __init__(self, element_id: str, err: Exception):
        super().__init__(
            f"error when selecting elements in the children list. element_id={element_id}, error={repr(err)}"
        )


class NoSelectableElementFound(SkyvernException):
    def __init__(self, element_id: str):
        super().__init__(f"No selectable elements found in the children list. element_id={element_id}")


class HttpException(SkyvernException):
    def __init__(self, status_code: int, url: str, msg: str | None = None) -> None:
        self.status_code = status_code
        self.url = url
        self.error_message = msg
        super().__init__(f"HTTP Exception, status_code={status_code}, url={url}" + (f", msg={msg}" if msg else ""))


class WrongElementToUploadFile(SkyvernException):
    def __init__(self, element_id: str):
        super().__init__(
            f"No file chooser dialog opens, so file can't be uploaded through element {element_id}. Please try to upload again with another element."
        )


class FailedToFetchSecret(SkyvernException):
    def __init__(self) -> None:
        super().__init__("Failed to get the actual value of the secret parameter")


class NoIncrementalElementFoundForCustomSelection(SkyvernException):
    def __init__(self, element_id: str) -> None:
        super().__init__(
            f"No incremental element found, try it again later or try another element. element_id={element_id}"
        )


class NoAvailableOptionFoundForCustomSelection(SkyvernException):
    """Raised when the dropdown was populated but no option matched the requested target."""

    def __init__(
        self,
        reason: str | None,
        target_value: str | None = None,
        observed_options: list[str] | None = None,
    ) -> None:
        observed_excerpt = observed_options[:5] if observed_options else []
        observed_count = len(observed_options) if observed_options is not None else 0
        parts = ["No available option to select.", "code=OPTION_NOT_AVAILABLE"]
        if target_value:
            parts.append(f"target_value={target_value!r}")
        if observed_options is not None:
            parts.append(f"observed_options_count={observed_count}")
        if observed_excerpt:
            parts.append(f"observed_options_excerpt={observed_excerpt}")
        if reason:
            parts.append(f"reason={reason!r}")
        super().__init__(" ".join(parts))
        self.code = "OPTION_NOT_AVAILABLE"
        self.target_value = target_value
        self.observed_options_count = observed_count
        self.observed_options_excerpt = observed_excerpt
        self.reason = reason
        # Set True when an earlier level of a cascading select already committed a click before this
        # miss, so the widget is partially mutated and the miss must not be reported as a clean skip.
        self.widget_mutated = False


class NoElementMatchedForTargetOption(SkyvernException):
    def __init__(self, target: str, reason: str | None) -> None:
        super().__init__(
            f"No element matches for the target value, try another value. reason: {reason}.  target_value='{target}'."
        )


class NoElementBoudingBox(SkyvernException):
    def __init__(self, element_id: str) -> None:
        super().__init__(f"Element does not have a bounding box. element_id={element_id}")


class NoIncrementalElementFoundForAutoCompletion(SkyvernException):
    def __init__(self, element_id: str, text: str) -> None:
        super().__init__(f"No auto completion shown up after fill in [{text}]. element_id={element_id}")


class NoSuitableAutoCompleteOption(SkyvernException):
    def __init__(self, reasoning: str | None, target_value: str) -> None:
        super().__init__(
            f"No suitable auto complete option to choose. target_value={target_value}, reasoning={reasoning}"
        )


class NoAutoCompleteOptionMeetCondition(SkyvernException):
    def __init__(
        self, reasoning: str | None, required_relevance: float, target_value: str, closest_relevance: float
    ) -> None:
        super().__init__(
            f"No auto complete option meet the condition(relevance_float>{required_relevance}). reasoning={reasoning}, target_value={target_value}, closest_relevance={closest_relevance}"
        )


class ErrEmptyTweakValue(SkyvernException):
    def __init__(self, reasoning: str | None, current_value: str) -> None:
        super().__init__(
            f"Empty tweaked value for the current value. reasoning={reasoning}, current_value={current_value}"
        )


class FailToFindAutocompleteOption(SkyvernException):
    def __init__(self, current_value: str) -> None:
        super().__init__(
            f"Can't find a suitable auto completion for the current value, maybe retry with another reasonable value. current_value={current_value}"
        )


class IllegitComplete(SkyvernException):
    def __init__(self, data: dict | None = None) -> None:
        data_str = f", data={data}" if data else ""
        super().__init__(f"Illegit complete{data_str}")


class CachedActionPlanError(SkyvernException):
    def __init__(self, message: str) -> None:
        super().__init__(message)


class InvalidUrl(SkyvernHTTPException):
    def __init__(self, url: str) -> None:
        super().__init__(f"Invalid URL: {url}. Skyvern supports HTTP and HTTPS urls with max 2083 character length.")


class BlockedHost(SkyvernHTTPException):
    def __init__(self, host: str) -> None:
        super().__init__(
            f"The host in your url is blocked: {host}",
            status_code=HTTPStatus.BAD_REQUEST,
        )


class UnresolvableHost(BlockedHost):
    pass


class InvalidWorkflowParameter(SkyvernHTTPException):
    def __init__(self, expected_parameter_type: str, value: str, workflow_permanent_id: str | None = None) -> None:
        message = f"Invalid workflow parameter. Expected parameter type: {expected_parameter_type}. Value: {value}."
        if workflow_permanent_id:
            message += f" Workflow permanent id: {workflow_permanent_id}"
        super().__init__(
            message,
            status_code=HTTPStatus.BAD_REQUEST,
        )


class ActionExecutionTimeout(SkyvernException):
    def __init__(self, action_type: str, timeout_seconds: float):
        super().__init__(
            f"Action execution timed out after {timeout_seconds:.0f} seconds and was aborted"
            f" (action_type={action_type}). The browser action did not complete in time —"
            " the page or browser may have become unresponsive."
        )


class InteractWithDisabledElement(SkyvernException):
    def __init__(self, element_id: str):
        super().__init__(
            f"The element(id={element_id}) now is disabled, try to interact with it later when it's enabled."
        )


class InputToInvisibleElement(SkyvernException):
    def __init__(self, element_id: str):
        super().__init__(
            f"The element(id={element_id}) now is not visible. Try to interact with other elements, or try to interact with it later when it's visible."
        )


class InputToReadonlyElement(SkyvernException):
    def __init__(self, element_id: str):
        super().__init__(
            f"The element(id={element_id}) now is readonly. Try to interact with other elements, or try to interact with it later when it's not readonly."
        )


class FailedToParseActionInstruction(SkyvernException):
    def __init__(self, reason: str | None, error_type: str | None):
        super().__init__(
            f"Failed to parse the action instruction as '{reason}({error_type})'",
        )


class UnsupportedTaskType(SkyvernException):
    def __init__(self, task_type: str):
        super().__init__(f"Not supported task type [{task_type}]")


class InteractWithDropdownContainer(SkyvernException):
    def __init__(self, element_id: str):
        super().__init__(
            f"Select on the dropdown container instead of the option, try again with another element. element_id={element_id}"
        )


class UrlGenerationFailure(SkyvernHTTPException):
    def __init__(self) -> None:
        super().__init__("Failed to generate the url for the prompt")


class TaskV2NotFound(SkyvernHTTPException):
    def __init__(self, task_v2_id: str) -> None:
        super().__init__(f"Task v2 {task_v2_id} not found")


class NoTOTPVerificationCodeFound(SkyvernHTTPException):
    # Status-code summary of what totp_verification_url returned, set only when the
    # endpoint answered every poll without ever honoring the documented response shape.
    webhook_diagnostics: str | None = None

    def __init__(
        self,
        task_id: str | None = None,
        workflow_run_id: str | None = None,
        workflow_id: str | None = None,
        totp_verification_url: str | None = None,
        totp_identifier: str | None = None,
        webhook_diagnostics: str | None = None,
    ) -> None:
        self.webhook_diagnostics = webhook_diagnostics
        msg = "No TOTP verification code found."
        if task_id:
            msg += f" task_id={task_id}"
        if workflow_run_id:
            msg += f" workflow_run_id={workflow_run_id}"
        if workflow_id:
            msg += f" workflow_id={workflow_id}"
        if totp_verification_url:
            msg += f" totp_verification_url={totp_verification_url}"
        if totp_identifier:
            msg += f" totp_identifier={totp_identifier}"
        if webhook_diagnostics:
            msg += f" {webhook_diagnostics}"
        super().__init__(msg)


class FailedToGetTOTPVerificationCode(SkyvernException):
    reason: str | None = None

    def __init__(
        self,
        task_id: str | None = None,
        workflow_run_id: str | None = None,
        workflow_id: str | None = None,
        totp_verification_url: str | None = None,
        totp_identifier: str | None = None,
        reason: str | None = None,
    ) -> None:
        self.reason = reason
        msg = "Failed to get TOTP verification code."
        if task_id:
            msg += f" task_id={task_id}"
        if workflow_run_id:
            msg += f" workflow_run_id={workflow_run_id}"
        if workflow_id:
            msg += f" workflow_id={workflow_id}"
        if totp_verification_url:
            msg += f" totp_verification_url={totp_verification_url}"
        if totp_identifier:
            msg += f" totp_identifier={totp_identifier}"
        super().__init__(f"Failed to get TOTP verification code. reason: {reason}")


class SkyvernContextWindowExceededError(SkyvernException):
    def __init__(self, model: str | None = None, prompt_name: str | None = None) -> None:
        details = []
        if model:
            details.append(f"model: {model}")
        if prompt_name:
            details.append(f"prompt: {prompt_name}")
        detail_str = f" ({', '.join(details)})" if details else ""
        message = f"LLM context window exceeded{detail_str}. The page may have too much content for the AI model to process. Please try again or contact support@skyvern.com for help."
        super().__init__(message)


class LLMCallerNotFoundError(SkyvernException):
    def __init__(self, uid: str) -> None:
        super().__init__(f"LLM caller for {uid} is not found")


class BrowserSessionAlreadyOccupiedError(SkyvernHTTPException):
    def __init__(self, browser_session_id: str, runnable_id: str) -> None:
        super().__init__(f"Browser session {browser_session_id} is already occupied by {runnable_id}")


class BrowserSessionOwnershipConflict(SkyvernHTTPException):
    def __init__(self, browser_session_id: str) -> None:
        super().__init__(
            f"Persistent browser session {browser_session_id} is owned by a different runnable",
            status_code=HTTPStatus.CONFLICT,
        )


class BrowserSessionNotRenewable(SkyvernException):
    def __init__(self, reason: str, browser_session_id: str) -> None:
        super().__init__(f"Browser session {browser_session_id} is not renewable: {reason}")


class MissingBrowserAddressError(SkyvernException):
    def __init__(self, browser_session_id: str) -> None:
        super().__init__(f"Browser session {browser_session_id} does not have an address.")


class MissingRoutedVncAddressError(SkyvernException):
    """No credential-bearing VNC address can be built for a session.

    Raised instead of falling back to the browser's own network address: a live view served
    over an unauthenticated internal address is worse than no live view (SKY-13287).
    """

    def __init__(self, browser_session_id: str) -> None:
        super().__init__(f"Browser session {browser_session_id} has no routed VNC address.")


class BrowserSessionClosed(SkyvernHTTPException):
    def __init__(self, browser_session_id: str, *, reason: str | None = None) -> None:
        super().__init__(
            f"Browser session {browser_session_id} {reason or 'is closed'}. Create a new browser session and retry.",
            status_code=HTTPStatus.GONE,
        )


class BrowserSessionNotFound(SkyvernHTTPException):
    def __init__(self, browser_session_id: str) -> None:
        super().__init__(
            f"Browser session {browser_session_id} does not exist or is not live.",
            status_code=HTTPStatus.NOT_FOUND,
        )


class MissingOrganizationForBrowserSession(SkyvernException):
    def __init__(self, browser_session_id: str) -> None:
        super().__init__(f"Cannot acquire browser session {browser_session_id} without an organization identity.")


class MissingBrowserStateForBrowserSession(SkyvernException):
    def __init__(self, browser_session_id: str) -> None:
        super().__init__(
            f"Browser session {browser_session_id} has no reusable browser state (cold or evicted); "
            "cannot acquire it for the script."
        )


class BrowserSessionSwitchNotAllowed(SkyvernException):
    def __init__(self, script_id: str | None, bound_session_id: str | None, requested_session_id: str) -> None:
        super().__init__(
            f"Script {script_id} already bound a browser (session {bound_session_id}); cannot switch to "
            f"browser session {requested_session_id} mid-run."
        )


class BrowserSessionStartupTimeout(SkyvernHTTPException):
    def __init__(self, browser_session_id: str) -> None:
        super().__init__(
            f"Browser session {browser_session_id} failed to start within the timeout period.",
            status_code=HTTPStatus.GATEWAY_TIMEOUT,
        )


class BrowserProfileNotFound(SkyvernHTTPException):
    def __init__(self, profile_id: str, organization_id: str | None = None) -> None:
        message = f"Browser profile {profile_id} not found"
        if organization_id:
            message += f" for organization {organization_id}"
        super().__init__(message, status_code=HTTPStatus.NOT_FOUND)


class APIKeyNotFound(SkyvernHTTPException):
    def __init__(self, organization_id: str) -> None:
        super().__init__(f"No valid API key token found for organization {organization_id}")


class ElementOutOfCurrentViewport(SkyvernException):
    def __init__(self, element_id: str):
        super().__init__(f"Element {element_id} is out of current viewport")


class ScriptNotFound(SkyvernHTTPException):
    def __init__(self, script_id: str) -> None:
        super().__init__(f"Script {script_id} not found")


class NoTOTPSecretFound(SkyvernException):
    def __init__(self) -> None:
        super().__init__("No TOTP secret found")


class NoElementFound(SkyvernException):
    def __init__(self) -> None:
        super().__init__("No element found.")


class OutputParameterNotFound(SkyvernHTTPException):
    def __init__(self, block_label: str, workflow_permanent_id: str) -> None:
        super().__init__(
            f"Output parameter for {block_label} not found in workflow {workflow_permanent_id}",
            status_code=HTTPStatus.BAD_REQUEST,
        )


class TemporalSubmissionFailed(SkyvernHTTPException):
    def __init__(self, workflow_type: str, workflow_run_id: str | None = None) -> None:
        workflow_run_str = f" for workflow_run_id={workflow_run_id}" if workflow_run_id else ""
        super().__init__(
            f"Failed to submit {workflow_type} to Temporal{workflow_run_str}",
            status_code=HTTPStatus.INTERNAL_SERVER_ERROR,
        )


class AzureBaseError(SkyvernException):
    def __init__(self, message: str) -> None:
        super().__init__(f"Azure error: {message}")


class AzureConfigurationError(AzureBaseError):
    def __init__(self, message: str) -> None:
        super().__init__(f"Error in Azure configuration: {message}")


###### Script Exceptions ######


class ScriptTerminationException(SkyvernException):
    def __init__(self, reason: str | None = None) -> None:
        super().__init__(reason)


class InProcessScriptExecutionDenied(SkyvernException):
    """Refusal to load a cached script into the worker process.

    ``fail_closed`` separates an integrity verdict the caller must not work around from a
    routing verdict it should absorb by running the workflow through the agent instead.
    """

    def __init__(self, *, seam: str, selection_reason: str, fail_closed: bool = True) -> None:
        self.seam = seam
        self.selection_reason = selection_reason
        self.fail_closed = fail_closed
        super().__init__(f"In-process script execution denied at {seam}: {selection_reason}")


class IllegitCompleteScriptTermination(ScriptTerminationException):
    """Raised when a cached script's page.complete() is rejected by the verifier; distinct from plain ScriptTerminationException, which is an intentional terminate()."""


class InvalidSchemaError(SkyvernException):
    def __init__(self, message: str, validation_errors: list[str] | None = None):
        self.message = message
        self.validation_errors = validation_errors or []
        super().__init__(self.message)


class PDFEmbedBase64DecodeError(SkyvernException):
    """Raised when failed to extract or decode base64 data from PDF embed src attribute."""

    def __init__(self, pdf_embed_src: str | None = None, reason: str | None = None):
        self.pdf_embed_src = pdf_embed_src
        self.reason = reason
        message = "Failed to extract or decode base64 data from PDF embed src"
        if reason:
            message += f". Reason: {reason}"
        if pdf_embed_src:
            # Truncate long base64 strings for logging
            src_preview = pdf_embed_src[:100] + "..." if len(pdf_embed_src) > 100 else pdf_embed_src
            message += f". PDF embed src: {src_preview}"
        super().__init__(message)


class PDFParsingError(SkyvernException):
    """Raised when PDF parsing fails with all available parsers."""

    def __init__(self, file_identifier: str, pypdf_error: str, pdfplumber_error: str):
        self.file_identifier = file_identifier
        self.pypdf_error = pypdf_error
        self.pdfplumber_error = pdfplumber_error
        super().__init__(
            f"Failed to parse PDF '{file_identifier}'. pypdf error: {pypdf_error}; pdfplumber error: {pdfplumber_error}"
        )


class ImaginarySecretValue(SkyvernException):
    def __init__(self, value: str) -> None:
        super().__init__(
            f"The value {value} is imaginary. Try to double-check to see if this value is included in the provided information"
        )


class CodeBlockRunnerSelectionError(SkyvernException):
    """Raised when the secure CodeBlock runner selection policy cannot be evaluated safely.

    The block-execution call site catches this and fails the block closed instead of
    silently falling back to in-process execution.
    """


class DownloadSaveIncompleteError(SkyvernException):
    """save_downloaded_files finished its loop but skipped at least one file.

    Files that could be saved are already saved when this raises, so a caller may treat
    the save as retryable-incomplete rather than failed."""

    def __init__(self, skipped_files: Sequence[str]) -> None:
        self.skipped_files = list(skipped_files)
        super().__init__(f"{len(self.skipped_files)} downloaded file(s) could not be fully saved and registered")
