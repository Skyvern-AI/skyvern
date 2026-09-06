import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from skyvern.forge.sdk.copilot.ask_user import (
    AskUserArguments,
    QuestionAnswer,
    QuestionResponse,
    ask_user,
    create_question_interaction,
    resolve_question_response,
)
from skyvern.forge.sdk.copilot.context import CopilotContext
from skyvern.forge.sdk.copilot.human_input_wait import HumanInputWait, pause_human_input
from skyvern.forge.sdk.copilot.output_utils import summarize_tool_result
from skyvern.forge.sdk.db.base_alchemy_db import BaseAlchemyDB
from skyvern.forge.sdk.db.repositories.workflow_parameters import WorkflowParametersRepository
from skyvern.forge.sdk.schemas.workflow_copilot import CopilotPendingTurn


def test_questions_preserve_content_and_correlate_answers_by_id():
    parts = [
        {"prompt": "What should I send you?", "choices": ["Send me the receipt", "No email"]},
        {"prompt": "Which actions should this workflow never take?", "choices": ["Send an email", "Delete a file"]},
        {"prompt": "Pick", "choices": [f"Option {i}" for i in range(9)]},
        {"prompt": "x" * 201, "choices": ["y" * 201]},
        {"prompt": "Which format?", "choices": ["PDF"]},
        {"prompt": "Which format?", "choices": ["CSV"]},
        *[{"prompt": f"Question {i}", "choices": []} for i in range(9)],
    ]
    interaction = create_question_interaction(
        AskUserArguments.model_validate({"parts": parts}), turn_id="turn", tool_call_id="call"
    )
    persisted = type(interaction).model_validate_json(interaction.model_dump_json())
    assert [part.prompt for part in persisted.parts] == [part["prompt"] for part in parts]
    assert [[choice.text for choice in part.choices] for part in persisted.parts] == [part["choices"] for part in parts]
    assert len({part.part_id for part in persisted.parts}) == len(parts)
    second_twin = persisted.parts[5]
    resolved = resolve_question_response(
        persisted,
        QuestionResponse(
            answers=[QuestionAnswer(part_id=second_twin.part_id, choice_id=second_twin.choices[0].choice_id)]
        ),
    )
    result = resolved.tool_result()
    assert result["tool_call_id"] == "call"
    assert result["parts"][4]["status"] == "unanswered"
    assert result["parts"][5]["status"] == "answered"
    assert result["parts"][5]["choice"]["text"] == "CSV"


@pytest.mark.parametrize("skipped,text", [(True, None), (False, "why do you need this?")])
def test_skip_and_composer_text_are_observations(skipped, text):
    pending = create_question_interaction(
        AskUserArguments.model_validate({"parts": [{"prompt": "Which day?"}]}),
        turn_id="turn",
        tool_call_id="call",
    )
    resolved = resolve_question_response(pending, QuestionResponse(skipped=skipped, text=text))
    assert resolved.status == "resolved"
    assert resolved.tool_result()["skipped"] is skipped
    assert resolved.tool_result()["text"] == text
    assert resolved.tool_result()["parts"][0]["status"] == "unanswered"
    assert pending.status == "pending"


def test_response_cannot_attribute_a_foreign_choice_or_part():
    pending = create_question_interaction(
        AskUserArguments.model_validate(
            {
                "parts": [
                    {"prompt": "Which day?", "choices": ["Monday"]},
                    {"prompt": "Which day?", "choices": ["Friday"]},
                ]
            }
        ),
        turn_id="turn",
        tool_call_id="call",
    )
    with pytest.raises(ValueError, match="choice"):
        resolve_question_response(
            pending,
            QuestionResponse(
                answers=[
                    QuestionAnswer(
                        part_id=pending.parts[0].part_id,
                        choice_id=pending.parts[1].choices[0].choice_id,
                    )
                ]
            ),
        )
    with pytest.raises(ValueError, match="part"):
        resolve_question_response(
            pending, QuestionResponse(answers=[QuestionAnswer(part_id="foreign", text="Tuesday")])
        )
    answer = QuestionAnswer(part_id=pending.parts[0].part_id, text="Tuesday")
    with pytest.raises(ValueError, match="duplicate"):
        resolve_question_response(pending, QuestionResponse(answers=[answer, answer]))


