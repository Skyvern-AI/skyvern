from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.forge import app
from skyvern.forge.sdk.routes.workflow_copilot import workflow_copilot_chat_post
from skyvern.forge.sdk.schemas.workflow_copilot import WorkflowCopilotChatRequest


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("auto_accept", "expect_restore"),
    [
        (True, False),
        (False, True),
    ],
)
async def test_disconnect_after_agent_loop_restore_behavior_follows_auto_accept_and_persisted_state(
    monkeypatch: pytest.MonkeyPatch,
    auto_accept: bool,
    expect_restore: bool,
) -> None:
    captured_handler: dict[str, object] = {}
    sentinel_response = object()

    def fake_create(request: object, handler: object, ping_interval: int = 10) -> object:
        del request, ping_interval
        captured_handler["handler"] = handler
        return sentinel_response

    monkeypatch.setattr(
        "skyvern.forge.sdk.routes.workflow_copilot.FastAPIEventSourceStream.create",
        fake_create,
    )

    async def fake_llm_handler(*args: object, **kwargs: object) -> None:
        del args, kwargs
        return None

    monkeypatch.setattr(
        "skyvern.forge.sdk.routes.workflow_copilot.get_llm_handler_for_prompt_type",
        fake_llm_handler,
    )

    restore_mock = AsyncMock()
    monkeypatch.setattr(
        "skyvern.forge.sdk.routes.workflow_copilot._restore_workflow_on_error",
        restore_mock,
    )

    agent_result = SimpleNamespace(
        user_response="done",
        updated_workflow=None,
        global_llm_context=None,
        workflow_yaml=None,
        workflow_was_persisted=True,
    )
    run_agent_mock = AsyncMock(return_value=agent_result)
    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.agent.run_copilot_agent",
        run_agent_mock,
    )

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

    app.DATABASE.get_workflow_copilot_chat_by_id = AsyncMock(return_value=chat)
    app.DATABASE.get_workflow_copilot_chat_messages = AsyncMock(return_value=[])
    app.DATABASE.get_workflow_by_permanent_id = AsyncMock(return_value=original_workflow)

    request = MagicMock()
    request.headers = {"x-api-key": "sk-test-key"}

    chat_request = WorkflowCopilotChatRequest(
        workflow_permanent_id="wpid-1",
        workflow_id="wf-request",
        workflow_copilot_chat_id="chat-1",
        workflow_run_id=None,
        message="Please update it",
        workflow_yaml="title: Example",
    )
    organization = SimpleNamespace(organization_id="org-1")

    response = await workflow_copilot_chat_post(request, chat_request, organization)
    assert response is sentinel_response

    stream = MagicMock()
    stream.send = AsyncMock(return_value=True)
    stream.is_disconnected = AsyncMock(side_effect=[False, True])

    handler = captured_handler["handler"]
    assert callable(handler)
    await handler(stream)

    if expect_restore:
        restore_mock.assert_awaited_once()
    else:
        restore_mock.assert_not_awaited()
