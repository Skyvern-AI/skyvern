from datetime import datetime

from pydantic import BaseModel, ConfigDict, field_validator

from skyvern.forge.sdk.utils.sanitization import sanitize_postgres_text


class TOTPCodeBase(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    totp_identifier: str | None = None
    task_id: str | None = None
    workflow_id: str | None = None
    workflow_run_id: str | None = None
    source: str | None = None
    content: str | None = None

    expired_at: datetime | None = None


class TOTPCodeCreate(TOTPCodeBase):
    totp_identifier: str
    content: str

    @field_validator("content")
    @classmethod
    def sanitize_content(cls, value: str) -> str:
        """Remove NUL (0x00) bytes from content to avoid PostgreSQL DataError."""
        return sanitize_postgres_text(value)


class TOTPCode(TOTPCodeCreate):
    totp_code_id: str
    code: str
    organization_id: str
    created_at: datetime
    modified_at: datetime
