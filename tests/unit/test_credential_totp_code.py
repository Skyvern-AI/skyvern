from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from urllib.parse import quote

import pyotp
import pytest
from fastapi import HTTPException, Response

from skyvern.forge.sdk.routes import credentials
from skyvern.forge.sdk.schemas.credentials import (
    CredentialItem,
    CredentialType,
    NonEmptyPasswordCredential,
    PasswordCredential,
    TotpType,
)
from skyvern.forge.sdk.services.credentials import AuthenticatorTotpErrorCode, AuthenticatorTotpParseResult


@pytest.fixture(autouse=True)
def clear_totp_code_preview_cache() -> None:
    credentials._TOTP_CODE_PREVIEW_CACHE.clear()


def _mock_totp_preview_dependencies(
    monkeypatch: pytest.MonkeyPatch,
    *,
    secret: str | None,
    credential_id: str = "cred_test",
    organization_id: str = "org_test",
) -> tuple[SimpleNamespace, SimpleNamespace, SimpleNamespace]:
    db_credential = SimpleNamespace(
        credential_id=credential_id,
        organization_id=organization_id,
        name="Example",
        vault_type=None,
        item_id="item_test",
        credential_type=CredentialType.PASSWORD,
        totp_type=TotpType.AUTHENTICATOR,
    )
    vault_service = SimpleNamespace(
        get_credential_item=AsyncMock(
            return_value=CredentialItem(
                item_id="item_test",
                name="Example",
                credential_type=CredentialType.PASSWORD,
                credential=PasswordCredential(
                    username="user@example.com",
                    password="pw",
                    totp=secret,
                    totp_type=TotpType.AUTHENTICATOR,
                ),
            )
        )
    )
    mock_credentials = SimpleNamespace(get_credential=AsyncMock(return_value=db_credential))
    monkeypatch.setattr(credentials.app, "DATABASE", SimpleNamespace(credentials=mock_credentials))
    monkeypatch.setattr(
        credentials.app,
        "AGENT_FUNCTION",
        SimpleNamespace(
            parse_enterprise_totp_secret=AsyncMock(return_value=None),
            parse_enterprise_totp_secret_result=AsyncMock(return_value=AuthenticatorTotpParseResult()),
        ),
    )
    monkeypatch.setattr(credentials, "_get_credential_vault_service", AsyncMock(return_value=vault_service))

    return db_credential, vault_service, mock_credentials


def test_clear_cached_totp_code_preview_removes_entry() -> None:
    credentials._cache_totp_code_preview(
        organization_id="org_test",
        credential_id="cred_test",
        code="123456",
        now=0,
        expires_at=30,
    )

    credentials._clear_cached_totp_code_preview(organization_id="org_test", credential_id="cred_test")

    assert (
        credentials._get_cached_totp_code_preview(
            organization_id="org_test",
            credential_id="cred_test",
            now=1,
        )
        is None
    )


def test_totp_code_preview_cache_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(credentials, "_TOTP_CODE_PREVIEW_CACHE_MAX_ENTRIES", 2)

    for index in range(3):
        credentials._cache_totp_code_preview(
            organization_id="org_test",
            credential_id=f"cred_{index}",
            code=f"12345{index}",
            now=0,
            expires_at=30,
        )

    assert len(credentials._TOTP_CODE_PREVIEW_CACHE) == 2
    assert ("org_test", "cred_0") not in credentials._TOTP_CODE_PREVIEW_CACHE


def test_authenticator_totp_validation_preserves_uri_configuration() -> None:
    totp_uri = (
        "otpauth://totp/Example:user@example.com"
        "?secret=JBSWY3DPEHPK3PXP&issuer=Example&algorithm=SHA256&digits=8&period=60"
    )
    credential = NonEmptyPasswordCredential(
        username="user@example.com",
        password="pw",
        totp=totp_uri,
        totp_type=TotpType.AUTHENTICATOR,
    )

    credentials._normalize_authenticator_totp_or_raise(credential)

    assert credential.totp == totp_uri


def test_authenticator_totp_validation_preserves_decoded_uri_configuration() -> None:
    totp_uri = (
        "otpauth://totp/Example:user@example.com"
        "?secret=JBSWY3DPEHPK3PXP&issuer=Example&algorithm=SHA256&digits=8&period=60"
    )
    credential = NonEmptyPasswordCredential(
        username="user@example.com",
        password="pw",
        totp=quote(totp_uri, safe=""),
        totp_type=TotpType.AUTHENTICATOR,
    )

    credentials._normalize_authenticator_totp_or_raise(credential)

    assert credential.totp == totp_uri


def test_authenticator_totp_validation_normalizes_raw_secret() -> None:
    credential = NonEmptyPasswordCredential(
        username="user@example.com",
        password="pw",
        totp="JBSW Y3DP-EHPK 3PXP",
        totp_type=TotpType.AUTHENTICATOR,
    )

    credentials._normalize_authenticator_totp_or_raise(credential)

    assert credential.totp == "JBSWY3DPEHPK3PXP"


