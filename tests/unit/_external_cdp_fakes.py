from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from skyvern.config import settings
from skyvern.forge.api_app import create_api_app
from skyvern.forge.sdk.db.models import Base, PersistentBrowserSessionModel
from skyvern.forge.sdk.db.repositories.browser_sessions import BrowserSessionsRepository
from skyvern.forge.sdk.encrypt import encryptor
from skyvern.forge.sdk.encrypt.bootstrap import register_aes_encryptor
from skyvern.forge.sdk.routes import browser_sessions as routes_mod
from skyvern.forge.sdk.schemas.persistent_browser_sessions import EXTERNAL_CDP_BROWSER_VENDOR, PersistentBrowserSession
from skyvern.forge.sdk.services import org_auth_service
from skyvern.webeye.browser_artifacts import BrowserArtifacts
from skyvern.webeye.default_persistent_sessions_manager import DefaultPersistentSessionsManager
from skyvern.webeye.real_browser_state import RealBrowserState

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
    manager = DefaultPersistentSessionsManager(database=SimpleNamespace(browser_sessions=repo))
    caller = SimpleNamespace(organization_id=ORG_A)
    http_app.dependency_overrides[org_auth_service.get_current_org] = lambda: SimpleNamespace(
        organization_id=caller.organization_id
    )
    route_app = SimpleNamespace(DATABASE=manager.database, PERSISTENT_SESSIONS_MANAGER=manager, STORAGE=None)
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
        await engine.dispose()


async def register_external(client: httpx.AsyncClient, timeout: int | None = None) -> httpx.Response:
    probe, _, _ = external_browser_state()
    body: dict[str, str | int] = {"cdp_url": CDP_URL}
    if timeout is not None:
        body["timeout"] = timeout
    with patch.object(routes_mod, "connect_external_cdp_browser", AsyncMock(return_value=probe)):
        return await client.post("/v1/browser_sessions/external", json=body)
