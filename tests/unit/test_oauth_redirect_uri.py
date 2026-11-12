import pytest
from structlog.testing import capture_logs

from skyvern.forge.sdk.services import oauth_redirect_uri


@pytest.mark.parametrize(
    ("configured_uris", "warns"),
    [([], True), (["https://app.skyvern.com/integrations/google/callback"], False)],
)
def test_removed_redirect_host_setting_warns_only_without_its_replacement(
    monkeypatch: pytest.MonkeyPatch, configured_uris: list[str], warns: bool
) -> None:
    monkeypatch.setenv("GOOGLE_OAUTH_REDIRECT_HOSTS", '["app.skyvern.com"]')
    monkeypatch.delenv("MICROSOFT_OAUTH_REDIRECT_HOSTS", raising=False)
    monkeypatch.setattr(oauth_redirect_uri.settings, "GOOGLE_OAUTH_REDIRECT_URIS", configured_uris)

    with capture_logs() as logs:
        oauth_redirect_uri.warn_removed_redirect_host_settings()

    warned = [log for log in logs if log.get("setting") == "GOOGLE_OAUTH_REDIRECT_HOSTS"]
    assert bool(warned) is warns
    if warns:
        assert warned[0]["replacement"] == "GOOGLE_OAUTH_REDIRECT_URIS"
