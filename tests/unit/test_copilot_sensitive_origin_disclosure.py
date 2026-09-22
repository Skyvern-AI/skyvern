"""The shared decision behind every seam that discloses a page a credential run left."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from skyvern.forge.sdk.copilot.context import CopilotContext
from skyvern.forge.sdk.copilot.runtime import (
    OriginRunRedactionRegistry,
    bound_call_browser_session,
    clear_sensitive_origin_page_taint,
    clear_sensitive_origin_page_taint_after_navigation,
    record_sensitive_origin_run_taint,
    register_sensitive_origin_run_lease,
    release_sensitive_origin_run_lease,
    sensitive_origin_page_facts_withheld,
    sensitive_origin_runs_for_session,
)
from skyvern.forge.sdk.copilot.secret_scrub import (
    clear_session_scrub_values,
    origin_runs_bound_to_scrubber,
    registered_scrub_values,
)
from skyvern.forge.sdk.copilot.tools import mcp_hooks
from tests.unit.copilot_test_helpers import (
    FakeTabbedBrowserState,
    make_copilot_ctx,
    patch_browser_tab_count,
    patch_browser_tabs,
)

PASSWORD = "Sp1r!t-Level-2026"
EARLIER_OTP = "917204"


def _ctx_after_run(run_id: str = "wr_credential") -> CopilotContext:
    ctx = make_copilot_ctx(browser_session_id="pbs_run")
    clear_session_scrub_values("pbs_run")
    ctx.last_run_blocks_workflow_run_id = run_id
    ctx.last_run_blocks_browser_session_id = "pbs_run"
    record_sensitive_origin_run_taint(ctx, workflow_run_id=run_id, session_id="pbs_run")
    ctx.origin_run_redaction_registry = OriginRunRedactionRegistry(
        run_id, {"password": PASSWORD}, contains_sensitive_values=True, contains_all_sensitive_values=True
    )
    return ctx


def test_a_terminal_run_with_a_complete_registry_discloses_and_binds_its_values() -> None:
    ctx = _ctx_after_run()

    assert sensitive_origin_page_facts_withheld(ctx, "wr_credential") is False
    assert origin_runs_bound_to_scrubber(ctx) == {"wr_credential"}
    assert PASSWORD in registered_scrub_values(ctx)


def test_an_active_run_withholds_before_any_value_is_registered() -> None:
    """The lease is checked first: a run still writing to the page binds nothing to the scrubber."""
    ctx = _ctx_after_run()
    register_sensitive_origin_run_lease(ctx, workflow_run_id="wr_credential", session_id="pbs_run")

    assert sensitive_origin_page_facts_withheld(ctx, "wr_credential") is True
    assert origin_runs_bound_to_scrubber(ctx) == set()
    assert PASSWORD not in registered_scrub_values(ctx)

    release_sensitive_origin_run_lease(ctx, workflow_run_id="wr_credential")
    assert sensitive_origin_page_facts_withheld(ctx, "wr_credential") is False


def test_an_earlier_run_on_the_same_page_that_never_bound_its_values_keeps_the_page_withheld() -> None:
    """Run A tainted this page and ended without completing its registry; run B completed on the
    same page. B's complete registry says nothing about what A typed, so the page stays withheld."""
    ctx = _ctx_after_run("wr_b")
    record_sensitive_origin_run_taint(ctx, workflow_run_id="wr_a", session_id="pbs_run")

    assert sensitive_origin_runs_for_session(ctx, "pbs_run") == {"wr_a", "wr_b"}
    assert sensitive_origin_page_facts_withheld(ctx, "wr_b") is True
    assert origin_runs_bound_to_scrubber(ctx) == {"wr_b"}


def test_an_earlier_run_that_was_bound_no_longer_blocks_the_page() -> None:
    ctx = _ctx_after_run("wr_b")
    record_sensitive_origin_run_taint(ctx, workflow_run_id="wr_a", session_id="pbs_run")
    ctx.origin_runs_bound_to_scrubber.add("wr_a")

    assert sensitive_origin_page_facts_withheld(ctx, "wr_b") is False


