"""Faithful Task V3 agent tool-loop.

A single persistent LLM conversation drives browser tools via native tool-calling:
the model emits ``tool_calls``, we execute them, thread the results back as ``tool``
messages, and repeat until the model calls a terminal tool (``finish``) or a budget
cap is hit. Perception is a tool the model chooses to call — nothing about the page
is injected automatically — which is what distinguishes this from the step engine's
scrape-every-step loop.

The loop itself is transport-agnostic: it depends only on an ``LLMCaller``-shaped
object and a list of ``ToolSpec``. Browser wiring lives in a separate module so this
core can be unit-tested with scripted fakes.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Literal

import structlog

LOG = structlog.get_logger()

ToolStatus = Literal["ok", "error"]
FinishStatus = Literal["completed", "failed", "terminated"]


@dataclass
class ToolResult:
    status: ToolStatus
    content: str
    data: dict[str, Any] | None = None

    @classmethod
    def ok(cls, content: str, data: dict[str, Any] | None = None) -> ToolResult:
        return cls("ok", content, data)

    @classmethod
    def error(cls, content: str) -> ToolResult:
        return cls("error", content, None)


ToolHandler = Callable[[dict[str, Any]], Awaitable[ToolResult]]


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: ToolHandler
    terminal: bool = False
    billable: bool = False  # a page-mutating browser action that meters like a step-engine action

    def to_openai_tool(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


@dataclass
class LoopOutcome:
    status: Literal["completed", "failed", "terminated", "budget_exhausted", "loop_error", "canceled"]
    reason: str
    extracted_output: Any = None
    turns: int = 0
    tool_calls: int = 0
    billable_actions: list[str] = field(default_factory=list)
    messages: list[dict[str, Any]] = field(default_factory=list)


def _get(obj: Any, key: str, default: Any = None) -> Any:
    # raw_response=True returns a model_dump() dict, but test fakes and some
    # providers hand back objects — accept either shape.
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _extract_message(response: Any) -> Any:
    choices = _get(response, "choices") or []
    if not choices:
        return None
    return _get(choices[0], "message")


def _extract_text(response: Any) -> str | None:
    message = _extract_message(response)
    if message is None:
        return None
    return _get(message, "content")


def _extract_tool_calls(response: Any) -> list[tuple[str, str, dict[str, Any]]]:
    message = _extract_message(response)
    if message is None:
        return []
    raw_tool_calls = _get(message, "tool_calls") or []
    tool_calls: list[tuple[str, str, dict[str, Any]]] = []
    for raw in raw_tool_calls:
        function = _get(raw, "function") or {}
        name = _get(function, "name")
        if not name:
            continue
        tool_call_id = _get(raw, "id") or f"call_{len(tool_calls)}"
        arguments = _get(function, "arguments")
        if isinstance(arguments, str):
            try:
                parsed_args = json.loads(arguments) if arguments else {}
            except json.JSONDecodeError:
                parsed_args = {}
        elif isinstance(arguments, dict):
            parsed_args = arguments
        else:
            parsed_args = {}
        tool_calls.append((tool_call_id, name, parsed_args))
    return tool_calls


NO_TOOL_CALL_NUDGE = (
    "You did not call a tool. Call a browser tool to make progress, or call "
    "finish(status, reason, extracted_output) if the goal is complete. Emit a tool call now."
)


def _append_skipped_tool_results(
    messages: list[dict[str, Any]], remaining: list[tuple[str, str, dict[str, Any]]], reason: str
) -> None:
    """Answer tool_calls we stopped before executing, so every id in the assistant turn has a
    matching tool result. An unanswered tool_call is an invalid transcript for the next call."""
    for tool_call_id, tool_name, _args in remaining:
        messages.append(
            {"role": "tool", "tool_call_id": tool_call_id, "name": tool_name, "content": f"skipped: {reason}"}
        )


def make_finish_tool() -> ToolSpec:
    async def handler(args: dict[str, Any]) -> ToolResult:
        status = args.get("status")
        if status not in ("completed", "failed", "terminated"):
            return ToolResult.error(
                f"invalid finish status: {status!r}; call finish again with status=completed|failed|terminated"
            )
        return ToolResult.ok(
            content="Task attempt ended. No further actions are permitted.",
            data={
                "status": status,
                "reason": args.get("reason") or "",
                "extracted_output": args.get("extracted_output"),
            },
        )

    return ToolSpec(
        name="finish",
        description=(
            "End the task and report whether the browser goal was completed. Call this only when "
            "the goal is met (status=completed) or is impossible/blocked (failed/terminated)."
        ),
        parameters={
            "type": "object",
            "properties": {
                "status": {"type": "string", "enum": ["completed", "failed", "terminated"]},
                "reason": {"type": "string", "maxLength": 2000},
                "extracted_output": {"description": "Structured output requested by the goal, if any."},
            },
            "required": ["status", "reason"],
        },
        handler=handler,
        terminal=True,
    )


async def run_agent_tool_loop(
    *,
    llm_caller: Any,
    system_prompt: str,
    user_prompt: str,
    tools: list[ToolSpec],
    max_turns: int,
    max_tool_calls: int,
    prompt_name: str = "taskv3-agent-loop",
    organization_id: str | None = None,
    call_kwargs: dict[str, Any] | None = None,
    should_cancel: Callable[[], Awaitable[bool]] | None = None,
    max_tokens: int | None = None,
    deadline_seconds: float | None = None,
    retryable_call_exceptions: tuple[type[BaseException], ...] = (),
    max_call_retries: int = 0,
    call_retry_base_delay: float = 1.0,
) -> LoopOutcome:
    tool_by_name = {tool.name: tool for tool in tools}
    openai_tools = [tool.to_openai_tool() for tool in tools]

    # We own the message array and assign it to the caller's message_history before
    # each call, passing prompt=None: LLMCaller.use_message_history never appends the
    # assistant reply or tool results itself, so multi-turn tool use must be threaded here.
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    outcome: LoopOutcome | None = None
    turns = 0
    total_tool_calls = 0
    total_tokens = 0
    billable_actions: list[str] = []
    started_at = time.monotonic()

    while outcome is None:
        if should_cancel is not None and await should_cancel():
            outcome = LoopOutcome("canceled", "run canceled")
            break
        if deadline_seconds is not None and time.monotonic() - started_at > deadline_seconds:
            outcome = LoopOutcome("budget_exhausted", f"deadline ({deadline_seconds:.0f}s) reached")
            break
        if max_tokens is not None and total_tokens >= max_tokens:
            outcome = LoopOutcome("budget_exhausted", f"max_tokens ({max_tokens}) reached")
            break
        if turns >= max_turns:
            outcome = LoopOutcome("budget_exhausted", f"max_turns ({max_turns}) reached")
            break
        if total_tool_calls >= max_tool_calls:
            outcome = LoopOutcome("budget_exhausted", f"max_tool_calls ({max_tool_calls}) reached")
            break
        turns += 1

        llm_caller.message_history = list(messages)
        # Retry only the LLM call on transient provider errors. No browser tool has run this
        # turn, so re-issuing the same call is side-effect-free — unlike a whole-task retry,
        # which would re-execute prior clicks/types. This restores the step engine's transient
        # resilience, which v3 otherwise loses by running as one non-retried unit.
        response = None
        call_attempt = 0
        while True:
            try:
                response = await llm_caller.call(
                    prompt=None,
                    prompt_name=prompt_name,
                    organization_id=organization_id,
                    tools=openai_tools,
                    use_message_history=True,
                    raw_response=True,
                    **(call_kwargs or {}),
                )
                break
            except retryable_call_exceptions as exc:
                call_attempt += 1
                if call_attempt > max_call_retries:
                    LOG.warning(
                        "taskv3 loop LLM call failed after retries", turn=turns, attempts=call_attempt, exc_info=True
                    )
                    outcome = LoopOutcome("loop_error", f"llm_call_failed: {type(exc).__name__}: {exc}")
                    break
                LOG.info("taskv3 loop retrying transient LLM error", turn=turns, attempt=call_attempt)
                await asyncio.sleep(call_retry_base_delay * (2 ** (call_attempt - 1)))
            except Exception as exc:
                LOG.warning("taskv3 loop LLM call failed", turn=turns, exc_info=True)
                outcome = LoopOutcome("loop_error", f"llm_call_failed: {type(exc).__name__}: {exc}")
                break
        if outcome is not None:
            break

        usage = _get(response, "usage") or {}
        turn_tokens = _get(usage, "total_tokens")
        if not turn_tokens:
            turn_tokens = (_get(usage, "prompt_tokens") or 0) + (_get(usage, "completion_tokens") or 0)
        total_tokens += int(turn_tokens or 0)

        text = _extract_text(response)
        tool_calls = _extract_tool_calls(response)

        assistant_message: dict[str, Any] = {"role": "assistant", "content": text or None}
        if tool_calls:
            assistant_message["tool_calls"] = [
                {"id": tool_call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}
                for tool_call_id, name, args in tool_calls
            ]
        messages.append(assistant_message)

        if not tool_calls:
            messages.append({"role": "user", "content": NO_TOOL_CALL_NUDGE})
            continue

        for idx, (tool_call_id, tool_name, args) in enumerate(tool_calls):
            # Enforce the cap per tool call so one batched turn cannot overrun it, and honor a
            # cancellation that arrives mid-batch before the next click/type/submit runs. Neither
            # this call nor the rest of the batch executes, so answer them as skipped.
            if total_tool_calls >= max_tool_calls:
                outcome = LoopOutcome("budget_exhausted", f"max_tool_calls ({max_tool_calls}) reached")
                _append_skipped_tool_results(messages, tool_calls[idx:], "tool-call budget reached")
                break
            if should_cancel is not None and await should_cancel():
                outcome = LoopOutcome("canceled", "run canceled")
                _append_skipped_tool_results(messages, tool_calls[idx:], "run canceled")
                break
            total_tool_calls += 1
            spec = tool_by_name.get(tool_name)
            if spec is None:
                result = ToolResult.error(f"unknown_tool: {tool_name}")
            else:
                try:
                    result = await spec.handler(args)
                except Exception as exc:
                    LOG.warning("taskv3 tool handler raised", tool=tool_name, exc_info=True)
                    result = ToolResult.error(f"tool_error: {type(exc).__name__}: {exc}")

            messages.append(
                {"role": "tool", "tool_call_id": tool_call_id, "name": tool_name, "content": result.content}
            )
            if spec is not None and spec.billable and result.status == "ok":
                billable_actions.append(tool_name)

            if spec is not None and spec.terminal and result.status == "ok":
                data = result.data or {}
                outcome = LoopOutcome(
                    status=data.get("status", "completed"),
                    reason=data.get("reason", ""),
                    extracted_output=data.get("extracted_output"),
                )
                break

            if result.status == "error":
                # A failed call can leave the page in a state the rest of this batch was not planned
                # against (e.g. a write after a failed navigate). Stop and skip the remaining calls so
                # the model re-plans next turn from the error rather than acting on a stale assumption.
                _append_skipped_tool_results(messages, tool_calls[idx + 1 :], "earlier tool call in this batch failed")
                break

    if outcome is None:
        outcome = LoopOutcome("loop_error", "loop exited without an outcome")

    outcome.turns = turns
    outcome.tool_calls = total_tool_calls
    outcome.billable_actions = billable_actions
    outcome.messages = messages
    return outcome