def test_question_secret_screen_preserves_safe_content():
    secret = "sk-proj-" + "aB3dE5fG7hJ9kL2mN4pQ6rS8tU0vW1xY" * 3
    interaction = create_question_interaction(
        AskUserArguments.model_validate(
            {
                "parts": [
                    {
                        "prompt": f"Use token {secret}?",
                        "choices": ["Send me the receipt", secret],
                    }
                ]
            }
        ),
        turn_id="turn",
        tool_call_id="call",
    )
    assert secret not in json.dumps(interaction.model_dump(mode="json"))
    assert interaction.parts[0].choices[0].text == "Send me the receipt"


async def setup_question_chat(sqlite_engine, monkeypatch):
    from fastapi import FastAPI, Request
    from httpx import ASGITransport, AsyncClient

    from skyvern.forge import app
    from skyvern.forge.sdk.routes import workflow_copilot as routes
    from skyvern.forge.sdk.schemas.organizations import Organization

    db = BaseAlchemyDB(sqlite_engine)
    repo = WorkflowParametersRepository(db.Session)
    monkeypatch.setattr(app.DATABASE, "workflow_params", repo)
    monkeypatch.setattr(app, "CACHE", None)
    monkeypatch.setattr(
        routes,
        "resolve_raw_secret_safety_handler",
        AsyncMock(return_value=AsyncMock(return_value={"version": "1", "state": "clean", "citations": []})),
    )
    monkeypatch.setattr("skyvern.forge.sdk.copilot.ask_user.QUESTION_POLL_SECONDS", 0.01)
    chat = await repo.create_workflow_copilot_chat(organization_id="org", workflow_permanent_id="workflow")
    await repo.start_copilot_turn(
        organization_id="org",
        workflow_copilot_chat_id=chat.workflow_copilot_chat_id,
        pending_turn=CopilotPendingTurn(turn_id="turn", started_at=datetime.now(UTC), cancel_token="stop"),
        user_message="Ask about the invoice format",
    )
    api = FastAPI()

    async def organization(request: Request):
        return Organization(
            organization_id=request.headers.get("test-org", "org"),
            organization_name="Fixture",
            created_at=datetime.now(UTC),
            modified_at=datetime.now(UTC),
        )

    api.dependency_overrides[routes.org_auth_service.get_current_org] = organization
    api.add_api_route("/reply", routes.workflow_copilot_question_response, methods=["POST"])
    api.add_api_route("/history", routes.workflow_copilot_chat_history, methods=["GET"])
    api.add_api_route("/cancel", routes.workflow_copilot_cancel, methods=["POST"], status_code=204)
    client = AsyncClient(transport=ASGITransport(app=api), base_url="http://fixture")
    frames = asyncio.Queue()
    ctx = CopilotContext(
        organization_id="org",
        workflow_id="workflow",
        workflow_permanent_id="workflow",
        workflow_yaml="",
        browser_session_id=None,
        stream=None,
        api_key="",
        turn_id="turn",
        workflow_copilot_chat_id=chat.workflow_copilot_chat_id,
        copilot_cancel_token="stop",
    )
    ctx.stream = SimpleNamespace(send=frames.put)
    return repo, client, ctx, frames


