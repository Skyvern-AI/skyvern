"""End-to-end route tests for workflow_copilot_chat_post.

Covers the three scenarios the debated plan requires:

1. Flag off -> old-copilot path runs, new-copilot is not reached.
2. Flag on, successful turn -> new-copilot handler runs and does not
   trigger the restore-on-error branch.
3. Flag on, mid-stream failure -> ``_restore_workflow_definition`` is
   awaited so a half-persisted draft is rolled back.

These tests exercise the dispatcher and stream-handler wiring in
``skyvern/forge/sdk/routes/workflow_copilot.py`` without reaching a
real database -- all DB / LLM / agent surfaces are patched.
"""

from __future__ import annotations

import contextlib
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import structlog.testing
from fastapi import HTTPException

from skyvern.config import settings
from skyvern.forge import app
from skyvern.forge.agent_functions import AgentFunction
from skyvern.forge.sdk.api.llm.exceptions import LLMProviderError
from skyvern.forge.sdk.artifact.models import ArtifactType
from skyvern.forge.sdk.copilot import agent as agent_module
from skyvern.forge.sdk.copilot.canonical_ownership import workflow_content_fingerprint
from skyvern.forge.sdk.copilot.context import AgentResult, TurnNarrativePayload
from skyvern.forge.sdk.copilot.enforcement import TOTAL_TIMEOUT_SECONDS
from skyvern.forge.sdk.copilot.terminal_envelope import (
    INTERRUPTED_TERMINAL_MESSAGE,
    INTERRUPTED_TERMINAL_REASON,
    assemble_terminal_envelope,
)
from skyvern.forge.sdk.copilot.turn_outcome import build_minimal_turn_outcome
from skyvern.forge.sdk.routes import workflow_copilot as workflow_copilot_route
from skyvern.forge.sdk.routes.workflow_copilot import (
    COPILOT_V2_FLAG_KEY,
    RECONCILE_ABANDON_AFTER_SECONDS,
    _persist_turn_messages,
    _validate_copilot_audio_artifact_id,
    convert_to_history_messages,
    workflow_copilot_chat_audio,
    workflow_copilot_chat_history,
    workflow_copilot_chat_post,
)
from skyvern.forge.sdk.schemas.copilot_turn_outcome import ResponseKind, TurnOutcome
from skyvern.forge.sdk.schemas.workflow_copilot import (
    CopilotPendingTurn,
    WorkflowCopilotChat,
    WorkflowCopilotChatMessage,
    WorkflowCopilotChatRequest,
    WorkflowCopilotChatSender,
    WorkflowCopilotStreamErrorUpdate,
    WorkflowCopilotStreamResponseUpdate,
)
from skyvern.forge.sdk.workflow.models.workflow import Workflow, WorkflowDefinition
from tests.unit.copilot_route_test_support import install_fake_create, setup_new_copilot_mocks


@pytest.fixture(autouse=True)
def _default_terminal_envelope_render_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        app.AGENT_FUNCTION,
        "should_render_copilot_terminal_from_envelope",
        AsyncMock(return_value=False),
    )


@pytest.fixture
def organization() -> SimpleNamespace:
    return SimpleNamespace(organization_id="org-1")


@pytest.fixture
def anon_request() -> MagicMock:
    request = MagicMock()
    request.headers = {}
    return request


@pytest.fixture
def api_key_request() -> MagicMock:
    request = MagicMock()
    request.headers = {"x-api-key": "sk-test-key"}
    return request


@pytest.fixture
def copilot_stream() -> MagicMock:
    stream = MagicMock()
    stream.send = AsyncMock(return_value=True)
    stream.is_disconnected = AsyncMock(return_value=False)
    return stream


def _make_chat_request(
    mode: str | None = None, code_block: bool | None = None, keep_pending_proposal: bool = False
) -> WorkflowCopilotChatRequest:
    return WorkflowCopilotChatRequest(
        workflow_permanent_id="wpid-1",
        workflow_id="wf-request",
        workflow_copilot_chat_id="chat-1",
        workflow_run_id=None,
        message="Please update it",
        workflow_yaml="title: Example",
        mode=mode,
        code_block=code_block,
        keep_pending_proposal=keep_pending_proposal,
    )


def _terminal_payload(
    *,
    verified: bool,
    workflow_applied: bool,
    workflow_mutated: bool = True,
    turn_outcome_response_kind: str = "build",
) -> dict[str, Any]:
    envelope = assemble_terminal_envelope(
        response_type="REPLY",
        verified=verified,
        workflow_applied=workflow_applied,
        proposal_disposition="no_proposal",
        run_outcomes=[],
        blocker_reason=None,
        halt_kind=None,
        attempted="Attempted full run.",
        workflow_mutated=workflow_mutated,
        turn_outcome_response_kind=turn_outcome_response_kind,
    )
    assert envelope is not None
    return envelope.model_dump(mode="json")


def _narrative_payload() -> dict[str, Any]:
    return {
        "turnId": "turn-1",
        "turnIndex": 0,
        "mode": "build",
        "designStarted": True,
        "designEnded": True,
        "draft": None,
        "blocks": [],
        "terminal": "response",
        "terminalMessage": "done",
        "narrativeSummary": "done",
        "priorBlockCount": None,
        "designActivity": [],
        "startedAt": None,
        "endedAt": None,
    }


