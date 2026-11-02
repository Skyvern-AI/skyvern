"""Regression tests for the batched option-list search behind custom dropdown selection (SKY-18709).

Finding the ``<ul>``/``role="listbox"`` inside an opened menu used to resolve a locator and read an
attribute per visited element, and the menu-locating pass and the scroll-container pass each walked
the subtree. On a menu with hundreds of options, over a remote browser, that was minutes per field.
The search now reads the whole frame once and answers both passes from that single read.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

import skyvern.webeye.scraper.scraper as scraper_mod
from skyvern.webeye.scraper.scraper import LIVE_ELEMENT_PROBE_JS, IncrementalScrapePage

_NODE = shutil.which("node")

# A dropdown menu rendered by a web component lives in an open shadow root, and a widget that
# clones its rows leaves one id matching several elements. Both decide whether the real snippet
# finds the option list, so the shipped expression is the one exercised here.
_PROBE_DRIVER_JS = """
const probe = %s;
const node = (tag, attrs, children, shadowChildren) => ({
  tagName: tag,
  children: children || [],
  getAttribute: (name) => (attrs && name in attrs ? attrs[name] : null),
  shadowRoot: shadowChildren ? { children: shadowChildren } : null,
});

const menu = node("UL", { unique_id: "menu" });
const host = node("DIV", { unique_id: "host" }, [], [menu]);
const clonedRow = node("LI", { unique_id: "row", role: "option" });
const specialIdListbox = node("UL", { unique_id: "__proto__", role: "listbox" });
const root = node("HTML", {}, [host, clonedRow, node("DIV", {}, [clonedRow]), specialIdListbox]);

global.document = { documentElement: root };
console.log(JSON.stringify({
  probes: probe("unique_id"),
  objectPrototypeHasCount: Object.prototype.hasOwnProperty.call(Object.prototype, "count"),
}));
"""


def _menu_element_tree() -> dict:
    return {
        "id": "menu",
        "tagName": "div",
        "attributes": {},
        "children": [
            {"id": "header", "tagName": "div", "attributes": {}, "children": []},
            {
                "id": "options-wrapper",
                "tagName": "div",
                "attributes": {},
                "children": [
                    {"id": "options", "tagName": "div", "attributes": {"role": "listbox"}, "children": []},
                ],
            },
        ],
    }


def _probes_for_menu() -> dict[str, dict]:
    return {
        "menu": {"count": 1, "tagName": "div", "role": None},
        "header": {"count": 1, "tagName": "div", "role": None},
        "options-wrapper": {"count": 1, "tagName": "div", "role": None},
        "options": {"count": 1, "tagName": "div", "role": "listbox"},
    }


def _incremental_page(monkeypatch: pytest.MonkeyPatch, evaluate: AsyncMock) -> IncrementalScrapePage:
    monkeypatch.setattr(scraper_mod.SkyvernFrame, "evaluate", evaluate)
    skyvern_frame = MagicMock()
    skyvern_frame.get_frame.return_value = MagicMock()
    skyvern_frame.get_incremental_element_tree = AsyncMock(return_value=([], []))
    return IncrementalScrapePage(skyvern_frame=skyvern_frame)


@pytest.mark.skipif(_NODE is None, reason="node not on PATH")
def test_probe_reads_shadow_roots_and_handles_special_ids() -> None:
    with tempfile.TemporaryDirectory() as workdir:
        driver = Path(workdir) / "driver.mjs"
        driver.write_text(_PROBE_DRIVER_JS % LIVE_ELEMENT_PROBE_JS)
        result = subprocess.run([str(_NODE), str(driver)], capture_output=True, text=True, timeout=30)

    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    result_data = json.loads(result.stdout)
    probes = result_data["probes"]
    assert probes["menu"] == {"count": 1, "tagName": "ul", "role": None}
    assert probes["host"]["count"] == 1
    assert probes["row"] == {"count": 2, "tagName": "li", "role": "option"}
    assert probes["__proto__"] == {"count": 1, "tagName": "ul", "role": "listbox"}
    assert result_data["objectPrototypeHasCount"] is False


@pytest.mark.asyncio
async def test_option_list_search_reads_the_page_once_for_repeated_walks(monkeypatch: pytest.MonkeyPatch) -> None:
    evaluate = AsyncMock(return_value=_probes_for_menu())
    page = _incremental_page(monkeypatch, evaluate)
    tree = _menu_element_tree()

    assert await page.find_option_list_element_id(tree) == "options"
    # The scroll-container pass walks the same subtree right after the menu-locating pass.
    assert await page.find_option_list_element_id(tree) == "options"

    assert evaluate.await_count == 1


@pytest.mark.asyncio
async def test_option_list_search_skips_an_id_matching_more_than_one_element(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probes = _probes_for_menu()
    probes["options"]["count"] = 2
    page = _incremental_page(monkeypatch, AsyncMock(return_value=probes))

    assert await page.find_option_list_element_id(_menu_element_tree()) is None


@pytest.mark.asyncio
async def test_option_list_search_uses_a_role_set_after_the_scrape(monkeypatch: pytest.MonkeyPatch) -> None:
    probes = _probes_for_menu()
    probes["options-wrapper"]["role"] = "listbox"
    page = _incremental_page(monkeypatch, AsyncMock(return_value=probes))

    # The scraped attributes hold no role for the wrapper, so the live one decides, and the wrapper
    # is reached before the nested listbox.
    assert await page.find_option_list_element_id(_menu_element_tree()) == "options-wrapper"


@pytest.mark.asyncio
async def test_option_list_search_prefers_live_role_over_stale_scraped_role(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tree = _menu_element_tree()
    tree["children"][1]["attributes"]["role"] = "option"
    probes = _probes_for_menu()
    probes["options-wrapper"]["role"] = "listbox"
    page = _incremental_page(monkeypatch, AsyncMock(return_value=probes))

    assert await page.find_option_list_element_id(tree) == "options-wrapper"


@pytest.mark.asyncio
async def test_option_list_search_survives_an_unreadable_page(monkeypatch: pytest.MonkeyPatch) -> None:
    page = _incremental_page(monkeypatch, AsyncMock(side_effect=RuntimeError("page is gone")))

    assert await page.find_option_list_element_id(_menu_element_tree()) is None


@pytest.mark.asyncio
async def test_a_new_incremental_element_tree_drops_the_cached_page_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """A cascading select re-scrapes between levels, so a read cached from the previous level must go."""
    evaluate = AsyncMock(return_value=_probes_for_menu())
    page = _incremental_page(monkeypatch, evaluate)

    assert await page.find_option_list_element_id(_menu_element_tree()) == "options"
    await page.get_incremental_element_tree(AsyncMock(return_value=[]))
    assert await page.find_option_list_element_id(_menu_element_tree()) == "options"

    assert evaluate.await_count == 2
