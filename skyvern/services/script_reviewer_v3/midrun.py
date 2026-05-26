"""Mid-run agent entry point.

Public API: :func:`v3_review_in_flight`. Invoked from the cohort-aware hook
inside :class:`SkyvernPage`-family AI methods (``ai_click``, ``ai_input_text``)
when the cached selector path fails AND PostHog routes the wpid to v3.

Lifecycle:

1. Build :class:`FailureContext` (caller does most of this — we receive the
   already-populated dataclass).
2. Acquire a run-budget invocation handle (returns None → no slot → caller
   falls through to agent fallback).
3. Run the agent loop with mid-run skills.
4. Process the terminal:
   - Class A (declare_success): atomic mark_episode_reviewed with the new
     script_revision_id (if persisted) so the run captures the in-flight fix.
   - Class B (give_up / budget_exhausted / loop_error): update episode with
     reviewer_output but leave reviewed=False; caller falls through to agent
     fallback and the post-run agent picks up the episode later.
5. Reconcile run-budget cost.
"""

from __future__ import annotations

import asyncio

import structlog

from skyvern.forge import app
from skyvern.forge.sdk.api.llm.api_handler_factory import LLMCaller
from skyvern.services.script_reviewer_v3.agent_loop import run_agent_loop
from skyvern.services.script_reviewer_v3.budget import (
    DEFAULT_MIDRUN_MAX_COST_USD,
    DEFAULT_MIDRUN_MAX_CYCLES,
    DEFAULT_MIDRUN_MAX_TOKENS,
    DEFAULT_MIDRUN_MAX_WALL_SECONDS,
    Budget,
)
from skyvern.services.script_reviewer_v3.cohort import resolve_v3_budget_payload
from skyvern.services.script_reviewer_v3.decision import Decision
from skyvern.services.script_reviewer_v3.llm_adapter import V3_REVIEWER_MODEL
from skyvern.services.script_reviewer_v3.prompts import (
    MIDRUN_SYSTEM_PROMPT,
    build_midrun_user_prompt,
)
from skyvern.services.script_reviewer_v3.registry_builder import build_registry
from skyvern.services.script_reviewer_v3.types import FailureContext, V3MidRunResult

LOG = structlog.get_logger()


_DEFAULT_MIDRUN_WALL_SECONDS = DEFAULT_MIDRUN_MAX_WALL_SECONDS


def _summarize_failure_context(fc: FailureContext) -> dict:
    return {
        "action_type": fc.action_type,
        "failed_selector": fc.failed_selector,
        "intention": fc.intention,
        "value": fc.value,
        "totp_identifier": fc.totp_identifier,
        "page_url": getattr(fc.page, "url", None) if fc.page is not None else None,
    }


async def _persist_midrun_terminal(
    *,
    organization_id: str,
    fc: FailureContext,
    decision: Decision,
) -> None:
    """Persist the mid-run terminal decision against the episode row.

    Class A: mark_episode_reviewed atomically with the new script revision id.
    Class B: update with reviewer_output but leave reviewed=False.
    """
    try:
        if decision.is_midrun_class_a():
            await app.DATABASE.scripts.mark_episode_reviewed(
                episode_id=fc.episode_id,
                organization_id=organization_id,
                reviewer_output=decision.investigation_summary or decision.reason,
                new_script_revision_id=decision.new_script_revision_id,
                reviewer_version="v3",
            )
        else:
            await app.DATABASE.scripts.update_fallback_episode(
                episode_id=fc.episode_id,
                organization_id=organization_id,
                reviewer_output=f"v3_midrun_{decision.type}: {decision.reason}",
                reviewer_version="v3",
            )
    except Exception:
        LOG.warning(
            "Failed to persist v3 mid-run terminal decision",
            decision_type=decision.type,
            episode_id=fc.episode_id,
            exc_info=True,
        )


