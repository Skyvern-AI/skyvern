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


class HealEpisodeView(BaseModel):
    heal_episode_id: str
    workflow_permanent_id: str
    workflow_id: str
    workflow_run_id: str
    workflow_run_block_id: str
    block_label: str
    engine: str
    status: HealStatus
    skip_reason: HealSkipReason | None = None
    snapshot_available: bool = False
    convergence_eligible: bool = False
    parameter_binding_keys: list[str] | dict | None = None
    exception_class: str | None = None
    failing_line: int | None = None
    matched_step_index: int | None = None
    escalation_task_id: str | None = None
    wall_clock_ms: int | None = None
    action_count: int | None = None
    output_obligation: OutputObligation | None = None
    dom_snapshot_artifact_id: str | None = None
    scout_transcript_artifact_id: str | None = None
    screenshot_artifact_id: str | None = None
    created_at: datetime
    modified_at: datetime

    @classmethod
    def from_episode(cls, episode: HealEpisode) -> HealEpisodeView:
        return cls(
            heal_episode_id=episode.heal_episode_id,
            workflow_permanent_id=episode.workflow_permanent_id,
            workflow_id=episode.workflow_id,
            workflow_run_id=episode.workflow_run_id,
            workflow_run_block_id=episode.workflow_run_block_id,
            block_label=episode.block_label,
            engine=episode.engine,
            status=episode.status,
            skip_reason=episode.skip_reason,
            snapshot_available=episode.snapshot_available,
            convergence_eligible=episode.convergence_eligible,
            parameter_binding_keys=episode.parameter_binding_keys,
            exception_class=episode.exception_class,
            failing_line=episode.failing_line,
            matched_step_index=episode.matched_step_index,
            escalation_task_id=episode.escalation_task_id,
            wall_clock_ms=episode.wall_clock_ms,
            action_count=episode.action_count,
            output_obligation=episode.output_obligation,
            dom_snapshot_artifact_id=episode.dom_snapshot_artifact_id,
            scout_transcript_artifact_id=episode.scout_transcript_artifact_id,
            screenshot_artifact_id=episode.screenshot_artifact_id,
            created_at=episode.created_at,
            modified_at=episode.modified_at,
        )


class HealEpisodeDetail(HealEpisodeView):
    sanitized_block_code: str | None = None
    sanitized_block_prompt: str | None = None
    sanitized_failure_message: str | None = None
    block_steps: list | dict | None = None


def resolve_block_outcome(
    episodes: list[HealEpisode],
) -> Literal["healed", "unverified", "failed", "skipped", "none"]:
    statuses = {episode.status for episode in episodes}
    if HealStatus.fired_completed in statuses:
        return "healed"
    if HealStatus.fired_unverified in statuses:
        return "unverified"
    if HealStatus.fired_failed in statuses:
        return "failed"
    if HealStatus.skipped in statuses:
        return "skipped"
    return "none"


class RunHealSummary(BaseModel):
    blocks_healed: int
    blocks_outcome_risk: list[str]
    blocks_with_heal_attempt: int


def summarize_run_heals(episodes: list[HealEpisode]) -> RunHealSummary:
    episodes_by_block: dict[str, list[HealEpisode]] = {}
    for episode in episodes:
        episodes_by_block.setdefault(episode.block_label, []).append(episode)

    blocks_healed = 0
    blocks_outcome_risk: list[str] = []
    for block_label, block_episodes in episodes_by_block.items():
        outcome = resolve_block_outcome(block_episodes)
        if outcome == "healed":
            blocks_healed += 1
            continue
        if outcome not in {"unverified", "failed"}:
            continue
        if any(
            episode.output_obligation in {OutputObligation.observed, OutputObligation.vestigial}
            for episode in block_episodes
        ):
            blocks_outcome_risk.append(block_label)

    return RunHealSummary(
        blocks_healed=blocks_healed,
        blocks_outcome_risk=sorted(blocks_outcome_risk),
        blocks_with_heal_attempt=len(episodes_by_block),
    )


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
