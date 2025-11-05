from datetime import datetime

from pydantic import BaseModel, ConfigDict


class BrowserProfile(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    browser_profile_id: str
    organization_id: str
    name: str
    description: str | None = None
    created_at: datetime
    modified_at: datetime
    deleted_at: datetime | None = None
