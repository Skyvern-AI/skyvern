from skyvern.forge.sdk.notification.base import BaseNotificationRegistry
from skyvern.forge.sdk.notification.local import LocalNotificationRegistry


class NotificationRegistryFactory:
    __registry: BaseNotificationRegistry = LocalNotificationRegistry()

    @staticmethod
    def set_registry(registry: BaseNotificationRegistry) -> None:
        NotificationRegistryFactory.__registry = registry

    @staticmethod
    def get_registry() -> BaseNotificationRegistry:
        return NotificationRegistryFactory.__registry
