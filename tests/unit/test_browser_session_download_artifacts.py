"""Tests for the browser-session download artifact pipeline.

See ``cloud_docs/BROWSER_SESSION_DOWNLOAD_ARTIFACTS.md`` for the design.
The unit-level tests below cover:

- ``ArtifactManager.create_browser_session_download_artifact`` — DB-only
  helper used by the watcher write site, idempotent on
  ``(organization_id, browser_session_id, uri)``.
- ``S3Storage.sync_browser_session_file(artifact_type="downloads")`` — write
  site that registers the artifact row after a successful upload, skips
  files matching ``BROWSER_DOWNLOADING_SUFFIX``.
- ``S3Storage.get_shared_downloaded_files_in_browser_session`` — artifact-
  first read with legacy S3-list fallback.
- The end-of-run claim ``UPDATE`` is exercised separately by the
  repository-level tests (DB-shape only — no claim wiring yet).
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


def _make_artifact(
    artifact_id: str,
    uri: str,
    *,
    browser_session_id: str = "pbs_1",
    run_id: str | None = None,
    checksum: str | None = None,
    created_at: str = "2026-04-25T00:00:00Z",
) -> Artifact:
    return Artifact(
        artifact_id=artifact_id,
        artifact_type=ArtifactType.DOWNLOAD,
        uri=uri,
        organization_id="o_1",
        run_id=run_id,
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
# create_browser_session_download_artifact (manager helper)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_browser_session_download_artifact_inserts_when_no_existing_row():
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
        artifact_id = await manager.create_browser_session_download_artifact(
            organization_id="o_1",
            browser_session_id="pbs_1",
            uri="s3://skyvern-artifacts/v1/local/o_1/browser_sessions/pbs_1/downloads/file.pdf",
            filename="file.pdf",
            checksum="sha-xyz",
        )

    assert artifact_id.startswith("a_")
    mock_db_create.assert_awaited_once()
    _, kwargs = mock_db_create.call_args
    assert kwargs["artifact_type"] == ArtifactType.DOWNLOAD
    assert kwargs["browser_session_id"] == "pbs_1"
    assert kwargs["organization_id"] == "o_1"
    assert kwargs["checksum"] == "sha-xyz"
    # No run_id at write time — claim happens at run finalization.
    assert kwargs.get("run_id") is None


@pytest.mark.asyncio
async def test_create_browser_session_download_artifact_is_idempotent_per_session_and_uri():
    """The watcher fires repeatedly as a downloaded file grows. Every call
    after the first must reuse the existing artifact_id."""
    manager = ArtifactManager()
    existing = _make_artifact(
        "a_existing",
        "s3://skyvern-artifacts/v1/local/o_1/browser_sessions/pbs_1/downloads/file.pdf",
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
        artifact_id = await manager.create_browser_session_download_artifact(
            organization_id="o_1",
            browser_session_id="pbs_1",
            uri=existing.uri,
            filename="file.pdf",
            checksum="sha-xyz",
        )

    assert artifact_id == "a_existing"
    mock_db_create.assert_not_awaited()


# ---------------------------------------------------------------------------
# Storage write site — sync_browser_session_file
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sync_browser_session_file_registers_download_artifact():
    """A successful 'downloads' sync must create an Artifact row scoped to the session."""
    storage = S3Storage()
    storage.async_client = MagicMock()
    storage.async_client.upload_file_from_path = AsyncMock()

    mock_create = AsyncMock(return_value="a_new")
    mock_artifact_manager = MagicMock()
    mock_artifact_manager.create_browser_session_download_artifact = mock_create

    with (
        patch.object(storage, "_get_storage_class_for_org", new=AsyncMock(return_value=MagicMock())),
        patch("skyvern.forge.sdk.artifact.storage.s3.calculate_sha256_for_file", return_value="sha-1"),
        patch("skyvern.forge.sdk.artifact.storage.s3.app") as app_module,
    ):
        app_module.ARTIFACT_MANAGER = mock_artifact_manager
        await storage.sync_browser_session_file(
            organization_id="o_1",
            browser_session_id="pbs_1",
            artifact_type="downloads",
            local_file_path="/tmp/file.pdf",
            remote_path="file.pdf",
        )

    mock_create.assert_awaited_once()
    _, kwargs = mock_create.call_args
    assert kwargs["organization_id"] == "o_1"
    assert kwargs["browser_session_id"] == "pbs_1"
    assert kwargs["filename"] == "file.pdf"
    assert kwargs["checksum"] == "sha-1"
    assert kwargs["uri"].startswith("s3://") and "browser_sessions/pbs_1/downloads/" in kwargs["uri"]


@pytest.mark.asyncio
async def test_sync_browser_session_file_skips_artifact_for_unhandled_types():
    """Non-download / non-recording artifact_types (e.g. ``har``) don't create
    artifact rows — they're served by the legacy LIST path."""
    storage = S3Storage()
    storage.async_client = MagicMock()
    storage.async_client.upload_file_from_path = AsyncMock()

    mock_download = AsyncMock()
    mock_recording = AsyncMock()
    mock_artifact_manager = MagicMock()
    mock_artifact_manager.create_browser_session_download_artifact = mock_download
    mock_artifact_manager.create_browser_session_recording_artifact = mock_recording

    with (
        patch.object(storage, "_get_storage_class_for_org", new=AsyncMock(return_value=MagicMock())),
        patch("skyvern.forge.sdk.artifact.storage.s3.calculate_sha256_for_file", return_value="sha-2"),
        patch("skyvern.forge.sdk.artifact.storage.s3.app") as app_module,
    ):
        app_module.ARTIFACT_MANAGER = mock_artifact_manager
        await storage.sync_browser_session_file(
            organization_id="o_1",
            browser_session_id="pbs_1",
            artifact_type="har",
            local_file_path="/tmp/network.har",
            remote_path="network.har",
        )

    mock_download.assert_not_awaited()
    mock_recording.assert_not_awaited()


