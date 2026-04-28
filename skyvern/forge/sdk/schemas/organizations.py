from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from skyvern.forge.sdk.db.enums import OrganizationAuthTokenType


class Organization(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    organization_id: str
    organization_name: str
    webhook_callback_url: str | None = None
    max_steps_per_run: int | None = None
    max_retries_per_step: int | None = None
    domain: str | None = None
    bw_organization_id: str | None = None
    bw_collection_ids: list[str] | None = None
    artifact_url_expiry_seconds: int | None = Field(
        None,
        description=(
            "Per-org override for the lifetime of signed /v1/artifacts/{id}/content URLs, "
            "in seconds. None means use the global default (12 hours). When set, every signed "
            "URL minted for artifacts owned by this org is valid for this many seconds. "
            "Bounded between 1 hour (3600) and 7 days (604800)."
        ),
    )

    created_at: datetime
    modified_at: datetime


class OrganizationAuthTokenBase(BaseModel):
    id: str
    organization_id: str
    token_type: OrganizationAuthTokenType
    valid: bool
    created_at: datetime
    modified_at: datetime


class OrganizationAuthToken(OrganizationAuthTokenBase):
    token: str


class AzureClientSecretCredential(BaseModel):
    tenant_id: str
    client_id: str
    client_secret: str


class AzureOrganizationAuthToken(OrganizationAuthTokenBase):
    """Represents OrganizationAuthToken for Azure; defined by 3 fields: tenant_id, client_id, and client_secret"""

    credential: AzureClientSecretCredential


class BitwardenCredential(BaseModel):
    email: EmailStr = Field(..., description="Bitwarden account email")
    master_password: str = Field(..., min_length=1, description="Bitwarden master password")


class BitwardenCredentialSafe(BaseModel):
    """Response-safe view of BitwardenCredential — master_password is never returned."""

    email: EmailStr


class BitwardenOrganizationAuthToken(OrganizationAuthTokenBase):
    """Represents OrganizationAuthToken for Bitwarden; defined by 2 fields: email and master_password"""

    credential: BitwardenCredential


class BitwardenOrganizationAuthTokenSafe(OrganizationAuthTokenBase):
    """Response-safe view — omits master_password for security."""

    credential: BitwardenCredentialSafe


class CreateBitwardenCredentialRequest(BaseModel):
    """Request model for creating or updating a Bitwarden credential."""

    credential: BitwardenCredential


class BitwardenCredentialResponse(BaseModel):
    """Response model for Bitwarden credential operations.

    The master_password is never returned in API responses for security.
    To update credentials, submit a new POST request with the full credential.
    """

    token: BitwardenOrganizationAuthTokenSafe = Field(
        ...,
        description="The Bitwarden credential (master_password redacted for security)",
    )


class CreateOnePasswordTokenRequest(BaseModel):
    """Request model for creating or updating a 1Password service account token."""

    token: str = Field(
        ...,
        description="The 1Password service account token",
        examples=["op_1234567890abcdef"],
    )


class CreateOnePasswordTokenResponse(BaseModel):
    """Response model for 1Password token operations."""

    token: OrganizationAuthToken = Field(
        ...,
        description="The created or updated 1Password service account token",
    )


class AzureClientSecretCredentialResponse(BaseModel):
    """Response model for Azure ClientSecretCredential operations."""

    token: AzureOrganizationAuthToken = Field(
        ...,
        description="The created or updated Azure ClientSecretCredential",
    )


class CreateAzureClientSecretCredentialRequest(BaseModel):
    """Request model for creating or updating an Azure ClientSecretCredential."""

    credential: AzureClientSecretCredential


class CustomCredentialServiceConfig(BaseModel):
    """Configuration for custom credential service."""

    api_base_url: str = Field(
        ...,
        description="Base URL for the custom credential API",
        examples=["https://credentials.company.com/api/v1/credentials"],
    )
    api_token: str = Field(
        ...,
        description="API token for authenticating with the custom credential service",
        examples=["your_api_token_here"],
    )


class CustomCredentialServiceConfigResponse(BaseModel):
    """Response model for custom credential service operations."""

    token: OrganizationAuthToken = Field(
        ...,
        description="The created or updated custom credential service configuration",
    )


class TestConnectionResponse(BaseModel):
    """Response model for the custom credential service connection test."""

    success: bool


class CreateCustomCredentialServiceConfigRequest(BaseModel):
    """Request model for creating or updating custom credential service configuration."""

    config: CustomCredentialServiceConfig


class GetOrganizationsResponse(BaseModel):
    organizations: list[Organization]


class GetOrganizationAPIKeysResponse(BaseModel):
    api_keys: list[OrganizationAuthToken]


class OrganizationUpdate(BaseModel):
    max_steps_per_run: int | None = Field(default=None, ge=1)
    # 0 is a valid "disable retries" value — see ForgeAgent.execute_step.
    max_retries_per_step: int | None = Field(default=None, ge=0)
    webhook_callback_url: str | None = None
    artifact_url_expiry_seconds: int | None = Field(
        None,
        description=(
            "Per-org override for the lifetime of signed /v1/artifacts/{id}/content URLs, "
            "in seconds. Bounded between 1 hour (3600) and 7 days (604800). Pass null to "
            "leave the current value unchanged. To explicitly clear the override (and fall "
            "back to the global 12-hour default) set ``clear_artifact_url_expiry_seconds`` "
            "to true."
        ),
    )
    clear_artifact_url_expiry_seconds: bool = Field(
        False,
        description=(
            "When true, resets ``artifact_url_expiry_seconds`` to NULL — the org will use "
            "the global 12-hour default. Mutually exclusive with a non-null value in "
            "``artifact_url_expiry_seconds`` (the clear flag wins)."
        ),
    )
