import asyncio
import copy
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response
from pydantic_core import to_json
from sqlalchemy import event, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session
from structlog.testing import capture_logs

from skyvern.config import settings
from skyvern.exceptions import HttpException
from skyvern.forge import app
from skyvern.forge.sdk.copilot.ask_user import (
    CREDENTIAL_DELETE_CLAIM_LEASE,
    MAX_CREDENTIALS_PER_CARD,
    CredentialDeleteOutcome,
    QuestionInteraction,
    QuestionResponse,
)
from skyvern.forge.sdk.copilot.browser_ablation import resolve_copilot_tool_surface
from skyvern.forge.sdk.copilot.context import CopilotContext
from skyvern.forge.sdk.copilot.tools import copilot_native_tools
from skyvern.forge.sdk.copilot.tools.credential_deletion import delete_saved_credentials
from skyvern.forge.sdk.db.models import CredentialModel, WorkflowCopilotChatMessageModel, WorkflowCopilotChatModel
from skyvern.forge.sdk.routes import credentials as credential_routes
from skyvern.forge.sdk.routes import workflow_copilot as routes
from skyvern.forge.sdk.schemas.credentials import Credential, CredentialVaultType
from skyvern.forge.sdk.schemas.workflow_copilot import CopilotPendingTurn
from skyvern.forge.sdk.schemas.workflow_copilot import WorkflowCopilotChatSender as Sender
from tests.unit.conftest import RUN_GROUP_ORG, RUN_GROUP_WPID, GroupEnv

SECRET = "vault-secret-fixture-55190"


@dataclass
class FakeVault:
    failing: set[str] = field(default_factory=set)
    gate: asyncio.Event | None = None
    entered: asyncio.Event = field(default_factory=asyncio.Event)
    deleted: list[str] = field(default_factory=list)
    get_credential_item: AsyncMock = field(
        default_factory=lambda: AsyncMock(return_value=SimpleNamespace(credential=SimpleNamespace(secret=SECRET)))
    )
    post_delete_credential_item: AsyncMock = field(default_factory=lambda: AsyncMock(return_value=True))

    async def delete_credential(self, credential: Credential) -> None:
        self.entered.set()
        if self.gate is not None:
            await self.gate.wait()
        if credential.credential_id in self.failing:
            raise HttpException(503, "https://vault.invalid")
        await app.DATABASE.credentials.delete_credential(credential.credential_id, credential.organization_id)
        self.deleted.append(credential.credential_id)


@dataclass
class Chat:
    env: GroupEnv
    ctx: CopilotContext
    client: AsyncClient
    frames: asyncio.Queue[dict[str, Any]]
    vault: FakeVault
    polled: asyncio.Event
    monkeypatch: pytest.MonkeyPatch

    @property
    def chat_id(self) -> str:
        assert self.ctx.workflow_copilot_chat_id is not None
        return self.ctx.workflow_copilot_chat_id


