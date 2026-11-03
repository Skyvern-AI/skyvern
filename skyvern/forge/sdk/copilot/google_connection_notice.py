from __future__ import annotations

import itertools
import json
from pathlib import Path
from typing import Literal, NotRequired

from pydantic import BaseModel, Field
from typing_extensions import TypedDict

from skyvern.config import settings
from skyvern.forge.sdk.schemas.copilot_turn_outcome import (
    ConnectedAccountChoice,
    GoogleSetupOffer,
    GoogleSetupScopeProfile,
)
from skyvern.forge.sdk.schemas.google_oauth import STATE_ACTIVE, GoogleOAuthCredentialBase
from skyvern.forge.sdk.services.google_oauth_service import (
    GOOGLE_GMAIL_READONLY_SCOPE,
    GOOGLE_GMAIL_SEND_SCOPES,
    GOOGLE_OAUTH_SCOPE_PROFILES,
    GOOGLE_SHEETS_DATA_SCOPE,
)
from skyvern.forge.sdk.workflow.models.block import BlockTypeVar, ForLoopBlock, WhileLoopBlock
from skyvern.forge.sdk.workflow.models.google_sheets_blocks import GoogleSheetsReadBlock, GoogleSheetsWriteBlock
from skyvern.forge.sdk.workflow.models.workflow import Workflow

GoogleConnectionNoticeOrigin = Literal["setup", "workflow"]

GOOGLE_SETUP_PROFILE_DATA_SCOPES: dict[GoogleSetupScopeProfile, str] = {
    "gmail": GOOGLE_GMAIL_READONLY_SCOPE,
    "google_sheets": GOOGLE_SHEETS_DATA_SCOPE,
}


class GoogleConnectionNoticePayload(TypedDict):
    provider: Literal["google"]
    connectionId: str | None
    displayName: str | None
    condition: Literal["missing", "unusable", "unbound"]
    scopeProfile: GoogleSetupScopeProfile
    origin: GoogleConnectionNoticeOrigin
    choices: NotRequired[list[dict[str, str | None]]]


class GoogleConnectionNotice(BaseModel):
    provider: Literal["google"] = "google"
    connectionId: str | None
    displayName: str | None = None
    condition: Literal["missing", "unusable", "unbound"]
    scopeProfile: GoogleSetupScopeProfile = "google_sheets"
    origin: GoogleConnectionNoticeOrigin = "workflow"
    choices: list[ConnectedAccountChoice] = Field(default_factory=list)

    def to_payload(self) -> GoogleConnectionNoticePayload:
        payload: GoogleConnectionNoticePayload = {
            "provider": self.provider,
            "connectionId": self.connectionId,
            "displayName": self.displayName,
            "condition": self.condition,
            "scopeProfile": self.scopeProfile,
            "origin": self.origin,
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
                        _choice(credential)
                        for credential in google_setup_ready_connections("google_sheets", visible_credentials)
                    ],
                )
            )
            continue
        credential = visible_by_id.get(connection_id)
        if credential is None:
            notices.append(GoogleConnectionNotice(connectionId=connection_id, condition="missing"))
        elif credential.state != STATE_ACTIVE or GOOGLE_SHEETS_DATA_SCOPE not in credential.scopes_granted:
            if _holds_other_profile_grant(credential, "google_sheets"):
                notices.append(
                    GoogleConnectionNotice(
                        connectionId=None,
                        condition="unbound",
                        choices=[
                            _choice(ready)
                            for ready in google_setup_ready_connections("google_sheets", visible_credentials)
                        ],
                    )
                )
            else:
                notices.append(
                    GoogleConnectionNotice(
                        connectionId=connection_id,
                        displayName=credential.credential_name,
                        condition="unusable",
                    )
                )
    return notices


def _holds_other_profile_grant(credential: GoogleOAuthCredentialBase, scope_profile: GoogleSetupScopeProfile) -> bool:
    """Reauthorizing a row replaces its granted scopes with this profile's, keeping only Gmail send, so any other
    profile scope it holds would be revoked."""
    kept = {*GOOGLE_OAUTH_SCOPE_PROFILES[scope_profile], *GOOGLE_GMAIL_SEND_SCOPES}
    return any(
        scope in credential.scopes_granted
        for scopes in GOOGLE_OAUTH_SCOPE_PROFILES.values()
        for scope in scopes
        if scope not in kept
    )


def _choice(credential: GoogleOAuthCredentialBase) -> ConnectedAccountChoice:
    return ConnectedAccountChoice(
        connection_id=credential.id,
        name=credential.credential_name,
        state=credential.state,
        email_address=credential.email_address,
    )


def google_setup_ready_connections(
    scope_profile: GoogleSetupScopeProfile, visible_credentials: list[GoogleOAuthCredentialBase]
) -> list[GoogleOAuthCredentialBase]:
    scope = GOOGLE_SETUP_PROFILE_DATA_SCOPES[scope_profile]
    return [
        credential
        for credential in visible_credentials
        if credential.state == STATE_ACTIVE and scope in credential.scopes_granted
    ]


def google_setup_notice(
    *,
    scope_profile: GoogleSetupScopeProfile,
    connection_id: str | None,
    visible_credentials: list[GoogleOAuthCredentialBase],
) -> GoogleConnectionNotice:
    """Reconnect only a row that was asked for this profile, since reauthorizing a row replaces its scopes."""
    scope = GOOGLE_SETUP_PROFILE_DATA_SCOPES[scope_profile]
    ready = google_setup_ready_connections(scope_profile, visible_credentials)
    target = next((credential for credential in visible_credentials if credential.id == connection_id), None)
    if (
        target is not None
        and all(credential.id != target.id for credential in ready)
        and scope in {*target.scopes_granted, *target.scopes_requested}
        and not _holds_other_profile_grant(target, scope_profile)
    ):
        return GoogleConnectionNotice(
            connectionId=target.id,
            displayName=target.credential_name,
            condition="unusable",
            scopeProfile=scope_profile,
            origin="setup",
        )
    return GoogleConnectionNotice(
        connectionId=None,
        condition="unbound",
        scopeProfile=scope_profile,
        origin="setup",
        choices=[_choice(credential) for credential in ready],
    )


def unselected_google_setup_offers(
    offers: list[GoogleSetupOffer], selected_scopes: list[str]
) -> list[GoogleSetupOffer]:
    return [offer for offer in offers if GOOGLE_SETUP_PROFILE_DATA_SCOPES[offer.scope_profile] not in selected_scopes]


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
