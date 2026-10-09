import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from agents import Agent, Model, RunConfig, SQLiteSession, function_tool
from agents.items import ModelResponse
from openai.types.responses import (
    Response,
    ResponseCompletedEvent,
    ResponseFunctionToolCall,
    ResponseOutputItemDoneEvent,
    ResponseOutputMessage,
    ResponseOutputText,
)

from skyvern.forge import app
from skyvern.forge.sdk.cache.local import LocalCache
from skyvern.forge.sdk.copilot.agent import _format_chat_history
from skyvern.forge.sdk.copilot.enforcement import run_with_enforcement
from skyvern.forge.sdk.copilot.hooks import CopilotRunHooks
from skyvern.forge.sdk.copilot.request_policy import (
    RequestPolicy,
    SteerMessageSiteURLSource,
    _ground_user_provided_sites,
)
from skyvern.forge.sdk.routes.workflow_copilot import _persist_turn_messages
from skyvern.forge.sdk.schemas.workflow_copilot import WorkflowCopilotChatHistoryResponse, WorkflowCopilotChatSender
from tests.unit.test_copilot_ask_user import setup_question_chat

STEER_TEXT = "Also extract the title from https://portal.example.com/home"


class DoorbellCache(LocalCache):
    """Signals the first read that finds a message waiting, so a test can act after the loop saw it."""

    def __init__(self) -> None:
        super().__init__()
        self.rang = asyncio.Event()

    async def get(self, key: str) -> Any:
        value = await super().get(key)
        if value and key.startswith("copilot_steer:"):
            self.rang.set()
        return value


def _completed(index: int, output: list[Any]) -> ResponseCompletedEvent:
    return ResponseCompletedEvent(
        sequence_number=0,
        type="response.completed",
        response=Response(
            id=f"resp_{index}",
            created_at=0.0,
            model="steer-test",
            object="response",
            output=output,
            parallel_tool_calls=True,
            tool_choice="auto",
            tools=[],
            status="completed",
        ),
    )


def _reply(text: str) -> ResponseOutputMessage:
    return ResponseOutputMessage(
        id="msg_reply",
        type="message",
        role="assistant",
        status="completed",
        content=[ResponseOutputText(type="output_text", text=text, annotations=[])],
    )


def _signal_when_steer_confirmed(monkeypatch: pytest.MonkeyPatch, repo: Any) -> asyncio.Event:
    """Set once the watcher has confirmed a waiting message; it then stops the stream before awaiting again."""
    confirmed = asyncio.Event()
    undelivered = repo.undelivered_copilot_steer_ids

    async def undelivered_then_signal(*args: str) -> set[str]:
        waiting = await undelivered(*args)
        if waiting:
            confirmed.set()
        return waiting

    monkeypatch.setattr(repo, "undelivered_copilot_steer_ids", undelivered_then_signal)
    return confirmed


