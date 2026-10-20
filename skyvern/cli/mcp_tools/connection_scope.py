from __future__ import annotations

from typing import Any

from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext

from skyvern.cli.core.session_manager import stateless_call_connection_scope


class MCPStatelessConnectionMiddleware(Middleware):
    async def on_call_tool(
        self,
        context: MiddlewareContext[Any],
        call_next: CallNext[Any, Any],
    ) -> Any:
        async with stateless_call_connection_scope():
            return await call_next(context)