def test_authenticator_totp_validation_rejects_invalid_secret() -> None:
    credential = NonEmptyPasswordCredential(
        username="user@example.com",
        password="pw",
        totp="not a valid secret!",
        totp_type=TotpType.AUTHENTICATOR,
    )

    with pytest.raises(HTTPException) as exc_info:
        credentials._normalize_authenticator_totp_or_raise(credential)

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == {
        "error_code": AuthenticatorTotpErrorCode.INVALID_AUTHENTICATOR_KEY.value,
        "message": credentials._AUTHENTICATOR_SECRET_INVALID_DETAIL,
    }


@pytest.mark.asyncio
async def test_authenticator_totp_validation_returns_enterprise_required_detail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parse_result = AsyncMock(
        return_value=AuthenticatorTotpParseResult(
            error_code=AuthenticatorTotpErrorCode.AUTHENTICATOR_FEATURE_RESTRICTED,
            message="Enterprise plan required for this authenticator QR.",
            vendor="okta",
        )
    )
    monkeypatch.setattr(
        credentials.app,
        "AGENT_FUNCTION",
        SimpleNamespace(parse_enterprise_totp_secret_result=parse_result),
    )
    credential = NonEmptyPasswordCredential(
        username="user@example.com",
        password="pw",
        totp="phonefactor://activate_account?sharedSecret=JBSWY3DPEHPK3PXP",
        totp_type=TotpType.AUTHENTICATOR,
    )

    with pytest.raises(HTTPException) as exc_info:
        await credentials._normalize_authenticator_totp_for_organization_or_raise(
            credential,
            organization_id="org_test",
        )

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == {
        "error_code": AuthenticatorTotpErrorCode.AUTHENTICATOR_FEATURE_RESTRICTED.value,
        "message": "Enterprise plan required for this authenticator QR.",
        "vendor": "okta",
    }
    parse_result.assert_awaited_once()


@pytest.mark.asyncio
async def test_authenticator_totp_validation_returns_enterprise_no_code_detail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        credentials.app,
        "AGENT_FUNCTION",
        SimpleNamespace(
            parse_enterprise_totp_secret_result=AsyncMock(
                return_value=AuthenticatorTotpParseResult(
                    error_code=AuthenticatorTotpErrorCode.AUTHENTICATOR_NO_CODE_SECRET,
                    message="This authenticator QR enrolls push approval and has no setup key.",
                    vendor="microsoft",
                )
            )
        ),
    )
    credential = NonEmptyPasswordCredential(
        username="user@example.com",
        password="pw",
        totp="phonefactor://activate_account?code=123456",
        totp_type=TotpType.AUTHENTICATOR,
    )

    with pytest.raises(HTTPException) as exc_info:
        await credentials._normalize_authenticator_totp_for_organization_or_raise(
            credential,
            organization_id="org_test",
        )

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == {
        "error_code": AuthenticatorTotpErrorCode.AUTHENTICATOR_NO_CODE_SECRET.value,
        "message": "This authenticator QR enrolls push approval and has no setup key.",
        "vendor": "microsoft",
    }


@pytest.mark.asyncio
async def test_authenticator_totp_validation_does_not_require_enterprise_for_generic_totp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parse_result = AsyncMock(return_value=AuthenticatorTotpParseResult())
    monkeypatch.setattr(
        credentials.app,
        "AGENT_FUNCTION",
        SimpleNamespace(parse_enterprise_totp_secret_result=parse_result),
    )
    credential = NonEmptyPasswordCredential(
        username="user@example.com",
        password="pw",
        totp="JBSW Y3DP-EHPK 3PXP",
        totp_type=TotpType.AUTHENTICATOR,
    )

    await credentials._normalize_authenticator_totp_for_organization_or_raise(
        credential,
        organization_id="org_test",
    )

    assert credential.totp == "JBSWY3DPEHPK3PXP"
    parse_result.assert_awaited_once()


@pytest.mark.asyncio
async def test_authenticator_totp_validation_saves_enterprise_secret_canonically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parse_result = AsyncMock(return_value=AuthenticatorTotpParseResult(secret="JBSW Y3DP-EHPK 3PXP"))
    monkeypatch.setattr(
        credentials.app,
        "AGENT_FUNCTION",
        SimpleNamespace(parse_enterprise_totp_secret_result=parse_result),
    )
    credential = NonEmptyPasswordCredential(
        username="user@example.com",
        password="pw",
        totp='{"methods":[{"type":"totp","sharedSecret":"JBSWY3DPEHPK3PXP"}]}',
        totp_type=TotpType.AUTHENTICATOR,
    )

    await credentials._normalize_authenticator_totp_for_organization_or_raise(
        credential,
        organization_id="org_test",
    )

    assert credential.totp == "JBSWY3DPEHPK3PXP"
    parse_result.assert_awaited_once()