@pytest.mark.asyncio
@pytest.mark.parametrize("form", ["choice", "text", "both", "partial", "skip"])
async def test_actual_handler_reply_endpoint_and_reloaded_history(sqlite_engine: AsyncEngine, monkeypatch, form):
    repo, client, ctx, frames = await setup_question_chat(sqlite_engine, monkeypatch)
    args = AskUserArguments.model_validate(
        {
            "parts": [
                {"prompt": "Which format?", "choices": ["PDF"]},
                {"prompt": "Which format?", "choices": ["Email", "CSV"]},
                {"prompt": "What should I send you?", "choices": ["Send me the receipt", "No email"]},
                {
                    "prompt": "Which actions should this workflow never take?",
                    "choices": ["Send an email", "Delete a file"],
                },
                {"prompt": "Which days should this workflow never run?", "choices": ["Saturday", "Sunday"]},
                {
                    "prompt": "Should ask_user explain the workflow run wr_fixture and session bs_fixture?",
                    "choices": ["Explain execute_workflow", "Show the recovery_hint"],
                },
                {"prompt": "x" * 201, "choices": ["y" * 201]},
                {"prompt": "Which option?", "choices": [f"Option {i}" for i in range(9)]},
                {"prompt": "Ninth question", "choices": []},
            ]
        }
    )
    async with client:
        task = asyncio.create_task(ask_user(ctx, args, "first-call"))
        try:
            frame = await asyncio.wait_for(frames.get(), 5)
            question = frame["interactions"][0]
            assert frame["type"] == "question_required"
            assert frame["cancel_token"] == "stop"
            assert [part["prompt"] for part in question["parts"]] == [part.prompt for part in args.parts]
            assert [[choice["text"] for choice in part["choices"]] for part in question["parts"]] == [
                part.choices for part in args.parts
            ]
            # Reload the saved record while the same handler is still waiting.
            history = await client.get("/history", params={"workflow_copilot_chat_id": ctx.workflow_copilot_chat_id})
            assert history.status_code == 200
            assert history.json()["question_interactions"] == [question]
            second = question["parts"][1]
            submitted = {}
            if form in {"choice", "both", "partial"}:
                submitted["answers"] = [{"part_id": second["part_id"], "choice_id": second["choices"][0]["choice_id"]}]
            if form in {"text", "both"}:
                submitted["text"] = "Do not email it.\nLet me download it instead."
            if form == "skip":
                submitted["skipped"] = True
            body = {
                "workflow_copilot_chat_id": ctx.workflow_copilot_chat_id,
                "interaction_id": question["interaction_id"],
                **submitted,
            }
            foreign = await client.post("/reply", json=body, headers={"test-org": "foreign"})
            assert foreign.status_code == 404
            other = await repo.create_workflow_copilot_chat(organization_id="org", workflow_permanent_id="workflow")
            wrong_chat = await client.post(
                "/reply", json={**body, "workflow_copilot_chat_id": other.workflow_copilot_chat_id}
            )
            assert wrong_chat.status_code == 404
            accepted = await client.post("/reply", json=body)
            assert accepted.status_code == 200, accepted.text
            duplicate = await client.post("/reply", json={**body, "text": "Changed retry"} if form != "skip" else body)
            assert duplicate.json() == accepted.json()
            result = await asyncio.wait_for(task, 5)
            assert result["ok"] is True
            assert summarize_tool_result("ask_user", result, for_display=True) == "OK"
            assert result["tool_call_id"] == "first-call"
            assert result["parts"][0]["status"] == "unanswered"
            if "answers" in submitted:
                assert result["parts"][1]["choice"]["text"] == "Email"
            assert result["text"] == submitted.get("text")
            assert result["skipped"] == (form == "skip")
            resolved_frame = await frames.get()
            assert resolved_frame["type"] == "question_resolved"
            assert frames.empty()
            history = await client.get("/history", params={"workflow_copilot_chat_id": ctx.workflow_copilot_chat_id})
            assert history.json()["question_interactions"] == [accepted.json()]
            # The same handler can ask again; no application-level interview veto.
            task = asyncio.create_task(ask_user(ctx, args, "second-call"))
            next_frame = await asyncio.wait_for(frames.get(), 5)
            followup = next_frame["interactions"][0]
            reply = await client.post(
                "/reply",
                json={
                    "workflow_copilot_chat_id": ctx.workflow_copilot_chat_id,
                    "interaction_id": followup["interaction_id"],
                    "skipped": True,
                },
            )
            assert reply.status_code == 200
            assert (await asyncio.wait_for(task, 5))["tool_call_id"] == "second-call"
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["cancel", "interrupted"])
async def test_stopped_owner_disables_saved_question_and_rejects_late_reply(sqlite_engine, monkeypatch, ending):
    repo, client, ctx, frames = await setup_question_chat(sqlite_engine, monkeypatch)
    polled = asyncio.Event()
    if ending == "interrupted":
        poll = repo.poll_copilot_question

        async def poll_then_wait(*args):
            result = await poll(*args)
            polled.set()
            # Cancel the actual owner after its real database session has closed,
            # rather than racing cancellation against SQLite's driver thread.
            await asyncio.Event().wait()
            return result

        monkeypatch.setattr(repo, "poll_copilot_question", poll_then_wait)
    async with client:
        task = asyncio.create_task(
            ask_user(ctx, AskUserArguments.model_validate({"parts": [{"prompt": "Format?"}]}), "call")
        )
        try:
            frame = await asyncio.wait_for(frames.get(), 5)
            question = frame["interactions"][0]
            if ending == "cancel":
                response = await client.post(
                    "/cancel", json={"workflow_copilot_chat_id": ctx.workflow_copilot_chat_id, "cancel_token": "stop"}
                )
                assert response.status_code == 204
            else:
                await asyncio.wait_for(polled.wait(), 5)
                task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 5)
            history = await client.get("/history", params={"workflow_copilot_chat_id": ctx.workflow_copilot_chat_id})
            assert history.json()["question_interactions"][0]["status"] == (
                "cancelled" if ending == "cancel" else "interrupted"
            )
            late = await client.post(
                "/reply",
                json={
                    "workflow_copilot_chat_id": ctx.workflow_copilot_chat_id,
                    "interaction_id": question["interaction_id"],
                    "text": "CSV",
                },
            )
            assert late.status_code == 409
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "first,second", [("question", "question"), ("question", "credential"), ("credential", "question")]
)
async def test_overlapping_human_waits_pause_deadline_once(monkeypatch, first, second):
    now = [10.0]
    monkeypatch.setattr("skyvern.forge.sdk.copilot.human_input_wait.time.monotonic", lambda: now[0])

    class Deadline:
        value = 100.0

        def when(self):
            return self.value

        def reschedule(self, value):
            self.value = value

        def expired(self):
            return False

    ctx = SimpleNamespace(
        human_input_wait=HumanInputWait(),
        model_stream_deadline=Deadline(),
        copilot_question_pause_seconds=0,
        copilot_credential_pause_seconds=0,
    )
    with pause_human_input(ctx, first):
        assert ctx.model_stream_deadline.when() is None
        now[0] = 20
        with pause_human_input(ctx, second):
            now[0] = 80
        assert ctx.model_stream_deadline.when() is None
        now[0] = 1000
    assert ctx.copilot_question_pause_seconds + ctx.copilot_credential_pause_seconds == 990
    assert ctx.model_stream_deadline.when() == 1090


