from datetime import datetime

from pydantic import BaseModel


class DebugSession(BaseModel):
    debug_session_id: str
    organization_id: str
    browser_session_id: str
    workflow_permanent_id: str
    user_id: str
    created_at: datetime
    modified_at: datetime
