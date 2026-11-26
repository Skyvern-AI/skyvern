import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest

from skyvern.config import settings
from skyvern.forge.sdk.artifact.storage.local import LocalStorage
from skyvern.forge.sdk.artifact.storage.s3 import S3Storage
from skyvern.forge.sdk.encrypt.files import FILE_MAGIC
from skyvern.schemas.browser_session_close import BrowserSessionCloseReason
from skyvern.webeye import default_persistent_sessions_manager as manager_mod
from skyvern.webeye.browser_artifacts import BrowserArtifacts
from skyvern.webeye.browser_factory import BrowserContextFactory
from skyvern.webeye.default_persistent_sessions_manager import BrowserSession, DefaultPersistentSessionsManager
from skyvern.webeye.persistent_sessions_manager import PBS_TASK_RUNNABLE_TYPE
from skyvern.webeye.profile_cookie_merge import BANKED_COOKIES_FILENAME
from skyvern.webeye.real_browser_state import RealBrowserState
from skyvern.webeye.session_cookies import read_persisted_session_cookies
from tests.unit.forge.sdk.artifact.storage.test_helpers import (
    ProfileTransport,
    artifact_root,
    install_s3_profile_transport,
)
from tests.unit.forge.sdk.test_create_browser_profile_from_session_route import _session as _profile_session
from tests.unit.test_browser_context_factory_marker import _factory_harness, _stub_local_launch


class _LaunchBrowserSessionsRepository:
    def __init__(self) -> None:
        self.updates: list[dict[str, object]] = []
        self.session = SimpleNamespace(
            persistent_browser_session_id="pbs_local",
            status="created",
            completed_at=None,
            proxy_location=None,
            proxy_session_id=None,
            browser_profile_id=None,
            profile_read_only=False,
            browser_address=None,
            upstream_cdp_url=None,
            started_at=None,
            browser_settings=None,
            browser_settings_receipt=None,
        )

    async def create_persistent_browser_session(self, **kwargs: object) -> SimpleNamespace:
        return self.session

    async def get_persistent_browser_session(self, session_id: str, organization_id: str) -> SimpleNamespace:
        return self.session

    async def update_persistent_browser_session(
        self,
        session_id: str,
        *,
        organization_id: str,
        **updates: object,
    ) -> SimpleNamespace:
        self.updates.append(updates)
        for name, value in updates.items():
            setattr(self.session, name, value)
        return self.session

    async def set_persistent_browser_session_browser_address(
        self,
        browser_session_id: str,
        browser_address: str | None,
        ip_address: str | None,
        ecs_task_arn: str | None,
        organization_id: str | None = None,
        upstream_cdp_url: str | None = None,
    ) -> None:
        raise AssertionError("launch readiness must be published in the status update")


def _launch_manager() -> tuple[DefaultPersistentSessionsManager, _LaunchBrowserSessionsRepository]:
    DefaultPersistentSessionsManager.instance = None
    DefaultPersistentSessionsManager._browser_sessions = {}
    repository = _LaunchBrowserSessionsRepository()
    database = SimpleNamespace(browser_sessions=repository)
    return DefaultPersistentSessionsManager(database=database), repository


@pytest.fixture
def manager() -> DefaultPersistentSessionsManager:
    DefaultPersistentSessionsManager.instance = None
    DefaultPersistentSessionsManager._browser_sessions = {}
    DefaultPersistentSessionsManager._background_tasks = set()
    DefaultPersistentSessionsManager._close_cleanup_tasks = {}
    DefaultPersistentSessionsManager._reaper_task = None
    db = MagicMock()
    db.browser_sessions.get_persistent_browser_session = AsyncMock()
    db.browser_sessions.close_persistent_browser_session = AsyncMock()
    db.browser_sessions.archive_browser_session_address = AsyncMock()
    db.browser_sessions.record_persistent_browser_session_close_reason = AsyncMock()
    return DefaultPersistentSessionsManager(database=db)


