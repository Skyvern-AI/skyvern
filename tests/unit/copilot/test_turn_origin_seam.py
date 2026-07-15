from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from structlog.testing import capture_logs

from skyvern.forge.sdk.copilot.runtime import (
    AgentContext,
    ensure_browser_session,
    mcp_browser_context,
    resolve_browser_state_for_context,
)
from skyvern.forge.sdk.copilot.turn_origin import (
    HealAdoptionFailed,
    TurnOrigin,
    make_self_heal_session_id,
)


class _FakeBrowser:
    def __init__(self, *, connected: bool = True) -> None:
        self._connected = connected

    def is_connected(self) -> bool:
        return self._connected


class _FakeBrowserContext:
    def __init__(self, *, connected: bool = True, closed: bool = False) -> None:
        self.browser = _FakeBrowser(connected=connected)
        self._impl_obj = SimpleNamespace(_close_was_called=closed, _closed=closed)


def _make_stream() -> MagicMock:
    stream = MagicMock()
    stream.is_disconnected = AsyncMock(return_value=False)
    return stream


def _make_ctx(**overrides: object) -> AgentContext:
    defaults = dict(
        organization_id="org_1",
        workflow_id="wf_1",
        workflow_permanent_id="wpid_1",
        workflow_yaml="",
        browser_session_id=None,
        stream=_make_stream(),
        api_key="test-api-key",
        turn_origin=TurnOrigin.interactive,
        injected_browser_state=None,
        heal_workflow_run_id=None,
    )
    defaults.update(overrides)
    return AgentContext(**defaults)


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", [TurnOrigin.runtime_self_heal, "runtime_self_heal"])
async def test_ensure_browser_session_self_heal_requires_injected_state_no_auto_create(
    monkeypatch: pytest.MonkeyPatch,
    origin: object,
) -> None:
    import skyvern.forge.sdk.copilot.runtime as runtime

    create_session = AsyncMock(side_effect=AssertionError("create_session must not run for runtime_self_heal"))
    get_browser_state = AsyncMock(side_effect=AssertionError("get_browser_state must not run for runtime_self_heal"))
    mock_manager = SimpleNamespace(create_session=create_session, get_browser_state=get_browser_state)
    monkeypatch.setattr(runtime, "app", SimpleNamespace(PERSISTENT_SESSIONS_MANAGER=mock_manager))

    ctx = _make_ctx(
        turn_origin=origin,
        injected_browser_state=None,
        heal_workflow_run_id="wr_1",
    )

    with pytest.raises(HealAdoptionFailed, match="injected_browser_state_missing"):
        await ensure_browser_session(ctx)

    create_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_synthetic_session_unregisters_when_context_body_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import skyvern.forge.sdk.copilot.runtime as runtime

    fake_browser_state = SimpleNamespace(
        browser_context=_FakeBrowserContext(),
        get_working_page=AsyncMock(return_value=MagicMock()),
    )
    monkeypatch.setattr(runtime, "app", SimpleNamespace(PERSISTENT_SESSIONS_MANAGER=SimpleNamespace()))
    monkeypatch.setattr(runtime, "get_skyvern", lambda: MagicMock())
    monkeypatch.setattr(runtime, "SkyvernBrowser", lambda *a, **kw: MagicMock())
    monkeypatch.setattr(runtime, "get_active_api_key", lambda: "sk-test-key")
    monkeypatch.setattr(runtime, "hash_api_key_for_cache", lambda key: f"hash_{key}")
    monkeypatch.setattr(runtime, "set_api_key_override", lambda _key: "token")
    monkeypatch.setattr(runtime, "reset_api_key_override", lambda _token: None)
    unregister_mock = MagicMock()
    monkeypatch.setattr(runtime, "register_copilot_session", MagicMock())
    monkeypatch.setattr(runtime, "unregister_copilot_session", unregister_mock)

    ctx = _make_ctx(
        turn_origin=TurnOrigin.runtime_self_heal,
        injected_browser_state=fake_browser_state,
        heal_workflow_run_id="wr_777",
    )

    with pytest.raises(RuntimeError, match="boom"):
        async with mcp_browser_context(ctx):
            raise RuntimeError("boom")

    unregister_mock.assert_called_once_with(make_self_heal_session_id("wr_777"))


