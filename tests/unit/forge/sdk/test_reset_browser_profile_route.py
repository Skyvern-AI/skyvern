"""Tests for POST /workflows/{wpid}/browser_session/reset_profile."""

from __future__ import annotations

from collections.abc import Generator
from types import SimpleNamespace
from unittest.mock import AsyncMock, call

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from skyvern.exceptions import SkyvernHTTPException, WorkflowNotFound
from skyvern.forge import app as forge_app
from skyvern.forge.sdk.routes import routers as routers_module
from skyvern.forge.sdk.services import org_auth_service


@pytest.fixture(scope="module")
def app_client_and_mocks() -> Generator[tuple[TestClient, SimpleNamespace]]:
    async def _fake_org() -> SimpleNamespace:
        return SimpleNamespace(organization_id="org_oss")

    mocks = SimpleNamespace(
        get_workflow=AsyncMock(),
        delete_browser_session=AsyncMock(),
        list_managed_profiles=AsyncMock(return_value=[]),
        delete_profile_blob=AsyncMock(),
        delete_db_profile=AsyncMock(),
    )
    module_patches = pytest.MonkeyPatch()
    try:
        module_patches.setattr(forge_app.WORKFLOW_SERVICE, "get_workflow_by_permanent_id", mocks.get_workflow)
        module_patches.setattr(forge_app.STORAGE, "delete_browser_session", mocks.delete_browser_session)
        module_patches.setattr(forge_app.STORAGE, "delete_browser_profile", mocks.delete_profile_blob)
        module_patches.setattr(
            forge_app.DATABASE.browser_sessions,
            "list_managed_browser_profiles_for_workflow",
            mocks.list_managed_profiles,
        )
        module_patches.setattr(forge_app.DATABASE.browser_sessions, "delete_browser_profile", mocks.delete_db_profile)

        fastapi_app = FastAPI()
        fastapi_app.dependency_overrides[org_auth_service.get_current_org] = _fake_org
        fastapi_app.include_router(routers_module.base_router, prefix="/v1")

        @fastapi_app.exception_handler(SkyvernHTTPException)
        async def _handle_skyvern_http_exception(request: Request, exc: SkyvernHTTPException) -> JSONResponse:
            return JSONResponse(status_code=exc.status_code, content={"detail": exc.message})

        with TestClient(fastapi_app) as client:
            yield client, mocks
    finally:
        module_patches.undo()


@pytest.fixture
def client_and_mocks(app_client_and_mocks: tuple[TestClient, SimpleNamespace]) -> tuple[TestClient, SimpleNamespace]:
    """Reuse the app/router while returning pristine mock state to every test."""
    client, mocks = app_client_and_mocks
    for mock in vars(mocks).values():
        mock.reset_mock(return_value=True, side_effect=True)
    mocks.list_managed_profiles.return_value = []
    return client, mocks


def test_reset_browser_profile_clears_storage(client_and_mocks: tuple[TestClient, SimpleNamespace]) -> None:
    client, mocks = client_and_mocks
    mocks.get_workflow.return_value = SimpleNamespace(workflow_permanent_id="wpid_123")

    response = client.post("/v1/workflows/wpid_123/browser_session/reset_profile")

    assert response.status_code == 204
    mocks.get_workflow.assert_awaited_once_with(workflow_permanent_id="wpid_123", organization_id="org_oss")
    mocks.delete_browser_session.assert_awaited_once_with(
        organization_id="org_oss",
        workflow_permanent_id="wpid_123",
    )
    mocks.delete_profile_blob.assert_not_awaited()
    mocks.delete_db_profile.assert_not_awaited()


def test_reset_browser_profile_workflow_not_found(client_and_mocks: tuple[TestClient, SimpleNamespace]) -> None:
    client, mocks = client_and_mocks
    mocks.get_workflow.side_effect = WorkflowNotFound(workflow_permanent_id="wpid_missing")

    response = client.post("/v1/workflows/wpid_missing/browser_session/reset_profile")

    assert response.status_code == 404
    mocks.delete_browser_session.assert_not_awaited()
    mocks.list_managed_profiles.assert_not_awaited()


