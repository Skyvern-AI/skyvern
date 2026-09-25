from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
import yaml
from pydantic import ValidationError
from structlog.testing import capture_logs

from skyvern.exceptions import WorkflowNotFound
from skyvern.forge import app
from skyvern.forge.sdk.copilot import workflow_yaml as workflow_yaml_module
from skyvern.forge.sdk.copilot.config import BlockAuthoringPolicy
from skyvern.forge.sdk.copilot.context import CopilotContext
from skyvern.forge.sdk.copilot.output_utils import sanitize_tool_result_for_llm
from skyvern.forge.sdk.copilot.request_policy import RequestPolicy
from skyvern.forge.sdk.copilot.tools import _mark_credential_deferred_draft
from skyvern.forge.sdk.copilot.tools import workflow_update as workflow_update_module
from skyvern.forge.sdk.copilot.tools.run_execution import _packet_workflow_readback
from skyvern.forge.sdk.copilot.tools.workflow_update import (
    _PERSISTENCE_MESSAGES,
    _update_workflow,
    carry_author_time_findings,
)
from skyvern.forge.sdk.copilot.workflow_yaml import _process_workflow_yaml, apply_block_edit
from skyvern.forge.sdk.workflow.models.workflow import Workflow, WorkflowDefinition
from skyvern.schemas.runs import ProxyLocation
from skyvern.schemas.workflows import WorkflowCreateYAMLRequest, WorkflowStatus

_SETTING_VALUES: dict[str, tuple[Any, Any]] = {
    "is_saved_task": (True, False),
    "description": ("Saved description", "Edited description"),
    "webhook_callback_url": ("https://example.test/saved-hook", "https://example.test/edited-hook"),
    "persist_browser_session": (True, False),
    "reuse_browser_session": (True, False),
    "browser_profile_id": ("bp_saved", "bp_edited"),
    "browser_profile_key": ("saved-profile", "edited-profile"),
    "model": ({"llm_key": "SAVED"}, {"llm_key": "EDITED"}),
    "max_screenshot_scrolls": (7, 0),
    "max_elapsed_time_minutes": (30, 15),
    "generate_script_on_terminal": (True, False),
    "status": (WorkflowStatus.draft.value, WorkflowStatus.published.value),
    "run_with": ("code", "agent"),
    "browser_type": ("msedge", "chrome"),
    "ai_fallback": (False, True),
    "cache_key": ("saved-cache", "edited-cache"),
    "adaptive_caching": (True, False),
    "code_version": (2, 1),
    "run_sequentially": (True, False),
    "sequential_key": ("saved-sequence", "edited-sequence"),
    "extra_http_headers": ({"X-Saved": "synthetic-http"}, {"X-Edited": "synthetic-new-http"}),
    "cdp_connect_headers": ({"X-Saved": "synthetic-cdp"}, {"X-Edited": "synthetic-new-cdp"}),
    "proxy_location": (
        {"url": "http://saved.proxy.example.test:8080"},
        {"url": "http://edited.proxy.example.test:8080"},
    ),
    "totp_identifier": ("synthetic-saved-otp", "synthetic-edited-otp"),
    "totp_verification_url": ("https://example.test/saved-otp", "https://example.test/edited-otp"),
    "enable_self_healing": (True, False),
    "mask_secrets": (True, False),
    "pin_saved_session_ip": (True, False),
}

# These request fields reject null before the conversion's inheritance rule applies.
_NON_NULL_SETTINGS = {
    "is_saved_task",
    "persist_browser_session",
    "reuse_browser_session",
    "generate_script_on_terminal",
    "status",
    "ai_fallback",
    "adaptive_caching",
    "run_sequentially",
    "pin_saved_session_ip",
}


