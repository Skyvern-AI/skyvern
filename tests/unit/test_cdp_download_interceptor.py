"""Unit tests for CDPDownloadInterceptor pure functions and proxy auth handling."""

import ast
import asyncio
import base64
import contextlib
import gc
import inspect
import textwrap
import threading
import weakref
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from structlog.testing import capture_logs

import skyvern.webeye.cdp_download_interceptor as mod
from skyvern.forge.sdk.core.http_request_authorization import deny_unenrolled_redirect_hop
from skyvern.webeye.cdp_download_interceptor import (
    CDPDownloadInterceptor,
    _is_stale_interception_error,
    extract_filename,
    is_download_response,
)


def _make_interceptor(
    *args: Any,
    network_egress_monitor: Any | None = None,
    redirect_hop_authorizer: Any | None = None,
    **kwargs: Any,
) -> CDPDownloadInterceptor:
    if network_egress_monitor is None:
        network_egress_monitor = MagicMock()
        network_egress_monitor.authorize_request.return_value = True
    if redirect_hop_authorizer is None:
        redirect_hop_authorizer = AsyncMock(side_effect=AssertionError("unexpected direct HTTP request"))
    return mod.CDPDownloadInterceptor(
        *args,
        network_egress_monitor=network_egress_monitor,
        redirect_hop_authorizer=redirect_hop_authorizer,
        **kwargs,
    )


@pytest.mark.parametrize(
    ("network_egress_monitor", "redirect_hop_authorizer"),
    [
        pytest.param(None, AsyncMock(), id="missing-monitor"),
        pytest.param(MagicMock(), None, id="missing-authorizer"),
    ],
)
def test_constructor_rejects_missing_required_collaborator(
    network_egress_monitor: Any, redirect_hop_authorizer: Any
) -> None:
    with pytest.raises(TypeError, match="required collaborators"):
        CDPDownloadInterceptor(
            network_egress_monitor=network_egress_monitor,
            redirect_hop_authorizer=redirect_hop_authorizer,
        )


class TestIsDownloadResponse:
    """Tests for is_download_response()."""

    @pytest.mark.parametrize(
        ("headers", "status_code", "resource_type", "expected"),
        [
            pytest.param(
                {"content-disposition": 'Attachment; filename="report.csv"', "content-type": "text/csv"},
                200,
                "",
                True,
                id="attachment_header",
            ),
            pytest.param(
                {"content-disposition": 'attachment; filename="report.csv"', "content-type": "text/csv"},
                200,
                "",
                True,
                id="attachment_header_lowercase",
            ),
            pytest.param(
                {"content-type": "application/pdf"},
                200,
                "",
                True,
                id="download_mime_pdf",
            ),
            pytest.param(
                {"content-type": "application/zip"},
                200,
                "",
                True,
                id="download_mime_zip",
            ),
            pytest.param(
                {"content-type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"},
                200,
                "",
                True,
                id="download_mime_xlsx",
            ),
            pytest.param(
                {"content-type": "application/octet-stream"},
                200,
                "",
                True,
                id="download_mime_octet_stream",
            ),
            pytest.param(
                {"content-type": "application/pdf; charset=utf-8"},
                200,
                "",
                True,
                id="download_mime_with_charset",
            ),
            pytest.param({"content-type": "text/html"}, 200, "", False, id="html_not_download"),
            pytest.param({"content-type": "application/json"}, 200, "", False, id="json_not_download"),
            pytest.param(
                {"content-disposition": "attachment", "content-type": "application/json"},
                200,
                "",
                False,
                id="api_attachment_not_download",
            ),
            pytest.param({"content-type": "application/xml"}, 200, "", False, id="xml_not_download"),
            pytest.param({"content-type": "application/grpc"}, 200, "", False, id="grpc_not_download"),
            pytest.param({}, 200, "", False, id="empty_headers_not_download"),
            pytest.param(
                {"content-disposition": "attachment", "content-type": "application/octet-stream"},
                200,
                "XHR",
                True,
                id="xhr_attachment_download_mime",
            ),
            pytest.param(
                {"content-disposition": "attachment", "content-type": "application/pdf"},
                200,
                "Fetch",
                True,
                id="fetch_attachment_download_mime",
            ),
            pytest.param(
                {
                    "content-disposition": (
                        "attachment; filename=Invoice_12345.pdf; filename*=UTF-8''Invoice_12345.pdf"
                    ),
                    "content-type": "application/pdf",
                },
                200,
                "XHR",
                True,
                id="xhr_attachment_pdf_filename_star",
            ),
            pytest.param(
                {"content-disposition": 'attachment; filename="f.txt"', "content-type": "text/plain; charset=UTF-8"},
                200,
                "XHR",
                False,
                id="xhr_attachment_text_plain_not_download",
            ),
            pytest.param(
                {"content-disposition": "attachment", "content-type": "text/html"},
                200,
                "XHR",
                False,
                id="xhr_attachment_text_html_not_download",
            ),
            pytest.param(
                {"content-disposition": "attachment"},
                200,
                "Fetch",
                False,
                id="fetch_attachment_only_not_download",
            ),
            pytest.param(
                {"content-disposition": "attachment", "content-type": "text/csv"},
                200,
                "XHR",
                True,
                id="xhr_attachment_csv_is_download",
            ),
            pytest.param(
                {"content-disposition": "attachment", "content-type": "application/csv"},
                200,
                "XHR",
                True,
                id="xhr_attachment_application_csv_is_download",
            ),
            pytest.param(
                {"content-type": "application/*", "content-length": "46681129"},
                200,
                "XHR",
                True,
                id="xhr_generic_binary_with_bytes_is_download",
            ),
            pytest.param(
                {"content-type": "application/*"},
                200,
                "XHR",
                False,
                id="xhr_generic_binary_no_length_not_download",
            ),
            pytest.param(
                {"content-type": "application/*", "content-length": "6"},
                200,
                "XHR",
                False,
                id="xhr_generic_binary_small_body_not_download",
            ),
            pytest.param(
                {"content-type": "application/*"},
                200,
                "",
                False,
                id="non_xhr_generic_binary_no_length_not_download",
            ),
            pytest.param(
                {"content-type": "application/*"},
                200,
                "Document",
                False,
                id="non_xhr_generic_binary_document_no_length_not_download",
            ),
            pytest.param(
                {"content-type": "application/*", "content-length": "9999999"},
                200,
                "Other",
                False,
                id="non_xhr_generic_binary_other_large_not_download",
            ),
            pytest.param(
                {"content-type": "application/*", "content-length": "2048"},
                200,
                "Fetch",
                True,
                id="fetch_generic_binary_with_bytes_is_download",
            ),
            pytest.param(
                {"content-type": "application/pdf"},
                200,
                "XHR",
                False,
                id="xhr_mime_only_not_download",
            ),
            pytest.param(
                {"content-type": "application/octet-stream"},
                200,
                "Fetch",
                False,
                id="fetch_mime_only_not_download",
            ),
            pytest.param(
                {"content-disposition": "attachment", "content-type": "application/json"},
                200,
                "XHR",
                False,
                id="xhr_json_attachment_not_download",
            ),
            pytest.param(
                {"content-type": "application/octet-stream"},
                200,
                "Font",
                False,
                id="font_resource_type_not_download",
            ),
            pytest.param(
                {"content-type": "application/octet-stream"},
                200,
                "Stylesheet",
                False,
                id="stylesheet_resource_type_not_download",
            ),
            pytest.param(
                {"content-type": "application/octet-stream"},
                200,
                "Script",
                False,
                id="script_resource_type_not_download",
            ),
            pytest.param(
                {"content-type": "application/octet-stream"},
                200,
                "Image",
                False,
                id="image_resource_type_not_download",
            ),
            pytest.param(
                {"content-disposition": "attachment", "content-type": "application/pdf"},
                200,
                "Document",
                True,
                id="document_resource_type_is_download",
            ),
            pytest.param(
                {"content-disposition": "attachment", "content-type": "application/pdf"},
                404,
                "",
                False,
                id="error_status_code_not_download",
            ),
            pytest.param(
                {"content-type": "application/octet-stream"},
                500,
                "",
                False,
                id="server_error_not_download",
            ),
        ],
    )
    def test_is_download_response_table(
        self,
        headers: dict[str, str],
        status_code: int,
        resource_type: str,
        expected: bool,
    ) -> None:
        assert is_download_response(headers, status_code, resource_type=resource_type) is expected

    def test_xhr_inline_pdf_with_filename_not_download(self) -> None:
        """XHR with inline + filename is NOT a CDP download — handled by ScopedXhrDownloadCapture instead."""
        headers = {
            "content-disposition": 'inline; filename="Denali 10.pdf"',
            "content-type": "application/pdf",
        }
        assert is_download_response(headers, 200, resource_type="XHR") is False


class TestExtractFilename:
    """Tests for extract_filename().

    extract_filename returns an empty string when no filename can be determined —
    the caller (_resolve_save_path) is responsible for generating a fallback name.
    """

    @pytest.mark.parametrize(
        ("headers", "url", "expected"),
        [
            pytest.param(
                {"content-disposition": "attachment; filename*=UTF-8''my%20report%282024%29.pdf"},
                "https://example.com/download",
                "my report(2024).pdf",
                id="rfc5987_filename_star",
            ),
            pytest.param(
                {"content-disposition": 'attachment; filename="report.csv"'},
                "https://example.com/download",
                "report.csv",
                id="regular_filename",
            ),
            pytest.param(
                {"content-disposition": "attachment; filename=report.csv"},
                "https://example.com/download",
                "report.csv",
                id="unquoted_filename",
            ),
            pytest.param(
                {"content-disposition": "attachment; filename=\"fallback.csv\"; filename*=UTF-8''preferred.csv"},
                "https://example.com/download",
                "preferred.csv",
                id="filename_star_takes_priority",
            ),
            pytest.param(
                {},
                "https://example.com/files/document.pdf",
                "document.pdf",
                id="url_path_fallback",
            ),
            pytest.param(
                {},
                "https://example.com/files/my%20report.xlsx",
                "my report.xlsx",
                id="url_path_with_encoded_chars",
            ),
            pytest.param(
                {},
                "https://example.com/download",
                "",
                id="url_path_no_extension_returns_empty",
            ),
            pytest.param(
                {},
                "https://example.com/api/export",
                "",
                id="no_headers_no_url_returns_empty",
            ),
            pytest.param(
                {"content-disposition": ""},
                "https://example.com/files/data.csv",
                "data.csv",
                id="empty_content_disposition_url_fallback",
            ),
            pytest.param(
                {"content-disposition": "inline"},
                "https://example.com/files/report.pdf",
                "report.pdf",
                id="content_disposition_inline_url_fallback",
            ),
        ],
    )
    def test_extract_filename_table(self, headers: dict[str, str], url: str, expected: str) -> None:
        assert extract_filename(headers, url) == expected

    def test_path_traversal_returned_raw(self) -> None:
        """extract_filename returns raw name; sanitization is done in _resolve_save_path."""
        headers = {"content-disposition": 'attachment; filename="../../etc/cron.d/evil"'}
        result = extract_filename(headers, "https://example.com/download")
        assert result == "../../etc/cron.d/evil"


class TestResolveSavePath:
    """Tests for CDPDownloadInterceptor._resolve_save_path()."""

    def _make_interceptor(self, tmp_path: Path) -> CDPDownloadInterceptor:
        interceptor = _make_interceptor(output_dir=str(tmp_path))
        return interceptor

    @pytest.mark.parametrize(
        ("raw_filename", "content_type", "expected_filename", "preexisting_file", "output_dir_parts"),
        [
            pytest.param("report.pdf", "", "report.pdf", False, (), id="normal_filename"),
            pytest.param("", "", None, False, (), id="empty_filename_uuid_fallback"),
            pytest.param(None, "", None, False, (), id="default_param_empty_string"),
            pytest.param("report.pdf", "", "report.pdf", True, (), id="collision_returns_same_path"),
            pytest.param("file.txt", "", "file.txt", False, ("sub", "dir"), id="creates_missing_output_dir"),
        ],
    )
    def test_resolve_save_path_table(
        self,
        tmp_path: Path,
        raw_filename: str | None,
        content_type: str,
        expected_filename: str | None,
        preexisting_file: bool,
        output_dir_parts: tuple[str, ...],
    ) -> None:
        output_dir = tmp_path.joinpath(*output_dir_parts)
        interceptor = self._make_interceptor(output_dir)
        if preexisting_file:
            (output_dir / raw_filename).write_bytes(b"existing")

        if raw_filename is None:
            save_path, filename = interceptor._resolve_save_path()
        else:
            save_path, filename = interceptor._resolve_save_path(raw_filename, content_type)

        if expected_filename is None:
            assert filename.startswith("download_")
            assert len(filename) > len("download_")
        else:
            assert filename == expected_filename
        assert save_path == output_dir / filename
        assert output_dir.exists()

    def test_empty_filename_gets_pdf_uuid_fallback(self, tmp_path: Path) -> None:
        interceptor = self._make_interceptor(tmp_path)
        save_path, filename = interceptor._resolve_save_path("", "application/pdf")
        assert filename.startswith("download_")
        assert filename.endswith(".pdf")
        assert save_path == tmp_path / filename

    @pytest.mark.parametrize(
        "filename",
        [
            pytest.param("../../etc/cron.d/evil", id="traversal"),
            pytest.param("../evil", id="parent"),
            pytest.param("nested/report.pdf", id="posix-separator"),
            pytest.param(r"nested\report.pdf", id="windows-separator"),
            pytest.param("/tmp/report.pdf", id="absolute-posix"),
            pytest.param(r"C:\temp\report.pdf", id="absolute-windows"),
            pytest.param("%2Ftmp%2Freport.pdf", id="encoded-posix-separator"),
            pytest.param(r"%5Cserver%5Cshare%5Creport.pdf", id="encoded-windows-separator"),
            pytest.param("%252Ftmp%252Freport.pdf", id="double-encoded-posix-separator"),
            pytest.param(r"%255Cserver%255Cshare%255Creport.pdf", id="double-encoded-windows-separator"),
            pytest.param("%252e%252e%252freport.pdf", id="double-encoded-traversal"),
            pytest.param(".", id="current-directory"),
            pytest.param("..", id="parent-directory"),
            pytest.param("report\x00.pdf", id="nul"),
            pytest.param("report\n.pdf", id="control-character"),
        ],
    )
    def test_non_basename_filename_fails_closed(self, tmp_path: Path, filename: str) -> None:
        interceptor = self._make_interceptor(tmp_path)

        with pytest.raises(ValueError, match="basename"):
            interceptor._resolve_save_path(filename)

    def test_missing_extension_uses_pdf_content_type(self, tmp_path: Path) -> None:
        interceptor = self._make_interceptor(tmp_path)
        save_path, filename = interceptor._resolve_save_path("2026", "application/pdf; charset=utf-8")
        assert filename == "2026.pdf"
        assert save_path == tmp_path / "2026.pdf"

    def test_existing_pdf_extension_not_duplicated(self, tmp_path: Path) -> None:
        interceptor = self._make_interceptor(tmp_path)
        save_path, filename = interceptor._resolve_save_path("invoice_2026.pdf", "application/pdf")
        assert filename == "invoice_2026.pdf"
        assert save_path == tmp_path / "invoice_2026.pdf"


