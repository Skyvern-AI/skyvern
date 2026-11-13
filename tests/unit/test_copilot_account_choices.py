from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from datetime import datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import pytest_asyncio

from skyvern.config import settings
from skyvern.forge import app
from skyvern.forge.sdk.copilot import agent as agent_module
from skyvern.forge.sdk.copilot import request_policy as request_policy_module
from skyvern.forge.sdk.copilot import tools as tools_module
from skyvern.forge.sdk.copilot.connected_accounts import (
    connection_is_cited,
    resolve_connection_reference,
)
from skyvern.forge.sdk.copilot.context import (
    ApprovedCredential,
    CopilotContext,
    StructuredContext,
    adopt_model_authored_context,
    record_approved_credentials_in_global_llm_context,
)
from skyvern.forge.sdk.copilot.hooks import CopilotRunHooks
from skyvern.forge.sdk.copilot.output_utils import sanitize_tool_result_for_llm
from skyvern.forge.sdk.copilot.tools import credentials as credentials_module
from skyvern.forge.sdk.copilot.tools import mcp_hooks
from skyvern.forge.sdk.copilot.tools.credentials import (
    UNAPPROVED_CONNECTION_REASON_CODE,
    _approved_run_credential_ids,
    _connection_approval_blocker_signal,
    _credential_run_approval_error,
    _parsed_workflow_definition,
    _retire_stale_connection_denial,
    admitted_connection_ids,
    canonicalize_named_connection_bindings,
    resolve_google_sheet_tabs_from_gid,
    run_credential_gate,
)
from skyvern.forge.sdk.copilot.tools.integrations import _read_google_sheet
from skyvern.forge.sdk.copilot.turn_outcome import (
    connected_account_choice_context,
    selected_connected_account_id,
)
from skyvern.forge.sdk.copilot.workflow_credential_utils import workflow_connection_refs
from skyvern.forge.sdk.integrations.connected_accounts import (
    ConnectionProvider,
    IncompleteConnectionRead,
    OrgConnection,
    org_connections,
)
from skyvern.forge.sdk.routes import workflow_copilot as workflow_copilot_route
from skyvern.forge.sdk.schemas.copilot_turn_outcome import (
    ConnectedAccountChoice,
    ResponseKind,
    TurnOutcome,
)
from skyvern.forge.sdk.schemas.google_oauth import GoogleOAuthCredentialBase
from skyvern.forge.sdk.schemas.microsoft_oauth import MicrosoftOAuthCredentialBase
from skyvern.forge.sdk.schemas.workflow_copilot import (
    WorkflowCopilotChatMessage,
    WorkflowCopilotChatRequest,
    WorkflowCopilotChatSender,
)
from skyvern.forge.sdk.services import google_oauth_service, microsoft_oauth_service
from skyvern.forge.sdk.workflow.models.agent_step_tools import ACCOUNT_TOOL_NAMES
from skyvern.forge.sdk.workflow.models.block import WorkflowTriggerBlock
from skyvern.utils.yaml_loader import safe_load_no_dates
from tests.unit.copilot_test_helpers import make_copilot_ctx


def _google(
    connection_id: str,
    name: str,
    state: str = "active",
    email_address: str | None = None,
    scopes_granted: list[str] | None = None,
    organization_id: str = "org-1",
) -> GoogleOAuthCredentialBase:
    return GoogleOAuthCredentialBase(
        id=connection_id,
        organization_id=organization_id,
        credential_name=name,
        email_address=email_address,
        state=state,
        scopes_requested=["https://www.googleapis.com/auth/spreadsheets"],
        scopes_granted=scopes_granted or ["https://www.googleapis.com/auth/spreadsheets"],
        created_at=datetime(2026, 8, 15),
        modified_at=datetime(2026, 8, 15),
    )


