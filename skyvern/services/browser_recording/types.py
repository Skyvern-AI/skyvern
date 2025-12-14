"""
A types module for browser recording actions and events.
"""

import enum
import typing as t
from typing import Literal

from pydantic import BaseModel

from skyvern.client.types.workflow_definition_yaml_blocks_item import (
    WorkflowDefinitionYamlBlocksItem_Action,
    WorkflowDefinitionYamlBlocksItem_GotoUrl,
    WorkflowDefinitionYamlBlocksItem_Wait,
)


class ActionKind(enum.StrEnum):
    CLICK = "click"
    HOVER = "hover"
    INPUT_TEXT = "input_text"
    URL_CHANGE = "url_change"
    WAIT = "wait"


class ActionBase(BaseModel):
    kind: ActionKind
    # --
    target: "ActionTarget"
    timestamp_start: float
    timestamp_end: float
    url: str


class ActionClick(ActionBase):
    kind: t.Literal[ActionKind.CLICK]


class ActionHover(ActionBase):
    kind: t.Literal[ActionKind.HOVER]
    # --
    DURATION_THRESHOLD_MS: t.ClassVar[int] = 2000
    MIN_DURATION_THRESHOLD_MS: t.ClassVar[int] = 1000


class ActionInputText(ActionBase):
    kind: t.Literal[ActionKind.INPUT_TEXT]
    # --
    input_value: str


class ActionUrlChange(ActionBase):
    kind: t.Literal[ActionKind.URL_CHANGE]


class ActionWait(ActionBase):
    kind: t.Literal[ActionKind.WAIT]
    # --
    duration_ms: int
    MIN_DURATION_THRESHOLD_MS: t.ClassVar[int] = 5000


Action = ActionClick | ActionHover | ActionInputText | ActionUrlChange | ActionWait

ActionBlockable = ActionClick | ActionHover | ActionInputText


class ActionTarget(BaseModel):
    class_name: str | None = None
    id: str | None = None
    mouse: "Mouse"
    sky_id: str | None = None
    tag_name: str | None = None
    texts: list[str] = []


class Mouse(BaseModel):
    xp: float | None = None
    """
    0 to 1.0 inclusive, percentage across the viewport
    """
    yp: float | None = None
    """
    0 to 1.0 inclusive, percentage down the viewport
    """


OutputBlock = t.Union[
    WorkflowDefinitionYamlBlocksItem_Action,
    WorkflowDefinitionYamlBlocksItem_GotoUrl,
    WorkflowDefinitionYamlBlocksItem_Wait,
]


class TargetInfo(BaseModel):
    attached: bool | None = None
    browserContextId: str | None = None
    canAccessOpener: bool | None = None
    targetId: str | None = None
    title: str | None = None
    type: str | None = None
    url: str | None = None


class CdpEventFrame(BaseModel):
    url: str | None = None

    class Config:
        extra = "allow"


class ExfiltratedEventCdpParams(BaseModel):
    # target_info_changed events
    targetInfo: TargetInfo | None = None

    # frame_requested_navigation events
    disposition: str | None = None
    frameId: str | None = None
    reason: str | None = None
    url: str | None = None

    # frame_navigated events
    frame: CdpEventFrame | None = None


class EventTarget(BaseModel):
    className: str | None = None
    id: str | None = None
    isHtml: bool = False
    isSvg: bool = False
    innerText: str | None = None
    skyId: str | None = None
    tagName: str | None = None
    text: list[str] = []
    value: str | int | None = None


class MousePosition(BaseModel):
    xa: float | None = None
    ya: float | None = None
    xp: float | None = None
    yp: float | None = None


class BoundingRect(BaseModel):
    bottom: float
    height: float
    left: float
    right: float
    top: float
    width: float
    x: float
    y: float


class Scroll(BaseModel):
    clientHeight: float
    clientWidth: float
    scrollHeight: float
    scrollLeft: float
    scrollTop: float
    scrollWidth: float


class ActiveElement(BaseModel):
    boundingRect: BoundingRect | None = None
    className: str | None = None
    id: str | None = None
    scroll: Scroll | None = None
    tagName: str | None = None


class Window(BaseModel):
    height: float
    scrollX: float
    scrollY: float
    width: float


class ExfiltratedEventConsoleParams(BaseModel):
    activeElement: ActiveElement
    code: str | None = None
    inputValue: str | None = None
    key: str | None = None
    mousePosition: MousePosition
    target: EventTarget
    timestamp: float
    type: str
    url: str
    window: Window


class ExfiltratedCdpEvent(BaseModel):
    kind: Literal["exfiltrated-event"]
    event_name: str
    params: ExfiltratedEventCdpParams
    source: Literal["cdp"]
    timestamp: float


class ExfiltratedConsoleEvent(BaseModel):
    kind: Literal["exfiltrated-event"]
    event_name: str
    params: ExfiltratedEventConsoleParams
    source: Literal["console"]
    timestamp: float


ExfiltratedEvent = ExfiltratedCdpEvent | ExfiltratedConsoleEvent


class StateMachineProtocol(t.Protocol):
    state: str

    def tick(self, event: ExfiltratedEvent, current_actions: list[Action]) -> Action | None: ...

    def on_action(self, action: Action, current_actions: list[Action]) -> bool: ...

    def reset(self) -> None: ...


# -- guards, predicates, etc.


def target_has_changed(current_target: EventTarget | None, event_target: EventTarget) -> bool:
    if not current_target:
        return False

    if not event_target.skyId and not event_target.id:
        return True  # sic: we cannot compare, so assume changed

    if not current_target.skyId and not current_target.id:
        return True  # sic: we cannot compare, so assume changed

    if current_target.id and event_target.id:
        if current_target.id != event_target.id:
            return True

    if current_target.skyId and event_target.skyId:
        if current_target.skyId != event_target.skyId:
            return True

    return False
