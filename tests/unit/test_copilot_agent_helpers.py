"""Tests for agent.py helpers that are hard to drive through run_copilot_agent."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.forge.sdk.copilot import agent as agent_module
from skyvern.forge.sdk.copilot.config import CopilotConfig


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

    def test_pre_run_coverage_guard_uses_completion_contract(self) -> None:
        from skyvern.forge.sdk.copilot.tools import _pre_run_workflow_coverage_error

        ctx = _ctx(
            user_message="Go to https://the-internet.herokuapp.com/download and then download the first file.",
            request_policy=SimpleNamespace(completion_contract="complete when the download starts"),
            last_update_block_count=1,
            coverage_nudge_count=0,
        )

        error = _pre_run_workflow_coverage_error(ctx)

        assert error is not None
        assert "has not been run" in error
        assert ctx.coverage_nudge_count == 1

    def test_pre_run_coverage_guard_allows_single_final_action_contract(self) -> None:
        from skyvern.forge.sdk.copilot.tools import _pre_run_workflow_coverage_error

        ctx = _ctx(
            user_message="Click Delete. Your goal is complete when the Delete button disappears.",
            request_policy=SimpleNamespace(completion_contract="complete when the Delete button disappears"),
            last_update_block_count=1,
            coverage_nudge_count=0,
        )

        assert _pre_run_workflow_coverage_error(ctx) is None
        assert ctx.coverage_nudge_count == 0

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

    def test_rewrite_untested_draft_request_surfaces_explicit_unverified_copy(self) -> None:
        from skyvern.forge.sdk.copilot.agent import _rewrite_failed_test_response

        ctx = _ctx(
            allow_untested_workflow_draft=True,
            last_workflow=object(),
            last_workflow_yaml="title: drafted",
            last_update_block_count=2,
            last_test_ok=None,
        )
        rewritten = _rewrite_failed_test_response("Done.", ctx)

        assert "without testing it, as requested" in rewritten
        assert "not been verified end-to-end" in rewritten
        assert "successful" not in rewritten.lower()

    def test_rewrite_appends_keep_draft_affordance_when_draft_on_hand(self) -> None:
        from skyvern.forge.sdk.copilot.agent import _rewrite_failed_test_response

        ctx = _ctx(
            last_workflow=object(),
            last_workflow_yaml="title: drafted",
            last_update_block_count=2,
            last_test_ok=False,
            last_test_failure_reason="A verification challenge is preventing submission.",
        )
        rewritten = _rewrite_failed_test_response("done", ctx)

        assert "test failed" in rewritten.lower()
        assert "keep the draft" in rewritten.lower()


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


class TestSupersededAgentIntentGates:
    def test_agent_no_longer_owns_request_policy_classification(self) -> None:
        assert not hasattr(agent_module, "_user_requests_untested_workflow_draft")
        assert not hasattr(agent_module, "_extract_user_supplied_credential_ids")
        assert not hasattr(agent_module, "_credential_validation_result_for_user_message")


class TestRequestPolicyInputGuardrail:
    @pytest.mark.asyncio
    async def test_sdk_input_guardrail_computes_and_stores_request_policy(self, monkeypatch) -> None:
        from agents import GuardrailFunctionOutput, InputGuardrail
        from agents.run_context import RunContextWrapper

        from skyvern.forge.sdk.copilot.request_policy import RequestPolicy

        policy = RequestPolicy(
            testing_intent="skip_test",
            credential_input_kind="credential_name",
            credential_refs=["Saved Login"],
            allow_run_blocks=False,
        )
        build_request_policy = AsyncMock(return_value=policy)
        monkeypatch.setattr(agent_module, "build_request_policy", build_request_policy)
        ctx = _ctx()
        policy_inputs = agent_module.RequestPolicyGuardrailInputs(
            user_message="just draft without testing",
            workflow_yaml="workflow: yaml",
            chat_history_text="user: build the login workflow",
            global_llm_context="",
            organization_id="org-1",
            handler=object(),
            previous_user_message="build the login workflow",
        )

        guardrails = agent_module._build_copilot_input_guardrails(
            InputGuardrail,
            GuardrailFunctionOutput,
            policy_inputs=policy_inputs,
        )
        result = await guardrails[0].run(SimpleNamespace(), "input", RunContextWrapper(context=ctx))

        assert result.output.tripwire_triggered is False
        assert ctx.request_policy is policy
        assert ctx.allow_untested_workflow_draft is True
        assert "Draft the workflow requested earlier" in ctx.user_message
        assert "build the login workflow" in ctx.user_message
        assert result.output.output_info["policy_present"] is True
        assert result.output.output_info["testing_intent"] == "skip_test"
        assert "completion_contract" not in result.output.output_info
        build_request_policy.assert_awaited_once_with(
            user_message="just draft without testing",
            workflow_yaml="workflow: yaml",
            chat_history="user: build the login workflow",
            global_llm_context="",
            organization_id="org-1",
            handler=policy_inputs.handler,
        )

    @pytest.mark.asyncio
    async def test_sdk_input_guardrail_trips_after_computing_blocked_policy(self, monkeypatch) -> None:
        from agents import GuardrailFunctionOutput, InputGuardrail
        from agents.run_context import RunContextWrapper

        from skyvern.forge.sdk.copilot.request_policy import RequestPolicy

        policy = RequestPolicy(
            credential_input_kind="raw_secret",
            user_response_policy="ask_clarification",
            allow_update_workflow=False,
            allow_run_blocks=False,
            raw_secret_detected=True,
            clarification_reason="raw_secret",
            clarification_question="Do not paste raw credentials.",
        )
        monkeypatch.setattr(agent_module, "build_request_policy", AsyncMock(return_value=policy))
        ctx = _ctx()
        guardrails = agent_module._build_copilot_input_guardrails(
            InputGuardrail,
            GuardrailFunctionOutput,
            policy_inputs=agent_module.RequestPolicyGuardrailInputs(
                user_message="use password=hunter2",
                workflow_yaml="",
                chat_history_text="",
                global_llm_context="",
                organization_id="org-1",
                handler=None,
            ),
        )

        result = await guardrails[0].run(SimpleNamespace(), "input", RunContextWrapper(context=ctx))

        assert result.output.tripwire_triggered is True
        assert ctx.request_policy is policy
        assert result.output.output_info["credential_input_kind"] == "raw_secret"
        assert result.output.output_info["blocked"] is True
        assert "hunter2" not in str(result.output.output_info)


class TestShouldRestorePersistedWorkflow:
    """SKY-9143: auto_accept=True must still restore when no proposal shipped."""

    def _result(self, *, persisted: bool, updated_workflow: object | None):
        r = MagicMock()
        r.workflow_was_persisted = persisted
        r.updated_workflow = updated_workflow
        r.unvalidated = False
        r.cancelled = False
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


class TestBlockGoalMainGoal:
    def test_empty_message_returns_empty(self) -> None:
        assert agent_module._build_block_goal_main_goal("", chat_history_text="", global_llm_context=None) == ""
        assert agent_module._build_block_goal_main_goal("   ", chat_history_text="", global_llm_context=None) == ""

    def test_no_prior_context_returns_message_verbatim(self) -> None:
        goal = agent_module._build_block_goal_main_goal(
            user_message="Go to a site and extract the latest release notes.",
            chat_history_text="",
            global_llm_context=None,
        )

        assert goal == "Go to a site and extract the latest release notes."

    def test_no_prior_context_escapes_code_fences(self) -> None:
        goal = agent_module._build_block_goal_main_goal(
            user_message="Use ```this``` safely.",
            chat_history_text="",
            global_llm_context=None,
        )

        assert goal == "Use ` ` `this` ` ` safely."

    def test_structured_user_goal_added_as_prior_high_level_goal(self) -> None:
        global_context = json.dumps(
            {"user_goal": "Locate research about gravitational waves this week.", "workflow_state": "draft"}
        )

        goal = agent_module._build_block_goal_main_goal(
            user_message="I meant black holes",
            chat_history_text="",
            global_llm_context=global_context,
        )

        assert "Prior high-level goal:\nLocate research about gravitational waves this week." in goal
        assert "Latest user message:\nI meant black holes" in goal
        assert "Recent chat history" not in goal

    def test_plain_global_context_becomes_prior_high_level_goal(self) -> None:
        goal = agent_module._build_block_goal_main_goal(
            user_message="I meant black holes",
            chat_history_text="",
            global_llm_context="Legacy goal with ```fenced``` context.",
        )

        assert "Prior high-level goal:\nLegacy goal with ` ` `fenced` ` ` context." in goal
        assert "Latest user message:\nI meant black holes" in goal

    def test_chat_history_added_when_present(self) -> None:
        goal = agent_module._build_block_goal_main_goal(
            user_message="I meant black holes",
            chat_history_text="user: Search arXiv for recent papers.\nai: Drafted workflow.",
            global_llm_context=None,
        )

        assert "Recent chat history:\nuser: Search arXiv for recent papers." in goal
        assert "ai: Drafted workflow." in goal
        assert "Latest user message:\nI meant black holes" in goal

    def test_chat_history_and_latest_message_escape_code_fences(self) -> None:
        goal = agent_module._build_block_goal_main_goal(
            user_message="I meant ```black holes```",
            chat_history_text="user: Search ```arXiv``` for recent papers.",
            global_llm_context=None,
        )

        assert "user: Search ` ` `arXiv` ` ` for recent papers." in goal
        assert "Latest user message:\nI meant ` ` `black holes` ` `" in goal
        assert "```" not in goal

    def test_includes_both_structured_goal_and_chat_history(self) -> None:
        global_context = json.dumps({"user_goal": "Find papers about gravitational waves."})

        goal = agent_module._build_block_goal_main_goal(
            user_message="I meant neutron stars",
            chat_history_text="user: Find papers about gravitational waves.",
            global_llm_context=global_context,
        )

        assert "Prior high-level goal:\nFind papers about gravitational waves." in goal
        assert "Recent chat history:" in goal
        assert "Latest user message:\nI meant neutron stars" in goal
        assert goal.find("Prior high-level goal") < goal.find("Recent chat history") < goal.find("Latest user message")


class TestTranslateToAgentResultGating:
    """Covers the three SKY-9143 invariants that live in _translate_to_agent_result."""

    def test_inline_replace_workflow_resets_test_ok_after_prior_pass(self, monkeypatch) -> None:
        # A prior run_blocks test passed for the old workflow (ctx.last_test_ok=True,
        # ctx.last_workflow=old_wf). The agent then emits inline REPLACE_WORKFLOW
        # with a different yaml. The translate helper must invalidate the prior
        # test result so _verified_workflow_or_none rejects the untested REPLACE.
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

    def test_inline_replace_workflow_rejects_stale_block_metadata(self, monkeypatch) -> None:
        # Inline REPLACE_WORKFLOW bypasses _update_workflow, so it must also
        # reject a corrected workflow whose labels/titles still describe the
        # prior subject.
        process_mock = MagicMock(return_value=SimpleNamespace(name="new"))
        monkeypatch.setattr("skyvern.forge.sdk.copilot.tools._process_workflow_yaml", process_mock)

        prior_yaml = """
