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


class ReliabilityState(StrEnum):
    healthy = "healthy"
    watch = "watch"
    action_needed = "action_needed"


RELIABILITY_WINDOW = 20
RELIABILITY_MIN_RUNS = 10


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


class RunHealGroup(BaseModel):
    workflow_run_id: str
    episodes: list[HealEpisode]


class WorkflowReliability(BaseModel):
    state: ReliabilityState
    outcome_risk: bool
    scored: bool
    window_runs: int
    healed_runs: int
    heal_rate: float
    consecutive_healed_runs: int
    floor_runs: int
    outcome_risk_runs: int


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


def compute_workflow_reliability(runs: list[RunHealGroup]) -> WorkflowReliability:
    run_flags: list[tuple[bool, bool, bool]] = []
    for run in runs:
        healed = any(
            episode.status in {HealStatus.fired_completed, HealStatus.fired_unverified, HealStatus.fired_failed}
            for episode in run.episodes
        )
        floor = any(episode.engine == "floor" for episode in run.episodes)
        outcome_risk = len(summarize_run_heals(run.episodes).blocks_outcome_risk) > 0
        run_flags.append((healed, floor, outcome_risk))

    window_runs = len(runs)
    healed_runs = sum(healed for healed, _, _ in run_flags)
    heal_rate = healed_runs / window_runs if window_runs else 0.0

    consecutive_healed_runs = 0
    for healed, _, _ in run_flags:
        if not healed:
            break
        consecutive_healed_runs += 1

    floor_runs = sum(floor for _, floor, _ in run_flags)
    recent_run_flags = run_flags[:10]
    recent_healed_runs = sum(healed for healed, _, _ in recent_run_flags)
    outcome_risk_runs = sum(outcome_risk for _, _, outcome_risk in recent_run_flags)

    scored = window_runs >= RELIABILITY_MIN_RUNS
    if not scored:
        state = ReliabilityState.healthy
    elif recent_healed_runs >= 3 or heal_rate >= 0.20 or floor_runs >= 2:
        state = ReliabilityState.action_needed
    elif healed_runs >= 2 or consecutive_healed_runs >= 2:
        state = ReliabilityState.watch
    else:
        state = ReliabilityState.healthy

    return WorkflowReliability(
        state=state,
        outcome_risk=outcome_risk_runs > 0,
        scored=scored,
        window_runs=window_runs,
        healed_runs=healed_runs,
        heal_rate=heal_rate,
        consecutive_healed_runs=consecutive_healed_runs,
        floor_runs=floor_runs,
        outcome_risk_runs=outcome_risk_runs,
    )


def reliability_state_transition(previous: ReliabilityState | None, current: ReliabilityState) -> bool:
    return previous is None or previous != current


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
