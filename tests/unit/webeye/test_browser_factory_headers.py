from skyvern.webeye.browser_factory import parse_extra_headers


def test_parse_extra_headers_strips_internal_headers() -> None:
    parsed = parse_extra_headers(
        {
            "X-Skyvern-Fresh-Context": "true",
            "Enable_Download": "true",
            "Accept": "text/html",
        }
    )
    assert parsed.use_fresh_context is True
    assert parsed.enable_download is True
    assert parsed.headers == {"Accept": "text/html"}


def test_parse_extra_headers_enable_download_requires_explicit_true() -> None:
    """Any non-empty header value used to be truthy, so even "false" enabled
    download interception."""
    for value in ("false", "0", "off", "", "no"):
        parsed = parse_extra_headers({"enable_download": value})
        assert parsed.enable_download is False, value


def test_parse_extra_headers_enable_download_case_insensitive() -> None:
    parsed = parse_extra_headers({"ENABLE_DOWNLOAD": "True"})
    assert parsed.enable_download is True


def test_parse_extra_headers_none_and_missing() -> None:
    parsed = parse_extra_headers(None)
    assert parsed.headers == {}
    assert parsed.use_fresh_context is False
    assert parsed.enable_download is False
