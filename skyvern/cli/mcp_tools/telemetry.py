from __future__ import annotations

import time
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, Literal

import structlog
from fastmcp.server.dependencies import get_http_request
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext

from skyvern import analytics

LOG = structlog.get_logger(__name__)

MCPServerMode = Literal["embedded", "local_cli", "cloud_hosted"]

UNKNOWN_MCP_CLIENT = "unknown"

_MAX_CLIENT_INFO_CHARS = 200


def _sanitize_client_info_value(value: str) -> str:
    # Bound before escaping so a huge client-controlled string does not
    # allocate an arbitrarily large intermediate during `.replace()`.
    sanitized = value[: _MAX_CLIENT_INFO_CHARS * 2].replace("\r", "\\r").replace("\n", "\\n")
    if len(sanitized) <= _MAX_CLIENT_INFO_CHARS:
        return sanitized
    return f"{sanitized[:_MAX_CLIENT_INFO_CHARS]}... [truncated]"


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


def _client_info_fields(client_info: Any) -> tuple[str, str] | None:
    name = getattr(client_info, "name", None)
    if not isinstance(name, str) or not name:
        return None
    version = getattr(client_info, "version", None)
    if not isinstance(version, str) or not version:
        version = UNKNOWN_MCP_CLIENT
    return _sanitize_client_info_value(name), _sanitize_client_info_value(version)


def _resolve_client_info(context: MiddlewareContext[Any]) -> tuple[str, str]:
    """clientInfo from the initialize message, else the session's stored client params.

    Clients may omit or malform clientInfo — fall back to "unknown", never raise.
    """
    params = getattr(context.message, "params", None)
    fields = _client_info_fields(getattr(params, "clientInfo", None))
    if fields is None and context.fastmcp_context is not None:
        with suppress(RuntimeError):
            client_params = getattr(context.fastmcp_context.session, "client_params", None)
            fields = _client_info_fields(getattr(client_params, "clientInfo", None))
    return fields or (UNKNOWN_MCP_CLIENT, UNKNOWN_MCP_CLIENT)


def _content_text_bytes(content_block: Any) -> int:
    text = getattr(content_block, "text", None)
    if not isinstance(text, str):
        return 0
    return len(text.encode("utf-8"))


def _capture_mcp_event(
    event_name: str,
    *,
    operation: str,
    context: MiddlewareContext[Any],
    ok: bool,
    tool_name: str | None = None,
    prompt_name: str | None = None,
    error: Exception | None = None,
    duration_ms: float | None = None,
    response_bytes: int | None = None,
) -> None:
    request = _resolve_http_request()
    organization_id = _resolve_organization_id(request)
    distinct_id, distinct_id_source = _resolve_distinct_id(organization_id)
    client_name, client_version = _resolve_client_info(context)

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
        "client_name": client_name,
        "client_version": client_version,
    }

    if tool_name is not None:
        data["tool"] = tool_name
    if prompt_name is not None:
        data["prompt"] = prompt_name
    if error is not None:
        data["error_type"] = type(error).__name__
    if duration_ms is not None:
        data["duration_ms"] = duration_ms
    if response_bytes is not None:
        data["response_bytes"] = response_bytes

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

        with suppress(Exception):
            client_name, client_version = _resolve_client_info(context)
            LOG.info(
                "mcp_session_initialized",
                mcp_client_name=client_name,
                mcp_client_version=client_version,
                session_id=_resolve_session_id(context),
                runtime_mode=_runtime_config.server_mode,
                transport=_runtime_config.transport,
            )
        _capture_mcp_event("mcp_request", operation="initialize", context=context, ok=True)
        return result

    async def on_call_tool(
        self,
        context: MiddlewareContext[Any],
        call_next: CallNext[Any, Any],
    ) -> Any:
        tool_name = getattr(context.message, "name", None)
        start = time.perf_counter()
        try:
            result = await call_next(context)
        except Exception as exc:
            duration_ms = (time.perf_counter() - start) * 1000
            # Exceptions do not produce MCP content, so response_bytes is only emitted for returned results.
            with suppress(Exception):
                _capture_mcp_event(
                    "mcp_tool_call",
                    operation="tools/call",
                    context=context,
                    ok=False,
                    tool_name=tool_name,
                    error=exc,
                    duration_ms=duration_ms,
                )
            raise

        duration_ms = (time.perf_counter() - start) * 1000
        response_bytes = sum(_content_text_bytes(content) for content in (getattr(result, "content", None) or []))
        with suppress(Exception):
            _capture_mcp_event(
                "mcp_tool_call",
                operation="tools/call",
                context=context,
                ok=_resolve_tool_call_ok(result),
                tool_name=tool_name,
                duration_ms=duration_ms,
                response_bytes=response_bytes,
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
