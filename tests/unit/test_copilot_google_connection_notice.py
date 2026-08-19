import json
from datetime import UTC, datetime
from pathlib import Path

from skyvern.forge.sdk.copilot.google_connection_notice import (
    GoogleConnectionNotice,
    collect_google_connection_notices,
    google_sheet_connection_bindings,
    google_sheet_connection_ids,
    retain_notices_after_lookup_failure,
    write_google_connection_notice_capture,
)
from skyvern.forge.sdk.schemas.google_oauth import GoogleOAuthCredentialBase
from skyvern.forge.sdk.workflow.models.block import ForLoopBlock
from skyvern.forge.sdk.workflow.models.google_sheets_blocks import GoogleSheetsReadBlock, GoogleSheetsWriteBlock
from skyvern.forge.sdk.workflow.models.parameter import OutputParameter
from skyvern.forge.sdk.workflow.models.workflow import Workflow, WorkflowDefinition


def _workflow(*blocks: object) -> Workflow:
    now = datetime.now(UTC)
    return Workflow(
        workflow_id="wf_test",
        organization_id="org_test",
        title="Test",
        workflow_permanent_id="wpid_test",
        version=1,
        is_saved_task=False,
        workflow_definition=WorkflowDefinition(parameters=[], blocks=list(blocks)),
        created_at=now,
        modified_at=now,
    )


def _write(label: str, connection_id: str | None) -> GoogleSheetsWriteBlock:
    return GoogleSheetsWriteBlock(
        label=label,
        spreadsheet_url="https://docs.google.com/spreadsheets/d/test",
        credential_id=connection_id,
        output_parameter=_output(label),
    )


def _output(label: str) -> OutputParameter:
    now = datetime.now(UTC)
    return OutputParameter(
        key=f"{label}_output",
        output_parameter_id=f"op_{label}",
        workflow_id="wf_test",
        created_at=now,
        modified_at=now,
    )


def _credential(
    connection_id: str,
    state: str,
    name: str = "Sheets account",
    scopes_granted: list[str] | None = None,
) -> GoogleOAuthCredentialBase:
    now = datetime.now(UTC)
    return GoogleOAuthCredentialBase(
        id=connection_id,
        organization_id="org_test",
        credential_name=name,
        state=state,
        scopes_granted=(["https://www.googleapis.com/auth/spreadsheets"] if scopes_granted is None else scopes_granted),
        created_at=now,
        modified_at=now,
    )


def test_reports_missing_and_unusable_new_bindings_but_not_active_or_preexisting() -> None:
    workflow = _workflow(
        _write("missing", "goac_missing"),
        _write("unusable", "goac_error"),
        _write("active", "goac_active"),
        _write("existing", "goac_existing"),
    )

    notices = collect_google_connection_notices(
        turn_start_bindings=(("existing", "goac_existing"),),
        current_bindings=google_sheet_connection_bindings(workflow),
        visible_credentials=[
            _credential("goac_error", "error", "Needs reconnect"),
            _credential("goac_active", "active"),
            _credential("goac_existing", "error"),
        ],
    )

    assert [notice.model_dump() for notice in notices] == [
        {
            "provider": "google",
            "connectionId": "goac_missing",
            "displayName": None,
            "condition": "missing",
        },
        {
            "provider": "google",
            "connectionId": "goac_error",
            "displayName": "Needs reconnect",
            "condition": "unusable",
        },
    ]


def test_nested_and_duplicate_bindings_are_flattened_and_deduplicated() -> None:
    nested = GoogleSheetsReadBlock(
        label="nested_read",
        spreadsheet_url="https://docs.google.com/spreadsheets/d/test",
        credential_id="goac_nested",
        output_parameter=_output("nested"),
    )
    loop = ForLoopBlock(
        label="loop",
        loop_over_parameter_key="items",
        loop_blocks=[nested, _write("duplicate", "goac_nested")],
        output_parameter=_output("loop"),
    )

    assert google_sheet_connection_ids(_workflow(loop)) == ("goac_nested",)


def test_new_block_using_a_preexisting_connection_is_still_a_new_binding() -> None:
    notices = collect_google_connection_notices(
        turn_start_bindings=(("existing", "goac_error"),),
        current_bindings=(("existing", "goac_error"), ("new", "goac_error")),
        visible_credentials=[_credential("goac_error", "error", "Needs reconnect")],
    )

    assert [notice.connectionId for notice in notices] == ["goac_error"]


def test_repeated_update_can_clear_notice_and_lookup_failure_only_retains_still_bound_notices() -> None:
    notice = GoogleConnectionNotice(connectionId="goac_error", displayName="Old", condition="unusable")
    active = collect_google_connection_notices(
        turn_start_bindings=(),
        current_bindings=(("write", "goac_error"),),
        visible_credentials=[_credential("goac_error", "active")],
    )
    retained = retain_notices_after_lookup_failure(
        current_connection_ids=("goac_error",),
        notices=[notice],
    )
    removed = retain_notices_after_lookup_failure(current_connection_ids=(), notices=[notice])

    assert active == []
    assert retained == [notice]
    assert removed == []


def test_active_connection_without_sheets_scope_is_unusable() -> None:
    notices = collect_google_connection_notices(
        turn_start_bindings=(),
        current_bindings=(("write", "goac_gmail"),),
        visible_credentials=[
            _credential(
                "goac_gmail",
                "active",
                "Mail only",
                scopes_granted=["https://www.googleapis.com/auth/gmail.readonly"],
            )
        ],
    )

    assert [notice.model_dump() for notice in notices] == [
        {
            "provider": "google",
            "connectionId": "goac_gmail",
            "displayName": "Mail only",
            "condition": "unusable",
        }
    ]


def test_capture_writes_one_token_free_packet_at_the_contract_path(tmp_path: Path) -> None:
    final = _workflow(_write("write", "goac_error"))
    credential = _credential("goac_error", "error", "Needs reconnect", scopes_granted=[])

    write_google_connection_notice_capture(
        output_root=str(tmp_path),
        turn_start_workflow=_workflow(),
        final_workflow=final,
        accepted_workflow_yaml="workflow_definition:\n  blocks: []\n",
        visible_credentials=[credential],
        observed_notices=[],
    )

    payload = json.loads((tmp_path / "capture-0001.json").read_text())
    assert payload["contractVersion"] == 1
    assert payload["visibleCredentials"] == [
        {
            "id": "goac_error",
            "organization_id": "org_test",
            "credential_name": "Needs reconnect",
            "provider": "google",
            "state": "error",
            "scopes_requested": [],
            "scopes_granted": [],
            "created_at": credential.created_at.isoformat(),
            "modified_at": credential.modified_at.isoformat(),
        }
    ]
