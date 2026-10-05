from __future__ import annotations

import math
import re
from enum import StrEnum
from typing import Any

import structlog

from skyvern.constants import PROXY_TRANSPORT_NAV_ERRORS
from skyvern.exceptions import NO_ADDRESS_RECORD_NAV_ERROR_MARKER, CaptchaSolveError, FailedToNavigateToUrl

LOG = structlog.get_logger(__name__)


class FailureCategory(StrEnum):
    ANTI_BOT_DETECTION = "ANTI_BOT_DETECTION"
    PROXY_ERROR = "PROXY_ERROR"
    BROWSER_ERROR = "BROWSER_ERROR"
    BROWSER_SESSION_EXPIRED = "BROWSER_SESSION_EXPIRED"
    NAVIGATION_FAILURE = "NAVIGATION_FAILURE"
    PAGE_LOAD_TIMEOUT = "PAGE_LOAD_TIMEOUT"
    ELEMENT_STATE_TIMEOUT = "ELEMENT_STATE_TIMEOUT"
    AUTH_FAILURE = "AUTH_FAILURE"
    LLM_ERROR = "LLM_ERROR"
    CREDENTIAL_ERROR = "CREDENTIAL_ERROR"
    DATA_EXTRACTION_FAILURE = "DATA_EXTRACTION_FAILURE"
    ELEMENT_NOT_FOUND = "ELEMENT_NOT_FOUND"
    WRONG_PAGE_STATE = "WRONG_PAGE_STATE"
    MAX_STEPS_EXCEEDED = "MAX_STEPS_EXCEEDED"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    LLM_REASONING_ERROR = "LLM_REASONING_ERROR"
    INFRASTRUCTURE_ERROR = "INFRASTRUCTURE_ERROR"
    WEBSITE_ERROR = "WEBSITE_ERROR"
    PARAMETER_BINDING_ERROR = "PARAMETER_BINDING_ERROR"
    UNKNOWN = "UNKNOWN"


# Matches the activity/heartbeat timeout tokens the worker persists when a Temporal-activity
# timeout finalizes a run. Word-anchored so "inactivity timeout" (a distinct page-level reason)
# is NOT caught and correctly stays PAGE_LOAD_TIMEOUT.
_INFRA_TIMEOUT_RE = re.compile(r"\b(?:activity|heartbeat) timeout\b")
# Playwright names the operation that timed out. A locator operation waiting for an element to
# reach a state is not a page-load failure, and telling the user to confirm the URL for one sends
# both them and copilot repair after the wrong thing. Truncated messages keep only the call-log
# tail ("waiting for locator(...)"), so the bare forms match too.
_ELEMENT_OPERATION_RE = re.compile(
    r"\b(?:locator\.[a-z_]+\b|locator\(|wait_for_selector\b|waiting for selector\b|get_by_[a-z_]+)"
)
# Selector payloads name page structure, not failure semantics: `locator('#password')` is the
# element waited for, not a rejected login. Auth keyword scans run on text with these excised.
_SELECTOR_PAYLOAD_RE = re.compile(r"(?:locator|get_by_[a-z_]+|wait_for_selector)\([^)]*\)|selector ['\"][^'\"]*['\"]")

_PROXY_TRANSPORT_CODES_LOWER = tuple(code.lower() for code in PROXY_TRANSPORT_NAV_ERRORS)
_NO_ADDRESS_RECORD_MARKER_LOWER = NO_ADDRESS_RECORD_NAV_ERROR_MARKER.lower()
# Without the exception, only the driver message inside FailedToNavigateToUrl's own sentence counts: other
# text (a model-written outcome reason, the navigated URL) can mention a transport code the proxy never raised.
_NAV_FAILURE_DRIVER_MESSAGE_RE = re.compile(r"failed to navigate to url \S+\. error message: (.*)", re.DOTALL)
_URL_RE = re.compile(r"[a-z][a-z0-9+.-]*://\S+")
# Throttle or block wording is never an LLM provider failure. In a browser-agent reason it is the site's own page
# (bot protection); in an integration's reason it is the called service throttling us (a website error).
_SITE_THROTTLE_RE = re.compile(
    r"\brate[\s-]?limit|too many requests|requested too many times|\b(?:http|status(?: code)?|error)\s*:?\s*429\b"
)
_SITE_FORBIDDEN_RE = re.compile(r"\b403 (?:error|forbidden)\b|\b(?:http|status(?: code)?|error)\s*:?\s*403\b")
# Integration, vault and storage APIs report their own quota or status, which no site produced. Each "<block_type>
# block failed|terminated|timed out" prefix (loops, conditionals and workflow triggers forward their child's) is read
# before the reason it wraps, and an unlisted block type counts as an integration so a new one is never labeled bot
# protection.
_BLOCK_FAILURE_PREFIX_RE = re.compile(
    r"\s*([a-z][a-z0-9_]*) block (?:failed|terminated|timed out)\b(?:\.\s*(?:failure reason|reason):\s*)?"
)
_BROWSER_AGENT_BLOCK_TYPES = frozenset(
    {"task", "task_v2", "navigation", "extraction", "login", "action", "validation", "file_download", "goto_url"}
)
_BLOCK_WRAPPER_TYPES = frozenset({"for_loop", "while_loop", "conditional", "workflow_trigger"})
# A code block drives the page, so a CAPTCHA it reports is the site's even though it is not a browser-agent block.
_PAGE_DRIVING_CODE_BLOCK_TYPES = frozenset({"code"})
# Our own code writes these before a task starts, so no browser agent wrote the text that follows.
_PRE_TASK_FAILURE_PREFIXES = (
    "failed to download file from google drive",
    "setup workflow failed",
    "failed to initialize workflow run context",
)
# Run setup and context initialization fail before any page loads; their reasons are credential-vault and API errors.
# The "failed to send" entries name their producer, because a task's own reason can open with a bare "Failed to send".
_NON_SITE_FAILURE_PREFIXES = (
    *_PRE_TASK_FAILURE_PREFIXES,
    "google sheets ",
    "reconnect the google account",
    "http request failed",
    "failed to send human interaction email",
    "failed to send webhook",
    "failed to send downloaded file",
    "failed to upload file to ",
    "1password ",
    "bitwarden ",
    "azure error:",
)
# Wording only our own LLM failure paths produce: every LLMProviderError message names "LLMProvider" whatever the
# cause class, and the max-steps summary says "LLM service errors".
_LLM_ORIGIN_TEXT_MARKERS = ("llm rate limit", "llmprovider", "ratelimiterror", "llm service error")


