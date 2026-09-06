from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from skyvern.forge.sdk.copilot import agent as agent_module
from skyvern.forge.sdk.copilot import request_policy as request_policy_module
from skyvern.forge.sdk.copilot.context import (
    ApprovedCredential,
    CopilotContext,
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
    _approve_server_verified_google_sheet_bindings,
    _approved_run_credential_ids,
    _credential_run_approval_blocker_signal,
    _credential_run_approval_error,
    _extract_credential_ids_for_labels,
    _google_connection_reference_ids,
    _parsed_workflow_definition,
    canonicalize_named_google_sheet_bindings,
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
from skyvern.forge.sdk.schemas.workflow_copilot import (
    WorkflowCopilotChatMessage,
    WorkflowCopilotChatRequest,
    WorkflowCopilotChatSender,
)
from tests.unit.copilot_test_helpers import make_copilot_ctx


def _google(
    connection_id: str,
    name: str,
    state: str = "active",
    email_address: str | None = None,
    scopes_granted: list[str] | None = None,
) -> GoogleOAuthCredentialBase:
    return GoogleOAuthCredentialBase(
        id=connection_id,
        organization_id="org-1",
        credential_name=name,
        email_address=email_address,
        state=state,
        scopes_requested=["https://www.googleapis.com/auth/spreadsheets"],
        scopes_granted=scopes_granted or ["https://www.googleapis.com/auth/spreadsheets"],
        created_at=datetime(2026, 8, 15),
        modified_at=datetime(2026, 8, 15),
    )


def _listed_integrations(*credentials: GoogleOAuthCredentialBase) -> list[dict[str, object]]:
    return [
        {
            "tool": "list_integrations",
            "integrations": [
                {
                    "connection_id": credential.id,
                    "provider": "google",
                    "state": credential.state,
                    "scopes_granted": list(credential.scopes_granted),
                }
                for credential in credentials
            ],
        }
    ]


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

    selected = connected_account_choice_context(outcome, explicit_selected_connection_id="goac_first")
    free_text = connected_account_choice_context(outcome)

    assert '"selected_connection_id":"goac_first"' in selected
    assert '"connection_id":"goac_second"' in selected
    assert '"selected_connection_id"' not in free_text
    assert '"connection_id":"goac_first"' in free_text
    assert selected_connected_account_id(outcome, "goac_first") == "goac_first"
    assert selected_connected_account_id(outcome, "use the first account") is None


@pytest.mark.parametrize("prior_outcome", [None, TurnOutcome(response_kind=ResponseKind.CLARIFY)])
def test_fresh_picker_selection_enters_model_context_without_prior_choices(prior_outcome) -> None:
    context = connected_account_choice_context(prior_outcome, explicit_selected_connection_id="goac_new")
    assert '"selected_connection_id":"goac_new"' in context
    assert '"selection_source":"user_picker"' in context
    assert connected_account_choice_context(prior_outcome) == ""


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

    request = WorkflowCopilotChatRequest(
        workflow_permanent_id="wpid_test",
        workflow_id="wf_test",
        message="goac_picked",
        workflow_yaml=draft,
        selected_connected_account_id="goac_picked",
    )
    assert selected_connected_account_id(None, request.message) is None
    picked = await request_policy_module._build_request_policy_bootstrap(
        user_message="goac_picked",
        workflow_yaml=draft,
        chat_history=[],
        global_llm_context="",
        organization_id="org-1",
        selected_connected_account_id=request.selected_connected_account_id,
    )
    assert picked.run_approved_google_connection_ids == ["goac_picked"]
    assert picked.selected_connected_account_id == "goac_picked"

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
    assert f'"connection_id":"{NAMED_PICK_ACCOUNT_ID}"' in connected_account_choice_context(offered)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("cited_connection_id", "active_connections"),
    [
        ("goac_inactive", [_google("goac_other", "Other active account")]),
        ("goac_unknown", []),
    ],
)
async def test_inactive_or_unknown_model_bound_account_stays_authority_denied(
    monkeypatch: pytest.MonkeyPatch,
    cited_connection_id: str,
    active_connections: list[GoogleOAuthCredentialBase],
) -> None:
    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.tools.credentials.google_oauth_service.get_visible_credentials_for_org",
        AsyncMock(return_value=active_connections),
    )
    policy = request_policy_module.RequestPolicy()

    approved = await _approve_server_verified_google_sheet_bindings(
        [(SHEETS_BLOCK_LABEL, cited_connection_id)],
        tool_activity=_listed_integrations(_google(cited_connection_id, "Cited account")),
        organization_id="org-1",
        request_policy=policy,
    )
    blocker = _credential_run_approval_blocker_signal([cited_connection_id], policy)

    assert approved == []
    assert policy.run_approved_google_connection_ids == []
    assert blocker is not None
    assert blocker.blocker_kind == "authority_denied"


