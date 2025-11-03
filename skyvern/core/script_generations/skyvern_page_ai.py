from __future__ import annotations

from typing import Any, Protocol

from skyvern.config import settings


class SkyvernPageAi(Protocol):
    """Protocol defining the interface for AI-powered page interactions."""

    async def ai_click(
        self,
        selector: str,
        intention: str,
        data: str | dict[str, Any] | None = None,
        timeout: float = settings.BROWSER_ACTION_TIMEOUT_MS,
    ) -> str:
        """Click an element using AI to locate it based on intention."""
        ...

    async def ai_input_text(
        self,
        selector: str | None,
        value: str,
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
        selector: str,
        files: str,
        intention: str,
        data: str | dict[str, Any] | None = None,
        timeout: float = settings.BROWSER_ACTION_TIMEOUT_MS,
    ) -> str:
        """Upload a file using AI to process the file URL."""
        ...

    async def ai_select_option(
        self,
        selector: str,
        value: str,
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