class TestConfinedDownloadWrites:
    def test_symlinked_output_directory_fails_closed(self, tmp_path: Path) -> None:
        outside = tmp_path / "outside"
        outside.mkdir()
        output_dir = tmp_path / "downloads"
        output_dir.symlink_to(outside, target_is_directory=True)
        interceptor = _make_interceptor(output_dir=str(output_dir))

        with pytest.raises(OSError):
            save_path, _ = interceptor._resolve_save_path("report.pdf")
            interceptor._atomically_write_bytes(save_path, b"private report")

        assert not (outside / "report.pdf").exists()

    def test_symlink_destination_fails_closed(self, tmp_path: Path) -> None:
        interceptor = _make_interceptor(output_dir=str(tmp_path))
        save_path, _ = interceptor._resolve_save_path("report.pdf")
        outside = tmp_path.parent / f"{tmp_path.name}-outside.txt"
        outside.write_bytes(b"outside")
        save_path.symlink_to(outside)

        with pytest.raises(OSError):
            interceptor._atomically_write_bytes(save_path, b"private report")

        assert outside.read_bytes() == b"outside"

    def test_hard_link_destination_fails_closed(self, tmp_path: Path) -> None:
        interceptor = _make_interceptor(output_dir=str(tmp_path))
        save_path, _ = interceptor._resolve_save_path("report.pdf")
        outside = tmp_path.parent / f"{tmp_path.name}-hard-link-outside.txt"
        outside.write_bytes(b"outside")
        save_path.hardlink_to(outside)

        with pytest.raises(FileExistsError):
            interceptor._atomically_write_bytes(save_path, b"private report")

        assert outside.read_bytes() == b"outside"

    def test_fdopen_failure_leaks_neither_fd_nor_temp_file(self, tmp_path: Path) -> None:
        """A failure between os.open and os.fdopen (e.g. MemoryError) must still close the raw fd,
        unlink the just-created temp file, and close the directory fd — not just the directory fd."""
        interceptor = _make_interceptor(output_dir=str(tmp_path))
        save_path, _ = interceptor._resolve_save_path("report.pdf")

        with patch("os.fdopen", side_effect=OSError("no file descriptors available")):
            with pytest.raises(OSError, match="no file descriptors available"):
                interceptor._atomically_write_bytes(save_path, b"private report")

        assert list(tmp_path.iterdir()) == []

    def test_collision_hard_link_race_fails_before_existing_inode_is_modified(self, tmp_path: Path) -> None:
        interceptor = _make_interceptor(output_dir=str(tmp_path))
        save_path, _ = interceptor._resolve_save_path("report.pdf")
        save_path.write_bytes(b"existing report")
        outside = tmp_path.parent / f"{tmp_path.name}-racing-hard-link.txt"
        real_ftruncate = mod.os.ftruncate

        def add_outside_link_before_truncate(file_descriptor: int, length: int) -> None:
            outside.hardlink_to(save_path)
            real_ftruncate(file_descriptor, length)

        with (
            patch.object(mod.os, "ftruncate", side_effect=add_outside_link_before_truncate),
            pytest.raises(FileExistsError),
        ):
            interceptor._atomically_write_bytes(save_path, b"private report")

        assert save_path.read_bytes() == b"existing report"
        assert not outside.exists()

    def test_post_check_destination_swap_fails_at_publication(self, tmp_path: Path) -> None:
        interceptor = _make_interceptor(output_dir=str(tmp_path))
        save_path, _ = interceptor._resolve_save_path("report.pdf")
        outside = tmp_path.parent / f"{tmp_path.name}-outside.txt"
        outside.write_bytes(b"outside")
        real_link = mod.os.link
        swapped = False

        def swap_destination_then_link(src: str, dst: str, **kwargs: Any) -> None:
            nonlocal swapped
            if dst == save_path.name:
                save_path.symlink_to(outside)
                swapped = True
            real_link(src, dst, **kwargs)

        with (
            patch.object(mod.os, "link", side_effect=swap_destination_then_link),
            pytest.raises(OSError),
        ):
            interceptor._atomically_write_bytes(save_path, b"private report")

        assert swapped
        assert save_path.is_symlink()
        assert outside.read_bytes() == b"outside"

    def test_alias_on_temporary_inode_during_link_fails_at_publication(self, tmp_path: Path) -> None:
        interceptor = _make_interceptor(output_dir=str(tmp_path))
        save_path, _ = interceptor._resolve_save_path("report.pdf")
        alias = tmp_path.parent / f"{tmp_path.name}-temporary-alias.txt"
        real_link = mod.os.link

        def alias_temporary_inode_then_link(src: str, dst: str, **kwargs: Any) -> None:
            real_link(tmp_path / src, alias)
            real_link(src, dst, **kwargs)

        with (
            patch.object(mod.os, "link", side_effect=alias_temporary_inode_then_link),
            pytest.raises(OSError, match="unexpected link"),
        ):
            interceptor._atomically_write_bytes(save_path, b"private report")

        assert not save_path.exists()
        assert list(tmp_path.iterdir()) == []

    def test_post_check_output_directory_swap_fails_closed(self, tmp_path: Path) -> None:
        output_dir = tmp_path / "downloads"
        interceptor = _make_interceptor(output_dir=str(output_dir))
        save_path, _ = interceptor._resolve_save_path("report.pdf")
        original_dir = tmp_path / "original-downloads"
        output_dir.rename(original_dir)
        output_dir.mkdir()

        with pytest.raises(OSError, match="changed"):
            interceptor._atomically_write_bytes(save_path, b"private report")

        assert list(output_dir.iterdir()) == []
        assert list(original_dir.iterdir()) == []


class TestCDPDownloadInterceptorProxyAuth:
    """Tests for CDP proxy authentication handling (Fetch.authRequired + continueWithAuth)."""

    def _make_interceptor(
        self,
        proxy_username: str | None = None,
        proxy_password: str | None = None,
    ) -> CDPDownloadInterceptor:
        network_egress_monitor = MagicMock()
        network_egress_monitor.authorize_request.return_value = True
        return _make_interceptor(
            output_dir="/tmp/test_downloads",
            proxy_username=proxy_username,
            proxy_password=proxy_password,
            network_egress_monitor=network_egress_monitor,
        )

    def _make_cdp_session(self) -> MagicMock:
        session = MagicMock()
        session.send = AsyncMock()
        return session

    @pytest.mark.asyncio
    async def test_proxy_auth_provides_credentials(self) -> None:
        """Proxy 407 challenge should respond with ProvideCredentials when credentials are available."""
        interceptor = self._make_interceptor(proxy_username="user1", proxy_password="pass1")
        cdp_session = self._make_cdp_session()

        event = {
            "requestId": "req-1",
            "authChallenge": {"source": "Proxy", "origin": "http://proxy.example.com"},
            "request": {"url": "https://example.com/page"},
        }

        await interceptor._handle_auth_required(event, cdp_session)

        cdp_session.send.assert_called_once_with(
            "Fetch.continueWithAuth",
            {
                "requestId": "req-1",
                "authChallengeResponse": {
                    "response": "ProvideCredentials",
                    "username": "user1",
                    "password": "pass1",
                },
            },
        )

    @pytest.mark.asyncio
    async def test_non_proxy_auth_cancels(self) -> None:
        """Non-proxy auth challenges (e.g., HTTP Basic from origin) should be cancelled."""
        interceptor = self._make_interceptor(proxy_username="user1", proxy_password="pass1")
        cdp_session = self._make_cdp_session()

        event = {
            "requestId": "req-2",
            "authChallenge": {"source": "Server", "origin": "https://example.com"},
            "request": {"url": "https://example.com/protected"},
        }

        await interceptor._handle_auth_required(event, cdp_session)

        cdp_session.send.assert_called_once_with(
            "Fetch.continueWithAuth",
            {
                "requestId": "req-2",
                "authChallengeResponse": {"response": "CancelAuth"},
            },
        )

    @pytest.mark.asyncio
    async def test_no_credentials_cancels_proxy_auth(self) -> None:
        """Proxy auth challenge without credentials should be cancelled."""
        interceptor = self._make_interceptor()  # No credentials
        cdp_session = self._make_cdp_session()

        event = {
            "requestId": "req-3",
            "authChallenge": {"source": "Proxy", "origin": "http://proxy.example.com"},
            "request": {"url": "https://example.com/page"},
        }

        await interceptor._handle_auth_required(event, cdp_session)

        cdp_session.send.assert_called_once_with(
            "Fetch.continueWithAuth",
            {
                "requestId": "req-3",
                "authChallengeResponse": {"response": "CancelAuth"},
            },
        )

    @pytest.mark.asyncio
    async def test_partial_credentials_cancels(self) -> None:
        """Proxy auth with only username (no password) should cancel."""
        interceptor = self._make_interceptor(proxy_username="user1")
        cdp_session = self._make_cdp_session()

        event = {
            "requestId": "req-4",
            "authChallenge": {"source": "Proxy", "origin": "http://proxy.example.com"},
            "request": {"url": "https://example.com/page"},
        }

        await interceptor._handle_auth_required(event, cdp_session)

        cdp_session.send.assert_called_once_with(
            "Fetch.continueWithAuth",
            {
                "requestId": "req-4",
                "authChallengeResponse": {"response": "CancelAuth"},
            },
        )

    @pytest.mark.asyncio
    async def test_auth_error_does_not_raise(self) -> None:
        """Errors during auth handling should be caught, not raised."""
        interceptor = self._make_interceptor(proxy_username="user1", proxy_password="pass1")
        cdp_session = self._make_cdp_session()
        cdp_session.send.side_effect = Exception("CDP connection lost")

        event = {
            "requestId": "req-5",
            "authChallenge": {"source": "Proxy", "origin": "http://proxy.example.com"},
            "request": {"url": "https://example.com/page"},
        }

        # Should not raise
        await interceptor._handle_auth_required(event, cdp_session)

    def test_init_stores_proxy_credentials(self) -> None:
        """Constructor should store proxy credentials."""
        interceptor = self._make_interceptor(proxy_username="user", proxy_password="pass")
        assert interceptor._proxy_username == "user"
        assert interceptor._proxy_password == "pass"

    def test_init_no_proxy_credentials(self) -> None:
        """Constructor without credentials should store None."""
        interceptor = self._make_interceptor()
        assert interceptor._proxy_username is None
        assert interceptor._proxy_password is None

    @pytest.mark.asyncio
    async def test_enable_for_page_with_proxy_auth(self) -> None:
        """enable_for_page with credentials should add Request-stage pattern and authRequired handler."""
        interceptor = self._make_interceptor(proxy_username="user", proxy_password="pass")

        mock_cdp_session = self._make_cdp_session()
        mock_page = MagicMock()
        mock_page.url = "about:blank"
        mock_page.context.new_cdp_session = AsyncMock(return_value=mock_cdp_session)

        await interceptor.enable_for_page(mock_page)

        # Verify Fetch.enable with both Response (downloads) and Request (auth) patterns
        mock_cdp_session.send.assert_called_once_with(
            "Fetch.enable",
            {
                "patterns": [
                    {"requestStage": "Response"},
                    {"urlPattern": "*", "requestStage": "Request"},
                ],
                "handleAuthRequests": True,
            },
        )

        # Verify both handlers registered
        event_names = [call.args[0] for call in mock_cdp_session.on.call_args_list]
        assert "Fetch.requestPaused" in event_names
        assert "Fetch.authRequired" in event_names

    @pytest.mark.asyncio
    async def test_enable_for_page_without_proxy_auth(self) -> None:
        """Network mediation requires Request-stage interception even without proxy auth."""
        interceptor = self._make_interceptor()

        mock_cdp_session = self._make_cdp_session()
        mock_page = MagicMock()
        mock_page.url = "about:blank"
        mock_page.context.new_cdp_session = AsyncMock(return_value=mock_cdp_session)

        await interceptor.enable_for_page(mock_page)

        # Request-stage mediation is always enabled; auth handling remains proxy-only.
        mock_cdp_session.send.assert_called_once_with(
            "Fetch.enable",
            {
                "patterns": [
                    {"requestStage": "Response"},
                    {"urlPattern": "*", "requestStage": "Request"},
                ],
                "handleAuthRequests": False,
            },
        )

        # Verify only requestPaused handler (no authRequired)
        event_names = [call.args[0] for call in mock_cdp_session.on.call_args_list]
        assert "Fetch.requestPaused" in event_names
        assert "Fetch.authRequired" not in event_names

    @pytest.mark.asyncio
    async def test_enable_registers_page_only_after_fetch_interception_is_live(self) -> None:
        interceptor = self._make_interceptor()
        monitor = interceptor._network_egress_monitor
        cdp_session = self._make_cdp_session()
        page = MagicMock(url="about:blank")
        page.context.new_cdp_session = AsyncMock(return_value=cdp_session)
        ordering: list[str] = []
        cdp_session.send.side_effect = lambda method, *_: ordering.append(method)
        monitor.register_active_request_interceptor.side_effect = lambda **_: ordering.append("register")

        await interceptor.enable_for_page(page)

        assert ordering == ["Fetch.enable", "register"]
        monitor.register_active_request_interceptor.assert_called_once_with(page=page, owner=interceptor)

    @pytest.mark.asyncio
    async def test_fetch_enable_failure_never_registers_page(self) -> None:
        interceptor = self._make_interceptor()
        monitor = interceptor._network_egress_monitor
        cdp_session = self._make_cdp_session()
        page = MagicMock(url="about:blank")
        page.context.new_cdp_session = AsyncMock(return_value=cdp_session)
        ordering: list[str] = []

        async def ambiguous_send(method: str, *_: object) -> None:
            ordering.append(method)
            if method == "Fetch.enable":
                raise RuntimeError("Fetch unavailable")

        cdp_session.send.side_effect = ambiguous_send
        monitor.invalidate.side_effect = lambda: ordering.append("invalidate")

        with pytest.raises(RuntimeError, match="Fetch unavailable"):
            await interceptor.enable_for_page(page)

        monitor.register_active_request_interceptor.assert_not_called()
        assert ordering == ["Fetch.enable", "invalidate", "Fetch.disable"]
        assert not interceptor._accepting_cdp_handlers
        assert interceptor._cdp_sessions == []

    @pytest.mark.asyncio
    async def test_registration_failure_invalidates_and_disables_fetch(self) -> None:
        interceptor = self._make_interceptor()
        monitor = interceptor._network_egress_monitor
        monitor.register_active_request_interceptor.side_effect = RuntimeError("registration failed")
        cdp_session = self._make_cdp_session()
        page = MagicMock(url="about:blank")
        page.context.new_cdp_session = AsyncMock(return_value=cdp_session)

        with pytest.raises(RuntimeError, match="registration failed"):
            await interceptor.enable_for_page(page)

        monitor.invalidate.assert_called_once_with()
        cdp_session.send.assert_awaited_with("Fetch.disable")
        assert interceptor._cdp_sessions == []

    @pytest.mark.asyncio
    async def test_page_closed_during_registration_unregisters_and_disables_fetch(self) -> None:
        interceptor = self._make_interceptor()
        monitor = interceptor._network_egress_monitor
        cdp_session = self._make_cdp_session()
        page = MagicMock(url="about:blank")
        page.is_closed.return_value = True
        page.context.new_cdp_session = AsyncMock(return_value=cdp_session)

        with pytest.raises(RuntimeError, match="closed during interceptor registration"):
            await interceptor.enable_for_page(page)

        monitor.unregister_active_request_interceptor.assert_called_once_with(page=page, owner=interceptor)
        cdp_session.send.assert_awaited_with("Fetch.disable")
        assert interceptor._cdp_sessions == []

    @pytest.mark.asyncio
    async def test_page_close_unregisters_exact_owner_once(self) -> None:
        interceptor = self._make_interceptor()
        monitor = interceptor._network_egress_monitor
        cdp_session = self._make_cdp_session()
        page = MagicMock(url="about:blank")
        page.context.new_cdp_session = AsyncMock(return_value=cdp_session)
        await interceptor.enable_for_page(page)
        close_listener = next(call.args[1] for call in page.on.call_args_list if call.args[0] == "close")

        close_listener(page)
        await interceptor.disable()

        monitor.unregister_active_request_interceptor.assert_called_once_with(page=page, owner=interceptor)

    @pytest.mark.asyncio
    async def test_page_close_identity_mismatch_invalidates_and_unregisters_bound_page(self) -> None:
        interceptor = self._make_interceptor()
        monitor = interceptor._network_egress_monitor
        cdp_session = self._make_cdp_session()
        page = MagicMock(url="about:blank")
        page.context.new_cdp_session = AsyncMock(return_value=cdp_session)
        await interceptor.enable_for_page(page)
        close_listener = next(call.args[1] for call in page.on.call_args_list if call.args[0] == "close")

        close_listener(MagicMock(url="about:blank"))

        monitor.invalidate.assert_called_once_with()
        monitor.unregister_active_request_interceptor.assert_called_once_with(page=page, owner=interceptor)

    @pytest.mark.asyncio
    async def test_sequential_duplicate_page_enable_fails_before_second_cdp_session(self) -> None:
        interceptor = self._make_interceptor()
        cdp_session = self._make_cdp_session()
        page = MagicMock(url="about:blank")
        page.context.new_cdp_session = AsyncMock(return_value=cdp_session)
        await interceptor.enable_for_page(page)

        with pytest.raises(RuntimeError, match="already active or enrolling"):
            await interceptor.enable_for_page(page)

        page.context.new_cdp_session.assert_awaited_once_with(page)
        assert len(interceptor._cdp_sessions) == 1

    @pytest.mark.asyncio
    async def test_concurrent_duplicate_page_enable_fails_while_first_is_in_flight(self) -> None:
        interceptor = self._make_interceptor()
        cdp_session = self._make_cdp_session()
        page = MagicMock(url="about:blank")
        first_started = asyncio.Event()
        release_first = asyncio.Event()
        session_calls = 0

        async def paused_new_session(_: object) -> MagicMock:
            nonlocal session_calls
            session_calls += 1
            if session_calls == 1:
                first_started.set()
                await release_first.wait()
            return cdp_session

        page.context.new_cdp_session = AsyncMock(side_effect=paused_new_session)
        first_enable = asyncio.create_task(interceptor.enable_for_page(page))
        await first_started.wait()
        try:
            with pytest.raises(RuntimeError, match="already active or enrolling"):
                await interceptor.enable_for_page(page)
        finally:
            release_first.set()
            await first_enable

        page.context.new_cdp_session.assert_awaited_once_with(page)
        assert len(interceptor._cdp_sessions) == 1

    @pytest.mark.asyncio
    async def test_disable_unregisters_every_page_before_disabling_fetch(self) -> None:
        interceptor = self._make_interceptor()
        monitor = interceptor._network_egress_monitor
        first_session = self._make_cdp_session()
        second_session = self._make_cdp_session()
        first_page = MagicMock(url="about:blank")
        second_page = MagicMock(url="about:blank")
        first_page.context.new_cdp_session = AsyncMock(return_value=first_session)
        second_page.context.new_cdp_session = AsyncMock(return_value=second_session)
        await interceptor.enable_for_page(first_page)
        await interceptor.enable_for_page(second_page)
        ordering: list[str] = []
        monitor.unregister_active_request_interceptor.side_effect = lambda **_: ordering.append("unregister")
        first_session.send.side_effect = lambda method, *_: ordering.append(method)
        second_session.send.side_effect = lambda method, *_: ordering.append(method)

        await interceptor.disable()

        assert ordering == ["unregister", "unregister", "Fetch.disable", "Fetch.disable"]

    @pytest.mark.asyncio
    async def test_unregister_failure_invalidates_before_fetch_disable(self) -> None:
        interceptor = self._make_interceptor()
        monitor = interceptor._network_egress_monitor
        cdp_session = self._make_cdp_session()
        page = MagicMock(url="about:blank")
        page.context.new_cdp_session = AsyncMock(return_value=cdp_session)
        await interceptor.enable_for_page(page)
        ordering: list[str] = []
        monitor.unregister_active_request_interceptor.side_effect = lambda **_: (
            ordering.append("unregister") or (_ for _ in ()).throw(RuntimeError("unregister failed"))
        )
        monitor.invalidate.side_effect = lambda: ordering.append("invalidate")
        cdp_session.send.side_effect = lambda method, *_: ordering.append(method)

        await interceptor.disable()

        assert ordering == ["unregister", "invalidate", "Fetch.disable"]

    @pytest.mark.asyncio
    async def test_fetch_disable_failure_invalidates_egress_monitor(self) -> None:
        interceptor = self._make_interceptor()
        monitor = interceptor._network_egress_monitor
        cdp_session = self._make_cdp_session()
        page = MagicMock(url="about:blank")
        page.context.new_cdp_session = AsyncMock(return_value=cdp_session)
        await interceptor.enable_for_page(page)
        monitor.invalidate.reset_mock()
        cdp_session.send.side_effect = RuntimeError("session detached")

        await interceptor.disable()

        monitor.invalidate.assert_called_once_with()
        assert interceptor._cdp_sessions == []

    @pytest.mark.asyncio
    async def test_fetch_disable_stale_interception_error_does_not_invalidate(self) -> None:
        """Normal teardown usually finds the target already closed, so Fetch.disable raising a
        stale-close error is a benign race, not a live-interception leak — it must not invalidate."""
        interceptor = self._make_interceptor()
        monitor = interceptor._network_egress_monitor
        cdp_session = self._make_cdp_session()
        page = MagicMock(url="about:blank")
        page.context.new_cdp_session = AsyncMock(return_value=cdp_session)
        await interceptor.enable_for_page(page)
        monitor.invalidate.reset_mock()
        cdp_session.send.side_effect = RuntimeError("Target closed")

        await interceptor.disable()

        monitor.invalidate.assert_not_called()
        assert interceptor._cdp_sessions == []

    @pytest.mark.asyncio
    async def test_cancelled_disable_still_unregisters_before_propagating(self) -> None:
        interceptor = self._make_interceptor()
        monitor = interceptor._network_egress_monitor
        cdp_session = self._make_cdp_session()
        page = MagicMock(url="about:blank")
        page.context.new_cdp_session = AsyncMock(return_value=cdp_session)
        await interceptor.enable_for_page(page)

        with (
            patch.object(interceptor, "_drain_tasks", new=AsyncMock(side_effect=asyncio.CancelledError)),
            pytest.raises(asyncio.CancelledError),
        ):
            await interceptor.disable()

        monitor.unregister_active_request_interceptor.assert_called_once_with(page=page, owner=interceptor)
        cdp_session.send.assert_awaited_with("Fetch.disable")

    @pytest.mark.asyncio
    async def test_cancelled_disable_waits_for_inflight_fetch_enable_cleanup(self) -> None:
        interceptor = self._make_interceptor()
        context = MagicMock()
        context._skyvern_cdp_download_interceptor = None
        page = MagicMock(url="about:blank", context=context)
        cdp_session = self._make_cdp_session()
        fetch_enable_started = asyncio.Event()
        methods: list[str] = []

        async def paused_send(method: str, *_: object) -> None:
            methods.append(method)
            if method == "Fetch.enable":
                fetch_enable_started.set()
                await asyncio.Event().wait()

        cdp_session.send.side_effect = paused_send
        context.new_cdp_session = AsyncMock(return_value=cdp_session)
        await interceptor.bind_to_context(context)
        page_listener = context.on.call_args.args[1]
        page_listener(page)
        await fetch_enable_started.wait()
        disabling = asyncio.create_task(interceptor.disable())
        await asyncio.sleep(0)
        disabling.cancel()

        with pytest.raises(asyncio.CancelledError):
            await disabling

        assert methods == ["Fetch.enable", "Fetch.disable"]
        assert not interceptor._page_enable_tasks
        assert not interceptor._accepting_cdp_handlers
        assert interceptor._cdp_sessions == []

    @pytest.mark.asyncio
    async def test_page_events_do_not_use_browser_download_admission(self) -> None:
        interceptor = self._make_interceptor(proxy_username="user", proxy_password="pass")
        cdp_session = self._make_cdp_session()
        page = MagicMock(url="about:blank")
        page.context.new_cdp_session = AsyncMock(return_value=cdp_session)

        with (
            patch.object(interceptor, "_handle_request_paused", new_callable=AsyncMock) as request_handler,
            patch.object(interceptor, "_handle_auth_required", new_callable=AsyncMock) as auth_handler,
        ):
            await interceptor.enable_for_page(page)
            listeners = {call.args[0]: call.args[1] for call in cdp_session.on.call_args_list}
            listeners["Fetch.requestPaused"]({"requestId": "request"})
            listeners["Fetch.authRequired"]({"requestId": "auth"})
            await asyncio.sleep(0)

        request_handler.assert_awaited_once_with({"requestId": "request"}, cdp_session)
        auth_handler.assert_awaited_once_with({"requestId": "auth"}, cdp_session)
        assert not interceptor._accepting_browser_downloads
        assert interceptor._browser_download_listener is None

    @pytest.mark.asyncio
    async def test_request_stage_authorizes_before_continuing(self) -> None:
        """The monitor must authorize before a request carrying browser credentials is dispatched."""
        interceptor = self._make_interceptor(proxy_username="user", proxy_password="pass")
        cdp_session = self._make_cdp_session()
        ordering: list[str] = []
        interceptor._network_egress_monitor.authorize_request.side_effect = lambda **_: (
            ordering.append("authorize") or True
        )
        cdp_session.send.side_effect = lambda *_: ordering.append("dispatch")

        event = {
            "requestId": "req-1",
            "request": {"method": "GET", "url": "https://example.com/page"},
            "resourceType": "Document",
            "frameId": "frame-1",
            # No responseStatusCode — this is a Request-stage event
        }

        await interceptor._handle_request_paused(event, cdp_session)

        assert ordering == ["authorize", "dispatch"]
        interceptor._network_egress_monitor.authorize_request.assert_called_once_with(
            method="GET",
            url="https://example.com/page",
            resource_type="document",
            frame="frame-1",
        )
        cdp_session.send.assert_called_once_with("Fetch.continueRequest", {"requestId": "req-1"})

    @pytest.mark.asyncio
    async def test_request_stage_dispatch_failure_does_not_log_url_or_exception_secret(self) -> None:
        interceptor = self._make_interceptor()
        cdp_session = self._make_cdp_session()
        cdp_session.send.side_effect = RuntimeError("transport-exception-secret")
        event = {
            "requestId": "req-secret",
            "request": {"method": "GET", "url": "https://example.com/private?sig=url-secret"},
            "resourceType": "Document",
            "frameId": "frame-1",
        }

        with capture_logs() as logs:
            await interceptor._handle_request_paused(event, cdp_session)

        serialized_logs = repr(logs)
        assert "url-secret" not in serialized_logs
        assert "private" not in serialized_logs
        assert "transport-exception-secret" not in serialized_logs

    @pytest.mark.asyncio
    async def test_unsafe_download_name_failure_does_not_log_url_secret(self) -> None:
        interceptor = self._make_interceptor()
        cdp_session = self._make_cdp_session()
        event = {
            "requestId": "download-secret",
            "request": {"method": "GET", "url": "https://example.com/private?sig=url-secret"},
            "resourceType": "Document",
            "frameId": "frame-1",
            "responseStatusCode": 200,
            "responseHeaders": [
                {"name": "content-type", "value": "application/pdf"},
                {"name": "content-disposition", "value": 'attachment; filename="../report.pdf"'},
            ],
        }

        with capture_logs() as logs:
            await interceptor._handle_request_paused(event, cdp_session)

        serialized_logs = repr(logs)
        assert "url-secret" not in serialized_logs
        assert "private" not in serialized_logs

    @pytest.mark.asyncio
    @pytest.mark.parametrize("monitor_verdict", [False, RuntimeError("monitor unavailable")])
    async def test_request_stage_denial_aborts_without_continuing(self, monitor_verdict: object) -> None:
        interceptor = self._make_interceptor()
        cdp_session = self._make_cdp_session()
        if isinstance(monitor_verdict, Exception):
            interceptor._network_egress_monitor.authorize_request.side_effect = monitor_verdict
        else:
            interceptor._network_egress_monitor.authorize_request.return_value = monitor_verdict
        event = {
            "requestId": "req-denied",
            "request": {"method": "GET", "url": "https://example.com/report.pdf"},
            "resourceType": "Document",
            "frameId": "frame-1",
        }

        await interceptor._handle_request_paused(event, cdp_session)

        cdp_session.send.assert_awaited_once_with(
            "Fetch.failRequest",
            {"requestId": "req-denied", "errorReason": "BlockedByClient"},
        )

    @pytest.mark.asyncio
    async def test_request_stage_missing_monitor_aborts_without_continuing(self) -> None:
        interceptor = self._make_interceptor()
        interceptor._network_egress_monitor = None
        cdp_session = self._make_cdp_session()
        event = {
            "requestId": "req-unenrolled",
            "request": {"method": "GET", "url": "https://example.com/report.pdf"},
            "resourceType": "Document",
            "frameId": "frame-1",
        }

        await interceptor._handle_request_paused(event, cdp_session)

        cdp_session.send.assert_awaited_once_with(
            "Fetch.failRequest",
            {"requestId": "req-unenrolled", "errorReason": "BlockedByClient"},
        )

    @pytest.mark.asyncio
    async def test_same_origin_redirect_without_fresh_slot_is_denied(self) -> None:
        interceptor = self._make_interceptor()
        interceptor._network_egress_monitor.authorize_request.return_value = False
        cdp_session = self._make_cdp_session()
        event = {
            "requestId": "redirect-hop",
            "redirectedRequestId": "initial-hop",
            "request": {"method": "GET", "url": "https://example.com/final.pdf"},
            "resourceType": "Document",
            "frameId": "frame-1",
        }

        await interceptor._handle_request_paused(event, cdp_session)

        cdp_session.send.assert_awaited_once_with(
            "Fetch.failRequest",
            {"requestId": "redirect-hop", "errorReason": "BlockedByClient"},
        )

    @pytest.mark.asyncio
    async def test_request_stage_error_does_not_retry(self) -> None:
        """Request-stage errors should not attempt recovery (no duplicate continueRequest)."""
        interceptor = self._make_interceptor(proxy_username="user", proxy_password="pass")
        cdp_session = self._make_cdp_session()

        cdp_session.send.side_effect = Exception("continueRequest failed")

        event = {
            "requestId": "req-err",
            "request": {"url": "https://example.com/page"},
            "resourceType": "Document",
            # No responseStatusCode — Request-stage event
        }

        await interceptor._handle_request_paused(event, cdp_session)

        # Only one call: the original continueRequest that failed. No recovery attempt.
        assert cdp_session.send.call_count == 1
        assert cdp_session.send.call_args.args[0] == "Fetch.continueRequest"

    @pytest.mark.asyncio
    async def test_malformed_event_missing_request_id(self) -> None:
        """Malformed event without requestId should be caught, not raise."""
        interceptor = self._make_interceptor(proxy_username="user1", proxy_password="pass1")
        cdp_session = self._make_cdp_session()

        event: dict = {
            "authChallenge": {"source": "Proxy", "origin": "http://proxy.example.com"},
            "request": {"url": "https://example.com/page"},
        }

        # Should not raise — KeyError is caught by the try/except
        await interceptor._handle_auth_required(event, cdp_session)
        cdp_session.send.assert_not_called()

    @pytest.mark.asyncio
    async def test_retry_loop_prevention(self) -> None:
        """Second auth attempt for the same requestId should CancelAuth to prevent infinite loop."""
        interceptor = self._make_interceptor(proxy_username="user1", proxy_password="pass1")
        cdp_session = self._make_cdp_session()

        event = {
            "requestId": "req-retry",
            "authChallenge": {"source": "Proxy", "origin": "http://proxy.example.com"},
            "request": {"url": "https://example.com/page"},
        }

        # First attempt: should provide credentials
        await interceptor._handle_auth_required(event, cdp_session)
        first_call = cdp_session.send.call_args
        assert first_call.args[1]["authChallengeResponse"]["response"] == "ProvideCredentials"

        cdp_session.send.reset_mock()

        # Second attempt (credentials rejected): should cancel
        await interceptor._handle_auth_required(event, cdp_session)
        second_call = cdp_session.send.call_args
        assert second_call.args[1]["authChallengeResponse"]["response"] == "CancelAuth"


