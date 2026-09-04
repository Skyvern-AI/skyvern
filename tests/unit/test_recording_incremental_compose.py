"""Phase 2 (SKY-15288) incremental same-key S3 compose — protocol & lifecycle tests.

A fake async S3 client models the exact MPU surface the compose path drives (create with Metadata /
upload_part_copy with CopySourceIfMatch fencing / upload_part / complete / abort / head with Metadata /
put / upload_fileobj), so these assert the product lifecycle logic — atomic same-key visibility, stale-ETag
conflict preservation, generation-token reconciliation, single-slot fallback ordering, and fd ownership —
without real S3. The real-S3 protocol probe (temp bucket) validates the wire protocol separately.
"""

from __future__ import annotations

import asyncio
import contextlib
import gc
import os
import tempfile
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from botocore.exceptions import ClientError

from skyvern.config import settings
from skyvern.forge.sdk.api.aws import (
    _MAX_COMPOSE_PART_BYTES,
    AsyncAWSClient,
    _ComposeState,
    _ComposeStateCache,
)
from skyvern.forge.sdk.artifact.models import Artifact, ArtifactType
from skyvern.forge.sdk.artifact.storage.s3 import S3Storage

MIB = 1024 * 1024
KEY = "v1/prod/org/task/rec.webm"
URI = f"s3://bucket/{KEY}"
GEN = "skyvern-compose-gen"


def _client_error(code: str, status: int = 400) -> ClientError:
    return ClientError({"Error": {"Code": code}, "ResponseMetadata": {"HTTPStatusCode": status}}, "op")


class FakeS3:
    def __init__(self) -> None:
        # key -> (bytes, etag, metadata)
        self.objects: dict[str, tuple[bytes, str, dict[str, str]]] = {}
        self.mpus: dict[str, dict[str, Any]] = {}
        self.aborted: list[str] = []
        self.abort_attempts: list[str] = []  # every abort attempt (incl. failed), in order
        self.abort_transient_failures = 0  # first N abort calls raise a transient (non-expired) error, then work
        self.ops: list[str] = []  # ordered op log (cleanup-before-replacement ordering assertions)
        self.completed: list[str] = []
        self._n = 0
        self.fail: dict[str, Exception] = {}  # op -> exception to raise once (ClientError OR transport error)
        self.on_complete: Any = None  # optional hook(key, uploadid) to simulate ambiguity/foreign writes
        self.on_before_complete: Any = None  # hook(key, uploadid) firing AFTER copy, BEFORE the IfMatch check
        self.complete_omit_etag = False  # model a successful Complete whose response omits ETag
        self.head_malformed = False  # model a HEAD 200 whose body omits ContentLength/ETag

    def _fail(self, op: str) -> None:
        err = self.fail.pop(op, None)
        if err is not None:
            raise err

    async def put_object(self, *, Bucket: str, Key: str, Body: Any, **kw: Any) -> dict[str, Any]:
        self.ops.append("put_object")
        self._fail("put_object")
        data = Body if isinstance(Body, bytes) else Body.read()
        self._n += 1
        etag = f'"put-{self._n}"'
        self.objects[Key] = (data, etag, {})
        return {"ETag": etag}

    async def upload_fileobj(
        self, Fileobj: Any, Bucket: str, Key: str, ExtraArgs: dict | None = None, Config: Any = None
    ) -> None:
        self.ops.append("upload_fileobj")
        self._fail("upload_fileobj")
        chunks = []
        while True:
            c = Fileobj.read(1 << 20)
            if not c:
                break
            chunks.append(c)
        self._n += 1
        meta = dict((ExtraArgs or {}).get("Metadata") or {})  # real S3 preserves user metadata on put/multipart
        self.objects[Key] = (b"".join(chunks), f'"stream-{self._n}"', meta)

    async def create_multipart_upload(
        self, *, Bucket: str, Key: str, Metadata: dict | None = None, **kw: Any
    ) -> dict[str, Any]:
        self.ops.append("create")
        self._fail("create")
        self._n += 1
        uid = f"mpu-{self._n}"
        self.mpus[uid] = {"key": Key, "parts": {}, "meta": dict(Metadata or {})}
        return {"UploadId": uid}

    async def upload_part_copy(
        self,
        *,
        Bucket: str,
        Key: str,
        UploadId: str,
        PartNumber: int,
        CopySource: dict[str, str],
        CopySourceIfMatch: str,
        CopySourceRange: str,
    ) -> dict[str, Any]:
        self.ops.append("copy")
        self._fail("copy")
        src = self.objects.get(CopySource["Key"])
        if src is None:
            raise _client_error("NoSuchKey", 404)
        data, etag, _ = src
        if CopySourceIfMatch.strip('"') != etag.strip('"'):
            raise _client_error("PreconditionFailed", 412)
        a, b = CopySourceRange.split("=", 1)[1].split("-")
        self.mpus[UploadId]["parts"][PartNumber] = data[int(a) : int(b) + 1]
        return {"CopyPartResult": {"ETag": f'"copy-{PartNumber}"'}}

    async def upload_part(self, *, Bucket: str, Key: str, UploadId: str, PartNumber: int, Body: Any) -> dict[str, Any]:
        self.ops.append("uploadpart")
        self._fail("uploadpart")
        body = Body if isinstance(Body, bytes) else Body.read()
        assert len(body) > 0, "final tail part must never be empty"
        self.mpus[UploadId]["parts"][PartNumber] = body
        return {"ETag": f'"part-{PartNumber}"'}

    async def complete_multipart_upload(
        self, *, Bucket: str, Key: str, UploadId: str, MultipartUpload: dict[str, Any], IfMatch: str | None = None
    ) -> dict[str, Any]:
        if self.on_before_complete is not None:
            # A foreign generation lands AFTER our UploadPartCopy but BEFORE Complete.
            self.on_before_complete(Key, UploadId)
        if IfMatch is not None:
            cur = self.objects.get(Key)
            cur_etag = cur[1] if cur is not None else None
            if cur_etag is None or cur_etag.strip('"') != IfMatch.strip('"'):
                # The destination changed under us: the conditional Complete is rejected (never overwrites).
                raise _client_error("PreconditionFailed", 412)
        m = self.mpus[UploadId]
        data = b"".join(
            m["parts"][p["PartNumber"]] for p in sorted(MultipartUpload["Parts"], key=lambda x: x["PartNumber"])
        )
        etag = f'"complete-{UploadId}"'
        self.objects[Key] = (data, etag, dict(m["meta"]))
        self.completed.append(UploadId)
        if self.on_complete is not None:
            self.on_complete(Key, UploadId)  # may mutate to simulate ambiguity/foreign writes
        self._fail("complete")
        return {} if self.complete_omit_etag else {"ETag": etag}

    async def abort_multipart_upload(self, *, Bucket: str, Key: str, UploadId: str) -> dict[str, Any]:
        self.ops.append("abort")
        self.abort_attempts.append(UploadId)
        if self.abort_transient_failures > 0:
            self.abort_transient_failures -= 1
            raise _client_error("RequestTimeout", 400)  # transient (non-expired) abort failure
        self.aborted.append(UploadId)
        self.mpus.pop(UploadId, None)
        return {}

    async def head_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:
        self._fail("head")
        obj = self.objects.get(Key)
        if obj is None:
            raise _client_error("NotFound", 404)
        data, etag, meta = obj
        if self.head_malformed:
            return {"Metadata": dict(meta)}  # a HEAD success that omits ContentLength/ETag
        return {"ETag": etag, "ContentLength": len(data), "Metadata": dict(meta)}


def _make_client(fake: FakeS3) -> AsyncAWSClient:
    c = AsyncAWSClient()

    @contextlib.asynccontextmanager
    async def _cm() -> Any:
        yield fake

    c._s3_client = _cm  # type: ignore[method-assign]
    return c


def _write(path: str, n: int, fill: bytes = b"\xab") -> None:
    with open(path, "wb") as f:
        f.write(fill * n)


@pytest.fixture
def tmpfile() -> Any:
    fd, path = tempfile.mkstemp(suffix=".webm")
    os.close(fd)
    yield path
    with contextlib.suppress(FileNotFoundError):
        os.unlink(path)


