"""Native Task V3 engine: assemble prompts + raw-browser tools + the tool-loop.

This is the platform-agnostic core of the native (non-Bun) engine. Given a page provider
(resolved fresh on every tool call, not a page bound once) and an ``LLMCaller``, it runs
one persistent conversation that perceives via ``observe`` and acts by selector until the
model calls ``finish``. Callers (the run/step dispatch) own browser acquisition, the
concrete LLMCaller, and mapping the returned ``LoopOutcome`` onto the task's status/output.

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

from skyvern.config import settings
from skyvern.forge.sdk.api.llm.exceptions import LLMProviderErrorRetryableTask
from skyvern.forge.taskv3.loop import LoopOutcome, ToolSpec, make_finish_tool, run_agent_tool_loop
from skyvern.forge.taskv3.tools import PageProvider, build_browser_tools

LOG = structlog.get_logger()

DEFAULT_MAX_TURNS = 80
DEFAULT_MAX_TOOL_CALLS = 300
DEFAULT_DEADLINE_SECONDS = 1800  # wall-clock runaway guard
# Backstop against a spiral re-reading the page every turn (full history is re-sent each call): a
# runaway trips this as budget_exhausted instead of surfacing as a provider context-window error.
DEFAULT_MAX_TOKENS = 1_500_000
DEFAULT_MAX_CALL_RETRIES = 2
# A productive action round is at most a few perception turns plus one (possibly heavily batched)
# action turn. Size the turn/tool-call runaway guards off the action-step budget with generous
# per-round headroom so a real run is bounded by max_action_steps, not by these guards; only a run
# spending far more perception/batching per round than any form needs trips them.
MAX_TURNS_PER_ACTION_STEP = 6
MAX_TOOL_CALLS_PER_ACTION_STEP = 25
# A caller's step cap is tuned for the step engine (one step = a full observe-then-act cycle); the v3
# loop makes comparable progress per action round but is less round-efficient, so a low cap starves it
# before it can finish an ordinary multi-field form. Floor the action-step budget so a step-engine-tuned
# cap can't cut a productive run short; this only raises a low cap and never lowers a generous one.
MIN_ACTION_STEPS = 20

PAGE_FREE_SYSTEM_PROMPT = """You are completing a data-only assessment. You have NO browser tools: do not attempt to observe or interact with any page. Judge strictly from the goal, criteria, and data provided, then call `finish(status, reason, extracted_output)` — status=completed when the completion criterion holds, status=terminated when the termination criterion holds, status=failed only if the provided information is insufficient to decide."""

SYSTEM_PROMPT = """You are an autonomous web agent completing a browser task. You drive the browser ONLY through the provided tools; nothing about the page is shown to you unless you call a tool.

How to work:
- Perceive with `observe`: it returns the page's visible interactive elements, each with a CSS selector, label, type, current value, and (for selects) options. Call it once per page state and act from that snapshot; re-observe only after the page changes.
- Act by CSS selector: `type`, `select_option`, `select_combobox`, `click`, `press_key`, `scroll`, `wait`, `navigate`, `file_upload`.
- Be efficient — this is the whole point of the engine. After observing a form once, fill every field you can before doing anything that reloads the page. Minimize tool calls and turns.
- Batch aggressively: in ONE turn you can `type` into many fields AND `click` many radio/checkbox options AND `select_option` on several dropdowns. Answer a whole form section in a single turn — never spend a separate turn on each click.
- Autocomplete / typeahead / combobox fields (location, school, employer lookups) render suggestions only AFTER you type, and the raw text you type is NOT accepted until you pick a suggestion. Use the `select_combobox` tool (selector + value) for these — it types, waits for the suggestions to render, selects the best-matching one, and verifies the field committed. Do NOT `type` into them or press keys yourself. If `select_combobox` returns an error, the field is genuinely unfilled — try a fuller value or report it; never treat it as done.
- `observe` already gives you everything you need to fill a field (selector, label, type, current value, options, and the surrounding question text) — act on it directly. `get_html` is a rare last resort for ONE specific element `observe` failed to describe: NEVER call it on a whole page/form/section, NEVER call it twice for the same element, and NEVER inspect more than once before acting.
- Inspecting the page does NOT progress the task — only `type`/`select_option`/`click` do. If your recent turns were mostly `observe`/`get_html` with little typing or clicking, you are stuck inspecting: stop, and fill every field you can from the latest `observe` snapshot using its selectors before doing anything else.
- Before calling finish with status=completed, re-check with `observe` that the goal's effect is present in the page's SETTLED, loaded content (no loading indicators or empty panels standing in for it), that every required field holds its intended value, and that the only remaining step is the final submit; fix anything missing first. Call `finish(status, reason, extracted_output)` when the goal is achieved (completed) or impossible/blocked (failed/terminated).

Rules:
- Fill fields from the task's data and satisfy required fields rather than failing over a missing value: prefer the provided values, and for an ordinary required field with no exact value, enter the most reasonable value you can. Do not invent sensitive or identifying values (government IDs, financial details, or legal/eligibility attestations); if one of those is required and not provided, stop and report it rather than guessing. Leave optional fields blank when you have no basis to fill them.
- Do not submit forms or take irreversible actions unless the goal explicitly instructs it."""


def taskv3_runaway_backstops(max_action_steps: int | None) -> tuple[int, int]:
    """Return (max_turns, max_tool_calls) anti-runaway guards for an action-step budget.

    Generous enough that a productive run is bounded by max_action_steps, not by these guards; with
    no action-step budget, fall back to the engine's fixed defaults."""
    if not max_action_steps:
        return DEFAULT_MAX_TURNS, DEFAULT_MAX_TOOL_CALLS
    return (
        max(DEFAULT_MAX_TURNS, max_action_steps * MAX_TURNS_PER_ACTION_STEP),
        max(DEFAULT_MAX_TOOL_CALLS, max_action_steps * MAX_TOOL_CALLS_PER_ACTION_STEP),
    )


