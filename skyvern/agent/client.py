from skyvern.client.client import AsyncSkyvern
from skyvern.config import settings
from skyvern.schemas.runs import ProxyLocation, RunEngine, RunResponse, RunType, TaskRunResponse, WorkflowRunResponse


class SkyvernClient:
    def __init__(
        self,
        base_url: str = settings.SKYVERN_BASE_URL,
        api_key: str = settings.SKYVERN_API_KEY,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self.base_url = base_url
        self.api_key = api_key
        self.client = AsyncSkyvern(base_url=base_url, api_key=api_key)
        self.extra_headers = extra_headers or {}
        self.user_agent = None
        if "X-User-Agent" in self.extra_headers:
            self.user_agent = self.extra_headers["X-User-Agent"]
        elif "x-user-agent" in self.extra_headers:
            self.user_agent = self.extra_headers["x-user-agent"]

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
    ) -> TaskRunResponse:
        task_run_obj = await self.client.agent.run_task(
            prompt=prompt,
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
            user_agent=self.user_agent,
        )
        return TaskRunResponse.model_validate(task_run_obj.dict())

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
    ) -> WorkflowRunResponse:
        workflow_run_obj = await self.client.agent.run_workflow(
            workflow_id=workflow_id,
            data=workflow_input,
            webhook_callback_url=webhook_url,
            proxy_location=proxy_location,
            totp_identifier=totp_identifier,
            totp_url=totp_url,
            browser_session_id=browser_session_id,
            template=template,
            user_agent=self.user_agent,
        )
        return WorkflowRunResponse.model_validate(workflow_run_obj.dict())

    async def get_run(
        self,
        run_id: str,
    ) -> RunResponse:
        run_obj = await self.client.agent.get_run(run_id=run_id)
        if run_obj.run_type in [RunType.task_v1, RunType.task_v2, RunType.openai_cua, RunType.anthropic_cua]:
            return TaskRunResponse.model_validate(run_obj.dict())
        elif run_obj.run_type == RunType.workflow_run:
            return WorkflowRunResponse.model_validate(run_obj.dict())
        raise ValueError(f"Invalid run type: {run_obj.run_type}")
