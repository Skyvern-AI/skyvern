from __future__ import annotations

from typing import Any

from skyvern.forge.sdk.workflow.web_search import WebSearchObservation, search_web


async def _search_web_impl(query: str, max_results: int = 10) -> dict[str, Any]:
    observation: WebSearchObservation = await search_web(query, max_results)
    if observation["error_kind"] == "not_configured":
        # A capability this deployment does not have, not a search that returned nothing. The
        # sibling discovery helpers report an unavailable capability the same way.
        return {"ok": False, "data": None, "error": "no web search provider is configured"}
    return {"ok": True, "data": observation, "error": None}