def _non_agent_block_type(reason: str) -> tuple[str | None, str]:
    """The first prefixed block type that is neither a browser agent nor a wrapper, and the reason it prefixes."""
    while prefix := _BLOCK_FAILURE_PREFIX_RE.match(reason):
        block_type = prefix.group(1)
        if block_type not in _BROWSER_AGENT_BLOCK_TYPES and block_type not in _BLOCK_WRAPPER_TYPES:
            return block_type, reason
        reason = reason[prefix.end() :]
    return None, reason.lstrip()


def _is_non_site_failure(reason: str) -> bool:
    block_type, inner_reason = _non_agent_block_type(reason)
    return block_type is not None or inner_reason.startswith(_NON_SITE_FAILURE_PREFIXES)


# Narrower than _is_non_site_failure, because it removes an anti-bot label instead of adding one. A task's own
# failure reason can start with "Failed to send" in the model's words, and the task block retry loop reads it.
def _is_pageless_failure(reason: str) -> bool:
    block_type, inner_reason = _non_agent_block_type(reason)
    if block_type is not None:
        return block_type not in _PAGE_DRIVING_CODE_BLOCK_TYPES
    return inner_reason.startswith(_PRE_TASK_FAILURE_PREFIXES)


def _proxy_transport_evidence(reason: str, exception: Exception | None) -> str | None:
    """The evidence source when a proxy transport error ended the navigation, else None."""
    if isinstance(exception, FailedToNavigateToUrl) and exception.nav_error_code:
        return "code_level" if exception.nav_error_code in PROXY_TRANSPORT_NAV_ERRORS else None
    if _NO_ADDRESS_RECORD_MARKER_LOWER in reason:
        return None
    driver_message = _NAV_FAILURE_DRIVER_MESSAGE_RE.search(reason)
    if driver_message is None:
        return None
    driver_text = _URL_RE.sub(" ", driver_message.group(1))
    return "keyword_match" if any(code in driver_text for code in _PROXY_TRANSPORT_CODES_LOWER) else None


def is_captcha_solve_failure(exception_name: str | None) -> bool:
    """Whether an action result's recorded exception type is a CaptchaSolveError.

    Subclasses are walked at call time because solver integrations register their failures (loop breaker,
    timeout) as subclasses when imported, and an action result keeps only the class name.
    """
    if not exception_name:
        return False
    pending: list[type[CaptchaSolveError]] = [CaptchaSolveError]
    while pending:
        error_type = pending.pop()
        if error_type.__name__ == exception_name:
            return True
        pending.extend(error_type.__subclasses__())
    return False