async def _consume_sdk_stream(result: Any, *_args: Any) -> None:
    async for _event in result.stream_events():
        pass


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["model_call", "tool"])
async def test_send_now_joins_the_running_turn_and_its_saved_history(sqlite_engine, monkeypatch, boundary):
    repo, client, ctx, frames = await setup_question_chat(sqlite_engine, monkeypatch)
    monkeypatch.setattr(app, "CACHE", LocalCache())
    monkeypatch.setattr("skyvern.forge.sdk.copilot.steer.STEER_POLL_SECONDS", 0.01)
    monkeypatch.setattr("skyvern.forge.sdk.copilot.streaming_adapter.stream_to_sse", _consume_sdk_stream)
    steer_confirmed = _signal_when_steer_confirmed(monkeypatch, repo)
    ctx.request_policy = RequestPolicy()
    model_inputs: list[list[Any]] = []
    aborted_calls: list[int] = []
    working = asyncio.Event()
    release_tool = asyncio.Event()

    @function_tool
    async def inspect_page() -> str:
        working.set()
        await release_tool.wait()
        return "page inspected"

    class SteerModel(Model):
        async def get_response(self, *args: Any, **kwargs: Any) -> ModelResponse:
            raise AssertionError("The production runner must stream")

        async def stream_response(self, *args: Any, **kwargs: Any):
            model_inputs.append(kwargs["input"] if "input" in kwargs else args[1])
            call = len(model_inputs)
            if call == 1 and boundary == "model_call":
                working.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    aborted_calls.append(call)
                    raise
            if call == 1:
                output: list[Any] = [
                    ResponseFunctionToolCall(
                        type="function_call", name="inspect_page", call_id="call_inspect", arguments="{}"
                    )
                ]
            else:
                output = [_reply("Built it, and it extracts the title.")]
            yield _completed(call, output)

    session = SQLiteSession("steer")
    async with client:
        turn = asyncio.create_task(
            run_with_enforcement(
                agent=Agent(name="steer-test", model=SteerModel(), tools=[inspect_page]),
                initial_input="Build the workflow",
                ctx=ctx,
                stream=MagicMock(),
                session=session,
                hooks=CopilotRunHooks(ctx),
                run_config=RunConfig(tracing_disabled=True),
            )
        )
        try:
            await asyncio.wait_for(working.wait(), 5)
            body = {
                "workflow_copilot_chat_id": ctx.workflow_copilot_chat_id,
                "cancel_token": "stop",
                "steer_id": "steer-1",
                "message": STEER_TEXT,
            }
            sent = await client.post("/steer", json=body)
            assert sent.status_code == 200, sent.text
            if boundary == "tool":
                await asyncio.wait_for(steer_confirmed.wait(), 5)
                release_tool.set()
            result = await asyncio.wait_for(turn, 5)

            assert result.final_output == "Built it, and it extracts the title."
            assert len(model_inputs) == 2
            next_input = model_inputs[1]
            assert next_input[-1] == {"role": "user", "content": STEER_TEXT}
            if boundary == "model_call":
                assert aborted_calls == [1]
                assert not any(item.get("type") == "function_call_output" for item in next_input)
            else:
                assert aborted_calls == []
                outputs = [item for item in next_input if item.get("type") == "function_call_output"]
                assert [item["output"] for item in outputs] == ["page inspected"]
            delivered = [frame for frame in frames._queue if frame.get("type") == "steer_delivered"]
            assert [message["steer_id"] for frame in delivered for message in frame["steer_messages"]] == ["steer-1"]
            assert ctx.request_policy.user_site_url_sources["https://portal.example.com/home"] == (
                SteerMessageSiteURLSource(steer_id="steer-1")
            )
            assert ctx.request_policy.canonical_user_message.endswith(STEER_TEXT)

            chat = await repo.get_workflow_copilot_chat_by_id("org", ctx.workflow_copilot_chat_id)
            await _persist_turn_messages(
                chat=chat,
                turn_id="turn",
                user_message="Build the workflow",
                audio_artifact_id=None,
                user_row_already_persisted=True,
                sender=WorkflowCopilotChatSender.USER,
                assistant_content="Built it, and it extracts the title.",
                global_llm_context=None,
                turn_outcome=None,
                narrative_payload=None,
            )
            history = await client.get("/history", params={"workflow_copilot_chat_id": ctx.workflow_copilot_chat_id})
            loaded = WorkflowCopilotChatHistoryResponse.model_validate(history.json())
            assert [message.sender for message in loaded.chat_history] == [
                WorkflowCopilotChatSender.USER,
                WorkflowCopilotChatSender.AI,
            ]
            saved = loaded.chat_history[-1].narrative_payload["steerMessages"]
            assert [(item["steer_id"], item["text"]) for item in saved] == [("steer-1", STEER_TEXT)]
            assert saved[0]["delivered_at"] is not None
            assert f"user (sent while you were working): {STEER_TEXT}" in _format_chat_history(loaded.chat_history)
            next_turn_policy = RequestPolicy()
            _ground_user_provided_sites(next_turn_policy, "Add pagination", loaded.chat_history)
            assert next_turn_policy.user_site_url_sources["https://portal.example.com/home"] == (
                SteerMessageSiteURLSource(steer_id="steer-1")
            )

            late = await client.post("/steer", json={**body, "steer_id": "steer-2"})
            assert late.status_code == 409
        finally:
            release_tool.set()
            if not turn.done():
                turn.cancel()
                await asyncio.gather(turn, return_exceptions=True)
            session.close()


