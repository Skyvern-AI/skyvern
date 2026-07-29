import pytest

from skyvern.services.browser_recording.code_first import (
    actions_to_code_first_blocks,
    apply_draft_overlay,
    segment_actions,
)
from skyvern.services.browser_recording.service import Processor
from skyvern.services.browser_recording.types import (
    Action,
    ActionClick,
    ActionHover,
    ActionInputText,
    ActionKind,
    ActionTarget,
    ActionUrlChange,
    ActionWait,
    Mouse,
    RecordingDraftStep,
)

ORG_ID = "org_123"
PBS_ID = "pbs_123"
WP_ID = "wpid_123"

START_URL = "https://example.com/start"


def make_target(**kwargs) -> ActionTarget:
    return ActionTarget(mouse=Mouse(xp=0.5, yp=0.5), **kwargs)


def make_click(ts: float, url: str = START_URL, **target_kwargs) -> ActionClick:
    return ActionClick(
        kind=ActionKind.CLICK.value,
        target=make_target(**target_kwargs),
        timestamp_start=ts,
        timestamp_end=ts,
        url=url,
    )


def make_input(ts: float, value: str, url: str = START_URL, **target_kwargs) -> ActionInputText:
    return ActionInputText(
        kind=ActionKind.INPUT_TEXT.value,
        target=make_target(**target_kwargs),
        timestamp_start=ts,
        timestamp_end=ts,
        url=url,
        input_value=value,
    )


def make_url_change(ts: float, url: str) -> ActionUrlChange:
    return ActionUrlChange(
        kind=ActionKind.URL_CHANGE.value,
        target=make_target(),
        timestamp_start=ts,
        timestamp_end=ts,
        url=url,
    )


def make_wait(ts: float, duration_ms: int, url: str = START_URL) -> ActionWait:
    return ActionWait(
        kind=ActionKind.WAIT.value,
        target=make_target(),
        timestamp_start=ts,
        timestamp_end=ts + duration_ms,
        url=url,
        duration_ms=duration_ms,
    )


def make_hover(ts: float, url: str = START_URL, **target_kwargs) -> ActionHover:
    return ActionHover(
        kind=ActionKind.HOVER.value,
        target=make_target(**target_kwargs),
        timestamp_start=ts,
        timestamp_end=ts + 2000,
        url=url,
    )


def draft_for(action: Action, **overrides) -> RecordingDraftStep:
    block_type = {
        ActionKind.URL_CHANGE: "goto_url",
        ActionKind.WAIT: "wait",
    }.get(action.kind, "action")
    fields = {
        "step_id": f"step_{action.timestamp_start}",
        "action_kind": action.kind,
        "block_type": block_type,
        "label": "step",
        "timestamp_start": action.timestamp_start,
        "timestamp_end": action.timestamp_end,
    }
    fields.update(overrides)
    return RecordingDraftStep(**fields)


def test_click_and_type_synthesize_single_code_block() -> None:
    actions: list[Action] = [
        make_input(1000, "widgets", selector="#search", accessible_name="Search"),
        make_click(2000, selector="#submit", role="button", accessible_name="Go"),
    ]

    result = actions_to_code_first_blocks(actions, None)

    assert result is not None
    blocks, parameters = result
    assert len(blocks) == 1
    block = blocks[0]
    assert block.block_type == "code"
    assert f'await page.goto("{START_URL}"'.format(START_URL=START_URL) in block.code
    assert 'await page.locator("#search").fill(str(search))' in block.code
    assert 'await page.locator("#submit").click()' in block.code
    assert block.parameter_keys == ["search"]
    assert block.model_dump()["parameters"] == [{"key": "search"}]
    assert len(parameters) == 1
    assert parameters[0].key == "search"
    # Recorded values never persist as defaults (legacy parity + secret safety).
    assert parameters[0].default_value == ""
    assert parameters[0].workflow_parameter_type == "string"
    assert "widgets" not in block.code


def test_password_input_becomes_parameter_without_default() -> None:
    actions: list[Action] = [
        make_input(1000, "hunter2", selector="#pw", accessible_name="Password", input_type="password"),
    ]

    result = actions_to_code_first_blocks(actions, None)

    assert result is not None
    blocks, parameters = result
    assert "hunter2" not in blocks[0].code
    # `password` is sandbox-reserved, so the synthesizer names the slot `password_field`.
    assert parameters[0].key == "password_field"
    assert parameters[0].default_value == ""


def test_select_element_maps_to_select_option() -> None:
    actions: list[Action] = [
        make_input(1000, "CA", selector="#state", tag_name="SELECT", accessible_name="State"),
    ]

    result = actions_to_code_first_blocks(actions, None)

    assert result is not None
    blocks, _ = result
    assert 'await page.locator("#state").select_option("CA")' in blocks[0].code


