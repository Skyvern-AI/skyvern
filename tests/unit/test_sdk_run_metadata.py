import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from structlog.testing import capture_logs

from skyvern.client import AsyncSkyvern, Skyvern
from skyvern.client.types.workflow_run_request_output import WorkflowRunRequestOutput as WorkflowRunRequest
from skyvern.library import local_browser_profile
from skyvern.library.constants import DEFAULT_CDP_PORT
from skyvern.library.skyvern import Skyvern as LibrarySkyvern


def _make_browser(_client: object, _context: object, **kwargs: object) -> SimpleNamespace:
    return SimpleNamespace(**kwargs)


def _make_local_profile(path: Path, events: list[str], label: str = "") -> Mock:
    path.mkdir(exist_ok=True)
    prefix = f"{label}:" if label else ""
    profile = Mock(spec=local_browser_profile.LocalBrowserProfile)
    profile.path = path
    profile.revalidate.side_effect = lambda: events.append(f"{prefix}revalidate") or True
    profile.release.side_effect = lambda: events.append(f"{prefix}release")
    return profile


def _cleanup_mock(events: list[str]) -> Mock:
    def cleanup(profile: Mock) -> bool:
        events.append("cleanup")
        profile.release()
        return True

    return Mock(side_effect=cleanup)


def test_workflow_run_request_accepts_run_metadata() -> None:
    request = WorkflowRunRequest(workflow_id="wpid_123", run_metadata={"customer": "acme"})

    assert request.run_metadata == {"customer": "acme"}


def test_workflow_run_request_accepts_max_elapsed_time() -> None:
    request = WorkflowRunRequest(workflow_id="wpid_123", max_elapsed_time_minutes=10)

    assert request.max_elapsed_time_minutes == 10


@pytest.mark.asyncio
async def test_launch_cloud_browser_sends_browser_profile_id(monkeypatch: pytest.MonkeyPatch) -> None:
    client = LibrarySkyvern(base_url="https://api.example.test", api_key="test-key")
    browser_session = SimpleNamespace(browser_session_id="pbs_123", app_url=None)
    browser = object()
    create_browser_session = AsyncMock(return_value=browser_session)
    connect_to_session = AsyncMock(return_value=browser)

    monkeypatch.setattr(client, "_ensure_cloud_environment", lambda: None)
    monkeypatch.setattr(client, "create_browser_session", create_browser_session)
    monkeypatch.setattr(client, "_connect_to_cloud_browser_session", connect_to_session)

    result = await client.launch_cloud_browser(timeout=30, browser_profile_id="bp_123")

    assert result is browser
    create_browser_session.assert_awaited_once_with(
        timeout=30,
        proxy_location=None,
        browser_profile_id="bp_123",
    )
    connect_to_session.assert_awaited_once_with(browser_session)


