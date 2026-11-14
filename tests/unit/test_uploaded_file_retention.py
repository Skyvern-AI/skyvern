"""Retention and deletion controls for uploaded files (SKY-14088).

The properties under test are the ones a caller's data depends on: a file id names a file
only inside the organization that uploaded it, the URI that gets deleted comes from the
server's own row rather than the request, a delete that did not remove the bytes is not
reported as success, and the expiry sweep can only reach files whose uploader asked for an
expiry.
"""

from __future__ import annotations

import io
import os
import re
import struct
import zipfile
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Callable
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException, UploadFile
from fastapi.testclient import TestClient
from PIL import Image
from starlette.datastructures import Headers
from structlog.testing import capture_logs

from skyvern.config import settings
from skyvern.exceptions import WorkflowNotFound
from skyvern.forge.sdk.api import files as files_api
from skyvern.forge.sdk.artifact.storage.local import LocalStorage
from skyvern.forge.sdk.artifact.storage.s3 import S3Storage
from skyvern.forge.sdk.schemas.files import UploadedFile
from skyvern.forge.sdk.schemas.organizations import Organization
from skyvern.forge.sdk.workflow.models.block import BlockType
from skyvern.schemas.runs import RunEngine
from skyvern.services import uploaded_file_service
from tests.unit._uploaded_files_fake import FakeUploadedFilesRepository, uploaded_file_uri
from tests.unit.shared_route_app import shared_route_app

VICTIM_ORG_ID = "o_victim"
ATTACKER_ORG_ID = "o_attacker"


class FakeStorage:
    """Enforces the same org-prefix rule the real storage backends do."""

    presigned_urls_expire = True

    def __init__(self) -> None:
        self.deleted: list[str] = []
        self.fail_with: Exception | None = None
        # URIs whose object is already gone. Deleting one succeeds without deleting
        # anything, as S3, GCS, Azure and local storage all do for an absent key.
        self.absent: set[str] = set()
        self.objects: dict[str, bytes] = {}
        self.sizes: dict[str, int | None] = {}
        self.downloads: list[str] = []

    @staticmethod
    def _assert_org_prefix(uri: str, organization_id: str) -> None:
        if not uri.startswith(f"s3://{settings.AWS_S3_BUCKET_UPLOADS}/{settings.ENV}/{organization_id}/"):
            raise PermissionError(f"No permission to access storage URI: {uri}")

    async def download_managed_file(self, uri: str, organization_id: str) -> bytes | None:
        self.downloads.append(uri)
        self._assert_org_prefix(uri, organization_id)
        return self.objects.get(uri)

    async def managed_file_size(self, uri: str, organization_id: str) -> int | None:
        self._assert_org_prefix(uri, organization_id)
        if uri in self.sizes:
            return self.sizes[uri]
        return len(self.objects[uri]) if uri in self.objects else None

    async def read_managed_file_head(self, uri: str, organization_id: str, length: int) -> bytes | None:
        content = await self.download_managed_file(uri, organization_id)
        return None if content is None else content[:length]

    async def presign_managed_file(
        self, uri: str, organization_id: str, *, expiry_seconds: int, inline_content_type: str
    ) -> str:
        self._assert_org_prefix(uri, organization_id)
        return f"https://signed.test/{uri.rsplit('/', 1)[-1]}?inline={inline_content_type}&ttl={expiry_seconds}"

    async def delete_legacy_file(self, *, organization_id: str, uri: str) -> None:
        self._assert_org_prefix(uri, organization_id)
        if self.fail_with:
            raise self.fail_with
        if uri in self.absent:
            return
        self.deleted.append(uri)


@pytest.fixture
def repo() -> FakeUploadedFilesRepository:
    return FakeUploadedFilesRepository()


@pytest.fixture
def storage() -> FakeStorage:
    return FakeStorage()


@pytest.fixture
def service_app(repo: FakeUploadedFilesRepository, storage: FakeStorage):  # type: ignore[no-untyped-def]
    fake_app = SimpleNamespace(
        DATABASE=SimpleNamespace(uploaded_files=repo),
        STORAGE=storage,
    )
    with patch.object(uploaded_file_service, "app", fake_app):
        yield fake_app


def _make_org(organization_id: str) -> Organization:
    now = datetime.now(timezone.utc)
    return Organization(
        organization_id=organization_id,
        organization_name="org",
        created_at=now,
        modified_at=now,
    )


def _client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    return TestClient(shared_route_app(monkeypatch))


@pytest.mark.asyncio
async def test_a_file_past_its_expiry_no_longer_dereferences(
    repo: FakeUploadedFilesRepository,
    service_app: object,
) -> None:
    """The purge runs hourly, so an expired row still names live bytes until it runs; a workflow that
    saved the id must not keep reading them in that window."""
    now = datetime.now(timezone.utc)
    expired = repo.seed(VICTIM_ORG_ID, expires_at=now - timedelta(minutes=1), filename="expired.csv")
    live = repo.seed(VICTIM_ORG_ID, expires_at=now + timedelta(days=1), filename="live.csv")

    assert await uploaded_file_service.resolve_file_reference(file_id=expired, organization_id=VICTIM_ORG_ID) is None
    assert await uploaded_file_service.resolve_file_reference(
        file_id=live, organization_id=VICTIM_ORG_ID
    ) == uploaded_file_uri(VICTIM_ORG_ID, "live.csv")


def _png(width: int, height: int) -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
        + struct.pack(">II", width, height)
        + b"\x08\x02\x00\x00\x00"
        + b"\x00" * 64
    )


def _webp(chunk: bytes, payload: bytes) -> bytes:
    body = chunk + len(payload).to_bytes(4, "little") + payload
    return b"RIFF" + (len(body) + 4).to_bytes(4, "little") + b"WEBP" + body + b"\x00" * 64


def _webp_vp8x(width: int, height: int) -> bytes:
    return _webp(b"VP8X", b"\x00" * 4 + (width - 1).to_bytes(3, "little") + (height - 1).to_bytes(3, "little"))


def _webp_vp8l(width: int, height: int) -> bytes:
    return _webp(b"VP8L", b"\x2f" + ((width - 1) | (height - 1) << 14).to_bytes(4, "little"))


def _webp_vp8(width: int, height: int) -> bytes:
    return _webp(b"VP8 ", b"\x00" * 3 + b"\x9d\x01\x2a" + struct.pack("<HH", width, height))


def _gif(width: int, height: int) -> bytes:
    frame = b"\x2c" + struct.pack("<HHHH", 0, 0, width, height) + b"\x00"
    return b"GIF89a" + struct.pack("<HH", width, height) + b"\x00" * 3 + frame + b"\x00" * 64


def _pillow(image_format: str, size: tuple[int, int] = (1200, 800), **save: Any) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", size, (40, 90, 160)).save(out, image_format, **save)
    return out.getvalue()


# A 1x1 screen whose first frame sits at (20000, 20000): Chromium and Pillow size it 20001x20001.
_GIF_FRAME_OFF_SCREEN = bytes.fromhex("47494638396101000100800000000000ffffff2c204e204e010001000002024401003b")


def _jpeg_decoy(after_soi: bytes) -> bytes:
    # Bytes a decoder skips without a length (a fill byte, RST0), then a comment that hides a 1x1 SOF where a walker
    # that reads a length after them lands; the real SOF declares 10000x9000.
    real = bytearray(_pillow("JPEG", (64, 64)))
    sof = real.index(b"\xff\xc0")
    real[sof + 5 : sof + 9] = struct.pack(">HH", 9000, 10000)
    app0_end = 4 + int.from_bytes(real[4:6], "big")
    head = bytes(real[:2]) + after_soi + bytes(real[2:app0_end])
    lands = 4 + int.from_bytes(head[4:6], "big") - len(head) - 4
    payload = b"\x00" * lands + b"\xff\xc0\x00\x0b\x08\x00\x01\x00\x01\x01\x01" + b"\x00" * 32
    return head + b"\xff\xfe" + (len(payload) + 2).to_bytes(2, "big") + payload + bytes(real[app0_end:])


def _sof(width: int, height: int) -> bytes:
    return b"\xff\xc2\x00\x11\x08" + struct.pack(">HH", height, width) + b"\x03" + b"\x00" * 9


_JPEG_SOS = b"\xff\xda\x00\x08\x01\x01\x00\x00\x3f\x00"


def _jpeg(width: int, height: int, app_segment_bytes: tuple[int, ...] = ()) -> bytes:
    app = b"".join(b"\xff\xe1" + (size + 2).to_bytes(2, "big") + b"\x00" * size for size in app_segment_bytes)
    jfif = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    return jfif + app + _sof(width, height) + _JPEG_SOS + b"\x00" * 64


def _gif_after_extension(sub_blocks: bytes, width: int = 64, height: int = 64) -> bytes:
    frame = b"\x2c" + struct.pack("<HHHH", 0, 0, width, height) + b"\x00"
    return b"GIF89a" + struct.pack("<HH", width, height) + b"\x00" * 3 + b"\x21\xfe" + sub_blocks + frame


