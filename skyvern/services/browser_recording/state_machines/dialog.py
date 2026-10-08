import typing as t

from skyvern.services.browser_recording.redact import is_secret_field
from skyvern.services.browser_recording.types import (
    Action,
    ActionDialog,
    ActionKind,
    ActionTarget,
    ExfiltratedEvent,
    Mouse,
)

from .state_machine import StateMachine


class StateMachineDialog(StateMachine):
    state: t.Literal["opening", "closed"] = "opening"
    dialog_type: str | None = None
    message: str | None = None
    timestamp_start: float | None = None
    url: str | None = None

    def __init__(self) -> None:
        self.reset()

    def tick(self, event: ExfiltratedEvent, current_actions: list[Action]) -> ActionDialog | None:
        if event.source != "cdp":
            return None

        if event.event_name == "dialog:opening":
            self.dialog_type = event.params.type
            self.message = event.params.message
            self.timestamp_start = event.timestamp * 1000
            self.url = event.params.url
            self.state = "closed"
            return None

        if event.event_name != "dialog:closed" or self.state != "closed":
            return None

        prompt_text = event.params.userInput if self.dialog_type == "prompt" and event.params.result else None
        prompt_text_redacted = bool(
            prompt_text
            and is_secret_field(
                None,
                None,
                accessible_name=self.message,
            )
        )
        if prompt_text_redacted:
            prompt_text = None

        timestamp_end = event.timestamp * 1000
        action = ActionDialog(
            kind=ActionKind.DIALOG,
            target=ActionTarget(mouse=Mouse()),
            timestamp_start=self.timestamp_start or timestamp_end,
            timestamp_end=timestamp_end,
            url=self.url or "",
            dialog_type=self.dialog_type or "unknown",
            response="accept" if event.params.result else "dismiss",
            prompt_text=prompt_text,
            prompt_text_redacted=prompt_text_redacted,
        )
        self.reset()
        return action

    def on_action(self, action: Action, current_actions: list[Action]) -> bool:
        if self.state == "closed":
            return True
        return super().on_action(action, current_actions)

    def reset(self) -> None:
        self.state = "opening"
        self.dialog_type = None
        self.message = None
        self.timestamp_start = None
        self.url = None