@pytest.mark.asyncio
async def test_sync_browser_session_file_propagates_artifact_row_failure():
    """If the artifact-row insert raises after the upload succeeds, the storage
    layer must propagate so the watcher's bounded retry catches it.
    Swallowing would leave the file in S3 with no row — invisible to the
    DB-backed agent baseline diffs."""
    storage = S3Storage()
    storage.async_client = MagicMock()
    storage.async_client.upload_file_from_path = AsyncMock()

    mock_create = AsyncMock(side_effect=RuntimeError("DB unreachable"))
    mock_artifact_manager = MagicMock()
    mock_artifact_manager.create_browser_session_download_artifact = mock_create

    with (
        patch.object(storage, "_get_storage_class_for_org", new=AsyncMock(return_value=MagicMock())),
        patch("skyvern.forge.sdk.artifact.storage.s3.calculate_sha256_for_file", return_value="sha-99"),
        patch("skyvern.forge.sdk.artifact.storage.s3.app") as app_module,
    ):
        app_module.ARTIFACT_MANAGER = mock_artifact_manager
        with pytest.raises(RuntimeError, match="DB unreachable"):
            await storage.sync_browser_session_file(
                organization_id="o_1",
                browser_session_id="pbs_1",
                artifact_type="downloads",
                local_file_path="/tmp/file.pdf",
                remote_path="file.pdf",
            )


@pytest.mark.asyncio
async def test_sync_browser_session_file_creates_partial_artifact_with_null_checksum():
    """Partials (``*.crdownload``) get an artifact row with checksum=None so
    the agent can detect "still downloading" via DB query. The row is dropped
    when Chrome's atomic rename fires Change.deleted."""
    from skyvern.constants import BROWSER_DOWNLOADING_SUFFIX

    storage = S3Storage()
    storage.async_client = MagicMock()
    storage.async_client.upload_file_from_path = AsyncMock()

    mock_create = AsyncMock(return_value="a_partial")
    mock_artifact_manager = MagicMock()
    mock_artifact_manager.create_browser_session_download_artifact = mock_create
    mock_checksum = MagicMock()  # must NOT be called for partials

    with (
        patch.object(storage, "_get_storage_class_for_org", new=AsyncMock(return_value=MagicMock())),
        patch("skyvern.forge.sdk.artifact.storage.s3.calculate_sha256_for_file", mock_checksum),
        patch("skyvern.forge.sdk.artifact.storage.s3.app") as app_module,
    ):
        app_module.ARTIFACT_MANAGER = mock_artifact_manager
        await storage.sync_browser_session_file(
            organization_id="o_1",
            browser_session_id="pbs_1",
            artifact_type="downloads",
            local_file_path="/tmp/file.pdf.crdownload",
            remote_path=f"file.pdf{BROWSER_DOWNLOADING_SUFFIX}",
        )

    mock_create.assert_awaited_once()
    _, kwargs = mock_create.call_args
    assert kwargs["checksum"] is None
    assert kwargs["uri"].endswith(BROWSER_DOWNLOADING_SUFFIX)
    # Partial files: checksum computation skipped — file is mid-write.
    mock_checksum.assert_not_called()


