"""Unit tests for CDPDownloadInterceptor pure functions and proxy auth handling."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.webeye.cdp_download_interceptor import CDPDownloadInterceptor, extract_filename, is_download_response


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
