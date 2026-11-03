from __future__ import annotations

import contextlib
import json
from collections.abc import Awaitable, Callable, Iterable
from typing import Any

from agents import FunctionTool
from agents.exceptions import ModelBehaviorError
from agents.function_schema import function_schema
from agents.run_context import RunContextWrapper
from agents.tool_context import ToolContext
from pydantic import BaseModel, ValidationError

from skyvern.forge.sdk.copilot.context import CopilotContext
from skyvern.forge.sdk.copilot.runtime import RunSecretsNotRegistered
from skyvern.forge.sdk.copilot.secret_scrub import scrub_secrets_from_structure

_ARGUMENT_MODELS: dict[str, type[BaseModel]] = {}


def register_argument_models(tools: Iterable[FunctionTool]) -> None:
    """The SDK drops the JSON or pydantic error behind "Invalid JSON input for tool X" unless tool-data
    logging is on, so ``copilot_tool_failure`` rebuilds it from the raw arguments with the same signature model."""
    for tool in tools:
        with contextlib.suppress(AttributeError):
            _ARGUMENT_MODELS[tool.name] = function_schema(
                tool.__wrapped__, strict_json_schema=False
            ).params_pydantic_model


def _argument_error(ctx: RunContextWrapper[CopilotContext]) -> Exception | None:
    if not isinstance(ctx, ToolContext):
        return None
    try:
        arguments = json.loads(ctx.tool_arguments or "{}")
    except json.JSONDecodeError as exc:
        return exc
    model = _ARGUMENT_MODELS.get(ctx.tool_name)
    if model is None or not isinstance(arguments, dict):
        return None
    try:
        model(**arguments)
    except ValidationError as exc:
        return exc
    return None


def copilot_tool_failure(ctx: RunContextWrapper[CopilotContext], error: Exception) -> str:
    cause = error.__cause__ if isinstance(error, ModelBehaviorError) else error
    if cause is None:
        cause = _argument_error(ctx)
    result: dict[str, Any] = {"ok": False}
    if isinstance(cause, ValidationError):
        errors = cause.errors(include_input=False, include_context=False, include_url=False)
        details = "; ".join(f"{'.'.join(map(str, item['loc']))}: {item['msg']}" for item in errors)
        result.update(error=f"Invalid tool arguments: {details}", data={"validation_errors": errors})
    elif isinstance(cause, json.JSONDecodeError):
        result["error"] = f"Invalid JSON tool arguments: {cause.msg}"
    else:
        result["error"] = str(error)
    return json.dumps(scrub_secrets_from_structure(ctx.context, result))


def reporting_withheld_browser(
    invoke: Callable[[RunContextWrapper[CopilotContext], str], Awaitable[str]],
) -> Callable[[RunContextWrapper[CopilotContext], str], Awaitable[str]]:
    """A hand-built ``FunctionTool`` has no ``failure_error_function``, so without this a withheld
    browser would end the turn instead of returning its reason."""

    async def invoke_or_report(ctx: RunContextWrapper[CopilotContext], arguments: str) -> str:
        try:
            return await invoke(ctx, arguments)
        except RunSecretsNotRegistered as exc:
            return copilot_tool_failure(ctx, exc)

    return invoke_or_report
