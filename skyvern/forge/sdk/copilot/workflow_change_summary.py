"""Typed signal: did the user change the workflow YAML between copilot turns?"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from skyvern.utils.yaml_loader import safe_load_no_dates

_MAX_LIST_ITEMS = 8
_MAX_LABEL_LEN = 60


class WorkflowChangeKind(str, Enum):
    FIRST_TURN_NO_PRIOR_STATE = "first_turn_no_prior_state"
    UNCHANGED_SINCE_LAST_TURN = "unchanged_since_last_turn"
    USER_MODIFIED_SINCE_LAST_TURN = "user_modified_since_last_turn"


@dataclass(frozen=True)
class WorkflowChangeSummary:
    kind: WorkflowChangeKind
    added_block_labels: tuple[str, ...] = field(default_factory=tuple)
    removed_block_labels: tuple[str, ...] = field(default_factory=tuple)
    modified_block_labels: tuple[str, ...] = field(default_factory=tuple)
    added_parameter_keys: tuple[str, ...] = field(default_factory=tuple)
    removed_parameter_keys: tuple[str, ...] = field(default_factory=tuple)
    modified_parameter_keys: tuple[str, ...] = field(default_factory=tuple)
    other_top_level_changes: tuple[str, ...] = field(default_factory=tuple)
    structural_diff_unavailable: bool = False

    def render_prompt_block(self) -> str:
        if self.kind is WorkflowChangeKind.FIRST_TURN_NO_PRIOR_STATE:
            return (
                "first_turn_no_prior_state: no prior copilot workflow baseline is available"
                " (first turn of this chat, or the previous turn ended without a workflow proposal)."
            )
        if self.kind is WorkflowChangeKind.UNCHANGED_SINCE_LAST_TURN:
            return (
                "unchanged_since_last_turn: the current workflow YAML matches what the copilot"
                " last persisted at the end of the previous turn."
            )

        sections: list[str] = ["user_modified_since_last_turn: the user changed the workflow YAML between turns."]
        if self.added_block_labels:
            sections.append(f"added blocks: {_format_list(self.added_block_labels)}")
        if self.removed_block_labels:
            sections.append(f"removed blocks: {_format_list(self.removed_block_labels)}")
        if self.modified_block_labels:
            sections.append(f"modified blocks: {_format_list(self.modified_block_labels)}")
        if self.added_parameter_keys:
            sections.append(f"added parameters: {_format_list(self.added_parameter_keys)}")
        if self.removed_parameter_keys:
            sections.append(f"removed parameters: {_format_list(self.removed_parameter_keys)}")
        if self.modified_parameter_keys:
            sections.append(f"modified parameters: {_format_list(self.modified_parameter_keys)}")
        if self.other_top_level_changes:
            sections.append(f"other changes: {_format_list(self.other_top_level_changes)}")
        if len(sections) == 1:
            if self.structural_diff_unavailable:
                sections.append("structural diff unavailable — YAML parse failed on one or both sides.")
            else:
                sections.append("no structural diff — block bodies or settings were changed in place.")
        return "\n".join(sections)


def summarize_user_workflow_change(
    prior_yaml: str | None,
    current_yaml: str | None,
) -> WorkflowChangeSummary:
    if not _has_content(prior_yaml):
        return WorkflowChangeSummary(kind=WorkflowChangeKind.FIRST_TURN_NO_PRIOR_STATE)
    if (current_yaml or "").strip() == (prior_yaml or "").strip():
        return WorkflowChangeSummary(kind=WorkflowChangeKind.UNCHANGED_SINCE_LAST_TURN)

    prior = _parse(prior_yaml)
    current = _parse(current_yaml)
    if prior is None or current is None:
        return WorkflowChangeSummary(
            kind=WorkflowChangeKind.USER_MODIFIED_SINCE_LAST_TURN,
            structural_diff_unavailable=True,
        )

    prior_blocks, prior_duplicates = _index_blocks(prior)
    current_blocks, current_duplicates = _index_blocks(current)
    if prior_duplicates or current_duplicates:
        return WorkflowChangeSummary(
            kind=WorkflowChangeKind.USER_MODIFIED_SINCE_LAST_TURN,
            structural_diff_unavailable=True,
        )
    prior_params = _index_parameters(prior)
    current_params = _index_parameters(current)

    added = tuple(label for label in current_blocks if label not in prior_blocks)
    removed = tuple(label for label in prior_blocks if label not in current_blocks)
    modified = tuple(
        label for label, body in current_blocks.items() if label in prior_blocks and prior_blocks[label] != body
    )
    added_params = tuple(key for key in current_params if key not in prior_params)
    removed_params = tuple(key for key in prior_params if key not in current_params)
    modified_params = tuple(
        key for key, body in current_params.items() if key in prior_params and prior_params[key] != body
    )
    other = _top_level_change_keys(prior, current)

    return WorkflowChangeSummary(
        kind=WorkflowChangeKind.USER_MODIFIED_SINCE_LAST_TURN,
        added_block_labels=added,
        removed_block_labels=removed,
        modified_block_labels=modified,
        added_parameter_keys=added_params,
        removed_parameter_keys=removed_params,
        modified_parameter_keys=modified_params,
        other_top_level_changes=other,
    )


def _has_content(value: str | None) -> bool:
    return bool(value and value.strip())


def _parse(yaml_text: str | None) -> dict[str, Any] | None:
    if not _has_content(yaml_text):
        return None
    try:
        parsed = safe_load_no_dates(yaml_text)
    except Exception:
        return None
    return parsed if isinstance(parsed, dict) else None


def _index_blocks(parsed: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Return ``(label -> block-body, duplicates_seen)``.

    ``duplicates_seen`` is True when two blocks at the same scope share a
    label — schema permits this and dedup-by-overwrite would silently mask
    add/remove edits, so the caller short-circuits to
    ``structural_diff_unavailable`` instead of returning a misleading diff."""
    definition = parsed.get("workflow_definition")
    if not isinstance(definition, dict):
        return {}, False
    blocks = definition.get("blocks")
    if not isinstance(blocks, list):
        return {}, False
    indexed: dict[str, Any] = {}
    duplicates = _collect_blocks_recursive(blocks, indexed, parent_label_prefix="")
    return indexed, duplicates


