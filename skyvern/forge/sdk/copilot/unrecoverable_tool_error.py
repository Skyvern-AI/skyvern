"""Unrecoverable browser-session tool-error detection, shared by enforcement and the stream adapter."""

import re
from typing import Any

import structlog

from skyvern.forge.sdk.copilot.diagnosis_repair_contract import build_diagnosis_repair_contract
from skyvern.forge.sdk.copilot.tracing_setup import copilot_span

LOG = structlog.get_logger()

UNRECOVERABLE_TOOL_ERROR_STOP_AT = 2


class CopilotUnrecoverableToolError(Exception):
    """Raised when browser-session tool failures prove the current loop cannot recover."""

    def __init__(self, tool_name: str, error_message: str) -> None:
        self.tool_name = tool_name
        self.error_message = error_message
        super().__init__(f"Unrecoverable tool error in {tool_name}: {error_message}")


_BROWSER_SESSION_TOOL_NAMES = frozenset(
    {
        "navigate_browser",
        "get_browser_screenshot",
        "evaluate",
        "click",
        "type_text",
        "scroll",
        "console_messages",
        "select_option",
        "press_key",
    }
)
_UNRECOVERABLE_TOOL_ERROR_CATEGORY = "UNRECOVERABLE_TOOL_ERROR"
_BROWSER_SESSION_ID_RE = re.compile(r"\bpbs_[A-Za-z0-9_-]+\b")
_BROWSER_SESSION_WITH_ID_RE = re.compile(r"\bbrowser session\s+pbs_[A-Za-z0-9_-]+\b", re.IGNORECASE)


def redact_browser_session_references(value: str) -> str:
    value = _BROWSER_SESSION_WITH_ID_RE.sub("Browser session", value)
    return _BROWSER_SESSION_ID_RE.sub("the browser session", value)


def _result_text_values(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        result: list[str] = []
        for item in value.values():
            result.extend(_result_text_values(item))
        return result
    if isinstance(value, list):
        result = []
        for item in value:
            result.extend(_result_text_values(item))
        return result
    return []


def _unrecoverable_tool_error_reason(output: dict[str, Any]) -> str:
    raw_reason = output.get("error")
    if not isinstance(raw_reason, str) or not raw_reason.strip():
        data = output.get("data")
        raw_reason = data.get("failure_reason") if isinstance(data, dict) else None
    if not isinstance(raw_reason, str) or not raw_reason.strip():
        raw_reason = " ".join(_result_text_values(output))
    reason = " ".join(str(raw_reason or "Browser session was no longer reachable.").split())
    reason = redact_browser_session_references(reason)
    return reason[:240].rstrip()


def _is_unrecoverable_browser_session_error(tool_name: str, output: dict[str, Any]) -> bool:
    if tool_name not in _BROWSER_SESSION_TOOL_NAMES or output.get("ok", True):
        return False
    lowered = " ".join(_result_text_values(output)).lower()
    if "no browser context" in lowered:
        return True
    has_session_signal = "browser session" in lowered or "browser context" in lowered
    has_lost_signal = "not found" in lowered or "404" in lowered
    return has_session_signal and has_lost_signal


def _record_unrecoverable_tool_error_contract(ctx: Any, tool_name: str, reason: str) -> None:
    result = {
        "ok": False,
        "error": reason,
        "data": {
            "overall_status": "aborted",
            "failure_reason": reason,
            "failure_categories": [{"category": _UNRECOVERABLE_TOOL_ERROR_CATEGORY, "reasoning": reason}],
        },
    }
    contract = build_diagnosis_repair_contract(source_tool=tool_name, result=result, ctx=ctx)
    ctx.latest_diagnosis_repair_contract = contract
    ctx.unrecoverable_tool_error_reason = reason
    ctx.unrecoverable_tool_error_tool_name = tool_name
    ctx.last_test_failure_reason = reason
    trace_data = contract.to_trace_data()
    LOG.warning(
        "Copilot unrecoverable tool error stop",
        tool_name=tool_name,
        error_reason=reason,
        **{f"diagnosis_repair_{key}": value for key, value in trace_data.items()},
    )
    with copilot_span("copilot_unrecoverable_tool_error", data={"tool_name": tool_name, **trace_data}):
        pass


def _maybe_raise_unrecoverable_tool_error(ctx: Any, tool_name: str, output: dict[str, Any]) -> None:
    if not _is_unrecoverable_browser_session_error(tool_name, output):
        if tool_name in _BROWSER_SESSION_TOOL_NAMES and output.get("ok", False):
            ctx.unrecoverable_tool_error_streak_count = 0
            ctx.unrecoverable_tool_error_signature = None
        return

    reason = _unrecoverable_tool_error_reason(output)
    signature = "browser_session_unreachable"
    prior_signature = getattr(ctx, "unrecoverable_tool_error_signature", None)
    prior_count = getattr(ctx, "unrecoverable_tool_error_streak_count", 0)
    prior_count = prior_count if isinstance(prior_count, int) else 0
    count = prior_count + 1 if prior_signature == signature else 1
    ctx.unrecoverable_tool_error_signature = signature
    ctx.unrecoverable_tool_error_streak_count = count
    ctx.unrecoverable_tool_error_reason = reason
    ctx.unrecoverable_tool_error_tool_name = tool_name

    if count >= UNRECOVERABLE_TOOL_ERROR_STOP_AT:
        _record_unrecoverable_tool_error_contract(ctx, tool_name, reason)
        raise CopilotUnrecoverableToolError(tool_name, reason)
