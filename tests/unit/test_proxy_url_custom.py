import json
import re
from hashlib import sha256
from unittest.mock import patch

import pytest
from pydantic import ValidationError
from structlog.testing import CapturingLogger

from skyvern.config import settings
from skyvern.forge.agent_functions import AgentFunction
from skyvern.forge.sdk.db.utils import deserialize_proxy_location, serialize_proxy_location
from skyvern.forge.sdk.schemas.browser_profiles import UpdateBrowserProfileRequest
from skyvern.forge.sdk.schemas.credentials import UpdateCredentialRequest
from skyvern.schemas.proxy_pinning import RedactedProxyLogValue, redact_proxy_location
from skyvern.schemas.runs import GeoTarget, ProxyLocation
from skyvern.webeye.browser_factory import BrowserContextFactory, _redact_url_query


def test_redact_url_query_strips_presigned_signature() -> None:
    redacted = _redact_url_query(
        "https://bucket.s3.amazonaws.com/docs/report?X-Amz-Signature=deadbeef&X-Amz-Credential=AKIA"
    )
    assert redacted == "https://bucket.s3.amazonaws.com/docs/report"
    assert "X-Amz-Signature" not in redacted


def test_redact_url_query_keeps_url_without_query() -> None:
    assert _redact_url_query("https://example.com/docs/report.pdf") == "https://example.com/docs/report.pdf"


def test_redact_url_query_plain_string_passes_through() -> None:
    assert _redact_url_query("not-a-url") == "not-a-url"


def test_build_browser_args_never_sets_proxy() -> None:
    args = BrowserContextFactory.build_browser_args(
        proxy_location={"url": "http://user:secret@proxy.example.com:8080"},
    )
    assert "proxy" not in args


def test_build_browser_args_defaults_to_playwright_recording_size() -> None:
    args = BrowserContextFactory.build_browser_args()

    assert "record_video_size" not in args


def test_build_browser_args_uses_configured_recording_size(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "BROWSER_RECORDING_WIDTH", 1280)
    monkeypatch.setattr(settings, "BROWSER_RECORDING_HEIGHT", 720)

    args = BrowserContextFactory.build_browser_args()

    assert args["record_video_size"] == {"width": 1280, "height": 720}


@pytest.mark.asyncio
async def test_resolve_recording_video_size_is_noop_in_oss() -> None:
    agent_function = AgentFunction()

    assert await agent_function.resolve_recording_video_size(None, distinct_id="wr_1", organization_id="o_1") is None
    existing = {"width": 1280, "height": 720}
    assert (
        await agent_function.resolve_recording_video_size(existing, distinct_id="wr_1", organization_id="o_1")
        == existing
    )


def test_deserialize_proxy_location_custom_url_returns_dict() -> None:
    result = deserialize_proxy_location('{"url": "http://user:pass@proxy.example.com:8080"}')
    assert result == {"url": "http://user:pass@proxy.example.com:8080"}


def test_proxy_location_db_round_trip_custom_url() -> None:
    original = {"url": "http://user:pass@proxy.example.com:8080"}
    serialized = serialize_proxy_location(original)
    assert serialized is not None
    assert deserialize_proxy_location(serialized) == original


@pytest.mark.parametrize("request_model", [UpdateCredentialRequest, UpdateBrowserProfileRequest])
def test_proxy_pin_requests_reject_custom_proxy_urls(request_model: type) -> None:
    with pytest.raises(ValidationError, match="Custom proxy URLs are not supported"):
        request_model(proxy_location={"url": "http://user:pass@proxy.example.com:8080"})


def test_proxy_location_db_round_trip_geo_target() -> None:
    original = GeoTarget(country="US", subdivision="CA", city="San Francisco")
    serialized = serialize_proxy_location(original)
    assert serialized is not None
    result = deserialize_proxy_location(serialized)
    assert isinstance(result, GeoTarget)
    assert result.country == "US"
    assert result.subdivision == "CA"
    assert result.city == "San Francisco"


def test_proxy_location_db_round_trip_enum_still_works() -> None:
    serialized = serialize_proxy_location(ProxyLocation.RESIDENTIAL)
    assert serialized is not None
    assert deserialize_proxy_location(serialized) == ProxyLocation.RESIDENTIAL