# ---------------------------------------------------------------------------
# Storage read site — get_shared_downloaded_files_in_browser_session
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_shared_downloaded_files_in_browser_session_uses_artifact_urls(keyring_configured):
    storage = S3Storage()
    storage.async_client = MagicMock()
    storage.async_client.list_files = AsyncMock()  # must NOT be called
    storage.async_client.create_presigned_urls = AsyncMock()  # must NOT be called

    artifact = _make_artifact(
        "a_42",
        "s3://skyvern-artifacts/v1/local/o_1/browser_sessions/pbs_1/downloads/invoice.pdf",
        checksum="sha-from-db",
    )
    mock_list = AsyncMock(return_value=[artifact])
    build_url = MagicMock(return_value="https://api.skyvern.com/v1/artifacts/a_42/content?expiry=x&kid=y&sig=z")

    with patch("skyvern.forge.sdk.artifact.storage.base.app") as base_app:
        with patch("skyvern.forge.sdk.artifact.storage.s3.app") as s3_app:
            s3_app.DATABASE.artifacts.list_artifacts_for_browser_session_by_type = mock_list
            base_app.ARTIFACT_MANAGER.build_signed_content_url = build_url
            base_app.ARTIFACT_MANAGER.resolve_artifact_url_expiry_seconds = AsyncMock(return_value=12 * 60 * 60)
            result = await storage.get_shared_downloaded_files_in_browser_session(
                organization_id="o_1", browser_session_id="pbs_1"
            )

    assert len(result) == 1
    assert result[0].url.startswith("https://api.skyvern.com/v1/artifacts/a_42/content")
    assert result[0].checksum == "sha-from-db"
    storage.async_client.list_files.assert_not_awaited()
    storage.async_client.create_presigned_urls.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_shared_downloaded_files_in_browser_session_falls_back_to_presigned_for_legacy(keyring_configured):
    """Pre-cutover sessions have no artifact rows. Files must still surface as presigned URLs."""
    storage = S3Storage()
    storage.async_client = MagicMock()
    object_uri = "s3://skyvern-artifacts/v1/local/o_1/browser_sessions/pbs_old/downloads/legacy.pdf"
    storage.async_client.list_files = AsyncMock(return_value=[object_uri.split("/", 3)[-1]])
    storage.async_client.get_object_info = AsyncMock(
        return_value={
            "Metadata": {"sha256_checksum": "sha-old", "original_filename": "legacy.pdf"},
            "LastModified": None,
        }
    )
    storage.async_client.create_presigned_urls = AsyncMock(
        return_value=["https://skyvern-artifacts.s3.amazonaws.com/...?sig=old"]
    )

    mock_list = AsyncMock(return_value=[])
    build_url = MagicMock()  # must NOT be called

    with patch("skyvern.forge.sdk.artifact.storage.base.app") as base_app:
        with patch("skyvern.forge.sdk.artifact.storage.s3.app") as s3_app:
            s3_app.DATABASE.artifacts.list_artifacts_for_browser_session_by_type = mock_list
            base_app.ARTIFACT_MANAGER.build_signed_content_url = build_url
            result = await storage.get_shared_downloaded_files_in_browser_session(
                organization_id="o_1", browser_session_id="pbs_old"
            )

    assert len(result) == 1
    assert _is_amazonaws_s3_url(result[0].url)
    build_url.assert_not_called()


