"""Observation-only projection of recorded actions: no goal classification, ranking, selector choice, or labeling."""

import math
import re
from typing import Literal
from urllib.parse import quote, quote_plus

import structlog
from pydantic import BaseModel

from skyvern.services.browser_recording.code_first import (
    ActionDraftPair,
    apply_draft_overlay,
    attribute_click_navigations,
    transfer_focus_click_credentials,
)
from skyvern.services.browser_recording.redact import (
    credential_kind_for_action,
    texts_are_labels,
    texts_are_typed_input,
)
from skyvern.services.browser_recording.types import (
    Action,
    ActionInputText,
    ActionKind,
    ActionPressKey,
    ActionUrlChange,
    ActionWait,
    CredentialKind,
    RecordingDraftStep,
)

LOG = structlog.get_logger(__name__)

MAX_EVIDENCE_CHARS = 120_000
MAX_VISIBLE_TEXTS = 5
MAX_VISIBLE_TEXT_CHARS = 120


class RecordedPointerEvidence(BaseModel):
    # Field names are the model's only cue that these are fractions of the viewport, not of the target.
    viewport_x_fraction: float
    viewport_y_fraction: float


class RecordedTargetEvidence(BaseModel):
    tag: str | None
    role: str | None
    accessible_name: str | None
    visible_texts: list[str]
    input_type: str | None
    autocomplete: str | None
    selector_candidates: list[str]
    pointer: RecordedPointerEvidence | None


class RecordedInputEvidence(BaseModel):
    opaque_id: str
    typed_length: int


class RecordedCredentialEvidence(BaseModel):
    credential_id: str | None
    credential_kind: CredentialKind


class RecordedActionEvidence(BaseModel):
    action_id: str
    kind: ActionKind
    timestamp_start: float
    timestamp_end: float
    url: str
    target: RecordedTargetEvidence | None
    input: RecordedInputEvidence | None
    credential: RecordedCredentialEvidence | None
    key: str | None
    duration_ms: int | None
    observed_effects: list[str]
    navigated_to: str | None
    draft_label: str | None


class RecordingIdentity(BaseModel):
    recording_attempt_id: str
    browser_session_id: str
    workflow_permanent_id: str
    started_at: float | None
    ended_at: float | None
    start_url: str | None
    end_url: str | None


class RecordingEvidencePacket(BaseModel):
    schema_version: Literal[1] = 1
    recording_id: str | None = None
    recording: RecordingIdentity
    actions: list[RecordedActionEvidence]
    deleted_action_ids: list[str]
    truncated_action_count: int = 0
    provenance: dict[str, str | int]


# CSS identifier grammar, applied to DOM id/class tokens (a machine format), not prose.
_CSS_IDENTIFIER_RE = re.compile(r"-?[A-Za-z_][\w-]*")
_REDACTED_INPUT = "[REDACTED_INPUT]"


def _redact_input_values(value: str | None, input_values: list[str]) -> str | None:
    if value is None:
        return None
    redacted = value
    variants: dict[str, bool] = {}
    for input_value in input_values:
        if not input_value:
            continue
        for variant in (input_value, quote(input_value, safe=""), quote_plus(input_value, safe="")):
            variants[variant] = variants.get(variant, False) or len(input_value) >= 3
    for variant, replace_anywhere in sorted(variants.items(), key=lambda item: len(item[0]), reverse=True):
        if replace_anywhere:
            redacted = redacted.replace(variant, _REDACTED_INPUT)
        else:
            redacted = re.sub(rf"(?<![\w.-]){re.escape(variant)}(?![\w.-])", _REDACTED_INPUT, redacted)
    return redacted


def _visible_texts(action: Action) -> list[str]:
    target = action.target
    if texts_are_labels(target.tag_name):
        return [text[:MAX_VISIBLE_TEXT_CHARS] for text in target.texts[:MAX_VISIBLE_TEXTS]]
    if isinstance(action, ActionInputText) or texts_are_typed_input(target.tag_name, target.role):
        return []
    return [text[:MAX_VISIBLE_TEXT_CHARS] for text in target.texts[:MAX_VISIBLE_TEXTS]]


def _pointer_evidence(action: Action) -> RecordedPointerEvidence | None:
    xp, yp = action.target.mouse.xp, action.target.mouse.yp
    if xp is None or yp is None or not all(math.isfinite(v) and 0 <= v <= 1 for v in (xp, yp)):
        return None
    return RecordedPointerEvidence(viewport_x_fraction=xp, viewport_y_fraction=yp)


def _target_evidence(action: Action, input_values: list[str]) -> RecordedTargetEvidence | None:
    if isinstance(action, (ActionWait, ActionUrlChange)):
        return None
    target = action.target
    selector_candidates = []
    if target.selector:
        selector_candidates.append(target.selector)
    if target.id and _CSS_IDENTIFIER_RE.fullmatch(target.id):
        selector_candidates.append(f"#{target.id}")
    for class_token in (target.class_name or "").split():
        if _CSS_IDENTIFIER_RE.fullmatch(class_token):
            selector_candidates.append(f".{class_token}")
    selector_candidates = list(dict.fromkeys(selector_candidates))
    return RecordedTargetEvidence(
        tag=target.tag_name,
        role=target.role,
        accessible_name=_redact_input_values(target.accessible_name, input_values),
        visible_texts=[
            redacted
            for text in _visible_texts(action)
            if (redacted := _redact_input_values(text, input_values)) is not None
        ],
        input_type=target.input_type,
        autocomplete=target.autocomplete,
        selector_candidates=[
            redacted
            for selector in selector_candidates
            if (redacted := _redact_input_values(selector, input_values)) is not None
        ],
        pointer=_pointer_evidence(action),
    )


