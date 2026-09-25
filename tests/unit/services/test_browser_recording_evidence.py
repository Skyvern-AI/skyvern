from skyvern.services.browser_recording.evidence import RecordedPointerEvidence, build_recording_evidence
from skyvern.services.browser_recording.state_machines.click import StateMachineClick
from skyvern.services.browser_recording.types import Action, ActionKind, Mouse
from tests.unit.services.test_browser_recording import make_console_event
from tests.unit.services.test_browser_recording_code_first import (
    PBS_ID,
    WP_ID,
    draft_for,
    make_click,
    make_input,
    make_url_change,
)


def test_typed_values_never_enter_evidence_packet() -> None:
    plain_value = "hunter2-distinct"
    password_value = "password-distinct"
    actions: list[Action] = [
        make_click(1000, selector="#start"),
        make_input(2000, plain_value, selector="#query", input_type="text"),
        make_input(3000, password_value, selector="#password", input_type="password"),
        make_click(4000, selector="#editor", tag_name="div", role="textbox", texts=[f"Notes: {plain_value}"]),
    ]

    packet = build_recording_evidence(
        actions,
        None,
        browser_session_id=PBS_ID,
        workflow_permanent_id=WP_ID,
        recording_attempt_id="rra_test",
    )

    serialized = packet.model_dump_json()
    assert plain_value not in serialized
    assert password_value not in serialized
    assert packet.actions[1].input is not None
    assert packet.actions[1].input.typed_length == len(plain_value)
    assert packet.actions[1].credential is None
    assert packet.actions[2].credential is not None
    assert packet.actions[2].credential.credential_kind == "password"
    assert packet.actions[2].input is None
    assert packet.actions[3].target is not None
    assert packet.actions[3].target.visible_texts == []


def test_typed_values_are_redacted_from_recorded_urls() -> None:
    typed_value = "private search"
    input_action = make_input(1000, typed_value, selector="#query", input_type="text")
    input_action.url = "https://example.com/search"
    navigation = make_url_change(2000, "https://example.com/search?q=private+search")

    packet = build_recording_evidence(
        [input_action, navigation],
        None,
        browser_session_id=PBS_ID,
        workflow_permanent_id=WP_ID,
        recording_attempt_id="rra_test",
    )

    serialized = packet.model_dump_json()
    assert typed_value not in serialized
    assert "private+search" not in serialized
    assert "[REDACTED_INPUT]" in serialized


def test_typed_values_are_redacted_from_later_target_metadata() -> None:
    typed_value = "private search"
    input_action = make_input(1000, typed_value, selector="#query", input_type="text")
    result = make_click(
        2000,
        selector='[data-query="private%20search"]',
        accessible_name="Open private search",
        texts=["Result: private+search"],
    )

    packet = build_recording_evidence(
        [input_action, result],
        None,
        browser_session_id=PBS_ID,
        workflow_permanent_id=WP_ID,
        recording_attempt_id="rra_test",
    )

    serialized = packet.model_dump_json()
    assert typed_value not in serialized
    assert "private%20search" not in serialized
    assert "private+search" not in serialized
    assert "[REDACTED_INPUT]" in serialized


def test_short_typed_values_do_not_corrupt_unrelated_metadata() -> None:
    input_action = make_input(1000, "a", selector="#query", input_type="text")
    input_action.url = "https://example.com/search?q=a"
    result = make_click(
        2000,
        selector='[data-query="a"]',
        accessible_name="Open a result",
        texts=["Result a"],
    )

    packet = build_recording_evidence(
        [input_action, result],
        None,
        browser_session_id=PBS_ID,
        workflow_permanent_id=WP_ID,
        recording_attempt_id="rra_test",
    )

    assert packet.actions[0].url == "https://example.com/search?q=[REDACTED_INPUT]"
    assert packet.actions[1].target is not None
    assert packet.actions[1].target.selector_candidates == ['[data-query="[REDACTED_INPUT]"]']
    assert packet.actions[1].target.accessible_name == "Open [REDACTED_INPUT] result"
    assert packet.actions[1].target.visible_texts == ["Result [REDACTED_INPUT]"]


def test_encoded_short_typed_values_only_redact_standalone_matches() -> None:
    input_action = make_input(1000, "?", selector="#query", input_type="text")
    input_action.url = "https://example.com/path%3Fhelp?q=%3F"

    packet = build_recording_evidence(
        [input_action],
        None,
        browser_session_id=PBS_ID,
        workflow_permanent_id=WP_ID,
        recording_attempt_id="rra_test",
    )

    assert packet.actions[0].url == "https://example.com/path%3Fhelp?q=[REDACTED_INPUT]"


