import asyncio
import json
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import ORJSONResponse
from sqlalchemy.ext.asyncio import AsyncEngine

from skyvern.forge.agent import ForgeAgent
from skyvern.forge.sdk.db.agent_db import AgentDB
from skyvern.forge.sdk.db.repositories import tasks as tasks_repository
from skyvern.forge.sdk.routes import agent_protocol
from skyvern.forge.sdk.routes.routers import base_router, legacy_base_router
from skyvern.forge.sdk.schemas.organizations import Organization
from skyvern.forge.sdk.schemas.tasks import Task
from skyvern.forge.sdk.services import org_auth_service

ORG_ID = "o_task_listing"


class _LoopProbe:
    """Holds a phase's first sync call until the event loop runs another coroutine.

    A call running on the loop thread blocks the loop, so the tick never comes and the wait times out.
    """

    def __init__(self) -> None:
        self._ticked = threading.Event()
        self.loop_ran: dict[str, bool] = {}

    async def tick_forever(self) -> None:
        while True:
            self._ticked.set()
            await asyncio.sleep(0.01)

    def gate(self, phase: str, fn: Callable[..., Any]) -> Callable[..., Any]:
        def gated(*args: Any, **kwargs: Any) -> Any:
            if phase not in self.loop_ran:
                self._ticked.clear()
                self.loop_ran[phase] = self._ticked.wait(timeout=5)
            return fn(*args, **kwargs)

        return gated


@pytest.mark.asyncio
async def test_task_listing_builds_and_serializes_the_page_off_the_event_loop(
    monkeypatch: pytest.MonkeyPatch, sqlite_engine: AsyncEngine
) -> None:
    db = AgentDB("sqlite+aiosqlite://", db_engine=sqlite_engine)
    for index in range(3):
        await db.tasks.create_task(
            url="https://example.com",
            title=f"Task {index}",
            navigation_goal="Find the pricing page",
            data_extraction_goal=None,
            navigation_payload={"index": index},
            organization_id=ORG_ID,
        )
    agent = ForgeAgent()
    app_instance = object.__getattribute__(agent_protocol.app, "_inst")
    monkeypatch.setattr(app_instance, "DATABASE", db)
    monkeypatch.setattr(app_instance, "agent", agent)
    monkeypatch.setattr(agent_protocol.analytics, "capture", lambda *args, **kwargs: None)

    tasks = await db.tasks.get_tasks(page=1, page_size=1000, organization_id=ORG_ID)
    expected_body = ORJSONResponse([(await agent.build_task_response(task=task)).model_dump() for task in tasks]).body

    probe = _LoopProbe()
    monkeypatch.setattr(tasks_repository, "convert_to_task", probe.gate("convert", tasks_repository.convert_to_task))
    monkeypatch.setattr(Task, "to_task_response", probe.gate("serialize", Task.to_task_response))

    now = datetime.now(UTC)
    organization = Organization(organization_id=ORG_ID, organization_name="Listing", created_at=now, modified_at=now)
    fastapi_app = FastAPI()
    fastapi_app.include_router(base_router, prefix="/v1")
    fastapi_app.include_router(legacy_base_router, prefix="/api/v1")
    fastapi_app.dependency_overrides[org_auth_service.get_current_org] = lambda: organization

    ticker = asyncio.create_task(probe.tick_forever())
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=fastapi_app), base_url="http://test") as client:
            response = await client.get("/api/v1/tasks", params={"page_size": 1000, "only_standalone_tasks": False})
    finally:
        ticker.cancel()

    assert response.status_code == 200, response.text
    assert probe.loop_ran == {"convert": True, "serialize": True}
    assert response.json() == json.loads(expected_body)
    assert len(response.json()) == 3
