from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class AISuggestionBase(BaseModel):
    output: dict[str, Any] | str | None = None


class AISuggestion(AISuggestionBase):
    model_config = ConfigDict(from_attributes=True)
    ai_suggestion_type: str
    ai_suggestion_id: str
    organization_id: str | None = None

    created_at: datetime
    modified_at: datetime


class AISuggestionRequest(BaseModel):
    input: str = Field(..., min_length=1)
    context: dict[str, Any] | None = None
