import base64
import hashlib
import hmac
from collections.abc import Mapping
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx

from skyvern.forge.sdk.schemas.organizations import TwilioCredential
from skyvern.utils.phone_validation import is_twilio_phone_number_sid

TWILIO_API_BASE = "https://api.twilio.com"
_TWILIO_API_VERSION = "2010-04-01"
_TWILIO_TIMEOUT_SECONDS = 15.0
_TWILIO_MAX_PAGES = 100


class TwilioApiError(Exception):
    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = status_code
        self.message = message
        super().__init__(message)


class TwilioClient:
    def __init__(
        self,
        account_sid: str,
        *,
        api_key_sid: str | None = None,
        api_key_secret: str | None = None,
        auth_token: str | None = None,
    ) -> None:
        self.account_sid = account_sid
        self._username: str
        self._password: str | None
        if api_key_sid and api_key_secret:
            self._username = api_key_sid
            self._password = api_key_secret
        else:
            self._username = account_sid
            self._password = auth_token

    def _new_http_client(self) -> httpx.AsyncClient:
        if self._password is None:
            raise TwilioApiError(0, "Twilio API credentials are incomplete")
        return httpx.AsyncClient(
            auth=(self._username, self._password),
            timeout=_TWILIO_TIMEOUT_SECONDS,
        )

    async def _request_json(
        self,
        client: httpx.AsyncClient,
        method: str,
        url: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        try:
            response = await client.request(method, url, **kwargs)
        except httpx.HTTPError as exc:
            raise TwilioApiError(0, "Twilio API request failed") from exc

        if not response.is_success:
            raise TwilioApiError(response.status_code, _error_message(response))

        try:
            payload = response.json()
        except ValueError as exc:
            raise TwilioApiError(response.status_code, "Twilio API returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise TwilioApiError(response.status_code, "Twilio API returned an invalid response")
        return payload

    def _account_api_url(self, resource: str = "") -> str:
        account_url = f"{TWILIO_API_BASE}/{_TWILIO_API_VERSION}/Accounts/{self.account_sid}"
        return f"{account_url}/{resource}" if resource else f"{account_url}.json"

    async def get_account(self) -> dict[str, Any]:
        async with self._new_http_client() as client:
            return await self._request_json(client, "GET", self._account_api_url())

    async def list_incoming_phone_numbers(self) -> list[dict[str, Any]]:
        url = self._account_api_url("IncomingPhoneNumbers.json")
        params: dict[str, str] | None = {"PageSize": "100"}
        phone_numbers: list[dict[str, Any]] = []
        page_count = 0

        async with self._new_http_client() as client:
            while url:
                payload = await self._request_json(client, "GET", url, params=params)
                page_count += 1
                page = payload.get("incoming_phone_numbers", [])
                if not isinstance(page, list) or not all(isinstance(number, dict) for number in page):
                    raise TwilioApiError(200, "Twilio API returned an invalid phone number list")
                phone_numbers.extend(page)

                next_page_uri = payload.get("next_page_uri")
                if next_page_uri is not None and not isinstance(next_page_uri, str):
                    raise TwilioApiError(200, "Twilio API returned an invalid next page URI")
                if not next_page_uri:
                    url = ""
                    continue
                if page_count >= _TWILIO_MAX_PAGES:
                    raise TwilioApiError(0, "Twilio pagination exceeded page limit")
                next_page_url = urljoin(f"{TWILIO_API_BASE}/", next_page_uri)
                parsed_next_page = urlsplit(next_page_url)
                if parsed_next_page.scheme != "https" or parsed_next_page.netloc != "api.twilio.com":
                    raise TwilioApiError(0, "Unexpected Twilio pagination URL")
                url = next_page_url
                params = None

        return phone_numbers

    async def get_incoming_phone_number(self, number_sid: str) -> dict[str, Any]:
        if not is_twilio_phone_number_sid(number_sid):
            raise ValueError("phone_number_sid must be a valid Twilio incoming phone number SID")
        url = self._account_api_url(f"IncomingPhoneNumbers/{number_sid}.json")
        async with self._new_http_client() as client:
            return await self._request_json(client, "GET", url)

    async def update_inbound_sms_config(
        self,
        phone_number_sid: str,
        sms_url: str,
        sms_method: str = "POST",
        sms_application_sid: str = "",
    ) -> dict[str, Any]:
        if not is_twilio_phone_number_sid(phone_number_sid):
            raise ValueError("phone_number_sid must be a valid Twilio incoming phone number SID")
        url = self._account_api_url(f"IncomingPhoneNumbers/{phone_number_sid}.json")
        async with self._new_http_client() as client:
            return await self._request_json(
                client,
                "POST",
                url,
                data={
                    "SmsUrl": sms_url,
                    "SmsMethod": sms_method,
                    "SmsApplicationSid": sms_application_sid,
                },
            )


def _error_message(response: httpx.Response) -> str:
    # Provider error bodies can contain webhook URLs, credentials, SMS bodies, or full SIDs.
    # Keep the exception safe to expose to API callers and logs.
    return f"Twilio API returned HTTP {response.status_code}"


def compute_twilio_signature(auth_token: str, url: str, params: Mapping[str, str]) -> str:
    signed_payload = url + "".join(key + params[key] for key in sorted(params))
    # Twilio signs webhook requests with HMAC-SHA1 (https://www.twilio.com/docs/usage/webhooks/webhooks-security).
    digest = hmac.new(auth_token.encode(), signed_payload.encode(), hashlib.sha1).digest()
    return base64.b64encode(digest).decode()


def validate_twilio_signature(
    auth_token: str,
    url: str,
    params: Mapping[str, str],
    signature: str,
) -> bool:
    expected_signature = compute_twilio_signature(auth_token, url, params).encode("ascii")
    return hmac.compare_digest(expected_signature, signature.encode("utf-8", errors="surrogatepass"))


def build_client_from_credential(credential: TwilioCredential) -> TwilioClient:
    return TwilioClient(
        credential.account_sid,
        api_key_sid=credential.api_key_sid,
        api_key_secret=credential.api_key_secret,
        auth_token=credential.auth_token,
    )


def build_inbound_sms_url(base_url: str, sms_config_id: str, webhook_secret: str) -> str:
    return f"{base_url.rstrip('/')}/v1/sms/inbound/{sms_config_id}?token={webhook_secret}"
