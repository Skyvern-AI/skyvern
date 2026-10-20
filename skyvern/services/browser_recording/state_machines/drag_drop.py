import typing as t

from skyvern.services.browser_recording.types import (
    Action,
    ActionDragDrop,
    ActionKind,
    ActionTarget,
    EventTarget,
    ExfiltratedEvent,
    Mouse,
    MousePosition,
)

from .state_machine import StateMachine


def _action_target(target: EventTarget, mouse: MousePosition | None) -> ActionTarget:
    return ActionTarget(
        class_name=target.className,
        id=target.id,
        mouse=Mouse(
            xp=mouse.xp if mouse else None,
            yp=mouse.yp if mouse else None,
        ),
        sky_id=target.skyId,
        tag_name=target.tagName,
        texts=target.text,
        selector=target.selector,
        role=target.role,
        accessible_name=target.accessibleName,
        input_type=target.inputType,
        autocomplete=target.autocomplete,
        in_child_frame=target.inChildFrame,
    )


class StateMachineDragDrop(StateMachine):
    state: t.Literal["dragstart", "drop"] = "dragstart"
    source: EventTarget | None = None
    source_mouse: MousePosition | None = None
    timestamp_start: float | None = None

    def __init__(self) -> None:
        self.reset()

    def tick(self, event: ExfiltratedEvent, current_actions: list[Action]) -> ActionDragDrop | None:
        if event.source != "console":
            return None

        if event.params.type == "dragstart":
            self.source = event.params.target
            self.source_mouse = event.params.mousePosition
            self.timestamp_start = event.params.timestamp
            self.state = "drop"
            return None

        if self.state != "drop":
            return None

        if event.params.type == "dragend":
            self.reset()
            return None
        if event.params.type != "drop" or self.source is None:
            return None

        action = ActionDragDrop(
            kind=ActionKind.DRAG_DROP,
            source=_action_target(self.source, self.source_mouse),
            target=_action_target(event.params.target, event.params.mousePosition),
            timestamp_start=self.timestamp_start or event.params.timestamp,
            timestamp_end=event.params.timestamp,
            url=event.params.url,
        )
        self.reset()
        return action

    def on_action(self, action: Action, current_actions: list[Action]) -> bool:
        if self.state == "drop":
            return True
        return super().on_action(action, current_actions)

    def reset(self) -> None:
        self.state = "dragstart"
        self.source = None
        self.source_mouse = None
        self.timestamp_start = None
