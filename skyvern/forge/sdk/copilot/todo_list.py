"""Per-iteration TODO list rendered from typed turn state; adds context, blocks nothing."""

from __future__ import annotations

from typing import Any

import structlog

from skyvern.forge.sdk.copilot.request_policy import CompletionCriterion, RequestPolicy
from skyvern.forge.sdk.copilot.runtime import AgentContext

LOG = structlog.get_logger(__name__)


def _page_key(url: object) -> str | None:
    if not isinstance(url, str) or not url.strip():
        return None
    return url.strip().split("#", 1)[0].rstrip("/")


def _credential_fill_page_keys(ctx: AgentContext) -> set[str] | None:
    """Pages where a credential fill happened, or None when no fill has happened at all."""
    keys: set[str] = set()
    fills = 0
    entries: list[dict[str, Any]] = [dict(interaction) for interaction in ctx.scout_trajectory]
    entries.extend(entry for entry in ctx.prior_fill_carry if isinstance(entry, dict))
    for entry in entries:
        if entry.get("tool_name") != "fill_credential_field":
            continue
        fills += 1
        key = _page_key(entry.get("source_url"))
        if key:
            keys.add(key)
    return keys if fills else None


def _interaction_reached_page_keys(ctx: AgentContext) -> set[str]:
    """Pages reached by actually interacting, not just looking — the only evidence that counts as login progress.

    Heuristic ceiling: any interaction-reached page off the fill page counts, so a non-submit
    navigation (e.g. a forgot-password link) can suppress the login line without a real login.
    """
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


def _login_line(ctx: AgentContext) -> str | None:
    policy = ctx.request_policy
    if not isinstance(policy, RequestPolicy) or not policy.login_intent or not policy.resolved_credentials:
        return None
    fill_pages = _credential_fill_page_keys(ctx)
    if fill_pages is None:
        return "Login: credential resolved but login not yet attempted"
    if _interaction_reached_page_keys(ctx) - fill_pages:
        return None
    return "Login: credential resolved but login not completed (no page reached by interaction yet)"


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
    if ctx.scout_trajectory or ctx.prior_fill_carry or _interaction_reached_page_keys(ctx):
        return None
    return "The site has not been acted on yet (0 interactions recorded)"


def render_todo_list(ctx: AgentContext) -> str | None:
    lines = [line for line in (_login_line(ctx), _outputs_line(ctx)) if line]
    if not lines:
        return None
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
