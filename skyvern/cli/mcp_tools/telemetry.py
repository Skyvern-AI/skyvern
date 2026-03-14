from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass
from typing import Any, Literal

from fastmcp.server.dependencies import get_http_request
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext

from skyvern import analytics

MCPServerMode = Literal["embedded", "local_cli", "cloud_hosted"]


@dataclass(frozen=True)
class MCPRuntimeConfig:
    server_mode: MCPServerMode = "embedded"
    transport: str | None = None


_runtime_config = MCPRuntimeConfig()


def configure_mcp_telemetry_runtime(server_mode: MCPServerMode, transport: str | None) -> None:
    global _runtime_config
    _runtime_config = MCPRuntimeConfig(server_mode=server_mode, transport=transport)


def reset_mcp_telemetry_runtime() -> None:
    configure_mcp_telemetry_runtime(server_mode="embedded", transport=None)


def _resolve_http_request() -> Any | None:
    try:
        return get_http_request()
    except RuntimeError:
        return None


def _resolve_organization_id(request: Any | None) -> str | None:
    if request is None:
        return None
    return getattr(getattr(request, "state", None), "organization_id", None)


def _resolve_distinct_id(organization_id: str | None) -> tuple[str, str]:
    if organization_id:
        return f"org:{organization_id}", "organization_id"
    return analytics.settings.ANALYTICS_ID, "analytics_id"


def _resolve_request_id(context: MiddlewareContext[Any]) -> str | None:
    fastmcp_context = context.fastmcp_context
    if fastmcp_context is None:
        return None
    with suppress(RuntimeError):
        return fastmcp_context.request_id
    return None


def _resolve_session_id(context: MiddlewareContext[Any]) -> str | None:
    fastmcp_context = context.fastmcp_context
    if fastmcp_context is None:
        return None
    with suppress(RuntimeError):
        return fastmcp_context.session_id
    return None


def _resolve_client_id(context: MiddlewareContext[Any]) -> str | None:
    fastmcp_context = context.fastmcp_context
    if fastmcp_context is None:
        return None
    with suppress(RuntimeError):
        return fastmcp_context.client_id
    return None


def _capture_mcp_event(
    event_name: str,
    *,
    operation: str,
    context: MiddlewareContext[Any],
    ok: bool,
    tool_name: str | None = None,
    prompt_name: str | None = None,
    error: Exception | None = None,
) -> None:
    request = _resolve_http_request()
    organization_id = _resolve_organization_id(request)
    distinct_id, distinct_id_source = _resolve_distinct_id(organization_id)

    data: dict[str, Any] = {
        **analytics.analytics_metadata(),
        "operation": operation,
        "ok": ok,
        "runtime_mode": _runtime_config.server_mode,
        "transport": _runtime_config.transport,
        "is_http": request is not None,
        "request_path": str(request.url.path) if request is not None else None,
        "request_method": str(request.method) if request is not None else None,
        "organization_id": organization_id,
        "distinct_id_source": distinct_id_source,
        "request_id": _resolve_request_id(context),
        "session_id": _resolve_session_id(context),
        "client_id": _resolve_client_id(context),
    }

    if tool_name is not None:
        data["tool"] = tool_name
    if prompt_name is not None:
        data["prompt"] = prompt_name
    if error is not None:
        data["error_type"] = type(error).__name__

    analytics.capture(
        event_name,
        data=data,
        distinct_id=distinct_id,
        api_key=analytics.settings.MCP_POSTHOG_PROJECT_API_KEY,
        host=analytics.settings.MCP_POSTHOG_PROJECT_HOST,
    )


def _resolve_tool_call_ok(result: Any) -> bool:
    is_error = getattr(result, "is_error", None)
    if not isinstance(is_error, bool):
        is_error = False

    data = getattr(result, "data", None)
    result_ok = None
    if isinstance(data, dict):
        candidate = data.get("ok")
        if isinstance(candidate, bool):
            result_ok = candidate

    if result_ok is None:
        return not is_error
    return (not is_error) and result_ok


class MCPTelemetryMiddleware(Middleware):
    async def on_initialize(
        self,
        context: MiddlewareContext[Any],
        call_next: CallNext[Any, Any],
    ) -> Any:
        try:
            result = await call_next(context)
        except Exception as exc:
            _capture_mcp_event("mcp_request", operation="initialize", context=context, ok=False, error=exc)
            raise

        _capture_mcp_event("mcp_request", operation="initialize", context=context, ok=True)
        return result

    async def on_call_tool(
        self,
        context: MiddlewareContext[Any],
        call_next: CallNext[Any, Any],
    ) -> Any:
        tool_name = getattr(context.message, "name", None)
        try:
            result = await call_next(context)
        except Exception as exc:
            _capture_mcp_event(
                "mcp_tool_call",
                operation="tools/call",
                context=context,
                ok=False,
                tool_name=tool_name,
                error=exc,
            )
            raise

        _capture_mcp_event(
            "mcp_tool_call",
            operation="tools/call",
            context=context,
            ok=_resolve_tool_call_ok(result),
            tool_name=tool_name,
        )
        return result

    async def on_list_tools(
        self,
        context: MiddlewareContext[Any],
        call_next: CallNext[Any, Any],
    ) -> Any:
        try:
            result = await call_next(context)
        except Exception as exc:
            _capture_mcp_event("mcp_request", operation="tools/list", context=context, ok=False, error=exc)
            raise

        _capture_mcp_event("mcp_request", operation="tools/list", context=context, ok=True)
        return result

    async def on_get_prompt(
        self,
        context: MiddlewareContext[Any],
        call_next: CallNext[Any, Any],
    ) -> Any:
        prompt_name = getattr(context.message, "name", None)
        try:
            result = await call_next(context)
        except Exception as exc:
            _capture_mcp_event(
                "mcp_request",
                operation="prompts/get",
                context=context,
                ok=False,
                prompt_name=prompt_name,
                error=exc,
            )
            raise

        _capture_mcp_event(
            "mcp_request",
            operation="prompts/get",
            context=context,
            ok=True,
            prompt_name=prompt_name,
        )
        return result

    async def on_list_prompts(
        self,
        context: MiddlewareContext[Any],
        call_next: CallNext[Any, Any],
    ) -> Any:
        try:
            result = await call_next(context)
        except Exception as exc:
            _capture_mcp_event("mcp_request", operation="prompts/list", context=context, ok=False, error=exc)
            raise

        _capture_mcp_event("mcp_request", operation="prompts/list", context=context, ok=True)
        return result
