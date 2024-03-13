import uuid
from datetime import datetime
from typing import Awaitable, Callable

import structlog
from fastapi import APIRouter, FastAPI, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.requests import HTTPConnection, Request
from starlette_context.middleware import RawContextMiddleware
from starlette_context.plugins.base import Plugin

from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.routes.agent_protocol import base_router
from skyvern.scheduler import SCHEDULER

LOG = structlog.get_logger()


class Agent:
    def get_agent_app(self, router: APIRouter = base_router) -> FastAPI:
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

        app.add_middleware(AgentMiddleware, agent=self)

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

        @app.exception_handler(Exception)
        async def unexpected_exception(request: Request, exc: Exception) -> JSONResponse:
            LOG.exception("Unexpected error in agent server.", exc_info=exc)
            return JSONResponse(status_code=500, content={"error": f"Unexpected error: {exc}"})

        @app.middleware("http")
        async def request_middleware(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
            request_id = str(uuid.uuid4())
            skyvern_context.set(SkyvernContext(request_id=request_id))

            try:
                return await call_next(request)
            finally:
                skyvern_context.reset()

        return app


class AgentMiddleware:
    """
    Middleware that injects the agent instance into the request scope.
    """

    def __init__(self, app: FastAPI, agent: Agent):
        self.app = app
        self.agent = agent

    async def __call__(self, scope, receive, send):  # type: ignore
        scope["agent"] = self.agent
        await self.app(scope, receive, send)


class ExecutionDatePlugin(Plugin):
    key = "execution_date"

    async def process_request(self, request: Request | HTTPConnection) -> datetime:
        return datetime.now()