CREDENTIALED_DB_VALUE = '{"url": "http://user:secret@proxy.example.com:8080"}'


def _capture_db_utils_logs() -> tuple[CapturingLogger, object]:
    """A CapturingLogger substituted for the db.utils module logger.

    Substitution rather than structlog's capture_logs: the deployed filtering bound logger
    turns LOG.debug into a no-op before any processor runs, so a processor-swapping capture
    sees nothing whenever the suite has configured logging at INFO.
    """
    logger = CapturingLogger()
    return logger, patch("skyvern.forge.sdk.db.utils.LOG", logger)


def test_deserialize_custom_url_logs_no_credentials() -> None:
    """Regression for SKY-14786: the success branch logged the raw stored JSON at INFO, and it is
    selected precisely when that JSON holds a credentialed proxy URL."""
    logger, patched = _capture_db_utils_logs()
    with patched:
        result = deserialize_proxy_location(CREDENTIALED_DB_VALUE)

    assert result == {"url": "http://user:secret@proxy.example.com:8080"}
    assert "secret" not in str(logger.calls)


def test_deserialize_malformed_value_warning_stays_diagnostic_without_credentials() -> None:
    logger, patched = _capture_db_utils_logs()
    with patched:
        deserialize_proxy_location(CREDENTIALED_DB_VALUE.rstrip("}"))

    warnings = [call for call in logger.calls if call.args[:1] == ("Failed to parse proxy_location as GeoTarget",)]
    assert len(warnings) == 1
    # The branch stays diagnostic by NAMING the value and identifying it, not by rendering it:
    # two different malformed rows are still distinguishable, and neither is readable.
    assert re.fullmatch(r"[a-z_]+:[0-9a-f]{12}", warnings[0].kwargs["db_value"])
    assert "secret" not in str(logger.calls)


def test_serialize_custom_url_logs_no_credentials() -> None:
    logger, patched = _capture_db_utils_logs()
    with patched:
        serialize_proxy_location({"url": "http://user:secret@proxy.example.com:8080"})

    assert "secret" not in str(logger.calls)


CREDENTIAL = "s3cr3t"
_BACKSLASH = chr(92)  # built from a codepoint so no fixture below can hold a literal @ by accident
# Every shape sixteen review rounds produced. They differ in where the credential sits and how it
# is encoded - the two axes a mechanism that renders a value has to keep up with, and that naming
# does not have at all.
CREDENTIALED_SHAPES: list[object] = [
    {"url": f"http://user:{CREDENTIAL}@proxy.example.com:8080"},
    {"url": f"http://{CREDENTIAL}@proxy.example.com:8080"},  # token as the sole userinfo
    {"url": f"http://user:pa'{CREDENTIAL}@proxy.example.com"},  # legal sub-delim
    {"url": f"http://alice@example.com:{CREDENTIAL}@proxy.example.com"},  # email-style username
    {"url": f"http://user:{CREDENTIAL}?part@proxy.example.com"},  # delimiter ends the authority
    {"url": f"http:/user:{CREDENTIAL}@proxy.example.com"},  # one slash: no authority marker
    {"url": f"http://user:{CREDENTIAL}@["},  # urlsplit raises on this
    {"url": ["http://user:%s@h" % CREDENTIAL]},  # url is not a string
    {"url": "http://proxy.example.com", "fallback_url": f"http://user:{CREDENTIAL}@backup.example.com"},
    {"proxy": f"http://user:{CREDENTIAL}@proxy.example.com"},  # no url key at all
    '{"country":"USA","url":"http://user:' + CREDENTIAL + _BACKSLASH + 'u0040["}',  # escaped separator
    '{"country":"USA","note":"http://user:' + CREDENTIAL + _BACKSLASH + 'u0040h"}',  # escaped, other key
    '﻿{"url":"http:' + _BACKSLASH + "/" + _BACKSLASH + f'/user:{CREDENTIAL}@h"',  # BOM + escaped
    f"http://user:{CREDENTIAL}@proxy.example.com",  # bare string
    f'{{"url": "http://user:{CREDENTIAL}@proxy.example.com"',  # json.loads rejects it
]

# name:identifier and nothing else. This is stronger than asserting the credential is absent: it
# asserts NO part of the value was rendered, so a new placement or encoding has nothing to reach.
_NAMED_ONLY_RE = re.compile(r"[a-z_]+:[0-9a-f]{12}")


