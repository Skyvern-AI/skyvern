from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import structlog.testing

from skyvern.forge import agent_functions
from skyvern.forge.agent_functions import AgentFunction
from skyvern.forge.sdk.schemas.totp_codes import OTPType
from skyvern.forge.sdk.services import google_gmail_service, google_oauth_service
from skyvern.services import otp_email, otp_service
from skyvern.services.otp_email import MAX_SEEN_EMAIL_MESSAGE_IDS, EmailOTPVerificationContext


def test_email_otp_source_context_caps_seen_message_ids() -> None:
    context = EmailOTPVerificationContext().for_source("gmail")

    for index in range(MAX_SEEN_EMAIL_MESSAGE_IDS + 5):
        context.remember_message("goac_1", f"msg_{index}")

    assert len(context.seen_message_keys) == MAX_SEEN_EMAIL_MESSAGE_IDS
    assert not context.has_seen_message("goac_1", "msg_0")
    assert context.has_seen_message("goac_1", f"msg_{MAX_SEEN_EMAIL_MESSAGE_IDS + 4}")


@pytest.mark.asyncio
async def test_gmail_source_filters_seen_candidates_for_current_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    source = otp_email.GmailOTPSource(AsyncMock(return_value=SimpleNamespace(token="AT")))
    candidates = [
        google_gmail_service.GmailMessageCandidate(
            message_id="seen",
            content="Verification code 111111",
            internal_date=datetime.now(timezone.utc),
        ),
        google_gmail_service.GmailMessageCandidate(
            message_id="unseen",
            content="Verification code 222222",
            internal_date=datetime.now(timezone.utc),
        ),
    ]
    search_messages = AsyncMock(return_value=candidates)
    monkeypatch.setattr(otp_email.google_gmail_service, "search_recent_otp_messages", search_messages)
    context = EmailOTPVerificationContext().for_source("gmail")
    context.remember_message("goac_1", "seen")
    context.remember_message("different_credential", "unseen")

    async with httpx.AsyncClient() as client:
        results = await source.search_recent_otp_messages(
            organization_id="org_1",
            credential_id="goac_1",
            totp_identifier="user@example.com",
            created_after=None,
            max_results=5,
            context=context,
            client=client,
        )

    assert [candidate.message_id for candidate in results] == ["unseen"]


