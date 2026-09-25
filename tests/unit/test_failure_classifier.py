from __future__ import annotations

import json
import math
from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern import exceptions as skyvern_exceptions
from skyvern.constants import PROXY_TRANSPORT_NAV_ERRORS
from skyvern.exceptions import ScrapingFailed
from skyvern.forge.failure_classifier import (
    BROWSER_SESSION_CLOSED_REASON_CODE,
    BROWSER_SESSION_STARTUP_TIMEOUT_REASON_CODE,
    CLASSIFIER_VERSION,
    FAILURE_ATTRIBUTION_SCHEMA_VERSION,
    PROXY_TRANSPORT_FAILED_REASON_CODE,
    FailureCategory,
    classify_from_failure_reason,
    derive_failure_attribution,
)
from skyvern.webeye.scraper.scraper import build_scraping_failed_reason


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
    # Every classifier category now carries a bounded provenance code (keyword_only preserved,
    # exception- vs keyword-derived stamped) so persisted attribution never depends on prose.
    for category in categories:
        assert category["evidence_source"] in {"keyword_only", "keyword_match", "exception_type"}


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


def test_secure_codeblock_runner_unavailable_classifies_as_infrastructure() -> None:
    reason = "code block failed. failure reason: Secure CodeBlock runner is unavailable. Please retry."
    categories = _categories_for(reason, fallback_to_unknown=True)

    assert categories[0] == "INFRASTRUCTURE_ERROR"
    assert "UNKNOWN" not in categories


def test_secure_codeblock_runner_unavailable_carries_a_reason_code() -> None:
    reason = "code block failed. failure reason: Secure CodeBlock runner is unavailable. Please retry."
    result = _classify(reason, fallback_to_unknown=True)

    infra = [entry for entry in result if entry["category"] == "INFRASTRUCTURE_ERROR"]
    assert len(infra) == 1
    assert infra[0]["reason_code"] == "secure_codeblock_runner_unavailable"


def test_ordinary_user_code_failure_is_not_infrastructure() -> None:
    # A block that reached the sandbox and raised is a user-code fault, not a deploy fault.
    reason = "code block failed. failure reason: CodeBlock failed while running user code."
    categories = _categories_for(reason, fallback_to_unknown=True)

    assert "INFRASTRUCTURE_ERROR" not in categories


def test_secure_codeblock_runner_internal_failure_classifies_as_infrastructure() -> None:
    reason = "code block failed. failure reason: Secure CodeBlock runner failed before completing. Please retry."
    result = _classify(reason, fallback_to_unknown=True)

    assert result[0]["category"] == "INFRASTRUCTURE_ERROR"
    assert result[0]["reason_code"] == "secure_codeblock_runner_internal"
    assert "UNKNOWN" not in [entry["category"] for entry in result]


def test_secure_codeblock_sandbox_exited_carries_its_own_reason_code() -> None:
    reason = (
        "code block failed. failure reason: Secure CodeBlock sandbox process exited before completing. Please retry."
    )
    result = _classify(reason, fallback_to_unknown=True)

    infra = [entry for entry in result if entry["category"] == "INFRASTRUCTURE_ERROR"]
    assert len(infra) == 1
    assert infra[0]["reason_code"] == "secure_codeblock_sandbox_exited"
    assert derive_failure_attribution(result)["primary_infra_component"] == "unattributed"


def test_secure_codeblock_sandbox_exited_ranks_below_the_unambiguous_runner_arms() -> None:
    # child_exited can also be user code self-terminating, so its confidence must stay
    # strictly below the runner-internal arm's.
    exited = _classify(
        "code block failed. failure reason: Secure CodeBlock sandbox process exited before completing. Please retry.",
        fallback_to_unknown=True,
    )
    internal = _classify(
        "code block failed. failure reason: Secure CodeBlock runner failed before completing. Please retry.",
        fallback_to_unknown=True,
    )

    assert exited[0]["confidence_float"] < internal[0]["confidence_float"]


def test_secure_codeblock_runner_busy_classifies_as_infrastructure() -> None:
    reason = "code block failed. failure reason: CodeBlock runner is already executing another CodeBlock. Please retry."
    result = _classify(reason, fallback_to_unknown=True)

    assert result[0]["category"] == "INFRASTRUCTURE_ERROR"
    assert result[0]["reason_code"] == "secure_codeblock_runner_busy"


