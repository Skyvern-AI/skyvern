from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator


class MicrosoftOAuthCredentialBase(BaseModel):
    id: str
    organization_id: str
    credential_name: str
    state: str
    scopes_requested: list[str] = Field(default_factory=list)
    scopes_granted: list[str] = Field(default_factory=list)
    created_at: datetime
    modified_at: datetime


class MicrosoftOAuthCredentialResponse(BaseModel):
    credential: MicrosoftOAuthCredentialBase
    app_origin: str | None = None


class MicrosoftOAuthCredentialListResponse(BaseModel):
    credentials: list[MicrosoftOAuthCredentialBase]


class CreateMicrosoftOAuthAuthorizeRequest(BaseModel):
    redirect_uri: str = Field(..., description="Redirect URI the consent flow will return to")
    credential_name: str = Field(default="Default", description="Human-readable name for this credential")
    scope_profile: Literal["outlook_mail"] | None = Field(
        default=None,
        description="Allowed Microsoft OAuth scope profile to request. Defaults to outlook_mail.",
        examples=["outlook_mail"],
    )
    app_origin: str | None = Field(
        default=None,
        description="Origin the callback should bounce back to. Must be on the MICROSOFT_OAUTH_APP_ORIGINS allowlist.",
    )


class MicrosoftOAuthAuthorizeResponse(BaseModel):
    authorize_url: str
    state: str


class CreateMicrosoftOAuthCallbackRequest(BaseModel):
    code: str = Field(..., description="Authorization code from Microsoft OAuth consent flow")
    state: str = Field(..., description="State token returned by Microsoft; must match a pending authorize request")


class UpdateMicrosoftOAuthCredentialRequest(BaseModel):
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