title: Count example.com topic alpha results
workflow_definition:
  blocks:
    - block_type: navigation
      label: search_topic_alpha
      title: Search Topic Alpha
      next_block_label: null
      navigation_goal: Search example.com for topic alpha.
"""
        submitted_yaml = """
title: Count example.com sample beta results
workflow_definition:
  blocks:
    - block_type: navigation
      label: search_topic_alpha
      title: Search Topic Alpha
      next_block_label: null
      navigation_goal: Search example.com for sample beta.
"""
        ctx = _ctx(workflow_yaml=prior_yaml, last_workflow_yaml=prior_yaml, last_workflow=object(), last_test_ok=True)
        result = _fake_run_result(
            {"type": "REPLACE_WORKFLOW", "user_response": "Here you go.", "workflow_yaml": submitted_yaml}
        )
        agent_result = agent_module._translate_to_agent_result(
            result, ctx, global_llm_context=None, chat_request=_chat_request(), organization_id="org-1"
        )

        process_mock.assert_not_called()
        assert "corrected block metadata still appears stale" in agent_result.user_response
        assert agent_result.updated_workflow is None
        assert agent_result.workflow_yaml is None

    def test_inline_replace_with_invalid_yaml_keeps_prior_pass(self, monkeypatch) -> None:
        # _process_workflow_yaml raising on a malformed REPLACE must leave
        # ctx untouched — no spurious last_test_ok reset, no workflow swap —
        # so a prior tested workflow remains available.
        import yaml as yaml_mod

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
        # The rewrite guard for ASK_QUESTION must hold: the agent's specific
        # clarifying question is not clobbered by the generic "share more
        # context" rewrite. SKY-9420 also drops any workflow under
        # ASK_QUESTION so an auto-accept user can't silently apply a partial.
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
        assert agent_result.updated_workflow is None
        assert agent_result.unvalidated is False
        assert agent_result.response_type == "ASK_QUESTION"

    def test_probable_site_block_ask_question_is_concise_and_proxy_aware(self) -> None:
        ctx = _ctx(
            last_test_ok=False,
            last_test_failure_reason="Skyvern failed to load the website. The page may have navigated unexpectedly.",
            probable_site_block_stop_nudge_count=1,
            effective_workflow_proxy_location="RESIDENTIAL",
        )
        verbose_response = (
            "Diagnostic recap:\n"
            "- I tried several workflow shapes with the same browser state.\n"
            '- global_llm_context: {"workflow_state": "many internal details"}\n'
            "- The final failure_reason was: Skyvern failed to load the website. "
            "The page may have navigated unexpectedly.\n"
            "- More implementation details that should not be user-facing.\n"
            "Would you like me to configure a proxy?"
        )
        result = _fake_run_result({"type": "ASK_QUESTION", "user_response": verbose_response})

        agent_result = agent_module._translate_to_agent_result(
            result, ctx, global_llm_context=None, chat_request=_chat_request(), organization_id="org-1"
        )

        assert agent_result.response_type == "ASK_QUESTION"
        assert len(agent_result.user_response.splitlines()) <= 8
        assert "global_llm_context" not in agent_result.user_response
        assert "configure a proxy" not in agent_result.user_response.lower()
        assert "Would you like me to whether" not in agent_result.user_response
        assert "different proxy location" in agent_result.user_response.lower()
        assert "US-CA" in agent_result.user_response
        assert "Skyvern failed to load the website. The page may have navigated unexpectedly." in (
            agent_result.user_response
        )
        assert "same IP/workflow shape" in agent_result.user_response

    def test_reply_still_rewrites_after_failed_test(self) -> None:
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
        assert agent_result.unvalidated is False

    def test_reply_after_failed_test_surfaces_unvalidated_wip_when_draft_on_hand(self) -> None:
        wf = SimpleNamespace(name="drafted")
        ctx = _ctx(
            last_workflow=wf,
            last_workflow_yaml="title: drafted",
            last_update_block_count=4,
            last_test_ok=False,
            last_test_failure_reason="A verification challenge is preventing submission.",
        )
        result = _fake_run_result({"type": "REPLY", "user_response": "Done."})
        agent_result = agent_module._translate_to_agent_result(
            result, ctx, global_llm_context=None, chat_request=_chat_request(), organization_id="org-1"
        )

        assert agent_result.updated_workflow is wf
        assert agent_result.workflow_yaml == "title: drafted"
        assert agent_result.unvalidated is True
        assert "test failed" in agent_result.user_response.lower()
        assert "keep the draft" in agent_result.user_response.lower()

    def test_reply_after_suspicious_success_surfaces_unvalidated_wip(self) -> None:
        wf = SimpleNamespace(name="drafted")
        ctx = _ctx(
            last_workflow=wf,
            last_workflow_yaml="title: drafted",
            last_update_block_count=2,
            last_test_ok=None,
            last_test_suspicious_success=True,
        )
        result = _fake_run_result({"type": "REPLY", "user_response": "Done."})
        agent_result = agent_module._translate_to_agent_result(
            result, ctx, global_llm_context=None, chat_request=_chat_request(), organization_id="org-1"
        )

        assert agent_result.updated_workflow is wf
        assert agent_result.unvalidated is True
        assert "review" in agent_result.user_response.lower()
        assert "accept" in agent_result.user_response.lower()
        assert "reject" in agent_result.user_response.lower()
        assert "discard" in agent_result.user_response.lower()

    def test_unvalidated_wip_reply_adds_proposal_affordance(self) -> None:
        wf = SimpleNamespace(name="drafted")
        ctx = _ctx(
            last_workflow=wf,
            last_workflow_yaml="title: drafted",
            last_update_block_count=None,
            last_test_ok=None,
        )
        result = _fake_run_result({"type": "REPLY", "user_response": "Please provide credentials before I continue."})
        agent_result = agent_module._translate_to_agent_result(
            result, ctx, global_llm_context=None, chat_request=_chat_request(), organization_id="org-1"
        )

        assert agent_result.updated_workflow is wf
        assert agent_result.workflow_yaml == "title: drafted"
        assert agent_result.unvalidated is True
        assert "Please provide credentials before I continue." in agent_result.user_response
        assert "review" in agent_result.user_response.lower()
        assert "accept" in agent_result.user_response.lower()
        assert "reject" in agent_result.user_response.lower()
        assert "discard" in agent_result.user_response.lower()

    def test_unvalidated_wip_reply_keeps_existing_ui_affordance(self) -> None:
        wf = SimpleNamespace(name="drafted")
        ctx = _ctx(
            last_workflow=wf,
            last_workflow_yaml="title: drafted",
            last_update_block_count=None,
            last_test_ok=None,
        )
        response = "I have a draft proposal. Use Review to inspect it, Accept to save it, or Reject it."
        result = _fake_run_result({"type": "REPLY", "user_response": response})
        agent_result = agent_module._translate_to_agent_result(
            result, ctx, global_llm_context=None, chat_request=_chat_request(), organization_id="org-1"
        )

        assert response in agent_result.user_response
        assert "not been tested or verified" in agent_result.user_response
        assert agent_result.updated_workflow is wf

    def test_goal_reached_false_flips_validated_proposal_to_unvalidated(self) -> None:
        # Agent-emitted goal_reached=False must override last_test_ok=True so
        # a draft the agent itself flagged as incomplete cannot auto-promote.
        wf = SimpleNamespace(name="drafted-but-incomplete")
        ctx = _ctx(
            last_workflow=wf,
            last_workflow_yaml="title: drafted",
            last_test_ok=True,
            last_update_block_count=8,
        )
        result = _fake_run_result(
            {
                "type": "REPLY",
                "user_response": "Cookie modal is blocking the form; the workflow needs to dismiss it first.",
                "goal_reached": False,
            }
        )
        agent_result = agent_module._translate_to_agent_result(
            result, ctx, global_llm_context=None, chat_request=_chat_request(), organization_id="org-1"
        )

        assert agent_result.updated_workflow is wf
        assert agent_result.workflow_yaml == "title: drafted"
        assert agent_result.unvalidated is True

    def test_goal_reached_default_true_keeps_verified_path(self) -> None:
        # Backwards-compat: stale prompts that omit goal_reached must continue
        # to surface a tested workflow as validated.
        wf = SimpleNamespace(name="drafted")
        ctx = _ctx(
            last_workflow=wf,
            last_workflow_yaml="title: drafted",
            last_test_ok=True,
            last_update_block_count=3,
        )
        result = _fake_run_result({"type": "REPLY", "user_response": "All set."})
        agent_result = agent_module._translate_to_agent_result(
            result, ctx, global_llm_context=None, chat_request=_chat_request(), organization_id="org-1"
        )

        assert agent_result.updated_workflow is wf
        assert agent_result.unvalidated is False

    def test_goal_reached_true_explicit_keeps_verified_path(self) -> None:
        wf = SimpleNamespace(name="drafted")
        ctx = _ctx(
            last_workflow=wf,
            last_workflow_yaml="title: drafted",
            last_test_ok=True,
            last_update_block_count=3,
        )
        result = _fake_run_result({"type": "REPLY", "user_response": "All set.", "goal_reached": True})
        agent_result = agent_module._translate_to_agent_result(
            result, ctx, global_llm_context=None, chat_request=_chat_request(), organization_id="org-1"
        )

        assert agent_result.updated_workflow is wf
        assert agent_result.unvalidated is False

    def test_goal_reached_string_false_is_coerced(self) -> None:
        # LLMs occasionally emit JSON-as-string values; ``"false"`` must flip
        # the gate the same as Python ``False``.
        wf = SimpleNamespace(name="drafted")
        ctx = _ctx(
            last_workflow=wf,
            last_workflow_yaml="title: drafted",
            last_test_ok=True,
            last_update_block_count=2,
        )
        result = _fake_run_result(
            {"type": "REPLY", "user_response": "Cookie modal blocked the form.", "goal_reached": "false"}
        )
        agent_result = agent_module._translate_to_agent_result(
            result, ctx, global_llm_context=None, chat_request=_chat_request(), organization_id="org-1"
        )

        assert agent_result.updated_workflow is wf
        assert agent_result.unvalidated is True

    def test_goal_reached_false_without_last_workflow_returns_no_proposal(self) -> None:
        # The unvalidated WIP fallback only fires when ``ctx.last_workflow``
        # exists. Self-reported failure on an empty context must not synthesize
        # a proposal out of thin air.
        ctx = _ctx(last_test_ok=None)
        result = _fake_run_result(
            {"type": "REPLY", "user_response": "I couldn't find the form.", "goal_reached": False}
        )
        agent_result = agent_module._translate_to_agent_result(
            result, ctx, global_llm_context=None, chat_request=_chat_request(), organization_id="org-1"
        )

        assert agent_result.updated_workflow is None
        assert agent_result.workflow_yaml is None
        assert agent_result.unvalidated is False

    def test_unbacked_workflow_claim_is_rewritten_without_proposal(self) -> None:
        ctx = _ctx(last_test_ok=None)
        result = _fake_run_result({"type": "REPLY", "user_response": "Here's the workflow."})
        agent_result = agent_module._translate_to_agent_result(
            result, ctx, global_llm_context=None, chat_request=_chat_request(), organization_id="org-1"
        )

        assert "here's the workflow" not in agent_result.user_response.lower()
        assert "wasn't able to produce a workflow proposal" in agent_result.user_response
        assert agent_result.updated_workflow is None
        assert agent_result.workflow_yaml is None

    def test_initial_part_workflow_claim_is_rewritten_without_proposal(self) -> None:
        ctx = _ctx(last_test_ok=None)
        result = _fake_run_result(
            {
                "type": "REPLY",
                "user_response": "In the meantime, I've drafted the initial part of your workflow with placeholders.",
            }
        )
        agent_result = agent_module._translate_to_agent_result(
            result, ctx, global_llm_context=None, chat_request=_chat_request(), organization_id="org-1"
        )

        assert "initial part of your workflow" not in agent_result.user_response.lower()
        assert "wasn't able to produce a workflow proposal" in agent_result.user_response
        assert agent_result.updated_workflow is None
        assert agent_result.workflow_yaml is None

    def test_unbacked_workflow_claim_not_rewritten_when_proposal_exists(self) -> None:
        wf = SimpleNamespace(name="drafted")
        ctx = _ctx(last_workflow=wf, last_workflow_yaml="title: drafted", last_test_ok=True)
        result = _fake_run_result({"type": "REPLY", "user_response": "Here's the workflow."})
        agent_result = agent_module._translate_to_agent_result(
            result, ctx, global_llm_context=None, chat_request=_chat_request(), organization_id="org-1"
        )

        assert agent_result.user_response == "Here's the workflow."
        assert agent_result.updated_workflow is wf

    def test_goal_reached_false_on_failed_test_does_not_double_unvalidate(self) -> None:
        # Failed-test path already routes to unvalidated WIP. A redundant
        # ``goal_reached: false`` from the agent must not change the outcome
        # (no double-effect, no regression of the existing failed-test rewrite).
        wf = SimpleNamespace(name="drafted")
        ctx = _ctx(
            last_workflow=wf,
            last_workflow_yaml="title: drafted",
            last_update_block_count=2,
            last_test_ok=False,
            last_test_failure_reason="A verification challenge is preventing submission.",
        )
        result = _fake_run_result({"type": "REPLY", "user_response": "Tried but blocked.", "goal_reached": False})
        agent_result = agent_module._translate_to_agent_result(
            result, ctx, global_llm_context=None, chat_request=_chat_request(), organization_id="org-1"
        )

        assert agent_result.updated_workflow is wf
        assert agent_result.unvalidated is True
        assert "test failed" in agent_result.user_response.lower()
        assert "keep the draft" in agent_result.user_response.lower()

    def test_inline_replace_workflow_wraps_block_goals_with_user_message(self, monkeypatch) -> None:
        # SKY-9174 parity: update_and_run_blocks_tool wraps block goals with
        # the user's chat message as big-goal context. The REPLACE_WORKFLOW
        # inline path must do the same, otherwise the untested yaml latches
        # onto ctx without user-intent framing and any downstream block run
        # hits the verifier-on-confirmation-surface bug this PR fixes.
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

    def test_inline_replace_workflow_prefers_resolved_block_goal_main_goal(self, monkeypatch) -> None:
        captured: dict[str, str] = {}

        def fake_process(**kwargs):
            captured["yaml"] = kwargs["workflow_yaml"]
            return SimpleNamespace(name="new-wf")

        def fake_wrap(workflow_yaml: str, user_message: str) -> str:
            return f"WRAPPED::{user_message}::{workflow_yaml}"

        monkeypatch.setattr("skyvern.forge.sdk.copilot.tools._process_workflow_yaml", fake_process)
        monkeypatch.setattr("skyvern.forge.sdk.copilot.agent.wrap_block_goals", fake_wrap)

        ctx = _ctx(
            user_message="I meant black holes",
            block_goal_main_goal="Go to arXiv and find research about black holes.",
        )
        result = _fake_run_result(
            {"type": "REPLACE_WORKFLOW", "user_response": "Here you go.", "workflow_yaml": "raw: yaml"}
        )
        agent_module._translate_to_agent_result(
            result, ctx, global_llm_context=None, chat_request=_chat_request(), organization_id="org-1"
        )

        assert captured["yaml"] == "WRAPPED::Go to arXiv and find research about black holes.::raw: yaml"

    def test_ask_question_with_verified_workflow_suppresses_and_clears(self) -> None:
        # A verified-but-non-terminal workflow built this turn must not surface
        # alongside the question; the clear flag also nulls any stale prior ghost.
        verified_wf = SimpleNamespace(name="verified-partial")
        ctx = _ctx(last_workflow=verified_wf, last_workflow_yaml="verified: yaml", last_test_ok=True)
        result = _fake_run_result({"type": "ASK_QUESTION", "user_response": "Need credentials before I can continue."})
        agent_result = agent_module._translate_to_agent_result(
            result, ctx, global_llm_context=None, chat_request=_chat_request(), organization_id="org-1"
        )

        assert agent_result.updated_workflow is None
        assert agent_result.workflow_yaml is None
        assert agent_result.response_type == "ASK_QUESTION"
        assert agent_result.clear_proposed_workflow is True

    def test_ask_question_without_workflow_still_sets_clear_flag(self) -> None:
        # An ASK_QUESTION turn with no draft this turn must still null any
        # prior persisted proposal so reload stays coherent.
        ctx = _ctx()
        result = _fake_run_result({"type": "ASK_QUESTION", "user_response": "Which site?"})
        agent_result = agent_module._translate_to_agent_result(
            result, ctx, global_llm_context=None, chat_request=_chat_request(), organization_id="org-1"
        )

        assert agent_result.updated_workflow is None
        assert agent_result.clear_proposed_workflow is True

    def test_reply_does_not_set_clear_proposed_flag(self) -> None:
        # Differential: a REPLY turn surfaces the verified workflow and leaves
        # any prior persisted proposal untouched.
        verified_wf = SimpleNamespace(name="final")
        ctx = _ctx(last_workflow=verified_wf, last_workflow_yaml="final: yaml", last_test_ok=True)
        result = _fake_run_result({"type": "REPLY", "user_response": "Here you go."})
        agent_result = agent_module._translate_to_agent_result(
            result, ctx, global_llm_context=None, chat_request=_chat_request(), organization_id="org-1"
        )

        assert agent_result.updated_workflow is verified_wf
        assert agent_result.workflow_yaml == "final: yaml"
        assert agent_result.response_type == "REPLY"
        assert agent_result.clear_proposed_workflow is False


class TestCredentialRefusalReachesAgent:
    """Prove the SKY-9189 refusal rule is actually delivered to the agent.

    `run_copilot_agent` constructs the openai-agents SDK `Agent(...)` with
    dynamic instructions derived from `_build_system_prompt(...)` and `tools=list(NATIVE_TOOLS)`.
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