def _credential_kind(action: Action, draft: RecordingDraftStep | None) -> CredentialKind | None:
    """The kind `code_first._credential_fill` would fill: the draft's binding wins, else the markup."""
    if draft is not None and draft.credential_kind is not None:
        return draft.credential_kind
    return credential_kind_for_action(action)


def _input_or_credential(
    action: ActionInputText,
    draft: RecordingDraftStep | None,
    action_id: str,
) -> tuple[RecordedInputEvidence | None, RecordedCredentialEvidence | None]:
    credential_kind = _credential_kind(action, draft)
    if credential_kind is not None:
        credential = RecordedCredentialEvidence(
            credential_id=draft.credential_id if draft is not None else None,
            credential_kind=credential_kind,
        )
        return None, credential
    return RecordedInputEvidence(opaque_id=f"{action_id}.input", typed_length=len(action.input_value)), None


def _action_evidence(
    action_id: str,
    action: Action,
    draft: RecordingDraftStep | None,
    navigated_to: str | None,
    input_values: list[str],
) -> RecordedActionEvidence:
    input_evidence: RecordedInputEvidence | None = None
    credential_evidence: RecordedCredentialEvidence | None = None
    key: str | None = None
    duration_ms: int | None = None

    if isinstance(action, ActionInputText):
        input_evidence, credential_evidence = _input_or_credential(action, draft, action_id)
    elif isinstance(action, ActionPressKey):
        key = action.key
    elif isinstance(action, ActionWait):
        duration_ms = action.duration_ms

    return RecordedActionEvidence(
        action_id=action_id,
        kind=action.kind,
        timestamp_start=action.timestamp_start,
        timestamp_end=action.timestamp_end,
        url=_redact_input_values(action.url, input_values) or "",
        target=_target_evidence(action, input_values),
        input=input_evidence,
        credential=credential_evidence,
        key=key,
        duration_ms=duration_ms,
        observed_effects=["navigation"] if navigated_to else [],
        navigated_to=_redact_input_values(navigated_to, input_values),
        draft_label=_redact_input_values(draft.label, input_values) if draft is not None else None,
    )


def _label_kept_actions(
    ordered_actions: list[Action],
    pairs: list[ActionDraftPair],
) -> tuple[list[tuple[str, Action, RecordingDraftStep | None]], list[str]]:
    """Name every recorded action, then split into the ones the draft overlay kept and deleted.

    The overlay returns a subsequence of the same action objects, so the ids stay the recording's
    own numbering and a deleted id still points at the step the user removed.
    """
    kept: list[tuple[str, Action, RecordingDraftStep | None]] = []
    deleted_action_ids: list[str] = []
    cursor = 0
    for index, action in enumerate(ordered_actions):
        action_id = f"a{index + 1:03d}"
        if cursor < len(pairs) and pairs[cursor][0] is action:
            kept.append((action_id, action, pairs[cursor][1]))
            cursor += 1
        else:
            deleted_action_ids.append(action_id)
    return kept, deleted_action_ids


def build_recording_evidence(
    actions: list[Action],
    draft_steps: list[RecordingDraftStep] | None,
    *,
    browser_session_id: str,
    workflow_permanent_id: str,
    recording_attempt_id: str,
) -> RecordingEvidencePacket:
    ordered_actions = sorted(actions, key=lambda action: action.timestamp_start)
    pairs = transfer_focus_click_credentials(apply_draft_overlay(ordered_actions, draft_steps))
    kept, deleted_action_ids = _label_kept_actions(ordered_actions, pairs)
    navigations = attribute_click_navigations(pairs)
    input_values = list(
        dict.fromkeys(action.input_value for action in ordered_actions if isinstance(action, ActionInputText))
    )

    evidence_actions: list[RecordedActionEvidence] = []
    evidence_chars = 0
    truncated_action_count = 0
    for index, (action_id, action, draft) in enumerate(kept):
        navigation_index = navigations.get(index)
        item = _action_evidence(
            action_id,
            action,
            draft,
            pairs[navigation_index][0].url if navigation_index is not None else None,
            input_values,
        )
        item_chars = len(item.model_dump_json())
        if evidence_actions and evidence_chars + item_chars > MAX_EVIDENCE_CHARS:
            truncated_action_count = len(kept) - index
            break
        evidence_actions.append(item)
        evidence_chars += item_chars

    if truncated_action_count:
        LOG.info(
            "record_browser.evidence_truncated",
            browser_session_id=browser_session_id,
            recording_attempt_id=recording_attempt_id,
            truncated_action_count=truncated_action_count,
        )

    return RecordingEvidencePacket(
        recording=RecordingIdentity(
            recording_attempt_id=recording_attempt_id,
            browser_session_id=browser_session_id,
            workflow_permanent_id=workflow_permanent_id,
            started_at=ordered_actions[0].timestamp_start if ordered_actions else None,
            ended_at=ordered_actions[-1].timestamp_end if ordered_actions else None,
            start_url=_redact_input_values(ordered_actions[0].url, input_values) if ordered_actions else None,
            end_url=_redact_input_values(ordered_actions[-1].url, input_values) if ordered_actions else None,
        ),
        actions=evidence_actions,
        deleted_action_ids=deleted_action_ids,
        truncated_action_count=truncated_action_count,
        provenance={
            "source": "browser_recording",
            "browser_session_id": browser_session_id,
            "action_count": len(ordered_actions),
        },
    )