def test_wait_and_hover_emit_deterministic_lines() -> None:
    actions: list[Action] = [
        make_hover(1000, selector="#menu", accessible_name="Menu"),
        make_wait(4000, 6000),
    ]

    result = actions_to_code_first_blocks(actions, None)

    assert result is not None
    blocks, _ = result
    assert 'await page.locator("#menu").hover()' in blocks[0].code
    assert "await page.wait_for_timeout(6000)" in blocks[0].code


def test_user_navigation_starts_new_segment() -> None:
    actions: list[Action] = [
        make_click(1000, selector="#one"),
        # 20s after the click: a typed/user-initiated navigation, not click-caused.
        make_url_change(21000, "https://other.example.com/page"),
        make_click(22000, url="https://other.example.com/page", selector="#two"),
    ]

    result = actions_to_code_first_blocks(actions, None)

    assert result is not None
    blocks, _ = result
    assert len(blocks) == 2
    assert '"https://other.example.com/page"' in blocks[1].code
    assert 'await page.locator("#two").click()' in blocks[1].code


def test_click_caused_navigation_stays_in_segment() -> None:
    actions: list[Action] = [
        make_click(1000, selector="#login"),
        make_url_change(1500, "https://example.com/dashboard"),
        make_click(2500, url="https://example.com/dashboard", selector="#profile"),
    ]

    result = actions_to_code_first_blocks(actions, None)

    assert result is not None
    blocks, _ = result
    assert len(blocks) == 1
    assert "dashboard" not in blocks[0].code


def test_goto_only_recording_emits_goto_code_block() -> None:
    actions: list[Action] = [make_url_change(1000, "https://example.com/target")]

    result = actions_to_code_first_blocks(actions, None)

    assert result is not None
    blocks, parameters = result
    assert len(blocks) == 1
    assert 'await page.goto("https://example.com/target"' in blocks[0].code
    assert blocks[0].model_dump()["parameters"] == []
    assert parameters == []


def test_actions_without_locators_fall_back_to_none() -> None:
    actions: list[Action] = [make_click(1000), make_click(2000)]

    assert actions_to_code_first_blocks(actions, None) is None


def test_draft_overlay_deletion_drops_action() -> None:
    kept = make_click(1000, selector="#keep", accessible_name="Keep")
    deleted = make_click(2000, selector="#delete", accessible_name="Delete")

    result = actions_to_code_first_blocks([kept, deleted], [draft_for(kept)])

    assert result is not None
    blocks, _ = result
    assert "#keep" in blocks[0].code
    assert "#delete" not in blocks[0].code


def test_empty_draft_steps_commits_empty_workflow() -> None:
    actions: list[Action] = [make_click(1000, selector="#keep", accessible_name="Keep")]

    assert apply_draft_overlay(actions, []) == []
    assert actions_to_code_first_blocks(actions, []) == ([], [])


def test_user_navigation_after_click_caused_navigation_starts_segment() -> None:
    actions: list[Action] = [
        make_click(1000, selector="#login"),
        # Click-caused navigation: consumed, no goto emitted.
        make_url_change(1500, "https://example.com/dashboard"),
        # A second navigation inside the click window is user-initiated.
        make_url_change(2500, "https://other.example.com/manual"),
        make_click(3000, url="https://other.example.com/manual", selector="#two"),
    ]

    result = actions_to_code_first_blocks(actions, None)

    assert result is not None
    blocks, _ = result
    assert len(blocks) == 2
    assert '"https://other.example.com/manual"' in blocks[1].code


def test_safety_gate_value_error_falls_back_to_legacy(monkeypatch: pytest.MonkeyPatch) -> None:
    def raise_value_error(code: str) -> None:
        raise ValueError("source code string cannot contain null bytes")

    monkeypatch.setattr(
        "skyvern.services.browser_recording.code_first.CodeBlock.is_safe_code",
        staticmethod(raise_value_error),
    )
    actions: list[Action] = [make_click(1000, selector="#go", accessible_name="Go")]

    assert actions_to_code_first_blocks(actions, None) is None


def test_draft_overlay_without_timestamps_keeps_all_actions() -> None:
    actions: list[Action] = [make_click(1000, selector="#keep")]
    draft = draft_for(actions[0], timestamp_start=None, timestamp_end=None)

    pairs = apply_draft_overlay(actions, [draft])

    assert [action for action, _ in pairs] == actions


def test_draft_overlay_wait_seconds_override() -> None:
    # The wait sits after a located click: a leading wait is absorbed by the
    # synthesizer's entry-replay visibility wait rather than emitted.
    click = make_click(1000, selector="#go")
    wait = make_wait(2000, 6000)

    result = actions_to_code_first_blocks([click, wait], [draft_for(click), draft_for(wait, wait_sec=12)])

    assert result is not None
    blocks, _ = result
    assert "await page.wait_for_timeout(12000)" in blocks[0].code


