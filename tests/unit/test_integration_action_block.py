from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from skyvern.forge import app
from skyvern.forge.agent_functions import AgentFunction
from skyvern.forge.sdk.integrations.connected_accounts import connected_account_fields
from skyvern.forge.sdk.workflow.models import block as block_module
from skyvern.forge.sdk.workflow.models.block import Block, FileUploadBlock, SendEmailBlock
from skyvern.forge.sdk.workflow.models.email_inbox_block import EmailInboxBlock
from skyvern.forge.sdk.workflow.models.google_sheets_blocks import GoogleSheetsReadBlock, GoogleSheetsWriteBlock
from skyvern.forge.sdk.workflow.models.integration_action_block import IntegrationActionBlock
from skyvern.forge.sdk.workflow.models.parameter import OutputParameter, ParameterType
from skyvern.forge.sdk.workflow.workflow_definition_converter import block_yaml_to_block
from skyvern.schemas.workflows import BlockResult, BlockStatus, IntegrationActionBlockYAML
from tests.unit.fake_workflow_run_context import FakeWorkflowRunContext
from tests.unit.test_send_email_block import offline_address_validation  # noqa: F401 - fixture

pytestmark = pytest.mark.usefixtures("offline_address_validation")

_NOW = datetime.now(UTC)
_SHEET_URL = "https://docs.google.com/spreadsheets/d/SHEET_ID/edit"


def _saved_block(label: str = "read_orders", **values: Any) -> IntegrationActionBlock:
    output = OutputParameter(
        parameter_type=ParameterType.OUTPUT,
        key=f"{label}_output",
        workflow_id="wf_test",
        output_parameter_id=f"op_{label}",
        created_at=_NOW,
        modified_at=_NOW,
    )
    block_yaml = IntegrationActionBlockYAML.model_validate(
        {
            "block_type": "integration_action",
            "label": label,
            "integration_id": "google_sheets",
            "action_id": "read_rows",
            "connection_id": "goac_sheets",
            "inputs": {"spreadsheet_url": _SHEET_URL, "sheet_name": "{{ tab }}", "has_header_row": False},
            **values,
        }
    )
    block = block_yaml_to_block(block_yaml, {output.key: output})  # type: ignore[arg-type]
    assert isinstance(block, IntegrationActionBlock)
    return block


