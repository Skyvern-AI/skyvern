from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from skyvern.forge import agent_functions
from skyvern.forge.agent_functions import AgentFunction
from skyvern.forge.sdk.schemas.totp_codes import OTPType
from skyvern.forge.sdk.services import google_gmail_service, google_oauth_service
from skyvern.services import otp_service
from skyvern.services.otp_gmail import MAX_SEEN_GMAIL_MESSAGE_IDS, GmailOTPVerificationContext


def test_gmail_otp_context_caps_seen_message_ids() -> None:
    context = GmailOTPVerificationContext()

    for index in range(MAX_SEEN_GMAIL_MESSAGE_IDS + 5):
        context.remember_message_id(f"msg_{index}")

    assert len(context.seen_message_ids) == MAX_SEEN_GMAIL_MESSAGE_IDS
    assert not context.has_seen_message_id("msg_0")
    assert context.has_seen_message_id(f"msg_{MAX_SEEN_GMAIL_MESSAGE_IDS + 4}")


@pytest.mark.asyncio
async def test_get_otp_value_from_gmail_uses_gmail_scoped_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    agent = AgentFunction()
    credential = SimpleNamespace(
        id="goac_1",
        scopes_granted=list(google_oauth_service.GOOGLE_GMAIL_SCOPES),
    )
    list_credentials = AsyncMock(return_value=[credential])
    monkeypatch.setattr(agent_functions.google_oauth_service, "get_credentials_for_org", list_credentials)

    get_credentials = AsyncMock(return_value=SimpleNamespace(token="AT"))
    monkeypatch.setattr(agent, "get_google_workspace_credentials", get_credentials)
    create_otp_code = AsyncMock()
    monkeypatch.setattr(
        agent_functions.app, "DATABASE", SimpleNamespace(otp=SimpleNamespace(create_otp_code=create_otp_code))
    )

    candidate = google_gmail_service.GmailMessageCandidate(
        message_id="msg_1",
        content="Your verification code is 123456",
        internal_date=datetime.now(timezone.utc),
    )
    search_messages = AsyncMock(return_value=[candidate])
    monkeypatch.setattr(agent_functions.google_gmail_service, "search_recent_otp_messages", search_messages)
    parse = AsyncMock(return_value=otp_service.OTPValue(value="123456", type=OTPType.TOTP))
    monkeypatch.setattr(otp_service, "parse_otp_login", parse)

    result = await agent.get_otp_value_from_gmail(
        organization_id="org_1",
        totp_identifier="user@example.com",
        workflow_id="wpid_1",
        workflow_run_id="wr_1",
        created_after=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )

    assert result == otp_service.OTPValue(value="123456", type=OTPType.TOTP)
    get_credentials.assert_awaited_once_with(
        organization_id="org_1",
        credential_id="goac_1",
        required_scopes=list(google_oauth_service.GOOGLE_GMAIL_SCOPES),
    )
    search_messages.assert_awaited_once()
    search_messages_args = search_messages.await_args
    assert search_messages_args is not None
    assert search_messages_args.kwargs["max_results"] == agent_functions.GMAIL_OTP_MAX_RESULTS
    assert search_messages_args.kwargs["client"] is not None
    parse.assert_awaited_once_with("Your verification code is 123456", "org_1")
    create_otp_code.assert_awaited_once_with(
        "org_1",
        "user@example.com",
        "123456",
        "123456",
        OTPType.TOTP,
        workflow_id="wpid_1",
        workflow_run_id="wr_1",
        source="gmail",
    )


@pytest.mark.asyncio
async def test_get_otp_value_from_gmail_uses_first_parseable_candidate(monkeypatch: pytest.MonkeyPatch) -> None:
    agent = AgentFunction()
    credential = SimpleNamespace(
        id="goac_1",
        scopes_granted=list(google_oauth_service.GOOGLE_GMAIL_SCOPES),
    )
    monkeypatch.setattr(
        agent_functions.google_oauth_service,
        "get_credentials_for_org",
        AsyncMock(return_value=[credential]),
    )
    monkeypatch.setattr(agent, "get_google_workspace_credentials", AsyncMock(return_value=SimpleNamespace(token="AT")))
    create_otp_code = AsyncMock()
    monkeypatch.setattr(
        agent_functions.app, "DATABASE", SimpleNamespace(otp=SimpleNamespace(create_otp_code=create_otp_code))
    )

    candidates = [
        google_gmail_service.GmailMessageCandidate(
            message_id="msg_unrelated",
            content="Security alert with no code",
            internal_date=datetime.now(timezone.utc),
        ),
        google_gmail_service.GmailMessageCandidate(
            message_id="msg_code",
            content="Your verification code is 654321",
            internal_date=datetime.now(timezone.utc),
        ),
    ]
    monkeypatch.setattr(
        agent_functions.google_gmail_service,
        "search_recent_otp_messages",
        AsyncMock(return_value=candidates),
    )
    parse = AsyncMock(side_effect=[None, otp_service.OTPValue(value="654321", type=OTPType.TOTP)])
    monkeypatch.setattr(otp_service, "parse_otp_login", parse)

    result = await agent.get_otp_value_from_gmail(
        organization_id="org_1",
        totp_identifier="user@example.com",
        workflow_id="wpid_1",
        workflow_run_id="wr_1",
    )

    assert result == otp_service.OTPValue(value="654321", type=OTPType.TOTP)
    assert parse.await_count == 2
    create_otp_code.assert_awaited_once_with(
        "org_1",
        "user@example.com",
        "654321",
        "654321",
        OTPType.TOTP,
        workflow_id="wpid_1",
        workflow_run_id="wr_1",
        source="gmail",
    )


