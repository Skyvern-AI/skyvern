"""Post-run agent entry point.

Public API: :func:`v3_review_post_run`. Called from
``WorkflowService._trigger_script_reviewer`` for v3-cohort workflow runs.

Lifecycle:

1. Build :class:`PostRunContext` from DB rows for the run.
2. Run the agent loop with all post-run skills.
3. Process per-episode terminals into ``mark_episode_reviewed`` /
   ``update_fallback_episode`` calls as appropriate.
4. Update the cap counter via ``_check_and_increment_cap_v3`` (or v2's helper
   — for now the post-run trigger reuses v2's bucketing semantics).
5. Return a :class:`V3PostRunResult`.
"""

from __future__ import annotations

import asyncio
from typing import Any

import structlog

from skyvern.forge import app
from skyvern.forge.sdk.api.llm.api_handler_factory import LLMCaller
from skyvern.services.script_reviewer_v3.agent_loop import run_agent_loop
from skyvern.services.script_reviewer_v3.budget import (
    DEFAULT_POSTRUN_MAX_COST_USD,
    DEFAULT_POSTRUN_MAX_CYCLES,
    DEFAULT_POSTRUN_MAX_TOKENS,
    DEFAULT_POSTRUN_MAX_WALL_SECONDS,
    Budget,
)
from skyvern.services.script_reviewer_v3.cohort import resolve_v3_budget_payload
from skyvern.services.script_reviewer_v3.decision import Decision
from skyvern.services.script_reviewer_v3.llm_adapter import V3_REVIEWER_MODEL
from skyvern.services.script_reviewer_v3.prompts import (
    POSTRUN_SYSTEM_PROMPT,
    build_postrun_user_prompt,
)
from skyvern.services.script_reviewer_v3.registry_builder import build_registry
from skyvern.services.script_reviewer_v3.types import (
    PerEpisodePersistResult,
    PostRunContext,
    V3PostRunResult,
)

LOG = structlog.get_logger()


_DEFAULT_POSTRUN_WALL_SECONDS = DEFAULT_POSTRUN_MAX_WALL_SECONDS


async def build_post_run_context(
    organization_id: str,
    workflow_run: Any,
    workflow_permanent_id: str,
    script_revision_id: str,
) -> PostRunContext:
    """Assemble :class:`PostRunContext` from DB rows.

    Failures fetching individual fields are tolerated — the agent gets
    whatever we could load. A wholesale failure raises to the caller, which
    treats it as an abandon_post_run.
    """
    episodes = await app.DATABASE.scripts.get_all_episodes_by_workflow_run_id(
        workflow_run_id=workflow_run.workflow_run_id,
        organization_id=organization_id,
    )
    mid_run_class_a_ids: list[str] = [
        e.episode_id for e in episodes if e.reviewer_version == "v3" and e.reviewed and not e.fallback_succeeded
    ]

    payload = await resolve_v3_budget_payload(workflow_permanent_id=workflow_permanent_id)
    budget = Budget(
        max_cycles=int(payload.get("postrun_max_cycles", DEFAULT_POSTRUN_MAX_CYCLES)),
        max_tokens=int(payload.get("postrun_max_tokens", DEFAULT_POSTRUN_MAX_TOKENS)),
        max_cost_usd=float(payload.get("postrun_max_cost_usd", DEFAULT_POSTRUN_MAX_COST_USD)),
    )

    duration: float = 0.0
    if workflow_run.started_at and workflow_run.modified_at:
        try:
            duration = (workflow_run.modified_at - workflow_run.started_at).total_seconds()
        except Exception:
            duration = 0.0

    last_block_label: str | None = None
    try:
        blocks = await app.DATABASE.observer.get_workflow_run_blocks(
            workflow_run_id=workflow_run.workflow_run_id,
            organization_id=organization_id,
        )
        if blocks:
            last_block_label = getattr(blocks[0], "label", None)
    except Exception:
        LOG.debug("Failed to load workflow_run_blocks for last-label hint", exc_info=True)

    status_value = (
        getattr(workflow_run.status, "value", str(workflow_run.status)) if workflow_run.status else "completed"
    )

    return PostRunContext(
        organization_id=organization_id,
        workflow_permanent_id=workflow_permanent_id,
        workflow_run_id=workflow_run.workflow_run_id,
        script_revision_id=script_revision_id,
        workflow_outcome=status_value,  # type: ignore[arg-type]
        workflow_error_message=getattr(workflow_run, "failure_reason", None),
        workflow_duration_seconds=duration,
        last_block_label=last_block_label,
        episodes=episodes,
        mid_run_class_a_episode_ids=mid_run_class_a_ids,
        budget=budget,
    )