def test_user_code_crash_mentioning_process_exit_is_not_infrastructure() -> None:
    # User-code detail prose can echo the words of the sandbox-exit message; only the
    # runner-authored literal may classify as infrastructure.
    reason = (
        "code block failed. failure reason: CodeBlock failed with RuntimeError at line 3: process exited with code 1."
    )
    categories = _categories_for(reason, fallback_to_unknown=True)

    assert "INFRASTRUCTURE_ERROR" not in categories


def _driver_nav_failure(code: str, url: str = "https://example.test/login") -> skyvern_exceptions.FailedToNavigateToUrl:
    return skyvern_exceptions.FailedToNavigateToUrl(
        url=url,
        error_message=f'Page.goto: {code} at {url}\nCall log:\n  - navigating to "{url}", waiting until "load"\n',
        nav_error_code=code,
    )


def _workflow_failure_reason(error: Exception) -> str:
    return f"login block failed. failure reason: {skyvern_exceptions.get_user_facing_exception_message(error)}"


def _classified_on_both_paths(error: Exception) -> list[list[dict]]:
    """The workflow-run path classifies the flattened sentence; the task path also has the exception."""
    return [
        _classify(_workflow_failure_reason(error), fallback_to_unknown=True),
        _classify(str(error), error, fallback_to_unknown=True),
    ]


@pytest.mark.parametrize("code", PROXY_TRANSPORT_NAV_ERRORS)
def test_a_proxy_transport_navigation_failure_is_a_proxy_error(code: str) -> None:
    evidence = []
    for categories in _classified_on_both_paths(_driver_nav_failure(code)):
        doc = derive_failure_attribution(categories)
        assert doc["failure_category"] == "PROXY_ERROR"
        assert doc["primary_infra_component"] == "proxy"
        assert doc["reason_code"] == PROXY_TRANSPORT_FAILED_REASON_CODE
        assert "NAVIGATION_FAILURE" not in [category["category"] for category in categories]
        evidence.append(doc["evidence_source"])
    # The flattened sentence is scanned text; only the exception carries the driver's typed code.
    assert evidence == ["keyword_match", "code_level"]


def test_a_transport_code_outside_the_driver_message_is_not_a_proxy_error() -> None:
    model_written_reason = (
        "The target page displayed net::ERR_TUNNEL_CONNECTION_FAILED, so the goal cannot be completed."
    )

    categories = _categories_for(model_written_reason, None, fallback_to_unknown=True)

    assert "PROXY_ERROR" not in categories


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(
            skyvern_exceptions.UnresolvableNavigationHost(
                url="https://gone.example.test/",
                host="gone.example.test",
                error_message="Page.goto: net::ERR_TUNNEL_CONNECTION_FAILED at https://gone.example.test/",
            ),
            id="target_with_no_dns_record_borrows_the_tunnel_code",
        ),
        pytest.param(_driver_nav_failure("net::ERR_CERT_AUTHORITY_INVALID"), id="site_certificate_chain"),
        pytest.param(_driver_nav_failure("net::ERR_NAME_NOT_RESOLVED"), id="site_dns"),
        pytest.param(_driver_nav_failure("net::ERR_CONNECTION_REFUSED"), id="site_refused_the_connection"),
    ],
)
def test_a_site_side_navigation_failure_stays_a_navigation_failure(error: Exception) -> None:
    for categories in _classified_on_both_paths(error):
        doc = derive_failure_attribution(categories)
        assert doc["failure_category"] == "NAVIGATION_FAILURE"
        assert doc["primary_infra_component"] == "unattributed"


def test_a_proxy_code_quoted_in_the_url_is_not_a_proxy_error() -> None:
    error = _driver_nav_failure(
        "net::ERR_CERT_DATE_INVALID", url="https://example.test/?next=net::ERR_TUNNEL_CONNECTION_FAILED"
    )

    for classified in _classified_on_both_paths(error):
        categories = [category["category"] for category in classified]
        assert categories[0] == "NAVIGATION_FAILURE"
        assert "PROXY_ERROR" not in categories


