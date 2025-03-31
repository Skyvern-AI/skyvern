import os
import shutil
from datetime import datetime

import structlog

from skyvern.config import settings
from skyvern.constants import DOWNLOAD_FILE_PREFIX
from skyvern.forge.sdk.api.aws import AsyncAWSClient
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
    def __init__(self, bucket: str | None = None) -> None:
        self.async_client = AsyncAWSClient()
        self.bucket = bucket or settings.AWS_S3_BUCKET_ARTIFACTS

    def build_uri(self, artifact_id: str, step: Step, artifact_type: ArtifactType) -> str:
        file_ext = FILE_EXTENTSION_MAP[artifact_type]
        return f"s3://{self.bucket}/{settings.ENV}/{step.task_id}/{step.order:02d}_{step.retry_index}_{step.step_id}/{datetime.utcnow().isoformat()}_{artifact_id}_{artifact_type}.{file_ext}"

    async def retrieve_global_workflows(self) -> list[str]:
        uri = f"s3://{self.bucket}/{settings.ENV}/global_workflows.txt"
        data = await self.async_client.download_file(uri, log_exception=False)
        if not data:
            return []
        return [line.strip() for line in data.decode("utf-8").split("\n") if line.strip()]

    def build_log_uri(self, log_entity_type: LogEntityType, log_entity_id: str, artifact_type: ArtifactType) -> str:
        file_ext = FILE_EXTENTSION_MAP[artifact_type]
        return f"s3://{self.bucket}/{settings.ENV}/logs/{log_entity_type}/{log_entity_id}/{datetime.utcnow().isoformat()}_{artifact_type}.{file_ext}"

    def build_thought_uri(self, artifact_id: str, thought: Thought, artifact_type: ArtifactType) -> str:
        file_ext = FILE_EXTENTSION_MAP[artifact_type]
        return f"s3://{self.bucket}/{settings.ENV}/observers/{thought.observer_cruise_id}/{thought.observer_thought_id}/{datetime.utcnow().isoformat()}_{artifact_id}_{artifact_type}.{file_ext}"

    def build_task_v2_uri(self, artifact_id: str, task_v2: TaskV2, artifact_type: ArtifactType) -> str:
        file_ext = FILE_EXTENTSION_MAP[artifact_type]
        return f"s3://{self.bucket}/{settings.ENV}/observers/{task_v2.observer_cruise_id}/{datetime.utcnow().isoformat()}_{artifact_id}_{artifact_type}.{file_ext}"

    def build_workflow_run_block_uri(
        self, artifact_id: str, workflow_run_block: WorkflowRunBlock, artifact_type: ArtifactType
    ) -> str:
        file_ext = FILE_EXTENTSION_MAP[artifact_type]
        return f"s3://{self.bucket}/{settings.ENV}/workflow_runs/{workflow_run_block.workflow_run_id}/{workflow_run_block.workflow_run_block_id}/{datetime.utcnow().isoformat()}_{artifact_id}_{artifact_type}.{file_ext}"

    def build_ai_suggestion_uri(
        self, artifact_id: str, ai_suggestion: AISuggestion, artifact_type: ArtifactType
    ) -> str:
        file_ext = FILE_EXTENTSION_MAP[artifact_type]
        return f"s3://{self.bucket}/{settings.ENV}/ai_suggestions/{ai_suggestion.ai_suggestion_id}/{datetime.utcnow().isoformat()}_{artifact_id}_{artifact_type}.{file_ext}"

    async def store_artifact(self, artifact: Artifact, data: bytes) -> None:
        await self.async_client.upload_file(artifact.uri, data)

    async def retrieve_artifact(self, artifact: Artifact) -> bytes | None:
        return await self.async_client.download_file(artifact.uri)

    async def get_share_link(self, artifact: Artifact) -> str | None:
        share_urls = await self.async_client.create_presigned_urls([artifact.uri])
        return share_urls[0] if share_urls else None

    async def get_share_links(self, artifacts: list[Artifact]) -> list[str] | None:
        return await self.async_client.create_presigned_urls([artifact.uri for artifact in artifacts])

    async def store_artifact_from_path(self, artifact: Artifact, path: str) -> None:
        await self.async_client.upload_file_from_path(artifact.uri, path)

    async def save_streaming_file(self, organization_id: str, file_name: str) -> None:
        from_path = f"{get_skyvern_temp_dir()}/{organization_id}/{file_name}"
        to_path = f"s3://{settings.AWS_S3_BUCKET_SCREENSHOTS}/{settings.ENV}/{organization_id}/{file_name}"
        await self.async_client.upload_file_from_path(to_path, from_path)

    async def get_streaming_file(self, organization_id: str, file_name: str, use_default: bool = True) -> bytes | None:
        path = f"s3://{settings.AWS_S3_BUCKET_SCREENSHOTS}/{settings.ENV}/{organization_id}/{file_name}"
        return await self.async_client.download_file(path, log_exception=False)

    async def store_browser_session(self, organization_id: str, workflow_permanent_id: str, directory: str) -> None:
        # Zip the directory to a temp file
        temp_zip_file = create_named_temporary_file()
        zip_file_path = shutil.make_archive(temp_zip_file.name, "zip", directory)
        browser_session_uri = f"s3://{settings.AWS_S3_BUCKET_BROWSER_SESSIONS}/{settings.ENV}/{organization_id}/{workflow_permanent_id}.zip"
        await self.async_client.upload_file_from_path(browser_session_uri, zip_file_path)

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
        for file in files:
            fpath = os.path.join(download_dir, file)
            if os.path.isfile(fpath):
                uri = f"s3://{settings.AWS_S3_BUCKET_UPLOADS}/{DOWNLOAD_FILE_PREFIX}/{settings.ENV}/{organization_id}/{workflow_run_id or task_id}/{file}"

                # Calculate SHA-256 checksum
                checksum = calculate_sha256_for_file(fpath)
                LOG.info("Calculated checksum for file", file=file, checksum=checksum)

                # Upload file with checksum metadata
                await self.async_client.upload_file_from_path(
                    uri=uri, file_path=fpath, metadata={"sha256_checksum": checksum, "original_filename": file}
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
