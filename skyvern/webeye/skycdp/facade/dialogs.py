"""JavaScript dialogs.

Enabling the Page domain turns off Chrome's own auto-dismissal, which makes an unanswered dialog a
permanent block: the renderer stops servicing anything until the dialog is handled, so every later
operation on that page hangs rather than failing. An engine that enables Page therefore takes on the
obligation to answer every dialog, and the default when the application registers no listener is to
dismiss -- the same choice Playwright makes, and the only one that cannot wedge a page.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from skyvern.webeye.skycdp.connection import CdpSession


class Dialog:
    """One open dialog. Exactly one of accept/dismiss may be called, and one of them must be."""

    def __init__(self, session: CdpSession, params: dict) -> None:
        self._session = session
        self._answered = False
        self.type = str(params.get("type", "alert"))
        self.message = str(params.get("message", ""))
        self.default_value = str(params.get("defaultPrompt", ""))

    async def accept(self, prompt_text: str | None = None) -> None:
        await self._answer(True, prompt_text)

    async def dismiss(self) -> None:
        await self._answer(False, None)

    async def _answer(self, accept: bool, prompt_text: str | None) -> None:
        if self._answered:
            return
        self._answered = True
        params: dict[str, object] = {"accept": accept}
        if prompt_text is not None:
            params["promptText"] = prompt_text
        await self._session.send("Page.handleJavaScriptDialog", params)

    @property
    def answered(self) -> bool:
        return self._answered
