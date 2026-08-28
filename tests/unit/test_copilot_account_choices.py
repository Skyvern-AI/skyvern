from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from skyvern.forge.sdk.copilot import agent as agent_module
from skyvern.forge.sdk.copilot import request_policy as request_policy_module
from skyvern.forge.sdk.copilot.context import (
    ApprovedCredential,
    StructuredContext,
    adopt_model_authored_context,
    record_approved_credentials_in_global_llm_context,
)
from skyvern.forge.sdk.copilot.run_outcome import RecordedRunOutcome
from skyvern.forge.sdk.copilot.terminal_envelope import (
    TerminalOutcomeEnvelope,
    assemble_terminal_envelope,
    render_terminal_message,
)
from skyvern.forge.sdk.copilot.tools.credentials import (
    _approved_run_credential_ids,
    _credential_run_approval_blocker_signal,
    _credential_run_approval_error,
    _extract_credential_ids_for_labels,
    _parsed_workflow_definition,
)
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


@pytest.mark.asyncio
async def test_picked_account_still_grants_run_authority_on_the_next_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pick has to outlive the turn it arrived on.

    Run authority is derived from the persisted workflow, and a copilot build that has not saved
    yet contributes nothing to it. Without the pick carried forward, the user is asked to choose
    an account again on the very next turn and the run never dispatches.
    """
    active = _google("goac_picked", "Google Sheets")
    monkeypatch.setattr(
        request_policy_module.google_oauth_service,
        "get_credentials_for_org",
        AsyncMock(return_value=[active]),
    )
    draft = "workflow_definition:\n  blocks:\n    - label: write\n      block_type: google_sheets_write\n      credential_id: goac_picked\n"

    picked = await request_policy_module._build_request_policy_bootstrap(
        user_message="goac_picked",
        workflow_yaml=draft,
        chat_history=[],
        global_llm_context="",
        organization_id="org-1",
        selected_connected_account_id="goac_picked",
    )
    assert picked.run_approved_google_connection_ids == ["goac_picked"]

    carried = record_approved_credentials_in_global_llm_context(
        SimpleNamespace(request_policy=picked, credential_pause_connected_credential_id=None),
        "",
    )
    assert carried is not None

    next_turn = await request_policy_module._build_request_policy_bootstrap(
        user_message="run the workflow now",
        workflow_yaml=draft,
        chat_history=[],
        global_llm_context=carried,
        organization_id="org-1",
        persisted_workflow_yaml=None,
    )

    assert next_turn.run_approved_google_connection_ids == ["goac_picked"]


@pytest.mark.asyncio
async def test_model_authored_context_cannot_forge_a_connection_approval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A carried approval is only worth anything if the model cannot write one itself."""
    forged = StructuredContext()
    forged.approved_connections = [ApprovedCredential(credential_id="goac_never_picked")]

    adopted = adopt_model_authored_context("", forged.model_dump(mode="json"))

    assert adopted.approved_connections == []


EDITOR_BOUND_ACCOUNT_ID = "goac_editor_bound"
NAMED_PICK_ACCOUNT_ID = "goac_named_pick"
STALE_ACCOUNT_ID = "goac_stale"
EMPTY_WORKFLOW_YAML = "workflow_definition:\n  blocks: []\n"
SHEETS_BLOCK_LABEL = "write"


def _sheets_workflow_yaml(connection_id: str) -> str:
    return (
        "workflow_definition:\n"
        "  blocks:\n"
        f"    - label: {SHEETS_BLOCK_LABEL}\n"
        "      block_type: google_sheets_write\n"
        f"      credential_id: {connection_id}\n"
    )


def _dispatch_credential_ids(workflow_yaml: str) -> list[str]:
    return _extract_credential_ids_for_labels(_parsed_workflow_definition(workflow_yaml), [SHEETS_BLOCK_LABEL])


def _terminal_envelope(run_outcomes: list[RecordedRunOutcome]) -> TerminalOutcomeEnvelope:
    envelope = assemble_terminal_envelope(
        response_type="REPLY",
        verified=True,
        workflow_applied=False,
        proposal_disposition="no_proposal",
        run_outcomes=run_outcomes,
        blocker_reason=None,
        halt_kind=None,
        attempted=None,
        workflow_mutated=True,
        workflow_attempted=True,
    )
    assert envelope is not None
    return envelope


@pytest.mark.asyncio
async def test_editor_bound_account_keeps_run_authority_from_resolution_through_the_dispatch_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        request_policy_module.google_oauth_service,
        "get_credentials_for_org",
        AsyncMock(return_value=[_google(EDITOR_BOUND_ACCOUNT_ID, "Google Sheets")]),
    )
    saved_yaml = _sheets_workflow_yaml(EDITOR_BOUND_ACCOUNT_ID)

    resolved = await request_policy_module._build_request_policy_bootstrap(
        user_message="run the workflow",
        workflow_yaml=saved_yaml,
        chat_history=[],
        global_llm_context="",
        organization_id="org-1",
        persisted_workflow_yaml=saved_yaml,
        selected_connected_account_id=None,
    )

    assert resolved.run_approved_google_connection_ids == [EDITOR_BOUND_ACCOUNT_ID]

    next_turn = await request_policy_module._build_request_policy_bootstrap(
        user_message="add a step that emails me when it finishes",
        workflow_yaml=EMPTY_WORKFLOW_YAML,
        chat_history=[],
        global_llm_context="",
        organization_id="org-1",
        persisted_workflow_yaml=saved_yaml,
    )

    dispatched_ids = _dispatch_credential_ids(saved_yaml)

    assert dispatched_ids == [EDITOR_BOUND_ACCOUNT_ID]
    assert next_turn.run_approved_google_connection_ids == [EDITOR_BOUND_ACCOUNT_ID]
    assert EDITOR_BOUND_ACCOUNT_ID in _approved_run_credential_ids(next_turn)
    assert _credential_run_approval_error(dispatched_ids, next_turn) is None
    assert _credential_run_approval_blocker_signal(dispatched_ids, next_turn) is None


