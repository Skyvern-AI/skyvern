"""Typed finding for an edited extraction schema whose fields map to no output the
workflow produces.

When a user edits a code block's confirmed ``extraction_schema`` to add fields that
overlap none of the block's known output contract (its top-level return keys plus
confirmed ``goal_value_paths``), re-authoring cannot reconcile the mismatch: there is
nothing on the page or in the return for the new field to bind to. The draft persists
and the finding names the fields that do not map, because a test-run cannot: the run
succeeds and silently omits them.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from skyvern.forge.sdk.copilot.blocker_signal import (
    SCHEMA_INCOMPATIBILITY_REASON_CODE as SCHEMA_INCOMPATIBILITY_REASON_CODE,
)
from skyvern.forge.sdk.copilot.blocker_signal import (
    assert_clean_user_facing_text,
)

SCHEMA_INCOMPATIBILITY_BLOCKED_TOOL = "update_and_run_blocks"

_DEFAULT_NEXT_ACTIONS: tuple[str, ...] = (
    "Map the field to a value this workflow already produces.",
    "Remove the field from the extraction schema.",
    "Describe what the field should capture so a step can be added to produce it.",
)

_GENERIC_USER_REASON = (
    "I saved the workflow, but some fields in the edited extraction schema don't match any value it "
    "produces, so those fields will come back empty. Tell me what they should map to and I'll wire them up."
)


class SchemaIncompatibility(BaseModel):
    model_config = ConfigDict(frozen=True)

    block_label: str
    incompatible_paths: tuple[str, ...]
    known_output_paths: tuple[str, ...]
    edited_schema_summary: str = ""
    preserves_workflow_draft: bool = True
    next_actions: tuple[str, ...] = Field(default=_DEFAULT_NEXT_ACTIONS)

    def to_summary_dict(self) -> dict[str, Any]:
        return {
            "block_label": self.block_label,
            "incompatible_paths": list(self.incompatible_paths),
            "known_output_paths": list(self.known_output_paths),
            "edited_schema_summary": self.edited_schema_summary,
            "preserves_workflow_draft": self.preserves_workflow_draft,
            "next_actions": list(self.next_actions),
        }


def merge_schema_incompatibilities(items: list[SchemaIncompatibility]) -> SchemaIncompatibility | None:
    """Fold per-block incompatibilities into a single record. The merged paths are
    de-duplicated and ordered; the first block label anchors the record."""
    real = [item for item in items if item is not None]
    if not real:
        return None
    if len(real) == 1:
        return real[0]
    incompatible: list[str] = []
    known: list[str] = []
    summaries: list[str] = []
    for item in real:
        for path in item.incompatible_paths:
            if path not in incompatible:
                incompatible.append(path)
        for path in item.known_output_paths:
            if path not in known:
                known.append(path)
        if item.edited_schema_summary and item.edited_schema_summary not in summaries:
            summaries.append(item.edited_schema_summary)
    return SchemaIncompatibility(
        block_label=real[0].block_label,
        incompatible_paths=tuple(incompatible),
        known_output_paths=tuple(known),
        edited_schema_summary="; ".join(summaries),
        preserves_workflow_draft=all(item.preserves_workflow_draft for item in real),
    )


def _field_list_phrase(paths: tuple[str, ...]) -> str:
    quoted = [f"`{path}`" for path in paths]
    if len(quoted) == 1:
        return quoted[0]
    if len(quoted) == 2:
        return f"{quoted[0]} and {quoted[1]}"
    return ", ".join(quoted[:-1]) + f", and {quoted[-1]}"


def render_schema_incompatibility_user_reason(incompat: SchemaIncompatibility) -> str:
    """Product-language reply rendered from the structured incompatibility. Falls back
    to a field-free message if an exotic field name trips the user-facing safety gate."""
    fields = _field_list_phrase(incompat.incompatible_paths)
    single = len(incompat.incompatible_paths) == 1
    verb = "doesn't" if single else "don't"
    subject = "it will" if single else "they will"
    sentences = [
        f"I saved the workflow, but the field {fields} {verb} match any value it produces, so {subject} come back empty."
    ]
    if incompat.known_output_paths:
        outputs = ", ".join(incompat.known_output_paths)
        sentences.append(f"This workflow's data currently covers {outputs}.")
    sentences.append("Tell me which existing output it should map to, or remove it, and I'll wire it up.")
    candidate = " ".join(sentences)
    try:
        assert_clean_user_facing_text(candidate, blocked_tool=SCHEMA_INCOMPATIBILITY_BLOCKED_TOOL)
    except ValueError:
        return _GENERIC_USER_REASON
    return candidate


def render_schema_incompatibility_agent_steer(incompat: SchemaIncompatibility) -> str:
    incompatible = ", ".join(incompat.incompatible_paths) or "(unknown)"
    known = ", ".join(incompat.known_output_paths) or "(none recorded)"
    return (
        f"The edited extraction_schema declares field(s) [{incompatible}] that map to no output block "
        f"`{incompat.block_label}` produces [{known}]. Re-authoring the same draft will not resolve it; "
        "ask the user which existing output the field should map to."
    )
