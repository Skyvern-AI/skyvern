from pydantic import BaseModel, Field


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