class TestStaleInterceptionRace:
    """Fetch.continueRequest/continueResponse can fail with 'Invalid InterceptionId' when the
    interception is resolved/cancelled or its target detaches before our async handler responds.
    That is a benign race (SKY-11964), not an error-level failure, and retrying it is futile."""

    _MOD = "skyvern.webeye.cdp_download_interceptor"

    def _make_interceptor(self) -> CDPDownloadInterceptor:
        return _make_interceptor(output_dir="/tmp/test_downloads")

    def _make_cdp_session(self) -> MagicMock:
        session = MagicMock()
        session.send = AsyncMock()
        return session

    @staticmethod
    def _response_event() -> dict:
        return {
            "requestId": "req-1",
            "request": {"url": "https://example.com/analytics/collect"},
            "resourceType": "XHR",
            "responseStatusCode": 200,
            "responseHeaders": [{"name": "content-type", "value": "text/plain"}],
        }

    @pytest.mark.parametrize(
        ("message", "expected"),
        [
            pytest.param("Protocol error (Fetch.continueResponse): Invalid InterceptionId", True, id="invalid_id"),
            pytest.param("Protocol error (Fetch.continueRequest): Invalid InterceptionId", True, id="invalid_id_req"),
            pytest.param("Target page, context or browser has been closed", True, id="target_closed"),
            pytest.param("Session closed. Most likely the page has been closed.", True, id="session_closed"),
            pytest.param("Protocol error (Fetch.continueResponse): Some other CDP failure", False, id="other_cdp"),
            pytest.param("Connection reset by peer", False, id="generic"),
        ],
    )
    def test_is_stale_interception_error(self, message: str, expected: bool) -> None:
        assert _is_stale_interception_error(Exception(message)) is expected

    @pytest.mark.asyncio
    async def test_stale_continue_response_not_retried_or_error_logged(self) -> None:
        interceptor = self._make_interceptor()
        cdp_session = self._make_cdp_session()
        cdp_session.send.side_effect = Exception("Protocol error (Fetch.continueResponse): Invalid InterceptionId")

        with patch(f"{self._MOD}.LOG") as mock_log:
            await interceptor._handle_request_paused(self._response_event(), cdp_session)

        # Only the original continueResponse — no futile recovery retry against a dead interception.
        assert cdp_session.send.call_count == 1
        mock_log.error.assert_not_called()

    @pytest.mark.asyncio
    async def test_non_stale_response_error_still_retries_and_logs(self) -> None:
        interceptor = self._make_interceptor()
        cdp_session = self._make_cdp_session()
        cdp_session.send.side_effect = Exception("Protocol error (Fetch.continueResponse): boom")

        with patch(f"{self._MOD}.LOG") as mock_log:
            await interceptor._handle_request_paused(self._response_event(), cdp_session)

        # Original continueResponse + one recovery attempt; real failures still surface as errors.
        assert cdp_session.send.call_count == 2
        mock_log.error.assert_called_once()

    @pytest.mark.asyncio
    async def test_stale_request_stage_error_not_logged_as_error(self) -> None:
        interceptor = self._make_interceptor()
        cdp_session = self._make_cdp_session()
        cdp_session.send.side_effect = Exception("Protocol error (Fetch.continueRequest): Invalid InterceptionId")
        event = {
            "requestId": "req-2",
            "request": {"url": "https://example.com/page"},
            "resourceType": "Document",
            # No responseStatusCode — Request-stage event
        }

        with patch(f"{self._MOD}.LOG") as mock_log:
            await interceptor._handle_request_paused(event, cdp_session)

        assert cdp_session.send.call_count == 1
        mock_log.error.assert_not_called()


