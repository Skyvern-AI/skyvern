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

from skyvern.exceptions import SkyvernContextWindowExceededError

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
    recordable: bool = False  # persisted as an action row (with screenshot) but not billed/budgeted
    compactable: bool = False  # a large perception result safe to elide from the transcript once superseded

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
    action_steps: int = 0
    # Turns where the model answered with prose instead of a tool call, costing a full round trip
    # plus the NO_TOOL_CALL_NUDGE recovery turn.
    no_tool_call_turns: int = 0
    # Whether tool_choice was still being sent when the run ended. Distinguishes a run that was
    # asked to force tool calls from one where the request was degraded away mid-run.
    tool_choice_in_effect: bool = False
    billable_actions: list[str] = field(default_factory=list)
    # Perception snapshots are compacted in place during the run, so superseded observe/get_html
    # content is already elided here — treat as lossy if ever persisted for audit.
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


def make_finish_tool(
    settle_probe: Callable[[], Awaitable[bool]] | None = None,
    max_settle_deferrals: int = 2,
) -> ToolSpec:
    """`settle_probe` returns True when the page has finished rendering. A finish(completed) on an
    unsettled page is deferred (bounded by `max_settle_deferrals`, then accepted) so the model
    re-verifies against the settled state instead of a mid-render shell — delayed loads otherwise
    produce stochastic false completions."""
    deferrals = 0

    async def handler(args: dict[str, Any]) -> ToolResult:
        nonlocal deferrals
        status = args.get("status")
        if status not in ("completed", "failed", "terminated"):
            return ToolResult.error(
                f"invalid finish status: {status!r}; call finish again with status=completed|failed|terminated"
            )
        if status == "completed" and settle_probe is not None and deferrals < max_settle_deferrals:
            try:
                settled = await settle_probe()
            except Exception:
                settled = True
            if not settled:
                deferrals += 1
                return ToolResult.error(
                    "the page was still rendering when you called finish. Wait for it to settle, "
                    "re-observe, confirm the goal's effect is present in the loaded content (not a "
                    "loading indicator or empty container), then finish again."
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


_COMPACTED_PREFIX = "[superseded "


def _compact_transcript(messages: list[dict[str, Any]], snapshot_indices: set[int]) -> None:
    """Bound the persistent conversation by eliding stale perception snapshots.

    The full transcript is re-sent every turn, so large perception outputs (an `observe` snapshot the
    agent has already acted past, or a 20k-char `get_html` dump) otherwise pile up until the token
    backstop trips on perception-heavy pages. `snapshot_indices` holds the message indices of the
    *successful* perception results (recorded as they are appended); keep the newest of each such tool
    and replace older ones' content with a short placeholder. Two things are deliberately protected:

    - The most-recent round (results after the last assistant message) is never touched — a single turn
      can batch several perception calls, and compaction runs *before* the model has seen that round's
      results, so eliding any of them would drop data the model requested but never read.
    - Only a successful snapshot is ever a candidate: a skip/error result is never recorded in
      `snapshot_indices`, so it can neither be elided nor shadow the real snapshot and leave the agent
      with no usable page view — regardless of content length (a verbose provider error included).

    Only a `tool` message's content is shrunk, never removed, so every tool_call keeps a matching result
    and the transcript stays valid. Eliding also drops the index, so re-running is a no-op and an elided
    placeholder can never re-anchor as the live snapshot."""
    if not snapshot_indices:
        return
    last_assistant_idx = -1
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get("role") == "assistant":
            last_assistant_idx = i
            break
    seen: set[str] = set()
    for i in sorted(snapshot_indices, reverse=True):
        name = messages[i]["name"]
        if i > last_assistant_idx or name not in seen:
            seen.add(name)  # the still-unread latest round, or the newest snapshot of this tool — keep
            continue
        messages[i]["content"] = f"{_COMPACTED_PREFIX}{name} output elided to bound context]"
        snapshot_indices.discard(i)


async def run_agent_tool_loop(
    *,
    llm_caller: Any,
    system_prompt: str,
    user_prompt: str,
    tools: list[ToolSpec],
    max_turns: int,
    max_tool_calls: int,
    max_action_steps: int | None = None,
    prompt_name: str = "taskv3-agent-loop",
    organization_id: str | None = None,
    call_kwargs: dict[str, Any] | None = None,
    should_cancel: Callable[[], Awaitable[bool]] | None = None,
    on_action_round: Callable[[list[tuple[str, dict[str, Any], bool]]], Awaitable[None]] | None = None,
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
    # Indices into `messages` of successful perception results, recorded as they are appended so
    # compaction can keep only the newest of each without inferring "real snapshot" from content size.
    snapshot_indices: set[int] = set()

    outcome: LoopOutcome | None = None
    # Mutable for the run: a provider that rejects tool_choice rejects it every turn, so a drop
    # made once must stick.
    active_call_kwargs = dict(call_kwargs or {})

    def _degrade_tool_choice(exc: BaseException) -> bool:
        """Drop tool_choice and report whether the turn is worth re-issuing.

        Called only when the turn is otherwise about to end the run, so the cost is one extra call
        on a run that was already failing. A context-window overflow is excluded because dropping a
        parameter provably cannot fix it.
        """
        if isinstance(exc, SkyvernContextWindowExceededError):
            return False
        if active_call_kwargs.pop("tool_choice", None) is None:
            return False
        LOG.warning("taskv3 loop retrying without tool_choice", turn=turns, exc_info=True)
        return True

    turns = 0
    no_tool_call_turns = 0
    total_tool_calls = 0
    total_tokens = 0
    billable_actions: list[str] = []
    action_steps = 0
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

        # Elide superseded perception results before re-sending the transcript, so a perception-heavy
        # run can't balloon the context to the token backstop (the pre-compaction runaway mode).
        _compact_transcript(messages, snapshot_indices)
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
                    **active_call_kwargs,
                )
                break
            except retryable_call_exceptions as exc:
                call_attempt += 1
                if call_attempt > max_call_retries:
                    # A provider rejecting the parameter surfaces here, not in the generic handler
                    # below: litellm's 400s subclass openai.APIError, which the LLM layer maps to
                    # the retryable type. Degrading only after the transient budget is spent keeps
                    # a passing blip from disabling the lever for the rest of the run.
                    if _degrade_tool_choice(exc):
                        # Spend the transient budget once, not once per parameter set: the degraded
                        # turn gets a single shot, which is what "last resort" is worth.
                        call_attempt = max_call_retries
                        continue
                    LOG.warning(
                        "taskv3 loop LLM call failed after retries", turn=turns, attempts=call_attempt, exc_info=True
                    )
                    outcome = LoopOutcome("loop_error", f"llm_call_failed: {type(exc).__name__}: {exc}")
                    break
                LOG.info("taskv3 loop retrying transient LLM error", turn=turns, attempt=call_attempt)
                await asyncio.sleep(call_retry_base_delay * (2 ** (call_attempt - 1)))
            except Exception as exc:
                if _degrade_tool_choice(exc):
                    continue
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
            no_tool_call_turns += 1
            LOG.info("taskv3 loop turn produced no tool call", turn=turns)
            messages.append({"role": "user", "content": NO_TOOL_CALL_NUDGE})
            continue

        turn_did_action = False
        round_actions: list[tuple[str, dict[str, Any], bool]] = []
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
            spec = tool_by_name.get(tool_name)
            # Once the action-step budget is spent, refuse a further page action — terminate, mirroring
            # the step engine's max-steps stop — but let perception/finish through, since the cap bounds
            # new action rounds, not the separate re-observe/finish turn the system prompt asks for.
            if spec is not None and spec.billable and max_action_steps is not None and action_steps >= max_action_steps:
                outcome = LoopOutcome("budget_exhausted", f"Reached the maximum steps ({max_action_steps})")
                _append_skipped_tool_results(messages, tool_calls[idx:], "action-step budget reached")
                break
            total_tool_calls += 1
            if spec is None:
                result = ToolResult.error(f"unknown_tool: {tool_name}")
            else:
                if spec.billable:
                    # A dispatched page action consumes a step even if it errors (it may mutate before
                    # failing); billing below counts successes only.
                    turn_did_action = True
                try:
                    result = await spec.handler(args)
                except Exception as exc:
                    LOG.warning("taskv3 tool handler raised", tool=tool_name, exc_info=True)
                    result = ToolResult.error(f"tool_error: {type(exc).__name__}: {exc}")

            if spec is not None and spec.compactable and result.status == "ok":
                snapshot_indices.add(len(messages))  # index this successful snapshot will occupy, pre-append
            messages.append(
                {"role": "tool", "tool_call_id": tool_call_id, "name": tool_name, "content": result.content}
            )
            if spec is not None and (spec.billable or spec.recordable):
                # Dispatched page actions enter the round with their outcome: a failed billable round
                # still consumed budget and must persist (else later blocks undercount the run
                # budget); recordable tools persist for artifact parity without billing/budget.
                round_actions.append((tool_name, args, result.status == "ok"))
                if spec.billable and result.status == "ok":
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

        # A "step" is one action round: a turn that ran >=1 page-mutating action. Perception-only
        # turns (observe/get_html) don't consume the caller's step budget — the step engine bundles
        # perception into each step, so counting v3's perception rounds against the same budget
        # under-counts equivalent work.
        if turn_did_action:
            action_steps += 1
        # Hand the round's executed actions to the caller so it can persist per-action artifacts
        # (screenshot, DB rows) — kept out of this transport-agnostic core, like should_cancel. A
        # persistence hiccup must not abort an otherwise-good run, so failures are contained here.
        if round_actions and on_action_round is not None:
            try:
                await on_action_round(round_actions)
            except Exception:
                LOG.warning("taskv3 on_action_round callback failed", turn=turns, exc_info=True)

    if outcome is None:
        outcome = LoopOutcome("loop_error", "loop exited without an outcome")

    outcome.turns = turns
    outcome.no_tool_call_turns = no_tool_call_turns
    outcome.tool_choice_in_effect = "tool_choice" in active_call_kwargs
    outcome.tool_calls = total_tool_calls
    outcome.action_steps = action_steps
    outcome.billable_actions = billable_actions
    outcome.messages = messages
    return outcome
