from __future__ import annotations

import asyncio
import os
import uuid
from pathlib import Path
from typing import Literal

import structlog
from pydantic import Field

from skyvern.config import settings
from skyvern.constants import (
    AZURE_BLOB_STORAGE_MAX_UPLOAD_FILE_COUNT,
    GET_DOWNLOADED_FILES_TIMEOUT,
    MAX_UPLOAD_FILE_COUNT,
)
from skyvern.exceptions import AzureConfigurationError
from skyvern.forge import app
from skyvern.forge.sdk.api.files import (
    get_path_for_workflow_download_directory,
    resolve_local_or_download_file,
    resolve_run_download_id,
    validate_local_file_path,
)
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.schemas.files import FileInfo
from skyvern.forge.sdk.services import google_drive_service, google_oauth_service
from skyvern.forge.sdk.workflow.context_manager import WorkflowRunContext
from skyvern.forge.sdk.workflow.models.block_base import Block
from skyvern.forge.sdk.workflow.models.parameter import PARAMETER_TYPE
from skyvern.schemas.workflows import (
    BlockResult,
    BlockStatus,
    BlockType,
    FileStorageType,
    FileUploadDestination,
)

LOG = structlog.get_logger()


class DownloadToS3Block(Block):
    # There is a mypy bug with Literal. Without the type: ignore, mypy will raise an error:
    # Parameter 1 of Literal[...] cannot be of type "Any"
    block_type: Literal[BlockType.DOWNLOAD_TO_S3] = BlockType.DOWNLOAD_TO_S3  # type: ignore

    url: str

    def get_all_parameters(
        self,
        workflow_run_id: str,
    ) -> list[PARAMETER_TYPE]:
        workflow_run_context = self.get_workflow_run_context(workflow_run_id)

        if self.url and workflow_run_context.has_parameter(self.url):
            return [workflow_run_context.get_parameter(self.url)]

        return []

    def format_potential_template_parameters(self, workflow_run_context: WorkflowRunContext) -> None:
        self.url = self.format_block_parameter_template_from_workflow_run_context(self.url, workflow_run_context)

    async def _upload_file_to_s3(self, uri: str, file_path: str, cleanup_file: bool = True) -> None:
        try:
            client = self.get_async_aws_client()
            await client.upload_file_from_path(uri=uri, file_path=file_path)
        finally:
            # Clean up the temporary file since it's created with delete=False
            if cleanup_file:
                os.unlink(file_path)

    async def execute(
        self,
        workflow_run_id: str,
        workflow_run_block_id: str,
        organization_id: str | None = None,
        browser_session_id: str | None = None,
        **kwargs: dict,
    ) -> BlockResult:
        # get workflow run context
        workflow_run_context = self.get_workflow_run_context(workflow_run_id)
        # get all parameters into a dictionary
        if self.url and workflow_run_context.has_parameter(self.url) and workflow_run_context.has_value(self.url):
            task_url_parameter_value = workflow_run_context.get_value(self.url)
            if task_url_parameter_value:
                LOG.info(
                    "DownloadToS3Block Task URL is parameterized, using parameter value",
                    task_url_parameter_value=task_url_parameter_value,
                    task_url_parameter_key=self.url,
                )
                self.url = task_url_parameter_value

        try:
            self.format_potential_template_parameters(workflow_run_context)
        except Exception as e:
            return await self.build_block_result(
                success=False,
                failure_reason=f"Failed to format jinja template: {str(e)}",
                output_parameter_value=None,
                status=BlockStatus.failed,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )

        try:
            context = skyvern_context.current()
            run_id = context.run_id if context and context.run_id else workflow_run_id
            file_path = await resolve_local_or_download_file(
                self.url, run_id, organization_id=organization_id, max_size_mb=10
            )
        except Exception as e:
            LOG.error("DownloadToS3Block Failed to download file", url=self.url, error=str(e))
            raise e

        uri = None
        try:
            uri = f"s3://{settings.AWS_S3_BUCKET_UPLOADS}/{settings.ENV}/{workflow_run_id}/{uuid.uuid4()}"
            await self._upload_file_to_s3(uri, file_path, cleanup_file=not self.url.startswith("/"))
        except Exception as e:
            LOG.error("DownloadToS3Block Failed to upload file to S3", uri=uri, error=str(e))
            raise e

        LOG.info("DownloadToS3Block File downloaded and uploaded to S3", uri=uri)
        await self.record_output_parameter_value(workflow_run_context, workflow_run_id, uri)
        return await self.build_block_result(
            success=True,
            failure_reason=None,
            output_parameter_value=uri,
            status=BlockStatus.completed,
            workflow_run_block_id=workflow_run_block_id,
            organization_id=organization_id,
        )


