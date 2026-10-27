import abc

from skyvern.forge.sdk.schemas.organizations import Organization


class PermissionChecker(abc.ABC):
    @abc.abstractmethod
    async def check(self, organization: Organization, browser_session_id: str | None = None) -> None:
        pass

    async def check_browser_session_access(self, organization: Organization) -> None:
        return


class NoopPermissionChecker(PermissionChecker):
    async def check(self, organization: Organization, browser_session_id: str | None = None) -> None:
        return