@pytest.mark.asyncio
async def test_close_session_skips_in_memory_export_for_mismatched_org(
    manager: DefaultPersistentSessionsManager,
) -> None:
    browser_state = MagicMock()
    browser_state.close = AsyncMock()
    browser_state.browser_artifacts = SimpleNamespace(
        browser_session_dir="/tmp/pbs_foreign",
        video_artifacts=[],
    )
    storage = MagicMock()
    storage.store_browser_profile = AsyncMock()

    manager._browser_sessions["pbs_foreign"] = BrowserSession(
        browser_state=browser_state,
        organization_id="org_owner",
    )

    with (
        patch.object(manager_mod, "app", SimpleNamespace(STORAGE=storage)),
        patch.object(manager_mod, "persist_session_cookies", new=AsyncMock()) as persist_session_cookies,
        patch.object(manager_mod.settings, "BROWSER_STREAMING_MODE", "vnc"),
    ):
        await manager.close_session("org_requester", "pbs_foreign")

    persist_session_cookies.assert_not_awaited()
    storage.store_browser_profile.assert_not_awaited()
    browser_state.close.assert_not_awaited()
    assert "pbs_foreign" in manager._browser_sessions
    manager.database.browser_sessions.get_persistent_browser_session.assert_not_awaited()
    manager.database.browser_sessions.close_persistent_browser_session.assert_awaited_once_with(
        "pbs_foreign",
        "org_requester",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode",
    [
        "legacy",
        "encrypted",
        "encrypted-read-only",
        "encrypted-failed-seed",
        "fork",
        "reuse",
        "stale-seed",
        "deleted-seed",
        "legacy-reuse",
        "legacy-fork",
        "legacy-remote-reuse",
        "legacy-stale-seed",
        "legacy-deleted-seed",
        "remote-fork",
        "remote-reuse",
        "remote-failed-close",
    ],
)
async def test_close_session_exports_and_closes_for_matching_org(
    manager: DefaultPersistentSessionsManager,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    mode: str,
) -> None:
    if mode != "legacy":
        await _assert_persistent_profile_export(manager, monkeypatch, tmp_path, mode)
        return
    browser_state = MagicMock()
    browser_state.close = AsyncMock()
    browser_state.browser_context = MagicMock()
    browser_state.profile_read_only = False
    browser_state.browser_artifacts = BrowserArtifacts(browser_session_dir="/tmp/pbs_owned")
    storage = MagicMock()
    storage.store_browser_profile = AsyncMock()
    persisted_session = MagicMock()
    persisted_session.generate_browser_profile = True
    persisted_session.should_export_profile.return_value = True
    manager.database.browser_sessions.get_persistent_browser_session.return_value = persisted_session

    cached = BrowserSession(
        browser_state=browser_state,
        organization_id="org_owner",
    )
    manager._browser_sessions["pbs_owned"] = cached

    with (
        patch.object(manager_mod, "app", SimpleNamespace(STORAGE=storage)),
        patch.object(manager_mod, "persist_session_cookies", new=AsyncMock()) as persist_session_cookies,
        patch.object(manager_mod.settings, "BROWSER_STREAMING_MODE", "vnc"),
    ):
        await manager.close_session("org_owner", "pbs_owned")

    persist_session_cookies.assert_awaited_once_with(browser_state.browser_context, "/tmp/pbs_owned")
    manager.database.browser_sessions.get_persistent_browser_session.assert_awaited_once_with(
        "pbs_owned",
        "org_owner",
    )
    storage.store_browser_profile.assert_awaited_once_with(
        organization_id="org_owner",
        profile_id="pbs_owned",
        directory="/tmp/pbs_owned",
        require_encryption=False,
    )
    browser_state.close.assert_awaited_once()
    assert cached.retirement.reason == "session_ending"
    assert "pbs_owned" not in manager._browser_sessions
    manager.database.browser_sessions.close_persistent_browser_session.assert_awaited_once_with(
        "pbs_owned",
        "org_owner",
    )


