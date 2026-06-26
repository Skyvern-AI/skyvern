from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator


class BrowserProfile(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    browser_profile_id: str
    organization_id: str
    name: str
    description: str | None = None
    source_browser_type: str | None = None
    created_at: datetime
    modified_at: datetime
    deleted_at: datetime | None = None


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

    @model_validator(mode="after")
    def _validate_source(self) -> "CreateBrowserProfileRequest":
        if self.browser_session_id is not None and self.workflow_run_id is not None:
            raise ValueError("Provide only one of browser_session_id or workflow_run_id")
        return self


class UpdateBrowserProfileRequest(BaseModel):
    # `None` for either field means "no change" — there is currently no way to
    # clear a description back to null through this endpoint.
    model_config = ConfigDict(str_strip_whitespace=True)

    name: str | None = Field(default=None, min_length=1, description="New name for the browser profile")
    description: str | None = Field(default=None, description="New description for the browser profile")

    @model_validator(mode="after")
    def _require_at_least_one_field(self) -> "UpdateBrowserProfileRequest":
        if self.name is None and self.description is None:
            raise ValueError("At least one of `name` or `description` must be provided")
        return self
