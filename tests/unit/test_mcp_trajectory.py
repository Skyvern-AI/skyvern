from __future__ import annotations

import json
from collections.abc import Iterator
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

from skyvern.cli.core import session_manager, trajectory_store
from skyvern.cli.core.browser_ops import NativeOptionSelection
from skyvern.cli.core.result import BrowserContext, set_concise_responses
from skyvern.cli.core.session_manager import SessionState, set_current_session
from skyvern.cli.core.session_ops import SessionCloseResult
from skyvern.cli.core.trajectory_store import append_trajectory_entry, delete_trajectory, get_trajectory
from skyvern.cli.mcp_tools import browser as mcp_browser
from skyvern.cli.mcp_tools import session as mcp_session
from skyvern.cli.mcp_tools import trajectory as mcp_trajectory
from skyvern.cli.mcp_tools.code_block import skyvern_code_block_synthesize
from skyvern.forge.sdk.copilot.typed_value_policy import typed_text_looks_secret

_HASH_A = "principal-a"
_HASH_B = "principal-b"
_SESSION_IDS = (
    "pbs_roundtrip",
    "pbs_sessionless",
    "pbs_failed_action",
    "pbs_empty_type",
    "pbs_isolation",
    "pbs_close_explicit",
    "pbs_close_refused",
    "pbs_close_current",
    "pbs_passive",
    "pbs_secret",
    "pbs_secret_select",
    "pbs_native_secret_option",
    "pbs_concise",
    "pbs_native_option",
    "pbs_native_index",
    "pbs_select_values",
    "pbs_transport_cap",
    "pbs_hybrid_variants",
    "pbs_direct_intent",
    "pbs_custom_select",
    "pbs_type_variants",
    "pbs_select_label",
    "pbs_click_variants",
    "pbs_press_variants",
    "pbs_expired",
    "pbs_source_url",
)


