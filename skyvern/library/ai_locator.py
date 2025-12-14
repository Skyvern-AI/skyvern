from __future__ import annotations

from typing import Any, Callable

from playwright.async_api import Locator, Page

from skyvern.core.script_generations.skyvern_page_ai import SkyvernPageAi

LOCATOR_CHAIN_METHODS = {
    "nth",
    "first",
    "last",
    "locator",
    "filter",
    "and_",
    "or_",
    "frame_locator",
    "get_by_alt_text",
    "get_by_label",
    "get_by_placeholder",
    "get_by_role",
    "get_by_test_id",
    "get_by_text",
    "get_by_title",
}


class AILocator(Locator):
    """A lazy proxy that acts like a Playwright Locator but resolves XPath via AI on first use.

    This class defers the AI call until an actual Playwright method is invoked,
    allowing the locator to be created synchronously while the AI resolution happens asynchronously.

    Supports fallback to a selector if AI resolution fails.
    """

    def __init__(
        self,
        page: Page,
        page_ai: SkyvernPageAi,
        prompt: str,
        selector: str | None = None,
        selector_kwargs: dict[str, Any] | None = None,
        try_selector_first: bool = True,
        parent_resolver: Callable[[], Any] | None = None,
    ):
        super().__init__(page)
        self._page = page
        self._page_ai = page_ai
        self._prompt = prompt
        self._selector = selector
        self._selector_kwargs = selector_kwargs or {}
        self._resolved_locator: Locator | None = None
        self._try_selector_first = try_selector_first

        # For chaining: store a resolver function that returns the final Locator
        self._parent_resolver = parent_resolver

    async def _resolve(self) -> Locator:
        if self._resolved_locator is None:
            if self._parent_resolver:
                self._resolved_locator = await self._parent_resolver()
            else:
                if self._try_selector_first and self._selector:
                    try:
                        selector_locator = self._page.locator(self._selector, **self._selector_kwargs)
                        count = await selector_locator.count()
                        if count > 0:
                            self._resolved_locator = selector_locator
                            return self._resolved_locator
                    except Exception:
                        # Selector failed, will try AI below
                        pass

                try:
                    xpath = await self._page_ai.ai_locate_element(prompt=self._prompt)
                    if not xpath:
                        raise ValueError(f"AI failed to locate element with prompt: {self._prompt}")

                    self._resolved_locator = self._page.locator(
                        xpath if xpath.startswith(("xpath=", "css=", "text=", "role=", "id=")) else f"xpath={xpath}"
                    )
                except Exception as e:
                    if self._selector and not self._try_selector_first:
                        self._resolved_locator = self._page.locator(self._selector, **self._selector_kwargs)
                    else:
                        raise e

        return self._resolved_locator

    def __getattribute__(self, name: str) -> Any:
        if name.startswith("_"):
            return object.__getattribute__(self, name)

        # Locator chaining method
        if name in LOCATOR_CHAIN_METHODS:

            def locator_chain_wrapper(*args: Any, **kwargs: Any) -> AILocator:
                async def resolver() -> Locator:
                    parent_locator = await self._resolve()
                    method = getattr(parent_locator, name)
                    return method(*args, **kwargs)

                return AILocator(
                    page=self._page,
                    page_ai=self._page_ai,
                    prompt=self._prompt,
                    selector=self._selector,
                    selector_kwargs=self._selector_kwargs,
                    try_selector_first=self._try_selector_first,
                    parent_resolver=resolver,
                )

            return locator_chain_wrapper

        # For all other methods (async actions like click, fill, etc.)
        async def async_method_wrapper(*args: Any, **kwargs: Any) -> Any:
            locator = await self._resolve()
            method = getattr(locator, name)
            result = method(*args, **kwargs)
            return await result

        return async_method_wrapper
