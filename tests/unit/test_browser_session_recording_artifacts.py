"""Tests for the browser-session recording artifact pipeline.

Recordings (Playwright .webm video) are uploaded once at session close via
``S3Storage.sync_browser_session_file(artifact_type="videos")``. This change
registers them as RECORDING artifact rows scoped to the session and serves
them via short signed ``/v1/artifacts/{id}/content`` URLs from
``get_shared_recordings_in_browser_session``.

Mirrors the SKY-8861 download artifact pipeline in
``test_browser_session_download_artifacts.py``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import urlparse

import pytest

from skyvern.forge.sdk.artifact.manager import ArtifactManager
from skyvern.forge.sdk.artifact.models import Artifact, ArtifactType
from skyvern.forge.sdk.artifact.storage.s3 import S3Storage

_DUMMY_KEYRING_JSON = '{"current_kid": "k1", "keys": {"k1": {"secret": "0000000000000000000000000000000000000000000000000000000000000000"}}}'


def _is_amazonaws_s3_url(url: str) -> bool:
    """Strict hostname-suffix check (closes CodeQL py/incomplete-url-substring-sanitization)."""
    host = urlparse(url).hostname
    if host is None:
        return False
    return host == "s3.amazonaws.com" or host.endswith(".s3.amazonaws.com")


def _make_recording_artifact(
    artifact_id: str,
    uri: str,
    *,
    browser_session_id: str = "pbs_1",
    checksum: str | None = "sha-r",
    created_at: str = "2026-04-26T00:00:00Z",
) -> Artifact:
    return Artifact(
        artifact_id=artifact_id,
        artifact_type=ArtifactType.RECORDING,
        uri=uri,
        organization_id="o_1",
        browser_session_id=browser_session_id,
        checksum=checksum,
        created_at=created_at,
        modified_at=created_at,
    )


@pytest.fixture
def keyring_configured():
    from skyvern.config import settings

    with patch.object(settings, "ARTIFACT_CONTENT_HMAC_KEYRING", _DUMMY_KEYRING_JSON):
        yield


# ---------------------------------------------------------------------------
# create_browser_session_recording_artifact (manager helper)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_browser_session_recording_artifact_inserts_when_no_existing_row():
    manager = ArtifactManager()
    find_existing = AsyncMock(return_value=None)
    mock_db_create = AsyncMock()

    with (
        patch(
            "skyvern.forge.sdk.artifact.manager.app.DATABASE.artifacts.find_artifact_for_browser_session",
            find_existing,
        ),
        patch(
            "skyvern.forge.sdk.artifact.manager.app.DATABASE.artifacts.create_artifact",
            mock_db_create,
        ),
    ):
        artifact_id = await manager.create_browser_session_recording_artifact(
            organization_id="o_1",
            browser_session_id="pbs_1",
            uri="s3://skyvern-artifacts/v1/local/o_1/browser_sessions/pbs_1/videos/2026-04-26/recording.webm",
            filename="recording.webm",
            checksum="sha-r",
        )

    assert artifact_id.startswith("a_")
    mock_db_create.assert_awaited_once()
    _, kwargs = mock_db_create.call_args
    assert kwargs["artifact_type"] == ArtifactType.RECORDING
    assert kwargs["browser_session_id"] == "pbs_1"
    assert kwargs["organization_id"] == "o_1"
    assert kwargs["checksum"] == "sha-r"
    # Recordings are session-scoped, not run-scoped.
    assert kwargs.get("run_id") is None


@pytest.mark.asyncio
async def test_create_browser_session_recording_artifact_is_idempotent():
    """End-of-session sync may run twice on retry. The second call must reuse the existing row."""
    manager = ArtifactManager()
    existing = _make_recording_artifact(
        "a_existing",
        "s3://skyvern-artifacts/v1/local/o_1/browser_sessions/pbs_1/videos/2026-04-26/recording.webm",
    )
    find_existing = AsyncMock(return_value=existing)
    mock_db_create = AsyncMock()

    with (
        patch(
            "skyvern.forge.sdk.artifact.manager.app.DATABASE.artifacts.find_artifact_for_browser_session",
            find_existing,
        ),
        patch(
            "skyvern.forge.sdk.artifact.manager.app.DATABASE.artifacts.create_artifact",
            mock_db_create,
        ),
    ):
        artifact_id = await manager.create_browser_session_recording_artifact(
            organization_id="o_1",
            browser_session_id="pbs_1",
            uri=existing.uri,
            filename="recording.webm",
            checksum="sha-r",
        )

    assert artifact_id == "a_existing"
    mock_db_create.assert_not_awaited()


# ---------------------------------------------------------------------------
# Storage write site — sync_browser_session_file(artifact_type="videos")
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sync_browser_session_file_registers_recording_artifact():
    """A successful 'videos' sync must create a RECORDING artifact row."""
    storage = S3Storage()
    storage.async_client = MagicMock()
    storage.async_client.upload_file_from_path = AsyncMock()

    mock_create = AsyncMock(return_value="a_new")
    mock_artifact_manager = MagicMock()
    mock_artifact_manager.create_browser_session_recording_artifact = mock_create

    with (
        patch.object(storage, "_get_storage_class_for_org", new=AsyncMock(return_value=MagicMock())),
        patch("skyvern.forge.sdk.artifact.storage.s3.calculate_sha256_for_file", return_value="sha-r"),
        patch("skyvern.forge.sdk.artifact.storage.s3.app") as app_module,
    ):
        app_module.ARTIFACT_MANAGER = mock_artifact_manager
        await storage.sync_browser_session_file(
            organization_id="o_1",
            browser_session_id="pbs_1",
            artifact_type="videos",
            local_file_path="/tmp/recording.webm",
            remote_path="recording.webm",
            date="2026-04-26",
        )

    mock_create.assert_awaited_once()
    _, kwargs = mock_create.call_args
    assert kwargs["organization_id"] == "o_1"
    assert kwargs["browser_session_id"] == "pbs_1"
    assert kwargs["filename"] == "recording.webm"
    assert kwargs["checksum"] == "sha-r"
    assert kwargs["uri"].startswith("s3://") and "/browser_sessions/pbs_1/videos/" in kwargs["uri"]


@pytest.mark.asyncio
async def test_sync_browser_session_file_recording_failure_propagates():
    """Contract: ``sync_browser_session_file(artifact_type="videos")``
    raises when the artifact-row insert fails. The exception propagates to
    the caller; ``DefaultPersistentSessionsManager.close_session`` is
    responsible for catching it (which it does, with ``LOG.exception``).

    This test covers the storage-layer half only. Resilience of the overall
    pipeline (e.g., that a row-less recording still reaches the API
    response) lives in the legacy listing fallback inside
    ``get_shared_recordings_in_browser_session`` and is covered by the
    fall-through tests in this module."""
    storage = S3Storage()
    storage.async_client = MagicMock()
    storage.async_client.upload_file_from_path = AsyncMock()

    mock_create = AsyncMock(side_effect=RuntimeError("DB unreachable"))
    mock_artifact_manager = MagicMock()
    mock_artifact_manager.create_browser_session_recording_artifact = mock_create

    with (
        patch.object(storage, "_get_storage_class_for_org", new=AsyncMock(return_value=MagicMock())),
        patch("skyvern.forge.sdk.artifact.storage.s3.calculate_sha256_for_file", return_value="sha-r"),
        patch("skyvern.forge.sdk.artifact.storage.s3.app") as app_module,
    ):
        app_module.ARTIFACT_MANAGER = mock_artifact_manager
        with pytest.raises(RuntimeError, match="DB unreachable"):
            await storage.sync_browser_session_file(
                organization_id="o_1",
                browser_session_id="pbs_1",
                artifact_type="videos",
                local_file_path="/tmp/recording.webm",
                remote_path="recording.webm",
            )


# ---------------------------------------------------------------------------
# Read site — get_shared_recordings_in_browser_session (DB-first)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_shared_recordings_returns_short_signed_urls(keyring_configured):
    """When RECORDING rows exist, return short signed /v1/artifacts URLs — no
    S3 round-trip per file, no presigned URLs."""
    storage = S3Storage()
    storage.async_client = MagicMock()
    storage.async_client.list_files = AsyncMock()  # must NOT be called
    storage.async_client.create_presigned_urls = AsyncMock()  # must NOT be called

    artifact = _make_recording_artifact(
        "a_42",
        "s3://skyvern-artifacts/v1/local/o_1/browser_sessions/pbs_1/videos/2026-04-26/recording.webm",
    )
    mock_list = AsyncMock(return_value=[artifact])
    build_url = MagicMock(return_value="https://api.skyvern.com/v1/artifacts/a_42/content?expiry=x&kid=y&sig=z")

    with (
        patch("skyvern.forge.sdk.artifact.storage.base.app") as base_app,
        patch("skyvern.forge.sdk.artifact.storage.s3.app") as s3_app,
    ):
        s3_app.DATABASE.artifacts.list_artifacts_for_browser_session_by_type = mock_list
        base_app.ARTIFACT_MANAGER.build_signed_content_url = build_url
        base_app.ARTIFACT_MANAGER.resolve_artifact_url_expiry_seconds = AsyncMock(return_value=12 * 60 * 60)
        result = await storage.get_shared_recordings_in_browser_session(
            organization_id="o_1", browser_session_id="pbs_1"
        )

    assert len(result) == 1
    assert result[0].url.startswith("https://api.skyvern.com/v1/artifacts/a_42/content")
    assert result[0].artifact_id == "a_42"
    storage.async_client.list_files.assert_not_awaited()
    storage.async_client.create_presigned_urls.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_shared_recordings_filters_unsupported_extensions(keyring_configured):
    """Defensively drop rows whose URI doesn't look like a recording — even if
    a stray non-video row landed under the session id."""
    storage = S3Storage()
    storage.async_client = MagicMock()

    good = _make_recording_artifact("a_good", "s3://b/v1/.../videos/2026-04-26/ok.webm")
    bad = _make_recording_artifact("a_bad", "s3://b/v1/.../videos/2026-04-26/sneaky.exe")
    mock_list = AsyncMock(return_value=[good, bad])
    build_url = MagicMock(return_value="https://api.skyvern.com/v1/artifacts/a_good/content")

    with (
        patch("skyvern.forge.sdk.artifact.storage.base.app") as base_app,
        patch("skyvern.forge.sdk.artifact.storage.s3.app") as s3_app,
    ):
        s3_app.DATABASE.artifacts.list_artifacts_for_browser_session_by_type = mock_list
        base_app.ARTIFACT_MANAGER.build_signed_content_url = build_url
        base_app.ARTIFACT_MANAGER.resolve_artifact_url_expiry_seconds = AsyncMock(return_value=12 * 60 * 60)
        result = await storage.get_shared_recordings_in_browser_session(
            organization_id="o_1", browser_session_id="pbs_1"
        )

    assert [fi.artifact_id for fi in result] == ["a_good"]


@pytest.mark.asyncio
async def test_get_shared_recordings_sorts_newest_first(keyring_configured):
    """Multiple recording rows: newest first, matches the legacy listing path's order."""
    storage = S3Storage()
    storage.async_client = MagicMock()

    older = _make_recording_artifact(
        "a_old", "s3://b/v1/.../videos/2026-04-25/older.webm", created_at="2026-04-25T00:00:00Z"
    )
    newer = _make_recording_artifact(
        "a_new", "s3://b/v1/.../videos/2026-04-26/newer.webm", created_at="2026-04-26T00:00:00Z"
    )
    mock_list = AsyncMock(return_value=[older, newer])
    build_url = MagicMock(
        side_effect=lambda artifact_id, **_: f"https://api.skyvern.com/v1/artifacts/{artifact_id}/content"
    )

    with (
        patch("skyvern.forge.sdk.artifact.storage.base.app") as base_app,
        patch("skyvern.forge.sdk.artifact.storage.s3.app") as s3_app,
    ):
        s3_app.DATABASE.artifacts.list_artifacts_for_browser_session_by_type = mock_list
        base_app.ARTIFACT_MANAGER.build_signed_content_url = build_url
        base_app.ARTIFACT_MANAGER.resolve_artifact_url_expiry_seconds = AsyncMock(return_value=12 * 60 * 60)
        result = await storage.get_shared_recordings_in_browser_session(
            organization_id="o_1", browser_session_id="pbs_1"
        )

    assert [fi.artifact_id for fi in result] == ["a_new", "a_old"]