def test_authenticator_totp_validation_ignores_non_authenticator_methods() -> None:
    credential = NonEmptyPasswordCredential(
        username="user@example.com",
        password="pw",
        totp=None,
        totp_type=TotpType.EMAIL,
    )

    credentials._normalize_authenticator_totp_or_raise(credential)

    assert credential.totp is None


@pytest.mark.asyncio
async def test_get_credential_totp_code_returns_current_generated_code(monkeypatch: pytest.MonkeyPatch) -> None:
    secret = "JBSWY3DPEHPK3PXP"
    db_credential, vault_service, mock_credentials = _mock_totp_preview_dependencies(monkeypatch, secret=secret)
    monkeypatch.setattr(credentials.time, "time", lambda: 0)

    response = await credentials.get_credential_totp_code(
        response=Response(),
        credential_id="cred_test",
        current_org=SimpleNamespace(organization_id="org_test"),
    )

    assert response.code == pyotp.TOTP(secret).at(0)
    assert response.seconds_remaining == 30
    mock_credentials.get_credential.assert_awaited_once_with(
        credential_id="cred_test",
        organization_id="org_test",
    )
    vault_service.get_credential_item.assert_awaited_once_with(db_credential)


@pytest.mark.asyncio
async def test_get_credential_totp_code_uses_otpauth_uri_parameters(monkeypatch: pytest.MonkeyPatch) -> None:
    secret = "JBSWY3DPEHPK3PXP"
    totp_uri = (
        f"otpauth://totp/Example:user@example.com?secret={secret}&issuer=Example&algorithm=SHA256&digits=8&period=60"
    )
    expected_totp = pyotp.parse_uri(totp_uri)
    _mock_totp_preview_dependencies(monkeypatch, secret=totp_uri)
    monkeypatch.setattr(credentials.time, "time", lambda: 0)

    response = await credentials.get_credential_totp_code(
        response=Response(),
        credential_id="cred_test",
        current_org=SimpleNamespace(organization_id="org_test"),
    )

    assert response.code == expected_totp.at(0)
    assert response.seconds_remaining == 60


@pytest.mark.asyncio
async def test_get_credential_totp_code_uses_cache_within_current_window(monkeypatch: pytest.MonkeyPatch) -> None:
    secret = "JBSWY3DPEHPK3PXP"
    db_credential, vault_service, mock_credentials = _mock_totp_preview_dependencies(monkeypatch, secret=secret)
    current_time = 0
    monkeypatch.setattr(credentials, "_get_credential_vault_service", AsyncMock(return_value=vault_service))
    monkeypatch.setattr(credentials.time, "time", lambda: current_time)

    first_response = await credentials.get_credential_totp_code(
        response=Response(),
        credential_id="cred_test",
        current_org=SimpleNamespace(organization_id="org_test"),
    )

    current_time = 1
    second_response = await credentials.get_credential_totp_code(
        response=Response(),
        credential_id="cred_test",
        current_org=SimpleNamespace(organization_id="org_test"),
    )

    assert first_response.code == pyotp.TOTP(secret).at(0)
    assert second_response.code == first_response.code
    assert second_response.seconds_remaining == 29
    mock_credentials.get_credential.assert_awaited_with(
        credential_id="cred_test",
        organization_id="org_test",
    )
    assert mock_credentials.get_credential.await_count == 2
    vault_service.get_credential_item.assert_awaited_once_with(db_credential)


@pytest.mark.asyncio
async def test_get_credential_totp_code_logs_invalid_saved_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_totp_preview_dependencies(monkeypatch, secret="not a valid secret!")
    monkeypatch.setattr(credentials.time, "time", lambda: 0)
    warning_mock = Mock()
    monkeypatch.setattr(credentials.LOG, "warning", warning_mock)

    with pytest.raises(HTTPException) as exc_info:
        await credentials.get_credential_totp_code(
            response=Response(),
            credential_id="cred_test",
            current_org=SimpleNamespace(organization_id="org_test"),
        )

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == {
        "error_code": AuthenticatorTotpErrorCode.INVALID_AUTHENTICATOR_KEY.value,
        "message": credentials._SAVED_AUTHENTICATOR_SECRET_INVALID_DETAIL,
    }
    warning_mock.assert_called_once_with(
        "Saved authenticator key is invalid for TOTP code preview",
        credential_id="cred_test",
        organization_id="org_test",
        vault_type=None,
    )


@pytest.mark.asyncio
async def test_get_credential_totp_code_sets_no_store_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    secret = "JBSWY3DPEHPK3PXP"
    _mock_totp_preview_dependencies(monkeypatch, secret=secret)
    monkeypatch.setattr(credentials.time, "time", lambda: 0)
    response = Response()

    await credentials.get_credential_totp_code(
        response=response,
        credential_id="cred_test",
        current_org=SimpleNamespace(organization_id="org_test"),
    )

    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["Pragma"] == "no-cache"