class _RecordingStream:
    def __init__(self) -> None:
        self.frames: list[Any] = []

    async def send(self, data: Any) -> bool:
        self.frames.append(data)
        return True

    async def is_disconnected(self) -> bool:
        return False


@pytest.mark.asyncio
async def test_send_now_waits_out_a_response_whose_tool_call_the_chat_already_shows(sqlite_engine, monkeypatch):
    repo, client, ctx, _ = await setup_question_chat(sqlite_engine, monkeypatch)
    monkeypatch.setattr(app, "CACHE", LocalCache())
    monkeypatch.setattr("skyvern.forge.sdk.copilot.steer.STEER_POLL_SECONDS", 0.01)
    steer_confirmed = _signal_when_steer_confirmed(monkeypatch, repo)
    model_inputs: list[list[Any]] = []
    aborted_calls: list[int] = []
    tool_call_streamed = asyncio.Event()
    release_model = asyncio.Event()
    call = ResponseFunctionToolCall(type="function_call", name="inspect_page", call_id="call_inspect", arguments="{}")

    @function_tool
    async def inspect_page() -> str:
        return "page inspected"

    class StreamingToolCallModel(Model):
        async def get_response(self, *args: Any, **kwargs: Any) -> ModelResponse:
            raise AssertionError("The production runner must stream")

        async def stream_response(self, *args: Any, **kwargs: Any):
            model_inputs.append(kwargs["input"] if "input" in kwargs else args[1])
            index = len(model_inputs)
            if index == 1:
                yield ResponseOutputItemDoneEvent(
                    item=call, output_index=0, sequence_number=0, type="response.output_item.done"
                )
                tool_call_streamed.set()
                try:
                    await release_model.wait()
                except asyncio.CancelledError:
                    aborted_calls.append(index)
                    raise
                yield _completed(index, [call])
                return
            yield _completed(index, [_reply("Built it, and it extracts the title.")])

    stream = _RecordingStream()
    session = SQLiteSession("streamed-tool-call")
    async with client:
        turn = asyncio.create_task(
            run_with_enforcement(
                agent=Agent(name="steer-test", model=StreamingToolCallModel(), tools=[inspect_page]),
                initial_input="Build the workflow",
                ctx=ctx,
                stream=stream,
                session=session,
                hooks=CopilotRunHooks(ctx),
                run_config=RunConfig(tracing_disabled=True),
            )
        )
        try:
            await asyncio.wait_for(tool_call_streamed.wait(), 5)
            sent = await client.post(
                "/steer",
                json={
                    "workflow_copilot_chat_id": ctx.workflow_copilot_chat_id,
                    "cancel_token": "stop",
                    "steer_id": "steer-1",
                    "message": STEER_TEXT,
                },
            )
            assert sent.status_code == 200, sent.text
            await asyncio.wait_for(steer_confirmed.wait(), 5)
            release_model.set()
            result = await asyncio.wait_for(turn, 5)

            assert aborted_calls == []
            assert result.final_output == "Built it, and it extracts the title."
            next_input = model_inputs[1]
            assert [item["output"] for item in next_input if item.get("type") == "function_call_output"] == [
                "page inspected"
            ]
            assert next_input[-1] == {"role": "user", "content": STEER_TEXT}
        finally:
            release_model.set()
            if not turn.done():
                turn.cancel()
                await asyncio.gather(turn, return_exceptions=True)
            session.close()


