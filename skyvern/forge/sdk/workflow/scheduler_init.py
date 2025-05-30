import asyncio
import logging

from skyvern.forge import app
from skyvern.forge.sdk.workflow.scheduler import workflow_scheduler

logger = logging.getLogger(__name__)


async def initialize_workflow_scheduler():
    """Initialize the workflow scheduler during application startup."""
    try:
        logger.info("Initializing workflow scheduler")
        await workflow_scheduler.initialize()
        logger.info("Workflow scheduler initialized successfully")
    except Exception as e:
        logger.error(f"Error initializing workflow scheduler: {e}", exc_info=True)
        # Don't re-raise the exception to allow the application to start even if scheduler fails


async def shutdown_workflow_scheduler():
    """Shutdown the workflow scheduler during application shutdown."""
    try:
        logger.info("Shutting down workflow scheduler")
        if hasattr(workflow_scheduler, 'scheduler') and workflow_scheduler.scheduler.running:
            workflow_scheduler.scheduler.shutdown()
        logger.info("Workflow scheduler shut down successfully")
    except Exception as e:
        logger.error(f"Error shutting down workflow scheduler: {e}", exc_info=True)
