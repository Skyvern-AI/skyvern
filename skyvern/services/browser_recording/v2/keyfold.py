from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from skyvern.services.browser_recording.v2.ledger import Gesture

FactKind = Literal["click", "type_text", "press_key"]
_NAVIGATION_KINDS = frozenset({"navigate", "go_back", "go_forward", "reload"})
_KEY_DOWN_TYPES = frozenset({"keyDown", "rawKeyDown"})
# CDP Input.dispatchKeyEvent modifiers: Alt=1, Ctrl=2, Meta=4, Shift=8. Shift still produces typed text.
_CHORD_MODIFIERS = 1 | 2 | 4


@dataclass(slots=True, repr=False)
class Fact:
    kind: FactKind
    t_start: float
    t_end: float
    page_key: str
    url: str
    x: int | None = None
    y: int | None = None
    button: str | None = None
    click_count: int | None = None
    key: str | None = None
    typed_value: str | None = None
    typed_length: int = 0
    gesture_seqs: list[int] = field(default_factory=list)
    target_id: str | None = None
    frame_id: str | None = None
    backend_node_id: int | None = None
    selector: str | None = None
    role: str | None = None
    accessible_name: str | None = None
    tag: str | None = None
    input_type: str | None = None
    shadow_path: list[str] | None = None

    def redact(self) -> None:
        self.typed_value = None

    def __repr__(self) -> str:
        return f"Fact(kind={self.kind!r}, typed_length={self.typed_length}, gesture_count={len(self.gesture_seqs)})"


def _fact(gesture: Gesture, kind: FactKind, **fields: Any) -> Fact:
    locator_source = fields.pop("locator_source", gesture)
    return Fact(
        kind,
        gesture.t_received,
        fields.pop("t_end", gesture.t_received),
        gesture.page_key,
        gesture.url,
        gesture_seqs=fields.pop("gesture_seqs", [gesture.seq]),
        target_id=gesture.target_id,
        frame_id=gesture.frame_id,
        backend_node_id=gesture.backend_node_id,
        selector=locator_source.selector,
        role=locator_source.role,
        accessible_name=locator_source.accessible_name,
        tag=locator_source.tag,
        input_type=locator_source.input_type,
        shadow_path=locator_source.shadow_path,
        **fields,
    )


def fold(gestures: Sequence[Gesture]) -> list[Fact]:
    facts: list[Fact] = []
    typed_chars: list[str] = []
    typed_gestures: list[Gesture] = []
    standalone_keys: list[Gesture] = []
    pending_clicks: dict[str | None, deque[Fact]] = {}
    focus_gesture: Gesture | None = None
    # Focus is secret-until-established: a run typed before the first click (autofocused field) is redacted.
    focus_unknown = True
    run_focus: Gesture | None = None
    run_focus_unknown = False

    def flush_standalone_keys() -> None:
        facts.extend(_fact(gesture, "press_key", key=gesture.key) for gesture in standalone_keys)
        standalone_keys.clear()

    def start_run() -> None:
        nonlocal run_focus, run_focus_unknown
        run_focus = focus_gesture
        run_focus_unknown = focus_unknown

    def seal_run() -> None:
        nonlocal run_focus, run_focus_unknown
        if typed_chars:
            first, last = typed_gestures[0], typed_gestures[-1]
            fact = _fact(
                first,
                "type_text",
                t_end=last.t_received,
                typed_value="".join(typed_chars),
                typed_length=len(typed_chars),
                gesture_seqs=[gesture.seq for gesture in typed_gestures],
                locator_source=run_focus or first,
            )
            if run_focus_unknown or (run_focus is not None and run_focus.is_secret):
                fact.redact()
            facts.append(fact)
        flush_standalone_keys()
        typed_chars.clear()
        typed_gestures.clear()
        run_focus = None
        run_focus_unknown = False

    for gesture in gestures:
        if gesture.kind == "mouse_pressed":
            seal_run()
            focus_gesture = gesture
            focus_unknown = False
            fact = _fact(
                gesture, "click", x=gesture.x, y=gesture.y, button=gesture.button, click_count=gesture.click_count
            )
            facts.append(fact)
            pending_clicks.setdefault(gesture.button, deque()).append(fact)
            continue

        if gesture.kind == "mouse_released":
            pending = pending_clicks.get(gesture.button)
            if pending:
                fact = pending.popleft()
                fact.t_end = gesture.t_received
                fact.gesture_seqs.append(gesture.seq)
            continue

        if gesture.kind in _NAVIGATION_KINDS:
            seal_run()
            # A press whose release never arrived (mousedown-triggered nav, interrupted drag)
            # would otherwise take the t_end of the next release on that button, on the new page.
            pending_clicks.clear()
            focus_gesture = None
            focus_unknown = True
            continue

        if gesture.kind == "paste":
            if gesture.text:
                if standalone_keys:
                    seal_run()
                if not typed_gestures:
                    start_run()
                typed_chars.extend(gesture.text)
                typed_gestures.append(gesture)
            continue

        if gesture.kind != "key" or gesture.key_event_type not in _KEY_DOWN_TYPES:
            continue

        if gesture.key in {"Enter", "Tab"}:
            seal_run()
            facts.append(_fact(gesture, "press_key", key=gesture.key))
            if gesture.key == "Tab":
                focus_gesture = None
                focus_unknown = True
            continue

        if gesture.key == "Backspace":
            # A key like ArrowLeft moved the caret, so the run it interrupted is over: popping
            # across it would delete a character the user never deleted.
            if standalone_keys:
                seal_run()
            if typed_chars:
                typed_chars.pop()
                typed_gestures.append(gesture)
            else:
                standalone_keys.append(gesture)
            continue

        if (
            gesture.key_event_type == "keyDown"
            and gesture.text
            and gesture.text.isprintable()
            and not gesture.modifiers & _CHORD_MODIFIERS
        ):
            if standalone_keys:
                seal_run()
            if not typed_gestures:
                start_run()
            typed_chars.extend(gesture.text)
            typed_gestures.append(gesture)
            continue

        standalone_keys.append(gesture)

    seal_run()
    return facts
