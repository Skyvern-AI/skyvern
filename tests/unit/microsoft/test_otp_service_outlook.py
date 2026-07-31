from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import structlog.testing

from skyvern.forge import agent_functions
from skyvern.forge.agent_functions import AgentFunction
from skyvern.forge.sdk.schemas.totp_codes import OTPType
from skyvern.services import otp_email, otp_service
from skyvern.services.email import outlook


def _agent(monkeypatch: pytest.MonkeyPatch, credentials: list[SimpleNamespace]) -> AgentFunction:
    monkeypatch.setattr(otp_email.google_oauth_service, "get_credentials_for_org", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        otp_email.microsoft_oauth_service,
        "get_credentials_for_org",
        AsyncMock(return_value=credentials),
    )
    return AgentFunction()


@pytest.mark.asyncio
async def test_outlook_source_uses_mail_read_credential_and_persists_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = _agent(
        monkeypatch,
        [
            SimpleNamespace(id="without_mail", scopes_granted=["User.Read"]),
            SimpleNamespace(id="with_mail", scopes_granted=["User.Read", "Mail.Read"]),
        ],
    )
    mint = AsyncMock(return_value="AT")
    search = AsyncMock(
        return_value=[
            outlook.OutlookMessageCandidate(
                "message",
                "Your verification code is 123456",
                datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc),
            )
        ]
    )
    monkeypatch.setattr(agent, "get_microsoft_credentials", mint)
    monkeypatch.setattr(otp_email.outlook, "search_recent_otp_messages", search)
    parsed = otp_service.OTPValue(value="123456", type=OTPType.TOTP)
    monkeypatch.setattr(otp_service, "parse_otp_login", AsyncMock(return_value=parsed))
    create = AsyncMock()
    monkeypatch.setattr(
        agent_functions.app,
        "DATABASE",
        SimpleNamespace(otp=SimpleNamespace(create_otp_code=create)),
    )
    context = otp_email.EmailOTPVerificationContext()
    context.for_source("outlook").remember_message("with_mail", "seen_message")

    result = await agent.get_otp_value_from_email(
        organization_id="org",
        totp_identifier="user@example.com",
        workflow_id="wpid",
        workflow_run_id="wr",
        context=context,
    )

    assert result == parsed
    mint.assert_awaited_once_with(
        organization_id="org",
        credential_id="with_mail",
        required_scopes=["Mail.Read"],
    )
    search_args = search.await_args
    assert search_args is not None
    assert search_args.kwargs["access_token"] == "AT"
    assert search_args.kwargs["max_results"] == agent_functions.EMAIL_OTP_MAX_RESULTS
    assert search_args.kwargs["state"] == {}
    assert search_args.kwargs["excluded_message_ids"] == {"seen_message"}
    create.assert_awaited_once()
    create_args = create.await_args
    assert create_args is not None
    assert create_args.kwargs == {"workflow_id": "wpid", "workflow_run_id": "wr", "source": "outlook"}
    tokenless = _agent(monkeypatch, [SimpleNamespace(id="without_token", scopes_granted=["Mail.Read"])])
    monkeypatch.setattr(tokenless, "get_microsoft_credentials", AsyncMock(return_value=None))
    search.reset_mock()
    assert await tokenless.get_otp_value_from_email(organization_id="org", totp_identifier="user@example.com") is None
    search.assert_not_awaited()


@pytest.mark.asyncio
async def test_outlook_source_logs_api_error_and_scans_other_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = _agent(
        monkeypatch,
        [
            SimpleNamespace(id="failing", scopes_granted=["Mail.Read"]),
            SimpleNamespace(id="healthy", scopes_granted=["Mail.Read"]),
        ],
    )

    async def mint(organization_id: str, credential_id: str, required_scopes: list[str] | None = None) -> str:
        del organization_id, required_scopes
        return credential_id

    searched: list[str] = []

    async def search(**kwargs: object) -> list[outlook.OutlookMessageCandidate]:
        token = str(kwargs["access_token"])
        searched.append(token)
        if token == "failing":
            raise outlook.OutlookAPIError(status=503, code="ServiceUnavailable", message="unavailable")
        return []

    monkeypatch.setattr(agent, "get_microsoft_credentials", mint)
    monkeypatch.setattr(otp_email.outlook, "search_recent_otp_messages", search)

    with structlog.testing.capture_logs() as logs:
        result = await agent.get_otp_value_from_email(organization_id="org", totp_identifier="user@example.com")

    assert result is None
    assert searched == ["failing", "healthy"]
    assert any(
        log.get("event") == "Email OTP lookup failed"
        and log.get("source") == "outlook"
        and log.get("credential_id") == "failing"
        and log.get("status") == 503
        and log.get("code") == "ServiceUnavailable"
        for log in logs
    )
