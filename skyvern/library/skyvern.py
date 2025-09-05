import asyncio
import os
import typing
from typing import Any

import httpx
from dotenv import load_dotenv

from skyvern.client import AsyncSkyvern
from skyvern.client.core.pydantic_utilities import parse_obj_as
from skyvern.client.environment import SkyvernEnvironment
from skyvern.client.types.get_run_response import GetRunResponse
from skyvern.client.types.task_run_response import TaskRunResponse
from skyvern.client.types.workflow_run_response import WorkflowRunResponse
from skyvern.config import settings
from skyvern.forge import app
from skyvern.forge.sdk.core import security, skyvern_context
from skyvern.forge.sdk.core.hashing import generate_url_hash
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.db.enums import OrganizationAuthTokenType
from skyvern.forge.sdk.schemas.organizations import Organization
from skyvern.forge.sdk.schemas.task_v2 import TaskV2, TaskV2Request, TaskV2Status
from skyvern.forge.sdk.schemas.tasks import CreateTaskResponse, Task, TaskRequest, TaskResponse, TaskStatus
from skyvern.forge.sdk.services.org_auth_token_service import API_KEY_LIFETIME
from skyvern.forge.sdk.workflow.models.workflow import WorkflowRunStatus
from skyvern.library.constants import DEFAULT_AGENT_HEARTBEAT_INTERVAL, DEFAULT_AGENT_TIMEOUT
from skyvern.schemas.run_blocks import CredentialType
from skyvern.schemas.runs import CUA_ENGINES, ProxyLocation, RunEngine, RunStatus, RunType
from skyvern.services import run_service, task_v1_service, task_v2_service
from skyvern.utils import migrate_db


