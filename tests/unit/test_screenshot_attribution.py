"""Behavioral tests for screenshot attribution signals."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from skyvern.forge.sdk.core.skyvern_context import SkyvernContext

# ---------------------------------------------------------------------------
# SkyvernContext field tests (behavioral)
# ---------------------------------------------------------------------------


def test_scrape_trigger_default_none() -> None:
    ctx = SkyvernContext()
    assert ctx.scrape_trigger is None


def test_scrape_screenshots_consumed_default_none() -> None:
    ctx = SkyvernContext()
    assert ctx.scrape_screenshots_consumed is None


def test_scrape_trigger_settable() -> None:
    ctx = SkyvernContext()
    ctx.scrape_trigger = "verification"
    assert ctx.scrape_trigger == "verification"


def test_screenshots_consumed_settable() -> None:
    ctx = SkyvernContext()
    ctx.scrape_screenshots_consumed = True
    assert ctx.scrape_screenshots_consumed is True
    ctx.scrape_screenshots_consumed = False
    assert ctx.scrape_screenshots_consumed is False


# ---------------------------------------------------------------------------
# Consumption decision logic tests (behavioral)
# ---------------------------------------------------------------------------


@pytest.fixture()
def _ctx_screenshots_enabled() -> SkyvernContext:
    ctx = SkyvernContext()
    ctx.enrich_tree_mode = MagicMock()
    ctx.enrich_tree_mode.__eq__ = lambda self, other: True  # force CONTROL match
    return ctx


def test_consumed_true_when_screenshots_enabled() -> None:
    ctx = SkyvernContext()
    ctx.step_retry_index = 0
    consumed = bool(ctx.llm_screenshots_enabled_for_prompt(retry_index=0))
    ctx.scrape_screenshots_consumed = consumed
    assert ctx.scrape_screenshots_consumed is True


def test_consumed_false_when_screenshots_disabled() -> None:
    from skyvern.forge.sdk.core.skyvern_context import EnrichTreeMode

    ctx = SkyvernContext()
    ctx.enrich_tree_mode = EnrichTreeMode.ENRICHED_TREE_NO_IMAGES
    ctx.step_retry_index = 0
    consumed = bool(ctx.llm_screenshots_enabled_for_prompt(retry_index=0))
    ctx.scrape_screenshots_consumed = consumed
    assert ctx.scrape_screenshots_consumed is False


# ---------------------------------------------------------------------------
# Caller sets context before scrape (source ordering)
# ---------------------------------------------------------------------------


def test_verification_sets_consumed_before_refresh() -> None:
    import inspect

    from skyvern.forge.agent import ForgeAgent

    source = inspect.getsource(ForgeAgent.complete_verify)
    idx_consumed = source.index("scrape_screenshots_consumed")
    idx_refresh = source.index("scraped_page.refresh(")
    assert idx_consumed < idx_refresh


def test_step_body_sets_consumed_before_scrape() -> None:
    import inspect

    from skyvern.forge.agent import ForgeAgent

    source = inspect.getsource(ForgeAgent.build_and_record_step_prompt)
    assert "scrape_screenshots_consumed" in source


def test_scraper_emits_screenshots_consumed() -> None:
    import inspect

    from skyvern.webeye.scraper import scraper

    source = inspect.getsource(scraper.scrape_web_unsafe)
    assert "screenshots_consumed" in source


def test_extraction_sets_trigger_and_consumed_before_refresh() -> None:
    import inspect

    from skyvern.webeye.actions import handler

    source = inspect.getsource(handler.extract_information_for_navigation_goal)
    idx_trigger = source.index('scrape_trigger = "extraction"')
    idx_consumed = source.index("scrape_screenshots_consumed = True")
    idx_refresh = source.index("scraped_page.refresh(")
    assert idx_trigger < idx_refresh
    assert idx_consumed < idx_refresh
