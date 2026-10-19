from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, call

import pytest
from fastapi import HTTPException

from skyvern.forge.sdk.db.enums import OrganizationAuthTokenType
from skyvern.forge.sdk.routes import credentials as routes
from skyvern.forge.sdk.schemas.organizations import (
    CreateOnePasswordTokenRequest,
    Organization,
    OrganizationAuthToken,
)


class FakeOrganizationsRepository:
    def __init__(self) -> None:
        self.tokens: list[OrganizationAuthToken] = []
        self.invalidate_calls: list[tuple[str, OrganizationAuthTokenType]] = []

    async def invalidate_org_auth_tokens(self, organization_id: str, token_type: OrganizationAuthTokenType) -> int:
        self.invalidate_calls.append((organization_id, token_type))
        invalidated_count = 0
        for token in self.tokens:
            if token.organization_id == organization_id and token.token_type == token_type and token.valid:
                token.valid = False
                invalidated_count += 1
        return invalidated_count

    async def get_valid_org_auth_token(self, organization_id: str, token_type: str) -> OrganizationAuthToken | None:
        return next(
            (
                t
                for t in self.tokens
                if t.organization_id == organization_id and t.token_type.value == token_type and t.valid
            ),
            None,
        )

    async def create_org_auth_token(
        self,
        organization_id: str,
        token_type: OrganizationAuthTokenType,
        token: str,
    ) -> OrganizationAuthToken:
        now = datetime.now(timezone.utc)
        auth_token = OrganizationAuthToken(
            id="oat_created",
            organization_id=organization_id,
            token_type=token_type,
            token=token,
            valid=True,
            created_at=now,
            modified_at=now,
        )
        self.tokens.append(auth_token)
        return auth_token


def _org() -> Organization:
    now = datetime.now(timezone.utc)
    return Organization(organization_id="o_test", organization_name="Test Org", created_at=now, modified_at=now)


GetRoute = Callable[[Organization], Awaitable[object]]


PROVIDERS: list[tuple[str, OrganizationAuthTokenType, GetRoute]] = [
    ("onepassword", OrganizationAuthTokenType.onepassword_service_account, routes.get_onepassword_token),
    ("bitwarden", OrganizationAuthTokenType.bitwarden_credential, routes.get_bitwarden_credential),
    (
        "azure_credential",
        OrganizationAuthTokenType.azure_client_secret_credential,
        routes.get_azure_client_secret_credential,
    ),
    (
        "custom_credential",
        OrganizationAuthTokenType.custom_credential_service,
        routes.get_custom_credential_service_config,
    ),
]


@pytest.fixture
def fake_organizations(monkeypatch: pytest.MonkeyPatch) -> FakeOrganizationsRepository:
    organizations = FakeOrganizationsRepository()
    monkeypatch.setattr(routes.app, "DATABASE", SimpleNamespace(organizations=organizations))
    return organizations


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_path, token_type, get_route", PROVIDERS, ids=[provider[0] for provider in PROVIDERS])
async def test_clear_org_auth_credential_invalidates_existing_and_get_404s(
    fake_organizations: FakeOrganizationsRepository,
    provider_path: str,
    token_type: OrganizationAuthTokenType,
    get_route: GetRoute,
) -> None:
    org = _org()
    now = datetime.now(timezone.utc)
    fake_organizations.tokens.append(
        OrganizationAuthToken(
            id="oat_test",
            organization_id=org.organization_id,
            token_type=token_type,
            token="secret",
            valid=True,
            created_at=now,
            modified_at=now,
        )
    )

    response = await routes.clear_org_auth_credential(provider_path, org)

    assert response.success is True
    assert fake_organizations.invalidate_calls == [(org.organization_id, token_type)]
    assert fake_organizations.tokens[0].valid is False
    with pytest.raises(HTTPException) as exc_info:
        await get_route(org)
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_clear_org_auth_credential_succeeds_without_existing_token(
    fake_organizations: FakeOrganizationsRepository,
) -> None:
    assert (await routes.clear_org_auth_credential("onepassword", _org())).success is True
    assert len(fake_organizations.invalidate_calls) == 1


