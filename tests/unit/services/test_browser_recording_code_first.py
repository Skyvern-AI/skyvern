import pytest

from skyvern.services.browser_recording.code_first import (
    _site_slug,
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
    ActionPressKey,
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


def make_press_key(ts: float, key: str, url: str = START_URL, **target_kwargs) -> ActionPressKey:
    return ActionPressKey(
        kind=ActionKind.PRESS_KEY.value,
        target=make_target(**target_kwargs),
        timestamp_start=ts,
        timestamp_end=ts,
        url=url,
        key=key,
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


def test_recorded_code_blocks_use_the_code_first_editor_shape() -> None:
    actions: list[Action] = [
        make_input(1000, "widgets", selector="#search", accessible_name="Search"),
        make_click(2000, selector="#submit", role="button", accessible_name="Go"),
    ]

    result = actions_to_code_first_blocks(actions, None)

    assert result is not None
    blocks, _ = result
    block = blocks[0]
    # A non-null prompt is what makes the editor render the code-first node; "" leaves the
    # Goal for the user to write rather than fabricating one.
    assert block.prompt == ""
    assert block.steps is not None
    assert [step.action_type for step in block.steps] == ["goto_url", "input_text", "click"]
    assert all(step.line_start is not None for step in block.steps)


def test_goto_only_recording_carries_the_code_first_editor_shape() -> None:
    actions: list[Action] = [make_url_change(1000, "https://example.com/only")]

    result = actions_to_code_first_blocks(actions, None)

    assert result is not None
    blocks, _ = result
    assert blocks[0].prompt == ""
    assert [step.action_type for step in blocks[0].steps or []] == ["goto_url"]


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
async def test_process_binds_credentials_only_for_a_caller_that_substitutes_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    email = make_input(1000, "user@example.com", selector="#email", accessible_name="Email", autocomplete="username")
    password = make_input(2000, "hunter2", selector="#pw", accessible_name="Password", input_type="password")
    actions: list[Action] = [email, password]
    drafts = [draft_for(email), draft_for(password, credential_id="cred_123", credential_kind="password")]
    monkeypatch.setattr(Processor, "compressed_chunks_to_events", lambda self, chunks: [])
    monkeypatch.setattr(
        Processor,
        "events_to_actions",
        lambda self, events, machines=None, initial_actions=None: actions,
    )
    processor = Processor(PBS_ID, ORG_ID, WP_ID)

    unsupported_blocks, _ = await processor.process(["chunk"], draft_steps=drafts, code_first=True)
    supported_blocks, _ = await processor.process(
        ["chunk"], draft_steps=drafts, code_first=True, supports_credential_tokens=True
    )

    # The route defaults to off, so an old frontend never receives code reading a token it
    # cannot rename - which would persist a block that fails at run time with a NameError.
    assert "cred_123" not in unsupported_blocks[0].code
    assert "cred_123.password" in supported_blocks[0].code


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


def test_recorded_login_binds_the_credential_and_its_identifier() -> None:
    email = make_input(1000, "user@example.com", selector="#email", accessible_name="Email", autocomplete="username")
    password = make_input(2000, "hunter2", selector="#pw", accessible_name="Password", input_type="password")
    submit = make_click(3000, selector="#login", role="button", accessible_name="Log in")
    drafts = [
        draft_for(email),
        draft_for(password, credential_id="cred_123", credential_kind="password"),
        draft_for(submit),
    ]

    result = actions_to_code_first_blocks([email, password, submit], drafts)

    assert result is not None
    blocks, parameters = result
    code = blocks[0].code
    assert 'await page.locator("#pw").fill(cred_123.password)' in code
    # The identifier typed just before the credential fill is bound too, or an empty
    # parameter lands in it at run time.
    assert 'await page.locator("#email").fill(cred_123.username)' in code
    assert "user@example.com" not in code
    # One credential parameter keyed by the credential id: a token the editor re-keys.
    assert [(parameter.key, parameter.parameter_type) for parameter in parameters] == [("cred_123", "credential")]
    assert parameters[0].credential_id == "cred_123"
    assert blocks[0].parameter_keys == ["cred_123"]


def test_recorded_secret_fill_binds_the_credentials_single_value_field() -> None:
    token = make_input(1000, "sk-live-abc", selector="#token", accessible_name="API token")
    save = make_click(2000, selector="#save", role="button", accessible_name="Save")
    drafts = [draft_for(token, credential_id="cred_secret", credential_kind="secret"), draft_for(save)]

    result = actions_to_code_first_blocks([token, save], drafts)

    assert result is not None
    blocks, parameters = result
    # A secret credential holds one value, so the fill can name it - as the legacy path does.
    assert 'await page.locator("#token").fill(cred_secret.secret_value)' in blocks[0].code
    assert "sk-live-abc" not in blocks[0].code
    assert [(parameter.key, parameter.parameter_type) for parameter in parameters] == [("cred_secret", "credential")]


def test_a_classified_field_before_a_password_is_not_claimed_as_the_identifier() -> None:
    current = make_input(1000, "old-pw", selector="#current", accessible_name="Current password", input_type="password")
    new = make_input(2000, "new-pw", selector="#new", accessible_name="New password", input_type="password")
    drafts = [draft_for(current), draft_for(new, credential_id="cred_login", credential_kind="password")]

    result = actions_to_code_first_blocks([current, new], drafts)

    assert result is not None
    blocks, _ = result
    # A second password box is not an identifier; filling the username into it types the wrong value.
    assert "cred_login.username" not in blocks[0].code
    assert 'await page.locator("#new").fill(cred_login.password)' in blocks[0].code


def test_recorded_credit_card_fill_stays_an_unbound_parameter() -> None:
    card = make_input(1000, "4111111111111111", selector="#card", accessible_name="Card number")
    drafts = [draft_for(card, credential_id="cred_card", credential_kind="credit_card")]

    result = actions_to_code_first_blocks([card], drafts)

    assert result is not None
    blocks, parameters = result
    # A credential parameter resolves only username/password/totp, and the recorder never
    # captures which card field was typed, so there is nothing to point the fill at.
    assert 'await page.locator("#card").fill(str(card_number))' in blocks[0].code
    assert [parameter.parameter_type for parameter in parameters] == ["workflow"]


def test_a_field_bound_to_another_credential_is_not_claimed_as_the_identifier() -> None:
    token = make_input(1000, "abc123", selector="#token", accessible_name="API token")
    password = make_input(2000, "hunter2", selector="#pw", accessible_name="Password", input_type="password")
    drafts = [
        draft_for(token, credential_id="cred_secret", credential_kind="secret"),
        draft_for(password, credential_id="cred_login", credential_kind="password"),
    ]

    result = actions_to_code_first_blocks([token, password], drafts)

    assert result is not None
    blocks, _ = result
    assert "cred_login.username" not in blocks[0].code
    assert 'await page.locator("#pw").fill(cred_login.password)' in blocks[0].code


def test_recorded_totp_fill_reads_the_credentials_runtime_code() -> None:
    email = make_input(1000, "user@example.com", selector="#email", accessible_name="Email", autocomplete="username")
    password = make_input(2000, "hunter2", selector="#pw", accessible_name="Password", input_type="password")
    submit = make_click(3000, selector="#login", role="button", accessible_name="Log in")
    code = make_input(4000, "123456", selector="#otp", accessible_name="One-time code")
    drafts = [
        draft_for(email),
        draft_for(password, credential_id="cred_123", credential_kind="password"),
        draft_for(submit),
        draft_for(code, credential_id="cred_123", credential_kind="totp"),
    ]

    result = actions_to_code_first_blocks([email, password, submit, code], drafts)

    assert result is not None
    blocks, parameters = result
    # A one-time code has no stored value: it resolves at run time through the credential.
    assert 'await page.locator("#otp").fill(await cred_123.otp())' in blocks[0].code
    assert "123456" not in blocks[0].code
    # Both fills read the one credential, so it stays a single parameter.
    assert [parameter.key for parameter in parameters] == ["cred_123"]


def test_only_an_identifier_field_is_claimed_for_the_username() -> None:
    email = make_input(1000, "user@example.com", selector="#email", accessible_name="Email", autocomplete="username")
    company = make_input(2000, "acme", selector="#company", accessible_name="Company domain", input_type="text")
    password = make_input(3000, "hunter2", selector="#pw", accessible_name="Password", input_type="password")
    drafts = [
        draft_for(email),
        draft_for(company),
        draft_for(password, credential_id="cred_123", credential_kind="password"),
    ]

    result = actions_to_code_first_blocks([email, company, password], drafts)

    assert result is not None
    blocks, parameters = result
    # The identifier is claimed even with another field of the same form between the two, and
    # the tenant field keeps its own parameter instead of receiving the username.
    assert 'await page.locator("#email").fill(cred_123.username)' in blocks[0].code
    assert 'await page.locator("#company").fill(str(company_domain))' in blocks[0].code
    assert [parameter.key for parameter in parameters] == ["company_domain", "cred_123"]


def test_a_search_box_before_a_password_is_not_claimed_for_the_username() -> None:
    search = make_input(1000, "widgets", selector="#q", accessible_name="Search", input_type="text")
    password = make_input(2000, "hunter2", selector="#pw", accessible_name="Password", input_type="password")
    drafts = [draft_for(search), draft_for(password, credential_id="cred_123", credential_kind="password")]

    result = actions_to_code_first_blocks([search, password], drafts)

    assert result is not None
    blocks, _ = result
    assert "cred_123.username" not in blocks[0].code
    assert 'await page.locator("#q").fill(str(search))' in blocks[0].code


def test_a_hover_between_the_identifier_and_the_password_keeps_the_binding() -> None:
    email = make_input(1000, "user@example.com", selector="#email", accessible_name="Email", autocomplete="username")
    hover = make_hover(2000, selector="#tooltip", accessible_name="Help")
    password = make_input(3000, "hunter2", selector="#pw", accessible_name="Password", input_type="password")
    drafts = [
        draft_for(email),
        draft_for(hover),
        draft_for(password, credential_id="cred_123", credential_kind="password"),
    ]

    result = actions_to_code_first_blocks([email, hover, password], drafts)

    assert result is not None
    blocks, _ = result
    # A stray hover has a 2s dwell of its own; it must not disable the binding.
    assert 'await page.locator("#email").fill(cred_123.username)' in blocks[0].code


def test_a_caller_that_cannot_substitute_tokens_gets_no_credential_binding() -> None:
    email = make_input(1000, "user@example.com", selector="#email", accessible_name="Email", autocomplete="username")
    password = make_input(2000, "hunter2", selector="#pw", accessible_name="Password", input_type="password")
    drafts = [draft_for(email), draft_for(password, credential_id="cred_123", credential_kind="password")]

    result = actions_to_code_first_blocks([email, password], drafts, bind_credentials=False)

    assert result is not None
    blocks, parameters = result
    # An old frontend renames the parameters but not the code, which would persist a block
    # whose code reads a token nothing declares - a NameError that saves clean.
    assert "cred_123" not in blocks[0].code
    assert [parameter.parameter_type for parameter in parameters] == ["workflow", "workflow"]


def test_an_email_field_is_not_assumed_to_be_a_login_identifier() -> None:
    login = make_input(
        1000,
        "new@example.com",
        selector="#login",
        accessible_name="Email",
        id="user_email",
        input_type="email",
        autocomplete="email",
    )
    password = make_input(2000, "hunter2", selector="#pw", accessible_name="Password", input_type="password")
    drafts = [draft_for(login), draft_for(password, credential_id="cred_123", credential_kind="password")]

    result = actions_to_code_first_blocks([login, password], drafts)

    assert result is not None
    blocks, _ = result
    # An email input is also how an account-settings form collects a new contact address, and
    # the confirmation below it is credential-bound. `type=email` and `autocomplete=email`
    # describe the value, not that it authenticates, so this field keeps its own parameter.
    assert "cred_123.username" not in blocks[0].code
    assert 'await page.locator("#login").fill(str(email))' in blocks[0].code


def test_an_autocomplete_username_field_is_claimed_for_the_username() -> None:
    login = make_input(1000, "someone", selector="#login", input_type="text", autocomplete="username")
    password = make_input(2000, "hunter2", selector="#pw", accessible_name="Password", input_type="password")
    drafts = [draft_for(login), draft_for(password, credential_id="cred_123", credential_kind="password")]

    result = actions_to_code_first_blocks([login, password], drafts)

    assert result is not None
    blocks, _ = result
    # The other typed identifier signal: a text input that declares what it holds.
    assert 'await page.locator("#login").fill(cred_123.username)' in blocks[0].code


def test_clicking_into_the_password_field_keeps_the_identifier_binding() -> None:
    email = make_input(1000, "user@example.com", selector="#email", autocomplete="username")
    focus = make_click(1500, selector="#pw", tag_name="INPUT", role="textbox")
    password = make_input(2000, "hunter2", selector="#pw", accessible_name="Password", input_type="password")
    drafts = [
        draft_for(email),
        draft_for(focus),
        draft_for(password, credential_id="cred_123", credential_kind="password"),
    ]

    result = actions_to_code_first_blocks([email, focus, password], drafts)

    assert result is not None
    blocks, _ = result
    # The ordinary mouse-driven login clicks the password box; the recorder emits that click.
    assert 'await page.locator("#email").fill(cred_123.username)' in blocks[0].code


def test_a_button_click_before_the_password_ends_the_form() -> None:
    email = make_input(1000, "user@example.com", selector="#email", autocomplete="username")
    submit = make_click(1500, selector="#continue", tag_name="BUTTON", role="button", accessible_name="Continue")
    password = make_input(2000, "hunter2", selector="#pw", accessible_name="Password", input_type="password")
    drafts = [
        draft_for(email),
        draft_for(submit),
        draft_for(password, credential_id="cred_123", credential_kind="password"),
    ]

    result = actions_to_code_first_blocks([email, submit, password], drafts)

    assert result is not None
    blocks, _ = result
    # A two-page login already leaves its first page a separate step; reaching over the button
    # would let one credential absorb whatever came before an unrelated form.
    assert "cred_123.username" not in blocks[0].code


def test_a_submit_input_before_the_password_ends_the_form() -> None:
    newsletter = make_input(1000, "user@example.com", selector="#news", autocomplete="username")
    submit = make_click(1500, selector="#subscribe", tag_name="INPUT", input_type="submit", role="button")
    password = make_input(2000, "hunter2", selector="#pw", accessible_name="Password", input_type="password")
    drafts = [
        draft_for(newsletter),
        draft_for(submit),
        draft_for(password, credential_id="cred_123", credential_kind="password"),
    ]

    result = actions_to_code_first_blocks([newsletter, submit, password], drafts)

    assert result is not None
    blocks, _ = result
    # `<input type="submit">` carries the same tag name as a text box but submits a form; an
    # email typed into that form belongs to it, not to the login that follows.
    assert "cred_123.username" not in blocks[0].code


def test_enter_submit_emits_a_press_and_keeps_its_navigation_in_segment() -> None:
    actions: list[Action] = [
        make_input(1000, "boots", selector="#search"),
        make_press_key(1100, "Enter", selector="#search"),
        make_url_change(1600, "https://example.com/results"),
        make_click(2600, url="https://example.com/results", selector="#first"),
    ]

    result = actions_to_code_first_blocks(actions, None)

    assert result is not None
    blocks, _ = result
    assert len(blocks) == 1
    assert 'await page.locator("#search").press("Enter")' in blocks[0].code
    assert "results" not in blocks[0].code


def test_press_key_without_a_locator_falls_back_to_the_keyboard() -> None:
    actions: list[Action] = [
        make_click(1000, selector="#menu"),
        make_press_key(2000, "Escape"),
    ]

    result = actions_to_code_first_blocks(actions, None)

    assert result is not None
    blocks, _ = result
    assert 'await page.keyboard.press("Escape")' in blocks[0].code


def test_block_labels_describe_what_each_segment_does() -> None:
    actions: list[Action] = [
        make_url_change(0, "http://127.0.0.1:8899/login"),
        make_input(1000, "user", url="http://127.0.0.1:8899/login", selector="#user", accessible_name="Username"),
        make_input(
            2000,
            "hunter2",
            url="http://127.0.0.1:8899/login",
            selector="#pw",
            accessible_name="Password",
            input_type="password",
        ),
        make_click(3000, url="http://127.0.0.1:8899/login", selector="#submit", accessible_name="Log in"),
        make_url_change(30000, "https://shop.example.com/"),
        make_input(
            31000, "widget", url="https://shop.example.com/", selector="#q", accessible_name="Search for a product"
        ),
        make_url_change(60000, "https://shop.example.com/cart"),
        make_click(61000, url="https://shop.example.com/cart", selector="#pay", accessible_name="Proceed to checkout"),
        make_url_change(90000, "https://shop.example.com/cart?retry=1"),
        make_click(
            91000, url="https://shop.example.com/cart?retry=1", selector="#pay", accessible_name="Proceed to checkout"
        ),
    ]

    result = actions_to_code_first_blocks(actions, None)

    assert result is not None
    blocks, _ = result
    # A local fixture host contributes no site name, so the login segment is just the verb;
    # the trailing "_2" keeps two identical checkout segments unique.
    assert [block.label for block in blocks] == [
        "log_in",
        "search_example",
        "click_proceed_to_checkout",
        "click_proceed_to_checkout_2",
    ]


def test_key_press_only_segment_is_named_after_the_key() -> None:
    actions: list[Action] = [
        make_url_change(0, "https://example.com/viewer"),
        make_press_key(1000, "Escape"),
    ]

    result = actions_to_code_first_blocks(actions, None)

    assert result is not None
    blocks, _ = result
    assert [block.label for block in blocks] == ["press_escape"]


def test_block_label_matches_search_as_a_complete_word() -> None:
    actions: list[Action] = [
        make_input(1000, "automation", selector="#topic", accessible_name="Research topic"),
    ]

    result = actions_to_code_first_blocks(actions, None)

    assert result is not None
    blocks, _ = result
    assert [block.label for block in blocks] == ["fill_in_research_topic"]


def test_block_label_does_not_use_textarea_content() -> None:
    actions: list[Action] = [
        make_input(
            1000,
            "replacement",
            selector='textarea[name="notes"]',
            tag_name="TEXTAREA",
            texts=["private customer content"],
        ),
    ]

    result = actions_to_code_first_blocks(actions, None)

    assert result is not None
    blocks, _ = result
    assert [block.label for block in blocks] == ["fill_in_a_field"]


def test_block_label_ignores_an_interaction_dropped_by_synthesis() -> None:
    actions: list[Action] = [
        make_url_change(0, "https://example.com/start"),
        make_hover(1000, accessible_name="Private menu copy"),
    ]

    result = actions_to_code_first_blocks(actions, None)

    assert result is not None
    blocks, _ = result
    assert [block.label for block in blocks] == ["open_example"]


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://app.example.com/x", "example"),
        # A multi-label public suffix must not name the block after "co".
        ("https://shop.example.co.uk/x", "example"),
        ("https://shop.example.ai/x", "example"),
        ("https://shop.example.co.kr/x", "example"),
        ("https://intranet/x", "intranet"),
        # Hosts with no name in them contribute nothing to the label.
        ("http://127.0.0.1:8899/login", ""),
        ("http://[::1]:8080/login", ""),
        ("http://localhost:3000/login", ""),
    ],
)
def test_site_slug_reads_a_name_only_from_named_hosts(url: str, expected: str) -> None:
    assert _site_slug(url) == expected