@pytest.fixture(autouse=True)
def _no_microsoft_connections(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(microsoft_oauth_service, "get_credentials_for_org", AsyncMock(return_value=[]))


def _blocker(
    refs: list[str],
    policy: request_policy_module.RequestPolicy | None,
    *,
    admitted: object = (),
    refs_extra: list[str] = (),
) -> object:
    return _connection_approval_blocker_signal(
        [*refs, *refs_extra],
        policy,
        admitted_ids=admitted,
        action="run the workflow",
        blocked_tool="update_and_run_blocks",
    )


def _connection_refs(workflow_definition: dict[str, object], labels: list[str]) -> list[str]:
    return workflow_connection_refs({"workflow_definition": workflow_definition}, selected_labels=set(labels))


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
        google_oauth_service,
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
        google_oauth_service,
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
async def test_a_microsoft_read_failure_still_verifies_google_choices(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_visible_credentials(monkeypatch, [_google("goac_first", "Google Sheets")])
    monkeypatch.setattr(
        microsoft_oauth_service, "get_credentials_for_org", AsyncMock(side_effect=RuntimeError("graph unavailable"))
    )

    choices = await agent_module._verified_connected_account_choices(
        {"connected_account_choices": [{"connection_id": "goac_first"}]},
        response_type="ASK_QUESTION",
        organization_id="org-1",
    )

    assert [choice.connection_id for choice in choices or []] == ["goac_first"]


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
        google_oauth_service,
        "get_visible_credentials_for_org",
        visible_for_org,
    )
    policy = request_policy_module.RequestPolicy(
        existing_workflow_connection_ids=["goac_model_staged"],
        run_approved_connection_ids=[],
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
        google_oauth_service,
        "get_visible_credentials_for_org",
        lookup,
    )
    policy = request_policy_module.RequestPolicy(
        existing_workflow_connection_ids=["goac_selected"],
        run_approved_connection_ids=["goac_selected"],
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


def test_server_owned_choice_copy_never_invites_prose_or_password_entry() -> None:
    reply = agent_module._connected_account_choice_reply()

    assert reply == (
        "Choose one of the connected accounts below so I can continue. "
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
        google_oauth_service,
        "get_visible_credentials_for_org",
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

    assert policy.run_approved_connection_ids == ["goac_first"]
    assert policy.resolved_credentials == []
    lookup.assert_awaited_once_with("org-1")


@pytest.mark.asyncio
async def test_persisted_active_account_grants_run_authority_but_staged_account_does_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active = _google("goac_saved", "Google Sheets")
    monkeypatch.setattr(
        google_oauth_service,
        "get_visible_credentials_for_org",
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

    assert policy.run_approved_connection_ids == ["goac_saved"]
    assert "goac_staged" not in policy.run_approved_connection_ids
    assert policy.resolved_credentials == []


@pytest.mark.asyncio
async def test_clicked_foreign_or_inactive_account_grants_no_run_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        google_oauth_service,
        "get_visible_credentials_for_org",
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

    assert policy.run_approved_connection_ids == []
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
        google_oauth_service,
        "get_visible_credentials_for_org",
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
    assert picked.run_approved_connection_ids == ["goac_picked"]
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

    assert next_turn.run_approved_connection_ids == ["goac_picked"]


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
SHEETS_BLOCK_LABEL = "read"


def _sheets_workflow_yaml(connection_id: str) -> str:
    return (
        "workflow_definition:\n"
        "  blocks:\n"
        f"    - label: {SHEETS_BLOCK_LABEL}\n"
        "      block_type: google_sheets_read\n"
        f"      credential_id: {connection_id}\n"
    )


def _dispatch_credential_ids(workflow_yaml: str) -> list[str]:
    return _connection_refs(_parsed_workflow_definition(workflow_yaml), [SHEETS_BLOCK_LABEL])


@pytest.mark.asyncio
async def test_editor_bound_account_keeps_run_authority_from_resolution_through_the_dispatch_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        google_oauth_service,
        "get_visible_credentials_for_org",
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

    assert resolved.run_approved_connection_ids == [EDITOR_BOUND_ACCOUNT_ID]

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
    assert next_turn.run_approved_connection_ids == [EDITOR_BOUND_ACCOUNT_ID]
    assert EDITOR_BOUND_ACCOUNT_ID in _approved_run_credential_ids(next_turn)
    assert _credential_run_approval_error(dispatched_ids, next_turn) is None
    assert _blocker(dispatched_ids, next_turn) is None


CONNECTED_GMAIL_ID = "nc_connected_gmail"
CONNECTED_GMAIL_YAML = (
    "workflow_definition:\n"
    "  blocks:\n"
    "    - label: send\n"
    "      block_type: send_email\n"
    "      transport: gmail\n"
    "      credential_source: nango\n"
    f"      connection_id: {CONNECTED_GMAIL_ID}\n"
    "      recipients: [ops@example.com]\n"
    "      subject: Report\n"
    "      body: Done\n"
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("owner", "picked", "approved"),
    [("user-1", False, True), ("user-2", False, False), ("user-1", True, True)],
    ids=["own_saved_binding", "another_users_saved_binding", "own_card_pick"],
)
async def test_connected_gmail_gets_run_authority_only_for_its_owner_and_never_reaches_the_agent_step(
    monkeypatch: pytest.MonkeyPatch, owner: str, picked: bool, approved: bool
) -> None:
    monkeypatch.setattr(google_oauth_service, "get_visible_credentials_for_org", AsyncMock(return_value=[]))
    for module in (request_policy_module, credentials_module):
        monkeypatch.setattr(module, "request_user_id", lambda organization_id: "user-1")

    async def own_connected_gmail(organization_id: str, user_id: str) -> list[OrgConnection]:
        return (
            [OrgConnection(CONNECTED_GMAIL_ID, "nango", "Connected Gmail", None, "active", ())]
            if user_id == owner
            else []
        )

    monkeypatch.setattr(app.AGENT_FUNCTION, "own_connected_gmail_connections", own_connected_gmail)

    policy = await request_policy_module._build_request_policy_bootstrap(
        user_message="run it",
        workflow_yaml=EMPTY_WORKFLOW_YAML if picked else CONNECTED_GMAIL_YAML,
        chat_history=[],
        global_llm_context="",
        organization_id="org-1",
        persisted_workflow_yaml=None if picked else CONNECTED_GMAIL_YAML,
        selected_connected_account_id=CONNECTED_GMAIL_ID if picked else None,
    )
    refs = _connection_refs(_parsed_workflow_definition(CONNECTED_GMAIL_YAML), ["send"])

    assert refs == [CONNECTED_GMAIL_ID]
    assert (policy.run_approved_connection_ids == [CONNECTED_GMAIL_ID]) is approved
    if picked:
        # The model is told what it picked, so it binds the account to the Gmail block's connection_id.
        facts = json.loads(
            connected_account_choice_context(
                None,
                explicit_selected_connection_id=policy.selected_connected_account_id,
                explicit_selected_connection_scopes=policy.selected_connected_account_scopes,
                explicit_selected_connection_provider=policy.selected_connected_account_provider,
            )
        )["selected_connection"]
        assert facts["provider"] == "nango" and facts["scopes_granted"] is None
        # The right binding for the pick dispatches; a wrong slot is left for the test run to report (rule 3).
        ctx = make_copilot_ctx(request_policy=policy)
        send = safe_load_no_dates(CONNECTED_GMAIL_YAML)["workflow_definition"]["blocks"][0]
        right = await run_credential_gate(ctx, {"parameters": [], "blocks": [send]}, labels=None, run_parameters=None)
        assert right.error is None and right.blocker is None
        # A leftover native id on a Connected Gmail send is never read, so it cannot hold the run.
        stale = {**send, "credential_id": "goac_stale"}
        left = await run_credential_gate(ctx, {"parameters": [], "blocks": [stale]}, labels=None, run_parameters=None)
        assert left.error is None and left.blocker is None
    assert (_blocker(refs, policy) is None) is approved
    agent_step_ids, _ = await credentials_module._agent_step_connection_ids(
        "org-1", {}, set(ACCOUNT_TOOL_NAMES), selected_labels=None, run_parameters=None
    )
    assert CONNECTED_GMAIL_ID not in agent_step_ids


@pytest.mark.asyncio
async def test_a_failed_connected_gmail_read_leaves_native_accounts_usable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        google_oauth_service,
        "get_visible_credentials_for_org",
        AsyncMock(return_value=[_google(EDITOR_BOUND_ACCOUNT_ID, "Google Sheets")]),
    )
    monkeypatch.setattr(credentials_module, "request_user_id", lambda organization_id: "user-1")
    monkeypatch.setattr(
        app.AGENT_FUNCTION, "own_connected_gmail_connections", AsyncMock(side_effect=RuntimeError("nango down"))
    )

    # The agent step cannot use a Nango account, so a failed Nango read must not refuse its native accounts.
    agent_step_ids, _ = await credentials_module._agent_step_connection_ids(
        "org-1", {}, set(ACCOUNT_TOOL_NAMES), selected_labels=None, run_parameters=None
    )

    assert agent_step_ids == [EDITOR_BOUND_ACCOUNT_ID]
    # Name resolution still learns the read was incomplete, since a hidden Connected Gmail row could share a name.
    with pytest.raises(IncompleteConnectionRead):
        await org_connections("org-1", "user-1")


@pytest.mark.asyncio
async def test_named_account_with_no_server_owned_choice_selects_nothing_and_stays_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lookup = AsyncMock(return_value=[_google(NAMED_PICK_ACCOUNT_ID, "Sheets Writer", email_address="w@example.test")])
    monkeypatch.setattr(google_oauth_service, "get_visible_credentials_for_org", lookup)
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
    assert policy.run_approved_connection_ids == []
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
        "skyvern.forge.sdk.services.google_oauth_service.get_visible_credentials_for_org",
        AsyncMock(return_value=active_connections),
    )
    policy = request_policy_module.RequestPolicy()

    approved = await admitted_connection_ids(
        [cited_connection_id],
        tool_activity=_listed_integrations(_google(cited_connection_id, "Cited account")),
        organization_id="org-1",
        request_policy=policy,
    )
    blocker = _blocker([cited_connection_id], policy)

    assert approved == set()
    assert policy.run_approved_connection_ids == []
    assert blocker is not None
    assert blocker.blocker_kind == "authority_denied"


@pytest.mark.asyncio
@pytest.mark.parametrize("cited", [True, False], ids=["cited", "listed"])
async def test_an_account_without_the_blocks_scope_is_still_approvable(
    monkeypatch: pytest.MonkeyPatch, cited: bool
) -> None:
    gmail_only = _google("goac_gmail", "Gmail Only", scopes_granted=["https://www.googleapis.com/auth/gmail.readonly"])
    _patch_visible_credentials(monkeypatch, [gmail_only])
    policy = request_policy_module.RequestPolicy(
        canonical_user_message="read the sheet with Gmail Only and test it" if cited else "test it"
    )

    approved = await admitted_connection_ids(
        ["goac_gmail"],
        tool_activity=[] if cited else _listed_integrations(gmail_only),
        organization_id="org-1",
        request_policy=policy,
    )

    assert approved == {"goac_gmail"}
    assert _blocker(["goac_gmail"], policy, admitted=approved) is None


@pytest.mark.asyncio
async def test_model_bound_account_lookup_failure_preserves_authority_denial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "skyvern.forge.sdk.services.google_oauth_service.get_visible_credentials_for_org",
        AsyncMock(side_effect=RuntimeError("database unavailable")),
    )
    policy = request_policy_module.RequestPolicy()

    approved = await admitted_connection_ids(
        [NAMED_PICK_ACCOUNT_ID],
        tool_activity=_listed_integrations(_google(NAMED_PICK_ACCOUNT_ID, "Sheets Writer")),
        organization_id="org-1",
        request_policy=policy,
    )

    assert approved == set()
    assert policy.run_approved_connection_ids == []


@pytest.mark.asyncio
async def test_model_bound_account_without_same_turn_list_result_stays_authority_denied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lookup = AsyncMock(return_value=[_google(NAMED_PICK_ACCOUNT_ID, "Sheets Writer")])
    monkeypatch.setattr(
        "skyvern.forge.sdk.services.google_oauth_service.get_visible_credentials_for_org",
        lookup,
    )
    policy = request_policy_module.RequestPolicy()

    approved = await admitted_connection_ids(
        [NAMED_PICK_ACCOUNT_ID],
        tool_activity=[],
        organization_id="org-1",
        request_policy=policy,
    )

    assert approved == set()
    assert policy.run_approved_connection_ids == []
    lookup.assert_not_awaited()


@pytest.mark.asyncio
async def test_named_account_pick_keeps_run_authority_from_resolution_through_the_dispatch_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        google_oauth_service,
        "get_visible_credentials_for_org",
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

    assert picked.run_approved_connection_ids == [NAMED_PICK_ACCOUNT_ID]

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
    assert next_turn.run_approved_connection_ids == [NAMED_PICK_ACCOUNT_ID]
    assert NAMED_PICK_ACCOUNT_ID in _approved_run_credential_ids(next_turn)
    assert _credential_run_approval_error(dispatched_ids, next_turn) is None
    assert _blocker(dispatched_ids, next_turn) is None


@pytest.mark.asyncio
async def test_editor_bound_account_that_is_no_longer_active_is_refused_at_the_dispatch_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        google_oauth_service,
        "get_visible_credentials_for_org",
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
    assert policy.persisted_workflow_connection_ids == [STALE_ACCOUNT_ID]
    assert policy.run_approved_connection_ids == []
    assert STALE_ACCOUNT_ID not in _approved_run_credential_ids(policy)
    assert _credential_run_approval_error(dispatched_ids, policy) is not None

    blocker = _blocker(dispatched_ids, policy)

    assert blocker is not None
    assert blocker.blocker_kind == "authority_denied"
    assert blocker.internal_reason_code == UNAPPROVED_CONNECTION_REASON_CODE


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
        "skyvern.forge.sdk.services.google_oauth_service.get_visible_credentials_for_org",
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
        "skyvern.forge.sdk.services.google_oauth_service.get_visible_credentials_for_org",
        AsyncMock(return_value=accounts),
    )
    draft = _named_sheets_yaml(citation)
    ctx = _canonicalization_ctx(draft, f'write the rows with the "{citation}" account')

    canonical_yaml, facts = await canonicalize_named_connection_bindings(draft, ctx)

    assert [(fact["status"], fact["canonicalized"], fact["connection_id"]) for fact in facts] == [
        ("resolved", True, CITED_ACCOUNT_ID)
    ]
    dispatched_ids = _dispatch_credential_ids(canonical_yaml)
    assert dispatched_ids == [CITED_ACCOUNT_ID]

    policy = ctx.request_policy
    approved = await admitted_connection_ids(
        [CITED_ACCOUNT_ID],
        tool_activity=[],
        organization_id="org-1",
        request_policy=policy,
    )

    assert approved == {CITED_ACCOUNT_ID}
    assert (
        _blocker(
            dispatched_ids,
            policy,
            admitted=approved,
            refs_extra=_connection_refs(_parsed_workflow_definition(canonical_yaml), [SHEETS_BLOCK_LABEL]),
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
        "skyvern.forge.sdk.services.google_oauth_service.get_visible_credentials_for_org",
        AsyncMock(return_value=accounts),
    )
    draft = _named_sheets_yaml("Shared Sheets Account")
    ctx = _canonicalization_ctx(draft, 'use the "Shared Sheets Account" connection')

    canonical_yaml, facts = await canonicalize_named_connection_bindings(draft, ctx)

    assert canonical_yaml == draft
    assert facts[0]["status"] == "ambiguous"
    assert facts[0]["detail"]
    assert [candidate["connection_id"] for candidate in facts[0]["candidates"]] == [
        AMBIGUOUS_ACCOUNT_ID,
        "goac_ambiguous_twin",
    ]

    policy = ctx.request_policy
    model_bound = await admitted_connection_ids(
        [AMBIGUOUS_ACCOUNT_ID],
        tool_activity=[],
        organization_id="org-1",
        request_policy=policy,
    )

    assert model_bound == set()

    blocker = _blocker(
        [AMBIGUOUS_ACCOUNT_ID],
        policy,
        admitted=model_bound,
    )

    assert blocker is not None
    assert blocker.blocker_kind == "authority_denied"
    assert blocker.preserves_workflow_draft

    name_in_slot_blocker = _blocker(
        _dispatch_credential_ids(canonical_yaml),
        policy,
        admitted=model_bound,
        refs_extra=_connection_refs(_parsed_workflow_definition(canonical_yaml), [SHEETS_BLOCK_LABEL]),
    )

    assert name_in_slot_blocker is not None
    assert name_in_slot_blocker.blocker_kind == "authority_denied"
    assert name_in_slot_blocker.preserves_workflow_draft


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reference", "user_message", "expected_status"),
    [
        ("Missing Reporting Connection", 'bind the "Missing Reporting Connection" account', "not_found"),
        ("Dead Sheets Account", 'bind the "Dead Sheets Account" account', "inactive"),
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

    canonical_yaml, facts = await canonicalize_named_connection_bindings(draft, ctx)

    assert canonical_yaml == draft
    assert facts[0]["status"] == expected_status
    assert facts[0]["canonicalized"] is False
    assert facts[0]["detail"]
    assert [row["connection_id"] for row in facts[0]["active_connections"]] == [CITED_ACCOUNT_ID]

    blocker = _blocker(
        _dispatch_credential_ids(canonical_yaml),
        ctx.request_policy,
        refs_extra=_connection_refs(_parsed_workflow_definition(canonical_yaml), [SHEETS_BLOCK_LABEL]),
    )

    assert blocker is not None
    assert blocker.blocker_kind == "authority_denied"


@pytest.mark.asyncio
async def test_a_saved_credential_id_in_an_account_field_is_gated_as_a_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_visible_credentials(monkeypatch, [_google(CITED_ACCOUNT_ID, CITED_ACCOUNT_NAME)])
    draft = _named_sheets_yaml("cred_qablogmetrics")
    ctx = _canonicalization_ctx(draft, "use cred_qablogmetrics for the sheet")

    canonical_yaml, facts = await canonicalize_named_connection_bindings(draft, ctx)

    assert canonical_yaml == draft
    assert [(fact["status"], fact["canonicalized"]) for fact in facts] == [("not_found", False)]
    refs = _dispatch_credential_ids(canonical_yaml)
    assert refs == ["cred_qablogmetrics"]
    assert _blocker(refs, ctx.request_policy) is not None


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
async def test_templated_slot_that_resolves_to_nothing_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    slot_value: str,
) -> None:
    _patch_visible_credentials(monkeypatch, [_google(CITED_ACCOUNT_ID, CITED_ACCOUNT_NAME)])
    draft = _named_sheets_yaml(slot_value)
    ctx = _canonicalization_ctx(draft, f"use {slot_value} for the sheet")

    canonical_yaml, facts = await canonicalize_named_connection_bindings(draft, ctx)

    assert canonical_yaml == draft
    assert facts == []
    refs = _dispatch_credential_ids(canonical_yaml)
    assert refs == [slot_value]
    blocker = _blocker(refs, ctx.request_policy)
    assert blocker is not None
    assert blocker.blocker_kind == "authority_denied"


@pytest.mark.parametrize(
    ("parameter", "run_parameters", "slot_value", "expected_refs"),
    [
        ({"parameter_type": "credential", "credential_id": "goac_param"}, None, "{{ conn }}", ["goac_param"]),
        # A string default can be replaced by the page or the run being repaired, so it does not resolve.
        ({"parameter_type": "workflow", "default_value": "moac_default"}, None, "{{conn}}", ["{{conn}}"]),
        (
            {"parameter_type": "workflow", "default_value": "moac_default"},
            {"conn": "moac_run"},
            "{{ conn }}",
            ["moac_run"],
        ),
        ({"parameter_type": "workflow"}, None, "{{ conn }}", ["{{ conn }}"]),
        ({"parameter_type": "workflow", "default_value": "goac_x"}, None, "goac_{{ conn }}", ["goac_{{ conn }}"]),
    ],
)
def test_a_bare_template_resolves_through_the_run_value_or_a_credential_parameter(
    parameter: dict[str, str],
    run_parameters: dict[str, str] | None,
    slot_value: str,
    expected_refs: list[str],
) -> None:
    parsed = {
        "workflow_definition": {
            "parameters": [{"key": "conn", **parameter}],
            "blocks": [
                {"label": "read", "block_type": "email_inbox", "email_client": "outlook", "credential_id": slot_value}
            ],
        }
    }

    assert workflow_connection_refs(parsed, run_parameters=run_parameters) == expected_refs


@pytest.mark.asyncio
async def test_connection_lookup_failure_keeps_the_draft_and_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_visible_credentials(monkeypatch, RuntimeError("database unavailable"))
    draft = _named_sheets_yaml(CITED_ACCOUNT_NAME)
    ctx = _canonicalization_ctx(draft, f'use the "{CITED_ACCOUNT_NAME}" account')

    canonical_yaml, facts = await canonicalize_named_connection_bindings(draft, ctx)

    assert canonical_yaml == draft
    assert facts[0]["status"] == "lookup_failed"
    assert facts[0]["detail"]

    blocker = _blocker(
        _dispatch_credential_ids(canonical_yaml),
        ctx.request_policy,
        refs_extra=_connection_refs(_parsed_workflow_definition(canonical_yaml), [SHEETS_BLOCK_LABEL]),
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
        "skyvern.forge.sdk.services.google_oauth_service.get_visible_credentials_for_org",
        AsyncMock(return_value=accounts),
    )

    approved = await admitted_connection_ids(
        [CITED_ACCOUNT_ID],
        tool_activity=[],
        organization_id="org-1",
        request_policy=request_policy_module.RequestPolicy(
            canonical_user_message='use the "Reporting Account" connection'
        ),
    )

    assert approved == set()


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

    canonical_yaml, facts = await canonicalize_named_connection_bindings(draft, ctx)

    assert canonical_yaml == draft
    assert [(fact["status"], fact["canonicalized"]) for fact in facts] == [("not_cited", False)]


@pytest.mark.asyncio
@pytest.mark.parametrize("longer_sibling_state", ["active", "error"])
async def test_name_contained_in_a_longer_sibling_name_admits_nothing_at_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    longer_sibling_state: str,
) -> None:
    monkeypatch.setattr(
        "skyvern.forge.sdk.services.google_oauth_service.get_visible_credentials_for_org",
        AsyncMock(return_value=_sibling_named_accounts(longer_sibling_state)),
    )

    approved = await admitted_connection_ids(
        ["goac_marketing"],
        tool_activity=[],
        organization_id="org-1",
        request_policy=request_policy_module.RequestPolicy(
            canonical_user_message="please read rows from the Marketing Archive connection"
        ),
    )

    assert approved == set()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state,scopes,organization_id",
    [
        ("revoked", ["https://www.googleapis.com/auth/spreadsheets"], "org-1"),
        ("active", ["https://www.googleapis.com/auth/spreadsheets"], "org-other"),
    ],
)
async def test_picker_selection_rejects_unusable_account_before_persisting_approval(
    monkeypatch, state, scopes, organization_id
):
    account = _google("goac_new", "New account", state=state, scopes_granted=scopes)
    account.organization_id = organization_id
    monkeypatch.setattr(google_oauth_service, "get_visible_credentials_for_org", AsyncMock(return_value=[account]))
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
    assert policy.run_approved_connection_ids == []


