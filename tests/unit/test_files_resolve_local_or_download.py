from __future__ import annotations

import io
import json
import os
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import parse_qs, urljoin, urlparse

import aiohttp
import pytest
from multidict import CIMultiDict, CIMultiDictProxy

from skyvern.config import settings
from skyvern.exceptions import DownloadFileMaxSizeExceeded, GoogleDriveFileNotAccessible, HttpException
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
from skyvern.forge.sdk.artifact.storage.local import LocalStorage


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
async def test_download_file_never_mutates_an_already_staged_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A staged path can already be held by the page: a file chooser pins the file by identity at
    selection time, so staging a later file onto it breaks the pending submit (SKY-16614)."""
    _patch_download_session(monkeypatch, b"first-source-bytes")
    first = await files.download_file(
        "https://example.com/a/attachment.pdf", output_dir=str(tmp_path), preserve_existing_files=True
    )
    staged_identity = os.stat(first).st_ino

    _patch_download_session(monkeypatch, b"second-source-bytes")
    second = await files.download_file(
        "https://example.com/b/attachment.pdf", output_dir=str(tmp_path), preserve_existing_files=True
    )

    assert second != first
    assert os.stat(first).st_ino == staged_identity
    assert Path(first).read_bytes() == b"first-source-bytes"
    assert Path(second).read_bytes() == b"second-source-bytes"
    assert Path(second).name == "attachment (1).pdf"
    # No temp residue: the taskv3 download-signal wrapper lists this directory and reports every
    # name it did not stage itself, so a leftover partial would surface as a phantom download.
    assert sorted(p.name for p in tmp_path.iterdir()) == ["attachment (1).pdf", "attachment.pdf"]


@pytest.mark.asyncio
async def test_download_file_restages_an_identical_source_onto_one_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same source staged repeatedly in one run is the common case. It must keep ONE file under
    its real name: the run's download directory is a delivery surface that FileUploadBlock and
    SendEmailBlock ship wholesale, and the name reaches the target site's upload validator."""
    for _ in range(3):
        _patch_download_session(monkeypatch, b"resume-bytes")
        path = await files.download_file(
            "https://example.com/a/attachment.pdf", output_dir=str(tmp_path), preserve_existing_files=True
        )
        assert Path(path).name == "attachment.pdf"

    assert sorted(p.name for p in tmp_path.iterdir()) == ["attachment.pdf"]


