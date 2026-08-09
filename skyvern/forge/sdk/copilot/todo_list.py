"""Per-iteration TODO list rendered from typed turn state; adds context, blocks nothing."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from collections.abc import Set as AbstractSet
from typing import Any

import structlog

from skyvern.forge.sdk.copilot.request_policy import CompletionCriterion, RequestPolicy
from skyvern.forge.sdk.copilot.runtime import AgentContext
from skyvern.forge.sdk.copilot.turn_intent import (
    REGISTERED_DOWNLOAD_OUTPUT_PATH,
    turn_intent_authorizes_registered_download,
)

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
    unmet = unmet_action_deliverable_criteria_from(minted, _inapplicable_criterion_ids(ctx))
    if unmet or any(
        "registered_download" in (criterion.deliverable_kind, criterion.declared_deliverable_kind)
        and criterion.level == "run"
        for criterion in minted
    ):
        return unmet
    if not turn_intent_authorizes_registered_download(getattr(ctx, "turn_intent", None)):
        return []
    if _registered_download_delivered(ctx):
        return []
    return [
        CompletionCriterion(
            id="__copilot_turn_intent_registered_download__",
            outcome="the requested file is registered as a browser download",
            deliverable_kind="registered_download",
            declared_deliverable_kind="registered_download",
            output_path=REGISTERED_DOWNLOAD_OUTPUT_PATH,
        )
    ]


def _registered_download_delivered(ctx: AgentContext) -> bool:
    # Offline replay probes deliberately use a lightweight context that omits optional
    # runtime-evidence carriers until they are observed.
    reached = getattr(ctx, "reached_download_target", None)
    if reached is not None and getattr(reached, "already_registered", False):
        return True
    output_maps = (
        getattr(ctx, "verified_terminal_block_outputs", None),
        getattr(ctx, "verified_block_outputs", None),
    )
    count_keys = ("downloaded_file_count", "downloaded_file_url_count", "downloaded_file_artifact_count")
    for outputs in output_maps:
        if not isinstance(outputs, Mapping):
            continue
        for payload in outputs.values():
            if not isinstance(payload, Mapping) or payload.get("download_registered") is not True:
                continue
            if any(
                isinstance(payload.get(key), int) and not isinstance(payload.get(key), bool) and payload.get(key, 0) > 0
                for key in count_keys
            ):
                return True
    return False


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