@pytest.mark.asyncio
async def test_registered_tool_returns_both_observations_in_next_model_input_and_saved_history(
    sqlite_engine, monkeypatch
):
    from agents import Agent, Model, ModelResponse, RunConfig, Runner, Usage
    from openai.types.responses import ResponseFunctionToolCall, ResponseOutputMessage, ResponseOutputText

    from skyvern.forge.sdk.copilot.tools import ask_user_tool
    from skyvern.forge.sdk.routes.workflow_copilot import _persist_turn_messages
    from skyvern.forge.sdk.schemas.workflow_copilot import WorkflowCopilotChatSender

    repo, client, ctx, frames = await setup_question_chat(sqlite_engine, monkeypatch)
    inputs = []
    arguments = {"parts": [{"prompt": "Delivery?", "choices": ["Email", "Download"]}]}

    class FixtureModel(Model):
        async def get_response(self, *args, **kwargs):
            inputs.append(kwargs["input"] if "input" in kwargs else args[1])
            if len(inputs) == 1:
                output = [
                    ResponseFunctionToolCall(
                        type="function_call",
                        call_id="fixture-call",
                        name="ask_user",
                        arguments=json.dumps(arguments),
                        id="fc_fixture",
                    )
                ]
            else:
                output = [
                    ResponseOutputMessage(
                        type="message",
                        id="message",
                        role="assistant",
                        status="completed",
                        content=[ResponseOutputText(type="output_text", text="Response received", annotations=[])],
                    )
                ]
            return ModelResponse(output=output, usage=Usage(), response_id=f"response-{len(inputs)}")

        async def stream_response(self, *args, **kwargs):
            raise AssertionError("This fixture uses the SDK's ordinary runner")
            yield

    guidance = "Keep questions and choice labels concise. Aim for 200 characters or fewer per question or choice, and offer no more than eight choices."
    assert guidance in ask_user_tool.description
    async with client:
        execution = asyncio.create_task(
            Runner.run(
                Agent(name="Fixture", model=FixtureModel(), tools=[ask_user_tool]),
                "Ask for delivery",
                context=ctx,
                run_config=RunConfig(tracing_disabled=True),
            )
        )
        try:
            frame = await asyncio.wait_for(frames.get(), 5)
            question = frame["interactions"][0]
            part = question["parts"][0]
            body = {
                "workflow_copilot_chat_id": ctx.workflow_copilot_chat_id,
                "interaction_id": question["interaction_id"],
                "answers": [{"part_id": part["part_id"], "choice_id": part["choices"][0]["choice_id"]}],
                "text": "Do not email it.\nLet me download it instead.",
            }
            accepted = await client.post("/reply", json=body)
            assert accepted.status_code == 200, accepted.text
            await asyncio.wait_for(execution, 5)
            outputs = [item for item in inputs[1] if item.get("type") == "function_call_output"]
            assert len(outputs) == 1
            assert outputs[0]["call_id"] == "fixture-call"
            result = json.loads(outputs[0]["output"])
            assert result["parts"][0]["choice"]["text"] == "Email"
            assert result["text"] == body["text"]
            chat = await repo.get_workflow_copilot_chat_by_id("org", ctx.workflow_copilot_chat_id)
            await _persist_turn_messages(
                chat=chat,
                turn_id="turn",
                user_message="Ask for delivery",
                audio_artifact_id=None,
                user_row_already_persisted=True,
                sender=WorkflowCopilotChatSender.USER,
                assistant_content="Response received",
                global_llm_context=None,
                turn_outcome=None,
                narrative_payload=None,
            )
            saved = await client.get("/history", params={"workflow_copilot_chat_id": ctx.workflow_copilot_chat_id})
            assert saved.json()["question_interactions"] == [accepted.json()]
            chat = await repo.get_workflow_copilot_chat_by_id("org", ctx.workflow_copilot_chat_id)
            assert not chat.pending_turns
            duplicate = await client.post("/reply", json=body)
            assert duplicate.json() == accepted.json()
            assert len(inputs) == 2
        finally:
            if not execution.done():
                execution.cancel()
                await asyncio.gather(execution, return_exceptions=True)