@pytest.mark.asyncio
async def test_download_file_stages_a_name_too_long_to_extend(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A sibling name must stay under NAME_MAX. os.link raises ENAMETOOLONG rather than
    FileExistsError, which would escape the retry and fail a download the overwriting path completed."""
    long_name = "a" * 250 + ".pdf"
    _patch_download_session(monkeypatch, b"first")
    first = await files.download_file(
        "https://example.com/a/x", output_dir=str(tmp_path), filename=long_name, preserve_existing_files=True
    )

    _patch_download_session(monkeypatch, b"second")
    second = await files.download_file(
        "https://example.com/b/x", output_dir=str(tmp_path), filename=long_name, preserve_existing_files=True
    )

    assert second != first
    assert Path(second).name.endswith(".pdf")
    assert len(Path(second).name.encode()) <= 255
    assert Path(first).read_bytes() == b"first"
    assert Path(second).read_bytes() == b"second"


def test_sibling_name_keeps_the_extension_a_validator_reads() -> None:
    """SKY-11982 was a fleet-wide upload regression caused by attaching files whose name had lost
    its extension, so the counter must never land between the name and its suffix."""
    assert files._sibling_filename("report.pdf", 1) == "report (1).pdf"
    assert files._sibling_filename("report.tar.gz", 2) == "report.tar (2).gz"
    # splitext reads a leading dot as a dotfile, so ".pdf" is all stem and naive counting would
    # produce ".pdf (1)" — extensionless, the exact SKY-11982 shape.
    assert files._sibling_filename(".pdf", 1) == "(1).pdf"


def test_sibling_name_fits_name_max_whatever_crowds_it() -> None:
    """os.link raises ENAMETOOLONG rather than FileExistsError, so a sibling that does not fit
    escapes the retry and fails a download the overwriting path completed. A long extension
    crowds the budget exactly as a long stem does."""
    for name in ("x" * 250 + ".pdf", "a." + "x" * 251, "a." + "x" * 300, "é" * 200 + ".pdf"):
        assert len(files._sibling_filename(name, 1).encode()) <= 255, name


@pytest.mark.asyncio
async def test_download_file_stages_past_exhausted_sibling_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Running out of sibling names must not fail the download. The staging root can be long-lived,
    and refusing to stage is a worse answer than a longer path, so the file keeps its real name in
    a private directory instead."""
    monkeypatch.setattr(files, "_MAX_STAGING_SIBLINGS", 3)
    results = []
    for i in range(5):
        _patch_download_session(monkeypatch, f"body-{i}".encode())
        results.append(
            await files.download_file(
                f"https://example.com/{i}/attachment.pdf", output_dir=str(tmp_path), preserve_existing_files=True
            )
        )

    assert all(Path(r).name == "attachment.pdf" for r in results[3:])
    assert len(set(results)) == 5
    for i, r in enumerate(results):
        assert Path(r).read_bytes() == f"body-{i}".encode()


@pytest.mark.asyncio
async def test_managed_storage_download_stages_in_the_directory_it_was_given(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The shared temp root is written by every run in the process, so a path handed to a browser
    is staged in the run's own directory where no other run can name the same file."""
    monkeypatch.setattr(settings, "TEMP_PATH", str(tmp_path / "shared"))
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    storage = MagicMock()
    storage.assert_managed_file_access = MagicMock(return_value=None)
    storage.download_managed_file = AsyncMock(return_value=b"stored-bytes")
    monkeypatch.setattr(forge_app, "STORAGE", storage)

    path = await files.download_file(
        "s3://bucket/org-1/attachment.pdf",
        organization_id="org-1",
        preserve_existing_files=True,
        staging_dir=str(run_dir),
    )

    assert Path(path).parent == run_dir
    assert Path(path).read_bytes() == b"stored-bytes"


@pytest.mark.asyncio
async def test_download_file_still_overwrites_by_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Callers that did not opt in keep the overwriting behaviour. A FileDownloadBlock in a loop
    writes one file per run directory on purpose; accumulating siblings there would change what a
    workflow delivers."""
    _patch_download_session(monkeypatch, b"first")
    first = await files.download_file("https://example.com/a/attachment.pdf", output_dir=str(tmp_path))

    _patch_download_session(monkeypatch, b"second")
    second = await files.download_file("https://example.com/b/attachment.pdf", output_dir=str(tmp_path))

    assert second == first
    assert Path(first).read_bytes() == b"second"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["attachment.pdf"]


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


@pytest.mark.asyncio
async def test_download_authorizes_the_first_dispatch_url_not_only_redirect_hops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Drive rewrite moves the first request to Google before any redirect; the caller's policy
    has to see that URL, and a Drive link is recognised by host, not by substring."""
    session = _patch_sequenced_download_session(
        monkeypatch, [_FakeDownloadResponse(b"x", headers={"Content-Type": "text/csv"})]
    )

    def same_site_only(next_url: str) -> bool:
        return urlparse(next_url).hostname == "files.example.com"

    with pytest.raises(HttpException, match="blocked by policy"):
        await files.download_file(
            "https://drive.google.com/file/d/SECRET123/view",
            output_dir=str(tmp_path),
            authorize_redirect=same_site_only,
        )
    assert session.requested_urls == []

    result = await files.download_file(
        "https://files.example.com/export?note=drive.google.com&path=/file/d/SECRET123",
        output_dir=str(tmp_path),
        authorize_redirect=same_site_only,
    )

    assert Path(result).read_bytes() == b"x"
    assert session.requested_urls == ["https://files.example.com/export?note=drive.google.com&path=/file/d/SECRET123"]


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


@pytest.mark.asyncio
async def test_managed_local_upload_is_read_through_storage_not_the_downloads_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A local-storage upload is a file:// URI outside the browser-downloads directory, so naming it
    by id must read it through the storage layer's own org check rather than the legacy branch."""
    storage = MagicMock()
    storage.manages_local_file_uri = MagicMock(return_value=True)
    storage.assert_managed_file_access = MagicMock(return_value=None)
    storage.download_managed_file = AsyncMock(return_value=b"row_id,url\n")
    monkeypatch.setattr(forge_app, "STORAGE", storage)
    monkeypatch.setattr(settings, "ENV", "local")
    monkeypatch.setattr(
        files,
        "resolve_uploaded_file_id",
        AsyncMock(return_value="file:///srv/artifacts/local/org-1/2026-01-01/x.csv"),
    )

    path = await files.download_file("file_572714440041209402", organization_id="org-1")

    assert Path(path).read_bytes() == b"row_id,url\n"
    storage.assert_managed_file_access.assert_called_with("file:///srv/artifacts/local/org-1/2026-01-01/x.csv", "org-1")


@pytest.mark.asyncio
async def test_managed_storage_download_never_rewrites_an_already_staged_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same defect on the managed-storage branch, which is how a stored file reaches an upload.
    It names a deterministic path in the temp dir and opens it "wb", rewriting an already-staged
    file in place — the inode survives but the size and mtime do not, and those are what a file
    chooser re-validates at submit (SKY-16614)."""
    monkeypatch.setattr(settings, "TEMP_PATH", str(tmp_path))
    storage = MagicMock()
    storage.assert_managed_file_access = MagicMock(return_value=None)
    storage.download_managed_file = AsyncMock(return_value=b"first-stored-bytes")
    monkeypatch.setattr(forge_app, "STORAGE", storage)

    first = await files.download_file(
        "s3://bucket/org-1/attachment.pdf", organization_id="org-1", preserve_existing_files=True
    )
    staged = os.stat(first)

    storage.download_managed_file = AsyncMock(return_value=b"second-stored-bytes")
    second = await files.download_file(
        "s3://bucket/org-1/attachment.pdf", organization_id="org-1", preserve_existing_files=True
    )

    assert second != first
    assert Path(first).read_bytes() == b"first-stored-bytes"
    assert (os.stat(first).st_size, os.stat(first).st_mtime_ns) == (staged.st_size, staged.st_mtime_ns)
    assert Path(second).read_bytes() == b"second-stored-bytes"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["attachment (1).pdf", "attachment.pdf"]


@pytest.mark.asyncio
async def test_a_raw_file_path_is_read_through_storage_only_when_it_is_a_registered_upload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The org's storage tree also holds artifacts and browser-session files, so sharing it authorizes
    nothing: only a live uploaded_files row for the exact URI and organization does."""
    # Local, so a refused path reaches the legacy downloads-directory check rather than the HTTP fetcher.
    monkeypatch.setattr(settings, "ENV", "local")
    storage = LocalStorage(artifact_path=str(tmp_path / "artifacts"))
    upload_uri, _ = await storage.save_legacy_file(
        organization_id="org-1", filename="file_1_inputs.csv", fileObj=io.BytesIO(b"row_id,url\n")
    )
    cookies = tmp_path / "artifacts" / "local" / "org-1" / "browser_sessions" / "pbs_1" / "cookies.json"
    cookies.parent.mkdir(parents=True)
    cookies.write_bytes(b"secret-session\n")
    rows = {("org-1", upload_uri): SimpleNamespace(file_id="file_1")}

    async def by_uri(storage_uri: str, organization_id: str) -> SimpleNamespace | None:
        return rows.get((organization_id, storage_uri))

    monkeypatch.setattr(forge_app, "STORAGE", storage)
    monkeypatch.setattr(
        forge_app,
        "DATABASE",
        SimpleNamespace(uploaded_files=SimpleNamespace(get_uploaded_file_by_storage_uri=by_uri)),
    )
    monkeypatch.setattr(files.uploaded_file_service, "resolve_file_reference", AsyncMock(return_value=upload_uri))
    monkeypatch.setattr(settings, "TEMP_PATH", str(tmp_path / "temp"))

    path = await files.download_file(upload_uri, organization_id="org-1")
    assert Path(path).read_bytes() == b"row_id,url\n"

    for refused_uri, organization_id in ((f"file://{cookies}", "org-1"), (upload_uri, "org-2")):
        with pytest.raises(PermissionError):
            await files.download_file(refused_uri, organization_id=organization_id)


@pytest.mark.asyncio
async def test_a_managed_local_upload_is_readable_outside_the_local_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The storage backend is chosen by SKYVERN_STORAGE_TYPE, not ENV, so a self-hosted install
    running LocalStorage under ENV=production must still read the files it stores."""
    storage = MagicMock()
    storage.manages_local_file_uri = MagicMock(return_value=True)
    storage.assert_managed_file_access = MagicMock(return_value=None)
    storage.download_managed_file = AsyncMock(return_value=b"row_id,url\n")
    monkeypatch.setattr(forge_app, "STORAGE", storage)
    monkeypatch.setattr(settings, "ENV", "production")
    monkeypatch.setattr(
        files,
        "resolve_uploaded_file_id",
        AsyncMock(return_value="file:///srv/artifacts/production/org-1/2026-01-01/x.csv"),
    )

    path = await files.download_file("file_572714440041209403", organization_id="org-1")

    assert Path(path).read_bytes() == b"row_id,url\n"


@pytest.mark.asyncio
async def test_a_managed_local_upload_with_a_long_non_ascii_name_downloads_under_its_decoded_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Percent-encoding triples a CJK name's length, so naming the temporary copy after the encoded
    URI segment overruns the filesystem's name limit for an upload storage already accepted."""
    from urllib.parse import quote

    name = "file_572714440041209402_" + "表" * 70 + ".csv"
    storage = MagicMock()
    storage.manages_local_file_uri = MagicMock(return_value=True)
    storage.assert_managed_file_access = MagicMock(return_value=None)
    storage.download_managed_file = AsyncMock(return_value=b"row_id,url\n")
    monkeypatch.setattr(forge_app, "STORAGE", storage)
    monkeypatch.setattr(settings, "TEMP_PATH", str(tmp_path))
    monkeypatch.setattr(
        files,
        "resolve_uploaded_file_id",
        AsyncMock(return_value="file://" + quote(f"/srv/artifacts/org-1/{name}")),
    )

    path = await files.download_file("file_572714440041209402", organization_id="org-1")

    assert Path(path).name == name
    assert Path(path).read_bytes() == b"row_id,url\n"


@pytest.mark.parametrize("env", ["local", "production"])
def test_validation_accepts_an_uploaded_file_id_in_any_environment(env: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """validate_download_url gates the SDK upload route before download_file runs, so the two must
    agree: an upload is named by its id in every ENV, and a raw path under the organization's
    storage prefix is not a substitute for one."""
    storage = MagicMock()
    storage.manages_local_file_uri = MagicMock(return_value=True)
    storage.assert_managed_file_access = MagicMock(return_value=None)
    monkeypatch.setattr(forge_app, "STORAGE", storage)
    monkeypatch.setattr(settings, "ENV", env)

    assert files.validate_download_url("file_572714440041209402", organization_id="org-1")
    # A raw path is judged by the legacy downloads-directory rule in either ENV, never by the
    # organization's storage prefix, and an artifact root is outside that directory.
    assert files.validate_download_url("file:///srv/artifacts/org-1/2026-01-01/x.csv", organization_id="org-1") is False


def test_validation_still_rejects_an_unmanaged_local_file_outside_the_local_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = MagicMock()
    storage.manages_local_file_uri = MagicMock(return_value=False)
    storage.assert_managed_file_access = MagicMock(side_effect=PermissionError("not managed"))
    monkeypatch.setattr(forge_app, "STORAGE", storage)
    monkeypatch.setattr(settings, "ENV", "production")

    assert not files.validate_download_url("file:///etc/passwd", organization_id="org-1")


@pytest.mark.asyncio
async def test_unmanaged_local_file_uri_still_goes_through_the_downloads_directory_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = MagicMock()
    storage.manages_local_file_uri = MagicMock(return_value=False)
    storage.assert_managed_file_access = MagicMock(side_effect=PermissionError("not managed"))
    storage.download_managed_file = AsyncMock()
    monkeypatch.setattr(forge_app, "STORAGE", storage)
    monkeypatch.setattr(settings, "ENV", "local")

    with pytest.raises(PermissionError, match="outside the downloads directory"):
        await files.download_file("file:///etc/passwd", organization_id="org-1")

    storage.download_managed_file.assert_not_awaited()