def classify_from_failure_reason(
    failure_reason: str | None,
    exception: Exception | None = None,
    fallback_to_unknown: bool = False,
    exception_name: str | None = None,
    unsolved_captcha_exception: str | None = None,
) -> list[dict] | None:
    """Classify failure from failure_reason text and/or exception type.

    Returns list of categories sorted by confidence, or None if no classification.

    ``exception_name`` classifies from a bare exception class name when the instance is
    unavailable — e.g. a Temporal activity failure whose cause type only crosses the
    serialization boundary as a string. Ignored when ``exception`` is provided.

    ``unsolved_captcha_exception`` names a captcha-solve failure the run was still stuck on when
    it ended. It outranks a BROWSER_ERROR: a browser lost while the run is blocked by a captcha
    only reports how the run stopped, not why it failed.

    When ``fallback_to_unknown`` is True and no keywords match, returns a single
    UNKNOWN category instead of None.  Use True for paths that are *always* failures
    (exception, max_steps, max_retries).  Use False (the default) for terminate paths
    where the absence of a classification may simply mean the termination was
    user-guided / expected.

    Categories (17):
        ANTI_BOT_DETECTION, PROXY_ERROR, BROWSER_ERROR, NAVIGATION_FAILURE,
        PAGE_LOAD_TIMEOUT, ELEMENT_STATE_TIMEOUT, AUTH_FAILURE, LLM_ERROR, CREDENTIAL_ERROR,
        DATA_EXTRACTION_FAILURE, ELEMENT_NOT_FOUND, WRONG_PAGE_STATE,
        MAX_STEPS_EXCEEDED, LLM_REASONING_ERROR, INFRASTRUCTURE_ERROR,
        PARAMETER_BINDING_ERROR, UNKNOWN
    """
    if not failure_reason and not exception and not exception_name:
        return None

    reason = (failure_reason or "").lower()
    auth_scan_reason = _SELECTOR_PAYLOAD_RE.sub(" ", reason)
    exc_name = type(exception).__name__ if exception else (exception_name or "")

    categories: list[dict] = []

    # Bot detection / CAPTCHA — use specific phrases to avoid false positives
    _auth_context_keywords = ["login", "auth", "password", "permission", "credential"]
    _has_auth_context = any(kw in auth_scan_reason for kw in _auth_context_keywords)
    _antibot_keywords = [
        "captcha",
        "cloudflare",
        "turnstile",
        "bot detect",
        "bot block",
        "ip block",
        "request block",
        "anti-bot",
        "human verification",
        "verify you are human",
    ]
    # "access denied" is ambiguous: it can be bot blocking OR auth failure.
    # Only treat it as bot detection when there are no auth-related keywords nearby.
    # Note: in Skyvern's context, failure_reason is LLM-generated from page observations,
    # so RBAC-style messages like "Access denied: insufficient privileges" are unlikely.
    # If this becomes a false-positive source, consider further narrowing (e.g. requiring
    # "access denied" appears without ANY qualifier, or adding more exclusion keywords).
    if not _has_auth_context:
        _antibot_keywords.append("access denied")

    _is_llm_exception = any(kw in exc_name for kw in ["LLM", "APIError", "RateLimit"])
    _has_llm_origin_text = any(marker in reason for marker in _LLM_ORIGIN_TEXT_MARKERS)
    _is_llm_origin = _is_llm_exception or _has_llm_origin_text
    _is_non_site = _is_non_site_failure(reason)
    _has_throttle_wording = bool(_SITE_THROTTLE_RE.search(reason))
    # A 403 is ambiguous like "access denied": with auth context it is a permission failure, not a bot block.
    _is_site_block = (
        not _is_llm_origin
        and not _is_non_site
        and (_has_throttle_wording or (not _has_auth_context and bool(_SITE_FORBIDDEN_RE.search(reason))))
    )

    # "http_request block failed" contains "request block", so integration failures skip the keyword list.
    if not _is_pageless_failure(reason) and any(kw in reason for kw in _antibot_keywords):
        categories.append(
            {
                "category": FailureCategory.ANTI_BOT_DETECTION.value,
                "confidence_float": 0.7,
                "reasoning": "Keywords matched in failure reason",
                # Provenance marker: a keyword match is not positive challenge
                # evidence, so evidence-gated consumers must not assert on it.
                "evidence_source": "keyword_only",
            }
        )
    elif _is_site_block:
        categories.append(
            {
                "category": FailureCategory.ANTI_BOT_DETECTION.value,
                "confidence_float": 0.7,
                "reasoning": "Site rate-limit or 403 wording in failure reason",
                "evidence_source": "keyword_only",
                # Lets runtime consumers keep treating this entry as they did before it was anti-bot.
                "reason_code": SITE_THROTTLE_REASON_CODE,
            }
        )
    # An integration or third-party API throttled our call: not our infrastructure, the model, or bot protection.
    elif _has_throttle_wording and _is_non_site and not _is_llm_origin:
        categories.append(
            {
                "category": FailureCategory.WEBSITE_ERROR.value,
                "confidence_float": 0.7,
                "reasoning": "External service rate-limit wording in an integration failure reason",
                "evidence_source": "keyword_match",
                "reason_code": EXTERNAL_SERVICE_THROTTLE_REASON_CODE,
            }
        )

    # Proxy errors — check before browser errors so proxy failures don't fall into BROWSER_ERROR.
    # The exception name may contain "Browser" (e.g. UnknownErrorWhileCreatingBrowserContext) but the
    # root cause is proxy pool exhaustion or proxy connectivity failure.
    _proxy_exc_keywords = ["NoProxy", "ProxyError", "GetOutboundIP"]
    _proxy_reason_keywords = ["no proxy available", "proxy unavailable", "failed to get outbound ip"]
    _proxy_transport_evidence_source = _proxy_transport_evidence(reason, exception)
    _is_proxy_transport = _proxy_transport_evidence_source is not None
    if any(kw in exc_name for kw in _proxy_exc_keywords) or any(kw in reason for kw in _proxy_reason_keywords):
        categories.append(
            {
                "category": FailureCategory.PROXY_ERROR.value,
                "confidence_float": 0.9,
                "reasoning": f"Exception: {exc_name}" if exc_name else "Keywords matched",
            }
        )

    # The category names the failed hop, not who runs it: a customer-supplied proxy URL fails the same way.
    elif _is_proxy_transport:
        categories.append(
            {
                "category": FailureCategory.PROXY_ERROR.value,
                "confidence_float": 0.9,
                "reason_code": PROXY_TRANSPORT_FAILED_REASON_CODE,
                "evidence_source": _proxy_transport_evidence_source,
                "reasoning": "Browser reported a proxy transport error",
            }
        )

    # Browser errors — only match if not already classified as PROXY_ERROR above
    elif any(kw in exc_name for kw in ["Browser", "CDP", "TargetClosed"]) or any(
        kw in reason for kw in ["browser context closed", "page closed", "browser crash"]
    ):
        categories.append(
            {
                "category": FailureCategory.BROWSER_ERROR.value,
                "confidence_float": 0.9,
                "reasoning": f"Exception: {exc_name}" if exc_name else "Keywords matched",
            }
        )

    # Navigation failure
    if not _is_proxy_transport and (
        "FailedToNavigateToUrl" in exc_name
        or any(kw in reason for kw in ["failed to navigate", "404", "redirect loop"])
    ):
        categories.append(
            {
                "category": FailureCategory.NAVIGATION_FAILURE.value,
                "confidence_float": 0.9,
                "reasoning": f"Exception: {exc_name}" if "FailedToNavigate" in exc_name else "Keywords matched",
            }
        )

    # Infrastructure timeout — a Temporal activity / heartbeat timeout finalizes the run from
    # the worker layer (a stalled activity or worker interruption), not a site/page-load issue.
    # Classify before PAGE_LOAD_TIMEOUT so these don't masquerade as site slowness.
    _is_infra_timeout = bool(_INFRA_TIMEOUT_RE.search(reason))
    if _is_infra_timeout:
        categories.append(
            {
                "category": FailureCategory.INFRASTRUCTURE_ERROR.value,
                "confidence_float": 0.9,
                "reasoning": "Activity/heartbeat timeout finalized the run",
            }
        )

    # The secure CodeBlock runner was unreachable from the pool this run landed on, so the block
    # failed closed without executing any user code. That is a deploy-topology fault.
    if "secure codeblock runner is unavailable" in reason:
        categories.append(
            {
                "category": FailureCategory.INFRASTRUCTURE_ERROR.value,
                "confidence_float": 0.95,
                "reason_code": "secure_codeblock_runner_unavailable",
                "reasoning": "Secure CodeBlock runner was unreachable",
            }
        )

    # The framed relay can exhaust the child address-space limit while it receives inputs,
    # before executing the block. Its message is distinct from a user-code memory limit.
    if "codeblock inputs exhausted the sandbox memory limit before the block started" in reason:
        categories.append(
            {
                "category": FailureCategory.INFRASTRUCTURE_ERROR.value,
                "confidence_float": 0.95,
                "reason_code": "secure_codeblock_input_memory_limit",
                "reasoning": "Secure CodeBlock sandbox ran out of memory before executing the block",
            }
        )

    # The runner's fail-closed message for its internal faults (protocol errors, handshake
    # failures, runner-side exceptions); user code cannot author this literal.
    if "secure codeblock runner failed before completing" in reason:
        categories.append(
            {
                "category": FailureCategory.INFRASTRUCTURE_ERROR.value,
                "confidence_float": 0.95,
                "reason_code": "secure_codeblock_runner_internal",
                "reasoning": "Secure CodeBlock runner failed internally before completing",
            }
        )

    # The sandbox child died without delivering a result. Usually a pod/deploy fault (child OOM
    # and blocked operations get their own codes first), but user code can still self-terminate
    # the interpreter, so confidence stays below the unambiguous runner arms.
    if "secure codeblock sandbox process exited" in reason:
        categories.append(
            {
                "category": FailureCategory.INFRASTRUCTURE_ERROR.value,
                "confidence_float": 0.6,
                "reason_code": "secure_codeblock_sandbox_exited",
                "reasoning": "Secure CodeBlock sandbox child process died before completing",
            }
        )

    # The worker process running the run died mid-flight (container restart; in practice an OOM
    # kill) and its replacement failed the orphaned activity with this typed cause. Keyed on the cause,
    # never on reason text, so a reason that merely quotes the wording is not pinned on the worker.
    if exc_name == WORKER_CONTAINER_RESTARTED_REASON_CODE:
        categories.append(
            {
                "category": FailureCategory.INFRASTRUCTURE_ERROR.value,
                "confidence_float": 0.95,
                "reason_code": WORKER_CONTAINER_RESTARTED_REASON_CODE,
                "reasoning": "Worker container restarted mid-run",
            }
        )

    # Runner-slot contention, not a fault; the distinct reason_code keeps it separable from
    # real runner failures in analytics.
    if "codeblock runner is already executing another codeblock" in reason:
        categories.append(
            {
                "category": FailureCategory.INFRASTRUCTURE_ERROR.value,
                "confidence_float": 0.9,
                "reason_code": "secure_codeblock_runner_busy",
                "reasoning": "Secure CodeBlock runner was busy with another CodeBlock",
            }
        )

    _is_timeout = "Timeout" in exc_name or "timeout" in reason
    _is_element_state_timeout = _is_timeout and bool(_ELEMENT_OPERATION_RE.search(reason))

    # Element-state timeout — a locator operation that never reached the state it waited for.
    if _is_element_state_timeout:
        categories.append(
            {
                "category": FailureCategory.ELEMENT_STATE_TIMEOUT.value,
                "confidence_float": 0.85,
                "reason_code": "locator_wait_for_timeout",
                "reasoning": "Locator operation timed out waiting for element state",
            }
        )

    # Page load timeout
    if _is_timeout and not _is_infra_timeout and not _is_element_state_timeout:
        categories.append(
            {
                "category": FailureCategory.PAGE_LOAD_TIMEOUT.value,
                "confidence_float": 0.8,
                "reasoning": f"Exception: {exc_name}" if "Timeout" in exc_name else "Timeout in failure reason",
            }
        )

    # Auth failure — also catches "access denied" when auth context is present. Selector payloads
    # are stripped above, so a genuine auth message keeps its signal even alongside a locator timeout.
    if any(kw in auth_scan_reason for kw in ["login fail", "authentication fail", "auth fail", "mfa", "password"]) or (
        "access denied" in auth_scan_reason and _has_auth_context
    ):
        categories.append(
            {
                "category": FailureCategory.AUTH_FAILURE.value,
                "confidence_float": 0.7,
                "reasoning": "Keywords matched",
            }
        )

    # Credential error
    if "Bitwarden" in exc_name or any(
        kw in reason
        for kw in [
            "credential not found",
            "missing credential",
            "username not found by key",
            "password not found by key",
            "secret not found by key",
        ]
    ):
        categories.append(
            {
                "category": FailureCategory.CREDENTIAL_ERROR.value,
                "confidence_float": 0.8,
                "reasoning": f"Exception: {exc_name}" if "Bitwarden" in exc_name else "Keywords matched",
            }
        )

    # LLM error
    # Free text says "rate limit" about a site's page as well, so it counts only with LLM-origin wording.
    if _is_llm_exception or ("rate limit" in reason and _has_llm_origin_text):
        categories.append(
            {
                "category": FailureCategory.LLM_ERROR.value,
                "confidence_float": 0.9,
                "reasoning": f"Exception: {exc_name}" if exc_name else "Keywords matched",
            }
        )

    # Scraping / data extraction failure
    if "ScrapingFailed" in exc_name or any(kw in reason for kw in ["scraping", "extraction fail", "empty extraction"]):
        categories.append(
            {
                "category": FailureCategory.DATA_EXTRACTION_FAILURE.value,
                "confidence_float": 0.7,
                "reasoning": f"Exception: {exc_name}" if "Scraping" in exc_name else "Keywords matched",
            }
        )

    # Element not found
    if "ElementNotFound" in exc_name or any(kw in reason for kw in ["element not found", "no matching element"]):
        categories.append(
            {
                "category": FailureCategory.ELEMENT_NOT_FOUND.value,
                "confidence_float": 0.8,
                "reasoning": f"Exception: {exc_name}" if "ElementNotFound" in exc_name else "Keywords matched",
            }
        )

    # Wrong page state
    if any(kw in reason for kw in ["unexpected page", "wrong page", "blank page"]):
        categories.append(
            {
                "category": FailureCategory.WRONG_PAGE_STATE.value,
                "confidence_float": 0.6,
                "reasoning": "Keywords matched",
            }
        )

    # Max steps exceeded
    if any(kw in reason for kw in ["max steps", "maximum steps", "max number of", "step limit"]):
        categories.append(
            {
                "category": FailureCategory.MAX_STEPS_EXCEEDED.value,
                "confidence_float": 0.9,
                "reasoning": "Keywords matched",
            }
        )

    # LLM reasoning error (wrong action, hallucination)
    if any(kw in reason for kw in ["wrong action", "invalid action", "hallucin"]):
        categories.append(
            {
                "category": FailureCategory.LLM_REASONING_ERROR.value,
                "confidence_float": 0.6,
                "reasoning": "Keywords matched",
            }
        )

    # Internal configuration mismatch — not a site/selector failure.
    _param_binding_keywords = [
        "should have already been set through workflow run parameters",
        "should have already been set through workflow run context init",
        "pre-run invariant: workflow_definition and persisted parameter rows disagree",
    ]
    if any(kw in reason for kw in _param_binding_keywords):
        categories.append(
            {
                "category": FailureCategory.PARAMETER_BINDING_ERROR.value,
                "confidence_float": 0.95,
                "reasoning": "Keywords matched",
            }
        )

    if unsolved_captcha_exception and any(
        category["category"] == FailureCategory.BROWSER_ERROR.value for category in categories
    ):
        categories.append(
            {
                "category": FailureCategory.ANTI_BOT_DETECTION.value,
                "confidence_float": 0.95,
                "reason_code": UNSOLVED_CAPTCHA_BEFORE_BROWSER_LOSS_REASON_CODE,
                "reasoning": f"Exception: {unsolved_captcha_exception} before the browser was lost",
                "evidence_source": "exception_type",
            }
        )

    if not categories:
        if fallback_to_unknown:
            return [
                {
                    "category": FailureCategory.UNKNOWN.value,
                    "confidence_float": 0.5,
                    "reasoning": "No keyword match found",
                }
            ]
        return None

    # Stamp provenance from classifier-owned fields; raw failure text is not a provenance code.
    # Explicit sources (including anti-bot keyword_only) take priority over bounded reason codes.
    for category in categories:
        explicit = category.get("evidence_source")
        if isinstance(explicit, str) and explicit in _EVIDENCE_SOURCES:
            continue
        if _bounded_reason_code(category):
            category["evidence_source"] = "reason_code"
        elif str(category.get("reasoning", "")).startswith("Exception:"):
            category["evidence_source"] = "exception_type"
        else:
            category["evidence_source"] = "keyword_match"

    # Sort by confidence descending
    categories.sort(key=lambda x: x["confidence_float"], reverse=True)
    return categories


