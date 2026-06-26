"""Tests for skyvern.services.otp_service.resolve_otp_value.

Verifies the unified OTP source priority used by both the normal Agent flow
and the CUA flow: navigation payload -> credential-backed TOTP -> webhook
polling. Polling exceptions surface unchanged so callers can react.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyvern.exceptions import FailedToGetTOTPVerificationCode, NoTOTPVerificationCodeFound
from skyvern.forge.sdk.schemas.totp_codes import OTPType
from skyvern.services.otp_service import OTPValue, resolve_otp_value


def _make_task(
    *,
    task_id: str = "tsk_test",
    workflow_run_id: str | None = "wr_test",
    organization_id: str | None = "o_test",
    totp_verification_url: str | None = None,
    totp_identifier: str | None = None,
    navigation_payload: object = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        task_id=task_id,
        workflow_run_id=workflow_run_id,
        organization_id=organization_id,
        totp_verification_url=totp_verification_url,
        totp_identifier=totp_identifier,
        navigation_payload=navigation_payload,
    )


def _otp_value(code: str = "123456") -> OTPValue:
    return OTPValue(value=code, type=OTPType.TOTP)


@pytest.mark.asyncio
async def test_payload_otp_returns_immediately_skipping_credential_and_poll() -> None:
    payload_value = _otp_value("999000")
    task = _make_task(
        navigation_payload={"otp_code": "999000"},
        totp_verification_url="https://example.com/webhook",
    )
    with (
        patch(
            "skyvern.services.otp_service.extract_totp_from_navigation_inputs", return_value=payload_value
        ) as payload,
        patch("skyvern.services.otp_service.try_generate_totp_from_credential") as credential,
        patch("skyvern.services.otp_service.poll_otp_value", new=AsyncMock()) as poll,
    ):
        result = await resolve_otp_value(task)

    assert result is payload_value
    payload.assert_called_once_with(task.navigation_payload)
    credential.assert_not_called()
    poll.assert_not_called()


@pytest.mark.asyncio
async def test_credential_returns_value_skipping_poll_even_when_url_configured() -> None:
    """Key SKY-9178 fix: credential TOTP wins over webhook polling when both are configured."""
    credential_value = _otp_value("424242")
    task = _make_task(
        totp_verification_url="https://example.com/webhook",
        totp_identifier="user@example.com",
    )
    with (
        patch("skyvern.services.otp_service.extract_totp_from_navigation_inputs", return_value=None),
        patch(
            "skyvern.services.otp_service.try_generate_totp_from_credential",
            return_value=credential_value,
        ) as credential,
        patch("skyvern.services.otp_service.poll_otp_value", new=AsyncMock()) as poll,
    ):
        result = await resolve_otp_value(task)

    assert result is credential_value
    credential.assert_called_once_with(task.workflow_run_id)
    poll.assert_not_called()


def _stub_workflow_run_lookup(
    monkeypatch_target,
    *,
    workflow_id: str | None,
    workflow_permanent_id: str | None,
    started_at: datetime | None = None,
) -> MagicMock:
    """Patch app.DATABASE.workflow_runs.get_workflow_run with a recording mock."""
    workflow_run = SimpleNamespace(
        workflow_id=workflow_id, workflow_permanent_id=workflow_permanent_id, started_at=started_at
    )
    get = AsyncMock(return_value=workflow_run)
    monkeypatch_target.setattr(
        "skyvern.services.otp_service.app.DATABASE.workflow_runs.get_workflow_run",
        get,
    )
    return get


@pytest.mark.asyncio
async def test_falls_through_to_poll_when_no_payload_or_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    poll_value = _otp_value("303030")
    task = _make_task(
        totp_verification_url="https://example.com/webhook",
        totp_identifier="user@example.com",
    )
    db_get = _stub_workflow_run_lookup(monkeypatch, workflow_id="w_test", workflow_permanent_id="wpid_test")
    with (
        patch("skyvern.services.otp_service.extract_totp_from_navigation_inputs", return_value=None),
        patch("skyvern.services.otp_service.try_generate_totp_from_credential", return_value=None),
        patch("skyvern.services.otp_service.poll_otp_value", new=AsyncMock(return_value=poll_value)) as poll,
    ):
        result = await resolve_otp_value(task)

    assert result is poll_value
    db_get.assert_awaited_once_with("wr_test")
    poll.assert_awaited_once()
    kwargs = poll.await_args.kwargs
    assert kwargs["organization_id"] == "o_test"
    assert kwargs["task_id"] == "tsk_test"
    assert kwargs["workflow_id"] == "w_test"
    assert kwargs["workflow_run_id"] == "wr_test"
    assert kwargs["workflow_permanent_id"] == "wpid_test"
    assert kwargs["totp_verification_url"] == "https://example.com/webhook"
    assert kwargs["totp_identifier"] == "user@example.com"


@pytest.mark.asyncio
async def test_forwards_run_started_at_as_created_after(monkeypatch: pytest.MonkeyPatch) -> None:
    """The run's started_at is forwarded as created_after so prior-run codes are disqualified."""
    poll_value = _otp_value("303030")
    started_at = datetime(2026, 6, 8, 20, 3, 0)
    task = _make_task(totp_identifier="otp@example.com")
    _stub_workflow_run_lookup(
        monkeypatch, workflow_id="w_test", workflow_permanent_id="wpid_test", started_at=started_at
    )
    with (
        patch("skyvern.services.otp_service.extract_totp_from_navigation_inputs", return_value=None),
        patch("skyvern.services.otp_service.try_generate_totp_from_credential", return_value=None),
        patch("skyvern.services.otp_service.poll_otp_value", new=AsyncMock(return_value=poll_value)) as poll,
    ):
        result = await resolve_otp_value(task)

    assert result is poll_value
    assert poll.await_args.kwargs["created_after"] == started_at