@pytest.mark.asyncio
async def test_page_analysis_timeout_reason_ranks_page_load_timeout() -> None:
    browser_state = MagicMock()
    browser_state.get_working_page = AsyncMock(return_value=None)

    timeout_reason = await build_scraping_failed_reason(browser_state, "https://example.com/path", timed_out=True)
    assert "timeout" in timeout_reason.lower()
    # A page-analysis timeout is wrapped in ScrapingFailed; its reason must still rank
    # PAGE_LOAD_TIMEOUT above DATA_EXTRACTION_FAILURE so it stays distinguishable.
    categories = _categories_for(timeout_reason, ScrapingFailed(reason=timeout_reason))
    assert categories[0] == "PAGE_LOAD_TIMEOUT"


@pytest.mark.asyncio
async def test_non_timeout_scraping_reason_stays_data_extraction() -> None:
    browser_state = MagicMock()
    browser_state.get_working_page = AsyncMock(return_value=None)

    generic_reason = await build_scraping_failed_reason(browser_state, "https://example.com/path", timed_out=False)
    assert "timeout" not in generic_reason.lower()
    assert _categories_for(generic_reason, ScrapingFailed(reason=generic_reason)) == ["DATA_EXTRACTION_FAILURE"]


def test_exception_name_classifies_when_instance_absent() -> None:
    """A bare cause-type name classifies via exception_name — the Temporal activity-failure
    path only carries the class name across the serialization boundary, not the instance."""
    result = classify_from_failure_reason(None, exception_name="BitwardenVaultError", fallback_to_unknown=True)

    assert result is not None
    assert result[0]["category"] == "CREDENTIAL_ERROR"


def test_exception_name_is_ignored_when_instance_provided() -> None:
    result = classify_from_failure_reason(
        "boom", exception=ElementNotFoundError(), exception_name="BitwardenVaultError"
    )

    assert result is not None
    assert result[0]["category"] == "ELEMENT_NOT_FOUND"


def test_exception_name_alone_returns_none_when_unrecognized() -> None:
    assert classify_from_failure_reason(None, exception_name="ValueError") is None


_LOCATOR_WAIT_TIMEOUT = (
    "Failed to execute code block. Reason: TimeoutError: Locator.wait_for: Timeout 60000ms exceeded. "
    'Call log: - waiting for locator("button[aria-label=\\"Log in\\"]")'
)


def test_locator_wait_timeout_is_an_element_state_failure_not_a_page_load_one() -> None:
    categories = [result["category"] for result in _classify(_LOCATOR_WAIT_TIMEOUT)]

    assert "ELEMENT_STATE_TIMEOUT" in categories
    assert "PAGE_LOAD_TIMEOUT" not in categories


def test_navigation_timeout_is_still_a_page_load_failure() -> None:
    categories = [result["category"] for result in _classify("Page.goto: Timeout 30000ms exceeded")]

    assert "PAGE_LOAD_TIMEOUT" in categories
    assert "ELEMENT_STATE_TIMEOUT" not in categories


def test_a_selector_naming_a_password_field_is_not_an_auth_failure() -> None:
    # The word is part of the locator the wait timed out on, not evidence that login was rejected.
    reason = 'TimeoutError: Locator.wait_for: Timeout 10000ms exceeded. Call log: - waiting for locator("#password")'

    categories = [result["category"] for result in _classify(reason)]

    assert "ELEMENT_STATE_TIMEOUT" in categories
    assert "AUTH_FAILURE" not in categories


def test_a_genuine_auth_failure_still_classifies() -> None:
    categories = [result["category"] for result in _classify("login failed: incorrect password")]

    assert "AUTH_FAILURE" in categories


def test_a_bare_waiting_for_locator_timeout_is_an_element_state_failure() -> None:
    # Truncated Playwright messages drop the "Locator.method:" prefix and keep only the call log.
    reason = "Timeout 30000ms exceeded waiting for locator('#password')"

    categories = [result["category"] for result in _classify(reason)]

    assert "ELEMENT_STATE_TIMEOUT" in categories
    assert "PAGE_LOAD_TIMEOUT" not in categories
    assert "AUTH_FAILURE" not in categories


