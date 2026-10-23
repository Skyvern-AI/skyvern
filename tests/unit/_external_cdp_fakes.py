import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta, timezone, tzinfo
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from skyvern.config import settings
from skyvern.forge.agent_functions import AgentFunction
from skyvern.forge.api_app import create_api_app
from skyvern.forge.sdk.db.datetime_utils import naive_utc_now
from skyvern.forge.sdk.db.models import Base, PersistentBrowserSessionModel
from skyvern.forge.sdk.db.repositories.browser_sessions import BrowserSessionsRepository
from skyvern.forge.sdk.encrypt import encryptor
from skyvern.forge.sdk.encrypt.bootstrap import register_aes_encryptor
from skyvern.forge.sdk.routes import browser_sessions as routes_mod
from skyvern.forge.sdk.schemas import persistent_browser_sessions as persistent_browser_sessions_mod
from skyvern.forge.sdk.schemas.persistent_browser_sessions import EXTERNAL_CDP_BROWSER_VENDOR, PersistentBrowserSession
from skyvern.forge.sdk.services import org_auth_service
from skyvern.webeye import external_cdp_sessions as external_mod
from skyvern.webeye.browser_artifacts import BrowserArtifacts
from skyvern.webeye.browser_state import BrowserState
from skyvern.webeye.default_persistent_sessions_manager import DefaultPersistentSessionsManager
from skyvern.webeye.persistent_sessions_manager import PersistentSessionsManager
from skyvern.webeye.real_browser_state import RealBrowserState
from tests.unit.scoped_asyncio import ScopedAsyncio

TOKEN = "tok-9f3c1d7e5b"
CDP_URL = f"wss://127.0.0.1:9333/devtools/browser/b1?token={TOKEN}"
ORG_A = "org_a"
ORG_B = "org_b"


def external_browser_state(pages: int = 0) -> tuple[RealBrowserState, MagicMock, MagicMock]:
    """A real external RealBrowserState over a driver and context that record what close touches."""
    browser = MagicMock()
    browser.is_connected.return_value = True
    browser.close = AsyncMock()
    context = MagicMock()
    context.pages = [MagicMock(url="https://example.test/", close=AsyncMock()) for _ in range(pages)]
    context.browser = browser
    context.close = AsyncMock()
    context.new_page = AsyncMock()
    context._impl_obj = SimpleNamespace(
        _close_was_called=False, _closed=False, _connection=SimpleNamespace(_closed_error=None)
    )
    context._skyvern_cdp_download_interceptor = None
    pw = MagicMock()
    pw.stop = AsyncMock()
    state = RealBrowserState(
        pw=pw,
        browser_context=context,
        browser_artifacts=BrowserArtifacts(),
        release_driver_on_close=True,
        external_browser=True,
    )
    return state, context, pw


def stall_external_detach(context: MagicMock, pw: MagicMock) -> MagicMock:
    """Make detach hang in interceptor disable; returns the driver process a kill would hit."""
    context._skyvern_cdp_download_interceptor = SimpleNamespace(disable=AsyncMock(side_effect=asyncio.Event().wait))
    driver_process = MagicMock(returncode=None)
    pw._impl_obj._connection._transport._proc = driver_process
    return driver_process


def external_session_row(
    *, session_id: str = "pbs_external", organization_id: str = "org_123", started_minutes_ago: int = 1
) -> PersistentBrowserSession:
    started_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=started_minutes_ago)
    return PersistentBrowserSession(
        persistent_browser_session_id=session_id,
        organization_id=organization_id,
        status="running",
        browser_vendor=EXTERNAL_CDP_BROWSER_VENDOR,
        upstream_cdp_url=CDP_URL,
        started_at=started_at,
        timeout_minutes=60,
        created_at=started_at,
        modified_at=started_at,
    )


