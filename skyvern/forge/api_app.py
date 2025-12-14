import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, AsyncGenerator, Awaitable, Callable

import structlog
from fastapi import FastAPI, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from starlette.requests import HTTPConnection, Request
from starlette_context.middleware import RawContextMiddleware
from starlette_context.plugins.base import Plugin

from skyvern.config import settings
from skyvern.exceptions import SkyvernHTTPException
from skyvern.forge import app as forge_app
from skyvern.forge.forge_app_initializer import start_forge_app
from skyvern.forge.request_logging import log_raw_request_middleware
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.db.exceptions import NotFoundError
from skyvern.forge.sdk.routes import internal_auth
from skyvern.forge.sdk.routes.routers import base_router, legacy_base_router, legacy_v2_router

LOG = structlog.get_logger()


class ExecutionDatePlugin(Plugin):
    key = "execution_date"

    async def process_request(self, request: Request | HTTPConnection) -> datetime:
        return datetime.now()


def custom_openapi(app: FastAPI) -> dict:
    if app.openapi_schema:
        return app.openapi_schema
    openapi_schema = get_openapi(
        title="Skyvern API",
        version="1.0.0",
        description="API for Skyvern",
        routes=app.routes,
    )
    openapi_schema["servers"] = [
        {"url": "https://api.skyvern.com", "x-fern-server-name": "Cloud"},
        {"url": "https://api-staging.skyvern.com", "x-fern-server-name": "Staging"},
        {"url": "http://localhost:8000", "x-fern-server-name": "Local"},
    ]
    app.openapi_schema = openapi_schema
    return app.openapi_schema


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncGenerator[None, Any]:
    """Lifespan context manager for FastAPI app startup and shutdown."""

    LOG.info("Server started")
    if forge_app.api_app_startup_event:
        LOG.info("Calling api app startup event")
        try:
            await forge_app.api_app_startup_event()
        except Exception:
            LOG.exception("Failed to execute api app startup event")
    yield
    if forge_app.api_app_shutdown_event:
        LOG.info("Calling api app shutdown event")
        try:
            await forge_app.api_app_shutdown_event()
        except Exception:
            LOG.exception("Failed to execute api app shutdown event")
    LOG.info("Server shutting down")


def create_api_app() -> FastAPI:
    """
    Start the agent server.
    """

    forge_app_instance = start_forge_app()

    fastapi_app = FastAPI(lifespan=lifespan)

    # Add CORS middleware
    fastapi_app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.ALLOWED_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    fastapi_app.include_router(base_router, prefix="/v1")
    fastapi_app.include_router(legacy_base_router, prefix="/api/v1")
    fastapi_app.include_router(legacy_v2_router, prefix="/api/v2")

    # local dev endpoints
    if settings.ENV == "local":
        fastapi_app.include_router(internal_auth.router, prefix="/v1")
        fastapi_app.include_router(internal_auth.router, prefix="/api/v1")
        fastapi_app.include_router(internal_auth.router, prefix="/api/v2")

    fastapi_app.openapi = lambda: custom_openapi(fastapi_app)

    fastapi_app.add_middleware(
        RawContextMiddleware,
        plugins=(
            # TODO (suchintan): We should set these up
            ExecutionDatePlugin(),
            # RequestIdPlugin(),
            # UserAgentPlugin(),
        ),
    )

    @fastapi_app.exception_handler(NotFoundError)
    async def handle_not_found_error(request: Request, exc: NotFoundError) -> Response:
        return Response(status_code=status.HTTP_404_NOT_FOUND)

    @fastapi_app.exception_handler(SkyvernHTTPException)
    async def handle_skyvern_http_exception(request: Request, exc: SkyvernHTTPException) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.message})

    @fastapi_app.exception_handler(ValidationError)
    async def handle_pydantic_validation_error(request: Request, exc: ValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={"detail": str(exc)},
        )

    @fastapi_app.exception_handler(Exception)
    async def unexpected_exception(request: Request, exc: Exception) -> JSONResponse:
        LOG.exception("Unexpected error in agent server.", exc_info=exc)
        return JSONResponse(status_code=500, content={"error": f"Unexpected error: {exc}"})

    @fastapi_app.middleware("http")
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

    @fastapi_app.middleware("http")
    async def raw_request_logging(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        return await log_raw_request_middleware(request, call_next)

    if forge_app_instance.setup_api_app:
        forge_app_instance.setup_api_app(fastapi_app)

    return fastapi_app
