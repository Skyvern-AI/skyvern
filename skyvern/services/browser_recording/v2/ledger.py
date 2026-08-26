from collections import deque
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field, replace
from typing import Literal

from skyvern.services.browser_recording.v2.resolver import stop_resolver

GestureKind = Literal[
    "mouse_pressed",
    "mouse_released",
    "mouse_moved",
    "wheel",
    "key",
    "paste",
    "navigate",
    "go_back",
    "go_forward",
    "reload",
]
EffectKind = Literal["navigation", "network_settle", "download", "file_chooser", "dialog", "target"]


@dataclass(slots=True, repr=False)
class Gesture:
    seq: int
    t_received: float
    kind: GestureKind
    page_key: str
    url: str
    x: int | None = None
    y: int | None = None
    button: str | None = None
    click_count: int | None = None
    modifiers: int = 0
    key: str | None = None
    code: str | None = None
    text: str | None = None
    windows_virtual_key_code: int | None = None
    key_event_type: str | None = None
    delta_x: int | None = None
    delta_y: int | None = None
    target_url: str | None = None
    target_id: str | None = None
    frame_id: str | None = None
    backend_node_id: int | None = None
    selector: str | None = None
    role: str | None = None
    accessible_name: str | None = None
    tag: str | None = None
    input_type: str | None = None
    shadow_path: list[str] | None = None
    is_secret: bool = False

    def __repr__(self) -> str:
        return f"Gesture(kind={self.kind!r}, text_length={len(self.text or '')})"


@dataclass(slots=True)
class Effect:
    seq: int
    t_received: float
    kind: EffectKind
    page_key: str
    url: str | None = None
    frame_id: str | None = None
    target_id: str | None = None
    caused_by_seq: int | None = None
    busy_ms: int | None = None
    is_main_frame: bool = False
    detail: dict[str, str | int | bool] = field(default_factory=dict)


class GestureLedger:
    def __init__(self, browser_session_id: str, *, capacity: int = 50_000) -> None:
        self.browser_session_id = browser_session_id
        self.capacity = capacity
        self.paused = False
        self._rows: deque[Gesture] = deque(maxlen=capacity)
        self._effects: deque[Effect] = deque(maxlen=capacity)
        self._stop_hooks: list[Callable[[], None]] = []
        self._next_seq = 1

    @property
    def version(self) -> int:
        return self._next_seq

    def append(self, gesture_without_seq: Gesture) -> Gesture | None:
        if self.paused:
            return None
        gesture = replace(gesture_without_seq, seq=self._next_seq)
        self._next_seq += 1
        # Native-rate moves would otherwise flood the count-capped ring and evict the clicks and keys the fold
        # needs; only the last move before a discrete gesture carries hover information.
        if (
            gesture.kind == "mouse_moved"
            and self._rows
            and self._rows[-1].kind == "mouse_moved"
            and self._rows[-1].page_key == gesture.page_key
        ):
            self._rows.pop()
        self._rows.append(gesture)
        return gesture

    def append_effect(self, effect_without_seq: Effect) -> Effect | None:
        if self.paused:
            return None
        effect = replace(effect_without_seq, seq=self._next_seq)
        self._next_seq += 1
        self._effects.append(effect)
        return effect

    def rows(self) -> list[Gesture]:
        return list(self._rows)

    def iter_recent(self) -> Iterator[Gesture]:
        return reversed(self._rows)

    def effects(self) -> list[Effect]:
        return list(self._effects)

    def on_stop(self, callback: Callable[[], None]) -> None:
        self._stop_hooks.append(callback)

    def _run_stop_hooks(self) -> None:
        hooks, self._stop_hooks = self._stop_hooks, []
        for callback in hooks:
            callback()

    def __len__(self) -> int:
        return len(self._rows)

    def __repr__(self) -> str:
        return f"GestureLedger(count={len(self)}, capacity={self.capacity})"


_ledgers: dict[str, GestureLedger] = {}


def start_ledger(browser_session_id: str) -> GestureLedger:
    return _ledgers.get(browser_session_id) or _ledgers.setdefault(
        browser_session_id, GestureLedger(browser_session_id)
    )


def get_ledger(browser_session_id: str) -> GestureLedger | None:
    return _ledgers.get(browser_session_id)


def stop_ledger(browser_session_id: str) -> GestureLedger | None:
    stop_resolver(browser_session_id)
    ledger = _ledgers.pop(browser_session_id, None)
    if ledger is not None:
        ledger._run_stop_hooks()
    return ledger
