from __future__ import annotations

from contextlib import suppress
from importlib.metadata import version
from typing import Annotated, Any, Literal

from pydantic import Field

from skyvern.cli.core.client import get_active_api_key
from skyvern.client.errors import NotFoundError

from ._common import ErrorCode, RawHTTPError, make_error, make_result, raw_http_post


def _feedback_context() -> dict[str, str]:
    from fastmcp.server.dependencies import get_context  # noqa: PLC0415

    from . import telemetry  # noqa: PLC0415

    context = {
        "client_name": "unknown",
        "client_version": "unknown",
        "runtime_mode": telemetry._runtime_config.server_mode,
        "skyvern_version": "unknown",
    }
    with suppress(Exception):
        client_info = get_context().session.client_params.clientInfo
        for key, value in (("client_name", client_info.name), ("client_version", client_info.version)):
            if isinstance(value, str) and value:
                context[key] = value[:200]
    with suppress(Exception):
        context["skyvern_version"] = version("skyvern")[:200]
    return context


async def skyvern_feedback(
    category: Annotated[
        Literal[
            "missing_capability", "tool_failure", "unexpected_behavior", "unclear_guidance", "user_frustration", "other"
        ],
        Field(description="The kind of Skyvern problem to report."),
    ],
    summary: Annotated[str, Field(description="One-sentence summary, 1-300 characters after trimming whitespace.")],
    details: Annotated[
        str | None, Field(description="Optional context, up to 4000 characters. Empty text is omitted.")
    ] = None,
    tool_name: Annotated[
        str | None, Field(description="The Skyvern tool or integration involved, up to 128 characters.")
    ] = None,
    severity: Annotated[
        Literal["low", "medium", "high"] | None, Field(description="Optional problem severity.")
    ] = None,
) -> dict[str, Any]:
    """Send a report to the team that operates the connected Skyvern server.

    Call once per distinct problem: a Skyvern tool failed or behaved unexpectedly and you could not work around it;
    the user needed a capability no Skyvern tool provides; tool guidance was unclear or wrong; or the user expressed
    frustration with Skyvern. Do not report routine recoverable errors, website problems unrelated to Skyvern, or
    questions to the user. Do not include passwords, API keys, tokens, or personal data. Known secret patterns are
    redacted before storage.
    """
    summary = summary.strip()
    if not summary:
        return make_result(
            "skyvern_feedback",
            ok=False,
            error=make_error(ErrorCode.INVALID_INPUT, "summary is empty. Write a one-sentence summary.", ""),
        )
    details = details or None
    for field, value, limit in (("summary", summary, 300), ("details", details, 4000), ("tool_name", tool_name, 128)):
        if value is not None and len(value) > limit:
            return make_result(
                "skyvern_feedback",
                ok=False,
                error=make_error(
                    ErrorCode.INVALID_INPUT,
                    f"{field} has {len(value)} characters. The limit is {limit}. Shorten it and call again.",
                    "",
                ),
            )

    unavailable_hint = "Do not retry. Tell the user the report could not be sent."
    if not get_active_api_key():
        return make_result(
            "skyvern_feedback",
            ok=False,
            error=make_error(
                "FEEDBACK_UNAVAILABLE",
                "Feedback needs a Skyvern server with an API key. This local in-memory mode does not keep reports.",
                unavailable_hint,
            ),
        )

    try:
        response = await raw_http_post(
            "v1/agent_feedback",
            json_body={
                "category": category,
                "summary": summary,
                "details": details,
                "tool_name": tool_name,
                "severity": severity,
                "context": _feedback_context(),
            },
        )
    except NotFoundError:
        error = make_error(
            "FEEDBACK_UNAVAILABLE",
            "This Skyvern server does not accept feedback reports. It may run an older version.",
            unavailable_hint,
        )
    except RawHTTPError as exc:
        if exc.status_code == 429 and isinstance(exc.detail, str):
            error = make_error("FEEDBACK_LIMIT_REACHED", exc.detail, "Do not retry this report now.")
        else:
            error = make_error(ErrorCode.API_ERROR, f"Feedback was not recorded: HTTP {exc.status_code}.", "")
    except Exception:
        error = make_error(
            ErrorCode.API_ERROR, "Feedback was not recorded: the Skyvern server could not be reached.", ""
        )
    else:
        if isinstance(response, dict) and response.get("agent_feedback_id"):
            return make_result(
                "skyvern_feedback",
                data={
                    "feedback_id": response["agent_feedback_id"],
                    "redacted": response.get("redacted", False),
                    "message": "Feedback recorded for the Skyvern team.",
                },
            )
        error = make_error(
            ErrorCode.API_ERROR, "Feedback was not recorded: the server response had no feedback id.", ""
        )
    return make_result("skyvern_feedback", ok=False, error=error)
