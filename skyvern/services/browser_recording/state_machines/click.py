import typing as t

import structlog

from skyvern.services.browser_recording.types import (
    Action,
    ActionClick,
    ActionKind,
    ActionTarget,
    EventTarget,
    ExfiltratedEvent,
    Mouse,
    MousePosition,
)

from .state_machine import StateMachine

LOG = structlog.get_logger()


class StateMachineClick(StateMachine):
    state: t.Literal["void"] = "void"
    target: EventTarget | None = None
    timestamp: float | None = None
    mouse: MousePosition | None = None
    url: str | None = None

    def __init__(self) -> None:
        self.reset()

    def tick(self, event: ExfiltratedEvent, current_actions: list[Action]) -> ActionClick | None:
        if event.source != "console":
            return None

        if event.params.type != "click":
            if event.params.mousePosition:
                if event.params.mousePosition.xp is not None and event.params.mousePosition.yp is not None:
                    self.mouse = event.params.mousePosition
            return None

        LOG.debug(f"~ click detected [{event.params.target.skyId or event.params.target.id}]")

        self.target = event.params.target
        self.timestamp = event.params.timestamp
        self.url = event.params.url

        if event.params.mousePosition:
            self.mouse = event.params.mousePosition

        return self.emit(event)

    def emit(self, event: ExfiltratedEvent) -> ActionClick | None:
        if not self.target:
            LOG.debug("~ cannot emit click, missing target; resetting")
            self.reset()
            return None

        xp = (self.mouse.xp or -1) if self.mouse else None
        yp = (self.mouse.yp or -1) if self.mouse else None

        LOG.debug("~ emitting click action", exfiltrated_event=event)

        action_target = ActionTarget(
            class_name=self.target.className,
            id=self.target.id,
            mouse=Mouse(xp=xp, yp=yp),
            sky_id=self.target.skyId,
            tag_name=self.target.tagName,
            texts=self.target.text,
        )

        action = ActionClick(
            kind=ActionKind.CLICK.value,
            target=action_target,
            timestamp_start=self.timestamp,
            timestamp_end=self.timestamp,
            url=self.url,
        )

        self.reset()

        return action

    def reset(self) -> None:
        self.state = "void"
        self.target = None
        self.timestamp = None
        self.mouse = None
        self.url = None