@pytest.mark.asyncio
async def test_finalise_normal_turn_finalizes_terminal_envelope_without_auto_accept(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chat = SimpleNamespace(
        organization_id="org-1",
        workflow_copilot_chat_id="chat-1",
        proposed_workflow=None,
        auto_accept=False,
    )
    original_workflow = SimpleNamespace(workflow_id="wf-canonical")
    updated_workflow = MagicMock()
    updated_workflow.model_dump.return_value = {"workflow_id": "wf-draft"}
    agent_result = AgentResult(
        user_response="done",
        updated_workflow=updated_workflow,
        global_llm_context=None,
        response_type="REPLY",
        proposal_disposition="auto_applicable",
        narrative_payload=_narrative_payload(),
        terminal_envelope=_terminal_payload(verified=True, workflow_applied=False),
    )
    _, workflow_params = setup_new_copilot_mocks(monkeypatch, chat, original_workflow, agent_result)
    stream = MagicMock(send=AsyncMock(return_value=True))

    await workflow_copilot_route._finalise_normal_turn(
        stream=stream,
        chat=chat,
        organization_id="org-1",
        original_workflow=original_workflow,
        chat_request=_make_chat_request(),
        agent_result=agent_result,
    )

    response_frame = stream.send.await_args.args[0]
    assert isinstance(response_frame, WorkflowCopilotStreamResponseUpdate)
    assert response_frame.terminal_envelope is not None
    assert response_frame.terminal_envelope["workflow_applied"] is False
    # Verified fix, not auto-accepted: a pending proposal for the review gate, not stopped.
    assert response_frame.terminal_envelope["next_state"] == "proposal_pending"
    assert response_frame.terminal_envelope["response_kind"] == "update"

    persisted_payload = workflow_params.create_workflow_copilot_chat_message.await_args_list[-1].kwargs[
        "narrative_payload"
    ]
    assert persisted_payload is not None
    assert persisted_payload["terminalEnvelope"]["workflow_applied"] is False
    assert persisted_payload["terminalEnvelope"]["next_state"] == "proposal_pending"
    assert persisted_payload["terminalEnvelope"]["response_kind"] == "update"


@pytest.mark.asyncio
async def test_finalise_normal_turn_finalizes_terminal_envelope_with_auto_accept(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chat = SimpleNamespace(
        organization_id="org-1",
        workflow_copilot_chat_id="chat-1",
        proposed_workflow=None,
        auto_accept=True,
    )
    original_workflow = SimpleNamespace(workflow_id="wf-canonical")
    updated_workflow = MagicMock()
    updated_workflow.model_dump.return_value = {"workflow_id": "wf-draft"}
    agent_result = AgentResult(
        user_response="done",
        updated_workflow=updated_workflow,
        global_llm_context=None,
        response_type="REPLY",
        proposal_disposition="auto_applicable",
        narrative_payload=_narrative_payload(),
        terminal_envelope=_terminal_payload(verified=True, workflow_applied=False),
    )
    _, workflow_params = setup_new_copilot_mocks(monkeypatch, chat, original_workflow, agent_result)
    stream = MagicMock(send=AsyncMock(return_value=True))

    await workflow_copilot_route._finalise_normal_turn(
        stream=stream,
        chat=chat,
        organization_id="org-1",
        original_workflow=original_workflow,
        chat_request=_make_chat_request(),
        agent_result=agent_result,
    )

    response_frame = stream.send.await_args.args[0]
    assert isinstance(response_frame, WorkflowCopilotStreamResponseUpdate)
    assert response_frame.terminal_envelope is not None
    assert response_frame.terminal_envelope["workflow_applied"] is True
    assert response_frame.terminal_envelope["next_state"] == "completed"
    assert response_frame.terminal_envelope["response_kind"] == "update"

    persisted_payload = workflow_params.create_workflow_copilot_chat_message.await_args_list[-1].kwargs[
        "narrative_payload"
    ]
    assert persisted_payload is not None
    assert persisted_payload["terminalEnvelope"]["workflow_applied"] is True
    assert persisted_payload["terminalEnvelope"]["next_state"] == "completed"
    assert persisted_payload["terminalEnvelope"]["response_kind"] == "update"


@pytest.mark.asyncio
async def test_finalise_normal_turn_envelope_not_applied_without_proposal_on_auto_accept(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chat = SimpleNamespace(
        organization_id="org-1",
        workflow_copilot_chat_id="chat-1",
        proposed_workflow=None,
        auto_accept=True,
    )
    original_workflow = SimpleNamespace(workflow_id="wf-canonical")
    # No proposal at all, and proposal_disposition left at the AgentResult
    # default (auto_applicable) — the shape ASK_QUESTION/answer builders emit.
    agent_result = AgentResult(
        user_response="done",
        updated_workflow=None,
        global_llm_context=None,
        response_type="REPLY",
        narrative_payload=_narrative_payload(),
        terminal_envelope=_terminal_payload(verified=True, workflow_applied=False),
    )
    _, workflow_params = setup_new_copilot_mocks(monkeypatch, chat, original_workflow, agent_result)
    stream = MagicMock(send=AsyncMock(return_value=True))

    await workflow_copilot_route._finalise_normal_turn(
        stream=stream,
        chat=chat,
        organization_id="org-1",
        original_workflow=original_workflow,
        chat_request=_make_chat_request(),
        agent_result=agent_result,
    )

    response_frame = stream.send.await_args.args[0]
    assert isinstance(response_frame, WorkflowCopilotStreamResponseUpdate)
    assert response_frame.terminal_envelope is not None
    assert response_frame.terminal_envelope["workflow_applied"] is False
    assert response_frame.terminal_envelope["next_state"] != "completed"

    persisted_payload = workflow_params.create_workflow_copilot_chat_message.await_args_list[-1].kwargs[
        "narrative_payload"
    ]
    assert persisted_payload is not None
    assert persisted_payload["terminalEnvelope"]["workflow_applied"] is False
    assert persisted_payload["terminalEnvelope"]["next_state"] != "completed"


@pytest.mark.asyncio
async def test_finalise_normal_turn_flag_off_keeps_legacy_message_and_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    chat = SimpleNamespace(
        organization_id="org-1",
        workflow_copilot_chat_id="chat-1",
        proposed_workflow=None,
        auto_accept=False,
    )
    original_workflow = SimpleNamespace(workflow_id="wf-canonical")
    updated_workflow = MagicMock()
    updated_workflow.model_dump.return_value = {"workflow_id": "wf-draft"}
    agent_result = AgentResult(
        user_response="done",
        updated_workflow=updated_workflow,
        global_llm_context=None,
        response_type="REPLY",
        proposal_disposition="auto_applicable",
        narrative_payload=_narrative_payload(),
        terminal_envelope=_terminal_payload(verified=False, workflow_applied=False),
    )
    _, workflow_params = setup_new_copilot_mocks(monkeypatch, chat, original_workflow, agent_result)
    stream = MagicMock(send=AsyncMock(return_value=True))

    await workflow_copilot_route._finalise_normal_turn(
        stream=stream,
        chat=chat,
        organization_id="org-1",
        original_workflow=original_workflow,
        chat_request=_make_chat_request(),
        agent_result=agent_result,
    )

    response_frame = stream.send.await_args.args[0]
    assert isinstance(response_frame, WorkflowCopilotStreamResponseUpdate)
    assert response_frame.message == "done"
    assert response_frame.narrative_payload is not None
    assert response_frame.narrative_payload["terminalMessage"] == "done"
    assert response_frame.narrative_payload["narrativeSummary"] == "done"
    assert response_frame.terminal_envelope is not None
    assert response_frame.terminal_envelope.get("rendered_from_envelope") in {None, False}

    persisted_writes = workflow_params.create_workflow_copilot_chat_message.await_args_list
    assert persisted_writes[-1].kwargs["content"] == "done"
    persisted_payload = persisted_writes[-1].kwargs["narrative_payload"]
    assert persisted_payload is not None
    assert persisted_payload["terminalMessage"] == "done"
    assert persisted_payload["narrativeSummary"] == "done"
    assert persisted_payload["terminalEnvelope"].get("rendered_from_envelope") in {None, False}


def test_with_rendered_terminal_text_overwrites_distinct_summary() -> None:
    payload = {
        **_narrative_payload(),
        "terminalMessage": "legacy terminal prose",
        "narrativeSummary": "Concise legacy summary.",
    }
    out = workflow_copilot_route._with_rendered_terminal_text(payload, rendered_message="rendered truth")
    assert out is not None
    assert out["terminalMessage"] == "rendered truth"
    assert out["narrativeSummary"] == "rendered truth"


def test_chat_history_preserves_terminal_envelope() -> None:
    now = datetime.now(timezone.utc)
    payload = {
        **_narrative_payload(),
        "terminalEnvelope": {
            **_terminal_payload(verified=False, workflow_applied=False),
            "rendered_from_envelope": True,
        },
    }
    history = convert_to_history_messages(
        [
            WorkflowCopilotChatMessage(
                workflow_copilot_chat_message_id="message-1",
                workflow_copilot_chat_id="chat-1",
                sender=WorkflowCopilotChatSender.AI,
                content="done",
                narrative_payload=payload,
                created_at=now,
                modified_at=now,
            )
        ]
    )
    served = history[0].narrative_payload
    assert served is not None
    assert served["terminalEnvelope"]["run_verdict"] is None
    assert served["terminalEnvelope"]["rendered_from_envelope"] is True


@pytest.mark.asyncio
async def test_cancel_turn_finalizes_terminal_envelope_to_non_completed_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chat = SimpleNamespace(
        organization_id="org-1",
        workflow_copilot_chat_id="chat-1",
        proposed_workflow=None,
        auto_accept=True,
    )
    original_workflow = SimpleNamespace(workflow_id="wf-canonical")
    agent_result = AgentResult(
        user_response="Cancelled by user.",
        updated_workflow=None,
        global_llm_context=None,
        response_type="REPLY",
        proposal_disposition="auto_applicable",
        cancelled=True,
        narrative_payload=_narrative_payload(),
        terminal_envelope=_terminal_payload(verified=True, workflow_applied=True),
    )
    _, workflow_params = setup_new_copilot_mocks(monkeypatch, chat, original_workflow, agent_result)
    stream = MagicMock(send=AsyncMock(return_value=True))

    await workflow_copilot_route._persist_cancel_turn(
        stream=stream,
        chat=chat,
        organization_id="org-1",
        original_workflow=original_workflow,
        user_message="stop",
        agent_result=agent_result,
    )

    response_frame = stream.send.await_args.args[0]
    assert isinstance(response_frame, WorkflowCopilotStreamResponseUpdate)
    assert response_frame.cancelled is True
    assert response_frame.terminal_envelope is not None
    assert response_frame.terminal_envelope["workflow_applied"] is False
    assert response_frame.terminal_envelope["next_state"] != "completed"
    assert response_frame.terminal_envelope["response_kind"] == "stopped"

    persisted_payload = workflow_params.create_workflow_copilot_chat_message.await_args_list[-1].kwargs[
        "narrative_payload"
    ]
    assert persisted_payload is not None
    assert persisted_payload["terminalEnvelope"]["workflow_applied"] is False
    assert persisted_payload["terminalEnvelope"]["next_state"] != "completed"
    assert persisted_payload["terminalEnvelope"]["response_kind"] == "stopped"


@pytest.mark.asyncio
@pytest.mark.parametrize("flag_enabled", [True, False], ids=["flag-on", "flag-off"])
async def test_finalise_normal_turn_logs_render_decision(
    monkeypatch: pytest.MonkeyPatch,
    flag_enabled: bool,
) -> None:
    monkeypatch.setattr(
        app.AGENT_FUNCTION,
        "should_render_copilot_terminal_from_envelope",
        AsyncMock(return_value=flag_enabled),
    )
    chat = SimpleNamespace(
        organization_id="org-1",
        workflow_copilot_chat_id="chat-1",
        proposed_workflow=None,
        auto_accept=False,
    )
    original_workflow = SimpleNamespace(workflow_id="wf-canonical")
    updated_workflow = MagicMock()
    updated_workflow.model_dump.return_value = {"workflow_id": "wf-draft"}
    agent_result = AgentResult(
        user_response="done",
        updated_workflow=updated_workflow,
        global_llm_context=None,
        response_type="REPLY",
        proposal_disposition="auto_applicable",
        narrative_payload=_narrative_payload(),
        terminal_envelope=_terminal_payload(verified=False, workflow_applied=False),
    )
    _, _ = setup_new_copilot_mocks(monkeypatch, chat, original_workflow, agent_result)
    stream = MagicMock(send=AsyncMock(return_value=True))

    with structlog.testing.capture_logs() as logs:
        await workflow_copilot_route._finalise_normal_turn(
            stream=stream,
            chat=chat,
            organization_id="org-1",
            original_workflow=original_workflow,
            chat_request=_make_chat_request(),
            agent_result=agent_result,
        )

    decisions = [entry for entry in logs if entry["event"] == "copilot_terminal_render_decision"]
    assert len(decisions) == 1
    assert decisions[0]["flag_enabled"] is flag_enabled
    # This fixture's envelope is a stopped/stopped shape, so flag-on replaces.
    assert decisions[0]["replaced"] is flag_enabled
    assert decisions[0]["next_state"] == "stopped"
    assert decisions[0]["response_kind"] == "stopped"


@pytest.mark.asyncio
async def test_finalise_normal_turn_flag_on_stopped_envelope_renders_terminal_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        app.AGENT_FUNCTION,
        "should_render_copilot_terminal_from_envelope",
        AsyncMock(return_value=True),
    )
    chat = SimpleNamespace(
        organization_id="org-1",
        workflow_copilot_chat_id="chat-1",
        proposed_workflow=None,
        auto_accept=False,
    )
    original_workflow = SimpleNamespace(workflow_id="wf-canonical")
    updated_workflow = MagicMock()
    updated_workflow.model_dump.return_value = {"workflow_id": "wf-draft"}
    agent_result = AgentResult(
        user_response="done",
        updated_workflow=updated_workflow,
        global_llm_context=None,
        response_type="REPLY",
        proposal_disposition="auto_applicable",
        narrative_payload=_narrative_payload(),
        terminal_envelope=_terminal_payload(verified=False, workflow_applied=False),
    )
    _, workflow_params = setup_new_copilot_mocks(monkeypatch, chat, original_workflow, agent_result)
    stream = MagicMock(send=AsyncMock(return_value=True))

    await workflow_copilot_route._finalise_normal_turn(
        stream=stream,
        chat=chat,
        organization_id="org-1",
        original_workflow=original_workflow,
        chat_request=_make_chat_request(),
        agent_result=agent_result,
    )

    expected = "I stopped without confirming the goal was met."
    response_frame = stream.send.await_args.args[0]
    assert isinstance(response_frame, WorkflowCopilotStreamResponseUpdate)
    assert response_frame.message == expected
    assert response_frame.narrative_summary == expected
    assert response_frame.terminal_envelope is not None
    assert response_frame.terminal_envelope["rendered_from_envelope"] is True
    assert response_frame.narrative_payload is not None
    assert response_frame.narrative_payload["terminalMessage"] == expected
    assert response_frame.narrative_payload["narrativeSummary"] == expected

    persisted_writes = workflow_params.create_workflow_copilot_chat_message.await_args_list
    assert persisted_writes[-1].kwargs["content"] == expected
    persisted_payload = persisted_writes[-1].kwargs["narrative_payload"]
    assert persisted_payload is not None
    assert persisted_payload["terminalMessage"] == expected
    assert persisted_payload["narrativeSummary"] == expected
    assert persisted_payload["terminalEnvelope"]["rendered_from_envelope"] is True


@pytest.mark.asyncio
async def test_finalise_normal_turn_preserves_and_persists_answer_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        app.AGENT_FUNCTION,
        "should_render_copilot_terminal_from_envelope",
        AsyncMock(return_value=True),
    )
    chat = SimpleNamespace(
        organization_id="org-1",
        workflow_copilot_chat_id="chat-1",
        proposed_workflow=None,
        auto_accept=False,
    )
    answer = "Google Sheets steps can read rows through a connected integration."
    outcome = build_minimal_turn_outcome(answer, response_kind=ResponseKind.ANSWER)
    agent_result = AgentResult(
        user_response=answer,
        updated_workflow=None,
        global_llm_context=None,
        response_type="REPLY",
        narrative_payload=_narrative_payload(),
        turn_outcome=outcome,
        terminal_envelope=_terminal_payload(
            verified=False,
            workflow_applied=False,
            workflow_mutated=False,
            turn_outcome_response_kind="answer",
        ),
    )
    setup_workflow = SimpleNamespace(workflow_id="wf-canonical")
    _, workflow_params = setup_new_copilot_mocks(monkeypatch, chat, setup_workflow, agent_result)
    stream = MagicMock(send=AsyncMock(return_value=True))

    await workflow_copilot_route._finalise_normal_turn(
        stream=stream,
        chat=chat,
        organization_id="org-1",
        original_workflow=None,
        chat_request=_make_chat_request(),
        agent_result=agent_result,
    )

    response_frame = stream.send.await_args.args[0]
    assert response_frame.message == answer
    assert response_frame.terminal_envelope is not None
    assert response_frame.terminal_envelope["response_kind"] == "answer"
    assert response_frame.terminal_envelope["user_action_required"] is False
    persisted = workflow_params.create_workflow_copilot_chat_message.await_args_list[-1].kwargs
    assert persisted["content"] == answer
    assert persisted["turn_outcome"].response_kind is ResponseKind.ANSWER


@pytest.mark.asyncio
async def test_finalise_normal_turn_flag_on_completed_envelope_keeps_message_but_sets_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        app.AGENT_FUNCTION,
        "should_render_copilot_terminal_from_envelope",
        AsyncMock(return_value=True),
    )
    chat = SimpleNamespace(
        organization_id="org-1",
        workflow_copilot_chat_id="chat-1",
        proposed_workflow=None,
        auto_accept=True,
    )
    original_workflow = SimpleNamespace(workflow_id="wf-canonical")
    updated_workflow = MagicMock()
    updated_workflow.model_dump.return_value = {"workflow_id": "wf-draft"}
    agent_result = AgentResult(
        user_response="done",
        updated_workflow=updated_workflow,
        global_llm_context=None,
        response_type="REPLY",
        proposal_disposition="auto_applicable",
        narrative_payload=_narrative_payload(),
        terminal_envelope=_terminal_payload(verified=True, workflow_applied=False),
    )
    _, workflow_params = setup_new_copilot_mocks(monkeypatch, chat, original_workflow, agent_result)
    stream = MagicMock(send=AsyncMock(return_value=True))

    await workflow_copilot_route._finalise_normal_turn(
        stream=stream,
        chat=chat,
        organization_id="org-1",
        original_workflow=original_workflow,
        chat_request=_make_chat_request(),
        agent_result=agent_result,
    )

    response_frame = stream.send.await_args.args[0]
    assert isinstance(response_frame, WorkflowCopilotStreamResponseUpdate)
    assert response_frame.message == "done"
    assert response_frame.terminal_envelope is not None
    assert response_frame.terminal_envelope["next_state"] == "completed"
    assert response_frame.terminal_envelope["rendered_from_envelope"] is True

    persisted_payload = workflow_params.create_workflow_copilot_chat_message.await_args_list[-1].kwargs[
        "narrative_payload"
    ]
    assert persisted_payload is not None
    assert persisted_payload["terminalEnvelope"]["rendered_from_envelope"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("finalizer", ["normal", "cancel"], ids=["normal-turn", "cancel-turn"])
async def test_terminal_envelope_finalization_failure_omits_envelope(
    monkeypatch: pytest.MonkeyPatch,
    finalizer: str,
) -> None:
    chat = SimpleNamespace(
        organization_id="org-1",
        workflow_copilot_chat_id="chat-1",
        proposed_workflow=None,
        auto_accept=(finalizer == "cancel"),
    )
    original_workflow = SimpleNamespace(workflow_id="wf-canonical")
    updated_workflow = MagicMock()
    updated_workflow.model_dump.return_value = {"workflow_id": "wf-draft"}
    agent_result = AgentResult(
        user_response="done" if finalizer == "normal" else "Cancelled by user.",
        updated_workflow=updated_workflow if finalizer == "normal" else None,
        global_llm_context=None,
        response_type="REPLY",
        proposal_disposition="auto_applicable",
        cancelled=(finalizer == "cancel"),
        narrative_payload=_narrative_payload(),
        terminal_envelope=_terminal_payload(verified=True, workflow_applied=False),
    )
    _, workflow_params = setup_new_copilot_mocks(monkeypatch, chat, original_workflow, agent_result)
    stream = MagicMock(send=AsyncMock(return_value=True))

    def _raise(*_: Any, **__: Any) -> Any:
        raise RuntimeError("finalize failed")

    monkeypatch.setattr(workflow_copilot_route, "finalize_applied_state", _raise)

    if finalizer == "normal":
        await workflow_copilot_route._finalise_normal_turn(
            stream=stream,
            chat=chat,
            organization_id="org-1",
            original_workflow=original_workflow,
            chat_request=_make_chat_request(),
            agent_result=agent_result,
        )
    else:
        await workflow_copilot_route._persist_cancel_turn(
            stream=stream,
            chat=chat,
            organization_id="org-1",
            original_workflow=original_workflow,
            user_message="stop",
            agent_result=agent_result,
        )

    response_frame = stream.send.await_args.args[0]
    assert isinstance(response_frame, WorkflowCopilotStreamResponseUpdate)
    assert response_frame.terminal_envelope is None
    if finalizer == "cancel":
        assert response_frame.cancelled is True

    persisted_payload = workflow_params.create_workflow_copilot_chat_message.await_args_list[-1].kwargs[
        "narrative_payload"
    ]
    assert persisted_payload is not None
    assert "terminalEnvelope" not in persisted_payload


@pytest.mark.asyncio
async def test_get_debug_run_info_bounds_visible_elements_html(monkeypatch: pytest.MonkeyPatch) -> None:
    max_chars = workflow_copilot_route.WORKFLOW_COPILOT_DEBUG_HTML_MAX_CHARS
    html = "HEAD_MARKER" + ("x" * (max_chars * 2)) + "TAIL_MARKER"
    artifact = SimpleNamespace()
    block = SimpleNamespace(
        label="block-1",
        block_type=SimpleNamespace(name="task"),
        status="failed",
        failure_reason="timed out",
    )
    monkeypatch.setattr(
        workflow_copilot_route,
        "_get_debug_artifact",
        AsyncMock(return_value=artifact),
    )
    monkeypatch.setattr(
        app.DATABASE,
        "observer",
        SimpleNamespace(get_workflow_run_blocks=AsyncMock(return_value=[block])),
    )
    monkeypatch.setattr(
        app,
        "ARTIFACT_MANAGER",
        SimpleNamespace(retrieve_artifact=AsyncMock(return_value=html.encode("utf-8"))),
    )

    run_info = await workflow_copilot_route._get_debug_run_info("org-1", "wr-1")

    assert run_info is not None
    assert run_info.html is not None
    assert len(run_info.html) < len(html)
    assert len(run_info.html) <= max_chars + 100
    assert "HEAD_MARKER" in run_info.html
    assert "TAIL_MARKER" in run_info.html
    assert "truncated by Skyvern" in run_info.html


@pytest.mark.asyncio
async def test_terminal_envelope_finalization_failure_flag_on_fails_closed_to_legacy_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        app.AGENT_FUNCTION,
        "should_render_copilot_terminal_from_envelope",
        AsyncMock(return_value=True),
    )
    chat = SimpleNamespace(
        organization_id="org-1",
        workflow_copilot_chat_id="chat-1",
        proposed_workflow=None,
        auto_accept=False,
    )
    original_workflow = SimpleNamespace(workflow_id="wf-canonical")
    updated_workflow = MagicMock()
    updated_workflow.model_dump.return_value = {"workflow_id": "wf-draft"}
    agent_result = AgentResult(
        user_response="done",
        updated_workflow=updated_workflow,
        global_llm_context=None,
        response_type="REPLY",
        proposal_disposition="auto_applicable",
        narrative_payload=_narrative_payload(),
        terminal_envelope=_terminal_payload(verified=False, workflow_applied=False),
    )
    _, workflow_params = setup_new_copilot_mocks(monkeypatch, chat, original_workflow, agent_result)
    stream = MagicMock(send=AsyncMock(return_value=True))

    def _raise(*_: Any, **__: Any) -> Any:
        raise RuntimeError("finalize failed")

    monkeypatch.setattr(workflow_copilot_route, "finalize_applied_state", _raise)

    await workflow_copilot_route._finalise_normal_turn(
        stream=stream,
        chat=chat,
        organization_id="org-1",
        original_workflow=original_workflow,
        chat_request=_make_chat_request(),
        agent_result=agent_result,
    )

    response_frame = stream.send.await_args.args[0]
    assert isinstance(response_frame, WorkflowCopilotStreamResponseUpdate)
    assert response_frame.message == "done"
    assert response_frame.terminal_envelope is None
    persisted_writes = workflow_params.create_workflow_copilot_chat_message.await_args_list
    assert persisted_writes[-1].kwargs["content"] == "done"


@pytest.mark.asyncio
async def test_cancel_turn_agent_result_flag_on_marks_envelope_but_keeps_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        app.AGENT_FUNCTION,
        "should_render_copilot_terminal_from_envelope",
        AsyncMock(return_value=True),
    )
    chat = SimpleNamespace(
        organization_id="org-1",
        workflow_copilot_chat_id="chat-1",
        proposed_workflow=None,
        auto_accept=True,
    )
    original_workflow = SimpleNamespace(workflow_id="wf-canonical")
    agent_result = AgentResult(
        user_response="Cancelled by user.",
        updated_workflow=None,
        global_llm_context=None,
        response_type="REPLY",
        proposal_disposition="auto_applicable",
        cancelled=True,
        narrative_payload=_narrative_payload(),
        terminal_envelope=_terminal_payload(verified=True, workflow_applied=True),
    )
    _, workflow_params = setup_new_copilot_mocks(monkeypatch, chat, original_workflow, agent_result)
    stream = MagicMock(send=AsyncMock(return_value=True))

    await workflow_copilot_route._persist_cancel_turn(
        stream=stream,
        chat=chat,
        organization_id="org-1",
        original_workflow=original_workflow,
        user_message="stop",
        agent_result=agent_result,
    )

    response_frame = stream.send.await_args.args[0]
    assert isinstance(response_frame, WorkflowCopilotStreamResponseUpdate)
    assert response_frame.message == "Cancelled by user."
    assert response_frame.terminal_envelope is not None
    assert response_frame.terminal_envelope["rendered_from_envelope"] is True
    persisted_writes = workflow_params.create_workflow_copilot_chat_message.await_args_list
    assert persisted_writes[-1].kwargs["content"] == "Cancelled by user."
    persisted_payload = persisted_writes[-1].kwargs["narrative_payload"]
    assert persisted_payload is not None
    assert persisted_payload["terminalEnvelope"]["rendered_from_envelope"] is True


@pytest.mark.asyncio
async def test_pre_agent_cancel_path_keeps_literal_cancelled_by_user(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        app.AGENT_FUNCTION,
        "should_render_copilot_terminal_from_envelope",
        AsyncMock(return_value=True),
    )
    chat = SimpleNamespace(
        organization_id="org-1",
        workflow_copilot_chat_id="chat-1",
        proposed_workflow=None,
        auto_accept=False,
    )
    original_workflow = SimpleNamespace(workflow_id="wf-canonical")
    _, workflow_params = setup_new_copilot_mocks(monkeypatch, chat, original_workflow, SimpleNamespace())
    stream = MagicMock(send=AsyncMock(return_value=True))

    await workflow_copilot_route._persist_cancel_turn(
        stream=stream,
        chat=chat,
        organization_id="org-1",
        original_workflow=original_workflow,
        user_message="stop",
        agent_result=None,
    )

    response_frame = stream.send.await_args.args[0]
    assert isinstance(response_frame, WorkflowCopilotStreamResponseUpdate)
    assert response_frame.message == "Cancelled by user."
    assert response_frame.terminal_envelope is None
    persisted_writes = workflow_params.create_workflow_copilot_chat_message.await_args_list
    assert persisted_writes[-1].kwargs["content"] == "Cancelled by user."


@pytest.mark.asyncio
async def test_agent_functions_oss_terminal_envelope_render_resolver_uses_settings_toggle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolver = AgentFunction()

    monkeypatch.setattr(settings, "WORKFLOW_COPILOT_TERMINAL_ENVELOPE_RENDER", False)
    assert await resolver.should_render_copilot_terminal_from_envelope("org-1") is False

    monkeypatch.setattr(settings, "WORKFLOW_COPILOT_TERMINAL_ENVELOPE_RENDER", True)
    assert await resolver.should_render_copilot_terminal_from_envelope("org-1") is True


