from datetime import datetime

from pydantic import BaseModel, ConfigDict


class TOTPCodeBase(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    totp_identifier: str | None = None
    task_id: str | None = None
    workflow_id: str | None = None
    source: str | None = None
    content: str | None = None

    expired_at: datetime | None = None


class TOTPCodeCreate(TOTPCodeBase):
    totp_identifier: str
    content: str


class TOTPCode(TOTPCodeCreate):
    totp_code_id: str
    code: str
    organization_id: str
    created_at: datetime
    modified_at: datetime
