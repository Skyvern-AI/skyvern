from typing import Any

from skyvern.core.script_generations.script_run_context_manager import script_run_context_manager
from skyvern.core.script_generations.skyvern_page import RunContext, SkyvernPage


async def setup(parameters: dict[str, Any], run_id: str | None = None) -> tuple[SkyvernPage, RunContext]:
    skyvern_page = await SkyvernPage.create()
    run_context = RunContext(parameters=parameters, page=skyvern_page)
    script_run_context_manager.set_run_context(run_context)
    return skyvern_page, run_context
