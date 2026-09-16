"""Tests for downloaded_files migration to short artifact URLs (SKY-8861)."""

from __future__ import annotations

import hashlib
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import urlparse

import pytest

import skyvern.forge.sdk.artifact.manager as manager_module
import skyvern.forge.sdk.artifact.storage.azure as azure_module
import skyvern.forge.sdk.artifact.storage.base as base_module
import skyvern.forge.sdk.artifact.storage.gcs as gcs_module
import skyvern.forge.sdk.artifact.storage.local as local_module
import skyvern.forge.sdk.artifact.storage.s3 as s3_module
from skyvern.config import settings
from skyvern.forge.sdk.api.files import parse_uri_to_path
from skyvern.forge.sdk.artifact.manager import ArtifactManager
from skyvern.forge.sdk.artifact.models import Artifact, ArtifactType
from skyvern.forge.sdk.artifact.storage.local import LocalStorage
from skyvern.forge.sdk.artifact.storage.s3 import S3Storage
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.schemas.files import FileInfo
from skyvern.forge.sdk.workflow.context_manager import WorkflowContextManager
from skyvern.forge.sdk.workflow.loop_download_filter import (
    DOWNLOADED_FILE_SIGS_KEY,
    filter_downloaded_files_for_current_iteration,
    to_downloaded_file_signature,
)
from skyvern.forge.sdk.workflow.models import block as block_module
from skyvern.forge.sdk.workflow.models.block import PrintPageBlock
from skyvern.forge.sdk.workflow.models.parameter import OutputParameter, ParameterType
from skyvern.forge.sdk.workflow.runtime_completion import CompletionCriterion, grade_completion_contract
from skyvern.forge.sdk.workflow.service import WorkflowService
from tests.unit.conftest import FakeWorkflowRunAttemptsRepository
from tests.unit.forge.sdk.artifact.storage.test_azure_storage import AzureStorageForTests
from tests.unit.forge.sdk.artifact.storage.test_gcs_storage import GcsStorageForTests


def _is_amazonaws_s3_url(url: str) -> bool:
    """Strict check that ``url`` is a real ``*.s3.amazonaws.com`` URL.

    Avoids the substring trap CodeQL flags as ``py/incomplete-url-substring-sanitization``
    — ``"s3.amazonaws.com" in url`` matches ``http://evil.com/?x=s3.amazonaws.com``
    and similar bypasses. Parse the URL and check the hostname suffix instead.
    """
    host = urlparse(url).hostname
    if host is None:
        return False
    return host == "s3.amazonaws.com" or host.endswith(".s3.amazonaws.com")


@pytest.mark.asyncio
async def test_create_download_artifact_is_idempotent_per_run_and_uri():
    """A repeat save (e.g. inside a loop) must return the existing artifact_id so
    downstream URL-based dedup (``loop_download_filter``) keeps seeing a stable URL.
    """
    manager = ArtifactManager()

    existing = Artifact(
        artifact_id="a_existing",
        artifact_type=ArtifactType.DOWNLOAD,
        uri="s3://skyvern-uploads/downloads/local/o_1/wr_1/file.pdf",
        organization_id="o_1",
        run_id="wr_1",
        workflow_run_id="wr_1",
        created_at="2026-04-23T00:00:00Z",
        modified_at="2026-04-23T00:00:00Z",
    )
    find_existing = AsyncMock(return_value=existing)
    mock_db_create = AsyncMock()
    mock_refresh = AsyncMock()

    with (
        patch(
            "skyvern.forge.sdk.artifact.manager.app.DATABASE.artifacts.find_download_artifact",
            find_existing,
        ),
        patch(
            "skyvern.forge.sdk.artifact.manager.app.DATABASE.artifacts.create_artifact",
            mock_db_create,
        ),
        patch(
            "skyvern.forge.sdk.artifact.manager.app.DATABASE.artifacts.refresh_download_artifact_content",
            mock_refresh,
        ),
    ):
        artifact_id = await manager.create_download_artifact(
            organization_id="o_1",
            run_id="wr_1",
            workflow_run_id="wr_1",
            uri=existing.uri,
            filename="file.pdf",
        )

    assert artifact_id == "a_existing"
    mock_db_create.assert_not_awaited()
    mock_refresh.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_download_artifact_inserts_row_without_uploading():
    """create_download_artifact only writes a DB row; bytes are already in S3."""
    manager = ArtifactManager()

    mock_db_create = AsyncMock(
        return_value=Artifact(
            artifact_id="a_abc123",
            artifact_type=ArtifactType.DOWNLOAD,
            uri="s3://skyvern-uploads/download/prod/o_1/wr_1/file.pdf",
            organization_id="o_1",
            run_id="wr_1",
            workflow_run_id="wr_1",
            created_at="2026-04-23T00:00:00Z",
            modified_at="2026-04-23T00:00:00Z",
        )
    )
    mock_store = AsyncMock()
    find_existing = AsyncMock(return_value=None)

    with (
        patch(
            "skyvern.forge.sdk.artifact.manager.app.DATABASE.artifacts.find_download_artifact",
            find_existing,
        ),
        patch("skyvern.forge.sdk.artifact.manager.app.DATABASE.artifacts.create_artifact", mock_db_create),
        patch("skyvern.forge.sdk.artifact.manager.app.STORAGE.store_artifact", mock_store),
        patch("skyvern.forge.sdk.artifact.manager.app.STORAGE.store_artifact_from_path", mock_store),
    ):
        artifact_id = await manager.create_download_artifact(
            organization_id="o_1",
            run_id="wr_1",
            workflow_run_id="wr_1",
            uri="s3://skyvern-uploads/download/prod/o_1/wr_1/file.pdf",
            filename="file.pdf",
            file_size=123,
        )

    assert artifact_id.startswith("a_")
    mock_db_create.assert_awaited_once()
    _, kwargs = mock_db_create.call_args
    assert kwargs["artifact_type"] == ArtifactType.DOWNLOAD
    assert kwargs["uri"] == "s3://skyvern-uploads/download/prod/o_1/wr_1/file.pdf"
    assert kwargs["organization_id"] == "o_1"
    assert kwargs["run_id"] == "wr_1"
    assert kwargs["workflow_run_id"] == "wr_1"
    assert kwargs["file_size"] == 123
    mock_store.assert_not_awaited()


@pytest.mark.asyncio
async def test_save_downloaded_files_registers_artifact_per_file(tmp_path, monkeypatch):
    """After uploading each file to S3, save_downloaded_files should create an
    Artifact row so later retrieval can build short /v1/artifacts URLs."""
    download_dir = tmp_path / "downloads"
    download_dir.mkdir()
    (download_dir / "invoice.pdf").write_bytes(b"%PDF-1.4 ...")
    (download_dir / "report.csv").write_bytes(b"a,b,c\n1,2,3\n")

    storage = S3Storage()
    storage.async_client = MagicMock()
    storage.async_client.upload_file_from_path = AsyncMock()

    mock_create_download = AsyncMock(return_value="a_new")
    mock_artifact_manager = MagicMock()
    mock_artifact_manager.create_download_artifact = mock_create_download

    with (
        patch("skyvern.forge.sdk.artifact.storage.s3.get_download_dir", return_value=str(download_dir)),
        patch.object(storage, "_get_storage_class_for_org", new=AsyncMock(return_value=MagicMock())),
        patch("skyvern.forge.sdk.artifact.storage.s3.calculate_sha256_for_file", return_value="sha-xyz"),
        patch("skyvern.forge.sdk.artifact.storage.s3.app") as app_module,
    ):
        app_module.ARTIFACT_MANAGER = mock_artifact_manager
        app_module.DATABASE.workflow_runs.get_workflow_run = AsyncMock(return_value=None)
        app_module.DATABASE.workflow_run_attempts = FakeWorkflowRunAttemptsRepository()
        monkeypatch.setattr(base_module, "app", app_module)
        await storage.save_downloaded_files(organization_id="o_1", run_id="wr_1")

    assert mock_create_download.await_count == 2
    uris = {call.kwargs["uri"] for call in mock_create_download.await_args_list}
    filenames = {call.kwargs["filename"] for call in mock_create_download.await_args_list}
    file_sizes = {call.kwargs["filename"]: call.kwargs["file_size"] for call in mock_create_download.await_args_list}
    assert filenames == {"invoice.pdf", "report.csv"}
    assert file_sizes == {
        "invoice.pdf": (download_dir / "invoice.pdf").stat().st_size,
        "report.csv": (download_dir / "report.csv").stat().st_size,
    }
    assert all(u.startswith("s3://") and "/downloads/" in u and "/o_1/wr_1/" in u for u in uris)
    for call in mock_create_download.await_args_list:
        assert call.kwargs["organization_id"] == "o_1"
        assert call.kwargs["run_id"] == "wr_1"