async def _seed(fake: FakeS3, n: int, fill: bytes = b"\xab") -> None:
    fake.objects[KEY] = (fill * n, f'"seed-{n}"', {})


def _seed_base(c: AsyncAWSClient, fake: FakeS3, n: int, fill: bytes = b"\xab") -> None:
    """Seed a process-AUTHORED base: the remote object AND the local compose state that a prior verified
    full replacement would have recorded. Compose only ever runs off such an owned base."""
    etag = f'"seed-{n}"'  # S3 etags are quoted on the wire
    fake.objects[KEY] = (fill * n, etag, {GEN: "prior-token"})
    c._compose_state.set(URI, _ComposeState(n, etag.strip('"')))  # _ComposeState holds the stripped etag


def _obj(fake: FakeS3) -> bytes:
    return fake.objects[KEY][0]


# --- unsupported / fallback (remote unchanged) -------------------------------------------------


@pytest.mark.asyncio
async def test_initial_step_full_replacement_when_no_object(tmpfile: str) -> None:
    fake = FakeS3()
    c = _make_client(fake)
    _write(tmpfile, 10 * MIB)
    assert await c.store_recording_prefix(URI, tmpfile, 10 * MIB, serialize_key=URI) == URI
    assert _obj(fake) == b"\xab" * (10 * MIB)  # full-prefix fallback seeded the object
    assert not fake.mpus and not fake.completed  # no compose attempted


@pytest.mark.asyncio
async def test_small_base_falls_back_to_full_replacement(tmpfile: str) -> None:
    fake = FakeS3()
    await _seed(fake, 2 * MIB)  # < 5 MiB copy floor
    c = _make_client(fake)
    _write(tmpfile, 4 * MIB)
    await c.store_recording_prefix(URI, tmpfile, 4 * MIB, serialize_key=URI)
    assert _obj(fake) == b"\xab" * (4 * MIB)
    assert not fake.completed


@pytest.mark.asyncio
async def test_over_5gib_base_guard_falls_back_without_mpu(tmpfile: str) -> None:
    fake = FakeS3()
    c = _make_client(fake)
    await _seed(fake, 8 * MIB)
    base_len = _MAX_COMPOSE_PART_BYTES + 1  # confirmed base over the single-copy-part limit
    c._compose_state.set(URI, _ComposeState(base_len, '"e"'))
    _write(tmpfile, 8 * MIB)
    # length MUST exceed base_len (a growing snapshot) so the truncate/no-op guards do NOT fire first and the
    # >5GiB base guard is actually reached. The file itself stays 8 MiB — the guard rejects before any tail
    # read, and the same-slot full replacement then uploads whatever the file holds.
    length = base_len + 2
    await c.store_recording_prefix(URI, tmpfile, length, serialize_key=URI)
    assert not fake.mpus  # >5GiB base guard fired before any create_multipart_upload
    assert not fake.completed
    assert _obj(fake) == b"\xab" * (8 * MIB)  # full-prefix fallback wrote the file bytes, no server-side copy


@pytest.mark.asyncio
async def test_custom_endpoint_refuses_compose_and_full_replaces(tmpfile: str, monkeypatch: Any) -> None:
    """A customer-supplied S3-compatible endpoint may ignore the AWS-specific IfMatch / CopySourceIfMatch
    fence and silently degrade compose to an unconditional overwrite. Compose must refuse when a custom
    endpoint is configured and fall back to the verified full-prefix replacement — even with an owned base
    that would otherwise compose, and even when the feature flag is enabled."""
    monkeypatch.setattr(settings, "RECORDING_INCREMENTAL_COMPOSE_ENABLED", True)
    fake = FakeS3()
    c = AsyncAWSClient(endpoint_url="https://minio.example.internal")

    @contextlib.asynccontextmanager
    async def _cm() -> Any:
        yield fake

    c._s3_client = _cm  # type: ignore[method-assign]
    _seed_base(c, fake, 8 * MIB)  # an owned base that WOULD compose against real AWS S3
    _write(tmpfile, 11 * MIB)
    await c.store_recording_prefix(URI, tmpfile, 11 * MIB, serialize_key=URI)
    assert not fake.mpus and not fake.completed  # no create_multipart_upload / upload_part_copy
    assert _obj(fake) == b"\xab" * (11 * MIB)  # full-prefix replacement wrote the file bytes


# --- successful compose ------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compose_success_copy_tail_complete(tmpfile: str) -> None:
    fake = FakeS3()
    c = _make_client(fake)
    _seed_base(c, fake, 8 * MIB)
    _write(tmpfile, 11 * MIB)
    await c.store_recording_prefix(URI, tmpfile, 11 * MIB, serialize_key=URI)
    assert _obj(fake) == b"\xab" * (11 * MIB)
    assert fake.completed and not fake.aborted
    assert c._compose_state.get(URI).length == 11 * MIB


@pytest.mark.asyncio
async def test_compose_chains_and_uses_cached_state(tmpfile: str) -> None:
    fake = FakeS3()
    c = _make_client(fake)
    _seed_base(c, fake, 8 * MIB)
    _write(tmpfile, 11 * MIB)
    await c.store_recording_prefix(URI, tmpfile, 11 * MIB, serialize_key=URI)
    fake.fail["head"] = _client_error("ServiceUnavailable", 503)  # cached state means no head needed
    _write(tmpfile, 13 * MIB)
    await c.store_recording_prefix(URI, tmpfile, 13 * MIB, serialize_key=URI)
    assert len(_obj(fake)) == 13 * MIB


# --- conflict preservation (blocker 2) ---------------------------------------------------------


@pytest.mark.asyncio
async def test_stale_etag_conflict_preserves_remote(tmpfile: str) -> None:
    fake = FakeS3()
    c = _make_client(fake)
    _seed_base(c, fake, 8 * MIB)
    _write(tmpfile, 11 * MIB)
    await c.store_recording_prefix(URI, tmpfile, 11 * MIB, serialize_key=URI)
    # A foreign writer replaces the object with a NEWER, longer generation.
    foreign = b"\x01" * (20 * MIB)
    fake.objects[KEY] = (foreign, '"foreign"', {})
    _write(tmpfile, 13 * MIB)
    await c.store_recording_prefix(URI, tmpfile, 13 * MIB, serialize_key=URI)
    # 412 on copy => conflict => remote preserved, NOT overwritten with our 13 MiB prefix.
    assert _obj(fake) == foreign
    assert fake.aborted  # our MPU aborted
    assert URI not in c._compose_state  # base reseeds next step


# --- review 3920309801: fence Complete against a destination change after copy -----------------


@pytest.mark.asyncio
async def test_foreign_generation_after_copy_before_complete_is_fenced_out(tmpfile: str) -> None:
    """A foreign producer can replace the destination AFTER our UploadPartCopy but BEFORE our Complete.
    The Complete is fenced with IfMatch on the verified base ETag, so it is rejected (412) instead of
    unconditionally overwriting the newer generation: preserve the foreign bytes, abort our MPU, clear
    state, never full-replace."""
    fake = FakeS3()
    c = _make_client(fake)
    _seed_base(c, fake, 8 * MIB)
    _write(tmpfile, 11 * MIB)
    foreign = b"\x0a" * (14 * MIB)

    def foreign_lands_before_complete(key: str, uid: str) -> None:
        fake.objects[key] = (foreign, '"foreign-newgen"', {GEN: "not-our-token"})

    fake.on_before_complete = foreign_lands_before_complete
    await c.store_recording_prefix(URI, tmpfile, 11 * MIB, serialize_key=URI)
    assert _obj(fake) == foreign  # newer foreign generation preserved, NOT clobbered by our Complete
    assert fake.aborted and not fake.completed  # our MPU aborted; our Complete never wrote
    assert URI not in c._compose_state  # conflict path cleared local state