class TestBlobDownloadCapture:
    """Browser-initiated blob: URL downloads (e.g. a page that builds the file client-side and
    triggers a blob download) must be read back via SkyvernFrame and saved, not dropped."""

    _READ_BLOB = "skyvern.webeye.cdp_download_interceptor.SkyvernFrame.read_blob_url_bytes"

    @staticmethod
    def _context(num_pages: int = 1) -> MagicMock:
        context = MagicMock()
        context.pages = [MagicMock() for _ in range(num_pages)]
        return context

    @pytest.mark.asyncio
    async def test_blob_download_read_and_saved(self, tmp_path: Path) -> None:
        interceptor = _make_interceptor(output_dir=str(tmp_path))
        interceptor._browser_context = self._context()
        pdf_bytes = b"%PDF-1.4 fake blob invoice bytes"

        with patch(self._READ_BLOB, new=AsyncMock(return_value=pdf_bytes)):
            await interceptor._handle_browser_download(
                {"url": "blob:https://example.com/abc-123", "suggestedFilename": "invoice.pdf"}
            )

        saved = list(tmp_path.iterdir())
        assert len(saved) == 1
        assert saved[0].name == "invoice.pdf"
        assert saved[0].read_bytes() == pdf_bytes

    @pytest.mark.asyncio
    async def test_blob_download_rejects_destination_symlink(self, tmp_path: Path) -> None:
        interceptor = _make_interceptor(output_dir=str(tmp_path))
        interceptor._browser_context = self._context()
        save_path, _ = interceptor._resolve_save_path("invoice.pdf")
        outside = tmp_path.parent / f"{tmp_path.name}-blob-outside.pdf"
        outside.write_bytes(b"outside")
        save_path.symlink_to(outside)

        with patch(self._READ_BLOB, new=AsyncMock(return_value=b"private blob")):
            await interceptor._handle_browser_download(
                {"url": "blob:https://example.com/confined", "suggestedFilename": "invoice.pdf"}
            )

        assert save_path.is_symlink()
        assert outside.read_bytes() == b"outside"

    @pytest.mark.asyncio
    async def test_blob_download_falls_through_pages_until_readable(self, tmp_path: Path) -> None:
        interceptor = _make_interceptor(output_dir=str(tmp_path))
        interceptor._browser_context = self._context(num_pages=2)
        pdf_bytes = b"%PDF blob"
        read = AsyncMock(side_effect=[None, pdf_bytes])

        with patch(self._READ_BLOB, new=read):
            await interceptor._handle_browser_download(
                {"url": "blob:https://example.com/xyz", "suggestedFilename": "bill.pdf"}
            )

        saved = list(tmp_path.iterdir())
        assert len(saved) == 1
        assert saved[0].read_bytes() == pdf_bytes
        assert read.await_count == 2

    @pytest.mark.asyncio
    async def test_blob_download_threads_max_size_and_guards_oversize(self, tmp_path: Path) -> None:
        interceptor = _make_interceptor(output_dir=str(tmp_path))
        interceptor._browser_context = self._context()
        read = AsyncMock(return_value=b"x" * 2048)  # exceeds the patched limit (defense-in-depth)

        with patch.object(mod, "MAX_FILE_SIZE_BYTES", 1024), patch(self._READ_BLOB, new=read):
            await interceptor._handle_browser_download(
                {"url": "blob:https://example.com/big", "suggestedFilename": "huge.pdf"}
            )

        assert list(tmp_path.iterdir()) == []
        # the in-page size limit is threaded to the shared reader, and probe mode quiets the
        # per-page fallback so non-owning pages don't spam ERROR logs
        assert read.await_args.kwargs["max_size_bytes"] == 1024
        assert read.await_args.kwargs["probe"] is True

    @pytest.mark.asyncio
    async def test_blob_download_unreadable_is_noop(self, tmp_path: Path) -> None:
        interceptor = _make_interceptor(output_dir=str(tmp_path))
        interceptor._browser_context = self._context()

        with patch(self._READ_BLOB, new=AsyncMock(return_value=None)):
            await interceptor._handle_browser_download(
                {"url": "blob:https://example.com/gone", "suggestedFilename": "x.pdf"}
            )

        assert list(tmp_path.iterdir()) == []

    @pytest.mark.asyncio
    async def test_blob_download_saves_distinct_file_with_identical_bytes(self, tmp_path: Path) -> None:
        # Two independent downloads can share bytes but differ by name — the second must not be
        # dropped just because matching bytes already exist on disk.
        interceptor = _make_interceptor(output_dir=str(tmp_path))
        interceptor._browser_context = self._context()
        pdf_bytes = b"%PDF identical bytes, different download"
        (tmp_path / "prior.pdf").write_bytes(pdf_bytes)

        with patch(self._READ_BLOB, new=AsyncMock(return_value=pdf_bytes)):
            await interceptor._handle_browser_download(
                {"url": "blob:https://example.com/second", "suggestedFilename": "invoice.pdf"}
            )

        names = sorted(p.name for p in tmp_path.iterdir())
        assert names == ["invoice.pdf", "prior.pdf"]

    @pytest.mark.asyncio
    async def test_blob_download_no_context_is_noop(self, tmp_path: Path) -> None:
        interceptor = _make_interceptor(output_dir=str(tmp_path))
        with patch(self._READ_BLOB, new=AsyncMock()) as read:
            await interceptor._handle_browser_download(
                {"url": "blob:https://example.com/none", "suggestedFilename": "x.pdf"}
            )
        read.assert_not_awaited()
        assert list(tmp_path.iterdir()) == []

    @pytest.mark.asyncio
    async def test_blob_download_already_captured_via_fetch_is_skipped(self, tmp_path: Path) -> None:
        interceptor = _make_interceptor(output_dir=str(tmp_path))
        url = "blob:https://example.com/dup"
        interceptor._downloaded_urls.add(url)
        interceptor._browser_context = self._context()

        with patch(self._READ_BLOB, new=AsyncMock()) as read:
            await interceptor._handle_browser_download({"url": url, "suggestedFilename": "x.pdf"})

        read.assert_not_awaited()
        assert list(tmp_path.iterdir()) == []

    @pytest.mark.asyncio
    async def test_empty_blob_does_not_clobber_captured_artifact(self, tmp_path: Path) -> None:
        # A large-response empty-body fulfill can make page JS re-emit a 0-byte blob with the same
        # suggestedFilename as a just-captured real download. Since _resolve_save_path overwrites on
        # collision, an empty blob must never be persisted — else it truncates the real artifact.
        interceptor = _make_interceptor(output_dir=str(tmp_path))
        interceptor._browser_context = self._context()
        real_bytes = b"%PDF-1.4 real captured document bytes"
        (tmp_path / "report.pdf").write_bytes(real_bytes)

        with patch(self._READ_BLOB, new=AsyncMock(return_value=b"")):
            await interceptor._handle_browser_download(
                {"url": "blob:https://example.com/empty", "suggestedFilename": "report.pdf"}
            )

        assert (tmp_path / "report.pdf").read_bytes() == real_bytes
        assert [p.name for p in tmp_path.iterdir()] == ["report.pdf"]

    @pytest.mark.asyncio
    async def test_empty_blob_writes_no_artifact(self, tmp_path: Path) -> None:
        # A genuinely empty blob is not a useful download and is skipped (no 0-byte file created).
        interceptor = _make_interceptor(output_dir=str(tmp_path))
        interceptor._browser_context = self._context()

        with patch(self._READ_BLOB, new=AsyncMock(return_value=b"")):
            await interceptor._handle_browser_download(
                {"url": "blob:https://example.com/empty2", "suggestedFilename": "x.pdf"}
            )

        assert list(tmp_path.iterdir()) == []


