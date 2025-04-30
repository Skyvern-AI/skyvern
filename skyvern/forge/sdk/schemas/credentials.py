from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class CredentialType(StrEnum):
    """Type of credential stored in the system."""

    PASSWORD = "password"
    CREDIT_CARD = "credit_card"


class PasswordCredentialResponse(BaseModel):
    """Response model for password credentials, containing only the username."""

    username: str = Field(..., description="The username associated with the credential", example="user@example.com")


class CreditCardCredentialResponse(BaseModel):
    """Response model for credit card credentials, containing only the last four digits and brand."""

    last_four: str = Field(..., description="Last four digits of the credit card number", example="1234")
    brand: str = Field(..., description="Brand of the credit card", example="visa")


class PasswordCredential(BaseModel):
    """Base model for password credentials."""

    password: str = Field(..., description="The password value", example="securepassword123")
    username: str = Field(..., description="The username associated with the credential", example="user@example.com")
    totp: str | None = Field(
        None,
        description="Optional TOTP (Time-based One-Time Password) string used to generate 2FA codes",
        example="JBSWY3DPEHPK3PXP",
    )


class NonEmptyPasswordCredential(PasswordCredential):
    """Password credential model that requires non-empty values."""

    password: str = Field(
        ..., min_length=1, description="The password value (must not be empty)", example="securepassword123"
    )
    username: str = Field(
        ...,
        min_length=1,
        description="The username associated with the credential (must not be empty)",
        example="user@example.com",
    )


class CreditCardCredential(BaseModel):
    """Base model for credit card credentials."""

    card_number: str = Field(..., description="The full credit card number", example="4111111111111111")
    card_cvv: str = Field(..., description="The card's CVV (Card Verification Value)", example="123")
    card_exp_month: str = Field(..., description="The card's expiration month", example="12")
    card_exp_year: str = Field(..., description="The card's expiration year", example="2025")
    card_brand: str = Field(..., description="The card's brand", example="visa")
    card_holder_name: str = Field(..., description="The name of the card holder", example="John Doe")


class NonEmptyCreditCardCredential(CreditCardCredential):
    """Credit card credential model that requires non-empty values."""

    card_number: str = Field(
        ..., min_length=1, description="The full credit card number (must not be empty)", example="4111111111111111"
    )
    card_cvv: str = Field(..., min_length=1, description="The card's CVV (must not be empty)", example="123")
    card_exp_month: str = Field(
        ..., min_length=1, description="The card's expiration month (must not be empty)", example="12"
    )
    card_exp_year: str = Field(
        ..., min_length=1, description="The card's expiration year (must not be empty)", example="2025"
    )
    card_brand: str = Field(..., min_length=1, description="The card's brand (must not be empty)", example="visa")
    card_holder_name: str = Field(
        ..., min_length=1, description="The name of the card holder (must not be empty)", example="John Doe"
    )


class CredentialItem(BaseModel):
    """Model representing a credential item in the system."""

    item_id: str = Field(..., description="Unique identifier for the credential item", example="cred_1234567890")
    name: str = Field(..., description="Name of the credential", example="Skyvern Login")
    credential_type: CredentialType = Field(..., description="Type of the credential. Eg password, credit card, etc.")
    credential: PasswordCredential | CreditCardCredential = Field(..., description="The actual credential data")


class CreateCredentialRequest(BaseModel):
    """Request model for creating a new credential."""

    name: str = Field(..., description="Name of the credential", example="My Credential")
    credential_type: CredentialType = Field(..., description="Type of credential to create")
    credential: NonEmptyPasswordCredential | NonEmptyCreditCardCredential = Field(
        ...,
        description="The credential data to store",
        example={"username": "user@example.com", "password": "securepassword123"},
    )


class CredentialResponse(BaseModel):
    """Response model for credential operations."""

    credential_id: str = Field(..., description="Unique identifier for the credential", example="cred_1234567890")
    credential: PasswordCredentialResponse | CreditCardCredentialResponse = Field(
        ..., description="The credential data"
    )
    credential_type: CredentialType = Field(..., description="Type of the credential")
    name: str = Field(..., description="Name of the credential", example="My Credential")


class Credential(BaseModel):
    """Database model for credentials."""

    model_config = ConfigDict(from_attributes=True)

    credential_id: str = Field(..., description="Unique identifier for the credential", example="cred_1234567890")
    organization_id: str = Field(
        ..., description="ID of the organization that owns the credential", example="o_1234567890"
    )
    name: str = Field(..., description="Name of the credential", example="Skyvern Login")
    credential_type: CredentialType = Field(..., description="Type of the credential. Eg password, credit card, etc.")
    item_id: str = Field(..., description="ID of the associated credential item", example="item_1234567890")

    created_at: datetime = Field(..., description="Timestamp when the credential was created")
    modified_at: datetime = Field(..., description="Timestamp when the credential was last modified")
    deleted_at: datetime | None = Field(None, description="Timestamp when the credential was deleted, if applicable")
