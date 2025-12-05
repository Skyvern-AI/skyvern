from skyvern.services.browser_recording.types import (
    Action,
    ExfiltratedEvent,
)


class StateMachine:
    """
    A minimal, concrete StateMachineProtocol.
    """

    state: str

    def tick(self, event: ExfiltratedEvent, current_actions: list[Action]) -> Action | None:
        return None

    def on_action(self, action: Action, current_actions: list[Action]) -> bool:
        """
        Optional callback when an action is emitted by _any_ state machine.
        Default is that a state machine resets.

        Return `True` (the default) to allow the action to proceed; return `False`
        to veto the action.
        """
        self.reset()

        return True

    def reset(self) -> None:
        pass
