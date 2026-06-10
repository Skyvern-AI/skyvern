"""Tests for the fail-open user-resolution dependency used for write attribution."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyvern.forge.sdk.services import org_auth_service


@pytest.mark.asyncio
async def test_returns_user_id_for_valid_bearer() -> None:
    with patch("skyvern.forge.sdk.services.org_auth_service.app") as mock_app:
        mock_app.authenticate_user_function = AsyncMock(return_value="user_abc")
        result = await org_auth_service.get_current_user_id_or_none(
            authorization="Bearer good-token",
            x_api_key=None,
            x_user_agent=None,
        )
    assert result == "user_abc"


@pytest.mark.asyncio
async def test_returns_none_without_credentials() -> None:
    with patch("skyvern.forge.sdk.services.org_auth_service.app") as mock_app:
        mock_app.authenticate_user_function = None
        result = await org_auth_service.get_current_user_id_or_none(
            authorization=None,
            x_api_key=None,
            x_user_agent=None,
        )
    assert result is None


@pytest.mark.asyncio
async def test_returns_none_for_invalid_bearer() -> None:
    with patch("skyvern.forge.sdk.services.org_auth_service.app") as mock_app:
        mock_app.authenticate_user_function = AsyncMock(return_value=None)
        result = await org_auth_service.get_current_user_id_or_none(
            authorization="Bearer bad-token",
            x_api_key=None,
            x_user_agent=None,
        )
    assert result is None


@pytest.mark.asyncio
async def test_returns_synthetic_id_for_api_key_with_ui_agent() -> None:
    organization = MagicMock()
    organization.organization_id = "o_123"
    with (
        patch("skyvern.forge.sdk.services.org_auth_service.app") as mock_app,
        patch(
            "skyvern.forge.sdk.services.org_auth_service.get_current_org_cached",
            new=AsyncMock(return_value=organization),
        ),
    ):
        mock_app.authenticate_user_function = None
        result = await org_auth_service.get_current_user_id_or_none(
            authorization=None,
            x_api_key="some-api-key",
            x_user_agent="skyvern-ui",
        )
    assert result == "o_123_user"


@pytest.mark.asyncio
async def test_returns_none_for_api_key_without_ui_agent() -> None:
    with patch("skyvern.forge.sdk.services.org_auth_service.app") as mock_app:
        mock_app.authenticate_user_function = None
        result = await org_auth_service.get_current_user_id_or_none(
            authorization=None,
            x_api_key="some-api-key",
            x_user_agent=None,
        )
    assert result is None


@pytest.mark.asyncio
async def test_returns_user_id_when_bearer_user_is_member_of_api_key_org() -> None:
    organization = MagicMock()
    organization.organization_id = "o_123"
    with (
        patch("skyvern.forge.sdk.services.org_auth_service.app") as mock_app,
        patch(
            "skyvern.forge.sdk.services.org_auth_service.get_current_org_cached",
            new=AsyncMock(return_value=organization),
        ),
    ):
        mock_app.authenticate_user_function = AsyncMock(return_value="user_abc")
        mock_app.authentication_function = AsyncMock(return_value=organization)
        mock_app.AGENT_FUNCTION.validate_user_organization_membership = AsyncMock(return_value=True)
        result = await org_auth_service.get_current_user_id_or_none(
            authorization="Bearer good-token",
            x_api_key="some-api-key",
            x_user_agent="skyvern-ui",
        )
    assert result == "user_abc"
    mock_app.AGENT_FUNCTION.validate_user_organization_membership.assert_awaited_once_with(
        user_id="user_abc",
        organization_id="o_123",
        bearer_token="good-token",
    )
    # Bearer org auth is side-effectful in overrides; the guard must never invoke it.
    mock_app.authentication_function.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("membership_verdict", [False, None])
async def test_returns_none_unless_bearer_user_membership_verified(membership_verdict: bool | None) -> None:
    # Org auth wins on x-api-key while the user comes from the bearer; a stale
    # cross-org key must not stamp the bearer user into the api-key org.
    key_org = MagicMock()
    key_org.organization_id = "o_key"
    with (
        patch("skyvern.forge.sdk.services.org_auth_service.app") as mock_app,
        patch(
            "skyvern.forge.sdk.services.org_auth_service.get_current_org_cached",
            new=AsyncMock(return_value=key_org),
        ),
    ):
        mock_app.authenticate_user_function = AsyncMock(return_value="user_abc")
        mock_app.authentication_function = AsyncMock(return_value=key_org)
        mock_app.AGENT_FUNCTION.validate_user_organization_membership = AsyncMock(return_value=membership_verdict)
        result = await org_auth_service.get_current_user_id_or_none(
            authorization="Bearer good-token",
            x_api_key="some-api-key",
            x_user_agent="skyvern-ui",
        )
    assert result is None
    mock_app.authentication_function.assert_not_called()


@pytest.mark.asyncio
async def test_returns_none_on_unexpected_auth_error() -> None:
    with patch("skyvern.forge.sdk.services.org_auth_service.app") as mock_app:
        mock_app.authenticate_user_function = AsyncMock(side_effect=RuntimeError("auth backend down"))
        result = await org_auth_service.get_current_user_id_or_none(
            authorization="Bearer token",
            x_api_key=None,
            x_user_agent=None,
        )
    assert result is None
