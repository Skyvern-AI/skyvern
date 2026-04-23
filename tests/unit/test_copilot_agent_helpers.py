"""Tests for agent.py helpers that are hard to drive through run_copilot_agent."""

from __future__ import annotations

import json
from types import SimpleNamespace
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


def _fake_run_result(payload: dict) -> SimpleNamespace:
    """Minimal shim for ``RunResultStreaming`` — extract_final_text reads ``final_output``."""
    return SimpleNamespace(final_output=json.dumps(payload), new_items=[])


def _chat_request() -> SimpleNamespace:
    return SimpleNamespace(
        workflow_id="wf-1",
        workflow_permanent_id="wfp-1",
        workflow_copilot_chat_id="chat-1",
    )


class TestTranslateToAgentResultGating:
    """Covers the three SKY-9143 invariants that live in _translate_to_agent_result."""

    def test_inline_replace_workflow_resets_test_ok_after_prior_pass(self, monkeypatch) -> None:
        # A prior run_blocks test passed for the old workflow (ctx.last_test_ok=True,
        # ctx.last_workflow=old_wf). The agent then emits inline REPLACE_WORKFLOW
        # with a different yaml. The translate helper must invalidate the prior
        # test result so _verified_workflow_or_none rejects the untested REPLACE.
        from skyvern.forge.sdk.copilot import agent as agent_module

        old_wf = SimpleNamespace(name="old")
        new_wf = SimpleNamespace(name="new-from-replace")
        monkeypatch.setattr(
            "skyvern.forge.sdk.copilot.tools._process_workflow_yaml",
            lambda **kwargs: new_wf,
        )

        ctx = _ctx(last_workflow=old_wf, last_workflow_yaml="old: yaml", last_test_ok=True)
        result = _fake_run_result(
            {"type": "REPLACE_WORKFLOW", "user_response": "Here you go.", "workflow_yaml": "new: yaml"}
        )
        agent_result = agent_module._translate_to_agent_result(
            result, ctx, global_llm_context=None, chat_request=_chat_request(), organization_id="org-1"
        )

        assert ctx.last_test_ok is None
        assert ctx.last_workflow is new_wf
        # The REPLACE yaml itself (not the stale snapshot) must land on ctx;
        # otherwise a future code path that reads last_workflow_yaml would
        # see a string that no longer matches last_workflow.
        assert ctx.last_workflow_yaml == "new: yaml"
        assert agent_result.updated_workflow is None
        assert agent_result.workflow_yaml is None
        assert agent_result.response_type == "REPLACE_WORKFLOW"

    def test_inline_replace_with_invalid_yaml_keeps_prior_pass(self, monkeypatch) -> None:
        # _process_workflow_yaml raising on a malformed REPLACE must leave
        # ctx untouched — no spurious last_test_ok reset, no workflow swap —
        # so a prior tested workflow remains available.
        import yaml as yaml_mod

        from skyvern.forge.sdk.copilot import agent as agent_module

        tested_wf = SimpleNamespace(name="tested")

        def boom(**kwargs):
            raise yaml_mod.YAMLError("mangled yaml")

        monkeypatch.setattr("skyvern.forge.sdk.copilot.tools._process_workflow_yaml", boom)

        ctx = _ctx(last_workflow=tested_wf, last_workflow_yaml="tested: yaml", last_test_ok=True)
        result = _fake_run_result(
            {"type": "REPLACE_WORKFLOW", "user_response": "here", "workflow_yaml": "::: not yaml"}
        )
        agent_result = agent_module._translate_to_agent_result(
            result, ctx, global_llm_context=None, chat_request=_chat_request(), organization_id="org-1"
        )

        assert ctx.last_workflow is tested_wf
        assert ctx.last_workflow_yaml == "tested: yaml"
        assert ctx.last_test_ok is True
        assert agent_result.updated_workflow is tested_wf
        assert "validation error" in agent_result.user_response.lower()

    def test_ask_question_preserves_model_specific_question(self) -> None:
        # The new prompt instructs the model to stop and ASK_QUESTION when it
        # cannot test an edit. Row-3 of _rewrite_failed_test_response would
        # clobber that specific unblocker with "Could you share more context";
        # the resp_type==ASK_QUESTION guard must skip the rewrite.
        from skyvern.forge.sdk.copilot import agent as agent_module

        ctx = _ctx(
            last_update_block_count=1,
            last_test_ok=None,
            last_workflow=SimpleNamespace(name="drafted"),
            last_workflow_yaml="drafted: yaml",
        )
        specific_question = "I need credentials for site.example — can you link one in Settings?"
        result = _fake_run_result({"type": "ASK_QUESTION", "user_response": specific_question})
        agent_result = agent_module._translate_to_agent_result(
            result, ctx, global_llm_context=None, chat_request=_chat_request(), organization_id="org-1"
        )

        assert agent_result.user_response == specific_question
        # Even ASK_QUESTION must obey the strict gate — no verified workflow this turn.
        assert agent_result.updated_workflow is None
        assert agent_result.response_type == "ASK_QUESTION"

    def test_reply_still_rewrites_after_failed_test(self) -> None:
        # Guard rail for the above: a plain REPLY after a failed test must
        # still flow through the "test failed" rewrite so we don't regress
        # the original SKY-9143 behavior.
        from skyvern.forge.sdk.copilot import agent as agent_module

        ctx = _ctx(
            last_update_block_count=2,
            last_test_ok=False,
            last_test_failure_reason="Failed to navigate to url https://bad.example.",
            last_failure_category_top="NAVIGATION_FAILURE",
        )
        result = _fake_run_result({"type": "REPLY", "user_response": "All done — your workflow is ready."})
        agent_result = agent_module._translate_to_agent_result(
            result, ctx, global_llm_context=None, chat_request=_chat_request(), organization_id="org-1"
        )

        assert "test failed" in agent_result.user_response.lower()
        assert "All done" not in agent_result.user_response
        assert agent_result.updated_workflow is None

    def test_inline_replace_workflow_wraps_block_goals_with_user_message(self, monkeypatch) -> None:
        # SKY-9174 parity: update_and_run_blocks_tool wraps block goals with
        # the user's chat message as big-goal context. The REPLACE_WORKFLOW
        # inline path must do the same, otherwise the untested yaml latches
        # onto ctx without user-intent framing and any downstream block run
        # hits the verifier-on-confirmation-surface bug this PR fixes.
        from skyvern.forge.sdk.copilot import agent as agent_module

        captured: dict[str, str] = {}

        def fake_process(**kwargs):
            captured["yaml"] = kwargs["workflow_yaml"]
            return SimpleNamespace(name="new-wf")

        def fake_wrap(workflow_yaml: str, user_message: str) -> str:
            return f"WRAPPED::{user_message}::{workflow_yaml}"

        monkeypatch.setattr("skyvern.forge.sdk.copilot.tools._process_workflow_yaml", fake_process)
        monkeypatch.setattr("skyvern.forge.sdk.copilot.agent.wrap_block_goals", fake_wrap)

        ctx = _ctx(user_message="Submit a contact form on example.com.")
        result = _fake_run_result(
            {"type": "REPLACE_WORKFLOW", "user_response": "Here you go.", "workflow_yaml": "raw: yaml"}
        )
        agent_module._translate_to_agent_result(
            result, ctx, global_llm_context=None, chat_request=_chat_request(), organization_id="org-1"
        )

        assert captured["yaml"] == "WRAPPED::Submit a contact form on example.com.::raw: yaml"
        assert ctx.last_workflow_yaml == "WRAPPED::Submit a contact form on example.com.::raw: yaml"