@pytest.mark.asyncio
async def test_model_bound_account_requires_sheets_scope_and_effective_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    incompatible = _google("goac_gmail", "Gmail account").model_copy(
        update={"scopes_granted": ["https://www.googleapis.com/auth/gmail.readonly"]}
    )
    lookup = AsyncMock(
        return_value=[
            incompatible,
            _google("goac_not_executed", "Unused Sheets account"),
            _google(NAMED_PICK_ACCOUNT_ID, "Sheets Writer"),
        ]
    )
    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.tools.credentials.google_oauth_service.get_visible_credentials_for_org",
        lookup,
    )
    policy = request_policy_module.RequestPolicy()

    approved = await _approve_server_verified_google_sheet_bindings(
        [(SHEETS_BLOCK_LABEL, NAMED_PICK_ACCOUNT_ID)],
        tool_activity=_listed_integrations(incompatible, _google(NAMED_PICK_ACCOUNT_ID, "Sheets Writer")),
        organization_id="org-1",
        request_policy=policy,
    )

    assert approved == [NAMED_PICK_ACCOUNT_ID]
    assert policy.run_approved_google_connection_ids == []
    assert (
        _credential_run_approval_blocker_signal(
            [NAMED_PICK_ACCOUNT_ID],
            policy,
            additional_approved_ids=approved,
        )
        is None
    )


@pytest.mark.asyncio
async def test_model_bound_account_lookup_failure_preserves_authority_denial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.tools.credentials.google_oauth_service.get_visible_credentials_for_org",
        AsyncMock(side_effect=RuntimeError("database unavailable")),
    )
    policy = request_policy_module.RequestPolicy()

    approved = await _approve_server_verified_google_sheet_bindings(
        [(SHEETS_BLOCK_LABEL, NAMED_PICK_ACCOUNT_ID)],
        tool_activity=_listed_integrations(_google(NAMED_PICK_ACCOUNT_ID, "Sheets Writer")),
        organization_id="org-1",
        request_policy=policy,
    )

    assert approved == []
    assert policy.run_approved_google_connection_ids == []


@pytest.mark.asyncio
async def test_model_bound_account_without_same_turn_list_result_stays_authority_denied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lookup = AsyncMock(return_value=[_google(NAMED_PICK_ACCOUNT_ID, "Sheets Writer")])
    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.tools.credentials.google_oauth_service.get_visible_credentials_for_org",
        lookup,
    )
    policy = request_policy_module.RequestPolicy()

    approved = await _approve_server_verified_google_sheet_bindings(
        [(SHEETS_BLOCK_LABEL, NAMED_PICK_ACCOUNT_ID)],
        tool_activity=[],
        organization_id="org-1",
        request_policy=policy,
    )

    assert approved == []
    assert policy.run_approved_google_connection_ids == []
    lookup.assert_not_awaited()


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


CITED_ACCOUNT_ID = "goac_cited"
CITED_ACCOUNT_NAME = "Blog Metrics Connection"
AMBIGUOUS_ACCOUNT_ID = "goac_ambiguous"


def _named_sheets_yaml(reference: str) -> str:
    return (
        "workflow_definition:\n"
        "  blocks:\n"
        f"    - label: {SHEETS_BLOCK_LABEL}\n"
        "      block_type: google_sheets_write\n"
        f'      credential_id: "{reference}"\n'
    )


def _canonicalization_ctx(workflow_yaml: str, user_message: str) -> CopilotContext:
    return make_copilot_ctx(
        workflow_yaml=workflow_yaml,
        request_policy=request_policy_module.RequestPolicy(canonical_user_message=user_message),
    )


def _patch_visible_credentials(
    monkeypatch: pytest.MonkeyPatch,
    credentials: list[GoogleOAuthCredentialBase] | Exception,
) -> None:
    mock = (
        AsyncMock(side_effect=credentials)
        if isinstance(credentials, Exception)
        else AsyncMock(return_value=credentials)
    )
    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.tools.credentials.google_oauth_service.get_visible_credentials_for_org",
        mock,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("citation", [CITED_ACCOUNT_NAME, CITED_ACCOUNT_NAME.casefold()])
