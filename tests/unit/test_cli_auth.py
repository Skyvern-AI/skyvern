"""Tests for skyvern/cli/auth_command.py"""

from __future__ import annotations

import http.server
import threading
import urllib.parse

from skyvern.cli.auth_command import _CallbackHandler, _derive_api_base_url, _find_free_port


class TestDeriveApiBaseUrl:
    def test_localhost(self) -> None:
        assert _derive_api_base_url("http://localhost:8080") == "http://localhost:8000"

    def test_localhost_no_port(self) -> None:
        assert _derive_api_base_url("http://localhost") == "http://localhost:8000"

    def test_127_0_0_1(self) -> None:
        assert _derive_api_base_url("http://127.0.0.1:5173") == "http://localhost:8000"

    def test_app_skyvern(self) -> None:
        assert _derive_api_base_url("https://app.skyvern.com") == "https://api.skyvern.com"

    def test_app_skyvern_with_port(self) -> None:
        assert _derive_api_base_url("https://app.skyvern.com:8443") == "https://api.skyvern.com:8443"

    def test_unknown_hostname_returns_input(self) -> None:
        result = _derive_api_base_url("https://staging.skyvern.com")
        assert result == "https://staging.skyvern.com"


class TestFindFreePort:
    def test_returns_bound_socket(self) -> None:
        sock = _find_free_port()
        try:
            port = sock.getsockname()[1]
            assert 1024 <= port <= 65535
            # Socket should still be open (bound)
            assert sock.fileno() != -1
        finally:
            sock.close()


class TestCallbackHandlerStateValidation:
    def _make_server(self, state: str) -> http.server.HTTPServer:
        sock = _find_free_port()
        port = sock.getsockname()[1]
        server = http.server.HTTPServer(("127.0.0.1", port), _CallbackHandler, bind_and_activate=False)
        server.socket = sock
        server.server_activate()
        server.auth_result = {"api_key": None, "organization_id": None, "email": None}  # type: ignore[attr-defined]
        server.received_event = threading.Event()  # type: ignore[attr-defined]
        server.expected_state = state  # type: ignore[attr-defined]
        return server

    def test_valid_state_accepted(self) -> None:
        server = self._make_server("test-nonce-123")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        port = server.server_address[1]
        try:
            import http.client

            conn = http.client.HTTPConnection("127.0.0.1", port)
            body = urllib.parse.urlencode(
                {
                    "api_key": "sk_test_key",
                    "organization_id": "o_123",
                    "email": "test@example.com",
                    "state": "test-nonce-123",
                }
            )
            conn.request("POST", "/callback", body=body, headers={"Content-Type": "application/x-www-form-urlencoded"})
            resp = conn.getresponse()
            assert resp.status == 200
            assert server.auth_result["api_key"] == "sk_test_key"  # type: ignore[attr-defined]
            assert server.auth_result["email"] == "test@example.com"  # type: ignore[attr-defined]
            assert server.received_event.wait(timeout=5)  # type: ignore[attr-defined]
            conn.close()
        finally:
            server.shutdown()

    def test_invalid_state_rejected(self) -> None:
        server = self._make_server("correct-nonce")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        port = server.server_address[1]
        try:
            import http.client

            conn = http.client.HTTPConnection("127.0.0.1", port)
            body = urllib.parse.urlencode(
                {
                    "api_key": "sk_test_key",
                    "state": "wrong-nonce",
                }
            )
            conn.request("POST", "/callback", body=body, headers={"Content-Type": "application/x-www-form-urlencoded"})
            resp = conn.getresponse()
            assert resp.status == 403
            assert server.auth_result["api_key"] is None  # type: ignore[attr-defined]
            assert not server.received_event.is_set()  # type: ignore[attr-defined]
            conn.close()
        finally:
            server.shutdown()

    def test_missing_api_key_rejected(self) -> None:
        server = self._make_server("test-nonce")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        port = server.server_address[1]
        try:
            import http.client

            conn = http.client.HTTPConnection("127.0.0.1", port)
            body = urllib.parse.urlencode({"state": "test-nonce"})
            conn.request("POST", "/callback", body=body, headers={"Content-Type": "application/x-www-form-urlencoded"})
            resp = conn.getresponse()
            assert resp.status == 400
            conn.close()
        finally:
            server.shutdown()