def test_a_waiting_for_selector_timeout_is_an_element_state_failure() -> None:
    reason = "Timeout 30000ms exceeded waiting for selector '#login-btn'"

    categories = [result["category"] for result in _classify(reason)]

    assert "ELEMENT_STATE_TIMEOUT" in categories
    assert "PAGE_LOAD_TIMEOUT" not in categories
    assert "AUTH_FAILURE" not in categories


def test_a_genuine_auth_failure_survives_an_appended_element_timeout() -> None:
    # Page-derived text can carry both; the element timeout must not silence the auth signal.
    reason = "Login failed: incorrect password; Timeout 5000ms exceeded on wait_for_selector"

    categories = [result["category"] for result in _classify(reason)]

    assert "AUTH_FAILURE" in categories
    assert "ELEMENT_STATE_TIMEOUT" in categories


# ── Infra-failure attribution derivation (SKY-16588) ──────────────────────────

_ALLOWED_ATTRIBUTION_KEYS = {
    "schema_version",
    "classifier_version",
    "failure_category",
    "primary_infra_component",
    "evidence_source",
    "heuristic_confidence",
    "reason_code",
}


def test_derive_maps_proxy_error_to_proxy_component() -> None:
    fc = [{"category": "PROXY_ERROR", "confidence_float": 0.9, "reasoning": "Exception: NoProxyAvailable"}]
    doc = derive_failure_attribution(fc)
    assert doc["primary_infra_component"] == "proxy"
    assert doc["failure_category"] == "PROXY_ERROR"
    assert doc["evidence_source"] == "exception_type"


def test_derive_maps_browser_error_to_browser_component() -> None:
    fc = [{"category": "BROWSER_ERROR", "confidence_float": 0.9, "reasoning": "Exception: TargetClosedError"}]
    assert derive_failure_attribution(fc)["primary_infra_component"] == "browser"


def test_derive_maps_llm_error_to_llm_component() -> None:
    fc = [{"category": "LLM_ERROR", "confidence_float": 0.9, "reasoning": "Exception: RateLimitError"}]
    assert derive_failure_attribution(fc)["primary_infra_component"] == "llm"


def test_derive_infrastructure_codeblock_reason_code_maps_to_codeblock() -> None:
    fc = [
        {
            "category": "INFRASTRUCTURE_ERROR",
            "confidence_float": 0.95,
            "reason_code": "secure_codeblock_runner_unavailable",
            "reasoning": "Secure CodeBlock runner was unreachable",
        }
    ]
    doc = derive_failure_attribution(fc)
    assert doc["primary_infra_component"] == "codeblock"
    assert doc["evidence_source"] == "reason_code"
    assert doc["reason_code"] == "secure_codeblock_runner_unavailable"


def test_derive_infrastructure_activity_timeout_maps_to_worker() -> None:
    fc = [
        {
            "category": "INFRASTRUCTURE_ERROR",
            "confidence_float": 0.9,
            "reasoning": "Activity/heartbeat timeout finalized the run",
        }
    ]
    assert derive_failure_attribution(fc)["primary_infra_component"] == "worker"


def test_derive_keyword_only_evidence_abstains_to_unattributed() -> None:
    fc = [
        {
            "category": "ANTI_BOT_DETECTION",
            "confidence_float": 0.7,
            "evidence_source": "keyword_only",
            "reasoning": "Keywords matched in failure reason",
        }
    ]
    assert derive_failure_attribution(fc)["primary_infra_component"] == "unattributed"


def test_derive_low_confidence_abstains_to_unattributed() -> None:
    fc = [{"category": "WRONG_PAGE_STATE", "confidence_float": 0.6, "reasoning": "Keywords matched"}]
    assert derive_failure_attribution(fc)["primary_infra_component"] == "unattributed"


def test_derive_max_steps_exceeded_maps_to_non_infra() -> None:
    fc = [{"category": "MAX_STEPS_EXCEEDED", "confidence_float": 0.9, "reasoning": "Keywords matched"}]
    doc = derive_failure_attribution(fc)
    assert doc["primary_infra_component"] == "non_infra"
    assert doc["evidence_source"] == "keyword_match"


@pytest.mark.parametrize("category", list(FailureCategory))
def test_every_typed_run_category_survives_attribution(category: FailureCategory) -> None:
    document = derive_failure_attribution([{"category": category.value, "confidence_float": 1.0}])
    assert document["failure_category"] == category.value


