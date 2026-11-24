from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from skyvern.core.script_generations.script_skyvern_page import ScriptSkyvernPage
from skyvern.core.script_generations.skyvern_page import SkyvernPage
from skyvern.core.script_generations.skyvern_page_ai import SkyvernPageAi


class AiStub:
    pass


class ScrapedPageStub:
    def __init__(self) -> None:
        self._browser_state = SimpleNamespace(begin_navigation=MagicMock(), record_navigation_result=AsyncMock())


def build_navigation_page(
    page_type: type[SkyvernPage], scraped_page: ScrapedPageStub | SimpleNamespace, ai: AiStub | SkyvernPageAi
) -> tuple[SkyvernPage, MagicMock]:
    raw_page = MagicMock(spec=["goto", "url", "title"])
    raw_page.goto = AsyncMock()
    raw_page.url = "about:blank"
    raw_page.title = AsyncMock(return_value="")
    with patch("skyvern.core.script_generations.skyvern_page.Page.__init__", return_value=None):
        if page_type is ScriptSkyvernPage:
            return ScriptSkyvernPage(scraped_page=scraped_page, page=raw_page, ai=ai), raw_page
        return page_type(page=raw_page, ai=ai), raw_page
