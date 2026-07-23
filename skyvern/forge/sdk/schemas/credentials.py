from datetime import datetime
from enum import StrEnum
from typing import Self

from fastapi import status
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from skyvern.exceptions import SkyvernHTTPException
from skyvern.schemas.proxy_location import ProxyLocationInput
from skyvern.schemas.proxy_pinning import parse_proxy_location_input, validate_proxy_session_id
from skyvern.utils.url_validators import validate_url


class CredentialVaultType(StrEnum):
    SKYVERN = "skyvern"
    BITWARDEN = "bitwarden"
    AZURE_VAULT = "azure_vault"
    GCP = "gcp"
    CUSTOM = "custom"


class CredentialType(StrEnum):
    """Type of credential stored in the system."""

    PASSWORD = "password"
    CREDIT_CARD = "credit_card"
    SECRET = "secret"


class TotpType(StrEnum):
    """Type of 2FA/TOTP method used."""

    AUTHENTICATOR = "authenticator"
    EMAIL = "email"
    TEXT = "text"
    NONE = "none"


class PasswordCredentialResponse(BaseModel):
    """Response model for password credentials — non-sensitive fields only.

    SECURITY: Must NEVER include password or TOTP secret.
    """

    username: str = Field(..., description="The username associated with the credential", examples=["user@example.com"])
    totp_type: TotpType = Field(
        TotpType.NONE,
        description="Type of 2FA method used for this credential",
        examples=[TotpType.AUTHENTICATOR],
    )
    totp_identifier: str | None = Field(
        default=None,
        description="Identifier (email or phone number) used to fetch TOTP codes",
        examples=["user@example.com", "+14155550123"],
    )


class CredentialTotpCodeResponse(BaseModel):
    """Current authenticator code for a password credential.

    SECURITY: This response must never include the TOTP seed/secret.
    """

    code: str = Field(..., description="Current generated authenticator code", examples=["123456"])
    seconds_remaining: int = Field(
        ...,
        ge=0,
        description="Seconds until this code rolls over",
        examples=[24],
    )


class CreditCardCredentialResponse(BaseModel):
    """Response model for credit card credentials — non-sensitive fields only.

    SECURITY: Must NEVER include full card number, CVV, expiration date, card holder name,
    billing fields, or metadata.
    """

    last_four: str = Field(..., description="Last four digits of the credit card number", examples=["1234"])
    brand: str = Field(..., description="Brand of the credit card", examples=["visa"])


class CreditCardBillingAddress(BaseModel):
    """Optional billing address fields associated with a credit card credential."""

    line1: str | None = Field(default=None, description="Billing address line 1", examples=["123 Main St"])
    line2: str | None = Field(default=None, description="Billing address line 2", examples=["Apt 4B"])
    city: str | None = Field(default=None, description="Billing city", examples=["San Francisco"])
    state: str | None = Field(default=None, description="Billing state or region", examples=["California"])
    state_code: str | None = Field(default=None, description="Billing state or region code", examples=["CA"])
    postal_code: str | None = Field(default=None, description="Billing postal code", examples=["94105"])
    country: str | None = Field(default=None, description="Billing country", examples=["United States"])
    country_code: str | None = Field(
        default=None, description="ISO 3166-1 alpha-2 billing country code", examples=["US"]
    )


class SecretCredentialResponse(BaseModel):
    """Response model for secret credentials — non-sensitive fields only.

    SECURITY: Must NEVER include the secret_value.
    """

    secret_label: str | None = Field(default=None, description="Optional label for the stored secret")