def test_derive_budget_exhaustion_maps_to_non_infra() -> None:
    document = derive_failure_attribution([{"category": "BUDGET_EXHAUSTED", "confidence_float": 1.0}])
    assert document["failure_category"] == "BUDGET_EXHAUSTED"
    assert document["primary_infra_component"] == "non_infra"


def test_derive_element_not_found_maps_to_non_infra() -> None:
    fc = [{"category": "ELEMENT_NOT_FOUND", "confidence_float": 0.8, "reasoning": "Exception: ElementNotFound"}]
    assert derive_failure_attribution(fc)["primary_infra_component"] == "non_infra"


def test_derive_none_input_is_explicit_unattributed_document() -> None:
    doc = derive_failure_attribution(None)
    assert doc["primary_infra_component"] == "unattributed"
    assert doc["failure_category"] is None
    assert doc["evidence_source"] == "none"
    assert doc["heuristic_confidence"] == 0.0


def test_derive_stamps_versions_and_never_persists_raw_text() -> None:
    fc = [
        {
            "category": "PROXY_ERROR",
            "confidence_float": 0.9,
            "reasoning": "Exception: NoProxyAvailable while dialing https://secret.example/login",
        }
    ]
    doc = derive_failure_attribution(fc)
    assert doc["schema_version"] == FAILURE_ATTRIBUTION_SCHEMA_VERSION
    assert doc["classifier_version"] == CLASSIFIER_VERSION
    # Bounded codes only — no reasoning/failure_reason/URL text bleeds into the document.
    assert set(doc).issubset(_ALLOWED_ATTRIBUTION_KEYS)
    assert "reasoning" not in doc and "failure_reason" not in doc
    assert "https://" not in repr(doc)


def test_derive_uses_highest_confidence_primary_first() -> None:
    # Classifier output is confidence-sorted; the first entry is the primary and wins ties.
    fc = [
        {"category": "PROXY_ERROR", "confidence_float": 0.9, "reasoning": "Exception: NoProxyAvailable"},
        {"category": "BROWSER_ERROR", "confidence_float": 0.9, "reasoning": "Exception: TargetClosedError"},
    ]
    assert derive_failure_attribution(fc)["primary_infra_component"] == "proxy"


def test_derive_end_to_end_from_real_classifier_output() -> None:
    fc = classify_from_failure_reason("No proxy available for this run", fallback_to_unknown=True)
    assert derive_failure_attribution(fc)["primary_infra_component"] == "proxy"


def test_derive_parameter_binding_error_maps_to_worker() -> None:
    # PM adjudication: an internal configuration mismatch is owned by the worker in v1.
    fc = [{"category": "PARAMETER_BINDING_ERROR", "confidence_float": 0.95, "reasoning": "Keywords matched"}]
    assert derive_failure_attribution(fc)["primary_infra_component"] == "worker"


def test_derive_unknown_is_none_provenance_not_keyword_match() -> None:
    # A no-match UNKNOWN has no positive signal; it must not be labeled keyword_match.
    fc = classify_from_failure_reason("something entirely unrecognized", fallback_to_unknown=True)
    doc = derive_failure_attribution(fc)
    assert doc["failure_category"] == "UNKNOWN"
    assert doc["primary_infra_component"] == "unattributed"
    assert doc["evidence_source"] == "none"


def test_derive_malformed_confidence_abstains_without_raising() -> None:
    fc = [{"category": "PROXY_ERROR", "confidence_float": "not-a-number", "reasoning": "Exception: NoProxyAvailable"}]
    doc = derive_failure_attribution(fc)
    assert doc["primary_infra_component"] == "unattributed"
    assert doc["heuristic_confidence"] == 0.0


def test_derive_non_dict_primary_abstains_safely() -> None:
    doc = derive_failure_attribution(["garbage-not-a-dict"])
    assert doc["primary_infra_component"] == "unattributed"
    assert doc["failure_category"] is None
    assert doc["evidence_source"] == "none"


