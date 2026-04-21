"""Tests for agent.py helpers that are hard to drive through run_copilot_agent."""

from __future__ import annotations

from unittest.mock import MagicMock


def _ctx(**overrides):
    from skyvern.forge.sdk.copilot.context import CopilotContext

    defaults = dict(
        organization_id="org-1",
        workflow_id="wf-1",
        workflow_permanent_id="wfp-1",
        workflow_yaml="",
        browser_session_id=None,
        stream=MagicMock(),
    )
    defaults.update(overrides)
    return CopilotContext(**defaults)


class TestFailedTestResponseNormalization:
    def test_rewrite_failed_test_response_avoids_success_language(self) -> None:
        from skyvern.forge.sdk.copilot.agent import _rewrite_failed_test_response

        ctx = _ctx(
            last_update_block_count=2,
            last_test_ok=False,
            last_test_failure_reason=(
                "Failed to navigate to url https://bad.example. "
                "Error: net::ERR_NAME_NOT_RESOLVED Call log: navigating..."
            ),
        )
        rewritten = _rewrite_failed_test_response("The workflow was successfully created.", ctx)

        assert "successfully created" not in rewritten.lower()
        assert "draft workflow with 2 blocks" in rewritten
        assert "test failed" in rewritten.lower()
        assert "Call log:" not in rewritten

    def test_failed_run_does_not_clear_last_workflow_state(self) -> None:
        from skyvern.forge.sdk.copilot.tools import _record_run_blocks_result

        sentinel_workflow = object()
        ctx = MagicMock()
        ctx.last_workflow = sentinel_workflow
        ctx.last_test_ok = None
        ctx.last_test_failure_reason = None

        _record_run_blocks_result(
            ctx,
            {
                "ok": False,
                "data": {
                    "blocks": [
                        {
                            "label": "open_website",
                            "failure_reason": "net::ERR_NAME_NOT_RESOLVED",
                        }
                    ]
                },
            },
        )

        assert ctx.last_workflow is sentinel_workflow
        assert ctx.last_test_ok is False
        assert ctx.last_test_failure_reason == "net::ERR_NAME_NOT_RESOLVED"

    def test_rewrite_includes_navigation_follow_up_when_category_matches(self) -> None:
        from skyvern.forge.sdk.copilot.agent import _rewrite_failed_test_response

        ctx = _ctx(
            last_update_block_count=1,
            last_test_ok=False,
            last_test_failure_reason="Failed to navigate to url https://bad.example.",
            last_failure_category_top="NAVIGATION_FAILURE",
        )
        rewritten = _rewrite_failed_test_response("done", ctx)

        assert "test failed" in rewritten.lower()
        assert "confirm the url" in rewritten.lower()

    def test_rewrite_untested_edit_asks_for_more_context(self) -> None:
        # SKY-9143 row 3: agent updated YAML without testing. The reply must
        # not promise the backend can re-run a durable draft — the restore
        # helper rolled it back and there is nothing to re-test next turn.
        from skyvern.forge.sdk.copilot.agent import _rewrite_failed_test_response

        sentinel_workflow = object()
        ctx = _ctx(
            last_update_block_count=1,
            last_test_ok=None,
            last_workflow=sentinel_workflow,
        )
        rewritten = _rewrite_failed_test_response("Here's the updated YAML.", ctx)

        assert "drafted an update" in rewritten.lower()
        assert "run it" not in rewritten.lower()
        assert "more context" in rewritten.lower() or "clarify" in rewritten.lower()

    def test_rewrite_passes_through_when_no_update_or_failure(self) -> None:
        from skyvern.forge.sdk.copilot.agent import _rewrite_failed_test_response

        ctx = _ctx()
        original = "Let me know what you want to build."
        assert _rewrite_failed_test_response(original, ctx) == original


class TestVerifiedWorkflowOrNone:
    """SKY-9143 strict invariant: a proposal surfaces only after a passing test this turn."""

    def _wf(self) -> object:
        return object()

    def test_passes_workflow_when_tested_successfully(self) -> None:
        from skyvern.forge.sdk.copilot.agent import _verified_workflow_or_none

        wf = self._wf()
        ctx = _ctx(last_workflow=wf, last_workflow_yaml="foo: bar", last_test_ok=True)
        assert _verified_workflow_or_none(ctx) == (wf, "foo: bar")

    def test_zeros_when_test_failed(self) -> None:
        from skyvern.forge.sdk.copilot.agent import _verified_workflow_or_none

        ctx = _ctx(last_workflow=self._wf(), last_workflow_yaml="foo: bar", last_test_ok=False)
        assert _verified_workflow_or_none(ctx) == (None, None)

    def test_zeros_when_untested_update(self) -> None:
        # Exactly the scenario where _record_workflow_update_result reset
        # last_test_ok to None after a standalone update_workflow or after
        # the agent edited post-failure without re-testing.
        from skyvern.forge.sdk.copilot.agent import _verified_workflow_or_none

        ctx = _ctx(last_workflow=self._wf(), last_workflow_yaml="foo: bar", last_test_ok=None)
        assert _verified_workflow_or_none(ctx) == (None, None)

    def test_zeros_when_no_last_workflow(self) -> None:
        from skyvern.forge.sdk.copilot.agent import _verified_workflow_or_none

        ctx = _ctx(last_workflow=None, last_test_ok=True)
        assert _verified_workflow_or_none(ctx) == (None, None)

    def test_zeros_on_suspicious_success(self) -> None:
        # _record_run_blocks_result sets last_test_ok=None when blocks ran ok
        # but produced no meaningful extraction data. Still an unverified
        # outcome; must not surface a proposal.
        from skyvern.forge.sdk.copilot.agent import _verified_workflow_or_none

        ctx = _ctx(
            last_workflow=self._wf(),
            last_workflow_yaml="foo: bar",
            last_test_ok=None,
            last_test_suspicious_success=True,
        )
        assert _verified_workflow_or_none(ctx) == (None, None)


class TestShouldRestorePersistedWorkflow:
    """SKY-9143: auto_accept=True must still restore when no proposal shipped."""

    def _result(self, *, persisted: bool, updated_workflow: object | None):
        r = MagicMock()
        r.workflow_was_persisted = persisted
        r.updated_workflow = updated_workflow
        return r

    def test_restores_when_no_proposal_even_under_auto_accept(self) -> None:
        from skyvern.forge.sdk.routes.workflow_copilot import _should_restore_persisted_workflow

        r = self._result(persisted=True, updated_workflow=None)
        assert _should_restore_persisted_workflow(True, r) is True

    def test_keeps_persisted_write_under_auto_accept_when_proposal_valid(self) -> None:
        from skyvern.forge.sdk.routes.workflow_copilot import _should_restore_persisted_workflow

        r = self._result(persisted=True, updated_workflow=object())
        assert _should_restore_persisted_workflow(True, r) is False

    def test_restores_when_not_auto_accept_and_persisted(self) -> None:
        from skyvern.forge.sdk.routes.workflow_copilot import _should_restore_persisted_workflow

        r = self._result(persisted=True, updated_workflow=object())
        assert _should_restore_persisted_workflow(False, r) is True

    def test_noop_when_nothing_was_persisted(self) -> None:
        from skyvern.forge.sdk.routes.workflow_copilot import _should_restore_persisted_workflow

        r = self._result(persisted=False, updated_workflow=None)
        assert _should_restore_persisted_workflow(True, r) is False
        assert _should_restore_persisted_workflow(False, r) is False
