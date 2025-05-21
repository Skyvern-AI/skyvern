from .mcp import setup_mcp


def setup_mcp_command() -> None:
    """Wrapper command to configure the MCP server."""
    setup_mcp()