class PasswordCredential(BaseModel):
    """Base model for password credentials."""

    password: str = Field(..., description="The password value", examples=["securepassword123"])
    username: str = Field(..., description="The username associated with the credential", examples=["user@example.com"])
    totp: str | None = Field(
        None,
        description="Optional TOTP (Time-based One-Time Password) string used to generate 2FA codes",
        examples=["JBSWY3DPEHPK3PXP"],
    )
    totp_type: TotpType = Field(
        TotpType.NONE,
        description="Type of 2FA method used for this credential",
        examples=[TotpType.AUTHENTICATOR],
    )
    totp_identifier: str | None = Field(
        default=None,
        description="Identifier (email or phone number) used to fetch TOTP codes",
        examples=["user@example.com", "+14155550123"],
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
    billing_address: CreditCardBillingAddress | None = Field(
        default=None,
        description="Optional billing address associated with the card",
    )
    billing_email: str | None = Field(default=None, description="Optional billing email address")
    billing_phone: str | None = Field(default=None, description="Optional billing phone number")
    metadata: dict[str, str] | None = Field(
        default=None,
        description="Optional additional credit card metadata fields",
    )

    @model_validator(mode="after")
    def normalize_empty_optional_fields(self) -> Self:
        if self.billing_address is not None and not self.billing_address.model_dump(exclude_none=True):
            self.billing_address = None
        if self.metadata == {}:
            self.metadata = None
        return self


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


class SecretCredential(BaseModel):
    """Generic secret credential."""

    secret_value: str = Field(..., min_length=1, description="The secret value", examples=["sk-abc123"])
    secret_label: str | None = Field(default=None, description="Optional label describing the secret")


class CredentialItem(BaseModel):
    """Model representing a credential item in the system."""

    item_id: str = Field(..., description="Unique identifier for the credential item", examples=["cred_1234567890"])
    name: str = Field(..., description="Name of the credential", examples=["Skyvern Login"])
    credential_type: CredentialType = Field(..., description="Type of the credential. Eg password, credit card, etc.")
    credential: PasswordCredential | CreditCardCredential | SecretCredential = Field(
        ..., description="The actual credential data"
    )


class CreateCredentialRequest(BaseModel):
    """Request model for creating a new credential."""

    name: str = Field(..., description="Name of the credential", examples=["Amazon Login"])
    credential_type: CredentialType = Field(..., description="Type of credential to create")
    credential: NonEmptyPasswordCredential | NonEmptyCreditCardCredential | SecretCredential = Field(
        ...,
        description="The credential data to store",
        examples=[{"username": "user@example.com", "password": "securepassword123"}],
    )
    vault_type: CredentialVaultType | None = Field(
        default=None,
        description="Which vault to store this credential in. If omitted, uses the instance default. "
        "Use this to mix Skyvern-hosted and custom credentials within the same organization.",
        examples=["skyvern", "custom", "azure_vault", "bitwarden"],
    )
    proxy_location: ProxyLocationInput = Field(
        default=None,
        description="Optional proxy location for this credential's pinned proxy identity.",
    )
    proxy_session_id: str | None = Field(
        default=None,
        description="Optional advanced reuse key for this credential's pinned proxy identity.",
    )
    rotate_proxy_session_id: bool = Field(
        default=False,
        description="Rotate the Skyvern-managed proxy sticky-session id when updating this credential.",
    )
    tested_url: str | None = Field(default=None, description="Login page URL used during the credential test")

    @field_validator("proxy_location", mode="before")
    @classmethod
    def deserialize_proxy_location_field(cls, value: object) -> object:
        return parse_proxy_location_input(value)

    @field_validator("proxy_session_id")
    @classmethod
    def validate_proxy_session_id_field(cls, value: str | None) -> str | None:
        return validate_proxy_session_id(value)


class CredentialResponse(BaseModel):
    """Response model for credential operations."""

    credential_id: str = Field(..., description="Unique identifier for the credential", examples=["cred_1234567890"])
    credential: PasswordCredentialResponse | CreditCardCredentialResponse | SecretCredentialResponse = Field(
        ..., description="The credential data"
    )
    credential_type: CredentialType = Field(..., description="Type of the credential")
    name: str = Field(..., description="Name of the credential", examples=["Amazon Login"])
    vault_type: CredentialVaultType | None = Field(
        default=None,
        description="Which vault stores this credential (e.g., 'skyvern', 'bitwarden', 'azure_vault', 'custom')",
    )
    browser_profile_id: str | None = Field(default=None, description="Browser profile ID linked to this credential")
    tested_url: str | None = Field(default=None, description="Login page URL used during the credential test")
    user_context: str | None = Field(
        default=None,
        description="User-provided context describing the login sequence (e.g., 'click SSO button first')",
    )
    save_browser_session_intent: bool | None = Field(
        default=None,
        description="Whether the user intends to save a browser session, regardless of test outcome",
    )
    folder_id: str | None = Field(
        default=None,
        description="ID of the credential folder this credential belongs to, if any",
        examples=["cfld_1234567890"],
    )
    proxy_location: ProxyLocationInput = Field(
        default=None,
        description="Optional proxy location used for the credential's pinned proxy identity.",
    )
    proxy_session_id: str | None = Field(
        default=None,
        description="Opaque Skyvern-managed proxy sticky-session id.",
    )

    @field_validator("proxy_session_id")
    @classmethod
    def validate_proxy_session_id_field(cls, value: str | None) -> str | None:
        return validate_proxy_session_id(value)


class OnePasswordItemOverview(BaseModel):
    """Response model for 1Password item metadata."""

    item_id: str = Field(..., description="The 1Password item ID")
    title: str = Field(..., description="The 1Password item title")
    vault_id: str = Field(..., description="The ID of the vault containing the item")
    vault_name: str = Field(..., description="The name of the vault containing the item")
    category: str = Field(..., description="The 1Password item category")
    url: str | None = Field(default=None, description="The primary website URL associated with the item, if any")


class OnePasswordItemsResponse(BaseModel):
    """Response model for listing 1Password item metadata."""

    configured: bool = Field(..., description="Whether a 1Password service account token is configured")
    items: list[OnePasswordItemOverview] = Field(..., description="The available 1Password item metadata")


class BitwardenItemOverview(BaseModel):
    """Response model for Bitwarden item metadata."""

    item_id: str = Field(..., description="The Bitwarden item ID")
    title: str = Field(..., description="The Bitwarden item title")
    collection_id: str | None = Field(
        default=None,
        description="The ID of a collection containing the item, if available",
    )
    credential_type: CredentialType = Field(..., description="The item's credential type")
    url: str | None = Field(default=None, description="The primary website URL associated with the item, if any")


class BitwardenItemsResponse(BaseModel):
    """Response model for listing Bitwarden item metadata."""

    configured: bool = Field(..., description="Whether Bitwarden credentials are configured")
    items: list[BitwardenItemOverview] = Field(..., description="The available Bitwarden item metadata")


class Credential(BaseModel):
    """Database model for credentials."""

    model_config = ConfigDict(from_attributes=True)

    credential_id: str = Field(..., description="Unique identifier for the credential", examples=["cred_1234567890"])
    organization_id: str = Field(
        ..., description="ID of the organization that owns the credential", examples=["o_1234567890"]
    )
    name: str = Field(..., description="Name of the credential", examples=["Skyvern Login"])
    vault_type: CredentialVaultType | None = Field(..., description="Where the secret is stored")
    item_id: str = Field(..., description="ID of the associated credential item", examples=["item_1234567890"])
    credential_type: CredentialType = Field(..., description="Type of the credential. Eg password, credit card, etc.")
    username: str | None = Field(..., description="For password credentials: the username")
    totp_type: TotpType = Field(
        TotpType.NONE,
        description="Type of 2FA method used for this credential",
        examples=[TotpType.AUTHENTICATOR],
    )
    totp_identifier: str | None = Field(
        default=None,
        description="Identifier (email or phone number) used to fetch TOTP codes",
        examples=["user@example.com", "+14155550123"],
    )
    card_last4: str | None = Field(..., description="For credit_card credentials: the last four digits of the card")
    card_brand: str | None = Field(..., description="For credit_card credentials: the card brand")
    secret_label: str | None = Field(default=None, description="For secret credentials: optional label")
    browser_profile_id: str | None = Field(default=None, description="Browser profile ID linked to this credential")
    tested_url: str | None = Field(default=None, description="Login page URL used during the credential test")
    user_context: str | None = Field(
        default=None,
        description="User-provided context describing the login sequence (e.g., 'click SSO button first')",
    )
    save_browser_session_intent: bool | None = Field(
        default=False,
        description="Whether the user intends to save a browser session, regardless of test outcome",
    )
    folder_id: str | None = Field(
        default=None,
        description="ID of the credential folder this credential belongs to, if any",
    )
    proxy_location: ProxyLocationInput = Field(
        default=None,
        description="Optional proxy location used for the credential's pinned proxy identity.",
    )
    proxy_session_id: str | None = Field(
        default=None,
        description="Opaque Skyvern-managed proxy sticky-session id.",
    )

    created_at: datetime = Field(..., description="Timestamp when the credential was created")
    modified_at: datetime = Field(..., description="Timestamp when the credential was last modified")
    deleted_at: datetime | None = Field(None, description="Timestamp when the credential was deleted, if applicable")

    @field_validator("proxy_location", mode="before")
    @classmethod
    def deserialize_proxy_location_field(cls, value: object) -> object:
        return parse_proxy_location_input(value)

    @field_validator("proxy_session_id")
    @classmethod
    def validate_proxy_session_id_field(cls, value: str | None) -> str | None:
        return validate_proxy_session_id(value)


class UpdateCredentialRequest(BaseModel):
    """Request model for updating credential metadata."""

    name: str | None = Field(
        default=None,
        min_length=1,
        description="New name for the credential",
        examples=["My Updated Credential"],
    )
    tested_url: str | None = Field(
        default=None,
        description="Optional login page URL associated with this credential",
        examples=["https://example.com/login"],
    )
    user_context: str | None = Field(
        default=None,
        max_length=1000,
        description="Optional user-provided context describing the login sequence (e.g., 'click SSO button first')",
    )
    save_browser_session_intent: bool | None = Field(
        default=None,
        description="Whether the user intends to save a browser session, regardless of test outcome",
    )
    proxy_location: ProxyLocationInput = Field(
        default=None,
        description="Optional proxy location for this credential's pinned proxy identity.",
    )
    proxy_session_id: str | None = Field(
        default=None,
        description="Opaque Skyvern-managed proxy sticky-session id.",
    )
    rotate_proxy_session_id: bool = Field(
        default=False,
        description="Rotate the Skyvern-managed proxy sticky-session id for this credential.",
    )

    @field_validator("user_context", mode="before")
    @classmethod
    def normalize_user_context(cls, v: str | None) -> str | None:
        return _normalize_optional_str(v)

    @field_validator("proxy_location", mode="before")
    @classmethod
    def deserialize_proxy_location_field(cls, value: object) -> object:
        return parse_proxy_location_input(value)

    @field_validator("proxy_session_id")
    @classmethod
    def validate_proxy_session_id_field(cls, value: str | None) -> str | None:
        return validate_proxy_session_id(value)

    @model_validator(mode="after")
    def _require_at_least_one_field(self) -> Self:
        if not self.model_fields_set:
            raise ValueError("At least one credential metadata field must be provided")
        return self


def _normalize_optional_str(v: str | None) -> str | None:
    """Normalize whitespace-only strings to None."""
    if v is not None and not v.strip():
        return None
    return v


class TestCredentialRequest(BaseModel):
    """Request model for testing a credential by logging into a website."""

    url: str = Field(
        ...,
        description="The login page URL to test the credential against",
        examples=["https://example.com/login"],
    )
    save_browser_profile: bool = Field(
        default=True,
        description="Whether to save the browser profile after a successful login test",
    )
    user_context: str | None = Field(
        default=None,
        max_length=1000,
        description="Optional user-provided context describing the login sequence (e.g., 'click SSO button first')",
    )

    @field_validator("user_context", mode="before")
    @classmethod
    def normalize_user_context(cls, v: str | None) -> str | None:
        return _normalize_optional_str(v)

    @model_validator(mode="after")
    def validate_url(self) -> Self:
        result = validate_url(self.url)
        if result is None:
            raise SkyvernHTTPException(message=f"Invalid URL: {self.url}", status_code=status.HTTP_400_BAD_REQUEST)
        self.url = result
        return self


class TestLoginRequest(BaseModel):
    """Request model for testing a login with inline credentials (no saved credential required)."""

    url: str = Field(
        ...,
        description="The login page URL to test against",
        examples=["https://example.com/login"],
    )
    username: str = Field(
        ...,
        min_length=1,
        description="The username to test",
        examples=["user@example.com"],
    )
    password: str = Field(
        ...,
        min_length=1,
        description="The password to test",
        examples=["securepassword123"],
    )
    totp: str | None = Field(
        default=None,
        description="Optional TOTP secret for 2FA",
    )
    totp_type: TotpType = Field(
        default=TotpType.NONE,
        description="Type of 2FA method",
    )
    totp_identifier: str | None = Field(
        default=None,
        description="Identifier (email or phone) for TOTP",
    )
    user_context: str | None = Field(
        default=None,
        max_length=1000,
        description="Optional user-provided context describing the login sequence (e.g., 'click SSO button first')",
    )
    proxy_location: ProxyLocationInput = Field(
        default=None,
        description="Optional proxy location for this test credential's pinned proxy identity.",
    )
    proxy_session_id: str | None = Field(
        default=None,
        description="Opaque Skyvern-managed proxy sticky-session id.",
    )

    @field_validator("user_context", mode="before")
    @classmethod
    def normalize_user_context(cls, v: str | None) -> str | None:
        return _normalize_optional_str(v)

    @field_validator("proxy_location", mode="before")
    @classmethod
    def deserialize_proxy_location_field(cls, value: object) -> object:
        return parse_proxy_location_input(value)

    @field_validator("proxy_session_id")
    @classmethod
    def validate_proxy_session_id_field(cls, value: str | None) -> str | None:
        return validate_proxy_session_id(value)

    @model_validator(mode="after")
    def validate_url(self) -> Self:
        result = validate_url(self.url)
        if result is None:
            raise SkyvernHTTPException(message=f"Invalid URL: {self.url}", status_code=status.HTTP_400_BAD_REQUEST)
        self.url = result
        return self


class TestCredentialResponse(BaseModel):
    """Response model for a credential test initiation."""

    credential_id: str = Field(..., description="The credential being tested")
    workflow_run_id: str = Field(
        ...,
        description="The workflow run ID to poll for test status",
        examples=["wr_1234567890"],
    )
    status: str = Field(
        ...,
        description="Current status of the test",
        examples=["running"],
    )


class TestLoginResponse(BaseModel):
    """Response model for an inline login test (no saved credential)."""

    credential_id: str = Field(
        ...,
        description="The temporary credential ID created for this test",
    )
    workflow_run_id: str = Field(
        ...,
        description="The workflow run ID to poll for test status",
        examples=["wr_1234567890"],
    )
    status: str = Field(
        ...,
        description="Current status of the test",
        examples=["running"],
    )


class TestCredentialStatusResponse(BaseModel):
    """Response model for credential test status polling."""

    credential_id: str = Field(..., description="The credential being tested")
    workflow_run_id: str = Field(..., description="The workflow run ID")
    status: str = Field(
        ...,
        description="Current status: created, running, completed, failed, timed_out",
        examples=["completed"],
    )
    failure_reason: str | None = Field(default=None, description="Reason for failure, if any")
    browser_profile_id: str | None = Field(
        default=None,
        description="Browser profile ID created from successful test.",
    )
    tested_url: str | None = Field(
        default=None,
        description="Login page URL used during the credential test.",
    )
    browser_profile_failure_reason: str | None = Field(
        default=None,
        description="Reason the browser profile failed to save, if applicable.",
    )


class CancelTestResponse(BaseModel):
    """Response model for canceling a credential test."""

    status: str = Field(
        ...,
        description="Result of the cancellation: 'canceled' or 'cancel_failed'",
        examples=["canceled"],
    )