# ── Infra-failure attribution (SKY-16588) ─────────────────────────────────────
# Derives one bounded internal attribution document from the typed failure_category
# above; introduces no second classifier. Persisted to workflow_runs.failure_attribution
# for internal warehouse analysis only — never exposed to customers, never carrying raw
# failure text.

# Bump when the taxonomy or the category->component mapping below changes, so a frozen
# coverage baseline stays reproducible per classifier_version.
CLASSIFIER_VERSION = 8
FAILURE_ATTRIBUTION_SCHEMA_VERSION = 1

# Bounded sentinels — neither is an infra component id.
UNATTRIBUTED = "unattributed"  # classifier ran and abstained
NON_INFRA = "non_infra"  # positive evidence the outcome is site/agent/customer-owned

# A component is asserted only above this primary-category confidence; weaker signals abstain.
_MIN_ATTRIBUTION_CONFIDENCE = 0.7

# Shared with typed run emitters; dynamic/user-defined categories remain excluded.
_FAILURE_CATEGORY_LITERALS = frozenset(category.value for category in FailureCategory)

# Bounded reason codes emitted by out-of-module terminal producers. Defined here as the single
# source of truth so a producer and this allowlist cannot drift: the browser-lease seam
# (skyvern/forge/sdk/workflow/service.py::_browser_lease_failure_category) imports these.
BROWSER_SESSION_CLOSED_REASON_CODE = "browser_session_closed"
BROWSER_SESSION_STARTUP_TIMEOUT_REASON_CODE = "browser_session_startup_timeout"
BROWSER_SESSION_EXPIRED_BEFORE_RUN_REASON_CODE = "browser_session_expired_before_run"
PROXY_TRANSPORT_FAILED_REASON_CODE = "proxy_transport_failed"
WORKER_CONTAINER_RESTARTED_REASON_CODE = "worker_container_restarted"
UNSOLVED_CAPTCHA_BEFORE_BROWSER_LOSS_REASON_CODE = "unsolved_captcha_before_browser_loss"
SITE_THROTTLE_REASON_CODE = "site_throttle"
EXTERNAL_SERVICE_THROTTLE_REASON_CODE = "external_service_throttle"

