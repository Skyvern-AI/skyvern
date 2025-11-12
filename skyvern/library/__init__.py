import typing
from typing import Any

if typing.TYPE_CHECKING:
    from skyvern.library.skyvern import Skyvern  # noqa: E402
    from skyvern.library.skyvern_sdk import SkyvernSdk  # noqa: E402

# noinspection PyUnresolvedReferences
__all__ = ["Skyvern", "SkyvernSdk"]


def __getattr__(name: str) -> Any:
    """Lazily import Skyvern."""
    if name == "SkyvernClient":
        # Direct import and assignment
        import skyvern.library.skyvern as _skyvern  # noqa: PLC0415

        Skyvern = _skyvern.Skyvern

        globals()["SkyvernClient"] = Skyvern
        return Skyvern
    if name == "SkyvernSdk":
        import skyvern.library.skyvern_sdk as _skyvern_sdk  # noqa: PLC0415

        SkyvernSdk = _skyvern_sdk.SkyvernSdk

        globals()["SkyvernSdk"] = SkyvernSdk
        return SkyvernSdk
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