@pytest.mark.asyncio
async def test_local_retry_snapshot_uri_survives_fragment_and_query_characters(tmp_path, monkeypatch):
    # A raw file:// URI turns "#" and "?" into a fragment and a query, so the snapshot path read back
    # from the artifact row no longer exists and the saved download drops out of the listing.
    storage = LocalStorage(str(tmp_path / "artifacts"))
    download_dir = tmp_path / "downloads"
    download_dir.mkdir()
    (download_dir / "report #1?.pdf").write_bytes(b"attempt two bytes")
    rows: list[Artifact] = []

    async def register(**kwargs):
        rows.append(
            _make_artifact(
                f"a_{len(rows)}",
                kwargs["uri"],
                checksum=kwargs["checksum"],
                file_size=kwargs["file_size"],
                created_at=datetime.now(UTC).isoformat(),
            )
        )

    fake_app = SimpleNamespace(
        STORAGE=storage,
        ARTIFACT_MANAGER=SimpleNamespace(create_download_artifact=register),
        DATABASE=SimpleNamespace(
            artifacts=SimpleNamespace(list_artifacts_for_run_by_type=AsyncMock(side_effect=lambda **kwargs: list(rows)))
        ),
    )
    monkeypatch.setattr(local_module, "app", fake_app)
    monkeypatch.setattr(local_module, "get_download_dir", lambda *args, **kwargs: str(download_dir))
    started_at = datetime.now(UTC) - timedelta(minutes=1)
    monkeypatch.setattr(base_module, "resolve_download_attempt", AsyncMock(return_value=("wr_1", 2, started_at)))

    await storage.save_downloaded_files(organization_id="o_1", run_id="wr_1")

    assert len(rows) == 1
    snapshot = Path(parse_uri_to_path(rows[0].uri))
    assert snapshot.name == "report #1?.pdf"
    assert snapshot.parent.name == "2"
    assert snapshot.read_bytes() == b"attempt two bytes"
    listed = await storage.get_downloaded_files("o_1", "wr_1")
    assert [(file.filename, file.artifact_id) for file in listed] == [("report #1?.pdf", "a_0")]


def _make_artifact(
    artifact_id: str,
    uri: str,
    run_id: str = "wr_1",
    *,
    checksum: str | None = None,
    file_size: int | None = None,
    created_at: str = "2026-04-23T00:00:00Z",
) -> Artifact:
    return Artifact(
        artifact_id=artifact_id,
        artifact_type=ArtifactType.DOWNLOAD,
        uri=uri,
        organization_id="o_1",
        run_id=run_id,
        workflow_run_id=run_id if run_id.startswith("wr_") else None,
        checksum=checksum,
        file_size=file_size,
        created_at=created_at,
        modified_at=created_at,
    )


_DUMMY_KEYRING_JSON = '{"current_kid": "k1", "keys": {"k1": {"secret": "0000000000000000000000000000000000000000000000000000000000000000"}}}'


@pytest.fixture
def keyring_configured():
    """Simulate cloud-style config: HMAC keyring is set so the artifact URL branch is active.
    Unit tests default to no keyring to match the OSS default, so tests that exercise the
    short-URL path must opt in."""
    from skyvern.config import settings

    with patch.object(settings, "ARTIFACT_CONTENT_HMAC_KEYRING", _DUMMY_KEYRING_JSON):
        yield


@pytest.mark.asyncio
async def test_get_downloaded_files_uses_artifact_urls_when_rows_exist(keyring_configured):
    """When DOWNLOAD artifact rows exist, retrieval skips S3 entirely:
    URL, checksum, filename, modified_at all come straight from the row."""
    storage = S3Storage()
    storage.async_client = MagicMock()
    storage.async_client.list_files = AsyncMock()  # must NOT be called
    storage.async_client.get_file_metadata = AsyncMock()  # must NOT be called
    storage.async_client.create_presigned_urls = AsyncMock()  # must NOT be called

    artifact = _make_artifact(
        "a_42",
        "s3://skyvern-uploads/downloads/local/o_1/wr_1/invoice.pdf",
        checksum="sha-from-db",
        file_size=4096,
    )
    mock_list = AsyncMock(return_value=[artifact])
    resolve_url = AsyncMock(return_value="https://api.skyvern.com/v1/artifacts/a_42/content?expiry=x&kid=y&sig=z")

    with patch("skyvern.forge.sdk.artifact.storage.base.app") as base_app:
        with patch("skyvern.forge.sdk.artifact.storage.s3.app") as s3_app:
            s3_app.DATABASE.artifacts.list_artifacts_for_run_by_type = mock_list
            base_app.ARTIFACT_MANAGER.resolve_share_url = resolve_url
            base_app.ARTIFACT_MANAGER.resolve_artifact_url_expiry_seconds = AsyncMock(return_value=12 * 60 * 60)
            result = await storage.get_downloaded_files(organization_id="o_1", run_id="wr_1")

    assert len(result) == 1
    assert result[0].url.startswith("https://api.skyvern.com/v1/artifacts/a_42/content")
    assert result[0].filename == "invoice.pdf"
    assert result[0].checksum == "sha-from-db"
    assert result[0].file_size == 4096
    assert result[0].modified_at is not None
    storage.async_client.list_files.assert_not_awaited()
    storage.async_client.get_file_metadata.assert_not_awaited()
    storage.async_client.create_presigned_urls.assert_not_awaited()
    mock_list.assert_awaited_once_with(run_id="wr_1", organization_id="o_1", artifact_type=ArtifactType.DOWNLOAD)


@pytest.mark.asyncio
async def test_get_downloaded_files_preserves_artifact_row_order(keyring_configured):
    """Artifact rows are returned ASC by created_at; FileInfo list must follow the
    same order (matches save order, drives loop_download_filter signatures)."""
    storage = S3Storage()
    storage.async_client = MagicMock()

    first = _make_artifact(
        "a_1",
        "s3://skyvern-uploads/downloads/local/o_1/wr_1/first.pdf",
        created_at="2026-04-23T00:00:00Z",
    )
    second = _make_artifact(
        "a_2",
        "s3://skyvern-uploads/downloads/local/o_1/wr_1/second.pdf",
        created_at="2026-04-23T00:01:00Z",
    )
    mock_list = AsyncMock(return_value=[first, second])
    resolve_url = AsyncMock(
        side_effect=lambda artifact, **_: f"https://api.skyvern.com/v1/artifacts/{artifact.artifact_id}/content"
    )

    with patch("skyvern.forge.sdk.artifact.storage.base.app") as base_app:
        with patch("skyvern.forge.sdk.artifact.storage.s3.app") as s3_app:
            s3_app.DATABASE.artifacts.list_artifacts_for_run_by_type = mock_list
            base_app.ARTIFACT_MANAGER.resolve_share_url = resolve_url
            base_app.ARTIFACT_MANAGER.resolve_artifact_url_expiry_seconds = AsyncMock(return_value=12 * 60 * 60)
            result = await storage.get_downloaded_files(organization_id="o_1", run_id="wr_1")

    assert [fi.filename for fi in result] == ["first.pdf", "second.pdf"]


