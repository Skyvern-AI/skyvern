import structlog

from skyvern.services.browser_recording.redact import is_secret_field
from skyvern.services.browser_recording.types import (
    Action,
    ActionInputText,
    ActionKind,
    ActionTarget,
    ExfiltratedEvent,
    Mouse,
)

from .state_machine import StateMachine

LOG = structlog.get_logger()


class StateMachineSelect(StateMachine):
    """Emits the option chosen from a native <select>.

    The input-text machine needs a keydown to leave its focus state, so it only ever saw a
    select driven by keyboard; one driven by mouse lost its value. Emitting ActionInputText
    rather than a new kind is deliberate: code_first already routes a SELECT-tagged
    input_text to select_option, so nothing downstream had to change.
    """

    def tick(self, event: ExfiltratedEvent, current_actions: list[Action]) -> ActionInputText | None:
        if event.source != "console":
            return None

        if event.params.type != "change":
            return None

        target = event.params.target

        # change also fires for text inputs, checkboxes and radios. Those are already covered by
        # the input-text and click machines, and emitting here would reset the input-text machine
        # mid-fill and lose the typed value.
        if (target.tagName or "").upper() != "SELECT":
            return None

        value = target.value
        # A card-expiry month is usually a <select autocomplete="cc-exp-month">, so a select can
        # classify as secret and reach here with its value already nulled by ingest redaction.
        # Keep the step blank rather than dropping it, as StateMachineInputText does, so the
        # legacy path still shows the field; code-first drops an empty select_option regardless.
        secret = is_secret_field(
            target.inputType,
            target.autocomplete,
            field_id=target.id,
            accessible_name=target.accessibleName,
            texts=target.text,
            tag_name=target.tagName,
        )

        # An empty value is the placeholder row ("-- choose --"); the synthesizer drops a
        # select_option with no value, so emitting one would only add a dead step.
        if not secret and (value is None or str(value) == ""):
            return None

        LOG.debug(f"~ emitting select action [{target.skyId or target.id}]")

        return ActionInputText(
            kind=ActionKind.INPUT_TEXT.value,
            target=ActionTarget(
                class_name=target.className,
                id=target.id,
                mouse=Mouse(xp=None, yp=None),
                sky_id=target.skyId,
                tag_name=target.tagName,
                texts=target.text,
                selector=target.selector,
                role=target.role,
                accessible_name=target.accessibleName,
                input_type=target.inputType,
                autocomplete=target.autocomplete,
            ),
            timestamp_start=event.params.timestamp,
            timestamp_end=event.params.timestamp,
            url=event.params.url,
            input_value="" if secret else str(value),
        )
