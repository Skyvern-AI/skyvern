"""Credential first-save boundary fixtures using real SQLite and archive storage."""

from __future__ import annotations

import asyncio
import io
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock
from zipfile import ZipFile

from botocore.exceptions import ClientError
from fastapi import BackgroundTasks, HTTPException
from sqlalchemy import select

from skyvern.config import settings
from skyvern.forge import app
from skyvern.forge.agent_functions import AgentFunction
from skyvern.forge.sdk.artifact.storage.s3 import S3Storage
from skyvern.forge.sdk.db.agent_db import AgentDB
from skyvern.forge.sdk.db.models import BrowserProfileModel, CredentialModel
from skyvern.forge.sdk.executor.factory import AsyncExecutorFactory
from skyvern.forge.sdk.routes import credentials
from skyvern.forge.sdk.schemas.credentials import CredentialType, CredentialVaultType
from skyvern.forge.sdk.schemas.credentials import TestCredentialRequest as CredentialTestRequest
from skyvern.forge.sdk.schemas.credentials import TotpType, UpdateCredentialRequest
from skyvern.forge.sdk.services.credential import credential_creation_service
from skyvern.forge.sdk.workflow import service as workflow_service
from skyvern.forge.sdk.workflow.models.workflow import WorkflowRunStatus
from skyvern.schemas.runs import ProxyLocation
from skyvern.webeye.browser_artifacts import BrowserArtifacts
from tests.unit.scoped_asyncio import ScopedAsyncio

if TYPE_CHECKING:
    from pytest import MonkeyPatch
    from sqlalchemy.ext.asyncio import AsyncEngine

    from skyvern.forge.sdk.executor.async_executor import AsyncExecutor
    from skyvern.forge.sdk.schemas.credentials import Credential
    from skyvern.webeye.browser_artifacts import CredentialProfileFinalizer
    from skyvern.webeye.browser_state import BrowserState


class ProfileArchiveTransport:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}
        self.profile_uploads: list[tuple[str, str]] = []
        self.downloads: list[tuple[str, str]] = []
        self.before_profile_upload: Callable[[], Awaitable[None]] | None = None

    async def upload_file(self, Filename: str, Bucket: str, Key: str, ExtraArgs: object) -> None:
        if "/profiles/" in Key:
            self.profile_uploads.append((Bucket, Key))
            if self.before_profile_upload is not None:
                await self.before_profile_upload()
        self.objects[Bucket, Key] = Path(Filename).read_bytes()

    async def get_object(self, Bucket: str, Key: str) -> dict[str, object]:
        self.downloads.append((Bucket, Key))
        if (Bucket, Key) not in self.objects:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        data = self.objects[Bucket, Key]

        async def read() -> bytes:
            return data

        return {"Body": SimpleNamespace(read=read)}

    async def delete_object(self, Bucket: str, Key: str, **_options: object) -> dict[str, object]:
        self.objects.pop((Bucket, Key), None)
        return {}

    def saved_cookies(self, profile_id: str) -> bytes:
        (destination,) = (key for key in self.objects if key[1].endswith(f"/profiles/{profile_id}.zip"))
        with ZipFile(io.BytesIO(self.objects[destination])) as archive:
            return archive.read("Default/Network/Cookies")


