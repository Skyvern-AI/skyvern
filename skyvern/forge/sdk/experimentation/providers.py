from abc import ABC, abstractmethod

import structlog

LOG = structlog.get_logger()


class BaseExperimentationProvider(ABC):
    # feature_name -> distinct_id -> result
    result_map: dict[str, dict[str, bool]] = {}
    variant_map: dict[str, dict[str, str | None]] = {}

    @abstractmethod
    def is_feature_enabled(self, feature_name: str, distinct_id: str, properties: dict | None = None) -> bool:
        """Check if a specific feature is enabled."""

    def is_feature_enabled_cached(self, feature_name: str, distinct_id: str, properties: dict | None = None) -> bool:
        if feature_name not in self.result_map:
            self.result_map[feature_name] = {}
        if distinct_id not in self.result_map[feature_name]:
            feature_flag_value = self.is_feature_enabled(feature_name, distinct_id, properties)
            self.result_map[feature_name][distinct_id] = feature_flag_value
            if feature_flag_value:
                LOG.info("Feature flag is enabled", flag=feature_name, distinct_id=distinct_id)

        return self.result_map[feature_name][distinct_id]

    @abstractmethod
    def get_value(self, feature_name: str, distinct_id: str, properties: dict | None = None) -> str | None:
        """Get the value of a feature."""

    def get_value_cached(self, feature_name: str, distinct_id: str, properties: dict | None = None) -> str | None:
        """Get the value of a feature."""
        if feature_name not in self.variant_map:
            self.variant_map[feature_name] = {}
        if distinct_id not in self.variant_map[feature_name]:
            variant = self.get_value(feature_name, distinct_id, properties)
            self.variant_map[feature_name][distinct_id] = variant
            if variant:
                LOG.info("Feature is found", flag=feature_name, distinct_id=distinct_id, variant=variant)
        return self.variant_map[feature_name][distinct_id]


class NoOpExperimentationProvider(BaseExperimentationProvider):
    def is_feature_enabled(self, feature_name: str, distinct_id: str, properties: dict | None = None) -> bool:
        return False

    def get_value(self, feature_name: str, distinct_id: str, properties: dict | None = None) -> str | None:
        return None
