"""Tests for the workflow-copilot v2 hard-cancel feature (SKY-9305).

Covers:

- ``_watch_for_cancel`` cancels its handler exactly when the cache flag flips
  truthy, sets the ``observed`` closure flag before the cancel, and exits
  cleanly when the handler completes on its own.
- ``_build_exit_result(cancelled=True)`` produces an ``AgentResult`` whose
  ``cancelled`` flag is True and whose ``workflow_was_persisted`` mirrors
  ``CopilotContext.workflow_persisted`` (so the route's rollback decision has
  the same source of truth on cancel as it does on success).
- The route's success-path branch on ``agent_result.cancelled`` runs the
  cancel-specific persistence (rollback + user msg + ``Cancelled by user.``
  AI msg + RESPONSE frame) and skips proposal persistence when there is no WIP.
- ``/workflow/copilot/cancel`` returns 503 when the Redis cache is absent and
  204 + the expected key/TTL when it is present.
- An operational cancel (``task.cancel()`` without ``user_cancel_observed[0]``
  set) does NOT persist a ``Cancelled by user.`` chat row.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException, status

from skyvern.config import settings
from skyvern.forge import app
from skyvern.forge.sdk.copilot.agent import _build_exit_result
from skyvern.forge.sdk.copilot.context import AgentResult, CopilotContext
from skyvern.forge.sdk.routes.workflow_copilot import (
    COPILOT_CANCEL_TTL,
    _copilot_cancel_key,
    _persist_cancel_turn,
    _persist_proposed_workflow_state,
    _watch_for_cancel,
    workflow_copilot_cancel,
    workflow_copilot_chat_post,
)
from skyvern.forge.sdk.schemas.workflow_copilot import (
    WorkflowCopilotCancelRequest,
    WorkflowCopilotChatRequest,
    WorkflowCopilotStreamMessageType,
)
from tests.unit.copilot_route_test_support import install_fake_create, setup_new_copilot_mocks


class _FakeCache:
    """Minimal in-memory double of the ``get`` / ``set`` surface of app.CACHE."""

    def __init__(self) -> None:
        self.store: dict[str, Any] = {}
        self.set_calls: list[tuple[str, Any, Any]] = []

    async def get(self, key: str) -> Any:
        return self.store.get(key)

    async def set(self, key: str, value: Any, ex: Any = None) -> None:
        self.store[key] = value
        self.set_calls.append((key, value, ex))


def _make_chat_request(
    cancel_token: str | None = "tok_abc", keep_pending_proposal: bool = False
) -> WorkflowCopilotChatRequest:
    return WorkflowCopilotChatRequest(
        workflow_permanent_id="wpid-1",
        workflow_id="wf-1",
        workflow_copilot_chat_id="chat-1",
        workflow_run_id=None,
        message="please update",
        workflow_yaml="title: Example",
        cancel_token=cancel_token,
        keep_pending_proposal=keep_pending_proposal,
    )


# ---------------------------------------------------------------------------
# _watch_for_cancel
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_watch_for_cancel_signals_and_sets_observed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Flag flips truthy -> handler_task.cancel() is issued once and observed[0] is True."""
    # Speed the watcher up so the test doesn't wait for the production cadence.
    monkeypatch.setattr("skyvern.forge.sdk.routes.workflow_copilot.COPILOT_CANCEL_POLL_SECONDS", 0.01)
    cache = _FakeCache()
    handler_task = asyncio.create_task(asyncio.sleep(60))
    observed: list[bool] = [False]
    watcher = asyncio.create_task(_watch_for_cancel(cache, "org-1", "tok_abc", handler_task, observed))

    await asyncio.sleep(0.05)
    cache.store[_copilot_cancel_key("org-1", "tok_abc")] = "1"

    with pytest.raises(asyncio.CancelledError):
        await handler_task

    await watcher  # Watcher exits after issuing the cancel.

    assert observed[0] is True


