import os
import shutil
import uuid
from datetime import datetime, timezone
from typing import BinaryIO

import structlog

from skyvern.config import settings
from skyvern.constants import DOWNLOAD_FILE_PREFIX
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

    async def save_downloaded_files(
        self, organization_id: str, task_id: str | None, workflow_run_id: str | None
    ) -> None:
        download_dir = get_download_dir(workflow_run_id=workflow_run_id, task_id=task_id)
        files = os.listdir(download_dir)
        sc = await self._get_storage_class_for_org(organization_id)
        tags = await self._get_tags_for_org(organization_id)
        base_uri = f"s3://{settings.AWS_S3_BUCKET_UPLOADS}/{DOWNLOAD_FILE_PREFIX}/{settings.ENV}/{organization_id}/{workflow_run_id or task_id}"
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

    async def get_downloaded_files(
        self, organization_id: str, task_id: str | None, workflow_run_id: str | None
    ) -> list[FileInfo]:
        uri = f"s3://{settings.AWS_S3_BUCKET_UPLOADS}/{DOWNLOAD_FILE_PREFIX}/{settings.ENV}/{organization_id}/{workflow_run_id or task_id}"
        object_keys = await self.async_client.list_files(uri=uri)
        if len(object_keys) == 0:
            return []

        file_infos: list[FileInfo] = []
        for key in object_keys:
            object_uri = f"s3://{settings.AWS_S3_BUCKET_UPLOADS}/{key}"

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
