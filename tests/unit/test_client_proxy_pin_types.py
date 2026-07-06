import datetime as dt
from types import SimpleNamespace
from typing import cast
from unittest.mock import Mock

from skyvern.client.core.client_wrapper import SyncClientWrapper
from skyvern.client.raw_client import RawSkyvern
from skyvern.client.types.browser_profile import BrowserProfile
from skyvern.client.types.credential_response import CredentialResponse
from skyvern.client.types.non_empty_password_credential import NonEmptyPasswordCredential
from skyvern.client.types.password_credential_response import PasswordCredentialResponse


def test_client_browser_profile_exposes_proxy_pin_fields() -> None:
    profile = BrowserProfile(
        browser_profile_id="bprof_123",
        organization_id="org_123",
        name="Pinned profile",
        proxy_location="RESIDENTIAL_ISP",
        proxy_session_id="abc1234567",
        created_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
        modified_at=dt.datetime(2026, 1, 2, tzinfo=dt.UTC),
    )

    assert profile.proxy_location == "RESIDENTIAL_ISP"
    assert profile.proxy_session_id == "abc1234567"


def test_client_credential_response_exposes_proxy_pin_fields() -> None:
    response = CredentialResponse(
        credential_id="cred_123",
        credential=PasswordCredentialResponse(username="user@example.com"),
        credential_type="password",
        name="Pinned credential",
        proxy_location="RESIDENTIAL_ISP",
        proxy_session_id="abc1234567",
    )

    assert response.proxy_location == "RESIDENTIAL_ISP"
    assert response.proxy_session_id == "abc1234567"


def _response(body: dict[str, object]) -> SimpleNamespace:
    return SimpleNamespace(
        status_code=200,
        headers={},
        text="",
        json=lambda: body,
    )


def _raw_client(request: Mock) -> RawSkyvern:
    client_wrapper = cast(SyncClientWrapper, SimpleNamespace(httpx_client=SimpleNamespace(request=request)))
    return RawSkyvern(client_wrapper=client_wrapper)


def test_raw_client_update_browser_profile_sends_rotate_proxy_session_id() -> None:
    request = Mock(
        return_value=_response(
            {
                "browser_profile_id": "bp_123",
                "organization_id": "org_123",
                "name": "Pinned profile",
                "proxy_location": "RESIDENTIAL_ISP",
                "proxy_session_id": "abc1234567",
                "created_at": "2026-01-01T00:00:00Z",
                "modified_at": "2026-01-02T00:00:00Z",
            }
        )
    )

    _raw_client(request).update_browser_profile(
        "bp_123",
        proxy_location="RESIDENTIAL_ISP",
        rotate_proxy_session_id=True,
    )

    assert request.call_args.kwargs["json"]["rotate_proxy_session_id"] is True


def test_raw_client_update_credential_sends_rotate_proxy_session_id() -> None:
    request = Mock(
        return_value=_response(
            {
                "credential_id": "cred_123",
                "credential": {"username": "user@example.com"},
                "credential_type": "password",
                "name": "Pinned credential",
                "proxy_location": "RESIDENTIAL_ISP",
                "proxy_session_id": "abc1234567",
            }
        )
    )

    _raw_client(request).update_credential(
        "cred_123",
        name="Pinned credential",
        credential_type="password",
        credential=NonEmptyPasswordCredential(username="user@example.com", password="pw"),
        proxy_location="RESIDENTIAL_ISP",
        rotate_proxy_session_id=True,
    )

    assert request.call_args.kwargs["json"]["rotate_proxy_session_id"] is True


def test_raw_client_create_browser_session_sends_startup_url() -> None:
    request = Mock(
        return_value=_response(
            {
                "browser_session_id": "pbs_123",
                "organization_id": "org_123",
                "status": "created",
                "created_at": "2026-01-01T00:00:00Z",
                "modified_at": "2026-01-02T00:00:00Z",
            }
        )
    )

    _raw_client(request).create_browser_session(url="https://web.telegram.org")

    assert request.call_args.kwargs["json"]["url"] == "https://web.telegram.org"