@pytest.mark.asyncio
async def test_long_wait_reply_cannot_be_recovered_before_delivery_and_redaction_keeps_structure(
    sqlite_engine, monkeypatch
):
    from sqlalchemy import select

    from skyvern.forge.sdk.copilot.agent import _format_chat_history
    from skyvern.forge.sdk.db.models import WorkflowCopilotChatModel
    from skyvern.forge.sdk.routes.workflow_copilot import _persist_turn_messages
    from skyvern.forge.sdk.schemas.workflow_copilot import WorkflowCopilotChatHistoryResponse, WorkflowCopilotChatSender

    repo, client, ctx, frames = await setup_question_chat(sqlite_engine, monkeypatch)
    # Prevent handler polling after its initial pending read until the HTTP/history checks finish.
    monkeypatch.setattr("skyvern.forge.sdk.copilot.ask_user.QUESTION_POLL_SECONDS", 0.2)
    async with client:
        task = asyncio.create_task(
            ask_user(ctx, AskUserArguments.model_validate({"parts": [{"prompt": "Format?"}]}), "call")
        )
        try:
            question = (await asyncio.wait_for(frames.get(), 5))["interactions"][0]
            async with repo.Session() as session:
                model = (
                    await session.scalars(
                        select(WorkflowCopilotChatModel).where(
                            WorkflowCopilotChatModel.workflow_copilot_chat_id == ctx.workflow_copilot_chat_id
                        )
                    )
                ).one()
                turn = CopilotPendingTurn.model_validate(model.pending_turns["turn"])
                turn.started_at = datetime.now(UTC) - timedelta(days=90)
                model.pending_turns = {"turn": turn.model_dump(mode="json")}
                await session.commit()
            response = await client.post(
                "/reply",
                json={
                    "workflow_copilot_chat_id": ctx.workflow_copilot_chat_id,
                    "interaction_id": question["interaction_id"],
                    "text": "CSV; password: fixture-secret-value",
                },
            )
            assert response.status_code == 200, response.text
            assert "fixture-secret-value" not in response.text
            history = await client.get("/history", params={"workflow_copilot_chat_id": ctx.workflow_copilot_chat_id})
            assert history.json()["question_interactions"][0]["status"] == "resolved"
            assert not await repo.claim_pending_copilot_turn(
                "org", ctx.workflow_copilot_chat_id, "turn", datetime.now(UTC) - timedelta(minutes=5)
            )
            result = await asyncio.wait_for(task, 5)
            assert result["text"].startswith("CSV;")
            chat = await repo.get_workflow_copilot_chat_by_id("org", ctx.workflow_copilot_chat_id)
            await _persist_turn_messages(
                chat=chat,
                turn_id="turn",
                user_message="Format?",
                audio_artifact_id=None,
                user_row_already_persisted=True,
                sender=WorkflowCopilotChatSender.USER,
                assistant_content="Ready.",
                global_llm_context=None,
                turn_outcome=None,
                narrative_payload=None,
            )
            history = await client.get("/history", params={"workflow_copilot_chat_id": ctx.workflow_copilot_chat_id})
            loaded = WorkflowCopilotChatHistoryResponse.model_validate(history.json())
            model_history = _format_chat_history(loaded.chat_history)
            assert "CSV;" in model_history
            assert "fixture-secret-value" not in model_history
            assert "ask_user result:" in model_history
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_abrupt_owner_loss_rejects_reply_and_disables_saved_card(sqlite_engine, monkeypatch):
    from sqlalchemy import select

    from skyvern.forge.sdk.db.models import WorkflowCopilotChatModel

    repo, client, ctx, _ = await setup_question_chat(sqlite_engine, monkeypatch)
    # Persisted fixture represents a killed worker: no handler/finally can run.
    question = create_question_interaction(
        AskUserArguments.model_validate({"parts": [{"prompt": "Format?"}]}), turn_id="turn", tool_call_id="lost-call"
    )
    await repo.start_copilot_question("org", ctx.workflow_copilot_chat_id, question)
    async with repo.Session() as session:
        model = (
            await session.scalars(
                select(WorkflowCopilotChatModel).where(
                    WorkflowCopilotChatModel.workflow_copilot_chat_id == ctx.workflow_copilot_chat_id
                )
            )
        ).one()
        turn = CopilotPendingTurn.model_validate(model.pending_turns["turn"])
        turn.question_heartbeat_at = datetime.now(UTC) - timedelta(minutes=1)
        model.pending_turns = {"turn": turn.model_dump(mode="json")}
        await session.commit()
    async with client:
        history = await client.get("/history", params={"workflow_copilot_chat_id": ctx.workflow_copilot_chat_id})
        assert history.json()["question_interactions"][0]["status"] == "interrupted"
        assert history.json()["pending_question_cancel_token"] is None
        late = await client.post(
            "/reply",
            json={
                "workflow_copilot_chat_id": ctx.workflow_copilot_chat_id,
                "interaction_id": question.interaction_id,
                "text": "CSV",
            },
        )
        assert late.status_code == 409