def test_reset_browser_profile_storage_failure_returns_500(
    client_and_mocks: tuple[TestClient, SimpleNamespace],
) -> None:
    client, mocks = client_and_mocks
    mocks.get_workflow.return_value = SimpleNamespace(workflow_permanent_id="wpid_123")
    mocks.delete_browser_session.side_effect = RuntimeError("s3 AccessDenied")

    response = client.post("/v1/workflows/wpid_123/browser_session/reset_profile")

    assert response.status_code == 500
    assert "retry" in response.json()["detail"].lower()


def test_reset_browser_profile_clears_managed_profile_blobs_and_rows(
    client_and_mocks: tuple[TestClient, SimpleNamespace],
) -> None:
    client, mocks = client_and_mocks
    mocks.get_workflow.return_value = SimpleNamespace(workflow_permanent_id="wpid_123")
    mocks.list_managed_profiles.return_value = [
        SimpleNamespace(browser_profile_id="bp_one", browser_profile_key_digest="", deleted_at=None),
        SimpleNamespace(browser_profile_id="bp_two", browser_profile_key_digest="", deleted_at=None),
    ]

    response = client.post("/v1/workflows/wpid_123/browser_session/reset_profile")

    assert response.status_code == 204
    mocks.list_managed_profiles.assert_awaited_once_with(
        organization_id="org_oss",
        workflow_permanent_id="wpid_123",
        include_deleted=True,
    )
    assert mocks.delete_profile_blob.await_args_list == [
        call(organization_id="org_oss", profile_id="bp_one"),
        call(organization_id="org_oss", profile_id="bp_two"),
    ]
    assert mocks.delete_db_profile.await_args_list == [
        call(profile_id="bp_one", organization_id="org_oss"),
        call(profile_id="bp_two", organization_id="org_oss"),
    ]


def test_reset_browser_profile_clears_segmented_legacy_archives(
    client_and_mocks: tuple[TestClient, SimpleNamespace],
) -> None:
    client, mocks = client_and_mocks
    mocks.get_workflow.return_value = SimpleNamespace(workflow_permanent_id="wpid_123")
    mocks.list_managed_profiles.return_value = [
        SimpleNamespace(browser_profile_id="bp_seg", browser_profile_key_digest="abc123digest", deleted_at=None),
        # Soft-deleted row: its blob/row are already gone, but its segment archive must still be cleared.
        SimpleNamespace(
            browser_profile_id="bp_gone", browser_profile_key_digest="def456digest", deleted_at="2026-01-01"
        ),
    ]

    response = client.post("/v1/workflows/wpid_123/browser_session/reset_profile")

    assert response.status_code == 204
    session_deletes = {c.kwargs["workflow_permanent_id"] for c in mocks.delete_browser_session.await_args_list}
    assert session_deletes == {
        "wpid_123",
        "wpid_123/profile_segments/abc123digest",
        "wpid_123/profile_segments/def456digest",
    }
    mocks.delete_profile_blob.assert_awaited_once_with(organization_id="org_oss", profile_id="bp_seg")
    mocks.delete_db_profile.assert_awaited_once_with(profile_id="bp_seg", organization_id="org_oss")


@pytest.mark.parametrize(
    "path",
    [
        "/v1/workflows/wpid_123/browser_session/refresh",
        "/v1/workflows/wpid_123/browser_session/refresh/",
        "/v1/workflows/wpid_123/browser_session/reset_profile/",
    ],
)
def test_alias_paths_still_work(client_and_mocks: tuple[TestClient, SimpleNamespace], path: str) -> None:
    client, mocks = client_and_mocks
    mocks.get_workflow.return_value = SimpleNamespace(workflow_permanent_id="wpid_123")

    response = client.post(path)

    assert response.status_code == 204
    mocks.delete_browser_session.assert_awaited_once_with(
        organization_id="org_oss",
        workflow_permanent_id="wpid_123",
    )