@pytest_asyncio.fixture
async def chat(run_group_env: GroupEnv, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Chat]:
    async with run_group_env.database.Session() as session:
        session.add(
            CredentialModel(
                credential_id="cred_3",
                organization_id=RUN_GROUP_ORG,
                name="Payments key",
                credential_type="secret",
                item_id="item_cred_3",
            )
        )
        await session.commit()
    vault = FakeVault()
    monkeypatch.setattr(
        object.__getattribute__(app, "_inst"),
        "CREDENTIAL_VAULT_SERVICES",
        dict.fromkeys(CredentialVaultType, vault),
        raising=False,
    )
    monkeypatch.setattr(app, "CACHE", None)
    monkeypatch.setattr("skyvern.forge.sdk.copilot.ask_user.QUESTION_POLL_SECONDS", 0.01)
    repo = run_group_env.database.workflow_params
    polled = asyncio.Event()
    poll = repo.poll_copilot_question

    async def poll_and_signal(organization_id: str, chat_id: str, interaction_id: str) -> QuestionInteraction:
        try:
            return await poll(organization_id, chat_id, interaction_id)
        finally:
            polled.set()

    monkeypatch.setattr(repo, "poll_copilot_question", poll_and_signal)
    row = await repo.create_workflow_copilot_chat(organization_id=RUN_GROUP_ORG, workflow_permanent_id=RUN_GROUP_WPID)
    await repo.start_copilot_turn(
        organization_id=RUN_GROUP_ORG,
        workflow_copilot_chat_id=row.workflow_copilot_chat_id,
        pending_turn=CopilotPendingTurn(turn_id="turn", started_at=datetime.now(UTC), cancel_token="stop"),
        user_message="Delete my saved credentials",
    )
    api = FastAPI()
    organization = run_group_env.organization
    api.dependency_overrides[routes.org_auth_service.get_current_org] = lambda: organization
    api.dependency_overrides[routes.org_auth_service.get_current_org_for_credential_routes] = lambda: organization
    api.dependency_overrides[routes.org_auth_service.get_current_user_id_or_none] = lambda: "user_1"
    api.add_api_route("/reply", routes.workflow_copilot_question_response, methods=["POST"])
    api.add_api_route("/confirm", routes.workflow_copilot_credential_deletion, methods=["POST"])
    api.add_api_route("/history", routes.workflow_copilot_chat_history, methods=["GET"])
    frames: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    ctx = CopilotContext(
        organization_id=RUN_GROUP_ORG,
        workflow_id="wf_1",
        workflow_permanent_id=RUN_GROUP_WPID,
        workflow_yaml="",
        browser_session_id=None,
        stream=None,
        api_key="",
        turn_id="turn",
        workflow_copilot_chat_id=row.workflow_copilot_chat_id,
        copilot_cancel_token="stop",
    )
    ctx.stream = SimpleNamespace(send=frames.put)
    async with AsyncClient(transport=ASGITransport(app=api), base_url="http://fixture") as client:
        yield Chat(run_group_env, ctx, client, frames, vault, polled, monkeypatch)
    while leaked := set(routes._DETACHED_CREDENTIAL_DELETE_TASKS):
        _, pending = await asyncio.wait(leaked, timeout=5)
        assert not pending, pending


def _start(chat: Chat, credential_ids: list[str]) -> asyncio.Task[dict[str, Any]]:
    return asyncio.create_task(delete_saved_credentials(chat.ctx, tool_call_id="call-1", credential_ids=credential_ids))


async def _cancel_tool(chat: Chat, task: asyncio.Task[dict[str, Any]]) -> None:
    # A cancel that lands mid-commit leaves the aiosqlite connection pooled inside an open transaction, which locks
    # the file for every later write; after the next poll returns, the long sleep keeps the tool out of the database.
    chat.monkeypatch.setattr("skyvern.forge.sdk.copilot.ask_user.QUESTION_POLL_SECONDS", 60)
    chat.polled.clear()
    await asyncio.wait_for(chat.polled.wait(), 5)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def _card(chat: Chat) -> dict[str, Any]:
    while (frame := await asyncio.wait_for(chat.frames.get(), 5))["type"] != "question_required":
        pass
    return frame["interactions"][0]


async def _confirm(chat: Chat, card: dict[str, Any], credential_ids: list[str]) -> Response:
    return await chat.client.post(
        "/confirm",
        json={
            "workflow_copilot_chat_id": chat.chat_id,
            "interaction_id": card["interaction_id"],
            "credential_ids": credential_ids,
        },
    )


async def _surviving(chat: Chat) -> set[str]:
    ids = ["cred_1", "cred_2", "cred_3"]
    org = await chat.env.database.credentials.get_credentials_by_ids(ids, organization_id=RUN_GROUP_ORG)
    other = await chat.env.database.credentials.get_credentials_by_ids(["cred_foreign"], organization_id="o_other")
    return {credential.credential_id for credential in [*org, *other]}


