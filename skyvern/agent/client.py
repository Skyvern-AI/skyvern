from enum import StrEnum

from skyvern.config import settings
from skyvern.forge.sdk.schemas.task_runs import TaskRun, TaskRunResponse, TaskRunStatus, TaskRunType
from skyvern.forge.sdk.schemas.tasks import ProxyLocation


class RunEngine(StrEnum):
    skyvern_v1 = "skyvern-1.0"
    skyvern_v2 = "skyvern-2.0"


class SkyvernClient:
    def __init__(
        self,
        base_url: str = settings.SKYVERN_BASE_URL,
        api_key: str = settings.SKYVERN_API_KEY,
    ) -> None:
        self.base_url = base_url
        self.api_key = api_key

    async def run_task(
        self,
        goal: str,
        engine: RunEngine = RunEngine.skyvern_v1,
        url: str | None = None,
        webhook_url: str | None = None,
        totp_identifier: str | None = None,
        totp_url: str | None = None,
        title: str | None = None,
        error_code_mapping: dict[str, str] | None = None,
        proxy_location: ProxyLocation | None = None,
        max_steps: int | None = None,
    ) -> TaskRunResponse:
        if engine == RunEngine.skyvern_v1:
            return TaskRunResponse()
        elif engine == RunEngine.skyvern_v2:
            return TaskRunResponse()
        raise ValueError(f"Invalid engine: {engine}")

    async def run_workflow(
        self,
        workflow_id: str,
        webhook_url: str | None = None,
        proxy_location: ProxyLocation | None = None,
    ) -> TaskRunResponse:
        return TaskRunResponse()

    async def get_run(
        self,
        run_id: str,
    ) -> TaskRunResponse:
        return TaskRunResponse()

    async def _get_task_run(self, run_id: str) -> TaskRun:
        return TaskRun()

    async def _convert_task_run_to_task_run_response(self, task_run: TaskRun) -> TaskRunResponse:
        if task_run.task_run_type == TaskRunType.task_v1:
            return TaskRunResponse(
                run_id=task_run.run_id,
                engine=RunEngine.skyvern_v1,
                status=TaskRunStatus.completed,
            )
        elif task_run.task_run_type == TaskRunType.task_v2:
            return TaskRunResponse(
                run_id=task_run.run_id,
                engine=RunEngine.skyvern_v2,
                status=TaskRunStatus.completed,
            )
        raise ValueError(f"Invalid task run type: {task_run.task_run_type}")
