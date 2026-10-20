from __future__ import annotations

import hashlib
import json
import struct
import zlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest
from playwright.async_api import Page, Route, async_playwright

from skyvern.forge import app
from skyvern.forge.sdk.copilot.context import CopilotContext
from skyvern.forge.sdk.copilot.runtime import AgentContext
from skyvern.forge.sdk.copilot.tools import _shared, attached_file_upload
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