class CredentialProfileRun:
    def __init__(self, db: AgentDB, storage: S3Storage, monkeypatch: MonkeyPatch, tmp_path: Path) -> None:
        self.db = db
        self.storage = storage
        self.monkeypatch = monkeypatch
        self.temp_dir = tmp_path
        self.transport = ProfileArchiveTransport()
        self.service = workflow_service.WorkflowService()
        self.pending: set[asyncio.Task[None]] = set()
        self.settled = asyncio.Event()
        self.visible_destination_ids: list[str] = []
        self.test_url = "https://example.invalid/login"
        self.vault_items = {"synthetic-first-save"}
        self.vault_deletions: list[str] = []

    async def set_up(self) -> None:
        self.org = await self.db.organizations.create_organization("First-save fixture", derive_slug=False)
        self.org_id = self.org.organization_id
        self.credential = await self.db.credentials.create_credential(
            self.org_id,
            "_test_login_first_save",
            CredentialVaultType.SKYVERN,
            "synthetic-first-save",
            CredentialType.PASSWORD,
            "synthetic@example.invalid",
            TotpType.NONE,
            None,
            None,
        )
        self.workflow = await self.db.workflows.create_workflow(
            title="Synthetic credential test",
            organization_id=self.org_id,
            workflow_definition={"parameters": [], "blocks": []},
            persist_browser_session=True,
        )
        self.run = await self.db.workflow_runs.create_workflow_run(
            workflow_id=self.workflow.workflow_id,
            workflow_permanent_id=self.workflow.workflow_permanent_id,
            organization_id=self.org_id,
            browser_type="chrome",
        )

        @asynccontextmanager
        async def object_client() -> AsyncIterator[ProfileArchiveTransport]:
            yield self.transport

        async def object_operation(_name: str, operation: Callable[[], Awaitable[Any]], **_options: object) -> Any:
            return await operation()

        async def delete_from_vault(credential: Credential) -> None:
            self.vault_deletions.append(credential.item_id)
            self.vault_items.remove(credential.item_id)
            await self.db.credentials.delete_credential(credential.credential_id, credential.organization_id)

        self.monkeypatch.setattr(self.storage.async_client, "_s3_client", object_client)
        self.monkeypatch.setattr(self.storage.async_client, "_s3_with_retry", object_operation)
        self.monkeypatch.setattr(self.storage, "save_downloaded_files", AsyncMock())
        self.monkeypatch.setattr(app, "DATABASE", self.db)
        self.monkeypatch.setattr(app, "STORAGE", self.storage)
        self.monkeypatch.setattr(app, "WORKFLOW_SERVICE", self.service)
        self.monkeypatch.setattr(
            app,
            "WORKFLOW_CONTEXT_MANAGER",
            SimpleNamespace(
                remove_workflow_run_context=lambda _run_id: None,
                has_workflow_run_context=lambda _run_id: False,
            ),
        )
        self.monkeypatch.setattr(settings, "TEMP_PATH", str(self.temp_dir))
        self.monkeypatch.setattr(credentials, "_background_tasks", self.pending)

        async def poll_pause(_seconds: float) -> None:
            await self.settled.wait()
            await asyncio.sleep(0)

        self.monkeypatch.setattr(credentials, "asyncio", ScopedAsyncio(sleep=poll_pause))
        self.monkeypatch.setattr(
            credential_creation_service,
            "get_credential_vault_service",
            AsyncMock(return_value=SimpleNamespace(delete_credential=delete_from_vault)),
        )
        agent_function = AgentFunction()
        self.monkeypatch.setattr(agent_function, "on_workflow_run_terminal", AsyncMock())
        self.monkeypatch.setattr(app, "AGENT_FUNCTION", agent_function)
        self.monkeypatch.setattr(app, "ARTIFACT_MANAGER", SimpleNamespace(wait_for_upload_aiotasks=AsyncMock()))
        self.monkeypatch.setattr(self.service, "create_workflow_from_request", AsyncMock(return_value=self.workflow))
        self.monkeypatch.setattr(self.service, "setup_workflow_run", AsyncMock(return_value=self.run))
        self.monkeypatch.setattr(
            self.service, "resolve_and_persist_reuse_bound_key", AsyncMock(return_value=(self.run, False))
        )
        self.monkeypatch.setattr(self.service, "persist_video_data", AsyncMock())
        self.monkeypatch.setattr(workflow_service, "is_retry_eligible_run", AsyncMock(return_value=False))
        self.monkeypatch.setattr(workflow_service, "annotate_failed_workflow_run", AsyncMock())
        self.monkeypatch.setattr(workflow_service.uploaded_file_service, "delete_files_attached_to_run", AsyncMock())

    def cookies_directory(self, name: str, contents: bytes) -> Path:
        path = self.temp_dir / name
        cookies = path / "Default" / "Network" / "Cookies"
        cookies.parent.mkdir(parents=True)
        cookies.write_bytes(contents)
        return path

    async def finish_capture(
        self,
        finalizer: CredentialProfileFinalizer,
        artifacts: BrowserArtifacts | None = None,
        *,
        write_back: bool = False,
    ) -> None:
        completed = await self.db.workflow_runs.update_workflow_run_if_not_final(
            self.run.workflow_run_id, WorkflowRunStatus.completed
        )
        assert completed is not None and completed.status == WorkflowRunStatus.completed
        if artifacts is None:
            directory = self.cookies_directory("closed-browser", b"captured Edge cookies")
            artifacts = BrowserArtifacts(browser_session_dir=str(directory))
            object.__setattr__(artifacts, "_credential_profile_writer", "msedge")
        cleanup = workflow_service.WorkflowBrowserCleanupResult(
            browser_state=cast("BrowserState", SimpleNamespace(browser_artifacts=artifacts)),
            tasks=[],
            all_workflow_task_ids=[],
            child_workflow_run_ids=[],
            close_browser_on_completion=True,
        )
        await self.service.clean_up_workflow(
            self.workflow,
            completed,
            browser_cleanup_result=cleanup,
            skip_browser_session_write_back=not write_back,
            schedule_credential_fallback_retry=False,
            need_call_webhook=False,
            final_diagnostic_owner=completed,
            credential_profile_finalizer=finalizer,
        )

    async def configure_upload(self, scenario: str) -> str | None:
        temporary = {"upload-error-null": None, "upload-error-temporary": True, "upload-error-linked": True}.get(
            scenario, False
        )
        if temporary is not None:
            await self.db.credentials.update_credential(
                self.credential.credential_id, self.org_id, is_test_login_temporary=temporary
            )
        if scenario == "captured-writer":
            await self.db.credentials.update_credential(
                self.credential.credential_id,
                self.org_id,
                proxy_location=ProxyLocation.RESIDENTIAL_ISP,
                proxy_session_id="test-profile-pin",
            )
        competing_id = None
        if scenario in {"competing-link", "upload-error-linked", "link-aba"}:
            profile = await self.db.browser_sessions.create_browser_profile(self.org_id, "Competing profile")
            competing_id = profile.browser_profile_id
        if scenario in {"competing-unlink", "dangling-link", "unlink-before-capture"}:
            await self.db.credentials.update_credential(
                self.credential.credential_id, self.org_id, browser_profile_id="bp_deleted_before_dispatch"
            )

        async def interleave() -> None:
            key = self.transport.profile_uploads[-1]
            profile_id = Path(key[1]).stem
            if scenario == "failed-put-concurrent-attach":
                try:
                    await credential_creation_service.validate_credential_browser_profile_id(
                        profile_id, self.org_id, self.credential.credential_id
                    )
                except HTTPException as exc:
                    assert exc.status_code == 400
                else:
                    self.visible_destination_ids.append(profile_id)
                    await self.db.credentials.update_credential(
                        self.credential.credential_id, self.org_id, browser_profile_id=profile_id
                    )
                self.transport.objects[key] = b"synthetic partial upload"
                raise OSError("synthetic S3 upload failed after writing partial bytes")
            if scenario == "link-aba":
                for choice in (competing_id, None):
                    await credentials.rename_credential(
                        self.credential.credential_id, UpdateCredentialRequest(browser_profile_id=choice), self.org
                    )
                    assert (await self.credential_row()).browser_profile_id == choice
            elif competing_id is not None or scenario == "competing-unlink":
                await self.db.credentials.update_credential(
                    self.credential.credential_id, self.org_id, browser_profile_id=competing_id
                )
            if scenario.startswith("upload-error"):
                raise OSError("synthetic S3 upload failed")

        self.transport.before_profile_upload = interleave
        return competing_id

    async def invoke(self, executor: AsyncExecutor) -> None:
        self.monkeypatch.setattr(AsyncExecutorFactory, "get_executor", lambda: executor)
        background = BackgroundTasks()
        try:
            response = await credentials.test_credential(
                background,
                credential_id=self.credential.credential_id,
                data=CredentialTestRequest(url=self.test_url, save_browser_profile=True),
                current_org=self.org,
                current_user_id=None,
                x_user_agent=None,
            )
            assert response.workflow_run_id == self.run.workflow_run_id
            await background()
        finally:
            self.settled.set()
            await asyncio.gather(*tuple(self.pending))

    async def profile_rows(self) -> list[BrowserProfileModel]:
        async with self.db.Session() as session:
            return list(await session.scalars(select(BrowserProfileModel).filter_by(organization_id=self.org_id)))

    async def credential_row(self) -> CredentialModel:
        async with self.db.Session() as session:
            row = await session.get(CredentialModel, self.credential.credential_id)
        assert row is not None
        return row

    async def assert_save_attempt(
        self, scenario: str, competing_id: str | None, *, from_saved_session: bool = False
    ) -> None:
        profiles = await self.profile_rows()
        credential = await self.credential_row()
        assert credential.deleted_at is None and credential.item_id in self.vault_items
        assert not self.vault_deletions
        if scenario == "unlink-before-capture":
            assert not profiles and not self.transport.profile_uploads
            assert credential.browser_profile_id is None and credential.tested_url is None
            return
        if scenario == "incomplete-capture":
            assert not profiles and not self.transport.profile_uploads and not self.transport.objects
            assert credential.browser_profile_id is None and credential.tested_url is None
            return
        assert len(self.transport.profile_uploads) == (2 if scenario == "capture-put-fails-fallback" else 1)
        if scenario in {
            "captured-writer",
            "dangling-link",
            "fallback",
            "attached-fallback",
            "writeback-fallback",
            "capture-put-fails-fallback",
        }:
            (profile,) = profiles
            assert profile.deleted_at is None
            assert credential.browser_profile_id == profile.browser_profile_id
            assert credential.tested_url == self.test_url
            assert profile.source_browser_type == (None if from_saved_session else "msedge")
            if scenario == "captured-writer":
                assert profile.proxy_location == credential.proxy_location
                assert profile.proxy_session_id == credential.proxy_session_id == "test-profile-pin"
            assert self.run.browser_type == "chrome"
            assert self.transport.saved_cookies(profile.browser_profile_id) == (
                b"session archive cookies" if from_saved_session else b"captured Edge cookies"
            )
        else:
            assert [row.browser_profile_id for row in profiles] == ([competing_id] if competing_id else [])
            assert credential.browser_profile_id == (None if scenario == "link-aba" else competing_id)
            assert credential.tested_url is None
            if scenario == "failed-put-concurrent-attach":
                assert not self.visible_destination_ids
                assert self.transport.objects[self.transport.profile_uploads[0]] == b"synthetic partial upload"
            else:
                assert self.transport.profile_uploads[0] not in self.transport.objects


async def make_credential_profile_run(
    sqlite_engine: AsyncEngine, monkeypatch: MonkeyPatch, tmp_path: Path
) -> CredentialProfileRun:
    db = AgentDB(str(sqlite_engine.url), db_engine=sqlite_engine)
    fixture = CredentialProfileRun(db, S3Storage(), monkeypatch, tmp_path)
    await fixture.set_up()
    return fixture