PROPOSAL_BOUND_ACCOUNT_ID = "goac_proposal_bound"
SECOND_PROPOSAL_ACCOUNT_ID = "goac_proposal_second"
UNBOUND_ACCOUNT_ID = "goac_never_bound"
NO_SHEETS_SCOPE = ["https://www.googleapis.com/auth/drive.file"]


def _multi_bound_proposal_yaml(first_connection_id: str, second_connection_id: str) -> str:
    return (
        "workflow_definition:\n"
        "  blocks:\n"
        f"    - label: {SHEETS_BLOCK_LABEL}\n"
        "      block_type: google_sheets_write\n"
        f"      credential_id: {first_connection_id}\n"
        "    - label: write_again\n"
        "      block_type: google_sheets_write\n"
        f"      credential_id: {second_connection_id}\n"
    )


async def _test_end_to_end_run_authority(
    monkeypatch: pytest.MonkeyPatch,
    *,
    pending_proposal_yaml: str,
    connections: list[GoogleOAuthCredentialBase],
    submitted_workflow_yaml: str = EMPTY_WORKFLOW_YAML,
    persisted_workflow_yaml: str | None = None,
) -> tuple[WorkflowCopilotChatRequest, request_policy_module.RequestPolicy]:
    monkeypatch.setattr(
        google_oauth_service,
        "get_visible_credentials_for_org",
        AsyncMock(return_value=connections),
    )
    chat_request = WorkflowCopilotChatRequest(
        workflow_permanent_id="wpid_test",
        workflow_id="wf_test",
        message="user prose the action replaces",
        workflow_yaml=submitted_workflow_yaml,
        product_action="test_end_to_end",
    )
    workflow_copilot_route._apply_test_end_to_end_action(chat_request, pending_proposal_yaml)
    policy = await request_policy_module._build_request_policy_bootstrap(
        user_message=chat_request.message,
        workflow_yaml=chat_request.workflow_yaml,
        chat_history=[],
        global_llm_context="",
        organization_id="org-1",
        persisted_workflow_yaml=persisted_workflow_yaml,
        selected_connected_account_id=chat_request.selected_connected_account_id,
        selected_connected_account_from_pending_proposal=chat_request.selected_connected_account_from_pending_proposal,
    )
    return chat_request, policy


