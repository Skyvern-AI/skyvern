from __future__ import annotations

import pytest

from skyvern.forge.failure_classifier import classify_from_failure_reason


class TestNoneAndEmptyInput:
    def test_none_input_returns_none(self) -> None:
        assert classify_from_failure_reason(None) is None

    def test_empty_string_returns_none(self) -> None:
        assert classify_from_failure_reason("") is None

    def test_none_with_none_exception_returns_none(self) -> None:
        assert classify_from_failure_reason(None, exception=None) is None

    def test_unrecognized_reason_returns_none(self) -> None:
        assert classify_from_failure_reason("something completely unrelated happened") is None


class TestAntiBotDetection:
    @pytest.mark.parametrize(
        "reason",
        [
            "Page blocked by captcha challenge",
            "Cloudflare protection detected",
            "Bot detected on the page",
            "IP blocked by server",
            "Request blocked due to suspicious activity",
            "Access denied by WAF",
            "Anti-bot system triggered",
            "Human verification required",
            "The current page shows an 'Access Denied' error",
            "Access Denied - website blocked the request",
        ],
    )
    def test_keywords_match(self, reason: str) -> None:
        result = classify_from_failure_reason(reason)
        assert result is not None
        categories = [r["category"] for r in result]
        assert "ANTI_BOT_DETECTION" in categories

    def test_access_denied_with_auth_context_is_not_antibot(self) -> None:
        """'Access denied' + auth keywords → AUTH_FAILURE, not ANTI_BOT_DETECTION."""
        result = classify_from_failure_reason("Access denied after login - user does not have permission")
        assert result is not None
        categories = [r["category"] for r in result]
        assert "AUTH_FAILURE" in categories
        assert "ANTI_BOT_DETECTION" not in categories

    def test_access_denied_with_password_context_is_not_antibot(self) -> None:
        """'Access denied' when password is mentioned → AUTH_FAILURE."""
        result = classify_from_failure_reason("The page shows 'Access Denied'. Unable to enter your password.")
        assert result is not None
        categories = [r["category"] for r in result]
        assert "AUTH_FAILURE" in categories
        assert "ANTI_BOT_DETECTION" not in categories

    def test_broad_blocked_does_not_match(self) -> None:
        """'blocked' alone should NOT trigger ANTI_BOT_DETECTION (narrowed keywords)."""
        result = classify_from_failure_reason("UI element blocked by overlay")
        if result:
            categories = [r["category"] for r in result]
            assert "ANTI_BOT_DETECTION" not in categories

    def test_broad_forbidden_does_not_match(self) -> None:
        result = classify_from_failure_reason("403 Forbidden from auth endpoint")
        if result:
            categories = [r["category"] for r in result]
            assert "ANTI_BOT_DETECTION" not in categories


class TestProxyError:
    def test_no_proxy_exception_is_proxy_not_browser(self) -> None:
        """NoProxyAvailable exception should be PROXY_ERROR, not BROWSER_ERROR."""

        class NoProxyAvailable(Exception):
            pass

        result = classify_from_failure_reason(
            "Failed to create browser context for dynamic-browser (NoProxyAvailable). No proxy available",
            exception=NoProxyAvailable(),
        )
        assert result is not None
        assert result[0]["category"] == "PROXY_ERROR"
        categories = [r["category"] for r in result]
        assert "BROWSER_ERROR" not in categories

    def test_unknown_error_creating_browser_context_with_proxy_reason(self) -> None:
        """UnknownErrorWhileCreatingBrowserContext caused by proxy should be PROXY_ERROR."""

        class UnknownErrorWhileCreatingBrowserContext(Exception):
            pass

        result = classify_from_failure_reason(
            "No proxy available, proxy_location=RESIDENTIAL, retry_count=15",
            exception=UnknownErrorWhileCreatingBrowserContext(),
        )
        assert result is not None
        categories = [r["category"] for r in result]
        assert "PROXY_ERROR" in categories
        assert "BROWSER_ERROR" not in categories

    def test_proxy_error_exception(self) -> None:
        class ProxyErrorOccurred(Exception):
            pass

        result = classify_from_failure_reason("proxy connection failed", exception=ProxyErrorOccurred())
        assert result is not None
        assert result[0]["category"] == "PROXY_ERROR"