@pytest.mark.asyncio
async def test_get_downloaded_files_falls_back_to_presigned_for_legacy_runs(keyring_configured):
    """Production-cloud legacy run: keyring IS configured, but the run pre-dates SKY-8861
    so no artifact rows exist. Files in S3 must still surface as presigned URLs — the
    whole point of keeping the fallback path."""
    storage = S3Storage()
    storage.async_client = MagicMock()
    s3_key = "downloads/local/o_1/wr_old/legacy.pdf"
    storage.async_client.list_files = AsyncMock(return_value=[s3_key])
    storage.async_client.get_object_info = AsyncMock(
        return_value={
            "Metadata": {"sha256_checksum": "sha-old", "original_filename": "legacy.pdf"},
            "ContentLength": 2048,
        }
    )
    storage.async_client.create_presigned_urls = AsyncMock(
        return_value=["https://skyvern-uploads.s3.amazonaws.com/...?sig=old"]
    )

    mock_list = AsyncMock(return_value=[])  # no artifact rows for this legacy run
    build_url = MagicMock()  # must NOT be called

    with patch("skyvern.forge.sdk.artifact.storage.base.app") as base_app:
        with patch("skyvern.forge.sdk.artifact.storage.s3.app") as s3_app:
            s3_app.DATABASE.artifacts.list_artifacts_for_run_by_type = mock_list
            base_app.ARTIFACT_MANAGER.build_signed_content_url = build_url
            result = await storage.get_downloaded_files(organization_id="o_1", run_id="wr_old")

    assert len(result) == 1
    assert result[0].filename == "legacy.pdf"
    assert result[0].checksum == "sha-old"
    assert result[0].file_size == 2048
    assert _is_amazonaws_s3_url(result[0].url)
    build_url.assert_not_called()
    storage.async_client.list_files.assert_awaited_once()
    storage.async_client.create_presigned_urls.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_downloaded_files_falls_back_to_presigned_when_keyring_unset(tmp_path):
    """Self-hosted OSS deployments without ARTIFACT_CONTENT_HMAC_KEYRING must keep
    serving presigned S3 URLs — the short Skyvern URL would be unsigned and the
    content endpoint would 401 without an API key."""
    from skyvern.config import settings

    storage = S3Storage()
    storage.async_client = MagicMock()
    s3_key = "downloads/local/o_1/wr_1/invoice.pdf"
    storage.async_client.list_files = AsyncMock(return_value=[s3_key])
    storage.async_client.get_object_info = AsyncMock(
        return_value={
            "Metadata": {"sha256_checksum": "sha-abc", "original_filename": "invoice.pdf"},
            "ContentLength": 1024,
        }
    )
    storage.async_client.create_presigned_urls = AsyncMock(
        return_value=["https://skyvern-uploads.s3.amazonaws.com/...?sig=fallback"]
    )

    artifact = _make_artifact("a_42", f"s3://skyvern-uploads/{s3_key}")
    mock_list = AsyncMock(return_value=[artifact])
    build_url = MagicMock()

    with (
        patch("skyvern.forge.sdk.artifact.storage.s3.app") as app_module,
        patch.object(settings, "ARTIFACT_CONTENT_HMAC_KEYRING", None),
    ):
        app_module.DATABASE.artifacts.list_artifacts_for_run_by_type = mock_list
        app_module.ARTIFACT_MANAGER.build_signed_content_url = build_url
        result = await storage.get_downloaded_files(organization_id="o_1", run_id="wr_1")

    assert len(result) == 1
    assert _is_amazonaws_s3_url(result[0].url)
    assert result[0].file_size == 1024
    build_url.assert_not_called()


@pytest.mark.asyncio
async def test_get_downloaded_files_artifact_lookup_failure_falls_back_to_listing(keyring_configured):
    """If the DB lookup raises (transient outage), retrieval must not 500 the
    run-output API — fall through to the legacy S3-listing path so files
    still surface as presigned URLs."""
    storage = S3Storage()
    storage.async_client = MagicMock()
    s3_key = "downloads/local/o_1/wr_1/recoverable.pdf"
    storage.async_client.list_files = AsyncMock(return_value=[s3_key])
    storage.async_client.get_object_info = AsyncMock(
        return_value={
            "Metadata": {"sha256_checksum": "sha-recover", "original_filename": "recoverable.pdf"},
            "ContentLength": 512,
        }
    )
    storage.async_client.create_presigned_urls = AsyncMock(
        return_value=["https://skyvern-uploads.s3.amazonaws.com/...?sig=fallback"]
    )

    mock_list = AsyncMock(side_effect=RuntimeError("DB unreachable"))

    with patch("skyvern.forge.sdk.artifact.storage.s3.app") as app_module:
        app_module.DATABASE.artifacts.list_artifacts_for_run_by_type = mock_list
        result = await storage.get_downloaded_files(organization_id="o_1", run_id="wr_1")

    assert len(result) == 1
    assert _is_amazonaws_s3_url(result[0].url)
    assert result[0].file_size == 512
    storage.async_client.list_files.assert_awaited_once()


@pytest.mark.asyncio
async def test_content_endpoint_download_returns_attachment_with_filename():
    """DOWNLOAD artifacts must serve with attachment disposition so browsers don't render
    PDFs inline (defeats the SKY-8862 XSS-via-PDF mitigation)."""
    from skyvern.forge.sdk.routes.agent_protocol import _artifact_response_config

    artifact = _make_artifact("a_dl", "s3://skyvern-uploads/downloads/local/o_1/wr_1/invoice.pdf")
    media_type, disposition = _artifact_response_config(artifact)
    assert media_type == "application/octet-stream"
    assert disposition.startswith("attachment;")
    assert 'filename="invoice.pdf"' in disposition
    local_snapshot = _make_artifact(
        "a_local", "file:///tmp/artifacts/downloads/local/o_1/wr_1/attempts/2/report%20%231.pdf"
    )
    _, local_disposition = _artifact_response_config(local_snapshot)
    assert 'filename="report #1.pdf"' in local_disposition


def test_content_endpoint_non_download_stays_inline():
    """Existing artifact types keep the inline disposition we had before."""
    from skyvern.forge.sdk.routes.agent_protocol import _artifact_response_config

    screenshot = Artifact(
        artifact_id="a_ss",
        artifact_type=ArtifactType.SCREENSHOT_FINAL,
        uri="s3://skyvern-artifacts/.../final.png",
        organization_id="o_1",
        created_at="2026-04-23T00:00:00Z",
        modified_at="2026-04-23T00:00:00Z",
    )
    media_type, disposition = _artifact_response_config(screenshot)
    assert media_type == "image/png"
    assert disposition == "inline"


def test_content_endpoint_audio_artifact_serves_audio_webm():
    from skyvern.forge.sdk.routes.agent_protocol import _artifact_response_config

    audio = Artifact(
        artifact_id="a_audio",
        artifact_type=ArtifactType.AUDIO,
        uri="s3://skyvern-artifacts/.../dictation.webm",
        organization_id="o_1",
        created_at="2026-04-23T00:00:00Z",
        modified_at="2026-04-23T00:00:00Z",
    )
    media_type, disposition = _artifact_response_config(audio)
    assert media_type == "audio/webm"
    assert disposition == "inline"


def test_content_endpoint_session_replay_falls_back_to_mp4_for_unknown_extension():
    from skyvern.forge.sdk.routes.agent_protocol import _artifact_response_config

    replay = Artifact(
        artifact_id="a_replay",
        artifact_type=ArtifactType.SESSION_REPLAY,
        uri="s3://skyvern-artifacts/.../session_replay",
        organization_id="o_1",
        created_at="2026-04-23T00:00:00Z",
        modified_at="2026-04-23T00:00:00Z",
    )
    media_type, disposition = _artifact_response_config(replay)
    assert media_type == "video/mp4"
    assert disposition == "inline"