class TestDataUrlDownloadCapture:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("url", "expected_filename", "expected_bytes"),
        [
            pytest.param("data:application/pdf;base64,JVBERi0xLjQK", "report.pdf", b"%PDF-1.4\n", id="base64"),
            pytest.param("data:application/octet-stream;base64,%2Bw==", "report", b"\xfb", id="escaped_base64"),
            pytest.param("data:text/csv,name%2Cvalue%0Aone%2C1", "report", b"name,value\none,1", id="percent_encoded"),
            pytest.param(
                "data:application/pdf;charset=utf-8;base64,JVBERg==",
                "report.pdf",
                b"%PDF",
                id="media_type_parameter",
            ),
            pytest.param("data:application/x'foo*`|~;p'k*`|~=v,ok", "report", b"ok", id="rfc_token_characters"),
        ],
    )
    async def test_data_url_download_saved(
        self, tmp_path: Path, url: str, expected_filename: str, expected_bytes: bytes
    ) -> None:
        interceptor = _make_interceptor(output_dir=str(tmp_path))

        await interceptor._handle_browser_download({"url": url, "suggestedFilename": "report"})

        saved = list(tmp_path.iterdir())
        assert len(saved) == 1
        assert saved[0].name == expected_filename
        assert saved[0].read_bytes() == expected_bytes
        assert url not in interceptor._downloaded_urls
        assert len(interceptor._downloaded_urls) == 1
        dedupe_key = next(iter(interceptor._downloaded_urls))
        assert dedupe_key.startswith("data:sha256:")
        assert len(dedupe_key) == len("data:sha256:") + 64

    @pytest.mark.asyncio
    async def test_data_url_rejects_non_basename_filename(self, tmp_path: Path) -> None:
        interceptor = _make_interceptor(output_dir=str(tmp_path))

        await interceptor._handle_browser_download(
            {"url": "data:application/pdf;base64,JVBERg==", "suggestedFilename": "../../report.pdf"}
        )

        assert list(tmp_path.iterdir()) == []
        assert not (tmp_path.parent / "report.pdf").exists()

    @pytest.mark.asyncio
    async def test_data_url_rejects_destination_symlink(self, tmp_path: Path) -> None:
        interceptor = _make_interceptor(output_dir=str(tmp_path))
        save_path, _ = interceptor._resolve_save_path("report.pdf")
        outside = tmp_path.parent / f"{tmp_path.name}-data-outside.pdf"
        outside.write_bytes(b"outside")
        save_path.symlink_to(outside)

        await interceptor._handle_browser_download(
            {"url": "data:application/pdf;base64,JVBERg==", "suggestedFilename": "report.pdf"}
        )

        assert save_path.is_symlink()
        assert outside.read_bytes() == b"outside"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "url",
        [
            pytest.param("data:application/pdf;base64,not-valid-@@", id="malformed_base64"),
            pytest.param("data:text/plain,bad%2payload", id="malformed_percent_encoding"),
            pytest.param("data:application/pdf;base64,", id="empty_payload"),
            pytest.param("data:application/pdf", id="missing_comma"),
            pytest.param("data:application/pdf;base64;charset=x,JVBERg==", id="misordered_base64_metadata"),
            pytest.param("data:application/pdf;base64;base64,JVBERg==", id="duplicate_base64_metadata"),
            pytest.param("data:application/pdf;invalid,JVBERg==", id="bare_metadata_token"),
        ],
    )
    async def test_malformed_data_url_does_not_create_artifact(self, tmp_path: Path, url: str) -> None:
        interceptor = _make_interceptor(output_dir=str(tmp_path))

        await interceptor._handle_browser_download({"url": url, "suggestedFilename": "report.pdf"})

        assert list(tmp_path.iterdir()) == []
        assert url not in interceptor._downloaded_urls

    @pytest.mark.asyncio
    async def test_duplicate_data_url_event_is_saved_once(self, tmp_path: Path) -> None:
        interceptor = _make_interceptor(output_dir=str(tmp_path))
        event = {"url": "data:text/plain,hello", "suggestedFilename": "note.txt"}

        await interceptor._handle_browser_download(event)
        await interceptor._handle_browser_download(event)

        assert [path.name for path in tmp_path.iterdir()] == ["note.txt"]
        assert (tmp_path / "note.txt").read_bytes() == b"hello"

    @pytest.mark.asyncio
    async def test_duplicate_data_url_logs_never_include_payload(self, tmp_path: Path) -> None:
        interceptor = _make_interceptor(output_dir=str(tmp_path))
        payload = "private-inline-payload"
        event = {"url": f"data:text/plain,{payload}", "suggestedFilename": "note.txt"}

        with capture_logs() as logs:
            await interceptor._handle_browser_download(event)
            await interceptor._handle_browser_download(event)

        assert payload not in repr(logs)

    @pytest.mark.asyncio
    async def test_non_base64_oversize_rejected_before_percent_decode(self, tmp_path: Path) -> None:
        interceptor = _make_interceptor(output_dir=str(tmp_path))
        with (
            patch.object(mod, "MAX_FILE_SIZE_BYTES", 4),
            patch.object(mod, "_percent_decode_payload", wraps=mod._percent_decode_payload) as decode,
        ):
            await interceptor._handle_browser_download(
                {"url": "data:text/plain,abcde", "suggestedFilename": "large.txt"}
            )

        decode.assert_not_called()
        assert list(tmp_path.iterdir()) == []

    @pytest.mark.asyncio
    async def test_concurrent_duplicate_is_reserved_and_failure_can_retry(self, tmp_path: Path) -> None:
        interceptor = _make_interceptor(output_dir=str(tmp_path))
        event = {"url": "data:text/plain,hello", "suggestedFilename": "note.txt"}
        started = asyncio.Event()
        release = asyncio.Event()
        real_to_thread = asyncio.to_thread

        async def paused_to_thread(function: Any, *args: Any) -> object:
            started.set()
            await asyncio.wait_for(release.wait(), timeout=2)
            return await real_to_thread(function, *args)

        with patch("skyvern.webeye.cdp_download_interceptor.asyncio.to_thread", new=paused_to_thread):
            first = asyncio.create_task(interceptor._handle_browser_download(event))
            await asyncio.wait_for(started.wait(), timeout=0.5)
            duplicate = asyncio.create_task(interceptor._handle_browser_download(event))
            await asyncio.sleep(0)
            assert not duplicate.done()
            release.set()
            await asyncio.gather(first, duplicate)

        assert [path.name for path in tmp_path.iterdir()] == ["note.txt"]

        retry_interceptor = _make_interceptor(output_dir=str(tmp_path / "retry"))
        with patch.object(retry_interceptor, "_decode_data_url", side_effect=OSError("transient")):
            await retry_interceptor._handle_browser_download(event)
        await retry_interceptor._handle_browser_download(event)

        assert (tmp_path / "retry" / "note.txt").read_bytes() == b"hello"
        assert len(retry_interceptor._downloaded_urls) == 1

    @pytest.mark.asyncio
    async def test_data_url_over_size_limit_does_not_create_artifact(self, tmp_path: Path) -> None:
        interceptor = _make_interceptor(output_dir=str(tmp_path))
        url = "data:application/octet-stream;base64,eHh4eHg="

        with patch.object(mod, "MAX_FILE_SIZE_BYTES", 4):
            await interceptor._handle_browser_download({"url": url, "suggestedFilename": "large.bin"})

        assert list(tmp_path.iterdir()) == []
        assert url not in interceptor._downloaded_urls

    @staticmethod
    def _paused_publication() -> tuple[threading.Event, threading.Event, Any]:
        entered, release = threading.Event(), threading.Event()
        real_link = mod.os.link

        def link(source: str, destination: str, **kwargs: Any) -> None:
            entered.set()
            assert release.wait(timeout=2)
            real_link(source, destination, **kwargs)

        return entered, release, patch.object(mod.os, "link", side_effect=link)

    @pytest.mark.asyncio
    async def test_data_url_is_atomically_published_from_incomplete_path(self, tmp_path: Path) -> None:
        interceptor = _make_interceptor(output_dir=str(tmp_path))
        entered_publication, release_publication, publication_patch = self._paused_publication()
        with publication_patch:
            task = asyncio.create_task(
                interceptor._handle_browser_download(
                    {"url": "data:text/plain,complete", "suggestedFilename": "note.txt"}
                )
            )
            assert await asyncio.to_thread(entered_publication.wait, 2)
            visible = list(tmp_path.iterdir())
            assert len(visible) == 1
            assert visible[0].name.startswith("note.txt.")
            assert visible[0].name.endswith(".crdownload")
            assert not (tmp_path / "note.txt").exists()
            release_publication.set()
            await asyncio.wait_for(task, timeout=2)

        assert [path.name for path in tmp_path.iterdir()] == ["note.txt"]

    @pytest.mark.asyncio
    async def test_cancellation_drains_publication_before_retry(self, tmp_path: Path) -> None:
        interceptor = _make_interceptor(output_dir=str(tmp_path))
        event = {"url": "data:text/plain,complete", "suggestedFilename": "note.txt"}
        entered_publication, release_publication, publication_patch = self._paused_publication()
        with publication_patch:
            first = asyncio.create_task(interceptor._handle_browser_download(event))
            assert await asyncio.to_thread(entered_publication.wait, 2)
            first.cancel()
            retry = asyncio.create_task(interceptor._handle_browser_download(event))
            await asyncio.sleep(0)
            assert not retry.done()
            visible = list(tmp_path.iterdir())
            assert len(visible) == 1
            assert visible[0].name.endswith(".crdownload")
            release_publication.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(first, timeout=2)
            await asyncio.wait_for(retry, timeout=2)

        assert (tmp_path / "note.txt").read_bytes() == b"complete"
        assert not list(tmp_path.glob("*.crdownload"))
        assert len(interceptor._downloaded_urls) == 1

    @pytest.mark.asyncio
    async def test_concurrent_distinct_data_urls_with_same_filename_fail_closed_on_collision(
        self, tmp_path: Path
    ) -> None:
        interceptor = _make_interceptor(output_dir=str(tmp_path))
        first = {"url": "data:text/plain,first", "suggestedFilename": "note.txt"}
        second = {"url": "data:text/plain,second", "suggestedFilename": "note.txt"}

        await asyncio.wait_for(
            asyncio.gather(
                interceptor._handle_browser_download(first),
                interceptor._handle_browser_download(second),
            ),
            timeout=2,
        )

        assert (tmp_path / "note.txt").read_bytes() in {b"first", b"second"}
        assert len(interceptor._downloaded_urls) == 1
        assert not list(tmp_path.glob("*.crdownload"))

    @pytest.mark.asyncio
    async def test_digest_is_off_loop_and_invalid_shape_rejected_before_digest(self, tmp_path: Path) -> None:
        interceptor = _make_interceptor(output_dir=str(tmp_path))
        event_loop_thread = threading.get_ident()

        real_identity = mod._download_identity
        digest_threads: list[int] = []

        def recording_identity(url: str) -> str:
            digest_threads.append(threading.get_ident())
            return real_identity(url)

        with patch.object(mod, "_download_identity", side_effect=recording_identity) as identity:
            await interceptor._handle_browser_download(
                {"url": "data:text/plain,valid", "suggestedFilename": "valid.txt"}
            )
            assert digest_threads and digest_threads[0] != event_loop_thread

            identity.reset_mock()
            await interceptor._handle_browser_download(
                {"url": "data:" + "x" * (mod._DATA_URL_MAX_METADATA_LENGTH + 1), "suggestedFilename": "bad.txt"}
            )
            identity.assert_not_called()

    @pytest.mark.asyncio
    async def test_disable_drains_active_browser_download_task(self, tmp_path: Path) -> None:
        interceptor = _make_interceptor(output_dir=str(tmp_path))
        browser_session = MagicMock()
        browser_session.send = AsyncMock()
        browser_session.detach = AsyncMock()
        browser = MagicMock()
        browser.new_browser_cdp_session = AsyncMock(return_value=browser_session)
        browser_context = MagicMock()
        started = asyncio.Event()
        release = asyncio.Event()

        async def paused_handler(event: dict[str, Any]) -> None:
            started.set()
            await release.wait()

        await interceptor.enable_browser_download_monitor(browser, browser_context)
        download_listener = browser_session.on.call_args.args[1]
        with patch.object(interceptor, "_handle_browser_download", side_effect=paused_handler):
            download_listener({"url": "data:text/plain,x"})
            await asyncio.wait_for(started.wait(), timeout=0.5)
            disabling = asyncio.create_task(interceptor.disable())
            await asyncio.sleep(0)
            assert not disabling.done()
            release.set()
            await asyncio.wait_for(disabling, timeout=2)

        assert not interceptor._browser_download_tasks
        assert not interceptor._accepting_browser_downloads
        browser_session.remove_listener.assert_called_once_with("Browser.downloadWillBegin", download_listener)

    @pytest.mark.asyncio
    async def test_settle_browser_downloads_includes_event_admitted_while_draining(self, tmp_path: Path) -> None:
        interceptor = _make_interceptor(output_dir=str(tmp_path))
        interceptor._accepting_browser_downloads = True
        first_started = asyncio.Event()
        release_first = asyncio.Event()
        handled_urls: list[str] = []

        async def paused_handler(event: dict[str, Any]) -> None:
            if not handled_urls:
                first_started.set()
                await release_first.wait()
            handled_urls.append(event["url"])

        with patch.object(interceptor, "_handle_browser_download", side_effect=paused_handler):
            interceptor._schedule_browser_download_handler({"url": "data:text/plain,ready"})
            await first_started.wait()
            entered = asyncio.Event()

            async def collect() -> None:
                async with interceptor.settle_browser_downloads():
                    entered.set()
                    assert set(handled_urls) == {"data:text/plain,ready", "data:text/plain,late"}

            collecting = asyncio.create_task(collect())
            await asyncio.sleep(0)
            assert not entered.is_set()
            interceptor._schedule_browser_download_handler({"url": "data:text/plain,late"})
            release_first.set()
            await asyncio.wait_for(collecting, timeout=2)

        assert interceptor._accepting_browser_downloads
        assert not interceptor._browser_download_tasks

    @pytest.mark.asyncio
    async def test_settle_browser_downloads_drains_event_admitted_inside_context(self, tmp_path: Path) -> None:
        interceptor = _make_interceptor(output_dir=str(tmp_path))
        interceptor._accepting_browser_downloads = True
        handler_started = asyncio.Event()
        release_handler = asyncio.Event()

        async def paused_handler(event: dict[str, Any]) -> None:
            handler_started.set()
            await release_handler.wait()
            (tmp_path / "late.txt").write_text(event["url"])

        async def collect() -> None:
            async with interceptor.settle_browser_downloads():
                interceptor._schedule_browser_download_handler({"url": "data:text/plain,late"})
                await handler_started.wait()

        with patch.object(interceptor, "_handle_browser_download", side_effect=paused_handler):
            collecting = asyncio.create_task(collect())
            await handler_started.wait()
            await asyncio.sleep(0)
            assert not collecting.done()
            release_handler.set()
            await asyncio.wait_for(collecting, timeout=2)

        assert (tmp_path / "late.txt").read_text() == "data:text/plain,late"
        assert not interceptor._browser_download_tasks

    @pytest.mark.asyncio
    async def test_cancelled_settle_does_not_poison_reused_interceptor(self) -> None:
        interceptor = _make_interceptor()
        interceptor._accepting_browser_downloads = True
        first_started = asyncio.Event()
        never_release = asyncio.Event()
        second_handled = asyncio.Event()

        async def paused_handler(event: dict[str, Any]) -> None:
            if event["url"].endswith("first"):
                first_started.set()
                await never_release.wait()
            else:
                second_handled.set()

        with patch.object(interceptor, "_handle_browser_download", side_effect=paused_handler):
            interceptor._schedule_browser_download_handler({"url": "data:text/plain,first"})
            await first_started.wait()

            async def settle() -> None:
                async with interceptor.settle_browser_downloads():
                    pass

            settling = asyncio.create_task(settle())
            await asyncio.sleep(0)
            settling.cancel()
            with pytest.raises(asyncio.CancelledError):
                await settling

            interceptor._schedule_browser_download_handler({"url": "data:text/plain,second"})
            await asyncio.wait_for(second_handled.wait(), timeout=2)

        assert interceptor._accepting_browser_downloads

    @pytest.mark.asyncio
    async def test_cancelled_settle_body_cancels_admitted_handler_and_remains_reusable(self) -> None:
        interceptor = _make_interceptor()
        interceptor._accepting_browser_downloads = True
        first_started = asyncio.Event()
        second_handled = asyncio.Event()

        async def paused_handler(event: dict[str, Any]) -> None:
            if event["url"].endswith("first"):
                first_started.set()
                await asyncio.Event().wait()
            else:
                second_handled.set()

        async def settle() -> None:
            async with interceptor.settle_browser_downloads():
                interceptor._schedule_browser_download_handler({"url": "data:text/plain,first"})
                await first_started.wait()
                await asyncio.Event().wait()

        with patch.object(interceptor, "_handle_browser_download", side_effect=paused_handler):
            settling = asyncio.create_task(settle())
            await first_started.wait()
            settling.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(settling, timeout=2)

            assert not interceptor._browser_download_tasks
            assert not interceptor._browser_download_monitor_lock.locked()
            interceptor._schedule_browser_download_handler({"url": "data:text/plain,second"})
            await asyncio.wait_for(second_handled.wait(), timeout=2)

        assert interceptor._accepting_browser_downloads

    def test_maximum_size_percent_encoded_payload_is_accepted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        interceptor = _make_interceptor()
        monkeypatch.setattr(mod, "MAX_FILE_SIZE_BYTES", 12)
        url = "data:text/plain," + "%41" * 12

        comma_index = mod._bounded_data_url_comma(url)
        _, _, data = interceptor._decode_data_url(url, comma_index)

        assert data == b"A" * 12

    def test_percent_escaped_base64_payload_is_accepted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        interceptor = _make_interceptor()
        monkeypatch.setattr(mod, "MAX_FILE_SIZE_BYTES", 1)
        url = "data:text/plain;base64,%51%51%3D%3D"

        comma_index = mod._bounded_data_url_comma(url)
        _, _, data = interceptor._decode_data_url(url, comma_index)

        assert data == b"A"

    def test_maximum_size_percent_escaped_base64_payload_is_accepted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        interceptor = _make_interceptor()
        monkeypatch.setattr(mod, "MAX_FILE_SIZE_BYTES", 4)
        url = "data:text/plain;base64," + "".join(f"%{byte:02X}" for byte in b"QUJDRA==")

        comma_index = mod._bounded_data_url_comma(url)
        _, _, data = interceptor._decode_data_url(url, comma_index)

        assert data == b"ABCD"

    def test_percent_escaped_base64_decoded_overflow_is_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        interceptor = _make_interceptor()
        monkeypatch.setattr(mod, "MAX_FILE_SIZE_BYTES", 4)
        url = "data:text/plain;base64," + "".join(f"%{byte:02X}" for byte in b"QUJDREU=")

        comma_index = mod._bounded_data_url_comma(url)
        with pytest.raises(ValueError, match="decoded payload exceeds size limit"):
            interceptor._decode_data_url(url, comma_index)

    @pytest.mark.parametrize(
        "payload",
        [
            pytest.param("%51%51%3", id="malformed_escape"),
            pytest.param("%51%51%40%40", id="malformed_base64"),
        ],
    )
    def test_malformed_percent_escaped_base64_is_rejected(self, monkeypatch: pytest.MonkeyPatch, payload: str) -> None:
        interceptor = _make_interceptor()
        monkeypatch.setattr(mod, "MAX_FILE_SIZE_BYTES", 4)
        url = f"data:text/plain;base64,{payload}"

        comma_index = mod._bounded_data_url_comma(url)
        with pytest.raises(ValueError):
            interceptor._decode_data_url(url, comma_index)

    def test_oversized_percent_escaped_base64_rejected_before_decode_allocation(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(mod, "MAX_FILE_SIZE_BYTES", 4)
        url = "data:text/plain;base64," + "%51" * 9

        with patch.object(mod, "_percent_decoded_payload_length") as decoded_length:
            with pytest.raises(ValueError, match="encoded payload exceeds size limit"):
                mod._bounded_data_url_comma(url)

        decoded_length.assert_not_called()

    def test_oversized_percent_payload_rejected_before_decode_allocation(self, monkeypatch: pytest.MonkeyPatch) -> None:
        interceptor = _make_interceptor()
        monkeypatch.setattr(mod, "MAX_FILE_SIZE_BYTES", 12)
        url = "data:text/plain," + "A" * 12 + "%41"

        with patch.object(mod, "_percent_decode_payload") as decode:
            comma_index = mod._bounded_data_url_comma(url)
            with pytest.raises(ValueError, match="decoded payload exceeds size limit"):
                interceptor._decode_data_url(url, comma_index)

        decode.assert_not_called()

    @pytest.mark.asyncio
    async def test_disable_waits_for_racing_browser_monitor_enable(self) -> None:
        interceptor = _make_interceptor()
        send_started = asyncio.Event()
        release_send = asyncio.Event()
        browser_session = MagicMock()

        async def suspended_send(method: str, params: dict[str, Any]) -> None:
            send_started.set()
            await release_send.wait()

        browser_session.send = AsyncMock(side_effect=suspended_send)
        browser_session.detach = AsyncMock()
        browser = MagicMock()
        browser.new_browser_cdp_session = AsyncMock(return_value=browser_session)

        enabling = asyncio.create_task(interceptor.enable_browser_download_monitor(browser, MagicMock()))
        await send_started.wait()
        disabling = asyncio.create_task(interceptor.disable())
        await asyncio.sleep(0)
        assert not disabling.done()

        release_send.set()
        await asyncio.wait_for(asyncio.gather(enabling, disabling), timeout=2)

        assert interceptor._browser_session is None
        assert interceptor._browser_context is None
        assert interceptor._browser_download_listener is None
        assert not interceptor._accepting_browser_downloads
        browser_session.detach.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_browser_monitor_can_reenable_after_disable(self) -> None:
        interceptor = _make_interceptor()
        first_session = MagicMock(send=AsyncMock(), detach=AsyncMock())
        second_session = MagicMock(send=AsyncMock(), detach=AsyncMock())
        browser = MagicMock()
        browser.new_browser_cdp_session = AsyncMock(side_effect=[first_session, second_session])
        browser_context = MagicMock()

        await interceptor.enable_browser_download_monitor(browser, browser_context)
        await interceptor.disable()
        await interceptor.enable_browser_download_monitor(browser, browser_context)

        assert interceptor._browser_session is second_session
        assert interceptor._browser_context is browser_context
        assert interceptor._browser_download_listener is second_session.on.call_args.args[1]
        assert interceptor._accepting_browser_downloads

        await asyncio.wait_for(interceptor.disable(), timeout=2)
        assert not interceptor._browser_download_tasks
        assert not interceptor._accepting_browser_downloads

    @pytest.mark.asyncio
    async def test_context_binding_owns_new_page_listener_and_tasks(self) -> None:
        interceptor = _make_interceptor()
        context = MagicMock()
        context._skyvern_cdp_download_interceptor = None
        page = MagicMock()
        started = asyncio.Event()
        release = asyncio.Event()

        async def paused_enable(new_page: Any) -> None:
            assert new_page is page
            started.set()
            await asyncio.wait_for(release.wait(), timeout=2)

        with patch.object(interceptor, "enable_for_page", side_effect=paused_enable):
            await interceptor.bind_to_context(context)
            page_listener = context.on.call_args.args[1]
            page_listener(page)
            await asyncio.wait_for(started.wait(), timeout=0.5)
            disabling = asyncio.create_task(interceptor.disable())
            await asyncio.sleep(0)
            assert not disabling.done()
            release.set()
            await asyncio.wait_for(disabling, timeout=2)

        context.remove_listener.assert_called_once_with("page", page_listener)
        assert not interceptor._page_enable_tasks
        assert context._skyvern_cdp_download_interceptor is None

    @pytest.mark.asyncio
    async def test_context_rebind_detaches_cancellation_resistant_disable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(mod, "BROWSER_INTERCEPTOR_DISABLE_TIMEOUT", 0.01)
        old_interceptor = _make_interceptor()
        new_interceptor = _make_interceptor()
        context = MagicMock()
        context._skyvern_cdp_download_interceptor = old_interceptor
        entered_cancel = asyncio.Event()
        release = asyncio.Event()

        async def stuck_disable() -> None:
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                entered_cancel.set()
                await asyncio.wait_for(release.wait(), timeout=2)
                if context._skyvern_cdp_download_interceptor is old_interceptor:
                    context._skyvern_cdp_download_interceptor = None
                raise RuntimeError("disable failed after detach")

        old_interceptor.disable = stuck_disable  # type: ignore[method-assign]
        unretrieved: list[dict[str, Any]] = []
        loop = asyncio.get_running_loop()
        previous_handler = loop.get_exception_handler()
        loop.set_exception_handler(lambda _loop, context_: unretrieved.append(context_))
        with capture_logs() as logs:
            try:
                await asyncio.wait_for(new_interceptor.bind_to_context(context), timeout=0.5)
                await asyncio.wait_for(entered_cancel.wait(), timeout=0.5)

                assert context._skyvern_cdp_download_interceptor is new_interceptor
                assert context.on.call_count == 1
                assert len(mod._DETACHED_DISABLE_TASKS) == 1

                release.set()
                callback_finished = asyncio.Event()
                next(iter(mod._DETACHED_DISABLE_TASKS)).add_done_callback(lambda _: callback_finished.set())
                await asyncio.wait_for(callback_finished.wait(), timeout=0.5)
                await asyncio.sleep(0)

                assert mod._DETACHED_DISABLE_TASKS == set()
                assert context._skyvern_cdp_download_interceptor is new_interceptor
            finally:
                loop.set_exception_handler(previous_handler)

        assert not any("never retrieved" in str(item.get("message", "")) for item in unretrieved)
        matching_logs = [
            log for log in logs if log.get("event") == "Previous CDP download interceptor disable failed after detach"
        ]
        assert matching_logs == [
            {
                "error_type": "RuntimeError",
                "event": "Previous CDP download interceptor disable failed after detach",
                "log_level": "warning",
            }
        ]

    @pytest.mark.asyncio
    async def test_context_rebind_awaits_fast_disable_before_binding(self) -> None:
        old_interceptor = _make_interceptor()
        new_interceptor = _make_interceptor()
        context = MagicMock()
        context._skyvern_cdp_download_interceptor = old_interceptor
        disabled = False

        async def fast_disable() -> None:
            nonlocal disabled
            disabled = True

        old_interceptor.disable = fast_disable  # type: ignore[method-assign]
        await asyncio.wait_for(new_interceptor.bind_to_context(context), timeout=0.5)

        assert disabled
        assert context._skyvern_cdp_download_interceptor is new_interceptor
        context.on.assert_called_once()
        assert mod._DETACHED_DISABLE_TASKS == set()

    @pytest.mark.asyncio
    async def test_context_rebind_propagates_fast_disable_failure(self) -> None:
        old_interceptor = _make_interceptor()
        new_interceptor = _make_interceptor()
        context = MagicMock()
        context._skyvern_cdp_download_interceptor = old_interceptor
        old_interceptor.disable = AsyncMock(side_effect=RuntimeError("disable failed"))  # type: ignore[method-assign]

        with pytest.raises(RuntimeError, match="disable failed"):
            await asyncio.wait_for(new_interceptor.bind_to_context(context), timeout=0.5)

        assert context._skyvern_cdp_download_interceptor is old_interceptor
        context.on.assert_not_called()
        assert mod._DETACHED_DISABLE_TASKS == set()

    @pytest.mark.asyncio
    async def test_context_rebind_cancellation_owns_old_disable_task(self) -> None:
        old_interceptor = _make_interceptor()
        new_interceptor = _make_interceptor()
        context = MagicMock()
        context._skyvern_cdp_download_interceptor = old_interceptor
        started = asyncio.Event()
        entered_cancel = asyncio.Event()
        release = asyncio.Event()

        async def stuck_disable() -> None:
            started.set()
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                entered_cancel.set()
                await asyncio.wait_for(release.wait(), timeout=2)

        old_interceptor.disable = stuck_disable  # type: ignore[method-assign]
        binding = asyncio.create_task(new_interceptor.bind_to_context(context))
        await asyncio.wait_for(started.wait(), timeout=0.5)
        binding.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(binding, timeout=0.5)

        await asyncio.wait_for(entered_cancel.wait(), timeout=0.5)
        assert len(mod._DETACHED_DISABLE_TASKS) == 1
        assert context._skyvern_cdp_download_interceptor is old_interceptor
        context.on.assert_not_called()

        release.set()
        callback_finished = asyncio.Event()
        next(iter(mod._DETACHED_DISABLE_TASKS)).add_done_callback(lambda _: callback_finished.set())
        await asyncio.wait_for(callback_finished.wait(), timeout=0.5)
        await asyncio.sleep(0)
        assert mod._DETACHED_DISABLE_TASKS == set()

    @pytest.mark.asyncio
    async def test_cancelled_rebind_detached_disable_has_external_gc_root(self) -> None:
        old_interceptor = _make_interceptor()
        context = MagicMock()
        context._skyvern_cdp_download_interceptor = old_interceptor
        started = asyncio.Event()
        entered_cancel = asyncio.Event()
        release = asyncio.Event()

        async def stuck_disable() -> None:
            started.set()
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                entered_cancel.set()
                await asyncio.wait_for(release.wait(), timeout=2)

        old_interceptor.disable = stuck_disable  # type: ignore[method-assign]
        new_interceptor = _make_interceptor()
        new_ref = weakref.ref(new_interceptor)
        binding = asyncio.create_task(new_interceptor.bind_to_context(context))
        binding_ref = weakref.ref(binding)
        await asyncio.wait_for(started.wait(), timeout=0.5)
        binding.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(binding, timeout=0.5)
        await asyncio.wait_for(entered_cancel.wait(), timeout=0.5)

        detached_ref = weakref.ref(next(iter(mod._DETACHED_DISABLE_TASKS)))
        del binding
        del new_interceptor
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        gc.collect()

        assert new_ref() is None
        assert binding_ref() is None
        assert detached_ref() is not None
        assert len(mod._DETACHED_DISABLE_TASKS) == 1

        callback_finished = asyncio.Event()
        detached_ref().add_done_callback(lambda _: callback_finished.set())  # type: ignore[union-attr]
        release.set()
        await asyncio.wait_for(callback_finished.wait(), timeout=0.5)
        await asyncio.sleep(0)
        gc.collect()

        assert mod._DETACHED_DISABLE_TASKS == set()
        assert detached_ref() is None

    @pytest.mark.asyncio
    async def test_context_binding_same_interceptor_is_idempotent(self) -> None:
        interceptor = _make_interceptor()
        context = MagicMock()
        context._skyvern_cdp_download_interceptor = None

        await asyncio.wait_for(interceptor.bind_to_context(context), timeout=0.5)
        page_listener = context.on.call_args.args[1]
        await asyncio.wait_for(interceptor.bind_to_context(context), timeout=0.5)
        await asyncio.wait_for(interceptor.disable(), timeout=0.5)

        context.on.assert_called_once_with("page", page_listener)
        context.remove_listener.assert_called_once_with("page", page_listener)
        assert context._skyvern_cdp_download_interceptor is None

    @pytest.mark.asyncio
    async def test_concurrent_context_rebinds_leave_only_last_listener(self) -> None:
        old_interceptor = _make_interceptor()
        first_interceptor = _make_interceptor()
        second_interceptor = _make_interceptor()
        context = MagicMock()
        context._skyvern_cdp_download_interceptor = old_interceptor
        old_disable_started = asyncio.Event()
        release_old_disable = asyncio.Event()
        first_disable_started = asyncio.Event()
        release_first_disable = asyncio.Event()

        async def paused_old_disable() -> None:
            old_disable_started.set()
            await asyncio.wait_for(release_old_disable.wait(), timeout=2)

        original_first_disable = first_interceptor.disable

        async def paused_first_disable() -> None:
            first_disable_started.set()
            await asyncio.wait_for(release_first_disable.wait(), timeout=2)
            await original_first_disable()

        old_interceptor.disable = paused_old_disable  # type: ignore[method-assign]
        first_interceptor.disable = paused_first_disable  # type: ignore[method-assign]

        first_binding = asyncio.create_task(first_interceptor.bind_to_context(context))
        await asyncio.wait_for(old_disable_started.wait(), timeout=0.5)
        second_binding = asyncio.create_task(second_interceptor.bind_to_context(context))
        await asyncio.sleep(0)
        assert not second_binding.done()

        release_old_disable.set()
        await asyncio.wait_for(first_disable_started.wait(), timeout=0.5)
        assert context._skyvern_cdp_download_interceptor is first_interceptor
        first_listener = context.on.call_args_list[0].args[1]

        release_first_disable.set()
        await asyncio.wait_for(asyncio.gather(first_binding, second_binding), timeout=0.5)
        second_listener = context.on.call_args_list[1].args[1]

        assert context._skyvern_cdp_download_interceptor is second_interceptor
        context.remove_listener.assert_called_once_with("page", first_listener)
        assert first_interceptor._page_listener is None
        assert not first_interceptor._accepting_pages
        assert second_interceptor._page_listener is second_listener
        assert second_interceptor._accepting_pages

    @pytest.mark.asyncio
    async def test_context_page_enable_failure_is_retrieved_and_logged(self) -> None:
        interceptor = _make_interceptor()
        context = MagicMock()
        context._skyvern_cdp_download_interceptor = None

        with (
            patch.object(interceptor, "enable_for_page", AsyncMock(side_effect=RuntimeError("sensitive detail"))),
            capture_logs() as logs,
        ):
            await interceptor.bind_to_context(context)
            page_listener = context.on.call_args.args[1]
            page_listener(MagicMock())
            await asyncio.sleep(0)
            await asyncio.sleep(0)

        matching_logs = [log for log in logs if log.get("event") == "Failed to enable CDP interception for page"]
        assert matching_logs == [
            {
                "error_type": "RuntimeError",
                "event": "Failed to enable CDP interception for page",
                "log_level": "warning",
            }
        ]
        assert not interceptor._page_enable_tasks

    @pytest.mark.asyncio
    async def test_disable_drains_admitted_fetch_handler(self) -> None:
        interceptor = _make_interceptor()
        started = asyncio.Event()
        release = asyncio.Event()
        session = MagicMock(send=AsyncMock())

        async def paused_handler(event: dict[str, Any], cdp_session: Any) -> None:
            assert cdp_session is session
            started.set()
            await release.wait()

        with patch.object(interceptor, "_handle_request_paused", side_effect=paused_handler):
            interceptor._on_request_paused({"requestId": "request-1"}, session)
            await started.wait()
            disabling = asyncio.create_task(interceptor.disable())
            await asyncio.sleep(0)
            assert not disabling.done()
            release.set()
            await asyncio.wait_for(disabling, timeout=2)

        assert not interceptor._cdp_handler_tasks


class TestDirectHttpDownloadAuthAndHtmlGuard:
    """Direct HTTP downloads fail closed without an enrolled backend and reject HTML login masquerades."""

    _URLOPEN = "urllib.request.urlopen"
    _BUILD_OPENER = "urllib.request.build_opener"

    @staticmethod
    def _context() -> MagicMock:
        context = MagicMock()
        context.cookies = AsyncMock(return_value=[])
        context.request.get = AsyncMock(side_effect=AssertionError("raw BrowserContext request bypass"))
        return context

    @staticmethod
    def _guarded_fetch(body: bytes, content_type: str, filename: str) -> AsyncMock:
        return AsyncMock(return_value=MagicMock(body=body, content_type=content_type, filename=filename))

    _LOGIN_HTML = (
        b'\n<!DOCTYPE HTML PUBLIC "-//W3C//DTD HTML 4.0 Transitional//EN">\n'
        b"<html><head><title>Login</title></head>"
        b"<body><form method='post' action='./Login.aspx'></form></body></html>"
    )

    @pytest.mark.asyncio
    async def test_failed_guarded_helper_does_not_fall_back_to_unenrolled_clients(self, tmp_path: Path) -> None:
        interceptor = _make_interceptor(output_dir=str(tmp_path))
        context = self._context()
        interceptor._browser_context = context
        guarded_fetch = AsyncMock(side_effect=RuntimeError("guarded backend unavailable"))
        urlopen = MagicMock(side_effect=AssertionError("raw urllib bypass"))
        build_opener = MagicMock(side_effect=AssertionError("raw urllib opener bypass"))

        with (
            patch.object(mod.file_api, "fetch_file_bytes", guarded_fetch, create=True),
            patch(self._URLOPEN, urlopen),
            patch(self._BUILD_OPENER, build_opener),
        ):
            await interceptor._download_url_directly("https://site.example/report.pdf", "report.pdf")

        context.request.get.assert_not_called()
        urlopen.assert_not_called()
        build_opener.assert_not_called()
        assert list(tmp_path.iterdir()) == []

    @pytest.mark.asyncio
    async def test_direct_download_uses_only_guarded_helper(self, tmp_path: Path) -> None:
        authorizer = AsyncMock()
        interceptor = _make_interceptor(output_dir=str(tmp_path))
        interceptor._redirect_hop_authorizer = authorizer
        context = MagicMock()
        context.cookies = AsyncMock(return_value=[{"name": "session", "value": "cookie-secret"}])
        context.request.get = AsyncMock(side_effect=AssertionError("raw BrowserContext request bypass"))
        interceptor._browser_context = context
        guarded_response = MagicMock(
            body=b"private report",
            content_type="application/pdf",
            filename="report.pdf",
        )
        guarded_fetch = AsyncMock(return_value=guarded_response)
        urlopen = MagicMock(side_effect=AssertionError("raw urllib bypass"))
        build_opener = MagicMock(side_effect=AssertionError("raw urllib opener bypass"))

        with (
            patch("skyvern.forge.sdk.api.files.fetch_file_bytes", guarded_fetch, create=True),
            patch(self._URLOPEN, urlopen),
            patch(self._BUILD_OPENER, build_opener),
        ):
            await interceptor._download_url_directly("https://site.example/report.pdf?sig=secret", "report.pdf")

        context.request.get.assert_not_called()
        urlopen.assert_not_called()
        build_opener.assert_not_called()
        guarded_fetch.assert_awaited_once_with(
            "https://site.example/report.pdf?sig=secret",
            max_size_mb=100,
            headers={"Cookie": "session=cookie-secret"},
            filename="report.pdf",
            authorize_request_hop=authorizer,
        )
        assert (tmp_path / "report.pdf").read_bytes() == b"private report"

    @pytest.mark.asyncio
    async def test_guarded_download_failure_does_not_log_url_or_exception_secret(self, tmp_path: Path) -> None:
        interceptor = _make_interceptor(output_dir=str(tmp_path))
        interceptor._browser_context = self._context()
        guarded_fetch = AsyncMock(side_effect=RuntimeError("exception-secret"))

        with (
            patch.object(mod.file_api, "fetch_file_bytes", guarded_fetch, create=True),
            capture_logs() as logs,
        ):
            await interceptor._handle_browser_download(
                {
                    "url": "https://site.example/protected-path?sig=url-secret",
                    "suggestedFilename": "report.pdf",
                }
            )

        serialized_logs = repr(logs)
        assert "url-secret" not in serialized_logs
        assert "protected-path" not in serialized_logs
        assert "exception-secret" not in serialized_logs
        assert any(
            log.get("event") == "Guarded direct download failed" and log.get("error_type") == "RuntimeError"
            for log in logs
        )

    @pytest.mark.asyncio
    async def test_guarded_download_failure_logs_the_raising_module(self, tmp_path: Path) -> None:
        interceptor = _make_interceptor(output_dir=str(tmp_path))
        interceptor._browser_context = self._context()

        async def guarded_fetch(*_args: Any, **_kwargs: Any) -> None:
            raise RuntimeError("exception-secret")

        with (
            patch.object(mod.file_api, "fetch_file_bytes", guarded_fetch, create=True),
            capture_logs() as logs,
        ):
            await interceptor._download_url_directly("https://site.example/report.pdf?sig=url-secret", "report.pdf")

        failures = [log for log in logs if log.get("event") == "Guarded direct download failed"]
        assert len(failures) == 1
        assert failures[0]["error_type"] == "RuntimeError"
        assert failures[0]["error_origin"].startswith(f"{__name__}:guarded_fetch:")
        serialized_logs = repr(logs)
        assert "url-secret" not in serialized_logs
        assert "exception-secret" not in serialized_logs

    @pytest.mark.asyncio
    async def test_unenrolled_hop_authorizer_is_reported_as_unenrolled(self, tmp_path: Path) -> None:
        interceptor = _make_interceptor(
            output_dir=str(tmp_path),
            redirect_hop_authorizer=deny_unenrolled_redirect_hop,
        )
        interceptor._browser_context = self._context()
        guarded_fetch = AsyncMock(side_effect=AssertionError("unenrolled authorization must not reach the fetch seam"))

        with (
            patch.object(mod.file_api, "fetch_file_bytes", guarded_fetch, create=True),
            capture_logs() as logs,
        ):
            await interceptor._download_url_directly("https://site.example/report.pdf?sig=url-secret", "report.pdf")

        guarded_fetch.assert_not_awaited()
        assert list(tmp_path.iterdir()) == []
        assert [log["event"] for log in logs] == [
            "Redirect hop authorization is unenrolled for this browser session, dropping direct download"
        ]
        assert "url-secret" not in repr(logs)

    @pytest.mark.asyncio
    async def test_cookie_header_omits_control_characters(self, tmp_path: Path) -> None:
        interceptor = _make_interceptor(output_dir=str(tmp_path))
        context = self._context()
        context.cookies.return_value = [
            {"name": "session", "value": "safe"},
            {"name": "injected\r\nHeader", "value": "bad"},
            {"name": "unsafe", "value": "bad\x00value"},
        ]
        interceptor._browser_context = context

        assert await interceptor._cookie_header_for_url("https://site.example/report.pdf") == "session=safe"

    @pytest.mark.parametrize(
        ("body", "content_type", "filename", "expected_filename"),
        [
            pytest.param(_LOGIN_HTML, "text/html", "statement.zip", None, id="binary-name"),
            pytest.param(_LOGIN_HTML, "text/html", "statement%2Ezip", None, id="encoded-binary-name"),
            pytest.param(_LOGIN_HTML, "text/html", "statement%252Ezip", None, id="double-encoded-binary-name"),
            pytest.param(_LOGIN_HTML, "application/octet-stream", "statement.zip", None, id="mislabelled-html"),
            pytest.param(_LOGIN_HTML, "application/octet-stream", "", None, id="nameless-binary-claim"),
            pytest.param(_LOGIN_HTML, "text/html", "statement", None, id="extensionless-name"),
            pytest.param(_LOGIN_HTML, "text/html", "report%2Ehtml", "report.html", id="encoded-html-name"),
            pytest.param(_LOGIN_HTML, "text/html", "report.html", "report.html", id="html-name"),
            pytest.param(_LOGIN_HTML, "text/html", "", "<generated>", id="nameless-html"),
            pytest.param(_LOGIN_HTML, "", "", "<generated>", id="nameless-no-content-type"),
            pytest.param(b"%PDF-1.7 report", "application/pdf", "invoice.pdf", "invoice.pdf", id="binary"),
            pytest.param(b"PK\x03\x04 archive", "text/html", "archive.zip", "archive.zip", id="mislabeled-binary"),
            pytest.param(
                b"<!-- generated --><!DOCTYPE html><html><body>login</body></html>",
                "application/octet-stream",
                "statement.zip",
                None,
                id="leading-comment",
            ),
            pytest.param(
                b'<?xml version="1.0"?><html><body>login</body></html>',
                "application/octet-stream",
                "statement.zip",
                None,
                id="xml-declaration",
            ),
            pytest.param(
                b"<head><title>Login</title></head><body>login</body>",
                "application/octet-stream",
                "statement.zip",
                None,
                id="omitted-html-root",
            ),
            pytest.param(
                b"<head\n><title>Login</title></head><body>login</body>",
                "application/octet-stream",
                "statement.zip",
                None,
                id="tag-newline",
            ),
        ],
    )
    @pytest.mark.asyncio
    async def test_html_masquerade_guard(
        self,
        tmp_path: Path,
        body: bytes,
        content_type: str,
        filename: str,
        expected_filename: str | None,
    ) -> None:
        interceptor = _make_interceptor(output_dir=str(tmp_path))
        interceptor._browser_context = self._context()
        guarded_fetch = self._guarded_fetch(body, content_type, filename)

        with patch.object(mod.file_api, "fetch_file_bytes", guarded_fetch, create=True):
            await interceptor._download_url_directly("https://site.example/download", filename)

        saved = list(tmp_path.iterdir())
        if expected_filename is None:
            assert saved == []
        else:
            assert len(saved) == 1
            if expected_filename != "<generated>":
                assert saved[0].name == expected_filename
            assert saved[0].read_bytes() == body

    @pytest.mark.asyncio
    async def test_direct_download_rejects_destination_symlink(self, tmp_path: Path) -> None:
        interceptor = _make_interceptor(output_dir=str(tmp_path))
        interceptor._browser_context = self._context()
        guarded_fetch = self._guarded_fetch(b"private report", "application/pdf", "report.pdf")
        save_path, _ = interceptor._resolve_save_path("report.pdf")
        outside = tmp_path.parent / f"{tmp_path.name}-direct-outside.pdf"
        outside.write_bytes(b"outside")
        save_path.symlink_to(outside)

        with patch.object(mod.file_api, "fetch_file_bytes", guarded_fetch, create=True):
            await interceptor._handle_browser_download(
                {"url": "https://example.com/report.pdf", "suggestedFilename": "report.pdf"}
            )

        assert save_path.is_symlink()
        assert outside.read_bytes() == b"outside"

    @pytest.mark.asyncio
    async def test_http_browser_download_routes_through_direct_download(self, tmp_path: Path) -> None:
        interceptor = _make_interceptor(output_dir=str(tmp_path))
        interceptor._browser_context = self._context()
        guarded_fetch = self._guarded_fetch(self._LOGIN_HTML, "text/html", "report.zip")

        with patch.object(mod.file_api, "fetch_file_bytes", guarded_fetch, create=True):
            await interceptor._handle_browser_download(
                {
                    "url": "https://site.example/download?f=report.zip",
                    "suggestedFilename": "report.zip",
                }
            )

        assert list(tmp_path.iterdir()) == []


class TestDownloadDirRebindDedup:
    """SKY-12769: a persistent/adopted interceptor is reused across runs via set_download_dir.

    Each captured URL in _downloaded_urls corresponds to a file already written into the previous
    _output_dir, so the dedupe set is directory-scoped. A genuine dir change must drop it, or an
    identical download in the new run's dir is skipped and its artifact goes missing. A same-dir
    rebind must keep it so repeated events stay idempotent.
    """

    @pytest.mark.asyncio
    async def test_data_url_reprocessed_after_dir_change(self, tmp_path: Path) -> None:
        dir_a = tmp_path / "run_a"
        dir_b = tmp_path / "run_b"
        interceptor = _make_interceptor(output_dir=str(dir_a))
        event = {"url": "data:text/plain,hello", "suggestedFilename": "note.txt"}

        await interceptor._handle_browser_download(event)
        assert (dir_a / "note.txt").read_bytes() == b"hello"

        interceptor.set_download_dir(str(dir_b))
        await interceptor._handle_browser_download(event)
        assert (dir_b / "note.txt").read_bytes() == b"hello"

        await interceptor._handle_browser_download(event)
        assert [path.name for path in dir_b.iterdir()] == ["note.txt"]

    @pytest.mark.asyncio
    async def test_same_dir_set_preserves_data_url_dedupe(self, tmp_path: Path) -> None:
        interceptor = _make_interceptor(output_dir=str(tmp_path))
        event = {"url": "data:text/plain,hello", "suggestedFilename": "note.txt"}

        await interceptor._handle_browser_download(event)
        assert interceptor._download_index == 1

        interceptor.set_download_dir(str(tmp_path))
        await interceptor._handle_browser_download(event)

        assert interceptor._download_index == 1
        assert [path.name for path in tmp_path.iterdir()] == ["note.txt"]

    def test_real_dir_change_clears_cross_path_dedupe(self, tmp_path: Path) -> None:
        interceptor = _make_interceptor(output_dir=str(tmp_path / "run_a"))
        interceptor._downloaded_urls.update(
            {"https://site.example/report.pdf", "blob:https://site.example/abc", "data:sha256:deadbeef"}
        )

        interceptor.set_download_dir(str(tmp_path / "run_b"))

        assert interceptor._downloaded_urls == set()

    def test_same_dir_set_preserves_cross_path_dedupe(self, tmp_path: Path) -> None:
        interceptor = _make_interceptor(output_dir=str(tmp_path))
        seeded = {"https://site.example/report.pdf", "blob:https://site.example/abc"}
        interceptor._downloaded_urls.update(seeded)

        interceptor.set_download_dir(str(tmp_path))

        assert interceptor._downloaded_urls == seeded

    def test_first_dir_set_from_none_does_not_touch_dedupe(self, tmp_path: Path) -> None:
        interceptor = _make_interceptor()
        assert interceptor._output_dir is None

        interceptor.set_download_dir(str(tmp_path))

        assert interceptor._downloaded_urls == set()
        assert interceptor._output_dir == tmp_path

    @pytest.mark.asyncio
    async def test_in_flight_data_write_does_not_readd_identity_into_new_scope(self, tmp_path: Path) -> None:
        """A data-URL write that began under dir A but publishes while a rebind to dir B is in flight
        must not re-insert its identity into dir B's (freshly cleared) dedupe scope — the file landed
        in dir A, so dir B could otherwise skip an identical download and miss its artifact."""
        dir_a = tmp_path / "run_a"
        dir_b = tmp_path / "run_b"
        interceptor = _make_interceptor(output_dir=str(dir_a))
        event = {"url": "data:text/plain,hello", "suggestedFilename": "note.txt"}

        entered_publication, release_publication, publication_patch = TestDataUrlDownloadCapture._paused_publication()
        with publication_patch:
            writing = asyncio.create_task(interceptor._handle_browser_download(event))
            assert await asyncio.to_thread(entered_publication.wait, 2)
            interceptor.set_download_dir(str(dir_b))
            assert interceptor._downloaded_urls == set()
            release_publication.set()
            await asyncio.wait_for(writing, timeout=2)

        assert (dir_a / "note.txt").read_bytes() == b"hello"
        assert interceptor._downloaded_urls == set()

        await interceptor._handle_browser_download(event)
        assert (dir_b / "note.txt").read_bytes() == b"hello"
        assert len(interceptor._downloaded_urls) == 1

        await interceptor._handle_browser_download(event)
        assert [path.name for path in dir_b.iterdir()] == ["note.txt"]

    @pytest.mark.asyncio
    async def test_mkdir_failure_then_same_dir_retry_clears_stale_dedupe_and_writes(self, tmp_path: Path) -> None:
        """A failed mkdir during a real dir change must not leave the new scope carrying the prior
        run's dedupe: the clear happens on scope assignment, before mkdir, so a same-dir retry
        (dir_changed=False) still starts from an empty set and can write."""
        interceptor = _make_interceptor(output_dir=str(tmp_path / "run_a"))
        interceptor._downloaded_urls.update({"https://site.example/report.pdf", "data:sha256:deadbeef"})
        target = tmp_path / "run_b"
        event = {"url": "data:text/plain,hello", "suggestedFilename": "note.txt"}

        with patch.object(mod.Path, "mkdir", autospec=True, side_effect=OSError("disk full")):
            with pytest.raises(OSError):
                interceptor.set_download_dir(str(target))

        assert interceptor._downloaded_urls == set()

        interceptor.set_download_dir(str(target))
        assert interceptor._downloaded_urls == set()

        await interceptor._handle_browser_download(event)
        assert (target / "note.txt").read_bytes() == b"hello"
        assert len(interceptor._downloaded_urls) == 1


class _StreamCDP:
    """Fake CDP session that dispatches Fetch/IO calls by method, for _handle_download streaming tests.

    IO.read emits the configured `chunks` one per call (last one carries eof), so a chunk and eof can
    arrive in the same message — mirroring real Chromium behavior.
    """

    def __init__(
        self,
        *,
        chunks: list[bytes] | None = None,
        take_error: BaseException | None = None,
        stream_missing: bool = False,
        read_error: BaseException | None = None,
        read_error_after: int = 0,
        getbody: bytes | None = None,
        getbody_error: BaseException | None = None,
        fulfill_error: BaseException | None = None,
        read_hang: bool = False,
        close_hang: bool = False,
        take_hang: bool = False,
        fulfill_hang: bool = False,
    ) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._chunks = list(chunks or [])
        self._take_error = take_error
        self._stream_missing = stream_missing
        self._read_error = read_error
        self._read_error_after = read_error_after
        self._getbody = getbody
        self._getbody_error = getbody_error
        self._fulfill_error = fulfill_error
        self._read_hang = read_hang
        self._close_hang = close_hang
        self._take_hang = take_hang
        self._fulfill_hang = fulfill_hang
        self._read_idx = 0

    async def send(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        params = params or {}
        self.calls.append((method, params))
        if method == "Fetch.takeResponseBodyAsStream":
            if self._take_hang:
                await asyncio.Event().wait()  # never returns — simulates a stalled stream-start
            if self._take_error is not None:
                raise self._take_error
            return {} if self._stream_missing else {"stream": "handle-1"}
        if method == "IO.read":
            if self._read_hang:
                await asyncio.Event().wait()  # never returns — simulates a stalled CDP read
            if self._read_error is not None and self._read_idx >= self._read_error_after:
                raise self._read_error
            if self._read_idx < len(self._chunks):
                chunk = self._chunks[self._read_idx]
                self._read_idx += 1
                eof = self._read_idx >= len(self._chunks)
                return {"data": base64.b64encode(chunk).decode(), "base64Encoded": True, "eof": eof}
            return {"data": "", "base64Encoded": False, "eof": True}
        if method == "IO.close":
            if self._close_hang:
                await asyncio.Event().wait()  # never returns — simulates a stalled close on a dead session
            return {}
        if method == "Fetch.getResponseBody":
            if self._getbody_error is not None:
                raise self._getbody_error
            if self._getbody is None:
                raise RuntimeError("getResponseBody not configured")
            return {"body": base64.b64encode(self._getbody).decode(), "base64Encoded": True}
        if method == "Fetch.fulfillRequest":
            if self._fulfill_hang:
                await asyncio.Event().wait()  # never returns — simulates a stalled fulfill on a dead session
            if self._fulfill_error is not None:
                raise self._fulfill_error
            return {}
        return {}

    def count(self, method: str) -> int:
        return sum(1 for m, _ in self.calls if m == method)

    def last(self, method: str) -> dict[str, Any] | None:
        for m, p in reversed(self.calls):
            if m == method:
                return p
        return None


def _raw_headers(
    *,
    content_length: int | None = None,
    content_type: str = "application/pdf",
    content_disposition: str | None = 'attachment; filename="doc.pdf"',
    content_encoding: str | None = None,
    transfer_encoding: str | None = None,
    content_range: str | None = None,
) -> list[dict[str, str]]:
    raw: list[dict[str, str]] = []
    if content_type:
        raw.append({"name": "Content-Type", "value": content_type})
    if content_disposition:
        raw.append({"name": "Content-Disposition", "value": content_disposition})
    if content_length is not None:
        raw.append({"name": "Content-Length", "value": str(content_length)})
    if content_encoding:
        raw.append({"name": "Content-Encoding", "value": content_encoding})
    if transfer_encoding:
        raw.append({"name": "Transfer-Encoding", "value": transfer_encoding})
    if content_range:
        raw.append({"name": "Content-Range", "value": content_range})
    return raw


@contextlib.contextmanager
def _stream_limits(threshold: int = 1024, cap: int = 4096) -> Any:
    """Shrink the stream threshold/cap so state-machine transitions can be tested on tiny payloads."""
    with (
        patch.object(mod, "STREAM_TO_DISK_THRESHOLD_BYTES", threshold),
        patch.object(mod, "MAX_STREAMED_FILE_SIZE_BYTES", cap),
    ):
        yield


async def _drive_download(
    interceptor: CDPDownloadInterceptor,
    cdp: _StreamCDP,
    raw: list[dict[str, str]],
    *,
    url: str = "https://example.com/doc.pdf",
    status: int = 200,
) -> None:
    await interceptor._handle_download(cdp, "req-1", url, mod._parse_headers(raw), status, raw)  # type: ignore[arg-type]


def _fulfill_body(cdp: _StreamCDP) -> str | None:
    p = cdp.last("Fetch.fulfillRequest")
    return None if p is None else p.get("body", "")


def _fulfill_headers(cdp: _StreamCDP) -> dict[str, str]:
    p = cdp.last("Fetch.fulfillRequest")
    if p is None:
        return {}
    return {h["name"].lower(): h["value"] for h in p.get("responseHeaders", [])}


def _only_file(tmp_path: Path) -> Path:
    files = list(tmp_path.iterdir())
    assert len(files) == 1, f"expected exactly one file, found {[f.name for f in files]}"
    return files[0]


async def _settle(interceptor: CDPDownloadInterceptor) -> None:
    async with interceptor.settle_browser_downloads():
        pass


def test_finalize_download_has_no_dead_buffered_threshold_or_fallback_branch() -> None:
    """A buffered outcome reaching _finalize_download is always below STREAM_TO_DISK_THRESHOLD_BYTES: the
    stream loop spills to disk (→ "streamed") the instant total bytes cross the threshold, so the buffered
    branch can never carry a body at/over the threshold or the removed getResponseBody-fallback cap. Guard
    against reintroducing a dead re-check of either size constant or a reference to the removed fallback."""
    source = inspect.getsource(CDPDownloadInterceptor._finalize_download)
    referenced_names = {node.id for node in ast.walk(ast.parse(textwrap.dedent(source))) if isinstance(node, ast.Name)}
    assert "MAX_STREAMED_FILE_SIZE_BYTES" not in referenced_names
    assert "STREAM_TO_DISK_THRESHOLD_BYTES" not in referenced_names
    assert "getResponseBody" not in source


class TestTwoPathDownloadStreaming:
    """SKY-12642: large captured downloads stream to a temp file and fulfill the browser with an empty
    body; small ones keep the legacy buffer + full replay. Threshold/cap patched tiny per test."""

    @pytest.mark.asyncio
    async def test_small_body_buffered_full_replay(self, tmp_path: Path) -> None:
        interceptor = _make_interceptor(output_dir=str(tmp_path))
        payload = b"A" * 500
        cdp = _StreamCDP(chunks=[payload])
        with _stream_limits():
            await _drive_download(interceptor, cdp, _raw_headers(content_length=500))

        assert _only_file(tmp_path).read_bytes() == payload
        assert _fulfill_body(cdp) == base64.b64encode(payload).decode()  # full-body replay preserved
        assert cdp.count("Fetch.getResponseBody") == 0
        assert cdp.count("Fetch.failRequest") == 0

    @pytest.mark.asyncio
    async def test_buffered_download_rejects_destination_symlink(self, tmp_path: Path) -> None:
        interceptor = _make_interceptor(output_dir=str(tmp_path))
        save_path, _ = interceptor._resolve_save_path("doc.pdf")
        outside = tmp_path.parent / f"{tmp_path.name}-buffered-outside.pdf"
        outside.write_bytes(b"outside")
        save_path.symlink_to(outside)
        cdp = _StreamCDP(chunks=[b"private report"])

        with _stream_limits(threshold=1024):
            await _drive_download(interceptor, cdp, _raw_headers(content_length=14))

        assert save_path.is_symlink()
        assert outside.read_bytes() == b"outside"
        assert cdp.count("Fetch.failRequest") == 1
        assert cdp.count("Fetch.fulfillRequest") == 0

    @pytest.mark.asyncio
    async def test_at_threshold_streams_to_disk_empty_fulfill(self, tmp_path: Path) -> None:
        interceptor = _make_interceptor(output_dir=str(tmp_path))
        payload = b"B" * 1024  # exactly the threshold → large path
        cdp = _StreamCDP(chunks=[payload])
        with _stream_limits(threshold=1024):
            await _drive_download(interceptor, cdp, _raw_headers(content_length=1024))

        assert _only_file(tmp_path).read_bytes() == payload
        assert _fulfill_body(cdp) == ""  # empty body, no base64 replay
        assert _fulfill_headers(cdp).get("content-length") == "0"
        assert cdp.count("Fetch.failRequest") == 0

    @pytest.mark.asyncio
    async def test_streamed_download_rejects_symlinked_output_directory(self, tmp_path: Path) -> None:
        outside = tmp_path / "outside"
        outside.mkdir()
        output_dir = tmp_path / "downloads"
        output_dir.symlink_to(outside, target_is_directory=True)
        interceptor = _make_interceptor(output_dir=str(output_dir))
        cdp = _StreamCDP(chunks=[b"B" * 1024])

        with _stream_limits(threshold=1024):
            await _drive_download(interceptor, cdp, _raw_headers(content_length=1024))

        assert list(outside.iterdir()) == []
        assert cdp.count("Fetch.failRequest") == 1
        assert cdp.count("Fetch.fulfillRequest") == 0

    @pytest.mark.asyncio
    async def test_streamed_download_destination_swap_fails_at_publication(self, tmp_path: Path) -> None:
        interceptor = _make_interceptor(output_dir=str(tmp_path))
        outside = tmp_path.parent / f"{tmp_path.name}-stream-outside.txt"
        outside.write_bytes(b"outside")
        cdp = _StreamCDP(chunks=[b"B" * 1024])
        real_link = mod.os.link
        swapped = False

        def swap_destination_then_link(src: str, dst: str, **kwargs: Any) -> None:
            nonlocal swapped
            if dst == "doc.pdf":
                (tmp_path / dst).symlink_to(outside)
                swapped = True
            real_link(src, dst, **kwargs)

        with (
            _stream_limits(threshold=1024),
            patch.object(mod.os, "link", side_effect=swap_destination_then_link),
        ):
            await _drive_download(interceptor, cdp, _raw_headers(content_length=1024))

        assert swapped
        assert (tmp_path / "doc.pdf").is_symlink()
        assert outside.read_bytes() == b"outside"
        assert cdp.count("Fetch.failRequest") == 1
        assert cdp.count("Fetch.fulfillRequest") == 0

    @pytest.mark.asyncio
    async def test_above_threshold_known_large_streams(self, tmp_path: Path) -> None:
        interceptor = _make_interceptor(output_dir=str(tmp_path))
        payload = b"C" * 2000
        cdp = _StreamCDP(chunks=[b"C" * 800, b"C" * 800, b"C" * 400])
        with _stream_limits(threshold=1024, cap=4096):
            await _drive_download(interceptor, cdp, _raw_headers(content_length=2000))

        assert _only_file(tmp_path).read_bytes() == payload
        assert _fulfill_body(cdp) == ""

    @pytest.mark.asyncio
    async def test_unknown_content_length_spills_to_disk(self, tmp_path: Path) -> None:
        interceptor = _make_interceptor(output_dir=str(tmp_path))
        # No Content-Length: must not assume small. Buffers, then spills once bytes cross the threshold.
        chunks = [b"D" * 400, b"D" * 400, b"D" * 400, b"D" * 400, b"D" * 400]  # 2000 total
        cdp = _StreamCDP(chunks=list(chunks))
        with _stream_limits(threshold=1024, cap=4096):
            await _drive_download(interceptor, cdp, _raw_headers(content_length=None))

        assert _only_file(tmp_path).read_bytes() == b"D" * 2000  # early buffered bytes preserved on spill
        assert _fulfill_body(cdp) == ""

    @pytest.mark.asyncio
    async def test_lying_small_content_length_spills(self, tmp_path: Path) -> None:
        interceptor = _make_interceptor(output_dir=str(tmp_path))
        # Header claims 100 bytes but the body is 2000 → must spill to the large path, not trust the header.
        cdp = _StreamCDP(chunks=[b"E" * 700, b"E" * 700, b"E" * 600])
        with _stream_limits(threshold=1024, cap=4096):
            await _drive_download(interceptor, cdp, _raw_headers(content_length=100))

        assert _only_file(tmp_path).read_bytes() == b"E" * 2000
        assert _fulfill_body(cdp) == ""

    @pytest.mark.asyncio
    async def test_lying_large_content_length_tiny_body_finalizes(self, tmp_path: Path) -> None:
        interceptor = _make_interceptor(output_dir=str(tmp_path))
        # Header claims large (>= threshold) → disk path from chunk 1, but the actual body is tiny.
        cdp = _StreamCDP(chunks=[b"F" * 300])
        with _stream_limits(threshold=1024, cap=4096):
            await _drive_download(interceptor, cdp, _raw_headers(content_length=2000))

        assert _only_file(tmp_path).read_bytes() == b"F" * 300
        assert _fulfill_body(cdp) == ""
        assert cdp.count("Fetch.failRequest") == 0

    @pytest.mark.asyncio
    async def test_cap_breach_unknown_cl_aborts_no_artifact(self, tmp_path: Path) -> None:
        interceptor = _make_interceptor(output_dir=str(tmp_path))
        cdp = _StreamCDP(chunks=[b"G" * 1500, b"G" * 1500, b"G" * 1500, b"G" * 1500])  # 6000 > cap 4096
        with _stream_limits(threshold=1024, cap=4096):
            await _drive_download(interceptor, cdp, _raw_headers(content_length=None))

        assert list(tmp_path.iterdir()) == []  # no artifact, no leftover .crdownload temp
        assert cdp.count("Fetch.failRequest") == 1
        assert cdp.count("Fetch.fulfillRequest") == 0

    @pytest.mark.asyncio
    async def test_header_content_length_over_cap_fast_fails(self, tmp_path: Path) -> None:
        interceptor = _make_interceptor(output_dir=str(tmp_path))
        cdp = _StreamCDP(chunks=[b"H" * 10])
        with _stream_limits(threshold=1024, cap=4096):
            await _drive_download(interceptor, cdp, _raw_headers(content_length=5000))  # > cap

        assert list(tmp_path.iterdir()) == []
        assert cdp.count("Fetch.takeResponseBodyAsStream") == 0  # fast-fail without streaming
        assert cdp.count("Fetch.failRequest") == 1

    @pytest.mark.asyncio
    async def test_header_content_length_over_cap_marks_url_handled(self, tmp_path: Path) -> None:
        # The oversized-by-header fast-fail must still mark the URL handled, or a queued
        # Browser.downloadWillBegin lets _handle_browser_download re-fetch it via _download_url_directly
        # (which reads the whole body before its 100 MiB check) and materializes the very payload the cap
        # exists to block (SKY-12642).
        interceptor = _make_interceptor(output_dir=str(tmp_path))
        cdp = _StreamCDP(chunks=[b"H" * 10])
        url = "https://example.com/huge.pdf"
        with _stream_limits(threshold=1024, cap=4096):
            await _drive_download(interceptor, cdp, _raw_headers(content_length=5000), url=url)  # > cap

        assert cdp.count("Fetch.failRequest") == 1
        assert interceptor._downloaded_urls == {url}

    @pytest.mark.asyncio
    async def test_midstream_read_error_cleans_temp_and_fails(self, tmp_path: Path) -> None:
        interceptor = _make_interceptor(output_dir=str(tmp_path))
        # Known-large → disk path; the second IO.read raises after one chunk is on disk.
        cdp = _StreamCDP(
            chunks=[b"I" * 800, b"I" * 800, b"I" * 800],
            read_error=RuntimeError("CDP IO.read boom"),
            read_error_after=1,
        )
        with _stream_limits(threshold=1024, cap=4096):
            await _drive_download(interceptor, cdp, _raw_headers(content_length=2400))

        assert list(tmp_path.iterdir()) == []  # partial temp cleaned up
        assert cdp.count("Fetch.fulfillRequest") == 0
        assert cdp.count("Fetch.failRequest") == 1

    @pytest.mark.asyncio
    async def test_cancelled_error_cleans_temp_and_propagates(self, tmp_path: Path) -> None:
        interceptor = _make_interceptor(output_dir=str(tmp_path))
        cdp = _StreamCDP(
            chunks=[b"J" * 800, b"J" * 800, b"J" * 800],
            read_error=asyncio.CancelledError(),
            read_error_after=1,
        )
        with _stream_limits(threshold=1024, cap=4096):
            with pytest.raises(asyncio.CancelledError):
                await _drive_download(interceptor, cdp, _raw_headers(content_length=2400))

        assert list(tmp_path.iterdir()) == []  # no partial artifact
        assert cdp.count("Fetch.failRequest") == 0  # never do I/O during cancellation

    @pytest.mark.asyncio
    async def test_streamed_empty_fulfill_normalizes_headers(self, tmp_path: Path) -> None:
        interceptor = _make_interceptor(output_dir=str(tmp_path))
        cdp = _StreamCDP(chunks=[b"K" * 2000])
        raw = _raw_headers(
            content_length=2000,
            content_type="application/pdf",
            content_disposition='attachment; filename="report.pdf"',
            content_encoding="gzip",
            transfer_encoding="chunked",
            content_range="bytes 0-1999/2000",
        )
        with _stream_limits(threshold=1024, cap=8192):
            await _drive_download(interceptor, cdp, raw)

        h = _fulfill_headers(cdp)
        assert h.get("content-length") == "0"
        assert "content-encoding" not in h  # empty body must not be content-decoded
        assert "transfer-encoding" not in h
        assert "content-range" not in h
        assert h.get("content-disposition") == 'attachment; filename="report.pdf"'  # MUST survive
        assert h.get("content-type") == "application/pdf"

    @pytest.mark.asyncio
    async def test_stream_start_fail_fails_request_never_falls_back_to_whole_body(self, tmp_path: Path) -> None:
        # takeResponseBodyAsStream failing must fail the request — we never fall back to getResponseBody,
        # whose whole-body materialization could OOM on a lying/understated Content-Length (SKY-12642).
        interceptor = _make_interceptor(output_dir=str(tmp_path))
        cdp = _StreamCDP(take_error=RuntimeError("takeResponseBodyAsStream unavailable"), getbody=b"L" * 300)
        with _stream_limits(threshold=1024, cap=4096):
            await _drive_download(interceptor, cdp, _raw_headers(content_length=300))

        assert list(tmp_path.iterdir()) == []  # nothing materialized or persisted
        assert cdp.count("Fetch.getResponseBody") == 0  # whole-body fallback removed
        assert cdp.count("Fetch.failRequest") == 1
        assert cdp.count("Fetch.fulfillRequest") == 0

    @pytest.mark.asyncio
    async def test_stream_start_fail_with_content_encoding_no_direct_fallback(self, tmp_path: Path) -> None:
        interceptor = _make_interceptor(output_dir=str(tmp_path))
        # Encoded small CL can decode to an unbounded body → getResponseBody would materialize it. Refuse.
        cdp = _StreamCDP(take_error=RuntimeError("take failed"), getbody=b"L" * 300)
        with _stream_limits(threshold=1024, cap=4096):
            await _drive_download(interceptor, cdp, _raw_headers(content_length=300, content_encoding="gzip"))

        assert list(tmp_path.iterdir()) == []
        assert cdp.count("Fetch.getResponseBody") == 0
        assert cdp.count("Fetch.failRequest") == 1

    @pytest.mark.asyncio
    async def test_extraction_serialized_second_download_waits_for_lock(self, tmp_path: Path) -> None:
        # Single-active guard: while one extraction holds the lock, a second _handle_download must block
        # (not stream concurrently), so N parallel downloads cannot each write up to the cap to disk.
        interceptor = _make_interceptor(output_dir=str(tmp_path))
        cdp = _StreamCDP(chunks=[b"A" * 200])
        await interceptor._download_extraction_lock.acquire()
        task = asyncio.create_task(_drive_download(interceptor, cdp, _raw_headers(content_length=200)))
        await asyncio.sleep(0.05)
        # Blocked on the lock: no extraction started, nothing written to disk.
        assert not task.done()
        assert cdp.count("Fetch.takeResponseBodyAsStream") == 0
        assert list(tmp_path.iterdir()) == []
        interceptor._download_extraction_lock.release()
        await asyncio.wait_for(task, timeout=2)
        assert _only_file(tmp_path).read_bytes() == b"A" * 200  # proceeds once serialized
        assert not interceptor._download_extraction_lock.locked()

    @pytest.mark.asyncio
    async def test_extraction_lock_released_on_success_error_and_start_failure(self, tmp_path: Path) -> None:
        # The single-active lock must be released on every exit path, or one failed download deadlocks all
        # later downloads on this interceptor.
        interceptor = _make_interceptor(output_dir=str(tmp_path))
        await _drive_download(interceptor, _StreamCDP(chunks=[b"S" * 100]), _raw_headers(content_length=100))
        assert not interceptor._download_extraction_lock.locked()  # success
        with _stream_limits(threshold=1024, cap=4096):
            await _drive_download(interceptor, _StreamCDP(chunks=[b"B" * 5000]), _raw_headers(content_length=None))
        assert not interceptor._download_extraction_lock.locked()  # cap abort
        await _drive_download(interceptor, _StreamCDP(stream_missing=True), _raw_headers(content_length=100))
        assert not interceptor._download_extraction_lock.locked()  # stream-start failure

    @pytest.mark.asyncio
    async def test_settle_drains_scheduled_cdp_handler(self, tmp_path: Path) -> None:
        # A capture runs inside a _cdp_handler_task (and a second Fetch.requestPaused may be scheduled but
        # not yet classified as a download). settle drains those to quiescence, so a capture queued behind
        # the lock — which owns no .crdownload yet — is still waited on before artifact collection instead
        # of being silently omitted.
        interceptor = _make_interceptor(output_dir=str(tmp_path))
        cdp = _StreamCDP(chunks=[b"Q" * 150])
        await interceptor._download_extraction_lock.acquire()  # hold the lock so the capture queues
        interceptor._schedule_cdp_handler(_drive_download(interceptor, cdp, _raw_headers(content_length=150)))
        await asyncio.sleep(0.05)
        assert len(interceptor._cdp_handler_tasks) == 1  # scheduled + queued on the lock, no .crdownload yet
        settling = asyncio.create_task(_settle(interceptor))
        await asyncio.sleep(0.05)
        assert not settling.done()  # settle blocks while the scheduled handler is in flight
        interceptor._download_extraction_lock.release()
        await asyncio.wait_for(settling, timeout=2)  # unblocks once the handler finishes
        assert _only_file(tmp_path).read_bytes() == b"Q" * 150
        assert not interceptor._cdp_handler_tasks  # drained to quiescence

    @pytest.mark.asyncio
    async def test_stalled_close_times_out_and_releases_lock(self, tmp_path: Path) -> None:
        # The IO.close cleanup is bounded too: a hung close on a dead session must not hold the extraction
        # lock or hang teardown. The streamed file is already published, so the download still finalizes.
        interceptor = _make_interceptor(output_dir=str(tmp_path))
        cdp = _StreamCDP(chunks=[b"C" * 2000], close_hang=True)  # IO.close never returns
        with _stream_limits(threshold=1024, cap=4096), patch.object(mod, "STREAM_IO_READ_TIMEOUT_SECONDS", 0.05):
            await asyncio.wait_for(_drive_download(interceptor, cdp, _raw_headers(content_length=2000)), timeout=5)
        assert _only_file(tmp_path).read_bytes() == b"C" * 2000  # saved despite the hung close
        assert not interceptor._download_extraction_lock.locked()

    @pytest.mark.asyncio
    async def test_settle_cancellation_does_not_cancel_capture(self, tmp_path: Path) -> None:
        # A settle-timeout cancellation must NOT cancel the in-flight/queued capture handlers (gather would,
        # destroying the temp mid-stream); the capture keeps running and its artifact still lands.
        interceptor = _make_interceptor(output_dir=str(tmp_path))
        cdp = _StreamCDP(chunks=[b"K" * 150])
        await interceptor._download_extraction_lock.acquire()  # hold the lock so the capture queues
        interceptor._schedule_cdp_handler(_drive_download(interceptor, cdp, _raw_headers(content_length=150)))
        await asyncio.sleep(0.05)
        handler_task = next(iter(interceptor._cdp_handler_tasks))
        settling = asyncio.create_task(_settle(interceptor))
        await asyncio.sleep(0.05)
        settling.cancel()  # the outer download-timeout cancels settle
        with pytest.raises(asyncio.CancelledError):
            await settling
        assert not handler_task.cancelled()  # the capture survives the settle cancellation
        interceptor._download_extraction_lock.release()
        await asyncio.wait_for(handler_task, timeout=2)
        assert _only_file(tmp_path).read_bytes() == b"K" * 150  # artifact lands, not destroyed

    @pytest.mark.asyncio
    async def test_stalled_take_stream_times_out_and_releases_lock(self, tmp_path: Path) -> None:
        # takeResponseBodyAsStream on a dead session is bounded too: on timeout it becomes _StreamStartError
        # → failRequest, so it can't hold the lock or hang settle's drain.
        interceptor = _make_interceptor(output_dir=str(tmp_path))
        cdp = _StreamCDP(take_hang=True)  # takeResponseBodyAsStream never returns
        with _stream_limits(threshold=1024, cap=4096), patch.object(mod, "STREAM_IO_READ_TIMEOUT_SECONDS", 0.05):
            await asyncio.wait_for(_drive_download(interceptor, cdp, _raw_headers(content_length=300)), timeout=5)
        assert cdp.count("Fetch.failRequest") == 1
        assert list(tmp_path.iterdir()) == []
        assert not interceptor._download_extraction_lock.locked()

    @pytest.mark.asyncio
    async def test_stalled_fulfill_times_out_and_releases_lock(self, tmp_path: Path) -> None:
        # A hung fulfillRequest (after the streamed file is already published) is bounded: the artifact is
        # saved, the download still finalizes, and the lock is released.
        interceptor = _make_interceptor(output_dir=str(tmp_path))
        cdp = _StreamCDP(chunks=[b"F" * 2000], fulfill_hang=True)  # fulfillRequest never returns
        with _stream_limits(threshold=1024, cap=4096), patch.object(mod, "STREAM_IO_READ_TIMEOUT_SECONDS", 0.05):
            await asyncio.wait_for(_drive_download(interceptor, cdp, _raw_headers(content_length=2000)), timeout=5)
        assert _only_file(tmp_path).read_bytes() == b"F" * 2000  # streamed artifact saved despite hung fulfill
        assert not interceptor._download_extraction_lock.locked()

    @pytest.mark.asyncio
    async def test_stalled_read_times_out_and_releases_lock(self, tmp_path: Path) -> None:
        # A stalled IO.read must not hold the extraction lock forever (which would deadlock later captures
        # and hang teardown's drain). The per-read timeout aborts it, cleans the temp, and releases the lock.
        interceptor = _make_interceptor(output_dir=str(tmp_path))
        cdp = _StreamCDP(read_hang=True)  # IO.read never returns
        with _stream_limits(threshold=1024, cap=4096), patch.object(mod, "STREAM_IO_READ_TIMEOUT_SECONDS", 0.05):
            # Outer bound so a regression (no per-read timeout) fails fast instead of hanging the suite.
            await asyncio.wait_for(_drive_download(interceptor, cdp, _raw_headers(content_length=2000)), timeout=5)
        assert cdp.count("Fetch.failRequest") == 1
        assert list(tmp_path.iterdir()) == []  # temp cleaned up, no artifact
        assert not interceptor._download_extraction_lock.locked()

    @pytest.mark.asyncio
    async def test_stream_start_fail_unknown_cl_no_direct_fallback(self, tmp_path: Path) -> None:
        interceptor = _make_interceptor(output_dir=str(tmp_path))
        cdp = _StreamCDP(take_error=RuntimeError("take failed"), getbody=b"M" * 300)
        with _stream_limits(threshold=1024, cap=4096):
            await _drive_download(interceptor, cdp, _raw_headers(content_length=None))

        assert list(tmp_path.iterdir()) == []
        assert cdp.count("Fetch.getResponseBody") == 0  # unbounded getResponseBody not safe on unknown size
        assert cdp.count("Fetch.continueResponse") == 0  # never let the browser materialize a large body
        assert cdp.count("Fetch.failRequest") == 1

    @pytest.mark.asyncio
    async def test_publication_failure_cleans_temp_and_fails(self, tmp_path: Path) -> None:
        interceptor = _make_interceptor(output_dir=str(tmp_path))
        cdp = _StreamCDP(chunks=[b"N" * 2000])
        with _stream_limits(threshold=1024, cap=4096):
            with patch.object(mod.os, "link", side_effect=OSError("publication failed")):
                await _drive_download(interceptor, cdp, _raw_headers(content_length=2000))

        assert list(tmp_path.iterdir()) == []  # temp cleaned even when finalize fails
        assert cdp.count("Fetch.fulfillRequest") == 0
        assert cdp.count("Fetch.failRequest") == 1

    @pytest.mark.asyncio
    async def test_read_data_and_eof_in_same_message_not_dropped(self, tmp_path: Path) -> None:
        interceptor = _make_interceptor(output_dir=str(tmp_path))
        # Single IO.read returns the whole body AND eof in one message; the last chunk must not be lost.
        cdp = _StreamCDP(chunks=[b"O" * 900])
        with _stream_limits(threshold=1024, cap=4096):
            await _drive_download(interceptor, cdp, _raw_headers(content_length=900))

        assert _only_file(tmp_path).read_bytes() == b"O" * 900
        assert cdp.count("IO.read") == 1

    @pytest.mark.asyncio
    async def test_zero_byte_download_saved_and_replayed(self, tmp_path: Path) -> None:
        interceptor = _make_interceptor(output_dir=str(tmp_path))
        cdp = _StreamCDP(chunks=[])  # immediate eof, no data
        with _stream_limits(threshold=1024, cap=4096):
            await _drive_download(interceptor, cdp, _raw_headers(content_length=0))

        assert _only_file(tmp_path).read_bytes() == b""
        assert cdp.count("Fetch.fulfillRequest") == 1
        assert cdp.count("Fetch.failRequest") == 0

    @pytest.mark.asyncio
    async def test_stale_interception_on_fulfill_is_benign_after_save(self, tmp_path: Path) -> None:
        interceptor = _make_interceptor(output_dir=str(tmp_path))
        stale = Exception("Protocol error (Fetch.fulfillRequest): Invalid InterceptionId")
        cdp = _StreamCDP(chunks=[b"P" * 2000], fulfill_error=stale)
        with _stream_limits(threshold=1024, cap=4096):
            with capture_logs() as logs:
                await _drive_download(interceptor, cdp, _raw_headers(content_length=2000))

        assert _only_file(tmp_path).read_bytes() == b"P" * 2000  # file saved despite the benign fulfill race
        assert not [log for log in logs if log.get("log_level") == "error"]
