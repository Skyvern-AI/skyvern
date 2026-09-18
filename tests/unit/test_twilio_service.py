import base64
import json
from collections.abc import Awaitable, Callable
from typing import Any
from unittest.mock import AsyncMock
from urllib.parse import parse_qs

import httpx
import pytest
from pydantic import BaseModel, ValidationError

from skyvern.forge.sdk.db import utils as db_utils
from skyvern.forge.sdk.db.enums import OrganizationAuthTokenType
from skyvern.forge.sdk.db.models import OrganizationAuthTokenModel
from skyvern.forge.sdk.schemas.organizations import (
    CreateTwilioCredentialRequest,
    TwilioCredential,
    TwilioCredentialResponse,
    TwilioCredentialSafe,
    TwilioOrganizationAuthToken,
)
from skyvern.forge.sdk.schemas.sms import EnablePhoneNumberRequest, RegisterManualPhoneNumberRequest
from skyvern.forge.sdk.services import twilio_service

HttpHandler = Callable[[httpx.Request], httpx.Response | Awaitable[httpx.Response]]


def _install_transport(monkeypatch: pytest.MonkeyPatch, handler: HttpHandler) -> None:
    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient

    def fake_async_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        assert kwargs["timeout"] == 15.0
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(twilio_service.httpx, "AsyncClient", fake_async_client)


VALID_ACCOUNT_SID = "AC" + "0123456789abcdef" * 2
VALID_API_KEY_SID = "SK" + "0123456789abcdef" * 2
VALID_PHONE_NUMBER_SID = "PN" + "a" * 32


def _assert_no_secret_inputs(error: ValueError) -> None:
    surfaces = [str(error), repr(error)]
    if isinstance(error, ValidationError):
        surfaces.extend([str(error.errors()), error.json()])
    for surface in surfaces:
        assert "AUTH_SENTINEL" not in surface
        assert "KEY_SENTINEL" not in surface


