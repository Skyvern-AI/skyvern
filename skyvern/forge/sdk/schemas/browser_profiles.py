from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from skyvern.schemas.proxy_location import ProxyLocationInput
from skyvern.schemas.proxy_pinning import parse_proxy_location_input, validate_proxy_session_id


class BrowserProfile(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    browser_profile_id: str
    organization_id: str
    name: str
    description: str | None = None
    source_browser_type: str | None = None
    proxy_location: ProxyLocationInput = None
    proxy_session_id: str | None = None
    created_at: datetime
    modified_at: datetime
    deleted_at: datetime | None = None

    @field_validator("proxy_location", mode="before")
    @classmethod
    def deserialize_proxy_location_field(cls, value: object) -> object:
        return parse_proxy_location_input(value)

    @field_validator("proxy_session_id")
    @classmethod
    def validate_proxy_session_id_field(cls, value: str | None) -> str | None:
        return validate_proxy_session_id(value)


class CreateBrowserProfileRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    name: str = Field(..., min_length=1, description="Name for the browser profile")
    description: str | None = Field(None, description="Optional profile description")
    browser_session_id: str | None = Field(
        default=None,
        min_length=1,
        description="Persistent browser session to convert into a profile. Omit for a blank profile.",
    )
    workflow_run_id: str | None = Field(
        default=None,
        min_length=1,
        description="Workflow run whose persisted session should be captured. Omit for a blank profile.",
    )
    proxy_location: ProxyLocationInput = Field(
        default=None,
        description="Optional proxy location for this profile's pinned proxy identity.",
    )
    proxy_session_id: str | None = Field(
        default=None,
        description="Explicit sticky-session id for this profile's pinned proxy identity.",
    )

    @model_validator(mode="after")
    def _validate_source(self) -> "CreateBrowserProfileRequest":
        if self.browser_session_id is not None and self.workflow_run_id is not None:
            raise ValueError("Provide only one of browser_session_id or workflow_run_id")
        return self

    @field_validator("proxy_location", mode="before")
    @classmethod
    def deserialize_proxy_location_field(cls, value: object) -> object:
        return parse_proxy_location_input(value)

    @field_validator("proxy_session_id")
    @classmethod
    def validate_proxy_session_id_field(cls, value: str | None) -> str | None:
        return validate_proxy_session_id(value)


class UpdateBrowserProfileRequest(BaseModel):
    # `None` for either field means "no change" — there is currently no way to
    # clear a description back to null through this endpoint.
    model_config = ConfigDict(str_strip_whitespace=True)

    name: str | None = Field(default=None, min_length=1, description="New name for the browser profile")
    description: str | None = Field(default=None, description="New description for the browser profile")
    proxy_location: ProxyLocationInput = Field(
        default=None,
        description="Optional proxy location for this profile's pinned proxy identity.",
    )
    proxy_session_id: str | None = Field(
        default=None,
        description="Opaque Skyvern-managed proxy sticky-session id.",
    )
    rotate_proxy_session_id: bool = Field(
        default=False,
        description="Rotate the Skyvern-managed proxy sticky-session id for this profile.",
    )

    @model_validator(mode="after")
    def _require_at_least_one_field(self) -> "UpdateBrowserProfileRequest":
        if not self.model_fields_set:
            raise ValueError("At least one browser profile field must be provided")
        return self

    @field_validator("proxy_location", mode="before")
    @classmethod
    def deserialize_proxy_location_field(cls, value: object) -> object:
        return parse_proxy_location_input(value)

    @field_validator("proxy_session_id")
    @classmethod
    def validate_proxy_session_id_field(cls, value: str | None) -> str | None:
        return validate_proxy_session_id(value)
