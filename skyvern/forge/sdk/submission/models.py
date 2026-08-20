from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import TypeAlias

from pydantic import BaseModel, ConfigDict, Field


class SubmissionSignal(StrEnum):
    SUBMITTED_VERIFIED = "submitted_verified"
    SUBMITTED_LIKELY = "submitted_likely"
    NOT_SUBMITTED = "not_submitted"
    NOT_EVALUATED = "not_evaluated"


class BrowserPath(StrEnum):
    SKYVERN_CREATED = "skyvern_created"
    CDP_CONNECT = "cdp_connect"
    VENDOR_REUSED = "vendor_reused"
    SESSION_ATTACHED = "session_attached"
    UNKNOWN = "unknown"


class SubmitCandidate(BaseModel):
    model_config = ConfigDict(frozen=True)

    action_id: str | None = None
    task_id: str | None = None
    step_id: str


class SubmitCandidateDetection(BaseModel):
    candidates: list[SubmitCandidate]
    coordinate_click: bool = False

    @property
    def submit_intent_detected(self) -> bool:
        return bool(self.candidates)


class CandidateWindow(BaseModel):
    model_config = ConfigDict(frozen=True)

    task_id: str | None = None
    step_id: str
    candidate_action_id: str | None = None
    started_at: datetime
    ended_at: datetime


class NetworkEvidence(BaseModel):
    model_config = ConfigDict(frozen=True)

    origin: str
    method: str
    status: int
    started_at: datetime
    correlated_step_id: str


class PageConfirmationEvidence(BaseModel):
    model_config = ConfigDict(frozen=True)

    phrase: str
    value_sha256: str
    absent_pre_submit: bool


class UrlTransitionEvidence(BaseModel):
    model_config = ConfigDict(frozen=True)

    from_origin: str
    to_origin: str
    path_changed: bool


class DownloadEvidence(BaseModel):
    model_config = ConfigDict(frozen=True)

    file_count: int


TierBEvidence: TypeAlias = PageConfirmationEvidence | UrlTransitionEvidence | DownloadEvidence


class TierAEvaluation(BaseModel):
    evidence: list[NetworkEvidence]
    har_present: bool
    har_parsed: bool
    har_entry_count: int
    ambiguous_entry_count: int = 0


class TierBEvaluation(BaseModel):
    evidence: list[TierBEvidence]
    page_confirmation_evaluated: bool


class SubmissionVerdict(BaseModel):
    signal: SubmissionSignal
    tier_a: list[NetworkEvidence]
    tier_b: list[TierBEvidence]
    submit_intent_detected: bool
    har_present: bool
    har_parsed: bool
    har_entry_count: int
    browser_path: BrowserPath
    winning_step_id: str | None = None
    capped: bool = False
    notes: list[str] = Field(default_factory=list)
