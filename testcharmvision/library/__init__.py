import typing
from typing import Any

if typing.TYPE_CHECKING:
    from testcharmvision.library.testcharmvision import Testcharmvision  # noqa: E402

# noinspection PyUnresolvedReferences
__all__ = ["Testcharmvision"]


def __getattr__(name: str) -> Any:
    """Lazily import Testcharmvision."""
    if name == "Testcharmvision":
        from testcharmvision.library.testcharmvision import Testcharmvision  # noqa: PLC0415

        globals()["Testcharmvision"] = Testcharmvision
        return Testcharmvision
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
