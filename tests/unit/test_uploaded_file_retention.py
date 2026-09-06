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
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from skyvern.config import settings
from skyvern.forge.sdk.api import files as files_api
from skyvern.forge.sdk.artifact.storage.s3 import S3Storage
from skyvern.forge.sdk.routes.routers import base_router
from skyvern.forge.sdk.schemas.files import UploadedFile
from skyvern.forge.sdk.schemas.organizations import Organization
from skyvern.schemas.runs import RunEngine
from skyvern.services import uploaded_file_service

VICTIM_ORG_ID = "o_victim"
ATTACKER_ORG_ID = "o_attacker"


def _uri(organization_id: str, filename: str = "secret.pdf") -> str:
    return f"s3://{settings.AWS_S3_BUCKET_UPLOADS}/{settings.ENV}/{organization_id}/2026-08-15/{filename}"


class FakeUploadedFilesRepository:
    """In-memory stand-in with the same org-scoping and soft-delete semantics as the real one."""

    def __init__(self) -> None:
        self.rows: dict[str, UploadedFile] = {}
        self._next_id = 0

    def seed(
        self,
        organization_id: str,
        expires_at: datetime | None = None,
        filename: str = "secret.pdf",
        run_id: str | None = None,
    ) -> str:
        self._next_id += 1
        file_id = f"file_{self._next_id}"
        now = datetime.now(timezone.utc)
        self.rows[file_id] = UploadedFile(
            file_id=file_id,
            organization_id=organization_id,
            storage_uri=_uri(organization_id, filename),
            filename=filename,
            expires_at=expires_at,
            run_id=run_id,
            created_at=now,
            modified_at=now,
        )
        return file_id

    def live_ids(self) -> set[str]:
        return {file_id for file_id, row in self.rows.items() if row.deleted_at is None}

    async def create_uploaded_file(
        self,
        file_id: str,
        organization_id: str,
        storage_uri: str,
        filename: str,
        size_bytes: int | None = None,
        expires_at: datetime | None = None,
    ) -> UploadedFile:
        now = datetime.now(timezone.utc)
        for row in self.rows.values():
            if row.organization_id == organization_id and row.storage_uri == storage_uri and row.deleted_at is None:
                row.deleted_at = now
        self.rows[file_id] = UploadedFile(
            file_id=file_id,
            organization_id=organization_id,
            storage_uri=storage_uri,
            filename=filename,
            size_bytes=size_bytes,
            expires_at=expires_at,
            created_at=now,
            modified_at=now,
        )
        return self.rows[file_id]

    async def get_uploaded_file(self, file_id: str, organization_id: str) -> UploadedFile | None:
        row = self.rows.get(file_id)
        if row is None or row.organization_id != organization_id or row.deleted_at is not None:
            return None
        return row

    async def claim_uploaded_file_for_deletion(self, file_id: str, organization_id: str) -> UploadedFile | None:
        row = await self.get_uploaded_file(file_id, organization_id)
        if row is None:
            return None
        row.deleted_at = datetime.now(timezone.utc)
        return row

    async def get_expired_uploaded_files(self, before: datetime, limit: int = 500) -> list[UploadedFile]:
        return [
            row
            for row in self.rows.values()
            if row.deleted_at is None and row.expires_at is not None and row.expires_at <= before
        ][:limit]

    async def get_uploaded_files_by_ids(self, file_ids: list[str], organization_id: str) -> list[UploadedFile]:
        return [
            row
            for file_id in file_ids
            if (row := self.rows.get(file_id)) is not None
            and row.organization_id == organization_id
            and row.deleted_at is None
        ]

    async def get_uploaded_files_for_run(self, run_id: str) -> list[UploadedFile]:
        return [row for row in self.rows.values() if row.run_id == run_id and row.deleted_at is None]

    async def attach_uploaded_files_to_run(
        self,
        file_ids: list[str],
        organization_id: str,
        run_id: str,
        expires_at: datetime,
    ) -> list[UploadedFile]:
        attached = []
        for file_id in file_ids:
            row = self.rows.get(file_id)
            if row is None or row.organization_id != organization_id or row.deleted_at is not None:
                continue
            if row.run_id is not None and row.run_id != run_id:
                continue
            row.run_id = run_id
            row.expires_at = min(row.expires_at, expires_at) if row.expires_at else expires_at
            attached.append(row)
        return attached


class FakeStorage:
    """Enforces the same org-prefix rule the real storage backends do."""

    def __init__(self) -> None:
        self.deleted: list[str] = []
        self.fail_with: Exception | None = None
        # URIs whose object is already gone. Deleting one succeeds without deleting
        # anything, as S3, GCS, Azure and local storage all do for an absent key.
        self.absent: set[str] = set()

    async def delete_legacy_file(self, *, organization_id: str, uri: str) -> None:
        if not uri.startswith(f"s3://{settings.AWS_S3_BUCKET_UPLOADS}/{settings.ENV}/{organization_id}/"):
            raise PermissionError(f"No permission to access storage URI: {uri}")
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


def _client() -> TestClient:
    test_app = FastAPI()
    test_app.include_router(base_router, prefix="/v1")
    return TestClient(test_app)


