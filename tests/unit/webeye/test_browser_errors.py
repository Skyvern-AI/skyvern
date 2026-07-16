from __future__ import annotations

import pytest
from playwright._impl._errors import Error as PWError
from playwright._impl._errors import TargetClosedError as PWTargetClosedError
from playwright._impl._errors import TimeoutError as PWTimeoutError

from skyvern.exceptions import SkyvernException
from skyvern.webeye.browser_errors import (
    BrowserAutomationError,
    BrowserCdpConnectionError,
    BrowserEngineErrorFamilies,
    BrowserErrorFamiliesConfigError,
    BrowserRetryableCdpError,
    BrowserTargetClosedError,
    BrowserTimeoutError,
    classify_browser_error,
)


def _stock_families(**overrides: object) -> BrowserEngineErrorFamilies:
    base: dict[str, object] = {
        "timeout_types": (PWTimeoutError,),
        "target_closed_types": (PWTargetClosedError,),
        "base_error_types": (PWError,),
    }
    base.update(overrides)
    return BrowserEngineErrorFamilies(**base)  # type: ignore[arg-type]


def test_taxonomy_subclasses_skyvern_exception() -> None:
    assert issubclass(BrowserAutomationError, SkyvernException)
    for cls in (
        BrowserTimeoutError,
        BrowserTargetClosedError,
        BrowserCdpConnectionError,
        BrowserRetryableCdpError,
    ):
        assert issubclass(cls, BrowserAutomationError)
    # Retryable CDP error is a specialization of the transport error.
    assert issubclass(BrowserRetryableCdpError, BrowserCdpConnectionError)


def test_timeout_type_classifies_to_timeout_with_cause() -> None:
    original = PWTimeoutError("navigation timed out")
    result = classify_browser_error(original, _stock_families())
    assert type(result) is BrowserTimeoutError
    assert result.__cause__ is original


def test_target_closed_type_classifies_to_target_closed_with_cause() -> None:
    original = PWTargetClosedError("Target page, context or browser has been closed")
    result = classify_browser_error(original, _stock_families())
    assert type(result) is BrowserTargetClosedError
    assert result.__cause__ is original


def test_transport_classification_via_type_and_predicate() -> None:
    # Transport/CDP classification is driven entirely by caller-supplied types and
    # predicates. The prose (message substrings) lives in the test, never in the
    # production module, proving the classifier is not coupled to a package's wording.
    class EngineTransportError(PWError):
        pass

    by_type = classify_browser_error(
        EngineTransportError("websocket closed"),
        _stock_families(cdp_connection_types=(EngineTransportError,)),
    )
    assert type(by_type) is BrowserCdpConnectionError

    def transport_predicate(exc: BaseException) -> bool:
        return "econnrefused" in str(exc).lower()

    original = PWError("connect ECONNREFUSED 127.0.0.1:9222")
    by_predicate = classify_browser_error(
        original,
        _stock_families(cdp_connection_predicate=transport_predicate),
    )
    assert type(by_predicate) is BrowserCdpConnectionError
    assert by_predicate.__cause__ is original

    # A generic engine Error with no transport signal falls back to the base taxonomy.
    generic = classify_browser_error(
        PWError("some other engine failure"),
        _stock_families(cdp_connection_predicate=transport_predicate),
    )
    assert type(generic) is BrowserAutomationError


