from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator


class BrowserProfile(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    browser_profile_id: str
    organization_id: str
    name: str
    description: str | None = None
    created_at: datetime
    modified_at: datetime
    deleted_at: datetime | None = None


class CreateBrowserProfileRequest(BaseModel):
    name: str = Field(..., description="Name for the browser profile")
    description: str | None = Field(None, description="Optional profile description")
    browser_session_id: str | None = Field(
        default=None, description="Persistent browser session to convert into a profile"
    )
    workflow_run_id: str | None = Field(
        default=None, description="Workflow run whose persisted session should be captured"
    )

    @model_validator(mode="after")
    def _validate_source(self) -> "CreateBrowserProfileRequest":
        if bool(self.browser_session_id) == bool(self.workflow_run_id):
            raise ValueError("Provide either browser_session_id or workflow_run_id")
        return self