class TestDeleteEndpoint:
    def _request(self, file_id: str, caller_org_id: str, storage: FakeStorage) -> object:
        with (
            patch("skyvern.forge.sdk.routes.agent_protocol.app", MagicMock()),
            patch(
                "skyvern.forge.sdk.services.org_auth_service.get_current_org_cached",
                new=AsyncMock(return_value=_make_org(caller_org_id)),
            ),
        ):
            return _client().delete(f"/v1/files/{file_id}", headers={"x-api-key": "key"})

    def test_deletes_the_bytes_at_the_uri_recorded_by_the_server(
        self, service_app: object, repo: FakeUploadedFilesRepository, storage: FakeStorage
    ) -> None:
        file_id = repo.seed(VICTIM_ORG_ID)

        resp = self._request(file_id, VICTIM_ORG_ID, storage)

        assert resp.status_code == 204  # type: ignore[attr-defined]
        assert storage.deleted == [_uri(VICTIM_ORG_ID)]
        assert repo.live_ids() == set()

    def test_another_orgs_file_id_is_a_404_that_deletes_nothing(
        self, service_app: object, repo: FakeUploadedFilesRepository, storage: FakeStorage
    ) -> None:
        """The whole authorization boundary: a guessed id from another tenant must be inert."""
        victim_file_id = repo.seed(VICTIM_ORG_ID)

        resp = self._request(victim_file_id, ATTACKER_ORG_ID, storage)

        assert resp.status_code == 404  # type: ignore[attr-defined]
        assert storage.deleted == []
        assert repo.live_ids() == {victim_file_id}

    def test_a_cross_org_id_is_indistinguishable_from_one_that_never_existed(
        self, service_app: object, repo: FakeUploadedFilesRepository, storage: FakeStorage
    ) -> None:
        """Otherwise the endpoint is an oracle for which file ids exist in other orgs."""
        victim_file_id = repo.seed(VICTIM_ORG_ID)

        cross_org = self._request(victim_file_id, ATTACKER_ORG_ID, storage)
        repo.rows.clear()
        never_existed = self._request(victim_file_id, ATTACKER_ORG_ID, storage)

        assert cross_org.status_code == never_existed.status_code == 404  # type: ignore[attr-defined]
        assert cross_org.json() == never_existed.json()  # type: ignore[attr-defined]

    def test_deleting_the_same_file_twice_is_a_404_not_a_second_delete(
        self, service_app: object, repo: FakeUploadedFilesRepository, storage: FakeStorage
    ) -> None:
        file_id = repo.seed(VICTIM_ORG_ID)

        assert self._request(file_id, VICTIM_ORG_ID, storage).status_code == 204  # type: ignore[attr-defined]
        assert self._request(file_id, VICTIM_ORG_ID, storage).status_code == 404  # type: ignore[attr-defined]
        assert storage.deleted == [_uri(VICTIM_ORG_ID)]


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

    def test_upload_rejects_a_bad_retention_before_writing_any_bytes(self) -> None:
        with (
            patch("skyvern.forge.sdk.routes.agent_protocol.app") as app_module,
            patch(
                "skyvern.forge.sdk.services.org_auth_service.get_current_org_cached",
                new=AsyncMock(return_value=_make_org(VICTIM_ORG_ID)),
            ),
        ):
            app_module.SETTINGS_MANAGER.MAX_UPLOAD_FILE_SIZE = settings.MAX_UPLOAD_FILE_SIZE
            app_module.STORAGE.save_legacy_file = AsyncMock()
            resp = _client().post(
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

    def test_two_uploads_of_a_pathlike_filename_get_distinct_storage_keys(self) -> None:
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
                return ("https://presigned.example/x", _uri(organization_id, storage_key))

            async def fake_create_uploaded_file(**kwargs: object) -> UploadedFile:
                now = datetime.now(timezone.utc)
                return UploadedFile(created_at=now, modified_at=now, **kwargs)  # type: ignore[arg-type]

            app_module.STORAGE.save_legacy_file = AsyncMock(side_effect=fake_save_legacy_file)
            app_module.DATABASE.uploaded_files.create_uploaded_file = AsyncMock(side_effect=fake_create_uploaded_file)

            def upload() -> object:
                return _client().post(
                    "/v1/upload_file",
                    headers={"x-api-key": "key"},
                    files={"file": ("dir/report.pdf", io.BytesIO(b"data"), "application/pdf")},
                )

            first, second = upload(), upload()

            assert first.status_code == 200, first.text  # type: ignore[attr-defined]
            assert second.status_code == 200, second.text  # type: ignore[attr-defined]
            assert storage_keys[0] != storage_keys[1]
            assert first.json()["s3_uri"] != second.json()["s3_uri"]  # type: ignore[attr-defined]
            assert first.json()["file_id"] != second.json()["file_id"]  # type: ignore[attr-defined]


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
        assert storage.deleted == [_uri(VICTIM_ORG_ID, "expired.csv")]
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
        storage.absent.add(_uri(VICTIM_ORG_ID, "lifecycle-expired.csv"))

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
        assert storage.deleted == [_uri(VICTIM_ORG_ID, "cv.pdf")]
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

        assert await files_api.resolve_uploaded_file_id(file_id, VICTIM_ORG_ID) == _uri(VICTIM_ORG_ID, "cv.pdf")
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
            await s3_storage.delete_legacy_file(organization_id=ATTACKER_ORG_ID, uri=_uri(VICTIM_ORG_ID))

        s3_storage.async_client.delete_file.assert_not_awaited()

    def test_a_traversal_segment_does_not_satisfy_the_org_prefix(self) -> None:
        """`{env}/{attacker}/../{victim}/x` starts with the attacker's prefix as a raw string."""
        traversal = f"s3://{settings.AWS_S3_BUCKET_UPLOADS}/{settings.ENV}/{ATTACKER_ORG_ID}/../{VICTIM_ORG_ID}/x.pdf"

        with pytest.raises(PermissionError):
            S3Storage().assert_managed_file_access(traversal, ATTACKER_ORG_ID)

    def test_the_orgs_own_file_is_still_reachable(self) -> None:
        """The traversal guard must not reject ordinary keys."""
        S3Storage().assert_managed_file_access(_uri(ATTACKER_ORG_ID), ATTACKER_ORG_ID)