@pytest.mark.asyncio
async def test_watch_for_cancel_exits_when_handler_finishes_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Handler returns before flag flips -> watcher exits cleanly without raising."""
    monkeypatch.setattr("skyvern.forge.sdk.routes.workflow_copilot.COPILOT_CANCEL_POLL_SECONDS", 0.01)
    cache = _FakeCache()
    handler_task = asyncio.create_task(asyncio.sleep(0.02))
    observed: list[bool] = [False]
    watcher = asyncio.create_task(_watch_for_cancel(cache, "org-1", "tok_abc", handler_task, observed))

    await handler_task
    await asyncio.wait_for(watcher, timeout=1.0)

    assert observed[0] is False


# ---------------------------------------------------------------------------
# _build_exit_result(cancelled=...)
# ---------------------------------------------------------------------------


def test_build_exit_result_cancelled_round_trips_workflow_persisted() -> None:
    ctx = CopilotContext(
        organization_id="org-1",
        workflow_id="wf-1",
        workflow_permanent_id="wpid-1",
        workflow_yaml="title: Example",
        browser_session_id=None,
        stream=MagicMock(),
        api_key=None,
        user_message="please update",
        workflow_copilot_chat_id="chat-1",
    )
    ctx.workflow_persisted = True

    result = _build_exit_result(ctx, "Cancelled by user.", None, cancelled=True)

    assert isinstance(result, AgentResult)
    assert result.cancelled is True
    assert result.workflow_was_persisted is True
    assert result.user_response == "Cancelled by user."


def test_build_exit_result_default_cancelled_false() -> None:
    ctx = CopilotContext(
        organization_id="org-1",
        workflow_id="wf-1",
        workflow_permanent_id="wpid-1",
        workflow_yaml="",
        browser_session_id=None,
        stream=MagicMock(),
        api_key=None,
        user_message="msg",
        workflow_copilot_chat_id="chat-1",
    )
    result = _build_exit_result(ctx, "Done.", None)
    assert result.cancelled is False


# ---------------------------------------------------------------------------
# Route cancel branch (agent_result.cancelled=True)
# ---------------------------------------------------------------------------


def _make_chat(*, proposed_workflow: Any = None, auto_accept: bool) -> SimpleNamespace:
    return SimpleNamespace(
        workflow_copilot_chat_id="chat-1",
        workflow_permanent_id="wpid-1",
        organization_id="org-1",
        proposed_workflow=proposed_workflow,
        auto_accept=auto_accept,
    )


def _make_original_workflow() -> SimpleNamespace:
    return SimpleNamespace(
        workflow_id="wf-canonical",
        title="Original",
        description="Original description",
        workflow_definition=None,
    )


@pytest.mark.asyncio
async def test_timeout_wip_persists_proposed_workflow_with_yaml_when_canonical_restored() -> None:
    workflow_params = SimpleNamespace(update_workflow_copilot_chat=AsyncMock())
    app.DATABASE.workflow_params = workflow_params
    chat = _make_chat(auto_accept=False)
    updated_workflow = MagicMock()
    updated_workflow.model_dump.return_value = {
        "workflow_id": "wf-canonical",
        "workflow_definition": {
            "parameters": [{"key": "full_name", "parameter_type": "workflow"}],
            "blocks": [{"block_type": "code", "label": "extract_record_status_info"}],
        },
    }
    agent_result = AgentResult(
        user_response="Timed out, but I have a tested draft.",
        updated_workflow=updated_workflow,
        global_llm_context=None,
        workflow_yaml=(
            "title: Record lookup\n"
            "workflow_definition:\n"
            "  blocks:\n"
            "  - block_type: code\n"
            "    label: extract_record_status_info\n"
        ),
        proposal_disposition="review_tested",
    )

    await _persist_proposed_workflow_state(chat, agent_result, restored=True)

    workflow_params.update_workflow_copilot_chat.assert_awaited_once()
    proposed_workflow = workflow_params.update_workflow_copilot_chat.await_args.kwargs["proposed_workflow"]
    assert proposed_workflow["workflow_definition"]["blocks"][0]["label"] == "extract_record_status_info"
    assert proposed_workflow["_copilot_yaml"].startswith("title: Record lookup")
    assert "_copilot_unvalidated" not in proposed_workflow


async def _drive_cancel_route(
    monkeypatch: pytest.MonkeyPatch,
    chat: SimpleNamespace,
    original_workflow: SimpleNamespace,
    agent_result: SimpleNamespace,
    keep_pending_proposal: bool = False,
) -> tuple[AsyncMock, SimpleNamespace, list[Any]]:
    """Run a single chat-post + handler turn and return (restore_mock, workflow_params, sent_payloads)."""
    monkeypatch.setattr(settings, "ENABLE_WORKFLOW_COPILOT_V2", True)
    captured = install_fake_create(monkeypatch)
    restore_mock, workflow_params = setup_new_copilot_mocks(monkeypatch, chat, original_workflow, agent_result)

    request = MagicMock()
    request.headers = {"x-api-key": "sk-test"}
    organization = SimpleNamespace(organization_id="org-1")

    response = await workflow_copilot_chat_post(
        request, _make_chat_request(keep_pending_proposal=keep_pending_proposal), organization
    )
    assert response is captured["sentinel"]

    sent_payloads: list[Any] = []
    stream = MagicMock()

    async def _send(payload: Any) -> bool:
        sent_payloads.append(payload)
        return True

    stream.send = _send
    stream.is_disconnected = AsyncMock(return_value=False)

    handler = captured["handler"]
    assert callable(handler)
    await handler(stream)
    # Cancel turn always emits exactly two chat rows: the user prompt and the
    # AI cancellation reply. Guard against a future regression that double-inserts.
    assert workflow_params.create_workflow_copilot_chat_message.await_count == 2
    return restore_mock, workflow_params, sent_payloads


@pytest.mark.asyncio
async def test_route_cancel_branch_persists_user_and_cancelled_messages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``agent_result.cancelled=True`` with no WIP -> rollback + cancellation RESPONSE."""
    chat = _make_chat(auto_accept=False)
    original_workflow = _make_original_workflow()
    agent_result = SimpleNamespace(
        user_response="Cancelled by user.",
        updated_workflow=None,
        global_llm_context=None,
        workflow_yaml=None,
        workflow_was_persisted=True,
        clear_proposed_workflow=False,
        cancelled=True,
        total_tokens=None,
        response_type="REPLY",
        turn_outcome=None,
    )
    restore_mock, workflow_params, sent_payloads = await _drive_cancel_route(
        monkeypatch, chat, original_workflow, agent_result
    )

    restore_mock.assert_awaited_once()

    # Two chat-message inserts: user msg + Cancelled by user. AI msg.
    insert_calls = workflow_params.create_workflow_copilot_chat_message.await_args_list
    senders = [c.kwargs.get("sender") for c in insert_calls]
    contents = [c.kwargs.get("content") for c in insert_calls]
    assert senders.count("user") == 1
    assert senders.count("ai") == 1
    assert "please update" in contents
    assert "Cancelled by user." in contents

    # No proposed_workflow update when the cancelled result has no WIP.
    workflow_params.update_workflow_copilot_chat.assert_not_awaited()
    response_frames = [
        p for p in sent_payloads if getattr(p, "type", None) == WorkflowCopilotStreamMessageType.RESPONSE
    ]
    assert len(response_frames) == 1
    assert response_frames[0].message == "Cancelled by user."
    assert response_frames[0].updated_workflow is None
    assert response_frames[0].cancelled is True

    error_frames = [p for p in sent_payloads if getattr(p, "type", None) == WorkflowCopilotStreamMessageType.ERROR]
    assert error_frames == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("disposition", "expect_unvalidated_marker", "title", "user_response"),
    [
        pytest.param(
            "review_untested",
            True,
            "Draft",
            "Cancelled. I have a draft workflow you can keep.",
            id="review_untested-carries-unvalidated-marker",
        ),
        pytest.param(
            "auto_applicable",
            False,
            "Tested Draft",
            "Cancelled. I have a tested draft for you. Accept it to save, or discard.",
            id="auto_applicable-forces-review-no-marker",
        ),
        pytest.param(
            "review_tested",
            False,
            "Last Good Draft",
            "Cancelled. I have a tested draft for you. Accept it to save, or discard.",
            id="review_tested-no-marker",
        ),
    ],
)
async def test_route_cancel_wip_persists_proposal_and_response_frame(
    monkeypatch: pytest.MonkeyPatch,
    disposition: str,
    expect_unvalidated_marker: bool,
    title: str,
    user_response: str,
) -> None:
    """Cancelled WIP under auto_accept=True always persists a review proposal + normal RESPONSE.

    Cancel never auto-applies, so Review/Accept/Reject can render; only
    ``review_untested`` carries the ``_copilot_unvalidated`` marker.
    """
    chat = _make_chat(auto_accept=True)
    original_workflow = _make_original_workflow()
    updated_workflow = MagicMock()
    updated_workflow.model_dump.return_value = {"workflow_id": "wf-canonical", "title": title}
    workflow_yaml = f"title: {title}"
    agent_result = SimpleNamespace(
        user_response=user_response,
        updated_workflow=updated_workflow,
        global_llm_context=None,
        workflow_yaml=workflow_yaml,
        workflow_was_persisted=True,
        clear_proposed_workflow=False,
        cancelled=True,
        total_tokens=123,
        response_type="REPLY",
        proposal_disposition=disposition,
        turn_outcome=None,
    )
    restore_mock, workflow_params, sent_payloads = await _drive_cancel_route(
        monkeypatch, chat, original_workflow, agent_result
    )

    restore_mock.assert_awaited_once()

    workflow_params.update_workflow_copilot_chat.assert_awaited_once()
    proposed_workflow = workflow_params.update_workflow_copilot_chat.await_args.kwargs["proposed_workflow"]
    expected_proposed = {
        "workflow_id": "wf-canonical",
        "title": title,
        "_copilot_yaml": workflow_yaml,
    }
    if expect_unvalidated_marker:
        expected_proposed["_copilot_unvalidated"] = True
    assert proposed_workflow == expected_proposed
    assert ("_copilot_unvalidated" in proposed_workflow) is expect_unvalidated_marker

    insert_calls = workflow_params.create_workflow_copilot_chat_message.await_args_list
    contents = [c.kwargs.get("content") for c in insert_calls]
    assert "please update" in contents
    assert user_response in contents

    response_frames = [
        p for p in sent_payloads if getattr(p, "type", None) == WorkflowCopilotStreamMessageType.RESPONSE
    ]
    assert len(response_frames) == 1
    frame = response_frames[0]
    assert frame.message == user_response
    assert frame.updated_workflow == {"workflow_id": "wf-canonical", "title": title}
    assert frame.proposal_disposition == disposition
    assert frame.cancelled is True

    error_frames = [p for p in sent_payloads if getattr(p, "type", None) == WorkflowCopilotStreamMessageType.ERROR]
    assert error_frames == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("workflow_was_persisted", "expect_restore"),
    [
        pytest.param(True, True, id="persisted-restores-canonical"),
        pytest.param(False, False, id="not-persisted-no-restore"),
    ],
)
async def test_route_cancel_clears_stale_proposed_workflow_when_no_wip(
    monkeypatch: pytest.MonkeyPatch,
    workflow_was_persisted: bool,
    expect_restore: bool,
) -> None:
    """Cancel with no WIP clears any stale proposed_workflow; restore fires only when persisted."""
    chat = _make_chat(
        proposed_workflow={"workflow_id": "wf-canonical", "title": "Stale"},
        auto_accept=False,
    )
    original_workflow = _make_original_workflow()
    agent_result = SimpleNamespace(
        user_response="Cancelled by user.",
        updated_workflow=None,
        global_llm_context=None,
        workflow_yaml=None,
        workflow_was_persisted=workflow_was_persisted,
        clear_proposed_workflow=False,
        cancelled=True,
        total_tokens=None,
        response_type="REPLY",
        proposal_disposition="auto_applicable",
        turn_outcome=None,
    )
    restore_mock, workflow_params, _sent = await _drive_cancel_route(monkeypatch, chat, original_workflow, agent_result)

    if expect_restore:
        restore_mock.assert_awaited_once()
    else:
        restore_mock.assert_not_awaited()
    workflow_params.update_workflow_copilot_chat.assert_awaited_once()
    assert workflow_params.update_workflow_copilot_chat.await_args.kwargs["proposed_workflow"] is None


