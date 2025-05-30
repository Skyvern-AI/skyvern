import asyncio
import datetime
import logging
from typing import Dict, List, Optional, Any

import croniter
import pytz
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from skyvern.forge import app
from skyvern.forge.sdk.schemas.organizations import Organization
from skyvern.forge.sdk.workflow.models.workflow import WorkflowRunStatus

logger = logging.getLogger(__name__)


class WorkflowScheduler:
    """Scheduler for workflow cron jobs.
    
    This class manages the scheduling of workflow executions based on cron expressions.
    It ensures that workflows with cron_enabled=True are scheduled to run at the appropriate times.
    """

    def __init__(self):
        self.scheduler = AsyncIOScheduler()
        self._job_map: Dict[str, str] = {}  # Maps workflow_id to job_id
        self._initialized = False

    async def initialize(self):
        """Initialize the scheduler and load existing cron jobs from the database."""
        if self._initialized:
            return

        # Start the scheduler
        self.scheduler.start()
        self._initialized = True

        # Load existing cron jobs from the database
        await self.load_scheduled_workflows()
        
        logger.info("Workflow scheduler initialized")

    async def load_scheduled_workflows(self):
        """Load and schedule all workflows with cron_enabled=True from the database."""
        # Get all workflows with cron_enabled=True
        workflows = await app.DATABASE.get_workflows_with_cron_enabled()
        
        for workflow in workflows:
            # Skip workflows without cron expression
            if not workflow.cron_expression:
                continue
                
            # Schedule the workflow
            await self.schedule_workflow(workflow.workflow_id)
            
        logger.info(f"Loaded {len(workflows)} scheduled workflows")

    async def schedule_workflow(self, workflow_id: str):
        """Schedule a workflow to run based on its cron expression.
        
        Args:
            workflow_id: The ID of the workflow to schedule
        """
        # Get the workflow
        workflow = await app.DATABASE.get_workflow(workflow_id)
        if not workflow:
            logger.error(f"Cannot schedule workflow {workflow_id}: not found")
            return
        
        # Skip if cron is not enabled or expression is not set
        if not workflow.cron_enabled or not workflow.cron_expression:
            logger.info(f"Skipping scheduling for workflow {workflow_id}: cron not enabled or expression not set")
            return
        
        # Remove existing job if it exists
        await self.unschedule_workflow(workflow_id)
        
        # Parse timezone
        timezone = pytz.timezone(workflow.timezone or "UTC")
        
        # Create a cron trigger
        trigger = CronTrigger.from_crontab(
            workflow.cron_expression,
            timezone=timezone
        )
        
        # Calculate next run time
        cron = croniter.croniter(workflow.cron_expression, datetime.datetime.now(timezone))
        next_run_time = cron.get_next(datetime.datetime)
        
        # Update the next_run_time in the database
        await app.DATABASE.update_workflow(
            workflow_id=workflow_id,
            next_run_time=next_run_time
        )
        
        # Add the job to the scheduler
        job = self.scheduler.add_job(
            self._execute_workflow,
            trigger=trigger,
            args=[workflow_id, workflow.organization_id],
            id=f"workflow_{workflow_id}",
            replace_existing=True,
            misfire_grace_time=3600  # Allow misfires up to 1 hour
        )
        
        # Store the job ID
        self._job_map[workflow_id] = job.id
        
        logger.info(f"Scheduled workflow {workflow_id} with cron expression '{workflow.cron_expression}' in timezone '{timezone}'. Next run at {next_run_time}")

    async def unschedule_workflow(self, workflow_id: str):
        """Remove a workflow from the scheduler.
        
        Args:
            workflow_id: The ID of the workflow to unschedule
        """
        job_id = self._job_map.get(workflow_id)
        if job_id:
            try:
                self.scheduler.remove_job(job_id)
                del self._job_map[workflow_id]
                logger.info(f"Unscheduled workflow {workflow_id}")
            except Exception as e:
                logger.error(f"Error unscheduling workflow {workflow_id}: {e}")

    async def update_workflow_schedule(self, workflow_id: str):
        """Update the schedule for a workflow.
        
        This is called when a workflow's cron expression or enabled status changes.
        
        Args:
            workflow_id: The ID of the workflow to update
        """
        # Get the workflow
        workflow = await app.DATABASE.get_workflow(workflow_id)
        if not workflow:
            logger.error(f"Cannot update schedule for workflow {workflow_id}: not found")
            return
        
        # If cron is enabled and expression is set, schedule the workflow
        if workflow.cron_enabled and workflow.cron_expression:
            await self.schedule_workflow(workflow_id)
        else:
            # Otherwise, unschedule it
            await self.unschedule_workflow(workflow_id)
            
            # Clear the next_run_time in the database
            await app.DATABASE.update_workflow(
                workflow_id=workflow_id,
                next_run_time=None
            )

    async def _execute_workflow(self, workflow_id: str, organization_id: str):
        """Execute a workflow as a scheduled job.
        
        This is the callback function that is called by the scheduler when a job is triggered.
        
        Args:
            workflow_id: The ID of the workflow to execute
            organization_id: The ID of the organization that owns the workflow
        """
        try:
            # Get the organization
            organization = await app.DATABASE.get_organization(organization_id)
            if not organization:
                logger.error(f"Cannot execute workflow {workflow_id}: organization {organization_id} not found")
                return
            
            # Create a workflow run
            workflow_run = await self._create_workflow_run(workflow_id, organization, triggered_by_cron=True)
            if not workflow_run:
                return
                
            # Execute the workflow
            await self._run_workflow(organization, workflow_id, workflow_run.workflow_run_id)
            
            # Update the next run time in the database
            await self._update_next_run_time(workflow_id)
            
            logger.info(f"Successfully executed scheduled workflow {workflow_id}, run ID: {workflow_run.workflow_run_id}")
        except Exception as e:
            logger.error(f"Error executing scheduled workflow {workflow_id}: {e}", exc_info=True)

    async def _create_workflow_run(self, workflow_id: str, organization: Organization, triggered_by_cron: bool = False):
        """Create a workflow run for a scheduled execution.
        
        Args:
            workflow_id: The ID of the workflow to execute
            organization: The organization that owns the workflow
            triggered_by_cron: Whether this run was triggered by a cron job
            
        Returns:
            The created workflow run, or None if creation failed
        """
        try:
            # Get the workflow
            workflow = await app.DATABASE.get_workflow(workflow_id)
            if not workflow:
                logger.error(f"Cannot create run for workflow {workflow_id}: not found")
                return None
                
            # Create the workflow run using the specialized method for cron-triggered runs
            if triggered_by_cron:
                workflow_run = await app.DATABASE.create_workflow_run_from_cron(
                    workflow_id=workflow_id,
                    workflow_permanent_id=workflow.workflow_permanent_id,
                    organization_id=organization.organization_id,
                    status=WorkflowRunStatus.created,
                    proxy_location=workflow.proxy_location,
                    webhook_callback_url=workflow.webhook_callback_url,
                    totp_verification_url=workflow.totp_verification_url,
                    totp_identifier=workflow.totp_identifier
                )
            else:
                # Use the regular method for non-cron-triggered runs
                workflow_run = await app.DATABASE.create_workflow_run(
                    workflow_id=workflow_id,
                    workflow_permanent_id=workflow.workflow_permanent_id,
                    organization_id=organization.organization_id,
                    status=WorkflowRunStatus.created,
                    proxy_location=workflow.proxy_location,
                    webhook_callback_url=workflow.webhook_callback_url,
                    totp_verification_url=workflow.totp_verification_url,
                    totp_identifier=workflow.totp_identifier
                )
            
            return workflow_run
        except Exception as e:
            logger.error(f"Error creating workflow run for {workflow_id}: {e}", exc_info=True)
            return None

    async def _run_workflow(self, organization: Organization, workflow_id: str, workflow_run_id: str):
        """Execute a workflow run.
        
        Args:
            organization: The organization that owns the workflow
            workflow_id: The ID of the workflow to execute
            workflow_run_id: The ID of the workflow run to execute
        """
        try:
            # Update the workflow run status to queued
            await app.DATABASE.update_workflow_run(
                workflow_run_id=workflow_run_id,
                status=WorkflowRunStatus.queued,
            )
            
            # Execute the workflow using the workflow service
            await app.WORKFLOW_SERVICE.execute_workflow(
                workflow_run_id=workflow_run_id,
                api_key=None,  # No API key for scheduled runs
                organization=organization,
                browser_session_id=None,  # No browser session for scheduled runs
            )
        except Exception as e:
            logger.error(f"Error executing workflow run {workflow_run_id}: {e}", exc_info=True)
            
            # Update the workflow run status to failed
            await app.DATABASE.update_workflow_run(
                workflow_run_id=workflow_run_id,
                status=WorkflowRunStatus.failed,
                failure_reason=str(e)
            )

    async def _update_next_run_time(self, workflow_id: str):
        """Update the next_run_time for a workflow in the database.
        
        Args:
            workflow_id: The ID of the workflow to update
        """
        try:
            # Get the workflow
            workflow = await app.DATABASE.get_workflow(workflow_id)
            if not workflow or not workflow.cron_expression:
                return
                
            # Parse timezone
            timezone = pytz.timezone(workflow.timezone or "UTC")
            
            # Calculate next run time
            cron = croniter.croniter(workflow.cron_expression, datetime.datetime.now(timezone))
            next_run_time = cron.get_next(datetime.datetime)
            
            # Update the next_run_time in the database
            await app.DATABASE.update_workflow(
                workflow_id=workflow_id,
                next_run_time=next_run_time
            )
            
            logger.info(f"Updated next run time for workflow {workflow_id} to {next_run_time}")
        except Exception as e:
            logger.error(f"Error updating next run time for workflow {workflow_id}: {e}", exc_info=True)


# Singleton instance
workflow_scheduler = WorkflowScheduler()
