from __future__ import annotations


def classify_from_failure_reason(
    failure_reason: str | None,
    exception: Exception | None = None,
    fallback_to_unknown: bool = False,
) -> list[dict] | None:
    """Classify failure from failure_reason text and/or exception type.

    Returns list of categories sorted by confidence, or None if no classification.

    When ``fallback_to_unknown`` is True and no keywords match, returns a single
    UNKNOWN category instead of None.  Use True for paths that are *always* failures
    (exception, max_steps, max_retries).  Use False (the default) for terminate paths
    where the absence of a classification may simply mean the termination was
    user-guided / expected.

    Categories (16):
        ANTI_BOT_DETECTION, PROXY_ERROR, BROWSER_ERROR, NAVIGATION_FAILURE,
        PAGE_LOAD_TIMEOUT, AUTH_FAILURE, LLM_ERROR, CREDENTIAL_ERROR,
        DATA_EXTRACTION_FAILURE, ELEMENT_NOT_FOUND, WRONG_PAGE_STATE,
        MAX_STEPS_EXCEEDED, LLM_REASONING_ERROR, INFRASTRUCTURE_ERROR,
        PARAMETER_BINDING_ERROR, UNKNOWN
    """
    if not failure_reason and not exception:
        return None

    reason = (failure_reason or "").lower()
    exc_name = type(exception).__name__ if exception else ""

    categories: list[dict] = []

    # Bot detection / CAPTCHA — use specific phrases to avoid false positives
    _auth_context_keywords = ["login", "auth", "password", "permission", "credential"]
    _has_auth_context = any(kw in reason for kw in _auth_context_keywords)
    _antibot_keywords = [
        "captcha",
        "cloudflare",
        "bot detect",
        "bot block",
        "ip block",
        "request block",
        "anti-bot",
        "human verification",
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
            }
        )

    # Proxy errors — check before browser errors so proxy failures don't fall into BROWSER_ERROR.
    # The exception name may contain "Browser" (e.g. UnknownErrorWhileCreatingBrowserContext) but the
    # root cause is proxy pool exhaustion.
    _proxy_exc_keywords = ["NoProxy", "ProxyError"]
    _proxy_reason_keywords = ["no proxy available", "proxy unavailable"]
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

    # Page load timeout
    if "Timeout" in exc_name or "timeout" in reason:
        categories.append(
            {
                "category": "PAGE_LOAD_TIMEOUT",
                "confidence_float": 0.8,
                "reasoning": f"Exception: {exc_name}" if "Timeout" in exc_name else "Timeout in failure reason",
            }
        )

    # Auth failure — also catches "access denied" when auth context is present
    if any(kw in reason for kw in ["login fail", "authentication fail", "auth fail", "mfa", "password"]) or (
        "access denied" in reason and _has_auth_context
    ):
        categories.append(
            {
                "category": "AUTH_FAILURE",
                "confidence_float": 0.7,
                "reasoning": "Keywords matched",
            }
        )

    # Credential error
    if "Bitwarden" in exc_name or any(kw in reason for kw in ["credential not found", "missing credential"]):
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
