import io
import os
import shutil
import uuid
import zipfile
from datetime import datetime, timezone
from typing import BinaryIO

import structlog
import zstandard as zstd

from skyvern.config import settings
from skyvern.constants import BROWSER_DOWNLOADING_SUFFIX, DOWNLOAD_FILE_PREFIX
from skyvern.forge import app
from skyvern.forge.sdk.api.aws import AsyncAWSClient, S3StorageClass, S3Uri
from skyvern.forge.sdk.api.files import (
    calculate_sha256_for_file,
    create_named_temporary_file,
    get_download_dir,
    get_skyvern_temp_dir,
    make_temp_directory,
    unzip_files,
)
from skyvern.forge.sdk.artifact.models import Artifact, ArtifactType, LogEntityType
from skyvern.forge.sdk.artifact.storage.base import (
    FILE_EXTENTSION_MAP,
    BaseStorage,
    _file_infos_from_download_artifacts,
)
from skyvern.forge.sdk.models import Step
from skyvern.forge.sdk.schemas.ai_suggestions import AISuggestion
from skyvern.forge.sdk.schemas.files import FileInfo
from skyvern.forge.sdk.schemas.task_v2 import TaskV2, Thought
from skyvern.forge.sdk.schemas.workflow_runs import WorkflowRunBlock

LOG = structlog.get_logger()


S3_ZSTD_COMPRESSED_SUFFIX = ".zst"