@pytest.mark.asyncio
async def test_route_cancel_keeps_stale_proposed_workflow_when_no_wip_and_keep_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """keep_pending_proposal=True survives a no-WIP cancel (chip/gate stays actionable)."""
    chat = _make_chat(
        proposed_workflow={"workflow_id": "wf-canonical", "title": "Stale"},
        auto_accept=False,
    )
    original_workflow = _make_original_workflow()
    agent_result = SimpleNamespace(
        user_response="Cancelled by user.",
        updated_workflow=None,
        global_llm_context=None,
        workflow_yaml=None,
        workflow_was_persisted=False,
        clear_proposed_workflow=False,
        cancelled=True,
        total_tokens=None,
        response_type="REPLY",
        proposal_disposition="auto_applicable",
        turn_outcome=None,
    )
    _restore_mock, workflow_params, _sent = await _drive_cancel_route(
        monkeypatch, chat, original_workflow, agent_result, keep_pending_proposal=True
    )

    workflow_params.update_workflow_copilot_chat.assert_not_awaited()


@pytest.mark.asyncio
async def test_route_cancel_explicit_clear_overrides_keep_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """keep_pending_proposal only neutralizes the restored-alone clear; an
    agent-explicit clear_proposed_workflow must still win in the cancel path too."""
    chat = _make_chat(
        proposed_workflow={"workflow_id": "wf-canonical", "title": "Stale"},
        auto_accept=False,
    )
    original_workflow = _make_original_workflow()
    agent_result = SimpleNamespace(
        user_response="Cancelled by user.",
        updated_workflow=None,
        global_llm_context=None,
        workflow_yaml=None,
        workflow_was_persisted=False,
        clear_proposed_workflow=True,
        cancelled=True,
        total_tokens=None,
        response_type="REPLY",
        proposal_disposition="no_proposal",
        turn_outcome=None,
    )
    _restore_mock, workflow_params, _sent = await _drive_cancel_route(
        monkeypatch, chat, original_workflow, agent_result, keep_pending_proposal=True
    )

    workflow_params.update_workflow_copilot_chat.assert_awaited_once()
    assert workflow_params.update_workflow_copilot_chat.await_args.kwargs["proposed_workflow"] is None