class TestRequestPolicyCredentialResolution:
    @pytest.mark.asyncio
    async def test_missing_user_supplied_credential_ids_ask_for_clarification(self, monkeypatch) -> None:
        from skyvern.forge.sdk.copilot import request_policy as policy_module
        from skyvern.forge.sdk.copilot.request_policy import build_request_policy

        get_credentials_by_ids = AsyncMock(return_value=[SimpleNamespace(credential_id="cred_valid")])
        monkeypatch.setattr(
            policy_module.app,
            "DATABASE",
            SimpleNamespace(credentials=SimpleNamespace(get_credentials_by_ids=get_credentials_by_ids)),
        )

        policy = await build_request_policy(
            user_message="Please build it with cred_valid and cred_missing.",
            workflow_yaml="",
            chat_history="",
            global_llm_context="",
            organization_id="org-1",
            handler=None,
        )

        assert policy.credential_input_kind == "credential_id"
        assert policy.credential_refs == ["cred_valid", "cred_missing"]
        assert policy.invalid_credential_ids == ["cred_missing"]
        assert policy.user_response_policy == "ask_clarification"
        assert policy.allow_update_workflow is False
        assert policy.allow_run_blocks is False
        assert policy.allow_missing_credentials_in_draft is False
        assert policy.clarification_question
        assert "cred_missing" in policy.clarification_question
        assert "not found in this organization" in policy.clarification_question
        assert "unvalidated draft" in policy.clarification_question
        get_credentials_by_ids.assert_awaited_once_with(["cred_valid", "cred_missing"], organization_id="org-1")

    @pytest.mark.asyncio
    async def test_skip_test_allows_missing_user_supplied_credential_ids_in_draft(self, monkeypatch) -> None:
        from skyvern.forge.sdk.copilot import request_policy as policy_module
        from skyvern.forge.sdk.copilot.request_policy import build_request_policy

        get_credentials_by_ids = AsyncMock(return_value=[])
        monkeypatch.setattr(
            policy_module.app,
            "DATABASE",
            SimpleNamespace(credentials=SimpleNamespace(get_credentials_by_ids=get_credentials_by_ids)),
        )

        async def handler(**kwargs):
            return {
                "testing_intent": "skip_test",
                "credential_input_kind": "credential_id",
                "credential_refs": ["cred_missing"],
            }

        policy = await build_request_policy(
            user_message="Build an untested draft with cred_missing.",
            workflow_yaml="",
            chat_history="",
            global_llm_context="",
            organization_id="org-1",
            handler=handler,
        )

        assert policy.testing_intent == "skip_test"
        assert policy.invalid_credential_ids == ["cred_missing"]
        assert policy.user_response_policy == "proceed"
        assert policy.allow_update_workflow is True
        assert policy.allow_run_blocks is False
        assert policy.allow_missing_credentials_in_draft is True
        get_credentials_by_ids.assert_awaited_once_with(["cred_missing"], organization_id="org-1")

    @pytest.mark.asyncio
    async def test_valid_user_supplied_credential_ids_continue_normally(self, monkeypatch) -> None:
        from skyvern.forge.sdk.copilot import request_policy as policy_module
        from skyvern.forge.sdk.copilot.request_policy import build_request_policy

        credential = SimpleNamespace(credential_id="cred_valid")
        get_credentials_by_ids = AsyncMock(return_value=[credential])
        monkeypatch.setattr(
            policy_module.app,
            "DATABASE",
            SimpleNamespace(credentials=SimpleNamespace(get_credentials_by_ids=get_credentials_by_ids)),
        )

        policy = await build_request_policy(
            user_message="Please build it with cred_valid.",
            workflow_yaml="",
            chat_history="",
            global_llm_context="",
            organization_id="org-1",
            handler=None,
        )

        assert policy.credential_refs == ["cred_valid"]
        assert policy.resolved_credentials == [credential]
        assert policy.invalid_credential_ids == []
        assert policy.user_response_policy == "proceed"
        assert policy.allow_run_blocks is True
        get_credentials_by_ids.assert_awaited_once_with(["cred_valid"], organization_id="org-1")

    @pytest.mark.asyncio
    async def test_raw_inline_secret_refuses_before_model_classification(self, monkeypatch) -> None:
        from skyvern.forge.sdk.copilot import request_policy as policy_module
        from skyvern.forge.sdk.copilot.request_policy import build_request_policy

        get_credentials_by_ids = AsyncMock()
        monkeypatch.setattr(
            policy_module.app,
            "DATABASE",
            SimpleNamespace(credentials=SimpleNamespace(get_credentials_by_ids=get_credentials_by_ids)),
        )
        handler = AsyncMock()

        policy = await build_request_policy(
            user_message="Use username test@example.com and password=hunter2 to log in.",
            workflow_yaml="",
            chat_history="",
            global_llm_context="",
            organization_id="org-1",
            handler=handler,
        )

        assert policy.raw_secret_detected is True
        assert policy.credential_input_kind == "raw_secret"
        assert policy.user_response_policy == "ask_clarification"
        assert policy.allow_update_workflow is False
        assert policy.allow_run_blocks is False
        assert "DO NOT PROVIDE RAW LOGIN/PASSWORD" in (policy.clarification_question or "")
        assert "hunter2" not in (policy.clarification_question or "")
        handler.assert_not_awaited()
        get_credentials_by_ids.assert_not_called()

    @pytest.mark.asyncio
    async def test_request_policy_url_matching_and_skip_draft(self, monkeypatch) -> None:
        from skyvern.forge.sdk.copilot import request_policy as policy_module
        from skyvern.forge.sdk.copilot.request_policy import build_request_policy

        credential = SimpleNamespace(credential_id="cred_bank", name="Bank", tested_url="https://bank.example/login")
        credentials = SimpleNamespace(get_credentials=AsyncMock(return_value=[credential]))
        monkeypatch.setattr(policy_module.app, "DATABASE", SimpleNamespace(credentials=credentials))

        async def handler(**kwargs):
            return handler.response

        args = dict(workflow_yaml="", global_llm_context="", organization_id="org-1", handler=handler)
        handler.response = {
            "credential_input_kind": "credential_name",
            "credential_refs": ["Bank"],
        }
        name_policy = await build_request_policy(user_message="use my saved Bank credential", chat_history="", **args)
        assert name_policy.user_response_policy == "proceed"
        assert name_policy.resolved_credentials == [credential]

        handler.response = {
            "credential_input_kind": "website_stored_credential",
            "login_page_urls": ["https://bank.example/login"],
        }
        site_policy = await build_request_policy(user_message="use the saved login", chat_history="", **args)
        assert site_policy.user_response_policy == "proceed"
        assert site_policy.resolved_credentials == [credential]

        handler.response = {
            "credential_input_kind": "website_stored_credential",
            "login_page_urls": ["https://evil.example/login"],
        }
        url_policy = await build_request_policy(user_message="use the saved login", chat_history="", **args)
        assert url_policy.user_response_policy == "ask_clarification" and not url_policy.resolved_credentials

        handler.response = {
            "testing_intent": "require_test",
            "credential_input_kind": "website_stored_credential",
            "login_page_urls": ["https://bank.example/login"],
            "requires_user_clarification": False,
            "clarification_reason": "none",
        }
        exact_url_policy = await build_request_policy(
            user_message=(
                "Build and test a workflow that logs into https://bank.example/login "
                "using the saved credential for that site."
            ),
            chat_history="",
            **args,
        )
        assert exact_url_policy.user_response_policy == "proceed"
        assert exact_url_policy.testing_intent == "require_test"
        assert exact_url_policy.credential_input_kind == "website_stored_credential"
        assert exact_url_policy.resolved_credentials == [credential]
        assert exact_url_policy.allow_update_workflow and exact_url_policy.allow_run_blocks

        handler.response = {
            "credential_input_kind": "website_stored_credential",
            "login_page_urls": ["https://evil.example/login"],
        }
        no_suffix_policy = await build_request_policy(
            user_message="Use the stored credential for https://evil.example/login.",
            chat_history="",
            **args,
        )
        assert no_suffix_policy.user_response_policy == "ask_clarification"
        assert not no_suffix_policy.resolved_credentials
        assert "could not find a stored credential" in (no_suffix_policy.clarification_question or "")

        handler.response = {
            "credential_input_kind": "website_stored_credential",
            "requires_user_clarification": True,
            "clarification_reason": "missing_target_context",
        }
        missing_url_policy = await build_request_policy(
            user_message="use my saved login for this site",
            chat_history="",
            **args,
        )
        assert missing_url_policy.user_response_policy == "ask_clarification"
        assert "stored credential" in (missing_url_policy.clarification_question or "")

        handler.response = {
            "testing_intent": "skip_test",
            "credential_input_kind": "none",
            "requires_user_clarification": True,
            "clarification_reason": "credential_name_unresolved",
        }
        vague_skip_policy = await build_request_policy(
            user_message="use my saved login for this site and finish the workflow",
            chat_history="user: create a login workflow",
            **args,
        )
        assert vague_skip_policy.user_response_policy == "ask_clarification"
        assert not vague_skip_policy.allow_update_workflow
        assert not vague_skip_policy.allow_run_blocks
        assert "saved credential" in (vague_skip_policy.clarification_question or "")

        prior_clarification_context = (
            '{"decisions_made":["request-policy clarification required: credential_name/credential_name_unresolved"]}'
        )
        history_refs_from_context = await build_request_policy(
            user_message="Just draft a workflow without testing it.",
            workflow_yaml="",
            chat_history="user: login using the 'azure_credentials' and get the code from the 'mfa_email'",
            global_llm_context=prior_clarification_context,
            organization_id="org-1",
            handler=handler,
        )
        assert history_refs_from_context.user_response_policy == "proceed"
        assert history_refs_from_context.allow_update_workflow and not history_refs_from_context.allow_run_blocks
        assert history_refs_from_context.allow_missing_credentials_in_draft

        handler.response = {
            "testing_intent": "skip_test",
            "credential_input_kind": "credential_name",
            "requires_user_clarification": True,
            "clarification_reason": "credential_name_unresolved",
        }
        first_turn_missing_name_policy = await build_request_policy(
            user_message="Draft but do not test using my saved credential.",
            workflow_yaml="",
            chat_history="",
            global_llm_context="",
            organization_id="org-1",
            handler=handler,
        )
        assert first_turn_missing_name_policy.user_response_policy == "ask_clarification"
        assert not first_turn_missing_name_policy.allow_update_workflow

        follow_up_missing_name_policy = await build_request_policy(
            user_message="Just draft a workflow without testing it.",
            workflow_yaml="",
            chat_history="user: login using azure_credentials",
            global_llm_context="",
            organization_id="org-1",
            handler=handler,
        )
        assert follow_up_missing_name_policy.user_response_policy == "ask_clarification"
        assert not follow_up_missing_name_policy.allow_update_workflow
        assert not follow_up_missing_name_policy.allow_run_blocks

        handler.response = {
            "testing_intent": "skip_test",
            "credential_input_kind": "credential_name",
            "credential_refs": ["azure_credentials", "mfa_email"],
            "requires_user_clarification": True,
            "clarification_reason": "credential_name_unresolved",
        }
        history_refs = await build_request_policy(
            user_message="Just draft a workflow without testing it.",
            chat_history="user: login using the 'azure_credentials' and get the code from the 'mfa_email'",
            **args,
        )
        assert history_refs.user_response_policy == "proceed"
        assert history_refs.credential_input_kind == "credential_name"
        assert history_refs.credential_refs == ["azure_credentials", "mfa_email"]
        assert history_refs.allow_update_workflow and not history_refs.allow_run_blocks
        assert history_refs.allow_missing_credentials_in_draft

        handler.response = {
            "testing_intent": "unspecified",
            "credential_input_kind": "credential_name",
            "credential_refs": ["azure_credentials", "mfa_email"],
            "requires_user_clarification": True,
            "clarification_reason": "missing_conditional_condition",
        }
        credential_priority_policy = await build_request_policy(
            user_message="Log in using the 'azure_credentials' and use 'mfa_email' for MFA. If no account is provided, search by account number.",
            chat_history="",
            **args,
        )
        assert credential_priority_policy.user_response_policy == "ask_clarification"
        assert credential_priority_policy.clarification_reason == "credential_name_unresolved"
        assert "azure_credentials" in (credential_priority_policy.clarification_question or "")
        assert "condition" not in (credential_priority_policy.clarification_question or "").lower()

        handler.response = {
            "testing_intent": "skip_test",
            "credential_input_kind": "credential_name",
            "credential_refs": ["azure_credentials", "mfa_email"],
            "requires_user_clarification": True,
            "clarification_reason": "missing_conditional_condition",
        }
        history_refs_with_noncredential_reason = await build_request_policy(
            user_message="Just draft a workflow without testing it.",
            chat_history="user: login using the 'azure_credentials' and get the code from the 'mfa_email'",
            **args,
        )
        assert history_refs_with_noncredential_reason.user_response_policy == "proceed"
        assert history_refs_with_noncredential_reason.clarification_reason == "credential_name_unresolved"
        assert history_refs_with_noncredential_reason.credential_refs == ["azure_credentials", "mfa_email"]
        assert history_refs_with_noncredential_reason.allow_update_workflow
        assert not history_refs_with_noncredential_reason.allow_run_blocks

        handler.response = {
            "testing_intent": "skip_test",
            "credential_input_kind": "credential_name",
            "credential_refs": ["azure_credentials"],
            "requires_user_clarification": True,
            "clarification_reason": "credential_name_unresolved",
        }
        bare_name_skip_policy = await build_request_policy(
            user_message=(
                "Draft but do not test a workflow that logs into https://example.com/login "
                "using azure_credentials and goes to Billing & Payment Activity."
            ),
            chat_history="",
            **args,
        )
        assert bare_name_skip_policy.user_response_policy == "proceed"
        assert bare_name_skip_policy.credential_refs == ["azure_credentials"]
        assert bare_name_skip_policy.allow_update_workflow and not bare_name_skip_policy.allow_run_blocks

        handler.response = {
            "testing_intent": "skip_test",
            "credential_input_kind": "credential_name",
            "credential_refs": ["azure_credentials"],
            "requires_user_clarification": True,
        }
        skip_policy = await build_request_policy(
            user_message="just draft without testing", chat_history="user: use azure_credentials", **args
        )
        assert skip_policy.user_response_policy == "proceed"
        assert skip_policy.allow_update_workflow and not skip_policy.allow_run_blocks
        assert skip_policy.allow_missing_credentials_in_draft

        handler.response = {
            "testing_intent": "skip_test",
            "completion_contract": "complete when the page says your message has been sent",
        }
        completion_not_skip_policy = await build_request_policy(
            user_message=(
                "Fill out the contact form and submit it. "
                "Your goal is complete when the page says your message has been sent."
            ),
            chat_history="",
            **args,
        )
        assert completion_not_skip_policy.testing_intent == "unspecified"
        assert completion_not_skip_policy.allow_run_blocks
        assert (
            completion_not_skip_policy.completion_contract == "complete when the page says your message has been sent"
        )

        handler.response = {
            "credential_input_kind": "website_stored_credential",
            "login_page_urls": ["https://bank.example/login"],
        }
        stored_credential_with_id_policy = await build_request_policy(
            user_message="use the saved login for https://bank.example/login, credential id cred_bank",
            chat_history="",
            **args,
        )
        assert stored_credential_with_id_policy.credential_input_kind == "website_stored_credential"
        assert stored_credential_with_id_policy.credential_refs == ["cred_bank"]
        assert stored_credential_with_id_policy.resolved_credentials == [credential]

        handler.response = {
            "completion_contract": "confirmation banner appears",
        }
        no_completion_condition_policy = await build_request_policy(
            user_message="submit the contact form and report whether it worked",
            chat_history="",
            **args,
        )
        assert no_completion_condition_policy.completion_contract is None

        handler.response = {
            "completion_contract": "confirmation banner appears",
        }
        paraphrased_completion_policy = await build_request_policy(
            user_message="submit the contact form until the requested success state is reached",
            chat_history="",
            **args,
        )
        assert paraphrased_completion_policy.completion_contract is None
        assert "completion_contract:" not in paraphrased_completion_policy.prompt_summary()
        assert paraphrased_completion_policy.to_trace_data()["has_completion_contract"] is False

    @pytest.mark.asyncio
    async def test_request_policy_noncredential_clarification_uses_specific_copy(self, monkeypatch) -> None:
        from skyvern.forge.sdk.copilot import request_policy as policy_module
        from skyvern.forge.sdk.copilot.request_policy import build_request_policy

        credentials = SimpleNamespace(get_credentials=AsyncMock(return_value=[]))
        monkeypatch.setattr(policy_module.app, "DATABASE", SimpleNamespace(credentials=credentials))

        async def handler(**kwargs):
            return handler.response

        handler.response = {
            "testing_intent": "unspecified",
            "credential_input_kind": "none",
            "requires_user_clarification": True,
            "clarification_reason": "missing_conditional_condition",
        }
        policy = await build_request_policy(
            user_message="Add a conditional that goes to https://example.com/dropdown.",
            workflow_yaml="",
            chat_history="",
            global_llm_context="",
            organization_id="org-1",
            handler=handler,
        )

        assert policy.user_response_policy == "ask_clarification"
        assert policy.clarification_reason == "missing_conditional_condition"
        assert policy.clarification_question == "What condition should trigger this conditional route?"

        handler.response = {
            "testing_intent": "unspecified",
            "credential_input_kind": "none",
            "requires_user_clarification": True,
            "clarification_reason": "ambiguous_loop_edit",
        }
        loop_policy = await build_request_policy(
            user_message="can you put it inside of a loop block",
            workflow_yaml="",
            chat_history="",
            global_llm_context="",
            organization_id="org-1",
            handler=handler,
        )

        assert loop_policy.user_response_policy == "ask_clarification"
        assert loop_policy.clarification_reason == "ambiguous_loop_edit"
        assert "inside the loop" in (loop_policy.clarification_question or "")

    @pytest.mark.asyncio
    async def test_request_policy_refuses_invented_credential_id(self, monkeypatch) -> None:
        from skyvern.forge.sdk.copilot import request_policy as policy_module
        from skyvern.forge.sdk.copilot.request_policy import build_request_policy

        credentials = SimpleNamespace(get_credentials=AsyncMock(return_value=[]))
        monkeypatch.setattr(policy_module.app, "DATABASE", SimpleNamespace(credentials=credentials))

        async def handler(**kwargs):
            return {
                "testing_intent": "unspecified",
                "credential_input_kind": "none",
                "requires_user_clarification": True,
                "clarification_reason": "credential_invention_requested",
            }

        policy = await build_request_policy(
            user_message="ya that sounds good and make up a credential id",
            workflow_yaml="",
            chat_history="",
            global_llm_context="",
            organization_id="org-1",
            handler=handler,
        )

        assert policy.user_response_policy == "ask_clarification"
        assert policy.clarification_reason == "credential_invention_requested"
        assert not policy.allow_update_workflow
        assert not policy.allow_run_blocks
        assert "cannot invent a credential ID" in (policy.clarification_question or "")

    @pytest.mark.asyncio
    async def test_request_policy_resolves_classifier_credential_refs(self, monkeypatch) -> None:
        from skyvern.forge.sdk.copilot import request_policy as policy_module
        from skyvern.forge.sdk.copilot.request_policy import build_request_policy

        credentials = SimpleNamespace(get_credentials=AsyncMock(return_value=[]))
        monkeypatch.setattr(policy_module.app, "DATABASE", SimpleNamespace(credentials=credentials))

        async def handler(**kwargs):
            return {
                "testing_intent": "unspecified",
                "credential_input_kind": "credential_name",
                "credential_refs": ["azure_credentials", "mfa_email"],
                "requires_user_clarification": True,
            }

        policy = await build_request_policy(
            user_message=(
                "Log in using the 'azure_credentials'. "
                "If prompted for 2FA, get the code from the 'mfa_email'. "
                "Then search for 'account_number'."
            ),
            workflow_yaml="",
            chat_history="",
            global_llm_context="",
            organization_id="org-1",
            handler=handler,
        )

        assert policy.user_response_policy == "ask_clarification"
        assert policy.credential_refs == ["azure_credentials", "mfa_email"]
        assert "azure_credentials" in (policy.clarification_question or "")
        assert "account_number" not in policy.credential_refs

    def test_translate_untested_draft_request_surfaces_unvalidated_workflow(self) -> None:
        wf = SimpleNamespace(name="drafted")
        ctx = _ctx(
            allow_untested_workflow_draft=True,
            last_workflow=wf,
            last_workflow_yaml="title: drafted",
            last_update_block_count=4,
            last_test_ok=None,
        )
        result = _fake_run_result({"type": "REPLY", "user_response": "Done."})

        agent_result = agent_module._translate_to_agent_result(
            result, ctx, global_llm_context=None, chat_request=_chat_request(), organization_id="org-1"
        )

        assert agent_result.updated_workflow is wf
        assert agent_result.workflow_yaml == "title: drafted"
        assert agent_result.unvalidated is True
        assert "without testing it, as requested" in agent_result.user_response