@pytest.mark.asyncio
async def test_fenced_complete_succeeds_when_destination_unchanged(tmpfile: str) -> None:
    """Normal fenced Complete: the destination still holds the verified base at Complete time, so the
    IfMatch fence passes and the compose lands."""
    fake = FakeS3()
    c = _make_client(fake)
    _seed_base(c, fake, 8 * MIB)
    _write(tmpfile, 11 * MIB)
    await c.store_recording_prefix(URI, tmpfile, 11 * MIB, serialize_key=URI)
    assert _obj(fake) == b"\xab" * (11 * MIB)  # compose landed under the fence
    assert fake.completed and not fake.aborted
    assert c._compose_state.get(URI).length == 11 * MIB


# --- ambiguous complete (blocker 3) ------------------------------------------------------------


@pytest.mark.asyncio
async def test_ambiguous_complete_reconciled_by_token(tmpfile: str) -> None:
    fake = FakeS3()
    c = _make_client(fake)
    _seed_base(c, fake, 8 * MIB)
    _write(tmpfile, 11 * MIB)
    # Complete writes the object (with our token) then the response is "lost".
    fake.fail["complete"] = _client_error("RequestTimeout", 400)
    await c.store_recording_prefix(URI, tmpfile, 11 * MIB, serialize_key=URI)
    assert len(_obj(fake)) == 11 * MIB
    assert not fake.aborted  # reconciled as durable via length + token; never abort a completed object


@pytest.mark.asyncio
async def test_ambiguous_complete_same_length_foreign_gen_not_accepted(tmpfile: str) -> None:
    fake = FakeS3()
    c = _make_client(fake)
    _seed_base(c, fake, 8 * MIB)
    _write(tmpfile, 11 * MIB)

    def foreign_same_length(key: str, uid: str) -> None:
        # Simulate a DIFFERENT generation of the SAME length (foreign token) landing at the key.
        fake.objects[key] = (b"\x09" * (11 * MIB), '"foreign-same-len"', {GEN: "not-our-token"})

    fake.on_complete = foreign_same_length
    fake.fail["complete"] = _client_error("RequestTimeout", 400)
    await c.store_recording_prefix(URI, tmpfile, 11 * MIB, serialize_key=URI)
    # Same length but foreign token => conflict => preserve foreign bytes, do NOT overwrite.
    assert _obj(fake) == b"\x09" * (11 * MIB)
    assert fake.aborted
    assert URI not in c._compose_state


@pytest.mark.asyncio
async def test_ambiguous_complete_old_base_intact_falls_back(tmpfile: str) -> None:
    fake = FakeS3()
    c = _make_client(fake)
    _seed_base(c, fake, 8 * MIB)
    _write(tmpfile, 11 * MIB)

    def revert_to_old_base(key: str, uid: str) -> None:
        fake.objects[key] = (b"\xab" * (8 * MIB), '"seed-8388608"', {})  # complete "didn't land"

    fake.on_complete = revert_to_old_base
    fake.fail["complete"] = _client_error("RequestTimeout", 400)
    await c.store_recording_prefix(URI, tmpfile, 11 * MIB, serialize_key=URI)
    # Old base intact => unsupported => safe full replacement to our 11 MiB prefix.
    assert _obj(fake) == b"\xab" * (11 * MIB)
    assert fake.aborted


# --- review 3917237493: unreadable HEAD after ambiguous complete must PRESERVE, never clobber ---


@pytest.mark.asyncio
async def test_ambiguous_complete_head_transport_failure_preserves_remote(tmpfile: str) -> None:
    """A NON-ClientError HEAD failure (endpoint/read timeout) during ambiguous-complete reconciliation must
    resolve to conflict and preserve the remote — never fall through to an unconditional full replacement
    that could clobber a foreign generation written during the same network blip."""
    from botocore.exceptions import EndpointConnectionError

    fake = FakeS3()
    c = _make_client(fake)
    _seed_base(c, fake, 8 * MIB)
    _write(tmpfile, 11 * MIB)

    def foreign_lands(key: str, uid: str) -> None:
        fake.objects[key] = (b"\x07" * (11 * MIB), '"foreign"', {GEN: "not-our-token"})

    fake.on_complete = foreign_lands
    fake.fail["complete"] = _client_error("RequestTimeout", 400)  # ambiguous complete
    fake.fail["head"] = EndpointConnectionError(endpoint_url="https://s3")  # transport failure, NOT a ClientError
    await c.store_recording_prefix(URI, tmpfile, 11 * MIB, serialize_key=URI)
    assert _obj(fake) == b"\x07" * (11 * MIB)  # foreign generation preserved, NOT overwritten with our prefix
    assert fake.aborted  # our MPU aborted
    assert URI not in c._compose_state  # base reseeds next step


@pytest.mark.asyncio
async def test_reconcile_complete_reraises_expired_token_for_retry() -> None:
    """An expired token on the reconciliation HEAD is recoverable, NOT a conflict: it must be re-raised so
    _s3_with_retry refreshes the session and retries the transaction, rather than being swallowed as
    "conflict" (which would skip the step on a recoverable expiry)."""
    from skyvern.forge.sdk.api.aws import S3Uri

    fake = FakeS3()
    c = _make_client(fake)
    fake.objects[KEY] = (b"\xab" * (8 * MIB), '"seed-8"', {GEN: "prior"})
    base = _ComposeState(8 * MIB, "seed-8")
    fake.fail["head"] = _client_error("ExpiredTokenException", 400)
    with pytest.raises(ClientError) as ei:
        await c._reconcile_complete(fake, S3Uri(URI), 11 * MIB, base, "token")
    assert ei.value.response["Error"]["Code"] == "ExpiredTokenException"


@pytest.mark.asyncio
async def test_ambiguous_complete_malformed_head_preserves_remote(tmpfile: str) -> None:
    """A HEAD that returns 200 but omits ContentLength/ETag is just as unreadable as a failed HEAD: the parse
    must not escape reconciliation and trigger a clobbering full replacement. Resolve to conflict, preserve."""
    fake = FakeS3()
    c = _make_client(fake)
    _seed_base(c, fake, 8 * MIB)
    _write(tmpfile, 11 * MIB)

    def foreign_lands(key: str, uid: str) -> None:
        fake.objects[key] = (b"\x06" * (11 * MIB), '"foreign"', {GEN: "not-our-token"})

    fake.on_complete = foreign_lands
    fake.fail["complete"] = _client_error("RequestTimeout", 400)
    fake.head_malformed = True  # HEAD 200 without ContentLength/ETag
    await c.store_recording_prefix(URI, tmpfile, 11 * MIB, serialize_key=URI)
    assert _obj(fake) == b"\x06" * (11 * MIB)  # preserved, not clobbered by a full replacement
    assert fake.aborted
    assert URI not in c._compose_state


@pytest.mark.asyncio
async def test_full_replace_malformed_head_pops_stale_compose_state(tmpfile: str) -> None:
    """A malformed HEAD (200 without ContentLength/ETag) after the full-replacement PUT must guard its read
    and pop the previous compose state rather than raise; a raise would strand the stale base cached and cost
    a snapshot self-healing as a 412 next step."""
    fake = FakeS3()
    c = _make_client(fake)
    _seed_base(c, fake, 2 * MIB)  # base below the 5-MiB copy floor -> ComposeUnsupportedError -> full replace
    _write(tmpfile, 4 * MIB)
    fake.head_malformed = True  # the seed-and-verify HEAD omits ContentLength/ETag
    assert await c.store_recording_prefix(URI, tmpfile, 4 * MIB, serialize_key=URI) == URI
    assert URI not in c._compose_state  # stale base popped, not left cached
    assert _obj(fake) == b"\xab" * (4 * MIB)  # replacement still wrote the file
    assert not fake.mpus and not fake.completed  # full-replace path, no compose MPU


# --- review 3917237500: Complete without ETag must validate length + token before caching -------