@pytest.fixture(autouse=True)
def _reset_trajectory_state(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(session_manager, "get_active_api_key", lambda: "key-a")
    monkeypatch.setattr(
        session_manager,
        "hash_api_key_for_cache",
        lambda api_key: {"key-a": _HASH_A, "key-b": _HASH_B}[api_key],
    )
    set_concise_responses(False)
    set_current_session(SessionState())
    for api_key_hash in (_HASH_A, _HASH_B, None):
        for session_id in _SESSION_IDS:
            delete_trajectory(api_key_hash=api_key_hash, session_id=session_id)
    yield
    set_concise_responses(False)
    set_current_session(SessionState())
    for api_key_hash in (_HASH_A, _HASH_B, None):
        for session_id in _SESSION_IDS:
            delete_trajectory(api_key_hash=api_key_hash, session_id=session_id)


def _patch_browser_page(
    monkeypatch: pytest.MonkeyPatch,
    *,
    session_id: str | None,
    mode: str = "cloud_session",
    api_key_hash: str | None = _HASH_A,
    source_url: str = "https://example.com/catalog",
) -> SimpleNamespace:
    locator = SimpleNamespace(press=AsyncMock(), select_option=AsyncMock(), evaluate=AsyncMock(return_value=False))
    locator.first = locator
    raw_page = SimpleNamespace(locator=MagicMock(return_value=locator))
    page = SimpleNamespace(
        url=source_url,
        page=raw_page,
        click=AsyncMock(return_value="#resolved-submit"),
        evaluate=AsyncMock(return_value=False),
        fill=AsyncMock(),
        keyboard=SimpleNamespace(press=AsyncMock()),
        locator=MagicMock(return_value=locator),
        select_option=AsyncMock(),
        type=AsyncMock(),
    )
    context = BrowserContext(mode=mode, session_id=session_id)
    set_current_session(SessionState(context=context, api_key_hash=api_key_hash))
    monkeypatch.setattr(mcp_browser, "get_page", AsyncMock(return_value=(page, context)))
    monkeypatch.setattr(mcp_browser, "select_native_option_if_targeted", AsyncMock(return_value=None))
    monkeypatch.setenv("SKYVERN_DISABLE_CUSTOM_SELECT", "1")
    return page


@pytest.mark.asyncio
async def test_browser_actions_round_trip_through_trajectory_get_and_synthesize(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = _patch_browser_page(monkeypatch, session_id="pbs_roundtrip")

    async def _click(**_: object) -> str:
        page.url = "https://example.com/results"
        return "#resolved-submit"

    page.click = AsyncMock(side_effect=_click)

    click_result = await mcp_browser.skyvern_click(selector="#submit")
    type_result = await mcp_browser.skyvern_type(text="widget", selector="#search")
    select_result = await mcp_browser.skyvern_select_option(value="west", selector="#region")
    press_result = await mcp_browser.skyvern_press_key(key="Enter", selector="#search")

    assert all(result["ok"] is True for result in (click_result, type_result, select_result, press_result))
    trajectory_result = await mcp_trajectory.skyvern_trajectory_get("pbs_roundtrip")
    entries = json.loads(trajectory_result["data"]["trajectory_json"])
    assert entries == [
        {
            "tool_name": "click",
            "selector": "#resolved-submit",
            "source_url": "https://example.com/catalog",
        },
        {
            "tool_name": "type_text",
            "selector": "#search",
            "source_url": "https://example.com/results",
            "typed_value": "widget",
            "typed_length": 6,
        },
        {
            "tool_name": "select_option",
            "selector": "#region",
            "value": "west",
            "source_url": "https://example.com/results",
        },
        {
            "tool_name": "press_key",
            "key": "Enter",
            "selector": "#search",
            "source_url": "https://example.com/results",
        },
    ]
    assert trajectory_result["data"]["entry_count"] == 4
    assert trajectory_result["data"]["truncated"] is False
    assert trajectory_result["data"]["capture_status"] == "found"

    synthesis_result = await skyvern_code_block_synthesize(trajectory_result["data"]["trajectory_json"])
    assert synthesis_result["ok"] is True
    assert synthesis_result["data"]["code"].strip()


@pytest.mark.asyncio
async def test_native_option_click_records_executed_select(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_browser_page(monkeypatch, session_id="pbs_native_option")
    monkeypatch.setattr(
        mcp_browser,
        "select_native_option_if_targeted",
        AsyncMock(
            return_value=NativeOptionSelection(
                select_selector="#region",
                value="east",
                label="East",
                selected_by="value",
            )
        ),
    )

    result = await mcp_browser.skyvern_click(selector="#region > option")

    assert result["ok"] is True
    assert get_trajectory(api_key_hash=_HASH_A, session_id="pbs_native_option") == (
        [
            {
                "tool_name": "select_option",
                "selector": "#region",
                "source_url": "https://example.com/catalog",
                "value": "east",
            }
        ],
        False,
        True,
    )


@pytest.mark.asyncio
async def test_secret_shaped_native_option_value_is_not_recorded(monkeypatch: pytest.MonkeyPatch) -> None:
    secret = "sk-test-secret-value"
    assert typed_text_looks_secret(secret)
    _patch_browser_page(monkeypatch, session_id="pbs_native_secret_option")
    monkeypatch.setattr(
        mcp_browser,
        "select_native_option_if_targeted",
        AsyncMock(
            return_value=NativeOptionSelection(
                select_selector="#region",
                value=secret,
                label="Secret",
                selected_by="value",
            )
        ),
    )

    result = await mcp_browser.skyvern_click(selector="#region > option")

    assert result["ok"] is True
    assert result["data"]["selected_option"]["value"] == secret
    entries, truncated, found = get_trajectory(api_key_hash=_HASH_A, session_id="pbs_native_secret_option")
    assert (entries, truncated, found) == ([], False, False)
    assert secret not in json.dumps(entries)


@pytest.mark.asyncio
async def test_index_or_label_native_selection_is_not_recorded(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_browser_page(monkeypatch, session_id="pbs_native_index")
    selections = [
        NativeOptionSelection(select_selector="#region", value=None, label="East", index=2, selected_by="index"),
        NativeOptionSelection(select_selector="#region", value="east", label="East", index=2, selected_by="index"),
        NativeOptionSelection(select_selector="#region", value="east", label="East", selected_by="label"),
        NativeOptionSelection(select_selector="#region", value="", label="East", selected_by="value"),
        NativeOptionSelection(select_selector="#region", value=" ", label="East", selected_by="value"),
        NativeOptionSelection(select_selector="#region", value=" east ", label="East", selected_by="value"),
    ]
    for selection in selections:
        monkeypatch.setattr(mcp_browser, "select_native_option_if_targeted", AsyncMock(return_value=selection))
        result = await mcp_browser.skyvern_click(selector="#region > option")
        assert result["ok"] is True

    assert get_trajectory(api_key_hash=_HASH_A, session_id="pbs_native_index") == ([], False, False)


@pytest.mark.asyncio
async def test_hybrid_ai_fallback_actions_are_not_recorded(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_browser_page(monkeypatch, session_id="pbs_hybrid_variants")

    hybrid_type = await mcp_browser.skyvern_type(text="widget", selector="#search", intent="the search field")
    hybrid_select = await mcp_browser.skyvern_select_option(
        value="west", selector="#region", intent="the region dropdown"
    )
    hybrid_press = await mcp_browser.skyvern_press_key(key="Enter", selector="#search", intent="the search field")

    assert all(result["ok"] is True for result in (hybrid_type, hybrid_select, hybrid_press))
    assert get_trajectory(api_key_hash=_HASH_A, session_id="pbs_hybrid_variants") == ([], False, False)


@pytest.mark.asyncio
async def test_direct_mode_with_intent_still_records(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_browser_page(monkeypatch, session_id="pbs_direct_intent")

    direct_type = await mcp_browser.skyvern_type(
        text="widget", selector="#search", intent="the search field", selector_mode="direct"
    )
    direct_select = await mcp_browser.skyvern_select_option(
        value="west", selector="#region", intent="the region dropdown", selector_mode="direct"
    )

    assert direct_type["ok"] is True
    assert direct_select["ok"] is True
    entries, truncated, found = get_trajectory(api_key_hash=_HASH_A, session_id="pbs_direct_intent")
    assert truncated is False
    assert found is True
    assert [(entry["tool_name"], entry["selector"]) for entry in entries] == [
        ("type_text", "#search"),
        ("select_option", "#region"),
    ]


@pytest.mark.asyncio
async def test_unfaithful_action_variants_are_not_recorded(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_browser_page(monkeypatch, session_id="pbs_type_variants")

    append_type = await mcp_browser.skyvern_type(text="widget", selector="#search", clear=False)
    intent_type = await mcp_browser.skyvern_type(text="widget", intent="the search field")

    assert append_type["ok"] is True
    assert intent_type["ok"] is True
    assert get_trajectory(api_key_hash=_HASH_A, session_id="pbs_type_variants") == ([], False, False)


@pytest.mark.asyncio
async def test_custom_widget_selection_is_not_recorded(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_browser_page(monkeypatch, session_id="pbs_custom_select")
    monkeypatch.delenv("SKYVERN_DISABLE_CUSTOM_SELECT")
    monkeypatch.setattr(mcp_browser, "do_select_option", AsyncMock(return_value="Music"))

    result = await mcp_browser.skyvern_select_option(selector="#category", value="music")

    assert result["ok"] is True
    assert get_trajectory(api_key_hash=_HASH_A, session_id="pbs_custom_select") == ([], False, False)


@pytest.mark.asyncio
async def test_unreplayable_select_values_are_not_recorded(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_browser_page(monkeypatch, session_id="pbs_select_values")

    empty_value = await mcp_browser.skyvern_select_option(value="", selector="#region")
    padded_value = await mcp_browser.skyvern_select_option(value=" west ", selector="#region")

    assert empty_value["ok"] is True
    assert padded_value["ok"] is True
    assert get_trajectory(api_key_hash=_HASH_A, session_id="pbs_select_values") == ([], False, False)


@pytest.mark.asyncio
async def test_secret_shaped_select_value_is_not_recorded(monkeypatch: pytest.MonkeyPatch) -> None:
    secret = "sk-test-secret-value"
    assert typed_text_looks_secret(secret)
    _patch_browser_page(monkeypatch, session_id="pbs_secret_select")

    result = await mcp_browser.skyvern_select_option(value=secret, selector="#region")

    assert result["ok"] is True
    assert result["data"]["value"] == secret
    entries, truncated, found = get_trajectory(api_key_hash=_HASH_A, session_id="pbs_secret_select")
    assert (entries, truncated, found) == ([], False, False)
    assert secret not in json.dumps(entries)


@pytest.mark.asyncio
async def test_by_label_selection_is_not_recorded(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_browser_page(monkeypatch, session_id="pbs_select_label")

    result = await mcp_browser.skyvern_select_option(selector="#region", value="East", by_label=True)

    assert result["ok"] is True
    assert get_trajectory(api_key_hash=_HASH_A, session_id="pbs_select_label") == ([], False, False)


@pytest.mark.asyncio
async def test_click_only_records_plain_left_click(monkeypatch: pytest.MonkeyPatch) -> None:
    page = _patch_browser_page(monkeypatch, session_id="pbs_click_variants")

    double_click = await mcp_browser.skyvern_click(selector="#submit", click_count=2)
    right_click = await mcp_browser.skyvern_click(selector="#submit", button="right")
    left_click = await mcp_browser.skyvern_click(selector="#submit", button="left")
    page.click.return_value = None
    unresolved_click = await mcp_browser.skyvern_click(intent="the submit button")

    assert all(result["ok"] is True for result in (double_click, right_click, left_click, unresolved_click))
    assert get_trajectory(api_key_hash=_HASH_A, session_id="pbs_click_variants") == (
        [
            {
                "tool_name": "click",
                "selector": "#resolved-submit",
                "source_url": "https://example.com/catalog",
            }
        ],
        False,
        True,
    )


@pytest.mark.asyncio
async def test_click_ai_fallback_only_records_resolved_selector(monkeypatch: pytest.MonkeyPatch) -> None:
    page = _patch_browser_page(monkeypatch, session_id="pbs_click_variants")
    page.click = AsyncMock(side_effect=[None, "#resolved-submit"])

    unresolved = await mcp_browser.skyvern_click(selector="#stale-submit", intent="the submit button")
    resolved = await mcp_browser.skyvern_click(selector="#stale-submit", intent="the submit button")

    assert unresolved["ok"] is True
    assert resolved["ok"] is True
    assert get_trajectory(api_key_hash=_HASH_A, session_id="pbs_click_variants") == (
        [
            {
                "tool_name": "click",
                "selector": "#resolved-submit",
                "source_url": "https://example.com/catalog",
            }
        ],
        False,
        True,
    )


@pytest.mark.asyncio
async def test_press_key_skips_intent_only_and_keeps_page_level(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_browser_page(monkeypatch, session_id="pbs_press_variants")

    intent_press = await mcp_browser.skyvern_press_key(key="Enter", intent="the search field")
    page_press = await mcp_browser.skyvern_press_key(key="Escape")
    blank_intent_press = await mcp_browser.skyvern_press_key(key="Enter", selector="#search", intent="")

    assert intent_press["ok"] is True
    assert page_press["ok"] is True
    assert blank_intent_press["ok"] is True
    assert get_trajectory(api_key_hash=_HASH_A, session_id="pbs_press_variants") == (
        [
            {
                "tool_name": "press_key",
                "key": "Escape",
                "source_url": "https://example.com/catalog",
            },
            {
                "tool_name": "press_key",
                "key": "Enter",
                "selector": "#search",
                "source_url": "https://example.com/catalog",
            },
        ],
        False,
        True,
    )


@pytest.mark.asyncio
async def test_press_key_only_records_named_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_browser_page(monkeypatch, session_id="pbs_press_variants")

    character_results = [
        await mcp_browser.skyvern_press_key(key=key, selector="#otp") for key in ("1", "2", "3", "Shift+1")
    ]

    assert all(result["ok"] is True for result in character_results)
    assert get_trajectory(api_key_hash=_HASH_A, session_id="pbs_press_variants") == ([], False, False)

    named_results = [
        await mcp_browser.skyvern_press_key(key=key, selector="#search") for key in ("Enter", "Control+Enter", "F1")
    ]

    assert all(result["ok"] is True for result in named_results)
    entries, truncated, found = get_trajectory(api_key_hash=_HASH_A, session_id="pbs_press_variants")
    assert truncated is False
    assert found is True
    assert [entry["key"] for entry in entries] == ["Enter", "Control+Enter", "F1"]


@pytest.mark.asyncio
async def test_sessionless_browser_action_does_not_capture(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_browser_page(monkeypatch, session_id=None, mode="local")
    append = Mock()
    monkeypatch.setattr(mcp_browser, "append_trajectory_entry", append)

    result = await mcp_browser.skyvern_click(selector="#submit")

    assert result["ok"] is True
    append.assert_not_called()
    assert get_trajectory(api_key_hash=_HASH_A, session_id="pbs_sessionless") == ([], False, False)


@pytest.mark.asyncio
async def test_failed_browser_action_does_not_capture(monkeypatch: pytest.MonkeyPatch) -> None:
    page = _patch_browser_page(monkeypatch, session_id="pbs_failed_action")
    page.click = AsyncMock(side_effect=RuntimeError("click failed"))

    result = await mcp_browser.skyvern_click(selector="#submit")

    assert result["ok"] is False
    assert get_trajectory(api_key_hash=_HASH_A, session_id="pbs_failed_action") == ([], False, False)


@pytest.mark.asyncio
async def test_empty_type_preserves_zero_typed_length(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_browser_page(monkeypatch, session_id="pbs_empty_type")

    result = await mcp_browser.skyvern_type(text="", selector="#search")

    assert result["ok"] is True
    entries, truncated, found = get_trajectory(api_key_hash=_HASH_A, session_id="pbs_empty_type")
    assert truncated is False
    assert found is True
    assert entries == [
        {
            "tool_name": "type_text",
            "selector": "#search",
            "source_url": "https://example.com/catalog",
            "typed_length": 0,
        }
    ]


@pytest.mark.asyncio
async def test_trajectory_get_uses_active_principal_instead_of_stored_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    append_trajectory_entry(
        api_key_hash=_HASH_A,
        session_id="pbs_isolation",
        entry={"tool_name": "click", "selector": "#submit"},
    )
    set_current_session(SessionState(api_key_hash=_HASH_A))
    active_key = ["key-b"]
    monkeypatch.setattr(session_manager, "get_active_api_key", lambda: active_key[0])

    foreign = await mcp_trajectory.skyvern_trajectory_get("pbs_isolation")
    never_existed = await mcp_trajectory.skyvern_trajectory_get("pbs_never_existed")
    assert foreign == never_existed
    assert foreign["data"] == {
        "trajectory_json": "[]",
        "entry_count": 0,
        "truncated": False,
        "capture_status": "not_found",
    }

    active_key[0] = "key-a"
    owner = await mcp_trajectory.skyvern_trajectory_get("pbs_isolation")
    assert owner["data"]["entry_count"] == 1
    assert owner["data"]["capture_status"] == "found"


@pytest.mark.asyncio
async def test_expired_trajectory_reports_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    now = [100.0]
    monkeypatch.setattr(trajectory_store, "TTL_SECONDS", 10)
    monkeypatch.setattr(trajectory_store.time, "monotonic", lambda: now[0])
    append_trajectory_entry(
        api_key_hash=_HASH_A,
        session_id="pbs_expired",
        entry={"tool_name": "click", "selector": "#submit"},
    )
    set_current_session(SessionState(api_key_hash=_HASH_A))
    now[0] = 111.0

    result = await mcp_trajectory.skyvern_trajectory_get("pbs_expired")

    assert result["data"] == {
        "trajectory_json": "[]",
        "entry_count": 0,
        "truncated": False,
        "capture_status": "not_found",
    }


@pytest.mark.asyncio
async def test_explicit_session_close_deletes_trajectory(monkeypatch: pytest.MonkeyPatch) -> None:
    append_trajectory_entry(
        api_key_hash=_HASH_A,
        session_id="pbs_close_explicit",
        entry={"tool_name": "click", "selector": "#submit"},
    )
    append_trajectory_entry(
        api_key_hash=_HASH_B,
        session_id="pbs_close_explicit",
        entry={"tool_name": "click", "selector": "#other-principal"},
    )
    set_current_session(SessionState(api_key_hash=_HASH_A))
    fake_skyvern = SimpleNamespace(get_browser_session=AsyncMock(side_effect=RuntimeError("not retained")))
    monkeypatch.setattr(mcp_session, "get_skyvern", lambda: fake_skyvern)
    monkeypatch.setattr(
        mcp_session,
        "do_session_close",
        AsyncMock(return_value=SessionCloseResult(session_id="pbs_close_explicit", closed=True)),
    )

    result = await mcp_session.skyvern_browser_session_close(session_id="pbs_close_explicit")

    assert result["ok"] is True
    assert get_trajectory(api_key_hash=_HASH_A, session_id="pbs_close_explicit") == ([], False, False)
    assert get_trajectory(api_key_hash=_HASH_B, session_id="pbs_close_explicit") == ([], False, False)


@pytest.mark.asyncio
async def test_failed_backend_close_preserves_trajectory(monkeypatch: pytest.MonkeyPatch) -> None:
    append_trajectory_entry(
        api_key_hash=_HASH_A,
        session_id="pbs_close_refused",
        entry={"tool_name": "click", "selector": "#submit"},
    )
    set_current_session(SessionState(api_key_hash=_HASH_A))
    monkeypatch.setattr(mcp_session, "get_skyvern", lambda: SimpleNamespace())
    monkeypatch.setattr(mcp_session, "do_session_close", AsyncMock(side_effect=RuntimeError("backend refused")))

    result = await mcp_session.skyvern_browser_session_close(session_id="pbs_close_refused")

    assert result["ok"] is False
    assert get_trajectory(api_key_hash=_HASH_A, session_id="pbs_close_refused") == (
        [{"tool_name": "click", "selector": "#submit"}],
        False,
        True,
    )


@pytest.mark.asyncio
async def test_current_session_close_deletes_trajectory(monkeypatch: pytest.MonkeyPatch) -> None:
    append_trajectory_entry(
        api_key_hash=_HASH_A,
        session_id="pbs_close_current",
        entry={"tool_name": "click", "selector": "#submit"},
    )
    browser = MagicMock()
    browser._browser_session_id = "pbs_close_current"
    browser.close = AsyncMock()
    context = BrowserContext(mode="cloud_session", session_id="pbs_close_current")
    set_current_session(SessionState(browser=browser, context=context, api_key_hash=_HASH_A))
    fake_skyvern = SimpleNamespace(get_browser_session=AsyncMock(side_effect=RuntimeError("not retained")))
    monkeypatch.setattr(mcp_session, "get_skyvern", lambda: fake_skyvern)
    monkeypatch.setattr(session_manager, "get_skyvern", lambda: fake_skyvern)
    monkeypatch.setattr(
        "skyvern.cli.core.session_ops.do_session_close",
        AsyncMock(return_value=SessionCloseResult(session_id="pbs_close_current", closed=True)),
    )

    result = await mcp_session.skyvern_browser_session_close()

    assert result["ok"] is True
    assert get_trajectory(api_key_hash=_HASH_A, session_id="pbs_close_current") == ([], False, False)


@pytest.mark.asyncio
async def test_recorder_failure_is_logged_and_does_not_change_tool_result(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_browser_page(monkeypatch, session_id="pbs_passive")
    set_concise_responses(True)
    monkeypatch.setattr(mcp_browser, "append_trajectory_entry", Mock())
    baseline = await mcp_browser.skyvern_click(selector="#submit")

    reached = False

    class RecorderSentinel(RuntimeError):
        pass

    def _raise(**_: object) -> None:
        nonlocal reached
        reached = True
        raise RecorderSentinel("armed")

    warning = Mock()
    monkeypatch.setattr(mcp_browser, "append_trajectory_entry", _raise)
    monkeypatch.setattr(mcp_browser.LOG, "warning", warning)

    result = await mcp_browser.skyvern_click(selector="#submit")

    assert reached is True
    warning.assert_called_once()
    assert result == baseline
    assert result["ok"] is True


@pytest.mark.asyncio
async def test_secret_shaped_type_value_never_reaches_store_or_getter(monkeypatch: pytest.MonkeyPatch) -> None:
    secret = "sk-test-secret-value"
    assert typed_text_looks_secret(secret)
    _patch_browser_page(monkeypatch, session_id="pbs_secret")

    result = await mcp_browser.skyvern_type(text=secret, selector="#search")

    assert result["ok"] is True
    entries, truncated, found = get_trajectory(api_key_hash=_HASH_A, session_id="pbs_secret")
    assert truncated is False
    assert found is True
    assert entries == [
        {
            "tool_name": "type_text",
            "selector": "#search",
            "source_url": "https://example.com/catalog",
            "typed_length": len(secret),
        }
    ]
    serialized_store = json.dumps(entries)
    trajectory_result = await mcp_trajectory.skyvern_trajectory_get("pbs_secret")
    assert secret not in serialized_store
    assert secret not in trajectory_result["data"]["trajectory_json"]
    assert "raw_typed_value" not in trajectory_result["data"]["trajectory_json"]


@pytest.mark.asyncio
async def test_source_url_is_scrubbed_before_storage(monkeypatch: pytest.MonkeyPatch) -> None:
    secret = "abc123"
    _patch_browser_page(
        monkeypatch,
        session_id="pbs_source_url",
        source_url=f"https://example.com/page?session={secret}&q=widgets",
    )

    result = await mcp_browser.skyvern_click(selector="#submit")
    trajectory_result = await mcp_trajectory.skyvern_trajectory_get("pbs_source_url")

    assert result["ok"] is True
    trajectory_json = trajectory_result["data"]["trajectory_json"]
    assert "session=__redacted__" in trajectory_json
    assert "q=widgets" in trajectory_json
    assert secret not in trajectory_json


@pytest.mark.asyncio
async def test_password_selector_is_rejected_without_append(monkeypatch: pytest.MonkeyPatch) -> None:
    append = Mock()
    get_page = AsyncMock(side_effect=AssertionError("password rejection must happen before session resolution"))
    monkeypatch.setattr(mcp_browser, "append_trajectory_entry", append)
    monkeypatch.setattr(mcp_browser, "get_page", get_page)

    result = await mcp_browser.skyvern_type(text="not-stored", selector="#password")

    assert result["ok"] is False
    append.assert_not_called()
    get_page.assert_not_awaited()


@pytest.mark.asyncio
async def test_full_store_response_fits_mcp_transport_cap() -> None:
    from skyvern.cli.core.trajectory_store import MAX_BYTES
    from skyvern.cli.mcp_tools.response import MCP_MAX_RESPONSE_CHARS

    set_current_session(SessionState(api_key_hash=_HASH_A))
    # Quote/backslash-heavy values maximize JSON-in-JSON escaping inflation.
    hostile_value = ('"\\' + "x" * 6) * 100
    for index in range(300):
        append_trajectory_entry(
            api_key_hash=_HASH_A,
            session_id="pbs_transport_cap",
            entry={"tool_name": "type_text", "selector": f"#field-{index}", "typed_value": hostile_value},
        )

    result = await mcp_trajectory.skyvern_trajectory_get("pbs_transport_cap")

    assert result["data"]["truncated"] is True
    entries = json.loads(result["data"]["trajectory_json"])
    assert entries
    serialized = json.dumps(result)
    assert MAX_BYTES < MCP_MAX_RESPONSE_CHARS
    assert len(serialized) < MCP_MAX_RESPONSE_CHARS


@pytest.mark.asyncio
async def test_trajectory_get_preserves_all_data_keys_in_concise_mode() -> None:
    set_current_session(SessionState(api_key_hash=_HASH_A))
    set_concise_responses(True)

    result = await mcp_trajectory.skyvern_trajectory_get("pbs_concise")

    assert result == {
        "ok": True,
        "data": {
            "trajectory_json": "[]",
            "entry_count": 0,
            "truncated": False,
            "capture_status": "not_found",
        },
    }
