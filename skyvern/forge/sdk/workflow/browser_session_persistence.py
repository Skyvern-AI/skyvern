from __future__ import annotations

from skyvern.forge import app
from skyvern.forge.sdk.workflow.models.workflow import Workflow, WorkflowRun


async def retrieve_persisted_workflow_browser_state_dir(
    *,
    organization_id: str,
    workflow: Workflow,
    workflow_run: WorkflowRun,
) -> str | None:
    browser_profile_id = workflow_run.browser_profile_id
    if browser_profile_id:
        profile = await app.DATABASE.browser_sessions.get_browser_profile(
            profile_id=browser_profile_id,
            organization_id=organization_id,
        )
        # Only managed profiles owned by this workflow receive the run's end-of-run
        # write-back; a user (or foreign managed) profile's blob is curated starting
        # state, so reading it here would return stale data.
        if profile and profile.is_managed and profile.workflow_permanent_id == workflow_run.workflow_permanent_id:
            session_dir = await app.STORAGE.retrieve_browser_profile(
                organization_id=organization_id,
                profile_id=browser_profile_id,
            )
            if session_dir:
                return session_dir

    browser_session_storage_key = await app.WORKFLOW_SERVICE.get_workflow_browser_session_storage_key(
        workflow=workflow,
        workflow_run=workflow_run,
    )
    return await app.STORAGE.retrieve_browser_session(
        organization_id=organization_id,
        workflow_permanent_id=browser_session_storage_key,
    )
