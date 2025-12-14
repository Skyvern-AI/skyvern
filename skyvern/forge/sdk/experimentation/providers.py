from abc import ABC, abstractmethod

import structlog

LOG = structlog.get_logger()


class BaseExperimentationProvider(ABC):
    # feature_name -> distinct_id -> result
    result_map: dict[str, dict[str, bool]] = {}
    variant_map: dict[str, dict[str, str | None]] = {}
    payload_map: dict[str, dict[str, str | None]] = {}

    @abstractmethod
    async def is_feature_enabled(self, feature_name: str, distinct_id: str, properties: dict | None = None) -> bool:
        """Check if a specific feature is enabled."""

    async def is_feature_enabled_cached(
        self, feature_name: str, distinct_id: str, properties: dict | None = None
    ) -> bool:
        if feature_name not in self.result_map:
            self.result_map[feature_name] = {}
        if distinct_id not in self.result_map[feature_name]:
            feature_flag_value = await self.is_feature_enabled(feature_name, distinct_id, properties)
            self.result_map[feature_name][distinct_id] = feature_flag_value
            if feature_flag_value:
                LOG.info("Feature flag is enabled", flag=feature_name, distinct_id=distinct_id)

        return self.result_map[feature_name][distinct_id]

    @abstractmethod
    async def get_value(self, feature_name: str, distinct_id: str, properties: dict | None = None) -> str | None:
        """Get the value of a feature."""

    @abstractmethod
    async def get_payload(self, feature_name: str, distinct_id: str, properties: dict | None = None) -> str | None:
        """Get the payload for a feature flag if it exists."""

    async def get_value_cached(self, feature_name: str, distinct_id: str, properties: dict | None = None) -> str | None:
        """Get the value of a feature."""
        if feature_name not in self.variant_map:
            self.variant_map[feature_name] = {}
        if distinct_id not in self.variant_map[feature_name]:
            variant = await self.get_value(feature_name, distinct_id, properties)
            self.variant_map[feature_name][distinct_id] = variant
            if variant:
                LOG.info("Feature is found", flag=feature_name, distinct_id=distinct_id, variant=variant)
        return self.variant_map[feature_name][distinct_id]

    async def get_payload_cached(
        self, feature_name: str, distinct_id: str, properties: dict | None = None
    ) -> str | None:
        """Get the payload for a feature flag if it exists."""
        if feature_name not in self.payload_map:
            self.payload_map[feature_name] = {}
        if distinct_id not in self.payload_map[feature_name]:
            payload = await self.get_payload(feature_name, distinct_id, properties)
            self.payload_map[feature_name][distinct_id] = payload
            if payload:
                LOG.info("Feature payload is found", flag=feature_name, distinct_id=distinct_id, payload=payload)
        return self.payload_map[feature_name][distinct_id]


class NoOpExperimentationProvider(BaseExperimentationProvider):
    async def is_feature_enabled(self, feature_name: str, distinct_id: str, properties: dict | None = None) -> bool:
        return False

    async def get_value(self, feature_name: str, distinct_id: str, properties: dict | None = None) -> str | None:
        return None

    async def get_payload(self, feature_name: str, distinct_id: str, properties: dict | None = None) -> str | None:
        return None