@pytest.mark.asyncio
async def test_complete_without_etag_foreign_gen_not_cached(tmpfile: str) -> None:
    """A successful Complete whose response omits ETag must reconcile by HEAD and validate our exact
    length + generation token; a foreign same-length generation that raced in must NOT be cached (else the
    next snapshot's conditional copy splices the foreign prefix with our tail)."""
    fake = FakeS3()
    c = _make_client(fake)
    _seed_base(c, fake, 8 * MIB)
    _write(tmpfile, 11 * MIB)
    fake.complete_omit_etag = True

    def foreign_same_len(key: str, uid: str) -> None:
        fake.objects[key] = (b"\x05" * (11 * MIB), '"foreign-noetag"', {GEN: "not-our-token"})

    fake.on_complete = foreign_same_len
    await c.store_recording_prefix(URI, tmpfile, 11 * MIB, serialize_key=URI)
    assert URI not in c._compose_state  # foreign generation NOT cached as our base
    assert _obj(fake) == b"\x05" * (11 * MIB)  # foreign preserved, not clobbered


@pytest.mark.asyncio
async def test_complete_without_etag_own_gen_cached(tmpfile: str) -> None:
    """Regression guard: a successful Complete omitting ETag, with OUR generation at the key, still caches
    the base (validated by exact length + token) so the compose chain continues."""
    fake = FakeS3()
    c = _make_client(fake)
    _seed_base(c, fake, 8 * MIB)
    _write(tmpfile, 11 * MIB)
    fake.complete_omit_etag = True
    await c.store_recording_prefix(URI, tmpfile, 11 * MIB, serialize_key=URI)
    assert len(_obj(fake)) == 11 * MIB
    assert c._compose_state.get(URI) is not None and c._compose_state.get(URI).length == 11 * MIB


# --- pre-complete failure aborts + preserves old object (remote unchanged => fallback) ----------


@pytest.mark.asyncio
@pytest.mark.parametrize("failop", ["create", "copy", "uploadpart"])
async def test_precomplete_failure_aborts_then_full_replacement(tmpfile: str, failop: str) -> None:
    fake = FakeS3()
    c = _make_client(fake)
    _seed_base(c, fake, 8 * MIB)
    _write(tmpfile, 11 * MIB)
    fake.fail[failop] = _client_error("InternalError", 500)
    await c.store_recording_prefix(URI, tmpfile, 11 * MIB, serialize_key=URI)
    # Remote was unchanged by the failed compose, so the same-slot full replacement lands our prefix.
    assert _obj(fake) == b"\xab" * (11 * MIB)
    if failop != "create":
        assert fake.aborted


# --- single-slot ordering (blocker 1) ----------------------------------------------------------


@pytest.mark.asyncio
async def test_failed_compose_fallback_and_next_step_stay_ordered() -> None:
    """Step A's compose fails and must complete its full-prefix fallback before step B (queued) writes."""
    fake = FakeS3()
    c = _make_client(fake)
    _seed_base(c, fake, 8 * MIB)
    order: list[str] = []
    gate = asyncio.Event()
    orig_fileobj = fake.upload_fileobj

    async def gated_fileobj(
        Fileobj: Any, Bucket: str, Key: str, ExtraArgs: dict | None = None, Config: Any = None
    ) -> None:
        await gate.wait()  # hold A inside its fallback
        await orig_fileobj(Fileobj, Bucket, Key, ExtraArgs, Config)
        order.append("A_fallback")

    fake.upload_fileobj = gated_fileobj  # type: ignore[method-assign]
    fake.fail["copy"] = _client_error("InternalError", 500)  # force A to fall back

    fa = tempfile.mkstemp(suffix=".webm")[1]
    fb = tempfile.mkstemp(suffix=".webm")[1]
    _write(fa, 11 * MIB)
    _write(fb, 13 * MIB)
    try:
        a = asyncio.create_task(c.store_recording_prefix(URI, fa, 11 * MIB, serialize_key=URI))
        await asyncio.sleep(0.05)  # let A reserve its slot and enter the gated fallback
        orig_complete = fake.complete_multipart_upload

        async def b_complete(**kw: Any) -> dict[str, Any]:
            order.append("B_complete")
            return await orig_complete(**kw)

        fake.complete_multipart_upload = b_complete  # type: ignore[method-assign]
        b = asyncio.create_task(c.store_recording_prefix(URI, fb, 13 * MIB, serialize_key=URI))
        await asyncio.sleep(0.05)
        assert "B_complete" not in order  # B must not overtake A's unfinished fallback
        gate.set()
        await asyncio.gather(a, b)
        assert order == ["A_fallback", "B_complete"]  # strict ordering held across compose+fallback
        assert len(_obj(fake)) == 13 * MIB
    finally:
        for f in (fa, fb):
            with contextlib.suppress(FileNotFoundError):
                os.unlink(f)


# --- fd ownership before detach (blocker 4) ----------------------------------------------------


@pytest.mark.asyncio
async def test_fd_owned_before_detach_survives_unlink(tmpfile: str) -> None:
    fake = FakeS3()
    await _seed(fake, 8 * MIB)
    c = _make_client(fake)
    _write(tmpfile, 11 * MIB)
    # Block the step at its queued wait, unlink the path, then release: the pre-opened fd must survive.
    _prev, prev_done = c._begin_serialized_write(URI)
    task = asyncio.create_task(c.store_recording_prefix(URI, tmpfile, 11 * MIB, serialize_key=URI))
    await asyncio.sleep(0.05)
    os.unlink(tmpfile)  # caller removes the file after the transfer was queued
    prev_done.set_result(None)
    assert await task == URI
    assert len(_obj(fake)) == 11 * MIB  # tail read from the owned fd despite the unlink


# --- storage gating / feature-off exact Phase 1 ------------------------------------------------


def _recording_artifact(uri: str, atype: ArtifactType = ArtifactType.RECORDING) -> Artifact:
    return Artifact(
        artifact_id="art_1",
        artifact_type=atype,
        uri=uri,
        bundle_key=None,
        organization_id="o_1",
        created_at=datetime.now(UTC),
        modified_at=datetime.now(UTC),
    )


def _mock_storage() -> S3Storage:
    s = S3Storage()
    s.async_client = AsyncMock()
    s.async_client.forget_compose_state = MagicMock()
    return s


@pytest.mark.asyncio
async def test_storage_gating_off_uses_exact_phase1_stream(tmpfile: str, monkeypatch: Any) -> None:
    monkeypatch.setattr(settings, "RECORDING_INCREMENTAL_COMPOSE_ENABLED", False)
    s = _mock_storage()
    _write(tmpfile, 10 * MIB)
    await s.store_artifact_prefix_from_path(_recording_artifact(URI), tmpfile, 10 * MIB)
    s.async_client.store_recording_prefix.assert_not_called()
    s.async_client.upload_file_stream.assert_awaited_once()


@pytest.mark.asyncio
async def test_storage_gating_on_recording_uses_compose_path(tmpfile: str, monkeypatch: Any) -> None:
    monkeypatch.setattr(settings, "RECORDING_INCREMENTAL_COMPOSE_ENABLED", True)
    s = _mock_storage()
    _write(tmpfile, 10 * MIB)
    await s.store_artifact_prefix_from_path(_recording_artifact(URI), tmpfile, 10 * MIB)
    s.async_client.store_recording_prefix.assert_awaited_once()
    s.async_client.upload_file_stream.assert_not_called()


@pytest.mark.asyncio
async def test_storage_non_recording_uses_phase1_even_when_enabled(tmpfile: str, monkeypatch: Any) -> None:
    monkeypatch.setattr(settings, "RECORDING_INCREMENTAL_COMPOSE_ENABLED", True)
    s = _mock_storage()
    _write(tmpfile, 10 * MIB)
    await s.store_artifact_prefix_from_path(_recording_artifact(URI, ArtifactType.SKYVERN_LOG), tmpfile, 10 * MIB)
    s.async_client.store_recording_prefix.assert_not_called()
    s.async_client.upload_file_stream.assert_awaited_once()


@pytest.mark.asyncio
async def test_terminal_forgets_compose_state(monkeypatch: Any) -> None:
    monkeypatch.setattr(settings, "RECORDING_INCREMENTAL_COMPOSE_ENABLED", True)
    s = _mock_storage()
    art = _recording_artifact(URI)
    await s.store_artifact(art, b"final-bytes", supersede_queued_prefixes=True)
    s.async_client.forget_compose_state.assert_called_once_with(art.uri)
    s.async_client.upload_file.assert_awaited_once()


