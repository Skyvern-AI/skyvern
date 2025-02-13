from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class CredentialType(StrEnum):
    PASSWORD = "password"
    CREDIT_CARD = "credit_card"


class PasswordCredential(BaseModel):
    password: str
    username: str


class CreditCardCredential(BaseModel):
    card_number: str
    card_cvv: str
    card_exp_month: str
    card_exp_year: str
    card_brand: str
    card_holder_name: str


class UpdateCredentialRequest(BaseModel):
    name: str | None = None
    website_url: str | None = None


class CreateCredentialRequest(BaseModel):
    name: str
    website_url: str | None = None
    credential_type: CredentialType
    credential: PasswordCredential | CreditCardCredential


class Credential(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    credential_id: str
    organization_id: str
    name: str
    website_url: str | None = None
    credential_type: CredentialType

    created_at: datetime
    modified_at: datetime
    deleted_at: datetime | None = None
