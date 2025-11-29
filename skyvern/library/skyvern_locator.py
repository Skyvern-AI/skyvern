from typing import Any, Pattern

from playwright.async_api import Locator


class SkyvernLocator:
    """Locator for finding and interacting with elements on a page.

    Provides methods for performing actions (click, fill, type), querying element state,
    and chaining locators to find specific elements. Compatible with Playwright's locator API.
    """

    def __init__(self, locator: Locator):
        self._locator = locator

    # Action methods
    async def click(self, **kwargs: Any) -> None:
        """Click the element."""
        await self._locator.click(**kwargs)

    async def fill(self, value: str, **kwargs: Any) -> None:
        """Fill an input element with text."""
        await self._locator.fill(value, **kwargs)

    async def type(self, text: str, **kwargs: Any) -> None:
        """Type text into the element character by character."""
        await self._locator.type(text, **kwargs)

    async def select_option(
        self,
        value: str | list[str] | None = None,
        **kwargs: Any,
    ) -> list[str]:
        """Select an option in a <select> element."""
        return await self._locator.select_option(value, **kwargs)

    async def check(self, **kwargs: Any) -> None:
        """Check a checkbox or radio button."""
        await self._locator.check(**kwargs)

    async def uncheck(self, **kwargs: Any) -> None:
        """Uncheck a checkbox."""
        await self._locator.uncheck(**kwargs)

    async def clear(self, **kwargs: Any) -> None:
        """Clear an input field."""
        await self._locator.clear(**kwargs)

    async def hover(self, **kwargs: Any) -> None:
        """Hover over the element."""
        await self._locator.scroll_into_view_if_needed()
        await self._locator.hover(**kwargs)

    async def focus(self, **kwargs: Any) -> None:
        """Focus the element."""
        await self._locator.focus(**kwargs)

    async def press(self, key: str, **kwargs: Any) -> None:
        """Press a key on the element."""
        await self._locator.press(key, **kwargs)

    # Query methods
    async def count(self) -> int:
        """Get the number of elements matching the locator."""
        return await self._locator.count()

    async def text_content(self, **kwargs: Any) -> str | None:
        """Get the text content of the element."""
        return await self._locator.text_content(**kwargs)

    async def inner_text(self, **kwargs: Any) -> str:
        """Get the inner text of the element."""
        return await self._locator.inner_text(**kwargs)

    async def inner_html(self, **kwargs: Any) -> str:
        """Get the inner HTML of the element."""
        return await self._locator.inner_html(**kwargs)

    async def get_attribute(self, name: str, **kwargs: Any) -> str | None:
        """Get an attribute value from the element."""
        return await self._locator.get_attribute(name, **kwargs)

    async def input_value(self, **kwargs: Any) -> str:
        """Get the value of an input element."""
        return await self._locator.input_value(**kwargs)

    # State methods
    async def is_visible(self, **kwargs: Any) -> bool:
        """Check if the element is visible."""
        return await self._locator.is_visible(**kwargs)

    async def is_hidden(self, **kwargs: Any) -> bool:
        """Check if the element is hidden."""
        return await self._locator.is_hidden(**kwargs)

    async def is_enabled(self, **kwargs: Any) -> bool:
        """Check if the element is enabled."""
        return await self._locator.is_enabled(**kwargs)

    async def is_disabled(self, **kwargs: Any) -> bool:
        """Check if the element is disabled."""
        return await self._locator.is_disabled(**kwargs)

    async def is_editable(self, **kwargs: Any) -> bool:
        """Check if the element is editable."""
        return await self._locator.is_editable(**kwargs)

    async def is_checked(self, **kwargs: Any) -> bool:
        """Check if a checkbox or radio button is checked."""
        return await self._locator.is_checked(**kwargs)

    # Filtering and chaining methods
    def first(self) -> "SkyvernLocator":
        """Get the first matching element."""
        return SkyvernLocator(self._locator.first)

    def last(self) -> "SkyvernLocator":
        """Get the last matching element."""
        return SkyvernLocator(self._locator.last)

    def nth(self, index: int) -> "SkyvernLocator":
        """Get the nth matching element (0-indexed)."""
        return SkyvernLocator(self._locator.nth(index))

    def filter(self, **kwargs: Any) -> "SkyvernLocator":
        """Filter the locator by additional criteria."""
        return SkyvernLocator(self._locator.filter(**kwargs))

    def locator(self, selector: str, **kwargs: Any) -> "SkyvernLocator":
        """Find a descendant element."""
        return SkyvernLocator(self._locator.locator(selector, **kwargs))

    def get_by_label(self, text: str | Pattern[str], **kwargs: Any) -> "SkyvernLocator":
        """Find an input element by its associated label text."""
        return SkyvernLocator(self._locator.get_by_label(text, **kwargs))

    def get_by_text(self, text: str | Pattern[str], **kwargs: Any) -> "SkyvernLocator":
        """Find an element containing the specified text."""
        return SkyvernLocator(self._locator.get_by_text(text, **kwargs))

    def get_by_title(self, text: str | Pattern[str], **kwargs: Any) -> "SkyvernLocator":
        """Find an element by its title attribute."""
        return SkyvernLocator(self._locator.get_by_title(text, **kwargs))

    def get_by_role(self, role: str, **kwargs: Any) -> "SkyvernLocator":
        """Find an element by its ARIA role."""
        return SkyvernLocator(self._locator.get_by_role(role, **kwargs))

    def get_by_placeholder(self, text: str | Pattern[str], **kwargs: Any) -> "SkyvernLocator":
        """Find an input element by its placeholder text."""
        return SkyvernLocator(self._locator.get_by_placeholder(text, **kwargs))

    def get_by_alt_text(self, text: str | Pattern[str], **kwargs: Any) -> "SkyvernLocator":
        """Find an element by its alt text (typically images)."""
        return SkyvernLocator(self._locator.get_by_alt_text(text, **kwargs))

    def get_by_test_id(self, test_id: str) -> "SkyvernLocator":
        """Find an element by its test ID attribute."""
        return SkyvernLocator(self._locator.get_by_test_id(test_id))

    # Waiting and screenshot
    async def wait_for(self, **kwargs: Any) -> None:
        """Wait for the element to reach a specific state."""
        await self._locator.wait_for(**kwargs)

    async def screenshot(self, **kwargs: Any) -> bytes:
        """Take a screenshot of the element."""
        return await self._locator.screenshot(**kwargs)

    # Access to underlying Playwright locator
    @property
    def playwright_locator(self) -> Locator:
        """Get the underlying Playwright Locator object."""
        return self._locator