@pytest.mark.asyncio
@pytest.mark.parametrize("field_name", _SETTING_VALUES)
@pytest.mark.parametrize("mode", ["omitted", "null", "replace"])
@pytest.mark.parametrize("source", ["stored", "fallback"])
async def test_workflow_yaml_settings_presence_contract(
    monkeypatch: pytest.MonkeyPatch, field_name: str, mode: str, source: str
) -> None:
    now = datetime.now(UTC)
    saved_value, replacement = _SETTING_VALUES[field_name]
    stored = Workflow(
        workflow_id="w",
        organization_id="o",
        workflow_permanent_id="wp",
        title="Settings contract",
        version=1,
        workflow_definition=WorkflowDefinition(parameters=[], blocks=[]),
        created_at=now,
        modified_at=now,
        **{name: values[0] for name, values in _SETTING_VALUES.items()},
    )
    monkeypatch.setattr(
        app.WORKFLOW_SERVICE,
        "get_workflow_by_permanent_id",
        AsyncMock(return_value=stored if source == "stored" else stored.model_copy(update={field_name: replacement})),
    )
    document = {
        "title": "Settings contract",
        "workflow_definition": {"parameters": [], "blocks": []},
        **{name: values[1] for name, values in _SETTING_VALUES.items()},
    }
    if mode == "omitted":
        del document[field_name]
    elif mode == "null":
        document[field_name] = None

    async def convert() -> Workflow:
        return await _process_workflow_yaml(
            "w",
            "wp",
            "o",
            yaml.safe_dump(document),
            settings_fallback_workflow=stored if source == "fallback" else None,
        )

    if mode == "null" and field_name in _NON_NULL_SETTINGS:
        with pytest.raises(ValidationError) as exc:
            await convert()
        assert any(error["loc"] == (field_name,) for error in exc.value.errors())
        return

    updated = await convert()
    if mode == "omitted" or mode == "null" and field_name in {"enable_self_healing", "mask_secrets"}:
        expected = saved_value
    elif mode == "null":
        expected = {
            "extra_http_headers": {},
            "cdp_connect_headers": {},
            "run_with": "agent",
        }.get(field_name)
    else:
        expected = replacement
    assert getattr(updated, field_name) == expected
    if field_name == "browser_type":
        assert yaml.safe_load(workflow_yaml_module.workflow_to_copilot_yaml(updated)).get(field_name) == expected


@pytest.mark.asyncio
async def test_workflow_yaml_settings_without_stored_workflow_use_request_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        app.WORKFLOW_SERVICE, "get_workflow_by_permanent_id", AsyncMock(side_effect=WorkflowNotFound("wp"))
    )
    document = {"title": "New workflow", "workflow_definition": {"parameters": [], "blocks": []}}
    request = WorkflowCreateYAMLRequest.model_validate(document)

    updated = await _process_workflow_yaml("w", "wp", "o", yaml.safe_dump(document))

    for field_name in _SETTING_VALUES:
        expected = getattr(request, field_name)
        if field_name in {"enable_self_healing", "mask_secrets"}:
            expected = False
        assert getattr(updated, field_name) == expected, field_name


@pytest.mark.parametrize("document", ["workflow_definition: [", "value: !!int invalid", "private scalar", None])
def test_strip_copilot_yaml_headers_withholds_unparsable_input(document: str | None) -> None:
    assert workflow_update_module.strip_copilot_yaml_headers(document) is None


@pytest.mark.parametrize("default", ["2024-01-02", "2024-01-02T03:04:05Z"])
@pytest.mark.parametrize("has_headers", [False, True])
def test_strip_copilot_yaml_headers_preserves_date_string_defaults(default: str, has_headers: bool) -> None:
    document = (
        "# preserve this comment when no headers need stripping\n"
        "title: Date parameter\n"
        "workflow_definition:\n"
        "  parameters:\n"
        "    - key: date\n"
        "      parameter_type: workflow\n"
        "      workflow_parameter_type: string\n"
        f"      default_value: {default}\n"
        "  blocks: []\n"
    )
    if has_headers:
        document += "extra_http_headers: {X-Test: synthetic-value}\n"
    stripped = workflow_update_module.strip_copilot_yaml_headers(document)
    if has_headers:
        parsed = yaml.safe_load(stripped)
        assert parsed["extra_http_headers"] == {"X-Test": "***"}
        assert parsed["workflow_definition"]["parameters"][0]["default_value"] == default
    else:
        assert stripped == document


