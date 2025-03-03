from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class CredentialType(StrEnum):
    PASSWORD = "password"
    CREDIT_CARD = "credit_card"


class PasswordCredentialResponse(BaseModel):
    username: str


class CreditCardCredentialResponse(BaseModel):
    last_four: str
    brand: str


class PasswordCredential(BaseModel):
    password: str
    username: str
    totp: str | None = None


class CreditCardCredential(BaseModel):
    card_number: str
    card_cvv: str
    card_exp_month: str
    card_exp_year: str
    card_brand: str
    card_holder_name: str


class CredentialItem(BaseModel):
    item_id: str
    name: str
    credential_type: CredentialType
    credential: PasswordCredential | CreditCardCredential


class CreateCredentialRequest(BaseModel):
    name: str
    credential_type: CredentialType
    credential: PasswordCredential | CreditCardCredential


class CredentialResponse(BaseModel):
    credential_id: str
    credential: PasswordCredentialResponse | CreditCardCredentialResponse
    credential_type: CredentialType
    name: str


class Credential(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    credential_id: str
    organization_id: str
    name: str
    credential_type: CredentialType

    item_id: str

    created_at: datetime
    modified_at: datetime
    deleted_at: datetime | None = None
