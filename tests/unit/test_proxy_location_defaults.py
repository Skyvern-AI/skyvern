import pytest

from skyvern.config import settings
from skyvern.schemas.proxy_location import GeoTarget, ProxyLocation, runtime_proxy_location


def test_runtime_proxy_location_preserves_legacy_default_when_rollout_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "RUNTIME_PROXY_DEFAULT_NONE_ENABLED", False)

    assert runtime_proxy_location(None) == ProxyLocation.RESIDENTIAL


def test_runtime_proxy_location_defaults_missing_value_to_no_proxy_when_rollout_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "RUNTIME_PROXY_DEFAULT_NONE_ENABLED", True)

    assert runtime_proxy_location(None) == ProxyLocation.NONE


def test_runtime_proxy_location_preserves_explicit_proxy_values() -> None:
    assert runtime_proxy_location(ProxyLocation.RESIDENTIAL) == ProxyLocation.RESIDENTIAL
    assert runtime_proxy_location(ProxyLocation.NONE) == ProxyLocation.NONE


def test_runtime_proxy_location_preserves_geotarget_and_custom_proxy_values() -> None:
    geo_target = GeoTarget(country="GB", city="London")
    custom_proxy = {"url": "http://proxy.example.com:8080"}

    assert runtime_proxy_location(geo_target) is geo_target
    assert runtime_proxy_location(custom_proxy) is custom_proxy