def test_retryable_classification_and_precedence() -> None:
    class RetryableTransportError(PWError):
        pass

    # A retryable type also matches base_error_types (via PWError inheritance);
    # retryable must win because it is the most specific family.
    retryable = classify_browser_error(
        RetryableTransportError("temporary CDP hiccup"),
        _stock_families(retryable_types=(RetryableTransportError,)),
    )
    assert type(retryable) is BrowserRetryableCdpError
    assert isinstance(retryable, BrowserCdpConnectionError)

    # A retryable predicate takes precedence over a plain cdp-connection type match.
    original = PWError("connection reset by peer")
    by_predicate = classify_browser_error(
        original,
        BrowserEngineErrorFamilies(
            cdp_connection_types=(PWError,),
            retryable_predicate=lambda exc: "reset" in str(exc).lower(),
        ),
    )
    assert type(by_predicate) is BrowserRetryableCdpError
    assert by_predicate.__cause__ is original

    # Timeout wins over the generic base family even though PWTimeoutError is a PWError.
    timeout = classify_browser_error(PWTimeoutError("slow"), _stock_families())
    assert type(timeout) is BrowserTimeoutError


def test_predicate_receives_every_otherwise_unmatched_exception() -> None:
    # Contract pin: predicates are NOT implicitly gated by engine or exception type.
    # A predicate that keys only on message text will claim an unrelated exception
    # carrying that text — this is by design, so predicates must scope themselves by
    # engine identity. If this ever changes to auto-type-gate predicates, this test
    # must fail loudly rather than the behavior shifting silently.
    naive_predicate = classify_browser_error(
        ValueError("connection reset"),
        BrowserEngineErrorFamilies(retryable_predicate=lambda exc: "reset" in str(exc).lower()),
    )
    assert type(naive_predicate) is BrowserRetryableCdpError

    # A predicate that scopes by engine identity leaves foreign exceptions unmatched.
    scoped_predicate = classify_browser_error(
        ValueError("connection reset"),
        BrowserEngineErrorFamilies(
            retryable_predicate=lambda exc: isinstance(exc, PWError) and "reset" in str(exc).lower()
        ),
    )
    assert scoped_predicate is None


class _EngineCdpError(PWError):
    """Stand-in for a selected engine's native CDP/transport error class."""


@pytest.mark.parametrize(
    "endpoint, secret_fragments",
    [
        ("ws://tok-9f3a@127.0.0.1:9222/devtools/browser", ("tok-9f3a", "127.0.0.1")),
        (
            "wss://user:secret@remote.example.internal/session?apiKey=SEKRET",
            ("secret", "SEKRET", "remote.example.internal"),
        ),
    ],
)
def test_ws_endpoint_urls_redacted_for_every_family(endpoint: str, secret_fragments: tuple[str, ...]) -> None:
    # A ws/wss devtools socket URL is unambiguously a CDP endpoint, so it is redacted even on a
    # non-CDP family (here a timeout) — this credentialed socket must never reach the user message.
    original = PWTimeoutError(f"Timeout 30000ms exceeded connecting to {endpoint}")
    result = classify_browser_error(original, _stock_families())
    assert type(result) is BrowserTimeoutError
    for fragment in secret_fragments:
        assert fragment not in result.message
    assert "[remote browser endpoint]" in result.message
    # Non-sensitive prose stays useful for the user.
    assert "Timeout 30000ms exceeded" in result.message
    # The raw, unredacted native error is preserved for internal diagnostics.
    assert result.__cause__ is original
    assert endpoint in str(result.__cause__)


@pytest.mark.parametrize(
    "cdp_family, expected_cls",
    [
        ({"cdp_connection_types": (_EngineCdpError,)}, BrowserCdpConnectionError),
        ({"retryable_types": (_EngineCdpError,)}, BrowserRetryableCdpError),
    ],
)
@pytest.mark.parametrize(
    "endpoint, secret_fragments",
    [
        ("http://token@10.0.0.4:9222/json/version", ("token", "10.0.0.4")),
        ("https://remote.example.internal/json/version?token=SEKRET", ("SEKRET", "remote.example.internal")),
    ],
)
def test_http_discovery_url_redacted_only_for_cdp_family(
    cdp_family: dict[str, object],
    expected_cls: type[BrowserCdpConnectionError],
    endpoint: str,
    secret_fragments: tuple[str, ...],
) -> None:
    # In a CDP-connection context an http(s) /json/version discovery URL carries the same vendor
    # host and session token as the ws socket, so it is redacted for the CDP family (retryable too).
    original = _EngineCdpError(f"connect_over_cdp: fetching {endpoint} failed")
    result = classify_browser_error(original, _stock_families(**cdp_family))
    assert type(result) is expected_cls
    for fragment in secret_fragments:
        assert fragment not in result.message
    assert "[remote browser endpoint]" in result.message
    assert result.__cause__ is original
    assert endpoint in str(result.__cause__)