# ANTI_BOT_DETECTION entries carrying one of these are labels only: runtime consumers must treat the
# failure exactly as they did before it was labeled anti-bot. Code that acts on a failure_category it
# reads (or hands to a model) must go through without_output_only_anti_bot or skip these entries.
OUTPUT_ONLY_ANTI_BOT_REASON_CODES = frozenset(
    {UNSOLVED_CAPTCHA_BEFORE_BROWSER_LOSS_REASON_CODE, SITE_THROTTLE_REASON_CODE}
)


def is_output_only_anti_bot_entry(category: object) -> bool:
    return (
        isinstance(category, dict)
        and category.get("category") == FailureCategory.ANTI_BOT_DETECTION.value
        and category.get("reason_code") in OUTPUT_ONLY_ANTI_BOT_REASON_CODES
    )


def without_output_only_anti_bot_categories(categories: list) -> list:
    return [category for category in categories if not is_output_only_anti_bot_entry(category)]


def without_output_only_anti_bot(value: Any) -> Any:
    """``value`` with output-only anti-bot entries removed from every ``failure_category`` list in it; customer
    data shaped like a category elsewhere is left alone, and a value with no marked entry is returned as is."""
    return _scrub_output_only_anti_bot(value) if _has_output_only_anti_bot(value) else value