@pytest.mark.asyncio
async def test_route_cancel_clears_stale_proposal_when_rollback_itself_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed rollback leaves canonical's state unverified — keep_pending_proposal
    must not be honored against an assumption ("nothing changed") that didn't hold."""
    monkeypatch.setattr(settings, "ENABLE_WORKFLOW_COPILOT_V2", True)
    captured = install_fake_create(monkeypatch)
    chat = _make_chat(
        proposed_workflow={"workflow_id": "wf-canonical", "title": "Stale"},
        auto_accept=False,
    )
    original_workflow = _make_original_workflow()
    agent_result = SimpleNamespace(
        user_response="Cancelled by user.",
        updated_workflow=None,
        global_llm_context=None,
        workflow_yaml=None,
        workflow_was_persisted=True,
        clear_proposed_workflow=False,
        cancelled=True,
        total_tokens=None,
        response_type="REPLY",
        proposal_disposition="no_proposal",
        turn_outcome=None,
    )
    restore_mock, workflow_params = setup_new_copilot_mocks(monkeypatch, chat, original_workflow, agent_result)
    restore_mock.side_effect = RuntimeError("rollback boom")

    request = MagicMock()
    request.headers = {"x-api-key": "sk-test"}
    organization = SimpleNamespace(organization_id="org-1")
    response = await workflow_copilot_chat_post(request, _make_chat_request(keep_pending_proposal=True), organization)
    assert response is captured["sentinel"]

    stream = MagicMock()
    stream.send = AsyncMock(return_value=True)
    stream.is_disconnected = AsyncMock(return_value=False)
    handler = captured["handler"]
    assert callable(handler)
    await handler(stream)

    restore_mock.assert_awaited_once()
    workflow_params.update_workflow_copilot_chat.assert_awaited_once()
    assert workflow_params.update_workflow_copilot_chat.await_args.kwargs["proposed_workflow"] is None


