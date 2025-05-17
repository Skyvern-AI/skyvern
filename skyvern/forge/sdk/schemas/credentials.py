from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class CredentialType(StrEnum):
    """Type of credential stored in the system."""

    PASSWORD = "password"
    CREDIT_CARD = "credit_card"


class PasswordCredentialResponse(BaseModel):
    """Response model for password credentials, containing only the username."""

    username: str = Field(..., description="The username associated with the credential", examples=["user@example.com"])


class CreditCardCredentialResponse(BaseModel):
    """Response model for credit card credentials, containing only the last four digits and brand."""

    last_four: str = Field(..., description="Last four digits of the credit card number", examples=["1234"])
    brand: str = Field(..., description="Brand of the credit card", examples=["visa"])


class PasswordCredential(BaseModel):
    """Base model for password credentials."""

    password: str = Field(..., description="The password value", examples=["securepassword123"])
    username: str = Field(..., description="The username associated with the credential", examples=["user@example.com"])
    totp: str | None = Field(
        None,
        description="Optional TOTP (Time-based One-Time Password) string used to generate 2FA codes",
        examples=["JBSWY3DPEHPK3PXP"],
    )


class NonEmptyPasswordCredential(PasswordCredential):
    """Password credential model that requires non-empty values."""

    password: str = Field(
        ..., min_length=1, description="The password value (must not be empty)", examples=["securepassword123"]
    )
    username: str = Field(
        ...,
        min_length=1,
        description="The username associated with the credential (must not be empty)",
        examples=["user@example.com"],
    )


class CreditCardCredential(BaseModel):
    """Base model for credit card credentials."""

    card_number: str = Field(..., description="The full credit card number", examples=["4111111111111111"])
    card_cvv: str = Field(..., description="The card's CVV (Card Verification Value)", examples=["123"])
    card_exp_month: str = Field(..., description="The card's expiration month", examples=["12"])
    card_exp_year: str = Field(..., description="The card's expiration year", examples=["2025"])
    card_brand: str = Field(..., description="The card's brand", examples=["visa"])
    card_holder_name: str = Field(..., description="The name of the card holder", examples=["John Doe"])


class NonEmptyCreditCardCredential(CreditCardCredential):
    """Credit card credential model that requires non-empty values."""

    card_number: str = Field(
        ..., min_length=1, description="The full credit card number (must not be empty)", examples=["4111111111111111"]
    )
    card_cvv: str = Field(..., min_length=1, description="The card's CVV (must not be empty)", examples=["123"])
    card_exp_month: str = Field(
        ..., min_length=1, description="The card's expiration month (must not be empty)", examples=["12"]
    )
    card_exp_year: str = Field(
        ..., min_length=1, description="The card's expiration year (must not be empty)", examples=["2025"]
    )
    card_brand: str = Field(..., min_length=1, description="The card's brand (must not be empty)", examples=["visa"])
    card_holder_name: str = Field(
        ..., min_length=1, description="The name of the card holder (must not be empty)", examples=["John Doe"]
    )


class CredentialItem(BaseModel):
    """Model representing a credential item in the system."""

    item_id: str = Field(..., description="Unique identifier for the credential item", examples=["cred_1234567890"])
    name: str = Field(..., description="Name of the credential", examples=["Skyvern Login"])
    credential_type: CredentialType = Field(..., description="Type of the credential. Eg password, credit card, etc.")
    credential: PasswordCredential | CreditCardCredential = Field(..., description="The actual credential data")


class CreateCredentialRequest(BaseModel):
    """Request model for creating a new credential."""

    name: str = Field(..., description="Name of the credential", examples=["Amazon Login"])
    credential_type: CredentialType = Field(..., description="Type of credential to create")
    credential: NonEmptyPasswordCredential | NonEmptyCreditCardCredential = Field(
        ...,
        description="The credential data to store",
        examples=[{"username": "user@example.com", "password": "securepassword123"}],
    )


class CredentialResponse(BaseModel):
    """Response model for credential operations."""

    credential_id: str = Field(..., description="Unique identifier for the credential", examples=["cred_1234567890"])
    credential: PasswordCredentialResponse | CreditCardCredentialResponse = Field(
        ..., description="The credential data"
    )
    credential_type: CredentialType = Field(..., description="Type of the credential")
    name: str = Field(..., description="Name of the credential", examples=["Amazon Login"])


class Credential(BaseModel):
    """Database model for credentials."""

    model_config = ConfigDict(from_attributes=True)

    credential_id: str = Field(..., description="Unique identifier for the credential", examples=["cred_1234567890"])
    organization_id: str = Field(
        ..., description="ID of the organization that owns the credential", examples=["o_1234567890"]
    )
    name: str = Field(..., description="Name of the credential", examples=["Skyvern Login"])
    credential_type: CredentialType = Field(..., description="Type of the credential. Eg password, credit card, etc.")
    item_id: str = Field(..., description="ID of the associated credential item", examples=["item_1234567890"])

    created_at: datetime = Field(..., description="Timestamp when the credential was created")
    modified_at: datetime = Field(..., description="Timestamp when the credential was last modified")
    deleted_at: datetime | None = Field(None, description="Timestamp when the credential was deleted, if applicable")