@pytest.mark.asyncio
async def test_skyvern_launch_local_browser_uses_unique_anonymous_instance_defaults(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    client = LibrarySkyvern(base_url="https://api.example.test", api_key="test-key")
    events: list[str] = []
    assigned_ports = iter((40101, 40102))
    profiles: list[Mock] = []
    for name in ("one", "two"):
        profiles.append(_make_local_profile(tmp_path / f"profile-{name}", events, name))
    profile_iterator = iter(profiles)

    def create_profile() -> Mock:
        profile = next(profile_iterator)
        events.append(f"{profile.path.name.removeprefix('profile-')}:create")
        return profile

    async def launch_persistent_context(**kwargs: object) -> object:
        user_data_dir = Path(str(kwargs["user_data_dir"]))
        events.append(f"{user_data_dir.name.removeprefix('profile-')}:launch")
        return object()

    async def read_port(user_data_dir: Path) -> int:
        events.append(f"{user_data_dir.name.removeprefix('profile-')}:read-port")
        return next(assigned_ports)

    launch = AsyncMock(side_effect=launch_persistent_context)
    playwright = SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=launch))
    create = Mock(side_effect=create_profile)
    monkeypatch.setattr(client, "_get_playwright", AsyncMock(return_value=playwright))
    monkeypatch.setattr(local_browser_profile, "create_local_browser_profile", create)
    monkeypatch.setattr("skyvern.library.skyvern._read_devtools_active_port", AsyncMock(side_effect=read_port))
    instance_dirs = iter((tmp_path / "skyvern-browser-one", tmp_path / "skyvern-browser-two"))

    def make_instance_dir(*, prefix: str) -> str:
        assert prefix == "skyvern-browser-"
        path = next(instance_dirs)
        path.mkdir()
        return str(path)

    monkeypatch.setattr("skyvern.library.skyvern.tempfile.mkdtemp", make_instance_dir)
    monkeypatch.setattr("skyvern.library.skyvern_browser.SkyvernBrowser", _make_browser)

    first = await client.launch_local_browser(headless=True)
    second = await client.launch_local_browser(headless=True)

    first_call, second_call = launch.await_args_list
    first_dir = Path(first_call.kwargs["user_data_dir"])
    second_dir = Path(second_call.kwargs["user_data_dir"])
    assert [first_dir, second_dir] == [profile.path for profile in profiles]
    assert events == [
        "one:create",
        "one:revalidate",
        "one:launch",
        "one:read-port",
        "two:create",
        "two:revalidate",
        "two:launch",
        "two:read-port",
    ]
    assert create.call_count == 2
    assert [call.kwargs["args"] for call in (first_call, second_call)] == [["--remote-debugging-port=0"]] * 2
    assert first.browser_address == "http://localhost:40101"
    assert second.browser_address == "http://localhost:40102"
    assert first.local_cdp_port == 40101
    assert second.local_cdp_port == 40102
    assert first.local_user_data_dir == str(first_dir)
    assert second.local_user_data_dir == str(second_dir)
    assert first.local_user_data_dir_owned is True
    assert second.local_user_data_dir_owned is True
    assert first.local_browser_profile is profiles[0]
    assert second.local_browser_profile is profiles[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_stage", ["lock_race", "identity_changed"])
async def test_skyvern_launch_local_browser_aborts_before_chromium_when_profile_creation_is_unsafe(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failure_stage: str,
) -> None:
    client = LibrarySkyvern(base_url="https://api.example.test", api_key="test-key")
    launch = AsyncMock(side_effect=AssertionError("Chromium must not launch"))
    playwright = SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=launch))
    profile = SimpleNamespace(
        path=tmp_path / "skyvern-browser-race",
        revalidate=Mock(return_value=failure_stage != "identity_changed"),
        release=Mock(),
    )
    create_profile = Mock(
        side_effect=BlockingIOError("profile lock is held") if failure_stage == "lock_race" else None,
        return_value=profile,
    )
    cleanup = Mock(side_effect=lambda candidate: candidate.release())

    monkeypatch.setattr(client, "_get_playwright", AsyncMock(return_value=playwright))
    monkeypatch.setattr(local_browser_profile, "create_local_browser_profile", create_profile)
    monkeypatch.setattr(local_browser_profile, "cleanup_local_browser_profile", cleanup)

    with pytest.raises(BlockingIOError if failure_stage == "lock_race" else RuntimeError):
        await client.launch_local_browser()

    create_profile.assert_called_once_with()
    launch.assert_not_awaited()
    if failure_stage == "identity_changed":
        profile.revalidate.assert_called_once_with()
        cleanup.assert_called_once_with(profile)
        profile.release.assert_called_once_with()
    else:
        cleanup.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("launch_kwargs", "expected_dir", "expected_port"),
    [
        ({"user_data_dir": "custom-profile"}, Path("custom-profile"), DEFAULT_CDP_PORT),
        ({"port": 9333}, Path("skyvern-browser"), 9333),
        ({"user_data_dir": "custom-profile", "port": 9333}, Path("custom-profile"), 9333),
    ],
)
async def test_skyvern_launch_local_browser_preserves_explicit_config_defaults(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    launch_kwargs: dict[str, object],
    expected_dir: Path,
    expected_port: int,
) -> None:
    client = LibrarySkyvern(base_url="https://api.example.test", api_key="test-key")
    launch = AsyncMock(return_value=object())
    playwright = SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=launch))
    monkeypatch.setattr(client, "_get_playwright", AsyncMock(return_value=playwright))
    monkeypatch.setattr("skyvern.library.skyvern.tempfile.gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr("skyvern.library.skyvern_browser.SkyvernBrowser", _make_browser)

    browser = await client.launch_local_browser(**launch_kwargs)  # type: ignore[arg-type]

    call_kwargs = launch.await_args.kwargs
    resolved_expected_dir = expected_dir if launch_kwargs.get("user_data_dir") else tmp_path / expected_dir
    assert call_kwargs == {
        "user_data_dir": str(resolved_expected_dir),
        "headless": False,
        "args": [f"--remote-debugging-port={expected_port}"],
    }
    assert browser.browser_address == f"http://localhost:{expected_port}"
    assert browser.local_cdp_port == expected_port
    assert browser.local_user_data_dir == str(resolved_expected_dir)
    assert browser.local_user_data_dir_owned is False


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_stage", ["launch", "port_read", "constructor"])
async def test_skyvern_launch_local_browser_rolls_back_setup_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failure_stage: str,
) -> None:
    client = LibrarySkyvern(base_url="https://api.example.test", api_key="test-key")
    events: list[str] = []
    profile = _make_local_profile(tmp_path / "profile-failed", events)

    async def close_context() -> None:
        events.append("context-close")

    browser_context = SimpleNamespace(close=AsyncMock(side_effect=close_context))

    async def launch_context(**_kwargs: object) -> object:
        events.append("launch")
        if failure_stage == "launch":
            raise RuntimeError("chromium failed to start")
        return browser_context

    async def read_port(_user_data_dir: Path) -> int:
        events.append("port-read")
        if failure_stage == "port_read":
            raise RuntimeError("missing port")
        return 40101

    def construct_browser(*args: object, **kwargs: object) -> SimpleNamespace:
        events.append("construct")
        if failure_stage == "constructor":
            raise RuntimeError("wrapper failed")
        return _make_browser(*args, **kwargs)

    launch = AsyncMock(side_effect=launch_context)
    playwright = SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=launch))
    create_profile = Mock(side_effect=lambda: events.append("create") or profile)
    cleanup = _cleanup_mock(events)
    constructor = Mock(side_effect=construct_browser)
    fallback_dir = tmp_path / "skyvern-browser-failed"

    monkeypatch.setattr(client, "_get_playwright", AsyncMock(return_value=playwright))
    monkeypatch.setattr(local_browser_profile, "create_local_browser_profile", create_profile)
    monkeypatch.setattr(local_browser_profile, "cleanup_local_browser_profile", cleanup)
    monkeypatch.setattr("skyvern.library.skyvern.tempfile.mkdtemp", lambda *, prefix: str(fallback_dir))
    monkeypatch.setattr("skyvern.library.skyvern._read_devtools_active_port", AsyncMock(side_effect=read_port))
    monkeypatch.setattr("skyvern.library.skyvern_browser.SkyvernBrowser", constructor)

    expected_error = {
        "launch": "chromium failed to start",
        "port_read": "missing port",
        "constructor": "wrapper failed",
    }[failure_stage]
    with pytest.raises(RuntimeError, match=expected_error):
        await client.launch_local_browser()

    if failure_stage == "launch":
        browser_context.close.assert_not_awaited()
    else:
        browser_context.close.assert_awaited_once_with()
    cleanup.assert_called_once_with(profile)
    profile.release.assert_called_once_with()
    expected_events = ["create", "revalidate", "launch"]
    if failure_stage != "launch":
        expected_events.append("port-read")
    if failure_stage == "constructor":
        expected_events.append("construct")
        assert constructor.call_args.kwargs["local_browser_profile"] is profile
    if failure_stage != "launch":
        expected_events.append("context-close")
    expected_events.extend(["cleanup", "release"])
    assert events == expected_events


