from __future__ import annotations

import itertools
import json
from pathlib import Path
from typing import Literal, NotRequired

from pydantic import BaseModel, Field
from typing_extensions import TypedDict

from skyvern.config import settings
from skyvern.forge.sdk.schemas.copilot_turn_outcome import ConnectedAccountChoice
from skyvern.forge.sdk.schemas.google_oauth import STATE_ACTIVE, GoogleOAuthCredentialBase
from skyvern.forge.sdk.services.google_oauth_service import GOOGLE_SHEETS_DATA_SCOPE
from skyvern.forge.sdk.workflow.models.block import BlockTypeVar, ForLoopBlock, WhileLoopBlock
from skyvern.forge.sdk.workflow.models.google_sheets_blocks import GoogleSheetsReadBlock, GoogleSheetsWriteBlock
from skyvern.forge.sdk.workflow.models.workflow import Workflow


class GoogleConnectionNoticePayload(TypedDict):
    provider: Literal["google"]
    connectionId: str | None
    displayName: str | None
    condition: Literal["missing", "unusable", "unbound"]
    choices: NotRequired[list[dict[str, str | None]]]


class GoogleConnectionNotice(BaseModel):
    provider: Literal["google"] = "google"
    connectionId: str | None
    displayName: str | None = None
    condition: Literal["missing", "unusable", "unbound"]
    choices: list[ConnectedAccountChoice] = Field(default_factory=list)

    def to_payload(self) -> GoogleConnectionNoticePayload:
        payload: GoogleConnectionNoticePayload = {
            "provider": self.provider,
            "connectionId": self.connectionId,
            "displayName": self.displayName,
            "condition": self.condition,
        }
        if self.condition == "unbound":
            payload["choices"] = [choice.model_dump(mode="json") for choice in self.choices]
        return payload


GoogleSheetConnectionBinding = tuple[str, str | None]

_CAPTURE_SEQ = itertools.count(1)


def google_sheet_connection_bindings(workflow: Workflow | None) -> tuple[GoogleSheetConnectionBinding, ...]:
    if workflow is None:
        return ()
    result: list[GoogleSheetConnectionBinding] = []

    def collect(blocks: list[BlockTypeVar]) -> None:
        for block in blocks:
            if isinstance(block, (GoogleSheetsReadBlock, GoogleSheetsWriteBlock)):
                connection_id = block.credential_id
                if not connection_id or connection_id.startswith("goac_"):
                    result.append((block.label, connection_id or None))
            elif isinstance(block, (ForLoopBlock, WhileLoopBlock)):
                collect(block.loop_blocks)

    collect(workflow.workflow_definition.blocks)
    return tuple(result)


def google_sheet_connection_ids(workflow: Workflow | None) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(connection_id for _, connection_id in google_sheet_connection_bindings(workflow) if connection_id)
    )


def collect_google_connection_notices(
    *,
    turn_start_bindings: tuple[GoogleSheetConnectionBinding, ...],
    current_bindings: tuple[GoogleSheetConnectionBinding, ...],
    visible_credentials: list[GoogleOAuthCredentialBase],
) -> list[GoogleConnectionNotice]:
    prior = set(turn_start_bindings)
    visible_by_id = {credential.id: credential for credential in visible_credentials}
    notices: list[GoogleConnectionNotice] = []
    noticed_ids: set[str | None] = set()
    for binding in current_bindings:
        if binding in prior:
            continue
        _, connection_id = binding
        if connection_id in noticed_ids:
            continue
        noticed_ids.add(connection_id)
        if connection_id is None:
            notices.append(
                GoogleConnectionNotice(
                    connectionId=None,
                    condition="unbound",
                    choices=[
                        ConnectedAccountChoice(
                            connection_id=credential.id,
                            name=credential.credential_name,
                            state=credential.state,
                            email_address=credential.email_address,
                        )
                        for credential in visible_credentials
                        if credential.state == STATE_ACTIVE and GOOGLE_SHEETS_DATA_SCOPE in credential.scopes_granted
                    ],
                )
            )
            continue
        credential = visible_by_id.get(connection_id)
        if credential is None:
            notices.append(GoogleConnectionNotice(connectionId=connection_id, condition="missing"))
        elif credential.state != STATE_ACTIVE or GOOGLE_SHEETS_DATA_SCOPE not in credential.scopes_granted:
            notices.append(
                GoogleConnectionNotice(
                    connectionId=connection_id,
                    displayName=credential.credential_name,
                    condition="unusable",
                )
            )
    return notices


def retain_notices_after_lookup_failure(
    *,
    current_connection_ids: tuple[str | None, ...],
    notices: list[GoogleConnectionNotice],
) -> list[GoogleConnectionNotice]:
    current_ids = set(current_connection_ids)
    return [notice for notice in notices if notice.connectionId in current_ids]


def write_google_connection_notice_capture(
    *,
    output_root: str,
    turn_id: str,
    turn_start_workflow: Workflow | None,
    final_workflow: Workflow,
    accepted_workflow_yaml: str,
    visible_credentials: list[GoogleOAuthCredentialBase],
    observed_notices: list[GoogleConnectionNotice],
) -> None:
    """The capture carries the org's visible connection names, so it stays behind a local ENV."""
    if settings.ENV != "local":
        return
    root = Path(output_root).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    payload = {
        "contractVersion": 2,
        "turnId": turn_id,
        "turnStartWorkflow": turn_start_workflow.model_dump(mode="json") if turn_start_workflow else None,
        "finalWorkflow": final_workflow.model_dump(mode="json"),
        "acceptedWorkflowYaml": accepted_workflow_yaml,
        "visibleCredentials": [
            {
                "id": credential.id,
                "organization_id": credential.organization_id,
                "credential_name": credential.credential_name,
                "provider": credential.provider,
                "state": credential.state,
                "scopes_requested": list(credential.scopes_requested),
                "scopes_granted": list(credential.scopes_granted),
                "created_at": credential.created_at.isoformat(),
                "modified_at": credential.modified_at.isoformat(),
            }
            for credential in visible_credentials
        ],
        "observedNotices": [notice.to_payload() for notice in observed_notices],
    }
    # The counter restarts with the process, so a later run would otherwise overwrite the
    # captures of an earlier one in the same directory.
    target = root / f"capture-{next(_CAPTURE_SEQ):04d}.json"
    while target.exists():
        target = root / f"capture-{next(_CAPTURE_SEQ):04d}.json"
    with target.open("x", encoding="utf-8") as capture_file:
        json.dump(payload, capture_file, indent=2)
        capture_file.write("\n")
    target.chmod(0o600)
