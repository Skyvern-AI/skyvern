"""Structural predicates over browser page/frame objects.

Skyvern runs on stock Playwright and Patchright today, with room for further
engine adapters (e.g. a Rustwright candidate) behind the same call sites. Each
engine ships its own ``Page`` / ``Frame`` classes with distinct identities, so
``isinstance(obj, Page)`` really asks "is this *that package's* Page" -- it
silently returns ``False`` for an equivalent object produced by a different
engine and forces a concrete driver import at the call site.

These predicates answer the actual question -- "does this object behave like a
top-level page, or like a subframe?" -- from structural capability alone, naming
no driver class. They also hold for ``MagicMock(spec=Page)`` /
``MagicMock(spec=Frame)`` fixtures, which expose exactly the spec'd attributes.
"""

from __future__ import annotations

from typing import Any, Protocol, TypeGuard

# Every member the ``PageLike`` Protocol promises a caller may dereference once
# ``is_page_like`` holds; the runtime check must verify all of them or the
# ``TypeGuard`` is unsound. ``main_frame`` + ``bring_to_front`` are also the
# page-vs-frame discriminators (a subframe exposes neither), and ``context`` is
# page-only; ``evaluate`` is shared with frames but is still dereferenced on a
# narrowed page, so it is required too. Page-only across the Playwright/Patchright
# surface and any capability-equivalent future adapter.
_REQUIRED_PAGE_CAPABILITIES = ("main_frame", "context", "bring_to_front", "evaluate")

# A subframe reports the page and parent frame that own it; a top-level page
# exposes neither. Used as a negative guard so an object that happens to carry a
# page capability is still rejected when it is really a frame.
_FRAME_ONLY_CAPABILITIES = ("page", "parent_frame")


# Every member ``DownloadLike`` promises. ``suggested_filename`` + ``save_as`` are the
# download discriminators: no page, frame, or response carries them, so an object that
# reaches an upload helper by mistake is rejected on capability alone.
_REQUIRED_DOWNLOAD_CAPABILITIES = ("suggested_filename", "path", "failure", "save_as", "page")


class DownloadLike(Protocol):
    """Structural download surface a caller may rely on once ``is_download_like`` holds."""

    @property
    def suggested_filename(self) -> str: ...

    @property
    def page(self) -> Any: ...

    def path(self) -> Any: ...

    def failure(self) -> Any: ...

    def save_as(self, path: Any) -> Any: ...


def is_download_like(obj: object) -> TypeGuard[DownloadLike]:
    """True if ``obj`` structurally presents as a browser download a claim resolved to.

    Engine-neutral stand-in for ``isinstance(obj, Download)``: a raw-CDP or Patchright
    download answers the same capabilities without sharing Playwright's class identity.
    """
    return all(hasattr(obj, capability) for capability in _REQUIRED_DOWNLOAD_CAPABILITIES)


class PageLike(Protocol):
    """Structural page surface a caller may rely on once ``is_page_like`` holds.

    Declares only the members Skyvern dereferences on a narrowed page, so the
    ``TypeGuard`` restores static narrowing (e.g. ``page.context``) without
    naming a concrete engine ``Page`` -- an alternate-engine object satisfies it
    by capability, not by nominal identity.
    """

    @property
    def main_frame(self) -> Any: ...

    @property
    def context(self) -> Any: ...

    def bring_to_front(self) -> Any: ...

    def evaluate(self, expression: str, arg: Any = ...) -> Any: ...


def is_page_like(obj: object) -> TypeGuard[PageLike]:
    """True if ``obj`` structurally presents as a top-level browser page.

    Engine-neutral stand-in for ``isinstance(obj, Page)`` when the only thing
    that matters is page-vs-frame. Requires every capability the ``PageLike``
    Protocol promises to be present and every frame-only capability to be absent,
    so a real ``Frame`` (or a ``MagicMock(spec=Frame)``) is rejected even though
    it shares most of the page surface, while a page from any engine is accepted.
    """
    if not all(hasattr(obj, capability) for capability in _REQUIRED_PAGE_CAPABILITIES):
        return False
    if any(hasattr(obj, capability) for capability in _FRAME_ONLY_CAPABILITIES):
        return False
    return True
