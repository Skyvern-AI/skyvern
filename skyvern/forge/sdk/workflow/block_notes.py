"""Notes are run-time instructions read by the steps on either side of them; descriptions are for people only.

Every function here accepts Block models and plain dicts (YAML or ``model_dump`` output), so the
run time, cache invalidation, the Copilot review gate and save-time carry-forward share one rule.
"""

import json
from collections import Counter
from collections.abc import Collection, Iterator, Mapping, Sequence
from itertools import pairwise
from typing import Any

from skyvern.utils.yaml_loader import dump_workflow_yaml, safe_load_no_dates

NOTE_FIELDS = ("notes", "trailing_notes")
CARRIED_FIELDS = (*NOTE_FIELDS, "description")
NOTES_SECTION_HEADER = "Notes for this step:"


def _field(block: Any, key: str) -> Any:
    return block.get(key) if isinstance(block, Mapping) else getattr(block, key, None)


def _is_block(value: Any) -> bool:
    return isinstance(_field(value, "label"), str) and _field(value, "block_type") is not None


def _block_type(block: Any) -> str:
    value = _field(block, "block_type")
    return str(getattr(value, "value", value) or "")


def _child_blocks(block: Any) -> Sequence[Any]:
    children = _field(block, "loop_blocks")
    return children if isinstance(children, list) else []


def _sequences(
    blocks: Sequence[Any], finally_block_label: str | None
) -> tuple[list[list[Any]], dict[str, Any], dict[str, str | None]]:
    """Split one block list into the sequences a reader sees, in definition order.

    Without conditionals a block's next sibling is its ``next_block_label`` or else the next list
    item, as in ``_build_workflow_graph``. With conditionals, each branch is the chain from its head
    to the conditional's own successor, and the conditional's next sibling is that successor.
    """
    # models.block imports this module through WorkflowRunContext; defer until the graph is initialized.
    from skyvern.forge.sdk.workflow.models.block import resolve_conditional_merge_edges, strip_finally_block_references

    candidates = [block for block in blocks if _is_block(block)]
    by_label = {_field(block, "label"): block for block in candidates}
    run_blocks = strip_finally_block_references(candidates, finally_block_label) if finally_block_label else candidates
    run_by_label = {_field(block, "label"): block for block in run_blocks}
    next_by_label = {
        _field(block, "label"): (
            _field(block, "next_block_label") if isinstance(_field(block, "next_block_label"), str) else None
        )
        for block in run_blocks
    }
    if not any(_block_type(block) == "conditional" for block in run_blocks):
        for previous, following in pairwise(run_blocks):
            label = _field(previous, "label")
            if next_by_label[label] is None:
                next_by_label[label] = _field(following, "label")
    resolve_conditional_merge_edges(run_blocks, run_by_label, next_by_label)

    placed: set[str] = set()
    sequences: list[list[Any]] = []
    pending_conditionals: list[Any] = []

    def walk(start: str | None, stop: str | None) -> None:
        sequence: list[Any] = []
        current = start
        while current and current in run_by_label and current not in placed and current != stop:
            placed.add(current)
            block = run_by_label[current]
            sequence.append(block)
            if _block_type(block) == "conditional":
                pending_conditionals.append(block)
            current = next_by_label[current]
        if sequence:
            sequences.append(sequence)

    targets = {target for target in next_by_label.values() if isinstance(target, str)}
    for block in run_blocks:
        branches = _field(block, "branch_conditions")
        if isinstance(branches, list):
            targets.update(
                target
                for target in (_field(branch, "next_block_label") for branch in branches)
                if isinstance(target, str)
            )
    for block in run_blocks:
        label = _field(block, "label")
        if label not in targets:
            walk(label, None)
            break
    while pending_conditionals:
        conditional = pending_conditionals.pop(0)
        merge = _field(conditional, "next_block_label")
        branches = _field(conditional, "branch_conditions")
        heads = [_field(branch, "next_block_label") for branch in branches] if isinstance(branches, list) else []
        for head in heads:
            walk(head, merge if isinstance(merge, str) else None)
    for block in candidates:
        label = _field(block, "label")
        if label not in placed:
            placed.add(label)
            sequences.append([block])
    return sequences, by_label, next_by_label


