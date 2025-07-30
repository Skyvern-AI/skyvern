from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class SkyvernProject(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    skyvern_project_id: str
    organization_id: str
    artifact_id: str | None = None
    structure: dict[str, Any] | None = None
    created_at: datetime
    modified_at: datetime
    deleted_at: datetime | None = None
