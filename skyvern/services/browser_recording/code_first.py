"""
Code-first recording conversion: captured actions -> deterministic Playwright code blocks.

Reuses the Workflow Copilot trajectory synthesizer so recordings and copilot scouting
share one code-generation substrate, parameter policy, and sandbox safety gate.
"""

import re
import textwrap
import typing as t
from dataclasses import dataclass, field
from urllib.parse import urlparse

import structlog

from skyvern.client.types.workflow_definition_yaml_blocks_item import WorkflowDefinitionYamlBlocksItem_Code
from skyvern.client.types.workflow_definition_yaml_parameters_item import WorkflowDefinitionYamlParametersItem_Workflow
from skyvern.forge.sdk.copilot.code_block_synthesis import synthesize_code_block, synthesize_goto_code_block
from skyvern.forge.sdk.workflow.exceptions import InsecureCodeDetected
from skyvern.forge.sdk.workflow.models.block import CodeBlock
from skyvern.services.browser_recording.types import (
    Action,
    ActionClick,
    ActionHover,
    ActionInputText,
    ActionUrlChange,
    ActionWait,
    RecordingDraftStep,
)

LOG = structlog.get_logger(__name__)

# A navigation this soon after a click or text submit was caused by it; the emitted
# click/press already waits for the load, so no goto is emitted for that navigation.
CLICK_NAVIGATION_WINDOW_MS = 3000

# Draft steps reference their source action by (kind, timestamp_start); tolerance
# absorbs float round-tripping through JSON, not clock skew.
DRAFT_TIMESTAMP_TOLERANCE_MS = 1.0

ActionDraftPair = tuple[Action, RecordingDraftStep | None]

# Interactions that silently vanish from a workflow if the synthesizer cannot
# locate them; any unlocatable one forces the legacy (LLM agent block) fallback.
# Dropped hovers are tolerated: they are usually incidental to a located click.
_REQUIRED_LOCATOR_TOOLS = frozenset({"click", "type_text", "select_option"})

CodeFirstResult = tuple[
    list[WorkflowDefinitionYamlBlocksItem_Code],
    list[WorkflowDefinitionYamlParametersItem_Workflow],
]


@dataclass
class RecordingSegment:
    source_url: str | None = None
    pairs: list[ActionDraftPair] = field(default_factory=list)


def apply_draft_overlay(
    actions: list[Action],
    draft_steps: list[RecordingDraftStep] | None,
) -> list[ActionDraftPair]:
    """Join user-edited draft steps back onto their source actions; a missing draft means deleted."""
    if draft_steps is None:
        return [(action, None) for action in actions]
    if not draft_steps:
        # An empty list means the user deleted every interpreted step.
        return []

    remaining = [draft for draft in draft_steps if draft.timestamp_start is not None]
    if not remaining:
        # Drafts without source timestamps cannot be joined back to actions;
        # keep the raw actions rather than treating everything as deleted.
        return [(action, None) for action in actions]

    pairs: list[ActionDraftPair] = []
    for action in actions:
        matched: RecordingDraftStep | None = None
        for index, draft in enumerate(remaining):
            if draft.action_kind != action.kind:
                continue
            if abs((draft.timestamp_start or 0.0) - action.timestamp_start) <= DRAFT_TIMESTAMP_TOLERANCE_MS:
                matched = remaining.pop(index)
                break
        if matched is None:
            LOG.debug(
                "record_browser.code_first_overlay_dropped_action",
                action_kind=action.kind,
                action_timestamp_start=action.timestamp_start,
            )
            continue
        pairs.append((action, matched))

    return pairs