@pytest.mark.asyncio
async def test_a_named_navigation_drops_the_page_and_its_run_attribution(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_browser_tab_count(monkeypatch, 1)
    ctx = _ctx_after_run()

    assert await clear_sensitive_origin_page_taint(ctx) is True

    assert "pbs_run" not in ctx.sensitive_origin_browser_session_ids
    assert sensitive_origin_runs_for_session(ctx, "pbs_run") == set()


@pytest.mark.asyncio
@pytest.mark.parametrize("open_tabs", [2, 0, None])
async def test_the_taint_outlives_one_tabs_navigation_while_other_tabs_or_no_browser_answer(
    monkeypatch: pytest.MonkeyPatch, open_tabs: int | None
) -> None:
    """The taint is session-wide and a navigation replaces one tab's document; with another tab
    still up, or a browser that cannot be read (no answer, or a closed context listing no tabs),
    the sensitive DOM may still be on screen."""
    patch_browser_tab_count(monkeypatch, open_tabs)
    ctx = _ctx_after_run()

    assert await clear_sensitive_origin_page_taint(ctx) is False

    assert "pbs_run" in ctx.sensitive_origin_browser_session_ids
    assert sensitive_origin_runs_for_session(ctx, "pbs_run") == {"wr_credential"}


@pytest.mark.asyncio
async def test_only_a_navigation_to_another_document_lifts_the_withholding(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every route that lifts the taint goes through this decision: a fragment hop keeps the
    sensitive DOM on screen, and an unknown starting URL is not evidence of leaving it."""
    patch_browser_tab_count(monkeypatch, 1)
    page = "https://portal.example/account?tab=billing"
    # The last case is a URL that is not a string at all, which a mocked or broken page read can
    # hand back: it keeps the taint instead of reaching the URL parser.
    unreadable = object()
    for source, result in (
        (page, page + "#top"),
        (None, "https://elsewhere.example/"),
        (page, None),
        (unreadable, "https://elsewhere.example/"),
    ):
        ctx = _ctx_after_run()
        assert (
            await clear_sensitive_origin_page_taint_after_navigation(ctx, source_url=source, result_url=result) is False
        )
        assert "pbs_run" in ctx.sensitive_origin_browser_session_ids, (source, result)

    ctx = _ctx_after_run()
    assert await clear_sensitive_origin_page_taint_after_navigation(
        ctx, source_url=page, result_url="https://elsewhere.example/"
    )
    assert "pbs_run" not in ctx.sensitive_origin_browser_session_ids


async def _navigate(
    ctx: CopilotContext, browser: FakeTabbedBrowserState, lands_on: str, reported: str | None = None
) -> dict[str, Any]:
    """One navigate_browser call on the browser's selected tab, pre-hook through post-hook."""
    assert await mcp_hooks._navigate_pre_hook({"url": lands_on}, ctx) is None
    assert browser.active is not None
    browser.active.url = lands_on
    # The adapter hands the hook a secret-scrubbed result, so the reported URL can differ from the page.
    return await mcp_hooks._navigate_post_hook({"ok": True, "data": {"url": reported or lands_on}}, {}, ctx)


def _silence_navigate_side_effects(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mcp_hooks, "_bind_login_credential_for_observed_url", AsyncMock())
    monkeypatch.setattr(mcp_hooks, "_capture_post_interaction_screenshot", AsyncMock(return_value=False))


@pytest.mark.asyncio
async def test_the_navigate_tool_lifts_the_withholding_only_when_it_leaves_the_document(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = "https://portal.example/account?tab=billing"
    _silence_navigate_side_effects(monkeypatch)

    fragment_ctx, fragment_browser = _ctx_after_run(), FakeTabbedBrowserState(page)
    patch_browser_tabs(monkeypatch, fragment_browser)
    fragment = await _navigate(fragment_ctx, fragment_browser, page + "#top")
    left_ctx, left_browser = _ctx_after_run(), FakeTabbedBrowserState(page)
    patch_browser_tabs(monkeypatch, left_browser)
    left = await _navigate(left_ctx, left_browser, "https://elsewhere.example/")
    # A registered value in the URL is redacted in the reported URL only; the hop is still same-document.
    page = "https://portal.example/u/alice-4412/account"
    redacted_ctx, redacted_browser = _ctx_after_run(), FakeTabbedBrowserState(page)
    patch_browser_tabs(monkeypatch, redacted_browser)
    redacted = await _navigate(
        redacted_ctx, redacted_browser, page + "#top", reported="https://portal.example/u/****/account#top"
    )

    assert fragment["ok"] is False and "url" not in fragment and "portal.example" not in str(fragment)
    assert "pbs_run" in fragment_ctx.sensitive_origin_browser_session_ids
    assert redacted["ok"] is False and "pbs_run" in redacted_ctx.sensitive_origin_browser_session_ids
    assert left["ok"] is True and "pbs_run" not in left_ctx.sensitive_origin_browser_session_ids
    # The withheld page's URL is compared and dropped, never recorded as a scouting fact.
    assert all(step.get("source_url") is None for step in left_ctx.scout_trajectory)
    assert left_ctx.pending_taint_sources == {}


@pytest.mark.asyncio
async def test_concurrent_navigations_on_two_tainted_browsers_judge_each_by_its_own_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The before-URL is kept per browser: a fragment hop in one browser is never compared against the
    other browser's page, so it cannot borrow that page's difference to clear its own taint."""
    browsers = {
        "pbs_run": FakeTabbedBrowserState("https://portal.example/account"),
        "pbs_other": FakeTabbedBrowserState("https://other.example/statement"),
    }
    ctx = _ctx_after_run()
    record_sensitive_origin_run_taint(ctx, workflow_run_id="wr_other", session_id="pbs_other")
    patch_browser_tabs(monkeypatch, browsers)
    _silence_navigate_side_effects(monkeypatch)

    # Both pre-hooks run before either post-hook, as two concurrent calls would.
    with bound_call_browser_session("pbs_other"):
        await mcp_hooks._navigate_pre_hook({"url": "https://other.example/statement#x"}, ctx)
    with bound_call_browser_session("pbs_run"):
        await mcp_hooks._navigate_pre_hook({"url": "https://elsewhere.example/"}, ctx)
    browsers["pbs_other"].tabs[0].url = "https://other.example/statement#x"
    browsers["pbs_run"].tabs[0].url = "https://elsewhere.example/"
    with bound_call_browser_session("pbs_other"):
        hop = await mcp_hooks._navigate_post_hook(
            {"ok": True, "data": {"url": "https://other.example/statement#x"}}, {}, ctx
        )
    with bound_call_browser_session("pbs_run"):
        left = await mcp_hooks._navigate_post_hook({"ok": True, "data": {"url": "https://elsewhere.example/"}}, {}, ctx)

    assert hop["ok"] is False and "pbs_other" in ctx.sensitive_origin_browser_session_ids
    assert left["ok"] is True and "pbs_run" not in ctx.sensitive_origin_browser_session_ids
    assert set(ctx.pending_taint_sources) == {"pbs_other"}


@pytest.mark.asyncio
async def test_the_source_a_hold_keeps_belongs_to_the_tab_that_is_navigated(monkeypatch: pytest.MonkeyPatch) -> None:
    """The multi-tab hold keeps the sensitive page's URL to judge the next navigation by, but only
    for the tab it was read from: closing that tab and moving another tab's fragment is not leaving
    the sensitive document, and a second run's page replaces what an earlier hold kept."""
    sensitive, other = "https://portal.example/account", "https://portal.example/help"
    _silence_navigate_side_effects(monkeypatch)

    # Close the tab the hold was read from, then hop the surviving tab's fragment.
    ctx, browser = _ctx_after_run(), FakeTabbedBrowserState(sensitive, other)
    patch_browser_tabs(monkeypatch, browser)
    held = await _navigate(ctx, browser, "https://elsewhere.example/")
    browser.close(browser.tabs[0])
    hop = await _navigate(ctx, browser, other + "#top")
    assert held["ok"] is False and "2 tabs" in held["error"] and "(index 1)" in held["error"]
    assert hop["ok"] is False and "pbs_run" in ctx.sensitive_origin_browser_session_ids
    left = await _navigate(ctx, browser, "https://elsewhere.example/")
    assert left["ok"] is True and "pbs_run" not in ctx.sensitive_origin_browser_session_ids

    # A second sensitive run taints the browser again while the first hold's source is staged.
    ctx, browser = _ctx_after_run(), FakeTabbedBrowserState(sensitive, other)
    patch_browser_tabs(monkeypatch, browser)
    await _navigate(ctx, browser, "https://elsewhere.example/")
    browser.tabs[0].url = "https://portal.example/statements"
    record_sensitive_origin_run_taint(ctx, workflow_run_id="wr_second", session_id="pbs_run")
    browser.close(browser.tabs[1])
    hop = await _navigate(ctx, browser, "https://portal.example/statements#q1")
    assert hop["ok"] is False and "pbs_run" in ctx.sensitive_origin_browser_session_ids

    # Re-navigating to the page the hold landed on lifts once the other tab is closed, and a failed
    # navigation in between does not lose the source the hold kept.
    ctx, browser = _ctx_after_run(), FakeTabbedBrowserState(sensitive, other)
    patch_browser_tabs(monkeypatch, browser)
    await _navigate(ctx, browser, "https://elsewhere.example/")
    assert await mcp_hooks._navigate_pre_hook({"url": "https://down.example/"}, ctx) is None
    failed = await mcp_hooks._navigate_post_hook({"ok": False, "error": "net::ERR_NAME_NOT_RESOLVED"}, {}, ctx)
    assert failed["ok"] is False and "pbs_run" in ctx.sensitive_origin_browser_session_ids
    browser.close(browser.tabs[1])
    again = await _navigate(ctx, browser, "https://elsewhere.example/")
    assert again["ok"] is True and "pbs_run" not in ctx.sensitive_origin_browser_session_ids
    assert ctx.pending_taint_sources == {}


@pytest.mark.asyncio
async def test_an_unreadable_page_before_a_navigation_drops_the_source_an_earlier_hold_kept(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A source staged for a since-closed tab must not judge the next navigation: with the browser
    unreadable at the pre-hook, a fragment hop on the surviving sensitive tab keeps the hold."""
    sensitive, other = "https://portal.example/account", "https://portal.example/help"
    _silence_navigate_side_effects(monkeypatch)
    ctx, browser = _ctx_after_run(), FakeTabbedBrowserState(sensitive, other)
    patch_browser_tabs(monkeypatch, browser)
    held = await _navigate(ctx, browser, "https://elsewhere.example/")
    assert held["ok"] is False and "pbs_run" in ctx.pending_taint_sources
    browser.close(browser.tabs[0])

    patch_browser_tabs(monkeypatch, None)
    assert await mcp_hooks._navigate_pre_hook({"url": other + "#top"}, ctx) is None
    assert ctx.pending_taint_sources == {}
    patch_browser_tabs(monkeypatch, browser)
    browser.tabs[1].url = other + "#top"
    hop = await mcp_hooks._navigate_post_hook({"ok": True, "data": {"url": other + "#top"}}, {}, ctx)

    assert hop["ok"] is False and "pbs_run" in ctx.sensitive_origin_browser_session_ids


def test_a_run_id_inherited_without_its_registry_keeps_the_page_withheld() -> None:
    """The registry a run binds while dispatching is what licenses disclosure, never the id alone."""
    ctx = _ctx_after_run()
    ctx.origin_run_redaction_registry = None

    assert sensitive_origin_page_facts_withheld(ctx, "wr_credential") is True
    assert origin_runs_bound_to_scrubber(ctx) == set()
    assert PASSWORD not in registered_scrub_values(ctx)
