from __future__ import annotations

import hashlib
import json
import struct
import zlib
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest
from playwright.async_api import Page, Route, async_playwright
from structlog.testing import capture_logs

from skyvern.forge import app
from skyvern.forge.sdk.copilot.context import CopilotContext
from skyvern.forge.sdk.copilot.runtime import AgentContext
from skyvern.forge.sdk.copilot.tools import _shared, attached_file_upload, read_attached_pdf_tool
from skyvern.forge.sdk.copilot.turn_context import AttachedFileContext, TranscriptContext, TurnContextPacket
from skyvern.forge.sdk.schemas.workflow_copilot import CopilotAttachedFile
from skyvern.services import uploaded_file_service
from skyvern.webeye.browser_errors import BrowserCdpAcquisitionError
from tests.unit.conftest import make_copilot_context
from tests.unit.copilot_test_helpers import skip_no_browser
from tests.unit.test_copilot_runtime import _make_ctx
from tests.unit.test_uploaded_file_retention import (
    ATTACKER_ORG_ID,
    VICTIM_ORG_ID,
    FakeStorage,
    FakeUploadedFilesRepository,
    _uri,
)

CHAT_ORG_ID = ATTACKER_ORG_ID
PAGE_URL = "https://upload.fixture.test/"

UPLOAD_PAGE = """<!doctype html>
<input type="file" id="resume" accept="application/pdf">
<input type="file" id="photo" accept="image/*"><div id="photo-receipt"></div>
<input type="file" id="video" accept="video/*"><div id="video-receipt"></div>
<script>
  for (const id of ["photo", "video"]) {
    document.getElementById(id).addEventListener("change", async (event) => {
      const file = event.target.files[0];
      const digest = await crypto.subtle.digest("SHA-256", await file.arrayBuffer());
      const hex = Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("");
      document.getElementById(`${id}-receipt`).textContent = `${file.name}:${file.size}:${hex}`;
    });
  }
</script>
"""


def _png() -> bytes:
    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))

    header = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(b"\x00\x10\x60\xd0"))
        + chunk(b"IEND", b"")
    )


def _mp4() -> bytes:
    return struct.pack(">I", 24) + b"ftypisom" + struct.pack(">I", 512) + b"isomiso2" + struct.pack(">I", 8) + b"mdat"


def _chat(monkeypatch: pytest.MonkeyPatch, page: Page | None, files: list[CopilotAttachedFile]) -> CopilotContext:
    @asynccontextmanager
    async def _admitted(_ctx: AgentContext) -> AsyncIterator[None]:
        yield

    async def _page(_ctx: AgentContext) -> Page | None:
        return page

    monkeypatch.setattr(_shared, "mcp_browser_context", _admitted)
    monkeypatch.setattr(_shared, "live_working_page", _page)
    ctx = make_copilot_context()
    ctx.organization_id = CHAT_ORG_ID
    ctx.browser_session_id = "pbs_chat"
    ctx.turn_context_packet = TurnContextPacket(
        transcript_context=TranscriptContext(
            earliest_user_turn="",
            latest_prior_user_turn="",
            latest_assistant_turn="",
            retained_history="",
            omitted_any=False,
        ),
        attached_file_context=AttachedFileContext(files=files),
    )
    return ctx


def _attach(
    repo: FakeUploadedFilesRepository,
    storage: FakeStorage,
    filename: str,
    data: bytes,
    *,
    organization_id: str = CHAT_ORG_ID,
    expires_at: datetime | None = None,
) -> CopilotAttachedFile:
    file_id = repo.seed(organization_id, expires_at=expires_at, filename=filename)
    storage.objects[_uri(organization_id, filename)] = data
    return CopilotAttachedFile(file_id=file_id, filename=filename, size_bytes=len(data))


@pytest.fixture
def repo() -> FakeUploadedFilesRepository:
    return FakeUploadedFilesRepository()


