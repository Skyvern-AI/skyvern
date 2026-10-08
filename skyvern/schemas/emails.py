from enum import StrEnum

from pydantic import BaseModel, Field


class EmailBodyFormat(StrEnum):
    TEXT = "text"
    HTML = "html"


class EmailTransport(StrEnum):
    SMTP = "smtp"
    GMAIL = "gmail"


class GmailSendOutcome(StrEnum):
    ACCEPTED = "accepted"
    FAILED = "failed"
    UNKNOWN = "unknown"


class GmailSendErrorCode(StrEnum):
    CONFIGURATION = "configuration"
    MISSING_SCOPE = "missing_scope"
    RECONNECT = "reconnect"
    NO_RECIPIENTS = "no_recipients"
    INVALID_RECIPIENT = "invalid_recipient"
    INVALID_HEADER = "invalid_header"
    ATTACHMENT_INVALID = "attachment_invalid"
    ATTACHMENT_TOO_LARGE = "attachment_too_large"
    TEMPLATE_ERROR = "template_error"
    PROVIDER_REJECTED = "provider_rejected"
    RATE_LIMITED = "rate_limited"
    OUTCOME_UNKNOWN = "outcome_unknown"
    INTERNAL_ERROR = "internal_error"


class EmailAttachment(BaseModel):
    name: str
    mime_type: str | None = None
    size: int | None = None
    attachment_id: str | None = None


class EmailMessage(BaseModel):
    id: str
    thread_id: str | None = None
    subject: str = ""
    from_email: str = ""
    from_name: str | None = None
    to: list[str] = Field(default_factory=list)
    cc: list[str] = Field(default_factory=list)
    date: str | None = None
    snippet: str = ""
    body_text: str = ""
    body_html: str | None = None
    has_attachments: bool | None = None
    attachments: list[EmailAttachment] = Field(default_factory=list)
    is_read: bool = True
    web_link: str | None = None