def test_content_endpoint_download_non_ascii_filename_does_not_crash_header_encoding():
    """Starlette encodes response headers as Latin-1. Unicode filenames must use RFC 5987
    (filename*=UTF-8''...) with an ASCII fallback so the endpoint does not 500."""
    from starlette.responses import Response

    from skyvern.forge.sdk.routes.agent_protocol import _artifact_response_config

    artifact = _make_artifact("a_unicode", "s3://skyvern-uploads/downloads/local/o_1/wr_1/文档.pdf")
    media_type, disposition = _artifact_response_config(artifact)
    # Must not raise — Starlette's header encoding rejects non-Latin-1 bytes.
    Response(content=b"x", media_type=media_type, headers={"Content-Disposition": disposition})
    assert "filename*=UTF-8''" in disposition
    assert "%E6%96%87%E6%A1%A3.pdf" in disposition or "%e6%96%87%e6%a1%a3.pdf" in disposition


def test_sanitize_header_filename_strips_crlf_and_quotes_directly():
    """Direct unit test on _sanitize_header_filename so we know the function works
    even when urlparse isn't part of the chain (defense in depth)."""
    from skyvern.forge.sdk.routes.agent_protocol import _sanitize_header_filename

    assert _sanitize_header_filename('evil"pdf') == "evilpdf"
    assert _sanitize_header_filename("hello\r\nworld.pdf") == "helloworld.pdf"
    assert _sanitize_header_filename("back\\slash.pdf") == "backslash.pdf"
    assert _sanitize_header_filename("") == "download"


def test_content_endpoint_download_filename_preserves_question_and_hash():
    """S3 keys may legitimately contain '?' or '#'; urlparse would otherwise strip them."""
    from skyvern.forge.sdk.routes.agent_protocol import _artifact_response_config

    artifact = _make_artifact("a_q", "s3://skyvern-uploads/downloads/local/o_1/wr_1/report?v=2#a.pdf")
    _, disposition = _artifact_response_config(artifact)
    assert "report%3Fv%3D2%23a.pdf" in disposition or "report?v=2#a.pdf" in disposition


def test_sanitize_header_filename_strips_bidi_and_format_characters():
    """Unicode bidi overrides and format chars (ZWSP, RLO, ZWNBSP) enable filename
    spoofing in the browser's download UI (``invoice\\u202efdp.exe`` -> ``invoice.exe.pdf``)."""
    from skyvern.forge.sdk.routes.agent_protocol import _sanitize_header_filename

    assert "\u202e" not in _sanitize_header_filename("invoice\u202efdp.exe")
    assert "\u200b" not in _sanitize_header_filename("stealth\u200b.pdf")
    assert "\ufeff" not in _sanitize_header_filename("bom\ufeff.pdf")


def test_ascii_fallback_filename_preserves_stem_for_pure_unicode_names():
    """Pure non-ASCII names (e.g. CJK, emoji) must not reduce to a bare ``.pdf`` hidden
    dotfile after the NFKD strip; fall back to a ``download`` stem instead."""
    from skyvern.forge.sdk.routes.agent_protocol import _ascii_fallback_filename

    assert _ascii_fallback_filename("文档.pdf") == "download.pdf"
    assert _ascii_fallback_filename("🎉.pdf") == "download.pdf"
    # Accented Latin still transliterates to keep the stem.
    assert _ascii_fallback_filename("fïlè.pdf") == "file.pdf"


def test_sanitize_header_filename_strips_control_characters():
    """NUL/DEL/C1 control chars are valid Latin-1 bytes but violate RFC 7230 header syntax."""
    from skyvern.forge.sdk.routes.agent_protocol import _sanitize_header_filename

    assert "\x00" not in _sanitize_header_filename("evil\x00.pdf")
    assert "\x7f" not in _sanitize_header_filename("evil\x7f.pdf")
    assert "\x1b" not in _sanitize_header_filename("evil\x1b.pdf")
    assert "\x80" not in _sanitize_header_filename("evil\x80.pdf")


@pytest.mark.asyncio
async def test_content_endpoint_sets_nosniff_header_end_to_end():
    """Hit the real route through a FastAPI TestClient and verify the response
    actually carries X-Content-Type-Options: nosniff on the DOWNLOAD path.

    Defence-in-depth for SKY-8862: prevents a refactor from silently dropping
    the header without this suite noticing."""
    import json

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from skyvern.config import settings
    from skyvern.forge.sdk.artifact.signing import sign_artifact_url
    from skyvern.forge.sdk.routes.routers import base_router

    artifact = _make_artifact("a_e2e", "s3://skyvern-uploads/downloads/local/o_1/wr_1/report.pdf")
    keyring_json = json.dumps({"current_kid": "k1", "keys": {"k1": {"secret": "0" * 64, "created_at": "2026-04-23"}}})

    with (
        patch.object(settings, "ARTIFACT_CONTENT_HMAC_KEYRING", keyring_json),
        patch.object(settings, "SKYVERN_BASE_URL", "http://testserver"),
        patch("skyvern.forge.sdk.routes.agent_protocol.app") as app_module,
    ):
        app_module.DATABASE.artifacts.get_artifact_by_id_no_org = AsyncMock(return_value=artifact)
        app_module.ARTIFACT_MANAGER.retrieve_artifact = AsyncMock(return_value=b"%PDF-1.4 fake body")

        from skyvern.forge.sdk.artifact.signing import parse_keyring

        signed_url = sign_artifact_url(
            base_url="http://testserver",
            artifact_id=artifact.artifact_id,
            keyring=parse_keyring(keyring_json),
            artifact_name="report.pdf",
            artifact_type="download",
        )

        test_app = FastAPI()
        test_app.include_router(base_router, prefix="/v1")
        client = TestClient(test_app)
        resp = client.get(signed_url.replace("http://testserver", ""))

    assert resp.status_code == 200, resp.text
    assert resp.headers.get("X-Content-Type-Options") == "nosniff"
    assert resp.headers.get("Content-Disposition", "").startswith("attachment;")
    assert resp.content == b"%PDF-1.4 fake body"


def test_content_endpoint_download_filename_strips_header_injection():
    """URI-derived filenames go straight into a Content-Disposition header;
    CR/LF and raw quotes must be stripped to prevent header injection."""
    from skyvern.forge.sdk.routes.agent_protocol import _artifact_response_config

    artifact = _make_artifact(
        "a_bad",
        's3://skyvern-uploads/downloads/local/o_1/wr_1/evil"\r\nSet-Cookie: x=y.pdf',
    )
    _, disposition = _artifact_response_config(artifact)
    assert "\r" not in disposition
    assert "\n" not in disposition
    assert disposition.count('"') == 2  # only the pair around filename


def _run_with_created_at(created_at):
    run = MagicMock()
    run.created_at = created_at
    return run


@pytest.mark.asyncio
async def test_get_downloaded_files_skips_listing_for_empty_post_cutover_run(keyring_configured):
    """A post-cutover run with zero DOWNLOAD rows returns [] without the legacy S3 LIST:
    every download registers a row at save time, so the LIST could only confirm emptiness."""
    from datetime import datetime

    from skyvern.config import settings

    storage = S3Storage()
    storage.async_client = MagicMock()
    storage.async_client.list_files = AsyncMock()  # must NOT be called

    mock_list = AsyncMock(return_value=[])
    mock_get_run = AsyncMock(return_value=_run_with_created_at(datetime(2026, 8, 2, 12, 0, 0)))

    with (
        patch.object(settings, "DOWNLOADS_EMPTY_S3_LISTING_CUTOVER", "2026-08-01T00:00:00"),
        patch("skyvern.forge.sdk.artifact.storage.s3.app") as s3_app,
    ):
        s3_app.DATABASE.artifacts.list_artifacts_for_run_by_type = mock_list
        s3_app.DATABASE.tasks.get_run = mock_get_run
        result = await storage.get_downloaded_files(organization_id="o_1", run_id="wr_new")

    assert result == []
    storage.async_client.list_files.assert_not_awaited()
    mock_get_run.assert_awaited_once_with(run_id="wr_new", organization_id="o_1")