@pytest.fixture
def storage(monkeypatch: pytest.MonkeyPatch, repo: FakeUploadedFilesRepository) -> FakeStorage:
    fake = FakeStorage()
    fake_app = SimpleNamespace(DATABASE=SimpleNamespace(uploaded_files=repo), STORAGE=fake)
    monkeypatch.setattr(uploaded_file_service, "app", fake_app)
    monkeypatch.setattr(app, "STORAGE", fake)
    return fake


@asynccontextmanager
async def _fixture_page(html: str = UPLOAD_PAGE) -> AsyncIterator[Page]:
    async def _serve(route: Route) -> None:
        await route.fulfill(content_type="text/html", body=html)

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        try:
            page = await browser.new_page()
            # An https origin is a secure context, which crypto.subtle needs for the receipt digest.
            await page.route(PAGE_URL, _serve)
            await page.goto(PAGE_URL)
            yield page
        finally:
            await browser.close()


async def _receipt(page: Page, receipt_id: str) -> str:
    await page.wait_for_function(f"document.getElementById({receipt_id!r}).textContent !== ''", timeout=5000)
    return await page.locator(f"#{receipt_id}").inner_text()


@skip_no_browser
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "data", "selector", "receipt_id"),
    [
        ("synthetic-photo.png", _png(), "#photo", "photo-receipt"),
        ("synthetic-clip.mp4", _mp4(), "#video", "video-receipt"),
    ],
    ids=["png", "mp4"],
)
async def test_the_attached_bytes_reach_the_page_and_the_result_is_the_pages_own_receipt(
    monkeypatch: pytest.MonkeyPatch,
    repo: FakeUploadedFilesRepository,
    storage: FakeStorage,
    name: str,
    data: bytes,
    selector: str,
    receipt_id: str,
) -> None:
    attached = _attach(repo, storage, name, data)
    async with _fixture_page() as page:
        ctx = _chat(monkeypatch, page, [attached])

        result = await attached_file_upload.upload_attached_file(ctx, attached.file_id, selector)

        receipt = await _receipt(page, receipt_id)

    assert receipt == f"{name}:{len(data)}:{hashlib.sha256(data).hexdigest()}"
    assert result == {
        "ok": True,
        "file_id": attached.file_id,
        "filename": name,
        "size_bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "change_delivered": True,
        "input_origin": PAGE_URL.rstrip("/"),
        "input_files": [{"name": name, "size": len(data)}],
    }


def _refusal_cases(
    repo: FakeUploadedFilesRepository, storage: FakeStorage
) -> dict[str, tuple[str, list[CopilotAttachedFile], bool]]:
    png = _png()
    foreign = _attach(repo, storage, "foreign-photo.png", png, organization_id=VICTIM_ORG_ID)
    expired = _attach(
        repo, storage, "expired-photo.png", png, expires_at=datetime.now(timezone.utc) - timedelta(minutes=1)
    )
    deleted = _attach(repo, storage, "deleted-photo.png", png)
    repo.rows[deleted.file_id].deleted_at = datetime.now(timezone.utc)
    missing = CopilotAttachedFile(file_id="file_999999", filename="missing-photo.png")
    unlisted = _attach(repo, storage, "unlisted-photo.png", png)
    return {
        "cross_org": (foreign.file_id, [foreign], True),
        "expired": (expired.file_id, [expired], True),
        "deleted": (deleted.file_id, [deleted], True),
        "missing": (missing.file_id, [missing], True),
        "not_in_this_turn": (unlisted.file_id, [], True),
        "no_turn_packet": (unlisted.file_id, [unlisted], False),
    }


def _assert_refused_naming_only(result: dict[str, Any], file_id: str) -> None:
    assert set(result) == {"ok", "error"}
    assert result["ok"] is False
    assert file_id in result["error"]
    assert "Nothing was uploaded" in result["error"]