async def _assert_persistent_profile_export(
    manager: DefaultPersistentSessionsManager, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, mode: str
) -> None:
    legacy_seed = mode.startswith("legacy-")
    mode = mode.removeprefix("legacy-")
    monkeypatch.setattr(settings, "BROWSER_SESSION_BASE_PATH", str(tmp_path / "storage"))
    monkeypatch.setattr(settings, "TEMP_PATH", str(tmp_path / "temp"))
    monkeypatch.setattr(settings, "SKYVERN_ARTIFACT_ENCRYPTION_KEYS", (artifact_root(),))
    monkeypatch.setattr(settings, "SKYVERN_ENCRYPT_BROWSER_PROFILES", not legacy_seed)
    working_dir = tmp_path / "profile"
    working_dir.mkdir()
    remote = mode.startswith("remote-")
    storage = S3Storage() if remote else LocalStorage()
    transport = ProfileTransport()
    if isinstance(storage, S3Storage):
        monkeypatch.setattr(settings, "AWS_S3_BUCKET_BROWSER_SESSIONS", "profile-test")
        install_s3_profile_transport(storage, transport, monkeypatch)
    cookie = {"name": "session", "value": "persisted", "domain": "fixture.invalid", "path": "/", "expires": -1}
    context = MagicMock()
    context.cookies = AsyncMock(return_value=[cookie])
    playwright = MagicMock()
    artifacts = BrowserArtifacts(browser_session_dir=str(working_dir))
    cleanup = None
    fork = mode in {"fork", "remote-fork"}
    seeded = fork or remote or mode in {"reuse", "stale-seed", "deleted-seed"}
    if seeded:
        (working_dir / "seed").write_text("before")
        (working_dir / BANKED_COOKIES_FILENAME).write_text("[]")
        await storage.store_browser_profile("org_owner", "bp_seed", str(working_dir))
        monkeypatch.setattr(settings, "SKYVERN_ENCRYPT_BROWSER_PROFILES", False)
        monkeypatch.setattr(settings, "BROWSER_TYPE", "chromium-headless")
        _factory_harness(monkeypatch, storage=storage)
        from skyvern.webeye import browser_factory

        monkeypatch.setattr(
            browser_factory.app.AGENT_FUNCTION, "should_apply_banked_cookies", AsyncMock(return_value=True)
        )
        playwright = _stub_local_launch(monkeypatch, tmp_path)
        playwright.chromium.launch_persistent_context = AsyncMock(return_value=context)
        _, artifacts, cleanup = await BrowserContextFactory.create_browser_context(
            playwright,
            organization_id="org_owner",
            browser_profile_id="bp_seed",
            browser_session_id="pbs_owned",
            _profile_export_fork=fork,
        )
        assert artifacts._seed_profile_encrypted is not legacy_seed
        if legacy_seed and not remote:
            durable = tmp_path / "storage" / "org_owner" / "profiles" / "bp_seed"
            assert (Path(artifacts.browser_session_dir or "") == durable) is not fork
            if mode in {"stale-seed", "deleted-seed"}:
                # Simulate Chromium closing after the durable profile was deleted or migrated.
                context.cookies.return_value = []

        if mode == "stale-seed":
            (working_dir / "seed").write_text("newer")
            await storage.store_browser_profile(
                "org_owner", "bp_seed", str(working_dir), require_encryption=legacy_seed
            )
        elif mode == "deleted-seed":
            await storage.delete_browser_profile("org_owner", "bp_seed", hard_delete=True)
    uploads_before_close = transport.objects.copy()

    async def close_context() -> None:
        if not legacy_seed:
            assert transport.objects == uploads_before_close or fork
        if mode == "remote-failed-close":
            raise RuntimeError("context close failed")
        if Path(str(artifacts.browser_session_dir)).is_dir():
            (Path(str(artifacts.browser_session_dir)) / "closed").touch()

    playwright.stop = AsyncMock()
    context.close = close_context
    state = RealBrowserState(
        pw=playwright, browser_context=context, browser_artifacts=artifacts, browser_cleanup=cleanup
    )
    state.profile_read_only = mode == "encrypted-read-only"
    if mode == "encrypted-failed-seed":
        artifacts.mark_seed_load_failed()
    manager._browser_sessions["pbs_owned"] = BrowserSession(browser_state=state, organization_id="org_owner")
    manager.database.browser_sessions.get_persistent_browser_session.return_value = _profile_session(
        persistent_browser_session_id="pbs_owned",
        organization_id="org_owner",
        browser_profile_id="bp_seed" if seeded else None,
        profile_read_only=state.profile_read_only,
        generate_browser_profile=fork or not seeded,
    )
    with (
        patch.object(manager_mod, "app", SimpleNamespace(STORAGE=storage)),
        patch.object(manager_mod.settings, "BROWSER_STREAMING_MODE", "vnc"),
    ):
        await manager.close_session("org_owner", "pbs_owned")

    assert "pbs_owned" not in manager._browser_sessions
    if seeded:
        seed = await storage.retrieve_browser_profile_snapshot("org_owner", "bp_seed")
        assert (seed is None) == (mode == "deleted-seed")
        exported_ok = fork or mode == "remote-reuse"
        assert await storage.browser_profile_exists("org_owner", "pbs_owned") is exported_ok
        if seed:
            reused = mode == "reuse"
            assert seed.encrypted is (not legacy_seed or mode == "stale-seed")
            assert read_persisted_session_cookies(seed.directory) == ([cookie] if reused else [])
            assert (Path(seed.directory) / "seed").read_text() == ("newer" if mode == "stale-seed" else "before")
            assert (Path(seed.directory) / "closed").exists() is reused
            if mode == "reuse":
                assert not (Path(seed.directory) / BANKED_COOKIES_FILENAME).exists()
        if exported_ok:
            assert all(transport.objects[uri] == content for uri, content in uploads_before_close.items())
            assert all(uri.endswith(".zip.enc") is not legacy_seed for uri in transport.objects)
            exported = await storage.retrieve_browser_profile_snapshot("org_owner", "pbs_owned")
            assert exported and exported.encrypted is not legacy_seed
            assert read_persisted_session_cookies(exported.directory) == [cookie]
        if mode == "remote-failed-close":
            assert transport.objects == uploads_before_close
        return
    files = list((tmp_path / "storage").rglob("*.enc"))
    if mode != "encrypted":
        assert files == []
        return
    (sealed,) = files
    assert sealed.name == "pbs_owned.zip.enc" and sealed.read_bytes().startswith(FILE_MAGIC)
    restored = await storage.retrieve_browser_profile("org_owner", "pbs_owned")
    assert read_persisted_session_cookies(restored) == [cookie]