@pytest.mark.asyncio
async def test_self_heal_injected_state_assigns_synthetic_session_and_mcp_registers_and_unregisters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import skyvern.forge.sdk.copilot.runtime as runtime

    fake_browser_state = SimpleNamespace(
        browser_context=_FakeBrowserContext(),
        get_working_page=AsyncMock(return_value=MagicMock()),
    )
    manager = SimpleNamespace(
        get_browser_state=AsyncMock(side_effect=AssertionError("persistent lookup must not run for runtime_self_heal"))
    )
    monkeypatch.setattr(runtime, "app", SimpleNamespace(PERSISTENT_SESSIONS_MANAGER=manager))
    monkeypatch.setattr(runtime, "get_skyvern", lambda: MagicMock())
    monkeypatch.setattr(runtime, "SkyvernBrowser", lambda *a, **kw: MagicMock())
    monkeypatch.setattr(runtime, "get_active_api_key", lambda: "sk-test-key")
    monkeypatch.setattr(runtime, "hash_api_key_for_cache", lambda key: f"hash_{key}")
    monkeypatch.setattr(runtime, "set_api_key_override", lambda _key: "token")
    monkeypatch.setattr(runtime, "reset_api_key_override", lambda _token: None)
    register_mock = MagicMock()
    unregister_mock = MagicMock()
    monkeypatch.setattr(runtime, "register_copilot_session", register_mock)
    monkeypatch.setattr(runtime, "unregister_copilot_session", unregister_mock)

    workflow_run_id = "wr_123"
    expected_session_id = make_self_heal_session_id(workflow_run_id)
    ctx = _make_ctx(
        turn_origin=TurnOrigin.runtime_self_heal,
        injected_browser_state=fake_browser_state,
        heal_workflow_run_id=workflow_run_id,
    )

    result = await ensure_browser_session(ctx)
    assert result is None
    assert ctx.browser_session_id == expected_session_id

    with capture_logs() as logs:
        async with mcp_browser_context(ctx):
            assert register_mock.call_args.args[0] == expected_session_id

    unregister_mock.assert_called_once_with(expected_session_id)
    manager.get_browser_state.assert_not_awaited()
    registered = [entry for entry in logs if entry["event"] == "registered self-heal browser session"]
    unregistered = [entry for entry in logs if entry["event"] == "unregistered self-heal browser session"]
    assert len(registered) == 1
    assert registered[0]["session_id"] == expected_session_id
    assert registered[0]["organization_id"] == "org_1"
    assert len(unregistered) == 1
    assert unregistered[0]["session_id"] == expected_session_id