def test_navigation_http_url_survives_on_non_cdp_family() -> None:
    # An ordinary navigation/target http(s) URL is the site the user is automating, not a browser
    # endpoint — on a non-CDP family it stays intact so it remains useful for debugging.
    url = "https://customer-portal.example.com/login"
    original = PWTimeoutError(f"Timeout 30000ms exceeded navigating to {url}")
    result = classify_browser_error(original, _stock_families())
    assert type(result) is BrowserTimeoutError
    assert url in result.message  # nosemgrep: incomplete-url-substring-sanitization
    assert "[remote browser endpoint]" not in result.message
    assert result.message == str(original)


def test_wrapped_message_without_endpoint_url_is_unchanged() -> None:
    original = PWTimeoutError("navigation timed out after 30000ms")
    result = classify_browser_error(original, _stock_families())
    assert result is not None
    assert result.message == str(original)


def test_plain_value_error_returns_none() -> None:
    assert classify_browser_error(ValueError("not an engine error"), _stock_families()) is None
    # An unknown error must never be swallowed or flattened into the base taxonomy.
    assert classify_browser_error(RuntimeError("boom"), _stock_families()) is None


def test_cross_engine_distinct_class_identities_are_adapter_bound() -> None:
    class OtherEngineError(Exception):
        pass

    class OtherEngineTimeoutError(OtherEngineError):
        pass

    other_families = BrowserEngineErrorFamilies(
        timeout_types=(OtherEngineTimeoutError,),
        base_error_types=(OtherEngineError,),
    )

    other_timeout = OtherEngineTimeoutError("other engine timed out")
    # Classified by its own engine's families...
    assert type(classify_browser_error(other_timeout, other_families)) is BrowserTimeoutError
    # ...but invisible to the stock engine's families (distinct class identities).
    assert classify_browser_error(other_timeout, _stock_families()) is None
    # And the stock engine's timeout is invisible to the other engine's families.
    assert classify_browser_error(PWTimeoutError("stock timed out"), other_families) is None


def test_positive_cases_preserve_original_as_cause() -> None:
    cases = [
        (PWTimeoutError("t"), _stock_families(), BrowserTimeoutError),
        (PWTargetClosedError("c"), _stock_families(), BrowserTargetClosedError),
        (PWError("generic"), _stock_families(), BrowserAutomationError),
    ]
    for original, families, expected in cases:
        result = classify_browser_error(original, families)
        assert type(result) is expected
        assert result.__cause__ is original


def test_overlapping_family_configuration_fails_clearly() -> None:
    with pytest.raises(BrowserErrorFamiliesConfigError) as excinfo:
        BrowserEngineErrorFamilies(
            timeout_types=(PWTimeoutError,),
            target_closed_types=(PWTimeoutError,),
        )
    assert "TimeoutError" in str(excinfo.value)


def test_duplicate_within_single_family_fails_clearly() -> None:
    with pytest.raises(BrowserErrorFamiliesConfigError) as excinfo:
        BrowserEngineErrorFamilies(timeout_types=(PWTimeoutError, PWTimeoutError))
    assert "twice in timeout_types" in str(excinfo.value)


def test_non_exception_family_entry_fails_clearly() -> None:
    with pytest.raises(BrowserErrorFamiliesConfigError):
        BrowserEngineErrorFamilies(timeout_types=(str,))  # type: ignore[arg-type]