@pytest.mark.asyncio
async def test_get_shared_downloaded_files_in_browser_session_filters_partial_artifacts(keyring_configured):
    """User-facing listing must hide ``*.crdownload`` rows even when DB returns them.
    Partial rows exist for the agent's "still downloading" check, not for end users."""
    storage = S3Storage()
    storage.async_client = MagicMock()
    storage.async_client.list_files = AsyncMock()  # must NOT be called

    completed = _make_artifact(
        "a_done",
        "s3://skyvern-artifacts/v1/local/o_1/browser_sessions/pbs_1/downloads/done.pdf",
        checksum="sha-1",
    )
    partial = _make_artifact(
        "a_partial",
        "s3://skyvern-artifacts/v1/local/o_1/browser_sessions/pbs_1/downloads/inflight.pdf.crdownload",
    )
    mock_list = AsyncMock(return_value=[partial, completed])
    build_url = MagicMock(return_value="https://api.skyvern.com/v1/artifacts/a_done/content?expiry=x&kid=y&sig=z")

    with (
        patch("skyvern.forge.sdk.artifact.storage.base.app") as base_app,
        patch("skyvern.forge.sdk.artifact.storage.s3.app") as s3_app,
    ):
        s3_app.DATABASE.artifacts.list_artifacts_for_browser_session_by_type = mock_list
        base_app.ARTIFACT_MANAGER.build_signed_content_url = build_url
        base_app.ARTIFACT_MANAGER.resolve_artifact_url_expiry_seconds = AsyncMock(return_value=12 * 60 * 60)
        result = await storage.get_shared_downloaded_files_in_browser_session(
            organization_id="o_1", browser_session_id="pbs_1"
        )

    assert len(result) == 1
    assert "a_done" in result[0].url
    storage.async_client.list_files.assert_not_awaited()


@pytest.mark.asyncio
async def test_list_downloaded_files_in_browser_session_db_backed_filters_partials(keyring_configured):
    """list_downloaded_files_in_browser_session is DB-backed and must exclude
    partials — the agent uses this for baseline diff and a .crdownload entry
    would falsely look like a completed download."""
    storage = S3Storage()
    storage.async_client = MagicMock()
    storage.async_client.list_files = AsyncMock()  # must NOT be called

    completed = _make_artifact(
        "a_done",
        "s3://skyvern-artifacts/v1/local/o_1/browser_sessions/pbs_1/downloads/done.pdf",
    )
    partial = _make_artifact(
        "a_partial",
        "s3://skyvern-artifacts/v1/local/o_1/browser_sessions/pbs_1/downloads/inflight.pdf.crdownload",
    )
    mock_list = AsyncMock(return_value=[partial, completed])

    with patch("skyvern.forge.sdk.artifact.storage.s3.app") as s3_app:
        s3_app.DATABASE.artifacts.list_artifacts_for_browser_session_by_type = mock_list
        result = await storage.list_downloaded_files_in_browser_session(
            organization_id="o_1", browser_session_id="pbs_1"
        )

    assert result == [completed.uri]
    storage.async_client.list_files.assert_not_awaited()


@pytest.mark.asyncio
async def test_list_downloading_files_in_browser_session_db_backed_returns_only_partials(keyring_configured):
    """list_downloading_files_in_browser_session is DB-backed and must return
    only ``*.crdownload`` rows — the agent waits on these for complete_on_download."""
    storage = S3Storage()
    storage.async_client = MagicMock()
    storage.async_client.list_files = AsyncMock()  # must NOT be called

    completed = _make_artifact(
        "a_done",
        "s3://skyvern-artifacts/v1/local/o_1/browser_sessions/pbs_1/downloads/done.pdf",
    )
    partial = _make_artifact(
        "a_partial",
        "s3://skyvern-artifacts/v1/local/o_1/browser_sessions/pbs_1/downloads/inflight.pdf.crdownload",
    )
    mock_list = AsyncMock(return_value=[completed, partial])

    with patch("skyvern.forge.sdk.artifact.storage.s3.app") as s3_app:
        s3_app.DATABASE.artifacts.list_artifacts_for_browser_session_by_type = mock_list
        result = await storage.list_downloading_files_in_browser_session(
            organization_id="o_1", browser_session_id="pbs_1"
        )

    assert result == [partial.uri]
    storage.async_client.list_files.assert_not_awaited()


