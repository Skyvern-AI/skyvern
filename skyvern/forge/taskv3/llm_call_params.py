"""Per-call LLM parameters for the Task V3 loop: the kwargs handed to ``LLMCaller.call()``.

Separate from prompt assembly -- these shape HOW the model is called (tool_choice, reasoning
summary), never what it is told.
"""

from __future__ import annotations

from typing import Any

from skyvern.config import settings


def build_call_kwargs(step: Any, llm_caller: Any) -> dict[str, Any] | None:
    # Asking here rather than only letting the LLM layer drop it keeps the run's own telemetry
    # honest: a run that reports tool_choice in effect has to have actually sent it.
    call_kwargs: dict[str, Any] = {}
    if step is not None:
        call_kwargs["step"] = step
    if settings.TASK_V3_TOOL_CHOICE_REQUIRED and llm_caller.supports_tool_choice():
        call_kwargs["tool_choice"] = "required"
    reasoning_with_summary = reasoning_effort_with_summary(llm_caller)
    if reasoning_with_summary is not None:
        call_kwargs["reasoning_effort"] = reasoning_with_summary
    return call_kwargs or None


def reasoning_effort_with_summary(llm_caller: Any) -> dict[str, str] | None:
    """A call-level reasoning_effort override that adds a summary request, for Task V3 loop calls
    only: `{"effort": <the effort the config would otherwise send>, "summary": "auto"}`. Passed as
    an LLMCaller.call() kwarg, which is applied after (and so wins over) the config-derived
    parameters -- this never changes how hard the model reasons, only whether litellm's
    chat->responses bridge joins a readable summary into message.reasoning_content.

    Only gpt-5.6 models routed through that bridge accept the dict form; a plain dict sent to any
    other model 400s ("Unknown parameter: 'reasoning'" observed), so this is gated to those models
    and returns None otherwise -- surfacing the provider's reasoning summary where available, since
    a continuation turn's tool call often carries empty message.content and no summary either.
    """
    llm_config = getattr(llm_caller, "llm_config", None)
    if llm_config is None:
        return None
    effort = getattr(llm_config, "reasoning_effort", None)
    # The caller-level check also covers the raw-client dispatch branches (custom/BYO models),
    # which never bridge regardless of the model's name.
    bridge_check = getattr(llm_caller, "uses_openai_responses_bridge", None)
    if effort is None or bridge_check is None or not bridge_check():
        return None
    return {"effort": effort, "summary": "auto"}
