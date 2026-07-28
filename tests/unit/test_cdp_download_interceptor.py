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
from skyvern.webeye.cdp_download_interceptor import (
    CDPDownloadInterceptor,
    _is_stale_interception_error,
    extract_filename,
    is_download_response,
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
        interceptor = CDPDownloadInterceptor(output_dir=str(tmp_path))
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

    def test_path_traversal_sanitized(self, tmp_path: Path) -> None:
        """Path traversal components should be stripped — only the final name is kept."""
        interceptor = self._make_interceptor(tmp_path)
        save_path, filename = interceptor._resolve_save_path("../../etc/cron.d/evil")
        assert filename == "evil"
        assert save_path == tmp_path / "evil"

    def test_header_date_separators_are_filename_chars(self, tmp_path: Path) -> None:
        """Invoice-style slashes in Content-Disposition should not collapse to the last date segment."""
        interceptor = self._make_interceptor(tmp_path)
        save_path, filename = interceptor._resolve_save_path("invoice_5/19/2026", "application/pdf")
        assert filename == "invoice_5_19_2026.pdf"
        assert save_path == tmp_path / "invoice_5_19_2026.pdf"

    def test_missing_extension_uses_pdf_content_type(self, tmp_path: Path) -> None:
        interceptor = self._make_interceptor(tmp_path)
        save_path, filename = interceptor._resolve_save_path("2026", "application/pdf; charset=utf-8")
        assert filename == "2026.pdf"
        assert save_path == tmp_path / "2026.pdf"

    def test_existing_pdf_extension_not_duplicated(self, tmp_path: Path) -> None:
        interceptor = self._make_interceptor(tmp_path)
        save_path, filename = interceptor._resolve_save_path("invoice_5/19/2026.pdf", "application/pdf")
        assert filename == "invoice_5_19_2026.pdf"
        assert save_path == tmp_path / "invoice_5_19_2026.pdf"


class TestCDPDownloadInterceptorProxyAuth:
    """Tests for CDP proxy authentication handling (Fetch.authRequired + continueWithAuth)."""

    def _make_interceptor(
        self,
        proxy_username: str | None = None,
        proxy_password: str | None = None,
    ) -> CDPDownloadInterceptor:
        return CDPDownloadInterceptor(
            output_dir="/tmp/test_downloads",
            proxy_username=proxy_username,
            proxy_password=proxy_password,
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
        """enable_for_page without credentials should only intercept Response stage."""
        interceptor = self._make_interceptor()

        mock_cdp_session = self._make_cdp_session()
        mock_page = MagicMock()
        mock_page.url = "about:blank"
        mock_page.context.new_cdp_session = AsyncMock(return_value=mock_cdp_session)

        await interceptor.enable_for_page(mock_page)

        # Verify Fetch.enable with Response-only pattern, no auth
        mock_cdp_session.send.assert_called_once_with(
            "Fetch.enable",
            {
                "patterns": [{"requestStage": "Response"}],
                "handleAuthRequests": False,
            },
        )

        # Verify only requestPaused handler (no authRequired)
        event_names = [call.args[0] for call in mock_cdp_session.on.call_args_list]
        assert "Fetch.requestPaused" in event_names
        assert "Fetch.authRequired" not in event_names

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
    async def test_request_stage_continues_request(self) -> None:
        """Request-stage events (no responseStatusCode) should be continued with Fetch.continueRequest."""
        interceptor = self._make_interceptor(proxy_username="user", proxy_password="pass")
        cdp_session = self._make_cdp_session()

        event = {
            "requestId": "req-1",
            "request": {"url": "https://example.com/page"},
            "resourceType": "Document",
            # No responseStatusCode — this is a Request-stage event
        }

        await interceptor._handle_request_paused(event, cdp_session)

        cdp_session.send.assert_called_once_with("Fetch.continueRequest", {"requestId": "req-1"})

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
        return CDPDownloadInterceptor(output_dir="/tmp/test_downloads")

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
        interceptor = CDPDownloadInterceptor(output_dir=str(tmp_path))
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
    async def test_blob_download_falls_through_pages_until_readable(self, tmp_path: Path) -> None:
        interceptor = CDPDownloadInterceptor(output_dir=str(tmp_path))
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
        interceptor = CDPDownloadInterceptor(output_dir=str(tmp_path))
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
        interceptor = CDPDownloadInterceptor(output_dir=str(tmp_path))
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
        interceptor = CDPDownloadInterceptor(output_dir=str(tmp_path))
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
        interceptor = CDPDownloadInterceptor(output_dir=str(tmp_path))
        with patch(self._READ_BLOB, new=AsyncMock()) as read:
            await interceptor._handle_browser_download(
                {"url": "blob:https://example.com/none", "suggestedFilename": "x.pdf"}
            )
        read.assert_not_awaited()
        assert list(tmp_path.iterdir()) == []

    @pytest.mark.asyncio
    async def test_blob_download_already_captured_via_fetch_is_skipped(self, tmp_path: Path) -> None:
        interceptor = CDPDownloadInterceptor(output_dir=str(tmp_path))
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
        interceptor = CDPDownloadInterceptor(output_dir=str(tmp_path))
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
        interceptor = CDPDownloadInterceptor(output_dir=str(tmp_path))
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
        interceptor = CDPDownloadInterceptor(output_dir=str(tmp_path))

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
    async def test_data_url_uses_safe_generated_filename(self, tmp_path: Path) -> None:
        interceptor = CDPDownloadInterceptor(output_dir=str(tmp_path))

        await interceptor._handle_browser_download(
            {"url": "data:application/pdf;base64,JVBERg==", "suggestedFilename": "../../report.pdf"}
        )

        assert [path.name for path in tmp_path.iterdir()] == ["report.pdf"]
        assert not (tmp_path.parent / "report.pdf").exists()

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
        interceptor = CDPDownloadInterceptor(output_dir=str(tmp_path))

        await interceptor._handle_browser_download({"url": url, "suggestedFilename": "report.pdf"})

        assert list(tmp_path.iterdir()) == []
        assert url not in interceptor._downloaded_urls

    @pytest.mark.asyncio
    async def test_duplicate_data_url_event_is_saved_once(self, tmp_path: Path) -> None:
        interceptor = CDPDownloadInterceptor(output_dir=str(tmp_path))
        event = {"url": "data:text/plain,hello", "suggestedFilename": "note.txt"}

        await interceptor._handle_browser_download(event)
        await interceptor._handle_browser_download(event)

        assert [path.name for path in tmp_path.iterdir()] == ["note.txt"]
        assert (tmp_path / "note.txt").read_bytes() == b"hello"

    @pytest.mark.asyncio
    async def test_duplicate_data_url_logs_never_include_payload(self, tmp_path: Path) -> None:
        interceptor = CDPDownloadInterceptor(output_dir=str(tmp_path))
        payload = "private-inline-payload"
        event = {"url": f"data:text/plain,{payload}", "suggestedFilename": "note.txt"}

        with capture_logs() as logs:
            await interceptor._handle_browser_download(event)
            await interceptor._handle_browser_download(event)

        assert payload not in repr(logs)

    @pytest.mark.asyncio
    async def test_non_base64_oversize_rejected_before_percent_decode(self, tmp_path: Path) -> None:
        interceptor = CDPDownloadInterceptor(output_dir=str(tmp_path))
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
        interceptor = CDPDownloadInterceptor(output_dir=str(tmp_path))
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

        retry_interceptor = CDPDownloadInterceptor(output_dir=str(tmp_path / "retry"))
        with patch.object(retry_interceptor, "_decode_data_url", side_effect=OSError("transient")):
            await retry_interceptor._handle_browser_download(event)
        await retry_interceptor._handle_browser_download(event)

        assert (tmp_path / "retry" / "note.txt").read_bytes() == b"hello"
        assert len(retry_interceptor._downloaded_urls) == 1

    @pytest.mark.asyncio
    async def test_data_url_over_size_limit_does_not_create_artifact(self, tmp_path: Path) -> None:
        interceptor = CDPDownloadInterceptor(output_dir=str(tmp_path))
        url = "data:application/octet-stream;base64,eHh4eHg="

        with patch.object(mod, "MAX_FILE_SIZE_BYTES", 4):
            await interceptor._handle_browser_download({"url": url, "suggestedFilename": "large.bin"})

        assert list(tmp_path.iterdir()) == []
        assert url not in interceptor._downloaded_urls

    @staticmethod
    def _paused_replace() -> tuple[threading.Event, threading.Event, Any]:
        entered, release = threading.Event(), threading.Event()
        real_replace = mod.os.replace

        def replace(source: Path, destination: Path) -> None:
            entered.set()
            assert release.wait(timeout=2)
            real_replace(source, destination)

        return entered, release, patch.object(mod.os, "replace", side_effect=replace)

    @pytest.mark.asyncio
    async def test_data_url_is_atomically_published_from_incomplete_path(self, tmp_path: Path) -> None:
        interceptor = CDPDownloadInterceptor(output_dir=str(tmp_path))
        entered_replace, release_replace, replace_patch = self._paused_replace()
        with replace_patch:
            task = asyncio.create_task(
                interceptor._handle_browser_download(
                    {"url": "data:text/plain,complete", "suggestedFilename": "note.txt"}
                )
            )
            assert await asyncio.to_thread(entered_replace.wait, 2)
            visible = list(tmp_path.iterdir())
            assert len(visible) == 1
            assert visible[0].name.startswith("note.txt.")
            assert visible[0].name.endswith(".crdownload")
            assert not (tmp_path / "note.txt").exists()
            release_replace.set()
            await asyncio.wait_for(task, timeout=2)

        assert [path.name for path in tmp_path.iterdir()] == ["note.txt"]

    @pytest.mark.asyncio
    async def test_cancellation_drains_publication_before_retry(self, tmp_path: Path) -> None:
        interceptor = CDPDownloadInterceptor(output_dir=str(tmp_path))
        event = {"url": "data:text/plain,complete", "suggestedFilename": "note.txt"}
        entered_replace, release_replace, replace_patch = self._paused_replace()
        with replace_patch:
            first = asyncio.create_task(interceptor._handle_browser_download(event))
            assert await asyncio.to_thread(entered_replace.wait, 2)
            first.cancel()
            retry = asyncio.create_task(interceptor._handle_browser_download(event))
            await asyncio.sleep(0)
            assert not retry.done()
            visible = list(tmp_path.iterdir())
            assert len(visible) == 1
            assert visible[0].name.endswith(".crdownload")
            release_replace.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(first, timeout=2)
            await asyncio.wait_for(retry, timeout=2)

        assert (tmp_path / "note.txt").read_bytes() == b"complete"
        assert not list(tmp_path.glob("*.crdownload"))
        assert len(interceptor._downloaded_urls) == 1

    @pytest.mark.asyncio
    async def test_concurrent_distinct_data_urls_with_same_filename_are_serialized(self, tmp_path: Path) -> None:
        interceptor = CDPDownloadInterceptor(output_dir=str(tmp_path))
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
        assert len(interceptor._downloaded_urls) == 2
        assert not list(tmp_path.glob("*.crdownload"))

    @pytest.mark.asyncio
    async def test_digest_is_off_loop_and_invalid_shape_rejected_before_digest(self, tmp_path: Path) -> None:
        interceptor = CDPDownloadInterceptor(output_dir=str(tmp_path))
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
        interceptor = CDPDownloadInterceptor(output_dir=str(tmp_path))
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
        interceptor = CDPDownloadInterceptor(output_dir=str(tmp_path))
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
        interceptor = CDPDownloadInterceptor(output_dir=str(tmp_path))
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
        interceptor = CDPDownloadInterceptor()
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
        interceptor = CDPDownloadInterceptor()
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
        interceptor = CDPDownloadInterceptor()
        monkeypatch.setattr(mod, "MAX_FILE_SIZE_BYTES", 12)
        url = "data:text/plain," + "%41" * 12

        comma_index = mod._bounded_data_url_comma(url)
        _, _, data = interceptor._decode_data_url(url, comma_index)

        assert data == b"A" * 12

    def test_percent_escaped_base64_payload_is_accepted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        interceptor = CDPDownloadInterceptor()
        monkeypatch.setattr(mod, "MAX_FILE_SIZE_BYTES", 1)
        url = "data:text/plain;base64,%51%51%3D%3D"

        comma_index = mod._bounded_data_url_comma(url)
        _, _, data = interceptor._decode_data_url(url, comma_index)

        assert data == b"A"

    def test_maximum_size_percent_escaped_base64_payload_is_accepted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        interceptor = CDPDownloadInterceptor()
        monkeypatch.setattr(mod, "MAX_FILE_SIZE_BYTES", 4)
        url = "data:text/plain;base64," + "".join(f"%{byte:02X}" for byte in b"QUJDRA==")

        comma_index = mod._bounded_data_url_comma(url)
        _, _, data = interceptor._decode_data_url(url, comma_index)

        assert data == b"ABCD"

    def test_percent_escaped_base64_decoded_overflow_is_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        interceptor = CDPDownloadInterceptor()
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
        interceptor = CDPDownloadInterceptor()
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
        interceptor = CDPDownloadInterceptor()
        monkeypatch.setattr(mod, "MAX_FILE_SIZE_BYTES", 12)
        url = "data:text/plain," + "A" * 12 + "%41"

        with patch.object(mod, "_percent_decode_payload") as decode:
            comma_index = mod._bounded_data_url_comma(url)
            with pytest.raises(ValueError, match="decoded payload exceeds size limit"):
                interceptor._decode_data_url(url, comma_index)

        decode.assert_not_called()

    @pytest.mark.asyncio
    async def test_disable_waits_for_racing_browser_monitor_enable(self) -> None:
        interceptor = CDPDownloadInterceptor()
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
        interceptor = CDPDownloadInterceptor()
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
        interceptor = CDPDownloadInterceptor()
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
        old_interceptor = CDPDownloadInterceptor()
        new_interceptor = CDPDownloadInterceptor()
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
        old_interceptor = CDPDownloadInterceptor()
        new_interceptor = CDPDownloadInterceptor()
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
        old_interceptor = CDPDownloadInterceptor()
        new_interceptor = CDPDownloadInterceptor()
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
        old_interceptor = CDPDownloadInterceptor()
        new_interceptor = CDPDownloadInterceptor()
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
        old_interceptor = CDPDownloadInterceptor()
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
        new_interceptor = CDPDownloadInterceptor()
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
        interceptor = CDPDownloadInterceptor()
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
        old_interceptor = CDPDownloadInterceptor()
        first_interceptor = CDPDownloadInterceptor()
        second_interceptor = CDPDownloadInterceptor()
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
        interceptor = CDPDownloadInterceptor()
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
        interceptor = CDPDownloadInterceptor()
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
    """_download_url_directly falls back from the cookie-sharing Playwright APIRequestContext to a
    raw urllib fetch. That fallback must (1) still carry the browser session's cookies, and (2) not
    silently save an HTML login/session-gate page under a binary filename.

    Regression: a session-gated download endpoint fetched without cookies returns its HTML login
    page (HTTP 200); the old fallback issued a cookieless request and saved that HTML as e.g. a
    .zip while reporting the download as successful.
    """

    _URLOPEN = "urllib.request.urlopen"

    @staticmethod
    def _fake_urlopen(body: bytes, content_type: str) -> MagicMock:
        class _Resp:
            headers = {"content-type": content_type}

            def read(self) -> bytes:
                return body

            def __enter__(self) -> "_Resp":
                return self

            def __exit__(self, *exc: object) -> bool:
                return False

        return MagicMock(return_value=_Resp())

    @staticmethod
    def _context_forcing_urllib(cookies: list[dict]) -> MagicMock:
        """Browser context whose APIRequestContext returns proxy-407 (non-OK), forcing the urllib
        fallback, and whose cookies() returns the given session cookies."""
        ctx = MagicMock()
        non_ok = MagicMock()
        non_ok.ok = False
        non_ok.status = 407
        ctx.request.get = AsyncMock(return_value=non_ok)
        ctx.cookies = AsyncMock(return_value=cookies)
        return ctx

    _LOGIN_HTML = (
        b'\n<!DOCTYPE HTML PUBLIC "-//W3C//DTD HTML 4.0 Transitional//EN">\n'
        b"<html><head><title>Login</title></head>"
        b"<body><form method='post' action='./Login.aspx'></form></body></html>"
    )

    @pytest.mark.asyncio
    async def test_urllib_fallback_forwards_browser_cookies(self, tmp_path: Path) -> None:
        interceptor = CDPDownloadInterceptor(output_dir=str(tmp_path))
        interceptor._browser_context = self._context_forcing_urllib(
            [{"name": "ASP.NET_SessionId", "value": "sess123"}, {"name": "auth", "value": "tok"}]
        )
        zip_bytes = b"PK\x03\x04 real zip payload"
        urlopen = self._fake_urlopen(zip_bytes, "application/zip")

        with patch(self._URLOPEN, urlopen):
            await interceptor._download_url_directly("https://site.example/download?f=statement.zip", "statement.zip")

        sent_request = urlopen.call_args.args[0]
        assert sent_request.get_header("Cookie") == "ASP.NET_SessionId=sess123; auth=tok"
        # The cookie must be an unredirected header so urllib does not replay it across a cross-host
        # redirect (session-cookie leak): urllib copies req.headers on redirect, not unredirected_hdrs.
        assert "Cookie" not in sent_request.headers
        assert sent_request.unredirected_hdrs.get("Cookie") == "ASP.NET_SessionId=sess123; auth=tok"
        saved = list(tmp_path.iterdir())
        assert [p.name for p in saved] == ["statement.zip"]
        assert saved[0].read_bytes() == zip_bytes

    @pytest.mark.asyncio
    async def test_cookie_header_skips_control_char_values(self, tmp_path: Path) -> None:
        # A stored cookie value with CR/LF must not inject extra directives into the Cookie line.
        interceptor = CDPDownloadInterceptor(output_dir=str(tmp_path))
        interceptor._browser_context = self._context_forcing_urllib(
            [{"name": "good", "value": "ok"}, {"name": "bad", "value": "x\r\nSet-Cookie: evil=1"}]
        )
        urlopen = self._fake_urlopen(b"PK\x03\x04 zip payload", "application/zip")

        with patch(self._URLOPEN, urlopen):
            await interceptor._download_url_directly("https://site.example/download?f=statement.zip", "statement.zip")

        assert urlopen.call_args.args[0].get_header("Cookie") == "good=ok"

    @pytest.mark.asyncio
    async def test_html_login_page_for_binary_filename_not_saved(self, tmp_path: Path) -> None:
        interceptor = CDPDownloadInterceptor(output_dir=str(tmp_path))
        interceptor._browser_context = self._context_forcing_urllib([])
        urlopen = self._fake_urlopen(self._LOGIN_HTML, "text/html; charset=utf-8")

        with patch(self._URLOPEN, urlopen):
            await interceptor._download_url_directly("https://site.example/download?f=statement.zip", "statement.zip")

        assert list(tmp_path.iterdir()) == []

    @pytest.mark.asyncio
    async def test_html_login_page_for_encoded_binary_filename_not_saved(self, tmp_path: Path) -> None:
        interceptor = CDPDownloadInterceptor(output_dir=str(tmp_path))
        interceptor._browser_context = self._context_forcing_urllib([])
        urlopen = self._fake_urlopen(self._LOGIN_HTML, "text/html; charset=utf-8")

        with patch(self._URLOPEN, urlopen):
            await interceptor._download_url_directly("https://site.example/download?f=statement.zip", "statement%2Ezip")

        assert list(tmp_path.iterdir()) == []

    @pytest.mark.asyncio
    async def test_html_payload_for_encoded_html_filename_is_saved(self, tmp_path: Path) -> None:
        interceptor = CDPDownloadInterceptor(output_dir=str(tmp_path))
        interceptor._browser_context = self._context_forcing_urllib([])
        urlopen = self._fake_urlopen(b"<!DOCTYPE html><html><body>report</body></html>", "text/html")

        with patch(self._URLOPEN, urlopen):
            await interceptor._download_url_directly("https://site.example/report", "report%2Ehtml")

        assert [path.name for path in tmp_path.iterdir()] == ["report.html"]

    @pytest.mark.asyncio
    async def test_html_payload_for_double_encoded_binary_filename_is_not_saved(self, tmp_path: Path) -> None:
        interceptor = CDPDownloadInterceptor(output_dir=str(tmp_path))
        interceptor._browser_context = self._context_forcing_urllib([])
        urlopen = self._fake_urlopen(self._LOGIN_HTML, "text/html")

        with patch(self._URLOPEN, urlopen):
            await interceptor._download_url_directly(
                "https://site.example/download?f=statement%252Ezip", "statement%252Ezip"
            )

        assert list(tmp_path.iterdir()) == []

    @pytest.mark.asyncio
    async def test_html_body_without_html_content_type_still_rejected(self, tmp_path: Path) -> None:
        # Some servers mislabel the login page's content-type; sniff the body markup too.
        interceptor = CDPDownloadInterceptor(output_dir=str(tmp_path))
        interceptor._browser_context = self._context_forcing_urllib([])
        urlopen = self._fake_urlopen(self._LOGIN_HTML, "application/octet-stream")

        with patch(self._URLOPEN, urlopen):
            await interceptor._download_url_directly("https://site.example/download?f=statement.zip", "statement.zip")

        assert list(tmp_path.iterdir()) == []

    @pytest.mark.parametrize(
        "body",
        [
            pytest.param(b"<!-- generated --><!DOCTYPE html><html><body>login</body></html>", id="comment"),
            pytest.param(b'<?xml version="1.0"?><html><body>login</body></html>', id="xml-declaration"),
            pytest.param(b"<head><title>Login</title></head><body>login</body>", id="omitted-html-root"),
            pytest.param(b"<head\n><title>Login</title></head><body>login</body>", id="tag-newline"),
        ],
    )
    @pytest.mark.asyncio
    async def test_html_body_with_legal_leading_markup_is_rejected(self, tmp_path: Path, body: bytes) -> None:
        interceptor = CDPDownloadInterceptor(output_dir=str(tmp_path))
        interceptor._browser_context = self._context_forcing_urllib([])
        urlopen = self._fake_urlopen(body, "application/octet-stream")

        with patch(self._URLOPEN, urlopen):
            await interceptor._download_url_directly("https://site.example/download", "statement.zip")

        assert list(tmp_path.iterdir()) == []

    @pytest.mark.asyncio
    async def test_real_binary_payload_is_saved(self, tmp_path: Path) -> None:
        # Guard must not over-reject genuine binary downloads.
        interceptor = CDPDownloadInterceptor(output_dir=str(tmp_path))
        interceptor._browser_context = self._context_forcing_urllib([])
        pdf = b"%PDF-1.7 real invoice bytes"
        urlopen = self._fake_urlopen(pdf, "application/pdf")

        with patch(self._URLOPEN, urlopen):
            await interceptor._download_url_directly("https://site.example/download?i=1", "invoice.pdf")

        saved = list(tmp_path.iterdir())
        assert [p.name for p in saved] == ["invoice.pdf"]
        assert saved[0].read_bytes() == pdf

    @pytest.mark.asyncio
    async def test_html_payload_for_html_filename_is_saved(self, tmp_path: Path) -> None:
        # An HTML document downloaded under an .html name is honest, not a masquerade — keep it.
        interceptor = CDPDownloadInterceptor(output_dir=str(tmp_path))
        interceptor._browser_context = self._context_forcing_urllib([])
        urlopen = self._fake_urlopen(b"<!DOCTYPE html><html><body>report</body></html>", "text/html")

        with patch(self._URLOPEN, urlopen):
            await interceptor._download_url_directly("https://site.example/report", "report.html")

        saved = list(tmp_path.iterdir())
        assert [p.name for p in saved] == ["report.html"]

    @pytest.mark.asyncio
    async def test_http_browser_download_routes_through_direct_download(self, tmp_path: Path) -> None:
        # Real wiring: an http(s) browser download goes through _download_url_directly, which
        # forwards cookies and rejects the HTML login masquerade.
        interceptor = CDPDownloadInterceptor(output_dir=str(tmp_path))
        interceptor._browser_context = self._context_forcing_urllib([{"name": "sid", "value": "x"}])
        urlopen = self._fake_urlopen(self._LOGIN_HTML, "text/html")

        with patch(self._URLOPEN, urlopen):
            await interceptor._handle_browser_download(
                {
                    "url": "https://site.example/download?f=report.zip",
                    "suggestedFilename": "report.zip",
                }
            )

        assert list(tmp_path.iterdir()) == []
        assert urlopen.call_args.args[0].get_header("Cookie") == "sid=x"

    @pytest.mark.asyncio
    async def test_nameless_binary_content_type_html_body_not_saved(self, tmp_path: Path) -> None:
        # No filename extension but a binary content-type + HTML body is still a masquerade.
        interceptor = CDPDownloadInterceptor(output_dir=str(tmp_path))
        interceptor._browser_context = self._context_forcing_urllib([])
        urlopen = self._fake_urlopen(self._LOGIN_HTML, "application/octet-stream")

        with patch(self._URLOPEN, urlopen):
            await interceptor._download_url_directly("https://site.example/download", "")

        assert list(tmp_path.iterdir()) == []

    @pytest.mark.asyncio
    async def test_nameless_html_content_type_is_saved(self, tmp_path: Path) -> None:
        # A nameless download that is honestly HTML (html content-type, no binary claim) is kept.
        interceptor = CDPDownloadInterceptor(output_dir=str(tmp_path))
        interceptor._browser_context = self._context_forcing_urllib([])
        urlopen = self._fake_urlopen(self._LOGIN_HTML, "text/html; charset=utf-8")

        with patch(self._URLOPEN, urlopen):
            await interceptor._download_url_directly("https://site.example/page", "")

        assert len(list(tmp_path.iterdir())) == 1

    @pytest.mark.asyncio
    async def test_extensionless_named_html_login_page_is_not_saved(self, tmp_path: Path) -> None:
        interceptor = CDPDownloadInterceptor(output_dir=str(tmp_path))
        interceptor._browser_context = self._context_forcing_urllib([])
        urlopen = self._fake_urlopen(self._LOGIN_HTML, "text/html; charset=utf-8")

        with patch(self._URLOPEN, urlopen):
            await interceptor._download_url_directly("https://site.example/download", "statement")

        assert list(tmp_path.iterdir()) == []

    @pytest.mark.asyncio
    async def test_binary_payload_mislabeled_as_html_is_saved(self, tmp_path: Path) -> None:
        # A real binary payload a server mislabels as text/html must be saved, not discarded:
        # the body is the ground truth, so a non-HTML body is never treated as a masquerade.
        interceptor = CDPDownloadInterceptor(output_dir=str(tmp_path))
        interceptor._browser_context = self._context_forcing_urllib([])
        zip_bytes = b"PK\x03\x04 genuine archive, not html"
        urlopen = self._fake_urlopen(zip_bytes, "text/html")

        with patch(self._URLOPEN, urlopen):
            await interceptor._download_url_directly("https://site.example/download?f=statement.zip", "statement.zip")

        saved = list(tmp_path.iterdir())
        assert [p.name for p in saved] == ["statement.zip"]
        assert saved[0].read_bytes() == zip_bytes

    @pytest.mark.asyncio
    async def test_nameless_no_content_type_html_is_saved(self, tmp_path: Path) -> None:
        # Nameless download with no content-type and an HTML body makes no binary claim, so it is
        # saved as-is (intentional "no claim, no mismatch"), not rejected.
        interceptor = CDPDownloadInterceptor(output_dir=str(tmp_path))
        interceptor._browser_context = self._context_forcing_urllib([])
        urlopen = self._fake_urlopen(self._LOGIN_HTML, "")

        with patch(self._URLOPEN, urlopen):
            await interceptor._download_url_directly("https://site.example/page", "")

        assert len(list(tmp_path.iterdir())) == 1


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
        interceptor = CDPDownloadInterceptor(output_dir=str(dir_a))
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
        interceptor = CDPDownloadInterceptor(output_dir=str(tmp_path))
        event = {"url": "data:text/plain,hello", "suggestedFilename": "note.txt"}

        await interceptor._handle_browser_download(event)
        assert interceptor._download_index == 1

        interceptor.set_download_dir(str(tmp_path))
        await interceptor._handle_browser_download(event)

        assert interceptor._download_index == 1
        assert [path.name for path in tmp_path.iterdir()] == ["note.txt"]

    def test_real_dir_change_clears_cross_path_dedupe(self, tmp_path: Path) -> None:
        interceptor = CDPDownloadInterceptor(output_dir=str(tmp_path / "run_a"))
        interceptor._downloaded_urls.update(
            {"https://site.example/report.pdf", "blob:https://site.example/abc", "data:sha256:deadbeef"}
        )

        interceptor.set_download_dir(str(tmp_path / "run_b"))

        assert interceptor._downloaded_urls == set()

    def test_same_dir_set_preserves_cross_path_dedupe(self, tmp_path: Path) -> None:
        interceptor = CDPDownloadInterceptor(output_dir=str(tmp_path))
        seeded = {"https://site.example/report.pdf", "blob:https://site.example/abc"}
        interceptor._downloaded_urls.update(seeded)

        interceptor.set_download_dir(str(tmp_path))

        assert interceptor._downloaded_urls == seeded

    def test_first_dir_set_from_none_does_not_touch_dedupe(self, tmp_path: Path) -> None:
        interceptor = CDPDownloadInterceptor()
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
        interceptor = CDPDownloadInterceptor(output_dir=str(dir_a))
        event = {"url": "data:text/plain,hello", "suggestedFilename": "note.txt"}

        entered_replace, release_replace, replace_patch = TestDataUrlDownloadCapture._paused_replace()
        with replace_patch:
            writing = asyncio.create_task(interceptor._handle_browser_download(event))
            assert await asyncio.to_thread(entered_replace.wait, 2)
            interceptor.set_download_dir(str(dir_b))
            assert interceptor._downloaded_urls == set()
            release_replace.set()
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
        interceptor = CDPDownloadInterceptor(output_dir=str(tmp_path / "run_a"))
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
        interceptor = CDPDownloadInterceptor(output_dir=str(tmp_path))
        payload = b"A" * 500
        cdp = _StreamCDP(chunks=[payload])
        with _stream_limits():
            await _drive_download(interceptor, cdp, _raw_headers(content_length=500))

        assert _only_file(tmp_path).read_bytes() == payload
        assert _fulfill_body(cdp) == base64.b64encode(payload).decode()  # full-body replay preserved
        assert cdp.count("Fetch.getResponseBody") == 0
        assert cdp.count("Fetch.failRequest") == 0

    @pytest.mark.asyncio
    async def test_at_threshold_streams_to_disk_empty_fulfill(self, tmp_path: Path) -> None:
        interceptor = CDPDownloadInterceptor(output_dir=str(tmp_path))
        payload = b"B" * 1024  # exactly the threshold → large path
        cdp = _StreamCDP(chunks=[payload])
        with _stream_limits(threshold=1024):
            await _drive_download(interceptor, cdp, _raw_headers(content_length=1024))

        assert _only_file(tmp_path).read_bytes() == payload
        assert _fulfill_body(cdp) == ""  # empty body, no base64 replay
        assert _fulfill_headers(cdp).get("content-length") == "0"
        assert cdp.count("Fetch.failRequest") == 0

    @pytest.mark.asyncio
    async def test_above_threshold_known_large_streams(self, tmp_path: Path) -> None:
        interceptor = CDPDownloadInterceptor(output_dir=str(tmp_path))
        payload = b"C" * 2000
        cdp = _StreamCDP(chunks=[b"C" * 800, b"C" * 800, b"C" * 400])
        with _stream_limits(threshold=1024, cap=4096):
            await _drive_download(interceptor, cdp, _raw_headers(content_length=2000))

        assert _only_file(tmp_path).read_bytes() == payload
        assert _fulfill_body(cdp) == ""

    @pytest.mark.asyncio
    async def test_unknown_content_length_spills_to_disk(self, tmp_path: Path) -> None:
        interceptor = CDPDownloadInterceptor(output_dir=str(tmp_path))
        # No Content-Length: must not assume small. Buffers, then spills once bytes cross the threshold.
        chunks = [b"D" * 400, b"D" * 400, b"D" * 400, b"D" * 400, b"D" * 400]  # 2000 total
        cdp = _StreamCDP(chunks=list(chunks))
        with _stream_limits(threshold=1024, cap=4096):
            await _drive_download(interceptor, cdp, _raw_headers(content_length=None))

        assert _only_file(tmp_path).read_bytes() == b"D" * 2000  # early buffered bytes preserved on spill
        assert _fulfill_body(cdp) == ""

    @pytest.mark.asyncio
    async def test_lying_small_content_length_spills(self, tmp_path: Path) -> None:
        interceptor = CDPDownloadInterceptor(output_dir=str(tmp_path))
        # Header claims 100 bytes but the body is 2000 → must spill to the large path, not trust the header.
        cdp = _StreamCDP(chunks=[b"E" * 700, b"E" * 700, b"E" * 600])
        with _stream_limits(threshold=1024, cap=4096):
            await _drive_download(interceptor, cdp, _raw_headers(content_length=100))

        assert _only_file(tmp_path).read_bytes() == b"E" * 2000
        assert _fulfill_body(cdp) == ""

    @pytest.mark.asyncio
    async def test_lying_large_content_length_tiny_body_finalizes(self, tmp_path: Path) -> None:
        interceptor = CDPDownloadInterceptor(output_dir=str(tmp_path))
        # Header claims large (>= threshold) → disk path from chunk 1, but the actual body is tiny.
        cdp = _StreamCDP(chunks=[b"F" * 300])
        with _stream_limits(threshold=1024, cap=4096):
            await _drive_download(interceptor, cdp, _raw_headers(content_length=2000))

        assert _only_file(tmp_path).read_bytes() == b"F" * 300
        assert _fulfill_body(cdp) == ""
        assert cdp.count("Fetch.failRequest") == 0

    @pytest.mark.asyncio
    async def test_cap_breach_unknown_cl_aborts_no_artifact(self, tmp_path: Path) -> None:
        interceptor = CDPDownloadInterceptor(output_dir=str(tmp_path))
        cdp = _StreamCDP(chunks=[b"G" * 1500, b"G" * 1500, b"G" * 1500, b"G" * 1500])  # 6000 > cap 4096
        with _stream_limits(threshold=1024, cap=4096):
            await _drive_download(interceptor, cdp, _raw_headers(content_length=None))

        assert list(tmp_path.iterdir()) == []  # no artifact, no leftover .crdownload temp
        assert cdp.count("Fetch.failRequest") == 1
        assert cdp.count("Fetch.fulfillRequest") == 0

    @pytest.mark.asyncio
    async def test_header_content_length_over_cap_fast_fails(self, tmp_path: Path) -> None:
        interceptor = CDPDownloadInterceptor(output_dir=str(tmp_path))
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
        interceptor = CDPDownloadInterceptor(output_dir=str(tmp_path))
        cdp = _StreamCDP(chunks=[b"H" * 10])
        url = "https://example.com/huge.pdf"
        with _stream_limits(threshold=1024, cap=4096):
            await _drive_download(interceptor, cdp, _raw_headers(content_length=5000), url=url)  # > cap

        assert cdp.count("Fetch.failRequest") == 1
        assert interceptor._downloaded_urls == {url}

    @pytest.mark.asyncio
    async def test_midstream_read_error_cleans_temp_and_fails(self, tmp_path: Path) -> None:
        interceptor = CDPDownloadInterceptor(output_dir=str(tmp_path))
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
        interceptor = CDPDownloadInterceptor(output_dir=str(tmp_path))
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
        interceptor = CDPDownloadInterceptor(output_dir=str(tmp_path))
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
        interceptor = CDPDownloadInterceptor(output_dir=str(tmp_path))
        cdp = _StreamCDP(take_error=RuntimeError("takeResponseBodyAsStream unavailable"), getbody=b"L" * 300)
        with _stream_limits(threshold=1024, cap=4096):
            await _drive_download(interceptor, cdp, _raw_headers(content_length=300))

        assert list(tmp_path.iterdir()) == []  # nothing materialized or persisted
        assert cdp.count("Fetch.getResponseBody") == 0  # whole-body fallback removed
        assert cdp.count("Fetch.failRequest") == 1
        assert cdp.count("Fetch.fulfillRequest") == 0

    @pytest.mark.asyncio
    async def test_stream_start_fail_with_content_encoding_no_direct_fallback(self, tmp_path: Path) -> None:
        interceptor = CDPDownloadInterceptor(output_dir=str(tmp_path))
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
        interceptor = CDPDownloadInterceptor(output_dir=str(tmp_path))
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
        interceptor = CDPDownloadInterceptor(output_dir=str(tmp_path))
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
        interceptor = CDPDownloadInterceptor(output_dir=str(tmp_path))
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
        interceptor = CDPDownloadInterceptor(output_dir=str(tmp_path))
        cdp = _StreamCDP(chunks=[b"C" * 2000], close_hang=True)  # IO.close never returns
        with _stream_limits(threshold=1024, cap=4096), patch.object(mod, "STREAM_IO_READ_TIMEOUT_SECONDS", 0.05):
            await asyncio.wait_for(_drive_download(interceptor, cdp, _raw_headers(content_length=2000)), timeout=5)
        assert _only_file(tmp_path).read_bytes() == b"C" * 2000  # saved despite the hung close
        assert not interceptor._download_extraction_lock.locked()

    @pytest.mark.asyncio
    async def test_settle_cancellation_does_not_cancel_capture(self, tmp_path: Path) -> None:
        # A settle-timeout cancellation must NOT cancel the in-flight/queued capture handlers (gather would,
        # destroying the temp mid-stream); the capture keeps running and its artifact still lands.
        interceptor = CDPDownloadInterceptor(output_dir=str(tmp_path))
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
        interceptor = CDPDownloadInterceptor(output_dir=str(tmp_path))
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
        interceptor = CDPDownloadInterceptor(output_dir=str(tmp_path))
        cdp = _StreamCDP(chunks=[b"F" * 2000], fulfill_hang=True)  # fulfillRequest never returns
        with _stream_limits(threshold=1024, cap=4096), patch.object(mod, "STREAM_IO_READ_TIMEOUT_SECONDS", 0.05):
            await asyncio.wait_for(_drive_download(interceptor, cdp, _raw_headers(content_length=2000)), timeout=5)
        assert _only_file(tmp_path).read_bytes() == b"F" * 2000  # streamed artifact saved despite hung fulfill
        assert not interceptor._download_extraction_lock.locked()

    @pytest.mark.asyncio
    async def test_stalled_read_times_out_and_releases_lock(self, tmp_path: Path) -> None:
        # A stalled IO.read must not hold the extraction lock forever (which would deadlock later captures
        # and hang teardown's drain). The per-read timeout aborts it, cleans the temp, and releases the lock.
        interceptor = CDPDownloadInterceptor(output_dir=str(tmp_path))
        cdp = _StreamCDP(read_hang=True)  # IO.read never returns
        with _stream_limits(threshold=1024, cap=4096), patch.object(mod, "STREAM_IO_READ_TIMEOUT_SECONDS", 0.05):
            # Outer bound so a regression (no per-read timeout) fails fast instead of hanging the suite.
            await asyncio.wait_for(_drive_download(interceptor, cdp, _raw_headers(content_length=2000)), timeout=5)
        assert cdp.count("Fetch.failRequest") == 1
        assert list(tmp_path.iterdir()) == []  # temp cleaned up, no artifact
        assert not interceptor._download_extraction_lock.locked()

    @pytest.mark.asyncio
    async def test_stream_start_fail_unknown_cl_no_direct_fallback(self, tmp_path: Path) -> None:
        interceptor = CDPDownloadInterceptor(output_dir=str(tmp_path))
        cdp = _StreamCDP(take_error=RuntimeError("take failed"), getbody=b"M" * 300)
        with _stream_limits(threshold=1024, cap=4096):
            await _drive_download(interceptor, cdp, _raw_headers(content_length=None))

        assert list(tmp_path.iterdir()) == []
        assert cdp.count("Fetch.getResponseBody") == 0  # unbounded getResponseBody not safe on unknown size
        assert cdp.count("Fetch.continueResponse") == 0  # never let the browser materialize a large body
        assert cdp.count("Fetch.failRequest") == 1

    @pytest.mark.asyncio
    async def test_os_replace_failure_cleans_temp_and_fails(self, tmp_path: Path) -> None:
        interceptor = CDPDownloadInterceptor(output_dir=str(tmp_path))
        cdp = _StreamCDP(chunks=[b"N" * 2000])
        with _stream_limits(threshold=1024, cap=4096):
            with patch.object(mod.os, "replace", side_effect=OSError("rename failed")):
                await _drive_download(interceptor, cdp, _raw_headers(content_length=2000))

        assert list(tmp_path.iterdir()) == []  # temp cleaned even when finalize fails
        assert cdp.count("Fetch.fulfillRequest") == 0
        assert cdp.count("Fetch.failRequest") == 1

    @pytest.mark.asyncio
    async def test_read_data_and_eof_in_same_message_not_dropped(self, tmp_path: Path) -> None:
        interceptor = CDPDownloadInterceptor(output_dir=str(tmp_path))
        # Single IO.read returns the whole body AND eof in one message; the last chunk must not be lost.
        cdp = _StreamCDP(chunks=[b"O" * 900])
        with _stream_limits(threshold=1024, cap=4096):
            await _drive_download(interceptor, cdp, _raw_headers(content_length=900))

        assert _only_file(tmp_path).read_bytes() == b"O" * 900
        assert cdp.count("IO.read") == 1

    @pytest.mark.asyncio
    async def test_zero_byte_download_saved_and_replayed(self, tmp_path: Path) -> None:
        interceptor = CDPDownloadInterceptor(output_dir=str(tmp_path))
        cdp = _StreamCDP(chunks=[])  # immediate eof, no data
        with _stream_limits(threshold=1024, cap=4096):
            await _drive_download(interceptor, cdp, _raw_headers(content_length=0))

        assert _only_file(tmp_path).read_bytes() == b""
        assert cdp.count("Fetch.fulfillRequest") == 1
        assert cdp.count("Fetch.failRequest") == 0

    @pytest.mark.asyncio
    async def test_stale_interception_on_fulfill_is_benign_after_save(self, tmp_path: Path) -> None:
        interceptor = CDPDownloadInterceptor(output_dir=str(tmp_path))
        stale = Exception("Protocol error (Fetch.fulfillRequest): Invalid InterceptionId")
        cdp = _StreamCDP(chunks=[b"P" * 2000], fulfill_error=stale)
        with _stream_limits(threshold=1024, cap=4096):
            with capture_logs() as logs:
                await _drive_download(interceptor, cdp, _raw_headers(content_length=2000))

        assert _only_file(tmp_path).read_bytes() == b"P" * 2000  # file saved despite the benign fulfill race
        assert not [log for log in logs if log.get("log_level") == "error"]