@pytest.mark.asyncio
async def test_chat_audio_upload_stores_artifact_for_existing_chat(monkeypatch: pytest.MonkeyPatch) -> None:
    chat = SimpleNamespace(
        workflow_copilot_chat_id="chat-1",
        workflow_permanent_id="wpid-1",
        organization_id="org-1",
    )
    monkeypatch.setattr(
        app.DATABASE,
        "workflow_params",
        SimpleNamespace(
            get_workflow_copilot_chat_by_id=AsyncMock(return_value=chat),
            create_workflow_copilot_chat=AsyncMock(),
        ),
    )
    artifact_manager = SimpleNamespace(
        create_log_artifact=AsyncMock(return_value="a_audio"),
        wait_for_upload_aiotasks=AsyncMock(),
    )
    monkeypatch.setattr(app, "ARTIFACT_MANAGER", artifact_manager)
    file = SimpleNamespace(
        content_type="audio/webm",
        read=AsyncMock(return_value=b"audio-bytes"),
        close=AsyncMock(),
    )

    response = await workflow_copilot_chat_audio(
        workflow_permanent_id="wpid-1",
        workflow_copilot_chat_id="chat-1",
        file=file,
        organization=SimpleNamespace(organization_id="org-1"),
    )

    assert response.workflow_copilot_chat_id == "chat-1"
    assert response.audio_artifact_id == "a_audio"
    app.DATABASE.workflow_params.create_workflow_copilot_chat.assert_not_called()
    artifact_manager.create_log_artifact.assert_awaited_once()
    artifact_manager.wait_for_upload_aiotasks.assert_awaited_once_with(["chat-1"])
    file.read.assert_awaited_once_with(settings.MAX_UPLOAD_FILE_SIZE + 1)
    file.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_chat_audio_upload_rejects_unsupported_audio_content_type() -> None:
    file = SimpleNamespace(
        content_type="audio/fake-binary",
        read=AsyncMock(return_value=b"audio-bytes"),
        close=AsyncMock(),
    )

    with pytest.raises(HTTPException) as exc_info:
        await workflow_copilot_chat_audio(
            workflow_permanent_id="wpid-1",
            workflow_copilot_chat_id="chat-1",
            file=file,
            organization=SimpleNamespace(organization_id="org-1"),
        )

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "Unsupported audio format"
    file.read.assert_not_awaited()
    file.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_chat_audio_upload_rejects_oversized_audio_without_reading_past_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "MAX_UPLOAD_FILE_SIZE", 4)
    file = SimpleNamespace(
        content_type="audio/webm;codecs=opus",
        read=AsyncMock(return_value=b"12345"),
        close=AsyncMock(),
    )

    with pytest.raises(HTTPException) as exc_info:
        await workflow_copilot_chat_audio(
            workflow_permanent_id="wpid-1",
            workflow_copilot_chat_id="chat-1",
            file=file,
            organization=SimpleNamespace(organization_id="org-1"),
        )

    assert exc_info.value.status_code == 413
    file.read.assert_awaited_once_with(5)
    file.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_validate_copilot_audio_artifact_id_rejects_foreign_artifact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = SimpleNamespace(
        artifact_type=ArtifactType.AUDIO,
        uri="s3://bucket/v1/local/org-1/logs/workflow_copilot_chat/chat-2/t.webm",
    )
    monkeypatch.setattr(
        app.DATABASE,
        "artifacts",
        SimpleNamespace(get_artifact_by_id=AsyncMock(return_value=artifact)),
    )

    with pytest.raises(HTTPException) as exc_info:
        await _validate_copilot_audio_artifact_id(
            audio_artifact_id="a_audio",
            organization_id="org-1",
            workflow_copilot_chat_id="chat-1",
        )

    assert exc_info.value.status_code == 400
    assert "not linked" in exc_info.value.detail


@pytest.mark.asyncio
async def test_validate_copilot_audio_artifact_id_accepts_chat_scoped_artifact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = SimpleNamespace(
        artifact_type=ArtifactType.AUDIO,
        uri="s3://bucket/v1/local/org-1/logs/workflow_copilot_chat/chat-1/t.webm",
    )
    monkeypatch.setattr(
        app.DATABASE,
        "artifacts",
        SimpleNamespace(get_artifact_by_id=AsyncMock(return_value=artifact)),
    )

    validated = await _validate_copilot_audio_artifact_id(
        audio_artifact_id="a_audio",
        organization_id="org-1",
        workflow_copilot_chat_id="chat-1",
    )

    assert validated == "a_audio"


def test_terminal_narrative_metadata_preserves_payload_and_adds_contract_fields() -> None:
    payload = {
        "turnId": "turn-1",
        "turnIndex": 0,
        "mode": "build",
        "designStarted": True,
        "designEnded": True,
        "draft": {"blockCount": 1, "blockLabels": ["open_page"], "summary": None},
        "blocks": [],
        "terminal": "response",
        "terminalMessage": "Cancelled.",
        "narrativeSummary": "Cancelled.",
        "priorBlockCount": None,
        "designActivity": [],
        "startedAt": "2026-05-25T00:00:00Z",
        "endedAt": "2026-05-25T00:00:05Z",
    }

    enriched = workflow_copilot_route._with_terminal_narrative_metadata(
        payload,
        cancelled=True,
        proposal_disposition="review_untested",
    )

    assert enriched is not None
    assert enriched["cancelled"] is True
    assert enriched["proposalDisposition"] == "review_untested"
    assert enriched["draft"] == payload["draft"]
    assert "cancelled" not in payload
    assert "proposalDisposition" not in payload


def _install_mock_provider(
    monkeypatch: pytest.MonkeyPatch,
    *,
    return_value: bool | None = None,
    side_effect: BaseException | None = None,
) -> AsyncMock:
    mock_provider = AsyncMock()
    if side_effect is not None:
        mock_provider.is_feature_enabled_cached.side_effect = side_effect
    else:
        mock_provider.is_feature_enabled_cached.return_value = return_value
    monkeypatch.setattr(app, "EXPERIMENTATION_PROVIDER", mock_provider)
    return mock_provider


@pytest.mark.asyncio
async def test_request_mode_ask_forces_v1_over_flag_on(
    monkeypatch: pytest.MonkeyPatch, anon_request: MagicMock, organization: SimpleNamespace
) -> None:
    """mode='ask' must take the v1 path even when the settings flag is on."""
    monkeypatch.setattr(settings, "ENABLE_WORKFLOW_COPILOT_V2", True)

    new_copilot_mock = AsyncMock(side_effect=AssertionError("mode='ask' must not reach the v2 path"))
    monkeypatch.setattr(
        "skyvern.forge.sdk.routes.workflow_copilot._new_copilot_chat_post",
        new_copilot_mock,
    )

    captured = install_fake_create(monkeypatch)

    response = await workflow_copilot_chat_post(anon_request, _make_chat_request(mode="ask"), organization)

    assert response is captured["sentinel"]
    new_copilot_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_request_mode_build_forces_v2_over_flag_off(
    monkeypatch: pytest.MonkeyPatch, anon_request: MagicMock, organization: SimpleNamespace
) -> None:
    """mode='build' must take the v2 path even when the settings flag is off."""
    monkeypatch.setattr(settings, "ENABLE_WORKFLOW_COPILOT_V2", False)
    _install_mock_provider(monkeypatch, return_value=False)

    sentinel = object()
    new_copilot_mock = AsyncMock(return_value=sentinel)
    monkeypatch.setattr(
        "skyvern.forge.sdk.routes.workflow_copilot._new_copilot_chat_post",
        new_copilot_mock,
    )

    response = await workflow_copilot_chat_post(anon_request, _make_chat_request(mode="build"), organization)

    assert response is sentinel
    new_copilot_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_request_mode_absent_follows_flag(
    monkeypatch: pytest.MonkeyPatch, anon_request: MagicMock, organization: SimpleNamespace
) -> None:
    """mode absent (None) keeps following the settings flag in both directions."""
    new_copilot_mock = AsyncMock(return_value=object())
    monkeypatch.setattr(
        "skyvern.forge.sdk.routes.workflow_copilot._new_copilot_chat_post",
        new_copilot_mock,
    )
    captured = install_fake_create(monkeypatch)

    monkeypatch.setattr(settings, "ENABLE_WORKFLOW_COPILOT_V2", False)
    _install_mock_provider(monkeypatch, return_value=False)
    response = await workflow_copilot_chat_post(anon_request, _make_chat_request(mode=None), organization)
    assert response is captured["sentinel"]
    new_copilot_mock.assert_not_awaited()

    monkeypatch.setattr(settings, "ENABLE_WORKFLOW_COPILOT_V2", True)
    response = await workflow_copilot_chat_post(anon_request, _make_chat_request(mode=None), organization)
    assert response is new_copilot_mock.return_value
    new_copilot_mock.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("auto_accept", "workflow_was_persisted", "has_valid_proposal", "expect_restore"),
    [
        # auto_accept + valid proposal => keep the DB write (frontend applied it)
        (True, True, True, False),
        # auto_accept + no proposal (SKY-9143) => restore the unverified mid-turn write
        (True, True, False, True),
        # Nothing was persisted => nothing to restore regardless of other flags
        (False, False, False, False),
        # Normal mid-stream disconnect with a persisted draft and no proposal => restore
        (False, True, False, True),
        # Normal mid-stream disconnect with a persisted draft and a valid proposal =>
        # still restore, user accepts via the panel to re-apply
        (False, True, True, True),
    ],
)
async def test_flag_on_mid_stream_disconnect_restores_when_persisted_and_not_auto_accept(
    monkeypatch: pytest.MonkeyPatch,
    auto_accept: bool,
    workflow_was_persisted: bool,
    has_valid_proposal: bool,
    expect_restore: bool,
    api_key_request: MagicMock,
    organization: SimpleNamespace,
) -> None:
    monkeypatch.setattr(settings, "ENABLE_WORKFLOW_COPILOT_V2", True)

    captured = install_fake_create(monkeypatch)

    chat = SimpleNamespace(
        workflow_copilot_chat_id="chat-1",
        workflow_permanent_id="wpid-1",
        organization_id="org-1",
        proposed_workflow=None,
        auto_accept=auto_accept,
    )
    original_workflow = SimpleNamespace(
        workflow_id="wf-canonical",
        title="Original",
        description="Original description",
        workflow_definition=None,
    )
    proposal = MagicMock(spec=["model_dump"]) if has_valid_proposal else None
    if proposal is not None:
        proposal.model_dump.return_value = {"workflow_id": "wf-canonical"}
    agent_result = SimpleNamespace(
        user_response="done",
        updated_workflow=proposal,
        global_llm_context=None,
        workflow_yaml=None,
        workflow_was_persisted=workflow_was_persisted,
        clear_proposed_workflow=False,
        proposal_disposition="auto_applicable",
        turn_outcome=None,
    )

    restore_mock, _ = setup_new_copilot_mocks(monkeypatch, chat, original_workflow, agent_result)

    response = await workflow_copilot_chat_post(api_key_request, _make_chat_request(), organization)
    assert response is captured["sentinel"]

    stream = MagicMock()
    stream.send = AsyncMock(return_value=True)
    # First call (before agent loop) -> False, second call (after agent loop) -> True
    # simulates a mid-stream client disconnect after the agent returned.
    stream.is_disconnected = AsyncMock(side_effect=[False, True])

    handler = captured["handler"]
    assert callable(handler)
    await handler(stream)

    if expect_restore:
        restore_mock.assert_awaited_once()
    else:
        restore_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_flag_on_pre_agent_failure_persists_recoverable_reply(
    monkeypatch: pytest.MonkeyPatch, anon_request: MagicMock, copilot_stream: MagicMock, organization: SimpleNamespace
) -> None:
    monkeypatch.setattr(settings, "ENABLE_WORKFLOW_COPILOT_V2", True)
    captured = install_fake_create(monkeypatch)

    chat = SimpleNamespace(
        workflow_copilot_chat_id="chat-1",
        workflow_permanent_id="wpid-1",
        organization_id="org-1",
        proposed_workflow=None,
        auto_accept=False,
    )
    original_workflow = SimpleNamespace(
        workflow_id="wf-canonical",
        title="Original",
        description="Original description",
        workflow_definition=None,
    )

    async def fake_llm_handler(*args: object, **kwargs: object) -> None:
        del args, kwargs
        return None

    monkeypatch.setattr(
        "skyvern.forge.sdk.routes.workflow_copilot.resolve_main_copilot_handler",
        fake_llm_handler,
    )
    monkeypatch.setattr(
        "skyvern.forge.sdk.routes.workflow_copilot._restore_workflow_definition",
        AsyncMock(),
    )
    monkeypatch.setattr(agent_module, "ensure_tracing_initialized", lambda: None)
    monkeypatch.setattr(
        agent_module,
        "_run_copilot_turn_impl",
        AsyncMock(side_effect=agent_module.CopilotRequestPolicyMissingError()),
    )

    workflow_params = SimpleNamespace(
        get_workflow_copilot_chat_by_id=AsyncMock(return_value=chat),
        get_workflow_copilot_chat_messages=AsyncMock(return_value=[]),
        update_workflow_copilot_chat=AsyncMock(),
        create_workflow_copilot_chat_message=AsyncMock(
            return_value=SimpleNamespace(created_at=datetime(2026, 4, 14, tzinfo=timezone.utc))
        ),
    )
    app.DATABASE.workflow_params = workflow_params
    app.DATABASE.workflows = SimpleNamespace(
        get_workflow_by_permanent_id=AsyncMock(return_value=original_workflow),
    )
    app.DATABASE.observer = SimpleNamespace(
        get_workflow_run_blocks=AsyncMock(return_value=[]),
    )
    app.AGENT_FUNCTION.get_copilot_security_rules = MagicMock(return_value="")
    app.AGENT_FUNCTION.get_copilot_config = MagicMock(return_value=None)
    app.AGENT_FUNCTION.get_copilot_config_for_request = AsyncMock(return_value=None)
    app.AGENT_FUNCTION.resolve_org_api_key = AsyncMock(return_value="sk-test-key")

    response = await workflow_copilot_chat_post(anon_request, _make_chat_request(), organization)
    assert response is captured["sentinel"]

    handler = captured["handler"]
    assert callable(handler)
    await handler(copilot_stream)

    contents = [
        call.kwargs.get("content") for call in workflow_params.create_workflow_copilot_chat_message.await_args_list
    ]
    assert "Please update it" in contents
    assistant_contents = [content for content in contents if isinstance(content, str) and content != "Please update it"]
    assert len(assistant_contents) == 1
    assert "An unexpected error occurred. Please try again." not in assistant_contents[0]
    assert "Copilot hit an internal error before it could finish this turn" in assistant_contents[0]
    assert "The workflow was not modified" in assistant_contents[0]
    assert "reference cpe_" in assistant_contents[0]

    frames = [call.args[0] for call in copilot_stream.send.await_args_list if call.args]
    response_frames = [frame for frame in frames if isinstance(frame, WorkflowCopilotStreamResponseUpdate)]
    assert response_frames
    assert response_frames[-1].narrative_payload is not None
    assert response_frames[-1].narrative_payload["terminal"] == "error"
    assert not any(isinstance(frame, WorkflowCopilotStreamErrorUpdate) for frame in frames)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "expected_summary"),
    [
        (RuntimeError("route boom"), "Copilot hit an internal error before it could finish this turn"),
        (LLMProviderError("OPENAI_GPT5_5"), "A Copilot dependency stopped responding"),
    ],
)
async def test_flag_on_route_error_after_chat_persists_recoverable_reply(
    monkeypatch: pytest.MonkeyPatch,
    error: BaseException,
    expected_summary: str,
    api_key_request: MagicMock,
    copilot_stream: MagicMock,
    organization: SimpleNamespace,
) -> None:
    monkeypatch.setattr(settings, "ENABLE_WORKFLOW_COPILOT_V2", True)
    captured = install_fake_create(monkeypatch)

    chat = SimpleNamespace(
        workflow_copilot_chat_id="chat-1",
        workflow_permanent_id="wpid-1",
        organization_id="org-1",
        proposed_workflow=None,
        auto_accept=False,
    )
    original_workflow = SimpleNamespace(
        workflow_id="wf-canonical",
        title="Original",
        description="Original description",
        workflow_definition=None,
    )
    agent_result = SimpleNamespace(
        user_response="unused",
        updated_workflow=None,
        global_llm_context=None,
        workflow_yaml=None,
        workflow_was_persisted=False,
        clear_proposed_workflow=False,
        unvalidated=False,
        turn_outcome=None,
    )
    setup_new_copilot_mocks(monkeypatch, chat, original_workflow, agent_result)
    monkeypatch.setattr(
        "skyvern.forge.sdk.routes.workflow_copilot.run_copilot_agent",
        AsyncMock(side_effect=error),
    )

    response = await workflow_copilot_chat_post(api_key_request, _make_chat_request(), organization)
    assert response is captured["sentinel"]

    handler = captured["handler"]
    assert callable(handler)
    await handler(copilot_stream)

    contents = [
        call.kwargs.get("content")
        for call in app.DATABASE.workflow_params.create_workflow_copilot_chat_message.await_args_list
    ]
    assert app.DATABASE.workflow_params.start_copilot_turn.await_args.kwargs["user_message"] == "Please update it"
    assistant_contents = [content for content in contents if isinstance(content, str)]
    assert len(assistant_contents) == 1
    assert expected_summary in assistant_contents[0]
    assert "The workflow was not modified" in assistant_contents[0]
    assert "reference cpe_" in assistant_contents[0]

    assistant_messages = [
        call.kwargs
        for call in app.DATABASE.workflow_params.create_workflow_copilot_chat_message.await_args_list
        if call.kwargs.get("sender") == WorkflowCopilotChatSender.AI
    ]
    assert len(assistant_messages) == 1
    turn_outcome = assistant_messages[0]["turn_outcome"]
    assert turn_outcome is not None
    assert turn_outcome.response_kind == "recover"
    assert turn_outcome.terminal_reason == workflow_copilot_route.COPILOT_RECOVERABLE_FAILURE_TERMINAL_REASON
    assert turn_outcome.copilot_effective_mode == "build"
    assert turn_outcome.copilot_turn_id is not None

    frames = [call.args[0] for call in copilot_stream.send.await_args_list if call.args]
    response_frames = [frame for frame in frames if isinstance(frame, WorkflowCopilotStreamResponseUpdate)]
    assert response_frames
    assert response_frames[-1].narrative_payload is not None
    assert response_frames[-1].narrative_payload["terminal"] == "error"
    assert not any(isinstance(frame, WorkflowCopilotStreamErrorUpdate) for frame in frames)