@skip_no_browser
@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["cross_org", "expired", "deleted", "missing", "not_in_this_turn", "no_turn_packet"])
async def test_an_unavailable_reference_uploads_nothing_and_names_only_the_id(
    monkeypatch: pytest.MonkeyPatch,
    repo: FakeUploadedFilesRepository,
    storage: FakeStorage,
    case: str,
) -> None:
    file_id, listed, with_packet = _refusal_cases(repo, storage)[case]
    async with _fixture_page() as page:
        ctx = _chat(monkeypatch, page, listed)
        if not with_packet:
            ctx.turn_context_packet = None

        result = await attached_file_upload.upload_attached_file(ctx, file_id, "#photo")

        held = await page.locator("#photo").evaluate("el => el.files.length")

    _assert_refused_naming_only(result, file_id)
    assert held == 0
    assert storage.downloads == []
    text = json.dumps(result)
    assert "photo.png" not in text and "s3://" not in text and "/" not in result["error"]


@pytest.mark.asyncio
async def test_a_context_that_is_not_a_copilot_chat_is_refused(
    repo: FakeUploadedFilesRepository, storage: FakeStorage
) -> None:
    attached = _attach(repo, storage, "synthetic-photo.png", b"png")
    ctx = _make_ctx()
    ctx.organization_id = CHAT_ORG_ID
    ctx.browser_session_id = "pbs_chat"

    result = await attached_file_upload.upload_attached_file(ctx, attached.file_id, "#photo")

    _assert_refused_naming_only(result, attached.file_id)
    assert storage.downloads == []


@skip_no_browser
@pytest.mark.asyncio
async def test_a_reattached_copy_uploads_after_the_original_was_removed(
    monkeypatch: pytest.MonkeyPatch, repo: FakeUploadedFilesRepository, storage: FakeStorage
) -> None:
    data = _png()
    removed = _attach(repo, storage, "synthetic-photo.png", data)
    repo.rows[removed.file_id].deleted_at = datetime.now(timezone.utc)
    async with _fixture_page() as page:
        ctx = _chat(monkeypatch, page, [removed])
        refused = await attached_file_upload.upload_attached_file(ctx, removed.file_id, "#photo")
        reattached = _attach(repo, storage, "synthetic-photo.png", data)
        assert ctx.turn_context_packet is not None and ctx.turn_context_packet.attached_file_context is not None
        ctx.turn_context_packet.attached_file_context.files.append(reattached)

        result = await attached_file_upload.upload_attached_file(ctx, reattached.file_id, "#photo")

        receipt = await _receipt(page, "photo-receipt")

    assert refused["ok"] is False
    assert result["ok"] is True
    assert receipt == f"synthetic-photo.png:{len(data)}:{hashlib.sha256(data).hexdigest()}"


@skip_no_browser
@pytest.mark.asyncio
async def test_a_secret_shaped_filename_reaches_the_page_but_not_the_result(
    monkeypatch: pytest.MonkeyPatch, repo: FakeUploadedFilesRepository, storage: FakeStorage
) -> None:
    name = "export password=hunter2.csv"
    attached = _attach(repo, storage, name, b"a,b\n1,2\n")
    async with _fixture_page() as page:
        ctx = _chat(monkeypatch, page, [attached])

        result = await attached_file_upload.upload_attached_file(ctx, attached.file_id, "#photo")

        receipt = await _receipt(page, "photo-receipt")

    assert receipt.startswith(f"{name}:")
    assert result["ok"] is True
    assert "hunter2" not in json.dumps(result)


_LABELLED_PICKERS = (
    '<label for="photo">Photo</label><input type="file" id="photo">'
    '<label id="wrapped"><span id="hint">Add image</span><input type="file"></label>'
)


@skip_no_browser
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "selector", ["label[for=photo]", "text=Photo", "#hint"], ids=["label_for", "text", "span_in_label"]
)
async def test_a_selector_naming_the_pickers_label_reports_the_input_playwright_filled(
    monkeypatch: pytest.MonkeyPatch, repo: FakeUploadedFilesRepository, storage: FakeStorage, selector: str
) -> None:
    data = _png()
    attached = _attach(repo, storage, "synthetic-photo.png", data)
    async with _fixture_page(_LABELLED_PICKERS) as page:
        ctx = _chat(monkeypatch, page, [attached])

        result = await attached_file_upload.upload_attached_file(ctx, attached.file_id, selector)

        held = await page.evaluate("Array.from(document.querySelectorAll('input[type=file]'), el => el.files.length)")

    assert result["ok"] is True
    assert result["input_files"] == [{"name": "synthetic-photo.png", "size": len(data)}]
    assert sum(held) == 1