def test_draft_overlay_tracks_deleted_actions_and_labels() -> None:
    kept = make_click(1000, selector="#keep")
    deleted = make_click(2000, selector="#delete")

    packet = build_recording_evidence(
        [kept, deleted],
        [draft_for(kept, label="Edited label")],
        browser_session_id=PBS_ID,
        workflow_permanent_id=WP_ID,
        recording_attempt_id="rra_test",
    )

    assert packet.deleted_action_ids == ["a002"]
    assert [action.action_id for action in packet.actions] == ["a001"]
    assert packet.actions[0].draft_label == "Edited label"


def test_navigation_attribution_and_action_order() -> None:
    click = make_click(1000, selector="#next")
    close_navigation = make_url_change(1500, "https://example.com/next")
    late_navigation = make_url_change(10000, "https://example.com/later")

    packet = build_recording_evidence(
        [late_navigation, close_navigation, click],
        None,
        browser_session_id=PBS_ID,
        workflow_permanent_id=WP_ID,
        recording_attempt_id="rra_test",
    )

    assert [action.action_id for action in packet.actions] == ["a001", "a002", "a003"]
    assert [action.kind for action in packet.actions] == [
        ActionKind.CLICK,
        ActionKind.URL_CHANGE,
        ActionKind.URL_CHANGE,
    ]
    assert packet.actions[0].observed_effects == ["navigation"]
    assert packet.actions[0].navigated_to == "https://example.com/next"
    assert packet.actions[2].observed_effects == []
    assert packet.actions[2].navigated_to is None


def test_focus_click_credential_transfers_to_fill() -> None:
    click = make_click(1000, selector="#password")
    fill = make_input(2000, "typed-secret", selector="#password", input_type="text")

    packet = build_recording_evidence(
        [click, fill],
        [draft_for(click, credential_kind="password", credential_id="cred_1"), draft_for(fill)],
        browser_session_id=PBS_ID,
        workflow_permanent_id=WP_ID,
        recording_attempt_id="rra_test",
    )

    assert packet.actions[1].credential is not None
    assert packet.actions[1].credential.credential_id == "cred_1"
    assert packet.actions[1].credential.credential_kind == "password"
    assert packet.actions[1].input is None


def test_selector_candidates_split_and_validate_class_tokens() -> None:
    click = make_click(1000, class_name="row selected")

    packet = build_recording_evidence(
        [click],
        None,
        browser_session_id=PBS_ID,
        workflow_permanent_id=WP_ID,
        recording_attempt_id="rra_test",
    )

    target = packet.actions[0].target
    assert target is not None
    assert target.selector_candidates == [".row", ".selected"]
    assert ".row selected" not in target.selector_candidates


def test_canvas_clicks_are_distinguished_by_pointer() -> None:
    first = make_click(1000, tag_name="canvas", selector="canvas")
    first.target.mouse = Mouse(xp=0.25, yp=0.4)
    second = make_click(2000, tag_name="canvas", selector="canvas")
    second.target.mouse = Mouse(xp=0.75, yp=0.6)
    button = make_click(3000, selector="#submit", tag_name="button")

    packet = build_recording_evidence(
        [first, second, button],
        None,
        browser_session_id=PBS_ID,
        workflow_permanent_id=WP_ID,
        recording_attempt_id="rra_test",
    )

    targets = [action.target for action in packet.actions if action.target is not None]
    assert len(targets) == 3
    assert targets[0].pointer == RecordedPointerEvidence(viewport_x_fraction=0.25, viewport_y_fraction=0.4)
    assert targets[1].pointer == RecordedPointerEvidence(viewport_x_fraction=0.75, viewport_y_fraction=0.6)
    assert targets[2].selector_candidates == ["#submit"]
    assert targets[2].pointer == RecordedPointerEvidence(viewport_x_fraction=0.5, viewport_y_fraction=0.5)


def test_left_edge_click_keeps_zero_pointer() -> None:
    event = make_console_event(
        {
            "type": "click",
            "target": {"id": "board", "skyId": "sky-1", "tagName": "CANVAS", "text": []},
            "timestamp": 1000,
            "mousePosition": {"xp": 0.0, "yp": 0.4},
        },
        timestamp=1000,
    )
    click = StateMachineClick().tick(event, [])
    assert click is not None

    packet = build_recording_evidence(
        [click],
        None,
        browser_session_id=PBS_ID,
        workflow_permanent_id=WP_ID,
        recording_attempt_id="rra_test",
    )

    target = packet.actions[0].target
    assert target is not None
    assert target.pointer == RecordedPointerEvidence(viewport_x_fraction=0.0, viewport_y_fraction=0.4)
