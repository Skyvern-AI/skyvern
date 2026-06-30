"""Tests for POST /browser_profiles with a browser_session_id (create-from-session + source reap)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError

from skyvern.exceptions import SkyvernHTTPException
from skyvern.forge import app as forge_app
from skyvern.forge.sdk.routes import browser_profiles as browser_profiles_route
from skyvern.forge.sdk.schemas.browser_profiles import BrowserProfile, CreateBrowserProfileRequest
from skyvern.forge.sdk.schemas.persistent_browser_sessions import PersistentBrowserSession
from skyvern.forge.sdk.services import org_auth_service
from skyvern.schemas.runs import ProxyLocation

_default_profile_template_candidates = browser_profiles_route._default_browser_profile_template_candidates


def _session(**kwargs: object) -> PersistentBrowserSession:
    base: dict[str, object] = {
        "persistent_browser_session_id": "pbs_1",
        "organization_id": "org_oss",
        "created_at": datetime(2026, 1, 1),
        "modified_at": datetime(2026, 1, 1),
    }
    base.update(kwargs)
    return PersistentBrowserSession(**base)


def _profile() -> BrowserProfile:
    return BrowserProfile(
        browser_profile_id="bp_new",
        organization_id="org_oss",
        name="my profile",
        created_at=datetime(2026, 1, 1),
        modified_at=datetime(2026, 1, 1),
    )


@pytest.fixture(autouse=True)
def _restore_forge_app() -> Iterator[None]:
    previous_app = object.__getattribute__(forge_app, "_inst")
    yield
    object.__setattr__(forge_app, "_inst", previous_app)


@pytest.fixture(autouse=True)
def _use_minimal_empty_profile_seed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(browser_profiles_route, "_default_browser_profile_template_candidates", lambda: [])


def _build_client(
    monkeypatch: pytest.MonkeyPatch,
    *,
    raise_server_exceptions: bool = False,
) -> tuple[TestClient, SimpleNamespace]:
    async def _fake_org() -> SimpleNamespace:
        return SimpleNamespace(organization_id="org_oss")

    mocks = SimpleNamespace(
        get_session=AsyncMock(),
        get_workflow_run=AsyncMock(),
        get_workflow=AsyncMock(),
        create_profile=AsyncMock(return_value=_profile()),
        delete_db_profile=AsyncMock(),
        hard_delete_db_profile=AsyncMock(),
        retrieve_profile=AsyncMock(),
        retrieve_session=AsyncMock(),
        store_profile=AsyncMock(),
        delete_profile_blob=AsyncMock(),
        rate_limit_submit_run=AsyncMock(),
        get_workflow_browser_session_storage_key=AsyncMock(return_value="wp_1"),
    )

    forge_app.set_app(
        SimpleNamespace(
            RATE_LIMITER=SimpleNamespace(rate_limit_submit_run=mocks.rate_limit_submit_run),
            WORKFLOW_SERVICE=SimpleNamespace(
                get_workflow_browser_session_storage_key=mocks.get_workflow_browser_session_storage_key,
            ),
            DATABASE=SimpleNamespace(
                browser_sessions=SimpleNamespace(
                    get_persistent_browser_session=mocks.get_session,
                    create_browser_profile=mocks.create_profile,
                    delete_browser_profile=mocks.delete_db_profile,
                    hard_delete_browser_profile=mocks.hard_delete_db_profile,
                ),
                workflow_runs=SimpleNamespace(get_workflow_run=mocks.get_workflow_run),
                workflows=SimpleNamespace(get_workflow=mocks.get_workflow),
            ),
            STORAGE=SimpleNamespace(
                retrieve_browser_profile=mocks.retrieve_profile,
                retrieve_browser_session=mocks.retrieve_session,
                store_browser_profile=mocks.store_profile,
                delete_browser_profile=mocks.delete_profile_blob,
            ),
        )
    )

    test_router = APIRouter()
    test_router.add_api_route(
        "/browser_profiles",
        browser_profiles_route.create_browser_profile,
        methods=["POST"],
        response_model=BrowserProfile,
    )
    test_router.add_api_route(
        "/browser_profiles/",
        browser_profiles_route.create_browser_profile,
        methods=["POST"],
        response_model=BrowserProfile,
        include_in_schema=False,
    )

    fastapi_app = FastAPI()
    fastapi_app.dependency_overrides[org_auth_service.get_current_org] = _fake_org
    fastapi_app.include_router(test_router, prefix="/v1")

    @fastapi_app.exception_handler(SkyvernHTTPException)
    async def _handle(request: Request, exc: SkyvernHTTPException) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.message})

    # raise_server_exceptions=False so an un-mapped error surfaces as a 500 response (as in prod)
    # rather than propagating out of the TestClient — the failed-promote path re-raises after rollback.
    return TestClient(fastapi_app, raise_server_exceptions=raise_server_exceptions), mocks


def test_request_model_accepts_no_source() -> None:
    request = CreateBrowserProfileRequest(name="fresh profile")

    assert request.browser_session_id is None
    assert request.workflow_run_id is None


def test_request_model_rejects_both_sources() -> None:
    with pytest.raises(ValidationError, match="Provide only one of browser_session_id or workflow_run_id"):
        CreateBrowserProfileRequest(name="my profile", browser_session_id="pbs_1", workflow_run_id="wr_1")


def test_request_model_rejects_whitespace_source_id() -> None:
    with pytest.raises(ValidationError):
        CreateBrowserProfileRequest(name="my profile", browser_session_id=" ")


def test_whitespace_source_id_returns_422(monkeypatch: pytest.MonkeyPatch) -> None:
    client, mocks = _build_client(monkeypatch)

    response = client.post("/v1/browser_profiles/", json={"name": "fresh profile", "browser_session_id": " "})

    assert response.status_code == 422
    mocks.create_profile.assert_not_awaited()
    mocks.store_profile.assert_not_awaited()


def _make_default_profile_template(
    tmp_path: Path,
    *,
    name: str = "default_profile",
    marker: str = "from-template",
) -> Path:
    template = tmp_path / name
    (template / "Default").mkdir(parents=True)
    (template / "Default" / "Preferences").write_text('{"profile": "template"}', encoding="utf-8")
    (template / "Default" / "template-marker.txt").write_text(marker, encoding="utf-8")
    (template / "Local State").write_text('{"local": "template"}', encoding="utf-8")
    (template / "ShaderCache").mkdir()
    (template / "ShaderCache" / "cache.bin").write_bytes(b"cache")
    return template


def test_create_empty_profile_stores_default_profile_seed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    client, mocks = _build_client(monkeypatch)
    template = _make_default_profile_template(tmp_path)
    captured_directory: dict[str, str] = {}

    async def _store_profile(**kwargs: str) -> None:
        directory = Path(kwargs["directory"])
        captured_directory["path"] = str(directory)
        assert (directory / "Default" / "Preferences").read_text(encoding="utf-8") == '{"profile": "template"}'
        assert (directory / "Default" / "template-marker.txt").read_text(encoding="utf-8") == "from-template"
        assert (directory / "Local State").read_text(encoding="utf-8") == '{"local": "template"}'
        assert not (directory / "ShaderCache").exists()

    monkeypatch.setattr(browser_profiles_route, "_default_browser_profile_template_candidates", lambda: [template])
    mocks.store_profile.side_effect = _store_profile

    response = client.post("/v1/browser_profiles/", json={"name": "fresh profile", "description": "blank"})

    assert response.status_code == 200
    mocks.rate_limit_submit_run.assert_awaited_once_with("org_oss")
    mocks.create_profile.assert_awaited_once_with(
        organization_id="org_oss",
        name="fresh profile",
        description="blank",
        proxy_location=None,
        proxy_session_id=None,
    )
    mocks.store_profile.assert_awaited_once()
    assert captured_directory["path"]
    assert not Path(captured_directory["path"]).exists()
    mocks.delete_db_profile.assert_not_awaited()
    mocks.hard_delete_db_profile.assert_not_awaited()


def test_create_empty_profile_persists_requested_proxy_pin(monkeypatch: pytest.MonkeyPatch) -> None:
    client, mocks = _build_client(monkeypatch)

    response = client.post(
        "/v1/browser_profiles/",
        json={
            "name": "fresh profile",
            "proxy_location": ProxyLocation.RESIDENTIAL_ISP,
            "proxy_session_id": "abc1234567",
        },
    )

    assert response.status_code == 200
    mocks.create_profile.assert_awaited_once_with(
        organization_id="org_oss",
        name="fresh profile",
        description=None,
        proxy_location=ProxyLocation.RESIDENTIAL_ISP,
        proxy_session_id="abc1234567",
    )
    mocks.store_profile.assert_awaited_once()


def test_default_profile_template_candidates_come_from_configured_directory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    (tmp_path / "chrome").mkdir()
    (tmp_path / "chrome_100").mkdir()
    (tmp_path / "chrome_200").mkdir()
    (tmp_path / "chrome_backup").mkdir()
    (tmp_path / "chromium_150").mkdir()
    (tmp_path / "firefox_999").mkdir()
    monkeypatch.setattr(browser_profiles_route.settings, "DEFAULT_BROWSER_PROFILE_DIR", str(tmp_path))

    assert _default_profile_template_candidates() == [
        tmp_path,
        tmp_path / "chrome_200",
        tmp_path / "chrome_100",
        tmp_path / "chrome",
        tmp_path / "chromium_150",
        tmp_path / "chromium",
    ]


def test_create_empty_profile_uses_latest_versioned_default_profile(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    client, mocks = _build_client(monkeypatch)
    _make_default_profile_template(tmp_path, name="chrome_100", marker="old-template")
    _make_default_profile_template(tmp_path, name="chrome_200", marker="new-template")

    async def _store_profile(**kwargs: str) -> None:
        directory = Path(kwargs["directory"])
        assert (directory / "Default" / "template-marker.txt").read_text(encoding="utf-8") == "new-template"

    monkeypatch.setattr(browser_profiles_route.settings, "DEFAULT_BROWSER_PROFILE_DIR", str(tmp_path))
    monkeypatch.setattr(
        browser_profiles_route,
        "_default_browser_profile_template_candidates",
        _default_profile_template_candidates,
    )
    mocks.store_profile.side_effect = _store_profile

    response = client.post("/v1/browser_profiles/", json={"name": "fresh profile"})

    assert response.status_code == 200
    mocks.store_profile.assert_awaited_once()


def test_create_empty_profile_falls_back_to_minimal_seed(monkeypatch: pytest.MonkeyPatch) -> None:
    client, mocks = _build_client(monkeypatch)
    captured_directory: dict[str, str] = {}

    async def _store_profile(**kwargs: str) -> None:
        directory = Path(kwargs["directory"])
        captured_directory["path"] = str(directory)
        assert (directory / "Default" / "Preferences").read_text(encoding="utf-8") == "{}"
        assert (directory / "Local State").read_text(encoding="utf-8") == "{}"

    monkeypatch.setattr(browser_profiles_route, "_default_browser_profile_template_candidates", lambda: [])
    mocks.store_profile.side_effect = _store_profile

    response = client.post("/v1/browser_profiles/", json={"name": "fresh profile"})

    assert response.status_code == 200
    mocks.rate_limit_submit_run.assert_awaited_once_with("org_oss")
    mocks.store_profile.assert_awaited_once()
    assert captured_directory["path"]
    assert not Path(captured_directory["path"]).exists()


def test_create_empty_profile_rate_limit_failure_does_not_seed_or_create(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, mocks = _build_client(monkeypatch)
    create_directory = Mock(side_effect=AssertionError("profile directory should not be created"))
    mocks.rate_limit_submit_run.side_effect = HTTPException(status_code=429, detail="rate limited")
    monkeypatch.setattr(browser_profiles_route, "_create_empty_browser_profile_directory", create_directory)

    response = client.post("/v1/browser_profiles/", json={"name": "fresh profile"})

    assert response.status_code == 429
    create_directory.assert_not_called()
    mocks.create_profile.assert_not_awaited()
    mocks.store_profile.assert_not_awaited()


def test_create_empty_profile_does_not_create_db_row_when_blank_directory_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, mocks = _build_client(monkeypatch)

    def _raise_directory_error() -> str:
        raise RuntimeError("temp dir failed")

    monkeypatch.setattr(browser_profiles_route, "_create_empty_browser_profile_directory", _raise_directory_error)

    response = client.post("/v1/browser_profiles/", json={"name": "fresh profile"})

    assert response.status_code == 500
    mocks.create_profile.assert_not_awaited()
    mocks.delete_db_profile.assert_not_awaited()
    mocks.hard_delete_db_profile.assert_not_awaited()
    mocks.store_profile.assert_not_awaited()


def test_create_empty_profile_rolls_back_when_archive_store_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    client, mocks = _build_client(monkeypatch)
    mocks.store_profile.side_effect = RuntimeError("s3 upload failed")

    response = client.post("/v1/browser_profiles/", json={"name": "fresh profile"})

    assert response.status_code == 500
    mocks.hard_delete_db_profile.assert_awaited_once_with("bp_new", organization_id="org_oss")
    mocks.delete_db_profile.assert_not_awaited()


def test_create_empty_profile_preserves_store_error_when_rollback_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    client, mocks = _build_client(monkeypatch, raise_server_exceptions=True)
    mocks.store_profile.side_effect = RuntimeError("s3 upload failed")
    mocks.hard_delete_db_profile.side_effect = RuntimeError("db rollback failed")

    with pytest.raises(RuntimeError, match="s3 upload failed"):
        client.post("/v1/browser_profiles/", json={"name": "fresh profile"})

    mocks.hard_delete_db_profile.assert_awaited_once_with("bp_new", organization_id="org_oss")
    mocks.delete_db_profile.assert_not_awaited()


def test_create_empty_profile_preserves_duplicate_name_handling(monkeypatch: pytest.MonkeyPatch) -> None:
    client, mocks = _build_client(monkeypatch)
    mocks.create_profile.side_effect = IntegrityError("insert", {}, Exception("duplicate"))

    response = client.post("/v1/browser_profiles/", json={"name": "my profile"})

    assert response.status_code == 409
    assert "already exists" in response.json()["detail"]
    mocks.store_profile.assert_not_awaited()


def test_create_profile_route_delegates_workflow_run_source(monkeypatch: pytest.MonkeyPatch) -> None:
    client, mocks = _build_client(monkeypatch)
    calls: list[dict[str, str | None]] = []

    async def _create_profile_from_workflow_run(**kwargs: str | None) -> BrowserProfile:
        calls.append(kwargs)
        return _profile()

    monkeypatch.setattr(
        browser_profiles_route,
        "_create_profile_from_workflow_run",
        _create_profile_from_workflow_run,
    )

    response = client.post(
        "/v1/browser_profiles/",
        json={"name": "my profile", "description": "from workflow", "workflow_run_id": "wr_1"},
    )

    assert response.status_code == 200
    assert calls == [
        {
            "organization_id": "org_oss",
            "name": "my profile",
            "description": "from workflow",
            "workflow_run_id": "wr_1",
            "proxy_location": None,
            "proxy_session_id": None,
        }
    ]
    mocks.rate_limit_submit_run.assert_not_awaited()
    mocks.get_workflow_run.assert_not_awaited()
    mocks.store_profile.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_profile_from_workflow_run_still_stores_workflow_archive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _client, mocks = _build_client(monkeypatch)
    mocks.get_workflow_run.return_value = SimpleNamespace(
        workflow_id="wf_1",
        workflow_permanent_id="wp_1",
    )
    mocks.get_workflow.return_value = SimpleNamespace(
        workflow_permanent_id="wp_1",
        persist_browser_session=True,
    )
    mocks.retrieve_session.return_value = "/tmp/workflow_session_dir"

    profile = await browser_profiles_route._create_profile_from_workflow_run(
        organization_id="org_oss",
        name="my profile",
        description=None,
        workflow_run_id="wr_1",
    )

    assert profile == _profile()
    mocks.retrieve_session.assert_awaited_once_with(
        organization_id="org_oss",
        workflow_permanent_id="wp_1",
    )
    mocks.store_profile.assert_awaited_once_with(
        organization_id="org_oss",
        profile_id="bp_new",
        directory="/tmp/workflow_session_dir",
    )


@pytest.mark.asyncio
async def test_failed_workflow_run_profile_creation_hard_deletes_half_created_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _client, mocks = _build_client(monkeypatch)
    mocks.get_workflow_run.return_value = SimpleNamespace(
        workflow_id="wf_1",
        workflow_permanent_id="wp_1",
    )
    mocks.get_workflow.return_value = SimpleNamespace(
        workflow_permanent_id="wp_1",
        persist_browser_session=True,
    )
    mocks.retrieve_session.return_value = "/tmp/workflow_session_dir"
    mocks.store_profile.side_effect = RuntimeError("s3 upload failed")

    with pytest.raises(RuntimeError, match="s3 upload failed"):
        await browser_profiles_route._create_profile_from_workflow_run(
            organization_id="org_oss",
            name="my profile",
            description=None,
            workflow_run_id="wr_1",
        )

    mocks.hard_delete_db_profile.assert_awaited_once_with("bp_new", organization_id="org_oss")
    mocks.delete_db_profile.assert_not_awaited()


def test_promote_deletes_source_session_blob(monkeypatch: pytest.MonkeyPatch) -> None:
    client, mocks = _build_client(monkeypatch)
    mocks.get_session.return_value = _session(generate_browser_profile=True)
    mocks.retrieve_profile.return_value = "/tmp/session_dir"

    response = client.post("/v1/browser_profiles/", json={"name": "my profile", "browser_session_id": "pbs_1"})

    assert response.status_code == 200
    mocks.rate_limit_submit_run.assert_not_awaited()
    mocks.store_profile.assert_awaited_once()
    mocks.delete_profile_blob.assert_awaited_once_with(organization_id="org_oss", profile_id="pbs_1")


def test_promote_inherits_session_proxy_pin(monkeypatch: pytest.MonkeyPatch) -> None:
    client, mocks = _build_client(monkeypatch)
    mocks.get_session.return_value = _session(
        generate_browser_profile=True,
        proxy_location=ProxyLocation.RESIDENTIAL_ISP,
        proxy_session_id="abc1234567",
    )
    mocks.retrieve_profile.return_value = "/tmp/session_dir"

    response = client.post("/v1/browser_profiles/", json={"name": "my profile", "browser_session_id": "pbs_1"})

    assert response.status_code == 200
    assert mocks.create_profile.await_args.kwargs["proxy_location"] == ProxyLocation.RESIDENTIAL_ISP
    assert mocks.create_profile.await_args.kwargs["proxy_session_id"] == "abc1234567"


def test_promote_inherits_session_proxy_pin_for_blank_residential_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, mocks = _build_client(monkeypatch)
    mocks.get_session.return_value = _session(
        generate_browser_profile=True,
        proxy_location=ProxyLocation.RESIDENTIAL_ISP,
        proxy_session_id="abc1234567",
    )
    mocks.retrieve_profile.return_value = "/tmp/session_dir"

    response = client.post(
        "/v1/browser_profiles/",
        json={
            "name": "my profile",
            "browser_session_id": "pbs_1",
            "proxy_location": ProxyLocation.RESIDENTIAL_ISP,
            "proxy_session_id": None,
        },
    )

    assert response.status_code == 200
    assert mocks.create_profile.await_args.kwargs["proxy_location"] == ProxyLocation.RESIDENTIAL_ISP
    assert mocks.create_profile.await_args.kwargs["proxy_session_id"] == "abc1234567"


def test_failed_promote_does_not_delete_source_blob(monkeypatch: pytest.MonkeyPatch) -> None:
    client, mocks = _build_client(monkeypatch)
    mocks.get_session.return_value = _session(generate_browser_profile=True)
    mocks.retrieve_profile.return_value = "/tmp/session_dir"
    mocks.store_profile.side_effect = RuntimeError("s3 upload failed")

    response = client.post("/v1/browser_profiles/", json={"name": "my profile", "browser_session_id": "pbs_1"})

    assert response.status_code == 500
    mocks.hard_delete_db_profile.assert_awaited_once_with("bp_new", organization_id="org_oss")
    mocks.delete_db_profile.assert_not_awaited()
    mocks.delete_profile_blob.assert_not_awaited()  # never reap the source on a failed promote


def test_promote_succeeds_even_if_source_reap_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    client, mocks = _build_client(monkeypatch)
    mocks.get_session.return_value = _session(generate_browser_profile=True)
    mocks.retrieve_profile.return_value = "/tmp/session_dir"
    mocks.delete_profile_blob.side_effect = RuntimeError("s3 AccessDenied")

    response = client.post("/v1/browser_profiles/", json={"name": "my profile", "browser_session_id": "pbs_1"})

    assert response.status_code == 200
    mocks.delete_profile_blob.assert_awaited_once()


def test_non_opted_in_session_returns_400(monkeypatch: pytest.MonkeyPatch) -> None:
    client, mocks = _build_client(monkeypatch)
    mocks.get_session.return_value = _session(generate_browser_profile=False)
    mocks.retrieve_profile.return_value = None

    response = client.post("/v1/browser_profiles/", json={"name": "my profile", "browser_session_id": "pbs_1"})

    assert response.status_code == 400
    assert "not configured to generate a browser profile" in response.json()["detail"]
    mocks.store_profile.assert_not_awaited()
    mocks.delete_profile_blob.assert_not_awaited()
