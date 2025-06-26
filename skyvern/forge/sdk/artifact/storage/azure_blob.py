import os
import shutil
import uuid
from datetime import datetime, timezone
from typing import BinaryIO

import structlog

from skyvern.config import settings
from skyvern.constants import DOWNLOAD_FILE_PREFIX
from skyvern.forge.sdk.api.azure import AsyncAzureClient, AzureBlobStorageClass
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


class AzureBlobStorage(BaseStorage):
    _PATH_VERSION = "v1"

    def __init__(
        self, account_name: str | None = None, account_key: str | None = None, connection_string: str | None = None
    ) -> None:
        self.async_client = AsyncAzureClient(
            account_name=account_name, account_key=account_key, connection_string=connection_string
        )
        self.container_artifacts = settings.AZURE_CONTAINER_ARTIFACTS
        self.container_screenshots = settings.AZURE_CONTAINER_SCREENSHOTS
        self.container_browser_sessions = settings.AZURE_CONTAINER_BROWSER_SESSIONS
        self.container_uploads = settings.AZURE_CONTAINER_UPLOADS

    def build_uri(self, *, organization_id: str, artifact_id: str, step: Step, artifact_type: ArtifactType) -> str:
        file_ext = FILE_EXTENTSION_MAP[artifact_type]
        blob_name = f"{self._build_base_path(organization_id)}/{step.task_id}/{step.order:02d}_{step.retry_index}_{step.step_id}/{datetime.utcnow().isoformat()}_{artifact_id}_{artifact_type}.{file_ext}"
        return f"https://{self.async_client.account_name}.blob.core.windows.net/{self.container_artifacts}/{blob_name}"

    async def retrieve_global_workflows(self) -> list[str]:
        blob_name = f"{settings.ENV}/global_workflows.txt"
        uri = f"https://{self.async_client.account_name}.blob.core.windows.net/{self.container_artifacts}/{blob_name}"
        data = await self.async_client.download_file(uri, log_exception=False)
        if not data:
            return []
        return [line.strip() for line in data.decode("utf-8").split("\n") if line.strip()]

    def _build_base_path(self, organization_id: str) -> str:
        return f"{self._PATH_VERSION}/{settings.ENV}/{organization_id}"

    def build_log_uri(
        self, *, organization_id: str, log_entity_type: LogEntityType, log_entity_id: str, artifact_type: ArtifactType
    ) -> str:
        file_ext = FILE_EXTENTSION_MAP[artifact_type]
        blob_name = f"{self._build_base_path(organization_id)}/logs/{log_entity_type}/{log_entity_id}/{datetime.utcnow().isoformat()}_{artifact_type}.{file_ext}"
        return f"https://{self.async_client.account_name}.blob.core.windows.net/{self.container_artifacts}/{blob_name}"

    def build_thought_uri(
        self, *, organization_id: str, artifact_id: str, thought: Thought, artifact_type: ArtifactType
    ) -> str:
        file_ext = FILE_EXTENTSION_MAP[artifact_type]
        blob_name = f"{self._build_base_path(organization_id)}/observers/{thought.observer_cruise_id}/{thought.observer_thought_id}/{datetime.utcnow().isoformat()}_{artifact_id}_{artifact_type}.{file_ext}"
        return f"https://{self.async_client.account_name}.blob.core.windows.net/{self.container_artifacts}/{blob_name}"

    def build_task_v2_uri(
        self, *, organization_id: str, artifact_id: str, task_v2: TaskV2, artifact_type: ArtifactType
    ) -> str:
        file_ext = FILE_EXTENTSION_MAP[artifact_type]
        blob_name = f"{self._build_base_path(organization_id)}/observers/{task_v2.observer_cruise_id}/{datetime.utcnow().isoformat()}_{artifact_id}_{artifact_type}.{file_ext}"
        return f"https://{self.async_client.account_name}.blob.core.windows.net/{self.container_artifacts}/{blob_name}"

    def build_workflow_run_block_uri(
        self,
        *,
        organization_id: str,
        artifact_id: str,
        workflow_run_block: WorkflowRunBlock,
        artifact_type: ArtifactType,
    ) -> str:
        file_ext = FILE_EXTENTSION_MAP[artifact_type]
        blob_name = f"{self._build_base_path(organization_id)}/workflow_runs/{workflow_run_block.workflow_run_id}/{workflow_run_block.workflow_run_block_id}/{datetime.utcnow().isoformat()}_{artifact_id}_{artifact_type}.{file_ext}"
        return f"https://{self.async_client.account_name}.blob.core.windows.net/{self.container_artifacts}/{blob_name}"

    def build_ai_suggestion_uri(
        self, *, organization_id: str, artifact_id: str, ai_suggestion: AISuggestion, artifact_type: ArtifactType
    ) -> str:
        file_ext = FILE_EXTENTSION_MAP[artifact_type]
        blob_name = f"{self._build_base_path(organization_id)}/ai_suggestions/{ai_suggestion.ai_suggestion_id}/{datetime.utcnow().isoformat()}_{artifact_id}_{artifact_type}.{file_ext}"
        return f"https://{self.async_client.account_name}.blob.core.windows.net/{self.container_artifacts}/{blob_name}"

    async def store_artifact(self, artifact: Artifact, data: bytes) -> None:
        sc = await self._get_storage_class_for_org(artifact.organization_id)
        metadata = await self._get_metadata_for_org(artifact.organization_id)
        await self.async_client.upload_file(artifact.uri, data, storage_class=sc, metadata=metadata)

    async def _get_storage_class_for_org(self, organization_id: str) -> AzureBlobStorageClass:
        return AzureBlobStorageClass.HOT

    async def _get_metadata_for_org(self, organization_id: str) -> dict[str, str]:
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
        metadata = await self._get_metadata_for_org(artifact.organization_id)
        await self.async_client.upload_file_from_path(artifact.uri, path, storage_class=sc, metadata=metadata)

    async def save_streaming_file(self, organization_id: str, file_name: str) -> None:
        from_path = f"{get_skyvern_temp_dir()}/{organization_id}/{file_name}"
        blob_name = f"{settings.ENV}/{organization_id}/{file_name}"
        to_uri = (
            f"https://{self.async_client.account_name}.blob.core.windows.net/{self.container_screenshots}/{blob_name}"
        )
        sc = await self._get_storage_class_for_org(organization_id)
        metadata = await self._get_metadata_for_org(organization_id)
        await self.async_client.upload_file_from_path(to_uri, from_path, storage_class=sc, metadata=metadata)

    async def get_streaming_file(self, organization_id: str, file_name: str, use_default: bool = True) -> bytes | None:
        blob_name = f"{settings.ENV}/{organization_id}/{file_name}"
        uri = f"https://{self.async_client.account_name}.blob.core.windows.net/{self.container_screenshots}/{blob_name}"
        return await self.async_client.download_file(uri, log_exception=False)

    async def store_browser_session(self, organization_id: str, workflow_permanent_id: str, directory: str) -> None:
        temp_zip_file = create_named_temporary_file()
        zip_file_path = shutil.make_archive(temp_zip_file.name, "zip", directory)
        blob_name = f"{settings.ENV}/{organization_id}/{workflow_permanent_id}.zip"
        browser_session_uri = f"https://{self.async_client.account_name}.blob.core.windows.net/{self.container_browser_sessions}/{blob_name}"
        sc = await self._get_storage_class_for_org(organization_id)
        metadata = await self._get_metadata_for_org(organization_id)
        await self.async_client.upload_file_from_path(
            browser_session_uri, zip_file_path, storage_class=sc, metadata=metadata
        )

    async def retrieve_browser_session(self, organization_id: str, workflow_permanent_id: str) -> str | None:
        blob_name = f"{settings.ENV}/{organization_id}/{workflow_permanent_id}.zip"
        browser_session_uri = f"https://{self.async_client.account_name}.blob.core.windows.net/{self.container_browser_sessions}/{blob_name}"
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
        metadata = await self._get_metadata_for_org(organization_id)
        base_blob_path = f"{DOWNLOAD_FILE_PREFIX}/{settings.ENV}/{organization_id}/{workflow_run_id or task_id}"

        for file in files:
            fpath = os.path.join(download_dir, file)
            if not os.path.isfile(fpath):
                continue
            blob_name = f"{base_blob_path}/{file}"
            uri = f"https://{self.async_client.account_name}.blob.core.windows.net/{self.container_uploads}/{blob_name}"
            checksum = calculate_sha256_for_file(fpath)
            file_metadata = {**metadata, "sha256_checksum": checksum, "original_filename": file}
            await self.async_client.upload_file_from_path(
                uri=uri,
                file_path=fpath,
                metadata=file_metadata,
                storage_class=sc,
            )

    async def get_downloaded_files(
        self, organization_id: str, task_id: str | None, workflow_run_id: str | None
    ) -> list[FileInfo]:
        prefix = f"{DOWNLOAD_FILE_PREFIX}/{settings.ENV}/{organization_id}/{workflow_run_id or task_id}/"
        blob_names = await self.async_client.list_blobs(self.container_uploads, prefix=prefix)

        if len(blob_names) == 0:
            return []

        file_infos: list[FileInfo] = []
        for blob_name in blob_names:
            blob_uri = (
                f"https://{self.async_client.account_name}.blob.core.windows.net/{self.container_uploads}/{blob_name}"
            )
            metadata = await self.async_client.get_file_metadata(blob_uri, log_exception=False)
            filename = os.path.basename(blob_name)
            checksum = metadata.get("sha256_checksum") if metadata else None
            presigned_urls = await self.async_client.create_presigned_urls([blob_uri])
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
        container = self.container_uploads
        sc = await self._get_storage_class_for_org(organization_id)
        metadata = await self._get_metadata_for_org(organization_id)

        # First try uploading with original filename
        try:
            sanitized_filename = os.path.basename(filename)
            blob_name = f"{settings.ENV}/{organization_id}/{todays_date}/{sanitized_filename}"
            blob_uri = f"https://{self.async_client.account_name}.blob.core.windows.net/{container}/{blob_name}"
            uploaded_blob_uri = await self.async_client.upload_file_stream(
                blob_uri, fileObj, storage_class=sc, metadata=metadata
            )
        except Exception:
            LOG.error("Failed to upload file to Azure Blob", exc_info=True)
            uploaded_blob_uri = None

        # If upload fails, try again with UUID prefix
        if not uploaded_blob_uri:
            uuid_prefixed_filename = f"{str(uuid.uuid4())}_{filename}"
            blob_name = f"{settings.ENV}/{organization_id}/{todays_date}/{uuid_prefixed_filename}"
            blob_uri = f"https://{self.async_client.account_name}.blob.core.windows.net/{container}/{blob_name}"
            fileObj.seek(0)
            uploaded_blob_uri = await self.async_client.upload_file_stream(
                blob_uri, fileObj, storage_class=sc, metadata=metadata
            )

        if not uploaded_blob_uri:
            return None

        presigned_urls = await self.async_client.create_presigned_urls([uploaded_blob_uri])
        if not presigned_urls:
            return None
        return presigned_urls[0], uploaded_blob_uri