@pytest.mark.asyncio
async def test_get_shared_recordings_falls_back_to_presigned_for_legacy_session(keyring_configured):
    """Legacy session: no RECORDING rows. Files in S3 still surface as presigned
    URLs via the listing fallback so existing recordings remain reachable."""
    storage = S3Storage()
    storage.async_client = MagicMock()
    s3_key = "v1/local/o_1/browser_sessions/pbs_old/videos/2026-04-26/legacy.webm"
    storage.async_client.list_files = AsyncMock(return_value=[s3_key])
    storage.async_client.get_object_info = AsyncMock(
        return_value={
            "Metadata": {"sha256_checksum": "sha-old"},
            "LastModified": None,
            "ContentLength": 1024,
        }
    )
    storage.async_client.create_presigned_urls = AsyncMock(
        return_value=["https://skyvern-artifacts.s3.amazonaws.com/...?sig=old"]
    )

    mock_list = AsyncMock(return_value=[])  # no rows
    build_url = MagicMock()  # must NOT be called

    with (
        patch("skyvern.forge.sdk.artifact.storage.base.app") as base_app,
        patch("skyvern.forge.sdk.artifact.storage.s3.app") as s3_app,
    ):
        s3_app.DATABASE.artifacts.list_artifacts_for_browser_session_by_type = mock_list
        base_app.ARTIFACT_MANAGER.build_signed_content_url = build_url
        base_app.ARTIFACT_MANAGER.resolve_artifact_url_expiry_seconds = AsyncMock(return_value=12 * 60 * 60)
        result = await storage.get_shared_recordings_in_browser_session(
            organization_id="o_1", browser_session_id="pbs_old"
        )

    assert len(result) == 1
    assert _is_amazonaws_s3_url(result[0].url)
    build_url.assert_not_called()


