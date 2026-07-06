import pytest

from skyvern.webeye.browser_factory import parse_extra_headers


@pytest.mark.parametrize("value", ["true", "1", "yes", "TRUE", "YES"])
def test_parse_extra_headers_enable_download_accepts_true_values(value: str) -> None:
    parsed_headers = parse_extra_headers({"enable_download": value})

    assert parsed_headers.enable_download is True


@pytest.mark.parametrize("value", ["false", "0", "no", ""])
def test_parse_extra_headers_enable_download_rejects_false_values(value: str) -> None:
    parsed_headers = parse_extra_headers({"enable_download": value})

    assert parsed_headers.enable_download is False
