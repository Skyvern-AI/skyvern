"""Tests for SKY-9294: 1Password failures must surface a clear, user-facing failure_reason
that names 1Password as the failing dependency rather than leaking the raw SDK error.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from onepassword import ItemFieldType
from onepassword.errors import DesktopSessionExpiredException, RateLimitExceededException

from skyvern.exceptions import (
    OnePasswordGetItemError,
    OnePasswordRateLimitError,
    OnePasswordServiceUnavailableError,
    OnePasswordSessionExpiredError,
)
from skyvern.forge import app as forge_app
from skyvern.forge.sdk.db.enums import OrganizationAuthTokenType
from skyvern.forge.sdk.routes.credentials import list_onepassword_items, update_onepassword_token
from skyvern.forge.sdk.schemas.organizations import CreateOnePasswordTokenRequest
from skyvern.forge.sdk.services.credentials import (
    extract_onepassword_upstream_5xx_status,
    is_onepassword_credential_error,
)
from skyvern.forge.sdk.workflow.context_manager import WorkflowRunContext
from skyvern.forge.sdk.workflow.models.parameter import OnePasswordCredentialParameter

_TEST_TOTP_SECRET = "JBSWY3DPEHPK3PXP"


def _make_context() -> WorkflowRunContext:
    return WorkflowRunContext(
        workflow_title="wf-title",
        workflow_id="wf-id",
        workflow_permanent_id="wf-perm-id",
        workflow_run_id="wf-run-id",
        aws_client=MagicMock(),
    )


def _make_parameter(
    vault_id: str = "vault123",
    item_id: str = "item123",
    totp_field_name: str | None = None,
) -> OnePasswordCredentialParameter:
    now = datetime.now(UTC)
    return OnePasswordCredentialParameter(
        key="op_cred",
        onepassword_credential_parameter_id="opcp_test",
        workflow_id="wf-id",
        vault_id=vault_id,
        item_id=item_id,
        totp_field_name=totp_field_name,
        created_at=now,
        modified_at=now,
    )


def _make_field(field_id: str, title: str | None, value: str, field_type: ItemFieldType) -> SimpleNamespace:
    return SimpleNamespace(id=field_id, title=title, value=value, field_type=field_type)


def _make_organization() -> MagicMock:
    org = MagicMock()
    org.organization_id = "o_test"
    return org


@pytest.fixture
def mocked_app_database():
    """Patch app.DATABASE so org-auth-token lookup returns None (forcing fallback to settings token)."""
    original = getattr(forge_app, "DATABASE", None)
    forge_app.DATABASE = MagicMock()
    forge_app.DATABASE.organizations.get_valid_org_auth_token = AsyncMock(return_value=None)
    forge_app.DATABASE.credentials.get_onepassword_credentials = AsyncMock(return_value=[])
    yield forge_app.DATABASE
    if original is None:
        del forge_app.DATABASE
    else:
        forge_app.DATABASE = original


@pytest.fixture
def patched_settings_token(monkeypatch):
    monkeypatch.setattr("skyvern.forge.sdk.workflow.context_manager.settings.OP_SERVICE_ACCOUNT_TOKEN", "fake-token")


@pytest.mark.asyncio
async def test_onepassword_503_surfaces_service_unavailable_error(mocked_app_database, patched_settings_token):
    ctx = _make_context()
    with patch(
        "skyvern.forge.sdk.services.credentials.OnePasswordClient.authenticate",
        new_callable=AsyncMock,
        side_effect=Exception("Server error: 503 Service Unavailable"),
    ):
        with pytest.raises(OnePasswordServiceUnavailableError) as exc_info:
            await ctx.register_onepassword_credential_parameter_value(_make_parameter(), _make_organization())

    message = exc_info.value.message
    assert "1Password is currently unavailable" in message
    assert "503" in message
    assert "upstream outage" in message


@pytest.mark.asyncio
async def test_onepassword_503_from_items_get_surfaces_service_unavailable_error(
    mocked_app_database, patched_settings_token
):
    ctx = _make_context()
    fake_client = MagicMock()
    fake_client.items.get = AsyncMock(side_effect=Exception("upstream returned 502 bad gateway"))
    with patch(
        "skyvern.forge.sdk.services.credentials.OnePasswordClient.authenticate",
        new_callable=AsyncMock,
        return_value=fake_client,
    ):
        with pytest.raises(OnePasswordServiceUnavailableError) as exc_info:
            await ctx.register_onepassword_credential_parameter_value(_make_parameter(), _make_organization())

    assert "502" in exc_info.value.message


@pytest.mark.asyncio
async def test_onepassword_rate_limit_maps_to_typed_error(mocked_app_database, patched_settings_token):
    ctx = _make_context()
    with patch(
        "skyvern.forge.sdk.services.credentials.OnePasswordClient.authenticate",
        new_callable=AsyncMock,
        side_effect=RateLimitExceededException("rate limit hit"),
    ):
        with pytest.raises(OnePasswordRateLimitError) as exc_info:
            await ctx.register_onepassword_credential_parameter_value(_make_parameter(), _make_organization())

    assert "rate limit" in exc_info.value.message.lower()
    assert "rate limit hit" in exc_info.value.message


@pytest.mark.asyncio
async def test_onepassword_session_expired_maps_to_typed_error(mocked_app_database, patched_settings_token):
    ctx = _make_context()
    with patch(
        "skyvern.forge.sdk.services.credentials.OnePasswordClient.authenticate",
        new_callable=AsyncMock,
        side_effect=DesktopSessionExpiredException("session expired"),
    ):
        with pytest.raises(OnePasswordSessionExpiredError) as exc_info:
            await ctx.register_onepassword_credential_parameter_value(_make_parameter(), _make_organization())

    assert "session expired" in exc_info.value.message
    assert "1Password" in exc_info.value.message


@pytest.mark.asyncio
async def test_onepassword_generic_error_falls_back_to_get_item_error(mocked_app_database, patched_settings_token):
    ctx = _make_context()
    with patch(
        "skyvern.forge.sdk.services.credentials.OnePasswordClient.authenticate",
        new_callable=AsyncMock,
        side_effect=Exception("something weird happened"),
    ):
        with pytest.raises(OnePasswordGetItemError) as exc_info:
            await ctx.register_onepassword_credential_parameter_value(_make_parameter(), _make_organization())

    assert "something weird happened" in exc_info.value.message
    assert "1Password" in exc_info.value.message


@pytest.mark.asyncio
async def test_onepassword_error_includes_resolved_vault_and_item_ids(
    mocked_app_database,
    patched_settings_token,
):
    ctx = _make_context()
    ctx.values["vault_param"] = "resolved-vault-id"
    ctx.values["item_param"] = "resolved-item-id"

    with patch(
        "skyvern.forge.sdk.services.credentials.OnePasswordClient.authenticate",
        new_callable=AsyncMock,
        side_effect=Exception("lookup failed"),
    ):
        with pytest.raises(OnePasswordGetItemError) as exc_info:
            await ctx.register_onepassword_credential_parameter_value(
                _make_parameter(vault_id="{{ vault_param }}", item_id="{{ item_param }}"),
                _make_organization(),
            )

    message = exc_info.value.message
    assert "vault_id=resolved-vault-id" in message
    assert "item_id=resolved-item-id" in message
    assert "{{ vault_param }}" not in message
    assert "{{ item_param }}" not in message
    assert "fake-token" not in message


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field_type, totp_field_name",
    [
        # Regression: 1Password's own TOTP field type is still auto-detected with no override set.
        (ItemFieldType.TOTP, None),
        # SKY-14541: a plain field (e.g. a custom field named "digits") isn't tagged as 1Password's
        # native TOTP type, so an external automation that writes the OTP seed there needs
        # totp_field_name to tell Skyvern which field carries it. Matching is case-insensitive.
        (ItemFieldType.CONCEALED, "DIGITS"),
    ],
)
async def test_onepassword_totp_field_is_detected(
    mocked_app_database, patched_settings_token, field_type, totp_field_name
):
    ctx = _make_context()
    forge_app.AGENT_FUNCTION.parse_enterprise_totp_secret = AsyncMock(return_value=None)
    fake_item = SimpleNamespace(
        fields=[_make_field("digits", "Digits", _TEST_TOTP_SECRET, field_type)],
        notes=None,
    )
    fake_client = MagicMock()
    fake_client.items.get = AsyncMock(return_value=fake_item)
    with patch(
        "skyvern.forge.sdk.services.credentials.OnePasswordClient.authenticate",
        new_callable=AsyncMock,
        return_value=fake_client,
    ):
        await ctx.register_onepassword_credential_parameter_value(
            _make_parameter(totp_field_name=totp_field_name), _make_organization()
        )

    totp_secret_id = ctx.values["op_cred"]["totp"]
    assert ctx.secrets[ctx.totp_secret_value_key(totp_secret_id)] == _TEST_TOTP_SECRET


@pytest.mark.asyncio
async def test_onepassword_totp_field_name_matches_by_id_when_title_is_missing(
    mocked_app_database, patched_settings_token
):
    """A field with no title (title=None is valid per the 1Password SDK) must still match by id
    instead of raising AttributeError out of the credential registration."""
    ctx = _make_context()
    forge_app.AGENT_FUNCTION.parse_enterprise_totp_secret = AsyncMock(return_value=None)
    fake_item = SimpleNamespace(
        fields=[_make_field("digits", None, _TEST_TOTP_SECRET, ItemFieldType.CONCEALED)],
        notes=None,
    )
    fake_client = MagicMock()
    fake_client.items.get = AsyncMock(return_value=fake_item)
    with patch(
        "skyvern.forge.sdk.services.credentials.OnePasswordClient.authenticate",
        new_callable=AsyncMock,
        return_value=fake_client,
    ):
        await ctx.register_onepassword_credential_parameter_value(
            _make_parameter(totp_field_name="digits"), _make_organization()
        )

    totp_secret_id = ctx.values["op_cred"]["totp"]
    assert ctx.secrets[ctx.totp_secret_value_key(totp_secret_id)] == _TEST_TOTP_SECRET


@pytest.mark.asyncio
async def test_onepassword_unmatched_totp_field_name_falls_back_to_plain_field(
    mocked_app_database, patched_settings_token
):
    """A totp_field_name that matches nothing must not crash the run — the field is registered
    as a plain secret and no TOTP value is set."""
    ctx = _make_context()
    fake_item = SimpleNamespace(
        fields=[_make_field("digits", "Digits", _TEST_TOTP_SECRET, ItemFieldType.CONCEALED)],
        notes=None,
    )
    fake_client = MagicMock()
    fake_client.items.get = AsyncMock(return_value=fake_item)
    with patch(
        "skyvern.forge.sdk.services.credentials.OnePasswordClient.authenticate",
        new_callable=AsyncMock,
        return_value=fake_client,
    ):
        await ctx.register_onepassword_credential_parameter_value(
            _make_parameter(totp_field_name="otp_code"), _make_organization()
        )

    assert "totp" not in ctx.values["op_cred"]


@pytest.mark.asyncio
async def test_onepassword_incidental_5xx_digits_do_not_trigger_service_unavailable(
    mocked_app_database, patched_settings_token
):
    """Numbers like '500' that appear in error messages without HTTP/status context must not be
    misclassified as service-unavailable. False-positives would mislead users into retrying a
    permanent failure as if it were a transient outage."""
    ctx = _make_context()
    with patch(
        "skyvern.forge.sdk.services.credentials.OnePasswordClient.authenticate",
        new_callable=AsyncMock,
        side_effect=Exception("vault has 500 items, exceeded soft limit"),
    ):
        with pytest.raises(OnePasswordGetItemError) as exc_info:
            await ctx.register_onepassword_credential_parameter_value(_make_parameter(), _make_organization())

    assert "exceeded soft limit" in exc_info.value.message


@pytest.mark.asyncio
async def test_onepassword_status_keyword_5xx_classified_as_service_unavailable(
    mocked_app_database, patched_settings_token
):
    """A 5xx digit paired with a status keyword (HTTP/status/code) must be classified."""
    ctx = _make_context()
    with patch(
        "skyvern.forge.sdk.services.credentials.OnePasswordClient.authenticate",
        new_callable=AsyncMock,
        side_effect=Exception("request failed with status: 504"),
    ):
        with pytest.raises(OnePasswordServiceUnavailableError) as exc_info:
            await ctx.register_onepassword_credential_parameter_value(_make_parameter(), _make_organization())

    assert "504" in exc_info.value.message


@pytest.mark.parametrize(
    "message, expected",
    [
        ("Server error: 503 Service Unavailable", 503),
        ("upstream returned 502 bad gateway", 502),
        ("request failed with status: 504", 504),
        ("HTTP 500 internal server error", 500),
    ],
)
def test_extract_onepassword_upstream_5xx_status_matches(message: str, expected: int) -> None:
    assert extract_onepassword_upstream_5xx_status(message) == expected


@pytest.mark.parametrize(
    "message",
    [
        "invalid service account token",
        "authentication failed: token is not valid",
        "vault has 500 items, exceeded soft limit",
        "could not parse token",
    ],
)
def test_extract_onepassword_upstream_5xx_status_ignores_credential_errors(message: str) -> None:
    # Bad-token / non-upstream failures must not look like a 5xx. The route only
    # maps them to 4xx when the credential-error classifier has positive evidence.
    assert extract_onepassword_upstream_5xx_status(message) is None


@pytest.mark.parametrize(
    "message",
    [
        "invalid service account token",
        "authentication failed: token is not valid",
        "request forbidden for this service account",
        "malformed credential payload",
        "could not parse token",
    ],
)
def test_is_onepassword_credential_error_matches_auth_failures(message: str) -> None:
    assert is_onepassword_credential_error(message)


@pytest.mark.parametrize(
    "message",
    [
        "",
        "request timed out",
        "rate limit hit",
        "vault has 500 items, exceeded soft limit",
        "unexpected parser bug",
    ],
)
def test_is_onepassword_credential_error_ignores_unknown_failures(message: str) -> None:
    assert not is_onepassword_credential_error(message)


@pytest.fixture
def mocked_route_app_database():
    original = getattr(forge_app, "DATABASE", None)
    forge_app.DATABASE = MagicMock()
    forge_app.DATABASE.organizations.get_valid_org_auth_token = AsyncMock(
        return_value=SimpleNamespace(token="org-token", modified_at=None)
    )
    yield forge_app.DATABASE
    if original is None:
        del forge_app.DATABASE
    else:
        forge_app.DATABASE = original


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "sdk_error, expected_status, expected_detail, expected_error_class",
    [
        (
            Exception("invalid service account token"),
            400,
            "service account token appears to be invalid or expired",
            "invalid_token",
        ),
        (
            Exception("authentication failed, error code: 500"),
            400,
            "service account token appears to be invalid or expired",
            "invalid_token",
        ),
        (Exception("Server error: 503 Service Unavailable"), 502, "temporarily unavailable", "upstream"),
        (TimeoutError(), 502, "temporarily unavailable", "timeout"),
        (RateLimitExceededException("rate limit hit"), 429, "rate limit exceeded", "rate_limited"),
        (DesktopSessionExpiredException("session expired"), 400, "session appears to be expired", "invalid_token"),
        (Exception("unexpected parser bug"), 500, "Failed to list 1Password items", "upstream"),
    ],
)
async def test_list_onepassword_items_classifies_failures(
    mocked_route_app_database,
    monkeypatch: pytest.MonkeyPatch,
    sdk_error: Exception,
    expected_status: int,
    expected_detail: str,
    expected_error_class: str,
) -> None:
    capture = AsyncMock()
    monkeypatch.setattr(
        forge_app,
        "AGENT_FUNCTION",
        SimpleNamespace(
            capture_onepassword_telemetry=capture,
            onepassword_instance_default_policy_mode=lambda: "unrestricted",
        ),
    )
    with (
        patch(
            "skyvern.forge.sdk.routes.credentials.OnePasswordClient.authenticate",
            new_callable=AsyncMock,
            side_effect=sdk_error,
        ),
        pytest.raises(HTTPException) as exc_info,
    ):
        await list_onepassword_items(_make_organization())

    assert exc_info.value.status_code == expected_status
    assert expected_detail in exc_info.value.detail
    capture.assert_called_once_with(
        organization_id="o_test",
        event="onepassword.items_loaded",
        success=False,
        error_class=expected_error_class,
    )


@pytest.mark.asyncio
async def test_list_onepassword_items_captures_count_without_item_metadata(
    mocked_route_app_database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capture = AsyncMock()
    monkeypatch.setattr(
        forge_app,
        "AGENT_FUNCTION",
        SimpleNamespace(
            capture_onepassword_telemetry=capture,
            onepassword_instance_default_policy_mode=lambda: "unrestricted",
            credential_folders_restricted=AsyncMock(return_value=False),
            accessible_credential_ids=AsyncMock(return_value=set()),
        ),
    )
    item = SimpleNamespace(
        id="item-id",
        title="Sensitive Item Title",
        category=SimpleNamespace(value="LOGIN"),
        websites=[SimpleNamespace(url="https://example.invalid")],
    )
    other_categories = [
        SimpleNamespace(
            id=category.lower(),
            title=category,
            category=SimpleNamespace(value=category),
            websites=[],
        )
        for category in ("CREDIT_CARD", "PASSWORD", "DOCUMENT")
    ]
    client = MagicMock()
    client.vaults.list = AsyncMock(return_value=[SimpleNamespace(id="vault-id", title="Sensitive Vault Name")])
    client.items.list = AsyncMock(return_value=[item, *other_categories])

    with patch(
        "skyvern.forge.sdk.routes.credentials.OnePasswordClient.authenticate",
        new_callable=AsyncMock,
        return_value=client,
    ):
        response = await list_onepassword_items(_make_organization())

    assert [item.importable for item in response.items] == [True, True, False, False]
    capture.assert_called_once_with(
        organization_id="o_test",
        event="onepassword.items_loaded",
        success=True,
        item_count=4,
    )
    assert "Sensitive Item Title" not in repr(capture.call_args)
    assert "Sensitive Vault Name" not in repr(capture.call_args)


@pytest.mark.asyncio
async def test_list_onepassword_items_classifies_local_conversion_failure_as_internal(
    mocked_route_app_database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capture = AsyncMock()
    monkeypatch.setattr(
        forge_app,
        "AGENT_FUNCTION",
        SimpleNamespace(
            capture_onepassword_telemetry=capture,
            onepassword_instance_default_policy_mode=lambda: "unrestricted",
        ),
    )
    client = MagicMock()
    client.vaults.list = AsyncMock(return_value=[SimpleNamespace(id="vault", title="Vault")])
    client.items.list = AsyncMock(
        return_value=[SimpleNamespace(id="item", title="Item", category=object(), websites=[])]
    )

    with (
        patch(
            "skyvern.forge.sdk.routes.credentials.OnePasswordClient.authenticate",
            new_callable=AsyncMock,
            return_value=client,
        ),
        pytest.raises(HTTPException) as exc_info,
    ):
        await list_onepassword_items(_make_organization())

    assert exc_info.value.status_code == 500
    assert exc_info.value.detail == "Failed to list 1Password items."
    capture.assert_awaited_once_with(
        organization_id="o_test",
        event="onepassword.items_loaded",
        success=False,
        error_class="internal",
    )


@pytest.mark.asyncio
async def test_list_onepassword_items_does_not_capture_unconfigured_org(
    mocked_route_app_database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mocked_route_app_database.organizations.get_valid_org_auth_token = AsyncMock(return_value=None)
    monkeypatch.setattr("skyvern.forge.sdk.services.onepassword_token_service.settings.OP_SERVICE_ACCOUNT_TOKEN", None)
    capture = AsyncMock()
    monkeypatch.setattr(
        forge_app,
        "AGENT_FUNCTION",
        SimpleNamespace(
            capture_onepassword_telemetry=capture,
            onepassword_instance_default_policy_mode=lambda: "unrestricted",
        ),
    )

    response = await list_onepassword_items(_make_organization())

    assert response.configured is False
    assert response.items == []
    capture.assert_not_awaited()


@pytest.mark.asyncio
async def test_list_onepassword_items_classifies_token_resolution_failure_as_internal(
    mocked_route_app_database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mocked_route_app_database.organizations.get_valid_org_auth_token.side_effect = RuntimeError("database unavailable")
    capture = AsyncMock()
    monkeypatch.setattr(
        forge_app,
        "AGENT_FUNCTION",
        SimpleNamespace(
            capture_onepassword_telemetry=capture,
            onepassword_instance_default_policy_mode=lambda: "unrestricted",
        ),
    )

    with pytest.raises(RuntimeError, match="database unavailable"):
        await list_onepassword_items(_make_organization())

    capture.assert_awaited_once_with(
        organization_id="o_test",
        event="onepassword.items_loaded",
        success=False,
        error_class="internal",
    )


@pytest.mark.asyncio
async def test_list_onepassword_items_captures_successful_empty_vault(
    mocked_route_app_database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capture = AsyncMock()
    monkeypatch.setattr(
        forge_app,
        "AGENT_FUNCTION",
        SimpleNamespace(
            capture_onepassword_telemetry=capture,
            onepassword_instance_default_policy_mode=lambda: "unrestricted",
        ),
    )
    client = MagicMock()
    client.vaults.list = AsyncMock(return_value=[])

    with patch(
        "skyvern.forge.sdk.routes.credentials.OnePasswordClient.authenticate",
        new_callable=AsyncMock,
        return_value=client,
    ):
        response = await list_onepassword_items(_make_organization())

    assert response.configured is True
    assert response.items == []
    capture.assert_awaited_once_with(
        organization_id="o_test",
        event="onepassword.items_loaded",
        success=True,
        item_count=0,
    )


@pytest.fixture
def tracked_onepassword_tokens(mocked_route_app_database):
    now = datetime.now(UTC)
    existing_token = SimpleNamespace(token="existing-token", valid=True)
    active_tokens = [existing_token]

    async def invalidate_tokens(**_kwargs):
        active_tokens.clear()

    async def create_token(**_kwargs):
        token = SimpleNamespace(
            id="new-token",
            organization_id="o_test",
            token_type=OrganizationAuthTokenType.onepassword_service_account,
            valid=True,
            created_at=now,
            modified_at=now,
        )
        active_tokens.append(token)
        return token

    mocked_route_app_database.organizations.invalidate_org_auth_tokens = AsyncMock(side_effect=invalidate_tokens)
    mocked_route_app_database.organizations.create_org_auth_token = AsyncMock(side_effect=create_token)
    return mocked_route_app_database, active_tokens, existing_token


@pytest.mark.asyncio
async def test_update_onepassword_token_rejects_invalid_token_before_replacing_existing(
    tracked_onepassword_tokens,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, active_tokens, existing_token = tracked_onepassword_tokens
    capture = AsyncMock()
    monkeypatch.setattr(
        forge_app,
        "AGENT_FUNCTION",
        SimpleNamespace(capture_onepassword_telemetry=capture),
    )
    with (
        patch(
            "skyvern.forge.sdk.routes.credentials.OnePasswordClient.authenticate",
            new_callable=AsyncMock,
            side_effect=Exception("invalid service account token"),
        ),
        patch("skyvern.forge.sdk.routes.credentials.record_request_audit_event", new_callable=AsyncMock),
        pytest.raises(HTTPException) as exc_info,
    ):
        await update_onepassword_token(CreateOnePasswordTokenRequest(token="invalid-token"), _make_organization())

    assert exc_info.value.status_code == 400
    assert "invalid or expired" in exc_info.value.detail
    assert active_tokens == [existing_token]
    database.organizations.invalidate_org_auth_tokens.assert_not_awaited()
    database.organizations.create_org_auth_token.assert_not_awaited()
    capture.assert_called_once_with(
        organization_id="o_test",
        event="onepassword.token_saved",
        success=False,
        error_class="invalid_token",
    )


@pytest.mark.asyncio
async def test_update_onepassword_token_keeps_existing_token_when_validation_times_out(
    tracked_onepassword_tokens,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, active_tokens, existing_token = tracked_onepassword_tokens
    capture = AsyncMock()
    monkeypatch.setattr(
        forge_app,
        "AGENT_FUNCTION",
        SimpleNamespace(capture_onepassword_telemetry=capture),
    )
    client = SimpleNamespace(vaults=SimpleNamespace(list=AsyncMock(side_effect=TimeoutError())))
    with (
        patch(
            "skyvern.forge.sdk.routes.credentials.OnePasswordClient.authenticate",
            new_callable=AsyncMock,
            return_value=client,
        ) as authenticate,
        pytest.raises(HTTPException) as exc_info,
    ):
        await update_onepassword_token(CreateOnePasswordTokenRequest(token="new-token"), _make_organization())

    assert exc_info.value.status_code == 502
    assert "temporarily unavailable" in exc_info.value.detail
    authenticate.assert_awaited_once()
    client.vaults.list.assert_awaited_once()
    assert active_tokens == [existing_token]
    database.organizations.invalidate_org_auth_tokens.assert_not_awaited()
    database.organizations.create_org_auth_token.assert_not_awaited()
    capture.assert_called_once_with(
        organization_id="o_test",
        event="onepassword.token_saved",
        success=False,
        error_class="timeout",
    )