@pytest.mark.asyncio
async def test_get_shared_recordings_keyring_unset_skips_artifact_lookup():
    """OSS default (no keyring) skips the artifact path — webhook consumers
    can't hit the signed endpoint without an API key."""
    from skyvern.config import settings

    storage = S3Storage()
    storage.async_client = MagicMock()
    s3_key = "v1/local/o_1/browser_sessions/pbs_1/videos/2026-04-26/recording.webm"
    storage.async_client.list_files = AsyncMock(return_value=[s3_key])
    storage.async_client.get_object_info = AsyncMock(
        return_value={"Metadata": {"sha256_checksum": "sha-x"}, "LastModified": None, "ContentLength": 1024}
    )
    storage.async_client.create_presigned_urls = AsyncMock(
        return_value=["https://skyvern-artifacts.s3.amazonaws.com/...?sig=fallback"]
    )

    mock_list = AsyncMock()  # must NOT be called

    with (
        patch.object(settings, "ARTIFACT_CONTENT_HMAC_KEYRING", None),
        patch("skyvern.forge.sdk.artifact.storage.s3.app") as s3_app,
    ):
        s3_app.DATABASE.artifacts.list_artifacts_for_browser_session_by_type = mock_list
        result = await storage.get_shared_recordings_in_browser_session(
            organization_id="o_1", browser_session_id="pbs_1"
        )

    assert len(result) == 1
    assert _is_amazonaws_s3_url(result[0].url)
    mock_list.assert_not_awaited()