@pytest.mark.asyncio
async def test_test_end_to_end_supplies_the_proposal_bound_account_to_unchanged_run_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A never-saved copilot build contributes nothing to persisted run authority, so without the
    account its own pending proposal binds the picker re-fires on the press and nothing dispatches."""
    active = _google(PROPOSAL_BOUND_ACCOUNT_ID, "Google Sheets")
    monkeypatch.setattr(
        google_oauth_service,
        "get_visible_credentials_for_org",
        AsyncMock(return_value=[active]),
    )
    pending_proposal_yaml = _sheets_workflow_yaml(PROPOSAL_BOUND_ACCOUNT_ID)

    unsupplied = await request_policy_module._build_request_policy_bootstrap(
        user_message="run it",
        workflow_yaml=pending_proposal_yaml,
        chat_history=[],
        global_llm_context="",
        organization_id="org-1",
        persisted_workflow_yaml=None,
        selected_connected_account_id=None,
    )
    assert unsupplied.run_approved_connection_ids == []

    chat_request, policy = await _test_end_to_end_run_authority(
        monkeypatch,
        pending_proposal_yaml=pending_proposal_yaml,
        connections=[active],
    )

    assert chat_request.selected_connected_account_id == PROPOSAL_BOUND_ACCOUNT_ID
    assert chat_request.workflow_yaml == pending_proposal_yaml
    assert policy.selected_connected_account_id == PROPOSAL_BOUND_ACCOUNT_ID
    assert policy.run_approved_connection_ids == [PROPOSAL_BOUND_ACCOUNT_ID]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state,scopes,organization_id",
    [
        ("revoked", ["https://www.googleapis.com/auth/spreadsheets"], "org-1"),
        ("active", ["https://www.googleapis.com/auth/spreadsheets"], "org-2"),
    ],
    ids=["inactive", "foreign_org"],
)
async def test_test_end_to_end_refuses_an_unusable_proposal_bound_account(
    monkeypatch: pytest.MonkeyPatch,
    state: str,
    scopes: list[str],
    organization_id: str,
) -> None:
    unusable = _google(
        PROPOSAL_BOUND_ACCOUNT_ID,
        "Google Sheets",
        state=state,
        scopes_granted=scopes,
        organization_id=organization_id,
    )

    chat_request, policy = await _test_end_to_end_run_authority(
        monkeypatch,
        pending_proposal_yaml=_sheets_workflow_yaml(PROPOSAL_BOUND_ACCOUNT_ID),
        connections=[unusable],
    )

    assert chat_request.selected_connected_account_id == PROPOSAL_BOUND_ACCOUNT_ID
    assert policy.selected_connected_account_id is None
    assert policy.run_approved_connection_ids == []


@pytest.mark.asyncio
async def test_test_end_to_end_refuses_an_account_the_pending_proposal_does_not_bind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unbound_but_valid = _google(UNBOUND_ACCOUNT_ID, "Google Sheets")

    chat_request, policy = await _test_end_to_end_run_authority(
        monkeypatch,
        pending_proposal_yaml=EMPTY_WORKFLOW_YAML,
        connections=[unbound_but_valid],
    )

    assert chat_request.selected_connected_account_id is None
    assert policy.selected_connected_account_id is None
    assert policy.run_approved_connection_ids == []


@pytest.mark.asyncio
async def test_test_end_to_end_refuses_a_binding_that_exists_only_on_the_live_canvas(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unaccepted canvas binding must never become durable run authority."""
    canvas_only = _google(PROPOSAL_BOUND_ACCOUNT_ID, "Google Sheets")

    chat_request, policy = await _test_end_to_end_run_authority(
        monkeypatch,
        pending_proposal_yaml=EMPTY_WORKFLOW_YAML,
        connections=[canvas_only],
        submitted_workflow_yaml=_sheets_workflow_yaml(PROPOSAL_BOUND_ACCOUNT_ID),
    )

    assert chat_request.selected_connected_account_id is None
    assert policy.selected_connected_account_id is None
    assert policy.run_approved_connection_ids == []


@pytest.mark.asyncio
async def test_test_end_to_end_leaves_a_multi_account_proposal_at_the_picker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _google(PROPOSAL_BOUND_ACCOUNT_ID, "Google Sheets")
    second = _google(SECOND_PROPOSAL_ACCOUNT_ID, "Other Sheets")

    chat_request, policy = await _test_end_to_end_run_authority(
        monkeypatch,
        pending_proposal_yaml=_multi_bound_proposal_yaml(PROPOSAL_BOUND_ACCOUNT_ID, SECOND_PROPOSAL_ACCOUNT_ID),
        connections=[first, second],
    )

    assert chat_request.selected_connected_account_id is None
    assert policy.selected_connected_account_id is None
    assert policy.run_approved_connection_ids == []


def test_test_end_to_end_never_overwrites_an_explicit_picker_selection() -> None:
    chat_request = WorkflowCopilotChatRequest(
        workflow_permanent_id="wpid_test",
        workflow_id="wf_test",
        message="run it",
        workflow_yaml=EMPTY_WORKFLOW_YAML,
        product_action="test_end_to_end",
        selected_connected_account_id=UNBOUND_ACCOUNT_ID,
    )

    workflow_copilot_route._apply_test_end_to_end_action(chat_request, _sheets_workflow_yaml(PROPOSAL_BOUND_ACCOUNT_ID))

    assert chat_request.selected_connected_account_id == UNBOUND_ACCOUNT_ID


@pytest.mark.asyncio
async def test_test_end_to_end_approval_carries_to_the_next_turn_and_dies_when_disconnected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active = _google(PROPOSAL_BOUND_ACCOUNT_ID, "Google Sheets")
    _chat_request, granted = await _test_end_to_end_run_authority(
        monkeypatch,
        pending_proposal_yaml=_sheets_workflow_yaml(PROPOSAL_BOUND_ACCOUNT_ID),
        connections=[active],
    )
    assert granted.run_approved_connection_ids == [PROPOSAL_BOUND_ACCOUNT_ID]

    carried = record_approved_credentials_in_global_llm_context(
        SimpleNamespace(request_policy=granted, credential_pause_connected_credential_id=None),
        "",
    )
    assert carried is not None
    assert PROPOSAL_BOUND_ACCOUNT_ID in carried

    next_turn = await request_policy_module._build_request_policy_bootstrap(
        user_message="run the workflow now",
        workflow_yaml=EMPTY_WORKFLOW_YAML,
        chat_history=[],
        global_llm_context=carried,
        organization_id="org-1",
        persisted_workflow_yaml=None,
    )
    assert next_turn.run_approved_connection_ids == [PROPOSAL_BOUND_ACCOUNT_ID]

    monkeypatch.setattr(
        google_oauth_service,
        "get_visible_credentials_for_org",
        AsyncMock(return_value=[]),
    )
    after_disconnect = await request_policy_module._build_request_policy_bootstrap(
        user_message="run the workflow now",
        workflow_yaml=EMPTY_WORKFLOW_YAML,
        chat_history=[],
        global_llm_context=carried,
        organization_id="org-1",
        persisted_workflow_yaml=None,
    )
    assert after_disconnect.run_approved_connection_ids == []


@pytest.mark.asyncio
@pytest.mark.parametrize("saved_workflow_binds_it", [False, True])
async def test_a_test_end_to_end_pick_never_admits_a_send_but_a_card_pick_does(
    monkeypatch: pytest.MonkeyPatch, saved_workflow_binds_it: bool
) -> None:
    proposal = _sheets_workflow_yaml(PROPOSAL_BOUND_ACCOUNT_ID)
    _chat_request, derived = await _test_end_to_end_run_authority(
        monkeypatch,
        pending_proposal_yaml=proposal,
        connections=[_google(PROPOSAL_BOUND_ACCOUNT_ID, "Google Sheets")],
        persisted_workflow_yaml=proposal if saved_workflow_binds_it else None,
    )
    carried = record_approved_credentials_in_global_llm_context(
        SimpleNamespace(request_policy=derived, credential_pause_connected_credential_id=None), ""
    )
    send = {
        "label": "send",
        "block_type": "send_email",
        "transport": "gmail",
        "credential_id": PROPOSAL_BOUND_ACCOUNT_ID,
    }

    async def send_blocker(policy: request_policy_module.RequestPolicy) -> object:
        ctx = make_copilot_ctx(request_policy=policy)
        return (await run_credential_gate(ctx, {"blocks": [send]}, labels=None, run_parameters=None)).blocker

    async def later_turn(picked: str | None) -> request_policy_module.RequestPolicy:
        return await request_policy_module._build_request_policy_bootstrap(
            user_message="now email me the results",
            workflow_yaml=EMPTY_WORKFLOW_YAML,
            chat_history=[],
            global_llm_context=carried,
            organization_id="org-1",
            selected_connected_account_id=picked,
        )

    # While the saved workflow binds the account it is fully approved; later_turn has no saved binding.
    assert (await send_blocker(derived) is None) is saved_workflow_binds_it
    assert await send_blocker(await later_turn(None)) is not None
    assert await send_blocker(await later_turn(PROPOSAL_BOUND_ACCOUNT_ID)) is None


def test_test_end_to_end_derived_account_is_labelled_as_a_pending_proposal_binding() -> None:
    chat_request = WorkflowCopilotChatRequest(
        workflow_permanent_id="wpid_test",
        workflow_id="wf_test",
        message="run it",
        workflow_yaml=EMPTY_WORKFLOW_YAML,
        product_action="test_end_to_end",
    )
    workflow_copilot_route._apply_test_end_to_end_action(chat_request, _sheets_workflow_yaml(PROPOSAL_BOUND_ACCOUNT_ID))

    context = connected_account_choice_context(
        None,
        explicit_selected_connection_id=chat_request.selected_connected_account_id,
        from_pending_proposal=chat_request.selected_connected_account_from_pending_proposal,
    )

    assert '"selection_source":"pending_proposal_binding"' in context
    assert PROPOSAL_BOUND_ACCOUNT_ID in context


def test_test_end_to_end_does_not_pick_an_account_that_sends() -> None:
    chat_request = WorkflowCopilotChatRequest(
        workflow_permanent_id="wpid_test",
        workflow_id="wf_test",
        message="run it",
        workflow_yaml=EMPTY_WORKFLOW_YAML,
        product_action="test_end_to_end",
    )
    sending_yaml = (
        "workflow_definition:\n"
        "  blocks:\n"
        "    - label: notify\n"
        "      block_type: send_email\n"
        "      transport: gmail\n"
        f"      credential_id: {PROPOSAL_BOUND_ACCOUNT_ID}\n"
    )
    workflow_copilot_route._apply_test_end_to_end_action(chat_request, sending_yaml)

    assert chat_request.selected_connected_account_id is None
    assert chat_request.selected_connected_account_from_pending_proposal is False


def test_test_end_to_end_explicit_client_pick_is_still_labelled_as_a_user_picker_selection() -> None:
    chat_request = WorkflowCopilotChatRequest(
        workflow_permanent_id="wpid_test",
        workflow_id="wf_test",
        message="run it",
        workflow_yaml=EMPTY_WORKFLOW_YAML,
        product_action="test_end_to_end",
        selected_connected_account_id=UNBOUND_ACCOUNT_ID,
    )
    workflow_copilot_route._apply_test_end_to_end_action(chat_request, _sheets_workflow_yaml(PROPOSAL_BOUND_ACCOUNT_ID))

    context = connected_account_choice_context(
        None,
        explicit_selected_connection_id=chat_request.selected_connected_account_id,
        from_pending_proposal=chat_request.selected_connected_account_from_pending_proposal,
    )

    assert chat_request.selected_connected_account_id == UNBOUND_ACCOUNT_ID
    assert '"selection_source":"user_picker"' in context


def _ctx_with_denial(policy: request_policy_module.RequestPolicy, refs: list[str]) -> tuple[CopilotContext, object]:
    ctx = make_copilot_ctx(request_policy=policy)
    signal = _blocker(refs, policy)
    assert signal is not None
    ctx.blocker_signal = signal
    ctx.latest_tool_blocker_signal = signal
    ctx.tool_blocker_signals = [signal]
    return ctx, signal