_CLEARS_ON_CHANGE = '<input type="file" id="picker" onchange="this.value = \'\'">'


@skip_no_browser
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("html", "change_delivered"),
    [(_CLEARS_ON_CHANGE, True), ('<input type="text" id="picker">', False)],
    ids=["page_clears_the_file", "not_a_file_input"],
)
async def test_a_file_the_input_does_not_keep_is_reported_as_not_uploaded(
    monkeypatch: pytest.MonkeyPatch,
    repo: FakeUploadedFilesRepository,
    storage: FakeStorage,
    html: str,
    change_delivered: bool,
) -> None:
    attached = _attach(repo, storage, "synthetic-photo.png", _png())
    async with _fixture_page(html) as page:
        ctx = _chat(monkeypatch, page, [attached])

        result = await attached_file_upload.upload_attached_file(ctx, attached.file_id, "#picker")

    assert result["ok"] is False
    assert result["change_delivered"] is change_delivered
    assert ("upload it twice" in result["error"]) is change_delivered


@skip_no_browser
@pytest.mark.asyncio
async def test_a_set_input_files_error_comes_back_as_the_failure(
    monkeypatch: pytest.MonkeyPatch, repo: FakeUploadedFilesRepository, storage: FakeStorage
) -> None:
    attached = _attach(repo, storage, "synthetic-photo.png", _png())
    async with _fixture_page() as page:
        ctx = _chat(monkeypatch, page, [attached])

        result = await attached_file_upload.upload_attached_file(ctx, attached.file_id, "input[type=file]")

        held = await page.evaluate("Array.from(document.querySelectorAll('input[type=file]'), el => el.files.length)")

    assert result["ok"] is False
    assert "strict mode violation" in result["error"]
    assert "twice" not in result["error"]
    assert held == [0, 0, 0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "expected_detail"),
    [
        (RuntimeError("connect ws://10.1.2.3:9222/devtools/browser/abc refused"), "RuntimeError"),
        (BrowserCdpAcquisitionError("the browser session is not reachable"), "the browser session is not reachable"),
    ],
    ids=["unclassified", "classified"],
)
async def test_a_browser_that_cannot_be_reached_reports_no_endpoint_text(
    monkeypatch: pytest.MonkeyPatch,
    repo: FakeUploadedFilesRepository,
    storage: FakeStorage,
    failure: Exception,
    expected_detail: str,
) -> None:
    attached = _attach(repo, storage, "synthetic-photo.png", b"png")
    ctx = _chat(monkeypatch, None, [attached])

    @asynccontextmanager
    async def _unreachable(_ctx: AgentContext) -> AsyncIterator[None]:
        raise failure
        yield

    monkeypatch.setattr(_shared, "mcp_browser_context", _unreachable)

    result = await attached_file_upload.upload_attached_file(ctx, attached.file_id, "#photo")

    assert result["ok"] is False
    assert expected_detail in result["error"]
    assert "10.1.2.3" not in json.dumps(result)


def _pdf(pages: list[list[str]]) -> bytes:
    def text(line: str) -> str:
        return line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

    objects = ["<< /Type /Catalog /Pages 2 0 R >>", "", "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    kids = []
    for lines in pages:
        stream = "BT /F1 9 Tf 11 TL 36 800 Td " + " ".join(f"({text(line)}) Tj T*" for line in lines) + " ET"
        objects.append(f"<< /Length {len(stream.encode('latin-1'))} >>\nstream\n{stream}\nendstream")
        objects.append(
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 842] "
            f"/Resources << /Font << /F1 3 0 R >> >> /Contents {len(objects)} 0 R >>"
        )
        kids.append(len(objects))
    objects[1] = f"<< /Type /Pages /Kids [{' '.join(f'{kid} 0 R' for kid in kids)}] /Count {len(kids)} >>"
    out = b"%PDF-1.4\n"
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n{body}\nendobj\n".encode("latin-1")
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets)
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return out


