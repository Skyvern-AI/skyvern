from __future__ import annotations

from types import SimpleNamespace

import pytest

from skyvern.forge.sdk.copilot.config import BlockAuthoringPolicy
from skyvern.forge.sdk.copilot.context import CopilotContext
from skyvern.forge.sdk.copilot.request_policy import RequestPolicy
from skyvern.forge.sdk.copilot.tools import workflow_update as workflow_update_module
from skyvern.forge.sdk.copilot.tools.workflow_update import _update_workflow
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
