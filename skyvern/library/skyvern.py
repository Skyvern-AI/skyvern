import asyncio
from typing import Any

from skyvern.client import AsyncSkyvern
from skyvern.client.types.task_run_response import TaskRunResponse
from skyvern.client.types.workflow_run_response import WorkflowRunResponse
from skyvern.library.constants import DEFAULT_AGENT_HEARTBEAT_INTERVAL, DEFAULT_AGENT_TIMEOUT
from skyvern.schemas.run_blocks import CredentialType
from skyvern.schemas.runs import ProxyLocation, RunEngine, RunStatus


class Skyvern(AsyncSkyvern):
    async def run_task(
        self,
        prompt: str,
        engine: RunEngine = RunEngine.skyvern_v2,
        model: dict[str, Any] | None = None,
        url: str | None = None,
        webhook_url: str | None = None,
        totp_identifier: str | None = None,
        totp_url: str | None = None,
        title: str | None = None,
        error_code_mapping: dict[str, str] | None = None,
        data_extraction_schema: dict[str, Any] | str | None = None,
        proxy_location: ProxyLocation | None = None,
        max_steps: int | None = None,
        wait_for_completion: bool = False,
        timeout: float = DEFAULT_AGENT_TIMEOUT,
        browser_session_id: str | None = None,
        user_agent: str | None = None,
    ) -> TaskRunResponse:
        task_run = await super().run_task(
            prompt=prompt,
            engine=engine,
            url=url,
            webhook_url=webhook_url,
            totp_identifier=totp_identifier,
            totp_url=totp_url,
            title=title,
            error_code_mapping=error_code_mapping,
            proxy_location=proxy_location,
            max_steps=max_steps,
            browser_session_id=browser_session_id,
            user_agent=user_agent,
        )

        if wait_for_completion:
            async with asyncio.timeout(timeout):
                while True:
                    task_run = await super().get_run(task_run.run_id)
                    if RunStatus(task_run.status).is_final():
                        break
                    await asyncio.sleep(DEFAULT_AGENT_HEARTBEAT_INTERVAL)
        return TaskRunResponse.model_validate(task_run.model_dump())

    async def run_workflow(
        self,
        workflow_id: str,
        parameters: dict[str, Any] | None = None,
        template: bool | None = None,
        title: str | None = None,
        proxy_location: ProxyLocation | None = None,
        webhook_url: str | None = None,
        totp_url: str | None = None,
        totp_identifier: str | None = None,
        browser_session_id: str | None = None,
        wait_for_completion: bool = False,
        timeout: float = DEFAULT_AGENT_TIMEOUT,
    ) -> WorkflowRunResponse:
        workflow_run = await super().run_workflow(
            workflow_id=workflow_id,
            parameters=parameters,
            template=template,
            title=title,
            proxy_location=proxy_location,
            webhook_url=webhook_url,
            totp_url=totp_url,
            totp_identifier=totp_identifier,
            browser_session_id=browser_session_id,
        )
        if wait_for_completion:
            async with asyncio.timeout(timeout):
                while True:
                    workflow_run = await super().get_run(workflow_run.run_id)
                    if RunStatus(workflow_run.status).is_final():
                        break
                    await asyncio.sleep(DEFAULT_AGENT_HEARTBEAT_INTERVAL)
        return WorkflowRunResponse.model_validate(workflow_run.model_dump())

    async def login(
        self,
        credential_type: CredentialType,
        *,
        url: str | None = None,
        credential_id: str | None = None,
        bitwarden_collection_id: str | None = None,
        bitwarden_item_id: str | None = None,
        onepassword_vault_id: str | None = None,
        onepassword_item_id: str | None = None,
        prompt: str | None = None,
        webhook_url: str | None = None,
        proxy_location: ProxyLocation | None = None,
        totp_identifier: str | None = None,
        totp_url: str | None = None,
        browser_session_id: str | None = None,
        extra_http_headers: dict[str, str] | None = None,
        wait_for_completion: bool = False,
        timeout: float = DEFAULT_AGENT_TIMEOUT,
    ) -> None:
        workflow_run = await super().login(
            credential_type=credential_type,
            url=url,
            credential_id=credential_id,
            bitwarden_collection_id=bitwarden_collection_id,
            bitwarden_item_id=bitwarden_item_id,
            onepassword_vault_id=onepassword_vault_id,
            onepassword_item_id=onepassword_item_id,
            prompt=prompt,
            webhook_url=webhook_url,
            proxy_location=proxy_location,
            totp_identifier=totp_identifier,
            totp_url=totp_url,
            browser_session_id=browser_session_id,
            extra_http_headers=extra_http_headers,
        )
        if wait_for_completion:
            async with asyncio.timeout(timeout):
                while True:
                    workflow_run = await super().get_run(workflow_run.run_id)
                    if RunStatus(workflow_run.status).is_final():
                        break
                    await asyncio.sleep(DEFAULT_AGENT_HEARTBEAT_INTERVAL)
        return WorkflowRunResponse.model_validate(workflow_run.model_dump())
