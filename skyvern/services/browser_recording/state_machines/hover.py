import typing as t

import structlog

from skyvern.services.browser_recording.types import (
    Action,
    ActionHover,
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


class StateMachineHover(StateMachine):
    state: t.Literal["mouseenter", "next-event"] = "mouseenter"
    target: EventTarget | None = None
    timestamp_start: float | None = None
    timestamp_end: float | None = None
    mouse: MousePosition | None = None
    # --
    threshold_ms: int = ActionHover.DURATION_THRESHOLD_MS

    def __init__(self, threshold_ms: int | None = None) -> None:
        self.threshold_ms = max(
            threshold_ms or ActionHover.DURATION_THRESHOLD_MS,
            ActionHover.MIN_DURATION_THRESHOLD_MS,
        )

        self.reset()

    def tick(self, event: ExfiltratedEvent, current_actions: list[Action]) -> ActionHover | None:
        if event.source != "console":
            return None

        match self.state:
            case "mouseenter":
                if event.params.mousePosition:
                    if event.params.mousePosition.xp is not None and event.params.mousePosition.yp is not None:
                        self.mouse = event.params.mousePosition

                if event.params.type != "mouseenter":
                    return None

                text = ",".join(event.params.target.text or [])
                LOG.debug(
                    f"~ hover: mouseenter detected [{event.params.target.skyId or event.params.target.id}] [{text}]"
                )

                self.target = event.params.target
                self.state = "next-event"
                self.timestamp_start = event.params.timestamp

            case "next-event":
                if event.params.type == "mouseenter":
                    if target_has_changed(self.target, event.params.target):
                        self.timestamp_start = event.params.timestamp
                        self.target = event.params.target
                        return None

                event_end = event.params.timestamp

                if not self.timestamp_start:
                    LOG.debug("~ missing hover start timestamp, resetting")
                    self.reset()

                    return None

                duration_ms = int(event_end - self.timestamp_start)

                if event.params.type == "mousemove":
                    target = event.params.target

                    if not target_has_changed(self.target, target):
                        return None

                if duration_ms < self.threshold_ms:
                    LOG.debug(f"~ hover duration {duration_ms}ms below threshold {self.threshold_ms}ms, resetting")
                    self.reset()

                    return None

                self.timestamp_end = event.params.timestamp
                LOG.debug(
                    f"~ hover duration {duration_ms}ms meets threshold {self.threshold_ms}ms, emitting",
                    start=self.timestamp_start,
                    end=self.timestamp_end,
                )

                # dedupe consecutive hover actions on same target
                if current_actions:
                    last_action = current_actions[-1]

                    if last_action.kind == ActionKind.HOVER and self.target:
                        target_id = self.target.skyId or self.target.id
                        last_action_target_id = last_action.target.sky_id or last_action.target.id

                        if target_id:
                            if target_id == last_action_target_id:
                                LOG.debug("~ hover: duplicate hover action - skipping", target=self.target)
                                self.reset()

                                return None

                return self.emit(event)

        return None

    def emit(self, event: ExfiltratedEvent) -> ActionHover | None:
        if not self.target:
            LOG.debug("~ cannot emit hover, missing target; resetting")
            self.reset()

            return None

        xp = (self.mouse.xp or -1) if self.mouse else None
        yp = (self.mouse.yp or -1) if self.mouse else None

        LOG.debug("~ emitting hover action", exfiltrated_event=event)

        action_target = ActionTarget(
            class_name=self.target.className,
            id=self.target.id,
            mouse=Mouse(xp=xp, yp=yp),
            sky_id=self.target.skyId,
            tag_name=self.target.tagName,
            texts=self.target.text,
        )

        action = ActionHover(
            kind=ActionKind.HOVER.value,
            target=action_target,
            timestamp_start=self.timestamp_start or -1,
            timestamp_end=self.timestamp_end or -1,
            url=event.params.url,
        )

        self.reset()

        return action

    def reset(self) -> None:
        self.state = "mouseenter"
        self.target = None
        self.timestamp_start = None
        self.timestamp_end = None
        self.mouse = None
