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


class OrganizationAuthToken(BaseModel):
    id: str
    organization_id: str
    token_type: OrganizationAuthTokenType
    token: str
    valid: bool
    created_at: datetime
    modified_at: datetime


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


class GetOrganizationsResponse(BaseModel):
    organizations: list[Organization]


class GetOrganizationAPIKeysResponse(BaseModel):
    api_keys: list[OrganizationAuthToken]


class OrganizationUpdate(BaseModel):
    max_steps_per_run: int | None = None