@pytest.mark.parametrize("saved_ref", ["Team Sheet", "goac_saved"])
def test_a_saved_binding_still_unapproved_holds_the_denial_open(saved_ref: str) -> None:
    policy = request_policy_module.RequestPolicy()
    policy.persisted_workflow_connection_ids = [saved_ref]
    ctx, signal = _ctx_with_denial(policy, [saved_ref])

    _retire_stale_connection_denial(ctx, workflow_refs=["goac_staged"], admitted_ids={"goac_staged"})

    assert ctx.blocker_signal is signal


def test_the_denial_trace_carries_a_count_and_no_references() -> None:
    signal = _blocker(["goac_plain", "goac_{{ suffix }}"], request_policy_module.RequestPolicy())

    assert signal is not None
    assert signal.extra == {"unapproved_connection_reference_count": 2}


def test_retiring_a_stale_denial_never_widens_the_run_approved_ids() -> None:
    policy = request_policy_module.RequestPolicy()
    ctx, _ = _ctx_with_denial(policy, ["goac_sheet"])
    ctx.connected_account_recovery_choices = [
        ConnectedAccountChoice(connection_id="goac_sheet", name="Sheets", state="active")
    ]

    _retire_stale_connection_denial(ctx, workflow_refs=["goac_sheet"], admitted_ids={"goac_sheet"})

    assert ctx.blocker_signal is None
    assert ctx.connected_account_recovery_choices == []
    assert policy.run_approved_connection_ids == []
    assert _approved_run_credential_ids(policy) == set()


SPREADSHEET_ID = "1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"
ENGINEERING_GID = 1226505961
RECORDED_TABS_PAYLOAD = {
    "sheets": [
        {"properties": {"sheetId": 0, "title": "Sheet1", "index": 0}},
        {"properties": {"sheetId": ENGINEERING_GID, "title": "Engineering", "index": 1}},
    ]
}


def _gid_sheets_yaml(block_type: str, url: str, extra: str = "") -> str:
    return (
        "workflow_definition:\n"
        "  blocks:\n"
        "    - label: engineering_rows\n"
        f"      block_type: {block_type}\n"
        f"      spreadsheet_url: {url}\n"
        f"      credential_id: {CITED_ACCOUNT_ID}\n"
        f"{extra}"
    )


def _gid_turn_policy() -> request_policy_module.RequestPolicy:
    return request_policy_module.RequestPolicy(
        canonical_user_message=f"append the total to the sheet using the {CITED_ACCOUNT_NAME}"
    )


def _saved_sheets_block(workflow_yaml: str) -> dict[str, object]:
    return safe_load_no_dates(workflow_yaml)["workflow_definition"]["blocks"][0]


@pytest.fixture
def recorded_sheets_tabs(
    monkeypatch: pytest.MonkeyPatch,
    mock_sheets_transport: Callable[[Callable[[httpx.Request], httpx.Response]], None],
) -> list[httpx.Request]:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=RECORDED_TABS_PAYLOAD)

    mock_sheets_transport(handler)
    connections = [_google(CITED_ACCOUNT_ID, CITED_ACCOUNT_NAME)]
    _patch_visible_credentials(monkeypatch, connections)
    monkeypatch.setattr(
        "skyvern.forge.sdk.services.google_oauth_service.get_credentials_for_org",
        AsyncMock(return_value=connections),
    )
    monkeypatch.setattr(app.AGENT_FUNCTION, "get_google_sheets_credentials", AsyncMock(return_value="sheets-token"))
    return requests


@pytest.mark.asyncio
@pytest.mark.parametrize("block_type", ["google_sheets_read", "google_sheets_write"])
@pytest.mark.parametrize("gid_suffix", [f"/edit#gid={ENGINEERING_GID}", f"/edit?gid={ENGINEERING_GID}"])
async def test_update_workflow_saves_the_tab_named_by_the_url_gid(
    monkeypatch: pytest.MonkeyPatch,
    recorded_sheets_tabs: list[httpx.Request],
    block_type: str,
    gid_suffix: str,
) -> None:
    ctx = make_copilot_ctx(request_policy=_gid_turn_policy())
    process_workflow_yaml = AsyncMock(return_value=MagicMock())
    monkeypatch.setattr("skyvern.forge.sdk.copilot.tools.workflow_update._process_workflow_yaml", process_workflow_yaml)
    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.tools.app.WORKFLOW_SERVICE",
        SimpleNamespace(update_workflow_definition=AsyncMock()),
    )
    url = f"https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}{gid_suffix}"

    result = await tools_module._update_workflow(
        {"workflow_yaml": _gid_sheets_yaml(block_type, url, "      range: D:D\n")}, ctx
    )

    assert result["ok"] is True, result
    assert [(fact["status"], fact.get("sheet_name")) for fact in result["data"]["google_sheet_tab_resolution"]] == [
        ("resolved", "Engineering")
    ]
    saved = _saved_sheets_block(process_workflow_yaml.await_args.kwargs["workflow_yaml"])
    assert (saved["sheet_name"], saved["range"], saved["spreadsheet_url"]) == ("Engineering", "D:D", url)
    assert _saved_sheets_block(ctx.staged_workflow_yaml)["sheet_name"] == "Engineering"
    assert [request.url.path for request in recorded_sheets_tabs] == [f"/v4/spreadsheets/{SPREADSHEET_ID}"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("gid", "extra", "expected_fact", "expected_sheet_name"),
    [
        (ENGINEERING_GID, "      sheet_name: Summary\n", None, "Summary"),
        (
            987654321,
            "",
            {"status": "gid_not_found", "available_tabs": ["Sheet1", "Engineering"]},
            None,
        ),
        (
            ENGINEERING_GID,
            "      range: \"'Sheet1'!D:D\"\n",
            {"status": "ineligible", "reason": "range_not_plain_cells"},
            None,
        ),
        (
            ENGINEERING_GID,
            '      range: "{{ destination_range }}"\n',
            {"status": "ineligible", "reason": "range_not_plain_cells"},
            None,
        ),
        (
            ENGINEERING_GID,
            "      range: MyNamedRange\n",
            {"status": "ineligible", "reason": "range_not_plain_cells"},
            None,
        ),
        (
            ENGINEERING_GID,
            "      range: TAX\n",
            {"status": "ineligible", "reason": "range_not_plain_cells"},
            None,
        ),
    ],
    ids=[
        "explicit_sheet_name",
        "gid_absent_from_tabs",
        "range_names_a_sheet",
        "templated_range",
        "named_range",
        "short_uppercase_named_range",
    ],
)
async def test_gid_resolution_never_overwrites_or_guesses_a_tab(
    recorded_sheets_tabs: list[httpx.Request],
    gid: int,
    extra: str,
    expected_fact: dict[str, object] | None,
    expected_sheet_name: str | None,
) -> None:
    url = f"https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}/edit#gid={gid}"
    draft = _gid_sheets_yaml("google_sheets_write", url, extra)

    resolved_yaml, facts = await resolve_google_sheet_tabs_from_gid(
        draft, make_copilot_ctx(workflow_yaml=draft, request_policy=_gid_turn_policy())
    )

    assert resolved_yaml == draft
    assert _saved_sheets_block(resolved_yaml).get("sheet_name") == expected_sheet_name
    if expected_fact is None:
        assert facts == []
    else:
        assert [{key: fact.get(key) for key in expected_fact} for fact in facts] == [expected_fact]
        assert facts[0]["resolved"] is False


@pytest.mark.asyncio
async def test_gid_resolution_reads_no_tabs_through_a_connection_this_turn_did_not_approve(
    recorded_sheets_tabs: list[httpx.Request],
) -> None:
    url = f"https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}/edit#gid={ENGINEERING_GID}"
    draft = _gid_sheets_yaml("google_sheets_write", url)
    ctx = make_copilot_ctx(
        workflow_yaml=draft,
        request_policy=request_policy_module.RequestPolicy(canonical_user_message="append the total to the sheet"),
    )

    resolved_yaml, facts = await resolve_google_sheet_tabs_from_gid(draft, ctx)

    assert resolved_yaml == draft
    assert [(fact["status"], fact.get("reason"), "available_tabs" in fact) for fact in facts] == [
        ("ineligible", "connection_not_approved", False)
    ]
    assert recorded_sheets_tabs == []


@pytest.mark.asyncio
async def test_gid_resolution_reads_tabs_through_an_approved_google_account_when_microsoft_fails(
    monkeypatch: pytest.MonkeyPatch, recorded_sheets_tabs: list[httpx.Request]
) -> None:
    monkeypatch.setattr(
        microsoft_oauth_service, "get_credentials_for_org", AsyncMock(side_effect=RuntimeError("graph unavailable"))
    )
    url = f"https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}/edit#gid={ENGINEERING_GID}"
    draft = _gid_sheets_yaml("google_sheets_write", url)
    policy = request_policy_module.RequestPolicy(run_approved_connection_ids=[CITED_ACCOUNT_ID])

    resolved_yaml, facts = await resolve_google_sheet_tabs_from_gid(
        draft, make_copilot_ctx(workflow_yaml=draft, request_policy=policy)
    )

    assert _saved_sheets_block(resolved_yaml)["sheet_name"] == "Engineering"
    assert [fact["status"] for fact in facts] == ["resolved"]


@pytest.mark.asyncio
async def test_gid_resolution_bounds_a_hung_tab_lookup_once_per_spreadsheet(
    monkeypatch: pytest.MonkeyPatch,
    recorded_sheets_tabs: list[httpx.Request],
) -> None:
    lookups: list[str] = []

    async def hung_token(organization_id: str, credential_id: str) -> str:
        lookups.append(credential_id)
        await asyncio.Event().wait()
        return "never"

    monkeypatch.setattr(app.AGENT_FUNCTION, "get_google_sheets_credentials", hung_token)
    monkeypatch.setattr("skyvern.forge.sdk.copilot.tools.credentials._GID_TAB_LOOKUP_TIMEOUT_SECONDS", 0.05)
    url = f"https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}/edit#gid={ENGINEERING_GID}"
    block = _gid_sheets_yaml("google_sheets_write", url).split("  blocks:\n", 1)[1]
    draft = "workflow_definition:\n  blocks:\n" + block + block.replace("engineering_rows", "engineering_copy")

    _, facts = await asyncio.wait_for(
        resolve_google_sheet_tabs_from_gid(
            draft, make_copilot_ctx(workflow_yaml=draft, request_policy=_gid_turn_policy())
        ),
        timeout=2,
    )

    assert [fact["status"] for fact in facts] == ["lookup_failed", "lookup_failed"]
    assert lookups == [CITED_ACCOUNT_ID]