# Iterative and copy-free: every registered block output pays this walk, and almost none carry a marked entry.
def _has_output_only_anti_bot(value: Any) -> bool:
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, dict):
            categories = item.get("failure_category")
            if isinstance(categories, list) and any(is_output_only_anti_bot_entry(c) for c in categories):
                return True
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
    return False


def _scrub_output_only_anti_bot(value: Any) -> Any:
    if isinstance(value, list):
        return [_scrub_output_only_anti_bot(item) for item in value]
    if isinstance(value, dict):
        return {
            key: without_output_only_anti_bot_categories(item)
            if key == "failure_category" and isinstance(item, list)
            else _scrub_output_only_anti_bot(item)
            for key, item in value.items()
        }
    return value


# The only reason_code literals persisted attribution recognizes; unknown values are dropped,
# not copied. secure_codeblock_*/locator_wait_for_timeout/site_throttle/external_service_throttle are emitted by
# classify_from_failure_reason above (same file); the browser-session codes are emitted by the browser-lease producer.
_REASON_CODE_LITERALS = frozenset(
    {
        "secure_codeblock_runner_unavailable",
        "secure_codeblock_input_memory_limit",
        "secure_codeblock_runner_internal",
        "secure_codeblock_sandbox_exited",
        "secure_codeblock_runner_busy",
        "locator_wait_for_timeout",
        BROWSER_SESSION_CLOSED_REASON_CODE,
        BROWSER_SESSION_STARTUP_TIMEOUT_REASON_CODE,
        BROWSER_SESSION_EXPIRED_BEFORE_RUN_REASON_CODE,
        PROXY_TRANSPORT_FAILED_REASON_CODE,
        SITE_THROTTLE_REASON_CODE,
        EXTERNAL_SERVICE_THROTTLE_REASON_CODE,
        UNSOLVED_CAPTCHA_BEFORE_BROWSER_LOSS_REASON_CODE,
        WORKER_CONTAINER_RESTARTED_REASON_CODE,
    }
)

