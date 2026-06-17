"""Pin the visual bounding box overlay opt-in default to False across scrape entry points.

Visual bounding box overlay rendering is being phased out; the corresponding
``draw_boxes`` parameter is retained briefly for backwards compatibility and
should default to False everywhere. This file uses ``inspect`` to read live
function signatures so the defaults cannot silently drift back to True.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

from skyvern.webeye.browser_state import BrowserState
from skyvern.webeye.real_browser_state import RealBrowserState
from skyvern.webeye.scraper.scraped_page import ScrapedPage
from skyvern.webeye.scraper.scraper import scrape_web_unsafe, scrape_website


def _draw_boxes_default(fn) -> object:
    sig = inspect.signature(fn)
    assert "draw_boxes" in sig.parameters, f"{fn.__qualname__} is missing the draw_boxes parameter"
    return sig.parameters["draw_boxes"].default


def test_scrape_website_default_is_false() -> None:
    assert _draw_boxes_default(scrape_website) is False


def test_scrape_web_unsafe_default_is_false() -> None:
    assert _draw_boxes_default(scrape_web_unsafe) is False


def test_browser_state_scrape_website_default_is_false() -> None:
    assert _draw_boxes_default(BrowserState.scrape_website) is False


def test_real_browser_state_scrape_website_default_is_false() -> None:
    assert _draw_boxes_default(RealBrowserState.scrape_website) is False


def test_scraped_page_refresh_default_is_false() -> None:
    assert _draw_boxes_default(ScrapedPage.refresh) is False


def test_scraped_page_generate_scraped_page_default_is_false() -> None:
    assert _draw_boxes_default(ScrapedPage.generate_scraped_page) is False


_AGENT_FILE = Path(__file__).resolve().parents[2] / "skyvern" / "forge" / "agent.py"
_SCRIPT_PAGE_FILE = (
    Path(__file__).resolve().parents[2] / "skyvern" / "core" / "script_generations" / "script_skyvern_page.py"
)


def test_agent_does_not_assign_draw_boxes_true() -> None:
    """``ForgeAgent`` callers must not flip ``draw_boxes`` back to True."""

    tree = ast.parse(_AGENT_FILE.read_text())
    offenders: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "draw_boxes":
                    if isinstance(node.value, ast.Constant) and node.value.value is True:
                        offenders.append(node.lineno)
    assert offenders == [], (
        f"agent.py assigns draw_boxes = True at lines {offenders}; "
        "the overlay is deprecated and these assignments must be False."
    )


def test_script_skyvern_page_does_not_pass_draw_boxes_true() -> None:
    """Cached-script scrape entry must not pass ``draw_boxes=True``."""

    tree = ast.parse(_SCRIPT_PAGE_FILE.read_text())
    offenders: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for kw in node.keywords:
                if kw.arg == "draw_boxes" and isinstance(kw.value, ast.Constant) and kw.value.value is True:
                    offenders.append(node.lineno)
    assert offenders == [], (
        f"script_skyvern_page.py passes draw_boxes=True at lines {offenders}; "
        "the overlay is deprecated and these call sites must be False."
    )
