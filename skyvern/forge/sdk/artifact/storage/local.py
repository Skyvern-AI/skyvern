import os
import shutil
from datetime import datetime
from pathlib import Path

import structlog

from skyvern.config import settings
from skyvern.forge.sdk.api.files import (
    calculate_sha256_for_file,
    get_download_dir,
    get_skyvern_temp_dir,
    parse_uri_to_path,
)
from skyvern.forge.sdk.artifact.models import Artifact, ArtifactType, LogEntityType
from skyvern.forge.sdk.artifact.storage.base import FILE_EXTENTSION_MAP, BaseStorage
from skyvern.forge.sdk.models import Step
from skyvern.forge.sdk.schemas.ai_suggestions import AISuggestion
from skyvern.forge.sdk.schemas.files import FileInfo
from skyvern.forge.sdk.schemas.task_v2 import TaskV2, Thought
from skyvern.forge.sdk.schemas.workflow_runs import WorkflowRunBlock

LOG = structlog.get_logger()


class LocalStorage(BaseStorage):
    def __init__(self, artifact_path: str = settings.ARTIFACT_STORAGE_PATH) -> None:
        self.artifact_path = artifact_path

    def build_uri(self, artifact_id: str, step: Step, artifact_type: ArtifactType) -> str:
        file_ext = FILE_EXTENTSION_MAP[artifact_type]
        return f"file://{self.artifact_path}/{step.task_id}/{step.order:02d}_{step.retry_index}_{step.step_id}/{datetime.utcnow().isoformat()}_{artifact_id}_{artifact_type}.{file_ext}"

    async def retrieve_global_workflows(self) -> list[str]:
        file_path = Path(f"{self.artifact_path}/{settings.ENV}/global_workflows.txt")
        self._create_directories_if_not_exists(file_path)
        if not file_path.exists():
            return []
        try:
            with open(file_path, "r") as f:
                return [line.strip() for line in f.readlines() if line.strip()]
        except Exception:
            return []

    def build_log_uri(self, log_entity_type: LogEntityType, log_entity_id: str, artifact_type: ArtifactType) -> str:
        file_ext = FILE_EXTENTSION_MAP[artifact_type]
        return f"file://{self.artifact_path}/logs/{log_entity_type}/{log_entity_id}/{datetime.utcnow().isoformat()}_{artifact_type}.{file_ext}"

    def build_thought_uri(self, artifact_id: str, thought: Thought, artifact_type: ArtifactType) -> str:
        file_ext = FILE_EXTENTSION_MAP[artifact_type]
        return f"file://{self.artifact_path}/{settings.ENV}/tasks/{thought.observer_cruise_id}/{thought.observer_thought_id}/{datetime.utcnow().isoformat()}_{artifact_id}_{artifact_type}.{file_ext}"

    def build_task_v2_uri(self, artifact_id: str, task_v2: TaskV2, artifact_type: ArtifactType) -> str:
        file_ext = FILE_EXTENTSION_MAP[artifact_type]
        return f"file://{self.artifact_path}/{settings.ENV}/observers/{task_v2.observer_cruise_id}/{datetime.utcnow().isoformat()}_{artifact_id}_{artifact_type}.{file_ext}"

    def build_workflow_run_block_uri(
        self, artifact_id: str, workflow_run_block: WorkflowRunBlock, artifact_type: ArtifactType
    ) -> str:
        file_ext = FILE_EXTENTSION_MAP[artifact_type]
        return f"file://{self.artifact_path}/{settings.ENV}/workflow_runs/{workflow_run_block.workflow_run_id}/{workflow_run_block.workflow_run_block_id}/{datetime.utcnow().isoformat()}_{artifact_id}_{artifact_type}.{file_ext}"

    def build_ai_suggestion_uri(
        self, artifact_id: str, ai_suggestion: AISuggestion, artifact_type: ArtifactType
    ) -> str:
        file_ext = FILE_EXTENTSION_MAP[artifact_type]
        return f"file://{self.artifact_path}/{settings.ENV}/ai_suggestions/{ai_suggestion.ai_suggestion_id}/{datetime.utcnow().isoformat()}_{artifact_id}_{artifact_type}.{file_ext}"

    async def store_artifact(self, artifact: Artifact, data: bytes) -> None:
        file_path = None
        try:
            file_path = Path(parse_uri_to_path(artifact.uri))
            self._create_directories_if_not_exists(file_path)
            with open(file_path, "wb") as f:
                f.write(data)
        except Exception:
            LOG.exception(
                "Failed to store artifact locally.",
                file_path=file_path,
                artifact=artifact,
            )

    async def store_artifact_from_path(self, artifact: Artifact, path: str) -> None:
        file_path = None
        try:
            file_path = Path(parse_uri_to_path(artifact.uri))
            self._create_directories_if_not_exists(file_path)
            Path(path).replace(file_path)
        except Exception:
            LOG.exception(
                "Failed to store artifact locally.",
                file_path=file_path,
                artifact=artifact,
            )

    async def retrieve_artifact(self, artifact: Artifact) -> bytes | None:
        file_path = None
        try:
            file_path = parse_uri_to_path(artifact.uri)
            with open(file_path, "rb") as f:
                return f.read()
        except Exception:
            LOG.exception(
                "Failed to retrieve local artifact.",
                file_path=file_path,
                artifact=artifact,
            )
            return None

    async def get_share_link(self, artifact: Artifact) -> str:
        return artifact.uri

    async def get_share_links(self, artifacts: list[Artifact]) -> list[str]:
        return [artifact.uri for artifact in artifacts]

    async def save_streaming_file(self, organization_id: str, file_name: str) -> None:
        return

    async def get_streaming_file(self, organization_id: str, file_name: str, use_default: bool = True) -> bytes | None:
        file_path = Path(f"{get_skyvern_temp_dir()}/skyvern_screenshot.png")
        if not use_default:
            file_path = Path(f"{get_skyvern_temp_dir()}/{organization_id}/{file_name}")
        try:
            with open(file_path, "rb") as f:
                return f.read()
        except Exception:
            return None

    async def store_browser_session(self, organization_id: str, workflow_permanent_id: str, directory: str) -> None:
        stored_folder_path = Path(settings.BROWSER_SESSION_BASE_PATH) / organization_id / workflow_permanent_id
        if directory == str(stored_folder_path):
            return
        self._create_directories_if_not_exists(stored_folder_path)
        LOG.info(
            "Storing browser session locally",
            organization_id=organization_id,
            workflow_permanent_id=workflow_permanent_id,
            directory=directory,
            browser_session_path=stored_folder_path,
        )

        # Copy all files from the directory to the stored folder
        for root, _, files in os.walk(directory):
            for file in files:
                source_file_path = Path(root) / file
                relative_path = source_file_path.relative_to(directory)
                target_file_path = stored_folder_path / relative_path
                self._create_directories_if_not_exists(target_file_path)
                shutil.copy2(source_file_path, target_file_path)

    async def retrieve_browser_session(self, organization_id: str, workflow_permanent_id: str) -> str | None:
        stored_folder_path = Path(settings.BROWSER_SESSION_BASE_PATH) / organization_id / workflow_permanent_id
        if not stored_folder_path.exists():
            return None
        return str(stored_folder_path)

    async def save_downloaded_files(
        self, organization_id: str, task_id: str | None, workflow_run_id: str | None
    ) -> None:
        pass

    async def get_downloaded_files(
        self, organization_id: str, task_id: str | None, workflow_run_id: str | None
    ) -> list[FileInfo]:
        download_dir = get_download_dir(workflow_run_id=workflow_run_id, task_id=task_id)
        file_infos: list[FileInfo] = []
        files_and_folders = os.listdir(download_dir)
        for file_or_folder in files_and_folders:
            path = os.path.join(download_dir, file_or_folder)
            if os.path.isfile(path):
                # Calculate checksum for the file
                checksum = calculate_sha256_for_file(path)
                file_info = FileInfo(url=f"file://{path}", checksum=checksum, filename=file_or_folder)
                file_infos.append(file_info)
        return file_infos

    @staticmethod
    def _create_directories_if_not_exists(path_including_file_name: Path) -> None:
        path = path_including_file_name.parent
        path.mkdir(parents=True, exist_ok=True)
