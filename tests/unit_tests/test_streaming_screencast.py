from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from tests.unit_tests._stub_streaming import import_with_stubs

screencast = import_with_stubs(
    "skyvern.forge.sdk.routes.streaming.screencast",
    extra_stubs=["skyvern.forge.sdk.routes.streaming.screenshot"],
)


def _make_app(browser_manager=None, persistent_sessions_manager=None):
    """Build a fake app namespace to replace screencast.app (an AppHolder proxy)."""
    return SimpleNamespace(
        BROWSER_MANAGER=browser_manager or SimpleNamespace(),
        PERSISTENT_SESSIONS_MANAGER=persistent_sessions_manager or SimpleNamespace(),
    )


@pytest.mark.asyncio
async def test_resolve_browser_state_for_workflow_run(monkeypatch: pytest.MonkeyPatch) -> None:
    expected_state = object()
    fake_app = _make_app(
        browser_manager=SimpleNamespace(get_for_workflow_run=Mock(return_value=expected_state), get_for_task=Mock()),
        persistent_sessions_manager=SimpleNamespace(get_observer_browser_state=AsyncMock()),
    )
    monkeypatch.setattr(screencast, "app", fake_app)

    result = await screencast._resolve_browser_state("wr_123", "workflow_run")

    assert result is expected_state
    fake_app.BROWSER_MANAGER.get_for_workflow_run.assert_called_once_with("wr_123")
    fake_app.BROWSER_MANAGER.get_for_task.assert_not_called()
    fake_app.PERSISTENT_SESSIONS_MANAGER.get_observer_browser_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_resolve_browser_state_for_task(monkeypatch: pytest.MonkeyPatch) -> None:
    expected_state = object()
    fake_app = _make_app(
        browser_manager=SimpleNamespace(get_for_workflow_run=Mock(), get_for_task=Mock(return_value=expected_state)),
        persistent_sessions_manager=SimpleNamespace(get_observer_browser_state=AsyncMock()),
    )
    monkeypatch.setattr(screencast, "app", fake_app)

    result = await screencast._resolve_browser_state("task_123", "task", workflow_run_id="wr_123")

    assert result is expected_state
    fake_app.BROWSER_MANAGER.get_for_task.assert_called_once_with("task_123", "wr_123")
    fake_app.BROWSER_MANAGER.get_for_workflow_run.assert_not_called()
    fake_app.PERSISTENT_SESSIONS_MANAGER.get_observer_browser_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_resolve_browser_state_for_browser_session(monkeypatch: pytest.MonkeyPatch) -> None:
    expected_state = object()
    fake_app = _make_app(
        browser_manager=SimpleNamespace(get_for_workflow_run=Mock(), get_for_task=Mock()),
        persistent_sessions_manager=SimpleNamespace(get_observer_browser_state=AsyncMock(return_value=expected_state)),
    )
    monkeypatch.setattr(screencast, "app", fake_app)

    result = await screencast._resolve_browser_state("bs_123", "browser_session", organization_id="o_123")

    assert result is expected_state
    fake_app.PERSISTENT_SESSIONS_MANAGER.get_observer_browser_state.assert_awaited_once_with("bs_123", "o_123")
    fake_app.BROWSER_MANAGER.get_for_workflow_run.assert_not_called()
    fake_app.BROWSER_MANAGER.get_for_task.assert_not_called()


