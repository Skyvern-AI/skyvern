from skyvern.forge.sdk.routes.credentials import (
    SESSION_VALIDATION_COMPLETE_CRITERION,
    SESSION_VALIDATION_TERMINATE_CRITERION,
    _build_login_test_blocks,
)
from skyvern.schemas.workflows import LoginBlockYAML, UrlBlockYAML, ValidationBlockYAML


def test_build_login_test_blocks_logs_in_then_revisits_and_validates() -> None:
    blocks = _build_login_test_blocks(
        url="https://example.com/login",
        navigation_goal="log in with the credential",
        parameter_key="credential",
        totp_identifier="user@example.com",
    )

    assert [type(b) for b in blocks] == [LoginBlockYAML, UrlBlockYAML, ValidationBlockYAML]

    login, revisit, validate = blocks
    assert login.url == "https://example.com/login"
    assert login.parameter_keys == ["credential"]
    assert login.totp_identifier == "user@example.com"
    # Re-save must log in fresh so the captured session persists (and can overwrite the profile).
    assert login.skip_saved_profile is True
    # A fresh re-navigation before validating defeats a stale "already logged in" page.
    assert revisit.url == "https://example.com/login"
    assert validate.complete_criterion == SESSION_VALIDATION_COMPLETE_CRITERION
    assert validate.terminate_criterion == SESSION_VALIDATION_TERMINATE_CRITERION

    labels = [b.label for b in blocks]
    assert labels == ["login", "verify_navigate", "verify_session"]
    assert all(label.isidentifier() for label in labels)
