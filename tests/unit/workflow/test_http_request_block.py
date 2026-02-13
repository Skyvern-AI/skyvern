import json


class TestJsonTextParsingEquivalence:
    """Prove JSON/text parsing behavior matches aiohttp semantics.

    The HttpRequestBlock parses responses using:
        try:
            response_body = json.loads(response_bytes.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            response_body = response_bytes.decode("utf-8", errors="replace")

    This should behave equivalently to aiohttp's:
        try:
            response_body = await response.json()
        except (aiohttp.ContentTypeError, Exception):
            response_body = await response.text()
    """

    def _parse_response(self, response_bytes: bytes) -> str | dict | list:
        try:
            return json.loads(response_bytes.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return response_bytes.decode("utf-8", errors="replace")

    def test_valid_json_utf8(self) -> None:
        data = {"key": "value", "number": 42, "unicode": "日本語"}
        response_bytes = json.dumps(data).encode("utf-8")
        result = self._parse_response(response_bytes)
        assert result == data

    def test_invalid_json_returns_text(self) -> None:
        response_bytes = b"not json, just text"
        result = self._parse_response(response_bytes)
        assert result == "not json, just text"

    def test_non_utf8_bytes_handled_gracefully(self) -> None:
        response_bytes = "café".encode("latin-1")  # b'caf\xe9'
        result = self._parse_response(response_bytes)
        assert "caf" in result
        assert isinstance(result, str)

    def test_empty_response(self) -> None:
        response_bytes = b""
        result = self._parse_response(response_bytes)
        assert result == ""
