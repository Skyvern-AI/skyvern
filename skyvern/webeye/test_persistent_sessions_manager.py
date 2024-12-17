from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select

from skyvern.forge.sdk.db.models import PersistentBrowserSessionModel
from skyvern.webeye.browser_factory import BrowserState
from skyvern.webeye.persistent_sessions_manager import PersistentSessionsManager


class MockPage(AsyncMock):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.url = None
        self.goto = AsyncMock()
        self.close = AsyncMock()


class MockBrowserContext(AsyncMock):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.new_page = AsyncMock(return_value=MockPage())
        self.tracing = MagicMock(stop=AsyncMock())
        self._close_callbacks = []

        async def close_with_event():
            for callback in self._close_callbacks:
                try:
                    callback()
                except Exception as e:
                    print(f"Error in close callback: {e}")

        self.close = AsyncMock(side_effect=close_with_event)

    def on(self, event_name, callback):
        if event_name == "close":
            print(f"Registering close callback: {callback}")
            self._close_callbacks.append(callback)


class MockPlaywright(AsyncMock):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.chromium = MagicMock(
            launch=AsyncMock(return_value=MagicMock(new_context=AsyncMock(return_value=MockBrowserContext())))
        )


@pytest.fixture(autouse=True)
def mock_browser_factory():
    """Mock the entire BrowserContextFactory class to prevent any real browser operations"""
    with patch("skyvern.webeye.persistent_sessions_manager.BrowserContextFactory") as mock:
        browser_context = MockBrowserContext()

        mock.create_browser_context = AsyncMock(
            return_value=(
                browser_context,
                MagicMock(
                    video_artifacts=[],
                    har_path=None,
                    browser_console_log_path=None,
                    traces_dir=None,
                ),
                AsyncMock(),
            )
        )
        yield mock


@pytest.fixture
def mock_playwright():
    with patch("skyvern.webeye.persistent_sessions_manager.async_playwright") as mock:
        mock.return_value.start = AsyncMock(return_value=MockPlaywright())
        yield mock


@pytest.fixture
def mock_browser_state_class(mock_browser_factory):
    """Mock the BrowserState class itself"""
    with patch("skyvern.webeye.persistent_sessions_manager.BrowserState") as mock:
        mock_instance = MagicMock(spec=BrowserState)
        mock_instance.get_or_create_page = AsyncMock(return_value=MockPage())
        mock_instance.get_working_page = AsyncMock(return_value=MockPage())
        mock_instance.close = AsyncMock()
        mock_instance.page = MockPage()
        mock_instance.browser_context = mock_browser_factory.create_browser_context.return_value[0]

        # Make the mock class return our configured instance
        mock.return_value = mock_instance
        yield mock


@pytest.fixture
async def sessions_manager(mock_playwright, mock_browser_state_class):
    manager = PersistentSessionsManager()
    # Clear any existing sessions
    manager.sessions = {}
    yield manager
    await manager.close()


@pytest.fixture
async def db_session():
    async with get_async_session() as session:
        yield session


async def test_create_session(sessions_manager, db_session):
    org_id = "test_org"
    session_id, browser_state = await sessions_manager.create_session(organization_id=org_id, url="https://example.com")

    # Check browser state
    assert isinstance(browser_state, BrowserState)
    assert session_id in sessions_manager._browser_states

    # Check database record
    result = await db_session.execute(
        select(PersistentBrowserSessionModel).where(
            PersistentBrowserSessionModel.persistent_browser_session_id == session_id
        )
    )
    db_session = result.scalar_one()
    assert db_session.organization_id == org_id
    assert db_session.deleted_at is None


async def test_get_session(sessions_manager):
    org_id = "test_org"
    session_id, browser_state = await sessions_manager.create_session(organization_id=org_id)

    retrieved_state = sessions_manager.get_session(org_id, session_id)
    assert retrieved_state == browser_state

    # Test non-existent session
    assert sessions_manager.get_session(org_id, "non_existent") is None
    assert sessions_manager.get_session("non_existent_org", session_id) is None


async def test_get_active_session_ids(sessions_manager):
    org_id = "test_org"

    # Create multiple sessions
    session_id1, _ = await sessions_manager.create_session(organization_id=org_id)
    session_id2, _ = await sessions_manager.create_session(organization_id=org_id)

    active_sessions = sessions_manager.get_active_session_ids(org_id)
    assert len(active_sessions) == 2
    assert session_id1 in active_sessions
    assert session_id2 in active_sessions

    # Test non-existent organization
    assert len(sessions_manager.get_active_session_ids("non_existent_org")) == 0


async def test_close_session(sessions_manager, db_session):
    org_id = "test_org"
    session_id, browser_state = await sessions_manager.create_session(organization_id=org_id)

    await sessions_manager.close_session(org_id, session_id)

    # Verify browser state was removed
    assert session_id not in sessions_manager._browser_states

    # Verify database record was marked as deleted
    result = await db_session.execute(
        select(PersistentBrowserSessionModel).where(
            PersistentBrowserSessionModel.persistent_browser_session_id == session_id
        )
    )
    db_session = result.scalar_one()
    assert db_session.deleted_at is not None


async def test_close_all_sessions(sessions_manager):
    org_id = "test_org"

    # Create multiple sessions
    session_id1, _ = await sessions_manager.create_session(organization_id=org_id)
    session_id2, _ = await sessions_manager.create_session(organization_id=org_id)

    assert len(sessions_manager.get_active_session_ids(org_id)) == 2

    await sessions_manager.close_all_sessions(org_id)

    # Verify all sessions were removed
    assert len(sessions_manager.get_active_session_ids(org_id)) == 0
    assert org_id not in sessions_manager.sessions


async def test_multiple_organizations(sessions_manager):
    org_id1 = "test_org_1"
    org_id2 = "test_org_2"

    # Create sessions for different organizations
    session_id1, _ = await sessions_manager.create_session(organization_id=org_id1)
    session_id2, _ = await sessions_manager.create_session(organization_id=org_id2)

    # Verify sessions are properly separated
    assert session_id1 in sessions_manager.get_active_session_ids(org_id1)
    assert session_id2 in sessions_manager.get_active_session_ids(org_id2)
    assert session_id1 not in sessions_manager.get_active_session_ids(org_id2)
    assert session_id2 not in sessions_manager.get_active_session_ids(org_id1)


async def test_browser_context_close_removes_session(sessions_manager):
    org_id = "test_org"
    session_id, browser_state = await sessions_manager.create_session(organization_id=org_id, url="https://example.com")

    # Verify session exists
    assert session_id in sessions_manager.get_active_session_ids(org_id)

    # Simulate browser context close event
    await browser_state.browser_context.close()

    # Verify the session was removed
    assert session_id not in sessions_manager.get_active_session_ids(org_id)
    assert org_id not in sessions_manager.sessions
