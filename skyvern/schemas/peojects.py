from datetime import datetime

from pydantic import BaseModel, ConfigDict


class Project(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    project_revision_id: str
    project_id: str
    organization_id: str
    artifact_id: str | None = None
    version: int | None = None
    created_at: datetime
    modified_at: datetime