# --- never regress/truncate a newer remote generation (addendum 1) -----------------------------


@pytest.mark.asyncio
async def test_larger_remote_generation_preserved_not_truncated(tmpfile: str) -> None:
    fake = FakeS3()
    c = _make_client(fake)
    _seed_base(c, fake, 20 * MIB)  # process-authored base LONGER than this step's snapshot
    _write(tmpfile, 13 * MIB)
    await c.store_recording_prefix(URI, tmpfile, 13 * MIB, serialize_key=URI)
    assert len(_obj(fake)) == 20 * MIB  # preserved; never overwritten with the shorter 13 MiB prefix
    assert not fake.completed  # no compose, and crucially no full-PUT truncation
    assert URI not in c._compose_state


@pytest.mark.asyncio
async def test_same_length_remote_is_noop(tmpfile: str) -> None:
    fake = FakeS3()
    c = _make_client(fake)
    _seed_base(c, fake, 8 * MIB)
    _write(tmpfile, 8 * MIB)
    before = _obj(fake)
    await c.store_recording_prefix(URI, tmpfile, 8 * MIB, serialize_key=URI)
    assert _obj(fake) == before  # no-op: no MPU, no full-PUT
    assert not fake.completed and not fake.mpus


# --- newest pre-terminal fallback contract (addendum 2) ----------------------------------------


async def _park_newest(c: AsyncAWSClient, fake: FakeS3, path: str, length: int) -> asyncio.Task:
    """Reserve a queued prefix, seal a terminal behind it so the queued prefix is the fallback target,
    and drive the queued prefix so it parks. Returns the terminal task (already reserved, awaiting)."""
    blk_prev, blk_done = c._begin_serialized_write(URI)  # blocker so the queued prefix waits
    q = asyncio.create_task(c.store_recording_prefix(URI, path, length, serialize_key=URI))
    await asyncio.sleep(0.03)  # queued prefix reserves its slot, then awaits blk_done
    return blk_done, q


@pytest.mark.asyncio
async def test_superseded_newest_parks_and_terminal_failure_uploads_it(tmpfile: str) -> None:
    fake = FakeS3()
    await _seed(fake, 8 * MIB)
    c = _make_client(fake)
    _write(tmpfile, 11 * MIB)
    blk_done, q = await _park_newest(c, fake, tmpfile, 11 * MIB)
    fake.fail["put_object"] = _client_error("InternalError", 500)  # terminal finalize fails -> use fallback
    t = asyncio.create_task(c.upload_file(URI, b"terminal-final", serialize_key=URI, supersede_queued=True))
    await asyncio.sleep(0.03)  # terminal reserves the seal (its prev = the queued prefix's done)
    blk_done.set_result(None)  # release: queued prefix parks; terminal runs, put fails, uploads fallback
    await asyncio.gather(q, t)
    assert _obj(fake) == b"\xab" * (11 * MIB)  # newest pre-terminal snapshot uploaded on terminal failure
    assert URI not in c._object_write_fallback  # consumed + cleaned up


@pytest.mark.asyncio
async def test_superseded_newest_parked_then_terminal_success_discards_it(tmpfile: str) -> None:
    fake = FakeS3()
    await _seed(fake, 8 * MIB)
    c = _make_client(fake)
    _write(tmpfile, 11 * MIB)
    blk_done, q = await _park_newest(c, fake, tmpfile, 11 * MIB)
    t = asyncio.create_task(c.upload_file(URI, b"terminal-final", serialize_key=URI, supersede_queued=True))
    await asyncio.sleep(0.03)
    blk_done.set_result(None)  # queued prefix parks; terminal put succeeds -> fallback discarded, not uploaded
    await asyncio.gather(q, t)
    assert _obj(fake) == b"terminal-final"  # terminal full replacement wins
    assert URI not in c._object_write_fallback


@pytest.mark.asyncio
async def test_superseded_older_prefix_skips_without_parking(tmpfile: str) -> None:
    fake = FakeS3()
    await _seed(fake, 8 * MIB)
    c = _make_client(fake)
    _write(tmpfile, 11 * MIB)
    # Seal the key with a DIFFERENT (sentinel) fallback target so this queued prefix is NOT the newest.
    sentinel: asyncio.Future = asyncio.get_running_loop().create_future()
    c._object_write_sealed.add(URI)
    c._object_write_fallback_target[URI] = sentinel
    await c.store_recording_prefix(URI, tmpfile, 11 * MIB, serialize_key=URI)
    assert URI not in c._object_write_fallback  # older superseded prefix skips, does not park
    assert not fake.completed


# --- MUST_FIX 1: a GC'd abandoned reader must NOT close the fd the same-slot fallback still reads ------


@pytest.mark.asyncio
async def test_gc_during_fallback_does_not_close_shared_fd(tmpfile: str) -> None:
    fake = FakeS3()
    c = _make_client(fake)
    _seed_base(c, fake, 8 * MIB)
    _write(tmpfile, 11 * MIB)
    # Compose creates a tail reader over the shared fd, then fails; the compose falls back to a full
    # replacement in the same slot. A gc.collect() before the fallback reads would, if any reader owned the
    # shared fd, run its __del__ -> close() and the fallback would die with "read of closed file".
    fake.fail["uploadpart"] = _client_error("InternalError", 500)
    orig_fileobj = fake.upload_fileobj

    async def gc_then_upload(
        Fileobj: Any, Bucket: str, Key: str, ExtraArgs: dict | None = None, Config: Any = None
    ) -> None:
        gc.collect()
        await orig_fileobj(Fileobj, Bucket, Key, ExtraArgs, Config)

    fake.upload_fileobj = gc_then_upload  # type: ignore[method-assign]
    assert await c.store_recording_prefix(URI, tmpfile, 11 * MIB, serialize_key=URI) == URI
    assert _obj(fake) == b"\xab" * (11 * MIB)  # fallback read the shared fd successfully after GC


# --- MUST_FIX 2: expired-token refresh+retry parity for compose AND the inline full-replace -----------


@pytest.mark.asyncio
async def test_compose_refreshes_and_retries_on_expired_token(tmpfile: str) -> None:
    fake = FakeS3()
    c = _make_client(fake)
    _seed_base(c, fake, 8 * MIB)
    _write(tmpfile, 11 * MIB)
    refreshed: list[bool] = []
    c.refresh_session = lambda: refreshed.append(True)  # type: ignore[method-assign]
    fake.fail["copy"] = _client_error("ExpiredTokenException", 400)  # first copy expires; popped -> retry ok
    await c.store_recording_prefix(URI, tmpfile, 11 * MIB, serialize_key=URI)
    assert refreshed  # session was refreshed instead of silently degrading
    assert len(_obj(fake)) == 11 * MIB and fake.completed  # compose landed on the retry
    assert c._compose_state.get(URI).length == 11 * MIB


@pytest.mark.asyncio
async def test_full_replace_refreshes_and_retries_on_expired_token(tmpfile: str) -> None:
    fake = FakeS3()
    c = _make_client(fake)  # no owned base -> full-replace path
    _write(tmpfile, 10 * MIB)
    refreshed: list[bool] = []
    c.refresh_session = lambda: refreshed.append(True)  # type: ignore[method-assign]
    fake.fail["upload_fileobj"] = _client_error("ExpiredTokenException", 400)
    await c.store_recording_prefix(URI, tmpfile, 10 * MIB, serialize_key=URI)
    assert refreshed
    assert _obj(fake) == b"\xab" * (10 * MIB)
    assert c._compose_state.get(URI) is not None  # verified generation seeded after the retry


# --- Aron review finding 2: expired-token orphan MPU must be aborted on the REFRESHED client ----


