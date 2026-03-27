from __future__ import annotations

from dataclasses import dataclass

import structlog
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from skyvern.config import settings

LOG = structlog.get_logger()


@dataclass
class _ResolvedExporterConfig:
    endpoint: str
    headers: str | None


class OTELSetup:
    _instance: OTELSetup | None = None

    def __init__(self) -> None:
        self._initialized = False

    @classmethod
    def get_instance(cls) -> OTELSetup:
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def _resolve_exporter_config(self) -> _ResolvedExporterConfig | None:
        endpoint = settings.get_otel_exporter_endpoint()
        if not endpoint:
            LOG.warning(
                "OTEL is enabled but no exporter endpoint is configured",
                configured_fields=[
                    "OTEL_EXPORTER_OTLP_ENDPOINT",
                    "LMNR_BASE_URL",
                    "LAMINAR_API_BASE",
                ],
            )
            return None

        headers = settings.get_otel_exporter_headers()
        return _ResolvedExporterConfig(endpoint=endpoint, headers=headers)

    def initialize_tracer_provider(self) -> None:
        if self._initialized:
            return

        exporter_config = self._resolve_exporter_config()
        if exporter_config is None:
            return

        resource = Resource.create({"service.name": settings.OTEL_SERVICE_NAME})
        provider = TracerProvider(resource=resource)

        exporter = OTLPSpanExporter(
            endpoint=exporter_config.endpoint,
            headers=exporter_config.headers,
            insecure=settings.OTEL_EXPORTER_INSECURE,
        )
        provider.add_span_processor(BatchSpanProcessor(exporter))
        trace.set_tracer_provider(provider)

        self._initialized = True
        LOG.info(
            "Initialized OTEL tracer provider",
            service_name=settings.OTEL_SERVICE_NAME,
            exporter_endpoint=exporter_config.endpoint,
            using_custom_headers=bool(exporter_config.headers),
        )
