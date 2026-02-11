from abc import ABC, abstractmethod

import structlog
from cachetools import TTLCache

LOG = structlog.get_logger()

EXPERIMENTATION_CACHE_TTL = 300  # seconds (5 minutes)
EXPERIMENTATION_CACHE_MAX_SIZE = 100000  # Max entries per cache


class BaseExperimentationProvider(ABC):
    def __init__(self) -> None:
        # Cache with composite key (feature_name, distinct_id) for per-entry TTL expiration
        self.result_map: TTLCache = TTLCache(maxsize=EXPERIMENTATION_CACHE_MAX_SIZE, ttl=EXPERIMENTATION_CACHE_TTL)
        self.variant_map: TTLCache = TTLCache(maxsize=EXPERIMENTATION_CACHE_MAX_SIZE, ttl=EXPERIMENTATION_CACHE_TTL)
        self.payload_map: TTLCache = TTLCache(maxsize=EXPERIMENTATION_CACHE_MAX_SIZE, ttl=EXPERIMENTATION_CACHE_TTL)

    @abstractmethod
    async def is_feature_enabled(self, feature_name: str, distinct_id: str, properties: dict | None = None) -> bool:
        """Check if a specific feature is enabled."""

    async def is_feature_enabled_cached(
        self, feature_name: str, distinct_id: str, properties: dict | None = None
    ) -> bool:
        cache_key = (feature_name, distinct_id)
        if cache_key not in self.result_map:
            feature_flag_value = await self.is_feature_enabled(feature_name, distinct_id, properties)
            self.result_map[cache_key] = feature_flag_value
            if feature_flag_value:
                LOG.info("Feature flag is enabled", flag=feature_name, distinct_id=distinct_id)

        return self.result_map[cache_key]

    @abstractmethod
    async def get_value(self, feature_name: str, distinct_id: str, properties: dict | None = None) -> str | None:
        """Get the value of a feature."""

    @abstractmethod
    async def get_payload(self, feature_name: str, distinct_id: str, properties: dict | None = None) -> str | None:
        """Get the payload for a feature flag if it exists."""

    async def get_value_cached(self, feature_name: str, distinct_id: str, properties: dict | None = None) -> str | None:
        """Get the value of a feature."""
        cache_key = (feature_name, distinct_id)
        if cache_key not in self.variant_map:
            variant = await self.get_value(feature_name, distinct_id, properties)
            self.variant_map[cache_key] = variant
            if variant:
                LOG.info("Feature is found", flag=feature_name, distinct_id=distinct_id, variant=variant)
        return self.variant_map[cache_key]

    async def get_payload_cached(
        self, feature_name: str, distinct_id: str, properties: dict | None = None
    ) -> str | None:
        """Get the payload for a feature flag if it exists."""
        cache_key = (feature_name, distinct_id)
        if cache_key not in self.payload_map:
            payload = await self.get_payload(feature_name, distinct_id, properties)
            self.payload_map[cache_key] = payload
            if payload:
                LOG.info("Feature payload is found", flag=feature_name, distinct_id=distinct_id, payload=payload)
        return self.payload_map[cache_key]


class NoOpExperimentationProvider(BaseExperimentationProvider):
    def __init__(self) -> None:
        super().__init__()

    async def is_feature_enabled(self, feature_name: str, distinct_id: str, properties: dict | None = None) -> bool:
        return False

    async def get_value(self, feature_name: str, distinct_id: str, properties: dict | None = None) -> str | None:
        return None

    async def get_payload(self, feature_name: str, distinct_id: str, properties: dict | None = None) -> str | None:
        return None