def test_a_credential_bound_on_the_focus_click_fills_the_field() -> None:
    login = make_input(1000, "someone", selector="#login", autocomplete="username")
    focus = make_click(1500, selector="#pw", tag_name="INPUT", input_type="password")
    password = make_input(2000, "hunter2", selector="#pw", accessible_name="Password", input_type="password")
    drafts = [
        draft_for(login),
        draft_for(focus, credential_id="cred_123", credential_kind="password"),
        draft_for(password),
    ]

    result = actions_to_code_first_blocks([login, focus, password], drafts)

    assert result is not None
    blocks, parameters = result
    # The panel offers the prompt on the click card and dismisses the fill's once bound, so the
    # binding arrives on the click; a code block still has to fill the field.
    assert 'await page.locator("#pw").fill(cred_123.password)' in blocks[0].code
    assert 'await page.locator("#login").fill(cred_123.username)' in blocks[0].code
    assert [parameter.key for parameter in parameters] == ["cred_123"]


def test_a_focus_click_credential_only_moves_to_its_own_field() -> None:
    focus = make_click(1000, selector="#pw", tag_name="INPUT", input_type="password")
    other = make_input(1500, "widgets", selector="#search", accessible_name="Search", input_type="text")
    password = make_input(2000, "hunter2", selector="#pw", accessible_name="Password", input_type="password")
    drafts = [
        draft_for(focus, credential_id="cred_123", credential_kind="password"),
        draft_for(other),
        draft_for(password),
    ]

    result = actions_to_code_first_blocks([focus, other, password], drafts)

    assert result is not None
    blocks, _ = result
    # The next fill after the click is not necessarily the field that was clicked.
    assert 'await page.locator("#search").fill(str(search))' in blocks[0].code
    assert 'await page.locator("#pw").fill(cred_123.password)' in blocks[0].code
