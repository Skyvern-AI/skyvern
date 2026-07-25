from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator


class GoogleOAuthClientConfig(BaseModel):
    client_id: str = Field(..., min_length=1)
    client_secret: str = Field(..., min_length=1)
    redirect_hosts: list[str] = Field(default_factory=list)
    app_origins: list[str] = Field(default_factory=list)

    @field_validator("client_id", "client_secret")
    @classmethod
    def _strip_required_string(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("value must not be blank")
        return stripped

    @field_validator("redirect_hosts", "app_origins")
    @classmethod
    def _strip_list_values(cls, values: list[str]) -> list[str]:
        return [item.strip() for item in values if item and item.strip()]


class GoogleOAuthClientConfigSafe(BaseModel):
    client_id: str | None = None
    redirect_hosts: list[str] = Field(default_factory=list)
    app_origins: list[str] = Field(default_factory=list)
    client_secret_configured: bool = False
    configured: bool = False
    source: str = "missing"
    encryption_enabled: bool = False


class GoogleOAuthClientConfigResponse(BaseModel):
    config: GoogleOAuthClientConfigSafe


class UpdateGoogleOAuthClientConfigRequest(BaseModel):
    client_id: str = Field(..., min_length=1)
    client_secret: str | None = Field(
        default=None,
        description="Google OAuth client secret. Omit or leave blank to preserve the current organization-level secret.",
    )
    redirect_hosts: list[str] = Field(default_factory=list)
    app_origins: list[str] = Field(default_factory=list)

    @field_validator("client_id")
    @classmethod
    def _strip_client_id(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("client_id must not be blank")
        return stripped

    @field_validator("client_secret")
    @classmethod
    def _strip_optional_secret(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None

    @field_validator("redirect_hosts", "app_origins")
    @classmethod
    def _strip_update_list_values(cls, values: list[str]) -> list[str]:
        return [item.strip() for item in values if item and item.strip()]


class GoogleOAuthCredentialBase(BaseModel):
    id: str
    organization_id: str
    credential_name: str
    provider: str = "google"
    state: str
    scopes_requested: list[str] = Field(default_factory=list)
    scopes_granted: list[str] = Field(default_factory=list)
    created_at: datetime
    modified_at: datetime


class GoogleOAuthCredentialResponse(BaseModel):
    credential: GoogleOAuthCredentialBase
    app_origin: str | None = None


class GoogleOAuthCredentialListResponse(BaseModel):
    credentials: list[GoogleOAuthCredentialBase]


class CreateGoogleOAuthAuthorizeRequest(BaseModel):
    redirect_uri: str = Field(..., description="Redirect URI the consent flow will return to")
    credential_name: str = Field(default="Default", description="Human-readable name for this credential")
    scope_profile: Literal["google_sheets", "gmail", "google_drive"] | None = Field(
        default=None,
        description="Allowed Google OAuth scope profile to request. Defaults to google_sheets.",
        examples=["google_sheets", "gmail", "google_drive"],
    )
    app_origin: str | None = Field(
        default=None,
        description="Origin the callback should bounce back to (e.g. a Vercel preview URL). "
        "Must be on the GOOGLE_OAUTH_APP_ORIGINS allowlist.",
    )
    credential_id: str | None = Field(
        default=None,
        description="Existing credential to re-authenticate in place. When set, the connection keeps "
        "its identity so workflows referencing it continue to work without edits.",
    )


class GoogleOAuthAuthorizeResponse(BaseModel):
    authorize_url: str
    state: str


class CreateGoogleOAuthCallbackRequest(BaseModel):
    code: str = Field(..., description="Authorization code from Google OAuth consent flow")
    state: str = Field(..., description="State token returned by Google; must match a pending authorize request")


class UpdateGoogleOAuthCredentialRequest(BaseModel):
    credential_name: str = Field(
        ...,
        min_length=1,
        max_length=128,
        description="New human-readable name for this credential",
    )

    @field_validator("credential_name")
    @classmethod
    def _strip_and_reject_blank(cls, v: str) -> str:
        stripped = v.strip()
        if not stripped:
            raise ValueError("credential_name must not be blank")
        return stripped
