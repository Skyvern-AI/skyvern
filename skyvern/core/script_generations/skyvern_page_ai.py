from __future__ import annotations

from typing import Any, Protocol

from skyvern.config import settings


class SkyvernPageAi(Protocol):
    """Protocol defining the interface for AI-powered page interactions."""

    async def ai_click(
        self,
        selector: str | None,
        intention: str,
        data: str | dict[str, Any] | None = None,
        timeout: float = settings.BROWSER_ACTION_TIMEOUT_MS,
    ) -> str | None:
        """Click an element using AI to locate it based on intention."""
        ...

    async def ai_input_text(
        self,
        selector: str | None,
        value: str | None,
        intention: str,
        data: str | dict[str, Any] | None = None,
        totp_identifier: str | None = None,
        totp_url: str | None = None,
        timeout: float = settings.BROWSER_ACTION_TIMEOUT_MS,
    ) -> str:
        """Input text into an element using AI to determine the value."""
        ...

    async def ai_upload_file(
        self,
        selector: str | None,
        files: str | None,
        intention: str,
        data: str | dict[str, Any] | None = None,
        timeout: float = settings.BROWSER_ACTION_TIMEOUT_MS,
        public_url_only: bool = False,
    ) -> str:
        """Upload a file using AI to process the file URL."""
        ...

    async def ai_select_option(
        self,
        selector: str | None,
        value: str | None,
        intention: str,
        data: str | dict[str, Any] | None = None,
        timeout: float = settings.BROWSER_ACTION_TIMEOUT_MS,
    ) -> str:
        """Select an option from a dropdown using AI."""
        ...

    async def ai_extract(
        self,
        prompt: str,
        schema: dict[str, Any] | list | str | None = None,
        error_code_mapping: dict[str, str] | None = None,
        intention: str | None = None,
        data: str | dict[str, Any] | None = None,
    ) -> dict[str, Any] | list | str | None:
        """Extract information from the page using AI."""
        ...

    async def ai_validate(
        self,
        prompt: str,
        model: dict[str, Any] | None = None,
    ) -> bool:
        """Validate the current page state using AI based on the given criteria."""
        ...

    async def ai_act(
        self,
        prompt: str,
    ) -> None:
        """Perform an action on the page using AI based on a natural language prompt."""
        ...

    async def ai_locate_element(
        self,
        prompt: str,
    ) -> str | None:
        """Locate an element on the page using AI and return its XPath selector."""
        ...

    async def ai_classify(
        self,
        options: dict[str, str],
        url_patterns: dict[str, str] | None = None,
        text_patterns: dict[str, str | list[str]] | None = None,
    ) -> str:
        """Classify the current page state against named options using a tiered cascade.

        Tier 0: URL pattern matching (FREE)
        Tier 1: Text presence check in extracted text (FREE, requires scrape)
        Tier 2: Mini-LLM classification (~$0.001)

        Returns the matching option key or "UNKNOWN".
        """
        ...

    async def ai_element_fallback(
        self,
        navigation_goal: str,
        max_steps: int = 10,
    ) -> None:
        """Activate the AI agent from the CURRENT page position to achieve a navigation goal.

        This is a mid-block fallback that picks up from the current page state
        instead of re-running the entire block from scratch.
        """
        ...

    async def ai_prompt(
        self,
        prompt: str,
        schema: dict[str, Any] | None = None,
        model: dict[str, Any] | None = None,
    ) -> dict[str, Any] | list | str | None:
        """Send a prompt to the LLM and get a response based on the provided schema."""
        ...
