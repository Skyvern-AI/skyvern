"""Per-call LLM parameters for the Task V3 loop: the kwargs handed to ``LLMCaller.call()``.

Separate from prompt assembly -- these shape HOW the model is called (the reasoning
summary), never what it is told.
"""

from __future__ import annotations

import re
from typing import Any

from skyvern.schemas.llm import LLMRouterConfig

# The text litellm 1.89.7's router appends to the primary leg's exception when a fallback leg also failed.
_FALLBACK_ERROR_MARKER = "Error doing the fallback:"
_LITELLM_ERROR_CLASS = re.compile(r"litellm\.(\w+)")
_REJECTION_CLASSES = frozenset({"BadRequestError", "NotFoundError"})


def build_call_kwargs(step: Any, llm_caller: Any) -> dict[str, Any] | None:
    call_kwargs: dict[str, Any] = {}
    if step is not None:
        call_kwargs["step"] = step
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


def carry_reasoning_route(llm_caller: Any) -> str:
    """``carries`` when this caller's calls go through litellm's Responses bridge to OpenAI from a main group whose
    deployments are interchangeable, else why not. A fallback group that cannot read the reasoning fails the call, and
    the loop retries without it."""
    bridge_check = getattr(llm_caller, "uses_openai_responses_bridge", None)
    if bridge_check is None or not bridge_check():
        return "not_responses_bridge"
    llm_config = llm_caller.llm_config
    if not isinstance(llm_config, LLMRouterConfig):
        return "azure" if llm_config.model_name.startswith("azure/") else "carries"
    main = [d.litellm_params for d in llm_config.model_list if d.model_name == llm_config.main_model_group]
    if not main:
        return "no_main_group"
    if any(str(params.get("model") or "").startswith("azure/") for params in main):
        return "azure"
    # A reasoning reference resolves only where it was made; deployments whose parameters differ in any way may not be
    # the same place, so the main group carries only when they are identical.
    return "carries" if all(params == main[0] for params in main) else "main_group_differs"


def carries_reasoning(llm_caller: Any) -> bool:
    return carry_reasoning_route(llm_caller) == "carries"


def fallback_error_class(error: BaseException) -> str | None:
    """The class of the last fallback leg's error, which litellm's router records only in the text of the primary
    leg's exception it re-raises; None when no fallback leg ran."""
    _, marker, tail = str(error).partition(_FALLBACK_ERROR_MARKER)
    found = _LITELLM_ERROR_CLASS.search(tail) if marker else None
    return found.group(1) if found else None


def rejects_carried_reasoning(error: BaseException) -> bool:
    """Whether an error is a deterministic rejection (400 or 404) from the leg that served or from the last fallback
    leg, which a retry with the same carried reasoning would only repeat."""
    return (
        getattr(error, "status_code", None) in (400, 404)
        or type(error).__name__ in _REJECTION_CLASSES
        or fallback_error_class(error) in _REJECTION_CLASSES
    )
