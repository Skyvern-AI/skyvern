"""Tests for poll_otp_value organization token usage by OTP context."""

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyvern.forge.sdk.db.enums import OrganizationAuthTokenType
from skyvern.forge.sdk.notification.local import LocalNotificationRegistry
from skyvern.services import otp_service
from skyvern.services.otp_service import OTPValue, poll_otp_value


@pytest.mark.asyncio
async def test_poll_otp_value_without_totp_url_does_not_require_org_token() -> None:
    """poll_otp_value should not depend on org token when no totp_verification_url is set."""
    expected_otp = OTPValue(value="123456")

    mock_db = AsyncMock()
    mock_db.get_valid_org_auth_token.return_value = None
    mock_db.update_task_2fa_state = AsyncMock()

    mock_app = MagicMock()
    mock_app.DATABASE = mock_db

    with (
        patch("skyvern.services.otp_service.app", new=mock_app),
        patch(
            "skyvern.services.otp_service._get_otp_value_by_run",
            new_callable=AsyncMock,
            return_value=expected_otp,
        ) as mock_get_otp_by_run,
        patch("skyvern.services.otp_service.asyncio.sleep", new_callable=AsyncMock),
    ):
        result = await poll_otp_value(
            organization_id="org_1",
            task_id="task_1",
        )

    assert result == expected_otp
    mock_get_otp_by_run.assert_awaited_once_with(
        "org_1",
        task_id="task_1",
        workflow_run_id=None,
    )
    mock_db.get_valid_org_auth_token.assert_not_awaited()


@pytest.mark.asyncio
async def test_poll_otp_value_with_totp_url_still_checks_org_token() -> None:
    """poll_otp_value should continue checking org token when totp_verification_url is configured."""
    mock_db = AsyncMock()
    mock_db.get_valid_org_auth_token.return_value = None

    mock_app = MagicMock()
    mock_app.DATABASE = mock_db

    with (
        patch("skyvern.services.otp_service.app", new=mock_app),
        patch("skyvern.services.otp_service._get_otp_value_from_url", new_callable=AsyncMock) as mock_from_url,
    ):
        result = await poll_otp_value(
            organization_id="org_1",
            task_id="task_1",
            totp_verification_url="https://otp.example.com",
        )

    assert result is None
    mock_db.get_valid_org_auth_token.assert_awaited_once_with(
        "org_1",
        OrganizationAuthTokenType.api.value,
    )
    mock_from_url.assert_not_awaited()


@pytest.mark.asyncio
async def test_set_waiting_state_updates_task_and_publishes_required_event() -> None:
    """_set_waiting_state should write task waiting state and publish required event to org channel."""
    started_at = datetime(2026, 1, 2, 3, 4, 5)

    mock_db = AsyncMock()
    mock_db.update_task_2fa_state = AsyncMock()

    mock_app = MagicMock()
    mock_app.DATABASE = mock_db

    registry = LocalNotificationRegistry()
    queue = registry.subscribe("org_1")

    ctx = otp_service.OTPPollContext(
        organization_id="org_1",
        task_id="tsk_1",
    )

    with (
        patch("skyvern.services.otp_service.app", new=mock_app),
        patch(
            "skyvern.forge.sdk.notification.factory.NotificationRegistryFactory._NotificationRegistryFactory__registry",
            new=registry,
        ),
    ):
        await otp_service._set_waiting_state(ctx, started_at)

    mock_db.update_task_2fa_state.assert_awaited_once()
    update_kwargs = mock_db.update_task_2fa_state.await_args.kwargs
    assert update_kwargs["organization_id"] == "org_1"
    assert update_kwargs["task_id"] == "tsk_1"
    assert update_kwargs["waiting_for_verification_code"] is True
    assert update_kwargs["verification_code_polling_started_at"] == started_at

    message = queue.get_nowait()
    assert message["type"] == "verification_code_required"
    assert message["task_id"] == "tsk_1"
    assert queue.empty()


@pytest.mark.asyncio
async def test_clear_waiting_state_updates_task_and_publishes_resolved_event() -> None:
    """_clear_waiting_state should clear task waiting state and publish resolved event to org channel."""
    mock_db = AsyncMock()
    mock_db.update_task_2fa_state = AsyncMock()

    mock_app = MagicMock()
    mock_app.DATABASE = mock_db

    registry = LocalNotificationRegistry()
    queue = registry.subscribe("org_1")

    ctx = otp_service.OTPPollContext(
        organization_id="org_1",
        task_id="tsk_1",
    )

    with (
        patch("skyvern.services.otp_service.app", new=mock_app),
        patch(
            "skyvern.forge.sdk.notification.factory.NotificationRegistryFactory._NotificationRegistryFactory__registry",
            new=registry,
        ),
    ):
        await otp_service._clear_waiting_state(ctx)

    mock_db.update_task_2fa_state.assert_awaited_once()
    update_kwargs = mock_db.update_task_2fa_state.await_args.kwargs
    assert update_kwargs["organization_id"] == "org_1"
    assert update_kwargs["task_id"] == "tsk_1"
    assert update_kwargs["waiting_for_verification_code"] is False

    message = queue.get_nowait()
    assert message["type"] == "verification_code_resolved"
    assert message["task_id"] == "tsk_1"
    assert queue.empty()
