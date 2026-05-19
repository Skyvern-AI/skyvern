"""Tests for ScopedXhrDownloadCapture — action-scoped XHR download listener."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.webeye.actions.handler import ScopedXhrDownloadCapture


def _make_response(
    *,
    resource_type: str = "xhr",
    status: int = 200,
    content_type: str = "application/pdf",
    content_disposition: str = 'inline; filename="report.pdf"',
    body: bytes = b"%PDF-1.4 fake",
    content_length: str | None = None,
) -> MagicMock:
    resp = AsyncMock()
    resp.status = status
    request_mock = MagicMock()
    request_mock.resource_type = resource_type
    resp.request = request_mock
    resp.url = "https://example.com/api/report"
    headers = {"content-type": content_type, "content-disposition": content_disposition}
    if content_length is not None:
        headers["content-length"] = content_length
    resp.headers = headers
    resp.body = AsyncMock(return_value=body)
    return resp


def _make_page(*, cdp_active: bool = False) -> MagicMock:
    page = MagicMock()
    context = MagicMock()
    context._skyvern_cdp_download_active = cdp_active
    page.context = context
    return page


class TestScopedXhrDownloadCapture:
    def test_enable_registers_listener_on_page_and_context(self) -> None:
        page = _make_page()
        capture = ScopedXhrDownloadCapture(page, Path("/tmp/downloads"))
        capture.enable()
        page.on.assert_called_once_with("response", capture._on_response)
        page.context.on.assert_called_once_with("page", capture._on_new_page)

    def test_disable_removes_all_listeners(self) -> None:
        page = _make_page()
        capture = ScopedXhrDownloadCapture(page, Path("/tmp/downloads"))
        capture.enable()
        capture.disable()
        page.remove_listener.assert_called_once_with("response", capture._on_response)
        page.context.remove_listener.assert_called_once_with("page", capture._on_new_page)

    def test_skips_when_cdp_interceptor_active(self) -> None:
        page = _make_page(cdp_active=True)
        capture = ScopedXhrDownloadCapture(page, Path("/tmp/downloads"))
        capture.enable()
        page.on.assert_not_called()
        assert not capture._active

    def test_disable_noop_when_not_enabled(self) -> None:
        page = _make_page(cdp_active=True)
        capture = ScopedXhrDownloadCapture(page, Path("/tmp/downloads"))
        capture.enable()
        capture.disable()
        page.remove_listener.assert_not_called()

    @pytest.mark.asyncio
    async def test_saves_xhr_inline_pdf_to_download_dir(self, tmp_path: Path) -> None:
        page = _make_page()
        capture = ScopedXhrDownloadCapture(page, tmp_path)
        response = _make_response()

        await capture._on_response(response)

        saved = tmp_path / "report.pdf"
        assert saved.exists()
        assert saved.read_bytes() == b"%PDF-1.4 fake"

    @pytest.mark.asyncio
    async def test_saves_xhr_attachment_pdf(self, tmp_path: Path) -> None:
        page = _make_page()
        capture = ScopedXhrDownloadCapture(page, tmp_path)
        response = _make_response(content_disposition='attachment; filename="invoice.pdf"')

        await capture._on_response(response)

        assert (tmp_path / "invoice.pdf").exists()

    @pytest.mark.asyncio
    async def test_ignores_non_xhr_response(self, tmp_path: Path) -> None:
        page = _make_page()
        capture = ScopedXhrDownloadCapture(page, tmp_path)
        response = _make_response(resource_type="document")

        await capture._on_response(response)

        assert list(tmp_path.iterdir()) == []

    @pytest.mark.asyncio
    async def test_ignores_json_response(self, tmp_path: Path) -> None:
        page = _make_page()
        capture = ScopedXhrDownloadCapture(page, tmp_path)
        response = _make_response(content_type="application/json", content_disposition="")

        await capture._on_response(response)

        assert list(tmp_path.iterdir()) == []

    @pytest.mark.asyncio
    async def test_ignores_xhr_pdf_without_filename(self, tmp_path: Path) -> None:
        page = _make_page()
        capture = ScopedXhrDownloadCapture(page, tmp_path)
        response = _make_response(content_disposition="inline")

        await capture._on_response(response)

        assert list(tmp_path.iterdir()) == []

    @pytest.mark.asyncio
    async def test_ignores_error_responses(self, tmp_path: Path) -> None:
        page = _make_page()
        capture = ScopedXhrDownloadCapture(page, tmp_path)
        response = _make_response(status=403)

        await capture._on_response(response)

        assert list(tmp_path.iterdir()) == []

    @pytest.mark.asyncio
    async def test_deduplicates_same_filename(self, tmp_path: Path) -> None:
        page = _make_page()
        capture = ScopedXhrDownloadCapture(page, tmp_path)
        response1 = _make_response(body=b"first")
        response2 = _make_response(body=b"second")

        await capture._on_response(response1)
        await capture._on_response(response2)

        saved = tmp_path / "report.pdf"
        assert saved.read_bytes() == b"first"

    @pytest.mark.asyncio
    async def test_sanitizes_path_traversal(self, tmp_path: Path) -> None:
        page = _make_page()
        capture = ScopedXhrDownloadCapture(page, tmp_path)
        response = _make_response(content_disposition='inline; filename="../../etc/evil.pdf"')

        await capture._on_response(response)

        assert not (tmp_path.parent.parent / "etc" / "evil.pdf").exists()
        assert (tmp_path / "evil.pdf").exists()

    @pytest.mark.asyncio
    async def test_skips_oversized_content_length(self, tmp_path: Path) -> None:
        page = _make_page()
        capture = ScopedXhrDownloadCapture(page, tmp_path)
        response = _make_response(content_length="200000000")

        await capture._on_response(response)

        assert list(tmp_path.iterdir()) == []

    def test_new_page_gets_listener(self) -> None:
        page = _make_page()
        capture = ScopedXhrDownloadCapture(page, Path("/tmp/downloads"))
        new_page = MagicMock()

        capture.enable()
        capture._on_new_page(new_page)

        new_page.on.assert_called_once_with("response", capture._on_response)
        assert new_page in capture._extra_pages

    def test_disable_cleans_up_extra_pages(self) -> None:
        page = _make_page()
        capture = ScopedXhrDownloadCapture(page, Path("/tmp/downloads"))
        new_page = MagicMock()

        capture.enable()
        capture._on_new_page(new_page)
        capture.disable()

        new_page.remove_listener.assert_called_once_with("response", capture._on_response)
        assert capture._extra_pages == []
        assert not capture._active
