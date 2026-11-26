from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from skyvern.forge import app
from skyvern.forge.agent_functions import AgentFunction
from skyvern.forge.sdk.integrations.action_runners import ACTION_RUNNERS, ActionRunContext, ActionRunFailed
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
            error_codes=kwargs.get("error_codes") or [],
            output_parameter=self.output_parameter,
            output_parameter_value=kwargs.get("output_parameter_value"),
            status=kwargs["status"],
        )

    monkeypatch.setattr(Block, "build_block_result", build_block_result)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    (
        "integration_id",
        "action_id",
        "inputs",
        "inner_class",
        "pinned",
        "acts",
        "read_rows",
        "described",
        "nango",
        "runtime_blocked",
    ),
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
            False,
            False,
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
            False,
            False,
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
            True,
            True,
        ),
        ("outlook", "read_inbox", {}, EmailInboxBlock, "email_client", False, False, True, False, False),
        # The account is among the org's Nango connections: it runs as Nango.
        ("gmail", "read_inbox", {}, EmailInboxBlock, "email_client", False, False, True, True, False),
        # The lookup failed: an account of unknown kind runs as neither.
        ("gmail", "read_inbox", {}, EmailInboxBlock, "email_client", False, False, True, None, False),
    ],
    ids=[
        "sheets-read",
        "sheets-write",
        "gmail-send",
        "drive-upload",
        "drive-upload-nango-blocked",
        "outlook-read",
        "gmail-read-nango",
        "gmail-read-lookup-failed",
    ],
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
    nango: bool | None,
    runtime_blocked: bool,
) -> None:
    context = FakeWorkflowRunContext(values={"account": "goac_acct"})

    async def nango_connection_ids(organization_id: str, connection_ids: set[str]) -> set[str]:
        if nango is None:
            raise RuntimeError("lookup down")
        return set(connection_ids) if nango else set()

    monkeypatch.setattr(app.AGENT_FUNCTION, "nango_connection_ids", nango_connection_ids)
    monkeypatch.setattr(Block, "get_workflow_run_context", staticmethod(lambda _: context))
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
    if nango is None:
        assert not ran and result.failure_reason == "Skyvern could not check this block's account. Run it again."
        return
    if runtime_blocked:
        assert (
            not ran
            and result.failure_reason == "Google Drive: this connected account is not available for this action yet."
        )
        original_runner = ACTION_RUNNERS["google_drive_folder"]
        seen_runner: dict[str, Any] = {}

        async def capture_runner(
            organization_id: str,
            connection_id: str,
            action_id: str,
            action_inputs: dict[str, Any],
            run_context: ActionRunContext,
        ) -> dict[str, Any]:
            seen_runner.update(inputs=dict(action_inputs), context=run_context)
            return await original_runner(organization_id, connection_id, action_id, action_inputs, run_context)

        monkeypatch.setitem(ACTION_RUNNERS, "google_drive_folder", capture_runner)

        async def nango_list(**_kwargs: Any) -> dict[str, Any]:
            return {"folder_id": "root", "files": [], "count": 0, "has_more": False}

        async def record_output(**_kwargs: Any) -> None:
            return None

        monkeypatch.setattr(app.AGENT_FUNCTION, "list_google_drive_folder_for_run", nango_list)
        context.register_output_parameter_value_post_execution = record_output  # type: ignore[attr-defined]
        read_block = _saved_block(
            integration_id="google_drive",
            action_id="list_folder",
            connection_id="{{ account }}",
            inputs={"google_drive_folder_id": "root"},
        )
        context._powerbi_hop_refusals[read_block.label] = "copilot_test_run"
        refused = await read_block.execute("wr_test", "wrb_test", organization_id="o_test")
        assert refused.error_codes == ["copilot_test_run_unsupported"]
        assert "save the workflow and run it" in (refused.failure_reason or "")
        context._powerbi_hop_refusals.clear()
        context._powerbi_hop_refusals[read_block.label] = "unsafe_selection_input_source"
        unsafe_selection = await read_block.execute("wr_test", "wrb_test", organization_id="o_test")
        assert not unsafe_selection.success
        assert "without templates" in (unsafe_selection.failure_reason or "")
        context._powerbi_hop_refusals.clear()
        context._powerbi_hop_tokens[read_block.label] = "synthetic-hop"
        read_result = await read_block.execute("wr_test", "wrb_test", organization_id="o_test")
        assert read_result.success and read_result.output_parameter_value == {
            "folder_id": "root",
            "files": [],
            "count": 0,
            "has_more": False,
        }
        assert seen_runner == {
            "inputs": {"google_drive_folder_id": "root"},
            "context": ActionRunContext("wr_test", "wrb_test", "synthetic-hop", True),
        }
        with pytest.raises(ActionRunFailed, match="not available for this action yet"):
            await original_runner(
                "o_test",
                "goac_acct",
                "create_folder",
                {"parent_folder_id": "root", "folder_name": "Reports"},
                ActionRunContext("wr_test", "wrb_test", "synthetic-hop", True),
            )
        return
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
    assert (getattr(inner, "credential_source", None) == "nango") is nango
    # A failure output the wrapped block recorded must not be cleared by the outer block's wrapper.
    assert block._output_recorded_this_execution
    assert connected_account_fields(block.model_dump(), acting_only=True) == (("connection_id",) if acts else ())
    saved = SimpleNamespace(workflow=SimpleNamespace(workflow_definition=SimpleNamespace(blocks=[block])))
    assert block_module._sheets_read_output_keys(saved) == (  # type: ignore[arg-type]
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
            {"integration_id": "google_drive", "action_id": "move_file", "inputs": {"file_id": "private-value"}},
            "Input 'destination_folder_id' is required.",
        ),
        (
            {"integration_id": "google_drive", "action_id": "download_from_drive", "inputs": {"items": []}},
            "Input 'items' does not fit this action",
        ),
        (
            {
                "integration_id": "google_drive",
                "action_id": "move_file",
                "connection_id": None,
                "inputs": {"file_id": "private-value", "destination_folder_id": "private-value"},
            },
            "Google Drive: Move file needs a connected account.",
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
        "drive-move-missing-destination",
        "drive-download-empty-items",
        "drive-move-no-account",
    ],
)
async def test_a_bad_action_or_input_fails_the_block_naming_it(values: dict[str, Any], reason: str) -> None:
    result = await _saved_block(**values).execute("wr_test", "wrb_test", organization_id="o_test")

    assert not result.success
    assert result.failure_reason is not None and result.failure_reason.startswith(reason)
    assert "private-value" not in result.failure_reason
