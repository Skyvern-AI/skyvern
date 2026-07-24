from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest
from multidict import CIMultiDict, CIMultiDictProxy

from skyvern.config import settings
from skyvern.exceptions import BlockedHost, DownloadFileMaxSizeExceeded
from skyvern.forge.sdk.api import files


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


@pytest.mark.asyncio
async def test_download_file_blocks_loopback_url_before_http_request(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_if_http_session_opens(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("loopback URL should be rejected before opening an HTTP session")

    monkeypatch.setattr(files.aiohttp, "ClientSession", fail_if_http_session_opens)

    with pytest.raises(BlockedHost, match="127.0.0.1"):
        await files.download_file("http://127.0.0.1:45427/private")
