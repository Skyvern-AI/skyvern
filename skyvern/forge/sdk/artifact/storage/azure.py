import os
import shutil
import uuid
from datetime import datetime, timezone
from typing import BinaryIO

import structlog

from skyvern.config import settings
from skyvern.constants import BROWSER_DOWNLOADING_SUFFIX, DOWNLOAD_FILE_PREFIX
from skyvern.forge import app
from skyvern.forge.sdk.api.azure import AzureUri, StandardBlobTier
from skyvern.forge.sdk.api.files import (
    calculate_sha256_for_file,
    create_named_temporary_file,
    get_download_dir,
    get_skyvern_temp_dir,
    make_temp_directory,
    unzip_files,
)
from skyvern.forge.sdk.api.real_azure import RealAsyncAzureStorageClient
from skyvern.forge.sdk.artifact.models import Artifact, ArtifactType, LogEntityType
from skyvern.forge.sdk.artifact.storage.base import (
    FILE_EXTENTSION_MAP,
    BaseStorage,
    _file_infos_from_artifacts,
    _file_infos_from_download_artifacts,
)
from skyvern.forge.sdk.models import Step
from skyvern.forge.sdk.schemas.ai_suggestions import AISuggestion
from skyvern.forge.sdk.schemas.files import FileInfo
from skyvern.forge.sdk.schemas.task_v2 import TaskV2, Thought
from skyvern.forge.sdk.schemas.workflow_runs import WorkflowRunBlock

LOG = structlog.get_logger()


