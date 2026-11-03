from __future__ import annotations

import asyncio
import hashlib
import json
import mimetypes
import tempfile
from typing import Any

import structlog
from playwright.async_api import FilePayload, Page

from skyvern.exceptions import PDFParsingError
from skyvern.forge import app
from skyvern.forge.sdk.api.files import resolve_uploaded_file_id
from skyvern.forge.sdk.copilot.context import CopilotContext
from skyvern.forge.sdk.copilot.llm_config import resolve_raw_secret_safety_handler
from skyvern.forge.sdk.copilot.request_policy import screen_raw_secret_safety
from skyvern.forge.sdk.copilot.runtime import AgentContext, CopilotBrowserSessionUnavailable
from skyvern.forge.sdk.copilot.secret_redaction import redact_raw_secrets_for_prompt, redact_secretlike_filename
from skyvern.forge.sdk.copilot.secret_scrub import scrub_secrets_from_structure
from skyvern.forge.sdk.schemas.workflow_copilot import CopilotAttachedFile
from skyvern.forge.sdk.utils.pdf_parser import extract_pdf_file, scrambled_text_layer_reason

from ._shared import browser_is_lost, on_working_page

LOG = structlog.get_logger()

UPLOAD_TOOL_NAME = "upload_attached_file"
READ_PDF_TOOL_NAME = "read_attached_pdf"
# Measured on the serialized text: the recent-tool-output cut (50k) applies to the JSON string, where
# newlines and quotes take two characters.
_READ_WINDOW_SERIALIZED_CHARS = 40_000
_SCREEN_CONTEXT_CHARS = 2_000
# Carries no secret keyword, colon or equals sign, so no redaction pattern can match across it.
_SCREEN_CONTEXT_SEPARATOR = "\n\n----- the text being read starts below this line -----\n\n"

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


def _not_attached(file_id: str) -> dict[str, Any]:
    return {"ok": False, "error": f"{file_id!r} is not an available file attached to this chat."}


def _redacted_pdf_text(ctx: AgentContext, data: bytes, filename: str) -> tuple[str, str | None]:
    with tempfile.NamedTemporaryFile(suffix=".pdf") as handle:
        handle.write(data)
        handle.flush()
        text = extract_pdf_file(handle.name, file_identifier=redact_secretlike_filename(filename))
    # Redacted before windowing: a value cut across two windows would match neither pattern nor scrub.
    return redact_raw_secrets_for_prompt(scrub_secrets_from_structure(ctx, text)), scrambled_text_layer_reason(text)


def _window_end(text: str, start: int) -> int:
    window = text[start : start + _READ_WINDOW_SERIALIZED_CHARS]
    while (size := len(json.dumps(window, ensure_ascii=False))) > _READ_WINDOW_SERIALIZED_CHARS:
        window = window[: len(window) * _READ_WINDOW_SERIALIZED_CHARS // size]
    end = start + len(window)
    if end < len(text):
        cut = text.rfind("\n", start, end)
        if cut <= start:
            cut = text.rfind(" ", start, end)
        if cut > start:
            end = cut + 1
    return end


def _window_bounds(text: str, offset: int) -> tuple[int, int]:
    if offset >= len(text):
        return len(text), len(text)
    # Windows start only where an earlier one ended (a line break where possible), so a model-chosen offset
    # cannot cut a secret in half or away from a label on its line.
    start, end = 0, _window_end(text, 0)
    while end <= offset:
        start, end = end, _window_end(text, end)
    return start, end


async def read_attached_pdf(ctx: AgentContext, file_id: str, offset: int = 0) -> dict[str, Any]:
    attached = _attached_in_this_turn(ctx, file_id)
    if attached is None or not attached.available:
        return _not_attached(file_id)
    data = await _read_attached_bytes(ctx, file_id)
    if data is None:
        return _not_attached(file_id)
    read: dict[str, Any] = {"file_id": file_id, "filename": redact_secretlike_filename(attached.filename)}
    try:
        text, scrambled_reason = await asyncio.to_thread(_redacted_pdf_text, ctx, data, attached.filename)
    except PDFParsingError:
        return {**read, "ok": False, "error": "The file could not be read as a PDF."}
    if not text.strip():
        return {
            **read,
            "ok": False,
            "error": "The PDF has no text layer (it may be scanned images), so no text could be extracted.",
        }

    start, end = _window_bounds(text, offset)
    read.update(offset=start, next_offset=end if end < len(text) else None, total_chars=len(text))
    if scrambled_reason:
        read["text_layer_warning"] = scrambled_reason
    if start == end:
        return {**read, "ok": True, "text": ""}

    handler = await resolve_raw_secret_safety_handler(ctx.workflow_permanent_id, ctx.organization_id)
    # The preceding text lets the screen recognise a value whose label ended the previous window; only the text
    # after the separator is returned.
    context = text[max(0, start - _SCREEN_CONTEXT_CHARS) : start]
    screen = await screen_raw_secret_safety(
        context + _SCREEN_CONTEXT_SEPARATOR + text[start:end], handler, organization_id=ctx.organization_id
    )
    screened = screen.canonical_user_message
    LOG.info(
        "copilot.attached_pdf_read",
        file_id=file_id,
        offset=start,
        window_chars=end - start,
        context_chars=len(context),
        total_chars=len(text),
        screen_status=screen.status,
        screen_failure_kind=screen.failure_kind,
        citation_count=screen.citation_count,
        screen_latency_ms=round(screen.latency_ms),
    )
    if screen.status not in ("clean", "detected") or screened.count(_SCREEN_CONTEXT_SEPARATOR) != 1:
        return {
            **read,
            "ok": False,
            "error": "This part of the PDF could not be checked for passwords and other secrets, so its text was withheld.",
        }
    return {**read, "ok": True, "text": screened.partition(_SCREEN_CONTEXT_SEPARATOR)[2]}
