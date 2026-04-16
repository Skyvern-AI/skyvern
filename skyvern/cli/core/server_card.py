from __future__ import annotations

_DOCUMENTATION_URL = "https://www.skyvern.com/docs"
_SOURCE_URL = "https://github.com/Skyvern-AI/skyvern"
_API_KEY_HEADER = "x-api-key"


def build_server_card(
    transport_type: str,
    endpoint_url: str,
    tool_count: int | None = None,
) -> dict:
    """Build an MCP server card dict conforming to the published MCP server-card schema.

    Args:
        transport_type: MCP transport type, e.g. ``"streamable-http"``.
        endpoint_url: HTTP endpoint for the MCP server (required).
        tool_count: Number of tools exposed by the server (informational). Omitted from
            the card when ``None``.

    Returns:
        A dict conforming to the MCP server-card schema.
    """
    card: dict = {
        "name": "Skyvern",
        "description": "AI-powered browser automation — navigate, extract, fill forms, run workflows",
        "transport": {"type": transport_type, "endpoint": endpoint_url},
        "authentication": {
            "required": True,
            "scheme": "api-key",
            "header": _API_KEY_HEADER,
        },
        "documentation": _DOCUMENTATION_URL,
        "source": _SOURCE_URL,
    }

    if tool_count is not None:
        card["tool_count"] = tool_count

    return card