OPENING_ACCOUNT_ID = "goac_opens_sheet"
DENIED_ACCOUNT_ID = "goac_cannot_open_sheet"
OPENING_ACCOUNT_NAME = "Ops Sheets"
READ_SHEET_URL = f"https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}/edit#gid={ENGINEERING_GID}"
READ_SHEET_METADATA = {
    "properties": {"title": "Fleet metrics"},
    "sheets": [
        {
            "properties": {
                "sheetId": ENGINEERING_GID,
                "title": "Engineering",
                "gridProperties": {"rowCount": 20, "columnCount": 4},
            }
        }
    ],
}


@pytest_asyncio.fixture
async def ctx_after_read_google_sheet(
    monkeypatch: pytest.MonkeyPatch,
    mock_sheets_transport: Callable[[Callable[[httpx.Request], httpx.Response]], None],
) -> CopilotContext:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers["Authorization"] != f"Bearer token-{OPENING_ACCOUNT_ID}":
            return httpx.Response(404, json={"error": {"code": 404, "message": "Requested entity was not found."}})
        if "ranges" in request.url.params:
            return httpx.Response(200, json={"sheets": [{"data": [{"rowData": []}]}]})
        return httpx.Response(200, json=READ_SHEET_METADATA)

    async def mint(organization_id: str, connection_id: str) -> str:
        return f"token-{connection_id}"

    mock_sheets_transport(handler)
    _patch_visible_credentials(
        monkeypatch,
        [_google(OPENING_ACCOUNT_ID, OPENING_ACCOUNT_NAME), _google(DENIED_ACCOUNT_ID, "Personal Sheets")],
    )
    monkeypatch.setattr(app.AGENT_FUNCTION, "get_google_sheets_credentials", mint)
    user_message = f"read Sessions Started from https://example.com/dashboard and update {READ_SHEET_URL}"
    policy = request_policy_module.RequestPolicy(canonical_user_message=user_message)
    request_policy_module._ground_user_provided_sites(policy, user_message, [])
    ctx = make_copilot_ctx(request_policy=policy)

    result = await _read_google_sheet({"spreadsheet_url": READ_SHEET_URL}, ctx)
    tool = SimpleNamespace(name="read_google_sheet")
    await CopilotRunHooks(ctx).on_tool_end(
        MagicMock(), MagicMock(), tool, json.dumps(sanitize_tool_result_for_llm("read_google_sheet", result))
    )
    return ctx


async def _approved_after_tool_activity(ctx: CopilotContext, credential_id: str) -> set[str]:
    assert isinstance(ctx.request_policy, request_policy_module.RequestPolicy)
    return await admitted_connection_ids(
        [credential_id],
        tool_activity=ctx.tool_activity,
        organization_id="org-1",
        request_policy=ctx.request_policy,
    )


@pytest.mark.asyncio
async def test_connection_that_opened_the_sheet_is_run_approved_in_the_same_turn(
    ctx_after_read_google_sheet: CopilotContext,
) -> None:
    ctx = ctx_after_read_google_sheet

    approved = await _approved_after_tool_activity(ctx, OPENING_ACCOUNT_ID)

    assert approved == {OPENING_ACCOUNT_ID}
    assert _blocker([OPENING_ACCOUNT_ID], ctx.request_policy, admitted=approved) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("credential_id", [OPENING_ACCOUNT_NAME, DENIED_ACCOUNT_ID, "goac_never_reported"])
async def test_name_or_connection_the_sheet_read_did_not_open_stays_authority_denied(
    ctx_after_read_google_sheet: CopilotContext, credential_id: str
) -> None:
    ctx = ctx_after_read_google_sheet

    assert await _approved_after_tool_activity(ctx, credential_id) == set()
    assert ctx.request_policy.run_approved_connection_ids == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "later_listing",
    [
        [{"tool": "list_integrations", "integrations": "malformed"}],
        _listed_integrations(_google("goac_other", "Other Sheets")),
    ],
)
async def test_later_listing_neither_erases_an_opened_connection_nor_admits_one_that_did_not_open(
    ctx_after_read_google_sheet: CopilotContext, later_listing: list[dict[str, object]]
) -> None:
    ctx = ctx_after_read_google_sheet
    ctx.tool_activity.extend(later_listing)

    assert await _approved_after_tool_activity(ctx, OPENING_ACCOUNT_ID) == {OPENING_ACCOUNT_ID}
    assert await _approved_after_tool_activity(ctx, DENIED_ACCOUNT_ID) == set()


OUTLOOK_ACCOUNT_ID = "moac_outlook"
DRIVE_ACCOUNT_ID = "goac_drive"


def _outlook(state: str = "active", organization_id: str = "org-1") -> MicrosoftOAuthCredentialBase:
    return MicrosoftOAuthCredentialBase(
        id=OUTLOOK_ACCOUNT_ID,
        organization_id=organization_id,
        credential_name="Outlook Mail",
        email_address="mail@example.org",
        state=state,
        scopes_granted=["Mail.Read"],
        created_at=datetime(2026, 8, 15),
        modified_at=datetime(2026, 8, 15),
    )


def _inbox_block(credential_id: str) -> dict[str, object]:
    return {"label": "mail", "block_type": "email_inbox", "email_client": "outlook", "credential_id": credential_id}


def _sheets_read_block() -> dict[str, object]:
    return {
        "label": "read",
        "block_type": "google_sheets_read",
        "spreadsheet_url": "https://docs.google.com/spreadsheets/d/abc/edit",
        "credential_id": DRIVE_ACCOUNT_ID,
    }


def _gate_ctx(
    monkeypatch: pytest.MonkeyPatch,
    *,
    listed: list[str] = (),
    outlook: MicrosoftOAuthCredentialBase | None = None,
    message: str = "test it",
) -> CopilotContext:
    _patch_visible_credentials(monkeypatch, [_google(DRIVE_ACCOUNT_ID, "Drive")])
    monkeypatch.setattr(
        microsoft_oauth_service, "get_credentials_for_org", AsyncMock(return_value=[outlook or _outlook()])
    )
    ctx = make_copilot_ctx(request_policy=request_policy_module.RequestPolicy(canonical_user_message=message))
    ctx.tool_activity = [
        {"tool": "list_integrations", "integrations": [{"connection_id": i, "state": "active"} for i in listed]}
    ]
    return ctx


async def _gate(
    monkeypatch: pytest.MonkeyPatch,
    definition: dict[str, object],
    *,
    listed: list[str] = (),
    outlook: MicrosoftOAuthCredentialBase | None = None,
    message: str = "test it",
    run_parameters: dict[str, object] | None = None,
) -> object:
    ctx = _gate_ctx(monkeypatch, listed=listed, outlook=outlook, message=message)
    return await run_credential_gate(ctx, definition, labels=None, run_parameters=run_parameters)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "blocks",
    [[_sheets_read_block()], [_inbox_block(OUTLOOK_ACCOUNT_ID)]],
    ids=["drive", "outlook"],
)
async def test_an_account_field_the_turn_did_not_list_or_cite_is_denied_until_it_does(
    monkeypatch: pytest.MonkeyPatch, blocks: list[dict[str, object]]
) -> None:
    acct = {
        "key": "acct",
        "parameter_type": "workflow",
        "workflow_parameter_type": "string",
        "default_value": OUTLOOK_ACCOUNT_ID,
    }
    definition = {"parameters": [acct], "blocks": blocks}

    denied = await _gate(monkeypatch, definition)
    admitted = await _gate(monkeypatch, definition, listed=[DRIVE_ACCOUNT_ID, OUTLOOK_ACCOUNT_ID])

    assert denied.blocker is not None and denied.blocker.blocker_kind == "authority_denied"
    assert admitted.blocker is None and admitted.error is None


@pytest.mark.asyncio
async def test_an_account_id_the_user_wrote_is_admitted_without_a_listing(monkeypatch: pytest.MonkeyPatch) -> None:
    definition = {"parameters": [], "blocks": [_inbox_block(OUTLOOK_ACCOUNT_ID)]}

    result = await _gate(monkeypatch, definition, message=f"read my mail through {OUTLOOK_ACCOUNT_ID} and test it")

    assert result.blocker is None and result.error is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("block", "gated"),
    [
        ({"block_type": "file_upload", "storage_type": "s3"}, False),
        ({"block_type": "file_upload", "storage_type": "google_drive"}, True),
        ({"block_type": "file_download", "download_target": "website"}, False),
        ({"block_type": "file_download", "download_target": "google_drive"}, True),
        ({"block_type": "send_email", "credential_id": "goac_stale"}, False),
        ({"block_type": "send_email", "transport": "gmail", "credential_id": "goac_stale"}, True),
    ],
    ids=["s3_upload", "drive_upload", "website_download", "drive_download", "smtp_send", "gmail_send"],
)
async def test_a_leftover_account_id_is_gated_only_where_the_block_reads_it(
    monkeypatch: pytest.MonkeyPatch, block: dict[str, str], gated: bool
) -> None:
    definition = {"parameters": [], "blocks": [{"label": "b", "google_credential_id": "goac_stale", **block}]}

    result = await _gate(monkeypatch, definition)

    assert (result.blocker is not None) is gated


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outlook", [_outlook(state="error"), _outlook(organization_id="org-other")], ids=["inactive", "foreign_org"]
)
async def test_a_listed_account_that_is_inactive_or_another_orgs_is_refused(
    monkeypatch: pytest.MonkeyPatch, outlook: MicrosoftOAuthCredentialBase
) -> None:
    result = await _gate(
        monkeypatch,
        {"parameters": [], "blocks": [_inbox_block(OUTLOOK_ACCOUNT_ID)]},
        listed=[OUTLOOK_ACCOUNT_ID],
        outlook=outlook,
    )

    assert result.blocker is not None


