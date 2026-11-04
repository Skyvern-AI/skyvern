from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.forge.sdk.copilot import agent as agent_module
from skyvern.forge.sdk.copilot.config import CopilotConfig
from skyvern.forge.sdk.copilot.context import CopilotContext
from skyvern.forge.sdk.copilot.request_policy import RAW_SECRET_REFUSAL_SENTINEL
from skyvern.forge.sdk.copilot.tools import read_chat_history_tool
from skyvern.forge.sdk.copilot.tools.chat_history import (
    EARLIER_UNAVAILABLE_ERROR,
    INVALID_CURSOR_ERROR,
    PAGE_CHAR_BUDGET,
)
from skyvern.forge.sdk.routes.workflow_copilot import CHAT_HISTORY_CONTEXT_MESSAGES
from skyvern.forge.sdk.schemas.workflow_copilot import (
    WorkflowCopilotChatHistoryMessage,
    WorkflowCopilotChatRequest,
    WorkflowCopilotChatSender,
)
from tests.unit.conftest import invoke_copilot_tool
from tests.unit.copilot_test_helpers import stub_copilot_agent_loop

_T0 = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
OFFER = "I can switch this workflow's browser to a residential proxy if you want; it is easy to undo."
DEFERRAL = "Not now, leave the proxy as it is."


def _msg(
    index: int,
    content: str,
    sender: str = "user",
    *,
    at: datetime | None = None,
    narrative_payload: dict[str, Any] | None = None,
) -> WorkflowCopilotChatHistoryMessage:
    return WorkflowCopilotChatHistoryMessage(
        workflow_copilot_chat_message_id=f"wcm_{index}",
        sender=WorkflowCopilotChatSender(sender),
        content=content,
        created_at=at or _T0 + timedelta(minutes=index),
        narrative_payload=narrative_payload,
    )


def _chat(
    length: int, overrides: dict[int, WorkflowCopilotChatHistoryMessage] | None = None
) -> list[WorkflowCopilotChatHistoryMessage]:
    rows = [_msg(i, f"harmless turn {i}", "user" if i % 2 == 0 else "ai") for i in range(length)]
    for index, row in (overrides or {}).items():
        rows[index] = row
    return rows


async def _run_turn(
    monkeypatch: pytest.MonkeyPatch,
    messages: list[WorkflowCopilotChatHistoryMessage],
    chat_history: list[WorkflowCopilotChatHistoryMessage] | None = None,
) -> tuple[CopilotContext, str]:
    """Hand the agent the route's two lists, both loaded before the current turn's row is saved."""
    monkeypatch.setattr(agent_module, "restore_pending_workflow_proposal", AsyncMock(return_value=None))
    monkeypatch.setattr(agent_module, "seed_repair_origin_run", AsyncMock(return_value=SimpleNamespace(finished=True)))
    monkeypatch.setattr(agent_module, "hydrate_prior_run_packet", AsyncMock(return_value=None))
    stub_copilot_agent_loop(
        monkeypatch,
        AsyncMock(return_value=SimpleNamespace(final_output='{"type":"REPLY","user_response":"ok"}', new_items=[])),
    )
    build_context = MagicMock(wraps=agent_module._build_user_context)
    monkeypatch.setattr(agent_module, "_build_user_context", build_context)
    sink: list[CopilotContext] = []
    await agent_module.run_copilot_agent(
        stream=MagicMock(),
        organization_id="org-1",
        chat_request=WorkflowCopilotChatRequest(
            message="what did you offer earlier?",
            workflow_id="wf-1",
            workflow_permanent_id="wfp-1",
            workflow_copilot_chat_id="chat-1",
            workflow_yaml="title: Canonical\n",
        ),
        chat_history=messages[-CHAT_HISTORY_CONTEXT_MESSAGES:] if chat_history is None else chat_history,
        prior_user_messages=messages,
        global_llm_context=None,
        llm_api_handler=SimpleNamespace(llm_key="PRIMARY"),
        raw_secret_safety_handler=AsyncMock(
            return_value={"version": "1", "state": "clean", "handling": "none", "citations": []}
        ),
        api_key="sk-test",
        config=CopilotConfig(),
        ctx_sink=sink,
    )
    return sink[0], build_context.call_args.kwargs["chat_history_text"]


