from __future__ import annotations

import typing
from collections.abc import Mapping
from contextvars import ContextVar, Token
from types import MappingProxyType
from typing import Any

if typing.TYPE_CHECKING:
    from skyvern.forge.forge_app import ForgeApp

# A run's own values for app attributes, such as the LLM handlers its experiments pick. A worker running several
# activities in one process shares the app, so a run's choices live in its context, where reads look first.
_RUN_SCOPED_ATTRIBUTES: ContextVar[Mapping[str, Any]] = ContextVar(
    "run_scoped_app_attributes", default=MappingProxyType({})
)


class AppHolder:
    def __init__(self) -> None:
        object.__setattr__(self, "_inst", None)

    def set_app(self, inst: ForgeApp) -> None:
        object.__setattr__(self, "_inst", inst)

    def __getattr__(self, name: str) -> Any:
        scoped = _RUN_SCOPED_ATTRIBUTES.get()
        if name in scoped:
            return scoped[name]
        inst = object.__getattribute__(self, "_inst")
        if inst is None:
            raise RuntimeError("ForgeApp is not initialized. Call start_forge_app() before accessing app properties.")

        return getattr(inst, name)

    def __setattr__(self, name: str, value: Any) -> None:
        inst = object.__getattribute__(self, "_inst")
        if inst is None:
            raise RuntimeError("ForgeApp is not initialized. Call start_forge_app() before accessing app properties.")

        setattr(inst, name, value)


if typing.TYPE_CHECKING:
    app: ForgeApp
else:
    app = AppHolder()  # type: ignore


def is_forge_app_initialized() -> bool:
    return object.__getattribute__(app, "_inst") is not None


def set_force_app_instance(inst: ForgeApp) -> None:
    app.set_app(inst)  # type: ignore


def set_run_scoped_app_attribute(name: str, value: Any) -> None:
    """Give the current run, meaning this task and the tasks it starts afterwards, its own value of an app attribute."""
    _RUN_SCOPED_ATTRIBUTES.set(MappingProxyType({**_RUN_SCOPED_ATTRIBUTES.get(), name: value}))


def begin_run_scoped_app_attributes() -> Token[Mapping[str, Any]]:
    """Start a run with no run-scoped app values; pass the token to end_run_scoped_app_attributes when it ends."""
    return _RUN_SCOPED_ATTRIBUTES.set(MappingProxyType({}))


def end_run_scoped_app_attributes(token: Token[Mapping[str, Any]]) -> None:
    """Drop the run's values, restoring those of any run that encloses it on the same task."""
    _RUN_SCOPED_ATTRIBUTES.reset(token)
