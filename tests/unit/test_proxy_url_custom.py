import pytest
from pydantic import ValidationError

from skyvern.config import settings
from skyvern.forge.agent_functions import AgentFunction
from skyvern.forge.sdk.db.utils import deserialize_proxy_location, serialize_proxy_location
from skyvern.forge.sdk.schemas.browser_profiles import UpdateBrowserProfileRequest
from skyvern.forge.sdk.schemas.credentials import UpdateCredentialRequest
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
