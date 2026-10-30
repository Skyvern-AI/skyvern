from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from skyvern.forge.sdk.copilot import agent as agent_module
from skyvern.forge.sdk.copilot.request_policy import RequestPolicy
from tests.unit.copilot_test_helpers import make_copilot_ctx


def test_inline_replace_workflow_reports_dropped_note_labels_without_text_when_doc_editor_is_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _process(**kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(name="replacement", extra_http_headers=None, cdp_connect_headers=None)

    monkeypatch.setattr("skyvern.forge.sdk.copilot.tools._process_workflow_yaml", _process)
    prior_yaml = """
workflow_definition:
  parameters: []
  blocks:
    - block_type: navigation
      label: open_page
      url: https://example.com
      navigation_goal: Go
      notes: Never click Delete.
"""
    ctx = make_copilot_ctx(
        last_workflow_yaml=prior_yaml,
        request_policy=RequestPolicy(allow_update_workflow=True, allow_run_blocks=True),
        stream=None,
    )
    result = SimpleNamespace(
        final_output=json.dumps(
            {
                "type": "REPLACE_WORKFLOW",
                "user_response": "Here is the updated workflow.",
                "workflow_yaml": "workflow_definition:\n  parameters: []\n  blocks: []\n",
            }
        ),
        new_items=[],
    )
    chat_request = SimpleNamespace(
        workflow_id="wf-1",
        workflow_permanent_id="wfp-1",
        workflow_copilot_chat_id="chat-1",
        workflow_run_id=None,
        workflow_yaml="",
        product_action=None,
    )

    agent_result = asyncio.run(
        agent_module._translate_to_agent_result(
            result,
            ctx,
            global_llm_context=None,
            chat_request=chat_request,
            organization_id="org-1",
        )
    )

    assert "open_page" in agent_result.user_response
    assert "Never click Delete" not in agent_result.user_response
