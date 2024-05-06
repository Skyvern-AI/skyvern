from abc import ABC, abstractmethod

import structlog

LOG = structlog.get_logger()


class BaseExperimentationProvider(ABC):
    @abstractmethod
    def is_feature_enabled(self, feature_name: str, distinct_id: str) -> bool:
        """Check if a specific feature is enabled."""


class NoOpExperimentationProvider(BaseExperimentationProvider):
    def is_feature_enabled(self, feature_name: str, distinct_id: str) -> bool:
        return False
