from __future__ import annotations

import ast
from pathlib import Path

import pytest

from skyvern.forge.agent_functions import AgentFunction
from skyvern.forge.taskv3 import loop as loop_module
from skyvern.forge.taskv3.application_context import DECLARE_NOTICE, ApplicationContext
from skyvern.forge.taskv3.loop import make_finish_tool
from tests.unit.test_taskv3_loop import _recording_tool, _run

DEFAULTS = "the defaults text"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "declared_at,state,delivered",
    [
        ({1: True}, "yes_declared", True),
        ({1: False}, "no_declared", False),
        ({}, "no_implicit", False),
        ({3: True}, "late_yes", True),
        ({1: False, 2: True}, "late_yes", True),
        ({1: True, 2: False}, "yes_declared", True),
        ({3: False}, "no_implicit", False),
        ({1: "yes"}, "no_implicit", False),
    ],
)
async def test_declaration_states_and_what_they_deliver(
    declared_at: dict[int, object], state: str, delivered: bool
) -> None:
    context = ApplicationContext(DEFAULTS)
    appended = []
    for turn in range(1, 6):
        notice = context.turn_notice(turn)
        if notice:
            appended.append(notice)
        if turn in declared_at:
            await context.tool.handler({"is_job_application": declared_at[turn]})
    assert context.final_state == state
    assert appended.count(DECLARE_NOTICE) == 1
    assert appended.count(DEFAULTS) == (1 if delivered else 0)
    assert context.delivered is delivered


@pytest.mark.asyncio
async def test_a_malformed_declaration_is_refused_and_records_nothing() -> None:
    context = ApplicationContext(DEFAULTS)
    context.turn_notice(1)
    result = await context.tool.handler({"is_job_application": "yes"})
    assert result.status == "error"
    context.turn_notice(2)
    assert context.final_state == "no_implicit" and not context.declared_yes


@pytest.mark.asyncio
async def test_the_tool_result_is_a_receipt_and_never_the_text() -> None:
    result = await ApplicationContext(DEFAULTS).tool.handler({"is_job_application": True})
    assert DEFAULTS not in str(result)


def test_a_run_that_ends_on_its_first_turn_without_a_declaration_reads_as_no_implicit() -> None:
    context = ApplicationContext(DEFAULTS)
    context.turn_notice(1)
    assert context.final_state == "no_implicit"


def test_a_run_never_asked_reads_not_asked_and_cannot_read_as_a_no() -> None:
    assert ApplicationContext(DEFAULTS).final_state == "not_asked"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "answers,declared_yes",
    [([], False), ([False], False), ([True], True), ([False, True], True), ([True, False], True)],
)
async def test_declared_yes_arms_the_audit_only_after_a_yes(answers: list[bool], declared_yes: bool) -> None:
    context = ApplicationContext(DEFAULTS)
    qualified = ApplicationContext(DEFAULTS, payload_qualified=True)
    assert context.audit_armed() is False and qualified.audit_armed() is True
    for answer in answers:
        await context.tool.handler({"is_job_application": answer})
        await qualified.tool.handler({"is_job_application": answer})
    assert context.declared_yes is declared_yes and context.audit_armed() is declared_yes
    assert qualified.audit_armed() is True


@pytest.mark.asyncio
async def test_loop_appends_the_notice_and_the_defaults_as_user_messages_once() -> None:
    context = ApplicationContext(DEFAULTS)
    tools = [_recording_tool("observe", []), context.tool, make_finish_tool()]
    script = [
        [("declare_application_context", {"is_job_application": True}), ("observe", {})],
        [("observe", {})],
        [("finish", {"status": "completed", "reason": "done"})],
    ]
    outcome, _ = await _run(script, tools, turn_notice=context.turn_notice)
    messages = outcome.messages
    users = [m["content"] for m in messages if m["role"] == "user"]
    assert users.count(DECLARE_NOTICE) == 1
    assert users.count(DEFAULTS) == 1
    first_reply = next(i for i, m in enumerate(messages) if m["role"] == "assistant")
    assert next(i for i, m in enumerate(messages) if m.get("content") == DECLARE_NOTICE) < first_reply
    assert next(i for i, m in enumerate(messages) if m.get("content") == DEFAULTS) > first_reply


@pytest.mark.asyncio
async def test_loop_delivers_once_the_turn_after_a_late_yes() -> None:
    context = ApplicationContext(DEFAULTS)
    tools = [_recording_tool("observe", []), context.tool, make_finish_tool()]
    script = [
        [("observe", {})],
        [("declare_application_context", {"is_job_application": True}), ("observe", {})],
        [("observe", {})],
        [("finish", {"status": "completed", "reason": "done"})],
    ]
    outcome, _ = await _run(script, tools, turn_notice=context.turn_notice)
    messages = outcome.messages
    assert [m["content"] for m in messages if m["role"] == "user"].count(DEFAULTS) == 1
    replies = [i for i, m in enumerate(messages) if m["role"] == "assistant"]
    assert replies[1] < messages.index({"role": "user", "content": DEFAULTS}) < replies[2]
    assert (context.final_state, context.delivered, context.audit_armed()) == ("late_yes", True, True)


@pytest.mark.asyncio
async def test_a_yes_sent_with_finish_delivers_nothing() -> None:
    context = ApplicationContext(DEFAULTS)
    tools = [_recording_tool("observe", []), context.tool, make_finish_tool()]
    script = [
        [("observe", {})],
        [
            ("declare_application_context", {"is_job_application": True}),
            ("finish", {"status": "completed", "reason": "done"}),
        ],
    ]
    outcome, _ = await _run(script, tools, turn_notice=context.turn_notice)
    assert DEFAULTS not in [m["content"] for m in outcome.messages if m["role"] == "user"]
    assert (context.final_state, context.delivered) == ("late_yes", False)


@pytest.mark.asyncio
async def test_a_failing_turn_notice_does_not_end_the_run() -> None:
    def failing(turn: int) -> str | None:
        raise RuntimeError("notice failed")

    script = [[("finish", {"status": "completed", "reason": "done"})]]
    outcome, _ = await _run(script, [make_finish_tool()], turn_notice=failing)
    assert outcome.status == "completed"


def test_oss_base_supplies_no_texts() -> None:
    assert AgentFunction().task_v3_application_context_texts({"resume": "text"}) is None


def test_loop_module_does_not_import_app() -> None:
    tree = ast.parse(Path(loop_module.__file__).read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert node.module != "skyvern.forge.app" and not (node.module or "").startswith("skyvern.forge.app.")
            if node.module == "skyvern.forge":
                assert all(alias.name != "app" for alias in node.names)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name != "skyvern.forge.app" and not alias.name.startswith("skyvern.forge.app.")