class TestBrowserError:
    def test_keyword_browser_context_closed(self) -> None:
        result = classify_from_failure_reason("browser context closed unexpectedly")
        assert result is not None
        assert result[0]["category"] == "BROWSER_ERROR"

    def test_keyword_page_closed(self) -> None:
        result = classify_from_failure_reason("page closed before action completed")
        assert result is not None
        assert result[0]["category"] == "BROWSER_ERROR"

    def test_exception_type_browser(self) -> None:
        class BrowserCrashError(Exception):
            pass

        result = classify_from_failure_reason("something failed", exception=BrowserCrashError())
        assert result is not None
        assert result[0]["category"] == "BROWSER_ERROR"
        assert "BrowserCrashError" in result[0]["reasoning"]

    def test_exception_type_cdp(self) -> None:
        class CDPConnectionError(Exception):
            pass

        result = classify_from_failure_reason("connection lost", exception=CDPConnectionError())
        assert result is not None
        assert result[0]["category"] == "BROWSER_ERROR"

    def test_exception_type_target_closed(self) -> None:
        class TargetClosedError(Exception):
            pass

        result = classify_from_failure_reason("target gone", exception=TargetClosedError())
        assert result is not None
        assert result[0]["category"] == "BROWSER_ERROR"


class TestNavigationFailure:
    def test_keyword_failed_to_navigate(self) -> None:
        result = classify_from_failure_reason("Failed to navigate to the login page")
        assert result is not None
        categories = [r["category"] for r in result]
        assert "NAVIGATION_FAILURE" in categories

    def test_keyword_404(self) -> None:
        result = classify_from_failure_reason("Server returned 404 for the URL")
        assert result is not None
        categories = [r["category"] for r in result]
        assert "NAVIGATION_FAILURE" in categories

    def test_exception_type(self) -> None:
        class FailedToNavigateToUrl(Exception):
            pass

        result = classify_from_failure_reason("url error", exception=FailedToNavigateToUrl())
        assert result is not None
        assert result[0]["category"] == "NAVIGATION_FAILURE"
        assert "FailedToNavigateToUrl" in result[0]["reasoning"]


class TestPageLoadTimeout:
    def test_keyword_timeout(self) -> None:
        result = classify_from_failure_reason("Page load timeout after 30s")
        assert result is not None
        assert result[0]["category"] == "PAGE_LOAD_TIMEOUT"

    def test_exception_type_timeout(self) -> None:
        result = classify_from_failure_reason("waiting for page", exception=TimeoutError())
        assert result is not None
        assert result[0]["category"] == "PAGE_LOAD_TIMEOUT"


class TestAuthFailure:
    @pytest.mark.parametrize(
        "reason",
        [
            "Login failed with invalid credentials",
            "Authentication failed for user",
            "Auth failed - wrong token",
            "MFA verification required",
        ],
    )
    def test_keywords_match(self, reason: str) -> None:
        result = classify_from_failure_reason(reason)
        assert result is not None
        categories = [r["category"] for r in result]
        assert "AUTH_FAILURE" in categories


class TestCredentialError:
    def test_keyword_credential_not_found(self) -> None:
        result = classify_from_failure_reason("Credential not found in vault")
        assert result is not None
        categories = [r["category"] for r in result]
        assert "CREDENTIAL_ERROR" in categories

    def test_exception_type_bitwarden(self) -> None:
        class BitwardenVaultError(Exception):
            pass

        result = classify_from_failure_reason("vault error", exception=BitwardenVaultError())
        assert result is not None
        assert result[0]["category"] == "CREDENTIAL_ERROR"


class TestLLMError:
    def test_keyword_rate_limit(self) -> None:
        result = classify_from_failure_reason("LLM call hit rate limit")
        assert result is not None
        categories = [r["category"] for r in result]
        assert "LLM_ERROR" in categories

    def test_exception_type_llm(self) -> None:
        class LLMProviderError(Exception):
            pass

        result = classify_from_failure_reason("provider down", exception=LLMProviderError())
        assert result is not None
        assert result[0]["category"] == "LLM_ERROR"

    def test_exception_type_rate_limit(self) -> None:
        class RateLimitExceeded(Exception):
            pass

        result = classify_from_failure_reason("too many requests", exception=RateLimitExceeded())
        assert result is not None
        categories = [r["category"] for r in result]
        assert "LLM_ERROR" in categories


class TestDataExtractionFailure:
    def test_keyword_scraping(self) -> None:
        result = classify_from_failure_reason("Scraping failed on the results page")
        assert result is not None
        categories = [r["category"] for r in result]
        assert "DATA_EXTRACTION_FAILURE" in categories

    def test_exception_type_scraping(self) -> None:
        class ScrapingFailedError(Exception):
            pass

        result = classify_from_failure_reason("page error", exception=ScrapingFailedError())
        assert result is not None
        categories = [r["category"] for r in result]
        assert "DATA_EXTRACTION_FAILURE" in categories


