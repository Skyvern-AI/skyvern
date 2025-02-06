from dotenv import load_dotenv

from skyvern.agent.parameter import TaskV1Request, TaskV2Request
from skyvern.forge import app
from skyvern.forge.sdk.core import security, skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.db.enums import OrganizationAuthTokenType
from skyvern.forge.sdk.schemas.observers import ObserverTask, ObserverTaskStatus
from skyvern.forge.sdk.schemas.organizations import Organization
from skyvern.forge.sdk.schemas.tasks import TaskResponse, TaskStatus
from skyvern.forge.sdk.services import observer_service
from skyvern.forge.sdk.services.org_auth_token_service import API_KEY_LIFETIME
from skyvern.forge.sdk.workflow.models.workflow import WorkflowRunStatus
from skyvern.utils import migrate_db


class Agent:
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

    async def run_task_v1(self, task_request: TaskV1Request) -> TaskResponse:
        organization = await self._get_organization()

        org_auth_token = await app.DATABASE.get_valid_org_auth_token(
            organization_id=organization.organization_id,
            token_type=OrganizationAuthTokenType.api,
        )

        created_task = await app.agent.create_task(task_request, organization.organization_id)

        skyvern_context.set(
            SkyvernContext(
                organization_id=organization.organization_id,
                task_id=created_task.task_id,
                max_steps_override=task_request.max_steps,
            )
        )

        step = await app.DATABASE.create_step(
            created_task.task_id,
            order=0,
            retry_index=0,
            organization_id=organization.organization_id,
        )
        updated_task = await app.DATABASE.update_task(
            created_task.task_id,
            status=TaskStatus.running,
            organization_id=organization.organization_id,
        )

        step, _, _ = await app.agent.execute_step(
            organization=organization,
            task=updated_task,
            step=step,
            api_key=org_auth_token.token if org_auth_token else None,
        )

        refreshed_task = await app.DATABASE.get_task(created_task.task_id, organization.organization_id)
        if refreshed_task:
            updated_task = refreshed_task

        failure_reason: str | None = None
        if updated_task.status == TaskStatus.failed and (step.output or updated_task.failure_reason):
            failure_reason = ""
            if updated_task.failure_reason:
                failure_reason += updated_task.failure_reason or ""
            if step.output is not None and step.output.actions_and_results is not None:
                action_results_string: list[str] = []
                for action, results in step.output.actions_and_results:
                    if len(results) == 0:
                        continue
                    if results[-1].success:
                        continue
                    action_results_string.append(f"{action.action_type} action failed.")

                if len(action_results_string) > 0:
                    failure_reason += "(Exceptions: " + str(action_results_string) + ")"
        return await app.agent.build_task_response(
            task=updated_task, last_step=step, failure_reason=failure_reason, need_browser_log=True
        )

    async def run_task_v2(self, task_request: TaskV2Request) -> ObserverTask:
        organization = await self._get_organization()

        observer_task = await observer_service.initialize_observer_task(
            organization=organization,
            user_prompt=task_request.user_prompt,
            user_url=str(task_request.url) if task_request.url else None,
            totp_identifier=task_request.totp_identifier,
            totp_verification_url=task_request.totp_verification_url,
            webhook_callback_url=task_request.webhook_callback_url,
            proxy_location=task_request.proxy_location,
            publish_workflow=task_request.publish_workflow,
        )

        if not observer_task.workflow_run_id:
            raise Exception("Observer cruise missing workflow run id")

        # mark observer cruise as queued
        await app.DATABASE.update_observer_cruise(
            observer_cruise_id=observer_task.observer_cruise_id,
            status=ObserverTaskStatus.queued,
            organization_id=organization.organization_id,
        )
        await app.DATABASE.update_workflow_run(
            workflow_run_id=observer_task.workflow_run_id,
            status=WorkflowRunStatus.queued,
        )

        await observer_service.run_observer_task(
            organization=organization,
            observer_cruise_id=observer_task.observer_cruise_id,
            max_iterations_override=task_request.max_iterations,
        )

        refreshed_observer_task = await app.DATABASE.get_observer_cruise(
            observer_cruise_id=observer_task.observer_cruise_id, organization_id=organization.organization_id
        )
        if refreshed_observer_task:
            return refreshed_observer_task

        return observer_task
