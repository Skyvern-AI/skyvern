from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from skyvern.schemas.runs import ProxyLocation


class PersistentBrowserSessionStatus(StrEnum):
    created = "created"
    running = "running"
    failed = "failed"
    completed = "completed"
    timeout = "timeout"
    retry = "retry"


FINAL_STATUSES = (
    PersistentBrowserSessionStatus.completed,
    PersistentBrowserSessionStatus.failed,
    PersistentBrowserSessionStatus.timeout,
)


def is_final_status(status: str | None) -> bool:
    return status in FINAL_STATUSES


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
    proxy_location: ProxyLocation | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    created_at: datetime
    modified_at: datetime
    deleted_at: datetime | None = None


class AddressablePersistentBrowserSession(PersistentBrowserSession):
    browser_address: str