def _summarize_post_run_context(prc: PostRunContext) -> dict:
    return {
        "workflow_permanent_id": prc.workflow_permanent_id,
        "workflow_run_id": prc.workflow_run_id,
        "script_revision_id": prc.script_revision_id,
        "workflow_outcome": prc.workflow_outcome,
        "workflow_error_message": prc.workflow_error_message,
        "workflow_duration_seconds": prc.workflow_duration_seconds,
        "episode_count": len(prc.episodes),
        "mid_run_class_a_count": len(prc.mid_run_class_a_episode_ids),
    }


async def _persist_episode_decision(
    *,
    organization_id: str,
    decision: Decision,
) -> PerEpisodePersistResult:
    """Map a per-episode terminal Decision to the appropriate repo call.

    Returns a structured outcome so the caller can audit which episodes were
    actually written. ``decision.episode_id`` is required — callers must
    filter decisions without it before invoking this function.
    """
    if decision.episode_id is None:
        raise ValueError("persist requires episode_id")
    episode_id = decision.episode_id
    try:
        if decision.type == "declare_review_complete":
            await app.DATABASE.scripts.mark_episode_reviewed(
                episode_id=episode_id,
                organization_id=organization_id,
                reviewer_output=decision.investigation_summary,
                new_script_revision_id=decision.new_script_revision_id,
                reviewer_version="v3",
            )
        elif decision.type == "give_up_episode":
            await app.DATABASE.scripts.update_fallback_episode(
                episode_id=episode_id,
                organization_id=organization_id,
                reviewer_output=f"v3_postrun_give_up: {decision.reason}",
                reviewer_version="v3",
            )
        elif decision.type == "demote_class_a":
            # Tag-only: does NOT flip reviewed (invariant preserved).
            await app.DATABASE.scripts.update_fallback_episode(
                episode_id=episode_id,
                organization_id=organization_id,
                reviewer_output=f"v3_postrun_demote_class_a: {decision.reason}",
                reviewer_version="v3-post-run-demoted",
            )
        else:
            return PerEpisodePersistResult(
                episode_id=episode_id,
                decision_type=decision.type,  # type: ignore[arg-type]
                persisted=False,
                error=f"unknown_decision_type:{decision.type}",
            )
        LOG.info(
            "v3_postrun_per_episode_decision_persisted",
            decision_type=decision.type,
            episode_id=episode_id,
            reason=decision.reason,
            new_script_revision_id=decision.new_script_revision_id,
        )
        return PerEpisodePersistResult(
            episode_id=episode_id,
            decision_type=decision.type,  # type: ignore[arg-type]
            persisted=True,
        )
    except Exception as e:
        LOG.warning(
            "Failed to persist v3 post-run per-episode decision",
            decision_type=decision.type,
            episode_id=episode_id,
            exc_info=True,
        )
        return PerEpisodePersistResult(
            episode_id=episode_id,
            decision_type=decision.type,  # type: ignore[arg-type]
            persisted=False,
            error=str(e),
        )