def test_derive_drops_unknown_category_string_and_abstains() -> None:
    # An adversarial/dynamic category value must never be copied into the document.
    fc = [{"category": "rm -rf / ; DROP TABLE runs", "confidence_float": 0.99, "reasoning": "Exception: X"}]
    doc = derive_failure_attribution(fc)
    assert doc["failure_category"] is None
    assert doc["primary_infra_component"] == "unattributed"
    assert doc["evidence_source"] == "none"
    assert "rm -rf" not in repr(doc)


def test_derive_drops_unlisted_reason_code() -> None:
    # A reason_code outside the known literals is dropped; the component still resolves.
    fc = [
        {
            "category": "INFRASTRUCTURE_ERROR",
            "confidence_float": 0.9,
            "reason_code": "attacker-controlled-string",
            "reasoning": "Activity/heartbeat timeout finalized the run",
        }
    ]
    doc = derive_failure_attribution(fc)
    assert "reason_code" not in doc
    assert doc["primary_infra_component"] == "worker"


def test_derive_document_keys_and_values_are_bounded() -> None:
    # Every value is a bounded code drawn from a fixed set — never free text.
    for reason in ["No proxy available", "browser context closed", "max steps exceeded", "totally novel text"]:
        fc = classify_from_failure_reason(reason, fallback_to_unknown=True)
        doc = derive_failure_attribution(fc)
        assert set(doc).issubset(_ALLOWED_ATTRIBUTION_KEYS)
        assert doc["primary_infra_component"] in {
            "proxy",
            "browser",
            "llm",
            "worker",
            "codeblock",
            "non_infra",
            "unattributed",
        }
        assert doc["evidence_source"] in {
            "keyword_only",
            "keyword_match",
            "exception_type",
            "reason_code",
            "code_level",
            "none",
        }


@pytest.mark.parametrize("category", ["BUDGET_EXHAUSTED", "LLM_ERROR", "INFRASTRUCTURE_ERROR"])
def test_derive_code_level_producer_is_not_labeled_keyword_match(category: str) -> None:
    # A category emitted directly by a typed/code-level producer (no keyword scan, no reason
    # code, no Exception: reasoning) must record code_level provenance, not keyword_match.
    doc = derive_failure_attribution([{"category": category, "confidence_float": 1.0}])
    assert doc["evidence_source"] == "code_level"


def test_derive_honors_explicit_bounded_evidence_source() -> None:
    doc = derive_failure_attribution(
        [{"category": "BUDGET_EXHAUSTED", "confidence_float": 1.0, "evidence_source": "code_level"}]
    )
    assert doc["evidence_source"] == "code_level"
    # An out-of-vocabulary explicit source is ignored (falls back to inference), never persisted.
    doc2 = derive_failure_attribution(
        [{"category": "MAX_STEPS_EXCEEDED", "confidence_float": 0.9, "evidence_source": "totally-made-up"}]
    )
    assert doc2["evidence_source"] == "code_level"


# ── Terminal-derivation robustness guards (SKY-16588 review round 3) ───────────


@pytest.mark.parametrize("bad_category", [["PROXY_ERROR"], {"category": "PROXY_ERROR"}, {"PROXY_ERROR"}, 123])
def test_derive_unhashable_or_nonstring_category_is_dropped_without_raising(bad_category: object) -> None:
    # A list/dict/set is unhashable; a membership lookup would raise TypeError and break the
    # never-raises terminal contract. Non-string categories must be dropped and abstain.
    doc = derive_failure_attribution([{"category": bad_category, "confidence_float": 0.99}])
    assert doc["failure_category"] is None
    assert doc["primary_infra_component"] == "unattributed"
    assert doc["evidence_source"] == "none"


@pytest.mark.parametrize("bad_reason_code", [["secure_codeblock_runner_unavailable"], {"x": 1}, 7])
def test_derive_unhashable_or_nonstring_reason_code_is_dropped_without_raising(bad_reason_code: object) -> None:
    doc = derive_failure_attribution(
        [{"category": "INFRASTRUCTURE_ERROR", "confidence_float": 0.9, "reason_code": bad_reason_code}]
    )
    assert "reason_code" not in doc
    # Unknown reason_code -> not codeblock; INFRASTRUCTURE_ERROR falls back to worker.
    assert doc["primary_infra_component"] == "worker"