@asynccontextmanager
async def external_cdp_stack() -> AsyncIterator[SimpleNamespace]:
    """The registration route and the OSS manager over one in-memory database, authenticated as ORG_A."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, tables=[PersistentBrowserSessionModel.__table__])
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    repo = BrowserSessionsRepository(session_factory=session_factory)
    http_app = create_api_app()
    DefaultPersistentSessionsManager.instance = None
    DefaultPersistentSessionsManager._browser_sessions = {}
    DefaultPersistentSessionsManager._background_tasks = set()
    DefaultPersistentSessionsManager._close_cleanup_tasks = {}
    DefaultPersistentSessionsManager._external_connect_locks = {}
    DefaultPersistentSessionsManager._external_deadlines = {}
    manager = DefaultPersistentSessionsManager(database=SimpleNamespace(browser_sessions=repo))
    caller = SimpleNamespace(organization_id=ORG_A)
    http_app.dependency_overrides[org_auth_service.get_current_org] = lambda: SimpleNamespace(
        organization_id=caller.organization_id
    )
    route_app = SimpleNamespace(
        DATABASE=manager.database, PERSISTENT_SESSIONS_MANAGER=manager, STORAGE=None, AGENT_FUNCTION=AgentFunction()
    )
    try:
        with (
            patch.object(routes_mod, "app", route_app),
            patch.object(settings, "LOG_RAW_API_REQUESTS", True),
            patch.object(settings, "ENV", "local"),
            patch.dict(encryptor._methods),
        ):
            register_aes_encryptor(secret_key="external-cdp-test-key")
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=http_app), base_url="http://test") as client:
                yield SimpleNamespace(
                    client=client, manager=manager, repo=repo, session_factory=session_factory, caller=caller
                )
    finally:
        for deadline in manager._external_deadlines.values():
            deadline.cancel()
        await engine.dispose()


class DeadlineClock:
    """The clock registration deadlines are read against and the deadline watcher's sleep: frozen until a test sets
    ``ticking``, after which each watcher poll moves it by exactly what the poll slept. Concurrent watchers share it,
    so it moves by the sum of their sleeps."""

    def __init__(self) -> None:
        self._now = datetime.now(UTC)
        self.ticking = asyncio.Event()
        self.polls: list[float] = []

    def now(self, tz: tzinfo) -> datetime:
        return self._now.astimezone(tz)

    async def sleep(self, seconds: float) -> None:
        self.polls.append(seconds)
        await self.ticking.wait()
        self._now += timedelta(seconds=seconds)


def install_deadline_clock(monkeypatch: pytest.MonkeyPatch) -> DeadlineClock:
    clock = DeadlineClock()
    monkeypatch.setattr(persistent_browser_sessions_mod, "datetime", clock)
    monkeypatch.setattr(external_mod, "asyncio", ScopedAsyncio(sleep=clock.sleep))
    return clock


async def backdate_registration(
    stack: SimpleNamespace, session_id: str, minutes: float, *, clock: DeadlineClock | None = None
) -> None:
    now = naive_utc_now() if clock is None else clock.now(UTC).replace(tzinfo=None)
    async with stack.session_factory() as session:
        await session.execute(
            update(PersistentBrowserSessionModel)
            .where(PersistentBrowserSessionModel.persistent_browser_session_id == session_id)
            .values(started_at=now - timedelta(minutes=minutes))
        )
        await session.commit()


async def register_external(client: httpx.AsyncClient, timeout: int | None = None) -> httpx.Response:
    probe, _, _ = external_browser_state()
    body: dict[str, str | int] = {"cdp_url": CDP_URL}
    if timeout is not None:
        body["timeout"] = timeout
    with patch.object(routes_mod, "connect_external_cdp_browser", AsyncMock(return_value=probe)):
        return await client.post("/v1/browser_sessions/external", json=body)


async def begin_lease(
    manager: PersistentSessionsManager, session_id: str, runnable_id: str, runnable_type: str = "task"
) -> str:
    return await manager.begin_session(
        browser_session_id=session_id, runnable_type=runnable_type, runnable_id=runnable_id, organization_id=ORG_A
    )


async def acquire_lease(
    manager: PersistentSessionsManager, session_id: str, runnable_id: str, generation: str
) -> BrowserState | None:
    return await manager.get_browser_state(
        session_id,
        ORG_A,
        acquire=True,
        expected_runnable_id=runnable_id,
        expected_runnable_generation_id=generation,
    )


async def release_lease(manager: PersistentSessionsManager, session_id: str, runnable_id: str, generation: str) -> bool:
    return await manager.release_browser_session(
        session_id, ORG_A, expected_runnable_id=runnable_id, expected_runnable_generation_id=generation
    )


async def release_lease_by(
    stack: SimpleNamespace,
    manager: PersistentSessionsManager,
    session_id: str,
    runnable_id: str,
    generation: str,
    state: BrowserState,
    *,
    stale: bool,
) -> bool:
    """Release through the owner path, or through the trusted stale-owner path over the cached wrapper."""
    if not stale:
        return await release_lease(manager, session_id, runnable_id, generation)
    observed = datetime.now(timezone.utc).replace(tzinfo=None)
    async with stack.session_factory() as session:
        await session.execute(
            update(PersistentBrowserSessionModel)
            .where(PersistentBrowserSessionModel.persistent_browser_session_id == session_id)
            .values(last_activity_at=observed)
        )
        await session.commit()
    return await manager.release_stale_browser_session(
        session_id,
        ORG_A,
        expected_runnable_id=runnable_id,
        expected_runnable_generation_id=generation,
        expected_browser_state=state,
        observed_last_activity_at=observed,
    )
