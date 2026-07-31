from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import structlog.testing

from skyvern.forge import agent_functions
from skyvern.forge.agent_functions import AgentFunction
from skyvern.forge.sdk.schemas.totp_codes import OTPType
from skyvern.services import otp_service
from skyvern.services.otp_email import EmailOTPCandidate, EmailOTPVerificationContext


class StubSource:
    def __init__(
        self,
        name: str,
        credentials: list[str] | None = None,
        candidates: list[EmailOTPCandidate] | None = None,
        events: list[str] | None = None,
        list_error: Exception | None = None,
    ) -> None:
        self.name, self.credentials, self.candidates = name, credentials or [], candidates or []
        self.events, self.list_error = events if events is not None else [], list_error

    async def list_credential_ids(self, organization_id: str) -> list[str]:
        self.events.append(f"list:{self.name}")
        if self.list_error:
            raise self.list_error
        return self.credentials

    async def search_recent_otp_messages(
        self,
        *,
        credential_id: str,
        **_kwargs: object,
    ) -> list[EmailOTPCandidate]:
        self.events.append(f"search:{self.name}:{credential_id}")
        return self.candidates


def _agent(monkeypatch: pytest.MonkeyPatch, sources: list[StubSource]) -> tuple[AgentFunction, AsyncMock]:
    monkeypatch.setattr(agent_functions, "build_email_otp_sources", lambda _agent: sources)
    create = AsyncMock()
    monkeypatch.setattr(
        agent_functions.app,
        "DATABASE",
        SimpleNamespace(otp=SimpleNamespace(create_otp_code=create)),
    )
    return AgentFunction(), create


@pytest.mark.asyncio
async def test_registry_scans_gmail_first_short_circuits_and_persists_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        agent_functions,
        "build_email_otp_sources",
        lambda _agent: (_ for _ in ()).throw(AssertionError("sources must not be built")),
    )
    assert await AgentFunction().get_otp_value_from_email(organization_id="org", totp_identifier="+15555550123") is None
    events: list[str] = []
    gmail = StubSource("gmail", ["g1"], [EmailOTPCandidate("gm", "gmail code")], events)
    outlook = StubSource("outlook", ["o1"], [EmailOTPCandidate("om", "outlook code")], events)
    agent, create = _agent(monkeypatch, [gmail, outlook])
    expected = otp_service.OTPValue(value="123456", type=OTPType.TOTP)
    monkeypatch.setattr(otp_service, "parse_otp_login", AsyncMock(return_value=expected))

    result = await agent.get_otp_value_from_email(
        organization_id="org",
        totp_identifier="user@example.com",
        workflow_id="wpid",
        workflow_run_id="wr",
    )

    assert result == expected
    assert events == ["list:gmail", "search:gmail:g1"]
    assert create.await_args.kwargs == {"workflow_id": "wpid", "workflow_run_id": "wr", "source": "gmail"}


@pytest.mark.asyncio
async def test_registry_isolates_empty_and_failing_sources(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []
    sources = [
        StubSource("gmail", events=events, list_error=RuntimeError("down")),
        StubSource("outlook", events=events),
        StubSource("backup", ["b1"], [EmailOTPCandidate("bm", "backup code")], events),
    ]
    agent, _ = _agent(monkeypatch, sources)
    monkeypatch.setattr(
        otp_service,
        "parse_otp_login",
        AsyncMock(return_value=otp_service.OTPValue(value="654321", type=OTPType.TOTP)),
    )

    with structlog.testing.capture_logs() as logs:
        result = await agent.get_otp_value_from_email(organization_id="org", totp_identifier="user@example.com")

    assert result is not None
    assert events == ["list:gmail", "list:outlook", "list:backup", "search:backup:b1"]
    assert any(
        log.get("event") == "Failed to list email OTP credentials" and log.get("source") == "gmail" for log in logs
    )


@pytest.mark.asyncio
async def test_registry_credit_skip_remembers_candidate_and_stops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    candidate = EmailOTPCandidate("gm", "gmail code")
    agent, create = _agent(
        monkeypatch,
        [
            StubSource("gmail", ["g1"], [candidate], events),
            StubSource("outlook", ["o1"], [EmailOTPCandidate("om", "outlook code")], events),
        ],
    )
    monkeypatch.setattr(
        otp_service,
        "parse_otp_login",
        AsyncMock(side_effect=otp_service.InsufficientCreditsForOTPParse),
    )
    context = EmailOTPVerificationContext()

    result = await agent.get_otp_value_from_email(
        organization_id="org", totp_identifier="user@example.com", context=context
    )

    assert result is None
    assert events == ["list:gmail", "search:gmail:g1"]
    assert context.for_source("gmail").has_seen_message("g1", "gm")
    create.assert_not_awaited()


def test_registry_seen_keys_do_not_collide_across_sources_or_credentials() -> None:
    shared_id = "shared"
    context = EmailOTPVerificationContext()

    context.for_source("gmail").remember_message("g1", shared_id)
    assert context.for_source("gmail").has_seen_message("g1", shared_id)
    assert not context.for_source("gmail").has_seen_message("o1", shared_id)
    assert not context.for_source("outlook").has_seen_message("g1", shared_id)
    assert context.for_source("gmail").seen_message_ids_for_credential("g1") == {shared_id}
    assert context.for_source("gmail").seen_message_ids_for_credential("o1") == set()
