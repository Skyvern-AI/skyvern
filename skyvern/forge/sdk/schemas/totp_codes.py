from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator

from skyvern.forge.sdk.utils.sanitization import sanitize_postgres_text


class TOTPCodeBase(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    totp_identifier: str | None = Field(
        default=None,
        description="The identifier of the TOTP code. It can be the email address, phone number, or the identifier of the user.",
        examples=["john.doe@example.com", "4155555555", "user_123"],
    )
    task_id: str | None = Field(
        default=None,
        description="The task_id the totp code is for. It can be the task_id of the task that the TOTP code is for.",
        examples=["task_123456"],
    )
    workflow_id: str | None = Field(
        default=None,
        description="The workflow ID the TOTP code is for. It can be the workflow ID of the workflow that the TOTP code is for.",
        examples=["wpid_123456"],
    )
    workflow_run_id: str | None = Field(
        default=None,
        description="The workflow run id that the TOTP code is for. It can be the workflow run id of the workflow run that the TOTP code is for.",
        examples=["wr_123456"],
    )
    source: str | None = Field(
        default=None,
        description="An optional field. The source of the TOTP code. e.g. email, sms, etc.",
        examples=["email", "sms", "app"],
    )
    content: str | None = Field(
        default=None,
        description="The content of the TOTP code. It can be the email content that contains the TOTP code, or the sms message that contains the TOTP code. Skyvern will automatically extract the TOTP code from the content.",
        examples=["Hello, your verification code is 123456"],
    )

    expired_at: datetime | None = Field(
        default=None,
        description="The timestamp when the TOTP code expires",
        examples=["2025-01-01T00:00:00Z"],
    )


class TOTPCodeCreate(TOTPCodeBase):
    totp_identifier: str = Field(
        ...,
        description="The identifier of the TOTP code. It can be the email address, phone number, or the identifier of the user.",
        examples=["john.doe@example.com", "4155555555", "user_123"],
    )
    content: str = Field(
        ...,
        description="The content of the TOTP code. It can be the email content that contains the TOTP code, or the sms message that contains the TOTP code. Skyvern will automatically extract the TOTP code from the content.",
        examples=["Hello, your verification code is 123456"],
    )

    @field_validator("content")
    @classmethod
    def sanitize_content(cls, value: str) -> str:
        """Remove NUL (0x00) bytes from content to avoid PostgreSQL DataError."""
        return sanitize_postgres_text(value)


class OTPType(StrEnum):
    TOTP = "totp"
    MAGIC_LINK = "magic_link"


class TOTPCode(TOTPCodeCreate):
    totp_code_id: str = Field(..., description="The skyvern ID of the TOTP code.")
    code: str = Field(..., description="The TOTP code extracted from the content.")
    organization_id: str = Field(..., description="The ID of the organization that the TOTP code is for.")
    created_at: datetime = Field(..., description="The timestamp when the TOTP code was created.")
    modified_at: datetime = Field(..., description="The timestamp when the TOTP code was modified.")
    otp_type: OTPType | None = Field(None, description="The type of the OTP code.")
