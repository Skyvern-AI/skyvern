from __future__ import annotations

import weakref
from dataclasses import dataclass

from playwright.async_api import BrowserContext, Page

PLAYWRIGHT_DEFAULT_TIMEOUT_MS = 30_000


@dataclass
class _PlaywrightContextInputDefaults:
    timeout_ms: float
    strict_selectors: bool


class PlaywrightInputDefaults:
    """Public runtime state needed to adapt Page.fill/type through Locator."""

    def __init__(
        self,
        timeout_ms: float = PLAYWRIGHT_DEFAULT_TIMEOUT_MS,
        strict_selectors: bool = False,
        *,
        context_defaults: _PlaywrightContextInputDefaults | None = None,
    ) -> None:
        self._context_defaults = context_defaults or _PlaywrightContextInputDefaults(
            timeout_ms=timeout_ms,
            strict_selectors=strict_selectors,
        )
        self._page_timeout_ms: float | None = None

    @property
    def timeout_ms(self) -> float:
        return self._context_defaults.timeout_ms if self._page_timeout_ms is None else self._page_timeout_ms

    @property
    def strict_selectors(self) -> bool:
        return self._context_defaults.strict_selectors

    @property
    def context_timeout_ms(self) -> float:
        return self._context_defaults.timeout_ms

    @property
    def page_timeout_ms(self) -> float | None:
        return self._page_timeout_ms

    def set_page_timeout(self, timeout_ms: float) -> None:
        self._page_timeout_ms = timeout_ms

    def restore_page_timeout(self, timeout_ms: float | None) -> None:
        self._page_timeout_ms = timeout_ms

    def set_context_timeout(self, timeout_ms: float) -> None:
        self._context_defaults.timeout_ms = timeout_ms


_CONTEXT_DEFAULTS: dict[
    int,
    tuple[weakref.ReferenceType[BrowserContext], _PlaywrightContextInputDefaults],
] = {}
_PAGE_DEFAULTS: dict[int, tuple[weakref.ReferenceType[Page], PlaywrightInputDefaults]] = {}


def _drop_context(context_id: int) -> None:
    _CONTEXT_DEFAULTS.pop(context_id, None)


def _drop_page(page_id: int) -> None:
    _PAGE_DEFAULTS.pop(page_id, None)


def register_playwright_input_context(
    context: BrowserContext,
    *,
    timeout_ms: float = PLAYWRIGHT_DEFAULT_TIMEOUT_MS,
    strict_selectors: bool = False,
) -> None:
    """Register the options used to create a live BrowserContext without private Playwright state."""
    context_id = id(context)
    existing = _CONTEXT_DEFAULTS.get(context_id)
    if existing is not None and existing[0]() is context:
        return
    defaults = _PlaywrightContextInputDefaults(timeout_ms=timeout_ms, strict_selectors=strict_selectors)
    try:
        reference = weakref.ref(context, lambda _reference: _drop_context(context_id))
    except TypeError:
        return
    _CONTEXT_DEFAULTS[context_id] = (reference, defaults)


def playwright_input_defaults_for_page(page: Page) -> PlaywrightInputDefaults:
    """Return mutable Page/Context defaults shared by every recorder for this live page."""
    page_id = id(page)
    existing_page = _PAGE_DEFAULTS.get(page_id)
    if existing_page is not None and existing_page[0]() is page:
        return existing_page[1]

    context = page.context
    context_id = id(context)
    existing_context = _CONTEXT_DEFAULTS.get(context_id)
    if existing_context is not None and existing_context[0]() is context:
        context_defaults = existing_context[1]
    else:
        context_defaults = _PlaywrightContextInputDefaults(
            timeout_ms=PLAYWRIGHT_DEFAULT_TIMEOUT_MS,
            strict_selectors=False,
        )
        try:
            context_reference = weakref.ref(context, lambda _reference: _drop_context(context_id))
        except TypeError:
            context_reference = None
        if context_reference is not None:
            _CONTEXT_DEFAULTS[context_id] = (context_reference, context_defaults)

    defaults = PlaywrightInputDefaults(context_defaults=context_defaults)
    try:
        page_reference = weakref.ref(page, lambda _reference: _drop_page(page_id))
    except TypeError:
        return defaults
    _PAGE_DEFAULTS[page_id] = (page_reference, defaults)
    return defaults