@pytest.mark.asyncio
async def test_scoped_code_edit_crosses_normal_persistence_without_rewriting_other_bytes(
    monkeypatch: pytest.MonkeyPatch,
    no_saved_workflow: None,
) -> None:
    stored = """# keep this comment
title: Account lookup
workflow_definition:
  parameters:
    - key: month
      parameter_type: workflow
      workflow_parameter_type: string
  blocks:
    - block_type: code
      label: open_statement
      code: |
        await page.goto("https://example.test/")
        await page.locator("#stale-button").click()
      next_block_label: read_total
    - block_type: code
      label: read_total
      code: |
        total = await page.locator("#total").inner_text()
        return {"output": {"total": total}}
      parameter_keys: [month] # preserve inline metadata
"""
    edited = apply_block_edit(
        stored,
        "open_statement",
        expected_code='page.locator("#stale-button")',
        replacement_code='page.get_by_role("button", name="View statement")',
    )
    persisted: list[str] = []

    async def process(**kwargs: Any) -> Workflow:
        persisted.append(str(kwargs["workflow_yaml"]))
        return await _process_workflow_yaml(**kwargs)

    async def prior(_ctx: CopilotContext) -> None:
        return None

    monkeypatch.setattr(workflow_update_module, "_process_workflow_yaml", process)
    monkeypatch.setattr(workflow_update_module, "_get_prior_workflow", prior)
    ctx = CopilotContext(
        organization_id="o",
        workflow_id="w",
        workflow_permanent_id="wp",
        workflow_yaml=stored,
        browser_session_id=None,
        stream=None,
    )
    ctx.block_authoring_policy = BlockAuthoringPolicy.CODE_ONLY_BROWSER
    ctx.request_policy = RequestPolicy(allow_update_workflow=True, allow_run_blocks=True)
    ctx.google_connection_turn_start_bindings = ()

    result = await _update_workflow({"workflow_yaml": edited}, ctx, allow_missing_credentials=True)

    assert result["ok"] is True
    assert persisted
    assert persisted[-1] == edited
    assert ctx.workflow_yaml == edited
    assert (
        edited.replace('page.get_by_role("button", name="View statement")', 'page.locator("#stale-button")') == stored
    )


_STAGED_WORKFLOW_YAML = """title: Account lookup
workflow_definition:
  parameters: []
  blocks:
    - block_type: code
      label: read_total
      code: |
        total = await page.locator("#total").inner_text()
        return {"output": {"total": total}}
"""


async def _staged_update(
    monkeypatch: pytest.MonkeyPatch, *, auto_accept: bool | None = False, canonical_param_write: bool = False
) -> tuple[dict[str, Any], CopilotContext]:
    edited = apply_block_edit(
        _STAGED_WORKFLOW_YAML,
        "read_total",
        expected_code='page.locator("#total")',
        replacement_code='page.locator("#grand-total")',
    )

    async def prior(_ctx: CopilotContext) -> None:
        return None

    monkeypatch.setattr(app.WORKFLOW_SERVICE, "get_workflow_by_permanent_id", AsyncMock(return_value=None))
    monkeypatch.setattr(workflow_update_module, "_get_prior_workflow", prior)
    ctx = CopilotContext(
        organization_id="o",
        workflow_id="w",
        workflow_permanent_id="wp",
        workflow_yaml=_STAGED_WORKFLOW_YAML,
        browser_session_id=None,
        stream=None,
    )
    ctx.block_authoring_policy = BlockAuthoringPolicy.CODE_ONLY_BROWSER
    ctx.request_policy = RequestPolicy(allow_update_workflow=True, allow_run_blocks=True)
    ctx.google_connection_turn_start_bindings = ()
    ctx.auto_accept = auto_accept
    ctx.canonical_was_persisted_due_to_param_change = canonical_param_write

    result = await _update_workflow({"workflow_yaml": edited}, ctx, allow_missing_credentials=True)
    return result, ctx


@pytest.mark.asyncio
async def test_staged_write_reports_the_saved_workflow_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    result, ctx = await _staged_update(monkeypatch)

    assert result["ok"] is True
    assert ctx.has_staged_proposal is True
    data = result["data"]
    assert data["persistence"] == "staged"
    message = data["message"]
    assert message == data["persistence_message"]
    assert "Accepting it makes this version the saved workflow" in message
    assert "discarding it keeps the current one" in message
    assert _save_claims(data) == []


@pytest.mark.asyncio
async def test_combined_tool_result_carries_staging_without_overwriting_its_run_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    update_result, _ = await _staged_update(monkeypatch)
    run_result: dict[str, Any] = {
        "ok": True,
        "data": {"message": "Ran 1 block. Extracted total: $42.00.", "workflow_updated": True},
    }

    skip_result: dict[str, Any] = {
        "ok": True,
        "message": "Skipped test run: required credentials are not configured.",
        "data": {"workflow_updated": True, "skipped_run": True},
    }

    carried = carry_author_time_findings(update_result, run_result)["data"]
    skipped = carry_author_time_findings(update_result, skip_result)["data"]

    assert carried["persistence"] == "staged"
    assert carried["persistence_message"] == update_result["data"]["persistence_message"]
    assert carried["message"] == "Ran 1 block. Extracted total: $42.00."
    assert skipped["persistence"] == "staged"
    assert skipped["persistence_message"] == update_result["data"]["persistence_message"]


