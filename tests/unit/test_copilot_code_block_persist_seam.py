"""Persistence custody tests for model-authored Workflow Copilot code blocks.

The accepted path is lossless: deterministic code synthesis, selector substitution, metadata
scaffolding, and output-envelope insertion are not persistence responsibilities. Existing hard
safety checks may reject a submission, and registered live credential values remain redacted.
"""

from __future__ import annotations

import json
import textwrap
from types import SimpleNamespace
from typing import NoReturn

import pytest

from skyvern.forge.sdk.copilot.config import BlockAuthoringPolicy
from skyvern.forge.sdk.copilot.context import CopilotContext
from skyvern.forge.sdk.copilot.request_policy import (
    CompletionCriterion,
    RequestPolicy,
)
from skyvern.forge.sdk.copilot.secret_scrub import REDACTED_SECRET_PLACEHOLDER, register_secret_scrub_value
from skyvern.forge.sdk.copilot.tools import workflow_update as workflow_update_module
from skyvern.forge.sdk.copilot.tools.workflow_update import CodeArtifactCompletionCriterion, _update_workflow
from skyvern.forge.sdk.copilot.workflow_credential_utils import parse_workflow_yaml, workflow_blocks
from skyvern.forge.sdk.copilot.workflow_yaml import delete_block_from_workflow


def _yaml(body: str) -> str:
    return textwrap.dedent(body).strip() + "\n"


def _ctx(workflow_yaml: str = "") -> CopilotContext:
    ctx = CopilotContext(
        organization_id="o",
        workflow_id="w",
        workflow_permanent_id="wp",
        workflow_yaml=workflow_yaml,
        browser_session_id=None,
        stream=None,
    )
    ctx.block_authoring_policy = BlockAuthoringPolicy.CODE_ONLY_BROWSER
    ctx.request_policy = RequestPolicy(allow_update_workflow=True, allow_run_blocks=False)
    return ctx


def _code_yaml(code: str, *, label: str = "submit_search") -> str:
    indented = "\n".join(f"          {line}" for line in textwrap.dedent(code).strip().splitlines())
    return (
        "title: Search\n"
        "workflow_definition:\n"
        "  blocks:\n"
        "  - block_type: code\n"
        f"    label: {label}\n"
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
async def test_accept_path_persists_model_yaml_and_code_exactly(monkeypatch: pytest.MonkeyPatch) -> None:
    persisted: list[str] = []
    _stub_successful_update(monkeypatch, persisted)
    ctx = _ctx()
    submitted = _code_yaml(
        """
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
    assert "imposed_substitutions" not in result["data"]


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
async def test_google_notice_capture_waits_for_a_relevant_binding(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_successful_update(monkeypatch)
    ctx = _ctx()
    ctx.google_connection_turn_start_bindings = ()
    current_bindings = iter(((), (("write_sheet", "goac_error"),)))
    captures: list[dict[str, object]] = []

    monkeypatch.setenv("COPILOT_DUMP_GOOGLE_CONNECTION_NOTICE_INPUTS", "/tmp/google-notice-capture")
    monkeypatch.setattr(
        workflow_update_module,
        "google_sheet_connection_bindings",
        lambda _workflow: next(current_bindings),
    )

    async def _visible_credentials(_organization_id: str) -> list[SimpleNamespace]:
        return [SimpleNamespace(id="goac_error", state="error", credential_name="Needs reconnect")]

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
    assert ctx.google_connection_notice_capture_written is False

    second = await _update_workflow(
        {"workflow_yaml": _code_yaml('return {"step": 2}')},
        ctx,
        allow_missing_credentials=True,
    )
    assert second["ok"] is True
    assert len(captures) == 1
    assert ctx.google_connection_notice_capture_written is True


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