async def _history_interaction(chat: Chat) -> dict[str, Any]:
    history = await chat.client.get("/history", params={"workflow_copilot_chat_id": chat.chat_id})
    [interaction] = history.json()["question_interactions"]
    return interaction


@pytest.mark.asyncio
async def test_confirm_deletes_only_the_checked_entries_and_reports_each_outcome(chat: Chat) -> None:
    chat.vault.failing = {"cred_3"}
    task = _start(chat, ["cred_1", "cred_2", "cred_3"])
    card = await _card(chat)
    review = card["credential_delete_review"]
    assert [(row["credential_id"], row["credential_type"]) for row in review["rows"]] == [
        ("cred_1", "password"),
        ("cred_2", "password"),
        ("cred_3", "secret"),
    ]
    assert review["total_credential_count"] == 3

    confirmed = await _confirm(chat, card, ["cred_1", "cred_3"])
    result = await asyncio.wait_for(task, 5)

    assert confirmed.status_code == 200
    expected = [("cred_1", "deleted"), ("cred_3", "failed")]
    assert [(row["credential_id"], row["outcome"]) for row in result["outcomes"]] == expected
    assert result["confirmed"] is True
    assert await _surviving(chat) == {"cred_2", "cred_3", "cred_foreign"}
    restored = await _history_interaction(chat)
    stored = restored["credential_delete_review"]["outcomes"]
    assert [(row["credential_id"], row["outcome"]) for row in stored] == expected
    assert (await _confirm(chat, card, ["cred_2"])).status_code == 409
    assert chat.vault.deleted == ["cred_1"]
    for surface in (card, result, restored, confirmed.json()):
        assert SECRET not in to_json(surface).decode()


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", ["skip", "approve_via_reply", "stop", "client_gone"])
async def test_an_unconfirmed_card_deletes_nothing(chat: Chat, answer: str) -> None:
    task = _start(chat, ["cred_1", "cred_2"])
    card = await _card(chat)
    reply = {"workflow_copilot_chat_id": chat.chat_id, "interaction_id": card["interaction_id"]}
    if answer == "skip":
        assert (await chat.client.post("/reply", json={**reply, "skipped": True})).status_code == 200
        result = await asyncio.wait_for(task, 5)
        assert result["confirmed"] is False and result["outcomes"] == []
    elif answer == "approve_via_reply":
        decision = {"approved": True, "credential_ids": ["cred_1"]}
        assert (await chat.client.post("/reply", json={**reply, "account_group_decision": decision})).status_code == 409
        await _cancel_tool(chat, task)
    else:
        if answer == "stop":
            await chat.env.database.workflow_params.cancel_copilot_questions(RUN_GROUP_ORG, chat.chat_id, "stop")
        else:
            await _age_pending_turn(chat, question_client_seen_at=datetime.now(UTC) - timedelta(hours=1))
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        assert (await _confirm(chat, card, ["cred_1"])).status_code == 409
    await asyncio.gather(task, return_exceptions=True)
    assert chat.vault.deleted == []
    assert await _surviving(chat) == {"cred_1", "cred_2", "cred_3", "cred_foreign"}


@pytest.mark.asyncio
async def test_a_confirmation_the_card_does_not_list_is_refused_before_any_delete(chat: Chat) -> None:
    task = _start(chat, ["cred_1"])
    card = await _card(chat)

    assert (await _confirm(chat, card, ["cred_1", "cred_2"])).status_code == 409
    assert (await _confirm(chat, card, ["cred_1", "cred_1"])).status_code == 409

    question = await _stored_question(chat)
    await _cancel_tool(chat, task)
    assert question.status == "pending" and question.credential_delete_review is not None
    assert question.credential_delete_review.claimed_at is None
    assert not chat.vault.entered.is_set()
    assert await _surviving(chat) == {"cred_1", "cred_2", "cred_3", "cred_foreign"}


