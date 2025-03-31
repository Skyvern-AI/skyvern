from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


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


class NonEmptyPasswordCredential(PasswordCredential):
    password: str = Field(..., min_length=1)
    username: str = Field(..., min_length=1)


class CreditCardCredential(BaseModel):
    card_number: str
    card_cvv: str
    card_exp_month: str
    card_exp_year: str
    card_brand: str
    card_holder_name: str


class NonEmptyCreditCardCredential(CreditCardCredential):
    card_number: str = Field(..., min_length=1)
    card_cvv: str = Field(..., min_length=1)
    card_exp_month: str = Field(..., min_length=1)
    card_exp_year: str = Field(..., min_length=1)
    card_brand: str = Field(..., min_length=1)
    card_holder_name: str = Field(..., min_length=1)


class CredentialItem(BaseModel):
    item_id: str
    name: str
    credential_type: CredentialType
    credential: PasswordCredential | CreditCardCredential


class CreateCredentialRequest(BaseModel):
    name: str
    credential_type: CredentialType
    credential: NonEmptyPasswordCredential | NonEmptyCreditCardCredential


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