def _note_text(block: Any, key: str) -> str | None:
    value = _field(block, key)
    return value.strip() if isinstance(value, str) and value.strip() else None


def step_notes_by_label(blocks: Sequence[Any], finally_block_label: str | None = None) -> dict[str, str]:
    """The raw note text each step reads: its own notes, the next sibling's notes, and its own trailing notes."""
    result: dict[str, str] = {}
    sequences, by_label, next_by_label = _sequences(blocks, finally_block_label)
    for sequence in sequences:
        for index, block in enumerate(sequence):
            following = (
                sequence[index + 1]
                if index + 1 < len(sequence)
                else by_label.get(next_by_label.get(_field(block, "label")) or "")
            )
            parts = [
                _note_text(block, "notes"),
                _note_text(following, "notes") if following is not None else None,
                _note_text(block, "trailing_notes"),
            ]
            text = "\n\n".join(part for part in parts if part)
            if text:
                result[_field(block, "label")] = text
            children = _child_blocks(block)
            if children:
                result.update(step_notes_by_label(children))
    return result


def definition_step_notes(definition: Any) -> dict[str, str]:
    blocks = _field(definition, "blocks")
    if not isinstance(blocks, list):
        return {}
    finally_label = _field(definition, "finally_block_label")
    return step_notes_by_label(blocks, finally_label if isinstance(finally_label, str) else None)


def format_notes_section(notes: str | None) -> str | None:
    return f"{NOTES_SECTION_HEADER}\n{notes}" if notes else None


def strip_inert_block_fields(value: Any) -> Any:
    """Copy a definition, block list or block dump without what a run never reads: block-level
    ``description`` and empty ``notes``/``trailing_notes`` (an empty note and an absent one run alike).

    Only block dicts are touched: data schemas, branch conditions, code steps and parameters
    carry their own ``description`` keys, and those change what a run does.
    """
    if isinstance(value, list):
        return [strip_inert_block_fields(item) for item in value]
    if not isinstance(value, Mapping):
        return value
    result = dict(value)
    if _is_block(result):
        result.pop("description", None)
        for key in NOTE_FIELDS:
            if key in result and not _note_text(result, key):
                result.pop(key)
        if isinstance(result.get("loop_blocks"), list):
            result["loop_blocks"] = strip_inert_block_fields(result["loop_blocks"])
        return result
    for container in ("workflow_definition", "blocks"):
        if container in result:
            result[container] = strip_inert_block_fields(result[container])
    return result


def annotate_notes_read(blocks: list[dict[str, Any]], notes_by_label: Mapping[str, str]) -> None:
    """Add ``notes_read`` to each block dict that reads notes, so a neighbour's note change shows on it."""
    for block in blocks:
        if not isinstance(block, dict):
            continue
        label = block.get("label")
        if isinstance(label, str) and label in notes_by_label:
            block["notes_read"] = notes_by_label[label]
        children = block.get("loop_blocks")
        if isinstance(children, list):
            annotate_notes_read(children, notes_by_label)


def _walk_blocks(blocks: Sequence[Any]) -> Iterator[Any]:
    for block in blocks:
        if not _is_block(block):
            continue
        yield block
        yield from _walk_blocks(_child_blocks(block))


def _present_fields(block: Any) -> Collection[str]:
    return block.keys() if isinstance(block, Mapping) else block.model_fields_set


def block_opts_out_of_workflow_prompt(blocks: Sequence[Any], label: str) -> bool:
    return any(
        _field(block, "label") == label and bool(_field(block, "ignore_workflow_system_prompt"))
        for block in _walk_blocks(blocks)
    )


def block_labels(blocks: Sequence[Any]) -> set[str]:
    return {_field(block, "label") for block in _walk_blocks(blocks)}