# Categories whose evidence is a typed exception / bounded reason_code map to an owning
# infra component. INFRASTRUCTURE_ERROR is resolved separately via its reason_code.
# PARAMETER_BINDING_ERROR is an internal configuration mismatch, owned by the worker (v1).
_INFRA_COMPONENT_BY_CATEGORY = {
    "PROXY_ERROR": "proxy",
    "BROWSER_ERROR": "browser",
    "LLM_ERROR": "llm",
    "PARAMETER_BINDING_ERROR": "worker",
}

# Categories that positively indicate a site/agent/customer-owned outcome rather than
# infrastructure, mapping to the explicit non_infra sentinel (never a component).
_NON_INFRA_CATEGORIES = {
    "MAX_STEPS_EXCEEDED",
    "BUDGET_EXHAUSTED",
    "WEBSITE_ERROR",
    "ELEMENT_NOT_FOUND",
    "DATA_EXTRACTION_FAILURE",
    "LLM_REASONING_ERROR",
    "WRONG_PAGE_STATE",
    "BROWSER_SESSION_EXPIRED",
}


def _safe_confidence(primary: dict) -> float:
    """Confidence as a finite float; malformed/None/non-finite values abstain at 0.0.

    Terminal finalization must never fail because a historical row carried a bad value.
    Non-finite values (``nan``/``inf``/``-inf``) are coerced to 0.0: NaN would otherwise
    slip past the ``< _MIN_ATTRIBUTION_CONFIDENCE`` threshold (every comparison with NaN is
    False) and serialize as invalid JSON that Postgres rejects under strict ``allow_nan=False``.
    """
    raw = primary.get("confidence_float", 0.0)
    # bool is an int subclass, so float(True) == 1.0 would otherwise pass as a valid score.
    if isinstance(raw, bool):
        return 0.0
    try:
        value = float(raw)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    # A confidence is a probability-like score: a finite value in [0, 1]. Anything else
    # (nan/inf, a count like 2, a huge float) is malformed and abstains at 0.0 rather than
    # persisting a corrupt score or letting an out-of-range value clear the attribution threshold.
    if not math.isfinite(value) or not 0.0 <= value <= 1.0:
        return 0.0
    return value


def _bounded_category(primary: dict) -> str | None:
    """The primary category only if it is a known string literal; otherwise None (dropped).

    A non-string value (list/dict is unhashable, or any other type) is dropped without a
    membership lookup, so this cannot raise ``TypeError`` and terminal derivation never fails.
    """
    category = primary.get("category")
    return category if isinstance(category, str) and category in _FAILURE_CATEGORY_LITERALS else None


def _bounded_reason_code(primary: dict) -> str | None:
    """The reason_code only if it is a known string literal; otherwise None (dropped)."""
    reason_code = primary.get("reason_code")
    return reason_code if isinstance(reason_code, str) and reason_code in _REASON_CODE_LITERALS else None