@pytest.mark.asyncio
async def test_route_error_after_restore_reports_workflow_not_modified(
    monkeypatch: pytest.MonkeyPatch,
    api_key_request: MagicMock,
    copilot_stream: MagicMock,
    organization: SimpleNamespace,
) -> None:
    monkeypatch.setattr(settings, "ENABLE_WORKFLOW_COPILOT_V2", True)
    captured = install_fake_create(monkeypatch)

    chat = SimpleNamespace(
        workflow_copilot_chat_id="chat-1",
        workflow_permanent_id="wpid-1",
        organization_id="org-1",
        proposed_workflow={"workflow_id": "stale"},
        auto_accept=False,
    )
    original_workflow = SimpleNamespace(
        workflow_id="wf-canonical",
        title="Original",
        description="Original description",
        workflow_definition=None,
    )
    agent_result = SimpleNamespace(
        user_response="unused",
        updated_workflow=None,
        global_llm_context=None,
        workflow_yaml=None,
        workflow_was_persisted=True,
        clear_proposed_workflow=False,
        unvalidated=False,
        turn_outcome=None,
    )
    restore_mock, _ = setup_new_copilot_mocks(monkeypatch, chat, original_workflow, agent_result)
    finalise_results: list[object] = []
    original_finalise = workflow_copilot_route._finalise_normal_turn

    async def flaky_finalise(*args: object, **kwargs: object) -> object:
        finalise_results.append(kwargs["agent_result"])
        if len(finalise_results) == 1:
            raise RuntimeError("post-agent route boom")
        return await original_finalise(*args, **kwargs)

    monkeypatch.setattr(workflow_copilot_route, "_finalise_normal_turn", flaky_finalise)

    response = await workflow_copilot_chat_post(api_key_request, _make_chat_request(), organization)
    assert response is captured["sentinel"]

    handler = captured["handler"]
    assert callable(handler)
    await handler(copilot_stream)

    restore_mock.assert_awaited_once()
    assert len(finalise_results) == 2
    recovered_result = finalise_results[1]
    assert "The workflow was not modified" in recovered_result.user_response
    assert "The workflow was preserved" not in recovered_result.user_response
    assert recovered_result.clear_proposed_workflow is True
    contents = [
        call.kwargs.get("content")
        for call in app.DATABASE.workflow_params.create_workflow_copilot_chat_message.await_args_list
    ]
    assistant_contents = [content for content in contents if isinstance(content, str) and content != "Please update it"]
    assert len(assistant_contents) == 1
    assert "The workflow was not modified" in assistant_contents[0]
    assert "The workflow was preserved" not in assistant_contents[0]
    update_calls = app.DATABASE.workflow_params.update_workflow_copilot_chat.await_args_list
    clear_calls = [c for c in update_calls if c.kwargs.get("proposed_workflow") is None]
    assert len(clear_calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raised_error",
    [RuntimeError("post-agent route boom"), LLMProviderError("OPENAI_GPT5_5")],
    ids=["generic-exception-handler", "llm-provider-error-handler"],
)
async def test_route_error_after_restore_keeps_bypassed_proposal_when_keep_pending(
    monkeypatch: pytest.MonkeyPatch,
    raised_error: BaseException,
    api_key_request: MagicMock,
    copilot_stream: MagicMock,
    organization: SimpleNamespace,
) -> None:
    # Route-level (not direct-function-call) pin: keep_pending_proposal must
    # reach both exception-recovery call sites, not just _persist_proposed_workflow_state.
    monkeypatch.setattr(settings, "ENABLE_WORKFLOW_COPILOT_V2", True)
    captured = install_fake_create(monkeypatch)

    chat = SimpleNamespace(
        workflow_copilot_chat_id="chat-1",
        workflow_permanent_id="wpid-1",
        organization_id="org-1",
        proposed_workflow={"workflow_id": "stale"},
        auto_accept=False,
    )
    original_workflow = SimpleNamespace(
        workflow_id="wf-canonical",
        title="Original",
        description="Original description",
        workflow_definition=None,
    )
    agent_result = SimpleNamespace(
        user_response="unused",
        updated_workflow=None,
        global_llm_context=None,
        workflow_yaml=None,
        workflow_was_persisted=True,
        clear_proposed_workflow=False,
        unvalidated=False,
        turn_outcome=None,
    )
    restore_mock, _ = setup_new_copilot_mocks(monkeypatch, chat, original_workflow, agent_result)
    finalise_results: list[object] = []
    original_finalise = workflow_copilot_route._finalise_normal_turn

    async def flaky_finalise(*args: object, **kwargs: object) -> object:
        finalise_results.append(kwargs["agent_result"])
        if len(finalise_results) == 1:
            raise raised_error
        return await original_finalise(*args, **kwargs)

    monkeypatch.setattr(workflow_copilot_route, "_finalise_normal_turn", flaky_finalise)

    response = await workflow_copilot_chat_post(
        api_key_request, _make_chat_request(keep_pending_proposal=True), organization
    )
    assert response is captured["sentinel"]

    handler = captured["handler"]
    assert callable(handler)
    await handler(copilot_stream)

    restore_mock.assert_awaited_once()
    assert len(finalise_results) == 2
    recovered_result = finalise_results[1]
    assert recovered_result.clear_proposed_workflow is False
    update_calls = app.DATABASE.workflow_params.update_workflow_copilot_chat.await_args_list
    clear_calls = [c for c in update_calls if c.kwargs.get("proposed_workflow") is None]
    assert not clear_calls, f"keep_pending_proposal=True must survive restore-driven recovery, got {update_calls!r}"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raised_error",
    [RuntimeError("post-agent route boom"), LLMProviderError("OPENAI_GPT5_5")],
    ids=["generic-exception-handler", "llm-provider-error-handler"],
)
async def test_route_error_honors_real_agent_explicit_clear_despite_keep_pending(
    monkeypatch: pytest.MonkeyPatch,
    raised_error: BaseException,
    api_key_request: MagicMock,
    copilot_stream: MagicMock,
    organization: SimpleNamespace,
) -> None:
    # The real (pre-exception) agent_result can itself carry clear_proposed_workflow=True;
    # the recovery path must not silently drop that signal just because it's rebuilding
    # a synthetic result for the error reply.
    monkeypatch.setattr(settings, "ENABLE_WORKFLOW_COPILOT_V2", True)
    captured = install_fake_create(monkeypatch)

    chat = SimpleNamespace(
        workflow_copilot_chat_id="chat-1",
        workflow_permanent_id="wpid-1",
        organization_id="org-1",
        proposed_workflow={"workflow_id": "stale"},
        auto_accept=False,
    )
    original_workflow = SimpleNamespace(
        workflow_id="wf-canonical",
        title="Original",
        description="Original description",
        workflow_definition=None,
    )
    agent_result = SimpleNamespace(
        user_response="unused",
        updated_workflow=None,
        global_llm_context=None,
        workflow_yaml=None,
        workflow_was_persisted=True,
        clear_proposed_workflow=True,
        unvalidated=False,
        turn_outcome=None,
    )
    restore_mock, _ = setup_new_copilot_mocks(monkeypatch, chat, original_workflow, agent_result)
    finalise_results: list[object] = []
    original_finalise = workflow_copilot_route._finalise_normal_turn

    async def flaky_finalise(*args: object, **kwargs: object) -> object:
        finalise_results.append(kwargs["agent_result"])
        if len(finalise_results) == 1:
            raise raised_error
        return await original_finalise(*args, **kwargs)

    monkeypatch.setattr(workflow_copilot_route, "_finalise_normal_turn", flaky_finalise)

    response = await workflow_copilot_chat_post(
        api_key_request, _make_chat_request(keep_pending_proposal=True), organization
    )
    assert response is captured["sentinel"]

    handler = captured["handler"]
    assert callable(handler)
    await handler(copilot_stream)

    restore_mock.assert_awaited_once()
    assert len(finalise_results) == 2
    recovered_result = finalise_results[1]
    assert recovered_result.clear_proposed_workflow is True
    update_calls = app.DATABASE.workflow_params.update_workflow_copilot_chat.await_args_list
    clear_calls = [c for c in update_calls if c.kwargs.get("proposed_workflow") is None]
    assert clear_calls, (
        f"real agent_result.clear_proposed_workflow=True must win even with keep_pending_proposal, got {update_calls!r}"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raised_error",
    [RuntimeError("post-commit route boom"), LLMProviderError("OPENAI_GPT5_5")],
    ids=["generic-exception-handler", "llm-provider-error-handler"],
)
async def test_route_error_after_staged_commit_clears_stale_proposal_despite_keep_pending(
    monkeypatch: pytest.MonkeyPatch,
    raised_error: BaseException,
    api_key_request: MagicMock,
    copilot_stream: MagicMock,
    organization: SimpleNamespace,
) -> None:
    # An auto-accept turn eligible for a staged commit (has_staged_proposal=True) that
    # then hits an exception elsewhere in finalisation still invalidates a stale kept
    # proposal — the recovered synthetic result doesn't carry has_staged_proposal
    # forward, so this must be pre-baked into clear_proposed_workflow at the call site.
    monkeypatch.setattr(settings, "ENABLE_WORKFLOW_COPILOT_V2", True)
    captured = install_fake_create(monkeypatch)

    chat = SimpleNamespace(
        workflow_copilot_chat_id="chat-1",
        workflow_permanent_id="wpid-1",
        organization_id="org-1",
        proposed_workflow={"workflow_id": "stale"},
        auto_accept=True,
    )
    original_workflow = SimpleNamespace(
        workflow_id="wf-canonical",
        title="Original",
        description="Original description",
        workflow_definition=None,
    )
    agent_result = SimpleNamespace(
        user_response="unused",
        updated_workflow=None,
        global_llm_context=None,
        workflow_yaml=None,
        workflow_was_persisted=False,
        clear_proposed_workflow=False,
        has_staged_proposal=True,
        proposal_disposition="auto_applicable",
        unvalidated=False,
        turn_outcome=None,
    )
    restore_mock, _ = setup_new_copilot_mocks(monkeypatch, chat, original_workflow, agent_result)
    finalise_results: list[object] = []
    original_finalise = workflow_copilot_route._finalise_normal_turn

    async def flaky_finalise(*args: object, **kwargs: object) -> object:
        finalise_results.append(kwargs["agent_result"])
        if len(finalise_results) == 1:
            raise raised_error
        return await original_finalise(*args, **kwargs)

    monkeypatch.setattr(workflow_copilot_route, "_finalise_normal_turn", flaky_finalise)

    response = await workflow_copilot_chat_post(
        api_key_request, _make_chat_request(keep_pending_proposal=True), organization
    )
    assert response is captured["sentinel"]

    handler = captured["handler"]
    assert callable(handler)
    await handler(copilot_stream)

    restore_mock.assert_not_awaited()
    assert len(finalise_results) == 2
    recovered_result = finalise_results[1]
    assert recovered_result.clear_proposed_workflow is True
    update_calls = app.DATABASE.workflow_params.update_workflow_copilot_chat.await_args_list
    clear_calls = [c for c in update_calls if c.kwargs.get("proposed_workflow") is None]
    assert clear_calls, (
        f"staged-commit-eligible turn must clear a stale proposal even with keep_pending_proposal, got {update_calls!r}"
    )


@pytest.mark.asyncio
async def test_finalise_normal_turn_clears_stale_proposal_when_rollback_itself_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed rollback leaves canonical's state unverified — keep_pending_proposal
    must not be honored against an assumption ("nothing changed") that didn't hold."""
    chat = SimpleNamespace(
        organization_id="org-1",
        workflow_copilot_chat_id="chat-1",
        proposed_workflow={"workflow_id": "stale"},
        auto_accept=False,
    )
    original_workflow = SimpleNamespace(
        workflow_id="wf-canonical",
        title="Original",
        description="Original description",
        workflow_definition=None,
    )
    agent_result = SimpleNamespace(
        user_response="Here is a plain reply.",
        updated_workflow=None,
        global_llm_context=None,
        workflow_yaml=None,
        workflow_was_persisted=True,
        clear_proposed_workflow=False,
        turn_outcome=None,
    )
    restore_mock, _ = setup_new_copilot_mocks(monkeypatch, chat, original_workflow, agent_result)
    restore_mock.side_effect = RuntimeError("rollback boom")

    stream = MagicMock()
    stream.send = AsyncMock(return_value=True)

    await workflow_copilot_route._finalise_normal_turn(
        stream=stream,
        chat=chat,
        organization_id="org-1",
        original_workflow=original_workflow,
        chat_request=_make_chat_request(keep_pending_proposal=True),
        agent_result=agent_result,
    )

    restore_mock.assert_awaited_once()
    update_calls = app.DATABASE.workflow_params.update_workflow_copilot_chat.await_args_list
    clear_calls = [c for c in update_calls if c.kwargs.get("proposed_workflow") is None]
    assert clear_calls, (
        f"failed rollback must clear a stale proposal even with keep_pending_proposal, got {update_calls!r}"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raised_error",
    [RuntimeError("post-agent route boom"), LLMProviderError("OPENAI_GPT5_5")],
    ids=["generic-exception-handler", "llm-provider-error-handler"],
)
async def test_route_error_recovery_clears_stale_proposal_when_its_own_rollback_fails(
    monkeypatch: pytest.MonkeyPatch,
    raised_error: BaseException,
    api_key_request: MagicMock,
    copilot_stream: MagicMock,
    organization: SimpleNamespace,
) -> None:
    # The recovery block's OWN restore attempt (not the main flow's — flaky_finalise
    # bypasses that entirely) can itself fail; that must still force-clear a kept
    # proposal rather than trust an unverified "rollback succeeded" assumption.
    monkeypatch.setattr(settings, "ENABLE_WORKFLOW_COPILOT_V2", True)
    captured = install_fake_create(monkeypatch)

    chat = SimpleNamespace(
        workflow_copilot_chat_id="chat-1",
        workflow_permanent_id="wpid-1",
        organization_id="org-1",
        proposed_workflow={"workflow_id": "stale"},
        auto_accept=False,
    )
    original_workflow = SimpleNamespace(
        workflow_id="wf-canonical",
        title="Original",
        description="Original description",
        workflow_definition=None,
    )
    agent_result = SimpleNamespace(
        user_response="unused",
        updated_workflow=None,
        global_llm_context=None,
        workflow_yaml=None,
        workflow_was_persisted=True,
        clear_proposed_workflow=False,
        unvalidated=False,
        turn_outcome=None,
    )
    restore_mock, _ = setup_new_copilot_mocks(monkeypatch, chat, original_workflow, agent_result)
    restore_mock.side_effect = RuntimeError("rollback boom")
    finalise_results: list[object] = []
    original_finalise = workflow_copilot_route._finalise_normal_turn

    async def flaky_finalise(*args: object, **kwargs: object) -> object:
        finalise_results.append(kwargs["agent_result"])
        if len(finalise_results) == 1:
            raise raised_error
        return await original_finalise(*args, **kwargs)

    monkeypatch.setattr(workflow_copilot_route, "_finalise_normal_turn", flaky_finalise)

    response = await workflow_copilot_chat_post(
        api_key_request, _make_chat_request(keep_pending_proposal=True), organization
    )
    assert response is captured["sentinel"]

    handler = captured["handler"]
    assert callable(handler)
    await handler(copilot_stream)

    assert len(finalise_results) == 2
    recovered_result = finalise_results[1]
    assert recovered_result.clear_proposed_workflow is True
    update_calls = app.DATABASE.workflow_params.update_workflow_copilot_chat.await_args_list
    clear_calls = [c for c in update_calls if c.kwargs.get("proposed_workflow") is None]
    assert clear_calls, f"a failed recovery-path rollback must clear a stale proposal, got {update_calls!r}"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raised_error",
    [RuntimeError("post-write route boom"), LLMProviderError("OPENAI_GPT5_5")],
    ids=["generic-exception-handler", "llm-provider-error-handler"],
)
async def test_route_error_before_write_keeps_older_proposal_despite_attempted_fresh_draft(
    monkeypatch: pytest.MonkeyPatch,
    raised_error: BaseException,
    api_key_request: MagicMock,
    copilot_stream: MagicMock,
    organization: SimpleNamespace,
) -> None:
    # Negative companion to test_route_error_after_real_fresh_write_clears_it_even_with_
    # no_prior_proposal: agent_result.updated_workflow being SET only means a write was
    # ATTEMPTED. flaky_finalise bypasses _finalise_normal_turn's real body entirely, so
    # the write never actually reaches chat.proposed_workflow — an older, legitimately
    # keep_pending_proposal-protected proposal must survive, not get force-cleared just
    # because this turn also carried an (unpersisted) fresh draft.
    monkeypatch.setattr(settings, "ENABLE_WORKFLOW_COPILOT_V2", True)
    captured = install_fake_create(monkeypatch)

    chat = SimpleNamespace(
        workflow_copilot_chat_id="chat-1",
        workflow_permanent_id="wpid-1",
        organization_id="org-1",
        proposed_workflow={"workflow_id": "older-stale"},
        auto_accept=False,
    )
    original_workflow = SimpleNamespace(
        workflow_id="wf-canonical",
        title="Original",
        description="Original description",
        workflow_definition=None,
    )
    agent_result = SimpleNamespace(
        user_response="unused",
        updated_workflow=SimpleNamespace(title="fresh draft", model_dump=lambda mode: {"title": "fresh draft"}),
        global_llm_context=None,
        workflow_yaml="title: fresh draft\n",
        workflow_was_persisted=False,
        clear_proposed_workflow=False,
        has_staged_proposal=False,
        proposal_disposition="review_untested",
        unvalidated=False,
        turn_outcome=None,
    )
    restore_mock, _ = setup_new_copilot_mocks(monkeypatch, chat, original_workflow, agent_result)
    finalise_results: list[object] = []
    original_finalise = workflow_copilot_route._finalise_normal_turn

    async def flaky_finalise(*args: object, **kwargs: object) -> object:
        finalise_results.append(kwargs["agent_result"])
        if len(finalise_results) == 1:
            raise raised_error
        return await original_finalise(*args, **kwargs)

    monkeypatch.setattr(workflow_copilot_route, "_finalise_normal_turn", flaky_finalise)

    response = await workflow_copilot_chat_post(
        api_key_request, _make_chat_request(keep_pending_proposal=True), organization
    )
    assert response is captured["sentinel"]

    handler = captured["handler"]
    assert callable(handler)
    await handler(copilot_stream)

    restore_mock.assert_not_awaited()
    assert len(finalise_results) == 2
    recovered_result = finalise_results[1]
    assert recovered_result.clear_proposed_workflow is False
    app.DATABASE.workflow_params.update_workflow_copilot_chat.assert_not_awaited()