class TestNativeToolCredentialIdValidation:
    def test_extracts_credential_ids_from_nested_tool_values(self) -> None:
        from skyvern.forge.sdk.copilot.tools import _extract_credential_ids_from_tool_value

        ids = _extract_credential_ids_from_tool_value(
            {
                "workflow_yaml": "credential_id: cred_valid",
                "parameters": {"login": "cred_missing", "note": "repeat cred_valid"},
            }
        )

        assert ids == ["cred_valid", "cred_missing"]

    def test_workflow_yaml_extraction_ignores_credential_like_parameter_keys(self) -> None:
        from skyvern.forge.sdk.copilot.tools import _extract_credential_ids_from_workflow_yaml

        ids = _extract_credential_ids_from_workflow_yaml(
            """
workflow_definition:
  parameters:
    - parameter_type: workflow
      workflow_parameter_type: credential_id
      key: cred_param
      default_value: cred_valid
    - parameter_type: workflow
      workflow_parameter_type: string
      key: cred_not_an_id
      default_value: cred_also_not_an_id
"""
        )

        assert ids == ["cred_valid"]

    @pytest.mark.asyncio
    async def test_missing_tool_credential_reference_returns_blocking_error(self, monkeypatch) -> None:
        from skyvern.forge.sdk.copilot.tools import _credential_reference_validation_error

        get_credentials_by_ids = AsyncMock(return_value=[SimpleNamespace(credential_id="cred_valid")])
        monkeypatch.setattr(
            agent_module.app,
            "DATABASE",
            SimpleNamespace(credentials=SimpleNamespace(get_credentials_by_ids=get_credentials_by_ids)),
        )

        error = await _credential_reference_validation_error(
            """
workflow_definition:
  parameters:
    - parameter_type: credential
      key: credentials
      credential_id: cred_valid
    - parameter_type: workflow
      workflow_parameter_type: credential_id
      key: backup_credentials
      default_value: cred_missing
""",
            _ctx(),
        )

        assert error is not None
        assert "cred_missing" in error
        assert "not found in this organization" in error
        assert "Stop before creating, updating, or running the workflow" in error
        get_credentials_by_ids.assert_awaited_once_with(["cred_valid", "cred_missing"], organization_id="org-1")

    @pytest.mark.asyncio
    async def test_valid_tool_credential_reference_allows_tool_path(self, monkeypatch) -> None:
        from skyvern.forge.sdk.copilot.tools import _credential_reference_validation_error

        get_credentials_by_ids = AsyncMock(return_value=[SimpleNamespace(credential_id="cred_valid")])
        monkeypatch.setattr(
            agent_module.app,
            "DATABASE",
            SimpleNamespace(credentials=SimpleNamespace(get_credentials_by_ids=get_credentials_by_ids)),
        )

        error = await _credential_reference_validation_error({"credential_id": "cred_valid"}, _ctx())

        assert error is None
        get_credentials_by_ids.assert_awaited_once_with(["cred_valid"], organization_id="org-1")

    @pytest.mark.asyncio
    async def test_update_workflow_allows_missing_credentials_for_explicit_untested_draft(self, monkeypatch) -> None:
        from skyvern.forge.sdk.copilot.tools import _update_workflow

        ctx = _ctx(allow_untested_workflow_draft=True)

        workflow = MagicMock()
        workflow.title = "Untested Draft"
        workflow.description = ""
        workflow.workflow_definition = MagicMock()
        workflow.workflow_definition.blocks = []
        workflow.proxy_location = None
        workflow.webhook_callback_url = None
        workflow.persist_browser_session = False
        workflow.model = None
        workflow.max_screenshot_scrolls = None
        workflow.extra_http_headers = None
        workflow.run_with = None
        workflow.ai_fallback = None
        workflow.cache_key = None
        workflow.run_sequentially = False
        workflow.sequential_key = None

        monkeypatch.setattr(
            "skyvern.forge.sdk.copilot.tools._process_workflow_yaml",
            lambda **kwargs: workflow,
        )
        monkeypatch.setattr(
            "skyvern.forge.sdk.copilot.tools.resolve_copilot_created_by_stamp",
            AsyncMock(return_value="copilot"),
        )
        workflow_service = MagicMock()
        workflow_service.update_workflow_definition = AsyncMock()
        monkeypatch.setattr("skyvern.forge.sdk.copilot.tools.app.WORKFLOW_SERVICE", workflow_service)
        get_credentials_by_ids = AsyncMock(return_value=[])
        monkeypatch.setattr(
            agent_module.app,
            "DATABASE",
            SimpleNamespace(credentials=SimpleNamespace(get_credentials_by_ids=get_credentials_by_ids)),
        )

        result = await _update_workflow(
            {
                "workflow_yaml": """
workflow_definition:
  parameters:
    - parameter_type: workflow
      workflow_parameter_type: credential_id
      key: login_credentials
      default_value: cred_missing
  blocks: []
"""
            },
            ctx,
        )

        assert result["ok"] is True
        get_credentials_by_ids.assert_not_called()


