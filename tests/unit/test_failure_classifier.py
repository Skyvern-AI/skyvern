from __future__ import annotations

import pytest

from skyvern.forge.failure_classifier import classify_from_failure_reason


class NoProxyAvailable(Exception):
    pass


class UnknownErrorWhileCreatingBrowserContext(Exception):
    pass


class ProxyErrorOccurred(Exception):
    pass

    def test_get_outbound_ip_failed_exception_is_proxy_not_browser(self) -> None:
        """GetOutboundIPFailed should be PROXY_ERROR, not BROWSER_ERROR."""

        class GetOutboundIPFailed(Exception):
            pass

        result = classify_from_failure_reason(
            "Failed to create browser context for dynamic-browser (GetOutboundIPFailed). "
            "Failed to get outbound ip (proxy_network=joinmassive-isp-dedicated): "
            "https://checkip.amazonaws.com=ProxyError",
            exception=GetOutboundIPFailed(),
        )
        assert result is not None
        assert result[0]["category"] == "PROXY_ERROR"
        categories = [r["category"] for r in result]
        assert "BROWSER_ERROR" not in categories

    def test_get_outbound_ip_failed_reason_without_exception(self) -> None:
        """GetOutboundIPFailed in failure_reason text alone should yield PROXY_ERROR.

        The workflow run failure path calls classify_from_failure_reason without exception=,
        so the proxy classification must be driven by the failure_reason text.
        """
        reason = (
            "goto_url block failed. failure reason: Failed to create browser context for dynamic-browser "
            "(GetOutboundIPFailed). Failed to get outbound ip (proxy_network=joinmassive-isp-dedicated): "
            "https://checkip.amazonaws.com=ProxyError, https://ipinfo.io/ip=ProxyError"
        )
        result = classify_from_failure_reason(reason, fallback_to_unknown=True)
        assert result is not None
        assert result[0]["category"] == "PROXY_ERROR"
        categories = [r["category"] for r in result]
        assert "UNKNOWN" not in categories
        assert "BROWSER_ERROR" not in categories


class BrowserCrashError(Exception):
    pass


class CDPConnectionError(Exception):
    pass


class TargetClosedError(Exception):
    pass


class FailedToNavigateToUrl(Exception):
    pass


class BitwardenVaultError(Exception):
    pass


class LLMProviderError(Exception):
    pass


class RateLimitExceeded(Exception):
    pass


class ScrapingFailedError(Exception):
    pass


class ElementNotFoundError(Exception):
    pass


def _classify(
    reason: str | None,
    exception: Exception | None = None,
    *,
    fallback_to_unknown: bool = False,
) -> list[dict]:
    result = classify_from_failure_reason(reason, exception=exception, fallback_to_unknown=fallback_to_unknown)
    assert result is not None
    return result


def _categories_for(
    reason: str | None,
    exception: Exception | None = None,
    *,
    fallback_to_unknown: bool = False,
) -> list[str]:
    return [result["category"] for result in _classify(reason, exception, fallback_to_unknown=fallback_to_unknown)]


def test_none_input_returns_none() -> None:
    assert classify_from_failure_reason(None) is None


def test_empty_string_returns_none() -> None:
    assert classify_from_failure_reason("") is None


@pytest.mark.parametrize(
    ("reason", "expected_categories"),
    [
        pytest.param(
            "Reached the max steps because captcha kept appearing",
            ["ANTI_BOT_DETECTION", "MAX_STEPS_EXCEEDED"],
            id="captcha-max-steps",
        ),
        pytest.param(
            "Failed to navigate after redirect loop; page load timeout on wrong page loaded",
            ["NAVIGATION_FAILURE", "PAGE_LOAD_TIMEOUT", "WRONG_PAGE_STATE"],
            id="navigation-timeout-state",
        ),
        pytest.param(
            "Login failed with invalid credentials; credential not found; LLM rate limit",
            ["AUTH_FAILURE", "CREDENTIAL_ERROR", "LLM_ERROR"],
            id="auth-credential-llm",
        ),
        pytest.param(
            "Scraping failed; target element not found; agent took wrong action; "
            "Value should have already been set through workflow run parameters",
            [
                "DATA_EXTRACTION_FAILURE",
                "ELEMENT_NOT_FOUND",
                "LLM_REASONING_ERROR",
                "PARAMETER_BINDING_ERROR",
            ],
            id="extraction-element-reasoning-params",
        ),
    ],
)
def test_keyword_category_matrix(reason: str, expected_categories: list[str]) -> None:
    categories = _categories_for(reason)

    for category in expected_categories:
        assert category in categories


