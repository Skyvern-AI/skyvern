import os
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

from skyvern.config import _ensure_sqlite_dir, settings
from skyvern.exceptions import SkyvernHTTPException
from skyvern.forge import app as forge_app
from skyvern.forge.forge_app_initializer import start_forge_app
from skyvern.forge.request_logging import log_raw_request_middleware
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.db.exceptions import NotFoundError
from skyvern.forge.sdk.db.models import Base
from skyvern.forge.sdk.routes import internal_auth
from skyvern.forge.sdk.routes.routers import base_router, legacy_base_router, legacy_v2_router
from skyvern.forge.sdk.services.local_org_auth_token_service import (
    ensure_local_api_key,
    ensure_local_org,
    fingerprint_token,
    regenerate_local_api_key,
)
from skyvern.services.cleanup_service import start_cleanup_scheduler, stop_cleanup_scheduler

LOG = structlog.get_logger()


def format_validation_errors(exc: ValidationError) -> str:
    """Format a Pydantic ValidationError into a human-readable string.

    Filters out uninformative path segments ('__root__', 'body') and joins
    multiple errors with '; '.
    """
    error_messages = []
    for error in exc.errors():
        loc = " -> ".join(str(part) for part in error["loc"] if part not in ("__root__", "body"))
        msg = error["msg"]
        if loc:
            error_messages.append(f"{loc}: {msg}")
        else:
            error_messages.append(msg)
    return (
        "; ".join(error_messages)
        if error_messages
        else "A validation error occurred. Please check your input and try again."
    )


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


async def _bootstrap_sqlite() -> None:
    """Auto-bootstrap SQLite on first server start.

    Creates tables, a local org, and an API key so that
    ``skyvern run server`` works out of the box with zero configuration.
    Idempotent: skips if the org already exists.
    """
    _ensure_sqlite_dir(settings.DATABASE_STRING)

    db = forge_app.DATABASE
    async with db.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # Preserve an existing API key if it's a real value (not the skeleton default).
    # settings.SKYVERN_API_KEY already incorporates env vars and .env via pydantic-settings.
    existing_key = settings.SKYVERN_API_KEY if settings.SKYVERN_API_KEY != "PLACEHOLDER" else None

    if existing_key:
        preserved = await ensure_local_api_key(existing_key)
        if preserved is not None:
            api_key, org_id = preserved
            LOG.info(
                "Existing SKYVERN_API_KEY detected — preserving env value and syncing it into the local SQLite DB.",
                organization_id=org_id,
                api_key_fingerprint=fingerprint_token(api_key),
            )
            return

        LOG.warning(
            "Existing SKYVERN_API_KEY could not be preserved for local SQLite bootstrap; generating a new local key.",
        )

    organization = await ensure_local_org()
    existing_token = await db.get_valid_org_auth_token(organization.organization_id, "api")
    if existing_token is not None:
        LOG.info("SQLite database already bootstrapped", organization_id=organization.organization_id)
        return

    api_key, org_id, backend_env, frontend_env = await regenerate_local_api_key()
    LOG.info(
        "SQLite bootstrap complete — local org and API key created",
        organization_id=org_id,
        api_key_fingerprint=fingerprint_token(api_key),
        env_file_written=backend_env,
    )


@asynccontextmanager
async def lifespan(fastapi_app: FastAPI) -> AsyncGenerator[None, Any]:
    """Lifespan context manager for FastAPI app startup and shutdown."""

    LOG.info("Server started")

    # Auto-bootstrap SQLite database on first server start.
    # Re-raise on failure — a server with no tables/org/API key is
    # useless and would produce confusing 401s on every request.
    if settings.is_sqlite():
        await _bootstrap_sqlite()

    if forge_app.api_app_startup_event:
        LOG.info("Calling api app startup event")
        try:
            await forge_app.api_app_startup_event(fastapi_app)
        except Exception:
            LOG.exception("Failed to execute api app startup event")

    # Close browser sessions left active by a previous process
    try:
        await forge_app.PERSISTENT_SESSIONS_MANAGER.cleanup_stale_sessions()
    except Exception:
        LOG.exception("Failed to clean up stale browser sessions")

    # Start cleanup scheduler if enabled
    cleanup_task = start_cleanup_scheduler()
    if cleanup_task:
        LOG.info("Cleanup scheduler started")

    # Start MCP sub-application lifespan if mounted. Starlette Mount does NOT
    # forward lifespan events to sub-apps, so we must enter the MCP app's
    # lifespan here. This initializes the streamable-http session manager's
    # task group which is required for handling MCP requests.
    mcp_app = getattr(fastapi_app.state, "mcp_starlette_app", None)
    if mcp_app:
        async with mcp_app.lifespan(mcp_app):
            LOG.info("MCP remote server lifespan started")
            yield
        LOG.info("MCP remote server lifespan stopped")
    else:
        yield

    # Stop cleanup scheduler
    await stop_cleanup_scheduler()

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
    # CRITICAL: Initialize OTEL FIRST, before any other code runs.
    # This must happen before start_forge_app() because that function
    # creates database connections. If we don't instrument the libraries
    # first, the DB spans won't be children of the HTTP request spans.
    # NOTE: The cloud import is lazy (not at module level) to avoid triggering
    # cloud/__init__.py side effects when skyvern is used in embedded mode.
    otel_setup_cls: Any = None
    if settings.OTEL_ENABLED:
        try:
            from cloud.observability.otel_setup import OTELSetup  # noqa: PLC0415

            otel_setup_cls = OTELSetup
        except ImportError:
            pass
    if otel_setup_cls is not None:
        try:
            otel = otel_setup_cls.get_instance()
            otel.initialize_tracer_provider()
            LOG.info("OTEL tracer provider initialized before forge app creation")
        except Exception as e:
            LOG.warning("Failed to initialize OTEL tracer provider early", error=str(e))

    forge_app_instance = start_forge_app()

    # Initialize Laminar tracing after ForgeApp so auto-instrumentation works.
    lmnr_api_key = os.environ.get("LMNR_PROJECT_API_KEY")
    if lmnr_api_key:
        try:
            from lmnr import Laminar  # noqa: PLC0415

            lmnr_base_url = os.environ.get("LMNR_BASE_URL", "http://localhost")
            lmnr_grpc_port = int(os.environ.get("LMNR_GRPC_PORT", "8001"))
            lmnr_http_port = int(os.environ.get("LMNR_HTTP_PORT", "8000"))
            Laminar.initialize(
                project_api_key=lmnr_api_key,
                base_url=lmnr_base_url,
                grpc_port=lmnr_grpc_port,
                http_port=lmnr_http_port,
            )
            LOG.info("Laminar tracing initialized", base_url=lmnr_base_url, grpc_port=lmnr_grpc_port)
        except Exception as e:
            LOG.warning("Failed to initialize Laminar tracing", error=str(e))

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
        detail = format_validation_errors(exc)
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content={"detail": detail},
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