class S3Storage(BaseStorage):
    _PATH_VERSION = "v1"

    def __init__(self, bucket: str | None = None, endpoint_url: str | None = None) -> None:
        self.async_client = AsyncAWSClient(endpoint_url=endpoint_url)
        self.bucket = bucket or settings.AWS_S3_BUCKET_ARTIFACTS

    def build_uri(self, *, organization_id: str, artifact_id: str, step: Step, artifact_type: ArtifactType) -> str:
        file_ext = FILE_EXTENTSION_MAP[artifact_type]
        if artifact_type == ArtifactType.HAR:
            file_ext = f"{file_ext}{S3_ZSTD_COMPRESSED_SUFFIX}"

        return f"{self._build_base_uri(organization_id)}/{step.task_id}/{step.order:02d}_{step.retry_index}_{step.step_id}/{datetime.utcnow().isoformat()}_{artifact_id}_{artifact_type}.{file_ext}"

    async def retrieve_global_workflows(self) -> list[str]:
        uri = f"s3://{self.bucket}/{settings.ENV}/global_workflows.txt"
        data = await self.async_client.download_file(uri, log_exception=False)
        if not data:
            return []
        return [line.strip() for line in data.decode("utf-8").split("\n") if line.strip()]

    def _build_base_uri(self, organization_id: str) -> str:
        return f"s3://{self.bucket}/{self._PATH_VERSION}/{settings.ENV}/{organization_id}"

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
        """Build the S3 URI for a script file.

        Args:
            organization_id: The organization ID
            script_id: The script ID
            script_version: The script version
            file_path: The file path relative to script root

        Returns:
            The S3 URI for the script file
        """
        return f"{self._build_base_uri(organization_id)}/scripts/{script_id}/{script_version}/{file_path}"

    async def store_artifact(self, artifact: Artifact, data: bytes) -> None:
        # We compress HAR files with zstd level 3 to reduce storage size.
        # HARs are easily compressible because they are mostly JSON.
        # Other artifacts are not compressed because they are not easily compressible.
        uri = artifact.uri
        if uri.endswith(S3_ZSTD_COMPRESSED_SUFFIX):
            cctx = zstd.ZstdCompressor(level=3)
            data = cctx.compress(data)

        sc = await self._get_storage_class_for_org(artifact.organization_id, self.bucket, len(data))
        LOG.debug(
            "Storing artifact",
            artifact_id=artifact.artifact_id,
            organization_id=artifact.organization_id,
            uri=uri,
            storage_class=sc,
        )
        await self.async_client.upload_file(uri, data, storage_class=sc)

    async def _get_storage_class_for_org(
        self,
        organization_id: str,
        bucket: str,
        object_size_bytes: int | None = None,
    ) -> S3StorageClass:
        return S3StorageClass.STANDARD

    async def retrieve_artifact(self, artifact: Artifact) -> bytes | None:
        data = await self.async_client.download_file(artifact.uri)
        # Decompress zstd-compressed files (HAR only)
        if data and artifact.uri.endswith(S3_ZSTD_COMPRESSED_SUFFIX):
            dctx = zstd.ZstdDecompressor()
            data = dctx.decompress(data)
        # Extract a named entry from a ZIP archive (STEP_ARCHIVE / TASK_ARCHIVE)
        if data and artifact.bundle_key:
            try:
                with zipfile.ZipFile(io.BytesIO(data)) as zf:
                    return zf.read(artifact.bundle_key)
            except (KeyError, zipfile.BadZipFile):
                LOG.warning(
                    "Failed to extract entry from archive",
                    bundle_key=artifact.bundle_key,
                    artifact_id=artifact.artifact_id,
                )
                return None
        return data

    async def get_share_link(self, artifact: Artifact) -> str | None:
        share_urls = await self.async_client.create_presigned_urls([artifact.uri])
        return share_urls[0] if share_urls else None

    async def get_share_links(self, artifacts: list[Artifact]) -> list[str] | None:
        return await self.async_client.create_presigned_urls([artifact.uri for artifact in artifacts])

    async def store_artifact_from_path(self, artifact: Artifact, path: str) -> None:
        sc = await self._get_storage_class_for_org(artifact.organization_id, self.bucket, os.path.getsize(path))
        LOG.debug(
            "Storing artifact from path",
            artifact_id=artifact.artifact_id,
            organization_id=artifact.organization_id,
            uri=artifact.uri,
            storage_class=sc,
            path=path,
        )
        await self.async_client.upload_file_from_path(artifact.uri, path, storage_class=sc)

    async def save_streaming_file(self, organization_id: str, file_name: str) -> None:
        from_path = f"{get_skyvern_temp_dir()}/{organization_id}/{file_name}"
        to_path = f"s3://{settings.AWS_S3_BUCKET_SCREENSHOTS}/{settings.ENV}/{organization_id}/{file_name}"
        sc = await self._get_storage_class_for_org(organization_id, settings.AWS_S3_BUCKET_SCREENSHOTS)
        LOG.debug(
            "Saving streaming file",
            organization_id=organization_id,
            file_name=file_name,
            from_path=from_path,
            to_path=to_path,
            storage_class=sc,
        )
        await self.async_client.upload_file_from_path(to_path, from_path, storage_class=sc)

    async def get_streaming_file(self, organization_id: str, file_name: str, use_default: bool = True) -> bytes | None:
        path = f"s3://{settings.AWS_S3_BUCKET_SCREENSHOTS}/{settings.ENV}/{organization_id}/{file_name}"
        return await self.async_client.download_file(path, log_exception=False)

    async def store_browser_session(self, organization_id: str, workflow_permanent_id: str, directory: str) -> None:
        # Zip the directory to a temp file
        temp_zip_file = create_named_temporary_file()
        zip_file_path = shutil.make_archive(temp_zip_file.name, "zip", directory)
        browser_session_uri = f"s3://{settings.AWS_S3_BUCKET_BROWSER_SESSIONS}/{settings.ENV}/{organization_id}/{workflow_permanent_id}.zip"
        sc = await self._get_storage_class_for_org(organization_id, settings.AWS_S3_BUCKET_BROWSER_SESSIONS)
        LOG.debug(
            "Storing browser session",
            organization_id=organization_id,
            workflow_permanent_id=workflow_permanent_id,
            zip_file_path=zip_file_path,
            browser_session_uri=browser_session_uri,
            storage_class=sc,
        )
        await self.async_client.upload_file_from_path(browser_session_uri, zip_file_path, storage_class=sc)

    async def retrieve_browser_session(self, organization_id: str, workflow_permanent_id: str) -> str | None:
        browser_session_uri = f"s3://{settings.AWS_S3_BUCKET_BROWSER_SESSIONS}/{settings.ENV}/{organization_id}/{workflow_permanent_id}.zip"
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
        browser_session_uri = f"s3://{settings.AWS_S3_BUCKET_BROWSER_SESSIONS}/{settings.ENV}/{organization_id}/{workflow_permanent_id}.zip"
        LOG.info(
            "Deleting persisted browser session",
            organization_id=organization_id,
            workflow_permanent_id=workflow_permanent_id,
            browser_session_uri=browser_session_uri,
        )
        # S3 DeleteObject is idempotent: deleting a missing key is a no-op, so only real
        # failures (AccessDenied, network, etc.) will raise here.
        await self.async_client.delete_file(browser_session_uri, log_exception=True, raise_on_error=True)

    async def store_browser_profile(self, organization_id: str, profile_id: str, directory: str) -> None:
        """Store browser profile to S3."""
        temp_zip_file = create_named_temporary_file()
        zip_file_path = shutil.make_archive(temp_zip_file.name, "zip", directory)
        profile_uri = (
            f"s3://{settings.AWS_S3_BUCKET_BROWSER_SESSIONS}/{settings.ENV}/{organization_id}/profiles/{profile_id}.zip"
        )
        sc = await self._get_storage_class_for_org(organization_id, settings.AWS_S3_BUCKET_BROWSER_SESSIONS)
        LOG.debug(
            "Storing browser profile",
            organization_id=organization_id,
            profile_id=profile_id,
            zip_file_path=zip_file_path,
            profile_uri=profile_uri,
            storage_class=sc,
        )
        await self.async_client.upload_file_from_path(profile_uri, zip_file_path, storage_class=sc)

    async def retrieve_browser_profile(self, organization_id: str, profile_id: str) -> str | None:
        """Retrieve browser profile from S3."""
        profile_uri = (
            f"s3://{settings.AWS_S3_BUCKET_BROWSER_SESSIONS}/{settings.ENV}/{organization_id}/profiles/{profile_id}.zip"
        )
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
        """Return S3 URIs of completed downloads in the session.

        DB-backed (artifact rows are the source of truth) — see
        ``cloud_docs/BROWSER_SESSION_DOWNLOAD_ARTIFACTS.md``. Used by the agent
        for baseline-before / baseline-after diffs to detect newly-downloaded
        files. Excludes ``*.crdownload`` partials; those go through
        ``list_downloading_files_in_browser_session`` instead.

        Falls back to S3 LIST when the keyring is unset (OSS default — no
        artifact rows exist) or when the DB lookup itself raises.
        """
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
        """Shared DB-backed lister with a partial-vs-final discriminator.

        Centralizes the keyring-gating + DB-failure-fallback so the two public
        methods stay parallel.
        """
        if settings.ARTIFACT_CONTENT_HMAC_KEYRING:
            try:
                artifacts = await app.DATABASE.artifacts.list_artifacts_for_browser_session_by_type(
                    browser_session_id=browser_session_id,
                    organization_id=organization_id,
                    artifact_type=ArtifactType.DOWNLOAD,
                )
            except Exception:
                LOG.warning(
                    "Failed to list browser-session download artifacts; falling back to S3 LIST",
                    organization_id=organization_id,
                    browser_session_id=browser_session_id,
                    in_progress=in_progress,
                    exc_info=True,
                )
                artifacts = None
            if artifacts is not None:
                # Honour the partial-vs-final discriminator the agent expects.
                return [a.uri for a in artifacts if a.uri and a.uri.endswith(BROWSER_DOWNLOADING_SUFFIX) == in_progress]

        bucket = settings.AWS_S3_BUCKET_ARTIFACTS
        uri = f"s3://{bucket}/{self._PATH_VERSION}/{settings.ENV}/{organization_id}/browser_sessions/{browser_session_id}/downloads"
        files = [f"s3://{bucket}/{file}" for file in await self.async_client.list_files(uri=uri)]
        return [f for f in files if f.endswith(BROWSER_DOWNLOADING_SUFFIX) == in_progress]

    async def get_shared_downloaded_files_in_browser_session(
        self, organization_id: str, browser_session_id: str
    ) -> list[FileInfo]:
        # Artifact-first when keyring is configured: query rows scoped to the
        # session, build short signed /v1/artifacts URLs from them. See
        # cloud_docs/BROWSER_SESSION_DOWNLOAD_ARTIFACTS.md.
        #
        # OSS-default deployments without HMAC signing fall straight to the
        # legacy listing path so webhook consumers (no API key) can still
        # fetch the files via presigned URLs.
        if settings.ARTIFACT_CONTENT_HMAC_KEYRING:
            try:
                artifacts = await app.DATABASE.artifacts.list_artifacts_for_browser_session_by_type(
                    browser_session_id=browser_session_id,
                    organization_id=organization_id,
                    artifact_type=ArtifactType.DOWNLOAD,
                )
            except Exception:
                LOG.warning(
                    "Failed to look up browser-session download artifacts; falling back to presigned S3 URLs",
                    organization_id=organization_id,
                    browser_session_id=browser_session_id,
                    exc_info=True,
                )
                artifacts = []
            # Filter out in-progress partials — the user-facing listing must
            # only show completed downloads. Partials still live as artifact
            # rows so the agent can detect "still downloading" via DB query.
            artifacts = [a for a in artifacts if a.uri and not a.uri.endswith(BROWSER_DOWNLOADING_SUFFIX)]
            if artifacts:
                return _file_infos_from_download_artifacts(artifacts)

        return await self._get_shared_downloaded_files_in_browser_session_via_listing(
            organization_id=organization_id, browser_session_id=browser_session_id
        )

    async def _get_shared_downloaded_files_in_browser_session_via_listing(
        self, *, organization_id: str, browser_session_id: str
    ) -> list[FileInfo]:
        # Direct S3 LIST: legacy fallback for sessions pre-cutover (no artifact
        # rows) and OSS deployments without a keyring. We can't go through
        # ``list_downloaded_files_in_browser_session`` here — that now sources
        # from artifact rows and would short-circuit to [] on legacy sessions.
        bucket = settings.AWS_S3_BUCKET_ARTIFACTS
        listing_uri = f"s3://{bucket}/{self._PATH_VERSION}/{settings.ENV}/{organization_id}/browser_sessions/{browser_session_id}/downloads"
        object_keys = [
            f"s3://{bucket}/{file}"
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
                metadata = object_info.get("Metadata", {})
                modified_at = object_info.get("LastModified")
            except Exception:
                LOG.exception("Object info retrieval failed", uri=key)

            # Create FileInfo object
            filename = os.path.basename(key)
            checksum = metadata.get("sha256_checksum") if metadata else None

            # Get presigned URL
            presigned_urls = await self.async_client.create_presigned_urls([key])
            if not presigned_urls:
                continue

            file_info = FileInfo(
                url=presigned_urls[0],
                checksum=checksum,
                filename=metadata.get("original_filename", filename) if metadata else filename,
                modified_at=modified_at,
            )
            file_infos.append(file_info)

        return file_infos

    async def list_downloading_files_in_browser_session(
        self, organization_id: str, browser_session_id: str
    ) -> list[str]:
        """Return S3 URIs of in-progress downloads (``*.crdownload``).

        DB-backed (artifact rows are the source of truth). The watcher creates
        a partial artifact row (``checksum=None``) the moment Chrome opens the
        ``.crdownload`` file; that row is dropped when Chrome's atomic rename
        fires ``Change.deleted``. Used by ``complete_on_download`` task blocks
        to wait until in-flight downloads finish.
        """
        return await self._list_downloads_for_session(
            organization_id=organization_id,
            browser_session_id=browser_session_id,
            in_progress=True,
        )

    async def list_recordings_in_browser_session(self, organization_id: str, browser_session_id: str) -> list[str]:
        """List all recording files for a browser session from S3."""
        bucket = settings.AWS_S3_BUCKET_ARTIFACTS
        uri = f"s3://{bucket}/{self._PATH_VERSION}/{settings.ENV}/{organization_id}/browser_sessions/{browser_session_id}/videos"
        return [f"s3://{bucket}/{file}" for file in await self.async_client.list_files(uri=uri)]

    async def get_shared_recordings_in_browser_session(
        self, organization_id: str, browser_session_id: str
    ) -> list[FileInfo]:
        """Get recording files with presigned URLs for a browser session."""
        object_keys = await self.list_recordings_in_browser_session(organization_id, browser_session_id)
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
                metadata = object_info.get("Metadata", {})
                modified_at = object_info.get("LastModified")
                content_length = object_info.get("ContentLength")
            except Exception:
                LOG.exception("Recording object info retrieval failed", uri=key)

            # Skip zero-byte objects (if any incompleted uploads)
            if content_length == 0:
                continue

            # Create FileInfo object
            filename = os.path.basename(key)
            checksum = metadata.get("sha256_checksum") if metadata else None

            # Get presigned URL
            presigned_urls = await self.async_client.create_presigned_urls([key])
            if not presigned_urls:
                continue

            file_info = FileInfo(
                url=presigned_urls[0],
                checksum=checksum,
                filename=metadata.get("original_filename", filename) if metadata else filename,
                modified_at=modified_at,
            )
            file_infos.append(file_info)

        # Prefer the newest recording first (S3 list order is not guaranteed).
        # Treat None as "oldest".
        file_infos.sort(key=lambda f: (f.modified_at is not None, f.modified_at), reverse=True)
        return file_infos

    async def save_downloaded_files(
        self,
        organization_id: str,
        run_id: str | None,
    ) -> None:
        sc = await self._get_storage_class_for_org(organization_id, settings.AWS_S3_BUCKET_UPLOADS)
        base_uri = (
            f"s3://{settings.AWS_S3_BUCKET_UPLOADS}/{DOWNLOAD_FILE_PREFIX}/{settings.ENV}/{organization_id}/{run_id}"
        )

        await self._save_downloaded_files_from_local(
            organization_id=organization_id,
            run_id=run_id,
            base_uri=base_uri,
            storage_class=sc,
        )

    async def _save_downloaded_files_from_local(
        self,
        organization_id: str,
        run_id: str | None,
        base_uri: str,
        storage_class: S3StorageClass,
    ) -> None:
        """Save files from local download directory to S3."""
        download_dir = get_download_dir(run_id=run_id)
        files = os.listdir(download_dir)
        for file in files:
            fpath = os.path.join(download_dir, file)
            if not os.path.isfile(fpath):
                continue
            uri = f"{base_uri}/{file}"
            checksum = calculate_sha256_for_file(fpath)
            LOG.info(
                "Calculated checksum for file",
                file=file,
                checksum=checksum,
                organization_id=organization_id,
                storage_class=storage_class,
            )
            # S3 object metadata only allows ASCII; non-ASCII filenames (CJK,
            # emoji) would otherwise raise ParamValidationError at upload time.
            # The full filename is still preserved in the S3 key and on the
            # Artifact row's URI.
            metadata: dict[str, str] = {"sha256_checksum": checksum}
            if file.isascii():
                metadata["original_filename"] = file
            # Upload with raise_exception=True so a partial failure aborts
            # this iteration and we never create an Artifact row for bytes
            # that didn't actually land in S3.
            try:
                await self.async_client.upload_file_from_path(
                    uri=uri,
                    file_path=fpath,
                    metadata=metadata,
                    storage_class=storage_class,
                    raise_exception=True,
                )
            except Exception:
                LOG.warning(
                    "Skipping downloaded file — S3 upload failed",
                    file=file,
                    organization_id=organization_id,
                    run_id=run_id,
                    exc_info=True,
                )
                continue

            # Register the file as an Artifact so GET run output can serve it via
            # the signed /v1/artifacts/{id}/content endpoint (SKY-8861). Persist
            # the SHA-256 we already computed so retrieval doesn't need an
            # extra S3 HEAD per file.
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
                        "Failed to register downloaded file as artifact; falling back to S3 listing for retrieval",
                        file=file,
                        organization_id=organization_id,
                        run_id=run_id,
                        exc_info=True,
                    )

    async def get_downloaded_files(self, organization_id: str, run_id: str | None) -> list[FileInfo]:
        # Artifact-first: when a run has DOWNLOAD artifact rows, return them as
        # the source of truth — the row carries enough to build a short signed
        # /v1/artifacts/{id}/content URL plus the SHA-256 we persisted at save
        # time, so we skip the S3 LIST and per-file HEAD entirely (SKY-8861).
        #
        # If HMAC signing isn't configured (self-hosted OSS default), the signed
        # endpoint requires an API key webhook consumers don't have, so we stay
        # on the legacy S3-list+presign path even when rows exist.
        if run_id is not None and settings.ARTIFACT_CONTENT_HMAC_KEYRING:
            artifacts = await self._list_download_artifacts_safe(organization_id=organization_id, run_id=run_id)
            if artifacts:
                return _file_infos_from_download_artifacts(artifacts)

        # Legacy fallback — runs predating SKY-8861 (no artifact rows) and
        # OSS-default deployments without HMAC signing both arrive here.
        return await self._get_downloaded_files_via_s3_listing(organization_id=organization_id, run_id=run_id)

    async def _list_download_artifacts_safe(self, *, organization_id: str, run_id: str) -> list[Artifact]:
        try:
            return await app.DATABASE.artifacts.list_artifacts_for_run_by_type(
                run_id=run_id,
                organization_id=organization_id,
                artifact_type=ArtifactType.DOWNLOAD,
            )
        except Exception:
            LOG.warning(
                "Failed to look up download artifacts; falling back to presigned S3 URLs",
                organization_id=organization_id,
                run_id=run_id,
                exc_info=True,
            )
            return []

    async def _get_downloaded_files_via_s3_listing(self, *, organization_id: str, run_id: str | None) -> list[FileInfo]:
        bucket = settings.AWS_S3_BUCKET_UPLOADS
        uri = f"s3://{bucket}/{DOWNLOAD_FILE_PREFIX}/{settings.ENV}/{organization_id}/{run_id}"
        object_keys = await self.async_client.list_files(uri=uri)
        if len(object_keys) == 0:
            return []

        file_infos: list[FileInfo] = []
        for key in object_keys:
            object_uri = f"s3://{bucket}/{key}"

            metadata = await self.async_client.get_file_metadata(object_uri, log_exception=False)
            filename = os.path.basename(key)
            checksum = metadata.get("sha256_checksum") if metadata else None
            display_name = metadata.get("original_filename", filename) if metadata else filename

            presigned_urls = await self.async_client.create_presigned_urls([object_uri])
            if not presigned_urls:
                continue

            file_infos.append(
                FileInfo(
                    url=presigned_urls[0],
                    checksum=checksum,
                    filename=display_name,
                )
            )
        return file_infos

    async def save_legacy_file(
        self, *, organization_id: str, filename: str, fileObj: BinaryIO
    ) -> tuple[str, str] | None:
        todays_date = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
        bucket = settings.AWS_S3_BUCKET_UPLOADS
        sc = await self._get_storage_class_for_org(organization_id, bucket)
        # First try uploading with original filename
        try:
            sanitized_filename = os.path.basename(filename)  # Remove any path components
            s3_uri = f"s3://{bucket}/{settings.ENV}/{organization_id}/{todays_date}/{sanitized_filename}"
            uploaded_s3_uri = await self.async_client.upload_file_stream(s3_uri, fileObj, storage_class=sc)
        except Exception:
            LOG.error("Failed to upload file to S3", exc_info=True)
            uploaded_s3_uri = None

        # If upload fails, try again with UUID prefix
        if not uploaded_s3_uri:
            uuid_prefixed_filename = f"{str(uuid.uuid4())}_{filename}"
            s3_uri = f"s3://{bucket}/{settings.ENV}/{organization_id}/{todays_date}/{uuid_prefixed_filename}"
            fileObj.seek(0)  # Reset file pointer
            uploaded_s3_uri = await self.async_client.upload_file_stream(s3_uri, fileObj, storage_class=sc)

        if not uploaded_s3_uri:
            LOG.error(
                "Failed to upload file to S3 after retrying with UUID prefix",
                organization_id=organization_id,
                storage_class=sc,
                filename=filename,
                exc_info=True,
            )
            return None
        LOG.debug(
            "Legacy file upload",
            organization_id=organization_id,
            storage_class=sc,
            filename=filename,
            uploaded_s3_uri=uploaded_s3_uri,
        )
        # Generate a presigned URL for the uploaded file
        presigned_urls = await self.async_client.create_presigned_urls([uploaded_s3_uri])
        if not presigned_urls:
            LOG.error(
                "Failed to create presigned URL for uploaded file",
                organization_id=organization_id,
                storage_class=sc,
                uploaded_s3_uri=uploaded_s3_uri,
                filename=filename,
                exc_info=True,
            )
            return None
        return presigned_urls[0], uploaded_s3_uri

    def _build_browser_session_uri(
        self,
        organization_id: str,
        browser_session_id: str,
        artifact_type: str,
        remote_path: str,
        date: str | None = None,
    ) -> str:
        """Build the S3 URI for a browser session file."""
        base = f"s3://{self.bucket}/{self._PATH_VERSION}/{settings.ENV}/{organization_id}/browser_sessions/{browser_session_id}/{artifact_type}"
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
        """Sync a file from local browser session to S3."""
        uri = self._build_browser_session_uri(organization_id, browser_session_id, artifact_type, remote_path, date)
        sc = await self._get_storage_class_for_org(organization_id, self.bucket)
        await self.async_client.upload_file_from_path(uri, local_file_path, storage_class=sc)

        # For downloaded files (only), register an Artifact row scoped to the
        # session so the DB is the single source of truth for both the
        # ``GET /v1/browser_sessions/{id}`` user-facing listing and the
        # agent's baseline-before/after / complete_on_download checks.
        # Partial files (``*.crdownload``) get a row too with checksum=None —
        # the agent's "still downloading" query reads URI-suffix from the
        # row. The row is dropped when Chrome's atomic rename fires
        # ``Change.deleted`` for the partial path.
        #
        # We deliberately let exceptions propagate so the watcher's bounded
        # retry can recover from a transient DB outage — silently swallowing
        # would leave the file in S3 with no row, invisible to baseline
        # diffs and complete_on_download. Both ``upload_file_from_path``
        # (S3 overwrite) and ``create_browser_session_download_artifact``
        # (idempotent on ``(session, uri)``) are safe to retry.
        if artifact_type == "downloads":
            is_partial = remote_path.endswith(BROWSER_DOWNLOADING_SUFFIX)
            checksum = None if is_partial else calculate_sha256_for_file(local_file_path)
            await app.ARTIFACT_MANAGER.create_browser_session_download_artifact(
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
        """Delete a file from browser session storage in S3.

        For ``downloads``, also drop the matching DOWNLOAD artifact row so a
        subsequent ``GET /v1/browser_sessions/{id}`` doesn't hand out a signed
        URL that 404s. The DB delete runs before the S3 delete: if S3 fails
        we'd rather have an artifact row missing (the listing fallback covers
        it) than a row pointing at a deleted object.
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
                    "Failed to delete browser-session download artifact row; proceeding with S3 delete",
                    organization_id=organization_id,
                    browser_session_id=browser_session_id,
                    remote_path=remote_path,
                    exc_info=True,
                )
        await self.async_client.delete_file(uri, log_exception=True)

    async def browser_session_file_exists(
        self,
        organization_id: str,
        browser_session_id: str,
        artifact_type: str,
        remote_path: str,
        date: str | None = None,
    ) -> bool:
        """Check if a file exists in browser session storage in S3."""
        uri = self._build_browser_session_uri(organization_id, browser_session_id, artifact_type, remote_path, date)
        try:
            info = await self.async_client.get_object_info(uri)
            return info is not None
        except Exception:
            return False

    def assert_managed_file_access(self, uri: str, organization_id: str) -> None:
        try:
            parsed_uri = S3Uri(uri)
        except Exception as e:
            raise PermissionError(f"No permission to access storage URI: {uri}") from e

        # Uploads bucket: keys use {env}/{org}/ or downloads/{env}/{org}/
        if parsed_uri.bucket == settings.AWS_S3_BUCKET_UPLOADS:
            allowed_prefixes = (
                f"{settings.ENV}/{organization_id}/",
                f"{DOWNLOAD_FILE_PREFIX}/{settings.ENV}/{organization_id}/",
            )
            if any(parsed_uri.key.startswith(prefix) for prefix in allowed_prefixes):
                return

        # Artifacts bucket: keys use v1/{env}/{org}/
        if parsed_uri.bucket == settings.AWS_S3_BUCKET_ARTIFACTS:
            artifact_prefix = f"{self._PATH_VERSION}/{settings.ENV}/{organization_id}/"
            if parsed_uri.key.startswith(artifact_prefix):
                return

        raise PermissionError(f"No permission to access storage URI: {uri}")

    async def download_managed_file(self, uri: str, organization_id: str) -> bytes | None:
        """Download a managed org-scoped file from S3."""
        self.assert_managed_file_access(uri, organization_id)
        return await self.async_client.download_file(uri, log_exception=False)

    async def file_exists(self, uri: str) -> bool:
        """Check if a file exists at the given S3 URI."""
        try:
            info = await self.async_client.get_object_info(uri)
            return info is not None
        except Exception:
            return False

    @property
    def storage_type(self) -> str:
        """Returns 's3' as the storage type."""
        return "s3"