def _collect_blocks_recursive(
    blocks: list[Any],
    indexed: dict[str, Any],
    *,
    parent_label_prefix: str,
) -> bool:
    seen_at_this_scope: set[str] = set()
    duplicates_seen = False
    for position, block in enumerate(blocks):
        if not isinstance(block, dict):
            continue
        raw_label = block.get("label")
        if isinstance(raw_label, str) and raw_label:
            local_label = raw_label
        else:
            local_label = f"<unlabeled_{position}>"
        if local_label in seen_at_this_scope:
            duplicates_seen = True
            continue
        seen_at_this_scope.add(local_label)
        qualified = f"{parent_label_prefix}{local_label}" if parent_label_prefix else local_label
        indexed[qualified] = block
        nested = block.get("loop_blocks")
        if isinstance(nested, list) and nested:
            duplicates_seen = (
                _collect_blocks_recursive(nested, indexed, parent_label_prefix=f"{qualified}/") or duplicates_seen
            )
    return duplicates_seen


def _index_parameters(parsed: dict[str, Any]) -> dict[str, Any]:
    definition = parsed.get("workflow_definition")
    if not isinstance(definition, dict):
        return {}
    parameters = definition.get("parameters")
    if not isinstance(parameters, list):
        return {}
    indexed: dict[str, Any] = {}
    fallback = 0
    for parameter in parameters:
        if not isinstance(parameter, dict):
            continue
        key = parameter.get("key")
        if not isinstance(key, str) or not key:
            key = f"<unkeyed_{fallback}>"
            fallback += 1
        indexed[key] = parameter
    return indexed


def _top_level_change_keys(prior: dict[str, Any], current: dict[str, Any]) -> tuple[str, ...]:
    keys = {key for key in (set(prior) | set(current)) if key != "workflow_definition"}
    changed = tuple(sorted(key for key in keys if prior.get(key) != current.get(key)))
    return changed


def _format_list(items: tuple[str, ...]) -> str:
    truncated = [_clip(item) for item in items[:_MAX_LIST_ITEMS]]
    if len(items) > _MAX_LIST_ITEMS:
        truncated.append(f"…(+{len(items) - _MAX_LIST_ITEMS} more)")
    return ", ".join(truncated)


def _clip(value: str) -> str:
    if len(value) <= _MAX_LABEL_LEN:
        return value
    return value[: _MAX_LABEL_LEN - 1] + "…"
