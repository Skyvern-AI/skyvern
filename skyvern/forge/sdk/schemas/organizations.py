from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

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


class CreateCustomCredentialServiceConfigRequest(BaseModel):
    """Request model for creating or updating custom credential service configuration."""

    config: CustomCredentialServiceConfig


class GetOrganizationsResponse(BaseModel):
    organizations: list[Organization]


class GetOrganizationAPIKeysResponse(BaseModel):
    api_keys: list[OrganizationAuthToken]


class OrganizationUpdate(BaseModel):
    max_steps_per_run: int | None = None
