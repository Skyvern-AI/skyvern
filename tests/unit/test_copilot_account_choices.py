from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock

import pytest

from skyvern.forge.sdk.copilot import agent as agent_module
from skyvern.forge.sdk.copilot import request_policy as request_policy_module
from skyvern.forge.sdk.copilot.turn_outcome import (
    connected_account_choice_context,
    selected_connected_account_id,
)
from skyvern.forge.sdk.schemas.copilot_turn_outcome import (
    ConnectedAccountChoice,
    ResponseKind,
    TurnOutcome,
)
from skyvern.forge.sdk.schemas.google_oauth import GoogleOAuthCredentialBase
from skyvern.forge.sdk.schemas.workflow_copilot import WorkflowCopilotChatMessage, WorkflowCopilotChatSender
from tests.unit.copilot_test_helpers import make_copilot_ctx


def _google(
    connection_id: str,
    name: str,
    state: str = "active",
    email_address: str | None = None,
) -> GoogleOAuthCredentialBase:
    return GoogleOAuthCredentialBase(
        id=connection_id,
        organization_id="org-1",
        credential_name=name,
        email_address=email_address,
        state=state,
        scopes_requested=["https://www.googleapis.com/auth/spreadsheets"],
        scopes_granted=["https://www.googleapis.com/auth/spreadsheets"],
        created_at=datetime(2026, 8, 15),
        modified_at=datetime(2026, 8, 15),
    )


def test_persisted_account_choice_allows_missing_display_email() -> None:
    message = WorkflowCopilotChatMessage(
        workflow_copilot_chat_message_id="message-1",
        workflow_copilot_chat_id="chat-1",
        sender=WorkflowCopilotChatSender.AI,
        content="Choose an account",
        narrative_payload={
            "turnId": "turn-1",
            "turnIndex": 1,
            "connectedAccountChoices": [
                {
                    "connection_id": "goac_first",
                    "name": "First account",
                    "state": "active",
                    "email_address": None,
                }
            ],
            "designStarted": False,
            "designEnded": False,
            "draft": None,
            "blocks": [],
            "terminal": "question",
            "terminalMessage": "Choose an account",
            "narrativeSummary": None,
            "priorBlockCount": 0,
            "designActivity": [],
            "startedAt": None,
            "endedAt": None,
        },
        created_at=datetime(2026, 8, 17),
        modified_at=datetime(2026, 8, 17),
    )

    assert message.narrative_payload is not None
    assert message.narrative_payload["connectedAccountChoices"][0]["email_address"] is None


@pytest.mark.asyncio
async def test_verifies_ask_choices_by_org_and_preserves_model_order(monkeypatch: pytest.MonkeyPatch) -> None:
    async def visible_for_org(organization_id: str) -> list[GoogleOAuthCredentialBase]:
        assert organization_id == "org-1"
        return [
            _google("goac_first", "Google Sheets", email_address="first@example.test"),
            _google("goac_second", "Google Sheets", state="error", email_address="second@example.test"),
        ]

    monkeypatch.setattr(
        agent_module.google_oauth_service,
        "get_visible_credentials_for_org",
        visible_for_org,
    )

    choices = await agent_module._verified_connected_account_choices(
        {
            "connected_account_choices": [
                {"connection_id": "goac_second", "name": "forged"},
                {"connection_id": "foreign"},
                {"connection_id": "goac_second"},
                {"connection_id": "goac_first"},
                {"not_an_id": "goac_first"},
            ]
        },
        response_type="ASK_QUESTION",
        organization_id="org-1",
    )

    assert choices == [
        ConnectedAccountChoice(
            connection_id="goac_second",
            name="Google Sheets",
            state="error",
            email_address="second@example.test",
        ),
        ConnectedAccountChoice(
            connection_id="goac_first",
            name="Google Sheets",
            state="active",
            email_address="first@example.test",
        ),
    ]