@pytest.mark.asyncio
async def test_steer_endpoint_screens_once_and_binds_only_to_the_running_turn(sqlite_engine, monkeypatch):
    from skyvern.forge.sdk.routes import workflow_copilot as routes

    repo, client, ctx, _ = await setup_question_chat(sqlite_engine, monkeypatch)
    monkeypatch.setattr(app, "CACHE", LocalCache())
    secret = "sk-proj-" + "aB3dE5fG7hJ9kL2mN4pQ6rS8tU0vW1xY" * 3
    body = {
        "workflow_copilot_chat_id": ctx.workflow_copilot_chat_id,
        "cancel_token": "stop",
        "steer_id": "steer-1",
        "message": f"Use the key {secret} for the API step",
    }
    async with client:
        foreign = await client.post("/steer", json=body, headers={"test-org": "foreign"})
        assert foreign.status_code == 404
        other_turn = await client.post("/steer", json={**body, "cancel_token": "other"})
        assert other_turn.status_code == 409
        routes.resolve_raw_secret_safety_handler.assert_not_awaited()

        accepted = await client.post("/steer", json=body)
        assert accepted.status_code == 200, accepted.text
        assert secret not in accepted.text
        retried = await client.post("/steer", json={**body, "message": "A different retry body"})
        assert retried.json() == accepted.json()
        routes.resolve_raw_secret_safety_handler.assert_awaited_once()

        chat = await repo.get_workflow_copilot_chat_by_id("org", ctx.workflow_copilot_chat_id)
        stored = chat.pending_turns["turn"].steer_messages
        assert [item.steer_id for item in stored] == ["steer-1"]
        assert secret not in stored[0].text
        assert await app.CACHE.get("copilot_steer:org:stop") == "steer-1"

        monkeypatch.setattr("skyvern.forge.sdk.db.repositories.workflow_parameters.MAX_STEER_MESSAGES_PER_TURN", 1)
        over_cap = await client.post("/steer", json={**body, "steer_id": "steer-2"})
        assert over_cap.status_code == 409
        routes.resolve_raw_secret_safety_handler.assert_awaited_once()


@pytest.mark.asyncio
async def test_doorbell_naming_a_message_this_turn_never_recorded_does_not_interrupt_it(sqlite_engine, monkeypatch):
    _, _, ctx, _ = await setup_question_chat(sqlite_engine, monkeypatch)
    cache = DoorbellCache()
    monkeypatch.setattr(app, "CACHE", cache)
    monkeypatch.setattr("skyvern.forge.sdk.copilot.steer.STEER_POLL_SECONDS", 0.01)
    monkeypatch.setattr("skyvern.forge.sdk.copilot.streaming_adapter.stream_to_sse", _consume_sdk_stream)
    await cache.set("copilot_steer:org:stop", "steer-from-an-earlier-turn")
    release_model = asyncio.Event()
    aborted_calls: list[int] = []

    class SlowModel(Model):
        async def get_response(self, *args: Any, **kwargs: Any) -> ModelResponse:
            raise AssertionError("The production runner must stream")

        async def stream_response(self, *args: Any, **kwargs: Any):
            try:
                await release_model.wait()
            except asyncio.CancelledError:
                aborted_calls.append(1)
                raise
            yield _completed(1, [_reply("Done.")])

    session = SQLiteSession("stale-doorbell")
    turn = asyncio.create_task(
        run_with_enforcement(
            agent=Agent(name="steer-test", model=SlowModel(), tools=[]),
            initial_input="Build the workflow",
            ctx=ctx,
            stream=MagicMock(),
            session=session,
            hooks=CopilotRunHooks(ctx),
            run_config=RunConfig(tracing_disabled=True),
        )
    )
    try:
        await asyncio.wait_for(cache.rang.wait(), 5)
        await asyncio.sleep(0.2)
        release_model.set()
        result = await asyncio.wait_for(turn, 5)
        assert result.final_output == "Done."
        assert aborted_calls == []
    finally:
        release_model.set()
        if not turn.done():
            turn.cancel()
            await asyncio.gather(turn, return_exceptions=True)
        session.close()