@pytest.mark.asyncio
async def test_pre_agent_cancel_clears_stale_proposed_workflow() -> None:
    """Pre-agent cancel (agent_result=None) must clear any stale proposal.

    Without this, reload reattaches the old card to the new "Cancelled by user." message.
    """
    chat = SimpleNamespace(
        organization_id="org-1",
        workflow_copilot_chat_id="chat-1",
        proposed_workflow={"workflow_id": "wf-canonical", "title": "Stale"},
        auto_accept=False,
    )
    workflow_params = SimpleNamespace(
        update_workflow_copilot_chat=AsyncMock(),
        create_workflow_copilot_chat_message=AsyncMock(
            return_value=SimpleNamespace(created_at=datetime(2026, 4, 27, tzinfo=timezone.utc))
        ),
    )
    app.DATABASE.workflow_params = workflow_params

    stream = MagicMock()
    stream.send = AsyncMock(return_value=True)

    await _persist_cancel_turn(
        stream=stream,
        chat=chat,
        organization_id="org-1",
        original_workflow=None,
        user_message="please update",
        agent_result=None,
    )

    workflow_params.update_workflow_copilot_chat.assert_awaited_once()
    assert workflow_params.update_workflow_copilot_chat.await_args.kwargs["proposed_workflow"] is None