@pytest.mark.asyncio
async def test_skyvern_launch_local_browser_preserves_port_error_when_context_close_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    client = LibrarySkyvern(base_url="https://api.example.test", api_key="test-key")
    events: list[str] = []
    profile = _make_local_profile(tmp_path / "profile-failed", events)
    browser_context = SimpleNamespace(close=AsyncMock(side_effect=RuntimeError("rollback close failed")))
    port_error = RuntimeError("original port failure")
    playwright = SimpleNamespace(
        chromium=SimpleNamespace(launch_persistent_context=AsyncMock(return_value=browser_context))
    )
    cleanup = _cleanup_mock(events)

    monkeypatch.setattr(client, "_get_playwright", AsyncMock(return_value=playwright))
    monkeypatch.setattr(local_browser_profile, "create_local_browser_profile", Mock(return_value=profile))
    monkeypatch.setattr(local_browser_profile, "cleanup_local_browser_profile", cleanup)
    monkeypatch.setattr("skyvern.library.skyvern._read_devtools_active_port", AsyncMock(side_effect=port_error))

    with capture_logs() as logs:
        with pytest.raises(RuntimeError) as exc_info:
            await client.launch_local_browser()

    assert exc_info.value is port_error
    browser_context.close.assert_awaited_once_with()
    cleanup.assert_called_once_with(profile)
    profile.release.assert_called_once_with()
    assert any(log.get("event") == "local_browser_context_close_rollback_failed" for log in logs)