SEMANTIC_SECRET = "Zq8!vL3#pR2w"
ScreenHandler = Callable[..., Awaitable[dict[str, Any]]] | None


async def _citing_screen(prompt: str, prompt_name: str) -> dict[str, Any]:
    cited = [SEMANTIC_SECRET] if SEMANTIC_SECRET in prompt else []
    return {"version": "1", "state": "detected" if cited else "clean", "citations": cited}


async def _read(ctx: CopilotContext, file_id: str, offset: int = 0) -> tuple[str, dict[str, Any]]:
    output = await read_attached_pdf_tool.on_invoke_tool(
        SimpleNamespace(context=ctx, tool_name=attached_file_upload.READ_PDF_TOOL_NAME),
        json.dumps({"file_id": file_id, "offset": offset}),
    )
    return output, json.loads(output)


@pytest.mark.asyncio
async def test_reading_an_attached_pdf_returns_every_line_once_with_secrets_redacted(
    monkeypatch: pytest.MonkeyPatch,
    repo: FakeUploadedFilesRepository,
    storage: FakeStorage,
) -> None:
    # Accents and quotes make the serialized text longer than the raw text, which is what the window is sized on.
    lines = [f'Étape {n}: vérifiez "Détails", réservé à l\'élève, ligne {n} terminée.' for n in range(1, 1601)]
    lines[2] = "Sign in with Password: Spring2026!"
    lines[700] = "The vault answer is RegisteredValue77"
    lines[1400] = f"Backup portal code {SEMANTIC_SECRET} when the first one fails"
    attached = _attach(repo, storage, "procedure.pdf", _pdf([lines[i : i + 60] for i in range(0, len(lines), 60)]))
    ctx = _chat(monkeypatch, None, [attached])
    ctx.secret_scrub_values = ["RegisteredValue77"]

    async def _handler(_workflow_permanent_id: str | None, _organization_id: str | None) -> ScreenHandler:
        return _citing_screen

    monkeypatch.setattr(attached_file_upload, "resolve_raw_secret_safety_handler", _handler)

    windows: list[str] = []
    offset: int | None = 0
    while offset is not None:
        output, result = await _read(ctx, attached.file_id, offset)
        assert result["ok"] is True
        assert result["offset"] == offset
        assert len(output) <= 50_000
        assert len(json.dumps(result["text"], ensure_ascii=False)) <= attached_file_upload._READ_WINDOW_SERIALIZED_CHARS
        windows.append(result["text"])
        offset = result["next_offset"]

    text = "".join(windows)
    assert len(windows) > 1
    assert all(window.endswith("\n") for window in windows[:-1])
    assert [n for n in range(1, 1601) if n not in (3, 701, 1401) and text.count(f" ligne {n} ") != 1] == []
    assert "Spring2026!" not in text
    assert "RegisteredValue77" not in text
    assert SEMANTIC_SECRET not in text
    assert text.count("[REDACTED_SECRET]") == 3


PASSPHRASE = "orchard apple moon"
PASSPHRASE_LABEL = "The account password is a phrase"


async def _label_aware_screen(prompt: str, prompt_name: str) -> dict[str, Any]:
    # Like the real screen, a bare phrase is only a secret when its label is in view.
    cited = [PASSPHRASE] if PASSPHRASE_LABEL in prompt and PASSPHRASE in prompt else []
    return {"version": "1", "state": "detected" if cited else "clean", "citations": cited}


async def _label_aware_handler(_workflow_permanent_id: str | None, _organization_id: str | None) -> ScreenHandler:
    return _label_aware_screen


