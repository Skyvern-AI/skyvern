import typing as t

import structlog

from skyvern.services.browser_recording.types import (
    Action,
    ActionKind,
    ActionTarget,
    ActionWait,
    ExfiltratedEvent,
    Mouse,
)

from .state_machine import StateMachine

LOG = structlog.get_logger()


class StateMachineWait(StateMachine):
    state: t.Literal["void"] = "void"
    last_event_timestamp: float | None = None
    threshold_ms: int = ActionWait.MIN_DURATION_THRESHOLD_MS

    def __init__(self, threshold_ms: int | None = None) -> None:
        self.threshold_ms = max(
            threshold_ms or ActionWait.MIN_DURATION_THRESHOLD_MS,
            ActionWait.MIN_DURATION_THRESHOLD_MS,
        )

        self.reset()

    def tick(self, event: ExfiltratedEvent, current_actions: list[Action]) -> ActionWait | None:
        if event.source != "console":
            return None

        if self.last_event_timestamp is not None:
            duration_ms = int(event.params.timestamp - self.last_event_timestamp)

            if duration_ms >= self.threshold_ms:
                LOG.debug("~ emitting wait action", duration_ms=duration_ms)

                action_target = ActionTarget(
                    class_name=None,
                    id=None,
                    mouse=Mouse(xp=None, yp=None),
                    sky_id=None,
                    tag_name=None,
                    texts=[],
                )

                action = ActionWait(
                    kind=ActionKind.WAIT.value,
                    target=action_target,
                    timestamp_start=self.last_event_timestamp,
                    timestamp_end=event.params.timestamp,
                    url=event.params.url,
                    duration_ms=duration_ms,
                )

                self.reset()

                return action

        self.last_event_timestamp = event.params.timestamp

        return None

    def on_action(self, action: Action, current_actions: list[Action]) -> bool:
        if action.kind == ActionKind.HOVER:
            return True

        self.reset()

        return True

    def reset(self) -> None:
        self.state = "void"
        self.last_event_timestamp = None