@pytest.mark.asyncio
async def test_skyvern_launch_local_browser_preserves_launch_error_when_cleanup_internals_fail(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    client = LibrarySkyvern(base_url="https://api.example.test", api_key="test-key")
    monkeypatch.setattr(local_browser_profile.tempfile, "tempdir", str(tmp_path))
    profile = local_browser_profile.create_local_browser_profile()
    launch = AsyncMock(side_effect=RuntimeError("original launch failure"))
    playwright = SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=launch))
    monkeypatch.setattr(client, "_get_playwright", AsyncMock(return_value=playwright))
    monkeypatch.setattr(local_browser_profile, "create_local_browser_profile", Mock(return_value=profile))
    monkeypatch.setattr(local_browser_profile, "terminate_local_browser_processes", Mock(return_value=True))
    monkeypatch.setattr(
        local_browser_profile,
        "_remove_profile_directory_bounded",
        Mock(side_effect=OSError("cleanup spawn failure")),
    )

    with pytest.raises(RuntimeError, match="original launch failure"):
        await client.launch_local_browser()

    assert profile._lock_fd is None


@pytest.mark.asyncio
@pytest.mark.parametrize(("managed", "close_count"), [(True, 1), (True, 2), (False, 2)])
async def test_skyvern_browser_close_uses_shared_cleanup_exactly_once(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    managed: bool,
    close_count: int,
) -> None:
    from skyvern.library.skyvern_browser import SkyvernBrowser

    events: list[str] = []
    user_data_dir = tmp_path / ("managed" if managed else "explicit")
    profile = _make_local_profile(user_data_dir, events)

    async def close_context() -> None:
        events.append("context-close")

    browser_context = SimpleNamespace(close=AsyncMock(side_effect=close_context))
    cleanup = _cleanup_mock(events)
    monkeypatch.setattr("skyvern.library.skyvern_browser.BrowserContext.__init__", lambda *_args: None)
    monkeypatch.setattr(local_browser_profile, "cleanup_local_browser_profile", cleanup)
    browser = SkyvernBrowser(
        Mock(),
        browser_context,
        local_user_data_dir=str(user_data_dir),
        local_user_data_dir_owned=managed,
        local_browser_profile=profile if managed else None,
    )

    for _ in range(close_count):
        await browser.close()

    browser_context.close.assert_awaited_once_with()
    if managed:
        cleanup.assert_called_once_with(profile)
        profile.release.assert_called_once_with()
        assert events == ["context-close", "cleanup", "release"]
    else:
        cleanup.assert_not_called()
        profile.release.assert_not_called()
        assert events == ["context-close"]