@pytest.mark.asyncio
async def test_pre_agent_cancel_keeps_stale_proposed_workflow_when_keep_pending() -> None:
    """keep_pending_proposal=True survives a pre-agent cancel too."""
    chat = SimpleNamespace(
        organization_id="org-1",
        workflow_copilot_chat_id="chat-1",
        proposed_workflow={"workflow_id": "wf-canonical", "title": "Stale"},
        auto_accept=False,
    )
    workflow_params = SimpleNamespace(
        update_workflow_copilot_chat=AsyncMock(),
        create_workflow_copilot_chat_message=AsyncMock(
            return_value=SimpleNamespace(created_at=datetime(2026, 4, 27, tzinfo=timezone.utc))
        ),
    )
    app.DATABASE.workflow_params = workflow_params

    stream = MagicMock()
    stream.send = AsyncMock(return_value=True)

    await _persist_cancel_turn(
        stream=stream,
        chat=chat,
        organization_id="org-1",
        original_workflow=None,
        user_message="please update",
        agent_result=None,
        keep_pending_proposal=True,
    )

    workflow_params.update_workflow_copilot_chat.assert_not_awaited()


@pytest.mark.asyncio
async def test_timeout_wip_result_streams_normal_response_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Timeout WIP rescue must use normal finalisation, not the cancel ERROR path."""
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
    updated_workflow = MagicMock()
    updated_workflow.model_dump.side_effect = lambda mode="json": {"workflow_id": "wf-draft", "title": "Draft"}
    agent_result = SimpleNamespace(
        user_response="I ran out of time before I could finish testing. I have a draft workflow you can keep.",
        updated_workflow=updated_workflow,
        global_llm_context=None,
        workflow_yaml="version: '1.0'",
        workflow_was_persisted=True,
        clear_proposed_workflow=False,
        cancelled=False,
        total_tokens=123,
        response_type="REPLY",
        proposal_disposition="review_untested",
        turn_outcome=None,
    )
    restore_mock, workflow_params = setup_new_copilot_mocks(monkeypatch, chat, original_workflow, agent_result)

    request = MagicMock()
    request.headers = {"x-api-key": "sk-test"}
    organization = SimpleNamespace(organization_id="org-1")

    response = await workflow_copilot_chat_post(request, _make_chat_request(), organization)
    assert response is captured["sentinel"]

    sent_payloads: list[Any] = []
    stream = MagicMock()

    async def _send(payload: Any) -> bool:
        sent_payloads.append(payload)
        return True

    stream.send = _send
    stream.is_disconnected = AsyncMock(return_value=False)

    handler = captured["handler"]
    assert callable(handler)
    await handler(stream)

    restore_mock.assert_awaited_once()
    proposal = workflow_params.update_workflow_copilot_chat.await_args.kwargs["proposed_workflow"]
    assert proposal["_copilot_yaml"] == "version: '1.0'"
    assert proposal["_copilot_unvalidated"] is True

    response_frames = [
        p for p in sent_payloads if getattr(p, "type", None) == WorkflowCopilotStreamMessageType.RESPONSE
    ]
    assert len(response_frames) == 1
    assert response_frames[0].updated_workflow == {"workflow_id": "wf-draft", "title": "Draft"}
    assert response_frames[0].proposal_disposition == "review_untested"
    assert response_frames[0].total_tokens == 123

    error_frames = [p for p in sent_payloads if getattr(p, "type", None) == WorkflowCopilotStreamMessageType.ERROR]
    assert error_frames == []
    contents = [c.kwargs.get("content") for c in workflow_params.create_workflow_copilot_chat_message.await_args_list]
    assert "Cancelled by user." not in contents


@pytest.mark.asyncio
async def test_verified_terminal_timeout_result_persists_proposed_workflow_with_blocks_and_params() -> None:
    workflow_params = SimpleNamespace(update_workflow_copilot_chat=AsyncMock())
    app.DATABASE.workflow_params = workflow_params
    chat = _make_chat(proposed_workflow=None, auto_accept=False)
    blocks = [
        {"block_type": "code", "label": "open_search_search_page"},
        {"block_type": "code", "label": "search_and_open_record_details"},
        {"block_type": "code", "label": "extract_record_status_record"},
    ]
    params = [{"key": f"param_{index}", "parameter_type": "workflow"} for index in range(6)]
    updated_workflow = MagicMock()
    updated_workflow.model_dump.side_effect = lambda mode="json": {
        "workflow_id": "wf-draft",
        "title": "Record Status Draft",
        "workflow_definition": {"blocks": blocks, "parameters": params},
    }
    agent_result = AgentResult(
        user_response="I created and tested the workflow successfully.",
        updated_workflow=updated_workflow,
        global_llm_context=None,
        workflow_yaml=(
            "title: Record Status Draft\n"
            "workflow_definition:\n"
            "  parameters:\n"
            "  - key: param_0\n"
            "  blocks:\n"
            "  - block_type: code\n"
            "    label: open_search_search_page\n"
            "  - block_type: code\n"
            "    label: search_and_open_record_details\n"
            "  - block_type: code\n"
            "    label: extract_record_status_record\n"
        ),
        workflow_was_persisted=True,
        proposal_disposition="auto_applicable",
    )

    await _persist_proposed_workflow_state(chat, agent_result, restored=False)

    proposal = workflow_params.update_workflow_copilot_chat.await_args.kwargs["proposed_workflow"]
    assert proposal["_copilot_yaml"].startswith("title: Record Status Draft")
    assert [block["label"] for block in proposal["workflow_definition"]["blocks"]] == [
        "open_search_search_page",
        "search_and_open_record_details",
        "extract_record_status_record",
    ]
    assert len(proposal["workflow_definition"]["parameters"]) == 6
    assert "_copilot_unvalidated" not in proposal


@pytest.mark.asyncio
async def test_timeout_wip_review_tested_propagates_to_response_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-cancel WIP rescue propagates ``review_tested`` so the frontend skips auto-apply."""
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
    updated_workflow = MagicMock()
    updated_workflow.model_dump.side_effect = lambda mode="json": {"workflow_id": "wf-good", "title": "Last Good"}
    agent_result = SimpleNamespace(
        user_response="I ran out of time, but I have a tested draft for you. Accept it to save, or discard.",
        updated_workflow=updated_workflow,
        global_llm_context=None,
        workflow_yaml="title: Last Good",
        workflow_was_persisted=True,
        clear_proposed_workflow=False,
        cancelled=False,
        total_tokens=789,
        response_type="REPLY",
        proposal_disposition="review_tested",
        turn_outcome=None,
    )
    restore_mock, workflow_params = setup_new_copilot_mocks(monkeypatch, chat, original_workflow, agent_result)

    request = MagicMock()
    request.headers = {"x-api-key": "sk-test"}
    organization = SimpleNamespace(organization_id="org-1")

    response = await workflow_copilot_chat_post(request, _make_chat_request(), organization)
    assert response is captured["sentinel"]

    sent_payloads: list[Any] = []
    stream = MagicMock()

    async def _send(payload: Any) -> bool:
        sent_payloads.append(payload)
        return True

    stream.send = _send
    stream.is_disconnected = AsyncMock(return_value=False)

    handler = captured["handler"]
    assert callable(handler)
    await handler(stream)

    restore_mock.assert_awaited_once()
    proposal = workflow_params.update_workflow_copilot_chat.await_args.kwargs["proposed_workflow"]
    assert proposal["_copilot_yaml"] == "title: Last Good"
    assert "_copilot_unvalidated" not in proposal

    response_frames = [
        p for p in sent_payloads if getattr(p, "type", None) == WorkflowCopilotStreamMessageType.RESPONSE
    ]
    assert len(response_frames) == 1
    assert response_frames[0].proposal_disposition == "review_tested"
    assert response_frames[0].cancelled is False


