from __future__ import annotations

from datetime import datetime
from typing import Literal, get_args

from pydantic import BaseModel, ConfigDict, field_validator

from skyvern.utils.phone_validation import (
    is_e164_phone_number,
    is_twilio_phone_number_sid,
    looks_like_phone_identifier,
    normalize_phone_identifier,
)

PhoneNumberStatus = Literal["active", "disabled", "released", "quarantined"]
PHONE_NUMBER_STATUSES: frozenset[str] = frozenset(get_args(PhoneNumberStatus))
SMSConfigMode = Literal["connected", "manual", "managed"]
SMS_CONFIG_MODES: frozenset[str] = frozenset(get_args(SMSConfigMode))


class SMSConfig(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    sms_config_id: str
    organization_id: str
    mode: SMSConfigMode
    daily_ingest_cap: int
    created_at: datetime


class SMSConfigCreated(SMSConfig):
    inbound_url: str


class OrganizationPhoneNumber(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    phone_number_id: str
    organization_id: str
    sms_config_id: str
    phone_number: str
    provider: str
    provider_number_sid: str | None = None
    previous_sms_url: str | None = None
    previous_sms_method: str | None = None
    previous_sms_application_sid: str | None = None
    credential_id: str | None = None
    provider_cost_cents: int | None = None
    price_cents: int | None = None
    status: PhoneNumberStatus
    quarantined_until: datetime | None = None
    created_at: datetime
    modified_at: datetime
    deleted_at: datetime | None = None


class TwilioPhoneNumberInfo(BaseModel):
    sid: str
    phone_number: str
    friendly_name: str
    sms_capable: bool
    current_sms_url: str | None = None
    enabled: bool
    phone_number_id: str | None = None


class EnablePhoneNumberRequest(BaseModel):
    phone_number_sid: str
    confirm_takeover: bool = False
    credential_id: str | None = None

    @field_validator("phone_number_sid")
    @classmethod
    def validate_phone_number_sid(cls, value: str) -> str:
        if not is_twilio_phone_number_sid(value):
            raise ValueError("phone_number_sid must be a valid Twilio incoming phone number SID")
        return value


class DisablePhoneNumberRequest(BaseModel):
    phone_number_id: str


class RegisterManualPhoneNumberRequest(BaseModel):
    phone_number: str

    @field_validator("phone_number")
    @classmethod
    def validate_phone_number(cls, value: str) -> str:
        if not looks_like_phone_identifier(value):
            raise ValueError("phone_number must include a country code and normalize to E.164")
        normalized = normalize_phone_identifier(value)
        if not is_e164_phone_number(normalized):
            raise ValueError("phone_number must include a country code and normalize to E.164")
        return normalized


class CreateSMSConfigRequest(BaseModel):
    mode: Literal["manual"] = "manual"
