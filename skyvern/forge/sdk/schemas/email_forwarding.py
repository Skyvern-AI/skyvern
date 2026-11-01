import re
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from skyvern.utils.email_validation import SAFE_EMAIL_ADDRESS_PATTERN, normalize_email_address

_MAX_EMAIL_ADDRESS_BYTES = 254
_SENDER_ALLOWLIST_ENTRY_PATTERN = re.compile(
    r"(?:[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@)?"
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
    r"(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*"
)


class ForwardingMatchMode(StrEnum):
    DEDICATED = "dedicated"
    ORIGINAL_RECIPIENT = "original_recipient"


class ForwardingAddressStatus(StrEnum):
    PENDING = "pending"
    ACTIVE = "active"
    DISABLED = "disabled"
    REVOKED = "revoked"


class InboundEmailCategory(StrEnum):
    OTP = "otp"
    PROVIDER_CONFIRMATION = "provider_confirmation"
    DROPPED_SPAM = "dropped_spam"
    DROPPED_OTHER = "dropped_other"


def _validated_email_address(value: str) -> str:
    normalized = normalize_email_address(value)
    if len(normalized.encode("utf-8")) > _MAX_EMAIL_ADDRESS_BYTES or not SAFE_EMAIL_ADDRESS_PATTERN.fullmatch(
        normalized
    ):
        raise ValueError("email_address must be a valid address no longer than 254 bytes")
    return normalized


class EmailConfigCreate(BaseModel):
    daily_inbound_cap: int = Field(default=500, ge=1, le=100_000)
    drop_flagged_spam: bool = True


class EmailConfig(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    email_config_id: str
    organization_id: str
    mode: str
    daily_inbound_cap: int
    drop_flagged_spam: bool
    created_at: datetime
    modified_at: datetime


class EmailConfigInternal(EmailConfig):
    webhook_secret_hash: str | None


class EmailConfigWithSecret(EmailConfig):
    receiver_url: str


class ForwardingAddressCreate(BaseModel):
    email_config_id: str
    email_address: str
    credential_id: str | None = None
    match_mode: ForwardingMatchMode = ForwardingMatchMode.DEDICATED
    sender_allowlist: list[str] | None = Field(
        default=None,
        max_length=50,
        description=(
            "Anti-noise filter over the sender-asserted From/Return-Path values, matched as "
            "exact addresses or registrable domains. It is not an authentication boundary: "
            "forwarded mail preserves whatever the original hop asserted, and this receiver "
            "does not verify DKIM/SPF itself. Authenticity comes from the secret receiver URL "
            "and the registered-address lookup."
        ),
    )

    @field_validator("email_address")
    @classmethod
    def validate_email_address(cls, value: str) -> str:
        return _validated_email_address(value)

    @field_validator("sender_allowlist")
    @classmethod
    def normalize_allowlist(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        normalized: list[str] = []
        for entry in value:
            if len(entry) > 253:
                raise ValueError("sender_allowlist entries cannot exceed 253 characters")
            if any(ord(char) < 32 or ord(char) == 127 for char in entry):
                raise ValueError("sender_allowlist entries cannot contain control characters")
            normalized_entry = entry.strip().casefold().lstrip("@")
            if not normalized_entry:
                raise ValueError("sender_allowlist entries cannot be empty")
            if not _SENDER_ALLOWLIST_ENTRY_PATTERN.fullmatch(normalized_entry):
                raise ValueError("sender_allowlist entries must be valid domains or email addresses")
            normalized.append(normalized_entry)
        return list(dict.fromkeys(normalized))

    @model_validator(mode="after")
    def validate_credential_match_mode(self) -> "ForwardingAddressCreate":
        if self.credential_id is not None and self.match_mode == ForwardingMatchMode.ORIGINAL_RECIPIENT:
            raise ValueError("credential_id cannot be used with match_mode=original_recipient")
        return self


class ForwardingAddressRotate(BaseModel):
    email_address: str

    @field_validator("email_address")
    @classmethod
    def validate_email_address(cls, value: str) -> str:
        return _validated_email_address(value)


class ForwardingAddress(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    forwarding_address_id: str
    organization_id: str
    email_config_id: str
    email_address: str
    org_slug: str
    match_mode: ForwardingMatchMode
    credential_id: str | None
    sender_allowlist: list[str] | None
    status: ForwardingAddressStatus
    last_received_at: datetime | None
    has_pending_confirmation: bool = False
    created_at: datetime
    modified_at: datetime


class ForwardingAddressRotation(BaseModel):
    revoked: ForwardingAddress
    replacement: ForwardingAddress


class InboundEmail(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    inbound_email_id: str
    organization_id: str
    forwarding_address_id: str
    external_message_id: str | None
    from_address: str
    original_recipient: str | None
    subject: str
    body_text: str
    links: list[dict[str, str]]
    category: InboundEmailCategory
    verdicts: dict[str, str] | None
    totp_code_id: str | None
    created_at: datetime
