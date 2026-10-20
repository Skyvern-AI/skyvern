import typing as t
from urllib.parse import urlsplit

import structlog

from skyvern.services.browser_recording.types import (
    CLICK_NAVIGATION_WINDOW_MS,
    Action,
    ActionKind,
    ActionTarget,
    ActionUrlChange,
    ExfiltratedEvent,
    Mouse,
)
from skyvern.utils.url_validators import redacted_url_origin

from .state_machine import StateMachine

LOG = structlog.get_logger()


class StateMachineUrlChange(StateMachine):
    state: t.Literal["void"] = "void"
    last_url: str | None = None
    pending_page_requests: dict[str, tuple[str, float]]
    opened_target_ids: set[str]
    target_first_seen: dict[str, float]
    unattributed_targets: dict[str, str]

    def __init__(self) -> None:
        self.reset()

    def tick(self, event: ExfiltratedEvent, current_actions: list[Action]) -> ActionUrlChange | None:
        if event.source != "cdp":
            return None

        if event.event_name in ("target_created", "target_info_changed"):
            target_info = event.params.targetInfo
            if (
                target_info
                and target_info.type == "page"
                and target_info.openerId
                and target_info.targetId
                and target_info.targetId not in self.opened_target_ids
            ):
                # The window runs from when the tab first appeared, so a later update to a tab no click opened
                # cannot pin its URL on an unrelated interaction.
                first_seen = self.target_first_seen.setdefault(target_info.targetId, event.timestamp)
                if target_info.url and urlsplit(target_info.url).scheme in {"http", "https"}:
                    if self._attribute_navigation(current_actions, first_seen, target_info.url):
                        self.opened_target_ids.add(target_info.targetId)
                        self.unattributed_targets.pop(target_info.targetId, None)
                    else:
                        self.unattributed_targets[target_info.targetId] = target_info.url
            return None

        if not event.event_name.startswith("nav:"):
            return None

        if event.event_name == "nav:frame_requested_navigation":
            frame_id = event.params.frameId
            if frame_id and event.params.url:
                self.pending_page_requests[frame_id] = (event.params.url, event.timestamp)

        if event.event_name == "nav:frame_started_navigating":
            frame_id = event.params.frameId
            url = event.params.url

            pending_page_request = self.pending_page_requests.get(frame_id) if frame_id else None
            if frame_id and pending_page_request and pending_page_request[0] != url:
                self.pending_page_requests.pop(frame_id)

            if url == self.last_url:
                LOG.debug("~ ignoring navigation to same URL", url=url)
                return None
        else:
            if event.params.frame:
                if event.event_name == "nav:frame_navigated" and event.params.frame.id:
                    pending_page_request = self.pending_page_requests.pop(event.params.frame.id, None)
                    if pending_page_request and not event.params.frame.parentId and event.params.frame.url:
                        self._attribute_navigation(current_actions, pending_page_request[1], event.params.frame.url)
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

        # CDP events carry server time.time() seconds; every other action uses the page's Date.now() ms.
        timestamp_ms = event.timestamp * 1000

        return ActionUrlChange(
            kind=ActionKind.URL_CHANGE.value,
            target=action_target,
            timestamp_start=timestamp_ms,
            timestamp_end=timestamp_ms,
            url=url,
        )

    def _attribute_navigation(self, current_actions: list[Action], request_timestamp: float, url: str) -> bool:
        # CDP events use server time.time() seconds; interactions use browser Date.now() milliseconds.
        request_timestamp_ms = request_timestamp * 1000
        interactions = (
            action
            for action in current_actions
            if action.kind in (ActionKind.CLICK, ActionKind.INPUT_TEXT, ActionKind.PRESS_KEY)
            and abs(action.timestamp_end - request_timestamp_ms) <= CLICK_NAVIGATION_WINDOW_MS
        )
        action = min(
            interactions, key=lambda candidate: abs(candidate.timestamp_end - request_timestamp_ms), default=None
        )
        if action is None:
            return False
        # Origin only, like durable recording evidence: paths and queries can carry reset links and one-time codes.
        action.navigated_to = redacted_url_origin(url)
        return True

    def on_action(self, action: Action, current_actions: list[Action]) -> bool:
        # A click recovered late from the page queue can arrive after the only target event for the tab it opened.
        for target_id, url in list(self.unattributed_targets.items()):
            if self._attribute_navigation([action], self.target_first_seen[target_id], url):
                self.opened_target_ids.add(target_id)
                del self.unattributed_targets[target_id]

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
        self.pending_page_requests = {}
        self.opened_target_ids = set()
        self.target_first_seen = {}
        self.unattributed_targets = {}