@pytest.mark.parametrize("value", CREDENTIALED_SHAPES)
def test_redact_proxy_location_never_renders_a_credentialed_value(value: object) -> None:
    rendered = redact_proxy_location(value)
    assert CREDENTIAL not in rendered
    assert _NAMED_ONLY_RE.fullmatch(rendered), rendered


def test_redact_proxy_location_returns_a_safe_log_value() -> None:
    rendered = redact_proxy_location({"url": "http://user:synthetic-secret@token.proxy.example:8080"})

    assert isinstance(rendered, RedactedProxyLogValue)
    assert "synthetic-secret" not in rendered
    assert "token.proxy.example" not in rendered


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (ProxyLocation.RESIDENTIAL, "RESIDENTIAL"),
        (GeoTarget(country="US", subdivision="CA", city="San Francisco"), "geo_target:US:"),
        ("RESIDENTIAL_ISP", "RESIDENTIAL_ISP"),
        ("RESIDENTIL", "RESIDENTIL"),  # a bad enum name is what the failure warning exists to show
    ],
)
def test_redact_proxy_location_renders_only_provably_safe_forms(value: object, expected: str) -> None:
    """The enum and an enum-shaped string cannot hold a URL by character class. A GeoTarget is NOT
    safe wholesale - city takes 100 characters of free text - so only its country is shown."""
    rendered = redact_proxy_location(value)
    assert rendered.startswith(expected) if expected.endswith(":") else rendered == expected


def test_identifier_correlates_within_a_process_but_cannot_verify_a_guess() -> None:
    value = {"url": f"http://user:{CREDENTIAL}@proxy.example.com"}
    rendered = redact_proxy_location(value)

    assert rendered == redact_proxy_location(value)
    assert rendered != redact_proxy_location({"url": "http://user:other@proxy.example.com"})
    assert sha256(repr(value).encode("utf-8")).hexdigest()[:12] not in rendered


@pytest.mark.parametrize(
    "stored",
    [
        # a credential in a field the warning logs BESIDE db_value
        {"country": "US", "subdivision": {"url": f"http://user:{CREDENTIAL}@p.example.com"}},
        # pydantic puts the offending value in the message - for a missing field, the whole object
        {"proxy": f"http://user:{CREDENTIAL}@p.example.com"},
    ],
)
def test_deserialize_warnings_carry_no_credential_in_any_field(stored: dict) -> None:
    """db_value being named is not enough: every OTHER kwarg on the same call is its own
    rendering path, and an exception's text is one of them."""
    logger, patched = _capture_db_utils_logs()
    with patched:
        deserialize_proxy_location(json.dumps(stored))

    assert CREDENTIAL not in str(logger.calls)


def test_a_validated_geotarget_is_not_treated_as_safe_wholesale() -> None:
    """city accepts 100 characters of free text, so validation does not prove the object holds no
    URL. Only country, which validate_country pins to a supported set, is rendered."""
    gt = GeoTarget(country="US", city=f"http://user:{CREDENTIAL}@h")
    rendered = redact_proxy_location(gt)

    assert CREDENTIAL not in rendered
    assert re.fullmatch(r"geo_target:US:[0-9a-f]{12}", rendered)


def test_validation_error_detail_carries_no_input_derived_message() -> None:
    """A custom validator embeds the offending value in its message, and pydantic keeps that msg
    even when input is excluded - so only the fixed type and the field location are emitted."""
    logger, patched = _capture_db_utils_logs()
    with patched:
        deserialize_proxy_location(json.dumps({"country": "pw"}))

    rendered = str(logger.calls)
    assert "pw" not in rendered.replace("proxy_location", "").replace("pw_", "")


def test_enum_allowlist_bound_comes_from_the_set_it_admits() -> None:
    """The bound exists to admit ProxyLocation values and typos of them, not to be a round number.
    Every real value must render; a token-shaped string of the same character class must not."""
    for member in ProxyLocation:
        assert redact_proxy_location(member.value) == member.value
    assert redact_proxy_location("RESIDENTIL_ISP") == "RESIDENTIL_ISP"  # a typo is still nameable
    assert re.fullmatch(r"string:[0-9a-f]{12}", redact_proxy_location("a" * 21))