@pytest.mark.asyncio
async def test_created_after_is_none_when_run_started_at_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Null-fallback: a run with no started_at must not gain a watermark filter."""
    poll_value = _otp_value("404040")
    task = _make_task(totp_identifier="user@example.com")
    _stub_workflow_run_lookup(monkeypatch, workflow_id="w_test", workflow_permanent_id="wpid_test", started_at=None)
    with (
        patch("skyvern.services.otp_service.extract_totp_from_navigation_inputs", return_value=None),
        patch("skyvern.services.otp_service.try_generate_totp_from_credential", return_value=None),
        patch("skyvern.services.otp_service.poll_otp_value", new=AsyncMock(return_value=poll_value)) as poll,
    ):
        result = await resolve_otp_value(task)

    assert result is poll_value
    assert poll.await_args.kwargs["created_after"] is None


@pytest.mark.asyncio
async def test_workflow_run_lookup_skipped_when_payload_resolves(monkeypatch: pytest.MonkeyPatch) -> None:
    """Codex must-fix: payload-resolved OTP must not pay a DB roundtrip."""
    payload_value = _otp_value("111111")
    task = _make_task(
        navigation_payload={"otp_code": "111111"},
        totp_verification_url="https://example.com/webhook",
    )
    db_get = _stub_workflow_run_lookup(monkeypatch, workflow_id="w_test", workflow_permanent_id="wpid_test")
    with (
        patch("skyvern.services.otp_service.extract_totp_from_navigation_inputs", return_value=payload_value),
        patch("skyvern.services.otp_service.try_generate_totp_from_credential") as credential,
        patch("skyvern.services.otp_service.poll_otp_value", new=AsyncMock()) as poll,
    ):
        result = await resolve_otp_value(task)

    assert result is payload_value
    db_get.assert_not_awaited()
    credential.assert_not_called()
    poll.assert_not_called()


@pytest.mark.asyncio
async def test_workflow_run_lookup_skipped_when_credential_resolves(monkeypatch: pytest.MonkeyPatch) -> None:
    """Codex must-fix: credential-resolved OTP must not pay a DB roundtrip."""
    credential_value = _otp_value("222222")
    task = _make_task(
        totp_verification_url="https://example.com/webhook",
        totp_identifier="user@example.com",
    )
    db_get = _stub_workflow_run_lookup(monkeypatch, workflow_id="w_test", workflow_permanent_id="wpid_test")
    with (
        patch("skyvern.services.otp_service.extract_totp_from_navigation_inputs", return_value=None),
        patch("skyvern.services.otp_service.try_generate_totp_from_credential", return_value=credential_value),
        patch("skyvern.services.otp_service.poll_otp_value", new=AsyncMock()) as poll,
    ):
        result = await resolve_otp_value(task)

    assert result is credential_value
    db_get.assert_not_awaited()
    poll.assert_not_called()


@pytest.mark.asyncio
async def test_polling_handles_missing_workflow_run_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    """If DB returns None for the workflow_run, polling still proceeds with None metadata."""
    poll_value = _otp_value("999")
    task = _make_task(totp_verification_url="https://example.com/webhook")
    get = AsyncMock(return_value=None)
    monkeypatch.setattr("skyvern.services.otp_service.app.DATABASE.workflow_runs.get_workflow_run", get)
    with (
        patch("skyvern.services.otp_service.extract_totp_from_navigation_inputs", return_value=None),
        patch("skyvern.services.otp_service.try_generate_totp_from_credential", return_value=None),
        patch("skyvern.services.otp_service.poll_otp_value", new=AsyncMock(return_value=poll_value)) as poll,
    ):
        result = await resolve_otp_value(task)

    assert result is poll_value
    kwargs = poll.await_args.kwargs
    assert kwargs["workflow_id"] is None
    assert kwargs["workflow_permanent_id"] is None


@pytest.mark.asyncio
async def test_returns_none_when_no_source_configured_at_all() -> None:
    task = _make_task()
    with (
        patch("skyvern.services.otp_service.extract_totp_from_navigation_inputs", return_value=None),
        patch("skyvern.services.otp_service.try_generate_totp_from_credential", return_value=None),
        patch("skyvern.services.otp_service.poll_otp_value", new=AsyncMock()) as poll,
    ):
        result = await resolve_otp_value(task)

    assert result is None
    poll.assert_not_called()


@pytest.mark.asyncio
async def test_polling_skipped_when_organization_id_missing() -> None:
    task = _make_task(organization_id=None, totp_verification_url="https://example.com/webhook")
    with (
        patch("skyvern.services.otp_service.extract_totp_from_navigation_inputs", return_value=None),
        patch("skyvern.services.otp_service.try_generate_totp_from_credential", return_value=None),
        patch("skyvern.services.otp_service.poll_otp_value", new=AsyncMock()) as poll,
    ):
        result = await resolve_otp_value(task)

    assert result is None
    poll.assert_not_called()


@pytest.mark.asyncio
async def test_no_totp_found_exception_propagates_from_poll(monkeypatch: pytest.MonkeyPatch) -> None:
    task = _make_task(totp_verification_url="https://example.com/webhook")
    _stub_workflow_run_lookup(monkeypatch, workflow_id=None, workflow_permanent_id=None)
    with (
        patch("skyvern.services.otp_service.extract_totp_from_navigation_inputs", return_value=None),
        patch("skyvern.services.otp_service.try_generate_totp_from_credential", return_value=None),
        patch(
            "skyvern.services.otp_service.poll_otp_value",
            new=AsyncMock(side_effect=NoTOTPVerificationCodeFound(task_id=task.task_id)),
        ),
    ):
        with pytest.raises(NoTOTPVerificationCodeFound):
            await resolve_otp_value(task)


@pytest.mark.asyncio
async def test_failed_to_get_totp_exception_propagates_from_poll(monkeypatch: pytest.MonkeyPatch) -> None:
    task = _make_task(totp_verification_url="https://example.com/webhook")
    _stub_workflow_run_lookup(monkeypatch, workflow_id=None, workflow_permanent_id=None)
    with (
        patch("skyvern.services.otp_service.extract_totp_from_navigation_inputs", return_value=None),
        patch("skyvern.services.otp_service.try_generate_totp_from_credential", return_value=None),
        patch(
            "skyvern.services.otp_service.poll_otp_value",
            new=AsyncMock(side_effect=FailedToGetTOTPVerificationCode(reason="bad body")),
        ),
    ):
        with pytest.raises(FailedToGetTOTPVerificationCode):
            await resolve_otp_value(task)


@pytest.mark.asyncio
async def test_identifier_only_routes_through_poll(monkeypatch: pytest.MonkeyPatch) -> None:
    poll_value = _otp_value("777")
    task = _make_task(totp_identifier="user@example.com")
    _stub_workflow_run_lookup(monkeypatch, workflow_id=None, workflow_permanent_id=None)
    with (
        patch("skyvern.services.otp_service.extract_totp_from_navigation_inputs", return_value=None),
        patch("skyvern.services.otp_service.try_generate_totp_from_credential", return_value=None),
        patch("skyvern.services.otp_service.poll_otp_value", new=AsyncMock(return_value=poll_value)) as poll,
    ):
        result = await resolve_otp_value(task)

    assert result is poll_value
    kwargs = poll.await_args.kwargs
    assert kwargs["totp_verification_url"] is None
    assert kwargs["totp_identifier"] == "user@example.com"


@pytest.mark.asyncio
async def test_credential_check_runs_even_when_workflow_run_id_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Helper still attempts credential lookup; DB lookup is skipped when no workflow_run_id."""
    task = _make_task(workflow_run_id=None, totp_verification_url="https://example.com/webhook")
    db_get = AsyncMock()
    monkeypatch.setattr("skyvern.services.otp_service.app.DATABASE.workflow_runs.get_workflow_run", db_get)
    with (
        patch("skyvern.services.otp_service.extract_totp_from_navigation_inputs", return_value=None),
        patch("skyvern.services.otp_service.try_generate_totp_from_credential", return_value=None) as credential,
        patch("skyvern.services.otp_service.poll_otp_value", new=AsyncMock(return_value=None)) as poll,
    ):
        result = await resolve_otp_value(task)

    assert result is None
    credential.assert_called_once_with(None)
    db_get.assert_not_awaited()
    poll.assert_awaited_once()