_SAVE_CLAIM_PHRASES = ("updated successfully", "has been saved", "saved the workflow", "workflow was saved")


def _save_claims(data: dict[str, Any]) -> list[str]:
    return [
        f"{key}: {value}"
        for key, value in data.items()
        if isinstance(value, str) and any(phrase in value.casefold() for phrase in _SAVE_CLAIM_PHRASES)
    ]


# The route refuses auto-apply for any proposal that is not ``auto_applicable`` — an unverified edit
# stays staged even on an auto-accept chat — so the tool may describe apply intent but never promise it.
_UNCONDITIONAL_APPLY_PHRASES = (
    "is accepted at the end",
    "will be accepted",
    "will be saved",
    "is applied automatically",
    "is saved automatically",
)


def _unconditional_apply_claims(data: dict[str, Any]) -> list[str]:
    return [
        f"{key}: {value}"
        for key, value in data.items()
        if isinstance(value, str) and any(phrase in value.casefold() for phrase in _UNCONDITIONAL_APPLY_PHRASES)
    ]


@pytest.mark.asyncio
async def test_auto_accept_write_reports_apply_intent_without_claiming_a_completed_save(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, ctx = await _staged_update(monkeypatch, auto_accept=True)

    assert ctx.has_staged_proposal is True
    data = result["data"]
    assert data["persistence"] == "staged_auto_apply"
    message = data["message"]
    assert message == data["persistence_message"]
    assert "accepts proposals automatically" in message
    assert "stays staged for review" in message
    assert _save_claims(data) == []
    assert _unconditional_apply_claims(data) == []


def test_no_disposition_value_asserts_a_completed_write() -> None:
    # The model reads the token as well as the message, so a value the turn can still refuse must not
    # read as an accomplished save. Nothing is persisted at tool time on either path.
    assert set(_PERSISTENCE_MESSAGES) == {"staged", "staged_auto_apply"}


@pytest.mark.asyncio
async def test_unknown_auto_accept_and_a_rolled_back_canonical_write_stay_staged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, _ = await _staged_update(monkeypatch, auto_accept=None)
    rolled_back, rolled_back_ctx = await _staged_update(monkeypatch, auto_accept=None, canonical_param_write=True)

    assert rolled_back_ctx.canonical_was_persisted_due_to_param_change is True
    assert result["data"]["persistence"] == "staged"
    assert rolled_back["data"]["persistence"] == "staged"
    assert _save_claims(result["data"]) == []
    assert _save_claims(rolled_back["data"]) == []


@pytest.mark.asyncio
async def test_credential_deferred_draft_rewrites_the_message_but_keeps_the_disposition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, ctx = await _staged_update(monkeypatch)

    _mark_credential_deferred_draft(ctx, result)

    data = result["data"]
    assert data["persistence"] == "staged"
    assert data["persistence_message"] == _PERSISTENCE_MESSAGES["staged"]
    assert data["message"] != data["persistence_message"]
    assert _save_claims(data) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("typed_proposal", [False, True])
async def test_restore_pending_proposal_withholds_malformed_yaml(
    monkeypatch: pytest.MonkeyPatch, typed_proposal: bool
) -> None:
    document = "totp_identifier: synthetic-private\nvalue: !!int invalid"
    proposal = {"_copilot_yaml": document}
    if typed_proposal:
        proposal["workflow_definition"] = {"blocks": [], "parameters": []}
    chat = SimpleNamespace(workflow_permanent_id="wp", proposed_workflow=proposal)
    monkeypatch.setattr(app.DATABASE.workflow_params, "get_workflow_copilot_chat_by_id", AsyncMock(return_value=chat))
    ctx = CopilotContext(
        organization_id="o",
        workflow_id="w",
        workflow_permanent_id="wp",
        workflow_copilot_chat_id="chat",
        workflow_yaml=document,
        browser_session_id=None,
        stream=None,
    )
    await workflow_update_module.restore_pending_workflow_proposal(ctx)
    assert ctx.workflow_yaml is None
    assert ctx.staged_workflow is None
    assert ctx.private_workflow_settings == {}


@pytest.mark.parametrize("authored", [False, True])
def test_candidate_proposal_resolves_masks_against_carried_headers(authored: bool) -> None:
    now = datetime.now(UTC)
    workflow = Workflow(
        workflow_id="w",
        workflow_permanent_id="wp",
        organization_id="o",
        title="Header fixture",
        version=1,
        is_saved_task=False,
        workflow_definition=WorkflowDefinition(parameters=[], blocks=[]),
        created_at=now,
        modified_at=now,
    )
    carried = {
        name: {"X-Rotated": f"unsaved-{name}", "X-Removed": "old", "X-Replaced": "old"}
        for name in ("extra_http_headers", "cdp_connect_headers")
    }
    ctx = CopilotContext(
        organization_id="o",
        workflow_id="w",
        workflow_permanent_id="wp",
        workflow_yaml="",
        private_workflow_settings={} if authored else carried,
        authored_private_workflow_settings=carried if authored else None,
        browser_session_id=None,
        stream=None,
    )
    document = {name: {"X-Rotated": "***", "X-Replaced": "model-literal", "X-Unbound": "***"} for name in carried}

    proposal = workflow_update_module._candidate_proposal_data(workflow, yaml.safe_dump(document), ctx)

    for name, headers in carried.items():
        expected = {"X-Rotated": headers["X-Rotated"], "X-Replaced": "model-literal", "X-Unbound": "***"}
        assert proposal[workflow_update_module.COPILOT_PRIVATE_SETTINGS_KEY][name] == expected
        assert proposal[name] == expected
        assert headers["X-Removed"] == "old"
        assert headers["X-Replaced"] == "old"


def test_private_settings_restore_preserves_untouched_request_fields() -> None:
    request = workflow_yaml_module._normalize_copilot_yaml(_STAGED_WORKFLOW_YAML)
    request = request.model_copy(
        update={
            "description": None,
            "cdp_connect_headers": {"Authorization": "synthetic-cdp-binding"},
            "model": {"llm_key": "PRIMARY"},
        }
    )
    private_settings = {"totp_identifier": None, "webhook_callback_url": None, "proxy_location": "residential"}
    original_fields_set = request.model_fields_set.copy()

    restored = workflow_yaml_module.with_private_workflow_settings(
        request,
        {
            **private_settings,
            "title": "Obsolete override",
            "block_totp_settings": {"removed": {"totp_identifier": "obsolete"}},
            "proxy_location_geo_fields": {"country": "US", "city": "obsolete"},
        },
    )

    assert restored.model_fields_set == original_fields_set | private_settings.keys()
    assert request.model_fields_set == original_fields_set
    assert restored.totp_identifier is None
    assert restored.webhook_callback_url is None
    assert restored.proxy_location == ProxyLocation.RESIDENTIAL
    assert restored.cdp_connect_headers == {"Authorization": "synthetic-cdp-binding"}
    for field_name in WorkflowCreateYAMLRequest.model_fields.keys() - private_settings.keys():
        assert getattr(restored, field_name) is getattr(request, field_name)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field_name", ["totp_identifier", "totp_verification_url", "webhook_callback_url", "proxy_location"]
)
async def test_private_workflow_validation_errors_withhold_values(no_saved_workflow: None, field_name: str) -> None:
    with pytest.raises(yaml.YAMLError) as error:
        await _process_workflow_yaml(
            "w",
            "wp",
            "o",
            _STAGED_WORKFLOW_YAML,
            private_workflow_settings={field_name: ["private-error-sentinel"]},
        )
    assert str(error.value) == f"Invalid private workflow settings ({field_name}): ValidationError"
    assert error.value.__suppress_context__ is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field_name,authored",
    [("webhook_callback_url", False), ("totp_verification_url", False), ("webhook_callback_url", True)],
)
async def test_update_carried_invalid_url_is_not_disclosed(
    monkeypatch: pytest.MonkeyPatch, no_saved_workflow: None, field_name: str, authored: bool
) -> None:
    private_url = "htps://hooks.example.test/ingest?signature=synthetic-private-signature"
    ctx = CopilotContext(
        organization_id="o",
        workflow_id="w",
        workflow_permanent_id="wp",
        workflow_yaml=_STAGED_WORKFLOW_YAML,
        private_workflow_settings={field_name: private_url},
        browser_session_id=None,
        stream=AsyncMock(),
    )
    ctx.google_connection_turn_start_bindings = ()
    monkeypatch.setattr(workflow_update_module, "_get_prior_workflow", AsyncMock(return_value=None))
    document = yaml.safe_load(_STAGED_WORKFLOW_YAML)
    if authored:
        document[field_name] = private_url
    with capture_logs() as logs:
        result = await _update_workflow(
            {"workflow_yaml": yaml.safe_dump(document)}, ctx, allow_missing_credentials=True
        )
    model_result = sanitize_tool_result_for_llm("update_workflow", result)
    if authored:
        assert result["ok"] is False
        assert private_url not in model_result["error"]
        assert "webhook_callback_url" in model_result["error"]
        assert "unsupported scheme" in model_result["error"]
        return
    streamed_errors = [
        call.args[0].model_dump(mode="json") for call in ctx.stream.send.await_args_list if call.args[0].type == "error"
    ]
    assert private_url not in json.dumps({"result": model_result, "logs": logs, "streamed_errors": streamed_errors})
    assert result["ok"] is True
    assert getattr(ctx.staged_workflow, field_name) == private_url