class Skyvern(AsyncSkyvern):
    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        cdp_url: str | None = None,
        browser_path: str | None = None,
        browser_type: str | None = None,
        environment: SkyvernEnvironment = SkyvernEnvironment.PRODUCTION,
        timeout: float | None = None,
        follow_redirects: bool | None = True,
        httpx_client: httpx.AsyncClient | None = None,
    ) -> None:
        super().__init__(
            base_url=base_url,
            api_key=api_key,
            x_api_key=api_key,
            environment=environment,
            timeout=timeout,
            follow_redirects=follow_redirects,
            httpx_client=httpx_client,
        )
        if base_url is None and api_key is None:
            if not os.path.exists(".env"):
                raise Exception("No .env file found. Please run 'skyvern init' first to set up your environment.")

            load_dotenv(".env")
            migrate_db()

        self._api_key = api_key
        self._cdp_url = cdp_url
        self._browser_path = browser_path
        self._browser_type = browser_type
        if browser_path:
            # TODO validate browser_path
            # Supported Browsers: Google Chrome, Brave Browser, Microsoft Edge, Firefox
            if "Chrome" in browser_path or "Brave" in browser_path or "Edge" in browser_path:
                self._cdp_url = "http://127.0.0.1:9222"
                settings.BROWSER_TYPE = "cdp-connect"
                settings.BROWSER_REMOTE_DEBUGGING_URL = self._cdp_url
                settings.CHROME_EXECUTABLE_PATH = browser_path
            else:
                raise ValueError(
                    f"Unsupported browser or invalid path: {browser_path}. "
                    "Here's a list of supported browsers Skyvern can connect to: Google Chrome, Brave Browser, Microsoft Edge, Firefox."
                )
        elif cdp_url:
            self._cdp_url = cdp_url
            settings.BROWSER_TYPE = "cdp-connect"
            settings.BROWSER_REMOTE_DEBUGGING_URL = self._cdp_url
        elif base_url is None and api_key is None:
            if not browser_type:
                # if "BROWSER_TYPE" not in os.environ:
                #     raise Exception("browser type is missing")
                browser_type = "chromium-headful"

            self._browser_type = browser_type
            settings.BROWSER_TYPE = browser_type
        elif api_key:
            self._api_key = api_key
        else:
            raise ValueError("Initializing Skyvern failed: api_key must be provided")

    async def get_organization(self) -> Organization:
        organization = await app.DATABASE.get_organization_by_domain("skyvern.local")
        if not organization:
            organization = await app.DATABASE.create_organization(
                organization_name="Skyvern-local",
                domain="skyvern.local",
                max_steps_per_run=10,
                max_retries_per_step=3,
            )
            api_key = security.create_access_token(
                organization.organization_id,
                expires_delta=API_KEY_LIFETIME,
            )
            # generate OrganizationAutoToken
            await app.DATABASE.create_org_auth_token(
                organization_id=organization.organization_id,
                token=api_key,
                token_type=OrganizationAuthTokenType.api,
            )
        return organization

    async def _run_task(
        self,
        organization: Organization,
        task: Task,
        max_steps: int | None = None,
        engine: RunEngine = RunEngine.skyvern_v1,
    ) -> None:
        org_auth_token = await app.DATABASE.get_valid_org_auth_token(
            organization_id=organization.organization_id,
            token_type=OrganizationAuthTokenType.api,
        )

        step = await app.DATABASE.create_step(
            task.task_id,
            order=0,
            retry_index=0,
            organization_id=organization.organization_id,
        )
        updated_task = await app.DATABASE.update_task(
            task.task_id,
            status=TaskStatus.running,
            organization_id=organization.organization_id,
        )
        try:
            context: skyvern_context.SkyvernContext | None = skyvern_context.current()
            current_run_id = context.run_id if context and context.run_id else task.task_id
            skyvern_context.set(
                SkyvernContext(
                    organization_id=organization.organization_id,
                    organization_name=organization.organization_name,
                    task_id=task.task_id,
                    run_id=current_run_id,
                    max_steps_override=max_steps,
                )
            )

            step, _, _ = await app.agent.execute_step(
                organization=organization,
                task=updated_task,
                step=step,
                api_key=org_auth_token.token if org_auth_token else None,
                engine=engine,
            )
        finally:
            skyvern_context.reset()

    async def _run_task_v2(self, organization: Organization, task_v2: TaskV2) -> None:
        # mark task v2 as queued
        await app.DATABASE.update_task_v2(
            task_v2_id=task_v2.observer_cruise_id,
            status=TaskV2Status.queued,
            organization_id=organization.organization_id,
        )
        assert task_v2.workflow_run_id
        await app.DATABASE.update_workflow_run(
            workflow_run_id=task_v2.workflow_run_id,
            status=WorkflowRunStatus.queued,
        )

        await task_v2_service.run_task_v2(
            organization=organization,
            task_v2_id=task_v2.observer_cruise_id,
        )

    async def create_task_v1(
        self,
        task_request: TaskRequest,
    ) -> CreateTaskResponse:
        organization = await self.get_organization()

        created_task = await app.agent.create_task(task_request, organization.organization_id)

        asyncio.create_task(self._run_task(organization, created_task, max_steps=task_request.max_steps_per_run))
        return CreateTaskResponse(task_id=created_task.task_id)

    async def get_task(
        self,
        task_id: str,
    ) -> TaskResponse | None:
        organization = await self.get_organization()
        task = await app.DATABASE.get_task(task_id, organization.organization_id)

        if task is None:
            return None

        latest_step = await app.DATABASE.get_latest_step(task_id, organization_id=organization.organization_id)
        if not latest_step:
            return await app.agent.build_task_response(task=task)

        failure_reason: str | None = None
        if task.status == TaskStatus.failed and (task.failure_reason):
            failure_reason = ""
            if task.failure_reason:
                failure_reason += task.failure_reason or ""
            if latest_step.output is not None and latest_step.output.actions_and_results is not None:
                action_results_string: list[str] = []
                for action, results in latest_step.output.actions_and_results:
                    if len(results) == 0:
                        continue
                    if results[-1].success:
                        continue
                    action_results_string.append(f"{action.action_type} action failed.")

                if len(action_results_string) > 0:
                    failure_reason += "(Exceptions: " + str(action_results_string) + ")"

        return await app.agent.build_task_response(
            task=task, last_step=latest_step, failure_reason=failure_reason, need_browser_log=True
        )

    async def run_task_v1(
        self,
        task_request: TaskRequest,
        timeout_seconds: int = 600,
    ) -> TaskResponse:
        created_task = await self.create_task_v1(task_request)

        async with asyncio.timeout(timeout_seconds):
            while True:
                task_response = await self.get_task(created_task.task_id)
                assert task_response is not None
                if task_response.status.is_final():
                    return task_response
                await asyncio.sleep(1)

    async def observer_task_v_2(self, task_request: TaskV2Request) -> TaskV2:
        organization = await self.get_organization()

        task_v2 = await task_v2_service.initialize_task_v2(
            organization=organization,
            user_prompt=task_request.user_prompt,
            user_url=str(task_request.url) if task_request.url else None,
            totp_identifier=task_request.totp_identifier,
            totp_verification_url=task_request.totp_verification_url,
            webhook_callback_url=task_request.webhook_callback_url,
            proxy_location=task_request.proxy_location,
            publish_workflow=task_request.publish_workflow,
        )

        if not task_v2.workflow_run_id:
            raise Exception("Task v2 missing workflow run id")

        asyncio.create_task(self._run_task_v2(organization, task_v2))
        return task_v2

    async def get_observer_task_v_2(self, task_id: str) -> TaskV2 | None:
        organization = await self.get_organization()
        return await app.DATABASE.get_task_v2(task_id, organization.organization_id)

    async def run_observer_task_v_2(self, task_request: TaskV2Request, timeout_seconds: int = 600) -> TaskV2:
        task_v2 = await self.observer_task_v_2(task_request)

        async with asyncio.timeout(timeout_seconds):
            while True:
                refreshed_task_v2 = await self.get_observer_task_v_2(task_v2.observer_cruise_id)
                assert refreshed_task_v2 is not None
                if refreshed_task_v2.status.is_final():
                    return refreshed_task_v2
                await asyncio.sleep(1)

    ############### officially supported interfaces ###############
    async def get_run(self, run_id: str) -> GetRunResponse | None:
        if not self._api_key:
            organization = await self.get_organization()
            get_run_internal_resp = await run_service.get_run_response(
                run_id, organization_id=organization.organization_id
            )
            if not get_run_internal_resp:
                return None
            return typing.cast(
                GetRunResponse,
                parse_obj_as(
                    type_=GetRunResponse,  # type: ignore
                    object_=get_run_internal_resp.model_dump(),
                ),
            )

        return await super().get_run(run_id)

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
        if not self._api_key:
            if engine == RunEngine.skyvern_v1 or engine in CUA_ENGINES:
                data_extraction_goal = None
                navigation_goal = prompt
                navigation_payload = None
                organization = await self.get_organization()
                task_generation = await task_v1_service.generate_task(
                    user_prompt=prompt,
                    organization=organization,
                )
                url = url or task_generation.url
                navigation_goal = task_generation.navigation_goal or prompt
                navigation_payload = task_generation.navigation_payload
                data_extraction_goal = task_generation.data_extraction_goal
                data_extraction_schema = data_extraction_schema or task_generation.extracted_information_schema

                task_request = TaskRequest(
                    title=title or task_generation.suggested_title,
                    url=url,
                    model=model,
                    navigation_goal=navigation_goal,
                    navigation_payload=navigation_payload,
                    data_extraction_goal=data_extraction_goal,
                    extracted_information_schema=data_extraction_schema,
                    error_code_mapping=error_code_mapping,
                    proxy_location=proxy_location,
                    totp_identifier=totp_identifier,
                    totp_verification_url=totp_url,
                    browser_session_id=browser_session_id,
                )

                created_task = await app.agent.create_task(task_request, organization.organization_id)
                url_hash = generate_url_hash(task_request.url)
                await app.DATABASE.create_task_run(
                    task_run_type=RunType.task_v1,
                    organization_id=organization.organization_id,
                    run_id=created_task.task_id,
                    title=task_request.title,
                    url=task_request.url,
                    url_hash=url_hash,
                )
                try:
                    await self._run_task(organization, created_task, engine=engine)
                    run_obj = await self.get_run(run_id=created_task.task_id)
                except Exception:
                    # TODO: better error handling and logging
                    run_obj = await self.get_run(run_id=created_task.task_id)
                if not run_obj:
                    raise Exception("Failed to get the task run after creating the task.")
                return from_run_to_task_run_response(run_obj)

            elif engine == RunEngine.skyvern_v2:
                # initialize task v2
                organization = await self.get_organization()

                task_v2 = await task_v2_service.initialize_task_v2(
                    organization=organization,
                    user_prompt=prompt,
                    user_url=url,
                    totp_identifier=totp_identifier,
                    totp_verification_url=totp_url,
                    webhook_callback_url=webhook_url,
                    proxy_location=proxy_location,
                    publish_workflow=False,
                    extracted_information_schema=data_extraction_schema,
                    error_code_mapping=error_code_mapping,
                    create_task_run=True,
                    model=model,
                )

                await self._run_task_v2(organization, task_v2)
                run_obj = await self.get_run(run_id=task_v2.observer_cruise_id)
                if not run_obj:
                    raise Exception("Failed to get the task run after creating the task.")
                return from_run_to_task_run_response(run_obj)
            else:
                raise ValueError("Local mode is not supported for this method")

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
        if not self._api_key:
            raise ValueError(
                "Local mode is not supported for run_workflow. Please instantiate Skyvern with an API key like this: Skyvern(api_key='your-api-key')"
            )
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
        if not self._api_key:
            raise ValueError(
                "Local mode is not supported for login. Please instantiate Skyvern with an API key like this: Skyvern(api_key='your-api-key')"
            )
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


def from_run_to_task_run_response(run_obj: GetRunResponse) -> TaskRunResponse:
    return TaskRunResponse.model_validate(run_obj.model_dump())