@pytest.mark.asyncio
async def test_route_error_after_real_fresh_write_clears_it_even_with_no_prior_proposal(
    monkeypatch: pytest.MonkeyPatch,
    api_key_request: MagicMock,
    copilot_stream: MagicMock,
    organization: SimpleNamespace,
) -> None:
    # Unlike the flaky_finalise-based tests above (which bypass _finalise_normal_turn's
    # real body entirely), this lets the REAL first attempt genuinely write the fresh
    # proposal to chat.proposed_workflow before a LATER step (chat-message creation)
    # fails — the bug this pins is that chat.proposed_workflow stayed in-memory None
    # (never synced after the write), so the retry's `elif chat.proposed_workflow is
    # not None` guard silently skipped the clear even though clear_proposed_workflow
    # correctly computed True.
    monkeypatch.setattr(settings, "ENABLE_WORKFLOW_COPILOT_V2", True)
    captured = install_fake_create(monkeypatch)

    chat = SimpleNamespace(
        workflow_copilot_chat_id="chat-1",
        workflow_permanent_id="wpid-1",
        organization_id="org-1",
        proposed_workflow=None,
        auto_accept=False,
    )
    original_workflow = SimpleNamespace(
        workflow_id="wf-canonical",
        title="Original",
        description="Original description",
        workflow_definition=None,
    )
    agent_result = SimpleNamespace(
        user_response="Here is your draft.",
        updated_workflow=SimpleNamespace(title="fresh draft", model_dump=lambda mode: {"title": "fresh draft"}),
        global_llm_context=None,
        workflow_yaml="title: fresh draft\n",
        workflow_was_persisted=False,
        clear_proposed_workflow=False,
        has_staged_proposal=False,
        proposal_disposition="review_untested",
        turn_outcome=None,
    )
    _restore_mock, workflow_params = setup_new_copilot_mocks(monkeypatch, chat, original_workflow, agent_result)

    call_count = {"n": 0}
    original_return = workflow_params.create_workflow_copilot_chat_message.return_value

    async def flaky_create_message(*args: object, **kwargs: object) -> object:
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("chat row insert boom")
        return original_return

    workflow_params.create_workflow_copilot_chat_message = AsyncMock(side_effect=flaky_create_message)

    response = await workflow_copilot_chat_post(
        api_key_request, _make_chat_request(keep_pending_proposal=True), organization
    )
    assert response is captured["sentinel"]

    handler = captured["handler"]
    assert callable(handler)
    await handler(copilot_stream)

    update_calls = workflow_params.update_workflow_copilot_chat.await_args_list
    write_calls = [c for c in update_calls if c.kwargs.get("proposed_workflow") is not None]
    clear_calls = [c for c in update_calls if c.kwargs.get("proposed_workflow") is None]
    assert write_calls, "the first attempt must have genuinely persisted the fresh draft"
    assert clear_calls, (
        f"the orphaned fresh draft must clear on retry even though chat had no prior proposal, got {update_calls!r}"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    (
        "auto_accept",
        "workflow_was_persisted",
        "has_valid_proposal",
        "prior_proposal",
        "clear_proposed_flag",
        "expect_clear_call",
    ),
    [
        # Restore-and-clear: persisted draft, no proposal, stale prior.
        (False, True, False, {"workflow_id": "stale"}, False, True),
        # Restore fires but nothing stale to clear.
        (False, True, False, None, False, False),
        # Chat-only turn with no clear flag must not touch a prior proposal.
        (False, False, False, {"workflow_id": "stale"}, False, False),
        # New valid proposal stores via the if-branch, not the clear-branch.
        (False, True, True, {"workflow_id": "stale"}, False, False),
        # auto_accept=True default paths: nothing to write.
        (True, True, False, None, False, False),
        (True, True, True, {"workflow_id": "stale"}, False, False),
        # Agent ran run_blocks (persisted=True) then ASK_QUESTIONed with the
        # clear flag set: restore AND clear fire in the same turn.
        (False, True, False, {"workflow_id": "stale"}, True, True),
        # auto_accept=True restore-driven clear: a stale proposal that survived
        # an auto-accept toggle gets nulled when the assistant invalidates it.
        (True, True, False, {"workflow_id": "stale"}, False, True),
        # The clear flag nulls the stale proposal under both auto_accept values
        # even when nothing was persisted this turn.
        (False, False, False, {"workflow_id": "stale"}, True, True),
        (True, False, False, {"workflow_id": "stale"}, True, True),
        # No prior proposal => no DB write even when the clear flag is set.
        (False, False, False, None, True, False),
        (True, False, False, None, True, False),
        # auto_accept=True turn with a stale UNVALIDATED proposal clears it
        # via the third elif (no clear flag, no restore needed).
        (True, False, False, {"workflow_id": "stale", "_copilot_unvalidated": True}, False, True),
        (True, True, True, {"workflow_id": "stale", "_copilot_unvalidated": True}, False, True),
    ],
)
async def test_proposed_workflow_cleared_on_restore(
    monkeypatch: pytest.MonkeyPatch,
    auto_accept: bool,
    workflow_was_persisted: bool,
    has_valid_proposal: bool,
    prior_proposal: dict | None,
    clear_proposed_flag: bool,
    expect_clear_call: bool,
    api_key_request: MagicMock,
    copilot_stream: MagicMock,
    organization: SimpleNamespace,
) -> None:
    monkeypatch.setattr(settings, "ENABLE_WORKFLOW_COPILOT_V2", True)

    captured = install_fake_create(monkeypatch)

    chat = SimpleNamespace(
        workflow_copilot_chat_id="chat-1",
        workflow_permanent_id="wpid-1",
        organization_id="org-1",
        proposed_workflow=prior_proposal,
        auto_accept=auto_accept,
    )
    original_workflow = SimpleNamespace(
        workflow_id="wf-canonical",
        title="Original",
        description="Original description",
        workflow_definition=None,
    )
    proposal = MagicMock(spec=["model_dump"]) if has_valid_proposal else None
    if proposal is not None:
        proposal.model_dump.return_value = {"workflow_id": "wf-canonical"}
    agent_result = SimpleNamespace(
        user_response="done",
        updated_workflow=proposal,
        global_llm_context=None,
        workflow_yaml=None,
        workflow_was_persisted=workflow_was_persisted,
        clear_proposed_workflow=clear_proposed_flag,
        proposal_disposition="auto_applicable",
        turn_outcome=None,
    )

    setup_new_copilot_mocks(monkeypatch, chat, original_workflow, agent_result)

    response = await workflow_copilot_chat_post(api_key_request, _make_chat_request(), organization)
    assert response is captured["sentinel"]

    handler = captured["handler"]
    assert callable(handler)
    await handler(copilot_stream)

    update_calls = app.DATABASE.workflow_params.update_workflow_copilot_chat.await_args_list
    clear_calls = [c for c in update_calls if c.kwargs.get("proposed_workflow") is None]

    if expect_clear_call:
        assert len(clear_calls) == 1, f"expected a proposed_workflow=None clear, got {update_calls!r}"
    else:
        assert not clear_calls, f"did not expect a clear call, got {update_calls!r}"

    # The FE's auto_accept code path reads the SSE payload, not
    # chat.proposed_workflow, so the payload must mirror agent_result.
    response_frames = [
        call.args[0]
        for call in copilot_stream.send.await_args_list
        if isinstance(call.args[0], WorkflowCopilotStreamResponseUpdate)
    ]
    assert len(response_frames) == 1, f"expected exactly one RESPONSE frame, got {response_frames!r}"
    expected_payload_workflow = proposal.model_dump.return_value if has_valid_proposal else None
    assert response_frames[0].updated_workflow == expected_payload_workflow
    if not auto_accept:
        assert response_frames[0].workflow_applied is False


@pytest.mark.asyncio
async def test_verified_code_only_fix_stays_pending_without_auto_accept(
    monkeypatch: pytest.MonkeyPatch,
    api_key_request: MagicMock,
    copilot_stream: MagicMock,
    organization: SimpleNamespace,
) -> None:
    # With auto_accept off, a verified CODE_ONLY_BROWSER fix (auto_applicable + staged)
    # must stay a pending proposal: no canonical commit, proposal persisted, terminal
    # frame reports workflow_applied False. ``apply_without_review`` is set truthy only
    # to prove that attribute can no longer force an auto-apply on its own.
    monkeypatch.setattr(settings, "ENABLE_WORKFLOW_COPILOT_V2", True)

    captured = install_fake_create(monkeypatch)

    chat = SimpleNamespace(
        workflow_copilot_chat_id="chat-1",
        workflow_permanent_id="wpid-1",
        organization_id="org-1",
        proposed_workflow={"workflow_id": "stale"},
        auto_accept=False,
    )
    original_workflow = SimpleNamespace(
        workflow_id="wf-canonical",
        title="Original",
        description="Original description",
        workflow_definition=None,
    )
    proposal = MagicMock()
    proposal.model_dump.return_value = {"workflow_id": "wf-applied"}
    proposal.title = "Applied"
    proposal.description = "Applied description"
    proposal.workflow_definition = SimpleNamespace(blocks=[])
    proposal.proxy_location = None
    proposal.webhook_callback_url = None
    proposal.totp_verification_url = None
    proposal.totp_identifier = None
    proposal.persist_browser_session = False
    proposal.browser_profile_id = None
    proposal.model = None
    proposal.max_screenshot_scrolls = None
    proposal.extra_http_headers = None
    proposal.cdp_connect_headers = None
    proposal.run_with = "agent"
    proposal.ai_fallback = None
    proposal.cache_key = None
    proposal.adaptive_caching = False
    proposal.code_version = 2
    proposal.run_sequentially = False
    proposal.sequential_key = None
    agent_result = SimpleNamespace(
        user_response="done",
        updated_workflow=proposal,
        global_llm_context=None,
        workflow_yaml="title: Applied",
        workflow_was_persisted=False,
        clear_proposed_workflow=False,
        proposal_disposition="auto_applicable",
        apply_without_review=True,
        has_staged_proposal=True,
        staged_workflow=proposal,
        turn_outcome=None,
    )

    restore_mock, _ = setup_new_copilot_mocks(monkeypatch, chat, original_workflow, agent_result)
    workflow_service = SimpleNamespace(update_workflow_definition=AsyncMock())
    monkeypatch.setattr(app, "WORKFLOW_SERVICE", workflow_service)
    monkeypatch.setattr(
        workflow_copilot_route,
        "resolve_copilot_created_by_stamp",
        AsyncMock(return_value="copilot"),
    )

    response = await workflow_copilot_chat_post(api_key_request, _make_chat_request(), organization)
    assert response is captured["sentinel"]

    handler = captured["handler"]
    assert callable(handler)
    await handler(copilot_stream)

    restore_mock.assert_not_awaited()
    workflow_service.update_workflow_definition.assert_not_awaited()
    update_calls = app.DATABASE.workflow_params.update_workflow_copilot_chat.await_args_list
    proposed_writes = [c.kwargs["proposed_workflow"] for c in update_calls if "proposed_workflow" in c.kwargs]
    assert proposed_writes, "expected the pending proposal to be persisted"
    assert all(w is not None for w in proposed_writes), "verified fix must stay pending, not be cleared"

    response_frames = [
        call.args[0]
        for call in copilot_stream.send.await_args_list
        if isinstance(call.args[0], WorkflowCopilotStreamResponseUpdate)
    ]
    assert len(response_frames) == 1
    assert response_frames[0].workflow_applied is False
    assert response_frames[0].updated_workflow == {"workflow_id": "wf-applied"}


@pytest.mark.asyncio
async def test_output_policy_block_preserves_unvalidated_prior_proposal_under_auto_accept(
    monkeypatch: pytest.MonkeyPatch,
    api_key_request: MagicMock,
    copilot_stream: MagicMock,
    organization: SimpleNamespace,
) -> None:
    monkeypatch.setattr(settings, "ENABLE_WORKFLOW_COPILOT_V2", True)

    captured = install_fake_create(monkeypatch)

    chat = SimpleNamespace(
        workflow_copilot_chat_id="chat-1",
        workflow_permanent_id="wpid-1",
        organization_id="org-1",
        proposed_workflow={"workflow_id": "staged", "_copilot_unvalidated": True},
        auto_accept=True,
    )
    original_workflow = SimpleNamespace(
        workflow_id="wf-canonical",
        title="Original",
        description="Original description",
        workflow_definition=None,
    )
    terminal_message = "I could not safely return that chat reply."
    agent_result = SimpleNamespace(
        user_response=terminal_message,
        updated_workflow=None,
        global_llm_context=None,
        workflow_yaml=None,
        workflow_was_persisted=False,
        clear_proposed_workflow=False,
        response_type="ASK_QUESTION",
        unvalidated=False,
        output_policy_diagnostics={
            "final_output_policy_allowed": False,
            "hard_block_reason_codes": ["internal_tool_instruction_leak"],
        },
        turn_outcome=None,
    )

    setup_new_copilot_mocks(monkeypatch, chat, original_workflow, agent_result)

    response = await workflow_copilot_chat_post(api_key_request, _make_chat_request(), organization)
    assert response is captured["sentinel"]

    handler = captured["handler"]
    assert callable(handler)
    await handler(copilot_stream)

    update_calls = app.DATABASE.workflow_params.update_workflow_copilot_chat.await_args_list
    clear_calls = [c for c in update_calls if c.kwargs.get("proposed_workflow") is None]
    assert not clear_calls, f"did not expect a clear call, got {update_calls!r}"

    assistant_call = next(
        call
        for call in app.DATABASE.workflow_params.create_workflow_copilot_chat_message.await_args_list
        if call.kwargs.get("sender") == WorkflowCopilotChatSender.AI
    )
    assert assistant_call.kwargs["content"] == terminal_message

    response_frames = [
        call.args[0]
        for call in copilot_stream.send.await_args_list
        if isinstance(call.args[0], WorkflowCopilotStreamResponseUpdate)
    ]
    assert len(response_frames) == 1
    frame = response_frames[0]
    assert frame.message == terminal_message
    assert frame.response_type == "ASK_QUESTION"