@pytest.mark.asyncio
async def test_skyvern_browser_close_warns_when_profile_cleanup_is_deferred(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from skyvern.library.skyvern_browser import SkyvernBrowser

    user_data_dir = tmp_path / "managed"
    profile = _make_local_profile(user_data_dir, [])
    browser_context = SimpleNamespace(close=AsyncMock())
    cleanup = Mock(return_value=False)
    monkeypatch.setattr("skyvern.library.skyvern_browser.BrowserContext.__init__", lambda *_args: None)
    monkeypatch.setattr(local_browser_profile, "cleanup_local_browser_profile", cleanup)
    browser = SkyvernBrowser(
        Mock(),
        browser_context,
        local_user_data_dir=str(user_data_dir),
        local_user_data_dir_owned=True,
        local_browser_profile=profile,
    )

    with capture_logs() as logs:
        await browser.close()

    browser_context.close.assert_awaited_once_with()
    cleanup.assert_called_once_with(profile)
    assert any(log.get("event") == "local_browser_profile_cleanup_deferred" for log in logs)


@pytest.mark.asyncio
async def test_skyvern_browser_concurrent_close_shares_context_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    from skyvern.library.skyvern_browser import SkyvernBrowser

    entered = asyncio.Event()
    release = asyncio.Event()
    close_error = RuntimeError("close failed")

    async def close_context() -> None:
        entered.set()
        await release.wait()
        raise close_error

    skyvern = SimpleNamespace(close_browser_session=AsyncMock())
    browser_context = SimpleNamespace(close=AsyncMock(side_effect=close_context))
    monkeypatch.setattr("skyvern.library.skyvern_browser.BrowserContext.__init__", lambda *_args: None)
    browser = SkyvernBrowser(skyvern, browser_context, browser_session_id="pbs_shared")

    first = asyncio.create_task(browser.close())
    await entered.wait()
    second = asyncio.create_task(browser.close())
    await asyncio.sleep(0)
    assert not second.done()
    release.set()
    results = await asyncio.gather(first, second, return_exceptions=True)

    assert all(result is close_error for result in results)
    browser_context.close.assert_awaited_once_with()
    skyvern.close_browser_session.assert_not_awaited()
    assert browser._close_task is None
    assert browser._closed is False


@pytest.mark.asyncio
async def test_skyvern_browser_concurrent_close_runs_sequence_once(monkeypatch: pytest.MonkeyPatch) -> None:
    from skyvern.library.skyvern_browser import SkyvernBrowser

    entered = asyncio.Event()
    release = asyncio.Event()

    async def close_context() -> None:
        entered.set()
        await release.wait()

    skyvern = SimpleNamespace(close_browser_session=AsyncMock())
    browser_context = SimpleNamespace(close=AsyncMock(side_effect=close_context))
    monkeypatch.setattr("skyvern.library.skyvern_browser.BrowserContext.__init__", lambda *_args: None)
    browser = SkyvernBrowser(skyvern, browser_context, browser_session_id="pbs_shared")

    first = asyncio.create_task(browser.close())
    await entered.wait()
    second = asyncio.create_task(browser.close())
    await asyncio.sleep(0)
    assert not second.done()
    release.set()
    await asyncio.gather(first, second)
    await browser.close()

    browser_context.close.assert_awaited_once_with()
    skyvern.close_browser_session.assert_awaited_once_with("pbs_shared")
    assert browser._close_task is None
    assert browser._closed is True


@pytest.mark.asyncio
async def test_skyvern_browser_cancelled_waiter_does_not_cancel_shared_close(monkeypatch: pytest.MonkeyPatch) -> None:
    from skyvern.library.skyvern_browser import SkyvernBrowser

    entered = asyncio.Event()
    release = asyncio.Event()

    async def close_context() -> None:
        entered.set()
        await release.wait()

    skyvern = SimpleNamespace(close_browser_session=AsyncMock())
    browser_context = SimpleNamespace(close=AsyncMock(side_effect=close_context))
    monkeypatch.setattr("skyvern.library.skyvern_browser.BrowserContext.__init__", lambda *_args: None)
    browser = SkyvernBrowser(skyvern, browser_context, browser_session_id="pbs_cancel_waiter")

    first = asyncio.create_task(browser.close())
    await entered.wait()
    second = asyncio.create_task(browser.close())
    await asyncio.sleep(0)
    second.cancel()
    with pytest.raises(asyncio.CancelledError):
        await second
    release.set()
    await first

    browser_context.close.assert_awaited_once_with()
    skyvern.close_browser_session.assert_awaited_once_with("pbs_cancel_waiter")
    assert browser._closed is True
    assert browser._close_task is None


@pytest.mark.asyncio
async def test_skyvern_browser_cancelled_creator_leaves_shared_close_running(monkeypatch: pytest.MonkeyPatch) -> None:
    from skyvern.library.skyvern_browser import SkyvernBrowser

    entered = asyncio.Event()
    release = asyncio.Event()

    async def close_context() -> None:
        entered.set()
        await release.wait()

    skyvern = SimpleNamespace(close_browser_session=AsyncMock())
    browser_context = SimpleNamespace(close=AsyncMock(side_effect=close_context))
    monkeypatch.setattr("skyvern.library.skyvern_browser.BrowserContext.__init__", lambda *_args: None)
    browser = SkyvernBrowser(skyvern, browser_context, browser_session_id="pbs_cancel_creator")

    first = asyncio.create_task(browser.close())
    await entered.wait()
    second = asyncio.create_task(browser.close())
    await asyncio.sleep(0)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    release.set()
    await second

    browser_context.close.assert_awaited_once_with()
    skyvern.close_browser_session.assert_awaited_once_with("pbs_cancel_creator")
    assert browser._closed is True
    assert browser._close_task is None


@pytest.mark.asyncio
async def test_skyvern_browser_close_retries_after_context_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    from skyvern.library.skyvern_browser import SkyvernBrowser

    skyvern = SimpleNamespace(close_browser_session=AsyncMock())
    browser_context = SimpleNamespace(close=AsyncMock(side_effect=[RuntimeError("close failed"), None]))
    monkeypatch.setattr("skyvern.library.skyvern_browser.BrowserContext.__init__", lambda *_args: None)
    browser = SkyvernBrowser(
        skyvern,
        browser_context,
        browser_session_id="pbs_retry",
    )

    with pytest.raises(RuntimeError, match="close failed"):
        await browser.close()

    assert browser._close_task is None
    assert browser._closed is False
    await browser.close()

    assert browser_context.close.await_count == 2
    skyvern.close_browser_session.assert_awaited_once_with("pbs_retry")
    assert browser._close_task is None
    assert browser._closed is True


@pytest.mark.asyncio
async def test_skyvern_launch_local_browser_windows_bypasses_managed_profiles_before_posix_calls(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    client = LibrarySkyvern(base_url="https://api.example.test", api_key="test-key")
    user_data_dir = tmp_path / "skyvern-browser-windows"
    launch = AsyncMock(return_value=object())
    playwright = SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=launch))
    getuid = Mock(side_effect=AssertionError("os.getuid must not run on Windows"))
    acquire_lock = Mock(side_effect=AssertionError("flock path must not run on Windows"))
    create_profile = Mock(wraps=local_browser_profile.create_local_browser_profile)

    def make_instance_dir(*, prefix: str) -> str:
        assert prefix == "skyvern-browser-"
        user_data_dir.mkdir()
        return str(user_data_dir)

    mkdtemp = Mock(side_effect=make_instance_dir)
    monkeypatch.setattr(client, "_get_playwright", AsyncMock(return_value=playwright))
    monkeypatch.setattr(local_browser_profile.sys, "platform", "win32")
    monkeypatch.setattr(local_browser_profile.os, "getuid", getuid)
    monkeypatch.setattr(local_browser_profile, "_lock_exclusive_nonblocking", acquire_lock)
    monkeypatch.setattr(local_browser_profile, "create_local_browser_profile", create_profile)
    monkeypatch.setattr("skyvern.library.skyvern.tempfile.mkdtemp", mkdtemp)
    monkeypatch.setattr("skyvern.library.skyvern._read_devtools_active_port", AsyncMock(return_value=40123))
    monkeypatch.setattr("skyvern.library.skyvern_browser.SkyvernBrowser", _make_browser)

    browser = await client.launch_local_browser(headless=True)

    create_profile.assert_called_once_with()
    getuid.assert_not_called()
    acquire_lock.assert_not_called()
    mkdtemp.assert_called_once_with(prefix="skyvern-browser-")
    assert launch.await_args.kwargs == {
        "user_data_dir": str(user_data_dir),
        "headless": True,
        "args": ["--remote-debugging-port=0"],
    }
    assert browser.local_cdp_port == 40123
    assert browser.local_user_data_dir == str(user_data_dir)
    assert browser.local_user_data_dir_owned is True


