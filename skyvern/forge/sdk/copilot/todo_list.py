"""Per-iteration TODO list rendered from typed turn state; adds context, blocks nothing."""

from __future__ import annotations

from collections.abc import Sequence
from collections.abc import Set as AbstractSet

import structlog

from skyvern.forge.sdk.copilot.request_policy import CompletionCriterion, RequestPolicy
from skyvern.forge.sdk.copilot.runtime import AgentContext

LOG = structlog.get_logger(__name__)


def _page_key(url: object) -> str | None:
    if not isinstance(url, str) or not url.strip():
        return None
    return url.strip().split("#", 1)[0].rstrip("/")


def _interaction_reached_page_keys(ctx: AgentContext) -> set[str]:
    """Pages reached by actually interacting, not just looking."""
    keys: set[str] = set()
    for page in ctx.prior_observed_acted_pages:
        if isinstance(page, dict) and page.get("reached_via") == "interaction":
            key = _page_key(page.get("url"))
            if key:
                keys.add(key)
    for entry in ctx.flow_evidence:
        if entry.get("reached_via") != "interaction":
            continue
        url = entry.get("url")
        evidence = entry.get("evidence")
        if not _page_key(url) and isinstance(evidence, dict):
            url = evidence.get("current_url") or evidence.get("inspected_url")
        key = _page_key(url)
        if key:
            keys.add(key)
    return keys


def _minted_criteria(ctx: AgentContext) -> list[CompletionCriterion]:
    turn_state = ctx.completion_criteria_turn_state
    if turn_state is not None and turn_state.decision is not None:
        return list(turn_state.decision.criteria)
    policy = ctx.request_policy
    if isinstance(policy, RequestPolicy):
        return policy.graded_completion_criteria()
    return []


def _satisfied_output_paths(ctx: AgentContext) -> set[str]:
    result = ctx.completion_verification_result
    if result is None:
        return set()
    paths: set[str] = set()
    for verdict in result.verdicts:
        if not verdict.satisfied:
            continue
        # A definition-plane satisfied verdict proves the workflow is configurable, never that
        # a run produced the output (same discriminator as is_fully_satisfied).
        if verdict.reason_code.startswith("definition_"):
            continue
        for path in (verdict.output_path, result.criterion_output_path_by_id.get(verdict.criterion_id)):
            if path:
                paths.add(path)
    return paths


def _outputs_line(ctx: AgentContext) -> str | None:
    satisfied = _satisfied_output_paths(ctx)
    pending: list[str] = []
    for criterion in _minted_criteria(ctx):
        # Definition-plane criteria are graded against the YAML and only ever get
        # definition_* verdicts, so a run-plane pending check would nag forever.
        if criterion.level == "definition":
            continue
        path = criterion.output_path
        if path and path not in satisfied and path not in pending:
            pending.append(path)
    if not pending:
        return None
    return "Outputs not yet observed: " + ", ".join(pending)


def _interactions_line(ctx: AgentContext) -> str | None:
    if ctx.scout_trajectory or ctx.prior_carried_trajectory or _interaction_reached_page_keys(ctx):
        return None
    return "The site has not been acted on yet (0 interactions recorded)"


def _inapplicable_criterion_ids(ctx: AgentContext) -> set[str]:
    """Criteria that no longer need action: satisfied, or structurally unfired because their
    antecedent condition did not hold (a conditional download on a branch the run did not take)."""
    result = ctx.completion_verification_result
    if result is None:
        return set()
    ids = {verdict.criterion_id for verdict in result.verdicts if verdict.satisfied}
    ids |= set(getattr(result, "structural_unfired_criterion_ids", ()) or ())
    return ids


def unmet_action_deliverable_criteria_from(
    criteria: Sequence[CompletionCriterion], inapplicable_ids: AbstractSet[str]
) -> list[CompletionCriterion]:
    """Run-plane criteria whose deliverable needs an action on the page (today: a registered
    browser download) and which no verification verdict has satisfied yet. Pure over its inputs
    so offline replay runs the same decision the live gate ran."""
    return [
        criterion
        for criterion in criteria
        if "registered_download" in (criterion.deliverable_kind, criterion.declared_deliverable_kind)
        and criterion.level == "run"
        and criterion.id not in inapplicable_ids
    ]


def unmet_action_deliverable_criteria(ctx: AgentContext) -> list[CompletionCriterion]:
    minted = _minted_criteria(ctx)
    return unmet_action_deliverable_criteria_from(minted, _inapplicable_criterion_ids(ctx))


def render_todo_list(ctx: AgentContext) -> str | None:
    outputs = _outputs_line(ctx)
    if not outputs:
        return None
    lines = [outputs]
    interactions = _interactions_line(ctx)
    if interactions:
        lines.append(interactions)
    return "\n".join(f"- {line}" for line in lines)


def todo_list_prompt(ctx: AgentContext) -> str:
    todo = render_todo_list(ctx)
    if not todo:
        return ""
    LOG.debug("copilot_todo_list_rendered", line_count=todo.count("\n") + 1)
    return "\n\nTODO — outstanding before you reply:\n" + todo
