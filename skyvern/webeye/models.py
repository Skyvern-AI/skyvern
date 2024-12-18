from datetime import datetime

from pydantic import BaseModel

class BrowserSessionResponse(BaseModel):
    session_id: str
    organization_id: str
    runnable_type: str
    runnable_id: str
    created_at: datetime
    modified_at: datetime
    deleted_at: datetime | None
