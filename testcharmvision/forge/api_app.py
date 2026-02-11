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

from testcharmvision.config import settings
from testcharmvision.exceptions import TestcharmvisionHTTPException
from testcharmvision.forge import app as forge_app
from testcharmvision.forge.forge_app_initializer import start_forge_app
from testcharmvision.forge.request_logging import log_raw_request_middleware
from testcharmvision.forge.sdk.core import testcharmvision_context
from testcharmvision.forge.sdk.core.testcharmvision_context import TestcharmvisionContext
from testcharmvision.forge.sdk.db.exceptions import NotFoundError
from testcharmvision.forge.sdk.routes import internal_auth
from testcharmvision.forge.sdk.routes.routers import base_router, legacy_base_router, legacy_v2_router

try:
    from cloud.observability.otel_setup import OTELSetup
except ImportError:
    OTELSetup = None  # type: ignore[assignment,misc]

LOG = structlog.get_logger()


class ExecutionDatePlugin(Plugin):
    key = "execution_date"

    async def process_request(self, request: Request | HTTPConnection) -> datetime:
        return datetime.now()


def custom_openapi(app: FastAPI) -> dict:
    if app.openapi_schema:
        return app.openapi_schema
    openapi_schema = get_openapi(
        title="Testcharmvision API",
        version="1.0.0",
        description="API for Testcharmvision",
        routes=app.routes,
    )
    openapi_schema["servers"] = [
        {"url": "https://api.testcharmvision.com", "x-fern-server-name": "Cloud"},
        {"url": "https://api-staging.testcharmvision.com", "x-fern-server-name": "Staging"},
        {"url": "http://localhost:8000", "x-fern-server-name": "Local"},
    ]
    app.openapi_schema = openapi_schema
    return app.openapi_schema


@asynccontextmanager
async def lifespan(fastapi_app: FastAPI) -> AsyncGenerator[None, Any]:
    """Lifespan context manager for FastAPI app startup and shutdown."""

    LOG.info("Server started")
    if forge_app.api_app_startup_event:
        LOG.info("Calling api app startup event")
        try:
            await forge_app.api_app_startup_event(fastapi_app)
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
    # CRITICAL: Initialize OTEL FIRST, before any other code runs
    # This must happen before start_forge_app() because that function
    # creates database connections. If we don't instrument the libraries
    # first, the DB spans won't be children of the HTTP request spans.
    if settings.OTEL_ENABLED and OTELSetup is not None:
        try:
            otel = OTELSetup.get_instance()
            otel.initialize_tracer_provider()
            LOG.info("OTEL tracer provider initialized before forge app creation")
        except Exception as e:
            LOG.warning("Failed to initialize OTEL tracer provider early", error=str(e))

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

    @fastapi_app.exception_handler(TestcharmvisionHTTPException)
    async def handle_testcharmvision_http_exception(request: Request, exc: TestcharmvisionHTTPException) -> JSONResponse:
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
        curr_ctx = testcharmvision_context.current()
        if not curr_ctx:
            request_id = str(uuid.uuid4())
            testcharmvision_context.set(TestcharmvisionContext(request_id=request_id))
        elif not curr_ctx.request_id:
            curr_ctx.request_id = str(uuid.uuid4())

        try:
            return await call_next(request)
        finally:
            testcharmvision_context.reset()

    @fastapi_app.middleware("http")
    async def raw_request_logging(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        return await log_raw_request_middleware(request, call_next)

    if forge_app_instance.setup_api_app:
        forge_app_instance.setup_api_app(fastapi_app)

    return fastapi_app
