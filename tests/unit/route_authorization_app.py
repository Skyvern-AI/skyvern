from fastapi.routing import APIRoute, APIWebSocketRoute
from starlette.requests import HTTPConnection


def route_connection(route: APIRoute | APIWebSocketRoute) -> HTTPConnection:
    is_websocket = isinstance(route, APIWebSocketRoute)
    path = route.path
    return HTTPConnection(
        {
            "type": "websocket" if is_websocket else "http",
            "scheme": "ws" if is_websocket else "http",
            "path": path,
            "root_path": "",
            "query_string": b"",
            "headers": [],
            "client": ("testclient", 123),
            "server": ("testserver", 80),
            "method": "GET",
            "path_params": {},
            "route": route,
        }
    )