class _GenBackend:
    """Shared S3 state across client 'generations'. Generation 1 is a stale/expired client: its complete
    AND abort both raise ExpiredTokenException (one expired session token invalidates the whole client, so
    aborting the orphaned MPU on that same client is futile). Generation 2 (post-refresh) works. Records
    which upload ids were aborted by which generation so a test can assert the refreshed client aborts the
    exact orphan before creating a replacement MPU."""

    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, str, dict[str, str]]] = {}
        self.mpus: dict[str, dict[str, Any]] = {}
        self.completed: list[str] = []
        self.completed_ids: set[str] = set()  # upload ids whose Complete actually landed
        self.aborted_by_gen: dict[int, list[str]] = {}  # successful aborts, per generation
        self.abort_attempts_by_gen: dict[int, list[str]] = {}  # every abort attempt (incl. failed), per gen
        self.fail_abort_gens: set[int] = set()  # generations whose abort raises (e.g. a transient failure)
        self.expired_code = "ExpiredTokenException"  # exact S3 error code to raise (S3 also uses "ExpiredToken")
        self.complete_fail_gens: set[int] = {1}  # generations whose Complete raises expired-token (orphan MPU)
        self.complete_omit_etag = False  # Complete succeeds but omits ETag
        self.head_expired_gens: set[int] = set()  # generations whose head_object raises expired-token
        self.nosuchupload_on_completed_abort = True  # abort of an already-completed id raises NoSuchUpload
        self._n = 0
        self.gen = 0

    def next_client(self) -> _GenClient:
        self.gen += 1
        return _GenClient(self, self.gen)


class _GenClient:
    def __init__(self, be: _GenBackend, gen: int) -> None:
        self.be = be
        self.gen = gen

    async def create_multipart_upload(self, *, Bucket: str, Key: str, Metadata: dict | None = None, **kw: Any) -> Any:
        self.be._n += 1
        uid = f"mpu-{self.be._n}"
        self.be.mpus[uid] = {"key": Key, "parts": {}, "meta": dict(Metadata or {})}
        return {"UploadId": uid}

    async def upload_part_copy(
        self,
        *,
        Bucket: str,
        Key: str,
        UploadId: str,
        PartNumber: int,
        CopySource: dict,
        CopySourceIfMatch: str,
        CopySourceRange: str,
    ) -> Any:
        data, etag, _ = self.be.objects[CopySource["Key"]]
        if CopySourceIfMatch.strip('"') != etag.strip('"'):
            raise _client_error("PreconditionFailed", 412)
        a, b = CopySourceRange.split("=", 1)[1].split("-")
        self.be.mpus[UploadId]["parts"][PartNumber] = data[int(a) : int(b) + 1]
        return {"CopyPartResult": {"ETag": f'"copy-{PartNumber}"'}}

    async def upload_part(self, *, Bucket: str, Key: str, UploadId: str, PartNumber: int, Body: Any) -> Any:
        body = Body if isinstance(Body, bytes) else Body.read()
        self.be.mpus[UploadId]["parts"][PartNumber] = body
        return {"ETag": f'"part-{PartNumber}"'}

    async def complete_multipart_upload(
        self, *, Bucket: str, Key: str, UploadId: str, MultipartUpload: dict, IfMatch: str | None = None
    ) -> Any:
        if self.gen in self.be.complete_fail_gens:
            raise _client_error(self.be.expired_code, 400)  # token expires at complete -> MPU orphaned
        if IfMatch is not None:
            cur = self.be.objects.get(Key)
            if cur is None or cur[1].strip('"') != IfMatch.strip('"'):
                raise _client_error("PreconditionFailed", 412)
        m = self.be.mpus[UploadId]
        data = b"".join(
            m["parts"][p["PartNumber"]] for p in sorted(MultipartUpload["Parts"], key=lambda x: x["PartNumber"])
        )
        etag = f'"complete-{UploadId}"'
        self.be.objects[Key] = (data, etag, dict(m["meta"]))
        self.be.completed.append(UploadId)
        self.be.completed_ids.add(UploadId)
        return {} if self.be.complete_omit_etag else {"ETag": etag}

    async def abort_multipart_upload(self, *, Bucket: str, Key: str, UploadId: str) -> Any:
        self.be.abort_attempts_by_gen.setdefault(self.gen, []).append(UploadId)  # record EVERY attempt
        if UploadId in self.be.completed_ids and self.be.nosuchupload_on_completed_abort:
            # An already-completed upload can no longer be aborted: real S3 returns NoSuchUpload.
            raise _client_error("NoSuchUpload", 404)
        if self.gen in self.be.complete_fail_gens or self.gen in self.be.fail_abort_gens:
            raise _client_error(self.be.expired_code, 400)  # stale client or injected transient abort failure
        self.be.aborted_by_gen.setdefault(self.gen, []).append(UploadId)
        self.be.mpus.pop(UploadId, None)
        return {}

    async def head_object(self, *, Bucket: str, Key: str) -> Any:
        if self.gen in self.be.head_expired_gens:
            raise _client_error(self.be.expired_code, 400)
        obj = self.be.objects.get(Key)
        if obj is None:
            raise _client_error("NotFound", 404)
        data, etag, meta = obj
        return {"ETag": etag, "ContentLength": len(data), "Metadata": dict(meta)}


def _make_client_gen(be: _GenBackend) -> AsyncAWSClient:
    c = AsyncAWSClient()

    @contextlib.asynccontextmanager
    async def _cm() -> Any:
        yield be.next_client()

    c._s3_client = _cm  # type: ignore[method-assign]
    return c


# S3 reports an expired STS/web-identity token under EITHER exact code; both must drive the same refresh+retry
# path (the compose retry, not just the helper). "ExpiredToken" is the bare S3 code the review flagged.
@pytest.mark.asyncio
@pytest.mark.parametrize("expired_code", ["ExpiredTokenException", "ExpiredToken"])
async def test_expired_token_orphan_mpu_aborted_on_refreshed_client(tmpfile: str, expired_code: str) -> None:
    be = _GenBackend()
    be.expired_code = expired_code
    be.objects[KEY] = (b"\xab" * (8 * MIB), '"seed-8"', {GEN: "prior"})  # process-owned base
    c = _make_client_gen(be)
    c._compose_state.set(URI, _ComposeState(8 * MIB, "seed-8"))
    c.refresh_session = lambda: None  # type: ignore[method-assign]  # no real STS session in the test
    _write(tmpfile, 11 * MIB)
    await c.store_recording_prefix(URI, tmpfile, 11 * MIB, serialize_key=URI)
    # gen1 created mpu-1, then expired at complete; aborting mpu-1 on that stale gen1 client also failed.
    # The refreshed gen2 client MUST abort the exact orphaned mpu-1 before completing the replacement mpu-2.
    assert be.aborted_by_gen.get(2) == ["mpu-1"]  # refreshed client aborted the exact old upload id
    assert 1 not in be.aborted_by_gen  # the stale client did NOT successfully abort it
    assert be.completed == ["mpu-2"]  # the replacement compose completed on the retry
    assert "mpu-1" not in be.mpus  # the orphan MPU is gone (no lingering incomplete upload)
    assert len(be.objects[KEY][0]) == 11 * MIB  # composed recording landed
    assert c._compose_state.get(URI).length == 11 * MIB


@pytest.mark.asyncio
async def test_expired_token_orphan_abort_failure_fails_closed_no_replacement(tmpfile: str) -> None:
    """If the REFRESHED client's abort of the carried orphan ALSO fails, fail closed: do NOT create a
    replacement compose MPU on top of an un-aborted orphan. Preserve the existing remote object, drop stale
    compose state (so the next step authors a fresh verified generation), and skip — the bucket
    AbortIncompleteMultipartUpload lifecycle rule is the documented backstop for the leaked MPU."""
    be = _GenBackend()
    base_bytes = b"\xab" * (8 * MIB)
    be.objects[KEY] = (base_bytes, '"seed-8"', {GEN: "prior"})
    be.fail_abort_gens = {2}  # the refreshed (gen2) client's abort of the orphan ALSO fails
    c = _make_client_gen(be)
    c._compose_state.set(URI, _ComposeState(8 * MIB, "seed-8"))
    c.refresh_session = lambda: None  # type: ignore[method-assign]
    _write(tmpfile, 11 * MIB)
    await c.store_recording_prefix(URI, tmpfile, 11 * MIB, serialize_key=URI)
    assert be.abort_attempts_by_gen.get(2) == ["mpu-1"]  # the orphan abort WAS attempted on the refreshed client
    assert be.aborted_by_gen.get(2) is None  # ...and it failed
    assert "mpu-2" not in be.mpus and be.completed == []  # fail-closed: NO replacement MPU created/completed
    assert be.objects[KEY][0] == base_bytes  # existing remote object preserved, not overwritten
    assert URI not in c._compose_state  # stale compose state dropped -> next step authors a fresh generation


