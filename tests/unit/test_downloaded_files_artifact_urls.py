"""Tests for downloaded_files migration to short artifact URLs (SKY-8861)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import urlparse

import pytest

from skyvern.forge.sdk.artifact.manager import ArtifactManager
from skyvern.forge.sdk.artifact.models import Artifact, ArtifactType
from skyvern.forge.sdk.artifact.storage.s3 import S3Storage


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

    with (
        patch(
            "skyvern.forge.sdk.artifact.manager.app.DATABASE.artifacts.find_download_artifact",
            find_existing,
        ),
        patch(
            "skyvern.forge.sdk.artifact.manager.app.DATABASE.artifacts.create_artifact",
            mock_db_create,
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
        )

    assert artifact_id.startswith("a_")
    mock_db_create.assert_awaited_once()
    _, kwargs = mock_db_create.call_args
    assert kwargs["artifact_type"] == ArtifactType.DOWNLOAD
    assert kwargs["uri"] == "s3://skyvern-uploads/download/prod/o_1/wr_1/file.pdf"
    assert kwargs["organization_id"] == "o_1"
    assert kwargs["run_id"] == "wr_1"
    assert kwargs["workflow_run_id"] == "wr_1"
    mock_store.assert_not_awaited()


@pytest.mark.asyncio
async def test_save_downloaded_files_registers_artifact_per_file(tmp_path):
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
        await storage.save_downloaded_files(organization_id="o_1", run_id="wr_1")

    assert mock_create_download.await_count == 2
    uris = {call.kwargs["uri"] for call in mock_create_download.await_args_list}
    filenames = {call.kwargs["filename"] for call in mock_create_download.await_args_list}
    assert filenames == {"invoice.pdf", "report.csv"}
    assert all(u.startswith("s3://") and "/downloads/" in u and "/o_1/wr_1/" in u for u in uris)
    for call in mock_create_download.await_args_list:
        assert call.kwargs["organization_id"] == "o_1"
        assert call.kwargs["run_id"] == "wr_1"


def _make_artifact(
    artifact_id: str,
    uri: str,
    run_id: str = "wr_1",
    *,
    checksum: str | None = None,
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
    )
    mock_list = AsyncMock(return_value=[artifact])
    build_url = MagicMock(return_value="https://api.skyvern.com/v1/artifacts/a_42/content?expiry=x&kid=y&sig=z")

    with patch("skyvern.forge.sdk.artifact.storage.base.app") as base_app:
        with patch("skyvern.forge.sdk.artifact.storage.s3.app") as s3_app:
            s3_app.DATABASE.artifacts.list_artifacts_for_run_by_type = mock_list
            base_app.ARTIFACT_MANAGER.build_signed_content_url = build_url
            base_app.ARTIFACT_MANAGER.resolve_artifact_url_expiry_seconds = AsyncMock(return_value=12 * 60 * 60)
            result = await storage.get_downloaded_files(organization_id="o_1", run_id="wr_1")

    assert len(result) == 1
    assert result[0].url.startswith("https://api.skyvern.com/v1/artifacts/a_42/content")
    assert result[0].filename == "invoice.pdf"
    assert result[0].checksum == "sha-from-db"
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
    build_url = MagicMock(
        side_effect=lambda artifact_id, **_: f"https://api.skyvern.com/v1/artifacts/{artifact_id}/content"
    )

    with patch("skyvern.forge.sdk.artifact.storage.base.app") as base_app:
        with patch("skyvern.forge.sdk.artifact.storage.s3.app") as s3_app:
            s3_app.DATABASE.artifacts.list_artifacts_for_run_by_type = mock_list
            base_app.ARTIFACT_MANAGER.build_signed_content_url = build_url
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
    storage.async_client.get_file_metadata = AsyncMock(
        return_value={"sha256_checksum": "sha-old", "original_filename": "legacy.pdf"}
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
    storage.async_client.get_file_metadata = AsyncMock(
        return_value={"sha256_checksum": "sha-abc", "original_filename": "invoice.pdf"}
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
    storage.async_client.get_file_metadata = AsyncMock(
        return_value={"sha256_checksum": "sha-recover", "original_filename": "recoverable.pdf"}
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
