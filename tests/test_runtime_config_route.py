import sys
from collections.abc import Iterator
from types import ModuleType, SimpleNamespace

import pytest

from tests.unit_tests._stub_streaming import import_with_stubs


class _FakeAPIRouter:
    def __init__(self, *, include_in_schema: bool = True, **_: object) -> None:
        self.include_in_schema = include_in_schema
        self.routes: list[SimpleNamespace] = []

    def get(self, path: str, *, include_in_schema: bool = True, **_: object):
        def decorator(fn):
            self.routes.append(
                SimpleNamespace(
                    path=path,
                    include_in_schema=include_in_schema and self.include_in_schema,
                    endpoint=fn,
                )
            )
            return fn

        return decorator


@pytest.fixture()
def runtime_config_module(monkeypatch: pytest.MonkeyPatch) -> Iterator[ModuleType]:
    module_names = [
        "skyvern.forge.sdk.routes.runtime_config",
        "skyvern.forge.sdk.routes.routers",
        "skyvern.forge.sdk.routes",
    ]
    missing = object()
    original_modules = {module_name: sys.modules.get(module_name, missing) for module_name in module_names}

    fastapi_stub = ModuleType("fastapi")
    fastapi_stub.APIRouter = _FakeAPIRouter
    monkeypatch.setitem(sys.modules, "fastapi", fastapi_stub)

    for module_name in module_names:
        sys.modules.pop(module_name, None)

    module = import_with_stubs(
        "skyvern.forge.sdk.routes.runtime_config",
        extra_stubs=[
            "skyvern.forge.sdk.routes.workflow_schedules",
            "skyvern.forge.sdk.routes.streaming.screenshot",
        ],
    )
    yield module

    for module_name in module_names:
        sys.modules.pop(module_name, None)
    for module_name, original_module in original_modules.items():
        if original_module is not missing:
            sys.modules[module_name] = original_module


@pytest.mark.asyncio
async def test_runtime_config_returns_normalized_browser_streaming_mode(
    runtime_config_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_config = runtime_config_module
    monkeypatch.setattr(runtime_config.settings, "BROWSER_STREAMING_MODE", "CDP")
    monkeypatch.setattr(runtime_config.settings, "ENV", "local")

    result = await runtime_config.get_runtime_config()

    assert result.browser_streaming_mode == "cdp"
    assert result.browser_streaming_label == "Local browser streaming"
    assert result.environment == "local"
    assert result.warnings == []


@pytest.mark.asyncio
async def test_runtime_config_falls_back_for_invalid_streaming_mode(
    runtime_config_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_config = runtime_config_module
    monkeypatch.setattr(runtime_config.settings, "BROWSER_STREAMING_MODE", "unexpected")

    result = await runtime_config.get_runtime_config()

    assert result.browser_streaming_mode == "vnc"
    assert result.warnings


def test_runtime_config_routes_are_hidden_from_openapi_schema(runtime_config_module: ModuleType) -> None:
    routers = sys.modules["skyvern.forge.sdk.routes.routers"]
    base_router = routers.base_router
    legacy_base_router = routers.legacy_base_router
    base_routes = [route for route in base_router.routes if getattr(route, "path", None) == "/config/runtime"]
    legacy_routes = [route for route in legacy_base_router.routes if getattr(route, "path", None) == "/config/runtime"]

    assert base_routes
    assert legacy_routes
    assert all(getattr(route, "include_in_schema", True) is False for route in base_routes)
    assert all(getattr(route, "include_in_schema", True) is False for route in legacy_routes)