def carry_forward_block_notes(
    blocks: Sequence[Any],
    previous_blocks: Sequence[Any],
    *,
    carry_deletions: bool = False,
    same_step_labels: Collection[str] = (),
) -> dict[str, list[str]]:
    """Fill omitted inert fields only for steps whose identity the caller has established.

    A matching label and block type is the identity claim, so a replacement step reusing both inherits; REST
    saves and Copilot proposals pass every submitted label because an omitted field there never means delete.
    An explicit ``null`` or ``""`` is always preserved. With ``carry_deletions``, a deletion recorded in the
    previous version is carried as written only for a proven same step. Returns the kept fields by label.
    """
    previous = {_field(block, "label"): block for block in _walk_blocks(previous_blocks)}
    kept: dict[str, list[str]] = {}
    for block in _walk_blocks(blocks):
        label = _field(block, "label")
        if label not in same_step_labels:
            continue
        prior = previous.get(label)
        if prior is None or _block_type(block) != _block_type(prior):
            continue
        present = _present_fields(block)
        prior_present = _present_fields(prior)
        for key in CARRIED_FIELDS:
            if key in present:
                continue
            value = _field(prior, key)
            if _note_text(prior, key):
                kept.setdefault(_field(block, "label"), []).append(key)
            elif not value and not (carry_deletions and key in prior_present):
                continue
            if isinstance(block, dict):
                block[key] = value
            else:
                setattr(block, key, value)
    return kept


def _parse_blocks(workflow_yaml: str | None) -> list[Any] | None:
    if not workflow_yaml:
        return None
    try:
        return _definition_blocks(safe_load_no_dates(workflow_yaml))
    except Exception:  # noqa: BLE001 - PyYAML constructors can raise non-YAML exceptions.
        return None


def carry_block_notes_from_versions(
    workflow_yaml: str, prior_yaml: str | None, persisted_yaml: str | None
) -> tuple[str, dict[str, list[str]], list[str]]:
    """Carry omitted notes from the latest draft, then from the saved workflow for anything still omitted.

    The draft can be editor YAML from a client that does not send notes, so the saved row is the backstop;
    a deletion in the draft is carried so the saved row can't undo it. Returns the YAML, the kept fields by
    label, and the labels whose notes the submission lost by renaming or removing the block.
    """
    try:
        submitted = safe_load_no_dates(workflow_yaml)
    except Exception:  # noqa: BLE001 - PyYAML constructors can raise non-YAML exceptions.
        return workflow_yaml, {}, []
    submitted_blocks = _definition_blocks(submitted)
    if submitted_blocks is None:
        return workflow_yaml, {}, []
    kept: dict[str, list[str]] = {}
    changed = False
    lost: dict[str, tuple[str | None, str | None]] = {}
    submitted_labels = block_labels(submitted_blocks)
    submitted_types = {_field(block, "label"): _block_type(block) for block in _walk_blocks(submitted_blocks)}
    for source_yaml, carry_deletions in ((prior_yaml, True), (persisted_yaml, False)):
        source_blocks = _parse_blocks(source_yaml)
        if source_blocks is None:
            continue
        before = json.dumps(submitted_blocks, sort_keys=True, default=str)
        for label, fields in carry_forward_block_notes(
            submitted_blocks, source_blocks, carry_deletions=carry_deletions, same_step_labels=submitted_labels
        ).items():
            kept.setdefault(label, []).extend(fields)
        changed = changed or json.dumps(submitted_blocks, sort_keys=True, default=str) != before
        for block in _walk_blocks(source_blocks):
            label = _field(block, "label")
            notes = (_note_text(block, "notes"), _note_text(block, "trailing_notes"))
            if any(notes) and submitted_types.get(label) != _block_type(block):
                lost.setdefault(label, notes)
    source_notes: dict[str, tuple[str, tuple[str | None, str | None]]] = {}
    for source_yaml in (prior_yaml, persisted_yaml):
        for block in _walk_blocks(_parse_blocks(source_yaml) or []):
            label = _field(block, "label")
            notes = (_note_text(block, "notes"), _note_text(block, "trailing_notes"))
            if isinstance(label, str) and any(notes):
                source_notes.setdefault(label, (_block_type(block), notes))
    submitted_note_counts: Counter[tuple[str | None, str | None]] = Counter()
    retained_note_counts: Counter[tuple[str | None, str | None]] = Counter()
    for block in _walk_blocks(submitted_blocks):
        notes = (_note_text(block, "notes"), _note_text(block, "trailing_notes"))
        if not any(notes):
            continue
        submitted_note_counts[notes] += 1
        if source_notes.get(_field(block, "label")) == (_block_type(block), notes):
            retained_note_counts[notes] += 1
    # Only copies beyond unchanged label ownership can represent a note moved from a removed block.
    moved_notes = submitted_note_counts - retained_note_counts
    dropped = []
    for label, notes in lost.items():
        if moved_notes[notes]:
            moved_notes[notes] -= 1
        else:
            dropped.append(label)
    dropped.sort()
    return (dump_workflow_yaml(submitted) if changed else workflow_yaml), dict(sorted(kept.items())), dropped


