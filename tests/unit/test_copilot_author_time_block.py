"""The author-time refusal capability: three hard blocks, one decision point.

OSS-synced: only example.* / RFC-2606 placeholder targets and synthetic labels.
"""

from __future__ import annotations

import dataclasses
import inspect
import json
import re
import textwrap
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from skyvern.forge.sdk.copilot import blocker_signal as blocker_signal_module
from skyvern.forge.sdk.copilot import build_phase as build_phase_module
from skyvern.forge.sdk.copilot import failure_tracking as failure_tracking_module
from skyvern.forge.sdk.copilot import run_outcome as run_outcome_module
from skyvern.forge.sdk.copilot import tools as tools_module
from skyvern.forge.sdk.copilot.author_time_block import (
    AUTHOR_TIME_HARD_BLOCKS,
    BANNED_BLOCKS_BLOCK_ID,
    CODE_SAFETY_BLOCK_ID,
    CREDENTIAL_SCOUT_BLOCK_ID,
    AuthorTimeBlock,
)
from skyvern.forge.sdk.copilot.blocker_signal import CopilotToolBlockerSignal
from skyvern.forge.sdk.copilot.config import BlockAuthoringPolicy
from skyvern.forge.sdk.copilot.context import CopilotContext
from skyvern.forge.sdk.copilot.output_policy import OutputPolicyReason, OutputPolicyVerdict
from skyvern.forge.sdk.copilot.request_policy import RequestPolicy
from skyvern.forge.sdk.copilot.schema_incompatibility import (
    SCHEMA_INCOMPATIBILITY_REASON_CODE,
    SchemaIncompatibility,
)
from skyvern.forge.sdk.copilot.tools import _update_workflow
from skyvern.forge.sdk.copilot.tools import blockers as blockers_module
from skyvern.forge.sdk.copilot.tools import guardrails as guardrails_module
from skyvern.forge.sdk.copilot.tools import workflow_update as workflow_update_module
from skyvern.forge.sdk.copilot.tools.guardrails import _workflow_yaml_output_policy_guardrail

_BUILD_PHASE_SOURCE = inspect.getsource(build_phase_module)
_BLOCKERS_SOURCE = inspect.getsource(blockers_module)
_BLOCKER_SIGNAL_SOURCE = inspect.getsource(blocker_signal_module)


def _yaml(body: str) -> str:
    return textwrap.dedent(body).strip() + "\n"


_SAFE_YAML = _yaml(
    """
    title: Registry lookup
    workflow_definition:
      blocks:
      - block_type: code
        label: search_registry
        code: |
          await page.goto("https://example.com/search")
          await page.locator("#search").fill("widget")
    """
)

_UNSAFE_CODE_YAML = _yaml(
    """
    title: Registry lookup
    workflow_definition:
      blocks:
      - block_type: code
        label: search_registry
        code: |
          import requests
          requests.get("https://example.com")
    """
)

_BANNED_BLOCK_YAML = _yaml(
    """
    title: Registry lookup
    workflow_definition:
      blocks:
      - block_type: task
        label: do_the_thing
        prompt: Find the widget
    """
)

_CONFLICT_MARKER_YAML = (
    "<<<<<<< HEAD\n"
    "title: Registry lookup\n"
    "=======\n"
    "title: Registry lookup v2\n"
    ">>>>>>> theirs\n"
    "workflow_definition:\n"
    "  blocks:\n"
    "  - block_type: code\n"
    "    label: search_registry\n"
    "    code: |\n"
    '      await page.goto("https://example.com/search")\n'
)

_CREDENTIAL_LOGIN_YAML = _yaml(
    """
    title: Portal login
    workflow_definition:
      blocks:
      - block_type: code
        label: portal_login
        code: |
          await page.goto("https://example.com/login")
          await page.locator("#submit").click()
    """
)


def _ctx() -> CopilotContext:
    ctx = CopilotContext(
        organization_id="o",
        workflow_id="w",
        workflow_permanent_id="wp",
        workflow_yaml="",
        browser_session_id=None,
        stream=SimpleNamespace(),  # type: ignore[arg-type]
    )
    ctx.block_authoring_policy = BlockAuthoringPolicy.CODE_ONLY_BROWSER
    ctx.scout_trajectory = [
        {
            "tool_name": "click",
            "selector": "#search-submit",
            "source_url": "https://example.com/search",
            "trajectory_index": 0,
        }
    ]
    return ctx


