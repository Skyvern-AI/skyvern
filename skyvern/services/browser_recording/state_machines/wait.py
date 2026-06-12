import collections
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

PAGE_ACTIVITY_EVENT_NAME = "net:activity"
PAGE_ACTIVITY_EVENT_NAME_PREFIX = "nav:"

MAX_PAGE_ACTIVITY_TIMESTAMPS = 256


class StateMachineWait(StateMachine):
    state: t.Literal["void"] = "void"
    last_event_timestamp: float | None = None
    threshold_ms: int = ActionWait.MIN_DURATION_THRESHOLD_MS

    def __init__(self, threshold_ms: int | None = None) -> None:
        self.threshold_ms = max(
            threshold_ms or ActionWait.MIN_DURATION_THRESHOLD_MS,
            ActionWait.MIN_DURATION_THRESHOLD_MS,
        )

        self.page_activity_timestamps_ms: collections.deque[float] = collections.deque(
            maxlen=MAX_PAGE_ACTIVITY_TIMESTAMPS
        )

        self.reset()

    def tick(self, event: ExfiltratedEvent, current_actions: list[Action]) -> ActionWait | None:
        if event.source == "cdp":
            if event.event_name == PAGE_ACTIVITY_EVENT_NAME or event.event_name.startswith(
                PAGE_ACTIVITY_EVENT_NAME_PREFIX
            ):
                self.page_activity_timestamps_ms.append(event.timestamp * 1000.0)
            return None

        if event.source != "console":
            return None

        if self.last_event_timestamp is not None:
            duration_ms = int(event.params.timestamp - self.last_event_timestamp)

            if duration_ms >= self.threshold_ms:
                if not self._page_was_active(self.last_event_timestamp, event.params.timestamp):
                    LOG.debug(
                        "~ suppressing wait action: no page activity during idle gap",
                        duration_ms=duration_ms,
                    )
                else:
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

    def _page_was_active(self, gap_start_ms: float, gap_end_ms: float) -> bool:
        while self.page_activity_timestamps_ms and self.page_activity_timestamps_ms[0] < gap_start_ms:
            self.page_activity_timestamps_ms.popleft()

        return any(timestamp <= gap_end_ms for timestamp in self.page_activity_timestamps_ms)

    def on_action(self, action: Action, current_actions: list[Action]) -> bool:
        if action.kind == ActionKind.HOVER:
            return True

        self.reset()

        return True

    def reset(self) -> None:
        self.state = "void"
        self.last_event_timestamp = None