@pytest.mark.parametrize("model", [TwilioCredential, CreateTwilioCredentialRequest])
@pytest.mark.parametrize("encoding", [str, bytes, bytearray])
def test_malformed_twilio_json_hides_secrets(model: type[BaseModel], encoding: type) -> None:
    payload: dict[str, Any] = {
        "account_sid": VALID_ACCOUNT_SID,
        "auth_token": "AUTH_SENTINEL",
        "api_key_secret": "KEY_SENTINEL",
    }
    if model is CreateTwilioCredentialRequest:
        payload = {"credential": payload}
    malformed = json.dumps(payload)[:-1]
    data = malformed if encoding is str else encoding(malformed, "utf-8")
    with pytest.raises(ValueError) as caught:
        model.model_validate_json(data)
    _assert_no_secret_inputs(caught.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("encrypted", [False, True])
async def test_malformed_twilio_json_reader_hides_secrets(monkeypatch: pytest.MonkeyPatch, encrypted: bool) -> None:
    malformed = '{"auth_token":"AUTH_SENTINEL","api_key_secret":"KEY_SENTINEL"'
    monkeypatch.setattr(db_utils, "encryptor", AsyncMock(decrypt=AsyncMock(return_value=malformed)))
    row = OrganizationAuthTokenModel(
        token="" if encrypted else malformed,
        encrypted_token="ciphertext" if encrypted else None,
        encrypted_method="aes" if encrypted else None,
    )
    with pytest.raises(ValueError) as caught:
        await db_utils.convert_to_organization_auth_token(row, OrganizationAuthTokenType.twilio_credential)
    _assert_no_secret_inputs(caught.value)


def test_twilio_credential_hides_secrets_in_representations_and_validation_errors() -> None:
    secrets = {"auth_token": "AUTH_SENTINEL", "api_key_secret": "KEY_SENTINEL"}
    credential = TwilioCredential(account_sid=VALID_ACCOUNT_SID, api_key_sid=VALID_API_KEY_SID, **secrets)
    with pytest.raises(ValidationError) as error:
        TwilioCredential(account_sid=VALID_ACCOUNT_SID, **secrets)

    for rendered in (repr(credential), str(credential), str(error.value)):
        assert all(secret not in rendered for secret in secrets.values())


def test_twilio_credential_accepts_valid_authentication_modes() -> None:
    assert TwilioCredential(account_sid=VALID_ACCOUNT_SID, auth_token="auth-token").auth_token == "auth-token"
    credential = TwilioCredential(
        account_sid=VALID_ACCOUNT_SID,
        api_key_sid=VALID_API_KEY_SID,
        api_key_secret="api-secret",
    )
    assert credential.api_key_sid == VALID_API_KEY_SID


@pytest.mark.parametrize(
    "model",
    [
        TwilioCredential,
        CreateTwilioCredentialRequest,
        TwilioOrganizationAuthToken,
        TwilioCredentialSafe,
        TwilioCredentialResponse,
    ],
)
@pytest.mark.parametrize("malformed_field", [False, True], ids=["missing-field", "invalid-type"])
def test_twilio_models_remove_inputs_from_all_validation_error_surfaces(
    model: type[BaseModel], malformed_field: bool
) -> None:
    payload: dict[str, Any] = {
        "account_sid": VALID_ACCOUNT_SID,
        "auth_token": "AUTH_SENTINEL",
        "api_key_secret": "KEY_SENTINEL",
    }
    if malformed_field:
        payload["auth_token"] = ["AUTH_SENTINEL"]
        payload["api_key_secret"] = ["KEY_SENTINEL"]
        payload["account_sid"] = ["AUTH_SENTINEL", "KEY_SENTINEL"]
    if model not in (TwilioCredential, TwilioCredentialSafe):
        payload = {"credential": payload}
    with pytest.raises(ValidationError) as caught:
        model.model_validate(payload)
    error = caught.value
    for rendered in (str(error), repr(error), str(error.errors()), error.json()):
        assert "AUTH_SENTINEL" not in rendered
        assert "KEY_SENTINEL" not in rendered


@pytest.mark.parametrize(
    "credential",
    [
        {"account_sid": "AC_short", "auth_token": "auth-token"},
        {"account_sid": VALID_API_KEY_SID, "auth_token": "auth-token"},
        {
            "account_sid": VALID_ACCOUNT_SID,
            "api_key_sid": "SK_short",
            "api_key_secret": "api-secret",
        },
        {"account_sid": VALID_ACCOUNT_SID, "api_key_sid": VALID_API_KEY_SID, "auth_token": "auth-token"},
        {"account_sid": VALID_ACCOUNT_SID, "api_key_secret": "api-secret", "auth_token": "auth-token"},
        {"account_sid": VALID_ACCOUNT_SID, "auth_token": "   "},
    ],
)
def test_twilio_credential_rejects_malformed_values(credential: dict[str, str]) -> None:
    with pytest.raises(ValidationError):
        TwilioCredential(**credential)


def test_manual_phone_number_request_normalizes_and_validates_e164() -> None:
    assert RegisterManualPhoneNumberRequest(phone_number="+1 (415) 555-2671").phone_number == "+14155552671"
    with pytest.raises(ValidationError):
        RegisterManualPhoneNumberRequest(phone_number="4155552671")
    with pytest.raises(ValidationError):
        RegisterManualPhoneNumberRequest(phone_number="+1--415-555-2671")
    with pytest.raises(ValidationError):
        RegisterManualPhoneNumberRequest(phone_number="+1-EXT-415-555-2671")


def test_enable_phone_number_request_validates_twilio_sid() -> None:
    assert EnablePhoneNumberRequest(phone_number_sid=VALID_PHONE_NUMBER_SID).phone_number_sid == VALID_PHONE_NUMBER_SID
    for invalid_sid in ("PN../../etc/passwd", "PN%2e%2e%2fetc%2fpasswd"):
        with pytest.raises(ValidationError):
            EnablePhoneNumberRequest(phone_number_sid=invalid_sid)


def test_compute_twilio_signature_matches_published_vector() -> None:
    # Source: twilio-python tests/unit/test_request_validator.py.
    url = "https://mycompany.com/myapp.php?foo=1&bar=2"
    params = {
        "CallSid": "CA1234567890ABCDE",
        "Digits": "1234",
        "From": "+14158675309",
        "To": "+18005551212",
        "Caller": "+14158675309",
    }

    signature = twilio_service.compute_twilio_signature("12345", url, params)

    assert signature == "RSOYDt4T1cUTdK1PDd93/VVr8B8="


def test_validate_twilio_signature_is_order_independent_and_rejects_tampering() -> None:
    url = "https://example.com/v1/sms/inbound/smsc_123?token=secret"
    params = {
        "From": "+14158675309",
        "To": "+18005551212",
        "Body": "123456",
        "MessageSid": "SM123",
    }
    signature = twilio_service.compute_twilio_signature("auth-token", url, params)
    reversed_params = dict(reversed(list(params.items())))

    assert twilio_service.validate_twilio_signature("auth-token", url, reversed_params, signature)
    assert not twilio_service.validate_twilio_signature(
        "auth-token",
        url,
        {**params, "Body": "654321"},
        signature,
    )
    assert not twilio_service.validate_twilio_signature("auth-token", url, params, "☃")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("api_key_sid", "api_key_secret", "auth_token", "expected_username", "expected_password"),
    [
        ("SK_api_key", "api-secret", "auth-token", "SK_api_key", "api-secret"),
        (None, None, "auth-token", "AC_account", "auth-token"),
        ("SK_api_key", None, "auth-token", "AC_account", "auth-token"),
        (None, "orphan-secret", "auth-token", "AC_account", "auth-token"),
    ],
)
async def test_client_uses_expected_basic_auth(
    monkeypatch: pytest.MonkeyPatch,
    api_key_sid: str | None,
    api_key_secret: str | None,
    auth_token: str,
    expected_username: str,
    expected_password: str,
) -> None:
    authorization_headers: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        authorization_headers.append(request.headers["Authorization"])
        return httpx.Response(200, json={"sid": "AC_account"})

    _install_transport(monkeypatch, handler)
    client = twilio_service.TwilioClient(
        "AC_account",
        api_key_sid=api_key_sid,
        api_key_secret=api_key_secret,
        auth_token=auth_token,
    )

    account = await client.get_account()

    expected_credentials = base64.b64encode(f"{expected_username}:{expected_password}".encode()).decode()
    assert authorization_headers == [f"Basic {expected_credentials}"]
    assert account == {"sid": "AC_account"}