async def test_cited_connection_name_canonicalizes_and_keeps_run_authority(
    monkeypatch: pytest.MonkeyPatch,
    citation: str,
) -> None:
    accounts = [_google(CITED_ACCOUNT_ID, CITED_ACCOUNT_NAME, email_address="metrics@example.test")]
    _patch_visible_credentials(monkeypatch, accounts)
    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.tools.credentials.google_oauth_service.get_visible_credentials_for_org",
        AsyncMock(return_value=accounts),
    )
    draft = _named_sheets_yaml(citation)
    ctx = _canonicalization_ctx(draft, f'write the rows with the "{citation}" account')

    canonical_yaml, facts = await canonicalize_named_google_sheet_bindings(draft, ctx)

    assert [(fact["status"], fact["canonicalized"], fact["connection_id"]) for fact in facts] == [
        ("resolved", True, CITED_ACCOUNT_ID)
    ]
    dispatched_ids = _dispatch_credential_ids(canonical_yaml)
    assert dispatched_ids == [CITED_ACCOUNT_ID]

    policy = ctx.request_policy
    approved = await _approve_server_verified_google_sheet_bindings(
        [(SHEETS_BLOCK_LABEL, CITED_ACCOUNT_ID)],
        tool_activity=[],
        organization_id="org-1",
        request_policy=policy,
    )

    assert approved == [CITED_ACCOUNT_ID]
    assert (
        _credential_run_approval_blocker_signal(
            dispatched_ids,
            policy,
            additional_approved_ids=approved,
            google_reference_ids=_google_connection_reference_ids(
                _parsed_workflow_definition(canonical_yaml), [SHEETS_BLOCK_LABEL]
            ),
        )
        is None
    )


@pytest.mark.asyncio
async def test_ambiguous_cited_name_admits_no_account_and_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    accounts = [
        _google(AMBIGUOUS_ACCOUNT_ID, "Shared Sheets Account", email_address="one@example.test"),
        _google("goac_ambiguous_twin", "shared sheets account", email_address="two@example.test"),
    ]
    _patch_visible_credentials(monkeypatch, accounts)
    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.tools.credentials.google_oauth_service.get_visible_credentials_for_org",
        AsyncMock(return_value=accounts),
    )
    draft = _named_sheets_yaml("Shared Sheets Account")
    ctx = _canonicalization_ctx(draft, 'use the "Shared Sheets Account" connection')

    canonical_yaml, facts = await canonicalize_named_google_sheet_bindings(draft, ctx)

    assert canonical_yaml == draft
    assert facts[0]["status"] == "ambiguous"
    assert [candidate["connection_id"] for candidate in facts[0]["candidates"]] == [
        AMBIGUOUS_ACCOUNT_ID,
        "goac_ambiguous_twin",
    ]

    policy = ctx.request_policy
    model_bound = await _approve_server_verified_google_sheet_bindings(
        [(SHEETS_BLOCK_LABEL, AMBIGUOUS_ACCOUNT_ID)],
        tool_activity=[],
        organization_id="org-1",
        request_policy=policy,
    )

    assert model_bound == []

    blocker = _credential_run_approval_blocker_signal(
        [AMBIGUOUS_ACCOUNT_ID],
        policy,
        additional_approved_ids=model_bound,
    )

    assert blocker is not None
    assert blocker.blocker_kind == "authority_denied"
    assert blocker.preserves_workflow_draft

    name_in_slot_blocker = _credential_run_approval_blocker_signal(
        _dispatch_credential_ids(canonical_yaml),
        policy,
        additional_approved_ids=model_bound,
        google_reference_ids=_google_connection_reference_ids(
            _parsed_workflow_definition(canonical_yaml), [SHEETS_BLOCK_LABEL]
        ),
    )

    assert name_in_slot_blocker is not None
    assert name_in_slot_blocker.blocker_kind == "authority_denied"
    assert name_in_slot_blocker.preserves_workflow_draft


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reference", "user_message", "expected_status"),
    [
        ("Missing Reporting Connection", 'bind the "Missing Reporting Connection" account', "not_found"),
        ("Dead Sheets Account", 'bind the "Dead Sheets Account" account', "ineligible"),
        ("Blog Metrics Connection", "bind whichever google account works", "not_cited"),
    ],
)
async def test_unresolved_connection_reference_reports_facts_and_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    reference: str,
    user_message: str,
    expected_status: str,
) -> None:
    accounts = [
        _google(CITED_ACCOUNT_ID, CITED_ACCOUNT_NAME, email_address="metrics@example.test"),
        _google("goac_dead", "Dead Sheets Account", state="error", email_address="dead@example.test"),
    ]
    _patch_visible_credentials(monkeypatch, accounts)
    draft = _named_sheets_yaml(reference)
    ctx = _canonicalization_ctx(draft, user_message)

    canonical_yaml, facts = await canonicalize_named_google_sheet_bindings(draft, ctx)

    assert canonical_yaml == draft
    assert facts[0]["status"] == expected_status
    assert facts[0]["canonicalized"] is False
    assert [row["connection_id"] for row in facts[0]["eligible_connections"]] == [CITED_ACCOUNT_ID]

    blocker = _credential_run_approval_blocker_signal(
        _dispatch_credential_ids(canonical_yaml),
        ctx.request_policy,
        google_reference_ids=_google_connection_reference_ids(
            _parsed_workflow_definition(canonical_yaml), [SHEETS_BLOCK_LABEL]
        ),
    )

    assert blocker is not None
    assert blocker.blocker_kind == "authority_denied"