@pytest.mark.asyncio
async def test_a_delete_slower_than_its_timeout_reports_failed_and_still_finishes_its_cleanup(
    chat: Chat, monkeypatch: pytest.MonkeyPatch
) -> None:
    audit = AsyncMock()
    monkeypatch.setattr(credential_routes, "record_request_audit_event", audit)
    monkeypatch.setattr(routes, "CREDENTIAL_DELETE_TIMEOUT", timedelta(milliseconds=50))
    chat.vault.gate = asyncio.Event()
    task = _start(chat, ["cred_1", "cred_2"])
    card = await _card(chat)

    assert (await _confirm(chat, card, ["cred_1"])).status_code == 200
    result = await asyncio.wait_for(task, 5)
    assert [(row["credential_id"], row["outcome"]) for row in result["outcomes"]] == [("cred_1", "failed")]

    with capture_logs() as logs:
        chat.vault.gate.set()
        while steps := set(routes._DETACHED_CREDENTIAL_DELETE_TASKS):
            await asyncio.wait_for(asyncio.gather(*steps), 5)
    late = [entry for entry in logs if entry["event"] == "copilot_credential_deletion_late_outcome"]
    assert [(entry["credential_id"], entry["outcome"]) for entry in late] == [("cred_1", "deleted")]
    assert await _surviving(chat) == {"cred_2", "cred_3", "cred_foreign"}
    audit.assert_awaited_once_with(RUN_GROUP_ORG, "credential.delete", "credential", "cred_1")
    chat.vault.post_delete_credential_item.assert_awaited_once()
    stored = (await _history_interaction(chat))["credential_delete_review"]["outcomes"]
    assert stored == [{"credential_id": "cred_1", "outcome": "failed"}]


@pytest.mark.asyncio
async def test_a_credential_removed_elsewhere_after_its_delete_timed_out_logs_a_late_not_found(
    chat: Chat, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate = asyncio.Event()

    async def slow_lock_check(**_: object) -> bool:
        await gate.wait()
        return False

    monkeypatch.setattr(app.AGENT_FUNCTION, "should_lock_credential_write", slow_lock_check)
    monkeypatch.setattr(routes, "CREDENTIAL_DELETE_TIMEOUT", timedelta(milliseconds=50))
    task = _start(chat, ["cred_1"])
    card = await _card(chat)
    assert (await _confirm(chat, card, ["cred_1"])).status_code == 200
    result = await asyncio.wait_for(task, 5)
    assert [(row["credential_id"], row["outcome"]) for row in result["outcomes"]] == [("cred_1", "failed")]

    await app.DATABASE.credentials.delete_credential("cred_1", RUN_GROUP_ORG)
    with capture_logs() as logs:
        gate.set()
        while steps := set(routes._DETACHED_CREDENTIAL_DELETE_TASKS):
            await asyncio.wait_for(asyncio.gather(*steps, return_exceptions=True), 5)
    late = [entry for entry in logs if entry["event"] == "copilot_credential_deletion_late_outcome"]
    assert [(entry["credential_id"], entry["outcome"]) for entry in late] == [("cred_1", "not_found")]
    assert chat.vault.deleted == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "credential_ids",
    [[], ["cred_1", "cred_foreign"], ["cred_1", "cred_missing"], ["cred_1", "cred_1"]],
    ids=["empty", "other_org", "unknown", "duplicate"],
)
async def test_a_refused_selection_shows_no_card(chat: Chat, credential_ids: list[str]) -> None:
    result = await asyncio.wait_for(_start(chat, credential_ids), 5)

    assert result["ok"] is False and result["deleted"] is False
    assert chat.frames.empty()