@pytest.mark.asyncio
async def test_close_session_records_the_reason_before_closing_the_row(
    manager: DefaultPersistentSessionsManager,
) -> None:
    """SKY-15022: the row close resolves its terminal status from the recorded reason."""
    events: list[str] = []
    manager.database.browser_sessions.record_persistent_browser_session_close_reason = AsyncMock(
        side_effect=lambda *_: events.append("reason")
    )
    manager.database.browser_sessions.close_persistent_browser_session = AsyncMock(
        side_effect=lambda *_: events.append("close")
    )

    with patch.object(manager_mod.settings, "BROWSER_STREAMING_MODE", "vnc"):
        await manager.close_session("org_owner", "pbs_aborted", reason=BrowserSessionCloseReason.aborted)

    manager.database.browser_sessions.record_persistent_browser_session_close_reason.assert_awaited_once_with(
        "pbs_aborted", "org_owner", "aborted"
    )
    assert events == ["reason", "close"]


@pytest.mark.asyncio
async def test_cancelled_close_retains_browser_and_port_cleanup(
    manager: DefaultPersistentSessionsManager,
) -> None:
    persist_started = asyncio.Event()
    allow_persist = asyncio.Event()

    async def _persist(*_args: object) -> None:
        persist_started.set()
        await allow_persist.wait()

    browser_state = MagicMock()
    browser_state.close = AsyncMock()
    browser_state.browser_context = MagicMock()
    browser_state.profile_read_only = False
    browser_state.browser_artifacts = BrowserArtifacts(browser_session_dir="/tmp/pbs_cancelled")
    persisted_session = MagicMock()
    persisted_session.generate_browser_profile = False
    persisted_session.should_export_profile.return_value = False
    manager.database.browser_sessions.get_persistent_browser_session.return_value = persisted_session
    manager._browser_sessions["pbs_cancelled"] = BrowserSession(
        browser_state=browser_state,
        organization_id="org_owner",
        cdp_port=9234,
    )

    with (
        patch.object(manager_mod, "app", SimpleNamespace(STORAGE=MagicMock())),
        patch.object(manager_mod, "persist_session_cookies", new=AsyncMock(side_effect=_persist)),
        patch.object(manager_mod, "_release_cdp_port") as release_cdp_port,
        patch.object(manager_mod.settings, "BROWSER_STREAMING_MODE", "vnc"),
    ):
        close_task = asyncio.create_task(manager.close_session("org_owner", "pbs_cancelled"))
        await asyncio.wait_for(persist_started.wait(), timeout=5)
        close_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await close_task

        retry_task = asyncio.create_task(manager.close_session("org_owner", "pbs_cancelled"))
        await asyncio.sleep(0)
        assert not retry_task.done()

        allow_persist.set()
        await retry_task
        for _ in range(10):
            if browser_state.close.await_count:
                break
            await asyncio.sleep(0)

        browser_state.close.assert_awaited_once_with()
        release_cdp_port.assert_called_once_with(9234)
        manager.database.browser_sessions.close_persistent_browser_session.assert_awaited_once_with(
            "pbs_cancelled",
            "org_owner",
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("owner_is_final,still_active", [(False, True), (True, False)])
async def test_owning_run_is_active_resolves_standalone_task_owners(
    manager: DefaultPersistentSessionsManager,
    owner_is_final: bool,
    still_active: bool,
) -> None:
    """A standalone task lease writes a runnable_type that is not a RunType member, so without an
    explicit branch the reaper reads it as unresolvable and protects the row forever."""
    task = MagicMock()
    task.status.is_final.return_value = owner_is_final
    manager.database.tasks.get_task = AsyncMock(return_value=task)

    assert await manager._owning_run_is_active("tsk_owner", PBS_TASK_RUNNABLE_TYPE, "org_1") is still_active
    manager.database.tasks.get_task.assert_awaited_once_with("tsk_owner", organization_id="org_1")


@pytest.mark.asyncio
async def test_owning_run_is_active_releases_a_task_that_no_longer_exists(
    manager: DefaultPersistentSessionsManager,
) -> None:
    manager.database.tasks.get_task = AsyncMock(return_value=None)

    assert await manager._owning_run_is_active("tsk_gone", PBS_TASK_RUNNABLE_TYPE, "org_1") is False


@pytest.mark.asyncio
async def test_launch_standalone_browser_publishes_cdp_address() -> None:
    manager, repository = _launch_manager()
    browser_state = MagicMock()
    browser_state.get_or_create_page = AsyncMock(return_value=MagicMock())
    browser_state.close = AsyncMock()
    browser_manager = MagicMock()
    browser_manager._create_browser_state = AsyncMock(return_value=browser_state)
    agent_function = MagicMock()
    agent_function.build_proxy_session_extra_http_headers.return_value = {}
    cdp_url = "ws://127.0.0.1:9242/devtools/browser/local"
    cdp_writer = MagicMock()
    cdp_writer.wait_closed = AsyncMock()
    open_connection = AsyncMock(return_value=(MagicMock(), cdp_writer))
    get_json = AsyncMock(return_value={"webSocketDebuggerUrl": cdp_url})

    with (
        patch.object(
            manager_mod, "app", SimpleNamespace(BROWSER_MANAGER=browser_manager, AGENT_FUNCTION=agent_function)
        ),
        patch.object(manager_mod.settings, "BROWSER_TYPE", "chromium-headful"),
        patch.object(manager_mod, "_allocate_cdp_port", return_value=9242),
        patch.object(manager_mod.asyncio, "open_connection", new=open_connection),
        patch.object(manager_mod, "aiohttp_get_json", new=get_json),
    ):
        await manager._launch_browser_for_session("pbs_local", "org_local")

    open_connection.assert_awaited_once_with("127.0.0.1", 9242)
    cdp_writer.close.assert_called_once_with()
    cdp_writer.wait_closed.assert_awaited_once_with()
    get_json.assert_awaited_once_with(
        "http://127.0.0.1:9242/json/version",
        retry=2,
        retry_timeout=0.25,
        timeout=1,
    )
    launch_args = browser_manager._create_browser_state.await_args.kwargs
    assert (launch_args["cdp_port"], launch_args["browser_session_id"]) == (9242, "pbs_local")
    assert manager._browser_sessions["pbs_local"].cdp_port == 9242
    assert repository.session.status == "running"
    assert repository.session.started_at is not None
    assert repository.session.browser_address == cdp_url
    assert repository.session.upstream_cdp_url == cdp_url
    assert len(repository.updates) == 1
    assert repository.updates[0] == {
        "status": "running",
        "completed_at": None,
        "started_at": repository.session.started_at,
        "browser_address": cdp_url,
        "upstream_cdp_url": cdp_url,
    }


@pytest.mark.asyncio
async def test_launch_standalone_browser_continues_when_cdp_probe_fails() -> None:
    manager, repository = _launch_manager()
    browser_state = MagicMock()
    browser_state.get_or_create_page = AsyncMock(return_value=MagicMock())
    browser_state.close = AsyncMock()
    browser_manager = MagicMock()
    browser_manager._create_browser_state = AsyncMock(return_value=browser_state)
    agent_function = MagicMock()
    agent_function.build_proxy_session_extra_http_headers.return_value = {}
    cdp_writer = MagicMock()
    cdp_writer.wait_closed = AsyncMock()
    open_connection = AsyncMock(return_value=(MagicMock(), cdp_writer))
    get_json = AsyncMock(side_effect=RuntimeError("CDP endpoint unavailable"))

    with (
        patch.object(
            manager_mod, "app", SimpleNamespace(BROWSER_MANAGER=browser_manager, AGENT_FUNCTION=agent_function)
        ),
        patch.object(manager_mod.settings, "BROWSER_TYPE", "chromium-headful"),
        patch.object(manager_mod, "_allocate_cdp_port", return_value=9243),
        patch.object(manager_mod.asyncio, "open_connection", new=open_connection),
        patch.object(manager_mod, "aiohttp_get_json", new=get_json),
    ):
        await manager._launch_browser_for_session("pbs_local", "org_local")

    open_connection.assert_awaited_once_with("127.0.0.1", 9243)
    get_json.assert_awaited_once_with(
        "http://127.0.0.1:9243/json/version",
        retry=2,
        retry_timeout=0.25,
        timeout=1,
    )
    assert manager._browser_sessions["pbs_local"].cdp_port == 9243
    assert repository.session.status == "running"
    assert repository.session.started_at is not None
    assert repository.session.browser_address is None
    assert repository.session.upstream_cdp_url is None
    assert len(repository.updates) == 1
    assert repository.updates[0] == {
        "status": "running",
        "completed_at": None,
        "started_at": repository.session.started_at,
        "browser_address": None,
        "upstream_cdp_url": None,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(("applied_browser_profile_id", "loaded"), [("bp_saved", True), (None, False)])
async def test_launch_records_whether_the_requested_profile_loaded_before_the_session_is_usable(
    applied_browser_profile_id: str | None, loaded: bool
) -> None:
    manager, repository = _launch_manager()
    repository.session.browser_profile_id = "bp_saved"
    repository.session.generate_browser_profile = False
    browser_state = MagicMock()
    browser_state.browser_artifacts = BrowserArtifacts(applied_browser_profile_id=applied_browser_profile_id)
    browser_state.get_or_create_page = AsyncMock(return_value=MagicMock())
    browser_state.close = AsyncMock()
    browser_manager = MagicMock()
    browser_manager._create_browser_state = AsyncMock(return_value=browser_state)
    agent_function = MagicMock()
    agent_function.build_proxy_session_extra_http_headers.return_value = {}

    with (
        patch.object(
            manager_mod, "app", SimpleNamespace(BROWSER_MANAGER=browser_manager, AGENT_FUNCTION=agent_function)
        ),
        patch.object(manager_mod.settings, "BROWSER_TYPE", "chromium-headful"),
        patch.object(manager_mod, "_allocate_cdp_port", return_value=9244),
        patch.object(manager_mod, "_probe_local_cdp_address", AsyncMock(return_value=None)),
    ):
        await manager._launch_browser_for_session("pbs_local", "org_local")

    assert repository.updates[0] == {"browser_profile_loaded": loaded}
    assert repository.session.status == "running"


@pytest.mark.asyncio
async def test_probe_local_cdp_address_stops_after_connection_refusal() -> None:
    open_connection = AsyncMock(side_effect=ConnectionRefusedError)
    wait_for = AsyncMock(wraps=manager_mod.asyncio.wait_for)
    get_json = AsyncMock()
    log = MagicMock()

    with (
        patch.object(manager_mod.asyncio, "open_connection", new=open_connection),
        patch.object(manager_mod.asyncio, "wait_for", new=wait_for),
        patch.object(manager_mod, "aiohttp_get_json", new=get_json),
        patch.object(manager_mod, "LOG", new=log),
    ):
        assert await manager_mod._probe_local_cdp_address(9244) is None

    open_connection.assert_awaited_once_with("127.0.0.1", 9244)
    wait_for.assert_awaited_once_with(ANY, timeout=1)
    get_json.assert_not_awaited()
    log.info.assert_called_once_with("Local browser did not open requested CDP port", cdp_port=9244)