@pytest.fixture(autouse=True)
def _run_context(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app, "AGENT_FUNCTION", AgentFunction())
    monkeypatch.setattr(Block, "get_workflow_run_context", staticmethod(lambda _: FakeWorkflowRunContext(values={})))

    async def build_block_result(self: Block, **kwargs: Any) -> BlockResult:
        return BlockResult(
            success=kwargs["success"],
            failure_reason=kwargs.get("failure_reason"),
            output_parameter=self.output_parameter,
            output_parameter_value=kwargs.get("output_parameter_value"),
            status=kwargs["status"],
        )

    monkeypatch.setattr(Block, "build_block_result", build_block_result)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("integration_id", "action_id", "inputs", "inner_class", "pinned", "acts", "read_rows", "described"),
    [
        (
            "google_sheets",
            "read_rows",
            {"spreadsheet_url": _SHEET_URL, "sheet_name": "{{ tab }}", "has_header_row": False},
            GoogleSheetsReadBlock,
            None,
            False,
            True,
            True,
        ),
        (
            "google_sheets",
            "write_rows",
            {"spreadsheet_url": _SHEET_URL},
            GoogleSheetsWriteBlock,
            None,
            True,
            False,
            True,
        ),
        (
            "gmail",
            "send_email",
            {"recipients": ["a@example.invalid"], "subject": "private", "body": "private"},
            SendEmailBlock,
            "transport",
            True,
            False,
            False,
        ),
        (
            "google_drive",
            "upload_files",
            {"google_drive_folder_id": "folder"},
            FileUploadBlock,
            "storage_type",
            True,
            False,
            True,
        ),
        ("outlook", "read_inbox", {}, EmailInboxBlock, "email_client", False, False, True),
    ],
    ids=["sheets-read", "sheets-write", "gmail-send", "drive-upload", "outlook-read"],
)
async def test_an_action_runs_the_block_it_wraps_as_the_connected_account(
    monkeypatch: pytest.MonkeyPatch,
    integration_id: str,
    action_id: str,
    inputs: dict[str, Any],
    inner_class: type[Block],
    pinned: str | None,
    acts: bool,
    read_rows: bool,
    described: bool,
) -> None:
    monkeypatch.setattr(
        Block,
        "get_workflow_run_context",
        staticmethod(lambda _: FakeWorkflowRunContext(values={"account": "goac_acct"})),
    )
    ran: list[Block] = []

    async def fail_with_recorded_output(self: Block, *args: Any, **kwargs: Any) -> BlockResult:
        ran.append(self)
        self._output_recorded_this_execution = True
        return await self.build_block_result(success=False, status=BlockStatus.failed)

    monkeypatch.setattr(inner_class, "execute", fail_with_recorded_output)
    prompts: list[str] = []

    def load_prompt(template: str, **kwargs: Any) -> str:
        prompts.append(template)
        raise RuntimeError("no description in tests")

    monkeypatch.setattr(block_module.prompt_engine, "load_prompt", load_prompt)
    block = _saved_block(
        integration_id=integration_id,
        action_id=action_id,
        connection_id="{{ account }}",
        inputs=inputs,
        ignore_workflow_system_prompt=True,
    )

    result = await block.execute("wr_test", "wrb_test", organization_id="o_test")
    await block._generate_workflow_run_block_description("wrb_test", "o_test")

    [inner] = ran
    assert result.status == BlockStatus.failed and result.output_parameter == block.output_parameter
    assert {name: getattr(inner, name) for name in inputs} == inputs
    account_fields = connected_account_fields(inner.model_dump())
    assert (inner.label, inner.output_parameter, inner.ignore_workflow_system_prompt) == (
        block.label,
        block.output_parameter,
        True,
    )
    # Runs as the rendered account, which the wrapped block must not render a second time.
    assert {getattr(inner, name) for name in account_fields} == {"goac_acct"}
    assert inner.render_templatable_field(account_fields[0], "{{ other }}", FakeWorkflowRunContext(values={})) == (
        "{{ other }}"
    )
    assert block.connection_id == "{{ account }}"
    if pinned:
        assert getattr(inner, pinned) == integration_id
    # A failure output the wrapped block recorded must not be cleared by the outer block's wrapper.
    assert block._output_recorded_this_execution
    assert connected_account_fields(block.model_dump(), acting_only=True) == (("connection_id",) if acts else ())
    context = SimpleNamespace(workflow=SimpleNamespace(workflow_definition=SimpleNamespace(blocks=[block])))
    assert block_module._sheets_read_output_keys(context) == (  # type: ignore[arg-type]
        {block.output_parameter.key} if read_rows else set()
    )
    # Gmail message contents never reach the description prompt.
    assert bool(prompts) is described


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("values", "reason"),
    [
        ({"action_id": "nope"}, "Unknown integration action google_sheets.nope."),
        ({"inputs": {"spreadsheet_url": _SHEET_URL, "has_header_row": "private-value"}}, "Input 'has_header_row'"),
        ({"inputs": {"spreadsheet_url": _SHEET_URL, "tab": "Orders"}}, "Unknown input 'tab'"),
        ({"connection_id": None}, "Google Sheets: Read rows needs a connected account."),
        ({"inputs": {"sheet_name": "Orders"}}, "Input 'spreadsheet_url' is required."),
        ({"inputs": {"spreadsheet_url": _SHEET_URL, "has_header_row": "{{ flag }}"}}, "Input 'has_header_row' is not"),
        (
            {"integration_id": "gmail", "action_id": "read_inbox", "inputs": {"email_client": "{{ client }}"}},
            "Input 'email_client'",
        ),
        (
            {"integration_id": "outlook", "action_id": "read_inbox", "inputs": {"email_client": "gmail"}},
            "Input 'email_client'",
        ),
        (
            {"integration_id": "powerbi", "action_id": "query", "inputs": {"dataset_id": "d", "query": "EVALUATE t"}},
            "Power BI: Run DAX query is not supported yet.",
        ),
        (
            {
                "integration_id": "powerbi",
                "action_id": "query",
                "connection_id": None,
                "inputs": {"dataset_id": "d", "query": "EVALUATE t"},
            },
            "Power BI: Run DAX query is not supported yet.",
        ),
        (
            {
                "integration_id": "gmail",
                "action_id": "send_email",
                "connection_id": "ngc_acct",
                "inputs": {"recipients": ["a@example.invalid"], "subject": "private-value", "body": "private-value"},
            },
            "Gmail: Send email with this account is not supported yet.",
        ),
    ],
    ids=[
        "unknown-action",
        "bad-input",
        "unknown-input",
        "no-account",
        "missing-input",
        "templated-bool",
        "templated-pin",
        "literal-pin",
        "nango-account",
        "nango-no-account",
        "nango-gmail-account",
    ],
)
async def test_a_bad_action_or_input_fails_the_block_naming_it(values: dict[str, Any], reason: str) -> None:
    result = await _saved_block(**values).execute("wr_test", "wrb_test", organization_id="o_test")

    assert not result.success
    assert result.failure_reason is not None and result.failure_reason.startswith(reason)
    assert "private-value" not in result.failure_reason
