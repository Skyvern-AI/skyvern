"""The shared decision behind every seam that discloses a page a credential run left."""

from __future__ import annotations

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
