from __future__ import annotations

import typing
from typing import Any

if typing.TYPE_CHECKING:
    from skyvern.forge.forge_app import ForgeApp


class AppHolder:
    def __init__(self) -> None:
        object.__setattr__(self, "_inst", None)

    def set_app(self, inst: ForgeApp) -> None:
        object.__setattr__(self, "_inst", inst)

    def __getattr__(self, name: str) -> Any:
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


def set_force_app_instance(inst: ForgeApp) -> None:
    app.set_app(inst)  # type: ignore
