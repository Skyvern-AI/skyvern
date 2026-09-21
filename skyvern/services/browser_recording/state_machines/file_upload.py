import structlog

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


class StateMachineFileUpload(StateMachine):
    def tick(self, event: ExfiltratedEvent, current_actions: list[Action]) -> ActionInputText | None:
        if event.source != "console" or event.params.type != "change":
            return None
        target = event.params.target
        if (target.tagName or "").upper() != "INPUT" or (target.inputType or "").lower() != "file":
            return None
        if not target.value:
            return None

        LOG.debug(f"~ emitting file upload action [{target.skyId or target.id}]")
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
            input_value="",
        )
