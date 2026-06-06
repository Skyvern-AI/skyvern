from skyvern.forge.sdk.db.utils import deserialize_proxy_location, serialize_proxy_location
from skyvern.schemas.runs import GeoTarget, ProxyLocation
from skyvern.webeye.browser_factory import BrowserContextFactory, _redact_proxy_url


def test_redact_proxy_url_strips_password() -> None:
    assert _redact_proxy_url("http://user:secret@proxy.example.com:8080") == "http://user:***@proxy.example.com:8080"


def test_redact_proxy_url_keeps_username_only() -> None:
    assert _redact_proxy_url("http://user@proxy.example.com:8080") == "http://user@proxy.example.com:8080"


def test_redact_proxy_url_no_creds() -> None:
    assert _redact_proxy_url("http://proxy.example.com:8080") == "http://proxy.example.com:8080"


def test_redact_proxy_url_invalid() -> None:
    assert _redact_proxy_url("not-a-url") == "<redacted>"


def test_build_browser_args_custom_proxy_url_takes_precedence() -> None:
    args = BrowserContextFactory.build_browser_args(
        proxy_location={"url": "http://user:secret@proxy.example.com:8080"},
    )
    assert args["proxy"] == {"server": "http://user:secret@proxy.example.com:8080"}


def test_build_browser_args_invalid_custom_proxy_url_ignored() -> None:
    args = BrowserContextFactory.build_browser_args(
        proxy_location={"url": "not-a-valid-proxy-url"},
    )
    assert "proxy" not in args


def test_build_browser_args_sets_record_video_size_to_viewport() -> None:
    args = BrowserContextFactory.build_browser_args()

    assert args["record_video_size"] == args["viewport"]


def test_deserialize_proxy_location_custom_url_returns_dict() -> None:
    result = deserialize_proxy_location('{"url": "http://user:pass@proxy.example.com:8080"}')
    assert result == {"url": "http://user:pass@proxy.example.com:8080"}


def test_proxy_location_db_round_trip_custom_url() -> None:
    original = {"url": "http://user:pass@proxy.example.com:8080"}
    serialized = serialize_proxy_location(original)
    assert serialized is not None
    assert deserialize_proxy_location(serialized) == original


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
