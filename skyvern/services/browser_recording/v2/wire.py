from skyvern.services.browser_recording.types import (
    ActionKind,
    RecordingDraftStep,
    RecordingDraftStepEditableField,
)
from skyvern.services.browser_recording.v2.session import StepV2

# The panel renders v1 draft steps, so v2 rides the same wire shape. Kinds the panel
# cannot render (download, upload, dialog) stay in the ledger until PR-9 gives them UI.
_V2_ACTION_KINDS: dict[str, ActionKind] = {
    "click": ActionKind.CLICK,
    "type_text": ActionKind.INPUT_TEXT,
    "press_key": ActionKind.CLICK,
    "goto_url": ActionKind.URL_CHANGE,
}


def draft_steps_from_v2(steps: list[StepV2], epoch_offset_ms: float) -> list[RecordingDraftStep]:
    draft_steps = []
    for step in steps:
        action_kind = _V2_ACTION_KINDS.get(step.kind)
        if action_kind is None:
            continue
        is_goto = step.kind == "goto_url"
        draft_steps.append(
            RecordingDraftStep(
                step_id=step.step_id,
                action_kind=action_kind,
                block_type="goto_url" if is_goto else "action",
                label=step.title,
                title=step.title,
                navigation_goal=None if is_goto else step.title,
                url=step.url,
                editable_fields=[RecordingDraftStepEditableField.TITLE]
                + ([RecordingDraftStepEditableField.URL] if is_goto else []),
                timestamp_start=step.t_start * 1000 + epoch_offset_ms,
                timestamp_end=step.t_end * 1000 + epoch_offset_ms,
            )
        )
    return draft_steps
