import pytest

from skyvern.services.browser_recording.v2.keyfold import Fact, fold
from skyvern.services.browser_recording.v2.ledger import Gesture, GestureKind, GestureLedger


def _gesture(seq: int, kind: GestureKind, **kwargs: object) -> Gesture:
    return Gesture(
        seq=seq,
        t_received=float(seq),
        kind=kind,
        page_key="page-1",
        url="https://example.test/form",
        **kwargs,
    )


@pytest.mark.parametrize(
    ("gestures", "expected"),
    [
        (
            [
                _gesture(1, "mouse_pressed", x=10, y=20, button="left", click_count=2),
                _gesture(2, "mouse_released", x=11, y=21, button="left", click_count=2),
            ],
            [("click", None, 0, None, "left", 2.0, [1, 2])],
        ),
        (
            [
                _gesture(1, "key", key="a", text="a", key_event_type="keyDown"),
                _gesture(2, "key", key="a", text="a", key_event_type="char"),
                _gesture(3, "key", key="a", key_event_type="keyUp"),
                _gesture(4, "key", key="Enter", key_event_type="rawKeyDown"),
                _gesture(5, "key", key="Enter", key_event_type="keyUp"),
            ],
            [
                ("type_text", None, 1, None, None, 1.0, [1]),
                ("press_key", None, 0, "Enter", None, 4.0, [4]),
            ],
        ),
        (
            [
                _gesture(1, "key", key="b", text="b", key_event_type="keyDown"),
                _gesture(2, "key", key="Tab", key_event_type="keyDown"),
            ],
            [
                ("type_text", None, 1, None, None, 1.0, [1]),
                ("press_key", None, 0, "Tab", None, 2.0, [2]),
            ],
        ),
        (
            [
                _gesture(1, "mouse_pressed", x=10, y=20, button="left", click_count=1),
                _gesture(2, "key", key="a", text="a", key_event_type="keyDown"),
                _gesture(3, "key", key="Tab", key_event_type="keyDown"),
                _gesture(4, "key", key="b", text="b", key_event_type="keyDown"),
            ],
            [
                ("click", None, 0, None, "left", 1.0, [1]),
                ("type_text", "a", 1, None, None, 2.0, [2]),
                ("press_key", None, 0, "Tab", None, 3.0, [3]),
                ("type_text", None, 1, None, None, 4.0, [4]),
            ],
        ),
        (
            [
                _gesture(1, "key", key="c", text="c", key_event_type="keyDown"),
                _gesture(2, "mouse_pressed", x=30, y=40, button="left", click_count=1),
            ],
            [
                ("type_text", None, 1, None, None, 1.0, [1]),
                ("click", None, 0, None, "left", 2.0, [2]),
            ],
        ),
        (
            [
                _gesture(1, "key", key="a", text="a", key_event_type="keyDown"),
                _gesture(2, "key", key="b", text="b", key_event_type="keyDown"),
                _gesture(3, "key", key="Backspace", key_event_type="rawKeyDown"),
                _gesture(4, "paste", text="cd"),
                _gesture(5, "key", key="ArrowLeft", key_event_type="rawKeyDown"),
                _gesture(6, "key", key="ArrowLeft", key_event_type="keyUp"),
            ],
            [
                ("type_text", None, 3, None, None, 4.0, [1, 2, 3, 4]),
                ("press_key", None, 0, "ArrowLeft", None, 5.0, [5]),
            ],
        ),
        (
            [
                _gesture(1, "key", key="a", text="a", modifiers=4, key_event_type="keyDown"),
                _gesture(2, "key", key="n", text="n", key_event_type="keyDown"),
                _gesture(3, "key", key="e", text="e", key_event_type="keyDown"),
                _gesture(4, "key", key="w", text="w", key_event_type="keyDown"),
            ],
            [
                ("press_key", None, 0, "a", None, 1.0, [1]),
                ("type_text", None, 3, None, None, 4.0, [2, 3, 4]),
            ],
        ),
        (
            [
                _gesture(1, "key", key="Escape", key_event_type="rawKeyDown"),
                _gesture(2, "key", key="a", text="a", key_event_type="keyDown"),
                _gesture(3, "key", key="Enter", key_event_type="rawKeyDown"),
            ],
            [
                ("press_key", None, 0, "Escape", None, 1.0, [1]),
                ("type_text", None, 1, None, None, 2.0, [2]),
                ("press_key", None, 0, "Enter", None, 3.0, [3]),
            ],
        ),
        (
            [
                _gesture(1, "key", key="Backspace", key_event_type="rawKeyDown"),
                _gesture(2, "key", key="Backspace", key_event_type="rawKeyDown"),
                _gesture(3, "key", key="x", text="x", key_event_type="keyDown"),
            ],
            [
                ("press_key", None, 0, "Backspace", None, 1.0, [1]),
                ("press_key", None, 0, "Backspace", None, 2.0, [2]),
                ("type_text", None, 1, None, None, 3.0, [3]),
            ],
        ),
        (
            [_gesture(1, "mouse_pressed", x=10, y=20, button="left", click_count=1)],
            [("click", None, 0, None, "left", 1.0, [1])],
        ),
        (
            [
                _gesture(1, "key", key="a", text="a", key_event_type="keyDown"),
                _gesture(2, "navigate", target_url="https://example.test/next"),
            ],
            [("type_text", None, 1, None, None, 1.0, [1])],
        ),
        (
            [
                _gesture(1, "mouse_pressed", button="left", click_count=1),
                _gesture(2, "mouse_pressed", button="right", click_count=1),
                _gesture(3, "mouse_released", button="left", click_count=1),
                _gesture(4, "mouse_released", button="right", click_count=1),
            ],
            [
                ("click", None, 0, None, "left", 3.0, [1, 3]),
                ("click", None, 0, None, "right", 4.0, [2, 4]),
            ],
        ),
        (
            [_gesture(1, "key", key="A", text="A", modifiers=8, key_event_type="keyDown")],
            [("type_text", None, 1, None, None, 1.0, [1])],
        ),
        (
            [
                _gesture(1, "key", key="a", text="a", key_event_type="keyDown"),
                _gesture(2, "key", key="ArrowLeft", key_event_type="rawKeyDown"),
                _gesture(3, "key", key="b", text="b", key_event_type="keyDown"),
            ],
            [
                ("type_text", None, 1, None, None, 1.0, [1]),
                ("press_key", None, 0, "ArrowLeft", None, 2.0, [2]),
                ("type_text", None, 1, None, None, 3.0, [3]),
            ],
        ),
        # The caret moved before the delete, so Backspace must not pop the earlier run's
        # character: the whole "a" plus the backspace would vanish from the recording.
        (
            [
                _gesture(1, "key", key="a", text="a", key_event_type="keyDown"),
                _gesture(2, "key", key="ArrowLeft", key_event_type="rawKeyDown"),
                _gesture(3, "key", key="Backspace", key_event_type="rawKeyDown"),
            ],
            [
                ("type_text", None, 1, None, None, 1.0, [1]),
                ("press_key", None, 0, "ArrowLeft", None, 2.0, [2]),
                ("press_key", None, 0, "Backspace", None, 3.0, [3]),
            ],
        ),
        # A press whose release never came before the navigation must not claim the next
        # release on that button, which belongs to a click on the new page.
        (
            [
                _gesture(1, "mouse_pressed", x=1, y=2, button="left", click_count=1),
                _gesture(2, "navigate"),
                _gesture(3, "mouse_pressed", x=3, y=4, button="left", click_count=1),
                _gesture(4, "mouse_released", button="left", click_count=1),
            ],
            [
                ("click", None, 0, None, "left", 1.0, [1]),
                ("click", None, 0, None, "left", 4.0, [3, 4]),
            ],
        ),
    ],
)
def test_fold_scripted_gestures(
    gestures: list[Gesture],
    expected: list[tuple[str, str | None, int, str | None, str | None, float, list[int]]],
) -> None:
    facts = fold(gestures)

    assert [
        (
            fact.kind,
            fact.typed_value,
            fact.typed_length,
            fact.key,
            fact.button,
            fact.t_end,
            fact.gesture_seqs,
        )
        for fact in facts
    ] == expected