@pytest.mark.asyncio
async def test_update_explicit_private_settings_are_absent_from_readback(
    monkeypatch: pytest.MonkeyPatch, no_saved_workflow: None
) -> None:
    private_settings = {
        "extra_http_headers": {"Authorization": "synthetic-http-secret"},
        "cdp_connect_headers": {"Authorization": "synthetic-cdp-secret"},
        "totp_identifier": "synthetic-private-identifier",
        "totp_verification_url": "https://example.test/totp?signature=synthetic-totp-secret",
        "webhook_callback_url": "https://example.test/hook?signature=synthetic-webhook-secret",
        "proxy_location": {"url": "http://synthetic-proxy-secret@proxy.example.test:8080"},
    }
    document = {**yaml.safe_load(_STAGED_WORKFLOW_YAML), **private_settings}
    ctx = CopilotContext(
        organization_id="o",
        workflow_id="w",
        workflow_permanent_id="wp",
        workflow_yaml=_STAGED_WORKFLOW_YAML,
        browser_session_id=None,
        stream=None,
    )
    ctx.google_connection_turn_start_bindings = ()
    monkeypatch.setattr(workflow_update_module, "_get_prior_workflow", AsyncMock(return_value=None))
    params = {"workflow_yaml": yaml.safe_dump(document)}
    result = await _update_workflow(params, ctx, allow_missing_credentials=True)
    assert result["ok"] is True
    workflow_update_module._record_workflow_update_result(ctx, result)
    readback, source = _packet_workflow_readback(ctx)
    assert source == "accepted_write_readback"
    for text in (readback, ctx.workflow_yaml, ctx.staged_workflow_yaml, params["workflow_yaml"]):
        parsed = yaml.safe_load(text)
        assert not (private_settings.keys() - {"extra_http_headers", "cdp_connect_headers"}) & parsed.keys()
        assert parsed["extra_http_headers"] == {"Authorization": "***"}
        assert parsed["cdp_connect_headers"] == {"Authorization": "***"}
        assert "synthetic-" not in text
    for name, value in private_settings.items():
        assert getattr(ctx.staged_workflow, name) == value
        assert ctx.private_workflow_settings[name] == value