async def v3_review_in_flight(
    fc: FailureContext,
    *,
    prompt_name: str = "script_reviewer_v3_midrun",
    wall_clock_seconds: float = _DEFAULT_MIDRUN_WALL_SECONDS,
) -> V3MidRunResult:
    """Run the v3 mid-run agent on an in-flight cached selector failure.

    Returns the result; the calling hook inspects the decision type to decide
    whether to return early (Class A) or fall through to agent fallback (Class
    B). The hook is responsible for invoking the agent fallback in the latter
    case — this function only touches the v3 lifecycle (LLM + episode).
    """
    org_id = getattr(fc.context, "organization_id", None)
    if not org_id:
        LOG.warning("FailureContext.context.organization_id missing — falling through")
        return V3MidRunResult(
            decision=Decision.loop_error("missing_organization_id"),
            budget_used=None,
            timeline=[],
        )

    wpid = getattr(fc.context, "workflow_permanent_id", None)
    payload = await resolve_v3_budget_payload(workflow_permanent_id=str(wpid)) if wpid else {}
    budget = Budget(
        max_cycles=int(payload.get("midrun_max_cycles", DEFAULT_MIDRUN_MAX_CYCLES)),
        max_tokens=int(payload.get("midrun_max_tokens", DEFAULT_MIDRUN_MAX_TOKENS)),
        max_cost_usd=float(payload.get("midrun_max_cost_usd", DEFAULT_MIDRUN_MAX_COST_USD)),
    )

    invocation_handle = None
    run_budget = getattr(fc.context, "v3_run_budget", None)
    if run_budget is not None:
        invocation_handle = await run_budget.try_acquire_invocation()
        if invocation_handle is None:
            LOG.info(
                "v3 mid-run run-budget cap exceeded; falling through",
                workflow_run_id=getattr(fc.context, "workflow_run_id", None),
            )
            await app.DATABASE.scripts.update_fallback_episode(
                episode_id=fc.episode_id,
                organization_id=org_id,
                reviewer_output="v3_midrun_run_budget_exhausted",
                reviewer_version="v3",
            )
            return V3MidRunResult(
                decision=Decision.budget_exhausted("run_budget_exhausted"),
                budget_used=None,
                timeline=[],
            )

    # If setup fails after this point, the reserved invocation is finalized
    # at $0.00 but the invocation slot remains consumed to prevent retry storms.
    try:
        registry = build_registry()
        llm_caller = LLMCaller(llm_key=V3_REVIEWER_MODEL)

        user_prompt = build_midrun_user_prompt(
            episode_id=fc.episode_id,
            fc_summary=_summarize_failure_context(fc),
        )

        LOG.info(
            "v3_midrun_started",
            episode_id=fc.episode_id,
            workflow_run_id=getattr(fc.context, "workflow_run_id", None),
            action_type=fc.action_type,
            failed_selector=fc.failed_selector,
            budget_max_cycles=budget.max_cycles,
            budget_max_cost_usd=budget.max_cost_usd,
        )

        try:
            result = await asyncio.wait_for(
                run_agent_loop(
                    llm_caller=llm_caller,
                    system_prompt=MIDRUN_SYSTEM_PROMPT,
                    user_prompt=user_prompt,
                    registry=registry,
                    agent_kind="midrun",
                    context=fc,
                    budget=budget,
                    organization_id=org_id,
                    prompt_name=prompt_name,
                ),
                timeout=wall_clock_seconds,
            )
        except asyncio.TimeoutError:
            LOG.warning(
                "v3_midrun_wall_clock_exceeded",
                episode_id=fc.episode_id,
                wall_clock_seconds=wall_clock_seconds,
                cycles_completed=budget.cycles_used,
            )
            decision = Decision.loop_error("wall_clock_exceeded")
            await _persist_midrun_terminal(organization_id=org_id, fc=fc, decision=decision)
            return V3MidRunResult(decision=decision, budget_used=budget, timeline=[])

        final_decision = result.terminal_decision or Decision.loop_error("no_terminal")
        LOG.info(
            "v3_midrun_complete",
            episode_id=fc.episode_id,
            workflow_run_id=getattr(fc.context, "workflow_run_id", None),
            decision_type=final_decision.type,
            decision_reason=final_decision.reason,
            investigation_summary=(final_decision.investigation_summary or "")[:300],
            new_script_revision_id=final_decision.new_script_revision_id,
            class_a=final_decision.is_midrun_class_a(),
            class_b=final_decision.is_midrun_class_b(),
            cycles_used=result.budget.cycles_used,
            tokens_used=result.budget.tokens_used,
            cost_usd_used=round(result.budget.cost_usd_used, 6),
            elapsed_seconds=round(result.budget.elapsed_seconds, 2),
        )

        await _persist_midrun_terminal(
            organization_id=org_id,
            fc=fc,
            decision=final_decision,
        )

        return V3MidRunResult(
            decision=final_decision,
            budget_used=result.budget,
            timeline=result.timeline,
        )
    finally:
        if invocation_handle is not None:
            try:
                await invocation_handle.finalize_cost(budget.cost_usd_used)
            except Exception:  # pragma: no cover
                LOG.warning("v3_midrun_invocation_handle_finalize_failed", exc_info=True)


__all__ = ["v3_review_in_flight"]