async def _read(ctx: CopilotContext, **arguments: str | int) -> dict[str, Any]:
    return await invoke_copilot_tool(read_chat_history_tool, ctx, arguments)


async def _read_backward(ctx: CopilotContext, limit: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    pages = [await _read(ctx, limit=limit)]
    while "earlier_cursor" in pages[-1]:
        pages.append(await _read(ctx, cursor=pages[-1]["earlier_cursor"], limit=limit))
    rows = [row for page in reversed(pages) for row in page["messages"]]
    return pages, rows


async def _read_clipped_row(ctx: CopilotContext, page: dict[str, Any]) -> tuple[str, list[int]]:
    entry = page["messages"][0]
    parts, offsets = [entry["text"]], [entry["offset"]]
    while entry["complete"] is False:
        page = await _read(ctx, cursor=entry["continuation_cursor"])
        assert len(json.dumps(page, ensure_ascii=False)) <= PAGE_CHAR_BUDGET
        entry = page["messages"][0]
        parts.append(entry["text"])
        offsets.append(entry["offset"])
    return "".join(parts), offsets


@pytest.mark.asyncio
async def test_an_offer_older_than_the_prompt_window_is_reached_by_paging_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    messages = _chat(30, {3: _msg(3, OFFER, "ai"), 4: _msg(4, DEFERRAL, "user"), 6: _msg(6, "Stop.", "user")})
    ctx, chat_history_text = await _run_turn(monkeypatch, messages)

    older = len(messages) - CHAT_HISTORY_CONTEXT_MESSAGES
    assert CHAT_HISTORY_CONTEXT_MESSAGES == 10
    assert chat_history_text.splitlines()[0] == (
        f"earlier_messages_not_shown: {older}; oldest_shown_message_id: wcm_{older}"
    )
    assert "harmless turn 29" in chat_history_text and f"harmless turn {older - 1}" not in chat_history_text
    assert OFFER not in chat_history_text

    pages, rows = await _read_backward(ctx, limit=6)
    assert [row["message_id"] for row in rows] == [f"wcm_{i}" for i in range(older)]
    assert pages[-1]["start_of_history"] is True
    offer_at = next(i for i, row in enumerate(rows) if OFFER in row["text"])
    assert (rows[offer_at]["sender"], rows[offer_at + 1]["sender"]) == ("ai", "user")
    assert rows[offer_at + 1]["text"] == f"user: {DEFERRAL}"
    assert (rows[6]["sender"], rows[6]["text"]) == ("user", "user: Stop.")
    assert await _read(ctx, cursor=pages[1]["earlier_cursor"], limit=6) == pages[2]
    assert await _read(ctx, cursor=f"wcm_{older}:0", limit=6) == pages[0]


@pytest.mark.asyncio
async def test_a_cursor_from_an_earlier_turn_reads_the_same_rows_after_the_chat_grows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    messages = _chat(40, {3: _msg(3, OFFER, "ai")})
    shorter, _ = await _run_turn(monkeypatch, messages[:25])
    longer, _ = await _run_turn(monkeypatch, messages)
    cursor = (await _read(shorter, limit=5))["earlier_cursor"]

    assert await _read(longer, cursor=cursor, limit=5) == await _read(shorter, cursor=cursor, limit=5)


_UNPLACEABLE_CHAT = _chat(30, {3: _msg(3, OFFER, "ai")})
_ID_LESS = {"workflow_copilot_chat_message_id": None}


@pytest.mark.parametrize(
    ("full_chat", "window"),
    [
        (_UNPLACEABLE_CHAT, _UNPLACEABLE_CHAT[5:15]),
        (_UNPLACEABLE_CHAT[:5], _UNPLACEABLE_CHAT[-10:]),
        ([_UNPLACEABLE_CHAT[0].model_copy(update=_ID_LESS), *_UNPLACEABLE_CHAT[1:]], _UNPLACEABLE_CHAT[-10:]),
        (
            [*_UNPLACEABLE_CHAT[:20], _UNPLACEABLE_CHAT[20].model_copy(update=_ID_LESS), *_UNPLACEABLE_CHAT[21:]],
            [_UNPLACEABLE_CHAT[20].model_copy(update=_ID_LESS), *_UNPLACEABLE_CHAT[21:]],
        ),
    ],
    ids=["window_mid_chat", "full_chat_shorter_than_window", "id_less_earlier_row", "id_less_oldest_shown_row"],
)
@pytest.mark.asyncio
async def test_a_window_that_is_not_the_tail_of_the_chat_gets_no_earlier_rows(
    monkeypatch: pytest.MonkeyPatch,
    full_chat: list[WorkflowCopilotChatHistoryMessage],
    window: list[WorkflowCopilotChatHistoryMessage],
) -> None:
    ctx, chat_history_text = await _run_turn(monkeypatch, full_chat, chat_history=window)

    assert "earlier_messages_not_shown" not in chat_history_text
    for cursor in (None, "wcm_5:0", "wcm_2:0"):
        result = await _read(ctx) if cursor is None else await _read(ctx, cursor=cursor)
        assert result == {"ok": False, "error": EARLIER_UNAVAILABLE_ERROR}


@pytest.mark.asyncio
async def test_a_short_chat_has_no_earlier_messages_and_no_history_fact(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx, chat_history_text = await _run_turn(monkeypatch, _chat(CHAT_HISTORY_CONTEXT_MESSAGES))

    assert "earlier_messages_not_shown" not in chat_history_text
    assert await _read(ctx) == {"ok": True, "messages": [], "start_of_history": True}


@pytest.mark.asyncio
async def test_rows_render_and_redact_as_the_recent_window_does(monkeypatch: pytest.MonkeyPatch) -> None:
    card = {
        "questionInteractions": [
            {
                "interaction_id": "qi_1",
                "turn_id": "turn_1",
                "tool_call_id": "call_1",
                "parts": [{"part_id": "p1", "prompt": "Which account?"}],
                "status": "resolved",
                "response": {"answers": [{"part_id": "p1", "text": "password: hunter2swordfish"}]},
            }
        ]
    }
    protected_card = {
        "questionInteractions": [
            {
                "interaction_id": "qi_2",
                "turn_id": "turn_2",
                "tool_call_id": "call_2",
                "parts": [{"part_id": "p2", "prompt": "How should I sign in?"}],
                "status": "resolved",
                "response": {
                    "answers": [{"part_id": "p2", "text": "passcode=39XqLm77!"}],
                    "text": "otp: 771205",
                    "raw_secret_detected": True,
                },
            }
        ]
    }
    overrides = {
        0: _msg(0, "Diagnose run wr_1 and repair the workflow.", "product"),
        3: _msg(3, OFFER, "ai"),
        4: _msg(4, "Stop", "user"),
        # The refusal is the first row of a page and the pasted secret the last row of the one before it.
        5: _msg(5, "my login is jdoe and the pw is Zebra!Quartz77", "user"),
        6: _msg(6, f"{RAW_SECRET_REFUSAL_SENTINEL}. Save it as a credential instead.", "ai"),
        7: _msg(7, "I can run it with that account once you confirm.", "ai", narrative_payload=card),
        8: _msg(8, "the verification code is 482913", "user"),
        11: _msg(11, "Signing in the way you chose.", "ai", narrative_payload=protected_card),
    }
    messages = _chat(22, overrides)
    ctx, _ = await _run_turn(monkeypatch, messages)

    first = await _read(ctx, limit=6)
    assert first["messages"][0]["message_id"] == "wcm_6"
    _, rows = await _read_backward(ctx, limit=6)
    _, window_text = await _run_turn(monkeypatch, messages[2:12])
    rendered = "\n".join(row["text"] for row in rows[2:])
    assert rendered == window_text
    for leaked in ("Zebra!Quartz77", "hunter2swordfish", "482913", "771205", "39XqLm77!"):
        assert leaked not in rendered
    assert (rows[0]["sender"], rows[0]["text"]) == ("product", "user: Diagnose run wr_1 and repair the workflow.")
    card_answer, proposal = rows[7]["text"].splitlines()
    assert rows[7]["sender"] == "ai" and card_answer.startswith("user question result: ")
    assert proposal == "ai: I can run it with that account once you confirm."
    assert f"{card_answer}\n{proposal}" in window_text
    assert (rows[3]["sender"], rows[3]["text"]) == ("ai", f"ai: {OFFER}")
    assert (rows[4]["sender"], rows[4]["text"]) == ("user", "user: Stop")
    assert "user: Stop" in window_text and f"ai: {OFFER}" in window_text


@pytest.mark.asyncio
async def test_pages_stay_under_the_char_budget_and_a_long_row_continues_exactly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    long_text = ('Zoë said "ok"\n日本語 ' * 2500)[:37_000]
    same_second = _T0 + timedelta(minutes=2)
    overrides = {
        0: _msg(0, "\n" * 20_000, "user"),
        1: _msg(1, "x" * 9_000, "ai"),
        2: _msg(2, "ü" * 9_000, "user", at=same_second),
        # Same created_at, and an id that sorts before its predecessor: snapshot order must hold.
        3: WorkflowCopilotChatHistoryMessage(
            workflow_copilot_chat_message_id="wcm_01",
            sender=WorkflowCopilotChatSender.AI,
            content="second",
            created_at=same_second,
        ),
        4: _msg(4, long_text, "ai"),
    }
    messages = _chat(16, overrides)
    ctx, _ = await _run_turn(monkeypatch, messages)

    pages, rows = await _read_backward(ctx, limit=50)
    for page in pages:
        assert len(json.dumps(page, ensure_ascii=False)) <= PAGE_CHAR_BUDGET
    expected = {"wcm_4": f"ai: {long_text}", "wcm_0": "user: " + "\n" * 20_000}
    for message_id, full_text in expected.items():
        clipped = next(page for page in pages if page["messages"][0]["message_id"] == message_id)
        assert len(clipped["messages"]) == 1 and clipped["messages"][0]["complete"] is False
        text, offsets = await _read_clipped_row(ctx, clipped)
        assert offsets[0] == 0 and offsets == sorted(set(offsets))
        assert text == full_text
    assert (await _read(ctx, cursor="wcm_4:1"))["earlier_cursor"] == "wcm_4:0"
    ids = [row["message_id"] for row in rows]
    assert ids == ["wcm_0", "wcm_1", "wcm_2", "wcm_01", "wcm_4", "wcm_5"]


@pytest.mark.asyncio
async def test_a_cursor_not_from_this_chat_returns_the_fixed_error(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx_a, _ = await _run_turn(monkeypatch, _chat(20))
    chat_b = [
        WorkflowCopilotChatHistoryMessage(
            workflow_copilot_chat_message_id=f"other_{i}",
            sender=WorkflowCopilotChatSender.USER,
            content=f"chat B secret plan {i}",
            created_at=_T0,
        )
        for i in range(20)
    ]
    ctx_b, _ = await _run_turn(monkeypatch, chat_b)
    cursor_b = (await _read(ctx_b, limit=2))["earlier_cursor"]

    forged = (cursor_b, "nope", "wcm_999:0", "wcm_3:abc", "wcm_3:-1", "wcm_3:99999", "wcm_15:0", ":0")
    for cursor in (*forged, "wcm_3:" + "9" * 5000, "wcm_3:" + "0" * 19):
        result = await _read(ctx_a, cursor=cursor)
        assert result == {"ok": False, "error": INVALID_CURSOR_ERROR}
    assert await _read(ctx_a, cursor="wcm_8:0", limit=3) == await _read(ctx_a, cursor="wcm_8:0", limit=3)
    assert (await _read(ctx_b, cursor=cursor_b, limit=2))["messages"][0]["text"].startswith("user: chat B")
    assert await _read(ctx_a, cursor="wcm_0:0") == {"ok": True, "messages": [], "start_of_history": True}


@pytest.mark.asyncio
async def test_an_unreadable_old_question_card_does_not_block_paging(monkeypatch: pytest.MonkeyPatch) -> None:
    stale = {"questionInteractions": [{"interaction_id": "qi_old", "parts": "not a list"}]}
    messages = _chat(24, {5: _msg(5, "Accepted: keep going", "product", narrative_payload=stale)})
    ctx, _ = await _run_turn(monkeypatch, messages)

    pages, rows = await _read_backward(ctx, limit=4)
    assert [row["message_id"] for row in rows] == [f"wcm_{i}" for i in range(14)]
    assert rows[5]["text"] == "user question interaction: unreadable\nuser: Accepted: keep going"
    assert len(pages) == 4 and "qi_old" not in json.dumps(pages)
