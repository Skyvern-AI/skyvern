from mcp.server.fastmcp import FastMCP

from skyvern.agent import SkyvernAgent
from skyvern.config import settings
from skyvern.schemas.runs import RunEngine

mcp = FastMCP("Skyvern")
skyvern_agent = SkyvernAgent(
    base_url=settings.SKYVERN_BASE_URL,
    api_key=settings.SKYVERN_API_KEY,
)


@mcp.tool()
async def skyvern_run_task(prompt: str, url: str) -> str:
    """Browse the internet using a browser to achieve a user goal.

    Args:
        prompt: brief description of what the user wants to accomplish
        url: the target website for the user goal
    """
    res = await skyvern_agent.run_task(prompt=prompt, url=url, engine=RunEngine.skyvern_v1)
    return res.model_dump()["output"]


if __name__ == "__main__":
    mcp.run(transport="stdio")