class TestElementNotFound:
    def test_keyword(self) -> None:
        result = classify_from_failure_reason("Target element not found on the page")
        assert result is not None
        categories = [r["category"] for r in result]
        assert "ELEMENT_NOT_FOUND" in categories

    def test_exception_type(self) -> None:
        class ElementNotFoundError(Exception):
            pass

        result = classify_from_failure_reason("missing", exception=ElementNotFoundError())
        assert result is not None
        categories = [r["category"] for r in result]
        assert "ELEMENT_NOT_FOUND" in categories


class TestWrongPageState:
    @pytest.mark.parametrize("reason", ["Unexpected page after login", "Wrong page loaded", "Blank page detected"])
    def test_keywords_match(self, reason: str) -> None:
        result = classify_from_failure_reason(reason)
        assert result is not None
        categories = [r["category"] for r in result]
        assert "WRONG_PAGE_STATE" in categories


class TestMaxStepsExceeded:
    @pytest.mark.parametrize(
        "reason",
        [
            "Reached the max steps limit",
            "Maximum steps exceeded for this task",
            "Reached the max number of 25 steps",
            "Step limit reached",
        ],
    )
    def test_keywords_match(self, reason: str) -> None:
        result = classify_from_failure_reason(reason)
        assert result is not None
        categories = [r["category"] for r in result]
        assert "MAX_STEPS_EXCEEDED" in categories


class TestLLMReasoningError:
    @pytest.mark.parametrize("reason", ["Agent took wrong action on the form", "Invalid action type returned"])
    def test_keywords_match(self, reason: str) -> None:
        result = classify_from_failure_reason(reason)
        assert result is not None
        categories = [r["category"] for r in result]
        assert "LLM_REASONING_ERROR" in categories


class TestParameterBindingError:
    @pytest.mark.parametrize(
        "reason",
        [
            "Value should have already been set through workflow run parameters",
            "Context should have already been set through workflow run context init",
            "Pre-run invariant: workflow_definition and persisted parameter rows disagree",
        ],
    )
    def test_keywords_match(self, reason: str) -> None:
        result = classify_from_failure_reason(reason)
        assert result is not None
        categories = [r["category"] for r in result]
        assert "PARAMETER_BINDING_ERROR" in categories


class TestMultipleCategories:
    def test_captcha_and_max_steps(self) -> None:
        """A max_steps failure caused by captcha should return both categories."""
        result = classify_from_failure_reason("Reached the max steps because captcha kept appearing")
        assert result is not None
        categories = [r["category"] for r in result]
        assert "ANTI_BOT_DETECTION" in categories
        assert "MAX_STEPS_EXCEEDED" in categories

    def test_sorted_by_confidence_descending(self) -> None:
        """Results should be sorted by confidence_float, highest first."""
        result = classify_from_failure_reason("Reached the max steps because captcha kept appearing")
        assert result is not None
        assert len(result) >= 2
        confidences = [r["confidence_float"] for r in result]
        assert confidences == sorted(confidences, reverse=True)


class TestExceptionOnlyClassification:
    def test_exception_without_reason(self) -> None:
        """Exception alone (no failure_reason) should still classify."""

        class BrowserCrashError(Exception):
            pass

        result = classify_from_failure_reason(None, exception=BrowserCrashError())
        assert result is not None
        assert result[0]["category"] == "BROWSER_ERROR"


class TestFallbackToUnknown:
    def test_unrecognized_with_fallback_returns_unknown(self) -> None:
        """When fallback_to_unknown=True and no keywords match, return UNKNOWN."""
        result = classify_from_failure_reason("The Resume/CV field is required but empty", fallback_to_unknown=True)
        assert result is not None
        assert len(result) == 1
        assert result[0]["category"] == "UNKNOWN"
        assert result[0]["confidence_float"] == 0.5

    def test_unrecognized_without_fallback_returns_none(self) -> None:
        """Default fallback_to_unknown=False preserves existing None behavior."""
        result = classify_from_failure_reason("The Resume/CV field is required but empty")
        assert result is None

    def test_none_input_with_fallback_returns_none(self) -> None:
        """None input returns None even with fallback_to_unknown=True (no data = no classification)."""
        result = classify_from_failure_reason(None, fallback_to_unknown=True)
        assert result is None

    def test_empty_input_with_fallback_returns_none(self) -> None:
        """Empty string returns None even with fallback_to_unknown=True."""
        result = classify_from_failure_reason("", fallback_to_unknown=True)
        assert result is None

    def test_keyword_match_ignores_fallback(self) -> None:
        """When keywords match, fallback_to_unknown has no effect on the result."""
        result_with = classify_from_failure_reason("browser crash detected", fallback_to_unknown=True)
        result_without = classify_from_failure_reason("browser crash detected", fallback_to_unknown=False)
        assert result_with is not None
        assert result_without is not None
        assert result_with[0]["category"] == result_without[0]["category"] == "BROWSER_ERROR"