@pytest.mark.asyncio
async def test_one_listed_account_bound_to_sheets_and_mail_is_admitted_for_both(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sheet = {
        "label": "sheet",
        "block_type": "google_sheets_read",
        "spreadsheet_url": "https://example.com",
        "credential_id": DRIVE_ACCOUNT_ID,
    }
    mail = {**_inbox_block(DRIVE_ACCOUNT_ID), "email_client": "gmail"}

    result = await _gate(monkeypatch, {"parameters": [], "blocks": [sheet, mail]}, listed=[DRIVE_ACCOUNT_ID])

    assert result.blocker is None and result.error is None


@pytest.mark.asyncio
async def test_an_account_field_in_a_block_that_does_not_run_does_not_waive_credential_approval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    login = {
        "key": "login",
        "parameter_type": "workflow",
        "workflow_parameter_type": "credential_id",
        "default_value": "cred_unapproved",
    }
    sign_in = {"label": "sign_in", "block_type": "login", "url": "https://example.com", "parameter_keys": ["login"]}
    definition = {"parameters": [login], "blocks": [sign_in, _inbox_block("cred_unapproved")]}
    _patch_visible_credentials(monkeypatch, [])
    ctx = make_copilot_ctx(request_policy=request_policy_module.RequestPolicy(canonical_user_message="test it"))

    result = await run_credential_gate(ctx, definition, labels=["sign_in"], run_parameters=None)

    assert result.blocker is None
    assert result.error is not None and "cred_unapproved" in result.error


@pytest.mark.asyncio
@pytest.mark.parametrize("listed", [True, False], ids=["listed", "named_only"])
async def test_a_microsoft_read_failure_admits_a_listed_google_account_but_no_named_one(
    monkeypatch: pytest.MonkeyPatch, listed: bool
) -> None:
    _patch_visible_credentials(monkeypatch, [_google(DRIVE_ACCOUNT_ID, "Drive")])
    monkeypatch.setattr(
        microsoft_oauth_service, "get_credentials_for_org", AsyncMock(side_effect=RuntimeError("graph down"))
    )
    ctx = make_copilot_ctx(request_policy=request_policy_module.RequestPolicy(canonical_user_message="use Drive"))
    ctx.tool_activity = _listed_integrations(_google(DRIVE_ACCOUNT_ID, "Drive")) if listed else []

    result = await run_credential_gate(
        ctx, {"parameters": [], "blocks": [_sheets_read_block()]}, labels=None, run_parameters=None
    )

    assert (result.blocker is None) is listed


@pytest.mark.asyncio
async def test_a_loop_variable_in_an_account_field_is_not_resolved_through_a_run_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop = {
        "label": "each",
        "block_type": "for_loop",
        "loop_over_parameter_key": "mailboxes",
        "loop_blocks": [_inbox_block("{{ current_value }}")],
    }
    definition = {"parameters": [{"key": "mailboxes", "parameter_type": "workflow"}], "blocks": [loop]}
    run_parameters = {"current_value": OUTLOOK_ACCOUNT_ID}

    refs = workflow_connection_refs({"workflow_definition": definition}, run_parameters=run_parameters)
    result = await _gate(monkeypatch, definition, listed=[OUTLOOK_ACCOUNT_ID], run_parameters=run_parameters)

    assert refs == ["{{ current_value }}"]
    assert result.blocker is not None and result.blocker.blocker_kind == "authority_denied"


_GMAIL_SEND = {
    "label": "send",
    "block_type": "send_email",
    "transport": "gmail",
    "credential_id": DRIVE_ACCOUNT_ID,
}
_DRIVE_UPLOAD = {
    "label": "upload",
    "block_type": "file_upload",
    "storage_type": "google_drive",
    "google_credential_id": DRIVE_ACCOUNT_ID,
}
_GMAIL_INBOX = {**_inbox_block(DRIVE_ACCOUNT_ID), "email_client": "gmail"}
_SHEETS_WRITE = {
    "label": "write",
    "block_type": "google_sheets_write",
    "spreadsheet_url": "https://docs.google.com/spreadsheets/d/abc/edit",
    "credential_id": DRIVE_ACCOUNT_ID,
}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("block", "message", "admitted"),
    [
        (_GMAIL_SEND, "test it", False),
        (_DRIVE_UPLOAD, "test it", False),
        (_SHEETS_WRITE, "test it", False),
        (_GMAIL_SEND, f"send it from {DRIVE_ACCOUNT_ID}", True),
        (_DRIVE_UPLOAD, f"upload it through {DRIVE_ACCOUNT_ID}", True),
        (_SHEETS_WRITE, f"write it with {DRIVE_ACCOUNT_ID}", True),
        (_GMAIL_INBOX, "test it", True),
    ],
    ids=[
        "send_listed",
        "upload_listed",
        "sheets_write_listed",
        "send_cited",
        "upload_cited",
        "sheets_write_cited",
        "inbox_listed",
    ],
)
async def test_a_listing_admits_an_account_only_where_the_block_reads_through_it(
    monkeypatch: pytest.MonkeyPatch, block: dict[str, object], message: str, admitted: bool
) -> None:
    result = await _gate(monkeypatch, {"parameters": [], "blocks": [block]}, listed=[DRIVE_ACCOUNT_ID], message=message)

    assert (result.blocker is None) is admitted


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("run_value", "message", "admitted"),
    [
        (DRIVE_ACCOUNT_ID, "test it", False),
        (DRIVE_ACCOUNT_ID, f"send it from {DRIVE_ACCOUNT_ID}", True),
        ("goac_matches_no_connection", "test it", True),
    ],
    ids=["listed", "cited", "no_connection"],
)
async def test_an_account_an_agent_step_can_read_is_admitted_only_as_acting(
    monkeypatch: pytest.MonkeyPatch, run_value: str, message: str, admitted: bool
) -> None:
    agent = {
        "label": "agent",
        "block_type": "agent_step",
        "prompt": "send the report from {{ acct }}",
        "tools": ["http_request"],
        "parameter_keys": ["acct"],
    }
    definition = {"parameters": [{"key": "acct", "parameter_type": "workflow"}], "blocks": [agent]}

    result = await _gate(
        monkeypatch, definition, listed=[DRIVE_ACCOUNT_ID], message=message, run_parameters={"acct": run_value}
    )

    assert (result.blocker is None and result.error is None) is admitted


@pytest.mark.asyncio
async def test_an_agent_step_account_is_still_checked_when_a_provider_read_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = {
        "label": "agent",
        "block_type": "agent_step",
        "prompt": f"send the report from {DRIVE_ACCOUNT_ID}",
        "tools": ["http_request"],
    }
    ctx = _gate_ctx(monkeypatch, listed=[DRIVE_ACCOUNT_ID])
    _patch_visible_credentials(monkeypatch, RuntimeError("google read failed"))

    result = await run_credential_gate(ctx, {"parameters": [], "blocks": [agent]}, labels=None, run_parameters=None)

    assert result.blocker is not None


@pytest.mark.asyncio
async def test_an_agent_step_offered_an_account_tool_acts_on_every_offered_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = {
        "label": "agent",
        "block_type": "agent_step",
        "prompt": "summarize the sheet",
        "tools": ["google_sheets_read"],
    }
    definition = {"parameters": [], "blocks": [agent]}

    sender = {**agent, "tools": ["send_email"]}
    listed = [DRIVE_ACCOUNT_ID, OUTLOOK_ACCOUNT_ID]

    unnamed = await _gate(monkeypatch, definition)
    listed_reader = await _gate(monkeypatch, definition, listed=listed)
    listed_sender = await _gate(monkeypatch, {"parameters": [], "blocks": [sender]}, listed=listed)
    ctx = _gate_ctx(monkeypatch)
    _patch_visible_credentials(monkeypatch, RuntimeError("google read failed"))
    unreadable = await run_credential_gate(ctx, definition, labels=None, run_parameters=None)

    assert unnamed.blocker is not None
    assert listed_reader.blocker is None and listed_reader.error is None
    assert listed_sender.blocker is not None
    assert unreadable.error is not None and unreadable.blocker is None


@pytest.mark.asyncio
async def test_a_copilot_test_run_needs_only_the_account_the_chat_picked(monkeypatch: pytest.MonkeyPatch) -> None:
    agent = {"label": "agent", "block_type": "agent_step", "prompt": "email the summary", "tools": ["send_email"]}
    definition = {"parameters": [], "blocks": [agent]}
    # Off, every account still needs approval, so a single pick does not let the run through.
    picked_off = _gate_ctx(monkeypatch)
    picked_off.request_policy.run_approved_connection_ids = [DRIVE_ACCOUNT_ID]
    off = await run_credential_gate(
        picked_off, definition, labels=None, run_parameters=None, narrow_agent_step_accounts=True
    )
    assert off.blocker is not None
    monkeypatch.setattr(settings, "COPILOT_NARROW_AGENT_STEP_ACCOUNTS", True)
    unapproved = _gate_ctx(monkeypatch)
    cited = _gate_ctx(monkeypatch, message=f"send it from {DRIVE_ACCOUNT_ID}")

    blocked = await run_credential_gate(
        unapproved, definition, labels=None, run_parameters=None, narrow_agent_step_accounts=True
    )
    mentioned = await run_credential_gate(
        cited, definition, labels=None, run_parameters=None, narrow_agent_step_accounts=True
    )

    assert blocked.blocker is not None and blocked.agent_step_connection_ids is None
    # A mention is not a typed approval, so it does not narrow the offer; the card still asks.
    assert mentioned.blocker is not None and mentioned.agent_step_connection_ids is None
    earlier = _gate_ctx(monkeypatch)
    earlier.request_policy.run_approved_connection_ids = [DRIVE_ACCOUNT_ID]
    picked_before = await run_credential_gate(
        earlier, definition, labels=None, run_parameters=None, narrow_agent_step_accounts=True
    )
    assert picked_before.blocker is None and picked_before.agent_step_connection_ids == (DRIVE_ACCOUNT_ID,)
    renamed = _gate_ctx(monkeypatch, message=f"send it from {OUTLOOK_ACCOUNT_ID} instead")
    renamed.request_policy.run_approved_connection_ids = [DRIVE_ACCOUNT_ID]
    named_now = await run_credential_gate(
        renamed, definition, labels=None, run_parameters=None, narrow_agent_step_accounts=True
    )
    # The account the current request names stays offered next to the earlier pick.
    assert named_now.blocker is None
    assert set(named_now.agent_step_connection_ids or ()) == {DRIVE_ACCOUNT_ID, OUTLOOK_ACCOUNT_ID}


