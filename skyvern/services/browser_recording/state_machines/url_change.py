import typing as t

import structlog

from skyvern.services.browser_recording.types import (
    Action,
    ActionKind,
    ActionTarget,
    ActionUrlChange,
    ExfiltratedEvent,
    Mouse,
)

from .state_machine import StateMachine

LOG = structlog.get_logger()


class StateMachineUrlChange(StateMachine):
    state: t.Literal["void"] = "void"
    last_url: str | None = None

    def __init__(self) -> None:
        self.reset()

    def tick(self, event: ExfiltratedEvent, current_actions: list[Action]) -> ActionUrlChange | None:
        if event.source != "cdp":
            return None

        if not event.event_name.startswith("nav:"):
            return None

        if event.event_name == "nav:frame_started_navigating":
            url = event.params.url

            if url == self.last_url:
                LOG.debug("~ ignoring navigation to same URL", url=url)
                return None
        else:
            if event.params.frame:
                self.last_url = event.params.frame.url
            elif event.params.url:
                self.last_url = event.params.url

            return None

        if not url:
            return None

        self.last_url = url

        LOG.debug("~ emitting URL change action", url=url)

        action_target = ActionTarget(
            class_name=None,
            id=None,
            mouse=Mouse(xp=None, yp=None),
            sky_id=None,
            tag_name=None,
            texts=[],
        )

        return ActionUrlChange(
            kind=ActionKind.URL_CHANGE.value,
            target=action_target,
            timestamp_start=event.timestamp,
            timestamp_end=event.timestamp,
            url=url,
        )

    def on_action(self, action: Action, current_actions: list[Action]) -> bool:
        if action.kind != ActionKind.URL_CHANGE:
            return True

        if not current_actions:
            return True

        last_action = current_actions[-1]

        if last_action.kind != ActionKind.URL_CHANGE:
            return True

        if last_action.url == action.url:
            LOG.debug("~ vetoing duplicate URL change action", url=action.url)
            return False

        return True

    def reset(self) -> None:
        self.state = "void"
        self.last_url = None