def _filled(prefix: bytes, unit: bytes) -> bytes:
    count = (uploaded_file_service.BLOCK_IMAGE_MAX_BYTES - len(prefix)) // len(unit)
    return prefix + unit * count


# Shapes that walk the whole 4 MiB one marker or one sub-block at a time unless the walk is bounded.
_PAST_THE_STEP_CAP = 4097
# A sub-block whose size byte reads as an image descriptor of a 1x1 frame.
_DESCRIPTOR_SUB_BLOCK = b"\x2c" + struct.pack("<HHHH", 0, 0, 1, 1) + b"\x00" * 36
CRAFTED_4_MIB_HEADERS = {
    "jpeg-fill-bytes": _filled(b"\xff\xd8", b"\xff"),
    "jpeg-ff01-pairs": _filled(b"\xff\xd8", b"\xff\x01"),
    "jpeg-empty-app-segments": _filled(b"\xff\xd8", b"\xff\xe1\x00\x02"),
    "gif-one-byte-comment-sub-blocks": _filled(b"GIF89a\x01\x00\x01\x00\x00\x00\x00\x21\xfe", b"\x01\x00"),
}


_SCREENSHOT_PNG = _png(1920, 1080)
_RASTER_HEADS = {
    "image/png": _SCREENSHOT_PNG,
    "image/jpeg": _jpeg(1920, 1080),
    "image/gif": _gif(640, 480),
    "image/webp": _webp_vp8(1920, 1080),
}


