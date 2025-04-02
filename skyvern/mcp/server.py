import os

from mcp.server.fastmcp import FastMCP

from skyvern.agent import SkyvernAgent

mcp = FastMCP("Skyvern")

if "SKYVERN_MCP_CLOUD_URL" in os.environ and "SKYVERN_MCP_API_KEY" in os.environ:
    skyvern_agent = SkyvernAgent(
        base_url=os.environ.get("SKYVERN_MCP_CLOUD_URL"), api_key=os.environ.get("SKYVERN_MCP_API_KEY")
    )
else:
    skyvern_agent = SkyvernAgent()


@mcp.tool()
async def skyvern_run_task(prompt: str, url: str) -> str:
    """Browse the internet using a browser to achieve a user goal.

    Args:
        prompt: brief description of what the user wants to accomplish
        url: the target website for the user goal
    """
    res = await skyvern_agent.run_task(prompt=prompt, url=url)
    return res.model_dump()["output"]


if __name__ == "__main__":
    mcp.run(transport="stdio")
