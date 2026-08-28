from types import SimpleNamespace
from unittest.mock import Mock

from skyvern.services.browser_recording.v2.ledger import get_ledger, start_ledger, stop_ledger
from skyvern.services.browser_recording.v2.resolver import start_resolver
from skyvern.services.browser_recording.v2.tap import tap_navigation, tap_paste, tap_pipelined


def test_tap_pipelined_is_inert_without_a_ledger_and_appends_validated_mouse_fields_with_one() -> None:
    browser_session_id = "pbs-tap"
    page = SimpleNamespace(url="https://example.test/form")
    validated = {
        "type": "mousePressed",
        "x": 12,
        "y": 34,
        "button": "left",
        "clickCount": 2,
        "modifiers": 4,
    }
    stop_ledger(browser_session_id)

    cdp_session = SimpleNamespace()

    tap_pipelined(browser_session_id, "mouseEvent", validated, 1.25, page, cdp_session)

    assert get_ledger(browser_session_id) is None

    ledger = start_ledger(browser_session_id)
    try:
        tap_pipelined(browser_session_id, "mouseEvent", validated, 1.25, page, cdp_session)
        tap_pipelined(browser_session_id, "mouseEvent", validated, 1.5, SimpleNamespace(), cdp_session)

        assert [row.seq for row in ledger.rows()] == [1, 2]
        row = ledger.rows()[0]
        assert row.kind == "mouse_pressed"
        assert row.t_received == 1.25
        assert row.page_key == str(id(page))
        assert row.url == page.url
        assert (row.x, row.y, row.button, row.click_count, row.modifiers) == (12, 34, "left", 2, 4)
        assert ledger.rows()[1].url == ""
    finally:
        stop_ledger(browser_session_id)


def test_tap_pipelined_schedules_resolution_for_mouse_pressed_only() -> None:
    browser_session_id = "pbs-resolver-tap"
    ledger = start_ledger(browser_session_id)
    resolver = start_resolver(browser_session_id, ledger)
    resolver.on_gesture = Mock()
    page = SimpleNamespace(url="https://example.test/form")
    cdp_session = SimpleNamespace()

    try:
        tap_pipelined(
            browser_session_id,
            "mouseEvent",
            {"type": "mousePressed", "x": 12, "y": 34},
            1.0,
            page,
            cdp_session,
        )
        tap_pipelined(
            browser_session_id,
            "mouseEvent",
            {"type": "mouseMoved", "x": 13, "y": 35},
            2.0,
            page,
            cdp_session,
        )

        resolver.on_gesture.assert_called_once_with(ledger.rows()[0], cdp_session, page)
    finally:
        stop_ledger(browser_session_id)


def test_tap_paste_uses_the_latest_ledger_page_identity_or_empty_strings() -> None:
    browser_session_id = "pbs-paste"
    stop_ledger(browser_session_id)
    ledger = start_ledger(browser_session_id)
    page = SimpleNamespace()
    try:
        tap_paste(browser_session_id, "first", 1.0)
        tap_navigation(browser_session_id, "reloadEvent", {}, 2.0, page)
        tap_paste(browser_session_id, "second", 3.0)

        first, navigation, second = ledger.rows()
        assert (first.page_key, first.url) == ("", "")
        assert navigation.url == ""
        assert (second.page_key, second.url) == (navigation.page_key, navigation.url)
    finally:
        stop_ledger(browser_session_id)


def test_tap_failure_is_logged_and_never_reaches_the_input_path(monkeypatch) -> None:
    browser_session_id = "pbs-guard"
    stop_ledger(browser_session_id)
    ledger = start_ledger(browser_session_id)
    try:
        monkeypatch.setattr(ledger, "append", Mock(side_effect=RuntimeError("recorder bug")))

        tap_pipelined(browser_session_id, "mouseEvent", {"type": "mousePressed"}, 1.0, SimpleNamespace(), None)
        tap_navigation(browser_session_id, "reloadEvent", {}, 2.0, SimpleNamespace())
        tap_paste(browser_session_id, "text", 3.0)
    finally:
        stop_ledger(browser_session_id)


def test_consecutive_mouse_moves_collapse_to_the_latest_so_they_cannot_evict_discrete_gestures() -> None:
    browser_session_id = "pbs-moves"
    stop_ledger(browser_session_id)
    ledger = start_ledger(browser_session_id)
    page = SimpleNamespace(url="https://example.test/")
    try:
        tap_pipelined(browser_session_id, "mouseEvent", {"type": "mousePressed", "x": 1, "y": 1}, 1.0, page, None)
        for i in range(ledger.capacity + 10):
            tap_pipelined(browser_session_id, "mouseEvent", {"type": "mouseMoved", "x": i, "y": 0}, 2.0 + i, page, None)
        tap_pipelined(
            browser_session_id, "mouseEvent", {"type": "mouseMoved", "x": 5, "y": 5}, 9.0, SimpleNamespace(), None
        )

        kinds = [(row.kind, row.x) for row in ledger.rows()]
        assert kinds == [("mouse_pressed", 1), ("mouse_moved", ledger.capacity + 9), ("mouse_moved", 5)]
    finally:
        stop_ledger(browser_session_id)
