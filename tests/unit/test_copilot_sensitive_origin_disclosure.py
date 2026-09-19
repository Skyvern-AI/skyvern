"""The shared decision behind every seam that discloses a page a credential run left."""

from __future__ import annotations

import pytest

from skyvern.forge.sdk.copilot.runtime import (
    OriginRunRedactionRegistry,
    clear_sensitive_origin_page_taint,
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
from tests.unit.copilot_test_helpers import make_copilot_ctx

PASSWORD = "Sp1r!t-Level-2026"
EARLIER_OTP = "917204"


def _ctx_after_run(run_id: str = "wr_credential") -> object:
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


def test_a_named_navigation_drops_the_page_and_its_run_attribution() -> None:
    ctx = _ctx_after_run()

    clear_sensitive_origin_page_taint(ctx)

    assert "pbs_run" not in ctx.sensitive_origin_browser_session_ids
    assert sensitive_origin_runs_for_session(ctx, "pbs_run") == set()


def test_only_a_navigation_to_another_document_lifts_the_withholding() -> None:
    """Every route that lifts the taint goes through this decision: a fragment hop keeps the
    sensitive DOM on screen, and an unknown starting URL is not evidence of leaving it."""
    from skyvern.forge.sdk.copilot.runtime import clear_sensitive_origin_page_taint_after_navigation

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
        assert clear_sensitive_origin_page_taint_after_navigation(ctx, source_url=source, result_url=result) is False
        assert "pbs_run" in ctx.sensitive_origin_browser_session_ids, (source, result)

    ctx = _ctx_after_run()
    assert clear_sensitive_origin_page_taint_after_navigation(
        ctx, source_url=page, result_url="https://elsewhere.example/"
    )
    assert "pbs_run" not in ctx.sensitive_origin_browser_session_ids


@pytest.mark.asyncio
async def test_the_navigate_tool_lifts_the_withholding_only_when_it_leaves_the_document(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import AsyncMock

    from skyvern.forge.sdk.copilot.tools import mcp_hooks

    page = "https://portal.example/account?tab=billing"
    live = {"url": page}
    monkeypatch.setattr(mcp_hooks, "_bind_login_credential_for_observed_url", AsyncMock())
    monkeypatch.setattr(mcp_hooks, "_capture_post_interaction_screenshot", AsyncMock(return_value=False))

    async def live_url(_ctx: object) -> str:
        return live["url"]

    monkeypatch.setattr(mcp_hooks, "_live_working_page_url", live_url)

    async def navigate(ctx: object, lands_on: str, reported: str | None = None) -> dict:
        live["url"] = page
        assert await mcp_hooks._navigate_pre_hook({"url": lands_on}, ctx) is None
        live["url"] = lands_on
        # The adapter hands the hook a secret-scrubbed result, so the reported URL can differ from the page.
        return await mcp_hooks._navigate_post_hook({"ok": True, "data": {"url": reported or lands_on}}, {}, ctx)

    fragment_ctx = _ctx_after_run()
    fragment = await navigate(fragment_ctx, page + "#top")
    left_ctx = _ctx_after_run()
    left = await navigate(left_ctx, "https://elsewhere.example/")
    # A registered value in the URL is redacted in the reported URL only; the hop is still same-document.
    page = "https://portal.example/u/alice-4412/account"
    redacted_ctx = _ctx_after_run()
    redacted = await navigate(redacted_ctx, page + "#top", reported="https://portal.example/u/****/account#top")

    assert fragment["ok"] is False and "url" not in fragment and "portal.example" not in str(fragment)
    assert "pbs_run" in fragment_ctx.sensitive_origin_browser_session_ids
    assert redacted["ok"] is False and "pbs_run" in redacted_ctx.sensitive_origin_browser_session_ids
    assert left["ok"] is True and "pbs_run" not in left_ctx.sensitive_origin_browser_session_ids
    # The withheld page's URL is compared and dropped, never recorded as a scouting fact.
    assert all(step.get("source_url") is None for step in left_ctx.scout_trajectory)
    assert left_ctx.pending_taint_source_urls == {} and fragment_ctx.pending_taint_source_urls == {}


@pytest.mark.asyncio
async def test_concurrent_navigations_on_two_tainted_browsers_judge_each_by_its_own_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The before-URL is kept per browser: a fragment hop in one browser is never compared against the
    other browser's page, so it cannot borrow that page's difference to clear its own taint."""
    from unittest.mock import AsyncMock

    from skyvern.forge.sdk.copilot.runtime import bound_call_browser_session
    from skyvern.forge.sdk.copilot.tools import mcp_hooks

    pages = {"pbs_run": "https://portal.example/account", "pbs_other": "https://other.example/statement"}
    ctx = _ctx_after_run()
    record_sensitive_origin_run_taint(ctx, workflow_run_id="wr_other", session_id="pbs_other")
    monkeypatch.setattr(mcp_hooks, "_bind_login_credential_for_observed_url", AsyncMock())
    monkeypatch.setattr(mcp_hooks, "_capture_post_interaction_screenshot", AsyncMock(return_value=False))

    async def live_url(inner: object) -> str:
        from skyvern.forge.sdk.copilot.runtime import effective_browser_session_id

        return pages[effective_browser_session_id(inner) or ""]

    monkeypatch.setattr(mcp_hooks, "_live_working_page_url", live_url)

    # Both pre-hooks run before either post-hook, as two concurrent calls would.
    with bound_call_browser_session("pbs_other"):
        await mcp_hooks._navigate_pre_hook({"url": pages["pbs_other"] + "#x"}, ctx)
    with bound_call_browser_session("pbs_run"):
        await mcp_hooks._navigate_pre_hook({"url": "https://elsewhere.example/"}, ctx)
    pages["pbs_other"] += "#x"
    pages["pbs_run"] = "https://elsewhere.example/"
    with bound_call_browser_session("pbs_other"):
        hop = await mcp_hooks._navigate_post_hook({"ok": True, "data": {"url": pages["pbs_other"]}}, {}, ctx)
    with bound_call_browser_session("pbs_run"):
        left = await mcp_hooks._navigate_post_hook({"ok": True, "data": {"url": "https://elsewhere.example/"}}, {}, ctx)

    assert hop["ok"] is False and "pbs_other" in ctx.sensitive_origin_browser_session_ids
    assert left["ok"] is True and "pbs_run" not in ctx.sensitive_origin_browser_session_ids
    assert ctx.pending_taint_source_urls == {}
