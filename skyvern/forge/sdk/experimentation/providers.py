from abc import ABC, abstractmethod

import structlog

LOG = structlog.get_logger()


class BaseExperimentationProvider(ABC):
    # feature_name -> distinct_id -> result
    result_map: dict[str, dict[str, bool]] = {}

    @abstractmethod
    def is_feature_enabled(self, feature_name: str, distinct_id: str, properties: dict | None = None) -> bool:
        """Check if a specific feature is enabled."""

    def is_feature_enabled_cached(self, feature_name: str, distinct_id: str, properties: dict | None = None) -> bool:
        if feature_name not in self.result_map:
            self.result_map[feature_name] = {}
        if distinct_id not in self.result_map[feature_name]:
            self.result_map[feature_name][distinct_id] = self.is_feature_enabled(feature_name, distinct_id, properties)
        return self.result_map[feature_name][distinct_id]


class NoOpExperimentationProvider(BaseExperimentationProvider):
    def is_feature_enabled(self, feature_name: str, distinct_id: str, properties: dict | None = None) -> bool:
        return False
