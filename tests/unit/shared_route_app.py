import copy
import functools
import os
from collections.abc import Mapping

import pytest
from fastapi import FastAPI
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute, APIWebSocketRoute
from starlette.datastructures import State

import skyvern.forge.sdk.routes  # noqa: F401
from skyvern.config import settings
from skyvern.forge.sdk.routes.routers import base_router, legacy_base_router

_lessees: dict[int, pytest.MonkeyPatch] = {}
_route_baselines: dict[int, list[tuple[object, ...]]] = {}
_build_settings: dict[int, dict[str, object]] = {}
_mutated_by: dict[int, str] = {}


def _dependency_ids(dependant: Dependant) -> tuple[int, ...]:
    ids: list[int] = []
    pending = list(dependant.dependencies)
    while pending:
        sub_dependant = pending.pop()
        ids.append(id(sub_dependant))
        pending.extend(sub_dependant.dependencies)
    return tuple(ids)


def _route_states(fastapi_app: FastAPI) -> list[tuple[object, ...]]:
    states: list[tuple[object, ...]] = []
    for route in fastapi_app.router.routes:
        if isinstance(route, APIRoute):
            states.append(
                (
                    route.path,
                    frozenset(route.methods),
                    _dependency_ids(route.dependant),
                    tuple(map(id, route.dependencies)),
                    tuple(route.tags),
                    route.responses,
                    route.openapi_extra,
                    len(route.callbacks or ()),
                )
            )
        elif isinstance(route, APIWebSocketRoute):
            states.append((route.path, _dependency_ids(route.dependant), tuple(map(id, route.dependencies))))
    return states


def _verify_routes_on_return(fastapi_app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    borrower = os.environ.get("PYTEST_CURRENT_TEST", "an unnamed borrower").rsplit(" (", 1)[0]
    undo = monkeypatch.undo

    def undo_then_verify() -> None:
        vars(monkeypatch).pop("undo", None)
        undo()
        baseline = _route_baselines[id(fastapi_app)]
        mutated = sorted({str(now[0]) for now, before in zip(_route_states(fastapi_app), baseline) if now != before})
        if mutated:
            _mutated_by[id(fastapi_app)] = borrower
            raise RuntimeError(f"{borrower} mutated shared route internals of {mutated}; give it a private FastAPI().")

    # Relies on MonkeyPatch having no __slots__ and on its teardown and context() calling undo() by attribute.
    vars(monkeypatch)["undo"] = undo_then_verify


def lend_app(
    fastapi_app: FastAPI, monkeypatch: pytest.MonkeyPatch, build_settings: Mapping[str, object] | None = None
) -> FastAPI:
    # Lends a process-wide app to one borrower at a time: overrides, handlers, middleware, state, the route list,
    # per-route attributes and lifecycle hooks are copies that undo discards. Undo then checks what stays shared (each
    # route's methods, dependant tree, dependencies, tags, responses, openapi_extra and callback count), and a borrow
    # refuses an app built under other settings.
    if id(fastapi_app) in _mutated_by:
        raise RuntimeError(f"The shared app's routes were mutated by {_mutated_by[id(fastapi_app)]}; fix that test.")
    if id(fastapi_app) in _lessees:
        raise RuntimeError(
            "This shared app is already lent to this test or a module-scoped fixture; borrow it once and reuse it."
        )
    current_settings = dict(build_settings or {})
    built_with = _build_settings.setdefault(id(fastapi_app), current_settings)
    changed_settings = sorted(
        name
        for name in built_with.keys() | current_settings.keys()
        if built_with.get(name) != current_settings.get(name)
    )
    if changed_settings:
        raise RuntimeError(f"The shared app was built under other values of {changed_settings}; build a separate app.")
    if id(fastapi_app) not in _route_baselines:
        _route_baselines[id(fastapi_app)] = copy.deepcopy(_route_states(fastapi_app))
    router = fastapi_app.router
    monkeypatch.setitem(_lessees, id(fastapi_app), monkeypatch)
    monkeypatch.setattr(fastapi_app, "dependency_overrides", {})
    monkeypatch.setattr(fastapi_app, "exception_handlers", dict(fastapi_app.exception_handlers))
    monkeypatch.setattr(fastapi_app, "user_middleware", list(fastapi_app.user_middleware))
    monkeypatch.setattr(fastapi_app, "middleware_stack", None)
    monkeypatch.setattr(fastapi_app, "state", State(dict(fastapi_app.state._state)))
    monkeypatch.setattr(fastapi_app, "openapi_schema", None)
    monkeypatch.setitem(vars(fastapi_app), "openapi", fastapi_app.openapi)
    monkeypatch.setattr(router, "routes", [copy.copy(route) for route in router.routes])
    monkeypatch.setattr(router, "on_startup", list(router.on_startup))
    monkeypatch.setattr(router, "on_shutdown", list(router.on_shutdown))
    monkeypatch.setattr(router, "lifespan_context", router.lifespan_context)
    _verify_routes_on_return(fastapi_app, monkeypatch)
    return fastapi_app


def api_app_build_settings() -> dict[str, object]:
    # create_api_app() fixes CORS, the local-only routers and the staging auth fixture from these when it builds.
    return {
        "ENV": settings.ENV,
        "OTEL_ENABLED": settings.OTEL_ENABLED,
        "ALLOWED_ORIGINS": list(settings.ALLOWED_ORIGINS),
        "ALLOWED_ORIGIN_REGEX": settings.ALLOWED_ORIGIN_REGEX,
        "ADDITIONAL_MODULES": list(settings.ADDITIONAL_MODULES),
        "COPILOT_EVAL_AUTH_FIXTURE_ENABLED": settings.COPILOT_EVAL_AUTH_FIXTURE_ENABLED,
    }


@functools.cache
def _route_app() -> FastAPI:
    fastapi_app = FastAPI()
    fastapi_app.include_router(base_router, prefix="/v1")
    fastapi_app.include_router(legacy_base_router, prefix="/api/v1")
    return fastapi_app


def shared_route_app(monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    # include_router over every route costs ~0.5 s, so the /v1 and /api/v1 mounts are built once per process.
    return lend_app(_route_app(), monkeypatch)
