import os
import shutil
import uuid
from datetime import datetime, timezone
from typing import BinaryIO

import structlog

from skyvern.config import settings
from skyvern.constants import BROWSER_DOWNLOADING_SUFFIX, DOWNLOAD_FILE_PREFIX
from skyvern.forge.sdk.api.aws import AsyncAWSClient, S3StorageClass
from skyvern.forge.sdk.api.files import (
    calculate_sha256_for_file,
    create_named_temporary_file,
    get_download_dir,
    get_skyvern_temp_dir,
    make_temp_directory,
    unzip_files,
)
from skyvern.forge.sdk.artifact.models import Artifact, ArtifactType, LogEntityType
from skyvern.forge.sdk.artifact.storage.base import FILE_EXTENTSION_MAP, BaseStorage
from skyvern.forge.sdk.models import Step
from skyvern.forge.sdk.schemas.ai_suggestions import AISuggestion
from skyvern.forge.sdk.schemas.files import FileInfo
from skyvern.forge.sdk.schemas.task_v2 import TaskV2, Thought
from skyvern.forge.sdk.schemas.workflow_runs import WorkflowRunBlock

LOG = structlog.get_logger()


class S3Storage(BaseStorage):
    _PATH_VERSION = "v1"

    def __init__(self, bucket: str | None = None, endpoint_url: str | None = None) -> None:
        self.async_client = AsyncAWSClient(endpoint_url=endpoint_url)
        self.bucket = bucket or settings.AWS_S3_BUCKET_ARTIFACTS

    def build_uri(self, *, organization_id: str, artifact_id: str, step: Step, artifact_type: ArtifactType) -> str:
        file_ext = FILE_EXTENTSION_MAP[artifact_type]
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
        sc = await self._get_storage_class_for_org(artifact.organization_id)
        tags = await self._get_tags_for_org(artifact.organization_id)
        LOG.debug(
            "Storing artifact",
            artifact_id=artifact.artifact_id,
            organization_id=artifact.organization_id,
            uri=artifact.uri,
            storage_class=sc,
            tags=tags,
        )
        await self.async_client.upload_file(artifact.uri, data, storage_class=sc, tags=tags)

    async def _get_storage_class_for_org(self, organization_id: str) -> S3StorageClass:
        return S3StorageClass.STANDARD

    async def _get_tags_for_org(self, organization_id: str) -> dict[str, str]:
        return {}

    async def retrieve_artifact(self, artifact: Artifact) -> bytes | None:
        return await self.async_client.download_file(artifact.uri)

    async def get_share_link(self, artifact: Artifact) -> str | None:
        share_urls = await self.async_client.create_presigned_urls([artifact.uri])
        return share_urls[0] if share_urls else None

    async def get_share_links(self, artifacts: list[Artifact]) -> list[str] | None:
        return await self.async_client.create_presigned_urls([artifact.uri for artifact in artifacts])

    async def store_artifact_from_path(self, artifact: Artifact, path: str) -> None:
        sc = await self._get_storage_class_for_org(artifact.organization_id)
        tags = await self._get_tags_for_org(artifact.organization_id)
        LOG.debug(
            "Storing artifact from path",
            artifact_id=artifact.artifact_id,
            organization_id=artifact.organization_id,
            uri=artifact.uri,
            storage_class=sc,
            path=path,
            tags=tags,
        )
        await self.async_client.upload_file_from_path(artifact.uri, path, storage_class=sc, tags=tags)

    async def save_streaming_file(self, organization_id: str, file_name: str) -> None:
        from_path = f"{get_skyvern_temp_dir()}/{organization_id}/{file_name}"
        to_path = f"s3://{settings.AWS_S3_BUCKET_SCREENSHOTS}/{settings.ENV}/{organization_id}/{file_name}"
        sc = await self._get_storage_class_for_org(organization_id)
        tags = await self._get_tags_for_org(organization_id)
        LOG.debug(
            "Saving streaming file",
            organization_id=organization_id,
            file_name=file_name,
            from_path=from_path,
            to_path=to_path,
            storage_class=sc,
            tags=tags,
        )
        await self.async_client.upload_file_from_path(to_path, from_path, storage_class=sc, tags=tags)

    async def get_streaming_file(self, organization_id: str, file_name: str, use_default: bool = True) -> bytes | None:
        path = f"s3://{settings.AWS_S3_BUCKET_SCREENSHOTS}/{settings.ENV}/{organization_id}/{file_name}"
        return await self.async_client.download_file(path, log_exception=False)

    async def store_browser_session(self, organization_id: str, workflow_permanent_id: str, directory: str) -> None:
        # Zip the directory to a temp file
        temp_zip_file = create_named_temporary_file()
        zip_file_path = shutil.make_archive(temp_zip_file.name, "zip", directory)
        browser_session_uri = f"s3://{settings.AWS_S3_BUCKET_BROWSER_SESSIONS}/{settings.ENV}/{organization_id}/{workflow_permanent_id}.zip"
        sc = await self._get_storage_class_for_org(organization_id)
        tags = await self._get_tags_for_org(organization_id)
        LOG.debug(
            "Storing browser session",
            organization_id=organization_id,
            workflow_permanent_id=workflow_permanent_id,
            zip_file_path=zip_file_path,
            browser_session_uri=browser_session_uri,
            storage_class=sc,
            tags=tags,
        )
        await self.async_client.upload_file_from_path(browser_session_uri, zip_file_path, storage_class=sc, tags=tags)

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

    async def store_browser_profile(self, organization_id: str, profile_id: str, directory: str) -> None:
        """Store browser profile to S3."""
        temp_zip_file = create_named_temporary_file()
        zip_file_path = shutil.make_archive(temp_zip_file.name, "zip", directory)
        profile_uri = (
            f"s3://{settings.AWS_S3_BUCKET_BROWSER_SESSIONS}/{settings.ENV}/{organization_id}/profiles/{profile_id}.zip"
        )
        sc = await self._get_storage_class_for_org(organization_id)
        tags = await self._get_tags_for_org(organization_id)
        LOG.debug(
            "Storing browser profile",
            organization_id=organization_id,
            profile_id=profile_id,
            zip_file_path=zip_file_path,
            profile_uri=profile_uri,
            storage_class=sc,
            tags=tags,
        )
        await self.async_client.upload_file_from_path(profile_uri, zip_file_path, storage_class=sc, tags=tags)

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
        bucket = settings.AWS_S3_BUCKET_ARTIFACTS
        uri = f"s3://{bucket}/v1/{settings.ENV}/{organization_id}/browser_sessions/{browser_session_id}/downloads"
        return [f"s3://{bucket}/{file}" for file in await self.async_client.list_files(uri=uri)]

    async def get_shared_downloaded_files_in_browser_session(
        self, organization_id: str, browser_session_id: str
    ) -> list[FileInfo]:
        object_keys = await self.list_downloaded_files_in_browser_session(organization_id, browser_session_id)
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
        bucket = settings.AWS_S3_BUCKET_ARTIFACTS
        uri = f"s3://{bucket}/v1/{settings.ENV}/{organization_id}/browser_sessions/{browser_session_id}/downloads"
        files = [f"s3://{bucket}/{file}" for file in await self.async_client.list_files(uri=uri)]
        return [file for file in files if file.endswith(BROWSER_DOWNLOADING_SUFFIX)]

    async def list_recordings_in_browser_session(self, organization_id: str, browser_session_id: str) -> list[str]:
        """List all recording files for a browser session from S3."""
        bucket = settings.AWS_S3_BUCKET_ARTIFACTS
        uri = f"s3://{bucket}/v1/{settings.ENV}/{organization_id}/browser_sessions/{browser_session_id}/videos"
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

    async def save_downloaded_files(self, organization_id: str, run_id: str | None) -> None:
        download_dir = get_download_dir(run_id=run_id)
        files = os.listdir(download_dir)
        sc = await self._get_storage_class_for_org(organization_id)
        tags = await self._get_tags_for_org(organization_id)
        base_uri = (
            f"s3://{settings.AWS_S3_BUCKET_UPLOADS}/{DOWNLOAD_FILE_PREFIX}/{settings.ENV}/{organization_id}/{run_id}"
        )
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
                storage_class=sc,
            )
            # Upload file with checksum metadata
            await self.async_client.upload_file_from_path(
                uri=uri,
                file_path=fpath,
                metadata={"sha256_checksum": checksum, "original_filename": file},
                storage_class=sc,
                tags=tags,
            )

    async def get_downloaded_files(self, organization_id: str, run_id: str | None) -> list[FileInfo]:
        bucket = settings.AWS_S3_BUCKET_UPLOADS
        uri = f"s3://{bucket}/{DOWNLOAD_FILE_PREFIX}/{settings.ENV}/{organization_id}/{run_id}"
        object_keys = await self.async_client.list_files(uri=uri)
        if len(object_keys) == 0:
            return []

        file_infos: list[FileInfo] = []
        for key in object_keys:
            object_uri = f"s3://{bucket}/{key}"

            # Get metadata (including checksum)
            metadata = await self.async_client.get_file_metadata(object_uri, log_exception=False)

            # Create FileInfo object
            filename = os.path.basename(key)
            checksum = metadata.get("sha256_checksum") if metadata else None

            # Get presigned URL
            presigned_urls = await self.async_client.create_presigned_urls([object_uri])
            if not presigned_urls:
                continue

            file_info = FileInfo(
                url=presigned_urls[0],
                checksum=checksum,
                filename=metadata.get("original_filename", filename) if metadata else filename,
            )
            file_infos.append(file_info)

        return file_infos

    async def save_legacy_file(
        self, *, organization_id: str, filename: str, fileObj: BinaryIO
    ) -> tuple[str, str] | None:
        todays_date = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
        bucket = settings.AWS_S3_BUCKET_UPLOADS
        sc = await self._get_storage_class_for_org(organization_id)
        tags = await self._get_tags_for_org(organization_id)
        # First try uploading with original filename
        try:
            sanitized_filename = os.path.basename(filename)  # Remove any path components
            s3_uri = f"s3://{bucket}/{settings.ENV}/{organization_id}/{todays_date}/{sanitized_filename}"
            uploaded_s3_uri = await self.async_client.upload_file_stream(s3_uri, fileObj, storage_class=sc, tags=tags)
        except Exception:
            LOG.error("Failed to upload file to S3", exc_info=True)
            uploaded_s3_uri = None

        # If upload fails, try again with UUID prefix
        if not uploaded_s3_uri:
            uuid_prefixed_filename = f"{str(uuid.uuid4())}_{filename}"
            s3_uri = f"s3://{bucket}/{settings.ENV}/{organization_id}/{todays_date}/{uuid_prefixed_filename}"
            fileObj.seek(0)  # Reset file pointer
            uploaded_s3_uri = await self.async_client.upload_file_stream(s3_uri, fileObj, storage_class=sc, tags=tags)

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
        base = f"s3://{self.bucket}/v1/{settings.ENV}/{organization_id}/browser_sessions/{browser_session_id}/{artifact_type}"
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
        sc = await self._get_storage_class_for_org(organization_id)
        tags = await self._get_tags_for_org(organization_id)
        await self.async_client.upload_file_from_path(uri, local_file_path, storage_class=sc, tags=tags)
        return uri

    async def delete_browser_session_file(
        self,
        organization_id: str,
        browser_session_id: str,
        artifact_type: str,
        remote_path: str,
        date: str | None = None,
    ) -> None:
        """Delete a file from browser session storage in S3."""
        uri = self._build_browser_session_uri(organization_id, browser_session_id, artifact_type, remote_path, date)
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

    async def download_uploaded_file(self, uri: str) -> bytes | None:
        """Download a user-uploaded file from S3."""
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
