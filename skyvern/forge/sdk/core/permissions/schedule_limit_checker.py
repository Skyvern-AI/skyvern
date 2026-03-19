import abc

from skyvern.forge.sdk.schemas.organizations import Organization


class ScheduleLimitChecker(abc.ABC):
    @abc.abstractmethod
    async def check_schedule_limit(
        self, organization: Organization, workflow_permanent_id: str, current_count: int
    ) -> None: ...

    @abc.abstractmethod
    async def get_schedule_limit(self, organization: Organization, workflow_permanent_id: str) -> int | None: ...


class NoopScheduleLimitChecker(ScheduleLimitChecker):
    async def check_schedule_limit(
        self, organization: Organization, workflow_permanent_id: str, current_count: int
    ) -> None:
        return  # unlimited in OSS

    async def get_schedule_limit(self, organization: Organization, workflow_permanent_id: str) -> int | None:
        return None  # unlimited in OSS


class ScheduleLimitCheckerFactory:
    __instance: ScheduleLimitChecker = NoopScheduleLimitChecker()

    @staticmethod
    def get_instance() -> ScheduleLimitChecker:
        return ScheduleLimitCheckerFactory.__instance

    @staticmethod
    def set_instance(checker: ScheduleLimitChecker) -> None:
        ScheduleLimitCheckerFactory.__instance = checker
