"""Shared dataclasses for the v3 agent loops.

Types are kept plain-old dataclasses (not Pydantic models) because they're
short-lived, in-process only, and never serialized to the DB or an API.
The :class:`ScriptFallbackEpisode` field on :class:`PostRunContext` IS a
Pydantic model — it flows in from the repository layer.
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Literal

from skyvern.schemas.scripts import ScriptFallbackEpisode
from skyvern.services.script_reviewer_v3.budget import Budget, RunBudget
from skyvern.services.script_reviewer_v3.decision import Decision

if TYPE_CHECKING:
    from playwright.async_api import Page as PlaywrightPage

    from skyvern.forge.sdk.core.skyvern_context import SkyvernContext


# Intercepted action types for mid-run v3 v1. select_option and upload_file
# are OUT of scope (see Phase 2a manifest in task_plan.md). Keeping the
# literal tight so type-checkers catch accidental extension.
InterceptedActionType = Literal["click", "fill", "type", "fill_autocomplete"]


@dataclasses.dataclass
class FailureContext:
    """Passed to ``v3_review_in_flight`` when the mid-run hook intercepts a
    cached-selector failure. Carries everything the agent needs to reproduce
    the action via ``apply_fix_in_flight``-equivalent skills AND the episode
    ID the agent-loop caller uses to persist outcomes.

    ``page`` is a live Playwright handle — the defining feature of mid-run v3.
    """

    failed_selector: str | None
    intention: str
    action_type: InterceptedActionType
    value: str | None  # for fill / type / fill_autocomplete
    totp_identifier: str | None  # for fill with TOTP
    totp_url: str | None
    page: PlaywrightPage
    context: SkyvernContext
    episode_id: str


@dataclasses.dataclass
class PostRunContext:
    """Assembled at workflow end for v3-cohort workflows. Feeds the post-run
    v3 agent loop.

    Note ``script_revision_id`` may have been advanced during the run by one or
    more mid-run ``persist_script_version`` calls; the post-run agent operates
    on the latest revision.
    """

    organization_id: str
    workflow_permanent_id: str
    workflow_run_id: str
    script_revision_id: str
    workflow_outcome: Literal["completed", "failed", "terminated", "canceled", "timeout"]
    workflow_error_message: str | None
    workflow_duration_seconds: float
    last_block_label: str | None
    # All episodes from this run, chronological.
    episodes: list[ScriptFallbackEpisode]
    # Episode IDs where mid-run produced declare_success. Candidates for
    # demotion review during the post-run pass.
    mid_run_class_a_episode_ids: list[str]
    budget: Budget


@dataclasses.dataclass
class V3MidRunResult:
    """Return value of ``v3_review_in_flight``. The mid-run hook inspects
    ``decision.type`` to decide whether to return early (Class A) or fall
    through to agent fallback (Class B)."""

    decision: Decision
    budget_used: Budget | None
    timeline: list[dict]


@dataclasses.dataclass
class PerEpisodePersistResult:
    """Structured outcome of one per-episode persist. The post-run loop
    collects per-episode decisions during execution, then a single
    deterministic pass at the loop boundary applies them. Each application
    produces one of these so the caller can audit which episodes were
    actually written vs. silently dropped.
    """

    episode_id: str
    decision_type: Literal["declare_review_complete", "give_up_episode", "demote_class_a"]
    persisted: bool
    error: str | None = None  # non-None when persist failed; carries the exception's str() for logs


@dataclasses.dataclass
class MintAuditContext:
    """Passed to ``v3_review_at_mint`` when a freshly-minted cached script
    has static findings. The agent operates on a single script revision and
    a fixed list of findings; there are no episodes (the script is fresh).

    Unlike :class:`PostRunContext`, this context exists to react to a
    static-analysis signal — not to a runtime fallback. The agent's job is
    narrow: decide whether each finding is a real defect, and if so, edit
    the block to remove it.
    """

    organization_id: str
    workflow_permanent_id: str
    workflow_run_id: str
    script_revision_id: str
    # The user's original task prompt, so the agent can reason about what
    # the script is supposed to be doing.
    user_prompt: str
    # Findings produced by ``find_suspicious_selector_literals``. Each entry
    # has fields {type, literal, selector, reason, file_path}.
    findings: list[dict]
    budget: Budget


@dataclasses.dataclass
class V3MintAuditResult:
    """Return value of ``v3_review_at_mint``."""

    decision: Decision  # global terminal — declare_post_run_complete / abandon_post_run / loop_error
    budget_used: Budget
    timeline: list[dict]
    # Per-finding persist outcomes — populated if the agent emitted any
    # persist_block_edit calls during the review.
    new_script_revision_ids: list[str] = dataclasses.field(default_factory=list)


@dataclasses.dataclass
class V3PostRunResult:
    """Return value of ``v3_review_post_run``. Post-run has both per-episode
    decisions and a single global terminal."""

    decision: Decision  # The global terminal (declare_post_run_complete / abandon_post_run / loop_error)
    per_episode_decisions: dict[str, Decision]  # episode_id -> per-episode terminal
    budget_used: Budget
    timeline: list[dict]
    # Structured persist outcomes from the post-loop deterministic pass.
    # Populated even when persists fail so the caller sees what was attempted.
    per_episode_persist_results: dict[str, PerEpisodePersistResult] = dataclasses.field(default_factory=dict)


__all__ = [
    "FailureContext",
    "InterceptedActionType",
    "MintAuditContext",
    "PerEpisodePersistResult",
    "PostRunContext",
    "RunBudget",
    "V3MidRunResult",
    "V3MintAuditResult",
    "V3PostRunResult",
]
