from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import parse_qs, urljoin, urlparse

import aiohttp
import pytest
from multidict import CIMultiDict, CIMultiDictProxy

from skyvern.config import settings
from skyvern.exceptions import DownloadFileMaxSizeExceeded, GoogleDriveFileNotAccessible
from skyvern.forge import app as forge_app
from skyvern.forge.sdk.api import files
from skyvern.forge.sdk.artifact.manager import ArtifactManager
from skyvern.forge.sdk.artifact.models import Artifact, ArtifactType
from skyvern.forge.sdk.artifact.signing import (
    parse_artifact_content_url,
    parse_keyring,
    sign_artifact_url,
    verify_artifact_signature,
)


class _FakeDownloadResponse:
    def __init__(
        self,
        data: bytes,
        headers: dict[str, str] | None = None,
        advertise_length: bool = True,
        status: int = 200,
    ) -> None:
        self._data = data
        # aiohttp exposes headers as a case-insensitive CIMultiDictProxy; mirror that.
        self.headers = CIMultiDictProxy(CIMultiDict(headers or {}))
        self.status = status
        self.reason = "Unknown Error" if status >= 400 else "OK"
        self.history = ()
        self.request_info = MagicMock(real_url="https://example.com/files/rate-limited.png")
        self.content_length = len(data) if advertise_length else None
        self.content = self
        self.body_read = False
        self.auto_raise_for_status = False

    async def iter_chunked(self, chunk_size: int) -> AsyncIterator[bytes]:
        self.body_read = True
        yield self._data

    async def __aenter__(self) -> _FakeDownloadResponse:
        if self.auto_raise_for_status:
            self.raise_for_status()
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    def raise_for_status(self) -> None:
        if self.status < 400:
            return
        raise aiohttp.ClientResponseError(
            request_info=self.request_info,
            history=self.history,
            status=self.status,
            message=self.reason,
            headers=self.headers,
        )


class _FakeDownloadSession:
    def __init__(self, response: _FakeDownloadResponse, *, raise_for_status: bool = False) -> None:
        self._response = response
        self._raise_for_status = raise_for_status

    def get(
        self, url: object, headers: dict[str, str] | None = None, allow_redirects: bool = True
    ) -> _FakeDownloadResponse:
        self._response.auto_raise_for_status = self._raise_for_status
        return self._response

    async def __aenter__(self) -> _FakeDownloadSession:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


def _patch_download_session(
    monkeypatch: pytest.MonkeyPatch,
    data: bytes,
    headers: dict[str, str] | None = None,
    advertise_length: bool = True,
    status: int = 200,
    captured_session_kwargs: dict[str, object] | None = None,
) -> _FakeDownloadResponse:
    response = _FakeDownloadResponse(data, headers, advertise_length=advertise_length, status=status)

    def make_session(**kwargs: object) -> _FakeDownloadSession:
        if captured_session_kwargs is not None:
            captured_session_kwargs.update(kwargs)
        return _FakeDownloadSession(response, raise_for_status=kwargs.get("raise_for_status") is True)

    monkeypatch.setattr(files.aiohttp, "ClientSession", make_session)
    return response


def _run_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, run_id: str, name: str = "data.txt") -> Path:
    download_root = tmp_path / "downloads"
    monkeypatch.setattr(settings, "DOWNLOAD_PATH", str(download_root))
    run_dir = download_root / run_id
    run_dir.mkdir(parents=True)
    path = run_dir / name
    path.write_text("hello")
    return path


@pytest.mark.asyncio
async def test_resolve_local_file_inside_run_download_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run_id = "wr_local"
    path = _run_file(tmp_path, monkeypatch, run_id)

    assert await files.resolve_local_or_download_file(str(path), run_id) == str(path.resolve())


