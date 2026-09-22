"""Persistence custody tests for model-authored Workflow Copilot code blocks.

The accepted path is lossless: deterministic code synthesis, selector substitution, metadata
scaffolding, and output-envelope insertion are not persistence responsibilities. Existing hard
safety checks may reject a submission, and registered live credential values remain redacted.
"""

from __future__ import annotations

import asyncio
import json
import textwrap
from types import SimpleNamespace
from typing import NoReturn
from unittest.mock import AsyncMock

import pytest

from skyvern.forge import app
from skyvern.forge.agent_functions import AgentFunction
from skyvern.forge.sdk.copilot import tools as tools_module
from skyvern.forge.sdk.copilot.code_block_preflight import CodeBlockScanFinding
from skyvern.forge.sdk.copilot.config import BlockAuthoringPolicy
from skyvern.forge.sdk.copilot.context import CopilotContext
from skyvern.forge.sdk.copilot.request_policy import (
    CompletionCriterion,
    RequestPolicy,
)
from skyvern.forge.sdk.copilot.runtime import CredentialOriginRecovery, CredentialOriginRecoveryState
from skyvern.forge.sdk.copilot.secret_scrub import REDACTED_SECRET_PLACEHOLDER, register_secret_scrub_value
from skyvern.forge.sdk.copilot.tools import workflow_update as workflow_update_module
from skyvern.forge.sdk.copilot.tools.guardrails import _authority_tool_error
from skyvern.forge.sdk.copilot.tools.workflow_update import (
    READINESS_WAIT_ADVISORY_REASON_CODE,
    WRAPPER_SCOPE_ADVISORY_REASON_CODE,
    CodeArtifactCompletionCriterion,
    _accepted_code_delta,
    _advisory_labels_by_diagnostic,
    _author_time_findings,
    _changed_code_blocks,
    _update_workflow,
    carry_author_time_findings,
)
from skyvern.forge.sdk.copilot.workflow_credential_utils import parse_workflow_yaml, workflow_blocks
from skyvern.forge.sdk.copilot.workflow_yaml import (
    BlockEditError,
    apply_block_edit,
    delete_block_from_workflow,
    stored_block_code,
    stored_workflow_yaml,
)
from skyvern.forge.sdk.services.google_oauth_service import GOOGLE_SHEETS_DATA_SCOPE


def _yaml(body: str) -> str:
    return textwrap.dedent(body).strip() + "\n"


def _ctx(
    workflow_yaml: str = "", *, policy: BlockAuthoringPolicy = BlockAuthoringPolicy.CODE_ONLY_BROWSER
) -> CopilotContext:
    ctx = CopilotContext(
        organization_id="o",
        workflow_id="w",
        workflow_permanent_id="wp",
        workflow_yaml=workflow_yaml,
        browser_session_id=None,
        stream=None,
    )
    ctx.block_authoring_policy = policy
    ctx.request_policy = RequestPolicy(allow_update_workflow=True, allow_run_blocks=False)
    return ctx


def _code_yaml(code: str, *, label: str = "submit_search", prompt: str | None = None) -> str:
    indented = "\n".join(f"          {line}" for line in textwrap.dedent(code).strip().splitlines())
    prompt_line = f"    prompt: {json.dumps(prompt, ensure_ascii=False)}\n" if prompt is not None else ""
    return (
        "title: Search\n"
        "workflow_definition:\n"
        "  blocks:\n"
        "  - block_type: code\n"
        f"    label: {label}\n"
        f"{prompt_line}"
        "    code: |\n"
        f"{indented}\n"
    )


def _single_code(workflow_yaml: str) -> str:
    parsed = parse_workflow_yaml(workflow_yaml)
    assert isinstance(parsed, dict)
    blocks = [block for block in workflow_blocks(parsed) if block.get("block_type") == "code"]
    assert len(blocks) == 1
    return str(blocks[0]["code"])


def test_code_artifact_criterion_preserves_registered_download_declaration() -> None:
    criterion = CodeArtifactCompletionCriterion.model_validate(
        {
            "id": "deliver_statement",
            "text": "The requested statement is delivered as a registered file.",
            "deliverable_kind": "registered_download",
        }
    )

    assert criterion.model_dump(mode="json", exclude_none=True)["deliverable_kind"] == "registered_download"


def _stub_successful_update(monkeypatch: pytest.MonkeyPatch, persisted: list[str] | None = None) -> None:
    async def _process(**kwargs: object) -> SimpleNamespace:
        if persisted is not None:
            persisted.append(str(kwargs["workflow_yaml"]))
        return SimpleNamespace(
            workflow_definition=SimpleNamespace(blocks=[SimpleNamespace(label="submit_search")]),
            proxy_location=None,
            webhook_callback_url=None,
        )

    async def _prior(_ctx: CopilotContext) -> None:
        return None

    monkeypatch.setattr(workflow_update_module, "_process_workflow_yaml", _process)
    monkeypatch.setattr(workflow_update_module, "_get_prior_workflow", _prior)