@pytest.mark.asyncio
async def test_unvalidated_timeout_wip_overrides_auto_accept(
    monkeypatch: pytest.MonkeyPatch, api_key_request: MagicMock, organization: SimpleNamespace
) -> None:
    monkeypatch.setattr(settings, "ENABLE_WORKFLOW_COPILOT_V2", True)

    captured = install_fake_create(monkeypatch)

    chat = SimpleNamespace(
        workflow_copilot_chat_id="chat-1",
        workflow_permanent_id="wpid-1",
        organization_id="org-1",
        proposed_workflow=None,
        auto_accept=True,
    )
    original_workflow = SimpleNamespace(
        workflow_id="wf-canonical",
        title="Original",
        description="Original description",
        workflow_definition=None,
    )
    proposal = MagicMock(spec=["model_dump", "title"])
    proposal.title = "WIP"
    proposal.model_dump.return_value = {"workflow_id": "wf-canonical"}
    agent_result = SimpleNamespace(
        user_response="I ran out of time before I could finish testing.",
        updated_workflow=proposal,
        global_llm_context=None,
        workflow_yaml="title: WIP",
        workflow_was_persisted=True,
        clear_proposed_workflow=False,
        proposal_disposition="review_untested",
        total_tokens=42,
        response_type="REPLY",
        output_policy_diagnostics={
            "raw_output_kind": "informational_answer",
            "final_output_kind": "informational_answer",
            "raw_reason_codes": ["internal_block_taxonomy_leak"],
            "hard_block_reason_codes": [],
            "soft_rewrite_reason_codes": ["internal_block_taxonomy_leak"],
            "raw_would_have_failed": True,
            "contained_failure": True,
        },
        turn_outcome=None,
    )

    restore_mock, _ = setup_new_copilot_mocks(monkeypatch, chat, original_workflow, agent_result)

    response = await workflow_copilot_chat_post(api_key_request, _make_chat_request(), organization)
    assert response is captured["sentinel"]

    sent_frames: list[object] = []
    stream = MagicMock()

    async def capture_send(payload: object) -> bool:
        sent_frames.append(payload)
        return True

    stream.send = capture_send
    stream.is_disconnected = AsyncMock(return_value=False)

    handler = captured["handler"]
    assert callable(handler)
    await handler(stream)

    restore_mock.assert_awaited_once()

    update_calls = app.DATABASE.workflow_params.update_workflow_copilot_chat.await_args_list
    proposal_writes = [c for c in update_calls if c.kwargs.get("proposed_workflow") is not None]
    assert len(proposal_writes) == 1
    proposed_data = proposal_writes[0].kwargs["proposed_workflow"]
    assert proposed_data.get("_copilot_unvalidated") is True
    assert proposed_data.get("_copilot_yaml") == "title: WIP"

    response_frame = next(
        (f for f in sent_frames if getattr(f, "type", None) and str(f.type).endswith("response")),
        None,
    )
    assert response_frame is not None
    assert response_frame.proposal_disposition == "review_untested"
    assert response_frame.output_policy_diagnostics == agent_result.output_policy_diagnostics
    assert not [f for f in sent_frames if isinstance(f, WorkflowCopilotStreamErrorUpdate)]


@pytest.mark.asyncio
async def test_env_on_short_circuits_posthog(
    monkeypatch: pytest.MonkeyPatch, anon_request: MagicMock, organization: SimpleNamespace
) -> None:
    """Env var True must skip the PostHog check entirely and route to v2."""
    monkeypatch.setattr(settings, "ENABLE_WORKFLOW_COPILOT_V2", True)
    mock_provider = _install_mock_provider(
        monkeypatch,
        side_effect=AssertionError("PostHog must not be consulted when env var is True"),
    )

    sentinel = object()
    new_copilot_mock = AsyncMock(return_value=sentinel)
    monkeypatch.setattr(
        "skyvern.forge.sdk.routes.workflow_copilot._new_copilot_chat_post",
        new_copilot_mock,
    )

    response = await workflow_copilot_chat_post(anon_request, _make_chat_request(), organization)

    assert response is sentinel
    new_copilot_mock.assert_awaited_once()
    mock_provider.is_feature_enabled_cached.assert_not_awaited()


@pytest.mark.asyncio
async def test_env_off_posthog_on_uses_v2(monkeypatch: pytest.MonkeyPatch, anon_request: MagicMock) -> None:
    """Env var False + PostHog True -> v2 path, with org_id as distinct_id."""
    monkeypatch.setattr(settings, "ENABLE_WORKFLOW_COPILOT_V2", False)
    mock_provider = _install_mock_provider(monkeypatch, return_value=True)

    sentinel = object()
    new_copilot_mock = AsyncMock(return_value=sentinel)
    monkeypatch.setattr(
        "skyvern.forge.sdk.routes.workflow_copilot._new_copilot_chat_post",
        new_copilot_mock,
    )

    organization = SimpleNamespace(organization_id="org-abc")

    response = await workflow_copilot_chat_post(anon_request, _make_chat_request(), organization)

    assert response is sentinel
    new_copilot_mock.assert_awaited_once()
    mock_provider.is_feature_enabled_cached.assert_awaited_once_with(
        COPILOT_V2_FLAG_KEY,
        distinct_id="org-abc",
        properties={"organization_id": "org-abc"},
    )


@pytest.mark.asyncio
async def test_provider_failure_falls_back_to_legacy(
    monkeypatch: pytest.MonkeyPatch, anon_request: MagicMock, organization: SimpleNamespace
) -> None:
    """Provider errors (PostHog down, DB hiccup) must not break the endpoint."""
    monkeypatch.setattr(settings, "ENABLE_WORKFLOW_COPILOT_V2", False)
    _install_mock_provider(monkeypatch, side_effect=RuntimeError("posthog unreachable"))

    new_copilot_mock = AsyncMock(side_effect=AssertionError("v2 path must not run when provider errors"))
    monkeypatch.setattr(
        "skyvern.forge.sdk.routes.workflow_copilot._new_copilot_chat_post",
        new_copilot_mock,
    )

    captured = install_fake_create(monkeypatch)

    response = await workflow_copilot_chat_post(anon_request, _make_chat_request(), organization)

    assert response is captured["sentinel"]
    new_copilot_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_env_off_posthog_off_uses_legacy(
    monkeypatch: pytest.MonkeyPatch, anon_request: MagicMock, organization: SimpleNamespace
) -> None:
    """Env var False + PostHog False -> legacy stream handler, v2 not reached."""
    monkeypatch.setattr(settings, "ENABLE_WORKFLOW_COPILOT_V2", False)
    mock_provider = _install_mock_provider(monkeypatch, return_value=False)

    new_copilot_mock = AsyncMock(side_effect=AssertionError("v2 path must not run when both gates are off"))
    monkeypatch.setattr(
        "skyvern.forge.sdk.routes.workflow_copilot._new_copilot_chat_post",
        new_copilot_mock,
    )

    captured = install_fake_create(monkeypatch)

    response = await workflow_copilot_chat_post(anon_request, _make_chat_request(), organization)

    assert response is captured["sentinel"]
    new_copilot_mock.assert_not_awaited()
    mock_provider.is_feature_enabled_cached.assert_awaited_once()


@pytest.mark.asyncio
async def test_legacy_path_persists_copilot_yaml_on_proposal(
    monkeypatch: pytest.MonkeyPatch, anon_request: MagicMock, copilot_stream: MagicMock, organization: SimpleNamespace
) -> None:
    """Legacy V1 path stashes _copilot_yaml so /apply-proposed-workflow can re-create the version.

    Regression for #10568 + SKY-9206: Accept on the frontend now hits
    /apply-proposed-workflow, which 400s when _copilot_yaml is missing from the
    proposal. V2 sets it (line 921); V1 must too or non-V2 users can never accept.
    """
    monkeypatch.setattr(settings, "ENABLE_WORKFLOW_COPILOT_V2", False)
    _install_mock_provider(monkeypatch, return_value=False)

    captured = install_fake_create(monkeypatch)

    chat = SimpleNamespace(
        workflow_copilot_chat_id="chat-1",
        workflow_permanent_id="wpid-1",
        organization_id="org-1",
        proposed_workflow=None,
        auto_accept=False,
    )

    proposal = MagicMock(spec=["model_dump", "title"])
    proposal.title = "Updated"
    proposal.model_dump.return_value = {"workflow_id": "wf-canonical", "title": "Updated"}

    workflow_yaml = "title: Updated\nworkflow_definition:\n  blocks: []\n"

    copilot_call_llm_mock = AsyncMock(return_value=("ok", proposal, None, workflow_yaml))
    monkeypatch.setattr(
        "skyvern.forge.sdk.routes.workflow_copilot.copilot_call_llm",
        copilot_call_llm_mock,
    )
    monkeypatch.setattr(
        "skyvern.forge.sdk.routes.workflow_copilot._get_debug_run_info",
        AsyncMock(return_value=None),
    )

    app.DATABASE.workflow_params = SimpleNamespace(
        get_workflow_copilot_chat_by_id=AsyncMock(return_value=chat),
        get_workflow_copilot_chat_messages=AsyncMock(return_value=[]),
        update_workflow_copilot_chat=AsyncMock(),
        create_workflow_copilot_chat_message=AsyncMock(
            return_value=SimpleNamespace(created_at=datetime(2026, 4, 28, tzinfo=timezone.utc))
        ),
    )

    response = await workflow_copilot_chat_post(anon_request, _make_chat_request(), organization)
    assert response is captured["sentinel"]

    handler = captured["handler"]
    assert callable(handler)
    await handler(copilot_stream)

    update_calls = app.DATABASE.workflow_params.update_workflow_copilot_chat.await_args_list
    persist_calls = [c for c in update_calls if c.kwargs.get("proposed_workflow") is not None]
    assert len(persist_calls) == 1, f"expected exactly one proposed_workflow persist, got {update_calls!r}"

    persisted = persist_calls[0].kwargs["proposed_workflow"]
    assert isinstance(persisted, dict)
    assert persisted.get("_copilot_yaml") == workflow_yaml, (
        "legacy V1 path must stash the LLM-emitted YAML on the proposal so "
        "/apply-proposed-workflow can re-create the workflow version"
    )


@pytest.mark.asyncio
async def test_persist_state_keeps_verified_review_tested_proposal(monkeypatch: pytest.MonkeyPatch) -> None:
    chat = SimpleNamespace(
        organization_id="org-1",
        workflow_copilot_chat_id="chat-1",
        auto_accept=False,
        proposed_workflow={"existing": True},
    )
    monkeypatch.setattr(
        app.DATABASE,
        "workflow_params",
        SimpleNamespace(update_workflow_copilot_chat=AsyncMock()),
    )
    agent_result = SimpleNamespace(
        updated_workflow=SimpleNamespace(title="built", model_dump=lambda mode: {"title": "built"}),
        workflow_yaml="title: built\n",
        clear_proposed_workflow=False,
        proposal_disposition="review_tested",
        cancelled=False,
        output_policy_diagnostics=None,
        canonical_was_persisted_due_to_param_change=False,
    )

    await workflow_copilot_route._persist_proposed_workflow_state(chat, agent_result, restored=False)

    calls = app.DATABASE.workflow_params.update_workflow_copilot_chat.await_args_list
    assert len(calls) == 1
    persisted = calls[0].kwargs["proposed_workflow"]
    assert persisted is not None
    assert persisted.get("_copilot_unvalidated") is not True


def _make_bypassed_proposal_agent_result(**overrides: object) -> SimpleNamespace:
    fields: dict[str, object] = dict(
        updated_workflow=None,
        clear_proposed_workflow=False,
        proposal_disposition="review_untested",
        cancelled=False,
        output_policy_diagnostics=None,
    )
    fields.update(overrides)
    return SimpleNamespace(**fields)


@pytest.mark.asyncio
async def test_persist_state_restored_keep_pending_proposal_survives(monkeypatch: pytest.MonkeyPatch) -> None:
    # Opt-in keep_pending_proposal suppresses the restored-alone clear so a bypassed
    # proposal stays actionable across a follow-up turn with no new draft.
    chat = SimpleNamespace(
        organization_id="org-1",
        workflow_copilot_chat_id="chat-1",
        auto_accept=False,
        proposed_workflow={"existing": True},
    )
    monkeypatch.setattr(
        app.DATABASE,
        "workflow_params",
        SimpleNamespace(update_workflow_copilot_chat=AsyncMock()),
    )
    agent_result = _make_bypassed_proposal_agent_result()

    await workflow_copilot_route._persist_proposed_workflow_state(
        chat, agent_result, restored=True, keep_pending_proposal=True
    )

    app.DATABASE.workflow_params.update_workflow_copilot_chat.assert_not_awaited()


@pytest.mark.asyncio
async def test_persist_state_restored_without_keep_still_clears(monkeypatch: pytest.MonkeyPatch) -> None:
    # Pin today's default (keep_pending_proposal=False) behavior next to its opt-in twin above.
    chat = SimpleNamespace(
        organization_id="org-1",
        workflow_copilot_chat_id="chat-1",
        auto_accept=False,
        proposed_workflow={"existing": True},
    )
    monkeypatch.setattr(
        app.DATABASE,
        "workflow_params",
        SimpleNamespace(update_workflow_copilot_chat=AsyncMock()),
    )
    agent_result = _make_bypassed_proposal_agent_result()

    await workflow_copilot_route._persist_proposed_workflow_state(chat, agent_result, restored=True)

    calls = app.DATABASE.workflow_params.update_workflow_copilot_chat.await_args_list
    assert len(calls) == 1
    assert calls[0].kwargs["proposed_workflow"] is None