async def v3_review_post_run(
    *,
    organization_id: str,
    workflow_run: Any,
    workflow_permanent_id: str,
    script_revision_id: str,
    prompt_name: str = "script_reviewer_v3_postrun",
    wall_clock_seconds: float = _DEFAULT_POSTRUN_WALL_SECONDS,
) -> V3PostRunResult:
    """Run the v3 post-run agent on a finished workflow run.

    Per-episode terminals emitted via tool calls during the loop are
    accumulated in ``per_episode_decisions``. The global terminal becomes
    ``result.decision``. Per-episode terminals are persisted to the DB inside
    this function so the post-run reviewer can be called fire-and-forget.
    """
    prc = await build_post_run_context(
        organization_id=organization_id,
        workflow_run=workflow_run,
        workflow_permanent_id=workflow_permanent_id,
        script_revision_id=script_revision_id,
    )

    per_episode_decisions: dict[str, Decision] = {}

    def _terminal_builder(tool_name: str, args: dict, skill_result: Any) -> tuple[Decision | None, bool]:
        """Route per-episode terminals into the accumulator dict and keep the
        loop running until the LLM emits a global terminal.

        Returns ``(decision, should_stop)``. For per-episode terminals we
        record the decision but return ``should_stop=False`` so the agent
        can continue investigating remaining episodes. Global terminals
        fall through (``(None, True)``) to the default schema builder.

        persists are NOT fired here.
        Decisions accumulate; a single deterministic pass after the loop
        terminates applies them and captures structured outcomes.
        """
        if tool_name in {"declare_review_complete", "give_up_episode", "demote_class_a"}:
            data = skill_result.data if isinstance(skill_result.data, dict) else {}
            ep_id = args.get("episode_id") or data.get("episode_id")
            if ep_id:
                if tool_name == "declare_review_complete":
                    d = Decision.declare_review_complete(
                        episode_id=str(ep_id),
                        investigation_summary=args.get("investigation_summary"),
                        new_script_revision_id=data.get("new_script_revision_id"),
                    )
                elif tool_name == "give_up_episode":
                    d = Decision.give_up_episode(str(ep_id), str(args.get("reason") or "unspecified"))
                else:
                    d = Decision.demote_class_a(str(ep_id), str(args.get("reason") or "unspecified"))
                # Last-write-wins is intentional: if the agent re-emits a
                # terminal for the same episode (e.g., switches from
                # demote_class_a to give_up_episode after more investigation),
                # the latest decision wins. The agent loop's anti-oscillation
                # guards block trivial repeats.
                per_episode_decisions[str(ep_id)] = d
                return d, False  # record decision, don't stop the loop
            return None, False  # per-episode terminal without episode_id — skip, keep going
        # Global terminals fall through to the default builder.
        return None, True

    async def _apply_per_episode_persists() -> dict[str, PerEpisodePersistResult]:
        """Single deterministic pass over collected per-episode decisions.

        Runs AFTER the agent loop terminates so persists never race the loop
        or each other, and every outcome is captured for logging/auditing.
        Persists are issued sequentially — the volume per run is bounded by
        the post-run cycle cap (typically 30), so there is no throughput
        concern, and serial application keeps DB write contention minimal.
        """
        outcomes: dict[str, PerEpisodePersistResult] = {}
        for ep_id, decision in per_episode_decisions.items():
            outcomes[ep_id] = await _persist_episode_decision(
                organization_id=organization_id,
                decision=decision,
            )
        return outcomes

    registry = build_registry()
    llm_caller = LLMCaller(llm_key=V3_REVIEWER_MODEL)

    user_prompt = build_postrun_user_prompt(prc_summary=_summarize_post_run_context(prc))

    LOG.info(
        "v3_postrun_started",
        workflow_run_id=workflow_run.workflow_run_id,
        workflow_permanent_id=workflow_permanent_id,
        script_revision_id=script_revision_id,
        episode_count=len(prc.episodes),
        mid_run_class_a_count=len(prc.mid_run_class_a_episode_ids),
        budget_max_cycles=prc.budget.max_cycles,
        budget_max_cost_usd=prc.budget.max_cost_usd,
    )

    try:
        result = await asyncio.wait_for(
            run_agent_loop(
                llm_caller=llm_caller,
                system_prompt=POSTRUN_SYSTEM_PROMPT,
                user_prompt=user_prompt,
                registry=registry,
                agent_kind="postrun",
                context=prc,
                budget=prc.budget,
                organization_id=organization_id,
                prompt_name=prompt_name,
                terminal_builder=_terminal_builder,
            ),
            timeout=wall_clock_seconds,
        )
    except asyncio.TimeoutError:
        LOG.warning(
            "v3_postrun_wall_clock_exceeded",
            workflow_run_id=workflow_run.workflow_run_id,
            wall_clock_seconds=wall_clock_seconds,
            cycles_completed=prc.budget.cycles_used,
            per_episode_decisions=len(per_episode_decisions),
        )
        # Apply per-episode persists collected before the wall-clock fired.
        # The loop's accumulated decisions are still actionable — the
        # timeout only means we don't get a global terminal.
        persist_results = await _apply_per_episode_persists()
        return V3PostRunResult(
            decision=Decision.loop_error("wall_clock_exceeded"),
            per_episode_decisions=per_episode_decisions,
            budget_used=prc.budget,
            timeline=[],
            per_episode_persist_results=persist_results,
        )

    # Single deterministic pass to apply per-episode persists. Persists run
    # AFTER the loop terminates so they never race the loop or each other,
    # and each outcome is captured in persist_results for caller auditing.
    persist_results = await _apply_per_episode_persists()
    persisted_count = sum(1 for r in persist_results.values() if r.persisted)
    failed_count = len(persist_results) - persisted_count

    LOG.info(
        "v3_postrun_complete",
        workflow_run_id=workflow_run.workflow_run_id,
        workflow_permanent_id=workflow_permanent_id,
        decision_type=result.terminal_decision.type if result.terminal_decision else "no_terminal",
        decision_reason=result.terminal_decision.reason if result.terminal_decision else None,
        investigation_summary=(result.terminal_decision.investigation_summary or "")[:300]
        if result.terminal_decision
        else None,
        per_episode_decisions=len(per_episode_decisions),
        per_episode_decision_types={
            d.type: sum(1 for x in per_episode_decisions.values() if x.type == d.type)
            for d in per_episode_decisions.values()
        }
        if per_episode_decisions
        else {},
        per_episode_persisted=persisted_count,
        per_episode_persist_failed=failed_count,
        cycles_used=result.budget.cycles_used,
        tokens_used=result.budget.tokens_used,
        cost_usd_used=round(result.budget.cost_usd_used, 6),
        elapsed_seconds=round(result.budget.elapsed_seconds, 2),
    )

    return V3PostRunResult(
        decision=result.terminal_decision or Decision.loop_error("no_global_terminal"),
        per_episode_decisions=per_episode_decisions,
        budget_used=result.budget,
        timeline=result.timeline,
        per_episode_persist_results=persist_results,
    )


__all__ = ["build_post_run_context", "v3_review_post_run"]
