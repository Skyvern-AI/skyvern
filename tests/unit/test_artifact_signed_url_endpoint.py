"""Tests for the on-demand artifact signed-URL endpoint (SKY-12541).

GET /v1/artifacts/{artifact_id}/signed-url mints a short-lived signed content
URL at the point of use, so consumers never depend on the long-lived URLs
embedded in earlier API responses.
"""

import json
import time
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlparse

from fastapi import FastAPI
from fastapi.testclient import TestClient

from skyvern.config import settings
from skyvern.forge.sdk.artifact.models import Artifact, ArtifactType
from skyvern.forge.sdk.artifact.signing import (
    ARTIFACT_URL_ON_DEMAND_EXPIRY_SECONDS,
)
from skyvern.forge.sdk.routes.routers import base_router
from skyvern.forge.sdk.schemas.organizations import Organization

_KEYRING_JSON = json.dumps({"current_kid": "k1", "keys": {"k1": {"secret": "0" * 64}}})


def _make_artifact(artifact_id: str = "a_1", organization_id: str = "o_1") -> Artifact:
    now = datetime.now(timezone.utc)
    return Artifact(
        artifact_id=artifact_id,
        artifact_type=ArtifactType.SCREENSHOT_ACTION,
        uri=f"s3://bucket/{artifact_id}.png",
        organization_id=organization_id,
        task_id="tsk_1",
        step_id="stp_1",
        created_at=now,
        modified_at=now,
    )


def _make_org(organization_id: str = "o_1") -> Organization:
    now = datetime.now(timezone.utc)
    return Organization(
        organization_id=organization_id,
        organization_name="org",
        created_at=now,
        modified_at=now,
    )


def _make_client() -> TestClient:
    test_app = FastAPI()
    test_app.include_router(base_router, prefix="/v1")
    return TestClient(test_app)


class TestGetArtifactSignedUrl:
    def test_mints_short_lived_signed_url(self) -> None:
        artifact = _make_artifact()
        with (
            patch.object(settings, "ARTIFACT_CONTENT_HMAC_KEYRING", _KEYRING_JSON),
            patch.object(settings, "SKYVERN_BASE_URL", "http://testserver"),
            patch("skyvern.forge.sdk.routes.agent_protocol.app") as app_module,
            patch(
                "skyvern.forge.sdk.services.org_auth_service.get_current_org_cached",
                new=AsyncMock(return_value=_make_org()),
            ),
        ):
            app_module.DATABASE.artifacts.get_artifact_by_id = AsyncMock(return_value=artifact)
            from skyvern.forge.sdk.artifact.manager import ArtifactManager

            app_module.ARTIFACT_MANAGER.resolve_share_url = ArtifactManager.resolve_share_url.__get__(ArtifactManager())

            before = int(time.time())
            resp = _make_client().get("/v1/artifacts/a_1/signed-url", headers={"x-api-key": "key"})
            after = int(time.time())

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["artifact_id"] == "a_1"
        parsed = urlparse(body["signed_url"])
        assert parsed.path == "/v1/artifacts/a_1/content"
        qs = parse_qs(parsed.query)
        expiry = int(qs["expiry"][0])
        assert before + ARTIFACT_URL_ON_DEMAND_EXPIRY_SECONDS <= expiry <= after + ARTIFACT_URL_ON_DEMAND_EXPIRY_SECONDS
        assert body["expires_at"] == expiry

    def test_short_ttl_is_minutes_not_hours(self) -> None:
        assert 60 <= ARTIFACT_URL_ON_DEMAND_EXPIRY_SECONDS <= 15 * 60

    def test_unknown_artifact_returns_404(self) -> None:
        with (
            patch.object(settings, "ARTIFACT_CONTENT_HMAC_KEYRING", _KEYRING_JSON),
            patch("skyvern.forge.sdk.routes.agent_protocol.app") as app_module,
            patch(
                "skyvern.forge.sdk.services.org_auth_service.get_current_org_cached",
                new=AsyncMock(return_value=_make_org()),
            ),
        ):
            app_module.DATABASE.artifacts.get_artifact_by_id = AsyncMock(return_value=None)
            resp = _make_client().get("/v1/artifacts/a_missing/signed-url", headers={"x-api-key": "key"})
        assert resp.status_code == 404

    def test_requires_authentication(self) -> None:
        with patch.object(settings, "ARTIFACT_CONTENT_HMAC_KEYRING", _KEYRING_JSON):
            resp = _make_client().get("/v1/artifacts/a_1/signed-url")
        assert resp.status_code == 403

    def test_org_scoping_uses_callers_org(self) -> None:
        """The artifact lookup must be scoped to the authenticated org."""
        artifact = _make_artifact()
        with (
            patch.object(settings, "ARTIFACT_CONTENT_HMAC_KEYRING", _KEYRING_JSON),
            patch.object(settings, "SKYVERN_BASE_URL", "http://testserver"),
            patch("skyvern.forge.sdk.routes.agent_protocol.app") as app_module,
            patch(
                "skyvern.forge.sdk.services.org_auth_service.get_current_org_cached",
                new=AsyncMock(return_value=_make_org("o_2")),
            ),
        ):
            app_module.DATABASE.artifacts.get_artifact_by_id = AsyncMock(return_value=artifact)
            from skyvern.forge.sdk.artifact.manager import ArtifactManager

            app_module.ARTIFACT_MANAGER.resolve_share_url = ArtifactManager.resolve_share_url.__get__(ArtifactManager())
            resp = _make_client().get("/v1/artifacts/a_1/signed-url", headers={"x-api-key": "key"})

        assert resp.status_code == 200
        app_module.DATABASE.artifacts.get_artifact_by_id.assert_awaited_once_with(
            artifact_id="a_1", organization_id="o_2"
        )