@pytest.mark.asyncio
async def test_persist_state_keep_pending_proposal_does_not_suppress_explicit_clear(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # keep_pending_proposal only neutralizes the restored-alone justification; an
    # agent-explicit clear_proposed_workflow must still win.
    chat = SimpleNamespace(
        organization_id="org-1",
        workflow_copilot_chat_id="chat-1",
        auto_accept=False,
        proposed_workflow={"existing": True},
    )
    monkeypatch.setattr(
        app.DATABASE,
        "workflow_params",
        SimpleNamespace(update_workflow_copilot_chat=AsyncMock()),
    )
    agent_result = _make_bypassed_proposal_agent_result(
        clear_proposed_workflow=True, proposal_disposition="no_proposal"
    )

    await workflow_copilot_route._persist_proposed_workflow_state(
        chat, agent_result, restored=False, keep_pending_proposal=True
    )

    calls = app.DATABASE.workflow_params.update_workflow_copilot_chat.await_args_list
    assert len(calls) == 1
    assert calls[0].kwargs["proposed_workflow"] is None


@pytest.mark.asyncio
async def test_persist_state_keep_pending_proposal_does_not_block_new_proposal_overwrite(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A fresh proposal always overwrites regardless of keep_pending_proposal/restored.
    chat = SimpleNamespace(
        organization_id="org-1",
        workflow_copilot_chat_id="chat-1",
        auto_accept=False,
        proposed_workflow={"existing": True},
    )
    monkeypatch.setattr(
        app.DATABASE,
        "workflow_params",
        SimpleNamespace(update_workflow_copilot_chat=AsyncMock()),
    )
    agent_result = _make_bypassed_proposal_agent_result(
        updated_workflow=SimpleNamespace(title="new draft", model_dump=lambda mode: {"title": "new draft"}),
        workflow_yaml="title: new draft\n",
    )

    await workflow_copilot_route._persist_proposed_workflow_state(
        chat, agent_result, restored=True, keep_pending_proposal=True
    )

    calls = app.DATABASE.workflow_params.update_workflow_copilot_chat.await_args_list
    assert len(calls) == 1
    persisted = calls[0].kwargs["proposed_workflow"]
    assert persisted is not None
    assert persisted.get("title") == "new draft"


@pytest.mark.asyncio
async def test_persist_state_keep_pending_proposal_survives_auto_accept_stale_unvalidated_clear(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # auto_accept doesn't cover review_untested/review_tested, so a gate-worthy
    # unvalidated proposal can coexist with chat.auto_accept=True; the third
    # elif's clear must also respect keep_pending_proposal.
    chat = SimpleNamespace(
        organization_id="org-1",
        workflow_copilot_chat_id="chat-1",
        auto_accept=True,
        proposed_workflow={"existing": True, "_copilot_unvalidated": True},
    )
    monkeypatch.setattr(
        app.DATABASE,
        "workflow_params",
        SimpleNamespace(update_workflow_copilot_chat=AsyncMock()),
    )
    agent_result = _make_bypassed_proposal_agent_result()

    await workflow_copilot_route._persist_proposed_workflow_state(
        chat, agent_result, restored=False, keep_pending_proposal=True
    )

    app.DATABASE.workflow_params.update_workflow_copilot_chat.assert_not_awaited()


@pytest.mark.asyncio
async def test_persist_state_auto_accept_stale_unvalidated_still_clears_without_keep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Pin today's default (keep_pending_proposal=False) next to its opt-in twin above.
    chat = SimpleNamespace(
        organization_id="org-1",
        workflow_copilot_chat_id="chat-1",
        auto_accept=True,
        proposed_workflow={"existing": True, "_copilot_unvalidated": True},
    )
    monkeypatch.setattr(
        app.DATABASE,
        "workflow_params",
        SimpleNamespace(update_workflow_copilot_chat=AsyncMock()),
    )
    agent_result = _make_bypassed_proposal_agent_result()

    await workflow_copilot_route._persist_proposed_workflow_state(chat, agent_result, restored=False)

    calls = app.DATABASE.workflow_params.update_workflow_copilot_chat.await_args_list
    assert len(calls) == 1
    assert calls[0].kwargs["proposed_workflow"] is None


@pytest.mark.asyncio
async def test_persist_state_staged_commit_clears_stale_proposal_despite_keep_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A later turn's own auto-accept commit (chat.auto_accept) supersedes an
    # earlier pending proposal even when the client asked to keep it — the
    # committed canonical workflow already moved past it.
    chat = SimpleNamespace(
        organization_id="org-1",
        workflow_copilot_chat_id="chat-1",
        auto_accept=True,
        proposed_workflow={"existing": True},
    )
    monkeypatch.setattr(
        app.DATABASE,
        "workflow_params",
        SimpleNamespace(update_workflow_copilot_chat=AsyncMock()),
    )
    agent_result = _make_bypassed_proposal_agent_result(
        proposal_disposition="auto_applicable",
        has_staged_proposal=True,
    )

    await workflow_copilot_route._persist_proposed_workflow_state(
        chat, agent_result, restored=False, keep_pending_proposal=True
    )

    calls = app.DATABASE.workflow_params.update_workflow_copilot_chat.await_args_list
    assert len(calls) == 1
    assert calls[0].kwargs["proposed_workflow"] is None


@pytest.mark.asyncio
async def test_persist_state_staged_commit_clears_stale_proposal_on_default_path_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # This clause is unflagged: it also fixes SKY-12130 orphaning for every current
    # client, not just callers that opt into keep_pending_proposal. Pin the default
    # (keep_pending_proposal=False, the pre-existing behavior for all callers today)
    # path explicitly, not just the opt-in one above.
    chat = SimpleNamespace(
        organization_id="org-1",
        workflow_copilot_chat_id="chat-1",
        auto_accept=True,
        proposed_workflow={"existing": True},
    )
    monkeypatch.setattr(
        app.DATABASE,
        "workflow_params",
        SimpleNamespace(update_workflow_copilot_chat=AsyncMock()),
    )
    agent_result = _make_bypassed_proposal_agent_result(
        proposal_disposition="auto_applicable",
        has_staged_proposal=True,
    )

    await workflow_copilot_route._persist_proposed_workflow_state(chat, agent_result, restored=False)

    calls = app.DATABASE.workflow_params.update_workflow_copilot_chat.await_args_list
    assert len(calls) == 1
    assert calls[0].kwargs["proposed_workflow"] is None


@pytest.mark.asyncio
async def test_persist_state_auto_applicable_without_staged_commit_still_protected_by_keep_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # auto_applicable alone isn't enough — only an actual staged commit this turn
    # invalidates the earlier proposal. No staged content means nothing
    # superseded it, so keep_pending_proposal still applies.
    chat = SimpleNamespace(
        organization_id="org-1",
        workflow_copilot_chat_id="chat-1",
        auto_accept=True,
        proposed_workflow={"existing": True},
    )
    monkeypatch.setattr(
        app.DATABASE,
        "workflow_params",
        SimpleNamespace(update_workflow_copilot_chat=AsyncMock()),
    )
    agent_result = _make_bypassed_proposal_agent_result(
        proposal_disposition="auto_applicable",
        has_staged_proposal=False,
    )

    await workflow_copilot_route._persist_proposed_workflow_state(
        chat, agent_result, restored=False, keep_pending_proposal=True
    )

    app.DATABASE.workflow_params.update_workflow_copilot_chat.assert_not_awaited()


_NOW = datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc)


def _make_copilot_workflow(title: str, modified_at: datetime) -> Workflow:
    return Workflow(
        workflow_id="wf-1",
        organization_id="org-1",
        title=title,
        workflow_permanent_id="wpid-1",
        version=1,
        is_saved_task=False,
        workflow_definition=WorkflowDefinition(parameters=[], blocks=[]),
        created_at=_NOW - timedelta(days=1),
        modified_at=modified_at,
    )


def _fingerprint_of(workflow: Workflow) -> str:
    return workflow_content_fingerprint(workflow.model_dump(mode="json"))


def _make_pending_turn(turn_id: str, age_seconds: float, **overrides: Any) -> CopilotPendingTurn:
    return CopilotPendingTurn(
        turn_id=turn_id,
        started_at=datetime.now(timezone.utc) - timedelta(seconds=age_seconds),
        **overrides,
    )


def _make_persisted_chat(
    pending: list[CopilotPendingTurn], proposed_workflow: dict | None = None
) -> WorkflowCopilotChat:
    return WorkflowCopilotChat(
        workflow_copilot_chat_id="chat-1",
        organization_id="org-1",
        workflow_permanent_id="wpid-1",
        proposed_workflow=proposed_workflow,
        auto_accept=False,
        pending_turns={entry.turn_id: entry for entry in pending},
        created_at=_NOW,
        modified_at=_NOW,
    )


class _FakeCopilotChatStore:
    """In-memory double of the chat/message repo surface reconcile-on-read touches."""

    def __init__(self, chat: WorkflowCopilotChat) -> None:
        self.chat = chat
        self.messages: list[WorkflowCopilotChatMessage] = []
        self.claim_calls: list[str] = []

    def add_message(
        self,
        sender: WorkflowCopilotChatSender,
        content: str,
        turn_outcome: TurnOutcome | None = None,
    ) -> WorkflowCopilotChatMessage:
        message = WorkflowCopilotChatMessage(
            workflow_copilot_chat_message_id=f"wccm-{len(self.messages)}",
            workflow_copilot_chat_id=self.chat.workflow_copilot_chat_id,
            sender=sender,
            content=content,
            turn_outcome=turn_outcome,
            created_at=_NOW,
            modified_at=_NOW,
        )
        self.messages.append(message)
        return message

    @property
    def assistant_messages(self) -> list[WorkflowCopilotChatMessage]:
        return [m for m in self.messages if m.sender == WorkflowCopilotChatSender.AI]

    @property
    def user_messages(self) -> list[WorkflowCopilotChatMessage]:
        return [m for m in self.messages if m.sender == WorkflowCopilotChatSender.USER]

    async def get_workflow_copilot_chat_by_id(
        self, organization_id: str, workflow_copilot_chat_id: str
    ) -> WorkflowCopilotChat:
        return self.chat

    async def get_workflow_copilot_chat_messages(
        self, workflow_copilot_chat_id: str
    ) -> list[WorkflowCopilotChatMessage]:
        return list(self.messages)

    async def create_workflow_copilot_chat_message(
        self,
        organization_id: str,
        workflow_copilot_chat_id: str,
        sender: WorkflowCopilotChatSender,
        content: str,
        audio_artifact_id: str | None = None,
        global_llm_context: str | None = None,
        turn_outcome: TurnOutcome | None = None,
        narrative_payload: Any = None,
    ) -> WorkflowCopilotChatMessage:
        return self.add_message(sender, content, turn_outcome)

    async def replace_workflow_copilot_chat_message(
        self,
        organization_id: str,
        workflow_copilot_chat_message_id: str,
        content: str,
        global_llm_context: str | None,
        turn_outcome: TurnOutcome | None,
        narrative_payload: TurnNarrativePayload | None = None,
    ) -> WorkflowCopilotChatMessage | None:
        for index, message in enumerate(self.messages):
            if message.workflow_copilot_chat_message_id == workflow_copilot_chat_message_id:
                replaced = message.model_copy(update={"content": content, "turn_outcome": turn_outcome})
                self.messages[index] = replaced
                return replaced
        return None

    async def record_pending_copilot_turn_canonical_write(
        self,
        organization_id: str,
        workflow_copilot_chat_id: str,
        turn_id: str,
        fingerprint: str,
    ) -> None:
        entry = self.chat.pending_turns.get(turn_id)
        if entry is not None:
            entry.canonical_write_fingerprint = fingerprint

    async def claim_pending_copilot_turn(
        self,
        organization_id: str,
        workflow_copilot_chat_id: str,
        turn_id: str,
        claim_before: datetime,
    ) -> bool:
        self.claim_calls.append(turn_id)
        entry = self.chat.pending_turns.get(turn_id)
        if entry is None:
            return False
        if entry.recovering_at is not None and entry.recovering_at > claim_before:
            return False
        entry.recovering_at = datetime.now(timezone.utc)
        return True

    async def clear_pending_copilot_turn(
        self, organization_id: str, workflow_copilot_chat_id: str, turn_id: str
    ) -> None:
        self.chat.pending_turns.pop(turn_id, None)

    async def update_workflow_copilot_chat(
        self,
        organization_id: str,
        workflow_copilot_chat_id: str,
        proposed_workflow: dict | None = None,
        auto_accept: bool | None = None,
    ) -> WorkflowCopilotChat:
        self.chat.proposed_workflow = proposed_workflow
        return self.chat


def _install_reconcile_store(
    monkeypatch: pytest.MonkeyPatch,
    chat: WorkflowCopilotChat,
    canonical: Workflow | None = None,
) -> tuple[_FakeCopilotChatStore, AsyncMock]:
    store = _FakeCopilotChatStore(chat)
    app.DATABASE.workflow_params = store
    app.DATABASE.workflows = SimpleNamespace(get_workflow_by_permanent_id=AsyncMock(return_value=canonical))
    restore_mock = AsyncMock()
    monkeypatch.setattr(workflow_copilot_route, "_restore_workflow_definition", restore_mock)
    return store, restore_mock


async def _load_history(chat_id: str = "chat-1") -> Any:
    return await workflow_copilot_chat_history(
        workflow_copilot_chat_id=chat_id,
        organization=SimpleNamespace(organization_id="org-1"),
    )


@pytest.mark.asyncio
async def test_chat_history_marks_abandoned_turn_interrupted_not_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A turn whose process died leaves the user row plus an honest interrupted outcome."""
    chat = _make_persisted_chat([_make_pending_turn("turn-a", RECONCILE_ABANDON_AFTER_SECONDS + 60)])
    store, restore_mock = _install_reconcile_store(monkeypatch, chat)
    store.add_message(WorkflowCopilotChatSender.USER, "build me a scraper")

    response = await _load_history()

    assert [m.content for m in store.user_messages] == ["build me a scraper"]
    assert len(store.assistant_messages) == 1
    outcome = store.assistant_messages[0].turn_outcome
    assert outcome is not None
    assert outcome.terminal_reason == INTERRUPTED_TERMINAL_REASON
    assert outcome.terminal_reason != "cancel"
    assert outcome.response_kind == ResponseKind.RECOVER
    assert outcome.copilot_turn_id == "turn-a"
    assert chat.pending_turns == {}
    assert response.chat_history[-1].turn_outcome is not None
    restore_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_chat_history_leaves_a_young_pending_turn_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    chat = _make_persisted_chat([_make_pending_turn("turn-a", 30)])
    store, _ = _install_reconcile_store(monkeypatch, chat)
    store.add_message(WorkflowCopilotChatSender.USER, "build me a scraper")

    await _load_history()

    assert store.assistant_messages == []
    assert "turn-a" in chat.pending_turns


@pytest.mark.asyncio
async def test_chat_history_shows_the_user_message_while_the_turn_is_still_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reloading mid-turn — a tab refresh — shows the user's message without inventing an outcome."""
    chat = _make_persisted_chat([_make_pending_turn("turn-a", 30)])
    store, _ = _install_reconcile_store(monkeypatch, chat)
    store.add_message(WorkflowCopilotChatSender.USER, "build me a scraper")

    response = await _load_history()

    assert [m.content for m in response.chat_history] == ["build me a scraper"]
    assert all(m.turn_outcome is None for m in response.chat_history)
    assert "turn-a" in chat.pending_turns


@pytest.mark.asyncio
async def test_chat_history_leaves_a_turn_inside_its_enforcement_budget_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Past the 900s agent budget but still live in wall clock thanks to a credential pause."""
    chat = _make_persisted_chat([_make_pending_turn("turn-a", 1000)])
    store, _ = _install_reconcile_store(monkeypatch, chat)
    store.add_message(WorkflowCopilotChatSender.USER, "build me a scraper")

    await _load_history()

    assert store.assistant_messages == []
    assert "turn-a" in chat.pending_turns


@pytest.mark.asyncio
async def test_reconcile_restores_real_cdp_headers_not_the_masked_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pre-turn snapshot is a JSON dump, which masks cdp_connect_headers; restoring it verbatim would destroy them."""
    pre_turn = _make_copilot_workflow("Before the turn", _NOW - timedelta(hours=2))
    pre_turn.cdp_connect_headers = {"x-api-key": "real-secret"}
    mid_turn_draft = _make_copilot_workflow("Half-built draft", datetime.now(timezone.utc) - timedelta(seconds=1500))
    mid_turn_draft.cdp_connect_headers = {"x-api-key": "real-secret"}
    chat = _make_persisted_chat(
        [
            _make_pending_turn(
                "turn-a",
                RECONCILE_ABANDON_AFTER_SECONDS + 60,
                pre_turn_workflow=pre_turn.model_dump(mode="json"),
                canonical_write_fingerprint=_fingerprint_of(mid_turn_draft),
            )
        ]
    )
    store, restore_mock = _install_reconcile_store(monkeypatch, chat, canonical=mid_turn_draft)
    store.add_message(WorkflowCopilotChatSender.USER, "build me a scraper")

    await _load_history()

    restore_mock.assert_awaited_once()
    assert restore_mock.await_args.args[0].cdp_connect_headers == {"x-api-key": "real-secret"}


@pytest.mark.asyncio
async def test_reconcile_does_not_stash_a_canonical_the_dying_turn_already_restored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A finalizer that restored canonical just before the kill leaves no displaced draft to stash."""
    pre_turn = _make_copilot_workflow("Before the turn", _NOW - timedelta(hours=2))
    turn_draft = _make_copilot_workflow("Half-built draft", datetime.now(timezone.utc) - timedelta(seconds=1600))
    already_restored = _make_copilot_workflow("Before the turn", datetime.now(timezone.utc) - timedelta(seconds=1500))
    chat = _make_persisted_chat(
        [
            _make_pending_turn(
                "turn-a",
                RECONCILE_ABANDON_AFTER_SECONDS + 60,
                pre_turn_workflow=pre_turn.model_dump(mode="json"),
                canonical_write_fingerprint=_fingerprint_of(turn_draft),
            )
        ]
    )
    store, _ = _install_reconcile_store(monkeypatch, chat, canonical=already_restored)
    store.add_message(WorkflowCopilotChatSender.USER, "build me a scraper")

    response = await _load_history()

    assert response.proposed_workflow is None


@pytest.mark.asyncio
async def test_reconcile_restores_canonical_and_stashes_the_mid_turn_draft(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pre_turn = _make_copilot_workflow("Before the turn", _NOW - timedelta(hours=2))
    mid_turn_draft = _make_copilot_workflow("Half-built draft", datetime.now(timezone.utc) - timedelta(seconds=1500))
    chat = _make_persisted_chat(
        [
            _make_pending_turn(
                "turn-a",
                RECONCILE_ABANDON_AFTER_SECONDS + 60,
                pre_turn_workflow=pre_turn.model_dump(mode="json"),
                canonical_write_fingerprint=_fingerprint_of(mid_turn_draft),
            )
        ]
    )
    store, restore_mock = _install_reconcile_store(monkeypatch, chat, canonical=mid_turn_draft)
    store.add_message(WorkflowCopilotChatSender.USER, "build me a scraper")

    response = await _load_history()

    restore_mock.assert_awaited_once()
    restored_workflow = restore_mock.await_args.args[0]
    assert restored_workflow.title == "Before the turn"
    assert restored_workflow.workflow_definition == pre_turn.workflow_definition
    assert response.proposed_workflow is not None
    assert response.proposed_workflow["title"] == "Half-built draft"


@pytest.mark.asyncio
async def test_reconcile_keeps_an_unowned_canonical_change_rather_than_undoing_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An auto-accept commit records no ownership; undoing it would discard work the user opted to apply."""
    pre_turn = _make_copilot_workflow("Before the turn", _NOW - timedelta(hours=2))
    committed = _make_copilot_workflow("Auto-accepted build", datetime.now(timezone.utc) - timedelta(seconds=1500))
    chat = _make_persisted_chat(
        [
            _make_pending_turn(
                "turn-a",
                RECONCILE_ABANDON_AFTER_SECONDS + 60,
                pre_turn_workflow=pre_turn.model_dump(mode="json"),
            )
        ]
    )
    store, restore_mock = _install_reconcile_store(monkeypatch, chat, canonical=committed)
    store.add_message(WorkflowCopilotChatSender.USER, "build me a scraper")

    response = await _load_history()

    restore_mock.assert_not_awaited()
    assert response.proposed_workflow is None
    assert len(store.assistant_messages) == 1


@pytest.mark.asyncio
async def test_reconcile_leaves_canonical_alone_when_the_turn_never_wrote(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pre_turn = _make_copilot_workflow("Untouched", _NOW - timedelta(hours=2))
    chat = _make_persisted_chat(
        [
            _make_pending_turn(
                "turn-a",
                RECONCILE_ABANDON_AFTER_SECONDS + 60,
                pre_turn_workflow=pre_turn.model_dump(mode="json"),
            )
        ]
    )
    store, restore_mock = _install_reconcile_store(monkeypatch, chat, canonical=pre_turn)
    store.add_message(WorkflowCopilotChatSender.USER, "build me a scraper")

    response = await _load_history()

    restore_mock.assert_not_awaited()
    assert response.proposed_workflow is None
    assert len(store.assistant_messages) == 1


@pytest.mark.asyncio
async def test_reconcile_leaves_canonical_alone_when_someone_else_wrote_after_the_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hand edit landing after the turn's own write forfeits the turn's rollback claim."""
    pre_turn = _make_copilot_workflow("Before the turn", _NOW - timedelta(days=2))
    turn_draft = _make_copilot_workflow("Half-built draft", datetime.now(timezone.utc) - timedelta(seconds=1600))
    later_edit = _make_copilot_workflow("Edited by hand afterwards", datetime.now(timezone.utc))
    chat = _make_persisted_chat(
        [
            _make_pending_turn(
                "turn-a",
                RECONCILE_ABANDON_AFTER_SECONDS * 3,
                pre_turn_workflow=pre_turn.model_dump(mode="json"),
                canonical_write_fingerprint=_fingerprint_of(turn_draft),
            )
        ]
    )
    store, restore_mock = _install_reconcile_store(monkeypatch, chat, canonical=later_edit)
    store.add_message(WorkflowCopilotChatSender.USER, "build me a scraper")

    response = await _load_history()

    restore_mock.assert_not_awaited()
    assert response.proposed_workflow is None


@pytest.mark.asyncio
async def test_reconcile_keeps_an_earlier_proposal_when_keep_pending_was_requested(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pre_turn = _make_copilot_workflow("Before the turn", _NOW - timedelta(hours=2))
    mid_turn_draft = _make_copilot_workflow("Half-built draft", datetime.now(timezone.utc) - timedelta(seconds=1500))
    earlier_proposal = {"workflow_id": "wf-1", "title": "Earlier proposal"}
    chat = _make_persisted_chat(
        [
            _make_pending_turn(
                "turn-a",
                RECONCILE_ABANDON_AFTER_SECONDS + 60,
                pre_turn_workflow=pre_turn.model_dump(mode="json"),
                canonical_write_fingerprint=_fingerprint_of(mid_turn_draft),
                pre_turn_proposed_workflow=earlier_proposal,
                keep_pending_proposal=True,
            )
        ],
        proposed_workflow=earlier_proposal,
    )
    store, restore_mock = _install_reconcile_store(monkeypatch, chat, canonical=mid_turn_draft)
    store.add_message(WorkflowCopilotChatSender.USER, "build me a scraper")

    response = await _load_history()

    restore_mock.assert_awaited_once()
    assert response.proposed_workflow == earlier_proposal


@pytest.mark.asyncio
async def test_reconcile_completes_after_a_crash_mid_recovery(monkeypatch: pytest.MonkeyPatch) -> None:
    """A recovery that died after claiming the turn is reclaimed and still yields one reply."""
    entry = _make_pending_turn("turn-a", RECONCILE_ABANDON_AFTER_SECONDS * 3)
    entry.recovering_at = datetime.now(timezone.utc) - timedelta(seconds=RECONCILE_ABANDON_AFTER_SECONDS * 2)
    chat = _make_persisted_chat([entry])
    store, _ = _install_reconcile_store(monkeypatch, chat)
    store.add_message(WorkflowCopilotChatSender.USER, "build me a scraper")

    await _load_history()

    assert len(store.assistant_messages) == 1
    assert chat.pending_turns == {}


@pytest.mark.asyncio
async def test_reconcile_skips_a_turn_another_reader_is_already_recovering(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry = _make_pending_turn("turn-a", RECONCILE_ABANDON_AFTER_SECONDS + 60)
    entry.recovering_at = datetime.now(timezone.utc)
    chat = _make_persisted_chat([entry])
    store, _ = _install_reconcile_store(monkeypatch, chat)
    store.add_message(WorkflowCopilotChatSender.USER, "build me a scraper")

    await _load_history()

    assert store.assistant_messages == []
    assert "turn-a" in chat.pending_turns


@pytest.mark.asyncio
async def test_reconcile_handles_two_overlapping_abandoned_turns(monkeypatch: pytest.MonkeyPatch) -> None:
    chat = _make_persisted_chat(
        [
            _make_pending_turn("turn-a", RECONCILE_ABANDON_AFTER_SECONDS + 120),
            _make_pending_turn("turn-b", RECONCILE_ABANDON_AFTER_SECONDS + 60),
        ]
    )
    store, _ = _install_reconcile_store(monkeypatch, chat)
    store.add_message(WorkflowCopilotChatSender.USER, "first ask")
    store.add_message(WorkflowCopilotChatSender.USER, "second ask")

    await _load_history()

    recovered_turn_ids = [m.turn_outcome.copilot_turn_id for m in store.assistant_messages if m.turn_outcome]
    assert sorted(recovered_turn_ids) == ["turn-a", "turn-b"]
    assert chat.pending_turns == {}


@pytest.mark.asyncio
async def test_reconcile_drops_the_marker_of_a_turn_that_already_replied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A finalised turn whose marker clear failed must not be rolled back or answered twice."""
    pre_turn = _make_copilot_workflow("Before the turn", _NOW - timedelta(hours=2))
    committed = _make_copilot_workflow("Accepted build", datetime.now(timezone.utc) - timedelta(seconds=1500))
    chat = _make_persisted_chat(
        [
            _make_pending_turn(
                "turn-a",
                RECONCILE_ABANDON_AFTER_SECONDS + 60,
                pre_turn_workflow=pre_turn.model_dump(mode="json"),
            )
        ]
    )
    store, restore_mock = _install_reconcile_store(monkeypatch, chat, canonical=committed)
    store.add_message(WorkflowCopilotChatSender.USER, "build me a scraper")
    store.add_message(
        WorkflowCopilotChatSender.AI,
        "Done.",
        TurnOutcome(response_kind=ResponseKind.BUILD, copilot_turn_id="turn-a"),
    )

    response = await _load_history()

    assert len(store.assistant_messages) == 1
    assert chat.pending_turns == {}
    restore_mock.assert_not_awaited()
    assert response.proposed_workflow is None


@pytest.mark.asyncio
async def test_persist_turn_messages_is_idempotent_for_one_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    """A second finalizer pass for the same turn adds no duplicate user or assistant row."""
    chat = _make_persisted_chat([_make_pending_turn("turn-a", 10)])
    store, _ = _install_reconcile_store(monkeypatch, chat)
    store.add_message(WorkflowCopilotChatSender.USER, "build me a scraper")

    for _ in range(2):
        await _persist_turn_messages(
            chat=chat,
            turn_id="turn-a",
            user_message="build me a scraper",
            audio_artifact_id=None,
            user_row_already_persisted=True,
            assistant_content="Here is your workflow.",
            global_llm_context=None,
            turn_outcome=TurnOutcome(response_kind=ResponseKind.BUILD),
            narrative_payload=None,
        )

    assert len(store.user_messages) == 1
    assert len(store.assistant_messages) == 1
    assert store.assistant_messages[0].turn_outcome is not None
    assert store.assistant_messages[0].turn_outcome.copilot_turn_id == "turn-a"


@pytest.mark.asyncio
async def test_a_finished_turn_replaces_the_interrupted_row_rather_than_dropping_its_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Recovery can reach a slow-but-live turn first; when that turn finishes, its reply is the truth."""
    chat = _make_persisted_chat([_make_pending_turn("turn-a", 10)])
    store, _ = _install_reconcile_store(monkeypatch, chat)
    store.add_message(WorkflowCopilotChatSender.USER, "build me a scraper")
    store.add_message(
        WorkflowCopilotChatSender.AI,
        INTERRUPTED_TERMINAL_MESSAGE,
        TurnOutcome(
            response_kind=ResponseKind.RECOVER,
            reason_code=INTERRUPTED_TERMINAL_REASON,
            terminal_reason=INTERRUPTED_TERMINAL_REASON,
            copilot_turn_id="turn-a",
        ),
    )

    await _persist_turn_messages(
        chat=chat,
        turn_id="turn-a",
        user_message="build me a scraper",
        audio_artifact_id=None,
        user_row_already_persisted=True,
        assistant_content="Here is your workflow.",
        global_llm_context=None,
        turn_outcome=TurnOutcome(response_kind=ResponseKind.BUILD),
        narrative_payload=None,
    )

    assert len(store.assistant_messages) == 1
    assert store.assistant_messages[0].content == "Here is your workflow."
    outcome = store.assistant_messages[0].turn_outcome
    assert outcome is not None
    assert outcome.terminal_reason != INTERRUPTED_TERMINAL_REASON


def test_reconcile_threshold_outlasts_the_turn_enforcement_ceiling() -> None:
    assert RECONCILE_ABANDON_AFTER_SECONDS > TOTAL_TIMEOUT_SECONDS


@pytest.mark.asyncio
async def test_marker_survives_a_finalizer_that_raises_with_no_assistant_row(
    monkeypatch: pytest.MonkeyPatch, api_key_request: MagicMock, copilot_stream: MagicMock
) -> None:
    """No assistant row was written, so the turn stays recoverable instead of being orphaned."""
    monkeypatch.setattr(settings, "ENABLE_WORKFLOW_COPILOT_V2", True)
    captured = install_fake_create(monkeypatch)

    chat = SimpleNamespace(
        workflow_copilot_chat_id="chat-1",
        workflow_permanent_id="wpid-1",
        organization_id="org-1",
        proposed_workflow=None,
        auto_accept=False,
    )
    original_workflow = SimpleNamespace(
        workflow_id="wf-canonical",
        title="Original",
        description="Original description",
        workflow_definition=None,
    )
    agent_result = SimpleNamespace(
        user_response="Here is your workflow.",
        updated_workflow=None,
        global_llm_context=None,
        workflow_yaml=None,
        workflow_was_persisted=False,
        clear_proposed_workflow=False,
        unvalidated=False,
        turn_outcome=None,
    )
    setup_new_copilot_mocks(monkeypatch, chat, original_workflow, agent_result)
    monkeypatch.setattr(
        workflow_copilot_route,
        "_persist_turn_messages",
        AsyncMock(side_effect=RuntimeError("database down")),
    )

    await workflow_copilot_chat_post(api_key_request, _make_chat_request(), SimpleNamespace(organization_id="org-1"))
    handler = captured["handler"]
    assert callable(handler)
    with contextlib.suppress(RuntimeError):
        await handler(copilot_stream)

    assert app.DATABASE.workflow_params.start_copilot_turn.await_count == 1
    app.DATABASE.workflow_params.clear_pending_copilot_turn.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconcile_writes_no_outcome_when_the_canonical_restore_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transient restore failure must not persist a 'recovered' turn over a stranded canonical."""
    pre_turn = _make_copilot_workflow("Before the turn", _NOW - timedelta(hours=2))
    mid_turn_draft = _make_copilot_workflow("Half-built draft", datetime.now(timezone.utc) - timedelta(seconds=1500))
    entry = _make_pending_turn(
        "turn-a",
        RECONCILE_ABANDON_AFTER_SECONDS + 60,
        pre_turn_workflow=pre_turn.model_dump(mode="json"),
        canonical_write_fingerprint=_fingerprint_of(mid_turn_draft),
    )
    chat = _make_persisted_chat([entry])
    store, restore_mock = _install_reconcile_store(monkeypatch, chat, canonical=mid_turn_draft)
    store.add_message(WorkflowCopilotChatSender.USER, "build me a scraper")
    restore_mock.side_effect = RuntimeError("restore failed")

    await _load_history()

    assert store.assistant_messages == []
    assert "turn-a" in chat.pending_turns

    restore_mock.side_effect = None
    entry.recovering_at = None

    await _load_history()

    assert len(store.assistant_messages) == 1
    assert store.assistant_messages[0].turn_outcome is not None
    assert store.assistant_messages[0].turn_outcome.terminal_reason == INTERRUPTED_TERMINAL_REASON
    assert chat.pending_turns == {}


@pytest.mark.asyncio
async def test_reconcile_does_not_roll_back_a_later_turns_canonical_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An older abandoned turn must not attribute — and undo — a newer turn's canonical edit."""
    pre_turn = _make_copilot_workflow("Before the turn", _NOW - timedelta(hours=2))
    later_write = _make_copilot_workflow(
        "Written by the later turn", datetime.now(timezone.utc) - timedelta(seconds=1350)
    )
    older_draft = _make_copilot_workflow("Older turn draft", datetime.now(timezone.utc) - timedelta(seconds=1500))
    older = _make_pending_turn(
        "turn-a",
        RECONCILE_ABANDON_AFTER_SECONDS + 180,
        pre_turn_workflow=pre_turn.model_dump(mode="json"),
        canonical_write_fingerprint=_fingerprint_of(older_draft),
    )
    newer = _make_pending_turn("turn-b", RECONCILE_ABANDON_AFTER_SECONDS + 80)
    chat = _make_persisted_chat([older, newer])
    store, restore_mock = _install_reconcile_store(monkeypatch, chat, canonical=later_write)
    store.add_message(WorkflowCopilotChatSender.USER, "first ask")
    store.add_message(WorkflowCopilotChatSender.USER, "second ask")

    response = await _load_history()

    restore_mock.assert_not_awaited()
    recovered = sorted(m.turn_outcome.copilot_turn_id for m in store.assistant_messages if m.turn_outcome)
    assert recovered == ["turn-a", "turn-b"]
    assert chat.pending_turns == {}
    assert response.proposed_workflow is None


@pytest.mark.asyncio
async def test_finalise_normal_turn_persists_deadline_cause_and_keeps_timeout_copy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        app.AGENT_FUNCTION,
        "should_render_copilot_terminal_from_envelope",
        AsyncMock(return_value=True),
    )
    chat = SimpleNamespace(
        organization_id="org-1",
        workflow_copilot_chat_id="chat-1",
        proposed_workflow=None,
        auto_accept=False,
    )
    original_workflow = SimpleNamespace(workflow_id="wf-canonical")
    timeout_copy = "I ran out of time processing your request. Here's what I have so far."
    envelope = assemble_terminal_envelope(
        response_type="REPLY",
        verified=False,
        workflow_applied=False,
        proposal_disposition="auto_applicable",
        run_outcomes=[],
        blocker_reason=None,
        halt_kind=None,
        attempted=None,
        workflow_mutated=False,
        turn_outcome_response_kind=None,
        terminal_cause="deadline_expired",
    )
    assert envelope is not None
    agent_result = AgentResult(
        user_response=timeout_copy,
        updated_workflow=None,
        global_llm_context=None,
        response_type="REPLY",
        proposal_disposition="auto_applicable",
        narrative_payload=_narrative_payload(),
        terminal_envelope=envelope.model_dump(mode="json"),
    )
    _, workflow_params = setup_new_copilot_mocks(monkeypatch, chat, original_workflow, agent_result)
    stream = MagicMock(send=AsyncMock(return_value=True))

    await workflow_copilot_route._finalise_normal_turn(
        stream=stream,
        chat=chat,
        organization_id="org-1",
        original_workflow=original_workflow,
        chat_request=_make_chat_request(),
        agent_result=agent_result,
    )

    response_frame = stream.send.await_args.args[0]
    assert response_frame.terminal_envelope is not None
    assert response_frame.terminal_envelope["terminal_cause"] == "deadline_expired"
    assert response_frame.message == timeout_copy

    persisted_payload = workflow_params.create_workflow_copilot_chat_message.await_args_list[-1].kwargs[
        "narrative_payload"
    ]
    assert persisted_payload["terminalEnvelope"]["terminal_cause"] == "deadline_expired"
    assert persisted_payload["terminalMessage"] == timeout_copy
    assert persisted_payload["narrativeSummary"] == timeout_copy