@pytest.mark.parametrize("bad_confidence", ["nan", "inf", "-inf", float("nan"), float("inf"), float("-inf")])
def test_derive_nonfinite_confidence_abstains_and_stays_zero(bad_confidence: object) -> None:
    doc = derive_failure_attribution([{"category": "PROXY_ERROR", "confidence_float": bad_confidence}])
    assert doc["heuristic_confidence"] == 0.0
    assert math.isfinite(doc["heuristic_confidence"])
    # NaN must not slip past the confidence threshold into a component assertion.
    assert doc["primary_infra_component"] == "unattributed"


@pytest.mark.parametrize(
    "primary",
    [
        {"category": "PROXY_ERROR", "confidence_float": float("nan")},
        {"category": ["PROXY_ERROR"], "confidence_float": 0.9},
        {"category": "PROXY_ERROR", "confidence_float": "inf", "reason_code": {"x": 1}},
        {"category": None, "confidence_float": None},
    ],
)
def test_derive_documents_are_strict_json_serializable(primary: dict) -> None:
    # allow_nan=False rejects NaN/Infinity; a document that fails this would break the
    # Postgres terminal write. Every derived document must be strict-JSON serializable.
    doc = derive_failure_attribution([primary])
    encoded = json.dumps(doc, allow_nan=False)
    assert "NaN" not in encoded and "Infinity" not in encoded


def test_derive_never_raises_on_adversarial_inputs() -> None:
    adversarial = [
        None,
        [],
        ["not-a-dict"],
        [{"category": {"nested": "dict"}, "confidence_float": ["also-bad"]}],
        [{"category": "PROXY_ERROR", "confidence_float": float("nan"), "reason_code": ["list"]}],
    ]
    for failure_category in adversarial:
        doc = derive_failure_attribution(failure_category)
        json.dumps(doc, allow_nan=False)  # must not raise
        assert doc["primary_infra_component"] in {
            "proxy",
            "browser",
            "llm",
            "worker",
            "codeblock",
            "non_infra",
            "unattributed",
        }


@pytest.mark.parametrize(
    "failure_category",
    [
        {"category": "PROXY_ERROR"},  # object-shaped, not a list
        {"0": {"category": "PROXY_ERROR"}},  # object with a "0" key
        42,  # scalar
        3.14,
        "PROXY_ERROR",  # bare string
        True,
    ],
)
def test_derive_object_or_scalar_failure_category_abstains_without_raising(failure_category: object) -> None:
    # A non-list value must not be indexed (KeyError/TypeError) inside the terminal transaction.
    doc = derive_failure_attribution(failure_category)  # type: ignore[arg-type]
    assert doc["primary_infra_component"] == "unattributed"
    assert doc["failure_category"] is None
    json.dumps(doc, allow_nan=False)


@pytest.mark.parametrize(
    "reason_code",
    [BROWSER_SESSION_CLOSED_REASON_CODE, BROWSER_SESSION_STARTUP_TIMEOUT_REASON_CODE],
)
def test_derive_preserves_browser_lease_reason_codes(reason_code: str) -> None:
    fc = [{"category": "BROWSER_ERROR", "confidence_float": 1.0, "reason_code": reason_code, "reasoning": "x"}]
    doc = derive_failure_attribution(fc)
    assert doc["primary_infra_component"] == "browser"
    assert doc["reason_code"] == reason_code
    assert doc["evidence_source"] == "reason_code"


@pytest.mark.parametrize("huge", [10**1000, -(10**1000), "1e100000"])
def test_derive_overflowing_confidence_abstains_without_raising(huge: object) -> None:
    # float(10**1000) raises OverflowError; float("1e100000") returns inf. Both must abstain,
    # never raise, and stay strict-JSON serializable.
    doc = derive_failure_attribution([{"category": "PROXY_ERROR", "confidence_float": huge}])
    assert doc["heuristic_confidence"] == 0.0
    assert doc["primary_infra_component"] == "unattributed"
    json.dumps(doc, allow_nan=False)