# ---------------------------------------------------------------------------
# /workflow/copilot/cancel endpoint
# ---------------------------------------------------------------------------
#
# ``app`` is an ``AppHolder`` proxy that forwards ``__setattr__`` / ``__getattr__``
# to a wrapped ``ForgeApp`` instance, but does not implement ``__delattr__``.
# That means ``monkeypatch.setattr(app, "CACHE", ...)`` works on assignment but
# its teardown ``delattr`` raises. Manipulate the underlying instance instead so
# monkeypatch's teardown lands on a normal attribute.


@pytest.mark.asyncio
async def test_cancel_endpoint_503_when_cache_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app._inst, "CACHE", None, raising=False)
    organization = SimpleNamespace(organization_id="org-1")

    with pytest.raises(HTTPException) as excinfo:
        await workflow_copilot_cancel(
            WorkflowCopilotCancelRequest(cancel_token="tok_abc"),
            organization=organization,
        )
    assert excinfo.value.status_code == status.HTTP_503_SERVICE_UNAVAILABLE


@pytest.mark.asyncio
async def test_cancel_endpoint_204_writes_redis_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    cache = _FakeCache()
    monkeypatch.setattr(app._inst, "CACHE", cache, raising=False)
    organization = SimpleNamespace(organization_id="org-1")

    result = await workflow_copilot_cancel(
        WorkflowCopilotCancelRequest(cancel_token="tok_abc"),
        organization=organization,
    )
    assert result is None  # 204 No Content

    expected_key = _copilot_cancel_key("org-1", "tok_abc")
    assert cache.store[expected_key] == "1"
    assert len(cache.set_calls) == 1
    key, value, ex = cache.set_calls[0]
    assert (key, value) == (expected_key, "1")
    assert isinstance(ex, timedelta)
    assert ex == COPILOT_CANCEL_TTL