def segment_actions(pairs: list[ActionDraftPair]) -> list[RecordingSegment]:
    """Split at user-initiated navigations; click-caused navigations stay inside their segment."""
    segments = [RecordingSegment()]
    last_interactive_end: float | None = None

    for action, draft in pairs:
        if isinstance(action, ActionUrlChange):
            caused_by_interaction = (
                last_interactive_end is not None
                and action.timestamp_start - last_interactive_end <= CLICK_NAVIGATION_WINDOW_MS
            )
            if caused_by_interaction:
                # One navigation per interaction: a later url_change inside the same
                # window is a genuine user navigation and must start a new segment.
                last_interactive_end = None
                continue
            url = ((draft.url or "").strip() if draft else "") or action.url
            if segments[-1].pairs:
                segments.append(RecordingSegment(source_url=url))
            else:
                segments[-1].source_url = url
            continue

        if isinstance(action, (ActionClick, ActionInputText)):
            last_interactive_end = action.timestamp_end

        segments[-1].pairs.append((action, draft))

    if segments[0].source_url is None and segments[0].pairs:
        segments[0].source_url = segments[0].pairs[0][0].url

    return [segment for segment in segments if segment.pairs or segment.source_url]


def _interaction_for_action(action: Action) -> dict[str, t.Any] | None:
    target = action.target
    base: dict[str, t.Any] = {}
    if target.selector:
        base["selector"] = target.selector
    if target.role:
        base["role"] = target.role
    if target.accessible_name:
        base["accessible_name"] = target.accessible_name

    if isinstance(action, ActionClick):
        return {"tool_name": "click", **base}
    if isinstance(action, ActionHover):
        return {"tool_name": "hover", **base}
    if isinstance(action, ActionInputText):
        if (target.tag_name or "").upper() == "SELECT":
            return {"tool_name": "select_option", "value": action.input_value, **base}
        interaction: dict[str, t.Any] = {"tool_name": "type_text", **base}
        # A password typed during recording must become a parameter slot without
        # a plaintext default; the value never reaches code or parameter defaults.
        if (target.input_type or "").lower() != "password":
            interaction["typed_value"] = action.input_value
        return interaction
    if isinstance(action, ActionWait):
        return {"tool_name": "wait", "duration_ms": action.duration_ms}
    return None


def segment_trajectory(segment: RecordingSegment) -> list[dict[str, t.Any]]:
    trajectory: list[dict[str, t.Any]] = []
    for action, draft in segment.pairs:
        interaction = _interaction_for_action(action)
        if interaction is None:
            continue
        if interaction["tool_name"] == "wait" and draft is not None and draft.wait_sec:
            interaction["duration_ms"] = int(draft.wait_sec) * 1000
        trajectory.append(interaction)
    if trajectory and segment.source_url:
        trajectory[0] = {**trajectory[0], "source_url": segment.source_url}
    return trajectory


def _segment_label(segment: RecordingSegment, used: set[str]) -> str:
    host = ""
    if segment.source_url:
        try:
            host = urlparse(segment.source_url).netloc
        except ValueError:
            pass
    base = re.sub(r"\W+", "_", f"recorded_{host}" if host else "recorded_steps").strip("_").lower()
    label = base
    suffix = 2
    while label in used:
        label = f"{base}_{suffix}"
        suffix += 1
    used.add(label)
    return label