# Bounded provenance codes for how the primary category was determined. keyword_match is
# reserved for categories a keyword scan actually matched; code_level is a category emitted
# directly by typed producers (e.g. Task V3 BUDGET_EXHAUSTED, _llm_error_category) with no
# keyword/exception/reason_code evidence — never mislabel those as keyword_match.
_EVIDENCE_SOURCES = frozenset({"none", "keyword_only", "keyword_match", "exception_type", "reason_code", "code_level"})


def _attribution_evidence_source(primary: dict) -> str:
    """Bounded provenance code for the primary category — never raw reasoning text.

    A no-match UNKNOWN (or an unrecognized category) has no positive signal, so it is ``none``.
    An explicit bounded ``evidence_source`` stamped by a producer wins. Otherwise the source is
    inferred: a bounded reason code, an ``Exception:`` reasoning, or a keyword-scan reasoning
    (classify_from_failure_reason marks keyword matches). A directly-emitted typed category with
    none of those is ``code_level`` — not ``keyword_match``, since no keyword scan occurred.
    """
    category = _bounded_category(primary)
    if category is None or category == "UNKNOWN":
        return "none"
    explicit = primary.get("evidence_source")
    if isinstance(explicit, str) and explicit in _EVIDENCE_SOURCES:
        return explicit
    if _bounded_reason_code(primary):
        return "reason_code"
    reasoning = str(primary.get("reasoning") or "")
    if reasoning.startswith("Exception:"):
        return "exception_type"
    if "keyword" in reasoning.lower():
        return "keyword_match"
    return "code_level"


def _primary_infra_component(primary: dict | None) -> str:
    if primary is None:
        return UNATTRIBUTED
    # A bare keyword match is not positive evidence of a component (or of non_infra).
    if primary.get("evidence_source") == "keyword_only":
        return UNATTRIBUTED
    if _safe_confidence(primary) < _MIN_ATTRIBUTION_CONFIDENCE:
        return UNATTRIBUTED
    category = _bounded_category(primary)
    if category is None:
        return UNATTRIBUTED
    if category == "INFRASTRUCTURE_ERROR":
        reason_code = _bounded_reason_code(primary) or ""
        return "codeblock" if reason_code.startswith("secure_codeblock") else "worker"
    if category in _INFRA_COMPONENT_BY_CATEGORY:
        return _INFRA_COMPONENT_BY_CATEGORY[category]
    if category in _NON_INFRA_CATEGORIES:
        return NON_INFRA
    return UNATTRIBUTED


def derive_failure_attribution(failure_category: list[dict] | None) -> dict[str, Any]:
    """Build the bounded internal attribution document for one non-success terminal run.

    Derives from the already-typed ``failure_category`` (highest-confidence entry first);
    introduces no second classifier. Callers persist this only for non-success terminal
    runs — completed/successful runs stay ``NULL``. Every field is a bounded code: the
    category and reason_code are dropped unless they match a known literal, so raw or
    dynamic text cannot be persisted by construction. Malformed rows (non-dict entry,
    bad confidence) abstain safely; this never raises.
    """
    try:
        # A historical/producer value may be a non-list (object or scalar); indexing it would
        # raise inside the terminal status transaction. Only a non-empty list is subscripted;
        # anything else abstains safely.
        primary = failure_category[0] if isinstance(failure_category, list) and failure_category else None
        if not isinstance(primary, dict):
            primary = None
        document: dict[str, Any] = {
            "schema_version": FAILURE_ATTRIBUTION_SCHEMA_VERSION,
            "classifier_version": CLASSIFIER_VERSION,
            "failure_category": _bounded_category(primary) if primary else None,
            "primary_infra_component": _primary_infra_component(primary),
            "evidence_source": _attribution_evidence_source(primary) if primary else "none",
            "heuristic_confidence": _safe_confidence(primary) if primary else 0.0,
        }
        reason_code = _bounded_reason_code(primary) if primary else None
        if reason_code:
            document["reason_code"] = reason_code
        return document
    except Exception:
        # Never raise inside the terminal status transaction. The bounded guards above handle
        # every known malformed shape and produce the most specific document; this catch-all
        # makes the never-raises contract true by construction for any unforeseen shape,
        # abstaining rather than blocking the run's terminal status write. The abstain document
        # is byte-identical to a legitimate no-category run, so log here to keep an unforeseen
        # shape visible instead of silently degrading attribution.
        LOG.exception("Failed to derive failure attribution; abstaining")
        return _abstain_document()


def _abstain_document() -> dict[str, Any]:
    """The bounded document for a run the classifier cannot attribute (or a malformed input)."""
    return {
        "schema_version": FAILURE_ATTRIBUTION_SCHEMA_VERSION,
        "classifier_version": CLASSIFIER_VERSION,
        "failure_category": None,
        "primary_infra_component": UNATTRIBUTED,
        "evidence_source": "none",
        "heuristic_confidence": 0.0,
    }