# ---------------------------------------------------------------------------
# Operational cancel disambiguation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_operational_cancel_does_not_persist_cancelled_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """task.cancel() without user_cancel_observed[0] -> no 'Cancelled by user.' chat row.

    The route should treat such a cancel as operational (deploy drain / SIGINT)
    and re-raise without manufacturing a user-cancel chat row.
    """
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
        description=None,
        workflow_definition=None,
    )
    # Agent raises CancelledError synchronously (simulates operational cancel
    # propagating into the await before user_cancel_observed gets set).
    monkeypatch.setattr(
        "skyvern.forge.sdk.routes.workflow_copilot.run_copilot_agent",
        AsyncMock(side_effect=asyncio.CancelledError()),
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

    workflow_params = SimpleNamespace(
        get_workflow_copilot_chat_by_id=AsyncMock(return_value=chat),
        get_workflow_copilot_chat_messages=AsyncMock(return_value=[]),
        update_workflow_copilot_chat=AsyncMock(),
        create_workflow_copilot_chat_message=AsyncMock(
            return_value=SimpleNamespace(created_at=datetime(2026, 4, 27, tzinfo=timezone.utc))
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

    # Make sure no cache is configured so the watcher never spawns and
    # user_cancel_observed[0] stays False.
    monkeypatch.setattr(app._inst, "CACHE", None, raising=False)

    request = MagicMock()
    request.headers = {"x-api-key": "sk-test"}
    organization = SimpleNamespace(organization_id="org-1")

    response = await workflow_copilot_chat_post(request, _make_chat_request(cancel_token=None), organization)
    assert response is captured["sentinel"]

    stream = MagicMock()
    stream.send = AsyncMock(return_value=True)
    stream.is_disconnected = AsyncMock(return_value=False)

    handler = captured["handler"]
    assert callable(handler)
    with pytest.raises(asyncio.CancelledError):
        await handler(stream)

    insert_calls = workflow_params.create_workflow_copilot_chat_message.await_args_list
    contents = [c.kwargs.get("content") for c in insert_calls]
    # No "Cancelled by user." row was written.
    assert "Cancelled by user." not in contents
