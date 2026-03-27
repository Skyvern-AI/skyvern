from skyvern.config import Settings


def test_get_otel_exporter_endpoint_uses_explicit_endpoint() -> None:
    settings = Settings(OTEL_EXPORTER_OTLP_ENDPOINT="http://otel-collector:4317")

    assert settings.get_otel_exporter_endpoint() == "http://otel-collector:4317"


def test_get_otel_exporter_endpoint_derives_from_laminar_base() -> None:
    settings = Settings(LAMINAR_API_BASE="http://laminar-app:8443")

    assert settings.get_otel_exporter_endpoint() == "http://laminar-app:8443/v1/traces"


def test_get_otel_exporter_endpoint_preserves_traces_suffix() -> None:
    settings = Settings(LMNR_BASE_URL="http://laminar-app:8443/v1/traces")

    assert settings.get_otel_exporter_endpoint() == "http://laminar-app:8443/v1/traces"


def test_get_otel_exporter_headers_prefers_explicit_headers() -> None:
    settings = Settings(
        OTEL_EXPORTER_OTLP_HEADERS="authorization=Bearer existing-token",
        LMNR_PROJECT_API_KEY="project-api-key",
    )

    assert settings.get_otel_exporter_headers() == "authorization=Bearer existing-token"


def test_get_otel_exporter_headers_derives_from_api_key() -> None:
    settings = Settings(LAMINAR_API_KEY="project-api-key")

    assert settings.get_otel_exporter_headers() == (
        "authorization=Bearer project-api-key,x-api-key=project-api-key"
    )
