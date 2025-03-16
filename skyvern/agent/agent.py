import asyncio

from dotenv import load_dotenv

from skyvern.forge import app
from skyvern.forge.sdk.core import security, skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.db.enums import OrganizationAuthTokenType
from skyvern.forge.sdk.schemas.organizations import Organization
from skyvern.forge.sdk.schemas.task_v2 import TaskV2, TaskV2Request, TaskV2Status
from skyvern.forge.sdk.schemas.tasks import CreateTaskResponse, Task, TaskRequest, TaskResponse, TaskStatus
from skyvern.forge.sdk.services import task_v2_service
from skyvern.forge.sdk.services.org_auth_token_service import API_KEY_LIFETIME
from skyvern.forge.sdk.workflow.models.workflow import WorkflowRunStatus
from skyvern.utils import migrate_db


class SkyvernAgent:
    def __init__(self) -> None:
        load_dotenv(".env")
        migrate_db()

    async def _get_organization(self) -> Organization:
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

    async def _run_task(self, organization: Organization, task: Task) -> None:
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

        step, _, _ = await app.agent.execute_step(
            organization=organization,
            task=updated_task,
            step=step,
            api_key=org_auth_token.token if org_auth_token else None,
        )

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

    async def create_task(
        self,
        task_request: TaskRequest,
    ) -> CreateTaskResponse:
        organization = await self._get_organization()

        created_task = await app.agent.create_task(task_request, organization.organization_id)
        skyvern_context.set(
            SkyvernContext(
                organization_id=organization.organization_id,
                task_id=created_task.task_id,
                max_steps_override=created_task.max_steps_per_run,
            )
        )

        asyncio.create_task(self._run_task(organization, created_task))
        return CreateTaskResponse(task_id=created_task.task_id)

    async def get_task(
        self,
        task_id: str,
    ) -> TaskResponse | None:
        organization = await self._get_organization()
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

    async def run_task(
        self,
        task_request: TaskRequest,
        timeout_seconds: int = 600,
    ) -> TaskResponse:
        created_task = await self.create_task(task_request)

        async with asyncio.timeout(timeout_seconds):
            while True:
                task_response = await self.get_task(created_task.task_id)
                assert task_response is not None
                if task_response.status.is_final():
                    return task_response
                await asyncio.sleep(1)

    async def observer_task_v_2(self, task_request: TaskV2Request) -> TaskV2:
        organization = await self._get_organization()

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
        organization = await self._get_organization()
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