class UploadToS3Block(Block):
    # There is a mypy bug with Literal. Without the type: ignore, mypy will raise an error:
    # Parameter 1 of Literal[...] cannot be of type "Any"
    block_type: Literal[BlockType.UPLOAD_TO_S3] = BlockType.UPLOAD_TO_S3  # type: ignore

    # TODO (kerem): A directory upload is supported but we should also support a list of files
    path: str | None = None

    def get_all_parameters(
        self,
        workflow_run_id: str,
    ) -> list[PARAMETER_TYPE]:
        workflow_run_context = self.get_workflow_run_context(workflow_run_id)

        if self.path and workflow_run_context.has_parameter(self.path):
            return [workflow_run_context.get_parameter(self.path)]

        return []

    def format_potential_template_parameters(self, workflow_run_context: WorkflowRunContext) -> None:
        if self.path:
            self.path = self.format_block_parameter_template_from_workflow_run_context(self.path, workflow_run_context)

    @staticmethod
    def _get_s3_uri(workflow_run_id: str, path: str) -> str:
        s3_bucket = settings.AWS_S3_BUCKET_UPLOADS
        s3_key = f"{settings.ENV}/{workflow_run_id}/{uuid.uuid4()}_{Path(path).name}"
        return f"s3://{s3_bucket}/{s3_key}"

    async def execute(
        self,
        workflow_run_id: str,
        workflow_run_block_id: str,
        organization_id: str | None = None,
        browser_session_id: str | None = None,
        **kwargs: dict,
    ) -> BlockResult:
        # get workflow run context
        workflow_run_context = self.get_workflow_run_context(workflow_run_id)
        # get all parameters into a dictionary
        if self.path and workflow_run_context.has_parameter(self.path) and workflow_run_context.has_value(self.path):
            file_path_parameter_value = workflow_run_context.get_value(self.path)
            if file_path_parameter_value:
                LOG.info(
                    "UploadToS3Block File path is parameterized, using parameter value",
                    file_path_parameter_value=file_path_parameter_value,
                    file_path_parameter_key=self.path,
                )
                self.path = file_path_parameter_value
        # if the path is WORKFLOW_DOWNLOAD_DIRECTORY_PARAMETER_KEY, use the download directory for the workflow run
        elif self.path == settings.WORKFLOW_DOWNLOAD_DIRECTORY_PARAMETER_KEY:
            context = skyvern_context.current()
            self.path = str(
                get_path_for_workflow_download_directory(
                    resolve_run_download_id(context, fallback_run_id=workflow_run_id)
                ).absolute()
            )

        try:
            self.format_potential_template_parameters(workflow_run_context)
        except Exception as e:
            return await self.build_block_result(
                success=False,
                failure_reason=f"Failed to format jinja template: {str(e)}",
                output_parameter_value=None,
                status=BlockStatus.failed,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )

        if not self.path:
            raise ValueError("UploadToS3Block path is required")

        context = skyvern_context.current()
        run_id = context.run_id if context and context.run_id else workflow_run_id
        resolved_path = validate_local_file_path(self.path, run_id)

        if not os.path.exists(resolved_path):
            raise FileNotFoundError(f"UploadToS3Block File not found at path: {resolved_path}")

        s3_uris = []
        try:
            client = self.get_async_aws_client()
            # is the file path a file or a directory?
            if os.path.isdir(resolved_path):
                # get all files in the directory, if there are more than 25 files, we will not upload them
                files = os.listdir(resolved_path)
                if len(files) > MAX_UPLOAD_FILE_COUNT:
                    raise ValueError("Too many files in the directory, not uploading")
                for file in files:
                    # if the file is a directory, we will not upload it
                    if os.path.isdir(os.path.join(resolved_path, file)):
                        LOG.warning("UploadToS3Block Skipping directory", file=file)
                        continue
                    file_path = os.path.join(resolved_path, file)
                    s3_uri = self._get_s3_uri(workflow_run_id, file_path)
                    s3_uris.append(s3_uri)
                    await client.upload_file_from_path(uri=s3_uri, file_path=file_path)
            else:
                s3_uri = self._get_s3_uri(workflow_run_id, resolved_path)
                s3_uris.append(s3_uri)
                await client.upload_file_from_path(uri=s3_uri, file_path=resolved_path)
        except Exception as e:
            LOG.exception("UploadToS3Block Failed to upload file to S3", file_path=self.path)
            raise e

        LOG.info("UploadToS3Block File(s) uploaded to S3", file_path=self.path)
        await self.record_output_parameter_value(workflow_run_context, workflow_run_id, s3_uris)
        return await self.build_block_result(
            success=True,
            failure_reason=None,
            output_parameter_value=s3_uris,
            status=BlockStatus.completed,
            workflow_run_block_id=workflow_run_block_id,
            organization_id=organization_id,
        )