@pytest.mark.asyncio
async def test_abort_compose_treats_nosuchupload_as_success() -> None:
    """A NoSuchUpload from abort means the upload no longer exists (e.g. an ambiguous Complete actually
    landed) — that IS the cleanup goal, so _abort_compose reports success (True), not an unabortable orphan.
    A genuine failure (AccessDenied) or any other error still reports False so it is not masked."""
    from skyvern.forge.sdk.api.aws import S3Uri

    c = _make_client(FakeS3())
    u = S3Uri(URI)

    async def _abort_raises(code: str) -> bool:
        client = MagicMock()
        client.abort_multipart_upload = AsyncMock(
            side_effect=_client_error(code, 404 if code == "NoSuchUpload" else 403)
        )
        return await c._abort_compose(client, u, "mpu-x")

    assert await _abort_raises("NoSuchUpload") is True  # already gone -> cleanup success
    assert await _abort_raises("AccessDenied") is False  # genuine failure not masked
    assert await _abort_raises("InternalError") is False  # any other error still reported as failure


@pytest.mark.asyncio
async def test_generic_failure_inline_abort_transient_then_fresh_cleanup_before_full_replace(tmpfile: str) -> None:
    """A non-expired pre-Complete failure leaves the remote unchanged (safe to full-replace), but the inline
    abort of our MPU fails transiently. The transaction MUST clean up on a freshly-created client BEFORE the
    full replacement — so no incomplete MPU leaks — and then land exactly one safe replacement, in that order
    (cleanup precedes replacement)."""
    fake = FakeS3()
    c = _make_client(fake)
    _seed_base(c, fake, 8 * MIB)
    _write(tmpfile, 11 * MIB)
    fake.fail["uploadpart"] = _client_error("InternalError", 500)  # non-expired pre-Complete failure
    fake.abort_transient_failures = 1  # inline abort fails once; the fresh-client abort then succeeds
    await c.store_recording_prefix(URI, tmpfile, 11 * MIB, serialize_key=URI)
    assert fake.abort_attempts == ["mpu-1", "mpu-1"]  # inline attempt, then the fresh-client retry (same id)
    assert fake.aborted == ["mpu-1"] and "mpu-1" not in fake.mpus  # orphan cleaned up on the fresh client
    assert not fake.completed  # no compose Complete
    assert _obj(fake) == b"\xab" * (11 * MIB)  # the safe full replacement landed our prefix
    assert fake.ops.count("upload_fileobj") == 1  # exactly one replacement, no duplicate PUT
    last_abort = max(i for i, o in enumerate(fake.ops) if o == "abort")
    assert fake.ops.index("upload_fileobj") > last_abort  # cleanup precedes the replacement
    assert c._compose_state.get(URI) is not None and c._compose_state.get(URI).length == 11 * MIB


@pytest.mark.asyncio
async def test_fresh_cleanup_refreshes_session_before_building_client(tmpfile: str) -> None:
    """The fresh-client cleanup may be reached because the token expired between the primary op and the inline
    abort. _s3_client reuses the cached session (recreated only on the 45-min TTL), so the cleanup must refresh
    the session BEFORE constructing the fresh client — otherwise it carries the same expired credentials and
    fails identically, and since this path returns normally _s3_with_retry never sees the token error."""
    fake = FakeS3()
    c = _make_client(fake)
    _seed_base(c, fake, 8 * MIB)
    _write(tmpfile, 11 * MIB)
    fake.fail["uploadpart"] = _client_error("InternalError", 500)  # non-expired pre-Complete failure
    fake.abort_transient_failures = 1  # inline abort fails once -> the fresh-client cleanup path is taken
    calls: list[str] = []
    real_refresh = c.refresh_session

    def _spy() -> None:
        calls.append("refresh")
        real_refresh()

    c.refresh_session = _spy  # type: ignore[method-assign]
    await c.store_recording_prefix(URI, tmpfile, 11 * MIB, serialize_key=URI)
    assert calls == ["refresh"]  # session refreshed once, on the fresh-cleanup path
    assert fake.aborted == ["mpu-1"]  # cleanup then succeeded on the refreshed fresh client


@pytest.mark.asyncio
async def test_conflict_inline_abort_transient_then_fresh_cleanup_preserves_remote(tmpfile: str) -> None:
    """A copy 412 conflict against a moved remote, where the inline abort fails transiently: the MPU must be
    cleaned up on a fresh client, the remote generation preserved (never overwritten), and NO replacement
    written."""
    fake = FakeS3()
    c = _make_client(fake)
    _seed_base(c, fake, 8 * MIB)
    _write(tmpfile, 11 * MIB)
    foreign = b"\x01" * (20 * MIB)
    fake.objects[KEY] = (foreign, '"foreign"', {})  # base moved under us -> copy 412 conflict
    fake.abort_transient_failures = 1  # inline abort fails; fresh-client abort succeeds
    await c.store_recording_prefix(URI, tmpfile, 11 * MIB, serialize_key=URI)
    assert fake.abort_attempts == ["mpu-1", "mpu-1"]  # inline attempt + fresh-client retry
    assert fake.aborted == ["mpu-1"] and "mpu-1" not in fake.mpus  # orphan cleaned up on the fresh client
    assert _obj(fake) == foreign  # remote preserved, never overwritten
    assert not fake.completed and fake.ops.count("upload_fileobj") == 0  # conflict path never full-replaces
    assert URI not in c._compose_state  # stale compose state cleared


@pytest.mark.asyncio
async def test_fenced_complete_conflict_inline_abort_transient_then_fresh_cleanup_preserves_remote(
    tmpfile: str,
) -> None:
    """A foreign generation lands AFTER our copy but BEFORE our fenced Complete (IfMatch 412), and the inline
    abort of our MPU then fails transiently. The fenced-Complete conflict site must converge exactly like the
    copy conflict site: clean up the MPU on a fresh client, preserve the foreign remote, and write NO
    replacement."""
    fake = FakeS3()
    c = _make_client(fake)
    _seed_base(c, fake, 8 * MIB)
    _write(tmpfile, 11 * MIB)
    foreign = b"\x0a" * (14 * MIB)

    def foreign_lands_before_complete(key: str, uid: str) -> None:
        fake.objects[key] = (foreign, '"foreign-newgen"', {GEN: "not-our-token"})

    fake.on_before_complete = foreign_lands_before_complete
    fake.abort_transient_failures = 1  # inline abort fails; the fresh-client abort then succeeds
    await c.store_recording_prefix(URI, tmpfile, 11 * MIB, serialize_key=URI)
    assert fake.abort_attempts == ["mpu-1", "mpu-1"]  # inline attempt + fresh-client retry (same id)
    assert fake.aborted == ["mpu-1"] and "mpu-1" not in fake.mpus  # orphan cleaned up on the fresh client
    assert _obj(fake) == foreign  # newer foreign generation preserved, never overwritten by a Complete/replace
    assert not fake.completed and fake.ops.count("upload_fileobj") == 0  # fenced-conflict path never full-replaces
    assert URI not in c._compose_state  # stale compose state cleared