@pytest.mark.asyncio
async def test_list_incoming_phone_numbers_follows_next_page_uri(monkeypatch: pytest.MonkeyPatch) -> None:
    requested_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_urls.append(str(request.url))
        if request.url.params.get("PageToken") == "next":
            return httpx.Response(
                200,
                json={
                    "incoming_phone_numbers": [{"sid": "PN_second"}],
                    "next_page_uri": None,
                },
            )
        assert request.url.params["PageSize"] == "100"
        return httpx.Response(
            200,
            json={
                "incoming_phone_numbers": [{"sid": "PN_first"}],
                "next_page_uri": ("/2010-04-01/Accounts/AC_account/IncomingPhoneNumbers.json?PageToken=next"),
            },
        )

    _install_transport(monkeypatch, handler)
    client = twilio_service.TwilioClient("AC_account", auth_token="auth-token")

    phone_numbers = await client.list_incoming_phone_numbers()

    assert phone_numbers == [{"sid": "PN_first"}, {"sid": "PN_second"}]
    assert len(requested_urls) == 2
    assert requested_urls[1].startswith(
        "https://api.twilio.com/2010-04-01/Accounts/AC_account/IncomingPhoneNumbers.json"
    )


@pytest.mark.asyncio
async def test_list_incoming_phone_numbers_rejects_cross_host_next_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "incoming_phone_numbers": [{"sid": "PN_first"}],
                "next_page_uri": "https://attacker.example/next",
            },
        )

    _install_transport(monkeypatch, handler)
    client = twilio_service.TwilioClient("AC_account", auth_token="auth-token")

    with pytest.raises(twilio_service.TwilioApiError) as exc_info:
        await client.list_incoming_phone_numbers()

    assert exc_info.value.status_code == 0
    assert exc_info.value.message == "Unexpected Twilio pagination URL"


