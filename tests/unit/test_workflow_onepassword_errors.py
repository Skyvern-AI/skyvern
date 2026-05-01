"""Tests for SKY-9294: 1Password failures must surface a clear, user-facing failure_reason
that names 1Password as the failing dependency rather than leaking the raw SDK error.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from onepassword.errors import DesktopSessionExpiredException, RateLimitExceededException

from skyvern.exceptions import (
    OnePasswordGetItemError,
    OnePasswordRateLimitError,
    OnePasswordServiceUnavailableError,
    OnePasswordSessionExpiredError,
)
from skyvern.forge import app as forge_app
from skyvern.forge.sdk.workflow.context_manager import WorkflowRunContext
from skyvern.forge.sdk.workflow.models.parameter import OnePasswordCredentialParameter


def _make_context() -> WorkflowRunContext:
    return WorkflowRunContext(
        workflow_title="wf-title",
        workflow_id="wf-id",
        workflow_permanent_id="wf-perm-id",
        workflow_run_id="wf-run-id",
        aws_client=MagicMock(),
    )


def _make_parameter() -> OnePasswordCredentialParameter:
    now = datetime.now(timezone.utc)
    return OnePasswordCredentialParameter(
        key="op_cred",
        onepassword_credential_parameter_id="opcp_test",
        workflow_id="wf-id",
        vault_id="vault123",
        item_id="item123",
        created_at=now,
        modified_at=now,
    )


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
        "skyvern.forge.sdk.workflow.context_manager.OnePasswordClient.authenticate",
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
        "skyvern.forge.sdk.workflow.context_manager.OnePasswordClient.authenticate",
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
        "skyvern.forge.sdk.workflow.context_manager.OnePasswordClient.authenticate",
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
        "skyvern.forge.sdk.workflow.context_manager.OnePasswordClient.authenticate",
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
        "skyvern.forge.sdk.workflow.context_manager.OnePasswordClient.authenticate",
        new_callable=AsyncMock,
        side_effect=Exception("something weird happened"),
    ):
        with pytest.raises(OnePasswordGetItemError) as exc_info:
            await ctx.register_onepassword_credential_parameter_value(_make_parameter(), _make_organization())

    assert "something weird happened" in exc_info.value.message
    assert "1Password" in exc_info.value.message


@pytest.mark.asyncio
async def test_onepassword_incidental_5xx_digits_do_not_trigger_service_unavailable(
    mocked_app_database, patched_settings_token
):
    """Numbers like '500' that appear in error messages without HTTP/status context must not be
    misclassified as service-unavailable. False-positives would mislead users into retrying a
    permanent failure as if it were a transient outage."""
    ctx = _make_context()
    with patch(
        "skyvern.forge.sdk.workflow.context_manager.OnePasswordClient.authenticate",
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
        "skyvern.forge.sdk.workflow.context_manager.OnePasswordClient.authenticate",
        new_callable=AsyncMock,
        side_effect=Exception("request failed with status: 504"),
    ):
        with pytest.raises(OnePasswordServiceUnavailableError) as exc_info:
            await ctx.register_onepassword_credential_parameter_value(_make_parameter(), _make_organization())

    assert "504" in exc_info.value.message
