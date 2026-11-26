"""Catalog actions that run as our own code, not a workflow block (``runs_via: runner:<name>``). Only the
integration_action block calls a runner; no workflow definition can name one."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

import structlog

from skyvern.constants import EMBER_TEST_RUN_UNSUPPORTED_CODE
from skyvern.forge import app
from skyvern.forge.sdk.integrations.catalog import action_credential_backends, runner_action
from skyvern.forge.sdk.integrations.connected_accounts import UseHop, resolve_account_token
from skyvern.forge.sdk.integrations.google.drive_download import run_drive_download
from skyvern.forge.sdk.schemas.google_drive import (
    GOOGLE_DRIVE_EMBER_TEST_RUN_REASON,
    GOOGLE_DRIVE_LITERAL_SELECTION_REASON,
    GoogleDriveHopRefusal,
)
from skyvern.forge.sdk.services import google_drive_service, google_oauth_service

LOG = structlog.get_logger()

_DRIVE_REQUIRED_INPUTS: dict[str, tuple[str, ...]] = {
    "list_folder": ("google_drive_folder_id",),
    "create_folder": ("parent_folder_id", "folder_name"),
    "move_file": ("file_id", "destination_folder_id"),
}


class ActionRunFailed(Exception):
    """A refusal or failure whose message is already written for the person running the block."""

    def __init__(self, reason: str, output: dict[str, Any] | None = None, error_code: str | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.output = output
        self.error_code = error_code


@dataclass(frozen=True)
class ActionRunContext:
    workflow_run_id: str
    workflow_run_block_id: str
    hop_token: str | None
    broker_connection: bool
    hop_refusal: str | None = None


ActionRunner = Callable[[str, str, str, Mapping[str, Any], ActionRunContext], Awaitable[dict[str, Any]]]


def _text(inputs: Mapping[str, Any], name: str) -> str:
    value = inputs.get(name)
    return value.strip() if isinstance(value, str) else ""


def google_drive_hop_refusal(context: ActionRunContext) -> ActionRunFailed | None:
    if context.broker_connection and not context.hop_token:
        if context.hop_refusal == GoogleDriveHopRefusal.ember_test_run:
            return ActionRunFailed(
                GOOGLE_DRIVE_EMBER_TEST_RUN_REASON,
                error_code=EMBER_TEST_RUN_UNSUPPORTED_CODE,
            )
        if context.hop_refusal == GoogleDriveHopRefusal.unsafe_selection_input_source:
            return ActionRunFailed(GOOGLE_DRIVE_LITERAL_SELECTION_REASON)
    return None


async def run_google_drive_folder_action(
    organization_id: str,
    connection_id: str,
    action_id: str,
    inputs: Mapping[str, Any],
    context: ActionRunContext | None = None,
) -> dict[str, Any]:
    """List a folder, create a subfolder or move an item through one connected Google account of the organization."""
    if missing := [name for name in _DRIVE_REQUIRED_INPUTS[action_id] if not _text(inputs, name)]:
        raise ActionRunFailed(f"Google Drive: {missing[0]!r} is required.")
    # Minted for this organization's own connection, so both ends of a move go through one account of the
    # organization running the block.
    context = context or ActionRunContext("", "", None, False)
    if refusal := google_drive_hop_refusal(context):
        raise refusal
    found = runner_action("google_drive", action_id)
    if context.broker_connection and (found is None or "nango" not in action_credential_backends(*found)):
        raise ActionRunFailed("Google Drive: this connected account is not available for this action yet.")
    access = (
        UseHop()
        if context.broker_connection
        else await resolve_account_token(
            organization_id,
            connection_id,
            list(google_oauth_service.GOOGLE_DRIVE_STEP_SCOPES),
            api="google",
        )
    )
    if not access:
        raise ActionRunFailed(await google_oauth_service.describe_credential_failure(organization_id, connection_id))
    access_token = access if isinstance(access, str) else ""
    try:
        if action_id == "list_folder":
            if isinstance(access, UseHop):
                output = await app.AGENT_FUNCTION.list_google_drive_folder_for_run(
                    organization_id=organization_id,
                    workflow_run_id=context.workflow_run_id,
                    workflow_run_block_id=context.workflow_run_block_id,
                    connection_id=connection_id,
                    hop_token=context.hop_token,
                    folder=_text(inputs, "google_drive_folder_id"),
                    max_results=int(inputs.get("max_results") or 100),
                )
            else:
                output = await google_drive_service.list_folder(
                    access_token=access,
                    folder=_text(inputs, "google_drive_folder_id"),
                    max_results=int(inputs.get("max_results") or 100),
                )
        elif action_id == "create_folder":
            output = await google_drive_service.create_folder(
                access_token=access_token,
                parent_folder=_text(inputs, "parent_folder_id"),
                name=_text(inputs, "folder_name"),
            )
        else:
            output = await google_drive_service.move_file(
                access_token=access_token,
                item=_text(inputs, "file_id"),
                destination_folder=_text(inputs, "destination_folder_id"),
            )
    except ValueError as e:
        raise ActionRunFailed(f"Google Drive: {e}.", {"error": "invalid_input"}) from None
    except google_drive_service.GoogleDriveAPIError as e:
        # Codes and counts only: a Drive item's name is the customer's data.
        LOG.info(
            "Google Drive folder action failed",
            operation=action_id,
            organization_id=organization_id,
            status_code=e.status,
            reason=e.code,
        )
        raise ActionRunFailed(e.message, {"error": e.code or "drive_error", "status_code": e.status}) from None
    LOG.info(
        "Google Drive folder action finished",
        operation=action_id,
        organization_id=organization_id,
        item_count=output.get("count"),
    )
    return output


ACTION_RUNNERS: dict[str, ActionRunner] = {
    "google_drive_folder": run_google_drive_folder_action,
    "google_drive_download": run_drive_download,
}