@pytest.mark.asyncio
async def test_a_read_cannot_start_inside_a_line_and_cut_a_secret_away_from_its_label(
    monkeypatch: pytest.MonkeyPatch,
    repo: FakeUploadedFilesRepository,
    storage: FakeStorage,
) -> None:
    attached = _attach(
        repo, storage, "procedure.pdf", _pdf([["Open the portal.", f"{PASSPHRASE_LABEL}: {PASSPHRASE}.", "Submit."]])
    )
    ctx = _chat(monkeypatch, None, [attached])
    monkeypatch.setattr(attached_file_upload, "resolve_raw_secret_safety_handler", _label_aware_handler)

    result = await attached_file_upload.read_attached_pdf(ctx, attached.file_id, offset=40)

    assert result["ok"] is True
    assert result["offset"] == 0
    assert PASSPHRASE not in result["text"]


@pytest.mark.asyncio
async def test_a_secret_shaped_attachment_name_stays_out_of_the_logs(
    monkeypatch: pytest.MonkeyPatch,
    repo: FakeUploadedFilesRepository,
    storage: FakeStorage,
) -> None:
    token = "aB3dE9fG2hJ7kL1mN5pQ8rS4"
    attached = _attach(repo, storage, f"vault_{token}.pdf", _pdf([["Open the claims form."]]))
    ctx = _chat(monkeypatch, None, [attached])
    monkeypatch.setattr(attached_file_upload, "resolve_raw_secret_safety_handler", _label_aware_handler)

    with capture_logs() as logs:
        result = await attached_file_upload.read_attached_pdf(ctx, attached.file_id)

    assert result["ok"] is True
    assert logs
    assert token not in json.dumps(logs, default=str)
    assert token not in json.dumps(result)


@pytest.mark.asyncio
async def test_each_window_is_screened_with_the_text_before_it(
    monkeypatch: pytest.MonkeyPatch,
    repo: FakeUploadedFilesRepository,
    storage: FakeStorage,
) -> None:
    lines = [f"Step {n}: copy row {n} from the export into the matching form field." for n in range(1, 1201)]
    attached = _attach(repo, storage, "procedure.pdf", _pdf([lines[i : i + 60] for i in range(0, len(lines), 60)]))
    ctx = _chat(monkeypatch, None, [attached])
    prompts: list[str] = []

    async def _recording_screen(prompt: str, prompt_name: str) -> dict[str, Any]:
        prompts.append(prompt)
        return {"version": "1", "state": "clean", "citations": []}

    async def _handler(_workflow_permanent_id: str | None, _organization_id: str | None) -> ScreenHandler:
        return _recording_screen

    monkeypatch.setattr(attached_file_upload, "resolve_raw_secret_safety_handler", _handler)

    first = await attached_file_upload.read_attached_pdf(ctx, attached.file_id)
    second = await attached_file_upload.read_attached_pdf(ctx, attached.file_id, first["next_offset"])

    assert first["text"].rstrip().splitlines()[-1] in prompts[1]
    assert first["text"].rstrip().splitlines()[-1] not in second["text"]


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["not_in_this_chat", "not_a_pdf", "no_text_layer", "screen_unavailable"])
async def test_an_attached_pdf_that_cannot_be_read_safely_returns_no_text(
    monkeypatch: pytest.MonkeyPatch,
    repo: FakeUploadedFilesRepository,
    storage: FakeStorage,
    case: str,
) -> None:
    data = {"not_a_pdf": _png(), "no_text_layer": _pdf([[]])}.get(case, _pdf([["Open the claims form."]]))
    attached = _attach(repo, storage, "procedure.pdf", data)
    ctx = _chat(monkeypatch, None, [] if case == "not_in_this_chat" else [attached])

    async def _handler(_workflow_permanent_id: str | None, _organization_id: str | None) -> ScreenHandler:
        return None if case == "screen_unavailable" else _citing_screen

    monkeypatch.setattr(attached_file_upload, "resolve_raw_secret_safety_handler", _handler)

    result = await attached_file_upload.read_attached_pdf(ctx, attached.file_id)

    assert result["ok"] is False
    assert result["error"]
    assert "text" not in result
