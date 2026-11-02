from __future__ import annotations

from collections.abc import Awaitable, Callable
from types import SimpleNamespace

import pytest

from skyvern.exceptions import SkyvernPageAnalysisTimeout
from skyvern.webeye.scraper import scraper as scraper_module
from skyvern.webeye.scraper.scraper import IncrementalScrapePage


class _CountLocator:
    def __init__(self, selector: str) -> None:
        self.selector = selector

    async def count(self) -> int:
        return 1


class _Frame:
    def locator(self, selector: str) -> _CountLocator:
        return _CountLocator(selector)


@pytest.mark.parametrize("timed_out_element_id", ["child", "parent"])
@pytest.mark.asyncio
async def test_select_one_element_by_value_propagates_lookup_deadline(
    monkeypatch: pytest.MonkeyPatch, timed_out_element_id: str
) -> None:
    page = IncrementalScrapePage(SimpleNamespace(get_frame=lambda: _Frame()))  # type: ignore[arg-type]
    page.element_tree = [
        {
            "id": "parent",
            "interactable": True,
            "children": [{"id": "child", "text": "target", "interactable": False}],
        }
    ]
    lookups: list[str] = []

    async def bounded_lookup(call: Callable[[], Awaitable[int]]) -> int:
        element_id = "child" if '"child"' in call.__self__.selector else "parent"
        lookups.append(element_id)
        if element_id == timed_out_element_id:
            raise SkyvernPageAnalysisTimeout("lookup timed out")
        return await call()

    monkeypatch.setattr(scraper_module, "bounded_lookup_call", bounded_lookup)

    with pytest.raises(SkyvernPageAnalysisTimeout):
        await page.select_one_element_by_value("target")

    assert lookups[-1] == timed_out_element_id
