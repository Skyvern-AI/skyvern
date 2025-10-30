from typing import Any

# noinspection PyUnresolvedReferences
__all__ = ["Skyvern"]


def __getattr__(name: str) -> Any:
    """Lazily import Skyvern."""
    if name == "Skyvern":
        from skyvern.library.skyvern import Skyvern  # noqa: PLC0415

        globals()["Skyvern"] = Skyvern
        return Skyvern
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
