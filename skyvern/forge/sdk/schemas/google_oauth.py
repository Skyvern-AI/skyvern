from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator


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
    scope_profile: Literal["google_sheets", "gmail"] | None = Field(
        default=None,
        description="Allowed Google OAuth scope profile to request. Defaults to google_sheets.",
        examples=["google_sheets", "gmail"],
    )
    app_origin: str | None = Field(
        default=None,
        description="Origin the callback should bounce back to (e.g. a Vercel preview URL). "
        "Must be on the GOOGLE_OAUTH_APP_ORIGINS allowlist.",
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