@pytest.mark.asyncio
async def test_invalid_and_duplicate_replies_do_not_call_secret_provider(sqlite_engine, monkeypatch):
    from skyvern.forge.sdk.routes import workflow_copilot as routes

    repo, client, ctx, _ = await setup_question_chat(sqlite_engine, monkeypatch)
    question = create_question_interaction(
        AskUserArguments.model_validate({"parts": [{"prompt": "Format?", "choices": ["CSV"]}]}),
        turn_id="turn",
        tool_call_id="call",
    )
    await repo.start_copilot_question("org", ctx.workflow_copilot_chat_id, question)
    resolver = routes.resolve_raw_secret_safety_handler
    body = {"workflow_copilot_chat_id": ctx.workflow_copilot_chat_id, "interaction_id": question.interaction_id}
    answer = {"part_id": question.parts[0].part_id, "text": "CSV"}
    async with client:
        for submission, status in [
            ({**body, "interaction_id": "unknown", "answers": [answer] * 100}, 404),
            ({**body, "answers": [answer, answer]}, 409),
            ({**body, "answers": [{"part_id": "foreign", "text": "CSV"}]}, 409),
            ({**body, "answers": [{**answer, "choice_id": "foreign"}]}, 409),
        ]:
            result = await client.post("/reply", json=submission)
            assert result.status_code == status
            resolver.assert_not_awaited()
        accepted = await client.post("/reply", json={**body, "text": "CSV"})
        assert accepted.status_code == 200
        resolver.reset_mock()
        duplicate = await client.post("/reply", json={**body, "answers": [answer] * 100})
        assert duplicate.json() == accepted.json()
        resolver.assert_not_awaited()


