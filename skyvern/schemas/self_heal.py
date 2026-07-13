from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict


class HealStatus(StrEnum):
    # fired_unverified: the heal ran but its outcome could not be verified against the block's
    # goal — distinct from fired_completed/fired_failed so an unverifiable heal is never recorded
    # as a definite success or failure (a light-proxy trap flagged in review).
    fired_completed = "fired_completed"
    fired_failed = "fired_failed"
    fired_unverified = "fired_unverified"
    skipped = "skipped"


class HealSkipReason(StrEnum):
    capped = "capped"
    adoption_failed = "adoption_failed"
    credential_unavailable = "credential_unavailable"
    timeout_class = "timeout_class"
    insecure_code = "insecure_code"
    unclassifiable = "unclassifiable"


class OutputObligation(StrEnum):
    none = "none"
    vestigial = "vestigial"
    observed = "observed"


@dataclass(frozen=True)
class HealClassification:
    healable: bool
    skip_reason: HealSkipReason | None


class HealEpisode(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    heal_episode_id: str
    organization_id: str
    workflow_permanent_id: str
    workflow_id: str
    workflow_run_id: str
    workflow_run_block_id: str
    block_label: str
    engine: str
    status: HealStatus
    skip_reason: HealSkipReason | None = None
    block_prompt: str | None = None
    block_code: str | None = None
    block_steps: list | dict | None = None
    snapshot_available: bool = False
    convergence_eligible: bool = False
    parameter_binding_keys: list[str] | dict | None = None
    exception_class: str | None = None
    failing_line: int | None = None
    matched_step_index: int | None = None
    failure_message: str | None = None
    escalation_task_id: str | None = None
    wall_clock_ms: int | None = None
    action_count: int | None = None
    output_obligation: OutputObligation | None = None
    dom_snapshot_artifact_id: str | None = None
    scout_transcript_artifact_id: str | None = None
    screenshot_artifact_id: str | None = None
    created_at: datetime
    modified_at: datetime


class WorkflowHealProposal(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    heal_proposal_id: str
    organization_id: str
    workflow_permanent_id: str
    block_label: str
    candidate_definition: dict | list
    provenance: dict | list | None = None
    episode_ids: list[str]
    rendered_diff: str | None = None
    base_version: int
    base_definition_hash: str
    status: Literal["proposed", "adopted", "rejected", "stale"] = "proposed"
    adopted_workflow_id: str | None = None
    episode_window: str | None = None
    created_at: datetime
    modified_at: datetime
