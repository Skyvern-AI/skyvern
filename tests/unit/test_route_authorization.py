from types import SimpleNamespace

from fastapi import APIRouter, Depends, FastAPI, WebSocket
from fastapi.routing import APIRoute, APIWebSocketRoute
from fastapi.testclient import TestClient

from skyvern.forge import api_app
from skyvern.forge import app as forge_app
from skyvern.forge.agent_functions import AgentFunction
from skyvern.forge.sdk.services import route_authorization
from skyvern.forge.sdk.services.route_authorization import (
    ROUTE_AUTHORIZATION_DEPENDENCY,
    observe_request_authorization,
    require_request_actions,
)
from tests.unit.route_authorization_app import route_connection


def test_every_public_api_route_has_authorization_and_an_action(monkeypatch) -> None:
    monkeypatch.setattr(api_app.settings, "OTEL_ENABLED", False)
    monkeypatch.setattr(api_app, "start_forge_app", lambda **_kwargs: SimpleNamespace(setup_api_app=None))
    app = api_app.create_api_app()
    routes = [route for route in app.routes if isinstance(route, (APIRoute, APIWebSocketRoute))]

    assert routes
    assert all(
        any(dependency.call is observe_request_authorization for dependency in route.dependant.dependencies)
        for route in routes
    )
    assert all(route_authorization._route_action(route_connection(route))[0] for route in routes)


def test_fastapi_router_dependencies_run_for_websocket_routes() -> None:
    router = APIRouter(dependencies=[ROUTE_AUTHORIZATION_DEPENDENCY])

    @router.get("/http")
    async def http_route() -> dict[str, bool]:
        return {"ok": True}

    @router.websocket("/ws")
    async def websocket_route(websocket: WebSocket) -> None:
        await websocket.accept()
        await websocket.send_text("connected")
        await websocket.close()

    app = FastAPI()
    app.include_router(router)

    with TestClient(app) as client:
        assert client.get("/http").status_code == 200
        with client.websocket_connect("/ws") as websocket:
            assert websocket.receive_text() == "connected"


def test_action_enforcement_dependency_preserves_oss_noop(monkeypatch) -> None:
    monkeypatch.setattr(forge_app, "AGENT_FUNCTION", AgentFunction())
    monkeypatch.setattr(route_authorization, "get_request_principal", lambda: None)
    router = APIRouter(dependencies=[Depends(require_request_actions(frozenset({"api_key.list"})))])

    @router.get("/api-keys")
    async def list_api_keys() -> dict[str, bool]:
        return {"handler_ran": True}

    api = FastAPI()
    api.include_router(router)

    with TestClient(api) as client:
        response = client.get("/api-keys")

    assert response.status_code == 200
    assert response.json() == {"handler_ran": True}
