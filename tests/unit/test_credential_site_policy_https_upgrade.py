import pytest

from skyvern.forge.sdk.credential_site_policy import LoginSite, same_origin, same_release_scope


@pytest.mark.parametrize("whole_site", [True, False])
def test_http_autofill_url_admits_https_page(whole_site: bool) -> None:
    """A vault URL saved as http:// still fills on the https:// page (implicit upgrade, #9605)."""
    site = LoginSite(url="http://app.raindrop.io", whole_site=whole_site)
    assert site.admits("https://app.raindrop.io/login")


def test_same_release_scope_and_origin_allow_http_to_https_upgrade() -> None:
    assert same_release_scope("https://app.raindrop.io", "http://app.raindrop.io")
    assert same_origin("https://app.raindrop.io/", "http://app.raindrop.io")


@pytest.mark.parametrize("whole_site", [True, False])
def test_https_autofill_url_still_refuses_http_page(whole_site: bool) -> None:
    """Downgrading https to http stays blocked."""
    site = LoginSite(url="https://app.raindrop.io", whole_site=whole_site)
    assert not site.admits("http://app.raindrop.io")


@pytest.mark.parametrize("whole_site", [True, False])
def test_upgrade_does_not_widen_host_or_port(whole_site: bool) -> None:
    site = LoginSite(url="http://app.raindrop.io", whole_site=whole_site)
    assert not site.admits("https://evil.example/")
    assert not site.admits("https://app.raindrop.io:8443/")