@pytest.mark.asyncio
@pytest.mark.parametrize("sms_url", ["https://example.com/v1/sms/inbound/smsc_123?token=secret", ""])
async def test_update_inbound_sms_config_posts_routing_fields(
    monkeypatch: pytest.MonkeyPatch,
    sms_url: str,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"sid": VALID_PHONE_NUMBER_SID, "sms_url": sms_url})

    _install_transport(monkeypatch, handler)
    client = twilio_service.TwilioClient("AC_account", auth_token="auth-token")

    result = await client.update_inbound_sms_config(VALID_PHONE_NUMBER_SID, sms_url, "POST", "")

    assert result == {"sid": VALID_PHONE_NUMBER_SID, "sms_url": sms_url}
    assert len(requests) == 1
    assert requests[0].method == "POST"
    assert requests[0].url.path.endswith(f"/IncomingPhoneNumbers/{VALID_PHONE_NUMBER_SID}.json")
    assert parse_qs(requests[0].content.decode(), keep_blank_values=True) == {
        "SmsUrl": [sms_url],
        "SmsMethod": ["POST"],
        "SmsApplicationSid": [""],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_sid", ["PN../../etc/passwd", "PN%2e%2e%2fetc%2fpasswd"])
async def test_get_incoming_phone_number_rejects_invalid_sid(monkeypatch: pytest.MonkeyPatch, invalid_sid: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        pytest.fail("invalid SID reached Twilio")

    _install_transport(monkeypatch, handler)
    client = twilio_service.TwilioClient("AC_account", auth_token="auth-token")
    with pytest.raises(ValueError, match="valid Twilio incoming phone number SID"):
        await client.get_incoming_phone_number(invalid_sid)


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_sid", ["PN../../etc/passwd", "PN%2e%2e%2fetc%2fpasswd"])
async def test_update_inbound_sms_config_rejects_invalid_sid(monkeypatch: pytest.MonkeyPatch, invalid_sid: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        pytest.fail("invalid SID reached Twilio")

    _install_transport(monkeypatch, handler)
    client = twilio_service.TwilioClient("AC_account", auth_token="auth-token")
    with pytest.raises(ValueError, match="valid Twilio incoming phone number SID"):
        await client.update_inbound_sms_config(invalid_sid, "", "POST", "")


@pytest.mark.asyncio
async def test_non_success_response_raises_twilio_api_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "The requested resource was not found"})

    _install_transport(monkeypatch, handler)
    client = twilio_service.TwilioClient("AC_account", auth_token="auth-token")

    with pytest.raises(twilio_service.TwilioApiError) as exc_info:
        await client.get_incoming_phone_number(VALID_PHONE_NUMBER_SID)

    assert exc_info.value.status_code == 404
    assert exc_info.value.message == "Twilio API returned HTTP 404"


@pytest.mark.asyncio
async def test_non_success_response_does_not_expose_provider_body(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "message": "token=secret-token Body=123456 MessageSid=SM_full_sid",
                "url": "https://example.test/v1/sms/inbound/smsc_123?token=secret-token",
            },
        )

    _install_transport(monkeypatch, handler)
    client = twilio_service.TwilioClient("AC_account", auth_token="auth-token")

    with pytest.raises(twilio_service.TwilioApiError) as exc_info:
        await client.get_account()

    assert exc_info.value.message == "Twilio API returned HTTP 400"
    assert "secret-token" not in str(exc_info.value)
    assert "123456" not in str(exc_info.value)
    assert "SM_full_sid" not in str(exc_info.value)


@pytest.mark.asyncio
async def test_transport_error_is_wrapped(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection failed", request=request)

    _install_transport(monkeypatch, handler)
    client = twilio_service.TwilioClient("AC_account", auth_token="auth-token")

    with pytest.raises(twilio_service.TwilioApiError) as exc_info:
        await client.get_account()

    assert exc_info.value.status_code == 0


def test_build_inbound_sms_url_strips_trailing_slashes() -> None:
    assert twilio_service.build_inbound_sms_url("https://api.example.com///", "smsc_123", "secret") == (
        "https://api.example.com/v1/sms/inbound/smsc_123?token=secret"
    )