@pytest.mark.asyncio
async def test_more_ids_than_one_card_holds_names_the_next_card(chat: Chat) -> None:
    result = await asyncio.wait_for(_start(chat, [f"cred_{i}" for i in range(MAX_CREDENTIALS_PER_CARD + 1)]), 5)

    assert result["ok"] is False and "another card" in result["error"]
    assert chat.frames.empty()


async def _age_pending_turn(chat: Chat, **timestamps: datetime) -> None:
    async with chat.env.database.Session() as session:
        stored = await session.get(WorkflowCopilotChatModel, chat.chat_id)
        assert stored is not None
        turn = {**stored.pending_turns["turn"], **{key: value.isoformat() for key, value in timestamps.items()}}
        await session.execute(
            update(WorkflowCopilotChatModel)
            .where(WorkflowCopilotChatModel.workflow_copilot_chat_id == chat.chat_id)
            .values(pending_turns={**stored.pending_turns, "turn": turn})
        )
        await session.commit()


async def _end_turn(chat: Chat) -> None:
    repo = chat.env.database.workflow_params
    stored = await repo.get_workflow_copilot_chat_by_id(RUN_GROUP_ORG, chat.chat_id)
    assert stored is not None
    await routes._persist_turn_messages(
        chat=stored,
        turn_id="turn",
        user_message="Delete my saved credentials",
        audio_artifact_id=None,
        user_row_already_persisted=True,
        assistant_content="Stopped.",
        global_llm_context=None,
        turn_outcome=None,
        narrative_payload=None,
        sender=Sender.USER,
    )
    ended = await repo.get_workflow_copilot_chat_by_id(RUN_GROUP_ORG, chat.chat_id)
    assert ended is not None and ended.pending_turns == {}