def test_run_workflow_sends_run_metadata() -> None:
    captured_bodies: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_bodies.append(json.loads(request.content))
        return httpx.Response(
            status_code=200,
            json={
                "run_id": "wr_123",
                "status": "queued",
                "created_at": datetime.now(UTC).isoformat(),
                "modified_at": datetime.now(UTC).isoformat(),
            },
        )

    client = Skyvern(
        base_url="https://api.example.test",
        api_key="test-key",
        httpx_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    response = client.run_workflow(
        agent_id="wpid_123",
        run_metadata={"customer": "acme", "tier": "enterprise"},
    )

    assert response.run_id == "wr_123"
    assert captured_bodies == [
        {
            "agent_id": "wpid_123",
            "run_metadata": {"customer": "acme", "tier": "enterprise"},
        }
    ]


def test_run_workflow_sends_max_elapsed_time() -> None:
    captured_bodies: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_bodies.append(json.loads(request.content))
        return httpx.Response(
            status_code=200,
            json={
                "run_id": "wr_123",
                "status": "queued",
                "created_at": datetime.now(UTC).isoformat(),
                "modified_at": datetime.now(UTC).isoformat(),
            },
        )

    client = Skyvern(
        base_url="https://api.example.test",
        api_key="test-key",
        httpx_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    response = client.run_workflow(
        agent_id="wpid_123",
        max_elapsed_time_minutes=10,
    )

    assert response.run_id == "wr_123"
    assert captured_bodies == [
        {
            "agent_id": "wpid_123",
            "max_elapsed_time_minutes": 10,
        }
    ]


def _workflow_run_payload() -> dict[str, object]:
    now = datetime.now(UTC).isoformat()
    return {
        "workflow_run_id": "wr_123",
        "workflow_id": "wf_123",
        "workflow_permanent_id": "wpid_123",
        "organization_id": "org_123",
        "status": "completed",
        "created_at": now,
        "modified_at": now,
    }


def _workflow_run_response_payload() -> dict[str, object]:
    now = datetime.now(UTC).isoformat()
    return {
        "run_id": "wr_retry",
        "status": "queued",
        "created_at": now,
        "modified_at": now,
    }


def test_retry_workflow_run_uses_retry_route() -> None:
    captured_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_requests.append(request)
        return httpx.Response(status_code=200, json=_workflow_run_response_payload())

    client = Skyvern(
        base_url="https://api.example.test",
        api_key="test-key",
        httpx_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    response = client.retry_workflow_run(
        "wr_original",
        max_steps_override=12,
        user_agent="skyvern-ui",
    )

    assert response.run_id == "wr_retry"
    assert captured_requests[0].method == "POST"
    assert captured_requests[0].url.path == "/v1/agents/runs/wr_original/retry"
    assert captured_requests[0].headers["x-max-steps-override"] == "12"
    assert captured_requests[0].headers["x-user-agent"] == "skyvern-ui"


@pytest.mark.asyncio
async def test_async_retry_workflow_run_uses_retry_route() -> None:
    captured_requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        captured_requests.append(request)
        return httpx.Response(status_code=200, json=_workflow_run_response_payload())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as httpx_client:
        client = AsyncSkyvern(
            base_url="https://api.example.test",
            api_key="test-key",
            httpx_client=httpx_client,
        )
        response = await client.retry_workflow_run(
            "wr_original",
            max_steps_override=8,
            user_agent="skyvern-ui",
        )

    assert response.run_id == "wr_retry"
    assert captured_requests[0].method == "POST"
    assert captured_requests[0].url.path == "/v1/agents/runs/wr_original/retry"
    assert captured_requests[0].headers["x-max-steps-override"] == "8"
    assert captured_requests[0].headers["x-user-agent"] == "skyvern-ui"


def test_get_workflow_runs_by_id_uses_workflow_scoped_route() -> None:
    captured_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_requests.append(request)
        return httpx.Response(status_code=200, json=[_workflow_run_payload()])

    client = Skyvern(
        base_url="https://api.example.test",
        api_key="test-key",
        httpx_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    runs = client.get_workflow_runs_by_id(
        "wpid_123",
        page=1,
        page_size=10,
        status="completed",
        search_key="acme",
        error_code="LOGIN_FAILED",
    )

    assert runs[0].workflow_run_id == "wr_123"
    assert captured_requests[0].method == "GET"
    assert captured_requests[0].url.path == "/v1/agents/wpid_123/runs"
    assert captured_requests[0].url.params["page"] == "1"
    assert captured_requests[0].url.params["page_size"] == "10"
    assert captured_requests[0].url.params["status"] == "completed"
    assert captured_requests[0].url.params["search_key"] == "acme"
    assert captured_requests[0].url.params["error_code"] == "LOGIN_FAILED"


@pytest.mark.asyncio
async def test_async_get_workflow_runs_by_id_uses_workflow_scoped_route() -> None:
    captured_requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        captured_requests.append(request)
        return httpx.Response(status_code=200, json=[_workflow_run_payload()])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as httpx_client:
        client = AsyncSkyvern(
            base_url="https://api.example.test",
            api_key="test-key",
            httpx_client=httpx_client,
        )
        runs = await client.get_workflow_runs_by_id(
            "wpid_123",
            page=2,
            page_size=5,
            status="failed",
            search_key="prod",
            error_code="TIMEOUT",
        )

    assert runs[0].workflow_run_id == "wr_123"
    assert captured_requests[0].method == "GET"
    assert captured_requests[0].url.path == "/v1/agents/wpid_123/runs"
    assert captured_requests[0].url.params["page"] == "2"
    assert captured_requests[0].url.params["page_size"] == "5"
    assert captured_requests[0].url.params["status"] == "failed"
    assert captured_requests[0].url.params["search_key"] == "prod"
    assert captured_requests[0].url.params["error_code"] == "TIMEOUT"


@pytest.mark.asyncio
async def test_connect_to_cloud_browser_session_threads_app_url(monkeypatch: pytest.MonkeyPatch) -> None:
    client = LibrarySkyvern(base_url="https://api.example.test", api_key="test-key")
    cdp_browser = SimpleNamespace(contexts=[SimpleNamespace(_loop=None)])
    fake_playwright = SimpleNamespace(chromium=SimpleNamespace(connect_over_cdp=AsyncMock(return_value=cdp_browser)))
    monkeypatch.setattr(client, "_get_playwright", AsyncMock(return_value=fake_playwright))
    browser_session = SimpleNamespace(
        browser_session_id="pbs_123",
        browser_address="wss://cdp.example.test",
        app_url="https://app.example.test/browser-sessions/pbs_123",
    )

    browser = await client._connect_to_cloud_browser_session(browser_session)

    assert browser.app_url == "https://app.example.test/browser-sessions/pbs_123"
    assert browser.browser_session_id == "pbs_123"