@pytest.mark.asyncio
async def test_get_downloaded_files_lists_for_empty_pre_cutover_run(keyring_configured):
    """A run created before the cutover keeps the legacy S3 LIST — its downloads may
    predate row registration."""
    from datetime import datetime

    from skyvern.config import settings

    storage = S3Storage()
    storage.async_client = MagicMock()
    storage.async_client.list_files = AsyncMock(return_value=[])

    mock_list = AsyncMock(return_value=[])
    mock_get_run = AsyncMock(return_value=_run_with_created_at(datetime(2026, 7, 1, 0, 0, 0)))

    with (
        patch.object(settings, "DOWNLOADS_EMPTY_S3_LISTING_CUTOVER", "2026-08-01T00:00:00"),
        patch("skyvern.forge.sdk.artifact.storage.s3.app") as s3_app,
    ):
        s3_app.DATABASE.artifacts.list_artifacts_for_run_by_type = mock_list
        s3_app.DATABASE.tasks.get_run = mock_get_run
        result = await storage.get_downloaded_files(organization_id="o_1", run_id="wr_old")

    assert result == []
    storage.async_client.list_files.assert_awaited_once()


@pytest.mark.parametrize("get_run_behavior", ["raises", "returns_none"])
@pytest.mark.asyncio
async def test_get_downloaded_files_lists_when_run_unresolvable(keyring_configured, get_run_behavior):
    """DB errors or a missing run row fail open to the legacy S3 LIST."""
    from skyvern.config import settings

    storage = S3Storage()
    storage.async_client = MagicMock()
    storage.async_client.list_files = AsyncMock(return_value=[])

    mock_list = AsyncMock(return_value=[])
    if get_run_behavior == "raises":
        mock_get_run = AsyncMock(side_effect=RuntimeError("db down"))
    else:
        mock_get_run = AsyncMock(return_value=None)

    with (
        patch.object(settings, "DOWNLOADS_EMPTY_S3_LISTING_CUTOVER", "2026-08-01T00:00:00"),
        patch("skyvern.forge.sdk.artifact.storage.s3.app") as s3_app,
    ):
        s3_app.DATABASE.artifacts.list_artifacts_for_run_by_type = mock_list
        s3_app.DATABASE.tasks.get_run = mock_get_run
        result = await storage.get_downloaded_files(organization_id="o_1", run_id="wr_x")

    assert result == []
    storage.async_client.list_files.assert_awaited_once()


@pytest.mark.asyncio
async def test_create_download_artifact_refreshes_checksum_for_changed_bytes():
    """A re-save of changed bytes under the same uri moves the row's checksum, so the loop
    filter's (filename, checksum, url) signature moves with the content (SKY-13782)."""
    manager = ArtifactManager()

    existing = Artifact(
        artifact_id="a_existing",
        artifact_type=ArtifactType.DOWNLOAD,
        uri="s3://skyvern-uploads/downloads/local/o_1/wr_1/file.pdf",
        organization_id="o_1",
        run_id="wr_1",
        workflow_run_id="wr_1",
        checksum="stale",
        created_at="2026-04-23T00:00:00Z",
        modified_at="2026-04-23T00:00:00Z",
    )
    find_existing = AsyncMock(return_value=existing)
    mock_db_create = AsyncMock()
    mock_refresh = AsyncMock()

    with (
        patch(
            "skyvern.forge.sdk.artifact.manager.app.DATABASE.artifacts.find_download_artifact",
            find_existing,
        ),
        patch(
            "skyvern.forge.sdk.artifact.manager.app.DATABASE.artifacts.create_artifact",
            mock_db_create,
        ),
        patch(
            "skyvern.forge.sdk.artifact.manager.app.DATABASE.artifacts.refresh_download_artifact_content",
            mock_refresh,
        ),
    ):
        artifact_id = await manager.create_download_artifact(
            organization_id="o_1",
            run_id="wr_1",
            workflow_run_id="wr_1",
            uri=existing.uri,
            filename="file.pdf",
            checksum="fresh",
            file_size=10,
        )

    assert artifact_id == "a_existing"
    mock_db_create.assert_not_awaited()
    mock_refresh.assert_awaited_once()
    assert mock_refresh.await_args.kwargs["checksum"] == "fresh"
    assert mock_refresh.await_args.kwargs["file_size"] == 10