async def _narrative_messages(chat: Chat, session: AsyncSession) -> list[WorkflowCopilotChatMessageModel]:
    return list(
        await session.scalars(
            select(WorkflowCopilotChatMessageModel)
            .where(WorkflowCopilotChatMessageModel.workflow_copilot_chat_id == chat.chat_id)
            .where(WorkflowCopilotChatMessageModel.narrative_payload.is_not(None))
        )
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("event", ["stop", "interrupt", "stale_heartbeat", "client_gone", "turn_ended"])
async def test_a_confirmed_deletion_records_its_outcomes_whatever_ends_the_wait(chat: Chat, event: str) -> None:
    repo = chat.env.database.workflow_params
    chat.vault.gate = asyncio.Event()
    task = _start(chat, ["cred_1", "cred_2"])
    card = await _card(chat)
    confirm = asyncio.create_task(_confirm(chat, card, ["cred_1"]))
    await asyncio.wait_for(chat.vault.entered.wait(), 5)

    if event == "stop":
        await repo.cancel_copilot_questions(RUN_GROUP_ORG, chat.chat_id, "stop")
    elif event == "interrupt":
        await repo.interrupt_copilot_question(RUN_GROUP_ORG, chat.chat_id, card["interaction_id"])
    elif event == "stale_heartbeat":
        await _cancel_tool(chat, task)
        await _age_pending_turn(chat, question_heartbeat_at=datetime.now(UTC) - timedelta(hours=1))
        skip = {"workflow_copilot_chat_id": chat.chat_id, "interaction_id": card["interaction_id"], "skipped": True}
        assert (await chat.client.post("/reply", json=skip)).status_code == 409
    elif event == "client_gone":
        await _age_pending_turn(chat, question_client_seen_at=datetime.now(UTC) - timedelta(hours=1))
        await repo.poll_copilot_question(RUN_GROUP_ORG, chat.chat_id, card["interaction_id"])
    else:
        await _cancel_tool(chat, task)
        await _end_turn(chat)
    assert (await _history_interaction(chat))["status"] == "pending"

    chat.vault.gate.set()
    assert (await asyncio.wait_for(confirm, 5)).status_code == 200

    restored = await _history_interaction(chat)
    assert restored["status"] == "resolved"
    assert restored["credential_delete_review"]["outcomes"] == [{"credential_id": "cred_1", "outcome": "deleted"}]
    assert await _surviving(chat) == {"cred_2", "cred_3", "cred_foreign"}
    if not task.done():
        result = await asyncio.wait_for(task, 5)
        assert [(row["credential_id"], row["outcome"]) for row in result["outcomes"]] == [("cred_1", "deleted")]


async def _age_claim(chat: Chat, age: timedelta) -> None:
    claimed_at = (datetime.now(UTC) - age).isoformat()
    async with chat.env.database.Session() as session:
        stored = await session.get(WorkflowCopilotChatModel, chat.chat_id)
        assert stored is not None
        if stored.pending_turns:
            turn = copy.deepcopy(stored.pending_turns["turn"])
            turn["question_interactions"][0]["credential_delete_review"]["claimed_at"] = claimed_at
            await session.execute(
                update(WorkflowCopilotChatModel)
                .where(WorkflowCopilotChatModel.workflow_copilot_chat_id == chat.chat_id)
                .values(pending_turns={**stored.pending_turns, "turn": turn})
            )
        for message in await _narrative_messages(chat, session):
            payload = copy.deepcopy(message.narrative_payload)
            for raw in payload.get("questionInteractions", []):
                raw["credential_delete_review"]["claimed_at"] = claimed_at
            message.narrative_payload = payload
        await session.commit()


async def _stored_question(chat: Chat) -> QuestionInteraction:
    async with chat.env.database.Session() as session:
        stored = await session.get(WorkflowCopilotChatModel, chat.chat_id)
        assert stored is not None
        if stored.pending_turns:
            [question] = CopilotPendingTurn.model_validate(stored.pending_turns["turn"]).question_interactions
            return question
        [raw] = [
            raw
            for message in await _narrative_messages(chat, session)
            for raw in message.narrative_payload.get("questionInteractions", [])
        ]
        return QuestionInteraction.model_validate(raw)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "seen_by", ["poll", "interrupt", "stop", "history_while_turn_pending", "history_after_turn_ended"]
)
async def test_a_claim_whose_outcomes_never_arrive_fails_every_entry_after_its_lease(
    chat: Chat, seen_by: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = chat.env.database.workflow_params
    task = _start(chat, ["cred_1", "cred_2"])
    card = await _card(chat)
    await repo.claim_copilot_credential_deletion(
        RUN_GROUP_ORG, chat.chat_id, card["interaction_id"], ["cred_1", "cred_2"], "user_1"
    )
    if seen_by != "poll":
        await _cancel_tool(chat, task)
    if seen_by == "history_after_turn_ended":
        await _end_turn(chat)
    await _age_claim(chat, CREDENTIAL_DELETE_CLAIM_LEASE - timedelta(seconds=5))
    history = (await chat.client.get("/history", params={"workflow_copilot_chat_id": chat.chat_id})).json()
    assert history["question_interactions"][0]["status"] == "pending"
    assert history["pending_question_cancel_token"] == (None if seen_by == "history_after_turn_ended" else "stop")

    await _age_claim(chat, CREDENTIAL_DELETE_CLAIM_LEASE + timedelta(seconds=1))
    if seen_by == "interrupt":
        await repo.interrupt_copilot_question(RUN_GROUP_ORG, chat.chat_id, card["interaction_id"], stale_only=True)
    elif seen_by == "stop":
        await repo.cancel_copilot_questions(RUN_GROUP_ORG, chat.chat_id, "stop")
    elif seen_by == "history_while_turn_pending":
        refresh = repo.refresh_copilot_question_client

        async def lease_lapses_after_refresh(organization_id: str, chat_id: str) -> dict[str, CopilotPendingTurn]:
            await _age_claim(chat, CREDENTIAL_DELETE_CLAIM_LEASE - timedelta(seconds=5))
            entries = await refresh(organization_id, chat_id)
            await _age_claim(chat, CREDENTIAL_DELETE_CLAIM_LEASE + timedelta(seconds=1))
            entries["turn"].question_interactions = [await _stored_question(chat)]
            return entries

        monkeypatch.setattr(repo, "refresh_copilot_question_client", lease_lapses_after_refresh)
        history = (await chat.client.get("/history", params={"workflow_copilot_chat_id": chat.chat_id})).json()
        [restored] = history["question_interactions"]
        assert restored["status"] == "resolved"
        assert [(row["credential_id"], row["outcome"]) for row in restored["credential_delete_review"]["outcomes"]] == [
            ("cred_1", "failed"),
            ("cred_2", "failed"),
        ]
        assert history["pending_question_cancel_token"] is None
    elif seen_by == "history_after_turn_ended":
        restored = (await _history_interaction(chat))["credential_delete_review"]["outcomes"]
        assert [(row["credential_id"], row["outcome"]) for row in restored] == [
            ("cred_1", "failed"),
            ("cred_2", "failed"),
        ]
    else:
        result = await asyncio.wait_for(task, 5)
        assert [(row["credential_id"], row["outcome"]) for row in result["outcomes"]] == [
            ("cred_1", "failed"),
            ("cred_2", "failed"),
        ]

    question = await _stored_question(chat)
    assert question.status == "resolved" and question.credential_delete_review is not None
    outcomes = question.credential_delete_review.outcomes or []
    assert [(item.credential_id, item.outcome) for item in outcomes] == [("cred_1", "failed"), ("cred_2", "failed")]
    assert chat.vault.deleted == []
    assert await _surviving(chat) == {"cred_1", "cred_2", "cred_3", "cred_foreign"}


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", ["confirm", "skip"])
async def test_an_answer_and_the_lease_expiry_it_finds_commit_together(chat: Chat, answer: str) -> None:
    repo = chat.env.database.workflow_params
    task = _start(chat, ["cred_1"])
    card = await _card(chat)
    await repo.claim_copilot_credential_deletion(RUN_GROUP_ORG, chat.chat_id, card["interaction_id"], ["cred_1"], "u")
    await _cancel_tool(chat, task)
    second = QuestionInteraction.model_validate({**card, "interaction_id": "q_2", "tool_call_id": "call-2"})
    await repo.start_copilot_question(RUN_GROUP_ORG, chat.chat_id, second)
    await _age_claim(chat, CREDENTIAL_DELETE_CLAIM_LEASE + timedelta(seconds=1))
    commits: list[Session] = []
    record_commit = commits.append
    event.listen(Session, "after_commit", record_commit)
    try:
        if answer == "confirm":
            await repo.claim_copilot_credential_deletion(RUN_GROUP_ORG, chat.chat_id, "q_2", ["cred_1"], "u")
        else:
            await repo.resolve_copilot_question(RUN_GROUP_ORG, chat.chat_id, "q_2", QuestionResponse(skipped=True))
    finally:
        event.remove(Session, "after_commit", record_commit)

    assert len(commits) == 1
    async with chat.env.database.Session() as session:
        stored = await session.get(WorkflowCopilotChatModel, chat.chat_id)
        assert stored is not None
        expired, answered = CopilotPendingTurn.model_validate(stored.pending_turns["turn"]).question_interactions
    assert expired.status == "resolved" and expired.credential_delete_review is not None
    assert [(item.credential_id, item.outcome) for item in expired.credential_delete_review.outcomes or []] == [
        ("cred_1", "failed")
    ]
    if answer == "confirm":
        assert answered.status == "pending" and answered.claimed_credential_ids == ["cred_1"]
    else:
        assert answered.status == "resolved" and answered.response is not None and answered.response.skipped


@pytest.mark.asyncio
async def test_read_time_recovery_leaves_a_turn_whose_claim_is_live(chat: Chat) -> None:
    repo = chat.env.database.workflow_params
    task = _start(chat, ["cred_1"])
    card = await _card(chat)
    await repo.claim_copilot_credential_deletion(RUN_GROUP_ORG, chat.chat_id, card["interaction_id"], ["cred_1"], "u")
    await _cancel_tool(chat, task)
    long_ago = datetime.now(UTC) - timedelta(seconds=2 * routes.RECONCILE_ABANDON_AFTER_SECONDS)
    await _age_pending_turn(chat, started_at=long_ago, question_heartbeat_at=long_ago)
    stored = await repo.get_workflow_copilot_chat_by_id(RUN_GROUP_ORG, chat.chat_id)
    assert stored is not None

    await routes._reconcile_interrupted_copilot_turns(stored, RUN_GROUP_ORG)

    async with chat.env.database.Session() as session:
        after = await session.get(WorkflowCopilotChatModel, chat.chat_id)
        assert after is not None and after.pending_turns["turn"].get("recovering_at") is None
    assert not await repo.claim_pending_copilot_turn(
        organization_id=RUN_GROUP_ORG,
        workflow_copilot_chat_id=chat.chat_id,
        turn_id="turn",
        claim_before=datetime.now(UTC) - timedelta(seconds=routes.RECONCILE_ABANDON_AFTER_SECONDS),
    )
    question = await _stored_question(chat)
    assert question.status == "pending" and question.claimed_credential_ids == ["cred_1"]


@pytest.mark.asyncio
async def test_a_record_that_raises_after_committing_keeps_the_recorded_outcomes(
    chat: Chat, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = chat.env.database.workflow_params
    record = repo.record_copilot_credential_deletion
    raised: list[bool] = []

    async def record_then_lose_the_acknowledgement(
        organization_id: str, chat_id: str, interaction_id: str, outcomes: list[CredentialDeleteOutcome]
    ) -> QuestionInteraction:
        recorded = await record(organization_id, chat_id, interaction_id, outcomes)
        if not raised:
            raised.append(True)
            raise ConnectionError("connection lost after commit")
        return recorded

    monkeypatch.setattr(repo, "record_copilot_credential_deletion", record_then_lose_the_acknowledgement)
    monkeypatch.setattr(routes, "CREDENTIAL_DELETE_RECORD_RETRY_DELAY", timedelta(0))
    task = _start(chat, ["cred_1"])
    card = await _card(chat)

    assert (await _confirm(chat, card, ["cred_1"])).status_code == 200
    result = await asyncio.wait_for(task, 5)

    assert raised == [True]
    assert [(row["credential_id"], row["outcome"]) for row in result["outcomes"]] == [("cred_1", "deleted")]
    assert await _surviving(chat) == {"cred_2", "cred_3", "cred_foreign"}


@pytest.mark.asyncio
@pytest.mark.parametrize("withheld_by", [None, "client", "flag"])
async def test_the_card_needs_both_the_client_capability_and_the_flag(
    chat: Chat, monkeypatch: pytest.MonkeyPatch, withheld_by: str | None
) -> None:
    if withheld_by == "flag":
        monkeypatch.setattr(settings, "COPILOT_CREDENTIAL_DELETE_ENABLED", False)
    surface = resolve_copilot_tool_surface(
        mode=None,
        native_tools=copilot_native_tools(
            supports_question_tool=True,
            supports_credential_delete_card=withheld_by != "client",
            browser_code_available=False,
            run_tools_available=False,
        ),
        alias_map={},
        overlays={},
        browser_tools_available=False,
    )

    assert ("delete_saved_credentials" in surface.ordered_native_names) is (withheld_by is None)
    if withheld_by == "flag":
        response = await chat.client.post(
            "/confirm",
            json={"workflow_copilot_chat_id": chat.chat_id, "interaction_id": "any", "credential_ids": ["cred_1"]},
        )
        assert response.status_code == 403
        assert chat.vault.deleted == []