@pytest.mark.asyncio
async def test_concurrent_writes_stash_their_diffs_under_their_own_call_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The originating call id must reach the stash as a parameter, not via shared context.

    The stash runs after the workflow is persisted, so when the id was read back off a field on
    ``CopilotContext`` a sibling authoring call could overwrite it in the awaits between — and the
    patch stashed under the wrong call, putting one write's code on another write's row. This
    drives two real ``_update_workflow`` calls whose persists interleave.
    """
    gate = asyncio.Event()
    first_entered = asyncio.Event()

    async def _process(**kwargs: object) -> SimpleNamespace:
        label = "alpha" if "alpha" in str(kwargs["workflow_yaml"]) else "beta"
        if label == "alpha":
            # Suspend the first write mid-persist so the second runs to completion inside it.
            first_entered.set()
            await gate.wait()
        return SimpleNamespace(
            workflow_definition=SimpleNamespace(blocks=[SimpleNamespace(label=label)]),
            proxy_location=None,
            webhook_callback_url=None,
        )

    async def _prior(_ctx: CopilotContext) -> None:
        return None

    monkeypatch.setattr(workflow_update_module, "_process_workflow_yaml", _process)
    monkeypatch.setattr(workflow_update_module, "_get_prior_workflow", _prior)

    ctx = _ctx()

    async def write(call_id: str, label: str, code: str) -> None:
        await _update_workflow(
            {"workflow_yaml": _code_yaml(code, label=label), "code_artifact_metadata": []},
            ctx,
            allow_missing_credentials=True,
            originating_call_id=call_id,
        )

    alpha = asyncio.create_task(write("c1", "alpha", 'return {"output": {"a": 1}}'))
    await first_entered.wait()
    await write("c2", "beta", 'return {"output": {"b": 2}}')
    gate.set()
    await alpha

    assert [d["label"] for d in ctx.pending_code_write_diffs["c1"]] == ["alpha"]
    assert [d["label"] for d in ctx.pending_code_write_diffs["c2"]] == ["beta"]


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["pending", "asked", "declined"])
async def test_origin_recovery_withdraws_neither_the_save_nor_the_run(
    monkeypatch: pytest.MonkeyPatch, state: CredentialOriginRecoveryState
) -> None:
    persisted: list[str] = []
    _stub_successful_update(monkeypatch, persisted)
    ctx = _ctx()
    ctx.credential_origin_recovery = CredentialOriginRecovery("https://idp.example.test", state, "cred_service")
    submitted = _code_yaml('return {"output": {"ok": True}}')

    result = await _update_workflow({"workflow_yaml": submitted, "code_artifact_metadata": []}, ctx)

    assert result["ok"] is True
    assert persisted == [submitted]
    for tool_name in ("run_blocks_and_collect_debug", "edit_block_and_run", "update_and_run_blocks"):
        assert _authority_tool_error(ctx, tool_name) is None


@pytest.mark.asyncio
async def test_accept_path_persists_model_yaml_and_code_exactly(monkeypatch: pytest.MonkeyPatch) -> None:
    persisted: list[str] = []
    _stub_successful_update(monkeypatch, persisted)
    ctx = _ctx()
    submitted = _code_yaml(
        """
        await page.locator("body").wait_for(state="visible", timeout=45000)
        await page.get_by_role("button", name="Search", exact=True).click()
        result = (await page.get_by_role("status").inner_text()).strip()
        return {"output": {"result": result}}
        """
    )

    result = await _update_workflow(
        {"workflow_yaml": submitted, "code_artifact_metadata": []},
        ctx,
        allow_missing_credentials=True,
    )

    assert result["ok"] is True
    assert persisted == [submitted]
    assert ctx.workflow_yaml == submitted
    assert _single_code(ctx.workflow_yaml) == _single_code(submitted)
    assert result["data"]["stored_code"]["submit_search"] == _single_code(submitted)
    assert 'page.locator("body").wait_for(state="visible", timeout=45000)' in _single_code(ctx.workflow_yaml)
    assert [finding["reason_code"] for finding in result["data"]["findings"]] == ["code_block_readiness_wait_advisory"]
    assert "imposed_substitutions" not in result["data"]


@pytest.mark.asyncio
async def test_accept_path_preserves_model_authored_goal_prompt_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    persisted: list[str] = []
    _stub_successful_update(monkeypatch, persisted)
    authored_prompt = 'Open the result, then return its visible "Status" — unchanged.'
    submitted = _code_yaml(
        'return {"output": {"status": await page.get_by_role("status").inner_text()}}',
        prompt=authored_prompt,
    )
    ctx = _ctx()

    result = await _update_workflow(
        {"workflow_yaml": submitted, "code_artifact_metadata": []},
        ctx,
        allow_missing_credentials=True,
    )

    assert result["ok"] is True
    assert persisted == [submitted]
    assert persisted[0].encode() == submitted.encode()
    submitted_block = workflow_blocks(parse_workflow_yaml(submitted))[0]
    persisted_block = workflow_blocks(parse_workflow_yaml(persisted[0]))[0]
    assert submitted_block["prompt"] == authored_prompt
    assert persisted_block["prompt"] == authored_prompt


@pytest.mark.asyncio
async def test_accept_path_does_not_synthesize_an_omitted_goal_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    persisted: list[str] = []
    _stub_successful_update(monkeypatch, persisted)
    submitted = _code_yaml('return {"output": {"status": "complete"}}')
    assert "prompt:" not in submitted
    ctx = _ctx()

    result = await _update_workflow(
        {"workflow_yaml": submitted, "code_artifact_metadata": []},
        ctx,
        allow_missing_credentials=True,
    )

    assert result["ok"] is True
    assert persisted == [submitted]
    assert persisted[0].encode() == submitted.encode()
    submitted_block = workflow_blocks(parse_workflow_yaml(submitted))[0]
    persisted_block = workflow_blocks(parse_workflow_yaml(persisted[0]))[0]
    assert "prompt" not in submitted_block
    assert "prompt" not in persisted_block


@pytest.mark.asyncio
async def test_requested_output_contract_does_not_rewrite_model_code(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_successful_update(monkeypatch)
    ctx = _ctx()
    ctx.request_policy = RequestPolicy(
        allow_update_workflow=True,
        allow_run_blocks=False,
        completion_criteria=[
            CompletionCriterion(
                id="record_id",
                outcome="Return the record id.",
                output_path="output.record_id",
                level="run",
                method_mandated=False,
                kind="outcome",
            )
        ],
    )
    submitted = _code_yaml(
        'record_id = "{{ business_name }}"\nreturn {"output": {"record_id": record_id}}',
        label="extract_record",
    )

    result = await _update_workflow(
        {"workflow_yaml": submitted},
        ctx,
        allow_missing_credentials=True,
    )

    assert result["ok"] is True
    assert _single_code(ctx.workflow_yaml) == (
        'record_id = "{{ business_name }}"\nreturn {"output": {"record_id": record_id}}\n'
    )


@pytest.mark.asyncio
async def test_google_lookup_failure_reuses_collected_bindings_without_retraversal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_successful_update(monkeypatch)
    ctx = _ctx()
    ctx.google_connection_turn_start_bindings = ()
    collected = False

    def _collect_once(_workflow: SimpleNamespace) -> tuple[tuple[str, str], ...]:
        nonlocal collected
        if collected:
            raise AssertionError("Google bindings were traversed more than once")
        collected = True
        return ()

    async def _lookup_fails(_organization_id: str) -> NoReturn:
        raise RuntimeError("lookup unavailable")

    monkeypatch.setattr(workflow_update_module, "google_sheet_connection_bindings", _collect_once)
    monkeypatch.setattr(
        workflow_update_module.google_oauth_service,
        "get_visible_credentials_for_org",
        _lookup_fails,
    )

    result = await _update_workflow(
        {"workflow_yaml": _code_yaml('return {"ok": True}')},
        ctx,
        allow_missing_credentials=True,
    )

    assert result["ok"] is True
    assert collected is True


@pytest.mark.asyncio
async def test_google_notice_baseline_is_captured_before_an_update_without_sheets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    baseline_yaml = _code_yaml('return {"turn_start": True}', label="turn_start")
    submitted_yaml = _code_yaml('return {"step": 1}')
    baseline_workflow = SimpleNamespace(
        workflow_definition=SimpleNamespace(blocks=[]),
        proxy_location=None,
        webhook_callback_url=None,
        google_bindings=(("existing_sheet", "goac_existing"),),
    )
    submitted_workflow = SimpleNamespace(
        workflow_definition=SimpleNamespace(blocks=[]),
        proxy_location=None,
        webhook_callback_url=None,
        google_bindings=(),
    )

    async def _process(**kwargs: object) -> SimpleNamespace:
        if kwargs["workflow_yaml"] == baseline_yaml:
            return baseline_workflow
        return submitted_workflow

    async def _prior(_ctx: CopilotContext) -> None:
        return None

    monkeypatch.setattr(workflow_update_module, "_process_workflow_yaml", _process)
    monkeypatch.setattr(workflow_update_module, "_get_prior_workflow", _prior)
    monkeypatch.setattr(
        workflow_update_module,
        "google_sheet_connection_bindings",
        lambda workflow: workflow.google_bindings,
    )
    monkeypatch.setattr(
        workflow_update_module.google_oauth_service,
        "get_visible_credentials_for_org",
        lambda _organization_id: _empty_credentials(),
    )
    ctx = _ctx(baseline_yaml)

    result = await _update_workflow(
        {"workflow_yaml": submitted_yaml},
        ctx,
        allow_missing_credentials=True,
    )

    assert result["ok"] is True
    assert ctx.google_connection_turn_start_bindings == (("existing_sheet", "goac_existing"),)


@pytest.mark.asyncio
async def test_google_notice_skips_lookup_when_turn_start_baseline_cannot_be_parsed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    baseline_yaml = _code_yaml('return {"turn_start": True}', label="turn_start")
    submitted_yaml = _code_yaml('return {"step": 1}')
    submitted_workflow = SimpleNamespace(
        workflow_definition=SimpleNamespace(blocks=[]),
        proxy_location=None,
        webhook_callback_url=None,
        google_bindings=(("new_sheet", "goac_error"),),
    )

    async def _process(**kwargs: object) -> SimpleNamespace:
        if kwargs["workflow_yaml"] == baseline_yaml:
            raise RuntimeError("baseline unavailable")
        return submitted_workflow

    async def _prior(_ctx: CopilotContext) -> None:
        return None

    async def _unexpected_lookup(_organization_id: str) -> NoReturn:
        raise AssertionError("credential lookup must wait for a valid baseline")

    monkeypatch.setattr(workflow_update_module, "_process_workflow_yaml", _process)
    monkeypatch.setattr(workflow_update_module, "_get_prior_workflow", _prior)
    monkeypatch.setattr(
        workflow_update_module,
        "google_sheet_connection_bindings",
        lambda workflow: workflow.google_bindings,
    )
    monkeypatch.setattr(
        workflow_update_module.google_oauth_service,
        "get_visible_credentials_for_org",
        _unexpected_lookup,
    )
    ctx = _ctx(baseline_yaml)

    result = await _update_workflow(
        {"workflow_yaml": submitted_yaml},
        ctx,
        allow_missing_credentials=True,
    )

    assert result["ok"] is True
    assert ctx.google_connection_turn_start_bindings is None
    assert ctx.google_connection_notices == []


async def _empty_credentials() -> list[SimpleNamespace]:
    return []


@pytest.mark.asyncio
async def test_google_notice_capture_records_every_sheets_binding_including_resolvable_ones(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_successful_update(monkeypatch)
    ctx = _ctx()
    ctx.google_connection_turn_start_bindings = ()
    current_bindings = iter(((), (("write_sheet", "goac_active"),), (("write_sheet", "goac_active"),)))
    captures: list[dict[str, object]] = []

    monkeypatch.setenv("COPILOT_DUMP_GOOGLE_CONNECTION_NOTICE_INPUTS", "/tmp/google-notice-capture")
    monkeypatch.setattr(
        workflow_update_module,
        "google_sheet_connection_bindings",
        lambda _workflow: next(current_bindings),
    )

    async def _visible_credentials(_organization_id: str) -> list[SimpleNamespace]:
        return [
            SimpleNamespace(
                id="goac_active",
                state="active",
                credential_name="Sheets account",
                scopes_granted=[GOOGLE_SHEETS_DATA_SCOPE],
            )
        ]

    monkeypatch.setattr(
        workflow_update_module.google_oauth_service,
        "get_visible_credentials_for_org",
        _visible_credentials,
    )
    monkeypatch.setattr(
        workflow_update_module,
        "write_google_connection_notice_capture",
        lambda **kwargs: captures.append(kwargs),
    )

    first = await _update_workflow(
        {"workflow_yaml": _code_yaml('return {"step": 1}')},
        ctx,
        allow_missing_credentials=True,
    )
    assert first["ok"] is True
    assert captures == []

    for step in (2, 3):
        accepted = await _update_workflow(
            {"workflow_yaml": _code_yaml(f'return {{"step": {step}}}')},
            ctx,
            allow_missing_credentials=True,
        )
        assert accepted["ok"] is True

    assert [capture["observed_notices"] for capture in captures] == [[], []]
    assert [capture["turn_id"] for capture in captures] == [ctx.turn_id, ctx.turn_id]


@pytest.mark.asyncio
async def test_model_declared_download_contract_is_written_into_proposed_yaml(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    persisted: list[str] = []
    _stub_successful_update(monkeypatch, persisted)
    ctx = _ctx()
    submitted = _code_yaml('return {"attempted": True}', label="download_statement")
    normalized_metadata = {
        "download_statement": {
            "block_label": "download_statement",
            "completion_criteria": [
                {
                    "id": "deliver_statement",
                    "text": "The requested statement is delivered as a registered file.",
                    "deliverable_kind": "registered_download",
                }
            ],
        }
    }
    monkeypatch.setattr(
        workflow_update_module,
        "_normalize_code_artifact_metadata_detailed",
        lambda *args, **kwargs: workflow_update_module.CodeArtifactNormalization(
            normalized_metadata,
            None,
            [],
            [],
        ),
    )

    result = await _update_workflow(
        {"workflow_yaml": submitted, "code_artifact_metadata": normalized_metadata},
        ctx,
        allow_missing_credentials=True,
    )

    assert result["ok"] is True
    parsed = parse_workflow_yaml(ctx.workflow_yaml)
    assert parsed["workflow_definition"]["completion_contract"] == {
        "schema_version": 1,
        "criteria": [{"id": "deliver_statement", "kind": "registered_download", "min_count": 1}],
    }
    assert _single_code(ctx.workflow_yaml) == _single_code(submitted)
    assert persisted == [ctx.workflow_yaml]


@pytest.mark.asyncio
async def test_model_declared_download_contract_keeps_write_seam_secret_redaction_effective(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    persisted: list[str] = []
    _stub_successful_update(monkeypatch, persisted)
    ctx = _ctx()
    # A whole-document YAML dump escapes the tab before the literal redactor can see it. This
    # pins the security-sensitive order: redact the submitted bytes first, then inject metadata.
    secret = "päss'word\t秘密-123456"
    register_secret_scrub_value(ctx, secret)
    submitted = _code_yaml(
        f'await page.locator("#password").fill("{secret}")',
        label="download_statement",
    )
    normalized_metadata = {
        "download_statement": {
            "block_label": "download_statement",
            "completion_criteria": [
                {
                    "id": "deliver_statement",
                    "text": "The requested statement is delivered as a registered file.",
                    "deliverable_kind": "registered_download",
                }
            ],
        }
    }
    monkeypatch.setattr(
        workflow_update_module,
        "_normalize_code_artifact_metadata_detailed",
        lambda *args, **kwargs: workflow_update_module.CodeArtifactNormalization(
            normalized_metadata,
            None,
            [],
            [],
        ),
    )

    result = await _update_workflow(
        {"workflow_yaml": submitted, "code_artifact_metadata": normalized_metadata},
        ctx,
        allow_missing_credentials=True,
    )

    assert result["ok"] is True
    assert secret not in ctx.workflow_yaml
    assert REDACTED_SECRET_PLACEHOLDER in ctx.workflow_yaml
    assert persisted and secret not in persisted[0]
    assert parse_workflow_yaml(ctx.workflow_yaml)["workflow_definition"]["completion_contract"]


@pytest.mark.asyncio
async def test_a_rewritten_submission_returns_the_stored_bytes_and_names_the_rewrite(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A following anchored edit is matched against what the server stored, so the write result has
    to say both what that is and that it is not what the model sent."""
    _stub_successful_update(monkeypatch)
    ctx = _ctx()
    secret = "hunter2-correct-horse"
    register_secret_scrub_value(ctx, secret)
    submitted = _code_yaml(f'await page.locator("#password").fill("{secret}")')

    result = await _update_workflow({"workflow_yaml": submitted}, ctx, allow_missing_credentials=True)

    stored = result["data"]["stored_code"]["submit_search"]
    assert result["data"]["stored_code_rewritten"] == ["submit_search"]
    assert stored == stored_block_code(stored_workflow_yaml(ctx), "submit_search")
    assert secret not in stored
    assert REDACTED_SECRET_PLACEHOLDER in stored

    edited = apply_block_edit(
        stored_workflow_yaml(ctx),
        "submit_search",
        expected_code=REDACTED_SECRET_PLACEHOLDER,
        replacement_code="{password}",
    )
    assert "{password}" in edited

    with pytest.raises(BlockEditError):
        apply_block_edit(
            stored_workflow_yaml(ctx), "submit_search", expected_code=secret, replacement_code="{password}"
        )


