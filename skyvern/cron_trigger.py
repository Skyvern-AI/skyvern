import asyncio
from dotenv import load_dotenv
import structlog

try:
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.cron import CronTrigger
except Exception as e:  # noqa: BLE001
    raise ImportError("apscheduler is required to run cron triggers") from e

from skyvern import analytics
from skyvern.forge import app
from skyvern.forge.sdk.workflow.models.workflow import WorkflowRequestBody

LOG = structlog.get_logger()


async def _run_workflow(workflow):
    organization = await app.DATABASE.get_organization(workflow.organization_id)
    request = WorkflowRequestBody(
        data={},
        proxy_location=workflow.proxy_location,
        webhook_callback_url=workflow.webhook_callback_url,
        totp_verification_url=workflow.totp_verification_url,
        totp_identifier=workflow.totp_identifier,
    )
    await app.WORKFLOW_SERVICE.run_workflow(
        workflow.workflow_permanent_id,
        organization=organization,
        workflow_request=request,
        version=workflow.version,
    )


async def _schedule_workflows(scheduler: AsyncIOScheduler) -> None:
    workflows = await app.DATABASE.get_workflows_with_cron()
    for workflow in workflows:
        if not workflow.cron:
            continue
        scheduler.add_job(
            _run_workflow,
            CronTrigger.from_crontab(workflow.cron),
            args=[workflow],
            id=workflow.workflow_id,
            replace_existing=True,
        )
        LOG.info("Scheduled workflow", workflow_id=workflow.workflow_id, cron=workflow.cron)


async def main() -> None:
    load_dotenv()
    scheduler = AsyncIOScheduler()
    await _schedule_workflows(scheduler)
    scheduler.start()
    analytics.capture("skyvern-oss-cron-start")
    LOG.info("Cron trigger started")
    try:
        await asyncio.Event().wait()
    finally:
        scheduler.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
