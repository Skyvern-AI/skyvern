from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from skyvern.forge.sdk.copilot.config import BlockAuthoringPolicy
from skyvern.forge.sdk.copilot.context import CopilotContext
from skyvern.forge.sdk.copilot.request_policy import RequestPolicy
from skyvern.forge.sdk.copilot.tools import _mark_credential_deferred_draft
from skyvern.forge.sdk.copilot.tools import workflow_update as workflow_update_module
from skyvern.forge.sdk.copilot.tools.workflow_update import (
    _PERSISTENCE_MESSAGES,
    _update_workflow,
    carry_author_time_findings,
)
from skyvern.forge.sdk.copilot.workflow_yaml import apply_block_edit


@pytest.mark.asyncio
async def test_scoped_code_edit_crosses_normal_persistence_without_rewriting_other_bytes(
    monkeypatch: pytest.MonkeyPatch,
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

    async def process(**kwargs: object) -> SimpleNamespace:
        persisted.append(str(kwargs["workflow_yaml"]))
        return SimpleNamespace(
            workflow_definition=SimpleNamespace(
                blocks=[SimpleNamespace(label="open_statement"), SimpleNamespace(label="read_total")]
            ),
            proxy_location=None,
            webhook_callback_url=None,
        )

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

    async def process(**kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(
            workflow_definition=SimpleNamespace(blocks=[SimpleNamespace(label="read_total")]),
            proxy_location=None,
            webhook_callback_url=None,
        )

    async def prior(_ctx: CopilotContext) -> None:
        return None

    monkeypatch.setattr(workflow_update_module, "_process_workflow_yaml", process)
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
