from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel


class JobStatus(str, Enum):
    PENDING = "PENDING"
    ACCEPTED = "ACCEPTED"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    DISPUTED = "DISPUTED"


class BlockerType(str, Enum):
    CAPTCHA = "captcha"
    IDENTITY_VERIFICATION = "identity_verification"
    PHONE_VERIFICATION = "phone_verification"
    MANUAL_REVIEW = "manual_review"
    LOGIN_REQUIRED = "login_required"
    OTHER = "other"


class HumanSearchResult(BaseModel):
    id: str
    name: str | None = None
    skills: list[str] = []
    rating: float | None = None
    available: bool = True


class JobCreateRequest(BaseModel):
    humanId: str
    title: str
    description: str
    priceUsdc: float
    deadlineHours: int


class JobResponse(BaseModel):
    id: str
    status: JobStatus
    humanId: str
    title: str
    description: str
    result: Any | None = None


class JobMessage(BaseModel):
    id: str
    sender: str
    content: str
    createdAt: str


class FallbackRequest(BaseModel):
    url: str
    blocker_type: BlockerType
    description: str
    screenshot_url: str | None = None
    additional_context: dict[str, Any] | None = None


class FallbackResult(BaseModel):
    job_id: str
    status: JobStatus
    result: Any | None = None
    messages: list[JobMessage] = []