@pytest.mark.asyncio
async def test_resolve_local_file_rejects_outside_run_download_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "DOWNLOAD_PATH", str(tmp_path / "downloads"))
    outside = tmp_path / "outside.txt"
    outside.write_text("nope")

    with pytest.raises(PermissionError):
        await files.resolve_local_or_download_file(str(outside), "wr_local")


@pytest.mark.asyncio
async def test_resolve_local_file_raises_for_missing_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run_id = "wr_missing"
    monkeypatch.setattr(settings, "DOWNLOAD_PATH", str(tmp_path / "downloads"))
    missing = tmp_path / "downloads" / run_id / "missing.txt"

    with pytest.raises(FileNotFoundError, match="Local file not found"):
        await files.resolve_local_or_download_file(str(missing), run_id)


@pytest.mark.asyncio
async def test_resolve_local_file_enforces_max_size(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run_id = "wr_size"
    path = _run_file(tmp_path, monkeypatch, run_id)
    path.write_bytes(b"x" * 2)

    with pytest.raises(DownloadFileMaxSizeExceeded):
        await files.resolve_local_or_download_file(str(path), run_id, max_size_mb=0)


@pytest.mark.asyncio
async def test_resolve_remote_url_downloads_file(monkeypatch: pytest.MonkeyPatch) -> None:
    download_mock = AsyncMock(return_value="/tmp/downloaded.pdf")
    monkeypatch.setattr(files, "download_file", download_mock)

    result = await files.resolve_local_or_download_file(
        "https://example.com/file.pdf",
        "wr_remote",
        organization_id="org-1",
        max_size_mb=10,
    )

    assert result == "/tmp/downloaded.pdf"
    download_mock.assert_awaited_once_with(
        "https://example.com/file.pdf",
        max_size_mb=10,
        organization_id="org-1",
    )


@pytest.mark.asyncio
async def test_download_file_preserves_url_filename(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_download_session(monkeypatch, b"resume-bytes")

    result = await files.download_file("https://example.com/files/Resume_Final.docx", output_dir=str(tmp_path))

    assert Path(result).name == "Resume_Final.docx"
    assert Path(result).parent == tmp_path.resolve()
    assert Path(result).read_bytes() == b"resume-bytes"


@pytest.mark.asyncio
async def test_download_file_uses_content_disposition_filename(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_download_session(
        monkeypatch, b"pdf-bytes", headers={"Content-Disposition": 'attachment; filename="candidate resume.pdf"'}
    )

    result = await files.download_file("https://example.com/f/abc123", output_dir=str(tmp_path))

    assert Path(result).name == "candidate resume.pdf"
    assert Path(result).read_bytes() == b"pdf-bytes"


@pytest.mark.asyncio
async def test_download_file_uses_lowercase_wire_headers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_download_session(
        monkeypatch, b"pdf-bytes", headers={"content-disposition": 'attachment; filename="report.pdf"'}
    )

    result = await files.download_file("https://example.com/f/abc123", output_dir=str(tmp_path))

    assert Path(result).name == "report.pdf"


@pytest.mark.asyncio
async def test_download_file_derives_extension_from_content_type_with_params(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_download_session(monkeypatch, b"pdf-bytes", headers={"Content-Type": "application/pdf; charset=utf-8"})

    result = await files.download_file("https://example.com/f/abc123", output_dir=str(tmp_path))

    assert Path(result).name == "abc123.pdf"


@pytest.mark.asyncio
async def test_download_file_rejects_path_escaping_filename(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    response = _patch_download_session(monkeypatch, b"x")

    with pytest.raises(ValueError, match="Unsafe filename"):
        await files.download_file("https://example.com/f/abc123", output_dir=str(tmp_path), filename="..")

    assert list(tmp_path.iterdir()) == []
    assert not response.body_read


@pytest.mark.asyncio
async def test_download_file_cleans_up_temp_file_when_max_size_exceeded_mid_stream(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_download_session(monkeypatch, b"x" * (1024 * 1024 + 1), advertise_length=False)

    with pytest.raises(DownloadFileMaxSizeExceeded):
        await files.download_file("https://example.com/files/big.bin", output_dir=str(tmp_path), max_size_mb=1)

    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_download_file_raises_http_error_without_aiohttp_auto_raise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured_session_kwargs: dict[str, object] = {}
    response = _patch_download_session(
        monkeypatch,
        b"",
        status=429,
        captured_session_kwargs=captured_session_kwargs,
    )

    with pytest.raises(aiohttp.ClientResponseError) as exc_info:
        await files.download_file("https://example.com/files/rate-limited.png", output_dir=str(tmp_path))

    assert exc_info.value.status == 429
    assert captured_session_kwargs.get("raise_for_status") is not True
    assert not response.body_read
    assert list(tmp_path.iterdir()) == []


# ---------------------------------------------------------------------------
# Google Drive HTML interstitial handling (SKY-13641)
# ---------------------------------------------------------------------------


class _FakeSequencedDownloadSession:
    """Serves one prepared response per GET, recording each requested URL."""

    def __init__(self, responses: list[_FakeDownloadResponse]) -> None:
        self._responses = list(responses)
        self.requested_urls: list[str] = []

    def get(
        self, url: object, headers: dict[str, str] | None = None, allow_redirects: bool = True
    ) -> _FakeDownloadResponse:
        self.requested_urls.append(str(url))
        return self._responses.pop(0)

    async def __aenter__(self) -> _FakeSequencedDownloadSession:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


def _patch_sequenced_download_session(
    monkeypatch: pytest.MonkeyPatch, responses: list[_FakeDownloadResponse]
) -> _FakeSequencedDownloadSession:
    """Patch the download session and skip DNS pinning so tests stay hermetic."""
    session = _FakeSequencedDownloadSession(responses)
    monkeypatch.setattr(files.aiohttp, "ClientSession", lambda **kwargs: session)

    async def fake_validate_fetch(url: str, resolver: object) -> str:
        return url

    async def fake_validate_redirect(url: str, location: str, resolver: object) -> str:
        return urljoin(url, location)

    monkeypatch.setattr(files, "validate_and_pin_fetch_url", fake_validate_fetch)
    monkeypatch.setattr(files, "validate_and_pin_redirect_url", fake_validate_redirect)
    return session


_DRIVE_INTERSTITIAL_HTML = """<!DOCTYPE html><html><head><title>Download anyway</title></head><body>
<form id="download-form" action="https://drive.usercontent.google.com/download" method="get">
<input type="submit" value="Download anyway"/>
<input type="hidden" name="id" value="FILE123"/>
<input type="hidden" name="export" value="download"/>
<input type="hidden" name="confirm" value="t"/>
<input type="hidden" name="uuid" value="abc-uuid"/>
</form></body></html>"""

_DRIVE_SIGNIN_HTML = """<!DOCTYPE html><html><head><title>Sign in</title></head><body>
<form action="https://accounts.google.com/signin/challenge" method="post">
<input type="email" name="identifier"/>
</form></body></html>"""


@pytest.mark.asyncio
async def test_download_google_drive_interstitial_follows_confirm_form(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _patch_sequenced_download_session(
        monkeypatch,
        [
            _FakeDownloadResponse(
                _DRIVE_INTERSTITIAL_HTML.encode(), headers={"Content-Type": "text/html; charset=utf-8"}
            ),
            _FakeDownloadResponse(
                b"%PDF-1.5 real drive bytes",
                headers={"Content-Disposition": 'attachment; filename="report.pdf"'},
            ),
        ],
    )

    result = await files.download_file("https://drive.google.com/file/d/FILE123/view", output_dir=str(tmp_path))

    assert Path(result).read_bytes() == b"%PDF-1.5 real drive bytes"
    assert Path(result).name == "report.pdf"
    assert len(session.requested_urls) == 2
    followed = urlparse(session.requested_urls[1])
    assert followed.hostname == "drive.usercontent.google.com"
    assert followed.path == "/download"
    query = parse_qs(followed.query)
    assert query["id"] == ["FILE123"]
    assert query["confirm"] == ["t"]
    assert query["uuid"] == ["abc-uuid"]


@pytest.mark.asyncio
async def test_download_google_drive_permission_page_raises_clear_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_sequenced_download_session(
        monkeypatch,
        [_FakeDownloadResponse(_DRIVE_SIGNIN_HTML.encode(), headers={"Content-Type": "text/html; charset=utf-8"})],
    )

    with pytest.raises(GoogleDriveFileNotAccessible, match="not publicly accessible"):
        await files.download_file("https://drive.google.com/file/d/FILE123/view", output_dir=str(tmp_path))

    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_download_google_drive_html_after_confirm_raises_instead_of_saving(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_sequenced_download_session(
        monkeypatch,
        [
            _FakeDownloadResponse(
                _DRIVE_INTERSTITIAL_HTML.encode(), headers={"Content-Type": "text/html; charset=utf-8"}
            ),
            _FakeDownloadResponse(_DRIVE_SIGNIN_HTML.encode(), headers={"Content-Type": "text/html; charset=utf-8"}),
        ],
    )

    with pytest.raises(GoogleDriveFileNotAccessible):
        await files.download_file("https://drive.google.com/file/d/FILE123/view", output_dir=str(tmp_path))

    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_download_non_drive_html_is_still_saved(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_sequenced_download_session(
        monkeypatch,
        [_FakeDownloadResponse(b"<html>a real html file</html>", headers={"Content-Type": "text/html"})],
    )

    result = await files.download_file("https://example.com/files/page.html", output_dir=str(tmp_path))

    assert Path(result).read_bytes() == b"<html>a real html file</html>"


@pytest.mark.asyncio
async def test_download_google_drive_non_html_downloads_directly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _patch_sequenced_download_session(
        monkeypatch,
        [_FakeDownloadResponse(b"csv,data\n1,2", headers={"Content-Type": "text/csv"})],
    )

    result = await files.download_file("https://drive.google.com/file/d/FILE123/view", output_dir=str(tmp_path))

    assert Path(result).read_bytes() == b"csv,data\n1,2"
    assert len(session.requested_urls) == 1


# ---------------------------------------------------------------------------
# First-party artifact URL recovery (SKY-13575)
# ---------------------------------------------------------------------------

_BASE_URL = "https://api.example.com"
_KEYRING_JSON = json.dumps({"current_kid": "k1", "keys": {"k1": {"secret": "0" * 64}}})


def _artifact(artifact_id: str = "a_1", organization_id: str = "org-1") -> Artifact:
    now = datetime(2026, 8, 5, tzinfo=timezone.utc)
    return Artifact(
        artifact_id=artifact_id,
        artifact_type=ArtifactType.DOWNLOAD,
        uri="s3://bucket/downloads/docs_5.pdf",
        organization_id=organization_id,
        created_at=now,
        modified_at=now,
    )


def _patch_artifact_lookup(monkeypatch: pytest.MonkeyPatch, artifact: Artifact | None) -> AsyncMock:
    """Wire a real ArtifactManager against fake artifact/organization repositories."""
    monkeypatch.setattr(settings, "SKYVERN_BASE_URL", _BASE_URL)
    monkeypatch.setattr(settings, "ARTIFACT_CONTENT_HMAC_KEYRING", _KEYRING_JSON)
    get_artifact_by_id = AsyncMock(return_value=artifact)
    database = MagicMock()
    database.artifacts.get_artifact_by_id = get_artifact_by_id
    database.organizations.get_organization = AsyncMock(return_value=None)
    monkeypatch.setattr(forge_app, "DATABASE", database)
    monkeypatch.setattr(forge_app, "ARTIFACT_MANAGER", ArtifactManager())
    return get_artifact_by_id


def _corrupt_signature(url: str, drop_index: int = 13) -> str:
    """Drop a single character from the middle of the URL's signature."""
    head, sig = url.split("&sig=")
    return f"{head}&sig={sig[:drop_index]}{sig[drop_index + 1 :]}"


@pytest.mark.asyncio
async def test_resolve_remints_first_party_url_with_corrupted_signature(monkeypatch: pytest.MonkeyPatch) -> None:
    get_artifact_by_id = _patch_artifact_lookup(monkeypatch, _artifact())
    download_mock = AsyncMock(return_value="/tmp/docs_5.pdf")
    monkeypatch.setattr(files, "download_file", download_mock)
    signed = sign_artifact_url(_BASE_URL, "a_1", parse_keyring(_KEYRING_JSON))
    corrupted = _corrupt_signature(signed)

    await files.resolve_local_or_download_file(corrupted, "wr_1", organization_id="org-1")

    get_artifact_by_id.assert_awaited_once_with(artifact_id="a_1", organization_id="org-1")
    downloaded_url = download_mock.await_args.args[0]
    assert downloaded_url != corrupted
    parsed = parse_artifact_content_url(downloaded_url, _BASE_URL)
    assert parsed is not None
    assert verify_artifact_signature(
        "a_1", parsed.expiry or "", parsed.kid or "", parsed.sig or "", parse_keyring(_KEYRING_JSON)
    )


@pytest.mark.asyncio
async def test_resolve_remints_expired_first_party_url(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_artifact_lookup(monkeypatch, _artifact())
    download_mock = AsyncMock(return_value="/tmp/docs_5.pdf")
    monkeypatch.setattr(files, "download_file", download_mock)
    expired = sign_artifact_url(_BASE_URL, "a_1", parse_keyring(_KEYRING_JSON), expiry_seconds=-60)

    await files.resolve_local_or_download_file(expired, "wr_1", organization_id="org-1")

    assert download_mock.await_args.args[0] != expired


@pytest.mark.asyncio
async def test_resolve_leaves_valid_first_party_url_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    get_artifact_by_id = _patch_artifact_lookup(monkeypatch, _artifact())
    download_mock = AsyncMock(return_value="/tmp/docs_5.pdf")
    monkeypatch.setattr(files, "download_file", download_mock)
    signed = sign_artifact_url(_BASE_URL, "a_1", parse_keyring(_KEYRING_JSON))

    await files.resolve_local_or_download_file(signed, "wr_1", organization_id="org-1")

    assert download_mock.await_args.args[0] == signed
    get_artifact_by_id.assert_not_awaited()


@pytest.mark.asyncio
async def test_resolve_leaves_foreign_url_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    get_artifact_by_id = _patch_artifact_lookup(monkeypatch, _artifact())
    download_mock = AsyncMock(return_value="/tmp/file.pdf")
    monkeypatch.setattr(files, "download_file", download_mock)
    foreign = "https://evil.example.com/v1/artifacts/a_1/content?expiry=1&kid=k1&sig=short"

    await files.resolve_local_or_download_file(foreign, "wr_1", organization_id="org-1")

    assert download_mock.await_args.args[0] == foreign
    get_artifact_by_id.assert_not_awaited()


@pytest.mark.asyncio
async def test_resolve_does_not_remint_artifact_owned_by_another_organization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_artifact_lookup(monkeypatch, None)
    download_mock = AsyncMock(return_value="/tmp/docs_5.pdf")
    monkeypatch.setattr(files, "download_file", download_mock)
    corrupted = _corrupt_signature(sign_artifact_url(_BASE_URL, "a_1", parse_keyring(_KEYRING_JSON)))

    await files.resolve_local_or_download_file(corrupted, "wr_1", organization_id="org-1")

    assert download_mock.await_args.args[0] == corrupted