class TestResponseTypeClassificationRuleReachesAgent:
    """Pin the classifier rule that selects ASK_QUESTION when `user_response` asks the user for required input — the agent.py null-out gate keys on `resp_type == "ASK_QUESTION"` and depends on this prompt text."""

    def test_build_system_prompt_carries_classification_rule(self) -> None:
        from skyvern.forge.sdk.copilot.agent import _build_system_prompt

        prompt = _build_system_prompt(tool_usage_guide="", security_rules="")

        assert "RESPONSE-TYPE CLASSIFICATION" in prompt
        assert "required before you can continue" in prompt
        assert "this turn built or tested a partial workflow" in prompt
        assert "goal_reached: false" in prompt
        assert "Classify by intent, not punctuation" in prompt
        assert "does NOT imply REPLY" in prompt
        assert "explicitly asks for an untested draft" in prompt
        assert "workflow was drafted without testing as requested" in prompt
        assert prompt.index("RESPONSE-TYPE CLASSIFICATION") < prompt.index("**Option 1: Reply to the user**")


class TestCopilotConfig:
    def test_system_prompt_uses_custom_security_rules(self) -> None:
        prompt = agent_module._build_system_prompt(
            tool_usage_guide="",
            config=CopilotConfig(security_rules="CUSTOM SECURITY RULE"),
        )

        assert "CUSTOM SECURITY RULE" in prompt

    def test_retriable_llm_error_detects_openai_rate_limit(self) -> None:
        class FakeRateLimitError(Exception):
            pass

        FakeRateLimitError.__module__ = "openai"

        assert agent_module._is_retriable_llm_error(FakeRateLimitError("rate limit"))

    def test_fallback_key_skips_missing_or_same_key(self) -> None:
        assert agent_module._fallback_llm_key(CopilotConfig(fallback_llm_key=None), "PRIMARY") is None
        assert agent_module._fallback_llm_key(CopilotConfig(fallback_llm_key="PRIMARY"), "PRIMARY") is None
        assert agent_module._fallback_llm_key(CopilotConfig(fallback_llm_key="SECONDARY"), "PRIMARY") == "SECONDARY"

    @pytest.mark.asyncio
    async def test_run_copilot_agent_retries_retriable_failure_with_fallback(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class FakeRateLimitError(Exception):
            pass

        FakeRateLimitError.__module__ = "openai"

        async def fake_feasibility_gate(**_kwargs):
            return SimpleNamespace(verdict="proceed", question=None, rationale=None)

        class FakeMCPServerManager:
            def __init__(self, servers):
                self.active_servers = servers

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return None

        resolved_keys: list[str] = []

        def fake_resolve_model_config(_handler, *, copilot_config=None, llm_key_override=None):
            del copilot_config
            key = llm_key_override or "PRIMARY"
            resolved_keys.append(key)
            return f"model-{key}", object(), key, True

        run_with_enforcement = AsyncMock(
            side_effect=[
                FakeRateLimitError("rate limit"),
                _fake_run_result({"type": "REPLY", "user_response": "ok", "goal_reached": True}),
            ]
        )

        monkeypatch.setattr(
            "skyvern.forge.sdk.copilot.feasibility_gate.run_feasibility_gate",
            fake_feasibility_gate,
        )
        monkeypatch.setattr(
            "skyvern.forge.sdk.copilot.agent._resolve_live_browser_session_id",
            AsyncMock(return_value=None),
        )
        monkeypatch.setattr("agents.mcp.MCPServerManager", FakeMCPServerManager)
        monkeypatch.setattr(
            "skyvern.forge.sdk.copilot.model_resolver.resolve_model_config",
            fake_resolve_model_config,
        )
        monkeypatch.setattr(
            "skyvern.forge.sdk.copilot.enforcement.run_with_enforcement",
            run_with_enforcement,
        )

        result = await agent_module.run_copilot_agent(
            stream=MagicMock(),
            organization_id="org-1",
            chat_request=SimpleNamespace(
                message="build it",
                workflow_id="wf-1",
                workflow_permanent_id="wfp-1",
                workflow_copilot_chat_id="chat-1",
                workflow_yaml="",
                browser_session_id=None,
            ),
            chat_history=[],
            global_llm_context=None,
            debug_run_info_text="",
            llm_api_handler=SimpleNamespace(llm_key="PRIMARY"),
            api_key="sk-test",
            config=CopilotConfig(fallback_llm_key="SECONDARY"),
        )

        assert result.user_response == "ok"
        assert resolved_keys == ["PRIMARY", "SECONDARY"]
        assert run_with_enforcement.await_count == 2
        for call in run_with_enforcement.await_args_list:
            assert not getattr(call.kwargs["agent"], "input_guardrails", None)