@pytest.mark.asyncio
async def test_saved_credential_slot_is_excluded_from_the_connection_seam(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_visible_credentials(monkeypatch, [_google(CITED_ACCOUNT_ID, CITED_ACCOUNT_NAME)])
    draft = _named_sheets_yaml("cred_qablogmetrics")
    ctx = _canonicalization_ctx(draft, "use cred_qablogmetrics for the sheet")

    canonical_yaml, facts = await canonicalize_named_google_sheet_bindings(draft, ctx)

    assert canonical_yaml == draft
    assert facts == []
    assert _google_connection_reference_ids(_parsed_workflow_definition(canonical_yaml), [SHEETS_BLOCK_LABEL]) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "slot_value",
    [
        "{{ sheets_connection }}",
        "{{ workflow.sheets_connection }}",
        "{{ sheets_connection | default('goac_x') }}",
        "{% if x %}goac_a{% else %}goac_b{% endif %}",
    ],
)
async def test_templated_slot_without_a_credential_parameter_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    slot_value: str,
) -> None:
    _patch_visible_credentials(monkeypatch, [_google(CITED_ACCOUNT_ID, CITED_ACCOUNT_NAME)])
    draft = _named_sheets_yaml(slot_value)
    ctx = _canonicalization_ctx(draft, f"use {slot_value} for the sheet")

    canonical_yaml, facts = await canonicalize_named_google_sheet_bindings(draft, ctx)

    assert canonical_yaml == draft
    assert facts == []
    reference_ids = _google_connection_reference_ids(_parsed_workflow_definition(canonical_yaml), [SHEETS_BLOCK_LABEL])
    assert reference_ids == [slot_value]
    blocker = _credential_run_approval_blocker_signal(
        _dispatch_credential_ids(canonical_yaml), ctx.request_policy, google_reference_ids=reference_ids
    )
    assert blocker is not None
    assert blocker.blocker_kind == "authority_denied"


@pytest.mark.parametrize(
    ("slot_value", "expected_reference_ids"),
    [
        ("{{ sheets_connection }}", []),
        ("goac_{{ sheets_connection }}", ["goac_{{ sheets_connection }}"]),
    ],
)
def test_templated_slot_backed_by_a_credential_parameter_carries_its_own_authority(
    slot_value: str,
    expected_reference_ids: list[str],
) -> None:
    workflow_definition = {
        "parameters": [{"key": "sheets_connection", "parameter_type": "credential", "credential_id": "cred_sheets"}],
        "blocks": [
            {
                "label": SHEETS_BLOCK_LABEL,
                "block_type": "google_sheets_write",
                "credential_id": slot_value,
            }
        ],
    }

    assert _google_connection_reference_ids(workflow_definition, [SHEETS_BLOCK_LABEL]) == expected_reference_ids


@pytest.mark.asyncio
async def test_connection_lookup_failure_keeps_the_draft_and_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_visible_credentials(monkeypatch, RuntimeError("database unavailable"))
    draft = _named_sheets_yaml(CITED_ACCOUNT_NAME)
    ctx = _canonicalization_ctx(draft, f'use the "{CITED_ACCOUNT_NAME}" account')

    canonical_yaml, facts = await canonicalize_named_google_sheet_bindings(draft, ctx)

    assert canonical_yaml == draft
    assert facts[0]["status"] == "lookup_failed"

    blocker = _credential_run_approval_blocker_signal(
        _dispatch_credential_ids(canonical_yaml),
        ctx.request_policy,
        google_reference_ids=_google_connection_reference_ids(
            _parsed_workflow_definition(canonical_yaml), [SHEETS_BLOCK_LABEL]
        ),
    )

    assert blocker is not None
    assert blocker.blocker_kind == "authority_denied"


