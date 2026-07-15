import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from skyvern.client import AsyncSkyvern, Skyvern
from skyvern.client.types.workflow_run_request_output import WorkflowRunRequestOutput as WorkflowRunRequest
from skyvern.library.constants import DEFAULT_CDP_PORT
from skyvern.library.skyvern import Skyvern as LibrarySkyvern


def _make_browser(_client: object, _context: object, **kwargs: object) -> SimpleNamespace:
    return SimpleNamespace(**kwargs)


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
    assigned_ports = iter((40101, 40102))

    async def launch_persistent_context(**kwargs: object) -> object:
        user_data_dir = Path(str(kwargs["user_data_dir"]))
        (user_data_dir / "DevToolsActivePort").write_text(f"{next(assigned_ports)}\n/devtools/browser/test\n")
        return object()

    launch = AsyncMock(side_effect=launch_persistent_context)
    playwright = SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=launch))
    monkeypatch.setattr(client, "_get_playwright", AsyncMock(return_value=playwright))
    monkeypatch.setattr("skyvern.library.skyvern.tempfile.gettempdir", lambda: str(tmp_path))
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
    assert first_dir != second_dir
    assert [call.kwargs["args"] for call in (first_call, second_call)] == [["--remote-debugging-port=0"]] * 2
    assert first.browser_address == "http://localhost:40101"
    assert second.browser_address == "http://localhost:40102"
    assert first.local_cdp_port == 40101
    assert second.local_cdp_port == 40102
    assert first.local_user_data_dir == str(first_dir)
    assert second.local_user_data_dir == str(second_dir)
    assert first.local_user_data_dir_owned is True
    assert second.local_user_data_dir_owned is True


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
@pytest.mark.parametrize("failure_stage", ["launch", "port_read"])
async def test_skyvern_launch_local_browser_removes_owned_dir_on_setup_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failure_stage: str,
) -> None:
    client = LibrarySkyvern(base_url="https://api.example.test", api_key="test-key")
    browser_context = SimpleNamespace(close=AsyncMock())
    launch_fails = failure_stage == "launch"
    launch = AsyncMock(
        side_effect=RuntimeError("chromium failed to start") if launch_fails else None,
        return_value=None if launch_fails else browser_context,
    )
    playwright = SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=launch))
    user_data_dir = tmp_path / "skyvern-browser-failed"
    user_data_dir.mkdir()

    monkeypatch.setattr(client, "_get_playwright", AsyncMock(return_value=playwright))
    monkeypatch.setattr("skyvern.library.skyvern.tempfile.gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr("skyvern.library.skyvern.tempfile.mkdtemp", lambda *, prefix: str(user_data_dir))
    monkeypatch.setattr(
        "skyvern.library.skyvern._read_devtools_active_port",
        AsyncMock(side_effect=RuntimeError("missing port")),
    )

    with pytest.raises(RuntimeError, match="chromium failed to start" if launch_fails else "missing port"):
        await client.launch_local_browser()

    if launch_fails:
        browser_context.close.assert_not_awaited()
    else:
        browser_context.close.assert_awaited_once_with()
    assert not user_data_dir.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("owned", [True, False])
async def test_skyvern_browser_close_removes_only_owned_user_data_dir(tmp_path: Path, owned: bool) -> None:
    from skyvern.library.skyvern_browser import SkyvernBrowser

    user_data_dir = tmp_path / ("owned" if owned else "explicit")
    user_data_dir.mkdir()
    browser_context = SimpleNamespace(close=AsyncMock())
    browser = object.__new__(SkyvernBrowser)
    object.__setattr__(browser, "_browser_context", browser_context)
    object.__setattr__(browser, "_browser_session_id", None)
    object.__setattr__(browser, "_local_user_data_dir", str(user_data_dir))
    object.__setattr__(browser, "_local_user_data_dir_owned", owned)

    await browser.close()

    browser_context.close.assert_awaited_once_with()
    assert user_data_dir.exists() is not owned


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
