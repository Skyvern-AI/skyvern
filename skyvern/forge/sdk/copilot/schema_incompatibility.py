"""Typed terminal outcome for an edited extraction schema whose fields map to no
output the workflow produces.

When a user edits a code block's confirmed ``extraction_schema`` to add fields that
overlap none of the block's known output contract (its top-level return keys plus
confirmed ``goal_value_paths``), re-authoring cannot reconcile the mismatch: there is
nothing on the page or in the return for the new field to bind to. Surfacing it as a
typed, non-repairable schema-incompatibility outcome — rather than letting the agent
churn until the repair ceiling — preserves the existing draft and tells the user
exactly which fields do not map.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from skyvern.forge.sdk.copilot.blocker_signal import (
    CopilotToolBlockerSignal,
    assert_clean_user_facing_text,
)

SCHEMA_INCOMPATIBILITY_REASON_CODE = "schema_incompatibility"
SCHEMA_INCOMPATIBILITY_FAILURE_TYPE = "schema_incompatibility"
SCHEMA_INCOMPATIBILITY_BLOCKED_TOOL = "update_and_run_blocks"

_DEFAULT_NEXT_ACTIONS: tuple[str, ...] = (
    "Map the field to a value this workflow already produces.",
    "Remove the field from the extraction schema.",
    "Describe what the field should capture so a step can be added to produce it.",
)

_GENERIC_USER_REASON = (
    "I couldn't apply the edited extraction schema because some of its fields don't match "
    "any value this workflow currently produces. Tell me what they should map to and I'll try again. "
    "Your current draft is unchanged."
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
    """Fold per-block incompatibilities into a single terminal record. The merged
    paths are de-duplicated and ordered; the first block label anchors the record."""
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
    verb = "doesn't" if len(incompat.incompatible_paths) == 1 else "don't"
    sentences = [
        f"I couldn't apply the edited extraction schema: the field {fields} {verb} match any value this workflow currently produces."
    ]
    if incompat.known_output_paths:
        outputs = ", ".join(incompat.known_output_paths)
        sentences.append(f"This workflow's data currently covers {outputs}.")
    sentences.append(
        "Tell me which existing output it should map to, or remove it, and I'll try again. Your current draft is unchanged."
    )
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
        f"STOP: the edited extraction_schema declares field(s) [{incompatible}] that map to no output block "
        f"`{incompat.block_label}` produces [{known}]. This is not repairable by re-authoring the same draft. "
        "Report the mismatch to the user and ask which existing output the field should map to. "
        "The prior draft is preserved; do not rerun the blocks."
    )


def build_schema_incompatibility_blocker_signal(incompat: SchemaIncompatibility) -> CopilotToolBlockerSignal:
    return CopilotToolBlockerSignal(
        blocker_kind="tool_error",
        agent_steering_text=render_schema_incompatibility_agent_steer(incompat),
        user_facing_reason=render_schema_incompatibility_user_reason(incompat),
        recovery_hint="report_blocker_to_user",
        cleared_by_tools=frozenset(),
        preserves_workflow_draft=incompat.preserves_workflow_draft,
        renders_final_reply=True,
        internal_reason_code=SCHEMA_INCOMPATIBILITY_REASON_CODE,
        blocked_tool=SCHEMA_INCOMPATIBILITY_BLOCKED_TOOL,
        extra={"schema_incompatibility": incompat.to_summary_dict()},
    )