@pytest.mark.asyncio
async def test_clear_onepassword_without_existing_token_does_not_capture_success_event(
    monkeypatch: pytest.MonkeyPatch,
    fake_organizations: FakeOrganizationsRepository,
) -> None:
    capture = AsyncMock()
    monkeypatch.setattr(
        routes.app,
        "AGENT_FUNCTION",
        SimpleNamespace(capture_onepassword_telemetry=capture),
    )

    response = await routes.clear_org_auth_credential("onepassword", _org())

    assert response.success is True
    capture.assert_not_awaited()


@pytest.mark.asyncio
async def test_onepassword_token_mutations_capture_safe_success_events(
    monkeypatch: pytest.MonkeyPatch,
    fake_organizations: FakeOrganizationsRepository,
) -> None:
    org = _org()
    capture = AsyncMock()
    monkeypatch.setattr(
        routes.app,
        "AGENT_FUNCTION",
        SimpleNamespace(capture_onepassword_telemetry=capture),
    )
    monkeypatch.setattr(routes, "record_request_audit_event", AsyncMock())
    monkeypatch.setattr(routes, "_validate_onepassword_service_account_token", AsyncMock())

    await routes.update_onepassword_token(CreateOnePasswordTokenRequest(token="op_service_secret"), org)
    await routes.clear_org_auth_credential("onepassword", org)

    assert capture.call_args_list == [
        call(
            organization_id=org.organization_id,
            event="onepassword.token_saved",
            success=True,
        ),
        call(
            organization_id=org.organization_id,
            event="onepassword.token_cleared",
            success=True,
        ),
    ]
    assert "op_service_secret" not in repr(capture.call_args_list)


@pytest.mark.asyncio
async def test_onepassword_token_mutation_failures_capture_only_error_class(
    monkeypatch: pytest.MonkeyPatch,
    fake_organizations: FakeOrganizationsRepository,
) -> None:
    org = _org()
    capture = AsyncMock()
    monkeypatch.setattr(
        routes.app,
        "AGENT_FUNCTION",
        SimpleNamespace(capture_onepassword_telemetry=capture),
    )
    monkeypatch.setattr(routes, "record_request_audit_event", AsyncMock())
    monkeypatch.setattr(routes, "_validate_onepassword_service_account_token", AsyncMock())
    fake_organizations.create_org_auth_token = AsyncMock(side_effect=RuntimeError("sensitive failure"))

    with pytest.raises(HTTPException):
        await routes.update_onepassword_token(CreateOnePasswordTokenRequest(token="op_service_secret"), org)
    fake_organizations.invalidate_org_auth_tokens = AsyncMock(side_effect=RuntimeError("sensitive failure"))
    with pytest.raises(HTTPException):
        await routes.clear_org_auth_credential("onepassword", org)

    assert capture.call_args_list == [
        call(
            organization_id=org.organization_id,
            event="onepassword.token_saved",
            success=False,
            error_class="internal",
        ),
        call(
            organization_id=org.organization_id,
            event="onepassword.token_cleared",
            success=False,
            error_class="internal",
        ),
    ]
    assert "sensitive failure" not in repr(capture.call_args_list)


@pytest.mark.asyncio
async def test_custom_credential_status_reports_configured_without_the_token(
    fake_organizations: FakeOrganizationsRepository,
) -> None:
    org = _org()
    now = datetime.now(timezone.utc)
    fake_organizations.tokens.append(
        OrganizationAuthToken(
            id="oat_test",
            organization_id=org.organization_id,
            token_type=OrganizationAuthTokenType.custom_credential_service,
            token="secret",
            valid=True,
            created_at=now,
            modified_at=now,
        )
    )

    status = await routes.get_custom_credential_service_status(org)
    assert status.model_dump() == {"configured": True}

    await routes.clear_org_auth_credential("custom_credential", org)
    assert (await routes.get_custom_credential_service_status(org)).configured is False
