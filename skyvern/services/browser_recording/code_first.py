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
from skyvern.client.types.workflow_definition_yaml_parameters_item import (
    WorkflowDefinitionYamlParametersItem,
    WorkflowDefinitionYamlParametersItem_Credential,
    WorkflowDefinitionYamlParametersItem_Workflow,
)
from skyvern.forge.sdk.copilot.code_block_steps import derive_code_block_steps
from skyvern.forge.sdk.copilot.code_block_synthesis import (
    CREDENTIAL_FILL_TOOL_NAME,
    synthesize_code_block,
    synthesize_goto_code_block,
)
from skyvern.forge.sdk.credential_site_policy import site_of
from skyvern.forge.sdk.workflow.exceptions import InsecureCodeDetected
from skyvern.forge.sdk.workflow.models.block import CodeBlock
from skyvern.services.browser_recording.redact import is_identifier_field, texts_are_labels
from skyvern.services.browser_recording.types import (
    Action,
    ActionClick,
    ActionHover,
    ActionInputText,
    ActionPressKey,
    ActionTarget,
    ActionUrlChange,
    ActionWait,
    CredentialKind,
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
_REQUIRED_LOCATOR_TOOLS = frozenset({"click", "type_text", "select_option", CREDENTIAL_FILL_TOOL_NAME})

# The credential field each recorded kind fills. `secret` mirrors the legacy path's single
# `secret_value`; `magic_link` resolves at runtime rather than as a field, and `credit_card`
# spreads over six fields the recorder cannot tell apart, so both stay plain parameters.
CREDENTIAL_FILL_FIELD_BY_KIND: dict[CredentialKind, str] = {
    "password": "password",
    "totp": "totp",
    "secret": "secret_value",
}
_ALLOWED_CREDENTIAL_FILL_FIELDS = frozenset(CREDENTIAL_FILL_FIELD_BY_KIND.values()) | {"username"}

# Interactions that can sit between the identifier and the password without ending the form:
# other fields of the same form, a stray hover, a dwell.
_FORM_INTERNAL_TOOLS = frozenset({"type_text", "hover", "wait"})

# A click that only moves focus into a field. The ordinary mouse-driven login clicks the
# password box between typing the identifier and typing the password, and the recorder emits
# an action for it; treating that as the end of the form loses the binding on the common flow.
_TEXT_ENTRY_TAGS = frozenset({"textarea", "select"})
_TEXT_ENTRY_ROLES = frozenset({"textbox", "searchbox", "combobox"})
# An <input> is only a field to focus for these types: submit, button, reset and image are
# controls that end the form, and they carry the same tag name.
_TEXT_ENTRY_INPUT_TYPES = frozenset({"", "text", "email", "tel", "url", "search", "password", "number", "date", "time"})

CodeFirstResult = tuple[
    list[WorkflowDefinitionYamlBlocksItem_Code],
    list[WorkflowDefinitionYamlParametersItem],
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


def transfer_focus_click_credentials(pairs: list[ActionDraftPair]) -> list[ActionDraftPair]:
    """Move a credential bound on a click onto the fill of the same field.

    The recording panel offers the credential prompt on every step it classifies, and the
    mouse-driven login clicks the password box before typing into it, so the user can bind on
    the click card - after which the panel dismisses the prompt on the fill. The legacy path
    reads the credential off whichever step carries it; a code block has to fill it.
    """
    transferred = list(pairs)
    for index, (action, draft) in enumerate(pairs):
        if not isinstance(action, ActionClick) or draft is None or draft.credential_kind is None:
            continue
        credential_id = (draft.credential_id or "").strip()
        selector = (action.target.selector or "").strip()
        if not credential_id or not selector:
            continue
        for later in range(index + 1, len(transferred)):
            later_action, later_draft = transferred[later]
            if not isinstance(later_action, ActionInputText):
                continue
            if (later_action.target.selector or "").strip() != selector:
                continue
            if later_draft is None or (later_draft.credential_id or "").strip():
                break
            transferred[later] = (
                later_action,
                later_draft.model_copy(
                    update={"credential_id": credential_id, "credential_kind": draft.credential_kind}
                ),
            )
            break
    return transferred


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

        if isinstance(action, (ActionClick, ActionInputText, ActionPressKey)):
            last_interactive_end = action.timestamp_end

        segments[-1].pairs.append((action, draft))

    if segments[0].source_url is None and segments[0].pairs:
        segments[0].source_url = segments[0].pairs[0][0].url

    return [segment for segment in segments if segment.pairs or segment.source_url]


def _credential_fill(draft: RecordingDraftStep | None) -> dict[str, t.Any] | None:
    """The credential fill this recorded step was bound to in the recording panel, if any."""
    if draft is None:
        return None
    credential_id = (draft.credential_id or "").strip()
    if not credential_id or draft.credential_kind is None:
        return None
    field = CREDENTIAL_FILL_FIELD_BY_KIND.get(draft.credential_kind)
    if field is None:
        return None
    return {
        "tool_name": CREDENTIAL_FILL_TOOL_NAME,
        "credential_id": credential_id,
        "credential_field": field,
        # The synthesizer derives the parameter key from this name; the credential id is a
        # token the editor swaps for a real key once it can see the workflow's parameters.
        "credential_name": credential_id,
    }


def _interaction_for_action(action: Action, draft: RecordingDraftStep | None = None) -> dict[str, t.Any] | None:
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
        credential_fill = _credential_fill(draft)
        if credential_fill is not None:
            return {**credential_fill, **base}
        if (target.tag_name or "").upper() == "SELECT":
            return {"tool_name": "select_option", "value": action.input_value, **base}
        interaction: dict[str, t.Any] = {"tool_name": "type_text", **base}
        # A password typed during recording must become a parameter slot without
        # a plaintext default; the value never reaches code or parameter defaults.
        if (target.input_type or "").lower() != "password":
            interaction["typed_value"] = action.input_value
        return interaction
    if isinstance(action, ActionPressKey):
        # Not in _REQUIRED_LOCATOR_TOOLS: an unlocatable press falls back to
        # page.keyboard.press, which is still deterministic replay.
        return {"tool_name": "press_key", "key": action.key, **base}
    if isinstance(action, ActionWait):
        return {"tool_name": "wait", "duration_ms": action.duration_ms}
    return None


def _is_focus_click(action: Action) -> bool:
    if not isinstance(action, ActionClick):
        return False
    target = action.target
    tag_name = (target.tag_name or "").lower()
    if tag_name in _TEXT_ENTRY_TAGS:
        return True
    if tag_name == "input":
        return (target.input_type or "").lower() in _TEXT_ENTRY_INPUT_TYPES
    return (target.role or "").lower() in _TEXT_ENTRY_ROLES


def _identifier_fill_index(
    trajectory: list[dict[str, t.Any]],
    sources: list[ActionDraftPair],
    index: int,
) -> int | None:
    """The index of the login identifier typed into the same form before `index`, if any.

    The search reaches back over other fields of that form - a login page can ask for a
    tenant domain beside the identifier, and a signup form for a name, and past a click that
    only moves focus into a field - but stops at anything that ends the form: a click on
    something other than a field, a navigation, another credential fill. A field already bound
    to a credential of the user's own is skipped rather than claimed.
    """
    for candidate in range(index - 1, -1, -1):
        tool_name = trajectory[candidate]["tool_name"]
        action, draft = sources[candidate]
        if tool_name == "click" and _is_focus_click(action):
            continue
        if tool_name not in _FORM_INTERNAL_TOOLS:
            return None
        if tool_name != "type_text":
            continue
        if draft is not None and (draft.credential_id or "").strip():
            continue
        if is_identifier_field(action):
            return candidate
    return None


def _bind_identifier_fills(
    trajectory: list[dict[str, t.Any]],
    sources: list[ActionDraftPair],
) -> None:
    """Point the login identifier typed before a password fill at the credential's username.

    Only the credential's password field is bound in the recording panel, so without this the
    identifier stays an empty workflow parameter and the login types nothing into it. The
    field has to look like an identifier to be claimed: "the field before the password" also
    describes a tenant domain or a search box, and filling a username into one of those types
    a wrong value while leaving the real identifier empty.
    """
    for index, interaction in enumerate(trajectory):
        if interaction.get("credential_field") != "password":
            continue
        candidate = _identifier_fill_index(trajectory, sources, index)
        if candidate is None:
            continue
        previous = trajectory[candidate]
        trajectory[candidate] = {
            key: value for key, value in previous.items() if key not in ("tool_name", "typed_value")
        } | {
            "tool_name": CREDENTIAL_FILL_TOOL_NAME,
            "credential_id": interaction["credential_id"],
            "credential_field": "username",
            "credential_name": interaction["credential_name"],
        }


def segment_trajectory(segment: RecordingSegment, *, bind_credentials: bool = True) -> list[dict[str, t.Any]]:
    trajectory: list[dict[str, t.Any]] = []
    sources: list[ActionDraftPair] = []
    for action, draft in segment.pairs:
        interaction = _interaction_for_action(action, draft if bind_credentials else None)
        if interaction is None:
            continue
        if interaction["tool_name"] == "wait" and draft is not None and draft.wait_sec:
            interaction["duration_ms"] = int(draft.wait_sec) * 1000
        trajectory.append(interaction)
        sources.append((action, draft))
    if bind_credentials:
        _bind_identifier_fills(trajectory, sources)
    if trajectory and segment.source_url:
        trajectory[0] = {**trajectory[0], "source_url": segment.source_url}
    return trajectory


_SEARCH_FIELD_HINTS = frozenset({"search", "query", "keyword"})
_MAX_NAME_WORDS = 4


def _slugify(text: str, max_words: int = _MAX_NAME_WORDS) -> str:
    words = [word for word in re.split(r"\W+", text.lower()) if word]
    return "_".join(words[:max_words])


def _site_slug(url: str | None) -> str:
    """A readable site name from the segment's entry URL; empty for IPs and localhost."""
    if not url:
        return ""
    site = site_of(url)
    if site is not None:
        return _slugify(site[2].split(".", 1)[0])
    try:
        host = urlparse(url).hostname or ""
    except ValueError:
        return ""
    if ":" in host:
        # An IPv6 literal; no name to read off it.
        return ""
    labels = [label for label in host.split(".") if label]
    if not labels or all(label.isdigit() for label in labels) or labels == ["localhost"]:
        return ""
    # A host with no registrable domain is useful only when it is a named intranet host.
    return _slugify(labels[0]) if len(labels) == 1 else ""


def _target_name(target: ActionTarget) -> str:
    candidates = [target.accessible_name, target.id]
    if texts_are_labels(target.tag_name):
        candidates.extend(target.texts)
    for candidate in candidates:
        slug = _slugify(candidate or "")
        if slug:
            return slug
    match = re.fullmatch(r"[#.]([\w-]+)", (target.selector or "").strip())
    return _slugify(match.group(1)) if match else ""


def _segment_summary(segment: RecordingSegment, emitted_indices: set[int] | None = None) -> str:
    """A general description of what the segment's interactions do, as a snake_case phrase."""
    site = _site_slug(segment.source_url)
    actions: list[Action] = []
    trajectory_index = 0
    for action, draft in segment.pairs:
        if _interaction_for_action(action, draft) is None:
            continue
        if emitted_indices is None or trajectory_index in emitted_indices:
            actions.append(action)
        trajectory_index += 1
    typed = [action for action in actions if isinstance(action, ActionInputText)]
    clicks = [action for action in actions if isinstance(action, ActionClick)]

    if any((action.target.input_type or "").lower() == "password" for action in typed):
        return f"log_in_to_{site}" if site else "log_in"
    if any(not _SEARCH_FIELD_HINTS.isdisjoint(_target_name(action.target).split("_")) for action in typed):
        return f"search_{site}" if site else "search"
    if typed and all((action.target.tag_name or "").upper() == "SELECT" for action in typed):
        name = _target_name(typed[0].target)
        return f"select_{name}" if name else "select_an_option"
    if typed:
        if len(typed) > 1:
            return "fill_out_the_form"
        name = _target_name(typed[0].target)
        return f"fill_in_{name}" if name else "fill_in_a_field"
    if clicks:
        # The last click is the one that advances the page (submit, next, a link).
        name = _target_name(clicks[-1].target)
        return f"click_{name}" if name else "click_through_the_page"
    presses = [action for action in actions if isinstance(action, ActionPressKey)]
    if presses:
        key = _slugify(presses[-1].key)
        return f"press_{key}" if key else "press_a_key"
    hovers = [action for action in actions if isinstance(action, ActionHover)]
    if hovers:
        name = _target_name(hovers[0].target)
        return f"hover_over_{name}" if name else "hover_over_the_page"
    if any(isinstance(action, ActionWait) for action in actions):
        return "wait"
    return f"open_{site}" if site else "open_the_page"


def _segment_label(segment: RecordingSegment, used: set[str], emitted_indices: set[int]) -> str:
    base = re.sub(r"\W+", "_", _segment_summary(segment, emitted_indices)).strip("_").lower() or "recorded_steps"
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
    *,
    bind_credentials: bool = True,
) -> CodeFirstResult | None:
    """Convert recorded actions into code blocks and parameters; None means fall back to legacy blocks."""
    if draft_steps is not None and not draft_steps:
        # The user deleted every interpreted step; commit an empty workflow rather
        # than falling back to blocks derived from the raw actions.
        return [], []
    pairs = transfer_focus_click_credentials(apply_draft_overlay(actions, draft_steps))
    segments = segment_actions(pairs)

    blocks: list[WorkflowDefinitionYamlBlocksItem_Code] = []
    # Global parameter key -> identity of the recorded field that minted it
    # (selector, role, accessible name). The same field re-filled in a later
    # segment reuses its key; a different same-labeled field gets a fresh key.
    parameter_identities: dict[str, tuple[str, str, str] | None] = {}
    # Credential parameter key (the credential id, a token the editor re-keys) -> credential id.
    credential_ids_by_key: dict[str, str] = {}
    used_labels: set[str] = set()

    for segment in segments:
        trajectory = segment_trajectory(segment, bind_credentials=bind_credentials)
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
            synthesized = synthesize_code_block(trajectory, allowed_credential_fields=_ALLOWED_CREDENTIAL_FILL_FIELDS)
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
            credential_id = str(parameter.get("credential_id") or "").strip()
            if credential_id:
                # The key is the credential id, so two credentials cannot share one and the
                # cross-block rename below has nothing to resolve.
                credential_ids_by_key[key] = credential_id
                block_parameter_keys.append(key)
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

        emitted_indices = {
            index
            for record in synthesized.diagnostics.emitted_interactions
            if isinstance((index := record.get("trajectory_index")), int) and index >= 0
        }
        blocks.append(
            WorkflowDefinitionYamlBlocksItem_Code(
                label=_segment_label(segment, used_labels, emitted_indices),
                code=code,
                parameter_keys=block_parameter_keys or None,
                # Editor's convertToNode reads block.parameters.map(p => p.key); mirror the action block.
                parameters=[{"key": key} for key in block_parameter_keys],
                # A non-null prompt is what makes the editor render the code-first node; "" is
                # runtime-neutral (every backend prompt check is truthiness based) and leaves the
                # Goal for the user, because a fabricated one would arm runtime self-heal.
                prompt="",
                steps=derive_code_block_steps(code) or None,
            )
        )

    if not blocks:
        return None

    parameters: list[WorkflowDefinitionYamlParametersItem] = [
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
    parameters.extend(
        WorkflowDefinitionYamlParametersItem_Credential(
            key=key,
            credential_id=credential_id,
            description="",
        )
        for key, credential_id in sorted(credential_ids_by_key.items())
    )
    return blocks, parameters