@pytest.mark.asyncio
async def test_cited_name_shared_with_an_unscoped_connection_admits_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    accounts = [
        _google(CITED_ACCOUNT_ID, "Reporting Account", email_address="sheets@example.test"),
        _google(
            "goac_no_sheets_scope",
            "Reporting Account",
            email_address="drive@example.test",
            scopes_granted=["https://www.googleapis.com/auth/drive.file"],
        ),
    ]
    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.tools.credentials.google_oauth_service.get_visible_credentials_for_org",
        AsyncMock(return_value=accounts),
    )

    approved = await _approve_server_verified_google_sheet_bindings(
        [(SHEETS_BLOCK_LABEL, CITED_ACCOUNT_ID)],
        tool_activity=[],
        organization_id="org-1",
        request_policy=request_policy_module.RequestPolicy(
            canonical_user_message='use the "Reporting Account" connection'
        ),
    )

    assert approved == []


def _sibling_named_accounts(longer_sibling_state: str = "active") -> list[GoogleOAuthCredentialBase]:
    return [
        _google("goac_marketing", "Marketing", email_address="marketing@example.test"),
        _google(
            "goac_marketing_archive",
            "Marketing Archive",
            state=longer_sibling_state,
            email_address="archive@example.test",
        ),
    ]


@pytest.mark.asyncio
async def test_name_contained_in_a_longer_sibling_name_is_not_cited(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_visible_credentials(monkeypatch, _sibling_named_accounts())
    draft = _named_sheets_yaml("Marketing")
    ctx = _canonicalization_ctx(draft, "please read rows from the Marketing Archive connection")

    canonical_yaml, facts = await canonicalize_named_google_sheet_bindings(draft, ctx)

    assert canonical_yaml == draft
    assert [(fact["status"], fact["canonicalized"]) for fact in facts] == [("not_cited", False)]


@pytest.mark.asyncio
@pytest.mark.parametrize("longer_sibling_state", ["active", "error"])
async def test_name_contained_in_a_longer_sibling_name_admits_nothing_at_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    longer_sibling_state: str,
) -> None:
    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.tools.credentials.google_oauth_service.get_visible_credentials_for_org",
        AsyncMock(return_value=_sibling_named_accounts(longer_sibling_state)),
    )

    approved = await _approve_server_verified_google_sheet_bindings(
        [(SHEETS_BLOCK_LABEL, "goac_marketing")],
        tool_activity=[],
        organization_id="org-1",
        request_policy=request_policy_module.RequestPolicy(
            canonical_user_message="please read rows from the Marketing Archive connection"
        ),
    )

    assert approved == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state,scopes,organization_id",
    [
        ("revoked", ["https://www.googleapis.com/auth/spreadsheets"], "org-1"),
        ("active", ["openid"], "org-1"),
        ("active", ["https://www.googleapis.com/auth/spreadsheets"], "org-other"),
    ],
)
async def test_picker_selection_rejects_unusable_account_before_persisting_approval(
    monkeypatch, state, scopes, organization_id
):
    account = _google("goac_new", "New account", state=state, scopes_granted=scopes)
    account.organization_id = organization_id
    monkeypatch.setattr(
        request_policy_module.google_oauth_service, "get_credentials_for_org", AsyncMock(return_value=[account])
    )
    policy = await request_policy_module._build_request_policy_bootstrap(
        user_message="goac_new",
        workflow_yaml="workflow_definition:\n  blocks: []\n",
        chat_history=[],
        global_llm_context="",
        organization_id="org-1",
        selected_connected_account_id="goac_new",
    )
    assert policy.selected_connected_account_id is None
    assert '"selected_connection_id"' not in connected_account_choice_context(
        TurnOutcome(
            response_kind=ResponseKind.CLARIFY,
            connected_account_choices=[
                ConnectedAccountChoice(connection_id="goac_new", name="New account", state="active")
            ],
        ),
        explicit_selected_connection_id=policy.selected_connected_account_id,
    )
    assert policy.run_approved_google_connection_ids == []