def actions_to_code_first_blocks(
    actions: list[Action],
    draft_steps: list[RecordingDraftStep] | None,
) -> CodeFirstResult | None:
    """Convert recorded actions into code blocks and parameters; None means fall back to legacy blocks."""
    if draft_steps is not None and not draft_steps:
        # The user deleted every interpreted step; commit an empty workflow rather
        # than falling back to blocks derived from the raw actions.
        return [], []
    pairs = apply_draft_overlay(actions, draft_steps)
    segments = segment_actions(pairs)

    blocks: list[WorkflowDefinitionYamlBlocksItem_Code] = []
    # Global parameter key -> identity of the recorded field that minted it
    # (selector, role, accessible name). The same field re-filled in a later
    # segment reuses its key; a different same-labeled field gets a fresh key.
    parameter_identities: dict[str, tuple[str, str, str] | None] = {}
    used_labels: set[str] = set()

    for segment in segments:
        trajectory = segment_trajectory(segment)
        for interaction in trajectory:
            if interaction["tool_name"] in _REQUIRED_LOCATOR_TOOLS and not (
                interaction.get("selector") or (interaction.get("role") and interaction.get("accessible_name"))
            ):
                LOG.info(
                    "record_browser.code_first_unlocatable_interaction",
                    tool_name=interaction["tool_name"],
                )
                return None
        if trajectory:
            synthesized = synthesize_code_block(trajectory)
        elif segment.source_url:
            synthesized = synthesize_goto_code_block(segment.source_url)
        else:
            synthesized = None
        if synthesized is None:
            continue

        code = textwrap.dedent(synthesized.code)
        identity_by_key: dict[str, tuple[str, str, str]] = {}
        for trajectory_index, param_key in synthesized.diagnostics.typed_param_bindings:
            if 0 <= trajectory_index < len(trajectory):
                source = trajectory[trajectory_index]
                identity_by_key.setdefault(
                    param_key,
                    (
                        str(source.get("selector") or ""),
                        str(source.get("role") or ""),
                        str(source.get("accessible_name") or ""),
                    ),
                )

        block_parameter_keys: list[str] = []
        block_original_keys = {str(parameter.get("key") or "").strip() for parameter in synthesized.parameters}
        renames: dict[str, str] = {}
        for parameter in synthesized.parameters:
            key = str(parameter.get("key") or "").strip()
            if not key:
                continue
            if parameter.get("credential_id"):
                # Recordings cannot bind credentials yet; the synthesizer only emits
                # these for fill_credential_field interactions, which we never map.
                LOG.warning("record_browser.code_first_unexpected_credential_parameter", parameter_key=key)
                continue
            identity = identity_by_key.get(key)
            resolved_key = key
            if key in parameter_identities and (identity is None or parameter_identities[key] != identity):
                suffix = 2
                while True:
                    candidate = f"{key}_{suffix}"
                    suffix += 1
                    if candidate in block_original_keys or candidate in renames.values():
                        continue
                    if candidate not in parameter_identities or (
                        identity is not None and parameter_identities[candidate] == identity
                    ):
                        resolved_key = candidate
                        break
                renames[key] = resolved_key
            parameter_identities.setdefault(resolved_key, identity)
            block_parameter_keys.append(resolved_key)

        for original_key, resolved_key in renames.items():
            # A typed-text fill is the only emission that reads a string parameter,
            # always as `str(<key>)`, so this rename cannot touch selectors. Rename
            # targets never collide with this block's own keys, so one rename cannot
            # cascade onto the fill of another parameter.
            code = code.replace(f"str({original_key})", f"str({resolved_key})")

        try:
            CodeBlock.is_safe_code(code)
        # ast.parse raises ValueError on e.g. null bytes; any failure inside the
        # safety gate must fall back to legacy blocks, never surface as a 500.
        except (SyntaxError, ValueError, InsecureCodeDetected):
            LOG.warning(
                "record_browser.code_first_safety_rejected",
                dropped_interaction_count=len(synthesized.diagnostics.dropped_interactions),
                exc_info=True,
            )
            return None

        blocks.append(
            WorkflowDefinitionYamlBlocksItem_Code(
                label=_segment_label(segment, used_labels),
                code=code,
                parameter_keys=block_parameter_keys or None,
                # Editor's convertToNode reads block.parameters.map(p => p.key); mirror the action block.
                parameters=[{"key": key} for key in block_parameter_keys],
            )
        )

    if not blocks:
        return None

    parameters = [
        WorkflowDefinitionYamlParametersItem_Workflow(
            key=key,
            workflow_parameter_type="string",
            # Like the legacy path, recorded values never persist as defaults: a
            # secret typed into any field (not just type=password) must not land
            # in a DB-stored, API-exposed default_value. The user binds values.
            default_value="",
            description="",
        )
        for key in parameter_identities
    ]
    return blocks, parameters
