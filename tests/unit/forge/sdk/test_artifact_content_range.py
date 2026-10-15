from __future__ import annotations

import asyncio
import contextlib
import io
import uuid
import zipfile
from datetime import datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import boto3
import pytest
import zstandard as zstd
from aiobotocore.response import StreamingBody
from fastapi import FastAPI
from fastapi.testclient import TestClient

from skyvern.config import settings
from skyvern.forge import app as forge_app
from skyvern.forge.sdk.artifact.manager import ArtifactManager
from skyvern.forge.sdk.artifact.models import Artifact, ArtifactType
from skyvern.forge.sdk.artifact.storage.s3 import S3Storage
from skyvern.forge.sdk.routes import agent_protocol
from skyvern.forge.sdk.routes import routers as routers_module
from skyvern.forge.sdk.routes.agent_protocol import _parse_range_header
from tests.unit.forge.sdk.artifact.storage.test_s3_storage_moto import moto_server  # noqa: F401

CONTENT = b"0123456789" * 100  # 1000 bytes for predictable slicing


def _build_client(monkeypatch: pytest.MonkeyPatch, *, content: bytes = CONTENT) -> TestClient:
    async def _fake_org(**_: object) -> SimpleNamespace:
        return SimpleNamespace(organization_id="org_oss")

    artifact = SimpleNamespace(
        artifact_id="a_42",
        artifact_type=ArtifactType.RECORDING,
        uri="s3://bucket/path/recording.webm",
        organization_id="org_oss",
    )

    artifacts_db = MagicMock()
    artifacts_db.get_artifact_by_id = AsyncMock(return_value=artifact)
    artifacts_db.get_artifact_by_id_no_org = AsyncMock(return_value=artifact)
    monkeypatch.setattr(forge_app.DATABASE, "artifacts", artifacts_db)
    monkeypatch.setattr(
        forge_app.ARTIFACT_MANAGER,
        "retrieve_artifact",
        AsyncMock(return_value=content),
    )
    # The endpoint calls get_current_org directly (not via Depends), so patch
    # the symbol on the route module rather than the service module.
    from skyvern.forge.sdk.routes import agent_protocol

    monkeypatch.setattr(agent_protocol.org_auth_service, "get_current_org", _fake_org)

    fastapi_app = FastAPI()
    fastapi_app.include_router(routers_module.base_router, prefix="/v1")
    return TestClient(fastapi_app)


class TestParseRangeHeader:
    def test_none_header_returns_none(self) -> None:
        assert _parse_range_header(None, 1000) is None

    def test_empty_header_returns_none(self) -> None:
        assert _parse_range_header("", 1000) is None

    def test_non_bytes_unit_returns_none(self) -> None:
        assert _parse_range_header("items=0-99", 1000) is None

    def test_full_range(self) -> None:
        assert _parse_range_header("bytes=0-99", 1000) == (0, 99)

    def test_open_ended_range(self) -> None:
        assert _parse_range_header("bytes=500-", 1000) == (500, 999)

    def test_open_ended_range_from_zero(self) -> None:
        assert _parse_range_header("bytes=0-", 1000) == (0, 999)

    def test_suffix_range_last_n_bytes(self) -> None:
        assert _parse_range_header("bytes=-100", 1000) == (900, 999)

    def test_suffix_range_larger_than_content_clamps_to_zero(self) -> None:
        assert _parse_range_header("bytes=-5000", 1000) == (0, 999)

    def test_end_beyond_content_is_clamped(self) -> None:
        assert _parse_range_header("bytes=900-9999", 1000) == (900, 999)

    def test_unsatisfiable_start_returns_sentinel(self) -> None:
        assert _parse_range_header("bytes=2000-3000", 1000) == (-1, -1)

    def test_inverted_range_returns_sentinel(self) -> None:
        assert _parse_range_header("bytes=500-100", 1000) == (-1, -1)

    def test_multipart_range_returns_none(self) -> None:
        # Multipart ranges (RFC 7233 §4.1) are valid but we don't implement them.
        assert _parse_range_header("bytes=0-100,200-300", 1000) is None

    def test_malformed_returns_none(self) -> None:
        assert _parse_range_header("bytes=abc", 1000) is None
        assert _parse_range_header("bytes=", 1000) is None
        assert _parse_range_header("bytes=10-abc", 1000) is None

    def test_zero_length_content(self) -> None:
        assert _parse_range_header("bytes=0-0", 0) == (-1, -1)

    def test_suffix_zero_returns_none(self) -> None:
        assert _parse_range_header("bytes=-0", 1000) is None

    def test_negative_position_treated_as_malformed(self) -> None:
        # "0--1" parses to start=0, end=-1; per RFC 7233 §3.1 malformed Range
        # headers must be ignored, not treated as unsatisfiable.
        assert _parse_range_header("bytes=0--1", 1000) is None
        assert _parse_range_header("bytes=-1-100", 1000) is None
        assert _parse_range_header("bytes=+5-100", 1000) is None

    def test_non_ascii_digits_rejected(self) -> None:
        # RFC 5234 byte-pos is ASCII DIGIT only; Unicode decimal digits like
        # Devanagari (० १) pass str.isdigit() but must not be accepted.
        assert _parse_range_header("bytes=०-१", 1000) is None
        assert _parse_range_header("bytes=0-१", 1000) is None
        assert _parse_range_header("bytes=-१", 1000) is None

    def test_case_insensitive_unit(self) -> None:
        # HTTP field tokens are case-insensitive (RFC 7230 §3.2.6).
        assert _parse_range_header("Bytes=0-99", 1000) == (0, 99)
        assert _parse_range_header("BYTES=500-", 1000) == (500, 999)

    def test_leading_whitespace_tolerated(self) -> None:
        assert _parse_range_header("  bytes=0-99", 1000) == (0, 99)