@pytest.mark.asyncio
async def test_omits_choices_for_non_ask_or_lookup_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    async def failing_lookup(organization_id: str) -> list[GoogleOAuthCredentialBase]:
        nonlocal calls
        calls += 1
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(
        agent_module.google_oauth_service,
        "get_visible_credentials_for_org",
        failing_lookup,
    )
    proposal = {"connected_account_choices": [{"connection_id": "goac_first"}]}

    assert (
        await agent_module._verified_connected_account_choices(
            proposal,
            response_type="REPLY",
            organization_id="org-1",
        )
        is None
    )
    assert calls == 0
    assert (
        await agent_module._verified_connected_account_choices(
            proposal,
            response_type="ASK_QUESTION",
            organization_id="org-1",
        )
        is None
    )
    assert calls == 1


@pytest.mark.asyncio
async def test_staged_unapproved_google_connection_offers_server_verified_recovery_choices(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def visible_for_org(organization_id: str) -> list[GoogleOAuthCredentialBase]:
        assert organization_id == "org-1"
        return [
            _google("goac_first", "First account", email_address="first@example.test"),
            _google("goac_second", "Second account", state="error", email_address="second@example.test"),
        ]

    monkeypatch.setattr(
        agent_module.google_oauth_service,
        "get_visible_credentials_for_org",
        visible_for_org,
    )
    policy = request_policy_module.RequestPolicy(
        existing_workflow_credential_ids=["goac_model_staged"],
        run_approved_google_connection_ids=[],
    )

    choices = await agent_module._server_verified_connected_account_recovery_choices(
        policy,
        organization_id="org-1",
    )

    assert choices == [
        ConnectedAccountChoice(
            connection_id="goac_first",
            name="First account",
            state="active",
            email_address="first@example.test",
        ),
        ConnectedAccountChoice(
            connection_id="goac_second",
            name="Second account",
            state="error",
            email_address="second@example.test",
        ),
    ]


@pytest.mark.asyncio
async def test_recovery_choices_are_omitted_without_an_unapproved_staged_google_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lookup = AsyncMock()
    monkeypatch.setattr(
        agent_module.google_oauth_service,
        "get_visible_credentials_for_org",
        lookup,
    )
    policy = request_policy_module.RequestPolicy(
        existing_workflow_credential_ids=["goac_selected", "cred_password"],
        run_approved_google_connection_ids=["goac_selected"],
    )

    assert (
        await agent_module._server_verified_connected_account_recovery_choices(
            policy,
            organization_id="org-1",
        )
        is None
    )
    lookup.assert_not_awaited()


def test_prior_choices_enter_context_and_only_exact_id_is_structurally_selected() -> None:
    outcome = TurnOutcome(
        response_kind=ResponseKind.CLARIFY,
        connected_account_choices=[
            ConnectedAccountChoice(connection_id="goac_first", name="First account", state="active"),
            ConnectedAccountChoice(connection_id="goac_second", name="Second account", state="error"),
        ],
    )

    selected = connected_account_choice_context(outcome, "goac_first")
    free_text = connected_account_choice_context(outcome, "use the first account")

    assert '"selected_connection_id":"goac_first"' in selected
    assert '"connection_id":"goac_second"' in selected
    assert '"selected_connection_id"' not in free_text
    assert '"connection_id":"goac_first"' in free_text
    assert selected_connected_account_id(outcome, "goac_first") == "goac_first"
    assert selected_connected_account_id(outcome, "use the first account") is None


def test_server_owned_google_choice_copy_never_invites_prose_or_password_entry() -> None:
    reply = agent_module._connected_google_account_choice_reply()

    assert reply == (
        "Choose one of the connected Google accounts below so I can continue. "
        "Reconnect any unavailable account on the Integrations page first."
    )
    assert "name" not in reply.lower()
    assert "password" not in reply.lower()
    assert "credential id" not in reply.lower()


def test_selected_connected_account_id_requires_an_exact_prior_server_owned_choice() -> None:
    outcome = TurnOutcome(
        response_kind=ResponseKind.CLARIFY,
        connected_account_choices=[
            ConnectedAccountChoice(connection_id="goac_first", name="First account", state="active")
        ],
    )

    assert selected_connected_account_id(outcome, "goac_foreign") is None
    assert selected_connected_account_id(None, "goac_first") is None


@pytest.mark.asyncio
async def test_exact_clicked_active_account_grants_run_only_authority(monkeypatch: pytest.MonkeyPatch) -> None:
    active = _google("goac_first", "Google Sheets")
    lookup = AsyncMock(return_value=[active])
    monkeypatch.setattr(
        request_policy_module.google_oauth_service,
        "get_credentials_for_org",
        lookup,
    )

    policy = await request_policy_module._build_request_policy_bootstrap(
        user_message="goac_first",
        workflow_yaml="workflow_definition:\n  blocks: []\n",
        chat_history=[],
        global_llm_context="",
        organization_id="org-1",
        selected_connected_account_id="goac_first",
    )

    assert policy.run_approved_google_connection_ids == ["goac_first"]
    assert policy.resolved_credentials == []
    lookup.assert_awaited_once_with("org-1")


@pytest.mark.asyncio
async def test_persisted_active_account_grants_run_authority_but_staged_account_does_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active = _google("goac_saved", "Google Sheets")
    monkeypatch.setattr(
        request_policy_module.google_oauth_service,
        "get_credentials_for_org",
        AsyncMock(return_value=[active]),
    )
    saved_yaml = """
workflow_definition:
  blocks:
    - label: write
      block_type: google_sheets_write
      credential_id: goac_saved
"""
    staged_yaml = saved_yaml.replace("goac_saved", "goac_staged")

    policy = await request_policy_module._build_request_policy_bootstrap(
        user_message="run it",
        workflow_yaml=staged_yaml,
        chat_history=[],
        global_llm_context="",
        organization_id="org-1",
        persisted_workflow_yaml=saved_yaml,
    )

    assert policy.run_approved_google_connection_ids == ["goac_saved"]
    assert "goac_staged" not in policy.run_approved_google_connection_ids
    assert policy.resolved_credentials == []


@pytest.mark.asyncio
async def test_clicked_foreign_or_inactive_account_grants_no_run_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        request_policy_module.google_oauth_service,
        "get_credentials_for_org",
        AsyncMock(return_value=[]),
    )

    policy = await request_policy_module._build_request_policy_bootstrap(
        user_message="goac_not_active_for_org",
        workflow_yaml="workflow_definition:\n  blocks: []\n",
        chat_history=[],
        global_llm_context="",
        organization_id="org-1",
        selected_connected_account_id="goac_not_active_for_org",
    )

    assert policy.run_approved_google_connection_ids == []
    assert policy.resolved_credentials == []


def test_turn_outcome_choices_are_derived_into_the_terminal_sse_payload() -> None:
    outcome = TurnOutcome(
        response_kind=ResponseKind.CLARIFY,
        connected_account_choices=[
            ConnectedAccountChoice(connection_id="goac_first", name="First account", state="active")
        ],
    )

    result = agent_module._make_agent_result(
        make_copilot_ctx(turn_id="turn-1"),
        user_response="Which account?",
        updated_workflow=None,
        turn_outcome=outcome,
        narrative_payload={
            "turnId": "turn-1",
            "turnIndex": 0,
            "designStarted": True,
            "designEnded": True,
            "draft": None,
            "blocks": [],
            "terminal": "response",
            "terminalMessage": "Which account?",
            "narrativeSummary": "Which account?",
            "priorBlockCount": 0,
            "designActivity": [],
            "startedAt": None,
            "endedAt": None,
        },
    )

    assert result.narrative_payload is not None
    assert result.narrative_payload["connectedAccountChoices"] == [
        {
            "connection_id": "goac_first",
            "name": "First account",
            "state": "active",
            "email_address": None,
        }
    ]
