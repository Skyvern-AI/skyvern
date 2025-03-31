from skyvern.client.client import AsyncSkyvern
from skyvern.config import settings
from skyvern.forge.sdk.workflow.models.workflow import RunWorkflowResponse
from skyvern.schemas.runs import ProxyLocation, RunEngine, RunResponse


class SkyvernClient:
    def __init__(
        self,
        base_url: str = settings.SKYVERN_BASE_URL,
        api_key: str = settings.SKYVERN_API_KEY,
    ) -> None:
        self.base_url = base_url
        self.api_key = api_key
        self.client = AsyncSkyvern(base_url=base_url, api_key=api_key)

    async def run_task(
        self,
        prompt: str,
        url: str | None = None,
        title: str | None = None,
        engine: RunEngine = RunEngine.skyvern_v2,
        webhook_url: str | None = None,
        totp_identifier: str | None = None,
        totp_url: str | None = None,
        error_code_mapping: dict[str, str] | None = None,
        proxy_location: ProxyLocation | None = None,
        max_steps: int | None = None,
        browser_session_id: str | None = None,
        publish_workflow: bool = False,
    ) -> RunResponse:
        task_run_obj = await self.client.agent.run_task(
            goal=prompt,
            url=url,
            title=title,
            engine=engine,
            webhook_url=webhook_url,
            totp_identifier=totp_identifier,
            totp_url=totp_url,
            error_code_mapping=error_code_mapping,
            proxy_location=proxy_location,
            max_steps=max_steps,
            browser_session_id=browser_session_id,
            publish_workflow=publish_workflow,
        )
        return RunResponse.model_validate(task_run_obj)

    async def run_workflow(
        self,
        workflow_id: str,
        workflow_input: dict | None = None,
        webhook_url: str | None = None,
        proxy_location: ProxyLocation | None = None,
        totp_identifier: str | None = None,
        totp_url: str | None = None,
        browser_session_id: str | None = None,
        template: bool = False,
    ) -> RunWorkflowResponse:
        workflow_run_obj = await self.client.agent.run_workflow(
            workflow_id=workflow_id,
            data=workflow_input,
            webhook_callback_url=webhook_url,
            proxy_location=proxy_location,
            totp_identifier=totp_identifier,
            totp_url=totp_url,
            browser_session_id=browser_session_id,
            template=template,
        )
        return RunWorkflowResponse.model_validate(workflow_run_obj)

    async def get_run(
        self,
        run_id: str,
    ) -> RunResponse:
        run_obj = await self.client.agent.get_run(run_id=run_id)
        return RunResponse.model_validate(run_obj)
