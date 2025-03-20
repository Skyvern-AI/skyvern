from enum import StrEnum
from typing import Any

import httpx

from skyvern.config import settings
from skyvern.exceptions import SkyvernClientException
from skyvern.forge.sdk.schemas.task_runs import TaskRunResponse
from skyvern.forge.sdk.schemas.tasks import ProxyLocation
from skyvern.forge.sdk.workflow.models.workflow import RunWorkflowResponse, WorkflowRunResponse


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
        workflow_input: dict | None = None,
        webhook_url: str | None = None,
        proxy_location: ProxyLocation | None = None,
        totp_identifier: str | None = None,
        totp_url: str | None = None,
    ) -> RunWorkflowResponse:
        data: dict[str, Any] = {
            "webhook_callback_url": webhook_url,
            "proxy_location": proxy_location,
            "totp_identifier": totp_identifier,
            "totp_url": totp_url,
        }
        if workflow_input:
            data["data"] = workflow_input
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{self.base_url}/api/v1/workflows/{workflow_id}/run",
                headers={"x-api-key": self.api_key},
                json=data,
            )
            if response.status_code != 200:
                raise SkyvernClientException(
                    f"Failed to run workflow: {response.text}",
                    status_code=response.status_code,
                )
            return RunWorkflowResponse.model_validate(response.json())

    async def get_run(
        self,
        run_id: str,
    ) -> TaskRunResponse:
        return TaskRunResponse()

    async def get_workflow_run(
        self,
        workflow_run_id: str,
    ) -> WorkflowRunResponse:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{self.base_url}/api/v1/workflows/runs/{workflow_run_id}",
                headers={"x-api-key": self.api_key},
                timeout=60,
            )
            if response.status_code != 200:
                raise SkyvernClientException(
                    f"Failed to get workflow run: {response.text}",
                    status_code=response.status_code,
                )
            return WorkflowRunResponse.model_validate(response.json())