class FileUploadBlock(Block):
    # There is a mypy bug with Literal. Without the type: ignore, mypy will raise an error:
    # Parameter 1 of Literal[...] cannot be of type "Any"
    block_type: Literal[BlockType.FILE_UPLOAD] = BlockType.FILE_UPLOAD  # type: ignore

    storage_type: FileStorageType = FileStorageType.S3
    s3_bucket: str | None = None
    aws_access_key_id: str | None = None
    aws_secret_access_key: str | None = None
    region_name: str | None = None
    azure_storage_account_name: str | None = None
    azure_storage_account_key: str | None = None
    azure_blob_container_name: str | None = None
    google_credential_id: str | None = None
    google_drive_folder_id: str | None = None
    path: str | None = None
    continue_on_empty: bool = Field(
        default=False,
        description=(
            "When the run download directory has no files, allow the empty upload only after confirming no registered, "
            "browser-session, or alternate candidate downloads exist (False, default). Set True to always allow an "
            "empty upload."
        ),
    )

    def get_all_parameters(
        self,
        workflow_run_id: str,
    ) -> list[PARAMETER_TYPE]:
        workflow_run_context = self.get_workflow_run_context(workflow_run_id)
        parameters = []

        if self.path and workflow_run_context.has_parameter(self.path):
            parameters.append(workflow_run_context.get_parameter(self.path))

        if self.s3_bucket and workflow_run_context.has_parameter(self.s3_bucket):
            parameters.append(workflow_run_context.get_parameter(self.s3_bucket))

        if self.aws_access_key_id and workflow_run_context.has_parameter(self.aws_access_key_id):
            parameters.append(workflow_run_context.get_parameter(self.aws_access_key_id))

        if self.aws_secret_access_key and workflow_run_context.has_parameter(self.aws_secret_access_key):
            parameters.append(workflow_run_context.get_parameter(self.aws_secret_access_key))

        if self.azure_storage_account_name and workflow_run_context.has_parameter(self.azure_storage_account_name):
            parameters.append(workflow_run_context.get_parameter(self.azure_storage_account_name))

        if self.azure_storage_account_key and workflow_run_context.has_parameter(self.azure_storage_account_key):
            parameters.append(workflow_run_context.get_parameter(self.azure_storage_account_key))

        if self.azure_blob_container_name and workflow_run_context.has_parameter(self.azure_blob_container_name):
            parameters.append(workflow_run_context.get_parameter(self.azure_blob_container_name))

        if self.google_credential_id and workflow_run_context.has_parameter(self.google_credential_id):
            parameters.append(workflow_run_context.get_parameter(self.google_credential_id))

        if self.google_drive_folder_id and workflow_run_context.has_parameter(self.google_drive_folder_id):
            parameters.append(workflow_run_context.get_parameter(self.google_drive_folder_id))

        return parameters

    def format_potential_template_parameters(self, workflow_run_context: WorkflowRunContext) -> None:
        if self.path:
            self.path = self.format_block_parameter_template_from_workflow_run_context(self.path, workflow_run_context)

        if self.s3_bucket:
            self.s3_bucket = self.format_block_parameter_template_from_workflow_run_context(
                self.s3_bucket, workflow_run_context
            )
        if self.aws_access_key_id:
            self.aws_access_key_id = self.format_block_parameter_template_from_workflow_run_context(
                self.aws_access_key_id, workflow_run_context
            )
        if self.aws_secret_access_key:
            self.aws_secret_access_key = self.format_block_parameter_template_from_workflow_run_context(
                self.aws_secret_access_key, workflow_run_context
            )
        if self.azure_storage_account_name:
            self.azure_storage_account_name = self.format_block_parameter_template_from_workflow_run_context(
                self.azure_storage_account_name, workflow_run_context
            )
        if self.azure_storage_account_key:
            self.azure_storage_account_key = self.format_block_parameter_template_from_workflow_run_context(
                self.azure_storage_account_key, workflow_run_context
            )
        if self.azure_blob_container_name:
            self.azure_blob_container_name = self.format_block_parameter_template_from_workflow_run_context(
                self.azure_blob_container_name, workflow_run_context
            )
        if self.google_credential_id:
            self.google_credential_id = self.format_block_parameter_template_from_workflow_run_context(
                self.google_credential_id, workflow_run_context
            )
        if self.google_drive_folder_id:
            self.google_drive_folder_id = self.format_block_parameter_template_from_workflow_run_context(
                self.google_drive_folder_id, workflow_run_context
            )

    def _get_s3_uri(self, workflow_run_id: str, path: str) -> str:
        folder_path = self.path or f"{workflow_run_id}"
        # Remove trailing slash from folder_path to avoid double slashes
        folder_path = folder_path.rstrip("/")
        # Remove any empty path segments to avoid double slashes
        folder_path = "/".join(segment for segment in folder_path.split("/") if segment)
        s3_suffix = f"{uuid.uuid4()}_{Path(path).name}"
        return f"s3://{self.s3_bucket}/{folder_path}/{s3_suffix}"

    def _get_azure_blob_name(self, workflow_run_id: str, file_path: str) -> str:
        blob_name = f"{uuid.uuid4()}_{Path(file_path).name}"
        folder_path = self.path or workflow_run_id
        # Remove trailing slash from folder_path to avoid double slashes
        folder_path = folder_path.rstrip("/")
        # Remove any empty path segments to avoid double slashes
        folder_path = "/".join(segment for segment in folder_path.split("/") if segment)
        return folder_path + "/" + blob_name

    def _get_azure_blob_uri(self, workflow_run_id: str, blob_name: str) -> str:
        return f"https://{self.azure_storage_account_name}.blob.core.windows.net/{self.azure_blob_container_name}/{blob_name}"

    def _build_s3_destination(
        self,
        workflow_run_id: str,
        file_path: str,
        aws_access_key_id: str | None,
        aws_secret_access_key: str | None,
    ) -> FileUploadDestination:
        s3_uri = self._get_s3_uri(workflow_run_id, file_path)
        # ``_get_s3_uri`` returns ``s3://{bucket}/{key}`` — split it back out for
        # the destination so the cloud override can compute a presigned URL.
        without_scheme = s3_uri[len("s3://") :]
        bucket, _, key = without_scheme.partition("/")
        return FileUploadDestination(
            storage_type=FileStorageType.S3,
            customer_uri=s3_uri,
            sdk_uri=s3_uri,
            s3_bucket=bucket,
            s3_key=key,
            aws_access_key_id=aws_access_key_id,
            aws_secret_access_key=aws_secret_access_key,
            aws_region_name=self.region_name,
        )

    def _build_azure_destination(
        self,
        workflow_run_id: str,
        file_path: str,
        azure_storage_account_name: str,
        azure_storage_account_key: str,
    ) -> FileUploadDestination:
        blob_name = self._get_azure_blob_name(workflow_run_id, file_path)
        customer_uri = self._get_azure_blob_uri(workflow_run_id, blob_name)
        sdk_uri = f"azure://{self.azure_blob_container_name or ''}/{blob_name}"
        return FileUploadDestination(
            storage_type=FileStorageType.AZURE,
            customer_uri=customer_uri,
            sdk_uri=sdk_uri,
            azure_storage_account_name=azure_storage_account_name,
            azure_storage_account_key=azure_storage_account_key,
            azure_blob_container_name=self.azure_blob_container_name,
            azure_blob_name=blob_name,
        )

    @staticmethod
    def _build_google_drive_destination(
        *,
        access_token: str,
        folder_id: str,
    ) -> FileUploadDestination:
        return FileUploadDestination(
            storage_type=FileStorageType.GOOGLE_DRIVE,
            customer_uri=f"https://drive.google.com/drive/folders/{folder_id}",
            sdk_uri=f"https://drive.google.com/drive/folders/{folder_id}",
            google_access_token=access_token,
            google_drive_folder_id=folder_id,
        )

    @staticmethod
    def _candidate_download_signal_run_ids(
        *,
        context: SkyvernContext | None,
        workflow_run_id: str,
        run_download_id: str | None,
    ) -> list[str]:
        candidate_run_ids: list[str] = []
        for candidate in (
            run_download_id,
            workflow_run_id,
            context.run_id if context else None,
            context.workflow_run_id if context else None,
        ):
            if candidate and candidate not in candidate_run_ids:
                candidate_run_ids.append(candidate)
        if not candidate_run_ids and context and context.task_id:
            candidate_run_ids.append(context.task_id)
        return candidate_run_ids

    def _get_files_to_upload_from_download_dir(
        self,
        *,
        download_files_path: str,
        max_file_count: int,
    ) -> list[str]:
        files_to_upload = []
        if os.path.isdir(download_files_path):
            files = os.listdir(download_files_path)
            if len(files) > max_file_count:
                raise ValueError(f"Too many files in the directory, not uploading. Max: {max_file_count}")
            for file in files:
                if os.path.isdir(os.path.join(download_files_path, file)):
                    LOG.warning("FileUploadBlock Skipping directory", file=file)
                    continue
                files_to_upload.append(os.path.join(download_files_path, file))
        return files_to_upload

    def _get_files_in_alternate_candidate_download_dirs(
        self,
        *,
        context: SkyvernContext | None,
        workflow_run_id: str,
        run_download_id: str | None,
        download_files_path: str,
        max_file_count: int,
    ) -> tuple[list[str] | None, str]:
        """Return alternate local files plus a failure-reason count label.

        None means the local evidence could not be trusted, so execute() fails closed rather than no-oping.
        """
        for candidate_run_id in self._candidate_download_signal_run_ids(
            context=context,
            workflow_run_id=workflow_run_id,
            run_download_id=run_download_id,
        ):
            candidate_download_files_path = str(get_path_for_workflow_download_directory(candidate_run_id).absolute())
            if candidate_download_files_path == download_files_path:
                continue
            try:
                alternate_files = self._get_files_to_upload_from_download_dir(
                    download_files_path=candidate_download_files_path,
                    max_file_count=max_file_count,
                )
            except ValueError:
                LOG.warning(
                    "FileUploadBlock found too many files in an alternate candidate download directory",
                    workflow_run_id=workflow_run_id,
                    candidate_run_id=candidate_run_id,
                    download_files_path=download_files_path,
                    candidate_download_files_path=candidate_download_files_path,
                    exc_info=True,
                )
                return None, "too_many"
            if alternate_files:
                LOG.warning(
                    "FileUploadBlock found files in an alternate candidate download directory",
                    workflow_run_id=workflow_run_id,
                    candidate_run_id=candidate_run_id,
                    download_files_path=download_files_path,
                    candidate_download_files_path=candidate_download_files_path,
                    file_count=len(alternate_files),
                )
                return alternate_files, str(len(alternate_files))
        return [], "0"

    async def _get_browser_session_downloaded_files_for_empty_scan(
        self,
        *,
        organization_id: str | None,
        workflow_run_id: str,
        workflow_run_block_id: str,
        browser_session_id: str | None,
    ) -> list[str] | None:
        if not browser_session_id:
            # No persistent-session namespace exists to inspect, so this signal cannot contain downloads.
            return []
        if not organization_id:
            LOG.warning(
                "FileUploadBlock cannot check browser-session downloads without organization_id",
                workflow_run_id=workflow_run_id,
                workflow_run_block_id=workflow_run_block_id,
                browser_session_id=browser_session_id,
            )
            return None

        try:
            async with asyncio.timeout(GET_DOWNLOADED_FILES_TIMEOUT):
                return await app.STORAGE.list_downloaded_files_in_browser_session(
                    organization_id=organization_id,
                    browser_session_id=browser_session_id,
                )
        except asyncio.TimeoutError:
            LOG.warning(
                "Timeout checking browser-session downloads for empty FileUploadBlock scan",
                workflow_run_id=workflow_run_id,
                workflow_run_block_id=workflow_run_block_id,
                browser_session_id=browser_session_id,
            )
            return None
        except Exception:
            LOG.warning(
                "Failed to check browser-session downloads for empty FileUploadBlock scan",
                workflow_run_id=workflow_run_id,
                workflow_run_block_id=workflow_run_block_id,
                browser_session_id=browser_session_id,
                exc_info=True,
            )
            return None

    async def _get_registered_downloaded_files_for_empty_scan(
        self,
        *,
        organization_id: str | None,
        workflow_run_id: str,
        workflow_run_block_id: str,
        run_download_id: str | None,
        context: SkyvernContext | None,
    ) -> list[FileInfo] | None:
        """Return registered downloads, or None when the signal is unknown.

        A timeout on any candidate stays unknown even if later candidates might be empty; an empty later lookup cannot
        prove that the timed-out candidate had no downloads, so the caller fails closed.
        """
        if not organization_id:
            LOG.warning(
                "FileUploadBlock cannot check registered downloads without organization_id",
                workflow_run_id=workflow_run_id,
                workflow_run_block_id=workflow_run_block_id,
            )
            return None

        registered_downloaded_files: list[FileInfo] = []
        for candidate_run_id in self._candidate_download_signal_run_ids(
            context=context,
            workflow_run_id=workflow_run_id,
            run_download_id=run_download_id,
        ):
            try:
                async with asyncio.timeout(GET_DOWNLOADED_FILES_TIMEOUT):
                    registered_downloaded_files.extend(
                        await app.STORAGE.get_downloaded_files(
                            organization_id=organization_id,
                            run_id=candidate_run_id,
                        )
                    )
                    if registered_downloaded_files:
                        return registered_downloaded_files
            except asyncio.TimeoutError:
                LOG.warning(
                    "Timeout checking registered downloads for empty FileUploadBlock scan",
                    workflow_run_id=workflow_run_id,
                    workflow_run_block_id=workflow_run_block_id,
                    candidate_run_id=candidate_run_id,
                )
                return None
            except Exception:
                LOG.warning(
                    "Failed to check registered downloads for empty FileUploadBlock scan",
                    workflow_run_id=workflow_run_id,
                    workflow_run_block_id=workflow_run_block_id,
                    candidate_run_id=candidate_run_id,
                    exc_info=True,
                )
                return None

        return registered_downloaded_files

    async def execute(
        self,
        workflow_run_id: str,
        workflow_run_block_id: str,
        organization_id: str | None = None,
        browser_session_id: str | None = None,
        **kwargs: dict,
    ) -> BlockResult:
        # get workflow run context
        workflow_run_context = self.get_workflow_run_context(workflow_run_id)
        # get all parameters into a dictionary
        # data validate before uploading
        missing_parameters = []
        if self.storage_type == FileStorageType.S3:
            if not self.s3_bucket:
                missing_parameters.append("s3_bucket")
            if not self.aws_access_key_id:
                missing_parameters.append("aws_access_key_id")
            if not self.aws_secret_access_key:
                missing_parameters.append("aws_secret_access_key")
        elif self.storage_type == FileStorageType.AZURE:
            if not self.azure_storage_account_name or self.azure_storage_account_name == "":
                missing_parameters.append("azure_storage_account_name")
            if not self.azure_storage_account_key or self.azure_storage_account_key == "":
                missing_parameters.append("azure_storage_account_key")
            if not self.azure_blob_container_name or self.azure_blob_container_name == "":
                missing_parameters.append("azure_blob_container_name")
        elif self.storage_type == FileStorageType.GOOGLE_DRIVE:
            if not self.google_credential_id:
                missing_parameters.append("google_credential_id")
            if not self.google_drive_folder_id:
                missing_parameters.append("google_drive_folder_id")
        else:
            return await self.build_block_result(
                success=False,
                failure_reason=f"Unsupported storage type: {self.storage_type}",
                output_parameter_value=None,
                status=BlockStatus.failed,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )

        if missing_parameters:
            return await self.build_block_result(
                success=False,
                failure_reason=f"Required block values are missing in the FileUploadBlock (label: {self.label}): {', '.join(missing_parameters)}",
                output_parameter_value=None,
                status=BlockStatus.failed,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )

        try:
            self.format_potential_template_parameters(workflow_run_context)
        except Exception as e:
            return await self.build_block_result(
                success=False,
                failure_reason=f"Failed to format jinja template: {str(e)}",
                output_parameter_value=None,
                status=BlockStatus.failed,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )

        context = skyvern_context.current()
        run_download_id = resolve_run_download_id(context, fallback_run_id=workflow_run_id)
        download_files_path = str(get_path_for_workflow_download_directory(run_download_id).absolute())

        uploaded_uris: list[str] = []
        try:
            workflow_run_context = self.get_workflow_run_context(workflow_run_id)
            files_to_upload = []
            max_file_count = (
                MAX_UPLOAD_FILE_COUNT
                if self.storage_type in {FileStorageType.S3, FileStorageType.GOOGLE_DRIVE}
                else AZURE_BLOB_STORAGE_MAX_UPLOAD_FILE_COUNT
            )
            files_to_upload = self._get_files_to_upload_from_download_dir(
                download_files_path=download_files_path,
                max_file_count=max_file_count,
            )

            if not files_to_upload and not self.continue_on_empty:
                (
                    (alternate_files, alternate_file_count),
                    browser_session_downloaded_files,
                    registered_downloaded_files,
                ) = await asyncio.gather(
                    asyncio.to_thread(
                        self._get_files_in_alternate_candidate_download_dirs,
                        context=context,
                        workflow_run_id=workflow_run_id,
                        run_download_id=run_download_id,
                        download_files_path=download_files_path,
                        max_file_count=max_file_count,
                    ),
                    self._get_browser_session_downloaded_files_for_empty_scan(
                        organization_id=organization_id or workflow_run_context.organization_id,
                        workflow_run_id=workflow_run_id,
                        workflow_run_block_id=workflow_run_block_id,
                        browser_session_id=browser_session_id or (context.browser_session_id if context else None),
                    ),
                    self._get_registered_downloaded_files_for_empty_scan(
                        organization_id=organization_id or workflow_run_context.organization_id,
                        workflow_run_id=workflow_run_id,
                        workflow_run_block_id=workflow_run_block_id,
                        run_download_id=run_download_id,
                        context=context,
                    ),
                )
                if (
                    registered_downloaded_files == []
                    and alternate_files == []
                    and browser_session_downloaded_files == []
                ):
                    LOG.info(
                        "FileUploadBlock empty scan has no registered downloads; treating as no-op",
                        workflow_run_id=workflow_run_id,
                        workflow_run_block_id=workflow_run_block_id,
                        download_files_path=download_files_path,
                        storage_type=self.storage_type,
                    )
                    await self.record_output_parameter_value(workflow_run_context, workflow_run_id, uploaded_uris)
                    return await self.build_block_result(
                        success=True,
                        failure_reason=None,
                        output_parameter_value=uploaded_uris,
                        status=BlockStatus.completed,
                        workflow_run_block_id=workflow_run_block_id,
                        organization_id=organization_id,
                    )

                registered_download_count = (
                    str(len(registered_downloaded_files)) if registered_downloaded_files is not None else "unknown"
                )
                browser_session_download_count = (
                    str(len(browser_session_downloaded_files))
                    if browser_session_downloaded_files is not None
                    else "unknown"
                )
                return await self.build_block_result(
                    success=False,
                    failure_reason=(
                        f"No files found to upload in the run download directory ({download_files_path}); "
                        f"registered_download_count={registered_download_count}; "
                        f"alternate_file_count={alternate_file_count}; "
                        f"browser_session_download_count={browser_session_download_count}; "
                        f"nothing was sent to {self.storage_type}."
                    ),
                    output_parameter_value=None,
                    status=BlockStatus.failed,
                    workflow_run_block_id=workflow_run_block_id,
                    organization_id=organization_id,
                )

            if self.storage_type == FileStorageType.S3:
                actual_aws_access_key_id = (
                    workflow_run_context.get_original_secret_value_or_none(self.aws_access_key_id)
                    or self.aws_access_key_id
                )
                actual_aws_secret_access_key = (
                    workflow_run_context.get_original_secret_value_or_none(self.aws_secret_access_key)
                    or self.aws_secret_access_key
                )
                for file_path in files_to_upload:
                    destination = self._build_s3_destination(
                        workflow_run_id=workflow_run_id,
                        file_path=file_path,
                        aws_access_key_id=actual_aws_access_key_id,
                        aws_secret_access_key=actual_aws_secret_access_key,
                    )
                    customer_uri = await app.AGENT_FUNCTION.upload_file_to_customer_storage(
                        file_path=file_path,
                        destination=destination,
                        organization_id=organization_id,
                        run_id=workflow_run_id,
                    )
                    uploaded_uris.append(customer_uri)
                LOG.info("FileUploadBlock File(s) uploaded to S3", file_path=self.path)
            elif self.storage_type == FileStorageType.AZURE:
                actual_azure_storage_account_name = (
                    workflow_run_context.get_original_secret_value_or_none(self.azure_storage_account_name)
                    or self.azure_storage_account_name
                )
                actual_azure_storage_account_key = (
                    workflow_run_context.get_original_secret_value_or_none(self.azure_storage_account_key)
                    or self.azure_storage_account_key
                )
                if actual_azure_storage_account_name is None or actual_azure_storage_account_key is None:
                    raise AzureConfigurationError("Azure Storage is not configured")

                for file_path in files_to_upload:
                    LOG.info("FileUploadBlock Uploading file to Azure Blob Storage", file_path=file_path)
                    destination = self._build_azure_destination(
                        workflow_run_id=workflow_run_id,
                        file_path=file_path,
                        azure_storage_account_name=actual_azure_storage_account_name,
                        azure_storage_account_key=actual_azure_storage_account_key,
                    )
                    customer_uri = await app.AGENT_FUNCTION.upload_file_to_customer_storage(
                        file_path=file_path,
                        destination=destination,
                        organization_id=organization_id,
                        run_id=workflow_run_id,
                    )
                    uploaded_uris.append(customer_uri)
                LOG.info("FileUploadBlock File(s) uploaded to Azure Blob Storage", file_path=self.path)
            elif self.storage_type == FileStorageType.GOOGLE_DRIVE:
                org_id = organization_id or workflow_run_context.organization_id
                if not org_id:
                    raise ValueError("organization_id is required for Google Drive uploads")
                google_credential_id = (
                    workflow_run_context.get_original_secret_value_or_none(self.google_credential_id)
                    or self.google_credential_id
                )
                if not google_credential_id:
                    raise ValueError("Google credential id is required")

                google_credentials = await app.AGENT_FUNCTION.get_google_workspace_credentials(
                    organization_id=org_id,
                    credential_id=google_credential_id,
                    required_scopes=list(google_oauth_service.GOOGLE_DRIVE_SCOPES),
                )
                if not google_credentials or not google_credentials.token:
                    raise ValueError("Google Drive credential is not connected or is missing required scopes")

                folder_id = google_drive_service.extract_folder_id(self.google_drive_folder_id or "")
                for file_path in files_to_upload:
                    LOG.info("FileUploadBlock Uploading file to Google Drive", file_path=file_path)
                    destination = self._build_google_drive_destination(
                        access_token=google_credentials.token,
                        folder_id=folder_id,
                    )
                    customer_uri = await app.AGENT_FUNCTION.upload_file_to_customer_storage(
                        file_path=file_path,
                        destination=destination,
                        organization_id=org_id,
                        run_id=workflow_run_id,
                    )
                    uploaded_uris.append(customer_uri)
                LOG.info("FileUploadBlock File(s) uploaded to Google Drive", file_path=self.path)
            else:
                # This case should ideally be caught by the initial validation
                raise ValueError(f"Unsupported storage type: {self.storage_type}")

        except Exception as e:
            LOG.exception("FileUploadBlock Failed to upload file", file_path=self.path, storage_type=self.storage_type)
            return await self.build_block_result(
                success=False,
                failure_reason=f"Failed to upload file to {self.storage_type}: {str(e)}",
                output_parameter_value=None,
                status=BlockStatus.failed,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )

        await self.record_output_parameter_value(workflow_run_context, workflow_run_id, uploaded_uris)
        return await self.build_block_result(
            success=True,
            failure_reason=None,
            output_parameter_value=uploaded_uris,
            status=BlockStatus.completed,
            workflow_run_block_id=workflow_run_block_id,
            organization_id=organization_id,
        )