@pytest.mark.asyncio
async def test_messages_keep_their_send_order_when_screens_finish_out_of_order(sqlite_engine, monkeypatch):
    from skyvern.forge.sdk.routes import workflow_copilot as routes

    repo, client, ctx, _ = await setup_question_chat(sqlite_engine, monkeypatch)
    monkeypatch.setattr(app, "CACHE", LocalCache())
    screened = {"first": asyncio.Event(), "second": asyncio.Event()}

    async def screen_when_released(text: str, *_args: object, **_kwargs: object) -> SimpleNamespace:
        await screened[text].wait()
        return SimpleNamespace(status="clean", canonical_user_message=text)

    monkeypatch.setattr(routes, "_screen_raw_secret_safety", screen_when_released)
    body = {"workflow_copilot_chat_id": ctx.workflow_copilot_chat_id, "cancel_token": "stop"}
    async with client:
        first = asyncio.create_task(client.post("/steer", json={**body, "steer_id": "steer-1", "message": "first"}))
        await asyncio.sleep(0.05)
        second = asyncio.create_task(client.post("/steer", json={**body, "steer_id": "steer-2", "message": "second"}))
        await asyncio.sleep(0.05)
        screened["second"].set()
        assert (await asyncio.wait_for(second, 5)).status_code == 200
        screened["first"].set()
        assert (await asyncio.wait_for(first, 5)).status_code == 200

    steers = await repo.take_copilot_steer_messages("org", ctx.workflow_copilot_chat_id, "turn")
    assert [steer.text for steer in steers] == ["first", "second"]


@pytest.mark.asyncio
async def test_message_sent_during_a_rejected_response_joins_the_call_that_follows_it(sqlite_engine, monkeypatch):
    _repo, client, ctx, _frames = await setup_question_chat(sqlite_engine, monkeypatch)
    monkeypatch.setattr(app, "CACHE", LocalCache())
    # The watcher never wakes within this test, so only the loop itself can hand the message over.
    monkeypatch.setattr("skyvern.forge.sdk.copilot.steer.STEER_POLL_SECONDS", 60)
    monkeypatch.setattr("skyvern.forge.sdk.copilot.streaming_adapter.stream_to_sse", _consume_sdk_stream)
    model_inputs: list[list[Any]] = []
    working = asyncio.Event()
    release_model = asyncio.Event()

    class MadeUpToolModel(Model):
        async def get_response(self, *args: Any, **kwargs: Any) -> ModelResponse:
            raise AssertionError("The production runner must stream")

        async def stream_response(self, *args: Any, **kwargs: Any):
            model_inputs.append(kwargs["input"] if "input" in kwargs else args[1])
            call = len(model_inputs)
            if call > 1:
                yield _completed(call, [_reply("Built it, and it extracts the title.")])
                return
            working.set()
            await release_model.wait()
            made_up = ResponseFunctionToolCall(
                type="function_call", name="REPLY", call_id="call_made_up", arguments="{}"
            )
            yield _completed(call, [made_up])

    session = SQLiteSession("steer-after-rejected-response")
    async with client:
        turn = asyncio.create_task(
            run_with_enforcement(
                agent=Agent(name="steer-test", model=MadeUpToolModel(), tools=[]),
                initial_input="Build the workflow",
                ctx=ctx,
                stream=SimpleNamespace(send=ctx.stream.send, is_disconnected=AsyncMock(return_value=False)),
                session=session,
                hooks=CopilotRunHooks(ctx),
                run_config=RunConfig(tracing_disabled=True),
            )
        )
        try:
            await asyncio.wait_for(working.wait(), 5)
            sent = await client.post(
                "/steer",
                json={
                    "workflow_copilot_chat_id": ctx.workflow_copilot_chat_id,
                    "cancel_token": "stop",
                    "steer_id": "steer-1",
                    "message": STEER_TEXT,
                },
            )
            assert sent.status_code == 200, sent.text
            release_model.set()
            result = await asyncio.wait_for(turn, 5)

            assert result.final_output == "Built it, and it extracts the title."
            assert len(model_inputs) == 2
            assert model_inputs[1][-1] == {"role": "user", "content": STEER_TEXT}
            assert [item["call_id"] for item in model_inputs[1] if item.get("type") == "function_call_output"] == [
                "call_made_up"
            ]
        finally:
            release_model.set()
            if not turn.done():
                turn.cancel()
                await asyncio.gather(turn, return_exceptions=True)
            session.close()