@pytest.mark.asyncio
async def test_client_checkins_extend_wait_and_abandoned_handler_exits(sqlite_engine, monkeypatch):
    from skyvern.forge.sdk.db.repositories import workflow_parameters as records

    repo, client, ctx, frames = await setup_question_chat(sqlite_engine, monkeypatch)
    now = [datetime.now(UTC)]

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now[0]

    monkeypatch.setattr(records, "datetime", Clock)
    async with client:
        task = asyncio.create_task(
            ask_user(ctx, AskUserArguments.model_validate({"parts": [{"prompt": "Format?"}]}), "call")
        )
        try:
            question = (await asyncio.wait_for(frames.get(), 5))["interactions"][0]
            for _ in range(3):
                now[0] += timedelta(minutes=4)
                # Authenticated history polling, including after a temporary disconnect,
                # keeps the recipient present without spending execution time.
                await repo.poll_copilot_question("org", ctx.workflow_copilot_chat_id, question["interaction_id"])
                history = await client.get(
                    "/history", params={"workflow_copilot_chat_id": ctx.workflow_copilot_chat_id}
                )
                assert history.json()["question_interactions"][0]["status"] == "pending"
                assert not task.done()
            now[0] += timedelta(minutes=6)
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 5)
            history = await client.get("/history", params={"workflow_copilot_chat_id": ctx.workflow_copilot_chat_id})
            assert history.json()["question_interactions"][0]["status"] == "interrupted"
            late = await client.post(
                "/reply",
                json={
                    "workflow_copilot_chat_id": ctx.workflow_copilot_chat_id,
                    "interaction_id": question["interaction_id"],
                    "text": "CSV",
                },
            )
            assert late.status_code == 409
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_during_screen", [False, True])
async def test_near_expiry_reply_renews_client_presence_and_rechecks_cancel(
    sqlite_engine, monkeypatch, cancel_during_screen
):
    from skyvern.forge.sdk.db.repositories import workflow_parameters as records
    from skyvern.forge.sdk.routes import workflow_copilot as routes

    repo, client, ctx, _ = await setup_question_chat(sqlite_engine, monkeypatch)
    now = [datetime.now(UTC)]

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now[0]

    monkeypatch.setattr(records, "datetime", Clock)
    question = create_question_interaction(
        AskUserArguments.model_validate({"parts": [{"prompt": "Format?"}]}), turn_id="turn", tool_call_id="call"
    )
    await repo.start_copilot_question("org", ctx.workflow_copilot_chat_id, question)
    now[0] += timedelta(minutes=4, seconds=59)
    await repo.poll_copilot_question("org", ctx.workflow_copilot_chat_id, question.interaction_id)

    async def screen(text, handler, **kwargs):
        now[0] += timedelta(minutes=2)
        if cancel_during_screen:
            await repo.cancel_copilot_questions("org", ctx.workflow_copilot_chat_id, "stop")
        # Simulate the actual owner polling while the provider is processing.
        recorded = await repo.poll_copilot_question("org", ctx.workflow_copilot_chat_id, question.interaction_id)
        assert recorded.status == ("cancelled" if cancel_during_screen else "pending")
        return SimpleNamespace(status="clean", canonical_user_message=text)

    monkeypatch.setattr(routes, "_screen_raw_secret_safety", screen)
    async with client:
        result = await client.post(
            "/reply",
            json={
                "workflow_copilot_chat_id": ctx.workflow_copilot_chat_id,
                "interaction_id": question.interaction_id,
                "text": "CSV",
            },
        )
        assert result.status_code == (409 if cancel_during_screen else 200)
        recorded = await repo.poll_copilot_question("org", ctx.workflow_copilot_chat_id, question.interaction_id)
        assert recorded.status == ("cancelled" if cancel_during_screen else "resolved")