def test_exception_category_matrix() -> None:
    cases = [
        (
            "no-proxy-exception",
            "Failed to create browser context for dynamic-browser (NoProxyAvailable). No proxy available",
            NoProxyAvailable(),
            ["PROXY_ERROR"],
            ["BROWSER_ERROR"],
        ),
        (
            "proxy-reason-over-browser-exception",
            "No proxy available, proxy_location=RESIDENTIAL, retry_count=15",
            UnknownErrorWhileCreatingBrowserContext(),
            ["PROXY_ERROR"],
            ["BROWSER_ERROR"],
        ),
        ("proxy-exception", "proxy connection failed", ProxyErrorOccurred(), ["PROXY_ERROR"], []),
        ("browser-exception-without-reason", None, BrowserCrashError(), ["BROWSER_ERROR"], []),
        ("cdp-exception", "connection lost", CDPConnectionError(), ["BROWSER_ERROR"], []),
        ("target-closed-exception", "target gone", TargetClosedError(), ["BROWSER_ERROR"], []),
        ("navigation-exception", "url error", FailedToNavigateToUrl(), ["NAVIGATION_FAILURE"], []),
        ("timeout-exception", "waiting for page", TimeoutError(), ["PAGE_LOAD_TIMEOUT"], []),
        ("credential-exception", "vault error", BitwardenVaultError(), ["CREDENTIAL_ERROR"], []),
        ("llm-exception", "provider down", LLMProviderError(), ["LLM_ERROR"], []),
        ("rate-limit-exception", "too many requests", RateLimitExceeded(), ["LLM_ERROR"], []),
        ("scraping-exception", "page error", ScrapingFailedError(), ["DATA_EXTRACTION_FAILURE"], []),
        ("element-exception", "missing", ElementNotFoundError(), ["ELEMENT_NOT_FOUND"], []),
    ]

    for case_id, reason, exception, expected_categories, unexpected_categories in cases:
        results = _classify(reason, exception=exception)
        categories = [result["category"] for result in results]

        for category in expected_categories:
            assert category in categories, case_id
        for category in unexpected_categories:
            assert category not in categories, case_id
        if exception:
            assert type(exception).__name__ in results[0]["reasoning"], case_id


def test_access_denied_with_auth_context_is_not_antibot() -> None:
    for reason in [
        "Access denied after login - user does not have permission",
        "The page shows 'Access Denied'. Unable to enter your password.",
    ]:
        categories = _categories_for(reason)

        assert "AUTH_FAILURE" in categories, reason
        assert "ANTI_BOT_DETECTION" not in categories, reason


def test_antibot_category_is_marked_keyword_only() -> None:
    categories = classify_from_failure_reason("Cloudflare turnstile challenge blocked the page after a timeout")

    assert categories is not None
    antibot = next(category for category in categories if category["category"] == "ANTI_BOT_DETECTION")
    assert antibot["evidence_source"] == "keyword_only"
    assert all(
        "evidence_source" not in category for category in categories if category["category"] != "ANTI_BOT_DETECTION"
    )


def test_broad_blocked_and_forbidden_do_not_match_antibot() -> None:
    for reason in ["UI element blocked by overlay", "403 Forbidden from auth endpoint"]:
        result = classify_from_failure_reason(reason)

        if result:
            assert "ANTI_BOT_DETECTION" not in [r["category"] for r in result], reason


def test_multiple_categories_are_sorted_by_confidence_descending() -> None:
    result = _classify("Reached the max steps because captcha kept appearing")

    assert len(result) >= 2
    assert [r["confidence_float"] for r in result] == sorted(
        [r["confidence_float"] for r in result],
        reverse=True,
    )


def test_unrecognized_with_fallback_returns_unknown() -> None:
    result = _classify("The Resume/CV field is required but empty", fallback_to_unknown=True)

    assert len(result) == 1
    assert result[0]["category"] == "UNKNOWN"
    assert result[0]["confidence_float"] == 0.5


def test_unrecognized_without_fallback_returns_none() -> None:
    assert classify_from_failure_reason("The Resume/CV field is required but empty") is None


def test_none_input_with_fallback_returns_none() -> None:
    assert classify_from_failure_reason(None, fallback_to_unknown=True) is None


def test_empty_input_with_fallback_returns_none() -> None:
    assert classify_from_failure_reason("", fallback_to_unknown=True) is None


def test_keyword_match_ignores_fallback() -> None:
    result_with = _classify("browser crash detected", fallback_to_unknown=True)
    result_without = _classify("browser crash detected", fallback_to_unknown=False)

    assert result_with[0]["category"] == result_without[0]["category"] == "BROWSER_ERROR"


def test_activity_heartbeat_timeout_classifies_as_infrastructure() -> None:
    reason = "Workflow run timed out: the workflow activity became unresponsive (activity heartbeat timeout)."
    categories = _categories_for(reason)

    assert categories[0] == "INFRASTRUCTURE_ERROR"
    # An infra-level activity timeout must not masquerade as site/page-load slowness.
    assert "PAGE_LOAD_TIMEOUT" not in categories


def test_generic_activity_timeout_classifies_as_infrastructure() -> None:
    reason = "Workflow run timed out: the workflow activity became unresponsive (activity timeout)."
    categories = _categories_for(reason)

    assert categories[0] == "INFRASTRUCTURE_ERROR"
    assert "PAGE_LOAD_TIMEOUT" not in categories


def test_page_load_timeout_still_classifies_without_activity_context() -> None:
    categories = _categories_for("Navigation timeout while waiting for page load")

    assert "PAGE_LOAD_TIMEOUT" in categories
    assert "INFRASTRUCTURE_ERROR" not in categories


def test_inactivity_timeout_is_not_infrastructure() -> None:
    # "inactivity timeout" is a session/page-level reason; a naive substring match on
    # "activity timeout" would wrongly reclassify it as INFRASTRUCTURE_ERROR and drop
    # PAGE_LOAD_TIMEOUT. The word-anchored match must leave it alone.
    categories = _categories_for("Session ended: inactivity timeout on the page")

    assert "INFRASTRUCTURE_ERROR" not in categories
    assert "PAGE_LOAD_TIMEOUT" in categories
