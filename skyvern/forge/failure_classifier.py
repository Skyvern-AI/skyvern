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

    Categories:
        ANTI_BOT_DETECTION, BROWSER_ERROR, NAVIGATION_FAILURE, PAGE_LOAD_TIMEOUT,
        AUTH_FAILURE, LLM_ERROR, CREDENTIAL_ERROR, DATA_EXTRACTION_FAILURE,
        ELEMENT_NOT_FOUND, WRONG_PAGE_STATE, MAX_STEPS_EXCEEDED,
        INFRASTRUCTURE_ERROR, UNKNOWN
    """
    if not failure_reason and not exception:
        return None

    reason = (failure_reason or "").lower()
    exc_name = type(exception).__name__ if exception else ""

    categories: list[dict] = []

    # Bot detection / CAPTCHA — use specific phrases to avoid false positives
    if any(
        kw in reason
        for kw in [
            "captcha",
            "cloudflare",
            "bot detect",
            "bot block",
            "ip block",
            "request block",
            "access denied by",
            "anti-bot",
            "human verification",
        ]
    ):
        categories.append(
            {
                "category": "ANTI_BOT_DETECTION",
                "confidence_float": 0.7,
                "reasoning": "Keywords matched in failure reason",
            }
        )

    # Browser errors
    if any(kw in exc_name for kw in ["Browser", "CDP", "TargetClosed"]) or any(
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

    # Auth failure
    if any(kw in reason for kw in ["login fail", "authentication fail", "auth fail", "mfa", "password"]):
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

    if not categories:
        if fallback_to_unknown:
            return [{"category": "UNKNOWN", "confidence_float": 0.5, "reasoning": "No keyword match found"}]
        return None

    # Sort by confidence descending
    categories.sort(key=lambda x: x["confidence_float"], reverse=True)
    return categories