@pytest.mark.asyncio
async def test_get_otp_value_from_gmail_retries_candidate_after_parser_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = AgentFunction()
    credential = SimpleNamespace(
        id="goac_1",
        scopes_granted=list(google_oauth_service.GOOGLE_GMAIL_SCOPES),
    )
    monkeypatch.setattr(
        agent_functions.google_oauth_service,
        "get_credentials_for_org",
        AsyncMock(return_value=[credential]),
    )
    monkeypatch.setattr(agent, "get_google_workspace_credentials", AsyncMock(return_value=SimpleNamespace(token="AT")))
    create_otp_code = AsyncMock()
    monkeypatch.setattr(
        agent_functions.app, "DATABASE", SimpleNamespace(otp=SimpleNamespace(create_otp_code=create_otp_code))
    )

    candidate = google_gmail_service.GmailMessageCandidate(
        message_id="msg_retry",
        content="Your verification code is 112233",
        internal_date=datetime.now(timezone.utc),
    )
    search_messages = AsyncMock(return_value=[candidate])
    monkeypatch.setattr(agent_functions.google_gmail_service, "search_recent_otp_messages", search_messages)
    parse = AsyncMock(
        side_effect=[RuntimeError("temporary parser outage"), otp_service.OTPValue(value="112233", type=OTPType.TOTP)]
    )
    monkeypatch.setattr(otp_service, "parse_otp_login", parse)

    context = GmailOTPVerificationContext()
    first_result = await agent.get_otp_value_from_gmail(
        organization_id="org_1",
        totp_identifier="user@example.com",
        workflow_id="wpid_1",
        workflow_run_id="wr_1",
        context=context,
    )
    context.last_searched_at_by_credential["goac_1"] = datetime(2025, 1, 1, tzinfo=timezone.utc)
    second_result = await agent.get_otp_value_from_gmail(
        organization_id="org_1",
        totp_identifier="user@example.com",
        workflow_id="wpid_1",
        workflow_run_id="wr_1",
        context=context,
    )

    assert first_result is None
    assert second_result == otp_service.OTPValue(value="112233", type=OTPType.TOTP)
    assert parse.await_count == 2
    create_otp_code.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_otp_value_from_gmail_skips_credentials_without_gmail_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = AgentFunction()
    credential = SimpleNamespace(
        id="goac_1",
        scopes_granted=list(google_oauth_service.GOOGLE_SHEETS_SCOPES),
    )
    monkeypatch.setattr(
        agent_functions.google_oauth_service,
        "get_credentials_for_org",
        AsyncMock(return_value=[credential]),
    )
    get_credentials = AsyncMock(return_value=SimpleNamespace(token="AT"))
    monkeypatch.setattr(agent, "get_google_workspace_credentials", get_credentials)

    result = await agent.get_otp_value_from_gmail(
        organization_id="org_1",
        totp_identifier="user@example.com",
    )

    assert result is None
    get_credentials.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_otp_value_from_gmail_throttles_credential_searches_within_polling_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = AgentFunction()
    credential = SimpleNamespace(
        id="goac_1",
        scopes_granted=list(google_oauth_service.GOOGLE_GMAIL_SCOPES),
    )
    list_credentials = AsyncMock(return_value=[credential])
    monkeypatch.setattr(agent_functions.google_oauth_service, "get_credentials_for_org", list_credentials)
    get_credentials = AsyncMock(return_value=SimpleNamespace(token="AT"))
    monkeypatch.setattr(agent, "get_google_workspace_credentials", get_credentials)
    search_messages = AsyncMock(return_value=[])
    monkeypatch.setattr(agent_functions.google_gmail_service, "search_recent_otp_messages", search_messages)

    context = GmailOTPVerificationContext()

    for _ in range(2):
        result = await agent.get_otp_value_from_gmail(
            organization_id="org_1",
            totp_identifier="user@example.com",
            context=context,
        )
        assert result is None

    list_credentials.assert_awaited_once_with("org_1")
    get_credentials.assert_awaited_once()
    search_messages.assert_awaited_once()