@pytest.mark.asyncio
async def test_a_triggered_childs_account_need_reaches_the_card_and_the_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "COPILOT_NARROW_AGENT_STEP_ACCOUNTS", True)
    child = {
        "parameters": [],
        "blocks": [{"label": "agent", "block_type": "agent_step", "prompt": "email it", "tools": ["send_email"]}],
    }
    trigger = {"label": "child", "block_type": "workflow_trigger", "workflow_permanent_id": "wpid_child"}
    definition = {"parameters": [], "blocks": [trigger]}
    unapproved = _gate_ctx(monkeypatch)
    picked = _gate_ctx(monkeypatch)
    picked.request_policy.run_approved_connection_ids = [DRIVE_ACCOUNT_ID]
    monkeypatch.setattr(
        app.DATABASE.workflows,
        "get_workflow_by_permanent_id",
        AsyncMock(return_value=SimpleNamespace(workflow_definition=SimpleNamespace(model_dump=lambda mode: child))),
    )

    blocked = await run_credential_gate(
        unapproved, definition, labels=None, run_parameters=None, narrow_agent_step_accounts=True
    )
    narrowed = await run_credential_gate(
        picked, definition, labels=None, run_parameters=None, narrow_agent_step_accounts=True
    )

    assert blocked.blocker is not None and blocked.agent_step_connection_ids is None
    assert narrowed.blocker is None and narrowed.agent_step_connection_ids == (DRIVE_ACCOUNT_ID,)

    # Off, the child is not read, so nothing new needs approval, but every account is recorded for a worker that is on.
    monkeypatch.setattr(settings, "COPILOT_NARROW_AGENT_STEP_ACCOUNTS", False)
    off = await run_credential_gate(
        _gate_ctx(monkeypatch), definition, labels=None, run_parameters=None, narrow_agent_step_accounts=True
    )
    assert off.blocker is None
    assert set(off.agent_step_connection_ids or ()) == {DRIVE_ACCOUNT_ID, OUTLOOK_ACCOUNT_ID}
    revoked = await run_credential_gate(
        _gate_ctx(monkeypatch, outlook=_outlook(state="revoked")),
        definition,
        labels=None,
        run_parameters=None,
        narrow_agent_step_accounts=True,
    )
    assert revoked.agent_step_connection_ids == (DRIVE_ACCOUNT_ID,)
    # An unreadable provider is not recorded as a partial list; the dispatch is refused so it can be retried.
    unreadable = _gate_ctx(monkeypatch)
    monkeypatch.setattr(
        microsoft_oauth_service, "get_credentials_for_org", AsyncMock(side_effect=RuntimeError("provider down"))
    )
    refused = await run_credential_gate(
        unreadable, definition, labels=None, run_parameters=None, narrow_agent_step_accounts=True
    )
    assert refused.error is not None and refused.agent_step_connection_ids is None

    # On, a triggered child with no account tool adds nothing to record.
    monkeypatch.setattr(settings, "COPILOT_NARROW_AGENT_STEP_ACCOUNTS", True)
    monkeypatch.setattr(
        app.DATABASE.workflows,
        "get_workflow_by_permanent_id",
        AsyncMock(
            return_value=SimpleNamespace(
                workflow_definition=SimpleNamespace(model_dump=lambda mode: {"parameters": [], "blocks": []})
            )
        ),
    )
    accountless = await run_credential_gate(
        _gate_ctx(monkeypatch), definition, labels=None, run_parameters=None, narrow_agent_step_accounts=True
    )
    assert accountless.blocker is None and accountless.agent_step_connection_ids is None


def _trigger(target: str) -> dict[str, Any]:
    return {"label": f"t_{target}", "block_type": "workflow_trigger", "workflow_permanent_id": target}


def _chain(levels: int) -> dict[str, dict[str, Any]]:
    return {
        f"wpid_{level}": {"parameters": [], "blocks": [_trigger(f"wpid_{level + 1}")] if level < levels else []}
        for level in range(1, levels + 1)
    }


_SENDER_CHILD = {
    "wpid_1": {
        "parameters": [],
        "blocks": [{"label": "agent", "block_type": "agent_step", "prompt": "email it", "tools": ["send_email"]}],
    }
}
_DEPTH = WorkflowTriggerBlock.MAX_TRIGGER_DEPTH


@pytest.mark.parametrize(
    ("workflows", "root_target", "narrowing_on", "narrow", "blocks", "reads"),
    [
        pytest.param(_chain(_DEPTH), "wpid_1", True, True, False, _DEPTH, id="at_the_depth_limit"),
        pytest.param(_chain(_DEPTH + 1), "wpid_1", True, True, True, _DEPTH, id="past_the_depth_limit"),
        pytest.param({}, "{{ target }}", True, True, True, 0, id="templated"),
        pytest.param({}, "wpid_1", True, True, True, 1, id="unreadable"),
        pytest.param({"wpid_1": None}, "wpid_1", True, True, True, 1, id="read_error"),
        pytest.param(
            {
                "wpid_1": {"parameters": [], "blocks": [_trigger("wpid_2")]},
                "wpid_2": {"parameters": [], "blocks": [_trigger("wpid_1")]},
            },
            "wpid_1",
            True,
            True,
            False,
            2,
            id="cycle_reads_each_once",
        ),
        pytest.param(_SENDER_CHILD, "wpid_1", True, True, True, 1, id="child_sends"),
        pytest.param(_SENDER_CHILD, "wpid_1", False, True, False, 0, id="setting_off"),
        pytest.param(_SENDER_CHILD, "wpid_1", True, False, False, 0, id="schedule_gate"),
    ],
)
@pytest.mark.asyncio
async def test_the_triggered_workflow_walk(
    monkeypatch: pytest.MonkeyPatch,
    workflows: dict[str, dict[str, Any] | None],
    root_target: str,
    narrowing_on: bool,
    narrow: bool,
    blocks: bool,
    reads: int,
) -> None:
    monkeypatch.setattr(settings, "COPILOT_NARROW_AGENT_STEP_ACCOUNTS", narrowing_on)

    async def read(*, workflow_permanent_id: str, organization_id: str) -> SimpleNamespace | None:
        if workflow_permanent_id in workflows and workflows[workflow_permanent_id] is None:
            raise RuntimeError("database down")
        definition = workflows.get(workflow_permanent_id)
        if definition is None:
            return None
        return SimpleNamespace(workflow_definition=SimpleNamespace(model_dump=lambda mode: definition))

    reader = AsyncMock(side_effect=read)
    monkeypatch.setattr(app.DATABASE.workflows, "get_workflow_by_permanent_id", reader)

    result = await run_credential_gate(
        _gate_ctx(monkeypatch),
        {"parameters": [], "blocks": [_trigger(root_target)]},
        labels=None,
        run_parameters=None,
        narrow_agent_step_accounts=narrow,
    )

    assert (result.blocker is not None) is blocks
    assert reader.await_count == reads


@pytest.mark.asyncio
async def test_a_copilot_test_run_records_the_accounts_this_turn_admitted(monkeypatch: pytest.MonkeyPatch) -> None:
    reader = {"label": "agent", "block_type": "agent_step", "prompt": "summarize", "tools": ["google_sheets_read"]}
    sender = {"label": "agent", "block_type": "agent_step", "prompt": "email it", "tools": ["send_email"]}
    listed = _gate_ctx(monkeypatch, listed=[DRIVE_ACCOUNT_ID, OUTLOOK_ACCOUNT_ID])
    read_by_listing = await run_credential_gate(
        listed,
        {"parameters": [], "blocks": [reader]},
        labels=None,
        run_parameters=None,
        narrow_agent_step_accounts=True,
    )
    named = _gate_ctx(monkeypatch, message=f"send it from {DRIVE_ACCOUNT_ID}")
    monkeypatch.setattr(microsoft_oauth_service, "get_credentials_for_org", AsyncMock(return_value=[]))
    sent_by_name = await run_credential_gate(
        named, {"parameters": [], "blocks": [sender]}, labels=None, run_parameters=None, narrow_agent_step_accounts=True
    )

    assert read_by_listing.blocker is None
    assert set(read_by_listing.agent_step_connection_ids or ()) == {DRIVE_ACCOUNT_ID, OUTLOOK_ACCOUNT_ID}
    assert sent_by_name.blocker is None and sent_by_name.agent_step_connection_ids == (DRIVE_ACCOUNT_ID,)


@pytest.mark.asyncio
async def test_an_unapproved_nango_gmail_send_is_held_but_a_saved_powerbi_query_is_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    send = {
        "label": "send",
        "block_type": "send_email",
        "transport": "gmail",
        "credential_source": "nango",
        "connection_id": "nango-conn-1",
        "recipients": ["ops@example.com"],
        "subject": "Report",
        "body": "Done",
    }

    powerbi = {
        "label": "query",
        "block_type": "powerbi_query",
        "connection_id": "nango-powerbi-1",
        "dataset_id": "dataset-1",
        "query": 'EVALUATE ROW("x", 1)',
    }

    held = await _gate(monkeypatch, {"parameters": [], "blocks": [send]})
    # Power BI's Nango accounts are not listed among the user's connections yet, so its field stays outside the check.
    passed = await _gate(monkeypatch, {"parameters": [], "blocks": [powerbi]})

    assert held.blocker is not None
    assert passed.blocker is None and passed.error is None


@pytest.mark.asyncio
async def test_a_schedule_refuses_an_account_the_turn_only_listed(monkeypatch: pytest.MonkeyPatch) -> None:
    definition = {"parameters": [], "blocks": [_GMAIL_INBOX]}
    monkeypatch.setattr(
        app.DATABASE.workflows,
        "get_workflow_by_permanent_id",
        AsyncMock(return_value=SimpleNamespace(workflow_definition=definition)),
    )
    listed_ctx = _gate_ctx(monkeypatch, listed=[DRIVE_ACCOUNT_ID])
    cited_ctx = _gate_ctx(monkeypatch, listed=[DRIVE_ACCOUNT_ID], message=f"schedule it with {DRIVE_ACCOUNT_ID}")

    denied = await mcp_hooks._schedule_parameters_credential_pre_hook({"parameters": {}}, listed_ctx)
    allowed = await mcp_hooks._schedule_parameters_credential_pre_hook({"parameters": {}}, cited_ctx)

    assert denied is not None and denied["ok"] is False
    assert allowed is None


def _named(connection_id: str, provider: ConnectionProvider, state: str) -> OrgConnection:
    return OrgConnection(connection_id, provider, "Work", None, state, ())


def test_a_name_shared_across_providers_is_not_resolved_or_cited_across_them() -> None:
    google_off = _named("goac_work", "google", "error")
    google_on = _named("goac_work", "google", "active")
    outlook_on = _named("moac_work", "microsoft", "active")

    # An inactive Google "Work" must not hand a Sheets slot to the active Outlook "Work".
    assert resolve_connection_reference([google_off, outlook_on], "Work").status == "ambiguous"
    # A bound Google id still counts as named when an Outlook account shares its name.
    assert connection_is_cited("use my Work account", google_on, [google_on, outlook_on])
