from typing import Any

from skyvern.core.script_generations.skyvern_page import RunContext, SkyvernPage


async def setup(parameters: dict[str, Any], generate_response: bool = False) -> tuple[SkyvernPage, RunContext]:
    skyvern_page = await SkyvernPage.create()
    run_context = RunContext(parameters=parameters, page=skyvern_page)
    return skyvern_page, run_context
