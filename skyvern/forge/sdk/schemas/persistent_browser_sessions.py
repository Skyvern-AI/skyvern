from datetime import datetime

from pydantic import BaseModel, ConfigDict


class PersistentBrowserSession(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    persistent_browser_session_id: str
    organization_id: str
    runnable_type: str | None = None
    runnable_id: str | None = None
    browser_address: str | None = None
    ip_address: str | None = None
    status: str | None = None
    timeout_minutes: int | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    created_at: datetime
    modified_at: datetime
    deleted_at: datetime | None = None


class AddressablePersistentBrowserSession(PersistentBrowserSession):
    browser_address: str
