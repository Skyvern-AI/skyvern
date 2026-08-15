"""Native Task V3 engine: assemble prompts + raw-browser tools + the tool-loop.

This is the platform-agnostic core of the native (non-Bun) engine. Given a live
Playwright page and an ``LLMCaller``, it runs one persistent conversation that
perceives via ``observe`` and acts by selector until the model calls ``finish``.
Callers (the run/step dispatch) own browser acquisition, the concrete LLMCaller,
and mapping the returned ``LoopOutcome`` onto the task's status/output.

Perception defaults to the compact ``observe`` snapshot rather than raw HTML: on
real-world multi-field forms compact-observe matched raw-DOM on cost/latency with a
tighter tail (raw-DOM occasionally spiraled re-reading the page), at equal success.

Why raw browser tools and not the MCP: this engine runs in-process and already owns the run's
Playwright page, whereas the MCP is a separate-process transport. The MCP's ``skyvern_observe``
and ``skyvern_execute`` are deterministic in-process primitives (``do_observe``/``do_execute`` in
``skyvern/cli/core/browser_ops.py``); its ``skyvern_act``/``skyvern_extract``/``skyvern_run_task``
are LLM-backed (the task/prompt ecosystem) — the per-step cost this persistent-conversation design
removes. The tools here are thin raw-DOM/selector ops. A fast-follow is to evaluate a small
in-process adapter over ``do_observe``/``do_execute`` for shared hardening + action instrumentation.
"""

from __future__ import annotations

import json
from typing import Any, Awaitable, Callable

import structlog

from skyvern.forge.sdk.api.llm.exceptions import LLMProviderErrorRetryableTask
from skyvern.forge.taskv3.loop import LoopOutcome, make_finish_tool, run_agent_tool_loop
from skyvern.forge.taskv3.tools import build_browser_tools

LOG = structlog.get_logger()

DEFAULT_MAX_TURNS = 80
DEFAULT_MAX_TOOL_CALLS = 300
DEFAULT_DEADLINE_SECONDS = 1800  # wall-clock runaway guard
# Backstop against a spiral re-reading the page every turn (full history is re-sent each call): a
# runaway trips this as budget_exhausted instead of surfacing as a provider context-window error.
DEFAULT_MAX_TOKENS = 1_500_000
DEFAULT_MAX_CALL_RETRIES = 2

SYSTEM_PROMPT = """You are an autonomous web agent completing a browser task. You drive the browser ONLY through the provided tools; nothing about the page is shown to you unless you call a tool.

How to work:
- Perceive with `observe`: it returns the page's visible interactive elements, each with a CSS selector, label, type, current value, and (for selects) options. Call it once per page state and act from that snapshot; re-observe only after the page changes.
- Act by CSS selector: `type`, `select_option`, `click`, `press_key`, `scroll`, `wait`, `navigate`, `file_upload`.
- Be efficient — this is the whole point of the engine. After observing a form once, fill every field you can before doing anything that reloads the page. Minimize tool calls and turns.
- Batch aggressively: in ONE turn you can `type` into many fields AND `click` many radio/checkbox options AND `select_option` on several dropdowns. Answer a whole form section in a single turn — never spend a separate turn on each click.
- `observe` already gives you everything you need to fill a field (selector, label, type, current value, options, and the surrounding question text) — act on it directly. `get_html` is a rare last resort for ONE specific element `observe` failed to describe: NEVER call it on a whole page/form/section, NEVER call it twice for the same element, and NEVER inspect more than once before acting.
- Inspecting the page does NOT progress the task — only `type`/`select_option`/`click` do. If your recent turns were mostly `observe`/`get_html` with little typing or clicking, you are stuck inspecting: stop, and fill every field you can from the latest `observe` snapshot using its selectors before doing anything else.
- Before calling finish with status=completed, re-check with `observe` that every required field holds its intended value and the only remaining step is the final submit; fix anything missing first. Call `finish(status, reason, extracted_output)` when the goal is achieved (completed) or impossible/blocked (failed/terminated).

Rules:
- Fill fields from the task's data and satisfy required fields rather than failing over a missing value: prefer the provided values, and for an ordinary required field with no exact value, enter the most reasonable value you can. Do not invent sensitive or identifying values (government IDs, financial details, or legal/eligibility attestations); if one of those is required and not provided, stop and report it rather than guessing. Leave optional fields blank when you have no basis to fill them.
- Do not submit forms or take irreversible actions unless the goal explicitly instructs it."""


def _build_user_prompt(goal: str, parameters: dict[str, Any] | None, starting_url: str | None) -> str:
    parts = [goal.strip()]
    if starting_url:
        parts.append(f"\nYou start on: {starting_url}")
    if parameters:
        parts.append("\nData provided for this task:\n" + json.dumps(parameters, indent=2, default=str))
    return "\n".join(parts)


async def run_task_v3_agent_loop(
    *,
    page: Any,
    llm_caller: Any,
    goal: str,
    parameters: dict[str, Any] | None = None,
    starting_url: str | None = None,
    downloads_dir: str | None = None,
    organization_id: str | None = None,
    max_turns: int = DEFAULT_MAX_TURNS,
    max_tool_calls: int = DEFAULT_MAX_TOOL_CALLS,
    prompt_name: str = "taskv3-agent-loop",
    step: Any = None,
    should_cancel: Callable[[], Awaitable[bool]] | None = None,
    max_tokens: int | None = DEFAULT_MAX_TOKENS,
    deadline_seconds: float | None = DEFAULT_DEADLINE_SECONDS,
) -> LoopOutcome:
    """Run one Task V3 task to completion against `page`, returning the loop outcome.

    `step` is threaded into every LLMCaller.call so the run's cost/tokens/model and LLM
    artifacts attribute to it. `should_cancel` is polled between turns and mid-batch; token
    and wall-clock budgets bound cost beyond the turn/tool-call caps. The pre-finish
    re-verification is guidance in the system prompt, not a forced extra model turn."""
    tools = build_browser_tools(page, downloads_dir=downloads_dir, organization_id=organization_id) + [
        make_finish_tool()
    ]
    outcome = await run_agent_tool_loop(
        llm_caller=llm_caller,
        system_prompt=SYSTEM_PROMPT,
        user_prompt=_build_user_prompt(goal, parameters, starting_url),
        tools=tools,
        max_turns=max_turns,
        max_tool_calls=max_tool_calls,
        prompt_name=prompt_name,
        organization_id=organization_id,
        call_kwargs={"step": step} if step is not None else None,
        should_cancel=should_cancel,
        max_tokens=max_tokens,
        deadline_seconds=deadline_seconds,
        retryable_call_exceptions=(LLMProviderErrorRetryableTask,),
        max_call_retries=DEFAULT_MAX_CALL_RETRIES,
    )
    LOG.info(
        "taskv3 engine loop finished",
        status=outcome.status,
        turns=outcome.turns,
        tool_calls=outcome.tool_calls,
    )
    return outcome
