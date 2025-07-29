from datetime import datetime

from pydantic import BaseModel, ConfigDict


class DebugSession(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    debug_session_id: str
    browser_session_id: str
    workflow_permanent_id: str | None = None
    created_at: datetime
    modified_at: datetime