class AzureStorage(BaseStorage):
    _PATH_VERSION = "v1"

    def __init__(
        self,
        container: str | None = None,
        account_name: str | None = None,
        account_key: str | None = None,
    ) -> None:
        self.async_client = RealAsyncAzureStorageClient(account_name=account_name, account_key=account_key)
        self.container = container or settings.AZURE_STORAGE_CONTAINER_ARTIFACTS

    def build_uri(self, *, organization_id: str, artifact_id: str, step: Step, artifact_type: ArtifactType) -> str:
        file_ext = FILE_EXTENTSION_MAP[artifact_type]
        return f"{self._build_base_uri(organization_id)}/{step.task_id}/{step.order:02d}_{step.retry_index}_{step.step_id}/{datetime.utcnow().isoformat()}_{artifact_id}_{artifact_type}.{file_ext}"

    async def retrieve_global_workflows(self) -> list[str]:
        uri = f"azure://{self.container}/{settings.ENV}/global_workflows.txt"
        data = await self.async_client.download_file(uri, log_exception=False)
        if not data:
            return []
        return [line.strip() for line in data.decode("utf-8").split("\n") if line.strip()]

    def _build_base_uri(self, organization_id: str) -> str:
        return f"azure://{self.container}/{self._PATH_VERSION}/{settings.ENV}/{organization_id}"

    def build_log_uri(
        self, *, organization_id: str, log_entity_type: LogEntityType, log_entity_id: str, artifact_type: ArtifactType
    ) -> str:
        file_ext = FILE_EXTENTSION_MAP[artifact_type]
        return f"{self._build_base_uri(organization_id)}/logs/{log_entity_type}/{log_entity_id}/{datetime.utcnow().isoformat()}_{artifact_type}.{file_ext}"

    def build_thought_uri(
        self, *, organization_id: str, artifact_id: str, thought: Thought, artifact_type: ArtifactType
    ) -> str:
        file_ext = FILE_EXTENTSION_MAP[artifact_type]
        return f"{self._build_base_uri(organization_id)}/observers/{thought.observer_cruise_id}/{thought.observer_thought_id}/{datetime.utcnow().isoformat()}_{artifact_id}_{artifact_type}.{file_ext}"

    def build_task_v2_uri(
        self, *, organization_id: str, artifact_id: str, task_v2: TaskV2, artifact_type: ArtifactType
    ) -> str:
        file_ext = FILE_EXTENTSION_MAP[artifact_type]
        return f"{self._build_base_uri(organization_id)}/observers/{task_v2.observer_cruise_id}/{datetime.utcnow().isoformat()}_{artifact_id}_{artifact_type}.{file_ext}"

    def build_workflow_run_block_uri(
        self,
        *,
        organization_id: str,
        artifact_id: str,
        workflow_run_block: WorkflowRunBlock,
        artifact_type: ArtifactType,
    ) -> str:
        file_ext = FILE_EXTENTSION_MAP[artifact_type]
        return f"{self._build_base_uri(organization_id)}/workflow_runs/{workflow_run_block.workflow_run_id}/{workflow_run_block.workflow_run_block_id}/{datetime.utcnow().isoformat()}_{artifact_id}_{artifact_type}.{file_ext}"

    def build_ai_suggestion_uri(
        self, *, organization_id: str, artifact_id: str, ai_suggestion: AISuggestion, artifact_type: ArtifactType
    ) -> str:
        file_ext = FILE_EXTENTSION_MAP[artifact_type]
        return f"{self._build_base_uri(organization_id)}/ai_suggestions/{ai_suggestion.ai_suggestion_id}/{datetime.utcnow().isoformat()}_{artifact_id}_{artifact_type}.{file_ext}"

    def build_script_file_uri(
        self, *, organization_id: str, script_id: str, script_version: int, file_path: str
    ) -> str:
        """Build the Azure URI for a script file."""
        return f"{self._build_base_uri(organization_id)}/scripts/{script_id}/{script_version}/{file_path}"

    async def store_artifact(self, artifact: Artifact, data: bytes) -> None:
        tier = await self._get_storage_tier_for_org(artifact.organization_id)
        tags = await self._get_tags_for_org(artifact.organization_id)
        LOG.debug(
            "Storing artifact",
            artifact_id=artifact.artifact_id,
            organization_id=artifact.organization_id,
            uri=artifact.uri,
            storage_tier=tier,
            tags=tags,
        )
        await self.async_client.upload_file(artifact.uri, data, tier=tier, tags=tags)

    async def _get_storage_tier_for_org(self, organization_id: str) -> StandardBlobTier:
        return StandardBlobTier.HOT

    async def _get_tags_for_org(self, organization_id: str) -> dict[str, str]:
        return {}

    async def retrieve_artifact(self, artifact: Artifact) -> bytes | None:
        return await self.async_client.download_file(artifact.uri)

    async def get_share_link(self, artifact: Artifact) -> str | None:
        share_urls = await self.async_client.create_sas_urls([artifact.uri])
        return share_urls[0] if share_urls else None

    async def get_share_links(self, artifacts: list[Artifact]) -> list[str] | None:
        return await self.async_client.create_sas_urls([artifact.uri for artifact in artifacts])

    async def store_artifact_from_path(self, artifact: Artifact, path: str) -> None:
        tier = await self._get_storage_tier_for_org(artifact.organization_id)
        tags = await self._get_tags_for_org(artifact.organization_id)
        LOG.debug(
            "Storing artifact from path",
            artifact_id=artifact.artifact_id,
            organization_id=artifact.organization_id,
            uri=artifact.uri,
            storage_tier=tier,
            path=path,
            tags=tags,
        )
        await self.async_client.upload_file_from_path(artifact.uri, path, tier=tier, tags=tags)

    async def save_streaming_file(self, organization_id: str, file_name: str) -> None:
        from_path = f"{get_skyvern_temp_dir()}/{organization_id}/{file_name}"
        to_path = f"azure://{settings.AZURE_STORAGE_CONTAINER_SCREENSHOTS}/{settings.ENV}/{organization_id}/{file_name}"
        tier = await self._get_storage_tier_for_org(organization_id)
        tags = await self._get_tags_for_org(organization_id)
        LOG.debug(
            "Saving streaming file",
            organization_id=organization_id,
            file_name=file_name,
            from_path=from_path,
            to_path=to_path,
            storage_tier=tier,
            tags=tags,
        )
        await self.async_client.upload_file_from_path(to_path, from_path, tier=tier, tags=tags)

    async def get_streaming_file(self, organization_id: str, file_name: str, use_default: bool = True) -> bytes | None:
        path = f"azure://{settings.AZURE_STORAGE_CONTAINER_SCREENSHOTS}/{settings.ENV}/{organization_id}/{file_name}"
        return await self.async_client.download_file(path, log_exception=False)

    async def store_browser_session(self, organization_id: str, workflow_permanent_id: str, directory: str) -> None:
        # Zip the directory to a temp file
        temp_zip_file = create_named_temporary_file()
        zip_file_path = shutil.make_archive(temp_zip_file.name, "zip", directory)
        browser_session_uri = f"azure://{settings.AZURE_STORAGE_CONTAINER_BROWSER_SESSIONS}/{settings.ENV}/{organization_id}/{workflow_permanent_id}.zip"
        tier = await self._get_storage_tier_for_org(organization_id)
        tags = await self._get_tags_for_org(organization_id)
        LOG.debug(
            "Storing browser session",
            organization_id=organization_id,
            workflow_permanent_id=workflow_permanent_id,
            zip_file_path=zip_file_path,
            browser_session_uri=browser_session_uri,
            storage_tier=tier,
            tags=tags,
        )
        await self.async_client.upload_file_from_path(browser_session_uri, zip_file_path, tier=tier, tags=tags)

    async def retrieve_browser_session(self, organization_id: str, workflow_permanent_id: str) -> str | None:
        browser_session_uri = f"azure://{settings.AZURE_STORAGE_CONTAINER_BROWSER_SESSIONS}/{settings.ENV}/{organization_id}/{workflow_permanent_id}.zip"
        downloaded_zip_bytes = await self.async_client.download_file(browser_session_uri, log_exception=True)
        if not downloaded_zip_bytes:
            return None
        temp_zip_file = create_named_temporary_file(delete=False)
        temp_zip_file.write(downloaded_zip_bytes)
        temp_zip_file_path = temp_zip_file.name

        temp_dir = make_temp_directory(prefix="skyvern_browser_session_")
        unzip_files(temp_zip_file_path, temp_dir)
        temp_zip_file.close()
        return temp_dir

    async def delete_browser_session(self, organization_id: str, workflow_permanent_id: str) -> None:
        browser_session_uri = f"azure://{settings.AZURE_STORAGE_CONTAINER_BROWSER_SESSIONS}/{settings.ENV}/{organization_id}/{workflow_permanent_id}.zip"
        LOG.info(
            "Deleting persisted browser session",
            organization_id=organization_id,
            workflow_permanent_id=workflow_permanent_id,
            browser_session_uri=browser_session_uri,
        )
        await self.async_client.delete_file(browser_session_uri)

    async def store_browser_profile(self, organization_id: str, profile_id: str, directory: str) -> None:
        """Store browser profile to Azure."""
        temp_zip_file = create_named_temporary_file()
        zip_file_path = shutil.make_archive(temp_zip_file.name, "zip", directory)
        profile_uri = f"azure://{settings.AZURE_STORAGE_CONTAINER_BROWSER_SESSIONS}/{settings.ENV}/{organization_id}/profiles/{profile_id}.zip"
        tier = await self._get_storage_tier_for_org(organization_id)
        tags = await self._get_tags_for_org(organization_id)
        LOG.debug(
            "Storing browser profile",
            organization_id=organization_id,
            profile_id=profile_id,
            zip_file_path=zip_file_path,
            profile_uri=profile_uri,
            storage_tier=tier,
            tags=tags,
        )
        await self.async_client.upload_file_from_path(profile_uri, zip_file_path, tier=tier, tags=tags)

    async def retrieve_browser_profile(self, organization_id: str, profile_id: str) -> str | None:
        """Retrieve browser profile from Azure."""
        profile_uri = f"azure://{settings.AZURE_STORAGE_CONTAINER_BROWSER_SESSIONS}/{settings.ENV}/{organization_id}/profiles/{profile_id}.zip"
        downloaded_zip_bytes = await self.async_client.download_file(profile_uri, log_exception=True)
        if not downloaded_zip_bytes:
            return None
        temp_zip_file = create_named_temporary_file(delete=False)
        temp_zip_file.write(downloaded_zip_bytes)
        temp_zip_file_path = temp_zip_file.name

        temp_dir = make_temp_directory(prefix="skyvern_browser_profile_")
        unzip_files(temp_zip_file_path, temp_dir)
        temp_zip_file.close()
        return temp_dir

    async def list_downloaded_files_in_browser_session(
        self, organization_id: str, browser_session_id: str
    ) -> list[str]:
        """Return Azure URIs of completed downloads. DB-backed; mirrors s3.py."""
        return await self._list_downloads_for_session(
            organization_id=organization_id,
            browser_session_id=browser_session_id,
            in_progress=False,
        )

    async def _list_downloads_for_session(
        self,
        *,
        organization_id: str,
        browser_session_id: str,
        in_progress: bool,
    ) -> list[str]:
        """Shared DB-backed lister with a partial-vs-final discriminator."""
        if settings.ARTIFACT_CONTENT_HMAC_KEYRING:
            try:
                artifacts = await app.DATABASE.artifacts.list_artifacts_for_browser_session_by_type(
                    browser_session_id=browser_session_id,
                    organization_id=organization_id,
                    artifact_type=ArtifactType.DOWNLOAD,
                )
            except Exception:
                LOG.warning(
                    "Failed to list browser-session download artifacts; falling back to Azure LIST",
                    organization_id=organization_id,
                    browser_session_id=browser_session_id,
                    in_progress=in_progress,
                    exc_info=True,
                )
                artifacts = None
            if artifacts is not None:
                return [a.uri for a in artifacts if a.uri and a.uri.endswith(BROWSER_DOWNLOADING_SUFFIX) == in_progress]

        uri = f"azure://{settings.AZURE_STORAGE_CONTAINER_ARTIFACTS}/v1/{settings.ENV}/{organization_id}/browser_sessions/{browser_session_id}/downloads"
        files = [
            f"azure://{settings.AZURE_STORAGE_CONTAINER_ARTIFACTS}/{file}"
            for file in await self.async_client.list_files(uri=uri)
        ]
        return [f for f in files if f.endswith(BROWSER_DOWNLOADING_SUFFIX) == in_progress]

    async def get_shared_downloaded_files_in_browser_session(
        self, organization_id: str, browser_session_id: str
    ) -> list[FileInfo]:
        # Artifact-first when keyring is configured — see s3.py for rationale.
        if settings.ARTIFACT_CONTENT_HMAC_KEYRING:
            try:
                artifacts = await app.DATABASE.artifacts.list_artifacts_for_browser_session_by_type(
                    browser_session_id=browser_session_id,
                    organization_id=organization_id,
                    artifact_type=ArtifactType.DOWNLOAD,
                )
            except Exception:
                LOG.warning(
                    "Failed to look up browser-session download artifacts; falling back to SAS URLs",
                    organization_id=organization_id,
                    browser_session_id=browser_session_id,
                    exc_info=True,
                )
                artifacts = []
            # Filter out in-progress partials — user-facing listing must only
            # show completed downloads. Mirrors s3.py.
            artifacts = [a for a in artifacts if a.uri and not a.uri.endswith(BROWSER_DOWNLOADING_SUFFIX)]
            if artifacts:
                return await _file_infos_from_download_artifacts(artifacts)

        return await self._get_shared_downloaded_files_in_browser_session_via_listing(
            organization_id=organization_id, browser_session_id=browser_session_id
        )

    async def _get_shared_downloaded_files_in_browser_session_via_listing(
        self, *, organization_id: str, browser_session_id: str
    ) -> list[FileInfo]:
        # Direct Azure LIST: legacy fallback for sessions pre-cutover.
        # ``list_downloaded_files_in_browser_session`` is now DB-backed, so
        # we can't reuse it here.
        listing_uri = f"azure://{settings.AZURE_STORAGE_CONTAINER_ARTIFACTS}/v1/{settings.ENV}/{organization_id}/browser_sessions/{browser_session_id}/downloads"
        object_keys = [
            f"azure://{settings.AZURE_STORAGE_CONTAINER_ARTIFACTS}/{file}"
            for file in await self.async_client.list_files(uri=listing_uri)
            if not file.endswith(BROWSER_DOWNLOADING_SUFFIX)
        ]
        if len(object_keys) == 0:
            return []

        file_infos: list[FileInfo] = []
        for key in object_keys:
            metadata = {}
            modified_at: datetime | None = None
            # Get metadata (including checksum)
            try:
                object_info = await self.async_client.get_object_info(key)
                if object_info:
                    metadata = object_info.get("Metadata", {})
                    modified_at = object_info.get("LastModified")
            except Exception:
                LOG.exception("Object info retrieval failed", uri=key)

            # Create FileInfo object
            filename = os.path.basename(key)
            checksum = metadata.get("sha256_checksum") if metadata else None

            # Get SAS URL
            sas_urls = await self.async_client.create_sas_urls([key])
            if not sas_urls:
                continue

            file_info = FileInfo(
                url=sas_urls[0],
                checksum=checksum,
                filename=metadata.get("original_filename", filename) if metadata else filename,
                modified_at=modified_at,
            )
            file_infos.append(file_info)

        return file_infos

    async def list_downloading_files_in_browser_session(
        self, organization_id: str, browser_session_id: str
    ) -> list[str]:
        """Return Azure URIs of in-progress (``*.crdownload``) downloads. DB-backed."""
        return await self._list_downloads_for_session(
            organization_id=organization_id,
            browser_session_id=browser_session_id,
            in_progress=True,
        )

    async def get_shared_recordings_in_browser_session(
        self, organization_id: str, browser_session_id: str
    ) -> list[FileInfo]:
        """Get recording files for a browser session.

        Artifact-first when the keyring is configured — see s3.py for the
        rationale. Falls back to direct Azure LIST + SAS URLs for legacy
        sessions and OSS-default deployments.
        """
        if settings.ARTIFACT_CONTENT_HMAC_KEYRING:
            try:
                artifacts = await app.DATABASE.artifacts.list_artifacts_for_browser_session_by_type(
                    browser_session_id=browser_session_id,
                    organization_id=organization_id,
                    artifact_type=ArtifactType.RECORDING,
                )
            except Exception:
                LOG.warning(
                    "Failed to look up browser-session recording artifacts; falling back to SAS URLs",
                    organization_id=organization_id,
                    browser_session_id=browser_session_id,
                    exc_info=True,
                )
                artifacts = []
            artifacts = [
                a for a in artifacts if a.uri and (a.uri.lower().endswith(".webm") or a.uri.lower().endswith(".mp4"))
            ]
            if artifacts:
                file_infos = await _file_infos_from_artifacts(artifacts, artifact_type=ArtifactType.RECORDING)
                file_infos.sort(key=lambda f: (f.modified_at is not None, f.modified_at), reverse=True)
                return file_infos

        # Legacy fallback: keyring unset, DB raised, or session pre-cutover
        # with no rows at all. SKY-9286: drop entirely after the bake-in
        # window (target 2026-05-03) — every call here is a billable
        # ListBlobs request.
        return await self._get_shared_recordings_in_browser_session_via_listing(
            organization_id=organization_id, browser_session_id=browser_session_id
        )

    async def _get_shared_recordings_in_browser_session_via_listing(
        self, *, organization_id: str, browser_session_id: str
    ) -> list[FileInfo]:
        # Direct Azure LIST: legacy fallback for pre-cutover sessions and
        # OSS deployments without a keyring.
        # SKY-9286: scheduled for removal once production sessions all have
        # rows — every call here is a billable ListBlobs request.
        listing_uri = f"azure://{settings.AZURE_STORAGE_CONTAINER_ARTIFACTS}/v1/{settings.ENV}/{organization_id}/browser_sessions/{browser_session_id}/videos"
        object_keys = [
            f"azure://{settings.AZURE_STORAGE_CONTAINER_ARTIFACTS}/{file}"
            for file in await self.async_client.list_files(uri=listing_uri)
        ]
        if len(object_keys) == 0:
            return []

        file_infos: list[FileInfo] = []
        for key in object_keys:
            # Playwright's record_video_dir should only contain .webm files.
            # Filter defensively in case of unexpected files.
            key_lower = key.lower()
            if not (key_lower.endswith(".webm") or key_lower.endswith(".mp4")):
                LOG.warning(
                    "Skipping recording file with unsupported extension",
                    uri=key,
                    organization_id=organization_id,
                    browser_session_id=browser_session_id,
                )
                continue

            metadata = {}
            modified_at: datetime | None = None
            content_length: int | None = None
            # Get metadata (including checksum)
            try:
                object_info = await self.async_client.get_object_info(key)
                if object_info:
                    metadata = object_info.get("Metadata", {})
                    modified_at = object_info.get("LastModified")
                    content_length = object_info.get("ContentLength") or object_info.get("Size")
            except Exception:
                LOG.exception("Recording object info retrieval failed", uri=key)

            # Skip zero-byte objects (if any incomplete uploads)
            if content_length == 0:
                continue

            # Create FileInfo object
            filename = os.path.basename(key)
            checksum = metadata.get("sha256_checksum") if metadata else None

            # Get SAS URL
            sas_urls = await self.async_client.create_sas_urls([key])
            if not sas_urls:
                continue

            file_info = FileInfo(
                url=sas_urls[0],
                checksum=checksum,
                filename=metadata.get("original_filename", filename) if metadata else filename,
                modified_at=modified_at,
            )
            file_infos.append(file_info)

        # Prefer the newest recording first (Azure list order is not guaranteed).
        # Treat None as "oldest".
        file_infos.sort(key=lambda f: (f.modified_at is not None, f.modified_at), reverse=True)
        return file_infos

    async def save_downloaded_files(
        self,
        organization_id: str,
        run_id: str | None,
    ) -> None:
        tier = await self._get_storage_tier_for_org(organization_id)
        tags = await self._get_tags_for_org(organization_id)
        base_uri = f"azure://{settings.AZURE_STORAGE_CONTAINER_UPLOADS}/{DOWNLOAD_FILE_PREFIX}/{settings.ENV}/{organization_id}/{run_id}"

        await self._save_downloaded_files_from_local(
            organization_id=organization_id,
            base_uri=base_uri,
            run_id=run_id,
            tier=tier,
            tags=tags,
        )

    async def _save_downloaded_files_from_local(
        self,
        organization_id: str,
        base_uri: str,
        run_id: str | None,
        tier: StandardBlobTier,
        tags: dict[str, str] | None,
    ) -> None:
        """Save files from local download directory to Azure."""
        download_dir = get_download_dir(run_id=run_id)
        files = os.listdir(download_dir)
        for file in files:
            fpath = os.path.join(download_dir, file)
            if not os.path.isfile(fpath):
                continue
            uri = f"{base_uri}/{file}"
            checksum = calculate_sha256_for_file(fpath)
            # Azure Blob metadata values must be ASCII; preserve the full
            # filename via the blob path / Artifact URI instead.
            metadata: dict[str, str] = {"sha256_checksum": checksum}
            if file.isascii():
                metadata["original_filename"] = file
            # Catch upload failures so we never create an Artifact row for
            # bytes that didn't actually land in storage.
            try:
                await self.async_client.upload_file_from_path(
                    uri=uri,
                    file_path=fpath,
                    metadata=metadata,
                    tier=tier,
                    tags=tags,
                )
            except Exception:
                LOG.warning(
                    "Skipping downloaded file — Azure upload failed",
                    file=file,
                    organization_id=organization_id,
                    run_id=run_id,
                    exc_info=True,
                )
                continue

            # Register the file as an Artifact so GET run output can serve it via
            # the signed /v1/artifacts/{id}/content endpoint (SKY-8861). Persist
            # the SHA-256 we already computed so retrieval doesn't need an
            # extra blob HEAD per file.
            if run_id is not None:
                try:
                    await app.ARTIFACT_MANAGER.create_download_artifact(
                        organization_id=organization_id,
                        run_id=run_id,
                        uri=uri,
                        filename=file,
                        checksum=checksum,
                    )
                except Exception:
                    LOG.warning(
                        "Failed to register downloaded file as artifact; falling back to SAS URLs for retrieval",
                        file=file,
                        organization_id=organization_id,
                        run_id=run_id,
                        exc_info=True,
                    )

    async def get_downloaded_files(self, organization_id: str, run_id: str | None) -> list[FileInfo]:
        # Artifact-first — see s3.py::get_downloaded_files for rationale. When
        # the keyring isn't configured (OSS default) or no artifact rows exist
        # (legacy run pre-SKY-8861) we fall back to the legacy listing path so
        # downloaded files remain reachable.
        if run_id is not None and settings.ARTIFACT_CONTENT_HMAC_KEYRING:
            artifacts = await self._list_download_artifacts_safe(organization_id=organization_id, run_id=run_id)
            if artifacts:
                return await _file_infos_from_download_artifacts(artifacts)

        return await self._get_downloaded_files_via_blob_listing(organization_id=organization_id, run_id=run_id)

    async def _list_download_artifacts_safe(self, *, organization_id: str, run_id: str) -> list[Artifact]:
        try:
            return await app.DATABASE.artifacts.list_artifacts_for_run_by_type(
                run_id=run_id,
                organization_id=organization_id,
                artifact_type=ArtifactType.DOWNLOAD,
            )
        except Exception:
            LOG.warning(
                "Failed to look up download artifacts; falling back to SAS URLs",
                organization_id=organization_id,
                run_id=run_id,
                exc_info=True,
            )
            return []

    async def _get_downloaded_files_via_blob_listing(
        self, *, organization_id: str, run_id: str | None
    ) -> list[FileInfo]:
        uri = f"azure://{settings.AZURE_STORAGE_CONTAINER_UPLOADS}/{DOWNLOAD_FILE_PREFIX}/{settings.ENV}/{organization_id}/{run_id}"
        object_keys = await self.async_client.list_files(uri=uri)
        if len(object_keys) == 0:
            return []

        file_infos: list[FileInfo] = []
        for key in object_keys:
            object_uri = f"azure://{settings.AZURE_STORAGE_CONTAINER_UPLOADS}/{key}"

            metadata = await self.async_client.get_file_metadata(object_uri, log_exception=False)
            filename = os.path.basename(key)
            checksum = metadata.get("sha256_checksum") if metadata else None
            display_name = metadata.get("original_filename", filename) if metadata else filename

            sas_urls = await self.async_client.create_sas_urls([object_uri])
            if not sas_urls:
                continue

            file_infos.append(
                FileInfo(
                    url=sas_urls[0],
                    checksum=checksum,
                    filename=display_name,
                )
            )

        return file_infos

    async def save_legacy_file(
        self, *, organization_id: str, filename: str, fileObj: BinaryIO
    ) -> tuple[str, str] | None:
        todays_date = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
        container = settings.AZURE_STORAGE_CONTAINER_UPLOADS
        tier = await self._get_storage_tier_for_org(organization_id)
        tags = await self._get_tags_for_org(organization_id)
        # First try uploading with original filename
        try:
            sanitized_filename = os.path.basename(filename)  # Remove any path components
            azure_uri = f"azure://{container}/{settings.ENV}/{organization_id}/{todays_date}/{sanitized_filename}"
            uploaded_uri = await self.async_client.upload_file_stream(azure_uri, fileObj, tier=tier, tags=tags)
        except Exception:
            LOG.error("Failed to upload file to Azure", exc_info=True)
            uploaded_uri = None

        # If upload fails, try again with UUID prefix
        if not uploaded_uri:
            uuid_prefixed_filename = f"{str(uuid.uuid4())}_{filename}"
            azure_uri = f"azure://{container}/{settings.ENV}/{organization_id}/{todays_date}/{uuid_prefixed_filename}"
            fileObj.seek(0)  # Reset file pointer
            uploaded_uri = await self.async_client.upload_file_stream(azure_uri, fileObj, tier=tier, tags=tags)

        if not uploaded_uri:
            LOG.error(
                "Failed to upload file to Azure after retrying with UUID prefix",
                organization_id=organization_id,
                storage_tier=tier,
                filename=filename,
                exc_info=True,
            )
            return None
        LOG.debug(
            "Legacy file upload",
            organization_id=organization_id,
            storage_tier=tier,
            filename=filename,
            uploaded_uri=uploaded_uri,
        )
        # Generate a SAS URL for the uploaded file
        sas_urls = await self.async_client.create_sas_urls([uploaded_uri])
        if not sas_urls:
            LOG.error(
                "Failed to create SAS URL for uploaded file",
                organization_id=organization_id,
                storage_tier=tier,
                uploaded_uri=uploaded_uri,
                filename=filename,
                exc_info=True,
            )
            return None
        return sas_urls[0], uploaded_uri

    def _build_browser_session_uri(
        self,
        organization_id: str,
        browser_session_id: str,
        artifact_type: str,
        remote_path: str,
        date: str | None = None,
    ) -> str:
        """Build the Azure URI for a browser session file."""
        base = f"azure://{self.container}/v1/{settings.ENV}/{organization_id}/browser_sessions/{browser_session_id}/{artifact_type}"
        if date:
            return f"{base}/{date}/{remote_path}"
        return f"{base}/{remote_path}"

    async def sync_browser_session_file(
        self,
        organization_id: str,
        browser_session_id: str,
        artifact_type: str,
        local_file_path: str,
        remote_path: str,
        date: str | None = None,
    ) -> str:
        """Sync a file from local browser session to Azure."""
        uri = self._build_browser_session_uri(organization_id, browser_session_id, artifact_type, remote_path, date)
        tier = await self._get_storage_tier_for_org(organization_id)
        tags = await self._get_tags_for_org(organization_id)
        await self.async_client.upload_file_from_path(uri, local_file_path, tier=tier, tags=tags)

        if artifact_type == "downloads":
            # See s3.py — DB is the single source of truth for the user-facing
            # listing and the agent's baseline / complete_on_download checks.
            # Partials get a row with checksum=None; the row is dropped on
            # Chrome's atomic-rename ``Change.deleted`` event. Exceptions
            # propagate so the watcher's bounded retry can recover from a
            # transient DB outage — both ops are idempotent.
            is_partial = remote_path.endswith(BROWSER_DOWNLOADING_SUFFIX)
            checksum = None if is_partial else calculate_sha256_for_file(local_file_path)
            await app.ARTIFACT_MANAGER.create_browser_session_download_artifact(
                organization_id=organization_id,
                browser_session_id=browser_session_id,
                uri=uri,
                filename=os.path.basename(remote_path),
                checksum=checksum,
            )
        elif artifact_type == "videos":
            # Recording uploaded once at session close — see s3.py. Artifact-
            # row creation is best-effort: the only caller swallows
            # exceptions without retry, so the gated legacy listing fallback
            # in ``get_shared_recordings_in_browser_session`` is the safety
            # net for missed writes (when the session has no RECORDING rows
            # we fall through to the Azure LIST path, so a row-less recording
            # still surfaces via the legacy SAS URL).
            checksum = calculate_sha256_for_file(local_file_path)
            await app.ARTIFACT_MANAGER.create_browser_session_recording_artifact(
                organization_id=organization_id,
                browser_session_id=browser_session_id,
                uri=uri,
                filename=os.path.basename(remote_path),
                checksum=checksum,
            )

        return uri

    async def delete_browser_session_file(
        self,
        organization_id: str,
        browser_session_id: str,
        artifact_type: str,
        remote_path: str,
        date: str | None = None,
    ) -> None:
        """Delete a file from browser session storage in Azure.

        For ``downloads``, also drop the matching DOWNLOAD artifact row so a
        subsequent ``GET /v1/browser_sessions/{id}`` doesn't hand out a signed
        URL that 404s. Mirrors the S3 implementation.
        """
        uri = self._build_browser_session_uri(organization_id, browser_session_id, artifact_type, remote_path, date)
        if artifact_type == "downloads":
            try:
                await app.DATABASE.artifacts.delete_artifact_for_browser_session(
                    organization_id=organization_id,
                    browser_session_id=browser_session_id,
                    uri=uri,
                    artifact_type=ArtifactType.DOWNLOAD,
                )
            except Exception:
                LOG.warning(
                    "Failed to delete browser-session download artifact row; proceeding with Azure delete",
                    organization_id=organization_id,
                    browser_session_id=browser_session_id,
                    remote_path=remote_path,
                    exc_info=True,
                )
        await self.async_client.delete_file(uri)

    async def browser_session_file_exists(
        self,
        organization_id: str,
        browser_session_id: str,
        artifact_type: str,
        remote_path: str,
        date: str | None = None,
    ) -> bool:
        """Check if a file exists in browser session storage in Azure."""
        uri = self._build_browser_session_uri(organization_id, browser_session_id, artifact_type, remote_path, date)
        try:
            info = await self.async_client.get_object_info(uri)
            return info is not None
        except Exception:
            return False

    def assert_managed_file_access(self, uri: str, organization_id: str) -> None:
        try:
            parsed_uri = AzureUri(uri)
        except Exception as e:
            raise PermissionError(f"No permission to access storage URI: {uri}") from e

        # Uploads container: blob paths use {env}/{org}/ or downloads/{env}/{org}/
        if parsed_uri.container == settings.AZURE_STORAGE_CONTAINER_UPLOADS:
            allowed_prefixes = (
                f"{settings.ENV}/{organization_id}/",
                f"{DOWNLOAD_FILE_PREFIX}/{settings.ENV}/{organization_id}/",
            )
            if any(parsed_uri.blob_path.startswith(prefix) for prefix in allowed_prefixes):
                return

        # Artifacts container: blob paths use v1/{env}/{org}/
        if parsed_uri.container == settings.AZURE_STORAGE_CONTAINER_ARTIFACTS:
            artifact_prefix = f"{self._PATH_VERSION}/{settings.ENV}/{organization_id}/"
            if parsed_uri.blob_path.startswith(artifact_prefix):
                return

        raise PermissionError(f"No permission to access storage URI: {uri}")

    async def download_managed_file(self, uri: str, organization_id: str) -> bytes | None:
        """Download a managed org-scoped file from Azure."""
        self.assert_managed_file_access(uri, organization_id)
        return await self.async_client.download_file(uri, log_exception=False)

    async def file_exists(self, uri: str) -> bool:
        """Check if a file exists at the given Azure URI."""
        try:
            info = await self.async_client.get_object_info(uri)
            return info is not None
        except Exception:
            return False

    @property
    def storage_type(self) -> str:
        """Returns 'azure' as the storage type."""
        return "azure"