@pytest.mark.asyncio
async def test_named_account_with_no_server_owned_choice_selects_nothing_and_stays_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lookup = AsyncMock(return_value=[_google(NAMED_PICK_ACCOUNT_ID, "Sheets Writer", email_address="w@example.test")])
    monkeypatch.setattr(request_policy_module.google_oauth_service, "get_credentials_for_org", lookup)
    named_message = "use my Sheets Writer google account"

    policy = await request_policy_module._build_request_policy_bootstrap(
        user_message=named_message,
        workflow_yaml=EMPTY_WORKFLOW_YAML,
        chat_history=[],
        global_llm_context="",
        organization_id="org-1",
        persisted_workflow_yaml=None,
        selected_connected_account_id=None,
    )

    assert policy.selected_connected_account_id is None
    assert policy.run_approved_google_connection_ids == []
    lookup.assert_not_awaited()
    assert _approved_run_credential_ids(policy) == set()
    assert _credential_run_approval_error([NAMED_PICK_ACCOUNT_ID], policy) is not None

    offered = TurnOutcome(
        response_kind=ResponseKind.CLARIFY,
        connected_account_choices=[
            ConnectedAccountChoice(connection_id=NAMED_PICK_ACCOUNT_ID, name="Sheets Writer", state="active")
        ],
    )

    assert selected_connected_account_id(offered, named_message) is None
    assert f'"connection_id":"{NAMED_PICK_ACCOUNT_ID}"' in connected_account_choice_context(offered, named_message)


@pytest.mark.asyncio
async def test_named_account_pick_keeps_run_authority_from_resolution_through_the_dispatch_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        request_policy_module.google_oauth_service,
        "get_credentials_for_org",
        AsyncMock(return_value=[_google(NAMED_PICK_ACCOUNT_ID, "Sheets Writer", email_address="w@example.test")]),
    )
    draft = _sheets_workflow_yaml(NAMED_PICK_ACCOUNT_ID)

    picked = await request_policy_module._build_request_policy_bootstrap(
        user_message=NAMED_PICK_ACCOUNT_ID,
        workflow_yaml=draft,
        chat_history=[],
        global_llm_context="",
        organization_id="org-1",
        selected_connected_account_id=NAMED_PICK_ACCOUNT_ID,
    )

    assert picked.run_approved_google_connection_ids == [NAMED_PICK_ACCOUNT_ID]

    carried = record_approved_credentials_in_global_llm_context(
        make_copilot_ctx(workflow_yaml=draft, request_policy=picked),
        "",
    )
    assert carried is not None

    next_turn = await request_policy_module._build_request_policy_bootstrap(
        user_message="run it now",
        workflow_yaml=draft,
        chat_history=[],
        global_llm_context=carried,
        organization_id="org-1",
        persisted_workflow_yaml=None,
    )

    dispatched_ids = _dispatch_credential_ids(draft)

    assert dispatched_ids == [NAMED_PICK_ACCOUNT_ID]
    assert next_turn.run_approved_google_connection_ids == [NAMED_PICK_ACCOUNT_ID]
    assert NAMED_PICK_ACCOUNT_ID in _approved_run_credential_ids(next_turn)
    assert _credential_run_approval_error(dispatched_ids, next_turn) is None
    assert _credential_run_approval_blocker_signal(dispatched_ids, next_turn) is None


@pytest.mark.asyncio
async def test_editor_bound_account_that_is_no_longer_active_is_refused_at_the_dispatch_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        request_policy_module.google_oauth_service,
        "get_credentials_for_org",
        AsyncMock(return_value=[_google("goac_other_active", "Another account")]),
    )
    saved_yaml = _sheets_workflow_yaml(STALE_ACCOUNT_ID)

    policy = await request_policy_module._build_request_policy_bootstrap(
        user_message="run the workflow",
        workflow_yaml=saved_yaml,
        chat_history=[],
        global_llm_context="",
        organization_id="org-1",
        persisted_workflow_yaml=saved_yaml,
    )

    dispatched_ids = _dispatch_credential_ids(saved_yaml)

    assert dispatched_ids == [STALE_ACCOUNT_ID]
    assert policy.persisted_workflow_credential_ids == [STALE_ACCOUNT_ID]
    assert policy.run_approved_google_connection_ids == []
    assert STALE_ACCOUNT_ID not in _approved_run_credential_ids(policy)
    assert _credential_run_approval_error(dispatched_ids, policy) is not None

    blocker = _credential_run_approval_blocker_signal(dispatched_ids, policy)

    assert blocker is not None
    assert blocker.blocker_kind == "authority_denied"
    assert blocker.internal_reason_code == "unapproved_google_connection_reference"


def test_terminal_without_a_run_receipt_reports_no_run() -> None:
    envelope = _terminal_envelope([])
    message, replaced = render_terminal_message(envelope, "ok", False)

    assert replaced
    assert "I ran the workflow" not in message
