from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

from agents.exceptions import ModelBehaviorError
from agents.run_context import RunContextWrapper
from pydantic import ValidationError

from skyvern.forge.sdk.copilot.context import CopilotContext
from skyvern.forge.sdk.copilot.runtime import RunSecretsNotRegistered
from skyvern.forge.sdk.copilot.secret_scrub import scrub_secrets_from_structure


def copilot_tool_failure(ctx: RunContextWrapper[CopilotContext], error: Exception) -> str:
    cause = error.__cause__ if isinstance(error, ModelBehaviorError) else error
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
