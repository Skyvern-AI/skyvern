from __future__ import annotations

from typing import Any

from playwright.async_api import Locator, Page

from skyvern.core.script_generations.skyvern_page_ai import SkyvernPageAi


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
        fallback_selector: str | None = None,
        fallback_kwargs: dict[str, Any] | None = None,
    ):
        super().__init__(page)
        self._page = page
        self._page_ai = page_ai
        self._prompt = prompt
        self._fallback_selector = fallback_selector
        self._fallback_kwargs = fallback_kwargs or {}
        self._resolved_locator: Locator | None = None

    async def _resolve(self) -> Locator:
        if self._resolved_locator is None:
            try:
                xpath = await self._page_ai.ai_locate_element(prompt=self._prompt)
                if not xpath:
                    raise ValueError(f"AI failed to locate element with prompt: {self._prompt}")

                self._resolved_locator = self._page.locator(f"xpath={xpath}")
            except Exception as e:
                if self._fallback_selector:
                    self._resolved_locator = self._page.locator(self._fallback_selector, **self._fallback_kwargs)
                else:
                    raise e

        return self._resolved_locator

    def __getattribute__(self, name: str) -> Any:
        if name.startswith("_") or name in ("__class__", "__dict__"):
            return object.__getattribute__(self, name)

        async def async_method_wrapper(*args: Any, **kwargs: Any) -> Any:
            locator = await object.__getattribute__(self, "_resolve")()
            method = getattr(locator, name)
            return await method(*args, **kwargs)

        return async_method_wrapper