def remove_note_fields(blocks: Sequence[Any]) -> bool:
    """Delete note and description fields from block dicts at every depth; True when one was removed."""
    changed = False
    for block in _walk_blocks(blocks):
        if isinstance(block, dict):
            for field in CARRIED_FIELDS:
                if field in block:
                    del block[field]
                    changed = True
    return changed


def drop_submitted_note_fields(workflow_yaml: str) -> str:
    """Remove note and description fields from submitted YAML, so the saved values are carried instead."""
    try:
        submitted = safe_load_no_dates(workflow_yaml)
    except Exception:  # noqa: BLE001 - PyYAML constructors can raise non-YAML exceptions.
        return workflow_yaml
    blocks = _definition_blocks(submitted)
    if blocks is None:
        return workflow_yaml
    return dump_workflow_yaml(submitted) if remove_note_fields(blocks) else workflow_yaml


def stored_notes_for_labels(labels: Collection[str], *workflow_yamls: str | None) -> dict[str, dict[str, str]]:
    """The note text each label had in the first of ``workflow_yamls`` that holds one, for a renamed step to reuse."""
    found: dict[str, dict[str, str]] = {}
    for workflow_yaml in workflow_yamls:
        for block in _walk_blocks(_parse_blocks(workflow_yaml) or []):
            label = _field(block, "label")
            if label not in labels or label in found:
                continue
            texts = {key: text for key in NOTE_FIELDS if (text := _note_text(block, key))}
            if texts:
                found[label] = texts
    return found


def block_field_texts(workflow_yaml: str | None) -> dict[str, dict[str, str]]:
    """The set note and description text of every block, by label and field."""
    found: dict[str, dict[str, str]] = {}
    for block in _walk_blocks(_parse_blocks(workflow_yaml) or []):
        texts = {key: text for key in CARRIED_FIELDS if (text := _note_text(block, key))}
        if texts:
            found[_field(block, "label")] = texts
    return found


def labels_with_different_notes(workflow_yaml: str | None, other_yaml: str | None) -> list[str]:
    """Labels in ``other_yaml`` whose notes differ from ``workflow_yaml``'s, including blocks it renamed or removed."""
    blocks = _parse_blocks(workflow_yaml)
    other_blocks = _parse_blocks(other_yaml)
    if blocks is None or other_blocks is None:
        return []
    texts = {_field(block, "label"): _note_texts(block) for block in _walk_blocks(blocks)}
    return sorted(
        _field(block, "label")
        for block in _walk_blocks(other_blocks)
        if texts.get(_field(block, "label"), (None, None)) != _note_texts(block)
    )


def _note_texts(block: Any) -> tuple[str | None, str | None]:
    return _note_text(block, "notes"), _note_text(block, "trailing_notes")


def _definition_blocks(parsed: Any) -> list[Any] | None:
    definition = parsed.get("workflow_definition") if isinstance(parsed, dict) else None
    blocks = definition.get("blocks") if isinstance(definition, dict) else None
    return blocks if isinstance(blocks, list) else None