class TestMintedUrlAgainstContentEndpoint:
    def _mint_and_fetch(self, *, age_seconds: int, range_header: str | None = None) -> "TestClient.Response":
        """Mint a short-lived URL, then fetch content ``age_seconds`` later (patched clock)."""
        artifact = _make_artifact()
        real_time = time.time
        with (
            patch.object(settings, "ARTIFACT_CONTENT_HMAC_KEYRING", _KEYRING_JSON),
            patch.object(settings, "SKYVERN_BASE_URL", "http://testserver"),
            patch("skyvern.forge.sdk.routes.agent_protocol.app") as app_module,
            patch(
                "skyvern.forge.sdk.services.org_auth_service.get_current_org_cached",
                new=AsyncMock(return_value=_make_org()),
            ),
        ):
            app_module.DATABASE.artifacts.get_artifact_by_id = AsyncMock(return_value=artifact)
            app_module.DATABASE.artifacts.get_artifact_by_id_no_org = AsyncMock(return_value=artifact)
            app_module.ARTIFACT_MANAGER.retrieve_artifact = AsyncMock(return_value=b"0123456789")
            from skyvern.forge.sdk.artifact.manager import ArtifactManager

            app_module.ARTIFACT_MANAGER.resolve_share_url = ArtifactManager.resolve_share_url.__get__(ArtifactManager())

            client = _make_client()
            minted = client.get("/v1/artifacts/a_1/signed-url", headers={"x-api-key": "key"})
            assert minted.status_code == 200, minted.text
            signed_url = minted.json()["signed_url"].replace("http://testserver", "")

            headers = {"Range": range_header} if range_header else {}
            with patch("skyvern.forge.sdk.artifact.signing.time.time", new=lambda: real_time() + age_seconds):
                return client.get(signed_url, headers=headers)

    def test_fresh_minted_url_serves_content(self) -> None:
        resp = self._mint_and_fetch(age_seconds=0)
        assert resp.status_code == 200
        assert resp.content == b"0123456789"

    def test_expired_minted_url_is_rejected(self) -> None:
        resp = self._mint_and_fetch(age_seconds=ARTIFACT_URL_ON_DEMAND_EXPIRY_SECONDS + 5)
        assert resp.status_code == 403

    def test_fresh_minted_url_serves_range_requests(self) -> None:
        resp = self._mint_and_fetch(age_seconds=0, range_header="bytes=2-5")
        assert resp.status_code == 206
        assert resp.content == b"2345"

    def test_range_request_past_expiry_is_rejected(self) -> None:
        """Playback continuation past the TTL boundary must re-mint — the URL itself dies."""
        resp = self._mint_and_fetch(
            age_seconds=ARTIFACT_URL_ON_DEMAND_EXPIRY_SECONDS + 5,
            range_header="bytes=2-5",
        )
        assert resp.status_code == 403

    def test_tampered_artifact_id_is_rejected(self) -> None:
        """A signature minted for one artifact must not authorize another (replay/splice)."""
        artifact = _make_artifact()
        with (
            patch.object(settings, "ARTIFACT_CONTENT_HMAC_KEYRING", _KEYRING_JSON),
            patch.object(settings, "SKYVERN_BASE_URL", "http://testserver"),
            patch("skyvern.forge.sdk.routes.agent_protocol.app") as app_module,
            patch(
                "skyvern.forge.sdk.services.org_auth_service.get_current_org_cached",
                new=AsyncMock(return_value=_make_org()),
            ),
        ):
            app_module.DATABASE.artifacts.get_artifact_by_id = AsyncMock(return_value=artifact)
            app_module.DATABASE.artifacts.get_artifact_by_id_no_org = AsyncMock(return_value=artifact)
            app_module.ARTIFACT_MANAGER.retrieve_artifact = AsyncMock(return_value=b"content")
            from skyvern.forge.sdk.artifact.manager import ArtifactManager

            app_module.ARTIFACT_MANAGER.resolve_share_url = ArtifactManager.resolve_share_url.__get__(ArtifactManager())
            client = _make_client()
            minted = client.get("/v1/artifacts/a_1/signed-url", headers={"x-api-key": "key"})
            signed_url = minted.json()["signed_url"].replace("http://testserver", "")
            spliced = signed_url.replace("/artifacts/a_1/", "/artifacts/a_other/")
            resp = client.get(spliced)
        assert resp.status_code == 403


class TestKeyringUnsetFallback:
    def test_falls_back_to_storage_presigned_url(self) -> None:
        """Self-host deployments without a keyring still get a working URL."""
        artifact = _make_artifact()
        with (
            patch.object(settings, "ARTIFACT_CONTENT_HMAC_KEYRING", None),
            patch("skyvern.forge.sdk.routes.agent_protocol.app") as app_module,
            patch(
                "skyvern.forge.sdk.services.org_auth_service.get_current_org_cached",
                new=AsyncMock(return_value=_make_org()),
            ),
        ):
            app_module.DATABASE.artifacts.get_artifact_by_id = AsyncMock(return_value=artifact)
            app_module.ARTIFACT_MANAGER.resolve_share_url = AsyncMock(
                return_value="https://bucket.s3.amazonaws.com/a_1.png?X-Amz-Signature=abc"
            )
            resp = _make_client().get("/v1/artifacts/a_1/signed-url", headers={"x-api-key": "key"})

        assert resp.status_code == 200
        body = resp.json()
        assert body["signed_url"] == "https://bucket.s3.amazonaws.com/a_1.png?X-Amz-Signature=abc"
        assert body["expires_at"] is None