@pytest.mark.asyncio
async def test_create_download_artifact_does_not_touch_row_for_identical_bytes(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        "skyvern.forge.sdk.artifact.manager.app.DATABASE.workflow_run_attempts",
        FakeWorkflowRunAttemptsRepository([SimpleNamespace(attempt_number=2, started_at=None)]),
    )
    manager = ArtifactManager()

    existing = Artifact(
        artifact_id="a_existing",
        artifact_type=ArtifactType.DOWNLOAD,
        uri="s3://skyvern-uploads/downloads/local/o_1/wr_1/file.pdf",
        organization_id="o_1",
        run_id="wr_1",
        workflow_run_id="wr_1",
        checksum="same",
        created_at="2026-04-23T00:00:00Z",
        modified_at="2026-04-23T00:00:00Z",
    )
    find_existing = AsyncMock(return_value=existing)
    mock_refresh = AsyncMock()

    with (
        patch(
            "skyvern.forge.sdk.artifact.manager.app.DATABASE.artifacts.find_download_artifact",
            find_existing,
        ),
        patch(
            "skyvern.forge.sdk.artifact.manager.app.DATABASE.artifacts.refresh_download_artifact_content",
            mock_refresh,
        ),
    ):
        artifact_id = await manager.create_download_artifact(
            organization_id="o_1",
            run_id="wr_1",
            workflow_run_id="wr_1",
            uri=existing.uri,
            filename="file.pdf",
            checksum="same",
        )

    assert artifact_id == "a_existing"
    mock_refresh.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_download_artifact_propagates_refresh_failure():
    """A stale row must not vouch for bytes it does not describe: a failed refresh raises so the
    save loop records the file as skipped instead of the binder trusting the stale registration."""
    manager = ArtifactManager()

    existing = Artifact(
        artifact_id="a_existing",
        artifact_type=ArtifactType.DOWNLOAD,
        uri="s3://skyvern-uploads/downloads/local/o_1/wr_1/file.pdf",
        organization_id="o_1",
        run_id="wr_1",
        workflow_run_id="wr_1",
        checksum="stale",
        created_at="2026-04-23T00:00:00Z",
        modified_at="2026-04-23T00:00:00Z",
    )
    find_existing = AsyncMock(return_value=existing)
    mock_refresh = AsyncMock(side_effect=RuntimeError("db down"))

    with (
        patch(
            "skyvern.forge.sdk.artifact.manager.app.DATABASE.artifacts.find_download_artifact",
            find_existing,
        ),
        patch(
            "skyvern.forge.sdk.artifact.manager.app.DATABASE.artifacts.refresh_download_artifact_content",
            mock_refresh,
        ),
    ):
        with pytest.raises(RuntimeError, match="db down"):
            await manager.create_download_artifact(
                organization_id="o_1",
                run_id="wr_1",
                workflow_run_id="wr_1",
                uri=existing.uri,
                filename="file.pdf",
                checksum="fresh",
                file_size=10,
            )


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["s3", "azure", "gcs", "local"])
@pytest.mark.parametrize("child_save", [False, True])
@pytest.mark.parametrize("retry_bytes", [b"attempt one", b"different attempt two bytes"])
@pytest.mark.parametrize("signed_artifact_urls", [True, False])
async def test_retry_downloads_preserve_attempt_objects_and_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    backend: str,
    child_save: bool,
    retry_bytes: bytes,
    signed_artifact_urls: bool,
):
    monkeypatch.setattr(
        settings, "ARTIFACT_CONTENT_HMAC_KEYRING", _DUMMY_KEYRING_JSON if signed_artifact_urls else None
    )
    storage = {
        "s3": lambda: S3Storage(),
        "azure": lambda: AzureStorageForTests("uploads"),
        "gcs": lambda: GcsStorageForTests("uploads"),
        "local": lambda: LocalStorage(str(tmp_path / "artifacts")),
    }[backend]()
    storage.async_client = MagicMock()
    if backend in ("s3", "gcs"):
        monkeypatch.setattr(storage, "_get_storage_class_for_org", AsyncMock(return_value="STANDARD"))
    if backend == "gcs":
        monkeypatch.setattr(storage, "_get_tags_for_org", AsyncMock(return_value=None))
    download_dir = tmp_path / "downloads"
    download_dir.mkdir()
    file_path = download_dir / "report.pdf"
    original_bytes = b"attempt one"
    file_path.write_bytes(original_bytes)
    first_start = datetime.now(UTC) - timedelta(minutes=2)
    second_start = first_start + timedelta(minutes=1)
    write_time = first_start + timedelta(seconds=1)
    attempts = FakeWorkflowRunAttemptsRepository([SimpleNamespace(attempt_number=1, started_at=first_start)])
    rows: dict[str, Artifact] = {}
    objects: dict[str, bytes] = {}
    object_info: dict[str, dict] = {}

    async def upload(*, uri: str, file_path: str, metadata: dict[str, str], **kwargs):
        objects[uri] = Path(file_path).read_bytes()
        object_info[uri] = {"Metadata": metadata, "ContentLength": len(objects[uri]), "LastModified": write_time}

    async def list_files(*, uri: str):
        return [urlparse(key).path.lstrip("/") for key in objects if key.startswith(uri + "/")]

    async def get_object_info(uri: str):
        return object_info[uri]

    async def sign_urls(uris: list[str]):
        return [f"https://storage.example{urlparse(uri).path}?signature=test" for uri in uris]

    async def find(*, uri: str, **kwargs):
        return next((row for row in rows.values() if row.uri == uri), None)

    async def create(**kwargs):
        row = Artifact(**kwargs, created_at=write_time, modified_at=write_time)
        rows[row.artifact_id] = row
        return row

    async def refresh(*, artifact_id: str, checksum: str | None, file_size: int | None, **kwargs):
        row = rows[artifact_id]
        row.checksum, row.file_size, row.modified_at = checksum, file_size, write_time

    async def list_rows(**kwargs):
        return list(rows.values())

    current_context = SkyvernContext(workflow_run_id="wr_child_1" if child_save else "wr_1")

    async def get_attempts(workflow_run_id):
        if workflow_run_id != "wr_1":
            return [SimpleNamespace(attempt_number=1, started_at=write_time)]
        return attempts.attempts

    manager = ArtifactManager()
    fake_app = SimpleNamespace(
        ARTIFACT_MANAGER=manager,
        WORKFLOW_CONTEXT_MANAGER=WorkflowContextManager(),
        DATABASE=SimpleNamespace(
            workflow_run_attempts=SimpleNamespace(get_attempts=get_attempts),
            workflow_runs=SimpleNamespace(get_workflow_run=AsyncMock(return_value=None)),
            artifacts=SimpleNamespace(
                find_download_artifact=find,
                create_artifact=create,
                refresh_download_artifact_content=refresh,
                list_artifacts_for_run_by_type=list_rows,
            ),
        ),
    )
    backend_module = {"s3": s3_module, "azure": azure_module, "gcs": gcs_module, "local": local_module}[backend]
    for module in (manager_module, base_module, backend_module):
        monkeypatch.setattr(module, "app", fake_app)
    monkeypatch.setattr(backend_module, "get_download_dir", lambda **kwargs: str(download_dir))
    monkeypatch.setattr(manager_module.skyvern_context, "current", lambda: current_context)
    monkeypatch.setattr(storage.async_client, "upload_file_from_path", upload)
    monkeypatch.setattr(storage.async_client, "list_files", list_files)
    monkeypatch.setattr(storage.async_client, "get_object_info", get_object_info)
    signing_method = {
        "s3": "create_presigned_urls",
        "azure": "create_sas_urls",
        "gcs": "create_signed_urls",
        "local": "unused",
    }[backend]
    monkeypatch.setattr(storage.async_client, signing_method, sign_urls)
    monkeypatch.setattr(
        manager,
        "resolve_share_url",
        AsyncMock(
            side_effect=lambda artifact, **kwargs: f"https://api.example/v1/artifacts/{artifact.artifact_id}/content"
        ),
    )
    monkeypatch.setattr(manager, "resolve_artifact_url_expiry_seconds", AsyncMock(return_value=3600))

    await storage.save_downloaded_files(organization_id="o_1", run_id="wr_1")
    first = next(iter(rows.values())).model_copy(deep=True)
    assert first.uri.endswith("/o_1/wr_1/report.pdf")
    await storage.save_downloaded_files(organization_id="o_1", run_id="wr_1")
    assert list(rows) == [first.artifact_id]
    first_files = await storage.get_downloaded_files(organization_id="o_1", run_id="wr_1")
    first_signature = to_downloaded_file_signature(first_files[0])

    current_context.workflow_run_id = "wr_child_2" if child_save else "wr_1"
    attempts.attempts.append(SimpleNamespace(attempt_number=2, started_at=second_start))
    write_time = second_start + timedelta(seconds=1)
    file_path.write_bytes(retry_bytes)
    os.utime(file_path, (write_time.timestamp(), write_time.timestamp()))
    await storage.save_downloaded_files(organization_id="o_1", run_id="wr_1")

    current_context.workflow_run_id = "wr_1"
    await storage.save_downloaded_files(organization_id="o_1", run_id="wr_1", attempt_number=2)
    if backend == "local":
        objects = {row.uri: await storage.retrieve_artifact(row) for row in rows.values()}
    assert len(objects) == 2
    assert len(rows) == 2
    second = next(row for row in rows.values() if row.artifact_id != first.artifact_id)
    assert second.uri == first.uri.removesuffix("report.pdf") + "attempts/2/report.pdf"
    assert objects[first.uri] == original_bytes
    assert rows[first.artifact_id] == first
    assert first.checksum == hashlib.sha256(original_bytes).hexdigest()
    assert objects[second.uri] == retry_bytes
    assert second.checksum == hashlib.sha256(retry_bytes).hexdigest()
    assert second.file_size == len(retry_bytes)

    service = WorkflowService()
    all_files = await storage.get_downloaded_files(organization_id="o_1", run_id="wr_1")
    current_files = service._filter_downloaded_files_to_attempt(
        all_files, attempt_rows=attempts.attempts, attempt_number=2, artifact_ids={second.artifact_id}
    )
    historical_files = service._filter_downloaded_files_to_attempt(
        all_files, attempt_rows=attempts.attempts, attempt_number=1, artifact_ids={first.artifact_id}
    )
    assert [file.artifact_id for file in current_files] == [second.artifact_id]
    assert [file.artifact_id for file in historical_files] == [first.artifact_id]
    assert to_downloaded_file_signature(historical_files[0]) == first_signature
    if backend == "local":
        assert first_files[0].url == first.uri
        assert Path(urlparse(first_files[0].url).path).read_bytes() == original_bytes
        assert first_files[0].file_size == first.file_size
        assert first_files[0].modified_at == first.modified_at
        assert historical_files[0].url == first.uri
        assert historical_files[0].checksum == first.checksum
        assert current_files[0].url == second.uri
        assert current_files[0].checksum == second.checksum
    assert (
        filter_downloaded_files_for_current_iteration(
            all_files,
            {DOWNLOADED_FILE_SIGS_KEY: [first_signature]},
            aliases=storage.get_downloaded_file_signature_aliases,
        )
        == current_files
    )
    second_signature = to_downloaded_file_signature(current_files[0])
    assert second_signature != first_signature
    assert grade_completion_contract(
        (CompletionCriterion("download", "registered_download"),), registered_download_count=len(current_files)
    ).satisfied
    assert not grade_completion_contract(
        (CompletionCriterion("downloads", "registered_download", min_count=2),),
        registered_download_count=len(current_files),
    ).satisfied

    second_snapshot = second.model_copy(deep=True)
    await storage.save_downloaded_files(organization_id="o_1", run_id="wr_1")
    assert len(rows) == 2
    assert rows[second.artifact_id] == second_snapshot
    assert (
        await manager.create_download_artifact(
            organization_id="o_1",
            run_id="wr_1",
            workflow_run_id="wr_1",
            uri=second.uri,
            filename="report.pdf",
            checksum=second.checksum,
            file_size=second.file_size,
        )
        == second.artifact_id
    )
    repeated = await storage.get_downloaded_files(organization_id="o_1", run_id="wr_1")
    assert (
        to_downloaded_file_signature(next(file for file in repeated if file.artifact_id == second.artifact_id))
        == second_signature
    )

    if backend == "local":
        fake_app.WORKFLOW_CONTEXT_MANAGER.workflow_run_contexts["wr_1"] = SimpleNamespace(attempt_number=1)
        historical_read = await storage.get_downloaded_files(organization_id="o_1", run_id="wr_1")
        assert {file.url for file in historical_read} == {first.uri, second.uri}
        file_path.unlink()
        assert await storage.get_downloaded_files(organization_id="o_1", run_id="wr_1") == historical_read

        file_path.write_bytes(b"unsaved replacement")
        unsaved_path = download_dir / "unsaved.pdf"
        unsaved_path.write_bytes(b"unsaved download")
        mixed_files = await storage.get_downloaded_files(organization_id="o_1", run_id="wr_1")
        assert {file.url for file in mixed_files} == {
            first.uri,
            second.uri,
            f"file://{file_path}",
            f"file://{unsaved_path}",
        }
        for live_path in (file_path, unsaved_path):
            live_file = next(file for file in mixed_files if file.url == f"file://{live_path}")
            assert live_file.artifact_id is None
            assert live_file.checksum == hashlib.sha256(live_path.read_bytes()).hexdigest()
            assert live_file.file_size == live_path.stat().st_size
            assert live_file.modified_at == datetime.fromtimestamp(live_path.stat().st_mtime, tz=UTC)

        monkeypatch.setattr(
            fake_app.DATABASE.artifacts,
            "list_artifacts_for_run_by_type",
            AsyncMock(side_effect=RuntimeError("artifact repository unavailable")),
        )
        unattributed_files = await storage.get_downloaded_files(organization_id="o_1", run_id="wr_1")
        assert {file.url for file in unattributed_files} == {f"file://{file_path}", f"file://{unsaved_path}"}
        assert all(file.artifact_id is None for file in unattributed_files)


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["s3", "azure", "gcs", "local"])
@pytest.mark.parametrize("failure", ["lookup_error", "unstarted_attempt"])
async def test_save_downloaded_files_fails_open_when_the_attempt_cannot_be_resolved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backend: str, failure: str
):
    """A database blip while resolving the attempt, or an attempt row with no start time, must
    save every downloaded file rather than none; the read path already fails open the same way."""
    storage = {
        "s3": lambda: S3Storage(),
        "azure": lambda: AzureStorageForTests("uploads"),
        "gcs": lambda: GcsStorageForTests("uploads"),
        "local": lambda: LocalStorage(str(tmp_path / "artifacts")),
    }[backend]()
    storage.async_client = MagicMock()
    if backend in ("s3", "gcs"):
        monkeypatch.setattr(storage, "_get_storage_class_for_org", AsyncMock(return_value="STANDARD"))
    if backend == "gcs":
        monkeypatch.setattr(storage, "_get_tags_for_org", AsyncMock(return_value=None))
    download_dir = tmp_path / "downloads"
    download_dir.mkdir()
    (download_dir / "report.pdf").write_bytes(b"attempt two bytes")
    now = datetime.now(UTC)
    rows: dict[str, Artifact] = {}
    objects: dict[str, bytes] = {}

    async def upload(*, uri: str, file_path: str, metadata: dict[str, str], **kwargs):
        objects[uri] = Path(file_path).read_bytes()

    async def find(*, uri: str, **kwargs):
        return next((row for row in rows.values() if row.uri == uri), None)

    async def create(**kwargs):
        row = Artifact(**kwargs, created_at=now, modified_at=now)
        rows[row.artifact_id] = row
        return row

    async def list_rows(**kwargs):
        return list(rows.values())

    get_workflow_run = (
        AsyncMock(side_effect=RuntimeError("database unavailable"))
        if failure == "lookup_error"
        else AsyncMock(return_value=SimpleNamespace(parent_workflow_run_id=None))
    )
    attempts = [
        SimpleNamespace(attempt_number=1, started_at=now - timedelta(minutes=5)),
        SimpleNamespace(attempt_number=2, started_at=None),
    ]
    fake_app = SimpleNamespace(
        ARTIFACT_MANAGER=ArtifactManager(),
        WORKFLOW_CONTEXT_MANAGER=WorkflowContextManager(),
        DATABASE=SimpleNamespace(
            workflow_run_attempts=SimpleNamespace(get_attempts=AsyncMock(return_value=attempts)),
            workflow_runs=SimpleNamespace(get_workflow_run=get_workflow_run),
            artifacts=SimpleNamespace(
                find_download_artifact=find,
                create_artifact=create,
                refresh_download_artifact_content=AsyncMock(),
                list_artifacts_for_run_by_type=list_rows,
            ),
        ),
    )
    backend_module = {"s3": s3_module, "azure": azure_module, "gcs": gcs_module, "local": local_module}[backend]
    for module in (manager_module, base_module, backend_module):
        monkeypatch.setattr(module, "app", fake_app)
    monkeypatch.setattr(backend_module, "get_download_dir", lambda **kwargs: str(download_dir))
    monkeypatch.setattr(manager_module.skyvern_context, "current", lambda: None)
    monkeypatch.setattr(base_module.skyvern_context, "current", lambda: None)
    monkeypatch.setattr(storage.async_client, "upload_file_from_path", upload)
    monkeypatch.setattr(storage.async_client, "list_files", AsyncMock(return_value=[]))

    await storage.save_downloaded_files(organization_id="o_1", run_id="wr_1", attempt_number=2)

    saved_uris = [row.uri for row in rows.values()]
    assert len(saved_uris) == 1
    assert saved_uris[0].endswith("/o_1/wr_1/attempts/2/report.pdf")
    if backend != "local":
        assert list(objects) == saved_uris


