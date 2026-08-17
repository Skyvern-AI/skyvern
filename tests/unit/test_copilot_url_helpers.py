import pytest

from skyvern.forge.sdk.copilot.credential_resolution import (
    is_resolved_page_url,
    loggable_origin,
    unresolved_page_url_for_log,
    url_parts,
)
from skyvern.forge.sdk.copilot.workflow_credential_utils import URL_CANDIDATE_RE, url_origin

# `urlparse` raises on a bracket it cannot read as an IPv6 literal. Every shape here is one a user
# can type into a chat turn, and the first is what pasting a markdown auto-link produces.
MALFORMED = [
    "https://example.com](https://example.com",
    "https://[example.com",
    "www.example.com]",
    "https://exam[ple.com/login",
]


@pytest.mark.parametrize("url", MALFORMED)
def test_url_helpers_decline_a_malformed_authority_instead_of_raising(url: str) -> None:
    assert url_parts(url) is None
    assert loggable_origin(url) == ""
    assert url_origin(url) is None
    assert is_resolved_page_url(url) is False


@pytest.mark.parametrize("url", [url for url in MALFORMED if "://" in url])
def test_unresolved_page_url_for_log_has_no_identity_to_name(url: str) -> None:
    assert unresolved_page_url_for_log(url) == ""


def test_markdown_autolink_paste_is_the_shape_that_reached_the_parser() -> None:
    """The regex that feeds these helpers takes the whole `example.com](https://example.com` run."""
    candidates = URL_CANDIDATE_RE.findall("log into [https://example.com](https://example.com) for me")
    assert candidates == ["https://example.com](https://example.com"]


def test_bracketed_ipv6_authority_still_parses() -> None:
    """The guard declines what is malformed, not every URL carrying a bracket."""
    assert url_parts("http://[::1]:8000/login") == (
        "http://[::1]:8000/login",
        "http://[::1]:8000/login",
        "http://[::1]:8000",
    )
    assert url_origin("http://[::1]:8000/login") == "http://[::1]:8000"
    assert is_resolved_page_url("http://[::1]:8000/login") is True
