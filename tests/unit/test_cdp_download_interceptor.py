"""Unit tests for CDPDownloadInterceptor pure functions and proxy auth handling."""

import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.webeye.cdp_download_interceptor import CDPDownloadInterceptor, extract_filename, is_download_response


class TestIsDownloadResponse:
    """Tests for is_download_response()."""

    def test_attachment_header(self) -> None:
        headers = {"content-disposition": 'attachment; filename="report.csv"', "content-type": "text/csv"}
        assert is_download_response(headers, 200) is True

    def test_attachment_header_case_insensitive(self) -> None:
        headers = {"content-disposition": 'Attachment; filename="report.csv"', "content-type": "text/csv"}
        assert is_download_response(headers, 200) is True

    def test_download_mime_type_pdf(self) -> None:
        headers = {"content-type": "application/pdf"}
        assert is_download_response(headers, 200) is True

    def test_download_mime_type_zip(self) -> None:
        headers = {"content-type": "application/zip"}
        assert is_download_response(headers, 200) is True

    def test_download_mime_type_xlsx(self) -> None:
        headers = {
            "content-type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        }
        assert is_download_response(headers, 200) is True

    def test_download_mime_type_octet_stream(self) -> None:
        headers = {"content-type": "application/octet-stream"}
        assert is_download_response(headers, 200) is True

    def test_download_mime_type_with_charset(self) -> None:
        headers = {"content-type": "application/pdf; charset=utf-8"}
        assert is_download_response(headers, 200) is True

    def test_html_not_download(self) -> None:
        headers = {"content-type": "text/html"}
        assert is_download_response(headers, 200) is False

    def test_json_not_download(self) -> None:
        headers = {"content-type": "application/json"}
        assert is_download_response(headers, 200) is False

    def test_json_with_attachment_not_download(self) -> None:
        """JSON responses with Content-Disposition: attachment should NOT be treated as downloads."""
        headers = {
            "content-disposition": "attachment",
            "content-type": "application/json",
        }
        assert is_download_response(headers, 200) is False

    def test_xml_not_download(self) -> None:
        headers = {"content-type": "application/xml"}
        assert is_download_response(headers, 200) is False

    def test_grpc_not_download(self) -> None:
        headers = {"content-type": "application/grpc"}
        assert is_download_response(headers, 200) is False

    def test_empty_headers_not_download(self) -> None:
        assert is_download_response({}, 200) is False

    # Resource type filtering — XHR/Fetch require BOTH attachment AND download MIME
    def test_xhr_attachment_and_download_mime_is_download(self) -> None:
        """XHR with attachment + download MIME type should be treated as download."""
        headers = {"content-disposition": "attachment", "content-type": "application/octet-stream"}
        assert is_download_response(headers, 200, resource_type="XHR") is True

    def test_fetch_attachment_and_download_mime_is_download(self) -> None:
        """Fetch with attachment + download MIME type should be treated as download."""
        headers = {"content-disposition": "attachment", "content-type": "application/pdf"}
        assert is_download_response(headers, 200, resource_type="Fetch") is True

    def test_xhr_attachment_pdf_is_download(self) -> None:
        """Real-world case: XHR download with attachment header and PDF content-type."""
        headers = {
            "content-disposition": "attachment; filename=Invoice_12345.pdf; filename*=UTF-8''Invoice_12345.pdf",
            "content-type": "application/pdf",
        }
        assert is_download_response(headers, 200, resource_type="XHR") is True

    # XHR/Fetch with attachment but non-download MIME should NOT be download
    def test_xhr_attachment_text_plain_not_download(self) -> None:
        """Google-style XHR: text/plain + attachment should NOT be treated as download."""
        headers = {"content-disposition": 'attachment; filename="f.txt"', "content-type": "text/plain; charset=UTF-8"}
        assert is_download_response(headers, 200, resource_type="XHR") is False

    def test_xhr_attachment_text_html_not_download(self) -> None:
        """XHR with text/html + attachment should NOT be treated as download."""
        headers = {"content-disposition": "attachment", "content-type": "text/html"}
        assert is_download_response(headers, 200, resource_type="XHR") is False

    def test_fetch_attachment_only_not_download(self) -> None:
        """Fetch with attachment but no download MIME type should NOT be download."""
        headers = {"content-disposition": "attachment"}
        assert is_download_response(headers, 200, resource_type="Fetch") is False

    def test_xhr_attachment_csv_not_download(self) -> None:
        """Known limitation: CSV via XHR is not detected because text/csv is not in DOWNLOAD_MIME_TYPES."""
        headers = {"content-disposition": "attachment", "content-type": "text/csv"}
        assert is_download_response(headers, 200, resource_type="XHR") is False

    # XHR/Fetch without attachment header should NOT be download (MIME-only false positive)
    def test_xhr_mime_only_not_download(self) -> None:
        """XHR with download MIME type but no attachment header should NOT be treated as download."""
        headers = {"content-type": "application/pdf"}
        assert is_download_response(headers, 200, resource_type="XHR") is False

    def test_fetch_mime_only_not_download(self) -> None:
        """Fetch with download MIME type but no attachment header should NOT be treated as download."""
        headers = {"content-type": "application/octet-stream"}
        assert is_download_response(headers, 200, resource_type="Fetch") is False

    # XHR/Fetch with attachment but API content-type should still be filtered
    def test_xhr_json_attachment_not_download(self) -> None:
        """XHR with JSON content-type and attachment header should NOT be download."""
        headers = {"content-disposition": "attachment", "content-type": "application/json"}
        assert is_download_response(headers, 200, resource_type="XHR") is False

    def test_font_resource_type_not_download(self) -> None:
        headers = {"content-type": "application/octet-stream"}
        assert is_download_response(headers, 200, resource_type="Font") is False

    def test_stylesheet_resource_type_not_download(self) -> None:
        headers = {"content-type": "application/octet-stream"}
        assert is_download_response(headers, 200, resource_type="Stylesheet") is False

    def test_script_resource_type_not_download(self) -> None:
        headers = {"content-type": "application/octet-stream"}
        assert is_download_response(headers, 200, resource_type="Script") is False

    def test_image_resource_type_not_download(self) -> None:
        headers = {"content-type": "application/octet-stream"}
        assert is_download_response(headers, 200, resource_type="Image") is False

    def test_document_resource_type_is_download(self) -> None:
        """Document resource type (link click) should allow download detection."""
        headers = {"content-disposition": "attachment", "content-type": "application/pdf"}
        assert is_download_response(headers, 200, resource_type="Document") is True

    def test_empty_resource_type_is_download(self) -> None:
        headers = {"content-type": "application/pdf"}
        assert is_download_response(headers, 200, resource_type="") is True

    def test_error_status_code_not_download(self) -> None:
        headers = {"content-disposition": "attachment", "content-type": "application/pdf"}
        assert is_download_response(headers, 404) is False

    def test_server_error_not_download(self) -> None:
        headers = {"content-type": "application/octet-stream"}
        assert is_download_response(headers, 500) is False


class TestExtractFilename:
    """Tests for extract_filename()."""

    def test_rfc5987_filename_star(self) -> None:
        headers = {"content-disposition": "attachment; filename*=UTF-8''my%20report%282024%29.pdf"}
        result = extract_filename(headers, "https://example.com/download", 1)
        assert result == "my report(2024).pdf"

    def test_regular_filename(self) -> None:
        headers = {"content-disposition": 'attachment; filename="report.csv"'}
        result = extract_filename(headers, "https://example.com/download", 1)
        assert result == "report.csv"

    def test_unquoted_filename(self) -> None:
        headers = {"content-disposition": "attachment; filename=report.csv"}
        result = extract_filename(headers, "https://example.com/download", 1)
        assert result == "report.csv"

    def test_filename_star_takes_priority(self) -> None:
        headers = {
            "content-disposition": "attachment; filename=\"fallback.csv\"; filename*=UTF-8''preferred.csv",
        }
        result = extract_filename(headers, "https://example.com/download", 1)
        assert result == "preferred.csv"

    def test_url_path_fallback(self) -> None:
        headers: dict[str, str] = {}
        result = extract_filename(headers, "https://example.com/files/document.pdf", 1)
        assert result == "document.pdf"

    def test_url_path_with_encoded_chars(self) -> None:
        headers: dict[str, str] = {}
        result = extract_filename(headers, "https://example.com/files/my%20report.xlsx", 1)
        assert result == "my report.xlsx"

    def test_url_path_no_extension_uses_fallback(self) -> None:
        headers: dict[str, str] = {}
        result = extract_filename(headers, "https://example.com/download", 1)
        assert result.startswith("download_")

    def test_fallback_format(self) -> None:
        headers: dict[str, str] = {}
        before = int(time.time())
        result = extract_filename(headers, "https://example.com/api/export", 42)
        after = int(time.time())
        # Should be download_{timestamp}_{index}
        parts = result.split("_")
        assert parts[0] == "download"
        assert before <= int(parts[1]) <= after
        assert parts[2] == "42"

    def test_empty_content_disposition(self) -> None:
        headers = {"content-disposition": ""}
        result = extract_filename(headers, "https://example.com/files/data.csv", 1)
        assert result == "data.csv"

    def test_content_disposition_inline(self) -> None:
        """inline disposition without filename should fall back to URL."""
        headers = {"content-disposition": "inline"}
        result = extract_filename(headers, "https://example.com/files/report.pdf", 1)
        assert result == "report.pdf"

    def test_path_traversal_stripped(self) -> None:
        """Path traversal in filename should be sanitized to just the filename part."""
        headers = {"content-disposition": 'attachment; filename="../../etc/cron.d/evil"'}
        result = extract_filename(headers, "https://example.com/download", 1)
        # extract_filename returns the raw name; sanitization is done in _handle_download.
        # But verify the raw output so tests document the behavior.
        assert result == "../../etc/cron.d/evil"


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
        """enable_for_page should set handleAuthRequests=True and register authRequired handler."""
        interceptor = self._make_interceptor(proxy_username="user", proxy_password="pass")

        mock_cdp_session = self._make_cdp_session()
        mock_page = MagicMock()
        mock_page.url = "about:blank"
        mock_page.context.new_cdp_session = AsyncMock(return_value=mock_cdp_session)

        await interceptor.enable_for_page(mock_page)

        # Verify Fetch.enable was called with handleAuthRequests=True
        mock_cdp_session.send.assert_called_once_with(
            "Fetch.enable",
            {
                "patterns": [{"requestStage": "Response"}],
                "handleAuthRequests": True,
            },
        )

        # Verify both event handlers were registered
        event_names = [call.args[0] for call in mock_cdp_session.on.call_args_list]
        assert "Fetch.requestPaused" in event_names
        assert "Fetch.authRequired" in event_names

    @pytest.mark.asyncio
    async def test_enable_for_page_without_proxy_auth(self) -> None:
        """enable_for_page without credentials should set handleAuthRequests=False."""
        interceptor = self._make_interceptor()

        mock_cdp_session = self._make_cdp_session()
        mock_page = MagicMock()
        mock_page.url = "about:blank"
        mock_page.context.new_cdp_session = AsyncMock(return_value=mock_cdp_session)

        await interceptor.enable_for_page(mock_page)

        # Verify Fetch.enable was called with handleAuthRequests=False
        mock_cdp_session.send.assert_called_once_with(
            "Fetch.enable",
            {
                "patterns": [{"requestStage": "Response"}],
                "handleAuthRequests": False,
            },
        )

        # Verify only requestPaused handler was registered (not authRequired)
        event_names = [call.args[0] for call in mock_cdp_session.on.call_args_list]
        assert "Fetch.requestPaused" in event_names
        assert "Fetch.authRequired" not in event_names

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