@pytest.mark.asyncio
@pytest.mark.parametrize("run_id", ["wr_root", "wr_nested", "tsk_v2_root", "tsk_v2_child"])
@pytest.mark.parametrize("with_context", [True, False])
async def test_download_attempt_resolves_parent_owner(monkeypatch, run_id, with_context):
    started_at = datetime.now(UTC)
    parents = {"wr_root": None, "wr_nested": "wr_root"}

    async def get_workflow_run(workflow_run_id, *, organization_id):
        assert organization_id == "o_1"
        return SimpleNamespace(parent_workflow_run_id=parents[workflow_run_id])

    get_attempts = AsyncMock(return_value=[SimpleNamespace(attempt_number=2, started_at=started_at)])
    fake_app = SimpleNamespace(
        DATABASE=SimpleNamespace(
            workflow_runs=SimpleNamespace(get_workflow_run=get_workflow_run),
            workflow_run_attempts=SimpleNamespace(get_attempts=get_attempts),
            tasks=SimpleNamespace(
                get_run=AsyncMock(
                    return_value=SimpleNamespace(
                        parent_workflow_run_id="wr_nested" if run_id == "tsk_v2_child" else None
                    )
                )
            ),
            observer=SimpleNamespace(get_task_v2=AsyncMock(return_value=SimpleNamespace(workflow_run_id="wr_root"))),
        ),
        WORKFLOW_CONTEXT_MANAGER=WorkflowContextManager(),
    )
    monkeypatch.setattr(base_module, "app", fake_app)
    context = SkyvernContext(workflow_run_id="wr_nested") if with_context else None
    monkeypatch.setattr(base_module.skyvern_context, "current", lambda: context)
    owner, number, start = await base_module.resolve_download_attempt("o_1", run_id)
    assert (owner, number, start) == ("wr_root", 2, started_at)
    get_attempts.assert_awaited_once_with("wr_root")
    if with_context or run_id in {"wr_nested", "tsk_v2_child"}:
        assert await base_module.resolve_download_attempt("o_1", run_id, attempt_number=1) == ("wr_root", 2, started_at)
    else:
        assert await base_module.resolve_download_attempt("o_1", run_id, attempt_number=1) == ("wr_root", 1, None)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "attempt_number, stale_retry_file, metadata_timeout",
    [
        (1, False, False),
        (2, False, False),
        (2, True, False),
        pytest.param(1, False, True, id="no-policy-metadata-timeout"),
    ],
)
async def test_print_page_excludes_live_http_download_after_snapshot_registration(
    tmp_path, monkeypatch, attempt_number, stale_retry_file, metadata_timeout
):
    storage = LocalStorage(str(tmp_path / "artifacts"))
    download_dir = tmp_path / "downloads"
    download_dir.mkdir()
    http_file = download_dir / ("printed.pdf" if stale_retry_file else "http-report.pdf")
    http_file.write_bytes(b"printed PDF" if stale_retry_file else b"HTTP response")
    context = SkyvernContext(workflow_run_id="wr_1", run_id="wr_1")
    rows: list[Artifact] = []

    async def register(**kwargs):
        rows.append(
            _make_artifact(
                f"a_{len(rows)}",
                kwargs["uri"],
                checksum=kwargs["checksum"],
                file_size=kwargs["file_size"],
                created_at=datetime.now(UTC).isoformat(),
            )
        )

    fake_app = SimpleNamespace(
        STORAGE=storage,
        ARTIFACT_MANAGER=SimpleNamespace(create_download_artifact=register),
        DATABASE=SimpleNamespace(
            artifacts=SimpleNamespace(list_artifacts_for_run_by_type=AsyncMock(side_effect=lambda **kwargs: list(rows)))
        ),
    )
    for module in (local_module, block_module):
        monkeypatch.setattr(module, "app", fake_app)
        monkeypatch.setattr(module, "get_download_dir", lambda *args, **kwargs: str(download_dir))
    started_at = datetime.now(UTC) - timedelta(minutes=1)
    resolve_attempt = AsyncMock(return_value=("wr_1", 1, started_at - timedelta(minutes=1)))
    monkeypatch.setattr(base_module, "resolve_download_attempt", resolve_attempt)
    if stale_retry_file:
        list_artifacts = fake_app.DATABASE.artifacts.list_artifacts_for_run_by_type
        list_artifacts.side_effect = RuntimeError("repository unavailable")
        with pytest.raises(RuntimeError, match="repository unavailable"):
            await storage.save_downloaded_files(organization_id="o_1", run_id="wr_1")
        list_artifacts.side_effect = lambda **kwargs: list(rows)
        stale_timestamp = (started_at - timedelta(seconds=1)).timestamp()
        os.utime(http_file, (stale_timestamp, stale_timestamp))
        assert rows == []
        assert [file.url for file in await storage.get_downloaded_files("o_1", "wr_1")] == [f"file://{http_file}"]
    resolve_attempt.return_value = ("wr_1", attempt_number, None if metadata_timeout else started_at)
    if metadata_timeout:

        async def resolve_listing_attempt(*args, **kwargs):
            if rows:
                raise TimeoutError("attempt metadata unavailable")
            return "wr_1", 1, None

        monkeypatch.setattr(base_module, "resolve_download_attempt", resolve_listing_attempt)
    monkeypatch.setattr(block_module.skyvern_context, "current", lambda: context)
    monkeypatch.setattr(
        PrintPageBlock, "get_workflow_run_context", lambda *args: SimpleNamespace(organization_id="o_1")
    )
    monkeypatch.setattr(PrintPageBlock, "render_templatable_field", lambda self, field, value, ctx: value)
    monkeypatch.setattr(
        PrintPageBlock,
        "get_or_create_browser_state",
        AsyncMock(
            return_value=SimpleNamespace(
                get_working_page=AsyncMock(return_value=SimpleNamespace(pdf=AsyncMock(return_value=b"printed PDF")))
            )
        ),
    )
    monkeypatch.setattr(PrintPageBlock, "_upload_pdf_artifact", AsyncMock(return_value=(None, None)))
    monkeypatch.setattr(PrintPageBlock, "record_output_parameter_value", AsyncMock())
    monkeypatch.setattr(
        PrintPageBlock, "build_block_result", AsyncMock(side_effect=lambda **kwargs: SimpleNamespace(**kwargs))
    )
    block = PrintPageBlock(
        label="print",
        custom_filename="printed.pdf",
        output_parameter=OutputParameter(
            parameter_type=ParameterType.OUTPUT,
            key="print_output",
            output_parameter_id="op_1",
            workflow_id="wf_1",
            created_at=datetime.now(UTC),
            modified_at=datetime.now(UTC),
        ),
    )

    result = await block.execute(workflow_run_id="wr_1", workflow_run_block_id="wrb_1", organization_id="o_1")

    assert result.success is True
    assert [file["filename"] for file in result.output_parameter_value["downloaded_files"]] == ["printed.pdf"]
    assert context.loop_internal_state[DOWNLOADED_FILE_SIGS_KEY] == (
        []
        if stale_retry_file
        else [("http-report.pdf", hashlib.sha256(b"HTTP response").hexdigest(), f"file://{http_file}")]
    )
    printed = next(row for row in rows if row.uri.endswith("/printed.pdf"))
    assert result.output_parameter_value["downloaded_file_urls"] == [printed.uri]
    assert Path(urlparse(printed.uri).path).read_bytes() == b"printed PDF"
    if stale_retry_file:
        assert printed.uri.endswith("/attempts/2/printed.pdf")
        assert result.output_parameter_value["downloaded_files"][0]["url"] == printed.uri


