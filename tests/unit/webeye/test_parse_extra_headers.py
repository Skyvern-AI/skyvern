from skyvern.webeye.browser_factory import parse_extra_headers


def test_parse_extra_headers_enable_download_false_string_is_falsy() -> None:
    parsed = parse_extra_headers({"enable_download": "false"})
    assert parsed.enable_download is False


def test_parse_extra_headers_enable_download_true_string_is_truthy() -> None:
    parsed = parse_extra_headers({"enable_download": "true"})
    assert parsed.enable_download is True


def test_parse_extra_headers_enable_download_case_insensitive() -> None:
    assert parse_extra_headers({"enable_download": "False"}).enable_download is False
    assert parse_extra_headers({"enable_download": "TRUE"}).enable_download is True


def test_parse_extra_headers_enable_download_missing_defaults_false() -> None:
    parsed = parse_extra_headers({"other-header": "value"})
    assert parsed.enable_download is False


def test_parse_extra_headers_enable_download_stripped_from_headers() -> None:
    parsed = parse_extra_headers({"enable_download": "false", "other-header": "value"})
    assert "enable_download" not in parsed.headers
    assert parsed.headers == {"other-header": "value"}
