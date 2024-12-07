import abc

from skyvern.forge.sdk.schemas.organizations import Organization


class PermissionChecker(abc.ABC):
    @abc.abstractmethod
    async def check(self, organization: Organization) -> None:
        pass


class NoopPermissionChecker(PermissionChecker):
    async def check(self, organization: Organization) -> None:
        return
