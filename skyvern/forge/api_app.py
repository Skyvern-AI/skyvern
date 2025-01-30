import uuid
from datetime import datetime
from typing import Awaitable, Callable

import structlog
from fastapi import FastAPI, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from starlette.requests import HTTPConnection, Request
from starlette_context.middleware import RawContextMiddleware
from starlette_context.plugins.base import Plugin

from skyvern.config import settings
from skyvern.exceptions import SkyvernHTTPException
from skyvern.forge import app as forge_app
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.db.exceptions import NotFoundError
from skyvern.forge.sdk.routes.agent_protocol import base_router, v2_router
from skyvern.forge.sdk.routes.streaming import websocket_router
from skyvern.forge.sdk.routes.totp import totp_router

LOG = structlog.get_logger()


class ExecutionDatePlugin(Plugin):
    key = "execution_date"

    async def process_request(self, request: Request | HTTPConnection) -> datetime:
        return datetime.now()


def get_agent_app() -> FastAPI:
    """
    Start the agent server.
    """

    app = FastAPI()

    # Add CORS middleware
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.ALLOWED_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(base_router, prefix="/api/v1")
    app.include_router(v2_router, prefix="/api/v2")
    app.include_router(websocket_router, prefix="/api/v1/stream")
    app.include_router(totp_router, prefix="/api/v1/totp")

    app.add_middleware(
        RawContextMiddleware,
        plugins=(
            # TODO (suchintan): We should set these up
            ExecutionDatePlugin(),
            # RequestIdPlugin(),
            # UserAgentPlugin(),
        ),
    )

    @app.exception_handler(NotFoundError)
    async def handle_not_found_error(request: Request, exc: NotFoundError) -> Response:
        return Response(status_code=status.HTTP_404_NOT_FOUND)

    @app.exception_handler(SkyvernHTTPException)
    async def handle_skyvern_http_exception(request: Request, exc: SkyvernHTTPException) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.message})

    @app.exception_handler(ValidationError)
    async def handle_pydantic_validation_error(request: Request, exc: ValidationError) -> JSONResponse:
        return JSONResponse(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, content={"detail": str(exc)})

    @app.exception_handler(Exception)
    async def unexpected_exception(request: Request, exc: Exception) -> JSONResponse:
        LOG.exception("Unexpected error in agent server.", exc_info=exc)
        return JSONResponse(status_code=500, content={"error": f"Unexpected error: {exc}"})

    @app.middleware("http")
    async def request_middleware(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        curr_ctx = skyvern_context.current()
        if not curr_ctx:
            request_id = str(uuid.uuid4())
            skyvern_context.set(SkyvernContext(request_id=request_id))
        elif not curr_ctx.request_id:
            curr_ctx.request_id = str(uuid.uuid4())

        try:
            return await call_next(request)
        finally:
            skyvern_context.reset()

    if settings.ADDITIONAL_MODULES:
        for module in settings.ADDITIONAL_MODULES:
            LOG.info("Loading additional module to set up api app", module=module)
            __import__(module)
        LOG.info(
            "Additional modules loaded to set up api app",
            modules=settings.ADDITIONAL_MODULES,
        )

    if forge_app.setup_api_app:
        forge_app.setup_api_app(app)

    return app


app = get_agent_app()
