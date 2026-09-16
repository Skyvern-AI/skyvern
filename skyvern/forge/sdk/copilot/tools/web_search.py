from __future__ import annotations

from typing import Any

from skyvern.forge import app
from skyvern.forge.sdk.copilot.context import CopilotContext
from skyvern.forge.sdk.workflow.web_search import FetchedPage, WebSearchObservation, search_web

from ._shared import _composition_get_html, _discovery_navigate


class ScoutingTabTransport:
    """Fetches on the copilot's scouting tab. It reports no HTTP status, so the observation
    carries `http_status: None` rather than an invented one."""

    def __init__(self, copilot_ctx: CopilotContext) -> None:
        self._ctx = copilot_ctx

    async def fetch(self, url: str) -> FetchedPage:
        navigation = await _discovery_navigate(self._ctx, url)
        if not navigation.get("ok"):
            # The reason only: a navigation error embeds the URL, which carries the query.
            return FetchedPage(url=url, title="", html="", error_kind="navigation_failed")
        data = navigation.get("data")
        title = str(data.get("title") or "") if isinstance(data, dict) else ""
        html, read_error, truncated, _used_stripped = await _composition_get_html(self._ctx)
        if read_error:
            return FetchedPage(url=url, title=title, html="", error_kind="page_read_failed")
        return FetchedPage(url=url, title=title, html=html, truncated=truncated)


async def _search_web_impl(copilot_ctx: CopilotContext, query: str, max_results: int = 10) -> dict[str, Any]:
    observation: WebSearchObservation = await search_web(
        app.AGENT_FUNCTION.web_search_provider(),
        ScoutingTabTransport(copilot_ctx),
        query,
        max_results,
    )
    if observation["error_kind"] == "not_configured":
        # A capability this deployment does not have, not a search that returned nothing. The
        # sibling discovery helpers report an unavailable capability the same way.
        return {"ok": False, "data": None, "error": "no web search provider is configured"}
    return {"ok": True, "data": observation, "error": None}