@pytest.mark.asyncio
async def test_get_otp_value_from_email_uses_gmail_scoped_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    agent = AgentFunction()
    credential = SimpleNamespace(
        id="goac_1",
        scopes_granted=list(google_oauth_service.GOOGLE_GMAIL_SCOPES),
    )
    list_credentials = AsyncMock(return_value=[credential])
    monkeypatch.setattr(otp_email.google_oauth_service, "get_credentials_for_org", list_credentials)

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
    monkeypatch.setattr(otp_email.google_gmail_service, "search_recent_otp_messages", search_messages)
    parse = AsyncMock(return_value=otp_service.OTPValue(value="123456", type=OTPType.TOTP))
    monkeypatch.setattr(otp_service, "parse_otp_login", parse)

    result = await agent.get_otp_value_from_email(
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
    search_messages_args = search_messages.await_args
    assert search_messages_args is not None
    assert search_messages_args.kwargs["max_results"] == agent_functions.EMAIL_OTP_MAX_RESULTS
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
async def test_get_otp_value_from_email_uses_first_parseable_gmail_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = AgentFunction()
    credential = SimpleNamespace(
        id="goac_1",
        scopes_granted=list(google_oauth_service.GOOGLE_GMAIL_SCOPES),
    )
    monkeypatch.setattr(
        otp_email.google_oauth_service,
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
        otp_email.google_gmail_service,
        "search_recent_otp_messages",
        AsyncMock(return_value=candidates),
    )
    parse = AsyncMock(side_effect=[None, otp_service.OTPValue(value="654321", type=OTPType.TOTP)])
    monkeypatch.setattr(otp_service, "parse_otp_login", parse)

    result = await agent.get_otp_value_from_email(
        organization_id="org_1",
        totp_identifier="user@example.com",
        workflow_id="wpid_1",
        workflow_run_id="wr_1",
    )

    assert result == otp_service.OTPValue(value="654321", type=OTPType.TOTP)
    assert [call.args[0] for call in parse.await_args_list] == [
        "Security alert with no code",
        "Your verification code is 654321",
    ]
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
async def test_get_otp_value_from_email_retries_gmail_candidate_after_parser_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = AgentFunction()
    credential = SimpleNamespace(
        id="goac_1",
        scopes_granted=list(google_oauth_service.GOOGLE_GMAIL_SCOPES),
    )
    monkeypatch.setattr(
        otp_email.google_oauth_service,
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
    monkeypatch.setattr(otp_email.google_gmail_service, "search_recent_otp_messages", search_messages)
    parse = AsyncMock(
        side_effect=[RuntimeError("temporary parser outage"), otp_service.OTPValue(value="112233", type=OTPType.TOTP)]
    )
    monkeypatch.setattr(otp_service, "parse_otp_login", parse)

    context = EmailOTPVerificationContext()
    with structlog.testing.capture_logs() as logs:
        first_result = await agent.get_otp_value_from_email(
            organization_id="org_1",
            totp_identifier="user@example.com",
            workflow_id="wpid_1",
            workflow_run_id="wr_1",
            context=context,
        )
        context.for_source("gmail").last_searched_at_by_credential["goac_1"] = datetime(2025, 1, 1, tzinfo=timezone.utc)
        second_result = await agent.get_otp_value_from_email(
            organization_id="org_1",
            totp_identifier="user@example.com",
            workflow_id="wpid_1",
            workflow_run_id="wr_1",
            context=context,
        )

    assert first_result is None
    assert second_result == otp_service.OTPValue(value="112233", type=OTPType.TOTP)
    assert [call.args[0] for call in parse.await_args_list] == [candidate.content, candidate.content]
    create_otp_code.assert_awaited_once()
    assert any(
        record.get("event") == "Failed to parse email OTP candidate" and record.get("source") == "gmail"
        for record in logs
    )


@pytest.mark.asyncio
async def test_get_otp_value_from_email_rechecks_unseen_gmail_candidate_after_credit_skip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = AgentFunction()
    credential = SimpleNamespace(
        id="goac_1",
        scopes_granted=list(google_oauth_service.GOOGLE_GMAIL_SCOPES),
    )
    monkeypatch.setattr(
        otp_email.google_oauth_service,
        "get_credentials_for_org",
        AsyncMock(return_value=[credential]),
    )
    monkeypatch.setattr(
        agent,
        "get_google_workspace_credentials",
        AsyncMock(return_value=SimpleNamespace(token=object())),
    )
    create_otp_code = AsyncMock()
    monkeypatch.setattr(
        agent_functions.app, "DATABASE", SimpleNamespace(otp=SimpleNamespace(create_otp_code=create_otp_code))
    )
    candidates = [
        google_gmail_service.GmailMessageCandidate(
            message_id=f"msg_credit_skipped_{index}",
            content=f"Long relayed authentication message {index}.",
            internal_date=datetime.now(timezone.utc),
        )
        for index in range(2)
    ]
    monkeypatch.setattr(
        otp_email.google_gmail_service,
        "search_recent_otp_messages",
        AsyncMock(return_value=candidates),
    )
    parsed = otp_service.OTPValue(value="placeholder", type=OTPType.TOTP)
    parse = AsyncMock(side_effect=[otp_service.InsufficientCreditsForOTPParse, parsed])
    monkeypatch.setattr(otp_service, "parse_otp_login", parse)
    context = EmailOTPVerificationContext()

    with structlog.testing.capture_logs() as logs:
        first_result = await agent.get_otp_value_from_email(
            organization_id="org_1",
            totp_identifier="relay@example.test",
            context=context,
        )
        context.for_source("gmail").last_searched_at_by_credential["goac_1"] = datetime(2025, 1, 1, tzinfo=timezone.utc)
        second_result = await agent.get_otp_value_from_email(
            organization_id="org_1",
            totp_identifier="relay@example.test",
            context=context,
        )

    assert first_result is None
    assert second_result == parsed
    assert parse.await_args_list[0].args[0] == candidates[0].content
    assert parse.await_args_list[1].args[0] == candidates[1].content
    create_otp_code.assert_awaited_once()
    gmail_context = context.for_source("gmail")
    assert gmail_context.has_seen_message("goac_1", candidates[0].message_id)
    assert gmail_context.has_seen_message("goac_1", candidates[1].message_id)
    assert all(
        record.get("event") != "Failed to parse email OTP candidate" or record.get("source") != "gmail"
        for record in logs
    )


@pytest.mark.asyncio
async def test_get_otp_value_from_email_skips_credentials_without_gmail_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = AgentFunction()
    credential = SimpleNamespace(
        id="goac_1",
        scopes_granted=list(google_oauth_service.GOOGLE_SHEETS_SCOPES),
    )
    monkeypatch.setattr(
        otp_email.google_oauth_service,
        "get_credentials_for_org",
        AsyncMock(return_value=[credential]),
    )
    get_credentials = AsyncMock(return_value=SimpleNamespace(token="AT"))
    monkeypatch.setattr(agent, "get_google_workspace_credentials", get_credentials)

    result = await agent.get_otp_value_from_email(
        organization_id="org_1",
        totp_identifier="user@example.com",
    )

    assert result is None
    get_credentials.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_otp_value_from_email_throttles_gmail_searches_within_polling_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = AgentFunction()
    credential = SimpleNamespace(
        id="goac_1",
        scopes_granted=list(google_oauth_service.GOOGLE_GMAIL_SCOPES),
    )
    list_credentials = AsyncMock(return_value=[credential])
    monkeypatch.setattr(otp_email.google_oauth_service, "get_credentials_for_org", list_credentials)
    get_credentials = AsyncMock(return_value=SimpleNamespace(token="AT"))
    monkeypatch.setattr(agent, "get_google_workspace_credentials", get_credentials)
    search_messages = AsyncMock(return_value=[])
    monkeypatch.setattr(otp_email.google_gmail_service, "search_recent_otp_messages", search_messages)
    monkeypatch.setattr(
        agent_functions,
        "build_email_otp_sources",
        lambda current_agent: [otp_email.GmailOTPSource(current_agent.get_google_workspace_credentials)],
    )

    context = EmailOTPVerificationContext()

    with structlog.testing.capture_logs() as logs:
        for _ in range(2):
            result = await agent.get_otp_value_from_email(
                organization_id="org_1",
                totp_identifier="user@example.com",
                context=context,
            )
            assert result is None

    list_credentials.assert_awaited_once_with("org_1")
    get_credentials.assert_awaited_once()
    search_messages.assert_awaited_once()
    assert all(log.get("event") != "Unexpected email OTP lookup failure" for log in logs)
