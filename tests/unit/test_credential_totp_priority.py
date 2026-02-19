"""Tests for credential TOTP priority over webhook (totp_url) and totp_identifier.

Verifies that try_generate_totp_from_credential() correctly generates TOTP codes
from credential secrets stored in workflow run context, and that callers check
credential TOTP before falling back to poll_otp_value.
"""

from unittest.mock import MagicMock, patch

import pyotp

from skyvern.forge.sdk.schemas.totp_codes import OTPType
from skyvern.services.otp_service import OTPValue, try_generate_totp_from_credential

# A valid base32 TOTP secret for testing
TEST_TOTP_SECRET = "JBSWY3DPEHPK3PXP"


def _make_workflow_run_context(
    values: dict | None = None,
    secrets: dict | None = None,
) -> MagicMock:
    """Create a mock WorkflowRunContext with the given values and secrets."""
    ctx = MagicMock()
    ctx.values = values or {}
    ctx.secrets = secrets or {}

    def totp_secret_value_key(totp_secret_id: str) -> str:
        return f"{totp_secret_id}_value"

    ctx.totp_secret_value_key = totp_secret_value_key

    def get_original_secret_value_or_none(secret_key: str) -> str | None:
        return ctx.secrets.get(secret_key)

    ctx.get_original_secret_value_or_none = get_original_secret_value_or_none
    return ctx


class TestTryGenerateTotpFromCredential:
    """Tests for the try_generate_totp_from_credential helper."""

    def test_returns_none_when_workflow_run_id_is_none(self) -> None:
        result = try_generate_totp_from_credential(None)
        assert result is None

    def test_returns_none_when_no_workflow_run_context(self) -> None:
        with patch("skyvern.services.otp_service.app") as mock_app:
            mock_app.WORKFLOW_CONTEXT_MANAGER.get_workflow_run_context.return_value = None
            result = try_generate_totp_from_credential("wfr_123")
            assert result is None

    def test_returns_none_when_no_credential_values(self) -> None:
        ctx = _make_workflow_run_context(values={"some_param": "plain_string"})
        with patch("skyvern.services.otp_service.app") as mock_app:
            mock_app.WORKFLOW_CONTEXT_MANAGER.get_workflow_run_context.return_value = ctx
            result = try_generate_totp_from_credential("wfr_123")
            assert result is None

    def test_returns_none_when_dict_value_has_no_totp_key(self) -> None:
        ctx = _make_workflow_run_context(
            values={"cred_param": {"username": "user", "password": "pass"}},
        )
        with patch("skyvern.services.otp_service.app") as mock_app:
            mock_app.WORKFLOW_CONTEXT_MANAGER.get_workflow_run_context.return_value = ctx
            result = try_generate_totp_from_credential("wfr_123")
            assert result is None

    def test_returns_none_when_totp_secret_id_is_empty(self) -> None:
        ctx = _make_workflow_run_context(
            values={"cred_param": {"totp": ""}},
        )
        with patch("skyvern.services.otp_service.app") as mock_app:
            mock_app.WORKFLOW_CONTEXT_MANAGER.get_workflow_run_context.return_value = ctx
            result = try_generate_totp_from_credential("wfr_123")
            assert result is None

    def test_returns_none_when_totp_secret_not_in_secrets(self) -> None:
        """When the secret ID doesn't resolve to an actual secret value."""
        ctx = _make_workflow_run_context(
            values={"cred_param": {"totp": "secret_id_123"}},
            secrets={},  # no secret stored
        )
        with patch("skyvern.services.otp_service.app") as mock_app:
            mock_app.WORKFLOW_CONTEXT_MANAGER.get_workflow_run_context.return_value = ctx
            result = try_generate_totp_from_credential("wfr_123")
            assert result is None

    def test_generates_totp_from_credential_secret(self) -> None:
        """Happy path: credential with valid TOTP secret generates a code."""
        ctx = _make_workflow_run_context(
            values={"cred_param": {"username": "user", "password": "pass", "totp": "totp_ref_1"}},
            secrets={"totp_ref_1_value": TEST_TOTP_SECRET},
        )
        with patch("skyvern.services.otp_service.app") as mock_app:
            mock_app.WORKFLOW_CONTEXT_MANAGER.get_workflow_run_context.return_value = ctx
            result = try_generate_totp_from_credential("wfr_123")

            assert result is not None
            assert isinstance(result, OTPValue)
            assert result.type == OTPType.TOTP
            # Verify the code matches what pyotp would generate
            expected_code = pyotp.TOTP(TEST_TOTP_SECRET).now()
            assert result.value == expected_code

    def test_returns_first_matching_credential(self) -> None:
        """When multiple credentials have TOTP, returns the first one found."""
        ctx = _make_workflow_run_context(
            values={
                "cred_a": {"totp": "ref_a"},
                "cred_b": {"totp": "ref_b"},
            },
            secrets={
                "ref_a_value": TEST_TOTP_SECRET,
                "ref_b_value": "ORSXG5DJNZTQ====",
            },
        )
        with patch("skyvern.services.otp_service.app") as mock_app:
            mock_app.WORKFLOW_CONTEXT_MANAGER.get_workflow_run_context.return_value = ctx
            result = try_generate_totp_from_credential("wfr_123")

            assert result is not None
            assert result.value == pyotp.TOTP(TEST_TOTP_SECRET).now()

    def test_skips_invalid_secret_and_continues(self) -> None:
        """If one credential has an invalid TOTP secret, skip it and try the next."""
        ctx = _make_workflow_run_context(
            values={
                "cred_bad": {"totp": "ref_bad"},
                "cred_good": {"totp": "ref_good"},
            },
            secrets={
                "ref_bad_value": "NOT_A_VALID_BASE32!!!",
                "ref_good_value": TEST_TOTP_SECRET,
            },
        )
        with patch("skyvern.services.otp_service.app") as mock_app:
            mock_app.WORKFLOW_CONTEXT_MANAGER.get_workflow_run_context.return_value = ctx
            result = try_generate_totp_from_credential("wfr_123")

            assert result is not None
            assert result.value == pyotp.TOTP(TEST_TOTP_SECRET).now()

    def test_skips_non_string_totp_id(self) -> None:
        """If the totp value is not a string (e.g., int or None), skip it."""
        ctx = _make_workflow_run_context(
            values={"cred_param": {"totp": 12345}},
        )
        with patch("skyvern.services.otp_service.app") as mock_app:
            mock_app.WORKFLOW_CONTEXT_MANAGER.get_workflow_run_context.return_value = ctx
            result = try_generate_totp_from_credential("wfr_123")
            assert result is None

    def test_skips_non_dict_values(self) -> None:
        """Non-dict values in the context should be ignored."""
        ctx = _make_workflow_run_context(
            values={
                "string_param": "hello",
                "int_param": 42,
                "list_param": [1, 2, 3],
                "none_param": None,
            },
        )
        with patch("skyvern.services.otp_service.app") as mock_app:
            mock_app.WORKFLOW_CONTEXT_MANAGER.get_workflow_run_context.return_value = ctx
            result = try_generate_totp_from_credential("wfr_123")
            assert result is None
