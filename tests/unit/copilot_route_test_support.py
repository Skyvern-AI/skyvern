"""Shared route-test scaffolding for the workflow-copilot chat-post stream handler.

Both the route and cancel suites drive ``workflow_copilot_chat_post`` through the same
DB / LLM / agent mocks; these helpers keep that wiring in one place.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.forge import app


def install_fake_create(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
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


def setup_new_copilot_mocks(
    monkeypatch: pytest.MonkeyPatch,
    chat: SimpleNamespace,
    original_workflow: SimpleNamespace,
    agent_result: SimpleNamespace,
) -> tuple[AsyncMock, SimpleNamespace]:
    """Wire up everything the new-copilot stream handler touches.

    Returns the restore-on-error mock and the ``workflow_params`` namespace so callers
    can assert on either.
    """
    if not hasattr(agent_result, "response_type"):
        agent_result.response_type = "REPLY"
    if not hasattr(agent_result, "total_tokens"):
        agent_result.total_tokens = None
    if not hasattr(agent_result, "output_policy_diagnostics"):
        agent_result.output_policy_diagnostics = None
    if not hasattr(agent_result, "turn_id"):
        agent_result.turn_id = None
    if not hasattr(agent_result, "narrative_summary"):
        agent_result.narrative_summary = None
    if not hasattr(agent_result, "narrative_payload"):
        agent_result.narrative_payload = None

    async def fake_llm_handler(*args: object, **kwargs: object) -> None:
        del args, kwargs
        return None

    monkeypatch.setattr(
        "skyvern.forge.sdk.routes.workflow_copilot.resolve_main_copilot_handler",
        fake_llm_handler,
    )

    restore_mock = AsyncMock()
    monkeypatch.setattr(
        "skyvern.forge.sdk.routes.workflow_copilot._restore_workflow_definition",
        restore_mock,
    )

    monkeypatch.setattr(
        "skyvern.forge.sdk.routes.workflow_copilot.run_copilot_agent",
        AsyncMock(return_value=agent_result),
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

    return restore_mock, workflow_params
