"""Shared route-test scaffolding for the workflow-copilot chat-post stream handler.

Both the route and cancel suites drive ``workflow_copilot_chat_post`` through the same
DB / LLM / agent mocks; these helpers keep that wiring in one place.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.forge import app
from skyvern.forge.sdk.copilot.config import BlockAuthoringPolicy, CopilotConfig
from skyvern.forge.sdk.db.exceptions import CopilotProposalConflictError
from skyvern.forge.sdk.schemas.workflow_copilot import COPILOT_PROPOSAL_METADATA_KEY, copilot_proposal_metadata


def terminal_narrative_payload() -> dict[str, Any]:
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


def narrative_payload_with_run(run_id: str | None) -> dict[str, Any]:
    return {
        **terminal_narrative_payload(),
        "turnFacts": {
            "factsAvailable": True,
            "evaluationState": None,
            "runId": run_id,
            "runCompleted": None,
            "terminalCause": None,
            "blocksRunThisTurn": None,
            "ranCleanOnCurrentSource": False,
        },
    }


@asynccontextmanager
async def no_finalisation_fence(*_: object) -> AsyncIterator[None]:
    yield


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
    if not hasattr(agent_result, "work_plan"):
        agent_result.work_plan = None
    if not hasattr(agent_result, "executed_block_fingerprints"):
        agent_result.executed_block_fingerprints = {}
    if not hasattr(agent_result, "cancellation_iteration"):
        agent_result.cancellation_iteration = None
    if not hasattr(agent_result, "cancellation_last_recorded_phase"):
        agent_result.cancellation_last_recorded_phase = None
    if not hasattr(agent_result, "cancellation_workflow_run_id"):
        agent_result.cancellation_workflow_run_id = None
    if not hasattr(agent_result, "proposal_owner_turn_id"):
        agent_result.proposal_owner_turn_id = None
    if not hasattr(agent_result, "proposal_revision"):
        agent_result.proposal_revision = None
    if not hasattr(agent_result, "proposal_workflow_run_id"):
        agent_result.proposal_workflow_run_id = None
    if not hasattr(chat, "workflow_permanent_id"):
        chat.workflow_permanent_id = getattr(original_workflow, "workflow_permanent_id", "wpid-1")
    if not hasattr(original_workflow, "modified_at"):
        original_workflow.modified_at = datetime(2026, 4, 14, tzinfo=timezone.utc)
    if not hasattr(original_workflow, "title"):
        original_workflow.title = "Original"
    if not hasattr(original_workflow, "model_dump"):
        original_workflow.model_dump = MagicMock(return_value={"workflow_id": original_workflow.workflow_id})

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

    async def publish_workflow_copilot_candidate(**kwargs: Any) -> SimpleNamespace:
        current = copilot_proposal_metadata(chat.proposed_workflow)
        expected = (kwargs["expected_owner_turn_id"], kwargs["expected_revision"])
        if (current is None and expected != (None, None)) or (
            current is not None and expected != (current.owner_turn_id, current.revision)
        ):
            raise CopilotProposalConflictError("Copilot proposal changed")
        owner_turn_id = kwargs["owner_turn_id"]
        metadata = {
            "owner_turn_id": owner_turn_id,
            "revision": current.revision + 1 if current is not None and current.owner_turn_id == owner_turn_id else 1,
            "canonical_fingerprint": kwargs["canonical_fingerprint"],
            "canonical_title": kwargs["canonical_title"],
            "disposition": kwargs["disposition"],
        }
        proposal = {**kwargs["proposal"], COPILOT_PROPOSAL_METADATA_KEY: metadata}
        chat.proposed_workflow = proposal
        return SimpleNamespace(proposed_workflow=proposal)

    async def clear_workflow_copilot_candidate(**kwargs: Any) -> None:
        current = copilot_proposal_metadata(chat.proposed_workflow)
        expected = (kwargs["expected_owner_turn_id"], kwargs["expected_revision"])
        if (current is None and expected != (None, None)) or (
            current is not None and expected != (current.owner_turn_id, current.revision)
        ):
            raise CopilotProposalConflictError("Copilot proposal changed")
        chat.proposed_workflow = None

    async def enrich_workflow_copilot_candidate(**kwargs: Any) -> SimpleNamespace:
        current = copilot_proposal_metadata(chat.proposed_workflow)
        if current is None or (current.owner_turn_id, current.revision) != (
            kwargs["expected_owner_turn_id"],
            kwargs["expected_revision"],
        ):
            raise CopilotProposalConflictError("Copilot proposal changed")
        metadata = current.model_copy(update={"disposition": kwargs["disposition"]})
        proposal = {**kwargs["proposal"], COPILOT_PROPOSAL_METADATA_KEY: metadata.model_dump(mode="json")}
        chat.proposed_workflow = proposal
        return SimpleNamespace(proposed_workflow=proposal)

    async def claim_workflow_copilot_candidate(**kwargs: Any) -> SimpleNamespace:
        current = copilot_proposal_metadata(chat.proposed_workflow)
        if current is None or (current.owner_turn_id, current.revision) != (
            kwargs["expected_owner_turn_id"],
            kwargs["expected_revision"],
        ):
            raise CopilotProposalConflictError("Copilot proposal changed")
        metadata = current.model_copy(update={"disposition": "accepting", "claimed_at": datetime.now(UTC)})
        proposal = {**chat.proposed_workflow, COPILOT_PROPOSAL_METADATA_KEY: metadata.model_dump(mode="json")}
        chat.proposed_workflow = proposal
        return SimpleNamespace(proposed_workflow=proposal)

    monkeypatch.setattr(
        "skyvern.forge.sdk.routes.workflow_copilot.run_copilot_agent",
        AsyncMock(return_value=agent_result),
    )

    workflow_params = SimpleNamespace(
        get_workflow_copilot_chat_by_id=AsyncMock(return_value=chat),
        get_workflow_copilot_chat_messages=AsyncMock(return_value=[]),
        update_workflow_copilot_chat=AsyncMock(),
        publish_workflow_copilot_candidate=AsyncMock(side_effect=publish_workflow_copilot_candidate),
        enrich_workflow_copilot_candidate=AsyncMock(side_effect=enrich_workflow_copilot_candidate),
        clear_workflow_copilot_candidate=AsyncMock(side_effect=clear_workflow_copilot_candidate),
        claim_workflow_copilot_candidate=AsyncMock(side_effect=claim_workflow_copilot_candidate),
        create_workflow_copilot_chat_message=AsyncMock(
            return_value=SimpleNamespace(created_at=datetime(2026, 4, 14, tzinfo=timezone.utc))
        ),
        start_copilot_turn=AsyncMock(
            return_value=SimpleNamespace(
                workflow_copilot_chat_message_id="wccm-user-1",
                created_at=datetime(2026, 4, 14, tzinfo=timezone.utc),
            )
        ),
        replace_workflow_copilot_chat_message=AsyncMock(),
        claim_pending_copilot_turn=AsyncMock(return_value=True),
        claim_pending_copilot_turn_for_finalisation=AsyncMock(return_value="claimed"),
        hold_copilot_turn_finalisation=no_finalisation_fence,
        clear_pending_copilot_turn=AsyncMock(),
        get_workflow_copilot_claim_expires_in=AsyncMock(return_value=None),
    )
    app.DATABASE.workflow_params = workflow_params
    app.DATABASE.workflows = SimpleNamespace(
        get_workflow_by_permanent_id=AsyncMock(return_value=original_workflow),
    )
    app.DATABASE.observer = SimpleNamespace(
        get_workflow_run_blocks=AsyncMock(return_value=[]),
    )
    app.AGENT_FUNCTION.get_copilot_config = MagicMock(return_value=None)
    app.AGENT_FUNCTION.get_copilot_config_for_request = AsyncMock(
        return_value=CopilotConfig(block_authoring_policy=BlockAuthoringPolicy.TASK_V3_PURE)
    )

    return restore_mock, workflow_params
