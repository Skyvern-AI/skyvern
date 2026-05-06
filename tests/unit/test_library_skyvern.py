from skyvern.schemas import runs
from skyvern.schemas.proxy_location import (
    SUPPORTED_GEO_COUNTRIES,
    GeoTarget,
    ProxyLocation,
    proxy_location_to_request,
)


def test_runs_reexports_proxy_location_types() -> None:
    assert runs.GeoTarget is GeoTarget
    assert runs.ProxyLocation is ProxyLocation
    assert runs.SUPPORTED_GEO_COUNTRIES is SUPPORTED_GEO_COUNTRIES
    assert runs.proxy_location_to_request is proxy_location_to_request


def test_proxy_location_to_request_dumps_geotarget_values() -> None:
    assert proxy_location_to_request(GeoTarget(country="us")) == {
        "country": "US",
        "subdivision": None,
        "city": None,
    }


def test_geotarget_schema_keeps_public_description() -> None:
    assert GeoTarget.model_json_schema()["description"]


def test_proxy_location_to_request_preserves_non_geotarget_values() -> None:
    location = {"country": "GB"}

    assert proxy_location_to_request(None) is None
    assert proxy_location_to_request(ProxyLocation.RESIDENTIAL) is ProxyLocation.RESIDENTIAL
    assert proxy_location_to_request(location) is location