@pytest.mark.asyncio
async def test_generic_failure_fresh_cleanup_also_fails_fails_closed_no_replacement(tmpfile: str) -> None:
    """If the inline abort AND the fresh-client abort both fail, fail closed: preserve the remote, clear stale
    compose state, and create NO replacement MPU / full PUT on top of the un-aborted orphan. The bucket
    AbortIncompleteMultipartUpload lifecycle rule is the only remaining backstop for the leaked MPU."""
    fake = FakeS3()
    c = _make_client(fake)
    _seed_base(c, fake, 8 * MIB)
    base = _obj(fake)
    _write(tmpfile, 11 * MIB)
    fake.fail["uploadpart"] = _client_error("InternalError", 500)  # non-expired pre-Complete failure
    fake.abort_transient_failures = 99  # both the inline AND the fresh-client abort fail
    await c.store_recording_prefix(URI, tmpfile, 11 * MIB, serialize_key=URI)
    assert fake.abort_attempts == ["mpu-1", "mpu-1"]  # tried inline, then a fresh client
    assert fake.aborted == [] and "mpu-1" in fake.mpus  # neither succeeded -> orphan remains (lifecycle backstop)
    assert _obj(fake) == base  # remote preserved: NO full replacement over the un-aborted orphan
    assert not fake.completed and fake.ops.count("upload_fileobj") == 0  # no replacement / new PUT authored
    assert URI not in c._compose_state  # stale compose state cleared; next step authors a fresh generation


@pytest.mark.asyncio
async def test_completed_no_etag_then_expired_reconcile_then_nosuchupload_abort_is_safe(tmpfile: str) -> None:
    """Full interleaving: Complete succeeds but omits ETag; the reconciliation HEAD then returns an expired
    token, so the transaction refreshes and retries; the refreshed abort of the (already-completed) orphan
    returns NoSuchUpload. That NoSuchUpload must count as cleanup success — no fail-closed skip, no false
    unabortable orphan — leaving a safe remote outcome and no lingering incomplete MPU."""
    be = _GenBackend()
    be.objects[KEY] = (b"\xab" * (8 * MIB), '"seed-8"', {GEN: "prior"})
    be.complete_fail_gens = set()  # gen1 Complete SUCCEEDS (does not raise)
    be.complete_omit_etag = True  # ...but omits ETag -> reconciliation HEAD is consulted
    be.head_expired_gens = {1}  # ...and that HEAD returns an expired token on the stale gen1 client
    c = _make_client_gen(be)
    c._compose_state.set(URI, _ComposeState(8 * MIB, "seed-8"))
    c.refresh_session = lambda: None  # type: ignore[method-assign]
    _write(tmpfile, 11 * MIB)
    await c.store_recording_prefix(URI, tmpfile, 11 * MIB, serialize_key=URI)
    # gen1 Complete landed mpu-1 (our 11 MiB generation), returned no ETag; the reconcile HEAD expired ->
    # refresh + retry. gen2 aborts the orphan mpu-1 -> NoSuchUpload (already completed) -> treated as success.
    assert "mpu-1" in be.abort_attempts_by_gen.get(2, [])  # the orphan abort was attempted on the refreshed client
    assert be.completed == ["mpu-1"]  # the original Complete had actually landed our generation
    assert be.objects[KEY][0] == b"\xab" * (11 * MIB)  # safe remote: our completed generation is preserved
    assert be._n == 2  # gen2 was NOT fail-closed: it proceeded to create a replacement (mpu-2)...
    assert "mpu-2" not in be.mpus  # ...which then conflicted on the moved destination and was aborted (no leak)
    assert URI not in c._compose_state  # conflict path cleared local state; next step reseeds


# --- MUST_FIX 3: compose-state cache is bounded (LRU + TTL) and cleared after the terminal drain -------


def test_compose_state_cache_bounds_by_lru_and_ttl() -> None:
    now = [0.0]
    cache = _ComposeStateCache(max_size=2, ttl_seconds=10, clock=lambda: now[0])
    cache.set("a", _ComposeState(1, "a"))
    cache.set("b", _ComposeState(2, "b"))
    assert cache.get("a").length == 1  # touch a -> a is now most-recently-used
    cache.set("c", _ComposeState(3, "c"))  # over cap -> evict LRU, which is b (not the touched a)
    assert cache.get("b") is None
    assert cache.get("a").length == 1 and cache.get("c").length == 3
    now[0] = 100  # advance past TTL
    assert cache.get("a") is None and cache.get("c") is None  # expired entries are dropped
    assert len(cache) == 0


@pytest.mark.asyncio
async def test_terminal_clears_compose_state_inside_serialized_slot(tmpfile: str) -> None:
    fake = FakeS3()
    c = _make_client(fake)
    _seed_base(c, fake, 8 * MIB)
    assert URI in c._compose_state
    # The terminal finalize clears state AFTER draining prior writes (race-safe), not only pre-emptively.
    await c.upload_file(URI, b"final-bytes", serialize_key=URI, supersede_queued=True)
    assert URI not in c._compose_state


# --- review 3920309806: any serialized full recording write must invalidate compose state ------


@pytest.mark.asyncio
async def test_nonterminal_full_write_invalidates_compose_state_so_next_snapshot_reseeds(tmpfile: str) -> None:
    """A NON-terminal serialized full byte write (supersede_queued=False — e.g. the mid-step byte fallback in
    _sync_video_artifact_after_step) replaces the object/metadata. It must clear the cached compose base too,
    or the next streamed snapshot's conditional copy 412s against the now-stale ETag and is skipped. Prove
    the state is invalidated and the next snapshot full-reseeds and lands."""
    fake = FakeS3()
    c = _make_client(fake)
    _seed_base(c, fake, 8 * MIB)  # compose base cached at etag "seed-8388608"
    assert URI in c._compose_state
    # A serialized, NON-terminal full write replaces the object with a new ETag ("put-N").
    await c.upload_file(URI, b"\x03" * (9 * MIB), serialize_key=URI, supersede_queued=False)
    assert URI not in c._compose_state  # stale compose base invalidated inside the serialized slot
    # The next streamed snapshot must full-reseed (base is gone) and LAND, not 412-skip on the stale ETag.
    _write(tmpfile, 11 * MIB, fill=b"\xcd")
    await c.store_recording_prefix(URI, tmpfile, 11 * MIB, serialize_key=URI)
    assert _obj(fake) == b"\xcd" * (11 * MIB)  # reseeded full replacement landed
    assert not fake.completed  # no compose (no owned base) -> no UploadPartCopy against a stale ETag
    assert c._compose_state.get(URI) is not None  # fresh verified generation seeded for the next step


# --- MUST_FIX 4: never splice an unowned/foreign remote generation --------------------------------------


@pytest.mark.asyncio
async def test_foreign_remote_object_is_not_spliced(tmpfile: str) -> None:
    fake = FakeS3()
    c = _make_client(fake)
    # A duplicate/zombie producer full-PUT a foreign >=5 MiB generation; this process has NO owned state.
    foreign = b"\x07" * (8 * MIB)
    fake.objects[KEY] = (foreign, '"foreign-gen"', {GEN: "foreign-token"})
    _write(tmpfile, 11 * MIB, fill=b"\xcd")
    await c.store_recording_prefix(URI, tmpfile, 11 * MIB, serialize_key=URI)
    # Must be a clean full replacement of OUR bytes — never foreign-prefix copy + our-tail splice.
    assert _obj(fake) == b"\xcd" * (11 * MIB)
    assert not fake.mpus and not fake.completed  # no UploadPartCopy of the foreign prefix
    # And local state is now seeded from OUR authored+verified generation for the next step to compose off.
    assert c._compose_state.get(URI) is not None


@pytest.mark.asyncio
async def test_full_replace_does_not_seed_when_foreign_writer_wins_the_race(tmpfile: str) -> None:
    fake = FakeS3()
    c = _make_client(fake)
    _write(tmpfile, 10 * MIB)
    orig_head = fake.head_object

    async def head_shows_foreign(*, Bucket: str, Key: str) -> dict[str, Any]:
        # A foreign writer overwrote our object between our PUT and this readback (token mismatch).
        fake.objects[Key] = (b"\x02" * (10 * MIB), '"foreign"', {GEN: "not-ours"})
        return await orig_head(Bucket=Bucket, Key=Key)

    fake.head_object = head_shows_foreign  # type: ignore[method-assign]
    await c.store_recording_prefix(URI, tmpfile, 10 * MIB, serialize_key=URI)
    # Readback token/length did not match ours -> do NOT seed; the next step safely full-replaces again.
    assert URI not in c._compose_state