def test_secret_values_are_absent_from_reprs_and_survive_redaction_as_lengths() -> None:
    secret = "private-value"
    gesture = _gesture(1, "key", key=secret, text=secret, key_event_type="keyDown")
    ledger = GestureLedger("pbs-secret")
    ledger.append(gesture)
    fact = Fact(
        kind="type_text",
        t_start=1.0,
        t_end=1.0,
        page_key="page-1",
        url="https://example.test/form",
        typed_value=secret,
        typed_length=len(secret),
    )

    assert secret not in repr(gesture)
    assert secret not in repr(ledger)
    assert secret not in repr(fact)

    fact.redact()

    assert fact.typed_value is None
    assert fact.typed_length == len(secret)


def test_fold_copies_focus_metadata_and_clears_it_after_tab() -> None:
    focus = _gesture(
        1,
        "mouse_pressed",
        selector="#password",
        role="textbox",
        accessible_name="Password",
        tag="INPUT",
        input_type="password",
        is_secret=True,
    )

    facts = fold(
        [
            focus,
            _gesture(2, "key", key="a", text="a", key_event_type="keyDown"),
            _gesture(3, "key", key="Tab", key_event_type="keyDown"),
            _gesture(4, "key", key="b", text="b", key_event_type="keyDown"),
        ]
    )

    first_run = facts[1]
    second_run = facts[3]
    assert (
        first_run.selector,
        first_run.role,
        first_run.accessible_name,
        first_run.tag,
        first_run.input_type,
    ) == ("#password", "textbox", "Password", "INPUT", "password")
    assert first_run.typed_value is None
    assert first_run.typed_length == 1
    assert second_run.selector is None
    assert second_run.typed_value is None
    assert second_run.typed_length == 1


def test_fold_clears_focus_and_redacts_run_started_after_navigation() -> None:
    focus = _gesture(
        1,
        "mouse_pressed",
        selector="#username",
        role="textbox",
        accessible_name="Username",
        tag="INPUT",
        input_type="text",
        is_secret=False,
    )

    facts = fold(
        [
            focus,
            _gesture(2, "navigate"),
            _gesture(3, "key", key="a", text="a", key_event_type="keyDown"),
        ]
    )

    run_after_navigation = facts[-1]
    assert run_after_navigation.selector is None
    assert run_after_navigation.typed_value is None
    assert run_after_navigation.typed_length == 1
