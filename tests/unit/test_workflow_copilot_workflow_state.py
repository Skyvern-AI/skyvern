from datetime import datetime, timezone

import yaml

from skyvern.forge.sdk.routes.workflow_copilot import _ensure_copilot_workflow_yaml, _workflow_to_copilot_yaml
from skyvern.forge.sdk.schemas.workflow_copilot import WorkflowCopilotChatRequest
from skyvern.forge.sdk.workflow.models.block import FileDownloadBlock
from skyvern.forge.sdk.workflow.models.parameter import OutputParameter, WorkflowParameter, WorkflowParameterType
from skyvern.forge.sdk.workflow.models.workflow import Workflow, WorkflowDefinition


def _output_parameter(now: datetime) -> OutputParameter:
    return OutputParameter(
        output_parameter_id="op_block_2",
        workflow_id="w_saved",
        key="block_2_output",
        created_at=now,
        modified_at=now,
    )


def _saved_workflow() -> Workflow:
    now = datetime.now(timezone.utc)
    invoice_date = WorkflowParameter(
        workflow_parameter_id="wp_invoice_date",
        workflow_id="w_saved",
        key="invoice_date",
        workflow_parameter_type=WorkflowParameterType.STRING,
        created_at=now,
        modified_at=now,
    )
    block_output = _output_parameter(now)
    block = FileDownloadBlock(
        label="block_2",
        output_parameter=block_output,
        navigation_goal="Download the invoice for {{ invoice_date }}",
        parameters=[invoice_date],
        error_code_mapping={
            "DATA_UNAVAILABLE": ("only if the account exists but the invoice for {{ invoice_date }} is missing"),
        },
    )

    return Workflow(
        workflow_id="w_saved",
        organization_id="o_test",
        title="Saved workflow",
        workflow_permanent_id="wpid_test",
        version=3,
        is_saved_task=False,
        workflow_definition=WorkflowDefinition(
            parameters=[invoice_date, block_output],
            blocks=[block],
        ),
        created_at=now,
        modified_at=now,
    )


def _chat_request(workflow_yaml: str) -> WorkflowCopilotChatRequest:
    return WorkflowCopilotChatRequest(
        workflow_permanent_id="wpid_test",
        workflow_id="w_client",
        message="why did block_2 not trigger DATA_UNAVAILABLE?",
        workflow_yaml=workflow_yaml,
    )


def test_workflow_to_copilot_yaml_keeps_saved_blocks_without_runtime_fields() -> None:
    persisted_yaml = _workflow_to_copilot_yaml(_saved_workflow())
    parsed = yaml.safe_load(persisted_yaml)

    blocks = parsed["workflow_definition"]["blocks"]
    assert blocks[0]["label"] == "block_2"
    assert blocks[0]["block_type"] == "file_download"
    assert blocks[0]["error_code_mapping"]["DATA_UNAVAILABLE"].startswith("only if the account exists")
    assert blocks[0]["parameter_keys"] == ["invoice_date"]
    assert "output_parameter" not in blocks[0]
    assert "parameters" not in blocks[0]
    assert all(parameter["parameter_type"] != "output" for parameter in parsed["workflow_definition"]["parameters"])


def test_ensure_copilot_workflow_yaml_uses_persisted_workflow_when_request_has_no_blocks() -> None:
    chat_request = _chat_request(
        """
title: Stale client workflow
workflow_definition:
  parameters: []
  blocks: []
"""
    )

    _ensure_copilot_workflow_yaml(chat_request, _saved_workflow())

    parsed = yaml.safe_load(chat_request.workflow_yaml)
    assert parsed["workflow_definition"]["blocks"][0]["label"] == "block_2"
    assert parsed["workflow_definition"]["blocks"][0]["block_type"] == "file_download"


def test_ensure_copilot_workflow_yaml_ignores_persisted_workflow_without_definition() -> None:
    workflow = _saved_workflow()
    workflow.workflow_definition = None
    chat_request = _chat_request(
        """
title: Stale client workflow
workflow_definition:
  parameters: []
  blocks: []
"""
    )

    _ensure_copilot_workflow_yaml(chat_request, workflow)

    parsed = yaml.safe_load(chat_request.workflow_yaml)
    assert parsed["workflow_definition"]["blocks"] == []


def test_ensure_copilot_workflow_yaml_preserves_client_workflow_when_it_has_blocks() -> None:
    client_yaml = """
title: Client workflow
workflow_definition:
  parameters: []
  blocks:
    - block_type: goto_url
      label: client_block
      url: https://example.com
"""
    chat_request = _chat_request(client_yaml)

    _ensure_copilot_workflow_yaml(chat_request, _saved_workflow())

    assert chat_request.workflow_yaml == client_yaml
