"""The author-time refusal capability: three hard blocks, one decision point.

OSS-synced: only example.* / RFC-2606 placeholder targets and synthetic labels.
"""

from __future__ import annotations

import dataclasses
import importlib.util
import inspect
import json
import textwrap
from types import SimpleNamespace

import pytest

from skyvern.forge.sdk.copilot import blocker_signal as blocker_signal_module
from skyvern.forge.sdk.copilot.author_time_block import (
    AUTHOR_TIME_HARD_BLOCKS,
    BANNED_BLOCKS_BLOCK_ID,
    CODE_SAFETY_BLOCK_ID,
    CREDENTIAL_SCOUT_BLOCK_ID,
    AuthorTimeBlock,
)
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
    ctx.request_policy = RequestPolicy(allow_update_workflow=True, allow_run_blocks=True)
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
            webhook_callback_url=None,
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
async def test_credential_reference_validation_still_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_persist(monkeypatch)
    ctx = _ctx()
    ctx.scout_trajectory = []

    async def _unresolved(*_a: object, **_k: object) -> str:
        return "Credential `cred_missing` was not found in this organization."

    monkeypatch.setattr(workflow_update_module, "_credential_reference_validation_error", _unresolved)

    result = await _update_workflow({"workflow_yaml": _CREDENTIAL_LOGIN_YAML}, ctx)

    assert result["ok"] is False
    assert result["block_id"] == CREDENTIAL_SCOUT_BLOCK_ID
    assert ctx.has_staged_proposal is False


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


def test_author_time_findings_label_a_persisted_draft_without_refusing_it() -> None:
    """The finding bucket is the whole replacement for the deleted refusals: two reason codes,
    each carrying a summary, and an empty list when the draft is clean."""
    assert workflow_update_module._author_time_findings(schema_incompatibility=None, metadata_violations=[]) == []

    incompat = SchemaIncompatibility(
        block_label="extract_registry_row",
        incompatible_paths=["output.serial_number"],
        known_output_paths=["output.title"],
    )
    findings = workflow_update_module._author_time_findings(
        schema_incompatibility=incompat,
        metadata_violations=["missing declared goal for extract_registry_row"],
    )

    assert [finding["reason_code"] for finding in findings] == [
        SCHEMA_INCOMPATIBILITY_REASON_CODE,
        "code_artifact_metadata_incomplete",
    ]
    assert all(finding["summary"] for finding in findings)
    assert findings[0]["schema_incompatibility"]["incompatible_paths"] == ["output.serial_number"]
    # A finding is a label, never an identity that could refuse.
    for finding in findings:
        assert finding["reason_code"] not in AUTHOR_TIME_HARD_BLOCKS


def test_build_phase_state_machine_is_deleted() -> None:
    assert importlib.util.find_spec("skyvern.forge.sdk.copilot.build_phase") is None


def test_loop_plane_refusal_registry_is_deleted() -> None:
    assert not hasattr(blockers_module, "LOOP_PLANE_REFUSAL_REASON_CODES")
    assert not hasattr(blockers_module, "_emit_loop_plane_refusal")


def test_loop_plane_refusal_symbols_stay_deleted() -> None:
    assert not hasattr(blockers_module, "LOOP_PLANE_REFUSAL_REASON_CODES")
    assert not hasattr(blockers_module, "_emit_loop_plane_refusal")
