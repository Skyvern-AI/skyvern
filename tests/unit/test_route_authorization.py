from types import SimpleNamespace

from fastapi import APIRouter, FastAPI, WebSocket
from fastapi.routing import APIRoute, APIWebSocketRoute
from fastapi.testclient import TestClient

from skyvern.forge import api_app
from skyvern.forge.sdk.services import route_authorization
from skyvern.forge.sdk.services.route_authorization import ROUTE_AUTHORIZATION_DEPENDENCY, observe_request_authorization
from tests.unit.route_authorization_app import route_connection


def test_every_public_api_route_has_authorization_and_an_action(monkeypatch) -> None:
    monkeypatch.setattr(api_app.settings, "OTEL_ENABLED", False)
    monkeypatch.setattr(api_app, "start_forge_app", lambda: SimpleNamespace(setup_api_app=None))
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