@pytest.mark.asyncio
async def test_resolve_browser_state_unknown_entity_type(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_app = _make_app(
        browser_manager=SimpleNamespace(get_for_workflow_run=Mock(), get_for_task=Mock()),
        persistent_sessions_manager=SimpleNamespace(get_observer_browser_state=AsyncMock()),
    )
    monkeypatch.setattr(screencast, "app", fake_app)

    result = await screencast._resolve_browser_state("id_123", "unknown")

    assert result is None
    fake_app.BROWSER_MANAGER.get_for_workflow_run.assert_not_called()
    fake_app.BROWSER_MANAGER.get_for_task.assert_not_called()
    fake_app.PERSISTENT_SESSIONS_MANAGER.get_observer_browser_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_wait_for_browser_state_returns_when_working_page_is_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    browser_state = SimpleNamespace(get_working_page=AsyncMock(return_value=object()))
    resolve_mock = AsyncMock(return_value=browser_state)
    sleep_mock = AsyncMock()
    monkeypatch.setattr(screencast, "_resolve_browser_state", resolve_mock)
    monkeypatch.setattr(screencast.asyncio, "sleep", sleep_mock)

    result = await screencast.wait_for_browser_state("wr_123", "workflow_run", timeout=1, poll_interval=0.1)

    assert result is browser_state
    resolve_mock.assert_awaited_once_with("wr_123", "workflow_run", None, organization_id=None)
    browser_state.get_working_page.assert_awaited_once()
    sleep_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_wait_for_browser_state_returns_none_on_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    browser_state = SimpleNamespace(get_working_page=AsyncMock(return_value=None))
    resolve_mock = AsyncMock(return_value=browser_state)
    sleep_mock = AsyncMock()
    monkeypatch.setattr(screencast, "_resolve_browser_state", resolve_mock)
    monkeypatch.setattr(screencast.asyncio, "sleep", sleep_mock)

    result = await screencast.wait_for_browser_state(
        "wr_123",
        "workflow_run",
        timeout=0.3,
        poll_interval=0.1,
    )

    assert result is None
    assert resolve_mock.await_count == 3
    assert browser_state.get_working_page.await_count == 3


@pytest.mark.parametrize(
    ("entity_id", "entity_type", "kwargs"),
    [
        ("wr_1", "workflow_run", {}),
        ("tsk_1", "task", {"workflow_run_id": "wr_1"}),
    ],
)
@pytest.mark.asyncio
async def test_a_run_without_in_process_state_never_adopts_the_session(
    monkeypatch: pytest.MonkeyPatch, entity_id: str, entity_type: str, kwargs: dict
) -> None:
    """A run's frames come from the worker that drives it, so the API process resolves a run only
    against its own manager. Reaching into the session instead would adopt a browser the worker is
    driving — and would miss anyway, since a task inside a workflow leases under the run's id."""
    fake_app = _make_app(
        browser_manager=SimpleNamespace(
            get_for_workflow_run=Mock(return_value=None), get_for_task=Mock(return_value=None)
        ),
        persistent_sessions_manager=SimpleNamespace(
            get_session_by_runnable_id=AsyncMock(),
            get_browser_state=AsyncMock(),
            get_observer_browser_state=AsyncMock(),
        ),
    )
    monkeypatch.setattr(screencast, "app", fake_app)

    assert await screencast._resolve_browser_state(entity_id, entity_type, organization_id="o_1", **kwargs) is None

    fake_app.PERSISTENT_SESSIONS_MANAGER.get_session_by_runnable_id.assert_not_awaited()
    fake_app.PERSISTENT_SESSIONS_MANAGER.get_browser_state.assert_not_awaited()
    fake_app.PERSISTENT_SESSIONS_MANAGER.get_observer_browser_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_waiting_out_a_session_holds_one_connection_and_gives_it_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each resolve mints its own connection, so re-resolving per poll would leave one adopted
    browser per tick behind."""
    browser_state = SimpleNamespace(get_working_page=AsyncMock(return_value=None))
    release = AsyncMock()
    fake_app = _make_app(
        persistent_sessions_manager=SimpleNamespace(
            get_observer_browser_state=AsyncMock(return_value=browser_state),
            release_observer_browser_state=release,
        ),
    )
    monkeypatch.setattr(screencast, "app", fake_app)
    monkeypatch.setattr(screencast.asyncio, "sleep", AsyncMock())

    result = await screencast.wait_for_browser_state(
        "bs_123", "browser_session", organization_id="o_1", timeout=0.3, poll_interval=0.1
    )

    assert result is None
    assert fake_app.PERSISTENT_SESSIONS_MANAGER.get_observer_browser_state.await_count == 1
    assert browser_state.get_working_page.await_count == 3
    release.assert_awaited_once_with("bs_123", browser_state)


@pytest.mark.asyncio
async def test_following_the_active_page_does_not_reconnect(monkeypatch: pytest.MonkeyPatch) -> None:
    page = object()
    browser_state = SimpleNamespace(get_working_page=AsyncMock(return_value=page))
    fake_app = _make_app(
        persistent_sessions_manager=SimpleNamespace(get_observer_browser_state=AsyncMock()),
    )
    monkeypatch.setattr(screencast, "app", fake_app)

    result = await screencast._resolve_working_page(browser_state, "bs_123", "browser_session", organization_id="o_1")

    assert result is page
    fake_app.PERSISTENT_SESSIONS_MANAGER.get_observer_browser_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_run_state_is_not_the_viewers_to_release(monkeypatch: pytest.MonkeyPatch) -> None:
    release = AsyncMock()
    fake_app = _make_app(
        persistent_sessions_manager=SimpleNamespace(release_observer_browser_state=release),
    )
    monkeypatch.setattr(screencast, "app", fake_app)

    await screencast.release_browser_state(SimpleNamespace(), "workflow_run", "wr_1")

    release.assert_not_awaited()