@pytest.mark.asyncio
@pytest.mark.parametrize("delayed", [False, True])
async def test_cleanup_failure_cannot_trap_cancelled_question_owner(sqlite_engine, monkeypatch, delayed):
    import importlib

    questions = importlib.import_module("skyvern.forge.sdk.copilot.ask_user")
    repo, client, ctx, frames = await setup_question_chat(sqlite_engine, monkeypatch)
    release = asyncio.Event()
    cleanup_started = asyncio.Event()
    cleanup_tasks = []

    async def failed_cleanup(*args):
        cleanup_tasks.append(asyncio.current_task())
        cleanup_started.set()
        if delayed:
            try:
                await release.wait()
            except asyncio.CancelledError:
                # A driver may need time to unwind after cancellation.
                await release.wait()
        raise RuntimeError("fixture cleanup failure")

    monkeypatch.setattr(repo, "interrupt_copilot_question", failed_cleanup)
    monkeypatch.setattr(questions, "QUESTION_CLEANUP_TIMEOUT_SECONDS", 0.01, raising=False)
    async with client:
        task = asyncio.create_task(
            ask_user(ctx, AskUserArguments.model_validate({"parts": [{"prompt": "Format?"}]}), "call")
        )
        try:
            await asyncio.wait_for(frames.get(), 5)
            await repo.cancel_copilot_questions("org", ctx.workflow_copilot_chat_id, "stop")
            await asyncio.wait_for(cleanup_started.wait(), 5)
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 1)
            if delayed:
                assert not cleanup_tasks[0].done()
                assert cleanup_tasks[0] in questions._PENDING_QUESTION_CLEANUPS
        finally:
            release.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, *cleanup_tasks, return_exceptions=True)
        assert all(item not in questions._PENDING_QUESTION_CLEANUPS for item in cleanup_tasks)


def test_question_records_do_not_exempt_dead_turns_from_retention():
    from skyvern.forge.sdk.db.repositories.workflow_parameters import _prune_pending_turns

    now = datetime.now(UTC)
    old = now - timedelta(days=90)
    question = create_question_interaction(
        AskUserArguments.model_validate({"parts": [{"prompt": "Format?"}]}), turn_id="turn", tool_call_id="call"
    )
    entry = CopilotPendingTurn(
        turn_id="turn",
        started_at=old,
        question_interactions=[question],
        question_heartbeat_at=now,
        question_client_seen_at=now,
    )
    live = entry.model_dump(mode="json")
    dead = {**live, "question_heartbeat_at": old.isoformat()}
    absent = {**live, "question_client_seen_at": old.isoformat()}
    resolved = entry.model_copy(deep=True)
    resolved.question_interactions[0].status = "resolved"
    resolved.question_interactions[0].resolved_at = now
    recent = resolved.model_dump(mode="json")
    resolved.question_interactions[0].resolved_at = old
    expired = resolved.model_dump(mode="json")
    assert _prune_pending_turns(
        {"live": live, "dead": dead, "absent": absent, "recent": recent, "expired": expired}
    ) == {"live": live, "recent": recent}
