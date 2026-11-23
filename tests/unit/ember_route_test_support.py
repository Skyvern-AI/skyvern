"""Shared route-test scaffolding for the workflow-copilot chat-post stream handler.

Both the route and cancel suites drive ``workflow_copilot_chat_post`` through the same
DB / LLM / agent mocks; these helpers keep that wiring in one place.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, nullcontext
from datetime import UTC, datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.forge import app
from skyvern.forge.sdk.db.exceptions import EmberProposalConflictError
from skyvern.forge.sdk.ember.config import BlockAuthoringPolicy, EmberConfig
from skyvern.forge.sdk.schemas.ember import EMBER_PROPOSAL_METADATA_KEY, ember_proposal_metadata


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


def install_candidate_store(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    async def publish(**kwargs: Any) -> SimpleNamespace:
        metadata = {
            "owner_turn_id": kwargs["owner_turn_id"],
            "revision": 1,
            "canonical_fingerprint": kwargs["canonical_fingerprint"],
            "canonical_title": kwargs["canonical_title"],
            "disposition": kwargs["disposition"],
        }
        return SimpleNamespace(proposed_workflow={**kwargs["proposal"], EMBER_PROPOSAL_METADATA_KEY: metadata})

    store = AsyncMock(side_effect=publish)
    monkeypatch.setattr(app.DATABASE, "workflow_params", SimpleNamespace(publish_ember_candidate=store))
    monkeypatch.setattr(app.DATABASE.workflows, "acquire_workflow_creation_lock", lambda _key: nullcontext())
    return store


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
        "skyvern.forge.sdk.routes.ember.FastAPIEventSourceStream.create",
        fake_create,
    )
    captured["sentinel"] = sentinel
    return captured


def setup_new_ember_mocks(
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
    if not hasattr(agent_result, "cancellation_stopped_facts"):
        agent_result.cancellation_stopped_facts = None
    if not hasattr(agent_result, "proposal_owner_turn_id"):
        agent_result.proposal_owner_turn_id = None
    if not hasattr(agent_result, "proposal_revision"):
        agent_result.proposal_revision = None
    if not hasattr(agent_result, "proposal_workflow_run_id"):
        agent_result.proposal_workflow_run_id = None
    if not hasattr(chat, "workflow_permanent_id"):
        chat.workflow_permanent_id = getattr(original_workflow, "workflow_permanent_id", "wpid-1")
    if not hasattr(original_workflow, "workflow_permanent_id"):
        original_workflow.workflow_permanent_id = chat.workflow_permanent_id
    if not hasattr(chat, "pending_turns"):
        chat.pending_turns = {}
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
        "skyvern.forge.sdk.routes.ember.resolve_main_ember_handler",
        fake_llm_handler,
    )

    restore_mock = AsyncMock()
    monkeypatch.setattr(
        "skyvern.forge.sdk.routes.ember._restore_workflow_definition",
        restore_mock,
    )

    async def publish_ember_candidate(**kwargs: Any) -> SimpleNamespace:
        current = ember_proposal_metadata(chat.proposed_workflow)
        expected = (kwargs["expected_owner_turn_id"], kwargs["expected_revision"])
        if (current is None and expected != (None, None)) or (
            current is not None and expected != (current.owner_turn_id, current.revision)
        ):
            raise EmberProposalConflictError("Copilot proposal changed")
        owner_turn_id = kwargs["owner_turn_id"]
        metadata = {
            "owner_turn_id": owner_turn_id,
            "revision": current.revision + 1 if current is not None and current.owner_turn_id == owner_turn_id else 1,
            "canonical_fingerprint": kwargs["canonical_fingerprint"],
            "canonical_title": kwargs["canonical_title"],
            "disposition": kwargs["disposition"],
        }
        proposal = {**kwargs["proposal"], EMBER_PROPOSAL_METADATA_KEY: metadata}
        chat.proposed_workflow = proposal
        return SimpleNamespace(proposed_workflow=proposal)

    async def clear_ember_candidate(**kwargs: Any) -> None:
        current = ember_proposal_metadata(chat.proposed_workflow)
        expected = (kwargs["expected_owner_turn_id"], kwargs["expected_revision"])
        if (current is None and expected != (None, None)) or (
            current is not None and expected != (current.owner_turn_id, current.revision)
        ):
            raise EmberProposalConflictError("Copilot proposal changed")
        chat.proposed_workflow = None

    async def enrich_ember_candidate(**kwargs: Any) -> SimpleNamespace:
        current = ember_proposal_metadata(chat.proposed_workflow)
        if current is None or (current.owner_turn_id, current.revision) != (
            kwargs["expected_owner_turn_id"],
            kwargs["expected_revision"],
        ):
            raise EmberProposalConflictError("Copilot proposal changed")
        metadata = current.model_copy(update={"disposition": kwargs["disposition"]})
        proposal = {**kwargs["proposal"], EMBER_PROPOSAL_METADATA_KEY: metadata.model_dump(mode="json")}
        chat.proposed_workflow = proposal
        return SimpleNamespace(proposed_workflow=proposal)

    async def claim_ember_candidate(**kwargs: Any) -> SimpleNamespace:
        current = ember_proposal_metadata(chat.proposed_workflow)
        if current is None or (current.owner_turn_id, current.revision) != (
            kwargs["expected_owner_turn_id"],
            kwargs["expected_revision"],
        ):
            raise EmberProposalConflictError("Copilot proposal changed")
        metadata = current.model_copy(update={"disposition": "accepting", "claimed_at": datetime.now(UTC)})
        proposal = {**chat.proposed_workflow, EMBER_PROPOSAL_METADATA_KEY: metadata.model_dump(mode="json")}
        chat.proposed_workflow = proposal
        return SimpleNamespace(proposed_workflow=proposal)

    monkeypatch.setattr(
        "skyvern.forge.sdk.routes.ember.run_ember_agent",
        AsyncMock(return_value=agent_result),
    )

    workflow_params = SimpleNamespace(
        get_ember_chat_by_id=AsyncMock(return_value=chat),
        get_ember_chat_messages=AsyncMock(return_value=[]),
        update_ember_chat=AsyncMock(),
        publish_ember_candidate=AsyncMock(side_effect=publish_ember_candidate),
        enrich_ember_candidate=AsyncMock(side_effect=enrich_ember_candidate),
        clear_ember_candidate=AsyncMock(side_effect=clear_ember_candidate),
        claim_ember_candidate=AsyncMock(side_effect=claim_ember_candidate),
        create_ember_chat_message=AsyncMock(
            return_value=SimpleNamespace(created_at=datetime(2026, 4, 14, tzinfo=timezone.utc))
        ),
        start_ember_turn=AsyncMock(
            return_value=SimpleNamespace(
                workflow_copilot_chat_message_id="wccm-user-1",
                created_at=datetime(2026, 4, 14, tzinfo=timezone.utc),
            )
        ),
        get_ended_ember_turn_ids=AsyncMock(return_value=set()),
        replace_ember_chat_message=AsyncMock(),
        claim_pending_ember_turn=AsyncMock(return_value="claimed"),
        claim_pending_ember_turn_for_finalisation=AsyncMock(return_value="claimed"),
        hold_ember_turn_finalisation=no_finalisation_fence,
        clear_pending_ember_turn=AsyncMock(),
        get_ember_claim_expires_in=AsyncMock(return_value=None),
    )
    app.DATABASE.workflow_params = workflow_params
    app.DATABASE.workflows = SimpleNamespace(
        get_workflow_by_permanent_id=AsyncMock(return_value=original_workflow),
    )
    app.DATABASE.observer = SimpleNamespace(
        get_workflow_run_blocks=AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        app.DATABASE.workflow_runs, "get_unfinished_run_id_on_version", AsyncMock(return_value=None), raising=False
    )
    app.AGENT_FUNCTION.get_ember_config = MagicMock(return_value=None)
    app.AGENT_FUNCTION.get_ember_config_for_request = AsyncMock(
        return_value=EmberConfig(block_authoring_policy=BlockAuthoringPolicy.TASK_V3_PURE)
    )

    return restore_mock, workflow_params