def test_local_snapshot_aliases_consume_primary_before_live_baseline_and_preserve_duplicates(tmp_path, monkeypatch):
    storage = LocalStorage(str(tmp_path / "artifacts"))
    download_dir = tmp_path / "downloads"
    monkeypatch.setattr(local_module, "get_download_dir", lambda **kwargs: str(download_dir / kwargs["run_id"]))
    first = FileInfo(
        url=f"file://{storage.artifact_path}/{local_module.DOWNLOAD_FILE_PREFIX}/{settings.ENV}/o_1/wr_1/report.pdf",
        filename="report.pdf",
        checksum="same-bytes",
    )
    retry = first.model_copy(update={"url": first.url.removesuffix("report.pdf") + "attempts/2/report.pdf"})
    live = first.model_copy(update={"url": f"file://{download_dir}/wr_1/report.pdf"})
    baseline = {DOWNLOADED_FILE_SIGS_KEY: [to_downloaded_file_signature(first), to_downloaded_file_signature(live)]}

    assert filter_downloaded_files_for_current_iteration(
        [first, retry, first], baseline, aliases=storage.get_downloaded_file_signature_aliases
    ) == [first]
    assert filter_downloaded_files_for_current_iteration(
        [retry, retry],
        {DOWNLOADED_FILE_SIGS_KEY: [to_downloaded_file_signature(live)]},
        aliases=storage.get_downloaded_file_signature_aliases,
    ) == [retry]
