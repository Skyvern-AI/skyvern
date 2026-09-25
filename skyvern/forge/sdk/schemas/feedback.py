from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

FeedbackTargetType = Literal["workflow_run", "task", "copilot_message"]
RunFeedbackTargetType = Literal["workflow_run", "task"]
FeedbackRating = Literal["up", "down"]


class FeedbackEvent(BaseModel):
    """Target-agnostic payload handed to ``AgentFunction.on_feedback_submitted`` after a rating is stored."""

    target_type: FeedbackTargetType
    target_id: str
    context_id: str | None = Field(
        None, description="Parent of the target: workflow_permanent_id for runs, chat id for copilot messages"
    )
    rating: FeedbackRating | None = Field(None, description="None means the user cleared a previous rating")
    previous_rating: FeedbackRating | None = Field(
        None, description="Rating stored before this write, so sinks can tell a new rating from added details"
    )
    reason: str | None = None
    needs_support: bool = False
    submitted_by: str | None = None


class RunFeedbackRequest(BaseModel):
    target_type: RunFeedbackTargetType
    target_id: str = Field(..., min_length=1, max_length=128)
    rating: FeedbackRating | None = Field(None, description="Thumbs up or down; null clears a previous rating")
    reason: str | None = Field(None, max_length=2000)
    needs_support: bool = False
    submitted_by: str | None = Field(None, max_length=320, description="Email of the signed-in user, when known")


class RunFeedback(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    run_feedback_id: str
    organization_id: str
    target_type: RunFeedbackTargetType
    target_id: str
    context_id: str | None = None
    rating: FeedbackRating
    reason: str | None = None
    needs_support: bool = False
    submitted_by: str | None = None
    created_at: datetime
    modified_at: datetime