@pytest.mark.asyncio
async def test_list_downloads_falls_back_to_s3_listing_when_db_raises(keyring_configured):
    """Transient DB outage must not break the agent — fall back to S3 LIST."""
    storage = S3Storage()
    storage.async_client = MagicMock()
    s3_key = "v1/local/o_1/browser_sessions/pbs_1/downloads/legacy.pdf"
    storage.async_client.list_files = AsyncMock(return_value=[s3_key])

    mock_list = AsyncMock(side_effect=RuntimeError("DB unreachable"))

    with patch("skyvern.forge.sdk.artifact.storage.s3.app") as s3_app:
        s3_app.DATABASE.artifacts.list_artifacts_for_browser_session_by_type = mock_list
        result = await storage.list_downloaded_files_in_browser_session(
            organization_id="o_1", browser_session_id="pbs_1"
        )

    assert len(result) == 1
    assert result[0].endswith("legacy.pdf")  # nosemgrep: incomplete-url-substring-sanitization
    storage.async_client.list_files.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_shared_downloaded_files_in_browser_session_keyring_unset_skips_artifact_lookup():
    """OSS default (no keyring) must skip the artifact path entirely — webhook consumers
    don't have an API key to hit the signed endpoint."""
    from skyvern.config import settings

    storage = S3Storage()
    storage.async_client = MagicMock()
    object_uri = "s3://skyvern-artifacts/v1/local/o_1/browser_sessions/pbs_1/downloads/legacy.pdf"
    storage.async_client.list_files = AsyncMock(return_value=[object_uri.split("/", 3)[-1]])
    storage.async_client.get_object_info = AsyncMock(
        return_value={"Metadata": {"sha256_checksum": "sha-x"}, "LastModified": None}
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
        result = await storage.get_shared_downloaded_files_in_browser_session(
            organization_id="o_1", browser_session_id="pbs_1"
        )

    assert len(result) == 1
    assert _is_amazonaws_s3_url(result[0].url)
    mock_list.assert_not_awaited()


# ---------------------------------------------------------------------------
# Storage delete site — delete_browser_session_file
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_browser_session_file_drops_artifact_row_for_downloads():
    """When the watcher fires Change.deleted for a download, the artifact row
    must be removed too — otherwise the next session read returns a signed
    URL pointing at a deleted S3 object."""
    storage = S3Storage()
    storage.async_client = MagicMock()
    storage.async_client.delete_file = AsyncMock()

    mock_delete_row = AsyncMock(return_value=1)

    with patch("skyvern.forge.sdk.artifact.storage.s3.app") as app_module:
        app_module.DATABASE.artifacts.delete_artifact_for_browser_session = mock_delete_row
        await storage.delete_browser_session_file(
            organization_id="o_1",
            browser_session_id="pbs_1",
            artifact_type="downloads",
            remote_path="invoice.pdf",
        )

    mock_delete_row.assert_awaited_once()
    _, kwargs = mock_delete_row.call_args
    assert kwargs["organization_id"] == "o_1"
    assert kwargs["browser_session_id"] == "pbs_1"
    assert kwargs["artifact_type"] == ArtifactType.DOWNLOAD
    assert "browser_sessions/pbs_1/downloads/" in kwargs["uri"]
    storage.async_client.delete_file.assert_awaited_once()


@pytest.mark.asyncio
async def test_delete_browser_session_file_skips_row_delete_for_non_download_types():
    """Videos/HAR have no artifact rows — don't even attempt the DB delete."""
    storage = S3Storage()
    storage.async_client = MagicMock()
    storage.async_client.delete_file = AsyncMock()

    mock_delete_row = AsyncMock()

    with patch("skyvern.forge.sdk.artifact.storage.s3.app") as app_module:
        app_module.DATABASE.artifacts.delete_artifact_for_browser_session = mock_delete_row
        await storage.delete_browser_session_file(
            organization_id="o_1",
            browser_session_id="pbs_1",
            artifact_type="videos",
            remote_path="recording.webm",
        )

    mock_delete_row.assert_not_awaited()
    storage.async_client.delete_file.assert_awaited_once()


@pytest.mark.asyncio
async def test_delete_browser_session_file_swallows_db_failure_and_still_deletes_s3():
    """A transient DB error must not block S3 cleanup — the listing fallback
    can still surface the file otherwise."""
    storage = S3Storage()
    storage.async_client = MagicMock()
    storage.async_client.delete_file = AsyncMock()

    mock_delete_row = AsyncMock(side_effect=RuntimeError("DB unreachable"))

    with patch("skyvern.forge.sdk.artifact.storage.s3.app") as app_module:
        app_module.DATABASE.artifacts.delete_artifact_for_browser_session = mock_delete_row
        await storage.delete_browser_session_file(
            organization_id="o_1",
            browser_session_id="pbs_1",
            artifact_type="downloads",
            remote_path="invoice.pdf",
        )

    storage.async_client.delete_file.assert_awaited_once()


# Watcher-level tests for browser_controller live under tests/cloud/ — the
# browser_controller module imports cloud-only dependencies (redis client) and
# can't load in the OSS-synced unit suite.