class TestCredentialRefusalReachesAgent:
    """Prove the SKY-9189 refusal rule is actually delivered to the agent.

    `run_copilot_agent` constructs the openai-agents SDK `Agent(...)` with
    `instructions=_build_system_prompt(...)` and `tools=list(NATIVE_TOOLS)`.
    A behavior test would require patching the agent loop and is fragile; a
    construction test (rule text flows through the exact helpers the route
    uses) is deterministic and catches both prompt and tool-surface drift.
    """

    def test_build_system_prompt_carries_refusal_clause(self) -> None:
        from skyvern.forge.sdk.copilot.agent import _build_system_prompt

        prompt = _build_system_prompt(tool_usage_guide="", security_rules="")

        assert "CREDENTIAL HANDLING - CRITICAL" in prompt
        assert "DO NOT PROVIDE RAW LOGIN/PASSWORD" in prompt
        assert "MUST NOT build, update, or run a workflow" in prompt
        assert "redacted from the outbound client stream" not in prompt

    def test_native_tools_carry_refusal_reference(self) -> None:
        import re

        from skyvern.forge.sdk.copilot.tools import NATIVE_TOOLS

        targets = {"run_blocks_and_collect_debug", "update_and_run_blocks"}
        matched = {tool.name for tool in NATIVE_TOOLS if tool.name in targets}
        assert matched == targets, f"missing tools in NATIVE_TOOLS: {targets - matched}"

        cross_ref = re.compile(r"CREDENTIAL\s+HANDLING refusal rule")
        for tool in NATIVE_TOOLS:
            if tool.name not in targets:
                continue
            desc = tool.description
            assert "redacted from" not in desc, f"{tool.name} still claims redaction"
            assert "you may pass it via" not in desc, f"{tool.name} still permits inline secrets"
            assert cross_ref.search(desc), f"{tool.name} missing refusal cross-reference"
