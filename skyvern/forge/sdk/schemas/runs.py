from datetime import datetime

from pydantic import BaseModel, ConfigDict

from skyvern.schemas.runs import RunType


class Run(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    task_run_id: str
    task_run_type: RunType
    run_id: str
    organization_id: str | None = None
    title: str | None = None
    url: str | None = None
    cached: bool = False
    created_at: datetime
    modified_at: datetime