@pytest.mark.asyncio
async def test_workflow_write_preserves_label_equal_to_private_value(
    monkeypatch: pytest.MonkeyPatch, no_saved_workflow: None
) -> None:
    private_value = "totp_input"
    private_url = "https://example.test/otp?signature=synthetic-private-signature"
    document = {
        "title": "Private settings",
        "workflow_definition": {
            "parameters": [],
            "blocks": [
                {
                    "block_type": "task",
                    "label": private_value,
                    "navigation_goal": "Continue",
                    "totp_identifier": private_value,
                    "totp_verification_url": private_url,
                }
            ],
        },
    }
    workflow_yaml = yaml.safe_dump(document)
    ctx = CopilotContext(
        organization_id="o",
        workflow_id="w",
        workflow_permanent_id="wp",
        workflow_yaml=workflow_update_module.strip_copilot_yaml_headers(workflow_yaml),
        private_workflow_settings=workflow_yaml_module.private_workflow_settings_from_yaml(workflow_yaml),
        browser_session_id=None,
        stream=None,
    )
    ctx.google_connection_turn_start_bindings = ()
    monkeypatch.setattr(workflow_update_module, "_get_prior_workflow", AsyncMock(return_value=None))
    result = await _update_workflow({"workflow_yaml": workflow_yaml}, ctx, allow_missing_credentials=True)
    assert result["ok"], result
    block = ctx.staged_workflow.workflow_definition.blocks[0]
    assert block.label == private_value
    assert block.totp_identifier == private_value
    assert block.totp_verification_url == private_url
    assert yaml.safe_load(ctx.workflow_yaml)["workflow_definition"]["blocks"][0]["label"] == private_value
