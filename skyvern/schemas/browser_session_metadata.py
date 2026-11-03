from datetime import datetime

from pydantic import BaseModel

from skyvern.forge.sdk.schemas.persistent_browser_sessions import PersistentBrowserSessionStatus
from skyvern.forge.sdk.workflow.models.workflow import WorkflowRunStatus


class BrowserSessionWorkflowMetadata(BaseModel):
    workflow_run_id: str
    organization_id: str
    browser_session_id: str | None
    association_browser_session_id: str
    workflow_permanent_id: str
    copilot_session_id: str | None
    status: WorkflowRunStatus
    created_at: datetime
    finished_at: datetime | None
    credits_used: int | None
    cached_credits_used: int | None


class BrowserSessionMetadata(BaseModel):
    browser_session_id: str
    organization_id: str
    status: PersistentBrowserSessionStatus
    created_at: datetime
    completed_at: datetime | None
    runnable_id: str | None
    associated_workflow_runs: list[BrowserSessionWorkflowMetadata]
    association_index_complete: bool
