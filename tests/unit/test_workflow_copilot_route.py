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

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.config import settings
from skyvern.forge import app
from skyvern.forge.sdk.routes.workflow_copilot import workflow_copilot_chat_post
from skyvern.forge.sdk.schemas.workflow_copilot import WorkflowCopilotChatRequest


def _make_chat_request() -> WorkflowCopilotChatRequest:
    return WorkflowCopilotChatRequest(
        workflow_permanent_id="wpid-1",
        workflow_id="wf-request",
        workflow_copilot_chat_id="chat-1",
        workflow_run_id=None,
        message="Please update it",
        workflow_yaml="title: Example",
    )


def _install_fake_create(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    """Capture the stream handler that the route hands to EventSourceStream."""
    captured: dict[str, object] = {}
    sentinel = object()

    def fake_create(request: object, handler: object, ping_interval: int = 10) -> object:
        del request, ping_interval
        captured["handler"] = handler
        return sentinel

    monkeypatch.setattr(
        "skyvern.forge.sdk.routes.workflow_copilot.FastAPIEventSourceStream.create",
        fake_create,
    )
    captured["sentinel"] = sentinel
    return captured


@pytest.mark.asyncio
async def test_flag_off_dispatches_to_old_copilot(monkeypatch: pytest.MonkeyPatch) -> None:
    """Flag off -> workflow_copilot_chat_post must use the old-copilot stream handler.

    We verify by patching _new_copilot_chat_post to something that would
    raise if called, then confirming the old path was used instead.
    """
    monkeypatch.setattr(settings, "ENABLE_WORKFLOW_COPILOT_V2", False)

    new_copilot_mock = AsyncMock(side_effect=AssertionError("new-copilot path must not run when flag is off"))
    monkeypatch.setattr(
        "skyvern.forge.sdk.routes.workflow_copilot._new_copilot_chat_post",
        new_copilot_mock,
    )

    captured = _install_fake_create(monkeypatch)

    request = MagicMock()
    request.headers = {}
    organization = SimpleNamespace(organization_id="org-1")

    response = await workflow_copilot_chat_post(request, _make_chat_request(), organization)

    assert response is captured["sentinel"]
    new_copilot_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_flag_on_dispatches_to_new_copilot(monkeypatch: pytest.MonkeyPatch) -> None:
    """Flag on -> workflow_copilot_chat_post delegates to _new_copilot_chat_post."""
    monkeypatch.setattr(settings, "ENABLE_WORKFLOW_COPILOT_V2", True)

    sentinel = object()
    new_copilot_mock = AsyncMock(return_value=sentinel)
    monkeypatch.setattr(
        "skyvern.forge.sdk.routes.workflow_copilot._new_copilot_chat_post",
        new_copilot_mock,
    )

    request = MagicMock()
    request.headers = {}
    organization = SimpleNamespace(organization_id="org-1")

    response = await workflow_copilot_chat_post(request, _make_chat_request(), organization)

    assert response is sentinel
    new_copilot_mock.assert_awaited_once()


def _setup_new_copilot_mocks(
    monkeypatch: pytest.MonkeyPatch,
    chat: SimpleNamespace,
    original_workflow: SimpleNamespace,
    agent_result: SimpleNamespace,
) -> AsyncMock:
    """Wire up everything the new-copilot stream handler touches.

    Returns the restore-on-error mock so callers can assert on it.
    """

    async def fake_llm_handler(*args: object, **kwargs: object) -> None:
        del args, kwargs
        return None

    monkeypatch.setattr(
        "skyvern.forge.sdk.routes.workflow_copilot.get_llm_handler_for_prompt_type",
        fake_llm_handler,
    )

    restore_mock = AsyncMock()
    monkeypatch.setattr(
        "skyvern.forge.sdk.routes.workflow_copilot._restore_workflow_definition",
        restore_mock,
    )

    run_agent_mock = AsyncMock(return_value=agent_result)
    monkeypatch.setattr(
        "skyvern.forge.sdk.routes.workflow_copilot.run_copilot_agent",
        run_agent_mock,
    )

    # DB surfaces: the new-copilot handler reaches the repository directly via
    # app.DATABASE.workflow_params.*  and app.DATABASE.workflows.*  -- mock
    # those attribute chains.
    app.DATABASE.workflow_params = SimpleNamespace(
        get_workflow_copilot_chat_by_id=AsyncMock(return_value=chat),
        get_workflow_copilot_chat_messages=AsyncMock(return_value=[]),
        update_workflow_copilot_chat=AsyncMock(),
        create_workflow_copilot_chat_message=AsyncMock(
            return_value=SimpleNamespace(created_at=datetime(2026, 4, 14, tzinfo=timezone.utc))
        ),
    )
    app.DATABASE.workflows = SimpleNamespace(
        get_workflow_by_permanent_id=AsyncMock(return_value=original_workflow),
    )
    app.DATABASE.observer = SimpleNamespace(
        get_workflow_run_blocks=AsyncMock(return_value=[]),
    )
    app.AGENT_FUNCTION.get_copilot_security_rules = MagicMock(return_value="")

    return restore_mock


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("auto_accept", "workflow_was_persisted", "expect_restore"),
    [
        (True, True, False),  # auto_accept True => no restore
        (False, False, False),  # nothing persisted => nothing to restore
        (False, True, True),  # mid-stream disconnect with a persisted draft => restore
    ],
)
async def test_flag_on_mid_stream_disconnect_restores_when_persisted_and_not_auto_accept(
    monkeypatch: pytest.MonkeyPatch,
    auto_accept: bool,
    workflow_was_persisted: bool,
    expect_restore: bool,
) -> None:
    monkeypatch.setattr(settings, "ENABLE_WORKFLOW_COPILOT_V2", True)

    captured = _install_fake_create(monkeypatch)

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
    agent_result = SimpleNamespace(
        user_response="done",
        updated_workflow=None,
        global_llm_context=None,
        workflow_yaml=None,
        workflow_was_persisted=workflow_was_persisted,
        clear_proposed_workflow=False,
    )

    restore_mock = _setup_new_copilot_mocks(monkeypatch, chat, original_workflow, agent_result)

    request = MagicMock()
    request.headers = {"x-api-key": "sk-test-key"}
    organization = SimpleNamespace(organization_id="org-1")

    response = await workflow_copilot_chat_post(request, _make_chat_request(), organization)
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
