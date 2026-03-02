"""Abstract base for notification registries."""

import asyncio
from abc import ABC, abstractmethod


class BaseNotificationRegistry(ABC):
    """Abstract pub/sub registry scoped by organization.

    Implementations must fan-out: a single publish call delivers the
    message to every active subscriber for that organization.
    """

    @abstractmethod
    def subscribe(self, organization_id: str) -> asyncio.Queue[dict]: ...

    @abstractmethod
    def unsubscribe(self, organization_id: str, queue: asyncio.Queue[dict]) -> None: ...

    @abstractmethod
    def publish(self, organization_id: str, message: dict) -> None: ...