@pytest.mark.asyncio
async def test_a_submission_the_server_stored_verbatim_names_no_rewrite(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_successful_update(monkeypatch)
    ctx = _ctx()
    submitted = _code_yaml('await page.locator("#go").click()')

    result = await _update_workflow({"workflow_yaml": submitted}, ctx, allow_missing_credentials=True)

    assert result["data"]["stored_code"]["submit_search"]
    assert "stored_code_rewritten" not in result["data"]


@pytest.mark.asyncio
async def test_deleting_download_block_removes_its_model_declared_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    persisted: list[str] = []
    _stub_successful_update(monkeypatch, persisted)
    ctx = _ctx()
    prior_contract = {
        "schema_version": 1,
        "criteria": [{"id": "deliver_statement", "kind": "registered_download", "min_count": 1}],
    }
    ctx.workflow_yaml = _yaml(
        f"""
        title: Search
        workflow_definition:
          completion_contract: {json.dumps(prior_contract)}
          blocks:
          - block_type: code
            label: download_statement
            code: |
              return {{"attempted": True}}
          - block_type: code
            label: keep_status
            code: |
              return {{"status": "ready"}}
        """
    )
    ctx.code_artifact_metadata = {
        "download_statement": {
            "block_label": "download_statement",
            "completion_criteria": [
                {
                    "id": "deliver_statement",
                    "text": "The requested statement is delivered as a registered file.",
                    "deliverable_kind": "registered_download",
                }
            ],
        }
    }
    submitted = delete_block_from_workflow(ctx.workflow_yaml, "download_statement")
    normalized_metadata = {
        "keep_status": {
            "block_label": "keep_status",
            "completion_criteria": [{"id": "return_status", "text": "Return the current status."}],
        }
    }
    monkeypatch.setattr(
        workflow_update_module,
        "_normalize_code_artifact_metadata_detailed",
        lambda *args, **kwargs: workflow_update_module.CodeArtifactNormalization(
            normalized_metadata,
            None,
            [],
            [],
        ),
    )

    result = await _update_workflow(
        {"workflow_yaml": submitted, "code_artifact_metadata": normalized_metadata},
        ctx,
        allow_missing_credentials=True,
    )

    assert result["ok"] is True
    parsed = parse_workflow_yaml(ctx.workflow_yaml)
    assert "completion_contract" not in parsed["workflow_definition"]
    assert set(ctx.code_artifact_metadata) == {"keep_status"}
    assert ctx.clear_persisted_completion_contract is True
    assert persisted == [ctx.workflow_yaml]


@pytest.mark.asyncio
async def test_unsafe_code_is_rejected_without_persisting(monkeypatch: pytest.MonkeyPatch) -> None:
    persisted: list[str] = []
    _stub_successful_update(monkeypatch, persisted)
    ctx = _ctx()
    submitted = _code_yaml(
        """
        import requests
        await page.goto("https://example.com")
        """
    )

    result = await _update_workflow({"workflow_yaml": submitted}, ctx, allow_missing_credentials=True)

    assert result["ok"] is False
    assert result["block_id"] == "code_safety"
    assert persisted == []
    assert ctx.workflow_yaml == ""


@pytest.mark.asyncio
async def test_registered_live_secret_is_redacted_at_hard_safety_boundary(monkeypatch: pytest.MonkeyPatch) -> None:
    persisted: list[str] = []
    _stub_successful_update(monkeypatch, persisted)
    ctx = _ctx()
    secret = "fake-pa55w0rd-7x9"
    register_secret_scrub_value(ctx, secret)
    submitted = _code_yaml(f'await page.locator("#password").fill("{secret}")')

    result = await _update_workflow({"workflow_yaml": submitted}, ctx, allow_missing_credentials=True)

    assert result["ok"] is True
    assert secret not in ctx.workflow_yaml
    assert persisted and secret not in persisted[0]
    assert REDACTED_SECRET_PLACEHOLDER in ctx.workflow_yaml
    assert secret not in json.dumps(result["data"])


@pytest.mark.asyncio
async def test_exact_source_promotion_persists_when_the_code_is_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    persisted: list[str] = []
    _stub_successful_update(monkeypatch, persisted)
    ctx = _ctx()
    source = 'await page.locator("#submit").click()\n'
    submitted = _code_yaml(source)

    result = await _update_workflow(
        {
            "workflow_yaml": submitted,
            "_expected_exact_code_by_label": {"submit_search": source},
        },
        ctx,
        allow_missing_credentials=True,
    )

    assert result["ok"] is True
    assert len(persisted) == 1
    assert _single_code(persisted[0]) == source
    assert _single_code(ctx.workflow_yaml) == source


@pytest.mark.asyncio
async def test_exact_source_promotion_rejects_a_persistence_scrub_that_would_change_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    persisted: list[str] = []
    _stub_successful_update(monkeypatch, persisted)
    ctx = _ctx()
    source = 'await page.locator("#password").click()\n'
    register_secret_scrub_value(ctx, "password")
    submitted = _code_yaml(source)

    result = await _update_workflow(
        {
            "workflow_yaml": submitted,
            "_expected_exact_code_by_label": {"submit_search": source},
        },
        ctx,
        allow_missing_credentials=True,
    )

    assert result["ok"] is False
    assert result["error_code"] == "executed_source_changed_before_persistence"
    assert persisted == []
    assert ctx.workflow_yaml == ""


@pytest.mark.asyncio
async def test_run_path_rejects_changed_raw_load_balancer_webhook(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _ctx()
    workflow_yaml = _code_yaml('return {"public_form_exists": False}', label="validate_public_path")
    raw_webhook_url = "https://service-123.elb.us-east-1.amazonaws.com/hook"

    async def _prior(_ctx: CopilotContext) -> SimpleNamespace:
        return SimpleNamespace(webhook_callback_url="https://webhook.example.com/hook")

    async def _process(**_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(webhook_callback_url=raw_webhook_url)

    monkeypatch.setattr(workflow_update_module, "_get_prior_workflow", _prior)
    monkeypatch.setattr(workflow_update_module, "_process_workflow_yaml", _process)

    result = await _update_workflow(
        {"workflow_yaml": workflow_yaml},
        ctx,
        allow_missing_credentials=True,
    )

    assert result["ok"] is False
    assert "stable custom hostname" in result["error"]


def test_no_persistence_synthesis_exports_remain() -> None:
    names = set(vars(workflow_update_module))
    assert "_maybe_impose_synthesized_code_block" not in names
    assert "_maybe_impose_synthesized_code_block_decision" not in names
    assert "_impose_output_contract_envelope_after_steering" not in names
    assert "_scaffold_metadata_contract_for_update" not in names
    assert "_apply_scouted_typed_default_promotions" not in names


def test_no_output_contract_actuation_meta_plane_exports_remain() -> None:
    names = set(vars(workflow_update_module))
    assert "_grant_output_contract_advisory_run" not in names
    assert "consume_output_contract_advisory_grant_for_run" not in names
    assert "consume_output_contract_advisory_grant_for_run_result" not in names
    assert "record_output_contract_run_output_evidence" not in names


class TestBodyReadinessAdvisoryDelivery:
    _BODY_WAIT = 'body = page.locator("body")\nawait body.wait_for(state="visible", timeout=30000)\n'

    def _oversized_block(self) -> str:
        padding = "\n".join(f'value_{index} = "{index}"' for index in range(6000))
        return f'{self._BODY_WAIT}{padding}\nreturn {{"ok": True}}\n'

    def _findings(self, prior: str | None, accepted: str) -> list[dict[str, object]]:
        return _author_time_findings(
            schema_incompatibility=None,
            metadata_violations=[],
            code_block_diagnostics=_advisory_labels_by_diagnostic(
                _changed_code_blocks(prior, accepted, accepted)[0], accepted
            ),
        )

    def test_budget_withheld_block_still_carries_the_advisory(self) -> None:
        accepted = _code_yaml(self._oversized_block(), label="read_summary")

        stored_code, withheld = _accepted_code_delta(_changed_code_blocks(None, accepted, accepted)[0])
        findings = self._findings(None, accepted)

        assert stored_code == {}
        assert withheld
        assert [finding["reason_code"] for finding in findings] == ["code_block_readiness_wait_advisory"]
        assert "`read_summary`" in str(findings[0]["summary"])

    def test_unchanged_block_carries_no_advisory(self) -> None:
        prior = _code_yaml(self._BODY_WAIT, label="read_summary")

        assert self._findings(prior, prior) == []


@pytest.mark.asyncio
async def test_wrapper_scope_advisory_is_its_own_finding_on_a_draft_that_persists_and_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    persisted: list[str] = []
    _stub_successful_update(monkeypatch, persisted)
    ctx = _ctx()
    submitted = _code_yaml(
        """
        await page.locator("body").wait_for(state="visible", timeout=45000)
        matches = 0

        async def count_match():
            global matches
            matches += 1

        for _ in range(3):
            await count_match()
        return {"output": {"matches": matches}}
        """
    )

    result = await _update_workflow(
        {"workflow_yaml": submitted, "code_artifact_metadata": []},
        ctx,
        allow_missing_credentials=True,
    )

    assert result["ok"] is True
    assert persisted == [submitted]
    findings = result["data"]["findings"]
    assert [finding["reason_code"] for finding in findings] == [
        READINESS_WAIT_ADVISORY_REASON_CODE,
        WRAPPER_SCOPE_ADVISORY_REASON_CODE,
    ]
    assert "`global matches` at line 5 inside `count_match`" in findings[1]["summary"]
    assert "`submit_search`" in findings[1]["summary"]
    run_result = {"ok": False, "data": {"workflow_run_id": "wr_x", "overall_status": "failed"}}
    carried = carry_author_time_findings(result, run_result)["data"]
    assert carried["findings"] == findings
    assert carried["workflow_run_id"] == "wr_x"


@pytest.mark.asyncio
async def test_wrapper_scope_advisory_covers_a_global_on_a_declared_workflow_parameter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    persisted: list[str] = []
    _stub_successful_update(monkeypatch, persisted)
    ctx = _ctx()
    code = "\n".join(
        [
            "          async def bump():",
            "              global retries",
            "              retries += 1",
            "",
            "          await bump()",
            '          return {"output": {"retries": retries}}',
        ]
    )
    submitted = (
        "title: Search\n"
        "workflow_definition:\n"
        "  parameters:\n"
        "  - parameter_type: workflow\n"
        "    workflow_parameter_type: integer\n"
        "    key: retries\n"
        "    default_value: 0\n"
        "  blocks:\n"
        "  - block_type: code\n"
        "    label: submit_search\n"
        "    parameter_keys: [retries]\n"
        "    code: |\n"
        f"{code}\n"
    )

    result = await _update_workflow(
        {"workflow_yaml": submitted, "code_artifact_metadata": []},
        ctx,
        allow_missing_credentials=True,
    )

    assert result["ok"] is True
    findings = result["data"]["findings"]
    assert [finding["reason_code"] for finding in findings] == [WRAPPER_SCOPE_ADVISORY_REASON_CODE]
    assert "`global retries` at line 2 inside `bump` for a workflow parameter" in findings[0]["summary"]


@pytest.mark.asyncio
async def test_scanner_advisory_findings_never_block_a_save(monkeypatch: pytest.MonkeyPatch) -> None:
    persisted: list[str] = []
    _stub_successful_update(monkeypatch, persisted)
    ctx = _ctx()
    finding = CodeBlockScanFinding(rule_id="exfiltrate-sensitive-data", line=1, message="Sends data off-page.")

    async def _scan(
        code: str, *, organization_id: str | None = None, timeout_seconds: float = 3.0
    ) -> list[CodeBlockScanFinding]:
        return [finding]

    monkeypatch.setattr(app.AGENT_FUNCTION, "scan_code_block_source", _scan)
    submitted = _code_yaml('await page.goto("https://example.com")\nreturn {"ok": True}')

    result = await _update_workflow(
        {"workflow_yaml": submitted, "code_artifact_metadata": []},
        ctx,
        allow_missing_credentials=True,
    )

    assert result["ok"] is True
    assert persisted == [submitted]
    scanner_findings = [f for f in result["data"]["findings"] if f["reason_code"] == "code_block_scanner_advisory"]
    assert len(scanner_findings) == 1
    assert "`submit_search`" in scanner_findings[0]["summary"]
    assert (
        "Flagged by scanner rule `exfiltrate-sensitive-data` at line 1. Sends data off-page."
        in scanner_findings[0]["summary"]
    )


@pytest.mark.asyncio
async def test_scanner_failure_never_blocks_a_save(monkeypatch: pytest.MonkeyPatch) -> None:
    persisted: list[str] = []
    _stub_successful_update(monkeypatch, persisted)
    ctx = _ctx()

    async def _scan(code: str, *, organization_id: str | None = None, timeout_seconds: float = 3.0) -> NoReturn:
        raise RuntimeError("scanner unavailable")

    monkeypatch.setattr(app.AGENT_FUNCTION, "scan_code_block_source", _scan)
    submitted = _code_yaml('await page.goto("https://example.com")\nreturn {"ok": True}')

    result = await _update_workflow(
        {"workflow_yaml": submitted, "code_artifact_metadata": []},
        ctx,
        allow_missing_credentials=True,
    )

    assert result["ok"] is True
    assert persisted == [submitted]
    assert all(f["reason_code"] != "code_block_scanner_advisory" for f in result["data"].get("findings", []))


@pytest.mark.asyncio
async def test_oss_base_hook_yields_no_scanner_findings(monkeypatch: pytest.MonkeyPatch) -> None:
    persisted: list[str] = []
    _stub_successful_update(monkeypatch, persisted)
    ctx = _ctx()
    monkeypatch.setattr(app.AGENT_FUNCTION, "scan_code_block_source", AgentFunction().scan_code_block_source)
    submitted = _code_yaml('await page.goto("https://example.com")\nreturn {"ok": True}')

    result = await _update_workflow(
        {"workflow_yaml": submitted, "code_artifact_metadata": []},
        ctx,
        allow_missing_credentials=True,
    )

    assert result["ok"] is True
    assert persisted == [submitted]
    assert all(f["reason_code"] != "code_block_scanner_advisory" for f in result["data"].get("findings", []))


@pytest.mark.asyncio
@pytest.mark.parametrize("placeholder", ['""', "|"], ids=["empty string", "empty block scalar"])
async def test_executed_source_fills_an_empty_code_block_byte_for_byte(
    monkeypatch: pytest.MonkeyPatch, placeholder: str
) -> None:
    _stub_successful_update(monkeypatch)
    ctx = _ctx()
    executed = 'rows = await page.locator("tr").count()\nreturn {"rows": rows}'
    draft = (
        "title: Search\n"
        "workflow_definition:\n"
        "  blocks:\n"
        "  - block_type: code\n"
        "    label: count_rows\n"
        f"    code: {placeholder}\n"
    )
    promoted = apply_block_edit(draft, "count_rows", expected_code="", replacement_code=executed)

    result = await _update_workflow(
        {
            "workflow_yaml": promoted,
            "code_artifact_metadata": [],
            "_expected_exact_code_by_label": {"count_rows": executed},
        },
        ctx,
        allow_missing_credentials=True,
    )

    assert result["ok"] is True, result
    assert _single_code(ctx.workflow_yaml) == executed


_PRIOR_SIX_BLOCK_WORKFLOW = _yaml(
    """
    title: Support contact
    workflow_definition:
      blocks:
      - block_type: task
        engine: skyvern-3.0
        label: open_support_page
        url: https://example.test/support
        navigation_goal: Open the support page.
      - block_type: task
        engine: skyvern-3.0
        label: read_support_contact
        url: ''
        navigation_goal: Find the support contact.
        data_extraction_goal: Extract the support email address.
      - block_type: task
        engine: skyvern-3.0
        label: confirm_contact_present
        url: ''
        navigation_goal: Confirm the support contact section is present.
      - block_type: navigation
        label: open_ticket_form
        url: https://example.test/tickets/new
        navigation_goal: Open the new ticket form.
      - block_type: for_loop
        label: each_ticket
        loop_over_parameter_key: tickets
        loop_blocks:
        - block_type: task
          engine: skyvern-3.0
          label: read_ticket
          url: ''
          navigation_goal: Read the ticket.
    """
)
_DROPPED_BY_SINGLE_CODE_WRITE = [
    "confirm_contact_present",
    "each_ticket",
    "open_support_page",
    "open_ticket_form",
    "read_ticket",
]
_RETYPED_BY_SINGLE_CODE_WRITE = {"read_support_contact": {"from": "task", "to": "code"}}


async def _public_update(monkeypatch: pytest.MonkeyPatch, ctx: CopilotContext, submitted: str) -> dict[str, object]:
    monkeypatch.setattr(tools_module, "_get_prior_workflow_definition", AsyncMock(return_value=None))
    raw = await tools_module.update_workflow_tool.on_invoke_tool(
        SimpleNamespace(context=ctx, tool_name="update_workflow"), json.dumps({"workflow_yaml": submitted})
    )
    return json.loads(raw)


@pytest.mark.asyncio
async def test_a_write_that_drops_and_retypes_prior_blocks_persists_and_names_both(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    persisted: list[str] = []
    _stub_successful_update(monkeypatch, persisted)
    ctx = _ctx(_PRIOR_SIX_BLOCK_WORKFLOW, policy=BlockAuthoringPolicy.STANDARD)
    submitted = _code_yaml('await page.goto("https://example.test/support")', label="read_support_contact")

    result = await _public_update(monkeypatch, ctx, submitted)

    assert result["ok"] is True
    assert persisted and "read_support_contact" in persisted[0]
    data = result["data"]
    assert data["dropped_prior_blocks"] == _DROPPED_BY_SINGLE_CODE_WRITE
    assert data["block_type_changes"] == _RETYPED_BY_SINGLE_CODE_WRITE

    run_result = {"ok": True, "data": {"workflow_run_id": "wr_x", "overall_status": "completed"}}
    skip_result = {"ok": True, "data": {"skipped_run": True, "skip_reason": "workflow_credential_inputs_unbound"}}
    for combined in (run_result, skip_result):
        carried = carry_author_time_findings(result, combined)["data"]
        assert carried["dropped_prior_blocks"] == _DROPPED_BY_SINGLE_CODE_WRITE
        assert carried["block_type_changes"] == _RETYPED_BY_SINGLE_CODE_WRITE


@pytest.mark.asyncio
async def test_a_write_that_keeps_every_prior_block_names_no_drop_or_type_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_successful_update(monkeypatch)
    ctx = _ctx(_PRIOR_SIX_BLOCK_WORKFLOW, policy=BlockAuthoringPolicy.STANDARD)
    submitted = _PRIOR_SIX_BLOCK_WORKFLOW.replace("block_type: navigation", "block_type: browser_task") + (
        '  - block_type: code\n    label: record_contact\n    code: |\n      return {"ok": True}\n'
    )

    result = await _public_update(monkeypatch, ctx, submitted)

    assert result["ok"] is True
    assert "dropped_prior_blocks" not in result["data"]
    assert "block_type_changes" not in result["data"]


@pytest.mark.asyncio
async def test_a_second_write_in_a_turn_diffs_against_the_last_in_turn_definition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_successful_update(monkeypatch)
    ctx = _ctx(_PRIOR_SIX_BLOCK_WORKFLOW, policy=BlockAuthoringPolicy.STANDARD)
    ctx.last_workflow_yaml = _yaml(
        """
        title: Support contact
        workflow_definition:
          blocks:
          - block_type: task
            engine: skyvern-3.0
            label: read_support_contact
            url: ''
            navigation_goal: Find the support contact.
          - block_type: task
            engine: skyvern-3.0
            label: confirm_contact_present
            url: ''
            navigation_goal: Confirm the support contact section is present.
        """
    )
    submitted = _code_yaml('await page.goto("https://example.test/support")', label="read_support_contact")

    result = await _public_update(monkeypatch, ctx, submitted)

    assert result["ok"] is True
    assert result["data"]["dropped_prior_blocks"] == ["confirm_contact_present"]
    assert result["data"]["block_type_changes"] == _RETYPED_BY_SINGLE_CODE_WRITE


@pytest.mark.asyncio
async def test_a_write_that_waited_on_a_sibling_write_diffs_against_what_it_replaces(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A sibling that published and released the lock, but whose tool call has not returned yet, is
    still the definition this write replaces; its blocks must not vanish unreported."""
    _stub_successful_update(monkeypatch)
    ctx = _ctx(_PRIOR_SIX_BLOCK_WORKFLOW, policy=BlockAuthoringPolicy.STANDARD)
    ctx.last_workflow_yaml = _PRIOR_SIX_BLOCK_WORKFLOW
    sibling_wrote = _yaml(
        """
        title: Support contact
        workflow_definition:
          blocks:
          - block_type: task
            engine: skyvern-3.0
            label: read_support_contact
            url: ''
            navigation_goal: Find the support contact.
          - block_type: navigation
            label: added_by_sibling
            url: https://example.test/tickets/new
            navigation_goal: Open the new ticket form.
        """
    )

    sibling = await workflow_update_module._update_workflow({"workflow_yaml": sibling_wrote}, ctx)
    assert sibling["ok"] is True
    submitted = _code_yaml('await page.goto("https://example.test/support")', label="read_support_contact")

    result = await _public_update(monkeypatch, ctx, submitted)

    assert result["ok"] is True
    assert result["data"]["dropped_prior_blocks"] == ["added_by_sibling"]
    assert result["data"]["block_type_changes"] == _RETYPED_BY_SINGLE_CODE_WRITE
