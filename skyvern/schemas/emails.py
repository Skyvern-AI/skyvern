from enum import StrEnum

from pydantic import BaseModel, Field, field_validator


class EmailBodyFormat(StrEnum):
    TEXT = "text"
    HTML = "html"


class EmailTransport(StrEnum):
    SMTP = "smtp"
    GMAIL = "gmail"


class GmailHopRefusal(StrEnum):
    """Why a run got no capability for a connected Gmail account, recorded per block when the run is prepared."""

    reconnect = "reconnect"
    run_not_supported = "run_not_supported"
    account_unavailable = "account_unavailable"
    resave_required = "resave_required"
    owner_unconfirmed = "owner_unconfirmed"
    copilot_test_run = "copilot_test_run"


# Shared by the inbox and send blocks, which word the rest of their refusal differently.
CONNECTED_ACCOUNT_RESAVE_REASON = (
    "This Gmail account can be used only by whoever last saved this workflow. Have the person this run belongs to "
    "open and save it — for a schedule, whoever last created or changed the schedule; for an event trigger, whoever "
    "last changed its mapping or secret."
)
CONNECTED_ACCOUNT_OWNER_UNCONFIRMED_REASON = (
    "This Gmail account was not used because Skyvern could not confirm the person this run acts for. That person "
    "(who started the run, or last changed its schedule or trigger) must still be a member, and a Trigger block may pass "
    "this workflow only its own inputs. If both hold, run it again."
)

# ponytail: the prefix only words the failure; authority still comes from the account lookup (ADR 0033).
_CONNECTED_GMAIL_ID_PREFIX = "ngc_"
CONNECTED_ACCOUNT_IN_CREDENTIAL_ID_REASON = (
    "This block's credential_id holds a Connected Gmail account. Set credential_source to nango and put the "
    "account in connection_id."
)


def is_connected_gmail_id(value: str | None) -> bool:
    return bool(value) and str(value).startswith(_CONNECTED_GMAIL_ID_PREFIX)


CONNECTED_ACCOUNT_COPILOT_TEST_RUN_REASON = (
    "Copilot test runs can't use a Connected Gmail account. Save the workflow, then run it to use this account."
)


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
    is_inline: bool = Field(default=False, exclude=True)
    # Gmail stores small attachment bytes in body.data instead of issuing an attachmentId.
    embedded_data: str | None = Field(default=None, exclude=True, repr=False)
    # Opaque proof, issued with the listing, that a connected-account download targets a listed attachment.
    download_grant: str | None = Field(default=None, exclude=True, repr=False)

    @field_validator("name")
    @classmethod
    def _replace_unpaired_surrogates(cls, name: str) -> str:
        # Provider JSON can carry lone surrogates, which Postgres JSONB output rejects.
        return name.encode("utf-8", "replace").decode("utf-8")


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
    # Provider error code when attachments were requested but could not all be listed.
    attachment_list_error: str | None = Field(default=None, exclude=True)
    is_read: bool = True
    web_link: str | None = None
