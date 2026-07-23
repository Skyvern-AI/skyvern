"""Tests for extra HTTP header sanitization in browser_factory.py.

A malformed header name passed through extra_http_headers makes Chromium reject the
whole batch via Network.setExtraHTTPHeaders ("Invalid header name"), which fails browser
context creation outright (SKY-8929). sanitize_browser_headers drops the bad names so the
launch survives with the valid headers intact.
"""

from skyvern.webeye.browser_factory import FRESH_CONTEXT_HEADER, parse_extra_headers, sanitize_browser_headers


class TestSanitizeBrowserHeaders:
    def test_none_passes_through(self) -> None:
        assert sanitize_browser_headers(None) is None

    def test_empty_returns_none(self) -> None:
        assert sanitize_browser_headers({}) is None

    def test_valid_headers_unchanged(self) -> None:
        headers = {"X-Custom-Header": "value", "Authorization": "Bearer abc", "Accept": "application/json"}
        assert sanitize_browser_headers(headers) == headers

    def test_token_special_characters_allowed(self) -> None:
        headers = {"X-Foo_Bar.Baz!#$%&'*+^`|~-": "ok"}
        assert sanitize_browser_headers(headers) == headers

    def test_drops_header_name_with_space(self) -> None:
        result = sanitize_browser_headers({"Invalid Header": "v", "Valid-Header": "keep"})
        assert result == {"Valid-Header": "keep"}

    def test_drops_header_name_with_colon_or_newline(self) -> None:
        result = sanitize_browser_headers({"Bad:Name": "v", "Bad\nName": "v", "Good-Name": "keep"})
        assert result == {"Good-Name": "keep"}

    def test_drops_empty_header_name(self) -> None:
        result = sanitize_browser_headers({"": "v", "Good": "keep"})
        assert result == {"Good": "keep"}

    def test_all_invalid_collapses_to_none(self) -> None:
        assert sanitize_browser_headers({"bad name": "v", "": "w"}) is None

    def test_drops_name_with_trailing_newline(self) -> None:
        # `$` matches before a trailing newline, so this must use fullmatch to be dropped.
        result = sanitize_browser_headers({"X-Custom\n": "v", "X-Ok": "keep"})
        assert result == {"X-Ok": "keep"}

    def test_drops_value_with_crlf(self) -> None:
        result = sanitize_browser_headers({"X-Bad": "ok\r\nInjected: evil", "X-Ok": "keep"})
        assert result == {"X-Ok": "keep"}

    def test_drops_value_with_newline_or_null(self) -> None:
        result = sanitize_browser_headers({"X-NL": "a\nb", "X-Null": "a\x00b", "X-Ok": "keep"})
        assert result == {"X-Ok": "keep"}


class TestParseExtraHeaders:
    def test_enable_download_true(self) -> None:
        assert parse_extra_headers({"enable_download": "true"}).enable_download is True
        assert parse_extra_headers({"enable_download": "TRUE"}).enable_download is True

    def test_enable_download_false_values_are_not_truthy(self) -> None:
        # Regression: the value was passed through bool(str), so any non-empty
        # string (including "false"/"0"/"no") enabled downloads.
        for falsy in ("false", "FALSE", "0", "no", ""):
            result = parse_extra_headers({"enable_download": falsy})
            assert result.enable_download is False, falsy

    def test_enable_download_defaults_false_when_absent(self) -> None:
        assert parse_extra_headers({"X-Other": "v"}).enable_download is False
        assert parse_extra_headers(None).enable_download is False

    def test_internal_headers_are_stripped_from_output(self) -> None:
        result = parse_extra_headers({"enable_download": "true", FRESH_CONTEXT_HEADER: "true", "X-Keep": "v"})
        assert result.headers == {"X-Keep": "v"}
        assert result.enable_download is True
        assert result.use_fresh_context is True

    def test_fresh_context_header_parses_boolean_text(self) -> None:
        assert parse_extra_headers({FRESH_CONTEXT_HEADER: "true"}).use_fresh_context is True
        assert parse_extra_headers({FRESH_CONTEXT_HEADER: "false"}).use_fresh_context is False