@pytest.mark.asyncio
async def test_self_heal_mcp_session_preserves_adopted_working_page_when_not_last_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A SWITCH_TAB-pinned tab (RealBrowserState.get_working_page() honoring
    set_active_page) need not be pages[-1]. The registered SessionState must carry that
    exact page, not fall back to a fresh SkyvernBrowser.get_working_page() -> pages[-1]."""
    import skyvern.forge.sdk.copilot.runtime as runtime

    last_page = MagicMock(name="last_page")
    pinned_page = MagicMock(name="pinned_page_not_last")
    fake_context = _FakeBrowserContext()
    fake_context.pages = [pinned_page, last_page]

    fake_browser_state = SimpleNamespace(
        browser_context=fake_context,
        get_working_page=AsyncMock(return_value=pinned_page),
    )
    monkeypatch.setattr(runtime, "app", SimpleNamespace(PERSISTENT_SESSIONS_MANAGER=SimpleNamespace()))
    monkeypatch.setattr(runtime, "get_skyvern", lambda: MagicMock())
    monkeypatch.setattr(runtime, "SkyvernBrowser", lambda *a, **kw: MagicMock())
    monkeypatch.setattr(runtime, "get_active_api_key", lambda: "sk-test-key")
    monkeypatch.setattr(runtime, "hash_api_key_for_cache", lambda key: f"hash_{key}")
    monkeypatch.setattr(runtime, "set_api_key_override", lambda _key: "token")
    monkeypatch.setattr(runtime, "reset_api_key_override", lambda _token: None)
    register_mock = MagicMock()
    monkeypatch.setattr(runtime, "register_copilot_session", register_mock)
    monkeypatch.setattr(runtime, "unregister_copilot_session", MagicMock())

    ctx = _make_ctx(
        turn_origin=TurnOrigin.runtime_self_heal,
        injected_browser_state=fake_browser_state,
        heal_workflow_run_id="wr_switch_tab",
    )

    async with mcp_browser_context(ctx):
        registered_state = register_mock.call_args.args[1]

    assert registered_state._active_page is pinned_page
    assert registered_state._active_page is not fake_context.pages[-1]


@pytest.mark.asyncio
async def test_ensure_browser_session_prefix_guard_never_looks_up_selfheal_in_persistent_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import skyvern.forge.sdk.copilot.runtime as runtime

    monkeypatch.setattr(
        runtime,
        "_get_persistent_browser_session",
        AsyncMock(side_effect=AssertionError("selfheal ids must not reach persistent lookup")),
    )

    created = SimpleNamespace(persistent_browser_session_id="pbs_created")
    ready_state = SimpleNamespace(browser_context=_FakeBrowserContext())
    manager = SimpleNamespace(
        create_session=AsyncMock(return_value=created),
        get_browser_state=AsyncMock(return_value=ready_state),
    )
    monkeypatch.setattr(runtime, "app", SimpleNamespace(PERSISTENT_SESSIONS_MANAGER=manager))
    monkeypatch.setattr(runtime, "_BROWSER_BOOT_POLL_INTERVAL_SECONDS", 0.0)

    ctx = _make_ctx(turn_origin=TurnOrigin.interactive, browser_session_id="selfheal:wr_old")
    result = await ensure_browser_session(ctx)

    assert result is None
    assert ctx.browser_session_id == "pbs_created"
    manager.create_session.assert_awaited_once()


@pytest.mark.asyncio
async def test_ensure_browser_session_interactive_still_auto_creates(monkeypatch: pytest.MonkeyPatch) -> None:
    import skyvern.forge.sdk.copilot.runtime as runtime

    created = SimpleNamespace(persistent_browser_session_id="pbs_new")
    ready_state = SimpleNamespace(browser_context=_FakeBrowserContext())
    manager = SimpleNamespace(
        create_session=AsyncMock(return_value=created),
        get_browser_state=AsyncMock(return_value=ready_state),
    )
    monkeypatch.setattr(runtime, "app", SimpleNamespace(PERSISTENT_SESSIONS_MANAGER=manager))
    monkeypatch.setattr(runtime, "_BROWSER_BOOT_POLL_INTERVAL_SECONDS", 0.0)

    ctx = _make_ctx(turn_origin=TurnOrigin.interactive, browser_session_id=None)
    result = await ensure_browser_session(ctx)

    assert result is None
    assert ctx.browser_session_id == "pbs_new"
    manager.create_session.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("origin", "session_id"),
    [
        pytest.param(TurnOrigin.runtime_self_heal, "selfheal:wr_123", id="runtime_self_heal_origin"),
        pytest.param(TurnOrigin.interactive, "selfheal:wr_123", id="selfheal_prefix_session"),
    ],
)
async def test_resolve_browser_state_for_context_skips_persistent_lookup_for_self_heal_paths(
    monkeypatch: pytest.MonkeyPatch,
    origin: TurnOrigin,
    session_id: str | None,
) -> None:
    import skyvern.forge.sdk.copilot.runtime as runtime

    fake_browser_state = SimpleNamespace(
        browser_context=_FakeBrowserContext(),
        get_working_page=AsyncMock(return_value=MagicMock()),
    )
    persistent_lookup = AsyncMock(side_effect=AssertionError("persistent lookup must not run for self-heal paths"))
    manager = SimpleNamespace(get_browser_state=persistent_lookup)
    monkeypatch.setattr(runtime, "app", SimpleNamespace(PERSISTENT_SESSIONS_MANAGER=manager))

    ctx = _make_ctx(
        turn_origin=origin,
        injected_browser_state=fake_browser_state,
        heal_workflow_run_id="wr_123",
        browser_session_id=session_id,
    )

    resolved = await resolve_browser_state_for_context(ctx, session_id=session_id)

    assert resolved is fake_browser_state
    persistent_lookup.assert_not_awaited()