def test_colliding_parameter_keys_are_renamed_across_segments() -> None:
    actions: list[Action] = [
        make_input(1000, "first", selector="#q1", accessible_name="Search"),
        make_url_change(21000, "https://other.example.com/page"),
        make_input(22000, "second", url="https://other.example.com/page", selector="#q2", accessible_name="Search"),
    ]

    result = actions_to_code_first_blocks(actions, None)

    assert result is not None
    blocks, parameters = result
    assert len(blocks) == 2
    assert "fill(str(search))" in blocks[0].code
    assert "fill(str(search_2))" in blocks[1].code
    assert {parameter.key for parameter in parameters} == {"search", "search_2"}
    assert all(parameter.default_value == "" for parameter in parameters)


def test_same_field_across_segments_reuses_parameter_key() -> None:
    actions: list[Action] = [
        make_input(1000, "first", selector="#q", accessible_name="Search"),
        make_url_change(21000, "https://other.example.com/page"),
        make_input(22000, "second", url="https://other.example.com/page", selector="#q", accessible_name="Search"),
    ]

    result = actions_to_code_first_blocks(actions, None)

    assert result is not None
    blocks, parameters = result
    assert len(blocks) == 2
    assert "fill(str(search))" in blocks[0].code
    assert "fill(str(search))" in blocks[1].code
    assert [parameter.key for parameter in parameters] == ["search"]


def test_same_labeled_fields_in_one_segment_do_not_conflate_after_rename() -> None:
    # Segment 1 claims `search`; segment 2 has two distinct same-labeled fields
    # locally deduped to `search`/`search_2`. The rename of segment 2's `search`
    # must not cascade onto its `search_2` fill.
    actions: list[Action] = [
        make_input(1000, "first", selector="#q1", accessible_name="Search"),
        make_url_change(21000, "https://other.example.com/page"),
        make_input(22000, "second", url="https://other.example.com/page", selector="#q2", accessible_name="Search"),
        make_input(23000, "third", url="https://other.example.com/page", selector="#q3", accessible_name="Search"),
    ]

    result = actions_to_code_first_blocks(actions, None)

    assert result is not None
    blocks, parameters = result
    assert len(blocks) == 2
    assert 'page.locator("#q2").fill(str(search_3))' in blocks[1].code
    assert 'page.locator("#q3").fill(str(search_2))' in blocks[1].code
    keys = [parameter.key for parameter in parameters]
    assert sorted(keys) == ["search", "search_2", "search_3"]


def test_typed_secret_in_non_password_field_never_reaches_default_value() -> None:
    actions: list[Action] = [
        make_input(1000, "sk-secret-token", selector="#otp", accessible_name="One-time code", input_type="text"),
    ]

    result = actions_to_code_first_blocks(actions, None)

    assert result is not None
    blocks, parameters = result
    assert "sk-secret-token" not in blocks[0].code
    assert all("sk-secret-token" not in (parameter.default_value or "") for parameter in parameters)
    assert all(parameter.default_value == "" for parameter in parameters)


def test_segment_actions_uses_first_action_url_as_entry() -> None:
    click = make_click(1000, selector="#one")

    segments = segment_actions([(click, None)])

    assert len(segments) == 1
    assert segments[0].source_url == START_URL


@pytest.mark.asyncio
async def test_process_code_first_returns_code_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    actions: list[Action] = [make_click(1000, selector="#submit", accessible_name="Go")]
    monkeypatch.setattr(Processor, "compressed_chunks_to_events", lambda self, chunks: [])
    monkeypatch.setattr(
        Processor,
        "events_to_actions",
        lambda self, events, machines=None, initial_actions=None: actions,
    )

    processor = Processor(PBS_ID, ORG_ID, WP_ID)
    blocks, parameters = await processor.process(["chunk"], code_first=True)

    assert len(blocks) == 1
    assert blocks[0].block_type == "code"
    assert parameters == []


@pytest.mark.asyncio
async def test_process_code_first_falls_back_to_legacy_when_synthesis_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(Processor, "compressed_chunks_to_events", lambda self, chunks: [])

    processor = Processor(PBS_ID, ORG_ID, WP_ID)
    blocks, parameters = await processor.process(["chunk"], code_first=True)

    assert blocks == []
    assert parameters == []


@pytest.mark.asyncio
async def test_process_code_first_prefers_draft_overlay_over_drafts_to_blocks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kept = make_click(1000, selector="#keep", accessible_name="Keep")
    deleted = make_click(2000, selector="#delete", accessible_name="Delete")
    monkeypatch.setattr(Processor, "compressed_chunks_to_events", lambda self, chunks: [])
    monkeypatch.setattr(
        Processor,
        "events_to_actions",
        lambda self, events, machines=None, initial_actions=None: [kept, deleted],
    )

    processor = Processor(PBS_ID, ORG_ID, WP_ID)
    blocks, _ = await processor.process(["chunk"], draft_steps=[draft_for(kept)], code_first=True)

    assert len(blocks) == 1
    assert blocks[0].block_type == "code"
    assert "#keep" in blocks[0].code
    assert "#delete" not in blocks[0].code