def _stub_persist(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_process_workflow_yaml(**_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(
            workflow_definition=SimpleNamespace(blocks=[SimpleNamespace(label="search_registry")]),
            proxy_location=None,
        )

    async def _fake_get_prior_workflow(_ctx: CopilotContext) -> None:
        return None

    monkeypatch.setattr(workflow_update_module, "_process_workflow_yaml", _fake_process_workflow_yaml)
    monkeypatch.setattr(workflow_update_module, "_get_prior_workflow", _fake_get_prior_workflow)


def test_hard_block_constant_has_exactly_three_members() -> None:
    assert AUTHOR_TIME_HARD_BLOCKS == frozenset(
        {CODE_SAFETY_BLOCK_ID, CREDENTIAL_SCOUT_BLOCK_ID, BANNED_BLOCKS_BLOCK_ID}
    )
    assert len(AUTHOR_TIME_HARD_BLOCKS) == 3
    assert sorted(AUTHOR_TIME_HARD_BLOCKS) == ["banned_blocks", "code_safety", "credential_scout"]


@pytest.mark.parametrize(
    "block_id",
    ["definition_contract_unsatisfied", "metadata_reject", "schema_incompatibility", "", "CODE_SAFETY"],
)
def test_constructing_a_block_outside_the_constant_raises(block_id: str) -> None:
    with pytest.raises(ValueError):
        AuthorTimeBlock(block_id=block_id, error="a new validator wants to refuse")


def test_each_retained_identity_constructs() -> None:
    for block_id in AUTHOR_TIME_HARD_BLOCKS:
        assert AuthorTimeBlock(block_id=block_id, error="x").block_id == block_id


def test_the_identity_cannot_be_swapped_after_construction() -> None:
    """The constructor check only bounds refusal if the identity is immutable afterwards —
    otherwise a caller constructs a declared block and reassigns its way out of the set."""
    block = AuthorTimeBlock(block_id=CODE_SAFETY_BLOCK_ID, error="x")

    with pytest.raises(dataclasses.FrozenInstanceError):
        block.block_id = "newly_invented_gate"  # type: ignore[misc]


@pytest.mark.asyncio
async def test_synthetic_validator_finding_cannot_block_the_persist(monkeypatch: pytest.MonkeyPatch) -> None:
    """A validator added next month reports a finding in the most refusal-shaped way it can —
    a violation list, a truthy error string, a rejection dict — and the draft still persists."""
    _stub_persist(monkeypatch)
    ctx = _ctx()

    monkeypatch.setattr(
        workflow_update_module,
        "_maybe_impose_synthesized_code_block",
        lambda yaml_text, *_a, **_k: workflow_update_module._SynthesizedCodeImpositionResult(
            workflow_yaml=yaml_text,
            violations=["REFUSED: imposition violated. ok: False. allowed=False."],
        ),
    )
    monkeypatch.setattr(
        workflow_update_module,
        "_apply_scouted_typed_default_promotions",
        lambda yaml_text, *_a, **_k: (yaml_text, ["REFUSED: typed default violation."], []),
    )
    monkeypatch.setattr(
        workflow_update_module,
        "merge_schema_incompatibilities",
        lambda *_a, **_k: None,
    )

    result = await _update_workflow({"workflow_yaml": _SAFE_YAML}, ctx)

    assert result["ok"] is True
    assert ctx.has_staged_proposal is True
    assert ctx.staged_workflow is not None
    assert ctx.blocker_signal is None
    assert ctx.turn_halt is None


@pytest.mark.asyncio
async def test_code_safety_still_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_persist(monkeypatch)
    ctx = _ctx()

    result = await _update_workflow({"workflow_yaml": _UNSAFE_CODE_YAML}, ctx)

    assert result["ok"] is False
    assert result["block_id"] == CODE_SAFETY_BLOCK_ID
    assert ctx.has_staged_proposal is False


@pytest.mark.asyncio
async def test_banned_block_type_still_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_persist(monkeypatch)
    ctx = _ctx()
    ctx.block_authoring_policy = BlockAuthoringPolicy.STANDARD

    result = await _update_workflow({"workflow_yaml": _BANNED_BLOCK_YAML}, ctx)

    assert result["ok"] is False
    assert result["block_id"] == BANNED_BLOCKS_BLOCK_ID
    assert ctx.has_staged_proposal is False


@pytest.mark.asyncio
async def test_credential_scout_gate_still_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_persist(monkeypatch)
    ctx = _ctx()
    ctx.scout_trajectory = []
    monkeypatch.setattr(
        workflow_update_module,
        "_credentialed_code_block_scout_gate_errors",
        lambda *_a, **_k: ["Scout the login form before authoring a credential fill."],
    )

    result = await _update_workflow({"workflow_yaml": _CREDENTIAL_LOGIN_YAML}, ctx)

    assert result["ok"] is False
    assert result["block_id"] == CREDENTIAL_SCOUT_BLOCK_ID
    assert ctx.has_staged_proposal is False


def test_residual_raw_credential_fill_is_admitted_on_an_allowed_verdict(monkeypatch: pytest.MonkeyPatch) -> None:
    workflow_yaml = _yaml(
        """
        title: Portal login
        workflow_definition:
          blocks:
          - block_type: code
            label: portal_login
            code: |
              await page.locator("#username").fill(parameters["operator"])
        """
    )
    tool_context = SimpleNamespace(
        tool_name="update_workflow",
        tool_call_id="call_1",
        tool_arguments=json.dumps({"workflow_yaml": workflow_yaml}),
        context=SimpleNamespace(request_policy=RequestPolicy()),
    )
    rebind = SimpleNamespace(
        changed=False,
        workflow_yaml=workflow_yaml,
        residual_selectors=("#username",),
        authored=(),
        skips=(),
    )
    monkeypatch.setattr(
        guardrails_module,
        "_rebind_scouted_credential_literals_in_place",
        lambda *_a, **_k: rebind,
    )

    output = _workflow_yaml_output_policy_guardrail(SimpleNamespace(context=tool_context))  # type: ignore[arg-type]

    assert output.output_info["allowed"] is True
    assert output.output_info["residual_raw_credential_fill_selectors"] == ["#username"]
    assert "block_id" not in output.output_info


def test_tool_input_guardrail_allows_a_non_credential_verdict(monkeypatch: pytest.MonkeyPatch) -> None:
    tool_context = SimpleNamespace(
        tool_name="update_workflow",
        tool_call_id="call_1",
        tool_arguments=json.dumps({"workflow_yaml": _SAFE_YAML}),
        context=SimpleNamespace(request_policy=RequestPolicy()),
    )
    monkeypatch.setattr(
        guardrails_module,
        "evaluate_output_policy",
        lambda **_k: OutputPolicyVerdict(reason_codes=[OutputPolicyReason.WORKFLOW_YAML_IN_REPLY]),
    )

    output = _workflow_yaml_output_policy_guardrail(SimpleNamespace(context=tool_context))  # type: ignore[arg-type]

    assert output.output_info["allowed"] is True
    assert "block_id" not in output.output_info


@pytest.mark.asyncio
async def test_unparseable_yaml_is_an_honest_error_not_a_block(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_persist(monkeypatch)
    ctx = _ctx()

    result = await _update_workflow({"workflow_yaml": _CONFLICT_MARKER_YAML}, ctx)

    assert result["ok"] is False
    assert "block_id" not in result
    assert ctx.blocker_signal is None
    assert ctx.turn_halt is None
    assert ctx.code_authoring_guardrail_reject_count == 0


def test_author_time_findings_label_a_persisted_draft_without_refusing_it() -> None:
    """The finding bucket is the whole replacement for the deleted refusals: three reason codes,
    each carrying a summary, and an empty list when the draft is clean."""
    assert (
        workflow_update_module._author_time_findings(
            schema_incompatibility=None, metadata_violations=[], imposition_violations=[]
        )
        == []
    )

    incompat = SchemaIncompatibility(
        block_label="extract_registry_row",
        incompatible_paths=["output.serial_number"],
        known_output_paths=["output.title"],
    )
    findings = workflow_update_module._author_time_findings(
        schema_incompatibility=incompat,
        metadata_violations=["missing declared goal for extract_registry_row"],
        imposition_violations=["could not impose the synthesized block"],
    )

    assert [finding["reason_code"] for finding in findings] == [
        SCHEMA_INCOMPATIBILITY_REASON_CODE,
        "code_artifact_metadata_incomplete",
        "synthesized_code_block_not_imposed",
    ]
    assert all(finding["summary"] for finding in findings)
    assert findings[0]["schema_incompatibility"]["incompatible_paths"] == ["output.serial_number"]
    # A finding is a label, never an identity that could refuse.
    for finding in findings:
        assert finding["reason_code"] not in AUTHOR_TIME_HARD_BLOCKS


def test_a_new_build_phase_gate_cannot_wall_the_turn() -> None:
    """The third plane's ratchet. `_phase_blocker_signal` ends turns without ever constructing an
    AuthorTimeBlock, so its vocabulary is bounded at its own reviewed constant."""
    synthetic = CopilotToolBlockerSignal(
        blocker_kind="phase_gated",
        agent_steering_text="Stop: this phase may not author.",
        user_facing_reason="I can't do that yet.",
        recovery_hint="ask_user_clarifying",
        internal_reason_code="build_phase_newly_invented_gate",
        blocked_tool="update_workflow",
    )

    with pytest.raises(ValueError, match="not a declared build-phase refusal"):
        build_phase_module._declared_phase_refusal(synthetic)


def test_the_three_planes_declare_disjoint_vocabularies() -> None:
    assert not build_phase_module.BUILD_PHASE_REFUSAL_REASON_CODES & blockers_module.LOOP_PLANE_REFUSAL_REASON_CODES
    assert not build_phase_module.BUILD_PHASE_REFUSAL_REASON_CODES & AUTHOR_TIME_HARD_BLOCKS


def test_every_build_phase_refusal_it_can_emit_is_declared() -> None:
    """The inverse ratchet: renaming a reason code in the builder would otherwise turn a working
    refusal into an unhandled ValueError mid-turn."""
    emitted = set(re.findall(r'internal_reason_code="(build_phase_[a-z_]+)"', _BUILD_PHASE_SOURCE))

    assert emitted == set(build_phase_module.BUILD_PHASE_REFUSAL_REASON_CODES)


def test_every_loop_plane_refusal_it_can_emit_is_declared() -> None:
    """Same inverse ratchet for the loop plane: every reason code that can reach
    `_emit_loop_plane_refusal` has to be declared, or it raises where a refusal used to work.

    Every code the loop plane actually carries is a named constant defined outside `blockers.py`,
    so matching literals in that one module asserts nothing about them — this compares the
    constants themselves, and fails if a rename moves a value out of the declared set.
    """
    carried = {
        blocker_signal_module.SYNTHESIZED_BLOCK_PERSISTENCE_REASON_CODE,
        blocker_signal_module.DISCOVERY_EXHAUSTED_NO_ENTRY_URL_REASON_CODE,
        failure_tracking_module.ACTIVE_RUN_TERMINAL_EVIDENCE_REASON_CODE,
        run_outcome_module.TERMINAL_CHALLENGE_BLOCKER_REASON_CODE,
        blockers_module._POST_BUDGET_CHALLENGE_RESULT_EVIDENCE_REASON,
        blockers_module._POST_BUDGET_CHALLENGE_BLOCKER_REASON,
    }
    # The loop-detection branch names its codes inline rather than via constants.
    carried |= set(re.findall(r'internal = "(loop_detected_[a-z_]+)"', _BLOCKER_SIGNAL_SOURCE))
    carried |= set(re.findall(r'internal_reason_code="([a-z_]+)"', _BLOCKERS_SOURCE))

    assert carried, "found no loop-plane reason codes — the collection above has gone stale"
    assert carried - blockers_module.LOOP_PLANE_REFUSAL_REASON_CODES == set()


def test_phase_blocker_signal_is_not_an_author_time_block() -> None:
    """Post-run and phase/authority signals live on the runtime plane; forcing them into the
    three author-time identities would re-admit refusal to a surface that authors nothing."""
    ctx = _ctx()
    ctx.build_phase = build_phase_module.BuildPhase.DISCOVERING

    signal = build_phase_module._phase_blocker_signal(ctx, "update_workflow")

    assert isinstance(signal, CopilotToolBlockerSignal)
    assert not isinstance(signal, AuthorTimeBlock)
    assert not hasattr(signal, "block_id")


def test_a_new_loop_plane_check_cannot_wall_the_turn() -> None:
    """The ratchet's other half. `_tool_loop_error` runs before the authoring seam and sees the
    submitted draft, so an undeclared refusal minted there would wall a turn while never
    constructing an AuthorTimeBlock."""
    synthetic = CopilotToolBlockerSignal(
        blocker_kind="tool_error",
        agent_steering_text="Stop: the draft omits an observation ref.",
        user_facing_reason="I couldn't save that workflow.",
        recovery_hint="report_blocker_to_user",
        renders_final_reply=True,
        internal_reason_code="tool_error_draft_missing_observation_ref",
        blocked_tool="update_workflow",
    )

    with pytest.raises(ValueError, match="not a declared loop-plane refusal"):
        blockers_module._emit_loop_plane_refusal(_ctx(), synthetic)


@pytest.mark.asyncio
async def test_findings_on_a_persisted_draft_survive_the_combined_update_and_run_tool(monkeypatch) -> None:
    """The findings bucket is what replaced the deleted refusals, so it is only worth anything if
    it reaches the model on the tool the agent actually authors with."""
    ctx = _ctx()

    async def fake_update_workflow(_payload, _ctx, **_kwargs):
        return {
            "ok": True,
            "data": {"message": "Workflow updated successfully.", "findings": [{"reason_code": "r", "summary": "s"}]},
        }

    monkeypatch.setattr(tools_module, "_request_policy_allows_update_and_skip_run", lambda *args: True)
    monkeypatch.setattr(tools_module, "_authority_tool_error", lambda *args, **kwargs: None)
    monkeypatch.setattr(tools_module, "_tool_loop_error", lambda *args, **kwargs: None)
    monkeypatch.setattr(tools_module, "_get_prior_workflow_definition", AsyncMock(return_value=None))
    monkeypatch.setattr(tools_module, "_update_workflow", fake_update_workflow)
    monkeypatch.setattr(tools_module, "_record_workflow_update_result", lambda *args, **kwargs: None)
    monkeypatch.setattr(tools_module, "_record_diagnosis_repair_contract", lambda *args, **kwargs: None)

    result = await tools_module.update_and_run_blocks_tool.on_invoke_tool(
        SimpleNamespace(context=ctx, tool_name="update_and_run_blocks"),
        json.dumps({"workflow_yaml": _SAFE_YAML, "block_labels": ["search_registry"]}),
    )

    parsed = json.loads(result)
    assert parsed["ok"] is True
    assert parsed["data"]["findings"] == [{"reason_code": "r", "summary": "s"}]


@pytest.mark.asyncio
async def test_a_credential_deferred_draft_persists_instead_of_being_redirected(monkeypatch) -> None:
    """Credentials that do not exist yet are not a disclosure, so the draft saves and says so
    rather than failing the save tool to make the model retry through a sibling tool."""
    ctx = _ctx()

    async def fake_update_workflow(_payload, _ctx, **kwargs):
        assert kwargs["allow_missing_credentials"] is True
        return {"ok": True, "data": {"block_count": 1}}

    monkeypatch.setattr(tools_module, "_tool_loop_error", lambda *args, **kwargs: None)
    monkeypatch.setattr(tools_module, "_request_policy_allows_credential_deferred_draft", lambda *args: True)
    monkeypatch.setattr(tools_module, "_get_prior_workflow_definition", AsyncMock(return_value=None))
    monkeypatch.setattr(tools_module, "_update_workflow", fake_update_workflow)
    monkeypatch.setattr(tools_module, "_record_workflow_update_result", lambda *args, **kwargs: None)

    result = await tools_module.update_workflow_tool.on_invoke_tool(
        SimpleNamespace(context=ctx, tool_name="update_workflow"),
        json.dumps({"workflow_yaml": _SAFE_YAML}),
    )

    parsed = json.loads(result)
    assert parsed["ok"] is True
    assert parsed["data"]["skipped_run"] is True
    assert parsed["data"]["skip_reason"] == "workflow_credential_inputs_unbound"
    assert "credentials aren't set up yet" in parsed["data"]["message"]
    assert ctx.last_run_skipped_unbound_credentials is True
    assert ctx.blocker_signal is None


def test_every_declared_loop_plane_refusal_is_runtime_not_draft_content() -> None:
    """Each entry is a reviewed decision. `tool_error_challenge_gated_submit_disabled` is the one
    that reads the submitted draft (its proxy value); SKY-13044 owns dispositioning it."""
    assert "tool_error_challenge_gated_submit_disabled" in blockers_module.LOOP_PLANE_REFUSAL_REASON_CODES
    assert not blockers_module.LOOP_PLANE_REFUSAL_REASON_CODES & AUTHOR_TIME_HARD_BLOCKS
