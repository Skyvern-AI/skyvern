"""Mint-time agent review entry point.

Public API: :func:`v3_review_at_mint`. Called from
``WorkflowScriptService._log_mint_audit_findings`` (fire-and-forget) when
the static audit returns 1+ ``SuspiciousLiteralFinding`` entries.

Lifecycle:

1. Build a :class:`MintAuditContext` from the findings + workflow info.
2. Run the agent loop with the postrun skill set (validators, code-read
   skills, persist_block_edit, and the global terminals). Per-episode
   terminals don't apply here — there are no episodes.
3. Return a :class:`V3MintAuditResult`. The caller logs but otherwise
   ignores the result (this is a fire-and-forget background task).
"""

from __future__ import annotations

import asyncio
from typing import Any

import structlog

from skyvern.forge.sdk.api.llm.api_handler_factory import LLMCaller
from skyvern.services.script_reviewer_v3.agent_loop import run_agent_loop
from skyvern.services.script_reviewer_v3.budget import Budget
from skyvern.services.script_reviewer_v3.decision import Decision
from skyvern.services.script_reviewer_v3.llm_adapter import V3_REVIEWER_MODEL
from skyvern.services.script_reviewer_v3.prompts import (
    MINT_AUDIT_SYSTEM_PROMPT,
    build_mint_audit_user_prompt,
)
from skyvern.services.script_reviewer_v3.registry_builder import build_registry
from skyvern.services.script_reviewer_v3.types import MintAuditContext, V3MintAuditResult

LOG = structlog.get_logger()


# Tiny budget compared to postrun — this is a focused single-script audit,
# not a sweeping episode-driven review.
_MINT_AUDIT_MAX_CYCLES = 10
_MINT_AUDIT_MAX_TOKENS = 100_000
_MINT_AUDIT_MAX_COST_USD = 0.30
_MINT_AUDIT_WALL_SECONDS = 120.0


async def v3_review_at_mint(
    *,
    organization_id: str,
    workflow_permanent_id: str,
    workflow_run_id: str,
    script_revision_id: str,
    user_prompt: str,
    findings: list[dict],
    wall_clock_seconds: float = _MINT_AUDIT_WALL_SECONDS,
) -> V3MintAuditResult:
    """Run the v3 mint-time agent on a fresh script with static findings.

    Designed to be fire-and-forget from the caller's perspective. Errors
    are caught and surfaced via the ``decision`` field (no exceptions
    escape the wall-clock guard); the caller should still wrap this in a
    try/except for any pre-call setup failures.
    """
    if not findings:
        # Defensive — caller should already have short-circuited.
        return V3MintAuditResult(
            decision=Decision.declare_post_run_complete("no findings to review"),
            budget_used=Budget(
                max_cycles=_MINT_AUDIT_MAX_CYCLES,
                max_tokens=_MINT_AUDIT_MAX_TOKENS,
                max_cost_usd=_MINT_AUDIT_MAX_COST_USD,
            ),
            timeline=[],
        )

    budget = Budget(
        max_cycles=_MINT_AUDIT_MAX_CYCLES,
        max_tokens=_MINT_AUDIT_MAX_TOKENS,
        max_cost_usd=_MINT_AUDIT_MAX_COST_USD,
    )

    context = MintAuditContext(
        organization_id=organization_id,
        workflow_permanent_id=workflow_permanent_id,
        workflow_run_id=workflow_run_id,
        script_revision_id=script_revision_id,
        user_prompt=user_prompt,
        findings=findings,
        budget=budget,
    )

    registry = build_registry()
    llm_caller = LLMCaller(llm_key=V3_REVIEWER_MODEL)

    user_prompt_for_llm = build_mint_audit_user_prompt(
        findings=findings,
        user_prompt_text=user_prompt,
    )

    LOG.info(
        "v3_mint_audit_started",
        workflow_run_id=workflow_run_id,
        workflow_permanent_id=workflow_permanent_id,
        script_revision_id=script_revision_id,
        finding_count=len(findings),
        budget_max_cycles=budget.max_cycles,
        budget_max_cost_usd=budget.max_cost_usd,
    )

    try:
        result = await asyncio.wait_for(
            run_agent_loop(
                llm_caller=llm_caller,
                system_prompt=MINT_AUDIT_SYSTEM_PROMPT,
                user_prompt=user_prompt_for_llm,
                registry=registry,
                # Reuse postrun's terminal set — declare_post_run_complete
                # and abandon_post_run are the right terminals here, and
                # the per-episode terminals (give_up_episode etc.) won't be
                # picked because there are no episode_ids to attach them to.
                agent_kind="postrun",
                context=context,
                budget=budget,
                organization_id=organization_id,
                prompt_name="script_reviewer_v3_mint_audit",
            ),
            timeout=wall_clock_seconds,
        )
    except asyncio.TimeoutError:
        LOG.warning(
            "v3_mint_audit_wall_clock_exceeded",
            workflow_run_id=workflow_run_id,
            script_revision_id=script_revision_id,
            wall_clock_seconds=wall_clock_seconds,
            cycles_completed=budget.cycles_used,
        )
        return V3MintAuditResult(
            decision=Decision.loop_error("wall_clock_exceeded"),
            budget_used=budget,
            timeline=[],
        )
    except Exception as exc:
        LOG.warning(
            "v3_mint_audit_unhandled_error",
            workflow_run_id=workflow_run_id,
            script_revision_id=script_revision_id,
            error=str(exc),
            exc_info=True,
        )
        return V3MintAuditResult(
            decision=Decision.loop_error(f"unhandled: {exc}"),
            budget_used=budget,
            timeline=[],
        )

    # Walk the timeline for new script revision IDs the agent persisted.
    new_revisions: list[str] = []
    for event in result.timeline:
        rev = event.get("new_script_revision_id")
        if rev and rev not in new_revisions:
            new_revisions.append(rev)

    LOG.info(
        "v3_mint_audit_complete",
        workflow_run_id=workflow_run_id,
        script_revision_id=script_revision_id,
        decision_type=result.terminal_decision.type if result.terminal_decision else "no_terminal",
        decision_reason=result.terminal_decision.reason if result.terminal_decision else None,
        new_script_revisions=new_revisions,
        cycles_used=result.budget.cycles_used,
        tokens_used=result.budget.tokens_used,
        cost_usd_used=round(result.budget.cost_usd_used, 6),
        elapsed_seconds=round(result.budget.elapsed_seconds, 2),
    )

    return V3MintAuditResult(
        decision=result.terminal_decision or Decision.loop_error("no_global_terminal"),
        budget_used=result.budget,
        timeline=result.timeline,
        new_script_revision_ids=new_revisions,
    )


