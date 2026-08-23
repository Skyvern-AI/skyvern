from __future__ import annotations

import re

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


def classify_from_failure_reason(
    failure_reason: str | None,
    exception: Exception | None = None,
    fallback_to_unknown: bool = False,
    exception_name: str | None = None,
) -> list[dict] | None:
    """Classify failure from failure_reason text and/or exception type.

    Returns list of categories sorted by confidence, or None if no classification.

    ``exception_name`` classifies from a bare exception class name when the instance is
    unavailable — e.g. a Temporal activity failure whose cause type only crosses the
    serialization boundary as a string. Ignored when ``exception`` is provided.

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

    if any(kw in reason for kw in _antibot_keywords):
        categories.append(
            {
                "category": "ANTI_BOT_DETECTION",
                "confidence_float": 0.7,
                "reasoning": "Keywords matched in failure reason",
                # Provenance marker: a keyword match is not positive challenge
                # evidence, so evidence-gated consumers must not assert on it.
                "evidence_source": "keyword_only",
            }
        )

    # Proxy errors — check before browser errors so proxy failures don't fall into BROWSER_ERROR.
    # The exception name may contain "Browser" (e.g. UnknownErrorWhileCreatingBrowserContext) but the
    # root cause is proxy pool exhaustion or proxy connectivity failure.
    _proxy_exc_keywords = ["NoProxy", "ProxyError", "GetOutboundIP"]
    _proxy_reason_keywords = ["no proxy available", "proxy unavailable", "failed to get outbound ip"]
    if any(kw in exc_name for kw in _proxy_exc_keywords) or any(kw in reason for kw in _proxy_reason_keywords):
        categories.append(
            {
                "category": "PROXY_ERROR",
                "confidence_float": 0.9,
                "reasoning": f"Exception: {exc_name}" if exc_name else "Keywords matched",
            }
        )

    # Browser errors — only match if not already classified as PROXY_ERROR above
    elif any(kw in exc_name for kw in ["Browser", "CDP", "TargetClosed"]) or any(
        kw in reason for kw in ["browser context closed", "page closed", "browser crash"]
    ):
        categories.append(
            {
                "category": "BROWSER_ERROR",
                "confidence_float": 0.9,
                "reasoning": f"Exception: {exc_name}" if exc_name else "Keywords matched",
            }
        )

    # Navigation failure
    if "FailedToNavigateToUrl" in exc_name or any(
        kw in reason for kw in ["failed to navigate", "404", "redirect loop"]
    ):
        categories.append(
            {
                "category": "NAVIGATION_FAILURE",
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
                "category": "INFRASTRUCTURE_ERROR",
                "confidence_float": 0.9,
                "reasoning": "Activity/heartbeat timeout finalized the run",
            }
        )

    # The secure CodeBlock runner was unreachable from the pool this run landed on, so the block
    # failed closed without executing any user code. That is a deploy-topology fault.
    if "secure codeblock runner is unavailable" in reason:
        categories.append(
            {
                "category": "INFRASTRUCTURE_ERROR",
                "confidence_float": 0.95,
                "reason_code": "secure_codeblock_runner_unavailable",
                "reasoning": "Secure CodeBlock runner was unreachable",
            }
        )

    # The runner's fail-closed message for its internal faults (protocol errors, handshake
    # failures, runner-side exceptions); user code cannot author this literal.
    if "secure codeblock runner failed before completing" in reason:
        categories.append(
            {
                "category": "INFRASTRUCTURE_ERROR",
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
                "category": "INFRASTRUCTURE_ERROR",
                "confidence_float": 0.6,
                "reason_code": "secure_codeblock_sandbox_exited",
                "reasoning": "Secure CodeBlock sandbox child process died before completing",
            }
        )

    # Runner-slot contention, not a fault; the distinct reason_code keeps it separable from
    # real runner failures in analytics.
    if "codeblock runner is already executing another codeblock" in reason:
        categories.append(
            {
                "category": "INFRASTRUCTURE_ERROR",
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
                "category": "ELEMENT_STATE_TIMEOUT",
                "confidence_float": 0.85,
                "reason_code": "locator_wait_for_timeout",
                "reasoning": "Locator operation timed out waiting for element state",
            }
        )

    # Page load timeout
    if _is_timeout and not _is_infra_timeout and not _is_element_state_timeout:
        categories.append(
            {
                "category": "PAGE_LOAD_TIMEOUT",
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
                "category": "AUTH_FAILURE",
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
                "category": "CREDENTIAL_ERROR",
                "confidence_float": 0.8,
                "reasoning": f"Exception: {exc_name}" if "Bitwarden" in exc_name else "Keywords matched",
            }
        )

    # LLM error
    if any(kw in exc_name for kw in ["LLM", "APIError", "RateLimit"]) or "rate limit" in reason:
        categories.append(
            {
                "category": "LLM_ERROR",
                "confidence_float": 0.9,
                "reasoning": f"Exception: {exc_name}" if exc_name else "Keywords matched",
            }
        )

    # Scraping / data extraction failure
    if "ScrapingFailed" in exc_name or any(kw in reason for kw in ["scraping", "extraction fail", "empty extraction"]):
        categories.append(
            {
                "category": "DATA_EXTRACTION_FAILURE",
                "confidence_float": 0.7,
                "reasoning": f"Exception: {exc_name}" if "Scraping" in exc_name else "Keywords matched",
            }
        )

    # Element not found
    if "ElementNotFound" in exc_name or any(kw in reason for kw in ["element not found", "no matching element"]):
        categories.append(
            {
                "category": "ELEMENT_NOT_FOUND",
                "confidence_float": 0.8,
                "reasoning": f"Exception: {exc_name}" if "ElementNotFound" in exc_name else "Keywords matched",
            }
        )

    # Wrong page state
    if any(kw in reason for kw in ["unexpected page", "wrong page", "blank page"]):
        categories.append(
            {
                "category": "WRONG_PAGE_STATE",
                "confidence_float": 0.6,
                "reasoning": "Keywords matched",
            }
        )

    # Max steps exceeded
    if any(kw in reason for kw in ["max steps", "maximum steps", "max number of", "step limit"]):
        categories.append(
            {
                "category": "MAX_STEPS_EXCEEDED",
                "confidence_float": 0.9,
                "reasoning": "Keywords matched",
            }
        )

    # LLM reasoning error (wrong action, hallucination)
    if any(kw in reason for kw in ["wrong action", "invalid action", "hallucin"]):
        categories.append(
            {
                "category": "LLM_REASONING_ERROR",
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
                "category": "PARAMETER_BINDING_ERROR",
                "confidence_float": 0.95,
                "reasoning": "Keywords matched",
            }
        )

    if not categories:
        if fallback_to_unknown:
            return [{"category": "UNKNOWN", "confidence_float": 0.5, "reasoning": "No keyword match found"}]
        return None

    # Sort by confidence descending
    categories.sort(key=lambda x: x["confidence_float"], reverse=True)
    return categories
