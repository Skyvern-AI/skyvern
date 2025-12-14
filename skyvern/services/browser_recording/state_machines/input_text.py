import typing as t

import structlog

from skyvern.services.browser_recording.types import (
    Action,
    ActionInputText,
    ActionKind,
    ActionTarget,
    EventTarget,
    ExfiltratedEvent,
    Mouse,
    MousePosition,
    target_has_changed,
)

from .state_machine import StateMachine

LOG = structlog.get_logger()


class StateMachineInputText(StateMachine):
    state: t.Literal["focus", "keydown", "blur"] = "focus"
    target: EventTarget | None = None
    timestamp_start: float | None = None
    mouse: MousePosition | None = None

    def __init__(self) -> None:
        self.reset()

    def tick(self, event: ExfiltratedEvent, current_actions: list[Action]) -> ActionInputText | None:
        if event.source != "console":
            return None

        match self.state:
            case "focus":
                if event.params.type != "focus":
                    if event.params.mousePosition:
                        if event.params.mousePosition.xp is not None and event.params.mousePosition.yp is not None:
                            self.mouse = event.params.mousePosition

                    return None

                LOG.debug(f"~ focus detected [{event.params.target.skyId or event.params.target.id}]")

                if event.params.type == "focus":
                    self.target = event.params.target
                    self.state = "keydown"
                    self.timestamp_start = event.params.timestamp

            case "keydown":
                if event.params.type != "keydown":
                    return None

                if self.update_target(event.params.target):
                    return None

                LOG.debug(f"~ initial keydown detected '{event.params.key}'")

                self.state = "blur"
            case "blur":
                if event.params.type == "keydown":
                    LOG.debug(f"~ input-text: subsequent keydown detected '{event.params.key}'")

                    if event.params.key == "Enter":
                        return self.emit(event)

                    return None

                if event.params.type != "blur":
                    return None

                if self.update_target(event.params.target):
                    return None

                LOG.debug("~ blur detected")

                return self.emit(event)

        return None

    def update_target(self, event_target: EventTarget) -> bool:
        if target_has_changed(self.target, event_target):
            self.target = event_target

            LOG.debug("~ input-text target changed, resetting state machine")

            self.reset()

            return True

        return False

    def emit(self, event: ExfiltratedEvent) -> ActionInputText | None:
        if not self.target:
            LOG.debug("~ cannot emit, missing target or mouse; resetting")

            self.reset()
            return None

        xp = (self.mouse.xp or -1) if self.mouse else None
        yp = (self.mouse.yp or -1) if self.mouse else None

        LOG.debug("~ emitting input text action", exfiltrated_event=event)

        input_value = event.params.target.value

        if input_value is None:
            LOG.debug("~ cannot emit, missing input value; resetting")

            self.reset()
            return None

        action_target = ActionTarget(
            class_name=self.target.className,
            id=self.target.id,
            mouse=Mouse(xp=xp, yp=yp),
            sky_id=self.target.skyId,
            tag_name=self.target.tagName,
            texts=self.target.text,
        )

        action = ActionInputText(
            kind=ActionKind.INPUT_TEXT.value,
            target=action_target,
            timestamp_start=self.timestamp_start,
            timestamp_end=event.params.timestamp,
            url=event.params.url,
            input_value=str(input_value),
        )

        self.reset()

        return action

    def on_action(self, action: Action, current_actions: list[Action]) -> bool:
        if action.kind == ActionKind.CLICK:
            # NOTE(jdo): skipping self.reset here; a focus event on an element can often be followed by a
            # click event, and the identity doesn't always match due to nesting. I think a more precise
            # check would be to:
            #  - check identity match for click and focus; if not matching:
            #   - ask the browser via cdp if there is a nesting relation between the two elements
            #   - if yes, allow and carry on, otherwise reset
            # That's another round trip. It's likely pretty fast, tho. For now, we'll just assume a click does
            # not invalidate the state of the input text state machine.
            return True

        self.reset()

        return True

    def reset(self) -> None:
        self.state = "focus"
        self.target = None
        self.mouse = None
        self.timestamp_start = None
