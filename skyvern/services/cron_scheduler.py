import asyncio
from zoneinfo import ZoneInfo

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from skyvern.forge import app
from skyvern.forge.sdk.schemas.organizations import Organization
from skyvern.forge.sdk.workflow.models.workflow import Workflow, WorkflowRequestBody
from skyvern.services import workflow_service

LOG = structlog.get_logger(__name__)

scheduler = AsyncIOScheduler()

async def schedule_workflow(workflow: Workflow, organization: Organization) -> None:
    cron_schedule = getattr(workflow, "cron_schedule", None)
    if not cron_schedule:
        return
    timezone = getattr(workflow, "cron_timezone", "UTC")
    trigger = CronTrigger.from_crontab(cron_schedule, timezone=ZoneInfo(timezone))
    scheduler.add_job(
        workflow_service.run_workflow,
        trigger=trigger,
        args=[workflow.workflow_permanent_id, organization, WorkflowRequestBody()],
        id=f"{organization.organization_id}_{workflow.workflow_permanent_id}",
        replace_existing=True,
    )

async def load_workflows() -> None:
    organizations = await app.DATABASE.get_all_organizations()
    for organization in organizations:
        workflows = await app.WORKFLOW_SERVICE.get_workflows_by_organization_id(
            organization_id=organization.organization_id,
            page_size=1000,
        )
        for workflow in workflows:
            await schedule_workflow(workflow, organization)

async def start_scheduler() -> None:
    await load_workflows()
    scheduler.start()
    LOG.info("Cron trigger started successfully")
    await asyncio.Event().wait()