def coerce_v3_parameters(navigation_payload: dict[str, Any] | list[Any] | str | None) -> dict[str, Any] | None:
    """Normalize a task's ``navigation_payload`` into the dict injected into the loop's prompt.

    The step engine surfaces the payload to the model regardless of its stored type; this mirrors
    that so v3 sees the same data. A dict is used as-is; a JSON-encoded string is parsed (some
    callers double-encode the payload, which an ``isinstance(dict)`` check would silently drop,
    stripping the applicant profile); a parsed non-dict or a plain string is wrapped so its
    contents still reach the model. Returns None only when there is genuinely no payload.
    """
    if navigation_payload is None:
        return None
    if isinstance(navigation_payload, dict):
        return navigation_payload
    if isinstance(navigation_payload, str):
        value: Any = navigation_payload.strip()
        if not value:
            return None
        # Unwrap nested JSON-string layers (callers may single- OR double-encode the payload); a
        # single parse would leave a double-encoded object as an escaped blob the model can't read.
        for _ in range(3):
            if not isinstance(value, str):
                break
            try:
                value = json.loads(value)
            except (ValueError, TypeError):
                break
        if value is None:
            return None
        return value if isinstance(value, dict) else {"task_data": value}
    return {"task_data": navigation_payload}


def _build_user_prompt(goal: str, parameters: dict[str, Any] | None, starting_url: str | None) -> str:
    parts = [goal.strip()]
    if starting_url:
        parts.append(f"\nYou start on: {starting_url}")
    if parameters:
        parts.append("\nData provided for this task:\n" + json.dumps(parameters, indent=2, default=str))
    return "\n".join(parts)


def _build_call_kwargs(step: Any, llm_caller: Any) -> dict[str, Any] | None:
    # Asking here rather than only letting the LLM layer drop it keeps the run's own telemetry
    # honest: a run that reports tool_choice in effect has to have actually sent it.
    call_kwargs: dict[str, Any] = {}
    if step is not None:
        call_kwargs["step"] = step
    if settings.TASK_V3_TOOL_CHOICE_REQUIRED and llm_caller.supports_tool_choice():
        call_kwargs["tool_choice"] = "required"
    return call_kwargs or None


async def run_task_v3_agent_loop(
    *,
    page_provider: PageProvider,
    llm_caller: Any,
    goal: str,
    parameters: dict[str, Any] | None = None,
    starting_url: str | None = None,
    downloads_dir: str | None = None,
    organization_id: str | None = None,
    max_turns: int = DEFAULT_MAX_TURNS,
    max_tool_calls: int = DEFAULT_MAX_TOOL_CALLS,
    max_action_steps: int | None = None,
    prompt_name: str = "taskv3-agent-loop",
    step: Any = None,
    should_cancel: Callable[[], Awaitable[bool]] | None = None,
    on_action_round: Callable[[list[tuple[str, dict[str, Any], bool]]], Awaitable[None]] | None = None,
    extra_tools: list[ToolSpec] | None = None,
    extra_system_guidance: str = "",
    max_tokens: int | None = DEFAULT_MAX_TOKENS,
    deadline_seconds: float | None = DEFAULT_DEADLINE_SECONDS,
    resolve_typed_text: Callable[[str], Any] | None = None,
    page_free: bool = False,
    settle_probe: Callable[[], Awaitable[bool]] | None = None,
) -> LoopOutcome:
    """Run one Task V3 task to completion against `page`, returning the loop outcome.

    `step` is threaded into every LLMCaller.call so the run's cost/tokens/model and LLM
    artifacts attribute to it. `should_cancel` is polled between turns and mid-batch; token
    and wall-clock budgets bound cost beyond the turn/tool-call caps. When a
    `settle_probe` is provided, a finish(completed) on an unsettled page IS forced back for a
    bounded re-verification turn; without one, pre-finish re-verification is prompt guidance only."""
    # Page-free mode is structural, not advisory: no browser tools exist to call and the system
    # prompt never mentions perception, so a data-only validation cannot read the live DOM.
    browser_tools = (
        []
        if page_free
        else build_browser_tools(
            page_provider,
            downloads_dir=downloads_dir,
            organization_id=organization_id,
            resolve_typed_text=resolve_typed_text,
        )
    )

    # The probe is caller-built (browser semantics live with the dispatcher, e.g. peeking without
    # page recovery); page-free runs never probe.
    finish_tool = make_finish_tool(settle_probe=None if page_free else settle_probe)
    tools = browser_tools + (extra_tools or []) + [finish_tool]
    base_system_prompt = PAGE_FREE_SYSTEM_PROMPT if page_free else SYSTEM_PROMPT
    outcome = await run_agent_tool_loop(
        llm_caller=llm_caller,
        system_prompt=base_system_prompt + extra_system_guidance,
        user_prompt=_build_user_prompt(goal, parameters, starting_url),
        tools=tools,
        max_turns=max_turns,
        max_tool_calls=max_tool_calls,
        max_action_steps=max_action_steps,
        prompt_name=prompt_name,
        organization_id=organization_id,
        call_kwargs=_build_call_kwargs(step, llm_caller),
        should_cancel=should_cancel,
        on_action_round=on_action_round,
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
        action_steps=outcome.action_steps,
        no_tool_call_turns=outcome.no_tool_call_turns,
        tool_choice_requested=settings.TASK_V3_TOOL_CHOICE_REQUIRED,
        tool_choice_in_effect=outcome.tool_choice_in_effect,
    )
    return outcome
