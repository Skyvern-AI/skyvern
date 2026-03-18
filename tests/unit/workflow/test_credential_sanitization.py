from skyvern.exceptions import (
    CredentialParameterNotFoundError,
    InvalidCredentialId,
    sanitize_credential_for_error,
)


class TestSanitizeCredentialForError:
    def test_normal_credential_id_passes_through(self) -> None:
        assert sanitize_credential_for_error("cred_abc123") == "cred_abc123"

    def test_uuid_credential_id_passes_through(self) -> None:
        assert sanitize_credential_for_error("550e8400-e29b-41d4-a716-446655440000") == (
            "550e8400-e29b-41d4-a716-446655440000"
        )

    def test_redacts_string_containing_password(self) -> None:
        value = "{'password': 'secret123', 'username': 'user@example.com'}"
        result = sanitize_credential_for_error(value)
        assert "secret123" not in result
        assert "user@example.com" not in result
        assert "<redacted" in result

    def test_redacts_string_containing_username(self) -> None:
        result = sanitize_credential_for_error("username=admin")
        assert "admin" not in result
        assert "<redacted" in result

    def test_redacts_string_containing_secret(self) -> None:
        result = sanitize_credential_for_error("secret_value=my_api_key")
        assert "my_api_key" not in result
        assert "<redacted" in result

    def test_redacts_string_containing_totp(self) -> None:
        result = sanitize_credential_for_error("totp=JBSWY3DPEHPK3PXP")
        assert "JBSWY3DPEHPK3PXP" not in result
        assert "<redacted" in result

    def test_redacts_excessively_long_string(self) -> None:
        value = "a" * 201
        result = sanitize_credential_for_error(value)
        assert result == "<redacted - value too long>"

    def test_case_insensitive_detection(self) -> None:
        result = sanitize_credential_for_error("{'PASSWORD': 'secret'}")
        assert "<redacted" in result

    def test_empty_string_passes_through(self) -> None:
        assert sanitize_credential_for_error("") == ""

    def test_none_returns_redacted(self) -> None:
        assert sanitize_credential_for_error(None) == "<redacted - non-string type: NoneType>"

    def test_dict_returns_redacted(self) -> None:
        result = sanitize_credential_for_error({"password": "secret", "username": "user"})
        assert "secret" not in result
        assert "user" not in result
        assert "<redacted - non-string type: dict>" == result


class TestInvalidCredentialIdSanitization:
    def test_normal_id_in_message(self) -> None:
        exc = InvalidCredentialId("cred_abc123")
        assert "cred_abc123" in str(exc)
        assert "Invalid credential ID" in str(exc)

    def test_credential_dict_redacted_in_message(self) -> None:
        credential_dict_str = str({"password": "real_password", "username": "real_user@example.com"})
        exc = InvalidCredentialId(credential_dict_str)
        assert "real_password" not in str(exc)
        assert "real_user@example.com" not in str(exc)
        assert "<redacted" in str(exc)

    def test_message_attribute_also_sanitized(self) -> None:
        credential_dict_str = str({"password": "secret", "username": "user"})
        exc = InvalidCredentialId(credential_dict_str)
        assert "secret" not in exc.message
        assert "<redacted" in exc.message


class TestInvalidCredentialIdTypeGuard:
    """Tests for the isinstance guard in service.py that rejects non-string credential values."""

    def test_dict_value_raises_with_safe_message(self) -> None:
        credential_dict = {"password": "real_password", "username": "real_user@example.com"}
        exc = InvalidCredentialId(f"<non-string value of type {type(credential_dict).__name__}>")
        assert "real_password" not in str(exc)
        assert "real_user@example.com" not in str(exc)
        assert "non-string value of type dict" in str(exc)

    def test_list_value_raises_with_safe_message(self) -> None:
        exc = InvalidCredentialId("<non-string value of type list>")
        assert "non-string value of type list" in str(exc)


class TestCredentialParameterNotFoundErrorSanitization:
    def test_normal_id_in_message(self) -> None:
        exc = CredentialParameterNotFoundError("cred_abc123")
        assert "cred_abc123" in str(exc)

    def test_credential_dict_redacted_in_message(self) -> None:
        credential_dict_str = str({"password": "real_password", "username": "real_user"})
        exc = CredentialParameterNotFoundError(credential_dict_str)
        assert "real_password" not in str(exc)
        assert "real_user" not in str(exc)
        assert "<redacted" in str(exc)
