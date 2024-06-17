import uuid
from datetime import datetime
from typing import Awaitable, Callable

import structlog
from fastapi import APIRouter, FastAPI, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from starlette.requests import HTTPConnection, Request
from starlette_context.middleware import RawContextMiddleware
from starlette_context.plugins.base import Plugin

from skyvern.exceptions import SkyvernHTTPException
from skyvern.forge import app as forge_app
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.db.exceptions import NotFoundError
from skyvern.forge.sdk.routes.agent_protocol import base_router
from skyvern.forge.sdk.settings_manager import SettingsManager
from skyvern.scheduler import SCHEDULER

LOG = structlog.get_logger()


class ExecutionDatePlugin(Plugin):
    key = "execution_date"

    async def process_request(self, request: Request | HTTPConnection) -> datetime:
        return datetime.now()


def get_agent_app(router: APIRouter = base_router) -> FastAPI:
    """
    Start the agent server.
    """

    app = FastAPI()

    # Add CORS middleware
    origins = [
        "http://localhost:5000",
        "http://127.0.0.1:5000",
        "http://localhost:8000",
        "http://127.0.0.1:8000",
        "http://localhost:8080",
        "http://127.0.0.1:8080",
        # Add any other origins you want to whitelist
    ]

    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(router, prefix="/api/v1")

    app.add_middleware(
        RawContextMiddleware,
        plugins=(
            # TODO (suchintan): We should set these up
            ExecutionDatePlugin(),
            # RequestIdPlugin(),
            # UserAgentPlugin(),
        ),
    )

    # Register the scheduler on startup so that we can schedule jobs dynamically
    @app.on_event("startup")
    def start_scheduler() -> None:
        LOG.info("Starting the skyvern scheduler.")
        SCHEDULER.start()

        LOG.info("Server startup complete. Skyvern is now online")

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

    if SettingsManager.get_settings().ADDITIONAL_MODULES:
        for module in SettingsManager.get_settings().ADDITIONAL_MODULES:
            LOG.info("Loading additional module to set up api app", module=module)
            __import__(module)
        LOG.info(
            "Additional modules loaded to set up api app",
            modules=SettingsManager.get_settings().ADDITIONAL_MODULES,
        )

    if forge_app.setup_api_app:
        forge_app.setup_api_app(app)

    return app


app = get_agent_app()