# Strong-reference set to prevent asyncio from GC'ing fire-and-forget mint
# reviews before they complete. Python's asyncio docs explicitly warn that
# tasks without a strong reference can be garbage-collected mid-execution.
_PENDING_MINT_REVIEWS: set[asyncio.Task[Any]] = set()


def fire_and_forget_mint_review(
    *,
    organization_id: str,
    workflow_permanent_id: str,
    workflow_run_id: str,
    script_revision_id: str,
    user_prompt: str,
    findings: list[dict],
) -> asyncio.Task[Any] | None:
    """Schedule a mint-audit review on the running event loop.

    Returns the created task (for tests / observability) or ``None`` if no
    loop is running (callers running outside an event loop, e.g. CLI
    scripts, should call ``v3_review_at_mint`` directly with their own
    ``asyncio.run``).

    The returned task is added to ``_PENDING_MINT_REVIEWS`` for the
    duration of its execution; a done callback removes it. This is the
    canonical asyncio fire-and-forget idiom — without it, the task can
    be garbage-collected mid-run.

    Logs a structured ``v3_mint_audit_task_failed`` line if the task
    raises — fire-and-forget without proper error logging is the bug we
    fixed in commit 42cc52e6d on the post-run persist path; same rule
    applies here.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        LOG.warning(
            "v3_mint_audit_no_event_loop",
            workflow_run_id=workflow_run_id,
            script_revision_id=script_revision_id,
        )
        return None

    async def _runner() -> V3MintAuditResult:
        try:
            return await v3_review_at_mint(
                organization_id=organization_id,
                workflow_permanent_id=workflow_permanent_id,
                workflow_run_id=workflow_run_id,
                script_revision_id=script_revision_id,
                user_prompt=user_prompt,
                findings=findings,
            )
        except Exception:
            LOG.warning(
                "v3_mint_audit_task_failed",
                workflow_run_id=workflow_run_id,
                script_revision_id=script_revision_id,
                exc_info=True,
            )
            raise

    task = loop.create_task(_runner())
    _PENDING_MINT_REVIEWS.add(task)
    task.add_done_callback(_PENDING_MINT_REVIEWS.discard)
    return task


__all__ = ["fire_and_forget_mint_review", "v3_review_at_mint"]
