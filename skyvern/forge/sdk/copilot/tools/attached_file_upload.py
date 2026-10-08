from __future__ import annotations

import hashlib
import mimetypes
from typing import Any

from playwright.async_api import FilePayload, Page

from skyvern.forge import app
from skyvern.forge.sdk.api.files import resolve_uploaded_file_id
from skyvern.forge.sdk.copilot.context import CopilotContext
from skyvern.forge.sdk.copilot.runtime import AgentContext, CopilotBrowserSessionUnavailable
from skyvern.forge.sdk.copilot.secret_redaction import redact_secretlike_filename
from skyvern.forge.sdk.schemas.workflow_copilot import CopilotAttachedFile

from ._shared import browser_is_lost, on_working_page

UPLOAD_TOOL_NAME = "upload_attached_file"

_SET_FILES_TIMEOUT_MS = 15000
_NO_BROWSER = "No browser is open in this chat yet. Navigate to the page with the file input first."
_NO_PAGE = "This chat's browser has no page open. Navigate to the page with the file input first."
# set_input_files retargets a <label>, or an element inside one, to the label's input; read the files from there.
_READ_FILES = """el => {
  const input = el instanceof HTMLInputElement ? el : (el.closest("label")?.control ?? el);
  return {
    origin: input.ownerDocument.location.origin,
    file_input: input instanceof HTMLInputElement && input.type === "file",
    files: Array.from(input.files || [], file => ({name: file.name, size: file.size})),
  };
}"""
_RETRY_NOTE = "Check the page before retrying: a retry can upload it twice."
_DELIVERED_NOTE = (
    "The file was set on the input and its change event fired, so the page may already have taken or sent it "
    f"(some pages clear the input after reading it). {_RETRY_NOTE}"
)
_NOT_A_FILE_INPUT = "The selector does not name a file input, so nothing was set."


def _unavailable(file_id: str) -> dict[str, Any]:
    return {
        "ok": False,
        "error": (
            f"Attached file {file_id!r} is not available to this chat. Nothing was uploaded; ask the user to "
            "attach the file again."
        ),
    }


def _attached_in_this_turn(ctx: AgentContext, file_id: str) -> CopilotAttachedFile | None:
    if not isinstance(ctx, CopilotContext) or ctx.turn_context_packet is None:
        return None
    attached_files = ctx.turn_context_packet.attached_file_context
    if attached_files is None:
        return None
    return next((attached for attached in attached_files.files if attached.file_id == file_id), None)


async def _read_attached_bytes(ctx: AgentContext, file_id: str) -> bytes | None:
    try:
        storage_uri = await resolve_uploaded_file_id(file_id, ctx.organization_id)
        return await app.STORAGE.download_managed_file(storage_uri, ctx.organization_id)
    except (FileNotFoundError, PermissionError):
        return None


async def upload_attached_file(ctx: AgentContext, file_id: str, selector: str) -> dict[str, Any]:
    attached = _attached_in_this_turn(ctx, file_id)
    if attached is None:
        return _unavailable(file_id)
    session_id = ctx.browser_session_id
    if not session_id:
        return {"ok": False, "error": _NO_BROWSER}
    data = await _read_attached_bytes(ctx, file_id)
    if data is None:
        return _unavailable(file_id)
    payload = FilePayload(
        name=attached.filename,
        mimeType=mimetypes.guess_type(attached.filename)[0] or "application/octet-stream",
        buffer=data,
    )
    uploaded: dict[str, Any] = {
        "file_id": file_id,
        "filename": redact_secretlike_filename(attached.filename),
        "size_bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }

    async def _upload(page: Page) -> dict[str, Any]:
        target = page.locator(selector)
        files_set = False
        try:
            await target.set_input_files(payload, timeout=_SET_FILES_TIMEOUT_MS)
            files_set = True
            readback = await target.evaluate(_READ_FILES, timeout=_SET_FILES_TIMEOUT_MS)
        except Exception as exc:
            if browser_is_lost(page):
                raise CopilotBrowserSessionUnavailable(session_id) from exc
            first_line = next(iter(str(exc).strip().splitlines()), "") or type(exc).__name__
            if files_set:
                first_line = f"{first_line}. The file may already be on the page. {_RETRY_NOTE}"
            return {**uploaded, "ok": False, "error": first_line}
        # set_input_files returns without error or events on a non-file input.
        if not readback["file_input"]:
            return {**uploaded, "ok": False, "change_delivered": False, "error": _NOT_A_FILE_INPUT}
        input_files = readback["files"]
        ok = {"name": attached.filename, "size": len(data)} in input_files
        result: dict[str, Any] = {
            **uploaded,
            "ok": ok,
            "change_delivered": True,
            "input_origin": readback["origin"],
            "input_files": [{**item, "name": redact_secretlike_filename(item["name"])} for item in input_files],
        }
        if not ok:
            result["error"] = f"The file input no longer holds this file. {_DELIVERED_NOTE}"
        return result

    return await on_working_page(ctx, tool_name=UPLOAD_TOOL_NAME, no_page_error=_NO_PAGE, act=_upload)