class TestArtifactContentRangeRequests:
    def test_no_range_returns_full_content_with_accept_ranges(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _build_client(monkeypatch)
        response = client.get("/v1/artifacts/a_42/content")

        assert response.status_code == 200
        assert response.content == CONTENT
        assert response.headers["accept-ranges"] == "bytes"
        assert "content-range" not in {k.lower() for k in response.headers}

    def test_range_request_returns_206_partial_content(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _build_client(monkeypatch)
        response = client.get("/v1/artifacts/a_42/content", headers={"Range": "bytes=10-19"})

        assert response.status_code == 206
        assert response.content == CONTENT[10:20]
        assert response.headers["content-range"] == f"bytes 10-19/{len(CONTENT)}"
        assert response.headers["accept-ranges"] == "bytes"

    def test_open_ended_range(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _build_client(monkeypatch)
        response = client.get("/v1/artifacts/a_42/content", headers={"Range": "bytes=900-"})

        assert response.status_code == 206
        assert response.content == CONTENT[900:]
        assert response.headers["content-range"] == f"bytes 900-999/{len(CONTENT)}"

    def test_suffix_range(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _build_client(monkeypatch)
        response = client.get("/v1/artifacts/a_42/content", headers={"Range": "bytes=-50"})

        assert response.status_code == 206
        assert response.content == CONTENT[-50:]
        assert response.headers["content-range"] == f"bytes 950-999/{len(CONTENT)}"

    def test_unsatisfiable_range_returns_416(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _build_client(monkeypatch)
        response = client.get("/v1/artifacts/a_42/content", headers={"Range": "bytes=5000-6000"})

        assert response.status_code == 416
        assert response.headers["content-range"] == f"bytes */{len(CONTENT)}"

    def test_invalid_range_falls_through_to_200(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Per RFC 7233 §3.1, unparseable Range headers MUST be ignored.
        client = _build_client(monkeypatch)
        response = client.get("/v1/artifacts/a_42/content", headers={"Range": "bytes=garbage"})

        assert response.status_code == 200
        assert response.content == CONTENT
        assert response.headers["accept-ranges"] == "bytes"


STREAM_BUCKET = "artifact-content-route"


@pytest.fixture
def stream_bucket(moto_server: str, no_env_proxy: None) -> Any:  # noqa: F811
    client = boto3.client(
        "s3",
        aws_access_key_id="testing",
        aws_secret_access_key="testing",
        region_name=settings.AWS_REGION,
        endpoint_url=moto_server,
    )
    with contextlib.suppress(client.exceptions.BucketAlreadyOwnedByYou):
        client.create_bucket(Bucket=STREAM_BUCKET)
    return client


@pytest.fixture
def s3_backed_storage(
    monkeypatch: pytest.MonkeyPatch,
    moto_server: str,  # noqa: F811
    stream_bucket: Any,
) -> S3Storage:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.delenv("AWS_SESSION_TOKEN", raising=False)
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    storage = S3Storage(bucket=STREAM_BUCKET, endpoint_url=moto_server)
    monkeypatch.setattr(forge_app, "STORAGE", storage)
    monkeypatch.setattr(forge_app, "ARTIFACT_MANAGER", ArtifactManager())
    return storage


@pytest.fixture
def storage_bodies(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Every read of an S3 response body as (requested amount, bytes returned), and how many bodies were closed."""
    observed = SimpleNamespace(reads=[], closes=0)
    real_read = StreamingBody.read

    async def read(self: StreamingBody, amt: int | None = None) -> bytes:
        chunk = await real_read(self, amt)
        observed.reads.append((amt, len(chunk)))
        return chunk

    def close(self: StreamingBody) -> None:
        observed.closes += 1
        self.__wrapped__.close()

    monkeypatch.setattr(StreamingBody, "read", read)
    monkeypatch.setattr(StreamingBody, "close", close, raising=False)
    return observed


def _store_artifact(
    s3: Any,
    data: bytes,
    *,
    artifact_type: ArtifactType = ArtifactType.DOWNLOAD,
    filename: str = "report.zip",
    bundle_key: str | None = None,
) -> Artifact:
    key = f"objects/{uuid.uuid4().hex}/{filename}"
    s3.put_object(Bucket=STREAM_BUCKET, Key=key, Body=data)
    now = datetime.utcnow()
    return Artifact(
        artifact_id="a_stored",
        artifact_type=artifact_type,
        uri=f"s3://{STREAM_BUCKET}/{key}",
        bundle_key=bundle_key,
        organization_id="org_oss",
        created_at=now,
        modified_at=now,
    )


def _artifact_route_app(monkeypatch: pytest.MonkeyPatch, artifact: Artifact) -> FastAPI:
    async def _fake_org(**_: object) -> SimpleNamespace:
        return SimpleNamespace(organization_id="org_oss")

    artifacts_db = MagicMock()
    artifacts_db.get_artifact_by_id = AsyncMock(return_value=artifact)
    monkeypatch.setattr(forge_app.DATABASE, "artifacts", artifacts_db)
    monkeypatch.setattr(agent_protocol.org_auth_service, "get_current_org", _fake_org)
    fastapi_app = FastAPI()
    fastapi_app.include_router(routers_module.base_router, prefix="/v1")
    return fastapi_app


def _asgi_get_scope(path: str) -> dict[str, Any]:
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": b"",
        "headers": [(b"host", b"testserver"), (b"x-api-key", b"synthetic")],
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
    }


class TestArtifactContentFromS3:
    @pytest.mark.parametrize(
        ("range_header", "status", "body", "content_range"),
        [
            pytest.param(None, 200, CONTENT, None, id="full"),
            pytest.param("bytes=10-19", 206, CONTENT[10:20], "bytes 10-19/1000", id="range"),
            pytest.param("bytes=900-", 206, CONTENT[900:], "bytes 900-999/1000", id="open-ended"),
            pytest.param("bytes=-50", 206, CONTENT[-50:], "bytes 950-999/1000", id="suffix"),
            pytest.param("bytes=990-5000", 206, CONTENT[990:], "bytes 990-999/1000", id="end-clamped"),
            pytest.param("bytes=5000-6000", 416, b"", "bytes */1000", id="unsatisfiable"),
            pytest.param("bytes=garbage", 200, CONTENT, None, id="ignored"),
        ],
    )
    def test_plain_object_is_read_in_bounded_chunks_with_unchanged_range_semantics(
        self,
        monkeypatch: pytest.MonkeyPatch,
        s3_backed_storage: S3Storage,
        stream_bucket: Any,
        storage_bodies: SimpleNamespace,
        range_header: str | None,
        status: int,
        body: bytes,
        content_range: str | None,
    ) -> None:
        artifact = _store_artifact(stream_bucket, CONTENT)
        with TestClient(_artifact_route_app(monkeypatch, artifact)) as client:
            response = client.get(
                "/v1/artifacts/a_stored/content", headers={"Range": range_header} if range_header else {}
            )

        assert response.status_code == status
        assert response.content == body
        assert response.headers.get("content-range") == content_range
        assert response.headers["content-length"] == str(len(body))
        assert response.headers["accept-ranges"] == "bytes"
        assert response.headers["content-disposition"].startswith("attachment;")
        # GiB-sized downloads killed API tasks: no read may be unbounded, and a slice reads only its bytes.
        assert all(amount is not None for amount, _ in storage_bodies.reads)
        assert sum(size for _, size in storage_bodies.reads) == len(body)

    @pytest.mark.parametrize("range_header", [None, "bytes=5-14"])
    @pytest.mark.parametrize("kind", ["bundle-member", "compressed-har"])
    def test_decoded_artifacts_keep_their_whole_object_semantics(
        self,
        monkeypatch: pytest.MonkeyPatch,
        s3_backed_storage: S3Storage,
        stream_bucket: Any,
        kind: str,
        range_header: str | None,
    ) -> None:
        if kind == "bundle-member":
            served = b"<html><body>bundled member page</body></html>"
            archive = io.BytesIO()
            with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
                zf.writestr("scrape.html", served)
                zf.writestr("other.txt", b"another member")
            artifact = _store_artifact(
                stream_bucket,
                archive.getvalue(),
                artifact_type=ArtifactType.HTML_SCRAPE,
                filename="step_archive.zip",
                bundle_key="scrape.html",
            )
        else:
            served = b'{"log": {"version": "1.2", "entries": []}}' * 20
            artifact = _store_artifact(
                stream_bucket,
                zstd.ZstdCompressor(level=3).compress(served),
                artifact_type=ArtifactType.HAR,
                filename="network.har.zst",
            )

        with TestClient(_artifact_route_app(monkeypatch, artifact)) as client:
            response = client.get(
                "/v1/artifacts/a_stored/content", headers={"Range": range_header} if range_header else {}
            )

        if range_header is None:
            assert response.status_code == 200
            assert response.content == served
        else:
            assert response.status_code == 206
            assert response.content == served[5:15]
            assert response.headers["content-range"] == f"bytes 5-14/{len(served)}"

    def test_range_read_of_an_object_replaced_after_sizing_serves_one_version(
        self,
        monkeypatch: pytest.MonkeyPatch,
        s3_backed_storage: S3Storage,
        stream_bucket: Any,
    ) -> None:
        artifact = _store_artifact(stream_bucket, CONTENT)
        replacement = b"abcdefghij" * 300
        sized_versions: list[Any] = []
        stat_artifact = s3_backed_storage.stat_artifact

        async def stat_then_replace(stored: Artifact) -> Any:
            version = await stat_artifact(stored)
            if not sized_versions:
                key = stored.uri.removeprefix(f"s3://{STREAM_BUCKET}/")
                stream_bucket.put_object(Bucket=STREAM_BUCKET, Key=key, Body=replacement)
            sized_versions.append(version)
            return version

        monkeypatch.setattr(s3_backed_storage, "stat_artifact", stat_then_replace)
        with TestClient(_artifact_route_app(monkeypatch, artifact)) as client:
            response = client.get("/v1/artifacts/a_stored/content", headers={"Range": "bytes=0-9"})

        assert response.status_code == 206
        assert response.content == replacement[:10]
        assert response.headers["content-range"] == f"bytes 0-9/{len(replacement)}"
        assert [version.size for version in sized_versions] == [len(CONTENT), len(replacement)]

    @pytest.mark.asyncio
    async def test_client_disconnect_stops_reading_and_closes_the_storage_body(
        self,
        monkeypatch: pytest.MonkeyPatch,
        s3_backed_storage: S3Storage,
        stream_bucket: Any,
        storage_bodies: SimpleNamespace,
    ) -> None:
        large = bytes(range(256)) * 32 * 1024  # 8 MiB
        fastapi_app = _artifact_route_app(monkeypatch, _store_artifact(stream_bucket, large))
        first_body_sent = asyncio.Event()
        sent: list[dict[str, Any]] = []
        request_delivered = False

        async def receive() -> dict[str, Any]:
            nonlocal request_delivered
            if not request_delivered:
                request_delivered = True
                return {"type": "http.request", "body": b"", "more_body": False}
            await first_body_sent.wait()
            return {"type": "http.disconnect"}

        async def send(message: dict[str, Any]) -> None:
            sent.append(message)
            if message["type"] == "http.response.body" and message.get("body"):
                first_body_sent.set()
            # A served send is an await point (BaseHTTPMiddleware passes each message through a memory stream).
            # Without one, anyio skips delivering the disconnect's cancellation while every local S3 read is complete.
            await asyncio.sleep(0)

        await fastapi_app(_asgi_get_scope("/v1/artifacts/a_stored/content"), receive, send)

        assert sent[0]["status"] == 200
        assert 0 < sum(len(message.get("body", b"")) for message in sent[1:]) < len(large)
        assert sum(size for _, size in storage_bodies.reads) < len(large)
        assert storage_bodies.closes == 1
        assert all(shared.leases == 0 for shared in s3_backed_storage.async_client._shared_s3_clients.values())