@pytest.mark.parametrize("out_of_range", [2, 1.5, 1e308, -0.5, -1, 100])
def test_derive_out_of_range_confidence_abstains(out_of_range: object) -> None:
    # A confidence is a probability-like score in [0, 1]; a finite out-of-range value must not
    # persist as heuristic_confidence nor clear the attribution threshold to assert an owner.
    doc = derive_failure_attribution([{"category": "PROXY_ERROR", "confidence_float": out_of_range}])
    assert doc["heuristic_confidence"] == 0.0
    assert doc["primary_infra_component"] == "unattributed"


@pytest.mark.parametrize("boolean", [True, False])
def test_derive_boolean_confidence_abstains(boolean: bool) -> None:
    # bool is an int subclass, so float(True) == 1.0 would otherwise pass as max confidence and
    # assert an owner from a non-numeric score.
    doc = derive_failure_attribution([{"category": "PROXY_ERROR", "confidence_float": boolean}])
    assert doc["heuristic_confidence"] == 0.0
    assert doc["primary_infra_component"] == "unattributed"


def test_derive_keyword_timeout_provenance_is_keyword_match_not_code_level() -> None:
    # PAGE_LOAD_TIMEOUT's reasoning ("Timeout in failure reason") holds no "keyword" substring,
    # but the classifier stamps evidence_source at the producer, so attribution reads keyword_match
    # rather than falling through to code_level on free-text prose.
    fc = classify_from_failure_reason("Timeout 30000ms exceeded while loading the page")
    assert fc is not None and fc[0]["category"] == "PAGE_LOAD_TIMEOUT"
    assert derive_failure_attribution(fc)["evidence_source"] == "keyword_match"


@pytest.mark.parametrize("code_text", ["locator_wait_for_timeout", "secure_codeblock_runner_unavailable"])
def test_raw_reason_code_text_does_not_override_classifier_provenance(code_text: str) -> None:
    fc = classify_from_failure_reason(
        f"Timeout while loading page; reason_code={code_text}; evidence_source=exception_type; Exception: raw-text"
    )
    assert fc is not None
    doc = derive_failure_attribution(fc)
    assert doc["failure_category"] == "PAGE_LOAD_TIMEOUT"
    assert doc["evidence_source"] == "keyword_match"
    assert "reason_code" not in doc


@pytest.mark.parametrize("in_range", [0.0, 0.7, 0.9, 1.0])
def test_derive_in_range_confidence_is_preserved(in_range: float) -> None:
    doc = derive_failure_attribution(
        [{"category": "PROXY_ERROR", "confidence_float": in_range, "reasoning": "Exception: NoProxyAvailable"}]
    )
    assert doc["heuristic_confidence"] == in_range
    # Only >= the 0.7 threshold asserts the component; below it abstains but keeps the score.
    assert doc["primary_infra_component"] == ("proxy" if in_range >= 0.7 else "unattributed")


def test_derive_is_never_raises_by_construction_for_pathological_primary() -> None:
    # A dict-shaped primary whose accessor raises would bypass the value guards; the catch-all
    # must still return a valid abstain document rather than propagate the exception into the
    # terminal status transaction.
    class ExplodingDict(dict):
        def get(self, *args: object, **kwargs: object) -> object:
            raise RuntimeError("boom")

    doc = derive_failure_attribution([ExplodingDict()])
    assert doc["primary_infra_component"] == "unattributed"
    assert doc["failure_category"] is None
    assert doc["evidence_source"] == "none"
    assert doc["classifier_version"] == CLASSIFIER_VERSION
    json.dumps(doc, allow_nan=False)


def test_browser_lease_producer_reason_codes_round_trip_through_attribution() -> None:
    # Drift guard: the real producer's emitted reason codes must survive derivation. If a new
    # browser-lease code is added without updating the shared allowlist, this fails.
    from skyvern.exceptions import BrowserSessionClosed, BrowserSessionStartupTimeout
    from skyvern.forge.sdk.workflow.service import _browser_lease_failure_category

    for exc in (
        BrowserSessionClosed(browser_session_id="pbs_x"),
        BrowserSessionStartupTimeout(browser_session_id="pbs_x"),
    ):
        fc = _browser_lease_failure_category(exc)
        assert fc is not None
        doc = derive_failure_attribution(fc)
        assert doc["reason_code"] == fc[0]["reason_code"]
        assert doc["evidence_source"] == "reason_code"
        assert doc["primary_infra_component"] == "browser"