class TestFileSignedUrlEndpoint:
    def _request(
        self,
        client: TestClient,
        file_id: str,
        caller_org_id: str,
        workflow_org_id: str,
        named: list[str],
        folder_denied: bool = False,
        version: int | None = None,
        named_by_version: dict[int, list[str]] | None = None,
        named_elsewhere: tuple[str, ...] = (),
    ) -> Any:
        async def get_workflow(workflow_permanent_id: str, organization_id: str, version: int | None) -> Any:
            if organization_id != workflow_org_id:
                raise WorkflowNotFound(workflow_permanent_id=workflow_permanent_id)
            names = named if version is None else (named_by_version or {}).get(version, named)
            images = [SimpleNamespace(file_id=file_id) for file_id in names]
            inner = SimpleNamespace(
                label="inner",
                block_type=BlockType.NAVIGATION,
                notes=" ".join(named_elsewhere),
                doc_images=images[::2],
                prompt_images=images[1::2],
            )
            loop = SimpleNamespace(
                label="loop", block_type=BlockType.FOR_LOOP, doc_images=None, prompt_images=None, loop_blocks=[inner]
            )
            dumped = {"blocks": [{"notes": inner.notes, "doc_images": [{"file_id": i.file_id} for i in images]}]}
            return SimpleNamespace(
                organization_id=workflow_org_id,
                workflow_definition=SimpleNamespace(blocks=[loop], model_dump=lambda mode: dumped),
            )

        route_app = MagicMock()
        route_app.WORKFLOW_SERVICE.get_workflow_by_permanent_id = AsyncMock(side_effect=get_workflow)
        route_app.AGENT_FUNCTION.enforce_folder_access = AsyncMock(
            side_effect=HTTPException(status_code=403, detail="Folder access denied") if folder_denied else None
        )
        self.folder_check = route_app.AGENT_FUNCTION.enforce_folder_access
        with (
            patch("skyvern.forge.sdk.routes.agent_protocol.app", route_app),
            patch(
                "skyvern.forge.sdk.services.org_auth_service.get_current_org_cached",
                new=AsyncMock(return_value=_make_org(caller_org_id)),
            ),
        ):
            return client.get(
                f"/v1/files/{file_id}/signed-url",
                params={"workflow_permanent_id": "wpid_doc", **({} if version is None else {"version": version})},
                headers={"x-api-key": "key"},
            )

    def _seed(self, repo: FakeUploadedFilesRepository, storage: FakeStorage, org: str, body: bytes, **seed: Any) -> str:
        file_id = repo.seed(org, **seed)
        storage.objects[repo.rows[file_id].storage_uri] = body
        return file_id

    @pytest.mark.parametrize(
        ("media_type", "head"),
        [
            *_RASTER_HEADS.items(),
            ("image/webp", _webp_vp8x(1920, 1080)),
            ("image/webp", _webp_vp8l(1920, 1080)),
            ("image/jpeg", _jpeg(1920, 1080, app_segment_bytes=(30_000, 30_000))),
            ("image/png", _pillow("PNG")),
            ("image/jpeg", _pillow("JPEG")),
            ("image/jpeg", _pillow("JPEG", progressive=True)),
            ("image/jpeg", _pillow("JPEG", icc_profile=b"\x00" * 70_000)),
            ("image/gif", _pillow("GIF")),
            ("image/webp", _pillow("WEBP")),
            ("image/webp", _pillow("WEBP", lossless=True)),
            ("image/gif", _gif_after_extension(b"\x03abc\x01d\x00")),
            ("image/jpeg", _jpeg(1920, 1080).replace(b"\xff\xc2", b"\xff\xff\xd0\xff\xff\xff\xc2")),
        ],
        ids=[
            *_RASTER_HEADS,
            "webp-vp8x",
            "webp-vp8l",
            "jpeg-app-segments",
            "pillow-png",
            "pillow-jpeg",
            "pillow-jpeg-progressive",
            "pillow-jpeg-sof-past-64-kib",
            "pillow-gif",
            "pillow-webp-lossy",
            "pillow-webp-lossless",
            "gif-after-a-comment",
            "jpeg-fill-bytes-and-rst0-before-sof",
        ],
    )
    def test_a_named_image_of_the_workflows_organization_gets_an_inline_one_hour_url(
        self,
        service_app: object,
        repo: FakeUploadedFilesRepository,
        storage: FakeStorage,
        monkeypatch: Any,
        media_type: str,
        head: bytes,
    ) -> None:
        file_id = self._seed(repo, storage, VICTIM_ORG_ID, head, size_bytes=len(head))

        resp = self._request(_client(monkeypatch), file_id, VICTIM_ORG_ID, VICTIM_ORG_ID, [file_id])

        assert resp.status_code == 200
        body = resp.json()
        assert body["content_type"] == media_type
        assert body["signed_url"].endswith(f"?inline={media_type}&ttl=3600")
        assert abs(body["expires_at"] - (datetime.now(timezone.utc).timestamp() + 3600)) < 60

    @pytest.mark.asyncio
    async def test_a_local_file_url_reports_no_expiry_because_it_never_expires(
        self, service_app: Any, repo: FakeUploadedFilesRepository, tmp_path: Any
    ) -> None:
        image = tmp_path / settings.ENV / VICTIM_ORG_ID / "uploads" / "shot.png"
        image.parent.mkdir(parents=True)
        image.write_bytes(_SCREENSHOT_PNG)
        file_id = repo.seed(VICTIM_ORG_ID, expires_at=datetime.now(timezone.utc) + timedelta(seconds=60))
        repo.rows[file_id].storage_uri = f"file://{image}"
        service_app.STORAGE = LocalStorage(artifact_path=str(tmp_path))

        signed = await uploaded_file_service.mint_file_url(file_id=file_id, organization_id=VICTIM_ORG_ID)

        assert signed is not None
        assert signed.signed_url == f"file://{image}"
        assert signed.expires_at is None

    def test_another_organization_never_gets_a_url(
        self, service_app: object, repo: FakeUploadedFilesRepository, storage: FakeStorage, monkeypatch: Any
    ) -> None:
        client = _client(monkeypatch)
        victim_file = self._seed(repo, storage, VICTIM_ORG_ID, _SCREENSHOT_PNG)

        # The attacker names the victim's file in its own workflow, or asks through the victim's workflow.
        own_workflow = self._request(client, victim_file, ATTACKER_ORG_ID, ATTACKER_ORG_ID, [victim_file])
        victim_workflow = self._request(client, victim_file, ATTACKER_ORG_ID, VICTIM_ORG_ID, [victim_file])
        repo.rows.clear()
        never_existed = self._request(client, victim_file, ATTACKER_ORG_ID, ATTACKER_ORG_ID, [victim_file])

        assert own_workflow.status_code == victim_workflow.status_code == never_existed.status_code == 404
        assert own_workflow.json() == victim_workflow.json() == never_existed.json()
        assert storage.downloads == []

    def test_version_selects_the_definition_that_must_name_the_file(
        self, service_app: object, repo: FakeUploadedFilesRepository, storage: FakeStorage, monkeypatch: Any
    ) -> None:
        client = _client(monkeypatch)
        file_id = self._seed(repo, storage, VICTIM_ORG_ID, _SCREENSHOT_PNG)
        only_in_v1 = {1: [file_id]}

        v1 = self._request(client, file_id, VICTIM_ORG_ID, VICTIM_ORG_ID, [], version=1, named_by_version=only_in_v1)
        latest = self._request(client, file_id, VICTIM_ORG_ID, VICTIM_ORG_ID, [], named_by_version=only_in_v1)

        assert v1.status_code == 200
        assert latest.status_code == 404

    @pytest.mark.parametrize("version", [0, 2**31])
    def test_a_version_outside_the_int4_range_is_rejected_like_the_other_versioned_workflow_routes(
        self,
        service_app: object,
        repo: FakeUploadedFilesRepository,
        storage: FakeStorage,
        monkeypatch: Any,
        version: int,
    ) -> None:
        file_id = self._seed(repo, storage, VICTIM_ORG_ID, _SCREENSHOT_PNG)

        resp = self._request(_client(monkeypatch), file_id, VICTIM_ORG_ID, VICTIM_ORG_ID, [file_id], version=version)

        assert resp.status_code == 422

    @pytest.mark.boundary
    def test_the_client_image_limits_match_the_servers(self) -> None:
        with open("skyvern-frontend/src/routes/workflows/editor/doc/docImageUploads.ts") as source_file:
            source = source_file.read()
        max_bytes = re.search(r"export const DOC_IMAGE_MAX_BYTES = ([\d *]+);", source)
        max_pixels = re.search(r"export const DOC_IMAGE_MAX_PIXELS = ([\d_]+);", source)
        types = re.search(r"export const DOC_IMAGE_TYPES = \[([^\]]*)\]", source)
        assert max_bytes and max_pixels and types, "a doc image limit was renamed; update this guard"

        product = 1
        for factor in max_bytes.group(1).split("*"):
            product *= int(factor)
        assert product == uploaded_file_service.BLOCK_IMAGE_MAX_BYTES
        assert int(max_pixels.group(1).replace("_", "")) == uploaded_file_service.BLOCK_IMAGE_MAX_PIXELS
        assert set(re.findall(r'"([^"]+)"', types.group(1))) == uploaded_file_service.BLOCK_IMAGE_MEDIA_TYPES

    def test_a_file_the_workflow_does_not_name_is_a_404(
        self, service_app: object, repo: FakeUploadedFilesRepository, storage: FakeStorage, monkeypatch: Any
    ) -> None:
        file_id = self._seed(repo, storage, VICTIM_ORG_ID, _SCREENSHOT_PNG)
        longer = f"{file_id}0"

        resp = self._request(_client(monkeypatch), file_id, VICTIM_ORG_ID, VICTIM_ORG_ID, [longer])

        assert resp.status_code == 404

    def test_a_reader_gets_no_url_for_an_upload_the_version_names_only_outside_its_image_fields(
        self, service_app: object, repo: FakeUploadedFilesRepository, storage: FakeStorage, monkeypatch: Any
    ) -> None:
        client = _client(monkeypatch)
        doc_image, prompt_image, elsewhere = (
            self._seed(repo, storage, VICTIM_ORG_ID, _SCREENSHOT_PNG) for _ in range(3)
        )
        named = [doc_image, prompt_image]

        outside = self._request(client, elsewhere, VICTIM_ORG_ID, VICTIM_ORG_ID, named, named_elsewhere=(elsewhere,))
        served = [self._request(client, file_id, VICTIM_ORG_ID, VICTIM_ORG_ID, named) for file_id in named]

        assert outside.status_code == 404
        assert [resp.status_code for resp in served] == [200, 200]

    def test_a_folder_the_caller_cannot_read_is_refused(
        self, service_app: object, repo: FakeUploadedFilesRepository, storage: FakeStorage, monkeypatch: Any
    ) -> None:
        file_id = self._seed(repo, storage, VICTIM_ORG_ID, _SCREENSHOT_PNG)

        resp = self._request(_client(monkeypatch), file_id, VICTIM_ORG_ID, VICTIM_ORG_ID, [file_id], folder_denied=True)

        assert resp.status_code == 403
        assert self.folder_check.await_args.args[1:] == ("workflow.read", {"workflow_permanent_id": "wpid_doc"})
        assert storage.downloads == []

    def test_a_deleted_expired_or_vanished_file_is_no_longer_available(
        self, service_app: object, repo: FakeUploadedFilesRepository, storage: FakeStorage, monkeypatch: Any
    ) -> None:
        client = _client(monkeypatch)
        expired = self._seed(
            repo, storage, VICTIM_ORG_ID, _SCREENSHOT_PNG, expires_at=datetime.now(timezone.utc) - timedelta(minutes=1)
        )
        vanished = repo.seed(VICTIM_ORG_ID, filename="vanished.png")
        deleted = self._seed(repo, storage, VICTIM_ORG_ID, _SCREENSHOT_PNG, filename="deleted.png")
        repo.rows[deleted].deleted_at = datetime.now(timezone.utc)

        for file_id in (expired, vanished, deleted):
            resp = self._request(client, file_id, VICTIM_ORG_ID, VICTIM_ORG_ID, [file_id])
            assert resp.status_code == 404
            assert resp.json() == {"detail": "File is no longer available"}

    @pytest.mark.parametrize(
        ("filename", "body"),
        [
            ("notes.pdf", b"%PDF-1.7 body"),
            ("table.csv", b"name,amount\nalpha,1\n"),
            ("pic.svg", b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'),
            ("page.html", b"<!doctype html><html><script>alert(1)</script></html>"),
            ("clip.mp4", b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"\x00" * 64),
        ],
    )
    def test_a_named_file_that_is_not_a_raster_image_answers_like_a_missing_file(
        self,
        service_app: object,
        repo: FakeUploadedFilesRepository,
        storage: FakeStorage,
        monkeypatch: Any,
        filename: str,
        body: bytes,
    ) -> None:
        client = _client(monkeypatch)
        file_id = self._seed(repo, storage, VICTIM_ORG_ID, body, filename=filename)
        missing = "file_missing"

        resp = self._request(client, file_id, VICTIM_ORG_ID, VICTIM_ORG_ID, [file_id])
        missing_resp = self._request(client, missing, VICTIM_ORG_ID, VICTIM_ORG_ID, [missing])

        assert resp.status_code == missing_resp.status_code == 404
        assert resp.json() == missing_resp.json() == {"detail": "File is no longer available"}

    def test_an_image_is_served_up_to_the_4_mib_cap_and_not_past_it(
        self, service_app: object, repo: FakeUploadedFilesRepository, storage: FakeStorage, monkeypatch: Any
    ) -> None:
        client = _client(monkeypatch)
        at_cap = self._seed(repo, storage, VICTIM_ORG_ID, _SCREENSHOT_PNG, filename="at.png", size_bytes=4194304)
        over_cap = self._seed(repo, storage, VICTIM_ORG_ID, _SCREENSHOT_PNG, filename="over.png", size_bytes=4194305)

        served = self._request(client, at_cap, VICTIM_ORG_ID, VICTIM_ORG_ID, [at_cap])
        refused = self._request(client, over_cap, VICTIM_ORG_ID, VICTIM_ORG_ID, [over_cap])

        assert served.status_code == 200
        assert refused.status_code == 404
        assert refused.json() == {"detail": "File is no longer available"}

    @pytest.mark.parametrize(("stored_size", "status"), [(4194304, 200), (4194305, 404), (None, 404)])
    def test_a_row_with_no_recorded_size_is_held_to_the_cap_by_the_stored_size(
        self,
        service_app: object,
        repo: FakeUploadedFilesRepository,
        storage: FakeStorage,
        monkeypatch: Any,
        stored_size: int | None,
        status: int,
    ) -> None:
        file_id = self._seed(repo, storage, VICTIM_ORG_ID, _SCREENSHOT_PNG)
        storage.sizes[repo.rows[file_id].storage_uri] = stored_size

        resp = self._request(_client(monkeypatch), file_id, VICTIM_ORG_ID, VICTIM_ORG_ID, [file_id])

        assert resp.status_code == status

    @pytest.mark.parametrize(
        "head",
        [
            _png(10_000, 8_001),
            _webp_vp8x(16_384, 16_384),
            _webp_vp8x(10_000, 8_001),
            _webp_vp8l(10_000, 8_001),
            _webp_vp8(10_000, 8_001),
            _gif(10_000, 8_001),
            _jpeg(10_000, 8_001, app_segment_bytes=(30_000, 30_000)),
            _png(0, 1080),
            b"\xff\xd8\xff\xe1\xff\xff" + b"\x00" * 70_000,
            _GIF_FRAME_OFF_SCREEN,
            b"GIF89a\x01\x00\x01\x00\x00\x00\x00\x3b" + b"\x00" * 64,
            _jpeg_decoy(b"\xff"),
            _jpeg_decoy(b"\xff\xd0"),
            _jpeg(1, 1).replace(_JPEG_SOS, _sof(10_000, 9_000) + _JPEG_SOS),
            _jpeg(10_000, 9_000).replace(_JPEG_SOS, _sof(1, 1) + _JPEG_SOS),
            _jpeg(64, 64).replace(b"\xff\xc2", b"\xff\x01" * _PAST_THE_STEP_CAP + b"\xff\xc2"),
            _gif_after_extension(_DESCRIPTOR_SUB_BLOCK * _PAST_THE_STEP_CAP + b"\x00"),
            *CRAFTED_4_MIB_HEADERS.values(),
        ],
        ids=[
            "png",
            "webp-268-megapixels",
            "webp-vp8x",
            "webp-vp8l",
            "webp-vp8",
            "gif",
            "jpeg-late-sof",
            "png-zero",
            "jpeg-no-sof",
            "gif-first-frame-off-screen",
            "gif-no-frame",
            "jpeg-fill-byte-decoy",
            "jpeg-rst0-decoy",
            "jpeg-second-sof-larger",
            "jpeg-second-sof-smaller",
            "jpeg-bare-markers-past-the-step-cap",
            "gif-sub-blocks-past-the-step-cap",
            *CRAFTED_4_MIB_HEADERS,
        ],
    )
    def test_an_image_declaring_more_than_80_megapixels_or_no_size_answers_like_a_missing_file(
        self,
        service_app: object,
        repo: FakeUploadedFilesRepository,
        storage: FakeStorage,
        monkeypatch: Any,
        head: bytes,
    ) -> None:
        file_id = self._seed(repo, storage, VICTIM_ORG_ID, head, size_bytes=len(head))

        resp = self._request(_client(monkeypatch), file_id, VICTIM_ORG_ID, VICTIM_ORG_ID, [file_id])

        assert resp.status_code == 404
        assert resp.json() == {"detail": "File is no longer available"}

    @pytest.mark.parametrize("header", [_png, _webp_vp8x, _webp_vp8l, _webp_vp8, _gif, _jpeg])
    def test_an_image_at_80_megapixels_is_served(
        self,
        service_app: object,
        repo: FakeUploadedFilesRepository,
        storage: FakeStorage,
        monkeypatch: Any,
        header: Callable[[int, int], bytes],
    ) -> None:
        file_id = self._seed(repo, storage, VICTIM_ORG_ID, header(10_000, 8_000), size_bytes=1024)

        resp = self._request(_client(monkeypatch), file_id, VICTIM_ORG_ID, VICTIM_ORG_ID, [file_id])

        assert resp.status_code == 200

    def test_a_run_bound_upload_named_by_the_workflow_is_no_longer_available(
        self, service_app: object, repo: FakeUploadedFilesRepository, storage: FakeStorage, monkeypatch: Any
    ) -> None:
        file_id = self._seed(repo, storage, VICTIM_ORG_ID, _SCREENSHOT_PNG, run_id="wr_1")
        presign = AsyncMock(wraps=storage.presign_managed_file)

        with patch.object(storage, "presign_managed_file", presign):
            resp = self._request(_client(monkeypatch), file_id, VICTIM_ORG_ID, VICTIM_ORG_ID, [file_id])

        assert resp.status_code == 404
        assert resp.json() == {"detail": "File is no longer available"}
        presign.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_row_of_another_organization_is_refused_even_if_the_lookup_returns_it(
        self, service_app: object, repo: FakeUploadedFilesRepository, storage: FakeStorage
    ) -> None:
        file_id = self._seed(repo, storage, VICTIM_ORG_ID, _SCREENSHOT_PNG)
        unscoped = AsyncMock(return_value=repo.rows[file_id])
        with patch.object(repo, "get_uploaded_file", unscoped):
            assert await uploaded_file_service.mint_file_url(file_id=file_id, organization_id=ATTACKER_ORG_ID) is None
        assert storage.downloads == []


class TestDeleteEndpoint:
    def _request(self, client: TestClient, file_id: str, caller_org_id: str, storage: FakeStorage) -> object:
        with (
            patch("skyvern.forge.sdk.routes.agent_protocol.app", MagicMock()),
            patch(
                "skyvern.forge.sdk.services.org_auth_service.get_current_org_cached",
                new=AsyncMock(return_value=_make_org(caller_org_id)),
            ),
        ):
            return client.delete(f"/v1/files/{file_id}", headers={"x-api-key": "key"})

    def test_deletes_the_bytes_at_the_uri_recorded_by_the_server(
        self,
        service_app: object,
        repo: FakeUploadedFilesRepository,
        storage: FakeStorage,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client = _client(monkeypatch)
        file_id = repo.seed(VICTIM_ORG_ID)

        resp = self._request(client, file_id, VICTIM_ORG_ID, storage)

        assert resp.status_code == 204  # type: ignore[attr-defined]
        assert storage.deleted == [uploaded_file_uri(VICTIM_ORG_ID)]
        assert repo.live_ids() == set()

    def test_another_orgs_file_id_is_a_404_that_deletes_nothing(
        self,
        service_app: object,
        repo: FakeUploadedFilesRepository,
        storage: FakeStorage,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The whole authorization boundary: a guessed id from another tenant must be inert."""
        client = _client(monkeypatch)
        victim_file_id = repo.seed(VICTIM_ORG_ID)

        resp = self._request(client, victim_file_id, ATTACKER_ORG_ID, storage)

        assert resp.status_code == 404  # type: ignore[attr-defined]
        assert storage.deleted == []
        assert repo.live_ids() == {victim_file_id}

    def test_a_cross_org_id_is_indistinguishable_from_one_that_never_existed(
        self,
        service_app: object,
        repo: FakeUploadedFilesRepository,
        storage: FakeStorage,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Otherwise the endpoint is an oracle for which file ids exist in other orgs."""
        client = _client(monkeypatch)
        victim_file_id = repo.seed(VICTIM_ORG_ID)

        cross_org = self._request(client, victim_file_id, ATTACKER_ORG_ID, storage)
        repo.rows.clear()
        never_existed = self._request(client, victim_file_id, ATTACKER_ORG_ID, storage)

        assert cross_org.status_code == never_existed.status_code == 404  # type: ignore[attr-defined]
        assert cross_org.json() == never_existed.json()  # type: ignore[attr-defined]

    def test_deleting_the_same_file_twice_is_a_404_not_a_second_delete(
        self,
        service_app: object,
        repo: FakeUploadedFilesRepository,
        storage: FakeStorage,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client = _client(monkeypatch)
        file_id = repo.seed(VICTIM_ORG_ID)

        assert self._request(client, file_id, VICTIM_ORG_ID, storage).status_code == 204  # type: ignore[attr-defined]
        assert self._request(client, file_id, VICTIM_ORG_ID, storage).status_code == 404  # type: ignore[attr-defined]
        assert storage.deleted == [uploaded_file_uri(VICTIM_ORG_ID)]


class TestDeleteFailureHandling:
    @pytest.mark.asyncio
    async def test_a_storage_failure_leaves_the_file_listed_rather_than_reporting_success(
        self, service_app: object, repo: FakeUploadedFilesRepository, storage: FakeStorage
    ) -> None:
        """A caller told "deleted" while the bytes survive is the one outcome this feature cannot have."""
        file_id = repo.seed(VICTIM_ORG_ID)
        storage.fail_with = RuntimeError("s3 is down")

        with pytest.raises(RuntimeError):
            await uploaded_file_service.delete_uploaded_file(file_id=file_id, organization_id=VICTIM_ORG_ID)

        assert repo.live_ids() == {file_id}


class TestRetentionPeriod:
    @pytest.mark.parametrize("retention_days", [0, -1, settings.MAX_UPLOADED_FILE_RETENTION_DAYS + 1])
    def test_out_of_range_retention_is_rejected(self, retention_days: int) -> None:
        with pytest.raises(uploaded_file_service.InvalidRetentionPeriod):
            uploaded_file_service.resolve_expires_at(retention_days)

    def test_no_retention_means_no_expiry_of_its_own(self) -> None:
        assert uploaded_file_service.resolve_expires_at(None) is None

    def test_retention_is_measured_in_days_from_upload(self) -> None:
        now = datetime(2026, 8, 15, tzinfo=timezone.utc)
        assert uploaded_file_service.resolve_expires_at(7, now=now) == now + timedelta(days=7)

    def test_upload_rejects_a_bad_retention_before_writing_any_bytes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        with (
            patch("skyvern.forge.sdk.routes.agent_protocol.app") as app_module,
            patch(
                "skyvern.forge.sdk.services.org_auth_service.get_current_org_cached",
                new=AsyncMock(return_value=_make_org(VICTIM_ORG_ID)),
            ),
        ):
            app_module.SETTINGS_MANAGER.MAX_UPLOAD_FILE_SIZE = settings.MAX_UPLOAD_FILE_SIZE
            app_module.STORAGE.save_legacy_file = AsyncMock()
            resp = _client(monkeypatch).post(
                "/v1/upload_file",
                headers={"x-api-key": "key"},
                files={"file": ("a.csv", io.BytesIO(b"data"), "text/csv")},
                data={"retention_days": "0"},
            )

            assert resp.status_code == 422, resp.text
            app_module.STORAGE.save_legacy_file.assert_not_awaited()


class TestStorageUriUniqueness:
    """A concurrent re-upload of the same filename must not be able to make a delete of one
    file's id destroy a different, still-live file's bytes.

    Before the fix, ``save_legacy_file`` was always called with the caller's original
    filename, so two uploads of the same name on the same day computed the identical
    storage key: the second upload's write silently overwrote the first's object, and a
    delete of the *first* upload's id (still holding the pre-overwrite URI) would then
    delete the *second* upload's bytes while its row stayed listed as live.
    """

    def test_two_uploads_of_a_pathlike_filename_get_distinct_storage_keys(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with (
            patch("skyvern.forge.sdk.routes.agent_protocol.app") as app_module,
            # record_upload lives in uploaded_file_service, which holds its own `app` name
            # bound at import time; patching only agent_protocol.app would leave it pointed
            # at the real (unconfigured) app in this test.
            patch("skyvern.services.uploaded_file_service.app", app_module),
            patch(
                "skyvern.forge.sdk.services.org_auth_service.get_current_org_cached",
                new=AsyncMock(return_value=_make_org(VICTIM_ORG_ID)),
            ),
        ):
            app_module.SETTINGS_MANAGER.MAX_UPLOAD_FILE_SIZE = settings.MAX_UPLOAD_FILE_SIZE
            storage_keys: list[str] = []

            async def fake_save_legacy_file(*, organization_id: str, filename: str, fileObj: object) -> tuple[str, str]:
                # The real backends strip path components before deriving the deterministic
                # storage key. The per-upload id must survive that normalization.
                storage_key = os.path.basename(filename)
                storage_keys.append(storage_key)
                return ("https://presigned.example/x", uploaded_file_uri(organization_id, storage_key))

            async def fake_create_uploaded_file(**kwargs: object) -> UploadedFile:
                now = datetime.now(timezone.utc)
                return UploadedFile(created_at=now, modified_at=now, **kwargs)  # type: ignore[arg-type]

            app_module.STORAGE.save_legacy_file = AsyncMock(side_effect=fake_save_legacy_file)
            app_module.DATABASE.uploaded_files.create_uploaded_file = AsyncMock(side_effect=fake_create_uploaded_file)

            client = _client(monkeypatch)

            def upload() -> object:
                return client.post(
                    "/v1/upload_file",
                    headers={"x-api-key": "key"},
                    files={"file": ("dir/report.pdf", io.BytesIO(b"%PDF-1.7 data"), "application/pdf")},
                )

            first, second = upload(), upload()

            assert first.status_code == 200, first.text  # type: ignore[attr-defined]
            assert second.status_code == 200, second.text  # type: ignore[attr-defined]
            assert storage_keys[0] != storage_keys[1]
            assert first.json()["s3_uri"] != second.json()["s3_uri"]  # type: ignore[attr-defined]
            assert first.json()["file_id"] != second.json()["file_id"]  # type: ignore[attr-defined]


def _zip_bytes(*member_names: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name in member_names:
            archive.writestr(name, b"x")
    return buffer.getvalue()


def _ole_bytes(*entry_names: str, body: bytes = b"") -> bytes:
    """A compound file whose directory sector sits past the sniff window, after ``body``."""
    entries = b""
    for name in entry_names:
        encoded = (name + "\0").encode("utf-16-le")
        entries += encoded.ljust(64, b"\x00") + len(encoded).to_bytes(2, "little") + b"\x02".ljust(62, b"\x00")
    header = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1".ljust(512, b"\x00")
    return header + body.ljust(16 * 1024, b"\x00") + entries


_PE_HEAD = b"MZ" + b"\x90" * 58 + (64).to_bytes(4, "little") + b"PE\x00\x00" + b"\x00" * 64
_OOXML_MEMBERS = ("[Content_Types].xml", "word/document.xml")
_PNG_HEAD = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
_TIFF_HEAD = b"II*\x00" + b"\x00" * 20
_HEIC_HEAD = b"\x00\x00\x00\x18ftypheic" + b"\x00" * 20
_MP4_HEAD = b"\x00\x00\x00\x18ftypisom" + b"\x00" * 20
_WEBM_HEAD = b"\x1a\x45\xdf\xa3" + b"\x00" * 20
_OCTET = "application/octet-stream"
_ODF_MEMBERS = ("mimetype", "META-INF/manifest.xml", "content.xml")
_SPREADSHEETML = (
    b'<?xml version="1.0"?>\n<?mso-application progid="Excel.Sheet"?>\n'
    b'<Workbook xmlns="urn:schemas-microsoft-com:office:spreadsheet"></Workbook>'
)
_WORD_2003_XML = (
    b'<?xml version="1.0"?>\n<?mso-application progid="Word.Document"?>\n'
    b'<w:wordDocument xmlns:w="http://schemas.microsoft.com/office/word/2003/wordml"></w:wordDocument>'
)


def _case(filename: str, content: bytes, expected_status: int, declared: str = _OCTET) -> tuple[str, bytes, str, int]:
    return filename, content, declared, expected_status


class TestUploadTypeRestrictions:
    """The allowlist refuses anything outside the expected types; the denylist and sniffing then refuse
    executables, scripts, browser-renderable markup and mislabeled content inside it."""

    @pytest.mark.parametrize(
        ("filename", "content", "declared", "expected_status"),
        [
            # Gate 1: allowlist by extension, declared type and (for extensionless files) sniffed type.
            _case("report.weirdext", bytes(range(256)) * 16, 415),
            _case("no_extension", b"plain bytes", 415),
            _case("backup.7z", b"7z\xbc\xaf\x27\x1c" + b"\x00" * 20, 415),
            _case("bundle.tar.gz", b"\x1f\x8b\x08" + b"\x00" * 20, 415),
            _case("tool.exe", _PE_HEAD, 415),
            _case("invoice.pdf.exe", b"%PDF-1.7", 415),
            _case("setup.EXE. ", b"anything", 415),
            _case("page.html", b"hello", 415),
            _case("page.shtml", b"hello", 415),
            _case("macro.docm", _zip_bytes(*_OOXML_MEMBERS), 415),
            _case("script.py", b"print(1)", 415),
            _case("shortcut.lnk", b"anything", 415),
            _case("notes.txt", b"hello", 415, declared="text/html"),
            _case("logo.png", _PNG_HEAD, 415, declared="image/svg+xml"),
            _case("data.csv", b"a,b\n", 415, declared="application/javascript"),
            # Gate 2: content sniffing inside the allowlist.
            _case("report.txt", _PE_HEAD, 415),
            _case("notes.txt", b"\xef\xbb\xbf  <!DOCTYPE html><html></html>", 415),
            _case("logo.png", b'<?xml version="1.0"?>\n<svg xmlns="http://www.w3.org/2000/svg"></svg>', 415),
            _case("run.txt", b"#!/bin/sh\nrm -rf /", 415),
            _case("saved.txt", b"<!-- saved from url -->\n<!--x--><html><body></body></html>", 415),
            _case("icon.xml", b'<?xml version="1.0"?><?xml-stylesheet href="a.css"?><!--c--><svg></svg>', 415),
            _case("utf16.txt", "\ufeff<html><script></script></html>".encode("utf-16-le"), 415),
            _case("padded.txt", b" " * 9000 + b"<html></html>", 415),
            _case("comment.txt", b"<!--" + b"x" * 9000 + b"--><svg></svg>", 415),
            _case("split_tag.txt", b" " * 8185 + b"<script>alert(1)</script>", 415),
            _case("split_comment.txt", b" " * 8189 + b"<!--x--><script></script>", 415),
            _case("split_doctype.txt", b" " * 8180 + b"<!doctype    html><p></p>", 415),
            _case("saved.txt", b"MIME-Version: 1.0\r\nContent-Type: multipart/related; boundary=x", 415),
            _case("binary.txt", b"\x7fELF\x02\x01\x01", 415),
            _case("invoice.txt", b"L\x00\x00\x00\x01\x14\x02\x00\x00\x00\x00\x00\xc0\x00\x00\x00\x00\x00\x00F", 415),
            # Gate 2: an allowed extension whose content does not carry the format's signature.
            _case("statement.pdf", b"<html>not a pdf</html>", 415),
            _case("statement.pdf", b"plain text", 415),
            _case("photo.jpg", b"not an image", 415),
            _case("scan.tif", b"not an image", 415),
            _case("movie.mp4", b"not a video", 415),
            _case("letter.odt", b"plain text", 415),
            _case("memo.rtf", b"plain text", 415),
            _case("renamed.docx", _ole_bytes("Root Entry", "WordDocument"), 415),
            _case("resume.docx", _zip_bytes("notes.txt"), 415),
            _case("letter.odt", _zip_bytes("notes.txt"), 415),
            _case("broken.odt", b"PK\x03\x04" + b"\x00" * 40, 415),
            # Gate 2: macro and package inspection of archives and compound files.
            _case("budget.xlsx", _zip_bytes("[Content_Types].xml", "xl/vbaProject.bin"), 415),
            _case("bundle.zip", _zip_bytes("META-INF/MANIFEST.MF", "com/example/Main.class"), 415),
            _case("bundle.zip", _zip_bytes("AndroidManifest.xml", "classes.dex"), 415),
            _case("legacy.doc", _ole_bytes("Root Entry", "WordDocument", "Macros", "VBA", "_VBA_PROJECT"), 415),
            _case("ledger.xls", _ole_bytes("Workbook", "_VBA_PROJECT_CUR", "VBA", "_VBA_PROJECT"), 415),
            _case("letter.odt", _zip_bytes(*_ODF_MEMBERS, "Basic/Standard/Module1.xml"), 415),
            _case("sheet.ods", _zip_bytes(*_ODF_MEMBERS, "Scripts/python/macro.py"), 415),
            _case("no_mimetype.odt", _zip_bytes("META-INF/manifest.xml", "Basic/Standard/Module1.xml"), 415),
            # Formats without a positive signature check still get the executable sniff.
            _case("ledger.xls", _PE_HEAD, 415),
            _case("legacy.doc", b"\x7fELF\x02\x01\x01", 415),
            _case("deck.ppt", b"#!/bin/sh\necho", 415),
            _case("movie.mp4", _PE_HEAD, 415),
            # Accepted: expected types whose content agrees with the name.
            _case("statement.pdf", b"%PDF-1.7\n%binary", 200),
            _case("no_extension_pdf", b"%PDF-1.7\n%binary", 200),
            _case("no_extension_png", _PNG_HEAD, 200),
            _case("letter.docx", _zip_bytes(*_OOXML_MEMBERS), 200),
            _case("locked.docx", _ole_bytes("Root Entry", "EncryptedInfo", "EncryptedPackage"), 200),
            _case("old.doc", _ole_bytes("Root Entry", "WordDocument"), 200),
            _case("old.doc", b"{\\rtf1\\ansi saved as .doc}", 200),
            _case("memo.rtf", b"{\\rtf1\\ansi hello}", 200),
            _case("letter.odt", _zip_bytes("mimetype", "content.xml"), 200),
            _case("ledger.xls", _ole_bytes("Workbook", body=b"\x00" + "_VBA_PROJECT\0".encode("utf-16-le")), 200),
            _case("data.csv", b"name,email\nada,ada@example.com\n", 200),
            _case("data.csv", b"name,email\n", 200, declared="application/vnd.ms-excel"),
            _case("utf16.csv", "\ufeffname,email\n".encode("utf-16-le"), 200),
            _case("report.txt", b"hello", 200, declared="text/plain"),
            _case("readme.md", b"# Title\n<p>inline html in markdown</p>", 200),
            _case("notes.md", b"<!-- generated -->\n# Title\n", 200),
            _case("indented.txt", b" " * 9000 + b"plain text", 200),
            _case("split_lt.txt", b" " * 8190 + b"<3 plain text", 200),
            _case("config.yaml", b"retries: 3\n", 200),
            _case("payload.json", b'{"a": 1}', 200, declared="application/json"),
            _case("forwarded.eml", b"MIME-Version: 1.0\r\nContent-Type: multipart/related; boundary=x", 200),
            _case("archive.zip", _zip_bytes("docs/readme.txt", "docs/tool.exe"), 200),
            _case("scan.tiff", _TIFF_HEAD, 200),
            _case("photo.heic", _HEIC_HEAD, 200),
            _case("movie.mp4", _MP4_HEAD, 200, declared="video/mp4"),
            _case("clip.webm", _WEBM_HEAD, 200),
            _case("legacy.mov", b"\x00\x00\x00\x08wide\x00\x00\x00\x10mdat" + b"\x00" * 20, 200),
            _case("fake.mov", _PNG_HEAD, 415),
            # Real-world variants of the legacy, media, RTF and image formats.
            _case("table.xls", b"<table><tr><td>1</td></tr></table>", 200),
            _case("export.xls", b"name\temail\nada\tada@example.com\n", 200),
            _case("workbook.xls", _SPREADSHEETML, 200),
            _case("renamed.xls", _zip_bytes("[Content_Types].xml", "xl/workbook.xml"), 200),
            _case("word2003.doc", _WORD_2003_XML, 200),
            _case("renamed.doc", _zip_bytes(*_OOXML_MEMBERS), 200),
            _case("deck.ppt", b"plain text", 200),
            _case("moov_first.mp4", b"\x00\x00\x00\x08moov" + b"\x00" * 20, 200),
            _case("free_first.mp4", b"\x00\x00\x00\x08free" + b"\x00" * 20, 200),
            _case("wide_first.mp4", b"\x00\x00\x00\x08wide" + b"\x00" * 20, 200),
            _case("bom.rtf", b"\xef\xbb\xbf{\\rtf1\\ansi hello}", 200),
            _case("crlf.rtf", b"\r\n{\\rtf1\\ansi hello}", 200),
            _case("heix.heic", b"\x00\x00\x00\x18ftypheix" + b"\x00" * 20, 200),
            _case("mif1.heic", b"\x00\x00\x00\x18ftypmif1" + b"\x00" * 20, 200),
            _case("big.tif", b"II+\x00\x08\x00\x00\x00" + b"\x00" * 20, 200),
            _case(
                "no_macros.odt", _zip_bytes(*_ODF_MEMBERS, "Basic/script-lc.xml", "Basic/Standard/script-lb.xml"), 200
            ),
            # Common non-standard declared types.
            _case("photo.jpg", b"\xff\xd8\xff\xe0" + b"\x00" * 20, 200, declared="image/jpg"),
            _case("data.csv", b"a,b\n", 200, declared="text/x-csv"),
            _case("data.csv", b"a,b\n", 200, declared="application/x-csv"),
            _case("archive.zip", _zip_bytes("docs/readme.txt"), 200, declared="multipart/x-zip"),
            _case("clip.mov", _MP4_HEAD, 200, declared="video/x-quicktime"),
            _case("memo.rtf", b"{\\rtf1 hi}", 200, declared="application/x-rtf"),
            _case("scan.tif", _TIFF_HEAD, 200, declared="image/x-tiff"),
            _case("data.csv", b"a,b\n", 200, declared="application/x-msexcel"),
            _case("logo.png", _PNG_HEAD, 200, declared="image/x-png"),
            _case("ledger.xls", _ole_bytes("Workbook"), 200, declared="application/x-excel"),
            # Audio inputs a run may attach, and benign text and image types.
            _case("song.mp3", b"ID3\x03" + b"\x00" * 20, 200, declared="audio/mpeg"),
            _case("memo.wav", b"RIFF\x00\x00\x00\x00WAVE" + b"\x00" * 20, 200),
            _case("memo.wav", b"RIFF\x00\x00\x00\x00WAVE" + b"\x00" * 20, 200, declared="audio/vnd.wave"),
            _case("clip.ogg", b"OggS\x00\x02" + b"\x00" * 20, 200, declared="application/ogg"),
            _case("meeting.ics", b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nEND:VCALENDAR\r\n", 200, declared="text/calendar"),
            _case("contact.vcf", b"BEGIN:VCARD\r\nVERSION:3.0\r\nEND:VCARD\r\n", 200),
            _case("server.log", b"2026-01-01 started\n", 200, declared="text/plain"),
            _case("photo.avif", b"\x00\x00\x00\x1cftypavif" + b"\x00" * 20, 200),
            _case("photo.jfif", b"\xff\xd8\xff\xe0\x00\x10JFIF" + b"\x00" * 20, 200),
            _case("invite.ics", b"<html><script>alert(1)</script></html>", 415),
            _case("song.mp3", _PE_HEAD, 415),
        ],
        ids=lambda value: value if isinstance(value, str) and "/" not in value else None,
    )
    def test_upload_type_is_enforced_at_the_upload_route(
        self, filename: str, content: bytes, declared: str, expected_status: int, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with (
            patch("skyvern.forge.sdk.routes.agent_protocol.app") as app_module,
            patch("skyvern.services.uploaded_file_service.app", app_module),
            patch(
                "skyvern.forge.sdk.services.org_auth_service.get_current_org_cached",
                new=AsyncMock(return_value=_make_org(VICTIM_ORG_ID)),
            ),
        ):
            app_module.SETTINGS_MANAGER.MAX_UPLOAD_FILE_SIZE = settings.MAX_UPLOAD_FILE_SIZE

            async def fake_create_uploaded_file(**kwargs: object) -> UploadedFile:
                now = datetime.now(timezone.utc)
                return UploadedFile(created_at=now, modified_at=now, **kwargs)  # type: ignore[arg-type]

            app_module.STORAGE.save_legacy_file = AsyncMock(
                return_value=("https://presigned.example/x", uploaded_file_uri(VICTIM_ORG_ID, "stored"))
            )
            app_module.DATABASE.uploaded_files.create_uploaded_file = AsyncMock(side_effect=fake_create_uploaded_file)

            resp = _client(monkeypatch).post(
                "/v1/upload_file",
                headers={"x-api-key": "key"},
                files={"file": (filename, io.BytesIO(content), declared)},
            )

            assert resp.status_code == expected_status, resp.text
            if expected_status == 415:
                assert resp.json()["detail"].startswith("File type not allowed: ")
            assert app_module.STORAGE.save_legacy_file.await_count == (0 if expected_status == 415 else 1)

    @pytest.mark.parametrize(
        ("filename", "content"),
        [("sop.docx", _zip_bytes(*_OOXML_MEMBERS)), ("sop.png", _PNG_HEAD)],
    )
    def test_pdf_import_refuses_types_the_generic_route_takes(
        self, filename: str, content: bytes, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with (
            patch("skyvern.forge.sdk.routes.agent_protocol.app") as app_module,
            patch(
                "skyvern.forge.sdk.services.org_auth_service.get_current_org_cached",
                new=AsyncMock(return_value=_make_org(VICTIM_ORG_ID)),
            ),
        ):
            app_module.SETTINGS_MANAGER.MAX_UPLOAD_FILE_SIZE = settings.MAX_UPLOAD_FILE_SIZE

            with capture_logs() as logs:
                resp = _client(monkeypatch).post(
                    "/api/v1/workflows/import-pdf",
                    headers={"x-api-key": "key"},
                    files={"file": (filename, io.BytesIO(content), _OCTET)},
                )

            assert resp.status_code == 415, resp.text
            assert (
                resp.json()["detail"] == f"File type not allowed: .{filename.rsplit('.', 1)[1]} files are not allowed"
            )
            [rejection] = [log for log in logs if log["event"] == "Rejected upload by file type"]
            assert rejection["purpose"] == "import_pdf"
            assert rejection["organization_id"] == VICTIM_ORG_ID

    @pytest.mark.parametrize(
        ("filename", "content", "declared", "expected_reason"),
        [
            ("dictation.webm", _MP4_HEAD, "", None),
            ("dictation.mp4", _MP4_HEAD, "", None),
            ("dictation.webm", b"OggS\x00\x02" + b"\x00" * 20, "", None),
            ("dictation.webm", b"RIFF\x00\x00\x00\x00WAVE" + b"\x00" * 20, "", None),
            ("dictation.webm", b"ID3\x03" + b"\x00" * 20, "", None),
            ("dictation.webm", b"fLaC" + b"\x00" * 20, "", None),
            ("dictation.mp3", b"ID3\x03" + b"\x00" * 20, "", None),
            ("dictation.m4a", b"\x00\x00\x00\x08free" + b"\x00" * 20, "", None),
            ("dictation.webm", _PNG_HEAD, "", "content does not match .webm"),
            ("dictation.pdf", _MP4_HEAD, "", ".pdf files are not allowed"),
        ],
    )
    def test_voice_notes_take_only_audio_types(
        self, filename: str, content: bytes, declared: str, expected_reason: str | None
    ) -> None:
        """Browsers name every dictation .webm whatever container they record into, so the check is by family."""
        file = UploadFile(file=io.BytesIO(content), filename=filename, headers=Headers({"content-type": declared}))
        reason = uploaded_file_service._upload_type_rejection_reason(
            file, uploaded_file_service.VOICE_NOTE_UPLOAD_EXTENSIONS, any_audio_container=True
        )
        assert reason == expected_reason

    def test_upload_rejection_runs_after_auth_and_names_the_organization(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Unauthenticated callers never reach the content scan, and a refusal says who was refused where."""
        with patch("skyvern.forge.sdk.routes.agent_protocol.app") as app_module:
            app_module.SETTINGS_MANAGER.MAX_UPLOAD_FILE_SIZE = settings.MAX_UPLOAD_FILE_SIZE
            client = _client(monkeypatch)
            with patch.object(uploaded_file_service, "_upload_type_rejection_reason") as scan:
                resp = client.post("/v1/upload_file", files={"file": ("tool.exe", io.BytesIO(_PE_HEAD), _OCTET)})
            assert resp.status_code in (401, 403), resp.text
            scan.assert_not_called()

            with (
                patch(
                    "skyvern.forge.sdk.services.org_auth_service.get_current_org_cached",
                    new=AsyncMock(return_value=_make_org(VICTIM_ORG_ID)),
                ),
                capture_logs() as logs,
            ):
                resp = client.post(
                    "/v1/upload_file",
                    headers={"x-api-key": "key"},
                    files={"file": ("backup.7z", io.BytesIO(b"7z"), _OCTET)},
                )
            assert resp.status_code == 415, resp.text
            [rejection] = [log for log in logs if log["event"] == "Rejected upload by file type"]
            assert rejection["organization_id"] == VICTIM_ORG_ID
            assert rejection["purpose"] == "upload_file"

    @pytest.mark.parametrize(
        ("extra", "filename", "content", "declared", "expected_reason"),
        [
            ([".ORG"], "todo.org", b"* TODO", "text/plain", None),
            (["gz"], "bundle.gz", b"\x1f\x8b\x08" + b"\x00" * 20, "application/gzip", None),
            ([], "todo.org", b"* TODO", "text/plain", ".org files are not allowed"),
            (["exe"], "tool.exe", _PE_HEAD, "text/plain", ".exe files are not allowed"),
            # Listing a built-in extension also reopens a declared-type refusal for it.
            ([], "memo.rtf", b"{\\rtf1 hi}", "application/x-unknown-rtf", "declared type"),
            (["rtf"], "memo.rtf", b"{\\rtf1 hi}", "application/x-unknown-rtf", None),
        ],
    )
    def test_operators_can_widen_the_generic_allowlist_but_not_past_the_denylist(
        self,
        extra: list[str],
        filename: str,
        content: bytes,
        declared: str,
        expected_reason: str | None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Through the public entry point, so the generic route's default allowlist is the one exercised."""
        monkeypatch.setattr(settings, "UPLOAD_EXTRA_ALLOWED_EXTENSIONS", extra)
        file = UploadFile(file=io.BytesIO(content), filename=filename, headers=Headers({"content-type": declared}))
        if expected_reason is None:
            uploaded_file_service.validate_upload_type(file, purpose="upload_file")
            return
        with pytest.raises(HTTPException) as refused:
            uploaded_file_service.validate_upload_type(file, purpose="upload_file")
        assert refused.value.status_code == 415
        assert expected_reason in refused.value.detail

    def test_operator_extensions_do_not_loosen_other_routes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "UPLOAD_EXTRA_ALLOWED_EXTENSIONS", ["pdf"])
        file = UploadFile(
            file=io.BytesIO(b"%PDF-1.7"), filename="sop.pdf", headers=Headers({"content-type": "image/png"})
        )
        with pytest.raises(HTTPException) as refused:
            uploaded_file_service.validate_upload_type(
                file, purpose="import_pdf", allowed=uploaded_file_service.PDF_UPLOAD_EXTENSIONS
            )
        assert "declared type image/png" in refused.value.detail

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("filename", "content", "logged_reason"),
        [
            ("capture.har", b'{"log": {}}', None),
            ("setup.exe", b"not really", ".exe files are not allowed"),
        ],
    )
    async def test_passthrough_logs_only_the_denylist(
        self, filename: str, content: bytes, logged_reason: str | None
    ) -> None:
        """A third-party site may ask for any type, so pass-through files skip the allowlist but not the denylist."""
        file = UploadFile(file=io.BytesIO(content), filename=filename, headers=Headers({"content-type": _OCTET}))
        with capture_logs() as logs:
            await uploaded_file_service.validate_passthrough_upload(file, max_size_bytes=None, purpose="test")
        reasons = [log["reason"] for log in logs if log["event"] == "Accepted a restricted file type for pass-through"]
        assert reasons == ([logged_reason] if logged_reason else [])

    def test_every_route_allowlist_is_inside_the_declared_type_table(self) -> None:
        """A route set outside the table would skip the declared-type check for those extensions."""
        for allowed in (
            uploaded_file_service.ALLOWED_UPLOAD_EXTENSIONS,
            uploaded_file_service.PDF_UPLOAD_EXTENSIONS,
            uploaded_file_service.RESUME_UPLOAD_EXTENSIONS,
            uploaded_file_service.VOICE_NOTE_UPLOAD_EXTENSIONS,
        ):
            assert allowed <= uploaded_file_service.DECLARED_UPLOAD_TYPES.keys()
        assert not uploaded_file_service.ALLOWED_UPLOAD_EXTENSIONS & uploaded_file_service.DENIED_UPLOAD_EXTENSIONS


class TestExpirySweep:
    @pytest.mark.asyncio
    async def test_only_files_whose_uploader_asked_for_an_expiry_are_purged(
        self, service_app: object, repo: FakeUploadedFilesRepository, storage: FakeStorage
    ) -> None:
        """The sweep deletes irreversibly, so its reach is limited to data a caller marked."""
        past = datetime.now(timezone.utc) - timedelta(days=1)
        future = datetime.now(timezone.utc) + timedelta(days=1)
        expired = repo.seed(VICTIM_ORG_ID, expires_at=past, filename="expired.csv")
        not_yet_expired = repo.seed(VICTIM_ORG_ID, expires_at=future, filename="later.csv")
        no_expiry = repo.seed(VICTIM_ORG_ID, expires_at=None, filename="forever.csv")

        result = await uploaded_file_service.purge_expired_files()

        assert result == {"examined": 1, "deleted": 1, "failed": 0}
        assert storage.deleted == [uploaded_file_uri(VICTIM_ORG_ID, "expired.csv")]
        assert repo.live_ids() == {not_yet_expired, no_expiry}
        assert expired not in repo.live_ids()

    @pytest.mark.asyncio
    async def test_a_file_that_fails_to_delete_stays_expired_for_the_next_sweep(
        self, service_app: object, repo: FakeUploadedFilesRepository, storage: FakeStorage
    ) -> None:
        past = datetime.now(timezone.utc) - timedelta(days=1)
        file_id = repo.seed(VICTIM_ORG_ID, expires_at=past)
        storage.fail_with = RuntimeError("s3 is down")

        result = await uploaded_file_service.purge_expired_files()

        assert result == {"examined": 1, "deleted": 0, "failed": 1}
        assert repo.live_ids() == {file_id}

    @pytest.mark.asyncio
    async def test_a_row_whose_object_is_already_gone_is_still_retired(
        self, service_app: object, repo: FakeUploadedFilesRepository, storage: FakeStorage
    ) -> None:
        """The scrubber stamps expires_at on rows whose objects a retention cap already deleted.

        Every row it hands over is in exactly this state, so a sweep that required the object
        to still exist would leave all of them live and the file ids resolving to dead URIs.
        """
        past = datetime.now(timezone.utc) - timedelta(days=1)
        file_id = repo.seed(VICTIM_ORG_ID, expires_at=past, filename="lifecycle-expired.csv")
        storage.absent.add(uploaded_file_uri(VICTIM_ORG_ID, "lifecycle-expired.csv"))

        result = await uploaded_file_service.purge_expired_files()

        assert result == {"examined": 1, "deleted": 1, "failed": 0}
        assert storage.deleted == []
        assert file_id not in repo.live_ids()


class TestRunAttachment:
    """Attaching a file to a run (SKY-14439): the run's end is what deletes it."""

    @pytest.mark.asyncio
    async def test_a_runs_attached_files_are_deleted_when_the_run_ends(
        self, service_app: object, repo: FakeUploadedFilesRepository, storage: FakeStorage
    ) -> None:
        attached = repo.seed(VICTIM_ORG_ID, filename="cv.pdf")
        other_run = repo.seed(VICTIM_ORG_ID, filename="other.pdf", run_id="wr_other")
        unattached = repo.seed(VICTIM_ORG_ID, filename="kept.pdf")
        await uploaded_file_service.attach_files_to_run(
            file_ids=[attached], organization_id=VICTIM_ORG_ID, run_id="wr_1"
        )

        deleted = await uploaded_file_service.delete_files_attached_to_run(run_id="wr_1")

        assert deleted == 1
        assert storage.deleted == [uploaded_file_uri(VICTIM_ORG_ID, "cv.pdf")]
        assert repo.live_ids() == {other_run, unattached}

    @pytest.mark.asyncio
    async def test_an_attached_file_gets_a_backstop_expiry(
        self, service_app: object, repo: FakeUploadedFilesRepository
    ) -> None:
        """A run that never reaches its terminal handler must not strand the bytes forever."""
        file_id = repo.seed(VICTIM_ORG_ID, expires_at=None)

        await uploaded_file_service.attach_files_to_run(
            file_ids=[file_id], organization_id=VICTIM_ORG_ID, run_id="wr_1"
        )

        expires_at = repo.rows[file_id].expires_at
        assert expires_at is not None
        assert expires_at <= datetime.now(timezone.utc) + timedelta(hours=settings.RUN_ATTACHED_FILE_BACKSTOP_HOURS)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("attach_to_other_run", [True, False])
    async def test_a_file_another_run_is_using_cannot_be_attached(
        self, service_app: object, repo: FakeUploadedFilesRepository, attach_to_other_run: bool
    ) -> None:
        """Re-attaching would move the deletion trigger onto a run the first one is still using."""
        file_id = repo.seed(VICTIM_ORG_ID, run_id="wr_first" if attach_to_other_run else None)

        if attach_to_other_run:
            with pytest.raises(uploaded_file_service.FileNotAttachable):
                await uploaded_file_service.assert_files_attachable(file_ids=[file_id], organization_id=VICTIM_ORG_ID)
        else:
            await uploaded_file_service.assert_files_attachable(file_ids=[file_id], organization_id=VICTIM_ORG_ID)

    @pytest.mark.asyncio
    async def test_another_orgs_file_is_not_attachable(
        self, service_app: object, repo: FakeUploadedFilesRepository
    ) -> None:
        victim_file = repo.seed(VICTIM_ORG_ID)

        with pytest.raises(uploaded_file_service.FileNotAttachable):
            await uploaded_file_service.assert_files_attachable(file_ids=[victim_file], organization_id=ATTACKER_ORG_ID)

    @pytest.mark.asyncio
    async def test_a_task_v1_file_is_bound_before_the_run_is_dispatched(
        self, service_app: object, repo: FakeUploadedFilesRepository, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Binding after dispatch would let a fast run reach teardown with nothing to delete."""
        from skyvern.forge.sdk.schemas.tasks import TaskRequest
        from skyvern.services import task_v1_service

        file_id = repo.seed(VICTIM_ORG_ID)
        bound_at_dispatch: list[str | None] = []

        async def execute_task(**kwargs: object) -> None:
            bound_at_dispatch.append(repo.rows[file_id].run_id)

        monkeypatch.setattr(task_v1_service, "_validate_task_v1_model_for_org", AsyncMock())
        monkeypatch.setattr(task_v1_service, "validate_fetch_url", lambda url: url)
        monkeypatch.setattr(
            task_v1_service.app.agent, "create_task", AsyncMock(return_value=SimpleNamespace(task_id="tsk_1"))
        )
        monkeypatch.setattr(
            task_v1_service.app.AGENT_FUNCTION, "resolve_run_engine", AsyncMock(return_value=RunEngine.skyvern_v1)
        )
        monkeypatch.setattr(task_v1_service.app.DATABASE.tasks, "create_task_run", AsyncMock())
        monkeypatch.setattr(
            task_v1_service.AsyncExecutorFactory, "get_executor", lambda: SimpleNamespace(execute_task=execute_task)
        )

        await task_v1_service.run_task(
            TaskRequest(url="https://task.example.test"), _make_org(VICTIM_ORG_ID), file_ids=[file_id]
        )

        assert bound_at_dispatch == ["tsk_1"]

    @pytest.mark.asyncio
    async def test_teardown_deletion_does_not_raise_when_storage_fails(
        self, service_app: object, repo: FakeUploadedFilesRepository, storage: FakeStorage
    ) -> None:
        """An exception here would cost the run its webhook; the sweep retries via the backstop."""
        file_id = repo.seed(VICTIM_ORG_ID, run_id="wr_1")
        storage.fail_with = RuntimeError("s3 is down")

        assert await uploaded_file_service.delete_files_attached_to_run(run_id="wr_1") == 0
        assert repo.live_ids() == {file_id}


class TestFileIdAsFileReference:
    """A file id can stand in for a URL, so the run never needs a presigned URL."""

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("file_384430212391591428", True),
            ("file:///etc/passwd", False),
            ("https://example.com/cv.pdf", False),
            ("file_", False),
        ],
    )
    def test_only_a_file_id_is_treated_as_one(self, value: str, expected: bool) -> None:
        assert files_api.is_uploaded_file_id(value) is expected

    @pytest.mark.asyncio
    async def test_a_file_id_resolves_to_its_own_orgs_uri_and_no_one_elses(
        self, service_app: object, repo: FakeUploadedFilesRepository
    ) -> None:
        file_id = repo.seed(VICTIM_ORG_ID, filename="cv.pdf")

        assert await files_api.resolve_uploaded_file_id(file_id, VICTIM_ORG_ID) == uploaded_file_uri(
            VICTIM_ORG_ID, "cv.pdf"
        )
        with pytest.raises(FileNotFoundError):
            await files_api.resolve_uploaded_file_id(file_id, ATTACKER_ORG_ID)

    @pytest.mark.asyncio
    async def test_a_file_id_without_an_organization_is_refused(
        self, service_app: object, repo: FakeUploadedFilesRepository
    ) -> None:
        """Unauthenticated call sites must fail closed rather than resolve someone's file."""
        file_id = repo.seed(VICTIM_ORG_ID)

        with pytest.raises(PermissionError):
            await files_api.resolve_uploaded_file_id(file_id, None)


class TestStorageGuard:
    """The real backends' org-prefix check, not a stub of it.

    ``test_file_download_access_control`` substitutes a fake storage, so it asserts what the
    fake was told to do; these run the shipped implementations.
    """

    @pytest.mark.asyncio
    async def test_s3_refuses_to_delete_a_uri_outside_the_orgs_prefix(self) -> None:
        """Defense in depth: a row pointing at another tenant still cannot delete their object."""
        s3_storage = S3Storage()
        s3_storage.async_client = MagicMock(delete_file=AsyncMock())

        with pytest.raises(PermissionError):
            await s3_storage.delete_legacy_file(organization_id=ATTACKER_ORG_ID, uri=uploaded_file_uri(VICTIM_ORG_ID))

        s3_storage.async_client.delete_file.assert_not_awaited()

    def test_a_traversal_segment_does_not_satisfy_the_org_prefix(self) -> None:
        """`{env}/{attacker}/../{victim}/x` starts with the attacker's prefix as a raw string."""
        traversal = f"s3://{settings.AWS_S3_BUCKET_UPLOADS}/{settings.ENV}/{ATTACKER_ORG_ID}/../{VICTIM_ORG_ID}/x.pdf"

        with pytest.raises(PermissionError):
            S3Storage().assert_managed_file_access(traversal, ATTACKER_ORG_ID)

    def test_the_orgs_own_file_is_still_reachable(self) -> None:
        """The traversal guard must not reject ordinary keys."""
        S3Storage().assert_managed_file_access(uploaded_file_uri(ATTACKER_ORG_ID), ATTACKER_ORG_ID)
